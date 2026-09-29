from __future__ import annotations

import json
from collections.abc import Callable
from types import SimpleNamespace
from typing import cast

import pytest
from sqlalchemy import Select
from sqlalchemy.orm import Session

from app.integrations.dingtalk.client import DingTalkIdentity
from app.platform.audit.models import AuditLog
from app.platform.identity.context import CurrentUser
from app.platform.identity.models import User
from app.platform.identity.service import find_or_create_dingtalk_user
from app.platform.identity.session import IdentitySessionStore


class FakeRedis:
    """以内存字典模拟会话存储所需的最小 Redis 行为。"""

    def __init__(self) -> None:
        """初始化空会话值和过期时间记录。"""

        self.values: dict[str, str] = {}
        self.expirations: dict[str, int] = {}

    def setex(self, name: str, time: int, value: str) -> bool:
        """保存文本会话值及其固定过期时间。"""

        self.values[name] = value
        self.expirations[name] = time
        return True

    def get(self, name: str) -> str | None:
        """读取会话文本，不存在时返回空值。"""

        return self.values.get(name)

    def delete(self, *names: str) -> int:
        """删除会话文本和过期时间，并返回删除数量。"""

        deleted_count = 0
        for name in names:
            existed = name in self.values
            self.values.pop(name, None)
            self.expirations.pop(name, None)
            deleted_count += int(existed)
        return deleted_count

    def ttl(self, key: str) -> int:
        """返回测试所记录的会话有效期。"""

        return self.expirations.get(key, -2)


class FakeMappingSession:
    """模拟钉钉首次映射所需的最小数据库会话，不连接真实 PostgreSQL。"""

    def __init__(
        self,
        existing_user: User | None = None,
        *,
        scalar_results: list[User | None] | None = None,
    ) -> None:
        """按需预置已存在用户并记录待写入对象。"""

        self.existing_user = existing_user
        self.scalar_results = list(scalar_results or [existing_user])
        self.claimed_user = (
            scalar_results[1] if scalar_results is not None and len(scalar_results) > 1 else None
        )
        self.scalar_statements: list[object] = []
        self.execute_calls: list[tuple[object, dict[str, object]]] = []
        self.added_objects: list[object] = []

    def scalar(self, statement: object) -> User | None:
        """返回外部身份键查询到的预置用户。"""

        self.scalar_statements.append(statement)
        return self.scalar_results.pop(0) if self.scalar_results else None

    def execute(self, statement: object, parameters: dict[str, object]) -> object:
        """模拟 revision20 legacy claim capability 返回既有用户主键。"""

        self.execute_calls.append((statement, parameters))
        value = None if self.claimed_user is None else self.claimed_user.id
        return SimpleNamespace(scalar_one_or_none=lambda: value)

    def get(self, entity: type[User], identity: int, *, populate_existing: bool) -> User | None:
        """模拟 capability 更新后按主键强制重载用户。"""

        assert entity is User
        assert populate_existing is True
        if self.claimed_user is None or self.claimed_user.id != identity:
            return None
        parameters = self.execute_calls[-1][1]
        self.claimed_user.provider_code = cast(str, parameters["provider_code"])
        self.claimed_user.provider_tenant_key = cast(str, parameters["provider_tenant_key"])
        self.claimed_user.external_subject = cast(str, parameters["external_subject"])
        return self.claimed_user

    def get_bind(self) -> object:
        """声明该内存会话不是 PostgreSQL，因此无需数据库身份锁。"""

        return SimpleNamespace(dialect=SimpleNamespace(name="sqlite"))

    def add(self, model: object) -> None:
        """记录服务写入的用户或审计对象。"""

        self.added_objects.append(model)

    def flush(self) -> None:
        """为首次映射用户模拟数据库主键生成。"""

        for model in self.added_objects:
            if isinstance(model, User) and model.id is None:
                model.id = 19


