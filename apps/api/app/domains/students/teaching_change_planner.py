"""Finite teaching impact expansion over explicit, complete authorized snapshots.

Only the existing canonical Writer executes the returned plan. Read-only shared
impacts are dependencies, never an implicit grant to edit another offering.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime
from typing import Any, cast

from .education_change_planner import (
    EducationChangeCommand,
    EducationChangePlan,
    EducationFactSnapshot,
    EducationTimelineSegment,
    PlanningIssue,
    parse_education_time,
    plan_education_change,
)
from .teaching_contracts import TEACHING_KINDS


def _content(change: Mapping[str, Any]) -> Mapping[str, Any]:
    return cast(Mapping[str, Any], change.get("content") or {})


def _contents(record: EducationFactSnapshot) -> tuple[Mapping[str, Any], ...]:
    return tuple(s.content for s in record.segments) or (record.content,)


def cancel_teaching_future(
    change: Mapping[str, Any],
    snapshot: EducationFactSnapshot,
    *,
    proof: Mapping[str, Any],
    now: datetime,
) -> dict[str, Any]:
    """Undo one proven future slice without replacing unrelated current history."""
    boundary = parse_education_time(change["effective_at"])
    selected = [s for s in snapshot.segments if s.effective_at == boundary]
    if (
        change["fact_kind"] not in TEACHING_KINDS
        or change["operation"] != "cancel"
        or boundary is None
        or boundary <= now
        or len(selected) != 1
        or proof.get("version") != snapshot.version
        or change["expected_version"] != snapshot.version
        or proof.get("source_key") != snapshot.source_key
        or proof.get("fact_kind") != snapshot.fact_kind
        or parse_education_time(proof.get("selected_start")) != boundary
        or not proof.get("publication_reference")
        or proof.get("introduced_content") != dict(selected[0].content)
    ):
        raise ValueError("取消须选择尚未生效的明确切片并核对其原正式版本。")
    coordinates = proof.get(
        "cancellation_segments",
        ({"effective_at": selected[0].effective_at, "valid_to": selected[0].valid_to},),
    )
    targets: list[EducationTimelineSegment] = []
    for coordinate in coordinates:
        matches = [
            s
            for s in snapshot.segments
            if s.effective_at == parse_education_time(coordinate["effective_at"])
            and s.valid_to == parse_education_time(coordinate.get("valid_to"))
            and s.effective_at >= boundary
            and dict(s.content) == proof["introduced_content"]
        ]
        if len(matches) != 1 or matches[0] in targets:
            raise ValueError("取消期间与当前正式切片不一致，须重新核对。")
        targets.append(matches[0])
    if selected[0] not in targets:
        raise ValueError("取消证明缺少选中的未来切片。")
    rows = [s for s in snapshot.segments if s not in targets]
    restored_origins: dict[int, datetime] = {}
    for target in targets:
        for prior in proof.get("prior_segments", ()):
            prior_start = parse_education_time(prior["effective_at"])
            if prior_start is None:
                raise ValueError("取消缺少原正式期间。")
            start = max(target.effective_at, prior_start)
            ends = [
                v
                for v in (parse_education_time(prior.get("valid_to")), target.valid_to)
                if v is not None
            ]
            end = min(ends) if ends else None
            if end is not None and start >= end:
                continue
            restored = EducationTimelineSegment(
                effective_at=start,
                valid_to=end,
                content={k: v for k, v in prior["content"].items() if k != "valid_to"},
            )
            rows.append(restored)
            restored_origins[id(restored)] = (
                parse_education_time(prior.get("origin_start")) or prior_start
            )
    merged: list[EducationTimelineSegment] = []
    for row in sorted(rows, key=lambda s: s.effective_at):
        if merged and (merged[-1].valid_to is None or merged[-1].valid_to > row.effective_at):
            raise ValueError("取消恢复期间与独立安排冲突，须重新核对。")
        if (
            merged
            and id(row) in restored_origins
            and merged[-1].effective_at >= restored_origins[id(row)]
            and merged[-1].valid_to == row.effective_at
            and merged[-1].content == row.content
            and merged[-1].cause_source_key == row.cause_source_key
            and merged[-1].cause_version == row.cause_version
        ):
            merged[-1] = replace(merged[-1], valid_to=row.valid_to)
        else:
            merged.append(row)
    return {
        **change,
        "operation": "correct",
        "content": None,
        # Existing causal coordinates distinguish withdrawing an introduction from
        # independently publishing the same value. Never infer this from prose.
        "cause_source_key": snapshot.source_key,
        "cause_version": proof["introduction_version"],
        "correction_reason": (
            f"取消未来安排 {boundary.isoformat()}"
            f"（{proof['publication_reference']}）；{change.get('correction_reason') or ''}"
        ),
        "replacement_segments": [
            dict(
                effective_at=s.effective_at,
                valid_to=s.valid_to,
                content=dict(s.content),
                cause_source_key=s.cause_source_key,
                cause_version=s.cause_version,
            )
            for s in merged
        ],
    }


def cancel_teaching_dependents(
    change: Mapping[str, Any],
    snapshots: tuple[EducationFactSnapshot, ...],
    *,
    proof: Mapping[str, Any],
    explicit_keys: frozenset[str],
) -> tuple[dict[str, Any], ...]:
    """Restore only the unchanged cutoff caused by this original publication."""
    result: list[dict[str, Any]] = []
    for record in snapshots:
        rows: list[EducationTimelineSegment] = []
        changed = False
        for index, segment in enumerate(record.segments):
            provenance = segment.provenance
            if (
                provenance is None
                or provenance.cause_source_key != change["source_key"]
                or provenance.cause_version != proof["introduction_version"]
            ):
                rows.append(segment)
                continue
            if record.source_key in explicit_keys:
                rows.append(segment)
                continue
            if record.version != provenance.ended_target_version:
                raise ValueError("原联动截止已独立修改，须明确处置后再取消。")
            prior_end = parse_education_time(provenance.prior_content.get("valid_to"))
            successor = record.segments[index + 1] if index + 1 < len(record.segments) else None
            end = (
                min(
                    v
                    for v in (prior_end, successor.effective_at if successor else None)
                    if v is not None
                )
                if prior_end is not None or successor
                else None
            )
            rows.append(
                replace(
                    segment,
                    valid_to=end,
                    content={k: v for k, v in provenance.prior_content.items() if k != "valid_to"},
                    cause_source_key=change["source_key"],
                    cause_version=change["expected_version"] + 1,
                )
            )
            changed = True
        if changed:
            for left, right in zip(rows, rows[1:], strict=False):
                if left.valid_to is None or left.valid_to > right.effective_at:
                    raise ValueError("取消恢复与后续独立安排冲突，须明确完整影响。")
            result.append(
                dict(
                    schema_version=3,
                    fact_kind=record.fact_kind,
                    source_key=record.source_key,
                    operation="correct",
                    expected_version=record.version,
                    effective_at=change["effective_at"],
                    content=None,
                    correction_reason=change["correction_reason"],
                    origin=change["origin"],
                    cause_source_key=change["source_key"],
                    cause_version=change["expected_version"] + 1,
                    replacement_segments=[
                        dict(
                            effective_at=s.effective_at,
                            valid_to=s.valid_to,
                            content=dict(s.content),
                            cause_source_key=s.cause_source_key,
                            cause_version=s.cause_version,
                        )
                        for s in rows
                    ],
                )
            )
    return tuple(result)


def reschedule_teaching_future(
    change: Mapping[str, Any], snapshot: EducationFactSnapshot, *, from_at: datetime, now: datetime
) -> dict[str, Any]:
    """Move one explicit future slice; independent endings never grow implicitly."""
    to_at = parse_education_time(change["effective_at"])
    if to_at is None:
        raise ValueError("Explicit reschedule timestamp required")
    selected = [s for s in snapshot.segments if s.effective_at == from_at]
    if (
        change["fact_kind"] not in TEACHING_KINDS
        or change["operation"] != "correct"
        or from_at <= now
        or to_at <= now
        or len(selected) != 1
        or from_at == to_at
    ):
        raise ValueError("改期须明确唯一尚未生效安排及不同的新未来时点。")
    target = selected[0]
    end = target.valid_to
    if end is not None and to_at >= end:
        raise ValueError("改期不能越过原安排明确结束时间。")
    segments = tuple(
        sorted(
            (
                replace(
                    s,
                    effective_at=to_at,
                    content={
                        k: v
                        for k, v in (change.get("content") or s.content).items()
                        if k != "valid_to"
                    },
                )
                if s is target
                else s
                for s in snapshot.segments
            ),
            key=lambda s: s.effective_at,
        )
    )
    for left, right in zip(segments, segments[1:], strict=False):
        if left.valid_to is None or left.valid_to > right.effective_at:
            raise ValueError("改期与其他独立安排重叠，须显式处理相关期间。")
    return {
        **change,
        "content": None,
        "replacement_segments": [
            dict(
                effective_at=s.effective_at,
                valid_to=s.valid_to,
                content=dict(s.content),
                cause_source_key=s.cause_source_key,
                cause_version=s.cause_version,
            )
            for s in segments
        ],
    }


def reschedule_teaching_dependents(
    change: Mapping[str, Any],
    snapshots: tuple[EducationFactSnapshot, ...],
    *,
    from_at: datetime,
    explicit_keys: frozenset[str],
) -> tuple[dict[str, Any], ...]:
    """Only exact unchanged causal cutoffs may follow a future handoff."""
    result: list[dict[str, Any]] = []
    to_at = parse_education_time(change["effective_at"])
    if to_at is None:
        raise ValueError("Explicit reschedule timestamp required")
    for row in snapshots:
        segments: list[EducationTimelineSegment] = []
        changed = False
        for segment in row.segments:
            proof = segment.provenance
            if (
                proof is None
                or proof.cause_source_key != change["source_key"]
                or proof.cause_version != change["expected_version"]
            ):
                segments.append(segment)
                continue
            if row.source_key in explicit_keys:
                segments.append(segment)
                continue
            prior_end = parse_education_time(proof.prior_content.get("valid_to"))
            if (
                row.version != proof.ended_target_version
                or segment.valid_to != from_at
                or segment.effective_at != proof.prior_effective_at
                or segment.effective_at >= to_at
                or (prior_end is not None and prior_end < to_at)
            ):
                raise ValueError("原联动结束已独立调整或改期超出原期间，须重新明确处置。")
            segments.append(
                replace(
                    segment,
                    valid_to=to_at,
                    cause_source_key=change["source_key"],
                    cause_version=change["expected_version"] + 1,
                )
            )
            changed = True
        if changed:
            result.append(
                dict(
                    schema_version=3,
                    fact_kind=row.fact_kind,
                    source_key=row.source_key,
                    operation="correct",
                    expected_version=row.version,
                    effective_at=to_at,
                    content=None,
                    correction_reason=change["correction_reason"],
                    origin=change["origin"],
                    cause_source_key=change["source_key"],
                    cause_version=change["expected_version"] + 1,
                    replacement_segments=[
                        dict(
                            effective_at=s.effective_at,
                            valid_to=s.valid_to,
                            content=dict(s.content),
                            cause_source_key=s.cause_source_key,
                            cause_version=s.cause_version,
                        )
                        for s in segments
                    ],
                )
            )
    return tuple(result)


def teaching_impact_requests(
    changes: tuple[Mapping[str, Any], ...] | list[dict[str, Any]],
    snapshots: tuple[EducationFactSnapshot, ...],
) -> tuple[tuple[str, str, str], ...]:
    """Finite keys/filters; caller retains empty-result proofs and current school CAS."""
    requests: set[tuple[str, str, str]] = set()
    groups: set[Any] = set()
    offerings: set[Any] = set()
    classes: set[Any] = set()
    students: set[Any] = set()
    for change in changes:
        kind, key = change["fact_kind"], change["source_key"]
        if kind == "teaching_group":
            groups.add(key)
        if kind == "teaching_offering":
            offerings.add(key)
        if kind == "education_class":
            classes.add(key)
        for content in (
            _content(change),
            *(s["content"] for s in change.get("replacement_segments") or ()),
        ):
            if kind == "student_school_relationship" and content.get("enrollment_status") in {
                "transferred_out",
                "withdrawn",
                "graduated",
                "suspended",
            }:
                students.add(content.get("student_source_key"))
            if kind == "student_class_membership":
                classes.add(content.get("class_source_key"))
            groups.add(content.get("group_source_key"))
            offerings.add(content.get("offering_source_key"))
    primary_keys = {c["source_key"] for c in changes}
    for record in snapshots:
        for content in _contents(record):
            if record.fact_kind in {"offering_member_source", "teaching_assignment"}:
                offerings.add(content.get("offering_source_key"))
            if record.source_key in primary_keys:
                groups.add(content.get("group_source_key"))
                classes.add(content.get("class_source_key"))
    for key in groups - {None}:
        requests.add(("teaching_group", "source_key", key))
        requests.add(("offering_member_source", "group_source_key", key))
        requests.add(("teaching_group_member", "group_source_key", key))
    for key in offerings - {None}:
        requests.add(("teaching_offering", "source_key", key))
        requests.add(("offering_member_source", "offering_source_key", key))
        requests.add(("teaching_assignment", "offering_source_key", key))
    for key in classes - {None}:
        requests.add(("offering_member_source", "class_source_key", key))
    for key in students - {None}:
        requests.add(("teaching_group_member", "student_source_key", key))
    return tuple(sorted(requests))


def teaching_change_notices(
    changes: tuple[Mapping[str, Any], ...] | list[dict[str, Any]],
    snapshots: tuple[EducationFactSnapshot, ...],
) -> tuple[str, ...]:
    notices: list[str] = []
    by_key = {r.source_key: r for r in snapshots}
    for change in changes:
        content = _content(change)
        old = by_key.get(change["source_key"])
        if (
            change["fact_kind"] == "offering_member_source"
            and content.get("source_kind") == "independent_group"
            and (old is None or old.content.get("source_kind") != "independent_group")
        ):
            notices.append("本次起采用正式独立名单，今后须独立维护，不再随行政班名单自动同步。")
        if (
            change["fact_kind"] == "student_school_relationship"
            and content.get("enrollment_status") == "suspended"
        ):
            notices.append(
                "休学期间保留仍有效的教学关系，但不表示实际参加；已结束安排不会自动恢复。"
            )
        if change["fact_kind"] in {"teaching_group", "teaching_group_member"}:
            notices.append("共享教学群体的名单变化会影响全部引用开课；各开课引用已纳入本次核对。")
        if change["fact_kind"] == "student_class_membership":
            notices.append("行政班来源按正式归班时点变化；独立教学名单保持原有正式安排。")
    return tuple(dict.fromkeys(notices))


def plan_teaching_change(
    primary_changes: tuple[Mapping[str, Any], ...],
    snapshots: tuple[EducationFactSnapshot, ...],
    *,
    now: datetime,
    authorized_keys: frozenset[str],
    complete_snapshot: bool,
    max_changes: int = 300,
) -> EducationChangePlan:
    """Expand only required endings, preserving independent future/history slices."""
    if not primary_changes:
        return EducationChangePlan((), (PlanningIssue("empty_teaching_batch", None),), (), ())
    normalized = tuple(
        replace(
            r,
            segments=r.segments
            or (
                EducationTimelineSegment(
                    effective_at=r.effective_at,
                    valid_to=parse_education_time(r.content.get("valid_to")),
                    content={k: v for k, v in r.content.items() if k != "valid_to"},
                    provenance=r.provenance,
                ),
            ),
        )
        for r in snapshots
    )
    proposed = {str(c["source_key"]): dict(c) for c in primary_changes}
    # Existing interval planner owns cancellation provenance and never invents restoration.
    result = None
    for primary in primary_changes:
        ended: set[str] = (
            {str(primary["source_key"])} if primary["operation"] in {"end", "revoke"} else set()
        )
        students = {
            str(_content(c).get("student_source_key"))
            for c in (primary,)
            if c["fact_kind"] == "student_school_relationship"
            and _content(c).get("enrollment_status")
            in {"graduated", "transferred_out", "withdrawn"}
        }
        affected = set(ended)
        changed = True
        while changed:
            previous = set(affected)
            for record in normalized:
                if record.source_key in proposed and proposed[record.source_key][
                    "operation"
                ] not in {"end", "revoke"}:
                    continue  # Explicit rerouting or replacement is validated by the Writer.
                for content in _contents(record):
                    if record.fact_kind == "teaching_group_member" and (
                        content.get("group_source_key") in affected
                        or content.get("student_source_key") in students
                    ):
                        affected.add(record.source_key)
                    if record.fact_kind == "offering_member_source" and (
                        record.source_key in affected
                        or content.get("group_source_key") in affected
                        or content.get("class_source_key") in affected
                        or content.get("offering_source_key") in affected
                    ):
                        affected.add(record.source_key)
                        offering = str(content["offering_source_key"])
                        rerouted = any(
                            c["fact_kind"] == "offering_member_source"
                            and c["operation"] in {"upsert", "correct"}
                            and _content(c).get("offering_source_key") == offering
                            for c in primary_changes
                        )
                        if not rerouted:
                            affected.add(offering)
                    if (
                        record.fact_kind == "teaching_assignment"
                        and content.get("offering_source_key") in affected
                    ):
                        affected.add(record.source_key)
            changed = previous != affected
        cancel = primary["operation"] == "cancel"
        boundary = parse_education_time(primary["effective_at"])
        if boundary is None:
            raise ValueError("Explicit teaching timestamp required")
        command = EducationChangeCommand(
            purpose="cancel_future"
            if cancel
            else "historical_correction"
            if primary["operation"] == "correct"
            else "replace_assignment"
            if affected
            else "record",
            corrected_purpose="replace_assignment"
            if primary["operation"] == "correct" and affected
            else None,
            effective_at=now if cancel else boundary,
            cause_source_key=str(primary["source_key"]),
            cause_version=int(primary["expected_version"]) + (0 if cancel else 1),
            primary_changes=tuple(proposed.values()),
            replaced_source_keys=frozenset(affected),
            correction_reason=str(primary.get("correction_reason") or "学校正式教学安排调整"),
        )
        result = plan_education_change(
            command,
            normalized,
            authorized_keys=authorized_keys,
            complete_snapshot=complete_snapshot,
        )
        if result.issues:
            return result
        proposed = {str(c["source_key"]): c for c in result.changes}
        affected.clear()  # One causal decision expands each dependent only once.
    assert result is not None
    impact = set(result.impact_keys) | {
        r.source_key for r in normalized if r.fact_kind in TEACHING_KINDS
    }
    if len(proposed) > max_changes or len(impact) > max_changes:
        return EducationChangePlan(
            (),
            (PlanningIssue("teaching_capacity", None),),
            tuple(sorted(impact)),
            result.dependencies,
        )
    return replace(result, impact_keys=tuple(sorted(impact)))
