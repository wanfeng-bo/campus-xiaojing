from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, LiteralString
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.types.json import Jsonb

from tests.master_data.conftest import PostgresRoles

API_ROOT = Path(__file__).resolve().parents[2]
REVISION_19 = "20260823_19"
REVISION_18 = "20260821_18"

GOVERNANCE_TABLES = frozenset(
    {
        "governance_duties",
        "governance_duty_versions",
        "governance_scopes",
        "governance_scope_versions",
        "governance_grants",
        "governance_grant_versions",
        "governance_assignment_epochs",
        "governance_bootstrap_states",
        "governance_command_receipts",
        "governance_audit_events",
    }
)
CRITICAL_COMPOSITE_FOREIGN_KEYS = frozenset(
    {
        "fk_governance_duty_versions_duty",
        "fk_governance_scope_versions_scope",
        "fk_governance_grants_assignment",
        "fk_governance_grants_duty",
        "fk_governance_grant_versions_duty_version",
        "fk_governance_grant_versions_grant_duty",
        "fk_governance_grant_versions_scope_version",
        "fk_governance_assignment_epochs_assignment",
        "fk_governance_command_receipts_actor_assignment",
        "fk_governance_audit_events_actor_assignment",
        "fk_governance_audit_events_command",
    }
)
CRITICAL_CHECKS = frozenset(
    {
        "ck_governance_duties_version_positive",
        "ck_governance_duty_versions_version_positive",
        "ck_governance_scopes_version_positive",
        "ck_governance_scope_versions_schema_version_positive",
        "ck_governance_scope_versions_version_positive",
        "ck_governance_grants_version_positive",
        "ck_governance_grant_versions_effective_order",
        "ck_governance_grant_versions_planned_interval",
        "ck_governance_grant_versions_positive_versions",
        "ck_governance_grant_versions_terminal_shape",
        "ck_governance_assignment_epochs_positive",
        "ck_governance_bootstrap_states_closed_shape",
        "ck_governance_bootstrap_states_closed_interval",
        "ck_governance_bootstrap_states_version_positive",
        "ck_governance_command_receipts_actor_assignment",
        "ck_governance_command_receipts_target",
        "ck_governance_audit_events_actor_assignment",
        "ck_governance_audit_events_target_version",
    }
)
EXPECTED_UNIQUE_CONSTRAINTS = frozenset(
    {
        "uq_governance_duties_school_duty_code",
        "uq_governance_duties_school_id_id",
        "uq_governance_duty_versions_target_version",
        "uq_governance_scopes_school_scope_key",
        "uq_governance_scopes_school_id_id",
        "uq_governance_scope_versions_target_version",
        "uq_governance_grants_school_id_duty",
        "uq_governance_grants_school_id_id",
        "uq_governance_grant_versions_target_version",
        "uq_governance_command_receipts_school_id_id",
        "uq_governance_command_receipts_school_key",
        "uq_governance_audit_events_command",
    }
)
EXPECTED_EXPLICIT_INDEXES = frozenset(
    {
        "ix_governance_duty_versions_as_of",
        "ix_governance_scope_versions_as_of",
        "ix_governance_scope_versions_schema",
        "ix_governance_grants_assignment",
        "ix_governance_grant_versions_as_of",
        "ix_governance_grant_versions_scope",
        "ix_governance_command_receipts_request",
        "ix_governance_audit_events_request",
        "ix_governance_audit_events_target",
    }
)
REQUIRED_INVARIANT_TRIGGERS = frozenset(
    {
        "trg_governance_grants_immutable_binding",
        "trg_governance_duty_versions_append_only",
        "trg_governance_scope_versions_append_only",
        "trg_governance_grant_versions_append_only",
        "trg_governance_bootstrap_states_guard",
        "trg_governance_audit_events_append_only",
    }
)


@dataclass(frozen=True, slots=True)
class GovernanceContractRows:
    school_id: UUID
    invalid_interval_school_id: UUID
    assignment_id: UUID
    replacement_assignment_id: UUID
    duty_id: UUID
    replacement_duty_id: UUID
    duty_version_id: UUID
    unreferenced_duty_version_id: UUID
    scope_id: UUID
    unreferenced_scope_id: UUID
    scope_version_id: UUID
    unreferenced_scope_version_id: UUID
    grant_id: UUID
    grant_version_id: UUID
    command_receipt_id: UUID
    audit_event_id: UUID


