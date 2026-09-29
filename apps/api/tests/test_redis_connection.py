from __future__ import annotations

import socket
import subprocess
import sys
import threading

from app.core import redis as redis_module
from app.core.settings import Settings

BASE_SETTINGS = {
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
}

_DEADLINE_PING_PROGRAM = """
import sys
import threading
import time
from redis import ConnectionPool
from redis.exceptions import TimeoutError
from app.core.redis import DeadlineConnection, DeadlineRedis

pool = ConnectionPool(
    connection_class=DeadlineConnection,
    host="127.0.0.1",
    port=int(sys.argv[1]),
    socket_connect_timeout=float(sys.argv[2]),
    socket_timeout=float(sys.argv[3]),
    decode_responses=True,
    retry=None,
    lib_name=None,
    lib_version=None,
)
client = DeadlineRedis(
    connection_pool=pool,
    operation_timeout_seconds=float(sys.argv[4]),
)
started_at = time.monotonic()
try:
    client.ping()
except TimeoutError:
    if time.monotonic() - started_at >= 0.75:
        raise SystemExit(4)
    if any(thread.name == "redis-response-watchdog" for thread in threading.enumerate()):
        raise SystemExit(3)
    raise SystemExit(0)
finally:
    client.close()
raise SystemExit(2)
"""

_DNS_GATE_PROGRAM = """
import socket
import threading
import time
from redis.exceptions import TimeoutError
from app.core import redis as redis_module

release = threading.Event()
resolver_started = threading.Event()
resolver_calls = 0

def controlled_getaddrinfo(*_args, **_kwargs):
    global resolver_calls
    resolver_calls += 1
    resolver_started.set()
    release.wait()
    return [
        (
            socket.AF_INET,
            socket.SOCK_STREAM,
            socket.IPPROTO_TCP,
            "",
            ("127.0.0.1", 6379),
        )
    ]

redis_module.socket.getaddrinfo = controlled_getaddrinfo
connection = redis_module.DeadlineConnection(
    host="blocked.invalid",
    port=6379,
    socket_connect_timeout=0.1,
    socket_timeout=0.1,
)
started_at = time.monotonic()
resolver_threads = []
try:
    try:
        connection.connect()
    except TimeoutError:
        pass
    else:
        raise AssertionError("blocking DNS unexpectedly completed")
    assert resolver_started.is_set()
    assert time.monotonic() - started_at < 0.5

    second_started_at = time.monotonic()
    try:
        redis_module._resolver_gate.resolve(
            "different.invalid",
            6379,
            0,
            0.1,
        )
    except TimeoutError:
        pass
    else:
        raise AssertionError("pending DNS unexpectedly allowed a different target")
    assert time.monotonic() - second_started_at < 0.05
    assert resolver_calls == 1
    resolver_threads = [
        thread for thread in threading.enumerate() if thread.name == "redis-dns-resolver"
    ]
    assert len(resolver_threads) == 1
    assert resolver_threads[0].daemon
finally:
    release.set()
    connection.disconnect()

resolver_threads[0].join(timeout=1)
assert not resolver_threads[0].is_alive()

addresses = redis_module._resolver_gate.resolve(
    "recovered.invalid",
    6379,
    0,
    0.1,
)
assert resolver_calls == 2
assert addresses[0][4] == ("127.0.0.1", 6379)

thread_deadline = time.monotonic() + 0.5
while (
    any(thread.name == "redis-dns-resolver" for thread in threading.enumerate())
    and time.monotonic() < thread_deadline
):
    time.sleep(0.01)
assert not any(
    thread.name == "redis-dns-resolver" for thread in threading.enumerate()
)
"""


