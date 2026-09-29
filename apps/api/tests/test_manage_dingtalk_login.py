from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.routing import APIRoute

from app.core.settings import Settings
from app.integrations.dingtalk.token_provider import DingTalkAppTokenProvider
from app.main import app
from app.platform.identity import manage_dingtalk_login as command
from app.platform.identity.login_maintenance import (
    DingTalkLoginMaintenanceStore,
    LoginMaintenanceConflictError,
    LoginMaintenanceStateError,
)


class FakeMaintenanceRedis:
    """以受控内存状态模拟维护租约所需的 Redis 原子操作。"""

    def __init__(self) -> None:
        """初始化空键值、TTL 和 Lua 调用记录，不连接真实 Redis。"""

        self.values: dict[str, str] = {}
        self.expirations: dict[str, int] = {}
        self.eval_calls: list[tuple[str, int, tuple[object, ...]]] = []

    def set(
        self,
        key: str,
        value: str,
        *,
        nx: bool = False,
        xx: bool = False,
        ex: int,
    ) -> bool:
        """模拟带 NX/XX 和正数有效期的 Redis SET。"""

        assert ex > 0
        assert not (nx and xx)
        if nx and key in self.values:
            return False
        if xx and key not in self.values:
            return False
        self.values[key] = value
        self.expirations[key] = ex
        return True

    def get(self, key: str) -> str | None:
        """读取文本维护状态。"""

        return self.values.get(key)

    def ttl(self, key: str) -> int:
        """返回受控 TTL；缺失键使用 Redis 的 -2 语义。"""

        return self.expirations.get(key, -2)

    def eval(self, script: str, numkeys: int, *keys_and_args: object) -> object:
        """按操作标识比较后模拟续期或删除，拒绝无条件覆盖。"""

        self.eval_calls.append((script, numkeys, keys_and_args))
        assert numkeys == 1
        key = keys_and_args[0]
        assert isinstance(key, str)
        if "redis.call('ttl'" in script:
            return [self.values.get(key), self.expirations.get(key, -2)]

        operation_id = keys_and_args[1]
        assert isinstance(operation_id, str)
        raw_value = self.values.get(key)
        if raw_value is None or json.loads(raw_value).get("operation_id") != operation_id:
            return 0
        if "redis.call('set'" in script:
            payload, ttl_seconds = keys_and_args[2:]
            assert isinstance(payload, str)
            assert isinstance(ttl_seconds, int)
            new_payload = json.loads(payload)
            current_payload = json.loads(raw_value)
            if "new_status['paused_at']" in script:
                new_payload["paused_at"] = current_payload["paused_at"]
            self.values[key] = json.dumps(new_payload, ensure_ascii=False, separators=(",", ":"))
            self.expirations[key] = ttl_seconds
            return 1 if "return 1" in script else "OK"
        if "redis.call('del'" in script:
            self.values.pop(key)
            self.expirations.pop(key, None)
            return 1
        raise AssertionError("未识别的维护 Lua 操作")


class FakeTokenProvider:
    """记录受控 Token 缓存失效调用，不访问外部服务。"""

    def __init__(self) -> None:
        """初始化尚未失效的测试状态。"""

        self.invalidated = False

    def invalidate(self) -> None:
        """记录同锁缓存失效请求。"""

        self.invalidated = True


class FixedClock:
    """提供可推进的 UTC 时钟，验证租约时间字段不依赖真实系统时间。"""

    def __init__(self, now: datetime) -> None:
        """以指定的 UTC 时刻初始化测试时钟。"""

        self.now_value = now

    def now(self) -> datetime:
        """返回当前受控 UTC 时刻。"""

        return self.now_value

    def advance(self, seconds: int) -> None:
        """推进时钟以模拟同一维护窗口内的续期。"""

        self.now_value += timedelta(seconds=seconds)


