from __future__ import annotations

import base64
import hashlib
import hmac
import os
import shutil
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from sqlalchemy import create_engine
from sqlalchemy.engine import URL
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from app.core.settings import get_settings
from app.db.connection import get_engine, get_session_factory
from app.db.migration_settings import get_migration_settings
from app.db.schema_contract import DatabaseSchemaContract

PROJECT_ROOT = Path(__file__).resolve().parents[4]
BOOTSTRAP_SCRIPT = PROJECT_ROOT / "infra" / "postgres" / "001-campus-ai-roles.sh"
INTEGRATION_FLAG = "RUN_POSTGRES_INTEGRATION"
DISPOSABLE_CLUSTER_SYSTEM_IDENTIFIER_ENV = "MASTER_DATA_DISPOSABLE_CLUSTER_SYSTEM_IDENTIFIER"
_TEST_CONTEXT_SIGNING_KEY = "master-data-schema-context-signing-key-32-bytes"


def _derive_cluster_test_password(
    *, bootstrap_password: str, system_identifier: str, role_name: str
) -> str:
    """Derive one stable disposable-cluster credential per database role."""

    digest = hmac.new(
        bootstrap_password.encode("utf-8"),
        f"campus-ai-pytest-role-v1|{system_identifier}|{role_name}".encode(),
        hashlib.sha256,
    ).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


@dataclass(frozen=True, slots=True)
class RoleFacts:
    can_login: bool
    is_superuser: bool
    can_create_db: bool
    can_create_role: bool
    can_replicate: bool
    bypass_rls: bool
    inherits: bool
    member_of: frozenset[str]


