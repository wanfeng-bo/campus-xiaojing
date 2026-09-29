"""Selected-child teaching through a fixed Reader projection and live release checks."""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import text

from app.composition.school_fact_education_query import active_segment
from app.composition.school_fact_education_reader import aware, timestamp
from app.composition.school_fact_family_query import SqlFamilyReadRepository
from app.composition.school_fact_teaching_query import teaching_view
from app.domains.students.family_read import FamilyReadEvidence
from app.platform.contracts import ActorContext, GuardianActorContext
from app.platform.school_facts.f7_coverage_basis import verify_coverage_basis

_READ = text("""SELECT app.school_fact_family_teaching(
 :school,:adult,:relation,:version,:factual_at,:period,:subject,:course,:contacts)""")
_STAMP = text("""SELECT app.school_fact_family_teaching_stamp(
 :school,:adult,:relation,:version)""")


class FamilyTeachingRepository(SqlFamilyReadRepository):
    def teaching_read(self, actor, *, factual_at, period, subject_name, course_name, contacts):
        with self.sessions() as session:
            raw = session.execute(
                _READ,
                self._parameters(actor)
                | {
                    "factual_at": factual_at,
                    "period": period,
                    "subject": subject_name,
                    "course": course_name,
                    "contacts": contacts,
                },
            ).scalar_one()
            if type(raw) is not dict:
                raise ValueError("Invalid teaching projection")
            return raw

    def teaching_stamp(self, actor):
        with self.sessions() as session:
            return session.execute(_STAMP, self._parameters(actor)).scalar_one()


@dataclass(frozen=True)
class FamilyTeachingResult:
    payload: dict[str, Any]
    evidence: FamilyReadEvidence | None = None

    def public(self):
        return {"kind": "family_teaching", **deepcopy(self.payload)}


def _teachers(rows, contacts):
    if type(rows) is not list or len(rows) > 100:
        raise ValueError("Invalid teacher projection")
    allowed = {"assignment_source_key", "name", "official_contacts", "source"}
    contact_fields = {
        "owner",
        "owner_kind",
        "contact_kind",
        "contact_value",
        "service_hours",
        "source",
    }
    result = {}
    for row in rows:
        if type(row) is not dict or set(row) - allowed or "assignment_source_key" not in row:
            raise ValueError("Overbroad teacher projection")
        if row["assignment_source_key"] in result:
            raise ValueError("Conflicting teacher projection")
        for contact in row.get("official_contacts", ()):
            if not contacts or type(contact) is not dict or set(contact) - contact_fields:
                raise ValueError("Unrequested contact projection")
            source = contact.get("source")
            if (
                type(source) is not dict
                or set(source) != {"kind", "label", "published_at"}
                or source["kind"] != "formal_publication"
                or not isinstance(source["label"], str)
                or not source["label"]
                or timestamp(source["published_at"]) is None
            ):
                raise ValueError("Overbroad or invalid official channel publication source")
        result[row["assignment_source_key"]] = row
    return result


def _quality(rows, *, school_id: UUID, records, at: datetime, school_fact_version: int):
    allowed = {
        "offering_source_key",
        "quality_owner_source_key",
        "teaching_role",
        "quality_kind",
        "negative_code",
        "coverage_status",
        "coverage_basis",
        "dispute_status",
        "source_status",
        "field_name",
        "target_source_key",
        "replacement_source_key",
        "verified_at",
        "review_due_at",
        "effective_at",
        "valid_to",
        "version",
        "source",
    }
    if (
        type(rows) is not list
        or len(rows) > 100
        or any(type(row) is not dict or set(row) - allowed for row in rows)
    ):
        raise ValueError("Overbroad teaching quality projection")
    exact = {
        record["source_key"]: record
        for record in records
        if record.get("fact_kind") == "teaching_assignment"
        and active_segment(record, at) is not None
    }
    if type(school_fact_version) is not int:
        raise ValueError("Missing exact teaching dependency version")
    safe = []
    for statement in rows:
        target_key = statement.get("target_source_key")
        owner_key = statement.get("quality_owner_source_key", statement.get("offering_source_key"))
        if owner_key not in {statement.get("offering_source_key"), target_key} or owner_key is None:
            raise ValueError("Teaching quality owner is outside the selected relationship")
        target = exact.get(target_key)
        field = statement.get("field_name")
        segment = active_segment(target, at) if target is not None else None
        verified = (
            statement.get("quality_kind") == "coverage"
            and statement.get("coverage_status") == "complete"
            and segment is not None
            and field is not None
            and field in segment["content"]
            and segment["content"].get("offering_source_key")
            == statement.get("offering_source_key")
            and verify_coverage_basis(
                statement.get("coverage_basis"),
                school_id=school_id,
                owner_kind="education",
                owner_source_key=owner_key,
                target_fact_kind="teaching_assignment",
                target_source_key=target_key,
                field_name=field,
                purpose=None,
                contact_kind=None,
                factual_at=at,
                school_fact_version=school_fact_version,
                members=((target_key, target["version"]),),
            )
        )
        safe.append(
            {
                key: value
                for key, value in statement.items()
                if key not in {"coverage_basis", "quality_owner_source_key"}
            }
            | {"verified_exact_complete": verified}
        )
    return safe


