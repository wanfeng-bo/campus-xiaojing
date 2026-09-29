from __future__ import annotations

import json
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import FrozenInstanceError
from importlib import import_module
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
from app.platform.assistant.contracts import (
    CapsuleUnchangedV1,
    MeaningModelCorrelationV1,
    MeaningModelRequestV1,
    MeaningV1,
    ModuleCallV1,
    ModuleConfirmationCallV1,
    ModuleRecoveryCallV1,
    ModuleResultCheckCallV1,
    ModuleResultV1,
    TextSpanEvidenceV1,
    ValidationReceiptV1,
)
from app.platform.assistant.runtime import AssistantRuntime


class _Refs:
    def __init__(self) -> None:
        self._counts: dict[str, int] = defaultdict(int)

    def __call__(self, kind: str) -> str:
        self._counts[kind] += 1
        return f"{kind}-{self._counts[kind]}"


class _ProbeMeaningAdapter:
    def __init__(
        self,
        module_ref: str,
        *,
        failure_canary: str | None = None,
        evidence_span_end: int | None = None,
        entered: Event | None = None,
        release: Event | None = None,
    ) -> None:
        self.module_ref = module_ref
        self.failure_canary = failure_canary
        self.evidence_span_end = evidence_span_end
        self.entered = entered
        self.release = release
        self.requests: list[MeaningModelRequestV1] = []
        self.correlations: list[MeaningModelCorrelationV1] = []

    def interpret(
        self,
        request: MeaningModelRequestV1,
        *,
        correlation: MeaningModelCorrelationV1,
    ) -> MeaningV1:
        self.requests.append(request)
        self.correlations.append(correlation)
        if self.entered is not None:
            self.entered.set()
        if self.release is not None and not self.release.wait(timeout=5):
            raise RuntimeError("probe meaning release timed out")
        if self.failure_canary is not None:
            raise RuntimeError(self.failure_canary)
        return MeaningV1(
            version="1",
            conversation_ref=correlation.conversation_ref,
            turn_ref=correlation.turn_ref,
            run_ref=correlation.run_ref,
            mode="dispatch",
            module_ref=self.module_ref,
            meaning_payload={"probe_token": "fixed"},
            evidence_refs=(
                ()
                if self.evidence_span_end is None
                else (
                    TextSpanEvidenceV1(
                        kind="text_span",
                        start=0,
                        end=self.evidence_span_end,
                    ),
                )
            ),
            candidate_steps=(),
            precomposition_candidate=None,
            model_safe_expression_input=None,
        )


class _ProbeModule:
    def __init__(
        self,
        label: str,
        *,
        display_kind: Literal["text", "card"] = "text",
    ) -> None:
        self.label = label
        self.display_kind: Literal["text", "card"] = display_kind
        self.calls: list[ModuleCallV1] = []

    def handle(self, request: ModuleCallV1) -> ModuleResultV1:
        self.calls.append(request)
        return ModuleResultV1(
            version="1",
            validation_receipt=ValidationReceiptV1(
                status="accepted",
                safe_result_family="fixture.probe",
            ),
            locked_notice=None,
            staged_state_ref="probe-state-1",
            state_revision=1,
            capsule_update=CapsuleUnchangedV1(kind="unchanged"),
            presentation_mode="direct",
            display_kind=self.display_kind,
            display_payload={"text": self.label},
            display_template=None,
            field_map=None,
            model_safe_narration=None,
            slot_map=None,
            fallback_presentation=None,
            model_safe_note="probe-safe-note",
            action_offer=None,
            recovery_control=None,
            result_check_handle=None,
            safe_error_projection=None,
            diagnostic_ref=None,
        )

    def confirm(self, request: ModuleConfirmationCallV1) -> ModuleResultV1:
        raise AssertionError(f"probe confirmation must not run: {request!r}")

    def recover(self, request: ModuleRecoveryCallV1) -> ModuleResultV1:
        raise AssertionError(f"probe recovery must not run: {request!r}")

    def check_result(self, request: ModuleResultCheckCallV1) -> ModuleResultV1:
        raise AssertionError(f"probe result check must not run: {request!r}")


