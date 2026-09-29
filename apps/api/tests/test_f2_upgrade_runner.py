"""Offline fail-closed tests; these never open a database connection."""

import importlib.util
import json
from copy import deepcopy
from pathlib import Path

import pytest


def runner():
    path = Path(__file__).resolve().parents[3] / "scripts/school_facts_f2_upgrade.py"
    assert path.is_file(), "The bounded F2 upgrade runner is missing"
    spec = importlib.util.spec_from_file_location("f2_upgrade_runner", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def document(tmp_path):
    evidence = tmp_path / "backup-evidence.json"
    evidence.write_text('{"verified_backup":true}', encoding="utf-8")
    from hashlib import sha256

    return {
        "purpose": "school-facts-f2-synthetic",
        "source_revision": "20260905_22",
        "target_revision": "20260906_24",
        "migration": {
            "host": "postgres",
            "port": 5432,
            "db": "campus_ai_school_facts_f1",
            "user": "campus_ai_migration",
            "password": "PRIVATE_PASSWORD",
            "context_signing_key": "PRIVATE_SIGNING_KEY" * 3,
        },
        "expected_host": "postgres",
        "expected_port": 5432,
        "expected_database": "campus_ai_school_facts_f1",
        "expected_database_oid": 16384,
        "expected_system_identifier": "7682019857215991846",
        "expected_anchor_id": "f147faa7-783e-47eb-9762-8815a72bf855",
        "expected_school_id": "c1137be2-0734-4b69-8937-37dbd741f141",
        "expected_initialization_id": "11111111-1111-4111-8111-111111111111",
        "expected_table_oids": {
            "school_database_anchor": 1,
            "schools": 2,
            "school_fact_initializations": 3,
            "governance_bootstrap_states": 4,
        },
        "backup_evidence_path": str(evidence),
        "backup_evidence_sha256": sha256(evidence.read_bytes()).hexdigest(),
        "migration_sha256": {"20260906_23": "a" * 64, "20260906_24": "b" * 64},
    }


def test_explicit_f2_configuration_is_required(tmp_path, monkeypatch):
    module = runner()
    raw = document(tmp_path)
    module.UpgradeConfiguration.model_validate(raw)
    for field, value in [
        ("purpose", "school-facts-f1-synthetic"),
        ("source_revision", "20260906_23"),
        ("target_revision", "head"),
        ("target_revision", "20260905_22"),
        ("expected_host", "other"),
        ("expected_database", "other"),
        ("expected_table_oids", {}),
        ("writer", {"password": "SECRET"}),
    ]:
        invalid = deepcopy(raw)
        invalid[field] = value
        with pytest.raises(ValueError):
            module.UpgradeConfiguration.model_validate(invalid)
    monkeypatch.setenv("POSTGRES_MIGRATION_PASSWORD", "ENV_SECRET")
    del raw["migration"]["password"]
    with pytest.raises(ValueError):
        module.UpgradeConfiguration.model_validate(raw)


def test_backup_and_migration_digest_gates(tmp_path):
    module = runner()
    raw = document(tmp_path)
    root = tmp_path / "api"
    versions = root / "alembic/versions"
    versions.mkdir(parents=True)
    from hashlib import sha256

    for revision in raw["migration_sha256"]:
        data = (revision + "-reviewed-migration").encode()
        (versions / (revision + "_test.py")).write_bytes(data)
        raw["migration_sha256"][revision] = sha256(data).hexdigest()
    config = module.UpgradeConfiguration.model_validate(raw)
    assert module.verify_artifacts(config, root) == raw["migration_sha256"]
    Path(raw["backup_evidence_path"]).write_text("changed", encoding="utf-8")
    with pytest.raises(ValueError):
        module.verify_artifacts(config, root)


def snapshot(raw):
    return {
        "schema": "20260905_22",
        "session_user": "campus_ai_migration",
        "database": raw["expected_database"],
        "database_oid": raw["expected_database_oid"],
        "system_identifier": raw["expected_system_identifier"],
        "anchor": {
            "anchor_id": raw["expected_anchor_id"],
            "school_id": raw["expected_school_id"],
            "database_name": raw["expected_database"],
            "database_oid": raw["expected_database_oid"],
            "cluster_system_identifier": raw["expected_system_identifier"],
        },
        "school_ids": [raw["expected_school_id"]],
        "table_oids": raw["expected_table_oids"],
        "fact_initialization": {
            "id": raw["expected_initialization_id"],
            "school_id": raw["expected_school_id"],
            "state": "closed",
            "closed_at": "2026-09-05T13:01:53Z",
        },
        "governance_bootstrap": {
            "school_id": raw["expected_school_id"],
            "state": "closed",
            "closed_at": "2026-09-05T13:01:25Z",
        },
        "governance_role": {
            "rolcanlogin": True,
            "rolinherit": False,
            "rolsuper": False,
            "rolcreatedb": False,
            "rolcreaterole": False,
            "rolreplication": False,
            "rolbypassrls": False,
            "memberships": 0,
            "can_connect": True,
        },
    }


def test_binding_schema_and_closed_state_fail_closed(tmp_path):
    module = runner()
    raw = document(tmp_path)
    config = module.UpgradeConfiguration.model_validate(raw)
    good = snapshot(raw)
    module.validate_snapshot(config, good)
    current24 = deepcopy(good)
    current24["schema"] = "20260906_24"
    module.validate_snapshot(config, current24)
    for field, value in [
        ("schema", "20260906_23"),
        ("schema", "20260824_20"),
        ("session_user", "campus_ai_db_owner"),
        ("database_oid", 999),
        ("system_identifier", "99999999999999"),
        ("school_ids", []),
        ("table_oids", {}),
        ("governance_role", None),
    ]:
        bad = deepcopy(good)
        bad[field] = value
        with pytest.raises(ValueError):
            module.validate_snapshot(config, bad)
    for field in ["fact_initialization", "governance_bootstrap"]:
        for key, value in [("state", "open"), ("closed_at", None), ("school_id", "other")]:
            bad = deepcopy(good)
            bad[field][key] = value
            with pytest.raises(ValueError):
                module.validate_snapshot(config, bad)
    for key in good["governance_role"]:
        bad = deepcopy(good)
        bad["governance_role"][key] = not good["governance_role"][key]
        with pytest.raises(ValueError):
            module.validate_snapshot(config, bad)


def test_execute_exact_upgrade_and_idempotent_recheck(tmp_path, monkeypatch):
    module = runner()
    raw = document(tmp_path)
    config = module.UpgradeConfiguration.model_validate(raw)
    before = snapshot(raw)
    after = deepcopy(before)
    after["schema"] = "20260906_24"
    calls = []
    monkeypatch.setattr(module, "verify_artifacts", lambda *_: raw["migration_sha256"])
    values = iter([before, after])
    monkeypatch.setattr(module, "read_snapshot", lambda *_: next(values))
    monkeypatch.setattr(module, "run_alembic", lambda *args: calls.append(args))
    result = module.execute(config, tmp_path)
    assert result["status"] == "upgraded"
    assert len(calls) == 1
    assert "PRIVATE" not in json.dumps(result)
    calls.clear()
    monkeypatch.setattr(module, "read_snapshot", lambda *_: after)
    assert module.execute(config, tmp_path)["status"] == "already_at_target"
    assert calls == []


def test_postcheck_changes_rejected_and_cli_errors_hide_secrets(tmp_path, monkeypatch, capsys):
    module = runner()
    raw = document(tmp_path)
    before = snapshot(raw)
    after = deepcopy(before)
    after["schema"] = "20260906_24"
    after["fact_initialization"]["closed_at"] = "changed"
    values = iter([before, after])
    monkeypatch.setattr(module, "verify_artifacts", lambda *_: raw["migration_sha256"])
    monkeypatch.setattr(module, "read_snapshot", lambda *_: next(values))
    monkeypatch.setattr(module, "run_alembic", lambda *_: None)
    with pytest.raises(ValueError):
        module.execute(module.UpgradeConfiguration.model_validate(raw), tmp_path)
    path = tmp_path / "private.json"
    path.write_text(json.dumps(raw), encoding="utf-8")

    def fail(*_):
        raise RuntimeError("PRIVATE_PASSWORD PRIVATE_SIGNING_KEY postgres://sensitive")

    monkeypatch.setattr(module, "execute", fail)
    assert module.main(["--config", str(path), "--output", str(tmp_path / "receipt.json")]) == 2
    output = capsys.readouterr()
    assert "PRIVATE" not in output.out + output.err
    assert "postgres://" not in output.out + output.err


def test_alembic_exact_target_and_minimal_environment(tmp_path, monkeypatch):
    module = runner()
    config = module.UpgradeConfiguration.model_validate(document(tmp_path))
    monkeypatch.setenv("POSTGRES_PASSWORD", "RUNTIME_SECRET")
    monkeypatch.setenv("POSTGRES_MIGRATION_HOST", "wrong-host")
    seen = []

    class Result:
        returncode = 0

    def run(command, **kwargs):
        seen.append((command, kwargs))
        return Result()

    monkeypatch.setattr(module.subprocess, "run", run)
    module.run_alembic(config, tmp_path)
    command, kwargs = seen[0]
    assert command[-3:] == ["alembic", "upgrade", "20260906_24"]
    assert kwargs["env"]["POSTGRES_MIGRATION_HOST"] == "postgres"
    assert "POSTGRES_PASSWORD" not in kwargs["env"]
    assert kwargs["stdout"] == module.subprocess.DEVNULL
    assert kwargs["stderr"] == module.subprocess.DEVNULL
    Result.returncode = 1
    with pytest.raises(ValueError):
        module.run_alembic(config, tmp_path)


def test_anchor_validator_receives_anchor_then_school_and_disposes(tmp_path, monkeypatch):
    module = runner()
    config = module.UpgradeConfiguration.model_validate(document(tmp_path))
    seen = []

    class Result:
        def mappings(self):
            return self

        def scalars(self):
            return self

        def one(self):
            return {}

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def execution_options(self, **_):
            return self

        def begin(self):
            return self

        def scalar(self, *_):
            return "campus_ai_migration"

        def execute(self, statement, parameters=None):
            if "assert_school_database_anchor" in str(statement):
                assert ":anchor, :school, :system" in str(statement)
                assert parameters is not None
                assert parameters["anchor"] == config.expected_anchor_id
                assert parameters["school"] == config.expected_school_id
                seen.append("correct_binding")
                raise ValueError("stop before further offline probe")
            return Result()

    class Engine:
        def connect(self):
            return Connection()

        def dispose(self):
            seen.append("disposed")

    monkeypatch.setattr(module, "create_engine", lambda *_args, **_kwargs: Engine())
    with pytest.raises(ValueError):
        module.read_snapshot(config)
    assert seen == ["correct_binding", "disposed"]


def document25(tmp_path, database="campus_ai_school_facts_f1"):
    raw = document(tmp_path)
    raw.update(
        source_revision="20260906_24",
        target_revision="20260906_25",
        expected_database=database,
        migration_sha256={"20260906_25": "c" * 64},
    )
    raw["migration"]["db"] = database
    return raw


def test_only_approved_revision_pairs_and_exact_clone_target(tmp_path):
    module = runner()
    for db in ["campus_ai_school_facts_f1", "campus_ai_school_facts_f2_validation_final"]:
        module.UpgradeConfiguration.model_validate(document25(tmp_path, db))
    for source, target in [
        ("20260905_22", "20260906_25"),
        ("20260906_25", "20260906_24"),
        ("20260906_24", "20260906_24"),
        ("20260906_24", "head"),
    ]:
        raw = document25(tmp_path)
        raw.update(source_revision=source, target_revision=target)
        with pytest.raises(ValueError):
            module.UpgradeConfiguration.model_validate(raw)
    raw = document(tmp_path)
    raw["expected_database"] = raw["migration"]["db"] = "campus_ai_school_facts_f2_validation_final"
    with pytest.raises(ValueError):
        module.UpgradeConfiguration.model_validate(raw)
    with pytest.raises(ValueError):
        module.UpgradeConfiguration.model_validate(document25(tmp_path, "another_database"))
    raw = document25(tmp_path)
    raw["migration_sha256"]["20260906_24"] = "b" * 64
    with pytest.raises(ValueError):
        module.UpgradeConfiguration.model_validate(raw)


def test_nonsecret_constraints_merge_existing_migration_only_in_memory(tmp_path):
    module = runner()
    raw = document25(tmp_path)
    private_path, constraints_path = tmp_path / "existing.json", tmp_path / "constraints.json"
    private = {"purpose": "school-facts-f1-synthetic", "migration": raw.pop("migration")}
    private_path.write_text(json.dumps(private), encoding="utf-8")
    constraints_path.write_text(json.dumps(raw), encoding="utf-8")
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    config = module.load_configuration(private_path, constraints_path)
    assert config.target_revision == "20260906_25"
    assert config.migration.password.get_secret_value() == "PRIVATE_PASSWORD"
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == before
    raw["migration"] = private["migration"]
    constraints_path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError):
        module.load_configuration(private_path, constraints_path)
    del raw["migration"]
    constraints_path.write_text(json.dumps(raw), encoding="utf-8")
    private["writer"] = {"password": "FORBIDDEN_WRITER_SECRET"}
    private_path.write_text(json.dumps(private), encoding="utf-8")
    with pytest.raises(ValueError):
        module.load_configuration(private_path, constraints_path)