def _owner_connection(
    postgres_roles: PostgresRoles,
    *,
    autocommit: bool = True,
) -> psycopg.Connection[Any]:
    connection = psycopg.connect(
        **postgres_roles.migration_connection_parameters,
        autocommit=autocommit,
    )
    connection.execute("SET ROLE campus_ai_db_owner")
    return connection


def _migration_environment(postgres_roles: PostgresRoles) -> dict[str, str]:
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
                "governance-database-contract-signing-key-32-bytes"
            ),
        }
    )
    return environment


def _run_alembic(
    postgres_roles: PostgresRoles,
    *arguments: str,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "alembic", *arguments],
        cwd=API_ROOT,
        env=_migration_environment(postgres_roles),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=90,
        check=False,
    )


def _current_revision(postgres_roles: PostgresRoles) -> str:
    with postgres_roles.migration_connection() as connection:
        connection.execute("SET ROLE campus_ai_db_owner")
        row = connection.execute("SELECT version_num FROM public.alembic_version").fetchone()
    assert row is not None
    return str(row[0])


def _seed_governance_contract_rows(
    postgres_roles: PostgresRoles,
) -> GovernanceContractRows:
    rows = GovernanceContractRows(
        school_id=uuid4(),
        invalid_interval_school_id=uuid4(),
        assignment_id=uuid4(),
        replacement_assignment_id=uuid4(),
        duty_id=uuid4(),
        replacement_duty_id=uuid4(),
        duty_version_id=uuid4(),
        unreferenced_duty_version_id=uuid4(),
        scope_id=uuid4(),
        unreferenced_scope_id=uuid4(),
        scope_version_id=uuid4(),
        unreferenced_scope_version_id=uuid4(),
        grant_id=uuid4(),
        grant_version_id=uuid4(),
        command_receipt_id=uuid4(),
        audit_event_id=uuid4(),
    )
    person_id = uuid4()
    first_unit_id = uuid4()
    second_unit_id = uuid4()
    replacement_duty_version_id = uuid4()
    as_of = datetime(2026, 1, 1, tzinfo=UTC)

    with _owner_connection(postgres_roles) as connection:
        connection.cursor().executemany(
            """
            INSERT INTO public.schools (
                id, source_key, school_code, display_name, timezone_name, status, version
            ) VALUES (%s, %s, %s, %s, 'Asia/Shanghai', 'active', 1)
            """,
            (
                (
                    rows.school_id,
                    f"test:governance-contract:school:{rows.school_id}",
                    "TEST-GOV-PRIMARY",
                    "虚构治理数据库合同学校",
                ),
                (
                    rows.invalid_interval_school_id,
                    f"test:governance-contract:school:{rows.invalid_interval_school_id}",
                    "TEST-GOV-INTERVAL",
                    "虚构治理时间合同学校",
                ),
            ),
        )
        connection.execute(
            """
            INSERT INTO public.school_people (id, school_id, source_key, status, version)
            VALUES (%s, %s, %s, 'active', 1)
            """,
            (person_id, rows.school_id, f"test:governance-contract:person:{person_id}"),
        )
        connection.cursor().executemany(
            """
            INSERT INTO public.organization_units (
                id, school_id, source_key, parent_organization_unit_id, kind, status, version
            ) VALUES (%s, %s, %s, NULL, 'department', 'active', 1)
            """,
            (
                (
                    first_unit_id,
                    rows.school_id,
                    f"test:governance-contract:organization:{first_unit_id}",
                ),
                (
                    second_unit_id,
                    rows.school_id,
                    f"test:governance-contract:organization:{second_unit_id}",
                ),
            ),
        )
        connection.cursor().executemany(
            """
            INSERT INTO public.staff_assignments (
                id, school_id, source_key, person_id, organization_unit_id,
                position_title, semantic_role_code, assignment_kind,
                appointment_source, approval_reference, valid_from, valid_to,
                revoked_at, status, version
            ) VALUES (
                %s, %s, %s, %s, %s,
                '虚构数据库合同任职', %s, 'substantive',
                'test-only', %s, %s, NULL,
                NULL, 'confirmed', 1
            )
            """,
            (
                (
                    rows.assignment_id,
                    rows.school_id,
                    f"test:governance-contract:assignment:{rows.assignment_id}",
                    person_id,
                    first_unit_id,
                    "test_only_governance_contract_primary",
                    "test-only:governance-contract:assignment:primary",
                    as_of,
                ),
                (
                    rows.replacement_assignment_id,
                    rows.school_id,
                    f"test:governance-contract:assignment:{rows.replacement_assignment_id}",
                    person_id,
                    second_unit_id,
                    "test_only_governance_contract_replacement",
                    "test-only:governance-contract:assignment:replacement",
                    as_of,
                ),
            ),
        )
        connection.cursor().executemany(
            """
            INSERT INTO public.governance_duties (
                id, school_id, duty_code, current_version
            ) VALUES (%s, %s, %s, 1)
            """,
            (
                (rows.duty_id, rows.school_id, "test_only_database_contract_primary"),
                (
                    rows.replacement_duty_id,
                    rows.school_id,
                    "test_only_database_contract_replacement",
                ),
            ),
        )
        connection.cursor().executemany(
            """
            INSERT INTO public.governance_duty_versions (
                id, school_id, duty_id, display_name, description,
                is_active, effective_from, version
            ) VALUES (%s, %s, %s, %s, 'test-only', true, %s, 1)
            """,
            (
                (
                    rows.duty_version_id,
                    rows.school_id,
                    rows.duty_id,
                    "虚构数据库合同职责",
                    as_of,
                ),
                (
                    replacement_duty_version_id,
                    rows.school_id,
                    rows.replacement_duty_id,
                    "虚构替代数据库合同职责",
                    as_of,
                ),
            ),
        )
        connection.execute(
            """
            INSERT INTO public.governance_duty_versions (
                id, school_id, duty_id, display_name, description,
                is_active, effective_from, version
            ) VALUES (%s, %s, %s, '虚构未引用职责版本', 'test-only', true, %s, 2)
            """,
            (
                rows.unreferenced_duty_version_id,
                rows.school_id,
                rows.replacement_duty_id,
                as_of,
            ),
        )
        connection.cursor().executemany(
            """
            INSERT INTO public.governance_scopes (
                id, school_id, scope_key, current_version
            ) VALUES (%s, %s, %s, 1)
            """,
            (
                (rows.scope_id, rows.school_id, "test-only:database-contract:primary"),
                (
                    rows.unreferenced_scope_id,
                    rows.school_id,
                    "test-only:database-contract:unreferenced",
                ),
            ),
        )
        connection.cursor().executemany(
            """
            INSERT INTO public.governance_scope_versions (
                id, school_id, scope_id, schema_owner, schema_name, schema_version,
                reference_document, reference_sha256, is_active, effective_from, version
            ) VALUES (
                %s, %s, %s, 'test-only', 'database-contract', 1,
                %s, %s, true, %s, 1
            )
            """,
            (
                (
                    rows.scope_version_id,
                    rows.school_id,
                    rows.scope_id,
                    Jsonb({"test_only": "primary"}),
                    "a" * 64,
                    as_of,
                ),
                (
                    rows.unreferenced_scope_version_id,
                    rows.school_id,
                    rows.unreferenced_scope_id,
                    Jsonb({"test_only": "unreferenced"}),
                    "b" * 64,
                    as_of,
                ),
            ),
        )
        connection.execute(
            """
            INSERT INTO public.governance_grants (
                id, school_id, assignment_id, duty_id, current_version
            ) VALUES (%s, %s, %s, %s, 1)
            """,
            (rows.grant_id, rows.school_id, rows.assignment_id, rows.duty_id),
        )
        connection.execute(
            """
            INSERT INTO public.governance_grant_versions (
                id, school_id, grant_id, duty_id, duty_version,
                scope_id, scope_version, basis_authority, basis_reference,
                valid_from, effective_from, planned_valid_to,
                formal_ended_at, revoked_at, transition_kind, version
            ) VALUES (
                %s, %s, %s, %s, 1,
                %s, 1, 'test-only', 'test-only:database-contract:basis',
                %s, %s, %s,
                NULL, NULL, 'create', 1
            )
            """,
            (
                rows.grant_version_id,
                rows.school_id,
                rows.grant_id,
                rows.duty_id,
                rows.scope_id,
                as_of,
                as_of,
                datetime(2027, 1, 1, tzinfo=UTC),
            ),
        )
        connection.execute(
            """
            INSERT INTO public.governance_command_receipts (
                id, school_id, idempotency_key_hash, request_sha256,
                command_name, actor_kind, actor_reference,
                actor_assignment_id, actor_assignment_version, request_id,
                status, target_kind, target_id, target_version, result_payload
            ) VALUES (
                %s, %s, %s, %s,
                'test-only.database-contract', 'external_test_authority',
                'test-only:database-contract:actor', NULL, NULL, %s,
                'succeeded', 'governance_grant', %s, 1, %s
            )
            """,
            (
                rows.command_receipt_id,
                rows.school_id,
                "c" * 64,
                "d" * 64,
                uuid4(),
                rows.grant_id,
                Jsonb({"test_only": True}),
            ),
        )
        connection.execute(
            """
            INSERT INTO public.governance_audit_events (
                id, school_id, command_receipt_id, actor_kind, actor_reference,
                actor_assignment_id, actor_assignment_version, event_type,
                target_kind, target_id, target_version, before_summary,
                after_summary, request_id, reason, occurred_at
            ) VALUES (
                %s, %s, %s, 'external_test_authority',
                'test-only:database-contract:actor', NULL, NULL,
                'test-only.database-contract.created', 'governance_grant',
                %s, 1, %s, %s, %s, 'test-only database invariant proof', %s
            )
            """,
            (
                rows.audit_event_id,
                rows.school_id,
                rows.command_receipt_id,
                rows.grant_id,
                Jsonb({}),
                Jsonb({"test_only": True}),
                uuid4(),
                as_of,
            ),
        )
        connection.execute(
            """
            INSERT INTO public.governance_bootstrap_states (
                school_id, authority_reference, state, version, opened_at, closed_at
            ) VALUES (
                %s, 'test-only:database-contract:bootstrap',
                'closed', 2, %s, %s
            )
            """,
            (rows.school_id, as_of, datetime(2026, 1, 2, tzinfo=UTC)),
        )
    return rows


