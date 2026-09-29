import json
from hashlib import sha256
from uuid import uuid4

import pytest

from app.composition.school_fact_f2_engineering_cli import (
    EngineeringManifest,
    build_grants,
    command_marker,
    load_manifest,
)


def document():
    return {
        "manifest_version": "f2_governance_v1",
        "school_id": str(uuid4()),
        "actor_reference": "synthetic-f2-operator",
        "valid_from": "2026-09-06T00:00:00Z",
        "valid_to": "2026-09-07T00:00:00Z",
        "commands": [
            {
                "command": "duty",
                "key": "person-read",
                "duty_code": "f2_person_reader",
                "display_name": "合成人员读取",
                "effective_from": "2026-09-06T00:00:00Z",
            }
        ],
    }


def test_manifest_pin_missing_changed_and_unknown_payload(tmp_path):
    path = tmp_path / "manifest.json"
    data = json.dumps(document()).encode()
    path.write_bytes(data)
    digest = sha256(data).hexdigest()
    assert load_manifest(path, digest).actor_reference == "synthetic-f2-operator"
    with pytest.raises(ValueError):
        load_manifest(path, "")
    path.write_bytes(data + b" ")
    with pytest.raises(ValueError):
        load_manifest(path, digest)
    value = document()
    value["commands"][0]["content_text"] = "SECRET_BODY"
    with pytest.raises(ValueError) as exc:
        EngineeringManifest.model_validate(value)
    assert "SECRET_BODY" not in str(exc.value)


def test_finite_commands_and_marker_bind_exact_manifest():
    manifest = EngineeringManifest.model_validate(document())
    command = manifest.materialize()[0]
    assert command.actor.actor_assignment_id is None
    assert command_marker("a" * 64, command) != command_marker("b" * 64, command)
    value = document()
    value["commands"][0]["duty_code"] = "governance_admin"
    with pytest.raises(ValueError):
        EngineeringManifest.model_validate(value)
    value = document()
    value["valid_to"] = value["valid_from"]
    with pytest.raises(ValueError):
        EngineeringManifest.model_validate(value)


def test_grants_require_real_receipts():
    manifest = EngineeringManifest.model_validate(document())

    class Missing:
        def receipt(self, command, *, credential):
            return None

    with pytest.raises(ValueError):
        build_grants(manifest, [], service=Missing(), credential="private")


@pytest.mark.parametrize("version", ["f2_governance_v1", "f2_governance_v2"])
def test_grants_resolve_authoritative_ids_and_forbid_operator_self_grant(version):
    from datetime import UTC, datetime, timedelta

    from app.composition.school_fact_f2_engineering_cli import (
        GrantBinding,
        ScopeMatrixRow,
        build_duties_and_scopes,
    )
    from app.platform.governance.contracts import CreateDutyCommand, GovernanceWriteResult

    now = datetime(2026, 9, 6, tzinfo=UTC)
    assignment, duty_id, scope_id = uuid4(), uuid4(), uuid4()
    manifest = build_duties_and_scopes(
        school_id=uuid4(),
        actor_reference=str(assignment),
        valid_from=now,
        valid_to=now + timedelta(days=1),
        rows=(
            ScopeMatrixRow(
                key="person-scope",
                fact_kind="person",
                purpose="reader",
                object_keys=("person:one",),
            ),
        ),
    )

    if version == "f2_governance_v2":
        manifest = EngineeringManifest.model_validate(
            {
                **manifest.model_dump(),
                "manifest_version": version,
                "grant_valid_to": now + timedelta(hours=48),
            }
        )

    class Receipts:
        def receipt(self, command, *, credential):
            return GovernanceWriteResult(
                duty_id if isinstance(command, CreateDutyCommand) else scope_id, 7, None, False
            )

    binding = GrantBinding(
        key="grant-person",
        assignment_id=uuid4(),
        duty_key="f2_person_reader",
        scope_key="person-scope",
    )
    result = build_grants(manifest, [binding], service=Receipts(), credential="private")
    assert result.commands[0].duty_id == duty_id
    assert result.commands[0].scope_id == scope_id
    assert result.commands[0].duty_version == 7
    assert result.manifest_version == version
    assert result.commands[0].planned_valid_to == (manifest.grant_valid_to or manifest.valid_to)
    with pytest.raises(ValueError):
        build_grants(
            manifest,
            [binding.model_copy(update={"assignment_id": assignment})],
            service=Receipts(),
            credential="private",
        )

    class Missing:
        def receipt(self, command, *, credential):
            return None

    with pytest.raises(ValueError):
        build_grants(manifest, [binding], service=Missing(), credential="private")


