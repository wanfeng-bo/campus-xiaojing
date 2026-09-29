from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Annotated, Never, Protocol
from uuid import UUID

from anyio import create_task_group, to_thread
from fastapi import APIRouter, Depends, Header, status
from fastapi.responses import StreamingResponse
from starlette.types import Receive, Scope, Send

from app.interfaces.http.assistant_schemas import (
    AssistantActiveRunResponse,
    AssistantControlResponse,
    AssistantConversationResponse,
    AssistantDisplayTurnResponse,
    AssistantPathRef,
    AssistantStopRequest,
    AssistantTurnErrorEvent,
    AssistantTurnRequest,
    AssistantTurnSnapshotEvent,
)
from app.interfaces.http.errors import ApiError, HttpErrorResponse
from app.interfaces.http.framework_request import (
    FrameworkRequestContext,
    FrameworkRequestPresence,
    get_framework_request_context,
    get_framework_request_presence,
)
from app.platform.assistant.contracts import JsonObject
from app.platform.assistant.controls import (
    AssistantControlError,
    AssistantControlService,
    ControlKind,
    ControlModuleError,
    ControlRejectedError,
    ControlRuntimeHooks,
    ControlStoreUnavailableError,
)
from app.platform.assistant.diagnostics import request_trace
from app.platform.assistant.runtime import (
    AcceptedRun,
    AssistantRuntime,
    AssistantRuntimeError,
    AssistantServiceError,
    ConversationAccessError,
    ConversationBusyError,
    ConversationClosedError,
    ConversationSnapshot,
    InvalidRuntimeInputError,
    RunSettlement,
    RuntimeInvariantError,
    StaleRunError,
)


@dataclass(frozen=True, slots=True)
class AssistantRequestIdentity:
    """由服务端身份组合层提供；HTTP 正文和 header 均不能构造。"""

    actor_context_ref: str


class AssistantTurnRunner(Protocol):
    def run_accepted(
        self,
        *,
        runtime: AssistantRuntime,
        accepted: AcceptedRun,
        actor_context_ref: str,
        request_id: UUID,
    ) -> ConversationSnapshot: ...


type IdentityDependency = Callable[..., AssistantRequestIdentity]


def _require_empty_request(presence: FrameworkRequestPresence) -> None:
    if presence.has_query_parameters or presence.has_body:
        raise ApiError(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            code="assistant.request_invalid",
            message="这个内核入口不接受附加请求内容。",
        )


def _raise_http_error(exc: AssistantRuntimeError) -> Never:
    if isinstance(exc, InvalidRuntimeInputError):
        raise ApiError(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            code="assistant.request_invalid",
            message="请求内容不符合内核接口契约。",
        ) from None
    if isinstance(exc, ConversationAccessError):
        raise ApiError(
            status_code=status.HTTP_404_NOT_FOUND,
            code="assistant.conversation_unavailable",
            message="对话不存在或当前身份不能访问。",
        ) from None
    if isinstance(exc, ConversationBusyError):
        raise ApiError(
            status_code=status.HTTP_409_CONFLICT,
            code="assistant.conversation_busy",
            message="这段对话正在处理上一句话，请稍后再发送。",
        ) from None
    if isinstance(exc, ConversationClosedError):
        raise ApiError(
            status_code=status.HTTP_409_CONFLICT,
            code="assistant.conversation_closed",
            message="这段对话已结束，请在新对话中发送。",
        ) from None
    if isinstance(exc, StaleRunError):
        raise ApiError(
            status_code=status.HTTP_409_CONFLICT,
            code="assistant.run_stale",
            message="这次处理已经结束或失效，请刷新对话。",
        ) from None
    if isinstance(exc, RuntimeInvariantError):
        raise ApiError(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            code="assistant.runtime_unavailable",
            message="小静暂时不可用，请稍后重试。",
        ) from None
    raise ApiError(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        code="assistant.runtime_unavailable",
        message="小静暂时不可用，请稍后重试。",
    ) from None


