from __future__ import annotations

import importlib
import os
import re
import runpy
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from uuid import UUID, uuid4

import psycopg
import pytest
import yaml
from alembic.config import Config
from alembic.script import ScriptDirectory
from psycopg import sql
from pydantic import ValidationError
from sqlalchemy import URL, create_engine, text
from sqlalchemy.exc import OperationalError as SQLAlchemyOperationalError
from sqlalchemy.pool import QueuePool

from app.core.settings import Settings
from app.db.connection import RuntimeDatabaseStartupRejected, build_engine
from tests.master_data.conftest import (
    PostgresRoles,
    _derive_cluster_test_password,
    _migration_environment,
    _run_bootstrap,
    _verify_disposable_postgres_16_cluster,
)

PROJECT_ROOT = Path(__file__).resolve().parents[4]
API_ROOT = PROJECT_ROOT / "apps" / "api"
BOOTSTRAP_SCRIPT = PROJECT_ROOT / "infra" / "postgres" / "001-campus-ai-roles.sh"
MIGRATION_SETTINGS = PROJECT_ROOT / "apps" / "api" / "app" / "db" / "migration_settings.py"
ALEMBIC_ENV = PROJECT_ROOT / "apps" / "api" / "alembic" / "env.py"
REVISION20_MIGRATION = (
    PROJECT_ROOT
    / "apps"
    / "api"
    / "alembic"
    / "versions"
    / "20260824_20_school_database_boundary.py"
)
LOCAL_DATABASE_CONTROL_EXAMPLE = PROJECT_ROOT / ".env.local-database-control.example"
SCHOOL_FACT_MIGRATION_ENV_EXAMPLE = PROJECT_ROOT / ".env.school-fact-migration.example"
COMPOSE_FILE = PROJECT_ROOT / "docker-compose.yml"
INTERNAL_COMPOSE_FILE = PROJECT_ROOT / "infra" / "docker" / "compose.internal-verification.yml"
ENV_EXAMPLE = PROJECT_ROOT / ".env.example"
ROLE_FIXTURES = PROJECT_ROOT / "apps" / "api" / "tests" / "master_data" / "conftest.py"
ROOT_FIXTURES = PROJECT_ROOT / "apps" / "api" / "tests" / "conftest.py"

DATABASE_ROLE_PASSWORD_ENVIRONMENT_NAMES = (
    "POSTGRES_BOOTSTRAP_PASSWORD",
    "POSTGRES_RUNTIME_PASSWORD",
    "POSTGRES_MIGRATION_PASSWORD",
    "POSTGRES_SCHOOL_FACT_WRITER_PASSWORD",
    "POSTGRES_SCHOOL_FACT_READER_PASSWORD",
)
DATABASE_ROLE_PASSWORD_COLLISION_PAIRS = tuple(
    (first, second)
    for first_index, first in enumerate(DATABASE_ROLE_PASSWORD_ENVIRONMENT_NAMES)
    for second in DATABASE_ROLE_PASSWORD_ENVIRONMENT_NAMES[first_index + 1 :]
)
assert len(DATABASE_ROLE_PASSWORD_COLLISION_PAIRS) == 10


def read_project_file(path: Path) -> str:
    assert path.is_file(), f"预期文件尚不存在：{path}"
    return path.read_text(encoding="utf-8")


def test_disposable_cluster_role_passwords_converge_across_pytest_processes() -> None:
    bootstrap_password = "synthetic-bootstrap-secret"
    first = _derive_cluster_test_password(
        bootstrap_password=bootstrap_password,
        system_identifier="1111111111111111111",
        role_name="campus_ai_school_fact_writer",
    )

    assert first == _derive_cluster_test_password(
        bootstrap_password=bootstrap_password,
        system_identifier="1111111111111111111",
        role_name="campus_ai_school_fact_writer",
    )
    assert first != _derive_cluster_test_password(
        bootstrap_password=bootstrap_password,
        system_identifier="1111111111111111111",
        role_name="campus_ai_school_fact_reader",
    )
    assert first != _derive_cluster_test_password(
        bootstrap_password=bootstrap_password,
        system_identifier="2222222222222222222",
        role_name="campus_ai_school_fact_writer",
    )
    assert bootstrap_password not in first
    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", first)


def test_school_database_anchor_is_registered_in_exact_orm_metadata() -> None:
    from app.db.base import Base
    from app.platform.tenancy import models as tenancy_models

    anchor_model = tenancy_models.SchoolDatabaseAnchor
    table = Base.metadata.tables["school_database_anchor"]

    assert anchor_model.__table__ is table
    assert {
        column.name: (type(column.type).__name__, column.nullable) for column in table.columns
    } == {
        "singleton_id": ("SmallInteger", False),
        "anchor_id": ("Uuid", False),
        "school_id": ("Uuid", False),
        "cluster_system_identifier": ("String", False),
        "database_oid": ("BigInteger", False),
        "database_name": ("String", False),
        "provisioned_at": ("DateTime", False),
    }
    assert getattr(table.c.cluster_system_identifier.type, "length", None) == 32
    assert getattr(table.c.database_name.type, "length", None) == 63
    assert table.c.provisioned_at.server_default is not None
    assert {constraint.name for constraint in table.constraints if constraint.name} == {
        "ck_school_database_anchor_singleton",
        "fk_school_database_anchor_school",
        "uq_school_database_anchor_anchor_id",
        "uq_school_database_anchor_school_id",
    }
    assert tuple(column.name for column in table.primary_key.columns) == ("singleton_id",)
    assert not anchor_model.__mapper__.relationships


def test_revision20_tenant_table_set_exactly_matches_current_school_bearing_orm() -> None:
    from app.db.base import Base

    for module_name in (
        "app.domains.campus.models",
        "app.domains.repairs.models",
        "app.domains.students.models",
        "app.platform.audit.models",
        "app.platform.authorization.models",
        "app.platform.calendar.models",
        "app.platform.directory.models",
        "app.platform.governance.models",
        "app.platform.idempotency.models",
        "app.platform.identity.models",
        "app.platform.master_data_imports.models",
        "app.platform.messaging.models",
        "app.platform.organization.models",
        "app.platform.policies.models",
        "app.platform.responsibility.models",
        "app.platform.tenancy.models",
    ):
        importlib.import_module(module_name)

    migration = runpy.run_path(str(REVISION20_MIGRATION))
    revision20_tenant_tables = set(migration["_SF5B_APP_ACCESS_TABLES"]) | set(
        migration["_ANCHOR_GUARD_ONLY_TABLES"]
    )
    orm_school_bearing_tables = {
        table.name
        for table in Base.metadata.tables.values()
        if ("school_id" in table.c or table.name == "schools")
        and table.name != "school_database_anchor"
    }

    assert len(revision20_tenant_tables) == 59
    # Revision20 is frozen; the only added school-bearing table belongs to explicit F1 schema21.
    assert revision20_tenant_tables | {"school_fact_initializations"} == orm_school_bearing_tables
    assert "school_fact_initializations" not in revision20_tenant_tables


def test_revision20_identity_capabilities_are_fail_closed_until_reconcile() -> None:
    source = read_project_file(REVISION20_MIGRATION)
    identity = source[
        source.index("def _create_identity_capabilities(") : source.index(
            "def _install_tenant_rls("
        )
    ]

    assert "REVOKE ALL ON FUNCTION" in identity
    assert "GRANT EXECUTE ON FUNCTION" not in identity


def test_revision20_audit_definer_returning_policy_is_narrow_and_reconcilable() -> None:
    source = read_project_file(REVISION20_MIGRATION)

    assert '"sf5b_audit_definer_returning",' in source
    assert "DROP POLICY IF EXISTS {policy_name}" in source
    assert source.count("CREATE POLICY sf5b_audit_definer_returning") == 1
    assert "AS PERMISSIVE FOR SELECT TO campus_ai_audit_definer" in source
    assert (
        "WITH CHECK (school_id IS NULL OR "
        "school_id = app.require_school_database_anchor())" in source
    )
    assert "USING (school_id IS NULL OR school_id = app.require_school_database_anchor())" in source
    assert "GRANT SELECT ON TABLE public.audit_logs TO campus_ai_audit_definer" not in source


def test_revision20_atomically_provisions_only_the_first_migration_school_root() -> None:
    source = read_project_file(REVISION20_MIGRATION)

    assert "app.provision_first_school_database_anchor()" in source
    assert "AFTER INSERT ON public.schools" in source
    assert "SESSION_USER <> 'campus_ai_migration'" in source
    assert "pg_catalog.gen_random_uuid()" in source
    assert "PERFORM app.provision_school_database_anchor(" in source
    assert 'sa.PrimaryKeyConstraint("singleton_id")' in source
    assert "ON CONFLICT" not in source


def test_bootstrap_script_is_fail_closed_and_contains_no_password_literal() -> None:
    script = read_project_file(BOOTSTRAP_SCRIPT)

    assert "set -eu" in script
    assert "set -x" not in script
    assert "POSTGRES_RUNTIME_PASSWORD" in script
    assert "POSTGRES_MIGRATION_PASSWORD" in script
    assert "POSTGRES_SCHOOL_FACT_WRITER_PASSWORD" in script
    assert "POSTGRES_SCHOOL_FACT_READER_PASSWORD" in script
    assert "POSTGRES_EXPECTED_SYSTEM_IDENTIFIER" in script
    assert "POSTGRES_DB" in script
    assert "database.datname <> :'target_database'" in script
    assert "\\getenv runtime_password POSTGRES_RUNTIME_PASSWORD" in script
    assert "\\getenv migration_password POSTGRES_MIGRATION_PASSWORD" in script
    assert "\\getenv school_fact_writer_password POSTGRES_SCHOOL_FACT_WRITER_PASSWORD" in script
    assert "\\getenv school_fact_reader_password POSTGRES_SCHOOL_FACT_READER_PASSWORD" in script
    assert "--set=runtime_password" not in script
    assert "--set=migration_password" not in script
    assert "campus_ai_db_owner" in script
    assert "campus_ai_context_definer" in script
    assert "campus_ai_audit_definer" in script
    assert "campus_ai_outbox_probe_definer" in script
    assert "campus_ai_takeover_definer" in script
    assert "campus_ai_migration" in script
    assert "campus_ai_runtime" in script
    assert "campus_ai_school_fact_writer" in script
    assert "campus_ai_school_fact_reader" in script
    assert "campus_ai_identity_definer" in script
    assert script.index("database role passwords must be pairwise distinct") < script.index(
        "psql -X"
    )
    assert "Fixed Writer table allowlist" in script
    assert "Fixed Reader relation allowlist" in script
    assert "Fixed Identity definer column allowlist" in script
    assert "SET search_path = pg_catalog" in script
    assert "pg_catalog.pg_control_system()" in script
    assert script.index("pg_catalog.pg_control_system()") < script.index("BEGIN;")
    assert re.search(r"(?i)\bPASSWORD\s+'", script) is None
    assert "change-me" not in script
    assert "not-a-real-password" not in script
    assert "ENABLE_RLS" not in script
    assert "APP_ENV" not in script
    assert "campus_ai_master_test" not in script


@pytest.mark.integration
@pytest.mark.parametrize(
    ("first_password_name", "second_password_name"),
    DATABASE_ROLE_PASSWORD_COLLISION_PAIRS,
    ids=lambda pair: pair.removeprefix("POSTGRES_").lower(),
)
def test_bootstrap_rejects_every_database_password_reuse_before_psql(
    postgres_roles: PostgresRoles,
    first_password_name: str,
    second_password_name: str,
) -> None:
    collision_secret = (
        "sf5b-pairwise-collision-secret-"
        f"{first_password_name.lower()}-{second_password_name.lower()}"
    )
    environment = postgres_roles.bootstrap_environment.copy()
    environment[first_password_name] = collision_secret
    environment[second_password_name] = collision_secret
    postgres_roles.verify_disposable_cluster()

    try:
        result = subprocess.run(
            postgres_roles.bootstrap_command,
            cwd=PROJECT_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
            check=False,
        )
        output = result.stdout + result.stderr
        assert result.returncode != 0
        assert collision_secret not in output
        assert "database role passwords must be pairwise distinct" in output
    finally:
        postgres_roles.rerun_bootstrap()


@pytest.mark.integration
def test_returned_runtime_pool_connection_reauthenticates_after_bounded_recycle(
    postgres_roles: PostgresRoles,
) -> None:
    runtime = postgres_roles.runtime_connection_parameters
    runtime_url = URL.create(
        drivername="postgresql+psycopg",
        username=str(runtime["user"]),
        password=str(runtime["password"]),
        host=str(runtime["host"]),
        port=int(runtime["port"]),
        database=str(runtime["dbname"]),
    )
    engine = create_engine(
        runtime_url,
        pool_pre_ping=True,
        pool_reset_on_return="rollback",
        pool_recycle=1,
        pool_size=1,
        max_overflow=0,
        pool_timeout=1,
    )
    original_bootstrap_environment = postgres_roles.bootstrap_environment.copy()
    rotated_password = "sf5b-runtime-rotation-proof-secret-20260824"

    try:
        with engine.connect() as connection:
            first_backend_pid = connection.execute(text("SELECT pg_backend_pid()")).scalar_one()
        assert isinstance(first_backend_pid, int)
        assert isinstance(engine.pool, QueuePool)
        assert engine.pool.checkedout() == 0

        with postgres_roles.admin_connection() as connection:
            connection.execute(
                sql.SQL("ALTER ROLE campus_ai_runtime PASSWORD {}").format(
                    sql.Literal(rotated_password)
                )
            )

        time.sleep(1.1)
        with pytest.raises(SQLAlchemyOperationalError):
            with engine.connect() as connection:
                connection.execute(text("SELECT 1"))
    finally:
        engine.dispose()
        postgres_roles.bootstrap_environment.clear()
        postgres_roles.bootstrap_environment.update(original_bootstrap_environment)
        postgres_roles.rerun_bootstrap()

    with postgres_roles.runtime_connection() as connection:
        assert connection.execute("SELECT current_user").fetchone() == ("campus_ai_runtime",)


def test_runtime_settings_do_not_expose_migration_or_bootstrap_credentials() -> None:
    field_names = set(Settings.model_fields)

    assert not {name for name in field_names if "migration" in name}
    assert not {name for name in field_names if "bootstrap" in name}
    assert not {name for name in field_names if "admin_password" in name}


def test_migration_settings_are_isolated_and_hide_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    try:
        from app.db.migration_settings import MigrationSettings
    except ModuleNotFoundError:
        pytest.fail(f"独立迁移配置尚不存在：{MIGRATION_SETTINGS}")

    monkeypatch.setenv("POSTGRES_MIGRATION_HOST", "127.0.0.1")
    monkeypatch.setenv("POSTGRES_MIGRATION_PORT", "55432")
    monkeypatch.setenv("POSTGRES_MIGRATION_DB", "campus_ai_master_test")
    monkeypatch.setenv("POSTGRES_MIGRATION_USER", "campus_ai_migration")
    monkeypatch.setenv("POSTGRES_MIGRATION_PASSWORD", "migration-secret-not-for-logs")
    monkeypatch.setenv(
        "POSTGRES_MIGRATION_CONTEXT_SIGNING_KEY",
        "context-signing-key-at-least-32-bytes",
    )

    settings = MigrationSettings()  # pyright: ignore[reportCallIssue]

    assert settings.owner_role == "campus_ai_db_owner"
    assert settings.database_url.username == "campus_ai_migration"
    assert settings.database_url.database == "campus_ai_master_test"
    assert "migration-secret-not-for-logs" not in repr(settings)
    assert "migration-secret-not-for-logs" not in str(settings.database_url)


def test_migration_settings_reject_short_context_signing_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    try:
        from app.db.migration_settings import MigrationSettings
    except ModuleNotFoundError:
        pytest.fail(f"独立迁移配置尚不存在：{MIGRATION_SETTINGS}")

    monkeypatch.setenv("POSTGRES_MIGRATION_HOST", "127.0.0.1")
    monkeypatch.setenv("POSTGRES_MIGRATION_DB", "campus_ai_master_test")
    monkeypatch.setenv("POSTGRES_MIGRATION_USER", "campus_ai_migration")
    monkeypatch.setenv("POSTGRES_MIGRATION_PASSWORD", "migration-secret-not-for-logs")
    monkeypatch.setenv("POSTGRES_MIGRATION_CONTEXT_SIGNING_KEY", "x" * 31)

    rejected_secret = "x" * 31
    with pytest.raises(ValidationError) as error:
        MigrationSettings()  # pyright: ignore[reportCallIssue]

    assert rejected_secret not in str(error.value)


def test_alembic_uses_only_migration_settings_and_sets_local_owner_role() -> None:
    alembic_environment = read_project_file(ALEMBIC_ENV)

    assert "app.db.migration_settings" in alembic_environment
    assert "get_settings" not in alembic_environment
    assert "SET LOCAL ROLE campus_ai_db_owner" in alembic_environment
    assert "set_config" in alembic_environment
    assert ":context_signing_key" in alembic_environment
    assert "hide_parameters=True" in alembic_environment


