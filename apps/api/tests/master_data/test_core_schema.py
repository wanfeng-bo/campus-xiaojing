from __future__ import annotations

import ast
import importlib
import os
import subprocess
import sys
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from importlib.util import resolve_name
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from psycopg.errors import CheckViolation, ExclusionViolation
from sqlalchemy import create_engine
from sqlalchemy.engine import URL
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from app.platform.contracts import AssignmentId, PersonId, SchoolId
from app.platform.directory.models import SchoolPerson, SchoolPersonName
from app.platform.identity.models import User
from app.platform.master_data_imports.models import SchoolFactSource
from app.platform.organization.models import (
    OrganizationUnit,
    OrganizationUnitVersion,
    StaffAssignment,
    StaffAssignmentVersion,
)
from app.platform.tenancy.models import School, SchoolVersion
from tests.master_data.conftest import PostgresRoles
from tests.master_data.test_execution_context import (
    _seed_formal_structure_lineage,  # pyright: ignore[reportPrivateUsage]
)

API_ROOT = Path(__file__).resolve().parents[2]
TASK3_TABLES = (
    "account_person_bindings",
    "organization_unit_versions",
    "organization_units",
    "school_people",
    "school_person_names",
    "schools",
    "staff_assignments",
)
T0 = datetime(2026, 1, 1, tzinfo=UTC)
T1 = T0 + timedelta(days=30)
T2 = T1 + timedelta(days=30)
T3 = T2 + timedelta(days=30)


def _current_alembic_head() -> str:
    """从迁移图读取当前唯一 head，避免旧任务测试把后继迁移误判为失败。"""

    config = Config(API_ROOT / "alembic.ini")
    head = ScriptDirectory.from_config(config).get_current_head()
    assert head is not None
    return head


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


def _imported_private_model_modules(tree: ast.AST, *, package: str) -> set[str]:
    """解析普通、相对和常量动态导入，返回命中的 owner 私有 models 模块。"""

    imported_modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.update(alias.name for alias in node.names)
            continue

        if isinstance(node, ast.ImportFrom):
            module_name = node.module or ""
            if node.level:
                module_name = resolve_name(f"{'.' * node.level}{module_name}", package)
            imported_modules.add(module_name)
            imported_modules.update(
                f"{module_name}.{alias.name}" for alias in node.names if alias.name == "models"
            )
            continue

        if not isinstance(node, ast.Call) or not node.args:
            continue
        function_name: str | None = None
        if isinstance(node.func, ast.Name):
            function_name = node.func.id
        elif isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name):
            function_name = f"{node.func.value.id}.{node.func.attr}"
        if function_name not in {"__import__", "import_module", "importlib.import_module"}:
            continue
        module_argument = node.args[0]
        if not isinstance(module_argument, ast.Constant) or not isinstance(
            module_argument.value, str
        ):
            continue
        module_name = module_argument.value
        if module_name.startswith("."):
            module_name = resolve_name(module_name, package)
        imported_modules.add(module_name)

    return {module_name for module_name in imported_modules if _is_owner_models_module(module_name)}


def _is_owner_models_module(module_name: str) -> bool:
    """识别平台或业务 owner 根包下的私有 ORM models 模块。"""

    parts = module_name.split(".")
    return (
        len(parts) == 4
        and parts[0] == "app"
        and parts[1] in {"platform", "domains"}
        and bool(parts[2])
        and parts[3] == "models"
    )


@pytest.fixture(scope="session")
def task3_schema(postgres_roles: PostgresRoles) -> PostgresRoles:
    """把专用测试库迁移到当前代码头，RED 时仍停在 Task 2 的旧迁移头。"""

    postgres_roles.verify_disposable_cluster()
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=API_ROOT,
        env=_migration_environment(postgres_roles),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return postgres_roles


@pytest.fixture
def owner_connection(
    task3_schema: PostgresRoles,
) -> Iterator[psycopg.Connection[Any]]:
    """使用 migration 登录后显式切到 NOLOGIN Owner，并在用例后整体回滚。"""

    connection = psycopg.connect(
        **task3_schema.migration_connection_parameters,
        autocommit=False,
    )
    connection.execute("SET ROLE campus_ai_db_owner")
    try:
        yield connection
    finally:
        connection.rollback()
        connection.close()


@pytest.fixture
def owner_session(task3_schema: PostgresRoles) -> Iterator[Session]:
    """把真实 SQLAlchemy Session 绑定到已显式切换 Owner 的同一连接。"""

    migration = task3_schema.migration_connection_parameters
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


def _insert_school(
    connection: psycopg.Connection[Any],
    school_id: UUID,
    *,
    code: str,
) -> None:
    connection.execute(
        """
        INSERT INTO schools (id, school_code, display_name, timezone_name, status, version)
        VALUES (%s, %s, %s, 'Asia/Shanghai', 'active', 1)
        """,
        (school_id, code, f"学校-{code}"),
    )


