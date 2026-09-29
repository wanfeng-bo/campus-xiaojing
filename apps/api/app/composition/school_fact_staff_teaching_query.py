"""Staff teaching joins over current exact grants and bounded relationship projections."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any, cast

from sqlalchemy import text

from app.composition.school_fact_education_query import (
    EducationQueryProof,
    EducationQueryRead,
    EducationQueryResult,
    EducationQueryService,
    active_segment,
    resolve_grade_from_records,
)
from app.composition.school_fact_education_reader import (
    EducationReadProof,
    EducationReadSelection,
    EducationReadStale,
    aware,
    timestamp,
)
from app.composition.school_fact_teaching_query import resolve_teaching_period, teaching_view
from app.platform.contracts import StaffActorContext
from app.platform.school_facts.f7_coverage_basis import verify_coverage_basis


@dataclass(frozen=True)
class StaffCoordinateProof:
    school_fact_version: int
    check: Callable[[], bool]
    time_window: tuple[Callable[[], datetime], datetime, datetime | None] | None = None

    def revalidate(self) -> None:
        if not self.check():
            raise EducationReadStale("当前任职或学校资料已变化。")
        if self.time_window is not None:
            from app.composition.school_fact_read_evidence import register_release_time_window

            clock, observed_at, valid_before = self.time_window
            register_release_time_window(clock, observed_at=observed_at, valid_before=valid_before)


class SqlStaffTeachingCoordinates:
    """Only the narrow trusted-current-actor SQL function resolves identity coordinates."""

    def __init__(self, sessions: Any, *, clock: Callable[[], datetime] = lambda: datetime.now(UTC)):
        self.sessions, self.clock = sessions, clock

    def _read(self, actor: StaffActorContext) -> dict[str, Any]:
        if type(actor) is not StaffActorContext:
            raise PermissionError("需要当前教职工身份。")
        with self.sessions() as session:
            value = session.scalar(
                text(
                    "SELECT app.school_fact_f5_staff_coordinates("
                    ":school,:person,:assignment,:as_of)"
                ),
                {
                    "school": actor.school_id,
                    "person": actor.person_id,
                    "assignment": actor.assignment_id,
                    "as_of": self.clock(),
                },
            )
        if not isinstance(value, dict):
            raise PermissionError("当前任职的正式来源无法核实。")
        value = cast(dict[str, Any], value)
        if (
            set(value) != {"assignment_source_key", "person_source_key", "school_fact_version"}
            or type(value["school_fact_version"]) is not int
            or any(
                not isinstance(value[key], str) or not value[key]
                for key in ("assignment_source_key", "person_source_key")
            )
        ):
            raise PermissionError("当前任职的正式来源无法核实。")
        return value

    def resolve(self, actor: StaffActorContext) -> dict[str, Any]:
        value = self._read(actor)
        return {
            **value,
            "proof": StaffCoordinateProof(
                value["school_fact_version"], lambda: self._read(actor) == value
            ),
        }


class StaffTeachingQueryService:
    def __init__(
        self,
        repository: Any,
        *,
        external_reader: Any,
        identity_coordinates: Any = None,
        class_query: Any = None,
    ):
        self.repository, self.external_reader = repository, external_reader
        self.identity_coordinates = identity_coordinates
        self.class_query = class_query or EducationQueryService(
            repository, external_reader=external_reader
        )

    def query(self, actor: StaffActorContext, **selection: Any) -> EducationQueryResult:
        period = selection.pop("period", "current")
        if period == "current":
            return self._query_point(actor, **selection)
        if period not in {"current_term", "next_term"} or selection.get("as_of") is not None:
            raise ValueError("请选择明确且一致的教学期间。")
        selection.pop("as_of", None)
        return self._query_period(actor, period, selection)

    def _query_period(self, actor, period, selection):
        now = aware(self.repository.clock())
        trace = []
        initial = self._query_point(actor, **selection, _trace=trace)
        proofs = [initial.private_proof.education]
        extras = list(initial.private_proof.external)
        evidence = list(initial.evidence)

        def read(kind, fields, *, keys=None, filters=None):
            if keys is not None:
                keys = tuple(sorted(set(keys)))
                if not keys:
                    return ()
            result = self.repository.read_records(
                actor, kind, fields=fields, source_keys=keys, filter=filters, as_of=now
            )
            proofs.append(result.proof)
            trace.extend(result.records)
            return result.records

        def values(rows, field):
            return {
                segment["content"][field]
                for row in rows
                for segment in row.get("segments", ())
                if segment["content"].get(field) is not None
            }

        offering_keys = {
            row["source_key"] for row in trace if row["fact_kind"] == "teaching_offering"
        }
        offering_keys |= values(
            [
                row
                for row in trace
                if row["fact_kind"] in {"teaching_assignment", "offering_member_source"}
            ],
            "offering_source_key",
        )
        offerings = read(
            "teaching_offering", ("term_source_key", "course_source_key"), keys=offering_keys
        )
        explicit_terms = values(offerings, "term_source_key")
        explicit_terms |= values(
            [row for row in trace if row["fact_kind"] == "teaching_organization_scope"],
            "term_source_key",
        )
        calendars = set()
        if explicit_terms:
            terms = read(
                "education_term", ("display_name", "calendar_source_key"), keys=explicit_terms
            )
            calendars |= values(terms, "calendar_source_key")
        else:
            stages = set()
            for key in offering_keys:
                sources = read(
                    "offering_member_source",
                    ("offering_source_key", "source_kind", "class_source_key", "group_source_key"),
                    filters={"offering_source_key": key},
                )
                classes = values(sources, "class_source_key")
                for class_key in classes:
                    placements = read(
                        "class_placement",
                        ("class_source_key", "calendar_source_key"),
                        filters={"class_source_key": class_key},
                    )
                    calendars |= values(placements, "calendar_source_key")
                if not classes:
                    courses = read(
                        "teaching_course",
                        ("stage_source_keys",),
                        keys=values(offerings, "course_source_key"),
                    )
                    stages |= {
                        key
                        for row in courses
                        for segment in row.get("segments", ())
                        for key in segment["content"].get("stage_source_keys", ())
                    }
            stages |= values(
                [row for row in trace if row["fact_kind"] == "teaching_organization_scope"],
                "stage_source_key",
            )
            for stage in stages:
                matching = read(
                    "education_calendar_rule",
                    ("stage_source_key",),
                    filters={"stage_source_key": stage},
                )
                calendars |= {row["source_key"] for row in matching}
        resolutions = []
        clock_boundaries = []
        for calendar in sorted(calendars):
            terms = read(
                "education_term",
                ("display_name", "calendar_source_key"),
                filters={"calendar_source_key": calendar},
            )
            resolutions.append(resolve_teaching_period(terms, now=now, period=period))
            current = resolve_teaching_period(terms, now=now, period="current_term")
            if current.end is not None:
                clock_boundaries.append(current.end)
        signatures = {(r.status, r.start, r.end) for r in resolutions}
        selected = resolutions[0] if len(signatures) == 1 else None
        snapshots: dict[datetime, dict[str, Any]] = {}

        def finish(document):
            base = EducationReadProof.combine(tuple(proofs))
            if clock_boundaries:
                end = min(clock_boundaries)
                extras.append(
                    StaffCoordinateProof(
                        base.school_fact_version,
                        lambda: self.repository.clock() < end,
                        (self.repository.clock, now, end),
                    )
                )
            proof = EducationQueryProof(base, tuple(extras))
            proof.revalidate(actor=actor)
            if "selection_scope" in initial.public_result:
                document["selection_scope"] = initial.public_result["selection_scope"]
            document["selection"] = {**initial.public_result.get("selection", {}), "period": period}
            documents = tuple(snapshots.values()) or (initial.public_result,)
            meanings = [
                d["object_identity_meaning"] for d in documents if d.get("object_identity_meaning")
            ]
            if meanings:
                document["object_identity_meaning"] = meanings[0]
            zones = {d.get("fact_timezone_name") for d in documents}
            if len(zones) == 1 and None not in zones:
                document["fact_timezone_name"] = next(iter(zones))
            return EducationQueryResult(document, tuple(evidence), proof)

        if (
            selected is None
            or selected.status != "available"
            or selected.start is None
            or selected.end is None
        ):
            return finish(
                {
                    "status": "ambiguous" if len(signatures) > 1 else "period_not_recorded",
                    "items": [],
                    "coverage": "unknown",
                    "scope": "正式关联的学期未登记或存在多个不同期间，请按具体群体缩小查询。",
                }
            )

        def boundaries(rows):
            return {
                value
                for row in rows
                for segment in row.get("segments", ())
                for name in ("effective_at", "valid_to")
                if (value := timestamp(segment.get(name))) is not None
                and selected.start < value < selected.end
            }

        pending = {selected.start} | boundaries(trace)
        while pending:
            if len(snapshots) >= 32:
                raise ValueError("该期间变化超过有限分段容量，请按具体日期缩小查询。")
            at = min(pending)
            pending.remove(at)
            if at in snapshots:
                continue
            segment_trace = []
            point = self._query_point(actor, **selection, as_of=at, _trace=segment_trace)
            snapshots[at] = point.public_result
            proofs.append(point.private_proof.education)
            extras.extend(point.private_proof.external)
            evidence.extend(point.evidence)
            pending |= boundaries(segment_trace) - snapshots.keys()
        dates = sorted(snapshots)
        items = []
        for index, at in enumerate(dates):
            end = dates[index + 1] if index + 1 < len(dates) else selected.end
            items.extend(
                {**item, "period_start": at.isoformat(), "period_end": end.isoformat()}
                for item in snapshots[at].get("items", ())
            )
        if len(items) > 100:
            raise ValueError("期间结果超过有限容量，请缩小开课或日期范围。")
        return finish(
            {
                "status": "available" if items else "no_current_records",
                "items": items,
                "observed_count": len(items),
                "coverage": "unknown",
                "period": period,
                "period_label": selected.label,
                "period_start": selected.start.isoformat(),
                "period_end": selected.end.isoformat(),
                "scope": "按正式学期内实际变化分段的获准教学安排；未包含学生明细。",
            }
        )

    def _query_point(
        self,
        actor: StaffActorContext,
        *,
        view: str = "my_teaching",
        class_reference: str | None = None,
        target_reference: str | None = None,
        name: str | None = None,
        grade_name: str | None = None,
        campus_name: str | None = None,
        class_number: int | None = None,
        subject_name: str | None = None,
        course_name: str | None = None,
        as_of: datetime | None = None,
        period: str = "current",
        include_contacts: bool = False,
        _trace: list[dict[str, Any]] | None = None,
    ) -> EducationQueryResult:
        if type(actor) is not StaffActorContext:
            raise PermissionError("需要当前教职工身份。")
        class_selection = dict(
            class_reference=class_reference,
            name=name,
            grade_name=grade_name,
            campus_name=campus_name,
            class_number=class_number,
        )
        has_class = any(value is not None for value in class_selection.values())
        if (
            view not in {"my_teaching", "teaching", "my_organizations"}
            or period not in {"current", "current_term", "next_term"}
            or as_of is not None
            and period != "current"
            or view == "teaching"
            and not (has_class or subject_name or course_name or target_reference)
            or view != "teaching"
            and (has_class or course_name)
            or view == "my_organizations"
            and include_contacts
            or target_reference is not None
            and (
                view != "teaching"
                or has_class
                or subject_name is not None
                or course_name is not None
            )
        ):
            raise ValueError("请选择明确且一致的教学查询条件。")
        target_kind = None
        if target_reference is not None:
            parts = target_reference.split(":", 2) if isinstance(target_reference, str) else ()
            if (
                len(parts) != 3
                or parts[0] != "f4"
                or parts[1] not in {"teaching_assignment", "teaching_offering"}
            ):
                raise ValueError("请选择正式返回的任教或开课引用。")
            target_kind = parts[1]
        at = aware(self.repository.clock() if as_of is None else as_of)
        reads: list[Any] = []
        class_proofs: list[EducationReadProof] = []
        class_resolution_reads: tuple[EducationQueryRead, ...] = ()
        extras: list[Any] = []
        records: dict[tuple[str, str], dict[str, Any]] = {}
        evidence: list[dict[str, Any]] = []
        fact_timezones: set[str] = set()
        exact_key: str | None = None
        object_ids: dict[tuple[str, str], dict[str, Any]] = {}
        references: dict[tuple[str, str], str] = {}

        def object_coordinates(kind: str, key: str) -> dict[str, Any]:
            # Only already-authorized rows may enter this projection. Teaching
            # targets reuse the existing scoped reference issuer; equality-only
            # coordinates for other related objects remain unchanged.
            if (kind, key) not in records:
                raise PermissionError("教学对象缺少本次获准读取依据。")
            if (kind, key) not in object_ids:
                identity = sha256(
                    json.dumps(
                        [
                            str(actor.school_id),
                            str(actor.person_id),
                            str(actor.assignment_id),
                            kind,
                            key,
                        ],
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ).encode()
                ).hexdigest()
                value: dict[str, Any] = {"fact_kind": kind, "object_identity": identity}
                if kind == target_kind and key == exact_key:
                    value["reference"] = target_reference
                elif kind in {"teaching_assignment", "teaching_offering"}:
                    value["reference"] = references[(kind, key)]
                object_ids[(kind, key)] = value
            return dict(object_ids[(kind, key)])

        cache: dict[tuple[Any, ...], tuple[dict[str, Any], ...]] = {}
        prefetched: dict[tuple[Any, ...], Any] = {}
        external_reads: dict[
            tuple[str, tuple[str, ...], tuple[str, ...]],
            tuple[dict[str, dict[str, Any]], tuple[Any, ...]],
        ] = {}

        def read(
            kind: str,
            fields: tuple[str, ...],
            *,
            keys: tuple[str, ...] | None = None,
            filters: dict[str, str] | None = None,
        ) -> tuple[dict[str, Any], ...]:
            if keys is not None:
                keys = tuple(sorted(set(keys)))
                if not keys:
                    return ()
            cache_key = kind, fields, keys, tuple(sorted((filters or {}).items()))
            if cache_key in cache:
                return cache[cache_key]
            result = prefetched.pop(cache_key, None)
            # Only the immediately preceding class resolver's actual returned
            # rows can satisfy this point's exact audience/scope dependencies.
            # Missing rows, other times, filters or extra fields need a new read.
            for prior in class_resolution_reads:
                if result is not None:
                    break
                if (
                    kind not in {"education_class", "class_placement", "education_calendar_rule"}
                    or prior.kind != kind
                    or prior.as_of != (as_of if period == "current" else at)
                    or prior.known_at is not None
                    or not set(fields) <= set(prior.fields)
                    or not prior.result.records
                    or prior.result.proof.repository is not self.repository
                    or prior.result.proof.actor != actor
                ):
                    continue
                available = {row["source_key"] for row in prior.result.records}
                if keys is not None:
                    if filters or not set(keys) <= available:
                        continue
                elif prior.source_keys is not None or prior.filters != tuple(
                    sorted((filters or {}).items())
                ):
                    continue
                prior.result.proof.revalidate(actor=actor)
                rows = tuple(
                    {
                        **row,
                        "segments": [
                            {
                                **segment,
                                "content": {
                                    field: value
                                    for field, value in segment["content"].items()
                                    if field in fields
                                },
                            }
                            for segment in row.get("segments", ())
                        ],
                    }
                    for row in prior.result.records
                    if keys is None or row["source_key"] in keys
                )
                result = replace(prior.result, records=rows)
                break
            if result is None:
                result = self.repository.read_records(
                    actor,
                    kind,
                    fields=fields,
                    source_keys=keys,
                    filter=filters,
                    as_of=as_of if period == "current" else at,
                )
            reads.append(result)
            references.update(((kind, key), ref) for key, ref in result.references)
            for row in result.records:
                if _trace is not None:
                    _trace.append(row)
                records[(kind, row["source_key"])] = row
                if kind == "education_calendar_rule":
                    segment = active_segment(row, at)
                    zone = segment["content"].get("timezone_name") if segment else None
                    if isinstance(zone, str) and zone:
                        fact_timezones.add(zone)
                evidence.append(
                    {
                        "fact_kind": kind,
                        "version": row.get("version"),
                        "source": [s.get("source", {}) for s in row.get("segments", ())],
                    }
                )
            active = tuple(row for row in result.records if active_segment(row, at) is not None)
            cache[cache_key] = active
            return active

        def content(row: dict[str, Any]) -> dict[str, Any]:
            segment = active_segment(row, at)
            if segment is None:
                raise EducationReadStale("教学适用时点已变化。")
            return segment["content"]

        def external(
            kind: str, keys: tuple[str, ...], fields: tuple[str, ...]
        ) -> dict[str, dict[str, Any]]:
            if not keys:
                return {}
            selected = tuple(sorted(set(keys)))
            read_key = kind, selected, fields
            previous = external_reads.get(read_key)
            if previous is not None:
                # Only this point's exact projection is shared. Recheck current
                # evidence before each reuse; no acquisition authority is cached.
                projected, proofs = previous
                for proof in proofs:
                    proof.revalidate()
                return projected
            result = self.external_reader.read_records(
                actor,
                kind,
                source_keys=selected,
                fields=fields,
                as_of=as_of if period == "current" else at,
            )
            proof = result.get("proof")
            if proof is None:
                raise PermissionError("关联读取没有当前依据。")
            extras.append(proof)
            if result.get("boundary_proof") is not None:
                extras.append(result["boundary_proof"])
            if _trace is not None and result.get("next_valid_boundary"):
                _trace.append(
                    {
                        "fact_kind": kind,
                        "segments": [
                            {"effective_at": result["next_valid_boundary"], "content": {}}
                        ],
                    }
                )
            rows = result.get("records", ())
            if any(
                row.get("source_key") not in selected
                or not set(row.get("content", {})) <= set(fields)
                for row in rows
            ):
                raise PermissionError("关联读取超出请求范围。")
            projected = {row["source_key"]: row["content"] for row in rows}
            boundary_proof = result.get("boundary_proof")
            external_reads[read_key] = (
                projected,
                (proof,) if boundary_proof is None else (proof, boundary_proof),
            )
            return projected

        def finish(document: dict[str, Any]) -> EducationQueryResult:
            if reads or class_proofs:
                proof = EducationReadProof.combine(
                    tuple(r.proof for r in reads) + tuple(class_proofs)
                )
            elif extras and type(getattr(extras[0], "school_fact_version", None)) is int:
                proof = EducationReadProof(
                    self.repository, actor, (), extras[0].school_fact_version, None
                )
            else:
                raise PermissionError("查询缺少实际读取依据。")
            combined = EducationQueryProof(proof, tuple(extras))
            combined.revalidate(actor=actor)
            document.update(as_of=at.isoformat(), coverage="unknown")
            document["selection"] = {
                "view": view,
                **{key: value for key, value in class_selection.items() if value is not None},
                **{
                    key: value
                    for key, value in {
                        "target_reference": target_reference,
                        "subject_name": subject_name,
                        "course_name": course_name,
                    }.items()
                    if value is not None
                },
                "period": period,
            }
            if len(fact_timezones) == 1:
                document["fact_timezone_name"] = next(iter(fact_timezones))
            if object_ids:
                document["object_identity_meaning"] = (
                    "object_identity只用于同一身份获准结果内辨认正式对象，不是工具引用或权限凭据；"
                    "不同标识是不同对象，同一标识本身不证明发生过修改。selection是实际查询条件，"
                    "按课程或学科名称返回的集合不等于原回执的单一目标。"
                )
            if target_kind is not None:
                document["selection_scope"] = {
                    "kind": target_kind,
                    "resolution": "resolved" if exact_key is not None else "not_resolved",
                    "meaning": (
                        "仅选定任教在所问时点的正式安排，不代表该开课全部教师；无生效登记不能替换为其他任教。"
                        if target_kind == "teaching_assignment"
                        else "仅选定开课在所问时点的获准任教集合，未返回不代表不存在或名单完整。"
                    ),
                }
            return EducationQueryResult(document, tuple(evidence), combined)

        if period != "current":
            result = self.repository.read_records(actor, "education_term", fields=("display_name",))
            reads.append(result)
            resolved = resolve_teaching_period(result.records, now=at, period=period)
            if resolved.status != "available" or resolved.start is None:
                return finish({"status": resolved.status, "items": []})
            at = resolved.start

        coordinates: dict[str, Any] | None = None
        if view in {"my_teaching", "my_organizations"}:
            if self.identity_coordinates is None:
                raise PermissionError("当前任职来源尚未接通。")
            coordinates = cast(dict[str, Any], self.identity_coordinates.resolve(actor))
            extras.append(coordinates["proof"])
        if view == "my_organizations":
            return self._organizations(
                actor, coordinates, at, as_of, subject_name, read, content, external, extras, finish
            )

        offering_keys: set[str] | None = None
        assignments: tuple[dict[str, Any], ...] = ()
        assignment_fields = (
            "offering_source_key",
            "assignment_source_key",
            "teaching_role",
            "subject_source_keys",
            "duty_scope",
        )
        if target_kind is not None:
            key = self.repository.source_for_reference(actor, target_kind, target_reference)
            exact_key = key
            if target_kind == "teaching_assignment":
                assignments = read(target_kind, assignment_fields, keys=(key,))
                offering_keys = {content(row)["offering_source_key"] for row in assignments}
            else:
                offering_keys = {key}
        elif view == "my_teaching":
            assert coordinates is not None
            assignments = read(
                "teaching_assignment",
                assignment_fields,
                filters={"assignment_source_key": coordinates["assignment_source_key"]},
            )
            offering_keys = {content(row)["offering_source_key"] for row in assignments}
        elif has_class:
            resolved = self.class_query.locate_class(
                actor,
                as_of=as_of if period == "current" else at,
                class_reference=class_reference,
                name=name,
                grade_name=grade_name,
                campus_name=campus_name,
                class_number=class_number,
            )
            class_proofs.append(resolved.private_proof.education)
            class_resolution_reads = resolved.private_reads
            extras.extend(resolved.private_proof.external)
            if resolved.public_result.get("fact_timezone_name"):
                fact_timezones.add(resolved.public_result["fact_timezone_name"])
            if resolved.public_result.get("status") != "known":
                return resolved
            selected_keys = {
                key
                for prior in class_resolution_reads
                if prior.kind == "education_class"
                and prior.as_of == (as_of if period == "current" else at)
                and prior.known_at is None
                and prior.result.proof.repository is self.repository
                and prior.result.proof.actor == actor
                for key, ref in prior.result.references
                if ref == resolved.public_result["reference"]
                and any(row["source_key"] == key for row in prior.result.records)
            }
            if len(selected_keys) == 1:
                # The freshly resolved private coordinate is a selector only;
                # each downstream fact read still acquires its own exact permit.
                key = next(iter(selected_keys))
            else:
                key = self.repository.source_for_reference(
                    actor, "education_class", resolved.public_result["reference"]
                )
            sources = read(
                "offering_member_source",
                ("offering_source_key", "source_kind", "class_source_key", "group_source_key"),
                filters={"class_source_key": key},
            )
            offering_keys = {
                content(row)["offering_source_key"]
                for row in sources
                if content(row)["source_kind"] == "administrative_class"
            }

        selected_courses: set[str] | None = None
        if subject_name is not None:
            subjects = read(
                "teaching_subject", ("display_name",), filters={"display_name": subject_name}
            )
            selected_courses = set()
            for subject in subjects:
                links = read(
                    "course_subject",
                    ("course_source_key", "subject_source_key"),
                    filters={"subject_source_key": subject["source_key"]},
                )
                selected_courses.update(content(row)["course_source_key"] for row in links)
        if course_name is not None:
            courses = read(
                "teaching_course", ("display_name",), filters={"display_name": course_name}
            )
            matches = {row["source_key"] for row in courses}
            selected_courses = matches if selected_courses is None else selected_courses & matches
        offering_fields = ("course_source_key", "display_name", "offering_status")
        if offering_keys is not None:
            offerings = read("teaching_offering", offering_fields, keys=tuple(offering_keys))
            if selected_courses is not None:
                offerings = tuple(
                    row
                    for row in offerings
                    if content(row)["course_source_key"] in selected_courses
                )
        else:
            offerings = tuple(
                row
                for key in sorted(selected_courses or ())
                for row in read(
                    "teaching_offering", offering_fields, filters={"course_source_key": key}
                )
            )
        if not offerings:
            return finish(
                {
                    "status": "no_current_records",
                    "items": [],
                    "observed_count": 0,
                    "scope": "当前获准范围和所问时点内没有相符的正式教学登记。",
                }
            )
        offering_keys = {row["source_key"] for row in offerings}
        # Keep prefilter dependencies while projecting only matching offerings.
        for key in tuple(records):
            if key[0] == "teaching_offering" and key[1] not in offering_keys:
                del records[key]
        course_keys = tuple(sorted({content(row)["course_source_key"] for row in offerings}))
        # These dependencies are already fixed by the authorized offerings. A
        # finite wave retains each projection/permit/proof while avoiding four
        # separate database and root-identity round trips. Other query branches
        # may still narrow offerings later, so they keep their existing reads.
        read_many = getattr(self.repository, "read_many", None)
        if view == "teaching" and target_kind != "teaching_assignment" and callable(read_many):
            selections = [
                EducationReadSelection(
                    "teaching_course",
                    ("display_name", "stage_source_keys", "grade_source_keys"),
                    source_keys=course_keys,
                )
            ]
            selections.extend(
                EducationReadSelection(
                    "course_subject",
                    ("course_source_key", "subject_source_key"),
                    filter={"course_source_key": key},
                )
                for key in course_keys
            )
            selections.extend(
                EducationReadSelection(
                    "teaching_assignment",
                    assignment_fields,
                    filter={"offering_source_key": key},
                )
                for key in sorted(offering_keys)
            )
            selections.extend(
                EducationReadSelection(
                    "offering_member_source",
                    ("offering_source_key", "source_kind", "class_source_key", "group_source_key"),
                    filter={"offering_source_key": key},
                )
                for key in sorted(offering_keys)
            )
            for start in range(0, len(selections), 4):
                wave = tuple(selections[start : start + 4])
                results = read_many(actor, wave, as_of=as_of if period == "current" else at)
                if len(results) != len(wave):
                    raise EducationReadStale("关联读取没有返回完整依据。")
                for selection, result in zip(wave, results, strict=True):
                    prefetched[
                        (
                            selection.kind,
                            selection.fields,
                            selection.source_keys,
                            tuple(sorted((selection.filter or {}).items())),
                        )
                    ] = result
        course_rows = read(
            "teaching_course",
            ("display_name", "stage_source_keys", "grade_source_keys"),
            keys=course_keys,
        )
        course_scopes = {row["source_key"]: content(row) for row in course_rows}
        links = tuple(
            row
            for key in course_keys
            for row in read(
                "course_subject",
                ("course_source_key", "subject_source_key"),
                filters={"course_source_key": key},
            )
        )
        read(
            "teaching_subject",
            ("display_name",),
            keys=tuple({content(row)["subject_source_key"] for row in links}),
        )
        if view != "my_teaching" and target_kind != "teaching_assignment":
            assignments = tuple(
                row
                for key in sorted(offering_keys)
                for row in read(
                    "teaching_assignment", assignment_fields, filters={"offering_source_key": key}
                )
            )
        else:
            assignments = tuple(
                row for row in assignments if content(row)["offering_source_key"] in offering_keys
            )
        if subject_name is not None:
            matched_subjects = {
                row["source_key"]
                for row in records.values()
                if row["fact_kind"] == "teaching_subject"
                and content(row).get("display_name") == subject_name
            }
            assignments = tuple(
                row
                for row in assignments
                if not content(row).get("subject_source_keys")
                or set(content(row)["subject_source_keys"]) & matched_subjects
            )
            if view == "my_teaching":
                own_offerings = {content(row)["offering_source_key"] for row in assignments}
                offerings = tuple(row for row in offerings if row["source_key"] in own_offerings)
                offering_keys &= own_offerings
        assignment_keys = tuple({content(row)["assignment_source_key"] for row in assignments})
        assigned = external("assignment", assignment_keys, ("person_source_key",))
        persons = external(
            "person",
            tuple(
                {v["person_source_key"] for v in assigned.values() if v.get("person_source_key")}
            ),
            ("display_name",),
        )
        teachers: dict[str, dict[str, Any]] = {
            key: {
                "name": persons.get(str(value.get("person_source_key", "")), {}).get("display_name")
            }
            for key, value in assigned.items()
        }
        if include_contacts:
            for key in assignment_keys:
                result = self.external_reader.official_contacts(
                    actor, assignment_keys=(key,), as_of=as_of if period == "current" else at
                )
                extras.append(result["proof"])
                if result.get("boundary_proof") is not None:
                    extras.append(result["boundary_proof"])
                if _trace is not None and result.get("next_valid_boundary"):
                    _trace.append(
                        {
                            "fact_kind": "official_contact",
                            "segments": [
                                {"effective_at": result["next_valid_boundary"], "content": {}}
                            ],
                        }
                    )
                teachers.setdefault(key, {})["official_contacts"] = result["records"]
        audiences: dict[str, list[dict[str, Any]]] = {}
        scope_notices: dict[str, list[str]] = {}
        for key in sorted(offering_keys):
            sources = read(
                "offering_member_source",
                ("offering_source_key", "source_kind", "class_source_key", "group_source_key"),
                filters={"offering_source_key": key},
            )
            if len(sources) > 1:
                raise EducationReadStale("同一开课存在多个有效名单来源。")
            audiences[key] = []
            scope_notices[key] = []
            offering = next(row for row in offerings if row["source_key"] == key)
            course_scope = course_scopes.get(content(offering)["course_source_key"], {})
            for row in sources:
                source = content(row)
                if source["source_kind"] == "administrative_class":
                    classes = read(
                        "education_class", ("display_name",), keys=(source["class_source_key"],)
                    )
                    label = content(classes[0]).get("display_name") if len(classes) == 1 else None
                    audience = {"name": label, "source": "行政班实时成员"}
                    if len(classes) == 1:
                        audience.update(
                            object_coordinates("education_class", source["class_source_key"])
                        )
                    audiences[key].append(audience)
                    grades, stages = (
                        course_scope.get("grade_source_keys"),
                        course_scope.get("stage_source_keys"),
                    )
                    if grades or stages:
                        placements = read(
                            "class_placement",
                            ("class_source_key", "calendar_source_key", "grade_source_key"),
                            filters={"class_source_key": source["class_source_key"]},
                        )
                        if len(placements) != 1:
                            scope_notices[key].append(
                                "班级当前年级依据尚未明确，课程适用范围需学校核对。"
                            )
                        else:
                            placement = placements[0]
                            calendar = read(
                                "education_calendar_rule",
                                (
                                    "start_cycle",
                                    "end_cycle",
                                    "month",
                                    "day",
                                    "timezone_name",
                                    "grade_source_keys",
                                    "stage_source_key",
                                ),
                                keys=(content(placement)["calendar_source_key"],),
                            )
                            resolved = resolve_grade_from_records(
                                at=at,
                                calendars=calendar,
                                placement_record=placement,
                                placement=active_segment(placement, at),
                            )
                            if resolved.next_boundary is not None:
                                if _trace is not None:
                                    _trace.append(
                                        {
                                            "fact_kind": "calendar_boundary",
                                            "segments": [
                                                {
                                                    "effective_at": resolved.next_boundary,
                                                    "content": {},
                                                }
                                            ],
                                        }
                                    )
                                if as_of is None:
                                    boundary = resolved.next_boundary
                                    extras.append(
                                        StaffCoordinateProof(
                                            self.repository.projection.current_version(
                                                actor.school_id
                                            ),
                                            lambda boundary=boundary: (
                                                self.repository.clock() < boundary
                                            ),
                                            (
                                                self.repository.clock,
                                                self.repository.clock(),
                                                boundary,
                                            ),
                                        )
                                    )
                            if resolved.grade_id is None:
                                scope_notices[key].append(
                                    "班级当前年级依据尚未明确，课程适用范围需学校核对。"
                                )
                            elif grades and resolved.grade_id not in grades:
                                scope_notices[key].append(
                                    "班级当前年级与课程登记的适用年级不一致，需学校核对；该开课尚未因此终止。"
                                )
                            if (
                                stages
                                and len(calendar) == 1
                                and content(calendar[0]).get("stage_source_key") not in stages
                            ):
                                scope_notices[key].append(
                                    "班级当前学段与课程登记的适用学段不一致，需学校核对。"
                                )
                else:
                    groups = read(
                        "teaching_group",
                        ("organization_source_key", "group_mode"),
                        keys=(source["group_source_key"],),
                    )
                    orgs = external(
                        "organization",
                        tuple({content(g)["organization_source_key"] for g in groups}),
                        ("display_name",),
                    )
                    audiences[key].extend(
                        {
                            "name": value.get("display_name"),
                            "source": "独立教学名单，需单独维护",
                            **object_coordinates("teaching_group", source["group_source_key"]),
                        }
                        for value in orgs.values()
                    )
        items: list[dict[str, Any]] = []
        from app.composition.school_fact_education_quality import EducationDependencyDenied

        for offering in offerings:
            key = offering["source_key"]
            selected_records = tuple(
                row
                for row in records.values()
                if row["fact_kind"] != "teaching_offering" or row["source_key"] == key
            )
            quality_statements = []
            quality_not_authorized = False
            quality_for = getattr(self.external_reader, "quality_for", None)
            if quality_for is not None:
                try:
                    quality = quality_for(
                        actor,
                        target_kind="teaching_assignment",
                        target_source_keys=tuple(
                            sorted(
                                row["source_key"]
                                for row in assignments
                                if content(row)["offering_source_key"] == key
                            )
                        ),
                        field_names=assignment_fields,
                        owner_kind="teaching_offering",
                        owner_source_keys=(key,),
                        teaching_roles=("lead", "co_teacher"),
                        include_target_owner=True,
                        as_of=as_of if period == "current" else at,
                    )
                except EducationDependencyDenied:
                    quality = None
                    quality_not_authorized = True
                if quality is not None:
                    if _trace is not None and quality.get("next_valid_boundary"):
                        _trace.append(
                            {
                                "fact_kind": "fact_quality",
                                "segments": [
                                    {"effective_at": quality["next_valid_boundary"], "content": {}}
                                ],
                            }
                        )
                    extras.append(quality["proof"])
                    for statement in quality["records"]:
                        fields = statement["content"]
                        target_key = statement.get("applies_to_source_key") or fields.get(
                            "target_source_key"
                        )
                        target_row = records.get(("teaching_assignment", target_key))
                        field_name = fields.get("field_name")
                        verified_exact_complete = (
                            fields.get("quality_kind") == "coverage"
                            and fields.get("coverage_status") == "complete"
                            and target_row is not None
                            and field_name in assignment_fields
                            and active_segment(target_row, at) is not None
                            and type(quality.get("school_fact_version")) is int
                            and all(
                                read.school_fact_version == quality["school_fact_version"]
                                for read in reads
                            )
                            and verify_coverage_basis(
                                fields.get("coverage_basis"),
                                school_id=actor.school_id,
                                owner_kind="education",
                                owner_source_key=fields.get("education_target_source_key", key),
                                target_fact_kind="teaching_assignment",
                                target_source_key=target_key,
                                field_name=field_name,
                                purpose=None,
                                contact_kind=None,
                                factual_at=at,
                                school_fact_version=quality["school_fact_version"],
                                members=((target_key, target_row["version"]),),
                            )
                        )
                        quality_statements.append(
                            {
                                "offering_source_key": key,
                                "target_source_key": target_key,
                                "verified_exact_complete": verified_exact_complete,
                                "effective_at": statement.get("effective_at"),
                                "valid_to": statement.get("valid_to"),
                                **{
                                    name: fields.get(name)
                                    for name in (
                                        "quality_kind",
                                        "negative_code",
                                        "teaching_role",
                                        "coverage_status",
                                        "source_status",
                                        "dispute_status",
                                        "verified_at",
                                        "review_due_at",
                                        "field_name",
                                    )
                                },
                                "source": {
                                    name: statement.get("source", {}).get(name)
                                    for name in ("label", "published_at")
                                },
                            }
                        )
            view_document = teaching_view(
                records=selected_records,
                teachers=teachers,
                at=at,
                subject_name=subject_name,
                include_contacts=include_contacts,
                quality=quality_statements,
                object_coordinates=object_coordinates,
            )
            for item in view_document["items"]:
                item["audiences"] = audiences[key]
                if quality_not_authorized:
                    item["quality_status"] = "not_authorized"
                if scope_notices[key]:
                    item["notices"] = list(
                        dict.fromkeys(item.get("notices", []) + scope_notices[key])
                    )
                items.append(item)
        return finish(
            {
                "status": "available" if items else "no_current_records",
                "items": items,
                "observed_count": len(items),
                "scope": "本次获准对象和字段内的正式教学安排，未包含学生明细。",
            }
        )

    def _organizations(
        self,
        actor: StaffActorContext,
        coordinates: Any,
        at: datetime,
        as_of: datetime | None,
        subject_name: str | None,
        read: Any,
        content: Any,
        external: Any,
        extras: list[Any],
        finish: Any,
    ) -> EducationQueryResult:
        from app.composition.school_fact_education_external import ExternalReadProof

        directory = self.external_reader.directory
        person_ref = directory.reference_for(
            actor=actor, fact_kind="person", source_key=coordinates["person_source_key"]
        )
        with self.external_reader.evidence.capture(
            actor=actor, current_time=as_of is None
        ) as captured:
            memberships = directory.query(
                actor=actor,
                fact_kind="organization_membership",
                related_ref=person_ref,
                factual_at=as_of,
                content_fields=("organization_source_key", "membership_kind"),
                page_size=100,
            )
            proof = captured.seal()
        if memberships.status == "forbidden" or proof is None:
            raise PermissionError("当前身份没有所需组织成员的读取权限。")
        if memberships.status not in {"known", "no_current_records"} or memberships.has_more:
            raise ValueError("组织成员候选超出有限查询容量，请缩小范围。")
        extras.append(ExternalReadProof(actor, (proof,)))
        org_keys: set[str] = set()
        member_roles: dict[str, str | None] = {}
        for row in memberships.records:
            if set(row.content) - {"organization_ref", "membership_kind"}:
                raise PermissionError("组织成员读取超出请求字段。")
            key = directory.source_for(
                actor=actor, fact_kind="organization", reference=row.content["organization_ref"]
            )
            org_keys.add(key)
            member_roles[key] = row.content.get("membership_kind")
        items: list[dict[str, Any]] = []
        scope_fields = (
            "organization_source_key",
            "subject_source_key",
            "scope_basis",
            "stage_source_key",
            "grade_source_key",
            "grade_group_source_key",
            "term_source_key",
        )
        for key in sorted(org_keys):
            scopes = read(
                "teaching_organization_scope",
                scope_fields,
                filters={"organization_source_key": key},
            )
            if not scopes:
                continue
            names = external("organization", (key,), ("display_name",))
            scope_items: list[dict[str, Any]] = []
            for scope in scopes:
                value = content(scope)
                subjects = read(
                    "teaching_subject", ("display_name",), keys=(value["subject_source_key"],)
                )
                label = content(subjects[0]).get("display_name") if len(subjects) == 1 else None
                if subject_name is not None and label != subject_name:
                    continue
                detail = {
                    "subject": label,
                    "basis": {
                        "stage": "学段",
                        "fixed_grade": "固定年级",
                        "academic_grade": "指定学年年级",
                    }[value["scope_basis"]],
                }
                for field, kind, target in (
                    ("stage_source_key", "education_stage", "stage"),
                    ("grade_source_key", "education_grade", "grade"),
                    ("grade_group_source_key", "education_grade_group", "academic_grade"),
                    ("term_source_key", "education_term", "term"),
                ):
                    if value.get(field):
                        if kind == "education_grade_group":
                            rows = read(
                                kind, ("academic_year", "grade_source_key"), keys=(value[field],)
                            )
                            detail["academic_year"] = (
                                content(rows[0])["academic_year"] if len(rows) == 1 else None
                            )
                            grades = (
                                read(
                                    "education_grade",
                                    ("display_name",),
                                    keys=(content(rows[0])["grade_source_key"],),
                                )
                                if len(rows) == 1
                                else ()
                            )
                            detail[target] = (
                                content(grades[0]).get("display_name") if len(grades) == 1 else None
                            )
                        else:
                            rows = read(kind, ("display_name",), keys=(value[field],))
                            detail[target] = (
                                content(rows[0]).get("display_name") if len(rows) == 1 else None
                            )
                scope_items.append(detail)
            if scope_items:
                items.append(
                    {
                        "name": names.get(key, {}).get("display_name"),
                        "membership": {"member": "成员", "participant": "参与人员"}.get(
                            member_roles[key]
                        ),
                        "scopes": scope_items,
                    }
                )
        return finish(
            {
                "status": "available" if items else "no_current_records",
                "items": items,
                "observed_count": len(items),
                "scope": "本人正式成员关系与已登记的教学适用范围；成员身份不授予学生资料权限。",
            }
        )
