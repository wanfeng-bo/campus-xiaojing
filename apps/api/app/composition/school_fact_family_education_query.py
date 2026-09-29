"""Selected-child education uses its own finite future-capable SQL boundary."""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text

from app.composition.school_fact_education_query import active_segment, resolve_grade_from_records
from app.composition.school_fact_family_query import SqlFamilyReadRepository
from app.domains.students.family_read import FamilyReadEvidence, ParentReadBasis
from app.platform.contracts import ActorContext, GuardianActorContext
from app.platform.school_facts.f7_coverage_basis import verify_coverage_basis

FIELDS = frozenset(
    {"class", "grade", "teachers", "classroom", "positions", "cohorts", "official_contacts"}
)
_EDUCATION = text("""SELECT app.school_fact_family_education(
 :school,:adult,:relation,:version,:factual_at,CAST(:fields AS text[]))""")


class FamilyEducationRepository(SqlFamilyReadRepository):
    def family_read(
        self, actor: GuardianActorContext, *, factual_at: datetime, fields: tuple[str, ...]
    ) -> dict[str, Any]:
        with self.sessions() as session:
            raw = session.execute(
                _EDUCATION,
                self._parameters(actor) | {"factual_at": factual_at, "fields": list(fields)},
            ).scalar_one()
            if type(raw) is not dict:
                raise ValueError("Invalid education projection")
            return raw


@dataclass(frozen=True)
class FamilyEducationResult:
    payload: dict[str, Any]
    evidence: FamilyReadEvidence | None = None

    def public(self) -> dict[str, Any]:
        return deepcopy(self.payload)


def _fingerprint(value: Any) -> Any:
    # SQL's current contact observation clock is not a data-version change.
    if isinstance(value, dict):
        return {k: _fingerprint(v) for k, v in value.items() if k not in {"contact_at", "as_of"}}
    if isinstance(value, list):
        return [_fingerprint(v) for v in value]
    return value


def personal_grade_is_resolved(arrangement: dict[str, Any], anchor: str | None) -> bool:
    status = arrangement.get("enrollment_status")
    if status not in {"enrolled", "suspended"}:
        return False
    cutoff = (
        arrangement.get("effective_at")
        if status == "suspended"
        else arrangement.get("last_suspended_until")
    )
    if cutoff is None:
        return status == "enrolled"
    boundary = datetime.fromisoformat(cutoff)
    if status == "enrolled" and arrangement.get("effective_at"):
        boundary = max(boundary, datetime.fromisoformat(arrangement["effective_at"]))
    return anchor is not None and datetime.fromisoformat(anchor) >= boundary


def _quality_year_matches(raw: dict[str, Any], statement: dict[str, Any], at: datetime) -> bool:
    year = statement.get("academic_year")
    if year is None:
        return True
    records = raw.get("records", [])
    indexed = {row["source_key"]: row for row in records}
    owner_key = statement.get("education_target_source_key")
    owner_kind = statement.get("education_target_kind")
    owner = indexed.get(owner_key)
    owner_segment = active_segment(owner, at) if owner is not None else None
    class_key = (
        owner_key
        if owner_kind == "education_class"
        else (owner_segment["content"].get("class_source_key") if owner_segment else None)
    )
    placements = []
    for record in records:
        segment = active_segment(record, at)
        if segment is None:
            continue
        if record["fact_kind"] == "class_placement" and (
            record["source_key"] == owner_key
            or class_key is not None
            and segment["content"].get("class_source_key") == class_key
        ):
            placements.append((record, segment))
        elif record["fact_kind"] == "student_grade_placement" and record["source_key"] == owner_key:
            placements.append((record, segment))
    if len(placements) != 1:
        return False
    record, segment = placements[0]
    calendar = indexed.get(segment["content"].get("calendar_source_key"))
    if calendar is None or not segment["content"].get("grade_source_key"):
        return False
    resolution = resolve_grade_from_records(
        at=at, calendars=(calendar,), placement_record=record, placement=segment
    )
    return resolution.cycle is not None and resolution.cycle == year