def _foreign_key_contract(
    connection: psycopg.Connection[Any],
    *,
    table_name: str,
    constraint_name: str,
) -> tuple[tuple[str, ...], str, tuple[str, ...], str]:
    row = connection.execute(
        """
        SELECT
            array_agg(source_attribute.attname ORDER BY key.ordinality),
            referenced_relation.relname,
            array_agg(referenced_attribute.attname ORDER BY key.ordinality),
            constraint_record.confdeltype
        FROM pg_constraint AS constraint_record
        JOIN pg_class AS relation ON relation.oid = constraint_record.conrelid
        JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
        JOIN pg_class AS referenced_relation
          ON referenced_relation.oid = constraint_record.confrelid
        CROSS JOIN LATERAL unnest(constraint_record.conkey, constraint_record.confkey)
          WITH ORDINALITY AS key(source_attnum, referenced_attnum, ordinality)
        JOIN pg_attribute AS source_attribute
          ON source_attribute.attrelid = relation.oid
         AND source_attribute.attnum = key.source_attnum
        JOIN pg_attribute AS referenced_attribute
          ON referenced_attribute.attrelid = referenced_relation.oid
         AND referenced_attribute.attnum = key.referenced_attnum
        WHERE namespace.nspname = 'public'
          AND relation.relname = %s
          AND constraint_record.conname = %s
          AND constraint_record.contype = 'f'
        GROUP BY referenced_relation.relname, constraint_record.confdeltype
        """,
        (table_name, constraint_name),
    ).fetchone()
    assert row is not None
    return (
        tuple(str(value) for value in row[0]),
        str(row[1]),
        tuple(str(value) for value in row[2]),
        str(row[3]),
    )


def _insert_user(connection: psycopg.Connection[Any]) -> int:
    row = connection.execute(
        """
        INSERT INTO users (external_subject, display_name)
        VALUES (%s, '测试账号')
        RETURNING id
        """,
        (f"task3-user-{uuid4()}",),
    ).fetchone()
    assert row is not None
    return int(row[0])


def _insert_person(
    connection: psycopg.Connection[Any],
    school_id: UUID,
    person_id: UUID,
) -> None:
    connection.execute(
        """
        INSERT INTO school_people (id, school_id, status, version)
        VALUES (%s, %s, 'active', 1)
        """,
        (person_id, school_id),
    )


def _insert_person_name(
    connection: psycopg.Connection[Any],
    *,
    school_id: UUID,
    person_id: UUID,
    display_name: str,
    valid_from: datetime,
    valid_to: datetime | None,
    status: str = "confirmed",
) -> None:
    connection.execute(
        """
        INSERT INTO school_person_names (
            id, school_id, person_id, display_name, valid_from, valid_to,
            source_kind, source_ref, status, version
        )
        VALUES (%s, %s, %s, %s, %s, %s, 'manual', %s, %s, 1)
        """,
        (
            uuid4(),
            school_id,
            person_id,
            display_name,
            valid_from,
            valid_to,
            str(uuid4()),
            status,
        ),
    )


def _insert_organization_unit(
    connection: psycopg.Connection[Any],
    *,
    school_id: UUID,
    unit_id: UUID,
    parent_id: UUID | None = None,
    kind: str = "department",
) -> None:
    connection.execute(
        """
        INSERT INTO organization_units (
            id, school_id, parent_organization_unit_id, kind, status, version
        )
        VALUES (%s, %s, %s, %s, 'active', 1)
        """,
        (unit_id, school_id, parent_id, kind),
    )


def _insert_organization_version(
    connection: psycopg.Connection[Any],
    *,
    school_id: UUID,
    unit_id: UUID,
    display_name: str,
    valid_from: datetime,
    valid_to: datetime | None,
    status: str = "confirmed",
) -> None:
    stable_row = connection.execute(
        """
        SELECT parent_organization_unit_id, source_key, version
        FROM organization_units
        WHERE school_id = %s AND id = %s
        FOR UPDATE
        """,
        (school_id, unit_id),
    ).fetchone()
    assert stable_row is not None
    latest_version_row = connection.execute(
        """
        SELECT max(version)
        FROM organization_unit_versions
        WHERE school_id = %s AND organization_unit_id = %s
        """,
        (school_id, unit_id),
    ).fetchone()
    assert latest_version_row is not None
    latest_version = latest_version_row[0]
    target_version = int(stable_row[2]) if latest_version is None else int(latest_version) + 1
    if int(stable_row[2]) != target_version:
        connection.execute(
            """
            UPDATE organization_units
            SET version = %s
            WHERE school_id = %s AND id = %s
            """,
            (target_version, school_id, unit_id),
        )

    actor_user_id = _insert_user(connection)
    actor_person_id = uuid4()
    actor_unit_id = uuid4()
    _insert_person(connection, school_id, actor_person_id)
    _insert_organization_unit(
        connection,
        school_id=school_id,
        unit_id=actor_unit_id,
        kind="administration",
    )
    actor_assignment_id = _insert_assignment(
        connection,
        school_id=school_id,
        person_id=actor_person_id,
        unit_id=actor_unit_id,
        semantic_role_code="school_master_data_manager",
        valid_from=T0 - timedelta(days=1),
        valid_to=None,
    )
    _insert_account_binding(
        connection,
        school_id=school_id,
        account_user_id=actor_user_id,
        person_id=actor_person_id,
        valid_from=T0 - timedelta(days=1),
        valid_to=None,
    )
    batch_id = _seed_formal_structure_lineage(
        connection,
        school_id=school_id,
        user_id=actor_user_id,
        person_id=actor_person_id,
        assignment_id=actor_assignment_id,
        fact_kind="organization",
        fact_source_key=str(stable_row[1]),
        target_id=unit_id,
        effective_at=valid_from,
        target_version=target_version,
        expected_version=target_version - 1,
    )
    connection.execute(
        """
        INSERT INTO organization_unit_versions (
            id, school_id, organization_unit_id, display_name, short_name,
            display_path, parent_organization_unit_id,
            valid_from, valid_to, source_kind, source_ref, status, version
        )
        VALUES (
            %s, %s, %s, %s, %s, %s, %s, %s, %s,
            'manual', %s, %s, %s
        )
        """,
        (
            uuid4(),
            school_id,
            unit_id,
            display_name,
            display_name,
            f"/{display_name}",
            stable_row[0],
            valid_from,
            valid_to,
            f"change-set:{batch_id}",
            status,
            target_version,
        ),
    )


