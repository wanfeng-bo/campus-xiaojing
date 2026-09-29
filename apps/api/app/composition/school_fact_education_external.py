"""Education-owned finite joins through the retained field-projected Readers."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from app.composition.school_fact_education_query import education_publication_evidence
from app.composition.school_fact_education_reader import EducationReadStale
from app.composition.school_fact_family_authority import FamilyFactAuthorityDenied
from app.composition.school_fact_family_staff_reader import (
    FamilyFactConflict,
    FamilyFactNotRecorded,
)
from app.platform.assistant.contracts import JsonObject
from app.platform.authorization.f2_reader_compiler import F2_DEFINITIONS
from app.platform.authorization.secure_reader_contracts import (
    SecureReadAuthorizationDrift,
    SecureReadDenied,
)
from app.platform.contracts import StaffActorContext
from app.platform.school_facts.school_scope_reference import (
    school_scope_reference_coordinates,
)

EXTERNAL_KINDS = frozenset(
    {
        "person",
        "student",
        "student_school_relationship",
        "assignment",
        "location",
        "organization",
        "official_contact",
    }
)


class EducationRelatedReadIncomplete(RuntimeError):
    """A required joined fact failed; this says nothing about the parent collection."""

    def __init__(
        self, status: Literal["not_recorded", "conflict"], kind: str, fields: tuple[str, ...]
    ) -> None:
        self.status, self.kind, self.fields = status, kind, fields
        super().__init__("Required related fact is incomplete")

    def public_result(self) -> JsonObject:
        return {
            "status": "incomplete",
            "related_fact": {
                "status": self.status,
                "fact_kind": self.kind,
                "fields": list(self.fields),
            },
            "message": (
                "所需关联资料在所问时点没有登记，尚不能完整核实本次查询。"
                if self.status == "not_recorded"
                else "所需关联资料存在冲突，尚不能完整核实本次查询。"
            ),
        }


@contextmanager
def _related_read_errors(kind: str, fields: tuple[str, ...]) -> Iterator[None]:
    """Translate the existing Reader contracts at the education-owned join boundary."""
    try:
        yield
    except SecureReadAuthorizationDrift:
        raise EducationReadStale("关联资料读取期间授权已变化。") from None
    except (FamilyFactAuthorityDenied, SecureReadDenied):
        raise PermissionError("当前身份没有所需关联资料的读取权限。") from None
    except FamilyFactNotRecorded:
        raise EducationRelatedReadIncomplete("not_recorded", kind, fields) from None
    except FamilyFactConflict:
        raise EducationRelatedReadIncomplete("conflict", kind, fields) from None
    except ValueError:
        # These are Reader/projection failures after selection was validated,
        # not an unsupported user selector.
        raise RuntimeError("关联资料的受控读取结果无法核实。") from None


@dataclass(frozen=True)
class ExternalReadProof:
    actor: StaffActorContext
    proofs: tuple[Any, ...]

    @property
    def school_fact_version(self) -> int | None:
        versions = {getattr(p, "version", None) for p in self.proofs}
        if len(versions) != 1:
            raise EducationReadStale("关联资料在查询期间已变化，请重新查询。")
        return next(iter(versions))

    def revalidate(self) -> None:
        from app.composition.school_fact_read_evidence import validation_batch

        with validation_batch():
            if any(not proof.check(self.actor) for proof in self.proofs):
                raise EducationReadStale("当前身份或关联资料已变化，请重新核对。")


@dataclass(frozen=True)
class ArrangementReadProof:
    school_fact_version: int
    expected: dict[str, Any]
    reread: Callable[[], dict[str, Any]]
    release_key: object | None = None

    def revalidate(self) -> None:
        from app.composition.school_fact_read_evidence import validate_fallback_read

        def current() -> bool:
            if self.reread() != self.expected:
                raise EducationReadStale("在校安排或当前读取依据已变化。")
            return True

        # The owner may identify identical captures of the full same selection,
        # identity and result. Every release still performs the authorized reread.
        # Owners without that complete contract remain isolated by proof identity.
        validate_fallback_read(
            (ArrangementReadProof, id(self) if self.release_key is None else self.release_key),
            current,
        )


@dataclass(frozen=True)
class ExternalBoundaryProof:
    school_fact_version: int
    proofs: tuple[Any, ...]

    def revalidate(self):
        for proof in self.proofs:
            if proof.school_fact_version != self.school_fact_version:
                raise EducationReadStale("External boundary versions changed")
            proof.revalidate()


class SqlEducationExternalBoundaries:
    """Dates only, after exact current grants from the configured fact owner."""

    def __init__(self, *, sessions: Any, guard: Any, family: Any = None):
        self.sessions, self.guard, self.family = sessions, guard, family

    def read(self, actor, kind, keys, at, *, organizations=()):
        from sqlalchemy import text

        from app.platform.school_facts.f2_policy import F2FactScope

        @_related_read_errors(kind, ())
        def current():
            permits = []
            family_permits = []
            family_keys = {
                key
                for key in (keys or ())
                if kind == "person"
                and self.family is not None
                and key in self.family.configuration.source_catalog.get("person", ())
            }
            selected = tuple(keys or ())
            directory_keys = tuple(key for key in selected if key not in family_keys)
            if not family_keys or directory_keys:
                permit = self.guard.require_current(actor, kind, purpose="read")
                scope = F2FactScope.from_reference(permit.scope_plan.reference)
                if scope.school_id != actor.school_id or scope.fact_kind != kind:
                    raise PermissionError("External metadata scope mismatch")
                if keys is None:
                    selected = scope.object_keys
                elif not set(directory_keys) <= set(scope.object_keys):
                    raise PermissionError("External metadata objects exceed exact scope")
                permits.append(permit)
            for source in sorted(family_keys):
                # The catalog chooses the owner only. It cannot supply a permit,
                # broaden its fields, or fall back to F2 after an F3 denial.
                family_permits.append(
                    self.family.authority.require_scope(
                        actor, "read", "person", source, False, self.family.repository._clock()
                    )
                )
            if organizations:
                organization = self.guard.require_current(actor, "organization", purpose="read")
                oscope = F2FactScope.from_reference(organization.scope_plan.reference)
                if (
                    oscope.school_id != actor.school_id
                    or oscope.fact_kind != "organization"
                    or not set(organizations) <= set(oscope.object_keys)
                ):
                    raise PermissionError("Contact organization metadata exceeds exact scope")
                permits.append(organization)

            def revalidate():
                for permit in permits:
                    self.guard.revalidate(permit, actor)
                for permit in family_permits:
                    self.family.authority.revalidate_permit(
                        permit, actor, permit.target, self.family.repository._clock()
                    )

            revalidate()
            with self.sessions() as session:
                raw = session.scalar(
                    text(
                        "SELECT app.school_fact_f5_external_boundary("
                        ":school,:kind,CAST(:keys AS text[]),CAST(:orgs AS text[]),:at)"
                    ),
                    {
                        "school": actor.school_id,
                        "kind": kind,
                        "keys": list(selected),
                        "orgs": list(organizations),
                        "at": at,
                    },
                )
            if (
                type(raw) is not dict
                or set(raw) != {"school_fact_version", "next_valid_boundary"}
                or type(raw["school_fact_version"]) is not int
            ):
                raise PermissionError("External boundary metadata unavailable")
            revalidate()
            return raw

        raw = current()
        return {**raw, "proof": ArrangementReadProof(raw["school_fact_version"], raw, current)}


class EducationExternalReader:
    def __init__(
        self,
        *,
        directory: Any,
        evidence: Any,
        family: Any = None,
        quality_bridge: Any = None,
        boundary_reader: Any = None,
    ) -> None:
        self.directory, self.evidence, self.family = directory, evidence, family
        self.quality_for = quality_bridge.quality_for if quality_bridge is not None else None
        self.boundary_reader = boundary_reader

    def _family_kind(self, kind: str, key: str) -> bool:
        if self.family is None:
            return False
        if kind in {"student", "student_school_relationship"}:
            return True
        return key in self.family.configuration.source_catalog.get(kind, ())

    def reference_for(
        self, *, actor: StaffActorContext, fact_kind: str, source_key: str, creation: bool = False
    ) -> str:
        if fact_kind not in EXTERNAL_KINDS or creation:
            raise ValueError("请选择已正式登记的关联对象。")
        repository = (
            self.family.repository if self._family_kind(fact_kind, source_key) else self.directory
        )
        return repository.reference_for(actor=actor, fact_kind=fact_kind, source_key=source_key)

    def source_for_reference(
        self, actor: StaffActorContext, fact_kind: str, reference: str, creation: bool = False
    ) -> str:
        if fact_kind not in EXTERNAL_KINDS or creation:
            raise ValueError("请选择已正式登记的关联对象。")
        coordinate = school_scope_reference_coordinates(reference)
        if (
            reference.startswith("f3:")
            or coordinate is not None and coordinate[0] == "f3" and not coordinate[2]
        ) and self.family is not None:
            kind, source = self.family.repository.source_for_reference(actor, reference)
            if kind != fact_kind:
                raise ValueError("关联对象类型不一致。")
        else:
            source = self.directory.source_for(
                actor=actor, fact_kind=fact_kind, reference=reference
            )
        return source

    def _canonical_fields(
        self, actor: StaffActorContext, kind: str, content: dict[str, Any]
    ) -> dict[str, Any]:
        refs = {
            name.removesuffix("_source_key") + "_ref": (name, target)
            for name, target, _ in F2_DEFINITIONS[kind].refs
        }
        result: dict[str, Any] = {}
        for field, value in content.items():
            if field in refs:
                name, target = refs[field]
                result[name] = (
                    None
                    if value is None
                    else self.directory.source_for(actor=actor, fact_kind=target, reference=value)
                )
            else:
                result[field] = value
        return result

    def read_records(
        self,
        actor: StaffActorContext,
        kind: str,
        *,
        source_keys: tuple[str, ...],
        fields: tuple[str, ...],
        as_of: datetime | None,
        include_boundaries: bool = True,
    ) -> dict[str, Any]:
        if (
            kind not in EXTERNAL_KINDS
            or not source_keys
            or len(set(source_keys)) != len(source_keys)
            or len(source_keys) > 100
            or not fields
            or len(set(fields)) != len(fields)
        ):
            raise ValueError("关联查询须有明确对象和必要字段。")
        rows: list[dict[str, Any]] = []
        proofs: list[Any] = []
        for source in source_keys:
            if self._family_kind(kind, source):
                repository = self.family.repository
                with (
                    _related_read_errors(kind, fields),
                    repository.evidence.capture(
                        actor=actor, current_time=as_of is None
                    ) as captured,
                ):
                    fact, _, _ = repository._read(
                        actor=actor,
                        fact_kind=kind,
                        source_key=source,
                        creation=False,
                        action="read",
                        factual_at=as_of,
                        requested_fields=fields,
                    )
                    proof = captured.seal()
                if set(fact.content) != set(fields):
                    raise PermissionError("当前身份未获准所需关联字段。")
                if fact.valid_from is not None:
                    rows.append({"source_key": source, "content": dict(fact.content)})
            else:
                if kind not in F2_DEFINITIONS:
                    raise PermissionError("当前没有该关联资料的获准读取范围。")
                with (
                    _related_read_errors(kind, fields),
                    self.evidence.capture(actor=actor, current_time=as_of is None) as captured,
                ):
                    result = self.directory.query(
                        actor=actor,
                        fact_kind=kind,
                        reference=self.directory.reference_for(
                            actor=actor, fact_kind=kind, source_key=source
                        ),
                        factual_at=as_of,
                        content_fields=fields,
                        page_size=2,
                    )
                    proof = captured.seal()
                if result.status == "forbidden":
                    raise PermissionError("当前身份没有所需关联资料的读取权限。")
                if result.has_more:
                    raise RuntimeError("关联资料未完整核实。")
                if result.status in {"no_current_records", "conflict"}:
                    raise EducationRelatedReadIncomplete(
                        "not_recorded" if result.status == "no_current_records" else "conflict",
                        kind,
                        fields,
                    )
                if result.status != "known":
                    raise RuntimeError("关联资料暂时无法核实。")
                for record in result.records:
                    content = self._canonical_fields(actor, kind, dict(record.content))
                    if set(content) != set(fields):
                        raise PermissionError("关联读取字段不完整或超出范围。")
                    rows.append({"source_key": source, "content": content})
            if proof is None:
                raise PermissionError("关联查询的有效依据无法核实。")
            proofs.append(proof)
        guard = ExternalReadProof(actor, tuple(proofs))
        guard.revalidate()
        result = {"records": rows, "proof": guard}
        if (
            include_boundaries
            and as_of is not None
            and self.boundary_reader is not None
            and kind in {"person", "assignment", "organization"}
        ):
            with _related_read_errors(kind, fields):
                metadata = self.boundary_reader.read(actor, kind, source_keys, as_of)
            result["next_valid_boundary"] = metadata["next_valid_boundary"]
            result["boundary_proof"] = metadata["proof"]
        return result

    def grade_arrangement(
        self,
        actor: StaffActorContext,
        *,
        student_source_key: str,
        relationship_keys: tuple[str, ...] = (),
        as_of: datetime | None = None,
    ) -> dict[str, Any]:
        """Finite existing SSR projection; full fresh reread on release, never a grant cache."""
        if (
            self.family is None
            or len(relationship_keys) > 20
            or len(set(relationship_keys)) != len(relationship_keys)
        ):
            raise PermissionError("在校安排需要获准的有限来源。")
        repository = self.family.repository
        from app.composition.school_fact_education_reader import timestamp

        @_related_read_errors(
            "student_school_relationship", ("student_source_key", "enrollment_status", "valid_to")
        )
        def read_current() -> dict[str, Any]:
            at = repository._clock() if as_of is None else as_of
            if at.utcoffset() is None:
                raise ValueError("事实时间必须带时区。")
            version = repository.evidence.version(actor)
            keys = relationship_keys
            if not keys:
                selected = repository.query(
                    actor=actor,
                    fact_kind="student_school_relationship",
                    related_field="student_ref",
                    related_ref=repository.reference_for(
                        actor=actor, fact_kind="student", source_key=student_source_key
                    ),
                    as_of=at,
                    limit=20,
                    requested_fields=("student_source_key",),
                )
                if selected.get("status") == "forbidden":
                    raise PermissionError("无权核验该学生的在校安排。")
                if (
                    selected.get("truncated")
                    or selected.get("has_more")
                    or selected.get("status") not in {"known", "no_current_records"}
                ):
                    raise ValueError("在校安排集合尚未完整核验。")
                keys = tuple(
                    repository.source_for_reference(actor, row["reference"])[1]
                    for row in selected["records"]
                )
            periods: list[tuple[datetime, datetime | None, str]] = []
            source_versions: list[tuple[str, int]] = []
            fields = ("student_source_key", "enrollment_status", "valid_to")
            for key in keys:
                permits: list[Any] = []

                def before(key: str = key, permits: list[Any] = permits):
                    permit = repository._authorize_scope(
                        actor,
                        "read",
                        "student_school_relationship",
                        key,
                        False,
                        repository._clock(),
                    )
                    if not set(fields) <= set(permit.scope.field_names):
                        raise PermissionError("个人年级需要明确在校状态和期间读取权。")
                    permits.append(permit)

                def after(permits: list[Any] = permits):
                    if len(permits) != 1:
                        raise PermissionError("在校安排前置授权缺失。")
                    repository._revalidate(
                        permits[0], actor, permits[0].target, repository._clock()
                    )

                raw = repository._projection.education_timeline(
                    school_id=actor.school_id,
                    source_key=key,
                    factual_at=at,
                    field_names=lambda: fields,
                    before_read=before,
                    after_read=after,
                )
                if (
                    raw.get("source_key") != key
                    or raw.get("fact_kind") != "student_school_relationship"
                    or raw.get("school_fact_version") != version
                    or not isinstance(raw.get("segments"), list)
                    or len(raw["segments"]) > 1000
                ):
                    raise PermissionError("在校安排坐标或版本不一致。")
                source_versions.append((key, raw["version"]))
                for segment in raw["segments"]:
                    content = segment.get("content", {})
                    if (
                        set(content) != set(fields)
                        or content["student_source_key"] != student_source_key
                    ):
                        raise PermissionError("在校安排投影超出该学生及字段范围。")
                    start, end = (
                        timestamp(segment.get("effective_at")),
                        timestamp(segment.get("valid_to")),
                    )
                    if start is None:
                        raise ValueError("在校安排缺少正式时间。")
                    periods.append((start, end, content["enrollment_status"]))
            current = [p for p in periods if p[0] <= at and (p[1] is None or at < p[1])]
            ended = [
                p[1] for p in periods if p[2] == "suspended" and p[1] is not None and p[1] <= at
            ]
            if repository.evidence.version(actor) != version:
                raise EducationReadStale("在校安排查询期间学校事实已变化。")
            return {
                "status": current[0][2]
                if len(current) == 1
                else "conflict"
                if current
                else "unknown",
                "effective_at": current[0][0].isoformat() if len(current) == 1 else None,
                "last_suspended_until": max(ended).isoformat() if ended else None,
                "school_fact_version": version,
                "source_versions": tuple(sorted(source_versions)),
            }

        data = read_current()
        return {
            **data,
            "proof": ArrangementReadProof(
                data["school_fact_version"], data, read_current,
                (
                    id(self), id(repository), actor, student_source_key,
                    relationship_keys, as_of, tuple(sorted(data.items())),
                ) if type(actor) is StaffActorContext else None,
            ),
        }

    def official_contacts(
        self, actor: StaffActorContext, *, assignment_keys: tuple[str, ...], as_of: datetime | None
    ) -> dict[str, Any]:
        assignments = self.read_records(
            actor,
            "assignment",
            source_keys=assignment_keys,
            fields=("organization_source_key",),
            as_of=as_of,
        )
        boundary_reads = [assignments] if assignments.get("boundary_proof") is not None else []
        organizations = sorted(
            {
                r["content"]["organization_source_key"]
                for r in assignments["records"]
                if r["content"].get("organization_source_key")
            }
        )
        rows: list[dict[str, Any]] = []
        proofs: list[Any] = list(assignments["proof"].proofs)
        for key in organizations:
            name = self.read_records(
                actor, "organization", source_keys=(key,), fields=("display_name",), as_of=as_of
            )
            proofs.extend(name["proof"].proofs)
            if name.get("boundary_proof") is not None:
                boundary_reads.append(name)
            names = name["records"]
            label = names[0]["content"]["display_name"] if len(names) == 1 else None
            reference = self.directory.reference_for(
                actor=actor, fact_kind="organization", source_key=key
            )
            with (
                _related_read_errors(
                    "official_contact", ("contact_kind", "contact_value", "service_hours")
                ),
                self.evidence.capture(actor=actor, current_time=as_of is None) as captured,
            ):
                result = self.directory.query(
                    actor=actor,
                    fact_kind="official_contact",
                    related_ref=reference,
                    factual_at=as_of,
                    content_fields=("contact_kind", "contact_value", "service_hours"),
                    page_size=100,
                )
                proof = captured.seal()
            if result.status == "forbidden":
                raise PermissionError("当前身份没有所需官方渠道的读取权限。")
            if (
                result.status not in {"known", "no_current_records"}
                or result.has_more
                or proof is None
            ):
                raise RuntimeError("官方渠道范围未完整核实，请缩小查询。")
            proofs.append(proof)
            rows.extend(
                {
                    "owner_name": label,
                    "owner_kind": "老师所属部门的官方渠道",
                    "contact_kind": r.content.get("contact_kind"),
                    "contact_value": r.content.get("contact_value"),
                    "service_hours": r.content.get("service_hours"),
                    "publication_evidence": [
                        education_publication_evidence(
                            {
                                "source": {
                                    "label": "学校正式发布的官方联系登记",
                                    "published_at": r.published_at.isoformat(),
                                },
                                "effective_at": r.valid_from.isoformat(),
                                "valid_to": r.valid_to.isoformat() if r.valid_to else None,
                            },
                            applies_to="官方联系渠道",
                        )
                    ],
                }
                for r in result.records
            )
        proof = ExternalReadProof(actor, tuple(proofs))
        proof.revalidate()
        result = {"records": rows, "proof": proof}
        if as_of is not None and self.boundary_reader is not None and organizations:
            with _related_read_errors(
                "official_contact", ("contact_kind", "contact_value", "service_hours")
            ):
                metadata = self.boundary_reader.read(
                    actor, "official_contact", None, as_of, organizations=tuple(organizations)
                )
            boundary_reads.append(
                {
                    "next_valid_boundary": metadata["next_valid_boundary"],
                    "boundary_proof": metadata["proof"],
                }
            )
        if boundary_reads:
            from app.composition.school_fact_education_reader import timestamp

            result["next_valid_boundary"] = min(
                (
                    timestamp(item["next_valid_boundary"])
                    for item in boundary_reads
                    if item.get("next_valid_boundary")
                ),
                default=None,
            )
            result["boundary_proof"] = ExternalBoundaryProof(
                proof.school_fact_version, tuple(item["boundary_proof"] for item in boundary_reads)
            )
        return result
