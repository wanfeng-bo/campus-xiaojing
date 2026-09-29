import importlib.util
import json
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.governance.test_f4_engineering import foundation


def module():
    path = Path(__file__).resolve().parents[4] / "scripts/f4_engineering/runtime_governance.py"
    spec = importlib.util.spec_from_file_location("f4_runtime_governance_test", path)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


@dataclass
class Receipt:
    target_version: int = 1


def test_unknown_governance_attempt_never_repeats_prepare_or_confirmation(tmp_path):
    helper = module()
    manifest = foundation()
    key = manifest.materialize()[0].idempotency_key
    path = tmp_path / "receipts.json"
    helper._save(path, {"manifest_sha256": "fixed", "commands": {key: {"state": "confirming"}}})
    service = SimpleNamespace(receipt=lambda *a, **k: None,
        prepare=lambda *a, **k: pytest.fail("unknown must not prepare"))
    with pytest.raises(RuntimeError, match="receipt lookup only"):
        helper.execute_reviewed_commands(manifest=manifest, service=service,
            credential="test-only", evidence_path=path, digest="fixed")


def test_confirm_intent_is_durable_before_dispatch_and_recovery_reads_original(tmp_path):
    helper = module()
    manifest = foundation()
    path = tmp_path / "receipts.json"
    committed = {}
    current = []
    def receipt(command, **kwargs):
        return committed.get(command.idempotency_key)
    def prepare(command, **kwargs):
        current[:] = [command.idempotency_key]
        return SimpleNamespace(confirmation_token="test-confirm")
    def confirm(token, **kwargs):
        assert json.loads(path.read_text())["commands"][current[0]]["state"] == "confirming"
        committed[current[0]] = Receipt()
    service = SimpleNamespace(receipt=receipt, prepare=prepare, confirm=confirm)
    result = helper.execute_reviewed_commands(manifest=manifest, service=service,
        credential="test-only", evidence_path=path, digest="fixed")
    assert result["status"] == "succeeded"
    service.prepare = lambda *a, **k: pytest.fail("original receipts must be reused")
    recovered = helper.execute_reviewed_commands(manifest=manifest, service=service,
        credential="test-only", evidence_path=path, digest="fixed", receipt_only=True)
    assert recovered == result


def test_changed_manifest_cannot_reuse_execution_journal(tmp_path):
    helper = module()
    path = tmp_path / "receipts.json"
    helper._save(path, {"manifest_sha256": "old", "commands": {}})
    with pytest.raises(ValueError, match="different fixed manifest"):
        helper.execute_reviewed_commands(manifest=foundation(), service=None,
            credential="test-only", evidence_path=path, digest="new")
