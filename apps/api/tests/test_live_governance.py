import importlib
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

NOW = datetime(2026, 9, 28, tzinfo=UTC)


def test_live_school_scope_binds_one_family_and_preserves_original_deadline():
    a = api()
    data = payload()
    data["commands"] = (
        dict(command="scope", key="school-people", family="f2", fact_kind="person",
             action="read", field_names=(), coverage="school",
             f2_object_level_acknowledged=True, effective_from=NOW),
    )
    manifest = a.EngineeringManifest(**data)
    command = manifest.materialize()[0]
    assert command.reference_document["coverage"] == "school"
    assert command.reference_document["school_id"] == str(UUID(int=1))
    assert command.reference_document["object_keys"] == []
    authority = a.LiveGovernanceCommandAuthorizer(
        approval=manifest.approval(), clock=lambda: NOW, approval_is_current=lambda: True
    )
    assert authority.authorize_command(command)
    assert not authority.authorize_command(replace(command, school_id=UUID(int=99)))
    assert manifest.grant_valid_to == NOW + timedelta(days=90)


@pytest.mark.parametrize("changes", [
    {"object_keys": ("person:old",)}, {"creation_keys": ("person:new",)},
    {"approved_dependency_sources": ("person:old",)}, {"prior_scope_sha256": "1" * 64},
])
def test_live_school_scope_rejects_mixed_exact_coordinates(changes):
    with pytest.raises(ValueError):
        api().ScopeInput(**(dict(
            command="scope", key="school-people", family="f2", fact_kind="person",
            action="maintain", field_names=(), coverage="school",
            f2_object_level_acknowledged=True, effective_from=NOW,
        ) | changes))


def test_old_engineering_parser_does_not_acquire_live_school_scope():
    from app.composition.school_fact_f5_engineering_governance import parse_scope
    with pytest.raises(ValueError):
        parse_scope("school_facts.f2", {
            "school_id": str(UUID(int=1)), "fact_kind": "person",
            "object_keys": [], "creation_keys": [], "coverage": "school",
        })


def api():
    return importlib.import_module("app.composition.school_fact_live_governance_cli")


def payload():
    return dict(
        manifest_version="live_governance_v1",
        school_id=UUID(int=1),
        actor_reference="reviewed:operator",
        approval_reference="reviewed:approval",
        valid_from=NOW,
        valid_to=NOW + timedelta(minutes=10),
        grant_epoch=NOW,
        grant_valid_to=NOW + timedelta(days=90),
        approved_duty_codes=frozenset({"f2_person_reader"}),
        approved_assignment_ids=frozenset({UUID(int=2)}),
        commands=(
            dict(
                command="duty",
                key="person-reader",
                duty_code="f2_person_reader",
                display_name="Person reader",
                effective_from=NOW,
            ),
        ),
    )


def test_fixed_ninety_days_without_changing_f7():
    m = api().EngineeringManifest(**payload())
    assert m.approval().grant_valid_to == NOW + timedelta(days=90)
    from app.composition.school_fact_f7_engineering_governance import F7EngineeringApproval

    with pytest.raises(ValueError):
        F7EngineeringApproval(
            **{
                k: getattr(m.approval(), k)
                for k in m.approval().__dataclass_fields__
                if k != "allowed_duties"
            }
        )


@pytest.mark.parametrize(
    "change",
    [
        dict(grant_valid_to=NOW + timedelta(days=91)),
        dict(valid_to=NOW + timedelta(minutes=16)),
        dict(actor_reference=str(UUID(int=2))),
        dict(approved_duty_codes=frozenset({"f4_invented_reader"})),
    ],
)
def test_manifest_rejects_scope_time_and_identity_growth(change):
    with pytest.raises(ValueError):
        api().EngineeringManifest(**(payload() | change))


def test_rechecks_expiry_and_closed_gate_and_exact_command():
    a = api()
    m = a.EngineeringManifest(**payload())
    current = [True]
    now = [NOW]
    auth = a.LiveGovernanceCommandAuthorizer(
        approval=m.approval(), clock=lambda: now[0], approval_is_current=lambda: current[0]
    )
    c = m.materialize()[0]
    assert auth.authorize_command(c)
    assert not auth.authorize_command(replace(c, duty_code="f2_location_reader"))
    current[0] = False
    assert not auth.authorize_command(c)
    current[0] = True
    now[0] = m.valid_to
    assert not auth.authorize_command(c)


