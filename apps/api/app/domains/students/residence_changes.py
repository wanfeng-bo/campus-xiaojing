"""Plan explicit residence units from an already authorized, complete impact snapshot.

This pure planner grants no access and never persists or allocates a bed. The caller
must bind and recheck the snapshot and permissions before the unique Writer confirms.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from app.platform.school_facts.contracts import CanonicalFactChangeV4


def residence_candidate_issues(
    *, rooms, beds, enrollments, residences, issue_targets=None
) -> tuple[str, ...]:
    """Check the read-only candidate intervals; PostgreSQL remains the write authority."""
    from collections import defaultdict

    def covers(periods, start, end):
        cursor = start
        for row in sorted(periods, key=lambda s: s.start):
            if row.end is not None and row.end <= cursor:
                continue
            if row.start > cursor:
                return False
            if row.end is None or end is not None and row.end >= end:
                return True
            cursor = row.end
        return False

    room_rows, available_rooms, eligible, occupied = (defaultdict(list) for _ in range(4))
    issues = set()

    def note(code, *targets):
        issues.add(code)
        if issue_targets is not None:
            issue_targets.setdefault(code, set()).update(targets)

    for rows in rooms.values():
        for row in rows:
            room_rows[row.payload["room_source_key"]].append(row)
            if row.payload["is_available"]:
                available_rooms[row.payload["room_source_key"]].append(row)
    for rows in enrollments.values():
        for row in rows:
            if (
                row.payload["enrollment_status"] in {"enrolled", "suspended"}
                and row.payload["residence_mode"] == "boarding"
            ):
                eligible[row.payload["student_source_key"]].append(row)
    available_beds, beds_by_room = defaultdict(list), defaultdict(list)
    for key, rows in beds.items():
        if len({row.payload["room_source_key"] for row in rows}) > 1:
            note("residence.bed_identity_changed", ("dorm_bed", key))
        for row in rows:
            if row.payload["availability"] != "available":
                continue
            available_beds[key].append(row)
            beds_by_room[row.payload["room_source_key"]].append((key, row))
            if not covers(available_rooms[row.payload["room_source_key"]], row.start, row.end):
                note(
                    "residence.room_availability_requires_same_batch_resolution",
                    ("dorm_bed", key),
                    ("location", row.payload["room_source_key"]),
                )
    for room, profiles in room_rows.items():
        active = beds_by_room[room]
        for point in {row.start for _, row in active} | {row.start for row in profiles}:
            count = len(
                {
                    key
                    for key, row in active
                    if row.start <= point and (row.end is None or point < row.end)
                }
            )
            if any(
                p.start <= point
                and (p.end is None or point < p.end)
                and count > p.payload["authorized_bed_capacity"]
                for p in profiles
            ):
                note(
                    "residence.capacity_requires_same_batch_resolution",
                    ("location", room),
                    *(
                        ("dorm_bed", key)
                        for key, row in active
                        if row.start <= point and (row.end is None or point < row.end)
                    ),
                )
    for key, rows in residences.items():
        for row in rows:
            student, bed = row.payload["student_source_key"], row.payload["bed_source_key"]
            if not covers(eligible[student], row.start, row.end):
                note(
                    "residence.eligibility_requires_same_batch_resolution",
                    ("student", student),
                    ("student_bed_residence", key),
                )
            if not covers(available_beds[bed], row.start, row.end):
                note(
                    "residence.bed_availability_requires_same_batch_resolution",
                    ("dorm_bed", bed),
                    ("student_bed_residence", key),
                )
            occupied["student", student].append((key, row))
            occupied["bed", bed].append((key, row))
    for (kind, _), rows in occupied.items():
        ordered = sorted(rows, key=lambda pair: pair[1].start)
        for (_key, row), (_other, next_row) in zip(ordered, ordered[1:], strict=False):
            if row.end is None or next_row.start < row.end:
                note(
                    f"residence.{kind}_overlap_requires_same_batch_resolution",
                    ("student_bed_residence", _key),
                    ("student_bed_residence", _other),
                )
    return tuple(sorted(issues))


@dataclass(frozen=True, slots=True)
class ResidenceFactSnapshot:
    source_key: str
    version: int
    student_source_key: str
    bed_source_key: str
    valid_from: datetime
    valid_to: datetime | None
    revoked_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class BoardingEligibilitySlice:
    valid_from: datetime
    valid_to: datetime | None
    enrollment_status: str
    residence_mode: str | None


def plan_residence_eligibility_change(
    *,
    student_source_key: str,
    snapshots: tuple[ResidenceFactSnapshot, ...],
    eligibility: tuple[BoardingEligibilitySlice, ...],
) -> tuple[CanonicalFactChangeV4, ...]:
    """Close each stay at its first eligibility gap; later boarding never restores it."""
    for row in (*snapshots, *eligibility):
        if (
            row.valid_from.utcoffset() is None
            or row.valid_to is not None
            and (row.valid_to.utcoffset() is None or row.valid_to <= row.valid_from)
        ):
            raise ValueError("完整住宿影响必须包含明确且有效的时间区间。")
    if any(row.student_source_key != student_source_key for row in snapshots):
        raise ValueError("完整住宿影响包含其他学生。")
    if len({row.source_key for row in snapshots}) != len(snapshots):
        raise ValueError("完整住宿影响有重复身份。")
    if any(row.residence_mode not in {None, "boarding", "day"} for row in eligibility):
        raise ValueError("未登记的住校属性。")
    eligible = sorted(
        (
            row
            for row in eligibility
            if row.enrollment_status in {"enrolled", "suspended"}
            and row.residence_mode == "boarding"
        ),
        key=lambda row: row.valid_from,
    )
    unknown = tuple(
        row
        for row in eligibility
        if row.enrollment_status in {"enrolled", "suspended"} and row.residence_mode is None
    )
    result = []
    for stay in sorted(snapshots, key=lambda row: (row.valid_from, row.source_key)):
        if stay.revoked_at is not None:
            continue
        cursor = stay.valid_from
        covered = False
        for row in eligible:
            if row.valid_to is not None and row.valid_to <= cursor:
                continue
            if row.valid_from > cursor:
                break
            if row.valid_to is None or stay.valid_to is not None and row.valid_to >= stay.valid_to:
                covered = True
                break
            cursor = row.valid_to
        if covered:
            continue
        if any(
            row.valid_from <= cursor and (row.valid_to is None or cursor < row.valid_to)
            for row in unknown
        ):
            raise ValueError("住校属性未知，必须显式处理已有住宿，不能推定退宿。")
        result.extend(
            plan_residence_change(
                operation="revoke" if cursor == stay.valid_from else "end",
                student_source_key=student_source_key,
                at=cursor,
                snapshots=(stay,),
                target_source_key=stay.source_key,
            )
        )
    return tuple(result)


def plan_residence_change(
    *,
    operation: str,
    student_source_key: str,
    at: datetime,
    snapshots: tuple[ResidenceFactSnapshot, ...],
    target_source_key: str | None = None,
    new_source_key: str | None = None,
    bed_source_key: str | None = None,
    valid_to: datetime | None = None,
    enrollment_status: str | None = None,
    residence_mode: str | None = None,
) -> tuple[CanonicalFactChangeV4, ...]:
    if at.tzinfo is None or (valid_to is not None and (valid_to.tzinfo is None or valid_to <= at)):
        raise ValueError("住宿变更必须有明确且有效的时间区间。")
    if operation not in {"publish", "move", "end", "revoke", "lifecycle"}:
        raise ValueError("未登记的住宿维护操作。")
    if any(s.student_source_key != student_source_key for s in snapshots):
        raise ValueError("住宿影响清单包含其他学生。")
    if len({s.source_key for s in snapshots}) != len(snapshots):
        raise ValueError("住宿影响清单有重复身份。")
    changes = []

    def unit(op, key, version, content=None):
        changes.append(
            CanonicalFactChangeV4.model_validate(
                dict(
                    operation=op,
                    fact_kind="student_bed_residence",
                    source_key=key,
                    expected_version=version,
                    effective_at=at,
                    origin={"sheet_code": "residence_maintenance", "row_number": len(changes) + 1},
                    content=content,
                )
            )
        )

    active = tuple(
        s for s in snapshots if s.revoked_at is None and (s.valid_to is None or s.valid_to > at)
    )
    if operation == "lifecycle":
        if enrollment_status not in {
            "enrolled",
            "suspended",
            "transferred_out",
            "withdrawn",
            "graduated",
            "ended",
        }:
            raise ValueError("在校状态未确定，不能推导住宿影响。")
        if enrollment_status in {"enrolled", "suspended"} and residence_mode == "boarding":
            return ()
        if residence_mode is None and active:
            raise ValueError("住校属性改为未知前，必须显式核对并处理已有住宿，不能推定退宿。")
        for row in sorted(active, key=lambda s: (s.valid_from, s.source_key)):
            unit("revoke" if row.valid_from >= at else "end", row.source_key, row.version)
        return tuple(changes)
    if operation in {"move", "end", "revoke"}:
        matches = tuple(
            s for s in snapshots if s.source_key == target_source_key and s.revoked_at is None
        )
        if len(matches) != 1:
            raise ValueError("住宿目标已变化或不在当前获准影响清单。")
        old = matches[0]
        if operation != "revoke" and (
            at <= old.valid_from or old.valid_to is not None and at >= old.valid_to
        ):
            raise ValueError("结束或换床时间必须位于目标住宿的有效区间内。")
        unit("revoke" if operation == "revoke" else "end", old.source_key, old.version)
    if operation in {"publish", "move"}:
        if (
            not new_source_key
            or not bed_source_key
            or new_source_key in {s.source_key for s in snapshots}
        ):
            raise ValueError("新住宿必须使用明确的新稳定标识及目标床位。")
        if operation == "move" and valid_to is None:
            valid_to = old.valid_to
        content = dict(student_source_key=student_source_key, bed_source_key=bed_source_key)
        if valid_to is not None:
            content["valid_to"] = valid_to
        unit("upsert", new_source_key, 0, content)
    return tuple(changes)