def _load_vertical_slice() -> tuple[type[Any], type[Any], type[Any]]:
    try:
        catalog_module = import_module("app.platform.assistant.catalog")
        orchestrator_module = import_module("app.platform.assistant.orchestrator")
    except ModuleNotFoundError:
        pytest.fail("K3-B static catalog and single-module orchestrator are missing")
    return (
        cast(type[Any], catalog_module.AssistantModuleCatalogEntry),
        cast(type[Any], catalog_module.StaticAssistantCatalog),
        cast(type[Any], orchestrator_module.AssistantTurnOrchestrator),
    )


@contextmanager
def _probe_client(
    *,
    actor_context_ref: str,
    failure_canary: str | None = None,
    evidence_span_end: int | None = None,
    meaning_entered: Event | None = None,
    meaning_release: Event | None = None,
    probe_display_kind: Literal["text", "card"] = "text",
    probe_field_names: tuple[str, ...] = (),
    probe_capabilities: tuple[Literal["direct", "precomposed", "compose"], ...] = ("direct",),
) -> Any:
    entry_type, catalog_type, orchestrator_type = _load_vertical_slice()
    selected_module = _ProbeModule(
        "probe-completed",
        display_kind=probe_display_kind,
    )
    untouched_module = _ProbeModule("must-not-run")
    meaning_adapter = _ProbeMeaningAdapter(
        "fixture.probe",
        failure_canary=failure_canary,
        evidence_span_end=evidence_span_end,
        entered=meaning_entered,
        release=meaning_release,
    )
    catalog = catalog_type(
        (
            entry_type(
                module_ref="fixture.probe",
                model_safe_capability="仅验证固定协议链路，不办理学校业务。",
                module=selected_module,
                model_visible_field_names=probe_field_names,
                presentation_capabilities=probe_capabilities,
            ),
            entry_type(
                module_ref="fixture.unused",
                model_safe_capability="仅用于证明单模块投递。",
                module=untouched_module,
            ),
        )
    )
    orchestrator = orchestrator_type(
        meaning_model=meaning_adapter,
        catalog=catalog,
    )
    runtime = AssistantRuntime(ref_factory=_Refs())

    def resolve_identity() -> AssistantRequestIdentity:
        return AssistantRequestIdentity(actor_context_ref=actor_context_ref)

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
        yield (
            client,
            runtime,
            meaning_adapter,
            selected_module,
            untouched_module,
        )


def test_business_free_probe_runs_once_through_http_runtime_and_three_ledgers() -> None:
    actor_canary = "SYSTEM-HELD-ACTOR-CANARY"
    teacher_utterance = "  老师原话自带号码 13800000000  "

    with _probe_client(actor_context_ref=actor_canary) as (
        client,
        runtime,
        meaning_adapter,
        selected_module,
        untouched_module,
    ):
        created = client.post("/api/v1/assistant/conversations")
        assert created.status_code == 201
        conversation_ref = cast(str, created.json()["conversation_ref"])

        completed = client.post(
            f"/api/v1/assistant/conversations/{conversation_ref}/turns",
            json={"teacher_utterance": teacher_utterance},
        )
        assert completed.status_code == 200
        assert completed.json()["turns"] == [
            {
                "turn_ref": "turn-1",
                "run_ref": "run-1",
                "generation": 1,
                "teacher_utterance": teacher_utterance,
                "visible_state": "completed",
                "display_kind": "text",
                "display_payload": {"text": "probe-completed"},
                "controls": [],
            }
        ]

        snapshot = runtime.snapshot(conversation_ref, actor_canary)

    assert len(meaning_adapter.requests) == 1
    assert meaning_adapter.requests[0].current_teacher_utterance == teacher_utterance
    provider_payload = meaning_adapter.requests[0].model_dump_json()
    assert actor_canary not in provider_payload
    assert "仅验证固定协议链路，不办理学校业务。" in provider_payload
    assert len(selected_module.calls) == 1
    assert selected_module.calls[0].actor_context_ref == actor_canary
    assert selected_module.calls[0].current_teacher_utterance == teacher_utterance
    assert untouched_module.calls == []
    assert [entry.state for entry in snapshot.causal_ledger] == [
        "conversation_opened",
        "run_active",
        "run_completed",
    ]
    assert snapshot.display_ledger[0].display_payload == {"text": "probe-completed"}
    assert snapshot.model_safe_ledger[0].model_safe_note == "probe-safe-note"
    assert actor_canary not in repr(snapshot.model_safe_ledger)


