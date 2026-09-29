from __future__ import annotations

import json
import math
import os
import re
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import md5, sha256
from pathlib import Path
from time import perf_counter_ns
from typing import Any, Final, LiteralString, cast
from uuid import UUID, uuid4

import psycopg
import pytest

from app.db.rls_context import set_local_execution_context
from app.platform.contracts import (
    AssignmentId,
    PersonId,
    SchoolId,
    _issue_staff_actor_context,  # pyright: ignore[reportPrivateUsage]
)
from app.platform.identity.context_proof import ContextProofSigner
from app.platform.identity.execution_context import compose_request_context

from .conftest import PostgresRoles

CAPACITY_FLAG: Final = "RUN_MASTER_DATA_CAPACITY"
REPORT_PATH_ENV: Final = "MASTER_DATA_CAPACITY_REPORT_PATH"
PROJECT_ROOT: Final = Path(__file__).resolve().parents[4]
CONTEXT_KEY: Final = b"master-data-schema-context-signing-key-32-bytes"
AS_OF: Final = datetime(2026, 8, 1, 8, tzinfo=UTC)
HISTORICAL_AS_OF: Final = datetime(2011, 2, 1, 8, tzinfo=UTC)
WARMUP_RUNS: Final = 3
MEASUREMENT_RUNS: Final = 30

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get(CAPACITY_FLAG) != "1",
        reason="需要显式启用共享主数据基线/双倍容量外部门",
    ),
]


@dataclass(frozen=True, slots=True)
class SchoolAnchors:
    school_id: UUID
    account_user_id: int
    person_id: UUID
    assignment_id: UUID
    administrative_class_id: UUID
    root_location_id: UUID
    calendar_version_id: UUID
    student_id: UUID


@dataclass(frozen=True, slots=True)
class QuerySpec:
    name: str
    sql: str
    parameters: tuple[object, ...]
    expected_indexes: frozenset[str]
    protected_relation: str
    p95_budget_ms: float
    p99_budget_ms: float
    alternative_index_groups: tuple[frozenset[str], ...] = ()


@dataclass(frozen=True, slots=True)
class PlanEvidence:
    planning_time_ms: float
    execution_time_ms: float
    index_names: tuple[str, ...]
    shared_hit_blocks: int
    shared_read_blocks: int
    temp_read_blocks: int
    temp_written_blocks: int
    protected_relation_examined_rows: int
    protected_relation_seq_scan: bool


def test_plan_evidence_counts_filtered_and_rechecked_rows_as_examined() -> None:
    """不能把过滤后只返回一行的完整历史扫描误报成只检查一行。"""

    plan_nodes: list[dict[str, Any]] = [
        {
            "Relation Name": "student_enrollments",
            "Actual Rows": 1,
            "Rows Removed by Filter": 59_999,
            "Actual Loops": 1,
        },
        {
            "Relation Name": "student_enrollments",
            "Actual Rows": 0.5,
            "Rows Removed by Index Recheck": 1.5,
            "Actual Loops": 2,
        },
        {
            "Relation Name": "student_enrollments",
            "Actual Rows": 10,
        },
        {
            "Relation Name": "school_people",
            "Actual Rows": 120_000,
            "Rows Removed by Filter": 120_000,
            "Actual Loops": 3,
        },
    ]

    assert _protected_relation_examined_rows(plan_nodes, "student_enrollments") == 60_014


def _require_report_path() -> Path:
    raw_path = os.environ.get(REPORT_PATH_ENV)
    if raw_path is None or not raw_path.strip():
        pytest.fail(f"启用容量门时必须显式设置 {REPORT_PATH_ENV}")
    report_path = Path(raw_path).expanduser().resolve()
    if report_path == PROJECT_ROOT or PROJECT_ROOT in report_path.parents:
        pytest.fail("容量报告必须写到仓库外的显式临时证据路径")
    return report_path


def _capacity_connection(postgres_roles: PostgresRoles) -> psycopg.Connection[Any]:
    return psycopg.connect(
        **postgres_roles.admin_connection_parameters,
        autocommit=False,
    )


def _capacity_source_id(school_code: str) -> UUID:
    return UUID(bytes=md5(f"{school_code}:source".encode(), usedforsecurity=False).digest())


def _provision_capacity_school(
    postgres_roles: PostgresRoles,
    *,
    school_id: UUID,
    school_code: str,
    display_name: str,
) -> None:
    """Use the migration session to establish revision 20's immutable school anchor."""

    with postgres_roles.migration_connection() as connection:
        connection.execute("SET ROLE campus_ai_db_owner")
        connection.execute(
            """
            INSERT INTO schools(
                id, school_code, display_name, timezone_name, status, version
            ) VALUES (%s, %s, %s, 'Asia/Shanghai', 'active', 1)
            """,
            (school_id, school_code, display_name),
        )
        connection.execute(
            """
            INSERT INTO school_fact_sources(
                id, school_id, source_key, source_kind, source_ref, schema_version
            ) VALUES (%s, %s, %s, 'legacy', %s, 1)
            """,
            (
                _capacity_source_id(school_code),
                school_id,
                f"{school_code}:source",
                f"{school_code}:source",
            ),
        )


def _set_owner_role(connection: psycopg.Connection[Any]) -> None:
    connection.execute("RESET ROLE")
    connection.execute("SET LOCAL ROLE campus_ai_db_owner")


def _set_runtime_role(connection: psycopg.Connection[Any]) -> None:
    connection.execute("RESET ROLE")
    connection.execute("SET LOCAL ROLE campus_ai_runtime")


def _set_reader_role(connection: psycopg.Connection[Any]) -> None:
    connection.execute("RESET ROLE")
    connection.execute("SET LOCAL ROLE campus_ai_school_fact_reader")


