"""F6 release preparation never changes a running service."""

import importlib
from pathlib import Path

import pytest


def release():
    return importlib.import_module("scripts.f6_engineering.release")


def test_f6_additions_are_bounded_to_owner_and_exclude_private_or_build_output():
    m = release()
    assert m.allowed_addition("apps/api/app/composition/school_fact_residence_query.py")
    assert m.allowed_addition("apps/api/alembic/versions/20260922_44_f6_family_corrections.py")
    assert not m.allowed_addition("apps/api/app/composition/unrelated.py")
    assert not m.allowed_addition("apps/web/.env.local")
    assert not m.allowed_addition("apps/api/build/private.json")


def test_f6_authorized_multi_child_adapter_is_included_without_opening_other_additions():
    m = release()
    assert m.allowed_addition("apps/api/app/composition/school_fact_family_children_gateway.py")
    assert not m.allowed_addition("apps/api/app/composition/unreviewed_family_gateway.py")


def test_snapshot_rejects_wrong_schema_and_mutated_source(tmp_path, monkeypatch):
    m = release()
    monkeypatch.setattr(m, "OUT", tmp_path)
    monkeypatch.setattr(m, "sources", lambda: {"apps/api/app/example.py": b"safe"})
    proof = m.source()
    assert proof["schema_contract"] == "school-facts-f6-v45"
    assert proof["services_updated"] is False
    m.require_snapshot(proof)
    with pytest.raises(ValueError):
        m.require_snapshot({**proof, "schema_contract": "school-facts-f5-v41"})
    (Path(proof["snapshot_path"]) / "apps/api/app/example.py").write_bytes(b"changed")
    with pytest.raises(ValueError):
        m.require_snapshot(proof)


def test_snapshot_rejects_unmanifested_code(tmp_path, monkeypatch):
    m = release()
    monkeypatch.setattr(m, "OUT", tmp_path)
    monkeypatch.setattr(m, "sources", lambda: {"apps/api/app/example.py": b"safe"})
    proof = m.source()
    folder = Path(proof["snapshot_path"])
    (folder / "apps/api/app/injected.py").write_bytes(b"unexpected")
    with pytest.raises(ValueError):
        m.require_snapshot(proof)


def test_f6_phase_uses_f6_adapter_and_original_closer_without_dispatch(tmp_path, monkeypatch):
    from scripts.f6_engineering import runtime_governance as m

    from tests.governance.test_f6_engineering import foundation

    retained = m.retained
    manifest = foundation().model_copy(
        update={"actor_reference": "f2-original-governance-operator"}
    )
    private = dict(
        actor_reference=manifest.actor_reference,
        database_name="campus_ai_school_facts_f1",
        database_user="campus_ai_governance_writer",
        expected_school_id=str(manifest.school_id),
        expected_anchor_id=str(manifest.school_id),
        expected_system_identifier="known",
    )
    monkeypatch.setattr(retained, "ROOT", tmp_path)
    monkeypatch.setattr(retained, "load_config_reference", lambda path: private)
    monkeypatch.setattr(retained, "config_reference", lambda *args: {"base_config": "unused"})
    observed = {}
    monkeypatch.setattr(
        retained, "OriginalSchoolGovernanceControl", lambda **kw: observed.setdefault("control", kw)
    )
    monkeypatch.setattr(
        retained, "run_bounded_window", lambda **kw: observed.setdefault("window", kw)
    )
    timeline = dict(
        school_id=str(manifest.school_id),
        authorization_reference="f6-user-approved",
        first_execution_t0=manifest.grant_epoch.isoformat(),
        fixed_deadline=manifest.grant_valid_to.isoformat(),
    )
    args = dict(
        docker=Path("never-invoked"),
        image="sha256:" + "a" * 64,
        folder=tmp_path / "phase",
        manifest=manifest,
        governance_base=tmp_path / "base.json",
        issuer_reference="user-20260922-F6-product-decision-maker",
        authorization_reference="f6-user-approved",
        timeline=timeline,
    )
    with pytest.raises(ValueError):
        m.run_phase(**(args | {"issuer_reference": "old-F5-issuer"}))
    assert not args["folder"].exists()
    m.run_phase(**args)
    assert observed["window"]["approval"]["fixed_deadline"] == timeline["fixed_deadline"]
    assert observed["window"]["control"] is observed["control"]
    assert observed["window"]["execute"] is True
    import json

    reviewed = json.loads((args["folder"] / "review.json").read_text())
    assert reviewed["manifest"]["manifest_version"] == "f6_governance_v1"
    assert reviewed["commands"][0]["confirm_marker"].startswith("CONFIRM-F6:")
    with pytest.raises(ValueError):
        m.run_phase(**args)


