from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from threading import Event, Lock
from typing import Any, cast
from uuid import UUID

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient

from app.core.request_id import assign_request_id
from app.interfaces.http.assistant import AssistantRequestIdentity, create_assistant_router
from app.interfaces.http.errors import ApiError, HttpErrorResponse
from app.interfaces.http.framework_request import (
    FrameworkRequestContext,
    FrameworkRequestPresence,
    api_error_handler,
    get_framework_request_presence,
    request_validation_error_handler,
)
from app.platform.assistant.catalog import AssistantModuleCatalogEntry, StaticAssistantCatalog
from app.platform.assistant.contracts import (
    ActionOfferV1,
    CapsuleUnchangedV1,
    MeaningModelCorrelationV1,
    MeaningModelRequestV1,
    MeaningV1,
    ModuleCallV1,
    ModuleConfirmationCallV1,
    ModuleRecoveryCallV1,
    ModuleResultCheckCallV1,
    ModuleResultV1,
    RecoveryControlV1,
    ResultCheckHandleV1,
    ValidationReceiptV1,
)
from app.platform.assistant.controls import AssistantControlService
from app.platform.assistant.orchestrator import AssistantTurnOrchestrator
from app.platform.assistant.runtime import AssistantRuntime, RuntimeInvariantError


class _Refs:
    def __init__(self) -> None:
        self._counts: dict[str, int] = defaultdict(int)

    def __call__(self, kind: str) -> str:
        self._counts[kind] += 1
        return f"{kind}-{self._counts[kind]}"


class _Store:
    def __init__(self) -> None:
        self.records: dict[str, str] = {}
        self.scopes: dict[str, str] = {}
        self.lock = Lock()
        self.fail_get = False
        self.fail_put = False
        self.block_atomic_consume = False
        self.consume_entered = Event()
        self.release_consume = Event()
        self.release_consume.set()
        self.fail_atomic_once = False

    def put(
        self,
        *,
        credential_ref: str,
        payload: str,
        ttl_seconds: int,
    ) -> bool:
        del ttl_seconds
        if self.fail_put:
            raise RuntimeError("POST-CALL-STORE-FAILURE")
        with self.lock:
            if credential_ref in self.records:
                return False
            self.records[credential_ref] = payload
            return True

    def get(self, *, credential_ref: str) -> str | None:
        if self.fail_get:
            raise RuntimeError("REDIS-FAILURE-PRIVATE-CANARY")
        with self.lock:
            return self.records.get(credential_ref)

    def consume(self, *, credential_ref: str, expected_payload: str, expires_at: int) -> bool:
        del expires_at
        with self.lock:
            if self.records.get(credential_ref) != expected_payload:
                return False
            del self.records[credential_ref]
            return True

    def consume_and_put(
        self,
        *,
        credential_ref: str,
        expected_payload: str,
        expires_at: int,
        replacement_credential_ref: str,
        replacement_payload: str,
        ttl_seconds: int,
    ) -> bool:
        del expires_at, ttl_seconds
        if self.block_atomic_consume:
            self.consume_entered.set()
            if not self.release_consume.wait(5):
                raise RuntimeError("atomic consume barrier timed out")
        if self.fail_atomic_once:
            self.fail_atomic_once = False
            raise RuntimeError("ATOMIC-CONSUME-FAILURE")
        with self.lock:
            if (
                self.records.get(credential_ref) != expected_payload
                or replacement_credential_ref in self.records
            ):
                return False
            self.records[replacement_credential_ref] = replacement_payload
            del self.records[credential_ref]
            return True

    def discard(self, *, credential_ref: str, expected_payload: str) -> bool:
        with self.lock:
            if self.records.get(credential_ref) != expected_payload:
                return False
            del self.records[credential_ref]
            return True


class _Meaning:
    def __init__(self) -> None:
        self.requests: list[MeaningModelRequestV1] = []

    def interpret(
        self,
        request: MeaningModelRequestV1,
        *,
        correlation: MeaningModelCorrelationV1,
    ) -> MeaningV1:
        self.requests.append(request)
        return MeaningV1(
            version="1",
            conversation_ref=correlation.conversation_ref,
            turn_ref=correlation.turn_ref,
            run_ref=correlation.run_ref,
            mode="dispatch",
            module_ref="fixture.control",
            meaning_payload={},
            evidence_refs=(),
            candidate_steps=(),
            precomposition_candidate=None,
            model_safe_expression_input=None,
        )


