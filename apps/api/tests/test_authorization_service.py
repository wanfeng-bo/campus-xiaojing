from __future__ import annotations

import os

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.engine import URL, Engine
from sqlalchemy.orm import Session

from app.platform.authorization.bootstrap import INITIAL_ROLE_CODES, seed_initial_roles
from app.platform.authorization.dependencies import require_permission
from app.platform.authorization.models import (
    ResponsibilityScope,
    Role,
    RolePermission,
    UserRole,
)
from app.platform.authorization.service import authorize
from app.platform.identity.context import CurrentUser
from app.platform.identity.models import User
from tests.master_data.conftest import PostgresRoles


def prepare_access_test_database(postgres_roles: PostgresRoles) -> Engine:
    """使用该用例独占、已迁移到 head 的临时数据库。"""

    runtime = postgres_roles.runtime_connection_parameters
    return create_engine(
        URL.create(
            drivername="postgresql+psycopg",
            username=str(runtime["user"]),
            password=str(runtime["password"]),
            host=str(runtime["host"]),
            port=int(runtime["port"]),
            database=str(runtime["dbname"]),
        )
    )


@pytest.mark.integration
def test_authorize_requires_action_and_matching_location_scope(
    isolated_migration_schema: PostgresRoles,
) -> None:
    if os.environ.get("RUN_POSTGRES_INTEGRATION") != "1":
        pytest.skip("需要显式启用本地 PostgreSQL 集成测试")

    engine = prepare_access_test_database(isolated_migration_schema)
    with Session(engine) as session:
        handler = User(external_subject="handler-001", display_name="处理教师", is_active=True)
        teacher = User(external_subject="teacher-001", display_name="普通教师", is_active=True)
        handler_role = Role(code="handler", name="责任处理人", is_system=True)
        teacher_role = Role(code="teacher", name="教师", is_system=True)
        session.add_all([handler, teacher, handler_role, teacher_role])
        session.flush()
        session.add_all(
            [
                UserRole(user_id=handler.id, role_id=handler_role.id),
                UserRole(user_id=teacher.id, role_id=teacher_role.id),
                RolePermission(
                    role_id=handler_role.id,
                    action_code="feedback.handle",
                    scope_type="location",
                ),
                ResponsibilityScope(
                    user_id=handler.id,
                    scope_type="location",
                    scope_value="building-a",
                ),
            ]
        )
        session.commit()

        allowed = authorize(
            session=session,
            current_user=CurrentUser(
                id=handler.id,
                external_subject=handler.external_subject,
                display_name=handler.display_name,
                security_version=handler.security_version,
            ),
            action_code="feedback.handle",
            scope_type="location",
            scope_value="building-a",
        )
        different_location = authorize(
            session=session,
            current_user=CurrentUser(
                id=handler.id,
                external_subject=handler.external_subject,
                display_name=handler.display_name,
                security_version=handler.security_version,
            ),
            action_code="feedback.handle",
            scope_type="location",
            scope_value="building-b",
        )
        teacher_denied = authorize(
            session=session,
            current_user=CurrentUser(
                id=teacher.id,
                external_subject=teacher.external_subject,
                display_name=teacher.display_name,
                security_version=teacher.security_version,
            ),
            action_code="feedback.handle",
            scope_type="location",
            scope_value="building-a",
        )

    assert allowed.allowed is True
    assert different_location.allowed is False
    assert teacher_denied.allowed is False


@pytest.mark.integration
def test_seed_initial_roles_is_repeatable_without_fixed_business_roles(
    isolated_migration_schema: PostgresRoles,
) -> None:
    if os.environ.get("RUN_POSTGRES_INTEGRATION") != "1":
        pytest.skip("需要显式启用本地 PostgreSQL 集成测试")

    engine = prepare_access_test_database(isolated_migration_schema)
    with Session(engine) as session:
        created_codes = seed_initial_roles(session)
        session.commit()

    with Session(engine) as session:
        saved_codes = set(session.scalars(select(Role.code)).all())
        repeated_codes = seed_initial_roles(session)

    assert set(created_codes) == set(INITIAL_ROLE_CODES)
    assert saved_codes == set(INITIAL_ROLE_CODES)
    assert repeated_codes == []


@pytest.mark.integration
def test_authorize_denies_inactive_user_even_with_permission_and_scope(
    isolated_migration_schema: PostgresRoles,
) -> None:
    if os.environ.get("RUN_POSTGRES_INTEGRATION") != "1":
        pytest.skip("需要显式启用本地 PostgreSQL 集成测试")

    engine = prepare_access_test_database(isolated_migration_schema)
    with Session(engine) as session:
        inactive_handler = User(
            external_subject="inactive-handler-001",
            display_name="已停用处理人",
            is_active=False,
        )
        handler_role = Role(code="handler", name="责任处理人", is_system=True)
        session.add_all([inactive_handler, handler_role])
        session.flush()
        session.add_all(
            [
                UserRole(user_id=inactive_handler.id, role_id=handler_role.id),
                RolePermission(
                    role_id=handler_role.id,
                    action_code="feedback.handle",
                    scope_type="location",
                ),
                ResponsibilityScope(
                    user_id=inactive_handler.id,
                    scope_type="location",
                    scope_value="building-a",
                ),
            ]
        )
        session.commit()

        decision = authorize(
            session=session,
            current_user=CurrentUser(
                id=inactive_handler.id,
                external_subject=inactive_handler.external_subject,
                display_name=inactive_handler.display_name,
                security_version=inactive_handler.security_version,
            ),
            action_code="feedback.handle",
            scope_type="location",
            scope_value="building-a",
        )

    assert decision.allowed is False
    assert decision.reason == "当前用户已停用或不存在。"


@pytest.mark.integration
def test_permission_dependency_returns_safe_forbidden_message(
    isolated_migration_schema: PostgresRoles,
) -> None:
    if os.environ.get("RUN_POSTGRES_INTEGRATION") != "1":
        pytest.skip("需要显式启用本地 PostgreSQL 集成测试")

    engine = prepare_access_test_database(isolated_migration_schema)
    with Session(engine) as session:
        handler = User(external_subject="handler-001", display_name="处理教师", is_active=True)
        handler_role = Role(code="handler", name="责任处理人", is_system=True)
        session.add_all([handler, handler_role])
        session.flush()
        session.add_all(
            [
                UserRole(user_id=handler.id, role_id=handler_role.id),
                RolePermission(
                    role_id=handler_role.id,
                    action_code="feedback.handle",
                    scope_type="location",
                ),
                ResponsibilityScope(
                    user_id=handler.id,
                    scope_type="location",
                    scope_value="building-a",
                ),
            ]
        )
        session.commit()
        current_user = CurrentUser(
            id=handler.id,
            external_subject=handler.external_subject,
            display_name=handler.display_name,
            security_version=handler.security_version,
        )

        allowed_dependency = require_permission("feedback.handle", "location", "building-a")
        denied_dependency = require_permission("feedback.handle", "location", "building-b")

        assert allowed_dependency(current_user=current_user, session=session) == current_user
        with pytest.raises(HTTPException) as error:
            denied_dependency(current_user=current_user, session=session)

    assert error.value.status_code == 403
    assert error.value.detail == "你暂无执行该操作的权限。"
