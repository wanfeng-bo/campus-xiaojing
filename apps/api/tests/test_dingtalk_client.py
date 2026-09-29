from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Event, Thread
from time import perf_counter, sleep
from typing import cast

import httpcore
import httpx
import pytest

from app.core.settings import Settings
from app.integrations.dingtalk import client as dingtalk_client
from app.integrations.dingtalk.client import (
    DingTalkCredentialError,
    DingTalkIdentity,
    DingTalkIdentityVerifier,
    DingTalkUnavailableError,
)
from app.integrations.dingtalk.token_provider import (
    DingTalkAppTokenProvider,
    FetchedDingTalkAppToken,
)

API_ROOT = Path(__file__).resolve().parents[1]

_BLOCKING_DNS_PROGRAM = r"""
import json
import os
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from app.core.settings import Settings
from app.integrations.dingtalk import client as dingtalk_client
from app.integrations.dingtalk.client import (
    DingTalkIdentity,
    DingTalkIdentityVerifier,
    DingTalkUnavailableError,
)
from app.integrations.dingtalk.token_provider import FetchedDingTalkAppToken


hard_watchdog = threading.Timer(6.0, lambda: os._exit(124))
hard_watchdog.daemon = True
hard_watchdog.start()


class TokenProvider:
    def get(self, fetcher):
        return fetcher().access_token


class Handler(BaseHTTPRequestHandler):
    def _send_json(self, payload):
        body = json.dumps(payload, separators=(",", ":")).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._send_json(
            {
                "errcode": 0,
                "access_token": "temporary-token",
                "expires_in": 7200,
            }
        )

    def do_POST(self):
        content_length = int(self.headers.get("Content-Length", "0"))
        if content_length:
            self.rfile.read(content_length)
        self._send_json(
            {
                "errcode": 0,
                "result": {"userid": "manager-001", "name": "测试管理员"},
            }
        )

    def log_message(self, format, *args):
        return


server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
server.daemon_threads = False
server.block_on_close = True
server_thread = threading.Thread(
    target=server.serve_forever,
    name="dingtalk-dns-test-server",
    daemon=True,
)
server_thread.start()
host, port = server.server_address

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
        "dingtalk_identity_timeout_seconds": 1,
    }
)
verifier = DingTalkIdentityVerifier(
    settings=settings,
    token_provider=TokenProvider(),
)
dingtalk_client.DINGTALK_ACCESS_TOKEN_URL = f"http://blocked.test:{port}/gettoken"
dingtalk_client.DINGTALK_USER_INFO_URL = (
    f"http://blocked.test:{port}/topapi/v2/user/getuserinfo"
)

original_getaddrinfo = socket.getaddrinfo
resolver_started = threading.Event()
release_resolver = threading.Event()
shared_resolver_started = threading.Event()
release_shared_resolver = threading.Event()
resolver_calls = 0
resolver_phase = "shared"


def controlled_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    global resolver_calls
    resolver_calls += 1
    if resolver_phase == "shared":
        shared_resolver_started.set()
        release_shared_resolver.wait()
    elif resolver_phase == "blocked":
        resolver_started.set()
        release_resolver.wait()
    return [
        (
            socket.AF_INET,
            socket.SOCK_STREAM,
            socket.IPPROTO_TCP,
            "",
            ("127.0.0.1", port),
        )
    ]


socket.getaddrinfo = controlled_getaddrinfo
shared_results = []
shared_callers_in_gate = threading.Event()
shared_gate_callers = set()


class ObservedLock:
    def __init__(self, lock):
        self._lock = lock

    def __enter__(self):
        self._lock.acquire()
        thread_name = threading.current_thread().name
        if thread_name.startswith("dns-sharer-"):
            shared_gate_callers.add(thread_name)
            if len(shared_gate_callers) == 2:
                shared_callers_in_gate.set()
        return self

    def __exit__(self, _exception_type, _exception, _traceback):
        self._lock.release()


dingtalk_client._dns_resolver_gate._lock = ObservedLock(
    dingtalk_client._dns_resolver_gate._lock
)


def resolve_shared():
    shared_results.append(
        dingtalk_client._dns_resolver_gate.resolve("shared.test", port, 0.5)
    )


shared_threads = [
    threading.Thread(target=resolve_shared, name=f"dns-sharer-{index}")
    for index in range(2)
]
shared_threads[0].start()
assert shared_resolver_started.wait(timeout=0.2)
shared_threads[1].start()
assert shared_callers_in_gate.wait(timeout=0.5)
assert resolver_calls == 1
release_shared_resolver.set()
for shared_thread in shared_threads:
    shared_thread.join(timeout=1)
    assert not shared_thread.is_alive()
assert len(shared_results) == 2
assert resolver_calls == 1
assert not [
    thread
    for thread in threading.enumerate()
    if thread.name == "dingtalk-dns-resolver"
]

resolver_phase = "blocked"
release_timer = threading.Timer(2.0, release_resolver.set)
release_timer.daemon = True
release_timer.start()

try:
    started_at = time.monotonic()
    try:
        verifier.verify("one-time-code")
    except DingTalkUnavailableError:
        pass
    else:
        raise AssertionError("blocking DNS unexpectedly completed")
    assert time.monotonic() - started_at < 1.5
    assert resolver_started.is_set()

    for _ in range(3):
        retry_started_at = time.monotonic()
        try:
            verifier.verify("one-time-code")
        except DingTalkUnavailableError:
            pass
        else:
            raise AssertionError("pending DNS unexpectedly allowed another request")
        assert time.monotonic() - retry_started_at < 0.3

    resolver_threads = [
        thread
        for thread in threading.enumerate()
        if thread.name == "dingtalk-dns-resolver"
    ]
    assert resolver_calls == 2
    assert len(resolver_threads) == 1
    assert resolver_threads[0].daemon
    assert not [
        thread
        for thread in threading.enumerate()
        if thread.name.startswith("asyncio_")
    ]

    release_resolver.set()
    resolver_threads[0].join(timeout=1)
    assert not resolver_threads[0].is_alive()

    recovery_verifier = DingTalkIdentityVerifier(
        settings=settings.model_copy(
            update={"dingtalk_identity_timeout_seconds": 5},
        ),
        token_provider=TokenProvider(),
    )
    assert recovery_verifier.verify("one-time-code") == DingTalkIdentity(
        corporation_id="ding-test-corp",
        personnel_id="manager-001",
        display_name="测试管理员",
    )
    assert resolver_calls == 4
    assert not [
        thread
        for thread in threading.enumerate()
        if thread.name in {"dingtalk-dns-resolver"} or thread.name.startswith("asyncio_")
    ]
finally:
    release_shared_resolver.set()
    release_resolver.set()
    release_timer.cancel()
    release_timer.join(timeout=1)
    socket.getaddrinfo = original_getaddrinfo
    server.shutdown()
    server.server_close()
    server_thread.join(timeout=1)
    assert not server_thread.is_alive()
    hard_watchdog.cancel()
    hard_watchdog.join(timeout=1)
"""


