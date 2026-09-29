from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from psycopg.types.json import Jsonb

from tests.master_data.conftest import PostgresRoles

API_ROOT = Path(__file__).resolve().parents[2]

_AUDIT_ARGUMENTS = (
    "integer,uuid,uuid,uuid,bigint,uuid,bigint,text,text,text,text,uuid,jsonb,jsonb,uuid,text"
)
_PUBLIC_AUDIT_SIGNATURE = f"app.append_business_audit({_AUDIT_ARGUMENTS})"
_SAVED_AUDIT_SIGNATURE = f"app_private.append_business_audit_revision_12({_AUDIT_ARGUMENTS})"


def _current_alembic_head() -> str:
    configuration = Config(str(API_ROOT / "alembic.ini"))
    configuration.set_main_option("script_location", str(API_ROOT / "alembic"))
    head = ScriptDirectory.from_config(configuration).get_current_head()
    assert head is not None
    return head


_APPEND_AUDIT_SQL = """
SELECT app.append_business_audit(
    %s, %s, %s, %s, 1, NULL, NULL, 'assignment', %s,
    %s, %s, %s, %s, %s, %s, %s
)
"""


@dataclass(frozen=True, slots=True)
class _AuditActor:
    user_id: int
    school_id: UUID
    person_id: UUID
    assignment_id: UUID


@dataclass(frozen=True, slots=True)
class _FunctionAcl:
    exists: bool
    owner: str | None
    security_definer: bool
    configuration: tuple[str, ...]
    non_owner_execute_grantees: tuple[str, ...]


def _seed_actor(connection: psycopg.Connection[Any]) -> _AuditActor:
    marker = uuid4().hex
    school_id = uuid4()
    person_id = uuid4()
    unit_id = uuid4()
    assignment_id = uuid4()
    valid_from = datetime.now(UTC) - timedelta(days=1)
    connection.execute(
        """
        INSERT INTO schools (
            id, school_code, display_name, timezone_name, status, version
        ) VALUES (%s, %s, '审计测试学校', 'Asia/Shanghai', 'active', 1)
        """,
        (school_id, f"audit-repair-{marker}"),
    )
    row = connection.execute(
        """
        INSERT INTO users (external_subject, display_name, is_active, security_version)
        VALUES (%s, '审计测试人员', true, 1)
        RETURNING id
        """,
        (f"audit-repair:{marker}",),
    ).fetchone()
    assert row is not None
    connection.execute(
        """
        INSERT INTO school_people (id, school_id, status, version)
        VALUES (%s, %s, 'active', 1)
        """,
        (person_id, school_id),
    )
    connection.execute(
        """
        INSERT INTO organization_units (id, school_id, kind, status, version)
        VALUES (%s, %s, 'department', 'active', 1)
        """,
        (unit_id, school_id),
    )
    has_structure_lineage_guard = connection.execute(
        "SELECT to_regprocedure('public.sf18_require_structure_version_lineage()') IS NOT NULL"
    ).fetchone() == (True,)
    if not has_structure_lineage_guard:
        connection.execute(
            """
            INSERT INTO organization_unit_versions (
                id, school_id, organization_unit_id, display_name, short_name,
                display_path, valid_from, source_kind, source_ref, status, version
            ) VALUES (
                %s, %s, %s, '审计测试组织', '审计测试组织', '/审计测试组织',
                %s, 'manual', %s, 'confirmed', 1
            )
            """,
            (uuid4(), school_id, unit_id, valid_from, f"audit-repair-{marker}"),
        )
    connection.execute(
        """
        INSERT INTO staff_assignments (
            id, school_id, person_id, organization_unit_id, position_title,
            semantic_role_code, appointment_source, approval_reference,
            valid_from, status, version
        ) VALUES (
            %s, %s, %s, %s, '报修人员', 'repair_staff', 'manual', %s,
            %s, 'confirmed', 1
        )
        """,
        (
            assignment_id,
            school_id,
            person_id,
            unit_id,
            f"audit-repair-{marker}",
            valid_from,
        ),
    )
    return _AuditActor(
        user_id=int(row[0]),
        school_id=school_id,
        person_id=person_id,
        assignment_id=assignment_id,
    )


def _repair_summaries(ticket_id: object) -> tuple[dict[str, object], dict[str, object]]:
    return (
        {
            "state_code": "repair.absent",
            "resource_version": None,
            "stable_reference_ids": [str(ticket_id)],
            "fact_codes": [],
        },
        {
            "state_code": "repair.submitted",
            "resource_version": 1,
            "stable_reference_ids": [str(ticket_id)],
            "fact_codes": ["repair.submitted"],
        },
    )


