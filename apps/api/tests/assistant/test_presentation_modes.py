from __future__ import annotations

from collections import defaultdict
from contextlib import contextmanager
from threading import Event, Thread
from typing import Any, Literal, cast

import pytest
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient

from app.core.request_id import assign_request_id
from app.interfaces.http.assistant import AssistantRequestIdentity, create_assistant_router
from app.interfaces.http.errors import ApiError, HttpErrorResponse
from app.interfaces.http.framework_request import (
    api_error_handler,
    request_validation_error_handler,
)
from app.platform.assistant.catalog import AssistantModuleCatalogEntry, StaticAssistantCatalog
from app.platform.assistant.contracts import (
    ActionOfferV1,
    CapsuleUnchangedV1,
    ExpressionModelRequestV1,
    ExpressionModelResultV1,
    FallbackPresentationV1,
    MeaningModelCorrelationV1,
    MeaningModelRequestV1,
    MeaningV1,
    ModuleCallV1,
    ModuleConfirmationCallV1,
    ModuleRecoveryCallV1,
    ModuleResultCheckCallV1,
    ModuleResultV1,
    PublicControlV1,
    RecoveryControlV1,
    ResultCheckHandleV1,
    ValidationReceiptV1,
)
from app.platform.assistant.controls import AssistantControlService, ControlIssueContext
from app.platform.assistant.orchestrator import AssistantTurnOrchestrator
from app.platform.assistant.runtime import AssistantRuntime


class _Refs:
    def __init__(self) -> None:
        self._counts: dict[str, int] = defaultdict(int)

    def __call__(self, kind: str) -> str:
        self._counts[kind] += 1
        return f"{kind}-{self._counts[kind]}"


class _MeaningAdapter:
    def __init__(
        self,
        module_ref: str = "fixture.presentation",
        *,
        mode: Literal["dispatch", "clarify", "none"] = "dispatch",
        expression_input: str | None = None,
    ) -> None:
        self.module_ref = module_ref
        self.mode: Literal["dispatch", "clarify", "none"] = mode
        self.expression_input = expression_input
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
            mode=self.mode,
            module_ref=self.module_ref if self.mode == "dispatch" else None,
            meaning_payload={},
            evidence_refs=(),
            candidate_steps=(),
            precomposition_candidate=None,
            model_safe_expression_input=(
                None if self.mode == "dispatch" else self.expression_input
            ),
        )


class _ExpressionAdapter:
    def __init__(
        self,
        *,
        rendered_template: str | None = None,
        error: Exception | None = None,
        entered: Event | None = None,
        release: Event | None = None,
    ) -> None:
        self.rendered_template = rendered_template
        self.error = error
        self.entered = entered
        self.release = release
        self.requests: list[ExpressionModelRequestV1] = []

    def compose(self, request: ExpressionModelRequestV1) -> ExpressionModelResultV1:
        self.requests.append(request)
        if self.entered is not None:
            self.entered.set()
        if self.release is not None and not self.release.wait(timeout=5):
            raise RuntimeError("expression release timed out")
        if self.error is not None:
            raise self.error
        if self.rendered_template is None:
            raise AssertionError("expression model must not be called")
        return ExpressionModelResultV1(
            version="1",
            rendered_template=self.rendered_template,
        )


class _Module:
    def __init__(self, result: ModuleResultV1) -> None:
        self.result = result
        self.calls: list[ModuleCallV1] = []

    def handle(self, request: ModuleCallV1) -> ModuleResultV1:
        self.calls.append(request)
        return self.result

    def confirm(self, request: ModuleConfirmationCallV1) -> ModuleResultV1:
        raise AssertionError(f"confirmation must not run: {request!r}")

    def recover(self, request: ModuleRecoveryCallV1) -> ModuleResultV1:
        raise AssertionError(f"recovery must not run: {request!r}")

    def check_result(self, request: ModuleResultCheckCallV1) -> ModuleResultV1:
        raise AssertionError(f"result check must not run: {request!r}")


class _ProjectionOnlyControlService:
    """K5 fixture supplies a non-null mechanical projection; K6 owns real persistence tests."""

    def issue_from_result(
        self,
        *,
        context: ControlIssueContext,
        result: ModuleResultV1,
    ) -> tuple[PublicControlV1, ...]:
        controls: list[PublicControlV1] = []
        if result.action_offer is not None:
            controls.append(
                PublicControlV1(
                    kind="action_offer",
                    handle=f"fixture-action-{context.generation}",
                    safe_label=result.action_offer.safe_label,
                )
            )
        if result.recovery_control is not None:
            controls.append(
                PublicControlV1(
                    kind="recovery_control",
                    handle=f"fixture-recovery-{context.generation}",
                    safe_label=result.recovery_control.safe_label,
                )
            )
        if result.result_check_handle is not None:
            controls.append(
                PublicControlV1(
                    kind="result_check_handle",
                    handle=f"fixture-check-{context.generation}",
                    safe_label="核验结果",
                )
            )
        return tuple(controls)


