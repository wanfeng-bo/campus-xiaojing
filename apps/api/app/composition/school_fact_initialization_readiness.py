# pyright: strict
"""Fixed initialization closure checks; no caller-supplied success callback."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import cast
from uuid import UUID, uuid4

from redis import Redis
from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

from app.composition.school_fact_live_initialization_contracts import (
    LiveInitializationManifest,
    LiveInitializationRuntime,
    LiveInitializationSession,
)
from app.composition.school_fact_live_initialization_identity import (
    require_live_initialization_account,
)
from app.composition.school_fact_product_identity import build_school_fact_product_identity_service
from app.composition.school_fact_product_services import create_product_thin_service
from app.composition.school_fact_query_repository import OfficialContactFinalAuthorizationGuard
from app.composition.school_fact_reader import (
    SchoolFactReaderSettings,
    build_school_fact_reader_engine,
)
from app.core.settings import Settings
from app.db.connection import build_engine, build_session_factory, probe_database
from app.platform.authorization.thin_contracts import ThinAuthorizationRequest, ThinDecisionKind
from app.platform.contracts import StaffActorContext
from app.platform.governance.models import GovernanceBootstrapState
from app.platform.identity.context_proof import ContextProofSigner
from app.platform.identity.execution_context import ActorContextResolver
from app.platform.identity.session import SessionRedisClient
from app.platform.school_facts.initialization_contracts import (
    SchoolFactInitializationRejected,
    SchoolFactInitializationRequest,
)
from app.platform.school_facts.read_policy import (
    OFFICIAL_CONTACT_MODULE,
    OFFICIAL_CONTACT_READ,
    OFFICIAL_CONTACTS,
)


class FirstMaintainerReadiness:
    """Always builds the real Identity resolver and Thin basis from checked component settings."""

    def __init__(
        self,
        *,
        runtime_settings: Settings,
        reader_settings: SchoolFactReaderSettings,
        scope_id: UUID,
        formal_identity: LiveInitializationSession | None = None,
        formal_manifest: LiveInitializationManifest | None = None,
    ) -> None:
        formal = formal_identity is not None
        if (
            (not formal and runtime_settings.app_env not in {"development", "test"})
            or (formal and type(runtime_settings) is not LiveInitializationRuntime)
            or (formal and type(formal_identity) is not LiveInitializationSession)
            or runtime_settings.postgres_schema_contract != "school-facts-f1-v22"
            or reader_settings.schema_contract != "school-facts-f1-v22"
            or runtime_settings.postgres_user != "campus_ai_runtime"
            or (not formal and runtime_settings.postgres_db != "campus_ai_school_facts_f1")
            or reader_settings.database_name != runtime_settings.postgres_db
            or (formal and reader_settings.database_host != runtime_settings.postgres_host)
            or (formal and reader_settings.database_port != runtime_settings.postgres_port)
            or runtime_settings.postgres_expected_school_id != reader_settings.expected_school_id
            or runtime_settings.postgres_expected_anchor_id != reader_settings.expected_anchor_id
            or runtime_settings.postgres_expected_system_identifier
            != reader_settings.expected_system_identifier
            or type(scope_id) is not UUID
        ):
            raise SchoolFactInitializationRejected("Initialization readiness settings rejected.")
        if formal_manifest is not None:
            try:
                if type(formal_manifest) is not LiveInitializationManifest or not formal:
                    raise ValueError("A real formal identity and manifest are required.")
                formal_manifest = LiveInitializationManifest.model_validate(
                    formal_manifest.model_dump(mode="python", exclude_none=True)
                )
                if (
                    formal_identity is None
                    or (formal_identity.corp_id, formal_identity.external_subject)
                    != (formal_manifest.corp_id, formal_manifest.external_subject)
                    or formal_manifest.database_name != runtime_settings.postgres_db
                    or formal_manifest.school_id != reader_settings.expected_school_id
                    or formal_manifest.anchor_id != reader_settings.expected_anchor_id
                    or formal_manifest.expected_system_identifier
                    != reader_settings.expected_system_identifier
                ):
                    raise ValueError("Formal initialization manifest binding differs.")
            except (TypeError, ValueError) as exc:
                raise SchoolFactInitializationRejected(
                    "Initialization readiness manifest rejected."
                ) from exc
        self._formal_manifest = formal_manifest
        self._runtime_settings = runtime_settings
        self._reader_settings = reader_settings
        self._scope_id = scope_id
        self._formal_identity = formal_identity

    def verify_before_governance_close(
        self,
        request: SchoolFactInitializationRequest,
        result: dict[str, object],
    ) -> None:
        self._verify_identity_and_duties(request, result)

    def verify_governance_close_transaction(
        self,
        session: Session,
        request: SchoolFactInitializationRequest,
        result: dict[str, object],
        *,
        initialization_id: UUID,
    ) -> None:
        # Bootstrap is already locked. Publication-end takes epoch first; never
        # wait here and form the opposite lock cycle. Contention rejects this
        # attempt without closing either authority; the same manifest may retry.
        assignment_id = self.assignment_from_result(result)
        session.execute(
            text(
                "SELECT epoch FROM public.governance_assignment_epochs "
                "WHERE school_id=:school_id AND assignment_id=:assignment_id FOR SHARE NOWAIT"
            ),
            {"school_id": request.school_id, "assignment_id": assignment_id},
        ).scalar_one()
        self.verify_publication(session, request=request, initialization_id=initialization_id)
        self._verify_identity_and_duties(request, result)

    def verify(
        self,
        session: Session,
        request: SchoolFactInitializationRequest,
        result: dict[str, object],
        *,
        initialization_id: UUID,
    ) -> None:
        bootstrap = session.scalar(
            select(GovernanceBootstrapState)
            .where(
                GovernanceBootstrapState.school_id == request.school_id,
            )
            .with_for_update(read=True)
        )
        if bootstrap is None or bootstrap.state != "closed":
            raise SchoolFactInitializationRejected("Governance bootstrap is not closed.")
        self.verify_publication(session, request=request, initialization_id=initialization_id)
        assignment_id = self.assignment_from_result(result)
        # Hold this lock while both duties and the real context are re-read.
        session.execute(
            text(
                "SELECT epoch FROM public.governance_assignment_epochs "
                "WHERE school_id=:school_id AND assignment_id=:assignment_id FOR SHARE"
            ),
            {"school_id": request.school_id, "assignment_id": assignment_id},
        ).scalar_one()
        self._verify_identity_and_duties(request, result)

    def verify_publication(
        self, session: Session, *, request: SchoolFactInitializationRequest, initialization_id: UUID
    ) -> None:
        from app.composition.school_fact_initial_publication import (
            verify_initial_publication_absent,
            verify_initial_publication_closed,
        )

        manifest = self._formal_manifest
        if manifest is not None and request != manifest.initialization_request():
            raise SchoolFactInitializationRejected("Initialization request differs from manifest.")
        if manifest is not None and manifest.publication_policy == "deferred":
            verify_initial_publication_absent(
                session, school_id=request.school_id, initialization_id=initialization_id
            )
        else:
            verify_initial_publication_closed(
                session, request=request, initialization_id=initialization_id
            )

    @staticmethod
    def assignment_from_result(result: dict[str, object]) -> UUID:
        targets = result.get("targets")
        if not isinstance(targets, list):
            raise SchoolFactInitializationRejected("Initialization result is incomplete.")
        rows = cast(list[dict[str, object]], targets)
        assignments = [row for row in rows if row.get("fact_kind") == "assignment"]
        if len(assignments) != 1:
            raise SchoolFactInitializationRejected("Initialization assignment is ambiguous.")
        return UUID(str(assignments[0]["target_id"]))

    def _verify_identity_and_duties(
        self,
        request: SchoolFactInitializationRequest,
        result: dict[str, object],
    ) -> None:
        if (
            request.school_id != self._reader_settings.expected_school_id
            or request.anchor_id != self._reader_settings.expected_anchor_id
            or request.expected_system_identifier
            != self._reader_settings.expected_system_identifier
        ):
            raise SchoolFactInitializationRejected("Initialization readiness anchor changed.")
        runtime = build_engine(self._runtime_settings)
        reader = None
        redis: Redis | None = None
        token: str | None = None
        identity = None
        try:
            probe_database(runtime)
            reader = build_school_fact_reader_engine(self._reader_settings)
            thin = create_product_thin_service(sessionmaker(bind=reader, expire_on_commit=False))
            guard = OfficialContactFinalAuthorizationGuard(
                thin_service=thin, scope_id=self._scope_id
            )
            redis = Redis.from_url(  # pyright: ignore[reportUnknownMemberType]
                self._runtime_settings.redis_url,
                decode_responses=True,
                socket_connect_timeout=0.5,
                socket_timeout=0.5,
            )
            if self._formal_identity is not None:
                account = require_live_initialization_account(
                    cast(LiveInitializationRuntime, self._runtime_settings),
                    build_session_factory(runtime),
                    cast(SessionRedisClient, redis),
                    self._formal_identity,
                )
                resolver = ActorContextResolver(
                    session_factory=build_session_factory(runtime),
                    redis=cast(SessionRedisClient, redis),
                    proof_signer=ContextProofSigner(self._runtime_settings.rls_context_signing_key),
                    current_user=account.current_user,
                    identity_session=account.identity_session,
                )
                options = resolver.list_options()
                if len(options) != 1:
                    raise SchoolFactInitializationRejected(
                        "First maintainer context is not unique."
                    )
                actor = resolver.resolve(options[0].opaque_ref)
            else:
                identity = build_school_fact_product_identity_service(
                    app_env=self._runtime_settings.app_env,
                    session_factory=build_session_factory(runtime),
                    redis=cast(SessionRedisClient, redis),
                    proof_signer=ContextProofSigner(self._runtime_settings.rls_context_signing_key),
                    can_maintain=lambda actor: guard.require_current(actor).allowed,
                )
                token = identity.login("maintainer")
                view = identity.session_view(token)
                if len(view.contexts) != 1:
                    raise SchoolFactInitializationRejected(
                        "First maintainer context is not unique."
                    )
                identity.select_context(token, view.contexts[0].ref)
                actor = identity.resolve_request(token).actor
            if self._formal_identity is not None and type(actor) is not StaffActorContext:
                raise SchoolFactInitializationRejected("First maintainer must be current staff.")
            actor = cast(StaffActorContext, actor)
            if (
                actor.school_id != request.school_id
                or actor.assignment_id != self.assignment_from_result(result)
            ):
                raise SchoolFactInitializationRejected("First maintainer identity does not match.")
            guard.require_current(actor)
            read = thin.decide(
                ThinAuthorizationRequest(
                    actor=actor,
                    module_name=OFFICIAL_CONTACT_MODULE,
                    action_code=OFFICIAL_CONTACT_READ,
                    scope_id=self._scope_id,
                    resource_type=OFFICIAL_CONTACTS,
                    resource_id=request.school_id,
                    resource_revision=actor.authorization_version,
                    request_id=uuid4(),
                    as_of=datetime.now(UTC),
                )
            )
            if read.kind is not ThinDecisionKind.ALLOW or read.basis is None:
                raise SchoolFactInitializationRejected(
                    "First maintainer requires explicit READ duty."
                )
            rechecked = thin.revalidate(
                read.basis,
                actor=actor,
                as_of=datetime.now(UTC),
                resource_revision=actor.authorization_version,
            )
            if rechecked.kind is not ThinDecisionKind.ALLOW:
                raise SchoolFactInitializationRejected("First maintainer READ duty changed.")
        finally:
            try:
                if token is not None and identity is not None and self._formal_identity is None:
                    identity.logout(token)
            finally:
                if redis is not None:
                    redis.close()
                if reader is not None:
                    reader.dispose()
                runtime.dispose()