def _insert_assignment(
    connection: psycopg.Connection[Any],
    *,
    school_id: UUID,
    person_id: UUID,
    unit_id: UUID,
    semantic_role_code: str,
    valid_from: datetime,
    valid_to: datetime | None,
    status: str = "confirmed",
) -> UUID:
    assignment_id = uuid4()
    source_id = uuid4()
    source_key = f"task3-assignment:{assignment_id}"
    approval_reference = str(uuid4())
    connection.execute(
        """
        INSERT INTO school_fact_sources (
            id, school_id, source_key, source_kind, source_ref, schema_version
        ) VALUES (%s, %s, %s, 'legacy', %s, 1)
        """,
        (source_id, school_id, source_key, source_key),
    )
    connection.execute(
        """
        INSERT INTO staff_assignments (
            id, school_id, person_id, organization_unit_id, position_title,
            semantic_role_code, assignment_kind, appointment_source, approval_reference,
            source_key,
            valid_from, valid_to, status, version
        )
        VALUES (
            %s, %s, %s, %s, '测试岗位', %s, 'substantive', 'manual', %s,
            %s, %s, %s, %s, 1
        )
        """,
        (
            assignment_id,
            school_id,
            person_id,
            unit_id,
            semantic_role_code,
            approval_reference,
            source_key,
            valid_from,
            valid_to,
            status,
        ),
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
            '测试岗位', %s, 'substantive',
            'manual', %s,
            %s, NULL, %s, NULL,
            %s, 1, %s, %s, NULL
        )
        """,
        (
            uuid4(),
            school_id,
            assignment_id,
            person_id,
            unit_id,
            semantic_role_code,
            approval_reference,
            valid_from,
            valid_to,
            status,
            source_key,
            source_id,
        ),
    )
    return assignment_id


def _insert_account_binding(
    connection: psycopg.Connection[Any],
    *,
    school_id: UUID,
    account_user_id: int,
    person_id: UUID,
    valid_from: datetime,
    valid_to: datetime | None,
    status: str = "confirmed",
) -> None:
    binding_id = uuid4()
    source_id = uuid4()
    source_key = f"task3-login-mapping:{binding_id}"
    identity_row = connection.execute(
        """
        SELECT provider_code, provider_tenant_key, external_subject
        FROM users
        WHERE id = %s
        """,
        (account_user_id,),
    ).fetchone()
    assert identity_row is not None
    connection.execute(
        """
        INSERT INTO school_fact_sources (
            id, school_id, source_key, source_kind, source_ref, schema_version
        ) VALUES (%s, %s, %s, 'legacy', %s, 1)
        """,
        (source_id, school_id, source_key, source_key),
    )
    connection.execute(
        """
        INSERT INTO account_person_bindings (
            id, school_id, account_user_id, person_id, source_key,
            valid_from, valid_to, status, version
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 1)
        """,
        (
            binding_id,
            school_id,
            account_user_id,
            person_id,
            source_key,
            valid_from,
            valid_to,
            status,
        ),
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
            %s, %s, %s,
            %s, %s, NULL, %s, 1,
            %s, %s, NULL
        )
        """,
        (
            uuid4(),
            school_id,
            binding_id,
            account_user_id,
            person_id,
            str(identity_row[0]),
            str(identity_row[1]),
            str(identity_row[2]),
            valid_from,
            valid_to,
            status,
            source_key,
            source_id,
        ),
    )