def _precomposed_result(
    *,
    template: str,
    field_map: dict[str, Any],
    display_kind: Literal["text", "card"] = "text",
) -> ModuleResultV1:
    return ModuleResultV1(
        version="1",
        validation_receipt=ValidationReceiptV1(
            status="accepted",
            safe_result_family="fixture.presentation",
        ),
        locked_notice=None,
        staged_state_ref="state-1",
        state_revision=1,
        capsule_update=CapsuleUnchangedV1(kind="unchanged"),
        presentation_mode="precomposed",
        display_kind=display_kind,
        display_payload=None,
        display_template=template,
        field_map=field_map,
        model_safe_narration=None,
        slot_map=None,
        fallback_presentation=FallbackPresentationV1(
            display_kind="card",
            display_payload={"type": "fallback-card", "safe": True},
        ),
        model_safe_note="safe-note-without-values",
        action_offer=None,
        recovery_control=None,
        result_check_handle=None,
        safe_error_projection=None,
        diagnostic_ref=None,
    )


def _compose_result(
    *,
    narration: str,
    slot_map: dict[str, Any],
    display_kind: Literal["text", "card"] = "text",
) -> ModuleResultV1:
    return ModuleResultV1(
        version="1",
        validation_receipt=ValidationReceiptV1(
            status="accepted",
            safe_result_family="fixture.presentation",
        ),
        locked_notice=None,
        staged_state_ref="state-1",
        state_revision=1,
        capsule_update=CapsuleUnchangedV1(kind="unchanged"),
        presentation_mode="compose",
        display_kind=display_kind,
        display_payload=None,
        display_template=None,
        field_map=None,
        model_safe_narration=narration,
        slot_map=slot_map,
        fallback_presentation=FallbackPresentationV1(
            display_kind="text",
            display_payload="安全确定性兜底",
        ),
        model_safe_note="safe-compose-note",
        action_offer=None,
        recovery_control=None,
        result_check_handle=None,
        safe_error_projection=None,
        diagnostic_ref=None,
    )


def _direct_result(
    *,
    display_kind: Literal["text", "card"],
    display_payload: Any,
) -> ModuleResultV1:
    return ModuleResultV1(
        version="1",
        validation_receipt=ValidationReceiptV1(
            status="accepted",
            safe_result_family="fixture.presentation",
        ),
        locked_notice=None,
        staged_state_ref="state-1",
        state_revision=1,
        capsule_update=CapsuleUnchangedV1(kind="unchanged"),
        presentation_mode="direct",
        display_kind=display_kind,
        display_payload=display_payload,
        display_template=None,
        field_map=None,
        model_safe_narration=None,
        slot_map=None,
        fallback_presentation=None,
        model_safe_note="safe-direct-note",
        action_offer=None,
        recovery_control=None,
        result_check_handle=None,
        safe_error_projection=None,
        diagnostic_ref=None,
    )


@contextmanager
def _client(
    result: ModuleResultV1,
    *,
    declared_fields: tuple[str, ...],
    presentation_capabilities: tuple[Literal["direct", "precomposed", "compose"], ...] = (
        "precomposed",
    ),
    expression: _ExpressionAdapter | None = None,
    meaning: _MeaningAdapter | None = None,
) -> Any:
    meaning_adapter = meaning or _MeaningAdapter()
    expression_adapter = expression or _ExpressionAdapter()
    module = _Module(result)
    catalog = StaticAssistantCatalog(
        (
            AssistantModuleCatalogEntry(
                module_ref="fixture.presentation",
                model_safe_capability="仅验证机械呈现。",
                module=module,
                model_visible_field_names=declared_fields,
                presentation_capabilities=presentation_capabilities,
            ),
        )
    )
    orchestrator = AssistantTurnOrchestrator(
        meaning_model=meaning_adapter,
        expression_model=expression_adapter,
        catalog=catalog,
        control_service=cast(AssistantControlService, _ProjectionOnlyControlService()),
    )
    runtime = AssistantRuntime(ref_factory=_Refs())

    def resolve_identity() -> AssistantRequestIdentity:
        return AssistantRequestIdentity(actor_context_ref="actor-context-canary")

    app = FastAPI(responses={422: {"model": HttpErrorResponse}})
    app.middleware("http")(assign_request_id)
    app.add_exception_handler(ApiError, api_error_handler)
    app.add_exception_handler(RequestValidationError, request_validation_error_handler)
    app.include_router(
        create_assistant_router(
            runtime=runtime,
            identity_dependency=resolve_identity,
            turn_orchestrator=orchestrator,
        )
    )
    with TestClient(app) as client:
        yield client, runtime, meaning_adapter, expression_adapter, module


def _complete_one(
    client: TestClient,
    *,
    teacher_utterance: str = "测试机械呈现",
) -> tuple[str, dict[str, Any]]:
    created = client.post("/api/v1/assistant/conversations")
    conversation_ref = cast(str, created.json()["conversation_ref"])
    response = client.post(
        f"/api/v1/assistant/conversations/{conversation_ref}/turns",
        json={"teacher_utterance": teacher_utterance},
    )
    return conversation_ref, cast(dict[str, Any], response.json()) | {
        "status_code": response.status_code
    }


@pytest.mark.parametrize(
    ("display_kind", "payload"),
    [
        ("text", "DIRECT-TEXT-SENSITIVE-CANARY"),
        ("card", {"private": "DIRECT-CARD-SENSITIVE-CANARY"}),
    ],
)
def test_direct_text_and_card_never_call_expression_or_enter_model_safe_storage(
    display_kind: Literal["text", "card"],
    payload: Any,
) -> None:
    expression = _ExpressionAdapter(rendered_template="must-not-run")
    result = _direct_result(display_kind=display_kind, display_payload=payload)

    with _client(
        result,
        declared_fields=(),
        presentation_capabilities=("direct",),
        expression=expression,
    ) as (client, runtime, _meaning, _expression, _module):
        conversation_ref, completed = _complete_one(client)
        snapshot = runtime.snapshot(conversation_ref, "actor-context-canary")

    assert completed["status_code"] == 200
    assert completed["turns"][0]["display_kind"] == display_kind
    assert completed["turns"][0]["display_payload"] == payload
    assert expression.requests == []
    assert "DIRECT-" not in repr(snapshot.model_safe_ledger)
    assert snapshot.model_safe_ledger[0].model_safe_template is None


