from __future__ import annotations

import os
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient
from sqlalchemy import URL, Engine, create_engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, SessionTransaction, sessionmaker
from sqlalchemy.pool import NullPool

from app.composition.school_fact_persistence import (
    SchoolFactPersistenceError,
    SqlAlchemySchoolFactPersistence,
)
from app.composition.school_facts import (
    HmacSchoolFactConfirmationCredentialIssuer,
    InMemoryPrivateSchoolFactSourceStore,
    InMemorySchoolFactPlanStore,
)
from app.core.request_id import assign_request_id
from app.interfaces.http.errors import ApiError
from app.interfaces.http.framework_request import (
    api_error_handler,
    request_validation_error_handler,
)
from app.interfaces.http.school_facts import (
    SchoolFactRequestIdentity,
    create_school_fact_router,
)
from app.platform import contracts as platform_contracts
from app.platform.contracts import CommandExecutionContext, SchoolId, StaffActorContext
from app.platform.identity.context_proof import ContextProofSigner
from app.platform.school_facts.application import SchoolFactChangeRejected, SchoolFactChangeService
from app.platform.school_facts.contracts import CanonicalFactChangeSetV1
from app.platform.school_facts.ports import (
    SchoolFactChangeProvenance,
    SchoolFactRowProvenance,
    SchoolFactSourceArtifact,
)
from tests.master_data.conftest import PostgresRoles

NOW = datetime(2026, 8, 20, 8, tzinfo=UTC)
CONFIRMATION_SIGNING_KEY = b"sf3-confirmation-replay-test-key-32-bytes"


def make_owner_factory(roles: PostgresRoles) -> tuple[sessionmaker[Session], Engine]:
    migration = roles.migration_connection_parameters
    engine = create_engine(
        URL.create(
            "postgresql+psycopg",
            username=str(migration["user"]),
            password=str(migration["password"]),
            host=str(migration["host"]),
            port=int(migration["port"]),
            database=str(migration["dbname"]),
            query={"options": "-c role=campus_ai_db_owner"},
        ),
        poolclass=NullPool,
    )
    return sessionmaker(engine, expire_on_commit=False), engine


def make_school_fact_persistence_factory(
    roles: PostgresRoles,
    owner_factory: sessionmaker[Session],
) -> tuple[sessionmaker[Session], object | None]:
    """Use the isolated Writer only after revision 20 has established its boundary."""

    with owner_factory() as session:
        has_isolated_writer = bool(
            session.scalar(text("SELECT to_regclass('public.school_database_anchor') IS NOT NULL"))
        )
    if not has_isolated_writer:
        return owner_factory, None

    writer = roles.writer_connection_parameters
    engine = create_engine(
        URL.create(
            "postgresql+psycopg",
            username=str(writer["user"]),
            password=str(writer["password"]),
            host=str(writer["host"]),
            port=int(writer["port"]),
            database=str(writer["dbname"]),
        ),
        poolclass=NullPool,
    )
    return sessionmaker(engine, expire_on_commit=False), engine


def seed_staff(
    factory: sessionmaker[Session],
    *,
    school_source_key: str | None = None,
    bootstrap_valid_from: datetime | None = None,
) -> tuple[UUID, int, UUID, UUID]:
    school_id, person_id, unit_id, assignment_id = uuid4(), uuid4(), uuid4(), uuid4()
    binding_id, relationship_source_id = uuid4(), uuid4()
    marker = uuid4().hex
    valid_from = bootstrap_valid_from or NOW - timedelta(days=1)
    with factory.begin() as session:
        has_relationship_content_slices = bool(
            session.execute(
                text("SELECT to_regclass('public.staff_assignment_versions') IS NOT NULL")
            ).scalar_one()
        )
        session.execute(
            text(
                "INSERT INTO schools "
                "(id, source_key, school_code, display_name, timezone_name, status, version) "
                "VALUES (:id, :source_key, :code, 'SF2 学校', 'Asia/Shanghai', 'active', 1)"
            ),
            {
                "id": school_id,
                "source_key": school_source_key or f"school:{marker}",
                "code": f"SF2-{marker}",
            },
        )
        user_id = int(
            session.execute(
                text(
                    "INSERT INTO users "
                    "(provider_code, provider_tenant_key, external_subject, display_name, "
                    "is_active, security_version) VALUES "
                    "('local-test', :tenant, :subject, 'SF2 管理员', true, 1) RETURNING id"
                ),
                {"tenant": marker, "subject": marker},
            ).scalar_one()
        )
        session.execute(
            text(
                "INSERT INTO users "
                "(provider_code, provider_tenant_key, external_subject, display_name, "
                "is_active, security_version) VALUES "
                "('local-test', 'sf2-all', 'parent-001', '学生家长', true, 1) "
                "ON CONFLICT (provider_code, provider_tenant_key, external_subject) DO NOTHING"
            )
        )
        session.execute(
            text(
                "INSERT INTO school_people (id, school_id, source_key, status, version) "
                "VALUES (:id, :school_id, :source_key, 'active', 1)"
            ),
            {"id": person_id, "school_id": school_id, "source_key": f"person:{marker}"},
        )
        session.execute(
            text(
                "INSERT INTO school_fact_sources "
                "(id, school_id, source_key, source_kind, source_ref, schema_version) "
                "VALUES (:id, :school_id, :source_key, 'legacy', :source_key, 1)"
            ),
            {
                "id": relationship_source_id,
                "school_id": school_id,
                "source_key": f"test:relationship-content:{marker}",
            },
        )
        if has_relationship_content_slices:
            session.execute(
                text(
                    "INSERT INTO school_versions "
                    "(id, school_id, business_code, display_name, timezone_name, "
                    "lifecycle_status, valid_from, version, source_id) VALUES "
                    "(:id, :school_id, :business_code, 'SF2 学校', 'Asia/Shanghai', "
                    "'active', :valid_from, 1, :source_id)"
                ),
                {
                    "id": uuid4(),
                    "school_id": school_id,
                    "business_code": f"SF2-{marker}",
                    "valid_from": valid_from,
                    "source_id": relationship_source_id,
                },
            )
        session.execute(
            text(
                "INSERT INTO organization_units "
                "(id, school_id, source_key, kind, status, version) "
                "VALUES (:id, :school_id, :source_key, 'department', 'active', 1)"
            ),
            {"id": unit_id, "school_id": school_id, "source_key": f"org:{marker}"},
        )
        if not has_relationship_content_slices:
            session.execute(
                text(
                    "INSERT INTO organization_unit_versions "
                    "(id, school_id, organization_unit_id, business_code, display_name, "
                    "display_path, valid_from, source_kind, source_ref, status, version) "
                    "VALUES (:id, :school_id, :unit_id, :business_code, 'SF2 数据部门', "
                    "'SF2 数据部门', :valid_from, 'legacy', :source_ref, 'confirmed', 1)"
                ),
                {
                    "id": uuid4(),
                    "school_id": school_id,
                    "unit_id": unit_id,
                    "business_code": f"SF2-ORG-{marker}",
                    "valid_from": valid_from,
                    "source_ref": f"test:relationship-content:{marker}",
                },
            )
        session.execute(
            text(
                "INSERT INTO staff_assignments "
                "(id, school_id, source_key, person_id, organization_unit_id, position_title, "
                "semantic_role_code, assignment_kind, appointment_source, approval_reference, "
                "valid_from, status, version) VALUES "
                "(:id, :school_id, :source_key, :person_id, :unit_id, '数据管理员', "
                "'school_data_admin', 'substantive', 'manual', :approval, :valid_from, "
                "'confirmed', 1)"
            ),
            {
                "id": assignment_id,
                "school_id": school_id,
                "source_key": f"assignment:{marker}",
                "person_id": person_id,
                "unit_id": unit_id,
                "approval": f"approval:{marker}",
                "valid_from": valid_from,
            },
        )
        if has_relationship_content_slices:
            session.execute(
                text(
                    "INSERT INTO staff_assignment_versions "
                    "(id, school_id, assignment_id, person_id, organization_unit_id, "
                    "position_title, semantic_role_code, assignment_kind, appointment_source, "
                    "approval_reference, effective_from, status, target_version, source_ref, "
                    "source_id) VALUES "
                    "(:version_id, :school_id, :assignment_id, :person_id, :unit_id, "
                    "'数据管理员', 'school_data_admin', 'substantive', 'manual', :approval, "
                    ":valid_from, 'confirmed', 1, :source_ref, :source_id)"
                ),
                {
                    "version_id": uuid4(),
                    "school_id": school_id,
                    "assignment_id": assignment_id,
                    "person_id": person_id,
                    "unit_id": unit_id,
                    "approval": f"approval:{marker}",
                    "valid_from": valid_from,
                    "source_ref": f"test:relationship-content:{marker}",
                    "source_id": relationship_source_id,
                },
            )
        session.execute(
            text(
                "INSERT INTO account_person_bindings "
                "(id, school_id, source_key, account_user_id, person_id, valid_from, "
                "status, version) VALUES "
                "(:id, :school_id, :source_key, :user_id, :person_id, :valid_from, "
                "'confirmed', 1)"
            ),
            {
                "id": binding_id,
                "school_id": school_id,
                "source_key": f"login:{marker}",
                "user_id": user_id,
                "person_id": person_id,
                "valid_from": valid_from,
            },
        )
        if has_relationship_content_slices:
            session.execute(
                text(
                    "INSERT INTO account_person_binding_versions "
                    "(id, school_id, binding_id, account_user_id, person_id, provider_code, "
                    "provider_tenant_key, external_subject, effective_from, "
                    "status, target_version, source_ref, source_id) VALUES "
                    "(:version_id, :school_id, :binding_id, :user_id, :person_id, 'local-test', "
                    ":tenant, :subject, :valid_from, 'confirmed', 1, "
                    ":source_ref, :source_id)"
                ),
                {
                    "version_id": uuid4(),
                    "school_id": school_id,
                    "binding_id": binding_id,
                    "user_id": user_id,
                    "person_id": person_id,
                    "tenant": marker,
                    "subject": marker,
                    "valid_from": valid_from,
                    "source_ref": f"test:relationship-content:{marker}",
                    "source_id": relationship_source_id,
                },
            )
    return school_id, user_id, person_id, assignment_id


