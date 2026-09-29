"""Only structural worker diagnostics may leave the private F6 process."""

import json

import pytest
from scripts.f6_engineering import runtime_governance as worker


def raising_worker(folder, *, receipt_only):
    password = "NEVER-SAVE-password-and-private-local"
    try:
        driver = RuntimeError("SELECT secret_column FROM secrets; " + password)
        driver.sqlstate = "28000"
        raise driver
    except RuntimeError as cause:
        raise ValueError("private-DSN user:password@host SQL parameters") from cause


def test_failed_receipt_worker_saves_only_structural_cause_diagnostics(
    tmp_path, monkeypatch, capsys
):
    calls = []

    def dispatch(folder, *, receipt_only):
        calls.append((folder, receipt_only))
        raising_worker(folder, receipt_only=receipt_only)

    monkeypatch.setattr(worker, "worker", dispatch)
    assert hasattr(worker, "main"), "F6 worker diagnostic entry is missing"
    assert worker.main(["entry", "--receipt-worker", str(tmp_path)]) == 2
    assert calls == [(tmp_path, True)]
    raw = (tmp_path / "worker-error.json").read_text()
    doc = json.loads(raw)
    assert set(doc) == {"exception_chain"}
    assert [item["type"] for item in doc["exception_chain"]] == ["ValueError", "RuntimeError"]
    assert doc["exception_chain"][1]["sqlstate"] == "28000"
    assert any(
        frame["file"] == "scripts/f6_engineering/runtime_governance.py"
        for item in doc["exception_chain"]
        for frame in item["frames"]
    )
    for item in doc["exception_chain"]:
        assert set(item) == {"type", "sqlstate", "frames"}
        for frame in item["frames"]:
            assert set(frame) == {"file", "line", "function"}
            assert not frame["file"].startswith(("/", "E:", "C:"))
    stdout = capsys.readouterr()
    assert not stdout.out
    for secret in (
        "NEVER-SAVE",
        "password",
        "SELECT",
        "private-DSN",
        "secret_column",
        str(tmp_path),
    ):
        assert secret not in raw + stdout.err


def test_diagnostic_write_failure_still_exits_two_and_never_prints_exception(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(worker, "worker", raising_worker)
    assert hasattr(worker, "main"), "F6 worker diagnostic entry is missing"
    # Missing output directory is intentional; the diagnostic must not reopen
    # governance, create a new execution folder or replace the safe exit path.
    missing = tmp_path / "not-created"
    assert worker.main(["entry", "--worker", str(missing)]) == 2
    assert not missing.exists()
    captured = capsys.readouterr()
    assert not captured.out and "NEVER-SAVE" not in captured.err
    assert "F6 Governance execution unavailable" in captured.err


def test_success_does_not_write_diagnostic_or_change_worker_arguments(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(worker, "worker", lambda folder, **kw: calls.append((folder, kw)))
    assert hasattr(worker, "main"), "F6 worker diagnostic entry is missing"
    assert worker.main(["entry", "--worker", str(tmp_path)]) == 0
    assert calls == [(tmp_path, {"receipt_only": False})]
    assert not (tmp_path / "worker-error.json").exists()


def test_only_fixed_entry_arguments_remain_accepted():
    assert hasattr(worker, "main"), "F6 worker diagnostic entry is missing"
    with pytest.raises(SystemExit):
        worker.main(["entry", "--retry", "/execution"])


def test_sqlstate_must_be_exact_and_cause_cycle_is_bounded():
    assert hasattr(worker, "safe_worker_error"), "F6 structural diagnostic is missing"
    error = RuntimeError("never serialize this")
    error.sqlstate = "28000-password"
    error.__cause__ = error
    doc = worker.safe_worker_error(error)
    assert len(doc["exception_chain"]) == 1
    assert doc["exception_chain"][0]["sqlstate"] is None
    assert "password" not in json.dumps(doc)


def test_real_main_entry_retains_exit_two_without_exception_traceback(
    tmp_path, monkeypatch, capsys
):
    import runpy
    import sys

    monkeypatch.setattr(
        worker.retained,
        "worker",
        lambda *args, **kwargs: raising_worker(*args, receipt_only=kwargs["receipt_only"]),
    )
    monkeypatch.setattr(sys, "argv", ["entry", "--receipt-worker", str(tmp_path)])
    with pytest.raises(SystemExit) as stopped:
        runpy.run_path(
            str(worker.ROOT / "scripts/f6_engineering/runtime_governance.py"), run_name="__main__"
        )
    assert stopped.value.code == 2
    output = capsys.readouterr()
    assert not output.out and "Traceback" not in output.err and "password" not in output.err
    assert (tmp_path / "worker-error.json").exists()
