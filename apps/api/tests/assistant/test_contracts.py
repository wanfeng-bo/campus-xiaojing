from __future__ import annotations

import math
from typing import get_type_hints

import pytest
from pydantic import ValidationError

from app.platform.assistant.contracts import (
    CapsuleClearV1,
    CapsuleReplaceV1,
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
    PrecompositionCandidateV1,
    TextSpanEvidenceV1,
    ValidationReceiptV1,
    VerbatimEvidenceV1,
)
from app.platform.assistant.ports import (
    AssistantModulePort,
    ExpressionModelPort,
    MeaningModelPort,
)

REFS = {
    "conversation_ref": "conversation-1",
    "turn_ref": "turn-1",
    "run_ref": "run-1",
}


def _meaning(**overrides: object) -> MeaningV1:
    values: dict[str, object] = {
        "version": "1",
        **REFS,
        "mode": "dispatch",
        "module_ref": "fixture.alpha",
        "meaning_payload": {"opaque_key": "opaque-value"},
        "evidence_refs": (TextSpanEvidenceV1(kind="text_span", start=0, end=2),),
        "candidate_steps": ({"opaque_step": 1},),
        "precomposition_candidate": None,
        "model_safe_expression_input": None,
    }
    values.update(overrides)
    return MeaningV1.model_validate(values)


def _module_result(**overrides: object) -> ModuleResultV1:
    values: dict[str, object] = {
        "version": "1",
        "validation_receipt": ValidationReceiptV1(
            status="accepted",
            safe_result_family="fixture.accepted",
        ),
        "locked_notice": None,
        "staged_state_ref": "state-2",
        "state_revision": 2,
        "capsule_update": CapsuleUnchangedV1(kind="unchanged"),
        "presentation_mode": "direct",
        "display_kind": "text",
        "display_payload": {"text": "safe display"},
        "display_template": None,
        "field_map": None,
        "model_safe_narration": None,
        "slot_map": None,
        "fallback_presentation": None,
        "model_safe_note": None,
        "action_offer": None,
        "recovery_control": None,
        "result_check_handle": None,
        "safe_error_projection": None,
        "diagnostic_ref": None,
    }
    values.update(overrides)
    return ModuleResultV1.model_validate(values)


def test_meaning_dispatch_has_exactly_one_module_and_preserves_opaque_payload() -> None:
    meaning = _meaning()

    assert meaning.module_ref == "fixture.alpha"
    assert meaning.meaning_payload == {"opaque_key": "opaque-value"}
    assert meaning.candidate_steps == ({"opaque_step": 1},)

    with pytest.raises(ValidationError):
        _meaning(module_ref=None)


@pytest.mark.parametrize("mode", ["clarify", "none"])
def test_non_dispatch_meaning_has_no_module_and_requires_safe_expression(mode: str) -> None:
    meaning = _meaning(
        mode=mode,
        module_ref=None,
        model_safe_expression_input="请老师补充一句。",
    )

    assert meaning.module_ref is None

    with pytest.raises(ValidationError):
        _meaning(mode=mode, module_ref=None, model_safe_expression_input=None)

    with pytest.raises(ValidationError):
        _meaning(
            mode=mode,
            module_ref="fixture.alpha",
            model_safe_expression_input="safe",
        )


def test_evidence_contracts_are_mechanical_spans_or_verbatim_text() -> None:
    meaning = _meaning(
        evidence_refs=(
            TextSpanEvidenceV1(kind="text_span", start=0, end=2),
            VerbatimEvidenceV1(kind="verbatim", text="原话"),
        )
    )

    assert [item.kind for item in meaning.evidence_refs] == ["text_span", "verbatim"]

    with pytest.raises(ValidationError):
        TextSpanEvidenceV1(kind="text_span", start=3, end=3)


def test_contracts_reject_unknown_fields_and_do_not_accept_business_authority() -> None:
    payload = _meaning().model_dump()
    payload["teacher_role"] = "admin"

    with pytest.raises(ValidationError):
        MeaningV1.model_validate(payload)


