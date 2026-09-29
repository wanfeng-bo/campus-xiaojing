"""Offline failure-path checks; no Docker or original database calls."""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from tests.governance.test_f4_runtime_update import helper


def test_quiescence_is_pinned_to_original_schema29_and_no_governance_sessions():
    captured = []

    def run(args, **kwargs):
        captured.append((args, kwargs["data"].decode()))
        return b"c1137be2-0734-4b69-8937-37dbd741f141\nready\n"

    helper().require_quiescent(SimpleNamespace(DOCKER=["mock-docker"], safe_run=run))
    args, sql = captured[0]
    assert "campus_ai_school_facts_f1" in args
    assert "IN ('20260913_29','20260913_30')" in sql and "20260913_27" not in sql
    assert "app.assert_school_database_anchor" in sql
    assert "7682019857215991846" in sql
    assert "usename='campus_ai_governance_writer')=0" in sql


@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_failed_candidate_is_stopped_or_explicitly_unknown(monkeypatch, tmp_path, cleanup_fails):
    module = helper()
    image = "sha256:" + "a" * 64
    old_image = "sha256:" + "b" * 64
    old = {"Id": "old-id", "Image": old_image, "State": {"Running": True}}
    containers = {"writer": old}
    events = []

    def inspect(name):
        return copy.deepcopy(containers[name])

    def run(args, **kwargs):
        events.append(tuple(args))
        if args[0] == "ps":
            return b""
        if args[0] == "stop":
            target = args[-1]
            if target == "new-id" and cleanup_fails:
                raise RuntimeError("synthetic unavailable stop")
            for value in containers.values():
                if target in {value["Id"], "writer"}:
                    value["State"]["Running"] = False
        elif args[0] == "rename":
            containers[args[2]] = containers.pop(args[1])
        elif args[0] == "create":
            containers["writer"] = {"Id": "new-id", "Image": image, "State": {"Running": False}}
            return b"new-id"
        elif args[0] == "start":
            containers[args[1]]["State"]["Running"] = True
        return b""

    def failed_surface(*args, **kwargs):
        raise ValueError("synthetic invalid candidate surface")

    fake = SimpleNamespace(
        WRITER="writer",
        PRODUCT="product",
        DOCKER=[],
        inspect=inspect,
        safe_run=run,
        read_json=lambda path: {},
        require_candidate_image=lambda *a: None,
        validate_runtime_config=lambda *a, **kw: None,
        recreate_command=lambda *a: ["create"],
        atomic_private_config=lambda *a: None,
        verify_manifest=lambda *a: None,
        verify_service_surface=failed_surface,
    )
    monkeypatch.setattr(module, "retained_helper", lambda: fake)
    monkeypatch.setattr(module, "require_config_delta", lambda *a, **kw: None)
    monkeypatch.setattr(module, "require_quiescent", lambda *a: None)
    path = tmp_path / "review-result.json"
    with pytest.raises(RuntimeError):
        module.update_services(
            proof={"image": image, "source_sha256": "fixed"},
            configurations={"writer": {"reader": {"schema_contract": "school-facts-f4-v29"}}},
            evidence_path=path,
            expected_images={"writer": old_image},
            execute=True,
        )
    report = json.loads(path.read_text(encoding="utf-8"))
    assert report["status"] == ("closure_unknown" if cleanup_fails else "stopped")
    assert containers["writer"]["State"]["Running"] is cleanup_fails
    assert ("stop", "--timeout", "30", "new-id") in events
    assert not any(event[0] == "start" and "before-f4" in event[-1] for event in events)


@pytest.mark.parametrize("failure", ["open", "worker", "close"])
def test_governance_window_always_closes_logical_approval_and_attempts_role_close(
    tmp_path, failure
):
    from scripts.f3_engineering.f3_runtime_handoff import run_bounded_window

    now = datetime.now(UTC)
    events = []

    class Control:
        def require_closed(self):
            events.append("verified_closed")

        def open(self):
            events.append("open")
            if failure == "open":
                raise RuntimeError("open failure")

        def close(self):
            events.append("close")
            if failure == "close":
                raise RuntimeError("close failure")

    def worker():
        if failure == "worker":
            raise RuntimeError("worker failure")

    path = tmp_path / "approval.json"
    approval = {
        "valid_from": (now - timedelta(seconds=1)).isoformat(),
        "valid_to": (now + timedelta(minutes=5)).isoformat(),
        "fixed_deadline": (now + timedelta(days=6)).isoformat(),
        "authorization_reference": "explicit",
        "issuer_reference": "independent-issuer",
        "actor_reference": "operator",
    }
    with pytest.raises(RuntimeError):
        run_bounded_window(
            approval_path=path,
            approval=approval,
            control=Control(),
            worker=worker,
            execute=True,
            clock=lambda: now,
        )
    assert json.loads(path.read_text(encoding="utf-8"))["state"] == "closed"
    assert "close" in events
    assert (events[-1] == "verified_closed") is (failure != "close")


