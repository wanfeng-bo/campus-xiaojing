from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

from app.db.rls_context import set_local_execution_context
from app.db.uow import SqlAlchemyUnitOfWork
from app.platform.contracts import GuardianActorContext, StaffActorContext
from app.platform.identity.context import CurrentUser, resolve_dingtalk_current_user
from app.platform.identity.context_proof import (
    ContextProofSigner,
    ContextSigningKeyMismatch,
)
from app.platform.identity.execution_context import (
    ActorContextResolver,
    ContextNoLongerValid,
    compose_command_context,
    compose_request_context,
)
from app.platform.identity.models import User
from app.platform.identity.session import IdentitySession, IdentitySessionStore
from tests.master_data.conftest import PostgresRoles

_CONTEXT_KEY = b"master-data-schema-context-signing-key-32-bytes"


class FakeRedis:
    """用严格文本值模拟身份会话和不透明上下文引用所需的 Redis 子集。"""

    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.expirations: dict[str, int] = {}

    def setex(self, name: str, time: int, value: str) -> bool:
        self.values[name] = value
        self.expirations[name] = time
        return True

    def get(self, name: str) -> str | None:
        return self.values.get(name)

    def delete(self, *names: str) -> int:
        deleted = 0
        for name in names:
            deleted += int(name in self.values)
            self.values.pop(name, None)
            self.expirations.pop(name, None)
        return deleted


@dataclass
class MutableClock:
    value: datetime

    def __call__(self) -> datetime:
        return self.value


@dataclass(frozen=True, slots=True)
class ExecutionFacts:
    school_id: UUID
    user_id: int
    person_id: UUID
    student_person_id: UUID
    assignment_id: UUID
    guardian_relationship_id: UUID
    student_id: UUID
    administrative_class_id: UUID
    location_id: UUID
    valid_from: datetime