def test_json_envelopes_reject_non_finite_numbers() -> None:
    with pytest.raises(ValidationError):
        _meaning(meaning_payload={"not_json": math.nan})


def test_module_call_carries_the_meaning_envelope_without_interpreting_it() -> None:
    meaning = _meaning(
        precomposition_candidate=PrecompositionCandidateV1(
            template_version="fixture-v1",
            display_template="{{field_a}}",
            field_names=("field_a",),
        )
    )
    assert meaning.module_ref is not None
    call = ModuleCallV1(
        version="1",
        module_ref=meaning.module_ref,
        **REFS,
        actor_context_ref="actor-context-1",
        current_teacher_utterance="老师原话",
        meaning_payload=meaning.meaning_payload,
        evidence_refs=meaning.evidence_refs,
        candidate_steps=meaning.candidate_steps,
        module_state_ref="state-1",
        state_revision=1,
        precomposition_candidate=meaning.precomposition_candidate,
    )

    assert call.meaning_payload == meaning.meaning_payload
    assert call.evidence_refs == meaning.evidence_refs
    assert call.candidate_steps == meaning.candidate_steps
    assert call.precomposition_candidate == meaning.precomposition_candidate
    assert call.actor_context_ref == "actor-context-1"


def test_module_call_state_reference_and_revision_are_an_atomic_pair() -> None:
    with pytest.raises(ValidationError):
        ModuleCallV1(
            version="1",
            module_ref="fixture.alpha",
            **REFS,
            actor_context_ref="actor-context-1",
            current_teacher_utterance="原话",
            meaning_payload={},
            evidence_refs=(),
            candidate_steps=(),
            module_state_ref="state-1",
            state_revision=None,
            precomposition_candidate=None,
        )


def test_capsule_update_only_replace_can_carry_model_safe_capsule() -> None:
    assert CapsuleUnchangedV1(kind="unchanged").model_dump() == {"kind": "unchanged"}
    assert CapsuleClearV1(kind="clear").model_dump() == {"kind": "clear"}
    assert (
        CapsuleReplaceV1(
            kind="replace",
            model_safe_capsule="safe capsule",
        ).model_safe_capsule
        == "safe capsule"
    )

    with pytest.raises(ValidationError):
        CapsuleUnchangedV1.model_validate(
            {"kind": "unchanged", "model_safe_capsule": "must not pass"}
        )


def test_direct_result_uses_only_direct_payload() -> None:
    result = _module_result()

    assert result.presentation_mode == "direct"
    assert result.display_payload == {"text": "safe display"}

    with pytest.raises(ValidationError):
        _module_result(model_safe_narration="must not accompany direct")


def test_precomposed_result_requires_field_map_and_deterministic_fallback() -> None:
    fallback = FallbackPresentationV1(display_kind="text", display_payload={"text": "fallback"})
    result = _module_result(
        presentation_mode="precomposed",
        display_payload=None,
        display_template="{{field_a}}",
        field_map={"field_a": "local secret value"},
        fallback_presentation=fallback,
    )

    assert result.field_map == {"field_a": "local secret value"}
    assert result.model_safe_narration is None

    with pytest.raises(ValidationError):
        _module_result(
            presentation_mode="precomposed",
            display_payload=None,
            display_template="{{field_a}}",
            field_map={"field_a": "value"},
            fallback_presentation=None,
        )


def test_fallback_presentation_rejects_a_null_display_payload() -> None:
    with pytest.raises(ValidationError):
        FallbackPresentationV1.model_validate({"display_kind": "text", "display_payload": None})


def test_compose_result_separates_model_safe_narration_from_local_slot_values() -> None:
    fallback = FallbackPresentationV1(display_kind="text", display_payload={"text": "fallback"})
    result = _module_result(
        presentation_mode="compose",
        display_payload=None,
        model_safe_narration="请组织句式并保留 {{S1}}。",
        slot_map={"S1": "system-held-sensitive-value"},
        fallback_presentation=fallback,
    )

    assert result.model_safe_narration == "请组织句式并保留 {{S1}}。"
    assert result.slot_map == {"S1": "system-held-sensitive-value"}

    with pytest.raises(ValidationError):
        _module_result(
            presentation_mode="compose",
            display_payload=None,
            model_safe_narration="safe",
            slot_map={"S1": "value"},
            field_map={"field_a": "must not coexist"},
            fallback_presentation=fallback,
        )


