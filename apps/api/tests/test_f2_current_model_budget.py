import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.composition.deepseek_assistant_model import ModelCallUsage
from app.composition.school_fact_model_budget import ApprovedModelBudget, SqliteModelCallPermit


def ledger(tmp_path):
    policy = ApprovedModelBudget(
        approval_reference="original",
        cap_fen=5000,
        input_cny_per_million=Decimal("3"),
        output_cny_per_million=Decimal("9"),
    )
    path = tmp_path / "budget.sqlite3"
    old = SqliteModelCallPermit(path=path, policy=policy)
    return path, policy, old


def current(path, policy, clock, approval=None):
    from app.composition.deepseek_assistant_model import CurrentModelApproval
    from app.composition.school_fact_current_model_budget import SqliteCurrentModelCallPermit

    approval = approval or CurrentModelApproval(
        model="deepseek-flash",
        approval_reference="approved-current",
        price_verified_on="2026-09-12",
    )
    permit = SqliteCurrentModelCallPermit(
        path=path, ledger_policy=policy, current_approval=approval, clock=lambda: clock[0]
    )
    assert permit.current_approval is approval
    return permit


def usage(**changes):
    fields = dict(
        model="deepseek-flash",
        request_id="r",
        completion_id="c",
        prompt_tokens=100,
        completion_tokens=100,
        total_tokens=200,
        prompt_cache_hit_tokens=80,
        prompt_cache_miss_tokens=20,
        reasoning_tokens=90,
    )
    fields.update(changes)
    return ModelCallUsage(**fields)


def row(path, reference):
    with sqlite3.connect(path) as db:
        return db.execute(
            "SELECT state,reserved,charged,usage_json FROM model_calls WHERE reference=?",
            (reference,),
        ).fetchone()


@pytest.mark.parametrize(
    "start,end,band,cost",
    [
        ("2026-09-10T00:00:00+00:00", "2026-09-10T00:01:00+00:00", "offpeak", 422),
        ("2026-09-10T01:00:00+00:00", "2026-09-10T01:01:00+00:00", "peak", 844),
        ("2026-09-10T00:59:59+00:00", "2026-09-10T01:00:00+00:00", "peak", 844),
        ("2026-09-10T00:00:00+00:00", "2026-09-10T11:00:00+00:00", "peak", 844),
        ("2026-09-10T04:00:00+00:00", "2026-09-10T05:59:59+00:00", "offpeak", 422),
        ("2026-09-10T05:59:59+00:00", "2026-09-10T06:00:00+00:00", "peak", 844),
        ("2026-09-10T09:59:59+00:00", "2026-09-10T10:00:00+00:00", "peak", 844),
        ("2026-09-10T10:00:00+00:00", "2026-09-10T10:01:00+00:00", "offpeak", 422),
        ("2026-09-12T01:00:00+00:00", "2026-09-13T10:00:00+00:00", "offpeak", 422),
        ("2026-09-11T11:00:00+00:00", "2026-09-14T11:00:00+00:00", "peak", 844),
    ],
)
def test_entire_actual_transmission_window(tmp_path, start, end, band, cost):
    path, policy, _ = ledger(tmp_path)
    clock = [datetime.fromisoformat(start)]
    permit = current(path, policy, clock)
    r = permit.reserve(1000, 1000)
    r.mark_transmission_started()
    clock[0] = datetime.fromisoformat(end)
    r.settle(usage())
    state, reserved, charged, encoded = row(path, r.reference)
    assert (state, reserved, charged) == ("settled", 10000, cost)
    meta = json.loads(encoded)["current_model_budget"]
    assert meta["billing_band"] == band
    assert meta["transmission_started_at"] == start
    assert meta["completed_at"] == end


def test_missing_cache_and_idempotency_unknown(tmp_path):
    path, policy, _ = ledger(tmp_path)
    clock = [datetime(2026, 9, 10, tzinfo=UTC)]
    permit = current(path, policy, clock)
    r = permit.reserve(1000, 1000)
    r.mark_transmission_started()
    with pytest.raises(RuntimeError):
        r.mark_transmission_started()
    r.unknown()
    measured = usage(prompt_cache_hit_tokens=None, prompt_cache_miss_tokens=None)
    r.settle(measured)
    before = row(path, r.reference)
    assert before[2] == 500
    clock[0] = datetime(2026, 9, 10, 2, tzinfo=UTC)
    r.settle(measured)
    r.unknown()
    assert row(path, r.reference) == before
    with pytest.raises(RuntimeError):
        r.settle(usage())


@pytest.mark.parametrize(
    "changes",
    [
        {"model": "deepseek-v4-flash"},
        {"model": "deepseek-v4-pro"},
        {"prompt_cache_miss_tokens": 19},
        {"reasoning_tokens": 101},
        {
            "prompt_tokens": 1001,
            "total_tokens": 1101,
            "prompt_cache_hit_tokens": None,
            "prompt_cache_miss_tokens": None,
        },
        {"completion_tokens": 1001, "total_tokens": 1101},
        {"total_tokens": 1},
        {"prompt_cache_hit_tokens": None},
    ],
)
def test_invalid_usage_keeps_full_reservation(tmp_path, changes):
    path, policy, _ = ledger(tmp_path)
    r = current(path, policy, [datetime(2026, 9, 10, tzinfo=UTC)]).reserve(1000, 1000)
    r.mark_transmission_started()
    before = row(path, r.reference)
    with pytest.raises(RuntimeError):
        r.settle(usage(**changes))
    assert row(path, r.reference) == before