@pytest.mark.parametrize(
    ("declared_fields", "template", "field_map", "expected"),
    [
        (
            ("first_field", "second_field"),
            "结果：{first_field}；详情：{second_field}。",
            {"first_field": "值{S1}不再解析", "second_field": {"b": 2, "a": 1}},
            '结果：值{S1}不再解析；详情：{"a":1,"b":2}。',
        ),
        (
            ("renamed_a", "renamed_b"),
            "结果：{renamed_a}；详情：{renamed_b}。",
            {"renamed_a": "值{S1}不再解析", "renamed_b": {"b": 2, "a": 1}},
            '结果：值{S1}不再解析；详情：{"a":1,"b":2}。',
        ),
        (
            ("renamed.group_a", "renamed.group_b"),
            "结果：{renamed.group_a}；详情：{renamed.group_b}。",
            {
                "renamed.group_a": "值{S1}不再解析",
                "renamed.group_b": {"b": 2, "a": 1},
            },
            '结果：值{S1}不再解析；详情：{"a":1,"b":2}。',
        ),
    ],
)
def test_valid_precomposed_fills_locally_and_renamed_handles_are_identical(
    declared_fields: tuple[str, ...],
    template: str,
    field_map: dict[str, Any],
    expected: str,
) -> None:
    result = _precomposed_result(template=template, field_map=field_map)

    with _client(result, declared_fields=declared_fields) as (
        client,
        runtime,
        _meaning,
        expression,
        _module,
    ):
        conversation_ref, completed = _complete_one(client)
        snapshot = runtime.snapshot(conversation_ref, "actor-context-canary")

    assert completed["status_code"] == 200
    assert completed["turns"][0]["display_kind"] == "text"
    assert completed["turns"][0]["display_payload"] == expected
    assert expression.requests == []
    assert snapshot.model_safe_ledger[0].model_safe_template == template
    assert "值{S1}不再解析" not in repr(snapshot.model_safe_ledger)


@pytest.mark.parametrize(
    ("template", "field_map", "declared_fields"),
    [
        ("{first}", {"first": "one", "second": "two"}, ("first", "second")),
        ("{first}{extra}", {"first": "one"}, ("first",)),
        ("{first}{first}", {"first": "one"}, ("first",)),
        ("{undeclared}", {"undeclared": "one"}, ("first",)),
        ("{first!r}", {"first": "one"}, ("first",)),
        ("{first:>10}", {"first": "one"}, ("first",)),
        ("{first[0]}", {"first": "one"}, ("first",)),
        ("{{first}}", {"first": "one"}, ("first",)),
        ("{first", {"first": "one"}, ("first",)),
        ("first}", {"first": "one"}, ("first",)),
    ],
)
def test_invalid_precomposed_placeholder_candidates_use_declared_fallback(
    template: str,
    field_map: dict[str, Any],
    declared_fields: tuple[str, ...],
) -> None:
    result = _precomposed_result(template=template, field_map=field_map)

    with _client(result, declared_fields=declared_fields) as (
        client,
        runtime,
        _meaning,
        expression,
        _module,
    ):
        conversation_ref, completed = _complete_one(client)
        snapshot = runtime.snapshot(conversation_ref, "actor-context-canary")

    assert completed["status_code"] == 200
    assert completed["turns"][0]["display_kind"] == "card"
    assert completed["turns"][0]["display_payload"] == {
        "type": "fallback-card",
        "safe": True,
    }
    assert expression.requests == []
    assert snapshot.model_safe_ledger[0].model_safe_template is None


def test_non_text_precomposed_mode_uses_fallback_without_expression_call() -> None:
    expression = _ExpressionAdapter(rendered_template="must-not-run")
    result = _precomposed_result(
        template="{field}",
        field_map={"field": "private"},
        display_kind="card",
    )

    with _client(
        result,
        declared_fields=("field",),
        expression=expression,
    ) as (client, _runtime, _meaning, _expression, _module):
        _conversation_ref, completed = _complete_one(client)

    assert completed["status_code"] == 200
    assert completed["turns"][0]["display_kind"] == "card"
    assert completed["turns"][0]["display_payload"] == {
        "type": "fallback-card",
        "safe": True,
    }
    assert expression.requests == []


def test_precomposed_template_literal_cannot_copy_a_local_field_value() -> None:
    field_canary = "PRECOMPOSED-LOCAL-VALUE-CANARY"
    result = _precomposed_result(
        template=f"字面泄漏 {field_canary}；正式槽 {{field}}",
        field_map={"field": field_canary},
    )

    with _client(result, declared_fields=("field",)) as (
        client,
        runtime,
        _meaning,
        expression,
        _module,
    ):
        conversation_ref, completed = _complete_one(client)
        snapshot = runtime.snapshot(conversation_ref, "actor-context-canary")

    assert completed["status_code"] == 200
    assert completed["turns"][0]["display_payload"] == {
        "type": "fallback-card",
        "safe": True,
    }
    assert expression.requests == []
    assert field_canary not in repr(snapshot.model_safe_ledger)


