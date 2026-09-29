"""Recovery is a new explicit attempt, gated by full rollback and expiry evidence."""

import json
from datetime import timedelta
from hashlib import sha256

import pytest

from app.platform.school_facts.contracts import canonical_plan_sha256
from tests.governance.test_f3_formal_writer_adapter import NOW
from tests.governance.test_f4_runtime_dispatch import load
from tests.governance.test_f4_seed_publication import setup


def fixture(tmp_path):
    m = load("prepare_seed_recovery")
    _, driver, _ = setup(tmp_path)
    cs = driver.stages["education"].model_copy(update={"base_version": 23})
    digest = canonical_plan_sha256(cs)
    deadline = NOW + timedelta(days=7)
    journal = {
        **driver.header,
        "fixed_deadline": deadline.isoformat(),
        "batches": {
            "foundation": {"state": "succeeded", "receipt": {"target_version": 23}},
            "education": {
                "state": "unknown",
                "change_set": cs.model_dump(mode="json", exclude_unset=True),
                "plan_sha256": digest,
            },
        },
    }
    raw = json.dumps(journal).encode()
    batch = {
        "id": "original-batch",
        "confirmation_expires_at": (NOW - timedelta(seconds=1)).isoformat(),
        "status": "ready_for_confirmation",
        "confirmation_consumed": False,
        "has_result": False,
        "active_reference": False,
        "base_version": 23,
        "planned_units": len(cs.changes),
        "units": len(cs.changes),
        "pending_units": len(cs.changes),
        "succeeded_units": 0,
        "non_ready_units": 0,
    }
    observed = {
        "observed_at": NOW.isoformat(),
        "schema": "20260913_29",
        "school_version": 23,
        "governance_login": False,
        "batches": [batch],
        **{
            k: 0
            for k in (
                "governance_connections",
                "writer_active",
                "active_batches",
                "prepared_transactions",
                "lineage_count",
            )
        },
    }
    evidence = {
        "stage": "education",
        "plan_sha256": digest,
        "journal_sha256": sha256(raw).hexdigest(),
        "status": "unapplied_snapshot",
        "old_worker_absent": True,
        "observed": observed,
    }
    return m, dict(
        raw_journal=raw,
        evidence=evidence,
        raw_publication=driver.raw,
        now=NOW,
        timeline={
            "first_execution_t0": NOW.isoformat(),
            "fixed_deadline": deadline.isoformat(),
            "school_id": journal["school_id"],
        },
    )


def test_recovery_preserves_unknown_and_successful_predecessor(tmp_path):
    m, kw = fixture(tmp_path)
    original = kw["raw_journal"]
    new, manifest = m.recovery_documents(**kw)
    assert kw["raw_journal"] == original
    assert json.loads(original)["batches"]["education"]["state"] == "unknown"
    assert list(new["batches"]) == ["foundation"]
    assert new["batches"]["foundation"] == json.loads(original)["batches"]["foundation"]
    assert (
        manifest["base_version"] == 23 and manifest["plan_sha256"] == kw["evidence"]["plan_sha256"]
    )
    assert manifest["fixed_deadline"] == kw["timeline"]["fixed_deadline"]


@pytest.mark.parametrize(
    "change",
    [
        "unexpired",
        "consumed",
        "units",
        "lineage",
        "version",
        "active",
        "stale",
        "worker",
        "digest",
        "deadline",
    ],
)
def test_none_receipt_or_incomplete_snapshot_never_enables_reprepare(tmp_path, change):
    m, kw = fixture(tmp_path)
    ev = kw["evidence"]
    ob = ev["observed"]
    batch = ob["batches"][0]
    if change == "unexpired":
        batch["confirmation_expires_at"] = (NOW + timedelta(seconds=1)).isoformat()
    elif change == "consumed":
        batch["confirmation_consumed"] = True
    elif change == "units":
        batch["non_ready_units"] = 1
    elif change == "lineage":
        ob["lineage_count"] = 1
    elif change == "version":
        ob["school_version"] = 24
    elif change == "active":
        ob["writer_active"] = 1
    elif change == "stale":
        ob["observed_at"] = (NOW - timedelta(seconds=61)).isoformat()
    elif change == "worker":
        ev["old_worker_absent"] = False
    elif change == "digest":
        ev["plan_sha256"] = "0" * 64
    elif change == "deadline":
        kw["timeline"]["fixed_deadline"] = (NOW + timedelta(days=8)).isoformat()
    with pytest.raises(ValueError):
        m.recovery_documents(**kw)


def test_current_redis_absence_allows_reprepare_before_old_expiry(tmp_path):
    m, kw = fixture(tmp_path)
    kw["evidence"]["observed"]["batches"][0].update(
        old_session_absent=True, confirmation_expires_at=(NOW + timedelta(minutes=20)).isoformat()
    )
    new, manifest = m.recovery_documents(**kw)
    assert manifest["old_session_absent"] is True
    assert list(new["batches"]) == ["foundation"]