def run_deadline_ping(
    port: int,
    *,
    connect_timeout_seconds: float,
    read_timeout_seconds: float,
    operation_timeout_seconds: float,
) -> subprocess.CompletedProcess[str]:
    """在有硬超时的子进程中执行 ping，回归时不会挂住 pytest 主进程。"""

    return subprocess.run(
        [
            sys.executable,
            "-c",
            _DEADLINE_PING_PROGRAM,
            str(port),
            str(connect_timeout_seconds),
            str(read_timeout_seconds),
            str(operation_timeout_seconds),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=1.5,
    )


def test_redis_client_uses_bounded_io_without_retries(monkeypatch) -> None:
    settings = Settings.model_validate(
        {
            **BASE_SETTINGS,
            "redis_connect_timeout_seconds": 0.25,
            "redis_read_timeout_seconds": 0.4,
        }
    )
    monkeypatch.setattr(redis_module, "get_settings", lambda: settings)
    redis_module.get_redis_client.cache_clear()

    client = redis_module.get_redis_client()

    try:
        connection_options = client.connection_pool.connection_kwargs
        assert connection_options["host"] == "127.0.0.1"
        assert connection_options["port"] == 6379
        assert connection_options["password"] == "not-a-real-redis-password"
        assert connection_options["decode_responses"] is True
        assert connection_options["socket_connect_timeout"] == 0.25
        assert connection_options["socket_timeout"] == 0.4
        assert connection_options["retry"].get_retries() == 0
        assert "retry_on_timeout" not in connection_options
        assert client.connection_pool.connection_class is redis_module.DeadlineConnection
        assert isinstance(client, redis_module.DeadlineRedis)
    finally:
        client.close()
        redis_module.get_redis_client.cache_clear()


def test_redis_read_timeout_is_enforced_against_loopback_blackhole(monkeypatch) -> None:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    listener.settimeout(0.05)
    stop = threading.Event()
    accepted = threading.Event()

    def blackhole() -> None:
        connection: socket.socket | None = None
        try:
            while not stop.is_set():
                try:
                    connection, _ = listener.accept()
                    accepted.set()
                    stop.wait()
                    return
                except TimeoutError:
                    continue
        finally:
            if connection is not None:
                connection.close()

    thread = threading.Thread(target=blackhole, name="redis-blackhole", daemon=True)
    thread.start()
    port = listener.getsockname()[1]
    settings = Settings.model_validate(
        {
            **BASE_SETTINGS,
            "redis_port": port,
            "redis_connect_timeout_seconds": 0.1,
            "redis_read_timeout_seconds": 0.1,
        }
    )
    try:
        result = run_deadline_ping(
            port,
            connect_timeout_seconds=settings.redis_connect_timeout_seconds,
            read_timeout_seconds=settings.redis_read_timeout_seconds,
            operation_timeout_seconds=settings.redis_read_timeout_seconds,
        )
        assert result.returncode == 0, result.stderr
        assert accepted.is_set()
    finally:
        stop.set()
        thread.join(timeout=1)
        listener.close()

    assert not thread.is_alive()


def test_complete_redis_response_has_a_wall_clock_deadline() -> None:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    listener.settimeout(0.05)
    stop = threading.Event()
    accepted = threading.Event()

    def drip_response() -> None:
        connection: socket.socket | None = None
        try:
            while not stop.is_set():
                try:
                    connection, _ = listener.accept()
                    accepted.set()
                    connection.recv(4096)
                    for byte in b"+PONG\r\n":
                        if stop.wait(0.04):
                            return
                        try:
                            connection.sendall(bytes((byte,)))
                        except OSError:
                            return
                    return
                except TimeoutError:
                    continue
        finally:
            if connection is not None:
                connection.close()

    thread = threading.Thread(target=drip_response, name="redis-drip", daemon=True)
    thread.start()
    port = listener.getsockname()[1]
    try:
        result = run_deadline_ping(
            port,
            connect_timeout_seconds=0.1,
            read_timeout_seconds=0.1,
            operation_timeout_seconds=0.2,
        )
        assert result.returncode == 0, result.stderr
        assert accepted.is_set()
    finally:
        stop.set()
        thread.join(timeout=1)
        listener.close()

    assert not thread.is_alive()


def test_dns_resolution_is_bounded_and_gate_recovers_without_thread_leaks() -> None:
    result = subprocess.run(
        [sys.executable, "-c", _DNS_GATE_PROGRAM],
        check=False,
        capture_output=True,
        text=True,
        timeout=1.5,
    )

    assert result.returncode == 0, result.stderr


def test_concurrent_dns_resolution_for_same_target_is_shared(monkeypatch) -> None:
    gate = redis_module._ResolverGate()
    resolver_started = threading.Event()
    release_resolver = threading.Event()
    resolver_calls = 0
    results: list[list[tuple[object, ...]]] = []
    errors: list[BaseException] = []

    def controlled_getaddrinfo(*_args, **_kwargs):
        nonlocal resolver_calls
        resolver_calls += 1
        resolver_started.set()
        release_resolver.wait(timeout=1)
        return [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("127.0.0.1", 6379),
            )
        ]

    def resolve() -> None:
        try:
            results.append(gate.resolve("redis", 6379, 0, 0.5))
        except BaseException as error:
            errors.append(error)

    monkeypatch.setattr(redis_module.socket, "getaddrinfo", controlled_getaddrinfo)
    first = threading.Thread(target=resolve, name="first-redis-caller")
    second = threading.Thread(target=resolve, name="second-redis-caller")

    first.start()
    assert resolver_started.wait(timeout=0.2)
    second.start()
    release_resolver.set()
    first.join(timeout=1)
    second.join(timeout=1)

    assert not first.is_alive()
    assert not second.is_alive()
    assert errors == []
    assert resolver_calls == 1
    assert len(results) == 2
    assert results[0] == results[1]
