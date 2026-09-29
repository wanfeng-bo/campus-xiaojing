# pyright: strict

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Protocol
from uuid import UUID, uuid4

from app.platform.contracts import StaffActorContext
from app.platform.governance.contracts import (
    GovernanceAuthorityQuery,
    GovernanceAuthorizationBasis,
)

from .thin_contracts import (
    ModuleAuthorizationRequirement,
    ModulePolicy,
    NamedProjection,
    ScopePlan,
    StaffActorBasis,
    ThinAuthorizationBasis,
    ThinAuthorizationDecision,
    ThinAuthorizationRequest,
    ThinDecisionKind,
    ThinDenyReason,
    ThinSelfAuthorizationBasis,
)

_SCOPE_TARGET_SUFFIX = ".scope-target"


class _GovernanceBasisService(Protocol):
    def issue(self, query: GovernanceAuthorityQuery) -> GovernanceAuthorizationBasis: ...

    def validate(self, basis: GovernanceAuthorizationBasis, *, as_of: datetime) -> bool: ...


class ThinAuthorizationService:
    """Mechanically combines Governance authority with one owning module Policy."""

    def __init__(
        self,
        *,
        governance_basis_service: _GovernanceBasisService,
        module_policies: Mapping[str, ModulePolicy],
        self_person_validator: Callable[[StaffActorContext, datetime], bool] | None = None,
    ) -> None:
        self._governance_basis_service = governance_basis_service
        self._module_policies = dict(module_policies)
        self._self_person_validator = self_person_validator

    def decide(self, request: ThinAuthorizationRequest) -> ThinAuthorizationDecision:
        decision_id = uuid4()
        actor = request.actor
        if type(actor) is not StaffActorContext:
            return _deny(
                request_id=request.request_id,
                decision_id=decision_id,
                reason=ThinDenyReason.UNTRUSTED_ACTOR,
            )
        policy = self._module_policies.get(request.module_name)
        requirement = self._requirement(policy, request)
        if policy is None or requirement is None:
            return _deny(
                request_id=request.request_id,
                decision_id=decision_id,
                reason=ThinDenyReason.UNKNOWN_ACTION,
            )

        if requirement.authority_kind == "self_person":
            if (
                not self._self_is_current(actor, request.as_of)
                or request.scope_id != actor.person_id
            ):
                return _deny(
                    request_id=request.request_id,
                    decision_id=decision_id,
                    reason=ThinDenyReason.ACTOR_CHANGED,
                )
            basis = ThinSelfAuthorizationBasis(
                actor=_actor_basis(actor),
                scope_id=request.scope_id,
                module_name=request.module_name,
                action_code=request.action_code,
                module_policy_version=policy.version,
                resource_type=request.resource_type,
                resource_id=request.resource_id,
                resource_revision=request.resource_revision,
                request_id=request.request_id,
                decision_id=decision_id,
            )
            return self._self_decision(basis, requirement)
        if requirement.authority_kind != "governance":
            return _deny(
                request_id=request.request_id,
                decision_id=decision_id,
                reason=ThinDenyReason.UNKNOWN_ACTION,
            )

        try:
            governance = self._governance_basis_service.issue(
                GovernanceAuthorityQuery(
                    school_id=actor.school_id,
                    assignment_id=actor.assignment_id,
                    duty_code=requirement.duty_code,
                    scope_id=request.scope_id,
                    as_of=request.as_of,
                )
            )
        except Exception as exc:
            return _deny(
                request_id=request.request_id,
                decision_id=decision_id,
                reason=(
                    ThinDenyReason.GOVERNANCE_AMBIGUOUS
                    if "ambiguous" in _exception_reason(exc)
                    else ThinDenyReason.GOVERNANCE_UNAVAILABLE
                ),
            )

        if not _governance_matches_actor(governance, actor):
            return _deny(
                request_id=request.request_id,
                decision_id=decision_id,
                reason=ThinDenyReason.ACTOR_CHANGED,
            )
        scope_plan = _scope_plan(governance)
        if scope_plan is None:
            return _deny(
                request_id=request.request_id,
                decision_id=decision_id,
                reason=ThinDenyReason.GOVERNANCE_AMBIGUOUS,
            )
        wrapped_basis = ThinAuthorizationBasis(
            governance=governance,
            actor=_actor_basis(actor),
            module_name=request.module_name,
            action_code=request.action_code,
            module_policy_version=policy.version,
            resource_type=request.resource_type,
            resource_id=request.resource_id,
            resource_revision=request.resource_revision,
            request_id=request.request_id,
            decision_id=decision_id,
        )
        return ThinAuthorizationDecision(
            kind=ThinDecisionKind.ALLOW,
            reason=None,
            scope_plan=scope_plan,
            projection=NamedProjection(
                name=requirement.projection_name,
                version=requirement.projection_version,
            ),
            basis=wrapped_basis,
            request_id=request.request_id,
            decision_id=decision_id,
        )

    def revalidate(
        self,
        basis: ThinAuthorizationBasis | ThinSelfAuthorizationBasis,
        *,
        actor: StaffActorContext,
        as_of: datetime,
        resource_revision: int,
    ) -> ThinAuthorizationDecision:
        if type(actor) is not StaffActorContext:
            return _deny_from_basis(basis, ThinDenyReason.UNTRUSTED_ACTOR)
        if _actor_basis(actor) != basis.actor:
            return _deny_from_basis(basis, ThinDenyReason.ACTOR_CHANGED)
        if resource_revision != basis.resource_revision:
            return _deny_from_basis(basis, ThinDenyReason.RESOURCE_CHANGED)

        policy = self._module_policies.get(basis.module_name)
        if policy is None or policy.module_name != basis.module_name:
            return _deny_from_basis(basis, ThinDenyReason.POLICY_CHANGED)
        if policy.version != basis.module_policy_version:
            return _deny_from_basis(basis, ThinDenyReason.POLICY_CHANGED)

        if isinstance(basis, ThinSelfAuthorizationBasis):
            request = ThinAuthorizationRequest(
                actor=actor,
                module_name=basis.module_name,
                action_code=basis.action_code,
                scope_id=basis.scope_id,
                resource_type=basis.resource_type,
                resource_id=basis.resource_id,
                resource_revision=resource_revision,
                request_id=basis.request_id,
                as_of=as_of,
            )
            requirement = self._requirement(policy, request)
            if (
                requirement is None
                or requirement.authority_kind != "self_person"
                or basis.scope_id != actor.person_id
                or not self._self_is_current(actor, as_of)
            ):
                return _deny_from_basis(basis, ThinDenyReason.ACTOR_CHANGED)
            return self._self_decision(basis, requirement)

        request = ThinAuthorizationRequest(
            actor=actor,
            module_name=basis.module_name,
            action_code=basis.action_code,
            scope_id=basis.governance.scope_id,
            resource_type=basis.resource_type,
            resource_id=basis.resource_id,
            resource_revision=resource_revision,
            request_id=basis.request_id,
            as_of=as_of,
        )
        requirement = self._requirement(policy, request)
        if requirement is None or requirement.duty_code != basis.governance.duty_code:
            return _deny_from_basis(basis, ThinDenyReason.POLICY_CHANGED)

        try:
            valid = self._governance_basis_service.validate(basis.governance, as_of=as_of)
        except Exception as exc:
            reason = (
                ThinDenyReason.BASIS_EXPIRED
                if _exception_reason(exc) == "basis_expired"
                else ThinDenyReason.GOVERNANCE_CHANGED
            )
            return _deny_from_basis(basis, reason)
        if not valid:
            return _deny_from_basis(basis, ThinDenyReason.GOVERNANCE_CHANGED)

        scope_plan = _scope_plan(basis.governance)
        if scope_plan is None:
            return _deny_from_basis(basis, ThinDenyReason.GOVERNANCE_CHANGED)
        return ThinAuthorizationDecision(
            kind=ThinDecisionKind.ALLOW,
            reason=None,
            scope_plan=scope_plan,
            projection=NamedProjection(
                name=requirement.projection_name,
                version=requirement.projection_version,
            ),
            basis=basis,
            request_id=basis.request_id,
            decision_id=basis.decision_id,
        )

    def _self_is_current(self, actor: StaffActorContext, as_of: datetime) -> bool:
        try:
            return (
                as_of.utcoffset() is not None
                and self._self_person_validator is not None
                and self._self_person_validator(actor, as_of) is True
            )
        except Exception:
            return False

    @staticmethod
    def _self_decision(
        basis: ThinSelfAuthorizationBasis, requirement: ModuleAuthorizationRequirement
    ):
        return ThinAuthorizationDecision(
            kind=ThinDecisionKind.ALLOW,
            reason=None,
            scope_plan=ScopePlan(
                basis.scope_id, f"{basis.module_name}.self", f"person:{basis.actor.person_id}", 1
            ),
            projection=NamedProjection(requirement.projection_name, requirement.projection_version),
            basis=basis,
            request_id=basis.request_id,
            decision_id=basis.decision_id,
        )

    @staticmethod
    def _requirement(
        policy: ModulePolicy | None,
        request: ThinAuthorizationRequest,
    ) -> ModuleAuthorizationRequirement | None:
        if policy is None or policy.module_name != request.module_name or policy.version < 1:
            return None
        try:
            requirement = policy.requirement_for(request)
        except Exception:
            return None
        if (
            requirement is None
            or not requirement.duty_code.strip()
            or not requirement.projection_name.strip()
            or requirement.projection_version < 1
        ):
            return None
        return requirement


