from __future__ import annotations

import asyncio
import re
import socket
import threading
import time
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass, field
from ipaddress import ip_address
from typing import Any, Final, Protocol, cast

import httpcore
import httpx

from app.core.settings import Settings
from app.integrations.dingtalk.errors import (
    DingTalkCredentialError,
    DingTalkUnavailableError,
)
from app.integrations.dingtalk.token_provider import (
    DingTalkAppTokenProvider,
    FetchedDingTalkAppToken,
)

DINGTALK_ACCESS_TOKEN_URL: Final = "https://oapi.dingtalk.com/gettoken"
DINGTALK_USER_INFO_URL: Final = "https://oapi.dingtalk.com/topapi/v2/user/getuserinfo"
DINGTALK_USER_DETAIL_URL: Final = "https://oapi.dingtalk.com/topapi/v2/user/get"
INVALID_AUTH_CODE_ERROR_CODES: Final = frozenset({40078, "40078"})
_TRUSTED_MOBILE_PATTERN: Final = re.compile(r"\+?[0-9]{7,15}")


@dataclass
class _ResolutionJob:
    """保存唯一在途的钉钉 DNS 解析结果。"""

    host: str
    port: int
    event: threading.Event = field(default_factory=threading.Event)
    result: list[tuple[Any, ...]] | None = None
    error: BaseException | None = None
    abandoned: bool = False


class _DingTalkDnsResolverGate:
    """用最多一个 daemon 工作者限制系统 DNS 阻塞的影响范围。"""

    def __init__(self) -> None:
        """初始化进程级解析锁和唯一在途任务。"""

        self._lock = threading.Lock()
        self._inflight: _ResolutionJob | None = None

    def resolve(
        self,
        host: str,
        port: int,
        timeout_seconds: float,
    ) -> list[tuple[Any, ...]]:
        """在连接预算内解析域名；同地址共享任务，异常在途任务令后续请求快速失败。"""

        owns_job = False
        with self._lock:
            current_job = self._inflight
            if current_job is not None and not current_job.event.is_set():
                if current_job.host != host or current_job.port != port or current_job.abandoned:
                    raise httpcore.ConnectTimeout("DingTalk DNS resolution is already pending")
                job = current_job
            elif current_job is not None and current_job.host == host and current_job.port == port:
                job = current_job
            else:
                job = _ResolutionJob(host=host, port=port)
                self._inflight = job
                owns_job = True

        if owns_job:
            worker = threading.Thread(
                target=self._resolve,
                args=(job, host, port),
                name="dingtalk-dns-resolver",
                daemon=True,
            )
            worker.start()
        if not job.event.wait(timeout_seconds):
            if owns_job:
                with self._lock:
                    if self._inflight is job and not job.event.is_set():
                        job.abandoned = True
            raise httpcore.ConnectTimeout("DingTalk DNS resolution deadline exceeded")
        if job.error is not None:
            raise job.error
        if job.result is None:
            raise OSError("socket.getaddrinfo returned an empty list")
        return job.result

    def _resolve(
        self,
        job: _ResolutionJob,
        host: str,
        port: int,
    ) -> None:
        """在唯一后台工作者中执行不可取消的系统解析并发布结果。"""

        try:
            job.result = list(socket.getaddrinfo(host, port, type=socket.SOCK_STREAM))
        except BaseException as error:
            job.error = error
        finally:
            job.event.set()
            with self._lock:
                if self._inflight is job:
                    self._inflight = None


_dns_resolver_gate = _DingTalkDnsResolverGate()


def _interleave_numeric_hosts(addresses: list[tuple[Any, ...]]) -> list[str]:
    """按系统首选族起始轮询各地址族，同时删除重复数值地址。"""

    family_order: list[int] = []
    hosts_by_family: dict[int, list[str]] = {}
    seen_hosts: set[str] = set()
    for family, _socktype, _proto, _canonname, socket_address in addresses:
        numeric_host = str(socket_address[0])
        if numeric_host in seen_hosts:
            continue
        seen_hosts.add(numeric_host)
        if family not in hosts_by_family:
            family_order.append(family)
            hosts_by_family[family] = []
        hosts_by_family[family].append(numeric_host)

    numeric_hosts: list[str] = []
    maximum_family_size = max(
        (len(hosts_by_family[family]) for family in family_order),
        default=0,
    )
    for address_index in range(maximum_family_size):
        for family in family_order:
            family_hosts = hosts_by_family[family]
            if address_index < len(family_hosts):
                numeric_hosts.append(family_hosts[address_index])
    return numeric_hosts