def _result(
    *,
    text: str,
    state_ref: str,
    revision: int,
    action_offer: ActionOfferV1 | None = None,
    recovery_control: RecoveryControlV1 | None = None,
    result_check_handle: ResultCheckHandleV1 | None = None,
) -> ModuleResultV1:
    return ModuleResultV1(
        version="1",
        validation_receipt=ValidationReceiptV1(
            status="accepted",
            safe_result_family="fixture.control",
        ),
        locked_notice=None,
        staged_state_ref=state_ref,
        state_revision=revision,
        capsule_update=CapsuleUnchangedV1(kind="unchanged"),
        presentation_mode="direct",
        display_kind="text",
        display_payload={"text": text},
        display_template=None,
        field_map=None,
        model_safe_narration=None,
        slot_map=None,
        fallback_presentation=None,
        model_safe_note="control-safe-note",
        action_offer=action_offer,
        recovery_control=recovery_control,
        result_check_handle=result_check_handle,
        safe_error_projection=None,
        diagnostic_ref=None,
    )


class _Module:
    def __init__(self, initial_kind: str, store: _Store, failure_mode: str | None = None) -> None:
        self.initial_kind = initial_kind
        self.store = store
        self.handle_calls: list[ModuleCallV1] = []
        self.confirm_calls: list[ModuleConfirmationCallV1] = []
        self.recover_calls: list[ModuleRecoveryCallV1] = []
        self.check_calls: list[ModuleResultCheckCallV1] = []
        self.failure_mode = failure_mode
        self.confirm_entered = Event()
        self.release_confirm = Event()
        self.release_confirm.set()
        self.check_entered = Event()
        self.release_check = Event()
        self.release_check.set()

    def handle(self, request: ModuleCallV1) -> ModuleResultV1:
        self.handle_calls.append(request)
        if self.initial_kind == "action_offer":
            return _result(
                text="待确认",
                state_ref="state-private-1",
                revision=1,
                action_offer=ActionOfferV1(
                    action_offer_ref="offer-private-canary",
                    safe_label="确认办理",
                ),
            )
        if self.initial_kind == "recovery_control":
            return _result(
                text="待确认",
                state_ref="state-private-1",
                revision=1,
                recovery_control=RecoveryControlV1(
                    recovery_control_ref="recovery-private-canary",
                    safe_label="放弃当前内容",
                ),
            )
        return _result(
            text="待确认",
            state_ref="state-private-1",
            revision=1,
            result_check_handle=ResultCheckHandleV1(result_check_ref="result-private-canary"),
        )

    def confirm(self, request: ModuleConfirmationCallV1) -> ModuleResultV1:
        self.confirm_calls.append(request)
        if self.failure_mode == "blocked_confirm":
            self.confirm_entered.set()
            if not self.release_confirm.wait(5):
                raise RuntimeError("confirm barrier timed out")
        if self.failure_mode == "module_exception":
            raise RuntimeError("POST-CALL-MODULE-FAILURE")
        if self.failure_mode == "post_call_store_failure":
            self.store.fail_put = True
            return _result(
                text="结果仍待核验",
                state_ref="state-private-2",
                revision=2,
                result_check_handle=ResultCheckHandleV1(
                    result_check_ref="module-result-private-canary"
                ),
            )
        if self.failure_mode == "result_unknown":
            return _result(
                text="结果仍待核验",
                state_ref="state-private-2",
                revision=2,
                result_check_handle=ResultCheckHandleV1(
                    result_check_ref="module-alternate-result-private-canary"
                ),
            )
        return _result(text="已确定办理结果", state_ref="state-private-2", revision=2)

    def recover(self, request: ModuleRecoveryCallV1) -> ModuleResultV1:
        self.recover_calls.append(request)
        return _result(text="已放弃", state_ref="state-private-2", revision=2)

    def check_result(self, request: ModuleResultCheckCallV1) -> ModuleResultV1:
        self.check_calls.append(request)
        if self.failure_mode == "blocked_check":
            self.check_entered.set()
            if not self.release_check.wait(5):
                raise RuntimeError("check barrier timed out")
            return _result(text="核验完成", state_ref="state-private-2", revision=2)
        return _result(
            text="结果仍待核验",
            state_ref="state-private-2",
            revision=2,
            result_check_handle=ResultCheckHandleV1(result_check_ref="result-private-next"),
        )