@pytest.mark.parametrize(
    "literal_leak",
    ["private_key_canary", "FIELD-NESTED-CANARY", "731", "true"],
)
def test_precomposed_literal_rejects_nested_json_keys_values_and_scalars(
    literal_leak: str,
) -> None:
    nested_value = {"private_key_canary": ["FIELD-NESTED-CANARY", {"count": 731, "flag": True}]}
    result = _precomposed_result(
        template=f"字面泄漏 {literal_leak}；正式槽 {{field}}",
        field_map={"field": nested_value},
    )

    with _client(result, declared_fields=("field",)) as (
        client,
        runtime,
        _meaning,
        expression,
        _module,
    ):
        conversation_ref, completed = _complete_one(client)
        snapshot = runtime.snapshot(conversation_ref, "actor-context-canary")

    assert completed["status_code"] == 200
    assert completed["turns"][0]["display_payload"] == {
        "type": "fallback-card",
        "safe": True,
    }
    assert expression.requests == []
    assert literal_leak not in repr(snapshot.model_safe_ledger)


def test_valid_compose_sends_only_safe_narration_and_neutral_names_then_fills_locally() -> None:
    slot_one = "SYSTEM-SLOT-CANARY-{S9}"
    slot_two = {"private": "SYSTEM-NESTED-CANARY", "order": 2}
    narration = "组织两个中性槽位的句子，不推断业务含义。"
    expression = _ExpressionAdapter(rendered_template="第二项 {S2}；第一项 {S1}。")
    result = _compose_result(
        narration=narration,
        slot_map={"S1": slot_one, "S2": slot_two},
    )

    with _client(
        result,
        declared_fields=(),
        presentation_capabilities=("compose",),
        expression=expression,
    ) as (client, runtime, _meaning, _expression, _module):
        conversation_ref, completed = _complete_one(client)
        snapshot = runtime.snapshot(conversation_ref, "actor-context-canary")

    assert completed["status_code"] == 200
    assert completed["turns"][0]["display_kind"] == "text"
    assert completed["turns"][0]["display_payload"] == (
        '第二项 {"order":2,"private":"SYSTEM-NESTED-CANARY"}；第一项 SYSTEM-SLOT-CANARY-{S9}。'
    )
    assert len(expression.requests) == 1
    assert expression.requests[0].model_dump(mode="json") == {
        "version": "1",
        "model_safe_expression_input": narration,
        "neutral_slot_names": ["S1", "S2"],
    }
    serialized_request = expression.requests[0].model_dump_json()
    for canary in (
        slot_one,
        "SYSTEM-NESTED-CANARY",
        "actor-context-canary",
        conversation_ref,
        "turn-1",
        "run-1",
    ):
        assert canary not in serialized_request
    for canary in (slot_one, "SYSTEM-NESTED-CANARY", "actor-context-canary"):
        assert canary not in repr(snapshot.model_safe_ledger)
    assert snapshot.model_safe_ledger[0].model_safe_template == "第二项 {S2}；第一项 {S1}。"


@pytest.mark.parametrize(
    "narration_canary",
    [
        "slot_key_canary",
        "SLOT-NARRATION-CANARY",
        "313",
        "true",
        "actor-context-canary",
        "conversation-1",
        "turn-1",
        "run-1",
        "staged-ref-canary",
        "action-ref-canary",
        "recovery-ref-canary",
        "result-ref-canary",
        "diagnostic-ref-canary",
    ],
)
def test_compose_narration_with_local_forbidden_token_skips_expression_and_falls_back(
    narration_canary: str,
) -> None:
    slot_value = {"slot_key_canary": ["SLOT-NARRATION-CANARY", {"number": 313, "flag": True}]}
    expression = _ExpressionAdapter(rendered_template="must-not-run {S1}")
    result = _compose_result(
        narration=narration_canary,
        slot_map={"S1": slot_value},
    ).model_copy(
        update={
            "staged_state_ref": "staged-ref-canary",
            "action_offer": ActionOfferV1(
                action_offer_ref="action-ref-canary",
                safe_label="安全动作",
            ),
            "recovery_control": RecoveryControlV1(
                recovery_control_ref="recovery-ref-canary",
                safe_label="安全恢复",
            ),
            "result_check_handle": ResultCheckHandleV1(result_check_ref="result-ref-canary"),
            "diagnostic_ref": "diagnostic-ref-canary",
        }
    )

    with _client(
        result,
        declared_fields=(),
        presentation_capabilities=("compose",),
        expression=expression,
    ) as (client, runtime, _meaning, _expression, _module):
        conversation_ref, completed = _complete_one(client)
        snapshot = runtime.snapshot(conversation_ref, "actor-context-canary")

    assert completed["status_code"] == 200
    assert completed["turns"][0]["display_payload"] == "安全确定性兜底"
    assert expression.requests == []
    assert snapshot.model_safe_ledger[0].model_safe_template is None


