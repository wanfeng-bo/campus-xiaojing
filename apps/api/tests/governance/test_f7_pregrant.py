"""F7 pregrant OFF copies: positive proof and bounded rejection paths."""

import json
from copy import deepcopy
from hashlib import sha256
from importlib import import_module
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def repository_imports(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[4]))
    from scripts.f7_engineering import credential_remediation, pregrant, private_paths

    private = tmp_path / "external-private"
    monkeypatch.setattr(private_paths, "PRIVATE_ROOT", private)
    monkeypatch.setattr(private_paths, "REPOSITORY_ROOTS", (tmp_path / "repo",))
    monkeypatch.setattr(private_paths, "require_external_private_acl", lambda _: None)
    monkeypatch.setattr(pregrant, "OUTPUT_PRIVATE", private / "f7-execution")
    monkeypatch.setattr(credential_remediation, "secure_directory", lambda _: None)


def module():
    return import_module("scripts.f7_engineering.pregrant")


def old_configs():
    from tests.governance.test_f7_runtime_update import configurations

    old_product, _, old_writer, _ = configurations()
    old_product["model"].update(
        model="deepseek-flash", thinking="enabled", reasoning_effort="high"
    )
    return old_product, old_writer


def actual_probe(image="sha256:" + "a" * 64, source="b" * 64):
    m = module()
    return {
        "kind": "f7-isolated-off-actual-http-v1",
        "image": image,
        "source_sha256": source,
        "schema_contract": "school-facts-f7-v55",
        "original_instance_touched": False,
        "paid_model_calls": 0,
        "test_database": "owned-f7-disposable-fixture",
        "status": "all_cases_passed",
        "cases": [
            {"label": name, "status": "passed", "exit_code": 0,
             "elapsed_seconds": 0.1, "evidence_class": kind}
            for name, kind in m.ACTUAL_CASES
        ],
    }


def test_off_copy_changes_only_approved_contract_coordinates():
    m = module()
    original_product, original_writer = old_configs()
    saved = deepcopy((original_product, original_writer))
    product, writer = m.draft_off_pair(original_product, original_writer)
    assert (original_product, original_writer) == saved
    assert product["f7_packages_enabled"] is False
    assert writer["f7_packages_enabled"] is False
    assert product["model"]["model"] == "deepseek-flash"
    assert product["model"]["state_directory"] == "original-ledger"
    assert product["model"]["stage_budget"]["cap_microyuan"] == 3_000_000
    assert product["directory"] == original_product["directory"]
    assert writer["directory"] == original_writer["directory"]
    m.verify_off_pair(original_product, original_writer, product, writer)
    product["model"]["state_directory"] = "new-ledger"
    with pytest.raises(ValueError):
        m.verify_off_pair(original_product, original_writer, product, writer)


def test_actual_http_and_real_pg_are_required_not_older_mock_probe():
    m = module()
    proof = {"image": "sha256:" + "a" * 64, "source_sha256": "b" * 64,
             "schema_contract": "school-facts-f7-v55"}
    value = actual_probe()
    assert m.recovery_flags(value, proof=proof) == {
        "legacy_routes_verified": True,
        "own_receipt_verified": True,
        "new_package_prepare_rejected": True,
        "new_package_confirm_rejected": True,
    }
    for bad in (
        value | {"kind": "f7-isolated-off-candidate-probe"},
        value | {"status": "failed_requires_review"},
        value | {"original_instance_touched": True},
        value | {"paid_model_calls": False},
        value | {"cases": value["cases"][:-1]},
        value | {"cases": [value["cases"][0], *value["cases"]]},
    ):
        with pytest.raises(ValueError):
            m.recovery_flags(bad, proof=proof)
    changed = deepcopy(value)
    changed["cases"][4]["status"] = "failed"
    with pytest.raises(ValueError):
        m.recovery_flags(changed, proof=proof)
    changed = deepcopy(value)
    changed["cases"][4]["exit_code"] = False
    with pytest.raises(ValueError):
        m.recovery_flags(changed, proof=proof)


def test_existing_original_ledger_needs_exact_f6_image_volume_and_read_only_summary(
    monkeypatch,
):
    m = module()
    product, _ = old_configs()
    product["model"]["state_directory"] = m.MODEL_STATE
    mount = {"Type": "volume", "Name": m.MODEL_VOLUME,
             "Destination": m.MODEL_STATE, "RW": True}
    ledger = {"ledger_verified": True, "rows": 4, "unknown": 1,
              "charged_microyuan": 300, "reserved_microyuan": 200}
    calls = []

    def fake(args, **_):
        calls.append(args)
        if args[:2] == ["inspect", "--format"]:
            if args[2] == "{{.Image}}":
                return m.release.BASE.encode()
            if args[2] == "{{.State.Running}}":
                return b"true"
            return json.dumps([mount]).encode()
        return json.dumps(ledger).encode()

    monkeypatch.setattr(m, "_docker", fake)
    assert m.verify_original_ledger(product) == ledger
    assert calls[-1][0:2] == ["exec", m.ORIGINAL_PRODUCT]
    mount["Name"] = "some-other-volume"
    with pytest.raises(ValueError, match="mount"):
        m.verify_original_ledger(product)
    assert len([args for args in calls if args[0] == "exec"]) == 1