def _seed_runtime_smoke(connection: psycopg.Connection[Any]) -> SchoolAnchors:
    """写入一组最小未提交事实，供 runtime+FORCE RLS 可见性先验。"""

    _set_owner_role(connection)
    row = connection.execute(
        """
        WITH school AS (
            SELECT id FROM schools
            WHERE id = md5('task9-capacity-smoke-school')::uuid
        ), account AS (
            INSERT INTO users(
                id, external_subject, display_name, is_active, security_version
            ) VALUES (
                700000001, 'capacity-smoke-account',
                'Synthetic Capacity Actor', true, 1
            ) RETURNING id
        ), person AS (
            INSERT INTO school_people(id, school_id, status, version)
            SELECT md5('task9-capacity-smoke-person')::uuid, school.id, 'active', 1
            FROM school RETURNING id, school_id
        ), person_name AS (
            INSERT INTO school_person_names(
                id, school_id, person_id, display_name, valid_from, valid_to,
                source_kind, source_ref, status, version
            )
            SELECT md5('task9-capacity-smoke-person-name')::uuid,
                   person.school_id, person.id, 'Synthetic Capacity Actor',
                   TIMESTAMPTZ '2025-01-01 00:00:00+00', NULL,
                   'capacity', 'capacity-smoke-name', 'confirmed', 1
            FROM person
        ), binding AS (
            INSERT INTO account_person_bindings(
                id, school_id, account_user_id, person_id,
                valid_from, valid_to, status, version
            )
            SELECT md5('task9-capacity-smoke-binding')::uuid,
                   person.school_id, account.id, person.id,
                   TIMESTAMPTZ '2025-01-01 00:00:00+00', NULL,
                   'confirmed', 1
            FROM person CROSS JOIN account
        ), unit AS (
            INSERT INTO organization_units(
                id, school_id, parent_organization_unit_id, kind, status, version
            )
            SELECT md5('task9-capacity-smoke-unit')::uuid,
                   school.id, NULL, 'department', 'active', 1
            FROM school RETURNING id, school_id
        ), unit_version AS (
            INSERT INTO organization_unit_versions(
                id, school_id, organization_unit_id, display_name, short_name,
                display_path, valid_from, valid_to, source_kind, source_ref,
                status, version
            )
            SELECT md5('task9-capacity-smoke-unit-version')::uuid,
                   unit.school_id, unit.id, 'Synthetic Capacity Unit', NULL,
                   '/capacity-smoke', TIMESTAMPTZ '2025-01-01 00:00:00+00',
                   NULL, 'capacity', 'capacity-smoke-unit-version', 'confirmed', 1
            FROM unit
        ), assignment AS (
            INSERT INTO staff_assignments(
                id, school_id, person_id, organization_unit_id,
                position_title, semantic_role_code, appointment_source,
                approval_reference, valid_from, valid_to, status, version
            )
            SELECT md5('task9-capacity-smoke-assignment')::uuid,
                   person.school_id, person.id, unit.id,
                   'Synthetic Capacity Role', 'capacity_reader', 'capacity',
                   'capacity-smoke-approval',
                   TIMESTAMPTZ '2025-01-01 00:00:00+00', NULL,
                   'confirmed', 1
            FROM person CROSS JOIN unit
            RETURNING id, school_id, person_id
        )
        SELECT assignment.school_id,
               account.id,
               assignment.person_id,
               assignment.id
        FROM assignment CROSS JOIN account
        """
    ).fetchone()
    assert row is not None
    school_id, account_user_id, person_id, assignment_id = row
    source_id = _capacity_source_id("capacity-smoke-school")
    connection.execute(
        """
        INSERT INTO account_person_binding_versions(
            id, school_id, binding_id, account_user_id, person_id,
            provider_code, provider_tenant_key, external_subject,
            effective_from, effective_to, revoked_at, status, target_version,
            source_ref, source_id, import_batch_id
        )
        SELECT md5('task9-capacity-smoke-binding-version')::uuid,
               binding.school_id, binding.id, binding.account_user_id, binding.person_id,
               'capacity', 'capacity-smoke', 'capacity-smoke-account',
               TIMESTAMPTZ '2025-01-01 00:00:00+00', NULL, NULL,
               'confirmed', 1, 'capacity-smoke-binding-version', %s, NULL
        FROM account_person_bindings AS binding
        WHERE binding.id = md5('task9-capacity-smoke-binding')::uuid
        """,
        (source_id,),
    )
    connection.execute(
        """
        INSERT INTO staff_assignment_versions(
            id, school_id, assignment_id, person_id, organization_unit_id,
            position_title, semantic_role_code, assignment_kind,
            appointment_source, approval_reference,
            effective_from, effective_to, planned_valid_to, revoked_at,
            status, target_version, source_ref, source_id, import_batch_id
        )
        SELECT md5('task9-capacity-smoke-assignment-version')::uuid,
               assignment.school_id, assignment.id, assignment.person_id,
               assignment.organization_unit_id, assignment.position_title,
               assignment.semantic_role_code, 'substantive',
               assignment.appointment_source, assignment.approval_reference,
               TIMESTAMPTZ '2025-01-01 00:00:00+00', NULL, NULL, NULL,
               'confirmed', 1, 'capacity-smoke-assignment-version', %s, NULL
        FROM staff_assignments AS assignment
        WHERE assignment.id = md5('task9-capacity-smoke-assignment')::uuid
        """,
        (source_id,),
    )
    return SchoolAnchors(
        school_id=school_id,
        account_user_id=account_user_id,
        person_id=person_id,
        assignment_id=assignment_id,
        administrative_class_id=UUID(int=0),
        root_location_id=UUID(int=0),
        calendar_version_id=UUID(int=0),
        student_id=UUID(int=0),
    )


def _bind_runtime_context(
    connection: psycopg.Connection[Any],
    anchors: SchoolAnchors,
) -> None:
    _set_runtime_role(connection)
    actor = _issue_staff_actor_context(
        account_user_id=anchors.account_user_id,
        school_id=SchoolId(anchors.school_id),
        person_id=PersonId(anchors.person_id),
        assignment_id=AssignmentId(anchors.assignment_id),
        assignment_version=1,
        authorization_version=1,
    )
    context = compose_request_context(actor, uuid4())
    envelope = ContextProofSigner(CONTEXT_KEY).sign_execution_context(
        context,
        session_nonce_hash=sha256(b"task9-capacity-session").hexdigest(),
        proof_expires_at=datetime.now(UTC) + timedelta(minutes=30),
    )
    set_local_execution_context(cast(Any, connection), envelope)


def _assert_runtime_rls_smoke(
    connection: psycopg.Connection[Any],
    anchors: SchoolAnchors,
) -> None:
    _bind_runtime_context(connection, anchors)
    identity = connection.execute(
        """
        SELECT current_user,
               current_setting('row_security'),
               role.rolbypassrls,
               table_info.relrowsecurity,
               table_info.relforcerowsecurity,
               app.require_school_context()
        FROM pg_roles AS role
        CROSS JOIN pg_class AS table_info
        JOIN pg_namespace AS namespace ON namespace.oid = table_info.relnamespace
        WHERE role.rolname = current_user
          AND namespace.nspname = 'public'
          AND table_info.relname = 'staff_assignments'
        """
    ).fetchone()
    assert identity == (
        "campus_ai_runtime",
        "on",
        False,
        True,
        True,
        anchors.school_id,
    )
    _set_reader_role(connection)
    reader_identity = connection.execute("SELECT current_user").fetchone()
    assert reader_identity == ("campus_ai_school_fact_reader",)
    visible = connection.execute(
        "SELECT count(*) FROM staff_assignments WHERE school_id = %s",
        (anchors.school_id,),
    ).fetchone()
    assert visible is not None and int(visible[0]) >= 1


def test_capacity_runtime_role_sees_uncommitted_seed_through_force_rls(
    current_master_data_schema: PostgresRoles,
) -> None:
    """容量计时前先证明同事务 runtime 读取未提交种子且 FORCE RLS 生效。"""

    smoke_school_id = UUID(
        bytes=md5(
            b"task9-capacity-smoke-school",
            usedforsecurity=False,
        ).digest()
    )
    _provision_capacity_school(
        current_master_data_schema,
        school_id=smoke_school_id,
        school_code="capacity-smoke-school",
        display_name="Synthetic Capacity School",
    )
    connection = _capacity_connection(current_master_data_schema)
    try:
        anchors = _seed_runtime_smoke(connection)
        seeded_schools = connection.execute(
            "SELECT count(*) FROM schools WHERE id = %s",
            (smoke_school_id,),
        ).fetchone()
        assert seeded_schools == (1,)
        assert anchors.school_id == smoke_school_id
        _assert_runtime_rls_smoke(connection, anchors)
    finally:
        connection.rollback()
        connection.close()