def _seed_formal_structure_lineage(
    connection: psycopg.Connection[Any],
    *,
    school_id: UUID,
    user_id: int,
    person_id: UUID,
    assignment_id: UUID,
    fact_kind: str,
    fact_source_key: str,
    target_id: UUID,
    effective_at: datetime,
    operation: str = "upsert",
    target_version: int = 1,
    expected_version: int = 0,
) -> UUID:
    """Create one exact applied School Facts unit and its lineage."""

    batch_id, unit_id = uuid4(), uuid4()
    idempotency_record_id = uuid4()
    source_sha256 = batch_id.hex * 2
    payload_sha256 = unit_id.hex * 2
    idempotency_key_hash = uuid4().hex * 2
    plan_sha256 = uuid4().hex * 2
    for setting_name, setting_value in (
        ("app.context_purpose", "command"),
        ("app.context_kind", "assignment"),
        ("app.account_user_id", str(user_id)),
        ("app.person_id", str(person_id)),
        ("app.assignment_id", str(assignment_id)),
        ("app.assignment_version", "1"),
    ):
        connection.execute(
            "SELECT set_config(%s, %s, true)",
            (setting_name, setting_value),
        )
    connection.execute(
        """
        INSERT INTO master_data_import_batches (
            id, school_id, mode, source_kind, source_ref, source_sha256,
            worksheet_names, status, planned_units, succeeded_units,
            failed_units, pending_units, confirmed_by_user_id,
            confirmed_by_person_id, confirmed_by_assignment_id, confirmed_at,
            request_id, idempotency_key_hash, plan_sha256, base_fact_version,
            target_fact_version, confirmation_token_sha256,
            confirmation_expires_at, confirmation_consumed_at,
            confirmation_session_sha256, staged_by_user_id,
            staged_by_person_id, staged_by_assignment_id,
            staged_assignment_version, staged_authorization_version, version
        ) VALUES (
            %s, %s, 'canonical_atomic_changes', 'manual', %s, %s,
            '[]'::jsonb, 'applied', 1, 1,
            0, 0, %s,
            %s, %s, %s,
            %s, %s, %s, 1,
            %s, %s,
            %s, %s,
            %s, %s,
            %s, %s,
            1, 1, 1
        )
        """,
        (
            batch_id,
            school_id,
            f"task6-structure:{batch_id}",
            source_sha256,
            user_id,
            person_id,
            assignment_id,
            effective_at,
            uuid4(),
            idempotency_key_hash,
            plan_sha256,
            target_version + 1,
            uuid4().hex * 2,
            effective_at + timedelta(hours=1),
            effective_at,
            uuid4().hex * 2,
            user_id,
            person_id,
            assignment_id,
        ),
    )
    connection.execute(
        """
        INSERT INTO master_data_import_units (
            id, school_id, batch_id, sequence, row_number, unit_kind, status,
            payload, payload_sha256, soft_anomalies, initial_soft_anomalies,
            target_id, target_version, version
        ) VALUES (
            %s, %s, %s, 1, 1, 'school_fact', 'applied',
            jsonb_build_object(
                'fact_kind', CAST(%s AS text),
                'source_key', CAST(%s AS text),
                'operation', CAST(%s AS text),
                'effective_at', CAST(%s AS text),
                'expected_version', CAST(%s AS bigint)
            ), %s, '[]'::jsonb, '[]'::jsonb,
            %s, %s, 1
        )
        """,
        (
            unit_id,
            school_id,
            batch_id,
            fact_kind,
            fact_source_key,
            operation,
            effective_at.isoformat(),
            expected_version,
            payload_sha256,
            target_id,
            target_version,
        ),
    )
    connection.execute(
        """
        INSERT INTO school_fact_change_lineage (
            id, school_id, batch_id, unit_id, fact_kind, fact_source_key,
            operation, target_id, target_version, source_sha256,
            sheet_code, row_number
        ) VALUES (
            %s, %s, %s, %s, %s, %s,
            %s, %s, %s, %s,
            'task6_fixture', 1
        )
        """,
        (
            uuid4(),
            school_id,
            batch_id,
            unit_id,
            fact_kind,
            fact_source_key,
            operation,
            target_id,
            target_version,
            source_sha256,
        ),
    )
    connection.execute(
        """
        INSERT INTO command_idempotency_records (
            id, school_id, action_code, actor_user_id, actor_person_id,
            actor_context_type, actor_context_key, actor_assignment_id,
            actor_assignment_version, resource_type, resource_id,
            idempotency_key_hash, request_sha256, status, result_type,
            result_payload
        ) VALUES (
            %s, %s, 'school_facts.confirm', %s, %s,
            'assignment', %s, %s,
            1, 'master_data.import_batch', %s,
            %s, %s, 'succeeded', 'master_data.import_outcome',
            jsonb_build_object(
                'resource_type', 'master_data.import_batch',
                'resource_id', CAST(%s AS text),
                'resource_version', 1,
                'outcome_code', 'applied'
            )
        )
        """,
        (
            idempotency_record_id,
            school_id,
            user_id,
            person_id,
            f"assignment:{assignment_id}:1",
            assignment_id,
            str(batch_id),
            idempotency_key_hash,
            plan_sha256,
            str(batch_id),
        ),
    )
    connection.execute(
        """
        INSERT INTO master_data_import_command_outcomes (
            id, school_id, idempotency_record_id, batch_id, action_code,
            status, batch_version, succeeded_units, failed_units, executed_units
        ) VALUES (
            %s, %s, %s, %s, 'school_facts.confirm',
            'applied', 1, ARRAY[1], ARRAY[]::integer[], ARRAY[1]
        )
        """,
        (uuid4(), school_id, idempotency_record_id, batch_id),
    )
    return batch_id


def _runtime_session_factory_for(
    roles: PostgresRoles,
) -> tuple[sessionmaker[Session], Any]:
    runtime = roles.runtime_connection_parameters
    engine = create_engine(
        URL.create(
            drivername="postgresql+psycopg",
            username=str(runtime["user"]),
            password=str(runtime["password"]),
            host=str(runtime["host"]),
            port=int(runtime["port"]),
            database=str(runtime["dbname"]),
        ),
        poolclass=NullPool,
    )
    return sessionmaker(bind=engine, autoflush=False, autocommit=False), engine


@pytest.fixture
def runtime_session_factory(
    current_master_data_schema: PostgresRoles,
) -> Iterator[sessionmaker[Session]]:
    factory, engine = _runtime_session_factory_for(current_master_data_schema)
    try:
        yield factory
    finally:
        engine.dispose()


