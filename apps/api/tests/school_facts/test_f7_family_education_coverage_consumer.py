"""A guardian's selected-child answer cannot turn an unproved claim into coverage."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from app.composition.school_fact_family_education_query import _quality
from app.platform.school_facts.f7_coverage_basis import mother_set_digest
from tests.school_facts.test_f4_family_education import fixture
from tests.test_f3_family_read import NOW

AT = datetime(2026, 9, 24, 4, 0, tzinfo=UTC)


def _statement(status: str) -> dict:
    return {
        "field": "class",
        "effective_at": (AT - timedelta(days=1)).isoformat(),
        "verified_at": (AT - timedelta(days=1)).isoformat(),
        "valid_to": None,
        "quality_kind": "coverage",
        "coverage_status": status,
        "field_name": None,
        "scope": "field",
        "source": {},
    }


def test_child_education_does_not_claim_complete_without_verified_mother_set() -> None:
    quality, sources = _quality(
        {"quality": [_statement("complete")]},
        ("class",),
        {"class": []},
        AT,
    )
    assert quality["class"]["coverage"] == "unknown"
    assert all(
        item.get("coverage_status") != "complete"
        for item in quality["class"]["statements"]
    )
    assert sources[0]["fact"] == "fact_quality"


def test_child_education_keeps_explicit_partial_scope_notice() -> None:
    quality, _ = _quality(
        {"quality": [_statement("partial")]},
        ("class",),
        {"class": []},
        AT,
    )
    assert quality["class"]["coverage"] == "partial"


def test_child_exact_teacher_claim_uses_selected_formal_member_without_whole_class_claim():
    school = uuid4()
    basis = {
        "schema_version": 1, "school_id": str(school),
        "owner_kind": "education", "owner_source_key": "class:a",
        "target_fact_kind": "class_teacher_assignment",
        "target_source_key": "teacher:a", "field_name": "teacher_role",
        "purpose": None, "contact_kind": None,
        "valid_from": (AT - timedelta(days=1)).isoformat(), "valid_to": None,
        "school_fact_version": 8, "member_count": 1,
        "member_sha256": mother_set_digest((("teacher:a", 3),))[1],
    }
    raw = {
        "school_fact_version": 9,
        "records": [
            {"fact_kind": kind, "source_key": key, "segments": [{
                "revision": version, "effective_at": (AT - timedelta(days=2)).isoformat(),
                "valid_to": None, "content": {},
            }]}
            for kind, key, version in (
                ("education_class", "class:a", 2),
                ("class_teacher_assignment", "teacher:a", 3),
            )
        ],
        "quality": [_statement("complete") | {
            "field": "teachers", "field_name": "teacher_role", "scope": "item",
            "education_target_kind": "education_class",
            "education_target_source_key": "class:a",
            "target_fact_kind": "class_teacher_assignment",
            "target_source_key": "teacher:a", "coverage_basis": basis,
        }],
    }
    profile = {"teachers": [{"display_name": "陈老师"}]}
    quality, _ = _quality(raw, ("teachers",), profile, AT, school_id=school)
    assert quality["teachers"]["coverage"] == "unknown"
    assert quality["teachers"]["statements"][0]["record_coverage"] == "complete"
    assert quality["teachers"]["statements"][0]["item_index"] == 0
    assert profile["teachers"][0]["quality"] == {"record_coverage": "complete"}
    assert "coverage_basis" not in str(quality)
    for mutation in (
        {"member_sha256": mother_set_digest((("teacher:other", 3),))[1]},
        {"school_id": str(uuid4())},
        {"owner_source_key": "class:other"},
        {"field_name": "assignment_source_key"},
        {"valid_from": (AT + timedelta(days=1)).isoformat()},
    ):
        original = {key: basis[key] for key in mutation}
        basis.update(mutation)
        changed_profile = {"teachers": [{"display_name": "陈老师"}]}
        changed, _ = _quality(raw, ("teachers",), changed_profile, AT, school_id=school)
        assert changed["teachers"]["statements"][0]["coverage_status"] == "unknown"
        assert "record_coverage" not in changed["teachers"]["statements"][0]
        assert "quality" not in changed_profile["teachers"][0]
        basis.update(original)


def test_parent_exact_claim_rechecks_selected_child_and_fact_before_model_use():
    service, actor, state = fixture()
    start = NOW - timedelta(days=1)
    school = actor.school_id
    state.payload.update({
        "status": "available", "school_fact_version": 9,
        "records": [
            {"fact_kind": kind, "source_key": key, "segments": [{
                "revision": version, "effective_at": start.isoformat(),
                "valid_to": None, "content": content,
            }]}
            for kind, key, version, content in (
                ("education_class", "class:a", 1, {}),
                ("class_teacher_assignment", "teacher:a", 3,
                 {"assignment_source_key": "assignment:a", "teacher_role": "primary"}),
            )
        ],
        "related_teachers": [{"assignment_source_key": "assignment:a",
                              "display_name": "陈老师"}],
        "quality": [{
            **_statement("complete"), "field": "teachers", "scope": "item",
            "field_name": "teacher_role", "education_target_kind": "education_class",
            "education_target_source_key": "class:a",
            "target_fact_kind": "class_teacher_assignment",
            "target_source_key": "teacher:a", "effective_at": start.isoformat(),
            "verified_at": start.isoformat(),
            "coverage_basis": {
                "schema_version": 1, "school_id": str(school),
                "owner_kind": "education", "owner_source_key": "class:a",
                "target_fact_kind": "class_teacher_assignment",
                "target_source_key": "teacher:a", "field_name": "teacher_role",
                "valid_from": start.isoformat(), "valid_to": None,
                "school_fact_version": 8, "member_count": 1,
                "member_sha256": mother_set_digest((("teacher:a", 3),))[1],
            },
        }],
    })
    answer = service.query(actor=actor, fields=("teachers",))
    assert answer.public()["profile"]["teachers"][0]["quality"] == {
        "record_coverage": "complete"
    }
    assert answer.evidence.valid(actor)
    state.payload["records"][1]["segments"][0]["revision"] = 4
    assert not answer.evidence.valid(actor)
    state.payload["records"][1]["segments"][0]["revision"] = 3
    state.active = False
    assert not answer.evidence.valid(actor)
