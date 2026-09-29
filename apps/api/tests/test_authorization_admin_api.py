from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine import URL, Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.db.connection import get_session
from app.main import app
from app.platform.audit.models import AuditLog
from app.platform.authorization.models import (
    ResponsibilityScope,
    Role,
    RolePermission,
    UserRole,
)
from app.platform.authorization.router import router as authorization_router
from app.platform.authorization.service import authorize
from app.platform.identity.context import CurrentUser
from app.platform.identity.models import User
from tests.master_data.conftest import PostgresRoles


@dataclass(frozen=True)
class PreparedAccessData:
    """保存管理 API 集成测试所需的用户与角色主键。"""

    admin_id: int
    teacher_id: int
    handler_id: int
    member_id: int


def _roles_engine(parameters: dict[str, object]) -> Engine:
    port = parameters["port"]
    if not isinstance(port, (int, str)):
        raise TypeError("PostgreSQL port must be an integer or decimal string")
    return create_engine(
        URL.create(
            drivername="postgresql+psycopg",
            username=str(parameters["user"]),
            password=str(parameters["password"]),
            host=str(parameters["host"]),
            port=int(port),
            database=str(parameters["dbname"]),
        )
    )


def prepare_admin_api_database(
    postgres_roles: PostgresRoles,
) -> tuple[Engine, PreparedAccessData]:
    """在该用例独占的临时库中准备最小授权事实。"""

    engine = _roles_engine(postgres_roles.runtime_connection_parameters)
    with Session(engine) as session:
        admin = User(external_subject="admin-001", display_name="测试管理员", is_active=True)
        teacher = User(external_subject="teacher-001", display_name="测试教师", is_active=True)
        handler = User(external_subject="handler-001", display_name="责任处理人", is_active=True)
        member = User(
            external_subject="member-acceptance-001",
            display_name="合成普通成员",
            is_active=True,
        )
        system_admin_role = Role(code="system_admin", name="系统管理员", is_system=True)
        teacher_role = Role(code="teacher", name="教师", is_system=True)
        handler_role = Role(code="handler", name="责任处理人", is_system=True)
        session.add_all(
            [admin, teacher, handler, member, system_admin_role, teacher_role, handler_role]
        )
        session.flush()
        session.add_all(
            [
                UserRole(user_id=admin.id, role_id=system_admin_role.id),
                UserRole(user_id=teacher.id, role_id=teacher_role.id),
                UserRole(user_id=handler.id, role_id=handler_role.id),
                RolePermission(
                    role_id=system_admin_role.id,
                    action_code="system.access.write",
                    scope_type="all",
                ),
                RolePermission(
                    role_id=handler_role.id,
                    action_code="feedback.handle",
                    scope_type="location",
                ),
            ]
        )
        prepared_data = PreparedAccessData(
            admin_id=admin.id,
            teacher_id=teacher.id,
            handler_id=handler.id,
            member_id=member.id,
        )
        session.commit()

    def isolated_runtime_session() -> Iterator[Session]:
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_session] = isolated_runtime_session

    return engine, prepared_data


def build_test_admin_headers() -> dict[str, str]:
    """返回模拟钉钉测试管理员身份的请求头。"""

    return {
        "X-Test-Subject": "admin-001",
        "X-Test-Display-Name": "Test Admin",
        "X-Request-ID": "client-controlled-request-id",
    }


def build_test_member_headers() -> dict[str, str]:
    """返回无角色合成普通成员的本地测试身份头。"""

    return {
        "X-Test-Subject": "member-acceptance-001",
        "X-Test-Display-Name": "Synthetic Member",
    }


@contextmanager
def audit_assertion_session(postgres_roles: PostgresRoles) -> Iterator[Session]:
    """用受控数据库 Owner 身份读取审计断言，业务请求仍走 runtime。"""

    engine = _roles_engine(postgres_roles.migration_connection_parameters)
    try:
        with Session(engine) as session:
            session.execute(text("SET LOCAL ROLE campus_ai_db_owner"))
            yield session
    finally:
        engine.dispose()


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(autouse=True)
def clear_isolated_runtime_session_override() -> Iterator[None]:
    yield
    app.dependency_overrides.pop(get_session, None)


