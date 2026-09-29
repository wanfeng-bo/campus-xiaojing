from __future__ import annotations

from collections.abc import Iterator
from ipaddress import ip_address, ip_network
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, Response
from sqlalchemy.orm import Session, sessionmaker

from app.composition.school_fact_dingtalk_identity import SchoolFactDingTalkIdentityService
from app.composition.school_fact_product_identity import (
    ProductIdentityUnavailable,
    ProductSessionView,
)
from app.core.settings import Settings
from app.db.connection import get_session
from app.integrations.dingtalk.client import DingTalkIdentityVerifier
from app.interfaces.http.school_fact_product_identity import (
    PRODUCT_BASE_PATH,
    SchoolFactProductIdentityHttp,
)
from app.platform.identity.login_guard import DingTalkLoginGuard
from app.platform.identity.router import (
    get_dingtalk_identity_verifier,
    get_dingtalk_login_guard,
    get_identity_session_store,
    issue_dingtalk_identity_session,
)
from app.platform.identity.schemas import DingTalkExchangeRequest, IdentityAccessStatus
from app.platform.identity.session import IdentitySessionStore, SessionRedisClient


class SchoolFactDingTalkIdentityHttp(SchoolFactProductIdentityHttp):
    """Same product contracts, genuine DingTalk entry, exact public HTTPS origin."""

    def __init__(
        self,
        *,
        service: SchoolFactDingTalkIdentityService,
        settings: Settings,
        allowed_origin: str,
        directory_enabled: bool = False,
        education_enabled: bool = False,
        trusted_proxy_cidrs: tuple[str, ...] = (),
    ) -> None:
        parsed = urlsplit(allowed_origin)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.path
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("formal identity requires one exact HTTPS origin")
        if settings.identity_session_cookie_name in {
            "campus_ai_session",
            "school_fact_product_session",
        }:
            raise ValueError("formal identity requires an isolated cookie")
        self._trusted_proxy_networks = tuple(
            ip_network(value, strict=True) for value in trusted_proxy_cidrs
        )
        if any(not n.is_private or n.num_addresses > 256 for n in self._trusted_proxy_networks):
            raise ValueError("formal proxy networks must be explicit narrow private peers")
        self._service = service
        self._settings = settings
        self._origin = allowed_origin
        self._cookie_name = settings.identity_session_cookie_name
        self._cookie_path = "/"
        self._cookie_secure = True
        self._directory_enabled = directory_enabled
        self._education_enabled = education_enabled
        self.router = APIRouter(prefix=PRODUCT_BASE_PATH, tags=["school-fact-dingtalk-identity"])
        self.router.add_api_route(
            "/session", self.session, methods=["GET"], response_model=ProductSessionView
        )
        self.router.add_api_route(
            "/context", self.context, methods=["POST"], response_model=ProductSessionView
        )
        self.router.add_api_route(
            "/logout", self.logout, methods=["POST"], response_model=ProductSessionView
        )
        self.router.add_api_route(
            "/dingtalk/exchange", self.exchange, methods=["POST"], response_model=ProductSessionView
        )

    def require_local_request(self, request: Request) -> None:
        # Forwarding headers alone never authenticate a proxy. The server must
        # preserve the peer address and explicitly configure its trusted peers.
        public_origin = f"{request.url.scheme}://{request.url.netloc}"
        if public_origin != self._origin and request.client is not None:
            try:
                peer = ip_address(request.client.host)
                trusted = any(peer in network for network in self._trusted_proxy_networks)
            except ValueError:
                trusted = False
            if trusted and request.headers.get("x-forwarded-proto") == "https":
                forwarded_host = request.headers.get("x-forwarded-host", request.url.netloc)
                public_origin = "https://" + forwarded_host
        if public_origin != self._origin or request.headers.get("sec-fetch-site") == "cross-site":
            raise HTTPException(status_code=403, detail="请从学校应用入口进入。")
        if (
            request.method not in {"GET", "HEAD", "OPTIONS"}
            and request.headers.get("origin") != self._origin
        ):
            raise HTTPException(status_code=403, detail="身份操作需要同源请求。")

    def exchange(
        self,
        body: DingTalkExchangeRequest,
        request: Request,
        response: Response,
        login_guard: Annotated[DingTalkLoginGuard, Depends(get_dingtalk_login_guard)],
        verifier: Annotated[DingTalkIdentityVerifier, Depends(get_dingtalk_identity_verifier)],
        identity_session_store: Annotated[
            IdentitySessionStore, Depends(get_identity_session_store)
        ],
        session: Annotated[Session, Depends(get_session)],
    ) -> ProductSessionView:
        self.require_local_request(request)
        self._headers(response)
        token, _ = issue_dingtalk_identity_session(
            body,
            request,
            response,
            self._settings,
            login_guard,
            verifier,
            identity_session_store,
            session,
            # Legacy UserRole is intentionally not the authority of this product.
            access_status_resolver=lambda _session, _user: IdentityAccessStatus(
                access_state="pending_configuration", can_manage_system=False
            ),
        )
        session.close()
        try:
            old_token = request.cookies.get(self._cookie_name)
            if old_token and old_token != token:
                self._service.logout(old_token)
            return self._session_view(token)
        except ProductIdentityUnavailable:
            self._service.logout(token)
            raise HTTPException(status_code=503, detail="当前履职身份暂时无法核验。") from None


def install_dingtalk_identity_dependencies(
    app: FastAPI, *, settings: Settings, sessions: sessionmaker[Session], redis: SessionRedisClient
) -> None:
    """Bind authentication to this product instance, never the default legacy resources."""
    from app.core.settings import get_settings
    from app.platform.identity.router import get_identity_redis

    def product_session() -> Iterator[Session]:
        with sessions() as session:
            yield session

    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_session] = product_session
    app.dependency_overrides[get_identity_redis] = lambda: redis
