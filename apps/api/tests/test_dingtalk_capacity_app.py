from __future__ import annotations

import importlib.util
import json
import logging
import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import cast

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from redis.exceptions import LockNotOwnedError

from app.core.settings import Settings
from app.integrations.dingtalk.client import (
    DINGTALK_USER_DETAIL_URL,
    DINGTALK_USER_INFO_URL,
    DingTalkIdentityVerifier,
)
from app.integrations.dingtalk.token_provider import (
    DingTalkAppTokenProvider,
    TokenRedisClient,
)


@pytest.fixture
def dingtalk_settings() -> Settings:
    """提供只含公开合成哨兵的隔离钉钉配置。"""

    return Settings(
        app_env="test",
        postgres_host="postgres",
        postgres_port=5432,
        postgres_db="campus_ai_capacity_test",
        postgres_user="campus_ai_capacity_test",
        postgres_password="not-a-secret",
        rls_context_signing_key=SecretStr("test-runtime-context-signing-key-32-bytes"),
        redis_host="redis",
        redis_port=6379,
        redis_password="not-a-secret",
        identity_provider="dingtalk",
        dingtalk_corp_id="capacity-corp.invalid",
        dingtalk_app_key="capacity-app.invalid",
        dingtalk_app_secret="capacity-secret-not-real",
        identity_session_cookie_name="campus_ai_capacity_session",
    )


@pytest.fixture
def capacity_module() -> Iterator[ModuleType]:
    """从测试负载目录加载组合入口，避免将其安装进生产应用包。"""

    from app.main import app as production_app

    original_routes = list(production_app.router.routes)
    original_overrides = production_app.dependency_overrides.copy()
    original_state = dict(production_app.state._state)
    original_openapi_schema = production_app.openapi_schema
    original_handlers = {
        logger_name: list(logging.getLogger(logger_name).handlers)
        for logger_name in (
            "app.platform.identity.router",
            "app.integrations.dingtalk.token_provider",
            "app.platform.identity.login_guard",
        )
    }
    module_path = (
        Path(__file__).resolve().parents[3] / "tests" / "load" / "dingtalk_capacity_app.py"
    )
    specification = importlib.util.spec_from_file_location("dingtalk_capacity_app", module_path)
    assert specification is not None
    assert specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    try:
        yield module
    finally:
        production_app.router.routes[:] = original_routes
        production_app.dependency_overrides.clear()
        production_app.dependency_overrides.update(original_overrides)
        production_app.state._state.clear()
        production_app.state._state.update(original_state)
        production_app.openapi_schema = original_openapi_schema
        for logger_name, handlers in original_handlers.items():
            logger = logging.getLogger(logger_name)
            logger.handlers[:] = handlers
            assert logger.handlers == handlers


class FakeLockLocal:
    """保存与 redis-py 锁公开接口一致的当前线程令牌。"""

    def __init__(self) -> None:
        self.token: str | None = None


class FakeCapacityLock:
    """实现应用 Token 单刷新者所需的最小非阻塞锁。"""

    def __init__(self, redis: FakeCapacityRedis) -> None:
        self._redis = redis
        self.local = FakeLockLocal()
        self._owned = False

    def acquire(self, *, blocking: bool = False) -> bool:
        assert blocking is False
        with self._redis.state_lock:
            if self._redis.lock_token is not None:
                self._redis.lock_contention_count += 1
                return False
            self._redis.lock_counter += 1
            self.local.token = f"lock-{self._redis.lock_counter}"
            self._redis.lock_token = self.local.token
            self._owned = True
            return True

    def release(self) -> None:
        with self._redis.state_lock:
            if not self._owned or self.local.token != self._redis.lock_token:
                raise LockNotOwnedError("lock is no longer owned")
            self._redis.lock_token = None
            self._owned = False
            self.local.token = None


