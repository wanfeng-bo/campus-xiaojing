from __future__ import annotations

import json
import math
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from redis.exceptions import LockNotOwnedError, RedisError

from app.core.settings import Settings
from app.integrations.dingtalk.errors import DingTalkUnavailableError
from app.integrations.dingtalk.token_provider import (
    DingTalkAppTokenProvider,
    FetchedDingTalkAppToken,
)


class FakeTokenRedis:
    """提供线程安全的最小文本 Redis 替身，隔离 Token 缓存与刷新锁行为。"""

    def __init__(
        self,
        *,
        lock_available: bool = True,
        lock_token_as_bytes: bool = False,
    ) -> None:
        """初始化可控制刷新锁可用性的内存键值存储。"""

        self.values: dict[str, str] = {}
        self.deleted_keys: list[str] = []
        self.lock_available = lock_available
        self.lock_token_as_bytes = lock_token_as_bytes
        self.lock_held = False
        self.active_lock_token: str | bytes | None = None
        self.lock_token_counter = 0
        self.eval_calls: list[tuple[str, int, tuple[object, ...]]] = []
        self.state_lock = threading.Lock()

    def get(self, key: str) -> str | None:
        """读取文本缓存值，不连接真实 Redis。"""

        with self.state_lock:
            return self.values.get(key)

    def set(self, key: str, value: str, *, ex: int) -> bool:
        """保存带正数有效期的文本缓存值。"""

        assert ex > 0
        with self.state_lock:
            self.values[key] = value
        return True

    def delete(self, key: str) -> int:
        """删除键并记录精确删除目标，供失效缓存断言使用。"""

        with self.state_lock:
            self.deleted_keys.append(key)
            return int(self.values.pop(key, None) is not None)

    def lock(self, key: str, *, timeout: int, blocking: bool) -> FakeOwnedLock:
        """返回固定十秒、非阻塞的共享刷新锁替身。"""

        assert key
        assert timeout == 10
        assert blocking is False
        return FakeOwnedLock(self)

    def eval(self, script: str, numkeys: int, *keys_and_args: object) -> int:
        """模拟仅当刷新锁令牌仍匹配时才原子写入 Token 缓存的 Lua 脚本。"""

        assert numkeys == 2
        self.eval_calls.append((script, numkeys, keys_and_args))
        lock_key, cache_key, expected_token = keys_and_args[:3]
        assert isinstance(lock_key, str)
        assert isinstance(cache_key, str)
        assert isinstance(expected_token, (str, bytes))
        assert "KEYS[1]" in script
        assert "KEYS[2]" in script
        with self.state_lock:
            if self.active_lock_token != expected_token:
                return 0
            if "redis.call('set'" in script:
                assert len(keys_and_args) == 5
                payload, effective_ttl = keys_and_args[3:]
                assert isinstance(payload, str)
                assert isinstance(effective_ttl, int)
                assert effective_ttl > 0
                self.values[cache_key] = payload
            elif "redis.call('del'" in script:
                assert len(keys_and_args) == 3
                self.deleted_keys.append(cache_key)
                self.values.pop(cache_key, None)
            else:
                raise AssertionError("未识别的条件 Lua 操作")
            return 1

    def expire_active_lock(self) -> None:
        """模拟 Redis 刷新锁在持锁获取器运行期间自然到期。"""

        with self.state_lock:
            self.lock_held = False
            self.active_lock_token = None


class FakeLockLocal:
    """保存测试刷新锁的当前线程令牌，匹配 redis-py 的最小公开状态。"""

    def __init__(self) -> None:
        """初始化没有持锁令牌的本地状态。"""

        self.token: str | bytes | None = None