def test_pregrant_rejects_unsafe_path_and_changed_original_before_private_read(
    tmp_path, monkeypatch
):
    m = module()
    root = tmp_path / "repo"
    original = root / "infra/school-facts-f1/private/product-config.json"
    original.parent.mkdir(parents=True)
    raw = json.dumps(old_configs()[0]).encode()
    original.write_bytes(raw)
    monkeypatch.setattr(m, "ROOT", root)
    monkeypatch.setattr(m.migration, "ROOT", root)
    monkeypatch.setattr(m.migration, "_require_private_acl", lambda paths: None)
    assert m.read_original_config("product")[0] == raw
    with pytest.raises(ValueError):
        m.read_original_config("writer", path=tmp_path / "other.json")
    original.write_bytes(raw + b" ")
    with pytest.raises(ValueError):
        m.require_original_unchanged({"product": (raw, sha256(raw).hexdigest())})


def test_pregrant_writes_only_private_off_copies_after_full_validation(
    tmp_path, monkeypatch
):
    m = module()
    root = tmp_path / "repo"
    old_private = root / "infra/school-facts-f1/private"
    output_private = m.OUTPUT_PRIVATE
    old_private.mkdir(parents=True)
    output_private.mkdir(parents=True)
    old_product, old_writer = old_configs()
    for service, value in (("product", old_product), ("writer", old_writer)):
        (old_private / f"{service}-config.json").write_text(json.dumps(value))
    monkeypatch.setattr(m, "ROOT", root)
    monkeypatch.setattr(m.migration, "ROOT", root)
    monkeypatch.setattr(m.migration, "_require_private_acl", lambda paths: None)
    proof_path = tmp_path / "source-proof.json"
    proof_path.write_bytes(b"proof")
    probe_path = tmp_path / "off-actual-http.json"
    probe_path.write_bytes(b"probe")
    raw = b"current-source"
    proof = {
        "image": "sha256:" + "a" * 64,
        "source_sha256": "b" * 64,
        "schema_contract": "school-facts-f7-v55",
        "files": {"reviewed.py": sha256(raw).hexdigest()},
    }
    monkeypatch.setattr(m, "_load_final_proof", lambda _: (proof, b"proof"))
    monkeypatch.setattr(
        m, "_load_actual_probe", lambda *args, **kwargs:
        (actual_probe(), b"probe", m.recovery_flags(actual_probe(), proof=proof))
    )
    monkeypatch.setattr(m.release, "sources", lambda: {"reviewed.py": raw})
    monkeypatch.setattr(m, "verify_original_ledger", lambda _:
                        {"ledger_verified": True, "rows": 3, "unknown": 1,
                         "charged_microyuan": 1, "reserved_microyuan": 2})
    called = []

    def validate(value, *, proof):
        called.append(value)
        product = json.loads(Path(value["product_config"]).read_text())
        writer = json.loads(Path(value["writer_config"]).read_text())
        m.verify_off_pair(old_product, old_writer, product, writer)
        assert proof["image"] == value["image"]

    monkeypatch.setattr(m.migration, "require_pregrant_recovery", validate)
    result = m.prepare(proof_path=proof_path, actual_probe_path=probe_path)
    assert result["status"] == "f7_pregrant_recovery_verified"
    assert result["ledger_unknown_calls"] == 1
    evidence = Path(result["evidence"])
    assert evidence.is_relative_to(output_private)
    assert len(called) == 1
    assert json.loads(evidence.read_text(encoding="utf-8")) == called[0]
    assert json.loads((old_private / "product-config.json").read_text()) == old_product
    assert json.loads((old_private / "writer-config.json").read_text()) == old_writer


def test_pregrant_aborts_if_original_changes_during_final_validation(
    tmp_path, monkeypatch
):
    m = module()
    root = tmp_path / "repo"
    old_private = root / "infra/school-facts-f1/private"
    output_private = m.OUTPUT_PRIVATE
    old_private.mkdir(parents=True)
    output_private.mkdir(parents=True)
    old_product, old_writer = old_configs()
    for service, value in (("product", old_product), ("writer", old_writer)):
        (old_private / f"{service}-config.json").write_text(json.dumps(value))
    monkeypatch.setattr(m, "ROOT", root)
    monkeypatch.setattr(m.migration, "ROOT", root)
    monkeypatch.setattr(m.migration, "_require_private_acl", lambda paths: None)
    proof_path = tmp_path / "proof.json"
    proof_path.write_bytes(b"proof")
    probe_path = tmp_path / "probe.json"
    probe_path.write_bytes(b"probe")
    raw = b"current-source"
    proof = {"image": "sha256:" + "a" * 64, "source_sha256": "b" * 64,
             "schema_contract": "school-facts-f7-v55",
             "files": {"reviewed.py": sha256(raw).hexdigest()}}
    monkeypatch.setattr(m, "_load_final_proof", lambda _: (proof, b"proof"))
    monkeypatch.setattr(m, "_load_actual_probe", lambda *args, **kwargs:
                        (actual_probe(), b"probe", m.recovery_flags(actual_probe(), proof=proof)))
    monkeypatch.setattr(m.release, "sources", lambda: {"reviewed.py": raw})
    monkeypatch.setattr(m, "verify_original_ledger", lambda _: {"ledger_verified": True,
        "rows": 0, "unknown": 0, "charged_microyuan": 0, "reserved_microyuan": 0})

    def mutate(*args, **kwargs):
        (old_private / "writer-config.json").write_text(json.dumps(old_writer) + " ")

    monkeypatch.setattr(m.migration, "require_pregrant_recovery", mutate)
    with pytest.raises(ValueError, match="changed"):
        m.prepare(proof_path=proof_path, actual_probe_path=probe_path)
    assert not list(output_private.rglob("pregrant-recovery.json"))


