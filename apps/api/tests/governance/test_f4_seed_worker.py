"""Fixed worker output contains only checked publication evidence."""

import sys
from pathlib import Path
from types import SimpleNamespace as N

import pytest

ROOT = Path(__file__).parents[4]
sys.path.insert(0, str(ROOT / "scripts/f4_engineering"))


def test_unknown_worker_recovers_only_and_emits_no_token():
    from seed_publication import SeedRecoveryPending
    from seed_worker import publish_stage

    calls = []
    row = {"state": "unknown", "change_set": {"changes": []}, "plan_sha256": "a" * 64}
    port = N(
        journal={"batches": {"foundation": row}},
        receipt=lambda stage: (_ for _ in ()).throw(SeedRecoveryPending()),
        prepare=lambda *a, **k: calls.append("prepare"),
        confirm=lambda *a: calls.append("confirm"),
    )
    with pytest.raises(SeedRecoveryPending):
        publish_stage(port, stage="foundation", assignment_epoch=3)
    assert calls == []


def test_worker_rejects_epoch_before_confirm():
    from seed_worker import publish_stage

    calls = []
    port = N(
        journal={"batches": {}},
        current_school_fact_version=lambda: 4,
        prepare=lambda *a, **k: N(permit=(None, None, None, 2)),
        confirm=lambda *a: calls.append("confirm"),
    )
    with pytest.raises(PermissionError):
        publish_stage(port, stage="foundation", assignment_epoch=3)
    assert calls == []


def test_worker_masks_exception_and_library_output(monkeypatch, capsys):
    import io

    import seed_worker as m

    secret = "selected-token-must-never-appear"
    monkeypatch.setattr(m.sys, "stdin", io.StringIO('{"maintainer_token":"' + secret + '"}'))

    def failing(value):
        print(value["maintainer_token"])
        print(value["maintainer_token"], file=sys.stderr)
        raise RuntimeError(value["maintainer_token"])

    monkeypatch.setattr(m, "run", failing)
    assert m.main() == 1
    output = capsys.readouterr()
    assert secret not in output.out + output.err
    assert output.err == ""
    assert "seed_worker_rejected" in output.out


def test_worker_rejects_incomplete_source_manifest(tmp_path):
    import hashlib

    from seed_worker import verify_sources

    raw = b"{}"
    (tmp_path / "source-manifest.json").write_bytes(raw)
    with pytest.raises(ValueError, match="incomplete"):
        verify_sources(hashlib.sha256(raw).hexdigest(), workspace=tmp_path)


def test_worker_emits_only_fixed_receipt_metadata():
    from uuid import uuid4

    from seed_worker import publish_stage

    from app.platform.school_facts.ports import SchoolFactApplyResult

    row = {
        "state": "succeeded",
        "change_set": {"schema_version": 3},
        "plan_sha256": "a" * 64,
        "receipt": {"batch_id": "fixed"},
        "unexpected_private_field": "do-not-emit",
    }
    result = SchoolFactApplyResult(uuid4(), 9, 2, False)
    port = N(journal={"batches": {"foundation": row}}, receipt=lambda stage: result)
    evidence = publish_stage(port, stage="foundation", assignment_epoch=3)
    assert evidence["result"]["target_version"] == 9
    assert set(evidence["journal"]) == {"state", "change_set", "plan_sha256", "receipt"}


def test_worker_host_unknown_cannot_prepare_without_journal():
    from seed_publication import SeedRecoveryPending
    from seed_worker import publish_stage

    calls = []
    port = N(journal={"batches": {}}, prepare=lambda *a, **kw: calls.append("prepare"))
    with pytest.raises(SeedRecoveryPending):
        publish_stage(port, stage="foundation", assignment_epoch=3, receipt_only=True)
    assert calls == []