def _append(
    connection: psycopg.Connection[Any],
    *,
    actor: _AuditActor,
    event_type: str = "repair.ticket.submitted",
    target_type: str = "repair_ticket",
    before_state: str = "repair.absent",
    after_state: str = "repair.submitted",
    fact_code: str = "repair.submitted",
    reason_code: str = "manual.repair-submitted",
    extra_key: tuple[str, str] | None = None,
) -> None:
    ticket_id = uuid4()
    request_id = uuid4()
    before_summary, after_summary = _repair_summaries(ticket_id)
    before_summary["state_code"] = before_state
    after_summary["state_code"] = after_state
    after_summary["fact_codes"] = [fact_code]
    if extra_key is not None:
        after_summary[extra_key[0]] = extra_key[1]

    connection.execute(
        _APPEND_AUDIT_SQL,
        (
            actor.user_id,
            actor.school_id,
            actor.person_id,
            actor.assignment_id,
            "0" * 64,
            event_type,
            target_type,
            ticket_id,
            Jsonb(before_summary),
            Jsonb(after_summary),
            request_id,
            reason_code,
        ),
    )


def _append_assignment(
    connection: psycopg.Connection[Any],
    actor: _AuditActor,
) -> None:
    assignment_id = uuid4()
    before_summary = {
        "state_code": "assignment.absent",
        "resource_version": None,
        "stable_reference_ids": [str(assignment_id)],
        "fact_codes": [],
    }
    after_summary = {
        "state_code": "assignment.confirmed",
        "resource_version": 1,
        "stable_reference_ids": [str(assignment_id)],
        "fact_codes": ["assignment.updated"],
    }
    connection.execute(
        _APPEND_AUDIT_SQL,
        (
            actor.user_id,
            actor.school_id,
            actor.person_id,
            actor.assignment_id,
            "0" * 64,
            "master_data.assignment.changed",
            "staff_assignment",
            assignment_id,
            Jsonb(before_summary),
            Jsonb(after_summary),
            uuid4(),
            "manual.assignment-confirmed",
        ),
    )


def _run_alembic(postgres_roles: PostgresRoles, *arguments: str) -> None:
    migration = postgres_roles.migration_connection_parameters
    environment = os.environ.copy()
    environment.update(
        {
            "POSTGRES_MIGRATION_HOST": str(migration["host"]),
            "POSTGRES_MIGRATION_PORT": str(migration["port"]),
            "POSTGRES_MIGRATION_DB": str(migration["dbname"]),
            "POSTGRES_MIGRATION_USER": str(migration["user"]),
            "POSTGRES_MIGRATION_PASSWORD": str(migration["password"]),
            "POSTGRES_MIGRATION_CONTEXT_SIGNING_KEY": (
                "master-data-schema-context-signing-key-32-bytes"
            ),
        }
    )
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *arguments],
        cwd=API_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=45,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def _function_acl(
    connection: psycopg.Connection[Any],
    signature: str,
) -> _FunctionAcl:
    qualified_name = signature.split("(", 1)[0]
    schema_name, function_name = qualified_name.split(".", 1)
    row = connection.execute(
        """
        SELECT
            target.oid IS NOT NULL,
            CASE WHEN target.oid IS NULL THEN NULL ELSE pg_get_userbyid(proc.proowner) END,
            COALESCE(proc.prosecdef, false),
            COALESCE(proc.proconfig, ARRAY[]::text[]),
            CASE WHEN target.oid IS NULL THEN ARRAY[]::text[] ELSE ARRAY(
                SELECT CASE
                    WHEN expanded.grantee = 0 THEN 'PUBLIC'
                    ELSE pg_get_userbyid(expanded.grantee)
                END
                FROM aclexplode(
                    COALESCE(proc.proacl, acldefault('f', proc.proowner))
                ) AS expanded
                WHERE expanded.grantee <> proc.proowner
                  AND expanded.privilege_type = 'EXECUTE'
                ORDER BY 1
            ) END
        FROM (
            SELECT proc.oid
            FROM pg_proc AS proc
            JOIN pg_namespace AS namespace ON namespace.oid = proc.pronamespace
            WHERE namespace.nspname = %s
              AND proc.proname = %s
              AND proc.pronargs = 16
        ) AS target
        LEFT JOIN pg_proc AS proc ON proc.oid = target.oid
        """,
        (schema_name, function_name),
    ).fetchone()
    if row is None:
        return _FunctionAcl(False, None, False, (), ())
    return _FunctionAcl(
        exists=bool(row[0]),
        owner=row[1],
        security_definer=bool(row[2]),
        configuration=tuple(row[3]),
        non_owner_execute_grantees=tuple(row[4]),
    )


