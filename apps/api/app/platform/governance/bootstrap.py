# pyright: strict

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime

from sqlalchemy.orm import Session, sessionmaker

from app.platform.governance.application import GovernanceWriter
from app.platform.governance.contracts import (
    CloseGovernanceBootstrapCommand,
    GovernanceBootstrapClosed,
    GovernanceBootstrapDenied,
    GovernanceWriteAuthorizationRequest,
    OpenGovernanceBootstrapCommand,
)
from app.platform.governance.ports import (
    GovernanceDependencyPort,
    GovernanceScopeOwnerResolver,
)
from app.platform.governance.repository import GovernanceRepository

_MIGRATION_SESSION_USER = "campus_ai_migration"
_MIGRATION_AUTHORITY_REFERENCE = "deployment:campus_ai_migration"
_BOOTSTRAP_COMMANDS = frozenset(
    {
        "governance.bootstrap.open",
        "governance.bootstrap.close",
        "governance.duty.create",
        "governance.scope.create",
        "governance.grant.create",
    }
)


class MigrationGovernanceBootstrapAuthorizer:
    """Authenticates the deployment trust root and serializes its closeable state."""

    def authorize(self, request: GovernanceWriteAuthorizationRequest) -> bool:
        actor = request.actor
        return (
            request.command_name in _BOOTSTRAP_COMMANDS
            and actor.actor_kind == "platform_bootstrap"
            and actor.actor_reference == _MIGRATION_AUTHORITY_REFERENCE
            and actor.actor_assignment_id is None
            and actor.actor_assignment_version is None
            and bool(actor.reason.strip())
        )

    def authorize_session(
        self,
        repository: GovernanceRepository,
        request: GovernanceWriteAuthorizationRequest,
    ) -> None:
        del request
        if repository.session_user() != _MIGRATION_SESSION_USER:
            raise GovernanceBootstrapDenied(
                "Only the migration deployment principal may use Governance bootstrap."
            )

    def authorize_mutation(
        self,
        repository: GovernanceRepository,
        request: GovernanceWriteAuthorizationRequest,
    ) -> None:
        state = repository.lock_bootstrap_state(request.school_id)
        if request.command_name == "governance.bootstrap.open":
            if state is None:
                return
            if state.state == "closed":
                raise GovernanceBootstrapClosed(
                    "The external Governance bootstrap authority is permanently closed."
                )
            raise GovernanceBootstrapDenied(
                "Governance bootstrap is already open under another command identity."
            )
        if state is None:
            raise GovernanceBootstrapDenied("Governance bootstrap is not open for this school.")
        if state.state == "closed":
            raise GovernanceBootstrapClosed(
                "The external Governance bootstrap authority is permanently closed."
            )
        if state.authority_reference != request.actor.actor_reference:
            raise GovernanceBootstrapDenied("Governance bootstrap authority does not match.")

    def authorize_replay(
        self,
        repository: GovernanceRepository,
        request: GovernanceWriteAuthorizationRequest,
    ) -> None:
        state = repository.lock_bootstrap_state(request.school_id)
        if state is None:
            raise GovernanceBootstrapDenied("Governance bootstrap is not open for this school.")
        if state.authority_reference != request.actor.actor_reference:
            raise GovernanceBootstrapDenied("Governance bootstrap authority does not match.")
        if request.command_name == "governance.bootstrap.close":
            if state.state == "closed":
                return
            raise GovernanceBootstrapDenied("Governance bootstrap close has not completed.")
        if state.state == "closed":
            raise GovernanceBootstrapClosed(
                "The external Governance bootstrap authority is permanently closed."
            )


def build_migration_bootstrap_writer(
    *,
    session_factory: sessionmaker[Session],
    clock: Callable[[], datetime],
    dependency_port: GovernanceDependencyPort,
    scope_owner_resolvers: Mapping[str, GovernanceScopeOwnerResolver],
) -> GovernanceWriter:
    """Build the sole Writer with the external deployment trust root, empty by default."""

    authorizer = MigrationGovernanceBootstrapAuthorizer()
    return GovernanceWriter(
        session_factory=session_factory,
        authorizer=authorizer,
        transaction_authorizer=authorizer,
        clock=clock,
        dependency_port=dependency_port,
        scope_owner_resolvers=scope_owner_resolvers,
    )


__all__ = [
    "CloseGovernanceBootstrapCommand",
    "GovernanceBootstrapClosed",
    "GovernanceBootstrapDenied",
    "MigrationGovernanceBootstrapAuthorizer",
    "OpenGovernanceBootstrapCommand",
    "build_migration_bootstrap_writer",
]
