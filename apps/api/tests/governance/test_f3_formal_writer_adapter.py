"""Offline safety and integration contracts for the finite real Writer adapter."""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

ROOT = Path(__file__).parents[4]
HERE = ROOT / "scripts/f3_engineering"
sys.path.insert(0, str(HERE))
NOW = datetime(2026, 9, 13, 9, tzinfo=UTC)


def module():
    assert (HERE / "f3_formal_writer_adapter.py").is_file(), "formal F3 factory is missing"
    import f3_formal_writer_adapter

    return f3_formal_writer_adapter


def plan():
    from prepare_runtime import build, load_json

    return build(load_json(HERE / "manifest.json"), {})


def actor():
    from app.platform import contracts as c

    return c._issue_staff_actor_context(
        account_user_id=9,
        school_id=c.SchoolId(UUID(int=1)),
        person_id=c.PersonId(UUID(int=2)),
        assignment_id=c.AssignmentId(UUID(int=3)),
        assignment_version=2,
        authorization_version=5,
    )


class Boundary:
    def __init__(self):
        self.calls = []
        self.result = None
        self.unknown = False

    def preflight(self, **kw):
        from app.platform.school_facts.contracts import PreflightReportV1, canonical_plan_sha256

        self.calls.append(("preflight", kw))
        return PreflightReportV1(
            plan_sha256=canonical_plan_sha256(kw["change_set"]), is_clean=True, issues=()
        )

    def prepare_confirmation(self, **kw):
        from app.platform.school_facts.contracts import ConfirmationReceiptV1, canonical_plan_sha256

        self.calls.append(("prepare", kw))
        return ConfirmationReceiptV1(
            plan_sha256=canonical_plan_sha256(kw["change_set"]),
            credential="x" * 48,
            expires_at=NOW + timedelta(minutes=5),
        )

    def confirm(self, **kw):
        from app.platform.school_facts.ports import SchoolFactApplyResult

        self.calls.append(("confirm", kw))
        self.result = SchoolFactApplyResult(
            uuid4(), kw["change_set"].base_version + 1, len(kw["change_set"].changes), False
        )
        if self.unknown:
            raise TimeoutError("sensitive-transport-data")
        return self.result

    def check_result_by_digest(self, **kw):
        self.calls.append(("result", kw))
        return self.result


class Guard:
    def __init__(self):
        self.denied = False
        self.calls = []

    def require_change_set(self, *args, **kw):
        self.calls.append("require")
        if self.denied:
            raise PermissionError("denied")
        return object()

    def final_guard(self, *args, **kw):
        self.calls.append("final")
        if self.denied:
            raise PermissionError("denied")


class Reader:
    def target(self, identity, kind, source, creation):
        return SimpleNamespace(
            source_key=source,
            expected_version=0 if creation else 1,
            school_fact_version=4,
            content={},
            reference="opaque-reference",
        )

    def external_source(self, identity, kind, reference):
        return {"organization": "existing-school", "location": "existing-campus"}[kind]


@pytest.fixture
def setup(tmp_path):
    m = module()
    boundary, guard = Boundary(), Guard()
    from app.composition.school_fact_product_identity import ProductIdentity

    identity = ProductIdentity(actor(), "a" * 64, "b" * 44)
    port = m.FormalF3WriterPort(
        plan=plan(),
        journal_path=tmp_path / "journal.json",
        school_id=identity.actor.school_id,
        identity_resolver=lambda alias: identity,
        reader=Reader(),
        guard=guard,
        boundary=boundary,
        execute=True,
        clock=lambda: NOW,
        fixed_deadline=NOW + timedelta(days=7),
    )
    return m, port, boundary, guard


def test_factory_exists_and_review_is_offline(tmp_path):
    m = module()
    review = m.create_formal_writer_port(
        private_config_path=tmp_path / "missing-secret.json",
        plan=plan(),
        journal_path=tmp_path / "journal.json",
    )
    assert review["mode"] == "review"
    assert review["logical_stages"] == 9
    assert len(review["physical_batches"]) == 12
    assert not (tmp_path / "journal.json").exists()


