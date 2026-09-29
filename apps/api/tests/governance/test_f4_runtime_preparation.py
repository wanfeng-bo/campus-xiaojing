"""Fixed publication truth is independent of model output and existing F3 children."""

import importlib.util
from datetime import datetime
from pathlib import Path

import pytest

from app.domains.students.education_contracts import EDUCATION_KINDS
from app.domains.students.education_validation import EducationRecord, validate_education_rules

spec = importlib.util.spec_from_file_location(
    "f4_prepare", Path(__file__).resolve().parents[4] / "scripts/f4_engineering/prepare_runtime.py"
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_all_education_kinds_and_six_distinct_fixed_classes_are_prepublished():
    plan = module.build_plan(member_at="2026-09-13T13:00:00+00:00")
    changes = plan["canonical_initial_batch"]["changes"]
    assert EDUCATION_KINDS <= {c["fact_kind"] for c in changes}
    classes = [c for c in changes if c["fact_kind"] == "education_class"]
    assert len(classes) == 6
    assert sum(c["content"]["display_name"] == "2023班" for c in classes) == 2
    assert not any(c["source_key"].startswith("f3:") for c in changes)
    assert not any(c["fact_kind"] == "guardian_relationship" for c in changes)
    validate_education_rules(
        tuple(
            EducationRecord(
                c["fact_kind"],
                c["source_key"],
                datetime.fromisoformat(c["effective_at"]),
                datetime.fromisoformat(c["content"]["valid_to"])
                if c["content"].get("valid_to")
                else None,
                c["content"],
            )
            for c in changes
            if c["fact_kind"] in EDUCATION_KINDS
        )
    )


def test_multiholder_students_are_independent_and_future_child_has_no_current_class():
    plan = module.build_plan(member_at="2026-09-13T13:00:00+00:00")
    members = [
        c["content"]
        for c in plan["canonical_initial_batch"]["changes"]
        if c["fact_kind"] == "student_class_membership"
    ]
    assert not any(c["student_source_key"] == "f3:student:c" for c in members)
    holders = [
        c["content"]
        for c in plan["canonical_initial_batch"]["changes"]
        if c["fact_kind"] == "student_class_position"
        and c["content"]["class_source_key"] == "f4:class:a-2028"
    ]
    assert {c["student_source_key"] for c in holders} == {"f4:student:d", "f4:student:e"}
    first, second = plan["canonical_followup_batches"]
    assert first["canonical"]["changes"][0]["expected_version"] == 0
    assert second["canonical"]["changes"][0]["operation"] == "cancel"
    assert second["canonical"]["changes"][0]["expected_version"] == 1


def test_preparation_is_repeatable_and_rejects_unaware_or_late_publication():
    assert module.build_plan(member_at="2026-09-13T13:00:00+00:00") == module.build_plan(
        member_at="2026-09-13T13:00:00+00:00"
    )
    for value in ("2026-09-13T13:00:00", "2026-10-01T00:00:00+08:00"):
        with pytest.raises(ValueError):
            module.build_plan(member_at=value)