class FakeOwnedLock:
    """模拟 Redis 分布式锁的所有权与释放边界。"""

    def __init__(self, redis: FakeTokenRedis) -> None:
        """绑定共享 Redis 替身以维护锁占用状态。"""

        self.redis = redis
        self.owns_lock = False
        self.local = FakeLockLocal()

    def acquire(self, *, blocking: bool = False) -> bool:
        """仅允许非阻塞获取；已占用或不可用时返回失败。"""

        assert blocking is False
        with self.redis.state_lock:
            if not self.redis.lock_available or self.redis.lock_held:
                return False
            self.redis.lock_token_counter += 1
            token = f"refresh-lock-token-{self.redis.lock_token_counter}"
            self.local.token = token.encode() if self.redis.lock_token_as_bytes else token
            self.redis.lock_held = True
            self.redis.active_lock_token = self.local.token
            self.owns_lock = True
            return True

    def release(self) -> None:
        """只允许锁拥有者释放共享刷新锁。"""

        with self.redis.state_lock:
            assert self.owns_lock
            if self.redis.active_lock_token != self.local.token:
                self.owns_lock = False
                self.local.token = None
                raise LockNotOwnedError("refresh lock expired")
            self.redis.lock_held = False
            self.redis.active_lock_token = None
            self.owns_lock = False
            self.local.token = None


class ReleaseFailingLock(FakeOwnedLock):
    """模拟 Redis 在成功释放锁后报告连接故障的边界。"""

    def release(self) -> None:
        """先完成释放，再抛出 Redis 协调异常以验证业务结果不被掩盖。"""

        super().release()
        raise RedisError("release connection lost")


class ReleaseFailingRedis(FakeTokenRedis):
    """只在刷新锁释放阶段失败的 Redis 替身。"""

    def lock(self, key: str, *, timeout: int, blocking: bool) -> ReleaseFailingLock:
        """返回与基础替身同契约、但释放后失败的锁。"""

        assert key
        assert timeout == 10
        assert blocking is False
        return ReleaseFailingLock(self)


class ExpireBeforeDeleteRedis(FakeTokenRedis):
    """在旧所有者删除前模拟锁到期和新所有者写入新应用 Token。"""

    def __init__(self) -> None:
        """初始化只触发一次的失锁删除竞态。"""

        super().__init__()
        self._race_triggered = False

    def delete(self, key: str) -> int:
        """模拟普通删除在失锁后误删新所有者缓存的旧实现。"""

        self._install_new_owner_cache(key)
        return super().delete(key)

    def eval(self, script: str, numkeys: int, *keys_and_args: object) -> int:
        """在条件删除前交接锁；Lua 比较失败后必须保留新缓存。"""

        if "redis.call('del'" in script:
            cache_key = keys_and_args[1]
            assert isinstance(cache_key, str)
            self._install_new_owner_cache(cache_key)
        return super().eval(script, numkeys, *keys_and_args)

    def _install_new_owner_cache(self, cache_key: str) -> None:
        """模拟新所有者已完成的有效缓存写入和旧锁令牌失效。"""

        with self.state_lock:
            if self._race_triggered:
                return
            self._race_triggered = True
            self.lock_held = False
            self.active_lock_token = None
            self.values[cache_key] = json.dumps(
                {
                    "access_token": "new-owner-token",
                    "cached_at": 1_000.0,
                    "expires_at": 7_000.0,
                }
            )


class FakeClock:
    """提供可预测的 epoch 与单调时间，sleep 只推进受控时间。"""

    def __init__(self, *, epoch: float) -> None:
        """以相同初值初始化墙钟和单调钟。"""

        self.epoch = epoch
        self.monotonic_seconds = 0.0

    def wall_clock(self) -> float:
        """返回用于缓存过期时间的固定 UTC epoch。"""

        return self.epoch

    def monotonic_clock(self) -> float:
        """返回可推进的单调时间。"""

        return self.monotonic_seconds

    def sleep(self, seconds: float) -> None:
        """推进单调时间而不等待真实时钟。"""

        self.monotonic_seconds += seconds


class AdvancingReadRedis(FakeTokenRedis):
    """每次缓存读取推进受控时钟，模拟完整 Redis 读取消耗最大 I/O 预算。"""

    def __init__(self, *, clock: FakeClock, read_seconds: float) -> None:
        """绑定读取耗时，并保持刷新锁始终由另一个请求持有。"""

        super().__init__(lock_available=False)
        self._clock = clock
        self._read_seconds = read_seconds

    def get(self, key: str) -> str | None:
        """先消耗完整读取预算，再返回缓存结果。"""

        self._clock.sleep(self._read_seconds)
        return super().get(key)