class SlowDingTalkHandler(BaseHTTPRequestHandler):
    """在本机持续返回小片段，复现分阶段 inactivity timeout 无法约束总耗时。"""

    request_completed = Event()

    def do_GET(self) -> None:
        """以短于读取超时的间隔滴流合法 Token 响应。"""

        body = json.dumps(
            {
                "errcode": 0,
                "access_token": "temporary-token",
                "expires_in": 7_200,
            },
            separators=(",", ":"),
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            for byte in body:
                self.wfile.write(bytes((byte,)))
                self.wfile.flush()
                sleep(0.03)
        except (BrokenPipeError, ConnectionResetError):
            return
        finally:
            self.request_completed.set()

    def do_POST(self) -> None:
        """在本机立即返回合法用户资料，确保测试绝不访问真实钉钉地址。"""

        body = json.dumps(
            {
                "errcode": 0,
                "result": {"userid": "manager-001", "name": "测试管理员"},
            },
            separators=(",", ":"),
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        """关闭测试服务器访问日志，避免把无关本机信息写入测试输出。"""


@contextmanager
def run_slow_dingtalk_server() -> Iterator[str]:
    """启动并可靠回收本地慢速 HTTP 服务，不遗留测试工作线程。"""

    SlowDingTalkHandler.request_completed.clear()
    server = ThreadingHTTPServer(("127.0.0.1", 0), SlowDingTalkHandler)
    server.daemon_threads = False
    server.block_on_close = True
    server_thread = Thread(
        target=server.serve_forever,
        name="test-dingtalk-slow-loopback",
    )
    server_thread.start()
    try:
        host, port = cast(tuple[str, int], server.server_address)
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=2)
        assert not server_thread.is_alive()
        assert SlowDingTalkHandler.request_completed.wait(timeout=0.1)


class FakeTokenProvider:
    """执行测试传入的获取器并隔离真实 Redis Token 缓存。"""

    def __init__(self) -> None:
        """初始化获取次数记录，供适配器调用边界断言使用。"""

        self.fetch_count = 0

    def get(self, fetcher: Callable[[], FetchedDingTalkAppToken]) -> str:
        """调用客户端受测获取器并仅返回其规范化应用 Token。"""

        self.fetch_count += 1
        fetched = fetcher()
        assert isinstance(fetched, FetchedDingTalkAppToken)
        return fetched.access_token


class CachedTokenProvider:
    """直接返回已缓存的测试 Token，使电话测试只观察一次用户详情请求。"""

    def get(self, _fetcher: Callable[[], FetchedDingTalkAppToken]) -> str:
        return "temporary-token"


def build_settings(**overrides: object) -> Settings:
    """构造仅用于适配器测试的钉钉运行配置，不读取真实环境密钥。"""

    values = {
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
    values.update(overrides)
    return Settings.model_validate(values)


def build_verifier(handler: httpx.MockTransport) -> DingTalkIdentityVerifier:
    """使用受控 HTTP 传输创建验证器，避免测试访问真实钉钉服务。"""

    return DingTalkIdentityVerifier(
        settings=build_settings(),
        token_provider=cast(DingTalkAppTokenProvider, FakeTokenProvider()),
        http_transport=handler,
    )


def build_verifier_with_cached_token(handler: httpx.MockTransport) -> DingTalkIdentityVerifier:
    """用固定缓存 Token 隔离可选电话读取的 endpoint、载荷与零重试行为。"""

    return DingTalkIdentityVerifier(
        settings=build_settings(),
        token_provider=cast(DingTalkAppTokenProvider, CachedTokenProvider()),
        http_transport=handler,
    )


def test_verifier_normalizes_only_trusted_fields() -> None:
    """验证器只输出企业、人员和显示名，不透传钉钉响应中的其他资料。"""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/gettoken":
            assert request.method == "GET"
            assert dict(request.url.params) == {
                "appkey": "test-app-key",
                "appsecret": "test-app-secret",
            }
            return httpx.Response(
                200,
                json={"errcode": 0, "access_token": "temporary-token", "expires_in": 7_200},
            )

        assert request.method == "POST"
        assert request.url.path == "/topapi/v2/user/getuserinfo"
        assert request.url.params["access_token"] == "temporary-token"
        assert json.loads(request.content) == {"code": "one-time-code"}
        return httpx.Response(
            200,
            json={
                "errcode": 0,
                "result": {
                    "userid": "manager-001",
                    "name": "测试管理员",
                    "unionid": "global-person-identifier",
                    "mobile": "13800000000",
                    "roles": ["system_admin"],
                },
            },
        )

    verifier = build_verifier(httpx.MockTransport(handler))

    assert verifier.verify("one-time-code") == DingTalkIdentity(
        corporation_id="ding-test-corp",
        personnel_id="manager-001",
        display_name="测试管理员",
    )


@pytest.mark.parametrize(
    ("supplier_mobile", "expected_mobile"),
    (
        ("13800000000", "13800000000"),
        ("  +8613800000000  ", "+8613800000000"),
        ("1234567", "1234567"),
    ),
)
def test_optional_mobile_uses_verified_userid_and_normalizes_ascii_number(
    supplier_mobile: str,
    expected_mobile: str,
) -> None:
    """电话只能由已验证 userid 查询，并且每次只调用一次固定用户详情接口。"""

    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.method == "POST"
        assert request.url.path == "/topapi/v2/user/get"
        assert dict(request.url.params) == {"access_token": "temporary-token"}
        assert json.loads(request.content) == {"userid": "manager-001"}
        return httpx.Response(
            200,
            json={
                "errcode": 0,
                "result": {"mobile": supplier_mobile, "hide_mobile": False},
            },
        )

    verifier = build_verifier_with_cached_token(httpx.MockTransport(handler))

    assert verifier.fetch_optional_mobile("manager-001") == expected_mobile
    assert len(requests) == 1


@pytest.mark.parametrize(
    "result",
    (
        {"mobile": "13800000000", "hide_mobile": True},
        {"mobile": "138 0000 0000", "hide_mobile": False},
        {"mobile": "138****0000", "hide_mobile": False},
        {"mobile": "１３８００００００００", "hide_mobile": False},
        {"mobile": "123456", "hide_mobile": False},
        {"mobile": "1234567890123456", "hide_mobile": False},
        {"mobile": "13800000000\n1", "hide_mobile": False},
        {"mobile": 13800000000, "hide_mobile": False},
        {"mobile": "13800000000", "hide_mobile": "false"},
        {},
    ),
)
def test_optional_mobile_rejects_hidden_or_noncanonical_supplier_values(
    result: dict[str, object],
) -> None:
    """隐藏、掩码、空格、非 ASCII、控制符、错类型和越界号码都按缺失处理。"""

    request_count = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return httpx.Response(200, json={"errcode": 0, "result": result})

    verifier = build_verifier_with_cached_token(httpx.MockTransport(handler))

    assert verifier.fetch_optional_mobile("manager-001") is None
    assert request_count == 1


@pytest.mark.parametrize(
    "response_factory",
    (
        lambda: httpx.Response(403, json={"errcode": 60011, "errmsg": "no permission"}),
        lambda: httpx.Response(503, json={"errcode": 500, "errmsg": "unavailable"}),
        lambda: httpx.Response(200, json={"errcode": 60011, "errmsg": "no permission"}),
        lambda: httpx.Response(200, content=b"not-json"),
        lambda: httpx.Response(200, json={"errcode": 0, "result": "malformed"}),
    ),
)
def test_optional_mobile_supplier_failures_return_none_without_retry(
    response_factory: Callable[[], httpx.Response],
) -> None:
    """权限、HTTP、业务码和畸形响应不会阻断登录，也不会自动重试。"""

    request_count = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return response_factory()

    verifier = build_verifier_with_cached_token(httpx.MockTransport(handler))

    assert verifier.fetch_optional_mobile("manager-001") is None
    assert request_count == 1


def test_optional_mobile_timeout_returns_none_without_retry() -> None:
    """用户详情超时只让电话缺失，且一次登录只尝试一次。"""

    request_count = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        raise httpx.ReadTimeout("controlled timeout")

    verifier = build_verifier_with_cached_token(httpx.MockTransport(handler))

    assert verifier.fetch_optional_mobile("manager-001") is None
    assert request_count == 1


def test_optional_mobile_does_not_swallow_programming_errors() -> None:
    """只有预期供应商故障可降级；程序错误必须保留为真实失败供开发修正。"""

    def handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("controlled programming error")

    verifier = build_verifier_with_cached_token(httpx.MockTransport(handler))

    with pytest.raises(AssertionError, match="controlled programming error"):
        verifier.fetch_optional_mobile("manager-001")


def test_verifier_maps_invalid_or_expired_code_to_credential_error() -> None:
    """免登码被钉钉拒绝时返回可安全处理的凭证错误，且不回显原始凭证。"""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/gettoken":
            return httpx.Response(
                200,
                json={"errcode": 0, "access_token": "temporary-token", "expires_in": 7_200},
            )
        return httpx.Response(200, json={"errcode": 40078, "errmsg": "invalid code"})

    verifier = build_verifier(httpx.MockTransport(handler))

    with pytest.raises(DingTalkCredentialError) as error:
        verifier.verify("one-time-code")

    assert "one-time-code" not in str(error.value)


def test_verifier_maps_http_400_with_invalid_code_to_credential_error() -> None:
    """HTTP 400 仅在携带已验证业务码 40078 时才表示一次性免登码失效。"""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/gettoken":
            return httpx.Response(
                200,
                json={"errcode": 0, "access_token": "temporary-token", "expires_in": 7_200},
            )
        return httpx.Response(400, json={"errcode": 40078, "errmsg": "invalid code"})

    verifier = build_verifier(httpx.MockTransport(handler))

    with pytest.raises(DingTalkCredentialError):
        verifier.verify("one-time-code")


def test_verifier_maps_timeout_to_unavailable_error() -> None:
    """钉钉网络超时必须被统一映射为可稍后重试的服务不可用错误。"""

    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out")

    verifier = build_verifier(httpx.MockTransport(handler))

    with pytest.raises(DingTalkUnavailableError):
        verifier.verify("one-time-code")


def test_verifier_maps_dingtalk_server_error_to_unavailable_error() -> None:
    """钉钉 5xx 不得被误判为教师凭证错误。"""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"errcode": 500, "errmsg": "service unavailable"})

    verifier = build_verifier(httpx.MockTransport(handler))

    with pytest.raises(DingTalkUnavailableError):
        verifier.verify("one-time-code")