def test_f6_recovery_does_not_confirm_unknown_attempt(tmp_path):
    from types import SimpleNamespace

    from scripts.f6_engineering import runtime_governance as m

    from tests.governance.test_f6_engineering import foundation

    manifest = foundation()
    key = manifest.materialize()[0].idempotency_key
    path = tmp_path / "journal.json"
    m.retained._save(path, {"manifest_sha256": "fixed", "commands": {key: {"state": "confirming"}}})
    service = SimpleNamespace(
        receipt=lambda *a, **k: None, prepare=lambda *a, **k: pytest.fail("must not dispatch")
    )
    with pytest.raises(RuntimeError, match="receipt lookup only"):
        m.execute_reviewed_commands(
            manifest=manifest,
            service=service,
            credential="test",
            evidence_path=path,
            digest="fixed",
        )


def test_worker_approval_requires_current_independent_f6_window():
    from datetime import timedelta

    from scripts.f6_engineering.runtime_governance import worker_approval_current

    from tests.governance.test_f6_engineering import T0

    approval = dict(
        state="open",
        issuer_reference="user-20260922-F6-product-decision-maker",
        actor_reference="f2-original-governance-operator",
        authorization_reference="current-f6",
        valid_from=T0.isoformat(),
        valid_to=(T0 + timedelta(minutes=10)).isoformat(),
        fixed_deadline=(T0 + timedelta(days=7)).isoformat(),
    )
    assert worker_approval_current(approval, now=T0)
    for change in (
        {"state": "closed"},
        {"issuer_reference": "old-F5"},
        {"valid_to": T0.isoformat()},
        {"valid_to": (T0 + timedelta(minutes=16)).isoformat()},
    ):
        assert not worker_approval_current(approval | change, now=T0)


def test_sources_preserve_current_guard_but_use_baseline_for_inherited_frontend(
    tmp_path, monkeypatch
):
    m = release()
    monkeypatch.setattr(m, "ROOT", tmp_path)
    guard = "apps/api/app/platform/school_facts/f2_read_authorization.py"
    excluded = "apps/web/index.html"
    for name, raw in ((guard, b"current-guard"), (excluded, b"unrelated-ui")):
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(raw)

    def command(args):
        if args[1] == "rev-parse":
            return m.F6_COMMITTED_ROOT.encode()
        if args[1] == "merge-base":
            return b""
        if args[1] == "ls-tree":
            return (guard + "\n" + excluded).encode()
        if args[1] == "ls-files":
            return b""
        if args[1] == "show":
            return b"approved-baseline-ui"
        pytest.fail("no Docker or service call allowed")

    monkeypatch.setattr(m, "command", command)
    result = m.sources()
    assert result[guard] == b"current-guard"
    assert result[excluded] == b"approved-baseline-ui"


@pytest.mark.parametrize(
    "committed",
    ["29f9a4e9537953c71579882f0b97c62b03037918", "b" * 40],
)
def test_committed_f6_descendant_keeps_baseline_and_actual_head_in_source_proof(
    tmp_path, monkeypatch, committed
):
    m = release()
    monkeypatch.setattr(m, "ROOT", tmp_path)
    monkeypatch.setattr(m, "OUT", tmp_path / "releases")
    guard = "apps/api/app/platform/school_facts/f2_read_authorization.py"
    addition = "apps/api/app/composition/school_fact_residence_query.py"
    for name in (guard, addition):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(name.encode())
    observed = []

    def command(args):
        observed.append(args)
        if args[1:3] == ["rev-parse", "HEAD"]:
            return committed.encode()
        if args[1] == "merge-base":
            if args[2:] not in (
                ["--is-ancestor", m.BASELINE, m.F6_COMMITTED_ROOT],
                ["--is-ancestor", m.F6_COMMITTED_ROOT, committed],
            ):
                raise RuntimeError("not an approved descendant")
            return b""
        if args[1] == "ls-tree":
            assert args[4] == m.BASELINE
            return (guard + "\n").encode()
        if args[1] == "ls-files":
            return (guard + "\n" + addition + "\n").encode()
        pytest.fail("No Docker or service call allowed")

    monkeypatch.setattr(m, "command", command)
    proof = m.source()
    assert proof["baseline"] == m.BASELINE
    assert proof["implementation_head"] == committed
    assert guard in proof["files"] and addition in proof["files"]
    assert any(args[1] == "merge-base" for args in observed)
    m.require_snapshot(proof)
    # An already-built baseline proof remains checkable after F6 is committed.
    m.require_snapshot({**proof, "implementation_head": m.BASELINE})
    with pytest.raises(ValueError, match="Exact F6 artifact identity required"):
        m.require_snapshot({**proof, "implementation_head": "a" * 40})


