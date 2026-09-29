"""Exact fact-owner dispatch for a mixed import, independent of input schema version.

This selects existing owners, never grants authority. Every selected owner must still
authorize all current coordinates and fields; no fallback to another owner is allowed.
"""

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

from sqlalchemy import text

from app.composition.school_fact_family_authority import FamilyFactAuthorityDenied
from app.domains.students.education_contracts import EDUCATION_FACT_KINDS
from app.platform.authorization.secure_reader_contracts import SecureReadDenied
from app.platform.contracts import StaffActorContext
from app.platform.school_facts.application import SchoolFactChangeRejected
from app.platform.school_facts.contracts import F7_FAMILY_QUALITY_TARGET_KINDS
from app.platform.school_facts.f2_policy import F2_FACT_KINDS, F2FactScope
from app.platform.school_facts.f3_policy import F3_FACT_KINDS
from app.platform.school_facts.f6_policy import F6_FACT_KINDS


def _coordinates(configuration: Any) -> frozenset[tuple[str, str]]:
    if configuration is None:
        return frozenset()
    return frozenset(
        (kind, key)
        for catalog in (configuration.source_catalog, configuration.creation_catalog)
        for kind, keys in catalog.items()
        for key in keys
    )


def package_quality_read_proofs(*, education=None, residence=None, family=None):
    """Current read-purpose owner proofs; a maintenance scope cannot substitute."""
    result = {}
    if education is not None:
        result["education_quality"] = lambda actor, kind, key, at: education.read_for_source(
            actor, kind, key, at
        )["proof"]
    if residence is not None:
        result["residence_quality"] = lambda actor, _kind, key, _at: residence.require_read(
            actor, key
        )
    if family is not None:
        result["family_quality"] = family
    return result


class PackageExistingQualityKind:
    """Classify one existing quality by exact current maintenance authority.

    Routing needs only the target kind. F3's sensitive detail Reader remains
    limited to F3 and is called only after the owner has been selected.
    """

    def __init__(self, *, f2_guard: Any, sessions: Any) -> None:
        self.f2_guard = f2_guard
        self.sessions = sessions

    def target_kind(self, actor: StaffActorContext, quality_key: str) -> str:
        if type(actor) is not StaffActorContext or type(quality_key) is not str or not quality_key:
            raise PermissionError("Trusted quality actor and exact key are required.")
        permit = self.f2_guard.require_current(actor, "fact_quality", purpose="maintain")
        scope = F2FactScope.from_reference(permit.scope_plan.reference)
        if scope.school_id != actor.school_id or not scope.allows_key(quality_key):
            raise PermissionError("Quality is outside current exact maintenance authority.")
        with self.sessions() as session:
            kind = session.scalar(
                text(
                    "SELECT app.school_fact_f7_package_quality_kind("
                    ":school,:assignment,:scope,:key)"
                ),
                {
                    "school": actor.school_id,
                    "assignment": actor.assignment_id,
                    "scope": permit.scope_plan.scope_id,
                    "key": quality_key,
                },
            )
        self.f2_guard.revalidate(permit, actor)
        if type(kind) is not str or not kind:
            raise PermissionError("Original quality target classification unavailable.")
        return kind