def test_24_to_25_exact_execution_and_idempotent_recheck(tmp_path, monkeypatch):
    module = runner()
    raw = document25(tmp_path)
    config = module.UpgradeConfiguration.model_validate(raw)
    before = snapshot(raw)
    before["schema"] = "20260906_24"
    after = deepcopy(before)
    after["schema"] = "20260906_25"
    for version in ["20260905_22", "20260906_23", "20260906_26"]:
        bad = {**before, "schema": version}
        with pytest.raises(ValueError):
            module.validate_snapshot(config, bad)
    seen = []

    class Result:
        returncode = 0

    def run(command, **kwargs):
        seen.append(command)
        return Result()

    monkeypatch.setattr(module.subprocess, "run", run)
    module.run_alembic(config, tmp_path)
    assert seen[0][-3:] == ["alembic", "upgrade", "20260906_25"]
    monkeypatch.setattr(module, "verify_artifacts", lambda *_: raw["migration_sha256"])
    values = iter([before, after])
    monkeypatch.setattr(module, "read_snapshot", lambda *_: next(values))
    result = module.execute(config, tmp_path)
    assert result["status"] == "upgraded" and result["target_revision"] == "20260906_25"
    seen.clear()
    monkeypatch.setattr(module, "read_snapshot", lambda *_: after)
    assert module.execute(config, tmp_path)["status"] == "already_at_target"
    assert seen == []