class AdvancingAcquireLock(FakeOwnedLock):
    """为每次非阻塞锁获取和成功后的释放推进受控时钟。"""

    def __init__(self, redis: AdvancingAcquireRedis) -> None:
        super().__init__(redis)
        self._advancing_redis = redis

    def acquire(self, *, blocking: bool = False) -> bool:
        self._advancing_redis.clock.sleep(self._advancing_redis.acquire_seconds)
        self._advancing_redis.acquire_attempts += 1
        if (
            self._advancing_redis.succeed_on_attempt is None
            or self._advancing_redis.acquire_attempts != self._advancing_redis.succeed_on_attempt
        ):
            return False
        acquired = super().acquire(blocking=blocking)
        if acquired:
            self._advancing_redis.acquired_at = self._advancing_redis.clock.monotonic_seconds
        return acquired

    def release(self) -> None:
        self._advancing_redis.clock.sleep(self._advancing_redis.release_seconds)
        super().release()


class AdvancingAcquireRedis(FakeTokenRedis):
    """模拟锁等待耗时，并把取得锁后的删除/释放耗时单独记录。"""

    def __init__(
        self,
        *,
        clock: FakeClock,
        acquire_seconds: float,
        succeed_on_attempt: int | None,
        delete_seconds: float = 0.0,
        release_seconds: float = 0.0,
    ) -> None:
        super().__init__()
        self.clock = clock
        self.acquire_seconds = acquire_seconds
        self.succeed_on_attempt = succeed_on_attempt
        self.delete_seconds = delete_seconds
        self.release_seconds = release_seconds
        self.acquire_attempts = 0
        self.acquired_at: float | None = None

    def lock(self, key: str, *, timeout: int, blocking: bool) -> AdvancingAcquireLock:
        assert key
        assert timeout == 10
        assert blocking is False
        return AdvancingAcquireLock(self)

    def eval(self, script: str, numkeys: int, *keys_and_args: object) -> int:
        self.clock.sleep(self.delete_seconds)
        return super().eval(script, numkeys, *keys_and_args)


class RealTestClock:
    """为并发等待测试提供真实但短暂的单调时间与睡眠。"""

    def wall_clock(self) -> float:
        """返回当前 UTC epoch。"""

        return time.time()

    def monotonic_clock(self) -> float:
        """返回当前单调时间。"""

        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        """执行真实短睡眠，让并发刷新者可完成写缓存。"""

        time.sleep(seconds)


class DeadlineCrossingClock:
    """模拟线程在循环条件与剩余时间计算之间被调度暂停到截止时间之后。"""

    def __init__(self) -> None:
        """预置 deadline、循环条件和剩余时间三次单调时钟读数。"""

        self._monotonic_values = iter((0.0, 2.9, 3.1))

    def wall_clock(self) -> float:
        """返回缓存校验所需的固定 UTC epoch。"""

        return 1_000.0

    def monotonic_clock(self) -> float:
        """依次返回跨越等待截止时间的受控单调时钟读数。"""

        return next(self._monotonic_values, 3.1)

    def sleep(self, seconds: float) -> None:
        """拒绝负数睡眠，确保实现先检查剩余等待时间。"""

        if seconds < 0:
            raise ValueError("sleep duration must not be negative")


def build_provider(
    *,
    redis: FakeTokenRedis,
    clock: FakeClock | RealTestClock,
) -> DingTalkAppTokenProvider:
    """构造注入受控时钟和零抖动函数的 Token 提供者。"""

    return DingTalkAppTokenProvider(
        redis=redis,
        cache_key="identity:dingtalk:app-token:test:0123456789abcdef",
        lock_key="identity:dingtalk:app-token-refresh:test:0123456789abcdef",
        wall_clock=clock.wall_clock,
        monotonic_clock=clock.monotonic_clock,
        sleep=clock.sleep,
        random_jitter=lambda _upper_bound: 0,
    )


