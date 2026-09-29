"""Offline tests for local F3 overlay and bounded Governance window mechanics."""

import importlib
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

HERE = Path(__file__).parents[4] / "scripts/f3_engineering"
sys.path.insert(0, str(HERE))
NOW = datetime(2026, 9, 13, 9, tzinfo=UTC)


def helper():
    assert (HERE / "f3_runtime_handoff.py").is_file(), "finite handoff helper missing"
    return importlib.import_module("f3_runtime_handoff")


def test_published_assignment_lookup_is_exact_reader_coordinate():
    from types import SimpleNamespace
    from uuid import uuid4

    m = helper()
    school, assignment, batch = uuid4(), uuid4(), uuid4()
    actor = SimpleNamespace(school_id=school, assignment_id=assignment, assignment_version=2)
    observed = []

    class Session:
        rows = [(assignment, 2)]

        def execute(self, sql, parameters):
            observed.append((str(sql), parameters))
            return SimpleNamespace(all=lambda: self.rows)

    session = Session()
    receipt = {
        "fact_kind": "assignment",
        "source_key": "approved-source",
        "writer_batch_id": str(batch),
    }
    assert m.verified_published_assignment(
        session, actor=actor, source="approved-source", receipt=receipt
    ) == str(assignment)
    query, values = observed[0]
    assert values == {"school": school, "source": "approved-source", "batch": batch}
    assert "staff_assignments" not in query and "target_id,target_version" in query
    for rows in ([], [(uuid4(), 2)], [(assignment, 1)], [(assignment, 2), (assignment, 2)]):
        session.rows = rows
        with pytest.raises(ValueError):
            m.verified_published_assignment(
                session, actor=actor, source="approved-source", receipt=receipt
            )


def test_overlay_is_secret_free_and_loaded_only_in_memory(tmp_path):
    m = helper()
    base = tmp_path / "private.json"
    base.write_text(
        json.dumps(
            {
                "runtime": {"password": "private-value", "schema": "old"},
                "model": {"key_file": "original"},
            }
        ),
        encoding="utf-8",
    )
    overlay = m.config_reference(base, {"runtime": {"schema": "new"}})
    assert "private-value" not in json.dumps(overlay)
    output = tmp_path / "overlay.json"
    output.write_text(json.dumps(overlay), encoding="utf-8")
    loaded = m.load_config_reference(output)
    assert loaded["runtime"] == {"password": "private-value", "schema": "new"}
    assert "old" in base.read_text(encoding="utf-8")


def test_timeline_is_persisted_once_and_later_windows_shrink(tmp_path):
    m = helper()
    path = tmp_path / "timeline.json"
    first = m.start_timeline(
        path, school_id="school", authorization_reference="user-approved-F3", now=NOW
    )
    later = m.start_timeline(
        path,
        school_id="school",
        authorization_reference="user-approved-F3",
        now=NOW + timedelta(days=2),
    )
    assert first == later
    assert datetime.fromisoformat(later["fixed_deadline"]) == NOW + timedelta(days=7)
    with pytest.raises(ValueError):
        m.start_timeline(
            path, school_id="different", authorization_reference="user-approved-F3", now=NOW
        )


def test_window_always_closes_approval_and_role_after_worker_failure(tmp_path):
    m = helper()
    approval = tmp_path / "approval.json"
    calls = []

    class Control:
        def require_closed(self):
            calls.append("require_closed")

        def open(self):
            calls.append("login")

        def close(self):
            calls.append("nologin")

    def worker():
        assert json.loads(approval.read_text(encoding="utf-8"))["state"] == "open"
        raise RuntimeError("safe-worker-failed")

    record = {
        "manifest_sha256": "a" * 64,
        "state": "issued",
        "issuer_reference": "current-user",
        "actor_reference": "independent-operator",
        "authorization_reference": "current-F3-approval",
        "valid_from": NOW.isoformat(),
        "valid_to": (NOW + timedelta(minutes=15)).isoformat(),
        "fixed_deadline": (NOW + timedelta(days=7)).isoformat(),
    }
    with pytest.raises(RuntimeError):
        m.run_bounded_window(
            approval_path=approval,
            approval=record,
            control=Control(),
            worker=worker,
            execute=True,
            clock=lambda: NOW,
        )
    assert calls == ["require_closed", "login", "nologin", "require_closed"]
    assert json.loads(approval.read_text(encoding="utf-8"))["state"] == "closed"