def context_for(
    *, school_id: UUID, user_id: int, person_id: UUID, assignment_id: UUID, key: str
) -> CommandExecutionContext:
    actor = platform_contracts._issue_staff_actor_context(  # pyright: ignore[reportPrivateUsage]
        account_user_id=user_id,
        school_id=SchoolId(school_id),
        person_id=platform_contracts.PersonId(person_id),
        assignment_id=platform_contracts.AssignmentId(assignment_id),
        assignment_version=1,
        authorization_version=1,
    )
    return platform_contracts._issue_command_execution_context(  # pyright: ignore[reportPrivateUsage]
        actor=actor,
        request_id=uuid4(),
        idempotency_key=key,
    )


def change_set(
    *, school_source_key: str, base_version: int = 1, include_person: bool = False
) -> CanonicalFactChangeSetV1:
    changes: list[dict[str, object]] = [
        {
            "schema_version": 1,
            "operation": "upsert",
            "fact_kind": "school",
            "source_key": school_source_key,
            "expected_version": base_version,
            "effective_at": NOW,
            "origin": {"sheet_code": "schools", "row_number": 2},
            "content": {
                "business_code": f"NEW-{school_source_key[-8:]}",
                "display_name": "SF2 更新学校",
                "timezone_name": "Asia/Shanghai",
                "lifecycle_status": "active",
            },
        }
    ]
    if include_person:
        changes.append(
            {
                "schema_version": 1,
                "operation": "upsert",
                "fact_kind": "person",
                "source_key": f"person:new:{uuid4().hex}",
                "expected_version": 0,
                "effective_at": NOW,
                "origin": {"sheet_code": "people", "row_number": 2},
                "content": {
                    "display_name": "新增人员",
                    "person_kind": "staff",
                    "lifecycle_status": "active",
                },
            }
        )
    return CanonicalFactChangeSetV1.model_validate(
        {
            "schema_version": 1,
            "source_sha256": "d" * 64,
            "base_version": base_version,
            "changes": tuple(changes),
        }
    )


def all_fact_kinds_change_set(*, school_source_key: str) -> CanonicalFactChangeSetV1:
    def fact(
        fact_kind: str,
        source_key: str,
        content: dict[str, object],
        row_number: int,
    ) -> dict[str, object]:
        return {
            "operation": "upsert",
            "fact_kind": fact_kind,
            "source_key": source_key,
            "expected_version": 1 if fact_kind == "school" else 0,
            "effective_at": NOW,
            "origin": {"sheet_code": fact_kind, "row_number": row_number},
            "content": content,
        }

    changes = (
        fact(
            "school",
            school_source_key,
            {
                "business_code": "SF2-ALL",
                "display_name": "SF2 全事实学校",
                "timezone_name": "Asia/Shanghai",
                "lifecycle_status": "active",
            },
            2,
        ),
        fact(
            "person",
            "person:student",
            {
                "business_code": "S-001",
                "display_name": "寄宿学生",
                "person_kind": "other",
                "lifecycle_status": "active",
            },
            3,
        ),
        fact(
            "person",
            "person:parent",
            {
                "display_name": "学生家长",
                "person_kind": "student_contact",
                "lifecycle_status": "active",
            },
            4,
        ),
        fact(
            "student",
            "student:001",
            {"person_source_key": "person:student", "status": "active"},
            5,
        ),
        fact(
            "student_school_relationship",
            "student-school:001",
            {
                "student_source_key": "student:001",
                "student_number": "20260001",
                "enrollment_status": "enrolled",
                "residence_mode": "boarding",
                "primary_campus_source_key": "location:campus",
                "administrative_campus_source_key": "location:campus",
            },
            6,
        ),
        fact(
            "login_mapping",
            "login:parent",
            {
                "provider_code": "local-test",
                "provider_tenant_key": "sf2-all",
                "external_subject": "parent-001",
                "person_source_key": "person:parent",
                "status": "confirmed",
            },
            7,
        ),
        fact(
            "organization",
            "organization:dorm",
            {
                "kind": "department",
                "business_code": "DORM",
                "display_name": "宿管处",
                "display_path": "/宿管处",
                "status": "active",
            },
            8,
        ),
        fact(
            "assignment",
            "assignment:parent",
            {
                "person_source_key": "person:parent",
                "organization_source_key": "organization:dorm",
                "position_title": "家校联系人",
                "semantic_role_code": "family_contact",
                "assignment_kind": "part_time",
                "appointment_source": "manual",
                "approval_reference": "approval:sf2-all",
                "status": "confirmed",
            },
            9,
        ),
        fact(
            "location",
            "location:campus",
            {
                "kind": "campus",
                "business_code": "MAIN",
                "display_name": "主校区",
                "display_path": "/主校区",
                "status": "active",
            },
            10,
        ),
        fact(
            "location",
            "location:room-101",
            {
                "parent_source_key": "location:campus",
                "kind": "dorm_room",
                "business_code": "A-101",
                "display_name": "A栋101",
                "display_path": "/A栋/101",
                "status": "active",
            },
            11,
        ),
        fact(
            "location_alias",
            "location-alias:room-101",
            {
                "location_source_key": "location:room-101",
                "alias": "101宿舍",
                "alias_kind": "common",
            },
            12,
        ),
        fact(
            "dorm_room",
            "dorm-room:101",
            {
                "room_source_key": "location:room-101",
                "authorized_bed_capacity": 4,
                "accommodation_zone": "boys",
                "is_available": True,
            },
            13,
        ),
        fact(
            "dorm_bed",
            "dorm-bed:101-1",
            {
                "room_source_key": "location:room-101",
                "business_code": "A-101-1",
                "display_name": "1号床",
                "availability": "available",
            },
            14,
        ),
        fact(
            "student_bed_residence",
            "residence:student-001",
            {"student_source_key": "student:001", "bed_source_key": "dorm-bed:101-1"},
            15,
        ),
        fact(
            "person_contact",
            "person-contact:parent-mobile",
            {
                "person_source_key": "person:parent",
                "contact_kind": "mobile",
                "contact_value": "13800000000",
                "is_primary": True,
                "visibility_scope": "school_authenticated",
            },
            16,
        ),
        fact(
            "student_contact",
            "student-contact:parent",
            {
                "student_source_key": "student:001",
                "contact_person_source_key": "person:parent",
                "relationship_kind": "father",
                "priority": 1,
                "is_emergency": True,
            },
            17,
        ),
        fact(
            "official_contact",
            "official-contact:dorm",
            {
                "owner_kind": "organization",
                "organization_source_key": "organization:dorm",
                "contact_kind": "phone",
                "contact_value": "010-12345678",
                "service_hours": "全天",
                "lifecycle_status": "active",
                "visibility_scope": "school_authenticated",
            },
            18,
        ),
        fact("policy", "policy:dorm", {}, 19),
        fact(
            "policy_version",
            "policy-version:dorm-v1",
            {
                "policy_source_key": "policy:dorm",
                "business_code": "DORM-001",
                "display_name": "住宿管理制度",
                "category_code": "dormitory",
                "classification": "internal",
                "publishing_organization_source_key": "organization:dorm",
                "published_at": NOW,
                "effective_from": NOW,
                "content_text": "住宿管理制度正文",
            },
            20,
        ),
    )
    return CanonicalFactChangeSetV1.model_validate(
        {
            "source_sha256": "e" * 64,
            "base_version": 1,
            "changes": changes,
        }
    )


@contextmanager
def sf2_environment(
    roles: PostgresRoles,
) -> Iterator[
    tuple[sessionmaker[Session], SqlAlchemySchoolFactPersistence, tuple[UUID, int, UUID, UUID]]
]:
    factory, engine = make_owner_factory(roles)
    persistence_engine = None
    try:
        facts = seed_staff(factory)
        persistence_factory, persistence_engine = make_school_fact_persistence_factory(
            roles,
            factory,
        )
        signing_key = (
            os.environ.get("RLS_CONTEXT_SIGNING_KEY")
            if persistence_engine is not None
            else "master-data-schema-context-signing-key-32-bytes"
        )
        if not signing_key:
            pytest.fail("revision 20 School Fact Writer 测试缺少 RLS_CONTEXT_SIGNING_KEY")
        signer = ContextProofSigner(signing_key.encode("utf-8"))
        persistence = SqlAlchemySchoolFactPersistence(
            persistence_factory,
            lambda context: signer.sign_execution_context(
                context,
                session_nonce_hash=sha256(b"sf2-postgres-session").hexdigest(),
                proof_expires_at=datetime.now(UTC) + timedelta(minutes=30),
            ),
            confirmation_credential_issuer=HmacSchoolFactConfirmationCredentialIssuer(
                CONFIRMATION_SIGNING_KEY
            ),
        )
        yield factory, persistence, facts
    finally:
        if persistence_engine is not None:
            persistence_engine.dispose()  # type: ignore[union-attr]
        engine.dispose()  # type: ignore[union-attr]