def test_frozen_capacity_baseline_and_double_scale(
    current_master_data_schema: PostgresRoles,
) -> None:
    """冻结基线/双倍规模必须满足查询预算、索引计划、缩放和脱敏证据契约。"""

    _run_capacity_gate(current_master_data_schema)


def _seed_school(connection: psycopg.Connection[Any], school_number: int) -> None:
    """用集合 SQL 增加一批同校冻结规模事实，不提交且不包含真实学校资料。"""

    parameters = {
        "school_number": school_number,
        "school_id": _school_anchors(1).school_id,
        "user_base": 700_000_000 + school_number * 10_000,
        "source_id": _capacity_source_id("capacity-school"),
    }
    connection.execute(
        """
        INSERT INTO users(
            id, external_subject, display_name, is_active, security_version
        )
        SELECT %(user_base)s + item,
               'capacity-account-' || %(school_number)s::text || '-' || item::text,
               'Synthetic Capacity Staff ' || lpad(item::text, 4, '0'),
               true, 1
        FROM generate_series(1, 500) AS item
        """,
        parameters,
    )
    connection.execute(
        """
        INSERT INTO school_people(id, school_id, status, version)
        SELECT md5(
                   'task9-capacity:' || %(school_number)s::text ||
                   ':staff-person:' || item::text
               )::uuid,
               %(school_id)s,
               'active', 1
        FROM generate_series(1, 500) AS item
        UNION ALL
        SELECT md5(
                   'task9-capacity:' || %(school_number)s::text ||
                   ':student-person:' || item::text
               )::uuid,
               %(school_id)s,
               'active', 1
        FROM generate_series(1, 5000) AS item
        """,
        parameters,
    )
    connection.execute(
        """
        INSERT INTO school_person_names(
            id, school_id, person_id, display_name, valid_from, valid_to,
            source_kind, source_ref, status, version
        )
        SELECT md5(
                   'task9-capacity:' || %(school_number)s::text ||
                   ':staff-name:' || item::text
               )::uuid,
               %(school_id)s,
               md5(
                   'task9-capacity:' || %(school_number)s::text ||
                   ':staff-person:' || item::text
               )::uuid,
               'Synthetic Capacity Staff ' || lpad(item::text, 4, '0'),
               TIMESTAMPTZ '2025-01-01 00:00:00+00', NULL::timestamptz,
               'capacity',
               'capacity-staff-name-' || %(school_number)s::text || '-' || item::text,
               'confirmed', 1
        FROM generate_series(1, 500) AS item
        UNION ALL
        SELECT md5(
                   'task9-capacity:' || %(school_number)s::text ||
                   ':student-name:' || item::text
               )::uuid,
               %(school_id)s,
               md5(
                   'task9-capacity:' || %(school_number)s::text ||
                   ':student-person:' || item::text
               )::uuid,
               'Synthetic Capacity Student ' || lpad(item::text, 5, '0'),
               TIMESTAMPTZ '2009-01-01 00:00:00+00', NULL::timestamptz,
               'capacity',
               'capacity-student-name-' || %(school_number)s::text || '-' || item::text,
               'confirmed', 1
        FROM generate_series(1, 5000) AS item
        """,
        parameters,
    )
    connection.execute(
        """
        INSERT INTO account_person_bindings(
            id, school_id, account_user_id, person_id,
            valid_from, valid_to, status, version
        )
        SELECT md5(
                   'task9-capacity:' || %(school_number)s::text ||
                   ':account-binding:' || item::text
               )::uuid,
               %(school_id)s,
               %(user_base)s + item,
               md5(
                   'task9-capacity:' || %(school_number)s::text ||
                   ':staff-person:' || item::text
               )::uuid,
               TIMESTAMPTZ '2025-01-01 00:00:00+00', NULL,
               'confirmed', 1
        FROM generate_series(1, 500) AS item
        """,
        parameters,
    )
    connection.execute(
        """
        INSERT INTO account_person_binding_versions(
            id, school_id, binding_id, account_user_id, person_id,
            provider_code, provider_tenant_key, external_subject,
            effective_from, effective_to, revoked_at, status, target_version,
            source_ref, source_id, import_batch_id
        )
        SELECT md5(
                   'task9-capacity:' || %(school_number)s::text ||
                   ':account-binding-version:1'
               )::uuid,
               binding.school_id, binding.id, binding.account_user_id, binding.person_id,
               'capacity', 'capacity-' || %(school_number)s::text,
               'capacity-account-' || %(school_number)s::text || '-1',
               TIMESTAMPTZ '2025-01-01 00:00:00+00', NULL, NULL,
               'confirmed', 1,
               'capacity-account-binding-version-' || %(school_number)s::text,
               %(source_id)s, NULL
        FROM account_person_bindings AS binding
        WHERE binding.school_id = %(school_id)s
          AND binding.id = md5(
              'task9-capacity:' || %(school_number)s::text || ':account-binding:1'
          )::uuid
        """,
        parameters,
    )
    connection.execute(
        """
        INSERT INTO organization_units(
            id, school_id, parent_organization_unit_id, kind, status, version
        )
        SELECT md5(
                   'task9-capacity:' || %(school_number)s::text ||
                   ':organization-unit:' || item::text
               )::uuid,
               %(school_id)s,
               CASE WHEN item = 1 THEN NULL ELSE md5(
                   'task9-capacity:' || %(school_number)s::text ||
                   ':organization-unit:1'
               )::uuid END,
               CASE WHEN item = 1 THEN 'school' ELSE 'department' END,
               'active', 1
        FROM generate_series(1, 6) AS item
        """,
        parameters,
    )
    connection.execute(
        """
        INSERT INTO organization_unit_versions(
            id, school_id, organization_unit_id, display_name, short_name,
            display_path, valid_from, valid_to, source_kind, source_ref,
            status, version
        )
        SELECT md5(
                   'task9-capacity:' || %(school_number)s::text ||
                   ':organization-unit-version:' || item::text
               )::uuid,
               %(school_id)s,
               md5(
                   'task9-capacity:' || %(school_number)s::text ||
                   ':organization-unit:' || item::text
               )::uuid,
               'Synthetic Capacity Unit ' || item::text,
               'SCU-' || %(school_number)s::text || '-' || item::text,
               '/capacity/' || %(school_number)s::text ||
                   '/unit-' || item::text,
               TIMESTAMPTZ '2025-01-01 00:00:00+00', NULL,
               'capacity',
               'capacity-unit-version-' || %(school_number)s::text || '-' || item::text,
               'confirmed', 1
        FROM generate_series(1, 6) AS item
        """,
        parameters,
    )
    connection.execute(
        """
        INSERT INTO staff_assignments(
            id, school_id, person_id, organization_unit_id,
            position_title, semantic_role_code, appointment_source,
            approval_reference, valid_from, valid_to, status, version
        )
        SELECT md5(
                   'task9-capacity:' || %(school_number)s::text ||
                   ':assignment:' || person_number::text || ':' || slot::text
               )::uuid,
               %(school_id)s,
               md5(
                   'task9-capacity:' || %(school_number)s::text ||
                   ':staff-person:' || person_number::text
               )::uuid,
               md5(
                   'task9-capacity:' || %(school_number)s::text ||
                   ':organization-unit:' || slot::text
               )::uuid,
               'Synthetic Capacity Position ' || slot::text,
               'capacity_role_' || slot::text,
               'capacity',
               'capacity-approval-' || %(school_number)s::text || '-' ||
                   person_number::text || '-' || slot::text,
               TIMESTAMPTZ '2025-01-01 00:00:00+00', NULL,
               'confirmed', 1
        FROM generate_series(1, 500) AS person_number
        CROSS JOIN generate_series(1, 3) AS slot
        """,
        parameters,
    )
    connection.execute(
        """
        INSERT INTO staff_assignment_versions(
            id, school_id, assignment_id, person_id, organization_unit_id,
            position_title, semantic_role_code, assignment_kind,
            appointment_source, approval_reference,
            effective_from, effective_to, planned_valid_to, revoked_at,
            status, target_version, source_ref, source_id, import_batch_id
        )
        SELECT md5(
                   'task9-capacity:' || %(school_number)s::text ||
                   ':assignment-version:' || person_number::text || ':' || slot::text
               )::uuid,
               assignment.school_id, assignment.id, assignment.person_id,
               assignment.organization_unit_id, assignment.position_title,
               assignment.semantic_role_code, 'substantive',
               assignment.appointment_source, assignment.approval_reference,
               TIMESTAMPTZ '2025-01-01 00:00:00+00', NULL, NULL, NULL,
               'confirmed', 1,
               'capacity-assignment-version-' || %(school_number)s::text || '-' ||
                   person_number::text || '-' || slot::text,
               %(source_id)s, NULL
        FROM generate_series(1, 500) AS person_number
        CROSS JOIN generate_series(1, 3) AS slot
        JOIN staff_assignments AS assignment
          ON assignment.school_id = %(school_id)s
         AND assignment.id = md5(
             'task9-capacity:' || %(school_number)s::text || ':assignment:' ||
             person_number::text || ':' || slot::text
         )::uuid
        """,
        parameters,
    )
    connection.execute(
        """
        INSERT INTO administrative_classes(
            id, school_id, school_year_code, grade_code, status, version
        )
        SELECT md5(
                   'task9-capacity:' || %(school_number)s::text ||
                   ':administrative-class:' || item::text
               )::uuid,
               %(school_id)s,
               '2026', 'grade-' || (((item - 1) %% 6) + 1)::text,
               'active', 1
        FROM generate_series(1, 50) AS item
        """,
        parameters,
    )
    connection.execute(
        """
        INSERT INTO administrative_class_versions(
            id, school_id, administrative_class_id, display_name, short_name,
            valid_from, valid_to, source_kind, source_ref, status, version
        )
        SELECT md5(
                   'task9-capacity:' || %(school_number)s::text ||
                   ':administrative-class-version:' || item::text
               )::uuid,
               %(school_id)s,
               md5(
                   'task9-capacity:' || %(school_number)s::text ||
                   ':administrative-class:' || item::text
               )::uuid,
               'Synthetic Capacity Class ' || lpad(item::text, 2, '0'),
               'C' || lpad(item::text, 2, '0'),
               TIMESTAMPTZ '2025-01-01 00:00:00+00', NULL,
               'capacity', 'capacity-class-' || %(school_number)s::text ||
                   '-' || item::text, 'confirmed', 1
        FROM generate_series(1, 50) AS item
        """,
        parameters,
    )
    connection.execute(
        """
        INSERT INTO students(id, school_id, person_id, status, version)
        SELECT md5(
                   'task9-capacity:' || %(school_number)s::text ||
                   ':student:' || item::text
               )::uuid,
               %(school_id)s,
               md5(
                   'task9-capacity:' || %(school_number)s::text ||
                   ':student-person:' || item::text
               )::uuid,
               'active', 1
        FROM generate_series(1, 5000) AS item
        """,
        parameters,
    )
    connection.execute(
        """
        INSERT INTO student_enrollments(
            id, school_id, student_id, administrative_class_id,
            student_number, seat_number, source_kind, source_ref,
            valid_from, valid_to, status, version
        )
        SELECT md5(
                   'task9-capacity:' || %(school_number)s::text ||
                   ':historical-enrollment:' || student_number::text ||
                   ':' || period_number::text
               )::uuid,
               %(school_id)s,
               md5(
                   'task9-capacity:' || %(school_number)s::text ||
                   ':student:' || student_number::text
               )::uuid,
               md5(
                   'task9-capacity:' || %(school_number)s::text ||
                   ':administrative-class:' ||
                   (((student_number + period_number - 2) %% 50) + 1)::text
               )::uuid,
               'H' || %(school_number)s::text || '-' ||
                   lpad(student_number::text, 6, '0'), NULL,
               'capacity',
               'capacity-history-' || %(school_number)s::text || '-' ||
                   student_number::text || '-' || period_number::text,
               TIMESTAMPTZ '2010-01-01 00:00:00+00'
                   + make_interval(years => period_number - 1),
               TIMESTAMPTZ '2010-01-01 00:00:00+00'
                   + make_interval(years => period_number),
               'confirmed', 1
        FROM generate_series(1, 5000) AS student_number
        CROSS JOIN generate_series(1, 12) AS period_number
        """,
        parameters,
    )
    connection.execute(
        """
        INSERT INTO student_enrollments(
            id, school_id, student_id, administrative_class_id,
            student_number, seat_number, source_kind, source_ref,
            valid_from, valid_to, status, version
        )
        SELECT md5(
                   'task9-capacity:' || %(school_number)s::text ||
                   ':current-enrollment:' || item::text
               )::uuid,
               %(school_id)s,
               md5(
                   'task9-capacity:' || %(school_number)s::text ||
                   ':student:' || item::text
               )::uuid,
               md5(
                   'task9-capacity:' || %(school_number)s::text ||
                   ':administrative-class:' || (((item - 1) %% 50) + 1)::text
               )::uuid,
               'S' || %(school_number)s::text || '-' ||
                   lpad(item::text, 6, '0'),
               (((item - 1) %% 50) + 1)::text,
               'capacity', 'capacity-current-' || %(school_number)s::text ||
                   '-' || item::text,
               TIMESTAMPTZ '2025-01-01 00:00:00+00', NULL,
               'confirmed', 1
        FROM generate_series(1, 5000) AS item
        """,
        parameters,
    )
    connection.execute(
        """
        INSERT INTO campus_locations(
            id, school_id, parent_location_id, kind, status, version
        )
        SELECT md5(
                   'task9-capacity:' || %(school_number)s::text ||
                   ':location:' || item::text
               )::uuid,
               %(school_id)s,
               NULL, 'campus', 'active', 1
        FROM generate_series(1, 1) AS item
        """,
        parameters,
    )
    connection.execute(
        """
        INSERT INTO campus_locations(
            id, school_id, parent_location_id, kind, status, version
        )
        SELECT md5(
                   'task9-capacity:' || %(school_number)s::text ||
                   ':location:' || item::text
               )::uuid,
               %(school_id)s,
               md5(
                   'task9-capacity:' || %(school_number)s::text || ':location:1'
               )::uuid,
               'building', 'active', 1
        FROM generate_series(2, 51) AS item
        """,
        parameters,
    )
    connection.execute(
        """
        INSERT INTO campus_locations(
            id, school_id, parent_location_id, kind, status, version
        )
        SELECT md5(
                   'task9-capacity:' || %(school_number)s::text ||
                   ':location:' || item::text
               )::uuid,
               %(school_id)s,
               md5(
                   'task9-capacity:' || %(school_number)s::text || ':location:' ||
                   (((item - 52) %% 50) + 2)::text
               )::uuid,
               'room', 'active', 1
        FROM generate_series(52, 1000) AS item
        """,
        parameters,
    )
    connection.execute(
        """
        INSERT INTO campus_location_versions(
            id, school_id, location_id, parent_location_id, kind, display_name, aliases,
            display_path,
            valid_from, valid_to, source_kind, source_ref, status, version
        )
        SELECT md5(
                   'task9-capacity:' || %(school_number)s::text ||
                   ':location-version:' || item::text
               )::uuid,
               %(school_id)s,
               md5(
                   'task9-capacity:' || %(school_number)s::text ||
                   ':location:' || item::text
               )::uuid,
               location.parent_location_id,
               location.kind,
               'Synthetic Capacity Location ' || lpad(item::text, 4, '0'),
               ARRAY[]::varchar[],
               '/capacity/' || %(school_number)s::text || '/' || item::text,
               TIMESTAMPTZ '2025-01-01 00:00:00+00', NULL,
               'capacity', 'capacity-location-' || %(school_number)s::text ||
                   '-' || item::text,
               'confirmed', 1
        FROM generate_series(1, 1000) AS item
        JOIN campus_locations AS location
          ON location.id = md5(
              'task9-capacity:' || %(school_number)s::text ||
              ':location:' || item::text
          )::uuid
         AND location.school_id = %(school_id)s
        """,
        parameters,
    )
    connection.execute(
        """
        INSERT INTO school_calendar_versions(
            id, school_id, display_name, timezone_name, valid_from, valid_to,
            source_kind, source_ref, status, version
        )
        SELECT md5(
                   'task9-capacity:' || %(school_number)s::text ||
                   ':calendar-version:' || item::text
               )::uuid,
               %(school_id)s,
               'Synthetic Capacity Calendar ' || item::text,
               'Asia/Shanghai',
               CASE WHEN %(school_number)s = 1 AND item = 1
                    THEN TIMESTAMPTZ '2025-01-01 00:00:00+00'
                    ELSE TIMESTAMPTZ '2000-01-01 00:00:00+00' END,
               CASE WHEN %(school_number)s = 1 AND item = 1
                    THEN NULL
                    ELSE TIMESTAMPTZ '2001-01-01 00:00:00+00' END,
               'capacity', 'capacity-calendar-' || %(school_number)s::text ||
                   '-' || item::text,
               CASE WHEN %(school_number)s = 1 AND item = 1
                    THEN 'published' ELSE 'superseded' END,
               1
        FROM generate_series(1, 1000) AS item
        """,
        parameters,
    )
    connection.execute(
        """
        INSERT INTO school_calendar_days(
            id, school_id, calendar_version_id, local_date, day_kind,
            actual_start_time, actual_end_time, source_kind, note, version
        )
        SELECT md5(
                   'task9-capacity:' || %(school_number)s::text ||
                   ':calendar-day:' || item::text
               )::uuid,
               %(school_id)s,
               md5(
                   'task9-capacity:' || %(school_number)s::text ||
                   ':calendar-version:1'
               )::uuid,
               DATE '2010-01-01' + (item - 1),
               CASE WHEN extract(isodow FROM DATE '2010-01-01' + (item - 1)) < 6
                    THEN 'working' ELSE 'rest' END,
               NULL, NULL, 'capacity', NULL, 1
        FROM generate_series(1, 7300) AS item
        """,
        parameters,
    )
    connection.execute(
        """
        INSERT INTO master_data_import_batches(
            id, school_id, mode, source_kind, source_ref, source_sha256,
            filename_summary, source_size_bytes, worksheet_names,
            mapping_version, mapping_payload, status,
            planned_units, succeeded_units, failed_units, pending_units,
            confirmation_card_sha256, confirmed_by_user_id,
            confirmed_by_person_id, confirmed_by_assignment_id, confirmed_at,
            request_id, idempotency_key_hash, active_idempotency_record_id,
            active_action_code, version
        )
        SELECT
            md5(
                'task9-capacity:' || %(school_number)s::text ||
                ':import-batch:' || batch_number::text
            )::uuid,
            %(school_id)s,
            'ordinary_atomic_units', 'manual',
            'capacity-import-' || %(school_number)s::text ||
                '-' || batch_number::text,
            repeat('a', 64), NULL, NULL, '[]'::jsonb,
            1, '{}'::jsonb, 'ready_for_confirmation',
            500, 0, 0, 500,
            NULL, NULL, NULL, NULL, NULL,
            md5(
                'task9-capacity:' || %(school_number)s::text ||
                ':import-request:' || batch_number::text
            )::uuid,
            repeat('b', 64), NULL, NULL, 1
        FROM generate_series(1, 200) AS batch_number
        """,
        parameters,
    )
    connection.execute(
        """
        INSERT INTO master_data_import_units(
            id, school_id, batch_id, sequence, row_number, unit_kind, status,
            payload, payload_sha256, soft_anomalies, initial_soft_anomalies,
            decision_reason, target_id, target_version, error_code,
            error_field_code, last_attempt_idempotency_record_id,
            last_attempt_completed, version
        )
        SELECT md5(
                   'task9-capacity:' || %(school_number)s::text ||
                   ':import-unit:' || item::text
               )::uuid,
               %(school_id)s,
               md5(
                   'task9-capacity:' || %(school_number)s::text ||
                   ':import-batch:' || (((item - 1) / 500) + 1)::text
               )::uuid,
               ((item - 1) %% 500) + 1, ((item - 1) %% 500) + 1,
               'person', 'pending', '{}'::jsonb,
               repeat('c', 64), '[]'::jsonb, '[]'::jsonb,
               NULL, NULL, NULL, NULL, NULL, NULL, NULL, 1
        FROM generate_series(1, 100000) AS item
        """,
        parameters,
    )