def test_cli_rejection_does_not_print_secret_or_body(tmp_path, capsys):
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[4] / "scripts/school_facts_f2_governance.py"
    spec = importlib.util.spec_from_file_location("f2_cli", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    config = tmp_path / "private.json"
    config.write_text(
        '{"credential":"SECRET_OPERATOR","database_password":"SECRET_DSN","content_text":"SECRET_BODY"}'
    )
    assert (
        module.main(
            [
                "execute",
                "--config",
                str(config),
                "--manifest",
                str(config),
                "--output",
                str(tmp_path / "out"),
            ]
        )
        == 2
    )
    output = capsys.readouterr()
    assert "SECRET" not in output.out + output.err


def test_cli_exact_confirmation_and_closed_window_fail_before_connection(tmp_path, monkeypatch):
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[4] / "scripts/school_facts_f2_governance.py"
    spec = importlib.util.spec_from_file_location("f2_cli_closed", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    manifest_path = tmp_path / "manifest.json"
    data = json.dumps(document()).encode()
    manifest_path.write_bytes(data)
    digest = sha256(data).hexdigest()
    doc = json.loads(data)
    config = tmp_path / "private.json"
    config.write_text(
        json.dumps(
            dict(
                approved_manifest_sha256=digest,
                actor_reference=doc["actor_reference"],
                credential="separate-operator-secret",
                database_host="127.0.0.1",
                database_port=55434,
                database_name="synthetic",
                database_user="campus_ai_governance_writer",
                database_password="private",
                expected_anchor_id=str(uuid4()),
                expected_school_id=doc["school_id"],
                expected_system_identifier="1234567890",
                reader={},
            )
        )
    )

    def no_connection(*args):
        raise AssertionError("failure must precede connection")

    monkeypatch.setattr(module, "compose", no_connection)
    base = [
        "--config",
        str(config),
        "--manifest",
        str(manifest_path),
        "--output",
        str(tmp_path / "out.json"),
    ]
    assert module.main(["execute", *base, "--index", "0", "--confirm", "wrong"]) == 2
    assert module.main(["close", *base, "--confirm", "CLOSE-F2:" + digest]) == 0
    assert config.with_suffix(".json.closed").exists()
    command = EngineeringManifest.model_validate(doc).materialize()[0]
    assert (
        module.main(
            ["execute", *base, "--index", "0", "--confirm", command_marker(digest, command)]
        )
        == 2
    )


def test_cli_standalone_import_registers_all_foreign_key_targets():
    import subprocess
    import sys

    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            "import app.composition.school_fact_f2_engineering_cli; "
            "from app.db.base import Base; "
            "assert all(fk.column is not None "
            "for t in Base.metadata.tables.values() for fk in t.foreign_keys)",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert probe.returncode == 0, "Standalone CLI metadata has unresolved foreign keys"


def grant_document():
    value = document()
    value.update(
        manifest_version="f2_governance_v2",
        valid_to="2026-09-06T00:30:00Z",
        grant_valid_to="2026-09-08T00:00:00Z",
    )
    value["commands"] = [
        dict(
            command="grant",
            key="grant-48-hours",
            effective_from=value["valid_from"],
            valid_from=value["valid_from"],
            planned_valid_to=value["grant_valid_to"],
            assignment_id=str(uuid4()),
            duty_id=str(uuid4()),
            duty_version=1,
            scope_id=str(uuid4()),
            scope_version=1,
        )
    ]
    return value


def test_v2_short_execution_window_accepts_exact_approved_grant_cap():
    from app.composition.school_fact_engineering_governance import (
        EngineeringCommandAuthorizer,
        EngineeringGovernanceService,
    )

    manifest = EngineeringManifest.model_validate(grant_document())
    command = manifest.materialize()[0]
    service = EngineeringGovernanceService(
        writer=object(),
        approval=manifest.approval(),
        credential="independent-secret",
        clock=lambda: manifest.valid_from,
    )
    assert EngineeringCommandAuthorizer(
        approval=manifest.approval(), clock=lambda: manifest.valid_from
    ).authorize(service._request(command))
    assert manifest.approval().grant_valid_to == command.planned_valid_to


@pytest.mark.parametrize(
    "change", ["missing_cap", "over_cap", "legacy_extension", "legacy_cap", "naive_cap"]
)
def test_versioned_grant_cap_rejects_ambiguous_or_excessive_bounds(change):
    value = grant_document()
    if change == "missing_cap":
        del value["grant_valid_to"]
    elif change == "over_cap":
        value["commands"][0]["planned_valid_to"] = "2026-09-08T00:00:01Z"
    elif change == "legacy_extension":
        value["manifest_version"] = "f2_governance_v1"
        del value["grant_valid_to"]
    elif change == "legacy_cap":
        value["manifest_version"] = "f2_governance_v1"
    else:
        value["grant_valid_to"] = "2026-09-08T00:00:00"
    with pytest.raises(ValueError):
        EngineeringManifest.model_validate(value)


@pytest.mark.parametrize(
    "loss", ["closed", "config_replaced", "manifest_replaced", "config_missing"]
)
def test_private_pin_is_rechecked_after_initial_load(tmp_path, loss):
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[4] / "scripts/school_facts_f2_governance.py"
    spec = importlib.util.spec_from_file_location("f2_current_pin", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    config, manifest = tmp_path / "private.json", tmp_path / "manifest.json"
    config.write_bytes(b"private-fixture")
    manifest.write_bytes(b"approved-fixture")
    guard = module.current_approval_guard(
        config,
        manifest,
        config_sha256=sha256(config.read_bytes()).hexdigest(),
        manifest_sha256=sha256(manifest.read_bytes()).hexdigest(),
    )
    assert guard()
    if loss == "closed":
        config.with_suffix(".json.closed").write_text("closed")
    elif loss == "config_replaced":
        config.write_bytes(b"different-private-fixture")
    elif loss == "manifest_replaced":
        manifest.write_bytes(b"different-approved-fixture")
    else:
        config.unlink()
    assert not guard()
    config.write_bytes(b"private-fixture")
    manifest.write_bytes(b"approved-fixture")
    if config.with_suffix(".json.closed").exists():
        config.with_suffix(".json.closed").unlink()
    assert not guard(), "Detected withdrawal cannot revive within an execution"


@pytest.mark.parametrize(
    "field",
    [
        "assignment_id",
        "duty_id",
        "scope_id",
        "school_id",
        "actor",
        "valid_from",
        "planned_valid_to",
        "effective_from",
        "idempotency_key",
    ],
)
def test_v2_approval_binds_exact_recipient_scope_identity_and_period(field):
    from dataclasses import replace
    from datetime import timedelta

    from app.composition.school_fact_engineering_governance import (
        EngineeringGovernanceDenied,
        EngineeringGovernanceService,
    )

    manifest = EngineeringManifest.model_validate(grant_document())
    original = manifest.materialize()[0]
    if field.endswith("_id"):
        value = uuid4()
    elif field == "actor":
        value = replace(original.actor, actor_reference="other-operator")
    elif field == "idempotency_key":
        value = "another-attempt"
    else:
        value = getattr(original, field) + timedelta(seconds=1)
    service = EngineeringGovernanceService(
        writer=object(),
        approval=manifest.approval(),
        credential="independent-secret",
        clock=lambda: manifest.valid_from,
    )
    with pytest.raises(EngineeringGovernanceDenied):
        service.prepare(replace(original, **{field: value}), credential="independent-secret")


def test_direct_authorizer_retains_legacy_cap_and_enforces_v2_cap():
    from dataclasses import replace
    from datetime import timedelta

    from app.composition.school_fact_engineering_governance import (
        EngineeringCommandAuthorizer,
        EngineeringGovernanceService,
        governance_command_digest,
    )

    manifest = EngineeringManifest.model_validate(grant_document())
    command, approval = manifest.materialize()[0], manifest.approval()
    service = EngineeringGovernanceService(
        writer=object(),
        approval=approval,
        credential="independent-secret",
        clock=lambda: manifest.valid_from,
    )
    assert not EngineeringCommandAuthorizer(
        approval=replace(approval, grant_valid_to=None), clock=lambda: manifest.valid_from
    ).authorize(service._request(command))
    command = replace(command, planned_valid_to=command.planned_valid_to + timedelta(seconds=1))
    approval = replace(approval, command_digests=frozenset({governance_command_digest(command)}))
    assert not EngineeringCommandAuthorizer(
        approval=approval, clock=lambda: manifest.valid_from
    ).authorize(service._request(command))


def test_repeat_confirmation_retains_original_absolute_grant_period():
    from datetime import timedelta

    from app.composition.school_fact_engineering_governance import EngineeringGovernanceService

    manifest = EngineeringManifest.model_validate(grant_document())
    original = manifest.materialize()[0]
    time, seen = [manifest.valid_from], []

    class Writer:
        def create_grant(self, command):
            seen.append(command)
            return "original-receipt"

    service = EngineeringGovernanceService(
        writer=Writer(),
        approval=manifest.approval(),
        credential="independent-secret",
        clock=lambda: time[0],
    )
    prepared = service.prepare(original, credential="independent-secret")
    assert (
        service.confirm(prepared.confirmation_token, credential="independent-secret")
        == "original-receipt"
    )
    time[0] += timedelta(minutes=5)
    again = service.prepare(original, credential="independent-secret")
    assert (
        service.confirm(again.confirmation_token, credential="independent-secret")
        == "original-receipt"
    )
    assert seen == [original, original]
    assert all(c.planned_valid_to == manifest.grant_valid_to for c in seen)
