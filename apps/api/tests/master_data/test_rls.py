from __future__ import annotations

import hmac
import os
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from importlib import import_module
from pathlib import Path
from types import ModuleType
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.types.json import Jsonb
from pydantic import ValidationError
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, Engine
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from app.platform import contracts as platform_contracts
from app.platform.audit.contracts import BusinessAuditRecord, BusinessAuditSummary
from app.platform.audit.service import append_audit_log, append_business_audit_log
from app.platform.contracts import AssignmentId, PersonId, SchoolId
from app.platform.idempotency.contracts import CommandIdempotencyIdentity, SafeCommandResult
from app.platform.idempotency.repository import SqlAlchemyCommandIdempotencyRepository
from app.platform.messaging.contracts import OutboxEvent
from app.platform.messaging.repository import SqlAlchemyOutboxWriter
from tests.master_data.conftest import PostgresRoles
from tests.master_data.test_execution_context import (
    _seed_formal_structure_lineage,  # pyright: ignore[reportPrivateUsage]
)

_TEST_CONTEXT_KEY = b"master-data-schema-context-signing-key-32-bytes"
_API_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True, slots=True)
class _RlsFacts:
    school_a: UUID
    school_b: UUID
    person_a: UUID
    person_a2: UUID
    person_b: UUID
    user_a: int
    user_a2: int
    user_b: int
    assignment_a: UUID
    assignment_a2: UUID
    assignment_b: UUID


