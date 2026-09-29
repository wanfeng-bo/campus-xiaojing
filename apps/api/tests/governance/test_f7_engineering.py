"""Offline F7 governance regression: no database/role or private configuration access."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from importlib import import_module
from types import SimpleNamespace
from uuid import UUID

import pytest

T0 = datetime(2026, 9, 24, tzinfo=UTC)
SCHOOL, STAFF = UUID(int=1), UUID(int=2)


def api():
    return import_module("app.composition.school_fact_f7_engineering_cli")


def foundation():
    m = api()
    return m.build_duties_and_scopes(
        school_id=SCHOOL,
        actor_reference="independent-operator",
        approval_reference="f7-approved",
        valid_from=T0,
        valid_to=T0 + timedelta(minutes=10),
        grant_epoch=T0,
        approved_assignment_ids=frozenset({STAFF}),
        rows=(
            m.ScopeMatrixRow(
                key="beds",
                family="f6",
                fact_kind="dorm_bed",
                action="maintain",
                object_keys=("old:bed",),
                approved_dependency_sources=("old:bed",),
                field_names=("display_name",),
            ),
        ),
    )


def terminal():
    m = api()
    from app.platform.school_facts.read_policy import school_scope_document_hash

    data = foundation().model_dump()
    data.update(
        approved_duty_codes={"f7_own_terminal_reader"},
        commands=[
            dict(
                command="grant",
                key="terminal",
                assignment_id=STAFF,
                duty_id=UUID(int=10),
                duty_version=1,
                scope_id=UUID(int=11),
                scope_version=1,
                duty_code="f7_own_terminal_reader",
                scope_owner="school_facts.school",
                scope_sha256=school_scope_document_hash(SCHOOL),
                effective_from=T0,
                valid_from=T0,
                planned_valid_to=T0 + timedelta(days=7),
            )
        ],
    )
    return m.EngineeringManifest.model_validate(data)


def test_f7_pins_commands_actor_and_seven_day_epoch():
    m, manifest = api(), foundation()
    auth = m.F7EngineeringCommandAuthorizer(
        approval=manifest.approval(),
        clock=lambda: T0,
        approval_is_current=lambda: True,
    )
    assert manifest.manifest_version == "f7_governance_v1"
    assert all(auth.authorize_command(c) for c in manifest.materialize())
    command = manifest.materialize()[0]
    assert not auth.authorize_command(replace(command, display_name="changed"))
    assert not auth.authorize_command(
        replace(
            command,
            actor=replace(command.actor, actor_kind="f6_engineering"),
        )
    )
    assert m.command_marker("a" * 64, command).startswith("CONFIRM-F7:")
    for changes in (
        {"manifest_version": "f6_governance_v1"},
        {"grant_valid_to": T0 + timedelta(days=8)},
        {"valid_to": T0 + timedelta(minutes=16)},
    ):
        with pytest.raises(ValueError):
            m.EngineeringManifest.model_validate({**manifest.model_dump(), **changes})


@pytest.mark.parametrize(
    "family,kind,field",
    [
        ("f2", "official_contact", "contact_value"),
        ("f3", "student", "status"),
        ("f4", "education_class", "display_name"),
        ("f5", "teaching_assignment", "assignment_source_key"),
        ("f6", "dorm_bed", "display_name"),
    ],
)
def test_reviewed_all_family_maintenance_is_finite(family, kind, field):
    m = api()
    values = dict(
        key="reviewed",
        family=family,
        fact_kind=kind,
        action="maintain",
        object_keys=("existing:one",),
        field_names=(field,),
    )
    with pytest.raises(ValueError):
        m.ScopeMatrixRow(**values)
    row = m.ScopeMatrixRow(**values, approved_dependency_sources=("existing:one",))
    assert row.scope(SCHOOL).object_keys == ("existing:one",)
    with pytest.raises(ValueError):
        m.ScopeMatrixRow(**values, approved_dependency_sources=("other",))


def test_terminal_grants_only_exact_existing_single_school_scope():
    m, manifest = api(), terminal()
    proof = manifest.commands[0].proof()
    values = dict(
        school_id=SCHOOL,
        actual_duty_code=proof.duty_code,
        actual_owner=proof.scope_owner,
        actual_document={"school_id": str(SCHOOL)},
        actual_sha256=proof.scope_sha256,
    )
    assert m.grant_reference_matches(proof, **values)
    for changes in (
        {"school_id": UUID(int=99)},
        {"actual_owner": "school_facts.f2"},
        {"actual_duty_code": "official_contact_reader"},
        {"actual_document": {"school_id": str(SCHOOL), "all_schools": True}},
        {"actual_sha256": "0" * 64},
    ):
        assert not m.grant_reference_matches(proof, **{**values, **changes})
    assert "school_facts.school" not in manifest.scope_schemas
    command = manifest.materialize()[0]
    assert command.basis_authority == "f7_engineering_policy"
    auth = m.F7EngineeringCommandAuthorizer(
        approval=manifest.approval(),
        clock=lambda: T0,
        approval_is_current=lambda: True,
        grant_reference_is_current=lambda p: p == proof,
    )
    service = m.F7EngineeringGovernanceService(
        writer=None,
        approval=manifest.approval(),
        credential="synthetic-credential",
        clock=lambda: T0,
        approval_is_current=lambda: True,
    )
    request = service._request(command)
    auth.authorize_session(
        SimpleNamespace(session_user=lambda: "campus_ai_governance_writer"), request
    )
    auth.grant_reference_is_current = lambda p: False
    with pytest.raises(PermissionError):
        auth.authorize_session(
            SimpleNamespace(session_user=lambda: "campus_ai_governance_writer"), request
        )


def test_terminal_cannot_bind_family_scope_or_self_assignment():
    m, raw = api(), terminal().model_dump()
    for changes in (
        {"scope_owner": "school_facts.f6"},
        {"duty_code": "f6_dorm_bed_reader"},
        {"scope_sha256": "0" * 64},
    ):
        with pytest.raises(ValueError):
            m.EngineeringManifest.model_validate(
                {**raw, "commands": [{**raw["commands"][0], **changes}]}
            )
    with pytest.raises(ValueError):
        m.EngineeringManifest.model_validate({**raw, "actor_reference": str(STAFF)})


def test_f2_field_limit_disclosed_and_closed_approval_denies():
    m, manifest = api(), foundation()
    assert "object" in m.review(manifest, "a" * 64)["f2_field_scope_note"]
    for now, current in ((T0, False), (T0 + timedelta(minutes=10), True)):
        auth = m.F7EngineeringCommandAuthorizer(
            approval=manifest.approval(),
            clock=lambda now=now: now,
            approval_is_current=lambda current=current: current,
        )
        assert not auth.authorize_command(manifest.materialize()[0])


def test_worker_rejects_old_f6_approval_and_delegates_only_f7(monkeypatch):
    worker = import_module("scripts.f7_engineering.runtime_governance")
    approval = dict(
        state="open",
        issuer_reference=worker.ISSUER_REFERENCE,
        actor_reference="f2-original-governance-operator",
        authorization_reference="f7-new",
        valid_from=T0.isoformat(),
        valid_to=(T0 + timedelta(minutes=10)).isoformat(),
        fixed_deadline=(T0 + timedelta(days=7)).isoformat(),
    )
    assert worker.worker_approval_current(approval, now=T0)
    for changes in (
        {"issuer_reference": "user-20260922-F6-product-decision-maker"},
        {"state": "closed"},
        {"actor_reference": str(STAFF)},
    ):
        assert not worker.worker_approval_current({**approval, **changes}, now=T0)
    calls = []
    monkeypatch.setattr(worker.retained, "run_phase", lambda **kw: calls.append(kw))
    with pytest.raises(ValueError):
        worker.run_phase(issuer_reference="user-20260922-F6-product-decision-maker")
    with pytest.raises(ValueError):
        worker.run_phase(
            issuer_reference=worker.ISSUER_REFERENCE, schema_contract="school-facts-f6-v45"
        )
    worker.run_phase(issuer_reference=worker.ISSUER_REFERENCE)
    assert calls[0]["_stage"] == "f7"
    assert calls[0]["_schema_contracts"] == {"school-facts-f7-v55"}


def test_guardian_verification_is_separate_and_never_creates_relationships():
    m = api()
    row = m.ScopeMatrixRow(
        key="verified-guardian",
        family="f3",
        fact_kind="guardian_relationship",
        action="verify",
        object_keys=("old:guardian",),
        approved_dependency_sources=("old:guardian",),
        field_names=("status", "verification_source", "verification_reference"),
    )
    assert row.duty_code == "f3_guardian_relationship_verifier"
    with pytest.raises(ValueError):
        m.ScopeMatrixRow(**{**row.model_dump(), "creation_keys": ("f7:guardian_relationship:new",)})
    with pytest.raises(ValueError):
        m.ScopeMatrixRow(**{**row.model_dump(), "fact_kind": "student"})
    scope = row.scope(SCHOOL)
    proof = m.GrantReferenceProof(
        UUID(int=10), 1, row.duty_code, UUID(int=11), 1, "school_facts.f3", scope.document_sha256
    )
    assert m.grant_reference_matches(
        proof,
        school_id=SCHOOL,
        actual_duty_code=row.duty_code,
        actual_owner="school_facts.f3",
        actual_document=scope.document,
        actual_sha256=scope.document_sha256,
    )


def test_residence_quality_owner_scope_retains_exact_owned_fields():
    m = api()
    row = m.ScopeMatrixRow(
        key="residence-quality",
        family="f6",
        fact_kind="student_bed_residence",
        action="maintain",
        quality_owners=(("person", "old:person"),),
        field_names=("residence_mode",),
    )
    scope = row.scope(SCHOOL)
    proof = m.GrantReferenceProof(
        UUID(int=10), 1, row.duty_code, UUID(int=11), 1, "school_facts.f6", scope.document_sha256
    )
    assert m.grant_reference_matches(
        proof,
        school_id=SCHOOL,
        actual_duty_code=row.duty_code,
        actual_owner="school_facts.f6",
        actual_document=scope.document,
        actual_sha256=scope.document_sha256,
    )


def test_new_source_is_explicitly_f7_scoped_and_read_never_creates():
    m = api()
    row = dict(
        key="new-bed",
        family="f6",
        fact_kind="dorm_bed",
        action="maintain",
        creation_keys=("f7:dorm_bed:new",),
        field_names=("display_name",),
    )
    assert m.ScopeMatrixRow(**row).scope(SCHOOL).creation_keys == ("f7:dorm_bed:new",)
    for changes in (
        {"creation_keys": ("f6:dorm_bed:old",)},
        {"action": "read"},
        {"creation_keys": ("f7:dorm_bed:*",)},
        {"field_names": ("private_other",)},
    ):
        with pytest.raises(ValueError):
            m.ScopeMatrixRow(**{**row, **changes}).scope(SCHOOL)


def test_family_grants_require_original_receipts_and_retain_epoch():
    from app.platform.governance.contracts import CreateDutyCommand, GovernanceWriteResult

    m, manifest = api(), foundation()
    bindings = [
        m.GrantBinding(
            key="grant-bed",
            assignment_id=STAFF,
            duty_key="f6_dorm_bed_maintainer",
            scope_key="beds",
        )
    ]
    args = dict(
        grant_valid_from=T0 + timedelta(days=1),
        execution_valid_to=T0 + timedelta(days=1, minutes=10),
        credential="synthetic",
    )
    with pytest.raises(ValueError):
        m.build_grants(
            manifest, bindings, service=SimpleNamespace(receipt=lambda *a, **k: None), **args
        )
    svc = SimpleNamespace(
        receipt=lambda c, **k: GovernanceWriteResult(
            UUID(int=10 if isinstance(c, CreateDutyCommand) else 11),
            1,
            None,
            False,
        )
    )
    granted = m.build_grants(manifest, bindings, service=svc, **args)
    assert granted.grant_epoch == T0
    assert granted.grant_valid_to == T0 + timedelta(days=7)
    assert granted.materialize()[0].basis_authority == "f7_engineering_policy"
    assert m.EngineeringManifest.model_validate_json(granted.model_dump_json()) == granted


def test_compose_rejects_operator_self_grant_before_opening_any_connection():
    m, manifest = api(), terminal()
    config = SimpleNamespace(
        expected_school_id=SCHOOL,
        actor_reference=manifest.actor_reference,
        approval_reference=manifest.approval_reference,
        grant_epoch=T0,
        operator_assignment_id=STAFF,
    )
    with pytest.raises(ValueError, match="itself"):
        m.compose(config, manifest, approval_is_current=lambda: True)


def test_original_receipt_is_idempotent_even_after_logical_approval_closes(tmp_path):
    from scripts.f7_engineering.runtime_governance import execute_reviewed_commands

    from app.platform.governance.contracts import GovernanceWriteResult

    m, manifest = api(), terminal()
    result = GovernanceWriteResult(UUID(int=25), 1, 2, True)
    calls = []
    writer = SimpleNamespace(
        lookup_command_result=lambda command, **kw: calls.append(command) or result
    )
    service = m.F7EngineeringGovernanceService(
        writer=writer,
        approval=manifest.approval(),
        credential="synthetic-credential",
        clock=lambda: T0 + timedelta(hours=1),
        approval_is_current=lambda: False,
    )
    for _ in range(2):
        evidence = execute_reviewed_commands(
            manifest=manifest,
            service=service,
            credential="synthetic-credential",
            evidence_path=tmp_path / "receipts.json",
            digest="a" * 64,
            receipt_only=True,
        )
        assert evidence["status"] == "succeeded"
    assert len(calls) == 2
    assert calls[0].idempotency_key == calls[1].idempotency_key


def test_unknown_commit_persists_intent_and_never_blindly_retries(tmp_path):
    import json

    from scripts.f7_engineering.runtime_governance import execute_reviewed_commands

    manifest, confirms = terminal(), []

    def confirm(*args, **kwargs):
        confirms.append(True)
        raise RuntimeError("simulated lost acknowledgement")

    service = SimpleNamespace(
        receipt=lambda *a, **kw: None,
        prepare=lambda *a, **kw: SimpleNamespace(confirmation_token="synthetic"),
        confirm=confirm,
    )
    args = dict(
        manifest=manifest,
        service=service,
        credential="synthetic",
        evidence_path=tmp_path / "intent.json",
        digest="a" * 64,
    )
    with pytest.raises(RuntimeError, match="lost acknowledgement"):
        execute_reviewed_commands(**args)
    assert (
        json.loads(args["evidence_path"].read_text())["commands"]["f7:terminal"]["state"]
        == "confirming"
    )
    with pytest.raises(RuntimeError, match="receipt lookup only"):
        execute_reviewed_commands(**args)
    assert confirms == [True]


@pytest.mark.parametrize("fail", [False, True])
def test_f7_uses_retained_supervisor_closure_on_success_or_failure(tmp_path, fail):
    import json

    from scripts.f7_engineering import runtime_governance as worker

    approval = dict(
        issuer_reference=worker.ISSUER_REFERENCE,
        actor_reference="f2-original-governance-operator",
        authorization_reference="f7-approved",
        valid_from=T0.isoformat(),
        valid_to=(T0 + timedelta(minutes=10)).isoformat(),
        fixed_deadline=(T0 + timedelta(days=7)).isoformat(),
    )
    events = []
    control = SimpleNamespace(
        require_closed=lambda: events.append("verify-closed"),
        open=lambda: events.append("LOGIN"),
        close=lambda: events.append("NOLOGIN"),
    )

    def execute():
        if fail:
            raise RuntimeError("simulated failure")
        return "succeeded"

    args = dict(
        approval_path=tmp_path / "approval.json",
        approval=approval,
        control=control,
        worker=execute,
        execute=True,
        clock=lambda: T0,
    )
    if fail:
        with pytest.raises(RuntimeError):
            worker.retained.run_bounded_window(**args)
    else:
        assert worker.retained.run_bounded_window(**args) == "succeeded"
    assert events == ["verify-closed", "LOGIN", "NOLOGIN", "verify-closed"]
    assert json.loads(args["approval_path"].read_text())["state"] == "closed"
    with pytest.raises(ValueError, match="cannot be reopened"):
        worker.retained.run_bounded_window(**args)
    assert len(events) == 4


def test_cli_review_close_and_f6_marker_rejection_never_connect(tmp_path, monkeypatch):
    import json
    from hashlib import sha256

    from scripts import school_facts_f7_governance as cli

    manifest = foundation()
    manifest_path, config_path = tmp_path / "manifest.json", tmp_path / "private-synthetic.json"
    manifest_path.write_text(manifest.model_dump_json(), encoding="utf-8")
    digest = sha256(manifest_path.read_bytes()).hexdigest()
    config_path.write_text(
        json.dumps(
            dict(
                approved_manifest_sha256=digest,
                actor_reference=manifest.actor_reference,
                approval_reference=manifest.approval_reference,
                grant_epoch=T0.isoformat(),
                credential="synthetic-credential",
                database_host="unused",
                database_port=5432,
                database_name="unused",
                database_user="campus_ai_governance_writer",
                database_password="synthetic-password",
                expected_anchor_id=str(SCHOOL),
                expected_school_id=str(SCHOOL),
                expected_system_identifier="123",
                expected_schema_contract="school-facts-f7-v55",
                reader={},
            )
        ),
        encoding="utf-8",
    )

    def never(*args, **kwargs):
        raise AssertionError("Offline review cannot connect")

    monkeypatch.setattr(cli.adapter, "compose", never)
    args = ["--config", str(config_path), "--manifest", str(manifest_path)]
    assert cli.main(["review", *args, "--output", str(tmp_path / "review.json")]) == 0
    assert (
        cli.main(
            [
                "close",
                *args,
                "--confirm",
                "CLOSE-F6:" + digest,
                "--output",
                str(tmp_path / "wrong.json"),
            ]
        )
        == 2
    )
    assert not config_path.with_suffix(".json.closed").exists()
    assert (
        cli.main(
            [
                "close",
                *args,
                "--confirm",
                "CLOSE-F7:" + digest,
                "--output",
                str(tmp_path / "closed.json"),
            ]
        )
        == 0
    )
    assert (
        cli.main(
            [
                "execute",
                *args,
                "--index",
                "0",
                "--confirm",
                api().command_marker(digest, manifest.materialize()[0]),
                "--output",
                str(tmp_path / "execution.json"),
            ]
        )
        == 2
    )


def test_phase_pins_actual_f7_review_and_fixed_timeline_before_supervisor(tmp_path, monkeypatch):
    import json

    from scripts.f7_engineering import runtime_governance as worker

    retained = worker.retained
    manifest = api().EngineeringManifest.model_validate(
        {
            **foundation().model_dump(),
            "actor_reference": "f2-original-governance-operator",
        }
    )
    private = dict(
        actor_reference=manifest.actor_reference,
        database_name="campus_ai_school_facts_f1",
        database_user="campus_ai_governance_writer",
        expected_school_id=str(SCHOOL),
        expected_anchor_id=str(SCHOOL),
        expected_system_identifier="123",
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
        school_id=str(SCHOOL),
        authorization_reference="f7-new-approval",
        first_execution_t0=T0.isoformat(),
        fixed_deadline=manifest.grant_valid_to.isoformat(),
    )
    args = dict(
        docker=tmp_path / "never-invoked",
        image="sha256:" + "a" * 64,
        folder=tmp_path / "phase",
        manifest=manifest,
        governance_base=tmp_path / "unused.json",
        issuer_reference=worker.ISSUER_REFERENCE,
        authorization_reference="f7-new-approval",
        timeline=timeline,
    )
    with pytest.raises(ValueError, match="timeline"):
        worker.run_phase(
            **{
                **args,
                "timeline": {
                    **timeline,
                    "first_execution_t0": (T0 + timedelta(days=1)).isoformat(),
                },
            }
        )
    assert not args["folder"].exists()
    worker.run_phase(**args)
    assert observed["window"]["approval"]["fixed_deadline"] == timeline["fixed_deadline"]
    assert observed["window"]["approval"]["issuer_reference"] == worker.ISSUER_REFERENCE
    review = json.loads((args["folder"] / "review.json").read_text())
    assert review["manifest"]["manifest_version"] == "f7_governance_v1"
    assert all(row["confirm_marker"].startswith("CONFIRM-F7:") for row in review["commands"])