_DIRECT_CHILD_ITEMS = {
    "class": ("student_class_membership",),
    "grade": ("student_grade_placement", "class_placement"),
    "teachers": ("class_teacher_assignment",),
    "classroom": ("class_location",),
    "positions": ("student_class_position",),
    "cohorts": ("student_cohort_membership",),
}


def _selected_item_position(
    raw: dict[str, Any],
    row: dict[str, Any],
    profile: dict[str, Any],
    at: datetime,
) -> int | None:
    field = row["field"]
    allowed = _DIRECT_CHILD_ITEMS.get(field, ())
    if row.get("scope") != "item" or row.get("target_fact_kind") not in allowed:
        return None
    items = [
        record
        for record in raw.get("records", ())
        if record.get("fact_kind") in allowed and active_segment(record, at) is not None
    ]
    displayed = profile.get(field)
    if not isinstance(displayed, list) or len(displayed) != len(items):
        return None
    matches = [
        index
        for index, record in enumerate(items)
        if record.get("fact_kind") == row.get("target_fact_kind")
        and record.get("source_key") == row.get("target_source_key")
    ]
    return matches[0] if len(matches) == 1 else None


def _verified_exact_item_position(
    raw: dict[str, Any],
    row: dict[str, Any],
    profile: dict[str, Any],
    at: datetime,
    school_id: UUID | None,
) -> int | None:
    """Prove one selected child-visible fact, never a whole class or field."""
    field = row["field"]
    kind = row.get("target_fact_kind")
    target_key = row.get("target_source_key")
    owner_key = row.get("education_target_source_key")
    allowed = _DIRECT_CHILD_ITEMS.get(field, ())
    if (
        school_id is None
        or kind not in allowed
        or not isinstance(target_key, str)
        or not isinstance(owner_key, str)
        or row.get("scope") != "item"
        or not isinstance(profile.get(field), list)
        or type(raw.get("school_fact_version")) is not int
    ):
        return None
    active = [(record, active_segment(record, at)) for record in raw.get("records", ())]
    active = [(record, segment) for record, segment in active if segment is not None]
    owner = [
        (record, segment)
        for record, segment in active
        if record.get("fact_kind") == row.get("education_target_kind")
        and record.get("source_key") == owner_key
    ]
    items = [(record, segment) for record, segment in active if record.get("fact_kind") in allowed]
    target = [
        (index, segment)
        for index, (record, segment) in enumerate(items)
        if record.get("fact_kind") == kind and record.get("source_key") == target_key
    ]
    if (
        len(owner) != 1
        or len(target) != 1
        or len(profile[field]) != len(items)
        or type(target[0][1].get("revision")) is not int
    ):
        return None
    index, segment = target[0]
    displayed = profile[field][index]
    if displayed is None or (
        isinstance(displayed, dict) and displayed.get("status") in {"unknown", "conflict"}
    ):
        return None
    if verify_coverage_basis(
        row.get("coverage_basis"),
        school_id=school_id,
        owner_kind="education",
        owner_source_key=owner_key,
        target_fact_kind=kind,
        target_source_key=target_key,
        field_name=row.get("field_name"),
        purpose=None,
        contact_kind=None,
        factual_at=at,
        school_fact_version=raw["school_fact_version"],
        members=((target_key, segment["revision"]),),
    ):
        return index
    return None