@pytest.mark.parametrize(
    "changes",
    [
        dict(object_keys=("all*",)),
        dict(object_keys=("person:one", "person:one")),
        dict(f2_object_level_acknowledged=False),
        dict(creation_keys=("person:new",)),
    ],
)
def test_scope_rejects_wildcard_duplicate_false_field_claim_and_read_creation(changes):
    with pytest.raises(ValueError):
        api().ScopeInput(
            command="scope",
            key="people",
            family="f2",
            fact_kind="person",
            action="read",
            field_names=(),
            effective_from=NOW,
            **(dict(object_keys=("person:one",), f2_object_level_acknowledged=True) | changes),
        )


def test_f4_duties_do_not_grow():
    from app.platform.school_facts.f4_policy import F4_FACT_KINDS

    assert {d for d in api().LIVE_ALLOWED_DUTIES if d.startswith("f4_")} == {
        f"f4_{k}_{a}" for k in F4_FACT_KINDS for a in ("reader", "maintainer")
    }


def test_original_receipt_survives_closure_but_no_mutation():
    from app.composition.school_fact_live_governance import LiveGovernanceService
    from app.platform.governance.contracts import GovernanceWriteResult

    a = api()
    m = a.EngineeringManifest(**payload())
    gate = [True]

    class Writer:
        result = None
        calls = 0

        def create_duty(self, command):
            self.calls += 1
            if self.result is None:
                self.result = GovernanceWriteResult(UUID(int=9), 1, None, False)
            return self.result

        def lookup_command_result(self, command, *, command_name):
            return self.result

    w = Writer()
    s = LiveGovernanceService(
        writer=w,
        approval=m.approval(),
        credential="private-test-credential",
        clock=lambda: NOW,
        approval_is_current=lambda: gate[0],
    )
    c = m.materialize()[0]
    p = s.prepare(c, credential="private-test-credential")
    original = s.confirm(p.confirmation_token, credential="private-test-credential")
    gate[0] = False
    assert s.receipt(c, credential="private-test-credential") == original
    with pytest.raises(RuntimeError):
        s.confirm(p.confirmation_token, credential="private-test-credential")
    assert w.calls == 1
    with pytest.raises(RuntimeError):
        s.receipt(replace(c, display_name="Changed"), credential="private-test-credential")


def test_grant_requires_current_references_and_exact_recipient():
    a = api()
    data = payload()
    data["commands"] = (
        dict(
            command="grant",
            key="person-grant",
            duty_code="f2_person_reader",
            assignment_id=UUID(int=2),
            duty_id=UUID(int=3),
            duty_version=1,
            scope_id=UUID(int=4),
            scope_version=1,
            scope_owner="school_facts.f2",
            scope_sha256="1" * 64,
            valid_from=NOW,
            effective_from=NOW,
            planned_valid_to=NOW + timedelta(days=90),
        ),
    )
    m = a.EngineeringManifest(**data)
    current = [False]
    auth = a.LiveGovernanceCommandAuthorizer(
        approval=m.approval(),
        clock=lambda: NOW,
        approval_is_current=lambda: True,
        grant_reference_is_current=lambda p: current[0],
    )
    from app.composition.school_fact_engineering_governance import EngineeringGovernanceService

    request = EngineeringGovernanceService._request(None, m.materialize()[0])

    class Repository:
        role = "campus_ai_governance_writer"

        def session_user(self):
            return self.role

    r = Repository()
    with pytest.raises(PermissionError):
        auth.authorize_mutation(r, request)
    current[0] = True
    auth.authorize_mutation(r, request)
    r.role = "campus_ai_migration"
    with pytest.raises(PermissionError):
        auth.authorize_mutation(r, request)
    assert not auth.authorize_command(replace(m.materialize()[0], assignment_id=UUID(int=99)))


