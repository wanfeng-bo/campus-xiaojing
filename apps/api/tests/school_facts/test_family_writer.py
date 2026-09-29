from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.domains.students.family_rules import (
    FamilyActorValidation,
    FamilyRelationshipState,
    validate_family_transition,
)
from app.platform.school_facts.contracts import CanonicalFactChangeV1, FactOriginV1

NOW = datetime(2026, 9, 13, 8, 0, tzinfo=UTC)


def _change(kind: str, content: dict[str, object]) -> CanonicalFactChangeV1:
    return CanonicalFactChangeV1(
        operation="upsert",
        fact_kind=kind,
        source_key=f"f3:{kind}:001",
        expected_version=0,
        effective_at=NOW,
        origin=FactOriginV1(sheet_code="f3_family", row_number=2),
        content=content,
    )


def _actor(*, person_id=None, assignment_id=None, action="verify") -> FamilyActorValidation:
    return FamilyActorValidation(
        actor_person_id=person_id or uuid4(),
        actor_assignment_id=assignment_id or uuid4(),
        checked_at=NOW,
        scope_kind="school",
        scope_id=uuid4(),
        action=action,
    )


def test_guardian_contract_has_no_client_supplied_actor_fields() -> None:
    change = _change(
        "guardian_relationship",
        {
            "guardian_person_source_key": "person:parent",
            "student_source_key": "student:001",
            "relationship_kind": "verified_guardian",
            "access_purpose": "guardian_student_profile",
            "verification_source": "school_review",
            "verification_reference": "synthetic:review:001",
            "status": "pending_verification",
        },
    )
    assert change.content is not None
    assert "requested_by_person_id" not in change.content.model_fields_set

    with pytest.raises(ValidationError):
        _change(
            "guardian_relationship",
            {
                **change.content.model_dump(exclude_none=True),
                "verified_by_person_id": str(uuid4()),
            },
        )


def test_person_region_is_district_only_and_voluntary() -> None:
    change = _change(
        "person_region",
        {
            "person_source_key": "person:student",
            "district_code": "330106",
            "district_name": "西湖区",
            "region_purpose": "student_profile",
        },
    )
    assert change.content is not None
    assert change.content.district_code == "330106"
    with pytest.raises(ValidationError):
        _change(
            "person_region",
            {
                "person_source_key": "person:student",
                "district_code": "330106",
                "district_name": "西湖区",
                "region_purpose": "student_profile",
                "street_address": "禁止保存的详细地址",
            },
        )


def test_pending_request_must_be_verified_by_another_current_actor() -> None:
    requester = _actor()
    pending = FamilyRelationshipState(
        source_key="family:001",
        authority_version=1,
        status="pending_verification",
        guardian_person_id=uuid4(),
        student_id=uuid4(),
        requested_by_person_id=requester.actor_person_id,
        requested_by_assignment_id=requester.actor_assignment_id,
        review_of_relationship_id=None,
    )
    with pytest.raises(ValueError, match="申请人与核验人必须分离"):
        validate_family_transition(previous=pending, desired_status="confirmed", actor=requester)

    reviewer = _actor()
    validate_family_transition(previous=pending, desired_status="confirmed", actor=reviewer)


def test_actor_cannot_request_or_verify_own_guardian_relationship() -> None:
    actor = _actor()
    pending = FamilyRelationshipState(
        source_key="family:self",
        authority_version=1,
        status="pending_verification",
        guardian_person_id=actor.actor_person_id,
        student_id=uuid4(),
        requested_by_person_id=uuid4(),
        requested_by_assignment_id=uuid4(),
        review_of_relationship_id=None,
    )
    with pytest.raises(ValueError, match="不能核验自己的家校关系"):
        validate_family_transition(previous=pending, desired_status="confirmed", actor=actor)


def test_terminated_relationship_cannot_be_republished() -> None:
    ended = FamilyRelationshipState(
        source_key="family:ended",
        authority_version=1,
        status="ended",
        guardian_person_id=uuid4(),
        student_id=uuid4(),
        requested_by_person_id=uuid4(),
        requested_by_assignment_id=uuid4(),
        review_of_relationship_id=None,
    )
    with pytest.raises(ValueError, match="新来源键"):
        validate_family_transition(previous=ended, desired_status="confirmed", actor=_actor())


