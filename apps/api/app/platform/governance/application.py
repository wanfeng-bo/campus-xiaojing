# pyright: strict

from __future__ import annotations

import copy
import json
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from datetime import datetime
from hashlib import sha256
from typing import Protocol
from uuid import UUID, uuid4

from sqlalchemy.orm import Session, sessionmaker

from app.db.uow import CommitOutcomeUnknown
from app.platform.governance.contracts import (
    CloseGovernanceBootstrapCommand,
    CreateDutyCommand,
    CreateGrantCommand,
    CreateScopeCommand,
    EndGrantCommand,
    GovernanceActorContext,
    GovernanceBootstrapClosed,
    GovernanceBootstrapDenied,
    GovernanceCommand,
    GovernanceCommandAuthorizer,
    GovernanceWriteAuthorizationRequest,
    GovernanceWriteResult,
    OpenGovernanceBootstrapCommand,
    RevokeGrantCommand,
    RootAuthoritySnapshot,
    ScopeResolutionRequest,
    UpdateGrantCommand,
)
from app.platform.governance.models import (
    GovernanceAuditEvent,
    GovernanceBootstrapState,
    GovernanceCommandReceipt,
    GovernanceDuty,
    GovernanceDutyVersion,
    GovernanceGrant,
    GovernanceGrantVersion,
    GovernanceScope,
    GovernanceScopeVersion,
)
from app.platform.governance.ports import (
    GovernanceDependencyPort,
    GovernanceScopeOwnerResolver,
)
from app.platform.governance.repository import GovernanceRepository
from app.platform.governance.uow import (
    GovernanceOutcomeProbeUnitOfWork,
    GovernanceUnitOfWork,
)


class GovernanceWriteError(RuntimeError):
    """Base class for deterministic Governance Writer rejections."""


class GovernanceAuthorizationDenied(GovernanceWriteError):
    pass


class GovernanceIdempotencyConflict(GovernanceWriteError):
    pass


class GovernanceVersionConflict(GovernanceWriteError):
    pass


class GovernanceTerminalGrant(GovernanceWriteError):
    pass


class GovernanceReferenceRejected(GovernanceWriteError):
    pass


class GovernanceOutcomeVerificationFailed(GovernanceWriteError):
    pass


class GovernanceTemporalConflict(GovernanceWriteError):
    pass