def test_constraints_cli_failure_retains_25_pending_receipt_without_secrets(
    tmp_path, monkeypatch, capsys
):
    module = runner()
    raw = document25(tmp_path)
    private_path, constraints_path, output = (
        tmp_path / name for name in ["existing.json", "constraints.json", "receipt.json"]
    )
    private_path.write_text(
        json.dumps({"purpose": "school-facts-f1-synthetic", "migration": raw.pop("migration")}),
        encoding="utf-8",
    )
    constraints_path.write_text(json.dumps(raw), encoding="utf-8")

    def fail(*_):
        raise RuntimeError("PRIVATE_PASSWORD")

    monkeypatch.setattr(module, "execute", fail)
    result = module.main(
        [
            "--config",
            str(private_path),
            "--constraints",
            str(constraints_path),
            "--output",
            str(output),
        ]
    )
    assert result == 2
    assert json.loads(output.read_text()) == {
        "status": "pending_or_unknown",
        "target_revision": "20260906_25",
    }
    captured = capsys.readouterr()
    assert "PRIVATE_PASSWORD" not in captured.out + captured.err


def test_25_requires_separate_constraints_and_exact_migration_artifact(tmp_path):
    from hashlib import sha256

    module = runner()
    raw = document25(tmp_path)
    full_private = tmp_path / "full-private.json"
    full_private.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="separate nonsecret constraints"):
        module.load_configuration(full_private)
    root = tmp_path / "api"
    versions = root / "alembic/versions"
    versions.mkdir(parents=True)
    migration = versions / "20260906_25_reviewed.py"
    migration.write_bytes(b"reviewed revision25 artifact")
    raw["migration_sha256"]["20260906_25"] = sha256(migration.read_bytes()).hexdigest()
    config = module.UpgradeConfiguration.model_validate(raw)
    assert module.verify_artifacts(config, root) == raw["migration_sha256"]
    migration.write_bytes(b"different revision25 artifact")
    with pytest.raises(ValueError, match="digest mismatch"):
        module.verify_artifacts(config, root)
    migration.write_bytes(b"reviewed revision25 artifact")
    (versions / "20260906_25_duplicate.py").write_bytes(migration.read_bytes())
    with pytest.raises(ValueError, match="Exact migration artifact"):
        module.verify_artifacts(config, root)


