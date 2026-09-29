"""Temporary, bounded login handoff. This app never initializes or grants access.

Run via uvicorn --factory with SCHOOL_FACT_SESSION_HANDOFF_CONFIG set to a
protected, reviewed configuration file. Disable uvicorn proxy-header rewriting;
the existing exact-origin policy checks the actual peer itself. Linux only for
secret output: POSIX mode bits must never be mistaken for Windows ACL evidence.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import stat
from collections.abc import AsyncGenerator, Callable, Generator
from contextlib import asynccontextmanager, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, BinaryIO, Literal, Self, cast

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator
from redis import Redis
from sqlalchemy.orm import Session, sessionmaker

from app.composition.school_fact_dingtalk_identity import SchoolFactDingTalkIdentityService
from app.composition.school_fact_live_initialization import load_reviewed_contract
from app.composition.school_fact_live_initialization_contracts import (
    LiveInitializationManifest,
    LiveInitializationRuntime,
    LiveInitializationSession,
    LiveSessionHandoffApproval,
)
from app.composition.school_fact_live_initialization_identity import (
    require_live_initialization_account,
)
from app.core.request_id import assign_request_id
from app.db.connection import (
    build_engine,
    build_session_factory,
    get_session,
    verify_runtime_context_signing_key,
)
from app.integrations.dingtalk.client import DingTalkIdentity, DingTalkIdentityVerifier
from app.integrations.dingtalk.errors import DingTalkCredentialError
from app.interfaces.http.school_fact_dingtalk_identity import (
    SchoolFactDingTalkIdentityHttp,
    install_dingtalk_identity_dependencies,
)
from app.platform.identity.context import CompleteIdentitySessionError, CurrentUser
from app.platform.identity.login_guard import DingTalkLoginGuard
from app.platform.identity.models import User
from app.platform.identity.router import (
    get_dingtalk_identity_verifier,
    get_dingtalk_login_guard,
    get_identity_session_store,
    issue_dingtalk_identity_session,
)
from app.platform.identity.schemas import DingTalkExchangeRequest, IdentityAccessStatus
from app.platform.identity.session import IdentitySessionStore, SessionRedisClient

logger = logging.getLogger(__name__)
BASE = "/api/v1/school-fact-initialization"


def _origin_policy(
    runtime: LiveInitializationRuntime, origin: str, proxies: tuple[str, ...]
) -> SchoolFactDingTalkIdentityHttp:
    return SchoolFactDingTalkIdentityHttp(
        service=cast(SchoolFactDingTalkIdentityService, None),
        settings=runtime,
        allowed_origin=origin,
        trusted_proxy_cidrs=proxies,
    )


class SessionHandoffConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    purpose: Literal["school-facts-live-session-handoff"]
    runtime: LiveInitializationRuntime = Field(repr=False)
    manifest: LiveSessionHandoffApproval | LiveInitializationManifest = Field(repr=False)
    approved_sha256: str
    allowed_origin: str
    trusted_proxy_cidrs: tuple[str, ...]
    output: Path = Field(repr=False)

    @model_validator(mode="after")
    def reviewed_target(self) -> Self:
        r, m = self.runtime, self.manifest
        if not secrets.compare_digest(m.manifest_sha256, self.approved_sha256):
            raise ValueError("Reviewed handoff digest differs.")
        if (
            r.postgres_db,
            r.postgres_expected_school_id,
            r.postgres_expected_anchor_id,
            r.postgres_expected_system_identifier,
            r.dingtalk_corp_id,
        ) != (
            m.database_name,
            m.school_id,
            m.anchor_id,
            m.expected_system_identifier,
            m.corp_id,
        ) or not self.output.is_absolute():
            raise ValueError("Handoff target differs from the reviewed manifest.")
        _origin_policy(r, self.allowed_origin, self.trusted_proxy_cidrs)
        return self


@contextmanager
def reserve_protected_output(
    path: Path, *, before_release: Callable[[], None] = lambda: None
) -> Generator[BinaryIO]:
    """Reserve once in an owned 0700 directory, never following directory links.

    A failed attempt retains an empty reservation. Reusing it requires explicit
    operator recovery; parallel requests cannot replace an existing credential.
    Directory descriptors prevent a path swap from redirecting the secret write.
    """
    if os.name != "posix" or not path.is_absolute() or ".." in path.parts:
        raise ValueError("Protected handoff output requires a private Linux directory.")
    directory = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in path.parent.parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        info = os.fstat(directory)
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise ValueError("Handoff output directory is not private.")
        descriptor = os.open(
            path.name, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600, dir_fd=directory
        )
        with os.fdopen(descriptor, "wb") as stream:
            try:
                yield stream
                stream.flush()
                os.fsync(stream.fileno())
                os.fsync(directory)
                before_release()
            except BaseException:
                stream.seek(0)
                stream.truncate()
                stream.flush()
                os.fsync(stream.fileno())
                raise
    finally:
        os.close(directory)


class _ReviewedVerifier:
    def __init__(
        self,
        delegate: DingTalkIdentityVerifier,
        manifest: LiveSessionHandoffApproval | LiveInitializationManifest,
        current: Callable[[], None],
    ) -> None:
        self.delegate, self.manifest, self.current = delegate, manifest, current

    def verify(self, code: str) -> DingTalkIdentity:
        self.current()
        identity = self.delegate.verify(code)
        self.current()
        if (identity.corporation_id, identity.personnel_id) != (
            self.manifest.corp_id,
            self.manifest.external_subject,
        ):
            raise DingTalkCredentialError("Reviewed identity mismatch")
        return identity

    def fetch_optional_mobile(self, subject: str) -> str | None:
        return self.delegate.fetch_optional_mobile(subject)


def create_session_handoff_app(
    configuration: SessionHandoffConfiguration,
    *,
    sessions: sessionmaker[Session],
    redis: SessionRedisClient,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> FastAPI:
    """Explicit dependency composition; no default product wiring or business API."""
    c = configuration
    policy = _origin_policy(c.runtime, c.allowed_origin, c.trusted_proxy_cidrs)
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.middleware("http")(assign_request_id)

    @app.exception_handler(RequestValidationError)
    async def invalid_body(_request: Request, _error: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={"detail": "身份请求格式不正确，请重新从钉钉进入。"},
            headers={"Cache-Control": "no-store"},
        )

    install_dingtalk_identity_dependencies(app, settings=c.runtime, sessions=sessions, redis=redis)
    router = APIRouter(prefix=BASE)

    def current() -> None:
        try:
            c.manifest.require_current(clock())
        except ValueError:
            raise HTTPException(403, "本次身份交付窗口已关闭。") from None

    def supplied(token: str) -> LiveInitializationSession:
        return LiveInitializationSession(
            token=SecretStr(token),
            corp_id=c.manifest.corp_id,
            external_subject=c.manifest.external_subject,
        )

    def discard(store: IdentitySessionStore, token: str | None) -> None:
        if token:
            try:
                store.delete(token)
            except Exception:
                logger.error("Handoff session cleanup unavailable; operator recovery required.")

    @router.post("/dingtalk/exchange")
    def exchange(
        body: DingTalkExchangeRequest,
        request: Request,
        response: Response,
        guard: Annotated[DingTalkLoginGuard, Depends(get_dingtalk_login_guard)],
        verifier: Annotated[DingTalkIdentityVerifier, Depends(get_dingtalk_identity_verifier)],
        store: Annotated[IdentitySessionStore, Depends(get_identity_session_store)],
        session: Annotated[Session, Depends(get_session)],
    ):
        policy.require_local_request(request)
        current()
        token = None

        def access(db: Session, user: CurrentUser) -> IdentityAccessStatus:
            current()
            row = db.get(User, user.id)
            if row is None or not row.is_active or row.security_version != user.security_version:
                raise HTTPException(403, "当前账号无法使用系统。")
            return IdentityAccessStatus(
                access_state="pending_configuration", can_manage_system=False
            )

        try:
            if c.output.exists():
                raise FileExistsError
            token, _ = issue_dingtalk_identity_session(
                body,
                request,
                response,
                c.runtime,
                guard,
                cast(DingTalkIdentityVerifier, _ReviewedVerifier(verifier, c.manifest, current)),
                store,
                session,
                access_status_resolver=access,
            )
            session.close()
            identity = supplied(token)
            authenticated = require_live_initialization_account(
                c.runtime, sessions, redis, identity
            )
            identity = LiveInitializationSession.model_validate(
                dict(
                    token=SecretStr(token),
                    corp_id=identity.corp_id,
                    external_subject=identity.external_subject,
                    display_name_candidate=authenticated.identity_session.display_name,
                )
            )
            current()
            with reserve_protected_output(c.output, before_release=current) as stream:
                current()
                # SecretStr serialization is intentionally bypassed only here,
                # into the exclusive protected file consumed by the original CLI.
                stream.write(
                    json.dumps(
                        dict(
                            token=token,
                            corp_id=identity.corp_id,
                            external_subject=identity.external_subject,
                            display_name_candidate=identity.display_name_candidate,
                        )
                    ).encode("utf-8")
                )
                stream.flush()
                os.fsync(stream.fileno())
                current()
        except FileExistsError:
            discard(store, token)
            raise HTTPException(409, "已有交付记录，请核对原结果。") from None
        except HTTPException:
            session.rollback()
            discard(store, token)
            raise
        except Exception:
            session.rollback()
            discard(store, token)
            raise HTTPException(503, "身份交付未完成，请联系本次操作负责人核查。") from None
        response.headers["Cache-Control"] = "no-store"
        return {"status": "identity_delivered"}

    @router.get("/session")
    def session_status(request: Request, response: Response):
        policy.require_local_request(request)
        response.headers["Cache-Control"] = "no-store"
        token = request.cookies.get(c.runtime.identity_session_cookie_name)
        if token:
            try:
                require_live_initialization_account(c.runtime, sessions, redis, supplied(token))
                return {"status": "authenticated"}
            except CompleteIdentitySessionError as exc:
                if exc.kind == "unavailable":
                    raise HTTPException(503, "身份服务暂时不可用。") from None
        return {"status": "authentication_required"}

    @router.post("/logout")
    def logout(
        request: Request,
        response: Response,
        store: Annotated[IdentitySessionStore, Depends(get_identity_session_store)],
    ):
        policy.require_local_request(request)
        token = request.cookies.get(c.runtime.identity_session_cookie_name)
        try:
            if token:
                store.delete(token)
        except Exception:
            raise HTTPException(503, "登录会话暂时不可用。") from None
        response.delete_cookie(
            c.runtime.identity_session_cookie_name,
            path="/",
            secure=True,
            httponly=True,
            samesite="lax",
        )
        response.headers["Cache-Control"] = "no-store"
        return {"status": "logged_out"}

    app.include_router(router)
    return app


def create_app() -> FastAPI:
    """Dedicated uvicorn factory; startup errors never print configuration values."""
    try:
        path = Path(os.environ["SCHOOL_FACT_SESSION_HANDOFF_CONFIG"])
        if not path.is_absolute():
            raise ValueError("absolute configuration required")
        config = load_reviewed_contract(SessionHandoffConfiguration, path)
        config.manifest.require_current(datetime.now(UTC))
        engine = build_engine(config.runtime)
        redis = Redis.from_url(  # pyright: ignore[reportUnknownMemberType]
            config.runtime.redis_url,
            decode_responses=True,
            socket_connect_timeout=0.5,
            socket_timeout=0.5,
        )
        app = create_session_handoff_app(
            config, sessions=build_session_factory(engine), redis=redis
        )
    except Exception:
        raise RuntimeError("Temporary identity handoff configuration rejected.") from None

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncGenerator[None]:
        try:
            try:
                verify_runtime_context_signing_key(engine, config.runtime)
            except Exception:
                raise RuntimeError("Temporary identity handoff startup rejected.") from None
            yield
        finally:
            redis.close()
            engine.dispose()

    app.router.lifespan_context = lifespan
    return app
