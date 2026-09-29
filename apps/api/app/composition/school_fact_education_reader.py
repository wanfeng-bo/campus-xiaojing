"""Exact education reads with current authority and fresh publication-version release checks."""

from __future__ import annotations

import hmac
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any, cast
from uuid import UUID, uuid4

from sqlalchemy import DateTime, Text, func, literal, select
from sqlalchemy.dialects.postgresql import JSONB, array
from sqlalchemy.exc import DBAPIError

from app.composition.school_fact_education_authority import EducationAuthority, EducationPermit
from app.platform.authorization.secure_reader_compiler import CompiledReadRelation
from app.platform.contracts import StaffActorContext
from app.platform.school_facts.f4_policy import F4_FACT_FIELDS, F4_FACT_KINDS
from app.platform.school_facts.f5_policy import F5_FACT_FIELDS, F5_FACT_KINDS
from app.platform.school_facts.school_scope_reference import (
    decode_school_scope_reference,
    encode_school_scope_reference,
    is_school_scope_reference,
)

FILTER_FIELDS = frozenset(
    {
        "class_source_key",
        "student_source_key",
        "campus_source_key",
        "stage_source_key",
        "calendar_source_key",
        "cohort_source_key",
        "position_source_key",
        "assignment_source_key",
        "display_name",
        "predecessor_class_source_key",
        "school_relationship_source_key",
        "organization_source_key",
        "location_source_key",
        "grade_source_key",
    }
)


TEACHING_FILTER_FIELDS = FILTER_FIELDS | frozenset(
    {
        "course_source_key",
        "subject_source_key",
        "offering_source_key",
        "group_source_key",
        "term_source_key",
        "grade_group_source_key",
        "source_kind",
        "offering_status",
        "teaching_role",
        "group_mode",
        "scope_basis",
    }
)


class EducationReadStale(RuntimeError):
    pass


class EducationCancellationRequiresCorrection(RuntimeError):
    code = "school_facts.teaching_cancellation_requires_correction"
    message = (
        "旧安排后来存在独立修改，或撤回依据不足。请明确受影响时段的完整调整，再重新预览。"
        "本次未形成新的可确认预览，也未发起新的修改；已提交的操作仍请查原回执。"
    )

    def __init__(self) -> None:
        super().__init__(self.message)


_CANCELLATION_CORRECTION_REASONS = frozenset(
    {
        "prior arrangement independently changed or lacks interval proof; "
        "complete correction required",
        "selected arrangement independently changed; review required",
        "invalid teaching cancellation cause",
        "selected slice has no original arrangement proof",
    }
)


def aware(value: datetime) -> datetime:
    if value.utcoffset() is None:
        raise ValueError("Education fact time requires a timezone")
    return value


def timestamp(value: object) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return aware(value)
    if not isinstance(value, str):
        raise ValueError("Invalid education timestamp")
    return aware(datetime.fromisoformat(value))


@dataclass(frozen=True)
class EducationReadSelection:
    """One fixed education projection in a synchronous, at most four-part read."""

    kind: str
    fields: tuple[str, ...]
    source_keys: tuple[str, ...] | None = None
    filter: dict[str, str | list[str] | int] | None = None


def _read_filter(kind: str, value: dict[str, str | list[str] | int] | None):
    if kind not in F4_FACT_KINDS | F5_FACT_KINDS:
        raise ValueError("Unknown education fact kind")
    selected = {
        key: list(item) if isinstance(item, list) else item for key, item in (value or {}).items()
    }
    allowed = TEACHING_FILTER_FIELDS if kind in F5_FACT_KINDS else FILTER_FIELDS
    if kind == "education_grade_group":
        allowed = allowed | {"academic_year"}
    if kind in F5_FACT_KINDS and any(
        type(item) is not str
        or not item
        or len(item) > 255
        or item != item.strip()
        or "*" in item
        or "?" in item
        for item in selected.values()
    ):
        raise ValueError("Teaching filters require finite exact scalar values")
    if set(selected) - allowed:
        raise ValueError("Only exact education relationship filters are supported")
    if "academic_year" in selected and (
        kind != "education_grade_group"
        or type(selected["academic_year"]) is not int
        or not 1900 <= selected["academic_year"] <= 9998
    ):
        raise ValueError("Only an exact bounded grade-group academic year is supported")
    return selected