@pytest.mark.integration
def test_task3_revision_tables_and_owner_are_exact(
    owner_connection: psycopg.Connection[Any],
) -> None:
    revision_row = owner_connection.execute(
        "SELECT version_num FROM public.alembic_version"
    ).fetchone()
    assert revision_row == (_current_alembic_head(),)

    rows = owner_connection.execute(
        """
        SELECT relation.relname, owner.rolname
        FROM pg_class AS relation
        JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
        JOIN pg_roles AS owner ON owner.oid = relation.relowner
        WHERE namespace.nspname = 'public'
          AND relation.relname = ANY(%s)
        ORDER BY relation.relname
        """,
        (
            [
                "schools",
                "school_people",
                "school_person_names",
                "account_person_bindings",
                "organization_units",
                "organization_unit_versions",
                "staff_assignments",
            ],
        ),
    ).fetchall()
    assert rows == [
        ("account_person_bindings", "campus_ai_db_owner"),
        ("organization_unit_versions", "campus_ai_db_owner"),
        ("organization_units", "campus_ai_db_owner"),
        ("school_people", "campus_ai_db_owner"),
        ("school_person_names", "campus_ai_db_owner"),
        ("schools", "campus_ai_db_owner"),
        ("staff_assignments", "campus_ai_db_owner"),
    ]


def test_existing_user_model_remains_login_account_only() -> None:
    assert "school_id" not in User.__table__.columns
    assert "person_id" not in User.__table__.columns
    assert User.__table__.columns["security_version"].nullable is False
    assert User.__table__.columns["security_version"].server_default is not None


@pytest.mark.integration
def test_staff_assignment_declares_school_scoped_person_and_unit_foreign_keys(
    owner_connection: psycopg.Connection[Any],
) -> None:
    assert _foreign_key_contract(
        owner_connection,
        table_name="staff_assignments",
        constraint_name="fk_staff_assignments_school_person",
    ) == (("school_id", "person_id"), "school_people", ("school_id", "id"), "r")
    assert _foreign_key_contract(
        owner_connection,
        table_name="staff_assignments",
        constraint_name="fk_staff_assignments_school_unit",
    ) == (
        ("school_id", "organization_unit_id"),
        "organization_units",
        ("school_id", "id"),
        "r",
    )


@pytest.mark.integration
def test_organization_unit_declares_school_scoped_parent_foreign_key(
    owner_connection: psycopg.Connection[Any],
) -> None:
    assert _foreign_key_contract(
        owner_connection,
        table_name="organization_units",
        constraint_name="fk_organization_units_school_parent",
    ) == (
        ("school_id", "parent_organization_unit_id"),
        "organization_units",
        ("school_id", "id"),
        "r",
    )


@pytest.mark.integration
def test_assignment_slot_rejects_overlap_but_allows_adjacent_periods_and_other_roles(
    owner_connection: psycopg.Connection[Any],
) -> None:
    school_id = uuid4()
    person_id = uuid4()
    unit_id = uuid4()
    _insert_school(owner_connection, school_id, code="A")
    _insert_person(owner_connection, school_id, person_id)
    _insert_organization_unit(owner_connection, school_id=school_id, unit_id=unit_id)
    _insert_assignment(
        owner_connection,
        school_id=school_id,
        person_id=person_id,
        unit_id=unit_id,
        semantic_role_code="repair.worker",
        valid_from=T0,
        valid_to=T1,
    )

    with pytest.raises(ExclusionViolation), owner_connection.transaction():
        _insert_assignment(
            owner_connection,
            school_id=school_id,
            person_id=person_id,
            unit_id=unit_id,
            semantic_role_code="repair.worker",
            valid_from=T0 + timedelta(days=1),
            valid_to=T2,
        )

    _insert_assignment(
        owner_connection,
        school_id=school_id,
        person_id=person_id,
        unit_id=unit_id,
        semantic_role_code="repair.worker",
        valid_from=T1,
        valid_to=T2,
    )
    _insert_assignment(
        owner_connection,
        school_id=school_id,
        person_id=person_id,
        unit_id=unit_id,
        semantic_role_code="repair.worker",
        valid_from=T0,
        valid_to=T2,
        status="draft",
    )
    _insert_assignment(
        owner_connection,
        school_id=school_id,
        person_id=person_id,
        unit_id=unit_id,
        semantic_role_code="repair.worker",
        valid_from=T0,
        valid_to=T2,
        status="rejected",
    )
    _insert_assignment(
        owner_connection,
        school_id=school_id,
        person_id=person_id,
        unit_id=unit_id,
        semantic_role_code="repair.dispatcher",
        valid_from=T0,
        valid_to=T2,
    )


@pytest.mark.integration
def test_database_rejects_impossible_effective_interval_and_security_version(
    owner_connection: psycopg.Connection[Any],
) -> None:
    school_id = uuid4()
    person_id = uuid4()
    unit_id = uuid4()
    user_id = _insert_user(owner_connection)
    _insert_school(owner_connection, school_id, code="A")
    _insert_person(owner_connection, school_id, person_id)
    _insert_organization_unit(owner_connection, school_id=school_id, unit_id=unit_id)

    with pytest.raises(CheckViolation), owner_connection.transaction():
        owner_connection.execute(
            "UPDATE users SET security_version = 0 WHERE id = %s",
            (user_id,),
        )

    with pytest.raises(CheckViolation), owner_connection.transaction():
        _insert_assignment(
            owner_connection,
            school_id=school_id,
            person_id=person_id,
            unit_id=unit_id,
            semantic_role_code="repair.worker",
            valid_from=T1,
            valid_to=T1,
        )


