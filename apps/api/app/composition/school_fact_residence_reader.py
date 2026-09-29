"""F6 fixed Reader functions and exact scoped repository. No model-chosen SQL."""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import text

from app.composition.school_fact_education_reader import aware, timestamp
from app.composition.school_fact_family_query import SqlFamilyReadRepository
from app.platform.contracts import StaffActorContext
from app.platform.school_facts.f6_policy import F6_FACT_KINDS
from app.platform.school_facts.school_scope_reference import (
    decode_school_scope_reference,
    encode_school_scope_reference,
    is_school_scope_reference,
    school_scope_reference_coordinates,
)


class SqlResidenceProjection:
    def __init__(self, sessions: Any) -> None:
        self.sessions = sessions

    def current_version(self, school_id: Any) -> Any:
        with self.sessions() as session:
            value = session.scalar(
                text("SELECT app.school_fact_current_version(:school)"), {"school": school_id}
            )
        if type(value) is not int or value < 0:
            raise ValueError("Invalid residence version")
        return value

    def scope_keys(
        self,
        *,
        school_id: Any,
        fact_kind: str,
        after_key: str | None,
        limit: int,
        family: str = "f6",
    ):
        with self.sessions() as session:
            return session.scalar(
                text("SELECT app.school_fact_scope_keys(:school,:family,:kind,:after,:limit)"),
                {
                    "school": school_id,
                    "family": family,
                    "kind": fact_kind,
                    "after": after_key,
                    "limit": limit,
                },
            )

    def student_impact_keys(self, *, school_id: Any, student_source_key: str, as_of: Any):
        with self.sessions() as session:
            return session.scalar(
                text("SELECT app.school_fact_f6_student_impact_keys(:school,:student,:at)"),
                {"school": school_id, "student": student_source_key, "at": as_of},
            )

    def quality_coordinates(self, school_id: Any, source_key: Any) -> Any:
        with self.sessions() as session:
            raw: dict[str, Any] = session.scalar(
                text("SELECT app.school_fact_f6_quality_coordinates(:school,:key)"),
                {"school": school_id, "key": source_key},
            )
        if type(raw) is not dict:
            raise ValueError("Invalid quality coordinates")
        return raw

    def quality_detail(self, school_id: Any, source_key: Any) -> Any:
        with self.sessions() as session:
            raw: dict[str, Any] = session.scalar(
                text("SELECT app.school_fact_f6_quality_detail(:school,:key)"),
                {"school": school_id, "key": source_key},
            )
        if type(raw) is not dict:
            raise ValueError("Invalid quality detail")
        return raw

    def exact_coverage(
        self, *, school_id: Any, fact_kind: str, source_key: str, as_of: Any
    ) -> list[dict[str, Any]]:
        with self.sessions() as session:
            raw = session.scalar(
                text(
                    "SELECT app.school_fact_f7_residence_exact_coverage(:school,:kind,:target,:at)"
                ),
                {"school": school_id, "kind": fact_kind, "target": source_key, "at": as_of},
            )
        if type(raw) is not list or len(raw) > 100 or any(type(item) is not dict for item in raw):
            raise ValueError("Invalid exact residence coverage projection")
        return raw

    def read_for_maintenance(
        self, *, school_id: Any, fact_kind: Any, source_key: Any, fields: Any, creation: Any
    ) -> Any:
        with self.sessions() as session:
            raw: dict[str, Any] = session.scalar(
                text(
                    "SELECT app.school_fact_f6_maintenance_target(:school,:kind,:"
                    "key,:fields,:creation)"
                ),
                {
                    "school": school_id,
                    "kind": fact_kind,
                    "key": source_key,
                    "fields": list(fields),
                    "creation": creation,
                },
            )
        if type(raw) is not dict:
            raise ValueError("Invalid maintenance projection")
        return raw

    def read_student_impacts(
        self, *, school_id: Any, student_source_key: Any, source_keys: Any, as_of: Any
    ) -> Any:
        with self.sessions() as session:
            raw: dict[str, Any] = session.scalar(
                text("SELECT app.school_fact_f6_student_impacts(:school,:student,:objects,:at)"),
                {
                    "school": school_id,
                    "student": student_source_key,
                    "objects": list(source_keys),
                    "at": as_of,
                },
            )
        if type(raw) is not dict:
            raise ValueError("Invalid student residence impact projection")
        return raw

    def read(
        self,
        *,
        school_id: Any,
        fact_kind: Any,
        source_keys: Any,
        fields: Any,
        as_of: Any,
        name: Any,
        related: Any,
        offset: Any,
        limit: Any,
    ) -> Any:
        with self.sessions() as session:
            raw: dict[str, Any] = session.scalar(
                text(
                    "SELECT app.school_fact_f6_targets(:school,:kind,:objects,:fi"
                    "elds,:at,:name,:related_kind,:related_key,:offset,:limit)"
                ),
                {
                    "school": school_id,
                    "kind": fact_kind,
                    "objects": list(source_keys),
                    "fields": list(fields),
                    "at": as_of,
                    "name": name,
                    "related_kind": related[0] if related else None,
                    "related_key": related[1] if related else None,
                    "offset": offset,
                    "limit": limit,
                },
            )
        if type(raw) is not dict:
            raise ValueError("Invalid residence projection")
        return raw