def test_unstarted_and_bad_clock_fail_closed(tmp_path):
    path, policy, _ = ledger(tmp_path)
    clock = [datetime(2026, 9, 10, tzinfo=UTC)]
    permit = current(path, policy, clock)
    r = permit.reserve(1000, 1000)
    with pytest.raises(RuntimeError):
        r.settle(usage())
    clock[0] = datetime(2026, 9, 9, tzinfo=UTC)
    with pytest.raises(RuntimeError):
        r.mark_transmission_started()
    clock[0] = datetime(2026, 9, 10, tzinfo=UTC)
    r.mark_transmission_started()
    clock[0] = datetime(2026, 9, 9, tzinfo=UTC)
    with pytest.raises(RuntimeError):
        r.settle(usage())
    clock[0] = datetime(2026, 9, 10)
    with pytest.raises(RuntimeError):
        permit.reserve(1, 1)
    with pytest.raises(RuntimeError):
        r.settle(usage())
    assert row(path, r.reference)[2] == 10000


def test_history_unknown_pro_and_schema_unchanged_with_no_ddl(tmp_path, monkeypatch):
    path, policy, old = ledger(tmp_path)
    original = old.reserve(0, 360448)
    original.unknown()  # exact old 3.244032 yuan remains occupied
    with sqlite3.connect(path) as db:
        for index in range(14):
            db.execute(
                "INSERT INTO model_calls VALUES (?, 'settled', 100, 90, ?)",
                (f"pro-{index}", '{"candidate_budget":{"policy":{"historical":true}}}'),
            )
        before = db.execute("SELECT * FROM model_calls ORDER BY reference").fetchall()
        schema = db.execute("SELECT * FROM sqlite_master").fetchall()
    statements = []
    connect = sqlite3.connect

    def traced(*args, **kwargs):
        db = connect(*args, **kwargs)
        db.set_trace_callback(statements.append)
        return db

    monkeypatch.setattr(sqlite3, "connect", traced)
    permit = current(path, policy, [datetime(2026, 9, 10, tzinfo=UTC)])
    r = permit.reserve(100, 100)
    r.mark_transmission_started()
    r.settle(usage())
    with sqlite3.connect(path) as db:
        after = db.execute(
            "SELECT * FROM model_calls WHERE reference != ? ORDER BY reference", (r.reference,)
        ).fetchall()
        assert db.execute("SELECT * FROM sqlite_master").fetchall() == schema
        assert (
            db.execute("SELECT fingerprint FROM model_budget").fetchone()[0] == policy.fingerprint
        )
    assert before == after
    assert row(path, original.reference)[2] == 3244032
    assert permit.summary()["global_occupied_microyuan"] == 3244032 + 14 * 90 + 422
    assert not any(
        s.lstrip().upper().startswith(("CREATE ", "ALTER ", "DROP ")) for s in statements
    )
    with pytest.raises((RuntimeError, sqlite3.Error)):
        current(tmp_path / "missing.db", policy, [datetime(2026, 9, 10, tzinfo=UTC)])
    assert not (tmp_path / "missing.db").exists()


def test_concurrent_shared_fifty_yuan_includes_all_states(tmp_path):
    path, policy, old = ledger(tmp_path)
    with sqlite3.connect(path) as db:
        db.executemany(
            "INSERT INTO model_calls VALUES(?,?,?, ?,NULL)",
            [
                ("old", "settled", 20000000, 20000000),
                ("unknown", "unknown", 20000000, 20000000),
                ("inflight", "reserved", 9500000, 9500000),
            ],
        )
    clock = [datetime(2026, 9, 10, tzinfo=UTC)]
    permit = current(path, policy, clock)

    def reserve(index):
        try:
            (old if index % 2 else permit).reserve(100000, 0).unknown()
            return True
        except RuntimeError:
            return False

    with ThreadPoolExecutor(max_workers=8) as pool:
        successes = sum(pool.map(reserve, range(8)))
    total = current(path, policy, clock).summary()["global_occupied_microyuan"]
    assert 1 <= successes <= 2
    assert 49700000 <= total <= 50000000
    assert total == old.summary()["reserved_or_unknown_microyuan"] + 20000000


def test_current_unknown_reopen_preserves_occupancy_without_candidate_ceiling(tmp_path):
    path, policy, _ = ledger(tmp_path)
    clock = [datetime(2026, 9, 10, tzinfo=UTC)]
    permit = current(path, policy, clock)
    r = permit.reserve(2000000, 0)
    r.unknown()
    reopened = current(path, policy, clock)
    reopened.reserve(2000000, 0)
    assert reopened.summary()["global_occupied_microyuan"] == 8000000
    assert row(path, r.reference)[:3] == ("unknown", 4000000, 4000000)


