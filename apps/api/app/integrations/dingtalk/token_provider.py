from __future__ import annotations

import hashlib
import json
import logging
import math
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final, Protocol, TypeGuard

from redis.exceptions import LockNotOwnedError, RedisError

from app.core.redis import redis_operation_timeout
from app.core.settings import Settings
from app.integrations.dingtalk.errors import DingTalkUnavailableError

TOKEN_REFRESH_LOCK_TTL_SECONDS: Final = 10
TOKEN_REFRESH_WAIT_SECONDS: Final = 3.0
TOKEN_REFRESH_POLL_SECONDS: Final = 0.15
TOKEN_REFRESH_POLL_JITTER_SECONDS: Final = 0.05
TOKEN_EXPIRY_SAFETY_SECONDS: Final = 300
TOKEN_EXPIRY_JITTER_MAX_SECONDS: Final = 30
_TOKEN_CACHE_EVENTS: Final = frozenset(
    {
        "hit",
        "miss",
        "refresh_success",
        "refresh_failed",
        "wait_timeout",
        "cache_invalid",
        "lock_expired",
        "invalidated",
    }
)

logger = logging.getLogger(__name__)

_CONDITIONAL_TOKEN_WRITE_SCRIPT: Final = """
local lock_token = redis.call('get', KEYS[1])
if not lock_token or lock_token ~= ARGV[1] then
    return 0
end
redis.call('set', KEYS[2], ARGV[2], 'EX', ARGV[3])
return 1
"""

_CONDITIONAL_TOKEN_DELETE_SCRIPT: Final = """
local lock_token = redis.call('get', KEYS[1])
if not lock_token or lock_token ~= ARGV[1] then
    return 0
end
redis.call('del', KEYS[2])
return 1
"""


class TokenRefreshLock(Protocol):
    """描述 Token 刷新互斥锁所需的非阻塞获取与安全释放接口。"""

    def acquire(self, *, blocking: bool = False) -> bool:
        """尝试非阻塞获取锁，成功时返回真值。"""

        ...

    def release(self) -> None:
        """释放当前请求拥有的锁；锁过期会由调用方安全处理。"""

        ...

    @property
    def local(self) -> TokenRefreshLockLocal:
        """返回 redis-py 为当前线程保存的锁令牌容器。"""

        ...


class TokenRefreshLockLocal(Protocol):
    """描述 redis-py 刷新锁在当前线程保存的令牌字段。"""

    token: str | bytes | None


class TokenRedisClient(Protocol):
    """描述应用 Token 缓存所需的最小文本 Redis 操作，不暴露业务会话。"""

    def get(self, key: str) -> str | None:
        """读取文本缓存值。"""

        ...

    def set(self, key: str, value: str, *, ex: int) -> bool:
        """以正数过期秒数写入文本缓存值。"""

        ...

    def delete(self, key: str) -> int:
        """删除单个缓存键并返回删除数量。"""

        ...

    def lock(self, key: str, *, timeout: int, blocking: bool) -> TokenRefreshLock:
        """创建具有超时的非阻塞分布式锁。"""

        ...

    def eval(self, script: str, numkeys: int, *keys_and_args: object) -> int:
        """执行 Redis 原子脚本，并返回条件写入是否成功。"""

        ...


@dataclass(frozen=True)
class FetchedDingTalkAppToken:
    """保存钉钉刚返回的应用 Token 和声明有效期，不负责持久化。"""

    access_token: str
    expires_in_seconds: int