def test_fixed_batches_have_no_duplicate_targets_and_correction_follows_revoke():
    p = plan()
    physical = p.get("writer_physical_batches", [])
    assert len(physical) == 12, "logical stages must expand to valid physical Canonical batches"
    from app.platform.school_facts.contracts import CanonicalFactChangeSetV1

    seen = []
    for b in physical:
        cs = CanonicalFactChangeSetV1.model_validate_json(
            json.dumps(
                {"source_sha256": b["changes_sha256"], "base_version": 4, "changes": b["changes"]}
            )
        )
        for c in cs.changes:
            if c.source_key == "f3:guardian:second-student-c" and c.expected_version == 0:
                assert ("f3:guardian:wrong-second-student-a", "revoke") in seen
            seen.append((c.source_key, c.operation))
    assert len({b["idempotency_key"] for b in physical}) == 12
    assert physical[-2]["actor_ref"] == "maintainer"
    assert physical[-1]["actor_ref"] == "verifier"


def test_real_boundary_sequence_and_authoritative_receipt(setup):
    _, port, boundary, guard = setup
    batch = port.physical_batches[0]
    prepared = port.prepare(stage=batch, base_version=4, bindings={})
    assert not any(name == "confirm" for name, _ in boundary.calls)
    result = port.confirm(prepared)
    assert result.status == "succeeded" and result.school_fact_version == 5
    assert len(result.targets) == 9
    assert all("id" not in row and "target_version" not in row for row in result.targets.values())
    calls = dict(boundary.calls)
    assert calls["confirm"]["context"].idempotency_key == batch["idempotency_key"]
    assert calls["prepare"]["provenance"].mapping_config_sha256 == port.plan["manifest_sha256"]
    assert "final" in guard.calls
    port.confirm(prepared)
    assert sum(name == "confirm" for name, _ in boundary.calls) == 1


def test_unknown_and_restart_never_reconfirm(setup):
    m, port, boundary, _ = setup
    batch = port.physical_batches[0]
    prepared = port.prepare(stage=batch, base_version=4, bindings={})
    boundary.unknown = True
    result = port.confirm(prepared)
    assert result.status == "unknown"
    recovered = port.receipt(batch["idempotency_key"])
    assert recovered.status == "succeeded"
    assert boundary.calls[-1][0] == "result"
    assert boundary.calls[-1][1]["context"].idempotency_key == batch["idempotency_key"]
    assert "sensitive-transport-data" not in port.journal_path.read_text(encoding="utf-8")
    assert "a" * 64 not in port.journal_path.read_text(encoding="utf-8")
    restarted = m.FormalF3WriterPort(
        plan=port.plan,
        journal_path=port.journal_path,
        school_id=actor().school_id,
        identity_resolver=port.identity_resolver,
        reader=port.reader,
        guard=port.guard,
        boundary=boundary,
        execute=True,
        clock=lambda: NOW,
        fixed_deadline=port.fixed_deadline,
    )
    assert restarted.receipt(batch["idempotency_key"]).status == "succeeded"
    assert sum(name == "confirm" for name, _ in boundary.calls) == 1


def test_unresolved_journal_blocks_new_prepare(setup):
    m, port, boundary, _ = setup
    batch = port.physical_batches[0]
    prepared = port.prepare(stage=batch, base_version=4, bindings={})
    boundary.confirm = lambda **kwargs: (_ for _ in ()).throw(TimeoutError())
    port.confirm(prepared)
    with pytest.raises(m.F3RecoveryPending):
        port.receipt(batch["idempotency_key"])
    with pytest.raises(m.F3RecoveryPending):
        port.prepare(stage=batch, base_version=4, bindings={})


def test_denial_and_changed_template_fail_before_writer(setup):
    _, port, boundary, guard = setup
    batch = port.physical_batches[0]
    guard.denied = True
    with pytest.raises(PermissionError):
        port.prepare(stage=batch, base_version=4, bindings={})
    assert boundary.calls == []
    guard.denied = False
    tampered = {**batch, "idempotency_key": batch["idempotency_key"] + "new"}
    with pytest.raises(ValueError):
        port.prepare(stage=tampered, base_version=4, bindings={})
    assert boundary.calls == []


def test_changed_identity_or_expired_deadline_cannot_confirm(setup):
    _, port, boundary, _ = setup
    prepared = port.prepare(stage=port.physical_batches[0], base_version=4, bindings={})
    port.clock = lambda: port.fixed_deadline + timedelta(seconds=1)
    with pytest.raises(PermissionError):
        port.confirm(prepared)
    assert not any(name == "confirm" for name, _ in boundary.calls)