def test_confirm_writes_fact_lineage_audit_and_batch_in_one_transaction(
    current_master_data_schema: PostgresRoles,
) -> None:
    with sf2_environment(current_master_data_schema) as (factory, persistence, facts):
        school_id, user_id, person_id, assignment_id = facts
        with factory() as session:
            source_key = str(
                session.execute(
                    text("SELECT source_key FROM schools WHERE id = :id"), {"id": school_id}
                ).scalar_one()
            )
        context = context_for(
            school_id=school_id,
            user_id=user_id,
            person_id=person_id,
            assignment_id=assignment_id,
            key=f"sf2:{uuid4().hex}",
        )
        service = SchoolFactChangeService(reader=persistence, writer=persistence, clock=lambda: NOW)
        changes = change_set(school_source_key=source_key, include_person=True)

        receipt = service.prepare_confirmation(
            change_set=changes, context=context, session_binding="session-sf2"
        )
        result = service.confirm(
            credential=receipt.credential,
            change_set=changes,
            context=context,
            session_binding="session-sf2",
        )

        assert result.changed_count == 2
        assert result.target_version == 2
        with factory() as session:
            assert session.execute(
                text("SELECT display_name, version FROM schools WHERE id = :id"),
                {"id": school_id},
            ).one() == ("SF2 更新学校", 2)
            assert (
                session.execute(
                    text("SELECT count(*) FROM school_fact_change_lineage WHERE batch_id = :id"),
                    {"id": result.batch_id},
                ).scalar_one()
                == 2
            )
            assert (
                session.execute(
                    text(
                        "SELECT count(*) FROM audit_logs "
                        "WHERE event_type = 'school_facts.change_set.applied' AND target_id = :id"
                    ),
                    {"id": str(result.batch_id)},
                ).scalar_one()
                == 1
            )

        replay = service.confirm(
            credential=receipt.credential,
            change_set=changes,
            context=context,
            session_binding="session-sf2",
        )
        assert replay.batch_id == result.batch_id
        assert replay.replayed is True


def test_one_confirmation_applies_all_seventeen_fact_kinds_in_one_transaction(
    current_master_data_schema: PostgresRoles,
) -> None:
    with sf2_environment(current_master_data_schema) as (factory, persistence, facts):
        school_id, user_id, person_id, assignment_id = facts
        with factory() as session:
            school_source_key = str(
                session.execute(
                    text("SELECT source_key FROM schools WHERE id = :id"), {"id": school_id}
                ).scalar_one()
            )
        context = context_for(
            school_id=school_id,
            user_id=user_id,
            person_id=person_id,
            assignment_id=assignment_id,
            key=f"sf2:{uuid4().hex}",
        )
        changes = all_fact_kinds_change_set(school_source_key=school_source_key)
        service = SchoolFactChangeService(
            reader=persistence.for_request(context),
            writer=persistence,
            clock=lambda: NOW,
        )

        report = service.preflight(school_id=SchoolId(school_id), change_set=changes)
        assert report.is_clean is True
        receipt = service.prepare_confirmation(
            change_set=changes,
            context=context,
            session_binding="session-all-facts",
        )
        result = service.confirm(
            credential=receipt.credential,
            change_set=changes,
            context=context,
            session_binding="session-all-facts",
        )

        assert result.changed_count == len(changes.changes)
        with factory() as session:
            kinds = set(
                session.execute(
                    text(
                        "SELECT fact_kind FROM school_fact_change_lineage "
                        "WHERE batch_id = :batch_id"
                    ),
                    {"batch_id": result.batch_id},
                ).scalars()
            )
            assert kinds == {change.fact_kind for change in changes.changes}
            school_version_batch_id = session.execute(
                text(
                    "SELECT import_batch_id FROM school_versions "
                    "WHERE school_id = :school_id ORDER BY version DESC LIMIT 1"
                ),
                {"school_id": school_id},
            ).scalar_one()
            assert school_version_batch_id == result.batch_id
            persisted_batch_text = str(
                session.execute(
                    text(
                        "SELECT jsonb_agg(payload)::text FROM master_data_import_units "
                        "WHERE batch_id = :batch_id"
                    ),
                    {"batch_id": result.batch_id},
                ).scalar_one()
            )
            assert "13800000000" not in persisted_batch_text
            assert "住宿管理制度正文" not in persisted_batch_text


def test_residence_transfer_uses_two_explicit_units_and_two_lineage_rows(
    current_master_data_schema: PostgresRoles,
) -> None:
    with sf2_environment(current_master_data_schema) as (factory, persistence, facts):
        school_id, user_id, person_id, assignment_id = facts
        with factory() as session:
            school_source_key = str(
                session.execute(
                    text("SELECT source_key FROM schools WHERE id = :id"),
                    {"id": school_id},
                ).scalar_one()
            )
        service = SchoolFactChangeService(reader=persistence, writer=persistence, clock=lambda: NOW)

        seed = all_fact_kinds_change_set(school_source_key=school_source_key)
        seed_context = context_for(
            school_id=school_id,
            user_id=user_id,
            person_id=person_id,
            assignment_id=assignment_id,
            key=f"sf2-residence-seed:{uuid4().hex}",
        )
        seed_receipt = service.prepare_confirmation(
            change_set=seed,
            context=seed_context,
            session_binding="sf2-residence-seed",
        )
        seed_result = service.confirm(
            credential=seed_receipt.credential,
            change_set=seed,
            context=seed_context,
            session_binding="sf2-residence-seed",
        )
        assert seed_result.target_version == 2

        second_bed = CanonicalFactChangeSetV1.model_validate(
            {
                "source_sha256": "8" * 64,
                "base_version": 2,
                "changes": (
                    {
                        "operation": "upsert",
                        "fact_kind": "dorm_bed",
                        "source_key": "dorm-bed:101-2",
                        "expected_version": 0,
                        "effective_at": NOW,
                        "origin": {"sheet_code": "dorm_beds", "row_number": 2},
                        "content": {
                            "room_source_key": "location:room-101",
                            "business_code": "A-101-2",
                            "display_name": "2号床",
                            "availability": "available",
                        },
                    },
                ),
            }
        )
        bed_context = context_for(
            school_id=school_id,
            user_id=user_id,
            person_id=person_id,
            assignment_id=assignment_id,
            key=f"sf2-residence-bed:{uuid4().hex}",
        )
        bed_receipt = service.prepare_confirmation(
            change_set=second_bed,
            context=bed_context,
            session_binding="sf2-residence-bed",
        )
        bed_result = service.confirm(
            credential=bed_receipt.credential,
            change_set=second_bed,
            context=bed_context,
            session_binding="sf2-residence-bed",
        )
        assert bed_result.target_version == 3

        transfer_at = NOW + timedelta(days=1)
        transfer = CanonicalFactChangeSetV1.model_validate(
            {
                "source_sha256": "6" * 64,
                "base_version": 3,
                "changes": (
                    {
                        "operation": "end",
                        "fact_kind": "student_bed_residence",
                        "source_key": "residence:student-001",
                        "expected_version": 1,
                        "effective_at": transfer_at,
                        "origin": {"sheet_code": "student_bed_residences", "row_number": 2},
                        "content": None,
                    },
                    {
                        "operation": "upsert",
                        "fact_kind": "student_bed_residence",
                        "source_key": "residence:student-001:move-2",
                        "expected_version": 0,
                        "effective_at": transfer_at,
                        "origin": {"sheet_code": "student_bed_residences", "row_number": 3},
                        "content": {
                            "student_source_key": "student:001",
                            "bed_source_key": "dorm-bed:101-2",
                        },
                    },
                ),
            }
        )
        transfer_context = context_for(
            school_id=school_id,
            user_id=user_id,
            person_id=person_id,
            assignment_id=assignment_id,
            key=f"sf2-residence-transfer:{uuid4().hex}",
        )
        transfer_receipt = service.prepare_confirmation(
            change_set=transfer,
            context=transfer_context,
            session_binding="sf2-residence-transfer",
        )
        result = service.confirm(
            credential=transfer_receipt.credential,
            change_set=transfer,
            context=transfer_context,
            session_binding="sf2-residence-transfer",
        )

        assert result.changed_count == 2
        assert result.target_version == 4
        with factory() as session:
            unit_and_lineage_counts = session.execute(
                text(
                    "SELECT "
                    "(SELECT count(*) FROM master_data_import_units WHERE batch_id = :batch_id), "
                    "(SELECT count(*) FROM school_fact_change_lineage WHERE batch_id = :batch_id)"
                ),
                {"batch_id": result.batch_id},
            ).one()
            assert unit_and_lineage_counts == (2, 2)
            lineage_rows = session.execute(
                text(
                    "SELECT fact_source_key, operation, target_id, target_version "
                    "FROM school_fact_change_lineage WHERE batch_id = :batch_id "
                    "ORDER BY fact_source_key"
                ),
                {"batch_id": result.batch_id},
            ).all()
            assert [(row[0], row[1], row[3]) for row in lineage_rows] == [
                ("residence:student-001", "end", 2),
                ("residence:student-001:move-2", "upsert", 1),
            ]
            old_id, new_id = lineage_rows[0][2], lineage_rows[1][2]
            old_row = session.execute(
                text(
                    "SELECT valid_from, valid_to, bed_id FROM student_bed_residences WHERE id = :id"
                ),
                {"id": old_id},
            ).one()
            new_row = session.execute(
                text(
                    "SELECT valid_from, valid_to, bed_id FROM student_bed_residences WHERE id = :id"
                ),
                {"id": new_id},
            ).one()
            assert old_row[0] == NOW
            assert old_row[1] == transfer_at
            assert new_row[0] == transfer_at
            assert new_row[1] is None
            assert old_row[2] != new_row[2]


