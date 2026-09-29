"""Finite formal first-maintainer adapter. Run with ``python -m``; no HTTP bootstrap.

The deployment operator must verify the school's actual issuer/qualification and
fixed manifest before passing its independently reviewed digest. References and
credentials are not a grant of authority. Existing domain Writers remain owners.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid5

from pydantic import BaseModel
from redis import Redis
from sqlalchemy import Connection, create_engine, event, select
from sqlalchemy.orm import Session, SessionTransaction, sessionmaker
from sqlalchemy.pool import NullPool

from app.composition.governance_dependencies import SqlAlchemyGovernanceDependencyAdapter
from app.composition.school_fact_deployment_root import (
    controlled_school_root,
    migration_session_factory,
)
from app.composition.school_fact_dingtalk_identity import (
    build_school_fact_dingtalk_identity_service,
)
from app.composition.school_fact_initial_publication import (
    PUBLICATION_MODULE,
    InitialPublicationFinalGuard,
    InitialPublicationManifest,
    InitialPublicationPolicy,
    InitialPublicationScopeResolver,
    InitialPublicationService,
    PublicationGrantReceipt,
    configure_initial_publication_grant,
    end_initial_publication_grant,
)
from app.composition.school_fact_initialization import SchoolFactInitializer
from app.composition.school_fact_initialization_readiness import FirstMaintainerReadiness
from app.composition.school_fact_live_initialization_contracts import (
    LiveInitializationConfiguration,
    LiveInitializationManifest,
    LiveInitializationMigration,
    LiveInitializationSession,
    LiveRootManifest,
)
from app.composition.school_fact_live_initialization_identity import (
    require_live_initialization_account,
    require_live_initialization_transaction,
)
from app.composition.school_fact_product_identity import ProductIdentity
from app.composition.school_fact_product_services import (
    ProductThinAuthorizationService,
    ReadOnlyGovernanceDependencies,
)
from app.composition.school_fact_reader import build_school_fact_reader_engine
from app.composition.school_fact_writer_executor import SqlAlchemySchoolFactWriterExecutorKernel
from app.composition.school_fact_writer_settings import build_school_fact_writer_engine
from app.db.connection import build_engine, build_session_factory, probe_database
from app.platform.governance.application import GovernanceWriter
from app.platform.governance.basis import GovernanceAuthorizationBasisService
from app.platform.governance.bootstrap import MigrationGovernanceBootstrapAuthorizer
from app.platform.governance.contracts import (
    CloseGovernanceBootstrapCommand,
    CreateDutyCommand,
    CreateGrantCommand,
    CreateScopeCommand,
    GovernanceActorContext,
    GovernanceWriteAuthorizationRequest,
    OpenGovernanceBootstrapCommand,
)
from app.platform.governance.models import GovernanceBootstrapState
from app.platform.governance.reader import GovernanceAuthorityReader
from app.platform.governance.repository import GovernanceRepository
from app.platform.identity.context_proof import ContextProofSigner
from app.platform.identity.session import SessionRedisClient
from app.platform.master_data_imports.models import SchoolFactInitialization
from app.platform.school_facts.contracts import ConfirmationReceiptV1
from app.platform.school_facts.initialization_contracts import INITIALIZATION_AUTHORITY
from app.platform.school_facts.read_policy import (
    SCHOOL_SCOPE_OWNER,
    SCHOOL_SCOPE_SCHEMA,
    SingleSchoolScopeResolver,
    school_scope_document_hash,
)

WRITE_ACTIONS = frozenset(
    {
        "prepare",
        "confirm",
        "governance",
        "publication-grant",
        "publication-prepare",
        "publication-confirm",
    }
)
TERMINAL_ACTIONS = frozenset({"result", "publication-result", "publication-end", "close"})


def require_action_window(manifest: LiveInitializationManifest, action: str, now: datetime) -> None:
    if action not in WRITE_ACTIONS | TERMINAL_ACTIONS:
        raise ValueError("Unsupported finite initialization action.")
    if manifest.publication_policy == "deferred" and action in {
        "publication-grant",
        "publication-prepare",
        "publication-confirm",
    }:
        raise ValueError("Initial publication is deferred by the reviewed manifest.")
    if action in WRITE_ACTIONS:
        manifest.require_current(now)


class _FiniteBootstrap(MigrationGovernanceBootstrapAuthorizer):
    """Recheck the fixed approval inside the existing Governance transaction."""

    def __init__(
        self,
        manifest: LiveInitializationManifest,
        clock: Callable[[], datetime],
        *,
        readiness: FirstMaintainerReadiness | None = None,
        initialization_id: UUID | None = None,
    ):
        self.manifest, self.clock = manifest, clock
        self.readiness, self.initialization_id = readiness, initialization_id

    def _verify_closure(
        self, repository: GovernanceRepository, request: GovernanceWriteAuthorizationRequest
    ) -> None:
        if request.command_name != "governance.bootstrap.close":
            return
        if type(self.readiness) is not FirstMaintainerReadiness or self.initialization_id is None:
            raise ValueError("Fixed publication closure verifier required.")
        session = repository._session  # pyright: ignore[reportPrivateUsage]
        original_request = self.manifest.initialization_request()
        record = session.scalar(
            select(SchoolFactInitialization).where(
                SchoolFactInitialization.school_id == self.manifest.school_id,
                SchoolFactInitialization.id == self.initialization_id,
            )
        )
        if (
            record is None
            or record.state not in {"applied", "closed"}
            or record.manifest_sha256 != original_request.manifest_sha256()
            or record.result_payload is None
        ):
            raise ValueError("The original initialization closure result is unavailable.")
        self.readiness.verify_governance_close_transaction(
            session,
            original_request,
            record.result_payload,
            initialization_id=self.initialization_id,
        )

    def authorize(self, request: GovernanceWriteAuthorizationRequest) -> bool:
        if not super().authorize(request) or request.school_id != self.manifest.school_id:
            return False
        if request.command_name != "governance.bootstrap.close":
            try:
                self.manifest.require_current(self.clock())
            except ValueError:
                return False
        return True

    def authorize_mutation(
        self, repository: GovernanceRepository, request: GovernanceWriteAuthorizationRequest
    ) -> None:
        if not self.authorize(request):
            raise ValueError("The fixed initialization approval has expired.")
        super().authorize_mutation(repository, request)
        self._verify_closure(repository, request)

    def authorize_replay(
        self, repository: GovernanceRepository, request: GovernanceWriteAuthorizationRequest
    ) -> None:
        if not self.authorize(request):
            raise ValueError("The fixed initialization approval has expired.")
        super().authorize_replay(repository, request)
        self._verify_closure(repository, request)


class LiveFirstMaintainer:
    """Owns one reviewed input and current authenticated session, never an arbitrary command."""

    def __init__(
        self,
        configuration: LiveInitializationConfiguration,
        manifest: LiveInitializationManifest,
        identity: LiveInitializationSession,
        *,
        redis: SessionRedisClient | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ):
        configuration.require_manifest(manifest)
        if (identity.corp_id, identity.external_subject) != (
            manifest.corp_id,
            manifest.external_subject,
        ):
            raise ValueError("Session identity differs from the reviewed first operator.")
        self.config, self.manifest, self.identity, self.clock = (
            configuration,
            manifest,
            identity,
            clock,
        )
        self.request = manifest.initialization_request()
        self._action: str | None = None
        self.resources = ExitStack()
        try:
            migration = create_engine(
                configuration.migration.database_url, poolclass=NullPool, hide_parameters=True
            )
            self.resources.callback(migration.dispose)
            self.sessions = migration_session_factory(migration)
            # Reader dependencies must not wait on the outer write's User row lock.
            self.dependency_sessions = migration_session_factory(migration)
            self.initializer = SchoolFactInitializer(session_factory=self.sessions, clock=clock)
            runtime = build_engine(configuration.runtime)
            self.resources.callback(runtime.dispose)
            probe_database(runtime)
            self.runtime_sessions = build_session_factory(runtime)
            if redis is None:
                live_redis = Redis.from_url(  # pyright: ignore[reportUnknownMemberType]
                    configuration.runtime.redis_url,
                    decode_responses=True,
                    socket_connect_timeout=0.5,
                    socket_timeout=0.5,
                )
                self.resources.callback(live_redis.close)
                redis = cast(SessionRedisClient, live_redis)
            self.redis = redis
            self._account()
            event.listen(self.sessions.class_, "after_begin", self._begin_transaction)
            event.listen(self.sessions.class_, "before_commit", self._commit_transaction)
        except BaseException:
            self.resources.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *_args: object) -> None:
        self.resources.close()

    def _account(self):
        return require_live_initialization_account(
            self.config.runtime, self.runtime_sessions, self.redis, self.identity
        )

    def _check(self, action: str) -> None:
        self._action = action
        require_action_window(self.manifest, action, self.clock())
        self._account()

    def _transaction_identity(self, connection: Connection) -> None:
        if self._action is None:
            raise ValueError("Formal transaction requires a finite reviewed action.")
        require_action_window(self.manifest, self._action, self.clock())
        require_live_initialization_transaction(
            connection, self.config.runtime, self.redis, self.identity
        )

    def _begin_transaction(
        self, _session: Session, _transaction: SessionTransaction, connection: Connection
    ) -> None:
        self._transaction_identity(connection)

    def _commit_transaction(self, session: Session) -> None:
        self._transaction_identity(session.connection())

    def first_facts(self, action: str, credential: str | None = None):
        if action not in {"prepare", "confirm", "result"}:
            raise ValueError("First facts action rejected.")
        self._check(action)
        if action == "prepare":
            return asdict(self.initializer.prepare(self.request))
        if action == "confirm":
            if not credential:
                raise ValueError("Original private confirmation is required.")
            return self.initializer.confirm(self.request, credential=credential)
        return self.initializer.result(self.request)

    def _applied(self, *, allow_closed: bool = False):
        receipt = self.initializer.result(self.request)
        allowed = {"applied", "closed"} if allow_closed else {"applied"}
        if (
            receipt is None
            or receipt["state"] not in allowed
            or not isinstance(receipt["result"], dict)
        ):
            raise ValueError("The original first facts are not applied and open.")
        return receipt, FirstMaintainerReadiness.assignment_from_result(
            cast(dict[str, object], receipt["result"])
        )

    def _writer(
        self,
        *,
        readiness: FirstMaintainerReadiness | None = None,
        initialization_id: UUID | None = None,
    ):
        authorizer = _FiniteBootstrap(
            self.manifest, self.clock, readiness=readiness, initialization_id=initialization_id
        )
        return GovernanceWriter(
            session_factory=self.sessions,
            authorizer=authorizer,
            transaction_authorizer=authorizer,
            clock=self.clock,
            dependency_port=SqlAlchemyGovernanceDependencyAdapter(self.dependency_sessions),
            scope_owner_resolvers={
                SCHOOL_SCOPE_OWNER: SingleSchoolScopeResolver(),
                PUBLICATION_MODULE: InitialPublicationScopeResolver(),
            },
        )

    def _actor(self, step: str):
        return GovernanceActorContext(
            school_id=self.manifest.school_id,
            actor_kind="platform_bootstrap",
            actor_reference=INITIALIZATION_AUTHORITY,
            actor_assignment_id=None,
            actor_assignment_version=None,
            request_id=uuid5(self.manifest.request_id, step),
            reason=self.request.approval_reference,
        )

    def configure_contacts(self):
        self._check("governance")
        _, assignment = self._applied()
        m, writer = self.manifest, self._writer()
        writer.open_governance_bootstrap(
            OpenGovernanceBootstrapCommand(
                m.school_id,
                INITIALIZATION_AUTHORITY,
                self._actor("open"),
                f"live-init:{m.request_id}:open",
            )
        )
        scope = writer.create_scope(
            CreateScopeCommand(
                school_id=m.school_id,
                scope_key=f"live-init:{m.request_id}:official-contacts",
                schema_owner=SCHOOL_SCOPE_OWNER,
                schema_name=SCHOOL_SCOPE_SCHEMA,
                schema_version=1,
                reference_document={"school_id": str(m.school_id)},
                reference_sha256=school_scope_document_hash(m.school_id),
                effective_from=m.grant_valid_from,
                actor=self._actor("scope"),
                idempotency_key=f"live-init:{m.request_id}:scope",
            )
        )
        grants: list[dict[str, Any]] = []
        for code in m.duties:
            duty = writer.create_duty(
                CreateDutyCommand(
                    m.school_id,
                    code,
                    code,
                    "学校明确批准的首维护官方联系职责",
                    m.grant_valid_from,
                    self._actor(code),
                    f"live-init:{m.request_id}:{code}",
                )
            )
            grant = writer.create_grant(
                CreateGrantCommand(
                    school_id=m.school_id,
                    assignment_id=assignment,
                    duty_id=duty.target_id,
                    duty_version=duty.target_version,
                    scope_id=scope.target_id,
                    scope_version=scope.target_version,
                    basis_authority="deployment.approval",
                    basis_reference=self.request.approval_reference,
                    valid_from=m.grant_valid_from,
                    effective_from=m.grant_valid_from,
                    planned_valid_to=m.grant_valid_to,
                    actor=self._actor("grant:" + code),
                    idempotency_key=f"live-init:{m.request_id}:grant:{code}",
                )
            )
            grants.append(asdict(grant))
        return {
            "scope_id": scope.target_id,
            "assignment_id": assignment,
            "grants": grants,
            "manifest_sha256": m.manifest_sha256,
        }

    def _publication(
        self, manifest: InitialPublicationManifest, *, allow_closed: bool = False
    ) -> None:
        receipt, assignment = self._applied(allow_closed=allow_closed)
        if (
            manifest.school_id != self.manifest.school_id
            or manifest.initialization_id != UUID(str(receipt["initialization_id"]))
            or manifest.assignment_id != assignment
            or manifest.approved_at < self.manifest.approved_at
            or manifest.expires_at > self.manifest.expires_at
        ):
            raise ValueError(
                "Publication must match the actual first facts and fixed deployment window."
            )

    def publication(
        self,
        action: str,
        manifest: InitialPublicationManifest,
        grant: PublicationGrantReceipt | None = None,
        confirmation: ConfirmationReceiptV1 | None = None,
    ):
        self._check(action)
        self._publication(manifest, allow_closed=action == "publication-result")
        if action == "publication-grant":
            if not manifest.approved_at <= self.clock() < manifest.expires_at:
                raise ValueError("Fixed publication window has expired.")
            return asdict(configure_initial_publication_grant(self._writer(), manifest=manifest))
        if grant is None:
            raise ValueError("Original publication grant receipt is required.")
        if action == "publication-end":
            return asdict(
                end_initial_publication_grant(
                    session_factory=self.sessions,
                    manifest=manifest,
                    grant=grant,
                    dependency_port=SqlAlchemyGovernanceDependencyAdapter(self.dependency_sessions),
                )
            )
        with ExitStack() as resources:
            reader = build_school_fact_reader_engine(self.config.reader)
            resources.callback(reader.dispose)
            read_sessions = sessionmaker(bind=reader, expire_on_commit=False)
            thin = ProductThinAuthorizationService(
                governance_basis_service=GovernanceAuthorizationBasisService(
                    session_factory=read_sessions,
                    authority_reader=GovernanceAuthorityReader(
                        session_factory=read_sessions,
                        dependency_port=ReadOnlyGovernanceDependencies(read_sessions),
                        scope_owner_resolvers={
                            PUBLICATION_MODULE: InitialPublicationScopeResolver()
                        },
                    ),
                ),
                module_policies={PUBLICATION_MODULE: InitialPublicationPolicy(manifest)},
            )
            guard = InitialPublicationFinalGuard(
                manifest=manifest,
                scope_id=grant.scope_id,
                grant_id=grant.grant_id,
                thin_service=thin,
                clock=self.clock,
            )
            identity = build_school_fact_dingtalk_identity_service(
                settings=self.config.runtime,
                session_factory=self.runtime_sessions,
                redis=self.redis,
                proof_signer=ContextProofSigner(self.config.runtime.rls_context_signing_key),
                can_maintain=(
                    (lambda _actor: False)
                    if action == "publication-result"
                    else (lambda actor: guard.require_current(actor).allowed)
                ),
            )
            token = self.identity.token.get_secret_value()
            view = identity.session_view(token)
            if len(view.contexts) != 1:
                raise ValueError("First maintainer current context must be unique.")
            identity.select_context(token, view.contexts[0].ref)
            current = identity.resolve_request(token)
            if type(current) is not ProductIdentity:
                raise ValueError("First publication requires the current staff context.")
            writer = build_school_fact_writer_engine(self.config.writer)
            resources.callback(writer.dispose)
            kernel = SqlAlchemySchoolFactWriterExecutorKernel(
                engine=writer, settings=self.config.writer, final_authorization_guard=guard
            )
            publication = InitialPublicationService(
                manifest=manifest,
                grant_id=grant.grant_id,
                kernel=kernel,
                require_current=guard.require_current,
                clock=self.clock,
            )
            if action == "publication-prepare":
                return publication.prepare(current).model_dump(mode="json", exclude_none=True)
            if action == "publication-confirm":
                if confirmation is None:
                    raise ValueError("Original publication confirmation is required.")
                return asdict(publication.confirm(current, confirmation))
            if action != "publication-result":
                raise ValueError("Publication action rejected.")
            return asdict(publication.result(current))

    def close(self, scope_id: UUID):
        self._check("close")
        receipt, _ = self._applied(allow_closed=True)
        if receipt["state"] == "closed":
            return {"state": "closed", "manifest_sha256": self.manifest.manifest_sha256}
        readiness = FirstMaintainerReadiness(
            runtime_settings=self.config.runtime,
            reader_settings=self.config.reader,
            scope_id=scope_id,
            formal_identity=self.identity,
            formal_manifest=self.manifest,
        )
        with self.sessions() as session:
            readiness.verify_publication(
                session,
                request=self.request,
                initialization_id=UUID(str(receipt["initialization_id"])),
            )
            state = session.scalar(
                select(GovernanceBootstrapState).where(
                    GovernanceBootstrapState.school_id == self.manifest.school_id
                )
            )
            if state is None:
                raise ValueError("Governance bootstrap does not exist.")
            version = state.version if state.state == "open" else state.version - 1
        readiness.verify_before_governance_close(
            self.request, cast(dict[str, object], receipt["result"])
        )
        self._writer(
            readiness=readiness, initialization_id=UUID(str(receipt["initialization_id"]))
        ).close_governance_bootstrap(
            CloseGovernanceBootstrapCommand(
                self.manifest.school_id,
                INITIALIZATION_AUTHORITY,
                version,
                self._actor("close"),
                f"live-init:{self.manifest.request_id}:close",
            )
        )
        self.initializer.close(self.request, readiness=readiness)
        return {"state": "closed", "manifest_sha256": self.manifest.manifest_sha256}


def _raw_json(path: Path) -> bytes:
    with path.open("rb") as stream:
        raw = stream.read(1_048_577)
    if len(raw) > 1_048_576:
        raise ValueError("Reviewed private input exceeds its bound.")
    # Reject duplicate keys before Pydantic's strict JSON parsing. Keep JSON types
    # intact: decoding to Python first rejects valid strict UUID/datetime inputs.
    json.loads(raw, object_pairs_hook=_unique_object)
    return raw


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Reviewed private input contains duplicate keys.")
        result[key] = value
    return result


def _read(path: Path) -> dict[str, Any]:
    value = json.loads(_raw_json(path))
    if not isinstance(value, dict):
        raise ValueError("Reviewed private input must be an object.")
    return cast(dict[str, Any], value)


def load_reviewed_contract[Contract: BaseModel](contract: type[Contract], path: Path) -> Contract:
    return contract.model_validate_json(_raw_json(path))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action", choices=sorted(WRITE_ACTIONS | TERMINAL_ACTIONS | {"root", "root-result"})
    )
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--approved-sha256", required=True)
    parser.add_argument("--session-file", type=Path)
    parser.add_argument("--confirmation", type=Path)
    parser.add_argument("--publication-manifest", type=Path)
    parser.add_argument("--publication-approved-sha256")
    parser.add_argument("--grant-receipt", type=Path)
    parser.add_argument("--scope-receipt", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.output.exists():
            raise ValueError("Receipt must be a new protected file.")
        if args.action in {"root", "root-result"}:
            manifest = load_reviewed_contract(LiveRootManifest, args.manifest)
            if not secrets.compare_digest(manifest.manifest_sha256, args.approved_sha256):
                raise ValueError("Independently reviewed root digest differs.")
            config = _read(args.config)
            if (
                set(config) != {"purpose", "migration"}
                or config["purpose"] != "school-facts-live-root"
            ):
                raise ValueError("Root configuration only contains its reviewed migration target.")
            migration = LiveInitializationMigration.model_validate_json(
                json.dumps(config["migration"])
            )
            if migration.db != manifest.db:
                raise ValueError("Root database does not match.")
            if args.action == "root":
                manifest.require_current(datetime.now(UTC))
            engine = create_engine(migration.database_url, poolclass=NullPool, hide_parameters=True)
            try:
                result = controlled_school_root(
                    engine,
                    manifest,
                    expected_database=manifest.db,
                    inspect_only=args.action == "root-result",
                )
            finally:
                engine.dispose()
        else:
            manifest = load_reviewed_contract(LiveInitializationManifest, args.manifest)
            if not secrets.compare_digest(manifest.manifest_sha256, args.approved_sha256):
                raise ValueError("Independently reviewed initialization digest differs.")
            config = load_reviewed_contract(LiveInitializationConfiguration, args.config)
            if args.session_file is None:
                raise ValueError(
                    "The real operator's protected current DingTalk session is required."
                )
            identity = load_reviewed_contract(LiveInitializationSession, args.session_file)
            with LiveFirstMaintainer(config, manifest, identity) as adapter:
                if args.action in {"prepare", "confirm", "result"}:
                    confirmation = _read(args.confirmation) if args.confirmation else {}
                    result = adapter.first_facts(args.action, confirmation.get("credential"))
                elif args.action == "governance":
                    result = adapter.configure_contacts()
                elif args.action == "close":
                    if args.scope_receipt is None:
                        raise ValueError("Original scope receipt required.")
                    scope = _read(args.scope_receipt)
                    if scope.get("manifest_sha256") != manifest.manifest_sha256:
                        raise ValueError("Scope receipt does not match the manifest.")
                    result = adapter.close(UUID(scope["scope_id"]))
                else:
                    if args.publication_manifest is None or not args.publication_approved_sha256:
                        raise ValueError("Reviewed exact publication manifest required.")
                    publication = load_reviewed_contract(
                        InitialPublicationManifest, args.publication_manifest
                    )
                    if not secrets.compare_digest(
                        publication.manifest_sha256, args.publication_approved_sha256
                    ):
                        raise ValueError("Reviewed publication digest differs.")
                    grant = None
                    if args.grant_receipt:
                        saved = _read(args.grant_receipt)
                        grant = PublicationGrantReceipt(
                            UUID(saved["scope_id"]), UUID(saved["grant_id"]), saved["grant_version"]
                        )
                    confirmation = (
                        load_reviewed_contract(ConfirmationReceiptV1, args.confirmation)
                        if args.confirmation
                        else None
                    )
                    result = adapter.publication(args.action, publication, grant, confirmation)
        descriptor = os.open(args.output, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(result, stream, ensure_ascii=False, default=str)
            stream.write("\n")
        print("Formal initialization acknowledged; protected receipt written.")
        return 0
    except Exception:
        print(
            "Formal initialization rejected or outcome unknown; "
            "inspect original protected receipts before retry.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
