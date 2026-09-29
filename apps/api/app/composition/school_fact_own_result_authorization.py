"""Compile current F2 owner authority before an own-receipt SQL projection."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, field
from typing import Protocol
from uuid import UUID

from app.platform.authorization.secure_reader_contracts import ReadAuthorization
from app.platform.authorization.secure_reader_registry import f2_operation_registry
from app.platform.authorization.thin_contracts import NamedProjection
from app.platform.contracts import CommandExecutionContext, StaffActorContext
from app.platform.school_facts.application import SchoolFactChangeRejected
from app.platform.school_facts.f2_policy import F2_FACT_KINDS, F2_SCOPE_OWNER, F2FactScope
from app.platform.school_facts.ports import SchoolFactOwnResultScope
from app.platform.school_facts.read_policy import SchoolFactAuthorizationUnavailable


class _CurrentF2Authority(Protocol):
    def require_current(
        self, actor: StaffActorContext, fact_kind: str, *, purpose: str
    ) -> ReadAuthorization: ...
    def require_current_many(
        self, actor: StaffActorContext
    ) -> tuple[tuple[str, ReadAuthorization], ...]: ...

    def revalidate_many(
        self, authorizations: tuple[ReadAuthorization, ...], actor: StaffActorContext
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class F2OwnResultAuthorization:
    allowed_targets: SchoolFactOwnResultScope
    _guard: _CurrentF2Authority = field(repr=False)
    _actor: StaffActorContext = field(repr=False)
    _authorizations: tuple[ReadAuthorization, ...] = field(repr=False)

    _round: Callable[[StaffActorContext], AbstractContextManager[None]] = field(repr=False)

    def revalidate(self) -> None:
        with self._round(self._actor):
            self._guard.revalidate_many(self._authorizations, self._actor)


class F2OwnResultAuthorizer:
    """No payload reads, creation-scope expansion, assignment union or school-wide fallback."""

    def __init__(
        self,
        guard: _CurrentF2Authority,
        *,
        authorization_round: Callable[[StaffActorContext], AbstractContextManager[None]]
        | None = None,
    ) -> None:
        self._guard = guard
        self._round: Callable[[StaffActorContext], AbstractContextManager[None]] = (
            authorization_round or (lambda _actor: nullcontext())
        )

    def __call__(self, context: CommandExecutionContext) -> F2OwnResultAuthorization:
        if (
            type(context) is not CommandExecutionContext
            or type(context.actor) is not StaffActorContext
        ):
            raise SchoolFactChangeRejected("Current staff identity is required for own receipts.")
        with self._round(context.actor):
            return self._authorize(context.actor)

    def for_targets(
        self, context: CommandExecutionContext, targets: SchoolFactOwnResultScope
    ) -> F2OwnResultAuthorization:
        if (
            type(context) is not CommandExecutionContext
            or type(context.actor) is not StaffActorContext
        ):
            raise SchoolFactChangeRejected("Current staff identity is required for own receipts.")
        with self._round(context.actor):
            return self._authorize(context.actor, targets)

    def _authorize(
        self, actor: StaffActorContext, requested: SchoolFactOwnResultScope | None = None
    ) -> F2OwnResultAuthorization:
        targets: set[tuple[str, str]] = set()
        authorizations: list[ReadAuthorization] = []
        registry = f2_operation_registry()
        # Historical receipt metadata needs current reading authority; maintenance
        # preview authority never expands this independently approved read route.
        purpose = "read"
        kinds = tuple(
            sorted(
                F2_FACT_KINDS
                if requested is None
                else {k for k, _ in requested} & set(F2_FACT_KINDS)
            )
        )
        decisions = (
            self._guard.require_current_many(actor)
            if requested is None
            else tuple(
                (kind, self._guard.require_current(actor, kind, purpose="read")) for kind in kinds
            )
        )
        if tuple(kind for kind, _ in decisions) != kinds:
            raise SchoolFactAuthorizationUnavailable("Current receipt batch is incomplete.")
        for kind, authorization in decisions:
            if not authorization.allowed:
                continue
            operation = registry.require(f"school_facts.f2.{kind}.{purpose}")
            if (
                not authorization.allowed
                or authorization.scope_plan is None
                or authorization.scope_plan.owner != F2_SCOPE_OWNER
                or authorization.scope_plan.version != 1
                or authorization.projection != NamedProjection(operation.projection_name, 1)
            ):
                raise SchoolFactAuthorizationUnavailable("Current receipt projection unavailable.")
            try:
                scope = F2FactScope.from_reference(authorization.scope_plan.reference)
            except (TypeError, ValueError, KeyError) as exc:
                raise SchoolFactAuthorizationUnavailable(
                    "Current receipt scope unavailable."
                ) from exc
            if scope.school_id != UUID(str(actor.school_id)) or scope.fact_kind != kind:
                raise SchoolFactAuthorizationUnavailable("Current receipt scope does not match.")
            selected = (
                {
                    (requested_kind, key)
                    for requested_kind, key in requested
                    if requested_kind == kind and scope.allows_key(key)
                }
                if scope.is_school_wide and requested is not None
                else {(kind, key) for key in scope.object_keys}
            )
            if requested is not None:
                selected.intersection_update(requested)
            if not selected:
                continue
            targets.update(selected)
            authorizations.append(authorization)
        if not targets:
            raise SchoolFactChangeRejected("No current object scope permits own receipt reading.")
        result = F2OwnResultAuthorization(
            frozenset(targets), self._guard, actor, tuple(authorizations), self._round
        )
        return result
