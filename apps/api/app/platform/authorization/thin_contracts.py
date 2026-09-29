# pyright: strict

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from app.platform.contracts import AssignmentId, PersonId, SchoolId, StaffActorContext
from app.platform.governance.contracts import GovernanceAuthorizationBasis


class ThinDecisionKind(StrEnum):
    ALLOW = "allow"
    DENY = "deny"


class ThinDenyReason(StrEnum):
    UNTRUSTED_ACTOR = "untrusted_actor"
    ACTOR_CHANGED = "actor_changed"
    UNKNOWN_ACTION = "unknown_action"
    GOVERNANCE_UNAVAILABLE = "governance_unavailable"
    GOVERNANCE_AMBIGUOUS = "governance_ambiguous"
    GOVERNANCE_CHANGED = "governance_changed"
    BASIS_EXPIRED = "basis_expired"
    POLICY_CHANGED = "policy_changed"
    RESOURCE_CHANGED = "resource_changed"


@dataclass(frozen=True, slots=True)
class ThinAuthorizationRequest:
    actor: StaffActorContext
    module_name: str
    action_code: str
    scope_id: UUID
    resource_type: str
    resource_id: UUID
    resource_revision: int
    request_id: UUID
    as_of: datetime


@dataclass(frozen=True, slots=True)
class ModuleAuthorizationRequirement:
    duty_code: str
    projection_name: str
    projection_version: int
    authority_kind: str = "governance"


class ModulePolicy(Protocol):
    @property
    def module_name(self) -> str: ...

    @property
    def version(self) -> int: ...

    def requirement_for(
        self, request: ThinAuthorizationRequest
    ) -> ModuleAuthorizationRequirement | None: ...


@dataclass(frozen=True, slots=True)
class ScopePlan:
    scope_id: UUID
    owner: str
    reference: str
    version: int


@dataclass(frozen=True, slots=True)
class NamedProjection:
    name: str
    version: int


@dataclass(frozen=True, slots=True)
class StaffActorBasis:
    account_user_id: int
    school_id: SchoolId
    person_id: PersonId
    assignment_id: AssignmentId
    assignment_version: int
    authorization_version: int


@dataclass(frozen=True, slots=True)
class ThinAuthorizationBasis:
    governance: GovernanceAuthorizationBasis
    actor: StaffActorBasis
    module_name: str
    action_code: str
    module_policy_version: int
    resource_type: str
    resource_id: UUID
    resource_revision: int
    request_id: UUID
    decision_id: UUID


@dataclass(frozen=True, slots=True)
class ThinSelfAuthorizationBasis:
    """Current authenticated person relation; intentionally carries no Grant."""

    actor: StaffActorBasis
    scope_id: UUID
    module_name: str
    action_code: str
    module_policy_version: int
    resource_type: str
    resource_id: UUID
    resource_revision: int
    request_id: UUID
    decision_id: UUID


@dataclass(frozen=True, slots=True)
class ThinAuthorizationDecision:
    kind: ThinDecisionKind
    reason: ThinDenyReason | None
    scope_plan: ScopePlan | None
    projection: NamedProjection | None
    basis: ThinAuthorizationBasis | ThinSelfAuthorizationBasis | None
    request_id: UUID
    decision_id: UUID


__all__ = [
    "ModuleAuthorizationRequirement",
    "ModulePolicy",
    "NamedProjection",
    "ScopePlan",
    "StaffActorBasis",
    "ThinAuthorizationBasis",
    "ThinAuthorizationDecision",
    "ThinAuthorizationRequest",
    "ThinDecisionKind",
    "ThinDenyReason",
]
