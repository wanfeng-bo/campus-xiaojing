from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest
from redis.exceptions import RedisError

from app.platform.identity.login_guard import (
    LOGIN_CONCURRENCY_KEY_TTL_SECONDS,
    LOGIN_CONCURRENCY_LEASE_SECONDS,
    LOGIN_RATE_TTL_SECONDS,
    DingTalkLoginGuard,
    DingTalkLoginGuardUnavailableError,
    DingTalkLoginMaintenanceError,
    DingTalkLoginRateLimitError,
)

EXPECTED_ACQUIRE_LOGIN_GUARD_SCRIPT = """
local rate_ttl_seconds = tonumber(ARGV[5])
local concurrency_lease_seconds = tonumber(ARGV[6])
local concurrency_key_ttl_seconds = tonumber(ARGV[7])
if redis.call('get', KEYS[1]) then
    return {'maintenance', 0, 0}
end
local rate_count = redis.call('incr', KEYS[2])
if rate_count == 1 then
    redis.call('expire', KEYS[2], rate_ttl_seconds)
end
if rate_count > tonumber(ARGV[1]) then
    return {'rate_limited', rate_count, 0}
end
redis.call('zremrangebyscore', KEYS[3], '-inf', tonumber(ARGV[3]))
local concurrency_count = redis.call('zcard', KEYS[3])
if concurrency_count >= tonumber(ARGV[2]) then
    return {'concurrency_limited', rate_count, concurrency_count}
end
redis.call('zadd', KEYS[3], tonumber(ARGV[3]) + concurrency_lease_seconds, ARGV[4])
redis.call('expire', KEYS[3], concurrency_key_ttl_seconds)
return {'allowed', rate_count, concurrency_count + 1}
"""