def test_role_revocation_route_is_registered() -> None:
    """普通角色撤销必须由唯一的 DELETE 管理路由提供。"""

    matches = [
        route
        for route in authorization_router.routes
        if getattr(route, "path", None) == "/api/v1/admin/users/{user_id}/roles/{role_code}"
        and "DELETE" in getattr(route, "methods", set())
    ]
    assert len(matches) == 1


@pytest.mark.anyio
async def test_admin_assigns_scope_with_audit_and_takes_effect_on_next_check(
    isolated_migration_schema: PostgresRoles,
) -> None:
    if os.environ.get("RUN_POSTGRES_INTEGRATION") != "1":
        pytest.skip("需要显式启用本地 PostgreSQL 集成测试")

    engine, prepared_data = prepare_admin_api_database(isolated_migration_schema)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            f"/api/v1/admin/users/{prepared_data.handler_id}/scopes",
            headers=build_test_admin_headers(),
            json={
                "scope_type": "location",
                "scope_value": "building-a",
                "reason": "负责范围调整",
            },
        )
        duplicate_response = await client.post(
            f"/api/v1/admin/users/{prepared_data.handler_id}/scopes",
            headers=build_test_admin_headers(),
            json={
                "scope_type": "location",
                "scope_value": "building-a",
                "reason": "重复配置验证",
            },
        )

    assert response.status_code == 201
    assert response.json()["scope_value"] == "building-a"
    assert duplicate_response.status_code == 409

    with Session(engine) as session:
        scope_count = session.scalar(select(func.count()).select_from(ResponsibilityScope))
        decision = authorize(
            session=session,
            current_user=CurrentUser(
                id=prepared_data.handler_id,
                external_subject="handler-001",
                display_name="责任处理人",
                security_version=1,
            ),
            action_code="feedback.handle",
            scope_type="location",
            scope_value="building-a",
        )
    with audit_assertion_session(isolated_migration_schema) as session:
        audit_logs = session.scalars(select(AuditLog).order_by(AuditLog.id)).all()

    assert scope_count == 1
    assert len(audit_logs) == 1
    assert audit_logs[0].event_type == "authorization.scope.assigned"
    assert audit_logs[0].actor_user_id == prepared_data.admin_id
    assert audit_logs[0].after_summary["scope_value"] == "building-a"
    assert audit_logs[0].request_id != "client-controlled-request-id"
    assert str(UUID(audit_logs[0].request_id)) == audit_logs[0].request_id
    assert decision.allowed is True


@pytest.mark.anyio
async def test_admin_api_rejects_unauthorized_user_and_missing_reason(
    isolated_migration_schema: PostgresRoles,
) -> None:
    if os.environ.get("RUN_POSTGRES_INTEGRATION") != "1":
        pytest.skip("需要显式启用本地 PostgreSQL 集成测试")

    engine, prepared_data = prepare_admin_api_database(isolated_migration_schema)
    teacher_headers = {
        "X-Test-Subject": "teacher-001",
        "X-Test-Display-Name": "Test Teacher",
    }
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        forbidden_response = await client.post(
            f"/api/v1/admin/users/{prepared_data.handler_id}/scopes",
            headers=teacher_headers,
            json={
                "scope_type": "location",
                "scope_value": "building-a",
                "reason": "越权操作验证",
            },
        )
        invalid_response = await client.post(
            f"/api/v1/admin/users/{prepared_data.handler_id}/scopes",
            headers=build_test_admin_headers(),
            json={"scope_type": "location", "scope_value": "building-a"},
        )

    with Session(engine) as session:
        scope_count = session.scalar(select(func.count()).select_from(ResponsibilityScope))
    with audit_assertion_session(isolated_migration_schema) as session:
        audit_count = session.scalar(select(func.count()).select_from(AuditLog))

    assert forbidden_response.status_code == 403
    assert forbidden_response.json()["detail"] == "你暂无执行该操作的权限。"
    assert invalid_response.status_code == 422
    assert scope_count == 0
    assert audit_count == 0