@pytest.mark.parametrize(
    ("status_code", "error_code"),
    [(400, 400), (403, 40078), (429, 40078)],
)
def test_verifier_maps_user_info_http_client_errors_to_unavailable(
    status_code: int,
    error_code: int,
) -> None:
    """用户资料接口的 HTTP 4xx 属于上游异常，不得误报为教师免登码失效。"""

    user_info_attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal user_info_attempts
        if request.url.path == "/gettoken":
            return httpx.Response(
                200,
                json={"errcode": 0, "access_token": "temporary-token", "expires_in": 7_200},
            )
        user_info_attempts += 1
        return httpx.Response(status_code, json={"errcode": error_code})

    verifier = build_verifier(httpx.MockTransport(handler))

    with pytest.raises(DingTalkUnavailableError):
        verifier.verify("one-time-code")

    assert user_info_attempts == 1


def test_verifier_maps_malformed_http_400_to_unavailable() -> None:
    """无法解析的 HTTP 400 响应不得被猜测为免登码失效。"""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/gettoken":
            return httpx.Response(
                200,
                json={"errcode": 0, "access_token": "temporary-token", "expires_in": 7_200},
            )
        return httpx.Response(400, content=b"not-json")

    verifier = build_verifier(httpx.MockTransport(handler))

    with pytest.raises(DingTalkUnavailableError):
        verifier.verify("one-time-code")