def _seed_execution_facts(current_master_data_schema: PostgresRoles) -> ExecutionFacts:
    """用 Owner 建立同一账号同时具有教师和家长关系的最小权威事实。"""

    school_id = uuid4()
    person_id = uuid4()
    student_person_id = uuid4()
    unit_id = uuid4()
    assignment_id = uuid4()
    binding_id = uuid4()
    fact_source_id = uuid4()
    administrative_class_id = uuid4()
    student_id = uuid4()
    guardian_relationship_id = uuid4()
    location_id = uuid4()
    calendar_version_id = uuid4()
    marker = uuid4().hex
    valid_from = datetime.now(UTC).replace(microsecond=0) - timedelta(days=30)

    with psycopg.connect(
        **current_master_data_schema.migration_connection_parameters,
        autocommit=False,
    ) as connection:
        connection.execute("SET LOCAL ROLE campus_ai_db_owner")
        school_code = f"task6-{marker}"
        connection.execute(
            """
            INSERT INTO schools (id, school_code, display_name, timezone_name, status, version)
            VALUES (%s, %s, 'Task 6 学校', 'Asia/Shanghai', 'active', 1)
            """,
            (school_id, school_code),
        )
        connection.execute(
            """
            INSERT INTO school_fact_sources (
                id, school_id, source_key, source_kind, source_ref, schema_version
            ) VALUES (%s, %s, %s, 'legacy', %s, 1)
            """,
            (
                fact_source_id,
                school_id,
                f"task6-content:{marker}",
                f"task6-content:{marker}",
            ),
        )
        connection.execute(
            """
            INSERT INTO school_versions (
                id, school_id, business_code, display_name, short_name,
                timezone_name, lifecycle_status, valid_from, valid_to,
                version, source_id, import_batch_id
            ) VALUES (
                %s, %s, %s, 'Task 6 学校', NULL,
                'Asia/Shanghai', 'active', %s, NULL,
                1, %s, NULL
            )
            """,
            (uuid4(), school_id, school_code, valid_from, fact_source_id),
        )
        user_row = connection.execute(
            """
            INSERT INTO users (external_subject, display_name, is_active, security_version)
            VALUES (%s, '双重身份教师', true, 1)
            RETURNING id
            """,
            (f"task6:{marker}",),
        ).fetchone()
        assert user_row is not None
        user_id = int(user_row[0])
        for parameters in ((person_id, school_id), (student_person_id, school_id)):
            connection.execute(
                "INSERT INTO school_people (id, school_id, status, version) "
                "VALUES (%s, %s, 'active', 1)",
                parameters,
            )
        for parameters in (
            (uuid4(), school_id, person_id, "双重身份教师", valid_from, marker),
            (uuid4(), school_id, student_person_id, "原姓名", valid_from, marker),
        ):
            connection.execute(
                """
                INSERT INTO school_person_names (
                    id, school_id, person_id, display_name, valid_from,
                    source_kind, source_ref, status, version
                ) VALUES (%s, %s, %s, %s, %s, 'manual', %s, 'confirmed', 1)
                """,
                parameters,
            )
        connection.execute(
            """
            INSERT INTO account_person_bindings (
                id, school_id, account_user_id, person_id, valid_from, status, version
            ) VALUES (%s, %s, %s, %s, %s, 'confirmed', 1)
            """,
            (binding_id, school_id, user_id, person_id, valid_from),
        )
        connection.execute(
            """
            INSERT INTO account_person_binding_versions (
                id, school_id, binding_id, account_user_id, person_id,
                provider_code, provider_tenant_key, external_subject,
                effective_from, effective_to, revoked_at, status, target_version,
                source_ref, source_id, import_batch_id
            ) VALUES (
                %s, %s, %s, %s, %s,
                'legacy', 'legacy', %s,
                %s, NULL, NULL, 'confirmed', 1,
                %s, %s, NULL
            )
            """,
            (
                uuid4(),
                school_id,
                binding_id,
                user_id,
                person_id,
                f"task6:{marker}",
                valid_from,
                f"task6-content:{marker}",
                fact_source_id,
            ),
        )
        connection.execute(
            """
            INSERT INTO organization_units (id, school_id, kind, status, version)
            VALUES (%s, %s, 'department', 'active', 1)
            """,
            (unit_id, school_id),
        )
        connection.execute(
            """
            INSERT INTO staff_assignments (
                id, school_id, person_id, organization_unit_id, position_title,
                semantic_role_code, appointment_source, approval_reference,
                valid_from, status, version
            ) VALUES (%s, %s, %s, %s, '维修人员', 'repair_worker', 'manual', %s,
                      %s, 'confirmed', 1)
            """,
            (assignment_id, school_id, person_id, unit_id, marker, valid_from),
        )
        connection.execute(
            """
            INSERT INTO staff_assignment_versions (
                id, school_id, assignment_id, person_id, organization_unit_id,
                position_title, semantic_role_code, assignment_kind,
                appointment_source, approval_reference,
                effective_from, effective_to, planned_valid_to, revoked_at,
                status, target_version, source_ref, source_id, import_batch_id
            ) VALUES (
                %s, %s, %s, %s, %s,
                '维修人员', 'repair_worker', 'substantive',
                'manual', %s,
                %s, NULL, NULL, NULL,
                'confirmed', 1, %s, %s, NULL
            )
            """,
            (
                uuid4(),
                school_id,
                assignment_id,
                person_id,
                unit_id,
                marker,
                valid_from,
                f"task6-content:{marker}",
                fact_source_id,
            ),
        )
        organization_source_row = connection.execute(
            "SELECT source_key FROM organization_units WHERE id = %s",
            (unit_id,),
        ).fetchone()
        assert organization_source_row is not None
        organization_source_key = str(organization_source_row[0])
        organization_batch_id = _seed_formal_structure_lineage(
            connection,
            school_id=school_id,
            user_id=user_id,
            person_id=person_id,
            assignment_id=assignment_id,
            fact_kind="organization",
            fact_source_key=organization_source_key,
            target_id=unit_id,
            effective_at=valid_from,
        )
        connection.execute(
            """
            INSERT INTO organization_unit_versions (
                id, school_id, organization_unit_id, parent_organization_unit_id,
                display_name, display_path, valid_from, source_kind,
                source_ref, status, version
            ) VALUES (
                %s, %s, %s, NULL,
                '总务处', 'Task 6 学校 / 总务处', %s, 'manual',
                %s, 'confirmed', 1
            )
            """,
            (
                uuid4(),
                school_id,
                unit_id,
                valid_from,
                f"change-set:{organization_batch_id}",
            ),
        )
        connection.execute(
            """
            INSERT INTO administrative_classes (
                id, school_id, school_year_code, grade_code, status, version
            ) VALUES (%s, %s, '2026', 'grade_1', 'active', 1)
            """,
            (administrative_class_id, school_id),
        )
        connection.execute(
            """
            INSERT INTO administrative_class_versions (
                id, school_id, administrative_class_id, display_name, valid_from,
                source_kind, source_ref, status, version
            ) VALUES (%s, %s, %s, '初一（3）班', %s, 'manual', %s, 'confirmed', 1)
            """,
            (uuid4(), school_id, administrative_class_id, valid_from, marker),
        )
        connection.execute(
            "INSERT INTO students (id, school_id, person_id, status, version) "
            "VALUES (%s, %s, %s, 'active', 1)",
            (student_id, school_id, student_person_id),
        )
        connection.execute(
            """
            INSERT INTO student_enrollments (
                id, school_id, student_id, administrative_class_id, student_number,
                source_kind, source_ref, valid_from, status, version
            ) VALUES (%s, %s, %s, %s, 'S-001', 'manual', %s, %s, 'confirmed', 1)
            """,
            (uuid4(), school_id, student_id, administrative_class_id, marker, valid_from),
        )
        connection.execute(
            """
            INSERT INTO guardian_relationships (
                id, school_id, guardian_person_id, student_id, relationship_kind,
                verification_source, verification_reference, verified_at,
                valid_from, status, version
            ) VALUES (%s, %s, %s, %s, 'guardian', 'manual', %s, %s, %s,
                      'confirmed', 1)
            """,
            (
                guardian_relationship_id,
                school_id,
                person_id,
                student_id,
                marker,
                valid_from,
                valid_from,
            ),
        )
        connection.execute(
            """
            INSERT INTO campus_locations (
                id, school_id, parent_location_id, kind, status, version
            ) VALUES (%s, %s, NULL, 'campus', 'active', 1)
            """,
            (location_id, school_id),
        )
        location_source_row = connection.execute(
            "SELECT source_key FROM campus_locations WHERE id = %s",
            (location_id,),
        ).fetchone()
        assert location_source_row is not None
        location_source_key = str(location_source_row[0])
        location_batch_id = _seed_formal_structure_lineage(
            connection,
            school_id=school_id,
            user_id=user_id,
            person_id=person_id,
            assignment_id=assignment_id,
            fact_kind="location",
            fact_source_key=location_source_key,
            target_id=location_id,
            effective_at=valid_from,
        )
        connection.execute(
            """
            INSERT INTO campus_location_versions (
                id, school_id, location_id, parent_location_id, kind,
                display_name, aliases, display_path,
                valid_from, source_kind, source_ref, status, version
            ) VALUES (%s, %s, %s, NULL, 'campus', '旧校区名', ARRAY[]::varchar[],
                      'Task 6 学校 / 旧校区名',
                      %s, 'manual', %s, 'confirmed', 1)
            """,
            (
                uuid4(),
                school_id,
                location_id,
                valid_from,
                f"change-set:{location_batch_id}",
            ),
        )
        connection.execute(
            """
            INSERT INTO school_calendar_versions (
                id, school_id, display_name, timezone_name, valid_from,
                source_kind, source_ref, status, version
            ) VALUES (%s, %s, '2026 学年校历', 'Asia/Shanghai', %s,
                      'manual', %s, 'published', 1)
            """,
            (calendar_version_id, school_id, valid_from, marker),
        )
        connection.commit()

    return ExecutionFacts(
        school_id=school_id,
        user_id=user_id,
        person_id=person_id,
        student_person_id=student_person_id,
        assignment_id=assignment_id,
        guardian_relationship_id=guardian_relationship_id,
        student_id=student_id,
        administrative_class_id=administrative_class_id,
        location_id=location_id,
        valid_from=valid_from,
    )


