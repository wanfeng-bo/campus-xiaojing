from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.master_data.conftest import PostgresRoles

API_ROOT = Path(__file__).resolve().parents[2]
MIGRATION_PATH = API_ROOT / "alembic/versions/20260820_16_school_fact_change_sets.py"
PUBLISHED_RLS_MIGRATION_PATH = (
    API_ROOT / "alembic/versions/20260805_07_master_data_rls_and_grants.py"
)


def _run_alembic(
    roles: PostgresRoles,
    *arguments: str,
) -> subprocess.CompletedProcess[str]:
    migration = roles.migration_connection_parameters
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
    return subprocess.run(
        [sys.executable, "-m", "alembic", *arguments],
        cwd=API_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=90,
        check=False,
    )


def test_migration_16_declares_linear_sf2_revision_and_loss_guard() -> None:
    source = MIGRATION_PATH.read_text(encoding="utf-8")
    assert 'revision = "20260820_16"' in source
    assert 'down_revision = "20260819_15"' in source
    normalized_source = " ".join(source.replace("'", "").split())
    assert (
        "revision 16 contains school fact changes not representable by revision 15"
        in normalized_source
    )
    assert "school_fact_change_lineage" in source
    assert "ENABLE ROW LEVEL SECURITY" in source
    assert "FORCE ROW LEVEL SECURITY" in source


def test_sf2_permission_delta_lives_only_in_revision_16() -> None:
    published_source = PUBLISHED_RLS_MIGRATION_PATH.read_text(encoding="utf-8")
    sf2_source = MIGRATION_PATH.read_text(encoding="utf-8")
    normalized_published = " ".join(published_source.split())
    db_owner_signature = (
        "app.bind_execution_context(uuid,integer,uuid,text,uuid,bigint,uuid,bigint,"
        "bigint,uuid,text,text,text,timestamptz,text) TO campus_ai_db_owner"
    )

    assert db_owner_signature not in normalized_published
    assert "GRANT EXECUTE ON FUNCTION app.bind_execution_context" in sf2_source
    assert "REVOKE EXECUTE ON FUNCTION app.bind_execution_context" in sf2_source
    assert sf2_source.count("SET LOCAL ROLE campus_ai_context_definer") == 2
    assert sf2_source.count("SET LOCAL ROLE campus_ai_db_owner") == 2


@pytest.mark.integration
@pytest.mark.destructive_migration
def test_upgrade_15_to_16_builds_exact_sf2_ledger_extensions_and_is_drift_free(
    isolated_revision19_schema: PostgresRoles,
) -> None:
    current_master_data_schema = isolated_revision19_schema
    downgraded = _run_alembic(current_master_data_schema, "downgrade", "20260819_15")
    assert downgraded.returncode == 0, downgraded.stdout + downgraded.stderr
    upgraded = _run_alembic(current_master_data_schema, "upgrade", "20260820_16")
    assert upgraded.returncode == 0, upgraded.stdout + upgraded.stderr

    with current_master_data_schema.migration_connection() as connection:
        connection.execute("SET ROLE campus_ai_db_owner")
        columns = {
            row[0]
            for row in connection.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'master_data_import_batches'
                """
            ).fetchall()
        }
        constraints = {
            row[0]
            for row in connection.execute(
                """
                SELECT conname
                FROM pg_catalog.pg_constraint
                WHERE conrelid = 'public.master_data_import_batches'::regclass
                """
            ).fetchall()
        }
        policies = {
            row[0]
            for row in connection.execute(
                """
                SELECT policyname
                FROM pg_catalog.pg_policies
                WHERE schemaname = 'public'
                  AND tablename = 'school_fact_change_lineage'
                """
            ).fetchall()
        }

    assert {
        "plan_sha256",
        "base_fact_version",
        "target_fact_version",
        "confirmation_token_sha256",
        "confirmation_session_sha256",
        "result_payload",
    }.issubset(columns)
    assert {
        "fk_master_data_import_batches_staged_user",
        "fk_master_data_import_batches_staged_person",
        "fk_master_data_import_batches_staged_assignment",
    }.issubset(constraints)
    assert policies == {"owner_insert", "tenant_select"}

    upgraded_head = _run_alembic(current_master_data_schema, "upgrade", "head")
    assert upgraded_head.returncode == 0, upgraded_head.stdout + upgraded_head.stderr
    checked = _run_alembic(current_master_data_schema, "check")
    assert checked.returncode == 0, checked.stdout + checked.stderr
    assert "No new upgrade operations detected" in checked.stdout