def test_static_catalog_rejects_duplicate_module_refs() -> None:
    entry_type, catalog_type, _orchestrator_type = _load_vertical_slice()
    module = _ProbeModule("probe")
    duplicate = entry_type(
        module_ref="fixture.duplicate",
        model_safe_capability="固定能力。",
        module=module,
    )

    with pytest.raises(ValueError, match="duplicate module_ref"):
        catalog_type((duplicate, duplicate))


def test_meaning_request_lists_only_deterministic_static_presentation_metadata() -> None:
    actor_canary = "RUNTIME-ACTOR-MUST-NOT-ENTER-CATALOG-METADATA"
    with _probe_client(
        actor_context_ref=actor_canary,
        probe_field_names=("zeta_field", "alpha.field"),
        probe_capabilities=("precomposed", "direct"),
    ) as (client, _runtime, meaning, _selected, _untouched):
        created = client.post("/api/v1/assistant/conversations")
        conversation_ref = cast(str, created.json()["conversation_ref"])
        completed = client.post(
            f"/api/v1/assistant/conversations/{conversation_ref}/turns",
            json={"teacher_utterance": "runtime utterance canary"},
        )

    assert completed.status_code == 200
    assert meaning.requests[0].model_safe_context == (
        "只返回一个符合 MeaningV1 的候选含义，不执行任何业务动作。",
        "module_ref=fixture.probe; capability=仅验证固定协议链路，不办理学校业务。; "
        "model_visible_field_names=[alpha.field,zeta_field]; "
        "presentation_capabilities=[direct,precomposed]",
        "module_ref=fixture.unused; capability=仅用于证明单模块投递。; "
        "model_visible_field_names=[]; presentation_capabilities=[direct]",
    )
    static_metadata = "\n".join(meaning.requests[0].model_safe_context[1:])
    for runtime_value in (
        actor_canary,
        conversation_ref,
        "turn-1",
        "run-1",
        "runtime utterance canary",
    ):
        assert runtime_value not in static_metadata


def test_static_catalog_cannot_be_mutated_after_construction() -> None:
    entry_type, catalog_type, _orchestrator_type = _load_vertical_slice()
    original = entry_type(
        module_ref="fixture.original",
        model_safe_capability="原始固定能力。",
        module=_ProbeModule("original"),
    )
    injected = entry_type(
        module_ref="fixture.injected",
        model_safe_capability="不得动态注入。",
        module=_ProbeModule("injected"),
    )
    catalog = catalog_type((original,))
    original_context = catalog.model_safe_context()

    with pytest.raises(FrozenInstanceError):
        catalog._entries = (injected,)
    with pytest.raises(AttributeError):
        catalog._by_ref["fixture.injected"] = injected

    assert catalog.model_safe_context() == original_context
    assert catalog.resolve("fixture.original") is original
    with pytest.raises(LookupError, match="module is not installed"):
        catalog.resolve("fixture.injected")


def test_catalog_entry_copies_mutable_metadata_inputs_into_tuples() -> None:
    entry_type, _catalog_type, _orchestrator_type = _load_vertical_slice()
    field_names = ["field.one"]
    capabilities = ["direct"]
    entry = entry_type(
        module_ref="fixture.normalized",
        model_safe_capability="固定静态元数据。",
        module=_ProbeModule("normalized"),
        model_visible_field_names=field_names,
        presentation_capabilities=capabilities,
    )

    field_names.append("field.two")
    capabilities.append("compose")

    assert entry.model_visible_field_names == ("field.one",)
    assert entry.presentation_capabilities == ("direct",)
    assert isinstance(entry.model_visible_field_names, tuple)
    assert isinstance(entry.presentation_capabilities, tuple)


def test_catalog_entry_rejects_unknown_presentation_capability() -> None:
    entry_type, _catalog_type, _orchestrator_type = _load_vertical_slice()

    with pytest.raises(ValueError, match="presentation capability is invalid"):
        entry_type(
            module_ref="fixture.invalidcap",
            model_safe_capability="固定静态元数据。",
            module=_ProbeModule("invalid"),
            presentation_capabilities=("direct", "runtime_plugin"),
        )


