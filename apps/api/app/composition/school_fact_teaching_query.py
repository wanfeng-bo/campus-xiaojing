"""Finite teaching compositions over already authorized, published projections.

This module does not infer a teaching relationship from names or grant access.
Callers supply the exact authorized offering closure at the requested factual time.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.composition.school_fact_education_query import active_segment
from app.composition.school_fact_education_reader import aware, timestamp


@dataclass(frozen=True)
class TeachingPeriod:
    status: str
    start: datetime | None = None
    end: datetime | None = None
    label: str | None = None


def publication_basis(segment):
    source = segment.get("source", {})
    label = source.get("summary") or source.get("label")
    return {
        "label": label if isinstance(label, str) and label else "学校正式发布的登记",
        "published_at": source.get("published_at"),
        "recorded_at": segment.get("recorded_at"),
    }


def teaching_quality(rows, *, offering_key, teachers, at, target_source_key=None):
    statuses, notices, evidence = {}, [], []
    conflict = False
    for statement in rows:
        if (
            statement.get("offering_source_key") != offering_key
            or statement.get("target_source_key") != target_source_key
        ):
            continue
        start, end = timestamp(statement.get("effective_at")), timestamp(statement.get("valid_to"))
        if start is None or at < start or end is not None and at >= end:
            continue
        role = {"lead": "主讲", "co_teacher": "共同任教"}.get(statement.get("teaching_role"))
        if (
            statement.get("quality_kind") == "negative"
            and statement.get("negative_code") == "no_teaching_assignment"
            and role
        ):
            if any(t["role"] == role for t in teachers):
                conflict = True
                notices.append(f"{role}的空缺声明与已登记任课资料不一致，需要学校核对。")
            else:
                statuses[role] = "学校明确未安排"
        if statement.get("dispute_status") == "open":
            conflict = True
            notices.append("这项教学安排有尚未解决的争议。")
        if statement.get("source_status") == "unavailable":
            notices.append("这项声明记载原资料来源目前不可用。")
        if (
            statement.get("quality_kind") == "coverage"
            and statement.get("coverage_status") == "complete"
        ):
            # Authorized offering rows are not proof of the whole mother set.
            # The caller retains original statements in restricted detail; public
            # evidence must not repeat an unverified complete claim or its label.
            continue
        evidence.append(
            {
                "applies_to_role": role
                or ("本条任课资料" if target_source_key else "该开课的所指字段"),
                "kind": {
                    "negative": "明确未安排",
                    "coverage": "登记范围说明",
                    "dispute": "争议说明",
                    "source": "来源情况",
                    "verification": "核实记录",
                }.get(statement.get("quality_kind"), "资料质量说明"),
                **(
                    {"coverage": statement["coverage_status"]}
                    if statement.get("quality_kind") == "coverage"
                    and statement.get("coverage_status") is not None
                    else {}
                ),
                **{
                    field: statement[field]
                    for field in ("verified_at", "review_due_at")
                    if statement.get(field) is not None
                },
                **(
                    {"status": "这条资料存在尚未解决的争议"}
                    if statement.get("dispute_status") == "open"
                    else {
                        "status": {"available": "来源可用", "unavailable": "来源暂不可用"}[
                            statement["source_status"]
                        ]
                    }
                    if statement.get("source_status") in {"available", "unavailable"}
                    else {}
                ),
                "basis": publication_basis(statement),
            }
        )
    return statuses, notices, evidence, conflict


def resolve_teaching_period(
    records: tuple[dict[str, Any], ...], *, now: datetime, period: str
) -> TeachingPeriod:
    """Use published term dates; never manufacture next term by adding months."""
    aware(now)
    if period == "current":
        return TeachingPeriod("available", now)
    if period not in {"current_term", "next_term"}:
        raise ValueError("Unsupported teaching period")
    terms = []
    for record in records:
        if record.get("fact_kind") != "education_term":
            continue
        for segment in record.get("segments", ()):
            start, end = timestamp(segment.get("effective_at")), timestamp(segment.get("valid_to"))
            if start is None or end is None:
                continue
            terms.append((start, end, segment.get("content", {}).get("display_name")))
    current = [term for term in terms if term[0] <= now < term[1]]
    if len(current) > 1:
        return TeachingPeriod("ambiguous")
    if period == "current_term":
        selected = current
    else:
        # Without a known current term, “next term” has no authoritative anchor.
        if not current:
            return TeachingPeriod("period_not_recorded")
        future = [term for term in terms if term[0] >= current[0][1]]
        selected = (
            [term for term in future if term[0] == min(t[0] for t in future)] if future else []
        )
    if not selected:
        return TeachingPeriod("period_not_recorded")
    if len(selected) != 1:
        return TeachingPeriod("ambiguous")
    start, end, label = selected[0]
    return TeachingPeriod("available", start, end, label)


def teaching_view(
    *,
    records: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    teachers: dict[str, dict[str, Any]],
    at: datetime,
    subject_name: str | None = None,
    include_contacts: bool = False,
    enrollment_status: str | None = None,
    quality: list[dict[str, Any]] | tuple[dict[str, Any], ...] = (),
    group_names: dict[str, str] | None = None,
    object_coordinates: Callable[[str, str], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Retain courses, teaching roles and evidence ownership, including empty sets."""
    aware(at)
    active = {
        record["source_key"]: (record, segment)
        for record in records
        if (segment := active_segment(record, at)) is not None
    }
    subjects = {
        key: segment["content"]
        for key, (record, segment) in active.items()
        if record["fact_kind"] == "teaching_subject"
    }
    courses = {
        key: segment["content"]
        for key, (record, segment) in active.items()
        if record["fact_kind"] == "teaching_course"
    }
    matches = {
        key
        for key, content in subjects.items()
        if subject_name is None or content.get("display_name") == subject_name
    }
    course_subjects: dict[str, set[str]] = {}
    for record, segment in active.values():
        if record["fact_kind"] == "course_subject":
            content = segment["content"]
            course_subjects.setdefault(content["course_source_key"], set()).add(
                content["subject_source_key"]
            )
    items = []
    for offering_key, (record, segment) in active.items():
        if record["fact_kind"] != "teaching_offering":
            continue
        content = segment["content"]
        course_key = content["course_source_key"]
        selected_subjects = course_subjects.get(course_key, set()) & matches
        if subject_name is not None and not selected_subjects:
            continue
        course = courses.get(course_key)
        if course is None:
            # A relationship without its authorized title is still not a named course.
            raise ValueError("Teaching projection has an unresolved course")
        staff = []
        for relation, assignment_segment in active.values():
            if relation["fact_kind"] != "teaching_assignment":
                continue
            assignment = assignment_segment["content"]
            if assignment["offering_source_key"] != offering_key:
                continue
            division = set(
                assignment.get("subject_source_keys") or course_subjects.get(course_key, ())
            )
            if subject_name is not None and not division & selected_subjects:
                continue
            person = teachers.get(assignment["assignment_source_key"])
            teacher = {
                "name": person.get("name") if person else None,
                "role": {"lead": "主讲", "co_teacher": "共同任教"}[assignment["teaching_role"]],
                "subjects": [
                    subjects[key]["display_name"] for key in sorted(division) if key in subjects
                ],
                "effective_from": assignment_segment["effective_at"],
                "effective_to": assignment_segment.get("valid_to"),
                "basis": publication_basis(assignment_segment),
            }
            if object_coordinates is not None:
                teacher.update(object_coordinates("teaching_assignment", relation["source_key"]))
            if person is None or not person.get("name"):
                teacher["status"] = "name_not_available"
            if assignment.get("duty_scope"):
                teacher["responsibility"] = assignment["duty_scope"]
            if any(
                statement.get("verified_exact_complete") is True
                and statement.get("offering_source_key") == offering_key
                and statement.get("target_source_key") == relation["source_key"]
                and (start := timestamp(statement.get("effective_at"))) is not None
                and start <= at
                and ((end := timestamp(statement.get("valid_to"))) is None or at < end)
                for statement in quality
            ):
                teacher["quality"] = {"record_coverage": "complete"}
            _, row_notices, row_declarations, row_conflict = teaching_quality(
                quality,
                offering_key=offering_key,
                teachers=[teacher],
                at=at,
                target_source_key=relation["source_key"],
            )
            if row_declarations:
                teacher["quality_declarations"] = row_declarations
            if row_notices:
                teacher["notices"] = list(dict.fromkeys(row_notices))
            if row_conflict:
                teacher["status"] = "conflict"
            if include_contacts:
                teacher["official_contacts"] = (
                    list(person.get("official_contacts", ())) if person else []
                )
            staff.append(teacher)
        role_status, notices, declarations, conflict = teaching_quality(
            quality, offering_key=offering_key, teachers=staff, at=at
        )
        item = {
            "course": course["display_name"],
            "offering": content.get("display_name"),
            "subjects": [
                subjects[key]["display_name"]
                for key in sorted(course_subjects.get(course_key, ()))
                if key in subjects
            ],
            "teachers": staff,
            "teacher_status": "conflict"
            if conflict or any(t.get("status") == "conflict" for t in staff)
            else "available"
            if staff
            else "no_current_records",
            "offering_status": "暂停"
            if content.get("offering_status") == "suspended"
            else "持续开设",
            "effective_from": segment["effective_at"],
            "effective_to": segment.get("valid_to"),
            "basis": publication_basis(segment),
        }
        if object_coordinates is not None:
            item.update(object_coordinates("teaching_offering", offering_key))
        if role_status:
            item["role_status"] = role_status
        if declarations:
            item["quality"] = declarations
        if notices:
            item["notices"] = list(dict.fromkeys(notices))
        audiences = []
        for source_record, source_segment in active.values():
            if (
                source_record["fact_kind"] != "offering_member_source"
                or source_segment["content"].get("offering_source_key") != offering_key
            ):
                continue
            scope = source_segment["content"]
            if scope["source_kind"] == "administrative_class":
                target = active.get(scope["class_source_key"])
                label = target[1]["content"].get("display_name") if target else None
                audiences.append({"name": label, "source": "行政班实时成员"})
            else:
                audiences.append(
                    {
                        "name": (group_names or {}).get(scope["group_source_key"]),
                        "source": "独立教学名单，需单独维护",
                    }
                )
        if audiences:
            item["audiences"] = audiences
        items.append(item)
    result = {
        "status": "conflict"
        if any(i["teacher_status"] == "conflict" for i in items)
        else "available"
        if items
        else "no_current_records",
        "as_of": at.isoformat(),
        "items": items,
        "observed_count": len(items),
        "scope": "本次获准范围中，与所问时点及条件相符的正式教学安排。",
    }
    if enrollment_status == "suspended":
        result["participation"] = "休学期间保留的教学关联不能证明实际参加课程。"
    return result