@contextmanager
def _client(
    *,
    initial_kind: str = "action_offer",
    actor: dict[str, str] | None = None,
    failure_mode: str | None = None,
    install_control_service: bool = True,
    fixed_presence_request_id: UUID | None = None,
    clock: Callable[[], datetime] | None = None,
) -> Iterator[tuple[TestClient, AssistantRuntime, _Meaning, _Module]]:
    class _FailOnceRuntime(AssistantRuntime):
        def __init__(self) -> None:
            super().__init__(clock=clock, ref_factory=_Refs())
            self.failed = False

        def apply_control_result(self, **kwargs: object):  # type: ignore[no-untyped-def]
            if not self.failed:
                self.failed = True
                raise RuntimeInvariantError("POST-CALL-RUNTIME-FAILURE")
            return super().apply_control_result(**kwargs)  # type: ignore[arg-type]

    runtime = (
        _FailOnceRuntime()
        if failure_mode == "runtime_failure"
        else AssistantRuntime(clock=clock, ref_factory=_Refs())
    )
    meaning = _Meaning()
    store = _Store()
    module = _Module(initial_kind, store, failure_mode)
    current_actor = actor or {"value": "actor-private-1"}
    catalog = StaticAssistantCatalog(
        (
            AssistantModuleCatalogEntry(
                module_ref="fixture.control",
                model_safe_capability="固定控制探针。",
                module=module,
            ),
        )
    )
    nonce_count = iter(range(16))
    service = AssistantControlService(
        catalog=catalog,
        store=store,
        signing_keys={"test": b"k" * 32},
        active_key_id="test",
        clock=clock,
        nonce_factory=lambda: f"{'a' * 63}{next(nonce_count):x}",
        confirmation_ref_factory=lambda: "confirmation-private-1",
    )
    if failure_mode == "presentation_failure":

        def fail_presentation(_execution: object) -> object:
            raise RuntimeError("POST-CALL-PRESENTATION-FAILURE")

        service.prepare_presentation = cast(Any, fail_presentation)
    orchestrator = AssistantTurnOrchestrator(
        meaning_model=meaning,
        catalog=catalog,
        control_service=service if install_control_service else None,
    )

    def identity() -> AssistantRequestIdentity:
        return AssistantRequestIdentity(actor_context_ref=current_actor["value"])

    app = FastAPI(responses={422: {"model": HttpErrorResponse}})
    app.middleware("http")(assign_request_id)
    app.add_exception_handler(ApiError, api_error_handler)
    app.add_exception_handler(RequestValidationError, request_validation_error_handler)
    if fixed_presence_request_id is not None:

        async def fixed_presence() -> FrameworkRequestPresence:
            return FrameworkRequestPresence(
                context=FrameworkRequestContext(request_id=fixed_presence_request_id),
                has_query_parameters=False,
                has_body=False,
            )

        app.dependency_overrides[get_framework_request_presence] = fixed_presence
    app.include_router(
        create_assistant_router(
            runtime=runtime,
            identity_dependency=identity,
            turn_orchestrator=orchestrator,
            control_service=service if install_control_service else None,
        )
    )
    with TestClient(app) as client:
        yield client, runtime, meaning, module


def test_public_action_handle_and_real_click_gate_use_handle_then_confirm_only() -> None:
    with _client() as (client, runtime, meaning, module):
        created = client.post("/api/v1/assistant/conversations")
        conversation_ref = cast(str, created.json()["conversation_ref"])
        offered = client.post(
            f"/api/v1/assistant/conversations/{conversation_ref}/turns",
            json={"teacher_utterance": "确认"},
        )
        assert offered.status_code == 200
        controls = offered.json()["turns"][0]["controls"]
        assert len(controls) == 1
        assert controls[0]["kind"] == "action_offer"
        assert controls[0]["safe_label"] == "确认办理"
        handle = cast(str, controls[0]["handle"])
        assert "offer-private-canary" not in offered.text
        assert "state-private-1" not in offered.text
        assert len(module.handle_calls) == 1
        assert module.confirm_calls == []
        assert len(meaning.requests) == 1
        assert handle not in meaning.requests[0].model_dump_json()

        clicked = client.post(
            f"/api/v1/assistant/conversations/{conversation_ref}/controls/action-offers/{handle}"
        )
        assert clicked.status_code == 200
        assert clicked.json()["turns"][0]["display_payload"] == {"text": "已确定办理结果"}
        assert clicked.json()["turns"][0]["controls"] == []
        assert len(module.handle_calls) == 1
        assert len(module.confirm_calls) == 1
        assert len(meaning.requests) == 1

        replay = client.post(
            f"/api/v1/assistant/conversations/{conversation_ref}/controls/action-offers/{handle}"
        )
        assert replay.status_code == 409
        assert replay.json()["code"] == "assistant.control_unavailable"
        assert handle not in replay.text
        assert "offer-private-canary" not in replay.text
        assert len(module.confirm_calls) == 1

        snapshot = runtime.snapshot(conversation_ref, "actor-private-1")
        assert handle not in repr(snapshot.model_safe_ledger)
        assert "offer-private-canary" not in repr(snapshot.model_safe_ledger)