def test_default_window_review_never_touches_controls(tmp_path):
    m = helper()
    result = m.run_bounded_window(
        approval_path=tmp_path / "approval.json", approval={}, control=None, worker=None
    )
    assert result == {"mode": "review", "mutations": 0}
    assert not (tmp_path / "approval.json").exists()


def test_cross_stage_grants_use_original_duty_and_new_scope_receipts():
    from types import SimpleNamespace
    from uuid import UUID, uuid4

    from app.platform.governance.contracts import (
        CreateDutyCommand,
        CreateScopeCommand,
        GovernanceActorContext,
        GovernanceWriteResult,
    )
    from app.platform.school_facts.f3_policy import F3FactScope

    m = helper()
    school = UUID(int=1)
    actor = GovernanceActorContext(
        school, "f3_engineering", "operator", None, None, uuid4(), "approved"
    )
    scope = F3FactScope(school, "student", (), ("f3:student:a",), ("person_source_key", "status"))
    duty = CreateDutyCommand(
        school, "f3_student_maintainer", "F3 student", None, NOW, actor, "f3:original-duty"
    )
    newer = NOW + timedelta(days=2)
    scope_command = CreateScopeCommand(
        school,
        "new-scope",
        "school_facts.f3",
        "exact_family_fact_set",
        1,
        scope.document,
        scope.document_sha256,
        newer,
        actor,
        "f3:new-scope",
    )
    duty_receipt = GovernanceWriteResult(UUID(int=10), 1, None, True)
    scope_receipt = GovernanceWriteResult(UUID(int=11), 1, None, False)
    calls = []

    def read(command):
        calls.append(command.idempotency_key)
        return duty_receipt if command is duty else scope_receipt

    grant = m.build_grants_from_receipts(
        school_id=school,
        actor_reference="operator",
        bindings=(
            SimpleNamespace(
                key="student-grant", assignment_id=UUID(int=3), duty_key="duty", scope_key="scope"
            ),
        ),
        metadata_commands={"duty": duty, "scope": scope_command},
        read_receipt=read,
        stage_start=newer,
        fixed_deadline=NOW + timedelta(days=7),
    )
    assert calls == ["f3:original-duty", "f3:new-scope"]
    command = grant.materialize()[0]
    assert command.duty_id == duty_receipt.target_id
    assert command.scope_id == scope_receipt.target_id
    assert command.valid_from == newer
    assert command.planned_valid_to == NOW + timedelta(days=7)
    assert grant.valid_to == newer + timedelta(minutes=15)


def test_original_school_control_uses_fixed_container_and_only_own_connection_cleanup(monkeypatch):
    from types import SimpleNamespace
    from uuid import UUID

    m = helper()
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs["input"]))
        return SimpleNamespace(returncode=0, stdout='{"login":false,"sessions":0}\n')

    monkeypatch.setattr(m.subprocess, "run", run)
    control = m.OriginalSchoolGovernanceControl(
        docker=Path("Docker.exe"),
        anchor_id=UUID(int=1),
        school_id=UUID(int=2),
        system_identifier="123",
        application_name="f3-window-aabbcc",
    )
    control.close()
    argv, sql = calls[0]
    assert "campus-school-facts-f1-postgres-1" in argv
    assert "campus_ai_school_facts_f1" in argv
    assert "pydanticai-stage2-controls-postgres" not in argv
    assert "app.assert_school_database_anchor" in sql
    assert "application_name=:'appname'" in sql
    assert "pg_terminate_backend(pid,5000)" in sql
    assert "ALTER ROLE campus_ai_governance_writer NOLOGIN" in sql