def test_cache_hit_does_not_call_fetcher() -> None:
    """有效缓存命中时不得向钉钉请求新的应用 Token。"""

    redis = FakeTokenRedis()
    clock = FakeClock(epoch=1_000.0)
    provider = build_provider(redis=redis, clock=clock)
    redis.values[provider.cache_key] = json.dumps(
        {
            "access_token": "cached-token",
            "cached_at": 900.0,
            "expires_at": 7_000.0,
        }
    )

    def forbidden_fetch() -> FetchedDingTalkAppToken:
        raise AssertionError("缓存命中时不得访问钉钉")

    assert provider.get(forbidden_fetch) == "cached-token"


def test_thirty_concurrent_cache_misses_have_one_fetcher() -> None:
    """三十个并发未命中请求只能由一个持锁请求向钉钉刷新。"""

    redis = FakeTokenRedis()
    provider = build_provider(redis=redis, clock=RealTestClock())
    fetch_count = 0
    fetch_lock = threading.Lock()
    fetch_started = threading.Event()
    release_fetch = threading.Event()

    def fetch() -> FetchedDingTalkAppToken:
        nonlocal fetch_count
        with fetch_lock:
            fetch_count += 1
        fetch_started.set()
        assert release_fetch.wait(timeout=2)
        return FetchedDingTalkAppToken("fresh-token", 7_200)

    with ThreadPoolExecutor(max_workers=30) as executor:
        futures = [executor.submit(provider.get, fetch) for _ in range(30)]
        assert fetch_started.wait(timeout=1)
        release_fetch.set()
        results = [future.result(timeout=4) for future in futures]

    assert results == ["fresh-token"] * 30
    assert fetch_count == 1


def test_expired_refresher_cannot_overwrite_the_new_lock_owner_cache() -> None:
    """旧刷新锁到期后，即使旧获取器才返回也不得覆盖新所有者写入的 Token。"""

    redis = FakeTokenRedis()
    clock = RealTestClock()
    old_provider = build_provider(redis=redis, clock=clock)
    new_provider = build_provider(redis=redis, clock=clock)
    old_fetch_started = threading.Event()
    release_old_fetch = threading.Event()

    def fetch_old_token() -> FetchedDingTalkAppToken:
        old_fetch_started.set()
        assert release_old_fetch.wait(timeout=2)
        return FetchedDingTalkAppToken("old-token", 7_200)

    with ThreadPoolExecutor(max_workers=1) as executor:
        old_result = executor.submit(old_provider.get, fetch_old_token)
        assert old_fetch_started.wait(timeout=1)
        redis.expire_active_lock()

        assert new_provider.get(lambda: FetchedDingTalkAppToken("new-token", 7_200)) == "new-token"
        release_old_fetch.set()

        assert old_result.result(timeout=2) == "new-token"

    assert json.loads(redis.values[old_provider.cache_key])["access_token"] == "new-token"


def test_conditional_write_binds_redis_bytes_lock_token_to_expected_keys_and_args() -> None:
    """条件写入必须传递 redis-py bytes 锁令牌，并按锁键、缓存键、令牌、值、TTL 排列。"""

    redis = FakeTokenRedis(lock_token_as_bytes=True)
    provider = build_provider(redis=redis, clock=FakeClock(epoch=1_000.0))

    assert provider.get(lambda: FetchedDingTalkAppToken("fresh-token", 7_200)) == "fresh-token"

    script, numkeys, keys_and_args = redis.eval_calls[-1]
    assert "redis.call('set'" in script
    assert numkeys == 2
    assert keys_and_args[0] == provider.lock_key
    assert keys_and_args[1] == provider.cache_key
    assert isinstance(keys_and_args[2], bytes)
    assert isinstance(keys_and_args[3], str)
    assert keys_and_args[4] == 6_900