@pytest.mark.parametrize("status_code", [400, 429])
def test_verifier_rejects_http_error_even_when_body_claims_success(
    status_code: int,
) -> None:
    """HTTP 错误状态不能因响应体伪装为 errcode=0 而被接受。"""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/gettoken":
            return httpx.Response(
                200,
                json={"errcode": 0, "access_token": "temporary-token", "expires_in": 7_200},
            )
        return httpx.Response(
            status_code,
            json={
                "errcode": 0,
                "result": {"userid": "manager-001", "name": "测试管理员"},
            },
        )

    verifier = build_verifier(httpx.MockTransport(handler))

    with pytest.raises(DingTalkUnavailableError):
        verifier.verify("one-time-code")


def test_verifier_maps_unknown_user_info_business_error_to_unavailable() -> None:
    """未知业务错误不能猜成一次性免登码错误，也不能重试可能已消费的 code。"""

    user_info_attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal user_info_attempts
        if request.url.path == "/gettoken":
            return httpx.Response(
                200,
                json={"errcode": 0, "access_token": "temporary-token", "expires_in": 7_200},
            )
        user_info_attempts += 1
        return httpx.Response(200, json={"errcode": 12345, "errmsg": "unknown"})

    verifier = build_verifier(httpx.MockTransport(handler))

    with pytest.raises(DingTalkUnavailableError):
        verifier.verify("one-time-code")

    assert user_info_attempts == 1