def test_pins_reject_changed_and_duplicate_input(tmp_path):
    from hashlib import sha256

    path = tmp_path / "input.json"
    path.write_bytes(b'{"a":1,"a":2}')
    with pytest.raises(ValueError):
        api().read_pinned(path, sha256(path.read_bytes()).hexdigest())
    path.write_bytes(b'{"a":1}')
    with pytest.raises(ValueError):
        api().read_pinned(path, "0" * 64)


def test_scope_existing_creation_exact_live_keys_and_f2_object_note():
    a = api()
    d = payload()
    d["approved_duty_codes"] = frozenset({"f2_person_maintainer"})
    d["commands"] = (
        dict(
            command="scope",
            key="staff",
            family="f2",
            fact_kind="person",
            action="maintain",
            object_keys=("person:reviewed",),
            creation_keys=("person:reviewed-new",),
            field_names=(),
            f2_object_level_acknowledged=True,
            effective_from=NOW,
        ),
    )
    m = a.EngineeringManifest(**d)
    c = m.materialize()[0]
    assert c.reference_document["creation_keys"] == ["person:reviewed-new"]
    assert a.LiveGovernanceCommandAuthorizer(
        approval=m.approval(), clock=lambda: NOW, approval_is_current=lambda: True
    ).authorize_command(c)
    assert "entire explicit objects" in a.review(m, "0" * 64)["f2_field_scope_note"]


def test_duplicate_duties_and_command_capacity():
    d = payload()
    d["commands"] = d["commands"] + (dict(d["commands"][0], key="different-key"),)
    with pytest.raises(ValueError):
        api().EngineeringManifest(**d)
    d = payload()
    d["commands"] = tuple(dict(d["commands"][0], key=f"key-{i}") for i in range(301))
    with pytest.raises(ValueError):
        api().EngineeringManifest(**d)


def test_build_grants_rejects_empty_bindings():
    a = api()
    m = a.EngineeringManifest(**payload())
    with pytest.raises(ValueError):
        a.build_grants(
            m,
            [],
            grant_valid_from=NOW + timedelta(minutes=1),
            execution_valid_to=m.valid_to,
            service=None,
            credential="unused",
        )


def evidence_fixture(tmp_path):
    import json
    from hashlib import sha256

    a = api()
    m = a.EngineeringManifest(**payload())
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(m.model_dump_json(), encoding="utf-8")
    digest = sha256(manifest_path.read_bytes()).hexdigest()
    e = dict(
        purpose="school-facts-live-governance",
        approval_reference=m.approval_reference,
        issuer_reference="school:reviewer",
        issuer_qualification_reference="school:formal-appointment",
        operator_reference=m.actor_reference,
        source_authority_reference="school:reviewed-roster",
        approved_at=m.valid_from,
        expires_at=m.valid_to,
        approved_manifest_sha256=digest,
        recipient_assignment_ids=[str(UUID(int=2))],
        operator_assignment_id=None,
        closure=dict(
            school_id=str(m.school_id),
            anchor_id=str(UUID(int=8)),
            system_identifier="1234567890123",
            database_name="campus_ai_school_facts_live_test",
            initialization_request_id=str(UUID(int=9)),
            initialization_close_receipt_id=str(UUID(int=10)),
            initialization_close_receipt_sha256="a" * 64,
            governance_close_receipt_id=str(UUID(int=11)),
            governance_close_receipt_sha256="b" * 64,
        ),
    )
    path = tmp_path / "evidence.json"
    path.write_text(json.dumps(e, default=str), encoding="utf-8")
    c = dict(
        approved_manifest_sha256=digest,
        manifest_path=str(manifest_path),
        actor_reference=m.actor_reference,
        credential="test-only-credential",
        database_host="localhost",
        database_port=5432,
        database_name="campus_ai_school_facts_live_test",
        database_user="campus_ai_governance_writer",
        database_password="test-only-password",
        expected_anchor_id=UUID(int=8),
        expected_school_id=m.school_id,
        expected_system_identifier="1234567890123",
        reader={},
        approval_reference=m.approval_reference,
        grant_epoch=m.grant_epoch,
        expected_schema_contract="school-facts-f7-v56",
        evidence_path=str(path),
        approved_evidence_sha256=sha256(path.read_bytes()).hexdigest(),
    )
    return m, c, e, path


