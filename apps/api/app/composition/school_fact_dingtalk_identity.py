from __future__ import annotations

from collections.abc import Callable

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.composition.school_fact_product_identity import (
    ProductActorContextResolver,
    ProductIdentityRejected,
    ProductIdentityUnavailable,
    ProductSessionView,
    SchoolFactIdentitySessionService,
)
from app.core.settings import Settings
from app.platform.contracts import StaffActorContext
from app.platform.identity.context import (
    CompleteIdentitySession,
    CompleteIdentitySessionError,
    CurrentUser,
    resolve_complete_identity_session,
)
from app.platform.identity.context_proof import ContextProofSigner
from app.platform.identity.execution_context import ActorContextResolver
from app.platform.identity.models import User
from app.platform.identity.session import IdentitySession, SessionRedisClient


class SchoolFactDingTalkIdentityService(SchoolFactIdentitySessionService):
    """Consumes genuine DingTalk sessions; never accepts an account selector."""

    def __init__(
        self,
        *,
        complete_session: Callable[[str | None], CompleteIdentitySession],
        redis: SessionRedisClient,
        resolver_factory: Callable[[CurrentUser, IdentitySession], ProductActorContextResolver],
        can_maintain: Callable[[StaffActorContext], bool],
        family_enabled: bool = False,
        ttl_seconds: int = 28800,
    ) -> None:
        super().__init__(
            redis=redis,
            resolver_factory=resolver_factory,
            can_maintain=can_maintain,
            family_enabled=family_enabled,
            ttl_seconds=ttl_seconds,
        )
        self._complete_session = complete_session

    def _complete(self, token: str | None) -> tuple[CurrentUser, IdentitySession]:
        try:
            complete = self._complete_session(token)
            return complete.current_user, complete.identity_session
        except CompleteIdentitySessionError as exc:
            if exc.kind == "unavailable":
                raise ProductIdentityUnavailable("当前身份暂时无法核验。") from None
            raise ProductIdentityRejected("请重新从钉钉进入。") from None

    def session_view(self, token: str | None) -> ProductSessionView:
        # Only fresh sessions auto-enter. Changed/revoked selections cannot resurrect.
        if token:
            try:
                user, identity = self._complete(token)
                if self._get(self._state_key(identity)) is None:
                    state = self._new_options(token, user, identity)
                    automatic = len(state.contexts) == 1
                    if state.contexts and all(
                        c.kind == "guardian_relationship" for c in state.contexts
                    ):
                        actors = [self._resolve(user, identity, c.ref) for c in state.contexts]
                        automatic = len({(a.school_id, a.person_id) for a in actors}) == 1
                    if automatic:
                        self.select_context(token, state.contexts[0].ref)
            except ProductIdentityRejected:
                return super().session_view(None)
        return super().session_view(token)


def build_school_fact_dingtalk_identity_service(
    *,
    settings: Settings,
    session_factory: sessionmaker[Session],
    redis: SessionRedisClient,
    proof_signer: ContextProofSigner,
    can_maintain: Callable[[StaffActorContext], bool],
    family_enabled: bool = False,
) -> SchoolFactDingTalkIdentityService:
    if settings.identity_provider != "dingtalk" or not settings.dingtalk_corp_id:
        raise ValueError("formal product requires the configured DingTalk organization")
    if settings.identity_session_cookie_name in {
        "campus_ai_session",
        "school_fact_product_session",
    }:
        raise ValueError("formal product requires an isolated identity cookie")

    def complete(token: str | None) -> CompleteIdentitySession:
        result = resolve_complete_identity_session(
            settings=settings,
            session_token=token,
            test_subject=None,
            test_display_name=None,
            session_factory=session_factory,
            redis=redis,
        )
        try:
            with session_factory() as session:
                matches = session.scalar(
                    select(User.id).where(
                        User.id == result.current_user.id,
                        User.provider_code == "dingtalk",
                        User.provider_tenant_key == settings.dingtalk_corp_id,
                        User.external_subject == result.current_user.external_subject,
                        User.security_version == result.current_user.security_version,
                        User.is_active.is_(True),
                    )
                )
        except SQLAlchemyError:
            raise CompleteIdentitySessionError("unavailable") from None
        if matches is None:
            raise CompleteIdentitySessionError("access_denied")
        return result

    return SchoolFactDingTalkIdentityService(
        complete_session=complete,
        redis=redis,
        resolver_factory=lambda user, identity: ActorContextResolver(
            session_factory=session_factory,
            redis=redis,
            proof_signer=proof_signer,
            current_user=user,
            identity_session=identity,
        ),
        can_maintain=can_maintain,
        family_enabled=family_enabled,
        ttl_seconds=settings.identity_session_ttl_seconds,
    )