def test_verifier_enforces_total_wall_clock_deadline_for_slow_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """真实慢速响应即使持续有字节到达，也必须在单次请求总时限内被取消。"""

    with run_slow_dingtalk_server() as base_url:
        monkeypatch.setattr(
            dingtalk_client,
            "DINGTALK_ACCESS_TOKEN_URL",
            f"{base_url}/gettoken",
        )
        monkeypatch.setattr(
            dingtalk_client,
            "DINGTALK_USER_INFO_URL",
            f"{base_url}/topapi/v2/user/getuserinfo",
        )
        verifier = DingTalkIdentityVerifier(
            settings=build_settings(dingtalk_identity_timeout_seconds=1),
            token_provider=cast(DingTalkAppTokenProvider, FakeTokenProvider()),
        )
        started_at = perf_counter()
        with pytest.raises(DingTalkUnavailableError):
            verifier.verify("one-time-code")
        elapsed_seconds = perf_counter() - started_at

    assert elapsed_seconds < 1.5


def test_verifier_bounds_blocking_dns_and_recovers_without_thread_leaks() -> None:
    """阻塞 DNS 必须有父子双层硬超时，且只保留一个可恢复的解析工作者。"""

    environment = dict(os.environ)
    environment["PYTHONPATH"] = os.pathsep.join(
        filter(
            None,
            (
                str(API_ROOT),
                environment.get("PYTHONPATH"),
            ),
        )
    )
    result = subprocess.run(
        [sys.executable, "-c", _BLOCKING_DNS_PROGRAM],
        cwd=API_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=8,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr


class CorruptGzipStream(httpx.AsyncByteStream):
    """返回损坏的 gzip 字节，触发 HTTPX 响应解码失败。"""

    async def __aiter__(self):
        """只输出一次受控损坏数据。"""

        yield b"not-a-valid-gzip-stream"


def test_verifier_maps_response_decoding_error_to_unavailable() -> None:
    """损坏的压缩响应必须转换为固定不可用错误且不得回显凭证。"""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Encoding": "gzip"},
            stream=CorruptGzipStream(),
        )

    verifier = build_verifier(httpx.MockTransport(handler))

    with pytest.raises(DingTalkUnavailableError) as error:
        verifier.verify("one-time-code")

    error_text = str(error.value)
    assert "one-time-code" not in error_text
    assert "test-app-secret" not in error_text
    assert "temporary-token" not in error_text