@pytest.mark.parametrize(
    "source",
    ["direct_payload", "field_map", "slot_map", "precomposed_fallback", "compose_fallback"],
)
def test_model_safe_note_cannot_copy_any_presentation_local_value(source: str) -> None:
    note_canary = f"NOTE-{source}-CANARY"
    expression: _ExpressionAdapter | None = None
    if source == "direct_payload":
        result = _direct_result(
            display_kind="card",
            display_payload={"nested": [note_canary]},
        )
        capabilities: tuple[Literal["direct", "precomposed", "compose"], ...] = ("direct",)
        declared_fields: tuple[str, ...] = ()
    elif source in {"field_map", "precomposed_fallback"}:
        result = _precomposed_result(
            template="结果 {field}",
            field_map={"field": {"nested": [note_canary if source == "field_map" else "value"]}},
        )
        if source == "precomposed_fallback":
            result = result.model_copy(
                update={
                    "fallback_presentation": FallbackPresentationV1(
                        display_kind="card",
                        display_payload={"nested": [note_canary]},
                    )
                }
            )
        capabilities = ("precomposed",)
        declared_fields = ("field",)
    else:
        result = _compose_result(
            narration="safe narration",
            slot_map={"S1": {"nested": [note_canary if source == "slot_map" else "value"]}},
        )
        if source == "compose_fallback":
            result = result.model_copy(
                update={
                    "fallback_presentation": FallbackPresentationV1(
                        display_kind="text",
                        display_payload={"nested": [note_canary]},
                    )
                }
            )
        capabilities = ("compose",)
        declared_fields = ()
        expression = _ExpressionAdapter(rendered_template="结果 {S1}")
    result = result.model_copy(update={"model_safe_note": note_canary})

    with _client(
        result,
        declared_fields=declared_fields,
        presentation_capabilities=capabilities,
        expression=expression,
    ) as (client, runtime, _meaning, _expression, _module):
        conversation_ref, completed = _complete_one(client)
        snapshot = runtime.snapshot(conversation_ref, "actor-context-canary")

    assert completed["status_code"] == 200
    assert snapshot.model_safe_ledger[0].model_safe_note is None


@pytest.mark.parametrize(
    "note_canary",
    [
        "actor-context-canary",
        "conversation-1",
        "turn-1",
        "run-1",
        "staged-note-ref",
        "action-note-ref",
        "recovery-note-ref",
        "result-note-ref",
        "diagnostic-note-ref",
    ],
)
def test_model_safe_note_cannot_copy_actor_correlation_or_module_opaque_ref(
    note_canary: str,
) -> None:
    result = _direct_result(display_kind="text", display_payload="visible-result").model_copy(
        update={
            "model_safe_note": note_canary,
            "staged_state_ref": "staged-note-ref",
            "action_offer": ActionOfferV1(
                action_offer_ref="action-note-ref",
                safe_label="安全动作",
            ),
            "recovery_control": RecoveryControlV1(
                recovery_control_ref="recovery-note-ref",
                safe_label="安全恢复",
            ),
            "result_check_handle": ResultCheckHandleV1(result_check_ref="result-note-ref"),
            "diagnostic_ref": "diagnostic-note-ref",
        }
    )

    with _client(
        result,
        declared_fields=(),
        presentation_capabilities=("direct",),
    ) as (client, runtime, _meaning, _expression, _module):
        conversation_ref, completed = _complete_one(client)
        snapshot = runtime.snapshot(conversation_ref, "actor-context-canary")

    assert completed["status_code"] == 200
    assert completed["turns"][0]["display_payload"] == "visible-result"
    assert snapshot.model_safe_ledger[0].model_safe_note is None


def test_teacher_utterance_alone_is_not_a_forbidden_local_token() -> None:
    teacher_utterance = "老师原话可安全再次出现"
    expression = _ExpressionAdapter(rendered_template="结果 {S1}")
    result = _compose_result(
        narration=teacher_utterance,
        slot_map={"S1": "private-value"},
    ).model_copy(update={"model_safe_note": teacher_utterance})

    with _client(
        result,
        declared_fields=(),
        presentation_capabilities=("compose",),
        expression=expression,
    ) as (client, runtime, _meaning, _expression, _module):
        conversation_ref, completed = _complete_one(
            client,
            teacher_utterance=teacher_utterance,
        )
        snapshot = runtime.snapshot(conversation_ref, "actor-context-canary")

    assert completed["status_code"] == 200
    assert len(expression.requests) == 1
    assert snapshot.model_safe_ledger[0].model_safe_note == teacher_utterance


@pytest.mark.parametrize("presentation_mode", ["precomposed", "compose"])
@pytest.mark.parametrize(
    "template_canary",
    [
        "actor-context-canary",
        "conversation-1",
        "turn-1",
        "run-1",
        "staged-template-ref",
        "action-template-ref",
        "recovery-template-ref",
        "result-template-ref",
        "diagnostic-template-ref",
    ],
)
def test_module_template_with_opaque_local_ref_uses_fallback_without_retry(
    presentation_mode: Literal["precomposed", "compose"],
    template_canary: str,
) -> None:
    expression: _ExpressionAdapter | None = None
    if presentation_mode == "precomposed":
        result = _precomposed_result(
            template=f"结果 {{field}}；{template_canary}",
            field_map={"field": "visible-value"},
        )
        declared_fields = ("field",)
        expected_fallback: Any = {"type": "fallback-card", "safe": True}
    else:
        result = _compose_result(
            narration="safe narration",
            slot_map={"S1": "visible-value"},
        )
        expression = _ExpressionAdapter(rendered_template=f"结果 {{S1}}；{template_canary}")
        declared_fields = ()
        expected_fallback = "安全确定性兜底"
    result = result.model_copy(
        update={
            "staged_state_ref": "staged-template-ref",
            "action_offer": ActionOfferV1(
                action_offer_ref="action-template-ref",
                safe_label="安全动作",
            ),
            "recovery_control": RecoveryControlV1(
                recovery_control_ref="recovery-template-ref",
                safe_label="安全恢复",
            ),
            "result_check_handle": ResultCheckHandleV1(result_check_ref="result-template-ref"),
            "diagnostic_ref": "diagnostic-template-ref",
        }
    )

    with _client(
        result,
        declared_fields=declared_fields,
        presentation_capabilities=(presentation_mode,),
        expression=expression,
    ) as (client, runtime, _meaning, expression_adapter, _module):
        conversation_ref, completed = _complete_one(client)
        snapshot = runtime.snapshot(conversation_ref, "actor-context-canary")

    assert completed["status_code"] == 200
    assert completed["turns"][0]["display_payload"] == expected_fallback
    assert len(expression_adapter.requests) == (1 if presentation_mode == "compose" else 0)
    assert snapshot.model_safe_ledger[0].model_safe_template is None