def _quality(
    raw: dict[str, Any],
    fields: tuple[str, ...],
    profile: dict[str, Any],
    at: datetime,
    *,
    school_id: UUID | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    rows = raw.get("quality", [])
    if not isinstance(rows, list) or len(rows) > 100:
        raise ValueError("Invalid child quality projection")
    selected: dict[str, list[dict[str, Any]]] = {field: [] for field in fields}
    sources: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict) or row.get("field") not in selected:
            raise ValueError("Quality exceeds requested child fields")
        start = datetime.fromisoformat(row["effective_at"])
        end = datetime.fromisoformat(row["valid_to"]) if row.get("valid_to") else None
        if start.tzinfo is None or (end is not None and end.tzinfo is None):
            raise ValueError("Quality has no factual timezone")
        if not (start <= at and (end is None or at < end)):
            continue
        if not _quality_year_matches(raw, row, at):
            continue
        verified = datetime.fromisoformat(row["verified_at"]) if row.get("verified_at") else None
        if verified is None or verified.tzinfo is None or verified > at:
            continue
        statement = {
            key: row[key]
            for key in (
                "quality_kind",
                "negative_code",
                "coverage_status",
                "dispute_status",
                "source_status",
                "review_due_at",
                "verified_at",
                "last_success_at",
                "effective_at",
                "valid_to",
                "version",
                "field_name",
                "scope",
                "academic_year",
            )
            if key in row
        }
        position = _selected_item_position(raw, row, profile, at)
        if position is not None:
            statement["item_index"] = position
            item = profile[row["field"]][position]
            if isinstance(item, dict) and statement.get("quality_kind") != "coverage":
                item.setdefault("quality_declarations", []).append(deepcopy(statement))
                if statement.get("field_name") is None:
                    if (
                        statement.get("quality_kind") == "dispute"
                        and statement.get("dispute_status") == "open"
                    ):
                        item["status"] = "conflict"
                    elif (
                        statement.get("quality_kind") == "source"
                        and statement.get("source_status") == "unavailable"
                    ):
                        item["status"] = "source_unavailable"
        # This selected-child projection does not prove the declaration's full
        # mother set. Keep the published statement available to the maintenance
        # owner, but do not tell a parent/model that this field is complete.
        if (
            statement.get("quality_kind") == "coverage"
            and statement.get("coverage_status") == "complete"
        ):
            position = _verified_exact_item_position(raw, row, profile, at, school_id)
            statement["coverage_status"] = "unknown"
            if position is not None:
                statement["record_coverage"] = "complete"
                statement["item_index"] = position
                item = profile[row["field"]][position]
                if isinstance(item, dict):
                    item["quality"] = {"record_coverage": "complete"}
        selected[row["field"]].append(statement)
        source = row.get("source", {})
        if not isinstance(source, dict):
            raise ValueError("Invalid quality source")
        sources.append(
            {
                key: source[key]
                for key in ("source_kind", "source_ref", "publication_reference", "published_at")
                if key in source
            }
            | {
                "fact": "fact_quality",
                "field": row["field"],
                "effective_at": row["effective_at"],
                "valid_to": row.get("valid_to"),
                "version": row.get("version"),
            }
        )
    result: dict[str, Any] = {}
    for field, statements in selected.items():
        # A per-object or per-column declaration is evidence, not whole-field completeness.
        general = [
            r
            for r in statements
            if r.get("field_name") is None and r.get("scope", "field") == "field"
        ]
        negatives = {r.get("negative_code") for r in general if r.get("quality_kind") == "negative"}
        values = profile.get(field)
        status = "known" if values else "unknown"
        if isinstance(values, list) and any(
            isinstance(v, dict) and v.get("status") == "unknown" for v in values
        ):
            status = "unknown"
        if isinstance(values, list) and any(
            isinstance(v, dict) and v.get("status") == "conflict" for v in values
        ):
            status = "conflict"
        if negatives:
            status = "conflict" if profile.get(field) or len(negatives) != 1 else "known_negative"
        if any(
            r.get("quality_kind") == "source" and r.get("source_status") == "unavailable"
            for r in general
        ):
            status = "source_unavailable"
        if any(
            r.get("quality_kind") == "dispute" and r.get("dispute_status") == "open"
            for r in general
        ):
            status = "conflict"
        coverage = {
            r.get("coverage_status") for r in general if r.get("quality_kind") == "coverage"
        }
        availability = {
            r.get("source_status") for r in general if r.get("quality_kind") == "source"
        }
        freshness = {
            "review_overdue"
            if datetime.fromisoformat(r["review_due_at"]) <= at
            else "within_review_period"
            for r in general
            if r.get("review_due_at")
        }
        result[field] = {
            "status": status,
            "source_availability": next(iter(availability))
            if len(availability) == 1
            else "unknown",
            "freshness": next(iter(freshness)) if len(freshness) == 1 else "unknown",
            "coverage": next(iter(coverage)) if len(coverage) == 1 else "unknown",
            "statements": statements,
        }
    return result, sources