def _assert_owner_statement_rejected(
    postgres_roles: PostgresRoles,
    statement: LiteralString,
    parameters: Mapping[str, object],
    *,
    expected_sqlstate: str,
) -> None:
    connection = _owner_connection(postgres_roles, autocommit=False)
    try:
        try:
            connection.execute(statement, parameters)
        except psycopg.Error as exc:
            assert exc.sqlstate == expected_sqlstate
        else:
            pytest.fail(f"database accepted a forbidden Governance mutation: {statement}")
    finally:
        connection.rollback()
        connection.close()


@pytest.mark.integration
@pytest.mark.destructive_migration
def test_revision19_governance_catalog_freezes_only_approved_database_contract(
    isolated_revision19_schema: PostgresRoles,
) -> None:
    with isolated_revision19_schema.migration_connection() as connection:
        owners = dict(
            connection.execute(
                """
                SELECT relation.relname, owner.rolname
                FROM pg_catalog.pg_class AS relation
                JOIN pg_catalog.pg_namespace AS namespace
                  ON namespace.oid = relation.relnamespace
                JOIN pg_catalog.pg_roles AS owner ON owner.oid = relation.relowner
                WHERE namespace.nspname = 'public'
                  AND relation.relkind IN ('r', 'p')
                  AND relation.relname = ANY(%s)
                """,
                (list(GOVERNANCE_TABLES),),
            ).fetchall()
        )
        constraints = connection.execute(
            """
            SELECT constraint_row.conname, constraint_row.contype,
                   pg_catalog.pg_get_constraintdef(constraint_row.oid, true)
            FROM pg_catalog.pg_constraint AS constraint_row
            JOIN pg_catalog.pg_class AS relation
              ON relation.oid = constraint_row.conrelid
            JOIN pg_catalog.pg_namespace AS namespace
              ON namespace.oid = relation.relnamespace
            WHERE namespace.nspname = 'public'
              AND relation.relname = ANY(%s)
            """,
            (list(GOVERNANCE_TABLES),),
        ).fetchall()
        explicit_indexes = {
            str(row[0])
            for row in connection.execute(
                """
                SELECT indexname
                FROM pg_catalog.pg_indexes
                WHERE schemaname = 'public'
                  AND tablename = ANY(%s)
                  AND indexname LIKE 'ix_governance_%%'
                """,
                (list(GOVERNANCE_TABLES),),
            ).fetchall()
        }
        triggers = {
            str(row[0])
            for row in connection.execute(
                """
                SELECT trigger_row.tgname
                FROM pg_catalog.pg_trigger AS trigger_row
                JOIN pg_catalog.pg_class AS relation ON relation.oid = trigger_row.tgrelid
                JOIN pg_catalog.pg_namespace AS namespace
                  ON namespace.oid = relation.relnamespace
                WHERE namespace.nspname = 'public'
                  AND relation.relname = ANY(%s)
                  AND NOT trigger_row.tgisinternal
                """,
                (list(GOVERNANCE_TABLES),),
            ).fetchall()
        }

    assert owners == {table_name: "campus_ai_db_owner" for table_name in GOVERNANCE_TABLES}
    foreign_keys = {str(name) for name, kind, _definition in constraints if kind == "f"}
    checks = {str(name) for name, kind, _definition in constraints if kind == "c"}
    unique_constraints = {str(name) for name, kind, _definition in constraints if kind == "u"}
    assert CRITICAL_COMPOSITE_FOREIGN_KEYS <= foreign_keys
    assert CRITICAL_CHECKS <= checks
    assert unique_constraints == EXPECTED_UNIQUE_CONSTRAINTS
    assert explicit_indexes == EXPECTED_EXPLICIT_INDEXES
    assert REQUIRED_INVARIANT_TRIGGERS <= triggers