class SqlFamilyResidenceRepository(SqlFamilyReadRepository):
    def exact_coverage(
        self, actor: Any, *, student_id: Any, fact_kind: str, source_key: str, as_of: Any
    ) -> list[dict[str, Any]]:
        before = self.authority(actor)
        if before is None or before.student_id != student_id:
            raise PermissionError("不是当前获准孩子的寄宿资料。")
        with self.sessions() as session:
            raw = session.scalar(
                text(
                    "SELECT app.school_fact_f7_residence_exact_coverage(:school,:kind,:target,:at)"
                ),
                {"school": actor.school_id, "kind": fact_kind, "target": source_key, "at": as_of},
            )
        if self.authority(actor) != before:
            raise PermissionError("家校关系已变化。")
        if type(raw) is not list or len(raw) > 100 or any(type(item) is not dict for item in raw):
            raise ValueError("Invalid selected-child coverage projection")
        return raw

    def residence_read(self, actor: Any, *, as_of: Any) -> Any:
        with self.sessions() as session:
            raw: dict[str, Any] = session.scalar(
                text(
                    "SELECT app.school_fact_family_residence(:school,:adult,:relation,:version,:at)"
                ),
                self._parameters(actor) | {"at": as_of},
            )
        if type(raw) is not dict:
            raise ValueError("Invalid family residence projection")
        return raw

    def residence_stamp(self, actor: Any) -> Any:
        with self.sessions() as session:
            return session.scalar(
                text(
                    "SELECT app.school_fact_family_residence_stamp(:school,:adult"
                    ",:relation,:version)"
                ),
                self._parameters(actor),
            )


@dataclass(frozen=True)
class ResidenceReadProof:
    repository: Any
    actor: StaffActorContext
    permit: Any
    school_fact_version: int
    observed_at: datetime
    next_boundary: datetime | None
    additional_permits: tuple[Any, ...] = ()
    related_check: Any = None
    time_proof: Any = None

    def revalidate(self, *, actor: Any = None) -> Any:
        current = self.actor if actor is None else actor
        if current != self.actor:
            raise PermissionError("当前身份已变化。")
        now = self.repository.clock()
        self.repository.authority.revalidate(self.permit, actor=current, checked_at=now)
        for permit in self.additional_permits:
            self.repository.authority.revalidate(permit, actor=current, checked_at=now)
        if self.related_check is not None and self.related_check() is not True:
            raise PermissionError("关联对象权限已变化。")
        if self.time_proof is not None:
            self.time_proof.revalidate(actor=current)
        if (
            now < self.observed_at
            or self.next_boundary is not None
            and now >= self.next_boundary
            or self.repository.current_version(current.school_id) != self.school_fact_version
        ):
            raise RuntimeError("寄宿资料或适用时点已变化，请重新查询。")