def test_stdin_configuration_uses_bounded_read_and_checks_target(tmp_path, monkeypatch):
    import io

    module = runner()
    raw = document25(tmp_path, "campus_ai_school_facts_f2_validation_final")
    payload = json.dumps(
        {"purpose": "school-facts-f2-synthetic", "migration": raw.pop("migration")}
    ).encode()
    constraints = tmp_path / "constraints.json"
    constraints.write_text(json.dumps(raw), encoding="utf-8")
    real_open = Path.open
    consumed = []

    class Pipe(io.BytesIO):
        def read(self, size=-1):
            consumed.append(size)
            return super().read(size)

    def open_path(path, *args, **kwargs):
        return (
            Pipe(payload) if path.as_posix() == "/dev/stdin" else real_open(path, *args, **kwargs)
        )

    monkeypatch.setattr(Path, "open", open_path)
    config = module.load_configuration(Path("/dev/stdin"), constraints)
    assert config.expected_database == "campus_ai_school_facts_f2_validation_final"
    assert consumed == [65537]
    bad = json.loads(payload)
    bad["migration"]["db"] = "campus_ai_school_facts_f1"
    payload = json.dumps(bad).encode()
    with pytest.raises(ValueError):
        module.load_configuration(Path("/dev/stdin"), constraints)
    payload = b" " * 65537
    with pytest.raises(ValueError, match="Small explicit JSON"):
        module.load_configuration(Path("/dev/stdin"), constraints)
