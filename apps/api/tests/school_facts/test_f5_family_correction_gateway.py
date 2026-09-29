import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.platform.assistant.runtime import AssistantRuntimeError
from app.platform.assistant.tool_loop import ModelToolCall, ToolCallContext
from tests.school_facts.test_f5_family_teaching import fixture


def environment():
    from app.composition.school_fact_family_correction_gateway import (
        NAME,
        FamilyCorrectionToolGateway,
    )
    from app.composition.school_fact_family_correction_product import PublicCorrectionPage

    _, actor, _ = fixture()
    state = SimpleNamespace(actor=actor, status="resolved", reads=0)
    report_id = str(uuid4())

    def own_list(identity, request_id, *, limit):
        state.reads += 1
        return PublicCorrectionPage.model_validate_json(
            json.dumps(
                {
                    "items": [
                        {
                            "report_id": report_id,
                            "fact_kind": "teaching_assignment",
                            "field_name": "assignment_source_key",
                            "status": state.status,
                            "submitted_at": "2026-09-14T00:00:00Z",
                            "resolved_at": "2026-09-14T01:00:00Z",
                        }
                    ]
                }
            )
        )

    gateway = FamilyCorrectionToolGateway(
        resolve_actor=lambda _: SimpleNamespace(actor=state.actor, session_binding="session"),
        product=SimpleNamespace(own_list=own_list),
    )
    turn = gateway.for_turn("identity")
    result = turn.execute(
        ModelToolCall(call_id="one", name=NAME, arguments={}),
        ToolCallContext("identity", "conversation", "turn", "run", 1, uuid4()),
    )
    return state, turn, result


def test_report_tool_is_read_only_and_minimal():
    state, turn, result = environment()
    assert len(turn.descriptors) == 1 and turn.descriptors[0].kind == "read"
    assert result.model_content["items"][0]["tool_context"]["status"] == "resolved"
    assert result.model_content["items"][0]["field_label"] == "任课教师"
    assert result.model_content["items"][0]["status_label"] == "已处理"
    assert "status_meaning" not in result.model_content["items"][0]
    assert "正式处理证明" in result.model_content["interpretation"]["status"]["resolved"]
    assert "正式处理证明" in result.fact_card["items"][0]["status_meaning"]
    assert result.model_content["coverage"] == "limited_page"
    assert "statement" not in str(result.model_content)
    assert "locator" not in str(result.model_content)
    assert result.history_check is not None


def test_report_model_view_preserves_selection_without_telling_unasked_history():
    _, _, result = environment()
    row = result.model_content["items"][0]
    assert set(row["tool_context"]) >= {"report_id", "fact_kind", "field_name", "status"}
    assert not set(row) & {"report_id", "fact_kind", "field_name", "status"}
    assert "当前问题" in result.model_content["interpretation"]["selection"]
    assert "当前事实" in result.model_content["interpretation"]["scope"]


def test_repeated_report_status_explanations_are_once_with_original_cards_and_coordinates():
    from copy import deepcopy

    from app.composition.school_fact_family_correction_gateway import material, model_material

    public = material({"items": [
        {
            "report_id": str(uuid4()), "fact_kind": "teaching_assignment",
            "field_name": "assignment_source_key", "status": status,
            "submitted_at": "2026-09-14T00:00:00Z", "resolved_at": None,
        }
        for status in ("resolved", "resolved", "submitted", "reviewed")
    ]})
    before = deepcopy(public)
    result = model_material(public)
    assert public == before
    assert "status_meaning" not in str(result)
    assert len(result["interpretation"]["status"]) == 3
    assert "正式处理证明" in result["interpretation"]["status"]["resolved"]
    assert "未修改事实" in result["interpretation"]["status"]["reviewed"]
    assert "不代表" in result["interpretation"]["status"]["submitted"]
    assert result["coverage"] == "limited_page" and result["scope"] == public["scope"]
    for row, original in zip(result["items"], public["items"], strict=True):
        assert row["tool_context"]["report_id"] == original["report_id"]
        assert row["tool_context"]["status"] == original["status"]
        assert row["submitted_at"] == original["submitted_at"]
        assert row["target_label"] == original["target_label"]


@pytest.mark.parametrize("change", ["report", "identity"])
def test_current_report_or_guardian_change_invalidates_release_and_history(change):
    state, turn, result = environment()
    if change == "report":
        state.status = "submitted"
    else:
        state.actor = object()
    assert result.history_check.refresh() is False
    with pytest.raises(AssistantRuntimeError):
        turn.revalidate("identity")


def test_model_cannot_supply_another_child_or_write_command():
    from app.composition.school_fact_family_correction_gateway import FamilyCorrectionInput

    with pytest.raises(ValueError):
        FamilyCorrectionInput.model_validate({"student_id": str(uuid4())})
    with pytest.raises(ValueError):
        FamilyCorrectionInput.model_validate({"limit": 21})


def test_teaching_material_collection_does_not_claim_all_teachers():
    from app.composition.school_fact_education_gateway import education_model_material

    result = education_model_material(
        {"kind": "family_teaching", "status": "available", "items": []}
    )
    assert result["collection_scope"]["unreturned_records"] == "not_determined"


def test_legacy_receipt_and_multiple_reports_preserve_separate_state_meanings():
    from app.composition.school_fact_family_correction_gateway import material
    from app.composition.school_fact_family_correction_product import PublicCorrectionPage

    rows = []
    for status in ("submitted", "resolved", "reviewed"):
        rows.append(
            {
                "report_id": str(uuid4()),
                "field_name": "region",
                "status": status,
                "submitted_at": "2026-09-14T00:00:00Z",
                "resolved_at": None if status == "submitted" else "2026-09-14T01:00:00Z",
                "review_explanation": "核对原资料无误" if status == "reviewed" else None,
            }
        )
    page = PublicCorrectionPage.model_validate_json(json.dumps({"items": rows})).model_dump(
        mode="json"
    )
    result = material(page)
    assert len(result["items"]) == 3
    assert result["items"][0]["target_label"] == "本人所报资料"
    assert "未修改事实" in result["items"][2]["status_meaning"]
    assert all(
        "statement" not in item and "resolution_locator_sha256" not in item
        for item in result["items"]
    )