class SnapshotSwitchingRedis(FakeMaintenanceRedis):
    """模拟 GET 后恰有维护租约续期，暴露非原子状态快照拼接。"""

    def __init__(self, *, replacement_value: str, replacement_ttl_seconds: int) -> None:
        """保存下一次维护租约状态，等待非原子 GET 读取旧值后切换。"""

        super().__init__()
        self._replacement_value = replacement_value
        self._replacement_ttl_seconds = replacement_ttl_seconds
        self._switched = False

    def get(self, key: str) -> str | None:
        """先返回旧值，再模拟另一请求完成续期写入。"""

        raw_value = super().get(key)
        if not self._switched:
            self.values[key] = self._replacement_value
            self.expirations[key] = self._replacement_ttl_seconds
            self._switched = True
        return raw_value

    def eval(self, script: str, numkeys: int, *keys_and_args: object) -> object:
        """原子快照在单次脚本开始前看到续期后的完整状态。"""

        if "redis.call('ttl'" in script and not self._switched:
            key = keys_and_args[0]
            assert isinstance(key, str)
            self.values[key] = self._replacement_value
            self.expirations[key] = self._replacement_ttl_seconds
            self._switched = True
        return super().eval(script, numkeys, *keys_and_args)


def build_store(
    redis: FakeMaintenanceRedis,
    *,
    now: Callable[[], datetime] | None = None,
) -> DingTalkLoginMaintenanceStore:
    """使用固定测试环境构造登录维护租约存储。"""

    return DingTalkLoginMaintenanceStore(redis=redis, app_env="test", now=now)


def build_dingtalk_settings() -> Settings:
    """构造不含真实凭证的最小钉钉测试环境配置。"""

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


def test_pause_creates_thirty_minute_lease_without_secrets() -> None:
    """首次暂停必须以 1800 秒租约保存仅含维护元数据的 JSON。"""

    redis = FakeMaintenanceRedis()
    store = build_store(redis)

    operation_id = store.pause(reason="轮换测试凭证")
    payload = json.loads(redis.values[store.key])

    assert redis.expirations[store.key] == 1800
    assert payload["operation_id"] == operation_id
    assert payload["reason"] == "轮换测试凭证"
    assert set(payload) == {"paused_at", "expires_at", "reason", "operation_id"}
    assert "secret" not in redis.values[store.key].lower()
    assert "token" not in redis.values[store.key].lower()


def test_wrong_operation_cannot_renew_or_resume() -> None:
    """不匹配的操作标识既不能续期，也不能解除当前维护租约。"""

    redis = FakeMaintenanceRedis()
    store = build_store(redis)
    operation_id = store.pause(reason="轮换测试凭证")
    wrong_operation_id = "B" * 32

    with pytest.raises(LoginMaintenanceConflictError):
        store.pause(reason="错误续期", operation_id=wrong_operation_id)

    assert store.resume(wrong_operation_id) is False
    assert store.status() is not None
    assert store.resume(operation_id) is True
    assert store.status() is None


def test_successful_renewal_returns_one_for_real_redis_set_ok_and_keeps_start_time() -> None:
    """续期脚本必须把 Redis SET 的 OK 显式映射为 1，并保留维护开始时间。"""

    redis = FakeMaintenanceRedis()
    clock = FixedClock(datetime(2026, 7, 29, tzinfo=UTC))
    store = build_store(redis, now=clock.now)
    operation_id = store.pause(reason="首次维护")
    initial_paused_at = json.loads(redis.values[store.key])["paused_at"]
    clock.advance(30)

    assert store.pause(reason="继续维护", operation_id=operation_id) == operation_id

    payload = json.loads(redis.values[store.key])
    assert redis.expirations[store.key] == 1800
    assert payload["paused_at"] == initial_paused_at
    assert payload["reason"] == "继续维护"
    assert payload["expires_at"] == (clock.now() + timedelta(seconds=1800)).isoformat()


