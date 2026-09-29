from __future__ import annotations

import logging
from collections.abc import Callable
from time import perf_counter
from typing import Annotated, cast

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from redis.exceptions import RedisError
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.core.redis import get_redis_client
from app.core.settings import Settings, get_settings
from app.db.connection import get_session, get_session_factory
from app.integrations.dingtalk.client import (
    DingTalkCredentialError,
    DingTalkIdentityVerifier,
    DingTalkUnavailableError,
)
from app.integrations.dingtalk.token_provider import (
    DingTalkAppTokenProvider,
    TokenRedisClient,
)
from app.interfaces.http.errors import ApiError
from app.platform.authorization.models import UserRole
from app.platform.authorization.service import authorize
from app.platform.identity.context import (
    CompleteIdentitySession,
    CompleteIdentitySessionError,
    CurrentUser,
    resolve_complete_identity_session,
)
from app.platform.identity.context_proof import ContextProofSigner
from app.platform.identity.execution_context import (
    ActorContextResolver,
    ContextNoLongerValid,
    ContextOptionRedisClient,
    ContextResolutionUnavailable,
)
from app.platform.identity.login_guard import (
    DingTalkLoginGuard,
    DingTalkLoginGuardUnavailableError,
    DingTalkLoginMaintenanceError,
    DingTalkLoginRateLimitError,
    LoginGuardRedisClient,
)
from app.platform.identity.schemas import (
    DingTalkExchangeRequest,
    IdentityAccessStatus,
    IdentityContextsResponse,
    LogoutResponse,
)
from app.platform.identity.service import find_or_create_dingtalk_user
from app.platform.identity.session import IdentitySessionStore, SessionRedisClient

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/auth", tags=["identity"])


def get_identity_redis() -> SessionRedisClient:
    return get_redis_client()


def get_complete_identity_session_context(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
    session_factory: Annotated[sessionmaker[Session], Depends(get_session_factory)],
    x_test_subject: Annotated[str | None, Header(alias="X-Test-Subject")] = None,
    x_test_display_name: Annotated[str | None, Header(alias="X-Test-Display-Name")] = None,
) -> CompleteIdentitySession:
    """完整解析身份且在返回前关闭数据库 Session，供上下文枚举与业务入口复用。"""

    try:
        return resolve_complete_identity_session(
            settings=settings,
            session_token=request.cookies.get(settings.identity_session_cookie_name),
            test_subject=x_test_subject,
            test_display_name=x_test_display_name,
            session_factory=session_factory,
            redis=cast("SessionRedisClient", get_redis_client()),
        )
    except CompleteIdentitySessionError as exc:
        if exc.kind == "authentication_required":
            raise ApiError(
                status_code=status.HTTP_401_UNAUTHORIZED,
                code="identity.session_required",
                message="请重新从学校工作台进入系统。",
            ) from None
        if exc.kind == "access_denied":
            raise ApiError(
                status_code=status.HTTP_403_FORBIDDEN,
                code="identity.access_denied",
                message="当前账号无法使用系统，请联系学校管理员。",
            ) from None
        raise ApiError(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            code="identity.unavailable",
            message="身份服务暂时不可用，请稍后重试。",
        ) from None


def get_actor_context_resolver(
    settings: Annotated[Settings, Depends(get_settings)],
    session_factory: Annotated[sessionmaker[Session], Depends(get_session_factory)],
    complete_session: Annotated[
        CompleteIdentitySession,
        Depends(get_complete_identity_session_context),
    ],
) -> ActorContextResolver:
    """组合只会在 list/resolve 调用内短开短关数据库连接的履职解析器。"""

    return ActorContextResolver(
        session_factory=session_factory,
        redis=cast("ContextOptionRedisClient", get_redis_client()),
        proof_signer=ContextProofSigner.from_settings(settings),
        current_user=complete_session.current_user,
        identity_session=complete_session.identity_session,
    )


@router.get("/contexts", response_model=IdentityContextsResponse)
def list_execution_contexts(
    resolver: Annotated[ActorContextResolver, Depends(get_actor_context_resolver)],
) -> IdentityContextsResponse:
    """枚举本次完整身份会话可选择的最小不透明履职上下文。"""

    try:
        return IdentityContextsResponse(items=list(resolver.list_options()))
    except ContextNoLongerValid:
        raise ApiError(
            status_code=status.HTTP_409_CONFLICT,
            code="identity.context_changed",
            message="履职身份已经变化，请重新选择。",
        ) from None
    except ContextResolutionUnavailable:
        raise ApiError(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            code="identity.context_unavailable",
            message="履职身份暂时无法读取，请稍后重试。",
        ) from None