class ControlledDualStackNetworkBackend(httpcore.AsyncNetworkBackend):
    """按地址制造连接超时或成功，并记录连接资源是否被回收。"""

    def __init__(self, timeout_hosts: set[str]) -> None:
        """保存超时地址、调用记录和唯一受控成功流。"""

        self.timeout_hosts = timeout_hosts
        self.calls: list[tuple[str, float | None]] = []
        self.active_attempts = 0
        self.maximum_active_attempts = 0
        self.stream = cast(httpcore.AsyncNetworkStream, object())

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        """让指定地址占满获配预算后超时，其余地址立即成功。"""

        del port, local_address, socket_options
        self.calls.append((host, timeout))
        self.active_attempts += 1
        self.maximum_active_attempts = max(
            self.maximum_active_attempts,
            self.active_attempts,
        )
        try:
            if host in self.timeout_hosts:
                assert timeout is not None
                await asyncio.sleep(timeout)
                raise httpcore.ConnectTimeout("controlled address timeout")
            return self.stream
        finally:
            self.active_attempts -= 1

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        """测试不允许退化为 Unix Socket。"""

        del path, timeout, socket_options
        raise AssertionError("unexpected Unix Socket connection")

    async def sleep(self, seconds: float) -> None:
        """保持完整网络后端契约。"""

        await asyncio.sleep(seconds)


class DualStackFailingTransport(httpx.AsyncBaseTransport):
    """通过真实受测 DNS 后端触发一次受控的双栈连接失败。"""

    def __init__(self, backend: dingtalk_client._BoundedDnsNetworkBackend) -> None:
        """保存只属于当前测试请求的受测后端。"""

        self.backend = backend

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        """执行一次双栈连接；测试场景要求它在返回响应前失败。"""

        del request
        await self.backend.connect_tcp(
            "dual-stack.test",
            443,
            timeout=0.2,
        )
        raise AssertionError("all controlled addresses should fail")