def test_evidence_must_pin_actual_manifest_not_merely_declared_digest(tmp_path):
    m, c, e, path = evidence_fixture(tmp_path)
    a = api()
    config = a.EngineeringPrivateConfiguration(**c)
    a.verify_evidence(config, m)
    altered = payload()
    altered["commands"] = (dict(altered["commands"][0], display_name="Unreviewed"),)
    with pytest.raises(ValueError):
        a.verify_evidence(config, a.EngineeringManifest(**altered))


@pytest.mark.parametrize(
    "change",
    [
        dict(database_name="campus_ai_school_facts_f1"),
        dict(expected_schema_contract="school-facts-f7-v55"),
        dict(database_user="campus_ai_migration"),
    ],
)
def test_production_configuration_rejects_old_database_schema_or_role(tmp_path, change):
    m, c, e, path = evidence_fixture(tmp_path)
    with pytest.raises(ValueError):
        api().EngineeringPrivateConfiguration(**(c | change))


@pytest.mark.parametrize(
    "field,value",
    [
        ("operator_reference", "someone-else"),
        ("recipient_assignment_ids", [str(UUID(int=99))]),
        ("issuer_qualification_reference", ""),
    ],
)
def test_qualification_identity_and_recipients_are_mandatory(tmp_path, field, value):
    import json
    from hashlib import sha256

    m, c, e, path = evidence_fixture(tmp_path)
    e[field] = value
    path.write_text(json.dumps(e, default=str), encoding="utf-8")
    c["approved_evidence_sha256"] = sha256(path.read_bytes()).hexdigest()
    with pytest.raises(ValueError):
        api().verify_evidence(api().EngineeringPrivateConfiguration(**c), m)


def test_evidence_changed_after_composition_revokes_every_write(tmp_path, monkeypatch):
    m, c, e, path = evidence_fixture(tmp_path)
    a = api()
    config = a.EngineeringPrivateConfiguration(**c)
    captured = {}

    def fake_compose(config, manifest, **kwargs):
        captured.update(kwargs)
        return object(), ()

    monkeypatch.setattr(a.base, "compose", fake_compose)
    a.compose(config, m, approval_is_current=lambda: True)
    assert captured["approval_is_current"]()
    old = path.read_bytes()
    path.write_bytes(old + b" ")
    assert not captured["approval_is_current"]()
    path.write_bytes(old)
    assert not captured["approval_is_current"]()


@pytest.mark.parametrize("execution_delay", [timedelta(0), timedelta(days=1)])
def test_build_grants_uses_original_receipts_and_keeps_epoch(execution_delay):
    from app.platform.governance.contracts import GovernanceWriteResult

    a = api()
    data = payload()
    start = NOW + execution_delay
    data["valid_from"] = start
    data["valid_to"] = start + timedelta(minutes=10)
    data["commands"] = (dict(data["commands"][0], effective_from=start),)
    data["commands"] = data["commands"] + (
        dict(
            command="scope",
            key="people",
            family="f2",
            fact_kind="person",
            action="read",
            object_keys=("person:one",),
            field_names=(),
            f2_object_level_acknowledged=True,
            effective_from=start,
        ),
    )
    m = a.EngineeringManifest(**data)

    class Receipts:
        def receipt(self, command, *, credential):
            return GovernanceWriteResult(
                UUID(int=10 if command.idempotency_key == "live:person-reader" else 11),
                1,
                None,
                False,
            )

    binding = a.GrantBinding(
        key="grant-one", assignment_id=UUID(int=2), duty_key="person-reader", scope_key="people"
    )
    result = a.build_grants(
        m,
        [binding],
        grant_valid_from=start,
        execution_valid_to=m.valid_to,
        service=Receipts(),
        credential="private-test-credential",
    )
    grant = result.commands[0]
    assert (grant.duty_id, grant.scope_id) == (UUID(int=10), UUID(int=11))
    assert (result.valid_from, result.valid_to, result.grant_epoch, result.grant_valid_to) == (
        start,
        m.valid_to,
        m.grant_epoch,
        m.grant_valid_to,
    )
    assert result.materialize()[0].planned_valid_to == NOW + timedelta(days=90)
    assert result.materialize()[0].valid_from == start
    for grant_start, execution_end in (
        (start - timedelta(microseconds=1), m.valid_to),
        (start + timedelta(minutes=1), m.valid_to),
        (m.valid_to, m.valid_to),
        (start, m.valid_to + timedelta(microseconds=1)),
    ):
        with pytest.raises(ValueError):
            a.build_grants(
                m,
                [binding],
                grant_valid_from=grant_start,
                execution_valid_to=execution_end,
                service=Receipts(),
                credential="test-only",
            )
    with pytest.raises(ValueError):
        a.build_grants(
            m,
            [binding, binding],
            grant_valid_from=start,
            execution_valid_to=m.valid_to,
            service=Receipts(),
            credential="private-test-credential",
        )