def test_session_has_opaque_token_and_eight_hour_ttl() -> None:
    """会话令牌必须是随机值，Redis 只按固定八小时有效期保存最小身份。"""

    redis = FakeRedis()
    store = IdentitySessionStore(redis=redis, ttl_seconds=28_800)
    current_user = CurrentUser(
        id=7,
        external_subject="dingtalk:corp:user",
        display_name="测试教师",
        security_version=1,
    )

    token = store.create(current_user)

    assert token != current_user.external_subject
    assert redis.ttl(f"identity:session:{token}") == 28_800
    assert store.read(token) is not None
    assert store.read(token).user_id == 7  # pyright: ignore[reportOptionalMemberAccess]


def test_session_carries_only_normalized_trusted_mobile_and_hides_it_from_repr() -> None:
    """新会话保存结构化可信电话，但对象调试文本不得泄漏完整号码。"""

    redis = FakeRedis()
    store = IdentitySessionStore(redis=redis, ttl_seconds=28_800)
    current_user = CurrentUser(
        id=7,
        external_subject="dingtalk:corp:user",
        display_name="测试教师",
        security_version=1,
        trusted_mobile="+8613800000000",
    )

    token = store.create(current_user)
    identity_session = store.read(token)

    assert identity_session is not None
    assert identity_session.trusted_mobile == "+8613800000000"
    assert "+8613800000000" not in repr(identity_session)
    assert "+8613800000000" not in repr(current_user)


def test_old_seven_field_session_remains_readable_without_mobile() -> None:
    """部署前创建的七字段会话在新增可选电话后仍可继续使用。"""

    redis = FakeRedis()
    store = IdentitySessionStore(redis=redis, ttl_seconds=28_800)
    token = store.create(
        CurrentUser(
            id=7,
            external_subject="dingtalk:corp:user",
            display_name="测试教师",
            security_version=1,
        )
    )
    redis_key = f"identity:session:{token}"
    legacy_payload = json.loads(redis.values[redis_key])
    legacy_payload.pop("trusted_mobile")
    redis.values[redis_key] = json.dumps(legacy_payload)

    identity_session = store.read(token)

    assert identity_session is not None
    assert identity_session.trusted_mobile is None


@pytest.mark.parametrize(
    "mutate_payload",
    (
        lambda payload: payload.update({"unknown_field": "forbidden"}),
        lambda payload: payload.update({"trusted_mobile": 13800000000}),
        lambda payload: payload.update({"trusted_mobile": "138 0000 0000"}),
    ),
)
def test_session_rejects_unknown_or_invalid_new_mobile_field(
    mutate_payload: Callable[[dict[str, object]], None],
) -> None:
    """八字段会话仍保持精确 Schema，未知字段、错类型或非规范号码均失败关闭。"""

    redis = FakeRedis()
    store = IdentitySessionStore(redis=redis, ttl_seconds=28_800)
    token = store.create(
        CurrentUser(
            id=7,
            external_subject="dingtalk:corp:user",
            display_name="测试教师",
            security_version=1,
        )
    )
    redis_key = f"identity:session:{token}"
    payload = json.loads(redis.values[redis_key])
    mutate_payload(payload)
    redis.values[redis_key] = json.dumps(payload)

    assert store.read(token) is None


def test_session_delete_removes_opaque_token() -> None:
    """退出操作删除 Redis 中的会话，不依赖浏览器保存任何身份字段。"""

    store = IdentitySessionStore(redis=FakeRedis(), ttl_seconds=28_800)
    token = store.create(
        CurrentUser(
            id=7,
            external_subject="dingtalk:corp:user",
            display_name="测试教师",
            security_version=1,
        )
    )

    store.delete(token)

    assert store.read(token) is None


