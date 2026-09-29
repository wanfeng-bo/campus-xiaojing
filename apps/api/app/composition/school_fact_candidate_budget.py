"""One approved Pro experiment on the existing cumulative model ledger; no DDL."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, time, timedelta, timezone
from decimal import ROUND_CEILING, Decimal
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from app.composition.deepseek_assistant_model import CandidateModelApproval, ModelCallUsage
from app.composition.school_fact_model_budget import ApprovedModelBudget, validate_model_usage
from app.platform.assistant.runtime import AssistantServiceError

EXPERIMENT_ID = "f2-pro-20260910"
_EXPERIMENT_CAP = 3_000_000
_SHANGHAI = timezone(timedelta(hours=8), "Asia/Shanghai")
_RATES = {"peak": ("0.30", "9", "27"), "offpeak": ("0.15", "4.5", "13.5")}


def _aware(value: object) -> datetime:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise RuntimeError("candidate billing requires an aware clock")
    return value.astimezone(UTC)


def _band(start: datetime, end: datetime) -> str:
    if end < start:
        raise RuntimeError("candidate billing clock moved backwards")
    start, end = start.astimezone(_SHANGHAI), end.astimezone(_SHANGHAI)
    day = start.date()
    # Intersect the entire observation window, including peak periods between offpeak endpoints.
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
        raise RuntimeError("invalid candidate token bound")
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
class _CandidateReservation:
    owner: SqliteCandidateModelCallPermit
    reference: str

    def mark_transmission_started(self) -> None:
        self.owner.mark_transmission_started(self.reference)

    def settle(self, usage: ModelCallUsage) -> None:
        self.owner.settle_reservation(self.reference, usage)

    def unknown(self) -> None:
        self.owner.mark_unknown(self.reference)


class SqliteCandidateModelCallPermit:
    """Peak reservations share the original 50 yuan and this fixed 3 yuan ceiling.

    Only existing metadata rows are inserted/updated. Historical rows and their policy
    fingerprint are never rewritten; unknown and reserved rows retain full occupancy.
    """

    def __init__(
        self,
        *,
        path: Path,
        ledger_policy: ApprovedModelBudget,
        candidate_approval: CandidateModelApproval,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if (
            candidate_approval.experiment_id != EXPERIMENT_ID
            or candidate_approval.model != "deepseek-v4-pro"
            or not candidate_approval.approval_reference
            or ledger_policy.cap_fen != 5000
        ):
            raise RuntimeError("unapproved candidate budget policy")
        _aware(candidate_approval.expires_at)
        self._candidate_approval = candidate_approval
        self._path, self._ledger_policy, self._clock = path, ledger_policy, clock
        self._policy: dict[str, Any] = {
            "experiment_id": candidate_approval.experiment_id,
            "approval_reference": candidate_approval.approval_reference,
            "model": candidate_approval.model,
            "expires_at": _aware(candidate_approval.expires_at).isoformat(),
            "ledger_fingerprint": ledger_policy.fingerprint,
            "cap_microyuan": _EXPERIMENT_CAP,
            "rates_cny_per_million": _RATES,
            "rate_order": ["cache_hit", "cache_miss", "output_including_reasoning"],
            "peak_window": "Asia/Shanghai Monday-Friday 09:00-12:00,14:00-18:00",
            "price_source": "https://api-docs.deepseek.com/zh-cn/quick_start/pricing/",
            "price_verified_on": "2026-09-10",
            "accounting": "usage estimate, not provider invoice; crossing bands use higher rate",
        }
        # Canonical JSON makes the in-memory tuples identical to persisted JSON arrays.
        self._policy = json.loads(_encoded(self._policy))
        with closing(self._connect()) as db:
            self._occupancy(db)

    @property
    def candidate_approval(self) -> CandidateModelApproval:
        return self._candidate_approval

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self._path.resolve().as_uri() + "?mode=rw", uri=True, timeout=5)
        db.execute("PRAGMA synchronous=FULL")
        return db

    def _active_time(self) -> datetime:
        now = _aware(self._clock())
        if now >= _aware(self.candidate_approval.expires_at):
            raise RuntimeError("candidate approval expired")
        return now

    def _occupancy(self, db: sqlite3.Connection, *, require_idle: bool = False) -> tuple[int, int]:
        fingerprint = db.execute("SELECT fingerprint FROM model_budget WHERE id=1").fetchone()
        if fingerprint is None or fingerprint[0] != self._ledger_policy.fingerprint:
            raise RuntimeError("original model ledger policy changed")
        total, experiment = 0, 0
        for state, charged, encoded in db.execute(
            "SELECT state, charged, usage_json FROM model_calls"
        ):
            if type(charged) is not int or charged < 0:
                raise RuntimeError("invalid model ledger occupancy")
            total += charged
            data = _decoded(encoded)
            if "candidate_budget" in data:
                meta: Any = data["candidate_budget"]
                if (
                    not isinstance(meta, dict)
                    or cast(dict[str, Any], meta).get("policy") != self._policy
                ):
                    raise RuntimeError("candidate policy drift; existing occupancy retained")
                if require_idle and state != "settled":
                    raise AssistantServiceError(
                        "prior candidate call remains in flight or unknown", reason="dependency"
                    )
                experiment += charged
        return total, experiment

    def _row(
        self, db: sqlite3.Connection, reference: str
    ) -> tuple[str, int, dict[str, Any], dict[str, Any]]:
        self._occupancy(db)
        row = db.execute(
            "SELECT state,reserved,usage_json FROM model_calls WHERE reference=?", (reference,)
        ).fetchone()
        if row is None:
            raise RuntimeError("candidate reservation unavailable")
        data = _decoded(row[2])
        meta = data.get("candidate_budget")
        if not isinstance(meta, dict) or cast(dict[str, Any], meta).get("policy") != self._policy:
            raise RuntimeError("reservation belongs to a different model policy")
        return row[0], row[1], data, cast(dict[str, Any], meta)

    def reserve(self, max_input_tokens: int, max_output_tokens: int) -> _CandidateReservation:
        reserved = _cost(0, max_input_tokens, max_output_tokens, "peak")
        if reserved <= 0:
            raise RuntimeError("candidate reservation must be positive")
        reference = uuid4().hex
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            now = self._active_time()
            total, experiment = self._occupancy(db, require_idle=True)
            if (
                total + reserved > self._ledger_policy.cap_fen * 10000
                or experiment + reserved > _EXPERIMENT_CAP
            ):
                raise AssistantServiceError("approved candidate budget exhausted", reason="budget")
            metadata = {
                "candidate_budget": {
                    "policy": self._policy,
                    "reserved_at": now.isoformat(),
                    "max_input_tokens": max_input_tokens,
                    "max_output_tokens": max_output_tokens,
                }
            }
            db.execute(
                "INSERT INTO model_calls VALUES (?, 'reserved', ?, ?, ?)",
                (reference, reserved, reserved, _encoded(metadata)),
            )
        return _CandidateReservation(self, reference)

    def mark_transmission_started(self, reference: str) -> None:
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            now = self._active_time()
            state, _, data, meta = self._row(db, reference)
            if state != "reserved" or "transmission_started_at" in meta:
                raise RuntimeError("candidate transmission already started or unavailable")
            if now < datetime.fromisoformat(meta["reserved_at"]):
                raise RuntimeError("candidate billing clock moved backwards")
            meta["transmission_started_at"] = now.isoformat()
            db.execute(
                "UPDATE model_calls SET usage_json=? WHERE reference=?", (_encoded(data), reference)
            )

    def settle_reservation(self, reference: str, usage: ModelCallUsage) -> None:
        validate_model_usage(usage)
        if usage.model != self.candidate_approval.model:
            raise RuntimeError("candidate response model differs from approval")
        measured = asdict(usage)
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            state, reserved, data, meta = self._row(db, reference)
            if state == "settled":
                if {
                    key: value for key, value in data.items() if key != "candidate_budget"
                } != measured:
                    raise RuntimeError("inconsistent candidate usage replay")
                return
            if (
                state not in {"reserved", "unknown"}
                or "transmission_started_at" not in meta
                or usage.prompt_tokens > meta["max_input_tokens"]
                or usage.completion_tokens > meta["max_output_tokens"]
            ):
                raise RuntimeError("candidate usage has no matching transmission or exceeds bound")
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
                raise RuntimeError("candidate cost exceeds reservation")
            meta.update(
                completed_at=completed.isoformat(),
                billing_band=band,
                cache_usage="reported"
                if usage.prompt_cache_hit_tokens is not None
                else "missing_assumed_miss",
            )
            encoded = _encoded({**measured, "candidate_budget": meta})
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
            total, experiment = self._occupancy(db)
        return {
            "global_occupied_microyuan": total,
            "experiment_occupied_microyuan": experiment,
            "global_cap_microyuan": self._ledger_policy.cap_fen * 10000,
            "experiment_cap_microyuan": _EXPERIMENT_CAP,
        }
