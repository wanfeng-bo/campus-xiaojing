"""Independent F7 duty for reading only the terminal status of one's own batch."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID

from app.platform.authorization.secure_reader_adapters import ThinAuthorizationReadAdapter
from app.platform.authorization.secure_reader_contracts import ReadAuthorization, SecureReadDenied
from app.platform.authorization.secure_reader_registry import ReadOperation
from app.platform.authorization.thin_contracts import (
    ModuleAuthorizationRequirement,
    NamedProjection,
    ThinAuthorizationBasis,
    ThinAuthorizationRequest,
)
from app.platform.contracts import CommandExecutionContext, StaffActorContext
from app.platform.school_facts.application import SchoolFactChangeRejected
from app.platform.school_facts.read_policy import (
    SCHOOL_SCOPE_OWNER,
    SchoolFactAuthorizationUnavailable,
)

F7_OWN_TERMINAL_MODULE = "school_facts.f7.own_terminal"
F7_OWN_TERMINAL_OPERATION = "school_facts.f7.own_terminal.read"
F7_OWN_TERMINAL_DUTY = "f7_own_terminal_reader"
_PROJECTION = "f7_own_terminal_status"
_OPERATION = ReadOperation(
    code=F7_OWN_TERMINAL_MODULE,
    projection_name=_PROJECTION,
    action_code=F7_OWN_TERMINAL_OPERATION,
    module_name=F7_OWN_TERMINAL_MODULE,
)


class F7OwnTerminalPolicy:
    """No fact-content authority and no inference from a staff title or old F2 grant."""

    module_name = F7_OWN_TERMINAL_MODULE
    version = 1

    def requirement_for(
        self, request: ThinAuthorizationRequest
    ) -> ModuleAuthorizationRequirement | None:
        if (
            type(request.actor) is not StaffActorContext
            or request.module_name != self.module_name
            or request.action_code != F7_OWN_TERMINAL_OPERATION
            or request.resource_type != F7_OWN_TERMINAL_MODULE
            or request.resource_id != UUID(str(request.actor.school_id))
            or request.resource_revision != request.actor.authorization_version
        ):
            return None
        return ModuleAuthorizationRequirement(F7_OWN_TERMINAL_DUTY, _PROJECTION, 1)


@dataclass(frozen=True, slots=True)
class F7OwnTerminalAuthorization:
    _adapter: ThinAuthorizationReadAdapter = field(repr=False)
    _actor: StaffActorContext = field(repr=False)
    _issued: ReadAuthorization = field(repr=False)
    _clock: Callable[[], datetime] = field(repr=False)

    def revalidate(self) -> None:
        try:
            if not self._adapter.revalidate(self._issued, actor=self._actor, as_of=self._clock()):
                raise SecureReadDenied("Own terminal authority changed before release.")
        except SecureReadDenied:
            raise
        except Exception as exc:
            raise SchoolFactAuthorizationUnavailable("Own terminal authority unavailable.") from exc


class F7OwnTerminalAuthorizer:
    def __init__(
        self,
        *,
        thin_service: object,
        school_scope_id: UUID,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._scope_id = school_scope_id
        self._clock = clock or (lambda: datetime.now(UTC))
        self._adapter = ThinAuthorizationReadAdapter(
            thin_service=thin_service,
            scope_id_resolver=lambda _operation, _actor: school_scope_id,
        )

    def __call__(self, context: CommandExecutionContext) -> F7OwnTerminalAuthorization:
        if (
            type(context) is not CommandExecutionContext
            or type(context.actor) is not StaffActorContext
        ):
            raise SchoolFactChangeRejected("Current staff identity is required for own terminal.")
        actor = context.actor
        try:
            result = self._adapter.decide(operation=_OPERATION, actor=actor, as_of=self._clock())
        except Exception as exc:
            raise SchoolFactAuthorizationUnavailable("Own terminal authority unavailable.") from exc
        basis = result.basis
        scope = result.scope_plan
        if (
            not result.allowed
            or type(basis) is not ThinAuthorizationBasis
            or basis.governance.duty_code != F7_OWN_TERMINAL_DUTY
            or basis.action_code != F7_OWN_TERMINAL_OPERATION
            or scope is None
            or scope.scope_id != self._scope_id
            or scope.owner != SCHOOL_SCOPE_OWNER
            or scope.reference != f"school:{actor.school_id}"
            or scope.version != 1
            or result.projection != NamedProjection(_PROJECTION, 1)
        ):
            raise SecureReadDenied("Current own terminal duty is absent.")
        return F7OwnTerminalAuthorization(self._adapter, actor, result, self._clock)