class _BoundedDnsNetworkBackend(httpcore.AsyncNetworkBackend):
    """在 httpcore 保留原始 Host/SNI 的同时，以有界 DNS 结果连接数值地址。"""

    def __init__(self) -> None:
        """创建只负责数值地址连接和 TLS 流包装的 AnyIO 后端。"""

        self._network_backend = cast(
            httpcore.AsyncNetworkBackend,
            httpcore.AnyIOBackend(),
        )

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        """在同一连接预算内完成一次受控解析和数值地址 TCP 连接。"""

        timeout_seconds = min(timeout if timeout is not None else 1.0, 1.0)
        if timeout_seconds <= 0:
            raise httpcore.ConnectTimeout("DingTalk connection deadline exceeded")
        deadline = time.monotonic() + timeout_seconds

        try:
            ip_address(host)
        except ValueError:
            try:
                addresses = _dns_resolver_gate.resolve(host, port, timeout_seconds)
            except httpcore.ConnectTimeout:
                raise
            except OSError as error:
                raise httpcore.ConnectError(str(error)) from None
            numeric_hosts = _interleave_numeric_hosts(addresses)
        else:
            numeric_hosts = [host]

        last_error: httpcore.ConnectError | httpcore.ConnectTimeout | None = None
        for address_index, numeric_host in enumerate(numeric_hosts):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise httpcore.ConnectTimeout("DingTalk connection deadline exceeded")
            remaining_address_count = len(numeric_hosts) - address_index
            attempt_timeout = remaining / remaining_address_count
            try:
                return await self._network_backend.connect_tcp(
                    numeric_host,
                    port,
                    timeout=attempt_timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as error:
                last_error = error
        if last_error is not None:
            raise last_error
        raise httpcore.ConnectError("DingTalk DNS returned no usable address")

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        """保持后端接口完整；钉钉固定 HTTPS URL 不使用 Unix Socket。"""

        return await self._network_backend.connect_unix_socket(
            path,
            timeout=timeout,
            socket_options=socket_options,
        )

    async def sleep(self, seconds: float) -> None:
        """委托 AnyIO 执行可取消等待；当前连接池禁用自动重试。"""

        await self._network_backend.sleep(seconds)


class _ClosableAsyncStream(Protocol):
    """描述 httpcore 返回给 HTTPX 的可关闭异步字节流。"""

    def __aiter__(self) -> AsyncIterator[bytes]:
        """返回异步响应迭代器。"""

        ...

    async def aclose(self) -> None:
        """关闭底层响应流。"""

        ...


class _HttpCoreResponseStream(httpx.AsyncByteStream):
    """把 httpcore 异步响应流适配为 HTTPX 响应流。"""

    def __init__(self, stream: _ClosableAsyncStream) -> None:
        """保存仅属于当前请求的响应流。"""

        self._stream = stream

    async def __aiter__(self) -> AsyncIterator[bytes]:
        """逐块转发响应数据，保留 HTTPX 的标准解码流程。"""

        async for part in self._stream:
            yield part

    async def aclose(self) -> None:
        """在响应完成或取消时关闭 httpcore 流。"""

        await self._stream.aclose()


class _BoundedDnsHttpTransport(httpx.AsyncBaseTransport):
    """用 public httpcore 连接池注入有界 DNS 后端并保持 TLS 证书校验。"""

    def __init__(self) -> None:
        """创建单请求连接池；不使用代理、连接重试或跨请求连接复用。"""

        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=httpx.create_ssl_context(verify=True, trust_env=False),
            max_connections=1,
            max_keepalive_connections=0,
            retries=0,
            network_backend=_BoundedDnsNetworkBackend(),
        )

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        """保持原始域名构造 core 请求，使 Host 与 TLS SNI 不被解析 IP 替换。"""

        core_request = httpcore.Request(
            method=request.method,
            url=httpcore.URL(
                scheme=request.url.raw_scheme,
                host=request.url.raw_host,
                port=request.url.port,
                target=request.url.raw_path,
            ),
            headers=request.headers.raw,
            content=request.stream,
            extensions=request.extensions,
        )
        response = await self._pool.handle_async_request(core_request)
        return httpx.Response(
            status_code=response.status,
            headers=response.headers,
            stream=_HttpCoreResponseStream(
                cast(_ClosableAsyncStream, response.stream),
            ),
            extensions=response.extensions,
        )

    async def aclose(self) -> None:
        """关闭当前请求的 core 连接池和所有底层流。"""

        await self._pool.aclose()