def test_recovery_and_result_check_routes_call_only_their_formal_entries_without_models() -> None:
    route_by_kind = {
        "recovery_control": "recovery-controls",
        "result_check_handle": "result-checks",
    }
    for kind, route in route_by_kind.items():
        with _client(initial_kind=kind) as (client, _runtime, meaning, module):
            created = client.post("/api/v1/assistant/conversations")
            conversation_ref = cast(str, created.json()["conversation_ref"])
            offered = client.post(
                f"/api/v1/assistant/conversations/{conversation_ref}/turns",
                json={"teacher_utterance": "请处理这句"},
            )
            original = offered.json()["turns"][0]["controls"][0]
            assert original["kind"] == kind

            clicked = client.post(
                f"/api/v1/assistant/conversations/{conversation_ref}/controls/"
                f"{route}/{original['handle']}"
            )
            assert clicked.status_code == 200
            assert len(meaning.requests) == 1
            assert module.confirm_calls == []
            if kind == "recovery_control":
                assert len(module.recover_calls) == 1
                assert module.check_calls == []
                assert clicked.json()["turns"][0]["controls"] == []
            else:
                assert len(module.check_calls) == 1
                assert module.recover_calls == []
                refreshed = clicked.json()["turns"][0]["controls"]
                assert len(refreshed) == 1
                assert refreshed[0]["kind"] == "result_check_handle"
                assert refreshed[0]["handle"] != original["handle"]
                replay = client.post(
                    f"/api/v1/assistant/conversations/{conversation_ref}/controls/"
                    f"result-checks/{original['handle']}"
                )
                assert replay.status_code == 409
                assert len(module.check_calls) == 1
                assert module.confirm_calls == []


def test_control_routes_reject_body_query_cross_conversation_actor_and_stale_generation() -> None:
    actor = {"value": "actor-private-1"}
    with _client(actor=actor) as (client, _runtime, _meaning, module):
        first = client.post("/api/v1/assistant/conversations").json()["conversation_ref"]
        offered = client.post(
            f"/api/v1/assistant/conversations/{first}/turns",
            json={"teacher_utterance": "第一句"},
        )
        handle = offered.json()["turns"][0]["controls"][0]["handle"]
        route = f"/api/v1/assistant/conversations/{first}/controls/action-offers/{handle}"

        hidden_canary = "HIDDEN-CONTROL-CANARY"
        body = client.request(
            "POST",
            route,
            content=hidden_canary,
            headers={"Content-Type": "text/plain"},
        )
        query = client.post(f"{route}?actor_context_ref=forged&nonce=forged")
        duplicate_query = client.post(f"{route}?nonce=first&nonce=second")
        assert body.status_code == 422
        assert query.status_code == 422
        assert duplicate_query.status_code == 422
        assert hidden_canary not in body.text
        assert module.confirm_calls == []

        second = client.post("/api/v1/assistant/conversations").json()["conversation_ref"]
        cross_conversation = client.post(
            f"/api/v1/assistant/conversations/{second}/controls/action-offers/{handle}"
        )
        assert cross_conversation.status_code == 409
        assert module.confirm_calls == []

        actor["value"] = "actor-private-2"
        wrong_actor = client.post(route)
        assert wrong_actor.status_code == 409
        assert module.confirm_calls == []
        actor["value"] = "actor-private-1"

        newer = client.post(
            f"/api/v1/assistant/conversations/{first}/turns",
            json={"teacher_utterance": "第二句"},
        )
        assert newer.status_code == 200
        stale = client.post(route)
        assert stale.status_code == 409
        assert stale.json()["code"] == "assistant.control_unavailable"
        assert handle not in stale.text
        assert len(module.handle_calls) == 2
        assert module.confirm_calls == []