class FakeLoginGuardRedis:
    """严格按登录 Lua 契约模拟文本 Redis 的单次原子执行，不连接真实 Redis。"""

    def __init__(
        self,
        *,
        maintenance: bool = False,
        rate_count: int = 0,
        lua_error: bool = False,
        response_override: list[object] | None = None,
        zrem_error: bool = False,
    ) -> None:
        """初始化可观测 Redis 状态和可控故障，不保存身份或请求资料。"""

        self.maintenance = maintenance
        self.rate_count = rate_count
        self.concurrent_members: set[str] = set()
        self.rate_ttls: dict[str, int] = {}
        self.concurrency_ttl_seconds: int | None = None
        self.command_trace: list[str] = []
        self.raw_responses: list[list[object]] = []
        self.rate_keys: list[str] = []
        self._rate_counts: dict[str, int] = {}
        self._initial_rate_count = rate_count
        self._member_expirations: dict[str, int] = {}
        self._lua_error = lua_error
        self._response_override = response_override
        self._zrem_error = zrem_error
        self._state_lock = threading.Lock()

    def add_concurrency_lease(self, member: str, *, expires_at: int) -> None:
        """预置其他请求租约，以真实 ZSET score 验证清理和上限分支。"""

        with self._state_lock:
            self.concurrent_members.add(member)
            self._member_expirations[member] = expires_at

    def eval(self, script: str, numkeys: int, *keys_and_args: str) -> list[object]:
        """校验完整 Lua、键序和参数后原子执行维护、速率与并发门。"""

        self._verify_eval_contract(script, numkeys, keys_and_args)
        if self._lua_error:
            raise RedisError("受控 Lua 故障")
        if self._response_override is not None:
            self.raw_responses.append(self._response_override)
            return self._response_override

        maintenance_key, rate_key, concurrency_key, *arguments = keys_and_args
        del maintenance_key, concurrency_key
        rate_limit, concurrency_limit, epoch_seconds, request_id, rate_ttl, lease_ttl, key_ttl = (
            arguments
        )
        epoch = int(epoch_seconds)
        with self._state_lock:
            self.command_trace.append("GET")
            if self.maintenance:
                return self._record_response(["maintenance", 0, 0])

            self.command_trace.append("INCR")
            self.rate_keys.append(rate_key)
            next_rate_count = self._rate_counts.get(rate_key, self._initial_rate_count) + 1
            self._rate_counts[rate_key] = next_rate_count
            self.rate_count = next_rate_count
            if next_rate_count == 1:
                self.command_trace.append("EXPIRE_RATE")
                self.rate_ttls[rate_key] = int(rate_ttl)
            self.command_trace.append("RATE_LIMIT")
            if next_rate_count > int(rate_limit):
                return self._record_response(["rate_limited", next_rate_count, 0])

            self.command_trace.append("ZREMRANGEBYSCORE")
            self.concurrent_members = {
                member
                for member in self.concurrent_members
                if self._member_expirations[member] > epoch
            }
            self._member_expirations = {
                member: expires_at
                for member, expires_at in self._member_expirations.items()
                if member in self.concurrent_members
            }
            self.command_trace.append("ZCARD")
            concurrency_count = len(self.concurrent_members)
            self.command_trace.append("CONCURRENCY_LIMIT")
            if concurrency_count >= int(concurrency_limit):
                return self._record_response(
                    ["concurrency_limited", next_rate_count, concurrency_count]
                )

            self.command_trace.append("ZADD")
            self.concurrent_members.add(request_id)
            self._member_expirations[request_id] = epoch + int(lease_ttl)
            self.command_trace.append("EXPIRE_CONCURRENCY")
            self.concurrency_ttl_seconds = int(key_ttl)
            return self._record_response(["allowed", next_rate_count, concurrency_count + 1])

    def zrem(self, name: str, *values: str) -> int:
        """模拟 redis-py ZREM 返回整数；可控故障用于验证 finally 不遮蔽下游异常。"""

        assert name == "identity:dingtalk:login:concurrency:test"
        assert len(values) == 1
        if self._zrem_error:
            raise RedisError("受控租约释放故障")
        member = values[0]
        with self._state_lock:
            existed = member in self.concurrent_members
            self.concurrent_members.discard(member)
            self._member_expirations.pop(member, None)
            return int(existed)

    def _verify_eval_contract(
        self,
        script: str,
        numkeys: int,
        keys_and_args: tuple[str, ...],
    ) -> None:
        """拒绝不完整脚本、错序键或漂移的 TTL 参数，模拟 Redis 调用契约而非预设答案。"""

        assert script == EXPECTED_ACQUIRE_LOGIN_GUARD_SCRIPT
        assert numkeys == 3
        assert len(keys_and_args) == 10
        maintenance_key, rate_key, concurrency_key, *arguments = keys_and_args
        assert maintenance_key == "identity:dingtalk:login:maintenance:test"
        assert rate_key.startswith("identity:dingtalk:login:rate:test:")
        assert concurrency_key == "identity:dingtalk:login:concurrency:test"
        assert all(isinstance(argument, str) for argument in arguments)
        assert len(arguments) == 7
        rate_limit, concurrency_limit, epoch_seconds, request_id, rate_ttl, lease_ttl, key_ttl = (
            arguments
        )
        assert int(rate_limit) > 0
        assert int(concurrency_limit) > 0
        assert int(epoch_seconds) > 0
        assert request_id
        assert rate_ttl == str(LOGIN_RATE_TTL_SECONDS)
        assert lease_ttl == str(LOGIN_CONCURRENCY_LEASE_SECONDS)
        assert key_ttl == str(LOGIN_CONCURRENCY_KEY_TTL_SECONDS)

    def _record_response(self, response: list[object]) -> list[object]:
        """记录并返回 redis-py decode_responses 下的文本结果与整数计数结构。"""

        assert isinstance(response[0], str)
        assert isinstance(response[1], int)
        assert isinstance(response[2], int)
        self.raw_responses.append(response)
        return response


class MutableClock:
    """提供可推进 UTC 时钟，验证跨分钟键隔离和 score 边界。"""

    def __init__(self, now: datetime) -> None:
        """从指定 UTC 时刻开始，避免依赖本机时间。"""

        self.now_value = now

    def now(self) -> datetime:
        """返回当前受控 UTC 时刻。"""

        return self.now_value

    def advance(self, seconds: int) -> None:
        """推进受控时钟以模拟分钟边界。"""

        self.now_value += timedelta(seconds=seconds)