def test_cli_fails_closed_without_exposing_input_on_unsupported_host(tmp_path, capsys, monkeypatch):
    import importlib.util
    from pathlib import Path

    root = Path(__file__).resolve().parents[3]
    spec = importlib.util.spec_from_file_location(
        "live_cli_test", root / "scripts/school_facts_live_governance.py"
    )
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)

    def denied(*args, **kwargs):
        raise ValueError("NEVER_ECHO_PRIVATE_VALIDATION")

    monkeypatch.setattr(cli, "protected", denied)
    assert (
        cli.main(
            [
                "review",
                "--config",
                str(tmp_path / "c"),
                "--manifest",
                str(tmp_path / "m"),
                "--output",
                str(tmp_path / "out"),
                "--config-sha256",
                "0" * 64,
            ]
        )
        == 2
    )
    assert "NEVER_ECHO" not in capsys.readouterr().err


def test_later_live_execution_window_keeps_original_deadline_and_rejects_tampering():
    a = api()
    data = payload()
    start = NOW + timedelta(days=1)
    data.update(valid_from=start, valid_to=start + timedelta(minutes=10))
    grant = dict(
        command="grant",
        key="later-person-grant",
        duty_code="f2_person_reader",
        assignment_id=UUID(int=2),
        duty_id=UUID(int=3),
        duty_version=1,
        scope_id=UUID(int=4),
        scope_version=1,
        scope_owner="school_facts.f2",
        scope_sha256="1" * 64,
        valid_from=start,
        effective_from=start,
        planned_valid_to=data["grant_valid_to"],
    )
    data["commands"] = (grant,)
    manifest = a.EngineeringManifest(**data)
    command = manifest.materialize()[0]
    now = [start]
    active = [True]
    authorizer = a.LiveGovernanceCommandAuthorizer(
        approval=manifest.approval(),
        clock=lambda: now[0],
        approval_is_current=lambda: active[0],
    )
    assert authorizer.authorize_command(command)
    now[0] = start - timedelta(microseconds=1)
    assert not authorizer.authorize_command(command)
    now[0] = start
    assert manifest.grant_epoch == NOW
    assert command.planned_valid_to == NOW + timedelta(days=90)
    assert not authorizer.authorize_command(
        replace(command, planned_valid_to=start + timedelta(days=90))
    )
    assert not authorizer.authorize_command(replace(command, valid_from=NOW))
    now[0] = manifest.valid_to
    assert not authorizer.authorize_command(command)
    now[0] = start
    active[0] = False
    assert not authorizer.authorize_command(command)
    for changed in (
        dict(valid_from=NOW),
        dict(valid_from=start + timedelta(minutes=1)),
        dict(valid_from=manifest.valid_to),
        dict(planned_valid_to=start + timedelta(days=90)),
    ):
        with pytest.raises(ValueError):
            a.EngineeringManifest(**(data | {"commands": (grant | changed,)}))
    with pytest.raises(ValueError):
        a.EngineeringManifest(**(data | {"grant_valid_to": start + timedelta(days=90)}))


