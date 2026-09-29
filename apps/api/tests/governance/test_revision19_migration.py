from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any, LiteralString

import psycopg
import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from psycopg import sql

from tests.master_data.conftest import PostgresRoles

API_ROOT = Path(__file__).resolve().parents[2]
REVISION_19_PATH = API_ROOT / "alembic/versions/20260823_19_governance_authority.py"
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
GOVERNANCE_RUNTIME_READ_TABLES = frozenset(
    {
        "governance_duties",
        "governance_duty_versions",
        "governance_scopes",
        "governance_scope_versions",
        "governance_grants",
        "governance_grant_versions",
        "governance_assignment_epochs",
    }
)
LEGACY_AUTHORIZATION_TABLES = frozenset(
    {"roles", "user_roles", "role_permissions", "responsibility_scopes"}
)
REPAIRS_RESPONSIBILITY_TABLES = frozenset(
    {
        "responsibility_types",
        "responsibility_groups",
        "responsibility_group_versions",
        "responsibility_group_memberships",
        "repair_responsibility_grants",
    }
)
SCHOOL_FACT_SENTINEL_TABLES = frozenset(
    {
        "schools",
        "school_people",
        "staff_assignments",
        "staff_assignment_versions",
        "account_person_bindings",
        "account_person_binding_versions",
        "organization_campus_relationships",
        "school_policy_scopes",
        "school_fact_sources",
        "school_fact_change_lineage",
    }
)


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
                "governance-migration-context-signing-key-32-bytes"
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


def _revision(postgres_roles: PostgresRoles) -> str:
    with postgres_roles.migration_connection() as connection:
        connection.execute("SET ROLE campus_ai_db_owner")
        row = connection.execute("SELECT version_num FROM public.alembic_version").fetchone()
    assert row is not None
    return str(row[0])


def _public_tables(postgres_roles: PostgresRoles) -> frozenset[str]:
    with postgres_roles.migration_connection() as connection:
        rows = connection.execute(
            """
            SELECT relation.relname
            FROM pg_catalog.pg_class AS relation
            JOIN pg_catalog.pg_namespace AS namespace
              ON namespace.oid = relation.relnamespace
            WHERE namespace.nspname = 'public'
              AND relation.relkind IN ('r', 'p')
            ORDER BY relation.relname
            """
        ).fetchall()
    return frozenset(str(row[0]) for row in rows)


def _catalog_rows(
    connection: psycopg.Connection[Any],
    statement: LiteralString,
    table_names: Sequence[str],
) -> tuple[tuple[object, ...], ...]:
    rows = connection.execute(statement, (list(table_names),)).fetchall()
    return tuple(tuple(row) for row in rows)