def _public(
    raw: dict[str, Any],
    fields: tuple[str, ...],
    at: datetime,
    *,
    school_id: UUID | None = None,
) -> dict[str, Any]:
    records = raw.get("records", [])
    if not isinstance(records, list) or len(records) > 100:
        raise ValueError("Invalid education records")
    indexed = {r["source_key"]: r for r in records}
    active = [(r, active_segment(r, at)) for r in records]
    active = [(r, s) for r, s in active if s is not None]

    def label(key: str | None) -> str | None:
        record = indexed.get(key)
        segment = None if record is None else active_segment(record, at)
        return None if segment is None else segment["content"].get("display_name")

    profile: dict[str, Any] = {}
    if "class" in fields:
        profile["class"] = [
            label(s["content"].get("class_source_key"))
            for r, s in active
            if r["fact_kind"] == "student_class_membership"
        ]
    if "grade" in fields:
        placements = [(r, s) for r, s in active if r["fact_kind"] == "student_grade_placement"]
        personal_anchor = placements[0][1]["effective_at"] if len(placements) == 1 else None
        personal_resolved = personal_grade_is_resolved(
            raw.get("grade_arrangement", {}), personal_anchor
        )
        if not placements:
            placements = [(r, s) for r, s in active if r["fact_kind"] == "class_placement"]
        grades = []
        if not personal_resolved:
            placements = []
            grades.append(
                {"status": "unknown", "display_name": None, "reason": "个人年级安排待明确"}
            )
        for record, segment in placements:
            calendar = indexed.get(segment["content"].get("calendar_source_key"))
            if calendar is None or not segment["content"].get("grade_source_key"):
                grades.append({"status": "unknown", "display_name": None})
                continue
            resolution = resolve_grade_from_records(
                at=at, calendars=(calendar,), placement_record=record, placement=segment
            )
            grades.append(
                {
                    "status": resolution.status,
                    "display_name": label(resolution.grade_id),
                    "academic_year": resolution.cycle,
                    "next_boundary": None
                    if resolution.next_boundary is None
                    else resolution.next_boundary.isoformat(),
                }
            )
        profile["grade"] = grades
    if "teachers" in fields:
        names = {
            r["assignment_source_key"]: r.get("display_name")
            for r in raw.get("related_teachers", [])
        }
        profile["teachers"] = [
            {
                "display_name": names.get(s["content"].get("assignment_source_key")),
                "teacher_role": s["content"].get("teacher_role"),
            }
            for r, s in active
            if r["fact_kind"] == "class_teacher_assignment"
        ]
    if "classroom" in fields:
        names = {r["location_source_key"]: r.get("display_name") for r in raw.get("locations", [])}
        profile["classroom"] = [
            {
                "display_name": names.get(s["content"].get("location_source_key")),
                "purpose": s["content"].get("location_purpose"),
            }
            for r, s in active
            if r["fact_kind"] == "class_location"
        ]
    for field, kind, reference in [
        ("positions", "student_class_position", "position_source_key"),
        ("cohorts", "student_cohort_membership", "cohort_source_key"),
    ]:
        if field in fields:
            profile[field] = [
                label(s["content"].get(reference)) for r, s in active if r["fact_kind"] == kind
            ]
    if "official_contacts" in fields:
        profile["official_contacts"] = [
            {
                k: v
                for k, v in contact.items()
                if k
                in {"owner_kind", "contact_kind", "contact_value", "service_hours", "contact_at"}
            }
            for contact in raw.get("official_contacts", [])
        ]
    sources = [
        dict(s["source"])
        | {
            "fact": r["fact_kind"],
            "recorded_at": s.get("recorded_at"),
            "effective_at": s["effective_at"],
            "valid_to": s.get("valid_to"),
        }
        for r, s in active
        if isinstance(s.get("source"), dict)
    ]
    quality, quality_sources = _quality(raw, fields, profile, at, school_id=school_id)
    timezones = {
        s["content"].get("timezone_name")
        for r, s in active
        if r["fact_kind"] == "education_calendar_rule" and s["content"].get("timezone_name")
    }
    return {
        "status": raw["status"],
        "profile": profile,
        "authorized_fields": list(fields),
        "as_of": at.isoformat(),
        "sources": sources + quality_sources,
        "quality": quality,
        "fact_timezone_name": next(iter(timezones)) if len(timezones) == 1 else None,
        "coverage": "unknown",
    }