def _raise_control_http_error(exc: AssistantControlError) -> Never:
    if isinstance(exc, ControlRejectedError):
        raise ApiError(
            status_code=status.HTTP_409_CONFLICT,
            code="assistant.control_unavailable",
            message="这个操作已经失效，请刷新对话。",
        ) from None
    if isinstance(exc, ControlStoreUnavailableError):
        raise ApiError(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            code="assistant.control_store_unavailable",
            message="点击操作暂时不可用，请稍后再试。",
        ) from None
    if isinstance(exc, ControlModuleError):
        raise ApiError(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            code="assistant.control_execution_unavailable",
            message="现在暂时没能完成这次操作，请稍后核对结果。",
        ) from None
    raise ApiError(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        code="assistant.control_execution_unavailable",
        message="现在暂时没能完成这次操作，请稍后核对结果。",
    ) from None


def _service_failure_payload(exc: AssistantServiceError) -> JsonObject:
    messages = {
        "tool_limit": "本次处理达到运行次数上限，尚未完成回答。",
        "deadline": "本次处理超过时间限制，已停止继续调用。尚未取得完整回答。",
        "budget": "当前已批准的模型预算不足，已停止付费调用。",
        "context_limit": "本次对话资料超过处理容量，请新建对话或缩小查询范围。",
        "output_limit": "这次未形成完整回答，已停止本轮回答。",
        "data_policy": (
            "这条内容含不适合发送给模型的信息，请去掉敏感内容后再试；"
            "获准资料可在页面查看。"
        ),
        "dependency": "模型或依赖服务暂时未能提供有效结果，请稍后再试。",
    }
    return {"text": messages[exc.reason], "error_code": "assistant." + exc.reason}


def _settle_failed_run(
    *,
    runtime: AssistantRuntime,
    accepted: AcceptedRun,
    actor_context_ref: str,
    request_id: UUID,
) -> None:
    try:
        runtime.settle_run(
            accepted.conversation_ref,
            actor_context_ref,
            accepted.run_ref,
            accepted.generation,
            request_id,
            RunSettlement(
                display_payload={
                    "text": "小静暂时不可用，请稍后重试。",
                    "error_code": "assistant.runtime_unavailable",
                },
            ),
        )
    except StaleRunError:
        return
    except AssistantRuntimeError as exc:
        _raise_http_error(exc)


def _conversation_response(
    snapshot: ConversationSnapshot,
    *,
    only_run: AcceptedRun | None = None,
) -> AssistantConversationResponse:
    active = snapshot.active_run
    return AssistantConversationResponse(
        version="1",
        conversation_ref=snapshot.conversation_ref,
        generation=snapshot.generation,
        active_run=(
            None
            if active is None
            else AssistantActiveRunResponse(
                turn_ref=active.turn_ref,
                run_ref=active.run_ref,
                generation=active.generation,
                continuation_of=active.continuation_of,
            )
        ),
        turns=tuple(
            AssistantDisplayTurnResponse(
                turn_ref=entry.turn_ref,
                run_ref=entry.run_ref,
                generation=entry.generation,
                teacher_utterance=entry.teacher_utterance,
                visible_state=entry.visible_state,
                display_kind=entry.display_kind,
                display_payload=entry.display_payload,
                controls=tuple(
                    AssistantControlResponse(
                        kind=control.kind,
                        handle=control.handle,
                        safe_label=control.safe_label,
                    )
                    for control in entry.controls
                ),
                continuation_of=entry.continuation_of,
            )
            for entry in snapshot.display_ledger
            if only_run is None
            or (entry.run_ref == only_run.run_ref and entry.generation == only_run.generation)
        ),
    )


class _TurnStreamResponse(StreamingResponse):
    """Invalidate only this natural run even if delivery ends before iteration starts."""

    def __init__(self, content: AsyncIterator[str], stop: Callable[[], None]) -> None:
        super().__init__(
            content,
            media_type="application/x-ndjson",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )
        self._stop = stop

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        del scope
        try:
            # ASGI >= 2.4 only notices a disconnect on send by default. Listen
            # throughout the bounded worker call, including its silent wait.
            async with create_task_group() as group:

                async def stream() -> None:
                    await self.stream_response(send)
                    group.cancel_scope.cancel()

                group.start_soon(stream)
                await self.listen_for_disconnect(receive)
                group.cancel_scope.cancel()
        finally:
            self._stop()