@pytest.fixture
def execution_facts(current_master_data_schema: PostgresRoles) -> ExecutionFacts:
    return _seed_execution_facts(current_master_data_schema)


def _identity(
    facts: ExecutionFacts,
    redis: FakeRedis,
) -> tuple[CurrentUser, IdentitySession, str]:
    current_user = CurrentUser(
        id=facts.user_id,
        external_subject=f"task6-user:{facts.user_id}",
        display_name="双重身份教师",
        security_version=1,
    )
    store = IdentitySessionStore(redis=redis, ttl_seconds=28_800)
    token = store.create(current_user)
    identity_session = store.read(token)
    assert identity_session is not None
    return current_user, identity_session, token


def _resolver(
    *,
    facts: ExecutionFacts,
    redis: FakeRedis,
    runtime_session_factory: sessionmaker[Session],
    clock: MutableClock | None = None,
) -> tuple[ActorContextResolver, IdentitySession, str]:
    current_user, identity_session, token = _identity(facts, redis)
    resolver = ActorContextResolver(
        session_factory=runtime_session_factory,
        redis=redis,
        proof_signer=ContextProofSigner(_CONTEXT_KEY),
        current_user=current_user,
        identity_session=identity_session,
        clock=clock,
    )
    return resolver, identity_session, token


