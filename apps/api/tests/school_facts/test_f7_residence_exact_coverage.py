"""F6 exact coverage can enrich one authorized fact, never a selected roster."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.composition.school_fact_residence_query import (
    _quality_for_answer,
    _verified_exact_residence_coverage,
)
from app.platform.school_facts.f7_coverage_basis import mother_set_digest

AT = datetime(2026, 9, 24, tzinfo=UTC)


def claim(school, *, target="residence:one", target_version=3, school_version=12):
    _, digest = mother_set_digest(((target, target_version),))
    return {
        "quality_source_key": "quality:one",
        "quality_version": 1,
        "target_fact_kind": "student_bed_residence",
        "target_source_key": target,
        "field_name": "bed_name",
        "owner_kind": "person",
        "owner_source_key": "person:student-one",
        "valid_from": AT.isoformat(),
        "valid_to": None,
        "coverage_basis": {
            "schema_version": 1,
            "school_id": str(school),
            "owner_kind": "person",
            "owner_source_key": "person:student-one",
            "target_fact_kind": "student_bed_residence",
            "target_source_key": target,
            "field_name": "bed_name",
            "purpose": None,
            "contact_kind": None,
            "valid_from": AT.isoformat(),
            "valid_to": None,
            "school_fact_version": school_version,
            "member_count": 1,
            "member_sha256": digest,
        },
        "source": {
            "kind": "formal_publication",
            "label": "学校发布的合成住宿资料",
            "published_at": AT.isoformat(),
        },
    }


def verified(rows, school, *, version=3, target="residence:one", school_version=13):
    return _verified_exact_residence_coverage(
        rows,
        school_id=school,
        fact_kind="student_bed_residence",
        source_key=target,
        version=version,
        factual_at=AT,
        school_fact_version=school_version,
        visible_fields={"bed_name"},
    )


def test_only_matching_current_exact_target_becomes_public_complete():
    school = uuid4()
    original = claim(school)
    promoted = verified([original], school)
    assert len(promoted) == 1
    assert promoted[0]["coverage_status"] == "complete"
    assert "coverage_basis" not in str(promoted)
    assert "quality_source_key" not in str(promoted)
    legacy = [{"quality_kind": "coverage", "coverage_status": "complete"}]
    assert _quality_for_answer(legacy, verified_complete=promoted) == promoted
    assert legacy[0]["coverage_status"] == "complete"


@pytest.mark.parametrize("change", ["version", "prior_school_version", "owner", "time", "field"])
def test_stale_or_unreadable_exact_claim_stays_unknown(change):
    school = uuid4()
    row = claim(school)
    actual_version, school_version, visible = 3, 13, {"bed_name"}
    if change == "version":
        actual_version = 4
    elif change == "prior_school_version":
        school_version = 12
    elif change == "owner":
        row["owner_source_key"] = "person:other"
    elif change == "time":
        row["coverage_basis"]["valid_to"] = (AT - timedelta(seconds=1)).isoformat()
    else:
        visible = set()
    original = deepcopy(row)
    promoted = _verified_exact_residence_coverage(
        [row], school_id=school, fact_kind="student_bed_residence",
        source_key="residence:one", version=actual_version, factual_at=AT,
        school_fact_version=school_version, visible_fields=visible,
    )
    assert promoted == []
    assert row == original
    assert _quality_for_answer(
        [{"quality_kind": "coverage", "coverage_status": "complete"}],
        verified_complete=promoted,
    )[0]["coverage_status"] == "unknown"


def test_cross_target_basis_rejected_without_exposing_other_student():
    school = uuid4()
    with pytest.raises(ValueError, match="目标不符"):
        verified([claim(school, target="residence:other")], school)


def test_one_exact_residence_claim_does_not_certify_the_whole_selected_result():
    from tests.school_facts.test_f6_residence_read import fixture, row
    from tests.test_f3_family_read import NOW

    service, actor, state = fixture()
    first = row()
    first["source_key"] = "residence:one"
    exact = claim(actor.school_id, target="residence:one", target_version=1, school_version=7)
    exact["field_name"] = None
    exact["coverage_basis"]["field_name"] = None
    exact["valid_from"] = NOW.isoformat()
    exact["coverage_basis"]["valid_from"] = NOW.isoformat()
    exact["source"]["published_at"] = NOW.isoformat()
    state.payload.update(status="available", records=[first], residence_mode="boarding")
    service.repository.exact_coverage = lambda _actor, **kw: (
        [exact] if kw["source_key"] == "residence:one" else []
    )

    public = service.query(actor=actor).public()
    assert public["status"] == "available"
    assert public["records"][0]["quality"][0]["coverage_status"] == "complete"
    assert public["coverage"]["status"] == "unknown"
