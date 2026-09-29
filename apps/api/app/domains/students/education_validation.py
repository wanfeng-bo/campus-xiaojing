"""Finite school-published cardinality and derived class-number postconditions."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import cast
from zoneinfo import ZoneInfo

from .education_time import CalendarRule, GradeAnchor, clip_calendar_rule, resolve_grade


def _integer(value: object) -> int:
    if type(value) is not int:
        raise ValueError("正式教育规则的整数值无效。")
    return value


def _grade_keys(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise ValueError("正式年级序列无效。")
    values = cast("list[object] | tuple[object, ...]", value)
    if not all(isinstance(item, str) for item in values):
        raise ValueError("正式年级引用无效。")
    return tuple(cast(str, item) for item in values)


@dataclass(frozen=True)
class EducationRecord:
    kind: str
    key: str
    start: datetime
    end: datetime | None
    content: Mapping[str, object]

    def active(self, at: datetime) -> bool:
        return self.start <= at and (self.end is None or at < self.end)


def validate_education_rules(rows: tuple[EducationRecord, ...]) -> None:
    classes: dict[str, list[EducationRecord]] = defaultdict(list)
    calendars: dict[str, list[EducationRecord]] = defaultdict(list)
    for row in rows:
        if row.kind == "education_class":
            classes[row.key].append(row)
        elif row.kind in {
            "class_teacher_assignment",
            "class_location",
            "class_position_definition",
            "student_class_position",
        }:
            classes[str(row.content["class_source_key"])].append(row)
        if row.kind == "education_calendar_rule":
            calendars[row.key].append(row)
        elif row.kind == "class_placement":
            calendars[str(row.content["calendar_source_key"])].append(row)
    for group in classes.values():
        if len(group) > 5000:
            raise ValueError("单班影响集超过已验证容量。")
        points = {row.start for row in group} | {row.end for row in group if row.end is not None}
        for at in points:
            active = [row for row in group if row.active(at)]
            roots = [row for row in active if row.kind == "education_class"]
            teachers = [row for row in active if row.kind == "class_teacher_assignment"]
            places = [
                row
                for row in active
                if row.kind == "class_location"
                and row.content.get("location_purpose") == "main_classroom"
            ]
            positions = [row for row in active if row.kind == "student_class_position"]
            if (teachers or places or positions) and len(roots) != 1:
                raise ValueError("教育关系超出班集体有效期间。")
            if teachers:
                config = roots[0].content
                limits = config.get("teacher_role_limits")
                mode = config.get("teacher_mode")
                if not isinstance(limits, dict) or mode not in {"single_primary", "joint"}:
                    raise ValueError("班主任人数与共同模式口径尚未正式登记。")
                for role in {row.content["teacher_role"] for row in teachers}:
                    count = sum(row.content["teacher_role"] == role for row in teachers)
                    if role not in limits or count > limits[role]:
                        raise ValueError("班主任人数超过学校该岗位的明确上限。")
                if mode == "single_primary" and any(
                    row.content["teacher_role"] == "joint" for row in teachers
                ):
                    raise ValueError("单主班主任模式不能同时登记共同班主任。")
                if mode == "joint" and any(
                    row.content["teacher_role"] == "primary" for row in teachers
                ):
                    raise ValueError("共同班主任模式不能同时登记单主岗位。")
            if places:
                limit = roots[0].content.get("primary_classroom_limit")
                if not isinstance(limit, int) or len(places) > limit:
                    raise ValueError("主要教室数量缺少明确学校口径或超过上限。")
            definitions = {
                row.key: row for row in active if row.kind == "class_position_definition"
            }
            for position in positions:
                key = str(position.content["position_source_key"])
                definition = definitions.get(key)
                if definition is None:
                    raise ValueError("学生岗位未引用本班有效岗位定义。")
                peers = [row for row in positions if row.content["position_source_key"] == key]
                if len({row.content["student_source_key"] for row in peers}) != len(peers):
                    raise ValueError("同一学生在同一岗位存在重复任职。")
                limit = definition.content.get("max_holders")
                if not isinstance(limit, int) or len(peers) > limit:
                    raise ValueError("学生岗位人数超过正式上限。")
                if not definition.content.get("allow_concurrent") and any(
                    row.key != position.key
                    and row.content.get("student_source_key")
                    == position.content.get("student_source_key")
                    for row in positions
                ):
                    raise ValueError("学校该岗位不允许同时兼任。")
    for group in calendars.values():
        _class_numbers(tuple(group))


def _class_numbers(rows: tuple[EducationRecord, ...]) -> None:
    placements = [
        row
        for row in rows
        if row.kind == "class_placement" and row.content.get("class_number") is not None
    ]
    if not placements:
        return
    definitions = [row for row in rows if row.kind == "education_calendar_rule"]
    if not definitions:
        raise ValueError("班序唯一性所需正式学年规则缺失。")
    rules: list[CalendarRule] = []
    for row in definitions:
        p = row.content
        end_cycle = p.get("end_cycle")
        start_cycle = _integer(p["start_cycle"])
        candidate = clip_calendar_rule(
            CalendarRule(
                rule_id=row.key + ":" + row.start.isoformat(),
                version=1,
                start_cycle=start_cycle,
                end_cycle=_integer(end_cycle) if end_cycle is not None else None,
                month=_integer(p["month"]),
                day=_integer(p["day"]),
                timezone=str(p["timezone_name"]),
                grade_ids=_grade_keys(p["grade_source_keys"]),
                recorded_at=row.start,
            ),
            valid_to=row.end,
        )
        if candidate is not None:
            rules.append(candidate)
    if not rules:
        raise ValueError("班序唯一性所需有效学年规则缺失。")
    first = min(row.start.year for row in placements)
    last = (
        min(
            9998,
            max(row.start.year for row in placements)
            + max(len(rule.grade_ids) for rule in rules)
            + 1,
        )
        if rules
        else first
    )
    if last - first > 200 or len(placements) > 5000:
        raise ValueError("班序影响期间或班级数超过已验证容量。")
    points = {row.start for row in placements} | {
        row.end for row in placements if row.end is not None
    }
    for rule in rules:
        points.update(
            datetime(year, rule.month, rule.day, tzinfo=ZoneInfo(rule.timezone)).astimezone(UTC)
            for year in range(first, last + 1)
        )
    anchors: dict[str, list[GradeAnchor]] = defaultdict(list)
    for row in placements:
        anchors[str(row.content["class_source_key"])].append(
            GradeAnchor(
                anchor_id=row.key + ":" + row.start.isoformat(),
                version=1,
                effective_from=row.start,
                grade_id=str(row.content["grade_source_key"]),
                recorded_at=row.start,
            )
        )
    for at in sorted(points):
        occupied = {}
        for row in placements:
            if not row.active(at):
                continue
            class_key = str(row.content["class_source_key"])
            result = resolve_grade(at=at, rules=tuple(rules), anchors=tuple(anchors[class_key]))
            if result.reason == "stage_exhausted":
                # A normal open class becomes explicitly unknown beyond its final grade.
                continue
            if result.status != "resolved":
                raise ValueError("班序唯一性无法由完整正式年级规则确定。")
            identity = (
                row.content["campus_source_key"],
                result.cycle,
                result.grade_id,
                row.content["class_number"],
            )
            if identity in occupied and occupied[identity] != class_key:
                raise ValueError("同校区、学年与实际年级的班序重复。")
            occupied[identity] = class_key