def test_compose_separates_bootstrap_migration_and_runtime_credentials() -> None:
    compose = read_project_file(COMPOSE_FILE)
    environment_example = read_project_file(ENV_EXAMPLE)
    control_environment_example = read_project_file(LOCAL_DATABASE_CONTROL_EXAMPLE)
    migration_environment_example = read_project_file(SCHOOL_FACT_MIGRATION_ENV_EXAMPLE)

    assert "POSTGRES_USER: ${POSTGRES_BOOTSTRAP_USER}" in compose
    assert "POSTGRES_PASSWORD: ${POSTGRES_BOOTSTRAP_PASSWORD}" in compose
    assert "POSTGRES_RUNTIME_PASSWORD: ${POSTGRES_RUNTIME_PASSWORD}" in compose
    assert "POSTGRES_MIGRATION_PASSWORD: ${POSTGRES_MIGRATION_PASSWORD}" in compose
    assert (
        compose.count(
            "POSTGRES_SCHOOL_FACT_WRITER_PASSWORD: ${POSTGRES_SCHOOL_FACT_WRITER_PASSWORD}"
        )
        == 1
    )
    assert (
        compose.count(
            "POSTGRES_SCHOOL_FACT_READER_PASSWORD: ${POSTGRES_SCHOOL_FACT_READER_PASSWORD}"
        )
        == 1
    )
    assert "infra/postgres/001-campus-ai-roles.sh" in compose
    assert "/docker-entrypoint-initdb.d/001-campus-ai-roles.sh:ro" in compose
    assert 'pg_isready -U \\"$${POSTGRES_BOOTSTRAP_USER}\\"' in compose

    expected_runtime_environment_lines = {
        "POSTGRES_USER=campus_ai_runtime",
        "POSTGRES_PASSWORD=change-me-runtime-password",
        "RLS_CONTEXT_SIGNING_KEY=replace-with-the-same-32-byte-random-secret",
    }
    expected_control_environment_lines = {
        "POSTGRES_BOOTSTRAP_USER=postgres",
        "POSTGRES_BOOTSTRAP_PASSWORD=change-me-bootstrap-password",
        "POSTGRES_RUNTIME_PASSWORD=change-me-runtime-password",
        "POSTGRES_MIGRATION_PASSWORD=change-me-migration-password",
        "POSTGRES_SCHOOL_FACT_WRITER_PASSWORD=change-me-school-fact-writer-password",
        "POSTGRES_SCHOOL_FACT_READER_PASSWORD=change-me-school-fact-reader-password",
    }
    expected_migration_environment_lines = {
        "POSTGRES_MIGRATION_HOST=postgres",
        "POSTGRES_MIGRATION_PORT=5432",
        "POSTGRES_MIGRATION_DB=campus_ai_internal",
        "POSTGRES_MIGRATION_USER=campus_ai_migration",
        "POSTGRES_MIGRATION_PASSWORD=change-me-migration-password-0001",
        "POSTGRES_MIGRATION_CONTEXT_SIGNING_KEY=change-me-shared-rls-context-signing-key-0001",
    }
    assert expected_runtime_environment_lines <= set(environment_example.splitlines())
    assert expected_control_environment_lines <= set(control_environment_example.splitlines())
    assert expected_migration_environment_lines <= set(migration_environment_example.splitlines())
    assert "POSTGRES_BOOTSTRAP_PASSWORD" not in environment_example
    assert "POSTGRES_ADMIN_PASSWORD" not in environment_example


def test_internal_writer_executor_is_a_separate_hardened_private_service() -> None:
    parsed = yaml.safe_load(read_project_file(INTERNAL_COMPOSE_FILE))
    assert isinstance(parsed, dict)
    services = parsed.get("services")
    assert isinstance(services, dict)
    writer = services.get("school-fact-writer")
    api = services.get("api")
    assert isinstance(writer, dict)
    assert isinstance(api, dict)
    command = writer.get("command")
    assert command == [
        "uvicorn",
        "app.composition.school_fact_writer_executor:production_school_fact_writer_executor_app",
        "--factory",
        "--host",
        "0.0.0.0",
        "--port",
        "8010",
        "--no-access-log",
    ]
    assert writer.get("user") == "10002:10002"
    assert "ports" not in writer
    assert writer.get("networks") == ["school-fact-database", "school-fact-writer-ipc"]
    assert writer.get("env_file") == [
        "${CAMPUS_AI_SCHOOL_FACT_WRITER_ENV_FILE:-../../.env.school-fact-writer}"
    ]
    assert api.get("env_file") != writer.get("env_file")
    assert writer.get("read_only") is True
    assert writer.get("security_opt") == ["no-new-privileges:true"]
    assert writer.get("cap_drop") == ["ALL"]
    assert writer.get("tmpfs") == ["/tmp:rw,noexec,nosuid,size=16m"]
    assert writer.get("depends_on") == {
        "school-fact-role-reconcile": {"condition": "service_completed_successfully"}
    }
    writer_environment = writer.get("environment", {})
    api_environment = api.get("environment", {})
    assert isinstance(writer_environment, dict)
    assert isinstance(api_environment, dict)
    for forbidden in (
        "POSTGRES_MIGRATION_PASSWORD",
        "POSTGRES_BOOTSTRAP_PASSWORD",
        "SCHOOL_FACT_WRITER_DATABASE_PASSWORD",
    ):
        assert forbidden not in writer_environment
        assert forbidden not in api_environment
    assert api_environment.get("SCHOOL_FACT_EXECUTOR_BASE_URL") == (
        "http://school-fact-writer:8010"
    )


def test_postgres_fixtures_reconcile_roles_after_migrations_before_app_connections() -> None:
    fixtures = read_project_file(ROLE_FIXTURES)
    isolated = fixtures[
        fixtures.index("def isolated_migration_schema(") : fixtures.index(
            "def isolated_revision19_schema("
        )
    ]
    template = fixtures[
        fixtures.index("def current_master_data_template(") : fixtures.index(
            "def current_master_data_schema("
        )
    ]
    current = fixtures[
        fixtures.index("def current_master_data_schema(") : fixtures.index(
            "def current_owner_connection("
        )
    ]

    migration = isolated.index('"-m", "alembic", "upgrade", "20260824_20"')
    reconcile = isolated.rindex("_run_bootstrap(")
    bind_application = isolated.index("_bind_isolated_application_environment(")
    assert migration < reconcile < bind_application
    assert (
        template.index("_run_bootstrap(")
        < template.index('"-m", "alembic", "upgrade", schema_revision')
        < template.rindex("_run_bootstrap(")
    )
    assert "_bind_isolated_application_environment(" not in template
    assert "_run_bootstrap(" not in current


def test_current_schema_clones_one_reconciled_session_template_per_test() -> None:
    fixtures = read_project_file(ROLE_FIXTURES)
    root_fixtures = read_project_file(ROOT_FIXTURES)
    current = fixtures[
        fixtures.index("def current_master_data_schema(") : fixtures.index(
            "def current_owner_connection("
        )
    ]

    assert "CREATE DATABASE" in current and "TEMPLATE" in current
    assert '"-m", "alembic", "upgrade", "20260824_20"' not in current
    assert "_run_bootstrap(" not in current
    assert "_bind_isolated_application_environment(" in current
    assert "pg_terminate_backend" in current
    assert "DROP DATABASE" in current
    template_registration = root_fixtures[
        root_fixtures.index("current_master_data_template =") : root_fixtures.index(
            "isolated_migration_schema ="
        )
    ]
    assert 'scope="session"' in template_registration
    assert "current_master_data_schema = pytest.fixture(" in root_fixtures


@pytest.mark.integration
def test_isolated_revision19_schema_never_traverses_revision20(
    isolated_revision19_schema: PostgresRoles,
) -> None:
    with isolated_revision19_schema.admin_connection() as connection:
        revisions = connection.execute(
            "SELECT version_num FROM public.alembic_version ORDER BY version_num"
        ).fetchall()
    assert revisions == [("20260823_19",)]


@pytest.mark.integration
@pytest.mark.destructive_migration
def test_revision20_real_downgrade_fails_closed_without_restoring_runtime_importer_dml(
    isolated_migration_schema: PostgresRoles,
) -> None:
    roles = isolated_migration_schema
    migration = runpy.run_path(str(REVISION20_MIGRATION))
    writer_tables = tuple(str(table_name) for table_name in migration["_SCHOOL_FACT_WRITER_TABLES"])

    def runtime_dml_catalog() -> list[tuple[object, ...]]:
        with roles.admin_connection() as connection:
            return connection.execute(
                """
                SELECT protected.table_name,
                       has_table_privilege(
                           'campus_ai_runtime',
                           pg_catalog.to_regclass('public.' || protected.table_name),
                           'INSERT'
                       ),
                       has_table_privilege(
                           'campus_ai_runtime',
                           pg_catalog.to_regclass('public.' || protected.table_name),
                           'UPDATE'
                       ),
                       has_table_privilege(
                           'campus_ai_runtime',
                           pg_catalog.to_regclass('public.' || protected.table_name),
                           'DELETE'
                       )
                      , has_table_privilege(
                           'campus_ai_runtime',
                           pg_catalog.to_regclass('public.' || protected.table_name),
                           'TRUNCATE'
                       )
                FROM pg_catalog.unnest(%s::text[]) AS protected(table_name)
                ORDER BY protected.table_name
                """,
                (list(writer_tables),),
            ).fetchall()

    expected_zero_dml = {table_name: (False, False, False, False) for table_name in writer_tables}

    def runtime_dml_by_table() -> dict[object, tuple[object, ...]]:
        return {row[0]: tuple(row[1:]) for row in runtime_dml_catalog()}

    assert runtime_dml_by_table() == expected_zero_dml

    result = subprocess.run(
        [sys.executable, "-m", "alembic", "downgrade", "20260823_19"],
        cwd=API_ROOT,
        env=_migration_environment(roles),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    output = result.stdout + result.stderr

    assert result.returncode != 0
    assert "revision 20 is an irreversible security boundary" in output
    assert "dangerous runtime or importer privileges" in output
    with roles.admin_connection() as connection:
        revision = connection.execute("SELECT version_num FROM public.alembic_version").fetchone()
    assert revision == ("20260824_20",)
    assert runtime_dml_by_table() == expected_zero_dml


@pytest.mark.integration
def test_identity_functions_stay_fail_closed_until_post_migration_reconcile(
    isolated_revision19_schema: PostgresRoles,
) -> None:
    roles = isolated_revision19_schema
    upgrade = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "20260824_20"],
        cwd=API_ROOT,
        env=_migration_environment(roles),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert upgrade.returncode == 0, upgrade.stdout + upgrade.stderr

    def identity_catalog() -> list[tuple[object, ...]]:
        with roles.admin_connection() as connection:
            return connection.execute(
                """
                SELECT procedure.oid::regprocedure::text,
                       owner.rolname,
                       has_function_privilege('public', procedure.oid, 'EXECUTE'),
                       has_function_privilege('campus_ai_runtime', procedure.oid, 'EXECUTE'),
                       has_function_privilege(
                           'campus_ai_school_fact_writer', procedure.oid, 'EXECUTE'
                       ),
                       has_function_privilege(
                           'campus_ai_school_fact_reader', procedure.oid, 'EXECUTE'
                       )
                FROM pg_proc AS procedure
                JOIN pg_roles AS owner ON owner.oid = procedure.proowner
                WHERE procedure.oid IN (
                    'app.lock_existing_identity_user_id(text,text,text)'::regprocedure,
                    'app.claim_legacy_identity_user_id(text,text,text,text)'::regprocedure
                )
                ORDER BY procedure.oid::regprocedure::text
                """
            ).fetchall()

    assert identity_catalog() == [
        (
            "app.claim_legacy_identity_user_id(text,text,text,text)",
            "campus_ai_db_owner",
            False,
            False,
            False,
            False,
        ),
        (
            "app.lock_existing_identity_user_id(text,text,text)",
            "campus_ai_db_owner",
            False,
            False,
            False,
            False,
        ),
    ]

    roles.rerun_bootstrap()
    assert identity_catalog() == [
        (
            "app.claim_legacy_identity_user_id(text,text,text,text)",
            "campus_ai_identity_definer",
            False,
            True,
            False,
            False,
        ),
        (
            "app.lock_existing_identity_user_id(text,text,text)",
            "campus_ai_identity_definer",
            False,
            False,
            True,
            False,
        ),
    ]


@pytest.mark.integration
def test_revision20_upgrade_clears_runtime_and_public_independent_column_acl(
    isolated_revision19_schema: PostgresRoles,
) -> None:
    roles = isolated_revision19_schema
    migration = runpy.run_path(str(REVISION20_MIGRATION))
    retired_repair_tables = tuple(migration["_RETIRED_REPAIRS_RUNTIME_ACCESS_TABLES"])
    assert set(retired_repair_tables) == {
        "repair_responsibility_grants",
        "repair_scope_bindings",
        "repair_tickets",
        "responsibility_group_memberships",
        "responsibility_group_versions",
        "responsibility_groups",
        "responsibility_types",
    }
    runtime_revoke_tables = set(migration["_SCHOOL_FACT_RUNTIME_DML_REVOKE_TABLES"])
    shared_support_tables = set(migration["_SHARED_SUPPORT_TABLES"])
    protected_tables = sorted(
        (runtime_revoke_tables - shared_support_tables) | set(retired_repair_tables)
    )

    with roles.admin_connection() as connection:
        connection.execute("GRANT UPDATE (status) ON public.school_people TO campus_ai_runtime")
        connection.execute("GRANT SELECT (status) ON public.school_people TO PUBLIC")
        connection.execute("GRANT UPDATE (id) ON public.responsibility_types TO campus_ai_runtime")
        connection.execute("GRANT SELECT (id) ON public.responsibility_types TO PUBLIC")

    upgrade = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "20260824_20"],
        cwd=API_ROOT,
        env=_migration_environment(roles),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert upgrade.returncode == 0, upgrade.stdout + upgrade.stderr

    with roles.admin_connection() as connection:
        remaining_column_acl = connection.execute(
            """
            SELECT COALESCE(grantee.rolname, 'PUBLIC'),
                   relation.relname, attribute.attname, acl.privilege_type
            FROM pg_class AS relation
            JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
            JOIN pg_attribute AS attribute ON attribute.attrelid = relation.oid
            CROSS JOIN LATERAL aclexplode(attribute.attacl) AS acl
            LEFT JOIN pg_roles AS grantee ON grantee.oid = acl.grantee
            WHERE namespace.nspname = 'public'
              AND relation.relname = ANY(%s)
              AND attribute.attnum > 0
              AND NOT attribute.attisdropped
              AND (acl.grantee = 0 OR grantee.rolname = 'campus_ai_runtime')
            ORDER BY 1, 2, 3, 4
            """,
            (protected_tables,),
        ).fetchall()
        retired_policy_rows = connection.execute(
            """
            SELECT tablename, policyname, cmd
            FROM pg_policies
            WHERE schemaname = 'public'
              AND tablename = ANY(%s)
            ORDER BY tablename, policyname
            """,
            (list(retired_repair_tables),),
        ).fetchall()
    assert remaining_column_acl == []
    assert retired_policy_rows == [
        (table_name, "sf5b_anchor_guard", "ALL") for table_name in retired_repair_tables
    ]


@pytest.mark.integration
def test_role_tests_run_only_on_verified_disposable_postgres_16_cluster(
    postgres_roles: PostgresRoles,
) -> None:
    assert postgres_roles.disposable_cluster_system_identifier.isdecimal()
    assert 160_000 <= postgres_roles.server_version_num < 170_000


@pytest.mark.integration
def test_bootstrap_accepts_its_non_test_target_database(
    postgres_roles: PostgresRoles,
) -> None:
    target_database = f"sf5b_bootstrap_target_{uuid4().hex[:12]}"
    environment = postgres_roles.bootstrap_environment.copy()
    environment["POSTGRES_DB"] = target_database
    environment["PGDATABASE"] = target_database

    with postgres_roles.admin_connection() as connection:
        connection.execute(
            sql.SQL("CREATE DATABASE {} WITH OWNER {}").format(
                sql.Identifier(target_database),
                sql.Identifier(str(postgres_roles.admin_connection_parameters["user"])),
            )
        )

    try:
        result = _run_bootstrap(
            command=postgres_roles.bootstrap_command,
            environment=environment,
            protected_secrets=postgres_roles.protected_test_secrets,
        )
        assert result.returncode == 0
    finally:
        with postgres_roles.admin_connection() as connection:
            connection.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                    sql.Identifier(target_database)
                )
            )


@pytest.mark.integration
def test_disposable_cluster_system_identifier_mismatch_is_rejected(
    postgres_roles: PostgresRoles,
) -> None:
    with pytest.raises(pytest.fail.Exception, match="system_identifier 不匹配"):
        _verify_disposable_postgres_16_cluster(
            admin_connection_parameters=postgres_roles.admin_connection_parameters,
            expected_system_identifier="9999999999999999999",
        )


@pytest.mark.integration
def test_every_bootstrap_revalidates_the_cluster_system_identifier(
    postgres_roles: PostgresRoles,
) -> None:
    original_identifier = postgres_roles.disposable_cluster_system_identifier
    postgres_roles.disposable_cluster_system_identifier = "9999999999999999999"
    try:
        with pytest.raises(pytest.fail.Exception, match="system_identifier 不匹配"):
            postgres_roles.rerun_bootstrap()
        with pytest.raises(pytest.fail.Exception, match="system_identifier 不匹配"):
            postgres_roles.run_bootstrap_with_passwords(
                runtime_password="must-not-be-applied-runtime-password",
                migration_password="must-not-be-applied-migration-password",
            )
    finally:
        postgres_roles.disposable_cluster_system_identifier = original_identifier

    with postgres_roles.runtime_connection() as connection:
        assert connection.execute("SELECT current_user").fetchone() == ("campus_ai_runtime",)