@router.get("/school-reactivation-contexts", response_model=IdentityContextsResponse)
def list_school_reactivation_contexts(
    resolver: Annotated[ActorContextResolver, Depends(get_actor_context_resolver)],
) -> IdentityContextsResponse:
    """仅枚举停用学校的当前教职工重启候选，不签发普通执行上下文。"""

    try:
        return IdentityContextsResponse(items=list(resolver.list_school_reactivation_candidates()))
    except ContextNoLongerValid:
        raise ApiError(
            status_code=status.HTTP_409_CONFLICT,
            code="identity.context_changed",
            message="履职身份已经变化，请重新选择。",
        ) from None
    except ContextResolutionUnavailable:
        raise ApiError(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            code="identity.context_unavailable",
            message="履职身份暂时无法读取，请稍后重试。",
        ) from None


def require_dingtalk_provider(
    settings: Annotated[Settings, Depends(get_settings)],
) -> Settings:
    """限制认证接口仅在钉钉身份模式启用，禁止本地测试模式伪造正式会话。"""

    if settings.identity_provider != "dingtalk":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="请从钉钉工作台打开系统。",
        )
    return settings


def get_dingtalk_identity_verifier(
    settings: Annotated[Settings, Depends(require_dingtalk_provider)],
    redis: Annotated[SessionRedisClient, Depends(get_identity_redis)],
) -> DingTalkIdentityVerifier:
    """为同步身份交换提供单请求异步 HTTP 总时限与共享应用 Token 缓存。"""

    token_provider = DingTalkAppTokenProvider.from_settings(
        redis=cast("TokenRedisClient", redis),
        settings=settings,
    )
    return DingTalkIdentityVerifier(
        settings=settings,
        token_provider=token_provider,
    )


def get_identity_session_store(
    settings: Annotated[Settings, Depends(require_dingtalk_provider)],
    redis: Annotated[SessionRedisClient, Depends(get_identity_redis)],
) -> IdentitySessionStore:
    """按受控配置提供八小时 Redis 会话存储，不让路由拼接 Redis 键或有效期。"""

    return IdentitySessionStore(
        redis=redis,
        ttl_seconds=settings.identity_session_ttl_seconds,
    )


def get_dingtalk_login_guard(
    settings: Annotated[Settings, Depends(require_dingtalk_provider)],
    redis: Annotated[SessionRedisClient, Depends(get_identity_redis)],
) -> DingTalkLoginGuard:
    """创建全实例共享的钉钉登录保护，不依赖客户端自报身份。"""

    return DingTalkLoginGuard.from_settings(
        redis=cast("LoginGuardRedisClient", redis),
        settings=settings,
    )


def determine_access_status(session: Session, current_user: CurrentUser) -> IdentityAccessStatus:
    """从当前数据库角色和授权规则派生前端访问状态，不返回角色或许可明细。"""

    has_any_role = session.scalar(select(UserRole.id).where(UserRole.user_id == current_user.id))
    can_manage_system = authorize(
        session=session,
        current_user=current_user,
        action_code="system.access.write",
        scope_type="all",
        scope_value=None,
    ).allowed
    return IdentityAccessStatus(
        access_state="ready" if has_any_role is not None else "pending_configuration",
        can_manage_system=can_manage_system,
    )


@router.post("/dingtalk/exchange", response_model=IdentityAccessStatus)
def exchange_dingtalk_auth_code(
    payload: DingTalkExchangeRequest,
    request: Request,
    response: Response,
    settings: Annotated[Settings, Depends(require_dingtalk_provider)],
    login_guard: Annotated[DingTalkLoginGuard, Depends(get_dingtalk_login_guard)],
    verifier: Annotated[DingTalkIdentityVerifier, Depends(get_dingtalk_identity_verifier)],
    identity_session_store: Annotated[IdentitySessionStore, Depends(get_identity_session_store)],
    session: Annotated[Session, Depends(get_session)],
) -> IdentityAccessStatus:
    _, access = issue_dingtalk_identity_session(
        payload,
        request,
        response,
        settings,
        login_guard,
        verifier,
        identity_session_store,
        session,
        access_status_resolver=determine_access_status,
    )
    return access