@dataclass(slots=True)
class PostgresRoles:
    admin_connection_parameters: dict[str, Any]
    runtime_connection_parameters: dict[str, Any]
    migration_connection_parameters: dict[str, Any]
    writer_connection_parameters: dict[str, Any]
    reader_connection_parameters: dict[str, Any]
    bootstrap_command: tuple[str, ...]
    bootstrap_environment: dict[str, str]
    protected_test_secrets: tuple[str, ...]
    bootstrap_output: str
    disposable_cluster_system_identifier: str
    server_version_num: int

    @contextmanager
    def admin_connection(self) -> Iterator[psycopg.Connection[Any]]:
        with psycopg.connect(**self.admin_connection_parameters, autocommit=True) as connection:
            connection.execute("SET ROLE NONE")
            connection.execute("SET search_path = pg_catalog")
            yield connection

    @contextmanager
    def runtime_connection(self) -> Iterator[psycopg.Connection[Any]]:
        with psycopg.connect(**self.runtime_connection_parameters, autocommit=True) as connection:
            yield connection

    @contextmanager
    def migration_connection(self) -> Iterator[psycopg.Connection[Any]]:
        with psycopg.connect(**self.migration_connection_parameters, autocommit=True) as connection:
            yield connection

    def role(self, role_name: str) -> RoleFacts:
        with self.admin_connection() as connection:
            row = connection.execute(
                """
                SELECT rolcanlogin,
                       rolsuper,
                       rolcreatedb,
                       rolcreaterole,
                       rolreplication,
                       rolbypassrls,
                       rolinherit
                FROM pg_roles
                WHERE rolname = %s
                """,
                (role_name,),
            ).fetchone()
            if row is None:
                raise AssertionError(f"数据库角色不存在：{role_name}")
            membership_rows = connection.execute(
                """
                SELECT parent.rolname
                FROM pg_auth_members AS membership
                JOIN pg_roles AS parent ON parent.oid = membership.roleid
                JOIN pg_roles AS member ON member.oid = membership.member
                WHERE member.rolname = %s
                """,
                (role_name,),
            ).fetchall()
            memberships = frozenset(membership[0] for membership in membership_rows)
        return RoleFacts(*row, member_of=memberships)

    def membership_options(self, member_name: str) -> dict[str, tuple[bool, bool, bool]]:
        with self.admin_connection() as connection:
            rows = connection.execute(
                """
                SELECT parent.rolname,
                       membership.admin_option,
                       membership.inherit_option,
                       membership.set_option
                FROM pg_auth_members AS membership
                JOIN pg_roles AS parent ON parent.oid = membership.roleid
                JOIN pg_roles AS member ON member.oid = membership.member
                WHERE member.rolname = %s
                """,
                (member_name,),
            ).fetchall()
        return {role_name: (admin, inherit, can_set) for role_name, admin, inherit, can_set in rows}

    def database_owner(self) -> str:
        with self.admin_connection() as connection:
            owner = connection.execute(
                """
                SELECT owner.rolname
                FROM pg_database AS database
                JOIN pg_roles AS owner ON owner.oid = database.datdba
                WHERE database.datname = current_database()
                """
            ).fetchone()
        if owner is None:
            raise AssertionError("当前测试数据库没有可解析的 Owner")
        return owner[0]

    def public_can_connect(self) -> bool:
        with self.admin_connection() as connection:
            row = connection.execute(
                """
                SELECT COALESCE(bool_or(
                    acl.grantee = 0 AND acl.privilege_type = 'CONNECT'
                ), false)
                FROM pg_database AS database
                CROSS JOIN LATERAL aclexplode(
                    COALESCE(database.datacl, acldefault('d', database.datdba))
                ) AS acl
                WHERE database.datname = current_database()
                """
            ).fetchone()
            if row is None:
                raise AssertionError("无法读取测试数据库的 PUBLIC CONNECT 权限")
            return bool(row[0])

    def role_can_connect(self, role_name: str) -> bool:
        with self.admin_connection() as connection:
            row = connection.execute(
                "SELECT has_database_privilege(%s, current_database(), 'CONNECT')",
                (role_name,),
            ).fetchone()
            if row is None:
                raise AssertionError(f"无法读取数据库 CONNECT 权限：{role_name}")
            return bool(row[0])

    def role_can_use_schema(self, role_name: str, schema_name: str) -> bool:
        with self.admin_connection() as connection:
            row = connection.execute(
                "SELECT has_schema_privilege(%s, %s, 'USAGE')",
                (role_name, schema_name),
            ).fetchone()
            if row is None:
                raise AssertionError(f"无法读取 {schema_name} Schema USAGE 权限：{role_name}")
            return bool(row[0])

    def role_can_create_in_schema(self, role_name: str, schema_name: str) -> bool:
        with self.admin_connection() as connection:
            row = connection.execute(
                "SELECT has_schema_privilege(%s, %s, 'CREATE')",
                (role_name, schema_name),
            ).fetchone()
            if row is None:
                raise AssertionError(f"无法读取 {schema_name} Schema CREATE 权限：{role_name}")
            return bool(row[0])

    def role_can_create_in_public_schema(self, role_name: str) -> bool:
        return self.role_can_create_in_schema(role_name, "public")

    def role_can_use_public_schema(self, role_name: str) -> bool:
        with self.admin_connection() as connection:
            row = connection.execute(
                "SELECT has_schema_privilege(%s, 'public', 'USAGE')",
                (role_name,),
            ).fetchone()
            if row is None:
                raise AssertionError(f"无法读取 public Schema USAGE 权限：{role_name}")
            return bool(row[0])

    def role_can_create_temporary_tables(self, role_name: str) -> bool:
        with self.admin_connection() as connection:
            row = connection.execute(
                "SELECT has_database_privilege(%s, current_database(), 'TEMPORARY')",
                (role_name,),
            ).fetchone()
            if row is None:
                raise AssertionError(f"无法读取数据库 TEMPORARY 权限：{role_name}")
            return bool(row[0])

    def role_can_set_role(self, member_name: str, target_name: str) -> bool:
        with self.admin_connection() as connection:
            row = connection.execute(
                "SELECT pg_has_role(%s, %s, 'SET')",
                (member_name, target_name),
            ).fetchone()
            if row is None:
                raise AssertionError(f"无法读取角色切换权限：{member_name} -> {target_name}")
            return bool(row[0])

    def application_startup_role_defaults(self) -> list[tuple[str | None, str | None, str]]:
        with self.admin_connection() as connection:
            connection.execute("SET ROLE NONE")
            rows = connection.execute(
                """
                SELECT role.rolname, database.datname, setting.value
                FROM pg_db_role_setting AS configured
                LEFT JOIN pg_roles AS role ON role.oid = configured.setrole
                LEFT JOIN pg_database AS database ON database.oid = configured.setdatabase
                CROSS JOIN LATERAL unnest(configured.setconfig) AS setting(value)
                WHERE setting.value LIKE 'role=%%'
                  AND (
                      role.rolname IN (
                          'campus_ai_db_owner',
                          'campus_ai_context_definer',
                          'campus_ai_audit_definer',
                          'campus_ai_outbox_probe_definer',
                          'campus_ai_takeover_definer',
                          'campus_ai_migration',
                          'campus_ai_runtime'
                      )
                      OR configured.setdatabase = (
                          SELECT oid FROM pg_database WHERE datname = current_database()
                      )
                  )
                ORDER BY role.rolname NULLS FIRST, database.datname NULLS FIRST
                """
            ).fetchall()
        return [(role_name, database_name, value) for role_name, database_name, value in rows]

    def owned_application_relations(self, role_name: str) -> int:
        with self.admin_connection() as connection:
            row = connection.execute(
                """
                SELECT count(*)
                FROM pg_class AS relation
                JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
                JOIN pg_roles AS owner ON owner.oid = relation.relowner
                WHERE owner.rolname = %s
                  AND namespace.nspname <> 'information_schema'
                  AND namespace.nspname NOT LIKE 'pg_%%'
                """,
                (role_name,),
            ).fetchone()
            if row is None:
                raise AssertionError(f"无法读取角色所有的关系数量：{role_name}")
            return int(row[0])

    def rerun_bootstrap(self) -> subprocess.CompletedProcess[str]:
        self.verify_disposable_cluster()
        return _run_bootstrap(
            command=self.bootstrap_command,
            environment=self.bootstrap_environment,
            protected_secrets=self.protected_test_secrets,
        )

    def run_bootstrap_with_passwords(
        self,
        *,
        runtime_password: str,
        migration_password: str,
        writer_password: str | None = None,
        reader_password: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        self.verify_disposable_cluster()
        environment = self.bootstrap_environment.copy()
        environment["POSTGRES_RUNTIME_PASSWORD"] = runtime_password
        environment["POSTGRES_MIGRATION_PASSWORD"] = migration_password
        effective_writer_password = writer_password or str(
            self.writer_connection_parameters["password"]
        )
        effective_reader_password = reader_password or str(
            self.reader_connection_parameters["password"]
        )
        environment["POSTGRES_SCHOOL_FACT_WRITER_PASSWORD"] = effective_writer_password
        environment["POSTGRES_SCHOOL_FACT_READER_PASSWORD"] = effective_reader_password
        return _run_bootstrap(
            command=self.bootstrap_command,
            environment=environment,
            protected_secrets=(
                self.protected_test_secrets[0],
                runtime_password,
                migration_password,
                effective_writer_password,
                effective_reader_password,
            ),
        )

    def verify_disposable_cluster(self) -> None:
        server_version_num = _verify_disposable_postgres_16_cluster(
            admin_connection_parameters=self.admin_connection_parameters,
            expected_system_identifier=self.disposable_cluster_system_identifier,
        )
        if server_version_num != self.server_version_num:
            pytest.fail("PostgreSQL 测试集群版本在同一次测试会话内发生变化")


def _required_environment(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        pytest.fail(f"真实 PostgreSQL 角色门缺少测试环境变量：{name}")
    return value


def _resolve_git_bash() -> Path:
    configured = os.environ.get("MASTER_DATA_GIT_BASH")
    candidates = [
        Path(configured) if configured else None,
        Path(r"C:\Program Files\Git\bin\bash.exe"),
        Path(r"C:\Program Files\Git\usr\bin\bash.exe"),
    ]
    for candidate in candidates:
        if candidate is not None and candidate.is_file():
            return candidate
    pytest.fail("真实角色门需要可用的 Git Bash 来执行仓库 bootstrap 脚本")


def _resolve_bootstrap_command() -> tuple[str, str]:
    """Keep Windows Git Bash validation while using the controlled POSIX shell in CI."""

    if sys.platform == "win32":
        return (_resolve_git_bash().as_posix(), BOOTSTRAP_SCRIPT.as_posix())

    shell = shutil.which("sh")
    if shell is None:
        pytest.fail("真实角色门需要可用的 POSIX sh 来执行仓库 bootstrap 脚本")
    return (shell, str(BOOTSTRAP_SCRIPT))


def _redact(output: str, protected_secrets: tuple[str, ...]) -> str:
    redacted = output
    for secret in protected_secrets:
        redacted = redacted.replace(secret, "[REDACTED]")
    return redacted


def _run_bootstrap(
    *,
    command: tuple[str, ...],
    environment: dict[str, str],
    protected_secrets: tuple[str, ...],
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        command,
        cwd=PROJECT_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=False,
    )
    raw_output = result.stdout + result.stderr
    if any(secret in raw_output for secret in protected_secrets):
        pytest.fail("数据库角色 bootstrap 脚本把测试密码写入了输出")
    if result.returncode != 0:
        safe_output = _redact(raw_output, protected_secrets)[-2_000:]
        pytest.fail(
            f"数据库角色 bootstrap 脚本失败，退出码 {result.returncode}；"
            f"已脱敏输出：\n{safe_output}"
        )
    return result


def _verify_disposable_postgres_16_cluster(
    *,
    admin_connection_parameters: dict[str, Any],
    expected_system_identifier: str,
) -> int:
    """在任何集群级角色变更前，证明目标是专用、可丢弃的 PostgreSQL 16 集群。"""

    if (
        not expected_system_identifier.isascii()
        or not expected_system_identifier.isdecimal()
        or len(expected_system_identifier) < 10
    ):
        pytest.fail(
            f"{DISPOSABLE_CLUSTER_SYSTEM_IDENTIFIER_ENV} 必须是完整的十进制 system_identifier"
        )

    with psycopg.connect(**admin_connection_parameters, autocommit=True) as connection:
        connection.execute("SET ROLE NONE")
        connection.execute("SET search_path = pg_catalog")
        identity_row = connection.execute(
            """
            SELECT control.system_identifier::text,
                   pg_catalog.current_setting('server_version_num')::integer,
                   (
                       SELECT rolsuper
                       FROM pg_catalog.pg_roles
                       WHERE rolname = session_user
                   ),
                   current_user = session_user
            FROM pg_catalog.pg_control_system() AS control
            """
        ).fetchone()
        unexpected_databases = connection.execute(
            """
            SELECT datname
            FROM pg_catalog.pg_database
            WHERE NOT datistemplate
              AND datname <> 'postgres'
              AND datname !~ '_test$'
            ORDER BY datname
            """
        ).fetchall()

    if identity_row is None:
        pytest.fail("无法读取 PostgreSQL 测试集群身份")
    (
        actual_system_identifier,
        server_version_num,
        bootstrap_is_superuser,
        role_is_reset,
    ) = identity_row
    if actual_system_identifier != expected_system_identifier:
        pytest.fail("拒绝执行集群级角色测试：PostgreSQL system_identifier 不匹配。")
    if not 160_000 <= server_version_num < 170_000:
        pytest.fail(
            "共享主数据角色测试只允许 PostgreSQL 16；"
            f"实际 server_version_num={server_version_num}。"
        )
    if bootstrap_is_superuser is not True:
        pytest.fail("角色 bootstrap 测试连接必须是可丢弃集群的超级用户")
    if role_is_reset is not True:
        pytest.fail("测试连接无法恢复到 bootstrap session_user")
    if unexpected_databases:
        names = "、".join(row[0] for row in unexpected_databases)
        pytest.fail(f"拒绝修改含非测试数据库的 PostgreSQL 集群：{names}")
    return server_version_num


def postgres_roles() -> PostgresRoles:
    if os.environ.get(INTEGRATION_FLAG) != "1":
        pytest.skip("需要显式启用共享主数据 PostgreSQL 角色集成测试")
    if not BOOTSTRAP_SCRIPT.is_file():
        pytest.fail(f"数据库角色 bootstrap 脚本尚不存在：{BOOTSTRAP_SCRIPT}")

    settings = get_settings()
    bootstrap_user = _required_environment("POSTGRES_BOOTSTRAP_USER")
    bootstrap_password = _required_environment("POSTGRES_BOOTSTRAP_PASSWORD")
    disposable_cluster_system_identifier = _required_environment(
        DISPOSABLE_CLUSTER_SYSTEM_IDENTIFIER_ENV
    )
    postgres_bin = Path(_required_environment("MASTER_DATA_POSTGRES_BIN"))
    psql_name = "psql.exe" if sys.platform == "win32" else "psql"
    if not (postgres_bin / psql_name).is_file():
        pytest.fail("MASTER_DATA_POSTGRES_BIN 没有指向 PostgreSQL 16 bin 目录")

    common_connection_parameters = {
        "host": settings.postgres_host,
        "port": settings.postgres_port,
        "dbname": settings.postgres_db,
        "connect_timeout": 5,
        "options": "",
    }
    admin_connection_parameters = {
        **common_connection_parameters,
        "user": bootstrap_user,
        "password": bootstrap_password,
    }
    server_version_num = _verify_disposable_postgres_16_cluster(
        admin_connection_parameters=admin_connection_parameters,
        expected_system_identifier=disposable_cluster_system_identifier,
    )

    role_passwords = {
        role_name: _derive_cluster_test_password(
            bootstrap_password=bootstrap_password,
            system_identifier=disposable_cluster_system_identifier,
            role_name=role_name,
        )
        for role_name in (
            "campus_ai_runtime",
            "campus_ai_migration",
            "campus_ai_school_fact_writer",
            "campus_ai_school_fact_reader",
        )
    }
    runtime_password = role_passwords["campus_ai_runtime"]
    migration_password = role_passwords["campus_ai_migration"]
    writer_password = role_passwords["campus_ai_school_fact_writer"]
    reader_password = role_passwords["campus_ai_school_fact_reader"]
    protected_secrets = (
        bootstrap_password,
        runtime_password,
        migration_password,
        writer_password,
        reader_password,
    )
    environment = os.environ.copy()
    environment.pop("PGOPTIONS", None)
    environment.update(
        {
            "PGHOST": settings.postgres_host,
            "PGPORT": str(settings.postgres_port),
            "PGDATABASE": settings.postgres_db,
            "PGUSER": bootstrap_user,
            "PGPASSWORD": bootstrap_password,
            "POSTGRES_DB": settings.postgres_db,
            "POSTGRES_EXPECTED_SYSTEM_IDENTIFIER": (disposable_cluster_system_identifier),
            "POSTGRES_RUNTIME_PASSWORD": runtime_password,
            "POSTGRES_MIGRATION_PASSWORD": migration_password,
            "POSTGRES_SCHOOL_FACT_WRITER_PASSWORD": writer_password,
            "POSTGRES_SCHOOL_FACT_READER_PASSWORD": reader_password,
            "PATH": f"{postgres_bin}{os.pathsep}{environment.get('PATH', '')}",
        }
    )
    command = _resolve_bootstrap_command()
    result = _run_bootstrap(
        command=command,
        environment=environment,
        protected_secrets=protected_secrets,
    )

    return PostgresRoles(
        admin_connection_parameters=admin_connection_parameters,
        runtime_connection_parameters={
            **common_connection_parameters,
            "user": "campus_ai_runtime",
            "password": runtime_password,
        },
        migration_connection_parameters={
            **common_connection_parameters,
            "user": "campus_ai_migration",
            "password": migration_password,
        },
        writer_connection_parameters={
            **common_connection_parameters,
            "user": "campus_ai_school_fact_writer",
            "password": writer_password,
        },
        reader_connection_parameters={
            **common_connection_parameters,
            "user": "campus_ai_school_fact_reader",
            "password": reader_password,
        },
        bootstrap_command=command,
        bootstrap_environment=environment,
        protected_test_secrets=protected_secrets,
        bootstrap_output=result.stdout + result.stderr,
        disposable_cluster_system_identifier=disposable_cluster_system_identifier,
        server_version_num=server_version_num,
    )


def _migration_environment(postgres_roles: PostgresRoles) -> dict[str, str]:
    """为当前主数据迁移构造只含测试凭据的隔离环境。"""

    migration = postgres_roles.migration_connection_parameters
    environment = os.environ.copy()
    environment.update(
        {
            "POSTGRES_MIGRATION_HOST": str(migration["host"]),
            "POSTGRES_MIGRATION_PORT": str(migration["port"]),
            "POSTGRES_MIGRATION_DB": str(migration["dbname"]),
            "POSTGRES_MIGRATION_USER": str(migration["user"]),
            "POSTGRES_MIGRATION_PASSWORD": str(migration["password"]),
            "POSTGRES_MIGRATION_CONTEXT_SIGNING_KEY": _TEST_CONTEXT_SIGNING_KEY,
        }
    )
    return environment


def _clear_application_database_caches() -> None:
    if get_engine.cache_info().currsize:
        get_engine().dispose()
    get_session_factory.cache_clear()
    get_engine.cache_clear()
    get_migration_settings.cache_clear()
    get_settings.cache_clear()


def _bind_isolated_application_environment(
    monkeypatch: pytest.MonkeyPatch,
    isolated_roles: PostgresRoles,
    *,
    schema_contract: DatabaseSchemaContract = "identity-v20",
) -> None:
    runtime = isolated_roles.runtime_connection_parameters
    migration = isolated_roles.migration_connection_parameters
    environment = {
        "POSTGRES_SCHEMA_CONTRACT": schema_contract,
        "POSTGRES_HOST": str(runtime["host"]),
        "POSTGRES_PORT": str(runtime["port"]),
        "POSTGRES_DB": str(runtime["dbname"]),
        "POSTGRES_DATABASE": str(runtime["dbname"]),
        "POSTGRES_USER": str(runtime["user"]),
        "POSTGRES_PASSWORD": str(runtime["password"]),
        "POSTGRES_RUNTIME_PASSWORD": str(runtime["password"]),
        "POSTGRES_MIGRATION_HOST": str(migration["host"]),
        "POSTGRES_MIGRATION_PORT": str(migration["port"]),
        "POSTGRES_MIGRATION_DB": str(migration["dbname"]),
        "POSTGRES_MIGRATION_USER": str(migration["user"]),
        "POSTGRES_MIGRATION_PASSWORD": str(migration["password"]),
        "POSTGRES_MIGRATION_CONTEXT_SIGNING_KEY": _TEST_CONTEXT_SIGNING_KEY,
        "RLS_CONTEXT_SIGNING_KEY": _TEST_CONTEXT_SIGNING_KEY,
        "PGHOST": str(migration["host"]),
        "PGPORT": str(migration["port"]),
        "PGDATABASE": str(migration["dbname"]),
    }
    _clear_application_database_caches()
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    _clear_application_database_caches()


def _roles_for_database(source: PostgresRoles, database_name: str) -> PostgresRoles:
    bootstrap_environment = source.bootstrap_environment.copy()
    bootstrap_environment.update(
        {
            "PGDATABASE": database_name,
            "POSTGRES_DB": database_name,
        }
    )
    return PostgresRoles(
        admin_connection_parameters={
            **source.admin_connection_parameters,
            "dbname": database_name,
        },
        runtime_connection_parameters={
            **source.runtime_connection_parameters,
            "dbname": database_name,
        },
        migration_connection_parameters={
            **source.migration_connection_parameters,
            "dbname": database_name,
        },
        writer_connection_parameters={
            **source.writer_connection_parameters,
            "dbname": database_name,
        },
        reader_connection_parameters={
            **source.reader_connection_parameters,
            "dbname": database_name,
        },
        bootstrap_command=source.bootstrap_command,
        bootstrap_environment=bootstrap_environment,
        protected_test_secrets=source.protected_test_secrets,
        bootstrap_output=source.bootstrap_output,
        disposable_cluster_system_identifier=source.disposable_cluster_system_identifier,
        server_version_num=source.server_version_num,
    )


def isolated_migration_schema(
    postgres_roles: PostgresRoles,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[PostgresRoles]:
    """Run destructive migration round trips in one exact disposable database.

    The full PostgreSQL suite deliberately shares its ordinary test database.  A
    migration test that travels to a pre-SF1 revision must not delete those shared
    authoritative facts or weaken the SF1 downgrade guard, so every such test gets
    a separately bootstrapped database that is dropped after the test.
    """

    postgres_roles.verify_disposable_cluster()
    database_name = f"campus_ai_rt_{uuid4().hex}_test"
    assert database_name.endswith("_test")
    assert len(database_name.encode("ascii")) <= 63
    assert database_name != postgres_roles.admin_connection_parameters["dbname"]

    with postgres_roles.admin_connection() as connection:
        connection.execute(
            sql.SQL("CREATE DATABASE {} TEMPLATE template0 ENCODING 'UTF8'").format(
                sql.Identifier(database_name)
            )
        )

    bootstrap_environment = postgres_roles.bootstrap_environment.copy()
    bootstrap_environment.update(
        {
            "PGDATABASE": database_name,
            "POSTGRES_DB": database_name,
        }
    )
    isolated_roles = PostgresRoles(
        admin_connection_parameters={
            **postgres_roles.admin_connection_parameters,
            "dbname": database_name,
        },
        runtime_connection_parameters={
            **postgres_roles.runtime_connection_parameters,
            "dbname": database_name,
        },
        migration_connection_parameters={
            **postgres_roles.migration_connection_parameters,
            "dbname": database_name,
        },
        bootstrap_command=postgres_roles.bootstrap_command,
        writer_connection_parameters={
            **postgres_roles.writer_connection_parameters,
            "dbname": database_name,
        },
        reader_connection_parameters={
            **postgres_roles.reader_connection_parameters,
            "dbname": database_name,
        },
        bootstrap_environment=bootstrap_environment,
        protected_test_secrets=postgres_roles.protected_test_secrets,
        bootstrap_output="",
        disposable_cluster_system_identifier=(postgres_roles.disposable_cluster_system_identifier),
        server_version_num=postgres_roles.server_version_num,
    )

    try:
        bootstrap = _run_bootstrap(
            command=isolated_roles.bootstrap_command,
            environment=isolated_roles.bootstrap_environment,
            protected_secrets=isolated_roles.protected_test_secrets,
        )
        isolated_roles.bootstrap_output = bootstrap.stdout + bootstrap.stderr
        upgrade = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "20260824_20"],
            cwd=PROJECT_ROOT / "apps" / "api",
            env=_migration_environment(isolated_roles),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert upgrade.returncode == 0, upgrade.stdout + upgrade.stderr
        reconcile = _run_bootstrap(
            command=isolated_roles.bootstrap_command,
            environment=isolated_roles.bootstrap_environment,
            protected_secrets=isolated_roles.protected_test_secrets,
        )
        isolated_roles.bootstrap_output += reconcile.stdout + reconcile.stderr
        _bind_isolated_application_environment(monkeypatch, isolated_roles)
        yield isolated_roles
    finally:
        _clear_application_database_caches()
        with postgres_roles.admin_connection() as connection:
            connection.execute(
                "SELECT pg_catalog.pg_terminate_backend(pid) "
                "FROM pg_catalog.pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_catalog.pg_backend_pid()",
                (database_name,),
            )
            drop_database = sql.SQL("DROP DATABASE {} WITH (FORCE)").format(
                sql.Identifier(database_name)
            )
            connection.execute(drop_database)


def isolated_revision19_schema(
    postgres_roles: PostgresRoles,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[PostgresRoles]:
    """Create a disposable database at revision19 without traversing revision20."""

    postgres_roles.verify_disposable_cluster()
    database_name = f"campus_ai_r19_{uuid4().hex}_test"
    assert database_name.endswith("_test")
    assert len(database_name.encode("ascii")) <= 63
    assert database_name != postgres_roles.admin_connection_parameters["dbname"]

    with postgres_roles.admin_connection() as connection:
        connection.execute(
            sql.SQL("CREATE DATABASE {} TEMPLATE template0 ENCODING 'UTF8'").format(
                sql.Identifier(database_name)
            )
        )

    bootstrap_environment = postgres_roles.bootstrap_environment.copy()
    bootstrap_environment.update(
        {
            "PGDATABASE": database_name,
            "POSTGRES_DB": database_name,
        }
    )
    isolated_roles = PostgresRoles(
        admin_connection_parameters={
            **postgres_roles.admin_connection_parameters,
            "dbname": database_name,
        },
        runtime_connection_parameters={
            **postgres_roles.runtime_connection_parameters,
            "dbname": database_name,
        },
        migration_connection_parameters={
            **postgres_roles.migration_connection_parameters,
            "dbname": database_name,
        },
        bootstrap_command=postgres_roles.bootstrap_command,
        writer_connection_parameters={
            **postgres_roles.writer_connection_parameters,
            "dbname": database_name,
        },
        reader_connection_parameters={
            **postgres_roles.reader_connection_parameters,
            "dbname": database_name,
        },
        bootstrap_environment=bootstrap_environment,
        protected_test_secrets=postgres_roles.protected_test_secrets,
        bootstrap_output="",
        disposable_cluster_system_identifier=postgres_roles.disposable_cluster_system_identifier,
        server_version_num=postgres_roles.server_version_num,
    )

    try:
        bootstrap = _run_bootstrap(
            command=isolated_roles.bootstrap_command,
            environment=isolated_roles.bootstrap_environment,
            protected_secrets=isolated_roles.protected_test_secrets,
        )
        isolated_roles.bootstrap_output = bootstrap.stdout + bootstrap.stderr
        upgrade = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "20260823_19"],
            cwd=PROJECT_ROOT / "apps" / "api",
            env=_migration_environment(isolated_roles),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert upgrade.returncode == 0, upgrade.stdout + upgrade.stderr
        _bind_isolated_application_environment(monkeypatch, isolated_roles)
        yield isolated_roles
    finally:
        _clear_application_database_caches()
        with postgres_roles.admin_connection() as connection:
            connection.execute(
                "SELECT pg_catalog.pg_terminate_backend(pid) "
                "FROM pg_catalog.pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_catalog.pg_backend_pid()",
                (database_name,),
            )
            connection.execute(
                sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(database_name))
            )


def current_master_data_template(
    postgres_roles: PostgresRoles,
    *,
    schema_revision: Literal["20260824_20", "20260905_21", "20260905_22"] = "20260824_20",
) -> Iterator[PostgresRoles]:
    """Build an empty reconciled template, keeping the default exact revision20."""
    if schema_revision not in {"20260824_20", "20260905_21", "20260905_22"}:
        raise ValueError("Only explicitly supported schema revisions can be templated.")

    postgres_roles.verify_disposable_cluster()
    label = schema_revision.rsplit("_", 1)[1]
    database_name = f"campus_ai_head{label}_template_{uuid4().hex}_test"
    assert len(database_name.encode("ascii")) <= 63
    with postgres_roles.admin_connection() as connection:
        connection.execute(
            sql.SQL("CREATE DATABASE {} TEMPLATE template0 ENCODING 'UTF8'").format(
                sql.Identifier(database_name)
            )
        )
    template_roles = _roles_for_database(postgres_roles, database_name)

    try:
        bootstrap = _run_bootstrap(
            command=template_roles.bootstrap_command,
            environment=template_roles.bootstrap_environment,
            protected_secrets=template_roles.protected_test_secrets,
        )
        template_roles.bootstrap_output = bootstrap.stdout + bootstrap.stderr
        upgrade = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", schema_revision],
            cwd=PROJECT_ROOT / "apps" / "api",
            env=_migration_environment(template_roles),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert upgrade.returncode == 0, upgrade.stdout + upgrade.stderr
        reconcile = _run_bootstrap(
            command=template_roles.bootstrap_command,
            environment=template_roles.bootstrap_environment,
            protected_secrets=template_roles.protected_test_secrets,
        )
        template_roles.bootstrap_output += reconcile.stdout + reconcile.stderr
        yield template_roles
    finally:
        with postgres_roles.admin_connection() as connection:
            connection.execute(
                "SELECT pg_catalog.pg_terminate_backend(pid) "
                "FROM pg_catalog.pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_catalog.pg_backend_pid()",
                (database_name,),
            )
            connection.execute(
                sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(database_name))
            )


