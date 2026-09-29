"""Offline original handoff guards; no database or Docker is invoked."""

import importlib.util
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest

ROOT = Path(__file__).resolve().parents[4]

# These deployment guards intentionally exercise retained, instance-specific
# recovery helpers. A normal checkout has no private instance or recovery state.
if not all(
    (ROOT / relative).is_file()
    for relative in (
        "runtime/f4-execution/dispatch_original.py",
        "runtime/f4-execution/prepare_original_migration.py",
        "runtime/f3-execution/prepare_original_migration.py",
    )
):
    pytest.skip(
        "Retained local F4 recovery helpers are required; portable contracts run separately",
        allow_module_level=True,
    )


def load(name):
    spec = importlib.util.spec_from_file_location(
        name, ROOT / "runtime/f4-execution" / (name + ".py")
    )
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def test_migration_runner_is_repeatable_and_checks_idle_governance_and_anchor():
    m = load("prepare_original_migration")
    original = m.h.RUNNER
    first = m.derive_runner()
    m.h.RUNNER = first
    assert m.derive_runner() == first
    assert "usename='campus_ai_governance_writer' OR" in first
    assert "app.assert_school_database_anchor" in first
    assert "f147faa7-783e-47eb-9762-8815a72bf855" in first
    assert "'20260913_27','c1137" in first
    assert "'upgrade','20260913_29'" in first
    compile(first, "migration", "exec")
    assert original != first


def test_receipt_uses_actual_assignment_epoch():
    m = load("dispatch_original")
    result = m.write_result(
        {
            "target_id": str(UUID(int=1)),
            "target_version": 2,
            "assignment_epoch": 8,
            "replayed": False,
        }
    )
    assert result.assignment_epoch == 8


def test_preflight_rejects_stale_future_or_wrong_instance():
    m = load("dispatch_original")
    now = datetime.now(UTC)
    value = {
        "observed_at": now.isoformat(),
        "snapshot": {
            "database": "campus_ai_school_facts_f1",
            "system_identifier": "7682019857215991846",
        },
    }
    m.require_fresh_preflight(value, now=now)
    for changed in (now - timedelta(minutes=16), now + timedelta(seconds=1)):
        with pytest.raises(ValueError):
            m.require_fresh_preflight({**value, "observed_at": changed.isoformat()}, now=now)
    with pytest.raises(ValueError):
        m.require_fresh_preflight(
            {**value, "snapshot": {**value["snapshot"], "database": "other"}}, now=now
        )


def test_phase_intent_is_persisted_before_dispatch_and_not_retried(tmp_path, monkeypatch):
    m = load("dispatch_original")
    m.OUT = tmp_path
    m.ROOT = tmp_path
    m.STATE = tmp_path / "state.json"
    from tests.governance.test_f4_engineering import foundation

    man = foundation()
    state = {"governance_phases": []}
    calls = []

    def fail(**kw):
        calls.append(kw)
        assert m.read(m.STATE)["pending_phase"]["manifest_sha256"]
        raise RuntimeError("dispatch interrupted")

    monkeypatch.setattr(m.rg, "run_phase", fail)
    with pytest.raises(RuntimeError):
        m.execute_phase(
            state, manifest=man, label="0-foundation-scope", image="sha256:" + "a" * 64, timeline={}
        )
    with pytest.raises(ValueError, match="recover"):
        m.execute_phase(
            state, manifest=man, label="0-foundation-scope", image="sha256:" + "a" * 64, timeline={}
        )
    assert len(calls) == 1


def test_completed_phase_reuses_original_receipt_without_dispatch(tmp_path, monkeypatch):
    m = load("dispatch_original")
    m.OUT = m.ROOT = tmp_path
    m.STATE = tmp_path / "state.json"
    from tests.governance.test_f4_engineering import foundation

    man = foundation()
    calls = []

    def run(**kw):
        calls.append(kw)
        folder = kw["folder"]
        folder.mkdir(parents=True)
        m.save(folder / "receipts.json", {"status": "succeeded", "commands": {}})
        return m.read(folder / "receipts.json")

    monkeypatch.setattr(m.rg, "run_phase", run)
    state = {"governance_phases": []}
    for _ in range(2):
        m.execute_phase(
            state, manifest=man, label="0-foundation-scope", image="sha256:" + "a" * 64, timeline={}
        )
    assert len(calls) == 1