def test_status_uses_one_atomic_snapshot_instead_of_splicing_get_and_ttl() -> None:
    """状态读取必须从同一 Lua 快照获得负载和 TTL，不能拼接并发续期前后的值。"""

    old_payload = json.dumps(
        {
            "paused_at": "2026-07-29T00:00:00+00:00",
            "expires_at": "2026-07-29T00:30:00+00:00",
            "reason": "旧维护",
            "operation_id": "A" * 32,
        }
    )
    replacement_payload = json.dumps(
        {
            "paused_at": "2026-07-29T00:00:00+00:00",
            "expires_at": "2026-07-29T00:20:00+00:00",
            "reason": "续期后维护",
            "operation_id": "A" * 32,
        }
    )
    redis = SnapshotSwitchingRedis(
        replacement_value=replacement_payload,
        replacement_ttl_seconds=1200,
    )
    store = build_store(
        redis,
        now=lambda: datetime(2026, 7, 29, tzinfo=UTC),
    )
    redis.values[store.key] = old_payload
    redis.expirations[store.key] = 1800

    status = store.status()

    assert status is not None
    assert status.reason == "续期后维护"
    assert status.ttl_seconds == 1200


@pytest.mark.parametrize(
    "paused_at, expires_at, ttl_seconds",
    [
        ("2026-07-29T08:00:00+08:00", "2026-07-29T08:30:00+08:00", 1800),
        ("2026-07-29T00:00:00+00:00", "2026-07-28T23:59:59+00:00", 1800),
        ("2026-07-29T00:00:00+00:00", "2026-07-29T00:01:00+00:00", 1800),
    ],
)
def test_status_rejects_non_utc_inconsistent_or_expired_timestamp_payloads(
    paused_at: str,
    expires_at: str,
    ttl_seconds: int,
) -> None:
    """维护快照的时间必须是 UTC、递增且与当前时钟和 Redis TTL 一致。"""

    redis = FakeMaintenanceRedis()
    store = build_store(
        redis,
        now=lambda: datetime(2026, 7, 29, tzinfo=UTC),
    )
    redis.values[store.key] = json.dumps(
        {
            "paused_at": paused_at,
            "expires_at": expires_at,
            "reason": "测试维护",
            "operation_id": "A" * 32,
        }
    )
    redis.expirations[store.key] = ttl_seconds

    with pytest.raises(LoginMaintenanceStateError):
        store.status()


@pytest.mark.parametrize(
    "raw_value, ttl_seconds",
    [("{", 1800), ("{}", 1800), ('{"paused_at": "x"}', 0)],
)
def test_corrupt_or_expired_state_is_not_reported_as_unpaused(
    raw_value: str,
    ttl_seconds: int,
) -> None:
    """损坏 JSON 或非正 TTL 必须报错，绝不能回退为未暂停状态。"""

    redis = FakeMaintenanceRedis()
    store = build_store(redis)
    redis.values[store.key] = raw_value
    redis.expirations[store.key] = ttl_seconds

    with pytest.raises(LoginMaintenanceStateError):
        store.status()


@pytest.mark.parametrize(
    ("field", "invalid_value"),
    [
        ("operation_id", "A" * 31 + "!"),
        ("reason", ""),
        ("reason", " \t "),
        ("reason", "A" * 201),
        ("reason", "维护\r原因"),
        ("reason", "维护\n原因"),
        ("reason", "\r维护原因"),
        ("reason", "维护原因\n"),
    ],
)
def test_status_rejects_invalid_operation_id_or_reason_metadata(
    field: str,
    invalid_value: str,
) -> None:
    """维护快照的操作标识和原因必须满足与受控命令相同的语义约束。"""

    redis = FakeMaintenanceRedis()
    frozen_now = datetime(2026, 7, 29, tzinfo=UTC)
    store = build_store(redis, now=lambda: frozen_now)
    payload = {
        "paused_at": frozen_now.isoformat(),
        "expires_at": (frozen_now + timedelta(seconds=1800)).isoformat(),
        "reason": "测试维护",
        "operation_id": "A" * 32,
    }
    payload[field] = invalid_value
    redis.values[store.key] = json.dumps(payload, ensure_ascii=False)
    redis.expirations[store.key] = 1800

    with pytest.raises(LoginMaintenanceStateError):
        store.status()