def test_formal_guardian_shape_allows_terminal_unverified_or_fully_verified_only() -> None:
    from sqlalchemy import (
        CheckConstraint,
        Column,
        Integer,
        MetaData,
        String,
        Table,
        create_engine,
    )
    from sqlalchemy.exc import IntegrityError

    from app.domains.students.models import GuardianRelationship

    formal = next(
        constraint
        for constraint in GuardianRelationship.__table__.constraints
        if constraint.name == "ck_guardian_relationships_formal_shape"
    )
    table = Table(
        "guardian_shape",
        MetaData(),
        Column("id", String, primary_key=True),
        Column("authority_version", Integer),
        Column("access_purpose", String),
        Column("status", String),
        Column("guardian_person_id", String),
        Column("requested_by_person_id", String),
        Column("requested_by_assignment_id", String),
        Column("requested_at", String),
        Column("source_id", String),
        Column("verified_by_person_id", String),
        Column("verified_by_assignment_id", String),
        Column("verified_at", String),
        CheckConstraint(str(formal.sqltext)),
    )
    engine = create_engine("sqlite://")
    table.create(engine)
    base = {
        "authority_version": 1,
        "access_purpose": "guardian_student_profile",
        "guardian_person_id": "guardian",
        "requested_by_person_id": "requester",
        "requested_by_assignment_id": "request-assignment",
        "requested_at": "2026-09-13T00:00:00Z",
        "source_id": "source",
    }
    verified = {
        "verified_by_person_id": "verifier",
        "verified_by_assignment_id": "verify-assignment",
        "verified_at": "2026-09-13T01:00:00Z",
    }
    with engine.begin() as connection:
        connection.execute(
            table.insert(), {**base, "id": "pending", "status": "pending_verification"}
        )
        connection.execute(table.update().where(table.c.id == "pending").values(status="ended"))
        pending_ended = connection.execute(table.select().where(table.c.id == "pending")).one()
        assert pending_ended.verified_by_person_id is None

        connection.execute(
            table.insert(),
            {**base, **verified, "id": "confirmed", "status": "confirmed"},
        )
        connection.execute(table.update().where(table.c.id == "confirmed").values(status="revoked"))
        confirmed_revoked = connection.execute(
            table.select().where(table.c.id == "confirmed")
        ).one()
        assert confirmed_revoked.verified_by_person_id == "verifier"
        assert confirmed_revoked.verified_at == verified["verified_at"]

        connection.execute(table.insert(), {**base, "id": "rejected", "status": "rejected"})
        connection.execute(
            table.insert(), {**base, **verified, "id": "verified-ended", "status": "ended"}
        )
        with pytest.raises(IntegrityError):
            connection.execute(
                table.insert(),
                {**base, "id": "bad-confirmed", "status": "confirmed"},
            )
        with pytest.raises(IntegrityError):
            connection.execute(
                table.insert(),
                {
                    **base,
                    "id": "half-terminal",
                    "status": "ended",
                    "verified_by_person_id": "verifier",
                },
            )
        with pytest.raises(IntegrityError):
            connection.execute(
                table.insert(),
                {
                    **base,
                    **verified,
                    "id": "verified-pending",
                    "status": "pending_verification",
                },
            )


def test_formal_family_models_preserve_legacy_rows_without_authority() -> None:
    from app.domains.students.models import GuardianRelationship, PersonRegion

    guardian = GuardianRelationship.__table__
    assert {
        "authority_version",
        "access_purpose",
        "requested_by_person_id",
        "requested_by_assignment_id",
        "requested_at",
        "verified_by_person_id",
        "verified_by_assignment_id",
        "review_of_relationship_id",
        "revoked_at",
        "source_id",
        "import_batch_id",
    } <= set(guardian.c.keys())
    assert guardian.c.authority_version.nullable is True
    assert PersonRegion.__tablename__ == "person_regions"


def test_unique_writer_registers_family_and_region_handlers() -> None:
    from app.composition.school_fact_persistence import SqlAlchemySchoolFactPersistence

    assert callable(SqlAlchemySchoolFactPersistence._upsert_guardian_relationship)
    assert callable(SqlAlchemySchoolFactPersistence._upsert_person_region)


def test_correction_may_keep_adult_or_student_but_not_change_both() -> None:
    from app.domains.students.family_rules import validate_new_family_request

    actor = _actor(action="request")
    old_guardian = uuid4()
    old_student = uuid4()
    terminal = FamilyRelationshipState(
        source_key="family:old",
        authority_version=1,
        status="revoked",
        guardian_person_id=old_guardian,
        student_id=old_student,
        requested_by_person_id=uuid4(),
        requested_by_assignment_id=uuid4(),
        review_of_relationship_id=None,
    )

    validate_new_family_request(
        actor=actor,
        guardian_person_id=old_guardian,
        student_id=uuid4(),
        predecessor=terminal,
    )
    validate_new_family_request(
        actor=actor,
        guardian_person_id=uuid4(),
        student_id=old_student,
        predecessor=terminal,
    )

    with pytest.raises(ValueError, match="至少保留一个关联端点"):
        validate_new_family_request(
            actor=actor,
            guardian_person_id=uuid4(),
            student_id=uuid4(),
            predecessor=terminal,
        )
    with pytest.raises(ValueError, match="完全相同"):
        validate_new_family_request(
            actor=actor,
            guardian_person_id=old_guardian,
            student_id=old_student,
            predecessor=terminal,
        )


def test_new_request_rejects_self_and_live_cross_person_predecessor() -> None:
    from app.domains.students.family_rules import validate_new_family_request

    actor = _actor(action="request")
    with pytest.raises(ValueError, match="自己的家校关系"):
        validate_new_family_request(
            actor=actor,
            guardian_person_id=actor.actor_person_id,
            student_id=uuid4(),
            predecessor=None,
        )

    live = FamilyRelationshipState(
        source_key="family:old",
        authority_version=1,
        status="confirmed",
        guardian_person_id=uuid4(),
        student_id=uuid4(),
        requested_by_person_id=uuid4(),
        requested_by_assignment_id=uuid4(),
        review_of_relationship_id=None,
    )
    with pytest.raises(ValueError, match="先停用旧关系"):
        validate_new_family_request(
            actor=actor,
            guardian_person_id=uuid4(),
            student_id=live.student_id,
            predecessor=live,
        )


@pytest.mark.parametrize(
    ("relationship_kind", "access_purpose"),
    [
        ("verified_guardian", "caregiver_student_summary"),
        ("recognized_caregiver", "guardian_student_profile"),
    ],
)
def test_relationship_kind_cannot_expand_the_wrong_purpose(
    relationship_kind: str, access_purpose: str
) -> None:
    with pytest.raises(ValidationError):
        _change(
            "guardian_relationship",
            {
                "guardian_person_source_key": "person:parent",
                "student_source_key": "student:001",
                "relationship_kind": relationship_kind,
                "access_purpose": access_purpose,
                "verification_source": "school_review",
                "verification_reference": "synthetic:review:002",
                "status": "pending_verification",
            },
        )
