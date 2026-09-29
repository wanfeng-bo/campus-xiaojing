"""Unverified complete claims stay in restricted details, never query conclusions."""

from copy import deepcopy
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.composition.school_fact_education_query import EducationQueryService
from app.platform.school_facts.f7_coverage_basis import mother_set_digest
from tests.school_facts.test_f4_education_query import External, Repository, actor
from tests.school_facts.test_f5_staff_teaching_query import service
from tests.school_facts.test_f5_teaching_query import render


def test_education_exact_current_teacher_claim_marks_only_matching_item():
    who = actor()
    at = datetime(2027, 9, 2, tzinfo=UTC)
    basis = {
        "schema_version": 1,
        "school_id": str(who.school_id),
        "owner_kind": "education",
        "owner_source_key": "class:a",
        "target_fact_kind": "class_teacher_assignment",
        "target_source_key": "teacher:a",
        "field_name": "teacher_role",
        "purpose": None,
        "contact_kind": None,
        "valid_from": "2026-09-01T00:00:00+08:00",
        "valid_to": None,
        "school_fact_version": 9,
        "member_count": 1,
        "member_sha256": mother_set_digest((("teacher:a", 1),))[1],
    }

    class Quality(External):
        def quality_for(self, who, **kwargs):
            return {
                "statements": [
                    {
                        "content": {
                            "quality_kind": "coverage",
                            "coverage_status": "complete",
                            "target_source_key": "teacher:a",
                            "field_name": "teacher_role",
                            "coverage_basis": basis,
                        }
                    }
                ],
                "school_fact_version": 10,
                "proof": SimpleNamespace(school_fact_version=10, revalidate=lambda: None),
            }

    result = EducationQueryService(Repository(), external_reader=Quality()).query(
        who,
        name="2023班",
        fields=("teachers",),
        as_of=at,
    )
    assert result.public_result["teachers"]["coverage"] == "unknown"
    assert result.public_result["teachers"]["items"][0]["quality"] == {
        "record_coverage": "complete"
    }
    assert "member_sha256" not in str(result.public_result)


def test_education_exact_claim_cannot_cross_field_or_current_version():
    who = actor()
    basis = {
        "schema_version": 1,
        "school_id": str(who.school_id),
        "owner_kind": "education",
        "owner_source_key": "class:a",
        "target_fact_kind": "class_teacher_assignment",
        "target_source_key": "teacher:a",
        "field_name": "teacher_role",
        "valid_from": "2026-09-01T00:00:00+08:00",
        "valid_to": None,
        "school_fact_version": 9,
        "member_count": 1,
        "member_sha256": mother_set_digest((("teacher:a", 1),))[1],
    }

    class Quality(External):
        def quality_for(self, who, **kwargs):
            return {
                "statements": [
                    {
                        "content": {
                            "quality_kind": "coverage",
                            "coverage_status": "complete",
                            "target_source_key": "teacher:a",
                            "field_name": "assignment_source_key",
                            "coverage_basis": basis,
                        }
                    }
                ],
                "school_fact_version": 10,
                "proof": SimpleNamespace(school_fact_version=10, revalidate=lambda: None),
            }

    result = EducationQueryService(Repository(), external_reader=Quality()).query(
        who,
        name="2023班",
        fields=("teachers",),
    )
    assert "quality" not in result.public_result["teachers"]["items"][0]
    basis["field_name"] = "assignment_source_key"
    basis["member_sha256"] = mother_set_digest((("teacher:a", 2),))[1]
    result = EducationQueryService(Repository(), external_reader=Quality()).query(
        who,
        name="2023班",
        fields=("teachers",),
    )
    assert "quality" not in result.public_result["teachers"]["items"][0]


def test_teaching_verified_exact_claim_marks_one_teacher_not_whole_offering():
    result = render(
        quality=[
            {
                "offering_source_key": "offering:project",
                "target_source_key": "teacher:project",
                "quality_kind": "coverage",
                "coverage_status": "complete",
                "verified_exact_complete": True,
                "effective_at": "2026-09-01T00:00:00+00:00",
            }
        ]
    )
    assert result["items"][1]["teachers"][0]["quality"] == {"record_coverage": "complete"}
    assert "quality" not in result["items"][1]["teachers"][1]
    assert "quality" not in result["items"][1]


def test_staff_teaching_uses_authorized_exact_basis_not_same_count_other_member():
    who = actor()
    basis = {
        "schema_version": 1,
        "school_id": str(who.school_id),
        "owner_kind": "education",
        "owner_source_key": "offering:a",
        "target_fact_kind": "teaching_assignment",
        "target_source_key": "teaching:a",
        "field_name": "teaching_role",
        "purpose": None,
        "contact_kind": None,
        "valid_from": "2026-09-01T00:00:00+08:00",
        "valid_to": None,
        "school_fact_version": 9,
        "member_count": 1,
        "member_sha256": mother_set_digest((("teaching:a", 1),))[1],
    }
    query, _repo, external, _checks = service()

    def quality(_actor, **_kwargs):
        return {
            "records": [
                {
                    "effective_at": "2026-09-01T00:00:00+08:00",
                    "content": {
                        "quality_kind": "coverage",
                        "coverage_status": "complete",
                        "target_source_key": "teaching:a",
                        "field_name": "teaching_role",
                        "coverage_basis": basis,
                    },
                }
            ],
            "school_fact_version": 10,
            "proof": SimpleNamespace(school_fact_version=10, revalidate=lambda: None),
        }

    external.quality_for = quality
    correct = query.query(who, view="my_teaching").public_result
    assert correct["items"][0]["teachers"][0]["quality"] == {"record_coverage": "complete"}
    assert "quality" not in correct["items"][0]

    basis["member_sha256"] = mother_set_digest((("teaching:other", 1),))[1]
    stale = query.query(who, view="my_teaching").public_result
    assert "quality" not in stale["items"][0]["teachers"][0]


