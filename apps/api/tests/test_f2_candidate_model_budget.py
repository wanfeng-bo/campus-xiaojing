import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from importlib.util import find_spec

import pytest

from app.composition.deepseek_assistant_model import ModelCallUsage
from app.composition.school_fact_model_budget import ApprovedModelBudget, SqliteModelCallPermit


def ledger(tmp_path):
    policy = ApprovedModelBudget(
        approval_reference="original-approved-flash",
        cap_fen=5000,
        input_cny_per_million=Decimal("3"),
        output_cny_per_million=Decimal("9"),
    )
    path = tmp_path / "budget.sqlite3"
    SqliteModelCallPermit(path=path, policy=policy)
    return path, policy


def candidate(path, policy, clock, approval=None):
    assert find_spec("app.composition.school_fact_candidate_budget"), "candidate budget missing"
    from app.composition.deepseek_assistant_model import CandidateModelApproval
    from app.composition.school_fact_candidate_budget import SqliteCandidateModelCallPermit

    approval = approval or CandidateModelApproval(
        experiment_id="f2-pro-20260910",
        approval_reference="approved-pro-limited",
        model="deepseek-v4-pro",
        expires_at=datetime(2026, 9, 10, 13, 40, tzinfo=UTC),
    )
    return SqliteCandidateModelCallPermit(
        path=path, ledger_policy=policy, candidate_approval=approval, clock=lambda: clock[0]
    )


