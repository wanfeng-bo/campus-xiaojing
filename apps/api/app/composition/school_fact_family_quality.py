"""Intersect F2 quality and F3 exact fact authority before any family quality read.

This is a family-domain authorization bridge, not a new generic grant. The
underlying F2 and F3 authorities keep their own current-decision and final
revalidation semantics; no authority survives a model wait.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import func, select, text

from app.platform.authorization.secure_reader_compiler import CompiledReadRelation
from app.platform.contracts import StaffActorContext
from app.platform.school_facts.contracts import F7_FAMILY_QUALITY_TARGET_KINDS
from app.platform.school_facts.f2_policy import F2FactScope
from app.platform.school_facts.f3_policy import F3_FACT_FIELDS


@dataclass(frozen=True, slots=True)
class FamilyQualityChangeProof:
    actor: StaffActorContext
    proofs: tuple[FamilyQualityProof, ...]
    clock: Callable[[], datetime]

    def revalidate(self) -> None:
        at = self.clock()
        for proof in self.proofs:
            proof.revalidate(self.actor, at)


class FamilyQualityChangeGuard:
    """A family quality modification needs quality and target duties independently."""

    def __init__(
        self,
        authorizer: FamilyQualityAuthorizer,
        *,
        clock: Callable[[], datetime],
        existing_content: Callable[[StaffActorContext, str], object] | None = None,
    ) -> None:
        self.authorizer = authorizer
        self.clock = clock
        self.existing_content = existing_content

    def require_change_set(
        self, actor: StaffActorContext, change_set: object
    ) -> FamilyQualityChangeProof:
        changes = getattr(change_set, "changes", ())
        if type(actor) is not StaffActorContext or not changes:
            raise PermissionError("No trusted family quality change set.")
        proofs = []
        for change in changes:
            if change.fact_kind != "fact_quality":
                raise PermissionError("Mixed owner changes need their own domain guard.")
            contents = []
            if change.expected_version:
                if self.existing_content is None:
                    raise PermissionError("Original quality evidence cannot be verified.")
                original = self.existing_content(actor, change.source_key)
                if (
                    not isinstance(original, dict)
                    or original.get("version") != change.expected_version
                    or not isinstance(original.get("content"), dict)
                    or not callable(getattr(original.get("proof"), "revalidate", None))
                ):
                    raise PermissionError("Original quality evidence changed.")
                original["proof"].revalidate()
                contents.append(original["content"])
            if change.content is not None:
                contents.append(change.content.model_dump(exclude_none=True, exclude_unset=True))
            if not contents or change.expected_version == 0 and change.operation != "upsert":
                raise PermissionError("Family quality target is not formally identified.")
            for content in contents:
                kind = content.get("target_fact_kind")
                target_key = content.get("target_source_key")
                if (
                    kind not in F7_FAMILY_QUALITY_TARGET_KINDS
                    or content.get("owner_kind") != "person"
                    or not isinstance(target_key, str)
                    or not target_key
                ):
                    raise PermissionError("Family quality needs one exact person-owned F3 target.")
                fields = (
                    (content["field_name"],)
                    if content.get("field_name")
                    else tuple(sorted(F3_FACT_FIELDS[kind]))
                )
                keys = (target_key,)
                if content.get("replacement_source_key"):
                    keys += (content["replacement_source_key"],)
                for key in keys:
                    proofs.append(
                        self.authorizer.acquire(
                            actor=actor,
                            quality_key=change.source_key,
                            target_kind=kind,
                            target_key=key,
                            field_names=fields,
                            purpose="maintain",
                            checked_at=self.clock(),
                            quality_creation=change.expected_version == 0,
                        )
                    )
        result = FamilyQualityChangeProof(actor, tuple(proofs), self.clock)
        result.revalidate()
        return result


class FamilyQualityFinalGuard:
    def __init__(self, changes: FamilyQualityChangeGuard) -> None:
        self.changes = changes

    def __call__(self, session: Any, context: Any, change_set: object) -> None:
        actor = getattr(context, "actor", None)
        if type(actor) is not StaffActorContext:
            raise PermissionError("Trusted family quality writer identity is missing.")
        epoch = session.scalar(
            text("SELECT app.lock_school_fact_governance_epoch(:school_id,:assignment_id)"),
            {"school_id": actor.school_id, "assignment_id": actor.assignment_id},
        )
        if type(epoch) is not int or epoch < 1:
            raise PermissionError("Family quality governance epoch is unavailable.")
        proof = self.changes.require_change_set(actor, change_set)
        for item in proof.proofs:
            for permit in (item.quality_permit, item.target_permit):
                basis = getattr(permit, "basis", None)
                governance = getattr(basis, "governance", None)
                if getattr(governance, "assignment_epoch", None) != epoch:
                    raise PermissionError("Family quality grant changed before execution.")
        proof.revalidate()


class FamilyQualityWriterDispatch:
    def __init__(
        self,
        family_quality: Any,
        retained: Any,
        *,
        existing_target_kind: Callable[[StaffActorContext, str], str] | None = None,
    ) -> None:
        self.family_quality = family_quality
        self.retained = retained
        self.existing_target_kind = existing_target_kind

    def __call__(self, session: Any, context: Any, change_set: Any) -> None:
        selected, other = [], []
        for change in change_set.changes:
            if change.fact_kind != "fact_quality":
                other.append(change)
                continue
            old_kind = None
            if getattr(change, "expected_version", 0):
                actor = getattr(context, "actor", None)
                if type(actor) is not StaffActorContext or self.existing_target_kind is None:
                    raise PermissionError("Existing quality target classification unavailable.")
                old_kind = self.existing_target_kind(actor, change.source_key)
            new_kind = getattr(change.content, "target_fact_kind", None)
            if (
                old_kind in F7_FAMILY_QUALITY_TARGET_KINDS
                or new_kind in F7_FAMILY_QUALITY_TARGET_KINDS
            ):
                selected.append(change)
            else:
                other.append(change)
        if other:
            self.retained(session, context, change_set.model_copy(update={"changes": tuple(other)}))
        if selected:
            self.family_quality(
                session, context, change_set.model_copy(update={"changes": tuple(selected)})
            )


@dataclass(frozen=True, slots=True)
class FamilyQualityProof:
    actor: StaffActorContext
    quality_permit: Any
    target_permit: Any
    quality_key: str
    target_kind: str
    target_key: str
    field_names: tuple[str, ...]
    f2_guard: Any
    family_authority: Any

    def revalidate(self, actor: StaffActorContext, checked_at: datetime) -> None:
        if actor != self.actor or checked_at.utcoffset() is None:
            raise PermissionError("F3 quality identity or time changed.")
        self.f2_guard.revalidate(self.quality_permit, actor)
        self.family_authority.revalidate_permit(
            self.target_permit,
            actor,
            self.target_permit.target,
            checked_at,
        )


class FamilyQualityAuthorizer:
    def __init__(self, f2_guard: Any, family_authority: Any) -> None:
        self._f2_guard = f2_guard
        self._family_authority = family_authority

    def acquire(
        self,
        *,
        actor: StaffActorContext,
        quality_key: str,
        target_kind: str,
        target_key: str,
        field_names: tuple[str, ...],
        purpose: str,
        checked_at: datetime,
        quality_creation: bool = False,
    ) -> FamilyQualityProof:
        if (
            type(actor) is not StaffActorContext
            or checked_at.utcoffset() is None
            or purpose not in {"read", "maintain"}
            or target_kind not in F7_FAMILY_QUALITY_TARGET_KINDS
            or not quality_key
            or not target_key
            or not field_names
            or len(field_names) != len(set(field_names))
            or not set(field_names) <= F3_FACT_FIELDS[target_kind]
        ):
            raise ValueError("An exact F3 quality target and fields are required.")
        quality = self._f2_guard.require_current(actor, "fact_quality", purpose=purpose)
        scope = F2FactScope.from_reference(quality.scope_plan.reference)
        if (
            scope.school_id != actor.school_id
            or scope.fact_kind != "fact_quality"
            or not scope.allows_key(quality_key, is_creation=quality_creation)
        ):
            raise PermissionError("F3 quality is outside the exact quality grant.")
        action = (
            "verify"
            if purpose == "maintain" and target_kind == "guardian_relationship"
            else purpose
        )
        target = self._family_authority.require_scope(
            actor, action, target_kind, target_key, False, checked_at
        )
        target_scope = target.scope
        if (
            target_scope.school_id != actor.school_id
            or target_scope.fact_kind != target_kind
            or not target_scope.allows_key(target_key)
            or not set(field_names) <= set(target_scope.field_names)
        ):
            raise PermissionError("F3 target or field is outside current authority.")
        return FamilyQualityProof(
            actor,
            quality,
            target,
            quality_key,
            target_kind,
            target_key,
            field_names,
            self._f2_guard,
            self._family_authority,
        )

    def acquire_batch(
        self,
        *,
        actor: StaffActorContext,
        target_kind: str,
        target_key: str,
        field_names: tuple[str, ...],
        checked_at: datetime,
    ) -> FamilyQualityBatchProof:
        if (
            type(actor) is not StaffActorContext
            or checked_at.utcoffset() is None
            or target_kind not in F7_FAMILY_QUALITY_TARGET_KINDS
            or not target_key
            or not field_names
            or len(field_names) != len(set(field_names))
            or not set(field_names) <= F3_FACT_FIELDS[target_kind]
        ):
            raise ValueError("An exact F3 quality target and fields are required.")
        quality = self._f2_guard.require_current(actor, "fact_quality", purpose="read")
        scope = F2FactScope.from_reference(quality.scope_plan.reference)
        keys = tuple(scope.object_keys)
        if (
            scope.school_id != actor.school_id
            or scope.fact_kind != "fact_quality"
            or len(keys) > 100
            or len(set(keys)) != len(keys)
        ):
            raise PermissionError("Family quality scope is not exact and bounded.")
        target = self._family_authority.require_scope(
            actor, "read", target_kind, target_key, False, checked_at
        )
        target_scope = target.scope
        if (
            target_scope.school_id != actor.school_id
            or target_scope.fact_kind != target_kind
            or not target_scope.allows_key(target_key)
            or not set(field_names) <= set(target_scope.field_names)
        ):
            raise PermissionError("F3 target or field is outside current authority.")
        result = FamilyQualityBatchProof(
            actor,
            quality,
            target,
            keys,
            target_kind,
            target_key,
            field_names,
            self._f2_guard,
            self._family_authority,
            scope.is_school_wide,
        )
        result.revalidate(actor, checked_at)
        return result


@dataclass(frozen=True, slots=True)
class FamilyQualityBatchProof:
    actor: StaffActorContext
    quality_permit: Any
    target_permit: Any
    quality_keys: tuple[str, ...]
    target_kind: str
    target_key: str
    field_names: tuple[str, ...]
    f2_guard: Any
    family_authority: Any
    school_wide_quality: bool = False

    def revalidate(self, actor: StaffActorContext, checked_at: datetime) -> None:
        if actor != self.actor:
            raise PermissionError("Family quality identity changed.")
        self.f2_guard.revalidate(self.quality_permit, actor)
        self.family_authority.revalidate_permit(
            self.target_permit, actor, self.target_permit.target, checked_at
        )


@dataclass(frozen=True, slots=True)
class FamilyQualityExistingProof:
    actor: StaffActorContext
    target: FamilyQualityProof
    school_fact_version: int
    current_version: Callable[[object], int]
    clock: Callable[[], datetime]

    def revalidate(self) -> None:
        self.target.revalidate(self.actor, self.clock())
        if self.current_version(self.actor.school_id) != self.school_fact_version:
            raise PermissionError("Original family quality changed before confirmation.")


class SqlFamilyQualityProjection:
    """Fixed Reader projection for one F3 quality declaration and school version."""

    def __init__(self, sessions: Any) -> None:
        self.sessions = sessions

    def current_version(self, school_id: object) -> int:
        with self.sessions() as session:
            version = session.scalar(
                text("SELECT app.school_fact_current_version(:school)"), {"school": school_id}
            )
        if type(version) is not int or version < 0:
            raise ValueError("Invalid family quality school version.")
        return version

    def quality_detail(self, school_id: object, quality_key: str) -> dict[str, Any]:
        with self.sessions() as session:
            detail = session.scalar(
                text("SELECT app.school_fact_f7_family_quality_detail(:school,:key)"),
                {"school": school_id, "key": quality_key},
            )
        if not isinstance(detail, dict):
            raise PermissionError("Original family quality is unavailable.")
        return detail

    def quality_coordinates(self, school_id: object, quality_key: str) -> dict[str, Any]:
        detail = self.quality_detail(school_id, quality_key)
        content = detail.get("content")
        if not isinstance(content, dict):
            raise PermissionError("Original family quality coordinate is unavailable.")
        return {"version": detail.get("version"), "content": content}


class FamilyQualityExistingContent:
    """Read an existing quality coordinate only after its exact F2 grant.

    The retained fixed Reader projection is reused for maintenance details;
    the F3 target authority is acquired separately before its body is used.
    """

    def __init__(
        self,
        *,
        f2_guard: Any,
        authorizer: FamilyQualityAuthorizer,
        projection: Any,
        clock: Callable[[], datetime],
    ) -> None:
        self.f2_guard = f2_guard
        self.authorizer = authorizer
        self.projection = projection
        self.clock = clock

    def _coordinates(
        self, actor: StaffActorContext, quality_key: str, *, purpose: str = "maintain"
    ) -> dict[str, Any]:
        if type(actor) is not StaffActorContext or not quality_key:
            raise PermissionError("Trusted family quality identity or target missing.")
        permit = self.f2_guard.require_current(actor, "fact_quality", purpose=purpose)
        scope = F2FactScope.from_reference(permit.scope_plan.reference)
        if (
            scope.school_id != actor.school_id
            or scope.fact_kind != "fact_quality"
            or not scope.allows_key(quality_key)
        ):
            raise PermissionError("Original quality is outside the exact maintenance grant.")
        value = self.projection.quality_coordinates(actor.school_id, quality_key)
        self.f2_guard.revalidate(permit, actor)
        if (
            not isinstance(value, dict)
            or type(value.get("version")) is not int
            or not isinstance(value.get("content"), dict)
        ):
            raise PermissionError("Original quality coordinate is unavailable.")
        return value

    def target_kind(self, actor: StaffActorContext, quality_key: str) -> str:
        kind = self._coordinates(actor, quality_key)["content"].get("target_fact_kind")
        if not isinstance(kind, str):
            raise PermissionError("Original quality target is unavailable.")
        return kind

    def __call__(self, actor: StaffActorContext, quality_key: str) -> dict[str, object]:
        version = self.projection.current_version(actor.school_id)
        coordinates = self._coordinates(actor, quality_key)
        original = coordinates["content"]
        kind, key = original.get("target_fact_kind"), original.get("target_source_key")
        if kind not in F7_FAMILY_QUALITY_TARGET_KINDS or not isinstance(key, str) or not key:
            raise PermissionError("Original quality belongs to a different fact owner.")
        field = original.get("field_name")
        fields = (field,) if isinstance(field, str) else tuple(sorted(F3_FACT_FIELDS[kind]))
        target = self.authorizer.acquire(
            actor=actor,
            quality_key=quality_key,
            target_kind=kind,
            target_key=key,
            field_names=fields,
            purpose="maintain",
            checked_at=self.clock(),
        )
        detail = self.projection.quality_detail(actor.school_id, quality_key)
        if (
            not isinstance(detail, dict)
            or detail.get("version") != coordinates["version"]
            or not isinstance(detail.get("content"), dict)
            or detail["content"].get("target_fact_kind") != kind
            or detail["content"].get("target_source_key") != key
        ):
            raise PermissionError("Original quality detail changed during read.")
        proof = FamilyQualityExistingProof(
            actor, target, version, self.projection.current_version, self.clock
        )
        proof.revalidate()
        return {"version": coordinates["version"], "content": detail["content"], "proof": proof}


@dataclass(frozen=True, slots=True)
class FamilyQualityCurrentProof:
    actor: StaffActorContext
    target: FamilyQualityProof
    reader_result: Any
    school_fact_version: int
    valid_to: datetime | None
    current_version: Callable[[object], int]
    clock: Callable[[], datetime]

    def revalidate(self) -> None:
        checked_at = self.clock()
        if self.valid_to is not None and checked_at >= self.valid_to:
            raise PermissionError("Current family quality period ended before use.")
        self.target.revalidate(self.actor, checked_at)
        self.reader_result.revalidate(self.actor)
        if self.current_version(self.actor.school_id) != self.school_fact_version:
            raise PermissionError("Current family quality changed before use.")


class FamilyQualityCurrentContent(FamilyQualityExistingContent):
    """Read one currently effective F3 quality body under independent read grants.

    Ended or future-only rows have no current authorized Reader result and fail
    closed. This read-purpose snapshot cannot stand in for maintenance authority.
    """

    def __init__(self, *, reader: Any, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.reader = reader

    def __call__(self, actor: StaffActorContext, quality_key: str) -> dict[str, object]:
        coordinates = self._coordinates(actor, quality_key, purpose="read")
        original = coordinates["content"]
        kind, key = original.get("target_fact_kind"), original.get("target_source_key")
        if kind not in F7_FAMILY_QUALITY_TARGET_KINDS or not isinstance(key, str) or not key:
            raise PermissionError("Current family quality belongs to a different fact owner.")
        field = original.get("field_name")
        fields = (field,) if isinstance(field, str) else tuple(sorted(F3_FACT_FIELDS[kind]))
        checked_at = self.clock()
        target = self.authorizer.acquire(
            actor=actor,
            quality_key=quality_key,
            target_kind=kind,
            target_key=key,
            field_names=fields,
            purpose="read",
            checked_at=checked_at,
        )
        current = self.reader.read(
            actor=actor,
            quality_key=quality_key,
            target_kind=kind,
            target_key=key,
            field_names=fields,
            as_of=checked_at,
        )
        if len(current.records) != 1:
            raise PermissionError("Current family quality is absent or outside the read grant.")
        row = current.records[0]
        if (
            row.get("source_key") != quality_key
            or row.get("version") != coordinates["version"]
            or not isinstance(row.get("valid_from"), str)
        ):
            raise PermissionError("Current family quality changed during read.")
        try:
            valid_from = datetime.fromisoformat(row["valid_from"])
            valid_to = (
                datetime.fromisoformat(row["valid_to"])
                if isinstance(row.get("valid_to"), str)
                else None
            )
        except ValueError as exc:
            raise PermissionError("Current family quality period is invalid.") from exc
        if (
            valid_from.utcoffset() is None
            or valid_from > checked_at
            or row.get("valid_to") is not None
            and valid_to is None
            or valid_to is not None
            and (valid_to.utcoffset() is None or checked_at >= valid_to)
        ):
            raise PermissionError("Current family quality period is unavailable.")
        detail = self.projection.quality_detail(actor.school_id, quality_key)
        if (
            not isinstance(detail, dict)
            or detail.get("version") != coordinates["version"]
            or not isinstance(detail.get("content"), dict)
            or detail["content"].get("target_fact_kind") != kind
            or detail["content"].get("target_source_key") != key
            or detail["content"].get("field_name") != original.get("field_name")
            or detail["content"].get("person_source_key") != original.get("person_source_key")
        ):
            raise PermissionError("Current family quality detail changed during read.")
        proof = FamilyQualityCurrentProof(
            actor,
            target,
            current,
            current.school_fact_version,
            valid_to,
            self.projection.current_version,
            self.clock,
        )
        proof.revalidate()
        return {
            "version": coordinates["version"],
            "content": detail["content"],
            "valid_from": row["valid_from"],
            "valid_to": row.get("valid_to"),
            "school_fact_version": current.school_fact_version,
            "proof": proof,
        }


_PUBLIC_QUALITY_FIELDS = frozenset(
    {
        "source_key",
        "owner_source_key",
        "quality_kind",
        "field_name",
        "coverage_status",
        "coverage_basis",
        "dispute_status",
        "source_status",
        "review_due_at",
        "verified_at",
        "valid_from",
        "valid_to",
        "version",
    }
)
_LEGACY_PUBLIC_QUALITY_FIELDS = _PUBLIC_QUALITY_FIELDS - {
    "source_key",
    "owner_source_key",
    "coverage_basis",
}


@dataclass(frozen=True, slots=True)
class FamilyQualityResult:
    records: tuple[dict[str, object], ...]
    school_fact_version: int
    proof: FamilyQualityProof
    current_version: Callable[[object], int]
    clock: Callable[[], datetime]

    def revalidate(self, actor: StaffActorContext) -> None:
        self.proof.revalidate(actor, self.clock())
        if self.current_version(actor.school_id) != self.school_fact_version:
            raise PermissionError("Family quality evidence changed before release.")


@dataclass(frozen=True, slots=True)
class FamilyQualityBatchResult:
    records: tuple[dict[str, object], ...]
    school_fact_version: int
    proof: FamilyQualityBatchProof
    current_version: Callable[[object], int]
    clock: Callable[[], datetime]

    def revalidate(self, actor: StaffActorContext) -> None:
        self.proof.revalidate(actor, self.clock())
        if self.current_version(actor.school_id) != self.school_fact_version:
            raise PermissionError("Family quality evidence changed before release.")


class FamilyQualityReader:
    """One exact F3 target and one exact quality key through the Reader role."""

    def __init__(
        self,
        *,
        authorizer: FamilyQualityAuthorizer,
        executor: Any,
        current_version: Callable[[object], int],
        clock: Callable[[], datetime],
    ) -> None:
        self.authorizer = authorizer
        self.executor = executor
        self.current_version = current_version
        self.clock = clock

    def query_quality_for_target(
        self,
        *,
        actor: StaffActorContext,
        target_kind: str,
        target_key: str,
        field_names: tuple[str, ...],
        as_of: datetime,
    ) -> FamilyQualityBatchResult:
        if as_of.utcoffset() is None:
            raise ValueError("Factual time must be explicit and aware.")
        proofs: list[FamilyQualityBatchProof] = []
        payloads: list[Mapping[str, object]] = []

        def before() -> None:
            proofs.append(
                self.authorizer.acquire_batch(
                    actor=actor,
                    target_kind=target_kind,
                    target_key=target_key,
                    field_names=field_names,
                    checked_at=self.clock(),
                )
            )

        def after() -> None:
            if len(proofs) != 1 or len(payloads) != 1:
                raise PermissionError("Family quality batch read proof is incomplete.")
            proofs[0].revalidate(actor, self.clock())
            if self.current_version(actor.school_id) != payloads[0].get("school_fact_version"):
                raise PermissionError("Family quality changed during the read.")

        with self.executor.validated_read(before, after) as transaction:

            def fetch_payload(statement):
                rows = transaction.fetch(
                    CompiledReadRelation(
                        "school_facts.f7.family.quality.target",
                        as_of,
                        1,
                        ("payload",),
                        select(1),
                        statement,
                    )
                )
                if (
                    len(rows) != 1
                    or set(rows[0]) != {"payload"}
                    or not isinstance(rows[0]["payload"], dict)
                ):
                    raise ValueError("Invalid family quality projection.")
                return rows[0]["payload"]

            proof = proofs[0]
            version = self.current_version(actor.school_id)
            records = []
            after_key = None
            while True:
                proof.revalidate(actor, self.clock())
                keys = proof.quality_keys
                more = False
                if proof.school_wide_quality:
                    page = fetch_payload(
                        select(
                            func.app.school_fact_scope_keys(
                                actor.school_id,
                                "f2",
                                "fact_quality",
                                after_key,
                                100,
                            ).label("payload")
                        )
                    )
                    page_keys = page.get("keys")
                    more = page.get("has_more")
                    next_after = page.get("next_after")
                    if (
                        page.get("school_fact_version") != version
                        or not isinstance(page_keys, list)
                        or len(page_keys) > 100
                        or any(not isinstance(key, str) or not key for key in page_keys)
                        or len(set(page_keys)) != len(page_keys)
                        or page_keys != sorted(page_keys)
                        or (after_key is not None and any(key <= after_key for key in page_keys))
                        or type(more) is not bool
                        or (more and (not page_keys or next_after != page_keys[-1]))
                        or (not more and next_after is not None)
                    ):
                        raise PermissionError("Invalid family quality scope page.")
                    keys = tuple(page_keys)
                    after_key = next_after
                if keys:
                    page = fetch_payload(
                        select(
                            func.app.school_fact_f7_family_quality(
                                actor.school_id,
                                list(keys),
                                target_kind,
                                target_key,
                                list(field_names),
                                as_of,
                            ).label("payload")
                        )
                    )
                    if page.get("school_fact_version") != version or not isinstance(
                        page.get("records"), list
                    ):
                        raise PermissionError("Family quality version changed during the read.")
                    records.extend(page["records"])
                    if len(records) > 100:
                        raise PermissionError(
                            "Family quality result exceeds its bounded projection."
                        )
                if not more:
                    break
            raw = {"records": records, "school_fact_version": version}
            payloads.append(raw)
        raw = payloads[0]
        records = raw.get("records")
        if (
            type(raw.get("school_fact_version")) is not int
            or not isinstance(records, list)
            or len(records) > 100
        ):
            raise ValueError("Invalid family quality count or version.")
        for row in records:
            if (
                not isinstance(row, dict)
                or set(row) not in {_PUBLIC_QUALITY_FIELDS, _LEGACY_PUBLIC_QUALITY_FIELDS}
                or row.get("field_name") not in (None, *field_names)
                or row.get("quality_kind")
                not in {"source", "coverage", "dispute", "review", "correction"}
                or type(row.get("version")) is not int
            ):
                raise PermissionError("Family quality exceeded exact target projection.")
        result = FamilyQualityBatchResult(
            tuple(dict(row) for row in records),
            raw["school_fact_version"],
            proofs[0],
            self.current_version,
            self.clock,
        )
        result.revalidate(actor)
        return result

    def read(
        self,
        *,
        actor: StaffActorContext,
        quality_key: str,
        target_kind: str,
        target_key: str,
        field_names: tuple[str, ...],
        as_of: datetime,
    ) -> FamilyQualityResult:
        if as_of.utcoffset() is None:
            raise ValueError("Factual time must be explicit and aware.")
        proofs: list[FamilyQualityProof] = []
        payloads: list[Mapping[str, object]] = []

        def before() -> None:
            proofs.append(
                self.authorizer.acquire(
                    actor=actor,
                    quality_key=quality_key,
                    target_kind=target_kind,
                    target_key=target_key,
                    field_names=field_names,
                    purpose="read",
                    checked_at=self.clock(),
                )
            )

        def after() -> None:
            if len(proofs) != 1 or len(payloads) != 1:
                raise PermissionError("Family quality read proof is incomplete.")
            proofs[0].revalidate(actor, self.clock())
            if self.current_version(actor.school_id) != payloads[0].get("school_fact_version"):
                raise PermissionError("Family quality changed during the read.")

        with self.executor.validated_read(before, after) as transaction:
            statement = select(
                func.app.school_fact_f7_family_quality(
                    actor.school_id,
                    [quality_key],
                    target_kind,
                    target_key,
                    list(field_names),
                    as_of,
                ).label("payload")
            )
            rows = transaction.fetch(
                CompiledReadRelation(
                    "school_facts.f7.family.quality",
                    as_of,
                    1,
                    ("payload",),
                    select(1),
                    statement,
                )
            )
            if len(rows) != 1 or set(rows[0]) != {"payload"}:
                raise ValueError("Invalid family quality projection.")
            raw = rows[0]["payload"]
            if not isinstance(raw, dict):
                raise ValueError("Invalid family quality projection.")
            payloads.append(raw)
        raw = payloads[0]
        records = raw.get("records")
        if (
            type(raw.get("school_fact_version")) is not int
            or not isinstance(records, list)
            or len(records) > 100
        ):
            raise ValueError("Invalid family quality count or version.")
        for row in records:
            if (
                not isinstance(row, dict)
                or set(row) not in {_PUBLIC_QUALITY_FIELDS, _LEGACY_PUBLIC_QUALITY_FIELDS}
                or row.get("field_name") not in (None, *field_names)
                or row.get("quality_kind")
                not in {"source", "coverage", "dispute", "review", "correction"}
                or type(row.get("version")) is not int
            ):
                raise PermissionError("Family quality exceeded exact target projection.")
        result = FamilyQualityResult(
            tuple(dict(row) for row in records),
            raw["school_fact_version"],
            proofs[0],
            self.current_version,
            self.clock,
        )
        result.revalidate(actor)
        return result
