"""Current Flash pricing on the original cumulative ledger; no schema changes."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, time, timedelta, timezone
from decimal import ROUND_CEILING, Decimal
from pathlib import Path
from typing import Any, Literal, cast
from uuid import uuid4

from app.composition.deepseek_assistant_model import CurrentModelApproval, ModelCallUsage
from app.composition.school_fact_model_budget import ApprovedModelBudget, validate_model_usage
from app.platform.assistant.runtime import AssistantServiceError

_SHANGHAI = timezone(timedelta(hours=8), "Asia/Shanghai")
_RATES = {"peak": ("0.04", "2", "8"), "offpeak": ("0.02", "1", "4")}


def _aware(value: object) -> datetime:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise RuntimeError("current model billing requires an aware clock")
    return value.astimezone(UTC)


def _band(start: datetime, end: datetime) -> str:
    if end < start:
        raise RuntimeError("current model billing clock moved backwards")
    start, end = start.astimezone(_SHANGHAI), end.astimezone(_SHANGHAI)
    day = start.date()
    while day <= end.date():
        if day.weekday() < 5:
            for opening, closing_hour in ((9, 12), (14, 18)):
                begin = datetime.combine(day, time(opening), _SHANGHAI)
                finish = datetime.combine(day, time(closing_hour), _SHANGHAI)
                if start < finish and end >= begin:
                    return "peak"
        day += timedelta(days=1)
    return "offpeak"


def _cost(hit: int, miss: int, outputs: int, band: str) -> int:
    if any(type(value) is not int or value < 0 for value in (hit, miss, outputs)):
        raise RuntimeError("invalid current model token bound")
    hit_rate, miss_rate, output_rate = map(Decimal, _RATES[band])
    return int(
        (hit * hit_rate + miss * miss_rate + outputs * output_rate).to_integral_value(
            rounding=ROUND_CEILING
        )
    )


def _encoded(value: dict[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _decoded(encoded: str | None) -> dict[str, Any]:
    value: object = json.loads(encoded) if encoded is not None else {}
    if not isinstance(value, dict):
        raise RuntimeError("invalid model ledger metadata")
    return cast(dict[str, Any], value)


@dataclass(frozen=True, slots=True)
class ApprovedModelStageBudget:
    """An explicit additional ceiling, recorded on new rows of the same ledger."""

    stage_id: str
    approval_reference: str
    cap_microyuan: int
    previous_cap_microyuan: int | None = None
    previous_approval_reference: str | None = None

    def __post_init__(self) -> None:
        if (
            type(self.stage_id) is not str
            or not 1 <= len(self.stage_id) <= 100
            or not all(c.isascii() and (c.isalnum() or c in "-_") for c in self.stage_id)
            or type(self.approval_reference) is not str
            or not 1 <= len(self.approval_reference.strip()) <= 200
            or type(self.cap_microyuan) is not int
            or not 1 <= self.cap_microyuan <= 50_000_000
        ):
            raise ValueError("explicit bounded stage budget required")
        if self.previous_cap_microyuan is not None or self.previous_approval_reference is not None:
            if (
                type(self.previous_cap_microyuan) is not int
                or not 1 <= self.previous_cap_microyuan < self.cap_microyuan
                or type(self.previous_approval_reference) is not str
                or not 1 <= len(self.previous_approval_reference.strip()) <= 200
                or self.previous_approval_reference == self.approval_reference
            ):
                raise ValueError("explicit bounded previous stage approval required")


@dataclass(frozen=True, slots=True)
class _CurrentReservation:
    owner: SqliteCurrentModelCallPermit
    reference: str

    def mark_transmission_started(self) -> None:
        self.owner.mark_transmission_started(self.reference)

    def settle(self, usage: ModelCallUsage) -> None:
        self.owner.settle_reservation(self.reference, usage)

    def unknown(self) -> None:
        self.owner.mark_unknown(self.reference)


class SqliteCurrentModelCallPermit:
    """Preserve all historical occupancy while pricing newly approved Flash calls.

    This is independent of the Pro experiment: its policy and rows are untouched.
    Unknown and in-flight calls retain their full peak reservation across restarts.
    """

    def __init__(
        self,
        *,
        path: Path,
        ledger_policy: ApprovedModelBudget,
        current_approval: CurrentModelApproval,
        stage_budget: ApprovedModelStageBudget | None = None,
        ledger_purpose: Literal["school-facts-live"] | None = None,
        budget_window_start: datetime | None = None,
        budget_window_end: datetime | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if (
            type(current_approval) is not CurrentModelApproval
            or current_approval.model != "deepseek-flash"
            or not current_approval.approval_reference
            or (
                ledger_purpose is None
                and ledger_policy.cap_fen != 5000
                or ledger_purpose == "school-facts-live"
                and (
                    ledger_policy.cap_fen != 10000
                    or stage_budget is not None
                    or budget_window_start is None
                    or budget_window_end is None
                )
                or ledger_purpose not in {None, "school-facts-live"}
                or ledger_purpose is None
                and (budget_window_start is not None or budget_window_end is not None)
            )
        ):
            raise RuntimeError("unapproved current model budget policy")
        window_policy: dict[str, str] = {}
        if ledger_purpose == "school-facts-live":
            start = _aware(budget_window_start)
            end = _aware(budget_window_end)
            if end != start + timedelta(days=30):
                raise RuntimeError("invalid formal model budget window")
            window_policy = {
                "budget_window_start": start.isoformat(),
                "budget_window_end": end.isoformat(),
            }
        if stage_budget is not None and type(stage_budget) is not ApprovedModelStageBudget:
            raise RuntimeError("invalid stage budget")
        self._stage_budget = stage_budget
        self._stage_policy = (
            {key: value for key, value in asdict(stage_budget).items() if value is not None}
            if stage_budget is not None
            else None
        )
        self._previous_stage_policy = (
            {
                "stage_id": stage_budget.stage_id,
                "approval_reference": stage_budget.previous_approval_reference,
                "cap_microyuan": stage_budget.previous_cap_microyuan,
            }
            if stage_budget is not None and stage_budget.previous_cap_microyuan is not None
            else None
        )
        self._current_approval = current_approval
        self._path, self._ledger_policy, self._clock = path, ledger_policy, clock
        self._policy: dict[str, Any] = json.loads(
            _encoded(
                {
                    "approval_reference": current_approval.approval_reference,
                    "model": current_approval.model,
                    "ledger_fingerprint": ledger_policy.fingerprint,
                    "rates_cny_per_million": _RATES,
                    "rate_order": ["cache_hit", "cache_miss", "output_including_reasoning"],
                    "peak_window": "Asia/Shanghai Monday-Friday 09:00-12:00,14:00-18:00",
                    "price_source": "https://api-docs.deepseek.com/zh-cn/quick_start/pricing/",
                    "price_verified_on": current_approval.price_verified_on,
                    "accounting": (
                        "usage estimate, not provider invoice; crossing bands use higher rate"
                    ),
                    **({"ledger_purpose": ledger_purpose} if ledger_purpose is not None else {}),
                    **window_policy,
                }
            )
        )
        with closing(self._connect()) as db:
            self._occupancy(db)

    @property
    def current_approval(self) -> CurrentModelApproval:
        return self._current_approval

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self._path.resolve().as_uri() + "?mode=rw", uri=True, timeout=5)
        db.execute("PRAGMA synchronous=FULL")
        return db

    def _occupancy(self, db: sqlite3.Connection) -> tuple[int, int, int]:
        fingerprint = db.execute("SELECT fingerprint FROM model_budget WHERE id=1").fetchone()
        if fingerprint is None or fingerprint[0] != self._ledger_policy.fingerprint:
            raise RuntimeError("original model ledger policy changed")
        total, current, stage = 0, 0, 0
        for charged, encoded in db.execute("SELECT charged, usage_json FROM model_calls"):
            if type(charged) is not int or charged < 0:
                raise RuntimeError("invalid model ledger occupancy")
            total += charged
            data = _decoded(encoded)
            if "current_model_budget" in data:
                meta: Any = data["current_model_budget"]
                if (
                    not isinstance(meta, dict)
                    or cast(dict[str, Any], meta).get("policy") != self._policy
                ):
                    raise RuntimeError("current model policy drift; existing occupancy retained")
                current += charged
                stage_meta = meta.get("stage")
                if stage_meta is not None and not isinstance(stage_meta, dict):
                    raise RuntimeError("invalid stage ledger metadata")
                if (
                    self._stage_policy is not None
                    and stage_meta is not None
                    and stage_meta.get("stage_id") == self._stage_policy["stage_id"]
                ):
                    if not self._matches_stage(stage_meta):
                        raise RuntimeError("stage policy drift; existing occupancy retained")
                    stage += charged
        return total, current, stage

    def _matches_stage(self, metadata: object) -> bool:
        # Only the exact explicitly named historical policy is compatible. New
        # reservations always record the current approval, including its history.
        return metadata == self._stage_policy or (
            self._previous_stage_policy is not None and metadata == self._previous_stage_policy
        )

    def _row(
        self, db: sqlite3.Connection, reference: str
    ) -> tuple[str, int, dict[str, Any], dict[str, Any]]:
        self._occupancy(db)
        row = db.execute(
            "SELECT state,reserved,usage_json FROM model_calls WHERE reference=?", (reference,)
        ).fetchone()
        if row is None:
            raise RuntimeError("current model reservation unavailable")
        data = _decoded(row[2])
        meta = data.get("current_model_budget")
        if (
            not isinstance(meta, dict)
            or cast(dict[str, Any], meta).get("policy") != self._policy
            or "candidate_budget" in data
            or not self._matches_stage(cast(dict[str, Any], meta).get("stage"))
        ):
            raise RuntimeError("reservation belongs to a different model policy")
        return row[0], row[1], data, cast(dict[str, Any], meta)

    def reserve(self, max_input_tokens: int, max_output_tokens: int) -> _CurrentReservation:
        reserved = _cost(0, max_input_tokens, max_output_tokens, "peak")
        if reserved <= 0:
            raise RuntimeError("current model reservation must be positive")
        reference = uuid4().hex
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            now = _aware(self._clock())
            total, _, stage = self._occupancy(db)
            if total + reserved > self._ledger_policy.cap_fen * 10000:
                raise AssistantServiceError("approved model budget exhausted", reason="budget")
            if (
                self._stage_budget is not None
                and stage + reserved > self._stage_budget.cap_microyuan
            ):
                raise AssistantServiceError("approved stage budget exhausted", reason="budget")
            metadata = {
                "current_model_budget": {
                    "policy": self._policy,
                    "reserved_at": now.isoformat(),
                    "max_input_tokens": max_input_tokens,
                    "max_output_tokens": max_output_tokens,
                }
            }
            if self._stage_policy is not None:
                metadata["current_model_budget"]["stage"] = self._stage_policy
            db.execute(
                "INSERT INTO model_calls VALUES (?, 'reserved', ?, ?, ?)",
                (reference, reserved, reserved, _encoded(metadata)),
            )
        return _CurrentReservation(self, reference)

    def mark_transmission_started(self, reference: str) -> None:
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            now = _aware(self._clock())
            state, _, data, meta = self._row(db, reference)
            if state != "reserved" or "transmission_started_at" in meta:
                raise RuntimeError("current model transmission already started or unavailable")
            if now < datetime.fromisoformat(meta["reserved_at"]):
                raise RuntimeError("current model billing clock moved backwards")
            meta["transmission_started_at"] = now.isoformat()
            db.execute(
                "UPDATE model_calls SET usage_json=? WHERE reference=?", (_encoded(data), reference)
            )

    def settle_reservation(self, reference: str, usage: ModelCallUsage) -> None:
        validate_model_usage(usage)
        if usage.model != self.current_approval.model:
            raise RuntimeError("current model response model differs from approval")
        measured = asdict(usage)
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            state, reserved, data, meta = self._row(db, reference)
            if state == "settled":
                if {
                    key: value for key, value in data.items() if key != "current_model_budget"
                } != measured:
                    raise RuntimeError("inconsistent current model usage replay")
                return
            if (
                state not in {"reserved", "unknown"}
                or "transmission_started_at" not in meta
                or usage.prompt_tokens > meta["max_input_tokens"]
                or usage.completion_tokens > meta["max_output_tokens"]
            ):
                raise RuntimeError(
                    "current model usage has no matching transmission or exceeds bound"
                )
            completed = _aware(self._clock())
            band = _band(datetime.fromisoformat(meta["transmission_started_at"]), completed)
            hit = usage.prompt_cache_hit_tokens or 0
            miss = (
                usage.prompt_tokens
                if usage.prompt_cache_miss_tokens is None
                else usage.prompt_cache_miss_tokens
            )
            charged = _cost(hit, miss, usage.completion_tokens, band)
            if charged > reserved:
                raise RuntimeError("current model cost exceeds reservation")
            meta.update(
                completed_at=completed.isoformat(),
                billing_band=band,
                cache_usage="reported"
                if usage.prompt_cache_hit_tokens is not None
                else "missing_assumed_miss",
            )
            encoded = _encoded({**measured, "current_model_budget": meta})
            db.execute(
                "UPDATE model_calls SET state='settled',charged=?,usage_json=? WHERE reference=?",
                (charged, encoded, reference),
            )

    def mark_unknown(self, reference: str) -> None:
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            self._row(db, reference)
            db.execute(
                "UPDATE model_calls SET state='unknown' WHERE reference=? AND state='reserved'",
                (reference,),
            )

    def summary(self) -> dict[str, int]:
        with closing(self._connect()) as db:
            total, current, stage = self._occupancy(db)
        result = {
            "global_occupied_microyuan": total,
            "current_occupied_microyuan": current,
            "global_cap_microyuan": self._ledger_policy.cap_fen * 10000,
        }
        if self._stage_budget is not None:
            result.update(
                stage_occupied_microyuan=stage,
                stage_cap_microyuan=self._stage_budget.cap_microyuan,
            )
        return result