def _find_option(options: tuple[Any, ...], kind: str) -> Any:
    return next(option for option in options if option.kind == kind)


def test_identity_session_rejects_legacy_or_tampered_security_context() -> None:
    redis = FakeRedis()
    store = IdentitySessionStore(redis=redis, ttl_seconds=28_800)
    current_user = CurrentUser(
        id=7,
        external_subject="dingtalk:corp:user",
        display_name="测试教师",
        security_version=3,
    )
    token = store.create(current_user)
    identity_session = store.read(token)
    assert identity_session is not None
    assert identity_session.security_version == 3
    assert len(identity_session.session_nonce_hash) == 64

    key = f"identity:session:{token}"
    payload = json.loads(redis.values[key])
    payload.pop("security_version")
    redis.values[key] = json.dumps(payload)
    assert store.read(token) is None

    token = store.create(current_user)
    key = f"identity:session:{token}"
    payload = json.loads(redis.values[key])
    payload["session_nonce_hash"] = "f" * 64
    redis.values[key] = json.dumps(payload)
    assert store.read(token) is None


def test_dingtalk_session_security_version_mismatch_requires_login() -> None:
    redis = FakeRedis()
    store = IdentitySessionStore(redis=redis, ttl_seconds=28_800)
    token = store.create(
        CurrentUser(
            id=9,
            external_subject="dingtalk:corp:user",
            display_name="测试教师",
            security_version=1,
        )
    )

    class UserSession:
        def scalar(self, _statement: object) -> User:
            return User(
                id=9,
                external_subject="dingtalk:corp:user",
                display_name="测试教师",
                is_active=True,
                security_version=2,
            )

    with pytest.raises(HTTPException) as error:
        resolve_dingtalk_current_user(
            session_token=token,
            session=UserSession(),  # type: ignore[arg-type]
            identity_session_store=store,
        )
    assert getattr(error.value, "status_code", None) == 401