def test_receipt_recovery_reuses_original_manifest_and_is_read_only(tmp_path, monkeypatch):
    m = load("dispatch_original")
    m.OUT = m.ROOT = tmp_path
    m.STATE = tmp_path / "state.json"
    from tests.governance.test_f4_engineering import foundation

    man = foundation()
    original = tmp_path / "governance" / "original"
    original.mkdir(parents=True)
    m.save(tmp_path / "fixed-seven-day-timeline.json", {})
    state = {
        "governance_phases": [],
        "pending_phase": {
            "label": "original",
            "folder": "governance/original",
            "manifest": m.rc.canonical_manifest_json(man),
            "manifest_sha256": "digest",
            "image": "sha256:" + "a" * 64,
        },
    }
    calls = []

    def run(**kw):
        calls.append(kw)
        assert kw["receipt_only_from"] == original
        assert kw["manifest"] == man
        return {"status": "succeeded", "commands": {}}

    monkeypatch.setattr(m.rg, "run_phase", run)
    assert m.recover(state)["new_grants"] == 0
    assert "pending_phase" not in state
    assert len(calls) == 1


def test_unchanged_services_verify_surface_and_skip_recreation(monkeypatch):
    m = load("dispatch_original")
    record = {"State": {"Running": True}, "Image": "sha256:" + "a" * 64, "Id": "original"}
    verified = []
    h = SimpleNamespace(
        require_candidate_image=lambda *a: None,
        inspect=lambda n: record,
        validate_runtime_config=lambda *a, **kw: None,
        recreate_command=lambda *a: [],
        verify_manifest=lambda n, p: verified.append(("manifest", n)),
        verify_service_surface=lambda n, **kw: verified.append(("surface", n)),
    )
    monkeypatch.setattr(m.ru, "require_config_delta", lambda *a, **kw: None)
    monkeypatch.setattr(m.ru, "require_preserved_shape", lambda *a: verified.append(("shape",)))
    configs = {m.PRODUCT: {"value": 1}, m.WRITER: {"value": 2}}
    changed, expected = m.select_updates(
        h, proof={"image": record["Image"]}, bases=configs, configs=configs
    )
    assert changed == expected == {}
    assert len(verified) == 6
    changed, _ = m.select_updates(
        h,
        proof={"image": record["Image"]},
        bases=configs,
        configs={m.PRODUCT: {"value": 1}, m.WRITER: {"value": 3}},
    )
    assert set(changed) == {m.WRITER}


def test_scope_success_grant_failure_recovers_without_duplicate_scope(tmp_path, monkeypatch):
    from hashlib import sha256

    m = load("dispatch_original")
    m.OUT = m.ROOT = tmp_path
    m.STATE = tmp_path / "state.json"
    m.save(tmp_path / "proof.json", {"image": "sha256:" + "a" * 64})
    state = {"source_proof": "proof.json", "existing_duties": [], "governance_phases": []}
    matrix = [
        {
            "key": "class",
            "family": "f4",
            "fact_kind": "education_class",
            "action": "maintain",
            "creation_keys": ["f4:class:a"],
            "object_keys": [],
            "field_names": ["display_name"],
        }
    ]
    monkeypatch.setattr(m, "current_matrix", lambda *a: matrix)
    monkeypatch.setattr(m.ru, "retained_helper", lambda: None)
    monkeypatch.setattr(m.ru, "require_quiescent", lambda h: None)
    monkeypatch.setattr(m, "update", lambda *a, **kw: {"status": "verified"})
    attempts = []

    def run(**kw):
        manifest = kw["manifest"]
        folder = kw["folder"]
        folder.mkdir(parents=True)
        (folder / "manifest.json").write_text(
            m.rc.canonical_manifest_json(manifest), encoding="utf-8"
        )
        attempts.append(kw)
        if len(attempts) == 2:
            raise RuntimeError("lost grant response")
        evidence = {
            "status": "succeeded",
            "manifest_sha256": sha256(m.rc.canonical_manifest_json(manifest).encode()).hexdigest(),
            "commands": {
                c.idempotency_key: {
                    "state": "succeeded",
                    "digest": m.rc.f4_governance_command_digest(c),
                    "receipt": {
                        "target_id": str(UUID(int=100 + i + len(attempts) * 10)),
                        "target_version": 1,
                        "assignment_epoch": 2,
                        "replayed": False,
                    },
                }
                for i, c in enumerate(manifest.materialize())
            },
        }
        m.save(folder / "receipts.json", evidence)
        return evidence

    monkeypatch.setattr(m.rg, "run_phase", run)
    with pytest.raises(RuntimeError):
        m.grant(state, stage="foundation")
    fixed = state["active_grant"]["manifest"]
    with pytest.raises(ValueError, match="recover"):
        m.grant(state, stage="foundation")
    assert len(attempts) == 2
    m.recover(state)
    assert m.grant(state, stage="foundation") == {"status": "verified"}
    assert "active_grant" not in state
    assert len(attempts) == 3
    assert attempts[2]["receipt_only_from"] == attempts[1]["folder"]
    assert m.rc.canonical_manifest_json(attempts[0]["manifest"]) == fixed


