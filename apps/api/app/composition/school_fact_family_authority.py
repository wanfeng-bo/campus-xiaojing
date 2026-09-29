"""Current Staff authority adapter for F3 family facts."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, cast
from uuid import UUID, uuid4

from app.domains.students.family_rules import (
    CurrentFamilyFactActorValidator,
    FamilyAction,
    FamilyActorValidation,
)
from app.platform.authorization.thin_contracts import (
    NamedProjection,
    ThinAuthorizationBasis,
    ThinDecisionKind,
)
from app.platform.authorization.thin_service import ThinAuthorizationService
from app.platform.contracts import CommandExecutionContext, StaffActorContext
from app.platform.school_facts.contracts import CanonicalFactChangeSetV1, CanonicalFactChangeV1
from app.platform.school_facts.f3_policy import (
    F3_SCOPE_OWNER,
    F3FactPolicy,
    F3FactScope,
    F3ScopeResolver,
)
from app.platform.school_facts.ports import SchoolFactOwnResultScope

from .school_fact_family_settings import FamilyConfiguration, FamilyReadDependencyBinding


class FamilyFactAuthorityDenied(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class FamilyFactTarget:
    fact_kind: str
    source_key: str
    field_names: tuple[str, ...]
    resource_revision: int
    is_creation: bool = False


@dataclass(frozen=True, slots=True)
class StaffFamilyFactPermit:
    actor: StaffActorContext
    action: str
    target: FamilyFactTarget
    scope_id: UUID
    scope: F3FactScope
    basis: ThinAuthorizationBasis


@dataclass(frozen=True, slots=True)
class F3SchoolScopePermit:
    actor: StaffActorContext
    action: str
    scope_id: UUID
    scope: F3FactScope
    basis: ThinAuthorizationBasis


@dataclass(frozen=True, slots=True)
class FamilyChangeSetPermit:
    actor: StaffActorContext
    change_set_sha256: str
    item_permits: tuple[StaffFamilyFactPermit, ...]


class StaffFamilyFactAuthorization:
    def __init__(self, *, thin: ThinAuthorizationService, scope_id: UUID) -> None:
        self._thin = thin
        self._scope_id = scope_id

    def require_current(
        self,
        actor: StaffActorContext,
        *,
        action: str,
        target: FamilyFactTarget,
        checked_at: datetime,
    ) -> StaffFamilyFactPermit:
        from app.platform.authorization.thin_contracts import ThinAuthorizationRequest

        if checked_at.utcoffset() is None or type(actor) is not StaffActorContext:
            raise FamilyFactAuthorityDenied("Current trusted Staff authority required.")
        operation = f"school_facts.f3.{target.fact_kind}.{action}"
        decision = self._thin.decide(
            ThinAuthorizationRequest(
                actor=actor,
                module_name=F3_SCOPE_OWNER,
                action_code=operation,
                scope_id=self._scope_id,
                resource_type=operation,
                resource_id=actor.school_id,
                resource_revision=actor.authorization_version,
                request_id=uuid4(),
                as_of=checked_at,
            )
        )
        if (
            decision.kind is not ThinDecisionKind.ALLOW
            or decision.scope_plan is None
            or decision.scope_plan.owner != F3_SCOPE_OWNER
            or not isinstance(decision.basis, ThinAuthorizationBasis)
        ):
            raise FamilyFactAuthorityDenied("F3 Staff authority denied.")
        try:
            scope = F3FactScope.from_reference(decision.scope_plan.reference)
        except (TypeError, ValueError, KeyError):
            raise FamilyFactAuthorityDenied("Invalid F3 authority scope.") from None
        if (
            scope.school_id != actor.school_id
            or scope.fact_kind != target.fact_kind
            or not scope.allows_key(target.source_key, is_creation=target.is_creation)
            or not target.field_names
            or not set(target.field_names) <= set(scope.field_names)
        ):
            raise FamilyFactAuthorityDenied("F3 target is outside the exact approved scope.")
        return StaffFamilyFactPermit(actor, action, target, self._scope_id, scope, decision.basis)

    def require_scope(
        self,
        actor: StaffActorContext,
        *,
        action: str,
        fact_kind: str,
        source_key: str,
        is_creation: bool,
        checked_at: datetime,
    ) -> StaffFamilyFactPermit:
        from app.platform.authorization.thin_contracts import ThinAuthorizationRequest

        operation = f"school_facts.f3.{fact_kind}.{action}"
        decision = self._thin.decide(
            ThinAuthorizationRequest(
                actor=actor,
                module_name=F3_SCOPE_OWNER,
                action_code=operation,
                scope_id=self._scope_id,
                resource_type=operation,
                resource_id=actor.school_id,
                resource_revision=actor.authorization_version,
                request_id=uuid4(),
                as_of=checked_at,
            )
        )
        if (
            decision.kind is not ThinDecisionKind.ALLOW
            or decision.scope_plan is None
            or not isinstance(decision.basis, ThinAuthorizationBasis)
        ):
            raise FamilyFactAuthorityDenied("F3 Staff authority denied.")
        scope = F3FactScope.from_reference(decision.scope_plan.reference)
        if (
            scope.school_id != actor.school_id
            or scope.fact_kind != fact_kind
            or not scope.allows_key(source_key, is_creation=is_creation)
        ):
            raise FamilyFactAuthorityDenied("F3 target outside current scope.")
        target = FamilyFactTarget(
            fact_kind, source_key, scope.field_names, actor.authorization_version, is_creation
        )
        return StaffFamilyFactPermit(actor, action, target, self._scope_id, scope, decision.basis)

    def final_guard(
        self,
        permit: StaffFamilyFactPermit,
        *,
        actor: StaffActorContext,
        target: FamilyFactTarget,
        checked_at: datetime,
    ) -> StaffFamilyFactPermit:
        if actor != permit.actor or target != permit.target or checked_at.utcoffset() is None:
            raise FamilyFactAuthorityDenied("F3 confirmation changed after preflight.")
        decision = self._thin.revalidate(
            permit.basis,
            actor=actor,
            as_of=checked_at,
            resource_revision=actor.authorization_version,
        )
        if decision.kind is not ThinDecisionKind.ALLOW:
            raise FamilyFactAuthorityDenied("F3 authority changed before final use.")
        return permit


_REQUEST_FIELDS = (
    "access_purpose",
    "guardian_person_source_key",
    "relationship_kind",
    "status",
    "student_source_key",
    "verification_reference",
    "verification_source",
)
_FINAL_FIELDS = ("status",)


class CurrentFamilyFactActorValidatorAdapter:
    """Domain hook: every call obtains a fresh Staff/Governance decision."""

    def __init__(self, authority: StaffFamilyFactAuthorization) -> None:
        self._authority = authority

    def validate(
        self,
        *,
        actor: StaffActorContext,
        action: FamilyAction,
        target: str,
        checked_at: datetime,
    ) -> FamilyActorValidation:
        fields = _REQUEST_FIELDS if action == "request" else _FINAL_FIELDS
        fact_target = FamilyFactTarget(
            "guardian_relationship",
            target,
            fields,
            actor.authorization_version,
            action == "request",
        )
        permit = self._authority.require_current(
            actor, action=action, target=fact_target, checked_at=checked_at
        )
        scope_kind: Literal["school", "relationship"] = (
            "school" if action == "request" else "relationship"
        )
        return FamilyActorValidation(
            actor_person_id=actor.person_id,
            actor_assignment_id=actor.assignment_id,
            checked_at=checked_at,
            scope_kind=scope_kind,
            scope_id=permit.scope_id,
            action=action,
        )


@dataclass(frozen=True, slots=True)
class F3OwnResultAuthorization:
    allowed_targets: SchoolFactOwnResultScope
    actor: StaffActorContext
    bases: tuple[ThinAuthorizationBasis, ...]
    thin: ThinAuthorizationService
    clock: Callable[[], datetime]

    def revalidate(self) -> None:
        for basis in self.bases:
            decision = self.thin.revalidate(
                basis,
                actor=self.actor,
                as_of=self.clock(),
                resource_revision=self.actor.authorization_version,
            )
            if decision.kind is not ThinDecisionKind.ALLOW:
                raise FamilyFactAuthorityDenied("F3 receipt read authority changed.")


class F3OwnResultAuthorizer:
    """Current F3 read scopes only; maintenance and creation scopes never expand receipts."""

    def __init__(
        self,
        *,
        thin: ThinAuthorizationService,
        configuration: FamilyConfiguration,
        clock: Callable[[], datetime],
    ) -> None:
        self.thin = thin
        self.configuration = configuration
        self.clock = clock

    def for_targets(
        self, context: CommandExecutionContext, targets: SchoolFactOwnResultScope
    ) -> F3OwnResultAuthorization:
        return self(context, targets=targets)

    def __call__(
        self, context: CommandExecutionContext, *, targets: SchoolFactOwnResultScope | None = None
    ) -> F3OwnResultAuthorization:
        from app.platform.authorization.thin_contracts import ThinAuthorizationRequest

        if (
            type(context) is not CommandExecutionContext
            or type(context.actor) is not StaffActorContext
        ):
            raise FamilyFactAuthorityDenied("Current Staff identity required for F3 receipts.")
        actor = context.actor
        requested = targets
        allowed: set[tuple[str, str]] = set()
        bases: list[ThinAuthorizationBasis] = []
        now = self.clock()
        for binding in self.configuration.scope_bindings:
            if binding.assignment_id != actor.assignment_id or not binding.operation_code.endswith(
                ".read"
            ):
                continue
            kind = binding.operation_code.split(".")[2]
            if requested is not None and kind not in {k for k, _ in requested}:
                continue
            decision = self.thin.decide(
                ThinAuthorizationRequest(
                    actor=actor,
                    module_name=F3_SCOPE_OWNER,
                    action_code=binding.operation_code,
                    scope_id=binding.scope_id,
                    resource_type=binding.operation_code,
                    resource_id=actor.school_id,
                    resource_revision=actor.authorization_version,
                    request_id=uuid4(),
                    as_of=now,
                )
            )
            if (
                decision.kind is not ThinDecisionKind.ALLOW
                or decision.scope_plan is None
                or decision.projection != NamedProjection(f"f3_{kind}_read", 1)
                or not isinstance(decision.basis, ThinAuthorizationBasis)
            ):
                continue
            scope = F3FactScope.from_reference(decision.scope_plan.reference)
            if scope.school_id != actor.school_id or scope.fact_kind != kind:
                raise FamilyFactAuthorityDenied("Invalid current F3 receipt scope.")
            selected = (
                {(kind, key) for key in scope.object_keys}
                if requested is None or not scope.is_school_wide
                else {
                    (candidate_kind, key)
                    for candidate_kind, key in requested
                    if candidate_kind == kind and scope.allows_key(key)
                }
            )
            if requested is not None and not scope.is_school_wide:
                selected.intersection_update(requested)
            if not selected:
                continue
            allowed.update(selected)
            bases.append(decision.basis)
        if not allowed or (requested is not None and not requested <= allowed):
            raise FamilyFactAuthorityDenied("No current F3 read targets permit own receipts.")
        return F3OwnResultAuthorization(
            frozenset(allowed), actor, tuple(bases), self.thin, self.clock
        )


class StaffFamilyFactChangeSetGuard:
    def __init__(
        self, *, thin: ThinAuthorizationService, configuration: FamilyConfiguration
    ) -> None:
        self._thin = thin
        self._bindings = {
            (b.assignment_id, b.operation_code): b.scope_id for b in configuration.scope_bindings
        }

    def scope_id(self, actor: StaffActorContext, operation_code: str) -> UUID | None:
        return self._bindings.get((actor.assignment_id, operation_code))

    @staticmethod
    def _digest(cs: CanonicalFactChangeSetV1) -> str:
        return hashlib.sha256(cs.model_dump_json().encode()).hexdigest()

    @staticmethod
    def _action(c: CanonicalFactChangeV1) -> str:
        if str(c.fact_kind) != "guardian_relationship":
            return "maintain"
        op = str(c.operation)
        if op in {"end", "revoke"}:
            return op
        return (
            "request"
            if c.content is not None and c.content.status == "pending_verification"
            else "verify"
        )

    @staticmethod
    def _target(c: CanonicalFactChangeV1) -> FamilyFactTarget:
        content = c.content
        fields = (
            tuple(sorted(content.model_fields_set))
            if content is not None
            else (
                ("valid_to",) if str(c.fact_kind) == "student_school_relationship" else ("status",)
            )
        )
        segments = getattr(c, "replacement_segments", None)
        if segments is not None:
            fields = tuple(
                sorted(
                    {"valid_to"}
                    | {name for segment in segments for name in segment.content.model_fields_set}
                )
            )
        return FamilyFactTarget(
            str(c.fact_kind),
            str(c.source_key),
            fields,
            int(c.expected_version),
            str(c.operation) == "upsert" and int(c.expected_version) == 0,
        )

    def require_change_set(
        self,
        actor: StaffActorContext,
        change_set: CanonicalFactChangeSetV1,
        *,
        checked_at: datetime,
    ) -> FamilyChangeSetPermit:
        permits: list[StaffFamilyFactPermit] = []
        for c in change_set.changes:
            action = self._action(c)
            code = f"school_facts.f3.{c.fact_kind}.{action}"
            sid = self._bindings.get((actor.assignment_id, code))
            if sid is None:
                raise FamilyFactAuthorityDenied("No exact F3 operation binding.")
            permits.append(
                StaffFamilyFactAuthorization(thin=self._thin, scope_id=sid).require_current(
                    actor, action=action, target=self._target(c), checked_at=checked_at
                )
            )
        return FamilyChangeSetPermit(actor, self._digest(change_set), tuple(permits))

    def final_guard(
        self,
        permit: FamilyChangeSetPermit,
        actor: StaffActorContext,
        change_set: CanonicalFactChangeSetV1,
        *,
        checked_at: datetime,
    ) -> FamilyChangeSetPermit:
        if actor != permit.actor or self._digest(change_set) != permit.change_set_sha256:
            raise FamilyFactAuthorityDenied("F3 change set changed after preflight.")
        targets = tuple(self._target(c) for c in change_set.changes)
        if len(targets) != len(permit.item_permits):
            raise FamilyFactAuthorityDenied("F3 change set changed after preflight.")
        for item, target in zip(permit.item_permits, targets, strict=True):
            StaffFamilyFactAuthorization(thin=self._thin, scope_id=item.scope_id).final_guard(
                item, actor=actor, target=target, checked_at=checked_at
            )
        return permit


class F3ScopeOwnerLookup:
    def __init__(self, sessions: Any) -> None:
        self.sessions = sessions

    def scope_exists(self, scope: F3FactScope, as_of: datetime) -> bool:
        from sqlalchemy import text

        with self.sessions.begin() as session:
            return (
                session.scalar(
                    text(
                        "SELECT app.school_fact_f3_scope_is_current(:school,:kind,:objects,:as_of)"
                    ),
                    {
                        "school": scope.school_id,
                        "kind": scope.fact_kind,
                        "objects": list(scope.object_keys),
                        "as_of": as_of,
                    },
                )
                is True
            )


@dataclass(frozen=True, slots=True)
class FamilyAuthorityComponents:
    thin: ThinAuthorizationService
    connection_authority: Any
    change_set_guard: StaffFamilyFactChangeSetGuard
    actor_validator: CurrentFamilyFactActorValidator
    read_dependency_bindings: tuple[FamilyReadDependencyBinding, ...] = ()

    def require_school_scope(
        self, actor: StaffActorContext, action: str, fact_kind: str, checked_at: datetime
    ) -> F3SchoolScopePermit:
        from app.platform.authorization.thin_contracts import ThinAuthorizationRequest

        if type(actor) is not StaffActorContext or checked_at.utcoffset() is None:
            raise FamilyFactAuthorityDenied("Current trusted Staff authority required.")
        code = f"school_facts.f3.{fact_kind}.{action}"
        scope_id = self.change_set_guard.scope_id(actor, code)
        if scope_id is None:
            raise FamilyFactAuthorityDenied("No current F3 school scope binding.")
        decision = self.thin.decide(
            ThinAuthorizationRequest(
                actor=actor,
                module_name=F3_SCOPE_OWNER,
                action_code=code,
                scope_id=scope_id,
                resource_type=code,
                resource_id=actor.school_id,
                resource_revision=actor.authorization_version,
                request_id=uuid4(),
                as_of=checked_at,
            )
        )
        if (
            decision.kind is not ThinDecisionKind.ALLOW
            or decision.scope_plan is None
            or decision.scope_plan.owner != F3_SCOPE_OWNER
            or not isinstance(decision.basis, ThinAuthorizationBasis)
        ):
            raise FamilyFactAuthorityDenied("F3 school scope denied.")
        scope = F3FactScope.from_reference(decision.scope_plan.reference)
        if (
            not scope.is_school_wide
            or scope.school_id != actor.school_id
            or scope.fact_kind != fact_kind
        ):
            raise FamilyFactAuthorityDenied("F3 school scope differs.")
        return F3SchoolScopePermit(actor, action, scope_id, scope, decision.basis)

    def revalidate_school_scope(
        self, permit: F3SchoolScopePermit, actor: StaffActorContext, checked_at: datetime
    ) -> None:
        if actor != permit.actor or checked_at.utcoffset() is None:
            raise FamilyFactAuthorityDenied("F3 school scope actor changed.")
        decision = self.thin.revalidate(
            permit.basis,
            actor=actor,
            as_of=checked_at,
            resource_revision=actor.authorization_version,
        )
        if decision.kind is not ThinDecisionKind.ALLOW:
            raise FamilyFactAuthorityDenied("F3 school scope changed.")

    def require_scope(
        self,
        actor: StaffActorContext,
        action: str,
        fact_kind: str,
        source_key: str,
        is_creation: bool,
        checked_at: datetime,
    ) -> StaffFamilyFactPermit:
        code = f"school_facts.f3.{fact_kind}.{action}"
        scope_id = self.change_set_guard.scope_id(actor, code)
        if action == "read" and not is_creation:
            for binding in self.read_dependency_bindings:
                if (
                    binding.assignment_id == actor.assignment_id
                    and binding.operation_code == code
                    and source_key in binding.object_keys
                ):
                    # Selection does not combine scopes or retry a denied dependency
                    # against a wider primary scope. Thin checks the original grant.
                    scope_id = binding.scope_id
                    break
        if scope_id is None:
            raise FamilyFactAuthorityDenied("No exact current F3 scope binding.")
        return StaffFamilyFactAuthorization(thin=self.thin, scope_id=scope_id).require_scope(
            actor,
            action=action,
            fact_kind=fact_kind,
            source_key=source_key,
            is_creation=is_creation,
            checked_at=checked_at,
        )

    def revalidate_permit(
        self,
        permit: StaffFamilyFactPermit,
        actor: StaffActorContext,
        target: FamilyFactTarget,
        checked_at: datetime,
    ) -> StaffFamilyFactPermit:
        return StaffFamilyFactAuthorization(thin=self.thin, scope_id=permit.scope_id).final_guard(
            permit, actor=actor, target=target, checked_at=checked_at
        )

    def require_change_set(
        self,
        actor: StaffActorContext,
        change_set: CanonicalFactChangeSetV1,
        *,
        checked_at: datetime,
    ) -> FamilyChangeSetPermit:
        return self.change_set_guard.require_change_set(actor, change_set, checked_at=checked_at)

    def final_guard(
        self,
        permit: FamilyChangeSetPermit,
        actor: StaffActorContext,
        change_set: CanonicalFactChangeSetV1,
        *,
        checked_at: datetime,
    ) -> FamilyChangeSetPermit:
        return self.change_set_guard.final_guard(permit, actor, change_set, checked_at=checked_at)


def create_family_authority_components(
    *,
    session_factory: Any,
    configuration: FamilyConfiguration,
    evidence_owner: object | None = None,
) -> FamilyAuthorityComponents:
    from app.composition.school_fact_authority_connection import ConnectionBoundAuthority
    from app.composition.school_fact_product_services import ReadOnlyGovernanceDependencies
    from app.composition.school_fact_read_capture import (
        CapturingThinService,
        EvidenceGovernanceDependencies,
    )
    from app.platform.governance.basis import GovernanceAuthorizationBasisService
    from app.platform.governance.reader import GovernanceAuthorityReader

    def build_basis(bound: Any) -> GovernanceAuthorizationBasisService:
        reader = GovernanceAuthorityReader(
            session_factory=bound,
            dependency_port=(
                ReadOnlyGovernanceDependencies(bound)
                if evidence_owner is None
                else EvidenceGovernanceDependencies(bound, owner=evidence_owner)
            ),
            scope_owner_resolvers={
                F3_SCOPE_OWNER: F3ScopeResolver(lookup_scope=F3ScopeOwnerLookup(bound).scope_exists)
            },
        )
        return GovernanceAuthorizationBasisService(session_factory=bound, authority_reader=reader)

    connection = ConnectionBoundAuthority(sessions=session_factory, build_basis=build_basis)
    thin = cast(
        ThinAuthorizationService,
        (ThinAuthorizationService if evidence_owner is None else CapturingThinService)(
            governance_basis_service=connection, module_policies={F3_SCOPE_OWNER: F3FactPolicy()}
        ),
    )
    guard = StaffFamilyFactChangeSetGuard(thin=thin, configuration=configuration)

    class Validator:
        def validate(
            self,
            *,
            actor: StaffActorContext,
            action: FamilyAction,
            target: str,
            checked_at: datetime,
        ) -> FamilyActorValidation:
            sid = guard.scope_id(actor, f"school_facts.f3.guardian_relationship.{action}")
            if sid is None:
                raise FamilyFactAuthorityDenied("No exact guardian binding.")
            return CurrentFamilyFactActorValidatorAdapter(
                StaffFamilyFactAuthorization(thin=thin, scope_id=sid)
            ).validate(actor=actor, action=action, target=target, checked_at=checked_at)

    return FamilyAuthorityComponents(
        thin, connection, guard, Validator(), configuration.read_dependency_bindings
    )


__all__ = [
    "CurrentFamilyFactActorValidatorAdapter",
    "F3ScopeOwnerLookup",
    "F3OwnResultAuthorization",
    "F3OwnResultAuthorizer",
    "FamilyAuthorityComponents",
    "create_family_authority_components",
    "FamilyFactAuthorityDenied",
    "FamilyFactTarget",
    "FamilyChangeSetPermit",
    "StaffFamilyFactAuthorization",
    "StaffFamilyFactChangeSetGuard",
    "StaffFamilyFactPermit",
]
