"""F3 final boundary: exact current authority under the existing Writer epoch lock."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.composition.school_fact_family_authority import (
    FamilyFactAuthorityDenied,
    StaffFamilyFactChangeSetGuard,
)
from app.composition.school_fact_family_settings import FamilyConfiguration
from app.platform.contracts import CommandExecutionContext, StaffActorContext
from app.platform.school_facts.application import SchoolFactChangeRejected
from app.platform.school_facts.contracts import CanonicalFactChangeSetV1
from app.platform.school_facts.ports import SchoolFactOwnResultScope

FinalWriter = Callable[[Session, CommandExecutionContext, CanonicalFactChangeSetV1], None]


class OwnProof(Protocol):
    @property
    def allowed_targets(self) -> SchoolFactOwnResultScope: ...
    def revalidate(self) -> None: ...


OwnAuthorizer = Callable[[CommandExecutionContext], OwnProof]


class FamilyFinalAuthorizationGuard:
    def __init__(self, guard: StaffFamilyFactChangeSetGuard, *, enabled: bool = True) -> None:
        self.guard = guard
        self.enabled = enabled

    def __call__(
        self,
        session: Session,
        context: CommandExecutionContext,
        change_set: CanonicalFactChangeSetV1,
    ) -> None:
        if not self.enabled:
            raise SchoolFactChangeRejected("新家校事实维护已暂停，正式记录和回执保持不变。")
        if type(context.actor) is not StaffActorContext:
            raise SchoolFactChangeRejected("当前正式执行身份不可验证。")
        actor = context.actor
        epoch = session.scalar(
            text("SELECT app.lock_school_fact_governance_epoch(:school_id,:assignment_id)"),
            {"school_id": actor.school_id, "assignment_id": actor.assignment_id},
        )
        if type(epoch) is not int or epoch < 1:
            raise SchoolFactChangeRejected("当前维护授权版本不可验证。")
        try:
            permit = self.guard.require_change_set(actor, change_set, checked_at=datetime.now(UTC))
            if any(item.basis.governance.assignment_epoch != epoch for item in permit.item_permits):
                raise SchoolFactChangeRejected("维护权限已经变化，请重新核对。")
            self.guard.final_guard(permit, actor, change_set, checked_at=datetime.now(UTC))
        except FamilyFactAuthorityDenied:
            raise SchoolFactChangeRejected("当前身份没有这项正式修改权限。") from None


class FamilyWriterDispatch:
    def __init__(
        self, *, family: FinalWriter, directory: FinalWriter, configuration: FamilyConfiguration
    ) -> None:
        from app.platform.school_facts.f2_policy import F2_FACT_KINDS
        from app.platform.school_facts.f3_policy import F3_FACT_KINDS

        self.family, self.directory = family, directory
        self.exclusive = F3_FACT_KINDS - F2_FACT_KINDS
        self.coordinates: frozenset[tuple[str, str]] = frozenset(
            (kind, key)
            for catalog in (configuration.source_catalog, configuration.creation_catalog)
            for kind, keys in catalog.items()
            for key in keys
        )

    def is_family(self, kind: str, source_key: str) -> bool:
        return kind in self.exclusive or (kind, source_key) in self.coordinates

    def __call__(
        self,
        session: Session,
        context: CommandExecutionContext,
        change_set: CanonicalFactChangeSetV1,
    ) -> None:
        family = tuple(c for c in change_set.changes if self.is_family(c.fact_kind, c.source_key))
        directory = tuple(
            c for c in change_set.changes if not self.is_family(c.fact_kind, c.source_key)
        )
        if directory:
            self.directory(session, context, change_set.model_copy(update={"changes": directory}))
        if family:
            self.family(session, context, change_set.model_copy(update={"changes": family}))


@dataclass(frozen=True, slots=True)
class PartitionedOwnResultAuthorization:
    allowed_targets: SchoolFactOwnResultScope
    authorizations: tuple[OwnProof, ...]

    @property
    def creation_only_targets(self) -> SchoolFactOwnResultScope:
        creations: set[tuple[str, str]] = set()
        readable: set[tuple[str, str]] = set()
        for proof in self.authorizations:
            # Child proofs can themselves be partitions. Evaluate their metadata
            # once in this traversal, not again for every authorized target.
            # This is local set algebra, never a cached authorization decision.
            child_creations = frozenset(getattr(proof, "creation_only_targets", ()))
            creations.update(child_creations)
            readable.update(proof.allowed_targets - child_creations)
        return frozenset(creations - readable) & self.allowed_targets

    def revalidate(self) -> None:
        for proof in self.authorizations:
            proof.revalidate()


class PartitionedOwnResultAuthorizer:
    def __init__(
        self, *, dispatch: FamilyWriterDispatch, directory: OwnAuthorizer, family: OwnAuthorizer
    ) -> None:
        self.dispatch, self.directory, self.family = dispatch, directory, family

    def for_targets(
        self, context: CommandExecutionContext, targets: SchoolFactOwnResultScope
    ) -> PartitionedOwnResultAuthorization:
        allowed = set()
        proofs = []
        for authorizer, is_family in ((self.directory, False), (self.family, True)):
            selected = frozenset(
                (kind, key)
                for kind, key in targets
                if self.dispatch.is_family(kind, key) is is_family
            )
            if not selected:
                continue
            try:
                authorize = getattr(authorizer, "for_targets", None)
                if not callable(authorize):
                    raise SchoolFactChangeRejected("Exact own receipt authority is unavailable.")
                proof = authorize(context, selected)
            except (SchoolFactChangeRejected, FamilyFactAuthorityDenied):
                continue
            allowed.update(proof.allowed_targets & selected)
            proofs.append(proof)
        if not allowed:
            raise SchoolFactChangeRejected("当前没有获准查看的本人操作回执。")
        return PartitionedOwnResultAuthorization(frozenset(allowed), tuple(proofs))

    def __call__(self, context: CommandExecutionContext) -> PartitionedOwnResultAuthorization:
        allowed: set[tuple[str, str]] = set()
        proofs: list[OwnProof] = []
        for authorizer, is_family in ((self.directory, False), (self.family, True)):
            try:
                proof = authorizer(context)
            except (SchoolFactChangeRejected, FamilyFactAuthorityDenied):
                continue
            targets = {
                (kind, key)
                for kind, key in proof.allowed_targets
                if self.dispatch.is_family(kind, key) is is_family
            }
            if targets:
                allowed.update(targets)
                proofs.append(proof)
        if not allowed:
            raise SchoolFactChangeRejected("当前没有获准查看的本人操作回执。")
        return PartitionedOwnResultAuthorization(frozenset(allowed), tuple(proofs))