@pytest.mark.integration
@pytest.mark.destructive_migration
def test_revision19_owner_cannot_mutate_frozen_bindings_history_or_closed_bootstrap(
    isolated_revision19_schema: PostgresRoles,
) -> None:
    rows = _seed_governance_contract_rows(isolated_revision19_schema)
    invalid_opened_at = datetime(2026, 1, 3, tzinfo=UTC)
    invalid_closed_at = datetime(2026, 1, 2, tzinfo=UTC)

    attacks: tuple[tuple[LiteralString, Mapping[str, object], str], ...] = (
        (
            """
            INSERT INTO public.governance_bootstrap_states (
                school_id, authority_reference, state, version, opened_at, closed_at
            ) VALUES (
                %(school_id)s, 'test-only:invalid-interval',
                'closed', 1, %(opened_at)s, %(closed_at)s
            )
            """,
            {
                "school_id": rows.invalid_interval_school_id,
                "opened_at": invalid_opened_at,
                "closed_at": invalid_closed_at,
            },
            "23514",
        ),
        (
            "UPDATE public.governance_grants "
            "SET assignment_id = %(replacement_id)s WHERE id = %(target_id)s",
            {
                "replacement_id": rows.replacement_assignment_id,
                "target_id": rows.grant_id,
            },
            "55000",
        ),
        (
            "UPDATE public.governance_grants "
            "SET duty_id = %(replacement_id)s WHERE id = %(target_id)s",
            {"replacement_id": rows.replacement_duty_id, "target_id": rows.grant_id},
            "55000",
        ),
        (
            "UPDATE public.governance_duty_versions "
            "SET display_name = 'forbidden' WHERE id = %(target_id)s",
            {"target_id": rows.duty_version_id},
            "55000",
        ),
        (
            "DELETE FROM public.governance_duty_versions WHERE id = %(target_id)s",
            {"target_id": rows.unreferenced_duty_version_id},
            "55000",
        ),
        (
            "UPDATE public.governance_scope_versions "
            "SET reference_sha256 = %(replacement_hash)s WHERE id = %(target_id)s",
            {"replacement_hash": "e" * 64, "target_id": rows.scope_version_id},
            "55000",
        ),
        (
            "DELETE FROM public.governance_scope_versions WHERE id = %(target_id)s",
            {"target_id": rows.unreferenced_scope_version_id},
            "55000",
        ),
        (
            "UPDATE public.governance_grant_versions "
            "SET basis_reference = 'forbidden' WHERE id = %(target_id)s",
            {"target_id": rows.grant_version_id},
            "55000",
        ),
        (
            "DELETE FROM public.governance_grant_versions WHERE id = %(target_id)s",
            {"target_id": rows.grant_version_id},
            "55000",
        ),
        (
            "UPDATE public.governance_audit_events "
            "SET reason = 'forbidden' WHERE id = %(target_id)s",
            {"target_id": rows.audit_event_id},
            "55000",
        ),
        (
            "DELETE FROM public.governance_audit_events WHERE id = %(target_id)s",
            {"target_id": rows.audit_event_id},
            "55000",
        ),
        (
            "UPDATE public.governance_bootstrap_states "
            "SET state = 'open', closed_at = NULL, version = 3 "
            "WHERE school_id = %(school_id)s",
            {"school_id": rows.school_id},
            "55000",
        ),
        (
            "UPDATE public.governance_bootstrap_states "
            "SET authority_reference = 'test-only:replacement', version = 3 "
            "WHERE school_id = %(school_id)s",
            {"school_id": rows.school_id},
            "55000",
        ),
        (
            "UPDATE public.governance_bootstrap_states "
            "SET version = 4 WHERE school_id = %(school_id)s",
            {"school_id": rows.school_id},
            "55000",
        ),
    )
    for statement, parameters, expected_sqlstate in attacks:
        _assert_owner_statement_rejected(
            isolated_revision19_schema,
            statement,
            parameters,
            expected_sqlstate=expected_sqlstate,
        )