def test_first_dingtalk_mapping_creates_user_and_audit_without_roles() -> None:
    """首次企业身份映射只创建本地用户和审计，不写入角色或责任范围。"""

    session = FakeMappingSession()
    identity = DingTalkIdentity(
        corporation_id="ding-test-corp",
        personnel_id="teacher-001",
        display_name="测试教师",
    )

    user = find_or_create_dingtalk_user(
        cast(Session, session),
        identity=identity,
        request_id="request-001",
    )

    assert user.provider_code == "dingtalk"
    assert user.provider_tenant_key == "ding-test-corp"
    assert user.external_subject == "teacher-001"
    assert user.display_name == "测试教师"
    audit_log = next(item for item in session.added_objects if isinstance(item, AuditLog))
    assert audit_log.event_type == "identity.mapping.created"
    assert audit_log.actor_user_id is None
    assert audit_log.target_id == "19"


def test_existing_dingtalk_mapping_only_refreshes_display_name() -> None:
    """同一企业人员再次登录时只刷新显示名，不新增映射审计或更改授权数据。"""

    existing_user = User(
        id=9,
        provider_code="dingtalk",
        provider_tenant_key="ding-test-corp",
        external_subject="teacher-001",
        display_name="旧名字",
        is_active=True,
    )
    session = FakeMappingSession(existing_user=existing_user)

    user = find_or_create_dingtalk_user(
        cast(Session, session),
        identity=DingTalkIdentity(
            corporation_id="ding-test-corp",
            personnel_id="teacher-001",
            display_name="新名字",
        ),
        request_id="request-002",
    )

    assert user is existing_user
    assert user.display_name == "新名字"
    assert session.added_objects == []


def test_dingtalk_login_claims_only_the_exact_legacy_compound_identity() -> None:
    """升级后的首次登录只认领完全匹配的旧复合键，并保留用户主键。"""

    legacy_user = User(
        id=27,
        provider_code="legacy",
        provider_tenant_key="legacy",
        external_subject="dingtalk:ding-test-corp:teacher-001",
        display_name="旧名字",
        is_active=True,
    )
    session = FakeMappingSession(scalar_results=[None, legacy_user])

    user = find_or_create_dingtalk_user(
        cast(Session, session),
        identity=DingTalkIdentity(
            corporation_id="ding-test-corp",
            personnel_id="teacher-001",
            display_name="新名字",
        ),
        request_id="request-legacy-claim",
    )

    assert user is legacy_user
    assert user.id == 27
    assert user.provider_code == "dingtalk"
    assert user.provider_tenant_key == "ding-test-corp"
    assert user.external_subject == "teacher-001"
    assert user.display_name == "新名字"
    assert session.added_objects == []
    assert len(session.scalar_statements) == 1
    namespace_query = str(session.scalar_statements[0])
    assert len(session.execute_calls) == 1
    claim_query = str(session.execute_calls[0][0])
    assert "users.provider_code" in namespace_query
    assert "users.provider_tenant_key" in namespace_query
    assert "users.external_subject" in namespace_query
    assert "app.claim_legacy_identity_user_id" in claim_query
    assert "FOR UPDATE" not in claim_query


class _LegacyClaimForbidden(RuntimeError):
    sqlstate = "42501"


class _NamespacedAccountSession(FakeMappingSession):
    """The F1 runtime may create Users but cannot invoke the legacy-claim capability."""

    def __init__(self, existing_user: User | None = None) -> None:
        super().__init__(existing_user)
        self.commits = 0

    def get_bind(self) -> object:
        return SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))

    def execute(self, statement: object, parameters: dict[str, object]) -> object:
        from sqlalchemy.exc import ProgrammingError

        self.execute_calls.append((statement, parameters))
        if "claim_legacy_identity_user_id" in str(statement):
            raise ProgrammingError(str(statement), parameters, _LegacyClaimForbidden("denied"))
        assert "pg_advisory_xact_lock" in str(statement)
        return SimpleNamespace()

    def commit(self) -> None:
        self.commits += 1


