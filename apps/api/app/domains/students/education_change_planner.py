"""Pure, explicit education impact planning; never reads or writes a database.

Snapshots must cover the complete authorized impact domain. The caller binds their
school and fact version to the existing Writer preflight/confirmation transaction.
A plan is not authorization: that Writer must revalidate the same complete set.
"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

Purpose = Literal[
    "record",
    "transfer",
    "graduation",
    "close_class",
    "replace_assignment",
    "cancel_future",
    "historical_correction",
]


def _aware(value: datetime) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("aware effective time required")


def _time(value: object) -> datetime | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    _aware(parsed)  # type: ignore[arg-type]
    return parsed  # type: ignore[return-value]


@dataclass(frozen=True, kw_only=True)
class EndProvenance:
    cause_source_key: str
    cause_version: int
    ended_target_version: int
    prior_effective_at: datetime
    prior_content: Mapping[str, object]

    def __post_init__(self):
        _aware(self.prior_effective_at)
        if not self.cause_source_key or min(self.cause_version, self.ended_target_version) < 1:
            raise ValueError("complete causal ending evidence required")


@dataclass(frozen=True, kw_only=True)
class EducationTimelineSegment:
    effective_at: datetime
    valid_to: datetime | None
    content: Mapping[str, object]
    cause_source_key: str | None = None
    cause_version: int | None = None
    provenance: EndProvenance | None = None

    def __post_init__(self):
        _aware(self.effective_at)
        if self.valid_to is not None:
            _aware(self.valid_to)
            if self.valid_to <= self.effective_at:
                raise ValueError("nonpositive timeline segment")


@dataclass(frozen=True, kw_only=True)
class EducationFactSnapshot:
    fact_kind: str
    source_key: str
    version: int
    effective_at: datetime
    content: Mapping[str, object]
    provenance: EndProvenance | None = None
    cancelled: bool = False
    segments: tuple[EducationTimelineSegment, ...] = ()
    prior_segments: tuple[EducationTimelineSegment, ...] = ()
    latest_operation: str | None = None

    def __post_init__(self):
        _aware(self.effective_at)
        if self.version < 1 or not self.source_key:
            raise ValueError("versioned stable fact required")


@dataclass(frozen=True, kw_only=True)
class EducationChangeCommand:
    purpose: Purpose
    effective_at: datetime
    cause_source_key: str
    cause_version: int
    primary_changes: tuple[Mapping[str, object], ...]
    corrected_purpose: (
        Literal["transfer", "graduation", "close_class", "replace_assignment"] | None
    ) = None
    student_source_key: str | None = None
    class_source_key: str | None = None
    replaced_source_keys: frozenset[str] = frozenset()
    correction_reason: str | None = None
    origin: Mapping[str, object] = field(
        default_factory=lambda: {"sheet_code": "EDUCATION", "row_number": 1}
    )

    def __post_init__(self):
        _aware(self.effective_at)
        if self.purpose not in {
            "record",
            "transfer",
            "graduation",
            "close_class",
            "replace_assignment",
            "cancel_future",
            "historical_correction",
        }:
            raise ValueError("explicit education operation required")
        if not self.cause_source_key or self.cause_version < 1 or not self.primary_changes:
            raise ValueError("formal cause and explicit primary changes required")
        purpose = self.corrected_purpose or self.purpose
        if self.corrected_purpose is not None and self.purpose != "historical_correction":
            raise ValueError("correction operation is only valid for historical corrections")
        if purpose in {"transfer", "graduation"} and not self.student_source_key:
            raise ValueError("student required")
        if purpose in {"transfer", "close_class"} and not self.class_source_key:
            raise ValueError("class required")
        if purpose == "replace_assignment" and not self.replaced_source_keys:
            raise ValueError("explicit replaced assignments required")
        if (
            self.purpose in {"cancel_future", "historical_correction"}
            and not (self.correction_reason or "").strip()
        ):
            raise ValueError("correction reason required")


@dataclass(frozen=True)
class PlanningIssue:
    code: str
    source_key: str | None


@dataclass(frozen=True)
class EducationChangePlan:
    changes: tuple[dict[str, object], ...]
    issues: tuple[PlanningIssue, ...]
    impact_keys: tuple[str, ...]
    dependencies: tuple[tuple[str, int], ...]


_MEMBERS = {"student_class_membership"}
_ROLES = {"student_class_position"}
_CLASS_RELATIONS = (
    _MEMBERS
    | _ROLES
    | {"class_teacher_assignment", "class_location", "class_placement", "class_position_definition"}
)


def _affected(command: EducationChangeCommand, record: EducationFactSnapshot) -> bool:
    c = record.content
    purpose = command.corrected_purpose or command.purpose
    if purpose == "transfer":
        return (
            record.fact_kind in _MEMBERS | _ROLES
            and c.get("student_source_key") == command.student_source_key
            and c.get("class_source_key") == command.class_source_key
        )
    if purpose == "graduation":
        return (
            record.fact_kind in _MEMBERS | _ROLES
            and c.get("student_source_key") == command.student_source_key
        )
    if purpose == "close_class":
        return (
            record.fact_kind in _CLASS_RELATIONS
            and c.get("class_source_key") == command.class_source_key
        )
    return purpose == "replace_assignment" and record.source_key in command.replaced_source_keys


def plan_education_change(
    command: EducationChangeCommand,
    snapshots: tuple[EducationFactSnapshot, ...],
    *,
    authorized_keys: frozenset[str],
    complete_snapshot: bool,
) -> EducationChangePlan:
    """Return all issues and zero changes on any incomplete or conflicting impact.

    The output consists of canonical-v3 wire mappings. Strict canonical validation
    and whole-batch authorization/CAS are the existing Writer's responsibility.
    Never call this to expand a batch after its preview has been confirmed.
    """
    if any(snapshot.segments for snapshot in snapshots):
        return _plan_segmented(
            command, snapshots, authorized_keys=authorized_keys, complete_snapshot=complete_snapshot
        )
    issues: list[PlanningIssue] = []
    proposed: dict[str, dict[str, object]] = {}
    records: dict[str, EducationFactSnapshot] = {}
    impact: set[str] = set()

    def issue(code: str, key: str | None = None):
        value = PlanningIssue(code, key)
        if value not in issues:
            issues.append(value)

    def add(change: Mapping[str, object]):
        data = deepcopy(dict(change))
        key = str(data["source_key"])
        impact.add(key)
        if key in proposed and proposed[key] != data:
            issue("conflicting_changes", key)
        else:
            proposed[key] = data

    if not complete_snapshot:
        issue("incomplete_snapshot")
    for record in snapshots:
        if record.source_key in records:
            issue("ambiguous_snapshot", record.source_key)
        records[record.source_key] = record
    for change in command.primary_changes:
        data = dict(change)
        if data.get("operation") in {"correct", "cancel"} and not data.get("correction_reason"):
            data["correction_reason"] = command.correction_reason
        add(data)
    primary_cause = proposed.get(command.cause_source_key)
    if primary_cause is None or primary_cause.get("expected_version") != command.cause_version - (
        0 if command.purpose == "cancel_future" else 1
    ):
        issue("invalid_cause", command.cause_source_key)
    if (command.corrected_purpose or command.purpose) == "replace_assignment":
        for key in command.replaced_source_keys - records.keys():
            impact.add(key)
            issue("missing_dependency", key)
    if command.purpose == "cancel_future":
        cancellation = proposed.get(command.cause_source_key)
        if cancellation is None or cancellation.get("operation") != "cancel":
            issue("missing_cancellation", command.cause_source_key)
        cause = records.get(command.cause_source_key)
        if cause is None:
            issue("missing_dependency", command.cause_source_key)
        elif cause.cancelled or cause.effective_at <= command.effective_at:
            issue("not_future_arrangement", cause.source_key)
        elif cause.version != command.cause_version:
            issue("stale_cause", cause.source_key)
        for record in snapshots:
            if (
                cause is not None
                and cause.fact_kind == "student_class_membership"
                and record.fact_kind == "student_class_position"
                and not record.cancelled
                and record.effective_at >= cause.effective_at
                and record.content.get("student_source_key")
                == cause.content.get("student_source_key")
                and record.content.get("class_source_key") == cause.content.get("class_source_key")
            ):
                impact.add(record.source_key)
                disposition = proposed.get(record.source_key)
                if disposition is None or disposition.get("operation") not in {"cancel", "correct"}:
                    issue("dependent_arrangement", record.source_key)
            p = record.provenance
            if (
                p is None
                or p.cause_source_key != command.cause_source_key
                or p.cause_version != command.cause_version
            ):
                continue
            impact.add(record.source_key)
            if record.version != p.ended_target_version or record.cancelled:
                issue("independent_change", record.source_key)
                continue
            add(
                dict(
                    schema_version=3,
                    operation="correct",
                    fact_kind=record.fact_kind,
                    source_key=record.source_key,
                    expected_version=record.version,
                    effective_at=p.prior_effective_at,
                    content=deepcopy(dict(p.prior_content)),
                    origin=dict(command.origin),
                    cause_source_key=command.cause_source_key,
                    cause_version=command.cause_version,
                    correction_reason=command.correction_reason,
                )
            )
    else:
        for record in snapshots:
            if record.cancelled or not _affected(command, record):
                continue
            replacement = proposed.get(record.source_key)
            replacement_content = None if replacement is None else replacement.get("content")
            if (
                record.source_key == command.cause_source_key
                and (command.corrected_purpose or command.purpose) == "transfer"
                and isinstance(replacement_content, Mapping)
                and replacement_content.get("class_source_key") != command.class_source_key
                and replacement_content.get("student_source_key") == command.student_source_key
            ):
                # The corrected root keeps its history and now expresses the destination.
                # Its original-class dependent positions still need explicit endings.
                continue
            end = _time(record.content.get("valid_to"))
            if end is not None and end <= command.effective_at:
                continue
            impact.add(record.source_key)
            if record.effective_at >= command.effective_at:
                explicit = proposed.get(record.source_key)
                if explicit is None or explicit.get("operation") != "cancel":
                    issue("future_dependency", record.source_key)
                continue
            ending = dict(
                schema_version=3,
                operation="end",
                fact_kind=record.fact_kind,
                source_key=record.source_key,
                expected_version=record.version,
                effective_at=command.effective_at,
                content=None,
                origin=dict(command.origin),
                cause_source_key=command.cause_source_key,
                cause_version=command.cause_version,
            )
            explicit = proposed.get(record.source_key)
            if (
                explicit is not None
                and explicit.get("operation") == "correct"
                and isinstance(explicit.get("content"), Mapping)
            ):
                content = dict(explicit["content"])
                requested_end = _time(content.get("valid_to"))
                # A shorter independently supplied cutoff is also a distinct decision;
                # do not overwrite it while merging two effects on the same key.
                if requested_end is not None and requested_end != command.effective_at:
                    issue("conflicting_changes", record.source_key)
                else:
                    proposed[record.source_key] = {
                        **explicit,
                        "content": {**content, "valid_to": command.effective_at},
                        "cause_source_key": command.cause_source_key,
                        "cause_version": command.cause_version,
                    }
            elif (
                explicit is not None
                and explicit.get("operation") == "end"
                and explicit.get("effective_at") == command.effective_at
            ):
                proposed[record.source_key] = {
                    **explicit,
                    "cause_source_key": command.cause_source_key,
                    "cause_version": command.cause_version,
                }
            else:
                add(ending)
    for key, change in proposed.items():
        existing = records.get(key)
        if change.get("expected_version") != (existing.version if existing else 0):
            issue("stale_version", key)
        if change.get("operation") == "end" and existing is not None:
            ending_at = _time(change.get("effective_at"))
            if ending_at is None or ending_at <= existing.effective_at:
                issue("negative_tenure", key)
        content = change.get("content")
        if isinstance(content, Mapping):
            start = _time(change.get("effective_at"))
            end = _time(content.get("valid_to"))
            if start is None or (end is not None and end <= start):
                issue("negative_tenure", key)
    # Validate the final effective intervals, including records unchanged by this
    # plan. A cancellation may uncover a later independent arrangement.
    final = {
        r.source_key: (r.fact_kind, r.effective_at, dict(r.content))
        for r in snapshots
        if not r.cancelled
    }
    for key, change in proposed.items():
        operation = change.get("operation")
        if operation in {"cancel", "revoke"}:
            final.pop(key, None)
        elif operation == "end" and key in final:
            kind, start, content = final[key]
            final[key] = (kind, start, {**content, "valid_to": change["effective_at"]})
        elif isinstance(change.get("content"), Mapping):
            start = _time(change.get("effective_at"))
            if start is not None:
                final[key] = (str(change["fact_kind"]), start, dict(change["content"]))
    groups: dict[tuple[object, ...], list[tuple[str, datetime, datetime | None]]] = {}
    for key, (kind, start, content) in final.items():
        group = None
        if kind == "student_class_membership":
            group = (kind, content.get("student_source_key"))
        elif kind == "class_placement":
            group = (kind, content.get("class_source_key"))
        elif kind == "student_class_position":
            definition = final.get(str(content.get("position_source_key")))
            if (
                definition is not None
                and definition[0] == "class_position_definition"
                and definition[2].get("allow_multiple") is False
            ):
                group = (kind, content.get("class_source_key"), content.get("position_source_key"))
        if group is not None:
            groups.setdefault(group, []).append((key, start, _time(content.get("valid_to"))))
    for rows in groups.values():
        for index, (left, start, end) in enumerate(rows):
            for right, other_start, other_end in rows[index + 1 :]:
                if left not in proposed and right not in proposed:
                    continue
                if (end is None or other_start < end) and (other_end is None or start < other_end):
                    impact.update((left, right))
                    issue("overlapping_tenure", left)
                    issue("overlapping_tenure", right)
    for key in sorted(impact - authorized_keys):
        issue("missing_authority", key)
    dependencies = tuple(sorted((r.source_key, r.version) for r in snapshots))
    return EducationChangePlan(
        tuple(proposed[k] for k in sorted(proposed)) if not issues else (),
        tuple(issues),
        tuple(sorted(impact)),
        dependencies,
    )


def _plan_segmented(command, snapshots, *, authorized_keys, complete_snapshot):
    """Plan interval edits without erasing independent segments of the same root."""
    from dataclasses import replace

    from app.domains.students.education_versions import EducationSlice, revise_timeline

    issues: list[PlanningIssue] = []
    records = {r.source_key: r for r in snapshots}
    proposed = {str(c["source_key"]): deepcopy(dict(c)) for c in command.primary_changes}
    impact = set(proposed)

    def issue(code, key=None):
        value = PlanningIssue(code, key)
        if value not in issues:
            issues.append(value)

    if not complete_snapshot:
        issue("incomplete_snapshot")
    if len(records) != len(snapshots) or len(proposed) != len(command.primary_changes):
        issue("ambiguous_snapshot")
    original = {
        r.source_key: tuple(r.segments)
        or (
            EducationTimelineSegment(
                effective_at=r.effective_at,
                valid_to=_time(r.content.get("valid_to")),
                content={k: v for k, v in r.content.items() if k != "valid_to"},
                provenance=r.provenance,
            ),
        )
        for r in snapshots
        if not r.cancelled
    }
    timelines = dict(original)
    kinds = {r.source_key: r.fact_kind for r in snapshots}
    for key, change in proposed.items():
        expected = records[key].version if key in records else 0
        if change.get("expected_version") != expected:
            issue("stale_version", key)
        kinds[key] = str(change["fact_kind"])
        operation = change["operation"]
        if operation in {"correct", "cancel"} and not change.get("correction_reason"):
            change["correction_reason"] = command.correction_reason
        try:
            if operation == "cancel" and any(
                s.effective_at < _time(change["effective_at"]) for s in original.get(key, ())
            ):
                if (
                    records[key].latest_operation != "upsert"
                    or not records[key].prior_segments
                    or _time(change["effective_at"]) <= command.effective_at
                    or not any(
                        s.effective_at == _time(change["effective_at"]) for s in original[key]
                    )
                ):
                    raise ValueError("Exact prior future replacement proof required")
                timelines[key] = records[key].prior_segments
                continue
            if operation == "end" and any(
                s.effective_at >= _time(change["effective_at"]) for s in original.get(key, ())
            ):
                boundary = _time(change["effective_at"])
                active = [
                    s
                    for s in original[key]
                    if s.effective_at < boundary and (s.valid_to is None or boundary < s.valid_to)
                ]
                if len(active) != 1:
                    raise ValueError("Current interval must be identified for ending")
                timelines[key] = tuple(
                    replace(s, valid_to=boundary, provenance=None) if s is active[0] else s
                    for s in original[key]
                )
                change.update(
                    operation="correct",
                    content=None,
                    correction_reason=command.correction_reason
                    or "End the selected current interval",
                    replacement_segments=[
                        dict(
                            effective_at=s.effective_at,
                            valid_to=s.valid_to,
                            content=dict(s.content),
                            cause_source_key=s.cause_source_key,
                            cause_version=s.cause_version,
                        )
                        for s in timelines[key]
                    ],
                )
                continue
            if "replacement_segments" in change and change["replacement_segments"] is not None:
                timelines[key] = tuple(
                    EducationTimelineSegment(
                        effective_at=_time(s["effective_at"]),
                        valid_to=_time(s.get("valid_to")),
                        content=s["content"],
                        cause_source_key=s.get("cause_source_key"),
                        cause_version=s.get("cause_version"),
                    )
                    for s in change["replacement_segments"]
                )
            else:
                content = change.get("content")
                rows = revise_timeline(
                    tuple(
                        EducationSlice(s.effective_at, s.valid_to, s.content)
                        for s in original.get(key, ())
                    ),
                    operation=operation,
                    at=_time(change["effective_at"]),
                    until=None if content is None else _time(content.get("valid_to")),
                    payload=None
                    if content is None
                    else {k: v for k, v in content.items() if k != "valid_to"},
                    now=command.effective_at,
                )
                timelines[key] = tuple(
                    EducationTimelineSegment(
                        effective_at=s.start, valid_to=s.end, content=s.payload
                    )
                    for s in rows
                )
        except (ValueError, TypeError):
            issue("invalid_timeline", key)
    primary = proposed.get(command.cause_source_key)
    if primary is None or primary.get("expected_version") != command.cause_version - (
        0 if command.purpose == "cancel_future" else 1
    ):
        issue("invalid_cause", command.cause_source_key)
    changed: set[str] = set()
    purpose = command.corrected_purpose or command.purpose
    until = None
    if command.purpose == "historical_correction" and primary is not None:
        until = _time((primary.get("content") or {}).get("valid_to"))
    if command.purpose == "cancel_future":
        cause = records.get(command.cause_source_key)
        if cause is None or primary is None or primary.get("operation") != "cancel":
            issue("missing_cancellation", command.cause_source_key)
        elif (
            cause.version != command.cause_version
            or _time(primary["effective_at"]) <= command.effective_at
        ):
            issue("not_future_arrangement", cause.source_key)
        for key, segments in original.items():
            updated = list(segments)
            for index, segment in enumerate(segments):
                provenance = segment.provenance
                if (
                    provenance is None
                    or provenance.cause_source_key != command.cause_source_key
                    or provenance.cause_version != command.cause_version
                ):
                    continue
                impact.add(key)
                if records[key].version != provenance.ended_target_version:
                    issue("independent_change", key)
                    continue
                if key in proposed:
                    issue("conflicting_changes", key)
                    continue
                updated[index] = EducationTimelineSegment(
                    effective_at=provenance.prior_effective_at,
                    valid_to=_time(provenance.prior_content.get("valid_to")),
                    content={k: v for k, v in provenance.prior_content.items() if k != "valid_to"},
                    cause_source_key=command.cause_source_key,
                    cause_version=command.cause_version,
                )
                changed.add(key)
            if key in changed:
                timelines[key] = tuple(updated)
        # A future assignment depending on the cancelled membership needs its own disposition.
        if cause is not None and cause.fact_kind == "student_class_membership":
            selected = [
                s
                for s in original[cause.source_key]
                if s.effective_at == _time(primary["effective_at"])
            ]
            cancelled_content = selected[0].content if len(selected) == 1 else cause.content
            for key, segments in original.items():
                if kinds[key] != "student_class_position" or key in proposed:
                    continue
                if any(
                    s.effective_at >= _time(primary["effective_at"])
                    and s.content.get("student_source_key")
                    == cancelled_content.get("student_source_key")
                    and s.content.get("class_source_key")
                    == cancelled_content.get("class_source_key")
                    for s in segments
                ):
                    impact.add(key)
                    issue("dependent_arrangement", key)
    else:
        if purpose == "replace_assignment":
            for key in command.replaced_source_keys - records.keys():
                impact.add(key)
                issue("missing_dependency", key)
        for key, segments in original.items():
            record = records[key]
            updated = []
            for segment in segments:
                view = replace(
                    record,
                    effective_at=segment.effective_at,
                    content={**segment.content, "valid_to": segment.valid_to},
                    segments=(),
                )
                if not _affected(command, view) or (
                    segment.valid_to is not None and segment.valid_to <= command.effective_at
                ):
                    updated.append(segment)
                    continue
                if until is not None and segment.effective_at >= until:
                    updated.append(segment)
                    continue
                if key == command.cause_source_key and purpose == "transfer":
                    # Explicit replacement of this root is already in timelines.
                    updated.append(segment)
                    continue
                impact.add(key)
                if key in proposed:
                    # An explicit whole-root timeline is authoritative; postconditions below
                    # and the sole Writer verify all its retained relationships.
                    updated.append(segment)
                    continue
                if segment.effective_at >= command.effective_at:
                    # A separate future arrangement remains explicit and is never silently lost.
                    updated.append(segment)
                    if not any(
                        s.effective_at < command.effective_at
                        and (s.valid_to is None or command.effective_at < s.valid_to)
                        for s in segments
                    ):
                        issue("future_dependency", key)
                    continue
                changed.add(key)
                updated.append(
                    replace(
                        segment,
                        valid_to=command.effective_at,
                        cause_source_key=command.cause_source_key,
                        cause_version=command.cause_version,
                        provenance=None,
                    )
                )
                if until is not None and (segment.valid_to is None or until < segment.valid_to):
                    updated.append(replace(segment, effective_at=until, provenance=None))
            if key in changed:
                timelines[key] = tuple(updated)
    for key in changed:
        proposed[key] = dict(
            schema_version=3,
            operation="correct",
            fact_kind=kinds[key],
            source_key=key,
            expected_version=records[key].version,
            effective_at=command.effective_at,
            content=None,
            replacement_segments=[
                dict(
                    effective_at=s.effective_at,
                    valid_to=s.valid_to,
                    content=dict(s.content),
                    cause_source_key=s.cause_source_key,
                    cause_version=s.cause_version,
                )
                for s in timelines[key]
            ],
            correction_reason=command.correction_reason or "Formal linked timeline correction",
            cause_source_key=command.cause_source_key,
            cause_version=command.cause_version,
            origin=dict(command.origin),
        )
    groups: dict[tuple, list] = {}
    for key, segments in timelines.items():
        previous = None
        for segment in sorted(segments, key=lambda s: s.effective_at):
            if previous is not None and (
                previous.valid_to is None or segment.effective_at < previous.valid_to
            ):
                issue("overlapping_tenure", key)
            previous = segment
            if kinds[key] == "student_class_membership":
                groups.setdefault(
                    (kinds[key], segment.content.get("student_source_key")), []
                ).append((key, segment))
            elif kinds[key] == "class_placement":
                groups.setdefault((kinds[key], segment.content.get("class_source_key")), []).append(
                    (key, segment)
                )
    for rows in groups.values():
        for index, (key, left) in enumerate(rows):
            for other, right in rows[index + 1 :]:
                if key not in proposed and other not in proposed:
                    continue
                if (left.valid_to is None or right.effective_at < left.valid_to) and (
                    right.valid_to is None or left.effective_at < right.valid_to
                ):
                    issue("overlapping_tenure", key)
                    issue("overlapping_tenure", other)
    for key in impact - authorized_keys:
        issue("missing_authority", key)
    return EducationChangePlan(
        tuple(proposed[k] for k in sorted(proposed)) if not issues else (),
        tuple(issues),
        tuple(sorted(impact)),
        tuple(sorted((r.source_key, r.version) for r in snapshots)),
    )


def parse_education_time(value: object) -> datetime | None:
    """Shared strict timestamp normalization for finite maintenance adapters."""
    return _time(value)