def test_catalog_context_accepts_exactly_8000_characters_and_rejects_one_more() -> None:
    entry_type, catalog_type, _orchestrator_type = _load_vertical_slice()
    probe = entry_type(
        module_ref="fixture.boundary",
        model_safe_capability="x",
        module=_ProbeModule("boundary"),
        model_visible_field_names=("field.one",),
        presentation_capabilities=("compose", "direct"),
    )
    probe_length = len(catalog_type((probe,)).model_safe_context()[0])
    exact_capability = "x" * (8_001 - probe_length)

    exact = entry_type(
        module_ref="fixture.boundary",
        model_safe_capability=exact_capability,
        module=_ProbeModule("boundary"),
        model_visible_field_names=("field.one",),
        presentation_capabilities=("compose", "direct"),
    )
    assert len(catalog_type((exact,)).model_safe_context()[0]) == 8_000

    with pytest.raises(ValueError, match="model-safe catalog context is too long"):
        entry_type(
            module_ref="fixture.boundary",
            model_safe_capability=f"{exact_capability}x",
            module=_ProbeModule("boundary"),
            model_visible_field_names=("field.one",),
            presentation_capabilities=("compose", "direct"),
        )


@pytest.mark.parametrize("stream", [False, True])
def test_meaning_failure_invalidates_the_run_without_leaking_or_poisoning_conversation(
    stream: bool,
) -> None:
    failure_canary = "MODEL-FAILURE-SENSITIVE-CANARY"
    with _probe_client(
        actor_context_ref="actor-context-1",
        failure_canary=failure_canary,
    ) as (client, runtime, _meaning, _selected, _untouched):
        created = client.post("/api/v1/assistant/conversations")
        conversation_ref = cast(str, created.json()["conversation_ref"])

        failed = client.post(
            f"/api/v1/assistant/conversations/{conversation_ref}/turns",
            json={"teacher_utterance": "第一句"},
            headers={"Accept": "application/x-ndjson"} if stream else {},
        )
        if stream:
            assert failed.status_code == 200
            events = [json.loads(line) for line in failed.text.splitlines()]
            assert [event["event"] for event in events] == ["accepted", "error"]
            assert events[-1] == {"event": "error", "code": "assistant.runtime_unavailable"}
        else:
            assert failed.status_code == 503
        assert failure_canary not in failed.text

        after_failure = client.get(f"/api/v1/assistant/conversations/{conversation_ref}")
        assert after_failure.status_code == 200
        assert after_failure.json()["active_run"] is None
        assert after_failure.json()["turns"][0] == {
            "turn_ref": "turn-1",
            "run_ref": "run-1",
            "generation": 1,
            "teacher_utterance": "第一句",
            "visible_state": "completed",
            "display_kind": "text",
            "display_payload": {
                "text": "小静暂时不可用，请稍后重试。",
                "error_code": "assistant.runtime_unavailable",
            },
            "controls": [],
        }

        next_failed = client.post(
            f"/api/v1/assistant/conversations/{conversation_ref}/turns",
            json={"teacher_utterance": "第二句"},
        )
        assert next_failed.status_code == 503
        assert next_failed.json()["code"] == "assistant.runtime_unavailable"
        snapshot = runtime.snapshot(conversation_ref, "actor-context-1")

    assert snapshot.active_run is None
    assert [entry.state for entry in snapshot.causal_ledger] == [
        "conversation_opened",
        "run_active",
        "run_completed",
        "run_active",
        "run_completed",
    ]
    assert failure_canary not in repr(snapshot.model_safe_ledger)