class FamilyEducationQueryService:
    def __init__(
        self,
        *,
        repository: FamilyEducationRepository,
        current_actor: Callable[[ActorContext], bool],
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.repository, self.current_actor, self.clock = repository, current_actor, clock

    def _basis(self, actor: ActorContext, at: datetime) -> ParentReadBasis | None:
        now = self.clock()
        if type(actor) is not GuardianActorContext or not self.current_actor(actor):
            return None
        basis = self.repository.authority(actor)
        if basis is None or not basis.permits(actor, now=now, factual_at=now):
            return None
        if (
            basis.relationship_kind != "verified_guardian"
            or at < basis.valid_from
            or (basis.valid_to is not None and at >= basis.valid_to)
        ):
            return None
        return basis

    def query(
        self, *, actor: ActorContext, fields: tuple[str, ...], as_of: datetime | None = None
    ) -> FamilyEducationResult:
        observed_at = self.clock()
        at = observed_at if as_of is None else as_of
        if at.tzinfo is None or not fields or len(fields) > 7 or not set(fields) <= FIELDS:
            raise ValueError("Invalid education request")
        try:
            basis = self._basis(actor, at)
            if basis is None:
                return FamilyEducationResult({"status": "denied"})
            raw = self.repository.family_read(actor, factual_at=at, fields=fields)
            if self._basis(actor, at) != basis:
                return FamilyEducationResult({"status": "denied"})
            if (
                str(raw.get("student_id")) != str(basis.student_id)
                or raw.get("student_version") != basis.student_version
                or raw.get("relationship_version") != basis.relationship_version
            ):
                raise ValueError("Selected child projection changed")
            if raw.get("status") not in {"available", "no_current_records"}:
                return FamilyEducationResult({"status": raw.get("status", "unavailable")})
            public = _public(raw, fields, at, school_id=actor.school_id)
            fingerprint = _fingerprint(raw)

            def valid(current: ActorContext) -> bool:
                check_at = self.clock() if as_of is None else at
                if current != actor or self._basis(current, check_at) != basis:
                    return False
                latest = self.repository.family_read(current, factual_at=check_at, fields=fields)
                return (
                    _fingerprint(latest) == fingerprint
                    and _fingerprint(_public(latest, fields, check_at, school_id=current.school_id))
                    == _fingerprint(public)
                    and self._basis(current, check_at) == basis
                )

            boundaries = [basis.valid_to] if basis.valid_to is not None else []
            if as_of is None:
                # These are exact transitions already present in this projection.
                # Fixed factual dates do not advance with the release clock.
                for row in [
                    *(
                        segment
                        for record in raw.get("records", [])
                        for segment in record.get("segments", [])
                    ),
                    *raw.get("quality", []),
                ]:
                    for field in ("effective_at", "valid_to", "verified_at", "review_due_at"):
                        if row.get(field):
                            boundary = datetime.fromisoformat(row[field])
                            if boundary > observed_at:
                                boundaries.append(boundary)
                boundaries.extend(
                    datetime.fromisoformat(grade["next_boundary"])
                    for grade in public["profile"].get("grade", [])
                    if grade.get("next_boundary")
                )
            return FamilyEducationResult(
                public,
                FamilyReadEvidence(valid, (self.clock, observed_at, min(boundaries, default=None))),
            )
        except Exception:
            return FamilyEducationResult({"status": "unavailable"})
