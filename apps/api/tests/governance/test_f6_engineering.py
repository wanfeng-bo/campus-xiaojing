from dataclasses import replace
from datetime import UTC, datetime, timedelta
from importlib import import_module
from uuid import UUID

import pytest

T0 = datetime(2026, 9, 22, tzinfo=UTC)
SCHOOL, STAFF = UUID(int=1), UUID(int=2)


def api():
    return import_module("app.composition.school_fact_f6_engineering_cli")


def foundation():
    m = api()
    return m.build_duties_and_scopes(
        school_id=SCHOOL,
        actor_reference="independent-operator",
        approval_reference="f6-p3",
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
                creation_keys=("f6:dorm_bed:one",),
                field_names=("display_name", "room_source_key"),
            ),
        ),
    )


def test_f6_manifest_pins_own_actor_scope_digest_and_fixed_deadline():
    m = api()
    manifest = foundation()
    auth = m.F6EngineeringCommandAuthorizer(
        approval=manifest.approval(), clock=lambda: T0, approval_is_current=lambda: True
    )
    commands = manifest.materialize()
    assert manifest.manifest_version == "f6_governance_v1"
    assert all(auth.authorize_command(c) for c in commands)
    assert commands[-1].schema_owner == "school_facts.f6"
    assert commands[-1].schema_name == "exact_residence_fact_set"
    assert not auth.authorize_command(replace(commands[0], display_name="changed"))
    assert not auth.authorize_command(
        replace(commands[0], actor=replace(commands[0].actor, actor_kind="f5_engineering"))
    )
    assert not m.F6EngineeringCommandAuthorizer(
        approval=manifest.approval(),
        clock=lambda: T0 + timedelta(minutes=10),
        approval_is_current=lambda: True,
    ).authorize_command(commands[0])
    with pytest.raises(ValueError):
        m.EngineeringManifest.model_validate(
            {**manifest.model_dump(), "grant_valid_to": T0 + timedelta(days=8)}
        )


@pytest.mark.parametrize(
    "family,kind",
    [
        ("f2", "official_contact"),
        ("f3", "assignment"),
        ("f4", "education_class"),
        ("f5", "teaching_assignment"),
    ],
)
def test_unrelated_dependency_maintenance_is_not_inherited(family, kind):
    with pytest.raises(ValueError):
        api().ScopeMatrixRow(
            key="bad",
            family=family,
            fact_kind=kind,
            action="maintain",
            object_keys=("old:one",),
            field_names=("display_name",),
            approved_dependency_sources=("old:one",),
        )


def test_dependency_read_requires_explicit_exact_review_and_read_cannot_create():
    m = api()
    with pytest.raises(ValueError):
        m.ScopeMatrixRow(
            key="dep",
            family="f3",
            fact_kind="student",
            action="read",
            object_keys=("student:one",),
            field_names=("status",),
        )
    row = m.ScopeMatrixRow(
        key="dep",
        family="f3",
        fact_kind="student",
        action="read",
        object_keys=("student:one",),
        field_names=("status",),
        approved_dependency_sources=("student:one",),
    )
    assert row.scope(SCHOOL).object_keys == ("student:one",)
    with pytest.raises(ValueError):
        m.ScopeMatrixRow(
            key="dep",
            family="f6",
            fact_kind="dorm_bed",
            action="read",
            creation_keys=("f6:dorm_bed:one",),
            field_names=("display_name",),
        )


def grants():
    from app.platform.governance.contracts import CreateDutyCommand, GovernanceWriteResult

    m = api()

    class Receipts:
        def receipt(self, command, *, credential):
            return GovernanceWriteResult(
                UUID(int=10 if isinstance(command, CreateDutyCommand) else 11), 1, None, False
            )

    manifest = foundation()
    return m.build_grants(
        manifest,
        [
            m.GrantBinding(
                key="bed-grant",
                assignment_id=STAFF,
                duty_key="f6_dorm_bed_maintainer",
                scope_key="beds",
            )
        ],
        grant_valid_from=T0 + timedelta(days=1),
        execution_valid_to=T0 + timedelta(days=1, minutes=10),
        service=Receipts(),
        credential="unit-secret",
    )


def test_grants_use_receipts_and_never_restart_seven_day_epoch():
    g = grants()
    (command,) = g.materialize()
    assert command.duty_id == UUID(int=10)
    assert command.scope_id == UUID(int=11)
    assert command.planned_valid_to == T0 + timedelta(days=7)
    assert command.basis_authority == "f6_engineering_policy"
    assert g.grant_epoch == T0
    assert api().EngineeringManifest.model_validate_json(g.model_dump_json()) == g

    class Missing:
        def receipt(self, *args, **kwargs):
            return None

    with pytest.raises(ValueError):
        api().build_grants(
            foundation(),
            [
                api().GrantBinding(
                    key="missing",
                    assignment_id=STAFF,
                    duty_key="f6_dorm_bed_maintainer",
                    scope_key="beds",
                )
            ],
            grant_valid_from=T0,
            execution_valid_to=T0 + timedelta(minutes=10),
            service=Missing(),
            credential="unit",
        )


