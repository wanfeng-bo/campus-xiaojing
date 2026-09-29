"""Finite education partition inside the retained sole Writer and receipt boundary."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text

from app.composition.school_fact_education_authority import EducationAuthorityDenied
from app.composition.school_fact_family_execution import PartitionedOwnResultAuthorization
from app.domains.students.education_contracts import EDUCATION_FACT_KINDS
from app.platform.contracts import StaffActorContext
from app.platform.school_facts.application import SchoolFactChangeRejected


class EducationFinalGuard:
    def __init__(self, authority: Any) -> None:
        self.authority = authority

    def __call__(self, session: Any, context: Any, change_set: Any) -> None:
        if type(context.actor) is not StaffActorContext:
            raise SchoolFactChangeRejected("当前执行身份不可确认。")
        actor = context.actor
        epoch = session.scalar(
            text("SELECT app.lock_school_fact_governance_epoch(:school,:assignment)"),
            {"school": actor.school_id, "assignment": actor.assignment_id},
        )
        if type(epoch) is not int or epoch < 1:
            raise SchoolFactChangeRejected("当前授权版本不可确认。")
        try:
            permit = self.authority.require_change_set(
                actor, change_set, checked_at=datetime.now(UTC)
            )
            if any(item.basis.governance.assignment_epoch != epoch for item in permit.item_permits):
                raise SchoolFactChangeRejected("权限在执行前已变化，请重新核对。")
            self.authority.final_guard(permit, actor, change_set, checked_at=datetime.now(UTC))
        except EducationAuthorityDenied:
            raise SchoolFactChangeRejected("当前身份没有本批教育资料的完整维护权限。") from None


class EducationWriterDispatch:
    def __init__(self, *, education: Any, retained: Any, dependency: Any = None) -> None:
        self.education, self.retained, self.dependency = education, retained, dependency

    def __call__(self, session: Any, context: Any, change_set: Any) -> None:
        education = tuple(c for c in change_set.changes if c.fact_kind in EDUCATION_FACT_KINDS)
        retained = tuple(c for c in change_set.changes if c.fact_kind not in EDUCATION_FACT_KINDS)
        dependencies = tuple(c for c in retained if getattr(c, "schema_version", None) in {3, 4})
        retained = tuple(c for c in retained if getattr(c, "schema_version", None) not in {3, 4})
        if education:
            self.education(session, context, change_set.model_copy(update={"changes": education}))
        if retained:
            self.retained(session, context, change_set.model_copy(update={"changes": retained}))
        if dependencies:
            if self.dependency is None:
                raise SchoolFactChangeRejected("教育关联资料的正式维护尚未接通。")
            self.dependency(
                session, context, change_set.model_copy(update={"changes": dependencies})
            )


class EducationDependencyFinalGuard:
    """Retain each existing fact owner under explicit F4 dependency scopes."""

    def __init__(self, *, quality: Any, retained: Any) -> None:
        self.quality, self.retained = quality, retained

    def __call__(self, session: Any, context: Any, change_set: Any) -> None:
        dependencies = tuple(
            c for c in change_set.changes if c.fact_kind in {"fact_quality", "policy_scope"}
        )
        retained = tuple(
            c for c in change_set.changes if c.fact_kind not in {"fact_quality", "policy_scope"}
        )
        if retained:
            self.retained(session, context, change_set.model_copy(update={"changes": retained}))
        if not dependencies:
            return
        if type(context.actor) is not StaffActorContext:
            raise SchoolFactChangeRejected("当前正式身份不可验证。")
        actor = context.actor
        epoch = session.scalar(
            text("SELECT app.lock_school_fact_governance_epoch(:school,:assignment)"),
            {"school": actor.school_id, "assignment": actor.assignment_id},
        )
        if type(epoch) is not int or epoch < 1:
            raise SchoolFactChangeRejected("当前维护授权版本不可验证。")
        try:
            proof = self.quality.require_change_set(
                actor, change_set.model_copy(update={"changes": dependencies})
            )
            if any(
                p.basis.governance.assignment_epoch != epoch
                for p in (*proof.f2_permits, *proof.education_permits)
            ):
                raise SchoolFactChangeRejected("教育依据的维护权限已经变化。")
            proof.revalidate()
        except PermissionError:
            raise SchoolFactChangeRejected("当前身份没有修改这项教育依据的完整权限。") from None


class EducationOwnResultAuthorizer:
    def __init__(
        self,
        *,
        education: Any,
        retained: Any,
        dependency: Any = None,
        original_authority: Any = None,
    ) -> None:
        self.education, self.retained, self.dependency = education, retained, dependency
        self.original_authority = original_authority

    def authorize_original(self, context: Any, target: Any, fields: tuple[str, ...]):
        from app.composition.school_fact_education_authority import EducationOwnResultProof
        from app.platform.school_facts.ports import validate_own_original_selection

        validate_own_original_selection({target.fact_kind: fields}, 0, 1)
        authority = self.original_authority
        if (
            authority is None
            or target.creation_receipt
            or type(context.actor) is not StaffActorContext
        ):
            raise PermissionError("Original receipt content has no current read authority.")
        permit = authority.require_scope(
            context.actor,
            fact_kind=target.fact_kind,
            action="read",
            checked_at=authority.clock(),
        )
        authority.require_object(permit, target.source_key, is_creation=False)
        authority.require_fields(permit, fields)
        return EducationOwnResultProof(
            frozenset({(target.fact_kind, target.source_key)}),
            (permit,),
            authority,
            authority.clock,
        )

    def for_targets(
        self, context: Any, targets: frozenset[tuple[str, str]]
    ) -> PartitionedOwnResultAuthorization:
        if not targets:
            raise SchoolFactChangeRejected("当前没有获准查看的本人正式回执。")
        proofs = []
        education = frozenset(t for t in targets if t[0] in EDUCATION_FACT_KINDS)
        retained = targets - education
        if education:
            proofs.append(self.education(context, targets=education))
        if retained:
            allowed = set()
            for authorizer in (self.retained, self.dependency):
                if authorizer is None:
                    continue
                try:
                    proof = authorizer.for_targets(context, retained)
                except (PermissionError, SchoolFactChangeRejected):
                    continue
                proofs.append(proof)
                allowed.update(proof.allowed_targets)
            if not retained <= allowed:
                raise SchoolFactChangeRejected("当前没有查看本回执全部目标的权限。")
        result = PartitionedOwnResultAuthorization(targets, tuple(proofs))
        return result

    def __call__(self, context: Any) -> PartitionedOwnResultAuthorization:
        targets: set[tuple[str, str]] = set()
        proofs: list[Any] = []
        for authorizer, education in (
            (self.education, True),
            (self.retained, False),
            (self.dependency, False),
        ):
            if authorizer is None:
                continue
            try:
                proof = authorizer(context)
            except (PermissionError, SchoolFactChangeRejected):
                continue
            selected = {
                (kind, key)
                for kind, key in proof.allowed_targets
                if (kind in EDUCATION_FACT_KINDS) is education
            }
            if selected:
                targets.update(selected)
                proofs.append(proof)
        if not targets:
            raise SchoolFactChangeRejected("当前没有获准查看的本人正式回执。")
        return PartitionedOwnResultAuthorization(frozenset(targets), tuple(proofs))