def _seed_rls_facts(schema: PostgresRoles) -> _RlsFacts:
    """建立一所真实锚定学校，并保留一个从未落库的跨校攻击目标。"""

    school_a, school_b = uuid4(), uuid4()
    person_a, person_a2, person_b = uuid4(), uuid4(), uuid4()
    binding_a, binding_a2 = uuid4(), uuid4()
    unit_a, unit_a2 = uuid4(), uuid4()
    assignment_a, assignment_a2, assignment_b = uuid4(), uuid4(), uuid4()
    marker = uuid4().hex
    now = datetime.now(UTC)
    source_id_by_school = {school_a: uuid4()}
    source_ref_by_school = {school_a: f"task5-content-a:{marker}"}
    with psycopg.connect(
        **schema.migration_connection_parameters,
        autocommit=False,
    ) as connection:
        connection.execute("SET LOCAL ROLE campus_ai_db_owner")
        # revision 20 永久锚定首所学校；school_b 只作伪造 context / 跨锚点 DML 目标。
        # 它故意不落库，避免测试 fixture 退回同库多租户语义。
        for parameters in (
            (
                school_a,
                f"legacy:schools:{school_a}",
                f"task5-a-{marker}",
                "Task 5 学校 A",
            ),
        ):
            connection.execute(
                """
                INSERT INTO public.schools (
                    id, source_key, school_code, display_name, timezone_name, status, version
                ) VALUES (%s, %s, %s, %s, 'Asia/Shanghai', 'active', 1)
                """,
                parameters,
            )
            connection.execute(
                """
                INSERT INTO public.school_fact_sources (
                    id, school_id, source_key, source_kind, source_ref, schema_version
                ) VALUES (%s, %s, %s, 'legacy', %s, 1)
                """,
                (
                    source_id_by_school[parameters[0]],
                    parameters[0],
                    source_ref_by_school[parameters[0]],
                    source_ref_by_school[parameters[0]],
                ),
            )
            connection.execute(
                """
                INSERT INTO public.school_versions (
                    id, school_id, business_code, display_name, timezone_name,
                    lifecycle_status, valid_from, version, source_id
                ) VALUES (
                    %s, %s, %s, %s, 'Asia/Shanghai',
                    'active', %s, 1, %s
                )
                """,
                (
                    uuid4(),
                    parameters[0],
                    parameters[2],
                    parameters[3],
                    now,
                    source_id_by_school[parameters[0]],
                ),
            )
        user_a = connection.execute(
            """
            INSERT INTO public.users (external_subject, display_name, is_active, security_version)
            VALUES (%s, 'Task 5 A', true, 1) RETURNING id
            """,
            (f"task5:a:{marker}",),
        ).fetchone()
        user_a2 = connection.execute(
            """
            INSERT INTO public.users (external_subject, display_name, is_active, security_version)
            VALUES (%s, 'Task 5 A2', true, 1) RETURNING id
            """,
            (f"task5:a2:{marker}",),
        ).fetchone()
        assert user_a is not None and user_a2 is not None
        external_subject_by_user_id = {
            int(user_a[0]): f"task5:a:{marker}",
            int(user_a2[0]): f"task5:a2:{marker}",
        }
        for person_id, school_id in (
            (person_a, school_a),
            (person_a2, school_a),
        ):
            connection.execute(
                "INSERT INTO public.school_people "
                "(id, school_id, source_key, status, version) "
                "VALUES (%s, %s, %s, 'active', 1)",
                (person_id, school_id, f"legacy:school_people:{person_id}"),
            )
        for binding_id, school_id, user_id, person_id in (
            (binding_a, school_a, user_a[0], person_a),
            (binding_a2, school_a, user_a2[0], person_a2),
        ):
            connection.execute(
                """
                INSERT INTO public.account_person_bindings (
                    id, school_id, source_key, account_user_id, person_id,
                    valid_from, status, version
                ) VALUES (%s, %s, %s, %s, %s, %s, 'confirmed', 1)
                """,
                (
                    binding_id,
                    school_id,
                    f"legacy:account_person_bindings:{binding_id}",
                    user_id,
                    person_id,
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO public.account_person_binding_versions (
                    id, school_id, binding_id, account_user_id, person_id,
                    provider_code, provider_tenant_key, external_subject,
                    effective_from, status, target_version, source_ref, source_id
                ) VALUES (
                    %s, %s, %s, %s, %s,
                    'legacy', 'legacy', %s,
                    %s, 'confirmed', 1, %s, %s
                )
                """,
                (
                    uuid4(),
                    school_id,
                    binding_id,
                    user_id,
                    person_id,
                    external_subject_by_user_id[int(user_id)],
                    now,
                    source_ref_by_school[school_id],
                    source_id_by_school[school_id],
                ),
            )
        for unit_id, school_id in (
            (unit_a, school_a),
            (unit_a2, school_a),
        ):
            connection.execute(
                """
                INSERT INTO public.organization_units (
                    id, school_id, source_key, kind, status, version
                ) VALUES (%s, %s, %s, 'department', 'active', 1)
                """,
                (unit_id, school_id, f"legacy:organization_units:{unit_id}"),
            )
        for parameters in (
            (assignment_a, school_a, person_a, unit_a, f"task5-a-{marker}", now),
            (assignment_a2, school_a, person_a2, unit_a2, f"task5-a2-{marker}", now),
        ):
            connection.execute(
                """
                INSERT INTO public.staff_assignments (
                    id, school_id, source_key, person_id, organization_unit_id,
                    position_title, semantic_role_code, appointment_source,
                    approval_reference, valid_from, status, version
                ) VALUES (
                    %s, %s, %s, %s, %s, '维修人员', 'repair_worker', 'manual',
                    %s, %s, 'confirmed', 1
                )
                """,
                (
                    parameters[0],
                    parameters[1],
                    f"legacy:staff_assignments:{parameters[0]}",
                    *parameters[2:],
                ),
            )
            connection.execute(
                """
                INSERT INTO public.staff_assignment_versions (
                    id, school_id, assignment_id, person_id, organization_unit_id,
                    position_title, semantic_role_code, assignment_kind,
                    appointment_source, approval_reference,
                    effective_from, status, target_version, source_ref, source_id
                ) VALUES (
                    %s, %s, %s, %s, %s,
                    '维修人员', 'repair_worker', 'substantive',
                    'manual', %s,
                    %s, 'confirmed', 1, %s, %s
                )
                """,
                (
                    uuid4(),
                    parameters[1],
                    parameters[0],
                    parameters[2],
                    parameters[3],
                    parameters[4],
                    parameters[5],
                    source_ref_by_school[parameters[1]],
                    source_id_by_school[parameters[1]],
                ),
            )
        for user_id, person_id, binding_id, assignment_id in (
            (user_a[0], person_a, binding_a, assignment_a),
            (user_a2[0], person_a2, binding_a2, assignment_a2),
        ):
            _seed_formal_structure_lineage(
                connection,
                school_id=school_a,
                user_id=int(user_id),
                person_id=person_id,
                assignment_id=assignment_id,
                fact_kind="login_mapping",
                fact_source_key=f"legacy:account_person_bindings:{binding_id}",
                target_id=binding_id,
                target_version=1,
                effective_at=now,
            )
            _seed_formal_structure_lineage(
                connection,
                school_id=school_a,
                user_id=int(user_id),
                person_id=person_id,
                assignment_id=assignment_id,
                fact_kind="assignment",
                fact_source_key=f"legacy:staff_assignments:{assignment_id}",
                target_id=assignment_id,
                target_version=1,
                effective_at=now,
            )
        for unit_id, school_id, user_id, person_id, assignment_id in (
            (unit_a, school_a, user_a[0], person_a, assignment_a),
            (unit_a2, school_a, user_a2[0], person_a2, assignment_a2),
        ):
            organization_source_key = f"legacy:organization_units:{unit_id}"
            organization_batch_id = _seed_formal_structure_lineage(
                connection,
                school_id=school_id,
                user_id=int(user_id),
                person_id=person_id,
                assignment_id=assignment_id,
                fact_kind="organization",
                fact_source_key=organization_source_key,
                target_id=unit_id,
                effective_at=now,
            )
            connection.execute(
                """
                INSERT INTO public.organization_unit_versions (
                    id, school_id, organization_unit_id, parent_organization_unit_id,
                    display_name, display_path, valid_from, source_kind,
                    source_ref, status, version
                ) VALUES (
                    %s, %s, %s, NULL,
                    'Task 5 组织', 'Task 5 组织', %s, 'legacy',
                    %s, 'confirmed', 1
                )
                """,
                (
                    uuid4(),
                    school_id,
                    unit_id,
                    now,
                    f"change-set:{organization_batch_id}",
                ),
            )
        assert connection.execute(
            "SELECT count(*) FROM public.schools WHERE id = %s",
            (school_b,),
        ).fetchone() == (0,)
        connection.commit()

    return _RlsFacts(
        school_a=school_a,
        school_b=school_b,
        person_a=person_a,
        person_a2=person_a2,
        person_b=person_b,
        user_a=int(user_a[0]),
        user_a2=int(user_a2[0]),
        user_b=2_147_483_647,
        assignment_a=assignment_a,
        assignment_a2=assignment_a2,
        assignment_b=assignment_b,
    )


@pytest.fixture
def rls_facts(current_master_data_schema: PostgresRoles) -> _RlsFacts:
    return _seed_rls_facts(current_master_data_schema)


@pytest.fixture
def isolated_rls_facts(isolated_revision19_schema: PostgresRoles) -> _RlsFacts:
    return _seed_rls_facts(isolated_revision19_schema)


def _staff_envelope(
    facts: _RlsFacts,
    *,
    school_b: bool = False,
    same_school_other: bool = False,
):
    context = _task5_module("app.db.rls_context")
    school_id = facts.school_b if school_b else facts.school_a
    person_id = (
        facts.person_b if school_b else facts.person_a2 if same_school_other else facts.person_a
    )
    assignment_id = (
        facts.assignment_b
        if school_b
        else facts.assignment_a2
        if same_school_other
        else facts.assignment_a
    )
    user_id = facts.user_b if school_b else facts.user_a2 if same_school_other else facts.user_a
    expires_at = (datetime.now(UTC) + timedelta(hours=1)).replace(microsecond=123456)
    unsigned = context.DatabaseExecutionContextEnvelope(
        school_id=school_id,
        account_user_id=user_id,
        person_id=person_id,
        context_kind="assignment",
        assignment_id=assignment_id,
        assignment_version=1,
        guardian_relationship_id=None,
        guardian_relationship_version=None,
        authorization_version=1,
        request_id=uuid4(),
        context_purpose="read",
        idempotency_key_hash=None,
        session_nonce_hash=sha256(uuid4().bytes).hexdigest(),
        proof_expires_at=expires_at,
        server_proof="0" * 64,
    )
    return _sign_envelope(context, unsigned)


def _sign_envelope(context: ModuleType, envelope):
    proof = hmac.new(
        _TEST_CONTEXT_KEY,
        context.execution_context_canonical_text(envelope).encode("utf-8"),
        "sha256",
    ).hexdigest()
    return replace(envelope, server_proof=proof)


def _account_options_arguments(
    context: ModuleType,
    *,
    account_user_id: int,
    authorization_version: int = 1,
    expires_at: datetime | None = None,
) -> tuple[int, int, str, datetime, str]:
    nonce_hash = sha256(uuid4().bytes).hexdigest()
    proof_expires_at = expires_at or (datetime.now(UTC) + timedelta(hours=1)).replace(
        microsecond=123456
    )
    canonical = context.account_options_canonical_text(
        account_user_id=account_user_id,
        authorization_version=authorization_version,
        session_nonce_hash=nonce_hash,
        proof_expires_at=proof_expires_at,
    )
    proof = hmac.new(_TEST_CONTEXT_KEY, canonical.encode("utf-8"), "sha256").hexdigest()
    return (
        account_user_id,
        authorization_version,
        nonce_hash,
        proof_expires_at,
        proof,
    )


def _task5_module(name: str) -> ModuleType:
    """把缺失生产模块表现为明确 RED，而不是用 skip 掩盖。"""

    return import_module(name)


def test_idempotency_contract_rejects_mixed_actor_contexts() -> None:
    """一个幂等范围只能绑定一类经过核验的人工履职上下文。"""

    contracts = _task5_module("app.platform.idempotency.contracts")
    common = {
        "school_id": uuid4(),
        "action_code": "master_data.assignment.change",
        "actor_user_id": 1,
        "actor_person_id": uuid4(),
        "actor_context_type": "assignment",
        "actor_assignment_id": uuid4(),
        "actor_assignment_version": 1,
        "actor_guardian_relationship_id": uuid4(),
        "actor_guardian_relationship_version": 1,
        "resource_type": "staff_assignment",
        "resource_id": str(uuid4()),
        "idempotency_key_hash": "a" * 64,
        "request_sha256": "b" * 64,
    }

    with pytest.raises(ValidationError, match="只能绑定一种操作者上下文"):
        contracts.CommandIdempotencyIdentity.model_validate(common)


def test_outbox_contract_exposes_only_stable_non_sensitive_payload() -> None:
    """Outbox 写口不接受姓名、电话、学生资料或现场照片等任意正文。"""

    contracts = _task5_module("app.platform.messaging.contracts")
    event = contracts.OutboxEvent(
        event_id=uuid4(),
        school_id=uuid4(),
        event_key=f"assignment.changed:{uuid4()}",
        event_type="master_data.assignment.changed",
        aggregate_type="staff_assignment",
        aggregate_id=uuid4(),
        aggregate_version=2,
        occurred_at=datetime.now(UTC),
    )

    assert set(event.safe_payload()) == {
        "event_type",
        "aggregate_type",
        "aggregate_id",
        "aggregate_version",
        "occurred_at",
    }
    with pytest.raises(ValidationError):
        contracts.OutboxEvent.model_validate(
            {**event.model_dump(), "student_name": "不应进入 Outbox"}
        )


@contextmanager
def _runtime_transaction(postgres_roles: PostgresRoles) -> Iterator[psycopg.Connection[Any]]:
    """提供真实 runtime 事务；RLS 上下文只能在该事务内有效。"""

    connection = psycopg.connect(
        **postgres_roles.runtime_connection_parameters,
        autocommit=False,
    )
    try:
        yield connection
    finally:
        connection.rollback()
        connection.close()


def _runtime_engine(postgres_roles: PostgresRoles) -> Engine:
    """构造不复用连接的真实 runtime SQLAlchemy Engine。"""

    runtime = postgres_roles.runtime_connection_parameters
    return create_engine(
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


def _migration_environment(postgres_roles: PostgresRoles) -> dict[str, str]:
    """为迁移往返测试构造只含专用测试凭据的环境。"""

    migration = postgres_roles.migration_connection_parameters
    environment = os.environ.copy()
    environment.update(
        {
            "POSTGRES_MIGRATION_HOST": str(migration["host"]),
            "POSTGRES_MIGRATION_PORT": str(migration["port"]),
            "POSTGRES_MIGRATION_DB": str(migration["dbname"]),
            "POSTGRES_MIGRATION_USER": str(migration["user"]),
            "POSTGRES_MIGRATION_PASSWORD": str(migration["password"]),
            "POSTGRES_MIGRATION_CONTEXT_SIGNING_KEY": _TEST_CONTEXT_KEY.decode("ascii"),
        }
    )
    return environment


def _run_alembic(
    environment: dict[str, str],
    *arguments: str,
) -> subprocess.CompletedProcess[str]:
    """Run one bounded Alembic command with the dedicated migration login."""

    return subprocess.run(
        [sys.executable, "-m", "alembic", *arguments],
        cwd=_API_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def _reset_import_schema_to_revision_10(
    postgres_roles: PostgresRoles,
    environment: dict[str, str],
) -> None:
    """Recreate an empty, immutable revision-10 import schema."""

    with postgres_roles.admin_connection() as connection:
        revision_row = connection.execute(
            "SELECT version_num FROM public.alembic_version"
        ).fetchone()
        assert revision_row is not None
        current_revision = str(revision_row[0])
        connection.execute("SET session_replication_role = replica")
        connection.execute(
            "DELETE FROM public.audit_logs WHERE event_type LIKE 'master_data.import.%'"
        )
        if connection.execute(
            "SELECT to_regclass('public.school_fact_change_lineage')"
        ).fetchone() != (None,):
            connection.execute("DELETE FROM public.school_fact_change_lineage")
        connection.execute("DELETE FROM public.master_data_import_takeovers")
        connection.execute("DELETE FROM public.master_data_import_units")
        connection.execute("DELETE FROM public.master_data_import_command_outcomes")
        connection.execute("DELETE FROM public.master_data_import_batches")
        connection.execute(
            "DELETE FROM public.command_idempotency_records "
            "WHERE action_code LIKE 'master_data.import.%' "
            "OR action_code = 'school_facts.confirm'"
        )
        connection.execute("SET session_replication_role = origin")
    if current_revision == "20260823_19":
        downgrade_revision19 = _run_alembic(environment, "downgrade", "20260821_18")
        assert downgrade_revision19.returncode == 0, (
            downgrade_revision19.stdout + downgrade_revision19.stderr
        )
        current_revision = "20260821_18"
    if current_revision == "20260821_18":
        downgrade_revision18 = _run_alembic(environment, "downgrade", "20260820_17")
        assert downgrade_revision18.returncode == 0, (
            downgrade_revision18.stdout + downgrade_revision18.stderr
        )
        with postgres_roles.admin_connection() as connection:
            connection.execute("SET session_replication_role = replica")
            connection.execute("DELETE FROM public.school_versions")
            connection.execute("DELETE FROM public.school_fact_sources")
            connection.execute("SET session_replication_role = origin")
    downgrade = _run_alembic(environment, "downgrade", "20260808_08")
    assert downgrade.returncode == 0, downgrade.stdout + downgrade.stderr
    with postgres_roles.admin_connection() as connection:
        connection.execute("SET ROLE campus_ai_db_owner")
        connection.execute(
            "DELETE FROM public.command_idempotency_records "
            "WHERE action_code LIKE 'master_data.import.%'"
        )
    upgrade = _run_alembic(environment, "upgrade", "20260809_10")
    assert upgrade.returncode == 0, upgrade.stdout + upgrade.stderr


def _revision_10_command_envelope(
    facts: _RlsFacts,
    *,
    request_id: UUID,
    idempotency_key_hash: str,
    replacement_actor: bool = True,
):
    """Create a signed command envelope for the replacement actor."""

    context = _task5_module("app.db.rls_context")
    unsigned = replace(
        _staff_envelope(facts, same_school_other=replacement_actor),
        request_id=request_id,
        context_purpose="command",
        idempotency_key_hash=idempotency_key_hash,
        server_proof="0" * 64,
    )
    return _sign_envelope(context, unsigned)


def _seed_revision_10_takeover_history(
    postgres_roles: PostgresRoles,
    facts: _RlsFacts,
    *,
    state: str,
) -> tuple[UUID, ...]:
    """Commit one revision-10 history shape without disabling constraints."""

    batch_id = uuid4()
    unit_id = uuid4()
    old_record_id = uuid4()
    new_record_id = uuid4()
    takeover_id = uuid4()
    old_request_id = uuid4()
    new_request_id = uuid4()
    old_key_hash = sha256(uuid4().bytes).hexdigest()
    new_key_hash = sha256(uuid4().bytes).hexdigest()
    old_request_sha256 = sha256(uuid4().bytes).hexdigest()
    new_request_sha256 = sha256(uuid4().bytes).hexdigest()
    context = _task5_module("app.db.rls_context")
    envelope = _revision_10_command_envelope(
        facts,
        request_id=new_request_id,
        idempotency_key_hash=new_key_hash,
    )
    old_envelope = _revision_10_command_envelope(
        facts,
        request_id=old_request_id,
        idempotency_key_hash=old_key_hash,
        replacement_actor=False,
    )

    with psycopg.connect(
        **postgres_roles.admin_connection_parameters,
        autocommit=False,
    ) as connection:
        connection.execute("SET LOCAL ROLE campus_ai_runtime")
        context.set_local_execution_context(connection, old_envelope)
        connection.execute("RESET ROLE")
        connection.execute("SET LOCAL ROLE campus_ai_db_owner")
        connection.execute(
            "UPDATE staff_assignments "
            "SET semantic_role_code = 'school_master_data_manager', status = 'confirmed' "
            "WHERE school_id = %s AND id IN (%s, %s)",
            (facts.school_a, facts.assignment_a, facts.assignment_a2),
        )
        connection.execute(
            """
            INSERT INTO command_idempotency_records (
                id, school_id, action_code, actor_user_id, actor_person_id,
                actor_context_type, actor_context_key, actor_assignment_id,
                actor_assignment_version, resource_type, resource_id,
                idempotency_key_hash, request_sha256, status
            ) VALUES (
                %s, %s, 'master_data.import.ordinary_resume', %s, %s,
                'assignment', %s, %s, 1, 'master_data.import_batch', %s,
                %s, %s, 'in_progress'
            )
            """,
            (
                old_record_id,
                facts.school_a,
                facts.user_a,
                facts.person_a,
                f"assignment:{facts.assignment_a}:1",
                facts.assignment_a,
                str(batch_id),
                old_key_hash,
                old_request_sha256,
            ),
        )
        connection.execute(
            """
            INSERT INTO master_data_import_batches (
                id, school_id, mode, source_kind, source_ref, source_sha256,
                filename_summary, source_size_bytes, worksheet_names, status,
                planned_units, succeeded_units, failed_units, pending_units,
                confirmed_by_user_id, confirmed_by_person_id,
                confirmed_by_assignment_id, confirmed_at, request_id,
                idempotency_key_hash, active_idempotency_record_id,
                active_action_code, version
            ) VALUES (
                %s, %s, 'ordinary_atomic_units', 'excel', %s, %s,
                'revision-10-history.xlsx', 1024, '[]'::jsonb, 'applying',
                1, 0, 0, 1, %s, %s, %s, clock_timestamp(), %s,
                %s, %s, 'master_data.import.ordinary_resume', 1
            )
            """,
            (
                batch_id,
                facts.school_a,
                f"protected-{uuid4().hex}",
                sha256(uuid4().bytes).hexdigest(),
                facts.user_a,
                facts.person_a,
                facts.assignment_a,
                old_request_id,
                old_key_hash,
                old_record_id,
            ),
        )
        connection.execute(
            """
            INSERT INTO master_data_import_units (
                id, school_id, batch_id, sequence, row_number, unit_kind, status,
                payload, payload_sha256, soft_anomalies, initial_soft_anomalies,
                last_attempt_idempotency_record_id, last_attempt_completed, version
            ) VALUES (
                %s, %s, %s, 1, 1, 'person', 'ready', '{}'::jsonb, %s,
                '[]'::jsonb, '[]'::jsonb, %s, false, 1
            )
            """,
            (
                unit_id,
                facts.school_a,
                batch_id,
                sha256(uuid4().bytes).hexdigest(),
                old_record_id,
            ),
        )
        connection.commit()

    with psycopg.connect(
        **postgres_roles.admin_connection_parameters,
        autocommit=False,
    ) as connection:
        connection.execute("SET LOCAL ROLE campus_ai_runtime")
        context.set_local_execution_context(connection, envelope)
        connection.execute("RESET ROLE")
        connection.execute("SET LOCAL ROLE campus_ai_db_owner")

        if state != "superseded_source_without_complete_takeover":
            connection.execute(
                """
                INSERT INTO command_idempotency_records (
                    id, school_id, action_code, actor_user_id, actor_person_id,
                    actor_context_type, actor_context_key, actor_assignment_id,
                    actor_assignment_version, resource_type, resource_id,
                    idempotency_key_hash, request_sha256, status
                ) VALUES (
                    %s, %s, 'master_data.import.ordinary_takeover', %s, %s,
                    'assignment', %s, %s, 1, 'master_data.import_batch', %s,
                    %s, %s, 'in_progress'
                )
                """,
                (
                    new_record_id,
                    facts.school_a,
                    facts.user_a2,
                    facts.person_a2,
                    f"assignment:{facts.assignment_a2}:1",
                    facts.assignment_a2,
                    str(batch_id),
                    new_key_hash,
                    new_request_sha256,
                ),
            )
            if state != "active_takeover_without_provenance":
                connection.execute(
                    """
                    INSERT INTO master_data_import_takeovers (
                        id, school_id, batch_id,
                        previous_idempotency_record_id, new_idempotency_record_id,
                        original_actor_user_id, original_actor_person_id,
                        original_actor_assignment_id, original_actor_assignment_version,
                        new_actor_user_id, new_actor_person_id,
                        new_actor_assignment_id, new_actor_assignment_version,
                        takeover_reason, request_id,
                        batch_version_before, batch_version_after
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s, 1,
                        %s, %s, %s, 1, %s, %s, 1, 2
                    )
                    """,
                    (
                        takeover_id,
                        facts.school_a,
                        batch_id,
                        old_record_id,
                        new_record_id,
                        facts.user_a,
                        facts.person_a,
                        facts.assignment_a,
                        facts.user_a2,
                        facts.person_a2,
                        facts.assignment_a2,
                        "revision 10 committed takeover history",
                        new_request_id,
                    ),
                )
            connection.execute(
                "UPDATE master_data_import_units "
                "SET last_attempt_idempotency_record_id = %s "
                "WHERE school_id = %s AND id = %s",
                (new_record_id, facts.school_a, unit_id),
            )
            connection.execute(
                "UPDATE master_data_import_batches "
                "SET active_idempotency_record_id = %s, "
                "active_action_code = 'master_data.import.ordinary_takeover', version = 2 "
                "WHERE school_id = %s AND id = %s",
                (new_record_id, facts.school_a, batch_id),
            )
        else:
            connection.execute(
                """
                UPDATE command_idempotency_records
                SET status = 'terminal_failure',
                    result_type = 'master_data.import_outcome',
                    result_payload = jsonb_build_object(
                        'resource_type', 'master_data.import_batch',
                        'resource_id', %s::text,
                        'resource_version', 1,
                        'outcome_code', 'master_data.import.command_superseded'
                    )
                WHERE school_id = %s AND id = %s
                """,
                (batch_id, facts.school_a, old_record_id),
            )

        if state == "complete_takeover":
            connection.execute(
                "UPDATE staff_assignments SET status = 'rejected' WHERE school_id = %s AND id = %s",
                (facts.school_a, facts.assignment_a),
            )
            connection.execute("SET LOCAL ROLE campus_ai_runtime")
            completed = connection.execute(
                "SELECT app.supersede_master_data_import_takeover_source(%s, %s, %s, %s, 1)",
                (facts.school_a, batch_id, old_record_id, old_request_sha256),
            ).fetchone()
            assert completed == (True,)
        connection.commit()

    return (
        facts.school_a,
        batch_id,
        unit_id,
        old_record_id,
        new_record_id,
        takeover_id,
    )


def _seed_revision_12_second_takeover(
    postgres_roles: PostgresRoles,
    facts: _RlsFacts,
    *,
    batch_id: UUID,
    previous_record_id: UUID,
) -> tuple[UUID, UUID]:
    """Commit a second fully validated takeover edge through revision-12 boundaries."""

    record_id = uuid4()
    takeover_id = uuid4()
    request_id = uuid4()
    key_hash = sha256(uuid4().bytes).hexdigest()
    request_sha256 = sha256(uuid4().bytes).hexdigest()
    context = _task5_module("app.db.rls_context")
    envelope = _revision_10_command_envelope(
        facts,
        request_id=request_id,
        idempotency_key_hash=key_hash,
        replacement_actor=False,
    )
    with psycopg.connect(
        **postgres_roles.admin_connection_parameters,
        autocommit=False,
    ) as connection:
        connection.execute("SET LOCAL ROLE campus_ai_db_owner")
        connection.execute(
            "UPDATE staff_assignments SET status = CASE id "
            "WHEN %s THEN 'confirmed' ELSE 'rejected' END "
            "WHERE school_id = %s AND id IN (%s, %s)",
            (
                facts.assignment_a,
                facts.school_a,
                facts.assignment_a,
                facts.assignment_a2,
            ),
        )
        previous_request_sha256 = connection.execute(
            "SELECT request_sha256 FROM command_idempotency_records "
            "WHERE school_id = %s AND id = %s",
            (facts.school_a, previous_record_id),
        ).fetchone()
        assert previous_request_sha256 is not None
        connection.execute("SET LOCAL ROLE campus_ai_runtime")
        context.set_local_execution_context(connection, envelope)
        connection.execute("SET LOCAL ROLE campus_ai_db_owner")
        connection.execute(
            """
            INSERT INTO command_idempotency_records (
                id, school_id, action_code, actor_user_id, actor_person_id,
                actor_context_type, actor_context_key, actor_assignment_id,
                actor_assignment_version, resource_type, resource_id,
                idempotency_key_hash, request_sha256, status
            ) VALUES (
                %s, %s, 'master_data.import.ordinary_takeover', %s, %s,
                'assignment', %s, %s, 1, 'master_data.import_batch', %s,
                %s, %s, 'in_progress'
            )
            """,
            (
                record_id,
                facts.school_a,
                facts.user_a,
                facts.person_a,
                f"assignment:{facts.assignment_a}:1",
                facts.assignment_a,
                str(batch_id),
                key_hash,
                request_sha256,
            ),
        )
        connection.execute(
            """
            INSERT INTO master_data_import_takeovers (
                id, school_id, batch_id,
                previous_idempotency_record_id, new_idempotency_record_id,
                original_actor_user_id, original_actor_person_id,
                original_actor_assignment_id, original_actor_assignment_version,
                new_actor_user_id, new_actor_person_id,
                new_actor_assignment_id, new_actor_assignment_version,
                takeover_reason, request_id,
                batch_version_before, batch_version_after
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, 1,
                %s, %s, %s, 1, %s, %s, 2, 3
            )
            """,
            (
                takeover_id,
                facts.school_a,
                batch_id,
                previous_record_id,
                record_id,
                facts.user_a2,
                facts.person_a2,
                facts.assignment_a2,
                facts.user_a,
                facts.person_a,
                facts.assignment_a,
                "revision 12 second takeover downgrade fixture",
                request_id,
            ),
        )
        connection.execute(
            "UPDATE master_data_import_units "
            "SET last_attempt_idempotency_record_id = %s "
            "WHERE school_id = %s AND batch_id = %s "
            "AND last_attempt_idempotency_record_id = %s "
            "AND last_attempt_completed IS FALSE",
            (record_id, facts.school_a, batch_id, previous_record_id),
        )
        connection.execute(
            "UPDATE master_data_import_batches "
            "SET active_idempotency_record_id = %s, "
            "active_action_code = 'master_data.import.ordinary_takeover', version = 3 "
            "WHERE school_id = %s AND id = %s AND version = 2",
            (record_id, facts.school_a, batch_id),
        )
        connection.execute("SET LOCAL ROLE campus_ai_runtime")
        superseded = connection.execute(
            "SELECT app.supersede_master_data_import_takeover_source(%s, %s, %s, %s, 2)",
            (
                facts.school_a,
                batch_id,
                previous_record_id,
                str(previous_request_sha256[0]),
            ),
        ).fetchone()
        assert superseded == (True,)
        connection.commit()
    return record_id, takeover_id


def _finish_revision_12_single_takeover(
    postgres_roles: PostgresRoles,
    facts: _RlsFacts,
    *,
    batch_id: UUID,
    new_record_id: UUID,
) -> UUID:
    """把合法单次 checkpoint 收敛成 revision-12 ordinary_takeover 首次终态。"""

    outcome_id = uuid4()
    with psycopg.connect(
        **postgres_roles.admin_connection_parameters,
        autocommit=False,
    ) as connection:
        context = _task5_module("app.db.rls_context")
        connection.execute("SET LOCAL ROLE campus_ai_runtime")
        context.set_local_execution_context(
            connection,
            _staff_envelope(facts, same_school_other=True),
        )
        connection.execute("RESET ROLE")
        connection.execute("SET LOCAL ROLE campus_ai_db_owner")
        updated_unit = connection.execute(
            """
            UPDATE master_data_import_units
            SET status = 'failed', error_code = 'unit_transaction_failed',
                error_field_code = NULL, target_id = NULL, target_version = NULL,
                last_attempt_completed = true, version = version + 1
            WHERE school_id = %s AND batch_id = %s
              AND last_attempt_idempotency_record_id = %s
              AND last_attempt_completed IS FALSE
            """,
            (facts.school_a, batch_id, new_record_id),
        ).rowcount
        assert updated_unit == 1
        updated_batch = connection.execute(
            """
            UPDATE master_data_import_batches
            SET status = 'partially_failed', succeeded_units = 0,
                failed_units = 1, pending_units = 0,
                active_idempotency_record_id = NULL, active_action_code = NULL,
                version = 4
            WHERE school_id = %s AND id = %s
              AND active_idempotency_record_id = %s AND version = 2
            """,
            (facts.school_a, batch_id, new_record_id),
        ).rowcount
        assert updated_batch == 1
        connection.execute(
            """
            INSERT INTO master_data_import_command_outcomes (
                id, school_id, idempotency_record_id, batch_id, action_code,
                status, batch_version, succeeded_units, failed_units, executed_units
            ) VALUES (
                %s, %s, %s, %s, 'master_data.import.ordinary_takeover',
                'partially_failed', 4, '{}'::integer[], ARRAY[1], ARRAY[1]
            )
            """,
            (outcome_id, facts.school_a, new_record_id, batch_id),
        )
        updated_record = connection.execute(
            """
            UPDATE command_idempotency_records
            SET status = 'succeeded', result_type = 'master_data.import_outcome',
                result_payload = jsonb_build_object(
                    'resource_type', 'master_data.import_batch',
                    'resource_id', %s::text,
                    'resource_version', 4,
                    'outcome_code', 'partially_failed'
                )
            WHERE school_id = %s AND id = %s
              AND action_code = 'master_data.import.ordinary_takeover'
              AND status = 'in_progress'
            """,
            (batch_id, facts.school_a, new_record_id),
        ).rowcount
        assert updated_record == 1
        connection.commit()
    return outcome_id


def _revision_10_school_snapshot(
    postgres_roles: PostgresRoles,
    school_id: UUID,
) -> tuple[Any, ...]:
    """Read the complete persisted import/takeover write set for one school."""

    with postgres_roles.admin_connection() as connection:
        connection.execute("SET ROLE campus_ai_db_owner")
        row = connection.execute(
            """
            SELECT
                (SELECT jsonb_agg(to_jsonb(item) ORDER BY item.id)
                 FROM public.command_idempotency_records AS item WHERE school_id = %s),
                (SELECT jsonb_agg(to_jsonb(item) ORDER BY item.id)
                 FROM public.master_data_import_batches AS item WHERE school_id = %s),
                (SELECT jsonb_agg(to_jsonb(item) ORDER BY item.id)
                 FROM public.master_data_import_units AS item WHERE school_id = %s),
                (SELECT jsonb_agg(to_jsonb(item) ORDER BY item.id)
                 FROM public.master_data_import_takeovers AS item WHERE school_id = %s),
                (SELECT jsonb_agg(to_jsonb(item) ORDER BY item.id)
                 FROM public.master_data_import_command_outcomes AS item WHERE school_id = %s),
                (SELECT jsonb_agg(to_jsonb(item) ORDER BY item.id)
                 FROM public.audit_logs AS item WHERE school_id = %s),
                (SELECT jsonb_agg(to_jsonb(item) ORDER BY item.id)
                 FROM public.staff_assignments AS item WHERE school_id = %s)
            """,
            (school_id,) * 7,
        ).fetchone()
    assert row is not None
    return tuple(row)


def _revision_11_takeover_function_snapshot(
    postgres_roles: PostgresRoles,
) -> tuple[str, ...]:
    """Capture every revision-11 function blob that revision 12 transforms."""

    signatures = (
        "app.lock_master_data_import_takeover_source(uuid,uuid,uuid)",
        "app.lock_master_data_import_takeover_assignment(uuid,uuid,uuid,uuid)",
        "app.assert_master_data_import_takeover_integrity(uuid,uuid,uuid)",
        "app.supersede_master_data_import_takeover_source(uuid,uuid,uuid,text,bigint)",
        "app_private.enforce_master_data_import_takeover_integrity()",
        "app.append_master_data_import_audit("
        "integer,uuid,uuid,uuid,bigint,text,text,text,uuid,uuid,uuid,jsonb,jsonb,uuid,text)",
    )
    with postgres_roles.admin_connection() as connection:
        rows = connection.execute(
            "SELECT pg_get_functiondef(item.signature::regprocedure) "
            "FROM unnest(%s::text[]) WITH ORDINALITY AS item(signature, ordinal) "
            "ORDER BY item.ordinal",
            (list(signatures),),
        ).fetchall()
    return tuple(str(row[0]) for row in rows)


def _exercise_revision_10_takeover_history_preflight(
    postgres_roles: PostgresRoles,
    facts: _RlsFacts,
    environment: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    """Prove three inconsistent histories fail closed and one complete history upgrades."""

    failure_categories = (
        "incomplete_takeover_bundle",
        "active_takeover_without_provenance",
        "superseded_source_without_complete_takeover",
    )
    results: list[tuple[str, bool, bool, bool, bool]] = []
    try:
        for category in failure_categories:
            _reset_import_schema_to_revision_10(postgres_roles, environment)
            identifiers = _seed_revision_10_takeover_history(
                postgres_roles,
                facts,
                state=category,
            )
            before = _revision_10_school_snapshot(postgres_roles, facts.school_a)
            upgrade = _run_alembic(environment, "upgrade", "20260809_11")
            combined_output = upgrade.stdout + upgrade.stderr
            with postgres_roles.admin_connection() as connection:
                revision = connection.execute(
                    "SELECT version_num FROM public.alembic_version"
                ).fetchone()
            after = _revision_10_school_snapshot(postgres_roles, facts.school_a)
            results.append(
                (
                    category,
                    upgrade.returncode != 0,
                    revision == ("20260809_10",),
                    after == before,
                    category in combined_output
                    and all(str(identifier) not in combined_output for identifier in identifiers)
                    and all(
                        secret not in combined_output
                        for secret in postgres_roles.protected_test_secrets
                    ),
                )
            )

        _reset_import_schema_to_revision_10(postgres_roles, environment)
        _seed_revision_10_takeover_history(
            postgres_roles,
            facts,
            state="complete_takeover",
        )
        complete_upgrade = _run_alembic(environment, "upgrade", "20260809_11")
        assert complete_upgrade.returncode == 0, complete_upgrade.stdout + complete_upgrade.stderr
    finally:
        current = _run_alembic(environment, "current")
        if "20260809_11" not in current.stdout:
            _reset_import_schema_to_revision_10(postgres_roles, environment)
            restore = _run_alembic(environment, "upgrade", "20260809_11")
            assert restore.returncode == 0, restore.stdout + restore.stderr

    assert results == [(category, True, True, True, True) for category in failure_categories]
    return complete_upgrade


@pytest.mark.integration
def test_task5_transaction_and_responsibility_tables_exist(
    current_master_data_schema: PostgresRoles,
) -> None:
    """责任、幂等和 Outbox 必须进入同一确定迁移图，不能只停留在契约。"""

    expected_tables = {
        "responsibility_types",
        "responsibility_groups",
        "responsibility_group_versions",
        "responsibility_group_memberships",
        "repair_responsibility_grants",
        "command_idempotency_records",
        "outbox_messages",
    }
    with current_master_data_schema.admin_connection() as connection:
        rows = connection.execute(
            """
            SELECT tablename
            FROM pg_catalog.pg_tables
            WHERE schemaname = 'public'
              AND tablename = ANY(%s)
            """,
            (list(expected_tables),),
        ).fetchall()

    assert {row[0] for row in rows} == expected_tables


@pytest.mark.integration
def test_location_does_not_own_repair_responsibility(
    current_master_data_schema: PostgresRoles,
) -> None:
    """地点表只能保存地点事实，不能重新混入人员或责任绑定。"""

    with current_master_data_schema.admin_connection() as connection:
        columns = {
            row[0]
            for row in connection.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'campus_locations'
                """
            ).fetchall()
        }

    assert columns.isdisjoint({"responsible_user_id", "assignment_id", "responsibility_group_id"})