def test_waiter_stops_after_three_seconds_without_bypassing_lock() -> None:
    """锁不可用时等待者三秒后失败，绝不绕过单刷新者协议调用钉钉。"""

    redis = FakeTokenRedis(lock_available=False)
    clock = FakeClock(epoch=1_000.0)
    provider = build_provider(redis=redis, clock=clock)
    fetch_count = 0

    def fetch() -> FetchedDingTalkAppToken:
        nonlocal fetch_count
        fetch_count += 1
        return FetchedDingTalkAppToken("must-not-be-used", 7_200)

    with pytest.raises(DingTalkUnavailableError):
        provider.get(fetch)

    assert fetch_count == 0
    assert clock.monotonic_seconds >= 3.0


def test_waiter_never_sleeps_past_the_three_second_deadline() -> None:
    """轮询抖动接近上限时，等待者仍不得把实际等待延长到三秒之外。"""

    redis = FakeTokenRedis(lock_available=False)
    clock = FakeClock(epoch=1_000.0)
    provider = DingTalkAppTokenProvider(
        redis=redis,
        cache_key="identity:dingtalk:app-token:test:0123456789abcdef",
        lock_key="identity:dingtalk:app-token-refresh:test:0123456789abcdef",
        wall_clock=clock.wall_clock,
        monotonic_clock=clock.monotonic_clock,
        sleep=clock.sleep,
        random_jitter=lambda _upper_bound: 49,
    )

    with pytest.raises(DingTalkUnavailableError):
        provider.get(lambda: FetchedDingTalkAppToken("must-not-be-used", 7_200))

    assert clock.monotonic_seconds == 3.0


def test_waiter_reserves_the_last_redis_read_inside_three_second_budget() -> None:
    """独立等待窗口必须包含最后一次完整 Redis 读取，不能只限制 sleep。"""

    settings = Settings.model_validate(
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
            "redis_read_timeout_seconds": 0.5,
        }
    )
    clock = FakeClock(epoch=1_000.0)
    redis = AdvancingReadRedis(clock=clock, read_seconds=0.5)
    provider = DingTalkAppTokenProvider.from_settings(
        redis=redis,
        settings=settings,
        wall_clock=clock.wall_clock,
        monotonic_clock=clock.monotonic_clock,
        sleep=clock.sleep,
        random_jitter=lambda _upper_bound: 50,
    )

    with pytest.raises(DingTalkUnavailableError):
        provider.get(lambda: FetchedDingTalkAppToken("must-not-be-used", 7_200))

    initial_cache_read_seconds = 0.5
    assert clock.monotonic_seconds - initial_cache_read_seconds <= 3.0


def test_waiter_handles_a_deadline_crossing_between_clock_reads() -> None:
    """线程在循环内被暂停到截止后时，必须返回受控不可用错误而非负数 sleep。"""

    redis = FakeTokenRedis(lock_available=False)
    clock = DeadlineCrossingClock()
    provider = DingTalkAppTokenProvider(
        redis=redis,
        cache_key="identity:dingtalk:app-token:test:0123456789abcdef",
        lock_key="identity:dingtalk:app-token-refresh:test:0123456789abcdef",
        wall_clock=clock.wall_clock,
        monotonic_clock=clock.monotonic_clock,
        sleep=clock.sleep,
        random_jitter=lambda _upper_bound: 0,
    )

    with pytest.raises(DingTalkUnavailableError):
        provider.get(lambda: FetchedDingTalkAppToken("must-not-be-used", 7_200))


def test_invalidate_handles_a_deadline_crossing_between_clock_reads() -> None:
    """受控失效在同样的调度边界下不得向 sleep 传递负数等待时间。"""

    redis = FakeTokenRedis(lock_available=False)
    clock = DeadlineCrossingClock()
    provider = DingTalkAppTokenProvider(
        redis=redis,
        cache_key="identity:dingtalk:app-token:test:0123456789abcdef",
        lock_key="identity:dingtalk:app-token-refresh:test:0123456789abcdef",
        wall_clock=clock.wall_clock,
        monotonic_clock=clock.monotonic_clock,
        sleep=clock.sleep,
        random_jitter=lambda _upper_bound: 0,
    )

    with pytest.raises(DingTalkUnavailableError):
        provider.invalidate()


