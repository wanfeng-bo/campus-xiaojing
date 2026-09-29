import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[4]
SCRIPT = ROOT / "scripts/f4_engineering/acceptance_runner.py"


def module():
    assert SCRIPT.exists(), "Bounded metadata-only browser acceptance runner missing"
    spec = importlib.util.spec_from_file_location("f4_acceptance_runner", SCRIPT)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def plan():
    import json

    return json.loads(
        (ROOT / "apps/api/tests/fixtures/f4_acceptance_fixed_v7.json").read_text(encoding="utf-8")
    )


def test_runner_pins_all_16_and_rejects_transcript_material():
    m = module()
    p = plan()
    state = m.new_run(p, plan_sha256="a" * 64, code_sha256="b" * 64)
    assert m.next_turn(state, p)["id"] == "M01"
    state = m.arm(state, p)
    with pytest.raises(ValueError):
        m.record(state, p, {"id": "M01", "reply": "full answer"})
    with pytest.raises(ValueError):
        m.record(state, p, {"id": "M02"})
    assert state["observations"] == []


def test_failed_unknown_attempt_stays_recorded_and_does_not_auto_retry():
    m = module()
    p = plan()
    state = m.new_run(p, plan_sha256="a" * 64, code_sha256="b" * 64)
    state = m.arm(state, p)
    state = m.record(
        state,
        p,
        {
            "id": "M01",
            "request_id": "req-1",
            "delivery": "unknown",
            "outcome": "timeout",
            "cold": True,
            "timing_ms": {"full": None},
            "assertions": [],
            "checks": {},
        },
    )
    with pytest.raises(ValueError):
        m.next_turn(state, p)
    state = m.reconcile(state, "M01", request_id="req-1", terminal="failed")
    assert m.next_turn(state, p)["id"] == "M02"
    assert state["observations"][0]["outcome"] == "timeout"


def test_timing_projection_rejects_body_and_retains_only_numeric_usage():
    m = module()
    value = m.sanitize_timing(
        {
            "request_id": "req-1",
            "seconds": 2,
            "prompt": "secret",
            "usage": [{"prompt_tokens": 5, "reasoning": "private text", "reasoning_tokens": 2}],
            "counts": {"model": 2, "prompt": "secret"},
        }
    )
    assert "secret" not in str(value) and "private text" not in str(value)
    assert value["usage"] == [{"prompt_tokens": 5, "reasoning_tokens": 2}]


def test_m12_cannot_be_called_success_without_real_confirmation_evidence():
    m = module()
    p = plan()
    state = m.new_run(p, plan_sha256="a" * 64, code_sha256="b" * 64)
    state["observations"] = [
        {"id": f"M{i:02}", "request_id": f"req-{i}", "delivery": "completed"} for i in range(1, 12)
    ]
    state = m.arm(state, p)
    sample = {
        "id": "M12",
        "request_id": "req-12",
        "delivery": "completed",
        "outcome": "correct_query",
        "cold": False,
        "timing_ms": {"accepted": 100, "first_usable": 4000, "full": 4000},
        "assertions": [True] * len(p["turns"][11]["required_conclusions"]),
        "checks": {
            "identity": True,
            "context": True,
            "prerequisites": True,
            "independent_read": True,
        },
    }
    with pytest.raises(ValueError):
        m.record(state, p, sample)
    sample.update(receipt_locator_sha256="c" * 64, receipt_state="applied")
    sample["checks"]["confirmed_in_product"] = True
    recorded = m.record(state, p, sample)
    assert recorded["observations"][-1]["classification_fixed"] == "综合"
    sample["receipt_state"] = "unknown"
    with pytest.raises(ValueError):
        m.record(state, p, sample)


def test_success_cannot_replace_missing_assertions_or_rewrite_classification():
    m = module()
    p = plan()
    state = m.arm(m.new_run(p, plan_sha256="a" * 64, code_sha256="b" * 64), p)
    sample = {
        "id": "M01",
        "request_id": "req-1",
        "delivery": "completed",
        "outcome": "correct_query",
        "cold": False,
        "timing_ms": {"full": 1},
        "assertions": [],
        "checks": {},
    }
    with pytest.raises(ValueError):
        m.record(state, p, sample)
    sample["classification_fixed"] = "综合"
    with pytest.raises(ValueError):
        m.record(state, p, sample)


def test_armed_attempt_blocks_advancement_and_duplicate_arming():
    m = module()
    p = plan()
    state = m.arm(m.new_run(p, plan_sha256="a" * 64, code_sha256="b" * 64), p)
    with pytest.raises(ValueError):
        m.next_turn(state, p)
    with pytest.raises(ValueError):
        m.arm(state, p)
    assert state["pending"]["id"] == "M01"


def test_snapshot_never_overwrites_and_cannot_escape_owned_directory(tmp_path):
    m = module()
    m.ROOT = tmp_path
    owned = tmp_path / "runtime/f4-execution"
    owned.mkdir(parents=True)
    output = owned / "observation.json"
    m.write_new(output, {"count": 1})
    with pytest.raises(FileExistsError):
        m.write_new(output, {"count": 2})
    assert "1" in output.read_text()
    with pytest.raises(ValueError):
        m.write_new(tmp_path / "outside.json", {})