@pytest.mark.parametrize("mode", ["clarify", "none"])
@pytest.mark.parametrize(
    "template_canary",
    ["actor-context-canary", "conversation-1", "turn-1", "run-1"],
)
def test_expression_only_template_with_local_ref_uses_fixed_technical_path(
    mode: Literal["clarify", "none"],
    template_canary: str,
) -> None:
    meaning = _MeaningAdapter(mode=mode, expression_input="safe input")
    expression = _ExpressionAdapter(rendered_template=f"unsafe {template_canary}")
    unused_result = _precomposed_result(template="{unused}", field_map={"unused": "value"})

    with _client(
        unused_result,
        declared_fields=("unused",),
        expression=expression,
        meaning=meaning,
    ) as (client, runtime, _meaning, _expression, module):
        conversation_ref, failed = _complete_one(client)
        snapshot = runtime.snapshot(conversation_ref, "actor-context-canary")

    assert failed["status_code"] == 503
    assert failed["code"] == "assistant.runtime_unavailable"
    assert module.calls == []
    assert len(expression.requests) == 1
    assert snapshot.display_ledger[0].display_payload == {"text": "小静暂时不可用，请稍后重试。"}
    assert snapshot.model_safe_ledger[0].model_safe_template is None


@pytest.mark.parametrize("mode", ["clarify", "none"])
@pytest.mark.parametrize(
    "expression_input_canary",
    ["actor-context-canary", "conversation-1", "turn-1", "run-1"],
)
def test_expression_only_input_with_local_ref_fails_before_expression_call(
    mode: Literal["clarify", "none"],
    expression_input_canary: str,
) -> None:
    meaning = _MeaningAdapter(
        mode=mode,
        expression_input=f"unsafe {expression_input_canary}",
    )
    expression = _ExpressionAdapter(rendered_template="must-not-run")
    unused_result = _precomposed_result(template="{unused}", field_map={"unused": "value"})

    with _client(
        unused_result,
        declared_fields=("unused",),
        expression=expression,
        meaning=meaning,
    ) as (client, runtime, _meaning, _expression, module):
        conversation_ref, failed = _complete_one(client)
        snapshot = runtime.snapshot(conversation_ref, "actor-context-canary")

    assert failed["status_code"] == 503
    assert failed["code"] == "assistant.runtime_unavailable"
    assert expression.requests == []
    assert module.calls == []
    assert snapshot.model_safe_ledger[0].model_safe_note is None
    assert snapshot.model_safe_ledger[0].model_safe_template is None


@pytest.mark.parametrize(
    "fallback_token",
    ["fallback_key_canary", "FALLBACK-STRING-CANARY", "619", "false"],
)
@pytest.mark.parametrize(
    "leak_path",
    ["precomposed_template", "compose_narration", "compose_template"],
)
def test_fallback_nested_token_cannot_enter_any_model_safe_path(
    fallback_token: str,
    leak_path: str,
) -> None:
    fallback_payload: Any = {
        "fallback_key_canary": [
            "FALLBACK-STRING-CANARY",
            {"number": 619, "flag": False},
        ]
    }
    expression: _ExpressionAdapter | None = None
    if leak_path == "precomposed_template":
        result = _precomposed_result(
            template=f"结果 {{field}}；{fallback_token}",
            field_map={"field": "visible-value"},
        )
        declared_fields = ("field",)
        capabilities: tuple[Literal["direct", "precomposed", "compose"], ...] = ("precomposed",)
    else:
        result = _compose_result(
            narration=(
                f"safe narration {fallback_token}"
                if leak_path == "compose_narration"
                else "safe narration"
            ),
            slot_map={"S1": "visible-value"},
        )
        expression = _ExpressionAdapter(
            rendered_template=(
                "must-not-run {S1}"
                if leak_path == "compose_narration"
                else f"结果 {{S1}}；{fallback_token}"
            )
        )
        declared_fields = ()
        capabilities = ("compose",)
    result = result.model_copy(
        update={
            "fallback_presentation": FallbackPresentationV1(
                display_kind="card",
                display_payload=fallback_payload,
            )
        }
    )

    with _client(
        result,
        declared_fields=declared_fields,
        presentation_capabilities=capabilities,
        expression=expression,
    ) as (client, runtime, _meaning, expression_adapter, _module):
        conversation_ref, completed = _complete_one(client)
        snapshot = runtime.snapshot(conversation_ref, "actor-context-canary")

    assert completed["status_code"] == 200
    assert completed["turns"][0]["display_payload"] == fallback_payload
    assert len(expression_adapter.requests) == (1 if leak_path == "compose_template" else 0)
    assert snapshot.model_safe_ledger[0].model_safe_template is None