def _scope_plan(governance: GovernanceAuthorizationBasis) -> ScopePlan | None:
    targets = [
        dependency
        for dependency in governance.dependency_versions
        if dependency.authority.endswith(_SCOPE_TARGET_SUFFIX)
    ]
    if len(targets) != 1:
        return None
    target = targets[0]
    owner = target.authority.removesuffix(_SCOPE_TARGET_SUFFIX)
    if not owner or not target.reference or target.version < 1:
        return None
    return ScopePlan(
        scope_id=governance.scope_id,
        owner=owner,
        reference=target.reference,
        version=target.version,
    )


def _actor_basis(actor: StaffActorContext) -> StaffActorBasis:
    return StaffActorBasis(
        account_user_id=actor.account_user_id,
        school_id=actor.school_id,
        person_id=actor.person_id,
        assignment_id=actor.assignment_id,
        assignment_version=actor.assignment_version,
        authorization_version=actor.authorization_version,
    )


def _governance_matches_actor(
    governance: GovernanceAuthorizationBasis,
    actor: StaffActorContext,
) -> bool:
    if governance.school_id != actor.school_id or governance.assignment_id != actor.assignment_id:
        return False
    assignments = [
        dependency
        for dependency in governance.dependency_versions
        if dependency.authority == "school_facts.assignment"
    ]
    people = [
        dependency
        for dependency in governance.dependency_versions
        if dependency.authority == "school_facts.person"
    ]
    return (
        len(assignments) == 1
        and assignments[0].reference == str(actor.assignment_id)
        and assignments[0].version == actor.assignment_version
        and len(people) == 1
        and people[0].reference == str(actor.person_id)
    )


def _exception_reason(error: Exception) -> str:
    reason = getattr(error, "reason", "")
    return str(getattr(reason, "value", reason))


def _deny_from_basis(
    basis: ThinAuthorizationBasis | ThinSelfAuthorizationBasis,
    reason: ThinDenyReason,
) -> ThinAuthorizationDecision:
    return _deny(
        request_id=basis.request_id,
        decision_id=uuid4(),
        reason=reason,
    )


def _deny(
    *,
    request_id: UUID,
    decision_id: UUID,
    reason: ThinDenyReason,
) -> ThinAuthorizationDecision:
    return ThinAuthorizationDecision(
        kind=ThinDecisionKind.DENY,
        reason=reason,
        scope_plan=None,
        projection=None,
        basis=None,
        request_id=request_id,
        decision_id=decision_id,
    )


__all__ = ["ThinAuthorizationService"]