def _school_anchors(school_number: int) -> SchoolAnchors:
    prefix = f"task9-capacity:{school_number}:"

    def identifier(suffix: str) -> UUID:
        digest = md5(  # noqa: S324 - deterministic synthetic UUID, not security
            f"{prefix}{suffix}".encode(), usedforsecurity=False
        ).digest()
        return UUID(bytes=digest)

    return SchoolAnchors(
        school_id=UUID(bytes=md5(b"task9-capacity:1:school", usedforsecurity=False).digest()),
        account_user_id=700_000_000 + school_number * 10_000 + 1,
        person_id=identifier("staff-person:1"),
        assignment_id=identifier("assignment:1:1"),
        administrative_class_id=identifier("administrative-class:1"),
        root_location_id=identifier("location:1"),
        calendar_version_id=identifier("calendar-version:1"),
        student_id=identifier("student:1"),
    )


CAPACITY_TABLES: Final = (
    "schools",
    "users",
    "school_people",
    "school_person_names",
    "account_person_bindings",
    "organization_units",
    "organization_unit_versions",
    "staff_assignments",
    "staff_assignment_versions",
    "administrative_classes",
    "administrative_class_versions",
    "students",
    "student_enrollments",
    "campus_locations",
    "campus_location_versions",
    "school_calendar_versions",
    "school_calendar_days",
    "master_data_import_batches",
    "master_data_import_units",
)