@pytest.mark.parametrize("path", ["precomposed", "compose", "clarify", "none"])
def test_teacher_utterance_is_not_forbidden_in_a_model_safe_template(path: str) -> None:
    teacher_utterance = "老师原话模板例外"
    meaning: _MeaningAdapter | None = None
    expression: _ExpressionAdapter | None = None
    if path == "precomposed":
        result = _precomposed_result(
            template=f"{teacher_utterance} {{field}}",
            field_map={"field": "visible-value"},
        )
        declared_fields = ("field",)
        capabilities: tuple[Literal["direct", "precomposed", "compose"], ...] = ("precomposed",)
    elif path == "compose":
        result = _compose_result(
            narration="safe narration",
            slot_map={"S1": "visible-value"},
        )
        expression = _ExpressionAdapter(rendered_template=f"{teacher_utterance} {{S1}}")
        declared_fields = ()
        capabilities = ("compose",)
    else:
        result = _precomposed_result(template="{unused}", field_map={"unused": "value"})
        meaning = _MeaningAdapter(
            mode=cast(Literal["clarify", "none"], path),
            expression_input="safe input",
        )
        expression = _ExpressionAdapter(rendered_template=teacher_utterance)
        declared_fields = ("unused",)
        capabilities = ("precomposed",)

    with _client(
        result,
        declared_fields=declared_fields,
        presentation_capabilities=capabilities,
        expression=expression,
        meaning=meaning,
    ) as (client, runtime, _meaning, _expression, _module):
        conversation_ref, completed = _complete_one(
            client,
            teacher_utterance=teacher_utterance,
        )
        snapshot = runtime.snapshot(conversation_ref, "actor-context-canary")

    assert completed["status_code"] == 200
    assert teacher_utterance in cast(
        str,
        snapshot.model_safe_ledger[0].model_safe_template,
    )


def test_compose_template_literal_cannot_copy_a_local_slot_value() -> None:
    slot_canary = "COMPOSE-LOCAL-VALUE-CANARY"
    expression = _ExpressionAdapter(
        rendered_template=f"字面泄漏 {slot_canary}；正式槽 {{S1}} 和 {{S2}}"
    )
    result = _compose_result(
        narration="safe narration",
        slot_map={"S1": slot_canary, "S2": "other-private-value"},
    )

    with _client(
        result,
        declared_fields=(),
        presentation_capabilities=("compose",),
        expression=expression,
    ) as (client, runtime, _meaning, _expression, _module):
        conversation_ref, completed = _complete_one(client)
        snapshot = runtime.snapshot(conversation_ref, "actor-context-canary")

    assert completed["status_code"] == 200
    assert completed["turns"][0]["display_payload"] == "安全确定性兜底"
    assert len(expression.requests) == 1
    assert slot_canary not in repr(snapshot.model_safe_ledger)


@pytest.mark.parametrize(
    "literal_leak",
    ["private_key_canary", "SLOT-NESTED-CANARY", "947", "false"],
)
def test_compose_literal_rejects_nested_json_keys_values_and_scalars(
    literal_leak: str,
) -> None:
    nested_value = {"private_key_canary": ["SLOT-NESTED-CANARY", {"count": 947, "flag": False}]}
    expression = _ExpressionAdapter(
        rendered_template=f"字面泄漏 {literal_leak}；正式槽 {{S1}} 和 {{S2}}"
    )
    result = _compose_result(
        narration="safe narration",
        slot_map={"S1": nested_value, "S2": "other-private-value"},
    )

    with _client(
        result,
        declared_fields=(),
        presentation_capabilities=("compose",),
        expression=expression,
    ) as (client, runtime, _meaning, _expression, _module):
        conversation_ref, completed = _complete_one(client)
        snapshot = runtime.snapshot(conversation_ref, "actor-context-canary")

    assert completed["status_code"] == 200
    assert completed["turns"][0]["display_payload"] == "安全确定性兜底"
    assert len(expression.requests) == 1
    assert literal_leak not in repr(snapshot.model_safe_ledger)


@pytest.mark.parametrize(
    "rendered_template",
    [
        "只有 {S1}",
        "{S1} {S2} {S3}",
        "{S1} {S1} {S2}",
        "{S1!r} {S2}",
        "{S1.name} {S2}",
        "{S1[0]} {S2}",
        "{{S1}} {S2}",
        "{S1 {S2}",
        "SYSTEM-SLOT-CANARY {S2}",
    ],
)
def test_invalid_compose_templates_use_fallback_without_retry(
    rendered_template: str,
) -> None:
    expression = _ExpressionAdapter(rendered_template=rendered_template)
    result = _compose_result(
        narration="safe narration",
        slot_map={"S1": "SYSTEM-SLOT-CANARY", "S2": "second"},
    )

    with _client(
        result,
        declared_fields=(),
        presentation_capabilities=("compose",),
        expression=expression,
    ) as (client, runtime, _meaning, _expression, _module):
        conversation_ref, completed = _complete_one(client)
        snapshot = runtime.snapshot(conversation_ref, "actor-context-canary")

    assert completed["status_code"] == 200
    assert completed["turns"][0]["display_payload"] == "安全确定性兜底"
    assert len(expression.requests) == 1
    assert snapshot.model_safe_ledger[0].model_safe_template is None
    assert "SYSTEM-SLOT-CANARY" not in repr(snapshot.model_safe_ledger)


