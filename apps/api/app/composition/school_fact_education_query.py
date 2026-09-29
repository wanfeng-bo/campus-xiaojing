"""Finite education compositions. Language interpretation belongs to the model, facts do not."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast

from app.composition.school_fact_education_reader import (
    EducationReadProof,
    EducationReadResult,
    EducationReadStale,
    EducationRepository,
    aware,
    timestamp,
)
from app.domains.students.education_time import (
    CalendarRule,
    GradeAnchor,
    GradeResolution,
    clip_calendar_rule,
    resolve_grade,
)
from app.platform.contracts import StaffActorContext
from app.platform.school_facts.f7_coverage_basis import verify_coverage_basis

QUERY_FIELDS = frozenset(
    {"grade", "teachers", "classroom", "positions", "cohorts", "members", "official_contacts"}
)
_PLACEMENT_FIELDS = (
    "class_source_key",
    "calendar_source_key",
    "grade_source_key",
    "campus_source_key",
    "class_number",
)
_CALENDAR_FIELDS = (
    "start_cycle",
    "end_cycle",
    "month",
    "day",
    "timezone_name",
    "grade_source_keys",
)
_ROLE_LABELS = {
    "primary": "班主任",
    "deputy": "副班主任",
    "joint": "共同班主任",
    "acting": "代任老师",
}


def required_timestamp(value: object) -> datetime:
    parsed = timestamp(value)
    if parsed is None:
        raise ValueError("Required education timestamp missing")
    return parsed


def education_publication_evidence(segment: dict[str, Any], *, applies_to: str) -> dict[str, Any]:
    """Only the supplied relation's publication, never proof of joined names or quality."""
    source = segment.get("source") or {}
    label = source.get("summary") or source.get("label")
    return {
        "applies_to": applies_to,
        "basis": {
            "label": label
            if isinstance(label, str) and label
            else ("学校正式发布的登记" if source else None),
            "published_at": source.get("published_at"),
            "recorded_at": segment.get("recorded_at"),
        },
        "effective_at": segment.get("effective_at"),
        "valid_to": segment.get("valid_to"),
    }


def resolve_grade_from_records(
    *,
    at: datetime,
    calendars: tuple[dict[str, Any], ...],
    placement_record: dict[str, Any],
    placement: dict[str, Any],
    known_at: datetime | None = None,
) -> GradeResolution:
    """Pure DTO adapter shared by authorized staff and own-child projections."""
    content = placement["content"]
    rules: list[CalendarRule] = []
    for calendar in calendars:
        for segment in calendar.get("segments", ()):
            c = segment["content"]
            candidate = clip_calendar_rule(
                CalendarRule(
                    rule_id=calendar["source_key"],
                    version=segment["revision"],
                    start_cycle=c["start_cycle"],
                    end_cycle=c.get("end_cycle"),
                    month=c["month"],
                    day=c["day"],
                    timezone=c["timezone_name"],
                    grade_ids=tuple(c["grade_source_keys"]),
                    recorded_at=required_timestamp(segment["recorded_at"]),
                    superseded_at=timestamp(segment.get("superseded_at")),
                ),
                valid_to=timestamp(segment.get("valid_to")),
            )
            if candidate is not None:
                rules.append(candidate)
    origin = GradeAnchor(
        anchor_id=placement_record["source_key"],
        version=placement["revision"],
        effective_from=required_timestamp(placement["effective_at"]),
        grade_id=content["grade_source_key"],
        recorded_at=required_timestamp(placement["recorded_at"]),
        superseded_at=timestamp(placement.get("superseded_at")),
    )
    return resolve_grade(at=at, rules=tuple(rules), anchors=(origin,), known_at=known_at)


def active_segment(record: dict[str, Any], at: datetime) -> dict[str, Any] | None:
    matches = [
        s
        for s in record.get("segments", ())
        if required_timestamp(s["effective_at"]) <= at
        and (timestamp(s.get("valid_to")) is None or at < required_timestamp(s["valid_to"]))
    ]
    if len(matches) > 1:
        raise EducationReadStale("所问时点的教育资料有冲突，需要先核实。")
    return matches[0] if matches else None


@dataclass(frozen=True)
class EducationQueryProof:
    education: EducationReadProof
    external: tuple[Any, ...] = ()

    def revalidate(self, *, actor: StaffActorContext | None = None) -> None:
        from app.composition.school_fact_read_evidence import validation_batch

        # One release only: all related evidence receives fresh post-read checks.
        with validation_batch():
            self.education.revalidate(actor=actor)
            for proof in self.external:
                if (
                    getattr(proof, "school_fact_version", None)
                    != self.education.school_fact_version
                ):
                    raise EducationReadStale("关联资料与教育资料版本不一致，需要重新查询。")
                proof.revalidate()


