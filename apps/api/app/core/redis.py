from __future__ import annotations

import math
import socket
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

from redis import ConnectionPool, Redis
from redis.backoff import NoBackoff
from redis.connection import Connection
from redis.exceptions import TimeoutError as RedisTimeoutError
from redis.retry import Retry

from app.core.settings import get_settings

_operation_deadline: ContextVar[float | None] = ContextVar(
    "redis_operation_deadline",
    default=None,
)


@contextmanager
def redis_operation_timeout(timeout_seconds: float):
    """把当前同步 Redis 调用限制在绝对单调时钟截止时间内。"""

    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise RedisTimeoutError("Redis operation deadline exhausted")
    now = time.monotonic()
    requested_deadline = now + timeout_seconds
    current_deadline = _operation_deadline.get()
    deadline = (
        requested_deadline
        if current_deadline is None
        else min(current_deadline, requested_deadline)
    )
    token = _operation_deadline.set(deadline)
    try:
        yield
    finally:
        _operation_deadline.reset(token)


def _remaining_timeout(configured_timeout: float | None) -> float:
    """返回配置与当前命令绝对截止时间中的较小剩余值。"""

    deadline = _operation_deadline.get()
    remaining = None if deadline is None else deadline - time.monotonic()
    if remaining is not None and remaining <= 0:
        raise RedisTimeoutError("Redis operation deadline exceeded")
    if configured_timeout is None:
        if remaining is None:
            raise RedisTimeoutError("Redis operation has no finite deadline")
        return remaining
    if remaining is None:
        return configured_timeout
    return min(configured_timeout, remaining)


@dataclass
class _ResolutionJob:
    """保存唯一在途 DNS 解析及其目标，超时调用方不会取消 worker。"""

    key: tuple[str, int, int]
    event: threading.Event = field(default_factory=threading.Event)
    result: list[tuple[Any, ...]] | None = None
    error: BaseException | None = None


class _ResolverGate:
    """用最多一个 daemon worker 为同步 getaddrinfo 提供失败关闭边界。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._inflight: _ResolutionJob | None = None

    def resolve(
        self,
        host: str,
        port: int,
        socket_type: int,
        timeout_seconds: float,
    ) -> list[tuple[Any, ...]]:
        """在给定预算内解析；同目标调用共享结果，异目标调用失败关闭。"""

        key = (host, port, socket_type)
        should_start_worker = False
        with self._lock:
            job = self._inflight
            if job is not None and not job.event.is_set():
                if job.key != key:
                    raise RedisTimeoutError(
                        "Redis DNS resolution for another target is already pending"
                    )
            else:
                job = _ResolutionJob(key=key)
                self._inflight = job
                should_start_worker = True

        if should_start_worker:
            worker = threading.Thread(
                target=self._resolve,
                args=(job, host, port, socket_type),
                name="redis-dns-resolver",
                daemon=True,
            )
            worker.start()
        if not job.event.wait(timeout_seconds):
            raise RedisTimeoutError("Redis DNS resolution deadline exceeded")
        if job.error is not None:
            raise job.error
        if job.result is None:
            raise OSError("socket.getaddrinfo returned an empty list")
        return list(job.result)

    def _resolve(
        self,
        job: _ResolutionJob,
        host: str,
        port: int,
        socket_type: int,
    ) -> None:
        try:
            job.result = list(socket.getaddrinfo(host, port, socket_type, socket.SOCK_STREAM))
        except BaseException as error:
            job.error = error
        finally:
            job.event.set()
            with self._lock:
                if self._inflight is job:
                    self._inflight = None


_resolver_gate = _ResolverGate()


class DeadlineConnection(Connection):
    """为 DNS、连接和完整 RESP 读取执行绝对墙钟截止时间。"""

    def _connect(self):
        resolution_timeout = _remaining_timeout(self.socket_connect_timeout)
        addresses = _resolver_gate.resolve(
            self.host,
            self.port,
            self.socket_type,
            resolution_timeout,
        )
        last_error: OSError | None = None
        for address in addresses:
            family, socktype, proto, _canonname, socket_address = address
            connection_socket: socket.socket | None = None
            try:
                connection_socket = socket.socket(family, socktype, proto)
                connection_socket.setsockopt(
                    socket.IPPROTO_TCP,
                    socket.TCP_NODELAY,
                    1,
                )
                if self.socket_keepalive:
                    connection_socket.setsockopt(
                        socket.SOL_SOCKET,
                        socket.SO_KEEPALIVE,
                        1,
                    )
                    for key, value in self.socket_keepalive_options.items():
                        connection_socket.setsockopt(socket.IPPROTO_TCP, key, value)
                connection_socket.settimeout(_remaining_timeout(self.socket_connect_timeout))
                connection_socket.connect(socket_address)
                connection_socket.settimeout(self.socket_timeout)
                return connection_socket
            except OSError as error:
                last_error = error
                if connection_socket is not None:
                    try:
                        connection_socket.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                    connection_socket.close()
        if last_error is not None:
            raise last_error
        raise OSError("socket.getaddrinfo returned an empty list")

    def read_response(self, *args, **kwargs):
        """在完整 RESP 解析超时后断开当前连接，并等待 watchdog 完全退出。"""

        timeout_seconds = _remaining_timeout(self.socket_timeout)
        expired = threading.Event()

        def expire_connection() -> None:
            expired.set()
            try:
                self.disconnect()
            except BaseException:
                pass

        watchdog = threading.Timer(timeout_seconds, expire_connection)
        watchdog.name = "redis-response-watchdog"
        watchdog.daemon = True
        watchdog.start()
        try:
            response = super().read_response(*args, **kwargs)
        except BaseException:
            if expired.is_set():
                raise RedisTimeoutError("Redis response deadline exceeded") from None
            raise
        finally:
            watchdog.cancel()
            watchdog.join()
        if expired.is_set():
            self.disconnect()
            raise RedisTimeoutError("Redis response deadline exceeded")
        return response


class DeadlineRedis(Redis):
    """在每条同步 Redis 命令外建立共享的绝对操作预算。"""

    def __init__(
        self,
        *args,
        operation_timeout_seconds: float,
        **kwargs,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._operation_timeout_seconds = operation_timeout_seconds

    def execute_command(self, *args, **options):
        with redis_operation_timeout(self._operation_timeout_seconds):
            return super().execute_command(*args, **options)


def create_redis_client(
    redis_url: str,
    *,
    connect_timeout_seconds: float,
    read_timeout_seconds: float,
) -> DeadlineRedis:
    """创建完整命令有界、零重试的 Redis 文本客户端。"""

    connection_pool = ConnectionPool.from_url(
        redis_url,
        connection_class=DeadlineConnection,
        decode_responses=True,
        socket_connect_timeout=connect_timeout_seconds,
        socket_timeout=read_timeout_seconds,
        retry=Retry(NoBackoff(), 0),
    )
    client = DeadlineRedis(
        connection_pool=connection_pool,
        operation_timeout_seconds=max(
            connect_timeout_seconds,
            read_timeout_seconds,
        ),
    )
    client.auto_close_connection_pool = True
    return client


@lru_cache
def get_redis_client() -> DeadlineRedis:
    """返回 DNS、连接、完整响应均有界且零重试的复用 Redis 客户端。"""

    settings = get_settings()
    return create_redis_client(
        settings.redis_url,
        connect_timeout_seconds=settings.redis_connect_timeout_seconds,
        read_timeout_seconds=settings.redis_read_timeout_seconds,
    )
