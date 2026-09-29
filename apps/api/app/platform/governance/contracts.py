# pyright: strict

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID


@dataclass(frozen=True, slots=True)
class GovernanceActorContext:
    """Actor claims that become usable only through the injected authorizer."""

    school_id: UUID
    actor_kind: str
    actor_reference: str
    actor_assignment_id: UUID | None
    actor_assignment_version: int | None
    request_id: UUID
    reason: str


@dataclass(frozen=True, slots=True)
class GovernanceWriteAuthorizationRequest:
    """Immutable canonical payload and digest bind preflight and transaction authority."""

    school_id: UUID
    command_name: str
    target_kind: str
    target_id: UUID | None
    actor: GovernanceActorContext
    command_payload_json: str = ""
    command_sha256: str = ""
    idempotency_key_sha256: str = ""


class GovernanceCommandAuthorizer(Protocol):
    """Authorizes every Governance command before its transaction starts."""

    def authorize(self, request: GovernanceWriteAuthorizationRequest) -> bool: ...


class GovernanceBootstrapError(RuntimeError):
    """Stable base for external Governance bootstrap authorization failures."""


class GovernanceBootstrapDenied(GovernanceBootstrapError):
    """The caller is not the approved external platform bootstrap authority."""


class GovernanceBootstrapClosed(GovernanceBootstrapError):
    """The persistent bootstrap state is closed and cannot be reopened or reused."""


@dataclass(frozen=True, slots=True)
class OpenGovernanceBootstrapCommand:
    school_id: UUID
    authority_reference: str
    actor: GovernanceActorContext
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class CloseGovernanceBootstrapCommand:
    school_id: UUID
    authority_reference: str
    expected_version: int
    actor: GovernanceActorContext
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class CreateDutyCommand:
    school_id: UUID
    duty_code: str
    display_name: str
    description: str | None
    effective_from: datetime
    actor: GovernanceActorContext
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class CreateScopeCommand:
    school_id: UUID
    scope_key: str
    schema_owner: str
    schema_name: str
    schema_version: int
    reference_document: dict[str, object]
    reference_sha256: str
    effective_from: datetime
    actor: GovernanceActorContext
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class CreateGrantCommand:
    school_id: UUID
    assignment_id: UUID
    duty_id: UUID
    duty_version: int
    scope_id: UUID
    scope_version: int
    basis_authority: str
    basis_reference: str
    valid_from: datetime
    effective_from: datetime
    planned_valid_to: datetime | None
    actor: GovernanceActorContext
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class UpdateGrantCommand:
    """Update only mutable Grant content; identity and Scope are deliberately absent."""

    school_id: UUID
    grant_id: UUID
    expected_version: int
    effective_from: datetime
    planned_valid_to: datetime | None
    basis_authority: str
    basis_reference: str
    actor: GovernanceActorContext
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class EndGrantCommand:
    school_id: UUID
    grant_id: UUID
    expected_version: int
    effective_at: datetime
    actor: GovernanceActorContext
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class RevokeGrantCommand:
    school_id: UUID
    grant_id: UUID
    expected_version: int
    effective_at: datetime
    actor: GovernanceActorContext
    idempotency_key: str


GovernanceCommand = (
    OpenGovernanceBootstrapCommand
    | CloseGovernanceBootstrapCommand
    | CreateDutyCommand
    | CreateScopeCommand
    | CreateGrantCommand
    | UpdateGrantCommand
    | EndGrantCommand
    | RevokeGrantCommand
)


@dataclass(frozen=True, slots=True)
class GovernanceWriteResult:
    target_id: UUID
    target_version: int
    assignment_epoch: int | None
    replayed: bool


@dataclass(frozen=True, slots=True)
class GovernanceAuthorityQuery:
    school_id: UUID
    assignment_id: UUID
    duty_code: str
    scope_id: UUID
    as_of: datetime


@dataclass(frozen=True, slots=True)
class AssignmentAuthoritySnapshot:
    school_id: UUID
    assignment_id: UUID
    person_id: UUID
    organization_id: UUID
    version: int
    is_effective: bool
    valid_until: datetime | None
    is_ambiguous: bool = False


@dataclass(frozen=True, slots=True)
class RootAuthoritySnapshot:
    school_id: UUID
    root_kind: str
    root_id: UUID
    version: int
    is_effective: bool
    valid_until: datetime | None
    is_ambiguous: bool = False


@dataclass(frozen=True, slots=True)
class ScopeResolutionRequest:
    school_id: UUID
    scope_id: UUID
    schema_owner: str
    schema_name: str
    schema_version: int
    reference_document: dict[str, object]
    reference_sha256: str
    as_of: datetime


@dataclass(frozen=True, slots=True)
class ScopeResolutionSnapshot:
    school_id: UUID
    owner: str
    reference: str
    version: int
    is_effective: bool
    is_ambiguous: bool
    valid_until: datetime | None


@dataclass(frozen=True, slots=True)
class GovernanceDependencyVersion:
    authority: str
    reference: str
    version: int


@dataclass(frozen=True, slots=True)
class GovernanceAuthoritySnapshot:
    school_id: UUID
    assignment_id: UUID
    duty_code: str
    scope_id: UUID
    grant_id: UUID
    grant_version: int
    duty_id: UUID
    duty_version: int
    scope_version: int
    assignment_epoch: int
    valid_from: datetime
    effective_from: datetime
    planned_valid_to: datetime | None
    basis_authority: str
    basis_reference: str
    valid_until: datetime | None
    dependency_versions: tuple[GovernanceDependencyVersion, ...]


@dataclass(frozen=True, slots=True)
class GovernanceAuthorizationBasis:
    school_id: UUID
    assignment_id: UUID
    duty_code: str
    scope_id: UUID
    grant_id: UUID
    grant_version: int
    assignment_epoch: int
    issued_as_of: datetime
    valid_until: datetime | None
    dependency_versions: tuple[GovernanceDependencyVersion, ...]