@pytest.mark.integration
def test_account_person_binding_declares_school_scoped_person_foreign_key(
    owner_connection: psycopg.Connection[Any],
) -> None:
    assert _foreign_key_contract(
        owner_connection,
        table_name="account_person_bindings",
        constraint_name="fk_account_person_bindings_school_person",
    ) == (("school_id", "person_id"), "school_people", ("school_id", "id"), "r")


@pytest.mark.integration
def test_person_names_allow_homonyms_reject_overlap_and_preserve_history(
    owner_connection: psycopg.Connection[Any],
) -> None:
    school_id = uuid4()
    first_person = uuid4()
    second_person = uuid4()
    _insert_school(owner_connection, school_id, code="A")
    _insert_person(owner_connection, school_id, first_person)
    _insert_person(owner_connection, school_id, second_person)
    _insert_person_name(
        owner_connection,
        school_id=school_id,
        person_id=first_person,
        display_name="李明",
        valid_from=T0,
        valid_to=T1,
    )
    _insert_person_name(
        owner_connection,
        school_id=school_id,
        person_id=second_person,
        display_name="李明",
        valid_from=T0,
        valid_to=None,
    )

    with pytest.raises(ExclusionViolation), owner_connection.transaction():
        _insert_person_name(
            owner_connection,
            school_id=school_id,
            person_id=first_person,
            display_name="李小明",
            valid_from=T0 + timedelta(days=1),
            valid_to=T2,
        )

    _insert_person_name(
        owner_connection,
        school_id=school_id,
        person_id=first_person,
        display_name="李小明",
        valid_from=T1,
        valid_to=None,
    )
    row = owner_connection.execute(
        """
        SELECT display_name
        FROM school_person_names
        WHERE school_id = %s
          AND person_id = %s
          AND status = 'confirmed'
          AND valid_from <= %s
          AND (valid_to IS NULL OR %s < valid_to)
        """,
        (school_id, first_person, T0 + timedelta(days=1), T0 + timedelta(days=1)),
    ).fetchone()
    assert row == ("李明",)


@pytest.mark.integration
def test_organization_versions_reject_overlap_and_resolve_historical_name(
    owner_connection: psycopg.Connection[Any],
) -> None:
    school_id = uuid4()
    unit_id = uuid4()
    _insert_school(owner_connection, school_id, code="A")
    _insert_organization_unit(owner_connection, school_id=school_id, unit_id=unit_id)
    _insert_organization_version(
        owner_connection,
        school_id=school_id,
        unit_id=unit_id,
        display_name="总务处",
        valid_from=T0,
        valid_to=T1,
    )

    with pytest.raises(ExclusionViolation), owner_connection.transaction():
        _insert_organization_version(
            owner_connection,
            school_id=school_id,
            unit_id=unit_id,
            display_name="后勤处",
            valid_from=T0 + timedelta(days=1),
            valid_to=T2,
        )

    _insert_organization_version(
        owner_connection,
        school_id=school_id,
        unit_id=unit_id,
        display_name="后勤处",
        valid_from=T1,
        valid_to=None,
    )
    row = owner_connection.execute(
        """
        SELECT display_name
        FROM organization_unit_versions
        WHERE school_id = %s
          AND organization_unit_id = %s
          AND status = 'confirmed'
          AND valid_from <= %s
          AND (valid_to IS NULL OR %s < valid_to)
        """,
        (school_id, unit_id, T0 + timedelta(days=1), T0 + timedelta(days=1)),
    ).fetchone()
    assert row == ("总务处",)


@pytest.mark.integration
def test_account_binding_rejects_one_account_bound_to_two_people_at_same_time(
    owner_connection: psycopg.Connection[Any],
) -> None:
    school_id = uuid4()
    account_id = _insert_user(owner_connection)
    first_person = uuid4()
    second_person = uuid4()
    _insert_school(owner_connection, school_id, code="A")
    _insert_person(owner_connection, school_id, first_person)
    _insert_person(owner_connection, school_id, second_person)
    _insert_account_binding(
        owner_connection,
        school_id=school_id,
        account_user_id=account_id,
        person_id=first_person,
        valid_from=T0,
        valid_to=T2,
    )

    with pytest.raises(ExclusionViolation), owner_connection.transaction():
        _insert_account_binding(
            owner_connection,
            school_id=school_id,
            account_user_id=account_id,
            person_id=second_person,
            valid_from=T1,
            valid_to=T3,
        )


@pytest.mark.integration
def test_account_binding_rejects_one_person_bound_to_two_accounts_at_same_time(
    owner_connection: psycopg.Connection[Any],
) -> None:
    school_id = uuid4()
    first_account = _insert_user(owner_connection)
    second_account = _insert_user(owner_connection)
    person_id = uuid4()
    _insert_school(owner_connection, school_id, code="A")
    _insert_person(owner_connection, school_id, person_id)
    _insert_account_binding(
        owner_connection,
        school_id=school_id,
        account_user_id=first_account,
        person_id=person_id,
        valid_from=T0,
        valid_to=T2,
    )

    with pytest.raises(ExclusionViolation), owner_connection.transaction():
        _insert_account_binding(
            owner_connection,
            school_id=school_id,
            account_user_id=second_account,
            person_id=person_id,
            valid_from=T1,
            valid_to=T3,
        )


