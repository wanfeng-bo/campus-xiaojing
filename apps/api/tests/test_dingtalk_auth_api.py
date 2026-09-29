from __future__ import annotations

import logging
from collections.abc import Generator
from contextlib import contextmanager
from typing import cast

import pytest
from httpx import ASGITransport, AsyncClient
from redis.exceptions import RedisError
from sqlalchemy.orm import Session

from app.core.settings import Settings, get_settings
from app.db.connection import get_session
from app.integrations.dingtalk.client import (
    DingTalkCredentialError,
    DingTalkIdentity,
    DingTalkUnavailableError,
)
from app.main import app
from app.platform.identity.context import CurrentUser, get_current_user
from app.platform.identity.login_guard import (
    DingTalkLoginGuardUnavailableError,
    DingTalkLoginMaintenanceError,
    DingTalkLoginRateLimitError,
)
from app.platform.identity.models import User
from app.platform.identity.router import (
    IdentityAccessStatus,
    get_dingtalk_identity_verifier,
    get_dingtalk_login_guard,
    get_identity_session_store,
)


class FakeVerifier:
    """按测试预设返回钉钉身份或抛出钉钉验证错误。"""

    def __init__(
        self,
        result: DingTalkIdentity | Exception,
        *,
        trusted_mobile: str | None = None,
        events: list[str] | None = None,
    ) -> None:
        """保存本次测试需要的身份结果。"""

        self.result = result
        self.trusted_mobile = trusted_mobile
        self.events = events
        self.verify_calls = 0
        self.mobile_calls = 0

    def verify(self, auth_code: str) -> DingTalkIdentity:
        """验证测试免登码，并确保路由没有替换或保存原始 code。"""

        self.verify_calls += 1
        if self.events is not None:
            self.events.append("verify")
        assert auth_code == "one-time-code"
        if isinstance(self.result, Exception):
            raise self.result
        return self.result

    def fetch_optional_mobile(self, personnel_id: str) -> str | None:
        """模拟验证完成后的可选电话查询，不把电话塞进 DingTalkIdentity。"""

        self.mobile_calls += 1
        if self.events is not None:
            self.events.append("mobile")
        if isinstance(self.result, Exception):
            raise AssertionError("身份验证失败后不应查询电话")
        assert personnel_id == self.result.personnel_id
        return self.trusted_mobile


class FakeSessionStore:
    """记录认证 API 创建和删除的本地会话，不连接真实 Redis。"""

    def __init__(self, *, create_error: Exception | None = None) -> None:
        """初始化没有会话副作用的存储替身。"""

        self.created_for: CurrentUser | None = None
        self.deleted_token: str | None = None
        self.create_error = create_error

    def create(self, current_user: CurrentUser) -> str:
        """记录创建会话的用户并返回固定不透明测试令牌。"""

        if self.create_error is not None:
            raise self.create_error
        self.created_for = current_user
        return "opaque-session-token"

    def delete(self, token: str) -> None:
        """记录退出时请求删除的会话令牌。"""

        self.deleted_token = token


class FakeDatabaseSession:
    """记录认证路由提交或回滚行为的最小数据库会话替身。"""

    def __init__(self) -> None:
        """初始化提交和回滚记录。"""

        self.committed = False
        self.rolled_back = False

    def commit(self) -> None:
        """记录映射和审计事务已提交。"""

        self.committed = True

    def rollback(self) -> None:
        """记录失败身份交换已回滚。"""

        self.rolled_back = True


class RejectingLoginGuard:
    """在 API 测试中模拟已分类的登录门拒绝，不访问 Redis。"""

    def __init__(self, error: Exception) -> None:
        """保存身份交换应接收的预设登录保护错误。"""

        self.error = error

    @contextmanager
    def admit(self, request_id: str):
        """以真实上下文管理器协议在验证一次性码前抛出拒绝错误。"""

        assert request_id
        raise self.error
        yield


class PermittingLoginGuard:
    """在 API 测试中提供无 Redis 副作用的允许上下文，隔离会话故障路径。"""

    @contextmanager
    def admit(self, request_id: str):
        """模拟已取得容量租约，允许测试进入后续会话创建逻辑。"""

        assert request_id
        yield


def build_dingtalk_settings() -> Settings:
    """构造认证 API 测试所需的无真实密钥钉钉运行配置。"""

    return Settings.model_validate(
        {
            "app_env": "test",
            "postgres_host": "127.0.0.1",
            "postgres_port": 5432,
            "postgres_db": "campus_ai_test",
            "postgres_user": "tester",
            "postgres_password": "not-a-real-password",
            "rls_context_signing_key": "test-runtime-context-signing-key-32-bytes",
            "redis_host": "127.0.0.1",
            "redis_port": 6379,
            "redis_password": "not-a-real-redis-password",
            "identity_provider": "dingtalk",
            "dingtalk_corp_id": "ding-test-corp",
            "dingtalk_app_key": "test-app-key",
            "dingtalk_app_secret": "test-app-secret",
        }
    )