@dataclass(frozen=True)
class EducationReadProof:
    repository: EducationRepository
    actor: StaffActorContext
    permits: tuple[EducationPermit, ...]
    school_fact_version: int
    next_boundary: datetime | None

    def revalidate(self, *, actor: StaffActorContext | None = None) -> None:
        current_actor = self.actor if actor is None else actor
        if current_actor != self.actor:
            raise PermissionError("当前身份已变化。")
        from app.composition.school_fact_read_evidence import (
            register_release_time_window,
            validation_batch,
        )

        with validation_batch() as release:
            observed_at = self.repository.clock()
            batch = getattr(self.repository.authority, "revalidate_many", None)
            if batch is not None:
                batch(self.permits, actor=current_actor, checked_at=self.repository.clock())
            else:
                for permit in self.permits:
                    self.repository.authority.revalidate(
                        permit, actor=current_actor, checked_at=self.repository.clock()
                    )

            def content_current() -> None:
                if (
                    self.repository.projection.current_version(current_actor.school_id)
                    != self.school_fact_version
                ):
                    raise EducationReadStale("学校资料已更新，请重新查询。")
                if self.next_boundary is not None and self.repository.clock() >= self.next_boundary:
                    raise EducationReadStale("资料适用时间已变化，请重新查询。")

            release.check_content(
                (id(self.repository), current_actor, self.school_fact_version, self.next_boundary),
                content_current,
            )
            register_release_time_window(
                self.repository.clock,
                observed_at=observed_at,
                valid_before=self.next_boundary,
            )

    @classmethod
    def combine(
        cls, proofs: tuple[EducationReadProof, ...], *, next_boundary: datetime | None = None
    ) -> EducationReadProof:
        if not proofs:
            raise ValueError("A query needs an actual authorized read proof")
        first = proofs[0]
        if any(
            (p.actor, p.repository, p.school_fact_version)
            != (first.actor, first.repository, first.school_fact_version)
            for p in proofs
        ):
            raise EducationReadStale("多项学校资料未形成一致版本，请重新查询。")
        permits: dict[tuple[str, str, str], EducationPermit] = {}
        for proof in proofs:
            for permit in proof.permits:
                permits.setdefault(
                    (permit.scope.fact_kind, permit.action, permit.scope.reference), permit
                )
        boundaries = [p.next_boundary for p in proofs if p.next_boundary is not None]
        if next_boundary is not None:
            boundaries.append(next_boundary)
        return cls(
            first.repository,
            first.actor,
            tuple(permits.values()),
            first.school_fact_version,
            min(boundaries, default=None),
        )


@dataclass(frozen=True)
class EducationReadResult:
    records: tuple[dict[str, Any], ...]
    school_fact_version: int
    as_of: datetime
    proof: EducationReadProof
    status: str
    # Private coordinates derived only from rows that passed this read. They do
    # not grant future access; callers retain and revalidate the read proof.
    references: tuple[tuple[str, str], ...] = ()

    def public_document(self) -> dict[str, Any]:
        """Authorized maintenance DTO; models require a separate public projection."""
        return {
            "status": self.status,
            "school_fact_version": self.school_fact_version,
            "as_of": self.as_of.isoformat(),
            "records": list(self.records),
        }