def _analyze_capacity_tables(connection: psycopg.Connection[Any]) -> None:
    _set_owner_role(connection)
    connection.execute("ANALYZE " + ", ".join(CAPACITY_TABLES))


def _dataset_size(scale: int) -> dict[str, int]:
    return {
        "schools": 1,
        "staff_people": 500 * scale,
        "account_bindings": 500 * scale,
        "student_people": 5_000 * scale,
        "current_students": 5_000 * scale,
        "historical_enrollments": 60_000 * scale,
        "current_enrollments": 5_000 * scale,
        "staff_assignments": 1_500 * scale,
        "locations": 1_000 * scale,
        "calendar_versions": 1_000 * scale,
        "calendar_days": 7_300 * scale,
        "import_units": 100_000 * scale,
    }


def _assert_dataset_size(
    connection: psycopg.Connection[Any],
    scale: int,
) -> None:
    expected = _dataset_size(scale)
    school_ids = (_school_anchors(1).school_id,)
    actual_row = connection.execute(
        """
        SELECT
            (SELECT count(*) FROM schools WHERE id = ANY(%(school_ids)s)),
            (SELECT count(*) FROM school_people
             WHERE school_id = ANY(%(school_ids)s)
               AND id::text IN (
                   SELECT md5(
                              'task9-capacity:' || school_number::text ||
                              ':staff-person:' || item::text
                          )::uuid::text
                   FROM generate_series(1, %(scale)s) AS school_number
                   CROSS JOIN generate_series(1, 500) AS item
               )),
            (SELECT count(*) FROM account_person_bindings
             WHERE school_id = ANY(%(school_ids)s)),
            (SELECT count(*) FROM school_people
             WHERE school_id = ANY(%(school_ids)s)) - (500 * %(scale)s),
            (SELECT count(*) FROM students WHERE school_id = ANY(%(school_ids)s)),
            (SELECT count(*) FROM student_enrollments
             WHERE school_id = ANY(%(school_ids)s) AND valid_to IS NOT NULL),
            (SELECT count(*) FROM student_enrollments
             WHERE school_id = ANY(%(school_ids)s) AND valid_to IS NULL),
            (SELECT count(*) FROM staff_assignments
             WHERE school_id = ANY(%(school_ids)s)),
            (SELECT count(*) FROM campus_locations
             WHERE school_id = ANY(%(school_ids)s)),
            (SELECT count(*) FROM school_calendar_versions
             WHERE school_id = ANY(%(school_ids)s)),
            (SELECT count(*) FROM school_calendar_days
             WHERE school_id = ANY(%(school_ids)s)),
            (SELECT count(*) FROM master_data_import_units
             WHERE school_id = ANY(%(school_ids)s))
        """,
        {"school_ids": list(school_ids), "scale": scale},
    ).fetchone()
    assert actual_row is not None
    keys = tuple(expected)
    assert dict(zip(keys, (int(value) for value in actual_row), strict=True)) == expected