def _groups(rows):
    if (
        type(rows) is not list
        or len(rows) > 100
        or any(type(row) is not dict or set(row) != {"group_source_key", "name"} for row in rows)
    ):
        raise ValueError("Overbroad teaching group projection")
    if len({row["group_source_key"] for row in rows}) != len(rows):
        raise ValueError("Ambiguous group name projection")
    return {row["group_source_key"]: row["name"] for row in rows}


def _student_participation(status: object) -> dict[str, str]:
    # Enrollment and teaching membership are evidence, not attendance records.
    labels = {
        "suspended": "休学",
        "enrolled": "在读",
        "graduated": "毕业",
        "transferred_out": "转出",
        "withdrawn": "退学",
    }
    known = status if isinstance(status, str) and status in labels else "unknown"
    state = (
        f"该孩子在所查时点处于{labels[known]}状态。"
        if known in labels
        else "本次未取得该孩子在所查时点的学生状态。"
    )
    return {
        "enrollment_status": known,
        "enrollment_label": labels.get(known, "未取得学生状态"),
        "actual_participation": "unknown",
        "evidence_meaning": state
        + "以下课程表示保留的正式教学关联，课程持续开设说明开课状态；"
        + "这些资料不能证明该孩子实际参加课程或实际到课，实际参与情况未知。",
    }


def _period_view(raw, *, observed, subject_name, contacts, school_id):
    """Expose every bounded factual interval, including gaps and formal changes."""
    points = raw.get("period_slices", [])
    if not points:
        return None
    end = timestamp(raw.get("period_end"))
    if type(points) is not list or len(points) > 40 or end is None:
        raise ValueError("Invalid teaching period projection")
    timeline = []
    previous = None
    for index, point in enumerate(points):
        if type(point) is not dict:
            raise ValueError("Invalid period slice")
        at = timestamp(point.get("as_of"))
        if at is None or at < observed or at >= end or previous is not None and at <= previous:
            raise ValueError("Invalid teaching period boundary")
        if index == 0 and at != observed:
            raise ValueError("Teaching period has an unaccounted first interval")
        rendered = teaching_view(
            records=point.get("records", []),
            teachers=_teachers(point.get("related_teachers", []), contacts),
            at=at,
            subject_name=subject_name,
            include_contacts=contacts,
            enrollment_status=point.get("enrollment_status", raw.get("enrollment_status")),
            quality=_quality(
                point.get("quality", []),
                school_id=school_id,
                records=point.get("records", []),
                at=at,
                school_fact_version=raw.get("school_fact_version"),
            ),
            group_names=_groups(point.get("related_groups", [])),
        )
        entry = {
            "from": at.isoformat(),
            "until": end.isoformat(),
            "items": rendered["items"],
            "status": rendered["status"],
            "student_participation": _student_participation(
                point.get("enrollment_status", raw.get("enrollment_status"))
            ),
        }
        if rendered.get("participation"):
            entry["participation"] = rendered["participation"]
        if timeline:
            timeline[-1]["until"] = at.isoformat()
        timeline.append(entry)
        previous = at
    return timeline