def create_assistant_router(
    *,
    runtime: AssistantRuntime,
    identity_dependency: IdentityDependency,
    turn_orchestrator: AssistantTurnRunner,
    control_service: AssistantControlService | None = None,
    on_conversation_created: Callable[[str, str, str], None] | None = None,
    on_user_input: Callable[[str], None] | None = None,
) -> APIRouter:
    """创建未来正式内核路由；K8 前只允许测试组合显式挂接。"""

    router = APIRouter(
        prefix="/api/v1/assistant",
        tags=["assistant"],
        responses={
            404: {"model": HttpErrorResponse},
            409: {"model": HttpErrorResponse},
            422: {"model": HttpErrorResponse},
            503: {"model": HttpErrorResponse},
        },
    )

    @router.post(
        "/conversations",
        response_model=AssistantConversationResponse,
        status_code=status.HTTP_201_CREATED,
    )
    def _create_conversation(
        identity: Annotated[AssistantRequestIdentity, Depends(identity_dependency)],
        presence: Annotated[
            FrameworkRequestPresence,
            Depends(get_framework_request_presence),
        ],
        context_claim: Annotated[
            str | None,
            Header(alias="X-Conversation-Context-Claim", min_length=32, max_length=128),
        ] = None,
    ) -> AssistantConversationResponse:
        _require_empty_request(presence)
        try:
            snapshot = runtime.create_conversation(
                identity.actor_context_ref,
                presence.context.request_id,
            )
            # The core transports one opaque claim. Its meaning and verification
            # belong entirely to the application composition callback.
            if context_claim is not None and on_conversation_created is not None:
                on_conversation_created(
                    identity.actor_context_ref, snapshot.conversation_ref, context_claim
                )
        except AssistantRuntimeError as exc:
            _raise_http_error(exc)
        return _conversation_response(snapshot)

    @router.post(
        "/conversations/{conversation_ref}/turns",
        response_model=AssistantConversationResponse,
        responses={
            200: {
                "content": {
                    "application/x-ndjson": {
                        "schema": {
                            "type": "string",
                            "description": (
                                "Accept selects newline-delimited events: "
                                "AssistantTurnSnapshotEvent (accepted/current turn only, "
                                "then completed/full snapshot), or "
                                "AssistantTurnErrorEvent (error with safe code)."
                            ),
                        }
                    }
                }
            }
        },
    )
    async def _accept_turn(
        conversation_ref: AssistantPathRef,
        payload: AssistantTurnRequest,
        identity: Annotated[AssistantRequestIdentity, Depends(identity_dependency)],
        request_context: Annotated[
            FrameworkRequestContext,
            Depends(get_framework_request_context),
        ],
        accept: Annotated[str | None, Header()] = None,
    ) -> AssistantConversationResponse | StreamingResponse:
        try:
            if on_user_input is not None:
                on_user_input(payload.teacher_utterance)
            accepted = runtime.accept_turn(
                conversation_ref,
                identity.actor_context_ref,
                request_context.request_id,
                payload.teacher_utterance,
            )
        except AssistantServiceError as exc:
            failure = _service_failure_payload(exc)
            raise ApiError(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                code=str(failure["error_code"]),
                message=str(failure["text"]),
            ) from None
        except AssistantRuntimeError as exc:
            _raise_http_error(exc)
        return await _run_turn_response(accepted, identity, request_context, accept)

    @router.post(
        "/conversations/{conversation_ref}/continuations/{handle}",
        response_model=AssistantConversationResponse,
    )
    async def _continue_result(
        conversation_ref: AssistantPathRef,
        handle: AssistantPathRef,
        identity: Annotated[AssistantRequestIdentity, Depends(identity_dependency)],
        presence: Annotated[FrameworkRequestPresence, Depends(get_framework_request_presence)],
        accept: Annotated[str | None, Header()] = None,
    ) -> AssistantConversationResponse | StreamingResponse:
        _require_empty_request(presence)
        try:
            loader = runtime.continuation_loader(
                conversation_ref, identity.actor_context_ref, handle
            )
            if loader is None:
                return _conversation_response(
                    runtime.snapshot(conversation_ref, identity.actor_context_ref)
                )
            # Server-owned read of the original result, not a status supplied by the browser.
            # Do not hold the conversation lock during domain/database work.
            evidence = await to_thread.run_sync(loader)
            accepted = runtime.accept_continuation(
                conversation_ref,
                identity.actor_context_ref,
                handle,
                presence.context.request_id,
                evidence=evidence,
            )
            if accepted is None:
                return _conversation_response(
                    runtime.snapshot(conversation_ref, identity.actor_context_ref)
                )
        except AssistantRuntimeError as exc:
            _raise_http_error(exc)
        except Exception:
            _raise_http_error(AssistantServiceError("execution result unavailable"))
        return await _run_turn_response(accepted, identity, presence.context, accept)

    async def _run_turn_response(
        accepted: AcceptedRun,
        identity: AssistantRequestIdentity,
        request_context: FrameworkRequestContext,
        accept: str | None,
    ) -> AssistantConversationResponse | StreamingResponse:
        conversation_ref = accepted.conversation_ref
        wants_stream = accept is not None and any(
            part.strip().lower() == "application/x-ndjson" for part in accept.split(",")
        )

        def execute() -> AssistantConversationResponse:
            try:
                with request_trace(str(request_context.request_id)):
                    snapshot = turn_orchestrator.run_accepted(
                        runtime=runtime,
                        accepted=accepted,
                        actor_context_ref=identity.actor_context_ref,
                        request_id=request_context.request_id,
                    )
            except AssistantServiceError as exc:
                try:
                    snapshot = runtime.settle_run(
                        accepted.conversation_ref,
                        identity.actor_context_ref,
                        accepted.run_ref,
                        accepted.generation,
                        request_context.request_id,
                        RunSettlement(display_payload=_service_failure_payload(exc)),
                    )
                except AssistantRuntimeError as settlement_error:
                    _raise_http_error(settlement_error)
                return _conversation_response(
                    snapshot,
                    only_run=accepted if wants_stream else None,
                )
            except AssistantRuntimeError as exc:
                _settle_failed_run(
                    runtime=runtime,
                    accepted=accepted,
                    actor_context_ref=identity.actor_context_ref,
                    request_id=request_context.request_id,
                )
                _raise_http_error(exc)
            return _conversation_response(snapshot)

        if not wants_stream:
            return await to_thread.run_sync(execute)

        def stop_original() -> None:
            try:
                runtime.stop_run(
                    accepted.conversation_ref,
                    identity.actor_context_ref,
                    accepted.run_ref,
                    accepted.generation,
                    request_context.request_id,
                )
            except AssistantRuntimeError:
                pass

        async def events() -> AsyncIterator[str]:
            try:
                snapshot = runtime.snapshot(conversation_ref, identity.actor_context_ref)
                runtime.require_current_run(
                    conversation_ref,
                    identity.actor_context_ref,
                    accepted.run_ref,
                    accepted.generation,
                )
                yield (
                    AssistantTurnSnapshotEvent(
                        event="accepted",
                        snapshot=_conversation_response(snapshot, only_run=accepted),
                    ).model_dump_json()
                    + "\n"
                )
                # Cancellation abandons only waiting: the worker's existing run guards
                # suppress all later model loops, settlements and controls.
                completed = await to_thread.run_sync(execute, abandon_on_cancel=True)
                yield (
                    AssistantTurnSnapshotEvent(
                        event="completed",
                        snapshot=completed,
                    ).model_dump_json()
                    + "\n"
                )
            except Exception as exc:
                if isinstance(exc, AssistantServiceError):
                    error = AssistantTurnErrorEvent.model_validate(
                        {
                            "code": "assistant." + exc.reason,
                        }
                    )
                else:
                    stale = isinstance(exc, StaleRunError) or (
                        isinstance(exc, ApiError) and exc.code == "assistant.run_stale"
                    )
                    error = AssistantTurnErrorEvent(
                        code="assistant.run_stale" if stale else "assistant.runtime_unavailable",
                    )
                yield error.model_dump_json() + "\n"
            finally:
                stop_original()

        return _TurnStreamResponse(events(), stop_original)

    @router.post(
        "/conversations/{conversation_ref}/close",
        response_model=AssistantConversationResponse,
    )
    def _close_conversation(
        conversation_ref: AssistantPathRef,
        identity: Annotated[AssistantRequestIdentity, Depends(identity_dependency)],
        presence: Annotated[FrameworkRequestPresence, Depends(get_framework_request_presence)],
    ) -> AssistantConversationResponse:
        _require_empty_request(presence)
        try:
            snapshot = runtime.close_conversation(
                conversation_ref,
                identity.actor_context_ref,
                presence.context.request_id,
            )
        except AssistantRuntimeError as exc:
            _raise_http_error(exc)
        return _conversation_response(snapshot)

    @router.get(
        "/conversations/{conversation_ref}",
        response_model=AssistantConversationResponse,
    )
    def _read_conversation(
        conversation_ref: AssistantPathRef,
        identity: Annotated[AssistantRequestIdentity, Depends(identity_dependency)],
        presence: Annotated[
            FrameworkRequestPresence,
            Depends(get_framework_request_presence),
        ],
    ) -> AssistantConversationResponse:
        _require_empty_request(presence)
        try:
            snapshot = runtime.snapshot(conversation_ref, identity.actor_context_ref)
        except AssistantRuntimeError as exc:
            _raise_http_error(exc)
        return _conversation_response(snapshot)

    @router.post(
        "/conversations/{conversation_ref}/runs/{run_ref}/stop",
        response_model=AssistantConversationResponse,
    )
    def _stop_run(
        conversation_ref: AssistantPathRef,
        run_ref: AssistantPathRef,
        payload: AssistantStopRequest,
        identity: Annotated[AssistantRequestIdentity, Depends(identity_dependency)],
        request_context: Annotated[
            FrameworkRequestContext,
            Depends(get_framework_request_context),
        ],
    ) -> AssistantConversationResponse:
        try:
            snapshot = runtime.stop_run(
                conversation_ref,
                identity.actor_context_ref,
                run_ref,
                payload.generation,
                request_context.request_id,
            )
        except AssistantRuntimeError as exc:
            _raise_http_error(exc)
        return _conversation_response(snapshot)

    def execute_control(
        *,
        expected_kind: ControlKind,
        conversation_ref: str,
        control_handle: str,
        actor_context_ref: str,
        click_request_id: UUID,
    ) -> AssistantConversationResponse:
        if control_service is None:
            raise ApiError(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                code="assistant.control_store_unavailable",
                message="点击操作暂时不可用，请稍后再试。",
            )
        try:
            execution = control_service.execute(
                handle=control_handle,
                expected_kind=expected_kind,
                actor_context_ref=actor_context_ref,
                conversation_ref=conversation_ref,
                click_request_id=click_request_id,
                runtime_hooks=ControlRuntimeHooks(
                    reserve=lambda binding, kind, request_id, expires_at: runtime.reserve_control(
                        conversation_ref=binding.conversation_ref,
                        actor_context_ref=binding.actor_context_ref,
                        turn_ref=binding.turn_ref,
                        run_ref=binding.run_ref,
                        generation=binding.generation,
                        module_ref=binding.module_ref,
                        module_state_ref=binding.module_state_ref,
                        state_revision=binding.state_revision,
                        issuance_request_id=binding.issuance_request_id,
                        click_request_id=request_id,
                        kind=kind,
                        expires_at=expires_at,
                    ),
                    retire_consumed=lambda reservation_ref: runtime.retire_consumed_control(
                        conversation_ref=conversation_ref,
                        actor_context_ref=actor_context_ref,
                        reservation_ref=reservation_ref,
                    ),
                    attach_reconciliation=lambda reservation_ref, control: (
                        runtime.attach_control_reconciliation(
                            conversation_ref=conversation_ref,
                            actor_context_ref=actor_context_ref,
                            reservation_ref=reservation_ref,
                            control=control,
                        )
                    ),
                    mark_invoked=lambda reservation_ref: runtime.mark_control_invoked(
                        conversation_ref=conversation_ref,
                        actor_context_ref=actor_context_ref,
                        reservation_ref=reservation_ref,
                    ),
                    release=lambda reservation_ref: runtime.release_control(
                        conversation_ref=conversation_ref,
                        actor_context_ref=actor_context_ref,
                        reservation_ref=reservation_ref,
                    ),
                ),
            )
            if execution.requires_reconciliation:
                snapshot = runtime.apply_control_reconciliation(
                    conversation_ref=conversation_ref,
                    actor_context_ref=actor_context_ref,
                    reservation_ref=execution.reservation_ref,
                )
                return _conversation_response(snapshot)
            try:
                presentation = control_service.prepare_presentation(execution)
            except Exception:
                snapshot = runtime.apply_control_reconciliation(
                    conversation_ref=conversation_ref,
                    actor_context_ref=actor_context_ref,
                    reservation_ref=execution.reservation_ref,
                )
                return _conversation_response(snapshot)
            source = execution.consumed_binding
            target = execution.binding
            try:
                snapshot = runtime.apply_control_result(
                    conversation_ref=source.conversation_ref,
                    actor_context_ref=source.actor_context_ref,
                    turn_ref=source.turn_ref,
                    run_ref=source.run_ref,
                    generation=source.generation,
                    previous_module_ref=source.module_ref,
                    previous_module_state_ref=source.module_state_ref,
                    previous_state_revision=source.state_revision,
                    reservation_ref=execution.reservation_ref,
                    next_module_state_ref=target.module_state_ref,
                    next_state_revision=target.state_revision,
                    display_kind=presentation.display_kind,
                    display_payload=presentation.display_payload,
                    controls=execution.controls,
                )
            except Exception:
                snapshot = runtime.apply_control_reconciliation(
                    conversation_ref=conversation_ref,
                    actor_context_ref=actor_context_ref,
                    reservation_ref=execution.reservation_ref,
                )
                return _conversation_response(snapshot)
            control_service.discard_reconciliation(execution)
        except AssistantControlError as exc:
            _raise_control_http_error(exc)
        except AssistantRuntimeError as exc:
            _raise_http_error(exc)
        return _conversation_response(snapshot)

    @router.post(
        "/conversations/{conversation_ref}/controls/action-offers/{control_handle}",
        response_model=AssistantConversationResponse,
    )
    def _confirm_action(
        conversation_ref: AssistantPathRef,
        control_handle: AssistantPathRef,
        identity: Annotated[AssistantRequestIdentity, Depends(identity_dependency)],
        presence: Annotated[
            FrameworkRequestPresence,
            Depends(get_framework_request_presence),
        ],
    ) -> AssistantConversationResponse:
        _require_empty_request(presence)
        return execute_control(
            expected_kind="action_offer",
            conversation_ref=conversation_ref,
            control_handle=control_handle,
            actor_context_ref=identity.actor_context_ref,
            click_request_id=presence.context.request_id,
        )

    @router.post(
        "/conversations/{conversation_ref}/controls/recovery-controls/{control_handle}",
        response_model=AssistantConversationResponse,
    )
    def _recover_state(
        conversation_ref: AssistantPathRef,
        control_handle: AssistantPathRef,
        identity: Annotated[AssistantRequestIdentity, Depends(identity_dependency)],
        presence: Annotated[
            FrameworkRequestPresence,
            Depends(get_framework_request_presence),
        ],
    ) -> AssistantConversationResponse:
        _require_empty_request(presence)
        return execute_control(
            expected_kind="recovery_control",
            conversation_ref=conversation_ref,
            control_handle=control_handle,
            actor_context_ref=identity.actor_context_ref,
            click_request_id=presence.context.request_id,
        )

    @router.post(
        "/conversations/{conversation_ref}/controls/result-checks/{control_handle}",
        response_model=AssistantConversationResponse,
    )
    def _check_result(
        conversation_ref: AssistantPathRef,
        control_handle: AssistantPathRef,
        identity: Annotated[AssistantRequestIdentity, Depends(identity_dependency)],
        presence: Annotated[
            FrameworkRequestPresence,
            Depends(get_framework_request_presence),
        ],
    ) -> AssistantConversationResponse:
        _require_empty_request(presence)
        return execute_control(
            expected_kind="result_check_handle",
            conversation_ref=conversation_ref,
            control_handle=control_handle,
            actor_context_ref=identity.actor_context_ref,
            click_request_id=presence.context.request_id,
        )

    assert all(
        callable(endpoint)
        for endpoint in (
            _create_conversation,
            _close_conversation,
            _accept_turn,
            _read_conversation,
            _stop_run,
            _confirm_action,
            _recover_state,
            _check_result,
        )
    )
    return router


__all__ = ["AssistantRequestIdentity", "create_assistant_router"]
