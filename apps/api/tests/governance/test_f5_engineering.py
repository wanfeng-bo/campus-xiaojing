from dataclasses import replace
from datetime import UTC, datetime, timedelta
from importlib import import_module
from importlib.util import find_spec
from uuid import UUID, uuid4

import pytest

T0 = datetime(2026, 9, 13, 18, tzinfo=UTC)
SCHOOL, STAFF = UUID(int=1), UUID(int=2)


def api():
    name = "app.composition.school_fact_f5_engineering_cli"
    assert find_spec(name) is not None, "F5 finite engineering adapter is absent"
    return import_module(name)


def foundation():
    m = api()
    return m.build_duties_and_scopes(
        school_id=SCHOOL,
        actor_reference="independent-f5-operator",
        approval_reference="user-f5-approved-20260913",
        valid_from=T0,
        valid_to=T0 + timedelta(minutes=10),
        grant_epoch=T0,
        approved_assignment_ids=frozenset({STAFF}),
        rows=(
            m.ScopeMatrixRow(
                key="class",
                family="f5",
                fact_kind="teaching_course",
                action="maintain",
                creation_keys=("f5:teaching_course:a",),
                field_names=("display_name",),
            ),
            m.ScopeMatrixRow(
                key="student",
                family="f3",
                fact_kind="student",
                action="read",
                object_keys=("f3:student:a",),
                field_names=("status",),
            ),
        ),
    )


def test_f5_exact_approval_excludes_changed_actor_fields_and_stale_approval():
    manifest = foundation()
    g = import_module("app.composition.school_fact_f5_engineering_governance")
    auth = g.F5EngineeringCommandAuthorizer(
        approval=manifest.approval(),
        clock=lambda: T0,
        approval_is_current=lambda: True,
    )
    commands = manifest.materialize()
    assert all(auth.authorize_command(c) for c in commands)
    assert not auth.authorize_command(replace(commands[0], display_name="changed"))
    assert not auth.authorize_command(
        replace(commands[0], actor=replace(commands[0].actor, actor_kind="f3_engineering"))
    )
    assert not g.F5EngineeringCommandAuthorizer(
        approval=manifest.approval(),
        clock=lambda: T0,
        approval_is_current=lambda: False,
    ).authorize_command(commands[0])


def test_legacy_dependency_never_accepts_maintenance_or_creation():
    m = api()
    for family, kind in (("f2", "location"), ("f3", "student")):
        with pytest.raises(ValueError):
            m.ScopeMatrixRow(
                key="bad",
                family=family,
                fact_kind=kind,
                action="maintain",
                object_keys=("f5:dep:a",),
                field_names=("status",),
            )
        with pytest.raises(ValueError):
            m.ScopeMatrixRow(
                key="bad",
                family=family,
                fact_kind=kind,
                action="read",
                creation_keys=("f5:dep:a",),
                field_names=("status",),
            )


def test_grant_receipts_bind_actual_ids_and_fixed_deadline_without_new_assignments():
    from app.platform.governance.contracts import CreateDutyCommand, GovernanceWriteResult

    m, manifest = api(), foundation()
    duty_id, scope_id = uuid4(), uuid4()

    class Receipts:
        def receipt(self, c, *, credential):
            return GovernanceWriteResult(
                duty_id if isinstance(c, CreateDutyCommand) else scope_id, 1, None, False
            )

    binding = m.GrantBinding(
        key="read-student", assignment_id=STAFF, duty_key="f3_student_reader", scope_key="student"
    )
    later = T0 + timedelta(days=1)
    result = m.build_grants(
        manifest,
        [binding],
        grant_valid_from=later,
        execution_valid_to=later + timedelta(minutes=10),
        service=Receipts(),
        credential="private",
    )
    grant = result.materialize()[0]
    assert grant.duty_id == duty_id and grant.scope_id == scope_id
    assert grant.valid_from == later
    assert grant.planned_valid_to == T0 + timedelta(days=7)
    assert result.grant_epoch == T0
    with pytest.raises(ValueError):
        m.build_grants(
            manifest,
            [binding.model_copy(update={"assignment_id": uuid4()})],
            grant_valid_from=later,
            execution_valid_to=later + timedelta(minutes=10),
            service=Receipts(),
            credential="private",
        )


