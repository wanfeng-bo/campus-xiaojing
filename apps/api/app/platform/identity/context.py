from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Annotated, Literal

from fastapi import Depends, Header, HTTPException, Request, status
from redis.exceptions import RedisError
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.redis import get_redis_client
from app.core.settings import Settings, get_settings
from app.db.connection import get_session
from app.platform.identity.models import User
from app.platform.identity.service import find_or_create_test_user
from app.platform.identity.session import (
    IdentitySession,
    IdentitySessionStore,
    SessionRedisClient,
)


@dataclass(frozen=True)
class CurrentUser:
    """向路由和授权能力提供已认证用户的最小可信身份上下文。"""

    id: int
    external_subject: str
    display_name: str
    security_version: int
    trusted_mobile: str | None = field(default=None, repr=False)


class CompleteIdentitySessionError(RuntimeError):
    """表示完整身份会话无法安全建立，不携带 Cookie、电话或数据库异常正文。"""

    def __init__(
        self,
        kind: Literal["authentication_required", "access_denied", "unavailable"],
    ) -> None:
        super().__init__(kind)
        self.kind = kind


@dataclass(frozen=True, slots=True)
class CompleteIdentitySession:
    """在短数据库访问结束后保留当前用户与已校验身份会话。"""

    current_user: CurrentUser
    identity_session: IdentitySession


def resolve_complete_identity_session(
    *,
    settings: Settings,
    session_token: str | None,
    test_subject: str | None,
    test_display_name: str | None,
    session_factory: Callable[[], Session],
    redis: SessionRedisClient | None,
) -> CompleteIdentitySession:
    """在函数返回前关闭身份数据库 Session，避免模型外呼占用连接。"""

    if settings.identity_provider == "local_test":
        try:
            with session_factory() as session:
                current_user = resolve_test_current_user(
                    app_env=settings.app_env,
                    subject=test_subject,
                    display_name=test_display_name,
                    session=session,
                )
        except HTTPException as exc:
            kind: Literal["authentication_required", "access_denied", "unavailable"] = (
                "access_denied"
                if exc.status_code == status.HTTP_403_FORBIDDEN
                else "authentication_required"
            )
            raise CompleteIdentitySessionError(kind) from None
        except SQLAlchemyError:
            raise CompleteIdentitySessionError("unavailable") from None
        now = datetime.now(UTC)
        nonce = sha256(
            f"local-test-context-v1\x1f{current_user.id}\x1f{current_user.security_version}\x1f{current_user.external_subject}".encode()
        ).hexdigest()
        identity_session = IdentitySession(
            user_id=current_user.id,
            external_subject=current_user.external_subject,
            display_name=current_user.display_name,
            security_version=current_user.security_version,
            session_nonce_hash=nonce,
            issued_at=now.isoformat(),
            expires_at=(now + timedelta(seconds=settings.identity_session_ttl_seconds)).isoformat(),
            trusted_mobile=None,
        )
        return CompleteIdentitySession(current_user=current_user, identity_session=identity_session)

    if redis is None or not session_token:
        raise CompleteIdentitySessionError("authentication_required")
    store = IdentitySessionStore(redis=redis, ttl_seconds=settings.identity_session_ttl_seconds)
    try:
        identity_session = store.read(session_token)
    except RedisError:
        raise CompleteIdentitySessionError("unavailable") from None
    if identity_session is None:
        raise CompleteIdentitySessionError("authentication_required")
    try:
        with session_factory() as session:
            user = session.scalar(select(User).where(User.id == identity_session.user_id))
    except SQLAlchemyError:
        raise CompleteIdentitySessionError("unavailable") from None
    if user is None or not user.is_active:
        raise CompleteIdentitySessionError("access_denied")
    if (
        user.external_subject != identity_session.external_subject
        or user.security_version != identity_session.security_version
    ):
        raise CompleteIdentitySessionError("authentication_required")
    current_user = CurrentUser(
        id=user.id,
        external_subject=user.external_subject,
        display_name=user.display_name,
        security_version=user.security_version,
        trusted_mobile=identity_session.trusted_mobile,
    )
    return CompleteIdentitySession(current_user=current_user, identity_session=identity_session)