def test_creation_intents_are_chinese_approved_samples_and_disambiguate_students():
    from prepare_runtime import MANIFEST, command_templates

    m = helper()
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    labels = m.creation_intents()
    allowed = command_templates(manifest)["creation_source_keys"]
    assert {kind: set(values) for kind, values in labels.items()} == {
        kind: set(keys) for kind, keys in allowed.items()
    }
    assert all("f3:" not in label for values in labels.values() for label in values.values())
    assert labels["student"]["f3:student:a"] == "新增学生档案：林同学（合成学生甲）"
    assert labels["student"]["f3:student:c"] == "新增学生档案：林同学（合成学生丙）"
    assert "纠正原关系" in labels["guardian_relationship"]["f3:guardian:second-student-c"]
    assert "照护关系" in labels["guardian_relationship"]["f3:guardian:caregiver-student-b"]


def test_original_foundation_review_defers_clock_and_materializes_exact_first_window():
    from prepare_runtime import MANIFEST

    m = helper()
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    identity = {
        "status": "current_staff_resolved",
        "school_id": "00000000-0000-0000-0000-000000000001",
        "maintainer_account_user_id": 1,
        "maintainer_person_id": "00000000-0000-0000-0000-000000000002",
        "maintainer_assignment_id": "00000000-0000-0000-0000-000000000003",
        "maintainer_assignment_version": 1,
        "authorization_version": 1,
    }
    arguments = dict(
        manifest=manifest,
        maintainer=identity,
        target_schema_contract="school-facts-f3-v27",
        authorization_reference="current-thread-approved-F3",
        issuer_reference="current-user-engineering-approval",
    )
    review = m.prepare_original_foundation(**arguments)
    assert review["mode"] == "review" and review["clock_started"] is False
    assert review["mutations"] == 0 and review["metadata_manifest"] is None
    assert len(review["writer_physical_batches"]) == 12
    assert len(review["foundation_grant_bindings"]) == 3
    prepared = m.prepare_original_foundation(**arguments, first_execution_at=NOW)
    assert prepared["clock_started"] is False and prepared["mutations"] == 0
    assert prepared["approval_record"]["state"] == "prepared_not_issued"
    assert prepared["approval_record"]["fixed_deadline"] == (NOW + timedelta(days=7)).isoformat()
    from app.composition.school_fact_f3_engineering_cli import EngineeringManifest

    actual = EngineeringManifest.model_validate_json(prepared["metadata_manifest_json"])
    assert len(actual.commands) == 20
    driver_rows = m.original_stage_matrix(
        manifest, "foundation", {}, {"maintainer": identity["maintainer_assignment_id"]}
    )
    driver_manifest = m._fixed_metadata(
        manifest, "foundation", driver_rows, identity["school_id"], NOW, NOW + timedelta(days=7)
    )
    assert m._manifest_document(driver_manifest) == prepared["metadata_manifest"]
    assert [b["scope_key"] for b in prepared["foundation_grant_bindings"]] == [
        r["key"] + ":scope" for r in driver_rows
    ]
    assert len(actual.approved_duty_codes) == 17
    assert {c.fact_kind for c in actual.commands if c.command == "scope"} == {
        "person",
        "login_mapping",
        "assignment",
    }
    assert len(prepared["review"]["commands"]) == 20
    assert (
        prepared["governance_private_overrides"]["reader"]["schema_contract"]
        == "school-facts-f3-v27"
    )


def test_original_driver_review_is_pure_and_fixes_full_sequence(tmp_path):
    from prepare_runtime import MANIFEST, build

    m = helper()
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    result = m.run_original_f3(
        manifest=manifest,
        plan=build(manifest, {}),
        maintainer={"school_id": "00000000-0000-0000-0000-000000000001"},
        directory_references={},
        authorization_reference="current-approved-F3",
        issuer_reference="independent-issuer",
        state_path=tmp_path / "execution.json",
    )
    assert result["mode"] == "review" and result["mutations"] == 0
    assert result["clock_started"] is False
    assert len(result["physical_batches"]) == 12
    assert result["sequence"][-1] == "writer:11"
    assert result["sequence"].index("writer:9") < result["sequence"].index("writer:10")
    assert not list(tmp_path.iterdir())