def test_invalidate_reserves_each_lock_acquire_inside_three_second_wait() -> None:
    """锁未取得时，最后一次 acquire 及 sleep 都不得越过独立三秒窗口。"""

    clock = FakeClock(epoch=1_000.0)
    redis = AdvancingAcquireRedis(
        clock=clock,
        acquire_seconds=0.5,
        succeed_on_attempt=None,
    )
    provider = DingTalkAppTokenProvider(
        redis=redis,
        cache_key="identity:dingtalk:app-token:test:0123456789abcdef",
        lock_key="identity:dingtalk:app-token-refresh:test:0123456789abcdef",
        wall_clock=clock.wall_clock,
        monotonic_clock=clock.monotonic_clock,
        sleep=clock.sleep,
        random_jitter=lambda _upper_bound: 0,
        redis_io_timeout_seconds=0.5,
    )

    with pytest.raises(DingTalkUnavailableError):
        provider.invalidate()

    assert clock.monotonic_seconds == 3.0


def test_invalidate_excludes_delete_and_release_after_lock_is_acquired() -> None:
    """三秒只约束锁等待；取得锁后的条件删除和释放使用各自命令 deadline。"""

    clock = FakeClock(epoch=1_000.0)
    redis = AdvancingAcquireRedis(
        clock=clock,
        acquire_seconds=0.5,
        succeed_on_attempt=4,
        delete_seconds=0.4,
        release_seconds=0.4,
    )
    provider = DingTalkAppTokenProvider(
        redis=redis,
        cache_key="identity:dingtalk:app-token:test:0123456789abcdef",
        lock_key="identity:dingtalk:app-token-refresh:test:0123456789abcdef",
        wall_clock=clock.wall_clock,
        monotonic_clock=clock.monotonic_clock,
        sleep=clock.sleep,
        random_jitter=lambda _upper_bound: 0,
        redis_io_timeout_seconds=0.5,
    )
    redis.values[provider.cache_key] = "cached-value"

    provider.invalidate()

    assert redis.acquired_at is not None
    assert redis.acquired_at <= 3.0
    assert clock.monotonic_seconds > redis.acquired_at
    assert provider.cache_key not in redis.values


def test_waiter_does_not_delete_invalid_cache_without_the_refresh_lock() -> None:
    """未持锁等待者不得删除缓存，避免与刷新者写入新 Token 发生删除竞态。"""

    redis = FakeTokenRedis(lock_available=False)
    provider = build_provider(redis=redis, clock=FakeClock(epoch=1_000.0))
    redis.values[provider.cache_key] = '{"access_token": 123}'

    with pytest.raises(DingTalkUnavailableError):
        provider.get(lambda: FetchedDingTalkAppToken("must-not-be-used", 7_200))

    assert redis.deleted_keys == []


def test_invalid_cache_is_deleted_before_refresh() -> None:
    """无效缓存必须先删除，之后才允许刷新并返回新的 Token。"""

    redis = FakeTokenRedis()
    provider = build_provider(redis=redis, clock=FakeClock(epoch=1_000.0))
    redis.values[provider.cache_key] = '{"access_token": 123}'

    token = provider.get(lambda: FetchedDingTalkAppToken("fresh-token", 7_200))

    assert token == "fresh-token"
    assert provider.cache_key in redis.deleted_keys


def test_invalidate_losing_lock_before_delete_keeps_new_owner_cache_and_fails() -> None:
    """受控失效失去锁令牌时不得删除新 Token，且必须返回受控不可用错误。"""

    redis = ExpireBeforeDeleteRedis()
    provider = build_provider(redis=redis, clock=FakeClock(epoch=1_000.0))
    redis.values[provider.cache_key] = json.dumps(
        {"access_token": "old-token", "cached_at": 900.0, "expires_at": 7_000.0}
    )

    with pytest.raises(DingTalkUnavailableError):
        provider.invalidate()

    assert json.loads(redis.values[provider.cache_key])["access_token"] == "new-owner-token"