def test_same_source_and_plan_reimport_is_a_zero_change_terminal_replay(
    current_master_data_schema: PostgresRoles,
) -> None:
    with sf2_environment(current_master_data_schema) as (factory, persistence, facts):
        school_id, user_id, person_id, assignment_id = facts
        with factory() as session:
            source_key = str(
                session.execute(
                    text("SELECT source_key FROM schools WHERE id = :id"), {"id": school_id}
                ).scalar_one()
            )
        context = context_for(
            school_id=school_id,
            user_id=user_id,
            person_id=person_id,
            assignment_id=assignment_id,
            key=f"sf2:{uuid4().hex}",
        )
        changes = change_set(school_source_key=source_key)
        service = SchoolFactChangeService(
            reader=persistence.for_request(context),
            writer=persistence,
            clock=lambda: NOW,
        )
        first = service.prepare_confirmation(
            change_set=changes, context=context, session_binding="same-file"
        )
        first_result = service.confirm(
            credential=first.credential,
            change_set=changes,
            context=context,
            session_binding="same-file",
        )
        second_context = context_for(
            school_id=school_id,
            user_id=user_id,
            person_id=person_id,
            assignment_id=assignment_id,
            key=f"sf2:{uuid4().hex}",
        )
        second = service.prepare_confirmation(
            change_set=changes,
            context=second_context,
            session_binding="same-file",
        )

        second_result = service.confirm(
            credential=second.credential,
            change_set=changes,
            context=second_context,
            session_binding="same-file",
        )

        assert first_result.changed_count == 1
        assert second_result.changed_count == 0
        assert second_result.replayed is True
        authoritative = service.check_result(
            school_id=SchoolId(school_id),
            change_set=changes,
        )
        assert authoritative is not None
        assert authoritative.batch_id == first_result.batch_id
        assert authoritative.changed_count == 1
        third_report = service.preflight(
            school_id=SchoolId(school_id),
            change_set=changes,
        )
        assert third_report.is_clean is True
        with factory() as session:
            assert (
                session.execute(
                    text("SELECT version FROM schools WHERE id = :id"), {"id": school_id}
                ).scalar_one()
                == 2
            )


def test_explicit_end_and_revoke_close_relationship_facts_without_inference(
    current_master_data_schema: PostgresRoles,
) -> None:
    with sf2_environment(current_master_data_schema) as (factory, persistence, facts):
        school_id, user_id, person_id, assignment_id = facts
        with factory() as session:
            school_source_key = str(
                session.execute(
                    text("SELECT source_key FROM schools WHERE id = :id"), {"id": school_id}
                ).scalar_one()
            )
        context = context_for(
            school_id=school_id,
            user_id=user_id,
            person_id=person_id,
            assignment_id=assignment_id,
            key=f"sf2:{uuid4().hex}",
        )
        service = SchoolFactChangeService(reader=persistence, writer=persistence, clock=lambda: NOW)
        initial = all_fact_kinds_change_set(school_source_key=school_source_key)
        receipt = service.prepare_confirmation(
            change_set=initial, context=context, session_binding="end-revoke"
        )
        service.confirm(
            credential=receipt.credential,
            change_set=initial,
            context=context,
            session_binding="end-revoke",
        )
        effective_at = NOW + timedelta(days=1)
        terminal = CanonicalFactChangeSetV1.model_validate(
            {
                "source_sha256": "f" * 64,
                "base_version": 2,
                "changes": (
                    {
                        "operation": "end",
                        "fact_kind": "person_contact",
                        "source_key": "person-contact:parent-mobile",
                        "expected_version": 1,
                        "effective_at": effective_at,
                        "origin": {"sheet_code": "person_contact", "row_number": 2},
                        "content": None,
                    },
                    {
                        "operation": "revoke",
                        "fact_kind": "student_bed_residence",
                        "source_key": "residence:student-001",
                        "expected_version": 1,
                        "effective_at": effective_at,
                        "origin": {"sheet_code": "student_bed_residence", "row_number": 2},
                        "content": None,
                    },
                ),
            }
        )
        terminal_context = context_for(
            school_id=school_id,
            user_id=user_id,
            person_id=person_id,
            assignment_id=assignment_id,
            key=f"sf2:{uuid4().hex}",
        )
        terminal_receipt = service.prepare_confirmation(
            change_set=terminal,
            context=terminal_context,
            session_binding="end-revoke",
        )

        result = service.confirm(
            credential=terminal_receipt.credential,
            change_set=terminal,
            context=terminal_context,
            session_binding="end-revoke",
        )

        assert result.target_version == 3
        with factory() as session:
            assert (
                session.execute(
                    text(
                        "SELECT valid_to FROM person_contact_points point "
                        "JOIN school_fact_change_lineage lineage ON lineage.target_id = point.id "
                        "WHERE lineage.fact_source_key = 'person-contact:parent-mobile' "
                        "AND lineage.school_id = :school_id "
                        "ORDER BY lineage.target_version LIMIT 1"
                    ),
                    {"school_id": school_id},
                ).scalar_one()
                == effective_at
            )
            assert (
                session.execute(
                    text(
                        "SELECT revoked_at FROM student_bed_residences residence "
                        "JOIN school_fact_change_lineage lineage "
                        "ON lineage.target_id = residence.id "
                        "WHERE lineage.fact_source_key = 'residence:student-001' "
                        "AND lineage.school_id = :school_id "
                        "ORDER BY lineage.target_version LIMIT 1"
                    ),
                    {"school_id": school_id},
                ).scalar_one()
                == effective_at
            )


def test_cross_session_content_actor_and_expiry_are_rejected_before_fact_write(
    current_master_data_schema: PostgresRoles,
) -> None:
    with sf2_environment(current_master_data_schema) as (factory, persistence, facts):
        school_id, user_id, person_id, assignment_id = facts
        with factory() as session:
            source_key = str(
                session.execute(
                    text("SELECT source_key FROM schools WHERE id = :id"), {"id": school_id}
                ).scalar_one()
            )
        context = context_for(
            school_id=school_id,
            user_id=user_id,
            person_id=person_id,
            assignment_id=assignment_id,
            key=f"sf2:{uuid4().hex}",
        )
        changes = change_set(school_source_key=source_key)
        service = SchoolFactChangeService(reader=persistence, writer=persistence, clock=lambda: NOW)
        receipt = service.prepare_confirmation(
            change_set=changes, context=context, session_binding="session-A"
        )
        tampered = change_set(school_source_key=source_key)
        tampered_change = tampered.changes[0].model_copy(
            update={
                "content": tampered.changes[0].content.model_copy(
                    update={"display_name": "伪造的新名称"}
                )
                if tampered.changes[0].content is not None
                else None
            }
        )
        tampered = tampered.model_copy(update={"changes": (tampered_change,)})
        with pytest.raises(SchoolFactChangeRejected):
            service.confirm(
                credential=receipt.credential,
                change_set=tampered,
                context=context,
                session_binding="session-A",
            )
        with pytest.raises(SchoolFactChangeRejected):
            service.confirm(
                credential=receipt.credential,
                change_set=changes,
                context=context,
                session_binding="session-B",
            )
        expired = SchoolFactChangeService(
            reader=persistence,
            writer=persistence,
            clock=lambda: NOW + timedelta(minutes=30),
        )
        with pytest.raises(SchoolFactChangeRejected):
            expired.confirm(
                credential=receipt.credential,
                change_set=changes,
                context=context,
                session_binding="session-A",
            )
        with factory() as session:
            assert (
                session.execute(
                    text("SELECT version FROM schools WHERE id = :id"), {"id": school_id}
                ).scalar_one()
                == 1
            )


class _LostCommitAcknowledgementPersistence(SqlAlchemySchoolFactPersistence):
    @staticmethod
    def _commit_transaction(transaction: SessionTransaction) -> None:
        transaction.commit()
        raise OperationalError(
            "COMMIT",
            {},
            RuntimeError("database-password=canary"),
            connection_invalidated=True,
        )