def current_master_data_schema(
    current_master_data_template: PostgresRoles,
    postgres_roles: PostgresRoles,
    monkeypatch: pytest.MonkeyPatch,
    *,
    schema_contract: DatabaseSchemaContract = "identity-v20",
) -> Iterator[PostgresRoles]:
    """Clone the chosen template for one test; never share seeded school roots."""
    if schema_contract not in {"identity-v20", "school-facts-f1-v21", "school-facts-f1-v22"}:
        raise ValueError("Unsupported test schema contract.")

    postgres_roles.verify_disposable_cluster()
    label = schema_contract.rsplit("-v", 1)[1]
    database_name = f"campus_ai_head{label}_{uuid4().hex}_test"
    assert len(database_name.encode("ascii")) <= 63
    assert database_name != current_master_data_template.admin_connection_parameters["dbname"]
    template_name = str(current_master_data_template.admin_connection_parameters["dbname"])
    with postgres_roles.admin_connection() as connection:
        connection.execute(
            sql.SQL("CREATE DATABASE {} WITH OWNER {} TEMPLATE {}").format(
                sql.Identifier(database_name),
                sql.Identifier("campus_ai_db_owner"),
                sql.Identifier(template_name),
            )
        )
    cloned_roles = _roles_for_database(current_master_data_template, database_name)

    try:
        _bind_isolated_application_environment(
            monkeypatch, cloned_roles, schema_contract=schema_contract
        )
        yield cloned_roles
    finally:
        _clear_application_database_caches()
        with postgres_roles.admin_connection() as connection:
            connection.execute(
                "SELECT pg_catalog.pg_terminate_backend(pid) "
                "FROM pg_catalog.pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_catalog.pg_backend_pid()",
                (database_name,),
            )
            connection.execute(
                sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(database_name))
            )


