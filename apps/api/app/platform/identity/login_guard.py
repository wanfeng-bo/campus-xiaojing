from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol

from redis.exceptions import RedisError

if TYPE_CHECKING:
    from app.core.settings import Settings


logger = logging.getLogger(__name__)

LOGIN_RATE_TTL_SECONDS = 120
LOGIN_CONCURRENCY_LEASE_SECONDS = 15
LOGIN_CONCURRENCY_KEY_TTL_SECONDS = 30

_ACQUIRE_LOGIN_GUARD_SCRIPT = """
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


class LoginGuardRedisClient(Protocol):
    """定义登录门所需的 Lua 执行和单请求租约释放 Redis 操作。"""

    def eval(self, script: str, numkeys: int, *keys_and_args: str) -> object:
        """在 Redis 内原子执行维护、速率与并发门脚本。"""

    def zrem(self, name: str, *values: str) -> object:
        """删除指定请求的有序集合并发租约。"""


class DingTalkLoginMaintenanceError(Exception):
    """表示受控维护租约仍有效，新的钉钉身份交换必须停止。"""


class DingTalkLoginRateLimitError(Exception):
    """表示全局分钟速率或同时处理请求数已达到安全上限。"""


class DingTalkLoginGuardUnavailableError(Exception):
    """表示 Redis 登录保护无法安全判定，身份交换必须失败关闭。"""


class DingTalkLoginGuard:
    """通过 Redis Lua 原子限制钉钉新登录，不读取客户端身份或影响既有会话。"""

    def __init__(
        self,
        *,
        redis: LoginGuardRedisClient,
        app_env: str,
        rate_limit: int,
        concurrency_limit: int,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        """绑定环境隔离键、受控容量阈值和 UTC 时钟，不保存请求或身份资料。"""

        self._redis = redis
        self._app_env = app_env
        self._rate_limit = rate_limit
        self._concurrency_limit = concurrency_limit
        self._now = now or (lambda: datetime.now(UTC))
        self._maintenance_key = f"identity:dingtalk:login:maintenance:{app_env}"
        self._concurrency_key = f"identity:dingtalk:login:concurrency:{app_env}"

    @classmethod
    def from_settings(
        cls, *, redis: LoginGuardRedisClient, settings: Settings
    ) -> DingTalkLoginGuard:
        """从受控应用配置创建跨实例共享的登录保护，不使用客户端自报信息。"""

        return cls(
            redis=redis,
            app_env=settings.app_env,
            rate_limit=settings.dingtalk_login_global_rate_per_minute,
            concurrency_limit=settings.dingtalk_login_concurrency_limit,
        )

    @contextmanager
    def admit(self, request_id: str) -> Iterator[None]:
        """原子取得登录容量租约，并确保任何下游异常都会释放当前请求。"""

        result = self._acquire(request_id)
        if result == "maintenance":
            raise DingTalkLoginMaintenanceError
        if result in {"rate_limited", "concurrency_limited"}:
            raise DingTalkLoginRateLimitError
        try:
            yield
        finally:
            try:
                self._redis.zrem(self._concurrency_key, request_id)
            except RedisError:
                logger.warning("钉钉登录并发租约释放失败，将由短租约自动清理")

    def _acquire(self, request_id: str) -> str:
        """在单次 Lua 调用中检查维护、递增速率并创建短期并发租约。"""

        now = self._now().astimezone(UTC)
        rate_key = f"identity:dingtalk:login:rate:{self._app_env}:{now.strftime('%Y%m%d%H%M')}"
        try:
            response = self._redis.eval(
                _ACQUIRE_LOGIN_GUARD_SCRIPT,
                3,
                self._maintenance_key,
                rate_key,
                self._concurrency_key,
                str(self._rate_limit),
                str(self._concurrency_limit),
                str(int(now.timestamp())),
                request_id,
                str(LOGIN_RATE_TTL_SECONDS),
                str(LOGIN_CONCURRENCY_LEASE_SECONDS),
                str(LOGIN_CONCURRENCY_KEY_TTL_SECONDS),
            )
        except RedisError:
            raise DingTalkLoginGuardUnavailableError from None
        result, rate_count, concurrency_count = self._read_response(response)
        logger.info(
            "dingtalk_login_guard result=%s rate_count=%s concurrency_count=%s",
            result,
            rate_count,
            concurrency_count,
        )
        return result

    @staticmethod
    def _read_response(response: object) -> tuple[str, int, int]:
        """严格校验 redis-py Lua 返回值，损坏响应一律按保护不可用处理。"""

        if (
            not isinstance(response, (list, tuple))
            or len(response) != 3
            or not isinstance(response[0], str)
            or response[0] not in {"maintenance", "rate_limited", "concurrency_limited", "allowed"}
            or not isinstance(response[1], int)
            or not isinstance(response[2], int)
            or response[1] < 0
            or response[2] < 0
        ):
            raise DingTalkLoginGuardUnavailableError
        return response[0], response[1], response[2]
