"""F6 partition within the existing sole Writer and own-receipt boundary."""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text

from app.composition.school_fact_family_execution import PartitionedOwnResultAuthorization
from app.composition.school_fact_residence_authority import ResidenceOwnResultProof
from app.composition.school_fact_residence_maintenance import residence_dependency_changes
from app.platform.contracts import StaffActorContext
from app.platform.school_facts.application import SchoolFactChangeRejected
from app.platform.school_facts.f6_policy import F6_FACT_FIELDS, F6_FACT_KINDS


class ResidenceFinalGuard:
    def __init__(self, authority: Any, *, enabled: bool = True) -> None:
        self.authority = authority
        self.enabled = enabled

    def __call__(self, session: Any, context: Any, change_set: Any) -> Any:
        if not self.enabled:
            raise SchoolFactChangeRejected("寄宿正式维护目前未启用。")
        actor = context.actor
        if type(actor) is not StaffActorContext:
            raise SchoolFactChangeRejected("当前执行身份不可确认。")
        epoch = session.scalar(
            text("SELECT app.lock_school_fact_governance_epoch(:school,:assignment)"),
            {"school": actor.school_id, "assignment": actor.assignment_id},
        )
        if type(epoch) is not int or epoch < 1:
            raise SchoolFactChangeRejected("当前授权版本不可确认。")
        try:
            permits = self.authority.require_change_set(
                actor, change_set, checked_at=datetime.now(UTC)
            )
            if not permits or any(p.basis.governance.assignment_epoch != epoch for p in permits):
                raise PermissionError("执行前权限已变化")
            for permit in permits:
                self.authority.revalidate(permit, actor=actor, checked_at=datetime.now(UTC))
        except PermissionError:
            raise SchoolFactChangeRejected("当前身份没有本批寄宿资料的完整维护权限。") from None


class ResidenceWriterDispatch:
    def __init__(
        self, *, residence: Any, retained: Any, quality: Any = None, dependency: Any = None
    ) -> None:
        self.residence, self.retained, self.quality = residence, retained, quality
        self.dependency = dependency

    def __call__(self, session: Any, context: Any, change_set: Any) -> Any:
        residence: list[Any]
        quality: list[Any]
        retained: list[Any]
        dependency: list[Any]
        residence, quality, retained, dependency = [], [], [], []
        dependency_changes = residence_dependency_changes(change_set)
        for change in change_set.changes:
            if change.fact_kind in F6_FACT_KINDS:
                residence.append(change)
            elif (
                change.fact_kind == "fact_quality"
                and self.quality is not None
                and self.quality.recognizes_change(context.actor, change)
            ):
                quality.append(change)
            elif (
                change.fact_kind == "fact_quality"
                and self.quality is None
                and (
                    getattr(change.content, "target_fact_kind", None) in F6_FACT_KINDS
                    or change.content is None
                )
            ):
                raise SchoolFactChangeRejected("质量依据所有者尚未核实，不能借用旧维护权限。")
            else:
                (dependency if change in dependency_changes else retained).append(change)
        for values, guard in (
            (residence, self.residence),
            (quality, self.quality),
            (dependency, self.dependency),
            (retained, self.retained),
        ):
            if not values:
                continue
            if guard is None:
                raise SchoolFactChangeRejected("这项寄宿依据的正式维护尚未接通。")
            guard(session, context, change_set.model_copy(update={"changes": tuple(values)}))


class ResidenceQualityFinalGuard:
    def __init__(self, bridge: Any, *, enabled: bool = True) -> None:
        self.bridge = bridge
        self.enabled = enabled

    def recognizes_change(self, actor: Any, change: Any) -> Any:
        return self.bridge.recognizes_change(actor, change)

    def __call__(self, session: Any, context: Any, change_set: Any) -> Any:
        if not self.enabled:
            raise SchoolFactChangeRejected("寄宿质量维护目前未启用。")
        actor = context.actor
        if type(actor) is not StaffActorContext:
            raise SchoolFactChangeRejected("当前执行身份不可确认。")
        epoch = session.scalar(
            text("SELECT app.lock_school_fact_governance_epoch(:school,:assignment)"),
            {"school": actor.school_id, "assignment": actor.assignment_id},
        )
        try:
            proof = self.bridge.require_change_set(actor, change_set)
            if (
                type(epoch) is not int
                or epoch < 1
                or any(
                    p.basis.governance.assignment_epoch != epoch
                    for p in (*proof.f2_permits, *proof.residence_permits)
                )
            ):
                raise PermissionError("质量依据权限已变化")
            proof.revalidate()
        except PermissionError:
            raise SchoolFactChangeRejected("当前没有本批寄宿质量及目标的完整维护权限。") from None