@dataclass(frozen=True)
class ResidenceReadResult:
    status: str
    records: tuple[dict[str, Any], ...]
    as_of: datetime
    school_fact_version: int
    has_more: bool
    proof: ResidenceReadProof

    def public_document(self) -> Any:
        return {
            "status": self.status,
            "records": list(self.records),
            "as_of": self.as_of.isoformat(),
            "school_fact_version": self.school_fact_version,
        }


@dataclass(frozen=True)
class ResidencePageContext:
    scope_hash: str
    expires: datetime | None
    proof: ResidenceReadProof


class ResidenceRepository:
    def __init__(
        self,
        *,
        authority: Any,
        projection: Any,
        reference_key: bytes,
        resolve_related: Any = None,
        related_reference_for: Any = None,
        clock: Any = lambda: datetime.now(UTC),
    ) -> None:
        if type(reference_key) is not bytes or len(reference_key) < 16:
            raise ValueError("Private F6 reference key required")
        self.authority, self.projection, self.clock, self.resolve_related = (
            authority,
            projection,
            clock,
            resolve_related,
        )
        self._key = hmac.digest(reference_key, b"f6-references-v1", "sha256")
        self.external_reference_for = related_reference_for

    def related_reference_for(self, actor: Any, kind: Any, key: Any) -> Any:
        if kind in F6_FACT_KINDS:
            return self.reference_for(actor, kind, key)
        if self.external_reference_for is None:
            raise PermissionError("关联对象读取未获准。")
        return self.external_reference_for(actor, kind, key)

    def _reference(self, actor: Any, kind: Any, key: Any, creation: Any) -> Any:
        prefix = "f6create" if creation else "f6"
        body = f"{prefix}:{actor.school_id}:{actor.assignment_id}:{kind}:{key}"
        return f"{prefix}:{kind}:" + hmac.digest(self._key, body.encode(), "sha256").hex()

    def reference_for(
        self, actor: Any, fact_kind: Any, source_key: Any, creation: Any = False
    ) -> Any:
        permit = self.authority.require_scope(
            actor,
            fact_kind=fact_kind,
            action="maintain" if creation else "read",
            checked_at=self.clock(),
        )
        self.authority.require_object(permit, source_key, is_creation=creation)
        if permit.scope.is_school_wide:
            return encode_school_scope_reference(
                secret=self._key,
                family="f6",
                school_id=actor.school_id,
                assignment_id=actor.assignment_id,
                fact_kind=fact_kind,
                source_key=source_key,
                creation=creation,
            )
        return self._reference(actor, fact_kind, source_key, creation)

    def source_for_reference(
        self, actor: Any, fact_kind: Any, reference: Any, creation: Any = False
    ) -> Any:
        permit = self.authority.require_scope(
            actor,
            fact_kind=fact_kind,
            action="maintain" if creation else "read",
            checked_at=self.clock(),
        )
        if permit.scope.is_school_wide and is_school_scope_reference(reference):
            key = decode_school_scope_reference(
                reference,
                secret=self._key,
                family="f6",
                school_id=actor.school_id,
                assignment_id=actor.assignment_id,
                fact_kind=fact_kind,
                creation=creation,
            )
            self.authority.require_object(permit, key, is_creation=creation)
            return key
        keys = permit.scope.creation_keys if creation else permit.scope.readable_keys
        for key in keys:
            if hmac.compare_digest(reference, self._reference(actor, fact_kind, key, creation)):
                return key
        raise PermissionError("引用不属于当前获准对象。")

    def creation_receipt_reference_for(self, *, actor: Any, fact_kind: str, source_key: str) -> str:
        """Writer-verified original creation metadata only; never a fact selector."""
        reference = self.reference_for(actor, fact_kind, source_key, creation=True)
        if is_school_scope_reference(reference):
            return "f6audit:" + reference
        return reference.replace("f6create:", "f6audit:", 1)

    def _school_keys(self, actor: Any, fact_kind: str, permit: Any):
        """Enumerate only source metadata, under current authority and one fact version."""
        version = self.projection.current_version(actor.school_id)
        keys: list[str] = []
        after = None
        while True:
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
                raise RuntimeError("学校资料枚举期间已变化。")
            selected = page["keys"]
            if any(type(key) is not str or not permit.scope.allows_key(key) for key in selected):
                raise ValueError("Invalid school source metadata")
            if (
                selected != sorted(set(selected))
                or selected
                and after is not None
                and selected[0] <= after
            ):
                raise ValueError("Invalid school source ordering")
            more = page["has_more"]
            expected_after = selected[-1] if more and selected else None
            if page.get("next_after") != expected_after or more and not selected:
                raise ValueError("Invalid school source continuation")
            keys.extend(selected)
            if not more:
                break
            after = expected_after
        self.authority.revalidate(permit, actor=actor, checked_at=self.clock())
        if self.projection.current_version(actor.school_id) != version:
            raise RuntimeError("学校资料枚举期间已变化。")
        return tuple(keys), version

    def _school_read(self, actor: Any, permit: Any, keys: Any, version: int, **kwargs):
        rows: list[Any] = []
        boundaries: list[datetime] = []
        offset, limit = kwargs.pop("offset"), kwargs.pop("limit")
        for start in range(0, max(len(keys), 1), 100):
            self.authority.revalidate(permit, actor=actor, checked_at=self.clock())
            batch = keys[start : start + 100]
            raw = self.projection.read(
                school_id=actor.school_id, source_keys=batch, offset=0, limit=100, **kwargs
            )
            if (
                type(raw) is not dict
                or raw.get("school_fact_version") != version
                or type(raw.get("records")) is not list
                or len(raw["records"]) > 100
                or raw.get("has_more") is not False
                or raw.get("status") not in {"available", "no_current_records", "ambiguous"}
                or any(
                    type(row) is not dict or row.get("source_key") not in batch
                    for row in raw["records"]
                )
            ):
                raise RuntimeError("寄宿资料批次无法完整核实。")
            rows.extend(raw["records"])
            boundary = timestamp(raw.get("next_boundary"))
            if boundary is not None:
                boundaries.append(boundary)
        if len({row["source_key"] for row in rows}) != len(rows):
            raise ValueError("Repeated residence source")
        rows.sort(key=lambda row: row["source_key"])
        self.authority.revalidate(permit, actor=actor, checked_at=self.clock())
        if self.projection.current_version(actor.school_id) != version:
            raise RuntimeError("学校资料读取期间已变化。")
        selected = rows[offset : offset + limit]
        return dict(
            records=selected,
            school_fact_version=version,
            has_more=offset + limit < len(rows),
            status="ambiguous"
            if kwargs.get("name") is not None and len(rows) > 1
            else "available"
            if selected
            else "no_current_records",
            next_boundary=min(boundaries).isoformat() if boundaries else None,
        )

    def catalog_creation_references(self, actor: Any, fact_kind: Any) -> Any:
        permit = self.authority.require_scope(
            actor, fact_kind=fact_kind, action="maintain", checked_at=self.clock()
        )
        if permit.scope.is_school_wide:
            self.authority.require_fields(permit, tuple(permit.scope.field_names))
            key = f"live:{fact_kind}:{uuid4().hex}"
            reference = self.reference_for(actor, fact_kind, key, creation=True)
            self.authority.revalidate(permit, actor=actor, checked_at=self.clock())
            return ({"reference": reference, "label": "新建寄宿资料"},)
        catalog = self.authority.configuration.creation_catalog.get(fact_kind, {})
        return tuple(
            {
                "reference": self._reference(actor, fact_kind, key, True),
                "label": catalog.get(key, "获准新建对象"),
            }
            for key in permit.scope.creation_keys
        )

    def current_version(self, school_id: Any) -> Any:
        return self.projection.current_version(school_id)

    def exact_coverage(
        self,
        actor: Any,
        *,
        fact_kind: str,
        source_key: str,
        as_of: Any,
        read_proof: ResidenceReadProof,
    ) -> list[dict[str, Any]]:
        if read_proof.actor != actor or read_proof.permit.scope.fact_kind != fact_kind:
            raise PermissionError("寄宿质量目标缺少原读取依据。")
        self.authority.require_object(read_proof.permit, source_key, is_creation=False)
        read_proof.revalidate(actor=actor)
        exact = getattr(self.projection, "exact_coverage", None)
        if exact is None:
            return []
        rows = exact(
            school_id=actor.school_id,
            fact_kind=fact_kind,
            source_key=source_key,
            as_of=as_of,
        )
        read_proof.revalidate(actor=actor)
        return rows

    def read_page_context(self, actor: Any, *, as_of: Any = None) -> ResidencePageContext:
        """Metadata only; exact F6 grants and the fixed Reader's next boundary."""
        permits: list[Any] = []
        now = self.clock()
        for kind in sorted(F6_FACT_KINDS):
            try:
                permit = self.authority.require_scope(
                    actor, fact_kind=kind, action="read", checked_at=now
                )
            except PermissionError:
                continue
            permits.append(permit)
        if not permits:
            raise PermissionError("当前没有获准的寄宿依据对象范围。")
        first = permits[0]
        metadata = self.read(
            actor,
            first.scope.fact_kind,
            source_keys=(),
            fields=(first.scope.field_names[0],),
            as_of=as_of,
            limit=1,
        )
        if metadata.records or metadata.has_more:
            raise ValueError("Unexpected F6 page metadata")
        boundary = metadata.proof.next_boundary
        for permit in permits:
            until = getattr(
                getattr(getattr(permit, "basis", None), "governance", None), "valid_until", None
            )
            if until is not None:
                boundary = min(boundary, until) if boundary else until
        proof = ResidenceReadProof(
            self, actor, first, metadata.school_fact_version, now, boundary, tuple(permits[1:])
        )
        proof.revalidate()
        scope_hash = hashlib.sha256(
            json.dumps(
                {p.scope.fact_kind: p.scope.document_sha256 for p in permits}, sort_keys=True
            ).encode()
        ).hexdigest()
        return ResidencePageContext(scope_hash, boundary, proof)

    def read_for_maintenance(
        self,
        actor: Any,
        fact_kind: Any,
        source_key: Any,
        creation: Any = False,
        required_fields: Any = None,
        required_maintenance_fields: Any = None,
    ) -> Any:
        now = self.clock()
        maintain = self.authority.require_scope(
            actor, fact_kind=fact_kind, action="maintain", checked_at=now
        )
        self.authority.require_object(maintain, source_key, is_creation=creation)
        if required_maintenance_fields is not None:
            self.authority.require_fields(maintain, required_maintenance_fields)
        permits = ()
        if creation:
            fields = tuple(maintain.scope.field_names)
        else:
            read = self.authority.require_scope(
                actor, fact_kind=fact_kind, action="read", checked_at=now
            )
            self.authority.require_object(read, source_key, is_creation=False)
            if required_fields is not None:
                self.authority.require_fields(read, required_fields)
            fields = tuple(read.scope.field_names)
            self.authority.require_fields(read, fields)
            permits = (read,)
        raw: dict[str, Any] = self.projection.read_for_maintenance(
            school_id=actor.school_id,
            fact_kind=fact_kind,
            source_key=source_key,
            fields=fields,
            creation=creation,
        )
        if (
            type(raw.get("school_fact_version")) is not int
            or type(raw.get("records")) is not list
            or len(raw["records"]) != 1
        ):
            raise ValueError("Invalid maintenance timeline")
        row: dict[str, Any] = raw["records"][0]
        if (
            type(row) is not dict
            or row.get("source_key") != source_key
            or row.get("fact_kind") != fact_kind
            or type(row.get("version")) is not int
            or type(row.get("segments")) is not list
            or len(row["segments"]) > 1000
            or creation
            and (row["version"] != 0 or row["segments"])
        ):
            raise ValueError("Unexpected maintenance target")
        segments: list[dict[str, Any]] = row["segments"]
        for segment in segments:
            if (
                type(segment) is not dict
                or type(segment.get("content")) is not dict
                or set(segment["content"]) - set(fields)
            ):
                raise ValueError("Unrequested maintenance timeline field")
            start = timestamp(segment["effective_at"])
            if start is None:
                raise ValueError("Missing timeline start")
            aware(start)
            if segment.get("valid_to") is not None:
                end = timestamp(segment["valid_to"])
                if end is None:
                    raise ValueError("Invalid timeline end")
                aware(end)
        proof = ResidenceReadProof(
            self, actor, maintain, raw["school_fact_version"], now, None, permits
        )
        proof.revalidate(actor=actor)
        return ResidenceReadResult(
            "available", (row,), now, raw["school_fact_version"], False, proof
        )

    def read_student_impacts(
        self, actor: Any, student_source_key: Any, *, as_of: Any = None
    ) -> Any:
        now = self.clock()
        at = aware(as_of or now)
        kind = "student_bed_residence"
        maintain = self.authority.require_scope(
            actor, fact_kind=kind, action="maintain", checked_at=now
        )
        read = self.authority.require_scope(actor, fact_kind=kind, action="read", checked_at=now)
        fields = ("student_source_key", "bed_source_key", "effective_at", "valid_to")
        self.authority.require_fields(read, fields)
        self.authority.require_fields(maintain, ("effective_at", "valid_to"))
        if read.scope.is_school_wide or maintain.scope.is_school_wide:
            before_version = self.projection.current_version(actor.school_id)
            metadata = self.projection.student_impact_keys(
                school_id=actor.school_id, student_source_key=student_source_key, as_of=at
            )
            if (
                type(metadata) is not dict
                or metadata.get("school_fact_version") != before_version
                or type(metadata.get("keys")) is not list
                or len(metadata["keys"]) > 100
            ):
                raise ValueError("寄宿关联影响范围无法完整核实。")
            keys = tuple(metadata["keys"])
            if len(keys) != len(set(keys)):
                raise ValueError("重复的寄宿关联对象。")
            for key in keys:
                self.authority.require_object(read, key, is_creation=False)
                self.authority.require_object(maintain, key, is_creation=False)
        else:
            keys = tuple(sorted(set(read.scope.readable_keys) & set(maintain.scope.object_keys)))
            before_version = None
        raw: dict[str, Any] = self.projection.read_student_impacts(
            school_id=actor.school_id,
            student_source_key=student_source_key,
            source_keys=keys,
            as_of=at,
        )
        if (
            type(raw.get("school_fact_version")) is not int
            or type(raw.get("records")) is not list
            or len(raw["records"]) > 100
            or before_version is not None
            and raw["school_fact_version"] != before_version
        ):
            raise ValueError("Invalid residence impact result")
        for row in raw["records"]:
            if (
                row.get("source_key") not in keys
                or row.get("student_source_key") != student_source_key
                or row.get("fact_kind") != kind
            ):
                raise ValueError("Unrequested student impact")
        proof = ResidenceReadProof(
            self,
            actor,
            maintain,
            raw["school_fact_version"],
            now,
            timestamp(raw.get("next_boundary")),
            (read,),
        )
        proof.revalidate(actor=actor)
        return ResidenceReadResult(
            "available", tuple(raw["records"]), at, raw["school_fact_version"], False, proof
        )

    def read(
        self,
        actor: Any,
        fact_kind: Any,
        *,
        as_of: Any = None,
        source_keys: Any = None,
        fields: Any = None,
        name: Any = None,
        related_ref: Any = None,
        offset: Any = 0,
        limit: Any = 25,
    ) -> Any:
        if type(actor) is not StaffActorContext or fact_kind not in F6_FACT_KINDS:
            raise PermissionError("当前身份或资料类型未获准。")
        now = self.clock()
        at = aware(as_of or now)
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Invalid finite residence page")
        if name is not None and (type(name) is not str or not name.strip() or len(name) > 200):
            raise ValueError("Exact residence name required")
        permit = self.authority.require_scope(
            actor, fact_kind=fact_kind, action="read", checked_at=now
        )
        if offset > 10000 and (not permit.scope.is_school_wide or source_keys is not None):
            raise ValueError("Invalid finite residence page")
        requested = tuple(permit.scope.field_names if fields is None else fields)
        self.authority.require_fields(permit, requested)
        name_field = (
            "student_name"
            if fact_kind == "student_bed_residence"
            else "room_name"
            if fact_kind == "dorm_room"
            else "display_name"
        )
        if name is not None:
            self.authority.require_fields(permit, (name_field,))
        school_version = None
        if permit.scope.is_school_wide and source_keys is None:
            keys, school_version = self._school_keys(actor, fact_kind, permit)
        else:
            keys = tuple(permit.scope.readable_keys if source_keys is None else source_keys)
            if len(keys) > 100 or len(set(keys)) != len(keys):
                raise ValueError("Exact bounded unique residence selection required")
            for key in keys:
                self.authority.require_object(permit, key, is_creation=False)
        related = None
        additional_permits = ()
        related_check = None
        if related_ref is not None:
            parts = related_ref.split(":", 2)
            coordinate = school_scope_reference_coordinates(related_ref)
            if coordinate and coordinate[0] == "f6" and not coordinate[2]:
                parts = ["f6", coordinate[1], related_ref]
            if len(parts) == 3 and parts[0] == "f6" and parts[1] in F6_FACT_KINDS:
                related = (parts[1], self.source_for_reference(actor, parts[1], related_ref))
                related_permit = self.authority.require_scope(
                    actor, fact_kind=parts[1], action="read", checked_at=self.clock()
                )
                self.authority.require_object(related_permit, related[1], is_creation=False)
                additional_permits = (related_permit,)
            elif self.resolve_related is not None:
                related = self.resolve_related(actor, related_ref)
                selected_relation = related

                def validate_related() -> bool:
                    return self.resolve_related(actor, related_ref) == selected_relation

                related_check = validate_related
            else:
                raise PermissionError("关联对象引用尚未获准。")
            if not related or related[0] not in {
                "student",
                "location",
                "dorm_bed",
                "dorm_room",
                "dormitory_building_profile",
            }:
                raise ValueError("Unsupported direct residence relation")
            if related[0] in {"dorm_room", "dormitory_building_profile"}:
                location_field = (
                    "room_source_key" if related[0] == "dorm_room" else "location_source_key"
                )
                own = self.read(
                    actor,
                    related[0],
                    as_of=at,
                    source_keys=(related[1],),
                    fields=(location_field,),
                    limit=1,
                )
                if len(own.records) != 1 or not own.records[0].get(location_field):
                    raise PermissionError("关联地点在所问时点尚不可核实。")
                related = ("location", own.records[0][location_field])
                additional_permits = (*additional_permits, own.proof.permit)
        arguments = dict(
            school_id=actor.school_id,
            fact_kind=fact_kind,
            source_keys=keys,
            fields=requested,
            as_of=at,
            name=name,
            related=related,
            offset=offset,
            limit=limit,
        )
        raw: dict[str, Any]
        if school_version is None:
            raw = self.projection.read(**arguments)
        else:
            arguments.pop("school_id")
            arguments.pop("source_keys")
            raw = self._school_read(actor, permit, keys, school_version, **arguments)
        self.authority.revalidate(permit, actor=actor, checked_at=self.clock())
        if (
            type(raw.get("school_fact_version")) is not int
            or raw.get("status")
            not in {"available", "no_current_records", "conflict", "partial", "ambiguous"}
            or type(raw.get("records")) is not list
            or len(raw["records"]) > limit
        ):
            raise ValueError("Invalid residence query payload")
        records: list[Any] = []
        originals: list[dict[str, Any]] = raw["records"]
        for original in originals:
            if (
                type(original) is not dict
                or original.get("source_key") not in keys
                or original.get("fact_kind") != fact_kind
                or type(original.get("content")) is not dict
                or set(original["content"]) - set(requested)
            ):
                raise ValueError("Unrequested residence fields or objects")
            row = dict(original)
            content = row.pop("content")
            row.update(content)
            row["fields"] = content
            records.append(row)
        boundary = timestamp(raw.get("next_boundary")) if as_of is None else None
        proof = ResidenceReadProof(
            self,
            actor,
            permit,
            raw["school_fact_version"],
            now,
            boundary,
            additional_permits,
            related_check,
        )
        proof.revalidate(actor=actor)
        return ResidenceReadResult(
            raw["status"],
            tuple(records),
            at,
            raw["school_fact_version"],
            bool(raw.get("has_more")),
            proof,
        )