def _query_specs(anchors: SchoolAnchors) -> tuple[QuerySpec, ...]:
    self_current_assignment = QuerySpec(
        name="self_current_assignment",
        sql="""
            SELECT assignment.assignment_id, assignment.organization_unit_id,
                   assignment.position_title, assignment.semantic_role_code,
                   assignment.assignment_kind, assignment.effective_from,
                   assignment.effective_to, assignment.planned_valid_to,
                   assignment.target_version
            FROM staff_assignment_versions AS assignment
            WHERE assignment.school_id = %s
              AND assignment.assignment_id = %s
              AND assignment.person_id = %s
              AND assignment.status = 'confirmed'
              AND assignment.revoked_at IS NULL
              AND assignment.effective_from <= %s
              AND (assignment.effective_to IS NULL OR assignment.effective_to > %s)
              AND (assignment.planned_valid_to IS NULL OR assignment.planned_valid_to > %s)
            ORDER BY assignment.assignment_id
            LIMIT 1
        """,
        parameters=(
            anchors.school_id,
            anchors.assignment_id,
            anchors.person_id,
            AS_OF,
            AS_OF,
            AS_OF,
        ),
        expected_indexes=frozenset(),
        protected_relation="staff_assignment_versions",
        p95_budget_ms=100.0,
        p99_budget_ms=250.0,
        alternative_index_groups=(
            frozenset(
                {
                    "ix_staff_assignment_versions_as_of",
                    "excl_staff_assignment_versions_confirmed_period",
                }
            ),
        ),
    )
    public_location_first_page = QuerySpec(
        name="public_location_first_page",
        sql="""
            SELECT location.id, version.kind, version.business_code,
                   version.display_name, version.display_path,
                   version.valid_from, version.valid_to, version.version
            FROM campus_locations AS location
            JOIN campus_location_versions AS version
              ON version.school_id = location.school_id
             AND version.location_id = location.id
            WHERE location.school_id = %s
              AND location.status = 'active'
              AND version.status = 'confirmed'
              AND version.valid_from <= %s
              AND (version.valid_to IS NULL OR version.valid_to > %s)
              AND app.school_fact_location_history_is_resolvable(
                  version.school_id, version.location_id, %s
              ) IS TRUE
            ORDER BY location.id
            LIMIT 50
        """,
        parameters=(
            anchors.school_id,
            AS_OF,
            AS_OF,
            AS_OF,
        ),
        expected_indexes=frozenset({"ix_campus_location_versions_as_of"}),
        protected_relation="campus_location_versions",
        p95_budget_ms=300.0,
        p99_budget_ms=800.0,
    )
    public_location_kind_page = QuerySpec(
        name="public_location_kind_page",
        sql="""
            SELECT location.id, version.kind, version.business_code,
                   version.display_name, version.display_path,
                   version.valid_from, version.valid_to, version.version
            FROM campus_locations AS location
            JOIN campus_location_versions AS version
              ON version.school_id = location.school_id
             AND version.location_id = location.id
            WHERE location.school_id = %s
              AND location.status = 'active'
              AND version.status = 'confirmed'
              AND version.kind = 'room'
              AND version.valid_from <= %s
              AND (version.valid_to IS NULL OR version.valid_to > %s)
              AND app.school_fact_location_history_is_resolvable(
                  version.school_id, version.location_id, %s
              ) IS TRUE
            ORDER BY location.id
            LIMIT 50
        """,
        parameters=(anchors.school_id, AS_OF, AS_OF, AS_OF),
        expected_indexes=frozenset({"ix_campus_location_versions_as_of"}),
        protected_relation="campus_location_versions",
        p95_budget_ms=300.0,
        p99_budget_ms=800.0,
    )
    public_location_after_cursor = QuerySpec(
        name="public_location_after_cursor",
        sql="""
            SELECT location.id, version.kind, version.business_code,
                   version.display_name, version.display_path,
                   version.valid_from, version.valid_to, version.version
            FROM campus_locations AS location
            JOIN campus_location_versions AS version
              ON version.school_id = location.school_id
             AND version.location_id = location.id
            WHERE location.school_id = %s
              AND location.id > %s
              AND location.status = 'active'
              AND version.status = 'confirmed'
              AND version.valid_from <= %s
              AND (version.valid_to IS NULL OR version.valid_to > %s)
              AND app.school_fact_location_history_is_resolvable(
                  version.school_id, version.location_id, %s
              ) IS TRUE
            ORDER BY location.id
            LIMIT 50
        """,
        parameters=(anchors.school_id, anchors.root_location_id, AS_OF, AS_OF, AS_OF),
        expected_indexes=frozenset(),
        protected_relation="campus_location_versions",
        p95_budget_ms=300.0,
        p99_budget_ms=800.0,
        alternative_index_groups=(
            frozenset(
                {
                    "ix_campus_location_versions_as_of",
                    "excl_campus_location_versions_confirmed_period",
                }
            ),
        ),
    )
    return (
        self_current_assignment,
        public_location_first_page,
        public_location_kind_page,
        public_location_after_cursor,
    )