def test_live_cli_build_grants_uses_later_batch_start_not_old_epoch(tmp_path, monkeypatch):
    import importlib.util
    import json
    from hashlib import sha256
    from pathlib import Path

    from app.platform.governance.contracts import GovernanceWriteResult

    a = api()
    _, config_data, evidence, evidence_path = evidence_fixture(tmp_path)
    data = payload()
    start = NOW + timedelta(days=1)
    data.update(valid_from=start, valid_to=start + timedelta(minutes=10))
    data["commands"] = (
        dict(data["commands"][0], effective_from=start),
        dict(
            command="scope",
            key="people",
            family="f2",
            fact_kind="person",
            action="read",
            coverage="school",
            field_names=(),
            f2_object_level_acknowledged=True,
            effective_from=start,
        ),
    )
    manifest = a.EngineeringManifest(**data)
    manifest_path = Path(config_data["manifest_path"])
    manifest_path.write_text(manifest.model_dump_json(), encoding="utf-8")
    digest = sha256(manifest_path.read_bytes()).hexdigest()
    evidence.update(
        approved_manifest_sha256=digest, approved_at=start, expires_at=manifest.valid_to
    )
    evidence_path.write_text(json.dumps(evidence, default=str), encoding="utf-8")
    config_data.update(
        approved_manifest_sha256=digest,
        approved_evidence_sha256=sha256(evidence_path.read_bytes()).hexdigest(),
    )
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config_data, default=str), encoding="utf-8")
    bindings = tmp_path / "bindings.json"
    bindings.write_text(
        json.dumps(
            [
                dict(
                    key="grant",
                    assignment_id=str(UUID(int=2)),
                    duty_key="person-reader",
                    scope_key="people",
                )
            ]
        ),
        encoding="utf-8",
    )
    spec = importlib.util.spec_from_file_location(
        "live_cli_later_window",
        Path(__file__).resolve().parents[3] / "scripts/school_facts_live_governance.py",
    )
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    outputs = []
    monkeypatch.setattr(cli, "protected", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli, "write_new", lambda path, value: outputs.append(value))
    monkeypatch.setattr(cli, "current_approval_guard", lambda *args, **kwargs: lambda: True)

    class Receipts:
        def receipt(self, command, *, credential):
            return GovernanceWriteResult(
                UUID(int=10 if command.idempotency_key == "live:person-reader" else 11),
                1,
                None,
                False,
            )

    monkeypatch.setattr(a, "compose", lambda *args, **kwargs: (Receipts(), ()))
    result = cli.main(
        [
            "build-grants",
            "--config",
            str(config_path),
            "--manifest",
            str(manifest_path),
            "--config-sha256",
            sha256(config_path.read_bytes()).hexdigest(),
            "--bindings",
            str(bindings),
            "--output",
            str(tmp_path / "output.json"),
        ]
    )
    assert result == 0
    generated = a.EngineeringManifest.model_validate_json(json.dumps(outputs[0]["manifest"]))
    assert generated.grant_epoch == NOW
    assert generated.commands[0].valid_from == start
    assert generated.grant_valid_to == NOW + timedelta(days=90)

def test_live_guardian_recorder_uses_existing_scope_without_changing_engineering():
    from types import SimpleNamespace

    from app.composition.school_fact_f7_engineering_governance import F7_ALLOWED_DUTIES
    from app.platform.school_facts.f3_policy import F3FactScope
    a = api()
    code = 'f3_guardian_relationship_recorder'
    assert code not in F7_ALLOWED_DUTIES
    assert code in a.LIVE_ALLOWED_DUTIES
    scope = F3FactScope(UUID(int=1), 'guardian_relationship', (), (),
                       ('status',), coverage='school')
    proof = SimpleNamespace(duty_code=code, scope_owner='school_facts.f3',
                            scope_sha256=scope.document_sha256)
    assert a.grant_reference_matches(proof, school_id=UUID(int=1), actual_duty_code=code,
        actual_owner='school_facts.f3', actual_document=scope.document,
        actual_sha256=scope.document_sha256)
    assert not a.grant_reference_matches(proof, school_id=UUID(int=2), actual_duty_code=code,
        actual_owner='school_facts.f3', actual_document=scope.document,
        actual_sha256=scope.document_sha256)
