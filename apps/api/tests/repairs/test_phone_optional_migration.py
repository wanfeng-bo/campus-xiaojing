from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import psycopg
import pytest
from sqlalchemy import CheckConstraint, Table

from app.domains.repairs.models import RepairTicket
from tests.master_data.conftest import PostgresRoles
from tests.repairs.test_schema_rls_acl import (
    _insert_unresolved_ticket,
    _seed_repair_parents,
)

API_ROOT = Path(__file__).resolve().parents[2]
_DOWNGRADE_BLOCKED_MESSAGE = "repair reporter phone contains NULL; downgrade is unsafe"


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
                "master-data-schema-context-signing-key-32-bytes"
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
        timeout=45,
        check=False,
    )


@pytest.fixture
def revision14_repair_schema(
    isolated_revision19_schema: PostgresRoles,
) -> PostgresRoles:
    prepared = _run_alembic(
        isolated_revision19_schema,
        "downgrade",
        "20260816_14",
    )
    assert prepared.returncode == 0, prepared.stdout + prepared.stderr
    return isolated_revision19_schema


def _constraint_definitions(
    connection: psycopg.Connection[Any],
) -> dict[str, str]:
    rows = connection.execute(
        """
        SELECT constraint_name, check_clause
        FROM information_schema.check_constraints
        WHERE constraint_schema = 'public'
          AND constraint_name IN (
              'ck_repair_tickets_required_text',
              'ck_repair_tickets_phone_nonblank'
          )
        ORDER BY constraint_name
        """
    ).fetchall()
    return {str(name): str(definition) for name, definition in rows}


def test_orm_phone_contract_matches_optional_database_shape() -> None:
    table = cast(Table, RepairTicket.__table__)
    phone = table.c.reporter_phone_snapshot
    checks = {
        constraint.name: str(constraint.sqltext)
        for constraint in table.constraints
        if isinstance(constraint, CheckConstraint) and constraint.name is not None
    }

    assert phone.nullable is True
    assert "reporter_phone_snapshot" not in checks["ck_repair_tickets_required_text"]
    assert "reporter_phone_snapshot IS NULL" in checks["ck_repair_tickets_phone_nonblank"]


def test_phone_column_and_checks_publish_explicit_nullable_semantics(
    current_master_data_schema: PostgresRoles,
) -> None:
    with current_master_data_schema.admin_connection() as connection:
        column = connection.execute(
            """
            SELECT is_nullable
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = 'repair_tickets'
              AND column_name = 'reporter_phone_snapshot'
            """
        ).fetchone()
        checks = _constraint_definitions(connection)

    assert column == ("YES",)
    assert "reporter_phone_snapshot" not in checks["ck_repair_tickets_required_text"]
    assert "reporter_phone_snapshot IS NULL" in checks["ck_repair_tickets_phone_nonblank"]


@pytest.mark.parametrize(
    ("overrides", "expected_error"),
    [
        ({"reporter_phone_snapshot": ""}, psycopg.errors.CheckViolation),
        ({"reporter_phone_snapshot": "   "}, psycopg.errors.CheckViolation),
        ({"reporter_name_snapshot": " "}, psycopg.errors.CheckViolation),
        ({"location_text": " "}, psycopg.errors.CheckViolation),
        ({"problem_description": " "}, psycopg.errors.CheckViolation),
    ],
)
def test_phone_and_required_text_truth_table(
    revision14_repair_schema: PostgresRoles,
    overrides: dict[str, object],
    expected_error: type[psycopg.errors.CheckViolation],
) -> None:
    with revision14_repair_schema.migration_connection() as connection:
        connection.execute("SET ROLE campus_ai_db_owner")
        facts = _seed_repair_parents(connection)

        with pytest.raises(expected_error):
            with connection.transaction():
                _insert_unresolved_ticket(connection, facts, **overrides)


def test_null_phone_is_accepted_by_real_database(
    revision14_repair_schema: PostgresRoles,
) -> None:
    with revision14_repair_schema.migration_connection() as connection:
        connection.execute("SET ROLE campus_ai_db_owner")
        facts = _seed_repair_parents(connection)
        _insert_unresolved_ticket(
            connection,
            facts,
            reporter_phone_snapshot=None,
        )

        stored = connection.execute(
            """
            SELECT reporter_phone_snapshot
            FROM repair_tickets
            WHERE school_id = %s AND reporter_person_id = %s
            """,
            (facts["school_id"], facts["person_id"]),
        ).fetchone()
        assert stored == (None,)


@pytest.mark.destructive_migration
def test_downgrade_rejects_existing_null_without_fabricating_phone(
    isolated_revision19_schema: PostgresRoles,
) -> None:
    current_master_data_schema = isolated_revision19_schema
    prepared = _run_alembic(
        current_master_data_schema,
        "downgrade",
        "20260816_14",
    )
    assert prepared.returncode == 0, prepared.stdout + prepared.stderr

    ticket_id = uuid4()
    with current_master_data_schema.migration_connection() as connection:
        connection.execute("SET ROLE campus_ai_db_owner")
        facts = _seed_repair_parents(connection)
        _insert_unresolved_ticket(
            connection,
            facts,
            id=ticket_id,
            reporter_phone_snapshot=None,
        )

    try:
        rejected = _run_alembic(
            current_master_data_schema,
            "downgrade",
            "20260815_13",
        )
        combined_output = rejected.stdout + rejected.stderr
        assert rejected.returncode != 0
        assert "NotNullViolation" in combined_output
        assert _DOWNGRADE_BLOCKED_MESSAGE in combined_output

        with current_master_data_schema.migration_connection() as connection:
            connection.execute("SET ROLE campus_ai_db_owner")
            revision = connection.execute(
                "SELECT version_num FROM public.alembic_version"
            ).fetchone()
            stored = connection.execute(
                """
                SELECT reporter_phone_snapshot
                FROM public.repair_tickets
                WHERE id = %s
                """,
                (ticket_id,),
            ).fetchone()

        assert revision == ("20260816_14",)
        assert stored == (None,)
    finally:
        with current_master_data_schema.migration_connection() as connection:
            connection.execute("SET ROLE campus_ai_db_owner")
            connection.execute(
                "DELETE FROM public.repair_tickets WHERE id = %s",
                (ticket_id,),
            )