def test_fixed_stage_scope_uses_only_receipt_sources_and_preserves_correction_creation():
    from prepare_runtime import MANIFEST, physical_writer_batches

    m = helper()
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    sources = {}
    for batch in physical_writer_batches(manifest)[:8]:
        for change in batch["changes"]:
            sources[change["source_key"]] = {"fact_kind": change["fact_kind"]}
    assignments = {
        "maintainer": "00000000-0000-0000-0000-000000000001",
        "verifier": "00000000-0000-0000-0000-000000000002",
        "teacher-parent": "00000000-0000-0000-0000-000000000003",
    }
    rows = m.original_stage_matrix(manifest, "request_access", sources, assignments)
    assert len(rows) == 25
    request = next(r for r in rows if r["grantee"] == "maintainer" and r["action"] == "request")
    assert request["creation_keys"] == ["f3:guardian:second-student-c"]
    assert all(set(r["object_keys"]) <= set(sources) for r in rows)
    assert "f3:guardian:second-student-c" not in request["object_keys"]
    assert (
        m.product_overrides({}, approval_reference="approved")["reader"]["schema_contract"]
        == "school-facts-f3-v27"
    )
    assert m.writer_overrides({})["writer"]["schema_contract"] == "school-facts-f3-v27"


def test_complete_original_driver_recovers_original_governance_and_twelve_writer_keys(tmp_path):
    from uuid import NAMESPACE_URL, UUID, uuid5

    from f3_writer_batch_executor import StageReceipt
    from prepare_runtime import MANIFEST, build

    from app.composition.school_fact_family_settings import FamilyConfiguration
    from app.platform.governance.contracts import GovernanceWriteResult

    m = helper()
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    plan = build(manifest, {})
    identity = {
        "status": "current_staff_resolved",
        "school_id": str(UUID(int=1)),
        "maintainer_account_user_id": 1,
        "maintainer_person_id": str(UUID(int=2)),
        "maintainer_assignment_id": str(UUID(int=3)),
        "maintainer_assignment_version": 1,
        "authorization_version": 1,
    }

    class Runtime:
        def __init__(self):
            self.governance = {}
            self.writer = {}
            self.calls = []
            self.families = []
            self.failed_once = False

        def preflight(self, *args):
            pass

        def ensure_accounts(self, accounts):
            assert len(accounts) == 6

        def resolve_assignments(self, published):
            assert published["f3:assignment:verifier"]["fact_kind"] == "assignment"
            return {"verifier": str(UUID(int=4)), "teacher-parent": str(UUID(int=5))}

        def governance_phase(self, *, step, manifest, lookup_only, **kwargs):
            self.calls.append((step, lookup_only))
            if lookup_only:
                assert step in self.governance
                return self.governance[step]
            assert step not in self.governance
            commands = manifest.materialize()
            if step == "foundation:metadata":
                assert len(commands) == 20
                assert "f3_guardian_relationship_reader" in manifest.approved_duty_codes
            self.governance[step] = {
                c.idempotency_key: GovernanceWriteResult(
                    uuid5(NAMESPACE_URL, c.idempotency_key), 1, None, False
                )
                for c in commands
            }
            if not self.failed_once:
                self.failed_once = True
                raise TimeoutError("committed but response unavailable")
            return self.governance[step]

        def writer_batch(self, plan, stage, deadline, refs):
            key = stage["idempotency_key"]
            assert deadline == NOW + timedelta(days=7)
            if key not in self.writer:
                assert len(self.writer) == stage["index"]
                self.writer[key] = StageReceipt(
                    key,
                    "succeeded",
                    8 + len(self.writer),
                    {
                        c["source_key"]: {
                            "fact_kind": c["fact_kind"],
                            "source_key": c["source_key"],
                            "writer_batch_id": str(uuid5(NAMESPACE_URL, key)),
                        }
                        for c in stage["changes"]
                    },
                )
            return self.writer[key]

        def activate_family(self, family):
            FamilyConfiguration.model_validate_json(json.dumps(family))
            self.families.append(family)

        def finalize_family(self):
            assert len(self.writer) == 12

    runtime = Runtime()
    arguments = dict(
        manifest=manifest,
        plan=plan,
        maintainer=identity,
        directory_references={},
        authorization_reference="current-approved-F3",
        issuer_reference="independent-issuer",
        state_path=tmp_path / "execution.json",
        runtime=runtime,
        execute=True,
        clock=lambda: NOW,
    )
    with pytest.raises(TimeoutError):
        m.run_original_f3(**arguments)
    result = m.run_original_f3(**arguments)
    assert result["status"] == "succeeded"
    assert runtime.calls[:2] == [("foundation:metadata", False), ("foundation:metadata", True)]
    assert len(runtime.writer) == 12 and len(runtime.governance) == 8
    assert len(runtime.families) == 4
    final = runtime.families[-1]
    assert len(final["scope_bindings"]) == 25
    assert "f3:guardian:second-student-c" in final["source_catalog"]["guardian_relationship"]
    calls_before = len(runtime.calls)
    m.run_original_f3(**arguments)
    assert all(lookup for _, lookup in runtime.calls[calls_before:])
    assert len(runtime.writer) == 12 and len(runtime.governance) == 8