@pytest.mark.parametrize("failure", [None, "secure", "validate"])
def test_new_private_directory_is_hardened_and_validated_before_config_write(
    tmp_path, monkeypatch, failure
):
    from scripts.f7_engineering import credential_remediation

    m = module()
    m.OUTPUT_PRIVATE.mkdir(parents=True)
    product, writer = old_configs()
    documents = {"product": product, "writer": writer}
    monkeypatch.setattr(m, "_load_final_proof", lambda _: ({"source_sha256": "b" * 64}, b"proof"))
    monkeypatch.setattr(m, "_load_actual_probe", lambda *a, **k: ({}, b"probe", {}))
    monkeypatch.setattr(m, "read_original_config", lambda service:
                        (json.dumps(documents[service]).encode(), documents[service]))
    monkeypatch.setattr(m, "verify_original_ledger", lambda _: {})
    events = []
    created = []

    def secure(folder):
        assert folder.parent == m.OUTPUT_PRIVATE
        assert folder.is_dir() and not list(folder.iterdir())
        created.append(folder)
        events.append("secure")
        if failure == "secure":
            raise ValueError("synthetic_dacl_write_failed")

    def validate(path, root):
        if path == m.OUTPUT_PRIVATE:
            return
        assert path in created, "new directory must receive an explicit current-user DACL"
        events.append("validate")
        if failure == "validate":
            raise ValueError("synthetic_strict_acl_rejected")

    def before_write(folder, service, value):
        assert events == ["secure", "validate"]
        events.append("write")
        raise RuntimeError("stop_before_synthetic_config_write")

    monkeypatch.setattr(credential_remediation, "secure_directory", secure)
    monkeypatch.setattr(m, "_acl", validate)
    monkeypatch.setattr(m, "_write_private_copy", before_write)
    expected = {
        None: (RuntimeError, "stop_before_synthetic_config_write", ["secure", "validate", "write"]),
        "secure": (ValueError, "synthetic_dacl_write_failed", ["secure"]),
        "validate": (ValueError, "synthetic_strict_acl_rejected", ["secure", "validate"]),
    }
    error, message, expected_events = expected[failure]
    with pytest.raises(error, match=message):
        m.prepare(proof_path=tmp_path / "proof", actual_probe_path=tmp_path / "probe")
    assert events == expected_events
    assert created and not list(created[0].iterdir())


def test_ledger_unknown_reservation_is_counted_once(tmp_path, monkeypatch):
    import contextlib
    import io
    import sqlite3
    import sys

    m = module()
    with sqlite3.connect(tmp_path / "model-budget.sqlite3") as db:
        db.execute("CREATE TABLE model_budget(id INTEGER, fingerprint TEXT)")
        db.execute("INSERT INTO model_budget VALUES(1, ?)", ("a" * 64,))
        db.execute("CREATE TABLE model_calls(state TEXT, reserved INTEGER, charged INTEGER)")
        db.executemany("INSERT INTO model_calls VALUES(?,?,?)", [
            ("settled", 43_000_000, 43_000_000),
            ("unknown", 6_000_000, 6_000_000),
        ])

    def docker(args, **kwargs):
        if args[:3] == ["inspect", "--format", "{{.Image}}"]:
            return m.release.BASE.encode()
        if args[:3] == ["inspect", "--format", "{{.State.Running}}"]:
            return b"true"
        if args[:3] == ["inspect", "--format", "{{json .Mounts}}"]:
            return json.dumps([{"Type": "volume", "Name": m.MODEL_VOLUME,
                "RW": True, "Destination": m.MODEL_STATE}]).encode()
        assert args[0] == "exec"
        output = io.StringIO()
        with monkeypatch.context() as local:
            local.setattr(sys, "argv", ["ledger-read", str(tmp_path)])
            with contextlib.redirect_stdout(output):
                exec(compile(args[args.index("-c") + 1], "<isolated-ledger-read>", "exec"), {})
        return output.getvalue().encode()

    monkeypatch.setattr(m, "_docker", docker)
    result = m.verify_original_ledger({"model": {"state_directory": m.MODEL_STATE,
        "budget": {"cap_fen": 5000}}})
    assert result["charged_microyuan"] == 49_000_000
    assert result["reserved_microyuan"] == 6_000_000