@dataclass(frozen=True)
class EducationQueryRead:
    """Private acquisition details for the caller's synchronous class resolution."""

    kind: str
    fields: tuple[str, ...]
    source_keys: tuple[str, ...] | None
    filters: tuple[tuple[str, str | tuple[str, ...]], ...]
    as_of: datetime | None
    known_at: datetime | None
    result: EducationReadResult


@dataclass(frozen=True)
class EducationQueryResult:
    public_result: dict[str, Any]
    evidence: tuple[dict[str, Any], ...]
    private_proof: EducationQueryProof
    private_reads: tuple[EducationQueryRead, ...] = ()


class EducationQueryService:
    def __init__(self, repository: EducationRepository, *, external_reader: Any = None) -> None:
        self.repository, self.external_reader = repository, external_reader

    def query(
        self,
        actor: StaffActorContext,
        *,
        class_reference: str | None = None,
        name: str | None = None,
        grade_name: str | None = None,
        campus_name: str | None = None,
        class_number: int | None = None,
        student_ref: str | None = None,
        fields: tuple[str, ...] = ("grade", "teachers"),
        as_of: datetime | None = None,
        known_at: datetime | None = None,
    ) -> EducationQueryResult:
        if not fields:
            raise ValueError("请选择明确的教育查询内容。")
        return self._query(
            actor,
            class_reference=class_reference,
            name=name,
            grade_name=grade_name,
            campus_name=campus_name,
            class_number=class_number,
            student_ref=student_ref,
            fields=fields,
            as_of=as_of,
            known_at=known_at,
        )

    def locate_class(
        self,
        actor: StaffActorContext,
        *,
        class_reference: str | None = None,
        name: str | None = None,
        grade_name: str | None = None,
        campus_name: str | None = None,
        class_number: int | None = None,
        as_of: datetime | None = None,
    ) -> EducationQueryResult:
        """Internal class selection; no unrequested grade/teacher presentation reads."""
        return self._query(
            actor,
            class_reference=class_reference,
            name=name,
            grade_name=grade_name,
            campus_name=campus_name,
            class_number=class_number,
            fields=(),
            as_of=as_of,
        )

    def _query(
        self,
        actor: StaffActorContext,
        *,
        class_reference: str | None = None,
        name: str | None = None,
        grade_name: str | None = None,
        campus_name: str | None = None,
        class_number: int | None = None,
        student_ref: str | None = None,
        fields: tuple[str, ...] = ("grade", "teachers"),
        as_of: datetime | None = None,
        known_at: datetime | None = None,
    ) -> EducationQueryResult:
        if not set(fields) <= QUERY_FIELDS or len(set(fields)) != len(fields):
            raise ValueError("请选择明确的教育查询内容。")
        if class_number is not None and (type(class_number) is not int or class_number < 1):
            raise ValueError("班序必须为正式登记的正整数。")
        at = aware(self.repository.clock() if as_of is None else as_of)
        reads: list[Any] = []
        private_reads: list[EducationQueryRead] = []
        references: dict[tuple[str, str], str] = {}
        external_proofs: list[Any] = []
        evidence: list[dict[str, Any]] = []
        boundary: datetime | None = None
        fact_timezones: set[str] = set()
        read_cache: dict[tuple[Any, ...], tuple[dict[str, Any], ...]] = {}
        external_cache: dict[tuple[Any, ...], dict[str, dict[str, Any]]] = {}

        def read(
            kind: str,
            selected: tuple[str, ...],
            *,
            keys: tuple[str, ...] | None = None,
            filters: dict[str, str | list[str]] | None = None,
        ) -> tuple[dict[str, Any], ...]:
            cache_key = (
                kind,
                tuple(sorted(selected)),
                None if keys is None else tuple(sorted(keys)),
                tuple(
                    sorted(
                        (key, tuple(sorted(value)) if isinstance(value, list) else value)
                        for key, value in (filters or {}).items()
                    )
                ),
            )
            if cache_key in read_cache:
                return read_cache[cache_key]
            result = self.repository.read_records(
                actor,
                kind,
                fields=selected,
                source_keys=keys,
                filter=filters,
                as_of=as_of,
                known_at=known_at,
            )
            reads.append(result)
            private_reads.append(
                EducationQueryRead(kind, selected, keys, cache_key[3], as_of, known_at, result)
            )
            references.update(((kind, key), ref) for key, ref in result.references)
            if kind == "education_calendar_rule":
                for row in result.records:
                    segment = active_segment(row, at)
                    if segment is not None and isinstance(
                        segment["content"].get("timezone_name"), str
                    ):
                        fact_timezones.add(segment["content"]["timezone_name"])
            for record in result.records:
                evidence.append(
                    {
                        "fact_kind": kind,
                        "version": record.get("version"),
                        "source": [s.get("source", {}) for s in record.get("segments", ())],
                    }
                )
            read_cache[cache_key] = result.records
            return result.records

        def external(
            kind: str, keys: tuple[str, ...], selected: tuple[str, ...]
        ) -> dict[str, dict[str, Any]]:
            nonlocal boundary
            if not keys:
                return {}
            cache_key = (kind, tuple(sorted(set(keys))), tuple(sorted(selected)))
            if cache_key in external_cache:
                return external_cache[cache_key]
            if self.external_reader is None:
                raise PermissionError("当前未接通所需关联资料的受控读取。")
            result = self.external_reader.read_records(
                actor, kind, source_keys=tuple(sorted(set(keys))), fields=selected, as_of=at
            )
            proof = result.get("proof")
            if proof is None or not callable(getattr(proof, "revalidate", None)):
                raise PermissionError("关联资料缺少当前读取依据。")
            external_proofs.append(proof)
            boundary_proof = result.get("boundary_proof")
            if boundary_proof is not None:
                external_proofs.append(boundary_proof)
                next_boundary = timestamp(result.get("next_valid_boundary"))
                if next_boundary is not None:
                    boundary = next_boundary if boundary is None else min(boundary, next_boundary)
            rows = result.get("records", ())
            if any(
                r.get("source_key") not in keys or not set(r.get("content", {})) <= set(selected)
                for r in rows
            ):
                raise PermissionError("关联资料超出所请求范围。")
            answer = {r["source_key"]: r.get("content", {}) for r in rows}
            external_cache[cache_key] = answer
            return answer

        def finish(document: dict[str, Any]) -> EducationQueryResult:
            document["fact_timezone_name"] = (
                next(iter(fact_timezones)) if len(fact_timezones) == 1 else None
            )
            document["as_of"] = at.isoformat()
            document["known_at"] = known_at.isoformat() if known_at is not None else None
            proof = EducationQueryProof(
                EducationReadProof.combine(
                    tuple(r.proof for r in reads), next_boundary=boundary if as_of is None else None
                ),
                tuple(external_proofs),
            )
            proof.revalidate(actor=actor)
            return EducationQueryResult(document, tuple(evidence), proof, tuple(private_reads))

        def people_names(owner: str, keys: tuple[str, ...]) -> dict[str, str | None]:
            if not keys:
                return {}
            owners = external(owner, keys, ("person_source_key",))
            person_keys = tuple(
                {
                    c["person_source_key"]
                    for c in owners.values()
                    if isinstance(c.get("person_source_key"), str)
                }
            )
            persons = external("person", person_keys, ("display_name",))
            return {
                key: persons.get(c.get("person_source_key", ""), {}).get("display_name")
                for key, c in owners.items()
            }

        def group(
            items: list[dict[str, Any]],
            *,
            kind: str | None = None,
            records: tuple[dict[str, Any], ...] = (),
            selected_fields: tuple[str, ...] = (),
        ) -> dict[str, Any]:
            result: dict[str, Any] = {
                "status": "known" if items else "not_recorded",
                "items": items,
                "coverage": "unknown",
            }
            quality_reader = getattr(self.external_reader, "quality_for", None)
            if kind is None or quality_reader is None or known_at is not None:
                return result
            quality = quality_reader(
                actor,
                target_kind=kind,
                target_source_keys=tuple(r["source_key"] for r in records),
                field_names=selected_fields,
                owner_kind="education_class",
                owner_source_keys=(class_key,),
                academic_year=grade_cache[class_key][0].cycle if class_key in grade_cache else None,
                as_of=as_of,
                include_target_owner=True,
            )
            if quality.get("proof") is None:
                raise PermissionError("教育质量缺少当前读取依据。")
            external_proofs.append(quality["proof"])
            statements = quality.get("statements", [])
            evidence.extend(statements)
            current_records = tuple(row for row in records if active_segment(row, at) is not None)
            quality_version = quality.get("school_fact_version")
            if (
                type(quality_version) is int
                and all(read.school_fact_version == quality_version for read in reads)
                and len(current_records) == len(items)
            ):
                for row, item in zip(current_records, items, strict=True):
                    for statement in statements:
                        content = statement.get("content", {})
                        field = content.get("field_name")
                        if (
                            content.get("quality_kind") != "coverage"
                            or content.get("coverage_status") != "complete"
                            or (
                                statement.get("applies_to_source_key")
                                or content.get("target_source_key")
                            )
                            != row["source_key"]
                            or field not in selected_fields
                            or not verify_coverage_basis(
                                content.get("coverage_basis"),
                                school_id=actor.school_id,
                                owner_kind="education",
                                owner_source_key=content.get(
                                    "education_target_source_key", class_key
                                ),
                                target_fact_kind=kind,
                                target_source_key=row["source_key"],
                                field_name=field,
                                purpose=None,
                                contact_kind=None,
                                factual_at=at,
                                school_fact_version=quality_version,
                                members=((row["source_key"], row["version"]),),
                            )
                        ):
                            continue
                        item["quality"] = {"record_coverage": "complete"}
                    declarations = []
                    for statement in statements:
                        content = statement.get("content", {})
                        key = statement.get("applies_to_source_key") or content.get(
                            "target_source_key"
                        )
                        field = content.get("field_name")
                        if (
                            key != row["source_key"]
                            or field is not None
                            and field not in selected_fields
                        ):
                            continue
                        # Completeness needs the independent mother-set proof above.
                        if (
                            content.get("quality_kind") == "coverage"
                            and content.get("coverage_status") == "complete"
                        ):
                            continue
                        from app.composition.school_fact_education_product import FIELD_LABELS

                        declaration = {
                            "applies_to": FIELD_LABELS.get(field, "本条记录的已查字段"),
                            "kind": {
                                "source": "来源情况",
                                "dispute": "争议说明",
                                "coverage": "登记范围说明",
                                "review": "复核记录",
                                "verification": "核实记录",
                                "negative": "明确未安排",
                            }.get(content.get("quality_kind"), "资料质量说明"),
                            "basis_reference": content.get("basis_reference"),
                            "verified_at": content.get("verified_at"),
                            "review_due_at": content.get("review_due_at"),
                        }
                        source = content.get("source_status")
                        if source in {"available", "unavailable"}:
                            declaration["status"] = (
                                "来源可用" if source == "available" else "来源暂不可用"
                            )
                        if content.get("dispute_status") not in {None, "resolved"}:
                            declaration["status"] = "这条资料存在尚未解决的争议"
                            item["status"] = "conflict"
                        if content.get("coverage_status") == "partial":
                            declaration["status"] = "本条记录的指定字段登记尚不完整"
                        declarations.append(declaration)
                    if declarations:
                        item["quality_declarations"] = declarations
            general = [
                r["content"]
                for r in statements
                if r["content"].get("target_source_key") is None
                and r.get("applies_to_source_key") is None
            ]

            def matching(field: str, quality_kind: str) -> list[dict[str, Any]]:
                return [
                    c
                    for c in general
                    if c.get("quality_kind") == quality_kind
                    and c.get("field_name") in {None, field}
                ]

            coverage = {
                c.get("coverage_status")
                for field in selected_fields
                for c in matching(field, "coverage")
            }
            # A quality read proof authorizes the statement, not a complete mother set.
            # Until this reader has that separately revalidated proof, retain complete
            # claims only in restricted evidence and never promote a visible subset.
            if coverage == {"partial"} and all(
                matching(field, "coverage") for field in selected_fields
            ):
                result["coverage"] = "partial"
            negatives = {
                c.get("negative_code")
                for field in selected_fields
                for c in matching(field, "negative")
            }
            if len(negatives) == 1 and all(
                matching(field, "negative") for field in selected_fields
            ):
                result["status"] = "conflict" if items else "known_negative"
                code = next(iter(negatives))
                result["notice"] = {
                    "no_class_teacher": "学校已明确登记当前未安排班主任。",
                    "no_fixed_location": "学校已明确登记暂无固定教室。",
                    "vacant_position": "学校已明确登记该岗位空缺。",
                }.get(code or "", "学校已明确登记此范围的否定事实。")
            if any(
                c.get("quality_kind") == "dispute"
                and c.get("dispute_status") not in {None, "resolved"}
                for c in general
            ):
                result["status"] = "conflict"
            result["quality_basis"] = [
                c["basis_reference"]
                for c in general
                if c.get("basis_reference")
                and not (
                    c.get("quality_kind") == "coverage" and c.get("coverage_status") == "complete"
                )
            ]
            return result

        def student_cohorts(key: str) -> dict[str, Any]:
            memberships = read(
                "student_cohort_membership",
                ("student_source_key", "cohort_source_key"),
                filters={"student_source_key": key},
            )
            cohort_keys = tuple(
                sorted(
                    {
                        segment["content"]["cohort_source_key"]
                        for row in memberships
                        if (segment := active_segment(row, at)) is not None
                    }
                )
            )
            rows = (
                read(
                    "education_cohort",
                    ("display_name", "cohort_kind", "academic_year"),
                    keys=cohort_keys,
                )
                if cohort_keys
                else ()
            )
            return group(
                [
                    {
                        "name": segment["content"].get("display_name"),
                        "kind": segment["content"].get("cohort_kind"),
                        "academic_year": segment["content"].get("academic_year"),
                        "publication_evidence": cohort_publication(row, segment, memberships),
                    }
                    for row in rows
                    if (segment := active_segment(row, at)) is not None
                ]
            )

        def cohort_publication(row, segment, memberships):
            return [
                education_publication_evidence(segment, applies_to="届别或批次定义"),
                *(
                    education_publication_evidence(s, applies_to="学生与届别批次关系")
                    for member in memberships
                    if (s := active_segment(member, at)) is not None
                    and s["content"].get("cohort_source_key") == row["source_key"]
                ),
            ]

        arrangement_cache: dict[tuple[str, tuple[str, ...]], dict[str, Any]] = {}

        def school_arrangement(key: str, relation_keys: tuple[str, ...]) -> dict[str, Any]:
            cache_key = key, tuple(sorted(set(relation_keys)))
            if cache_key in arrangement_cache:
                return arrangement_cache[cache_key]
            reader = getattr(self.external_reader, "grade_arrangement", None)
            if reader is None:
                raise PermissionError("缺少在校状态的受控读取。")
            arrangement = reader(
                actor, student_source_key=key, relationship_keys=cache_key[1], as_of=as_of
            )
            proof = arrangement.get("proof")
            if proof is None or not callable(getattr(proof, "revalidate", None)):
                raise PermissionError("缺少在校状态的当前读取依据。")
            external_proofs.append(proof)
            arrangement_cache[cache_key] = arrangement
            return arrangement

        def personal_grade_for(key: str, relation_keys: tuple[str, ...]) -> dict[str, Any] | None:
            nonlocal boundary
            unknown = {"status": "unknown", "name": None, "cycle": None}
            rows = read(
                "student_grade_placement",
                ("student_source_key", "calendar_source_key", "grade_source_key"),
                filters={"student_source_key": key},
            )
            current = [
                (row, segment) for row in rows if (segment := active_segment(row, at)) is not None
            ]
            if len(current) > 1:
                return {**unknown, "status": "conflict"}
            arrangement = school_arrangement(key, relation_keys)
            if known_at is not None:
                # SSR exposes corrected fact history, not a historical knowledge view.
                return unknown
            limit = timestamp(arrangement.get("last_suspended_until"))
            status = arrangement.get("status")
            if status == "suspended" or limit is not None:
                effective = timestamp(arrangement.get("effective_at"))
                limit = (
                    max(t for t in (limit, effective) if t is not None)
                    if (limit or effective)
                    else at
                )
            if status not in {"enrolled", "suspended"}:
                return {**unknown, "status": "conflict" if status == "conflict" else "unknown"}
            if not current:
                return unknown if limit is not None or status != "enrolled" else None
            row, segment = current[0]
            if limit is not None and required_timestamp(segment["effective_at"]) < limit:
                return unknown
            content = segment["content"]
            if not content.get("grade_source_key"):
                return unknown
            calendars = read(
                "education_calendar_rule", _CALENDAR_FIELDS, keys=(content["calendar_source_key"],)
            )
            resolved = resolve_grade_from_records(
                at=at,
                calendars=calendars,
                placement_record=row,
                placement=segment,
                known_at=known_at,
            )
            name = None
            if resolved.grade_id:
                labels = read("education_grade", ("display_name",), keys=(resolved.grade_id,))
                values = [
                    part["content"].get("display_name")
                    for item in labels
                    if (part := active_segment(item, at)) is not None
                ]
                name = values[0] if len(values) == 1 else None
            if resolved.next_boundary is not None:
                boundary = (
                    min(boundary, resolved.next_boundary) if boundary else resolved.next_boundary
                )
            return {"status": resolved.status, "name": name, "cycle": resolved.cycle}

        personal_grade: dict[str, Any] | None = None
        student_key: str | None = None
        class_keys: tuple[str, ...] | None = None
        if class_reference is not None:
            class_keys = (
                self.repository.source_for_reference(actor, "education_class", class_reference),
            )
        if student_ref is not None:
            if self.external_reader is None:
                raise PermissionError("当前未接通学生的受控引用解析。")
            student_key = self.external_reader.source_for_reference(actor, "student", student_ref)
            if not isinstance(student_key, str):
                raise PermissionError("学生引用未解析为获准对象。")
            memberships = read(
                "student_class_membership",
                (
                    "student_source_key",
                    "class_source_key",
                    "membership_status",
                    "school_relationship_source_key",
                ),
                filters={"student_source_key": student_key},
            )
            assigned = [
                s["content"]["class_source_key"]
                for r in memberships
                if (s := active_segment(r, at)) is not None
                and s["content"].get("membership_status") == "assigned"
                and s["content"].get("class_source_key")
            ]
            if class_keys is not None and set(assigned) != set(class_keys):
                return finish(
                    {"status": "not_recorded", "text": "该学生的正式归班不符合所选班级。"}
                )
            if "grade" in fields:
                relation_keys = tuple(
                    sorted(
                        {
                            str(segment["content"]["school_relationship_source_key"])
                            for row in memberships
                            if (segment := active_segment(row, at)) is not None
                            and segment["content"].get("school_relationship_source_key")
                        }
                    )
                )
                personal_grade = personal_grade_for(student_key, relation_keys)
            if len(set(assigned)) != 1:
                status = "not_recorded" if not assigned else "conflict"
                membership_status = (
                    "unassigned"
                    if not assigned
                    and any(
                        (segment := active_segment(row, at)) is not None
                        and segment["content"].get("membership_status") == "unassigned"
                        for row in memberships
                    )
                    else status
                )
                document = {"status": status, "membership_status": membership_status}
                phrases = [
                    "已正式登记待分班"
                    if membership_status == "unassigned"
                    else "尚未明确当前归班"
                    if not assigned
                    else "归班资料有冲突，需要先核实"
                ]
                for field in fields:
                    if field == "grade":
                        document[field] = personal_grade or {
                            "status": "unknown",
                            "name": None,
                            "cycle": None,
                        }
                        phrases.append(document[field]["name"] or "个人年级安排尚未明确")
                    elif field == "cohorts":
                        document[field] = student_cohorts(student_key)
                    else:
                        document[field] = {"status": status, "items": [], "coverage": "unknown"}
                document["text"] = "；".join(phrases)
                return finish(document)
            class_keys = tuple(set(assigned))
        classes = read("education_class", ("display_name",), keys=class_keys)
        candidates = [
            (r, s)
            for r in classes
            if (s := active_segment(r, at)) is not None
            and (name is None or s["content"].get("display_name") == name)
        ]
        placements: tuple[dict[str, Any], ...] = ()
        grade_cache: dict[str, tuple[GradeResolution, str | None, dict[str, Any] | None]] = {}

        def placement_for(class_key: str):
            rows = placements or read(
                "class_placement", _PLACEMENT_FIELDS, filters={"class_source_key": class_key}
            )
            selected = [
                (r, s)
                for r in rows
                if (s := active_segment(r, at)) is not None
                and s["content"].get("class_source_key") == class_key
            ]
            return selected

        def grade_for(class_key: str) -> tuple[GradeResolution, str | None, dict[str, Any] | None]:
            nonlocal boundary
            if class_key in grade_cache:
                return grade_cache[class_key]
            selected = placement_for(class_key)
            if len(selected) != 1:
                answer = (
                    GradeResolution(
                        "unknown" if not selected else "conflict",
                        None,
                        None,
                        None,
                        None,
                        (),
                        "missing_origin",
                    ),
                    None,
                    None,
                )
                grade_cache[class_key] = answer
                return answer
            row, placement = selected[0]
            content = placement["content"]
            calendars = read(
                "education_calendar_rule", _CALENDAR_FIELDS, keys=(content["calendar_source_key"],)
            )
            resolution = resolve_grade_from_records(
                at=at,
                calendars=calendars,
                placement_record=row,
                placement=placement,
                known_at=known_at,
            )
            label = None
            if resolution.grade_id:
                grade_rows = read("education_grade", ("display_name",), keys=(resolution.grade_id,))
                grade_labels = [
                    s["content"].get("display_name")
                    for r in grade_rows
                    if (s := active_segment(r, at)) is not None
                ]
                label = grade_labels[0] if len(grade_labels) == 1 else None
            if resolution.next_boundary is not None:
                boundary = (
                    min(boundary, resolution.next_boundary)
                    if boundary
                    else resolution.next_boundary
                )
            answer = resolution, label, content
            grade_cache[class_key] = answer
            return answer

        if grade_name is not None or campus_name is not None or class_number is not None:
            placements = read("class_placement", _PLACEMENT_FIELDS)
            located: list[tuple[dict[str, Any], dict[str, Any]]] = []
            for candidate in candidates:
                if grade_name is not None:
                    _, label, placement = grade_for(candidate[0]["source_key"])
                    if label != grade_name:
                        continue
                else:
                    selected = placement_for(candidate[0]["source_key"])
                    placement = selected[0][1]["content"] if len(selected) == 1 else None
                if placement is None:
                    continue
                if class_number is not None and placement.get("class_number") != class_number:
                    continue
                if campus_name is not None:
                    campus_key = placement.get("campus_source_key")
                    campus = (
                        external("location", (campus_key,), ("display_name",)) if campus_key else {}
                    )
                    if campus.get(campus_key or "", {}).get("display_name") != campus_name:
                        continue
                located.append(candidate)
            candidates = located
        if not candidates:
            return finish(
                {"status": "not_recorded", "text": "没有找到符合这些条件的正式班级登记。"}
            )
        if len(candidates) > 1:
            # Only authorized locator facts help distinguish candidates. Teacher
            # relations remain unread until the user has selected a single class.
            locator_fields: dict[str, dict[str, Any]] = {}
            for row, _ in candidates:
                try:
                    _, grade_label, placement = grade_for(row["source_key"])
                    if placement is None:
                        continue
                    campus_key = placement.get("campus_source_key")
                    campus = (
                        external("location", (campus_key,), ("display_name",)) if campus_key else {}
                    )
                    locator_fields[row["source_key"]] = {
                        "grade_name": grade_label,
                        "class_number": placement.get("class_number"),
                        "campus_name": campus.get(campus_key, {}).get("display_name"),
                    }
                except PermissionError:
                    # A name-only grant still permits ambiguity, never a guessed
                    # coordinate or an implicit permission expansion.
                    continue
            return finish(
                {
                    "status": "ambiguous",
                    "text": "有多个符合条件的班级，请选定一个。",
                    "candidates": [
                        {
                            "reference": references[("education_class", r["source_key"])],
                            "name": s["content"]["display_name"],
                            **locator_fields.get(r["source_key"], {}),
                        }
                        for r, s in candidates
                    ],
                }
            )
        class_row, class_segment = candidates[0]
        class_key, class_name = class_row["source_key"], class_segment["content"]["display_name"]
        document: dict[str, Any] = {
            "status": "known",
            "name": class_name,
            "reference": references[("education_class", class_key)],
        }
        phrases = [class_name]
        if "grade" in fields:
            if personal_grade is not None:
                document["grade"] = personal_grade
            else:
                resolved, grade_label, placement = grade_for(class_key)
                document["grade"] = {
                    "status": resolved.status,
                    "name": grade_label,
                    "cycle": resolved.cycle,
                }
            phrases.append(
                document["grade"]["name"] or "个人年级安排尚未明确"
                if student_key
                else document["grade"]["name"] or "年级安排尚未明确"
            )
        if "teachers" in fields or "official_contacts" in fields:
            teachers = read(
                "class_teacher_assignment",
                ("class_source_key", "assignment_source_key", "teacher_role", "duty_scope"),
                filters={"class_source_key": class_key},
            )
            current_teachers = [
                s["content"] for r in teachers if (s := active_segment(r, at)) is not None
            ]
            teacher_names = (
                people_names(
                    "assignment", tuple(c["assignment_source_key"] for c in current_teachers)
                )
                if "teachers" in fields
                else {}
            )
            teacher_items: list[dict[str, Any]] = []
            for row in teachers:
                segment = active_segment(row, at)
                if segment is None:
                    continue
                c = segment["content"]
                label = teacher_names.get(c["assignment_source_key"])
                item = {
                    "role": _ROLE_LABELS.get(c["teacher_role"], "带班老师"),
                    "name": label,
                    "publication_evidence": [
                        education_publication_evidence(segment, applies_to="班主任任职关系")
                    ],
                }
                if c.get("duty_scope"):
                    item["duty_scope"] = c["duty_scope"]
                teacher_items.append(item)
            if "teachers" in fields:
                document["teachers"] = group(
                    teacher_items,
                    kind="class_teacher_assignment",
                    records=teachers,
                    selected_fields=("teacher_role", "assignment_source_key", "duty_scope"),
                )
                phrases.extend(
                    [f"{t['role']}：{t['name'] or '姓名未获准读取'}" for t in teacher_items]
                    or ["暂没有当前班主任登记"]
                )
            if "official_contacts" in fields:
                contact_reader = getattr(self.external_reader, "official_contacts", None)
                if contact_reader is None:
                    document["official_contacts"] = {
                        "status": "unavailable",
                        "items": [],
                        "message": "当前未获准查询可用联系渠道。",
                    }
                else:
                    contacts = contact_reader(
                        actor,
                        assignment_keys=tuple(c["assignment_source_key"] for c in current_teachers),
                        as_of=at,
                    )
                    if contacts.get("proof") is None:
                        raise PermissionError("联系渠道缺少当前读取依据。")
                    external_proofs.append(contacts["proof"])
                    document["official_contacts"] = group(contacts.get("records", []))
        if "classroom" in fields:
            room_rows = read(
                "class_location",
                ("class_source_key", "location_source_key", "location_purpose"),
                filters={"class_source_key": class_key},
            )
            rooms = [s["content"] for r in room_rows if (s := active_segment(r, at)) is not None]
            room_names = external(
                "location", tuple(c["location_source_key"] for c in rooms), ("display_name",)
            )
            document["classroom"] = group(
                [
                    {
                        "name": room_names.get(c["location_source_key"], {}).get("display_name"),
                        "purpose": "主要教室"
                        if segment["content"]["location_purpose"] == "main_classroom"
                        else "其他固定地点",
                        "publication_evidence": [
                            education_publication_evidence(segment, applies_to="班级固定地点安排")
                        ],
                    }
                    for row in room_rows
                    if (segment := active_segment(row, at)) is not None
                    for c in (segment["content"],)
                ],
                kind="class_location",
                records=room_rows,
                selected_fields=("location_source_key", "location_purpose"),
            )
            phrases.extend(
                [
                    f"{item['purpose']}：{item['name'] or '名称未获准读取'}"
                    for item in document["classroom"]["items"]
                ]
                or ["暂没有当前固定教室登记"]
            )
        if "positions" in fields:
            positions = read(
                "student_class_position",
                ("class_source_key", "student_source_key", "position_source_key"),
                filters={
                    "class_source_key": class_key,
                    **({"student_source_key": student_key} if student_key else {}),
                },
            )
            holders = [s["content"] for r in positions if (s := active_segment(r, at)) is not None]
            position_names: dict[str, Any] = {}
            if holders:
                definitions = read(
                    "class_position_definition",
                    ("class_source_key", "display_name"),
                    keys=tuple(sorted({c["position_source_key"] for c in holders})),
                    filters={"class_source_key": class_key},
                )
                position_names = {
                    r["source_key"]: s["content"].get("display_name")
                    for r in definitions
                    if (s := active_segment(r, at)) is not None
                }
            names = people_names("student", tuple(c["student_source_key"] for c in holders))
            document["positions"] = group(
                [
                    {
                        "position": position_names.get(c["position_source_key"]),
                        "name": names.get(c["student_source_key"]),
                        "publication_evidence": [
                            education_publication_evidence(segment, applies_to="班内学生职务关系")
                        ],
                    }
                    for row in positions
                    if (segment := active_segment(row, at)) is not None
                    for c in (segment["content"],)
                ],
                kind="student_class_position",
                records=positions,
                selected_fields=("student_source_key", "position_source_key"),
            )
            phrases.extend(
                [
                    f"{item['position'] or '已登记职务'}：{item['name'] or '姓名未获准读取'}"
                    for item in document["positions"]["items"]
                ]
                or ["暂没有当前学生职务登记"]
            )
        if "members" in fields or "cohorts" in fields:
            member_rows = read(
                "student_class_membership",
                ("class_source_key", "student_source_key", "membership_status")
                + (("school_relationship_source_key",) if "members" in fields else ()),
                filters={"class_source_key": class_key},
            )
            student_keys = tuple(
                sorted(
                    {
                        s["content"]["student_source_key"]
                        for r in member_rows
                        if (s := active_segment(r, at)) is not None
                        and s["content"].get("membership_status") == "assigned"
                    }
                )
            )
            if "members" in fields:
                names = people_names("student", student_keys)
                member_items: list[dict[str, Any]] = []
                for key in student_keys:
                    relation_keys = tuple(
                        sorted(
                            {
                                s["content"]["school_relationship_source_key"]
                                for r in member_rows
                                if (s := active_segment(r, at)) is not None
                                and s["content"].get("student_source_key") == key
                                and s["content"].get("school_relationship_source_key")
                            }
                        )
                    )
                    arrangement = school_arrangement(key, relation_keys)
                    state = (
                        str(arrangement.get("status") or "unknown")
                        if known_at is None
                        else "unknown"
                    )
                    labels = {
                        "enrolled": "正常在班",
                        "suspended": "休学，保留原班关联",
                        "graduated": "已毕业，班关联待核实",
                        "transferred_out": "已转出，班关联待核实",
                        "conflict": "在校状态有冲突",
                    }
                    member_items.append(
                        {
                            "name": names.get(key),
                            "enrollment_status": state if state in labels else None,
                            "status_label": labels.get(state, "在校状态未明确"),
                            "publication_evidence": [
                                education_publication_evidence(s, applies_to="学生与行政班关系")
                                for r in member_rows
                                if (s := active_segment(r, at)) is not None
                                and s["content"].get("student_source_key") == key
                                and s["content"].get("membership_status") == "assigned"
                            ],
                        }
                    )
                document["members"] = group(
                    member_items,
                    kind="student_class_membership",
                    records=member_rows,
                    selected_fields=("student_source_key", "membership_status"),
                )
                phrases.append(f"当前获准范围内登记{len(student_keys)}名成员")
            if "cohorts" in fields:
                cohort_items: list[dict[str, Any]] = []
                target_students = (student_key,) if student_key else student_keys
                if target_students:
                    memberships = read(
                        "student_cohort_membership",
                        ("student_source_key", "cohort_source_key"),
                        filters={"student_source_key": list(target_students)},
                    )
                    cohort_keys = tuple(
                        sorted(
                            {
                                s["content"]["cohort_source_key"]
                                for r in memberships
                                if (s := active_segment(r, at)) is not None
                            }
                        )
                    )
                    if cohort_keys:
                        cohorts = read(
                            "education_cohort", ("display_name", "cohort_kind"), keys=cohort_keys
                        )
                        cohort_types = {
                            "original_intake": "原学段入学批次",
                            "school_transfer_intake": "转入本校批次",
                            "expected_graduation": "预计毕业分组",
                        }
                        cohort_items = [
                            {
                                "name": s["content"].get("display_name"),
                                "kind": cohort_types.get(
                                    s["content"].get("cohort_kind"), "批次口径待明确"
                                ),
                                "publication_evidence": cohort_publication(r, s, memberships),
                            }
                            for r in cohorts
                            if (s := active_segment(r, at)) is not None
                        ]
                document["cohorts"] = group(cohort_items)
                phrases.extend(
                    [f"{item['kind']}：{item['name']}" for item in cohort_items]
                    or ["暂没有当前届别或批次登记"]
                )
        phrases.extend(
            str(cast(dict[str, Any], value)["notice"])
            for value in document.values()
            if isinstance(value, dict) and "notice" in value
        )
        document["text"] = "，".join(phrases) + "。"
        return finish(document)