@pytest.mark.integration
def test_bootstrap_rechecks_system_identifier_in_the_same_psql_session(
    postgres_roles: PostgresRoles,
) -> None:
    environment_name = "POSTGRES_EXPECTED_SYSTEM_IDENTIFIER"
    original_identifier = postgres_roles.bootstrap_environment[environment_name]
    postgres_roles.bootstrap_environment[environment_name] = "9999999999999999999"
    try:
        with pytest.raises(pytest.fail.Exception, match="bootstrap 脚本失败"):
            postgres_roles.rerun_bootstrap()
    finally:
        postgres_roles.bootstrap_environment[environment_name] = original_identifier

    with postgres_roles.runtime_connection() as connection:
        assert connection.execute("SELECT current_user").fetchone() == ("campus_ai_runtime",)


@pytest.mark.integration
def test_cluster_guards_ignore_bootstrap_role_search_path_poisoning(
    postgres_roles: PostgresRoles,
) -> None:
    attacker_schema = "task1_attacker_catalog"
    fake_system_identifier = "8888888888888888888"
    bootstrap_role = str(postgres_roles.admin_connection_parameters["user"])
    environment_name = "POSTGRES_EXPECTED_SYSTEM_IDENTIFIER"
    original_identifier = postgres_roles.disposable_cluster_system_identifier
    original_environment_identifier = postgres_roles.bootstrap_environment[environment_name]

    try:
        with postgres_roles.admin_connection() as connection:
            connection.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(attacker_schema))
            )
            connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(attacker_schema)))
            connection.execute(
                sql.SQL(
                    """
                    CREATE FUNCTION {}.pg_control_system(
                        OUT pg_control_version integer,
                        OUT catalog_version_no integer,
                        OUT system_identifier bigint,
                        OUT pg_control_last_modified timestamp with time zone
                    ) RETURNS record
                    LANGUAGE sql
                    AS $function$
                    SELECT 0::integer, 0::integer, {}::bigint, pg_catalog.now()
                    $function$
                    """
                ).format(
                    sql.Identifier(attacker_schema),
                    sql.Literal(fake_system_identifier),
                )
            )
            connection.execute(
                sql.SQL("ALTER ROLE {} SET search_path TO {}, pg_catalog").format(
                    sql.Identifier(bootstrap_role),
                    sql.Identifier(attacker_schema),
                )
            )

        postgres_roles.disposable_cluster_system_identifier = fake_system_identifier
        with pytest.raises(pytest.fail.Exception, match="system_identifier 不匹配"):
            postgres_roles.verify_disposable_cluster()
        postgres_roles.disposable_cluster_system_identifier = original_identifier

        postgres_roles.bootstrap_environment[environment_name] = fake_system_identifier
        with pytest.raises(pytest.fail.Exception, match="bootstrap 脚本失败"):
            postgres_roles.rerun_bootstrap()
        postgres_roles.bootstrap_environment[environment_name] = original_environment_identifier

        postgres_roles.rerun_bootstrap()
    finally:
        postgres_roles.disposable_cluster_system_identifier = original_identifier
        postgres_roles.bootstrap_environment[environment_name] = original_environment_identifier
        with postgres_roles.admin_connection() as connection:
            connection.execute(
                sql.SQL("ALTER ROLE {} RESET search_path").format(sql.Identifier(bootstrap_role))
            )
            connection.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(attacker_schema))
            )


@pytest.mark.integration
def test_runtime_is_not_owner_superuser_member_or_ddl_capable(
    postgres_roles: PostgresRoles,
) -> None:
    runtime = postgres_roles.role("campus_ai_runtime")

    assert runtime.can_login is True
    assert runtime.is_superuser is False
    assert runtime.can_create_db is False
    assert runtime.can_create_role is False
    assert runtime.can_replicate is False
    assert runtime.bypass_rls is False
    assert runtime.inherits is False
    assert runtime.member_of == frozenset()

    with postgres_roles.runtime_connection() as connection:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            connection.execute("CREATE TABLE forbidden_runtime_ddl(id integer)")


@pytest.mark.integration
def test_owner_and_definers_are_nologin_minimum_roles(
    postgres_roles: PostgresRoles,
) -> None:
    for role_name in (
        "campus_ai_db_owner",
        "campus_ai_context_definer",
        "campus_ai_audit_definer",
        "campus_ai_outbox_probe_definer",
        "campus_ai_takeover_definer",
    ):
        role = postgres_roles.role(role_name)
        assert role.can_login is False
        assert role.is_superuser is False
        assert role.can_create_db is False
        assert role.can_create_role is False
        assert role.can_replicate is False
        assert role.bypass_rls is False
        assert role.inherits is False
        assert role.member_of == frozenset()

    assert postgres_roles.owned_application_relations("campus_ai_context_definer") == 0
    assert postgres_roles.owned_application_relations("campus_ai_audit_definer") == 0
    assert postgres_roles.owned_application_relations("campus_ai_outbox_probe_definer") == 0
    assert postgres_roles.owned_application_relations("campus_ai_takeover_definer") == 0


@pytest.mark.integration
def test_takeover_definer_is_not_an_ordinary_runtime_writer_bypass(
    current_master_data_schema: PostgresRoles,
) -> None:
    postgres_roles = current_master_data_schema
    takeover_definer = postgres_roles.role("campus_ai_takeover_definer")
    assert takeover_definer == takeover_definer.__class__(
        can_login=False,
        is_superuser=False,
        can_create_db=False,
        can_create_role=False,
        can_replicate=False,
        bypass_rls=False,
        inherits=False,
        member_of=frozenset(),
    )
    assert postgres_roles.owned_application_relations("campus_ai_takeover_definer") == 0

    with postgres_roles.admin_connection() as connection:
        function_rows = connection.execute(
            """
            SELECT procedure.proname,
                   owner.rolname,
                   procedure.prosecdef,
                   procedure.proconfig,
                   has_function_privilege(
                       'campus_ai_runtime', procedure.oid, 'EXECUTE'
                   ),
                   has_function_privilege('public', procedure.oid, 'EXECUTE')
            FROM pg_proc AS procedure
            JOIN pg_namespace AS namespace ON namespace.oid = procedure.pronamespace
            JOIN pg_roles AS owner ON owner.oid = procedure.proowner
            WHERE namespace.nspname = 'app'
              AND procedure.proname IN (
                  'lock_master_data_import_takeover_source',
                  'lock_master_data_import_takeover_assignment',
                  'supersede_master_data_import_takeover_source'
              )
            ORDER BY procedure.proname
            """
        ).fetchall()
        table_acl_rows = connection.execute(
            """
            SELECT table_name, privilege_type
            FROM information_schema.role_table_grants
            WHERE grantee = 'campus_ai_takeover_definer'
            ORDER BY table_name, privilege_type
            """
        ).fetchall()
        column_acl_rows = connection.execute(
            """
            SELECT table_name, column_name, privilege_type
            FROM information_schema.column_privileges
            WHERE grantee = 'campus_ai_takeover_definer'
            ORDER BY table_name, column_name, privilege_type
            """
        ).fetchall()
        policy_rows = connection.execute(
            """
            SELECT tablename, policyname, cmd, roles
            FROM pg_policies
            WHERE policyname LIKE 'takeover_definer_%%'
            ORDER BY tablename, policyname
            """
        ).fetchall()
        takeover_audit_execute = connection.execute(
            """
            SELECT has_function_privilege(
                'campus_ai_takeover_definer',
                'app.append_master_data_import_audit(integer,uuid,uuid,uuid,bigint,text,text,text,uuid,uuid,uuid,jsonb,jsonb,uuid,text)',
                'EXECUTE'
            ),
            has_function_privilege(
                'public',
                'app.append_master_data_import_audit(integer,uuid,uuid,uuid,bigint,text,text,text,uuid,uuid,uuid,jsonb,jsonb,uuid,text)',
                'EXECUTE'
            )
            """
        ).fetchone()

    assert function_rows == [
        (
            "lock_master_data_import_takeover_assignment",
            "campus_ai_takeover_definer",
            True,
            ["search_path=pg_catalog"],
            False,
            False,
        ),
        (
            "lock_master_data_import_takeover_source",
            "campus_ai_takeover_definer",
            True,
            ["search_path=pg_catalog"],
            False,
            False,
        ),
        (
            "supersede_master_data_import_takeover_source",
            "campus_ai_takeover_definer",
            True,
            ["search_path=pg_catalog"],
            False,
            False,
        ),
    ]
    assert table_acl_rows == []
    assert column_acl_rows == [
        ("audit_logs", "actor_assignment_id", "SELECT"),
        ("audit_logs", "actor_assignment_version", "SELECT"),
        ("audit_logs", "actor_context_type", "SELECT"),
        ("audit_logs", "actor_person_id", "SELECT"),
        ("audit_logs", "actor_user_id", "SELECT"),
        ("audit_logs", "event_type", "SELECT"),
        ("audit_logs", "id", "SELECT"),
        ("audit_logs", "idempotency_key_hash", "SELECT"),
        ("audit_logs", "reason", "SELECT"),
        ("audit_logs", "request_id", "SELECT"),
        ("audit_logs", "school_id", "SELECT"),
        ("audit_logs", "target_id", "SELECT"),
        ("audit_logs", "target_type", "SELECT"),
        ("command_idempotency_records", "action_code", "SELECT"),
        ("command_idempotency_records", "actor_assignment_id", "SELECT"),
        ("command_idempotency_records", "actor_assignment_version", "SELECT"),
        ("command_idempotency_records", "actor_context_type", "SELECT"),
        ("command_idempotency_records", "actor_person_id", "SELECT"),
        ("command_idempotency_records", "actor_user_id", "SELECT"),
        ("command_idempotency_records", "created_at", "SELECT"),
        ("command_idempotency_records", "id", "SELECT"),
        ("command_idempotency_records", "idempotency_key_hash", "SELECT"),
        ("command_idempotency_records", "request_sha256", "SELECT"),
        ("command_idempotency_records", "resource_id", "SELECT"),
        ("command_idempotency_records", "resource_type", "SELECT"),
        ("command_idempotency_records", "result_payload", "SELECT"),
        ("command_idempotency_records", "result_payload", "UPDATE"),
        ("command_idempotency_records", "result_type", "SELECT"),
        ("command_idempotency_records", "result_type", "UPDATE"),
        ("command_idempotency_records", "school_id", "SELECT"),
        ("command_idempotency_records", "status", "SELECT"),
        ("command_idempotency_records", "status", "UPDATE"),
        ("command_idempotency_records", "updated_at", "SELECT"),
        ("command_idempotency_records", "updated_at", "UPDATE"),
        ("master_data_import_batches", "active_action_code", "SELECT"),
        ("master_data_import_batches", "active_idempotency_record_id", "SELECT"),
        ("master_data_import_batches", "id", "SELECT"),
        ("master_data_import_batches", "mode", "SELECT"),
        ("master_data_import_batches", "planned_units", "SELECT"),
        ("master_data_import_batches", "school_id", "SELECT"),
        ("master_data_import_batches", "source_kind", "SELECT"),
        ("master_data_import_batches", "status", "SELECT"),
        ("master_data_import_batches", "version", "SELECT"),
        ("master_data_import_command_outcomes", "action_code", "SELECT"),
        ("master_data_import_command_outcomes", "batch_id", "SELECT"),
        ("master_data_import_command_outcomes", "batch_version", "SELECT"),
        ("master_data_import_command_outcomes", "executed_units", "SELECT"),
        ("master_data_import_command_outcomes", "failed_units", "SELECT"),
        (
            "master_data_import_command_outcomes",
            "idempotency_record_id",
            "SELECT",
        ),
        ("master_data_import_command_outcomes", "school_id", "SELECT"),
        ("master_data_import_command_outcomes", "status", "SELECT"),
        ("master_data_import_command_outcomes", "succeeded_units", "SELECT"),
        ("master_data_import_command_outcomes", "unit_outcome_snapshots", "SELECT"),
        ("master_data_import_takeovers", "batch_id", "SELECT"),
        ("master_data_import_takeovers", "batch_version_after", "SELECT"),
        ("master_data_import_takeovers", "batch_version_before", "SELECT"),
        ("master_data_import_takeovers", "id", "SELECT"),
        ("master_data_import_takeovers", "new_actor_assignment_id", "SELECT"),
        ("master_data_import_takeovers", "new_actor_assignment_version", "SELECT"),
        ("master_data_import_takeovers", "new_actor_person_id", "SELECT"),
        ("master_data_import_takeovers", "new_actor_user_id", "SELECT"),
        ("master_data_import_takeovers", "new_idempotency_record_id", "SELECT"),
        ("master_data_import_takeovers", "original_actor_assignment_id", "SELECT"),
        ("master_data_import_takeovers", "original_actor_assignment_version", "SELECT"),
        ("master_data_import_takeovers", "original_actor_person_id", "SELECT"),
        ("master_data_import_takeovers", "original_actor_user_id", "SELECT"),
        ("master_data_import_takeovers", "previous_idempotency_record_id", "SELECT"),
        ("master_data_import_takeovers", "request_id", "SELECT"),
        ("master_data_import_takeovers", "school_id", "SELECT"),
        ("master_data_import_takeovers", "transferred_sequences", "SELECT"),
        ("master_data_import_takeovers", "transferred_unit_versions", "SELECT"),
        ("master_data_import_units", "batch_id", "SELECT"),
        ("master_data_import_units", "error_code", "SELECT"),
        ("master_data_import_units", "error_field_code", "SELECT"),
        ("master_data_import_units", "id", "SELECT"),
        ("master_data_import_units", "last_attempt_completed", "SELECT"),
        ("master_data_import_units", "last_attempt_idempotency_record_id", "SELECT"),
        ("master_data_import_units", "school_id", "SELECT"),
        ("master_data_import_units", "sequence", "SELECT"),
        ("master_data_import_units", "status", "SELECT"),
        ("master_data_import_units", "target_id", "SELECT"),
        ("master_data_import_units", "target_version", "SELECT"),
        ("master_data_import_units", "version", "SELECT"),
        ("staff_assignments", "id", "SELECT"),
        ("staff_assignments", "id", "UPDATE"),
        ("staff_assignments", "person_id", "SELECT"),
        ("staff_assignments", "school_id", "SELECT"),
        ("staff_assignments", "semantic_role_code", "SELECT"),
        ("staff_assignments", "status", "SELECT"),
        ("staff_assignments", "valid_from", "SELECT"),
        ("staff_assignments", "valid_to", "SELECT"),
        ("staff_assignments", "version", "SELECT"),
    ]
    assert policy_rows == [
        (
            "audit_logs",
            "takeover_definer_tenant_select",
            "SELECT",
            ["campus_ai_takeover_definer"],
        ),
        (
            "command_idempotency_records",
            "takeover_definer_tenant_select",
            "SELECT",
            ["campus_ai_takeover_definer"],
        ),
        (
            "command_idempotency_records",
            "takeover_definer_tenant_update",
            "UPDATE",
            ["campus_ai_takeover_definer"],
        ),
        (
            "master_data_import_batches",
            "takeover_definer_tenant_select",
            "SELECT",
            ["campus_ai_takeover_definer"],
        ),
        (
            "master_data_import_takeovers",
            "takeover_definer_tenant_select",
            "SELECT",
            ["campus_ai_takeover_definer"],
        ),
        (
            "master_data_import_units",
            "takeover_definer_tenant_select",
            "SELECT",
            ["campus_ai_takeover_definer"],
        ),
        (
            "staff_assignments",
            "takeover_definer_tenant_select",
            "SELECT",
            ["campus_ai_takeover_definer"],
        ),
    ]
    assert takeover_audit_execute == (True, False)


@pytest.mark.integration
@pytest.mark.parametrize(
    "definer_role",
    [
        "campus_ai_context_definer",
        "campus_ai_audit_definer",
        "campus_ai_outbox_probe_definer",
        "campus_ai_takeover_definer",
    ],
)
def test_bootstrap_returns_definer_owned_business_relations_to_database_owner(
    postgres_roles: PostgresRoles,
    definer_role: str,
) -> None:
    probe_table = f"task1_{definer_role.removeprefix('campus_ai_')}_owned_business_probe"

    try:
        with postgres_roles.admin_connection() as connection:
            connection.execute("SET ROLE NONE")
            connection.execute(
                sql.SQL("DROP TABLE IF EXISTS public.{}").format(sql.Identifier(probe_table))
            )
            connection.execute(
                sql.SQL("CREATE TABLE public.{} (id integer)").format(sql.Identifier(probe_table))
            )
            connection.execute(
                sql.SQL("ALTER TABLE public.{} OWNER TO {}").format(
                    sql.Identifier(probe_table), sql.Identifier(definer_role)
                )
            )

        assert postgres_roles.owned_application_relations(definer_role) == 1
        postgres_roles.rerun_bootstrap()

        with postgres_roles.admin_connection() as connection:
            owner_row = connection.execute(
                """
                SELECT owner.rolname
                FROM pg_class AS relation
                JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
                JOIN pg_roles AS owner ON owner.oid = relation.relowner
                WHERE namespace.nspname = 'public' AND relation.relname = %s
                """,
                (probe_table,),
            ).fetchone()
        assert owner_row == ("campus_ai_db_owner",)
        assert postgres_roles.owned_application_relations(definer_role) == 0
    finally:
        with postgres_roles.admin_connection() as connection:
            connection.execute("SET ROLE NONE")
            connection.execute(
                sql.SQL("DROP TABLE IF EXISTS public.{}").format(sql.Identifier(probe_table))
            )


