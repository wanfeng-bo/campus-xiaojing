"""Human review of the explicit batch roster; never a whole-class census."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Any


def summarize_regroup(impacts: list[dict[str, Any]]) -> dict[str, Any] | None:
    empty: dict[str, Any] = {}
    members = [i for i in impacts if i["fact_kind"] == "student_class_membership"]
    if any(
        i["operation"] == "cancel" or (i["operation"] == "correct" and i.get("after"))
        for i in members
    ):
        # Arbitrary history uses the full segment comparison, not one flat after row.
        return None
    if not any(i.get("after") for i in members):
        return None
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in members:
        groups[str(item["effective_at"])].append(item)
    periods: list[dict[str, Any]] = []
    for at, items in sorted(groups.items(), key=lambda p: datetime.fromisoformat(p[0])):
        students: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for item in items:
            student = (item.get("after") or item.get("before") or empty).get("student_ref")
            if not student:
                raise ValueError("本批归班学生尚未明确，不能生成名单摘要。")
            students[student].append(item)
        departures: list[dict[str, Any]] = []
        classes: set[str] = set()
        unassigned: list[str] = []
        for student, rows in sorted(students.items()):
            after = [r["after"] for r in rows if r.get("after")]
            if len(after) > 1:
                raise ValueError(f"本批学生重复列入归班安排：{student}。请合并明确唯一去向。")
            if not after:
                # Graduation/transfer-out are explicit school facts, not unassigned pupils.
                exits = [
                    i
                    for i in impacts
                    if i["fact_kind"] == "student_school_relationship"
                    and (i.get("after") or empty).get("student_ref") == student
                    and (i.get("after") or empty).get("enrollment_status")
                    in {"graduated", "transferred_out"}
                ]
                if exits:
                    continue
                raise ValueError(f"本批学生尚未明确新去向或正式待分班：{student}。")
            destination = after[0].get("class_ref")
            status = after[0].get("membership_status")
            if (status == "assigned" and not destination) or status not in {
                "assigned",
                "unassigned",
            }:
                raise ValueError(f"本批学生去向未明确：{student}。")
            if status == "unassigned":
                if destination:
                    raise ValueError("待分班不能同时指定新班。")
                unassigned.append(student)
            origins = {
                r["before"]["class_ref"]
                for r in rows
                if r.get("before") and r["before"].get("class_ref")
            }
            if len(origins) > 1:
                raise ValueError(f"本批学生原班存在重复：{student}。")
            origin = next(iter(origins), None)
            classes.update(v for v in (origin, destination) if v)
            departures.append(
                dict(
                    student_ref=student,
                    from_class_ref=origin,
                    to_class_ref=destination,
                    status=status,
                )
            )
        class_rows: list[dict[str, Any]] = []
        for key in sorted(classes):
            changes = [
                i
                for i in impacts
                if i["fact_kind"] == "education_class"
                and i["reference"] == key
                and str(i["effective_at"]) == at
            ]
            change = changes[0] if len(changes) == 1 else None
            state = (
                "unchanged"
                if change is None
                else "ended"
                if change.get("after") is None
                else "recorded"
            )
            class_fields = (
                (change or empty).get("after") or (change or empty).get("before") or empty
            )
            label = class_fields.get("display_name")
            class_rows.append(
                dict(
                    class_ref=key,
                    label=label,
                    class_state=state,
                    outgoing_count=sum(d["from_class_ref"] == key for d in departures),
                    incoming_count=sum(d["to_class_ref"] == key for d in departures),
                )
            )
        periods.append(
            dict(
                effective_at=at,
                student_count=len(departures),
                departures=departures,
                classes=class_rows,
                unassigned=unassigned,
                duplicates=[],
            )
        )
    return dict(
        scope="batch_only",
        notice="人数仅统计本批明确名单，未列入的正式名单保持原安排；不代表全班人数或全班已处理。",
        periods=periods,
    )