def school_facts_f1_template(postgres_roles: PostgresRoles) -> Iterator[PostgresRoles]:
    """Use the same guarded disposable cluster; only the current F1 template upgrades to22."""
    yield from current_master_data_template(postgres_roles, schema_revision="20260905_22")


def school_facts_f1_schema(
    school_facts_f1_template: PostgresRoles,
    postgres_roles: PostgresRoles,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[PostgresRoles]:
    yield from current_master_data_schema(
        school_facts_f1_template,
        postgres_roles,
        monkeypatch,
        schema_contract="school-facts-f1-v22",
    )


def current_owner_connection(
    current_master_data_schema: PostgresRoles,
) -> Iterator[psycopg.Connection[Any]]:
    """用 migration 登录后切换 Owner，并在测试后回滚全部数据。"""

    connection = psycopg.connect(
        **current_master_data_schema.migration_connection_parameters,
        autocommit=False,
    )
    connection.execute("SET ROLE campus_ai_db_owner")
    try:
        yield connection
    finally:
        connection.rollback()
        connection.close()


def current_owner_session(current_master_data_schema: PostgresRoles) -> Iterator[Session]:
    """提供绑定 Owner 连接且不替 Repository 管理事务的真实 Session。"""

    migration = current_master_data_schema.migration_connection_parameters
    engine = create_engine(
        URL.create(
            drivername="postgresql+psycopg",
            username=str(migration["user"]),
            password=str(migration["password"]),
            host=str(migration["host"]),
            port=int(migration["port"]),
            database=str(migration["dbname"]),
        ),
        poolclass=NullPool,
    )
    connection = engine.connect()
    connection.exec_driver_sql("SET ROLE campus_ai_db_owner")
    session = Session(bind=connection, autoflush=False)
    try:
        yield session
    finally:
        session.rollback()
        session.close()
        connection.close()
        engine.dispose()