class FakeCapacityRedis:
    """提供合成 transport 与真实 Token Provider 共用的线程安全 Redis 最小替身。"""

    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.set_calls: list[tuple[str, str, bool, int]] = []
        self.deleted_keys: list[str] = []
        self.eval_calls: list[tuple[str, int, tuple[object, ...]]] = []
        self.lock_token: str | None = None
        self.lock_counter = 0
        self.lock_contention_count = 0
        self.state_lock = threading.Lock()

    def set(self, key: str, value: str, *, nx: bool = False, ex: int) -> bool:
        with self.state_lock:
            self.set_calls.append((key, value, nx, ex))
            if nx and key in self.values:
                return False
            self.values[key] = value
            return True

    def get(self, key: str) -> str | None:
        with self.state_lock:
            return self.values.get(key)

    def delete(self, key: str, *keys: str) -> int:
        with self.state_lock:
            deleted_keys = (key, *keys)
            self.deleted_keys.extend(deleted_keys)
            return sum(self.values.pop(item, None) is not None for item in deleted_keys)

    def scan(self, *_args: object, **_kwargs: object) -> None:
        """拒绝容量 reset 使用 Redis 迭代扫描。"""

        raise AssertionError("容量 reset 禁止使用 SCAN")

    def scan_iter(self, *_args: object, **_kwargs: object) -> None:
        """拒绝容量 reset 使用 Redis 模式扫描迭代器。"""

        raise AssertionError("容量 reset 禁止使用 scan_iter")

    def keys(self, *_args: object, **_kwargs: object) -> None:
        """拒绝容量 reset 使用 Redis 通配符键查询。"""

        raise AssertionError("容量 reset 禁止使用 KEYS")

    def flushdb(self, *_args: object, **_kwargs: object) -> None:
        """拒绝容量 reset 清空隔离 Redis 数据库。"""

        raise AssertionError("容量 reset 禁止使用 FLUSHDB")

    def lock(self, key: str, *, timeout: int, blocking: bool) -> FakeCapacityLock:
        assert key
        assert timeout == 10
        assert blocking is False
        return FakeCapacityLock(self)

    def eval(self, script: str, numkeys: int, *keys_and_args: object) -> int:
        assert numkeys == 2
        self.eval_calls.append((script, numkeys, keys_and_args))
        lock_key, cache_key, expected_token = keys_and_args[:3]
        assert isinstance(lock_key, str)
        assert isinstance(cache_key, str)
        with self.state_lock:
            if self.lock_token != expected_token:
                return 0
            if "redis.call('set'" in script:
                payload, _ttl = keys_and_args[3:]
                assert isinstance(payload, str)
                self.values[cache_key] = payload
            elif "redis.call('del'" in script:
                self.values.pop(cache_key, None)
            else:
                raise AssertionError("未识别的 Token Lua 操作")
        return 1


def test_capacity_environment_rejects_public_or_wrong_database(
    capacity_module: ModuleType,
    dingtalk_settings: Settings,
) -> None:
    """容量入口必须拒绝非专用数据库，即使显式启用了合成开关。"""

    with pytest.raises(RuntimeError, match="隔离容量环境校验失败"):
        capacity_module.validate_capacity_environment(
            dingtalk_settings,
            {
                "CAMPUS_AI_SYNTHETIC_CAPACITY": "true",
                "POSTGRES_DB": "campus_ai_dingtalk_test",
            },
        )


@pytest.mark.anyio
async def test_synthetic_transport_consumes_each_code_once(
    capacity_module: ModuleType,
) -> None:
    """同一合成 code 必须由隔离 Redis 原子消费一次。"""

    redis = FakeCapacityRedis()
    transport = capacity_module.SyntheticDingTalkTransport(
        redis=redis,
        metrics=capacity_module.CapacityMetrics(),
        identity_delay_seconds=0,
    )
    request = httpx.Request(
        "POST",
        DINGTALK_USER_INFO_URL,
        json={"code": "CAPACITY-v1-rate100-0001-0123456789abcdef"},
    )

    first = await transport.handle_async_request(request)
    second = await transport.handle_async_request(request)

    assert first.status_code == 200
    assert second.status_code == 400
    assert second.json() == {"errcode": 40078}
    assert len(redis.set_calls) == 2
    assert all(call[2:] == (True, 300) for call in redis.set_calls)