class ResidenceOwnResultAuthorizer:
    def __init__(
        self,
        *,
        residence: Any,
        retained: Any,
        original_authority: Any = None,
        quality: Any = None,
        dependency: Any = None,
    ) -> None:
        self.residence, self.retained, self.original_authority, self.quality = (
            residence,
            retained,
            original_authority,
            quality,
        )
        self.dependency = dependency

    def authorize_original(self, context: Any, target: Any, fields: Any) -> Any:
        if target.creation_receipt:
            raise PermissionError("新建回执只允许核对审计元数据。")
        if target.fact_kind not in F6_FACT_KINDS:
            selected = frozenset({(target.fact_kind, target.source_key)})
            proofs: list[Any] = []
            for authorizer in (self.retained, self.dependency):
                if authorizer is None:
                    continue
                try:
                    proof = authorizer.authorize_original(context, target, fields)
                except (PermissionError, SchoolFactChangeRejected):
                    continue
                if selected <= proof.allowed_targets:
                    proofs.append(proof)
            if not proofs:
                raise PermissionError("当前没有原始内容的完整字段读取权限。")
            if target.fact_kind == "fact_quality" and self.quality is not None:
                quality = self.quality.require_read(context.actor, target.source_key)
                if quality is not None:
                    proofs.append(quality)
            result = PartitionedOwnResultAuthorization(selected, tuple(proofs))
            result.revalidate()
            return result
        authority = self.original_authority
        if (
            authority is None
            or target.creation_receipt
            or type(context.actor) is not StaffActorContext
        ):
            raise PermissionError("当前没有原始寄宿内容读取权限。")
        if (
            not fields
            or len(set(fields)) != len(fields)
            or not set(fields) <= F6_FACT_FIELDS[target.fact_kind]
        ):
            raise ValueError("须选择有限寄宿原文内容字段。")
        permit = authority.require_scope(
            context.actor, fact_kind=target.fact_kind, action="read", checked_at=authority.clock()
        )
        authority.require_object(permit, target.source_key, is_creation=False)
        authority.require_fields(permit, fields)
        proof = ResidenceOwnResultProof(
            frozenset({(target.fact_kind, target.source_key)}),
            (permit,),
            authority,
            authority.clock,
        )
        proof.revalidate()
        return proof

    def for_targets(self, context: Any, targets: Any) -> Any:
        if not targets:
            raise PermissionError("没有获准回执目标。")
        selected = frozenset(t for t in targets if t[0] in F6_FACT_KINDS)
        other = targets - selected
        proofs: list[Any] = []
        if selected:
            proofs.append(self.residence(context, targets=selected))
        for target in sorted(other):
            exact = frozenset({target})
            for authorizer in (self.retained, self.dependency):
                if authorizer is None:
                    continue
                try:
                    proof = authorizer.for_targets(context, exact)
                except (PermissionError, SchoolFactChangeRejected):
                    continue
                if exact <= proof.allowed_targets:
                    proofs.append(PartitionedOwnResultAuthorization(exact, (proof,)))
        if not targets <= frozenset(t for proof in proofs for t in proof.allowed_targets):
            raise PermissionError("当前没有本回执全部目标的读取权限。")
        if self.quality is not None:
            creation_only = PartitionedOwnResultAuthorization(
                targets, tuple(proofs)
            ).creation_only_targets
            for kind, key in other - creation_only:
                if kind == "fact_quality":
                    proof = self.quality.require_read(context.actor, key)
                    if proof is not None:
                        proofs.append(proof)
        result = PartitionedOwnResultAuthorization(targets, tuple(proofs))
        result.revalidate()
        return result

    def __call__(self, context: Any) -> Any:
        targets: set[Any]
        proofs: list[Any]
        targets, proofs = set(), []
        for callback, residence in (
            (self.residence, True),
            (self.retained, False),
            (self.dependency, False),
        ):
            if callback is None:
                continue
            try:
                proof = callback(context)
            except (PermissionError, SchoolFactChangeRejected):
                continue
            subset = {t for t in proof.allowed_targets if (t[0] in F6_FACT_KINDS) is residence}
            if subset:
                targets.update(subset)
                # Retain this request's owner proof. Its scope already covers
                # these exact objects; reissuing it per object adds no authority.
                # Partition wrappers preserve owner and creation-only semantics.
                proofs.append(PartitionedOwnResultAuthorization(frozenset(subset), (proof,)))
        allowed = set(targets)
        if self.quality is not None:
            creation_only = PartitionedOwnResultAuthorization(
                frozenset(targets), tuple(proofs)
            ).creation_only_targets
            for target in sorted(targets - creation_only):
                kind, key = target
                if kind != "fact_quality":
                    continue
                try:
                    proof = self.quality.require_read(context.actor, key)
                except (PermissionError, SchoolFactChangeRejected):
                    allowed.remove(target)
                    continue
                if proof is not None:
                    proofs.append(PartitionedOwnResultAuthorization(frozenset({target}), (proof,)))
        if not allowed:
            raise PermissionError("没有获准回执目标。")
        selected = frozenset(allowed)
        result = PartitionedOwnResultAuthorization(
            selected,
            tuple(
                PartitionedOwnResultAuthorization(proof.allowed_targets & selected, (proof,))
                for proof in proofs
                if proof.allowed_targets & selected
            ),
        )
        # Current scopes and quality targets must still hold before the limited
        # receipt projection; Writer repeats this check after reading. No proof
        # is cached across requests, model waits, identities or receipt uses.
        result.revalidate()
        return result