class PackageFactPartition:
    def __init__(
        self,
        *,
        directory_configuration: Any,
        family_configuration: Any,
        education_configuration: Any,
        existing_quality_kind: Callable[[StaffActorContext, str], str],
        live_directory_guard: Any = None,
    ) -> None:
        self.family_coordinates = _coordinates(family_configuration)
        self.directory_coordinates = _coordinates(directory_configuration)
        self.education_coordinates = _coordinates(
            getattr(education_configuration, "dependency_directory", None)
        )
        self.existing_quality_kind = existing_quality_kind
        self.live_directory_guard = live_directory_guard

    def _live_shared_owner(self, actor, kind, key, creation):
        # People and staff assignments retain their directory owner. This
        # selects that owner for new coordinates only when its explicit live
        # school scope exists; the selected Writer must still authorize the
        # complete change, references and current identity at confirmation.
        if self.live_directory_guard is None:
            raise PermissionError("This shared object's owner is not explicitly registered.")
        permit = self.live_directory_guard.require_current(actor, kind, purpose="maintain")
        scope = F2FactScope.from_reference(permit.scope_plan.reference)
        if (scope.school_id != actor.school_id or scope.fact_kind != kind
                or not scope.is_school_wide or not scope.allows_key(key, is_creation=creation)):
            raise PermissionError("This shared object's owner is not explicitly registered.")
        return "directory"

    def _quality_owner(self, change: Any, kind: str, *, content: Any = None) -> str:
        if kind in F7_FAMILY_QUALITY_TARGET_KINDS:
            return "family_quality"
        if kind in F6_FACT_KINDS:
            return "residence_quality"
        if (
            kind in EDUCATION_FACT_KINDS
            or (change.fact_kind, change.source_key) in self.education_coordinates
            or getattr(content, "education_target_kind", None) in EDUCATION_FACT_KINDS
        ):
            return "education_quality"
        if kind in F2_FACT_KINDS - {"fact_quality"}:
            return "directory"
        raise PermissionError("The quality target owner could not be verified.")

    def group(self, actor: StaffActorContext, changes: tuple[Any, ...]) -> dict[str, tuple]:
        if type(actor) is not StaffActorContext:
            raise PermissionError("A current trusted staff actor is required.")
        groups: dict[str, list] = {}
        for change in changes:
            kind, key = change.fact_kind, change.source_key
            if kind in F6_FACT_KINDS:
                owner = "residence"
            elif kind in EDUCATION_FACT_KINDS:
                owner = "education"
            elif kind == "fact_quality":
                old_owner = (
                    self._quality_owner(change, self.existing_quality_kind(actor, key))
                    if change.expected_version > 0
                    else None
                )
                new_owner = (
                    self._quality_owner(
                        change,
                        getattr(change.content, "target_fact_kind", None),
                        content=change.content,
                    )
                    if change.content is not None
                    else None
                )
                if old_owner and new_owner and old_owner != new_owner:
                    raise PermissionError("A quality claim cannot change its fact owner.")
                owner = old_owner or new_owner
                if owner is None:
                    raise PermissionError("The quality target owner is missing.")
            elif kind == "policy_scope" and (
                (kind, key) in self.education_coordinates
                or getattr(change.content, "education_target_kind", None) in EDUCATION_FACT_KINDS
            ):
                owner = "education_quality"
            elif kind in F3_FACT_KINDS - F2_FACT_KINDS or (kind, key) in self.family_coordinates:
                owner = "family"
            elif kind in {"person", "assignment"} and (kind, key) not in self.directory_coordinates:
                owner = self._live_shared_owner(actor, kind, key, change.expected_version == 0)
            elif kind in F2_FACT_KINDS:
                owner = "directory"
            else:
                raise PermissionError("The fact owner is not registered.")
            groups.setdefault(owner, []).append(change)
        return {owner: tuple(values) for owner, values in groups.items()}


class PackageChangeAuthorization:
    """Product must call this before preflight and before releasing its result."""

    def __init__(
        self,
        partition: PackageFactPartition,
        guards: Mapping[str, Callable],
        *,
        references: Callable,
    ):
        self.partition, self.guards = partition, dict(guards)
        self.references = references

    def __call__(self, actor: StaffActorContext, change_set: Any) -> None:
        try:
            for owner, changes in self.partition.group(actor, change_set.changes).items():
                guard = self.guards.get(owner)
                if guard is None:
                    raise PermissionError("The required fact owner is unavailable.")
                guard(actor, change_set.model_copy(update={"changes": changes}))
            self.references(actor, change_set)
        except (FamilyFactAuthorityDenied, SecureReadDenied, SchoolFactChangeRejected):
            raise PermissionError("Current exact import authority was denied.") from None


class PackageWriterAuthorization:
    """Invoked only for a persisted, validated package confirmation batch."""

    def __init__(
        self,
        partition: PackageFactPartition,
        guards: Mapping[str, Callable],
        *,
        references: Callable,
    ):
        self.partition, self.guards = partition, dict(guards)
        self.references = references

    def __call__(self, session: Any, context: Any, change_set: Any) -> None:
        try:
            for owner, changes in self.partition.group(context.actor, change_set.changes).items():
                guard = self.guards.get(owner)
                if guard is None:
                    raise PermissionError("The required fact owner is unavailable.")
                guard(session, context, change_set.model_copy(update={"changes": changes}))
            self.references(context.actor, change_set)
        except (PermissionError, FamilyFactAuthorityDenied, SecureReadDenied):
            raise SchoolFactChangeRejected("当前身份没有本批导入资料的完整维护权限。") from None

    def require_replacement(self, context: Any, targets: tuple) -> None:
        try:
            self.references.require_selected(context.actor, targets)
        except (PermissionError, FamilyFactAuthorityDenied, SecureReadDenied):
            raise SchoolFactChangeRejected("完整替换选定范围的读取权限已经变化。") from None