@pytest.mark.anyio
async def test_synthetic_transport_only_accepts_fixed_dingtalk_urls(
    capacity_module: ModuleType,
) -> None:
    """transport 不得为未知地址创建任何替代网络通道。"""

    transport = capacity_module.SyntheticDingTalkTransport(
        redis=FakeCapacityRedis(),
        metrics=capacity_module.CapacityMetrics(),
        identity_delay_seconds=0,
    )

    response = await transport.handle_async_request(httpx.Request("GET", "https://example.invalid"))

    assert response.status_code == 404


@pytest.mark.anyio
async def test_synthetic_transport_returns_mobile_with_one_fixed_detail_delay(
    capacity_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """用户详情成功链必须返回合成电话，且每次详情请求只注入一次固定时延。"""

    sleep_calls: list[float] = []

    async def record_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    monkeypatch.setattr(capacity_module.asyncio, "sleep", record_sleep)
    transport = capacity_module.SyntheticDingTalkTransport(
        redis=FakeCapacityRedis(),
        metrics=capacity_module.CapacityMetrics(),
        identity_delay_seconds=0.125,
    )
    request = httpx.Request(
        "POST",
        DINGTALK_USER_DETAIL_URL,
        params={"access_token": capacity_module._SYNTHETIC_ACCESS_TOKEN},
        json={"userid": "capacity-user-0123456789abcdef"},
    )

    response = await transport.handle_async_request(request)

    assert response.status_code == 200
    assert response.json() == {
        "errcode": 0,
        "result": {"mobile": "10000000", "hide_mobile": False},
    }
    assert sleep_calls == [0.125]


def test_capacity_metrics_aggregates_only_safe_log_contracts(capacity_module: ModuleType) -> None:
    """指标处理器必须接受正式汇总器定义的全部八个交换结果。"""

    metrics = capacity_module.CapacityMetrics()
    handler = capacity_module.CapacityMetricsHandler(metrics)
    expected_results = {
        "success",
        "maintenance",
        "rate_limited",
        "login_guard_unavailable",
        "credential_error",
        "dingtalk_unavailable",
        "session_unavailable",
        "database_error",
    }

    assert capacity_module._EXCHANGE_RESULTS == frozenset(expected_results)
    for result in expected_results:
        record = logging.LogRecord(
            "app.platform.identity.router",
            20,
            "",
            0,
            "identity_exchange_completed request_id=private-request-007 "
            f"identity_provider=dingtalk result={result} duration_ms=401.5",
            (),
            None,
        )
        handler.emit(record)

    snapshot = metrics.snapshot()

    assert snapshot["exchange_results"] == {result: 1 for result in expected_results}
    assert "private-request-007" not in json.dumps(snapshot)


def test_capacity_metrics_handler_rejects_cross_logger_and_polluted_contracts(
    capacity_module: ModuleType,
) -> None:
    """同名指标只能由精确生产 logger 输出，格式追加字段也必须拒绝。"""

    metrics = capacity_module.CapacityMetrics()
    handler = capacity_module.CapacityMetricsHandler(metrics)
    accepted_token = logging.LogRecord(
        "app.integrations.dingtalk.token_provider",
        logging.INFO,
        "",
        0,
        "dingtalk_token_cache event=refresh_success duration_ms=12",
        (),
        None,
    )
    polluted_records = (
        logging.LogRecord(
            "app.platform.identity.router.child",
            logging.INFO,
            "",
            0,
            "identity_exchange_completed request_id=private-request-007 "
            "identity_provider=dingtalk result=success duration_ms=1",
            (),
            None,
        ),
        logging.LogRecord(
            "app.platform.identity.login_guard",
            logging.INFO,
            "",
            0,
            "dingtalk_token_cache event=hit",
            (),
            None,
        ),
        logging.LogRecord(
            "app.integrations.dingtalk.token_provider_like",
            logging.INFO,
            "",
            0,
            "dingtalk_token_cache event=hit",
            (),
            None,
        ),
        logging.LogRecord(
            "app.integrations.dingtalk.token_provider",
            logging.INFO,
            "",
            0,
            "dingtalk_token_cache event=hit duration_ms=12 unexpected=value",
            (),
            None,
        ),
    )

    handler.emit(accepted_token)
    for record in polluted_records:
        handler.emit(record)

    assert metrics.snapshot()["token_events"] == {"refresh_success": 1}
    assert metrics.snapshot()["exchange_results"] == {}
    assert metrics.snapshot()["guard_results"] == {}


@pytest.mark.parametrize(
    ("mode", "expected_key_count"),
    (("profile", 2), ("cold_cache", 4)),
)
def test_capacity_control_endpoints_require_header_and_reset_exact_keys(
    capacity_module: ModuleType,
    dingtalk_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    expected_key_count: int,
) -> None:
    """两种 reset 模式必须按固定 UTC 分钟精确删除各自允许的键。"""

    redis = FakeCapacityRedis()
    fixed_now = datetime(2026, 7, 31, 12, 34, tzinfo=UTC)
    monkeypatch.setattr(capacity_module, "get_settings", lambda: dingtalk_settings)
    monkeypatch.setattr(capacity_module, "get_redis_client", lambda: redis)
    monkeypatch.setattr(capacity_module.os, "environ", {"CAMPUS_AI_SYNTHETIC_CAPACITY": "true"})

    app = capacity_module.create_capacity_app(utc_now=lambda: fixed_now)
    client = TestClient(app)

    assert client.get("/__capacity__/metrics").status_code == 404
    assert (
        client.get(
            "/__capacity__/metrics",
            headers={"X-Campus-AI-Capacity-Control": "capacity-synthetic-v1"},
        ).status_code
        == 200
    )
    assert (
        client.post(
            "/__capacity__/reset",
            headers={"X-Campus-AI-Capacity-Control": "wrong"},
            json={"mode": mode},
        ).status_code
        == 404
    )

    token_provider = DingTalkAppTokenProvider.from_settings(
        redis=cast(TokenRedisClient, redis),
        settings=dingtalk_settings,
    )
    profile_keys = (
        "identity:dingtalk:login:rate:test:202607311234",
        "identity:dingtalk:login:concurrency:test",
    )
    keys = (
        profile_keys
        if mode == "profile"
        else (*profile_keys, token_provider.cache_key, token_provider.lock_key)
    )
    assert len(keys) == expected_key_count
    for expected_deleted in (0, expected_key_count // 2, expected_key_count):
        redis.values.update({key: "value" for key in keys[:expected_deleted]})
        deleted_key_count = len(redis.deleted_keys)
        response = client.post(
            "/__capacity__/reset",
            headers={"X-Campus-AI-Capacity-Control": "capacity-synthetic-v1"},
            json={"mode": mode},
        )

        assert response.status_code == 200
        assert response.json() == {
            "reset": True,
            "mode": mode,
            "deleted": expected_deleted,
        }
        assert redis.deleted_keys[deleted_key_count:] == list(keys)


@pytest.mark.parametrize("payload", ({}, {"mode": "all"}, {"mode": "profile", "extra": True}))
def test_capacity_reset_rejects_missing_wrong_or_non_fixed_mode(
    capacity_module: ModuleType,
    dingtalk_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    payload: dict[str, object],
) -> None:
    """正确控制头下只接受固定单字段 mode，错误输入以安全 422 拒绝。"""

    redis = FakeCapacityRedis()
    monkeypatch.setattr(capacity_module, "get_settings", lambda: dingtalk_settings)
    monkeypatch.setattr(capacity_module, "get_redis_client", lambda: redis)
    monkeypatch.setattr(capacity_module.os, "environ", {"CAMPUS_AI_SYNTHETIC_CAPACITY": "true"})
    client = TestClient(capacity_module.create_capacity_app())

    response = client.post(
        "/__capacity__/reset",
        headers={"X-Campus-AI-Capacity-Control": "capacity-synthetic-v1"},
        json=payload,
    )

    assert response.status_code == 422
    assert response.json() == {"detail": "容量重置模式无效。"}
    assert redis.deleted_keys == []


def test_capacity_reset_requires_control_header_before_reading_mode(
    capacity_module: ModuleType,
    dingtalk_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """控制头缺失时即使请求体也无效，端点仍必须隐藏为安全 404。"""

    redis = FakeCapacityRedis()
    monkeypatch.setattr(capacity_module, "get_settings", lambda: dingtalk_settings)
    monkeypatch.setattr(capacity_module, "get_redis_client", lambda: redis)
    monkeypatch.setattr(capacity_module.os, "environ", {"CAMPUS_AI_SYNTHETIC_CAPACITY": "true"})
    client = TestClient(capacity_module.create_capacity_app())

    response = client.post("/__capacity__/reset", json={})

    assert response.status_code == 404
    assert redis.deleted_keys == []


def test_capacity_module_keeps_the_production_app_private(capacity_module: ModuleType) -> None:
    """组合模块只能通过工厂取得生产应用，不能公开全局 app 绕过校验。"""

    assert hasattr(capacity_module, "_production_app")
    assert not hasattr(capacity_module, "app")


def test_capacity_module_fixture_restores_shared_fastapi_state(capacity_module: ModuleType) -> None:
    """每次动态加载都从未注入容量路由与依赖覆盖的稳定生产状态开始。"""

    from app.main import app as production_app
    from app.platform.identity.router import get_dingtalk_identity_verifier

    assert all(
        not getattr(route, "path", "").startswith("/__capacity__/")
        for route in production_app.router.routes
    )
    assert get_dingtalk_identity_verifier not in production_app.dependency_overrides


def test_real_token_provider_with_synthetic_transport_refreshes_once(
    capacity_module: ModuleType,
    dingtalk_settings: Settings,
) -> None:
    """三十个同步登录完成身份与电话链时只允许一次合成 Token 获取。"""

    redis = FakeCapacityRedis()
    metrics = capacity_module.CapacityMetrics()
    transport = capacity_module.SyntheticDingTalkTransport(
        redis=redis,
        metrics=metrics,
        identity_delay_seconds=0,
        token_delay_seconds=0.05,
    )
    barrier = threading.Barrier(30)

    def verify(index: int) -> str | None:
        barrier.wait(timeout=2)
        token_provider = DingTalkAppTokenProvider.from_settings(
            redis=cast(TokenRedisClient, redis),
            settings=dingtalk_settings,
        )
        verifier = DingTalkIdentityVerifier(
            settings=dingtalk_settings,
            token_provider=token_provider,
            http_transport=transport,
        )
        code = f"CAPACITY-v1-concurrent-{index:04d}-0123456789abcdef"
        identity = verifier.verify(code)
        return verifier.fetch_optional_mobile(identity.personnel_id)

    handler = capacity_module.CapacityMetricsHandler(metrics)
    token_logger = logging.getLogger("app.integrations.dingtalk.token_provider")
    token_logger.addHandler(handler)
    try:
        with ThreadPoolExecutor(max_workers=30) as executor:
            results = list(executor.map(verify, range(30)))
    finally:
        token_logger.removeHandler(handler)

    snapshot = metrics.snapshot()
    assert results == 30 * ["10000000"]
    assert snapshot["external_token_fetches"] == 1
    assert snapshot["token_events"]["miss"] == 1
    assert snapshot["token_events"]["refresh_success"] == 1
    assert 30 <= snapshot["token_events"]["hit"] <= 59
    assert snapshot["token_events"].get("refresh_failed", 0) == 0
    assert redis.lock_contention_count > 0