@pytest.mark.integration
def test_runtime_without_rls_context_fails_closed(
    current_master_data_schema: PostgresRoles,
    rls_facts: _RlsFacts,
) -> None:
    """缺少可信上下文时 runtime 对敏感原表必须在 ACL/RLS 边界失败。"""

    # 表中确有事实，因而权限错误不能被空结果误报为安全通过。
    assert rls_facts.person_a is not None

    with pytest.raises(psycopg.errors.InsufficientPrivilege, match="school_people"):
        with _runtime_transaction(current_master_data_schema) as connection:
            connection.execute("SELECT id FROM public.school_people").fetchall()


@pytest.mark.integration
def test_runtime_cannot_delete_audit_or_create_table(
    current_master_data_schema: PostgresRoles,
) -> None:
    """应用运行身份没有审计删除或 DDL 能力。"""

    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with _runtime_transaction(current_master_data_schema) as connection:
            connection.execute("DELETE FROM public.audit_logs")

    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with _runtime_transaction(current_master_data_schema) as connection:
            connection.execute("CREATE TABLE public.forbidden(id integer)")


@pytest.mark.integration
def test_valid_context_does_not_restore_runtime_sensitive_base_select(
    current_master_data_schema: PostgresRoles,
    rls_facts: _RlsFacts,
) -> None:
    """合法签名 context 也不能给 runtime 恢复敏感 School Facts 原表 SELECT。"""

    context = _task5_module("app.db.rls_context")
    envelope = _staff_envelope(rls_facts)
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match="school_people"):
        with _runtime_transaction(current_master_data_schema) as connection:
            context.set_local_execution_context(connection, envelope)
            connection.execute("SELECT id FROM public.school_people ORDER BY id").fetchall()


@pytest.mark.integration
def test_bound_context_rejects_cross_school_insert(
    current_master_data_schema: PostgresRoles,
    rls_facts: _RlsFacts,
) -> None:
    """合法 A 校上下文也不能写入 B 校行。"""

    context = _task5_module("app.db.rls_context")
    envelope = replace(
        _staff_envelope(rls_facts),
        context_purpose="command",
        idempotency_key_hash=sha256(uuid4().bytes).hexdigest(),
    )
    envelope = _sign_envelope(context, envelope)
    with _runtime_transaction(current_master_data_schema) as connection:
        context.set_local_execution_context(connection, envelope)
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            connection.execute(
                """
                INSERT INTO public.responsibility_types (
                    id, school_id, code, display_name, status, version
                ) VALUES (%s, %s, 'repair', '维修责任', 'active', 1)
                """,
                (uuid4(), rls_facts.school_b),
            )


@pytest.mark.integration
def test_read_context_cannot_write_its_own_school(
    current_master_data_schema: PostgresRoles,
    rls_facts: _RlsFacts,
) -> None:
    """同校只读证明也不能被复用为任何主数据写权限。"""

    context = _task5_module("app.db.rls_context")
    envelope = _staff_envelope(rls_facts)
    with _runtime_transaction(current_master_data_schema) as connection:
        context.set_local_execution_context(connection, envelope)
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            connection.execute(
                """
                INSERT INTO public.responsibility_types (
                    id, school_id, code, display_name, status, version
                ) VALUES (%s, %s, 'repair-read-forbidden', '只读不可写', 'active', 1)
                """,
                (uuid4(), rls_facts.school_a),
            )