@pytest.mark.parametrize("basis", [None, {"member_count": 1, "verified": True}])
def test_education_complete_claim_cannot_certify_the_authorized_query_subset(basis):
    statement = {
        "content": {
            "quality_kind": "coverage",
            "coverage_status": "complete",
            "target_source_key": None,
            "field_name": None,
            "basis_reference": "UNVERIFIED_WHOLE_CLASS_COMPLETE",
            "coverage_basis": basis,
        }
    }

    class Quality(External):
        def quality_for(self, who, **kwargs):
            return {
                "statements": [statement],
                "proof": SimpleNamespace(school_fact_version=10, revalidate=lambda: None),
            }

    original = deepcopy(statement)
    result = EducationQueryService(Repository(), external_reader=Quality()).query(
        actor(), name="2023班", fields=("teachers",)
    )
    assert result.public_result["teachers"]["coverage"] == "unknown"
    assert "UNVERIFIED_WHOLE_CLASS_COMPLETE" not in str(result.public_result)
    assert statement in result.evidence
    assert statement == original


@pytest.mark.parametrize("basis", [None, {"member_count": 1, "verified": True}])
def test_teaching_complete_claim_does_not_reach_public_evidence(basis):
    statement = {
        "offering_source_key": "offering:math",
        "quality_kind": "coverage",
        "coverage_status": "complete",
        "coverage_basis": basis,
        "effective_at": "2026-09-01T00:00:00+00:00",
        "source": {"summary": "UNVERIFIED_ALL_TEACHERS_COMPLETE"},
    }
    original = deepcopy(statement)
    result = render(quality=[statement])
    assert "complete" not in str(result)
    assert "UNVERIFIED_ALL_TEACHERS_COMPLETE" not in str(result)
    assert statement == original


@pytest.mark.parametrize("coverage", ["partial", "complete"])
def test_education_claim_keeps_current_quality_permission_required(coverage):
    revoked = False

    def revalidate():
        if revoked:
            raise PermissionError("quality read revoked")

    class Quality(External):
        def quality_for(self, who, **kwargs):
            return {
                "statements": [
                    {
                        "content": {
                            "quality_kind": "coverage",
                            "coverage_status": coverage,
                            "basis_reference": "已核实仅登记部分",
                        }
                    }
                ],
                "proof": SimpleNamespace(school_fact_version=10, revalidate=revalidate),
            }

    result = EducationQueryService(Repository(), external_reader=Quality()).query(
        actor(), name="2023班", fields=("teachers",)
    )
    assert result.public_result["teachers"]["coverage"] == (
        "partial" if coverage == "partial" else "unknown"
    )
    revoked = True
    with pytest.raises(PermissionError, match="revoked"):
        result.private_proof.revalidate(actor=actor())


def test_teaching_partial_claim_remains_available():
    result = render(
        quality=[
            {
                "offering_source_key": "offering:math",
                "quality_kind": "coverage",
                "coverage_status": "partial",
                "effective_at": "2026-09-01T00:00:00+00:00",
            }
        ]
    )
    assert result["items"][0]["quality"][0]["coverage"] == "partial"


@pytest.mark.parametrize("state", ["available", "unavailable"])
def test_education_relationship_source_is_visible_only_on_its_own_item(state):
    from tests.school_facts.test_f4_education_query import record

    repo = Repository()
    repo.rows["class_teacher_assignment"].append(
        record(
            "class_teacher_assignment",
            "teacher:b",
            {
                "class_source_key": "class:a",
                "assignment_source_key": "assignment:a",
                "teacher_role": "deputy",
            },
        )
    )
    calls = []

    class Quality(External):
        def quality_for(self, who, **kwargs):
            calls.append(kwargs)
            return {
                "statements": [
                    {
                        "applies_to_source_key": "teacher:a",
                        "content": {
                            "education_target_kind": "class_teacher_assignment",
                            "education_target_source_key": "teacher:a",
                            "target_fact_kind": "class_teacher_assignment",
                            "target_source_key": None,
                            "field_name": "assignment_source_key",
                            "quality_kind": "source",
                            "source_status": state,
                            "basis_reference": "独立任职核对",
                            "verified_at": datetime(2026, 9, 24, tzinfo=UTC).isoformat(),
                        },
                    }
                ],
                "school_fact_version": 10,
                "proof": SimpleNamespace(school_fact_version=10, revalidate=lambda: None),
            }

    result = EducationQueryService(repo, external_reader=Quality()).query(
        actor(), name="2023班", fields=("teachers",)
    )
    teachers = result.public_result["teachers"]
    assert calls[0]["include_target_owner"] is True
    assert teachers["coverage"] == "unknown" and teachers["quality_basis"] == []
    assert teachers["items"][0]["quality_declarations"][0]["status"] == (
        "来源可用" if state == "available" else "来源暂不可用"
    )
    assert "quality_declarations" not in teachers["items"][1]
    assert "teacher:a" not in str(teachers)