@pytest.mark.anyio
async def test_admin_assigns_role_and_permission_with_audit_records(
    isolated_migration_schema: PostgresRoles,
) -> None:
    if os.environ.get("RUN_POSTGRES_INTEGRATION") != "1":
        pytest.skip("需要显式启用本地 PostgreSQL 集成测试")

    engine, prepared_data = prepare_admin_api_database(isolated_migration_schema)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        role_response = await client.post(
            f"/api/v1/admin/users/{prepared_data.teacher_id}/roles",
            headers=build_test_admin_headers(),
            json={"role_code": "handler", "reason": "增加处理职责"},
        )
        permission_response = await client.post(
            "/api/v1/admin/roles/teacher/permissions",
            headers=build_test_admin_headers(),
            json={
                "action_code": "feedback.handle",
                "scope_type": "location",
                "reason": "补充处理权限",
            },
        )

    with audit_assertion_session(isolated_migration_schema) as session:
        audit_event_types = session.scalars(select(AuditLog.event_type).order_by(AuditLog.id)).all()

    assert role_response.status_code == 201
    assert role_response.json()["role_code"] == "handler"
    assert permission_response.status_code == 201
    assert permission_response.json()["action_code"] == "feedback.handle"
    assert audit_event_types == [
        "authorization.role.assigned",
        "authorization.permission.granted",
    ]


@pytest.mark.anyio
@pytest.mark.integration
async def test_admin_revokes_non_admin_role_with_audit(
    isolated_migration_schema: PostgresRoles,
) -> None:
    """管理员撤销普通角色后，关系删除且只写入一次完整撤权审计。"""

    if os.environ.get("RUN_POSTGRES_INTEGRATION") != "1":
        pytest.skip("需要显式启用本地 PostgreSQL 集成测试")

    engine, prepared_data = prepare_admin_api_database(isolated_migration_schema)
    revoke_url = f"/api/v1/admin/users/{prepared_data.member_id}/roles/handler"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assign_response = await client.post(
            f"/api/v1/admin/users/{prepared_data.member_id}/roles",
            headers=build_test_admin_headers(),
            json={"role_code": "handler", "reason": "授予最小处理职责"},
        )
        revoke_response = await client.request(
            "DELETE",
            revoke_url,
            headers=build_test_admin_headers(),
            json={"reason": "结束最小权限验收"},
        )
        duplicate_response = await client.request(
            "DELETE",
            revoke_url,
            headers=build_test_admin_headers(),
            json={"reason": "重复撤权验证"},
        )

    assert assign_response.status_code == 201
    assert revoke_response.status_code == 200
    assert revoke_response.json() == {
        "id": assign_response.json()["id"],
        "user_id": prepared_data.member_id,
        "role_code": "handler",
        "status": "deleted",
    }
    assert duplicate_response.status_code == 404

    with Session(engine) as session:
        assignment = session.scalar(
            select(UserRole)
            .join(Role, Role.id == UserRole.role_id)
            .where(UserRole.user_id == prepared_data.member_id, Role.code == "handler")
        )
    with audit_assertion_session(isolated_migration_schema) as session:
        audit_logs = session.scalars(select(AuditLog).order_by(AuditLog.id)).all()

    assert assignment is None
    assert [item.event_type for item in audit_logs] == [
        "authorization.role.assigned",
        "authorization.role.revoked",
    ]
    assert audit_logs[-1].before_summary == {
        "user_id": prepared_data.member_id,
        "role_code": "handler",
    }
    assert audit_logs[-1].after_summary == {}
    assert audit_logs[-1].reason == "结束最小权限验收"
    assert str(UUID(audit_logs[-1].request_id)) == audit_logs[-1].request_id