def _history_query(anchors: SchoolAnchors) -> QuerySpec:
    return QuerySpec(
        name="historical_public_location_as_of",
        sql="""
            SELECT location.id, version.kind, version.display_name,
                   version.valid_from, version.valid_to, version.version
            FROM campus_locations AS location
            JOIN campus_location_versions AS version
              ON version.school_id = location.school_id
             AND version.location_id = location.id
            WHERE location.school_id = %s
              AND location.status = 'active'
              AND version.status = 'confirmed'
              AND version.valid_from <= %s
              AND (version.valid_to IS NULL OR version.valid_to > %s)
              AND app.school_fact_location_history_is_resolvable(
                  version.school_id, version.location_id, %s
              ) IS TRUE
            ORDER BY location.id
            LIMIT 50
        """,
        parameters=(
            anchors.school_id,
            HISTORICAL_AS_OF,
            HISTORICAL_AS_OF,
            HISTORICAL_AS_OF,
        ),
        expected_indexes=frozenset({"ix_campus_location_versions_as_of"}),
        protected_relation="campus_location_versions",
        p95_budget_ms=0.0,
        p99_budget_ms=0.0,
    )


def _walk_plan(node: dict[str, Any]) -> list[dict[str, Any]]:
    nodes = [node]
    for child in node.get("Plans", []):
        assert isinstance(child, dict)
        nodes.extend(_walk_plan(child))
    return nodes


def _plan_nonnegative_number(
    node: dict[str, Any],
    key: str,
    *,
    default: float,
) -> float:
    value = node.get(key, default)
    assert not isinstance(value, bool) and isinstance(value, (int, float)), (
        f"EXPLAIN {key} 必须是数值"
    )
    numeric = float(value)
    assert math.isfinite(numeric) and numeric >= 0, f"EXPLAIN {key} 必须是非负有限数值"
    return numeric


def _protected_relation_examined_rows(
    nodes: list[dict[str, Any]],
    protected_relation: str,
) -> int:
    examined_rows = 0.0
    for node in nodes:
        if node.get("Relation Name") != protected_relation:
            continue
        loops = _plan_nonnegative_number(node, "Actual Loops", default=1.0)
        rows_per_loop = sum(
            _plan_nonnegative_number(node, key, default=0.0)
            for key in (
                "Actual Rows",
                "Rows Removed by Filter",
                "Rows Removed by Index Recheck",
            )
        )
        examined_rows += rows_per_loop * loops
    return math.ceil(examined_rows)


def _explain_query(
    connection: psycopg.Connection[Any],
    spec: QuerySpec,
) -> PlanEvidence:
    explain_sql = cast(
        LiteralString,
        "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + spec.sql,
    )
    row = connection.execute(
        explain_sql,
        spec.parameters,
    ).fetchone()
    assert row is not None
    document = row[0]
    assert isinstance(document, list) and len(document) == 1
    statement = document[0]
    assert isinstance(statement, dict)
    root = statement["Plan"]
    assert isinstance(root, dict)
    nodes = _walk_plan(root)
    indexes = tuple(
        sorted(
            {str(node["Index Name"]) for node in nodes if isinstance(node.get("Index Name"), str)}
        )
    )
    protected_nodes = [
        node for node in nodes if node.get("Relation Name") == spec.protected_relation
    ]
    protected_examined_rows = _protected_relation_examined_rows(
        nodes,
        spec.protected_relation,
    )
    protected_seq_scan = any(
        node.get("Node Type") in {"Seq Scan", "Parallel Seq Scan"} for node in protected_nodes
    )
    return PlanEvidence(
        planning_time_ms=float(statement["Planning Time"]),
        execution_time_ms=float(statement["Execution Time"]),
        index_names=indexes,
        shared_hit_blocks=int(root.get("Shared Hit Blocks", 0)),
        shared_read_blocks=int(root.get("Shared Read Blocks", 0)),
        temp_read_blocks=int(root.get("Temp Read Blocks", 0)),
        temp_written_blocks=int(root.get("Temp Written Blocks", 0)),
        protected_relation_examined_rows=protected_examined_rows,
        protected_relation_seq_scan=protected_seq_scan,
    )


def _assert_plan(spec: QuerySpec, evidence: PlanEvidence) -> None:
    missing_indexes = spec.expected_indexes.difference(evidence.index_names)
    assert not missing_indexes, (
        f"{spec.name} 未采用冻结索引: {sorted(missing_indexes)}; 实际索引: {evidence.index_names}"
    )
    for alternatives in spec.alternative_index_groups:
        assert alternatives.intersection(evidence.index_names), (
            f"{spec.name} 未采用任一等价冻结索引: {sorted(alternatives)}; "
            f"实际索引: {evidence.index_names}"
        )
    assert not evidence.protected_relation_seq_scan, (
        f"{spec.name} 对 {spec.protected_relation} 使用了全表顺序扫描"
    )
    assert evidence.protected_relation_examined_rows < 5_000, (
        f"{spec.name} 检查 {evidence.protected_relation_examined_rows} 条保护关系记录"
    )


def _percentile(samples: list[float], quantile: float) -> float:
    assert samples
    ordered = sorted(samples)
    index = max(0, math.ceil(quantile * len(ordered)) - 1)
    return round(ordered[index], 4)