def test_all_twelve_batches_use_formal_contracts_and_resume(setup):
    _, port, boundary, _ = setup
    results = {}
    current = {"version": 4, "targets": {}}
    old_confirm = boundary.confirm

    def confirm(**kw):
        result = old_confirm(**kw)
        cs = kw["change_set"]
        current["version"] = result.target_version
        for c in cs.changes:
            current["targets"][c.source_key] = c.expected_version + 1
        results[kw["context"].idempotency_key] = result
        return result

    boundary.confirm = confirm

    def lookup(**kw):
        boundary.calls.append(("result", kw))
        return results.get(kw["context"].idempotency_key)

    boundary.check_result_by_digest = lookup

    def target(identity, kind, source, creation):
        return SimpleNamespace(
            source_key=source,
            expected_version=current["targets"].get(source, 0),
            school_fact_version=current["version"],
            content={},
            reference="opaque",
        )

    port.reader.target = target
    for batch in port.physical_batches:
        prepared = port.prepare(
            stage=batch,
            base_version=current["version"],
            bindings={"organization_ref": "school-ref", "primary_campus_ref": "campus-ref"},
        )
        result = port.confirm(prepared)
        assert result.status == "succeeded"
    assert len(results) == 12
    assert current["version"] == 16
    assert current["targets"]["f3:guardian:wrong-second-student-a"] == 3
    assert current["targets"]["f3:guardian:second-student-c"] == 2
    assert (
        sum(len(kw["change_set"].changes) for name, kw in boundary.calls if name == "confirm") == 39
    )
    for batch in port.physical_batches:
        assert port.receipt(batch["idempotency_key"]).status == "succeeded"
    assert sum(name == "confirm" for name, _ in boundary.calls) == 12


def test_actual_executor_client_serializes_preflight_prepare_confirm_and_result(setup):
    _, port, _, _ = setup
    import httpx

    from app.composition.school_fact_executor_client_settings import (
        SchoolFactExecutorClientSettings,
    )
    from app.composition.school_fact_writer_change_boundary import SchoolFactExecutorChangeBoundary
    from app.composition.school_fact_writer_client import SchoolFactWriterExecutorClient
    from app.platform.school_facts.contracts import CanonicalFactChangeSetV1, canonical_plan_sha256

    calls = []
    batch_id = uuid4()

    def handler(request):
        payload = json.loads(request.content)
        calls.append((request.url.path, payload))
        if request.url.path.endswith("/preflight"):
            cs = CanonicalFactChangeSetV1.model_validate_json(json.dumps(payload["change_set"]))
            data = {"plan_sha256": canonical_plan_sha256(cs), "is_clean": True, "issues": []}
        elif request.url.path.endswith("/prepare"):
            data = {
                "plan_sha256": payload["plan_sha256"],
                "credential": "c" * 48,
                "expires_at": (NOW + timedelta(minutes=5)).isoformat(),
            }
        else:
            result = {
                "batch_id": str(batch_id),
                "target_version": 5,
                "changed_count": 9,
                "replayed": False,
            }
            data = (
                {"found": True, "result": result}
                if request.url.path.endswith("/result")
                else result
            )
        return httpx.Response(200, json=data)

    settings = SchoolFactExecutorClientSettings(
        base_url="http://writer.invalid",
        caller_auth_key="k" * 32,
        context_transport_hmac_key="h" * 32,
    )
    with SchoolFactWriterExecutorClient(
        settings=settings, transport=httpx.MockTransport(handler)
    ) as client:
        port.boundary = SchoolFactExecutorChangeBoundary(client)
        prepared = port.prepare(stage=port.physical_batches[0], base_version=4, bindings={})
        assert port.confirm(prepared).school_fact_version == 5
        assert port.receipt(port.physical_batches[0]["idempotency_key"]).school_fact_version == 5
    assert [path.rsplit("/", 1)[1] for path, _ in calls] == [
        "preflight",
        "prepare",
        "confirm",
        "result",
    ]
    assert calls[1][1]["provenance"]["mapping_config_sha256"] == port.plan["manifest_sha256"]
    assert all("context_token" in payload for _, payload in calls)


def test_guard_revocation_and_identity_change_prevent_confirm(setup):
    _, port, boundary, guard = setup
    prepared = port.prepare(stage=port.physical_batches[0], base_version=4, bindings={})
    guard.denied = True
    with pytest.raises(PermissionError):
        port.confirm(prepared)
    guard.denied = False
    from app.composition.school_fact_product_identity import ProductIdentity

    port.identity_resolver = lambda _: ProductIdentity(actor(), "c" * 64, "d" * 44)
    with pytest.raises(PermissionError):
        port.confirm(prepared)
    assert not any(name == "confirm" for name, _ in boundary.calls)


