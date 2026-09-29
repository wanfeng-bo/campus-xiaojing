"""F6 matrix never replaces retained F5 bindings or turns slots into published reads."""

import json
from copy import deepcopy
from datetime import timedelta
from uuid import uuid4

import pytest
from scripts.f6_engineering.prepare_samples import build_plan

from tests.governance.test_f3_formal_writer_adapter import NOW


def test_residence_has_one_optional_explicit_dependency_component():
    from app.composition.school_fact_residence_settings import ResidenceConfiguration

    value = ResidenceConfiguration.model_validate(
        {
            "approval_reference": "approved",
            "scope_bindings": [],
            "dependency_education": {"approval_reference": "approved", "scope_bindings": []},
        }
    )
    assert value.dependency_education is not None
    assert (
        ResidenceConfiguration(approval_reference="old", scope_bindings=()).dependency_education
        is None
    )


def test_initial_matrix_is_exact_creation_not_read_and_future_is_blocked():
    from scripts.f6_engineering.runtime_configuration import fixed_matrix

    plan = build_plan()
    rows = fixed_matrix(plan, stage="foundation")
    assert all(r["actor_key"] == "assignment:f1-maintainer" for r in rows)
    location = [r["row"] for r in rows if r["row"]["fact_kind"] == "location"]
    assert len(location) == 2
    assert next(r for r in location if r["action"] == "read")["object_keys"] == [
        "f4:location:a",
        "f4:location:b",
    ]
    assert len(next(r for r in location if r["action"] == "maintain")["creation_keys"]) == 5
    assert not any(r["row"]["family"] == "f6" for r in rows)
    with pytest.raises(ValueError, match="predecessor"):
        fixed_matrix(plan, stage="facilities")


def receipt(plan, stage):
    from app.platform.school_facts.contracts import CanonicalFactChangeSetV4, canonical_plan_sha256

    cs = CanonicalFactChangeSetV4.model_validate_json(json.dumps(plan["batches"][stage]))
    result = {
        "batch_id": str(uuid4()),
        "target_version": cs.base_version + 1,
        "changed_count": len(cs.changes),
        "replayed": False,
    }
    return {
        "stage": stage,
        "status": "succeeded",
        "journal": {
            "state": "succeeded",
            "change_set": cs.model_dump(mode="json", exclude_unset=True),
            "plan_sha256": canonical_plan_sha256(cs),
            "receipt": {
                key: result[key] for key in ("batch_id", "target_version", "changed_count")
            },
        },
        "result": result,
    }


def test_published_locations_require_exact_original_receipt():
    from scripts.f6_engineering.runtime_configuration import fixed_matrix

    plan = build_plan()
    proof = receipt(plan, "foundation")
    rows = fixed_matrix(plan, stage="facilities", published_receipts=(proof,))
    location = next(
        r["row"]
        for r in rows
        if r["row"]["fact_kind"] == "location" and r["row"]["action"] == "read"
    )
    assert len(location["object_keys"]) == 7
    beds = next(r["row"] for r in rows if r["row"]["fact_kind"] == "dorm_bed")
    assert (
        beds["action"] == "maintain" and not beds["object_keys"] and len(beds["creation_keys"]) == 6
    )
    proof["journal"]["change_set"]["changes"][0]["source_key"] = "unapproved"
    with pytest.raises(ValueError):
        fixed_matrix(plan, stage="facilities", published_receipts=(proof,))


def test_receipt_binding_isolated_and_missing_proof_fails(monkeypatch):
    from scripts.f6_engineering import runtime_configuration as m

    original = {
        "directory": {"retained": "f2"},
        "family": {"retained": "f3"},
        "education": {"retained": "f4f5"},
        "f5_features_enabled": True,
    }
    before = deepcopy(original)
    rows = m.fixed_matrix(build_plan(), stage="foundation")
    proofs = []
    for entry in rows:
        row = m.ScopeMatrixRow.model_validate(entry["row"])
        proofs.append(
            {
                "assignment_id": str(m.ASSIGNMENT),
                "scope_owner": "school_facts." + row.family,
                "scope_sha256": row.scope(m.SCHOOL).document_sha256,
                "duty_code": row.duty_code,
                "scope_id": str(uuid4()),
            }
        )
    monkeypatch.setattr(m, "governance_bindings", lambda **kwargs: proofs)
    result = m.configuration_from_receipts(
        original=original, matrix=rows, epoch=NOW, deadline=NOW + timedelta(days=7), receipts=()
    )
    assert original == before
    for name in ("directory", "family", "education"):
        assert result[name] == before[name]
    assert (
        len(result["residence"]["dependency_education"]["dependency_directory"]["scope_bindings"])
        == 2
    )
    assert result["residence"]["dependency_education"]["dependency_family"]["scope_bindings"] == []
    proofs.pop()
    with pytest.raises(ValueError, match="receipt"):
        m.configuration_from_receipts(
            original=original, matrix=rows, epoch=NOW, deadline=NOW + timedelta(days=7), receipts=()
        )
    with pytest.raises(ValueError, match="seven-day"):
        m.configuration_from_receipts(
            original=original, matrix=rows, epoch=NOW, deadline=NOW + timedelta(days=8), receipts=()
        )


