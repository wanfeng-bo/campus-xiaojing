from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import asdict, dataclass
from decimal import ROUND_CEILING, Decimal
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

from pydantic import Field

from app.composition.deepseek_assistant_model import ModelCallUsage
from app.platform.assistant.runtime import AssistantServiceError
from app.platform.contracts import FrozenContractModel


class ApprovedModelBudget(FrozenContractModel):
    """Explicit reviewed policy; no candidate price, budget or approval default."""

    approval_reference: str = Field(min_length=1, max_length=200)
    cap_fen: int = Field(strict=True, gt=0)
    input_cny_per_million: Decimal = Field(gt=0)
    output_cny_per_million: Decimal = Field(gt=0)

    @property
    def fingerprint(self) -> str:
        return sha256(self.model_dump_json().encode()).hexdigest()


def validate_model_usage(usage: ModelCallUsage) -> None:
    if type(usage) is not ModelCallUsage:
        raise RuntimeError("invalid model usage metadata")
    counts = (usage.prompt_tokens, usage.completion_tokens, usage.total_tokens)
    if any(type(value) is not int or value < 0 for value in counts):
        raise RuntimeError("invalid model usage metadata")
    if usage.prompt_tokens + usage.completion_tokens != usage.total_tokens:
        raise RuntimeError("inconsistent model usage metadata")
    reasoning = usage.reasoning_tokens
    if reasoning is not None and (
        type(reasoning) is not int or reasoning < 0 or reasoning > usage.completion_tokens
    ):
        raise RuntimeError("inconsistent reasoning usage metadata")
    hit, miss = usage.prompt_cache_hit_tokens, usage.prompt_cache_miss_tokens
    if (hit is None) != (miss is None) or any(
        value is not None and (type(value) is not int or value < 0) for value in (hit, miss)
    ):
        raise RuntimeError("invalid cache usage metadata")
    if hit is not None and miss is not None and hit + miss != usage.prompt_tokens:
        raise RuntimeError("inconsistent cache usage metadata")
    for value in (usage.model, usage.request_id, usage.completion_id):
        if type(value) is not str or not 1 <= len(value) <= 200:
            raise RuntimeError("invalid model usage reference")


@dataclass(frozen=True, slots=True)
class _Reservation:
    owner: SqliteModelCallPermit
    reference: str

    def settle(self, usage: ModelCallUsage) -> None:
        self.owner.settle_reservation(self.reference, usage)

    def unknown(self) -> None:
        self.owner.mark_unknown(self.reference)


class SqliteModelCallPermit:
    """Local project-only spend ceiling ledger, not a payment or recharge service.

    It stores usage metadata only. Cache/time discounts are deliberately excluded from the
    conservative estimate. Unknown calls retain their whole reservation across process restarts.
    """

    def __init__(self, *, path: Path, policy: ApprovedModelBudget) -> None:
        self._path = path
        self._policy = policy
        if not path.parent.is_dir():
            raise RuntimeError("budget directory must be explicitly provisioned")
        with closing(self._connect()) as db, db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS model_budget "
                "(id INTEGER PRIMARY KEY CHECK(id=1), fingerprint TEXT NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS model_calls (reference TEXT PRIMARY KEY, "
                "state TEXT NOT NULL, reserved INTEGER NOT NULL, "
                "charged INTEGER NOT NULL, usage_json TEXT)"
            )
            db.execute("INSERT OR IGNORE INTO model_budget VALUES (1, ?)", (policy.fingerprint,))
            row = db.execute("SELECT fingerprint FROM model_budget WHERE id=1").fetchone()
            if row is None or row[0] != policy.fingerprint:
                raise RuntimeError("existing budget policy differs; review without resetting spend")

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self._path, timeout=5)
        db.execute("PRAGMA synchronous=FULL")
        return db

    def _cost(self, inputs: int, outputs: int) -> int:
        if type(inputs) is not int or type(outputs) is not int or inputs < 0 or outputs < 0:
            raise RuntimeError("invalid usage bound")
        return int(
            (
                inputs * self._policy.input_cny_per_million
                + outputs * self._policy.output_cny_per_million
            ).to_integral_value(rounding=ROUND_CEILING)
        )

    def reserve(self, max_input_tokens: int, max_output_tokens: int) -> _Reservation:
        reserved = self._cost(max_input_tokens, max_output_tokens)
        if reserved <= 0:
            raise RuntimeError("a model call requires a positive reservation")
        reference = uuid4().hex
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            fingerprint = db.execute("SELECT fingerprint FROM model_budget WHERE id=1").fetchone()
            if fingerprint is None or fingerprint[0] != self._policy.fingerprint:
                raise RuntimeError("budget policy unavailable")
            spent = int(
                db.execute("SELECT COALESCE(SUM(charged), 0) FROM model_calls").fetchone()[0]
            )
            if spent + reserved > self._policy.cap_fen * 10000:
                raise AssistantServiceError("approved model budget exhausted", reason="budget")
            db.execute(
                "INSERT INTO model_calls VALUES (?, 'reserved', ?, ?, NULL)",
                (reference, reserved, reserved),
            )
        return _Reservation(self, reference)

    def settle_reservation(self, reference: str, usage: ModelCallUsage) -> None:
        validate_model_usage(usage)
        # completion_tokens already includes reasoning; never add reasoning_tokens again.
        # This is a peak cache-miss estimate, not an attributed provider invoice.
        charged = self._cost(usage.prompt_tokens, usage.completion_tokens)
        encoded = json.dumps(asdict(usage), sort_keys=True, separators=(",", ":"))
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT state, reserved, usage_json FROM model_calls WHERE reference=?",
                (reference,),
            ).fetchone()
            if row is None:
                raise RuntimeError("model reservation unavailable")
            if row[2] is not None and {"candidate_budget", "current_model_budget"}.intersection(
                json.loads(row[2])
            ):
                raise RuntimeError("reservation requires its approved model price policy")
            if charged > row[1]:
                # A broken usage/bound contract cannot release any conservative reservation.
                raise RuntimeError("model usage exceeds its approved reservation")
            if row[0] == "settled":
                if row[2] != encoded:
                    raise RuntimeError("inconsistent model usage replay")
                return
            db.execute(
                "UPDATE model_calls SET state='settled', charged=?, usage_json=? WHERE reference=?",
                (charged, encoded, reference),
            )

    def mark_unknown(self, reference: str) -> None:
        with closing(self._connect()) as db, db:
            db.execute(
                "UPDATE model_calls SET state='unknown' WHERE reference=? AND state='reserved'",
                (reference,),
            )

    def summary(self) -> dict[str, int]:
        with closing(self._connect()) as db:
            rows = db.execute(
                "SELECT state, COUNT(*), SUM(charged) FROM model_calls GROUP BY state"
            ).fetchall()
        result = {
            "call_count": 0,
            "settled_estimated_microyuan": 0,
            "reserved_or_unknown_microyuan": 0,
            "approved_cap_fen": self._policy.cap_fen,
        }
        for state, count, charged in rows:
            result["call_count"] += count
            key = (
                "settled_estimated_microyuan"
                if state == "settled"
                else "reserved_or_unknown_microyuan"
            )
            result[key] += charged
        return result