def test_original_test_account_creation_reproduces_forbidden_legacy_claim() -> None:
    from sqlalchemy.exc import ProgrammingError

    from app.platform.identity import service

    session = _NamespacedAccountSession()
    with pytest.raises(ProgrammingError) as failure:
        service.find_or_create_test_user(cast(Session, session), "f1-maintainer", "合成工程维护员")
    assert getattr(failure.value.orig, "sqlstate", None) == "42501"
    assert session.added_objects == [] and session.commits == 0


def test_namespaced_test_account_creates_only_exact_identity_without_legacy_claim() -> None:
    from app.platform.identity import service

    create = service.find_or_create_namespaced_test_user
    session = _NamespacedAccountSession()
    user = create(cast(Session, session), "f1-maintainer", "合成工程维护员")
    assert (user.provider_code, user.provider_tenant_key, user.external_subject) == (
        "local_test",
        "development",
        "f1-maintainer",
    )
    assert user.is_active is True and user.security_version == 1
    assert session.added_objects == [user] and type(user) is User
    assert session.commits == 1
    assert len(session.execute_calls) == 1
    assert "pg_advisory_xact_lock" in str(session.execute_calls[0][0])
    query = cast(Select[tuple[User]], session.scalar_statements[0])
    parameters = query.compile().params
    assert set(parameters.values()) == {"local_test", "development", "f1-maintainer"}


def test_namespaced_test_account_preserves_existing_disabled_identity_without_reactivation() -> (
    None
):
    from app.platform.identity import service

    create = service.find_or_create_namespaced_test_user
    existing = User(
        id=57,
        provider_code="local_test",
        provider_tenant_key="development",
        external_subject="f1-maintainer",
        display_name="原工程姓名",
        is_active=False,
        security_version=9,
    )
    session = _NamespacedAccountSession(existing)
    result = create(cast(Session, session), "f1-maintainer", "不得重写姓名")
    assert result is existing
    assert result.display_name == "原工程姓名" and result.is_active is False
    assert result.security_version == 9 and session.commits == 0
    assert session.added_objects == []


@pytest.mark.parametrize("subject,label", [("", "label"), (" ", "label"), ("subject", "")])
def test_namespaced_test_account_rejects_empty_input_before_identity_lock(subject, label) -> None:
    from app.platform.identity import service

    create = service.find_or_create_namespaced_test_user
    session = _NamespacedAccountSession()
    with pytest.raises(ValueError):
        create(cast(Session, session), subject, label)
    assert session.execute_calls == [] and session.scalar_statements == []
    assert session.added_objects == [] and session.commits == 0


def test_f1_identity_cli_only_initializes_fixed_engineering_accounts_without_legacy(monkeypatch):
    import runpy
    from contextlib import nullcontext
    from pathlib import Path

    from app.platform.identity import service

    cli = runpy.run_path(str(Path(__file__).parents[3] / "scripts/school_facts_f1_init.py"))
    initialize = cli["initialize_identity"]
    disposed = []
    initialized = []
    session = object()
    engine = SimpleNamespace(dispose=lambda: disposed.append(True))
    monkeypatch.setitem(
        initialize.__globals__,
        "runtime_factory",
        lambda _config: (None, engine, lambda: nullcontext(session)),
    )

    def create(current_session, subject, label):
        assert current_session is session
        initialized.append((subject, label))
        return SimpleNamespace(id=len(initialized), is_active=True)

    monkeypatch.setattr(service, "find_or_create_namespaced_test_user", create)
    monkeypatch.setattr(
        service,
        "find_or_create_test_user",
        lambda *_args: pytest.fail("F1 cannot enter legacy identity claim"),
    )
    result = initialize({"accounts": ["arbitrary-subject-cannot-expand-fixed-list"]})
    assert initialized == [
        ("f1-maintainer", "合成工程维护员"),
        ("f1-reader", "合成工程查询员"),
        ("f1-unprivileged", "合成工程无维护职责账号"),
    ]
    assert [account["ref"] for account in result["accounts"]] == [
        "maintainer",
        "reader",
        "unprivileged",
    ]
    assert disposed == [True]
