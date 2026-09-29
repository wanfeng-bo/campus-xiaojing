"""Business interval surgery. Writer retains the superseded system-time revision."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class EducationSlice:
    start: datetime
    end: datetime | None
    payload: Mapping[str, object]


def revise_timeline(
    rows: tuple[EducationSlice, ...],
    *,
    operation: str,
    at: datetime,
    until: datetime | None,
    payload: Mapping[str, object] | None,
    now: datetime,
) -> tuple[EducationSlice, ...]:
    for value in (at, until, now):
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("Aware education timestamps required.")
    if until is not None and until <= at:
        raise ValueError("Nonpositive education interval.")
    ordered = tuple(sorted(rows, key=lambda row: row.start))
    if operation == "cancel":
        if at <= now or not ordered or any(row.start < at for row in ordered):
            raise ValueError("Only a wholly future arrangement can be cancelled.")
        return ()
    if operation in {"end", "revoke"}:
        if not ordered or any(row.start >= at for row in ordered):
            raise ValueError(
                "Termination touches a later arrangement; explicit correction required."
            )
        if all(row.end is not None and row.end <= at for row in ordered):
            raise ValueError("Termination cannot extend or repeat a prior endpoint.")
        return tuple(
            EducationSlice(row.start, min(row.end, at) if row.end else at, row.payload)
            for row in ordered
        )
    if operation not in {"upsert", "correct"} or payload is None:
        raise ValueError("Unsupported education revision.")
    if (
        operation == "upsert"
        and ordered
        and (at <= ordered[-1].start or (ordered[-1].end is not None and at >= ordered[-1].end))
    ):
        raise ValueError("Ordinary replacement cannot rewrite history or revive an ended fact.")
    result: list[EducationSlice] = []
    for row in ordered:
        if (row.end is not None and row.end <= at) or (until is not None and row.start >= until):
            result.append(row)
            continue
        if row.start < at:
            result.append(EducationSlice(row.start, at, row.payload))
        if until is not None and (row.end is None or row.end > until):
            result.append(EducationSlice(until, row.end, row.payload))
    result.append(EducationSlice(at, until, dict(payload)))
    return tuple(sorted(result, key=lambda row: row.start))
