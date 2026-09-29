import sqlite3
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import pytest

from app.composition.deepseek_assistant_model import ModelCallUsage


def budget(tmp_path, **changes):
    from app.composition.school_fact_model_budget import ApprovedModelBudget, SqliteModelCallPermit

    fields = dict(
        approval_reference="synthetic-unit-test-only",
        cap_fen=10,
        input_cny_per_million=Decimal("10"),
        output_cny_per_million=Decimal("20"),
    )
    fields.update(changes)
    return SqliteModelCallPermit(
        path=tmp_path / "budget.sqlite3", policy=ApprovedModelBudget.model_validate(fields)
    )


def test_budget_is_durable_across_reopen_and_unknown_does_not_release(tmp_path):
    first = budget(tmp_path)
    reservation = first.reserve(5000, 2500)
    reservation.unknown()
    with pytest.raises(RuntimeError):
        budget(tmp_path).reserve(1, 1)
    assert first.summary()["reserved_or_unknown_microyuan"] == 100000


def test_settled_usage_frees_only_unused_reservation_once(tmp_path):
    permit = budget(tmp_path)
    reservation = permit.reserve(5000, 2500)
    usage = ModelCallUsage("explicit-test-model", "r1", "c1", 100, 100, 200, None, None)
    reservation.settle(usage)
    reservation.settle(usage)
    summary = permit.summary()
    assert summary["settled_estimated_microyuan"] == 3000
    assert summary["reserved_or_unknown_microyuan"] == 0
    budget(tmp_path).reserve(1, 1)


def test_concurrent_reservations_cannot_each_spend_the_full_cap(tmp_path):
    permit = budget(tmp_path)

    def reserve(_):
        try:
            permit.reserve(1000, 500)
            return True
        except RuntimeError:
            return False

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(reserve, range(12))) == 5


def test_changed_approval_cannot_reset_an_existing_budget(tmp_path):
    budget(tmp_path).reserve(1, 1)
    with pytest.raises(RuntimeError):
        budget(tmp_path, cap_fen=50)


def test_budget_persists_usage_metadata_but_no_prompt_or_key(tmp_path):
    permit = budget(tmp_path)
    permit.reserve(10, 10).settle(ModelCallUsage("m", "r", "c", 1, 1, 2, None, None))
    with sqlite3.connect(tmp_path / "budget.sqlite3") as db:
        payload = db.execute("select usage_json from model_calls").fetchone()[0]
    assert "prompt_tokens" in payload and "api_key" not in payload and "messages" not in payload


def test_peak_estimate_counts_reasoning_inside_completion_once_and_keeps_metadata(tmp_path):
    permit = budget(
        tmp_path,
        cap_fen=5000,
        input_cny_per_million=Decimal("3"),
        output_cny_per_million=Decimal("9"),
    )
    usage = ModelCallUsage(
        "deepseek-v4-flash",
        "r1",
        "c1",
        500,
        1000,
        1500,
        450,
        50,
        reasoning_tokens=800,
    )
    reservation = permit.reserve(1000, 2000)
    reservation.settle(usage)
    assert permit.summary()["settled_estimated_microyuan"] == 10500
    with sqlite3.connect(tmp_path / "budget.sqlite3") as db:
        payload = db.execute("SELECT usage_json FROM model_calls").fetchone()[0]
    import json

    assert json.loads(payload)["reasoning_tokens"] == 800
    assert "reasoning_content" not in payload
    assert (
        budget(
            tmp_path,
            cap_fen=5000,
            input_cny_per_million=Decimal("3"),
            output_cny_per_million=Decimal("9"),
        ).summary()
        == permit.summary()
    )


@pytest.mark.parametrize(
    "inputs,outputs,total,reasoning",
    [(1, 1, 999, 0), (1, 1, 2, 2), (True, 1, 2, 0), (1, 1, 2, -1), (1, 20, 21, 0)],
)
def test_invalid_or_over_reserved_usage_never_releases_inflight_budget(
    tmp_path, inputs, outputs, total, reasoning
):
    permit = budget(tmp_path)
    reservation = permit.reserve(10, 10)
    usage = ModelCallUsage(
        "deepseek-v4-flash",
        "r1",
        "c1",
        inputs,
        outputs,
        total,
        None,
        None,
        reasoning_tokens=reasoning,
    )
    with pytest.raises(RuntimeError):
        reservation.settle(usage)
    assert permit.summary()["reserved_or_unknown_microyuan"] == 300
    assert permit.summary()["settled_estimated_microyuan"] == 0


def test_reasoning_usage_replay_must_be_identical_and_unknown_is_not_automatic_refund(tmp_path):
    permit = budget(tmp_path)
    first = permit.reserve(10, 10)
    first.unknown()
    assert budget(tmp_path).summary()["reserved_or_unknown_microyuan"] == 300
    first.settle(ModelCallUsage("m", "r", "c", 1, 2, 3, None, None, reasoning_tokens=1))
    with pytest.raises(RuntimeError):
        first.settle(ModelCallUsage("m", "r", "c", 1, 2, 3, None, None, reasoning_tokens=2))
    assert permit.summary()["settled_estimated_microyuan"] == 50