@pytest.mark.integration
def test_teacher_and_guardian_contexts_are_never_merged(
    runtime_session_factory: sessionmaker[Session],
    execution_facts: ExecutionFacts,
) -> None:
    resolver, _identity_session, _token = _resolver(
        facts=execution_facts,
        redis=FakeRedis(),
        runtime_session_factory=runtime_session_factory,
    )

    options = resolver.list_options()

    assert {option.kind for option in options} == {
        "staff_assignment",
        "guardian_relationship",
    }
    assert all(
        set(option.model_dump())
        == {
            "opaque_ref",
            "kind",
            "display_label",
            "school_label",
            "effective_until",
        }
        for option in options
    )
    serialized = json.dumps(
        [option.model_dump(mode="json") for option in options],
        ensure_ascii=False,
    )
    assert str(execution_facts.school_id) not in serialized
    assert str(execution_facts.assignment_id) not in serialized
    assert str(execution_facts.guardian_relationship_id) not in serialized

    staff = resolver.resolve(_find_option(options, "staff_assignment").opaque_ref)
    guardian = resolver.resolve(_find_option(options, "guardian_relationship").opaque_ref)
    assert type(staff) is StaffActorContext
    assert not hasattr(staff, "guardian_relationship_id")
    assert type(guardian) is GuardianActorContext
    assert not hasattr(guardian, "assignment_id")


@pytest.mark.integration
def test_denied_context_does_not_fall_back_to_other_identity(
    current_master_data_schema: PostgresRoles,
    runtime_session_factory: sessionmaker[Session],
    execution_facts: ExecutionFacts,
) -> None:
    resolver, _identity_session, _token = _resolver(
        facts=execution_facts,
        redis=FakeRedis(),
        runtime_session_factory=runtime_session_factory,
    )
    options = resolver.list_options()
    staff_ref = _find_option(options, "staff_assignment").opaque_ref
    assert type(resolver.resolve(staff_ref)) is StaffActorContext

    with psycopg.connect(
        **current_master_data_schema.migration_connection_parameters,
        autocommit=True,
    ) as connection:
        connection.execute("SET ROLE campus_ai_db_owner")
        connection.execute(
            "UPDATE staff_assignments SET status = 'rejected', version = version + 1 WHERE id = %s",
            (execution_facts.assignment_id,),
        )

    with pytest.raises(ContextNoLongerValid):
        resolver.resolve(staff_ref)


@pytest.mark.integration
def test_guardian_context_rechecks_the_related_student_root(
    isolated_migration_schema: PostgresRoles,
) -> None:
    """A guardian relationship alone cannot keep an inactive student authoritative."""

    runtime_session_factory, engine = _runtime_session_factory_for(isolated_migration_schema)
    try:
        execution_facts = _seed_execution_facts(isolated_migration_schema)
        resolver, _identity_session, _token = _resolver(
            facts=execution_facts,
            redis=FakeRedis(),
            runtime_session_factory=runtime_session_factory,
        )
        guardian_ref = _find_option(resolver.list_options(), "guardian_relationship").opaque_ref
        with psycopg.connect(
            **isolated_migration_schema.migration_connection_parameters,
            autocommit=True,
        ) as connection:
            connection.execute("SET ROLE campus_ai_db_owner")
            connection.execute(
                "UPDATE students SET status = 'inactive', version = version + 1 WHERE id = %s",
                (execution_facts.student_id,),
            )

        assert {option.kind for option in resolver.list_options()} == {"staff_assignment"}
        with pytest.raises(ContextNoLongerValid):
            resolver.resolve(guardian_ref)
    finally:
        engine.dispose()