def test_quality_owner_is_finite_and_unrelated_actor_cannot_be_bound(monkeypatch):
    from scripts.f6_engineering import runtime_configuration as m

    plan = build_plan()
    rows = m.fixed_matrix(
        plan,
        stage="quality",
        published_receipts=tuple(
            receipt(plan, s) for s in ("foundation", "facilities", "residences")
        ),
    )
    owner = next(
        r["row"]
        for r in rows
        if r["row"]["fact_kind"] == "student_bed_residence" and r["row"]["action"] == "maintain"
    )
    assert owner["quality_owners"] == [["person", "f3:person:student-b"]]
    assert len(owner["object_keys"]) == 4 and owner["creation_keys"] == ["f6:residence:a-m10"]
    assert not any(r["row"]["family"] in {"f4", "f5"} for r in rows)
    monkeypatch.setattr(m, "governance_bindings", lambda **k: [])
    rows[0]["actor_key"] = "f3:assignment:teacher-parent"
    with pytest.raises(ValueError, match="maintainer"):
        m.configuration_from_receipts(
            original={}, matrix=rows, epoch=NOW, deadline=NOW + timedelta(days=7), receipts=()
        )


def test_stage_creation_never_accepts_future_receipt_or_partial_receipt():
    from scripts.f6_engineering.runtime_configuration import fixed_matrix

    plan = build_plan()
    with pytest.raises(ValueError, match="predecessor"):
        fixed_matrix(plan, stage="quality", published_receipts=(receipt(plan, "quality"),))
    proofs = [receipt(plan, s) for s in ("foundation", "facilities")]
    with pytest.raises(ValueError, match="Future"):
        fixed_matrix(plan, stage="foundation", published_receipts=proofs)
    proofs[0]["result"]["changed_count"] -= 1
    with pytest.raises(ValueError):
        fixed_matrix(plan, stage="facilities", published_receipts=proofs[:1])


def test_m09_unknown_preserves_existing_c_school_status_and_coordinates():
    plan = build_plan()
    rows = [
        c
        for c in plan["batches"]["residences"]["changes"]
        if c["source_key"] == "f3:student-school:c"
    ]
    assert len(rows) == 1
    change = rows[0]
    assert change["expected_version"] == 2
    assert change["content"]["enrollment_status"] == "suspended"
    assert change["content"]["residence_mode"] is None
    assert change["content"]["student_number"] == "F3-C001"
    assert change["content"]["primary_campus_source_key"] == "location:f1-main-campus"
    assert change["content"]["administrative_campus_source_key"] == "location:f1-main-campus"


def test_m10_slot_is_never_an_initial_published_fact():
    plan = build_plan()
    assert plan["reserved_followup_keys"] == {"student_bed_residence": ["f6:residence:a-m10"]}
    assert all(
        c["source_key"] != "f6:residence:a-m10"
        for b in plan["batches"].values()
        for c in b["changes"]
    )


def test_m10_has_explicit_separate_reserved_read_and_creation_grants():
    from scripts.f6_engineering.runtime_configuration import fixed_matrix

    plan = build_plan()
    rows = fixed_matrix(
        plan,
        stage="quality",
        published_receipts=tuple(
            receipt(plan, s) for s in ("foundation", "facilities", "residences", "quality")
        ),
    )
    reads = next(
        r["row"]
        for r in rows
        if r["row"]["fact_kind"] == "student_bed_residence" and r["row"]["action"] == "read"
    )
    writes = next(
        r["row"]
        for r in rows
        if r["row"]["fact_kind"] == "student_bed_residence" and r["row"]["action"] == "maintain"
    )
    assert reads["reserved_read_keys"] == ["f6:residence:a-m10"] and not reads["creation_keys"]
    assert writes["creation_keys"] == ["f6:residence:a-m10"] and not writes["reserved_read_keys"]
    from app.composition.school_fact_f6_engineering_cli import ScopeMatrixRow

    with pytest.raises(ValueError):
        ScopeMatrixRow.model_validate({**reads, "action": "maintain"})