def test_store_unavailability_and_corruption_are_safe_503_with_zero_formal_calls() -> None:
    for failure_kind in ("unavailable", "corrupt"):
        with _client() as (client, _runtime, _meaning, module):
            conversation_ref = client.post("/api/v1/assistant/conversations").json()[
                "conversation_ref"
            ]
            offered = client.post(
                f"/api/v1/assistant/conversations/{conversation_ref}/turns",
                json={"teacher_utterance": "形成待确认动作"},
            )
            handle = offered.json()["turns"][0]["controls"][0]["handle"]
            store = cast(_Store, module.store)
            if failure_kind == "unavailable":
                store.fail_get = True
            else:
                record_key = next(iter(store.records))
                store.records[record_key] = '{"private_ref":"CORRUPT-PRIVATE-CANARY"}'

            failed = client.post(
                f"/api/v1/assistant/conversations/{conversation_ref}/controls/"
                f"action-offers/{handle}"
            )

            assert failed.status_code == 503
            assert failed.json()["code"] == "assistant.control_store_unavailable"
            assert handle not in failed.text
            assert "CORRUPT-PRIVATE-CANARY" not in failed.text
            assert "REDIS-FAILURE-PRIVATE-CANARY" not in failed.text
            assert module.confirm_calls == []


def test_every_post_call_failure_exposes_prebuilt_check_refreshes_and_never_reenters_confirm() -> (
    None
):
    for failure_mode in (
        "module_exception",
        "post_call_store_failure",
        "presentation_failure",
        "runtime_failure",
    ):
        with _client(failure_mode=failure_mode) as (client, _runtime, meaning, module):
            conversation_ref = client.post("/api/v1/assistant/conversations").json()[
                "conversation_ref"
            ]
            offered = client.post(
                f"/api/v1/assistant/conversations/{conversation_ref}/turns",
                json={"teacher_utterance": "形成正式动作"},
            )
            action = offered.json()["turns"][0]["controls"][0]

            uncertain = client.post(
                f"/api/v1/assistant/conversations/{conversation_ref}/controls/"
                f"action-offers/{action['handle']}"
            )

            assert uncertain.status_code == 200, failure_mode
            visible = uncertain.json()["turns"][0]
            assert visible["display_payload"] == {"text": "结果待核验，请点击核验结果。"}
            assert len(visible["controls"]) == 1
            check = visible["controls"][0]
            assert check["kind"] == "result_check_handle"
            assert len(module.confirm_calls) == 1
            assert len(meaning.requests) == 1

            refreshed = client.get(f"/api/v1/assistant/conversations/{conversation_ref}")
            assert refreshed.status_code == 200
            assert refreshed.json()["turns"][0]["controls"] == [check]

            checked = client.post(
                f"/api/v1/assistant/conversations/{conversation_ref}/controls/"
                f"result-checks/{check['handle']}"
            )
            assert checked.status_code == 200
            assert module.check_calls[-1].result_check_ref == "confirmation-private-1"
            assert len(module.confirm_calls) == 1


def test_action_result_unknown_exposes_prebuilt_confirmation_check_not_module_alternate() -> None:
    with _client(failure_mode="result_unknown") as (client, _runtime, meaning, module):
        conversation_ref = client.post("/api/v1/assistant/conversations").json()["conversation_ref"]
        offered = client.post(
            f"/api/v1/assistant/conversations/{conversation_ref}/turns",
            json={"teacher_utterance": "形成正式动作"},
        )
        action = offered.json()["turns"][0]["controls"][0]

        uncertain = client.post(
            f"/api/v1/assistant/conversations/{conversation_ref}/controls/"
            f"action-offers/{action['handle']}"
        )

        assert uncertain.status_code == 200
        visible = uncertain.json()["turns"][0]
        assert visible["display_payload"] == {"text": "结果待核验，请点击核验结果。"}
        assert len(visible["controls"]) == 1
        check = visible["controls"][0]
        assert check["kind"] == "result_check_handle"
        assert "module-alternate-result-private-canary" not in uncertain.text
        store = cast(_Store, module.store)
        assert "module-alternate-result-private-canary" not in repr(store.records)

        checked = client.post(
            f"/api/v1/assistant/conversations/{conversation_ref}/controls/"
            f"result-checks/{check['handle']}"
        )

        assert checked.status_code == 200
        assert module.check_calls[-1].result_check_ref == "confirmation-private-1"
        assert len(module.confirm_calls) == 1
        assert len(meaning.requests) == 1
        next_check = checked.json()["turns"][0]["controls"][0]
        assert next_check["kind"] == "result_check_handle"
        assert next_check["handle"] != check["handle"]