def test_revision_13_accepts_only_the_complete_repair_submission_tuple(
    current_master_data_schema: PostgresRoles,
) -> None:
    with current_master_data_schema.migration_connection() as connection:
        connection.execute("SET ROLE campus_ai_db_owner")
        actor = _seed_actor(connection)
        _append(connection, actor=actor)


def test_revision_13_saved_audit_function_is_private(
    current_master_data_schema: PostgresRoles,
) -> None:
    with current_master_data_schema.migration_connection() as connection:
        assert _function_acl(connection, _SAVED_AUDIT_SIGNATURE) == _FunctionAcl(
            exists=True,
            owner="campus_ai_audit_definer",
            security_definer=True,
            configuration=("search_path=pg_catalog",),
            non_owner_execute_grantees=(),
        )
        assert _function_acl(connection, _PUBLIC_AUDIT_SIGNATURE) == _FunctionAcl(
            exists=True,
            owner="campus_ai_audit_definer",
            security_definer=True,
            configuration=("search_path=pg_catalog",),
            non_owner_execute_grantees=("campus_ai_db_owner", "campus_ai_runtime"),
        )


@pytest.mark.parametrize(
    "change",
    [
        {"event_type": "master_data.assignment.changed"},
        {"target_type": "staff_assignment"},
        {"before_state": "assignment.absent"},
        {"after_state": "assignment.confirmed"},
        {"fact_code": "assignment.updated"},
        {"reason_code": "manual.assignment-confirmed"},
        {"event_type": "repair.unknown"},
        {"extra_key": ("phone", "13800000000")},
    ],
)
def test_revision_13_rejects_partial_tuple_swaps_and_sensitive_content(
    current_master_data_schema: PostgresRoles,
    change: dict[str, object],
) -> None:
    with current_master_data_schema.migration_connection() as connection:
        connection.execute("SET ROLE campus_ai_db_owner")
        actor = _seed_actor(connection)
        with pytest.raises(
            psycopg.errors.InvalidParameterValue,
            match="unsafe business audit",
        ):
            _append(connection, actor=actor, **change)  # type: ignore[arg-type]


@pytest.mark.destructive_migration
def test_revision_12_audit_allowlist_restores_current_head_after_downgrade(
    isolated_revision19_schema: PostgresRoles,
) -> None:
    """降级验证 revision 12 allowlist 后，必须恢复到仓库当前唯一 head。"""

    current_master_data_schema = isolated_revision19_schema

    _run_alembic(current_master_data_schema, "downgrade", "20260809_12")
    try:
        with current_master_data_schema.migration_connection() as connection:
            assert _function_acl(connection, _SAVED_AUDIT_SIGNATURE) == _FunctionAcl(
                exists=False,
                owner=None,
                security_definer=False,
                configuration=(),
                non_owner_execute_grantees=(),
            )
            assert _function_acl(connection, _PUBLIC_AUDIT_SIGNATURE) == _FunctionAcl(
                exists=True,
                owner="campus_ai_audit_definer",
                security_definer=True,
                configuration=("search_path=pg_catalog",),
                non_owner_execute_grantees=("campus_ai_db_owner", "campus_ai_runtime"),
            )
            connection.execute("SET ROLE campus_ai_db_owner")
            actor = _seed_actor(connection)
            _append_assignment(connection, actor)
            with pytest.raises(
                psycopg.errors.InvalidParameterValue,
                match="unsafe business audit",
            ):
                _append(connection, actor=actor)
    finally:
        _run_alembic(current_master_data_schema, "upgrade", "head")

    with current_master_data_schema.migration_connection() as connection:
        connection.execute("SET ROLE campus_ai_db_owner")
        restored_revision = connection.execute(
            "SELECT version_num FROM public.alembic_version"
        ).fetchone()
        assert restored_revision == (_current_alembic_head(),)
        assert _function_acl(connection, _SAVED_AUDIT_SIGNATURE) == _FunctionAcl(
            exists=True,
            owner="campus_ai_audit_definer",
            security_definer=True,
            configuration=("search_path=pg_catalog",),
            non_owner_execute_grantees=(),
        )
        connection.execute("SET ROLE campus_ai_db_owner")
        _append(connection, actor=actor)