def test_commit_acknowledgement_loss_probes_durable_result_without_reapplying(
    current_master_data_schema: PostgresRoles,
) -> None:
    with sf2_environment(current_master_data_schema) as (factory, persistence, facts):
        school_id, user_id, person_id, assignment_id = facts
        with factory() as session:
            source_key = str(
                session.execute(
                    text("SELECT source_key FROM schools WHERE id = :id"), {"id": school_id}
                ).scalar_one()
            )
        lossy = _LostCommitAcknowledgementPersistence(
            factory,
            persistence._envelope_factory,  # pyright: ignore[reportPrivateUsage]
        )
        context = context_for(
            school_id=school_id,
            user_id=user_id,
            person_id=person_id,
            assignment_id=assignment_id,
            key=f"sf2:{uuid4().hex}",
        )
        changes = change_set(school_source_key=source_key)
        service = SchoolFactChangeService(reader=lossy, writer=lossy, clock=lambda: NOW)
        receipt = service.prepare_confirmation(
            change_set=changes,
            context=context,
            session_binding="lost-commit-ack",
        )

        result = service.confirm(
            credential=receipt.credential,
            change_set=changes,
            context=context,
            session_binding="lost-commit-ack",
        )

        assert result.replayed is True
        with factory() as session:
            assert (
                session.execute(
                    text("SELECT version FROM schools WHERE id = :id"), {"id": school_id}
                ).scalar_one()
                == 2
            )
            assert (
                session.execute(
                    text(
                        "SELECT count(*) FROM audit_logs "
                        "WHERE event_type = 'school_facts.change_set.applied' AND target_id = :id"
                    ),
                    {"id": str(result.batch_id)},
                ).scalar_one()
                == 1
            )


def test_version_change_after_preflight_rejects_confirmation_without_consuming_it(
    current_master_data_schema: PostgresRoles,
) -> None:
    with sf2_environment(current_master_data_schema) as (factory, persistence, facts):
        school_id, user_id, person_id, assignment_id = facts
        with factory() as session:
            source_key = str(
                session.execute(
                    text("SELECT source_key FROM schools WHERE id = :id"), {"id": school_id}
                ).scalar_one()
            )
        context = context_for(
            school_id=school_id,
            user_id=user_id,
            person_id=person_id,
            assignment_id=assignment_id,
            key=f"sf2:{uuid4().hex}",
        )
        changes = change_set(school_source_key=source_key)
        service = SchoolFactChangeService(reader=persistence, writer=persistence, clock=lambda: NOW)
        receipt = service.prepare_confirmation(
            change_set=changes,
            context=context,
            session_binding="version-race",
        )
        with factory.begin() as session:
            session.execute(
                text("UPDATE schools SET version = 2 WHERE id = :id"), {"id": school_id}
            )

        with pytest.raises(SchoolFactChangeRejected):
            service.confirm(
                credential=receipt.credential,
                change_set=changes,
                context=context,
                session_binding="version-race",
            )

        with factory() as session:
            assert (
                session.execute(
                    text(
                        "SELECT confirmation_consumed_at FROM master_data_import_batches "
                        "WHERE confirmation_token_sha256 = :digest"
                    ),
                    {"digest": sha256(receipt.credential.encode("ascii")).hexdigest()},
                ).scalar_one()
                is None
            )
            assert (
                session.execute(
                    text(
                        "SELECT count(*) FROM school_fact_change_lineage lineage "
                        "JOIN master_data_import_batches batch ON batch.id = lineage.batch_id "
                        "WHERE batch.confirmation_token_sha256 = :digest"
                    ),
                    {"digest": sha256(receipt.credential.encode("ascii")).hexdigest()},
                ).scalar_one()
                == 0
            )


@pytest.mark.parametrize("fault_target", ["second_fact", "audit"])
def test_postgres_failure_rolls_back_every_fact_lineage_and_batch_result(
    current_master_data_schema: PostgresRoles,
    fault_target: str,
) -> None:
    with sf2_environment(current_master_data_schema) as (factory, persistence, facts):
        school_id, user_id, person_id, assignment_id = facts
        with factory() as session:
            source_key = str(
                session.execute(
                    text("SELECT source_key FROM schools WHERE id = :id"), {"id": school_id}
                ).scalar_one()
            )
        context = context_for(
            school_id=school_id,
            user_id=user_id,
            person_id=person_id,
            assignment_id=assignment_id,
            key=f"sf2:{uuid4().hex}",
        )
        changes = change_set(school_source_key=source_key, include_person=True)
        service = SchoolFactChangeService(reader=persistence, writer=persistence, clock=lambda: NOW)
        receipt = service.prepare_confirmation(
            change_set=changes, context=context, session_binding="session-fault"
        )
        table_name = "school_people" if fault_target == "second_fact" else "audit_logs"
        with factory.begin() as session:
            session.execute(
                text(
                    f"CREATE TRIGGER sf2_test_failure BEFORE INSERT ON {table_name} "  # noqa: S608
                    "FOR EACH STATEMENT EXECUTE FUNCTION sf1_reject_history_delete()"
                )
            )
        try:
            with pytest.raises(SchoolFactPersistenceError):
                service.confirm(
                    credential=receipt.credential,
                    change_set=changes,
                    context=context,
                    session_binding="session-fault",
                )
        finally:
            with factory.begin() as session:
                session.execute(text(f"DROP TRIGGER sf2_test_failure ON {table_name}"))  # noqa: S608
        with factory() as session:
            assert (
                session.execute(
                    text("SELECT version FROM schools WHERE id = :id"), {"id": school_id}
                ).scalar_one()
                == 1
            )
            assert (
                session.execute(
                    text(
                        "SELECT count(*) FROM school_fact_change_lineage lineage "
                        "JOIN master_data_import_batches batch ON batch.id = lineage.batch_id "
                        "WHERE batch.confirmation_token_sha256 = :digest"
                    ),
                    {"digest": sha256(receipt.credential.encode("ascii")).hexdigest()},
                ).scalar_one()
                == 0
            )


def test_own_receipt_new_context_reads_exact_scoped_result_without_changing_old_session_contract(
    current_master_data_schema: PostgresRoles,
) -> None:
    with sf2_environment(current_master_data_schema) as (factory, persistence, facts):
        school_id, user_id, person_id, assignment_id = facts
        with factory() as session:
            source_key = str(
                session.execute(
                    text("SELECT source_key FROM schools WHERE id = :id"), {"id": school_id}
                ).scalar_one()
            )
        original_context = context_for(
            school_id=school_id,
            user_id=user_id,
            person_id=person_id,
            assignment_id=assignment_id,
            key=f"own-receipt:{uuid4().hex}",
        )
        changes = change_set(school_source_key=source_key)
        locator = sha256(f"sf3plan_{uuid4().hex}".encode()).hexdigest()
        provenance = SchoolFactChangeProvenance(
            channel="admin",
            raw_source_sha256=changes.source_sha256,
            template_version="school_facts_v1",
            mapping_version="admin_v1",
            mapping_config_sha256="c" * 64,
            source_size_bytes=None,
            worksheet_names=(),
            result_locator_sha256=locator,
        )
        service = SchoolFactChangeService(reader=persistence, writer=persistence, clock=lambda: NOW)
        prepared = service.prepare_confirmation(
            change_set=changes,
            context=original_context,
            session_binding="original-receipt-session",
            provenance=provenance,
        )
        original = service.confirm(
            credential=prepared.credential,
            change_set=changes,
            context=original_context,
            session_binding="original-receipt-session",
        )
        retained_counts = text(
            "SELECT (SELECT count(*) FROM master_data_import_batches), "
            "(SELECT count(*) FROM school_fact_change_lineage), "
            "(SELECT count(*) FROM master_data_import_command_outcomes), "
            "(SELECT count(*) FROM audit_logs)"
        )
        with factory() as session:
            before_counts = session.execute(retained_counts).one()
        current_context = context_for(
            school_id=school_id,
            user_id=user_id,
            person_id=person_id,
            assignment_id=assignment_id,
            key=f"own-receipt-read:{uuid4().hex}",
        )
        allowed = frozenset({("school", source_key)})
        recovered = service.check_own_result_by_locator(
            context=current_context,
            allowed_targets=allowed,
            locator_sha256=locator,
        )
        assert recovered is not None
        assert recovered.result.batch_id == original.batch_id
        assert recovered.result.changed_count == original.changed_count == 1
        assert recovered.result.target_version == original.target_version
        assert recovered.targets[0].source_key == source_key
        page = service.list_own_results(context=current_context, allowed_targets=allowed, limit=1)
        assert page.items == (recovered,) and page.has_more is False
        assert (
            service.check_own_result_by_locator(
                context=current_context,
                allowed_targets=frozenset({("school", "different-target")}),
                locator_sha256=locator,
            )
            is None
        )
        # New independent read does not relax the existing session-bound lookup.
        assert not service.check_result_by_locator(
            school_id=SchoolId(school_id),
            actor_user_id=user_id,
            session_binding="different-login-session",
            locator_sha256=locator,
            context=current_context,
        ).found
        assert service.check_result_by_locator(
            school_id=SchoolId(school_id),
            actor_user_id=user_id,
            session_binding="original-receipt-session",
            locator_sha256=locator,
            context=current_context,
        ).found
        with factory() as session:
            assert session.execute(retained_counts).one() == before_counts
            original_metadata = session.execute(
                text(
                    "SELECT confirmation_session_sha256, status, target_fact_version "
                    "FROM master_data_import_batches WHERE id = :id"
                ),
                {"id": original.batch_id},
            ).one()
            assert original_metadata == (
                sha256(b"original-receipt-session").hexdigest(),
                "applied",
                original.target_version,
            )


