"""Exact F6 authorization, shared by reads and the sole Writer's final guard."""

from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import text

from app.platform.authorization.thin_contracts import (
    NamedProjection,
    ThinAuthorizationBasis,
    ThinAuthorizationRequest,
    ThinDecisionKind,
)
from app.platform.contracts import StaffActorContext
from app.platform.school_facts.f6_policy import (
    F6_FACT_KINDS,
    F6_SCOPE_OWNER,
    F6FactPolicy,
    F6FactScope,
    F6ScopeResolver,
)


@dataclass(frozen=True)
class ResidencePermit:
    actor: StaffActorContext
    action: str
    scope: F6FactScope
    scope_id: Any
    basis: ThinAuthorizationBasis


@dataclass(frozen=True)
class ResidenceOwnResultProof:
    allowed_targets: frozenset[tuple[str, str]]
    permits: tuple[Any, ...]
    authority: Any
    clock: Any
    creation_only_targets: frozenset[tuple[str, str]] = frozenset()

    def revalidate(self) -> Any:
        for permit in self.permits:
            self.authority.revalidate(permit, actor=permit.actor, checked_at=self.clock())


class ResidenceAuthority:
    def __init__(
        self,
        *,
        thin: Any,
        configuration: Any,
        connection_authority: Any = None,
        clock: Any = lambda: datetime.now(UTC),
    ) -> None:
        self.thin, self.configuration, self.connection_authority, self.clock = (
            thin,
            configuration,
            connection_authority,
            clock,
        )
        self.bindings = {
            (b.assignment_id, b.operation_code): b.scope_id for b in configuration.scope_bindings
        }

    def _scope(self, actor: Any) -> Any:
        return (
            self.connection_authority.read_scope(actor)
            if self.connection_authority
            else nullcontext()
        )

    def require_scope(self, actor: Any, *, fact_kind: Any, action: Any, checked_at: Any) -> Any:
        if (
            type(actor) is not StaffActorContext
            or not self.configuration.enabled
            or fact_kind not in F6_FACT_KINDS
            or action not in {"read", "maintain"}
            or checked_at.utcoffset() is None
        ):
            raise PermissionError("当前身份或寄宿资料用途未获准。")
        operation = f"{F6_SCOPE_OWNER}.{fact_kind}.{action}"
        sid = self.bindings.get((actor.assignment_id, operation))
        if sid is None:
            raise PermissionError("当前身份没有这项寄宿资料权限。")
        request = ThinAuthorizationRequest(
            actor,
            F6_SCOPE_OWNER,
            operation,
            sid,
            operation,
            actor.school_id,
            actor.authorization_version,
            uuid4(),
            checked_at,
        )
        with self._scope(actor):
            decision = self.thin.decide(request)
        if (
            decision.kind is not ThinDecisionKind.ALLOW
            or decision.scope_plan is None
            or decision.scope_plan.owner != F6_SCOPE_OWNER
            or decision.scope_plan.scope_id != sid
            or decision.projection != NamedProjection(f"f6_{fact_kind}_{action}", 1)
            or not isinstance(decision.basis, ThinAuthorizationBasis)
        ):
            raise PermissionError("当前寄宿资料权限无法核实。")
        scope = F6FactScope.from_reference(decision.scope_plan.reference)
        if scope.school_id != actor.school_id or scope.fact_kind != fact_kind:
            raise PermissionError("寄宿资料范围与当前学校不符。")
        if action != "read" and scope.reserved_read_keys:
            raise PermissionError("预留读取对象不能用于维护职责。")
        return ResidencePermit(actor, action, scope, sid, decision.basis)

    @staticmethod
    def require_fields(permit: Any, fields: Any) -> Any:
        if not fields or not set(fields) <= set(permit.scope.field_names):
            raise PermissionError("当前身份无权读取或修改这些字段。")

    @staticmethod
    def require_object(permit: Any, source_key: Any, *, is_creation: Any) -> Any:
        keys = (
            permit.scope.creation_keys
            if is_creation
            else permit.scope.readable_keys
            if permit.action == "read"
            else permit.scope.object_keys
        )
        allowed = (
            permit.scope.allows_key(source_key, is_creation=is_creation)
            if permit.scope.is_school_wide
            else source_key in keys
        )
        if not allowed or is_creation and permit.action != "maintain":
            raise PermissionError("对象不在明确授权范围内。")

    def revalidate(self, permit: Any, *, actor: Any, checked_at: Any) -> Any:
        if actor != permit.actor or checked_at.utcoffset() is None:
            raise PermissionError("当前身份已变化。")
        with self._scope(actor):
            decision = self.thin.revalidate(
                permit.basis,
                actor=actor,
                as_of=checked_at,
                resource_revision=actor.authorization_version,
            )
        if decision.kind is not ThinDecisionKind.ALLOW:
            raise PermissionError("寄宿资料权限已变化。")
        from app.composition.school_fact_read_evidence import register_release_time_window

        governance = permit.basis.governance
        register_release_time_window(
            self.clock,
            observed_at=governance.issued_as_of,
            valid_before=governance.valid_until,
        )

    def require_change_set(self, actor: Any, change_set: Any, *, checked_at: Any = None) -> Any:
        permits: list[Any] = []
        for change in change_set.changes:
            permit = self.require_scope(
                actor,
                fact_kind=change.fact_kind,
                action="maintain",
                checked_at=checked_at or self.clock(),
            )
            self.require_object(permit, change.source_key, is_creation=change.expected_version == 0)
            fields = (
                tuple(change.content.model_fields_set)
                if change.content is not None
                else ("valid_to",)
            )
            if getattr(change, "replacement_segments", None) is not None:
                fields = tuple(
                    {"valid_to"}
                    | {
                        field
                        for segment in change.replacement_segments
                        for field in segment.content.model_fields_set
                    }
                )
            if change.fact_kind == "student_bed_residence":
                fields = (*fields, "effective_at")
            self.require_fields(permit, fields)
            permits.append(permit)
        for permit in permits:
            self.revalidate(permit, actor=actor, checked_at=self.clock())
        return tuple(permits)

    def require_action(
        self,
        *,
        actor: Any,
        fact_kind: Any,
        source_key: Any,
        field_name: Any,
        action: Any,
        checked_at: Any,
    ) -> Any:
        permit = self.require_scope(
            actor, fact_kind=fact_kind, action=action, checked_at=checked_at
        )
        self.require_object(permit, source_key, is_creation=False)
        self.require_fields(permit, (field_name,))
        return permit

    def own_result(self, context: Any, clock: Any = None, *, targets: Any = None) -> Any:
        permits: list[Any]
        actor, permits = context.actor, []
        if type(actor) is not StaffActorContext:
            raise PermissionError("当前回执身份未获准。")
        clock = clock or self.clock
        readable: set[Any]
        creations: set[Any]
        readable, creations = set(), set()
        kinds = F6_FACT_KINDS if targets is None else {k for k, _ in targets}
        for kind in sorted(kinds):
            for action in ("read", "maintain"):
                try:
                    permit = self.require_scope(
                        actor, fact_kind=kind, action=action, checked_at=clock()
                    )
                except PermissionError:
                    continue
                permits.append(permit)
                if permit.scope.is_school_wide:
                    if targets is not None:
                        (readable if action == "read" else creations).update(
                            (kind, key)
                            for target_kind, key in targets
                            if target_kind == kind
                            and permit.scope.allows_key(key, is_creation=action == "maintain")
                        )
                    continue
                (readable if action == "read" else creations).update(
                    (kind, k)
                    for k in (
                        permit.scope.readable_keys
                        if action == "read"
                        else permit.scope.creation_keys
                    )
                )
        allowed = frozenset(readable | creations)
        if not allowed or targets is not None and (not targets or not targets <= allowed):
            raise PermissionError("当前没有这项本人寄宿回执的读取权限。")
        proof = ResidenceOwnResultProof(
            allowed if targets is None else targets,
            tuple(permits),
            self,
            clock,
            frozenset(creations - readable) & (allowed if targets is None else targets),
        )
        proof.revalidate()
        return proof