@pytest.mark.integration
def test_migration_can_only_set_the_five_nologin_roles(
    postgres_roles: PostgresRoles,
) -> None:
    migration = postgres_roles.role("campus_ai_migration")

    assert migration.can_login is True
    assert migration.is_superuser is False
    assert migration.can_create_db is False
    assert migration.can_create_role is False
    assert migration.can_replicate is False
    assert migration.bypass_rls is False
    assert migration.inherits is False
    assert migration.member_of == frozenset(
        {
            "campus_ai_db_owner",
            "campus_ai_context_definer",
            "campus_ai_audit_definer",
            "campus_ai_outbox_probe_definer",
            "campus_ai_takeover_definer",
        }
    )
    assert postgres_roles.membership_options("campus_ai_migration") == {
        "campus_ai_db_owner": (False, False, True),
        "campus_ai_context_definer": (False, False, True),
        "campus_ai_audit_definer": (False, False, True),
        "campus_ai_outbox_probe_definer": (False, False, True),
        "campus_ai_takeover_definer": (False, False, True),
    }

    with postgres_roles.migration_connection() as connection:
        for target_role in (
            "campus_ai_db_owner",
            "campus_ai_context_definer",
            "campus_ai_audit_definer",
            "campus_ai_outbox_probe_definer",
            "campus_ai_takeover_definer",
        ):
            connection.execute(f"SET ROLE {target_role}")
            current_user_row = connection.execute("SELECT current_user").fetchone()
            assert current_user_row is not None
            assert current_user_row[0] == target_role
            connection.execute("SET ROLE NONE")


@pytest.mark.integration
def test_runtime_cannot_set_owner_or_definer_role(postgres_roles: PostgresRoles) -> None:
    with postgres_roles.runtime_connection() as connection:
        for target_role in (
            "campus_ai_db_owner",
            "campus_ai_context_definer",
            "campus_ai_audit_definer",
            "campus_ai_outbox_probe_definer",
            "campus_ai_takeover_definer",
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                connection.execute(f"SET ROLE {target_role}")


@pytest.mark.integration
def test_bootstrap_clears_global_database_and_role_database_startup_role_defaults(
    postgres_roles: PostgresRoles,
) -> None:
    database_name = str(postgres_roles.admin_connection_parameters["dbname"])

    try:
        with postgres_roles.admin_connection() as connection:
            connection.execute("SET ROLE NONE")
            connection.execute("ALTER ROLE campus_ai_migration SET role TO campus_ai_db_owner")
            connection.execute(
                sql.SQL(
                    "ALTER ROLE campus_ai_migration IN DATABASE {} SET role TO campus_ai_db_owner"
                ).format(sql.Identifier(database_name))
            )
            connection.execute(
                sql.SQL("ALTER DATABASE {} SET role TO campus_ai_db_owner").format(
                    sql.Identifier(database_name)
                )
            )

        assert postgres_roles.application_startup_role_defaults()
        postgres_roles.rerun_bootstrap()

        assert postgres_roles.application_startup_role_defaults() == []
        with postgres_roles.migration_connection() as connection:
            assert connection.execute("SELECT session_user, current_user").fetchone() == (
                "campus_ai_migration",
                "campus_ai_migration",
            )
    finally:
        with postgres_roles.admin_connection() as connection:
            connection.execute("SET ROLE NONE")
            connection.execute("ALTER ROLE campus_ai_migration RESET role")
            connection.execute(
                sql.SQL("ALTER ROLE campus_ai_migration IN DATABASE {} RESET role").format(
                    sql.Identifier(database_name)
                )
            )
            connection.execute(
                sql.SQL("ALTER DATABASE {} RESET role").format(sql.Identifier(database_name))
            )


@pytest.mark.integration
def test_database_owner_connect_and_public_schema_acl_are_minimal(
    postgres_roles: PostgresRoles,
) -> None:
    assert postgres_roles.database_owner() == "campus_ai_db_owner"
    assert postgres_roles.public_can_connect() is False
    assert postgres_roles.role_can_connect("campus_ai_runtime") is True
    assert postgres_roles.role_can_connect("campus_ai_migration") is True
    assert postgres_roles.role_can_create_temporary_tables("campus_ai_runtime") is False
    assert postgres_roles.role_can_create_temporary_tables("campus_ai_migration") is False
    assert postgres_roles.role_can_create_in_public_schema("campus_ai_runtime") is False
    assert postgres_roles.role_can_use_public_schema("campus_ai_runtime") is True
    assert postgres_roles.role_can_use_public_schema("campus_ai_migration") is True


@pytest.mark.integration
def test_alembic_migrates_as_nologin_owner_without_echoing_secrets(
    postgres_roles: PostgresRoles,
) -> None:
    migration = postgres_roles.migration_connection_parameters
    migration_password = str(migration["password"])
    context_signing_key = "task1-context-signing-key-32-bytes-minimum"
    environment = os.environ.copy()
    environment.update(
        {
            "POSTGRES_MIGRATION_HOST": str(migration["host"]),
            "POSTGRES_MIGRATION_PORT": str(migration["port"]),
            "POSTGRES_MIGRATION_DB": str(migration["dbname"]),
            "POSTGRES_MIGRATION_USER": str(migration["user"]),
            "POSTGRES_MIGRATION_PASSWORD": migration_password,
            "POSTGRES_MIGRATION_CONTEXT_SIGNING_KEY": context_signing_key,
        }
    )

    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "20260824_20"],
        cwd=API_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    output = result.stdout + result.stderr

    assert migration_password not in output
    assert context_signing_key not in output
    assert result.returncode == 0, output

    with postgres_roles.admin_connection() as connection:
        revision_row = connection.execute(
            "SELECT version_num FROM public.alembic_version"
        ).fetchone()
        assert revision_row is not None
        revision = revision_row[0]
        public_relation_owners = {
            row[0]
            for row in connection.execute(
                """
                SELECT DISTINCT owner.rolname
                FROM pg_class AS relation
                JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
                JOIN pg_roles AS owner ON owner.oid = relation.relowner
                WHERE namespace.nspname = 'public'
                  AND relation.relkind IN ('r', 'p', 'S')
                """
            ).fetchall()
        }

    alembic_configuration = Config(str(API_ROOT / "alembic.ini"))
    alembic_configuration.set_main_option("script_location", str(API_ROOT / "alembic"))
    expected_revision = (
        ScriptDirectory.from_config(alembic_configuration).get_revision("20260824_20").revision
    )

    assert expected_revision is not None
    assert revision == expected_revision
    assert public_relation_owners == {"campus_ai_db_owner"}
    assert postgres_roles.owned_application_relations("campus_ai_context_definer") == 0
    assert postgres_roles.owned_application_relations("campus_ai_audit_definer") == 0
    assert postgres_roles.owned_application_relations("campus_ai_outbox_probe_definer") == 0
    assert postgres_roles.owned_application_relations("campus_ai_takeover_definer") == 0


@pytest.mark.integration
def test_failed_alembic_statement_does_not_echo_context_or_database_password(
    postgres_roles: PostgresRoles,
) -> None:
    migration = postgres_roles.migration_connection_parameters
    migration_password = str(migration["password"])
    context_signing_key = "task1-failing-context-key-must-never-appear"
    environment = os.environ.copy()
    environment.update(
        {
            "POSTGRES_MIGRATION_HOST": str(migration["host"]),
            "POSTGRES_MIGRATION_PORT": str(migration["port"]),
            "POSTGRES_MIGRATION_DB": str(migration["dbname"]),
            "POSTGRES_MIGRATION_USER": str(migration["user"]),
            "POSTGRES_MIGRATION_PASSWORD": migration_password,
            "POSTGRES_MIGRATION_CONTEXT_SIGNING_KEY": context_signing_key,
        }
    )

    with postgres_roles.admin_connection() as connection:
        connection.execute(
            "REVOKE EXECUTE ON FUNCTION pg_catalog.set_config(text, text, boolean) FROM PUBLIC"
        )
    try:
        result = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "20260824_20"],
            cwd=API_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    finally:
        with postgres_roles.admin_connection() as connection:
            connection.execute(
                "GRANT EXECUTE ON FUNCTION pg_catalog.set_config(text, text, boolean) TO PUBLIC"
            )

    output = result.stdout + result.stderr
    assert result.returncode != 0, "受控失败探针没有触发 set_config 权限错误"
    assert migration_password not in output
    assert context_signing_key not in output


@pytest.mark.integration
def test_bootstrap_is_idempotent_and_does_not_echo_test_passwords(
    postgres_roles: PostgresRoles,
) -> None:
    first_output = postgres_roles.bootstrap_output
    second_result = postgres_roles.rerun_bootstrap()

    for protected_secret in postgres_roles.protected_test_secrets:
        assert protected_secret not in first_output
        assert protected_secret not in second_result.stdout
        assert protected_secret not in second_result.stderr


@pytest.mark.integration
def test_bootstrap_converges_preexisting_dangerous_role_attributes(
    current_master_data_schema: PostgresRoles,
) -> None:
    postgres_roles = current_master_data_schema
    with postgres_roles.admin_connection() as connection:
        connection.execute(
            "ALTER ROLE campus_ai_db_owner LOGIN INHERIT SUPERUSER CREATEDB CREATEROLE "
            "REPLICATION BYPASSRLS"
        )
        connection.execute(
            "ALTER ROLE campus_ai_migration INHERIT SUPERUSER CREATEDB CREATEROLE "
            "REPLICATION BYPASSRLS"
        )
        connection.execute(
            "ALTER ROLE campus_ai_runtime INHERIT SUPERUSER CREATEDB CREATEROLE "
            "REPLICATION BYPASSRLS"
        )
        connection.execute(
            "ALTER ROLE campus_ai_audit_definer LOGIN INHERIT SUPERUSER CREATEDB CREATEROLE "
            "REPLICATION BYPASSRLS"
        )
        connection.execute(
            "ALTER ROLE campus_ai_outbox_probe_definer LOGIN INHERIT SUPERUSER CREATEDB "
            "CREATEROLE REPLICATION BYPASSRLS"
        )
        connection.execute(
            "ALTER ROLE campus_ai_takeover_definer LOGIN INHERIT SUPERUSER CREATEDB "
            "CREATEROLE REPLICATION BYPASSRLS"
        )
        connection.execute("GRANT CREATE ON SCHEMA public TO campus_ai_context_definer")
        connection.execute("GRANT CREATE ON SCHEMA app TO campus_ai_context_definer")
        connection.execute("GRANT CREATE ON SCHEMA public TO campus_ai_audit_definer")
        connection.execute("GRANT CREATE ON SCHEMA app TO campus_ai_audit_definer")
        connection.execute("GRANT CREATE ON SCHEMA app_private TO campus_ai_audit_definer")
        connection.execute("GRANT CREATE ON SCHEMA public TO campus_ai_outbox_probe_definer")
        connection.execute("GRANT CREATE ON SCHEMA app TO campus_ai_outbox_probe_definer")
        connection.execute("GRANT CREATE ON SCHEMA public TO campus_ai_takeover_definer")
        connection.execute("GRANT CREATE ON SCHEMA app TO campus_ai_takeover_definer")
        connection.execute(
            "GRANT ALL ON TABLE public.outbox_messages TO campus_ai_outbox_probe_definer"
        )
        connection.execute(
            "ALTER FUNCTION app.verify_staff_duty_binding_outbox(uuid,uuid,uuid,bigint) "
            "OWNER TO campus_ai_db_owner"
        )
        connection.execute(
            "GRANT EXECUTE ON FUNCTION "
            "app.verify_staff_duty_binding_outbox(uuid,uuid,uuid,bigint) TO PUBLIC"
        )
        connection.execute(
            "GRANT EXECUTE ON FUNCTION app.append_business_audit("
            "integer,uuid,uuid,uuid,bigint,uuid,bigint,text,text,text,text,"
            "uuid,jsonb,jsonb,uuid,text) TO PUBLIC"
        )
        connection.execute(
            "GRANT EXECUTE ON FUNCTION app_private.is_safe_business_audit_summary(jsonb) "
            "TO campus_ai_runtime"
        )
        connection.execute(
            "GRANT INSERT (event_type, target_type, target_id) "
            "ON TABLE public.audit_logs TO campus_ai_runtime"
        )
        connection.execute("GRANT USAGE ON SEQUENCE public.audit_logs_id_seq TO campus_ai_runtime")
        connection.execute("GRANT pg_read_all_data TO campus_ai_runtime")
        connection.execute("GRANT pg_write_all_data TO campus_ai_migration")

    postgres_roles.rerun_bootstrap()

    runtime = postgres_roles.role("campus_ai_runtime")
    migration = postgres_roles.role("campus_ai_migration")
    owner = postgres_roles.role("campus_ai_db_owner")
    context_definer = postgres_roles.role("campus_ai_context_definer")
    audit_definer = postgres_roles.role("campus_ai_audit_definer")
    outbox_probe_definer = postgres_roles.role("campus_ai_outbox_probe_definer")
    takeover_definer = postgres_roles.role("campus_ai_takeover_definer")
    assert runtime == runtime.__class__(
        can_login=True,
        is_superuser=False,
        can_create_db=False,
        can_create_role=False,
        can_replicate=False,
        bypass_rls=False,
        inherits=False,
        member_of=frozenset(),
    )
    assert migration.is_superuser is False
    assert migration.can_create_db is False
    assert migration.can_create_role is False
    assert migration.can_replicate is False
    assert migration.bypass_rls is False
    assert migration.inherits is False
    assert migration.member_of == frozenset(
        {
            "campus_ai_db_owner",
            "campus_ai_context_definer",
            "campus_ai_audit_definer",
            "campus_ai_outbox_probe_definer",
            "campus_ai_takeover_definer",
        }
    )
    assert owner.can_login is False
    assert owner.is_superuser is False
    assert owner.can_create_db is False
    assert owner.can_create_role is False
    assert owner.can_replicate is False
    assert owner.bypass_rls is False
    assert owner.inherits is False
    assert context_definer.can_login is False
    assert context_definer.is_superuser is False
    assert context_definer.can_create_db is False
    assert context_definer.can_create_role is False
    assert context_definer.can_replicate is False
    assert context_definer.bypass_rls is False
    assert context_definer.inherits is False
    assert audit_definer.can_login is False
    assert audit_definer.is_superuser is False
    assert audit_definer.can_create_db is False
    assert audit_definer.can_create_role is False
    assert audit_definer.can_replicate is False
    assert audit_definer.bypass_rls is False
    assert audit_definer.inherits is False
    assert outbox_probe_definer.can_login is False
    assert outbox_probe_definer.is_superuser is False
    assert outbox_probe_definer.can_create_db is False
    assert outbox_probe_definer.can_create_role is False
    assert outbox_probe_definer.can_replicate is False
    assert outbox_probe_definer.bypass_rls is False
    assert outbox_probe_definer.inherits is False
    assert takeover_definer.can_login is False
    assert takeover_definer.is_superuser is False
    assert takeover_definer.can_create_db is False
    assert takeover_definer.can_create_role is False
    assert takeover_definer.can_replicate is False
    assert takeover_definer.bypass_rls is False
    assert takeover_definer.inherits is False
    assert postgres_roles.role_can_create_in_public_schema("campus_ai_context_definer") is False
    assert postgres_roles.role_can_create_in_schema("campus_ai_context_definer", "app") is False
    assert postgres_roles.role_can_create_in_public_schema("campus_ai_audit_definer") is False
    assert postgres_roles.role_can_create_in_schema("campus_ai_audit_definer", "app") is False
    assert (
        postgres_roles.role_can_create_in_schema("campus_ai_audit_definer", "app_private") is False
    )
    assert (
        postgres_roles.role_can_create_in_public_schema("campus_ai_outbox_probe_definer") is False
    )
    assert (
        postgres_roles.role_can_create_in_schema("campus_ai_outbox_probe_definer", "app") is False
    )
    assert postgres_roles.role_can_create_in_public_schema("campus_ai_takeover_definer") is False
    assert postgres_roles.role_can_create_in_schema("campus_ai_takeover_definer", "app") is False
    with postgres_roles.admin_connection() as connection:
        function_acl_row = connection.execute(
            """
            SELECT has_function_privilege(
                       'campus_ai_runtime',
                       'app_private.is_safe_business_audit_summary(jsonb)',
                       'EXECUTE'
                   ),
                   EXISTS (
                       SELECT 1
                       FROM pg_proc AS procedure
                       JOIN pg_namespace AS namespace
                         ON namespace.oid = procedure.pronamespace
                       CROSS JOIN LATERAL aclexplode(
                           COALESCE(
                               procedure.proacl,
                               acldefault('f', procedure.proowner)
                           )
                       ) AS acl
                       WHERE namespace.nspname = 'app'
                         AND procedure.proname = 'append_business_audit'
                         AND acl.grantee = 0
                         AND acl.privilege_type = 'EXECUTE'
                   )
            """
        ).fetchone()
    assert function_acl_row is not None
    helper_runtime_execute, business_public_execute = function_acl_row
    assert helper_runtime_execute is False
    assert business_public_execute is False
    with postgres_roles.admin_connection() as connection:
        audit_acl_row = connection.execute(
            """
            SELECT has_table_privilege(
                       'campus_ai_runtime', 'public.audit_logs', 'INSERT'
                   ),
                   has_sequence_privilege(
                       'campus_ai_runtime', 'public.audit_logs_id_seq', 'USAGE'
                   ),
                   (
                       SELECT count(*)
                       FROM information_schema.column_privileges
                       WHERE table_schema = 'public'
                         AND table_name = 'audit_logs'
                         AND grantee = 'campus_ai_runtime'
                   )
            """
        ).fetchone()
    assert audit_acl_row == (False, False, 0)
    with postgres_roles.admin_connection() as connection:
        outbox_probe_security = connection.execute(
            """
            SELECT owner.rolname,
                   has_function_privilege(
                       'public',
                       'app.verify_staff_duty_binding_outbox(uuid,uuid,uuid,bigint)',
                       'EXECUTE'
                   ),
                   has_table_privilege(
                       'campus_ai_outbox_probe_definer',
                       'public.outbox_messages',
                       'SELECT'
                   ),
                   string_agg(columns.column_name, ',' ORDER BY columns.column_name)
                       FILTER (WHERE columns.privilege_type = 'SELECT')
            FROM pg_proc AS procedure
            JOIN pg_namespace AS namespace ON namespace.oid = procedure.pronamespace
            JOIN pg_roles AS owner ON owner.oid = procedure.proowner
            LEFT JOIN information_schema.column_privileges AS columns
              ON columns.table_schema = 'public'
             AND columns.table_name = 'outbox_messages'
             AND columns.grantee = 'campus_ai_outbox_probe_definer'
            WHERE namespace.nspname = 'app'
              AND procedure.proname = 'verify_staff_duty_binding_outbox'
            GROUP BY owner.rolname
            """
        ).fetchone()
    assert outbox_probe_security == (
        "campus_ai_outbox_probe_definer",
        False,
        False,
        "aggregate_id,aggregate_type,aggregate_version,event_key,event_type,id,school_id",
    )