def _legacy_catalog_snapshot(
    postgres_roles: PostgresRoles,
    table_names: frozenset[str],
) -> dict[str, tuple[tuple[object, ...], ...]]:
    names = tuple(sorted(table_names))
    with postgres_roles.migration_connection() as connection:
        return {
            "relations": _catalog_rows(
                connection,
                """
                SELECT relation.relname,
                       owner.rolname,
                       relation.relrowsecurity,
                       relation.relforcerowsecurity,
                       relation.relreplident,
                       COALESCE(relation.relacl::text, '')
                FROM pg_catalog.pg_class AS relation
                JOIN pg_catalog.pg_namespace AS namespace
                  ON namespace.oid = relation.relnamespace
                JOIN pg_catalog.pg_roles AS owner ON owner.oid = relation.relowner
                WHERE namespace.nspname = 'public'
                  AND relation.relname = ANY(%s)
                ORDER BY relation.relname
                """,
                names,
            ),
            "columns": _catalog_rows(
                connection,
                """
                SELECT relation.relname,
                       attribute.attnum,
                       attribute.attname,
                       pg_catalog.format_type(attribute.atttypid, attribute.atttypmod),
                       attribute.attnotnull,
                       COALESCE(pg_catalog.pg_get_expr(default_value.adbin,
                                                       default_value.adrelid), ''),
                       attribute.attidentity,
                       attribute.attgenerated,
                       COALESCE(attribute.attacl::text, '')
                FROM pg_catalog.pg_class AS relation
                JOIN pg_catalog.pg_namespace AS namespace
                  ON namespace.oid = relation.relnamespace
                JOIN pg_catalog.pg_attribute AS attribute
                  ON attribute.attrelid = relation.oid
                 AND attribute.attnum > 0
                 AND NOT attribute.attisdropped
                LEFT JOIN pg_catalog.pg_attrdef AS default_value
                  ON default_value.adrelid = relation.oid
                 AND default_value.adnum = attribute.attnum
                WHERE namespace.nspname = 'public'
                  AND relation.relname = ANY(%s)
                ORDER BY relation.relname, attribute.attnum
                """,
                names,
            ),
            "constraints": _catalog_rows(
                connection,
                """
                SELECT relation.relname,
                       constraint_row.conname,
                       constraint_row.contype,
                       pg_catalog.pg_get_constraintdef(constraint_row.oid, true)
                FROM pg_catalog.pg_constraint AS constraint_row
                JOIN pg_catalog.pg_class AS relation
                  ON relation.oid = constraint_row.conrelid
                JOIN pg_catalog.pg_namespace AS namespace
                  ON namespace.oid = relation.relnamespace
                WHERE namespace.nspname = 'public'
                  AND relation.relname = ANY(%s)
                ORDER BY relation.relname, constraint_row.conname
                """,
                names,
            ),
            "indexes": _catalog_rows(
                connection,
                """
                SELECT tablename, indexname, indexdef
                FROM pg_catalog.pg_indexes
                WHERE schemaname = 'public'
                  AND tablename = ANY(%s)
                ORDER BY tablename, indexname
                """,
                names,
            ),
            "policies": _catalog_rows(
                connection,
                """
                SELECT tablename, policyname, permissive, roles, cmd,
                       COALESCE(qual, ''), COALESCE(with_check, '')
                FROM pg_catalog.pg_policies
                WHERE schemaname = 'public'
                  AND tablename = ANY(%s)
                ORDER BY tablename, policyname
                """,
                names,
            ),
            "triggers": _catalog_rows(
                connection,
                """
                SELECT relation.relname,
                       trigger_row.tgname,
                       pg_catalog.pg_get_triggerdef(trigger_row.oid, true)
                FROM pg_catalog.pg_trigger AS trigger_row
                JOIN pg_catalog.pg_class AS relation ON relation.oid = trigger_row.tgrelid
                JOIN pg_catalog.pg_namespace AS namespace
                  ON namespace.oid = relation.relnamespace
                WHERE namespace.nspname = 'public'
                  AND relation.relname = ANY(%s)
                  AND NOT trigger_row.tgisinternal
                ORDER BY relation.relname, trigger_row.tgname
                """,
                names,
            ),
        }


def _assert_governance_defaults_and_runtime_denials(
    postgres_roles: PostgresRoles,
) -> None:
    with postgres_roles.migration_connection() as connection:
        connection.execute("SET ROLE campus_ai_db_owner")
        counts: dict[str, int] = {}
        for table_name in GOVERNANCE_TABLES:
            row = connection.execute(
                sql.SQL("SELECT count(*) FROM public.{}").format(sql.Identifier(table_name))
            ).fetchone()
            assert row is not None
            counts[table_name] = int(row[0])
    assert counts == {table_name: 0 for table_name in GOVERNANCE_TABLES}

    with postgres_roles.runtime_connection() as connection:
        for table_name in sorted(GOVERNANCE_TABLES):
            privileges = connection.execute(
                """
                SELECT has_table_privilege(current_user, %s, 'SELECT'),
                       has_table_privilege(current_user, %s, 'INSERT'),
                       has_table_privilege(current_user, %s, 'UPDATE'),
                       has_table_privilege(current_user, %s, 'DELETE'),
                       has_table_privilege(current_user, %s, 'TRUNCATE')
                """,
                (f"public.{table_name}",) * 5,
            ).fetchone()
            assert privileges == (
                table_name in GOVERNANCE_RUNTIME_READ_TABLES,
                False,
                False,
                False,
                False,
            )
            if table_name in GOVERNANCE_RUNTIME_READ_TABLES:
                row = connection.execute(
                    sql.SQL("SELECT count(*) FROM public.{}").format(sql.Identifier(table_name))
                ).fetchone()
                assert row == (0,)
            else:
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    connection.execute(
                        sql.SQL("SELECT count(*) FROM public.{}").format(sql.Identifier(table_name))
                    )
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                connection.execute(
                    sql.SQL("INSERT INTO public.{} DEFAULT VALUES").format(
                        sql.Identifier(table_name)
                    )
                )