class GovernanceTransactionAuthorizer(Protocol):
    """Adds trusted-session and locked-state checks inside the Writer transaction."""

    def authorize_session(
        self,
        repository: GovernanceRepository,
        request: GovernanceWriteAuthorizationRequest,
    ) -> None: ...

    def authorize_mutation(
        self,
        repository: GovernanceRepository,
        request: GovernanceWriteAuthorizationRequest,
    ) -> None: ...

    def authorize_replay(
        self,
        repository: GovernanceRepository,
        request: GovernanceWriteAuthorizationRequest,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class _MutationOutcome:
    target_id: UUID
    target_version: int
    target_kind: str
    event_type: str
    assignment_id: UUID | None
    before_summary: dict[str, object]
    after_summary: dict[str, object]


def _json_default(value: object) -> str:
    if isinstance(value, (UUID, datetime)):
        return str(value) if isinstance(value, UUID) else value.isoformat()
    raise TypeError(f"Unsupported Governance command value: {type(value).__name__}")


def _hash_text(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()


def _command_payload_json(command: GovernanceCommand, command_name: str) -> str:
    payload = asdict(command)
    payload.pop("idempotency_key", None)
    actor_payload = asdict(command.actor)
    actor_payload.pop("request_id", None)
    payload["actor"] = actor_payload
    canonical = json.dumps(
        {"command_name": command_name, "payload": payload},
        default=_json_default,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return canonical


def _command_sha256(command: GovernanceCommand, command_name: str) -> str:
    return _hash_text(_command_payload_json(command, command_name))


def _document_sha256(document: dict[str, object]) -> str:
    canonical = json.dumps(
        document,
        default=_json_default,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return _hash_text(canonical)


def _at_or_after_boundary(as_of: datetime, boundary: datetime | None) -> bool:
    return boundary is not None and as_of >= boundary


class GovernanceWriter:
    """The sole application Writer for Governance duty, Scope, and Grant authority."""

    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        authorizer: GovernanceCommandAuthorizer,
        clock: Callable[[], datetime],
        dependency_port: GovernanceDependencyPort,
        scope_owner_resolvers: Mapping[str, GovernanceScopeOwnerResolver],
        transaction_authorizer: GovernanceTransactionAuthorizer | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._authorizer = authorizer
        self._clock = clock
        self._dependency_port = dependency_port
        self._scope_owner_resolvers = dict(scope_owner_resolvers)
        self._transaction_authorizer = transaction_authorizer

    def open_governance_bootstrap(
        self, command: OpenGovernanceBootstrapCommand
    ) -> GovernanceWriteResult:
        if command.authority_reference != command.actor.actor_reference:
            raise GovernanceBootstrapDenied("Bootstrap command and actor authority do not match.")
        return self._execute(
            command,
            command_name="governance.bootstrap.open",
            target_kind="bootstrap_state",
            target_id=command.school_id,
            mutate=lambda repository: self._open_governance_bootstrap(repository, command),
        )

    def close_governance_bootstrap(
        self, command: CloseGovernanceBootstrapCommand
    ) -> GovernanceWriteResult:
        if command.authority_reference != command.actor.actor_reference:
            raise GovernanceBootstrapDenied("Bootstrap command and actor authority do not match.")
        return self._execute(
            command,
            command_name="governance.bootstrap.close",
            target_kind="bootstrap_state",
            target_id=command.school_id,
            mutate=lambda repository: self._close_governance_bootstrap(repository, command),
        )

    def create_duty(self, command: CreateDutyCommand) -> GovernanceWriteResult:
        return self._execute(
            command,
            command_name="governance.duty.create",
            target_kind="duty",
            target_id=None,
            mutate=lambda repository: self._create_duty(repository, command),
        )

    def create_scope(self, command: CreateScopeCommand) -> GovernanceWriteResult:
        command = copy.deepcopy(command)
        if _document_sha256(command.reference_document) != command.reference_sha256:
            raise GovernanceReferenceRejected("Scope canonical reference hash does not match.")
        return self._execute(
            command,
            command_name="governance.scope.create",
            target_kind="scope",
            target_id=None,
            mutate=lambda repository: self._create_scope(repository, command),
        )

    def create_grant(self, command: CreateGrantCommand) -> GovernanceWriteResult:
        return self._execute(
            command,
            command_name="governance.grant.create",
            target_kind="grant",
            target_id=None,
            mutate=lambda repository: self._create_grant(repository, command),
        )

    def update_grant(self, command: UpdateGrantCommand) -> GovernanceWriteResult:
        return self._execute(
            command,
            command_name="governance.grant.update",
            target_kind="grant",
            target_id=command.grant_id,
            mutate=lambda repository: self._change_grant(
                repository,
                school_id=command.school_id,
                grant_id=command.grant_id,
                expected_version=command.expected_version,
                transition_kind="update",
                effective_at=command.effective_from,
                planned_valid_to=command.planned_valid_to,
                basis_authority=command.basis_authority,
                basis_reference=command.basis_reference,
            ),
        )

    def end_grant(self, command: EndGrantCommand) -> GovernanceWriteResult:
        return self._execute(
            command,
            command_name="governance.grant.end",
            target_kind="grant",
            target_id=command.grant_id,
            mutate=lambda repository: self._terminal_grant(
                repository,
                school_id=command.school_id,
                grant_id=command.grant_id,
                expected_version=command.expected_version,
                transition_kind="end",
                effective_at=command.effective_at,
            ),
        )

    def revoke_grant(self, command: RevokeGrantCommand) -> GovernanceWriteResult:
        return self._execute(
            command,
            command_name="governance.grant.revoke",
            target_kind="grant",
            target_id=command.grant_id,
            mutate=lambda repository: self._terminal_grant(
                repository,
                school_id=command.school_id,
                grant_id=command.grant_id,
                expected_version=command.expected_version,
                transition_kind="revoke",
                effective_at=command.effective_at,
            ),
        )

    def _execute(
        self,
        command: GovernanceCommand,
        *,
        command_name: str,
        target_kind: str,
        target_id: UUID | None,
        mutate: Callable[[GovernanceRepository], _MutationOutcome],
    ) -> GovernanceWriteResult:
        authorization_request = self._authorize(
            command.actor,
            school_id=command.school_id,
            command_name=command_name,
            target_kind=target_kind,
            target_id=target_id,
            command_payload_json=_command_payload_json(command, command_name),
            command_sha256=_command_sha256(command, command_name),
            idempotency_key_sha256=_hash_text(command.idempotency_key),
        )
        key_hash = _hash_text(command.idempotency_key)
        request_sha256 = _command_sha256(command, command_name)
        with GovernanceUnitOfWork(self._session_factory) as uow:
            repository = uow.repository
            if self._transaction_authorizer is not None:
                self._transaction_authorizer.authorize_session(
                    repository,
                    authorization_request,
                )
            candidate = GovernanceCommandReceipt(
                id=uuid4(),
                school_id=command.school_id,
                idempotency_key_hash=key_hash,
                request_sha256=request_sha256,
                command_name=command_name,
                actor_kind=command.actor.actor_kind,
                actor_reference=command.actor.actor_reference,
                actor_assignment_id=command.actor.actor_assignment_id,
                actor_assignment_version=command.actor.actor_assignment_version,
                request_id=command.actor.request_id,
                status="started",
                target_kind=None,
                target_id=None,
                target_version=None,
                result_payload={},
            )
            receipt, acquired = repository.acquire_receipt(candidate)
            if not acquired:
                if self._transaction_authorizer is not None:
                    self._transaction_authorizer.authorize_replay(
                        repository,
                        authorization_request,
                    )
                result = self._replay(receipt, request_sha256, command_name)
                try:
                    uow.commit()
                except CommitOutcomeUnknown:
                    return self._probe_committed_result(
                        school_id=command.school_id,
                        key_hash=key_hash,
                        request_sha256=request_sha256,
                        command_name=command_name,
                    )
                return result

            if self._transaction_authorizer is not None:
                self._transaction_authorizer.authorize_mutation(
                    repository,
                    authorization_request,
                )
            outcome = mutate(repository)
            repository.flush()
            assignment_epoch = (
                repository.advance_assignment_epoch(command.school_id, outcome.assignment_id)
                if outcome.assignment_id is not None
                else None
            )
            audit = GovernanceAuditEvent(
                id=uuid4(),
                school_id=command.school_id,
                command_receipt_id=receipt.id,
                actor_kind=command.actor.actor_kind,
                actor_reference=command.actor.actor_reference,
                actor_assignment_id=command.actor.actor_assignment_id,
                actor_assignment_version=command.actor.actor_assignment_version,
                event_type=outcome.event_type,
                target_kind=outcome.target_kind,
                target_id=outcome.target_id,
                target_version=outcome.target_version,
                before_summary=outcome.before_summary,
                after_summary=outcome.after_summary,
                request_id=command.actor.request_id,
                reason=command.actor.reason,
                occurred_at=self._clock(),
            )
            repository.add(audit)
            repository.flush()

            receipt.status = "succeeded"
            receipt.target_kind = outcome.target_kind
            receipt.target_id = outcome.target_id
            receipt.target_version = outcome.target_version
            receipt.result_payload = {
                "target_id": str(outcome.target_id),
                "target_version": outcome.target_version,
                "assignment_epoch": assignment_epoch,
            }
            repository.flush()
            try:
                uow.commit()
            except CommitOutcomeUnknown:
                return self._probe_committed_result(
                    school_id=command.school_id,
                    key_hash=key_hash,
                    request_sha256=request_sha256,
                    command_name=command_name,
                )
            return GovernanceWriteResult(
                target_id=outcome.target_id,
                target_version=outcome.target_version,
                assignment_epoch=assignment_epoch,
                replayed=False,
            )

    def _probe_committed_result(
        self,
        *,
        school_id: UUID,
        key_hash: str,
        request_sha256: str,
        command_name: str,
    ) -> GovernanceWriteResult:
        with GovernanceOutcomeProbeUnitOfWork(self._session_factory) as uow:
            receipt = uow.repository.receipt(school_id, key_hash)
            if receipt is None or not uow.repository.authoritative_target_matches(receipt):
                raise GovernanceOutcomeVerificationFailed(
                    "Governance commit outcome could not be verified from authority."
                )
            try:
                return self._replay(receipt, request_sha256, command_name)
            except GovernanceIdempotencyConflict as exc:
                raise GovernanceOutcomeVerificationFailed(
                    "Governance commit outcome did not match the original command."
                ) from exc

    def lookup_command_result(
        self, command: GovernanceCommand, *, command_name: str
    ) -> GovernanceWriteResult | None:
        """Read the original durable result only; never retries a mutation."""
        with GovernanceOutcomeProbeUnitOfWork(self._session_factory) as uow:
            receipt = uow.repository.receipt(command.school_id, _hash_text(command.idempotency_key))
            if receipt is None:
                return None
            return self._replay(receipt, _command_sha256(command, command_name), command_name)

    def _authorize(
        self,
        actor: GovernanceActorContext,
        *,
        school_id: UUID,
        command_name: str,
        target_kind: str,
        target_id: UUID | None,
        command_payload_json: str,
        command_sha256: str,
        idempotency_key_sha256: str,
    ) -> GovernanceWriteAuthorizationRequest:
        if actor.school_id != school_id:
            raise GovernanceAuthorizationDenied("Actor school does not match command school.")
        request = GovernanceWriteAuthorizationRequest(
            school_id=school_id,
            command_name=command_name,
            target_kind=target_kind,
            target_id=target_id,
            actor=actor,
            command_payload_json=command_payload_json,
            command_sha256=command_sha256,
            idempotency_key_sha256=idempotency_key_sha256,
        )
        if not self._authorizer.authorize(request):
            raise GovernanceAuthorizationDenied("Governance command was denied.")
        return request

    @staticmethod
    def _replay(
        receipt: GovernanceCommandReceipt,
        request_sha256: str,
        command_name: str,
    ) -> GovernanceWriteResult:
        if receipt.command_name != command_name or receipt.request_sha256 != request_sha256:
            raise GovernanceIdempotencyConflict(
                "The idempotency key is bound to a different Governance command."
            )
        if (
            receipt.status != "succeeded"
            or receipt.target_id is None
            or receipt.target_version is None
        ):
            raise GovernanceIdempotencyConflict(
                "The idempotency key has no committed authoritative result."
            )
        epoch_value = receipt.result_payload.get("assignment_epoch")
        assignment_epoch = int(epoch_value) if epoch_value is not None else None
        return GovernanceWriteResult(
            target_id=receipt.target_id,
            target_version=receipt.target_version,
            assignment_epoch=assignment_epoch,
            replayed=True,
        )

    def _open_governance_bootstrap(
        self,
        repository: GovernanceRepository,
        command: OpenGovernanceBootstrapCommand,
    ) -> _MutationOutcome:
        existing = repository.lock_bootstrap_state(command.school_id)
        if existing is not None:
            if existing.state == "closed":
                raise GovernanceBootstrapClosed(
                    "The external Governance bootstrap authority is permanently closed."
                )
            raise GovernanceBootstrapDenied(
                "Governance bootstrap is already open under another command identity."
            )
        opened_at = self._clock()
        repository.add(
            GovernanceBootstrapState(
                school_id=command.school_id,
                authority_reference=command.authority_reference,
                state="open",
                version=1,
                opened_at=opened_at,
                closed_at=None,
            )
        )
        return _MutationOutcome(
            target_id=command.school_id,
            target_version=1,
            target_kind="bootstrap_state",
            event_type="governance.bootstrap.opened",
            assignment_id=None,
            before_summary={},
            after_summary={
                "authority_reference": command.authority_reference,
                "state": "open",
            },
        )

    def _close_governance_bootstrap(
        self,
        repository: GovernanceRepository,
        command: CloseGovernanceBootstrapCommand,
    ) -> _MutationOutcome:
        state = repository.lock_bootstrap_state(command.school_id)
        if state is None:
            raise GovernanceBootstrapDenied("Governance bootstrap has not been opened.")
        if state.state == "closed":
            raise GovernanceBootstrapClosed(
                "The external Governance bootstrap authority is permanently closed."
            )
        if state.authority_reference != command.authority_reference:
            raise GovernanceBootstrapDenied("Governance bootstrap authority does not match.")
        if state.version != command.expected_version:
            raise GovernanceVersionConflict("Governance bootstrap state version has changed.")
        before: dict[str, object] = {
            "authority_reference": state.authority_reference,
            "state": state.state,
        }
        state.state = "closed"
        state.version = command.expected_version + 1
        state.closed_at = self._clock()
        return _MutationOutcome(
            target_id=command.school_id,
            target_version=state.version,
            target_kind="bootstrap_state",
            event_type="governance.bootstrap.closed",
            assignment_id=None,
            before_summary=before,
            after_summary={
                "authority_reference": state.authority_reference,
                "state": "closed",
            },
        )

    @staticmethod
    def _create_duty(
        repository: GovernanceRepository, command: CreateDutyCommand
    ) -> _MutationOutcome:
        duty_id = uuid4()
        repository.add(
            GovernanceDuty(
                id=duty_id,
                school_id=command.school_id,
                duty_code=command.duty_code,
                current_version=1,
            )
        )
        repository.add(
            GovernanceDutyVersion(
                id=uuid4(),
                school_id=command.school_id,
                duty_id=duty_id,
                display_name=command.display_name,
                description=command.description,
                is_active=True,
                effective_from=command.effective_from,
                version=1,
            )
        )
        after: dict[str, object] = {
            "duty_code": command.duty_code,
            "display_name": command.display_name,
            "is_active": True,
        }
        return _MutationOutcome(
            target_id=duty_id,
            target_version=1,
            target_kind="duty",
            event_type="governance.duty.created",
            assignment_id=None,
            before_summary={},
            after_summary=after,
        )

    def _create_scope(
        self, repository: GovernanceRepository, command: CreateScopeCommand
    ) -> _MutationOutcome:
        scope_id = uuid4()
        resolver = self._scope_owner_resolvers.get(command.schema_owner)
        resolved = (
            None
            if resolver is None
            else resolver.resolve(
                ScopeResolutionRequest(
                    school_id=command.school_id,
                    scope_id=scope_id,
                    schema_owner=command.schema_owner,
                    schema_name=command.schema_name,
                    schema_version=command.schema_version,
                    reference_document=command.reference_document,
                    reference_sha256=command.reference_sha256,
                    as_of=self._clock(),
                )
            )
        )
        if (
            resolved is None
            or not resolved.is_effective
            or resolved.is_ambiguous
            or resolved.school_id != command.school_id
            or resolved.owner != command.schema_owner
            or resolved.version < 1
            or (resolved.valid_until is not None and self._clock() >= resolved.valid_until)
        ):
            raise GovernanceReferenceRejected(
                "Scope owner did not validate the exact current reference."
            )
        repository.add(
            GovernanceScope(
                id=scope_id,
                school_id=command.school_id,
                scope_key=command.scope_key,
                current_version=1,
            )
        )
        repository.add(
            GovernanceScopeVersion(
                id=uuid4(),
                school_id=command.school_id,
                scope_id=scope_id,
                schema_owner=command.schema_owner,
                schema_name=command.schema_name,
                schema_version=command.schema_version,
                reference_document=command.reference_document,
                reference_sha256=command.reference_sha256,
                is_active=True,
                effective_from=command.effective_from,
                version=1,
            )
        )
        after: dict[str, object] = {
            "scope_key": command.scope_key,
            "schema_owner": command.schema_owner,
            "schema_name": command.schema_name,
            "schema_version": command.schema_version,
            "reference_sha256": command.reference_sha256,
        }
        return _MutationOutcome(
            target_id=scope_id,
            target_version=1,
            target_kind="scope",
            event_type="governance.scope.created",
            assignment_id=None,
            before_summary={},
            after_summary=after,
        )

    def _create_grant(
        self,
        repository: GovernanceRepository,
        command: CreateGrantCommand,
    ) -> _MutationOutcome:
        self._require_grant_dependencies(repository, command)
        grant_id = uuid4()
        repository.add(
            GovernanceGrant(
                id=grant_id,
                school_id=command.school_id,
                assignment_id=command.assignment_id,
                duty_id=command.duty_id,
                current_version=1,
            )
        )
        repository.add(
            GovernanceGrantVersion(
                id=uuid4(),
                school_id=command.school_id,
                grant_id=grant_id,
                duty_id=command.duty_id,
                duty_version=command.duty_version,
                scope_id=command.scope_id,
                scope_version=command.scope_version,
                basis_authority=command.basis_authority,
                basis_reference=command.basis_reference,
                valid_from=command.valid_from,
                effective_from=command.effective_from,
                planned_valid_to=command.planned_valid_to,
                formal_ended_at=None,
                revoked_at=None,
                transition_kind="create",
                version=1,
            )
        )
        after = GovernanceWriter._grant_summary(
            assignment_id=command.assignment_id,
            duty_id=command.duty_id,
            duty_version=command.duty_version,
            scope_id=command.scope_id,
            scope_version=command.scope_version,
            basis_authority=command.basis_authority,
            basis_reference=command.basis_reference,
            planned_valid_to=command.planned_valid_to,
            transition_kind="create",
        )
        return _MutationOutcome(
            target_id=grant_id,
            target_version=1,
            target_kind="grant",
            event_type="governance.grant.created",
            assignment_id=command.assignment_id,
            before_summary={},
            after_summary=after,
        )

    def _require_grant_dependencies(
        self,
        repository: GovernanceRepository,
        command: CreateGrantCommand,
    ) -> None:
        as_of = command.effective_from
        try:
            assignment = self._dependency_port.read_assignment(
                school_id=command.school_id,
                assignment_id=command.assignment_id,
                as_of=as_of,
            )
        except Exception as exc:
            raise GovernanceReferenceRejected("Assignment owner failed.") from exc
        if (
            assignment is None
            or assignment.is_ambiguous
            or assignment.school_id != command.school_id
            or assignment.assignment_id != command.assignment_id
            or assignment.version < 1
            or not assignment.is_effective
            or _at_or_after_boundary(as_of, assignment.valid_until)
        ):
            raise GovernanceReferenceRejected("Assignment is not effective for this Grant.")

        try:
            person = self._dependency_port.read_person_root(
                school_id=command.school_id,
                person_id=assignment.person_id,
                as_of=as_of,
            )
        except Exception as exc:
            raise GovernanceReferenceRejected("Person owner failed.") from exc
        self._require_root_dependency(
            person,
            school_id=command.school_id,
            root_kind="person",
            root_id=assignment.person_id,
            as_of=as_of,
        )

        try:
            organization = self._dependency_port.read_organization_root(
                school_id=command.school_id,
                organization_id=assignment.organization_id,
                as_of=as_of,
            )
        except Exception as exc:
            raise GovernanceReferenceRejected("Organization owner failed.") from exc
        self._require_root_dependency(
            organization,
            school_id=command.school_id,
            root_kind="organization",
            root_id=assignment.organization_id,
            as_of=as_of,
        )

        scope = repository.scope_version(
            command.school_id,
            command.scope_id,
            command.scope_version,
        )
        if scope is None or not scope.is_active or scope.effective_from > as_of:
            raise GovernanceReferenceRejected("Governance Scope is not effective for this Grant.")
        resolver = self._scope_owner_resolvers.get(scope.schema_owner)
        if resolver is None:
            raise GovernanceReferenceRejected("Scope owner is missing.")
        request = ScopeResolutionRequest(
            school_id=command.school_id,
            scope_id=command.scope_id,
            schema_owner=scope.schema_owner,
            schema_name=scope.schema_name,
            schema_version=scope.schema_version,
            reference_document=scope.reference_document,
            reference_sha256=scope.reference_sha256,
            as_of=as_of,
        )
        try:
            target = resolver.resolve(request)
        except Exception as exc:
            raise GovernanceReferenceRejected("Scope owner failed.") from exc
        if (
            target is None
            or target.is_ambiguous
            or target.school_id != command.school_id
            or target.owner != scope.schema_owner
            or not target.reference.strip()
            or target.version < 1
            or not target.is_effective
            or _at_or_after_boundary(as_of, target.valid_until)
        ):
            raise GovernanceReferenceRejected("Scope target is not effective for this Grant.")

    @staticmethod
    def _require_root_dependency(
        value: RootAuthoritySnapshot | None,
        *,
        school_id: UUID,
        root_kind: str,
        root_id: UUID,
        as_of: datetime,
    ) -> RootAuthoritySnapshot:
        if (
            value is None
            or value.is_ambiguous
            or value.school_id != school_id
            or value.root_kind != root_kind
            or value.root_id != root_id
            or value.version < 1
            or not value.is_effective
            or _at_or_after_boundary(as_of, value.valid_until)
        ):
            raise GovernanceReferenceRejected(
                f"{root_kind.capitalize()} is not effective for this Grant."
            )
        return value

    @staticmethod
    def _require_changeable_grant(
        repository: GovernanceRepository,
        *,
        school_id: UUID,
        grant_id: UUID,
        expected_version: int,
    ) -> tuple[GovernanceGrant, GovernanceGrantVersion]:
        root = repository.lock_grant(school_id, grant_id)
        if root is None:
            raise GovernanceVersionConflict("Governance Grant does not exist in this school.")
        if root.current_version != expected_version:
            raise GovernanceVersionConflict("Governance Grant version has changed.")
        previous = repository.grant_version(school_id, grant_id, expected_version)
        if previous is None:
            raise GovernanceVersionConflict("Governance Grant content version is missing.")
        if previous.transition_kind in {"end", "revoke"}:
            raise GovernanceTerminalGrant("A terminal Governance Grant cannot be changed.")
        return root, previous

    def _change_grant(
        self,
        repository: GovernanceRepository,
        *,
        school_id: UUID,
        grant_id: UUID,
        expected_version: int,
        transition_kind: str,
        effective_at: datetime,
        planned_valid_to: datetime | None,
        basis_authority: str,
        basis_reference: str,
    ) -> _MutationOutcome:
        root, previous = self._require_changeable_grant(
            repository,
            school_id=school_id,
            grant_id=grant_id,
            expected_version=expected_version,
        )
        next_version = expected_version + 1
        before = self._grant_summary_from_rows(root, previous)
        root.current_version = next_version
        root.updated_at = self._clock()
        repository.add(
            GovernanceGrantVersion(
                id=uuid4(),
                school_id=school_id,
                grant_id=grant_id,
                duty_id=root.duty_id,
                duty_version=previous.duty_version,
                scope_id=previous.scope_id,
                scope_version=previous.scope_version,
                basis_authority=basis_authority,
                basis_reference=basis_reference,
                valid_from=previous.valid_from,
                effective_from=effective_at,
                planned_valid_to=planned_valid_to,
                formal_ended_at=None,
                revoked_at=None,
                transition_kind=transition_kind,
                version=next_version,
            )
        )
        after = self._grant_summary(
            assignment_id=root.assignment_id,
            duty_id=root.duty_id,
            duty_version=previous.duty_version,
            scope_id=previous.scope_id,
            scope_version=previous.scope_version,
            basis_authority=basis_authority,
            basis_reference=basis_reference,
            planned_valid_to=planned_valid_to,
            transition_kind=transition_kind,
        )
        return _MutationOutcome(
            target_id=grant_id,
            target_version=next_version,
            target_kind="grant",
            event_type="governance.grant.updated",
            assignment_id=root.assignment_id,
            before_summary=before,
            after_summary=after,
        )

    def _terminal_grant(
        self,
        repository: GovernanceRepository,
        *,
        school_id: UUID,
        grant_id: UUID,
        expected_version: int,
        transition_kind: str,
        effective_at: datetime,
    ) -> _MutationOutcome:
        root, _latest_written = self._require_changeable_grant(
            repository,
            school_id=school_id,
            grant_id=grant_id,
            expected_version=expected_version,
        )
        latest_effective_from = repository.latest_grant_effective_from(
            school_id,
            grant_id,
        )
        if latest_effective_from is None or effective_at < latest_effective_from:
            raise GovernanceTemporalConflict(
                "A Governance terminal transition cannot precede its latest content slice."
            )
        current = repository.effective_grant_version(
            school_id,
            grant_id,
            as_of=effective_at,
        )
        if current is None:
            raise GovernanceTemporalConflict(
                "A Governance terminal transition requires effective Grant content."
            )
        if transition_kind not in {"end", "revoke"}:
            raise ValueError("Unknown Governance terminal transition.")
        next_version = expected_version + 1
        before = self._grant_summary_from_rows(root, current)
        root.current_version = next_version
        root.updated_at = self._clock()
        repository.add(
            GovernanceGrantVersion(
                id=uuid4(),
                school_id=school_id,
                grant_id=grant_id,
                duty_id=root.duty_id,
                duty_version=current.duty_version,
                scope_id=current.scope_id,
                scope_version=current.scope_version,
                basis_authority=current.basis_authority,
                basis_reference=current.basis_reference,
                valid_from=current.valid_from,
                effective_from=effective_at,
                planned_valid_to=current.planned_valid_to,
                formal_ended_at=effective_at if transition_kind == "end" else None,
                revoked_at=effective_at if transition_kind == "revoke" else None,
                transition_kind=transition_kind,
                version=next_version,
            )
        )
        after = self._grant_summary(
            assignment_id=root.assignment_id,
            duty_id=root.duty_id,
            duty_version=current.duty_version,
            scope_id=current.scope_id,
            scope_version=current.scope_version,
            basis_authority=current.basis_authority,
            basis_reference=current.basis_reference,
            planned_valid_to=current.planned_valid_to,
            transition_kind=transition_kind,
        )
        return _MutationOutcome(
            target_id=grant_id,
            target_version=next_version,
            target_kind="grant",
            event_type=f"governance.grant.{transition_kind}",
            assignment_id=root.assignment_id,
            before_summary=before,
            after_summary=after,
        )

    @staticmethod
    def _grant_summary_from_rows(
        root: GovernanceGrant, version: GovernanceGrantVersion
    ) -> dict[str, object]:
        return GovernanceWriter._grant_summary(
            assignment_id=root.assignment_id,
            duty_id=root.duty_id,
            duty_version=version.duty_version,
            scope_id=version.scope_id,
            scope_version=version.scope_version,
            basis_authority=version.basis_authority,
            basis_reference=version.basis_reference,
            planned_valid_to=version.planned_valid_to,
            transition_kind=version.transition_kind,
        )

    @staticmethod
    def _grant_summary(
        *,
        assignment_id: UUID,
        duty_id: UUID,
        duty_version: int,
        scope_id: UUID,
        scope_version: int,
        basis_authority: str,
        basis_reference: str,
        planned_valid_to: datetime | None,
        transition_kind: str,
    ) -> dict[str, object]:
        return {
            "assignment_id": str(assignment_id),
            "duty_id": str(duty_id),
            "duty_version": duty_version,
            "scope_id": str(scope_id),
            "scope_version": scope_version,
            "basis_authority": basis_authority,
            "basis_reference": basis_reference,
            "planned_valid_to": (
                planned_valid_to.isoformat() if planned_valid_to is not None else None
            ),
            "transition_kind": transition_kind,
        }


__all__ = [
    "GovernanceAuthorizationDenied",
    "GovernanceIdempotencyConflict",
    "GovernanceOutcomeVerificationFailed",
    "GovernanceReferenceRejected",
    "GovernanceTerminalGrant",
    "GovernanceTemporalConflict",
    "GovernanceVersionConflict",
    "GovernanceWriteError",
    "GovernanceWriter",
]