@pytest.mark.parametrize("raw", [b"", b"0", b"1\n2", b"not-an-epoch"])
def test_current_seed_epoch_rejects_missing_or_ambiguous_read(raw):
    m = load("publish_original")
    h = SimpleNamespace(DOCKER=["docker"], safe_run=lambda *a, **kw: raw)
    with pytest.raises(ValueError):
        m.current_assignment_epoch(h)


def test_current_seed_epoch_is_narrow_guarded_and_does_not_use_receipts():
    m = load("publish_original")
    calls = []

    def run(args, **kw):
        calls.append((args, kw))
        return b"83\n"

    assert m.current_assignment_epoch(SimpleNamespace(DOCKER=["docker"], safe_run=run)) == 83
    sql = calls[0][1]["data"].decode()
    assert "BEGIN READ ONLY" in sql and "app.assert_school_database_anchor" in sql
    assert "20260913_29" in sql and "governance_assignment_epochs" in sql
    assert str(m.ACTOR) in sql and "SELECT epoch" in sql
    assert "UPDATE " not in sql and "INSERT " not in sql


@pytest.mark.parametrize("failure", [False, True])
def test_selected_seed_session_logs_out_on_success_and_exception(failure):
    m = load("publish_original")
    calls = []

    class Client:
        cookies = SimpleNamespace(get=lambda key: "synthetic-token")

        def post(self, path, **kw):
            calls.append(path)
            value = (
                {"contexts": [{"ref": "selected-context"}]}
                if path.endswith("/login")
                else {"selected_context_kind": "staff_assignment"}
            )
            return SimpleNamespace(raise_for_status=lambda: None, json=lambda: value)

    if failure:
        with pytest.raises(RuntimeError):
            with m.selected_session(Client()) as token:
                assert token == "synthetic-token"
                raise RuntimeError("unknown worker outcome")
    else:
        with m.selected_session(Client()) as token:
            assert token == "synthetic-token"
    assert calls[-1].endswith("/logout")


def test_incremental30_runner_requires29_and_never_reexecutes27_chain():
    m = load("prepare_original_migration30")
    code = m.derive_runner()
    assert "'20260913_29','c1137" in code
    assert "'upgrade','20260913_30'" in code
    assert "20260913_27" not in code
    assert "app.assert_school_database_anchor" in code
    assert "usename='campus_ai_governance_writer' OR" in code
    compile(code, "incremental30", "exec")


@pytest.mark.parametrize("revision", [30, 31])
def test_incremental_update_preserves_scopes_and_rejects_downgrade(revision):
    from tests.governance.test_f4_runtime_update import configs

    m = load("dispatch_original")
    _, base = configs()
    contract = f"school-facts-f4-v{revision}"
    result = m.initial_configuration(base, product=True, schema_contract=contract)
    assert (
        result["reader"]["schema_contract"]
        == result["runtime"]["postgres_schema_contract"]
        == contract
    )
    assert result["family"] == base["family"] and result["directory"] == base["directory"]
    with pytest.raises(ValueError):
        m.ru.require_config_delta(result, base, product=True)
    with pytest.raises(ValueError):
        m.initial_configuration(base, product=True, schema_contract="school-facts-f4-v32")


def test_incremental31_runner_requires30_and_never_reexecutes_old_chain():
    m = load("prepare_original_migration31")
    code = m.derive_runner()
    assert "'20260913_30','c1137" in code
    assert "'upgrade','20260913_31'" in code
    assert "20260913_27" not in code and "20260913_29" not in code
    assert "app.assert_school_database_anchor" in code
    assert "usename='campus_ai_governance_writer' OR" in code
    compile(code, "incremental31", "exec")