def usage(**changes):
    fields = dict(
        model="deepseek-v4-pro",
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


def test_candidate_open_does_not_create_or_change_schema_or_history(tmp_path):
    path, policy = ledger(tmp_path)
    original = SqliteModelCallPermit(path=path, policy=policy).reserve(10, 20)
    original.unknown()
    with sqlite3.connect(path) as db:
        before = (
            db.execute("SELECT * FROM sqlite_master").fetchall(),
            db.execute("SELECT * FROM model_budget").fetchall(),
            db.execute("SELECT * FROM model_calls").fetchall(),
        )
    candidate(path, policy, [datetime(2026, 9, 10, tzinfo=UTC)])
    with sqlite3.connect(path) as db:
        after = (
            db.execute("SELECT * FROM sqlite_master").fetchall(),
            db.execute("SELECT * FROM model_budget").fetchall(),
            db.execute("SELECT * FROM model_calls").fetchall(),
        )
    assert before == after
    with pytest.raises((RuntimeError, sqlite3.Error)):
        candidate(tmp_path / "missing.sqlite3", policy, [datetime(2026, 9, 10, tzinfo=UTC)])
    assert not (tmp_path / "missing.sqlite3").exists()


def test_atomic_experiment_limit_includes_unknown_and_survives_reopen(tmp_path):
    path, policy = ledger(tmp_path)
    clock = [datetime(2026, 9, 10, tzinfo=UTC)]
    permit = candidate(path, policy, clock)

    def reserve(_):
        try:
            permit.reserve(100000, 10000).unknown()  # 1.17 yuan peak reservation
            return True
        except RuntimeError:
            return False

    with ThreadPoolExecutor(max_workers=6) as pool:
        assert sum(pool.map(reserve, range(6))) == 1
    assert candidate(path, policy, clock).summary()["experiment_occupied_microyuan"] == 1170000
    with sqlite3.connect(path) as db:
        values = db.execute("SELECT usage_json FROM model_calls").fetchall()
    assert all(
        json.loads(value[0])["candidate_budget"]["policy"]["experiment_id"] == "f2-pro-20260910"
        for value in values
    )


def test_global_limit_is_shared_with_flash(tmp_path):
    path, policy = ledger(tmp_path)
    flash = SqliteModelCallPermit(path=path, policy=policy)
    flash.reserve(0, 5333333).unknown()  # 47.999997 yuan occupied
    permit = candidate(path, policy, [datetime(2026, 9, 10, tzinfo=UTC)])
    permit.reserve(100000, 10000)
    with pytest.raises(RuntimeError):
        permit.reserve(100000, 10000)
    with pytest.raises(RuntimeError):
        flash.reserve(300000, 1)


@pytest.mark.parametrize(
    "start,end,expected_band,expected_cost",
    [
        ("2026-09-10T00:00:00+00:00", "2026-09-10T00:01:00+00:00", "offpeak", 1452),
        ("2026-09-10T01:00:00+00:00", "2026-09-10T01:01:00+00:00", "peak", 2904),
        ("2026-09-10T00:59:59+00:00", "2026-09-10T01:00:01+00:00", "peak", 2904),
        ("2026-09-10T00:00:00+00:00", "2026-09-10T11:00:00+00:00", "peak", 2904),
    ],
)
def test_actual_transmission_window_selects_conservative_cache_price(
    tmp_path, start, end, expected_band, expected_cost
):
    path, policy = ledger(tmp_path)
    clock = [datetime.fromisoformat(start) - timedelta(minutes=1)]
    permit = candidate(path, policy, clock)
    reservation = permit.reserve(1000, 1000)
    clock[0] = datetime.fromisoformat(start)
    reservation.mark_transmission_started()
    clock[0] = datetime.fromisoformat(end)
    reservation.settle(usage())
    state, reserved, charged, encoded = row(path, reservation.reference)
    assert state == "settled" and reserved == 36000 and charged == expected_cost
    metadata = json.loads(encoded)
    assert metadata["model"] == "deepseek-v4-pro"
    assert metadata["candidate_budget"]["billing_band"] == expected_band
    assert metadata["candidate_budget"]["transmission_started_at"] == start
    assert metadata["candidate_budget"]["completed_at"] == end


def test_cache_missing_uses_miss_and_replay_does_not_reprice_later(tmp_path):
    path, policy = ledger(tmp_path)
    clock = [datetime(2026, 9, 10, 0, tzinfo=UTC)]
    permit = candidate(path, policy, clock)
    reservation = permit.reserve(1000, 1000)
    reservation.mark_transmission_started()
    measured = usage(prompt_cache_hit_tokens=None, prompt_cache_miss_tokens=None)
    reservation.settle(measured)
    first = row(path, reservation.reference)
    assert first[2] == 1800
    clock[0] = datetime(2026, 9, 10, 2, tzinfo=UTC)
    reservation.settle(measured)
    assert row(path, reservation.reference) == first
    with pytest.raises(RuntimeError):
        reservation.settle(usage())


@pytest.mark.parametrize(
    "changes",
    [
        {"model": "deepseek-v4-flash"},
        {"prompt_cache_miss_tokens": 19},
        {"reasoning_tokens": 101},
        {"prompt_tokens": 1001, "total_tokens": 1101},
    ],
)
def test_invalid_usage_never_releases_reservation(tmp_path, changes):
    path, policy = ledger(tmp_path)
    permit = candidate(path, policy, [datetime(2026, 9, 10, tzinfo=UTC)])
    reservation = permit.reserve(1000, 1000)
    reservation.mark_transmission_started()
    with pytest.raises(RuntimeError):
        reservation.settle(usage(**changes))
    reservation.unknown()
    assert row(path, reservation.reference)[:3] == ("unknown", 36000, 36000)


def test_expiry_missing_start_and_wrong_owner_cannot_release_or_send(tmp_path):
    path, policy = ledger(tmp_path)
    clock = [datetime(2026, 9, 10, tzinfo=UTC)]
    permit = candidate(path, policy, clock)
    reservation = permit.reserve(1000, 1000)
    with pytest.raises(RuntimeError):
        reservation.settle(usage())
    with pytest.raises(RuntimeError):
        SqliteModelCallPermit(path=path, policy=policy).settle_reservation(
            reservation.reference, usage()
        )
    assert row(path, reservation.reference)[2] == 36000
    clock[0] = permit.candidate_approval.expires_at
    with pytest.raises(RuntimeError):
        reservation.mark_transmission_started()
    with pytest.raises(RuntimeError):
        permit.reserve(1, 1)


def test_same_experiment_cannot_reset_by_changing_identity_or_approval(tmp_path):
    path, policy = ledger(tmp_path)
    clock = [datetime(2026, 9, 10, tzinfo=UTC)]
    permit = candidate(path, policy, clock)
    permit.reserve(100, 100).unknown()
    with pytest.raises(RuntimeError):
        candidate(path, policy, clock, replace(permit.candidate_approval, experiment_id="new-run"))
    with pytest.raises(RuntimeError):
        candidate(
            path, policy, clock, replace(permit.candidate_approval, approval_reference="new-policy")
        )


@pytest.mark.parametrize(
    "start,end,band",
    [
        ("2026-09-10T03:59:59+00:00", "2026-09-10T04:00:00+00:00", "peak"),
        ("2026-09-10T04:00:00+00:00", "2026-09-10T05:59:59+00:00", "offpeak"),
        ("2026-09-10T05:59:59+00:00", "2026-09-10T06:00:00+00:00", "peak"),
        ("2026-09-10T10:00:00+00:00", "2026-09-10T11:00:00+00:00", "offpeak"),
        ("2026-09-12T01:00:00+00:00", "2026-09-12T10:00:00+00:00", "offpeak"),
        ("2026-09-11T11:00:00+00:00", "2026-09-14T02:00:00+00:00", "peak"),
    ],
)
def test_peak_calendar_includes_lunch_boundaries_weekends_and_interior_days(start, end, band):
    from app.composition.school_fact_candidate_budget import _band

    assert _band(datetime.fromisoformat(start), datetime.fromisoformat(end)) == band


def test_flash_and_pro_concurrent_reservations_share_one_atomic_global_cap(tmp_path):
    path, policy = ledger(tmp_path)
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO model_calls VALUES('prior','unknown',49500000,49500000,NULL)")
    flash = SqliteModelCallPermit(path=path, policy=policy)
    pro = candidate(path, policy, [datetime(2026, 9, 10, tzinfo=UTC)])

    def reserve(index):
        try:
            if index % 2:
                flash.reserve(100000, 0)
            else:
                pro.reserve(33333, 0)
            return True
        except RuntimeError:
            return False

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(reserve, range(8))) == 1
    assert pro.summary()["global_occupied_microyuan"] <= 50000000