def test_persisted_result_locator_survives_service_restart_and_binds_owner(
    current_master_data_schema: PostgresRoles,
) -> None:
    with sf2_environment(current_master_data_schema) as (factory, persistence, facts):
        school_id, user_id, person_id, assignment_id = facts
        with factory() as session:
            source_key = str(
                session.execute(
                    text("SELECT source_key FROM schools WHERE id = :id"), {"id": school_id}
                ).scalar_one()
            )
        context = context_for(
            school_id=school_id,
            user_id=user_id,
            person_id=person_id,
            assignment_id=assignment_id,
            key=f"sf3-locator:{uuid4().hex}",
        )
        changes = change_set(school_source_key=source_key)
        plan_ref = f"sf3plan_{uuid4().hex}"
        locator_sha256 = sha256(plan_ref.encode("ascii")).hexdigest()
        provenance = SchoolFactChangeProvenance(
            channel="admin",
            raw_source_sha256=changes.source_sha256,
            template_version="school_facts_v1",
            mapping_version="admin_v1",
            mapping_config_sha256="c" * 64,
            source_size_bytes=None,
            worksheet_names=(),
            result_locator_sha256=locator_sha256,
        )
        service = SchoolFactChangeService(
            reader=persistence.for_request(context),
            writer=persistence,
            clock=lambda: NOW,
        )
        receipt = service.prepare_confirmation(
            change_set=changes,
            context=context,
            session_binding="durable-result-session",
            provenance=provenance,
        )
        result = service.confirm(
            credential=receipt.credential,
            change_set=changes,
            context=context,
            session_binding="durable-result-session",
        )
        # 历史数据可能已在单活门引入前留下较新的 ready 记录；权威终态仍须优先。
        later_ready_batch_id = uuid4()
        with factory.begin() as session:
            session.execute(
                text(
                    "INSERT INTO master_data_import_batches ("
                    "id, school_id, mode, source_kind, source_ref, source_sha256, "
                    "filename_summary, source_size_bytes, worksheet_names, mapping_version, "
                    "mapping_payload, status, planned_units, succeeded_units, failed_units, "
                    "pending_units, confirmation_card_sha256, request_id, "
                    "idempotency_key_hash, plan_sha256, base_fact_version, "
                    "target_fact_version, confirmation_token_sha256, "
                    "confirmation_expires_at, confirmation_session_sha256, "
                    "staged_by_user_id, staged_by_person_id, staged_by_assignment_id, "
                    "staged_assignment_version, staged_authorization_version, version) "
                    "SELECT :new_id, school_id, mode, source_kind, :source_ref, source_sha256, "
                    "filename_summary, source_size_bytes, worksheet_names, mapping_version, "
                    "mapping_payload, 'ready_for_confirmation', planned_units, 0, 0, "
                    "planned_units, confirmation_card_sha256, :request_id, :idempotency_hash, "
                    "plan_sha256, base_fact_version, target_fact_version, :credential_hash, "
                    ":expires_at, confirmation_session_sha256, staged_by_user_id, "
                    "staged_by_person_id, staged_by_assignment_id, staged_assignment_version, "
                    "staged_authorization_version, 1 "
                    "FROM master_data_import_batches WHERE id = :applied_id"
                ),
                {
                    "new_id": later_ready_batch_id,
                    "source_ref": f"sf2:{later_ready_batch_id}",
                    "request_id": uuid4(),
                    "idempotency_hash": "f" * 64,
                    "credential_hash": "e" * 64,
                    "expires_at": NOW + timedelta(minutes=30),
                    "applied_id": result.batch_id,
                },
            )
            session.execute(
                text(
                    "INSERT INTO master_data_import_units ("
                    "id, school_id, batch_id, sequence, row_number, unit_kind, status, "
                    "payload, payload_sha256, soft_anomalies, initial_soft_anomalies, version) "
                    "SELECT :new_unit_id, school_id, :new_batch_id, sequence, row_number, "
                    "unit_kind, 'ready', payload, payload_sha256, soft_anomalies, "
                    "initial_soft_anomalies, 1 FROM master_data_import_units "
                    "WHERE batch_id = :applied_id"
                ),
                {
                    "new_unit_id": uuid4(),
                    "new_batch_id": later_ready_batch_id,
                    "applied_id": result.batch_id,
                },
            )

        restarted_signer = ContextProofSigner(b"master-data-schema-context-signing-key-32-bytes")
        restarted_persistence = SqlAlchemySchoolFactPersistence(
            factory,
            lambda execution_context: restarted_signer.sign_execution_context(
                execution_context,
                session_nonce_hash=sha256(b"sf2-postgres-session").hexdigest(),
                proof_expires_at=datetime.now(UTC) + timedelta(minutes=30),
            ),
        )
        restarted_service = SchoolFactChangeService(
            reader=restarted_persistence,
            writer=restarted_persistence,
            clock=lambda: NOW,
        )
        restored = restarted_service.check_result_by_locator(
            school_id=SchoolId(school_id),
            actor_user_id=user_id,
            session_binding="durable-result-session",
            locator_sha256=locator_sha256,
        )

        assert restored.found is True
        assert restored.result is not None
        assert restored.result.batch_id == result.batch_id
        assert restored.result.changed_count == 1
        routed_actor = context.actor
        assert isinstance(routed_actor, StaffActorContext)

        def identity() -> SchoolFactRequestIdentity:
            return SchoolFactRequestIdentity(
                actor=routed_actor,
                session_binding="durable-result-session",
                authorized_actions=frozenset({"school_facts.manage"}),
            )

        route_app = FastAPI()
        route_app.middleware("http")(assign_request_id)
        route_app.add_exception_handler(ApiError, api_error_handler)
        route_app.add_exception_handler(
            RequestValidationError,
            request_validation_error_handler,
        )
        route_app.include_router(
            create_school_fact_router(
                change_service=restarted_service,
                source_store=InMemoryPrivateSchoolFactSourceStore(clock=lambda: NOW),
                plan_store=InMemorySchoolFactPlanStore(clock=lambda: NOW),
                identity_dependency=identity,
                clock=lambda: NOW,
            )
        )
        with TestClient(route_app) as client:
            routed = client.get(f"/api/v1/school-facts/plans/{plan_ref}/result")

        assert routed.status_code == 200
        assert routed.json() == {
            "plan_ref": plan_ref,
            "state": "applied",
            "batch_id": str(result.batch_id),
            "target_version": result.target_version,
            "changed_count": 1,
        }
        assert (
            restarted_service.check_result_by_locator(
                school_id=SchoolId(school_id),
                actor_user_id=user_id + 1,
                session_binding="durable-result-session",
                locator_sha256=locator_sha256,
            ).found
            is False
        )
        assert (
            restarted_service.check_result_by_locator(
                school_id=SchoolId(school_id),
                actor_user_id=user_id,
                session_binding="another-session",
                locator_sha256=locator_sha256,
            ).found
            is False
        )
        assert (
            restarted_service.check_result_by_locator(
                school_id=SchoolId(uuid4()),
                actor_user_id=user_id,
                session_binding="durable-result-session",
                locator_sha256=locator_sha256,
            ).found
            is False
        )
        assert (
            restarted_service.check_result_by_locator(
                school_id=SchoolId(school_id),
                actor_user_id=user_id,
                session_binding="durable-result-session",
                locator_sha256="f" * 64,
            ).found
            is False
        )
        with factory() as session:
            mapping_payload, batch_count, outcome_count, audit_count, locator_statuses = (
                session.execute(
                    text(
                        "SELECT batch.mapping_payload, "
                        "(SELECT count(*) FROM master_data_import_batches "
                        " WHERE id = :batch_id), "
                        "(SELECT count(*) FROM master_data_import_command_outcomes "
                        " WHERE batch_id = :batch_id), "
                        "(SELECT count(*) FROM audit_logs WHERE target_id = :target_id) "
                        ", (SELECT array_agg(candidate.status ORDER BY candidate.created_at) "
                        "FROM master_data_import_batches AS candidate "
                        "WHERE candidate.school_id = batch.school_id "
                        "AND candidate.mapping_payload->>'result_locator_sha256' "
                        "= :locator_sha256) "
                        "FROM master_data_import_batches AS batch WHERE batch.id = :batch_id"
                    ),
                    {
                        "batch_id": result.batch_id,
                        "target_id": str(result.batch_id),
                        "locator_sha256": locator_sha256,
                    },
                ).one()
            )
        assert mapping_payload["result_locator_sha256"] == locator_sha256
        assert plan_ref not in str(mapping_payload)
        assert (batch_count, outcome_count, audit_count) == (1, 1, 1)
        assert locator_statuses == ["applied", "ready_for_confirmation"]