def test_impact_matrix_uses_complete_current_revision_including_ended_history():
    from scripts.f6_engineering.runtime_configuration import SCHOOL, impact_rows_from_evidence

    evidence = {
        "snapshot": {
            "school_id": str(SCHOOL),
            "system_identifier": "7682019857215991846",
            "database": "campus_ai_school_facts_f1",
            "schema": "20260914_41",
        },
        "impact_scope": {
            "student_keys": ["f3:student:a", "f3:student:c", "f4:student:d"],
            "fact_kinds": [
                "student_class_membership",
                "student_class_position",
                "teaching_group_member",
            ],
            "all_unsuperseded_segments": True,
            "truncated": False,
        },
        "impacts": [
            {
                "student_key": "f3:student:a",
                "kind": "student_class_position",
                "source_key": "f4:position-holder:a",
                "version": 2,
                "valid_to": "2026-09-14T05:14:18+00:00",
            },
            {
                "student_key": "f4:student:d",
                "kind": "student_class_membership",
                "source_key": "f4:membership:d",
                "version": 1,
            },
            {
                "student_key": "f3:student:a",
                "kind": "teaching_group_member",
                "source_key": "f5:teaching_group_member:independent-a",
                "version": 1,
            },
        ],
    }
    rows = impact_rows_from_evidence(evidence)
    assert len(rows) == 6
    assert all(not r["creation_keys"] for r in rows)
    assert next(r for r in rows if r["fact_kind"] == "student_class_position")["object_keys"] == [
        "f4:position-holder:a"
    ]
    assert not any("student:c" in k for r in rows for k in r["object_keys"])
    for bad in (
        {},
        {**evidence["impact_scope"], "truncated": True},
        {**evidence["impact_scope"], "all_unsuperseded_segments": False},
    ):
        with pytest.raises(ValueError):
            impact_rows_from_evidence({**evidence, "impact_scope": bad})
    with pytest.raises(ValueError):
        impact_rows_from_evidence({**evidence, "impacts": evidence["impacts"][1:]})
    evidence["impacts"][0]["student_key"] = "unselected"
    with pytest.raises(ValueError):
        impact_rows_from_evidence(evidence)


def test_three_field_journal_matches_four_field_original_receipt_lookup():
    from scripts.f6_engineering.runtime_configuration import published_coordinates

    plan = build_plan()
    proof = receipt(plan, "foundation")
    proof["result"] = {**proof["result"], "replayed": True}
    assert set(proof["journal"]["receipt"]) == {"batch_id", "target_version", "changed_count"}
    assert published_coordinates(plan, [proof]) == {
        (c["fact_kind"], c["source_key"]) for c in plan["batches"]["foundation"]["changes"]
    }


@pytest.mark.parametrize("side", ["result", "journal"])
@pytest.mark.parametrize(
    "field,value",
    [
        ("batch_id", "00000000-0000-0000-0000-000000000000"),
        ("batch_id", str(uuid4())),
        ("target_version", 900),
        ("target_version", True),
        ("changed_count", 900),
        ("changed_count", True),
        ("replayed", 1),
        ("replayed", "true"),
        ("extra", "not-a-receipt-field"),
    ],
)
def test_publication_receipt_recovery_rejects_changed_or_malformed_either_copy(side, field, value):
    from scripts.f6_engineering.runtime_configuration import published_coordinates

    plan = build_plan()
    proof = receipt(plan, "foundation")
    proof["result"] = {**proof["result"], "replayed": True}
    proof["journal"]["receipt"] = {**proof["journal"]["receipt"]}
    target = proof["result"] if side == "result" else proof["journal"]["receipt"]
    target[field] = value
    with pytest.raises(ValueError):
        published_coordinates(plan, [proof])


def test_matching_extra_receipt_fields_are_not_accepted_as_original_shape():
    from scripts.f6_engineering.runtime_configuration import published_coordinates

    plan = build_plan()
    proof = receipt(plan, "foundation")
    proof["result"]["extra"] = "not-part-of-contract"
    proof["journal"]["receipt"]["extra"] = "not-part-of-contract"
    with pytest.raises(ValueError):
        published_coordinates(plan, [proof])


def test_journal_rejects_even_boolean_replayed_not_written_by_publication():
    from scripts.f6_engineering.runtime_configuration import published_coordinates

    plan = build_plan()
    proof = receipt(plan, "foundation")
    proof["journal"]["receipt"]["replayed"] = False
    with pytest.raises(ValueError):
        published_coordinates(plan, [proof])