def test_started_unknown_can_settle_after_expiry_but_cannot_transmit_twice(tmp_path):
    path, policy = ledger(tmp_path)
    clock = [datetime(2026, 9, 10, tzinfo=UTC)]
    permit = candidate(path, policy, clock)
    reservation = permit.reserve(1000, 1000)
    reservation.mark_transmission_started()
    with pytest.raises(RuntimeError):
        reservation.mark_transmission_started()
    reservation.unknown()
    clock[0] = permit.candidate_approval.expires_at + timedelta(seconds=1)
    reservation.settle(usage())
    assert row(path, reservation.reference)[:3] == ("settled", 36000, 2904)
    reservation.unknown()
    assert row(path, reservation.reference)[0] == "settled"


def test_original_fingerprint_or_candidate_price_drift_fails_closed(tmp_path):
    path, policy = ledger(tmp_path)
    clock = [datetime(2026, 9, 10, tzinfo=UTC)]
    permit = candidate(path, policy, clock)
    reservation = permit.reserve(1000, 1000)
    with sqlite3.connect(path) as db:
        encoded = row(path, reservation.reference)[3]
        metadata = json.loads(encoded)
        metadata["candidate_budget"]["policy"]["rates_cny_per_million"]["peak"][1] = "1"
        db.execute(
            "UPDATE model_calls SET usage_json=? WHERE reference=?",
            (json.dumps(metadata), reservation.reference),
        )
    with pytest.raises(RuntimeError):
        permit.reserve(1, 1)
    assert row(path, reservation.reference)[2] == 36000
    with sqlite3.connect(path) as db:
        db.execute(
            "UPDATE model_calls SET usage_json=? WHERE reference=?",
            (encoded, reservation.reference),
        )
        db.execute("UPDATE model_budget SET fingerprint='changed'")
    with pytest.raises(RuntimeError):
        permit.reserve(1, 1)


def test_candidate_statements_never_execute_ddl(tmp_path, monkeypatch):
    path, policy = ledger(tmp_path)
    statements = []
    connect = sqlite3.connect

    def traced_connect(*args, **kwargs):
        connection = connect(*args, **kwargs)
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(sqlite3, "connect", traced_connect)
    permit = candidate(path, policy, [datetime(2026, 9, 10, tzinfo=UTC)])
    reservation = permit.reserve(100, 100)
    reservation.mark_transmission_started()
    reservation.settle(usage())
    assert statements
    assert not any(
        statement.lstrip().upper().startswith(("CREATE ", "ALTER ", "DROP "))
        for statement in statements
    )


def test_unknown_candidate_blocks_new_calls_after_restart_without_refunding(tmp_path):
    from app.platform.assistant.runtime import AssistantServiceError

    path, policy = ledger(tmp_path)
    clock = [datetime(2026, 9, 10, tzinfo=UTC)]
    original = SqliteModelCallPermit(path=path, policy=policy).reserve(1, 1)
    original.unknown()
    permit = candidate(path, policy, clock)
    prior = permit.reserve(100, 100)
    with pytest.raises(AssistantServiceError) as pending:
        candidate(path, policy, clock).reserve(1, 1)
    assert pending.value.reason == "dependency"
    prior.unknown()
    with pytest.raises(AssistantServiceError) as unknown:
        candidate(path, policy, clock).reserve(1, 1)
    assert unknown.value.reason == "dependency"
    assert row(path, prior.reference)[:3] == ("unknown", 3600, 3600)


def test_settled_calls_use_three_yuan_experiment_cap_without_reset(tmp_path):
    from app.platform.assistant.runtime import AssistantServiceError

    path, policy = ledger(tmp_path)
    clock = [datetime(2026, 9, 10, 1, tzinfo=UTC)]
    for _ in range(2):
        permit = candidate(path, policy, clock)
        reservation = permit.reserve(100000, 10000)
        reservation.mark_transmission_started()
        reservation.settle(
            usage(
                prompt_tokens=100000,
                completion_tokens=10000,
                total_tokens=110000,
                prompt_cache_hit_tokens=None,
                prompt_cache_miss_tokens=None,
            )
        )
    assert permit.summary()["experiment_occupied_microyuan"] == 2340000
    with pytest.raises(AssistantServiceError) as exhausted:
        candidate(path, policy, clock).reserve(100000, 10000)
    assert exhausted.value.reason == "budget"