@pytest.mark.parametrize(
    ("reason", "operation_id"),
    [
        (" \t ", None),
        ("A" * 201, None),
        ("维护\r原因", None),
        ("维护\n原因", None),
        ("\r维护原因", None),
        ("维护原因\n", None),
        ("有效原因", "A" * 31 + "!"),
    ],
)
def test_store_rejects_invalid_internal_maintenance_metadata_before_redis(
    reason: str,
    operation_id: str | None,
) -> None:
    """Store 写入边界必须在任何 Redis 副作用前拒绝无效内部元数据。"""

    redis = FakeMaintenanceRedis()
    store = build_store(redis)

    with pytest.raises(ValueError):
        store.pause(reason=reason, operation_id=operation_id)

    assert redis.values == {}
    assert redis.eval_calls == []


def test_store_normalizes_reason_before_persisting() -> None:
    """维护原因按统一规则去除首尾空白后再进入 Redis。"""

    redis = FakeMaintenanceRedis()
    store = build_store(redis)

    store.pause(reason="  轮换测试凭证\t")

    payload = json.loads(redis.values[store.key])
    assert payload["reason"] == "轮换测试凭证"


def test_invalidate_token_does_not_delete_identity_sessions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Token 失效命令只调用应用缓存提供者，不得触碰教师身份会话。"""

    redis = FakeMaintenanceRedis()
    redis.values["identity:session:teacher-session"] = "session"
    fake_provider = FakeTokenProvider()
    monkeypatch.setattr(command, "build_token_provider", lambda *_args: fake_provider)
    monkeypatch.setattr(command, "get_redis_client", lambda: redis)
    monkeypatch.setattr(command, "get_settings", build_dingtalk_settings)

    result = command.main(
        [
            "invalidate-token",
            "--confirm-environment",
            "test",
            "--reason",
            "清除异常缓存",
        ]
    )

    assert result == 0
    assert fake_provider.invalidated is True
    assert redis.values["identity:session:teacher-session"] == "session"


def test_command_returns_nonzero_for_corrupt_maintenance_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """状态命令遇到损坏维护状态时必须返回非零退出码。"""

    redis = FakeMaintenanceRedis()
    redis.values["identity:dingtalk:login:maintenance:test"] = "{"
    redis.expirations["identity:dingtalk:login:maintenance:test"] = 1800
    monkeypatch.setattr(command, "get_redis_client", lambda: redis)
    monkeypatch.setattr(command, "get_settings", build_dingtalk_settings)

    assert command.main(["status", "--confirm-environment", "test"]) == 1


@pytest.mark.parametrize(
    ("field", "invalid_value"),
    [
        ("operation_id", "A" * 31 + "!"),
        ("reason", "污染标记\r\n伪造输出"),
        ("reason", "\r污染标记"),
        ("reason", "污染标记\n"),
    ],
)
def test_status_command_fails_closed_without_outputting_corrupt_metadata(
    field: str,
    invalid_value: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """损坏维护元数据不得被 CLI 回显、误报为未暂停或表现为可安全解除。"""

    redis = FakeMaintenanceRedis()
    frozen_now = datetime(2026, 7, 29, tzinfo=UTC)
    payload = {
        "paused_at": frozen_now.isoformat(),
        "expires_at": (frozen_now + timedelta(seconds=1800)).isoformat(),
        "reason": "测试维护",
        "operation_id": "A" * 32,
    }
    payload[field] = invalid_value
    redis.values["identity:dingtalk:login:maintenance:test"] = json.dumps(
        payload,
        ensure_ascii=False,
    )
    redis.expirations["identity:dingtalk:login:maintenance:test"] = 1800
    monkeypatch.setattr(command, "get_redis_client", lambda: redis)
    monkeypatch.setattr(command, "get_settings", build_dingtalk_settings)

    assert command.main(["status", "--confirm-environment", "test"]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert invalid_value not in captured.err
    assert "未暂停" not in captured.out


def test_status_command_does_not_output_maintenance_operation_id(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """状态命令仅输出运维可见状态，绝不回显可用于解除维护的操作标识。"""

    redis = FakeMaintenanceRedis()
    store = build_store(redis)
    operation_id = store.pause(reason="测试维护")
    monkeypatch.setattr(command, "get_redis_client", lambda: redis)
    monkeypatch.setattr(command, "get_settings", build_dingtalk_settings)

    assert command.main(["status", "--confirm-environment", "test"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert set(payload) == {"paused", "reason", "paused_at", "ttl_seconds"}
    assert payload["paused"] is True
    assert payload["reason"] == "测试维护"
    assert operation_id not in json.dumps(payload, ensure_ascii=False)


@pytest.mark.parametrize(
    "arguments",
    [
        ["pause", "--operation-id", "A" * 31 + "!"],
        ["resume", "--operation-id", "A" * 31 + "\n"],
    ],
)
def test_invalid_external_operation_id_is_rejected_before_redis_or_logging(
    arguments: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """暂停续期和解除命令必须在 Redis 副作用或日志前拒绝无效操作标识。"""

    redis = FakeMaintenanceRedis()
    monkeypatch.setattr(command, "get_redis_client", lambda: redis)
    monkeypatch.setattr(command, "get_settings", build_dingtalk_settings)
    monkeypatch.setattr(
        command,
        "_log_result",
        lambda *_args, **_kwargs: pytest.fail("无效操作标识不得记录日志"),
    )
    command_arguments = [
        *arguments,
        "--confirm-environment",
        "test",
        "--reason",
        "测试维护",
    ]

    with pytest.raises(SystemExit) as error:
        command.main(command_arguments)

    assert error.value.code == 2
    assert redis.values == {}
    assert redis.eval_calls == []


@pytest.mark.parametrize("reason", ["\r污染标记", "污染标记\n", "\n污染标记\r"])
def test_command_rejects_raw_reason_newlines_before_redis_or_logging(
    reason: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """CLI 必须在 strip 前拒绝任意 CR/LF，且不得回显原始污染文本。"""

    monkeypatch.setattr(
        command,
        "get_redis_client",
        lambda: pytest.fail("无效原因不得连接 Redis"),
    )
    monkeypatch.setattr(command, "get_settings", build_dingtalk_settings)
    monkeypatch.setattr(
        command,
        "_log_result",
        lambda *_args, **_kwargs: pytest.fail("无效原因不得记录日志"),
    )

    with pytest.raises(SystemExit) as error:
        command.main(
            [
                "pause",
                "--confirm-environment",
                "test",
                "--reason",
                reason,
            ]
        )

    assert error.value.code == 2
    captured = capsys.readouterr()
    assert reason not in captured.out
    assert reason not in captured.err


def test_command_reuses_shared_maintenance_reason_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CLI 必须调用维护状态模块的同一原因规范化函数，避免双份规则漂移。"""

    validated_values: list[object] = []

    def validate_reason(value: object) -> str:
        validated_values.append(value)
        return "统一规范后的原因"

    redis = FakeMaintenanceRedis()
    fake_provider = FakeTokenProvider()
    monkeypatch.setattr(command, "normalize_maintenance_reason", validate_reason)
    monkeypatch.setattr(command, "build_token_provider", lambda *_args: fake_provider)
    monkeypatch.setattr(command, "get_redis_client", lambda: redis)
    monkeypatch.setattr(command, "get_settings", build_dingtalk_settings)

    assert (
        command.main(
            [
                "invalidate-token",
                "--confirm-environment",
                "test",
                "--reason",
                " 原始输入 ",
            ]
        )
        == 0
    )
    assert validated_values == [" 原始输入 "]