@pytest.mark.integration
def test_bootstrap_revokes_execute_from_unapproved_app_functions(
    current_master_data_schema: PostgresRoles,
) -> None:
    """runtime 的函数能力必须来自签名白名单，而不是 app schema 的历史漂移。"""

    postgres_roles = current_master_data_schema
    function_signature = "app.task5_unapproved_runtime_probe()"
    try:
        with postgres_roles.admin_connection() as connection:
            connection.execute("SET ROLE campus_ai_db_owner")
            connection.execute(
                "CREATE OR REPLACE FUNCTION app.task5_unapproved_runtime_probe() "
                "RETURNS integer LANGUAGE sql AS 'SELECT 1'"
            )
            connection.execute(
                f"GRANT EXECUTE ON FUNCTION {function_signature} TO PUBLIC, campus_ai_runtime"
            )

        with postgres_roles.runtime_connection() as connection:
            assert connection.execute("SELECT app.task5_unapproved_runtime_probe()").fetchone() == (
                1,
            )

        postgres_roles.rerun_bootstrap()

        with postgres_roles.runtime_connection() as connection:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                connection.execute("SELECT app.task5_unapproved_runtime_probe()")
    finally:
        with postgres_roles.admin_connection() as connection:
            connection.execute("SET ROLE campus_ai_db_owner")
            connection.execute(f"DROP FUNCTION IF EXISTS {function_signature}")


@pytest.mark.integration
def test_bootstrap_twice_restores_private_takeover_validator_owner_and_acl(
    current_master_data_schema: PostgresRoles,
) -> None:
    """bootstrap 必须幂等收敛私有证明函数的 owner、ACL 与隔离角色属性。"""

    postgres_roles = current_master_data_schema
    private_signatures = (
        "app_private.master_data_import_takeover_chain_is_valid(uuid,uuid,uuid,bigint)",
        "app_private.populate_master_data_import_unit_outcome_snapshot()",
        "app_private.reject_master_data_import_outcome_mutation()",
    )
    pollution_table = "public.task6_takeover_definer_pollution"
    polluted_grantees = (
        "PUBLIC, campus_ai_runtime, campus_ai_db_owner, campus_ai_context_definer, "
        "campus_ai_audit_definer, campus_ai_outbox_probe_definer, campus_ai_migration"
    )

    def private_function_security(signature: str) -> tuple[str, bool, list[str]]:
        with postgres_roles.admin_connection() as connection:
            row = connection.execute(
                """
                SELECT owner.rolname,
                       procedure.prosecdef,
                       coalesce(
                           array_agg(
                               CASE WHEN acl.grantee = 0 THEN 'PUBLIC' ELSE grantee.rolname END
                               ORDER BY CASE
                                   WHEN acl.grantee = 0 THEN 'PUBLIC'
                                   ELSE grantee.rolname
                               END
                           ) FILTER (WHERE acl.privilege_type = 'EXECUTE'),
                           ARRAY[]::text[]
                       )
                FROM pg_proc AS procedure
                JOIN pg_namespace AS namespace ON namespace.oid = procedure.pronamespace
                JOIN pg_roles AS owner ON owner.oid = procedure.proowner
                LEFT JOIN LATERAL aclexplode(
                    coalesce(procedure.proacl, acldefault('f', procedure.proowner))
                ) AS acl ON true
                LEFT JOIN pg_roles AS grantee ON grantee.oid = acl.grantee
                WHERE procedure.oid = %s::regprocedure
                GROUP BY owner.rolname, procedure.prosecdef
                """,
                (signature,),
            ).fetchone()
        assert row is not None
        return row[0], row[1], list(row[2])

    try:
        with postgres_roles.admin_connection() as connection:
            for signature in private_signatures:
                connection.execute(f"ALTER FUNCTION {signature} OWNER TO campus_ai_audit_definer")
                connection.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO {polluted_grantees}")
            connection.execute("ALTER ROLE campus_ai_takeover_definer LOGIN INHERIT BYPASSRLS")
            connection.execute(f"CREATE TABLE {pollution_table} (id integer PRIMARY KEY)")
            connection.execute(f"ALTER TABLE {pollution_table} OWNER TO campus_ai_takeover_definer")

        postgres_roles.rerun_bootstrap()
        postgres_roles.rerun_bootstrap()

        for signature in private_signatures:
            assert private_function_security(signature) == (
                "campus_ai_takeover_definer",
                True,
                ["campus_ai_takeover_definer"],
            )
        with postgres_roles.admin_connection() as connection:
            assert connection.execute(
                """
                SELECT rolcanlogin, rolinherit, rolbypassrls
                FROM pg_roles
                WHERE rolname = 'campus_ai_takeover_definer'
                """
            ).fetchone() == (False, False, False)
            assert connection.execute(
                """
                SELECT count(*)
                FROM pg_class AS relation
                JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
                JOIN pg_roles AS owner ON owner.oid = relation.relowner
                WHERE owner.rolname = 'campus_ai_takeover_definer'
                  AND namespace.nspname IN ('public', 'app', 'app_private')
                """
            ).fetchone() == (0,)
    finally:
        with postgres_roles.admin_connection() as connection:
            connection.execute(f"DROP TABLE IF EXISTS {pollution_table}")
            for signature in private_signatures:
                if connection.execute("SELECT to_regprocedure(%s)", (signature,)).fetchone() != (
                    None,
                ):
                    connection.execute(
                        f"ALTER FUNCTION {signature} OWNER TO campus_ai_takeover_definer"
                    )
                    connection.execute(
                        f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC, "
                        "campus_ai_runtime, campus_ai_db_owner, campus_ai_context_definer, "
                        "campus_ai_audit_definer, campus_ai_outbox_probe_definer, "
                        "campus_ai_migration"
                    )
                    connection.execute(
                        f"GRANT EXECUTE ON FUNCTION {signature} TO campus_ai_takeover_definer"
                    )
            connection.execute(
                "ALTER ROLE campus_ai_takeover_definer NOLOGIN NOINHERIT NOBYPASSRLS"
            )


@pytest.mark.integration
def test_bootstrap_removes_rogue_memberships_and_explicit_acl_grants(
    postgres_roles: PostgresRoles,
) -> None:
    rogue_role = "campus_ai_task1_rogue_login"
    intermediate_role = "campus_ai_task1_rogue_group"
    delegated_role = "campus_ai_task1_rogue_delegate"
    database_name = str(postgres_roles.admin_connection_parameters["dbname"])

    def remove_rogue_test_roles() -> None:
        with postgres_roles.admin_connection() as connection:
            connection.execute("SET ROLE NONE")
            existing_roles = {
                row[0]
                for row in connection.execute(
                    "SELECT rolname FROM pg_roles WHERE rolname = ANY(%s)",
                    ([rogue_role, intermediate_role, delegated_role],),
                ).fetchall()
            }
            for role_name in (rogue_role, delegated_role):
                if role_name not in existing_roles:
                    continue
                connection.execute(
                    sql.SQL("REVOKE ALL PRIVILEGES ON DATABASE {} FROM {} CASCADE").format(
                        sql.Identifier(database_name),
                        sql.Identifier(role_name),
                    )
                )
                connection.execute(
                    sql.SQL("REVOKE ALL PRIVILEGES ON SCHEMA public FROM {} CASCADE").format(
                        sql.Identifier(role_name)
                    )
                )
            if rogue_role in existing_roles:
                connection.execute(
                    sql.SQL("REVOKE campus_ai_migration FROM {} CASCADE").format(
                        sql.Identifier(rogue_role)
                    )
                )
            if rogue_role in existing_roles and intermediate_role in existing_roles:
                connection.execute(
                    sql.SQL("REVOKE {} FROM {} CASCADE").format(
                        sql.Identifier(intermediate_role),
                        sql.Identifier(rogue_role),
                    )
                )
            if intermediate_role in existing_roles:
                connection.execute(
                    sql.SQL("REVOKE campus_ai_db_owner FROM {} CASCADE").format(
                        sql.Identifier(intermediate_role)
                    )
                )
            if rogue_role in existing_roles:
                connection.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(rogue_role)))
            if delegated_role in existing_roles:
                connection.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(delegated_role)))
            if intermediate_role in existing_roles:
                connection.execute(
                    sql.SQL("DROP ROLE {}").format(sql.Identifier(intermediate_role))
                )

    try:
        postgres_roles.rerun_bootstrap()
        remove_rogue_test_roles()
        with postgres_roles.admin_connection() as connection:
            connection.execute(sql.SQL("CREATE ROLE {} LOGIN").format(sql.Identifier(rogue_role)))
            connection.execute(
                sql.SQL("CREATE ROLE {} NOLOGIN").format(sql.Identifier(intermediate_role))
            )
            connection.execute(
                sql.SQL("CREATE ROLE {} NOLOGIN").format(sql.Identifier(delegated_role))
            )
            connection.execute(
                sql.SQL("GRANT campus_ai_db_owner TO {}").format(sql.Identifier(intermediate_role))
            )
            connection.execute(
                sql.SQL("GRANT {} TO {}").format(
                    sql.Identifier(intermediate_role),
                    sql.Identifier(rogue_role),
                )
            )
            connection.execute(
                sql.SQL("GRANT campus_ai_migration TO {}").format(sql.Identifier(rogue_role))
            )
            connection.execute(
                sql.SQL("GRANT CONNECT, TEMPORARY ON DATABASE {} TO {} WITH GRANT OPTION").format(
                    sql.Identifier(database_name),
                    sql.Identifier(rogue_role),
                )
            )
            connection.execute(
                sql.SQL("GRANT USAGE, CREATE ON SCHEMA public TO {} WITH GRANT OPTION").format(
                    sql.Identifier(rogue_role)
                )
            )
            connection.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(rogue_role)))
            connection.execute(
                sql.SQL("GRANT CONNECT, TEMPORARY ON DATABASE {} TO {}").format(
                    sql.Identifier(database_name),
                    sql.Identifier(delegated_role),
                )
            )
            connection.execute(
                sql.SQL("GRANT USAGE, CREATE ON SCHEMA public TO {}").format(
                    sql.Identifier(delegated_role)
                )
            )
            connection.execute("SET ROLE NONE")

        assert postgres_roles.role_can_set_role(rogue_role, "campus_ai_db_owner") is True
        assert postgres_roles.role_can_connect(rogue_role) is True
        assert postgres_roles.role_can_create_temporary_tables(rogue_role) is True
        assert postgres_roles.role_can_create_in_public_schema(rogue_role) is True
        assert postgres_roles.role_can_connect(delegated_role) is True
        assert postgres_roles.role_can_create_temporary_tables(delegated_role) is True
        assert postgres_roles.role_can_create_in_public_schema(delegated_role) is True

        postgres_roles.rerun_bootstrap()

        assert postgres_roles.role_can_set_role(rogue_role, "campus_ai_db_owner") is False
        assert postgres_roles.role_can_set_role(rogue_role, "campus_ai_migration") is False
        assert postgres_roles.role_can_connect(rogue_role) is False
        assert postgres_roles.role_can_create_temporary_tables(rogue_role) is False
        assert postgres_roles.role_can_use_public_schema(rogue_role) is False
        assert postgres_roles.role_can_create_in_public_schema(rogue_role) is False
        assert postgres_roles.role_can_connect(delegated_role) is False
        assert postgres_roles.role_can_create_temporary_tables(delegated_role) is False
        assert postgres_roles.role_can_use_public_schema(delegated_role) is False
        assert postgres_roles.role_can_create_in_public_schema(delegated_role) is False
    finally:
        remove_rogue_test_roles()


@pytest.mark.integration
def test_bootstrap_passwords_are_psql_literals_not_shell_or_sql(
    postgres_roles: PostgresRoles,
) -> None:
    runtime_password = "runtime quote ' slash \\ space ; $ test"
    migration_password = 'migration quote " slash \\ space ; $ test'
    writer_password = "writer quote ' slash \\ space ; $ test"
    reader_password = 'reader quote " slash \\ space ; $ test'

    try:
        postgres_roles.run_bootstrap_with_passwords(
            runtime_password=runtime_password,
            migration_password=migration_password,
            writer_password=writer_password,
            reader_password=reader_password,
        )
        runtime_parameters = {
            **postgres_roles.runtime_connection_parameters,
            "password": runtime_password,
        }
        migration_parameters = {
            **postgres_roles.migration_connection_parameters,
            "password": migration_password,
        }
        writer_parameters = {
            **postgres_roles.writer_connection_parameters,
            "password": writer_password,
        }
        reader_parameters = {
            **postgres_roles.reader_connection_parameters,
            "password": reader_password,
        }
        with psycopg.connect(**runtime_parameters, autocommit=True) as runtime_connection:
            assert runtime_connection.execute("SELECT current_user").fetchone() == (
                "campus_ai_runtime",
            )
        with psycopg.connect(**migration_parameters, autocommit=True) as migration_connection:
            assert migration_connection.execute("SELECT current_user").fetchone() == (
                "campus_ai_migration",
            )
        with psycopg.connect(**writer_parameters, autocommit=True) as writer_connection:
            assert writer_connection.execute("SELECT current_user").fetchone() == (
                "campus_ai_school_fact_writer",
            )
        with psycopg.connect(**reader_parameters, autocommit=True) as reader_connection:
            assert reader_connection.execute("SELECT current_user").fetchone() == (
                "campus_ai_school_fact_reader",
            )
        with pytest.raises(psycopg.OperationalError):
            psycopg.connect(**{**writer_parameters, "password": reader_password}, autocommit=True)
    finally:
        postgres_roles.rerun_bootstrap()


@pytest.mark.integration
def test_school_fact_database_roles_converge_fresh_and_repeat_without_cross_root_access(
    current_master_data_schema: PostgresRoles,
) -> None:
    postgres_roles = current_master_data_schema
    expected = {
        "campus_ai_school_fact_writer": postgres_roles.writer_connection_parameters,
        "campus_ai_school_fact_reader": postgres_roles.reader_connection_parameters,
    }
    for _ in range(2):
        postgres_roles.rerun_bootstrap()
        for role_name, parameters in expected.items():
            role = postgres_roles.role(role_name)
            assert role == role.__class__(
                can_login=True,
                is_superuser=False,
                can_create_db=False,
                can_create_role=False,
                can_replicate=False,
                bypass_rls=False,
                inherits=False,
                member_of=frozenset(),
            )
            assert postgres_roles.owned_application_relations(role_name) == 0
            assert postgres_roles.role_can_connect(role_name) is True
            assert postgres_roles.role_can_create_temporary_tables(role_name) is False
            assert postgres_roles.role_can_use_public_schema(role_name) is True
            assert postgres_roles.role_can_create_in_public_schema(role_name) is False
            assert postgres_roles.role_can_create_in_schema(role_name, "app") is False
            assert postgres_roles.role_can_use_schema(role_name, "app") is True
            for target_role in (
                "campus_ai_db_owner",
                "campus_ai_migration",
                "campus_ai_runtime",
            ):
                assert postgres_roles.role_can_set_role(role_name, target_role) is False
            with psycopg.connect(**parameters, autocommit=True) as connection:
                assert connection.execute("SELECT current_user").fetchone() == (role_name,)

        identity_definer = postgres_roles.role("campus_ai_identity_definer")
        assert identity_definer == identity_definer.__class__(
            can_login=False,
            is_superuser=False,
            can_create_db=False,
            can_create_role=False,
            can_replicate=False,
            bypass_rls=False,
            inherits=False,
            member_of=frozenset(),
        )
        assert postgres_roles.owned_application_relations("campus_ai_identity_definer") == 0
        assert postgres_roles.role_can_connect("campus_ai_identity_definer") is False
        assert postgres_roles.role_can_use_public_schema("campus_ai_identity_definer") is True
        assert (
            postgres_roles.role_can_create_in_public_schema("campus_ai_identity_definer") is False
        )
        assert (
            postgres_roles.role_can_create_in_schema("campus_ai_identity_definer", "app") is False
        )
        assert postgres_roles.role_can_use_schema("campus_ai_identity_definer", "app") is True
        for target_role in (
            "campus_ai_db_owner",
            "campus_ai_migration",
            "campus_ai_runtime",
        ):
            assert (
                postgres_roles.role_can_set_role("campus_ai_identity_definer", target_role) is False
            )

        with postgres_roles.admin_connection() as connection:
            identity_function_owners = connection.execute(
                """
                SELECT procedure.oid::regprocedure::text,
                       owner.rolname,
                       has_function_privilege('public', procedure.oid, 'EXECUTE'),
                       has_function_privilege('campus_ai_runtime', procedure.oid, 'EXECUTE'),
                       has_function_privilege(
                           'campus_ai_school_fact_writer', procedure.oid, 'EXECUTE'
                       ),
                       has_function_privilege(
                           'campus_ai_school_fact_reader', procedure.oid, 'EXECUTE'
                       )
                FROM pg_proc AS procedure
                JOIN pg_namespace AS namespace ON namespace.oid = procedure.pronamespace
                JOIN pg_roles AS owner ON owner.oid = procedure.proowner
                WHERE procedure.oid IN (
                    'app.lock_existing_identity_user_id(text,text,text)'::regprocedure,
                    'app.claim_legacy_identity_user_id(text,text,text,text)'::regprocedure
                )
                ORDER BY procedure.oid::regprocedure::text
                """
            ).fetchall()
        assert identity_function_owners == [
            (
                "app.claim_legacy_identity_user_id(text,text,text,text)",
                "campus_ai_identity_definer",
                False,
                True,
                False,
                False,
            ),
            (
                "app.lock_existing_identity_user_id(text,text,text)",
                "campus_ai_identity_definer",
                False,
                False,
                True,
                False,
            ),
        ]