def test_reader_version_mismatch_prevents_preflight(setup):
    _, port, boundary, _ = setup
    with pytest.raises(ValueError):
        port.prepare(stage=port.physical_batches[0], base_version=999, bindings={})
    assert boundary.calls == []


def test_real_factory_constructs_full_composition_without_connecting_or_loading_model_key(
    tmp_path, monkeypatch
):
    m = module()
    from sqlalchemy import create_engine, event

    from app.composition import school_fact_product_services as services
    from app.db import connection
    from tests.assistant.test_f3_product_configuration import f3_document

    data = f3_document(tmp_path)
    path = tmp_path / "synthetic-config.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    from f3_runtime_handoff import config_reference

    overlay_path = tmp_path / "reference-overlay.json"
    overlay_path.write_text(json.dumps(config_reference(path, {})), encoding="utf-8")
    path = overlay_path
    engines = []

    def no_network_engine(*args, **kwargs):
        engine = create_engine("sqlite://")

        @event.listens_for(engine, "connect")
        def forbid_connect(*args):
            raise AssertionError("factory must not connect in offline test")

        engines.append(engine)
        return engine

    monkeypatch.setattr(connection, "build_engine", no_network_engine)
    monkeypatch.setattr(connection, "verify_runtime_context_signing_key", lambda *args: None)
    monkeypatch.setattr(services, "build_school_fact_reader_engine", no_network_engine)
    monkeypatch.setattr(services, "verify_school_fact_reader_database", lambda *args, **kw: None)
    monkeypatch.setattr(m, "ROOT", tmp_path)
    port = m.create_formal_writer_port(
        private_config_path=path,
        plan=plan(),
        journal_path=tmp_path / "journal.json",
        execute=True,
        fixed_deadline=datetime.now(UTC) + timedelta(hours=1),
    )
    from app.composition.school_fact_family_authority import StaffFamilyFactChangeSetGuard
    from app.composition.school_fact_writer_change_boundary import SchoolFactExecutorChangeBoundary

    assert isinstance(port.guard, StaffFamilyFactChangeSetGuard)
    assert isinstance(port.boundary, SchoolFactExecutorChangeBoundary)
    assert isinstance(port.reader, m.FormalFamilyReader)
    assert isinstance(port.identity_resolver, m.CurrentEngineeringStaff)
    assert len(engines) == 2
    assert not (tmp_path / "journal.json").exists()
    port.close()


def test_existing_session_cannot_substitute_maintainer_for_verifier():
    m = module()
    from app.composition.school_fact_product_identity import ProductIdentity

    identity = ProductIdentity(actor(), "a" * 64, "b" * 44)
    service = SimpleNamespace(resolve_request=lambda _: identity)
    store = SimpleNamespace(
        read=lambda _: SimpleNamespace(
            external_subject="f1-maintainer",
            user_id=actor().account_user_id,
            session_nonce_hash="a" * 64,
        )
    )
    resolver = m.CurrentEngineeringStaff(
        service, store, {"maintainer": "existing", "verifier": "existing"}
    )
    assert resolver("maintainer") == identity
    with pytest.raises(PermissionError):
        resolver("verifier")


def test_engineering_metadata_preserves_model_policy_without_key_or_ledger_access(
    tmp_path, monkeypatch
):
    from pydantic import ValidationError

    from app.composition import school_fact_current_model_budget as current_budget
    from app.composition import school_fact_model_budget as legacy_budget
    from app.composition import school_fact_product_settings as settings
    from tests.assistant.test_f3_product_configuration import f3_document

    m = module()
    data = f3_document(tmp_path)
    data["model"]["state_directory"] = str(tmp_path / "unmounted-model-state")
    data["model"]["api_key_file"] = str(tmp_path / "unmounted-key")
    path = tmp_path / "worker-metadata.json"
    path.write_text(json.dumps(data), encoding="utf-8")

    def forbidden(*args, **kwargs):
        pytest.fail("F3 execution metadata must never load a key or reserve model money")

    monkeypatch.setattr(settings, "load_product_model_api_key", forbidden)
    for mod in (legacy_budget, current_budget):
        for value in vars(mod).values():
            if isinstance(value, type) and "reserve" in vars(value):
                monkeypatch.setattr(value, "reserve", forbidden)
    parsed = m.load_execution_configuration(path)
    assert parsed.model.stage_budget.stage_id == "school-facts-f3"
    assert not (tmp_path / "unmounted-model-state").exists()
    assert not (tmp_path / "unmounted-key").exists()
    data["model"]["stage_budget"]["cap_microyuan"] = 3000001
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValidationError):
        m.load_execution_configuration(path)