def _measure_query(
    connection: psycopg.Connection[Any],
    spec: QuerySpec,
) -> dict[str, Any]:
    evidence = _explain_query(connection, spec)
    _assert_plan(spec, evidence)
    query_sql = cast(LiteralString, spec.sql)
    for _ in range(WARMUP_RUNS):
        warm_rows = connection.execute(query_sql, spec.parameters).fetchall()
        assert warm_rows
    samples: list[float] = []
    for _ in range(MEASUREMENT_RUNS):
        started_at = perf_counter_ns()
        measured_rows = connection.execute(query_sql, spec.parameters).fetchall()
        elapsed_ms = (perf_counter_ns() - started_at) / 1_000_000
        assert measured_rows
        samples.append(elapsed_ms)
    latency = {
        "p50": _percentile(samples, 0.50),
        "p95": _percentile(samples, 0.95),
        "p99": _percentile(samples, 0.99),
    }
    assert latency["p95"] <= spec.p95_budget_ms, (
        f"{spec.name} P95 {latency['p95']}ms 超过 {spec.p95_budget_ms}ms"
    )
    assert latency["p99"] <= spec.p99_budget_ms, (
        f"{spec.name} P99 {latency['p99']}ms 超过 {spec.p99_budget_ms}ms"
    )
    normalized_sql = " ".join(spec.sql.split())
    return {
        "sql_sha256": sha256(normalized_sql.encode()).hexdigest(),
        "latency_ms": latency,
        "plan": {
            "planning_time_ms": round(evidence.planning_time_ms, 4),
            "execution_time_ms": round(evidence.execution_time_ms, 4),
            "buffers": {
                "shared_hit_blocks": evidence.shared_hit_blocks,
                "shared_read_blocks": evidence.shared_read_blocks,
                "temp_read_blocks": evidence.temp_read_blocks,
                "temp_written_blocks": evidence.temp_written_blocks,
            },
            "index_names": list(evidence.index_names),
            "protected_relation_examined_rows": evidence.protected_relation_examined_rows,
            "protected_relation_seq_scan": evidence.protected_relation_seq_scan,
        },
    }


def _plan_only_check(
    connection: psycopg.Connection[Any],
    spec: QuerySpec,
) -> dict[str, Any]:
    evidence = _explain_query(connection, spec)
    _assert_plan(spec, evidence)
    normalized_sql = " ".join(spec.sql.split())
    return {
        "sql_sha256": sha256(normalized_sql.encode()).hexdigest(),
        "planning_time_ms": round(evidence.planning_time_ms, 4),
        "execution_time_ms": round(evidence.execution_time_ms, 4),
        "buffers": {
            "shared_hit_blocks": evidence.shared_hit_blocks,
            "shared_read_blocks": evidence.shared_read_blocks,
            "temp_read_blocks": evidence.temp_read_blocks,
            "temp_written_blocks": evidence.temp_written_blocks,
        },
        "index_names": list(evidence.index_names),
        "protected_relation_examined_rows": evidence.protected_relation_examined_rows,
        "protected_relation_seq_scan": evidence.protected_relation_seq_scan,
    }


def _measure_scale(
    connection: psycopg.Connection[Any],
    *,
    scale_name: str,
    dataset_scale: int,
    anchors: SchoolAnchors,
) -> dict[str, Any]:
    _bind_runtime_context(connection, anchors)
    _set_reader_role(connection)
    reader_identity = connection.execute(
        "SELECT current_user, current_setting('row_security')"
    ).fetchone()
    assert reader_identity == ("campus_ai_school_fact_reader", "on")
    queries = {spec.name: _measure_query(connection, spec) for spec in _query_specs(anchors)}
    history_spec = _history_query(anchors)
    return {
        "scale": scale_name,
        "dataset": _dataset_size(dataset_scale),
        "database_role": "campus_ai_school_fact_reader",
        "queries": queries,
        "plan_checks": {
            history_spec.name: _plan_only_check(connection, history_spec),
        },
    }


def _assert_double_scale(
    baseline: dict[str, Any],
    doubled: dict[str, Any],
) -> None:
    baseline_queries = baseline["queries"]
    doubled_queries = doubled["queries"]
    assert isinstance(baseline_queries, dict)
    assert isinstance(doubled_queries, dict)
    assert baseline_queries.keys() == doubled_queries.keys()
    for query_name, baseline_result in baseline_queries.items():
        doubled_result = doubled_queries[query_name]
        baseline_p95 = float(baseline_result["latency_ms"]["p95"])
        doubled_p95 = float(doubled_result["latency_ms"]["p95"])
        assert doubled_p95 <= baseline_p95 * 2, (
            f"{query_name} 双倍规模 P95 {doubled_p95}ms 超过基线 {baseline_p95}ms 的 2 倍"
        )


def _assert_report_sanitized(report: dict[str, Any]) -> None:
    forbidden_keys = {
        "sql",
        "parameters",
        "parameter",
        "path",
        "school_id",
        "person_id",
        "student_id",
        "assignment_id",
        "account_user_id",
        "display_name",
        "source_ref",
        "password",
        "secret",
        "token",
    }
    uuid_pattern = re.compile(
        r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
        r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
    )
    windows_path = re.compile(r"\b[A-Za-z]:[\\/]")
    sql_text = re.compile(
        r"\b(?:SELECT|INSERT|UPDATE|DELETE|FROM|WHERE|JOIN|EXPLAIN)\b",
        re.IGNORECASE,
    )

    def inspect(value: Any) -> None:
        if isinstance(value, dict):
            assert forbidden_keys.isdisjoint(value), (
                f"容量证据包含禁止字段: {sorted(forbidden_keys.intersection(value))}"
            )
            for nested in value.values():
                inspect(nested)
        elif isinstance(value, list):
            for nested in value:
                inspect(nested)
        elif isinstance(value, str):
            assert uuid_pattern.search(value) is None
            assert windows_path.search(value) is None
            assert not value.startswith(("/", "\\\\"))
            assert sql_text.search(value) is None

    inspect(report)
    serialized = json.dumps(report, ensure_ascii=False, sort_keys=True)
    assert "Synthetic Capacity" not in serialized
    assert str(PROJECT_ROOT) not in serialized


def _write_report(report_path: Path, report: dict[str, Any]) -> None:
    _assert_report_sanitized(report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = report_path.with_suffix(report_path.suffix + ".tmp")
    temporary_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary_path.replace(report_path)
    assert json.loads(report_path.read_text(encoding="utf-8")) == report


def _run_capacity_gate(postgres_roles: PostgresRoles) -> None:
    report_path = _require_report_path()
    anchors = _school_anchors(1)
    _provision_capacity_school(
        postgres_roles,
        school_id=anchors.school_id,
        school_code="capacity-school",
        display_name="Synthetic Capacity School",
    )
    connection = _capacity_connection(postgres_roles)
    try:
        _set_owner_role(connection)
        _seed_school(connection, 1)
        _assert_dataset_size(connection, 1)
        _analyze_capacity_tables(connection)
        baseline = _measure_scale(
            connection,
            scale_name="baseline",
            dataset_scale=1,
            anchors=anchors,
        )

        _set_owner_role(connection)
        _seed_school(connection, 2)
        _assert_dataset_size(connection, 2)
        _analyze_capacity_tables(connection)
        doubled = _measure_scale(
            connection,
            scale_name="double",
            dataset_scale=2,
            anchors=anchors,
        )
        _assert_double_scale(baseline, doubled)
        server_version_row = connection.execute("SHOW server_version").fetchone()
        assert server_version_row is not None
        authoritative_linux_ci = (
            sys.platform.startswith("linux") and os.environ.get("GITHUB_ACTIONS") == "true"
        )
        report = {
            "report_version": 1,
            "authority": "linux_ci" if authoritative_linux_ci else "local_diagnostic",
            "postgres_version": str(server_version_row[0]),
            "measurement": {
                "warmup_runs": WARMUP_RUNS,
                "measurement_runs": MEASUREMENT_RUNS,
                "database_role": "campus_ai_school_fact_reader",
            },
            "scales": [baseline, doubled],
        }
        _write_report(report_path, report)
    finally:
        connection.rollback()
        connection.close()