@pytest.mark.integration
def test_school_reactivation_bridge_catalog_converges_to_writer_only(
    current_master_data_schema: PostgresRoles,
) -> None:
    postgres_roles = current_master_data_schema
    expected_functions = {
        (
            "school_fact_school_reactivation_preflight",
            "uuid, integer, uuid, uuid, bigint, bigint, uuid, text, text, "
            "timestamp with time zone, text, text, bigint, timestamp with time zone, "
            "bigint, text, text",
            "campus_ai_context_definer",
            True,
            ("search_path=pg_catalog, pg_temp",),
            False,
            False,
            True,
            False,
            False,
        ),
        (
            "school_fact_school_reactivation_prepare",
            "uuid, integer, uuid, uuid, bigint, bigint, uuid, text, text, "
            "timestamp with time zone, text, text, bigint, timestamp with time zone, "
            "bigint, text, text, uuid, uuid, text, text, timestamp with time zone, text, text, "
            "text, text, text, text, integer",
            "campus_ai_context_definer",
            True,
            ("search_path=pg_catalog, pg_temp",),
            False,
            False,
            True,
            False,
            False,
        ),
    }
    expected_insert_columns = {
        ("master_data_import_batches", column_name)
        for column_name in {
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
        }
    } | {
        ("master_data_import_units", column_name)
        for column_name in {
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
        }
    }

    def freeze(row: tuple[object, ...]) -> tuple[object, ...]:
        return tuple(tuple(value) if isinstance(value, list) else value for value in row)

    def snapshot() -> tuple[
        set[tuple[object, ...]], set[tuple[object, ...]], set[tuple[object, ...]]
    ]:
        with postgres_roles.admin_connection() as connection:
            functions = {
                freeze(row)
                for row in connection.execute(
                    """
                    SELECT procedure.proname,
                           pg_catalog.oidvectortypes(procedure.proargtypes),
                           owner.rolname,
                           procedure.prosecdef,
                           procedure.proconfig,
                           has_function_privilege('public', procedure.oid, 'EXECUTE'),
                           has_function_privilege('campus_ai_runtime', procedure.oid, 'EXECUTE'),
                           has_function_privilege(
                               'campus_ai_school_fact_writer', procedure.oid, 'EXECUTE'
                           ),
                           has_function_privilege(
                               'campus_ai_school_fact_reader', procedure.oid, 'EXECUTE'
                           ),
                           has_function_privilege(
                               'campus_ai_migration', procedure.oid, 'EXECUTE'
                           )
                    FROM pg_catalog.pg_proc AS procedure
                    JOIN pg_catalog.pg_namespace AS namespace
                      ON namespace.oid = procedure.pronamespace
                    JOIN pg_catalog.pg_roles AS owner ON owner.oid = procedure.proowner
                    WHERE namespace.nspname = 'app'
                      AND procedure.proname IN (
                          'school_fact_school_reactivation_preflight',
                          'school_fact_school_reactivation_prepare'
                      )
                    ORDER BY procedure.proname
                    """
                ).fetchall()
            }
            policies = {
                freeze(row)
                for row in connection.execute(
                    """
                    SELECT relation.relname,
                           policy.polname,
                           policy.polcmd,
                           ARRAY(
                               SELECT role.rolname
                               FROM pg_catalog.pg_roles AS role
                               WHERE role.oid = ANY(policy.polroles)
                               ORDER BY role.rolname
                           ),
                           pg_catalog.pg_get_expr(policy.polwithcheck, policy.polrelid)
                    FROM pg_catalog.pg_policy AS policy
                    JOIN pg_catalog.pg_class AS relation ON relation.oid = policy.polrelid
                    JOIN pg_catalog.pg_namespace AS namespace
                      ON namespace.oid = relation.relnamespace
                    WHERE namespace.nspname = 'public'
                      AND relation.relname IN (
                          'master_data_import_batches', 'master_data_import_units'
                      )
                      AND policy.polname = 'sf5b_school_reactivation_prepare_insert'
                    ORDER BY relation.relname
                    """
                ).fetchall()
            }
            insert_columns = {
                freeze(row)
                for row in connection.execute(
                    """
                    SELECT table_name, column_name
                    FROM information_schema.column_privileges
                    WHERE table_schema = 'public'
                      AND grantee = 'campus_ai_context_definer'
                      AND privilege_type = 'INSERT'
                      AND table_name IN (
                          'master_data_import_batches', 'master_data_import_units'
                      )
                    ORDER BY table_name, column_name
                    """
                ).fetchall()
            }
        return functions, policies, insert_columns

    expected_policies = {
        (
            table_name,
            "sf5b_school_reactivation_prepare_insert",
            "a",
            ("campus_ai_context_definer",),
            "(school_id = app.require_school_database_anchor())",
        )
        for table_name in ("master_data_import_batches", "master_data_import_units")
    }
    functions, policies, insert_columns = snapshot()
    assert functions == expected_functions
    assert policies == expected_policies
    assert insert_columns == expected_insert_columns

    postgres_roles.rerun_bootstrap()
    assert snapshot() == (expected_functions, expected_policies, expected_insert_columns)


@pytest.mark.integration
def test_revision20_catalog_is_exact_after_bootstrap_repeat(
    current_master_data_schema: PostgresRoles,
) -> None:
    postgres_roles = current_master_data_schema
    migration = runpy.run_path(str(REVISION20_MIGRATION))
    app_tables = tuple(migration["_SF5B_APP_ACCESS_TABLES"])
    guard_only_tables = tuple(migration["_ANCHOR_GUARD_ONLY_TABLES"])
    school_bearing_tables = app_tables + guard_only_tables
    writer_tables = tuple(migration["_SCHOOL_FACT_WRITER_TABLES"])
    governance_reader_tables = tuple(migration["_GOVERNANCE_READER_TABLES"])
    r1_reader_tables = tuple(migration["_R1_READER_TABLES"])
    retired_repair_tables = tuple(migration["_RETIRED_REPAIRS_RUNTIME_ACCESS_TABLES"])
    legacy_context_policy_drop_tables = set(migration["_LEGACY_CONTEXT_POLICY_DROP_TABLES"])
    legacy_import_policy_drop_tables = set(migration["_LEGACY_IMPORT_POLICY_DROP_TABLES"])
    runtime_dml_revoke_tables = tuple(migration["_SCHOOL_FACT_RUNTIME_DML_REVOKE_TABLES"])
    shared_support_tables = tuple(migration["_SHARED_SUPPORT_TABLES"])
    assert len(app_tables) == 46
    assert len(school_bearing_tables) == 59

    with postgres_roles.admin_connection() as connection:
        policy_rows = connection.execute(
            """
            SELECT relation.relname,
                   relation.relrowsecurity,
                   relation.relforcerowsecurity,
                   policy.polname,
                   policy.polpermissive,
                   policy.polcmd,
                   CASE
                       WHEN policy.polroles = ARRAY[0::oid] THEN ARRAY['PUBLIC']::text[]
                       ELSE ARRAY(
                           SELECT role.rolname
                           FROM pg_roles AS role
                           WHERE role.oid = ANY(policy.polroles)
                           ORDER BY role.rolname
                       )
                   END,
                   pg_get_expr(policy.polqual, policy.polrelid),
                   pg_get_expr(policy.polwithcheck, policy.polrelid)
            FROM pg_class AS relation
            JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
            LEFT JOIN pg_policy AS policy ON policy.polrelid = relation.oid
            WHERE namespace.nspname = 'public'
              AND relation.relname = ANY(%s)
            ORDER BY relation.relname, policy.polname
            """,
            (list(school_bearing_tables),),
        ).fetchall()
        writer_grants = set(
            connection.execute(
                """
                SELECT table_name, privilege_type
                FROM information_schema.role_table_grants
                WHERE table_schema = 'public'
                  AND grantee = 'campus_ai_school_fact_writer'
                ORDER BY table_name, privilege_type
                """
            ).fetchall()
        )
        reader_grants = set(
            connection.execute(
                """
                SELECT table_name, privilege_type
                FROM information_schema.role_table_grants
                WHERE table_schema = 'public'
                  AND grantee = 'campus_ai_school_fact_reader'
                ORDER BY table_name, privilege_type
                """
            ).fetchall()
        )
        writer_insert_columns = set(
            connection.execute(
                """
                SELECT table_name, column_name
                FROM information_schema.column_privileges
                WHERE table_schema = 'public'
                  AND grantee = 'campus_ai_school_fact_writer'
                  AND privilege_type = 'INSERT'
                  AND table_name IN ('audit_logs', 'command_idempotency_records')
                ORDER BY table_name, column_name
                """
            ).fetchall()
        )
        isolated_capability_roles = ("campus_ai_identity_definer",)
        isolated_capability_table_grants = set(
            connection.execute(
                """
                SELECT grantee, table_name, privilege_type
                FROM information_schema.role_table_grants
                WHERE table_schema = 'public'
                  AND grantee = ANY(%s)
                ORDER BY grantee, table_name, privilege_type
                """,
                (list(isolated_capability_roles),),
            ).fetchall()
        )
        isolated_capability_column_grants = set(
            connection.execute(
                """
                SELECT grantee, table_name, column_name, privilege_type
                FROM information_schema.column_privileges
                WHERE table_schema = 'public'
                  AND grantee = ANY(%s)
                ORDER BY grantee, table_name, column_name, privilege_type
                """,
                (list(isolated_capability_roles),),
            ).fetchall()
        )
        isolated_capability_sequence_grants = set(
            connection.execute(
                """
                SELECT grantee.rolname, relation.relname, acl.privilege_type
                FROM pg_class AS relation
                JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
                CROSS JOIN LATERAL aclexplode(relation.relacl) AS acl
                JOIN pg_roles AS grantee ON grantee.oid = acl.grantee
                WHERE namespace.nspname = 'public'
                  AND relation.relkind = 'S'
                  AND grantee.rolname = ANY(%s)
                ORDER BY grantee.rolname, relation.relname, acl.privilege_type
                """,
                (list(isolated_capability_roles),),
            ).fetchall()
        )
        security_function_acl = set(
            connection.execute(
                """
                SELECT procedure.oid::regprocedure::text,
                       owner.rolname,
                       procedure.prosecdef,
                       procedure.proconfig =
                           ARRAY['search_path=pg_catalog, pg_temp']::text[],
                       has_function_privilege('public', procedure.oid, 'EXECUTE'),
                       has_function_privilege(
                           'campus_ai_runtime', procedure.oid, 'EXECUTE'
                       ),
                       has_function_privilege(
                           'campus_ai_school_fact_writer', procedure.oid, 'EXECUTE'
                       ),
                       has_function_privilege(
                           'campus_ai_school_fact_reader', procedure.oid, 'EXECUTE'
                       )
                FROM pg_proc AS procedure
                JOIN pg_roles AS owner ON owner.oid = procedure.proowner
                WHERE procedure.oid = ANY(
                    ARRAY[
                        'app.lock_existing_identity_user_id(text,text,text)'::regprocedure,
                        'app.claim_legacy_identity_user_id(text,text,text,text)'::regprocedure
                    ]
                )
                ORDER BY procedure.oid::regprocedure::text
                """
            ).fetchall()
        )
        location_history_helper_acl = connection.execute(
            """
            SELECT owner.rolname,
                   procedure.prosecdef,
                   procedure.proconfig,
                   has_function_privilege('public', procedure.oid, 'EXECUTE'),
                   has_function_privilege(
                       'campus_ai_runtime', procedure.oid, 'EXECUTE'
                   ),
                   has_function_privilege(
                       'campus_ai_school_fact_writer', procedure.oid, 'EXECUTE'
                   ),
                   has_function_privilege(
                       'campus_ai_school_fact_reader', procedure.oid, 'EXECUTE'
                   )
            FROM pg_proc AS procedure
            JOIN pg_roles AS owner ON owner.oid = procedure.proowner
            WHERE procedure.oid =
                'app.school_fact_location_history_is_resolvable('
                'uuid,uuid,timestamptz)'::regprocedure
            """
        ).fetchone()
        runtime_takeover_privileges = connection.execute(
            """
            SELECT has_table_privilege(
                       'campus_ai_runtime',
                       'public.master_data_import_takeovers',
                       'INSERT'
                   ),
                   has_table_privilege(
                       'campus_ai_runtime',
                       'public.master_data_import_takeovers',
                       'UPDATE'
                   ),
                   has_table_privilege(
                       'campus_ai_runtime',
                       'public.master_data_import_takeovers',
                       'DELETE'
                   ),
                   has_table_privilege(
                       'campus_ai_runtime',
                       'public.master_data_import_takeovers',
                       'TRUNCATE'
                   )
            """
        ).fetchone()
        runtime_takeover_function_execute = connection.execute(
            """
            SELECT procedure.oid::regprocedure::text,
                   has_function_privilege(
                       'campus_ai_runtime', procedure.oid, 'EXECUTE'
                   )
            FROM pg_proc AS procedure
            WHERE procedure.oid = ANY(
                ARRAY[
                    'app.lock_master_data_import_takeover_source(uuid,uuid,uuid)'::regprocedure,
                    'app.lock_master_data_import_takeover_assignment(uuid,uuid,uuid,uuid)'::regprocedure,
                    'app.supersede_master_data_import_takeover_source(uuid,uuid,uuid,text,bigint)'::regprocedure
                ]
            )
            ORDER BY procedure.oid::regprocedure::text
            """
        ).fetchall()

    assert "master_data_import_takeovers" in runtime_dml_revoke_tables
    assert runtime_takeover_privileges == (False, False, False, False)
    assert len(runtime_takeover_function_execute) == 3
    assert all(not row[1] for row in runtime_takeover_function_execute)

    assert isolated_capability_table_grants == set()
    assert isolated_capability_sequence_grants == set()
    expected_isolated_capability_column_grants = {
        ("campus_ai_identity_definer", "users", column_name, "SELECT")
        for column_name in {
            "id",
            "provider_code",
            "provider_tenant_key",
            "external_subject",
        }
    }
    expected_isolated_capability_column_grants |= {
        ("campus_ai_identity_definer", "users", column_name, "UPDATE")
        for column_name in {
            "provider_code",
            "provider_tenant_key",
            "external_subject",
            "updated_at",
        }
    }
    assert isolated_capability_column_grants == expected_isolated_capability_column_grants
    assert security_function_acl == {
        (
            "app.claim_legacy_identity_user_id(text,text,text,text)",
            "campus_ai_identity_definer",
            True,
            True,
            False,
            True,
            False,
            False,
        ),
        (
            "app.lock_existing_identity_user_id(text,text,text)",
            "campus_ai_identity_definer",
            True,
            True,
            False,
            False,
            True,
            False,
        ),
    }
    assert location_history_helper_acl == (
        "campus_ai_context_definer",
        True,
        ["search_path=pg_catalog"],
        False,
        True,
        True,
        True,
    )

    assert {row[0] for row in policy_rows} == set(school_bearing_tables)
    assert all(row[1:3] == (True, True) for row in policy_rows)

    reader_tables = set(governance_reader_tables + r1_reader_tables)
    ordinary_roles = {
        "campus_ai_runtime",
        "campus_ai_school_fact_writer",
        "campus_ai_school_fact_reader",
    }
    for row in policy_rows:
        roles = set(row[6] or ())
        if row[4] is True and roles & ordinary_roles:
            assert str(row[7]).strip().lower() != "true"
            if row[8] is not None:
                assert str(row[8]).strip().lower() != "true"

    for table_name in school_bearing_tables:
        table_policies = {row[3]: row for row in policy_rows if row[0] == table_name}
        guard = table_policies["sf5b_anchor_guard"]
        assert guard[4:7] == (False, "*", ["PUBLIC"])
        assert "require_school_database_anchor" in str(guard[7])
        assert "require_school_database_anchor" in str(guard[8])
        if table_name in retired_repair_tables:
            assert set(table_policies) == {"sf5b_anchor_guard"}

        expected_sf5b_names = {"sf5b_anchor_guard"}
        if table_name in legacy_context_policy_drop_tables:
            assert {"tenant_select", "owner_write"}.isdisjoint(table_policies)
        if table_name in legacy_import_policy_drop_tables:
            assert {
                "tenant_select",
                "tenant_insert",
                "tenant_update",
                "owner_delete",
            }.isdisjoint(table_policies)
        if table_name in app_tables:
            owner = table_policies["sf5b_owner_migration_access"]
            assert owner[4:7] == (
                True,
                "*",
                ["campus_ai_db_owner", "campus_ai_migration"],
            )
            assert "current_user" in str(owner[7]).lower()
            assert "current_user" in str(owner[8]).lower()
            expected_sf5b_names.add("sf5b_owner_migration_access")

        if table_name in writer_tables:
            writer = table_policies["sf5b_writer_context_access"]
            assert writer[4:7] == (True, "*", ["campus_ai_school_fact_writer"])
            assert "require_school_context" in str(writer[7])
            assert "require_school_context" in str(writer[8])
            expected_sf5b_names.add("sf5b_writer_context_access")

        if table_name in legacy_import_policy_drop_tables:
            reactivation_prepare = table_policies["sf5b_school_reactivation_prepare_insert"]
            assert reactivation_prepare[4:7] == (
                True,
                "a",
                ["campus_ai_context_definer"],
            )
            assert reactivation_prepare[7] is None
            assert "require_school_database_anchor" in str(reactivation_prepare[8])
            expected_sf5b_names.add("sf5b_school_reactivation_prepare_insert")
            cleanup_read = table_policies["sf5b_expired_preparation_cleanup_read"]
            assert cleanup_read[4:7] == (
                True,
                "r",
                ["campus_ai_context_definer"],
            )
            assert "require_school_context" in str(cleanup_read[7])
            assert "statement_timestamp" in str(cleanup_read[7])
            assert cleanup_read[8] is None
            expected_sf5b_names.add("sf5b_expired_preparation_cleanup_read")
            cleanup_delete = table_policies["sf5b_expired_preparation_cleanup_delete"]
            assert cleanup_delete[4:7] == (
                True,
                "d",
                ["campus_ai_context_definer"],
            )
            assert "require_school_context" in str(cleanup_delete[7])
            assert "statement_timestamp" in str(cleanup_delete[7])
            assert cleanup_delete[8] is None
            expected_sf5b_names.add("sf5b_expired_preparation_cleanup_delete")

        if table_name in reader_tables:
            reader = table_policies["sf5b_reader_anchor_access"]
            assert reader[4:7] == (True, "r", ["campus_ai_school_fact_reader"])
            assert "require_school_database_anchor" in str(reader[7])
            assert reader[8] is None
            expected_sf5b_names.add("sf5b_reader_anchor_access")
            for policy in table_policies.values():
                policy_roles = set(policy[6] or ())
                if policy[5] in {"*", "r"} and policy_roles & {
                    "PUBLIC",
                    "campus_ai_school_fact_reader",
                }:
                    assert "require_school_context" not in str(policy[7] or "")
                    assert "require_school_context" not in str(policy[8] or "")

        if table_name in legacy_context_policy_drop_tables:
            context_reader = table_policies["sf5b_context_definer_anchor_access"]
            assert context_reader[4:7] == (
                True,
                "r",
                ["campus_ai_context_definer"],
            )
            assert "require_school_database_anchor" in str(context_reader[7])
            assert context_reader[8] is None
            expected_sf5b_names.add("sf5b_context_definer_anchor_access")

        if table_name in shared_support_tables:
            writer_required = table_policies["sf5b_writer_school_required"]
            assert writer_required[4:7] == (
                False,
                "*",
                ["campus_ai_school_fact_writer"],
            )
            assert "require_school_database_anchor" in str(writer_required[7])
            assert "require_school_database_anchor" in str(writer_required[8])
            expected_sf5b_names.add("sf5b_writer_school_required")

        if table_name == "audit_logs":
            audit_append = table_policies["sf5b_audit_definer_append"]
            assert audit_append[4:7] == (True, "a", ["campus_ai_audit_definer"])
            assert audit_append[7] is None
            assert "school_id IS NULL" in str(audit_append[8])
            assert "require_school_database_anchor" in str(audit_append[8])
            expected_sf5b_names.add("sf5b_audit_definer_append")
            audit_returning = table_policies["sf5b_audit_definer_returning"]
            assert audit_returning[4:7] == (
                True,
                "r",
                ["campus_ai_audit_definer"],
            )
            assert "school_id IS NULL" in str(audit_returning[7])
            assert "require_school_database_anchor" in str(audit_returning[7])
            assert audit_returning[8] is None
            expected_sf5b_names.add("sf5b_audit_definer_returning")

        if table_name == "command_idempotency_records":
            runtime_deny = table_policies["sf5b_runtime_no_school_fact_idempotency"]
            assert runtime_deny[4:7] == (False, "*", ["campus_ai_runtime"])
            assert "school_facts.confirm" in str(runtime_deny[7])
            assert "school_facts.confirm" in str(runtime_deny[8])
            expected_sf5b_names.add("sf5b_runtime_no_school_fact_idempotency")

        assert {
            policy_name for policy_name in table_policies if str(policy_name).startswith("sf5b_")
        } == expected_sf5b_names

    assert all(
        any(
            row[0] == table_name and row[4] is True and not str(row[3]).startswith("sf5b_")
            for row in policy_rows
        )
        for table_name in set(guard_only_tables) - set(retired_repair_tables)
    )

    append_only = {
        "audit_logs",
        "command_idempotency_records",
        "master_data_import_command_outcomes",
        "school_fact_change_lineage",
    }
    expected_writer_grants = {
        (table_name, privilege)
        for table_name in writer_tables
        if table_name not in append_only
        for privilege in ("INSERT", "SELECT", "UPDATE")
    }
    expected_writer_grants |= {
        ("master_data_import_command_outcomes", "INSERT"),
        ("school_fact_change_lineage", "INSERT"),
        ("school_fact_change_lineage", "SELECT"),
        ("alembic_version", "SELECT"),
    }
    assert writer_grants == expected_writer_grants

    reader_projection_tables = {
        "school_people",
        "organization_units",
        "organization_unit_versions",
        "staff_assignments",
        "staff_assignment_versions",
        "campus_locations",
        "campus_location_versions",
        "official_contact_points",
    }
    assert reader_grants == {
        (table_name, "SELECT")
        for table_name in set(governance_reader_tables)
        | reader_projection_tables
        | {"alembic_version"}
    }

    expected_audit_columns = {
        "actor_user_id",
        "school_id",
        "actor_person_id",
        "actor_assignment_id",
        "actor_assignment_version",
        "actor_context_type",
        "idempotency_key_hash",
        "event_type",
        "target_type",
        "target_id",
        "before_summary",
        "after_summary",
        "request_id",
        "reason",
    }
    expected_idempotency_columns = {
        "id",
        "school_id",
        "action_code",
        "actor_user_id",
        "actor_person_id",
        "actor_context_type",
        "actor_context_key",
        "actor_assignment_id",
        "actor_assignment_version",
        "resource_type",
        "resource_id",
        "idempotency_key_hash",
        "request_sha256",
        "status",
        "result_type",
        "result_payload",
    }
    assert writer_insert_columns == {
        ("audit_logs", column_name) for column_name in expected_audit_columns
    } | {
        ("command_idempotency_records", column_name) for column_name in expected_idempotency_columns
    }