@pytest.mark.anyio
@pytest.mark.integration
@pytest.mark.parametrize(
    ("target", "payload", "expected_status"),
    [
        ("missing-user", {"reason": "不存在用户"}, 404),
        ("missing-role", {"reason": "不存在角色"}, 404),
        ("missing-assignment", {"reason": "不存在关系"}, 404),
        ("missing-reason", {}, 422),
        ("blank-reason", {"reason": "   "}, 422),
    ],
)
async def test_role_revocation_contract_errors(
    isolated_migration_schema: PostgresRoles,
    target: str,
    payload: dict[str, str],
    expected_status: int,
) -> None:
    """错误目标或理由不得删除角色，也不得产生撤权审计。"""

    if os.environ.get("RUN_POSTGRES_INTEGRATION") != "1":
        pytest.skip("需要显式启用本地 PostgreSQL 集成测试")

    engine, prepared_data = prepare_admin_api_database(isolated_migration_schema)
    targets = {
        "missing-user": "/api/v1/admin/users/999999/roles/handler",
        "missing-role": f"/api/v1/admin/users/{prepared_data.member_id}/roles/missing_role",
        "missing-assignment": f"/api/v1/admin/users/{prepared_data.member_id}/roles/handler",
        "missing-reason": f"/api/v1/admin/users/{prepared_data.member_id}/roles/handler",
        "blank-reason": f"/api/v1/admin/users/{prepared_data.member_id}/roles/handler",
    }
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.request(
            "DELETE",
            targets[target],
            headers=build_test_admin_headers(),
            json=payload,
        )

    with audit_assertion_session(isolated_migration_schema) as session:
        revoke_audit_count = session.scalar(
            select(func.count())
            .select_from(AuditLog)
            .where(AuditLog.event_type == "authorization.role.revoked")
        )

    assert response.status_code == expected_status
    assert revoke_audit_count == 0


@pytest.mark.anyio
@pytest.mark.integration
async def test_role_revocation_rejects_unauthorized_user(
    isolated_migration_schema: PostgresRoles,
) -> None:
    """普通教师不能撤销其他用户的角色。"""

    if os.environ.get("RUN_POSTGRES_INTEGRATION") != "1":
        pytest.skip("需要显式启用本地 PostgreSQL 集成测试")

    engine, prepared_data = prepare_admin_api_database(isolated_migration_schema)
    revoke_url = f"/api/v1/admin/users/{prepared_data.member_id}/roles/handler"
    teacher_headers = {
        "X-Test-Subject": "teacher-001",
        "X-Test-Display-Name": "Test Teacher",
    }
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assign_response = await client.post(
            f"/api/v1/admin/users/{prepared_data.member_id}/roles",
            headers=build_test_admin_headers(),
            json={"role_code": "handler", "reason": "准备越权撤销测试"},
        )
        forbidden_response = await client.request(
            "DELETE",
            revoke_url,
            headers=teacher_headers,
            json={"reason": "不应允许的撤权"},
        )

    assert assign_response.status_code == 201
    assert forbidden_response.status_code == 403
    assert forbidden_response.json()["detail"] == "你暂无执行该操作的权限。"
    with Session(engine) as session:
        assignment_count = session.scalar(
            select(func.count())
            .select_from(UserRole)
            .join(Role, Role.id == UserRole.role_id)
            .where(UserRole.user_id == prepared_data.member_id, Role.code == "handler")
        )
    with audit_assertion_session(isolated_migration_schema) as session:
        revoke_audit_count = session.scalar(
            select(func.count())
            .select_from(AuditLog)
            .where(AuditLog.event_type == "authorization.role.revoked")
        )
    assert assignment_count == 1
    assert revoke_audit_count == 0


