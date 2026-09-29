# pyright: strict
from __future__ import annotations

from ipaddress import IPv4Network, IPv6Network, ip_address, ip_network
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from app.composition.school_fact_product_identity import (
    SESSION_TTL_SECONDS,
    ProductFamilyIdentity,
    ProductIdentity,
    ProductIdentityRejected,
    ProductIdentityUnavailable,
    ProductSessionView,
    SchoolFactIdentitySessionService,
    SchoolFactProductIdentityService,
)

PRODUCT_BASE_PATH = "/api/v1/school-fact-product"
PRODUCT_COOKIE_PATH = "/api/v1"
PRODUCT_COOKIE_NAME = "school_fact_product_session"


class _LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    account_ref: str = Field(min_length=1, max_length=60)


class _ContextRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    context_ref: str = Field(min_length=32, max_length=128)


class SchoolFactProductIdentityHttp:
    """An opt-in loopback surface; the Hangzhou/default router remains untouched."""

    def __init__(
        self,
        *,
        service: SchoolFactIdentitySessionService,
        allowed_origin: str,
        directory_enabled: bool = False,
        education_enabled: bool = False,
        trusted_proxy_cidrs: tuple[str, ...] = (),
    ) -> None:
        parsed = urlsplit(allowed_origin)
        if (
            parsed.scheme not in {"http", "https"}
            or parsed.username
            or parsed.password
            or parsed.path
            or parsed.query
            or parsed.fragment
            or not parsed.hostname
            or not _loopback_host(parsed.hostname)
        ):
            raise ValueError("engineering identity requires one explicit loopback origin")
        self._trusted_proxy_networks: tuple[IPv4Network | IPv6Network, ...] = tuple(
            ip_network(value, strict=True) for value in trusted_proxy_cidrs
        )
        if any(
            not network.is_private or network.num_addresses > 256
            for network in self._trusted_proxy_networks
        ):
            raise ValueError("engineering proxy networks must be explicit narrow private peers")
        self._cookie_name = PRODUCT_COOKIE_NAME
        self._cookie_path = PRODUCT_COOKIE_PATH
        self._service = service
        self._directory_enabled = directory_enabled
        self._education_enabled = education_enabled
        self._origin = allowed_origin
        self._cookie_secure = parsed.scheme == "https"
        self.router = APIRouter(prefix=PRODUCT_BASE_PATH, tags=["school-fact-product-identity"])
        self.router.add_api_route(
            "/session", self.session, methods=["GET"], response_model=ProductSessionView
        )
        self.router.add_api_route(
            "/login", self.login, methods=["POST"], response_model=ProductSessionView
        )
        self.router.add_api_route(
            "/context", self.context, methods=["POST"], response_model=ProductSessionView
        )
        self.router.add_api_route(
            "/logout", self.logout, methods=["POST"], response_model=ProductSessionView
        )

    def _session_view(self, token: str | None) -> ProductSessionView:
        return self._service.session_view(token).model_copy(
            update={
                "directory_enabled": self._directory_enabled,
                "education_enabled": self._education_enabled,
            }
        )

    def request_dependency(self, request: Request) -> ProductIdentity:
        self.require_local_request(request)
        try:
            return self._service.resolve_request(request.cookies.get(self._cookie_name))
        except ProductIdentityRejected:
            raise HTTPException(status_code=401, detail="请登录并选择当前履职身份。") from None
        except ProductIdentityUnavailable:
            raise HTTPException(status_code=503, detail="当前身份暂时无法核验。") from None

    def assistant_request_dependency(
        self, request: Request
    ) -> ProductIdentity | ProductFamilyIdentity:
        self.require_local_request(request)
        try:
            return self._service.resolve_assistant_request(request.cookies.get(self._cookie_name))
        except ProductIdentityRejected:
            raise HTTPException(status_code=401, detail="请登录并选择家长或履职身份。") from None
        except ProductIdentityUnavailable:
            raise HTTPException(status_code=503, detail="当前身份暂时无法核验。") from None

    def require_local_request(self, request: Request) -> None:
        if (
            request.client is None
            or not self._trusted_peer(request.client.host)
            or f"{request.url.scheme}://{request.url.netloc}" != self._origin
            or request.headers.get("sec-fetch-site") == "cross-site"
        ):
            raise HTTPException(status_code=403, detail="此工程入口仅在本机同源使用。")
        if (
            request.method not in {"GET", "HEAD", "OPTIONS"}
            and request.headers.get("origin") != self._origin
        ):
            raise HTTPException(status_code=403, detail="工程身份操作需要同源请求。")

    def _trusted_peer(self, host: str) -> bool:
        if _loopback_host(host):
            return True
        try:
            peer = ip_address(host)
            return any(peer in network for network in self._trusted_proxy_networks)
        except ValueError:
            return False

    @staticmethod
    def _headers(response: Response) -> None:
        response.headers["Cache-Control"] = "no-store"
        response.headers["Vary"] = "Cookie"

    def session(self, request: Request, response: Response) -> ProductSessionView:
        self.require_local_request(request)
        self._headers(response)
        try:
            result = self._session_view(request.cookies.get(self._cookie_name))
            if result.status == "signed_out":
                response.delete_cookie(
                    self._cookie_name,
                    path=self._cookie_path,
                    httponly=True,
                    samesite="strict",
                    secure=self._cookie_secure,
                )
            return result
        except ProductIdentityUnavailable:
            raise HTTPException(status_code=503, detail="当前身份暂时无法核验。") from None

    def login(
        self, body: _LoginRequest, request: Request, response: Response
    ) -> ProductSessionView:
        self.require_local_request(request)
        self._headers(response)
        try:
            if not isinstance(self._service, SchoolFactProductIdentityService):
                raise ProductIdentityRejected("此入口不接受工程账号。")
            token = self._service.login(body.account_ref)
            old_token = request.cookies.get(self._cookie_name)
            if old_token:
                self._service.logout(old_token)
            result = self._session_view(token)
            response.set_cookie(
                self._cookie_name,
                token,
                max_age=SESSION_TTL_SECONDS,
                path=self._cookie_path,
                httponly=True,
                samesite="strict",
                secure=self._cookie_secure,
            )
            return result
        except ProductIdentityRejected:
            raise HTTPException(status_code=403, detail="该合成工程账号尚不可用。") from None
        except ProductIdentityUnavailable:
            raise HTTPException(status_code=503, detail="工程登录服务暂时不可用。") from None

    def context(
        self, body: _ContextRequest, request: Request, response: Response
    ) -> ProductSessionView:
        self.require_local_request(request)
        self._headers(response)
        token = request.cookies.get(self._cookie_name)
        try:
            transition = self._service.select_context(token, body.context_ref)
            return self._session_view(token).model_copy(
                update={"selection_transition_ref": transition}
            )
        except ProductIdentityRejected:
            raise HTTPException(status_code=409, detail="履职身份已变化，请重新选择。") from None
        except ProductIdentityUnavailable:
            raise HTTPException(status_code=503, detail="履职身份暂时无法核验。") from None

    def logout(self, request: Request, response: Response) -> ProductSessionView:
        self.require_local_request(request)
        self._headers(response)
        try:
            self._service.logout(request.cookies.get(self._cookie_name))
            response.delete_cookie(
                self._cookie_name,
                path=self._cookie_path,
                httponly=True,
                samesite="strict",
                secure=self._cookie_secure,
            )
            return self._session_view(None)
        except ProductIdentityUnavailable:
            raise HTTPException(status_code=503, detail="退出服务暂时不可用，请重试。") from None


def _loopback_host(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ip_address(host).is_loopback
    except ValueError:
        return False