def test_old_and_pro_rows_cannot_be_settled_by_current_or_reverse(tmp_path):
    path, policy, old = ledger(tmp_path)
    permit = current(path, policy, [datetime(2026, 9, 10, tzinfo=UTC)])
    original = old.reserve(1000, 1000)
    with sqlite3.connect(path) as db:
        db.execute(
            "INSERT INTO model_calls VALUES('pro','reserved',10000,10000,?)",
            ('{"candidate_budget":{"policy":{}}}',),
        )
    for reference in (original.reference, "pro"):
        before = row(path, reference)
        with pytest.raises(RuntimeError):
            permit.settle_reservation(reference, usage())
        assert row(path, reference) == before
    r = permit.reserve(1000, 1000)
    r.mark_transmission_started()
    before = row(path, r.reference)
    with pytest.raises(RuntimeError):
        old.settle_reservation(r.reference, usage())
    assert row(path, r.reference) == before


def test_policy_or_fingerprint_drift_never_reprices(tmp_path):
    path, policy, _ = ledger(tmp_path)
    clock = [datetime(2026, 9, 10, tzinfo=UTC)]
    permit = current(path, policy, clock)
    r = permit.reserve(1000, 1000)
    r.mark_transmission_started()
    changed = replace(permit.current_approval, approval_reference="different")
    with pytest.raises(RuntimeError):
        current(path, policy, clock, changed)
    encoded = row(path, r.reference)[3]
    metadata = json.loads(encoded)
    metadata["current_model_budget"]["policy"]["rates_cny_per_million"]["peak"][1] = "1"
    with sqlite3.connect(path) as db:
        db.execute(
            "UPDATE model_calls SET usage_json=? WHERE reference=?",
            (json.dumps(metadata), r.reference),
        )
    for action in (lambda: permit.reserve(1, 1), lambda: r.settle(usage())):
        with pytest.raises(RuntimeError):
            action()
    with sqlite3.connect(path) as db:
        db.execute("UPDATE model_calls SET usage_json=? WHERE reference=?", (encoded, r.reference))
        db.execute("UPDATE model_budget SET fingerprint='different'")
    for action in (lambda: permit.reserve(1, 1), lambda: r.settle(usage())):
        with pytest.raises(RuntimeError):
            action()
    assert row(path, r.reference)[2] == 10000


@pytest.mark.parametrize("inputs,outputs", [(0, 0), (-1, 1), (1, -1), (True, 1), (1.5, 1)])
def test_invalid_bounds_do_not_reserve(tmp_path, inputs, outputs):
    path, policy, old = ledger(tmp_path)
    permit = current(path, policy, [datetime(2026, 9, 10, tzinfo=UTC)])
    with pytest.raises(RuntimeError):
        permit.reserve(inputs, outputs)
    assert old.summary()["call_count"] == 0


def test_reservation_wait_does_not_overcharge_offpeak_transmission(tmp_path):
    path, policy, _ = ledger(tmp_path)
    clock = [datetime(2026, 9, 10, 3, tzinfo=UTC)]
    r = current(path, policy, clock).reserve(100, 100)
    clock[0] = datetime(2026, 9, 10, 4, tzinfo=UTC)
    r.mark_transmission_started()
    r.settle(usage())
    assert row(path, r.reference)[2] == 422


def test_pro_permit_cannot_settle_current_row(tmp_path):
    from app.composition.deepseek_assistant_model import CandidateModelApproval
    from app.composition.school_fact_candidate_budget import SqliteCandidateModelCallPermit

    path, policy, _ = ledger(tmp_path)
    clock = [datetime(2026, 9, 10, tzinfo=UTC)]
    r = current(path, policy, clock).reserve(1000, 1000)
    r.mark_transmission_started()
    pro = SqliteCandidateModelCallPermit(
        path=path,
        ledger_policy=policy,
        candidate_approval=CandidateModelApproval(
            experiment_id="f2-pro-20260910",
            approval_reference="pro",
            model="deepseek-v4-pro",
            expires_at=datetime(2026, 9, 11, tzinfo=UTC),
        ),
        clock=lambda: clock[0],
    )
    before = row(path, r.reference)
    with pytest.raises(RuntimeError):
        pro.settle_reservation(r.reference, usage(model="deepseek-v4-pro"))
    assert row(path, r.reference) == before


def test_usage_cannot_exceed_recorded_reservation_even_with_corrupt_bounds(tmp_path):
    path, policy, _ = ledger(tmp_path)
    clock = [datetime(2026, 9, 10, 1, tzinfo=UTC)]
    r = current(path, policy, clock).reserve(1, 1)
    r.mark_transmission_started()
    data = json.loads(row(path, r.reference)[3])
    data["current_model_budget"].update(max_input_tokens=1000, max_output_tokens=1000)
    with sqlite3.connect(path) as db:
        db.execute(
            "UPDATE model_calls SET usage_json=? WHERE reference=?", (json.dumps(data), r.reference)
        )
    before = row(path, r.reference)
    with pytest.raises(RuntimeError):
        r.settle(usage())
    assert row(path, r.reference) == before