@pytest.mark.integration
def test_bootstrap_repeat_restores_exact_location_history_helper_acl(
    current_master_data_schema: PostgresRoles,
) -> None:
    postgres_roles = current_master_data_schema
    signature = "app.school_fact_location_history_is_resolvable(uuid,uuid,timestamptz)"

    def helper_acl() -> tuple[object, ...] | None:
        with postgres_roles.admin_connection() as connection:
            return connection.execute(
                """
                SELECT owner.rolname,
                       procedure.prosecdef,
                       procedure.proconfig,
                       has_function_privilege('public', procedure.oid, 'EXECUTE'),
                       has_function_privilege(
                           'campus_ai_runtime', procedure.oid, 'EXECUTE'
                       ),
                       has_function_privilege(
                           'campus_ai_school_fact_writer', procedure.oid, 'EXECUTE'
                       ),
                       has_function_privilege(
                           'campus_ai_school_fact_reader', procedure.oid, 'EXECUTE'
                       )
                FROM pg_proc AS procedure
                JOIN pg_roles AS owner ON owner.oid = procedure.proowner
                WHERE procedure.oid = %s::regprocedure
                """,
                (signature,),
            ).fetchone()

    def reader_revision18_writer_helpers() -> list[str]:
        with postgres_roles.admin_connection() as connection:
            return [
                row[0]
                for row in connection.execute(
                    """
                    SELECT procedure.proname
                    FROM pg_proc AS procedure
                    WHERE procedure.oid = ANY(
                        ARRAY[
                            'app.school_fact_content_version_terminal('
                            'uuid,text,uuid,bigint)'::regprocedure,
                            'app.school_fact_root_is_effective_at('
                            'uuid,text,uuid,timestamptz)'::regprocedure,
                            'app.school_fact_location_history_is_resolvable('
                            'uuid,uuid,timestamptz)'::regprocedure,
                            'app.school_fact_location_history_interval_is_resolvable('
                            'uuid,uuid,timestamptz,timestamptz)'::regprocedure
                        ]
                    )
                      AND has_function_privilege(
                          'campus_ai_school_fact_reader', procedure.oid, 'EXECUTE'
                      )
                    ORDER BY procedure.proname
                    """
                ).fetchall()
            ]

    expected_acl = (
        "campus_ai_context_definer",
        True,
        ["search_path=pg_catalog"],
        False,
        True,
        True,
        True,
    )
    postgres_roles.rerun_bootstrap()
    assert helper_acl() == expected_acl
    assert reader_revision18_writer_helpers() == ["school_fact_location_history_is_resolvable"]

    try:
        with postgres_roles.admin_connection() as connection:
            connection.execute(
                sql.SQL("REVOKE EXECUTE ON FUNCTION {} FROM campus_ai_school_fact_reader").format(
                    sql.SQL(signature)
                )
            )
            connection.execute(
                sql.SQL("GRANT EXECUTE ON FUNCTION {} TO PUBLIC").format(sql.SQL(signature))
            )
        assert helper_acl() != expected_acl

        assert postgres_roles.rerun_bootstrap().returncode == 0
        assert postgres_roles.rerun_bootstrap().returncode == 0
        assert helper_acl() == expected_acl
        assert reader_revision18_writer_helpers() == ["school_fact_location_history_is_resolvable"]
    finally:
        with postgres_roles.admin_connection() as connection:
            connection.execute(
                sql.SQL("REVOKE EXECUTE ON FUNCTION {} FROM PUBLIC").format(sql.SQL(signature))
            )
            connection.execute(
                sql.SQL("GRANT EXECUTE ON FUNCTION {} TO campus_ai_school_fact_reader").format(
                    sql.SQL(signature)
                )
            )
        postgres_roles.rerun_bootstrap()


@pytest.mark.integration
def test_bootstrap_repeat_removes_legacy_runtime_takeover_writer_grants(
    current_master_data_schema: PostgresRoles,
) -> None:
    postgres_roles = current_master_data_schema
    takeover_functions = (
        "app.lock_master_data_import_takeover_source(uuid,uuid,uuid)",
        "app.lock_master_data_import_takeover_assignment(uuid,uuid,uuid,uuid)",
        "app.supersede_master_data_import_takeover_source(uuid,uuid,uuid,text,bigint)",
    )

    def runtime_takeover_acl() -> tuple[
        tuple[bool, bool, bool, bool],
        tuple[tuple[str, bool], ...],
    ]:
        with postgres_roles.admin_connection() as connection:
            table_acl = connection.execute(
                """
                SELECT has_table_privilege(
                           'campus_ai_runtime',
                           'public.master_data_import_takeovers',
                           'INSERT'
                       ),
                       has_table_privilege(
                           'campus_ai_runtime',
                           'public.master_data_import_takeovers',
                           'UPDATE'
                       ),
                       has_table_privilege(
                           'campus_ai_runtime',
                           'public.master_data_import_takeovers',
                           'DELETE'
                       ),
                       has_table_privilege(
                           'campus_ai_runtime',
                           'public.master_data_import_takeovers',
                           'TRUNCATE'
                       )
                """
            ).fetchone()
            function_acl = connection.execute(
                """
                SELECT procedure.oid::regprocedure::text,
                       has_function_privilege(
                           'campus_ai_runtime', procedure.oid, 'EXECUTE'
                       )
                FROM pg_proc AS procedure
                WHERE procedure.oid = ANY(%s::regprocedure[])
                ORDER BY procedure.oid::regprocedure::text
                """,
                (list(takeover_functions),),
            ).fetchall()
        assert table_acl is not None
        return (
            (bool(table_acl[0]), bool(table_acl[1]), bool(table_acl[2]), bool(table_acl[3])),
            tuple((str(signature), bool(can_execute)) for signature, can_execute in function_acl),
        )

    postgres_roles.rerun_bootstrap()
    expected_closed = (
        (False, False, False, False),
        tuple((signature, False) for signature in sorted(takeover_functions)),
    )
    assert runtime_takeover_acl() == expected_closed

    try:
        with postgres_roles.admin_connection() as connection:
            connection.execute(
                """
                GRANT INSERT, UPDATE, DELETE, TRUNCATE
                    ON TABLE public.master_data_import_takeovers
                    TO campus_ai_runtime
                """
            )
            for signature in takeover_functions:
                connection.execute(
                    sql.SQL("GRANT EXECUTE ON FUNCTION {} TO campus_ai_runtime").format(
                        sql.SQL(signature)
                    )
                )

        rogue_table_acl, rogue_function_acl = runtime_takeover_acl()
        assert rogue_table_acl == (True, True, True, True)
        assert rogue_function_acl == tuple(
            (signature, True) for signature in sorted(takeover_functions)
        )

        postgres_roles.rerun_bootstrap()
        assert runtime_takeover_acl() == expected_closed
    finally:
        with postgres_roles.admin_connection() as connection:
            connection.execute(
                "REVOKE ALL PRIVILEGES ON TABLE public.master_data_import_takeovers "
                "FROM campus_ai_runtime"
            )
            for signature in takeover_functions:
                connection.execute(
                    sql.SQL("REVOKE ALL ON FUNCTION {} FROM campus_ai_runtime").format(
                        sql.SQL(signature)
                    )
                )
        postgres_roles.rerun_bootstrap()


@pytest.mark.integration
def test_bootstrap_repeat_removes_isolated_role_table_column_and_sequence_acl_drift(
    current_master_data_schema: PostgresRoles,
) -> None:
    postgres_roles = current_master_data_schema
    isolated_roles = (
        "campus_ai_school_fact_writer",
        "campus_ai_school_fact_reader",
        "campus_ai_identity_definer",
    )

    def isolated_acl_catalog() -> set[tuple[object, ...]]:
        with postgres_roles.admin_connection() as connection:
            rows = connection.execute(
                """
                SELECT grantee.rolname,
                       CASE relation.relkind
                           WHEN 'S' THEN 'sequence'
                           ELSE 'table'
                       END AS object_kind,
                       relation.relname,
                       NULL::text AS column_name,
                       acl.privilege_type
                FROM pg_class AS relation
                JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
                CROSS JOIN LATERAL aclexplode(relation.relacl) AS acl
                JOIN pg_roles AS grantee ON grantee.oid = acl.grantee
                WHERE namespace.nspname = 'public'
                  AND relation.relkind IN ('r', 'p', 'S')
                  AND grantee.rolname = ANY(%s)
                UNION ALL
                SELECT grantee.rolname,
                       'column',
                       relation.relname,
                       attribute.attname,
                       acl.privilege_type
                FROM pg_class AS relation
                JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
                JOIN pg_attribute AS attribute ON attribute.attrelid = relation.oid
                CROSS JOIN LATERAL aclexplode(attribute.attacl) AS acl
                JOIN pg_roles AS grantee ON grantee.oid = acl.grantee
                WHERE namespace.nspname = 'public'
                  AND relation.relkind IN ('r', 'p')
                  AND attribute.attnum > 0
                  AND NOT attribute.attisdropped
                  AND grantee.rolname = ANY(%s)
                ORDER BY 1, 2, 3, 4 NULLS FIRST, 5
                """,
                (list(isolated_roles), list(isolated_roles)),
            ).fetchall()
        return set(rows)

    postgres_roles.rerun_bootstrap()
    baseline = isolated_acl_catalog()
    expected_baseline_samples = {
        ("campus_ai_school_fact_writer", "table", "schools", None, "INSERT"),
        ("campus_ai_school_fact_reader", "table", "governance_grants", None, "SELECT"),
        ("campus_ai_identity_definer", "column", "users", "provider_code", "SELECT"),
        ("campus_ai_school_fact_writer", "sequence", "audit_logs_id_seq", None, "USAGE"),
    }
    assert expected_baseline_samples <= baseline

    rogue_grants = {
        ("campus_ai_school_fact_writer", "table", "schools", None, "DELETE"),
        ("campus_ai_school_fact_writer", "column", "schools", "display_name", "REFERENCES"),
        ("campus_ai_school_fact_writer", "sequence", "audit_logs_id_seq", None, "SELECT"),
        ("campus_ai_school_fact_reader", "table", "schools", None, "UPDATE"),
        ("campus_ai_school_fact_reader", "column", "schools", "display_name", "INSERT"),
        ("campus_ai_school_fact_reader", "sequence", "audit_logs_id_seq", None, "USAGE"),
        ("campus_ai_identity_definer", "table", "schools", None, "SELECT"),
        ("campus_ai_identity_definer", "column", "schools", "display_name", "UPDATE"),
        ("campus_ai_identity_definer", "sequence", "audit_logs_id_seq", None, "SELECT"),
    }

    try:
        with postgres_roles.admin_connection() as connection:
            connection.execute(
                """
                GRANT DELETE ON TABLE public.schools TO campus_ai_school_fact_writer;
                GRANT REFERENCES (display_name) ON TABLE public.schools
                    TO campus_ai_school_fact_writer;
                GRANT SELECT ON SEQUENCE public.audit_logs_id_seq
                    TO campus_ai_school_fact_writer;
                GRANT UPDATE ON TABLE public.schools TO campus_ai_school_fact_reader;
                GRANT INSERT (display_name) ON TABLE public.schools
                    TO campus_ai_school_fact_reader;
                GRANT USAGE ON SEQUENCE public.audit_logs_id_seq
                    TO campus_ai_school_fact_reader;
                GRANT SELECT ON TABLE public.schools TO campus_ai_identity_definer;
                GRANT UPDATE (display_name) ON TABLE public.schools
                    TO campus_ai_identity_definer;
                GRANT SELECT ON SEQUENCE public.audit_logs_id_seq
                    TO campus_ai_identity_definer;
                """
            )
        assert rogue_grants <= isolated_acl_catalog()

        postgres_roles.rerun_bootstrap()
        assert isolated_acl_catalog() == baseline
    finally:
        with postgres_roles.admin_connection() as connection:
            connection.execute(
                """
                REVOKE DELETE ON TABLE public.schools FROM campus_ai_school_fact_writer;
                REVOKE REFERENCES (display_name) ON TABLE public.schools
                    FROM campus_ai_school_fact_writer;
                REVOKE SELECT ON SEQUENCE public.audit_logs_id_seq
                    FROM campus_ai_school_fact_writer;
                REVOKE UPDATE ON TABLE public.schools FROM campus_ai_school_fact_reader;
                REVOKE INSERT (display_name) ON TABLE public.schools
                    FROM campus_ai_school_fact_reader;
                REVOKE USAGE ON SEQUENCE public.audit_logs_id_seq
                    FROM campus_ai_school_fact_reader;
                REVOKE SELECT ON TABLE public.schools FROM campus_ai_identity_definer;
                REVOKE UPDATE (display_name) ON TABLE public.schools
                    FROM campus_ai_identity_definer;
                REVOKE SELECT ON SEQUENCE public.audit_logs_id_seq
                    FROM campus_ai_identity_definer;
                """
            )
        postgres_roles.rerun_bootstrap()


