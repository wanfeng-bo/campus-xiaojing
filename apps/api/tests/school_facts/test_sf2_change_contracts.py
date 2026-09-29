from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.platform.school_facts.contracts import (
    CanonicalFactChangeSetV1,
    CanonicalFactChangeV1,
    CanonicalFactContentV1,
    FactOriginV1,
    canonical_plan_sha256,
)

NOW = datetime(2026, 8, 20, 8, tzinfo=UTC)


def _school_change(**overrides: object) -> CanonicalFactChangeV1:
    payload: dict[str, object] = {
        "schema_version": 1,
        "operation": "upsert",
        "fact_kind": "school",
        "source_key": "school:demo",
        "expected_version": 1,
        "effective_at": NOW,
        "origin": {"sheet_code": "schools", "row_number": 2},
        "content": {
            "business_code": "XJ-001",
            "display_name": "小静寄宿制学校",
            "timezone_name": "Asia/Shanghai",
            "lifecycle_status": "active",
        },
    }
    payload.update(overrides)
    return CanonicalFactChangeV1.model_validate(payload)


def test_change_contract_is_strict_and_plan_hash_is_deterministic() -> None:
    change = _school_change()
    change_set = CanonicalFactChangeSetV1(
        schema_version=1,
        source_sha256="a" * 64,
        base_version=7,
        changes=(change,),
    )
    same = CanonicalFactChangeSetV1.model_validate(
        change_set.model_dump(mode="python", exclude_none=True, exclude_unset=True)
    )

    assert canonical_plan_sha256(change_set) == canonical_plan_sha256(same)
    assert len(canonical_plan_sha256(change_set)) == 64


@pytest.mark.parametrize("operation", ["delete", "merge", "checkout", "confirm"])
def test_change_contract_rejects_implicit_or_unregistered_operations(operation: str) -> None:
    with pytest.raises(ValidationError):
        _school_change(operation=operation)


@pytest.mark.parametrize(
    "forbidden",
    [
        {"id": "70f5ef50-97cb-43bf-a5ee-bb17bdaec0dd"},
        {"school_id": "70f5ef50-97cb-43bf-a5ee-bb17bdaec0dd"},
        {"actor_user_id": 9},
        {"permissions": ["master-data:write"]},
        {"table_name": "schools"},
        {"extra": "escape"},
    ],
)
def test_change_contract_rejects_client_authority_database_and_extra_fields(
    forbidden: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        CanonicalFactChangeV1.model_validate(
            {
                **_school_change().model_dump(mode="python"),
                **forbidden,
            }
        )


def test_fact_content_rejects_fields_not_owned_by_declared_fact_kind() -> None:
    with pytest.raises(ValidationError):
        _school_change(
            content={
                "business_code": "XJ-001",
                "display_name": "小静寄宿制学校",
                "timezone_name": "Asia/Shanghai",
                "lifecycle_status": "active",
                "student_number": "STU-001",
            }
        )


@pytest.mark.parametrize("operation", ["end", "revoke"])
def test_end_and_revoke_are_explicit_and_cannot_carry_replacement_content(
    operation: str,
) -> None:
    change = _school_change(operation=operation, content=None)
    assert change.operation == operation

    with pytest.raises(ValidationError):
        _school_change(operation=operation)


def test_upsert_requires_normalized_content_and_expected_version() -> None:
    with pytest.raises(ValidationError):
        _school_change(content=None)
    with pytest.raises(ValidationError):
        _school_change(expected_version=-1)


def test_content_itself_forbids_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        CanonicalFactContentV1.model_validate({"display_name": "学校", "unknown": True})


def test_origin_uses_safe_stable_coordinates_only() -> None:
    origin = FactOriginV1(sheet_code="students", row_number=17, column_code="student_number")
    assert origin.row_number == 17
    with pytest.raises(ValidationError):
        FactOriginV1.model_validate(
            {"sheet_code": "students", "row_number": 17, "filename": "secret.xlsx"}
        )


@pytest.mark.parametrize(
    ("fact_kind", "content"),
    [
        (
            "school",
            {
                "business_code": "XJ-001",
                "display_name": "学校",
                "timezone_name": "Asia/Shanghai",
                "lifecycle_status": "normal",
            },
        ),
        (
            "student",
            {"person_source_key": "person:001", "status": "enrolled_wrong"},
        ),
        (
            "assignment",
            {
                "person_source_key": "person:001",
                "organization_source_key": "organization:001",
                "position_title": "教师",
                "semantic_role_code": "teacher",
                "assignment_kind": "regular",
                "appointment_source": "manual",
                "approval_reference": "approval:001",
                "status": "confirmed",
            },
        ),
        (
            "dorm_bed",
            {
                "room_source_key": "location:room",
                "business_code": "A-1",
                "display_name": "1号床",
                "availability": "maintenance",
            },
        ),
    ],
)
def test_canonical_contract_rejects_unmapped_or_nonstandard_enum_values(
    fact_kind: str,
    content: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        CanonicalFactChangeV1.model_validate(
            {
                "operation": "upsert",
                "fact_kind": fact_kind,
                "source_key": "fact:001",
                "expected_version": 0,
                "effective_at": NOW,
                "origin": {"sheet_code": "facts", "row_number": 2},
                "content": content,
            }
        )


def test_official_contact_owner_shape_is_strict_before_preflight() -> None:
    with pytest.raises(ValidationError):
        CanonicalFactChangeV1.model_validate(
            {
                "operation": "upsert",
                "fact_kind": "official_contact",
                "source_key": "official-contact:001",
                "expected_version": 0,
                "effective_at": NOW,
                "origin": {"sheet_code": "official_contacts", "row_number": 2},
                "content": {
                    "owner_kind": "organization",
                    "location_source_key": "location:001",
                    "contact_kind": "phone",
                    "contact_value": "010-12345678",
                    "lifecycle_status": "active",
                    "visibility_scope": "public",
                },
            }
        )
