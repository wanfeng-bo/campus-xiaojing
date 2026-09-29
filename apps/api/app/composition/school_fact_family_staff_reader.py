"""Production Staff F3 narrow Reader and maintenance repository."""

from __future__ import annotations

import hmac
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any, Protocol, cast
from uuid import UUID

from pydantic import JsonValue
from sqlalchemy import DateTime, Select, Text, column, func, literal, or_, select, text, true
from sqlalchemy import cast as sql_cast
from sqlalchemy.dialects.postgresql import JSONB, array

from app.composition.school_fact_family_authority import (
    F3SchoolScopePermit,
    FamilyFactAuthorityDenied,
    FamilyFactTarget,
    StaffFamilyFactPermit,
)
from app.composition.school_fact_family_product_adapter import FamilyMaintenanceTarget
from app.composition.school_fact_family_settings import FamilyConfiguration
from app.composition.school_fact_read_capture import F2EvidenceBackend, ReadCapture
from app.platform.authorization.secure_reader_compiler import (
    CompiledReadRelation,
    ServerOwnedProjectionCompiler,
)
from app.platform.authorization.secure_reader_service import ReadTransaction
from app.platform.contracts import StaffActorContext
from app.platform.master_data_imports.models import SchoolFactChangeLineage
from app.platform.school_facts.f3_policy import F3_FACT_FIELDS, F3_FACT_KINDS
from app.platform.school_facts.school_scope_reference import (
    decode_school_scope_reference,
    encode_school_scope_reference,
    is_school_scope_reference,
)


class ExternalFamilyReferencePort(Protocol):
    def resolve(self, *, actor: StaffActorContext, fact_kind: str, reference: str) -> str: ...
    def reference_for(
        self, *, actor: StaffActorContext, fact_kind: str, source_key: str
    ) -> str: ...


_REFERENCE_KINDS = {
    "person_ref": "person",
    "guardian_person_ref": "person",
    "contact_person_ref": "person",
    "student_ref": "student",
    "review_of_relationship_ref": "guardian_relationship",
}
_EXTERNAL_REFERENCE_KINDS = {
    "primary_campus_ref": "location",
    "administrative_campus_ref": "location",
    "organization_ref": "organization",
}
_RELATED_FIELDS: dict[str, dict[str, str]] = {
    "student": {"person_ref": "person"},
    "student_school_relationship": {
        "student_ref": "student",
        "primary_campus_ref": "location",
        "administrative_campus_ref": "location",
    },
    "student_contact": {
        "student_ref": "student",
        "contact_person_ref": "person",
    },
    "guardian_relationship": {
        "guardian_person_ref": "person",
        "student_ref": "student",
        "review_of_relationship_ref": "guardian_relationship",
    },
    "person_region": {"person_ref": "person"},
    "login_mapping": {"person_ref": "person"},
    "assignment": {"person_ref": "person", "organization_ref": "organization"},
}
ScopeAuthorize = Callable[[StaffActorContext, str, str, str, bool, datetime], StaffFamilyFactPermit]
Revalidate = Callable[
    [StaffFamilyFactPermit, StaffActorContext, FamilyFactTarget, datetime], object
]
SchoolScopeAuthorize = Callable[[StaffActorContext, str, str, datetime], F3SchoolScopePermit]
SchoolScopeRevalidate = Callable[[F3SchoolScopePermit, StaffActorContext, datetime], None]


@dataclass(frozen=True, slots=True)
class FamilyStaffFact:
    fact_kind: str
    reference: str
    label: str
    content: dict[str, JsonValue]
    expected_version: int
    school_fact_version: int
    factual_at: datetime
    valid_from: datetime | None
    valid_to: datetime | None
    source_reference: str
    source_name: str | None
    source_trace_reference: str | None
    source_published_at: datetime | None


class _ValidatedRead(Protocol):
    def validated_read(
        self, before_read: Callable[[], None], after_read: Callable[[], None]
    ) -> AbstractContextManager[ReadTransaction]: ...


class _MetadataTransaction(Protocol):
    def metadata(self, relation: CompiledReadRelation) -> tuple[tuple[object, ...], ...]: ...


class F3SignatureCompiler(ServerOwnedProjectionCompiler):
    """Hash only the exact authorized F3 target projection inside PostgreSQL."""

    def compile(self, **kwargs: Any) -> CompiledReadRelation:
        if not kwargs.get("family_signature"):
            return super().compile(**kwargs)
        school, kind, sources, fields, at = (
            kwargs[name] for name in ("school_id", "fact_kind", "sources", "fields", "as_of")
        )
        if (
            kind not in F3_FACT_KINDS
            or not 1 <= len(sources) <= 100
            or len(set(sources)) != len(sources)
            or not fields
            or not set(fields) <= F3_FACT_FIELDS[kind]
        ):
            raise ValueError("Invalid fixed F3 evidence projection")
        source = (
            func.unnest(array(list(sources), type_=Text))
            .table_valued("source_key")
            .render_derived()
        )
        payload = sql_cast(
            func.app.school_fact_f3_staff_target(
                school, kind, source.c.source_key, False, at, list(fields)
            ),
            JSONB,
        )
        targets = (
            select(source.c.source_key, payload.label("payload"))
            .cte("f3_authorized_targets")
            .prefix_with("MATERIALIZED")
        )
        payload = targets.c.payload
        signature = func.encode(
            func.sha256(func.convert_to(sql_cast(payload.op("-")("factual_at"), Text), "UTF8")),
            "hex",
        )
        lineage = SchoolFactChangeLineage.__table__
        publications = (
            select(func.count())
            .where(
                lineage.c.school_id == school,
                lineage.c.fact_kind == kind,
                lineage.c.fact_source_key == targets.c.source_key,
            )
            .correlate(targets)
            .scalar_subquery()
        )
        statement = select(
            targets.c.source_key,
            signature.label("signature"),
            payload["status"].astext.label("status"),
            sql_cast(payload["valid_to"].astext, DateTime(timezone=True)).label("valid_to"),
            publications.label("publication_count"),
        ).order_by(targets.c.source_key)
        return CompiledReadRelation(
            "school_facts.f3.staff.signature",
            at,
            len(sources),
            ("source_key", "signature", "status", "valid_to", "publication_count"),
            select(literal(len(sources))),
            statement,
            statement,
        )