@pytest.mark.integration
def test_bootstrap_repeat_removes_runtime_and_public_school_fact_acl_drift(
    current_master_data_schema: PostgresRoles,
) -> None:
    postgres_roles = current_master_data_schema
    migration = runpy.run_path(str(REVISION20_MIGRATION))
    runtime_revoke_tables = set(migration["_SCHOOL_FACT_RUNTIME_DML_REVOKE_TABLES"])
    shared_support_tables = set(migration["_SHARED_SUPPORT_TABLES"])
    protected_tables = sorted(runtime_revoke_tables - shared_support_tables)

    def protected_acl_catalog() -> set[tuple[object, ...]]:
        with postgres_roles.admin_connection() as connection:
            rows = connection.execute(
                """
                SELECT COALESCE(grantee.rolname, 'PUBLIC'),
                       'table', relation.relname, NULL::text, acl.privilege_type
                FROM pg_class AS relation
                JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
                CROSS JOIN LATERAL aclexplode(relation.relacl) AS acl
                LEFT JOIN pg_roles AS grantee ON grantee.oid = acl.grantee
                WHERE namespace.nspname = 'public'
                  AND relation.relname = ANY(%s)
                  AND (acl.grantee = 0 OR grantee.rolname = 'campus_ai_runtime')
                UNION ALL
                SELECT COALESCE(grantee.rolname, 'PUBLIC'),
                       'column', relation.relname, attribute.attname, acl.privilege_type
                FROM pg_class AS relation
                JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
                JOIN pg_attribute AS attribute ON attribute.attrelid = relation.oid
                CROSS JOIN LATERAL aclexplode(attribute.attacl) AS acl
                LEFT JOIN pg_roles AS grantee ON grantee.oid = acl.grantee
                WHERE namespace.nspname = 'public'
                  AND relation.relname = ANY(%s)
                  AND attribute.attnum > 0
                  AND NOT attribute.attisdropped
                  AND (acl.grantee = 0 OR grantee.rolname = 'campus_ai_runtime')
                ORDER BY 1, 2, 3, 4 NULLS FIRST, 5
                """,
                (protected_tables, protected_tables),
            ).fetchall()
        return set(rows)

    postgres_roles.rerun_bootstrap()
    baseline = protected_acl_catalog()
    assert baseline == set()
    rogue_grants = {
        ("PUBLIC", "column", "school_people", "status", "SELECT"),
        ("PUBLIC", "table", "school_people", None, "SELECT"),
        ("campus_ai_runtime", "column", "school_people", "status", "UPDATE"),
        ("campus_ai_runtime", "table", "school_people", None, "DELETE"),
    }

    try:
        with postgres_roles.admin_connection() as connection:
            connection.execute(
                """
                GRANT SELECT ON TABLE public.school_people TO PUBLIC;
                GRANT SELECT (status) ON TABLE public.school_people TO PUBLIC;
                GRANT DELETE ON TABLE public.school_people TO campus_ai_runtime;
                GRANT UPDATE (status) ON TABLE public.school_people TO campus_ai_runtime;
                """
            )
        assert rogue_grants <= protected_acl_catalog()

        postgres_roles.rerun_bootstrap()
        postgres_roles.rerun_bootstrap()
        assert protected_acl_catalog() == baseline
    finally:
        with postgres_roles.admin_connection() as connection:
            connection.execute(
                """
                REVOKE SELECT ON TABLE public.school_people FROM PUBLIC CASCADE;
                REVOKE SELECT (status) ON TABLE public.school_people FROM PUBLIC CASCADE;
                REVOKE DELETE ON TABLE public.school_people FROM campus_ai_runtime CASCADE;
                REVOKE UPDATE (status) ON TABLE public.school_people
                    FROM campus_ai_runtime CASCADE;
                """
            )
        postgres_roles.rerun_bootstrap()


@pytest.mark.integration
def test_school_database_anchor_is_concurrent_immutable_and_restore_bound(
    isolated_migration_schema: PostgresRoles,
    postgres_roles: PostgresRoles,
) -> None:
    roles = isolated_migration_schema
    school_ids = (uuid4(), uuid4())
    system_identifier = roles.disposable_cluster_system_identifier
    start = Barrier(2)

    def insert_first_root(school_id: object) -> tuple[str, str]:
        try:
            start.wait(timeout=5)
            with roles.migration_connection() as connection:
                connection.execute("SET ROLE campus_ai_db_owner")
                connection.execute(
                    """
                    INSERT INTO public.schools (
                        id, source_key, school_code, display_name,
                        timezone_name, status, version
                    ) VALUES (%s, %s, %s, 'SF5B Concurrent Root',
                              'Asia/Shanghai', 'active', 1)
                    """,
                    (school_id, f"sf5b:{school_id}", f"sf5b-{school_id}"),
                )
            return "ok", str(school_id)
        except psycopg.Error as error:
            return "error", str(error.sqlstate)

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(insert_first_root, school_ids))

    assert sorted(outcome[0] for outcome in outcomes) == ["error", "ok"]
    error_states = [outcome[1] for outcome in outcomes if outcome[0] == "error"]
    assert len(error_states) == 1
    assert error_states[0] in {"23505", "23514", "55000"}
    winning_school = next(
        school_id for school_id in school_ids if ("ok", str(school_id)) in outcomes
    )

    with roles.migration_connection() as connection:
        connection.execute("SET ROLE campus_ai_db_owner")
        catalog = connection.execute(
            """
            SELECT (SELECT count(*) FROM public.schools),
                   (SELECT count(*) FROM public.school_database_anchor),
                   anchor.anchor_id, anchor.school_id
            FROM public.school_database_anchor AS anchor
            WHERE anchor.singleton_id = 1
            """
        ).fetchone()
        assert catalog is not None
        assert catalog[:2] == (1, 1)
        winning_anchor = catalog[2]
        assert catalog[3] == winning_school
        assert connection.execute(
            "SELECT app.assert_school_database_anchor(%s, %s, %s)",
            (winning_anchor, winning_school, system_identifier),
        ).fetchone() == (winning_school,)

        for expected in (
            (uuid4(), winning_school, system_identifier),
            (winning_anchor, uuid4(), system_identifier),
            (winning_anchor, winning_school, "9999999999999999999"),
        ):
            with pytest.raises(psycopg.Error) as wrong_identity:
                connection.execute(
                    "SELECT app.assert_school_database_anchor(%s, %s, %s)",
                    expected,
                )
            assert wrong_identity.value.sqlstate == "55000"

        with pytest.raises(psycopg.Error) as second_root:
            connection.execute(
                """
                INSERT INTO public.schools (
                    id, source_key, school_code, display_name, timezone_name, status, version
                ) VALUES (%s, 'sf5b-second-school', 'sf5b-second', 'Second School',
                          'Asia/Shanghai', 'active', 1)
                """,
                (uuid4(),),
            )
        assert second_root.value.sqlstate == "23514"

        for statement in (
            (
                "UPDATE public.school_database_anchor SET anchor_id = "
                "'00000000-0000-0000-0000-000000000001'::uuid"
            ),
            "DELETE FROM public.school_database_anchor",
            "TRUNCATE public.school_database_anchor",
        ):
            with pytest.raises(psycopg.Error) as immutable:
                connection.execute(statement)
            assert immutable.value.sqlstate == "55000"

    clone_name = f"campus_ai_wrong_restore_{uuid4().hex}_test"
    source_name = str(roles.admin_connection_parameters["dbname"])
    assert len(clone_name.encode("ascii")) <= 63
    try:
        with postgres_roles.admin_connection() as connection:
            connection.execute(
                sql.SQL("CREATE DATABASE {} WITH OWNER {} TEMPLATE {}").format(
                    sql.Identifier(clone_name),
                    sql.Identifier("campus_ai_db_owner"),
                    sql.Identifier(source_name),
                )
            )
        clone_parameters = {
            **roles.migration_connection_parameters,
            "dbname": clone_name,
        }
        with psycopg.connect(**clone_parameters, autocommit=True) as connection:
            connection.execute("SET ROLE campus_ai_db_owner")
            with pytest.raises(psycopg.Error) as wrong_database:
                connection.execute(
                    "SELECT app.assert_school_database_anchor(%s, %s, %s)",
                    (winning_anchor, winning_school, system_identifier),
                )
            assert wrong_database.value.sqlstate == "55000"
    finally:
        with postgres_roles.admin_connection() as connection:
            connection.execute(
                "SELECT pg_catalog.pg_terminate_backend(pid) "
                "FROM pg_catalog.pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_catalog.pg_backend_pid()",
                (clone_name,),
            )
            connection.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                    sql.Identifier(clone_name)
                )
            )


def _provision_runtime_gate_anchor(
    roles: PostgresRoles,
) -> tuple[UUID, UUID, str]:
    school_id = uuid4()
    with roles.migration_connection() as connection:
        connection.execute("SET ROLE campus_ai_db_owner")
        connection.execute(
            """
            INSERT INTO public.schools (
                id, source_key, school_code, display_name,
                timezone_name, status, version
            ) VALUES (%s, %s, %s, 'SF5B Runtime Gate School',
                      'Asia/Shanghai', 'active', 1)
            """,
            (school_id, f"sf5b-runtime:{school_id}", f"sf5b-runtime-{school_id}"),
        )
        anchor = connection.execute(
            """
            SELECT anchor_id, school_id, cluster_system_identifier
            FROM public.school_database_anchor
            WHERE singleton_id = 1
            """
        ).fetchone()
    assert anchor is not None
    assert anchor[1] == school_id
    return UUID(str(anchor[0])), UUID(str(anchor[1])), str(anchor[2])


def _runtime_gate_settings(
    roles: PostgresRoles,
    *,
    anchor_id: UUID,
    school_id: UUID,
    system_identifier: str,
    connection_parameters: dict[str, object] | None = None,
) -> Settings:
    runtime = connection_parameters or roles.runtime_connection_parameters
    return Settings.model_validate(
        {
            "app_env": "test",
            "postgres_host": str(runtime["host"]),
            "postgres_port": int(str(runtime["port"])),
            "postgres_db": str(runtime["dbname"]),
            "postgres_user": str(runtime["user"]),
            "postgres_password": str(runtime["password"]),
            "postgres_expected_anchor_id": anchor_id,
            "postgres_expected_school_id": school_id,
            "postgres_expected_system_identifier": system_identifier,
            "postgres_pool_size": 1,
            "postgres_max_overflow": 0,
            "rls_context_signing_key": "sf5b-runtime-gate-test-signing-key-32-bytes",
            "redis_port": 6379,
            "redis_password": "sf5b-runtime-gate-unused-redis-password",
        }
    )


@pytest.mark.integration
def test_runtime_pool_accepts_exact_role_head_and_full_database_anchor(
    current_master_data_schema: PostgresRoles,
) -> None:
    roles = current_master_data_schema
    anchor_id, school_id, system_identifier = _provision_runtime_gate_anchor(roles)
    settings = _runtime_gate_settings(
        roles,
        anchor_id=anchor_id,
        school_id=school_id,
        system_identifier=system_identifier,
    )
    engine = build_engine(settings)
    try:
        with engine.connect() as connection:
            facts = connection.execute(
                text(
                    """
                    SELECT current_user::text,
                           session_user::text,
                           (SELECT version_num FROM public.alembic_version),
                           app.assert_school_database_anchor(
                               :anchor_id, :school_id, :system_id
                           )
                    """
                ),
                {
                    "anchor_id": anchor_id,
                    "school_id": school_id,
                    "system_id": system_identifier,
                },
            ).one()
        assert facts == (
            "campus_ai_runtime",
            "campus_ai_runtime",
            "20260824_20",
            school_id,
        )
    finally:
        engine.dispose()


@pytest.mark.integration
def test_runtime_pool_rejects_non_runtime_postgres_user_and_role(
    current_master_data_schema: PostgresRoles,
) -> None:
    roles = current_master_data_schema
    anchor_id, school_id, system_identifier = _provision_runtime_gate_anchor(roles)
    settings = _runtime_gate_settings(
        roles,
        anchor_id=anchor_id,
        school_id=school_id,
        system_identifier=system_identifier,
        connection_parameters=roles.writer_connection_parameters,
    )
    engine = build_engine(settings)
    try:
        with pytest.raises(RuntimeDatabaseStartupRejected):
            with engine.connect():
                pass
    finally:
        engine.dispose()


@pytest.mark.integration
def test_runtime_pool_rejects_each_wrong_full_database_anchor_component(
    current_master_data_schema: PostgresRoles,
) -> None:
    roles = current_master_data_schema
    anchor_id, school_id, system_identifier = _provision_runtime_gate_anchor(roles)
    wrong_identities = (
        (uuid4(), school_id, system_identifier),
        (anchor_id, uuid4(), system_identifier),
        (anchor_id, school_id, "9999999999999999999"),
    )

    for wrong_anchor_id, wrong_school_id, wrong_system_identifier in wrong_identities:
        settings = _runtime_gate_settings(
            roles,
            anchor_id=wrong_anchor_id,
            school_id=wrong_school_id,
            system_identifier=wrong_system_identifier,
        )
        engine = build_engine(settings)
        try:
            with pytest.raises(RuntimeDatabaseStartupRejected):
                with engine.connect():
                    pass
        finally:
            engine.dispose()


@pytest.mark.integration
def test_runtime_pool_rejects_wrong_alembic_head(
    current_master_data_schema: PostgresRoles,
) -> None:
    roles = current_master_data_schema
    anchor_id, school_id, system_identifier = _provision_runtime_gate_anchor(roles)
    with roles.migration_connection() as connection:
        connection.execute("SET ROLE campus_ai_db_owner")
        connection.execute("UPDATE public.alembic_version SET version_num = '20260823_19'")

    settings = _runtime_gate_settings(
        roles,
        anchor_id=anchor_id,
        school_id=school_id,
        system_identifier=system_identifier,
    )
    engine = build_engine(settings)
    try:
        with pytest.raises(RuntimeDatabaseStartupRejected):
            with engine.connect():
                pass
    finally:
        engine.dispose()


@pytest.mark.integration
def test_runtime_pool_hard_invalidates_dirty_connection_before_reuse(
    current_master_data_schema: PostgresRoles,
) -> None:
    roles = current_master_data_schema
    anchor_id, school_id, system_identifier = _provision_runtime_gate_anchor(roles)
    settings = _runtime_gate_settings(
        roles,
        anchor_id=anchor_id,
        school_id=school_id,
        system_identifier=system_identifier,
    )
    engine = build_engine(settings)
    try:
        with engine.connect() as connection:
            dirty_backend_pid = connection.execute(text("SELECT pg_backend_pid()")).scalar_one()
            connection.execute(text("SET SESSION app.request_id = 'sf5b-dirty-context'"))
            connection.commit()

        with pytest.raises(RuntimeDatabaseStartupRejected):
            with engine.connect():
                pass

        with roles.admin_connection() as connection:
            dirty_backend_is_gone = connection.execute(
                "SELECT NOT EXISTS (SELECT 1 FROM pg_stat_activity WHERE pid = %s)",
                (dirty_backend_pid,),
            ).fetchone()
        assert dirty_backend_is_gone == (True,)

        with engine.connect() as connection:
            clean_backend_pid = connection.execute(text("SELECT pg_backend_pid()")).scalar_one()
            assert connection.execute(
                text("SELECT current_setting('app.request_id', true)")
            ).scalar_one() in (None, "")
        assert clean_backend_pid != dirty_backend_pid
    finally:
        engine.dispose()