def test_verified_existing_duty_is_reused_but_new_scope_receipt_is_still_required():
    from app.platform.governance.contracts import CreateDutyCommand, GovernanceWriteResult

    m = api()
    prior = foundation()
    duty_id, scope_id = uuid4(), uuid4()
    existing = m.ExistingDutyReference(
        duty_code="f3_student_reader", duty_id=duty_id, duty_version=2
    )
    manifest = m.build_duties_and_scopes(
        school_id=SCHOOL,
        actor_reference=prior.actor_reference,
        approval_reference=prior.approval_reference,
        valid_from=T0,
        valid_to=T0 + timedelta(minutes=10),
        grant_epoch=T0,
        approved_assignment_ids=frozenset({STAFF}),
        rows=tuple(c for c in prior.commands if isinstance(c, m.ScopeInput)),
        existing_duties=(existing,),
    )
    assert not any(
        isinstance(c, CreateDutyCommand) and c.duty_code == existing.duty_code
        for c in manifest.materialize()
    )

    class Receipts:
        def receipt(self, command, *, credential):
            assert not isinstance(command, CreateDutyCommand)
            return GovernanceWriteResult(scope_id, 1, None, False)

    binding = m.GrantBinding(
        key="use-existing", assignment_id=STAFF, duty_key=existing.duty_code, scope_key="student"
    )
    result = m.build_grants(
        manifest,
        [binding],
        grant_valid_from=T0,
        execution_valid_to=T0 + timedelta(minutes=10),
        service=Receipts(),
        credential="unit",
    )
    grant = result.materialize()[0]
    assert (grant.duty_id, grant.duty_version, grant.scope_id) == (duty_id, 2, scope_id)
    assert result.approval().grant_references[0].duty_id == duty_id


def test_window_fixed_epoch_and_missing_receipt_fail_closed():
    m, manifest = api(), foundation()
    for update in (
        {"valid_to": T0 + timedelta(minutes=16)},
        {"grant_valid_to": T0 + timedelta(days=7, seconds=1)},
        {"approved_duty_codes": frozenset({"f2_location_maintainer"})},
    ):
        with pytest.raises(ValueError):
            m.EngineeringManifest.model_validate({**manifest.model_dump(), **update})

    class Missing:
        def receipt(self, *args, **kwargs):
            return None

    with pytest.raises(ValueError):
        m.build_grants(
            manifest,
            [
                m.GrantBinding(
                    key="read-student",
                    assignment_id=STAFF,
                    duty_key="f3_student_reader",
                    scope_key="student",
                )
            ],
            grant_valid_from=T0,
            execution_valid_to=T0 + timedelta(minutes=10),
            service=Missing(),
            credential="private",
        )


def test_closed_after_prepare_blocks_write_but_original_receipt_remains_available():
    from app.platform.governance.contracts import GovernanceWriteResult

    manifest = foundation()
    g = import_module("app.composition.school_fact_f5_engineering_governance")
    current, writes = [True], []
    expected = GovernanceWriteResult(uuid4(), 1, None, False)

    class Writer:
        def create_duty(self, c):
            writes.append(c)
            return expected

        def lookup_command_result(self, c, *, command_name):
            return expected

    service = g.F5EngineeringGovernanceService(
        writer=Writer(),
        approval=manifest.approval(),
        credential="independent-secret-credential",
        clock=lambda: T0,
        approval_is_current=lambda: current[0],
    )
    command = manifest.materialize()[0]
    prepared = service.prepare(command, credential="independent-secret-credential")
    current[0] = False
    with pytest.raises(RuntimeError, match="no longer approved"):
        service.confirm(prepared.confirmation_token, credential="independent-secret-credential")
    assert writes == []
    assert service.receipt(command, credential="independent-secret-credential") == expected


def test_grant_transaction_rejects_a_disguised_actual_maintainer_binding():
    from app.platform.governance.contracts import CreateDutyCommand, GovernanceWriteResult

    m, manifest = api(), foundation()
    ids = (uuid4(), uuid4())

    class Receipts:
        def receipt(self, c, *, credential):
            return GovernanceWriteResult(
                ids[0 if isinstance(c, CreateDutyCommand) else 1], 1, None, False
            )

    grants = m.build_grants(
        manifest,
        [
            m.GrantBinding(
                key="read", assignment_id=STAFF, duty_key="f3_student_reader", scope_key="student"
            )
        ],
        grant_valid_from=T0,
        execution_valid_to=T0 + timedelta(minutes=10),
        service=Receipts(),
        credential="private",
    )
    g = import_module("app.composition.school_fact_f5_engineering_governance")
    service = g.F5EngineeringGovernanceService(
        writer=Receipts(),
        approval=grants.approval(),
        credential="independent-secret-credential",
        clock=lambda: T0,
        approval_is_current=lambda: True,
    )
    request = service._request(grants.materialize()[0])

    class Session:
        def session_user(self):
            return "campus_ai_governance_writer"

    authorizer = g.F5EngineeringCommandAuthorizer(
        approval=grants.approval(),
        clock=lambda: T0,
        approval_is_current=lambda: True,
        grant_reference_is_current=lambda p: False,
    )
    with pytest.raises(PermissionError):
        authorizer.authorize_mutation(Session(), request)