@pytest.mark.integration
def test_account_binding_allows_adjacent_periods_on_both_sides(
    owner_connection: psycopg.Connection[Any],
) -> None:
    school_id = uuid4()
    shared_account = _insert_user(owner_connection)
    next_account = _insert_user(owner_connection)
    first_person = uuid4()
    second_person = uuid4()
    _insert_school(owner_connection, school_id, code="A")
    _insert_person(owner_connection, school_id, first_person)
    _insert_person(owner_connection, school_id, second_person)
    _insert_account_binding(
        owner_connection,
        school_id=school_id,
        account_user_id=shared_account,
        person_id=first_person,
        valid_from=T0,
        valid_to=T1,
    )
    _insert_account_binding(
        owner_connection,
        school_id=school_id,
        account_user_id=shared_account,
        person_id=second_person,
        valid_from=T1,
        valid_to=T2,
    )
    _insert_account_binding(
        owner_connection,
        school_id=school_id,
        account_user_id=next_account,
        person_id=first_person,
        valid_from=T1,
        valid_to=T2,
    )


@pytest.mark.integration
def test_unconfirmed_name_unit_and_account_versions_do_not_enter_exclusion_set(
    owner_connection: psycopg.Connection[Any],
) -> None:
    school_id = uuid4()
    person_id = uuid4()
    unit_id = uuid4()
    account_id = _insert_user(owner_connection)
    second_account_id = _insert_user(owner_connection)
    _insert_school(owner_connection, school_id, code="A")
    _insert_person(owner_connection, school_id, person_id)
    _insert_organization_unit(owner_connection, school_id=school_id, unit_id=unit_id)
    _insert_person_name(
        owner_connection,
        school_id=school_id,
        person_id=person_id,
        display_name="已确认姓名",
        valid_from=T0,
        valid_to=T2,
    )
    _insert_organization_version(
        owner_connection,
        school_id=school_id,
        unit_id=unit_id,
        display_name="已确认部门",
        valid_from=T0,
        valid_to=T2,
    )
    _insert_account_binding(
        owner_connection,
        school_id=school_id,
        account_user_id=account_id,
        person_id=person_id,
        valid_from=T0,
        valid_to=T2,
    )

    for status in ("draft", "rejected"):
        _insert_person_name(
            owner_connection,
            school_id=school_id,
            person_id=person_id,
            display_name=f"{status}-姓名",
            valid_from=T1,
            valid_to=T3,
            status=status,
        )
        _insert_organization_version(
            owner_connection,
            school_id=school_id,
            unit_id=unit_id,
            display_name=f"{status}-部门",
            valid_from=T1,
            valid_to=T3,
            status=status,
        )
        _insert_account_binding(
            owner_connection,
            school_id=school_id,
            account_user_id=second_account_id,
            person_id=person_id,
            valid_from=T1,
            valid_to=T3,
            status=status,
        )


