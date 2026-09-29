import hashlib
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest


def test_preflight_reports_all_errors_without_execution():
    from scripts.f5_engineering.seed_worker import check_job

    now = datetime.now(UTC)
    issues = check_job(
        {
            "stage": "arbitrary",
            "assignment_epoch": 0,
            "receipt_only": "yes",
            "issued_at": now.isoformat(),
            "fixed_deadline": (now + timedelta(days=8)).isoformat(),
            "source_manifest_sha256": "x",
            "approved_input_sha256": "x",
        },
        {},
        now=now,
    )
    assert len(issues) >= 5


def test_worker_argv_has_only_private_network_and_no_logs(tmp_path):
    from scripts.f5_engineering.runtime_seed import NETWORK, worker_argv

    args = worker_argv(
        docker="docker",
        image="sha256:" + "a" * 64,
        name="f5-seed-test",
        owner="owned",
        folder=tmp_path,
        mounts=[],
        network=NETWORK,
    )
    assert "none" == args[args.index("--log-driver") + 1]
    assert "--read-only" in args
    assert args[-1] == "/workspace/scripts/f5_engineering/seed_worker.py"
    assert not any("f4" in str(a) for a in args)


def test_worker_receipt_only_uses_original_port_without_prepare():
    from scripts.f5_engineering.publication import PublicationRecoveryPending, publish_stage

    calls = []
    port = SimpleNamespace(
        stages={"foundation"},
        journal={"batches": {"foundation": {"state": "unknown"}}},
        receipt=lambda stage: None,
        prepare=lambda *a, **k: calls.append("prepare"),
    )
    with pytest.raises(PublicationRecoveryPending):
        publish_stage(port, stage="foundation", assignment_epoch=3, receipt_only=True)
    assert not calls


def test_source_manifest_requires_f5_worker_and_publication(tmp_path):
    from scripts.f5_engineering.seed_worker import verify_sources

    raw = json.dumps({}).encode()
    (tmp_path / "source-manifest.json").write_bytes(raw)
    with pytest.raises(ValueError):
        verify_sources(hashlib.sha256(raw).hexdigest(), workspace=tmp_path)


def test_pure_preparation_lists_all_missing_coordinates():
    from scripts.f5_engineering.runtime_seed import preparation_checklist

    result = preparation_checklist(raw=b"{}", approved_input_sha256="0" * 64, coordinates={})
    assert result["status"] == "preparation_incomplete"
    assert len(result["issues"]) >= 8
    assert result["executed"] is False
    assert "token" not in json.dumps(result)


def test_worker_masks_library_and_exception_credentials(monkeypatch, capsys):
    from io import StringIO

    from scripts.f5_engineering import seed_worker

    monkeypatch.setattr(
        seed_worker.sys, "stdin", StringIO('{"maintainer_token":"SECRET_VALUE_NEVER_LOG"}')
    )

    def bad(value):
        print("SECRET_VALUE_NEVER_LOG")
        raise RuntimeError("SECRET_VALUE_NEVER_LOG")

    monkeypatch.setattr(seed_worker, "run", bad)
    assert seed_worker.main() == 1
    captured = capsys.readouterr()
    assert "SECRET_VALUE_NEVER_LOG" not in captured.out + captured.err