class DingTalkAppTokenProvider:
    """通过 Redis 缓存和单刷新者锁协调钉钉应用 Token，绝不记录 Token 内容。"""

    def __init__(
        self,
        *,
        redis: TokenRedisClient,
        cache_key: str,
        lock_key: str,
        wall_clock: Callable[[], float] = time.time,
        monotonic_clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        random_jitter: Callable[[int], int] = secrets.randbelow,
        redis_io_timeout_seconds: float = 0.5,
    ) -> None:
        """绑定文本 Redis、无 Secret 的键和可注入时钟，供安全缓存协调使用。"""

        self._redis = redis
        self.cache_key = cache_key
        self.lock_key = lock_key
        self._wall_clock = wall_clock
        self._monotonic_clock = monotonic_clock
        self._sleep = sleep
        self._random_jitter = random_jitter
        self._redis_io_timeout_seconds = redis_io_timeout_seconds

    @classmethod
    def from_settings(
        cls,
        *,
        redis: TokenRedisClient,
        settings: Settings,
        wall_clock: Callable[[], float] = time.time,
        monotonic_clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        random_jitter: Callable[[int], int] = secrets.randbelow,
    ) -> DingTalkAppTokenProvider:
        """从受控配置生成不含 Secret 的缓存命名空间。"""

        identity = f"{settings.app_env}:{settings.dingtalk_corp_id}:{settings.dingtalk_app_key}"
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
        return cls(
            redis=redis,
            cache_key=f"identity:dingtalk:app-token:{settings.app_env}:{digest}",
            lock_key=f"identity:dingtalk:app-token-refresh:{settings.app_env}:{digest}",
            wall_clock=wall_clock,
            monotonic_clock=monotonic_clock,
            sleep=sleep,
            random_jitter=random_jitter,
            redis_io_timeout_seconds=max(
                settings.redis_connect_timeout_seconds,
                settings.redis_read_timeout_seconds,
            ),
        )

    def get(self, fetcher: Callable[[], FetchedDingTalkAppToken]) -> str:
        """读取有效缓存；未命中时仅允许一个持锁请求刷新。"""

        cached_token = self._read_valid_cache(delete_invalid=False)
        if cached_token is not None:
            self._log_event("hit")
            return cached_token

        lock = self._create_refresh_lock()
        if self._acquire_lock(lock):
            try:
                cached_token = self._read_valid_cache(lock=lock)
                if cached_token is not None:
                    return cached_token
                self._log_event("miss")
                try:
                    fetched = fetcher()
                    token = self._write_fetched_token(fetched, lock=lock)
                except Exception:
                    self._log_event("refresh_failed")
                    raise
                self._log_event("refresh_success")
                return token
            finally:
                self._release_lock(lock)

        deadline = self._monotonic_clock() + TOKEN_REFRESH_WAIT_SECONDS
        while self._monotonic_clock() < deadline:
            jitter = (self._random_jitter(101) - 50) / 1000
            poll_seconds = max(0.1, TOKEN_REFRESH_POLL_SECONDS + jitter)
            remaining_seconds = deadline - self._monotonic_clock()
            if remaining_seconds < self._redis_io_timeout_seconds:
                if remaining_seconds > 0:
                    self._sleep(remaining_seconds)
                break
            sleep_seconds = min(
                poll_seconds,
                remaining_seconds - self._redis_io_timeout_seconds,
            )
            if sleep_seconds > 0:
                self._sleep(sleep_seconds)
            remaining_seconds = deadline - self._monotonic_clock()
            if remaining_seconds < self._redis_io_timeout_seconds:
                break
            with redis_operation_timeout(min(self._redis_io_timeout_seconds, remaining_seconds)):
                cached_token = self._read_valid_cache(delete_invalid=False)
            if cached_token is not None:
                return cached_token
            remaining_seconds = deadline - self._monotonic_clock()
            if remaining_seconds <= self._redis_io_timeout_seconds:
                if remaining_seconds > 0:
                    self._sleep(remaining_seconds)
                break
        self._log_event("wait_timeout", duration_ms=TOKEN_REFRESH_WAIT_SECONDS * 1000)
        raise DingTalkUnavailableError("钉钉身份服务暂时繁忙。")

    def invalidate(self) -> None:
        """在持有同一刷新锁时精确删除应用 Token，不影响教师会话。"""

        deadline = self._monotonic_clock() + TOKEN_REFRESH_WAIT_SECONDS
        while self._monotonic_clock() < deadline:
            remaining_seconds = deadline - self._monotonic_clock()
            if remaining_seconds < self._redis_io_timeout_seconds:
                if remaining_seconds > 0:
                    self._sleep(remaining_seconds)
                break
            lock = self._create_refresh_lock()
            with redis_operation_timeout(min(self._redis_io_timeout_seconds, remaining_seconds)):
                acquired = self._acquire_lock(lock)
            if acquired:
                try:
                    if not self._delete_cache(lock=lock):
                        raise DingTalkUnavailableError("钉钉应用 Token 正在刷新，请稍后重试。")
                    self._log_event("invalidated")
                    return
                finally:
                    self._release_lock(lock)
            remaining_seconds = deadline - self._monotonic_clock()
            if remaining_seconds <= self._redis_io_timeout_seconds:
                if remaining_seconds > 0:
                    self._sleep(remaining_seconds)
                break
            self._sleep(
                min(
                    TOKEN_REFRESH_POLL_SECONDS,
                    remaining_seconds - self._redis_io_timeout_seconds,
                )
            )
        raise DingTalkUnavailableError("钉钉应用 Token 正在刷新，请稍后重试。")

    def _read_valid_cache(
        self,
        *,
        delete_invalid: bool = True,
        lock: TokenRefreshLock | None = None,
    ) -> str | None:
        """读取并校验缓存 JSON；仅持锁刷新者删除无效值，避免并发误删新 Token。"""

        try:
            raw_value = self._redis.get(self.cache_key)
        except RedisError:
            raise DingTalkUnavailableError("钉钉身份服务暂时繁忙。") from None
        if raw_value is None:
            return None

        try:
            payload = json.loads(raw_value)
        except (TypeError, ValueError):
            return self._handle_invalid_cache(delete_invalid, lock=lock)
        if not isinstance(payload, dict):
            return self._handle_invalid_cache(delete_invalid, lock=lock)

        access_token = payload.get("access_token")
        cached_at = payload.get("cached_at")
        expires_at = payload.get("expires_at")
        if (
            not isinstance(access_token, str)
            or not access_token
            or not self._is_number(cached_at)
            or not self._is_number(expires_at)
            or expires_at <= self._wall_clock()
        ):
            return self._handle_invalid_cache(delete_invalid, lock=lock)
        return access_token

    def _write_fetched_token(
        self,
        fetched: FetchedDingTalkAppToken,
        *,
        lock: TokenRefreshLock,
    ) -> str:
        """校验钉钉返回值，并仅在当前 Redis 锁令牌仍有效时原子写入缓存。"""

        if (
            not isinstance(fetched.access_token, str)
            or not fetched.access_token
            or not isinstance(fetched.expires_in_seconds, int)
            or isinstance(fetched.expires_in_seconds, bool)
            or fetched.expires_in_seconds <= TOKEN_EXPIRY_SAFETY_SECONDS + 30
        ):
            raise DingTalkUnavailableError("钉钉身份服务返回异常。")

        effective_ttl = (
            fetched.expires_in_seconds
            - TOKEN_EXPIRY_SAFETY_SECONDS
            - self._random_jitter(TOKEN_EXPIRY_JITTER_MAX_SECONDS + 1)
        )
        cached_at = self._wall_clock()
        payload = json.dumps(
            {
                "access_token": fetched.access_token,
                "cached_at": cached_at,
                "expires_at": cached_at + effective_ttl,
            }
        )
        lock_token = self._get_lock_token(lock)
        try:
            written = self._redis.eval(
                _CONDITIONAL_TOKEN_WRITE_SCRIPT,
                2,
                self.lock_key,
                self.cache_key,
                lock_token,
                payload,
                effective_ttl,
            )
        except RedisError:
            raise DingTalkUnavailableError("钉钉身份服务暂时繁忙。") from None
        if written != 1:
            cached_token = self._read_valid_cache(delete_invalid=False)
            if cached_token is not None:
                return cached_token
            raise DingTalkUnavailableError("钉钉身份服务暂时繁忙。")
        return fetched.access_token

    def _create_refresh_lock(self) -> TokenRefreshLock:
        """创建固定十秒、非阻塞的 Redis 刷新锁，并安全映射协调层故障。"""

        try:
            return self._redis.lock(
                self.lock_key,
                timeout=TOKEN_REFRESH_LOCK_TTL_SECONDS,
                blocking=False,
            )
        except RedisError:
            raise DingTalkUnavailableError("钉钉身份服务暂时繁忙。") from None

    @staticmethod
    def _is_number(value: object) -> TypeGuard[int | float]:
        """识别可用于缓存时间比较的数值，同时拒绝 Python 布尔值。"""

        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return False
        try:
            return math.isfinite(value)
        except OverflowError:
            return False

    @staticmethod
    def _get_lock_token(lock: TokenRefreshLock) -> str | bytes:
        """读取 redis-py 当前线程锁令牌；缺失时拒绝执行无条件缓存写入。"""

        lock_token = lock.local.token
        if not isinstance(lock_token, (str, bytes)) or not lock_token:
            raise DingTalkUnavailableError("钉钉身份服务暂时繁忙。")
        return lock_token

    def _acquire_lock(self, lock: TokenRefreshLock) -> bool:
        """非阻塞获取刷新锁，并将 Redis 协调异常映射为安全不可用错误。"""

        try:
            return lock.acquire(blocking=False)
        except RedisError:
            raise DingTalkUnavailableError("钉钉身份服务暂时繁忙。") from None

    def _release_lock(self, lock: TokenRefreshLock) -> None:
        """释放当前刷新锁；锁已过期时只记录安全事件，不覆盖业务结果。"""

        try:
            lock.release()
        except (LockNotOwnedError, RedisError):
            self._log_event("lock_expired")

    def _handle_invalid_cache(
        self,
        delete_invalid: bool,
        *,
        lock: TokenRefreshLock | None,
    ) -> str | None:
        """仅持锁者原子删除无效缓存；失锁时只读取新所有者缓存或受控失败。"""

        if not delete_invalid:
            return None
        if lock is None:
            raise DingTalkUnavailableError("钉钉身份服务暂时繁忙。")
        if self._delete_invalid_cache(lock=lock):
            return None
        cached_token = self._read_valid_cache(delete_invalid=False)
        if cached_token is not None:
            return cached_token
        raise DingTalkUnavailableError("钉钉身份服务暂时繁忙。")

    def _delete_invalid_cache(self, *, lock: TokenRefreshLock) -> bool:
        """以当前锁令牌条件删除无效缓存；失锁时不删除也不记录成功事件。"""

        deleted = self._delete_cache(lock=lock)
        if deleted:
            self._log_event("cache_invalid")
        return deleted

    def _delete_cache(self, *, lock: TokenRefreshLock) -> bool:
        """仅在 Redis 当前锁令牌匹配时原子删除应用 Token 缓存键。"""

        lock_token = self._get_lock_token(lock)
        try:
            deleted = self._redis.eval(
                _CONDITIONAL_TOKEN_DELETE_SCRIPT,
                2,
                self.lock_key,
                self.cache_key,
                lock_token,
            )
        except RedisError:
            raise DingTalkUnavailableError("钉钉身份服务暂时繁忙。") from None
        return deleted == 1

    @staticmethod
    def _log_event(event: str, *, duration_ms: float | None = None) -> None:
        """输出固定字段的脱敏缓存事件，拒绝记录缓存键、Token、App Key 或异常内容。"""

        if event not in _TOKEN_CACHE_EVENTS:
            raise ValueError("不允许记录未定义的钉钉 Token 缓存事件。")
        if duration_ms is None:
            logger.info("dingtalk_token_cache event=%s", event)
            return
        logger.info("dingtalk_token_cache event=%s duration_ms=%d", event, round(duration_ms))