@pytest.mark.parametrize("occupied,foreign", [(True, False), (False, True), (False, False)])
def test_fixed_worker_cleanup_requires_this_creation_token_and_image(
    tmp_path, monkeypatch, occupied, foreign
):
    import subprocess
    from types import SimpleNamespace

    import prepare_runtime

    m = helper()
    monkeypatch.setattr(prepare_runtime, "ROOT", tmp_path)
    calls, owner = [], []
    image = "sha256:" + "a" * 64

    def run(argv, **kwargs):
        calls.append(argv)
        if "ps" in argv:
            return SimpleNamespace(returncode=0, stdout=b"older" if occupied else b"")
        if "run" in argv:
            label = argv[argv.index("--label") + 1]
            owner.append(label.split("=", 1)[1])
            assert not any("model-key" in part or "migration-config" in part for part in argv)
            raise subprocess.TimeoutExpired("fixed-worker", 1)
        if "inspect" in argv:
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps(
                    [
                        {
                            "Id": "owned-id",
                            "Image": image,
                            "Config": {
                                "Labels": {
                                    "school-facts.f3.worker-owner": "foreign"
                                    if foreign
                                    else owner[0]
                                }
                            },
                        }
                    ]
                ).encode(),
            )
        assert argv[-3:] == ["rm", "--force", "owned-id"]
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(m.subprocess, "run", run)
    runtime = m.OriginalF3Runtime(
        docker=Path("docker.exe"),
        image=image,
        source_proof={
            "image": image,
            "files": {"app.py": "b" * 64},
            "image_files": {"app.py": "b" * 64},
            "source_sha256": __import__("hashlib")
            .sha256(json.dumps({"app.py": "b" * 64}, sort_keys=True).encode())
            .hexdigest(),
        },
        product_base=tmp_path / "base-product.json",
        writer_base=tmp_path / "base-writer.json",
        governance_base=tmp_path / "base-governance.json",
        active_product=tmp_path / "product.json",
        active_writer=tmp_path / "writer.json",
        output=tmp_path / "driver",
        authorization_reference="approved-F3",
    )
    with pytest.raises((RuntimeError, subprocess.TimeoutExpired)):
        runtime._job("accounts", {}, name="identity-accounts")
    assert any("rm" in command for command in calls) is (not occupied and not foreign)
    if occupied:
        assert len(calls) == 1