@pytest.mark.integration
def test_repositories_resolve_historical_snapshots_without_committing(
    owner_session: Session,
) -> None:
    tenancy_repository = importlib.import_module("app.platform.tenancy.repository")
    directory_repository = importlib.import_module("app.platform.directory.repository")
    organization_repository = importlib.import_module("app.platform.organization.repository")

    school_id = uuid4()
    person_id = uuid4()
    unit_id = uuid4()
    assignment_id = uuid4()
    source_id = uuid4()
    school_repository = tenancy_repository.SqlAlchemySchoolRepository(owner_session)
    person_repository = directory_repository.SqlAlchemyPersonRepository(owner_session)
    organization = organization_repository.SqlAlchemyOrganizationRepository(owner_session)

    school = School(
        id=school_id,
        source_key=f"school:repository:{school_id}",
        school_code="A",
        display_name="测试学校",
        timezone_name="Asia/Shanghai",
        status="active",
        version=1,
        created_at=T0,
        updated_at=T0,
    )
    school_repository.add(school)
    owner_session.add(
        SchoolFactSource(
            id=source_id,
            school_id=school_id,
            source_key=f"content:repository:{school_id}",
            source_kind="manual",
            source_ref=f"repository:{school_id}",
            schema_version=1,
            created_at=T0,
        )
    )
    owner_session.flush()
    owner_session.add(
        SchoolVersion(
            id=uuid4(),
            school_id=school_id,
            business_code="A",
            display_name="测试学校",
            short_name=None,
            timezone_name="Asia/Shanghai",
            lifecycle_status="active",
            valid_from=T0,
            valid_to=None,
            version=1,
            source_id=source_id,
            import_batch_id=None,
            created_at=T0,
        )
    )
    owner_session.flush()
    person = SchoolPerson(
        id=person_id,
        school_id=school_id,
        status="active",
        version=1,
        created_at=T0,
        updated_at=T0,
    )
    person_repository.add_person(person)
    person_repository.add_name(
        SchoolPersonName(
            id=uuid4(),
            school_id=school_id,
            person_id=person_id,
            display_name="李明",
            valid_from=T0,
            valid_to=T1,
            source_kind="manual",
            source_ref=str(uuid4()),
            status="confirmed",
            version=1,
            created_at=T0,
            updated_at=T0,
        )
    )
    person_repository.add_name(
        SchoolPersonName(
            id=uuid4(),
            school_id=school_id,
            person_id=person_id,
            display_name="草稿姓名",
            valid_from=T0,
            valid_to=None,
            source_kind="manual",
            source_ref=str(uuid4()),
            status="draft",
            version=3,
            created_at=T1,
            updated_at=T1,
        )
    )
    person_repository.add_name(
        SchoolPersonName(
            id=uuid4(),
            school_id=school_id,
            person_id=person_id,
            display_name="李小明",
            valid_from=T1,
            valid_to=None,
            source_kind="manual",
            source_ref=str(uuid4()),
            status="confirmed",
            version=2,
            created_at=T1,
            updated_at=T1,
        )
    )
    unit = OrganizationUnit(
        id=unit_id,
        school_id=school_id,
        parent_organization_unit_id=None,
        kind="department",
        status="active",
        version=1,
        created_at=T0,
        updated_at=T0,
    )
    organization.add_unit(unit)
    organization.add_unit_version(
        OrganizationUnitVersion(
            id=uuid4(),
            school_id=school_id,
            organization_unit_id=unit_id,
            display_name="总务处",
            short_name="总务",
            display_path="/总务处",
            valid_from=T0,
            valid_to=T1,
            source_kind="manual",
            source_ref=str(uuid4()),
            status="confirmed",
            version=1,
            created_at=T0,
            updated_at=T0,
        )
    )
    organization.add_unit_version(
        OrganizationUnitVersion(
            id=uuid4(),
            school_id=school_id,
            organization_unit_id=unit_id,
            display_name="草稿部门",
            short_name="草稿",
            display_path="/草稿部门",
            valid_from=T0,
            valid_to=None,
            source_kind="manual",
            source_ref=str(uuid4()),
            status="draft",
            version=3,
            created_at=T1,
            updated_at=T1,
        )
    )
    organization.add_unit_version(
        OrganizationUnitVersion(
            id=uuid4(),
            school_id=school_id,
            organization_unit_id=unit_id,
            display_name="后勤处",
            short_name="后勤",
            display_path="/后勤处",
            valid_from=T1,
            valid_to=T3,
            source_kind="manual",
            source_ref=str(uuid4()),
            status="confirmed",
            version=2,
            created_at=T1,
            updated_at=T1,
        )
    )
    assignment = StaffAssignment(
        id=assignment_id,
        school_id=school_id,
        source_key=f"assignment:repository:{assignment_id}",
        person_id=person_id,
        organization_unit_id=unit_id,
        position_title="水电工",
        semantic_role_code="repair.worker",
        appointment_source="manual",
        approval_reference=str(uuid4()),
        valid_from=T0,
        valid_to=T3,
        status="confirmed",
        version=1,
        created_at=T0,
        updated_at=T0,
    )
    organization.add_assignment(assignment)
    owner_session.add(
        StaffAssignmentVersion(
            id=uuid4(),
            school_id=school_id,
            assignment_id=assignment_id,
            person_id=person_id,
            organization_unit_id=unit_id,
            position_title="水电工",
            semantic_role_code="repair.worker",
            assignment_kind="substantive",
            appointment_source="manual",
            approval_reference=assignment.approval_reference,
            effective_from=T0,
            effective_to=None,
            planned_valid_to=T3,
            revoked_at=None,
            status="confirmed",
            target_version=1,
            source_ref=f"repository:{assignment_id}",
            source_id=source_id,
            import_batch_id=None,
            created_at=T0,
            updated_at=T0,
        )
    )
    owner_session.flush()

    assert school_repository.get_for_update(SchoolId(school_id)) is school
    assert (
        person_repository.get_person_for_update(SchoolId(school_id), PersonId(person_id)) is person
    )
    assert (
        organization.get_assignment_for_update(SchoolId(school_id), AssignmentId(assignment_id))
        is assignment
    )
    assert (
        school_repository.get_current_school(SchoolId(school_id), as_of=T2).display_name
        == "测试学校"
    )
    assert (
        person_repository.resolve_person_snapshot(
            SchoolId(school_id), PersonId(person_id), as_of=T0 + timedelta(days=1)
        ).display_name
        == "李明"
    )
    assert (
        person_repository.resolve_person_snapshot(
            SchoolId(school_id), PersonId(person_id), as_of=T2
        ).display_name
        == "李小明"
    )
    assert (
        organization.resolve_assignment_snapshot(
            SchoolId(school_id), AssignmentId(assignment_id), as_of=T0 + timedelta(days=1)
        ).organization_display_name
        == "总务处"
    )
    assert (
        organization.get_current_assignment(
            SchoolId(school_id), AssignmentId(assignment_id), as_of=T2
        ).organization_display_name
        == "后勤处"
    )

    assert (
        organization.get_current_assignment(
            SchoolId(school_id), AssignmentId(assignment_id), as_of=T3
        )
        is None
    )
    assert (
        organization.resolve_assignment_snapshot(
            SchoolId(school_id), AssignmentId(assignment_id), as_of=T2
        )
        is not None
    )

    assert owner_session.in_transaction()