def test_real_reference_comparison_checks_duty_family_and_scope_document():
    m = api()
    assert hasattr(m, "grant_reference_matches"), "Authoritative grant reference comparison missing"
    from app.platform.school_facts.f3_policy import F3FactScope

    g = import_module("app.composition.school_fact_f5_engineering_governance")
    scope = F3FactScope(SCHOOL, "student", ("f3:student:a",), (), ("status",))
    proof = g.GrantReferenceProof(
        uuid4(), 1, "f3_student_reader", uuid4(), 1, "school_facts.f3", scope.document_sha256
    )
    assert m.grant_reference_matches(
        proof,
        school_id=SCHOOL,
        actual_duty_code="f3_student_reader",
        actual_owner="school_facts.f3",
        actual_document=scope.document,
        actual_sha256=scope.document_sha256,
    )
    assert not m.grant_reference_matches(
        proof,
        school_id=SCHOOL,
        actual_duty_code="f3_student_maintainer",
        actual_owner="school_facts.f3",
        actual_document=scope.document,
        actual_sha256=scope.document_sha256,
    )

    assert not m.grant_reference_matches(
        proof,
        school_id=UUID(int=9),
        actual_duty_code="f3_student_reader",
        actual_owner="school_facts.f3",
        actual_document=scope.document,
        actual_sha256=scope.document_sha256,
    )


def test_only_explicit_f5_owner_dependency_maintenance_exceptions_are_accepted():
    m = api()
    school_relationship = m.ScopeMatrixRow(
        key="graduation",
        family="f3",
        fact_kind="student_school_relationship",
        action="maintain",
        object_keys=("f3:enrollment:a",),
        field_names=("enrollment_status",),
    )
    assert school_relationship.scope(SCHOOL).object_keys == ("f3:enrollment:a",)
    for kind in ("policy_scope", "fact_quality"):
        row = m.ScopeMatrixRow(
            key=kind,
            family="f2",
            fact_kind=kind,
            action="maintain",
            creation_keys=(f"f5:{kind}:class-a",),
            field_names=("education_target_kind",),
        )
        assert row.scope(SCHOOL).creation_keys == (f"f5:{kind}:class-a",)
        with pytest.raises(ValueError):
            m.ScopeMatrixRow(
                key=kind,
                family="f2",
                fact_kind=kind,
                action="maintain",
                object_keys=(f"f2:{kind}:old",),
                field_names=("education_target_kind",),
            )
    with pytest.raises(ValueError):
        m.ScopeMatrixRow(
            key="new-enrollment",
            family="f3",
            fact_kind="student_school_relationship",
            action="maintain",
            creation_keys=("f5:enrollment:new",),
            field_names=("enrollment_status",),
        )


def test_f4_dependency_stays_its_own_typed_owner_and_new_f5_scope_cannot_masquerade():
    m = api()
    row = m.ScopeMatrixRow(
        key="class-read",
        family="f4",
        fact_kind="education_class",
        action="read",
        object_keys=("f4:class:old",),
        field_names=("display_name",),
    )
    manifest = m.build_duties_and_scopes(
        school_id=SCHOOL,
        actor_reference="f5-op",
        approval_reference="approved",
        valid_from=T0,
        valid_to=T0 + timedelta(minutes=10),
        grant_epoch=T0,
        approved_assignment_ids=frozenset({STAFF}),
        rows=(row,),
    )
    scopes = [c for c in manifest.materialize() if hasattr(c, "schema_owner")]
    assert scopes[0].schema_owner == "school_facts.f4"
    assert scopes[0].schema_name == "exact_education_fact_set"
    g = import_module("app.composition.school_fact_f5_engineering_governance")
    assert g.F5EngineeringCommandAuthorizer(
        approval=manifest.approval(), clock=lambda: T0, approval_is_current=lambda: True
    ).authorize_command(scopes[0])
    with pytest.raises(ValueError):
        m.ScopeMatrixRow(
            key="bad",
            family="f4",
            fact_kind="teaching_course",
            action="read",
            object_keys=("f5:course:one",),
            field_names=("display_name",),
        ).scope(SCHOOL)


def test_same_command_replay_rechecks_window_identity_and_current_approval():
    g = import_module("app.composition.school_fact_f5_engineering_governance")
    manifest = foundation()
    now = [T0]
    current = [True]
    auth = g.F5EngineeringCommandAuthorizer(
        approval=manifest.approval(), clock=lambda: now[0], approval_is_current=lambda: current[0]
    )
    command = manifest.materialize()[0]
    assert auth.authorize_command(command)
    now[0] = manifest.valid_to
    assert not auth.authorize_command(command)
    now[0] = T0
    current[0] = False
    assert not auth.authorize_command(command)
    current[0] = True
    assert not auth.authorize_command(
        replace(command, actor=replace(command.actor, school_id=UUID(int=99)))
    )