@pytest.mark.integration
def test_forged_cross_session_expired_and_stale_refs_are_rejected(
    current_master_data_schema: PostgresRoles,
    runtime_session_factory: sessionmaker[Session],
    execution_facts: ExecutionFacts,
) -> None:
    redis = FakeRedis()
    clock = MutableClock(datetime.now(UTC))
    resolver, _identity_session, _token = _resolver(
        facts=execution_facts,
        redis=redis,
        runtime_session_factory=runtime_session_factory,
        clock=clock,
    )
    staff_ref = _find_option(resolver.list_options(), "staff_assignment").opaque_ref

    with pytest.raises(ContextNoLongerValid):
        resolver.resolve(str(uuid4()))

    second_resolver, _second_session, _second_token = _resolver(
        facts=execution_facts,
        redis=redis,
        runtime_session_factory=runtime_session_factory,
        clock=clock,
    )
    with pytest.raises(ContextNoLongerValid):
        second_resolver.resolve(staff_ref)

    clock.value += timedelta(minutes=16)
    with pytest.raises(ContextNoLongerValid):
        resolver.resolve(staff_ref)

    clock.value -= timedelta(minutes=16)
    with psycopg.connect(
        **current_master_data_schema.migration_connection_parameters,
        autocommit=True,
    ) as connection:
        connection.execute("SET ROLE campus_ai_db_owner")
        connection.execute(
            "UPDATE staff_assignments SET version = version + 1 WHERE id = %s",
            (execution_facts.assignment_id,),
        )
    with pytest.raises(ContextNoLongerValid):
        resolver.resolve(staff_ref)


@pytest.mark.integration
def test_next_resolution_rechecks_account_security_version(
    current_master_data_schema: PostgresRoles,
    runtime_session_factory: sessionmaker[Session],
    execution_facts: ExecutionFacts,
) -> None:
    resolver, _identity_session, _token = _resolver(
        facts=execution_facts,
        redis=FakeRedis(),
        runtime_session_factory=runtime_session_factory,
    )
    staff_ref = _find_option(resolver.list_options(), "staff_assignment").opaque_ref
    assert type(resolver.resolve(staff_ref)) is StaffActorContext

    with psycopg.connect(
        **current_master_data_schema.migration_connection_parameters,
        autocommit=True,
    ) as connection:
        connection.execute("SET ROLE campus_ai_db_owner")
        connection.execute(
            "UPDATE users SET security_version = security_version + 1 WHERE id = %s",
            (execution_facts.user_id,),
        )

    with pytest.raises(ContextNoLongerValid):
        resolver.resolve(staff_ref)


@pytest.mark.integration
def test_python_proofs_match_postgresql_and_reject_tampering(
    runtime_session_factory: sessionmaker[Session],
    execution_facts: ExecutionFacts,
) -> None:
    redis = FakeRedis()
    resolver, identity_session, _token = _resolver(
        facts=execution_facts,
        redis=redis,
        runtime_session_factory=runtime_session_factory,
    )
    staff_option = _find_option(resolver.list_options(), "staff_assignment")
    actor = resolver.resolve(staff_option.opaque_ref)
    context = compose_request_context(actor, uuid4())
    signer = ContextProofSigner(_CONTEXT_KEY)
    envelope = signer.sign_execution_context(
        context,
        session_nonce_hash=identity_session.session_nonce_hash,
        proof_expires_at=staff_option.effective_until,
    )

    with runtime_session_factory() as session, session.begin():
        signer.verify_database_fingerprint(session)
        set_local_execution_context(session, envelope)
        assert session.scalar(text("SELECT current_setting('app.school_id')")) == str(
            execution_facts.school_id
        )

    for tampered in (
        replace(envelope, request_id=uuid4()),
        replace(envelope, session_nonce_hash="f" * 64),
        replace(envelope, school_id=uuid4()),
    ):
        with pytest.raises(DBAPIError, match="proof"):
            with runtime_session_factory() as session, session.begin():
                set_local_execution_context(session, tampered)

    wrong_signer = ContextProofSigner(b"wrong-deployment-context-signing-key-32-bytes")
    wrong_envelope = wrong_signer.sign_execution_context(
        context,
        session_nonce_hash=identity_session.session_nonce_hash,
        proof_expires_at=staff_option.effective_until,
    )
    with pytest.raises(DBAPIError, match="proof"):
        with runtime_session_factory() as session, session.begin():
            set_local_execution_context(session, wrong_envelope)
    with pytest.raises(ContextSigningKeyMismatch):
        with runtime_session_factory() as session, session.begin():
            wrong_signer.verify_database_fingerprint(session)