def test_compose_provider_exception_uses_fallback_without_retry_or_exception_leak() -> None:
    provider_canary = "PROVIDER-EXCEPTION-CANARY"
    expression = _ExpressionAdapter(error=RuntimeError(provider_canary))
    result = _compose_result(narration="safe narration", slot_map={"S1": "private"})

    with _client(
        result,
        declared_fields=(),
        presentation_capabilities=("compose",),
        expression=expression,
    ) as (client, runtime, _meaning, _expression, _module):
        conversation_ref, completed = _complete_one(client)
        snapshot = runtime.snapshot(conversation_ref, "actor-context-canary")

    assert completed["status_code"] == 200
    assert completed["turns"][0]["display_payload"] == "安全确定性兜底"
    assert len(expression.requests) == 1
    assert provider_canary not in repr(snapshot)


def test_non_text_generated_mode_uses_fallback_without_expression_call() -> None:
    expression = _ExpressionAdapter(rendered_template="must-not-run {S1}")
    result = _compose_result(
        narration="safe narration",
        slot_map={"S1": "private"},
        display_kind="card",
    )

    with _client(
        result,
        declared_fields=(),
        presentation_capabilities=("compose",),
        expression=expression,
    ) as (client, _runtime, _meaning, _expression, _module):
        _conversation_ref, completed = _complete_one(client)

    assert completed["status_code"] == 200
    assert completed["turns"][0]["display_kind"] == "text"
    assert completed["turns"][0]["display_payload"] == "安全确定性兜底"
    assert expression.requests == []


def test_stop_while_expression_is_in_flight_rejects_the_late_presentation() -> None:
    entered = Event()
    release = Event()
    expression = _ExpressionAdapter(
        rendered_template="完成 {S1}",
        entered=entered,
        release=release,
    )
    result = _compose_result(narration="safe narration", slot_map={"S1": "private"})

    with _client(
        result,
        declared_fields=(),
        presentation_capabilities=("compose",),
        expression=expression,
    ) as (client, runtime, _meaning, _expression, _module):
        created = client.post("/api/v1/assistant/conversations")
        conversation_ref = cast(str, created.json()["conversation_ref"])
        response: list[Any] = []

        def send_turn() -> None:
            response.append(
                client.post(
                    f"/api/v1/assistant/conversations/{conversation_ref}/turns",
                    json={"teacher_utterance": "启动第二次表达"},
                )
            )

        worker = Thread(target=send_turn)
        worker.start()
        try:
            assert entered.wait(timeout=3)
            stopped = client.post(
                f"/api/v1/assistant/conversations/{conversation_ref}/runs/run-1/stop",
                json={"generation": 1},
            )
            assert stopped.status_code == 200
        finally:
            release.set()
            worker.join(timeout=5)
        snapshot = runtime.snapshot(conversation_ref, "actor-context-canary")

    assert not worker.is_alive()
    assert response[0].status_code == 409
    assert snapshot.display_ledger[0].visible_state == "stopped"
    assert snapshot.display_ledger[0].display_payload is None
    assert snapshot.model_safe_ledger[0].technical_marker == "stopped"


@pytest.mark.parametrize("mode", ["clarify", "none"])
def test_clarify_and_none_call_expression_once_without_calling_a_module(
    mode: Literal["clarify", "none"],
) -> None:
    safe_input = f"safe {mode} expression"
    meaning = _MeaningAdapter(mode=mode, expression_input=safe_input)
    expression = _ExpressionAdapter(rendered_template=f"{mode} 的安全回复")
    unused_result = _precomposed_result(template="{unused}", field_map={"unused": "value"})

    with _client(
        unused_result,
        declared_fields=("unused",),
        expression=expression,
        meaning=meaning,
    ) as (client, runtime, _meaning, _expression, module):
        conversation_ref, completed = _complete_one(client)
        snapshot = runtime.snapshot(conversation_ref, "actor-context-canary")

    assert completed["status_code"] == 200
    assert completed["turns"][0]["display_kind"] == "text"
    assert completed["turns"][0]["display_payload"] == f"{mode} 的安全回复"
    assert module.calls == []
    assert len(expression.requests) == 1
    assert expression.requests[0].model_dump(mode="json") == {
        "version": "1",
        "model_safe_expression_input": safe_input,
        "neutral_slot_names": [],
    }
    assert snapshot.model_safe_ledger[0].model_safe_template == f"{mode} 的安全回复"


@pytest.mark.parametrize("mode", ["clarify", "none"])
def test_clarify_and_none_expression_failure_uses_fixed_technical_path(
    mode: Literal["clarify", "none"],
) -> None:
    provider_canary = "CLARIFY-PROVIDER-FAILURE-CANARY"
    meaning = _MeaningAdapter(mode=mode, expression_input="safe input")
    expression = _ExpressionAdapter(error=RuntimeError(provider_canary))
    unused_result = _precomposed_result(template="{unused}", field_map={"unused": "value"})

    with _client(
        unused_result,
        declared_fields=("unused",),
        expression=expression,
        meaning=meaning,
    ) as (client, runtime, _meaning, _expression, module):
        conversation_ref, failed = _complete_one(client)
        snapshot = runtime.snapshot(conversation_ref, "actor-context-canary")

    assert failed["status_code"] == 503
    assert failed["code"] == "assistant.runtime_unavailable"
    assert module.calls == []
    assert len(expression.requests) == 1
    assert snapshot.display_ledger[0].display_kind == "text"
    assert snapshot.display_ledger[0].display_payload == {"text": "小静暂时不可用，请稍后重试。"}
    assert provider_canary not in repr(snapshot)