def test_adapted_multifile_provenance_persists_only_safe_manifest_and_coordinates(
    current_master_data_schema: PostgresRoles,
) -> None:
    with sf2_environment(current_master_data_schema) as (factory, persistence, facts):
        school_id, user_id, person_id, assignment_id = facts
        with factory() as session:
            school_source_key = str(
                session.execute(
                    text("SELECT source_key FROM schools WHERE id = :id"),
                    {"id": school_id},
                ).scalar_one()
            )
        context = context_for(
            school_id=school_id,
            user_id=user_id,
            person_id=person_id,
            assignment_id=assignment_id,
            key=f"sf3-adapted-provenance:{uuid4().hex}",
        )
        changes = change_set(school_source_key=school_source_key)
        standard_artifact_size = 4096
        provenance = SchoolFactChangeProvenance(
            channel="excel",
            raw_source_sha256="a" * 64,
            template_version="school_facts_v1",
            mapping_version="legacy_multi_v1",
            mapping_config_sha256="b" * 64,
            source_size_bytes=standard_artifact_size,
            worksheet_names=("00_导入说明", "01_学校", "02_人员"),
            source_manifest=(
                SchoolFactSourceArtifact(
                    logical_source_code="school_file",
                    source_sha256="c" * 64,
                    size_bytes=6 * 1024 * 1024,
                ),
                SchoolFactSourceArtifact(
                    logical_source_code="staff_file",
                    source_sha256="d" * 64,
                    size_bytes=6 * 1024 * 1024,
                ),
            ),
            row_lineage=(
                SchoolFactRowProvenance(
                    fact_kind="school",
                    fact_source_key=school_source_key,
                    raw_source_sha256="c" * 64,
                    source_slot="school_file",
                    source_sheet_code="legacy_school",
                    source_row_number=27,
                    target_field_codes=(
                        "business_code",
                        "display_name",
                        "lifecycle_status",
                        "timezone_name",
                    ),
                    source_field_codes=(
                        "school_code",
                        "school_name",
                        "school_state",
                        "school_timezone",
                    ),
                ),
            ),
        )
        service = SchoolFactChangeService(
            reader=persistence,
            writer=persistence,
            clock=lambda: NOW,
        )
        receipt = service.prepare_confirmation(
            change_set=changes,
            context=context,
            session_binding="adapted-provenance-session",
            provenance=provenance,
        )
        result = service.confirm(
            credential=receipt.credential,
            change_set=changes,
            context=context,
            session_binding="adapted-provenance-session",
        )

        with factory() as session:
            source_size_bytes, filename_summary, worksheet_names, mapping_payload = session.execute(
                text(
                    "SELECT source_size_bytes, filename_summary, worksheet_names, mapping_payload "
                    "FROM master_data_import_batches WHERE id = :batch_id"
                ),
                {"batch_id": result.batch_id},
            ).one()

    assert source_size_bytes == standard_artifact_size
    assert source_size_bytes <= 10 * 1024 * 1024
    assert source_size_bytes != 12 * 1024 * 1024
    assert filename_summary == "school-facts.xlsx"
    assert worksheet_names == ["00_导入说明", "01_学校", "02_人员"]
    assert mapping_payload == {
        "schema_version": 1,
        "raw_source_sha256": "a" * 64,
        "template_version": "school_facts_v1",
        "mapping_version": "legacy_multi_v1",
        "mapping_config_sha256": "b" * 64,
        "result_locator_sha256": None,
        "source_manifest": [
            {
                "logical_source_code": "school_file",
                "source_sha256": "c" * 64,
                "size_bytes": 6 * 1024 * 1024,
            },
            {
                "logical_source_code": "staff_file",
                "source_sha256": "d" * 64,
                "size_bytes": 6 * 1024 * 1024,
            },
        ],
        "row_lineage": [
            {
                "fact_kind": "school",
                "fact_source_key": school_source_key,
                "raw_source_sha256": "c" * 64,
                "source_slot": "school_file",
                "source_sheet_code": "legacy_school",
                "source_row_number": 27,
                "field_lineage": [
                    {
                        "target_field_code": "business_code",
                        "source_field_code": "school_code",
                    },
                    {
                        "target_field_code": "display_name",
                        "source_field_code": "school_name",
                    },
                    {
                        "target_field_code": "lifecycle_status",
                        "source_field_code": "school_state",
                    },
                    {
                        "target_field_code": "timezone_name",
                        "source_field_code": "school_timezone",
                    },
                ],
            }
        ],
    }
    serialized = str(mapping_payload)
    for forbidden in (
        "学校人事最终版(真实姓名).xlsx",
        "在职员工花名册",
        "13800000000-SENSITIVE-CELL",
        "original_status_value",
        "在岗",
    ):
        assert forbidden not in serialized


def test_prepare_same_key_replays_one_ready_batch_across_restart_and_applied_never_stages_again(
    current_master_data_schema: PostgresRoles,
) -> None:
    with sf2_environment(current_master_data_schema) as (factory, persistence, facts):
        school_id, user_id, person_id, assignment_id = facts
        with factory() as session:
            school_source_key = str(
                session.execute(
                    text("SELECT source_key FROM schools WHERE id = :id"),
                    {"id": school_id},
                ).scalar_one()
            )
        changes = change_set(school_source_key=school_source_key)
        locator_sha256 = sha256(f"sf3plan_{uuid4().hex}".encode("ascii")).hexdigest()
        provenance = SchoolFactChangeProvenance(
            channel="admin",
            raw_source_sha256=changes.source_sha256,
            template_version="school_facts_v1",
            mapping_version="admin_v1",
            mapping_config_sha256="a" * 64,
            source_size_bytes=None,
            worksheet_names=(),
            result_locator_sha256=locator_sha256,
        )
        service = SchoolFactChangeService(
            reader=persistence,
            writer=persistence,
            clock=lambda: NOW,
        )
        contexts = tuple(
            context_for(
                school_id=school_id,
                user_id=user_id,
                person_id=person_id,
                assignment_id=assignment_id,
                key="sf3-prepare-same-idempotency-key",
            )
            for index in range(3)
        )

        receipts = tuple(
            service.prepare_confirmation(
                change_set=changes,
                context=context,
                session_binding="single-active-session",
                provenance=provenance,
            )
            for context in contexts
        )

        with factory() as session:
            batch_id, batch_count, unit_count = session.execute(
                text(
                    "SELECT min(id::text)::uuid, count(*), "
                    "(SELECT count(*) FROM master_data_import_units AS unit "
                    " JOIN master_data_import_batches AS child ON child.id = unit.batch_id "
                    " WHERE child.school_id = :school_id "
                    " AND child.mapping_payload->>'result_locator_sha256' = :locator) "
                    "FROM master_data_import_batches "
                    "WHERE school_id = :school_id "
                    "AND mapping_payload->>'result_locator_sha256' = :locator"
                ),
                {"school_id": school_id, "locator": locator_sha256},
            ).one()
        assert batch_count == 1
        assert unit_count == len(changes.changes)
        assert len({receipt.credential for receipt in receipts}) == 1
        assert len({receipt.expires_at for receipt in receipts}) == 1

        restarted_signer = ContextProofSigner(b"master-data-schema-context-signing-key-32-bytes")
        restarted_persistence = SqlAlchemySchoolFactPersistence(
            factory,
            lambda execution_context: restarted_signer.sign_execution_context(
                execution_context,
                session_nonce_hash=sha256(b"sf2-postgres-session").hexdigest(),
                proof_expires_at=datetime.now(UTC) + timedelta(minutes=30),
            ),
            confirmation_credential_issuer=HmacSchoolFactConfirmationCredentialIssuer(
                CONFIRMATION_SIGNING_KEY
            ),
        )
        restarted_service = SchoolFactChangeService(
            reader=restarted_persistence,
            writer=restarted_persistence,
            clock=lambda: NOW,
        )
        lost_response_retry = restarted_service.prepare_confirmation(
            change_set=changes,
            context=contexts[0],
            session_binding="single-active-session",
            provenance=provenance,
        )
        assert lost_response_retry == receipts[0]

        different_key = context_for(
            school_id=school_id,
            user_id=user_id,
            person_id=person_id,
            assignment_id=assignment_id,
            key="sf3-prepare-different-key",
        )
        with pytest.raises(SchoolFactChangeRejected):
            service.prepare_confirmation(
                change_set=changes,
                context=different_key,
                session_binding="single-active-session",
                provenance=provenance,
            )
        result = service.confirm(
            credential=receipts[0].credential,
            change_set=changes,
            context=contexts[-1],
            session_binding="single-active-session",
        )
        assert result.batch_id == batch_id
        with pytest.raises(SchoolFactChangeRejected):
            service.prepare_confirmation(
                change_set=changes,
                context=contexts[-1],
                session_binding="single-active-session",
                provenance=provenance,
            )
        with factory() as session:
            assert (
                session.execute(
                    text(
                        "SELECT count(*) FROM master_data_import_batches "
                        "WHERE school_id = :school_id "
                        "AND mapping_payload->>'result_locator_sha256' = :locator"
                    ),
                    {"school_id": school_id, "locator": locator_sha256},
                ).scalar_one()
                == 1
            )