class FamilyTeachingQueryService:
    def __init__(self, *, repository, current_actor, clock=lambda: datetime.now(UTC)):
        self.repository, self.current_actor, self.clock = repository, current_actor, clock

    def _basis(self, actor, at):
        now = self.clock()
        if type(actor) is not GuardianActorContext or not self.current_actor(actor):
            return None
        basis = self.repository.authority(actor)
        if (
            basis is None
            or basis.relationship_kind != "verified_guardian"
            or not basis.permits(actor, now=now, factual_at=now)
            or at < basis.valid_from
            or basis.valid_to is not None
            and at >= basis.valid_to
        ):
            return None
        return basis

    def query(
        self,
        *,
        actor: ActorContext,
        subject_name: str | None = None,
        course_name: str | None = None,
        include_official_contacts: bool = False,
        as_of: datetime | None = None,
        period: str = "current",
    ) -> FamilyTeachingResult:
        if (
            period not in {"current", "current_term", "next_term"}
            or as_of is not None
            and period != "current"
        ):
            raise ValueError("Specify a date or one teaching period")
        if any(
            value is not None and (type(value) is not str or not value.strip() or len(value) > 120)
            for value in (subject_name, course_name)
        ):
            raise ValueError("Invalid teaching selector")
        if type(include_official_contacts) is not bool:
            raise ValueError("Invalid contact selection")
        queried_at = self.clock()
        at = as_of or queried_at
        aware(at)
        try:
            basis = self._basis(actor, at)
            if basis is None:
                return FamilyTeachingResult({"status": "denied"})
            raw = self.repository.teaching_read(
                actor,
                factual_at=as_of,
                period=period,
                subject_name=subject_name,
                course_name=course_name,
                contacts=include_official_contacts,
            )
            if self._basis(actor, at) != basis:
                return FamilyTeachingResult({"status": "denied"})
            if (
                raw.get("student_id") != str(basis.student_id)
                or raw.get("student_version") != basis.student_version
                or raw.get("relationship_version") != basis.relationship_version
            ):
                raise ValueError("Selected child dependency changed")
            status = raw.get("status")
            if status not in {
                "available",
                "no_current_records",
                "conflict",
                "period_not_recorded",
                "ambiguous",
            }:
                return FamilyTeachingResult({"status": "unavailable"})
            observed = timestamp(raw.get("as_of")) or at
            teachers = _teachers(raw.get("related_teachers", []), include_official_contacts)
            if status in {"conflict", "ambiguous"}:
                public = {"status": status, "as_of": observed.isoformat()}
            else:
                public = teaching_view(
                    records=raw.get("records", []),
                    teachers=teachers,
                    at=observed,
                    subject_name=subject_name,
                    include_contacts=include_official_contacts,
                    enrollment_status=raw.get("enrollment_status"),
                    quality=_quality(
                        raw.get("quality", []),
                        school_id=actor.school_id,
                        records=raw.get("records", []),
                        at=observed,
                        school_fact_version=raw.get("school_fact_version"),
                    ),
                    group_names=_groups(raw.get("related_groups", [])),
                )
                public["student_participation"] = _student_participation(
                    raw.get("enrollment_status")
                )
                if status == "period_not_recorded":
                    public["period_status"] = status
                    public["period_meaning"] = (
                        "学校尚未登记可定位的学期日期；以下仅说明现有持续安排，不能确认整个学期。"
                    )
                if raw.get("period_end"):
                    public["period_end"] = raw["period_end"]
                    public["query_window"] = {
                        "start": raw.get("period_start") or observed.isoformat(),
                        "end_exclusive": raw["period_end"],
                        "meaning": "查询期间包含起点、不包含终点；不改变各项事实自身的有效期。",
                    }
                timeline = _period_view(
                    raw,
                    observed=observed,
                    subject_name=subject_name,
                    contacts=include_official_contacts,
                    school_id=actor.school_id,
                )
                if timeline is not None:
                    public["timeline"] = timeline
                    public["status"] = (
                        "conflict"
                        if any(part["status"] == "conflict" for part in timeline)
                        else "available"
                        if any(part["items"] for part in timeline)
                        else "no_current_records"
                    )
                    public["items_meaning"] = "查询期间首段安排；各时间段列出期间内已登记的变化。"
                if raw.get("period_limited_to_authorized") is True:
                    public["period_meaning"] = (
                        "只查询了本学期内您获准查看的期间，不能代表整个学期的安排。"
                    )
                    public["period_limited_to_authorized"] = True
                public["sources"] = [
                    {
                        "fact": r["fact_kind"],
                        "source": deepcopy(s.get("source", {})),
                        "effective_at": s["effective_at"],
                        "valid_to": s.get("valid_to"),
                        "recorded_at": s.get("recorded_at"),
                    }
                    for projection in ([raw] + raw.get("period_slices", []))
                    for r in projection.get("records", [])
                    for s in r.get("segments", [])
                    if timestamp(s["effective_at"])
                    <= (timestamp(projection.get("as_of")) or observed)
                    and (
                        timestamp(s.get("valid_to")) is None
                        or (timestamp(projection.get("as_of")) or observed)
                        < timestamp(s["valid_to"])
                    )
                ]
            if observed > self.clock() or period == "next_term":
                public["future_basis"] = "按目前已登记的安排"
            public["kind"] = "family_teaching"
            zones = {
                s["content"]["timezone_name"]
                for projection in [raw] + raw.get("period_slices", [])
                for record in projection.get("records", [])
                if record.get("fact_kind") == "education_calendar_rule"
                for s in record.get("segments", [])
                if s.get("content", {}).get("timezone_name")
            }
            if len(zones) == 1:
                zone_name = next(iter(zones))
                ZoneInfo(zone_name)
                public["fact_timezone_name"] = zone_name
            version = raw.get("school_fact_version")
            boundary = timestamp(raw.get("next_boundary"))
            if type(version) is not int or version < 1:
                raise ValueError("Missing teaching dependency version")

            def valid(current):
                # Every release checks live identity and guardian authority, including
                # model waits. A changed stamp/boundary forces a fresh narrow query.
                return (
                    current == actor
                    and self._basis(current, observed) == basis
                    and (boundary is None or self.clock() < boundary)
                    and self.repository.teaching_stamp(current) == version
                    and self._basis(current, observed) == basis
                )

            if not valid(actor):
                return FamilyTeachingResult({"status": "unavailable"})
            valid_before = min(
                (value for value in (basis.valid_to, boundary) if value is not None), default=None
            )
            return FamilyTeachingResult(
                public, FamilyReadEvidence(valid, (self.clock, queried_at, valid_before))
            )
        except Exception:
            return FamilyTeachingResult({"status": "unavailable"})


def create_family_teaching_query_service(*, session_factory, current_actor: Callable):
    return FamilyTeachingQueryService(
        repository=FamilyTeachingRepository(session_factory),
        current_actor=current_actor,
    )