@pytest.mark.parametrize(
    ("addresses", "timeout_host", "expected_hosts"),
    (
        (
            (
                (socket.AF_INET6, ("2001:db8::1", 443, 0, 0)),
                (socket.AF_INET6, ("2001:db8::2", 443, 0, 0)),
                (socket.AF_INET, ("127.0.0.1", 443)),
            ),
            "2001:db8::1",
            ["2001:db8::1", "127.0.0.1"],
        ),
        (
            (
                (socket.AF_INET, ("192.0.2.1", 443)),
                (socket.AF_INET, ("192.0.2.2", 443)),
                (socket.AF_INET6, ("::1", 443, 0, 0)),
            ),
            "192.0.2.1",
            ["192.0.2.1", "::1"],
        ),
    ),
)
def test_dual_stack_timeout_falls_back_to_other_address_family_within_budget(
    monkeypatch: pytest.MonkeyPatch,
    addresses: tuple[tuple[int, tuple[object, ...]], ...],
    timeout_host: str,
    expected_hosts: list[str],
) -> None:
    """首选族超时时必须交错尝试另一族，并为后续候选保留连接预算。"""

    resolved_addresses = [
        (family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", socket_address)
        for family, socket_address in addresses
    ]
    monkeypatch.setattr(
        dingtalk_client._dns_resolver_gate,
        "resolve",
        lambda _host, _port, _timeout: resolved_addresses,
    )
    network_backend = ControlledDualStackNetworkBackend({timeout_host})
    backend = dingtalk_client._BoundedDnsNetworkBackend()
    backend._network_backend = network_backend

    started_at = perf_counter()
    stream = asyncio.run(
        backend.connect_tcp(
            "dual-stack.test",
            443,
            timeout=0.2,
        )
    )
    elapsed_seconds = perf_counter() - started_at

    assert stream is network_backend.stream
    assert [host for host, _timeout in network_backend.calls] == expected_hosts
    assert network_backend.calls[0][1] is not None
    assert cast(float, network_backend.calls[0][1]) < 0.1
    assert elapsed_seconds < 0.3
    assert network_backend.active_attempts == 0
    assert network_backend.maximum_active_attempts == 1


def test_all_dual_stack_addresses_fail_within_total_budget_and_map_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """双栈地址全部超时时仍须尝试两族、回收连接并在总预算内返回不可用。"""

    resolved_addresses = [
        (
            socket.AF_INET6,
            socket.SOCK_STREAM,
            socket.IPPROTO_TCP,
            "",
            ("2001:db8::1", 443, 0, 0),
        ),
        (
            socket.AF_INET,
            socket.SOCK_STREAM,
            socket.IPPROTO_TCP,
            "",
            ("192.0.2.1", 443),
        ),
    ]
    monkeypatch.setattr(
        dingtalk_client._dns_resolver_gate,
        "resolve",
        lambda _host, _port, _timeout: resolved_addresses,
    )
    network_backend = ControlledDualStackNetworkBackend({"2001:db8::1", "192.0.2.1"})
    backend = dingtalk_client._BoundedDnsNetworkBackend()
    backend._network_backend = network_backend
    verifier = DingTalkIdentityVerifier(
        settings=build_settings(dingtalk_identity_timeout_seconds=1),
        token_provider=cast(DingTalkAppTokenProvider, FakeTokenProvider()),
        http_transport=DualStackFailingTransport(backend),
    )

    started_at = perf_counter()
    with pytest.raises(DingTalkUnavailableError):
        verifier.verify("one-time-code")
    elapsed_seconds = perf_counter() - started_at

    assert [host for host, _timeout in network_backend.calls] == [
        "2001:db8::1",
        "192.0.2.1",
    ]
    assert elapsed_seconds < 0.3
    assert network_backend.active_attempts == 0
    assert network_backend.maximum_active_attempts == 1