def test_out_of_bounds_text_evidence_is_rejected_before_module_delivery() -> None:
    with _probe_client(
        actor_context_ref="actor-context-1",
        evidence_span_end=999,
    ) as (client, runtime, _meaning, selected, untouched):
        created = client.post("/api/v1/assistant/conversations")
        conversation_ref = cast(str, created.json()["conversation_ref"])

        failed = client.post(
            f"/api/v1/assistant/conversations/{conversation_ref}/turns",
            json={"teacher_utterance": "短句"},
        )
        snapshot = runtime.snapshot(conversation_ref, "actor-context-1")

    assert failed.status_code == 503
    assert selected.calls == []
    assert untouched.calls == []
    assert snapshot.active_run is None
    assert snapshot.display_ledger[0].visible_state == "completed"
    assert snapshot.display_ledger[0].display_payload == {
        "text": "小静暂时不可用，请稍后重试。",
        "error_code": "assistant.runtime_unavailable",
    }


def test_stop_while_meaning_is_in_flight_prevents_late_module_delivery() -> None:
    entered = Event()
    release = Event()
    with _probe_client(
        actor_context_ref="actor-context-1",
        meaning_entered=entered,
        meaning_release=release,
    ) as (client, runtime, _meaning, selected, untouched):
        created = client.post("/api/v1/assistant/conversations")
        conversation_ref = cast(str, created.json()["conversation_ref"])
        turn_response: list[Any] = []

        def send_turn() -> None:
            turn_response.append(
                client.post(
                    f"/api/v1/assistant/conversations/{conversation_ref}/turns",
                    json={"teacher_utterance": "请处理这句无业务探针文本"},
                )
            )

        worker = Thread(target=send_turn)
        worker.start()
        try:
            assert entered.wait(timeout=3)
            active = client.get(f"/api/v1/assistant/conversations/{conversation_ref}").json()
            stopped = client.post(
                f"/api/v1/assistant/conversations/{conversation_ref}/runs/run-1/stop",
                json={"generation": active["generation"]},
            )
            assert stopped.status_code == 200
        finally:
            release.set()
            worker.join(timeout=5)

        assert not worker.is_alive()
        snapshot = runtime.snapshot(conversation_ref, "actor-context-1")

    assert turn_response[0].status_code == 409
    assert selected.calls == []
    assert untouched.calls == []
    assert snapshot.active_run is None
    assert [entry.state for entry in snapshot.causal_ledger] == [
        "conversation_opened",
        "run_active",
        "run_stopped",
    ]


def test_direct_card_preserves_kind_through_http_runtime_and_display_ledger() -> None:
    with _probe_client(
        actor_context_ref="actor-context-1",
        probe_display_kind="card",
    ) as (client, runtime, _meaning, selected, _untouched):
        created = client.post("/api/v1/assistant/conversations")
        conversation_ref = cast(str, created.json()["conversation_ref"])

        completed = client.post(
            f"/api/v1/assistant/conversations/{conversation_ref}/turns",
            json={"teacher_utterance": "测试卡片边界"},
        )
        snapshot = runtime.snapshot(conversation_ref, "actor-context-1")

    assert completed.status_code == 200
    assert completed.json()["turns"][0]["display_kind"] == "card"
    assert completed.json()["turns"][0]["display_payload"] == {"text": "probe-completed"}
    assert len(selected.calls) == 1
    assert snapshot.active_run is None
    assert snapshot.display_ledger[0].display_kind == "card"
    assert snapshot.display_ledger[0].display_payload == {"text": "probe-completed"}


def test_direct_result_without_declared_capability_uses_fixed_technical_failure() -> None:
    with _probe_client(
        actor_context_ref="actor-context-1",
        probe_capabilities=("precomposed",),
    ) as (client, runtime, _meaning, selected, _untouched):
        created = client.post("/api/v1/assistant/conversations")
        conversation_ref = cast(str, created.json()["conversation_ref"])

        failed = client.post(
            f"/api/v1/assistant/conversations/{conversation_ref}/turns",
            json={"teacher_utterance": "模块未声明 direct"},
        )
        snapshot = runtime.snapshot(conversation_ref, "actor-context-1")

    assert failed.status_code == 503
    assert failed.json()["code"] == "assistant.runtime_unavailable"
    assert len(selected.calls) == 1
    assert snapshot.active_run is None
    assert snapshot.display_ledger[0].display_kind == "text"
    assert snapshot.display_ledger[0].display_payload == {
        "text": "小静暂时不可用，请稍后重试。",
        "error_code": "assistant.runtime_unavailable",
    }
    assert snapshot.model_safe_ledger[0].model_safe_note is None