def build_guard(
    redis: FakeLoginGuardRedis,
    *,
    rate_limit: int = 120,
    concurrency_limit: int = 30,
    now: Callable[[], datetime] | None = None,
) -> DingTalkLoginGuard:
    """使用固定测试环境和受控时钟构造应用级登录保护。"""

    return DingTalkLoginGuard(
        redis=redis,
        app_env="test",
        rate_limit=rate_limit,
        concurrency_limit=concurrency_limit,
        now=now or (lambda: datetime(2026, 7, 29, tzinfo=UTC)),
    )


def test_acquire_lua_has_fixed_command_order_and_parameterized_ttls() -> None:
    """Lua 必须以固定原子顺序使用三项 TTL 参数，不能退化为散命令或硬编码漂移。"""

    from app.platform.identity.login_guard import _ACQUIRE_LOGIN_GUARD_SCRIPT

    assert _ACQUIRE_LOGIN_GUARD_SCRIPT == EXPECTED_ACQUIRE_LOGIN_GUARD_SCRIPT
    commands = [
        "redis.call('get', KEYS[1])",
        "redis.call('incr', KEYS[2])",
        "redis.call('expire', KEYS[2], rate_ttl_seconds)",
        "if rate_count > tonumber(ARGV[1])",
        "redis.call('zremrangebyscore', KEYS[3], '-inf', tonumber(ARGV[3]))",
        "redis.call('zcard', KEYS[3])",
        "if concurrency_count >= tonumber(ARGV[2])",
        "redis.call('zadd', KEYS[3], tonumber(ARGV[3]) + concurrency_lease_seconds, ARGV[4])",
        "redis.call('expire', KEYS[3], concurrency_key_ttl_seconds)",
        "return {'allowed', rate_count, concurrency_count + 1}",
    ]

    assert [EXPECTED_ACQUIRE_LOGIN_GUARD_SCRIPT.index(command) for command in commands] == sorted(
        EXPECTED_ACQUIRE_LOGIN_GUARD_SCRIPT.index(command) for command in commands
    )
    assert "ARGV[5]" in EXPECTED_ACQUIRE_LOGIN_GUARD_SCRIPT
    assert "ARGV[6]" in EXPECTED_ACQUIRE_LOGIN_GUARD_SCRIPT
    assert "ARGV[7]" in EXPECTED_ACQUIRE_LOGIN_GUARD_SCRIPT


def test_maintenance_rejects_without_consuming_rate_or_concurrency() -> None:
    """维护租约存在时必须在速率和并发副作用前拒绝身份交换。"""

    redis = FakeLoginGuardRedis(maintenance=True)
    guard = build_guard(redis)

    with pytest.raises(DingTalkLoginMaintenanceError):
        with guard.admit("request-1"):
            raise AssertionError("维护期间不得进入身份交换")

    assert redis.rate_count == 0
    assert redis.concurrent_members == set()
    assert redis.command_trace == ["GET"]
    assert redis.raw_responses == [["maintenance", 0, 0]]


def test_global_rate_rejection_has_no_concurrency_lease() -> None:
    """超出全局分钟上限的请求不得占用并发租约或进入身份交换。"""

    redis = FakeLoginGuardRedis(rate_count=2)
    guard = build_guard(redis, rate_limit=2, concurrency_limit=30)

    with pytest.raises(DingTalkLoginRateLimitError):
        with guard.admit("request-3"):
            raise AssertionError("超限请求不得执行")

    assert "request-3" not in redis.concurrent_members
    assert redis.command_trace == ["GET", "INCR", "RATE_LIMIT"]
    assert redis.raw_responses == [["rate_limited", 3, 0]]


