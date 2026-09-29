from __future__ import annotations

from types import SimpleNamespace
from typing import cast

import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy.orm import Session

from app.core.settings import Settings, get_settings
from app.db.connection import get_session
from app.main import app
from app.platform.identity.context import (
    CurrentUser,
    get_current_user,
    resolve_current_user_for_provider,
    resolve_dingtalk_current_user,
    resolve_test_current_user,
)
from app.platform.identity.models import User
from app.platform.identity.schemas import IdentityAccessStatus
from app.platform.identity.session import IdentitySession, IdentitySessionStore


class FakeSession:
    """以最小行为模拟本地测试身份服务所需的数据库会话。"""

    def __init__(self, existing_user: User | None = None) -> None:
        self.existing_user = existing_user
        self.added_user: User | None = None
        self.committed = False

    def scalar(self, _statement: object) -> User | None:
        """返回预设的身份主体查询结果。"""

        return self.existing_user

    def execute(self, _statement: object, _parameters: object) -> object:
        """本地替身没有可认领的 legacy 身份主体。"""

        return SimpleNamespace(scalar_one_or_none=lambda: None)

    def get_bind(self) -> object:
        """声明测试替身不是 PostgreSQL，不执行数据库 advisory lock。"""

        return SimpleNamespace(dialect=SimpleNamespace(name="sqlite"))

    def add(self, user: User) -> None:
        """记录待新增的本地用户。"""

        self.added_user = user

    def flush(self) -> None:
        """模拟数据库写入后生成用户主键。"""

        assert self.added_user is not None
        self.added_user.id = 42

    def commit(self) -> None:
        """记录本地用户映射已被写入事务。"""

        self.committed = True


class FakeIdentitySessionStore:
    """返回已经过会话层校验的可信钉钉身份，不访问 Redis。"""

    def __init__(self, identity_session: IdentitySession) -> None:
        self.identity_session = identity_session

    def read(self, token: str) -> IdentitySession:
        assert token == "opaque-session-token"
        return self.identity_session


def test_test_identity_creates_local_user_in_development() -> None:
    session = FakeSession()

    current_user = resolve_test_current_user(
        app_env="development",
        subject="test-user-001",
        display_name="测试教师",
        session=cast(Session, session),
    )

    assert current_user == CurrentUser(
        id=42,
        external_subject="test-user-001",
        display_name="测试教师",
        security_version=1,
    )
    assert session.added_user is not None
    assert session.added_user.is_active is True
    assert session.committed is True


def test_existing_test_identity_mapping_does_not_commit_the_callers_transaction() -> None:
    """已存在的本地测试映射只读取，不应意外提交调用方同一 Session 的其他工作。"""

    existing_user = User(
        id=43,
        provider_code="local_test",
        provider_tenant_key="development",
        external_subject="test-user-existing",
        display_name="既有测试用户",
        is_active=True,
        security_version=1,
    )
    session = FakeSession(existing_user=existing_user)

    current_user = resolve_test_current_user(
        app_env="development",
        subject="test-user-existing",
        display_name="请求显示名",
        session=cast(Session, session),
    )

    assert current_user.display_name == "既有测试用户"
    assert session.committed is False


def test_test_identity_rejects_production_environment() -> None:
    with pytest.raises(HTTPException) as error:
        resolve_test_current_user(
            app_env="production",
            subject="test-user-001",
            display_name="测试教师",
            session=cast(Session, FakeSession()),
        )

    assert error.value.status_code == 401
    assert error.value.detail == "当前环境不接受测试身份，请通过钉钉进入系统。"


def test_dingtalk_provider_rejects_test_headers_without_session() -> None:
    """钉钉模式没有有效会话时必须拒绝测试请求头，避免伪造教师身份。"""

    with pytest.raises(HTTPException) as error:
        resolve_current_user_for_provider(
            app_env="test",
            identity_provider="dingtalk",
            session_token=None,
            test_subject="forged-user",
            test_display_name="伪造教师",
            session=cast(Session, FakeSession()),
            identity_session_store=None,
        )

    assert error.value.status_code == 401
    assert error.value.detail == "请从钉钉工作台打开系统。"


def test_dingtalk_context_carries_trusted_mobile_from_verified_session() -> None:
    """当前用户只从服务端会话继承可信电话，数据库用户资料不参与猜测。"""

    user = User(
        id=7,
        external_subject="dingtalk:ding-test-corp:teacher-001",
        display_name="测试教师",
        security_version=1,
        is_active=True,
    )
    identity_session = IdentitySession(
        user_id=7,
        external_subject=user.external_subject,
        display_name=user.display_name,
        security_version=1,
        session_nonce_hash="not-used-by-this-unit-test",
        issued_at="2026-08-15T00:00:00+00:00",
        expires_at="2026-08-15T08:00:00+00:00",
        trusted_mobile="13800000000",
    )

    current_user = resolve_dingtalk_current_user(
        session_token="opaque-session-token",
        session=cast(Session, FakeSession(existing_user=user)),
        identity_session_store=cast(
            IdentitySessionStore, FakeIdentitySessionStore(identity_session)
        ),
    )

    assert current_user.trusted_mobile == "13800000000"
    assert "13800000000" not in repr(current_user)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_me_returns_current_user_context(monkeypatch) -> None:
    """当前用户接口返回最小身份和派生状态，不触发真实数据库连接。"""

    from app import main as main_module

    expected_user = CurrentUser(
        id=7,
        external_subject="test-user-007",
        display_name="测试管理员",
        security_version=1,
        trusted_mobile="13800000000",
    )
    monkeypatch.setattr(
        main_module,
        "determine_access_status",
        lambda *_args, **_kwargs: IdentityAccessStatus(
            access_state="pending_configuration",
            can_manage_system=False,
        ),
    )
    app.dependency_overrides[get_current_user] = lambda: expected_user
    app.dependency_overrides[get_session] = lambda: cast(Session, FakeSession())

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/api/v1/me")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {
        "id": 7,
        "external_subject": "test-user-007",
        "display_name": "测试管理员",
        "access_state": "pending_configuration",
        "can_manage_system": False,
    }
    assert "trusted_mobile" not in response.json()
    assert "13800000000" not in response.text


@pytest.mark.anyio
async def test_me_rejects_test_headers_in_production() -> None:
    """生产钉钉模式必须忽略伪造测试请求头，并要求有效的工作台会话。"""

    production_settings = Settings.model_validate(
        {
            "app_env": "production",
            "postgres_host": "127.0.0.1",
            "postgres_port": 5432,
            "postgres_db": "unused",
            "postgres_user": "unused",
            "postgres_password": "unused",
            "postgres_expected_anchor_id": "10000000-0000-4000-8000-000000000001",
            "postgres_expected_school_id": "10000000-0000-4000-8000-000000000002",
            "postgres_expected_system_identifier": "1234567890123456789",
            "rls_context_signing_key": "test-runtime-context-signing-key-32-bytes",
            "identity_provider": "dingtalk",
            "dingtalk_corp_id": "ding-production-corp",
            "dingtalk_app_key": "production-app-key",
            "dingtalk_app_secret": "production-app-secret",
        }
    )
    app.dependency_overrides[get_settings] = lambda: production_settings
    app.dependency_overrides[get_session] = lambda: None

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get(
                "/api/v1/me",
                headers={
                    "X-Test-Subject": "test-user-001",
                    "X-Test-Display-Name": "Test Teacher",
                },
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 401
    assert response.json()["detail"] == "请从钉钉工作台打开系统。"