def test_receipt_recovery_preserves_closed_original_and_never_dispatches_write_worker(
    monkeypatch, tmp_path
):
    import hashlib

    from tests.governance.test_f4_engineering import foundation
    from tests.governance.test_f4_runtime_governance import module as governance_module

    module = governance_module()
    monkeypatch.setattr(module, "ROOT", tmp_path)
    now = datetime.now(UTC)
    epoch = now - timedelta(days=7) + timedelta(seconds=90)
    original = foundation()
    manifest = type(original).model_validate_json(
        original.model_copy(
            update={
                "actor_reference": "f2-original-governance-operator",
                "grant_epoch": epoch,
                "grant_valid_to": epoch + timedelta(days=7),
                "valid_from": epoch,
                "valid_to": epoch + timedelta(minutes=10),
                "commands": tuple(
                    c.model_copy(update={"effective_from": epoch}) for c in original.commands
                ),
            }
        ).model_dump_json()
    )
    old = tmp_path / "original"
    old.mkdir()
    from scripts.f4_engineering.runtime_configuration import canonical_manifest_json

    raw = canonical_manifest_json(manifest)
    digest = hashlib.sha256(raw.encode()).hexdigest()
    (old / "manifest.json").write_text(raw, encoding="utf-8")
    module._save(
        old / "execution-approval.json",
        {"state": "closed", "manifest_sha256": digest, "authorization_reference": "fixed-original"},
    )
    module._save(
        old / "receipts.json", {"manifest_sha256": digest, "commands": {}, "status": "unknown"}
    )
    originals = {p.name: p.read_bytes() for p in old.iterdir()}
    base = tmp_path / "private.json"
    base.write_text(
        json.dumps(
            {
                "actor_reference": manifest.actor_reference,
                "database_name": "campus_ai_school_facts_f1",
                "expected_school_id": str(manifest.school_id),
                "expected_anchor_id": str(manifest.school_id),
                "expected_system_identifier": "123",
                "credential": "never-copy-this-test-secret",
            }
        )
    )
    events = []

    class Control:
        def __init__(self, **kw):
            pass

        def require_closed(self):
            events.append("closed")

        def open(self):
            events.append("open")

        def close(self):
            events.append("close")

    monkeypatch.setattr(module, "OriginalSchoolGovernanceControl", Control)

    def run(args, **kw):
        if "run" in args:
            assert "--receipt-worker" in args and "--worker" not in args
            assert 0 < kw["timeout"] <= 30
            events.append("receipt_dispatch")
            return SimpleNamespace(returncode=0)
        assert "inspect" in args
        return SimpleNamespace(returncode=1)

    monkeypatch.setattr(module.subprocess, "run", run)
    timeline = {
        "school_id": str(manifest.school_id),
        "authorization_reference": "fixed-original",
        "first_execution_t0": epoch.isoformat(),
        "fixed_deadline": manifest.grant_valid_to.isoformat(),
    }
    recovered = tmp_path / "recovered"
    result = module.run_phase(
        docker=tmp_path / "unused-docker",
        image="sha256:" + "a" * 64,
        folder=recovered,
        manifest=manifest,
        governance_base=base,
        issuer_reference="independent-issuer",
        authorization_reference="fixed-original",
        timeline=timeline,
        receipt_only_from=old,
    )
    assert result["status"] == "unknown"
    assert {p.name: p.read_bytes() for p in old.iterdir()} == originals
    assert events == ["closed", "open", "receipt_dispatch", "close", "closed"]
    approval = json.loads((recovered / "execution-approval.json").read_text(encoding="utf-8"))
    assert approval["receipt_only"] is True and approval["state"] == "closed"
    assert approval["fixed_deadline"] == manifest.grant_valid_to.isoformat()
    assert all(
        "never-copy-this-test-secret" not in p.read_text(encoding="utf-8")
        for p in recovered.iterdir()
    )
    with pytest.raises(ValueError, match="original fixed seven-day"):
        module.run_phase(
            docker=tmp_path / "unused-docker",
            image="sha256:" + "a" * 64,
            folder=tmp_path / "rolled",
            manifest=manifest,
            governance_base=base,
            issuer_reference="independent-issuer",
            authorization_reference="fixed-original",
            timeline={**timeline, "first_execution_t0": now.isoformat()},
        )
    assert not (tmp_path / "rolled").exists()