def test_atomic_consume_barrier_blocks_new_runs_and_double_clicks() -> None:
    with _client() as (client, _runtime, _meaning, module):
        conversation_ref = client.post("/api/v1/assistant/conversations").json()["conversation_ref"]
        offered = client.post(
            f"/api/v1/assistant/conversations/{conversation_ref}/turns",
            json={"teacher_utterance": "形成待确认动作"},
        )
        handle = offered.json()["turns"][0]["controls"][0]["handle"]
        route = (
            f"/api/v1/assistant/conversations/{conversation_ref}/controls/action-offers/{handle}"
        )
        store = cast(_Store, module.store)
        store.block_atomic_consume = True
        store.release_consume.clear()

        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(client.post, route)
            assert store.consume_entered.wait(2)
            newer = client.post(
                f"/api/v1/assistant/conversations/{conversation_ref}/turns",
                json={"teacher_utterance": "并发新一句"},
            )
            second = client.post(route)
            assert newer.status_code == 409
            assert second.status_code == 409
            assert module.confirm_calls == []
            store.release_consume.set()
            completed = first.result(timeout=3)

        assert completed.status_code == 200
        assert len(module.confirm_calls) == 1
        assert len(module.handle_calls) == 1


def test_inflight_hidden_check_expires_on_refresh_and_late_module_result_stays_manual() -> None:
    current = [datetime(2026, 8, 18, 1, 0, tzinfo=UTC)]

    def clock() -> datetime:
        return current[0]

    with _client(failure_mode="blocked_confirm", clock=clock) as (
        client,
        _runtime,
        meaning,
        module,
    ):
        conversation_ref = client.post("/api/v1/assistant/conversations").json()["conversation_ref"]
        offered = client.post(
            f"/api/v1/assistant/conversations/{conversation_ref}/turns",
            json={"teacher_utterance": "形成待确认动作"},
        )
        action = offered.json()["turns"][0]["controls"][0]
        route = (
            f"/api/v1/assistant/conversations/{conversation_ref}/controls/"
            f"action-offers/{action['handle']}"
        )
        module.release_confirm.clear()

        with ThreadPoolExecutor(max_workers=1) as executor:
            click = executor.submit(client.post, route)
            assert module.confirm_entered.wait(2)
            current[0] += timedelta(seconds=300)
            refreshed = client.get(f"/api/v1/assistant/conversations/{conversation_ref}")

            assert refreshed.status_code == 200
            visible = refreshed.json()["turns"][0]
            assert visible["controls"] == []
            assert visible["display_payload"] == {"text": "待人工核对"}
            assert len(module.confirm_calls) == 1
            assert len(meaning.requests) == 1

            again = client.get(f"/api/v1/assistant/conversations/{conversation_ref}")
            assert again.json()["turns"][0] == visible
            module.release_confirm.set()
            late = click.result(timeout=3)

        assert late.status_code == 200
        assert late.json()["turns"][0]["controls"] == []
        assert late.json()["turns"][0]["display_payload"] == {"text": "待人工核对"}
        assert len(module.confirm_calls) == 1


def test_consumed_near_expiry_source_check_retires_without_false_manual_review() -> None:
    current = [datetime(2026, 8, 18, 1, 0, tzinfo=UTC)]

    def clock() -> datetime:
        return current[0]

    with _client(
        initial_kind="result_check_handle",
        failure_mode="blocked_check",
        clock=clock,
    ) as (client, _runtime, meaning, module):
        conversation_ref = client.post("/api/v1/assistant/conversations").json()["conversation_ref"]
        offered = client.post(
            f"/api/v1/assistant/conversations/{conversation_ref}/turns",
            json={"teacher_utterance": "形成待核验结果"},
        )
        source = offered.json()["turns"][0]["controls"][0]
        current[0] += timedelta(seconds=299)
        route = (
            f"/api/v1/assistant/conversations/{conversation_ref}/controls/"
            f"result-checks/{source['handle']}"
        )
        module.release_check.clear()

        with ThreadPoolExecutor(max_workers=1) as executor:
            click = executor.submit(client.post, route)
            assert module.check_entered.wait(2)
            after_consume = client.get(f"/api/v1/assistant/conversations/{conversation_ref}")
            current[0] += timedelta(seconds=1)
            at_source_expiry = client.get(f"/api/v1/assistant/conversations/{conversation_ref}")
            module.release_check.set()
            definite = click.result(timeout=3)

        assert after_consume.json()["turns"][0]["controls"] == []
        assert at_source_expiry.json()["turns"][0]["controls"] == []
        assert at_source_expiry.json()["turns"][0]["display_payload"] == {"text": "待确认"}
        assert definite.json()["turns"][0]["display_payload"] == {"text": "核验完成"}
        assert len(module.check_calls) == 1
        assert module.confirm_calls == []
        assert len(meaning.requests) == 1


