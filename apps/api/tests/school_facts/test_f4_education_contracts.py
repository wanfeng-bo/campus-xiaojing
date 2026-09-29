from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.platform.school_facts.contracts import CanonicalFactChangeV1


def change(kind="education_class", content=None, **extra):
    return CanonicalFactChangeV1.model_validate(
        {
            "schema_version": 3,
            "fact_kind": kind,
            "operation": "upsert",
            "source_key": "class:one",
            "expected_version": 0,
            "effective_at": datetime(2026, 9, 1, tzinfo=UTC),
            "origin": {"sheet_code": "education", "row_number": 1},
            "content": content or {"display_name": "2023班"},
            **extra,
        }
    )


def test_fixed_class_name_is_not_a_year_or_grade():
    value = change()
    assert value.content.display_name == "2023班"
    assert value.content.grade_source_key is None


def test_education_contract_rejects_old_schema_and_foreign_fields():
    with pytest.raises(ValidationError):
        change(schema_version=2)
    with pytest.raises(ValidationError):
        change(content={"display_name": "班", "enrollment_status": "graduated"})


def test_rule_has_explicit_order_and_cycle_without_annual_rows():
    value = change(
        "education_calendar_rule",
        {
            "display_name": "初中学年",
            "stage_source_key": "stage:junior",
            "start_cycle": 2026,
            "month": 9,
            "day": 1,
            "timezone_name": "Asia/Shanghai",
            "grade_source_keys": ("grade:one", "grade:two", "grade:three"),
        },
    )
    assert value.content.start_cycle == 2026
    with pytest.raises(ValidationError):
        change(
            "education_calendar_rule", {**value.content.model_dump(exclude_none=True), "day": 32}
        )


def test_correction_requires_reason_and_existing_version():
    with pytest.raises(ValidationError):
        change(operation="correct")
    value = change(operation="correct", expected_version=2, correction_reason="补正原始登记")
    assert value.operation == "correct"


def test_school_status_is_not_copied_into_student_membership():
    with pytest.raises(ValidationError):
        change(
            "student_class_membership",
            {
                "student_source_key": "student:one",
                "class_source_key": "class:one",
                "membership_status": "assigned",
                "enrollment_status": "active",
            },
        )


def test_education_policy_scope_has_explicit_target_and_optional_conjoined_campus():
    value = change(
        "policy_scope",
        {
            "policy_source_key": "policy:one",
            "education_target_kind": "education_class",
            "education_target_source_key": "class:one",
            "academic_year": 2026,
            "campus_source_key": "campus:one",
        },
    )
    assert value.content.academic_year == 2026


def test_vacancy_quality_is_bound_to_exact_class_position():
    value = change(
        "fact_quality",
        {
            "quality_kind": "negative",
            "negative_code": "vacant_position",
            "owner_kind": "education",
            "education_target_kind": "education_class",
            "education_target_source_key": "class:one",
            "target_fact_kind": "student_class_position",
            "position_source_key": "position:leader",
            "responsibility": "正式登记",
            "basis_reference": "正式确认",
            "verified_at": datetime(2026, 9, 1, tzinfo=UTC),
        },
    )
    assert value.content.position_source_key == "position:leader"


def test_explicit_full_timeline_correction_is_hashed_and_preserves_a_future_segment():
    value = change(content={"display_name": "unused"}).model_dump(mode="json")
    value.update(
        operation="correct",
        expected_version=3,
        content=None,
        correction_reason="结束当前保留独立未来",
    )
    value["replacement_segments"] = [
        {
            "effective_at": "2026-09-01T00:00:00+00:00",
            "valid_to": "2026-10-01T00:00:00+00:00",
            "content": {"display_name": "原班"},
        },
        {
            "effective_at": "2026-11-01T00:00:00+00:00",
            "valid_to": None,
            "content": {"display_name": "未来班"},
        },
    ]
    parsed = CanonicalFactChangeV1.model_validate_json(__import__("json").dumps(value))
    assert len(parsed.replacement_segments) == 2
    value["replacement_segments"][1]["effective_at"] = "2026-09-15T00:00:00+00:00"
    with pytest.raises(ValidationError):
        CanonicalFactChangeV1.model_validate_json(__import__("json").dumps(value))