@pytest.fixture
def anyio_backend() -> str:
    """将 API 测试固定在 asyncio 后端。"""

    return "asyncio"


@pytest.fixture(autouse=True)
def isolate_dingtalk_auth_dependencies() -> Generator[None]:
    """默认隔离登录守卫，并在每个测试后恢复共享应用原有依赖覆盖。"""

    previous_overrides = app.dependency_overrides.copy()
    app.dependency_overrides[get_dingtalk_login_guard] = PermittingLoginGuard
    try:
        yield
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous_overrides)


@pytest.mark.anyio
async def test_exchange_sets_cookie_and_returns_pending_state(monkeypatch) -> None:
    """首次登录只建立本地会话，未配置角色时仅返回待配置访问状态。"""

    from app.platform.identity import router as identity_router

    database_session = FakeDatabaseSession()
    session_store = FakeSessionStore()
    identity = DingTalkIdentity(
        corporation_id="ding-test-corp",
        personnel_id="teacher-001",
        display_name="测试教师",
    )
    mapped_user = User(
        id=7,
        external_subject="dingtalk:ding-test-corp:teacher-001",
        display_name="测试教师",
        security_version=1,
        is_active=True,
    )
    monkeypatch.setattr(
        identity_router,
        "find_or_create_dingtalk_user",
        lambda *_args, **_kwargs: mapped_user,
    )
    monkeypatch.setattr(
        identity_router,
        "determine_access_status",
        lambda *_args, **_kwargs: IdentityAccessStatus(
            access_state="pending_configuration",
            can_manage_system=False,
        ),
    )
    app.dependency_overrides[get_settings] = build_dingtalk_settings
    app.dependency_overrides[get_session] = lambda: cast(Session, database_session)
    verifier = FakeVerifier(identity, trusted_mobile="13800000000")
    app.dependency_overrides[get_dingtalk_identity_verifier] = lambda: verifier
    app.dependency_overrides[get_identity_session_store] = lambda: session_store

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as client:
            response = await client.post(
                "/api/v1/auth/dingtalk/exchange",
                json={"auth_code": "one-time-code"},
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {
        "access_state": "pending_configuration",
        "can_manage_system": False,
    }
    assert "HttpOnly" in response.headers["set-cookie"]
    assert "Secure" in response.headers["set-cookie"]
    assert "SameSite=lax" in response.headers["set-cookie"]
    assert session_store.created_for == CurrentUser(
        id=7,
        external_subject="dingtalk:ding-test-corp:teacher-001",
        display_name="测试教师",
        security_version=1,
        trusted_mobile="13800000000",
    )
    assert verifier.mobile_calls == 1
    assert database_session.committed is True


@pytest.mark.anyio
async def test_exchange_fetches_mobile_after_userid_verification_and_before_database(
    monkeypatch,
) -> None:
    """可选电话必须由已验证 userid 获取，并且在登录链第一次数据库访问之前完成。"""

    from app.platform.identity import router as identity_router

    events: list[str] = []
    database_session = FakeDatabaseSession()
    identity = DingTalkIdentity(
        corporation_id="ding-test-corp",
        personnel_id="teacher-001",
        display_name="测试教师",
    )
    mapped_user = User(
        id=7,
        external_subject="dingtalk:ding-test-corp:teacher-001",
        display_name="测试教师",
        security_version=1,
        is_active=True,
    )
    verifier = FakeVerifier(identity, trusted_mobile=None, events=events)

    def map_user(*_args, **_kwargs) -> User:
        events.append("database")
        return mapped_user

    monkeypatch.setattr(identity_router, "find_or_create_dingtalk_user", map_user)
    monkeypatch.setattr(
        identity_router,
        "determine_access_status",
        lambda *_args, **_kwargs: IdentityAccessStatus(
            access_state="pending_configuration",
            can_manage_system=False,
        ),
    )
    app.dependency_overrides[get_settings] = build_dingtalk_settings
    app.dependency_overrides[get_session] = lambda: cast(Session, database_session)
    app.dependency_overrides[get_dingtalk_identity_verifier] = lambda: verifier
    app.dependency_overrides[get_identity_session_store] = lambda: FakeSessionStore()

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as client:
            response = await client.post(
                "/api/v1/auth/dingtalk/exchange",
                json={"auth_code": "one-time-code"},
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert events == ["verify", "mobile", "database"]


@pytest.mark.anyio
async def test_exchange_maps_invalid_code_to_safe_unauthorized_response() -> None:
    """钉钉拒绝一次性 code 时不得创建本地会话，并返回重新进入工作台提示。"""

    app.dependency_overrides[get_settings] = build_dingtalk_settings
    app.dependency_overrides[get_dingtalk_identity_verifier] = lambda: FakeVerifier(
        DingTalkCredentialError("invalid code")
    )
    app.dependency_overrides[get_identity_session_store] = lambda: FakeSessionStore()

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as client:
            response = await client.post(
                "/api/v1/auth/dingtalk/exchange",
                json={"auth_code": "one-time-code"},
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 401
    assert response.json()["detail"] == "钉钉身份验证失败，请重新从工作台进入。"
    assert "set-cookie" not in response.headers


@pytest.mark.anyio
async def test_exchange_maps_dingtalk_unavailable_to_safe_service_response() -> None:
    """钉钉超时或服务不可用时返回 503，不把外部异常文本暴露给教师。"""

    app.dependency_overrides[get_settings] = build_dingtalk_settings
    app.dependency_overrides[get_dingtalk_identity_verifier] = lambda: FakeVerifier(
        DingTalkUnavailableError("upstream timeout")
    )
    app.dependency_overrides[get_identity_session_store] = lambda: FakeSessionStore()

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as client:
            response = await client.post(
                "/api/v1/auth/dingtalk/exchange",
                json={"auth_code": "one-time-code"},
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 503
    assert response.json()["detail"] == "钉钉身份服务暂时不可用，请稍后重试。"
    assert "upstream timeout" not in response.text


@pytest.mark.anyio
async def test_exchange_returns_429_with_retry_after_when_globally_limited(caplog) -> None:
    """应用全局登录门拒绝时必须返回可重试的中文 429，且不验证一次性码。"""

    verifier = FakeVerifier(
        DingTalkIdentity(
            corporation_id="ding-test-corp",
            personnel_id="teacher-001",
            display_name="测试教师",
        )
    )
    database_session = FakeDatabaseSession()
    app.dependency_overrides[get_dingtalk_login_guard] = lambda: RejectingLoginGuard(
        DingTalkLoginRateLimitError()
    )
    app.dependency_overrides[get_dingtalk_identity_verifier] = lambda: verifier
    app.dependency_overrides[get_session] = lambda: cast(Session, database_session)
    app.dependency_overrides[get_settings] = build_dingtalk_settings
    try:
        with caplog.at_level(logging.INFO, logger="app.platform.identity.router"):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="https://test"
            ) as client:
                response = await client.post(
                    "/api/v1/auth/dingtalk/exchange",
                    json={"auth_code": "one-time-code"},
                )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 429
    assert response.headers["retry-after"] == "2"
    assert response.json()["detail"] == "当前登录人数较多，请稍后重新进入。"
    assert verifier.verify_calls == 0
    assert database_session.rolled_back is True
    assert "result=rate_limited" in caplog.records[-1].getMessage()


@pytest.mark.anyio
async def test_exchange_returns_maintenance_503_before_verifying_code(caplog) -> None:
    """维护门必须回滚并返回精确 503/Retry-After，且绝不验证一次性免登码。"""

    verifier = FakeVerifier(
        DingTalkIdentity(
            corporation_id="ding-test-corp",
            personnel_id="teacher-001",
            display_name="测试教师",
        )
    )
    database_session = FakeDatabaseSession()
    app.dependency_overrides[get_dingtalk_login_guard] = lambda: RejectingLoginGuard(
        DingTalkLoginMaintenanceError()
    )
    app.dependency_overrides[get_dingtalk_identity_verifier] = lambda: verifier
    app.dependency_overrides[get_session] = lambda: cast(Session, database_session)
    app.dependency_overrides[get_settings] = build_dingtalk_settings
    try:
        with caplog.at_level(logging.INFO, logger="app.platform.identity.router"):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="https://test"
            ) as client:
                response = await client.post(
                    "/api/v1/auth/dingtalk/exchange",
                    json={"auth_code": "one-time-code"},
                )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 503
    assert response.headers["retry-after"] == "30"
    assert response.json()["detail"] == "登录服务正在维护，请稍后重新从钉钉工作台进入。"
    assert verifier.verify_calls == 0
    assert database_session.rolled_back is True
    assert "result=maintenance" in caplog.records[-1].getMessage()


@pytest.mark.anyio
async def test_exchange_records_login_guard_unavailable_before_verifying_code(caplog) -> None:
    """登录门 Redis 故障必须以独立结果分类为 503，且不调用钉钉验证器。"""

    verifier = FakeVerifier(
        DingTalkIdentity(
            corporation_id="ding-test-corp",
            personnel_id="teacher-001",
            display_name="测试教师",
        )
    )
    database_session = FakeDatabaseSession()
    app.dependency_overrides[get_dingtalk_login_guard] = lambda: RejectingLoginGuard(
        DingTalkLoginGuardUnavailableError()
    )
    app.dependency_overrides[get_dingtalk_identity_verifier] = lambda: verifier
    app.dependency_overrides[get_session] = lambda: cast(Session, database_session)
    app.dependency_overrides[get_settings] = build_dingtalk_settings
    try:
        with caplog.at_level(logging.INFO, logger="app.platform.identity.router"):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="https://test"
            ) as client:
                response = await client.post(
                    "/api/v1/auth/dingtalk/exchange",
                    json={"auth_code": "one-time-code"},
                )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 503
    assert response.json()["detail"] == "登录服务暂时不可用，请稍后重新从钉钉工作台进入。"
    assert verifier.verify_calls == 0
    assert database_session.rolled_back is True
    assert "result=login_guard_unavailable" in caplog.records[-1].getMessage()


@pytest.mark.anyio
async def test_exchange_records_session_unavailable_after_verifying_code(
    caplog,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """会话 Redis 故障必须与登录门故障区分记录，并在验证后返回原有安全 503。"""

    verifier = FakeVerifier(
        DingTalkIdentity(
            corporation_id="ding-test-corp",
            personnel_id="teacher-001",
            display_name="测试教师",
        )
    )
    database_session = FakeDatabaseSession()
    mapped_user = User(
        id=7,
        external_subject="dingtalk:ding-test-corp:teacher-001",
        display_name="测试教师",
        is_active=True,
    )
    from app.platform.identity import router as identity_router

    monkeypatch.setattr(
        identity_router,
        "find_or_create_dingtalk_user",
        lambda *_args, **_kwargs: mapped_user,
    )
    monkeypatch.setattr(
        identity_router,
        "determine_access_status",
        lambda *_args, **_kwargs: IdentityAccessStatus(
            access_state="pending_configuration",
            can_manage_system=False,
        ),
    )
    app.dependency_overrides[get_dingtalk_login_guard] = PermittingLoginGuard
    app.dependency_overrides[get_dingtalk_identity_verifier] = lambda: verifier
    app.dependency_overrides[get_identity_session_store] = lambda: FakeSessionStore(
        create_error=RedisError("受控会话 Redis 故障")
    )
    app.dependency_overrides[get_session] = lambda: cast(Session, database_session)
    app.dependency_overrides[get_settings] = build_dingtalk_settings
    try:
        with caplog.at_level(logging.INFO, logger="app.platform.identity.router"):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="https://test"
            ) as client:
                response = await client.post(
                    "/api/v1/auth/dingtalk/exchange",
                    json={"auth_code": "one-time-code"},
                )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 503
    assert response.json()["detail"] == "登录会话暂时不可用，请稍后重试。"
    assert verifier.verify_calls == 1
    assert database_session.rolled_back is True
    assert "result=session_unavailable" in caplog.records[-1].getMessage()


@pytest.mark.anyio
async def test_logout_deletes_redis_session_and_clears_cookie() -> None:
    """退出接口删除不透明 Redis 会话，并以相同安全属性清除浏览器 Cookie。"""

    session_store = FakeSessionStore()
    app.dependency_overrides[get_settings] = build_dingtalk_settings
    app.dependency_overrides[get_identity_session_store] = lambda: session_store

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as client:
            client.cookies.set("campus_ai_session", "opaque-session-token")
            response = await client.post("/api/v1/auth/logout")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {"status": "logged_out"}
    assert session_store.deleted_token == "opaque-session-token"
    assert "HttpOnly" in response.headers["set-cookie"]
    assert "Secure" in response.headers["set-cookie"]


@pytest.mark.anyio
async def test_me_exposes_derived_capability_not_role_names(monkeypatch) -> None:
    """当前用户接口只返回派生访问状态，不泄露角色名或完整权限集合。"""

    from app import main as main_module

    expected_user = CurrentUser(
        id=7,
        external_subject="dingtalk:ding-test-corp:manager-001",
        display_name="测试管理员",
        security_version=1,
    )
    monkeypatch.setattr(
        main_module,
        "determine_access_status",
        lambda *_args, **_kwargs: IdentityAccessStatus(
            access_state="ready",
            can_manage_system=True,
        ),
    )
    app.dependency_overrides[get_current_user] = lambda: expected_user
    app.dependency_overrides[get_session] = lambda: cast(Session, FakeDatabaseSession())

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as client:
            response = await client.get("/api/v1/me")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {
        "id": 7,
        "external_subject": "dingtalk:ding-test-corp:manager-001",
        "display_name": "测试管理员",
        "access_state": "ready",
        "can_manage_system": True,
    }
    assert "roles" not in response.json()