def test_grant_transaction_requires_current_binding_and_governance_session():
    from app.composition.school_fact_f6_engineering_governance import F6EngineeringGovernanceService

    manifest = grants()
    service = F6EngineeringGovernanceService(
        writer=object(),
        approval=manifest.approval(),
        credential="unit-secret-credential",
        clock=lambda: T0 + timedelta(days=1),
        approval_is_current=lambda: True,
    )
    request = service._request(manifest.materialize()[0])

    class Session:
        role = "campus_ai_governance_writer"

        def session_user(self):
            return self.role

    state = [True]
    auth = api().F6EngineeringCommandAuthorizer(
        approval=manifest.approval(),
        clock=lambda: T0 + timedelta(days=1),
        approval_is_current=lambda: True,
        grant_reference_is_current=lambda proof: state[0],
    )
    session = Session()
    auth.authorize_session(session, request)
    state[0] = False
    with pytest.raises(PermissionError):
        auth.authorize_session(session, request)
    state[0] = True
    session.role = "campus_ai_school_fact_writer"
    with pytest.raises(PermissionError):
        auth.authorize_session(session, request)
    (proof,) = manifest.approval().grant_references
    scope = foundation().commands[-1].scope(SCHOOL)
    args = dict(
        school_id=SCHOOL,
        actual_duty_code=proof.duty_code,
        actual_owner=proof.scope_owner,
        actual_document=scope.document,
        actual_sha256=scope.document_sha256,
    )
    assert api().grant_reference_matches(proof, **args)
    assert not api().grant_reference_matches(
        proof, **(args | {"actual_duty_code": "f6_dorm_bed_reader"})
    )
    assert not api().grant_reference_matches(proof, **(args | {"school_id": UUID(int=999)}))


def test_closed_after_prepare_never_dispatches_but_receipt_remains_readable():
    from app.composition.school_fact_f6_engineering_governance import F6EngineeringGovernanceService
    from app.platform.governance.contracts import GovernanceWriteResult

    state = [True]
    writes = []
    result = GovernanceWriteResult(UUID(int=9), 1, None, False)

    class Writer:
        def create_duty(self, c):
            writes.append(c)
            return result

        def lookup_command_result(self, c, *, command_name):
            return result

    manifest = foundation()
    service = F6EngineeringGovernanceService(
        writer=Writer(),
        approval=manifest.approval(),
        credential="unit-secret-credential",
        clock=lambda: T0,
        approval_is_current=lambda: state[0],
    )
    command = manifest.materialize()[0]
    prepared = service.prepare(command, credential="unit-secret-credential")
    state[0] = False
    with pytest.raises(RuntimeError):
        service.confirm(prepared.confirmation_token, credential="unit-secret-credential")
    assert writes == []
    assert service.receipt(command, credential="unit-secret-credential") == result


@pytest.mark.parametrize(
    "family,kind,field",
    [
        ("f3", "student_school_relationship", "residence_mode"),
        ("f4", "student_class_membership", "valid_to"),
        ("f4", "student_class_position", "valid_to"),
        ("f5", "teaching_group_member", "valid_to"),
    ],
)
def test_only_exact_reviewed_lifecycle_dependencies_can_be_maintained(family, kind, field):
    m = api()
    row = m.ScopeMatrixRow(
        key="dependent",
        family=family,
        fact_kind=kind,
        action="maintain",
        object_keys=("old:one",),
        field_names=(field,),
        approved_dependency_sources=("old:one",),
    )
    assert row.scope(SCHOOL).object_keys == ("old:one",)
    with pytest.raises(ValueError):
        m.ScopeMatrixRow.model_validate({**row.model_dump(), "object_keys": ("old:one", "old:two")})
    with pytest.raises(ValueError):
        m.ScopeMatrixRow.model_validate({**row.model_dump(), "creation_keys": ("f6:new:one",)})


def test_v5_manifest_and_f6_manifest_do_not_interchange():
    from app.composition.school_fact_f5_engineering_cli import EngineeringManifest as OldManifest

    with pytest.raises(ValueError):
        OldManifest.model_validate(foundation().model_dump())
    with pytest.raises(ValueError):
        api().EngineeringManifest.model_validate(
            {**foundation().model_dump(), "manifest_version": "f5_governance_v1"}
        )


def test_quality_owners_bind_only_f6_correct_owner_and_hash():
    m = api()
    row = m.ScopeMatrixRow(
        key="owner",
        family="f6",
        fact_kind="student_bed_residence",
        action="read",
        field_names=("student_name",),
        quality_owners=(("person", "person:one"),),
    )
    scope = row.scope(SCHOOL)
    assert scope.document["quality_owners"] == [["person", "person:one"]]
    with pytest.raises(ValueError):
        m.ScopeMatrixRow.model_validate({**row.model_dump(), "family": "f3"})
    with pytest.raises(ValueError):
        m.ScopeMatrixRow.model_validate(
            {**row.model_dump(), "quality_owners": (("location", "location:one"),)}
        ).scope(SCHOOL)
    plain = m.ScopeMatrixRow(
        key="plain",
        family="f6",
        fact_kind="dorm_bed",
        action="read",
        object_keys=("bed:one",),
        field_names=("display_name",),
    ).scope(SCHOOL)
    assert "quality_owners" not in plain.document