class SqlEducationProjection:
    def __init__(self, executor: Any, *, current_sessions: Callable[[], Any]) -> None:
        self.executor, self.current_sessions = executor, current_sessions

    def current_version(self, school_id: UUID) -> int:
        with self.current_sessions()() as session:
            version = session.scalar(select(func.app.school_fact_current_version(school_id)))
        if type(version) is not int or version < 0:
            raise ValueError("Invalid authoritative school fact version")
        return version

    def scope_keys(self, *, school_id: UUID, fact_kind: str, after_key: str | None, limit: int):
        if fact_kind not in F4_FACT_KINDS | F5_FACT_KINDS or not 1 <= limit <= 100:
            raise ValueError("Invalid bounded education key page")
        family = "f5" if fact_kind in F5_FACT_KINDS else "f4"
        with self.current_sessions()() as session:
            return session.scalar(
                select(
                    func.app.school_fact_scope_keys(school_id, family, fact_kind, after_key, limit)
                )
            )

    def read_many(self, *, school_id, selections, as_of, before_read, after_read):
        """One fixed-function SQL statement; no facts before the complete precheck."""
        raw = None

        def after():
            if raw is None:
                raise ValueError("Education wave did not return a result")
            after_read(raw)

        with self.executor.validated_read(before_read, after) as transaction:
            chosen = selections()
            if not 1 <= len(chosen) <= 4:
                raise ValueError("Invalid bounded education read wave")
            statements = []
            for index, (kind, keys, fields, filters) in enumerate(chosen):
                if kind not in F4_FACT_KINDS | F5_FACT_KINDS:
                    raise ValueError("Unknown education fact kind")
                phase = "f5" if kind in F5_FACT_KINDS else "f4"
                payload = getattr(func.app, f"school_fact_{phase}_targets")(
                    school_id,
                    kind,
                    array(list(keys), type_=Text),
                    array(list(fields), type_=Text),
                    literal(as_of, DateTime(timezone=True)),
                    literal(None, DateTime(timezone=True)),
                    literal(filters, JSONB),
                )
                statements.append(select(literal(index).label("ordinal"), payload.label("payload")))
            statement = (
                statements[0].union_all(*statements[1:]) if len(statements) > 1 else statements[0]
            )
            rows = transaction.fetch(
                CompiledReadRelation(
                    "school_facts.education.fixed_wave",
                    as_of,
                    len(chosen),
                    ("ordinal", "payload"),
                    select(1),
                    statement,
                )
            )
            if (
                len(rows) != len(chosen)
                or any(
                    type(row.get("ordinal")) is not int or type(row.get("payload")) is not dict
                    for row in rows
                )
                or {row["ordinal"] for row in rows} != set(range(len(chosen)))
            ):
                raise ValueError("Malformed education read wave")
            raw = tuple(row["payload"] for row in sorted(rows, key=lambda row: row["ordinal"]))
        return raw

    def read(
        self,
        *,
        school_id: UUID,
        fact_kind: str,
        as_of: datetime,
        known_at: datetime | None,
        filter: dict[str, str | list[str] | int],
        metadata_only: bool,
        source_keys: Callable[[], tuple[str, ...]],
        field_names: Callable[[], tuple[str, ...]],
        before_read: Callable[[], None],
        after_read: Callable[[dict[str, Any]], None],
        cancellation_start: datetime | None = None,
        expected_version: int | None = None,
    ) -> dict[str, Any]:
        if fact_kind not in F4_FACT_KINDS | F5_FACT_KINDS:
            raise ValueError("Unknown education projection kind")
        phase = "f5" if fact_kind in F5_FACT_KINDS else "f4"
        raw: dict[str, Any] | None = None

        def after() -> None:
            if raw is None:
                raise ValueError("Education projection did not return a result")
            after_read(raw)

        with self.executor.validated_read(before_read, after) as transaction:
            keys = array(list(source_keys()), type_=Text)
            if cancellation_start is not None:
                payload = func.app.school_fact_f5_cancellation_proof(
                    school_id,
                    fact_kind,
                    source_keys()[0],
                    array(list(field_names()), type_=Text),
                    literal(cancellation_start, DateTime(timezone=True)),
                    expected_version,
                )
            elif metadata_only:
                payload = getattr(func.app, f"school_fact_{phase}_coordinates")(
                    school_id, fact_kind, keys
                )
            else:
                payload = getattr(func.app, f"school_fact_{phase}_targets")(
                    school_id,
                    fact_kind,
                    keys,
                    array(list(field_names()), type_=Text),
                    literal(as_of, DateTime(timezone=True)),
                    literal(known_at, DateTime(timezone=True)),
                    literal(filter, JSONB),
                )
            try:
                rows = transaction.fetch(
                    CompiledReadRelation(
                        f"school_facts.{phase}.fixed",
                        as_of,
                        1,
                        ("payload",),
                        select(1),
                        select(payload.label("payload")),
                    )
                )
            except DBAPIError as error:
                # Only the exact cancellation proof's known semantic refusals
                # become a public reason. Authority/transport/unknown SQL errors
                # retain their existing unavailable boundary, without SQL text.
                if (
                    cancellation_start is not None
                    and getattr(error.orig, "sqlstate", None) == "22023"
                    and getattr(getattr(error.orig, "diag", None), "message_primary", None)
                    in _CANCELLATION_CORRECTION_REASONS
                ):
                    raise EducationCancellationRequiresCorrection() from None
                raise
            if len(rows) != 1 or type(rows[0].get("payload")) is not dict:
                raise ValueError("Invalid education database projection")
            raw = rows[0]["payload"]
        if raw is None:
            raise ValueError("Missing education projection")
        return raw


