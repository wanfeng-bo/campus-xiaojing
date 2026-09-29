"""Current authenticated DingTalk account check before first facts exist."""

from redis.exceptions import RedisError
from sqlalchemy import Connection, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.composition.school_fact_live_initialization_contracts import (
    LiveInitializationRuntime,
    LiveInitializationSession,
)
from app.platform.identity.context import (
    CompleteIdentitySession,
    CompleteIdentitySessionError,
    resolve_complete_identity_session,
)
from app.platform.identity.models import User
from app.platform.identity.session import IdentitySessionStore, SessionRedisClient


def require_live_initialization_account(
    settings: LiveInitializationRuntime,
    sessions: sessionmaker[Session],
    redis: SessionRedisClient,
    supplied: LiveInitializationSession,
) -> CompleteIdentitySession:
    if (
        type(settings) is not LiveInitializationRuntime
        or supplied.corp_id != settings.dingtalk_corp_id
    ):
        raise CompleteIdentitySessionError("access_denied")
    result = resolve_complete_identity_session(
        settings=settings,
        session_token=supplied.token.get_secret_value(),
        test_subject=None,
        test_display_name=None,
        session_factory=sessions,
        redis=redis,
    )
    try:
        with sessions() as session:
            matched = session.scalar(
                select(User.id).where(
                    User.id == result.current_user.id,
                    User.provider_code == "dingtalk",
                    User.provider_tenant_key == supplied.corp_id,
                    User.external_subject == supplied.external_subject,
                    User.is_active.is_(True),
                    User.security_version == result.current_user.security_version,
                )
            )
    except SQLAlchemyError:
        raise CompleteIdentitySessionError("unavailable") from None
    if matched is None or result.current_user.external_subject != supplied.external_subject:
        raise CompleteIdentitySessionError("access_denied")
    return result


def require_live_initialization_transaction(
    connection: Connection,
    settings: LiveInitializationRuntime,
    redis: SessionRedisClient,
    supplied: LiveInitializationSession,
) -> None:
    """Lock the exact current account in the deployment write transaction.

    Read Redis again after acquiring the row lock and at the caller's pre-commit
    checkpoint. PostgreSQL updates to this account serialize with this lock;
    Redis logout after the last read and PostgreSQL commit are not atomic.
    """
    if (
        type(settings) is not LiveInitializationRuntime
        or supplied.corp_id != settings.dingtalk_corp_id
    ):
        raise CompleteIdentitySessionError("access_denied")
    store = IdentitySessionStore(redis=redis, ttl_seconds=settings.identity_session_ttl_seconds)
    token = supplied.token.get_secret_value()
    try:
        current = store.read(token)
        if current is None or current.external_subject != supplied.external_subject:
            raise CompleteIdentitySessionError("authentication_required")
        matched = connection.execute(
            select(User.id, User.security_version)
            .where(
                User.id == current.user_id,
                User.provider_code == "dingtalk",
                User.provider_tenant_key == supplied.corp_id,
                User.external_subject == supplied.external_subject,
                User.is_active.is_(True),
                User.security_version == current.security_version,
            )
            .with_for_update()
        ).one_or_none()
        if matched is None:
            raise CompleteIdentitySessionError("access_denied")
        if store.read(token) != current:
            raise CompleteIdentitySessionError("authentication_required")
    except (RedisError, SQLAlchemyError):
        raise CompleteIdentitySessionError("unavailable") from None