def test_concurrent_same_key_prepare_calls_replay_one_credential_and_bounded_units(
    current_master_data_schema: PostgresRoles,
) -> None:
    with sf2_environment(current_master_data_schema) as (factory, persistence, facts):
        school_id, user_id, person_id, assignment_id = facts
        with factory() as session:
            school_source_key = str(
                session.execute(
                    text("SELECT source_key FROM schools WHERE id = :id"),
                    {"id": school_id},
                ).scalar_one()
            )
        changes = change_set(school_source_key=school_source_key)
        locator_sha256 = sha256(f"sf3plan_{uuid4().hex}".encode("ascii")).hexdigest()
        provenance = SchoolFactChangeProvenance(
            channel="admin",
            raw_source_sha256=changes.source_sha256,
            template_version="school_facts_v1",
            mapping_version="admin_v1",
            mapping_config_sha256="b" * 64,
            source_size_bytes=None,
            worksheet_names=(),
            result_locator_sha256=locator_sha256,
        )
        service = SchoolFactChangeService(
            reader=persistence,
            writer=persistence,
            clock=lambda: NOW,
        )

        def prepare(index: int) -> str:
            context = context_for(
                school_id=school_id,
                user_id=user_id,
                person_id=person_id,
                assignment_id=assignment_id,
                key="sf3-prepare-concurrent-same-key",
            )
            return service.prepare_confirmation(
                change_set=changes,
                context=context,
                session_binding="concurrent-single-active-session",
                provenance=provenance,
            ).credential

        with ThreadPoolExecutor(max_workers=8) as executor:
            credentials = tuple(executor.map(prepare, range(8)))

        with factory() as session:
            batch_count, unit_count, active_credential_sha256 = session.execute(
                text(
                    "SELECT count(*), "
                    "(SELECT count(*) FROM master_data_import_units AS unit "
                    " JOIN master_data_import_batches AS child ON child.id = unit.batch_id "
                    " WHERE child.school_id = :school_id "
                    " AND child.mapping_payload->>'result_locator_sha256' = :locator), "
                    "max(confirmation_token_sha256) "
                    "FROM master_data_import_batches "
                    "WHERE school_id = :school_id "
                    "AND mapping_payload->>'result_locator_sha256' = :locator"
                ),
                {"school_id": school_id, "locator": locator_sha256},
            ).one()

        assert batch_count == 1
        assert unit_count == len(changes.changes)
        assert len(set(credentials)) == 1
        assert active_credential_sha256 == sha256(credentials[0].encode("ascii")).hexdigest()


def test_concurrent_different_prepare_keys_have_one_winner_without_invalidating_it(
    current_master_data_schema: PostgresRoles,
) -> None:
    with sf2_environment(current_master_data_schema) as (factory, persistence, facts):
        school_id, user_id, person_id, assignment_id = facts
        with factory() as session:
            school_source_key = str(
                session.execute(
                    text("SELECT source_key FROM schools WHERE id = :id"),
                    {"id": school_id},
                ).scalar_one()
            )
        changes = change_set(school_source_key=school_source_key)
        locator_sha256 = sha256(f"sf3plan_{uuid4().hex}".encode("ascii")).hexdigest()
        provenance = SchoolFactChangeProvenance(
            channel="admin",
            raw_source_sha256=changes.source_sha256,
            template_version="school_facts_v1",
            mapping_version="admin_v1",
            mapping_config_sha256="c" * 64,
            source_size_bytes=None,
            worksheet_names=(),
            result_locator_sha256=locator_sha256,
        )
        service = SchoolFactChangeService(
            reader=persistence,
            writer=persistence,
            clock=lambda: NOW,
        )

        def prepare(index: int) -> object:
            try:
                return service.prepare_confirmation(
                    change_set=changes,
                    context=context_for(
                        school_id=school_id,
                        user_id=user_id,
                        person_id=person_id,
                        assignment_id=assignment_id,
                        key=f"sf3-competing-key-{index}",
                    ),
                    session_binding="concurrent-different-key-session",
                    provenance=provenance,
                )
            except Exception as exc:
                return exc

        with ThreadPoolExecutor(max_workers=8) as executor:
            outcomes = tuple(executor.map(prepare, range(8)))

        winners = tuple(outcome for outcome in outcomes if not isinstance(outcome, Exception))
        losers = tuple(outcome for outcome in outcomes if isinstance(outcome, Exception))
        assert len(winners) == 1
        assert len(losers) == 7
        assert all(isinstance(outcome, SchoolFactChangeRejected) for outcome in losers)
        winning_receipt = winners[0]
        assert hasattr(winning_receipt, "credential")

        with factory() as session:
            batch_count, unit_count, stored_digest = session.execute(
                text(
                    "SELECT count(*), "
                    "(SELECT count(*) FROM master_data_import_units AS unit "
                    " JOIN master_data_import_batches AS child ON child.id = unit.batch_id "
                    " WHERE child.school_id = :school_id "
                    " AND child.mapping_payload->>'result_locator_sha256' = :locator), "
                    "max(confirmation_token_sha256) "
                    "FROM master_data_import_batches "
                    "WHERE school_id = :school_id "
                    "AND mapping_payload->>'result_locator_sha256' = :locator"
                ),
                {"school_id": school_id, "locator": locator_sha256},
            ).one()
        assert batch_count == 1
        assert unit_count == len(changes.changes)
        assert (
            stored_digest
            == sha256(
                winning_receipt.credential.encode("ascii")  # type: ignore[union-attr]
            ).hexdigest()
        )


def test_expired_unconfirmed_batch_is_physically_purged_after_restart_and_can_reprepare(
    current_master_data_schema: PostgresRoles,
) -> None:
    with sf2_environment(current_master_data_schema) as (factory, persistence, facts):
        school_id, user_id, person_id, assignment_id = facts
        with factory() as session:
            school_source_key = str(
                session.execute(
                    text("SELECT source_key FROM schools WHERE id = :id"),
                    {"id": school_id},
                ).scalar_one()
            )
        changes = change_set(school_source_key=school_source_key)
        plan_ref = f"sf3plan_{uuid4().hex}"
        locator_sha256 = sha256(plan_ref.encode("ascii")).hexdigest()
        provenance = SchoolFactChangeProvenance(
            channel="admin",
            raw_source_sha256=changes.source_sha256,
            template_version="school_facts_v1",
            mapping_version="admin_v1",
            mapping_config_sha256="d" * 64,
            source_size_bytes=None,
            worksheet_names=(),
            result_locator_sha256=locator_sha256,
        )
        context = context_for(
            school_id=school_id,
            user_id=user_id,
            person_id=person_id,
            assignment_id=assignment_id,
            key="sf3-expired-lost-response",
        )
        current_now = datetime.now(UTC)
        service = SchoolFactChangeService(
            reader=persistence.for_request(context),
            writer=persistence,
            clock=lambda: current_now - timedelta(minutes=31),
        )
        service.prepare_confirmation(
            change_set=changes,
            context=context,
            session_binding="expired-restart-session",
            provenance=provenance,
        )

        assert (
            service.check_result_by_locator(
                school_id=SchoolId(school_id),
                actor_user_id=user_id,
                session_binding="expired-restart-session",
                locator_sha256=locator_sha256,
            ).found
            is True
        )

        assert persistence.for_request(context).purge_expired() >= 1
        with factory() as session:
            assert (
                session.execute(
                    text(
                        "SELECT count(*) FROM master_data_import_batches "
                        "WHERE school_id = :school_id "
                        "AND mapping_payload->>'result_locator_sha256' = :locator"
                    ),
                    {"school_id": school_id, "locator": locator_sha256},
                ).scalar_one()
                == 0
            )
            assert (
                session.execute(
                    text(
                        "SELECT count(*) FROM master_data_import_units AS unit "
                        "JOIN master_data_import_batches AS batch ON batch.id = unit.batch_id "
                        "WHERE batch.school_id = :school_id "
                        "AND batch.mapping_payload->>'result_locator_sha256' = :locator"
                    ),
                    {"school_id": school_id, "locator": locator_sha256},
                ).scalar_one()
                == 0
            )

        restarted_signer = ContextProofSigner(b"master-data-schema-context-signing-key-32-bytes")
        restarted_persistence = SqlAlchemySchoolFactPersistence(
            persistence._session_factory,  # noqa: SLF001
            lambda execution_context: restarted_signer.sign_execution_context(
                execution_context,
                session_nonce_hash=sha256(b"sf2-postgres-session").hexdigest(),
                proof_expires_at=datetime.now(UTC) + timedelta(minutes=30),
            ),
            confirmation_credential_issuer=HmacSchoolFactConfirmationCredentialIssuer(
                CONFIRMATION_SIGNING_KEY
            ),
        )
        restarted_service = SchoolFactChangeService(
            reader=restarted_persistence.for_request(context),
            writer=restarted_persistence,
            clock=lambda: current_now,
        )
        assert (
            restarted_service.check_result_by_locator(
                school_id=SchoolId(school_id),
                actor_user_id=user_id,
                session_binding="expired-restart-session",
                locator_sha256=locator_sha256,
            ).found
            is False
        )
        new_receipt = restarted_service.prepare_confirmation(
            change_set=changes,
            context=context,
            session_binding="expired-restart-session",
            provenance=provenance,
        )
        applied = restarted_service.confirm(
            credential=new_receipt.credential,
            change_set=changes,
            context=context,
            session_binding="expired-restart-session",
        )
        assert applied.changed_count == 1
        restarted_persistence.for_request(context).purge_expired()
        with factory() as session:
            assert session.execute(
                text(
                    "SELECT status, confirmation_consumed_at IS NOT NULL "
                    "FROM master_data_import_batches WHERE id = :batch_id"
                ),
                {"batch_id": applied.batch_id},
            ).one() == ("applied", True)