class EducationRepository:
    def __init__(
        self,
        *,
        projection: Any,
        authority: EducationAuthority,
        reference_key: bytes,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if type(reference_key) is not bytes or len(reference_key) < 16:
            raise ValueError("Private education reference key required")
        self.projection, self.authority, self.clock = projection, authority, clock
        self._key = hmac.digest(reference_key, b"education-references-v1", "sha256")

    def authorized_source_keys(
        self, actor: StaffActorContext, fact_kind: str, fields: tuple[str, ...]
    ) -> tuple[tuple[str, ...], int]:
        """Current metadata coordinates only; field authority precedes enumeration."""
        permit = self.authority.require_scope(
            actor, fact_kind=fact_kind, action="read", checked_at=self.clock()
        )
        self.authority.require_fields(permit, fields)
        if not permit.scope.is_school_wide:
            return tuple(permit.scope.object_keys), self.projection.current_version(actor.school_id)
        version = self.projection.current_version(actor.school_id)
        keys: list[str] = []
        after = None
        from time import monotonic

        deadline = monotonic() + 30
        while True:
            if monotonic() >= deadline or len(keys) >= 10000:
                raise EducationReadStale("学校资料范围过大，请缩小查询条件。")
            self.authority.revalidate(permit, actor=actor, checked_at=self.clock())
            page = self.projection.scope_keys(
                school_id=actor.school_id, fact_kind=fact_kind, after_key=after, limit=100
            )
            if (
                type(page) is not dict
                or type(page.get("keys")) is not list
                or len(page["keys"]) > 100
                or type(page.get("has_more")) is not bool
                or page.get("school_fact_version") != version
            ):
                raise EducationReadStale("学校资料枚举期间已变化。")
            selected = tuple(page["keys"])
            if any(type(key) is not str or not permit.scope.allows_key(key) for key in selected):
                raise ValueError("Invalid school source metadata")
            if selected != tuple(sorted(set(selected), key=lambda key: key.encode("utf-8"))) or (
                after is not None
                and selected
                and selected[0].encode("utf-8") <= after.encode("utf-8")
            ):
                raise ValueError("Invalid school source ordering")
            more = page["has_more"]
            next_after = selected[-1] if more and selected else None
            if page.get("next_after") != next_after or (more and not selected):
                raise ValueError("Invalid school source continuation")
            keys.extend(selected)
            if not more:
                break
            after = next_after
        self.authority.revalidate(permit, actor=actor, checked_at=self.clock())
        if self.projection.current_version(actor.school_id) != version:
            raise EducationReadStale("学校资料枚举期间已变化。")
        return tuple(keys), version

    def _reference(
        self, actor: StaffActorContext, kind: str, key: str, creation: bool,
        school_wide: bool = False,
    ) -> str:
        if school_wide:
            return encode_school_scope_reference(
                secret=self._key,
                family="f5" if kind in F5_FACT_KINDS else "f4",
                school_id=actor.school_id,
                assignment_id=actor.assignment_id,
                fact_kind=kind,
                source_key=key,
                creation=creation,
            )
        prefix = "f4create" if creation else "f4"
        body = f"{prefix}:{actor.school_id}:{actor.assignment_id}:{kind}:{key}"
        return f"{prefix}:{kind}:{hmac.new(self._key, body.encode(), sha256).hexdigest()}"

    def reference_for(
        self, actor: StaffActorContext, fact_kind: str, source_key: str, creation: bool = False
    ) -> str:
        permit = self.authority.require_scope(
            actor,
            fact_kind=fact_kind,
            action="maintain" if creation else "read",
            checked_at=self.clock(),
        )
        self.authority.require_object(permit, source_key, is_creation=creation)
        return self._reference(
            actor, fact_kind, source_key, creation, permit.scope.is_school_wide
        )

    def catalog_creation_references(
        self, actor: StaffActorContext, fact_kind: str, source_keys: tuple[str, ...]
    ) -> tuple[tuple[str, str], ...]:
        """Current finite creation coordinates; no object content is read."""
        if len(source_keys) > 100 or len(set(source_keys)) != len(source_keys):
            raise ValueError("Exact bounded unique creation catalog required")
        try:
            permit = self.authority.require_scope(
                actor, fact_kind=fact_kind, action="maintain", checked_at=self.clock()
            )
        except PermissionError:
            return ()
        if permit.scope.is_school_wide:
            if len(source_keys) >= 100:
                raise ValueError("School creation catalog leaves no bounded live choice")
            source_keys = (*source_keys, f"live:{fact_kind}:{uuid4()}")
        references = []
        for key in source_keys:
            try:
                self.authority.require_object(permit, key, is_creation=True)
            except PermissionError:
                continue
            references.append(
                (key, self._reference(actor, fact_kind, key, True, permit.scope.is_school_wide))
            )
        self.authority.revalidate(permit, actor=actor, checked_at=self.clock())
        return tuple(references)

    def creation_receipt_reference_for(self, *, actor, fact_kind: str, source_key: str) -> str:
        """Only called for Writer-authorized own audit targets, never to read a fact."""
        reference = self.reference_for(actor, fact_kind, source_key, creation=True)
        if is_school_scope_reference(reference):
            return "f4audit:" + reference
        return reference.replace("f4create:", "f4audit:", 1)

    def source_for_reference(
        self, actor: StaffActorContext, fact_kind: str, reference: str, creation: bool = False
    ) -> str:
        permit = self.authority.require_scope(
            actor,
            fact_kind=fact_kind,
            action="maintain" if creation else "read",
            checked_at=self.clock(),
        )
        if is_school_scope_reference(reference):
            source = decode_school_scope_reference(
                reference,
                secret=self._key,
                family="f5" if fact_kind in F5_FACT_KINDS else "f4",
                school_id=actor.school_id,
                assignment_id=actor.assignment_id,
                fact_kind=fact_kind,
                creation=creation,
            )
            if not permit.scope.is_school_wide:
                raise PermissionError("对象引用不属于当前获准范围。")
            self.authority.require_object(permit, source, is_creation=creation)
            self.authority.revalidate(permit, actor=actor, checked_at=self.clock())
            return source
        keys = permit.scope.creation_keys if creation else permit.scope.object_keys
        for key in keys:
            if hmac.compare_digest(reference, self._reference(actor, fact_kind, key, creation)):
                return key
        raise PermissionError("对象引用不属于当前获准范围。")

    def read_many(
        self,
        actor: StaffActorContext,
        selections: tuple[EducationReadSelection, ...],
        *,
        as_of: datetime | None = None,
    ) -> tuple[EducationReadResult, ...]:
        """Finite current read wave. No creation, maintenance or historical correction path."""
        if type(actor) is not StaffActorContext or not 1 <= len(selections) <= 4:
            raise ValueError("Invalid bounded education read wave")
        if any(type(item) is not EducationReadSelection for item in selections):
            raise ValueError("Fixed education selections required")
        selected = tuple(
            (
                item.kind,
                tuple(item.fields),
                None if item.source_keys is None else tuple(item.source_keys),
                _read_filter(item.kind, item.filter),
            )
            for item in selections
        )
        if any(keys is None for _, _, keys, _ in selected):
            preview = self.authority.require_scopes(
                actor, requests=tuple((kind, "read") for kind, *_ in selected),
                checked_at=self.clock(),
            )
            if any(
                keys is None and permit.scope.is_school_wide
                for (_, _, keys, _), permit in zip(selected, preview, strict=True)
            ):
                results = tuple(
                    self.read_records(
                        actor, kind, fields=fields, source_keys=keys,
                        filter=filters, as_of=as_of,
                    )
                    for kind, fields, keys, filters in selected
                )
                EducationReadProof.combine(tuple(result.proof for result in results)).revalidate(
                    actor=actor
                )
                return results
        at = aware(self.clock() if as_of is None else as_of)
        permits: list[EducationPermit] = []
        chosen: list[tuple] = []

        def before():
            fresh = self.authority.require_scopes(
                actor,
                requests=tuple((kind, "read") for kind, *_ in selected),
                checked_at=self.clock(),
            )
            if len(fresh) != len(selected):
                raise PermissionError("读取依据数量不一致。")
            for (kind, fields, keys, filters), permit in zip(selected, fresh, strict=True):
                if (
                    permit.actor != actor
                    or permit.action != "read"
                    or permit.scope.fact_kind != kind
                ):
                    raise PermissionError("读取依据对象不一致。")
                self.authority.require_fields(permit, fields)
                if filters:
                    self.authority.require_fields(permit, tuple(filters))
                allowed = F5_FACT_FIELDS[kind] if kind in F5_FACT_KINDS else F4_FACT_FIELDS[kind]
                if not fields or not set(fields) <= allowed or not set(filters) <= set(fields):
                    raise PermissionError("读取投影或筛选字段无权使用。")
                keys = tuple(permit.scope.object_keys) if keys is None else keys
                if len(keys) > 100 or len(set(keys)) != len(keys):
                    raise ValueError("Exact bounded unique education object set required")
                for key in keys:
                    self.authority.require_object(permit, key, is_creation=False)
                permits.append(permit)
                chosen.append((kind, keys, fields, filters))

        def after(raw):
            self.authority.revalidate_read_wave(
                tuple(permits), actor=actor, checked_at=self.clock()
            )
            current = self.projection.current_version(actor.school_id)
            if any(item.get("school_fact_version") != current for item in raw):
                raise EducationReadStale("学校资料在读取期间已经变化。")

        raw = self.projection.read_many(
            school_id=actor.school_id,
            selections=lambda: tuple(chosen),
            as_of=at,
            before_read=before,
            after_read=after,
        )
        if len(raw) != len(selected):
            raise ValueError("Malformed education read wave")
        return tuple(
            self._result(actor, kind, payload, (keys, fields), (permit,), at=at, as_of=as_of)
            for (kind, keys, fields, _), permit, payload in zip(chosen, permits, raw, strict=True)
        )

    def read_records(
        self,
        actor: StaffActorContext,
        kind: str,
        fields: tuple[str, ...] | None = None,
        source_keys: tuple[str, ...] | None = None,
        filter: dict[str, str | list[str] | int] | None = None,
        as_of: datetime | None = None,
        known_at: datetime | None = None,
        action: str = "read",
        creation: bool = False,
        cancellation_start: datetime | None = None,
        expected_version: int | None = None,
    ) -> EducationReadResult:
        if kind not in F4_FACT_KINDS | F5_FACT_KINDS:
            raise ValueError("Unknown education fact kind")
        if (action != "read" or creation) and (action != "maintain" or not creation):
            raise PermissionError("维护权限不能代替已有资料的读取权限。")
        at = aware(self.clock() if as_of is None else as_of)
        if cancellation_start is not None and (
            kind not in F5_FACT_KINDS
            or action != "read"
            or creation
            or source_keys is None
            or len(source_keys) != 1
            or known_at is not None
            or expected_version is None
            or expected_version < 1
            or aware(cancellation_start) <= self.clock()
        ):
            raise ValueError("取消证据须精确选择一个尚未生效的教学对象与当前版本。")
        if known_at is not None:
            aware(known_at)
            if known_at > self.clock():
                raise ValueError("Audit observation cannot be in the future")
        selected_filter = _read_filter(kind, filter)
        allowed_fields = F5_FACT_FIELDS[kind] if kind in F5_FACT_KINDS else F4_FACT_FIELDS[kind]
        initial_permit = None
        if source_keys is None and not creation and cancellation_start is None:
            permit = initial_permit = self.authority.require_scope(
                actor, fact_kind=kind, action=action, checked_at=self.clock()
            )
            if permit.scope.is_school_wide:
                requested_fields = (
                    tuple(permit.scope.field_names) if fields is None else tuple(fields)
                )
                self.authority.require_fields(permit, requested_fields)
                if selected_filter:
                    self.authority.require_fields(permit, tuple(selected_filter))
                if not set(requested_fields) <= allowed_fields or not set(
                    selected_filter
                ) <= set(requested_fields):
                    raise PermissionError("关联筛选字段必须包含在获准投影中。")
                version = self.projection.current_version(actor.school_id)
                all_results: list[EducationReadResult] = []
                after_key = None
                seen = 0
                from time import monotonic

                deadline = monotonic() + 30
                while True:
                    if monotonic() >= deadline or seen >= 10000:
                        raise EducationReadStale("学校资料范围过大，请缩小查询条件。")
                    self.authority.revalidate(permit, actor=actor, checked_at=self.clock())
                    page = self.projection.scope_keys(
                        school_id=actor.school_id, fact_kind=kind, after_key=after_key, limit=100
                    )
                    if (
                        type(page) is not dict
                        or type(page.get("keys")) is not list
                        or len(page["keys"]) > 100
                        or type(page.get("has_more")) is not bool
                        or page.get("school_fact_version") != version
                    ):
                        raise EducationReadStale("学校资料枚举期间已变化。")
                    keys = tuple(page["keys"])
                    if any(
                        type(key) is not str or not permit.scope.allows_key(key)
                        for key in keys
                    ):
                        raise ValueError("Invalid school source metadata")
                    if keys != tuple(sorted(set(keys), key=lambda key: key.encode("utf-8"))) or (
                        after_key is not None
                        and keys
                        and keys[0].encode("utf-8") <= after_key.encode("utf-8")
                    ):
                        raise ValueError("Invalid school source ordering")
                    more = page["has_more"]
                    next_after = keys[-1] if more and keys else None
                    if page.get("next_after") != next_after or (more and not keys):
                        raise ValueError("Invalid school source continuation")
                    seen += len(keys)
                    if seen > 10000:
                        raise EducationReadStale("学校资料范围过大，请缩小查询条件。")
                    if keys:
                        all_results.append(self.read_records(
                            actor, kind, fields=requested_fields, source_keys=keys,
                            filter=selected_filter, as_of=at, known_at=known_at, action=action,
                        ))
                        if all_results[-1].school_fact_version != version:
                            raise EducationReadStale("学校资料枚举期间已变化。")
                    if not more:
                        break
                    after_key = next_after
                self.authority.revalidate(permit, actor=actor, checked_at=self.clock())
                if self.projection.current_version(actor.school_id) != version:
                    raise EducationReadStale("学校资料枚举期间已变化。")
                if not all_results:
                    return self.read_records(
                        actor, kind, fields=requested_fields, source_keys=(),
                        filter=selected_filter, as_of=at, known_at=known_at, action=action,
                    )
                proof = EducationReadProof.combine(tuple(result.proof for result in all_results))
                proof.revalidate(actor=actor)
                return EducationReadResult(
                    tuple(record for result in all_results for record in result.records),
                    version, at, proof,
                    "available"
                    if any(result.records for result in all_results)
                    else "no_current_records",
                    tuple(ref for result in all_results for ref in result.references),
                )
        permits: list[EducationPermit] = []
        chosen: list[tuple[str, ...]] = []

        def before() -> None:
            permit = initial_permit or self.authority.require_scope(
                actor, fact_kind=kind, action=action, checked_at=self.clock()
            )
            if initial_permit is not None:
                self.authority.revalidate(permit, actor=actor, checked_at=self.clock())
            requested_fields = tuple(permit.scope.field_names) if fields is None else tuple(fields)
            self.authority.require_fields(permit, requested_fields)
            self.authority.require_fields(
                permit, tuple(selected_filter)
            ) if selected_filter else None
            if not set(requested_fields) <= allowed_fields:
                raise PermissionError("字段不属于该教育事实。")
            if not set(selected_filter) <= set(requested_fields):
                raise PermissionError("关联筛选字段必须包含在获准投影中。")
            requested_keys = (
                (permit.scope.creation_keys if creation else permit.scope.object_keys)
                if source_keys is None
                else tuple(source_keys)
            )
            if len(requested_keys) > 100 or len(set(requested_keys)) != len(requested_keys):
                raise ValueError("Exact bounded unique education object set required")
            for key in requested_keys:
                self.authority.require_object(permit, key, is_creation=creation)
            permits.append(permit)
            if cancellation_start is not None:
                maintenance = self.authority.require_scope(
                    actor, fact_kind=kind, action="maintain", checked_at=self.clock()
                )
                self.authority.require_object(maintenance, requested_keys[0], is_creation=False)
                self.authority.require_fields(maintenance, requested_fields)
                permits.append(maintenance)
            chosen.extend((tuple(requested_keys), requested_fields))

        def after(raw: dict[str, Any]) -> None:
            for permit in permits:
                self.authority.revalidate(permit, actor=actor, checked_at=self.clock())
            if raw.get("school_fact_version") != self.projection.current_version(actor.school_id):
                raise EducationReadStale("学校资料在读取期间已经变化。")

        cancellation_arguments = (
            {}
            if cancellation_start is None
            else {
                "cancellation_start": cancellation_start,
                "expected_version": expected_version,
            }
        )
        raw = self.projection.read(
            school_id=actor.school_id,
            fact_kind=kind,
            as_of=at,
            known_at=known_at,
            filter=selected_filter,
            metadata_only=creation,
            source_keys=lambda: chosen[0],
            field_names=lambda: chosen[1],
            before_read=before,
            after_read=after,
            **cancellation_arguments,
        )
        return self._result(
            actor,
            kind,
            raw,
            chosen,
            permits,
            at=at,
            as_of=as_of,
            creation=creation,
            cancellation_start=cancellation_start,
        )

    def _result(
        self,
        actor,
        kind,
        raw,
        chosen,
        permits,
        *,
        at,
        as_of,
        creation=False,
        cancellation_start=None,
    ):
        raw_records = raw.get("records")
        version = raw.get("school_fact_version")
        if type(raw_records) is not list or type(version) is not int:
            raise ValueError("Malformed education records")
        records = cast(list[dict[str, Any]], raw_records)
        boundaries: list[datetime] = []
        for record in records:
            if record.get("source_key") not in chosen[0]:
                raise PermissionError("数据库投影超出获准对象。")
            if creation:
                if record.get("status") != "not_recorded" or record.get("version") != 0:
                    raise PermissionError("创建对象已经存在，请先取得正式读取权限。")
                if set(record) - {"source_key", "version", "status"}:
                    raise ValueError("Creation coordinates must not contain existing content")
            else:
                if record.get("fact_kind") != kind:
                    raise ValueError("Education projection family mismatch")
                if not set(record.get("introduced_content", {})) <= set(chosen[1]):
                    raise PermissionError("取消原态超出获准字段。")
                for segment in (*record.get("segments", []), *record.get("prior_segments", [])):
                    if not set(segment.get("content", {})) <= set(chosen[1]):
                        raise PermissionError("数据库投影超出获准字段。")
                    prior = segment.get("causal_prior")
                    if prior is not None and not set(prior.get("content", {})) <= set(chosen[1]):
                        raise PermissionError("因果前态投影超出获准字段。")
                boundary = timestamp(record.get("next_valid_boundary"))
                if boundary is not None and (as_of is None or cancellation_start is not None):
                    boundaries.append(boundary)
        proof = EducationReadProof(
            self, actor, tuple(permits), version, min(boundaries, default=None)
        )
        if proof.next_boundary is not None and self.clock() >= proof.next_boundary:
            raise EducationReadStale("资料适用时间已经变化。")
        return EducationReadResult(
            tuple(records),
            version,
            at,
            proof,
            str(raw.get("status", "no_current_records")),
            tuple(
                (
                    row["source_key"],
                    self._reference(
                        actor, kind, row["source_key"], creation,
                        permits[0].scope.is_school_wide,
                    ),
                )
                for row in records
            ),
        )