def test_compose_slots_must_use_neutral_numbered_names() -> None:
    with pytest.raises(ValidationError):
        ExpressionModelRequestV1(
            version="1",
            model_safe_expression_input="safe explanation",
            neutral_slot_names=("teacher_phone",),
        )

    fallback = FallbackPresentationV1(display_kind="text", display_payload={"text": "fallback"})
    with pytest.raises(ValidationError):
        _module_result(
            presentation_mode="compose",
            display_payload=None,
            model_safe_narration="请保留 {{S1}}。",
            slot_map={"student_name": "system-held-sensitive-value"},
            fallback_presentation=fallback,
        )


def test_model_boundary_requests_expose_only_explicit_model_safe_fields() -> None:
    meaning_request = MeaningModelRequestV1(
        version="1",
        current_teacher_utterance="老师主动发来的原话可进入模型",
        model_safe_context=("fixed prompt", "fixture capability"),
    )
    expression_request = ExpressionModelRequestV1(
        version="1",
        model_safe_expression_input="safe explanation",
        neutral_slot_names=("S1",),
    )

    assert set(meaning_request.model_dump()) == {
        "version",
        "current_teacher_utterance",
        "model_safe_context",
    }
    assert set(expression_request.model_dump()) == {
        "version",
        "model_safe_expression_input",
        "neutral_slot_names",
    }

    with pytest.raises(ValidationError):
        MeaningModelRequestV1.model_validate(
            {
                "version": "1",
                "conversation_ref": "must-stay-local",
                "current_teacher_utterance": "老师原话",
                "model_safe_context": (),
            }
        )


def test_model_port_keeps_causal_correlation_outside_provider_request() -> None:
    correlation = MeaningModelCorrelationV1(
        conversation_ref="conversation-1",
        turn_ref="turn-1",
        run_ref="run-1",
    )

    assert set(correlation.model_dump()) == set(REFS)
    hints = get_type_hints(MeaningModelPort.interpret)
    assert hints["correlation"] is MeaningModelCorrelationV1
    assert hints["return"] is MeaningV1


def test_required_safe_text_rejects_whitespace_without_trimming_teacher_utterance() -> None:
    with pytest.raises(ValidationError):
        _meaning(
            mode="clarify",
            module_ref=None,
            model_safe_expression_input="   ",
        )

    request = MeaningModelRequestV1(
        version="1",
        current_teacher_utterance="  老师原话  ",
        model_safe_context=("safe",),
    )
    assert request.current_teacher_utterance == "  老师原话  "


def test_only_protocol_ports_are_exposed_for_models_and_modules() -> None:
    assert getattr(MeaningModelPort, "_is_protocol", False)
    assert getattr(ExpressionModelPort, "_is_protocol", False)
    assert getattr(AssistantModulePort, "_is_protocol", False)
    assert set(get_type_hints(MeaningModelPort.interpret)) == {
        "request",
        "correlation",
        "return",
    }
    assert set(get_type_hints(ExpressionModelPort.compose)) == {"request", "return"}
    assert {name for name in AssistantModulePort.__dict__ if not name.startswith("_")} == {
        "handle",
        "confirm",
        "recover",
        "check_result",
    }

    assert get_type_hints(ExpressionModelPort.compose)["return"] is ExpressionModelResultV1
    assert get_type_hints(AssistantModulePort.confirm)["request"] is ModuleConfirmationCallV1
    assert get_type_hints(AssistantModulePort.recover)["request"] is ModuleRecoveryCallV1
    assert get_type_hints(AssistantModulePort.check_result)["request"] is ModuleResultCheckCallV1