class ResidenceScopeOwnerLookup:
    def __init__(self, sessions: Any) -> None:
        self.sessions = sessions

    def scope_exists(self, scope: Any, as_of: Any) -> Any:
        with self.sessions() as session:
            basic = (
                session.scalar(
                    text("SELECT app.school_fact_f6_scope_is_current(:school,:kind,:objects,:at)"),
                    {
                        "school": scope.school_id,
                        "kind": scope.fact_kind,
                        "objects": list(scope.object_keys),
                        "at": as_of,
                    },
                )
                is True
            )
            if not basic or not scope.quality_owners:
                return basic
            return (
                session.scalar(
                    text(
                        "SELECT app.school_fact_f6_quality_owners_current(:school,:ki"
                        "nd,:owners,:keys,:at)"
                    ),
                    {
                        "school": scope.school_id,
                        "kind": scope.fact_kind,
                        "owners": [p[0] for p in scope.quality_owners],
                        "keys": [p[1] for p in scope.quality_owners],
                        "at": as_of,
                    },
                )
                is True
            )


def create_residence_authority(*, sessions: Any, configuration: Any) -> Any:
    from app.composition.school_fact_authority_connection import ConnectionBoundAuthority
    from app.composition.school_fact_product_services import ReadOnlyGovernanceDependencies
    from app.platform.authorization.thin_service import ThinAuthorizationService
    from app.platform.governance.basis import GovernanceAuthorizationBasisService
    from app.platform.governance.reader import GovernanceAuthorityReader

    def build_basis(bound: Any) -> Any:
        reader = GovernanceAuthorityReader(
            session_factory=bound,
            dependency_port=ReadOnlyGovernanceDependencies(bound),
            scope_owner_resolvers={
                F6_SCOPE_OWNER: F6ScopeResolver(
                    lookup_scope=ResidenceScopeOwnerLookup(bound).scope_exists
                )
            },
        )
        return GovernanceAuthorizationBasisService(session_factory=bound, authority_reader=reader)

    connection = ConnectionBoundAuthority(sessions=sessions, build_basis=build_basis)
    thin = ThinAuthorizationService(
        governance_basis_service=connection, module_policies={F6_SCOPE_OWNER: F6FactPolicy()}
    )
    return ResidenceAuthority(
        thin=thin, configuration=configuration, connection_authority=connection
    )