def test_original_f4_manifest_does_not_accept_f5_duty_or_scope():
    old = import_module("app.composition.school_fact_f4_engineering_cli")
    with pytest.raises(ValueError):
        old.ScopeMatrixRow(
            key="f5",
            family="f5",
            fact_kind="teaching_course",
            action="read",
            object_keys=("one",),
            field_names=("display_name",),
        )


def test_existing_f4_linked_maintenance_requires_exact_pinned_scope_digest():
    m = api()
    from app.platform.school_facts.f4_policy import F4FactScope

    keys = ("f4:membership:a",)
    fields = (
        "student_source_key",
        "class_source_key",
        "school_relationship_source_key",
        "membership_status",
    )
    scope = F4FactScope(SCHOOL, "student_class_membership", keys, (), fields)
    values = dict(
        key="original-linked",
        family="f4",
        fact_kind="student_class_membership",
        action="maintain",
        object_keys=keys,
        field_names=fields,
    )
    with pytest.raises(ValueError):
        m.ScopeMatrixRow(**values)
    with pytest.raises(ValueError):
        m.ScopeMatrixRow(**values, prior_scope_sha256="0" * 64).scope(SCHOOL)
    assert (
        m.ScopeMatrixRow(**values, prior_scope_sha256=scope.document_sha256).scope(SCHOOL).document
        == scope.document
    )


def test_f5_organization_dependency_maintenance_is_exact_new_source_only():
    m = api()
    for kind in ("organization_membership", "organization_campus_relationship"):
        row = m.ScopeMatrixRow(
            key=kind,
            family="f2",
            fact_kind=kind,
            action="maintain",
            creation_keys=(f"f5:{kind}:one",),
            field_names=("organization_source_key",),
        )
        assert row.scope(SCHOOL).fact_kind == kind
        with pytest.raises(ValueError):
            m.ScopeMatrixRow(
                key=kind,
                family="f2",
                fact_kind=kind,
                action="maintain",
                creation_keys=(f"f4:{kind}:one",),
                field_names=("organization_source_key",),
            )


def test_reviewed_f2_dependency_union_is_explicit_without_fake_prior_scope_proof():
    m = api()
    keys = ("f4:assignment:a:current", "f5:assignment:zhou")
    row = m.ScopeMatrixRow(
        key="required-staff",
        family="f2",
        fact_kind="assignment",
        action="read",
        object_keys=keys,
        field_names=("person_source_key",),
        approved_dependency_sources=keys,
    )
    assert row.scope(SCHOOL).object_keys == keys
    for invalid in (
        {"approved_dependency_sources": keys[:1]},
        {"approved_dependency_sources": keys, "prior_scope_sha256": "0" * 64},
    ):
        with pytest.raises(ValueError):
            m.ScopeMatrixRow(
                key="required-staff",
                family="f2",
                fact_kind="assignment",
                action="read",
                object_keys=keys,
                field_names=("person_source_key",),
                **invalid,
            )


@pytest.mark.parametrize("kind", ["person", "assignment"])
def test_f5_leadership_f2_maintenance_has_its_own_exact_new_scope(kind):
    m = api()
    row = m.ScopeMatrixRow(
        key="leader",
        family="f2",
        fact_kind=kind,
        action="maintain",
        object_keys=(f"f5:{kind}:leader",),
        field_names=("display_name" if kind == "person" else "position_title",),
    )
    assert row.duty_code == f"f2_{kind}_maintainer"
    assert (
        row.duty_code
        in import_module("app.composition.school_fact_f5_engineering_governance").F5_ALLOWED_DUTIES
    )
    with pytest.raises(ValueError):
        m.ScopeMatrixRow(
            key="legacy",
            family="f2",
            fact_kind=kind,
            action="maintain",
            object_keys=(f"f3:{kind}:old",),
            field_names=row.field_names,
        )


def test_reviewed_f4_linked_subset_does_not_claim_the_old_superset_hash():
    m = api()
    keys = ("f4:membership:a",)
    row = m.ScopeMatrixRow(
        key="linked-subset",
        family="f4",
        fact_kind="student_class_membership",
        action="maintain",
        object_keys=keys,
        field_names=("class_source_key",),
        approved_dependency_sources=keys,
    )
    assert row.prior_scope_sha256 is None and row.scope(SCHOOL).object_keys == keys
    with pytest.raises(ValueError):
        m.ScopeMatrixRow(
            key="new",
            family="f4",
            fact_kind="student_class_membership",
            action="maintain",
            creation_keys=("old:new",),
            field_names=("class_source_key",),
            approved_dependency_sources=("old:new",),
        )
