import importlib.util
import json
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.governance.test_f5_engineering import foundation


def module():
    path = Path(__file__).resolve().parents[4] / "scripts/f5_engineering/runtime_governance.py"
    spec = importlib.util.spec_from_file_location("f5_runtime_governance_test", path)
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
    service = SimpleNamespace(
        receipt=lambda *a, **k: None,
        prepare=lambda *a, **k: pytest.fail("unknown must not prepare"),
    )
    with pytest.raises(RuntimeError, match="receipt lookup only"):
        helper.execute_reviewed_commands(
            manifest=manifest,
            service=service,
            credential="test-only",
            evidence_path=path,
            digest="fixed",
        )


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
    result = helper.execute_reviewed_commands(
        manifest=manifest,
        service=service,
        credential="test-only",
        evidence_path=path,
        digest="fixed",
    )
    assert result["status"] == "succeeded"
    service.prepare = lambda *a, **k: pytest.fail("original receipts must be reused")
    recovered = helper.execute_reviewed_commands(
        manifest=manifest,
        service=service,
        credential="test-only",
        evidence_path=path,
        digest="fixed",
        receipt_only=True,
    )
    assert recovered == result


def test_changed_manifest_cannot_reuse_execution_journal(tmp_path):
    helper = module()
    path = tmp_path / "receipts.json"
    helper._save(path, {"manifest_sha256": "old", "commands": {}})
    with pytest.raises(ValueError, match="different fixed manifest"):
        helper.execute_reviewed_commands(
            manifest=foundation(),
            service=None,
            credential="test-only",
            evidence_path=path,
            digest="new",
        )


def test_phase_binds_current_schema_original_identity_and_existing_closing_supervisor(
    tmp_path, monkeypatch
):
    helper = module()
    manifest = foundation()
    manifest = manifest.model_copy(update={"actor_reference": "f2-original-governance-operator"})
    private = {
        "actor_reference": manifest.actor_reference,
        "database_name": "campus_ai_school_facts_f1",
        "database_user": "campus_ai_governance_writer",
        "expected_school_id": str(manifest.school_id),
        "expected_anchor_id": str(manifest.school_id),
        "expected_system_identifier": "known-system",
    }
    monkeypatch.setattr(helper, "ROOT", tmp_path)
    monkeypatch.setattr(helper, "load_config_reference", lambda path: private)
    monkeypatch.setattr(helper, "config_reference", lambda *args: {"base_config": "unused"})
    observed = {}
    monkeypatch.setattr(
        helper,
        "OriginalSchoolGovernanceControl",
        lambda **kwargs: observed.setdefault("control", kwargs),
    )

    def bounded(**kwargs):
        observed["window"] = kwargs
        return {"status": "stub-only"}

    monkeypatch.setattr(helper, "run_bounded_window", bounded)
    timeline = {
        "school_id": str(manifest.school_id),
        "authorization_reference": "approved-exact",
        "first_execution_t0": manifest.grant_epoch.isoformat(),
        "fixed_deadline": manifest.grant_valid_to.isoformat(),
    }
    args = dict(
        docker=Path("not-invoked"),
        image="sha256:" + "a" * 64,
        folder=tmp_path / "phase",
        manifest=manifest,
        governance_base=tmp_path / "base.json",
        issuer_reference="independent-issuer",
        authorization_reference="approved-exact",
        timeline=timeline,
    )
    with pytest.raises(ValueError):
        helper.run_phase(**args, schema_contract="school-facts-f4-v31")
    assert not args["folder"].exists()
    assert helper.run_phase(**args) == {"status": "stub-only"}
    assert observed["control"]["system_identifier"] == "known-system"
    assert observed["control"]["application_name"].startswith("f3-window-")
    assert observed["window"]["execute"] is True
    assert observed["window"]["approval"]["fixed_deadline"] == timeline["fixed_deadline"]
    assert (
        json.loads((args["folder"] / "config-reference.json").read_text())["base_config"]
        == "/run/secrets/governance-base.json"
    )


def test_receipt_only_without_original_outcome_never_creates_a_new_attempt(tmp_path):
    helper = module()
    service = SimpleNamespace(
        receipt=lambda *a, **k: None,
        prepare=lambda *a, **k: pytest.fail("receipt-only may not prepare"),
    )
    with pytest.raises(RuntimeError, match="receipt lookup only"):
        helper.execute_reviewed_commands(
            manifest=foundation(),
            service=service,
            credential="test",
            evidence_path=tmp_path / "journal.json",
            digest="fixed",
            receipt_only=True,
        )