def test_revision19_is_the_unique_revision18_successor() -> None:
    assert REVISION_19_PATH.is_file(), REVISION_19_PATH
    source = REVISION_19_PATH.read_text(encoding="utf-8")
    assert f'revision = "{REVISION_19}"' in source
    assert f'down_revision = "{REVISION_18}"' in source

    configuration = Config(str(API_ROOT / "alembic.ini"))
    configuration.set_main_option("script_location", str(API_ROOT / "alembic"))
    scripts = ScriptDirectory.from_config(configuration)
    revision18_successors = sorted(
        revision.revision
        for revision in scripts.walk_revisions()
        if revision.down_revision == REVISION_18
    )
    assert revision18_successors == [REVISION_19]


@pytest.mark.integration
@pytest.mark.destructive_migration
def test_revision19_fresh_defaults_runtime_dml_and_round_trip_preserve_legacy_catalog(
    isolated_revision19_schema: PostgresRoles,
) -> None:
    assert _revision(isolated_revision19_schema) == REVISION_19
    head_tables = _public_tables(isolated_revision19_schema)
    assert GOVERNANCE_TABLES <= head_tables
    _assert_governance_defaults_and_runtime_denials(isolated_revision19_schema)

    downgrade = _run_alembic(isolated_revision19_schema, "downgrade", REVISION_18)
    assert downgrade.returncode == 0, downgrade.stdout + downgrade.stderr
    assert _revision(isolated_revision19_schema) == REVISION_18
    revision18_tables = _public_tables(isolated_revision19_schema)
    assert GOVERNANCE_TABLES.isdisjoint(revision18_tables)
    assert LEGACY_AUTHORIZATION_TABLES <= revision18_tables
    assert REPAIRS_RESPONSIBILITY_TABLES <= revision18_tables
    assert SCHOOL_FACT_SENTINEL_TABLES <= revision18_tables
    legacy_snapshot = _legacy_catalog_snapshot(
        isolated_revision19_schema,
        revision18_tables - {"alembic_version"},
    )

    upgrade = _run_alembic(isolated_revision19_schema, "upgrade", REVISION_19)
    assert upgrade.returncode == 0, upgrade.stdout + upgrade.stderr
    assert _revision(isolated_revision19_schema) == REVISION_19
    upgraded_tables = _public_tables(isolated_revision19_schema)
    assert upgraded_tables - revision18_tables == GOVERNANCE_TABLES
    assert (
        _legacy_catalog_snapshot(
            isolated_revision19_schema,
            revision18_tables - {"alembic_version"},
        )
        == legacy_snapshot
    )
    _assert_governance_defaults_and_runtime_denials(isolated_revision19_schema)

    second_downgrade = _run_alembic(isolated_revision19_schema, "downgrade", REVISION_18)
    assert second_downgrade.returncode == 0, second_downgrade.stdout + second_downgrade.stderr
    assert _revision(isolated_revision19_schema) == REVISION_18
    assert GOVERNANCE_TABLES.isdisjoint(_public_tables(isolated_revision19_schema))

    second_upgrade = _run_alembic(isolated_revision19_schema, "upgrade", REVISION_19)
    assert second_upgrade.returncode == 0, second_upgrade.stdout + second_upgrade.stderr
    assert _revision(isolated_revision19_schema) == REVISION_19
    assert (
        _legacy_catalog_snapshot(
            isolated_revision19_schema,
            revision18_tables - {"alembic_version"},
        )
        == legacy_snapshot
    )
    _assert_governance_defaults_and_runtime_denials(isolated_revision19_schema)