@dataclass(frozen=True)
class DingTalkIdentity:
    """保存钉钉验证后可用于本地映射的最小可信字段。"""

    corporation_id: str
    personnel_id: str
    display_name: str


class DingTalkIdentityVerifier:
    """调用钉钉 H5 免登服务端接口并规范化企业内身份，不保存或记录外部凭证。"""

    def __init__(
        self,
        *,
        settings: Settings,
        token_provider: DingTalkAppTokenProvider,
        http_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """绑定配置、共享 Token 提供者及可选受控异步传输。"""

        self._settings = settings
        self._token_provider = token_provider
        self._http_transport = http_transport
        self._total_timeout_seconds = settings.dingtalk_identity_timeout_seconds

    def verify(self, auth_code: str) -> DingTalkIdentity:
        """验证一次性免登码并返回最小身份；超时不重试，且绝不记录 code 或钉钉令牌。"""

        access_token = self._token_provider.get(self._fetch_access_token)
        response_payload = self._get_user_info(auth_code, access_token)
        return self._normalize_identity(response_payload)

    def fetch_optional_mobile(self, personnel_id: str) -> str | None:
        """按已验证 userid 尝试读取结构化电话；供应商失败只降级为电话缺失。"""

        if not personnel_id:
            raise ValueError("personnel_id must not be empty")
        try:
            access_token = self._token_provider.get(self._fetch_access_token)
            response_payload = self._get_user_detail(personnel_id, access_token)
        except DingTalkUnavailableError:
            return None
        return self._normalize_optional_mobile(response_payload)

    def _fetch_access_token(self) -> FetchedDingTalkAppToken:
        """调用钉钉获取应用 Token，并严格校验 Token 与有效期。"""

        response = self._send_request(
            method="GET",
            url=DINGTALK_ACCESS_TOKEN_URL,
            params={
                "appkey": self._settings.dingtalk_app_key,
                "appsecret": self._settings.dingtalk_app_secret,
            },
        )
        payload = self._read_success_payload(response, allow_invalid_auth_code=False)
        access_token = payload.get("access_token")
        expires_in = payload.get("expires_in")
        if (
            not isinstance(access_token, str)
            or not access_token
            or not isinstance(expires_in, int)
            or expires_in <= 330
        ):
            raise DingTalkUnavailableError("钉钉身份服务返回异常。")
        return FetchedDingTalkAppToken(access_token, expires_in)

    def _get_user_info(self, auth_code: str, access_token: str) -> dict[str, object]:
        """以一次性免登码读取企业内用户资料，不对可能已消费的 code 自动重试。"""

        response = self._send_request(
            method="POST",
            url=DINGTALK_USER_INFO_URL,
            params={"access_token": access_token},
            json={"code": auth_code},
        )
        return self._read_success_payload(response, allow_invalid_auth_code=True)

    def _get_user_detail(self, personnel_id: str, access_token: str) -> dict[str, object]:
        """以已验证 userid 读取一次用户详情；电话是可选增强，因此不自动重试。"""

        response = self._send_request(
            method="POST",
            url=DINGTALK_USER_DETAIL_URL,
            params={"access_token": access_token},
            json={"userid": personnel_id},
        )
        return self._read_success_payload(response, allow_invalid_auth_code=False)

    def _send_request(
        self,
        *,
        method: str,
        url: str,
        params: dict[str, str | None],
        json: dict[str, str] | None = None,
    ) -> httpx.Response:
        """在总墙钟时限内执行一次钉钉请求，不重试或记录请求参数与响应体。"""

        try:
            response = asyncio.run(
                self._send_request_async(
                    method=method,
                    url=url,
                    params=params,
                    json=json,
                )
            )
        except (
            TimeoutError,
            httpx.RequestError,
            httpcore.TimeoutException,
            httpcore.NetworkError,
            httpcore.ProtocolError,
        ):
            raise DingTalkUnavailableError("钉钉身份服务暂时不可用。") from None

        if response.status_code >= 500:
            raise DingTalkUnavailableError("钉钉身份服务暂时不可用。")
        return response

    async def _send_request_async(
        self,
        *,
        method: str,
        url: str,
        params: dict[str, str | None],
        json: dict[str, str] | None,
    ) -> httpx.Response:
        """用异步取消约束完整请求墙钟时间，并在取消后关闭本次独立连接资源。"""

        phase_timeout = httpx.Timeout(
            timeout=self._total_timeout_seconds,
            connect=min(1.0, self._total_timeout_seconds),
        )
        async with asyncio.timeout(self._total_timeout_seconds):
            http_transport = self._http_transport or _BoundedDnsHttpTransport()
            async with httpx.AsyncClient(
                transport=http_transport,
                trust_env=False,
            ) as http_client:
                return await http_client.request(
                    method,
                    url,
                    params=params,
                    json=json,
                    timeout=phase_timeout,
                )

    def _read_success_payload(
        self,
        response: httpx.Response,
        *,
        allow_invalid_auth_code: bool,
    ) -> dict[str, object]:
        """读取钉钉成功响应并检查业务错误码，避免把供应商字段传出适配层。"""

        try:
            payload = response.json()
        except ValueError:
            raise DingTalkUnavailableError("钉钉身份服务返回异常。") from None
        if not isinstance(payload, dict):
            raise DingTalkUnavailableError("钉钉身份服务返回异常。")

        error_code = payload.get("errcode")
        invalid_auth_code = allow_invalid_auth_code and error_code in INVALID_AUTH_CODE_ERROR_CODES
        if not 200 <= response.status_code < 300:
            if response.status_code == 400 and invalid_auth_code:
                raise DingTalkCredentialError("钉钉身份凭证验证失败。")
            raise DingTalkUnavailableError("钉钉身份服务暂时不可用。")
        if error_code not in (None, 0, "0"):
            if invalid_auth_code:
                raise DingTalkCredentialError("钉钉身份凭证验证失败。")
            raise DingTalkUnavailableError("钉钉身份服务暂时不可用。")
        return payload

    def _normalize_identity(self, payload: dict[str, object]) -> DingTalkIdentity:
        """从钉钉响应中提取固定身份键和显示名，拒绝字段缺失或类型异常的响应。"""

        result = payload.get("result")
        if not isinstance(result, dict):
            raise DingTalkUnavailableError("钉钉身份服务返回异常。")

        personnel_id = result.get("userid", result.get("userId"))
        display_name = result.get("name")
        if not isinstance(personnel_id, str) or not personnel_id:
            raise DingTalkUnavailableError("钉钉身份服务返回异常。")
        if not isinstance(display_name, str) or not display_name:
            raise DingTalkUnavailableError("钉钉身份服务返回异常。")

        return DingTalkIdentity(
            corporation_id=self._settings.dingtalk_corp_id or "",
            personnel_id=personnel_id,
            display_name=display_name,
        )

    @staticmethod
    def _normalize_optional_mobile(payload: dict[str, object]) -> str | None:
        """只接受未隐藏且 trim 后符合 ASCII `+?[0-9]{7,15}` 的供应商 mobile。"""

        result = payload.get("result")
        if not isinstance(result, dict):
            return None
        if "hide_mobile" in result:
            hide_mobile = result["hide_mobile"]
            if type(hide_mobile) is not bool or hide_mobile:
                return None
        mobile = result.get("mobile")
        if not isinstance(mobile, str):
            return None
        normalized_mobile = mobile.strip()
        if _TRUSTED_MOBILE_PATTERN.fullmatch(normalized_mobile) is None:
            return None
        return normalized_mobile
