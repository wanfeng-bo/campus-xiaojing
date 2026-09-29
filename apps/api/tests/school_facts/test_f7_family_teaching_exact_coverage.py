"""Own-child teaching may certify one exact row, never the whole class/course."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.composition.school_fact_family_teaching_query import _quality
from app.platform.school_facts.f7_coverage_basis import mother_set_digest
from tests.school_facts.test_f5_teaching_query import row


def test_family_teaching_exact_basis_is_checked_then_hidden_from_answer():
    school = uuid4()
    at = datetime(2027, 9, 9, tzinfo=UTC)
    records = [
        row(
            "teaching_assignment",
            "teacher:math",
            {"offering_source_key": "offering:math", "teaching_role": "lead"},
        )
    ]
    basis = {
        "schema_version": 1,
        "school_id": str(school),
        "owner_kind": "education",
        "owner_source_key": "offering:math",
        "target_fact_kind": "teaching_assignment",
        "target_source_key": "teacher:math",
        "field_name": "teaching_role",
        "valid_from": "2026-09-01T00:00:00+00:00",
        "valid_to": None,
        "school_fact_version": 9,
        "member_count": 1,
        "member_sha256": mother_set_digest((("teacher:math", 1),))[1],
    }
    statement = {
        "offering_source_key": "offering:math",
        "teaching_role": "lead",
        "quality_kind": "coverage",
        "coverage_status": "complete",
        "field_name": "teaching_role",
        "target_source_key": "teacher:math",
        "effective_at": "2026-09-01T00:00:00+00:00",
        "coverage_basis": basis,
    }
    correct = _quality(
        [statement],
        school_id=school,
        records=records,
        at=at,
        school_fact_version=10,
    )
    assert correct[0]["verified_exact_complete"] is True
    assert "coverage_basis" not in correct[0]

    basis["member_sha256"] = mother_set_digest((("teacher:other", 1),))[1]
    stale = _quality(
        [statement],
        school_id=school,
        records=records,
        at=at,
        school_fact_version=10,
    )
    assert stale[0]["verified_exact_complete"] is False


def test_family_teaching_self_owned_basis_is_exact_and_owner_is_not_public():
    school = uuid4()
    at = datetime(2027, 9, 9, tzinfo=UTC)
    records = [
        row(
            "teaching_assignment",
            "teacher:math",
            {
                "offering_source_key": "offering:math",
                "teaching_role": "lead",
            },
        )
    ]
    statement = {
        "offering_source_key": "offering:math",
        "quality_owner_source_key": "teacher:math",
        "quality_kind": "coverage",
        "coverage_status": "complete",
        "field_name": "teaching_role",
        "target_source_key": "teacher:math",
        "effective_at": "2026-09-01T00:00:00Z",
        "coverage_basis": {
            "schema_version": 1,
            "school_id": str(school),
            "owner_kind": "education",
            "owner_source_key": "teacher:math",
            "target_fact_kind": "teaching_assignment",
            "target_source_key": "teacher:math",
            "field_name": "teaching_role",
            "valid_from": "2026-09-01T00:00:00Z",
            "valid_to": None,
            "school_fact_version": 9,
            "member_count": 1,
            "member_sha256": mother_set_digest((("teacher:math", 1),))[1],
        },
    }
    current = _quality(
        [statement], school_id=school, records=records, at=at, school_fact_version=10
    )
    assert current[0]["verified_exact_complete"] is True
    assert "quality_owner_source_key" not in current[0]
    for owner in ("teacher:other", "offering:other"):
        with pytest.raises(ValueError):
            _quality(
                [statement | {"quality_owner_source_key": owner}],
                school_id=school,
                records=records,
                at=at,
                school_fact_version=10,
            )