@pytest.mark.integration
def test_inactive_school_has_one_read_only_staff_candidate_for_reactivation(
    isolated_migration_schema: PostgresRoles,
) -> None:
    """Normal resolution stays closed while the narrow candidate only reissues one actor."""

    runtime_session_factory, engine = _runtime_session_factory_for(isolated_migration_schema)
    try:
        execution_facts = _seed_execution_facts(isolated_migration_schema)
        redis = FakeRedis()
        resolver, _identity_session, _token = _resolver(
            facts=execution_facts,
            redis=redis,
            runtime_session_factory=runtime_session_factory,
        )
        options = resolver.list_options()
        staff_ref = _find_option(options, "staff_assignment").opaque_ref
        guardian_ref = _find_option(options, "guardian_relationship").opaque_ref
        with psycopg.connect(
            **isolated_migration_schema.migration_connection_parameters,
            autocommit=False,
        ) as connection:
            connection.execute("SET LOCAL ROLE campus_ai_db_owner")
            school_source_row = connection.execute(
                "SELECT source_key FROM schools WHERE id = %s",
                (execution_facts.school_id,),
            ).fetchone()
            assert school_source_row is not None
            school_source_key = str(school_source_row[0])
            _seed_formal_structure_lineage(
                connection,
                school_id=execution_facts.school_id,
                user_id=execution_facts.user_id,
                person_id=execution_facts.person_id,
                assignment_id=execution_facts.assignment_id,
                fact_kind="school",
                fact_source_key=school_source_key,
                target_id=execution_facts.school_id,
                effective_at=datetime.now(UTC).replace(microsecond=0),
                operation="end",
                target_version=2,
                expected_version=1,
            )
            connection.execute(
                "UPDATE schools SET status = 'inactive', version = 2 WHERE id = %s",
                (execution_facts.school_id,),
            )
            connection.commit()

        with pytest.raises(ContextNoLongerValid):
            resolver.resolve(staff_ref)
        assert resolver.list_options() == ()
        redis.values.clear()
        redis.expirations.clear()
        candidates = resolver.list_school_reactivation_candidates()
        assert len(candidates) == 1
        assert candidates[0].kind == "staff_assignment"
        with pytest.raises(ContextNoLongerValid):
            resolver.resolve(candidates[0].opaque_ref)
        candidate = resolver.resolve_school_reactivation_candidate(candidates[0].opaque_ref)
        assert type(candidate) is StaffActorContext
        assert candidate.school_id == execution_facts.school_id
        with pytest.raises(ContextNoLongerValid):
            resolver.resolve_school_reactivation_candidate(guardian_ref)
    finally:
        engine.dispose()


@pytest.mark.integration
def test_uow_rolls_back_without_commit_and_commits_only_explicitly(
    current_master_data_schema: PostgresRoles,
    runtime_session_factory: sessionmaker[Session],
    execution_facts: ExecutionFacts,
) -> None:
    redis = FakeRedis()
    resolver, identity_session, _token = _resolver(
        facts=execution_facts,
        redis=redis,
        runtime_session_factory=runtime_session_factory,
    )
    staff_option = _find_option(resolver.list_options(), "staff_assignment")
    actor = resolver.resolve(staff_option.opaque_ref)
    command_context = compose_command_context(actor, uuid4(), "task6-uow-command-001")
    envelope = ContextProofSigner(_CONTEXT_KEY).sign_execution_context(
        command_context,
        session_nonce_hash=identity_session.session_nonce_hash,
        proof_expires_at=staff_option.effective_until,
    )

    original_display_name = _user_display_name(current_master_data_schema, execution_facts.user_id)

    with SqlAlchemyUnitOfWork(runtime_session_factory, envelope) as uow:
        uow.session.execute(
            text(
                "UPDATE users SET display_name = :display_name, updated_at = clock_timestamp() "
                "WHERE id = :user_id"
            ),
            {"display_name": "uow-rolled-back", "user_id": execution_facts.user_id},
        )

    assert (
        _user_display_name(current_master_data_schema, execution_facts.user_id)
        == original_display_name
    )

    with SqlAlchemyUnitOfWork(runtime_session_factory, envelope) as uow:
        uow.session.execute(
            text(
                "UPDATE users SET display_name = :display_name, updated_at = clock_timestamp() "
                "WHERE id = :user_id"
            ),
            {"display_name": "uow-committed", "user_id": execution_facts.user_id},
        )
        uow.commit()

    assert (
        _user_display_name(current_master_data_schema, execution_facts.user_id) == "uow-committed"
    )


def _user_display_name(postgres_roles: PostgresRoles, user_id: int) -> str:
    with psycopg.connect(
        **postgres_roles.migration_connection_parameters,
        autocommit=True,
    ) as connection:
        connection.execute("SET ROLE campus_ai_db_owner")
        row = connection.execute(
            "SELECT display_name FROM users WHERE id = %s",
            (user_id,),
        ).fetchone()
    assert row is not None
    return str(row[0])
