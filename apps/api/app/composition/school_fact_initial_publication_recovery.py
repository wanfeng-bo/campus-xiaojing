# pyright: strict
"""One-off renewal of the exact approved publication through the Governance Writer."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Self, cast
from uuid import UUID, uuid5

from pydantic import AwareDatetime, model_validator
from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

from app.composition.governance_dependencies import SqlAlchemyGovernanceDependencyAdapter
from app.composition.school_fact_initial_publication import (
    PUBLICATION_MODULE,
    InitialPublicationManifest,
    InitialPublicationScopeResolver,
    PublicationGrantReceipt,
    end_initial_publication_grant,
    inspect_initial_publication,
)
from app.platform.contracts import FrozenContractModel
from app.platform.governance.application import GovernanceWriter
from app.platform.governance.bootstrap import MigrationGovernanceBootstrapAuthorizer
from app.platform.governance.contracts import (
    CreateGrantCommand,
    EndGrantCommand,
    GovernanceActorContext,
    GovernanceWriteAuthorizationRequest,
)
from app.platform.governance.models import GovernanceCommandReceipt
from app.platform.governance.repository import GovernanceRepository
from app.platform.school_facts.application import SchoolFactChangeRejected
from app.platform.school_facts.contracts import Sha256Hex
from app.platform.school_facts.initialization_contracts import INITIALIZATION_HEAD


class InitialPublicationRecoveryRequest(FrozenContractModel):
    request_id: UUID
    manifest_sha256: Sha256Hex
    window_opened_at: AwareDatetime
    window_expires_at: AwareDatetime

    @model_validator(mode="after")
    def finite_window(self) -> Self:
        if self.request_id.int == 0 or not timedelta(0) < (
            self.window_expires_at - self.window_opened_at
        ) <= timedelta(minutes=15):
            raise ValueError("Recovery needs a unique request and at most fifteen minutes.")
        return self


@dataclass(frozen=True, slots=True)
class InitialPublicationRecoveryResult:
    state: str
    applied_batch_id: UUID | None
    grant: PublicationGrantReceipt | None


def require_recovery_authority(session: Session, manifest: InitialPublicationManifest) -> None:
    if session.scalar(text("SELECT SESSION_USER")) != "campus_ai_migration":
        raise SchoolFactChangeRejected("Publication recovery requires actual migration identity.")
    session.execute(text("SET LOCAL ROLE campus_ai_db_owner"))
    if tuple(session.scalars(text("SELECT version_num FROM public.alembic_version"))) != (
        INITIALIZATION_HEAD,
    ):
        raise SchoolFactChangeRejected("Publication recovery requires exact F1 schema.")
    if session.scalar(text("SELECT app.require_school_database_anchor()")) != manifest.school_id:
        raise SchoolFactChangeRejected("Publication recovery school anchor mismatch.")
    params = {
        "school": manifest.school_id,
        "assignment": manifest.assignment_id,
        "initialization": manifest.initialization_id,
    }
    epoch = session.scalar(
        text(
            "SELECT epoch FROM public.governance_assignment_epochs WHERE school_id=:school "
            "AND assignment_id=:assignment FOR UPDATE"
        ),
        params,
    )
    if type(epoch) is not int:
        raise SchoolFactChangeRejected("Publication recovery requires current assignment epoch.")
    record = (
        session.execute(
            text(
                "SELECT state, result_payload FROM public.school_fact_initializations "
                "WHERE school_id=:school AND id=:initialization FOR UPDATE"
            ),
            params,
        )
        .mappings()
        .one_or_none()
    )
    if record is None or record["state"] != "applied":
        raise SchoolFactChangeRejected("Publication initialization is missing or closed.")
    result = record["result_payload"]
    targets = cast(dict[str, object], result).get("targets") if isinstance(result, dict) else None
    if not isinstance(targets, list) or not any(
        isinstance(item, dict)
        and cast(dict[str, object], item).get("fact_kind") == "assignment"
        and cast(dict[str, object], item).get("target_id") == str(manifest.assignment_id)
        for item in cast(list[object], targets)
    ):
        raise SchoolFactChangeRejected("Publication recovery assignment is not initialized.")
    state = session.scalar(
        text(
            "SELECT state FROM public.governance_bootstrap_states "
            "WHERE school_id=:school FOR UPDATE"
        ),
        params,
    )
    if state != "open":
        raise SchoolFactChangeRejected("Publication Governance bootstrap is closed.")


class _RecoveryAuthorizer(MigrationGovernanceBootstrapAuthorizer):
    def __init__(
        self, manifest: InitialPublicationManifest, command: CreateGrantCommand | EndGrantCommand
    ) -> None:
        self.manifest, self.command = manifest, command

    def authorize(self, request: GovernanceWriteAuthorizationRequest) -> bool:
        command = self.command
        target = command.grant_id if isinstance(command, EndGrantCommand) else None
        name = (
            "governance.grant.end"
            if isinstance(command, EndGrantCommand)
            else "governance.grant.create"
        )
        return (
            request.school_id == self.manifest.school_id
            and request.command_name == name
            and request.target_kind == "grant"
            and request.target_id == target
            and request.actor == command.actor
        )

    def authorize_session(
        self, repository: GovernanceRepository, request: GovernanceWriteAuthorizationRequest
    ) -> None:
        super().authorize_session(repository, request)
        if not self.authorize(request):
            raise SchoolFactChangeRejected("Recovery command differs from the fixed command.")
        require_recovery_authority(repository._session, self.manifest)  # pyright: ignore[reportPrivateUsage]

    def _check(self, repository: GovernanceRepository, *, replay: bool) -> None:
        snapshot = inspect_initial_publication(
            repository._session,  # pyright: ignore[reportPrivateUsage]
            manifest=self.manifest,
        )
        if snapshot.applied_batch_id is not None:
            raise SchoolFactChangeRejected(
                "Publication is already applied; only closure is allowed."
            )
        command = self.command
        if isinstance(command, EndGrantCommand):
            matches = [g for g in snapshot.grants if g.grant_id == command.grant_id]
            if len(matches) != 1 or (
                not replay and matches[0].current_version != command.expected_version
            ):
                raise SchoolFactChangeRejected(
                    "Recovery may terminate only its exact temporary Grant."
                )
        elif (
            command.scope_id != snapshot.scope_id
            or command.scope_version != 1
            or command.assignment_id != self.manifest.assignment_id
            or command.duty_id != snapshot.duty_id
            or command.duty_version != snapshot.duty_version
            or (not replay and any(not g.is_terminal for g in snapshot.grants))
        ):
            raise SchoolFactChangeRejected(
                "Recovery requires the same Scope and ended prior Grants."
            )

    def authorize_mutation(
        self, repository: GovernanceRepository, request: GovernanceWriteAuthorizationRequest
    ) -> None:
        super().authorize_mutation(repository, request)
        self._check(repository, replay=False)

    def authorize_replay(
        self, repository: GovernanceRepository, request: GovernanceWriteAuthorizationRequest
    ) -> None:
        super().authorize_replay(repository, request)
        self._check(repository, replay=True)


def recover_initial_publication(
    *,
    session_factory: sessionmaker[Session],
    manifest: InitialPublicationManifest,
    request: InitialPublicationRecoveryRequest,
    now: datetime | None = None,
) -> InitialPublicationRecoveryResult:
    """Serialize result inspection, formal terminal transitions and a finite new Grant."""
    current_time = now or datetime.now(UTC)
    if request.manifest_sha256 != manifest.manifest_sha256:
        raise SchoolFactChangeRejected("Recovery cannot change the approved publication manifest.")
    with session_factory() as session, session.begin():
        require_recovery_authority(session, manifest)
        snapshot = inspect_initial_publication(session, manifest=manifest)
        # All domain writes use nested UoWs on this same physical transaction.
        nested = sessionmaker(
            bind=session.connection(),
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        )
        dependencies = SqlAlchemyGovernanceDependencyAdapter(nested)
        if snapshot.applied_batch_id is not None:
            for grant in snapshot.grants:
                if not grant.is_terminal:
                    end_initial_publication_grant(
                        session_factory=nested,
                        manifest=manifest,
                        grant=PublicationGrantReceipt(
                            snapshot.scope_id, grant.grant_id, grant.current_version
                        ),
                        dependency_port=dependencies,
                    )
            return InitialPublicationRecoveryResult("published", snapshot.applied_batch_id, None)

        def actor(step: str) -> GovernanceActorContext:
            return GovernanceActorContext(
                school_id=manifest.school_id,
                actor_kind="platform_bootstrap",
                actor_reference="deployment:campus_ai_migration",
                actor_assignment_id=None,
                actor_assignment_version=None,
                request_id=uuid5(request.request_id, step),
                reason=manifest.approval_reference,
            )

        create = CreateGrantCommand(
            school_id=manifest.school_id,
            assignment_id=manifest.assignment_id,
            duty_id=snapshot.duty_id,
            duty_version=snapshot.duty_version,
            scope_id=snapshot.scope_id,
            scope_version=1,
            basis_authority="deployment.approval",
            basis_reference=manifest.approval_reference,
            valid_from=request.window_opened_at,
            effective_from=request.window_opened_at,
            planned_valid_to=request.window_expires_at,
            actor=actor("create"),
            idempotency_key=f"f1-publication-recovery:{request.request_id}:create",
        )
        key_hash = hashlib.sha256(create.idempotency_key.encode()).hexdigest()
        prior = session.scalar(
            select(GovernanceCommandReceipt).where(
                GovernanceCommandReceipt.school_id == manifest.school_id,
                GovernanceCommandReceipt.idempotency_key_hash == key_hash,
            )
        )

        def writer(command: CreateGrantCommand | EndGrantCommand) -> GovernanceWriter:
            authorizer = _RecoveryAuthorizer(manifest, command)
            return GovernanceWriter(
                session_factory=nested,
                authorizer=authorizer,
                transaction_authorizer=authorizer,
                clock=lambda: current_time,
                dependency_port=dependencies,
                scope_owner_resolvers={PUBLICATION_MODULE: InitialPublicationScopeResolver()},
            )

        if prior is None:
            if not request.window_opened_at <= current_time < request.window_expires_at:
                raise SchoolFactChangeRejected("Recovery window is not currently open.")
            for grant in snapshot.grants:
                if not grant.is_terminal:
                    if grant.effective_from > request.window_opened_at:
                        raise SchoolFactChangeRejected(
                            "Recovery cannot predate a Grant transition."
                        )
                    end = EndGrantCommand(
                        school_id=manifest.school_id,
                        grant_id=grant.grant_id,
                        expected_version=grant.current_version,
                        effective_at=request.window_opened_at,
                        actor=actor(f"end:{grant.grant_id}"),
                        idempotency_key=f"f1-publication-recovery:{request.request_id}:end:{grant.grant_id}",
                    )
                    writer(end).end_grant(end)
        result = writer(create).create_grant(create)
        return InitialPublicationRecoveryResult(
            "window_recorded",
            None,
            PublicationGrantReceipt(snapshot.scope_id, result.target_id, result.target_version),
        )