def resolve_test_current_user(
    *,
    app_env: Literal["development", "test", "production"],
    subject: str | None,
    display_name: str | None,
    session: Session,
) -> CurrentUser:
    """仅在开发或测试环境根据受控请求头解析本地测试身份。"""

    if app_env == "production":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="当前环境不接受测试身份，请通过钉钉进入系统。",
        )
    if not subject or not display_name:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="测试身份信息不完整，请提供 X-Test-Subject 和 X-Test-Display-Name。",
        )

    user = find_or_create_test_user(session, subject, display_name)
    return CurrentUser(
        id=user.id,
        external_subject=user.external_subject,
        display_name=user.display_name,
        security_version=user.security_version,
    )


def resolve_dingtalk_current_user(
    *,
    session_token: str | None,
    session: Session,
    identity_session_store: IdentitySessionStore,
) -> CurrentUser:
    """仅从 Redis 会话解析钉钉身份，并在每次请求重新检查本地用户是否启用。"""

    if not session_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="请从钉钉工作台打开系统。",
        )
    try:
        identity_session = identity_session_store.read(session_token)
    except RedisError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="登录会话暂时不可用，请稍后重试。",
        ) from None
    if identity_session is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="请从钉钉工作台打开系统。",
        )

    user = session.scalar(select(User).where(User.id == identity_session.user_id))
    if user is None or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="当前账号已停用，请联系系统管理员。",
        )
    if user.external_subject != identity_session.external_subject:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="请从钉钉工作台打开系统。",
        )
    if user.security_version != identity_session.security_version:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="登录状态已更新，请重新从钉钉工作台进入。",
        )
    return CurrentUser(
        id=user.id,
        external_subject=user.external_subject,
        display_name=user.display_name,
        security_version=user.security_version,
        trusted_mobile=identity_session.trusted_mobile,
    )


def resolve_current_user_for_provider(
    *,
    app_env: Literal["development", "test", "production"],
    identity_provider: Literal["local_test", "dingtalk"],
    session_token: str | None,
    test_subject: str | None,
    test_display_name: str | None,
    session: Session,
    identity_session_store: IdentitySessionStore | None,
) -> CurrentUser:
    """按已配置的唯一身份提供者解析用户，钉钉模式完全忽略测试请求头。"""

    if identity_provider == "local_test":
        return resolve_test_current_user(
            app_env=app_env,
            subject=test_subject,
            display_name=test_display_name,
            session=session,
        )
    if identity_session_store is None:
        if session_token is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="请从钉钉工作台打开系统。",
            )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="登录会话暂时不可用，请稍后重试。",
        )
    return resolve_dingtalk_current_user(
        session_token=session_token,
        session=session,
        identity_session_store=identity_session_store,
    )


def get_current_user(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[Session, Depends(get_session)],
    x_test_subject: Annotated[str | None, Header(alias="X-Test-Subject")] = None,
    x_test_display_name: Annotated[str | None, Header(alias="X-Test-Display-Name")] = None,
) -> CurrentUser:
    """提供当前用户身份；钉钉模式只接受 Redis 会话，本地模式才读取测试请求头。"""

    session_token = request.cookies.get(settings.identity_session_cookie_name)
    identity_session_store = (
        IdentitySessionStore(
            redis=get_redis_client(),
            ttl_seconds=settings.identity_session_ttl_seconds,
        )
        if settings.identity_provider == "dingtalk"
        else None
    )
    return resolve_current_user_for_provider(
        app_env=settings.app_env,
        identity_provider=settings.identity_provider,
        session_token=session_token,
        test_subject=x_test_subject,
        test_display_name=x_test_display_name,
        session=session,
        identity_session_store=identity_session_store,
    )
