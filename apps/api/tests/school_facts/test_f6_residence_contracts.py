from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from app.platform.school_facts.contracts import (
    CanonicalFactChangeSetV1,
    CanonicalFactChangeV1,
    canonical_plan_sha256,
    safe_content_payload,
)

NOW = datetime(2026, 9, 22, tzinfo=UTC)
PROFILE = dict(
    location_source_key="location:building",
    usage_kind="building",
    display_name="综合楼住宿区域",
    is_available=True,
)
ENROLLMENT = dict(
    student_source_key="student:one",
    student_number="S001",
    enrollment_status="enrolled",
    residence_mode=None,
    primary_campus_source_key="location:campus",
    administrative_campus_source_key="location:campus",
)


def change(kind="dormitory_building_profile", content=None, **kw):
    payload = dict(
        schema_version=4,
        fact_kind=kind,
        operation="upsert",
        source_key="f6:one",
        expected_version=0,
        effective_at=NOW,
        origin={"sheet_code": "f6", "row_number": 1},
        content=PROFILE if content is None else content,
    )
    payload.update(kw)
    return CanonicalFactChangeV1.model_validate(payload)


def test_profile_uses_an_existing_location_and_explicit_f6_contract():
    value = change()
    assert safe_content_payload(value.content) == PROFILE
    for version in (1, 2, 3):
        payload = value.model_dump()
        payload["schema_version"] = version
        with pytest.raises(ValidationError):
            CanonicalFactChangeV1.model_validate(payload)
    with pytest.raises(ValidationError):
        change(content=dict(PROFILE, usage_kind="whole_school"))


def test_unknown_is_explicit_and_legacy_contracts_still_reject_it():
    value = change("student_school_relationship", ENROLLMENT)
    assert "residence_mode" in safe_content_payload(value.content)
    assert safe_content_payload(value.content)["residence_mode"] is None
    omitted = dict(ENROLLMENT)
    del omitted["residence_mode"]
    with pytest.raises(ValidationError):
        change("student_school_relationship", omitted)
    for version in (1, 2, 3):
        payload = value.model_dump(exclude_unset=True)
        payload["schema_version"] = version
        with pytest.raises(ValidationError):
            CanonicalFactChangeV1.model_validate(payload)


def test_explicit_unknown_survives_wire_and_plan_hash():
    value = change("student_school_relationship", ENROLLMENT)
    plan = CanonicalFactChangeSetV1(
        schema_version=4, source_sha256="a" * 64, base_version=1, changes=(value,)
    )
    reloaded = CanonicalFactChangeSetV1.model_validate_json(plan.model_dump_json(exclude_none=True))
    assert safe_content_payload(reloaded.changes[0].content)["residence_mode"] is None
    assert canonical_plan_sha256(plan) == canonical_plan_sha256(reloaded)
    known = change("student_school_relationship", dict(ENROLLMENT, residence_mode="boarding"))
    assert canonical_plan_sha256(plan) != canonical_plan_sha256(
        plan.model_copy(update={"changes": (known,)})
    )


@pytest.mark.parametrize(
    "kind,content",
    [
        (
            "dorm_room",
            dict(
                room_source_key="location:room",
                authorized_bed_capacity=2,
                accommodation_zone="boarding",
                is_available=True,
            ),
        ),
        (
            "dorm_bed",
            dict(
                room_source_key="location:room",
                business_code="B1",
                display_name="1号床",
                availability="available",
            ),
        ),
        ("dormitory_building_profile", PROFILE),
    ],
)
def test_facilities_accept_bounded_and_complete_timeline_correction(kind, content):
    value = change(kind, dict(content, valid_to=NOW + timedelta(days=7)))
    assert value.content.valid_to == NOW + timedelta(days=7)
    payload = value.model_dump(exclude_unset=True)
    payload.update(
        operation="correct",
        expected_version=1,
        correction_reason="正式纠正",
        content=None,
        replacement_segments=(dict(effective_at=NOW, valid_to=None, content=content),),
    )
    assert len(CanonicalFactChangeV1.model_validate(payload).replacement_segments) == 1


def test_residence_correction_stays_explicit_and_has_no_self_edges():
    with pytest.raises(ValidationError):
        change(
            "student_bed_residence",
            dict(student_source_key="student:one", bed_source_key="bed:one"),
            operation="correct",
        )
    with pytest.raises(ValidationError):
        change(
            "student_bed_residence",
            dict(student_source_key="student:one", bed_source_key="bed:two"),
            expected_version=1,
        )


def test_f6_quality_is_versioned_without_expanding_f2_target_whitelist():
    from app.platform.school_facts.contracts import F2_QUALITY_TARGET_KINDS

    assert "student_bed_residence" not in F2_QUALITY_TARGET_KINDS
    value = change(
        "fact_quality",
        dict(
            quality_kind="negative",
            target_fact_kind="student_bed_residence",
            owner_kind="person",
            person_source_key="person:one",
            negative_code="no_residence",
            basis_reference="正式住宿核对",
            responsibility="住宿资料维护",
            verified_at=NOW,
        ),
    )
    assert value.content.negative_code == "no_residence"


def test_facility_schema_keeps_superseded_slices_out_of_active_exclusion():
    from app.domains.campus import models

    assert hasattr(models, "DormitoryBuildingProfileVersion")
    for cls in (
        models.DormRoomProfileVersion,
        models.DormBedVersion,
        models.DormitoryBuildingProfileVersion,
    ):
        assert "superseded_at" in cls.__table__.c
    from app.domains.students.models import StudentSchoolRelationship

    assert StudentSchoolRelationship.__table__.c.residence_mode.nullable


def test_quality_storage_accepts_only_registered_f6_negatives():
    from app.platform.school_facts.models import FactQuality

    constraints = {
        c.name: str(c.sqltext) for c in FactQuality.__table__.constraints if hasattr(c, "sqltext")
    }
    assert "dormitory_building_profile" in constraints["ck_school_fact_qualities_target"]
    assert "no_residence" in constraints["ck_school_fact_qualities_negative"]


@pytest.mark.parametrize(
    "negative,kind,owner,ref",
    [
        ("no_residence", "student_bed_residence", "school", {}),
        (
            "no_dormitory",
            "dormitory_building_profile",
            "person",
            {"person_source_key": "person:one"},
        ),
    ],
)
def test_formal_residence_negatives_cannot_claim_an_unrelated_owner(negative, kind, owner, ref):
    with pytest.raises(ValidationError):
        change(
            "fact_quality",
            dict(
                quality_kind="negative",
                target_fact_kind=kind,
                owner_kind=owner,
                negative_code=negative,
                basis_reference="正式核对",
                responsibility="资料维护",
                verified_at=NOW,
                **ref,
            ),
        )
