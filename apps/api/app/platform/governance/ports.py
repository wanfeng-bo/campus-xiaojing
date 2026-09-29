# pyright: strict

from __future__ import annotations

from datetime import datetime
from typing import Protocol
from uuid import UUID

from app.platform.governance.contracts import (
    AssignmentAuthoritySnapshot,
    RootAuthoritySnapshot,
    ScopeResolutionRequest,
    ScopeResolutionSnapshot,
)


class GovernanceDependencyPort(Protocol):
    """Owner-provided School Facts projections required by Governance."""

    def read_assignment(
        self, *, school_id: UUID, assignment_id: UUID, as_of: datetime
    ) -> AssignmentAuthoritySnapshot | None: ...

    def read_person_root(
        self, *, school_id: UUID, person_id: UUID, as_of: datetime
    ) -> RootAuthoritySnapshot | None: ...

    def read_organization_root(
        self, *, school_id: UUID, organization_id: UUID, as_of: datetime
    ) -> RootAuthoritySnapshot | None: ...


class GovernanceScopeOwnerResolver(Protocol):
    """Resolves one opaque Scope document under its declared owner's schema."""

    def resolve(self, request: ScopeResolutionRequest) -> ScopeResolutionSnapshot | None: ...


type AuthorityRootBundle = tuple[
    AssignmentAuthoritySnapshot | None, RootAuthoritySnapshot | None, RootAuthoritySnapshot | None
]


class GovernanceBundledDependencyPort(Protocol):
    """Optional single-decision owner read; None means unsupported, never denied."""

    def read_authority_roots(
        self, *, school_id: UUID, assignment_id: UUID, as_of: datetime
    ) -> AuthorityRootBundle | None: ...


__all__ = ["GovernanceDependencyPort", "GovernanceScopeOwnerResolver"]