@pytest.mark.integration
@pytest.mark.destructive_migration
def test_revision19_nonempty_authority_refuses_downgrade_without_losing_data(
    isolated_revision19_schema: PostgresRoles,
) -> None:
    school_id = uuid4()
    opened_at = datetime(2026, 1, 1, tzinfo=UTC)
    with _owner_connection(isolated_revision19_schema) as connection:
        connection.execute(
            """
            INSERT INTO public.schools (
                id, source_key, school_code, display_name, timezone_name, status, version
            ) VALUES (
                %s, %s, 'TEST-GOV-DOWNGRADE',
                '虚构治理降级合同学校', 'Asia/Shanghai', 'active', 1
            )
            """,
            (school_id, f"test:governance-downgrade:school:{school_id}"),
        )
        connection.execute(
            """
            INSERT INTO public.governance_bootstrap_states (
                school_id, authority_reference, state, version, opened_at, closed_at
            ) VALUES (%s, 'test-only:governance-downgrade', 'open', 1, %s, NULL)
            """,
            (school_id, opened_at),
        )

    refused = _run_alembic(isolated_revision19_schema, "downgrade", REVISION_18)
    assert refused.returncode != 0, refused.stdout + refused.stderr
    assert "revision 19 refuses to discard Governance authority data" in (
        refused.stdout + refused.stderr
    )
    assert _current_revision(isolated_revision19_schema) == REVISION_19
    with _owner_connection(isolated_revision19_schema) as connection:
        row = connection.execute(
            """
            SELECT authority_reference, state, version, opened_at, closed_at
            FROM public.governance_bootstrap_states
            WHERE school_id = %s
            """,
            (school_id,),
        ).fetchone()
        assert row == (
            "test-only:governance-downgrade",
            "open",
            1,
            opened_at,
            None,
        )
        connection.execute(
            "DELETE FROM public.governance_bootstrap_states WHERE school_id = %s",
            (school_id,),
        )

    allowed = _run_alembic(isolated_revision19_schema, "downgrade", REVISION_18)
    assert allowed.returncode == 0, allowed.stdout + allowed.stderr
    assert _current_revision(isolated_revision19_schema) == REVISION_18