@pytest.mark.integration
def test_runtime_can_only_append_a_matching_safe_outbox_row(
    current_master_data_schema: PostgresRoles,
    rls_facts: _RlsFacts,
) -> None:
    """命令上下文可以追加同校安全事件，但列 ACL 不开放 Outbox 读取或改写。"""

    context = _task5_module("app.db.rls_context")
    envelope = replace(
        _staff_envelope(rls_facts),
        context_purpose="command",
        idempotency_key_hash="d" * 64,
    )
    envelope = _sign_envelope(context, envelope)
    event_id = uuid4()
    occurred_at = datetime(2026, 1, 1, tzinfo=UTC)
    aggregate_id = uuid4()
    payload = {
        "event_type": "master_data.assignment.changed",
        "aggregate_type": "staff_assignment",
        "aggregate_id": str(aggregate_id),
        "aggregate_version": 2,
        "occurred_at": occurred_at.isoformat(),
    }
    with _runtime_transaction(current_master_data_schema) as connection:
        context.set_local_execution_context(connection, envelope)
        connection.execute(
            """
            INSERT INTO public.outbox_messages (
                id, school_id, event_key, event_type, aggregate_type,
                aggregate_id, aggregate_version, occurred_at, payload
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                event_id,
                rls_facts.school_a,
                f"assignment.changed:{event_id}",
                payload["event_type"],
                payload["aggregate_type"],
                aggregate_id,
                2,
                occurred_at,
                Jsonb(payload),
            ),
        )
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            connection.execute("SELECT id FROM public.outbox_messages").fetchall()


@pytest.mark.integration
def test_runtime_sqlalchemy_writers_use_real_minimum_acl(
    current_master_data_schema: PostgresRoles,
    rls_facts: _RlsFacts,
) -> None:
    """生产 Audit/Outbox Writer 必须在无表级 SELECT 的 runtime 下真实提交。"""

    context = _task5_module("app.db.rls_context")
    idempotency_key = f"task5-runtime-writer-{uuid4()}"
    idempotency_hash = sha256(idempotency_key.encode("utf-8")).hexdigest()
    envelope = replace(
        _staff_envelope(rls_facts),
        context_purpose="command",
        idempotency_key_hash=idempotency_hash,
    )
    envelope = _sign_envelope(context, envelope)
    event = OutboxEvent(
        event_id=uuid4(),
        school_id=SchoolId(rls_facts.school_a),
        event_key=f"assignment.changed:{uuid4()}",
        event_type="master_data.assignment.changed",
        aggregate_type="staff_assignment",
        aggregate_id=rls_facts.assignment_a,
        aggregate_version=2,
        occurred_at=datetime.now(UTC),
    )
    actor = platform_contracts._issue_staff_actor_context(  # pyright: ignore[reportPrivateUsage]
        account_user_id=rls_facts.user_a,
        school_id=SchoolId(rls_facts.school_a),
        person_id=PersonId(rls_facts.person_a),
        assignment_id=AssignmentId(rls_facts.assignment_a),
        assignment_version=1,
        authorization_version=1,
    )
    command = platform_contracts._issue_command_execution_context(  # pyright: ignore[reportPrivateUsage]
        actor=actor,
        request_id=envelope.request_id,
        idempotency_key=idempotency_key,
    )
    engine = _runtime_engine(current_master_data_schema)
    legacy_request_id = str(uuid4())
    try:
        with Session(engine) as session:
            context.set_local_execution_context(session, envelope)
            SqlAlchemyOutboxWriter(session).append(event)
            audit = append_business_audit_log(
                session=session,
                context=command,
                record=BusinessAuditRecord(
                    event_type="master_data.assignment.changed",
                    target_type="staff_assignment",
                    target_id=event.aggregate_id,
                    before_summary=BusinessAuditSummary(
                        state_code="assignment.absent",
                        stable_reference_ids=(event.aggregate_id,),
                    ),
                    after_summary=BusinessAuditSummary(
                        state_code="assignment.confirmed",
                        resource_version=1,
                        stable_reference_ids=(event.aggregate_id,),
                        fact_codes=("assignment.updated",),
                    ),
                    reason_code="manual.assignment-confirmed",
                ),
            )
            assert audit.idempotency_key_hash == idempotency_hash
            session.commit()
        with Session(engine) as session:
            legacy = append_audit_log(
                session=session,
                actor_user_id=rls_facts.user_a,
                event_type="identity.mapping.created",
                target_type="user",
                target_id=str(rls_facts.user_a),
                before_summary={},
                after_summary={"user_id": rls_facts.user_a},
                request_id=legacy_request_id,
                reason="兼容审计窄函数测试",
            )
            assert legacy.id >= 1
            session.commit()
    finally:
        engine.dispose()

    with current_master_data_schema.admin_connection() as connection:
        assert connection.execute(
            "SELECT count(*) FROM public.outbox_messages WHERE id = %s",
            (event.event_id,),
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT count(*) FROM public.audit_logs WHERE request_id = %s",
            (str(envelope.request_id),),
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT school_id, event_type FROM public.audit_logs WHERE request_id = %s",
            (legacy_request_id,),
        ).fetchone() == (None, "identity.mapping.created")


@pytest.mark.integration
def test_runtime_cannot_choose_outbox_state_or_bypass_business_audit_writer(
    current_master_data_schema: PostgresRoles,
    rls_facts: _RlsFacts,
) -> None:
    """Outbox 初态由数据库确定，审计表只能经受控函数追加。"""

    context = _task5_module("app.db.rls_context")
    idempotency_hash = sha256(uuid4().bytes).hexdigest()
    envelope = replace(
        _staff_envelope(rls_facts),
        context_purpose="command",
        idempotency_key_hash=idempotency_hash,
    )
    envelope = _sign_envelope(context, envelope)
    event_id = uuid4()
    aggregate_id = uuid4()
    occurred_at = datetime.now(UTC)
    payload = {
        "event_type": "master_data.assignment.changed",
        "aggregate_type": "staff_assignment",
        "aggregate_id": str(aggregate_id),
        "aggregate_version": 2,
        "occurred_at": occurred_at.isoformat(),
    }
    with _runtime_transaction(current_master_data_schema) as connection:
        context.set_local_execution_context(connection, envelope)
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            connection.execute(
                """
                INSERT INTO public.outbox_messages (
                    id, school_id, event_key, event_type, aggregate_type,
                    aggregate_id, aggregate_version, occurred_at, payload, status
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'sent')
                """,
                (
                    event_id,
                    rls_facts.school_a,
                    f"assignment.changed:{event_id}",
                    payload["event_type"],
                    payload["aggregate_type"],
                    aggregate_id,
                    2,
                    occurred_at,
                    Jsonb(payload),
                ),
            )

    with _runtime_transaction(current_master_data_schema) as connection:
        context.set_local_execution_context(connection, envelope)
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            connection.execute(
                """
                INSERT INTO public.audit_logs (
                    actor_user_id, event_type, target_type, target_id,
                    before_summary, after_summary, request_id, reason
                ) VALUES (%s, 'forged.legacy', 'staff_assignment', %s,
                          '{}'::jsonb, '{}'::jsonb, %s, '伪造兼容审计')
                """,
                (
                    rls_facts.user_a,
                    rls_facts.assignment_a,
                    str(envelope.request_id),
                ),
            )

    unsafe_summary = {
        "state_code": "assignment.confirmed",
        "resource_version": 1,
        "stable_reference_ids": [str(rls_facts.assignment_a)],
        "fact_codes": [],
        "student_name": "李明",
    }
    safe_summary = {
        "state_code": "assignment.confirmed",
        "resource_version": 1,
        "stable_reference_ids": [str(rls_facts.assignment_a)],
        "fact_codes": [],
    }
    absent_summary = {
        "state_code": "assignment.absent",
        "resource_version": None,
        "stable_reference_ids": [str(rls_facts.assignment_a)],
        "fact_codes": [],
    }
    with _runtime_transaction(current_master_data_schema) as connection:
        context.set_local_execution_context(connection, envelope)
        with pytest.raises(psycopg.errors.CheckViolation):
            connection.execute(
                """
                SELECT app.append_business_audit(
                    %s, %s, %s, %s, %s, NULL, NULL, 'assignment', %s,
                    'master_data.assignment.changed', 'staff_assignment', %s,
                    %s, %s, %s, 'manual.assignment-confirmed'
                )
                """,
                (
                    rls_facts.user_a,
                    rls_facts.school_a,
                    rls_facts.person_a,
                    rls_facts.assignment_a,
                    1,
                    idempotency_hash,
                    uuid4(),
                    Jsonb(safe_summary),
                    Jsonb(absent_summary),
                    envelope.request_id,
                ),
            )
    with _runtime_transaction(current_master_data_schema) as connection:
        context.set_local_execution_context(connection, envelope)
        with pytest.raises(psycopg.errors.InvalidParameterValue, match="unsafe business audit"):
            connection.execute(
                """
                SELECT app.append_business_audit(
                    %s, %s, %s, %s, %s, NULL, NULL, 'assignment', %s,
                    'master_data.assignment.changed', 'staff_assignment', %s,
                    %s, %s, %s, 'manual.assignment-confirmed'
                )
                """,
                (
                    rls_facts.user_a,
                    rls_facts.school_a,
                    rls_facts.person_a,
                    rls_facts.assignment_a,
                    1,
                    idempotency_hash,
                    rls_facts.assignment_a,
                    Jsonb(unsafe_summary),
                    Jsonb(safe_summary),
                    envelope.request_id,
                ),
            )

    for null_field in ("stable_reference_ids", "fact_codes"):
        null_summary = dict(safe_summary)
        null_summary[null_field] = [None]
        with _runtime_transaction(current_master_data_schema) as connection:
            context.set_local_execution_context(connection, envelope)
            with pytest.raises(
                psycopg.errors.InvalidParameterValue,
                match="unsafe business audit",
            ):
                connection.execute(
                    """
                    SELECT app.append_business_audit(
                        %s, %s, %s, %s, %s, NULL, NULL, 'assignment', %s,
                        'master_data.assignment.changed', 'staff_assignment', %s,
                        %s, %s, %s, 'manual.assignment-confirmed'
                    )
                    """,
                    (
                        rls_facts.user_a,
                        rls_facts.school_a,
                        rls_facts.person_a,
                        rls_facts.assignment_a,
                        1,
                        idempotency_hash,
                        rls_facts.assignment_a,
                        Jsonb(null_summary),
                        Jsonb(safe_summary),
                        envelope.request_id,
                    ),
                )

    with _runtime_transaction(current_master_data_schema) as connection:
        context.set_local_execution_context(connection, envelope)
        with pytest.raises(psycopg.errors.InvalidParameterValue, match="unsafe business audit"):
            connection.execute(
                """
                SELECT app.append_business_audit(
                    %s, %s, %s, %s, %s, NULL, NULL, 'assignment', %s,
                    'master_data.assignment.changed', 'staff_assignment', %s,
                    %s, %s, %s, 'phone.13800000000'
                )
                """,
                (
                    rls_facts.user_a,
                    rls_facts.school_a,
                    rls_facts.person_a,
                    rls_facts.assignment_a,
                    1,
                    idempotency_hash,
                    rls_facts.assignment_a,
                    Jsonb(safe_summary),
                    Jsonb(safe_summary),
                    envelope.request_id,
                ),
            )

    with _runtime_transaction(current_master_data_schema) as connection:
        with pytest.raises(psycopg.errors.InvalidParameterValue, match="invalid legacy audit"):
            connection.execute(
                """
                SELECT app.append_legacy_audit(
                    %s, 'authorization.superadmin.granted', 'user_role', %s,
                    '{}'::jsonb, '{}'::jsonb, %s, '不得冒充业务审计'
                )
                """,
                (
                    rls_facts.user_a,
                    str(rls_facts.assignment_a),
                    str(envelope.request_id),
                ),
            )


@pytest.mark.integration
def test_business_audit_rejects_python_and_database_context_mismatch(
    current_master_data_schema: PostgresRoles,
    rls_facts: _RlsFacts,
) -> None:
    """服务层 context 与已绑定数据库 envelope 不一致时必须失败，不得静默改写归属。"""

    context = _task5_module("app.db.rls_context")
    bound_idempotency_key = f"task5-bound-{uuid4()}"
    envelope = replace(
        _staff_envelope(rls_facts),
        context_purpose="command",
        idempotency_key_hash=sha256(bound_idempotency_key.encode("utf-8")).hexdigest(),
    )
    envelope = _sign_envelope(context, envelope)
    actor = platform_contracts._issue_staff_actor_context(  # pyright: ignore[reportPrivateUsage]
        account_user_id=rls_facts.user_a,
        school_id=SchoolId(rls_facts.school_a),
        person_id=PersonId(rls_facts.person_a),
        assignment_id=AssignmentId(rls_facts.assignment_a),
        assignment_version=1,
        authorization_version=1,
    )
    mismatched_command = platform_contracts._issue_command_execution_context(  # pyright: ignore[reportPrivateUsage]
        actor=actor,
        request_id=uuid4(),
        idempotency_key=f"task5-mismatched-{uuid4()}",
    )
    record = BusinessAuditRecord(
        event_type="master_data.assignment.changed",
        target_type="staff_assignment",
        target_id=rls_facts.assignment_a,
        before_summary=BusinessAuditSummary(
            state_code="assignment.absent",
            stable_reference_ids=(rls_facts.assignment_a,),
        ),
        after_summary=BusinessAuditSummary(
            state_code="assignment.confirmed",
            resource_version=1,
            stable_reference_ids=(rls_facts.assignment_a,),
            fact_codes=("assignment.updated",),
        ),
        reason_code="manual.assignment-confirmed",
    )
    engine = _runtime_engine(current_master_data_schema)
    try:
        with Session(engine) as session:
            context.set_local_execution_context(session, envelope)
            with pytest.raises(DBAPIError, match="business audit execution context mismatch"):
                append_business_audit_log(
                    session=session,
                    context=mismatched_command,
                    record=record,
                )
            session.rollback()
    finally:
        engine.dispose()

    with current_master_data_schema.admin_connection() as connection:
        assert connection.execute(
            "SELECT count(*) FROM public.audit_logs WHERE request_id IN (%s, %s)",
            (str(envelope.request_id), str(mismatched_command.request_id)),
        ).fetchone() == (0,)


@pytest.mark.integration
def test_runtime_idempotency_is_bound_to_actor_and_terminal_result_is_immutable(
    current_master_data_schema: PostgresRoles,
    rls_facts: _RlsFacts,
) -> None:
    """同校另一 actor 看不到或污染幂等行，首次终态结果也不可重写。"""

    context = _task5_module("app.db.rls_context")
    idempotency_hash = sha256(uuid4().bytes).hexdigest()
    envelope_a = replace(
        _staff_envelope(rls_facts),
        context_purpose="command",
        idempotency_key_hash=idempotency_hash,
    )
    envelope_a = _sign_envelope(context, envelope_a)
    identity = CommandIdempotencyIdentity(
        school_id=SchoolId(rls_facts.school_a),
        action_code="master_data.assignment.change",
        actor_user_id=rls_facts.user_a,
        actor_person_id=PersonId(rls_facts.person_a),
        actor_context_type="assignment",
        actor_assignment_id=AssignmentId(rls_facts.assignment_a),
        actor_assignment_version=1,
        resource_type="staff_assignment",
        resource_id=str(rls_facts.assignment_a),
        idempotency_key_hash=idempotency_hash,
        request_sha256="b" * 64,
    )
    engine = _runtime_engine(current_master_data_schema)
    try:
        with Session(engine) as session:
            context.set_local_execution_context(session, envelope_a)
            record = SqlAlchemyCommandIdempotencyRepository(session).acquire(identity)
            session.commit()

        envelope_b = replace(
            _staff_envelope(rls_facts, same_school_other=True),
            context_purpose="command",
            idempotency_key_hash=idempotency_hash,
        )
        envelope_b = _sign_envelope(context, envelope_b)
        with Session(engine) as session:
            context.set_local_execution_context(session, envelope_b)
            assert (
                session.execute(
                    text("SELECT count(*) FROM command_idempotency_records WHERE id = :record_id"),
                    {"record_id": record.record_id},
                ).scalar_one()
                == 0
            )
            session.rollback()

        with Session(engine) as session:
            context.set_local_execution_context(session, envelope_a)
            with pytest.raises(DBAPIError, match="permission denied"):
                session.execute(
                    text(
                        "UPDATE command_idempotency_records "
                        "SET request_sha256 = :request_sha256 WHERE id = :record_id"
                    ),
                    {"request_sha256": "c" * 64, "record_id": record.record_id},
                )
            session.rollback()

        result = SafeCommandResult(
            resource_type="staff_assignment",
            resource_id=rls_facts.assignment_a,
            resource_version=2,
            outcome_code="updated",
        )
        with Session(engine) as session:
            context.set_local_execution_context(session, envelope_a)
            completed = SqlAlchemyCommandIdempotencyRepository(session).mark_succeeded(
                identity,
                result_type="master_data.assignment.result",
                result=result,
            )
            assert completed.status == "succeeded"
            session.commit()

        with Session(engine) as session:
            context.set_local_execution_context(session, envelope_a)
            with pytest.raises(DBAPIError, match="idempotency result is immutable"):
                session.execute(
                    text(
                        """
                        UPDATE command_idempotency_records
                        SET result_payload = jsonb_build_object(
                            'resource_type', 'staff_assignment',
                            'resource_id', CAST(:resource_id AS text),
                            'resource_version', 3,
                            'outcome_code', 'overwritten'
                        )
                        WHERE id = :record_id
                        """
                    ),
                    {
                        "resource_id": rls_facts.assignment_a,
                        "record_id": record.record_id,
                    },
                )
            session.rollback()
    finally:
        engine.dispose()


@pytest.mark.integration
@pytest.mark.parametrize(
    ("guc_name", "forged_value"),
    [
        ("app.school_id", str(uuid4())),
        ("app.school_id", "x' OR true --"),
        ("app.account_user_id", "999999999"),
        ("app.request_id", str(uuid4())),
        ("app.idempotency_key_hash", "f" * 64),
    ],
)
def test_direct_guc_tampering_fails_before_read(
    current_master_data_schema: PostgresRoles,
    rls_facts: _RlsFacts,
    guc_name: str,
    forged_value: str,
) -> None:
    """GUC 只是载体；篡改学校、actor、请求或幂等字段都破坏二次证明。"""

    context = _task5_module("app.db.rls_context")
    envelope = replace(
        _staff_envelope(rls_facts),
        context_purpose="command",
        idempotency_key_hash="e" * 64,
    )
    proof = hmac.new(
        _TEST_CONTEXT_KEY,
        context.execution_context_canonical_text(envelope).encode("utf-8"),
        "sha256",
    ).hexdigest()
    envelope = replace(envelope, server_proof=proof)
    with _runtime_transaction(current_master_data_schema) as connection:
        context.set_local_execution_context(connection, envelope)
        connection.execute("SELECT set_config(%s, %s, true)", (guc_name, forged_value))
        assert connection.execute("SELECT current_setting(%s, true)", (guc_name,)).fetchone() == (
            forged_value,
        )
        with pytest.raises(psycopg.DatabaseError, match="RLS context integrity check failed"):
            connection.execute("SELECT app.require_school_context()").fetchall()


@pytest.mark.integration
def test_context_is_transaction_and_backend_bound(
    current_master_data_schema: PostgresRoles,
    rls_facts: _RlsFacts,
) -> None:
    """合法证明和二次 GUC 不能复制到下一事务或另一 backend。"""

    context = _task5_module("app.db.rls_context")
    envelope = _staff_envelope(rls_facts)
    with psycopg.connect(
        **current_master_data_schema.runtime_connection_parameters,
        autocommit=False,
    ) as first:
        context.set_local_execution_context(first, envelope)
        assert first.execute("SELECT app.require_school_context()").fetchone() == (
            rls_facts.school_a,
        )

        guc_names = (
            "app.context_key_version",
            "app.school_id",
            "app.account_user_id",
            "app.person_id",
            "app.context_kind",
            "app.assignment_id",
            "app.assignment_version",
            "app.guardian_relationship_id",
            "app.guardian_relationship_version",
            "app.authorization_version",
            "app.request_id",
            "app.context_purpose",
            "app.idempotency_key_hash",
            "app.session_nonce_hash",
            "app.proof_expires_at",
            "app.server_proof",
            "app.binding_database",
            "app.binding_backend_pid",
            "app.binding_xact_id",
            "app.binding_proof",
        )
        copied_values: dict[str, object] = {}
        for name in guc_names:
            row = first.execute(
                "SELECT current_setting(%s, true)",
                (name,),
            ).fetchone()
            assert row is not None
            copied_values[name] = row[0]
        with psycopg.connect(
            **current_master_data_schema.runtime_connection_parameters,
            autocommit=False,
        ) as second:
            for name, value in copied_values.items():
                second.execute("SELECT set_config(%s, %s, true)", (name, value))
            with pytest.raises(
                psycopg.DatabaseError,
                match="RLS context integrity check failed",
            ):
                second.execute("SELECT app.require_school_context()").fetchall()

        first.commit()
        with pytest.raises(psycopg.DatabaseError, match="master data RLS context is missing"):
            first.execute("SELECT app.require_school_context()").fetchall()


@pytest.mark.integration
def test_account_options_proof_returns_only_current_minimum_context(
    current_master_data_schema: PostgresRoles,
    rls_facts: _RlsFacts,
) -> None:
    """未选学校前只能经独立证明枚举最小履职键、版本和安全标签。"""

    context = _task5_module("app.db.rls_context")
    arguments = _account_options_arguments(
        context,
        account_user_id=rls_facts.user_a,
    )
    with _runtime_transaction(current_master_data_schema) as connection:
        rows = connection.execute(
            """
            SELECT *
            FROM app.list_execution_context_options(%s, %s, %s, %s, %s)
            """,
            arguments,
        ).fetchall()

    assert rows == [
        (
            "assignment",
            rls_facts.assignment_a,
            1,
            rls_facts.school_a,
            rls_facts.person_a,
            "Task 5 学校 A · Task 5 组织",
        )
    ]


@pytest.mark.integration
@pytest.mark.parametrize("failure_kind", ["tampered", "expired", "stale_version"])
def test_account_options_rejects_invalid_or_stale_proof_before_listing(
    current_master_data_schema: PostgresRoles,
    rls_facts: _RlsFacts,
    failure_kind: str,
) -> None:
    """伪造、过期证明和已变化 security version 都不能枚举跨校身份。"""

    context = _task5_module("app.db.rls_context")
    authorization_version = 2 if failure_kind == "stale_version" else 1
    expires_at = datetime.now(UTC) - timedelta(seconds=1) if failure_kind == "expired" else None
    arguments = list(
        _account_options_arguments(
            context,
            account_user_id=rls_facts.user_a,
            authorization_version=authorization_version,
            expires_at=expires_at,
        )
    )
    if failure_kind == "tampered":
        arguments[-1] = "f" * 64

    with pytest.raises(psycopg.DatabaseError, match="proof|actor is not current"):
        with _runtime_transaction(current_master_data_schema) as connection:
            connection.execute(
                "SELECT * FROM app.list_execution_context_options(%s, %s, %s, %s, %s)",
                tuple(arguments),
            ).fetchall()


@pytest.mark.integration
@pytest.mark.parametrize("argument_index", [2, 3, 4])
def test_account_options_rejects_null_proof_components(
    current_master_data_schema: PostgresRoles,
    rls_facts: _RlsFacts,
    argument_index: int,
) -> None:
    """SQL NULL 不能利用 PL/pgSQL 三值逻辑绕过 nonce、到期时间或证明校验。"""

    context = _task5_module("app.db.rls_context")
    arguments: list[object | None] = list(
        _account_options_arguments(context, account_user_id=rls_facts.user_a)
    )
    arguments[argument_index] = None
    with pytest.raises(psycopg.DatabaseError, match="invalid or expired account options proof"):
        with _runtime_transaction(current_master_data_schema) as connection:
            connection.execute(
                "SELECT * FROM app.list_execution_context_options(%s, %s, %s, %s, %s)",
                tuple(arguments),
            ).fetchall()


@pytest.mark.integration
@pytest.mark.parametrize(
    "parameter_name",
    ["session_nonce_hash", "proof_expires_at", "server_proof"],
)
def test_binding_rejects_null_proof_components(
    current_master_data_schema: PostgresRoles,
    rls_facts: _RlsFacts,
    parameter_name: str,
) -> None:
    """绑定函数必须在读取任何权威关系前拒绝 NULL 证明组成部分。"""

    context = _task5_module("app.db.rls_context")
    parameters = context.execution_context_parameters(_staff_envelope(rls_facts))
    parameters[parameter_name] = None
    with pytest.raises(psycopg.DatabaseError, match="invalid or expired execution context proof"):
        with _runtime_transaction(current_master_data_schema) as connection:
            connection.execute(
                """
                SELECT app.bind_execution_context(
                    %(school_id)s, %(account_user_id)s, %(person_id)s,
                    %(context_kind)s, %(assignment_id)s, %(assignment_version)s,
                    %(guardian_relationship_id)s, %(guardian_relationship_version)s,
                    %(authorization_version)s, %(request_id)s, %(context_purpose)s,
                    %(idempotency_key_hash)s, %(session_nonce_hash)s,
                    %(proof_expires_at)s, %(server_proof)s
                )
                """,
                parameters,
            )


@pytest.mark.integration
@pytest.mark.parametrize("failure_kind", ["expired", "wrong_assignment", "wrong_school"])
def test_binding_revalidates_expiry_and_current_database_facts(
    current_master_data_schema: PostgresRoles,
    rls_facts: _RlsFacts,
    failure_kind: str,
) -> None:
    """有效 HMAC 也不能复活过期证明或不存在的学校/任职关系。"""

    context = _task5_module("app.db.rls_context")
    envelope = _staff_envelope(rls_facts)
    if failure_kind == "expired":
        envelope = replace(envelope, proof_expires_at=datetime.now(UTC) - timedelta(seconds=1))
    elif failure_kind == "wrong_assignment":
        envelope = replace(envelope, assignment_id=uuid4())
    else:
        envelope = replace(envelope, school_id=UUID(str(rls_facts.school_b)))
    envelope = _sign_envelope(context, envelope)

    with pytest.raises(psycopg.DatabaseError, match="expired|not current"):
        with _runtime_transaction(current_master_data_schema) as connection:
            context.set_local_execution_context(connection, envelope)


@pytest.mark.integration
def test_runtime_can_verify_only_the_signing_key_fingerprint(
    current_master_data_schema: PostgresRoles,
) -> None:
    """运行账号只能取得部署密钥指纹，不能读取私有密钥表。"""

    expected_fingerprint = sha256(_TEST_CONTEXT_KEY).hexdigest()
    with _runtime_transaction(current_master_data_schema) as connection:
        assert connection.execute(
            "SELECT * FROM app.context_signing_key_fingerprint()"
        ).fetchone() == (1, expected_fingerprint)
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            connection.execute("SELECT signing_key FROM app_private.context_proof_keys")


@pytest.mark.integration
def test_sqlalchemy_session_binds_the_same_transaction_local_context(
    current_master_data_schema: PostgresRoles,
    rls_facts: _RlsFacts,
) -> None:
    """正式 UoW 使用的 SQLAlchemy Session 走同一受控函数且不会自动提交。"""

    runtime = current_master_data_schema.runtime_connection_parameters
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
    context = _task5_module("app.db.rls_context")
    try:
        with Session(engine) as session:
            context.set_local_execution_context(session, _staff_envelope(rls_facts))
            assert (
                session.execute(text("SELECT app.require_school_context()")).scalar_one()
                == rls_facts.school_a
            )
            assert session.in_transaction()
            session.rollback()
            with pytest.raises(DBAPIError, match="master data RLS context is missing"):
                session.execute(text("SELECT app.require_school_context()")).all()
    finally:
        engine.dispose()


@pytest.mark.integration
def test_rls_and_function_catalog_security_is_exact(
    current_master_data_schema: PostgresRoles,
) -> None:
    """敏感表强制 RLS，上下文与审计窄函数归最小 Owner 且不公开执行。"""

    context_functions = {
        "bind_execution_context_rev07",
        "bind_root_reactivation_context",
        "bind_school_reactivation_context",
        "context_signing_key_fingerprint",
        "list_execution_context_options",
        "list_execution_context_options_rev07",
        "list_school_reactivation_candidates",
        "bind_execution_context",
        "require_root_reactivation_context",
        "require_school_context",
        "require_school_reactivation_context",
        "school_fact_content_version_terminal",
        "school_fact_location_history_interval_is_resolvable",
        "school_fact_location_history_is_resolvable",
        "school_fact_root_is_effective_at",
        "school_fact_school_snapshot_as_of",
        "school_reactivation_replay_result",
        "validate_school_reactivation_candidate",
    }
    audit_functions = {
        "append_business_audit",
        "append_legacy_audit",
        "append_master_data_import_audit",
    }
    recovery_functions = {"verify_staff_duty_binding_outbox"}
    takeover_functions = {
        "assert_master_data_import_takeover_integrity",
        "lock_master_data_import_takeover_assignment",
        "lock_master_data_import_takeover_source",
        "read_master_data_import_attempt_lineage",
        "read_master_data_import_succeeded_attempt",
        "supersede_master_data_import_takeover_source",
    }
    listed_functions = context_functions | audit_functions | recovery_functions | takeover_functions
    with current_master_data_schema.admin_connection() as connection:
        table_rows = connection.execute(
            """
            SELECT relname, relrowsecurity, relforcerowsecurity
            FROM pg_class AS relation
            JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
            WHERE namespace.nspname = 'public'
              AND relname IN ('schools', 'school_people', 'staff_assignments',
                              'responsibility_groups', 'command_idempotency_records',
                              'outbox_messages', 'repair_scope_bindings',
                              'repair_tickets')
            """
        ).fetchall()
        function_rows = connection.execute(
            """
            SELECT procedure.proname, owner.rolname, procedure.prosecdef,
                   procedure.proconfig,
                   has_function_privilege('public', procedure.oid, 'EXECUTE')
            FROM pg_proc AS procedure
            JOIN pg_namespace AS namespace ON namespace.oid = procedure.pronamespace
            JOIN pg_roles AS owner ON owner.oid = procedure.proowner
            WHERE namespace.nspname = 'app'
              AND procedure.proname::text = ANY(%s::text[])
            """,
            (sorted(listed_functions),),
        ).fetchall()
        function_acl = connection.execute(
            """
            SELECT procedure.proname,
                   COALESCE(grantee.rolname, 'PUBLIC'),
                   acl.privilege_type
            FROM pg_proc AS procedure
            JOIN pg_namespace AS namespace ON namespace.oid = procedure.pronamespace
            CROSS JOIN LATERAL aclexplode(
                COALESCE(procedure.proacl, acldefault('f'::"char", procedure.proowner))
            ) AS acl
            LEFT JOIN pg_roles AS grantee ON grantee.oid = acl.grantee
            WHERE namespace.nspname = 'app'
              AND procedure.proname::text = ANY(%s::text[])
            ORDER BY procedure.proname, COALESCE(grantee.rolname, 'PUBLIC')
            """,
            (sorted(listed_functions),),
        ).fetchall()
        policy_rows = connection.execute(
            """
            SELECT relation.relname, policy.polname, policy.polcmd, policy.polroles,
                   pg_get_expr(policy.polqual, policy.polrelid),
                   pg_get_expr(policy.polwithcheck, policy.polrelid)
            FROM pg_policy AS policy
            JOIN pg_class AS relation ON relation.oid = policy.polrelid
            JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
            WHERE namespace.nspname = 'public'
              AND policy.polname IN (
                    'tenant_select', 'tenant_insert', 'tenant_update', 'owner_delete'
              )
            ORDER BY relation.relname, policy.polname
            """
        ).fetchall()
        audit_definer_policy_rows = connection.execute(
            """
            SELECT relation.relname, policy.polname, policy.polcmd,
                   ARRAY(
                       SELECT role.rolname
                       FROM pg_roles AS role
                       WHERE role.oid = ANY(policy.polroles)
                       ORDER BY role.rolname
                   ),
                   pg_get_expr(policy.polqual, policy.polrelid),
                   pg_get_expr(policy.polwithcheck, policy.polrelid)
            FROM pg_policy AS policy
            JOIN pg_class AS relation ON relation.oid = policy.polrelid
            JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
            WHERE namespace.nspname = 'public'
              AND policy.polname = 'audit_definer_tenant_select'
            """
        ).fetchall()
        takeover_lineage_boundary = connection.execute(
            """
            SELECT owner.rolname, procedure.prosecdef, procedure.proconfig,
                   has_function_privilege('public', procedure.oid, 'EXECUTE'),
                   has_function_privilege('campus_ai_runtime', procedure.oid, 'EXECUTE'),
                   has_function_privilege(
                       'campus_ai_takeover_definer', procedure.oid, 'EXECUTE'
                   ),
                   has_function_privilege(
                       'campus_ai_audit_definer', procedure.oid, 'EXECUTE'
                   ),
                   has_schema_privilege(
                       'campus_ai_runtime', 'app_private', 'USAGE'
                   ),
                   has_schema_privilege(
                       'campus_ai_takeover_definer', 'app_private', 'USAGE'
                   ),
                   has_schema_privilege(
                       'campus_ai_takeover_definer', 'app_private', 'CREATE'
                   ),
                   pg_get_functiondef(procedure.oid)
            FROM pg_proc AS procedure
            JOIN pg_namespace AS namespace ON namespace.oid = procedure.pronamespace
            JOIN pg_roles AS owner ON owner.oid = procedure.proowner
            WHERE namespace.nspname = 'app_private'
              AND procedure.proname = 'master_data_import_takeover_chain_is_valid'
            """
        ).fetchone()
        audit_lineage_boundary = connection.execute(
            """
            SELECT owner.rolname, procedure.prosecdef, procedure.proconfig,
                   has_function_privilege('public', procedure.oid, 'EXECUTE'),
                   has_function_privilege('campus_ai_runtime', procedure.oid, 'EXECUTE'),
                   has_function_privilege(
                       'campus_ai_takeover_definer', procedure.oid, 'EXECUTE'
                   ),
                   has_function_privilege(
                       'campus_ai_audit_definer', procedure.oid, 'EXECUTE'
                   ),
                   pg_get_functiondef(procedure.oid)
            FROM pg_proc AS procedure
            JOIN pg_namespace AS namespace ON namespace.oid = procedure.pronamespace
            JOIN pg_roles AS owner ON owner.oid = procedure.proowner
            WHERE namespace.nspname = 'app_private'
              AND procedure.proname =
                  'master_data_import_takeover_chain_is_valid_for_audit'
            """
        ).fetchone()

    assert {row[0] for row in table_rows} == {
        "schools",
        "school_people",
        "staff_assignments",
        "responsibility_groups",
        "command_idempotency_records",
        "outbox_messages",
        "repair_scope_bindings",
        "repair_tickets",
    }
    assert all(row[1] is True and row[2] is True for row in table_rows)
    assert {row[0] for row in function_rows} == listed_functions
    assert all(
        row[1] == "campus_ai_context_definer"
        for row in function_rows
        if row[0] in context_functions
    )
    assert all(
        row[1] == "campus_ai_audit_definer" for row in function_rows if row[0] in audit_functions
    )
    assert all(
        row[1] == "campus_ai_outbox_probe_definer"
        for row in function_rows
        if row[0] in recovery_functions
    )
    assert all(
        row[1] == "campus_ai_takeover_definer"
        for row in function_rows
        if row[0] in takeover_functions
    )
    assert all(row[2] is True for row in function_rows)
    assert all(
        row[3] == ["search_path=pg_catalog"] for row in function_rows if row[0] in context_functions
    )
    assert all(row[3] == ["search_path=pg_catalog"] for row in function_rows)
    assert all(row[4] is False for row in function_rows)
    assert function_acl == [
        ("append_business_audit", "campus_ai_audit_definer", "EXECUTE"),
        ("append_business_audit", "campus_ai_db_owner", "EXECUTE"),
        ("append_business_audit", "campus_ai_runtime", "EXECUTE"),
        ("append_legacy_audit", "campus_ai_audit_definer", "EXECUTE"),
        ("append_legacy_audit", "campus_ai_db_owner", "EXECUTE"),
        ("append_legacy_audit", "campus_ai_runtime", "EXECUTE"),
        ("append_master_data_import_audit", "campus_ai_audit_definer", "EXECUTE"),
        ("append_master_data_import_audit", "campus_ai_db_owner", "EXECUTE"),
        ("append_master_data_import_audit", "campus_ai_runtime", "EXECUTE"),
        ("append_master_data_import_audit", "campus_ai_takeover_definer", "EXECUTE"),
        (
            "assert_master_data_import_takeover_integrity",
            "campus_ai_takeover_definer",
            "EXECUTE",
        ),
        ("bind_execution_context", "campus_ai_context_definer", "EXECUTE"),
        ("bind_execution_context", "campus_ai_db_owner", "EXECUTE"),
        ("bind_execution_context", "campus_ai_runtime", "EXECUTE"),
        ("bind_execution_context", "campus_ai_school_fact_writer", "EXECUTE"),
        ("bind_execution_context_rev07", "campus_ai_context_definer", "EXECUTE"),
        ("bind_root_reactivation_context", "campus_ai_context_definer", "EXECUTE"),
        ("bind_root_reactivation_context", "campus_ai_db_owner", "EXECUTE"),
        ("bind_root_reactivation_context", "campus_ai_runtime", "EXECUTE"),
        ("bind_root_reactivation_context", "campus_ai_school_fact_writer", "EXECUTE"),
        ("bind_school_reactivation_context", "campus_ai_context_definer", "EXECUTE"),
        ("bind_school_reactivation_context", "campus_ai_db_owner", "EXECUTE"),
        ("bind_school_reactivation_context", "campus_ai_runtime", "EXECUTE"),
        ("bind_school_reactivation_context", "campus_ai_school_fact_writer", "EXECUTE"),
        ("context_signing_key_fingerprint", "campus_ai_context_definer", "EXECUTE"),
        ("context_signing_key_fingerprint", "campus_ai_runtime", "EXECUTE"),
        ("list_execution_context_options", "campus_ai_context_definer", "EXECUTE"),
        ("list_execution_context_options", "campus_ai_runtime", "EXECUTE"),
        (
            "list_execution_context_options_rev07",
            "campus_ai_context_definer",
            "EXECUTE",
        ),
        ("list_school_reactivation_candidates", "campus_ai_context_definer", "EXECUTE"),
        ("list_school_reactivation_candidates", "campus_ai_db_owner", "EXECUTE"),
        ("list_school_reactivation_candidates", "campus_ai_runtime", "EXECUTE"),
        (
            "lock_master_data_import_takeover_assignment",
            "campus_ai_takeover_definer",
            "EXECUTE",
        ),
        (
            "lock_master_data_import_takeover_source",
            "campus_ai_takeover_definer",
            "EXECUTE",
        ),
        (
            "read_master_data_import_attempt_lineage",
            "campus_ai_runtime",
            "EXECUTE",
        ),
        (
            "read_master_data_import_attempt_lineage",
            "campus_ai_takeover_definer",
            "EXECUTE",
        ),
        (
            "read_master_data_import_succeeded_attempt",
            "campus_ai_runtime",
            "EXECUTE",
        ),
        (
            "read_master_data_import_succeeded_attempt",
            "campus_ai_takeover_definer",
            "EXECUTE",
        ),
        ("require_root_reactivation_context", "campus_ai_context_definer", "EXECUTE"),
        ("require_root_reactivation_context", "campus_ai_db_owner", "EXECUTE"),
        ("require_root_reactivation_context", "campus_ai_runtime", "EXECUTE"),
        ("require_root_reactivation_context", "campus_ai_school_fact_writer", "EXECUTE"),
        ("require_school_context", "campus_ai_audit_definer", "EXECUTE"),
        ("require_school_context", "campus_ai_context_definer", "EXECUTE"),
        ("require_school_context", "campus_ai_db_owner", "EXECUTE"),
        ("require_school_context", "campus_ai_outbox_probe_definer", "EXECUTE"),
        ("require_school_context", "campus_ai_runtime", "EXECUTE"),
        ("require_school_context", "campus_ai_school_fact_writer", "EXECUTE"),
        ("require_school_context", "campus_ai_takeover_definer", "EXECUTE"),
        ("require_school_reactivation_context", "campus_ai_context_definer", "EXECUTE"),
        ("require_school_reactivation_context", "campus_ai_db_owner", "EXECUTE"),
        ("require_school_reactivation_context", "campus_ai_runtime", "EXECUTE"),
        ("require_school_reactivation_context", "campus_ai_school_fact_writer", "EXECUTE"),
        ("school_fact_content_version_terminal", "campus_ai_context_definer", "EXECUTE"),
        ("school_fact_content_version_terminal", "campus_ai_db_owner", "EXECUTE"),
        ("school_fact_content_version_terminal", "campus_ai_runtime", "EXECUTE"),
        ("school_fact_content_version_terminal", "campus_ai_school_fact_writer", "EXECUTE"),
        (
            "school_fact_location_history_interval_is_resolvable",
            "campus_ai_context_definer",
            "EXECUTE",
        ),
        (
            "school_fact_location_history_interval_is_resolvable",
            "campus_ai_db_owner",
            "EXECUTE",
        ),
        (
            "school_fact_location_history_interval_is_resolvable",
            "campus_ai_runtime",
            "EXECUTE",
        ),
        (
            "school_fact_location_history_interval_is_resolvable",
            "campus_ai_school_fact_writer",
            "EXECUTE",
        ),
        (
            "school_fact_location_history_is_resolvable",
            "campus_ai_context_definer",
            "EXECUTE",
        ),
        (
            "school_fact_location_history_is_resolvable",
            "campus_ai_db_owner",
            "EXECUTE",
        ),
        (
            "school_fact_location_history_is_resolvable",
            "campus_ai_runtime",
            "EXECUTE",
        ),
        (
            "school_fact_location_history_is_resolvable",
            "campus_ai_school_fact_reader",
            "EXECUTE",
        ),
        (
            "school_fact_location_history_is_resolvable",
            "campus_ai_school_fact_writer",
            "EXECUTE",
        ),
        ("school_fact_root_is_effective_at", "campus_ai_context_definer", "EXECUTE"),
        ("school_fact_root_is_effective_at", "campus_ai_db_owner", "EXECUTE"),
        ("school_fact_root_is_effective_at", "campus_ai_runtime", "EXECUTE"),
        ("school_fact_root_is_effective_at", "campus_ai_school_fact_writer", "EXECUTE"),
        ("school_fact_school_snapshot_as_of", "campus_ai_context_definer", "EXECUTE"),
        ("school_fact_school_snapshot_as_of", "campus_ai_db_owner", "EXECUTE"),
        ("school_fact_school_snapshot_as_of", "campus_ai_runtime", "EXECUTE"),
        ("school_reactivation_replay_result", "campus_ai_context_definer", "EXECUTE"),
        ("school_reactivation_replay_result", "campus_ai_db_owner", "EXECUTE"),
        ("school_reactivation_replay_result", "campus_ai_runtime", "EXECUTE"),
        ("school_reactivation_replay_result", "campus_ai_school_fact_writer", "EXECUTE"),
        (
            "supersede_master_data_import_takeover_source",
            "campus_ai_takeover_definer",
            "EXECUTE",
        ),
        (
            "validate_school_reactivation_candidate",
            "campus_ai_context_definer",
            "EXECUTE",
        ),
        ("validate_school_reactivation_candidate", "campus_ai_db_owner", "EXECUTE"),
        ("validate_school_reactivation_candidate", "campus_ai_runtime", "EXECUTE"),
        (
            "verify_staff_duty_binding_outbox",
            "campus_ai_outbox_probe_definer",
            "EXECUTE",
        ),
        ("verify_staff_duty_binding_outbox", "campus_ai_runtime", "EXECUTE"),
    ]
    assert takeover_lineage_boundary is not None
    assert takeover_lineage_boundary[:-1] == (
        "campus_ai_takeover_definer",
        True,
        ["search_path=pg_catalog"],
        False,
        False,
        True,
        False,
        False,
        True,
        False,
    )
    lineage_definition = str(takeover_lineage_boundary[-1]).lower()
    forbidden_lineage_fragments = ("%rowtype", "takeover.*", "record.*")
    assert all(fragment not in lineage_definition for fragment in forbidden_lineage_fragments)
    assert audit_lineage_boundary is not None
    assert audit_lineage_boundary[:-1] == (
        "campus_ai_takeover_definer",
        True,
        ["search_path=pg_catalog"],
        False,
        False,
        True,
        True,
    )
    assert "app_private.master_data_import_takeover_chain_is_valid" in str(
        audit_lineage_boundary[-1]
    )
    legacy_policy_tables = {row[0] for row in policy_rows}
    assert legacy_policy_tables.isdisjoint(
        {
            "repair_responsibility_grants",
            "repair_scope_bindings",
            "repair_tickets",
            "responsibility_group_memberships",
            "responsibility_group_versions",
            "responsibility_groups",
            "responsibility_types",
        }
    )
    legacy_tenant_select_tables = {row[0] for row in policy_rows if row[1] == "tenant_select"}
    assert legacy_tenant_select_tables.isdisjoint(
        {
            "school_people",
            "organization_units",
            "organization_unit_versions",
            "staff_assignments",
            "staff_assignment_versions",
            "campus_locations",
            "campus_location_versions",
            "official_contact_points",
            "account_person_binding_versions",
            "school_versions",
        }
    )
    assert audit_definer_policy_rows == [
        (
            "command_idempotency_records",
            "audit_definer_tenant_select",
            "r",
            ["campus_ai_audit_definer"],
            "(school_id = ( SELECT app.require_school_context() AS require_school_context))",
            None,
        )
    ]
    assert {(row[1], row[2]) for row in policy_rows} == {
        ("tenant_select", "r"),
        ("tenant_insert", "a"),
        ("tenant_update", "w"),
        ("owner_delete", "d"),
    }
    assert all(row[3] == [0] for row in policy_rows)
    for _, policy_name, _, _, using_expression, check_expression in policy_rows:
        if policy_name == "tenant_select":
            assert using_expression is not None
            assert "require_school_context" in using_expression
            assert check_expression is None
        elif policy_name == "tenant_insert":
            assert using_expression is None
            assert check_expression is not None
            assert "require_school_context" in check_expression
            assert "context_purpose" in check_expression
        elif policy_name == "tenant_update":
            assert using_expression is not None and check_expression is not None
            assert "context_purpose" in using_expression
            assert "context_purpose" in check_expression
        else:
            assert using_expression is not None
            assert "campus_ai_db_owner" in using_expression
            assert "require_school_context" not in using_expression
            assert check_expression is None


@pytest.mark.integration
@pytest.mark.destructive_migration
def test_task5_audit_route_upgrades_existing_revision_10_and_round_trips(
    isolated_revision19_schema: PostgresRoles,
    isolated_rls_facts: _RlsFacts,
) -> None:
    """既有 10 数据库升级后接管审计改走持久事实，降级可恢复原定义。"""

    current_master_data_schema = isolated_revision19_schema
    rls_facts = isolated_rls_facts

    environment = _migration_environment(current_master_data_schema)
    signature = "integer,uuid,uuid,uuid,bigint,text,text,text,uuid,uuid,uuid,jsonb,jsonb,uuid,text"
    upgrade: subprocess.CompletedProcess[str] | None = None
    try:
        _reset_import_schema_to_revision_10(current_master_data_schema, environment)
        with current_master_data_schema.admin_connection() as connection:
            revision_10 = connection.execute(
                """
                SELECT version_num,
                       pg_get_functiondef(
                           ('app.append_master_data_import_audit(' || %s || ')')::regprocedure
                       ),
                       to_regprocedure(
                           'app_private.append_master_data_import_audit_xid(' || %s || ')'
                       ),
                       has_column_privilege(
                           'campus_ai_audit_definer',
                           'public.master_data_import_takeovers',
                           'xmin',
                           'SELECT'
                       )
                FROM public.alembic_version
                """,
                (signature, signature),
            ).fetchone()
            baseline_boundary = connection.execute(
                """
                SELECT pg_get_functiondef(
                           'app.supersede_master_data_import_takeover_source('
                           'uuid,uuid,uuid,text,bigint)'::regprocedure
                       ),
                       pg_get_functiondef(
                           'app_private.populate_master_data_import_takeover_manifest()'::regprocedure
                       ),
                       ARRAY(
                           SELECT trigger_row.tgname
                           FROM pg_trigger AS trigger_row
                           WHERE trigger_row.tgname LIKE
                               'enforce_master_data_import_takeover_%%'
                              OR trigger_row.tgname =
                               'enforce_master_data_import_attempt_transition'
                           ORDER BY trigger_row.tgname
                       ),
                       ARRAY(
                           SELECT has_column_privilege(
                               'campus_ai_takeover_definer',
                               'public.' || table_name,
                               'xmin',
                               'SELECT'
                           )
                           FROM unnest(ARRAY[
                               'command_idempotency_records',
                               'master_data_import_batches',
                               'master_data_import_takeovers',
                               'master_data_import_units'
                           ]) AS table_name
                           ORDER BY table_name
                       )
                """
            ).fetchone()
        assert revision_10 is not None
        assert revision_10[0] == "20260809_10"
        assert "takeover.xmin" in revision_10[1].lower()
        assert revision_10[2] is None
        assert revision_10[3] is True
        assert baseline_boundary is not None
        assert "pg_current_xact_id" in baseline_boundary[0].lower()
        assert "pg_xact_status" in baseline_boundary[1].lower()
        assert baseline_boundary[2] == []
        assert baseline_boundary[3] == [True, True, True, True]
    finally:
        upgrade = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "20260809_11"],
            cwd=_API_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

    assert upgrade is not None
    assert upgrade.returncode == 0, upgrade.stdout + upgrade.stderr
    with current_master_data_schema.admin_connection() as connection:
        revision_11 = connection.execute(
            """
            SELECT version_num,
                   pg_get_functiondef(
                       ('app.append_master_data_import_audit(' || %s || ')')::regprocedure
                   ),
                   owner_role.rolname,
                   has_function_privilege('campus_ai_runtime', private_function.oid, 'EXECUTE'),
                   has_function_privilege(
                       'campus_ai_takeover_definer', private_function.oid, 'EXECUTE'
                   ),
                   has_function_privilege('public', private_function.oid, 'EXECUTE'),
                   has_column_privilege(
                       'campus_ai_audit_definer',
                       'public.master_data_import_takeovers',
                       'xmin',
                       'SELECT'
                   )
            FROM public.alembic_version
            JOIN pg_proc AS private_function
              ON private_function.oid = (
                  'app_private.append_master_data_import_audit_xid(' || %s || ')'
              )::regprocedure
            JOIN pg_roles AS owner_role ON owner_role.oid = private_function.proowner
            """,
            (signature, signature),
        ).fetchone()
        current_boundary = connection.execute(
            """
            SELECT pg_get_functiondef(
                       'app.supersede_master_data_import_takeover_source('
                       'uuid,uuid,uuid,text,bigint)'::regprocedure
                   ),
                   pg_get_functiondef(
                       'app_private.populate_master_data_import_takeover_manifest()'::regprocedure
                   ),
                   ARRAY(
                       SELECT trigger_row.tgname
                       FROM pg_trigger AS trigger_row
                       WHERE trigger_row.tgname LIKE
                           'enforce_master_data_import_takeover_%%'
                          OR trigger_row.tgname =
                           'enforce_master_data_import_attempt_transition'
                       ORDER BY trigger_row.tgname
                   ),
                   ARRAY(
                       SELECT has_column_privilege(
                           'campus_ai_takeover_definer',
                           'public.' || table_name,
                           'xmin',
                           'SELECT'
                       )
                       FROM unnest(ARRAY[
                           'command_idempotency_records',
                           'master_data_import_batches',
                           'master_data_import_takeovers',
                           'master_data_import_units'
                       ]) AS table_name
                       ORDER BY table_name
                   )
            """
        ).fetchone()
    assert revision_11 is not None
    assert revision_11[0] == "20260809_11"
    wrapper_definition = revision_11[1].lower()
    assert all(
        fragment not in wrapper_definition
        for fragment in ("xmin", "pg_current_xact_id", "pg_xact_status", "xid8")
    )
    assert revision_11[2:] == (
        "campus_ai_audit_definer",
        False,
        False,
        False,
        False,
    )
    assert current_boundary is not None
    current_boundary_definitions = (current_boundary[0] + current_boundary[1]).lower()
    assert all(
        fragment not in current_boundary_definitions
        for fragment in ("xmin", "pg_current_xact_id", "pg_xact_status", "xid8")
    )
    assert current_boundary[2] == [
        "enforce_master_data_import_attempt_transition",
        "enforce_master_data_import_takeover_batch_integrity",
        "enforce_master_data_import_takeover_command_integrity",
        "enforce_master_data_import_takeover_insert_integrity",
    ]
    assert current_boundary[3] == [False, False, False, False]

    round_trip_downgrade = subprocess.run(
        [sys.executable, "-m", "alembic", "downgrade", "20260809_10"],
        cwd=_API_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert round_trip_downgrade.returncode == 0, (
        round_trip_downgrade.stdout + round_trip_downgrade.stderr
    )
    with current_master_data_schema.admin_connection() as connection:
        restored_revision_10 = connection.execute(
            """
                SELECT version_num,
                       pg_get_functiondef(
                           ('app.append_master_data_import_audit(' || %s || ')')::regprocedure
                       ),
                       to_regprocedure(
                           'app_private.append_master_data_import_audit_xid(' || %s || ')'
                       ),
                       has_column_privilege(
                           'campus_ai_audit_definer',
                           'public.master_data_import_takeovers',
                           'xmin',
                           'SELECT'
                       )
                FROM public.alembic_version
            """,
            (signature, signature),
        ).fetchone()
        restored_boundary = connection.execute(
            """
                SELECT pg_get_functiondef(
                           'app.supersede_master_data_import_takeover_source('
                           'uuid,uuid,uuid,text,bigint)'::regprocedure
                       ),
                       pg_get_functiondef(
                           'app_private.populate_master_data_import_takeover_manifest()'::regprocedure
                       ),
                       ARRAY(
                           SELECT trigger_row.tgname
                           FROM pg_trigger AS trigger_row
                           WHERE trigger_row.tgname LIKE
                               'enforce_master_data_import_takeover_%%'
                              OR trigger_row.tgname =
                               'enforce_master_data_import_attempt_transition'
                           ORDER BY trigger_row.tgname
                       ),
                       ARRAY(
                           SELECT has_column_privilege(
                               'campus_ai_takeover_definer',
                               'public.' || table_name,
                               'xmin',
                               'SELECT'
                           )
                           FROM unnest(ARRAY[
                               'command_idempotency_records',
                               'master_data_import_batches',
                               'master_data_import_takeovers',
                               'master_data_import_units'
                           ]) AS table_name
                           ORDER BY table_name
                       )
                """
        ).fetchone()
    assert restored_revision_10 is not None
    assert restored_revision_10[0] == "20260809_10"
    assert "takeover.xmin" in restored_revision_10[1].lower()
    assert restored_revision_10[2] is None
    assert restored_revision_10[3] is True
    assert restored_boundary is not None
    assert "pg_current_xact_id" in restored_boundary[0].lower()
    assert "pg_xact_status" in restored_boundary[1].lower()
    assert restored_boundary[2] == []
    assert restored_boundary[3] == [True, True, True, True]

    round_trip_upgrade = _exercise_revision_10_takeover_history_preflight(
        current_master_data_schema,
        rls_facts,
        environment,
    )
    assert round_trip_upgrade.returncode == 0, round_trip_upgrade.stdout + round_trip_upgrade.stderr


@pytest.mark.integration
@pytest.mark.destructive_migration
def test_task6_revision_11_single_takeover_upgrades_and_round_trips_exactly(
    isolated_revision19_schema: PostgresRoles,
    isolated_rls_facts: _RlsFacts,
) -> None:
    """A complete revision-11 takeover survives 11->12 and 12->11->12 unchanged."""

    current_master_data_schema = isolated_revision19_schema
    rls_facts = isolated_rls_facts

    environment = _migration_environment(current_master_data_schema)
    try:
        _reset_import_schema_to_revision_10(current_master_data_schema, environment)
        _seed_revision_10_takeover_history(
            current_master_data_schema,
            rls_facts,
            state="complete_takeover",
        )
        to_revision_11 = _run_alembic(environment, "upgrade", "20260809_11")
        assert to_revision_11.returncode == 0, to_revision_11.stdout + to_revision_11.stderr
        revision_11_snapshot = _revision_10_school_snapshot(
            current_master_data_schema,
            rls_facts.school_a,
        )
        revision_11_functions = _revision_11_takeover_function_snapshot(current_master_data_schema)

        first_upgrade = _run_alembic(environment, "upgrade", "20260809_12")
        assert first_upgrade.returncode == 0, first_upgrade.stdout + first_upgrade.stderr
        assert (
            _revision_10_school_snapshot(current_master_data_schema, rls_facts.school_a)
            == revision_11_snapshot
        )
        with current_master_data_schema.admin_connection() as connection:
            revision_12 = connection.execute(
                "SELECT version_num, "
                "to_regprocedure('app.read_master_data_import_attempt_lineage(uuid,uuid)'), "
                "to_regprocedure('app.read_master_data_import_succeeded_attempt(uuid,uuid,uuid)'), "
                "EXISTS (SELECT 1 FROM pg_attribute "
                "WHERE attrelid = 'public.master_data_import_command_outcomes'::regclass "
                "AND attname = 'unit_outcome_snapshots' AND NOT attisdropped), "
                "to_regprocedure("
                "'app_private.populate_master_data_import_unit_outcome_snapshot()'), "
                "to_regprocedure('app_private.reject_master_data_import_outcome_mutation()') "
                "FROM public.alembic_version"
            ).fetchone()
        assert revision_12 is not None
        assert revision_12[0] == "20260809_12"
        assert revision_12[1] is not None
        assert revision_12[2] is not None
        assert revision_12[3:] == (
            True,
            "app_private.populate_master_data_import_unit_outcome_snapshot()",
            "app_private.reject_master_data_import_outcome_mutation()",
        )

        downgrade = _run_alembic(environment, "downgrade", "20260809_11")
        assert downgrade.returncode == 0, downgrade.stdout + downgrade.stderr
        assert (
            _revision_10_school_snapshot(current_master_data_schema, rls_facts.school_a)
            == revision_11_snapshot
        )
        assert (
            _revision_11_takeover_function_snapshot(current_master_data_schema)
            == revision_11_functions
        )
        with current_master_data_schema.admin_connection() as connection:
            restored_revision_11 = connection.execute(
                "SELECT version_num, "
                "to_regprocedure('app.read_master_data_import_attempt_lineage(uuid,uuid)'), "
                "to_regprocedure('app.read_master_data_import_succeeded_attempt(uuid,uuid,uuid)'), "
                "EXISTS (SELECT 1 FROM pg_attribute "
                "WHERE attrelid = 'public.master_data_import_command_outcomes'::regclass "
                "AND attname = 'unit_outcome_snapshots' AND NOT attisdropped), "
                "to_regprocedure("
                "'app_private.populate_master_data_import_unit_outcome_snapshot()'), "
                "to_regprocedure('app_private.reject_master_data_import_outcome_mutation()') "
                "FROM public.alembic_version"
            ).fetchone()
        assert restored_revision_11 == ("20260809_11", None, None, False, None, None)
        with current_master_data_schema.admin_connection() as connection:
            revision_11_reader_acl = connection.execute(
                """
                SELECT to_regprocedure(
                           'app.read_master_data_import_succeeded_attempt(uuid,uuid,uuid)'
                       ),
                       count(*)
                FROM information_schema.column_privileges
                WHERE table_schema = 'public'
                  AND table_name = 'master_data_import_command_outcomes'
                  AND grantee = 'campus_ai_takeover_definer'
                """
            ).fetchone()
        assert revision_11_reader_acl == (None, 0)

        second_upgrade = _run_alembic(environment, "upgrade", "20260809_12")
        assert second_upgrade.returncode == 0, second_upgrade.stdout + second_upgrade.stderr
        assert (
            _revision_10_school_snapshot(current_master_data_schema, rls_facts.school_a)
            == revision_11_snapshot
        )
        with current_master_data_schema.admin_connection() as connection:
            restored_revision_12_reader_acl = connection.execute(
                """
                SELECT to_regprocedure(
                           'app.read_master_data_import_succeeded_attempt(uuid,uuid,uuid)'
                       ) IS NOT NULL,
                       count(*)
                FROM information_schema.column_privileges
                WHERE table_schema = 'public'
                  AND table_name = 'master_data_import_command_outcomes'
                  AND grantee = 'campus_ai_takeover_definer'
                """
            ).fetchone()
        assert restored_revision_12_reader_acl == (True, 10)
    finally:
        current = _run_alembic(environment, "current")
        if "20260809_12" not in current.stdout:
            _reset_import_schema_to_revision_10(current_master_data_schema, environment)
            restore = _run_alembic(environment, "upgrade", "20260809_12")
            assert restore.returncode == 0, restore.stdout + restore.stderr


@pytest.mark.integration
@pytest.mark.destructive_migration
def test_task6_revision_12_rejects_downgrade_that_would_drop_unit_outcomes(
    isolated_revision19_schema: PostgresRoles,
    isolated_rls_facts: _RlsFacts,
) -> None:
    """Even an empty DB-generated ordinary snapshot is revision-12-only evidence."""

    current_master_data_schema = isolated_revision19_schema
    rls_facts = isolated_rls_facts

    environment = _migration_environment(current_master_data_schema)
    try:
        _reset_import_schema_to_revision_10(current_master_data_schema, environment)
        identifiers = _seed_revision_10_takeover_history(
            current_master_data_schema,
            rls_facts,
            state="complete_takeover",
        )
        upgrade = _run_alembic(environment, "upgrade", "20260809_12")
        assert upgrade.returncode == 0, upgrade.stdout + upgrade.stderr
        outcome_id = uuid4()
        with current_master_data_schema.admin_connection() as connection:
            connection.execute("SET session_replication_role = replica")
            connection.execute(
                "DELETE FROM public.master_data_import_takeovers WHERE id = %s",
                (identifiers[5],),
            )
            connection.execute(
                "UPDATE public.command_idempotency_records "
                "SET action_code = 'master_data.import.ordinary_resume' WHERE id = %s",
                (identifiers[4],),
            )
            connection.execute(
                "UPDATE public.master_data_import_batches "
                "SET active_action_code = 'master_data.import.ordinary_resume' WHERE id = %s",
                (identifiers[1],),
            )
            connection.execute("SET session_replication_role = origin")
            connection.execute("SET ROLE campus_ai_db_owner")
            connection.execute(
                """
                INSERT INTO public.master_data_import_command_outcomes (
                    id, school_id, idempotency_record_id, batch_id, action_code,
                    status, batch_version, succeeded_units, failed_units, executed_units
                ) VALUES (
                    %s, %s, %s, %s, 'master_data.import.ordinary_resume',
                    'applied', 2, '{}'::integer[], '{}'::integer[], '{}'::integer[]
                )
                """,
                (outcome_id, rls_facts.school_a, identifiers[4], identifiers[1]),
            )
            snapshot = connection.execute(
                "SELECT unit_outcome_snapshots "
                "FROM public.master_data_import_command_outcomes WHERE id = %s",
                (outcome_id,),
            ).fetchone()
            connection.commit()
        assert snapshot == ([],)

        rejected = _run_alembic(environment, "downgrade", "20260809_11")
        combined_output = rejected.stdout + rejected.stderr
        assert rejected.returncode != 0
        assert (
            "revision 12 contains immutable unit outcomes not representable by revision 11"
            in combined_output
        )
        assert all(str(identifier) not in combined_output for identifier in identifiers)
        assert str(outcome_id) not in combined_output
        with current_master_data_schema.admin_connection() as connection:
            revision = connection.execute(
                "SELECT version_num FROM public.alembic_version"
            ).fetchone()
            snapshot_after = connection.execute(
                "SELECT unit_outcome_snapshots "
                "FROM public.master_data_import_command_outcomes WHERE id = %s",
                (outcome_id,),
            ).fetchone()
        assert revision == ("20260809_12",)
        assert snapshot_after == snapshot
    finally:
        _reset_import_schema_to_revision_10(current_master_data_schema, environment)
        restore = _run_alembic(environment, "upgrade", "head")
        assert restore.returncode == 0, restore.stdout + restore.stderr


@pytest.mark.integration
@pytest.mark.destructive_migration
def test_task6_revision_12_rejects_downgrade_with_single_takeover_terminal_facts(
    isolated_revision19_schema: PostgresRoles,
    isolated_rls_facts: _RlsFacts,
) -> None:
    """单次接管一旦形成 Task 6 首次终态，revision 11 不得接收该数据。"""

    current_master_data_schema = isolated_revision19_schema
    rls_facts = isolated_rls_facts

    environment = _migration_environment(current_master_data_schema)
    try:
        _reset_import_schema_to_revision_10(current_master_data_schema, environment)
        identifiers = _seed_revision_10_takeover_history(
            current_master_data_schema,
            rls_facts,
            state="complete_takeover",
        )
        upgrade = _run_alembic(environment, "upgrade", "20260809_12")
        assert upgrade.returncode == 0, upgrade.stdout + upgrade.stderr
        outcome_id = _finish_revision_12_single_takeover(
            current_master_data_schema,
            rls_facts,
            batch_id=identifiers[1],
            new_record_id=identifiers[4],
        )
        before = _revision_10_school_snapshot(
            current_master_data_schema,
            rls_facts.school_a,
        )

        rejected = _run_alembic(environment, "downgrade", "20260809_11")
        combined_output = rejected.stdout + rejected.stderr
        assert rejected.returncode != 0
        assert (
            "revision 12 contains terminal takeover facts not representable by revision 11"
            in combined_output
        )
        assert all(
            str(identifier) not in combined_output for identifier in (*identifiers, outcome_id)
        )
        assert all(
            secret not in combined_output
            for secret in current_master_data_schema.protected_test_secrets
        )
        with current_master_data_schema.admin_connection() as connection:
            revision = connection.execute(
                "SELECT version_num FROM public.alembic_version"
            ).fetchone()
        assert revision == ("20260809_12",)
        assert (
            _revision_10_school_snapshot(current_master_data_schema, rls_facts.school_a) == before
        )
    finally:
        _reset_import_schema_to_revision_10(current_master_data_schema, environment)
        restore = _run_alembic(environment, "upgrade", "head")
        assert restore.returncode == 0, restore.stdout + restore.stderr


@pytest.mark.integration
@pytest.mark.destructive_migration
def test_task6_revision_12_rejects_partial_revision_11_takeover_without_changes(
    isolated_revision19_schema: PostgresRoles,
    isolated_rls_facts: _RlsFacts,
) -> None:
    """Revision 12 fails closed when a revision-11 provenance edge is incomplete."""

    current_master_data_schema = isolated_revision19_schema
    rls_facts = isolated_rls_facts

    environment = _migration_environment(current_master_data_schema)
    try:
        _reset_import_schema_to_revision_10(current_master_data_schema, environment)
        identifiers = _seed_revision_10_takeover_history(
            current_master_data_schema,
            rls_facts,
            state="complete_takeover",
        )
        to_revision_11 = _run_alembic(environment, "upgrade", "20260809_11")
        assert to_revision_11.returncode == 0, to_revision_11.stdout + to_revision_11.stderr
        with current_master_data_schema.admin_connection() as connection:
            connection.execute("SET session_replication_role = replica")
            connection.execute(
                "DELETE FROM public.audit_logs "
                "WHERE school_id = %s AND event_type = 'master_data.import.taken_over'",
                (rls_facts.school_a,),
            )
            connection.execute("SET session_replication_role = origin")
            connection.commit()
        partial_snapshot = _revision_10_school_snapshot(
            current_master_data_schema,
            rls_facts.school_a,
        )

        rejected = _run_alembic(environment, "upgrade", "20260809_12")
        combined_output = rejected.stdout + rejected.stderr
        assert rejected.returncode != 0
        assert "revision 11 contains partial takeover lineage" in combined_output
        assert all(str(identifier) not in combined_output for identifier in identifiers)
        assert all(
            secret not in combined_output
            for secret in current_master_data_schema.protected_test_secrets
        )
        with current_master_data_schema.admin_connection() as connection:
            revision = connection.execute(
                "SELECT version_num FROM public.alembic_version"
            ).fetchone()
        assert revision == ("20260809_11",)
        assert (
            _revision_10_school_snapshot(current_master_data_schema, rls_facts.school_a)
            == partial_snapshot
        )
    finally:
        _reset_import_schema_to_revision_10(current_master_data_schema, environment)
        restore = _run_alembic(environment, "upgrade", "head")
        assert restore.returncode == 0, restore.stdout + restore.stderr


@pytest.mark.integration
@pytest.mark.destructive_migration
def test_task6_revision_12_rejects_downgrade_with_second_takeover_facts(
    isolated_revision19_schema: PostgresRoles,
    isolated_rls_facts: _RlsFacts,
) -> None:
    """A chain that revision 11 cannot express keeps revision 12 and all facts intact."""

    current_master_data_schema = isolated_revision19_schema
    rls_facts = isolated_rls_facts

    environment = _migration_environment(current_master_data_schema)
    try:
        _reset_import_schema_to_revision_10(current_master_data_schema, environment)
        identifiers = _seed_revision_10_takeover_history(
            current_master_data_schema,
            rls_facts,
            state="complete_takeover",
        )
        upgrade = _run_alembic(environment, "upgrade", "20260809_12")
        assert upgrade.returncode == 0, upgrade.stdout + upgrade.stderr
        second_identifiers = _seed_revision_12_second_takeover(
            current_master_data_schema,
            rls_facts,
            batch_id=identifiers[1],
            previous_record_id=identifiers[4],
        )
        before = _revision_10_school_snapshot(
            current_master_data_schema,
            rls_facts.school_a,
        )

        rejected = _run_alembic(environment, "downgrade", "20260809_11")
        combined_output = rejected.stdout + rejected.stderr
        assert rejected.returncode != 0
        assert (
            "revision 12 contains takeover lineage not representable by revision 11"
            in combined_output
        )
        assert all(
            str(identifier) not in combined_output
            for identifier in (*identifiers, *second_identifiers)
        )
        assert all(
            secret not in combined_output
            for secret in current_master_data_schema.protected_test_secrets
        )
        with current_master_data_schema.admin_connection() as connection:
            revision = connection.execute(
                "SELECT version_num FROM public.alembic_version"
            ).fetchone()
        assert revision == ("20260809_12",)
        after_rejected_downgrade = _revision_10_school_snapshot(
            current_master_data_schema, rls_facts.school_a
        )
        assert after_rejected_downgrade == before
    finally:
        with current_master_data_schema.admin_connection() as connection:
            connection.execute("SET session_replication_role = replica")
            connection.execute(
                "DELETE FROM public.audit_logs WHERE school_id = %s "
                "AND event_type LIKE 'master_data.import.%%'",
                (rls_facts.school_a,),
            )
            connection.execute(
                "DELETE FROM public.master_data_import_takeovers WHERE school_id = %s",
                (rls_facts.school_a,),
            )
            connection.execute(
                "DELETE FROM public.master_data_import_units WHERE school_id = %s",
                (rls_facts.school_a,),
            )
            connection.execute(
                "DELETE FROM public.master_data_import_command_outcomes WHERE school_id = %s",
                (rls_facts.school_a,),
            )
            connection.execute(
                "DELETE FROM public.master_data_import_batches WHERE school_id = %s",
                (rls_facts.school_a,),
            )
            connection.execute(
                "DELETE FROM public.command_idempotency_records "
                "WHERE school_id = %s AND action_code LIKE 'master_data.import.%%'",
                (rls_facts.school_a,),
            )
            connection.execute("SET session_replication_role = origin")
            connection.commit()
        _reset_import_schema_to_revision_10(current_master_data_schema, environment)
        restore = _run_alembic(environment, "upgrade", "head")
        assert restore.returncode == 0, restore.stdout + restore.stderr


@pytest.mark.integration
@pytest.mark.destructive_migration
def test_task5_rls_downgrade_revokes_definer_column_acl_and_upgrades_cleanly(
    isolated_revision19_schema: PostgresRoles,
) -> None:
    """07→06 不遗留 definer 读权，随后必须能恢复到同一 head。"""

    current_master_data_schema = isolated_revision19_schema

    environment = _migration_environment(current_master_data_schema)
    downgrade = subprocess.run(
        [sys.executable, "-m", "alembic", "downgrade", "20260805_06"],
        cwd=_API_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    upgrade: subprocess.CompletedProcess[str] | None = None
    try:
        assert downgrade.returncode == 0, downgrade.stdout + downgrade.stderr
        with current_master_data_schema.admin_connection() as connection:
            connection.execute("SET ROLE campus_ai_db_owner")
            connection.execute(
                "DELETE FROM public.command_idempotency_records "
                "WHERE action_code LIKE 'master_data.import.%'"
            )
            residual_acl = connection.execute(
                """
                SELECT table_name, column_name, privilege_type
                FROM information_schema.column_privileges
                WHERE table_schema = 'public'
                  AND grantee IN ('campus_ai_context_definer', 'campus_ai_audit_definer')
                ORDER BY table_name, column_name, privilege_type
                """
            ).fetchall()
            revision = connection.execute(
                "SELECT version_num FROM public.alembic_version"
            ).fetchone()
        assert residual_acl == []
        assert revision == ("20260805_06",)
    finally:
        upgrade = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=_API_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

    assert upgrade is not None
    assert upgrade.returncode == 0, upgrade.stdout + upgrade.stderr


@pytest.mark.integration
def test_role_membership_schema_and_runtime_table_acl_are_minimal(
    current_master_data_schema: PostgresRoles,
) -> None:
    """七角色、私有 schema、表所有权和特殊表权限均收敛到书面白名单。"""

    with current_master_data_schema.admin_connection() as connection:
        connection.execute(
            "GRANT SELECT (display_name) ON TABLE public.schools TO campus_ai_audit_definer"
        )
        connection.execute("GRANT USAGE ON SEQUENCE public.users_id_seq TO campus_ai_audit_definer")
        connection.commit()
    assert current_master_data_schema.rerun_bootstrap().returncode == 0
    assert current_master_data_schema.rerun_bootstrap().returncode == 0
    with current_master_data_schema.admin_connection() as connection:
        roles = connection.execute(
            """
            SELECT rolname, rolcanlogin, rolinherit, rolsuper, rolcreatedb,
                   rolcreaterole, rolreplication, rolbypassrls
            FROM pg_roles
            WHERE rolname IN ('campus_ai_db_owner', 'campus_ai_context_definer',
                              'campus_ai_audit_definer',
                              'campus_ai_outbox_probe_definer',
                              'campus_ai_takeover_definer',
                              'campus_ai_migration', 'campus_ai_runtime')
            ORDER BY rolname
            """
        ).fetchall()
        memberships = connection.execute(
            """
            SELECT parent.rolname, member.rolname,
                   membership.admin_option, membership.inherit_option,
                   membership.set_option
            FROM pg_auth_members AS membership
            JOIN pg_roles AS parent ON parent.oid = membership.roleid
            JOIN pg_roles AS member ON member.oid = membership.member
            WHERE parent.rolname IN ('campus_ai_db_owner', 'campus_ai_context_definer',
                                     'campus_ai_audit_definer',
                                     'campus_ai_outbox_probe_definer',
                                     'campus_ai_takeover_definer')
               OR member.rolname IN ('campus_ai_runtime', 'campus_ai_migration')
            ORDER BY parent.rolname, member.rolname
            """
        ).fetchall()
        acl = connection.execute(
            """
            SELECT
                has_schema_privilege('campus_ai_runtime', 'app', 'USAGE'),
                has_schema_privilege('campus_ai_runtime', 'app', 'CREATE'),
                has_schema_privilege('campus_ai_runtime', 'app_private', 'USAGE'),
                has_schema_privilege('campus_ai_context_definer', 'app', 'CREATE'),
                has_schema_privilege('campus_ai_audit_definer', 'app', 'CREATE'),
                has_schema_privilege('campus_ai_audit_definer', 'app_private', 'USAGE'),
                has_schema_privilege('campus_ai_audit_definer', 'app_private', 'CREATE'),
                has_schema_privilege('campus_ai_audit_definer', 'public', 'USAGE'),
                has_schema_privilege(
                    'campus_ai_outbox_probe_definer', 'app', 'CREATE'
                ),
                has_schema_privilege(
                    'campus_ai_outbox_probe_definer', 'app_private', 'USAGE'
                ),
                has_schema_privilege(
                    'campus_ai_outbox_probe_definer', 'public', 'USAGE'
                ),
                has_table_privilege(
                    'campus_ai_outbox_probe_definer', 'public.outbox_messages', 'SELECT'
                ),
                has_table_privilege(
                    'campus_ai_outbox_probe_definer', 'public.outbox_messages', 'INSERT'
                ),
                has_table_privilege(
                    'campus_ai_outbox_probe_definer', 'public.outbox_messages', 'UPDATE'
                ),
                has_table_privilege(
                    'campus_ai_outbox_probe_definer', 'public.outbox_messages', 'DELETE'
                ),
                has_table_privilege('campus_ai_runtime', 'public.audit_logs', 'SELECT'),
                has_table_privilege('campus_ai_runtime', 'public.audit_logs', 'INSERT'),
                has_table_privilege('campus_ai_runtime', 'public.audit_logs', 'UPDATE'),
                has_table_privilege('campus_ai_runtime', 'public.audit_logs', 'DELETE'),
                has_table_privilege('campus_ai_runtime', 'public.outbox_messages', 'SELECT'),
                has_table_privilege('campus_ai_runtime', 'public.outbox_messages', 'UPDATE'),
                has_table_privilege('campus_ai_runtime', 'public.outbox_messages', 'DELETE'),
                has_table_privilege(
                    'campus_ai_runtime', 'public.command_idempotency_records', 'SELECT'
                ),
                has_table_privilege(
                    'campus_ai_runtime', 'public.command_idempotency_records', 'INSERT'
                ),
                has_table_privilege(
                    'campus_ai_runtime', 'public.command_idempotency_records', 'UPDATE'
                ),
                has_table_privilege(
                    'campus_ai_runtime', 'public.command_idempotency_records', 'DELETE'
                ),
                has_table_privilege(
                    'campus_ai_runtime',
                    'public.master_data_import_command_outcomes', 'SELECT'
                ),
                has_table_privilege(
                    'campus_ai_runtime',
                    'public.master_data_import_command_outcomes', 'INSERT'
                ),
                has_table_privilege(
                    'campus_ai_runtime',
                    'public.master_data_import_command_outcomes', 'UPDATE'
                ),
                has_table_privilege(
                    'campus_ai_runtime',
                    'public.master_data_import_command_outcomes', 'DELETE'
                ),
                has_schema_privilege(
                    'campus_ai_takeover_definer', 'app_private', 'USAGE'
                ),
                has_schema_privilege(
                    'campus_ai_takeover_definer', 'app_private', 'CREATE'
                ),
                has_function_privilege(
                    'campus_ai_runtime',
                    'app.read_master_data_import_attempt_lineage(uuid,uuid)',
                    'EXECUTE'
                ),
                has_function_privilege(
                    'public',
                    'app.read_master_data_import_attempt_lineage(uuid,uuid)',
                    'EXECUTE'
                ),
                has_function_privilege(
                    'campus_ai_runtime',
                    'app_private.master_data_import_takeover_chain_is_valid('
                    'uuid,uuid,uuid,bigint)',
                    'EXECUTE'
                ),
                has_function_privilege(
                    'campus_ai_takeover_definer',
                    'app_private.master_data_import_takeover_chain_is_valid('
                    'uuid,uuid,uuid,bigint)',
                    'EXECUTE'
                ),
                has_function_privilege(
                    'campus_ai_runtime',
                    'app.read_master_data_import_succeeded_attempt(uuid,uuid,uuid)',
                    'EXECUTE'
                ),
                has_function_privilege(
                    'public',
                    'app.read_master_data_import_succeeded_attempt(uuid,uuid,uuid)',
                    'EXECUTE'
                )
            """
        ).fetchone()
        owned = connection.execute(
            """
            SELECT owner.rolname, count(*)
            FROM pg_class AS relation
            JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
            JOIN pg_roles AS owner ON owner.oid = relation.relowner
            WHERE namespace.nspname IN ('public', 'app', 'app_private')
              AND owner.rolname IN ('campus_ai_runtime', 'campus_ai_migration',
                                    'campus_ai_context_definer',
                                    'campus_ai_audit_definer',
                                    'campus_ai_outbox_probe_definer',
                                    'campus_ai_takeover_definer')
              AND relation.relkind IN ('r', 'p', 'S', 'v', 'm', 'f')
            GROUP BY owner.rolname
            """
        ).fetchall()
        special_column_acl = connection.execute(
            """
            SELECT table_name, column_name, privilege_type
            FROM information_schema.column_privileges
            WHERE table_schema = 'public'
              AND grantee = 'campus_ai_runtime'
              AND table_name IN ('audit_logs', 'outbox_messages')
            ORDER BY table_name, column_name, privilege_type
            """
        ).fetchall()
        idempotency_column_acl = connection.execute(
            """
            SELECT column_name, privilege_type
            FROM information_schema.column_privileges
            WHERE table_schema = 'public'
              AND table_name = 'command_idempotency_records'
              AND grantee = 'campus_ai_runtime'
            ORDER BY column_name, privilege_type
            """
        ).fetchall()
        definer_column_acl = connection.execute(
            """
            SELECT table_schema, table_name, column_name, privilege_type
            FROM information_schema.column_privileges
            WHERE grantee = 'campus_ai_context_definer'
              AND table_schema IN ('public', 'app_private')
            ORDER BY table_schema, table_name, column_name, privilege_type
            """
        ).fetchall()
        definer_default_acl = connection.execute(
            """
            SELECT owner.rolname, namespace.nspname, defaults.defaclobjtype,
                   COALESCE(grantee.rolname, 'PUBLIC'), acl.privilege_type
            FROM pg_default_acl AS defaults
            JOIN pg_roles AS owner ON owner.oid = defaults.defaclrole
            LEFT JOIN pg_namespace AS namespace ON namespace.oid = defaults.defaclnamespace
            CROSS JOIN LATERAL aclexplode(defaults.defaclacl) AS acl
            LEFT JOIN pg_roles AS grantee ON grantee.oid = acl.grantee
            WHERE owner.rolname IN (
                    'campus_ai_context_definer', 'campus_ai_audit_definer',
                    'campus_ai_outbox_probe_definer', 'campus_ai_takeover_definer'
                  )
              AND defaults.defaclnamespace = 0
              AND defaults.defaclobjtype = 'f'
            ORDER BY owner.rolname,
                     COALESCE(grantee.rolname, 'PUBLIC'), acl.privilege_type
            """
        ).fetchall()
        audit_definer_column_acl = connection.execute(
            """
            SELECT table_name, column_name, privilege_type
            FROM information_schema.column_privileges
            WHERE table_schema = 'public'
              AND grantee = 'campus_ai_audit_definer'
            ORDER BY table_name, column_name, privilege_type
            """
        ).fetchall()
        audit_definer_sequence_acl_row = connection.execute(
            """
            SELECT has_sequence_privilege(
                'campus_ai_audit_definer', 'public.audit_logs_id_seq', 'USAGE'
            )
            """
        ).fetchone()
        audit_definer_pollution_removed = connection.execute(
            """
            SELECT
                has_column_privilege(
                    'campus_ai_audit_definer', 'public.schools', 'display_name', 'SELECT'
                ),
                has_sequence_privilege(
                    'campus_ai_audit_definer', 'public.users_id_seq', 'USAGE'
                )
            """
        ).fetchone()
        audit_definer_xmin_acl = connection.execute(
            """
            SELECT table_name,
                   has_column_privilege(
                       'campus_ai_audit_definer',
                       'public.' || table_name,
                       'xmin',
                       'SELECT'
                   )
            FROM unnest(ARRAY[
                'command_idempotency_records',
                'master_data_import_batches',
                'master_data_import_command_outcomes',
                'master_data_import_takeovers',
                'master_data_import_units',
                'school_calendar_versions',
                'school_people',
                'staff_assignments'
            ]) AS table_name
            ORDER BY table_name
            """
        ).fetchall()
        outbox_probe_column_acl = connection.execute(
            """
            SELECT column_name, privilege_type
            FROM information_schema.column_privileges
            WHERE table_schema = 'public'
              AND table_name = 'outbox_messages'
              AND grantee = 'campus_ai_outbox_probe_definer'
            ORDER BY column_name, privilege_type
            """
        ).fetchall()
        audit_function_acl = connection.execute(
            """
            SELECT namespace.nspname, procedure.proname,
                   has_function_privilege(
                       'campus_ai_runtime', procedure.oid, 'EXECUTE'
                   ) AS runtime_execute,
                   EXISTS (
                       SELECT 1
                       FROM aclexplode(
                           COALESCE(
                               procedure.proacl,
                               acldefault('f', procedure.proowner)
                           )
                       ) AS acl
                       WHERE acl.grantee = 0
                         AND acl.privilege_type = 'EXECUTE'
                   ) AS public_execute
            FROM pg_proc AS procedure
            JOIN pg_namespace AS namespace ON namespace.oid = procedure.pronamespace
            WHERE (namespace.nspname = 'app' AND procedure.proname IN (
                       'append_business_audit', 'append_legacy_audit',
                       'append_master_data_import_audit'
                   ))
               OR (namespace.nspname = 'app_private'
                   AND procedure.proname IN (
                       'append_master_data_import_audit_xid',
                       'is_safe_business_audit_summary'
                   ))
            ORDER BY namespace.nspname, procedure.proname
            """
        ).fetchall()

    with psycopg.connect(
        **current_master_data_schema.admin_connection_parameters,
        autocommit=False,
    ) as probe_connection:
        try:
            probe_connection.execute("SET LOCAL ROLE campus_ai_db_owner")
            probe_connection.execute("GRANT CREATE ON SCHEMA app TO campus_ai_context_definer")
            probe_connection.execute("SET LOCAL ROLE campus_ai_context_definer")
            probe_connection.execute(
                "CREATE FUNCTION app.task5_context_default_acl_probe() "
                "RETURNS integer LANGUAGE sql AS 'SELECT 1'"
            )
            probe_connection.execute("SET LOCAL ROLE NONE")
            probe_connection.execute("SET LOCAL ROLE campus_ai_db_owner")
            probe_connection.execute(
                "GRANT CREATE ON SCHEMA app, app_private TO campus_ai_audit_definer"
            )
            probe_connection.execute("SET LOCAL ROLE campus_ai_audit_definer")
            probe_connection.execute(
                "CREATE FUNCTION app.task5_audit_default_acl_probe() "
                "RETURNS integer LANGUAGE sql AS 'SELECT 1'"
            )
            probe_connection.execute(
                "CREATE FUNCTION app_private.task5_audit_private_default_acl_probe() "
                "RETURNS integer LANGUAGE sql AS 'SELECT 1'"
            )
            probe_connection.execute("SET LOCAL ROLE NONE")
            probe_connection.execute("SET LOCAL ROLE campus_ai_db_owner")
            probe_connection.execute("GRANT CREATE ON SCHEMA app TO campus_ai_outbox_probe_definer")
            probe_connection.execute("SET LOCAL ROLE campus_ai_outbox_probe_definer")
            probe_connection.execute(
                "CREATE FUNCTION app.task7_outbox_probe_default_acl_probe() "
                "RETURNS integer LANGUAGE sql AS 'SELECT 1'"
            )
            probe_connection.execute("SET LOCAL ROLE NONE")
            probe_connection.execute("SET LOCAL ROLE campus_ai_db_owner")
            probe_connection.execute("GRANT CREATE ON SCHEMA app TO campus_ai_takeover_definer")
            probe_connection.execute("SET LOCAL ROLE campus_ai_takeover_definer")
            probe_connection.execute(
                "CREATE FUNCTION app.task5_takeover_default_acl_probe() "
                "RETURNS integer LANGUAGE sql AS 'SELECT 1'"
            )
            probe_connection.execute("SET LOCAL ROLE NONE")
            future_function_acl = probe_connection.execute(
                """
                SELECT namespace.nspname, procedure.proname, owner.rolname,
                       has_function_privilege('public', procedure.oid, 'EXECUTE')
                FROM pg_proc AS procedure
                JOIN pg_namespace AS namespace ON namespace.oid = procedure.pronamespace
                JOIN pg_roles AS owner ON owner.oid = procedure.proowner
                WHERE procedure.proname IN (
                        'task5_context_default_acl_probe',
                        'task5_audit_default_acl_probe',
                        'task5_audit_private_default_acl_probe',
                        'task7_outbox_probe_default_acl_probe',
                        'task5_takeover_default_acl_probe'
                )
                ORDER BY namespace.nspname, procedure.proname
                """
            ).fetchall()
        finally:
            probe_connection.rollback()

    assert roles == [
        ("campus_ai_audit_definer", False, False, False, False, False, False, False),
        ("campus_ai_context_definer", False, False, False, False, False, False, False),
        ("campus_ai_db_owner", False, False, False, False, False, False, False),
        ("campus_ai_migration", True, False, False, False, False, False, False),
        (
            "campus_ai_outbox_probe_definer",
            False,
            False,
            False,
            False,
            False,
            False,
            False,
        ),
        ("campus_ai_runtime", True, False, False, False, False, False, False),
        ("campus_ai_takeover_definer", False, False, False, False, False, False, False),
    ]
    assert memberships == [
        ("campus_ai_audit_definer", "campus_ai_migration", False, False, True),
        ("campus_ai_context_definer", "campus_ai_migration", False, False, True),
        ("campus_ai_db_owner", "campus_ai_migration", False, False, True),
        (
            "campus_ai_outbox_probe_definer",
            "campus_ai_migration",
            False,
            False,
            True,
        ),
        ("campus_ai_takeover_definer", "campus_ai_migration", False, False, True),
    ]
    assert acl is not None
    assert acl[:11] == (
        True,
        False,
        False,
        False,
        False,
        True,
        False,
        True,
        False,
        False,
        True,
    )
    assert acl[11:26] == (False,) * 15
    assert acl[26:] == (
        False,
        False,
        False,
        False,
        True,
        False,
        True,
        False,
        False,
        True,
        True,
        False,
    )
    assert owned == []
    expected_outbox_insert_columns = {
        "aggregate_id",
        "aggregate_type",
        "aggregate_version",
        "event_key",
        "event_type",
        "id",
        "occurred_at",
        "payload",
        "school_id",
    }
    assert set(special_column_acl) == {
        ("outbox_messages", column, "INSERT") for column in expected_outbox_insert_columns
    }
    idempotency_all_columns = {
        "action_code",
        "actor_assignment_id",
        "actor_assignment_version",
        "actor_context_key",
        "actor_context_type",
        "actor_guardian_relationship_id",
        "actor_guardian_relationship_version",
        "actor_person_id",
        "actor_user_id",
        "created_at",
        "id",
        "idempotency_key_hash",
        "request_sha256",
        "resource_id",
        "resource_type",
        "result_payload",
        "result_type",
        "school_id",
        "status",
        "updated_at",
    }
    idempotency_insert_columns = idempotency_all_columns - {
        "created_at",
        "result_payload",
        "result_type",
        "status",
        "updated_at",
    }
    idempotency_update_columns = {"result_payload", "result_type", "status", "updated_at"}
    assert set(idempotency_column_acl) == {
        (column, "SELECT") for column in idempotency_all_columns
    } | {(column, "INSERT") for column in idempotency_insert_columns} | {
        (column, "UPDATE") for column in idempotency_update_columns
    }
    assert definer_default_acl == [
        (
            "campus_ai_audit_definer",
            None,
            "f",
            "campus_ai_audit_definer",
            "EXECUTE",
        ),
        (
            "campus_ai_context_definer",
            None,
            "f",
            "campus_ai_context_definer",
            "EXECUTE",
        ),
        (
            "campus_ai_outbox_probe_definer",
            None,
            "f",
            "campus_ai_outbox_probe_definer",
            "EXECUTE",
        ),
        (
            "campus_ai_takeover_definer",
            None,
            "f",
            "campus_ai_takeover_definer",
            "EXECUTE",
        ),
    ]
    assert future_function_acl == [
        (
            "app",
            "task5_audit_default_acl_probe",
            "campus_ai_audit_definer",
            False,
        ),
        (
            "app",
            "task5_context_default_acl_probe",
            "campus_ai_context_definer",
            False,
        ),
        (
            "app",
            "task5_takeover_default_acl_probe",
            "campus_ai_takeover_definer",
            False,
        ),
        (
            "app",
            "task7_outbox_probe_default_acl_probe",
            "campus_ai_outbox_probe_definer",
            False,
        ),
        (
            "app_private",
            "task5_audit_private_default_acl_probe",
            "campus_ai_audit_definer",
            False,
        ),
    ]
    expected_audit_insert_columns = {
        "actor_assignment_id",
        "actor_assignment_version",
        "actor_context_type",
        "actor_guardian_relationship_id",
        "actor_guardian_relationship_version",
        "actor_person_id",
        "actor_user_id",
        "after_summary",
        "before_summary",
        "event_type",
        "idempotency_key_hash",
        "reason",
        "request_id",
        "school_id",
        "target_id",
        "target_type",
    }
    expected_audit_fact_select_columns = {
        "staff_assignments": {
            "school_id",
            "id",
            "person_id",
            "semantic_role_code",
            "valid_from",
            "valid_to",
            "status",
            "version",
        },
        "master_data_import_batches": {
            "school_id",
            "id",
            "mode",
            "source_kind",
            "source_ref",
            "status",
            "active_idempotency_record_id",
            "active_action_code",
            "version",
        },
        "master_data_import_units": {
            "school_id",
            "id",
            "batch_id",
            "unit_kind",
            "status",
            "target_id",
            "target_version",
            "soft_anomalies",
            "decision_reason",
            "last_attempt_idempotency_record_id",
            "last_attempt_completed",
        },
        "master_data_import_command_outcomes": {
            "school_id",
            "idempotency_record_id",
            "batch_id",
            "action_code",
            "status",
            "batch_version",
        },
        "master_data_import_takeovers": {
            "school_id",
            "id",
            "batch_id",
            "previous_idempotency_record_id",
            "new_idempotency_record_id",
            "original_actor_user_id",
            "original_actor_person_id",
            "original_actor_assignment_id",
            "original_actor_assignment_version",
            "new_actor_user_id",
            "new_actor_person_id",
            "new_actor_assignment_id",
            "new_actor_assignment_version",
            "request_id",
            "batch_version_before",
            "batch_version_after",
        },
        "command_idempotency_records": {
            "school_id",
            "id",
            "action_code",
            "actor_user_id",
            "actor_person_id",
            "actor_assignment_id",
            "actor_assignment_version",
            "resource_type",
            "resource_id",
            "idempotency_key_hash",
            "status",
            "result_type",
            "result_payload",
        },
        "school_people": {"school_id", "id", "status", "version"},
        "school_calendar_versions": {
            "school_id",
            "id",
            "status",
            "version",
            "source_kind",
            "source_ref",
        },
    }
    assert set(audit_definer_column_acl) == {
        ("audit_logs", column, "INSERT") for column in expected_audit_insert_columns
    } | {("audit_logs", "id", "SELECT")} | {
        (table_name, column, "SELECT")
        for table_name, columns in expected_audit_fact_select_columns.items()
        for column in columns
    }
    assert audit_definer_sequence_acl_row == (True,)
    assert audit_definer_pollution_removed == (False, False)
    assert audit_definer_xmin_acl == [
        ("command_idempotency_records", True),
        ("master_data_import_batches", True),
        ("master_data_import_command_outcomes", True),
        ("master_data_import_takeovers", False),
        ("master_data_import_units", True),
        ("school_calendar_versions", True),
        ("school_people", True),
        ("staff_assignments", False),
    ]
    assert set(outbox_probe_column_acl) == {
        (column, "SELECT")
        for column in {
            "aggregate_id",
            "aggregate_type",
            "aggregate_version",
            "event_key",
            "event_type",
            "id",
            "school_id",
        }
    }
    assert audit_function_acl == [
        ("app", "append_business_audit", True, False),
        ("app", "append_legacy_audit", True, False),
        ("app", "append_master_data_import_audit", True, False),
        ("app_private", "append_master_data_import_audit_xid", False, False),
        ("app_private", "is_safe_business_audit_summary", False, False),
    ]
    expected_definer_columns = {
        "app_private.context_proof_keys": {
            "fingerprint_sha256",
            "is_current",
            "key_version",
            "signing_key",
        },
        "public.account_person_binding_versions": {
            "account_user_id",
            "binding_id",
            "created_at",
            "effective_from",
            "effective_to",
            "external_subject",
            "id",
            "import_batch_id",
            "person_id",
            "provider_code",
            "provider_tenant_key",
            "revoked_at",
            "school_id",
            "source_id",
            "source_ref",
            "status",
            "target_version",
            "updated_at",
        },
        "public.account_person_bindings": {
            "account_user_id",
            "person_id",
            "school_id",
            "status",
            "valid_from",
            "valid_to",
            "version",
        },
        "public.campus_location_versions": {
            "kind",
            "location_id",
            "parent_location_id",
            "school_id",
            "status",
            "valid_from",
            "valid_to",
            "version",
        },
        "public.campus_locations": {
            "id",
            "school_id",
            "source_key",
            "status",
            "version",
        },
        "public.guardian_relationships": {
            "guardian_person_id",
            "id",
            "school_id",
            "status",
            "student_id",
            "valid_from",
            "valid_to",
            "version",
        },
        "public.master_data_import_batches": {
            "base_fact_version",
            "confirmation_card_sha256",
            "confirmation_consumed_at",
            "confirmation_expires_at",
            "confirmation_session_sha256",
            "confirmation_token_sha256",
            "confirmed_at",
            "created_at",
            "failed_units",
            "filename_summary",
            "id",
            "idempotency_key_hash",
            "mapping_payload",
            "mapping_version",
            "mode",
            "pending_units",
            "plan_sha256",
            "planned_units",
            "request_id",
            "result_payload",
            "school_id",
            "source_kind",
            "source_ref",
            "source_sha256",
            "source_size_bytes",
            "staged_assignment_version",
            "staged_authorization_version",
            "staged_by_assignment_id",
            "staged_by_person_id",
            "staged_by_user_id",
            "status",
            "succeeded_units",
            "target_fact_version",
            "version",
            "worksheet_names",
        },
        "public.master_data_import_units": {
            "batch_id",
            "decision_reason",
            "error_code",
            "error_field_code",
            "id",
            "initial_soft_anomalies",
            "last_attempt_completed",
            "last_attempt_idempotency_record_id",
            "payload",
            "payload_sha256",
            "row_number",
            "school_id",
            "sequence",
            "soft_anomalies",
            "status",
            "target_id",
            "target_version",
            "unit_kind",
            "version",
        },
        "public.organization_unit_versions": {
            "display_name",
            "organization_unit_id",
            "school_id",
            "status",
            "valid_from",
            "valid_to",
            "version",
        },
        "public.organization_units": {
            "id",
            "school_id",
            "source_key",
            "status",
            "version",
        },
        "public.school_fact_change_lineage": {
            "batch_id",
            "fact_kind",
            "fact_source_key",
            "operation",
            "school_id",
            "target_id",
            "target_version",
            "unit_id",
        },
        "public.school_people": {
            "id",
            "school_id",
            "source_key",
            "status",
            "version",
        },
        "public.school_person_profile_versions": {
            "lifecycle_status",
            "person_id",
            "school_id",
            "valid_from",
            "valid_to",
            "version",
        },
        "public.school_versions": {
            "display_name",
            "lifecycle_status",
            "school_id",
            "timezone_name",
            "valid_from",
            "valid_to",
            "version",
        },
        "public.schools": {"display_name", "id", "source_key", "status", "version"},
        "public.staff_assignment_versions": {
            "appointment_source",
            "approval_reference",
            "assignment_id",
            "assignment_kind",
            "created_at",
            "effective_from",
            "effective_to",
            "id",
            "import_batch_id",
            "organization_unit_id",
            "person_id",
            "planned_valid_to",
            "position_title",
            "revoked_at",
            "school_id",
            "semantic_role_code",
            "source_id",
            "source_ref",
            "status",
            "target_version",
            "updated_at",
        },
        "public.staff_assignments": {
            "id",
            "organization_unit_id",
            "person_id",
            "position_title",
            "school_id",
            "status",
            "valid_from",
            "valid_to",
            "version",
        },
        "public.students": {"id", "school_id", "source_key", "status", "version"},
        "public.users": {"id", "is_active", "security_version"},
    }
    expected_reactivation_prepare_insert_columns = {
        "master_data_import_batches": {
            "id",
            "school_id",
            "mode",
            "source_kind",
            "source_ref",
            "source_sha256",
            "filename_summary",
            "source_size_bytes",
            "worksheet_names",
            "mapping_version",
            "mapping_payload",
            "status",
            "planned_units",
            "succeeded_units",
            "failed_units",
            "pending_units",
            "confirmation_card_sha256",
            "request_id",
            "idempotency_key_hash",
            "plan_sha256",
            "base_fact_version",
            "target_fact_version",
            "confirmation_token_sha256",
            "confirmation_expires_at",
            "confirmation_session_sha256",
            "staged_by_user_id",
            "staged_by_person_id",
            "staged_by_assignment_id",
            "staged_assignment_version",
            "staged_authorization_version",
        },
        "master_data_import_units": {
            "id",
            "school_id",
            "batch_id",
            "sequence",
            "row_number",
            "unit_kind",
            "status",
            "payload",
            "payload_sha256",
            "soft_anomalies",
            "initial_soft_anomalies",
        },
    }
    assert set(definer_column_acl) == {
        (schema_table.split(".", 1)[0], schema_table.split(".", 1)[1], column, "SELECT")
        for schema_table, columns in expected_definer_columns.items()
        for column in columns
    } | {("public", "schools", "status", "UPDATE")} | {
        ("public", table_name, column, "INSERT")
        for table_name, columns in expected_reactivation_prepare_insert_columns.items()
        for column in columns
    }