@pytest.mark.anyio
@pytest.mark.integration
async def test_role_revocation_protects_system_admin(
    isolated_migration_schema: PostgresRoles,
) -> None:
    """通用撤权接口必须拒绝删除系统管理员角色。"""

    if os.environ.get("RUN_POSTGRES_INTEGRATION") != "1":
        pytest.skip("需要显式启用本地 PostgreSQL 集成测试")

    engine, prepared_data = prepare_admin_api_database(isolated_migration_schema)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.request(
            "DELETE",
            f"/api/v1/admin/users/{prepared_data.admin_id}/roles/system_admin",
            headers=build_test_admin_headers(),
            json={"reason": "不应允许的系统管理员撤权"},
        )

    assert response.status_code == 409
    with Session(engine) as session:
        assignment_count = session.scalar(
            select(func.count())
            .select_from(UserRole)
            .join(Role, Role.id == UserRole.role_id)
            .where(UserRole.user_id == prepared_data.admin_id, Role.code == "system_admin")
        )
    with audit_assertion_session(isolated_migration_schema) as session:
        audit_count = session.scalar(select(func.count()).select_from(AuditLog))
    assert assignment_count == 1
    assert audit_count == 0


@pytest.mark.anyio
@pytest.mark.integration
async def test_role_revocation_rolls_back_when_audit_write_fails(
    isolated_migration_schema: PostgresRoles,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """撤权审计失败时必须回滚角色删除。"""

    if os.environ.get("RUN_POSTGRES_INTEGRATION") != "1":
        pytest.skip("需要显式启用本地 PostgreSQL 集成测试")

    engine, prepared_data = prepare_admin_api_database(isolated_migration_schema)
    revoke_url = f"/api/v1/admin/users/{prepared_data.member_id}/roles/handler"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assign_response = await client.post(
            f"/api/v1/admin/users/{prepared_data.member_id}/roles",
            headers=build_test_admin_headers(),
            json={"role_code": "handler", "reason": "准备回滚测试"},
        )

        def fail_audit_write(**_: object) -> None:
            raise SQLAlchemyError("forced audit failure")

        monkeypatch.setattr(
            "app.platform.authorization.router.append_audit_log",
            fail_audit_write,
        )
        response = await client.request(
            "DELETE",
            revoke_url,
            headers=build_test_admin_headers(),
            json={"reason": "触发审计失败回滚"},
        )

    assert assign_response.status_code == 201
    assert response.status_code == 500
    assert response.json()["detail"] == "角色撤销保存失败，请稍后重试。"
    with Session(engine) as session:
        assignment_count = session.scalar(
            select(func.count())
            .select_from(UserRole)
            .join(Role, Role.id == UserRole.role_id)
            .where(UserRole.user_id == prepared_data.member_id, Role.code == "handler")
        )
    with audit_assertion_session(isolated_migration_schema) as session:
        event_types = session.scalars(select(AuditLog.event_type).order_by(AuditLog.id)).all()
    assert assignment_count == 1
    assert event_types == ["authorization.role.assigned"]


@pytest.mark.anyio
@pytest.mark.integration
async def test_concurrent_role_revocations_succeed_once(
    isolated_migration_schema: PostgresRoles,
) -> None:
    """两个并发撤权请求最多一个成功，并且只产生一条撤权审计。"""

    if os.environ.get("RUN_POSTGRES_INTEGRATION") != "1":
        pytest.skip("需要显式启用本地 PostgreSQL 集成测试")

    engine, prepared_data = prepare_admin_api_database(isolated_migration_schema)
    revoke_url = f"/api/v1/admin/users/{prepared_data.member_id}/roles/handler"
    admin_headers = build_test_admin_headers()
    revoke_payload = {"reason": "并发撤权验收"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assign_response = await client.post(
            f"/api/v1/admin/users/{prepared_data.member_id}/roles",
            headers=admin_headers,
            json={"role_code": "handler", "reason": "准备并发撤权测试"},
        )
        responses = await asyncio.gather(
            client.request("DELETE", revoke_url, headers=admin_headers, json=revoke_payload),
            client.request("DELETE", revoke_url, headers=admin_headers, json=revoke_payload),
        )

    assert assign_response.status_code == 201
    assert sorted(response.status_code for response in responses) == [200, 404]
    with audit_assertion_session(isolated_migration_schema) as session:
        revoke_audit_count = session.scalar(
            select(func.count())
            .select_from(AuditLog)
            .where(AuditLog.event_type == "authorization.role.revoked")
        )
    assert revoke_audit_count == 1


@pytest.mark.anyio
@pytest.mark.integration
async def test_ordinary_member_permission_lifecycle_is_immediate_and_scoped(
    isolated_migration_schema: PostgresRoles,
) -> None:
    """普通成员授权只覆盖指定地点，撤权后下一次判断立即失效。"""

    if os.environ.get("RUN_POSTGRES_INTEGRATION") != "1":
        pytest.skip("需要显式启用本地 PostgreSQL 集成测试")

    engine, prepared_data = prepare_admin_api_database(isolated_migration_schema)
    member = CurrentUser(
        id=prepared_data.member_id,
        external_subject="member-acceptance-001",
        display_name="合成普通成员",
        security_version=1,
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        pending_response = await client.get("/api/v1/me", headers=build_test_member_headers())
        with Session(engine) as session:
            denied_before = authorize(
                session=session,
                current_user=member,
                action_code="feedback.handle",
                scope_type="location",
                scope_value="acceptance-location",
            )

        scope_response = await client.post(
            f"/api/v1/admin/users/{prepared_data.member_id}/scopes",
            headers=build_test_admin_headers(),
            json={
                "scope_type": "location",
                "scope_value": "acceptance-location",
                "reason": "配置验收地点范围",
            },
        )
        role_response = await client.post(
            f"/api/v1/admin/users/{prepared_data.member_id}/roles",
            headers=build_test_admin_headers(),
            json={"role_code": "handler", "reason": "授予最小反馈处理职责"},
        )
        ready_response = await client.get("/api/v1/me", headers=build_test_member_headers())
        with Session(engine) as session:
            allowed_target = authorize(
                session=session,
                current_user=member,
                action_code="feedback.handle",
                scope_type="location",
                scope_value="acceptance-location",
            )
            denied_other = authorize(
                session=session,
                current_user=member,
                action_code="feedback.handle",
                scope_type="location",
                scope_value="other-location",
            )

        revoke_response = await client.request(
            "DELETE",
            f"/api/v1/admin/users/{prepared_data.member_id}/roles/handler",
            headers=build_test_admin_headers(),
            json={"reason": "结束最小权限验收"},
        )
        pending_again_response = await client.get(
            "/api/v1/me",
            headers=build_test_member_headers(),
        )

    with Session(engine) as session:
        denied_after = authorize(
            session=session,
            current_user=member,
            action_code="feedback.handle",
            scope_type="location",
            scope_value="acceptance-location",
        )
        remaining_scope_count = session.scalar(
            select(func.count())
            .select_from(ResponsibilityScope)
            .where(ResponsibilityScope.user_id == prepared_data.member_id)
        )
    with audit_assertion_session(isolated_migration_schema) as session:
        event_types = session.scalars(select(AuditLog.event_type).order_by(AuditLog.id)).all()

    assert pending_response.json() == {
        "id": prepared_data.member_id,
        "external_subject": "member-acceptance-001",
        "display_name": "合成普通成员",
        "access_state": "pending_configuration",
        "can_manage_system": False,
    }
    assert denied_before.allowed is False
    assert scope_response.status_code == 201
    assert role_response.status_code == 201
    assert ready_response.json()["access_state"] == "ready"
    assert ready_response.json()["can_manage_system"] is False
    assert allowed_target.allowed is True
    assert denied_other.allowed is False
    assert revoke_response.status_code == 200
    assert pending_again_response.json()["access_state"] == "pending_configuration"
    assert pending_again_response.json()["can_manage_system"] is False
    assert denied_after.allowed is False
    assert remaining_scope_count == 1
    assert event_types == [
        "authorization.scope.assigned",
        "authorization.role.assigned",
        "authorization.role.revoked",
    ]
