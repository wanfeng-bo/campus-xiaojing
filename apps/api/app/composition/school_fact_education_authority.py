"""Current exact education authority; one decision per scope, never per result row."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from contextlib import contextmanager, nullcontext
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID, uuid4

from app.composition.school_fact_education_settings import EducationConfiguration
from app.platform.authorization.thin_contracts import (
    NamedProjection,
    StaffActorBasis,
    ThinAuthorizationBasis,
    ThinAuthorizationDecision,
    ThinAuthorizationRequest,
    ThinDecisionKind,
)
from app.platform.contracts import CommandExecutionContext, StaffActorContext
from app.platform.school_facts.contracts import CanonicalFactChangeSetV1
from app.platform.school_facts.f4_policy import (
    F4_FACT_KINDS,
    F4_SCOPE_OWNER,
    F4FactPolicy,
    F4FactScope,
    F4ScopeResolver,
)
from app.platform.school_facts.f5_policy import (
    F5_FACT_KINDS,
    F5_SCOPE_OWNER,
    F5FactPolicy,
    F5FactScope,
    F5ScopeResolver,
)
from app.platform.school_facts.ports import SchoolFactOwnResultScope


class EducationAuthorityDenied(PermissionError):
    pass


@dataclass(frozen=True, slots=True)
class EducationPermit:
    actor: StaffActorContext
    action: str
    scope: F4FactScope | F5FactScope
    scope_id: UUID
    basis: ThinAuthorizationBasis


@dataclass(frozen=True, slots=True)
class _EducationReleasePermit:
    """One exact authority dependency, owned only by a synchronous release."""

    key: object
    permit: EducationPermit = field(compare=False, hash=False)


def _release_permit(permit: EducationPermit) -> _EducationReleasePermit:
    from app.platform.authorization.secure_reader_adapters import authorization_basis_fingerprint

    # This optimization is for result reads only. Maintenance and Writer final
    # guards retain their prior exact-permit behavior even inside a release.
    fingerprint = authorization_basis_fingerprint(permit.basis) if permit.action == "read" else None
    key = (
        ("exact", permit)
        if fingerprint is None
        else (permit.actor, permit.action, permit.scope_id, permit.scope, fingerprint)
    )
    return _EducationReleasePermit(key, permit)


@dataclass(frozen=True, slots=True)
class EducationChangePermit:
    actor: StaffActorContext
    change_set_sha256: str
    item_permits: tuple[EducationPermit, ...]


@dataclass(frozen=True, slots=True)
class EducationOwnResultProof:
    allowed_targets: SchoolFactOwnResultScope
    permits: tuple[EducationPermit, ...]
    authority: EducationAuthority
    clock: Callable[[], datetime]
    creation_only_targets: SchoolFactOwnResultScope = frozenset()

    def revalidate(self) -> None:
        if self.permits:
            self.authority.revalidate_many(
                self.permits, actor=self.permits[0].actor, checked_at=self.clock()
            )
            self.authority.register_permit_times(self.permits, self.clock)


class EducationAuthority:
    def __init__(
        self,
        *,
        thin: Any,
        configuration: EducationConfiguration,
        connection_authority: Any = None,
        root_validation: Any = None,
        fresh_root_validation: Any = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.thin, self.configuration = thin, configuration
        self.connection_authority = connection_authority
        self.root_validation = root_validation
        self.fresh_root_validation = fresh_root_validation
        self.clock = clock
        self.bindings = {
            (b.assignment_id, b.operation_code): b.scope_id for b in configuration.scope_bindings
        }

    def require_scope(
        self, actor: StaffActorContext, *, fact_kind: str, action: str, checked_at: datetime
    ) -> EducationPermit:
        if type(actor) is not StaffActorContext or checked_at.utcoffset() is None:
            raise EducationAuthorityDenied("当前身份不可验证。")
        if not self.configuration.enabled or fact_kind not in F4_FACT_KINDS | F5_FACT_KINDS:
            raise EducationAuthorityDenied("资料种类未获准。")
        owner = F5_SCOPE_OWNER if fact_kind in F5_FACT_KINDS else F4_SCOPE_OWNER
        code = f"{owner}.{fact_kind}.{action}"
        sid = self.bindings.get((actor.assignment_id, code))
        if sid is None:
            raise EducationAuthorityDenied("当前身份没有这项资料权限。")
        decision = self.thin.decide(
            ThinAuthorizationRequest(
                actor,
                owner,
                code,
                sid,
                code,
                actor.school_id,
                actor.authorization_version,
                uuid4(),
                checked_at,
            )
        )
        return self._scope_permit(actor, fact_kind, action, sid, decision)

    def require_scopes(
        self,
        actor: StaffActorContext,
        *,
        requests: tuple[tuple[str, str], ...],
        checked_at: datetime,
    ) -> tuple[EducationPermit, ...]:
        """Issue exact permits for one bounded synchronous read wave.

        Preflight the whole batch before owner IO. Only duplicate requests in
        this invocation share a decision; no permit or root snapshot is retained
        for another read wave, even inside a larger result-release batch.
        """
        if (
            type(actor) is not StaffActorContext
            or checked_at.utcoffset() is None
            or len(requests) > 128
        ):
            raise EducationAuthorityDenied("当前身份或核验范围无法验证。")
        if not self.configuration.enabled:
            raise EducationAuthorityDenied("资料种类未获准。")
        prepared: dict[tuple[str, str], ThinAuthorizationRequest] = {}
        for fact_kind, action in requests:
            if fact_kind not in F4_FACT_KINDS | F5_FACT_KINDS or action not in ("read", "maintain"):
                raise EducationAuthorityDenied("资料种类或操作未获准。")
            owner = F5_SCOPE_OWNER if fact_kind in F5_FACT_KINDS else F4_SCOPE_OWNER
            code = f"{owner}.{fact_kind}.{action}"
            sid = self.bindings.get((actor.assignment_id, code))
            if sid is None:
                raise EducationAuthorityDenied("当前身份没有这项资料权限。")
            if (fact_kind, action) not in prepared:
                prepared[fact_kind, action] = ThinAuthorizationRequest(
                    actor,
                    owner,
                    code,
                    sid,
                    code,
                    actor.school_id,
                    actor.authorization_version,
                    uuid4(),
                    checked_at,
                )
        if not prepared:
            return ()
        root_check = self.fresh_root_validation or self.root_validation
        root_scope = root_check(actor, checked_at) if root_check else nullcontext()
        batch = getattr(self.thin, "decide_many", None)
        trusted_actor = StaffActorBasis(**asdict(actor))
        permits: dict[tuple[str, str], EducationPermit] = {}
        with root_scope:
            pending = tuple(prepared.values())
            decisions = (
                batch(pending)
                if batch is not None
                else tuple(self.thin.decide(request) for request in pending)
            )
            if len(decisions) != len(pending):
                raise EducationAuthorityDenied("资料授权结果不完整。")
            for (fact_kind, action), request, decision in zip(
                prepared, pending, decisions, strict=True
            ):
                if isinstance(decision, Exception):
                    raise decision
                policy = F5FactPolicy() if fact_kind in F5_FACT_KINDS else F4FactPolicy()
                requirement = policy.requirement_for(request)
                if (
                    not isinstance(decision, ThinAuthorizationDecision)
                    or not isinstance(decision.basis, ThinAuthorizationBasis)
                    or decision.request_id != request.request_id
                    or decision.basis.request_id != request.request_id
                    or decision.decision_id != decision.basis.decision_id
                    or decision.basis.actor != trusted_actor
                    or decision.basis.module_name != request.module_name
                    or decision.basis.action_code != request.action_code
                    or decision.basis.resource_type != request.resource_type
                    or decision.basis.resource_id != request.resource_id
                    or decision.basis.resource_revision != request.resource_revision
                    or decision.basis.governance.school_id != actor.school_id
                    or decision.basis.governance.assignment_id != actor.assignment_id
                    or decision.basis.governance.scope_id != request.scope_id
                    or decision.basis.module_policy_version != policy.version
                    or requirement is None
                    or decision.basis.governance.duty_code != requirement.duty_code
                ):
                    raise EducationAuthorityDenied("资料授权结果与本次请求不符。")
                permits[fact_kind, action] = self._scope_permit(
                    actor, fact_kind, action, request.scope_id, decision
                )
        self.register_permit_times(tuple(permits.values()), self.clock)
        return tuple(permits[request] for request in requests)

    def revalidate_read_wave(
        self,
        permits: tuple[EducationPermit, ...],
        *,
        actor: StaffActorContext,
        checked_at: datetime,
    ) -> None:
        """Immediately recheck this read wave, independently of release reuse."""
        if (
            len(permits) > 128
            or checked_at.utcoffset() is None
            or type(actor) is not StaffActorContext
            or any(permit.actor != actor for permit in permits)
        ):
            raise EducationAuthorityDenied("当前身份或核验范围已经变化。")
        unique = tuple(dict.fromkeys(permits))
        if unique:
            self._revalidate_many_now(unique, actor=actor, checked_at=checked_at, fresh_roots=True)
            self.register_permit_times(unique, self.clock)

    @staticmethod
    def _scope_permit(
        actor: StaffActorContext,
        fact_kind: str,
        action: str,
        sid: UUID,
        decision: ThinAuthorizationDecision,
    ) -> EducationPermit:
        owner = F5_SCOPE_OWNER if fact_kind in F5_FACT_KINDS else F4_SCOPE_OWNER
        scope_type = F5FactScope if fact_kind in F5_FACT_KINDS else F4FactScope
        phase = "f5" if fact_kind in F5_FACT_KINDS else "f4"
        if (
            decision.kind is not ThinDecisionKind.ALLOW
            or decision.scope_plan is None
            or decision.scope_plan.owner != owner
            or decision.scope_plan.scope_id != sid
            or decision.projection != NamedProjection(f"{phase}_{fact_kind}_{action}", 1)
            or not isinstance(decision.basis, ThinAuthorizationBasis)
        ):
            raise EducationAuthorityDenied("当前身份没有这项资料权限。")
        scope = scope_type.from_reference(decision.scope_plan.reference)
        if scope.school_id != actor.school_id or scope.fact_kind != fact_kind:
            raise EducationAuthorityDenied("资料范围无法核实。")
        return EducationPermit(actor, action, scope, sid, decision.basis)

    @staticmethod
    def require_fields(permit: EducationPermit, fields: tuple[str, ...]) -> None:
        if not fields or not set(fields) <= set(permit.scope.field_names):
            raise EducationAuthorityDenied("当前身份无权查看或修改这些内容。")

    @staticmethod
    def require_object(permit: EducationPermit, source_key: str, *, is_creation: bool) -> None:
        if is_creation and permit.action != "maintain":
            raise EducationAuthorityDenied("读取不能代替新建授权。")
        if not permit.scope.allows_key(source_key, is_creation=is_creation):
            raise EducationAuthorityDenied("对象不在本次获准范围内。")

    def revalidate(
        self, permit: EducationPermit, *, actor: StaffActorContext, checked_at: datetime
    ) -> None:
        if actor != permit.actor or checked_at.utcoffset() is None:
            raise EducationAuthorityDenied("当前身份已经变化。")
        decision = self.thin.revalidate(
            permit.basis,
            actor=actor,
            as_of=checked_at,
            resource_revision=actor.authorization_version,
        )
        if decision.kind is not ThinDecisionKind.ALLOW:
            raise EducationAuthorityDenied("权限已经变化，请重新核对。")

    def revalidate_many(
        self,
        permits: tuple[EducationPermit, ...],
        *,
        actor: StaffActorContext,
        checked_at: datetime,
    ) -> None:
        """One current owner round, retaining every exact Thin/Policy decision.

        Exact permits and immutable roots may share one trusted synchronous
        release batch. Its exit rechecks live decisions, roots and time; nothing
        survives that batch, a model wait, or the next current/release boundary.
        """
        if (
            len(permits) > 128
            or checked_at.utcoffset() is None
            or type(actor) is not StaffActorContext
            or any(permit.actor != actor for permit in permits)
        ):
            raise EducationAuthorityDenied("当前身份或核验范围已经变化。")
        unique = tuple(dict.fromkeys(permits))
        if not unique:
            return
        from app.composition.school_fact_read_evidence import current_validation_batch

        release = current_validation_batch()
        if release is not None:
            # Trace ids/issue instants do not create another current authority.
            # The stable basis includes grant, scope, epoch and dependency
            # versions. Keep content/time proofs separate and register EVERY
            # original permit's own window below, including equivalent items.
            items = tuple(_release_permit(permit) for permit in unique)
            release.check_authority(
                (id(self), actor),
                items,
                lambda selected: self._revalidate_many_now(
                    tuple(item.permit for item in selected), actor=actor, checked_at=checked_at
                ),
                lambda items: self._revalidate_many_now(
                    tuple(item.permit for item in items), actor=actor, checked_at=self.clock()
                ),
            )
            self.register_permit_times(unique, self.clock)
            return
        self._revalidate_many_now(unique, actor=actor, checked_at=checked_at)
        self.register_permit_times(unique, self.clock)

    @staticmethod
    def register_permit_times(
        permits: tuple[EducationPermit, ...], clock: Callable[[], datetime]
    ) -> None:
        from app.composition.school_fact_read_evidence import register_release_time_window
        from app.platform.governance.contracts import GovernanceAuthorizationBasis

        for permit in permits:
            governance = getattr(getattr(permit, "basis", None), "governance", None)
            if isinstance(governance, GovernanceAuthorizationBasis):
                register_release_time_window(
                    clock, observed_at=governance.issued_as_of, valid_before=governance.valid_until
                )

    def _revalidate_many_now(
        self,
        unique: tuple[EducationPermit, ...],
        *,
        actor: StaffActorContext,
        checked_at: datetime,
        fresh_roots: bool = False,
    ) -> None:
        batch = getattr(self.thin, "revalidate_many", None)
        if batch is None and not fresh_roots:
            for permit in unique:
                self.revalidate(permit, actor=actor, checked_at=checked_at)
            return
        root_check = (
            self.fresh_root_validation or self.root_validation
            if fresh_roots
            else self.root_validation
        )
        scope = root_check(actor, checked_at) if root_check else nullcontext()
        with scope:
            if batch is None:
                for permit in unique:
                    self.revalidate(permit, actor=actor, checked_at=checked_at)
                return
            results = batch(
                tuple(permit.basis for permit in unique),
                actor=actor,
                as_of=checked_at,
                resource_revision=actor.authorization_version,
            )
            if len(results) != len(unique) or any(
                isinstance(result, Exception) or result.kind is not ThinDecisionKind.ALLOW
                for result in results
            ):
                raise EducationAuthorityDenied("权限已经变化或无法核实，请重新核对。")

    @staticmethod
    def _digest(change_set: CanonicalFactChangeSetV1) -> str:
        return hashlib.sha256(change_set.model_dump_json().encode()).hexdigest()

    def require_change_set(
        self,
        actor: StaffActorContext,
        change_set: CanonicalFactChangeSetV1,
        *,
        checked_at: datetime,
    ) -> EducationChangePermit:
        if not self.configuration.enabled:
            raise EducationAuthorityDenied("教育资料维护已暂停。")
        permits: dict[str, EducationPermit] = {}
        for change in change_set.changes:
            kind = str(change.fact_kind)
            if kind not in permits:
                permits[kind] = self.require_scope(
                    actor, fact_kind=kind, action="maintain", checked_at=checked_at
                )
            permit = permits[kind]
            self.require_object(
                permit, str(change.source_key), is_creation=change.expected_version == 0
            )
            fields = (
                tuple(change.content.model_fields_set)
                if change.content is not None
                else ("valid_to",)
            )
            if change.replacement_segments is not None:
                fields = tuple(
                    sorted(
                        {"valid_to"}
                        | {
                            field
                            for segment in change.replacement_segments
                            for field in segment.content.model_fields_set
                        }
                    )
                )
            self.require_fields(permit, fields)
        if not permits:
            raise EducationAuthorityDenied("没有可确认的教育资料修改。")
        return EducationChangePermit(actor, self._digest(change_set), tuple(permits.values()))

    def final_guard(
        self,
        permit: EducationChangePermit,
        actor: StaffActorContext,
        change_set: CanonicalFactChangeSetV1,
        *,
        checked_at: datetime,
    ) -> None:
        if actor != permit.actor or self._digest(change_set) != permit.change_set_sha256:
            raise EducationAuthorityDenied("确认内容已经变化。")
        self.revalidate_many(permit.item_permits, actor=actor, checked_at=checked_at)

    def own_result(
        self,
        context: CommandExecutionContext,
        clock: Callable[[], datetime] | None = None,
        *,
        targets: frozenset[tuple[str, str]] | None = None,
    ) -> EducationOwnResultProof:
        if (
            type(context) is not CommandExecutionContext
            or type(context.actor) is not StaffActorContext
        ):
            raise EducationAuthorityDenied("本人历史回执需要当前登录身份。")
        actor = context.actor
        clock = clock or self.clock
        operations = tuple(
            operation
            for assignment, operation in self.bindings
            if assignment == actor.assignment_id
            and operation.endswith((".read", ".maintain"))
            and (targets is None or operation.split(".")[2] in {k for k, _ in targets})
        )
        permits = self._own_result_permits(actor, operations, clock)
        readable = frozenset(
            (p.scope.fact_kind, key)
            for p in permits
            if p.action == "read"
            for key in (
                (
                    key
                    for kind, key in targets
                    if kind == p.scope.fact_kind and p.scope.allows_key(key)
                )
                if targets is not None and p.scope.is_school_wide
                else p.scope.object_keys
            )
        )
        creations = frozenset(
            (p.scope.fact_kind, key)
            for p in permits
            if p.action == "maintain"
            for key in (
                (
                    key for kind, key in targets
                    if kind == p.scope.fact_kind and p.scope.allows_key(key, is_creation=True)
                )
                if targets is not None and p.scope.is_school_wide
                else p.scope.creation_keys
            )
        )
        allowed = readable | creations
        if not allowed or (targets is not None and (not targets or not targets <= allowed)):
            raise EducationAuthorityDenied("当前没有获准查看的本人教育资料回执。")
        self.register_permit_times(tuple(permits), clock)
        return EducationOwnResultProof(
            allowed if targets is None else targets,
            tuple(permits),
            self,
            clock,
            (creations - readable) & (allowed if targets is None else targets),
        )

    def _own_result_permits(
        self, actor: StaffActorContext, operations: tuple[str, ...], clock: Callable[[], datetime]
    ) -> list[EducationPermit]:
        """Batch current owner IO; every read/maintain action keeps its exact decision.

        The root round ends before any receipt read and is never retained in the
        returned proof. Writer still revalidates that proof after reading.
        """
        permits: list[EducationPermit] = []
        batch = getattr(getattr(self, "thin", None), "decide_many", None)
        if not callable(batch):
            for operation in operations:
                try:
                    permits.append(
                        self.require_scope(
                            actor,
                            fact_kind=operation.split(".")[2],
                            action=operation.rsplit(".", 1)[1],
                            checked_at=clock(),
                        )
                    )
                except EducationAuthorityDenied:
                    continue
            return permits
        if not operations or not self.configuration.enabled:
            return permits
        at = clock()
        if at.utcoffset() is None:
            raise EducationAuthorityDenied("当前身份不可验证。")
        requests = tuple(
            ThinAuthorizationRequest(
                actor,
                operation.rsplit(".", 2)[0],
                operation,
                self.bindings[(actor.assignment_id, operation)],
                operation,
                actor.school_id,
                actor.authorization_version,
                uuid4(),
                at,
            )
            for operation in operations
            if operation.split(".")[2] in F4_FACT_KINDS | F5_FACT_KINDS
        )
        scope = self.root_validation(actor, at) if self.root_validation else nullcontext()
        with scope:
            decisions = cast(tuple[ThinAuthorizationDecision, ...], batch(requests))
            if len(decisions) != len(requests):
                raise EducationAuthorityDenied("当前回执读取授权不完整。")
        for request, decision in zip(requests, decisions, strict=True):
            kind, action = request.action_code.split(".")[2:]
            try:
                permit = self._scope_permit(actor, kind, action, request.scope_id, decision)
                if (
                    permit.basis.actor != StaffActorBasis(**asdict(actor))
                    or permit.basis.module_name != request.module_name
                    or permit.basis.action_code != request.action_code
                    or permit.basis.resource_type != request.resource_type
                    or permit.basis.resource_id != request.resource_id
                    or permit.basis.resource_revision != request.resource_revision
                    or permit.basis.request_id != request.request_id
                    or decision.request_id != request.request_id
                ):
                    raise EducationAuthorityDenied("当前回执读取身份或操作已变化。")
                permits.append(permit)
            except EducationAuthorityDenied:
                # An independently denied scope never prevents another permitted
                # receipt; complete requested target coverage is checked by caller.
                continue
        return permits


class EducationScopeOwnerLookup:
    def __init__(self, sessions: Any) -> None:
        self.sessions = sessions

    def scope_exists(self, scope: F4FactScope | F5FactScope, as_of: datetime) -> bool:
        from sqlalchemy import text

        function = (
            "school_fact_f5_scope_is_current"
            if type(scope) is F5FactScope
            else "school_fact_f4_scope_is_current"
        )
        with self.sessions.begin() as session:
            return (
                session.scalar(
                    text(f"SELECT app.{function}(:school,:kind,:objects,:as_of)"),
                    {
                        "school": scope.school_id,
                        "kind": scope.fact_kind,
                        "objects": list(scope.object_keys),
                        "as_of": as_of,
                    },
                )
                is True
            )

    def scope_exists_many(
        self, scopes: tuple[F4FactScope | F5FactScope, ...], times: tuple[datetime, ...]
    ) -> tuple[bool, ...]:
        """Call the unchanged Reader functions in one bounded current SQL statement."""
        from sqlalchemy import text

        if (
            len(scopes) > 128
            or len(scopes) != len(times)
            or any(type(scope) not in {F4FactScope, F5FactScope} for scope in scopes)
            or any(type(at) is not datetime or at.utcoffset() is None for at in times)
        ):
            raise ValueError("Invalid bounded education scope request.")
        if not scopes:
            return ()
        requests = [
            {
                "ordinal": index,
                "phase": "f5" if type(scope) is F5FactScope else "f4",
                "school_id": str(scope.school_id),
                "kind": scope.fact_kind,
                "objects": list(scope.object_keys),
                "as_of": at.isoformat(),
            }
            for index, (scope, at) in enumerate(zip(scopes, times, strict=True))
        ]
        # Fixed SQL function names only. A retained F4 deployment need not have
        # the later F5 function installed, even in an unused CASE branch.
        arguments = (
            "(r.school_id, r.kind, ARRAY(SELECT jsonb_array_elements_text(r.objects)), r.as_of)"
        )
        checks = {
            F4FactScope: "app.school_fact_f4_scope_is_current" + arguments,
            F5FactScope: "app.school_fact_f5_scope_is_current" + arguments,
        }
        kinds = {type(scope) for scope in scopes}
        check = (
            checks[type(scopes[0])]
            if len(kinds) == 1
            else f"CASE r.phase WHEN 'f5' THEN {checks[F5FactScope]} ELSE {checks[F4FactScope]} END"
        )
        with self.sessions.begin() as session:
            rows = session.execute(
                text(f"""
                    SELECT r.ordinal, {check} AS allowed
                    FROM jsonb_to_recordset(CAST(:requests AS jsonb)) AS r(
                        ordinal integer, phase text, school_id uuid, kind text,
                        objects jsonb, as_of timestamptz)
                    ORDER BY r.ordinal
                """),
                {"requests": json.dumps(requests)},
            ).all()
        if len(rows) != len(scopes) or any(
            type(ordinal) is not int or ordinal != index or type(allowed) is not bool
            for index, (ordinal, allowed) in enumerate(rows)
        ):
            raise ValueError("Education scope owner returned an invalid batch.")
        return tuple(allowed for _, allowed in rows)


def create_education_authority(
    *, sessions: Any, configuration: EducationConfiguration, evidence_owner: object | None = None
) -> EducationAuthority:
    from app.composition.school_fact_authority_connection import ConnectionBoundAuthority
    from app.composition.school_fact_product_services import (
        ProductThinAuthorizationService,
        ReadOnlyGovernanceDependencies,
    )
    from app.composition.school_fact_read_capture import (
        CapturingThinService,
        EvidenceGovernanceDependencies,
        _OwnerRootRound,  # pyright: ignore[reportPrivateUsage]
    )
    from app.platform.governance.basis import GovernanceAuthorizationBasisService
    from app.platform.governance.reader import GovernanceAuthorityReader

    dependency_owner = evidence_owner if evidence_owner is not None else object()

    def build_basis(bound: Any) -> GovernanceAuthorizationBasisService:
        scope_owner = EducationScopeOwnerLookup(bound)
        reader = GovernanceAuthorityReader(
            session_factory=bound,
            dependency_port=EvidenceGovernanceDependencies(bound, owner=dependency_owner),
            scope_owner_resolvers={
                F4_SCOPE_OWNER: F4ScopeResolver(
                    lookup_scope=scope_owner.scope_exists,
                    lookup_scope_many=scope_owner.scope_exists_many,
                ),
                F5_SCOPE_OWNER: F5ScopeResolver(
                    lookup_scope=scope_owner.scope_exists,
                    lookup_scope_many=scope_owner.scope_exists_many,
                ),
            },
        )
        return GovernanceAuthorizationBasisService(session_factory=bound, authority_reader=reader)

    connection = ConnectionBoundAuthority(sessions=sessions, build_basis=build_basis)
    thin = (ProductThinAuthorizationService if evidence_owner is None else CapturingThinService)(
        governance_basis_service=connection,
        module_policies={F4_SCOPE_OWNER: F4FactPolicy(), F5_SCOPE_OWNER: F5FactPolicy()},
    )
    from app.composition.school_fact_directory_services import (
        _fresh_root_scope,  # pyright: ignore[reportPrivateUsage]
    )
    from app.platform.assistant.runtime import AssistantRuntimeError

    @contextmanager
    def root_validation(
        actor: StaffActorContext, checked_at: datetime, *, independent: bool = False
    ):
        # Only this synchronous batch owns these immutable dependency snapshots.
        # Thin, grant, scope and epoch reads are never cached by this round.
        from app.composition.school_fact_read_evidence import current_validation_batch

        release = None if independent else current_validation_batch()
        key = ("education", id(dependency_owner), actor)
        round_ = release.root_rounds.get(key) if release is not None else None
        if round_ is None:
            round_ = _OwnerRootRound(
                dependency_owner,
                actor,
                checked_at,
                ReadOnlyGovernanceDependencies(sessions),
                lambda: datetime.now(UTC),
                fresh_scope=lambda: _fresh_root_scope(sessions),
            )
            if release is not None:
                release.root_rounds[key] = round_
        try:
            with connection.read_scope(actor), round_.activate(decision_at=checked_at):
                yield
            # Run before revalidate_many returns, within its existing error boundary.
            try:
                if release is None or not release.rechecking_authority:
                    round_.finish()
            except AssistantRuntimeError as exc:
                raise EducationAuthorityDenied("当前身份依赖已经变化，请重新核对。") from exc
        finally:
            if release is None:
                round_.clear()

    return EducationAuthority(
        thin=thin,
        configuration=configuration,
        connection_authority=connection,
        root_validation=root_validation,
        fresh_root_validation=lambda actor, checked_at: root_validation(
            actor, checked_at, independent=True
        ),
    )