def issue_dingtalk_identity_session(
    payload: DingTalkExchangeRequest,
    request: Request,
    response: Response,
    settings: Annotated[Settings, Depends(require_dingtalk_provider)],
    login_guard: Annotated[DingTalkLoginGuard, Depends(get_dingtalk_login_guard)],
    verifier: Annotated[DingTalkIdentityVerifier, Depends(get_dingtalk_identity_verifier)],
    identity_session_store: Annotated[IdentitySessionStore, Depends(get_identity_session_store)],
    session: Annotated[Session, Depends(get_session)],
    access_status_resolver: Callable[
        [Session, CurrentUser], IdentityAccessStatus
    ] = determine_access_status,
) -> tuple[str, IdentityAccessStatus]:
    """交换一次性钉钉免登码、原子映射本地身份并设置安全的八小时 Cookie 会话。"""

    started_at = perf_counter()
    request_id = request.state.request_id
    session_token: str | None = None
    try:
        with login_guard.admit(request_id):
            identity = verifier.verify(payload.auth_code)
            trusted_mobile = verifier.fetch_optional_mobile(identity.personnel_id)
            user = find_or_create_dingtalk_user(
                session,
                identity=identity,
                request_id=request_id,
            )
            current_user = CurrentUser(
                id=user.id,
                external_subject=user.external_subject,
                display_name=user.display_name,
                security_version=user.security_version,
                trusted_mobile=trusted_mobile,
            )
            access_status = access_status_resolver(session, current_user)
            session_token = identity_session_store.create(current_user)
            session.commit()
    except DingTalkLoginMaintenanceError:
        session.rollback()
        _record_exchange_result(request_id, started_at, "maintenance")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="登录服务正在维护，请稍后重新从钉钉工作台进入。",
            headers={"Retry-After": "30"},
        ) from None
    except DingTalkLoginRateLimitError:
        session.rollback()
        _record_exchange_result(request_id, started_at, "rate_limited")
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="当前登录人数较多，请稍后重新进入。",
            headers={"Retry-After": "2"},
        ) from None
    except DingTalkLoginGuardUnavailableError:
        session.rollback()
        _record_exchange_result(request_id, started_at, "login_guard_unavailable")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="登录服务暂时不可用，请稍后重新从钉钉工作台进入。",
        ) from None
    except DingTalkCredentialError:
        session.rollback()
        _record_exchange_result(request_id, started_at, "credential_error")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="钉钉身份验证失败，请重新从工作台进入。",
        ) from None
    except DingTalkUnavailableError:
        session.rollback()
        _record_exchange_result(request_id, started_at, "dingtalk_unavailable")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="钉钉身份服务暂时不可用，请稍后重试。",
        ) from None
    except RedisError:
        session.rollback()
        _discard_session(identity_session_store, session_token)
        _record_exchange_result(request_id, started_at, "session_unavailable")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="登录会话暂时不可用，请稍后重试。",
        ) from None
    except SQLAlchemyError:
        session.rollback()
        _discard_session(identity_session_store, session_token)
        _record_exchange_result(request_id, started_at, "database_error")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="身份服务暂时不可用，请稍后重试。",
        ) from None

    response.set_cookie(
        key=settings.identity_session_cookie_name,
        value=session_token,
        max_age=settings.identity_session_ttl_seconds,
        path="/",
        secure=True,
        httponly=True,
        samesite="lax",
    )
    _record_exchange_result(request_id, started_at, "success")
    assert session_token is not None
    return session_token, access_status


@router.post("/logout", response_model=LogoutResponse)
def logout(
    request: Request,
    response: Response,
    settings: Annotated[Settings, Depends(require_dingtalk_provider)],
    identity_session_store: Annotated[IdentitySessionStore, Depends(get_identity_session_store)],
) -> LogoutResponse:
    """删除当前 Redis 会话并以同样安全属性清除浏览器 Cookie，不暴露会话内容。"""

    session_token = request.cookies.get(settings.identity_session_cookie_name)
    try:
        if session_token:
            identity_session_store.delete(session_token)
    except RedisError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="登录会话暂时不可用，请稍后重试。",
        ) from None

    response.delete_cookie(
        key=settings.identity_session_cookie_name,
        path="/",
        secure=True,
        httponly=True,
        samesite="lax",
    )
    return LogoutResponse(status="logged_out")


def _discard_session(
    identity_session_store: IdentitySessionStore,
    session_token: str | None,
) -> None:
    """在数据库提交失败后尽力删除刚创建的 Redis 会话，日志中不写会话令牌。"""

    if not session_token:
        return
    try:
        identity_session_store.delete(session_token)
    except RedisError:
        logger.warning("身份事务回滚后无法删除 Redis 会话")


def _record_exchange_result(request_id: str, started_at: float, result: str) -> None:
    """记录不含凭证和身份资料的交换耗时，供测试企业 P95/P99 验收使用。"""

    duration_ms = round((perf_counter() - started_at) * 1000, 3)
    logger.info(
        "identity_exchange_completed "
        "request_id=%s "
        "identity_provider=dingtalk "
        "result=%s "
        "duration_ms=%.3f",
        request_id,
        result,
        duration_ms,
    )