def test_invalid_cache_cleanup_losing_lock_returns_new_owner_cache_without_fetching() -> None:
    """持锁清理无效缓存时失锁后必须停止刷新并采用已写入的新有效缓存。"""

    redis = ExpireBeforeDeleteRedis()
    provider = build_provider(redis=redis, clock=FakeClock(epoch=1_000.0))
    redis.values[provider.cache_key] = '{"access_token": 123}'

    def forbidden_fetch() -> FetchedDingTalkAppToken:
        raise AssertionError("失锁后不得无锁刷新")

    assert provider.get(forbidden_fetch) == "new-owner-token"


def test_extremely_large_integer_cache_time_is_deleted_before_refresh() -> None:
    """无法安全转换为有限浮点数的超大 JSON 整数时间戳必须视为损坏缓存。"""

    redis = FakeTokenRedis()
    provider = build_provider(redis=redis, clock=FakeClock(epoch=1_000.0))
    redis.values[provider.cache_key] = json.dumps(
        {
            "access_token": "cached-token",
            "cached_at": 10**400,
            "expires_at": 7_000.0,
        }
    )

    assert provider.get(lambda: FetchedDingTalkAppToken("fresh-token", 7_200)) == "fresh-token"
    assert provider.cache_key in redis.deleted_keys


@pytest.mark.parametrize("invalid_time", [math.nan, math.inf, -math.inf])
def test_non_finite_cache_times_are_deleted_before_refresh(invalid_time: float) -> None:
    """NaN 和无穷大时间戳不是有效缓存时间，必须删除后刷新。"""

    redis = FakeTokenRedis()
    provider = build_provider(redis=redis, clock=FakeClock(epoch=1_000.0))
    redis.values[provider.cache_key] = json.dumps(
        {
            "access_token": "cached-token",
            "cached_at": invalid_time,
            "expires_at": 7_000.0,
        }
    )

    assert provider.get(lambda: FetchedDingTalkAppToken("fresh-token", 7_200)) == "fresh-token"
    assert provider.cache_key in redis.deleted_keys


def test_from_settings_uses_an_app_scoped_key_without_secret() -> None:
    """配置构造器必须按环境和应用隔离缓存键，且永不拼入 App Secret。"""

    settings = Settings.model_validate(
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

    provider = DingTalkAppTokenProvider.from_settings(
        redis=FakeTokenRedis(),
        settings=settings,
    )

    assert provider.cache_key.startswith("identity:dingtalk:app-token:test:")
    assert provider.lock_key.startswith("identity:dingtalk:app-token-refresh:test:")
    assert "test-app-secret" not in provider.cache_key
    assert "test-app-secret" not in provider.lock_key


def test_invalidate_deletes_the_cache_only_while_holding_the_refresh_lock() -> None:
    """受控失效操作使用同一刷新锁，只精确删除应用 Token 缓存键。"""

    redis = FakeTokenRedis()
    provider = build_provider(redis=redis, clock=FakeClock(epoch=1_000.0))
    redis.values[provider.cache_key] = "cached-value"

    provider.invalidate()

    assert provider.cache_key in redis.deleted_keys
    assert provider.cache_key not in redis.values
    assert redis.lock_held is False


def test_release_failure_does_not_mask_a_successful_refresh() -> None:
    """刷新写入成功后，锁释放的 Redis 故障不得把可用 Token 错误返回为失败。"""

    redis = ReleaseFailingRedis()
    provider = build_provider(redis=redis, clock=FakeClock(epoch=1_000.0))

    assert provider.get(lambda: FetchedDingTalkAppToken("fresh-token", 7_200)) == "fresh-token"


def test_release_failure_does_not_mask_the_fetcher_error() -> None:
    """刷新获取器失败时，锁释放故障不得覆盖原始失败原因。"""

    redis = ReleaseFailingRedis()
    provider = build_provider(redis=redis, clock=FakeClock(epoch=1_000.0))

    def failing_fetcher() -> FetchedDingTalkAppToken:
        raise RuntimeError("expected fetch failure")

    with pytest.raises(RuntimeError, match="expected fetch failure"):
        provider.get(failing_fetcher)