def test_repositories_do_not_own_transaction_or_session_lifecycle() -> None:
    forbidden_calls = {
        "begin",
        "close",
        "commit",
        "create_engine",
        "rollback",
        "sessionmaker",
    }
    for module_name in (
        "app.platform.tenancy.repository",
        "app.platform.directory.repository",
        "app.platform.organization.repository",
        "app.domains.students.repository",
        "app.domains.campus.repository",
        "app.platform.calendar.repository",
    ):
        module = importlib.import_module(module_name)
        module_file = module.__file__
        assert module_file is not None
        module_path = Path(module_file).resolve()
        tree = ast.parse(module_path.read_text(encoding="utf-8"))
        called_attributes = {
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        called_names = {
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        assert forbidden_calls.isdisjoint(called_attributes | called_names), module_name


def test_owner_repositories_do_not_import_other_owner_private_models() -> None:
    repository_owners = {
        "app.platform.tenancy.repository": "app.platform.tenancy.models",
        "app.platform.directory.repository": "app.platform.directory.models",
        "app.platform.organization.repository": "app.platform.organization.models",
        "app.domains.students.repository": "app.domains.students.models",
        "app.domains.campus.repository": "app.domains.campus.models",
        "app.platform.calendar.repository": "app.platform.calendar.models",
    }
    for module_name, allowed_models_module in repository_owners.items():
        module = importlib.import_module(module_name)
        module_file = module.__file__
        assert module_file is not None
        tree = ast.parse(Path(module_file).read_text(encoding="utf-8"))
        package = module_name.rsplit(".", maxsplit=1)[0]
        imported_private_models = _imported_private_model_modules(tree, package=package)
        assert imported_private_models <= {allowed_models_module}, module_name


@pytest.mark.parametrize(
    "source",
    [
        "import app.platform.directory.models as directory_models",
        "from app.platform.directory import models as directory_models",
        "from ..directory import models as directory_models",
        (
            "import importlib\n"
            "directory_models = importlib.import_module('app.platform.directory.models')"
        ),
        (
            "from importlib import import_module\n"
            "directory_models = import_module('app.platform.directory.models')"
        ),
        "directory_models = __import__('app.platform.directory.models')",
    ],
)
def test_private_model_import_gate_rejects_equivalent_bypass_forms(source: str) -> None:
    tree = ast.parse(source)
    assert _imported_private_model_modules(
        tree,
        package="app.platform.organization",
    ) == {"app.platform.directory.models"}


def test_private_model_import_gate_rejects_identity_owner_from_directory() -> None:
    tree = ast.parse("from app.platform.identity.models import User")
    assert _imported_private_model_modules(
        tree,
        package="app.platform.directory",
    ) == {"app.platform.identity.models"}


@pytest.mark.integration
@pytest.mark.destructive_migration
def test_task3_migration_downgrade_and_upgrade_round_trip(
    isolated_revision19_schema: PostgresRoles,
) -> None:
    task3_schema = isolated_revision19_schema
    environment = _migration_environment(task3_schema)
    downgrade = subprocess.run(
        [sys.executable, "-m", "alembic", "downgrade", "20260729_03"],
        cwd=API_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert downgrade.returncode == 0, downgrade.stdout + downgrade.stderr

    try:
        with task3_schema.migration_connection() as connection:
            connection.execute("SET ROLE campus_ai_db_owner")
            remaining_tables = connection.execute(
                """
                SELECT table_name
                FROM information_schema.tables
                WHERE table_schema = 'public'
                  AND table_name = ANY(%s)
                ORDER BY table_name
                """,
                (list(TASK3_TABLES),),
            ).fetchall()
            row = connection.execute(
                """
                SELECT
                    EXISTS (
                        SELECT 1
                        FROM information_schema.columns
                        WHERE table_schema = 'public'
                          AND table_name = 'users'
                          AND column_name = 'security_version'
                    ),
                    (SELECT version_num FROM public.alembic_version)
                """
            ).fetchone()
        assert row == (False, "20260729_03")
        assert remaining_tables == []
    finally:
        upgrade = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=API_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

    assert upgrade.returncode == 0, upgrade.stdout + upgrade.stderr
    with task3_schema.migration_connection() as connection:
        connection.execute("SET ROLE campus_ai_db_owner")
        restored_tables = connection.execute(
            """
            SELECT relation.relname, owner.rolname
            FROM pg_class AS relation
            JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
            JOIN pg_roles AS owner ON owner.oid = relation.relowner
            WHERE namespace.nspname = 'public'
              AND relation.relname = ANY(%s)
            ORDER BY relation.relname
            """,
            (list(TASK3_TABLES),),
        ).fetchall()
        restored = connection.execute(
            """
            SELECT
                EXISTS (
                    SELECT 1
                    FROM information_schema.columns
                    WHERE table_schema = 'public'
                      AND table_name = 'users'
                      AND column_name = 'security_version'
                ),
                (SELECT version_num FROM public.alembic_version)
            """
        ).fetchone()
    assert restored_tables == [(table_name, "campus_ai_db_owner") for table_name in TASK3_TABLES]
    assert restored == (True, _current_alembic_head())