def test_framework_click_request_id_replay_is_rejected_and_safe_causal_entry_has_no_secret() -> (
    None
):
    click_request_id = UUID("00000000-0000-0000-0000-00000000c106")
    with _client(fixed_presence_request_id=click_request_id) as (
        client,
        runtime,
        _meaning,
        module,
    ):
        conversation_ref = client.post("/api/v1/assistant/conversations").json()["conversation_ref"]
        offered = client.post(
            f"/api/v1/assistant/conversations/{conversation_ref}/turns",
            json={"teacher_utterance": "形成待确认动作"},
        )
        handle = offered.json()["turns"][0]["controls"][0]["handle"]
        route = (
            f"/api/v1/assistant/conversations/{conversation_ref}/controls/action-offers/{handle}"
        )
        store = cast(_Store, module.store)
        store.fail_atomic_once = True

        failed = client.post(route)
        after_failed_consume = client.get(f"/api/v1/assistant/conversations/{conversation_ref}")
        replayed_request = client.post(route)

        assert failed.status_code == 503
        assert after_failed_consume.json()["turns"][0]["controls"] == [
            {
                "kind": "action_offer",
                "handle": handle,
                "safe_label": "确认办理",
            }
        ]
        assert replayed_request.status_code == 409
        assert module.confirm_calls == []
        causal = runtime.snapshot(conversation_ref, "actor-private-1").causal_ledger
        click_entries = [
            entry
            for entry in causal
            if entry.request_id == click_request_id and entry.state.startswith("control_")
        ]
        assert [entry.state for entry in click_entries] == ["control_active"]
        assert handle not in repr(click_entries)
        assert "offer-private-canary" not in repr(click_entries)
        assert "confirmation-private-1" not in repr(click_entries)


def test_control_bearing_module_result_fixed_fails_when_control_service_is_absent() -> None:
    with _client(install_control_service=False) as (client, runtime, _meaning, module):
        conversation_ref = client.post("/api/v1/assistant/conversations").json()["conversation_ref"]

        failed = client.post(
            f"/api/v1/assistant/conversations/{conversation_ref}/turns",
            json={"teacher_utterance": "形成待确认动作"},
        )

        assert failed.status_code == 503
        assert failed.json()["code"] == "assistant.runtime_unavailable"
        assert len(module.handle_calls) == 1
        snapshot = runtime.snapshot(conversation_ref, "actor-private-1")
        assert snapshot.display_ledger[0].controls == ()
        assert snapshot.display_ledger[0].display_payload == {
            "text": "小静暂时不可用，请稍后重试。",
            "error_code": "assistant.runtime_unavailable",
        }


def test_closed_conversation_blocks_new_formal_actions_but_retains_result_checks() -> None:
    for kind, route in (
        ("action_offer", "action-offers"),
        ("recovery_control", "recovery-controls"),
        ("result_check_handle", "result-checks"),
    ):
        with _client(initial_kind=kind) as (client, _runtime, meaning, module):
            ref = client.post("/api/v1/assistant/conversations").json()["conversation_ref"]
            base = f"/api/v1/assistant/conversations/{ref}"
            offered = client.post(base + "/turns", json={"teacher_utterance": "prepare"})
            handle = offered.json()["turns"][0]["controls"][0]["handle"]
            assert client.post(base + "/close").status_code == 200
            result = client.post(f"{base}/controls/{route}/{handle}")
            if kind == "result_check_handle":
                assert result.status_code == 200
                assert len(module.check_calls) == 1
            else:
                assert result.status_code == 409
                assert module.confirm_calls == []
                assert module.recover_calls == []
            assert len(meaning.requests) == 1
            assert client.get(base).status_code == 200