class PackageReferenceAuthorization:
    """Authorize only the exact referenced coordinates before preflight looks them up.

    This reads no target body. The relation's submitted fields are checked by its
    change owner; reference existence requires a current read scope of its owner.
    Same-batch new objects have already passed their own exact creation guards.
    Missing dependency grants fail closed; no fallback or extra scope is invented.
    """

    def __init__(
        self,
        partition,
        *,
        directory_guard,
        family_authority,
        education_authority,
        residence_authority,
        quality_reads=None,
    ):
        self.partition = partition
        self.directory = directory_guard
        self.family = family_authority
        self.education = education_authority
        self.residence = residence_authority
        self.quality_reads = dict(quality_reads or {})

    def __call__(self, actor, change_set):
        from app.platform.school_facts.application import SchoolFactChangeService
        from app.platform.school_facts.contracts import fact_content_fields

        # A package confirms full old/new material, not a blind write. The same
        # exact read premise is required again by the formal Writer after its
        # current identity/governance check. Creation is independently proven
        # absent under the approved creation scope, never by a normal read.
        for change in change_set.changes:
            if change.expected_version:
                fields, _ = fact_content_fields(change.fact_kind, change.schema_version)
                self.require_coordinate(
                    actor,
                    change.fact_kind,
                    change.source_key,
                    tuple(sorted(fields)),
                    effective_at=change.effective_at,
                )

        creations = {
            (c.fact_kind, c.source_key)
            for c in change_set.changes
            if c.operation == "upsert" and c.expected_version == 0
        }
        references = {
            (kind, key)
            for change in change_set.changes
            for _field, kind, key in SchoolFactChangeService._reference_bindings(change)
        }
        for kind, key in sorted(references - creations):
            self.require_coordinate(actor, kind, key)

    def require_selected(self, actor, targets):
        for target in targets:
            self.require_coordinate(
                actor,
                target.fact_kind,
                target.source_key,
                target.field_names,
                effective_at=target.effective_at,
            )

    def require_coordinate(self, actor, kind, key, fields=(), *, effective_at=None):
        from app.platform.school_facts.f2_policy import F2FactScope

        coordinate = SimpleNamespace(
            fact_kind=kind,
            source_key=key,
            expected_version=1,
            content=None,
        )
        owner = next(iter(self.partition.group(actor, (coordinate,))))
        at = datetime.now(UTC)
        if owner in self.quality_reads:
            proof = self.quality_reads[owner](actor, kind, key, effective_at)
            if proof is None or not callable(getattr(proof, "revalidate", None)):
                raise PermissionError("Selected quality read proof is unavailable.")
            proof.revalidate()
        elif owner == "directory" and self.directory is not None:
            permit = self.directory.require_current(actor, kind, purpose="read")
            scope = F2FactScope.from_reference(permit.scope_plan.reference)
            if scope.school_id != actor.school_id or not scope.allows_key(key):
                raise PermissionError("The referenced object is outside current read scope.")
            self.directory.revalidate(permit, actor)
        elif owner == "family" and self.family is not None:
            permit = self.family.require_scope(actor, "read", kind, key, False, at)
            if fields and not set(fields) <= set(permit.scope.field_names):
                raise PermissionError("Selected fields are outside current read scope.")
            self.family.revalidate_permit(permit, actor, permit.target, datetime.now(UTC))
        elif owner in {"education", "residence"}:
            authority = self.education if owner == "education" else self.residence
            if authority is None:
                raise PermissionError("The referenced fact owner is unavailable.")
            permit = authority.require_scope(actor, fact_kind=kind, action="read", checked_at=at)
            authority.require_object(permit, key, is_creation=False)
            if fields:
                authority.require_fields(permit, fields)
            authority.revalidate(permit, actor=actor, checked_at=datetime.now(UTC))
        else:
            raise PermissionError("The referenced fact owner is unavailable.")