def _iter_app_api_routes(routes: Sequence[object]) -> list[APIRoute]:
    """展开当前 FastAPI 版本的惰性 include_router，返回实际装配的 API 路由。"""

    api_routes: list[APIRoute] = []
    for route in routes:
        if isinstance(route, APIRoute):
            api_routes.append(route)
            continue
        original_router = getattr(route, "original_router", None)
        nested_routes = getattr(original_router, "routes", None)
        if isinstance(nested_routes, list):
            api_routes.extend(_iter_app_api_routes(nested_routes))
    return api_routes


def _iter_route_dependency_calls(route: APIRoute) -> set[object]:
    """收集实际 FastAPI 路由端点和递归依赖，检查服务器命令隔离边界。"""

    calls: set[object] = {route.endpoint}
    dependencies = list(route.dependant.dependencies)
    while dependencies:
        dependency = dependencies.pop()
        if dependency.call is not None:
            calls.add(dependency.call)
        dependencies.extend(dependency.dependencies)
    return calls


def test_application_http_contract_exposes_only_approved_business_endpoints() -> None:
    """全应用业务 API 的路径、方法、端点模块和函数必须逐项经过安全审查。"""

    api_routes = _iter_app_api_routes(app.routes)
    assert {
        (
            route.path,
            frozenset(route.methods or set()),
            route.endpoint.__module__,
            route.endpoint.__name__,
        )
        for route in api_routes
    } == {
        (
            "/api/v1/admin/users/{user_id}/roles",
            frozenset({"POST"}),
            "app.platform.authorization.router",
            "assign_user_role",
        ),
        (
            "/api/v1/admin/users/{user_id}/roles/{role_code}",
            frozenset({"DELETE"}),
            "app.platform.authorization.router",
            "revoke_user_role",
        ),
        (
            "/api/v1/admin/users/{user_id}/scopes",
            frozenset({"POST"}),
            "app.platform.authorization.router",
            "assign_responsibility_scope",
        ),
        (
            "/api/v1/admin/roles/{role_code}/permissions",
            frozenset({"POST"}),
            "app.platform.authorization.router",
            "grant_role_permission",
        ),
        (
            "/api/v1/auth/contexts",
            frozenset({"GET"}),
            "app.platform.identity.router",
            "list_execution_contexts",
        ),
        (
            "/api/v1/auth/school-reactivation-contexts",
            frozenset({"GET"}),
            "app.platform.identity.router",
            "list_school_reactivation_contexts",
        ),
        (
            "/api/v1/auth/dingtalk/exchange",
            frozenset({"POST"}),
            "app.platform.identity.router",
            "exchange_dingtalk_auth_code",
        ),
        (
            "/api/v1/auth/logout",
            frozenset({"POST"}),
            "app.platform.identity.router",
            "logout",
        ),
        (
            "/api/v1/health",
            frozenset({"GET"}),
            "app.main",
            "read_health",
        ),
        (
            "/api/v1/me",
            frozenset({"GET"}),
            "app.main",
            "read_current_user",
        ),
    }


def test_server_maintenance_commands_are_not_http_endpoints_or_dependencies() -> None:
    """维护命令模块及 Store 的写操作不得成为任何 FastAPI 端点或依赖。"""

    forbidden_calls = {
        DingTalkAppTokenProvider.invalidate,
        DingTalkLoginMaintenanceStore.pause,
        DingTalkLoginMaintenanceStore.resume,
        DingTalkLoginMaintenanceStore.status,
    }
    for route in _iter_app_api_routes(app.routes):
        route_calls = _iter_route_dependency_calls(route)
        assert forbidden_calls.isdisjoint(route_calls)
        assert all(getattr(call, "__module__", None) != command.__name__ for call in route_calls)