@pytest.mark.parametrize(
    "head",
    ["442b4cb414fb86462ff090eeb12b395b60eabdc9", "0" * 40, "a" * 40],
)
def test_sources_rejects_heads_outside_committed_f6_lineage(tmp_path, monkeypatch, head):
    m = release()
    monkeypatch.setattr(m, "ROOT", tmp_path)

    def command(args):
        if args[1:3] == ["rev-parse", "HEAD"]:
            return head.encode()
        if args[1] == "merge-base":
            raise RuntimeError("not an approved descendant")
        pytest.fail("A rejected head must not inspect source or services")

    monkeypatch.setattr(m, "command", command)
    with pytest.raises(RuntimeError, match="F6 approved implementation baseline changed"):
        m.sources()


@pytest.mark.parametrize(
    ("staged_addition", "approved"),
    [
        ("apps/api/app/composition/school_fact_residence_query.py", True),
        ("apps/api/app/composition/unreviewed.py", False),
    ],
)
def test_sources_include_only_approved_staged_additions(
    tmp_path, monkeypatch, staged_addition, approved
):
    m = release()
    monkeypatch.setattr(m, "ROOT", tmp_path)
    guard = "apps/api/app/platform/school_facts/f2_read_authorization.py"
    for name in (guard, staged_addition):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(name.encode())

    def command(args):
        if args[1] == "rev-parse":
            return m.F6_COMMITTED_ROOT.encode()
        if args[1] == "merge-base":
            return b""
        if args[1] == "ls-tree":
            return (guard + "\n").encode()
        if args[1] == "ls-files":
            # A staged new file is indexed, and no longer appears under --others.
            if "--cached" in args:
                return (guard + "\n" + staged_addition + "\n").encode()
            return b""
        pytest.fail("no Docker or service call allowed")

    monkeypatch.setattr(m, "command", command)
    if approved:
        result = m.sources()
        assert result[guard] == guard.encode()
        assert result[staged_addition] == staged_addition.encode()
    else:
        with pytest.raises(RuntimeError, match="Unreviewed F6 source addition"):
            m.sources()


def test_build_verifies_manifest_without_service_update(tmp_path, monkeypatch):
    import json
    from types import SimpleNamespace

    m = release()
    monkeypatch.setattr(m, "OUT", tmp_path)
    monkeypatch.setattr(m, "sources", lambda: {"apps/api/app/example.py": b"safe"})
    proof = m.source()
    calls = []

    def command(args):
        calls.append(args)
        if "run" in args:
            return json.dumps(proof["image_files"]).encode()
        if "--format" not in args:
            return json.dumps([{"Id": m.BASE, "RepoTags": ["safe-base"]}]).encode()
        return (m.BASE if args[-1] == "safe-base" else "sha256:" + "b" * 64).encode()

    monkeypatch.setattr(m, "command", command)
    monkeypatch.setattr(m, "service_ids", lambda: ["fixed-service"])
    monkeypatch.setattr(
        m.subprocess,
        "run",
        lambda args, **kw: SimpleNamespace(returncode=0, stdout=b"ok", stderr=b""),
    )
    built = m.build(proof)
    assert built["image_manifest_verified"] is True
    assert built["services_updated"] is False
    assert all("compose" not in args and "restart" not in args for args in calls)
    monkeypatch.setattr(m, "sources", lambda: {"apps/api/app/example.py": b"changed"})
    with pytest.raises(ValueError, match="Worktree changed"):
        m.build(proof)