def test_concurrency_limit_rejects_after_cleaning_only_expired_leases() -> None:
    """未过期租约达到上限时返回固定计数，不为新请求写入 ZADD。"""

    now = datetime(2026, 7, 29, tzinfo=UTC)
    redis = FakeLoginGuardRedis()
    redis.add_concurrency_lease("in-flight", expires_at=int(now.timestamp()) + 1)
    guard = build_guard(redis, concurrency_limit=1, now=lambda: now)

    with pytest.raises(DingTalkLoginRateLimitError):
        with guard.admit("request-2"):
            raise AssertionError("并发超限不得进入身份交换")

    assert redis.concurrent_members == {"in-flight"}
    assert redis.command_trace == [
        "GET",
        "INCR",
        "EXPIRE_RATE",
        "RATE_LIMIT",
        "ZREMRANGEBYSCORE",
        "ZCARD",
        "CONCURRENCY_LIMIT",
    ]
    assert redis.raw_responses == [["concurrency_limited", 1, 1]]


def test_score_at_current_epoch_is_expired_before_concurrency_limit_check() -> None:
    """ZREMRANGEBYSCORE 必须清理 score 等于当前 epoch 的租约，避免边界阻塞新登录。"""

    now = datetime(2026, 7, 29, tzinfo=UTC)
    redis = FakeLoginGuardRedis()
    redis.add_concurrency_lease("expired-now", expires_at=int(now.timestamp()))
    guard = build_guard(redis, concurrency_limit=1, now=lambda: now)

    with guard.admit("request-1"):
        assert redis.concurrent_members == {"request-1"}

    assert redis.concurrent_members == set()
    assert redis.raw_responses == [["allowed", 1, 1]]


def test_first_rate_and_concurrency_writes_use_fixed_ttls_and_lease_duration() -> None:
    """首次分钟计数、并发键和 ZSET score 分别使用 120、30、15 秒命名常量。"""

    now = datetime(2026, 7, 29, tzinfo=UTC)
    redis = FakeLoginGuardRedis()
    guard = build_guard(redis, now=lambda: now)

    with guard.admit("request-1"):
        assert redis._member_expirations["request-1"] == int(now.timestamp()) + 15

    assert redis.rate_ttls == {"identity:dingtalk:login:rate:test:202607290000": 120}
    assert redis.concurrency_ttl_seconds == 30


def test_utc_minute_bucket_changes_after_minute_boundary() -> None:
    """同一环境跨 UTC 分钟时必须写入新速率键，而不是延续上一分钟计数。"""

    clock = MutableClock(datetime(2026, 7, 29, 0, 0, 59, tzinfo=UTC))
    redis = FakeLoginGuardRedis()
    guard = build_guard(redis, now=clock.now)

    with guard.admit("request-1"):
        pass
    clock.advance(1)
    with guard.admit("request-2"):
        pass

    assert redis.rate_keys == [
        "identity:dingtalk:login:rate:test:202607290000",
        "identity:dingtalk:login:rate:test:202607290001",
    ]
    assert redis.raw_responses == [["allowed", 1, 1], ["allowed", 1, 1]]


@pytest.mark.parametrize(
    "redis",
    [
        FakeLoginGuardRedis(lua_error=True),
        FakeLoginGuardRedis(response_override=["allowed", "not-an-int", 1]),
    ],
)
def test_lua_failure_or_corrupt_response_fails_closed(redis: FakeLoginGuardRedis) -> None:
    """Lua 执行异常或非 redis-py 文本/整数结果必须拒绝身份交换，不进入下游。"""

    guard = build_guard(redis)

    with pytest.raises(DingTalkLoginGuardUnavailableError):
        with guard.admit("request-1"):
            raise AssertionError("登录门不可用时不得进入身份交换")


def test_release_redis_error_does_not_mask_downstream_error_or_log_identifiers(caplog) -> None:
    """释放失败仅记录无标识安全警告，必须保留下游异常且不泄露请求或 Redis 键。"""

    redis = FakeLoginGuardRedis(zrem_error=True)
    guard = build_guard(redis)

    with caplog.at_level(logging.WARNING, logger="app.platform.identity.login_guard"):
        with pytest.raises(RuntimeError, match="模拟下游失败"):
            with guard.admit("request-safe-001"):
                raise RuntimeError("模拟下游失败")

    message = caplog.records[-1].getMessage()
    assert "request-safe-001" not in message
    assert "identity:dingtalk" not in message