class F3EvidenceBackend(F2EvidenceBackend):
    """Reuse short F2 proof batches with F3-owned whole-membership SQL signatures."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.compiler = F3SignatureCompiler()
        self._active: ContextVar[ReadCapture | None] = ContextVar(
            "f3_signature_capture", default=None
        )

    @contextmanager
    def capture(self, *, actor: StaffActorContext, current_time: bool = True):
        with super().capture(actor=actor, current_time=current_time) as capture:
            token = self._active.set(capture)
            try:
                yield capture
            finally:
                self._active.reset(token)

    def unsupported(self) -> None:
        if (capture := self._active.get()) is not None:
            capture.supported = False

    def record(
        self,
        transaction: ReadTransaction,
        *,
        school_id: UUID,
        fact_kind: str,
        sources: tuple[str, ...],
        fields: tuple[str, ...],
        as_of: datetime,
    ) -> None:
        capture = self._active.get()
        if capture is None or not capture.supported or not sources:
            return
        arguments = dict(
            family_signature=True,
            school_id=school_id,
            fact_kind=fact_kind,
            sources=sources,
            fields=fields,
            as_of=as_of,
        )
        relation = self.compiler.compile(**arguments)
        capture.relations[id(relation)] = (relation, arguments, capture.current_time)
        metadata = cast(_MetadataTransaction, transaction).metadata(relation)
        if capture.current_time:
            for _source, _signature, status, valid_to, publications in metadata:
                # Existing Reader ACLs deliberately exclude unit.payload. Without
                # all publication effective_at values we cannot prove the next
                # natural transition of a future/ended/multi-publication source.
                # Those queries retain the complete fresh-read fallback.
                if not (publications == 0 or publications == 1 and status == "available"):
                    capture.supported = False
                    return
                if isinstance(valid_to, datetime):
                    capture.deadlines.append(valid_to)
        capture.record_page(relation, len(sources), metadata, ())


class SqlFamilyStaffProjection:
    """One fixed projection inside the shared pre-read/RR/post-read connection flow."""

    def __init__(self, executor: _ValidatedRead, evidence: F3EvidenceBackend | None = None) -> None:
        self.executor = executor
        self.evidence = evidence

    def current_version(self, school_id: UUID) -> int:
        with self.executor.sessions() as session:
            value = session.scalar(
                text("SELECT app.school_fact_current_version(:school)"), {"school": school_id}
            )
        if type(value) is not int or value < 0:
            raise ValueError("Invalid school fact version")
        return value

    def scope_keys(
        self, *, school_id, fact_kind, after_key, limit, factual_at, before_read, after_read
    ):
        with self.executor.validated_read(before_read, after_read) as transaction:
            statement = select(
                func.app.school_fact_scope_keys(
                    school_id, "f3", fact_kind, after_key, limit
                ).label("payload")
            )
            rows = transaction.fetch(
                CompiledReadRelation(
                    "school_facts.f3.school.scope_keys",
                    factual_at,
                    1,
                    ("payload",),
                    select(1),
                    statement,
                )
            )
            if self.evidence is not None:
                self.evidence.unsupported()
        if len(rows) != 1 or set(rows[0]) != {"payload"} or type(rows[0]["payload"]) is not dict:
            raise ValueError("Invalid school key projection")
        return rows[0]["payload"]

    def student_relationship_keys(
        self, *, school_id, student_source_key, allowed_keys, factual_at, before_read, after_read
    ):
        with self.executor.validated_read(before_read, after_read) as transaction:
            statement = select(
                func.app.school_fact_f6_student_relationship_keys(
                    school_id, student_source_key, list(allowed_keys())
                ).label("payload")
            )
            rows = transaction.fetch(
                CompiledReadRelation(
                    "school_facts.f6.student.relationship_keys",
                    factual_at,
                    1,
                    ("payload",),
                    select(1),
                    statement,
                )
            )
            if self.evidence is not None:
                # The complete-set closure has its own synchronous proof below.
                self.evidence.unsupported()
        if len(rows) != 1 or set(rows[0]) != {"payload"} or type(rows[0]["payload"]) is not dict:
            raise ValueError("Invalid complete student relationship projection")
        return rows[0]["payload"]

    def student_school_relationship_keys(
        self, *, school_id, student_source_key, factual_at, before_read, after_read
    ):
        with self.executor.validated_read(before_read, after_read) as transaction:
            rows = transaction.fetch(
                CompiledReadRelation(
                    "school_facts.f6.student.school_relationship_metadata",
                    factual_at, 1, ("payload",), select(1),
                    select(func.app.school_fact_f6_student_school_relationship_keys(
                        school_id, student_source_key
                    ).label("payload")),
                )
            )
        if len(rows) != 1 or set(rows[0]) != {"payload"} or type(rows[0]["payload"]) is not dict:
            raise ValueError("Invalid student relationship metadata")
        return rows[0]["payload"]

    def education_timeline(
        self, *, school_id, source_key, factual_at, field_names, before_read, after_read
    ):
        with self.executor.validated_read(before_read, after_read) as transaction:
            statement = select(
                func.app.school_fact_f4_ssr_timeline(
                    school_id, source_key, list(field_names())
                ).label("payload")
            )
            rows = transaction.fetch(
                CompiledReadRelation(
                    "school_facts.f4.ssr.timeline",
                    factual_at,
                    1,
                    ("payload",),
                    select(1),
                    statement,
                )
            )
            if self.evidence is not None:
                self.evidence.record(
                    transaction,
                    school_id=school_id,
                    fact_kind="student_school_relationship",
                    sources=(source_key,),
                    fields=field_names(),
                    as_of=factual_at,
                )
        if len(rows) != 1 or set(rows[0]) != {"payload"} or type(rows[0]["payload"]) is not dict:
            raise ValueError("Invalid F4 SSR timeline")
        return rows[0]["payload"]

    def read(
        self,
        *,
        school_id: UUID,
        fact_kind: str,
        source_key: str,
        creation: bool,
        factual_at: datetime,
        field_names: Callable[[], tuple[str, ...]],
        before_read: Callable[[], None],
        after_read: Callable[[], None],
    ) -> Mapping[str, object]:
        with self.executor.validated_read(before_read, after_read) as transaction:
            statement = select(
                func.app.school_fact_f3_staff_target(
                    school_id, fact_kind, source_key, creation, factual_at, list(field_names())
                ).label("payload")
            )
            rows = transaction.fetch(
                CompiledReadRelation(
                    "school_facts.f3.staff.fixed", factual_at, 1, ("payload",), select(1), statement
                )
            )
            if self.evidence is not None:
                if creation:
                    self.evidence.unsupported()
                else:
                    self.evidence.record(
                        transaction,
                        school_id=school_id,
                        fact_kind=fact_kind,
                        sources=(source_key,),
                        fields=field_names(),
                        as_of=factual_at,
                    )
        if len(rows) != 1 or set(rows[0]) != {"payload"} or type(rows[0]["payload"]) is not dict:
            raise ValueError("Invalid F3 Staff projection")
        return cast(dict[str, object], rows[0]["payload"])

    def candidates(
        self,
        *,
        school_id: UUID,
        fact_kind: str,
        factual_at: datetime,
        match_field: str | None,
        match_value: str | None,
        limit: int,
        source_groups: Callable[[], tuple[tuple[tuple[str, ...], tuple[str, ...]], ...]],
        before_read: Callable[[], None],
        after_read: Callable[[], None],
        formal_future: bool = False,
    ) -> tuple[Mapping[str, object], ...]:
        collected: list[Mapping[str, object]] = []
        with self.executor.validated_read(before_read, after_read) as transaction:
            for sources, fields in source_groups():
                if self.evidence is not None and formal_future:
                    # F6 uses its full exact-query history recheck. The ordinary
                    # F3 signature deliberately rejects future factual times.
                    self.evidence.unsupported()
                elif self.evidence is not None:
                    self.evidence.record(
                        transaction,
                        school_id=school_id,
                        fact_kind=fact_kind,
                        sources=sources,
                        fields=fields,
                        as_of=factual_at,
                    )
                remaining = limit - len(collected)
                if remaining <= 0:
                    continue
                if formal_future:
                    if fact_kind != "student_school_relationship":
                        raise ValueError("Formal future selection belongs to SSR")
                    statement = self._future_relationship_statement(
                        school_id, sources, fields, factual_at, match_field, match_value, remaining
                    )
                else:
                    relation = func.app.school_fact_f3_staff_candidates(
                        school_id,
                        fact_kind,
                        list(sources),
                        factual_at,
                        list(fields),
                        match_field,
                        match_value,
                        remaining,
                    ).table_valued("source_key", "label", "payload")
                    statement = select(relation.c.source_key, relation.c.label, relation.c.payload)
                rows = transaction.fetch(
                    CompiledReadRelation(
                        "school_facts.f3.staff.candidates",
                        factual_at,
                        remaining,
                        ("source_key", "label", "payload"),
                        select(1),
                        statement,
                    )
                )
                collected.extend(rows)
        if len(collected) > limit or any(
            set(row) != {"source_key", "label", "payload"}
            or type(row["source_key"]) is not str
            or type(row["label"]) is not str
            or type(row["payload"]) is not dict
            for row in collected
        ):
            raise ValueError("Invalid F3 candidate projection")
        return tuple(collected)

    @staticmethod
    def _future_relationship_statement(
        school: UUID,
        sources: tuple[str, ...],
        fields: tuple[str, ...],
        at: datetime,
        match_field: str | None,
        match_value: str | None,
        limit: int,
    ) -> Select[Any]:
        """Filter the existing fixed F4 timeline in SQL, before returning any rows."""
        if (
            not 1 <= len(sources) <= 100
            or len(set(sources)) != len(sources)
            or not fields
            or not set(fields) <= F3_FACT_FIELDS["student_school_relationship"]
            or not 1 <= limit <= 21
            or (match_field is None) != (match_value is None)
            or (
                match_field is not None
                and (
                    match_field
                    not in {
                        "student_number",
                        "student_source_key",
                        "primary_campus_source_key",
                        "administrative_campus_source_key",
                    }
                    or match_field not in fields
                    or not match_value
                    or len(match_value) > 1000
                )
            )
        ):
            raise ValueError("Invalid finite future SSR projection")
        source = (
            func.unnest(array(list(sources), type_=Text))
            .table_valued("source_key")
            .render_derived()
        )
        timeline = (
            select(
                sql_cast(
                    func.app.school_fact_f4_ssr_timeline(school, source.c.source_key, list(fields)),
                    JSONB,
                ).label("payload")
            )
            .correlate(source)
            .lateral()
        )
        segment = (
            func.jsonb_array_elements(timeline.c.payload["segments"])
            .table_valued(column("value", JSONB))
            .lateral()
        )
        value = segment.c.value
        content, origin = value["content"], value["source"]
        projected = func.jsonb_build_object(
            "status",
            "available",
            "fact_kind",
            "student_school_relationship",
            "source_key",
            source.c.source_key,
            "content",
            content,
            "expected_version",
            timeline.c.payload["version"],
            "school_fact_version",
            timeline.c.payload["school_fact_version"],
            "label",
            "学生在校关系",
            "source_reference",
            origin["publication_reference"],
            "source_published_at",
            origin["published_at"],
            "source_trace_reference",
            origin["source_ref"],
            "source_name",
            None,
            "factual_at",
            at,
            "valid_from",
            value["effective_at"],
            "valid_to",
            value["valid_to"],
        )
        statement = (
            select(
                source.c.source_key,
                literal("学生在校关系").label("label"),
                projected.label("payload"),
            )
            .select_from(source.join(timeline, true()).join(segment, true()))
            .where(
                sql_cast(value["effective_at"].astext, DateTime(timezone=True)) <= at,
                or_(
                    value["valid_to"].astext.is_(None),
                    sql_cast(value["valid_to"].astext, DateTime(timezone=True)) > at,
                ),
            )
        )
        if match_field is not None:
            statement = statement.where(content[match_field].astext == match_value)
        return statement.order_by(source.c.source_key).limit(limit)


class FamilyFactNotRecorded(RuntimeError):
    pass


class FamilyFactConflict(RuntimeError):
    pass


class FamilyStaffRepository:
    def __init__(
        self,
        *,
        projection: SqlFamilyStaffProjection,
        configuration: FamilyConfiguration,
        reference_key: bytes,
        authorize_scope: ScopeAuthorize,
        revalidate: Revalidate,
        authorize_school_scope: SchoolScopeAuthorize | None = None,
        revalidate_school_scope: SchoolScopeRevalidate | None = None,
        external_references: ExternalFamilyReferencePort | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if type(reference_key) is not bytes or len(reference_key) < 16:
            raise ValueError("Private F3 reference key required")
        if (authorize_school_scope is None) != (revalidate_school_scope is None):
            raise ValueError("School scope callbacks must be paired")
        self._projection, self._configuration = projection, configuration
        self._key = hmac.digest(reference_key, b"school-facts-f3-references-v1", "sha256")
        self._authorize_scope, self._revalidate, self._clock = authorize_scope, revalidate, clock
        self._authorize_school_scope = authorize_school_scope
        self._revalidate_school_scope = revalidate_school_scope
        self._external_references = external_references
        self.evidence = getattr(projection, "evidence", None)

    def _school_source_pages(
        self, actor: StaffActorContext, fact_kind: str, fields: tuple[str, ...] | None
    ):
        if self._authorize_school_scope is None or self._revalidate_school_scope is None:
            raise FamilyFactAuthorityDenied("School scope authority is unavailable.")
        initial = self._authorize_school_scope(actor, "read", fact_kind, self._clock())
        if not initial.scope.is_school_wide or (
            fields is not None and not set(fields) <= set(initial.scope.field_names)
        ):
            raise FamilyFactAuthorityDenied("School scope fields are not authorized.")
        version = self._projection.current_version(actor.school_id)
        after: str | None = None
        from time import monotonic

        deadline = monotonic() + 30
        for _ in range(100):
            if monotonic() >= deadline:
                raise ValueError("School source key enumeration exceeded its time limit.")
            permits: list[F3SchoolScopePermit] = []

            def before(permits=permits) -> None:
                permit = self._authorize_school_scope(actor, "read", fact_kind, self._clock())
                if not permit.scope.is_school_wide or (
                    fields is not None and not set(fields) <= set(permit.scope.field_names)
                ):
                    raise FamilyFactAuthorityDenied("School scope fields are not authorized.")
                permits.append(permit)

            def after_read(permits=permits) -> None:
                if len(permits) != 1:
                    raise FamilyFactAuthorityDenied("School scope is missing.")
                self._revalidate_school_scope(permits[0], actor, self._clock())

            page = self._projection.scope_keys(
                school_id=actor.school_id,
                fact_kind=fact_kind,
                after_key=after,
                limit=100,
                factual_at=self._clock(),
                before_read=before,
                after_read=after_read,
            )
            keys = page.get("keys")
            more = page.get("has_more")
            next_after = page.get("next_after")
            if (
                type(keys) is not list
                or len(keys) > 100
                or type(more) is not bool
                or page.get("school_fact_version") != version
                or next_after != (keys[-1] if more and keys else None)
                or (more and not keys)
                or any(
                    type(key) is not str
                    or not permits[0].scope.allows_key(key)
                    for key in keys
                )
                or (after is not None and keys and keys[0].encode() <= after.encode())
                or any(
                    left.encode() >= right.encode()
                    for left, right in zip(keys, keys[1:], strict=False)
                )
            ):
                raise ValueError("School source key page changed or is invalid.")
            yield tuple(keys)
            if not more:
                self._revalidate_school_scope(initial, actor, self._clock())
                if self._projection.current_version(actor.school_id) != version:
                    raise ValueError("School source key version changed.")
                return
            after = next_after
        raise ValueError("School source key pagination did not complete.")

    def reference_for(
        self, *, actor: StaffActorContext, fact_kind: str, source_key: str,
        creation: bool = False, school_wide: bool = False,
    ) -> str:
        if fact_kind not in F3_FACT_KINDS or not source_key:
            raise ValueError("Invalid F3 reference")
        if school_wide:
            action = (
                "request" if creation and fact_kind == "guardian_relationship"
                else "maintain" if creation else "read"
            )
            permit = self._authorize_scope(
                actor, action, fact_kind, source_key, creation, self._clock()
            )
            if not permit.scope.is_school_wide or not permit.scope.allows_key(
                source_key, is_creation=creation
            ):
                raise FamilyFactAuthorityDenied("Current school scope does not cover reference.")
            self._revalidate(permit, actor, permit.target, self._clock())
            return encode_school_scope_reference(
                secret=self._key,
                family="f3",
                school_id=actor.school_id,
                assignment_id=actor.assignment_id,
                fact_kind=fact_kind,
                source_key=source_key,
                creation=creation,
            )
        prefix = "f3create" if creation else "f3"
        digest = hmac.new(
            self._key, f"{prefix}:{actor.school_id}:{fact_kind}:{source_key}".encode(), sha256
        ).hexdigest()
        return f"{prefix}:{fact_kind}:{digest}"

    def _source_for(
        self, actor: StaffActorContext, fact_kind: str, reference: str, creation: bool
    ) -> str:
        if is_school_scope_reference(reference):
            source = decode_school_scope_reference(
                reference,
                secret=self._key,
                family="f3",
                school_id=actor.school_id,
                assignment_id=actor.assignment_id,
                fact_kind=fact_kind,
                creation=creation,
            )
            action = (
                "request" if creation and fact_kind == "guardian_relationship"
                else "maintain" if creation else "read"
            )
            permit = self._authorize_scope(
                actor, action, fact_kind, source, creation, self._clock()
            )
            if not permit.scope.is_school_wide or not permit.scope.allows_key(
                source, is_creation=creation
            ):
                raise FamilyFactAuthorityDenied("Current school scope does not cover reference.")
            self._revalidate(permit, actor, permit.target, self._clock())
            return source
        prefix = "f3create" if creation else "f3"
        if not reference.startswith(f"{prefix}:{fact_kind}:"):
            raise ValueError("Reference belongs to another F3 kind")
        candidates = (
            self._configuration.creation_catalog.get(fact_kind, {})
            if creation
            else self._configuration.source_catalog.get(fact_kind, ())
        )
        for source in candidates:
            if hmac.compare_digest(
                reference,
                self.reference_for(
                    actor=actor, fact_kind=fact_kind, source_key=source, creation=creation
                ),
            ):
                return source
        raise ValueError("Reference unavailable; select again")

    def source_for_reference(self, actor: StaffActorContext, reference: str) -> tuple[str, str]:
        """Resolve a signed existing target; this does not grant read/maintenance permission."""
        parts = reference.split(":")
        if is_school_scope_reference(reference):
            if len(parts) != 6 or parts[2] not in F3_FACT_KINDS:
                raise ValueError("An existing F3 target is required")
            return parts[2], self._source_for(actor, parts[2], reference, False)
        if len(parts) != 3 or parts[0] != "f3" or parts[1] not in F3_FACT_KINDS:
            raise ValueError("An existing F3 target is required")
        return parts[1], self._source_for(actor, parts[1], reference, False)

    def _fact_from_raw(
        self,
        *,
        actor: StaffActorContext,
        fact_kind: str,
        source_key: str,
        creation: bool,
        permit: StaffFamilyFactPermit,
        raw: Mapping[str, object],
    ) -> FamilyStaffFact:
        required = {
            "status",
            "fact_kind",
            "source_key",
            "content",
            "expected_version",
            "school_fact_version",
            "label",
            "source_reference",
            "factual_at",
            "valid_from",
            "valid_to",
            "source_published_at",
            "source_name",
            "source_trace_reference",
        }
        if set(raw) == required and raw["status"] == "no_current_records":
            raise FamilyFactNotRecorded()
        if set(raw) == required and raw["status"] == "conflict":
            raise FamilyFactConflict()
        if (
            set(raw) != required
            or raw["status"] != ("available_creation" if creation else "available")
            or raw["fact_kind"] != fact_kind
            or raw["source_key"] != source_key
            or type(raw["content"]) is not dict
        ):
            raise ValueError("F3 target changed, is ambiguous, or projection is invalid")
        content = cast(dict[str, JsonValue], raw["content"])
        if set(content) - F3_FACT_FIELDS[fact_kind] or set(content) - set(permit.scope.field_names):
            raise ValueError("F3 projection returned unapproved fields")
        if raw["status"] == "available" and raw["valid_from"] is None:
            raise ValueError("Available F3 fact requires real valid_from")
        if type(raw["expected_version"]) is not int or type(raw["school_fact_version"]) is not int:
            raise ValueError("Invalid F3 version projection")

        def optional_time(value: object) -> datetime | None:
            return None if value is None else datetime.fromisoformat(str(value))

        return FamilyStaffFact(
            fact_kind=fact_kind,
            reference=self.reference_for(
                actor=actor, fact_kind=fact_kind, source_key=source_key,
                creation=creation, school_wide=permit.scope.is_school_wide,
            ),
            label=str(raw["label"]),
            content=dict(content),
            expected_version=raw["expected_version"],
            school_fact_version=raw["school_fact_version"],
            factual_at=datetime.fromisoformat(str(raw["factual_at"])),
            valid_from=optional_time(raw["valid_from"]),
            valid_to=optional_time(raw["valid_to"]),
            source_reference=str(raw["source_reference"]),
            source_name=None if raw["source_name"] is None else str(raw["source_name"]),
            source_trace_reference=(
                None
                if raw["source_trace_reference"] is None
                else str(raw["source_trace_reference"])
            ),
            source_published_at=optional_time(raw["source_published_at"]),
        )

    def _read(
        self,
        *,
        actor: StaffActorContext,
        fact_kind: str,
        source_key: str,
        creation: bool,
        action: str = "maintain",
        factual_at: datetime | None = None,
        requested_fields: tuple[str, ...] | None = None,
    ) -> tuple[FamilyStaffFact, FamilyFactTarget, StaffFamilyFactPermit]:
        checked_at = self._clock()
        at = checked_at if factual_at is None else factual_at
        permits: list[StaffFamilyFactPermit] = []

        def before() -> None:
            permits.append(
                self._authorize_scope(actor, action, fact_kind, source_key, creation, checked_at)
            )

        def after() -> None:
            if len(permits) != 1:
                raise ValueError("F3 pre-read authority missing")
            self._revalidate(permits[0], actor, permits[0].target, self._clock())

        def projected_fields() -> tuple[str, ...]:
            allowed = permits[0].scope.field_names
            selected = (
                allowed
                if requested_fields is None
                else tuple(name for name in allowed if name in requested_fields)
            )
            if not selected:
                raise FamilyFactAuthorityDenied("Requested fields are not authorized.")
            return selected

        raw = self._projection.read(
            school_id=actor.school_id,
            fact_kind=fact_kind,
            source_key=source_key,
            creation=creation,
            factual_at=at,
            field_names=projected_fields,
            before_read=before,
            after_read=after,
        )
        fact = self._fact_from_raw(
            actor=actor,
            fact_kind=fact_kind,
            source_key=source_key,
            creation=creation,
            permit=permits[0],
            raw=raw,
        )
        if set(fact.content) - set(projected_fields()):
            raise ValueError("F3 projection returned unrequested fields")
        return fact, permits[0].target, permits[0]

    def read_education_timeline(self, *, actor: StaffActorContext, source_key: str):
        """F4 full-current timeline; independently require every replaced field."""
        permits = []
        fields = tuple(sorted(F3_FACT_FIELDS["student_school_relationship"]))
        now = self._clock()

        def before():
            permit = self._authorize_scope(
                actor, "read", "student_school_relationship", source_key, False, now
            )
            if not set(fields) <= set(permit.scope.field_names):
                raise ValueError("完整在校关系补正需要全部原字段读取权限。")
            permits.append(permit)

        def after():
            if len(permits) != 1:
                raise FamilyFactAuthorityDenied("SSR timeline read authority missing")
            self._revalidate(permits[0], actor, permits[0].target, self._clock())

        raw = self._projection.education_timeline(
            school_id=actor.school_id,
            source_key=source_key,
            factual_at=now,
            field_names=lambda: fields,
            before_read=before,
            after_read=after,
        )
        if (
            raw.get("fact_kind") != "student_school_relationship"
            or raw.get("source_key") != source_key
            or type(raw.get("segments")) is not list
            or len(raw["segments"]) > 1000
            or type(raw.get("version")) is not int
            or type(raw.get("school_fact_version")) is not int
            or any(
                type(s.get("content")) is not dict or set(s["content"]) != set(fields)
                for s in raw["segments"]
            )
        ):
            raise ValueError("Invalid authorized SSR timeline")
        return raw

    def read_residence_eligibility(self, *, actor, student_source_key, source_key):
        """Read every enrollment source only through existing F3 field/object authority."""
        fields = set(F3_FACT_FIELDS["student_school_relationship"])
        permits = []

        def closed_keys(current):
            school_permit = self._authorize_scope(
                current, "read", "student_school_relationship", source_key, False, self._clock()
            )
            school_keys = None
            school_version = None
            if school_permit.scope.is_school_wide:
                if not fields <= set(school_permit.scope.field_names):
                    raise ValueError("完整住宿影响需要全部在校关系字段读取权限。")
                metadata = self._projection.student_school_relationship_keys(
                    school_id=current.school_id,
                    student_source_key=student_source_key,
                    factual_at=self._clock(),
                    before_read=lambda: self._revalidate(
                        school_permit, current, school_permit.target, self._clock()
                    ),
                    after_read=lambda: self._revalidate(
                        school_permit, current, school_permit.target, self._clock()
                    ),
                )
                school_keys = metadata.get("keys")
                school_version = metadata.get("school_fact_version")
                if (
                    type(school_keys) is not list or len(school_keys) > 100
                    or type(school_version) is not int
                    or len(set(school_keys)) != len(school_keys)
                    or any(type(key) is not str for key in school_keys)
                ):
                    raise ValueError("完整在校关系对象集合无法核实。")
                for key in school_keys:
                    permit = self._authorize_scope(
                        current, "read", "student_school_relationship", key, False, self._clock()
                    )
                    if not permit.scope.is_school_wide or not fields <= set(
                        permit.scope.field_names
                    ):
                        raise ValueError("完整住宿影响需要全部在校关系字段读取权限。")
                    self._revalidate(permit, current, permit.target, self._clock())
            def before():
                permit = self._authorize_scope(
                    current, "read", "student_school_relationship", source_key, False, self._clock()
                )
                if not fields <= set(permit.scope.field_names):
                    raise ValueError("完整住宿影响需要全部在校关系字段读取权限。")
                permits.append(permit)

            def after():
                self._revalidate(permits[-1], current, permits[-1].target, self._clock())

            raw = self._projection.student_relationship_keys(
                school_id=current.school_id,
                student_source_key=student_source_key,
                allowed_keys=lambda: tuple(
                    school_keys if school_keys is not None else permits[-1].scope.object_keys
                ),
                factual_at=self._clock(),
                before_read=before,
                after_read=after,
            )
            keys = raw.get("source_keys")
            if (
                type(raw.get("school_fact_version")) is not int
                or type(keys) is not list
                or len(keys) > 1000
                or len(set(keys)) != len(keys)
                or any(
                    type(key) is not str or not permits[-1].scope.allows_key(key)
                    for key in keys
                )
                or (
                    school_keys is not None
                    and (
                        set(keys) != set(school_keys)
                        or raw.get("school_fact_version") != school_version
                    )
                )
            ):
                raise ValueError("完整在校关系对象集合无法核实。")
            return raw

        initial = closed_keys(actor)
        timelines = tuple(
            self.read_education_timeline(actor=actor, source_key=key)
            for key in initial["source_keys"]
        )
        if any(
            t["school_fact_version"] != initial["school_fact_version"]
            or any(
                s["content"].get("student_source_key") != student_source_key for s in t["segments"]
            )
            for t in timelines
        ):
            raise ValueError("完整在校关系期间或学生归属已变化。")

        def revalidate(current):
            if current != actor or closed_keys(current) != initial:
                raise ValueError("在校关系权限或完整影响已变化，请重新核对。")

        revalidate(actor)
        return dict(
            school_fact_version=initial["school_fact_version"],
            timelines=timelines,
            revalidate=revalidate,
        )

    def _related_target(
        self,
        *,
        actor: StaffActorContext,
        fact_kind: str,
        related_field: str,
        related_ref: str,
    ) -> tuple[str, str, str]:
        related_kind = _RELATED_FIELDS.get(fact_kind, {}).get(related_field)
        if related_kind is None:
            raise ValueError("Unsupported F3 related field")
        canonical_field = related_field[:-4] + "_source_key"
        if related_kind in {"location", "organization"}:
            if self._external_references is None:
                raise ValueError("External reference resolver unavailable")
            source = self._external_references.resolve(
                actor=actor, fact_kind=related_kind, reference=related_ref
            )
        else:
            source = self._source_for(actor, related_kind, related_ref, False)
        return canonical_field, related_kind, source

    def _candidate_facts(
        self,
        *,
        actor: StaffActorContext,
        fact_kind: str,
        sources: tuple[str, ...],
        factual_at: datetime,
        match_field: str | None,
        match_value: str | None,
        limit: int,
        related: tuple[str, str] | None,
        external_related: tuple[str, str, str] | None = None,
        requested_fields: tuple[str, ...] | None = None,
        require_match_permission: bool = False,
        formal_future: bool = False,
    ) -> tuple[tuple[FamilyStaffFact, ...], int]:
        permits: list[StaffFamilyFactPermit] = []
        target_permits: dict[str, StaffFamilyFactPermit] = {}

        def before() -> None:
            for source in sources:
                operation = f"school_facts.f3.{fact_kind}.read"
                selected_scope = next(
                    (
                        binding.scope_id
                        for binding in self._configuration.read_dependency_bindings
                        if binding.assignment_id == actor.assignment_id
                        and binding.operation_code == operation
                        and source in binding.object_keys
                    ),
                    None,
                )
                if selected_scope is None:
                    selected_scope = next(
                        (
                            binding.scope_id
                            for binding in self._configuration.scope_bindings
                            if binding.assignment_id == actor.assignment_id
                            and binding.operation_code == operation
                        ),
                        None,
                    )
                try:
                    shared = next(
                        (
                            p
                            for p in permits
                            if type(p) is StaffFamilyFactPermit
                            and p.actor == actor
                            and p.action == "read"
                            and p.scope.fact_kind == fact_kind
                            and source in p.scope.object_keys
                            and (
                                not self._configuration.read_dependency_bindings
                                or p.scope_id == selected_scope
                            )
                        ),
                        None,
                    )
                    permit = (
                        replace(shared, target=replace(shared.target, source_key=source))
                        if shared is not None
                        else self._authorize_scope(
                            actor, "read", fact_kind, source, False, self._clock()
                        )
                    )
                except FamilyFactAuthorityDenied:
                    continue
                if match_field is None or match_field in permit.scope.field_names:
                    if shared is None:
                        permits.append(permit)
                    target_permits[source] = permit
                elif require_match_permission:
                    raise FamilyFactAuthorityDenied("The selection field is not authorized.")
            if external_related is not None:
                if self.evidence is not None:
                    self.evidence.unsupported()
                external_kind, external_ref, expected_source = external_related
                if (
                    self._external_references is None
                    or self._external_references.resolve(
                        actor=actor, fact_kind=external_kind, reference=external_ref
                    )
                    != expected_source
                ):
                    raise FamilyFactAuthorityDenied("External related authority changed.")
            if related is not None:
                related_kind, related_source = related
                permits.append(
                    self._authorize_scope(
                        actor,
                        "read",
                        related_kind,
                        related_source,
                        False,
                        self._clock(),
                    )
                )

        def groups() -> tuple[tuple[tuple[str, ...], tuple[str, ...]], ...]:
            grouped: dict[tuple[str, ...], list[str]] = {}
            for source, permit in target_permits.items():
                fields = permit.scope.field_names
                if requested_fields is not None:
                    fields = tuple(
                        name for name in fields if name in requested_fields or name == match_field
                    )
                grouped.setdefault(fields, []).append(source)
            return tuple(
                (tuple(group_sources), field_names)
                for field_names, group_sources in grouped.items()
            )

        def after() -> None:
            for permit in permits:
                self._revalidate(permit, actor, permit.target, self._clock())
            if external_related is not None:
                external_kind, external_ref, expected_source = external_related
                if (
                    self._external_references is None
                    or self._external_references.resolve(
                        actor=actor, fact_kind=external_kind, reference=external_ref
                    )
                    != expected_source
                ):
                    raise FamilyFactAuthorityDenied("External related authority changed.")

        rows = self._projection.candidates(
            school_id=actor.school_id,
            fact_kind=fact_kind,
            factual_at=factual_at,
            match_field=match_field,
            match_value=match_value,
            limit=limit + 1,
            source_groups=groups,
            before_read=before,
            after_read=after,
            **({"formal_future": True} if formal_future else {}),
        )
        facts: list[FamilyStaffFact] = []
        for row in rows:
            source = str(row["source_key"])
            permit = target_permits.get(source)
            if permit is None:
                raise ValueError("Candidate escaped authorized source set")
            content = cast(dict[str, object], row["payload"]).get("content")
            if (
                requested_fields is not None
                and isinstance(content, dict)
                and set(cast(dict[str, object], content)) - {*requested_fields, match_field}
            ):
                raise ValueError("F3 candidate projection returned unrequested fields")
            facts.append(
                self._fact_from_raw(
                    actor=actor,
                    fact_kind=fact_kind,
                    source_key=source,
                    creation=False,
                    permit=permit,
                    raw=cast(Mapping[str, object], row["payload"]),
                )
            )
        return tuple(facts), len(target_permits)

    def _school_candidate_facts(self, **kwargs):
        actor = kwargs["actor"]
        fact_kind = kwargs["fact_kind"]
        limit = kwargs["limit"]
        requested_fields = kwargs.get("requested_fields")
        match_field = kwargs.get("match_field")
        required_fields = tuple(
            dict.fromkeys((*(() if requested_fields is None else requested_fields),
                           *(() if match_field is None else (match_field,))))
        )
        facts: list[FamilyStaffFact] = []
        authorized = 0
        saw_keys = False
        filled = False
        for page in self._school_source_pages(actor, fact_kind, required_fields or None):
            if not page:
                continue
            saw_keys = True
            if filled:
                continue
            selected, count = self._candidate_facts(
                **(kwargs | {"sources": page, "limit": max(1, limit - len(facts))})
            )
            facts.extend(selected)
            authorized += count
            if len(facts) > limit:
                filled = True
        return tuple(facts), (1 if not saw_keys else authorized)

    def query(
        self,
        *,
        actor: StaffActorContext,
        fact_kind: str,
        reference: str | None = None,
        query_text: str | None = None,
        student_number: str | None = None,
        related_field: str | None = None,
        related_ref: str | None = None,
        as_of: datetime | None = None,
        limit: int = 20,
        requested_fields: tuple[str, ...] | None = None,
        _allow_formal_future: bool = False,
    ) -> dict[str, object]:
        selections = (
            reference is not None,
            query_text is not None,
            related_field is not None or related_ref is not None,
            student_number is not None,
        )
        if (
            fact_kind not in F3_FACT_KINDS
            or type(limit) is not int
            or not 1 <= limit <= 20
            or sum(selections) > 1
            or (related_field is None) != (related_ref is None)
            or (as_of is not None and as_of.utcoffset() is None)
            or (
                student_number is not None
                and (
                    fact_kind != "student_school_relationship"
                    or type(student_number) is not str
                    or not student_number.strip()
                    or len(student_number) > 1000
                )
            )
            or (
                requested_fields is not None
                and (not requested_fields or not set(requested_fields) <= F3_FACT_FIELDS[fact_kind])
            )
        ):
            raise ValueError("Invalid fixed F3 query")
        factual_at = self._clock() if as_of is None else as_of
        formal_future = (
            _allow_formal_future
            and fact_kind == "student_school_relationship"
            and factual_at > self._clock()
        )
        candidate_facts: tuple[FamilyStaffFact, ...] | None = None
        candidate_authorized = 0
        if reference is not None:
            sources = (self._source_for(actor, fact_kind, reference, False),)
        else:
            sources = tuple(self._configuration.source_catalog.get(fact_kind, ()))
        school_mode = False
        if reference is None and self._authorize_school_scope is not None:
            try:
                school = self._authorize_school_scope(actor, "read", fact_kind, self._clock())
            except FamilyFactAuthorityDenied:
                pass
            else:
                if not school.scope.is_school_wide:
                    raise FamilyFactAuthorityDenied("Current school scope differs.")
                self._revalidate_school_scope(school, actor, self._clock())
                school_mode = True
        candidates = self._school_candidate_facts if school_mode else self._candidate_facts
        if student_number is not None:
            candidate_facts, candidate_authorized = candidates(
                actor=actor,
                fact_kind=fact_kind,
                sources=sources,
                factual_at=factual_at,
                match_field="student_number",
                match_value=student_number,
                limit=limit,
                related=None,
                requested_fields=requested_fields,
                require_match_permission=True,
                formal_future=formal_future,
            )
        elif query_text is not None:
            if fact_kind != "person":
                raise ValueError(
                    "This F3 kind has no authorized formal name; select a returned related ref."
                )
            candidate_facts, candidate_authorized = candidates(
                actor=actor,
                fact_kind=fact_kind,
                sources=sources,
                factual_at=factual_at,
                match_field="display_name",
                match_value=query_text,
                limit=limit,
                related=None,
                external_related=None,
                requested_fields=requested_fields,
                formal_future=formal_future,
            )
        elif related_field is not None and related_ref is not None:
            match_field, related_kind, related_source = self._related_target(
                actor=actor,
                fact_kind=fact_kind,
                related_field=related_field,
                related_ref=related_ref,
            )
            candidate_facts, candidate_authorized = candidates(
                actor=actor,
                fact_kind=fact_kind,
                sources=sources,
                factual_at=factual_at,
                match_field=match_field,
                match_value=related_source,
                limit=limit,
                related=((related_kind, related_source) if related_kind in F3_FACT_KINDS else None),
                external_related=(
                    (related_kind, related_ref, related_source)
                    if related_kind not in F3_FACT_KINDS
                    else None
                ),
                requested_fields=requested_fields,
                formal_future=formal_future,
            )
        elif formal_future:
            candidate_facts, candidate_authorized = candidates(
                actor=actor,
                fact_kind=fact_kind,
                sources=sources,
                factual_at=factual_at,
                match_field=None,
                match_value=None,
                limit=limit,
                related=None,
                requested_fields=requested_fields,
                formal_future=True,
            )
        elif reference is None:
            if school_mode:
                gathered: list[str] = []
                for page in self._school_source_pages(actor, fact_kind, requested_fields):
                    gathered.extend(page)
                sources = tuple(gathered)
            checked_at = self._clock()
            authorized_sources: list[str] = []
            for source in sources:
                try:
                    self._authorize_scope(actor, "read", fact_kind, source, False, checked_at)
                    authorized_sources.append(source)
                except FamilyFactAuthorityDenied:
                    continue
            sources = tuple(authorized_sources)
        selected_count = len(sources) if candidate_facts is None else len(candidate_facts)
        if selected_count > limit:
            return {
                "kind": "f3_staff_facts",
                "status": "ambiguous",
                "fact_kind": fact_kind,
                "records": [],
                "coverage": "partial",
                "has_more": True,
                "truncated": True,
                "queried_at": self._clock().isoformat(),
            }
        facts: list[FamilyStaffFact] = list(candidate_facts or ())
        authorized = candidate_authorized
        if school_mode and not sources and candidate_facts is None:
            authorized = 1
        conflicts = 0
        if candidate_facts is None:
            for source in sources:
                try:
                    row, _, _ = self._read(
                        actor=actor,
                        fact_kind=fact_kind,
                        source_key=source,
                        creation=False,
                        action="read",
                        factual_at=as_of,
                        requested_fields=requested_fields,
                    )
                    authorized += 1
                except FamilyFactAuthorityDenied:
                    continue
                except FamilyFactNotRecorded:
                    authorized += 1
                    continue
                except FamilyFactConflict:
                    authorized += 1
                    conflicts += 1
                    continue
                facts.append(row)
        records: list[dict[str, object]] = [
            {
                "reference": row.reference,
                "label": row.label,
                "content": dict(row.content),
                "version": row.expected_version,
                "factual_at": row.factual_at.isoformat(),
                "valid_from": None if row.valid_from is None else row.valid_from.isoformat(),
                "valid_to": None if row.valid_to is None else row.valid_to.isoformat(),
                "source_reference": row.source_reference,
                "source_name": row.source_name,
                "source_trace_reference": row.source_trace_reference,
                "source_published_at": None
                if row.source_published_at is None
                else row.source_published_at.isoformat(),
            }
            for row in facts
        ]
        status = (
            "conflict"
            if conflicts
            else "known"
            if records
            else "forbidden"
            if not authorized
            else "object_unresolved"
            if query_text is not None
            else "no_current_records"
        )
        return {
            "kind": "f3_staff_facts",
            "status": status,
            "fact_kind": fact_kind,
            "records": records,
            "coverage": "unknown",
            "has_more": False,
            "truncated": False,
            "queried_at": self._clock().isoformat(),
        }

    def describe(self, *, actor: StaffActorContext, fact_kind: str) -> dict[str, object]:
        if fact_kind not in F3_FACT_KINDS:
            raise ValueError("Unknown F3 fact kind")
        return {
            "kind": "f3_staff_fact_description",
            "fact_kind": fact_kind,
            "fields": sorted(
                name[:-11] + "_ref" if name.endswith("_source_key") else name
                for name in F3_FACT_FIELDS[fact_kind]
            ),
            "creation_choices": list(self.creation_choices(actor=actor, fact_kind=fact_kind)),
            "coverage": "unknown",
        }

    def prepare_target(
        self,
        *,
        actor: StaffActorContext,
        fact_kind: str,
        target_ref: str | None,
        creation_ref: str | None,
    ) -> FamilyMaintenanceTarget:
        if fact_kind not in F3_FACT_KINDS or (target_ref is None) == (creation_ref is None):
            raise ValueError("Select one exact F3 target")
        creation = creation_ref is not None
        source = self._source_for(actor, fact_kind, cast(str, creation_ref or target_ref), creation)
        row, _, _ = self._read(
            actor=actor,
            fact_kind=fact_kind,
            source_key=source,
            creation=creation,
            action=(
                "request"
                if creation and fact_kind == "guardian_relationship"
                else "maintain"
                if creation
                else "read"
            ),
        )
        label = row.label
        if fact_kind == "person_region" and not creation:
            label = "区县资料（关联人员姓名未获权读取，请核对人员引用）"
            person_source = row.content.get("person_source_key")
            if isinstance(person_source, str):
                try:
                    person, _, _ = self._read(
                        actor=actor,
                        fact_kind="person",
                        source_key=person_source,
                        creation=False,
                        action="read",
                    )
                except FamilyFactAuthorityDenied:
                    pass
                else:
                    name = person.content.get("display_name")
                    if isinstance(name, str) and name.strip():
                        label = f"{name}的区县资料"
                        number = self._student_number_for_person(actor, person.reference)
                        if number is not None:
                            label = f"{name}（学号{number}）的区县资料"
        return FamilyMaintenanceTarget(
            fact_kind,
            source,
            row.expected_version,
            row.school_fact_version,
            row.content,
            label,
            row.reference,
        )

    def _student_number_for_person(self, actor: StaffActorContext, person_ref: str) -> str | None:
        # Two fixed, bounded relation selections; every read retains its own
        # current object/field authorization. Ambiguous or denied data adds no label.
        if not all(
            self._configuration.source_catalog.get(kind)
            for kind in ("student", "student_school_relationship")
        ):
            return None
        related_ref = person_ref
        for kind, field in (
            ("student", "person_ref"),
            ("student_school_relationship", "student_ref"),
        ):
            try:
                result = self.query(
                    actor=actor,
                    fact_kind=kind,
                    related_field=field,
                    related_ref=related_ref,
                    limit=1,
                )
            except FamilyFactAuthorityDenied:
                return None
            records = result.get("records")
            if (
                result.get("status") != "known"
                or not isinstance(records, list)
                or len(cast(list[object], records)) != 1
            ):
                return None
            record = cast(dict[str, object], records[0])
            if kind == "student":
                reference = record.get("reference")
                if not isinstance(reference, str):
                    return None
                related_ref = reference
            else:
                content = record.get("content")
                number = (
                    cast(dict[str, object], content).get("student_number")
                    if isinstance(content, dict)
                    else None
                )
                return number if isinstance(number, str) and number.strip() else None
        return None

    def creation_choices(
        self, *, actor: StaffActorContext, fact_kind: str
    ) -> tuple[Mapping[str, JsonValue], ...]:
        choices: list[dict[str, JsonValue]] = []
        action = "request" if fact_kind == "guardian_relationship" else "maintain"
        if self._authorize_school_scope is not None and self._revalidate_school_scope is not None:
            try:
                school = self._authorize_school_scope(actor, action, fact_kind, self._clock())
            except FamilyFactAuthorityDenied:
                school = None
            if school is not None and school.scope.is_school_wide:
                from uuid import uuid4

                key = f"live:{fact_kind}:{uuid4()}"
                self._revalidate_school_scope(school, actor, self._clock())
                return ({
                    "creation_ref": self.reference_for(
                        actor=actor, fact_kind=fact_kind, source_key=key,
                        creation=True, school_wide=True,
                    ),
                    "label": "新登记项",
                },)
        for key, label in self._configuration.creation_catalog.get(fact_kind, {}).items():
            try:
                self._authorize_scope(actor, action, fact_kind, key, True, self._clock())
            except FamilyFactAuthorityDenied:
                continue
            choices.append(
                {
                    "creation_ref": self.reference_for(
                        actor=actor, fact_kind=fact_kind, source_key=key, creation=True
                    ),
                    "label": label,
                }
            )
        return tuple(choices)

    def public_field_references(
        self,
        *,
        actor: StaffActorContext,
        fact_kind: str,
        fields: Mapping[str, JsonValue],
    ) -> Mapping[str, JsonValue]:
        if fact_kind not in F3_FACT_KINDS:
            raise ValueError("Unknown F3 fact kind")
        internal = {public[:-4] + "_source_key": kind for public, kind in _REFERENCE_KINDS.items()}
        external = {
            public[:-4] + "_source_key": kind for public, kind in _EXTERNAL_REFERENCE_KINDS.items()
        }
        result: dict[str, JsonValue] = {}
        for key, value in fields.items():
            if not key.endswith("_source_key"):
                result[key] = value
                continue
            if key not in F3_FACT_FIELDS[fact_kind]:
                raise ValueError("Invalid canonical F3 reference field")
            public = key[:-11] + "_ref"
            if value is None:
                result[public] = None
            elif key in internal:
                target_kind = internal[key]
                target_source = cast(str, value)
                permit = self._authorize_scope(
                    actor, "read", target_kind, target_source, False, self._clock()
                )
                result[public] = self.reference_for(
                    actor=actor,
                    fact_kind=target_kind,
                    source_key=target_source,
                    school_wide=permit.scope.is_school_wide,
                )
            elif key in external:
                if self._external_references is None:
                    raise ValueError("External reference projector unavailable")
                result[public] = self._external_references.reference_for(
                    actor=actor,
                    fact_kind=external[key],
                    source_key=cast(str, value),
                )
            else:
                raise ValueError("Unregistered canonical F3 reference field")
        return result

    def resolve_field_references(
        self, *, actor: StaffActorContext, fact_kind: str, fields: Mapping[str, JsonValue]
    ) -> Mapping[str, JsonValue]:
        result: dict[str, JsonValue] = dict(fields)
        if any(k.endswith("_source_key") for k in result):
            raise ValueError("Canonical keys are private")
        for key, value in tuple(fields.items()):
            if not key.endswith("_ref"):
                continue
            canonical = key[:-4] + "_source_key"
            if canonical not in F3_FACT_FIELDS[fact_kind]:
                raise ValueError("Invalid F3 field reference")
            if value is None:
                result[canonical] = None
                del result[key]
                continue
            if key in _EXTERNAL_REFERENCE_KINDS:
                if self._external_references is None:
                    raise ValueError("External reference resolver unavailable")
                source = self._external_references.resolve(
                    actor=actor,
                    fact_kind=_EXTERNAL_REFERENCE_KINDS[key],
                    reference=cast(str, value),
                )
            else:
                kind = _REFERENCE_KINDS.get(key)
                if kind is None:
                    raise ValueError("Unregistered F3 field reference")
                source = self._source_for(actor, kind, cast(str, value), False)
            result[canonical] = source
            del result[key]
        return result


__all__ = ["FamilyStaffFact", "FamilyStaffRepository", "SqlFamilyStaffProjection"]
