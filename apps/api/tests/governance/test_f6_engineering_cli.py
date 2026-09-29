import importlib.util
import json
from hashlib import sha256
from pathlib import Path

from tests.governance.test_f6_engineering import T0, foundation


def cli():
    path = Path(__file__).resolve().parents[4] / "scripts/school_facts_f6_governance.py"
    assert path.exists(), "F5 deployment CLI is missing"
    spec = importlib.util.spec_from_file_location("f6_engineering_test_cli", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def files(tmp_path):
    manifest = foundation()
    manifest_path, config_path = tmp_path / "manifest.json", tmp_path / "private.json"
    manifest_path.write_text(manifest.model_dump_json(), encoding="utf-8")
    digest = sha256(manifest_path.read_bytes()).hexdigest()
    config_path.write_text(
        json.dumps(
            dict(
                approved_manifest_sha256=digest,
                actor_reference=manifest.actor_reference,
                approval_reference=manifest.approval_reference,
                grant_epoch=T0.isoformat(),
                credential="independent-private-credential",
                database_host="127.0.0.1",
                database_port=5432,
                database_name="unused_test",
                database_user="campus_ai_governance_writer",
                database_password="never-log-secret",
                expected_anchor_id=str(manifest.school_id),
                expected_school_id=str(manifest.school_id),
                expected_system_identifier="test",
                expected_schema_contract="school-facts-f6-v44",
                reader={},
            )
        ),
        encoding="utf-8",
    )
    return manifest_path, config_path, digest


def test_review_and_close_never_connect_and_closed_execute_is_rejected(tmp_path, monkeypatch):
    tool = cli()
    manifest, config, digest = files(tmp_path)
    monkeypatch.setattr(
        tool,
        "compose",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("No database connection authorized")),
    )
    base = ["--manifest", str(manifest), "--config", str(config)]
    assert tool.main(["review", *base, "--output", str(tmp_path / "review.json")]) == 0
    assert (
        tool.main(
            [
                "close",
                *base,
                "--confirm",
                "CLOSE-F6:" + digest,
                "--output",
                str(tmp_path / "closed.json"),
            ]
        )
        == 0
    )
    assert config.with_suffix(".json.closed").exists()
    assert (
        tool.main(
            [
                "execute",
                *base,
                "--index",
                "0",
                "--confirm",
                "wrong",
                "--output",
                str(tmp_path / "write.json"),
            ]
        )
        == 2
    )


def test_mutated_pin_stays_revoked_even_if_file_restored(tmp_path):
    tool = cli()
    manifest, config, digest = files(tmp_path)
    original = config.read_bytes()
    current = tool.current_approval_guard(
        config, manifest, config_sha256=sha256(original).hexdigest(), manifest_sha256=digest
    )
    assert current()
    config.write_bytes(original + b" ")
    assert not current()
    config.write_bytes(original)
    assert not current()


def test_failed_execution_disposes_connections_and_can_be_closed_without_database(
    tmp_path, monkeypatch, capsys
):
    tool = cli()
    manifest, config, digest = files(tmp_path)
    model = foundation()
    disposed = []

    class Engine:
        def dispose(self):
            disposed.append(True)

    class Broken:
        def receipt(self, *args, **kwargs):
            raise RuntimeError("database_password=never-log-secret")

    monkeypatch.setattr(tool, "compose", lambda *a, **k: (Broken(), (Engine(),)))
    base = ["--manifest", str(manifest), "--config", str(config)]
    assert (
        tool.main(
            [
                "execute",
                *base,
                "--index",
                "0",
                "--confirm",
                tool.command_marker(digest, model.materialize()[0]),
                "--output",
                str(tmp_path / "failed.json"),
            ]
        )
        == 2
    )
    assert disposed == [True]
    assert "never-log-secret" not in capsys.readouterr().err
    assert (
        tool.main(
            [
                "close",
                *base,
                "--confirm",
                "CLOSE-F6:" + digest,
                "--output",
                str(tmp_path / "closed.json"),
            ]
        )
        == 0
    )


def test_closed_receipt_uses_receipt_only_composition_without_reopening_approval(
    tmp_path, monkeypatch
):
    tool = cli()
    manifest, config, digest = files(tmp_path)
    config.with_suffix(".json.closed").write_text("closed", encoding="utf-8")
    observed = []

    class Reader:
        def receipt(self, *args, **kwargs):
            return None

    def compose(*args, **kwargs):
        observed.append(kwargs.get("receipt_only"))
        assert not kwargs["approval_is_current"]()
        return Reader(), ()

    monkeypatch.setattr(tool, "compose", compose)
    assert (
        tool.main(
            [
                "receipt",
                "--manifest",
                str(manifest),
                "--config",
                str(config),
                "--index",
                "0",
                "--output",
                str(tmp_path / "receipt.json"),
            ]
        )
        == 0
    )
    assert observed == [True]
