from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from app.composition.school_fact_f3_engineering_cli import (
    EngineeringManifest,
    GrantBinding,
    GrantInput,
    ScopeMatrixRow,
    build_duties_and_scopes,
    build_grants,
)
from app.composition.school_fact_f3_engineering_governance import (
    F3EngineeringCommandAuthorizer,
    F3EngineeringGovernanceService,
)

T0 = datetime(2026, 9, 13, 6, tzinfo=UTC)
SCHOOL = UUID(int=1)
MAINTAINER = UUID(int=2)


def foundation_manifest() -> EngineeringManifest:
    return build_duties_and_scopes(
        school_id=SCHOOL,
        actor_reference="f3-independent-operator",
        valid_from=T0,
        valid_to=T0 + timedelta(minutes=10),
        grant_valid_to=T0 + timedelta(days=7),
        approved_assignment_ids=frozenset({MAINTAINER}),
        rows=(
            ScopeMatrixRow(
                key="person-create",
                fact_kind="person",
                action="maintain",
                creation_keys=("f3:person:guardian",),
                field_names=("display_name", "person_kind", "lifecycle_status"),
            ),
            ScopeMatrixRow(
                key="guardian-request",
                fact_kind="guardian_relationship",
                action="request",
                creation_keys=("f3:guardian:guardian-student-a",),
                field_names=(
                    "guardian_person_source_key",
                    "student_source_key",
                    "relationship_kind",
                    "access_purpose",
                    "verification_source",
                    "verification_reference",
                    "status",
                ),
            ),
        ),
    )


def test_f3_manifest_materializes_exact_fields_and_f3_authorizer() -> None:
    manifest = foundation_manifest()
    commands = manifest.materialize()
    scope = next(c for c in commands if getattr(c, "scope_key", "") == "guardian-request")
    assert scope.schema_owner == "school_facts.f3"
    assert scope.schema_name == "exact_family_fact_set"
    assert scope.reference_document["field_names"]
    approval = manifest.approval()
    authorizer = F3EngineeringCommandAuthorizer(
        approval=approval,
        clock=lambda: T0,
        approval_is_current=lambda: True,
    )
    assert all(authorizer.authorize_command(command) for command in commands)


def test_later_grant_starts_now_and_keeps_original_fixed_deadline() -> None:
    later = T0 + timedelta(days=1)
    deadline = T0 + timedelta(days=7)
    assignment = uuid4()
    document = {
        "manifest_version": "f3_governance_v1",
        "school_id": SCHOOL,
        "actor_reference": "f3-independent-operator",
        "valid_from": later,
        "valid_to": later + timedelta(minutes=10),
        "grant_valid_to": deadline,
        "approved_duty_codes": frozenset({"f3_student_reader"}),
        "approved_assignment_ids": frozenset({assignment}),
        "commands": (
            GrantInput(
                command="grant",
                key="later-reader",
                assignment_id=assignment,
                duty_id=uuid4(),
                duty_version=1,
                scope_id=uuid4(),
                scope_version=1,
                valid_from=later,
                effective_from=later,
                planned_valid_to=deadline,
            ),
        ),
    }
    manifest = EngineeringManifest.model_validate(document)
    command = manifest.materialize()[0]
    assert command.valid_from == later
    assert command.planned_valid_to == deadline
    with pytest.raises(ValidationError):
        EngineeringManifest.model_validate(
            {
                **document,
                "commands": (document["commands"][0].model_copy(update={"valid_from": T0}),),
            }
        )
    with pytest.raises(ValidationError):
        EngineeringManifest.model_validate(
            {
                **document,
                "commands": (
                    document["commands"][0].model_copy(
                        update={"planned_valid_to": deadline + timedelta(seconds=1)}
                    ),
                ),
            }
        )


def test_build_grants_uses_later_stage_time_without_extending_deadline() -> None:
    from app.platform.governance.contracts import CreateDutyCommand, GovernanceWriteResult

    manifest = foundation_manifest()
    assignment, duty_id, scope_id = uuid4(), uuid4(), uuid4()
    later = T0 + timedelta(days=1)

    class Receipts:
        def receipt(self, command, *, credential):
            return GovernanceWriteResult(
                duty_id if isinstance(command, CreateDutyCommand) else scope_id, 3, None, False
            )

    binding = GrantBinding(
        key="grant-person-create",
        assignment_id=assignment,
        duty_key="f3_person_maintainer",
        scope_key="person-create",
    )
    result = build_grants(
        manifest,
        [binding],
        grant_valid_from=later,
        execution_valid_to=later + timedelta(minutes=10),
        service=Receipts(),
        credential="private",
    )
    grant = result.commands[0]
    assert result.valid_from == later
    assert result.valid_to == later + timedelta(minutes=10)
    assert result.grant_valid_to == T0 + timedelta(days=7)
    assert grant.valid_from == later
    assert grant.planned_valid_to == T0 + timedelta(days=7)
    with pytest.raises(ValueError):
        build_grants(
            manifest,
            [binding],
            grant_valid_from=later,
            execution_valid_to=later + timedelta(minutes=10),
            fixed_grant_valid_to=T0 + timedelta(days=7, seconds=1),
            service=Receipts(),
            credential="private",
        )


def test_manifest_rejects_unapproved_assignment_duty_and_reader_creation() -> None:
    manifest = foundation_manifest()
    grant = GrantInput(
        command="grant",
        key="wrong-assignment",
        assignment_id=uuid4(),
        duty_id=uuid4(),
        duty_version=1,
        scope_id=uuid4(),
        scope_version=1,
        valid_from=T0,
        effective_from=T0,
        planned_valid_to=T0 + timedelta(days=7),
    )
    with pytest.raises(ValidationError):
        EngineeringManifest.model_validate({**manifest.model_dump(), "commands": (grant,)})
    with pytest.raises(ValueError):
        build_duties_and_scopes(
            school_id=SCHOOL,
            actor_reference="f3-independent-operator",
            valid_from=T0,
            valid_to=T0 + timedelta(minutes=10),
            grant_valid_to=T0 + timedelta(days=7),
            approved_assignment_ids=frozenset({MAINTAINER}),
            rows=(
                ScopeMatrixRow(
                    key="bad-reader",
                    fact_kind="student",
                    action="read",
                    creation_keys=("f3:student:a",),
                    field_names=("status",),
                ),
            ),
        )
    with pytest.raises(ValidationError):
        EngineeringManifest.model_validate(
            {
                **manifest.model_dump(),
                "approved_duty_codes": frozenset({"f2_student_reader"}),
            }
        )


def test_f3_service_uses_f3_payload_for_prepare_and_receipt() -> None:
    from app.platform.governance.contracts import GovernanceWriteResult

    manifest = foundation_manifest()
    command = manifest.materialize()[0]
    expected = GovernanceWriteResult(uuid4(), 1, None, False)

    class Writer:
        def lookup_command_result(self, candidate, *, command_name):
            assert candidate == command
            assert command_name == "governance.duty.create"
            return expected

    service = F3EngineeringGovernanceService(
        writer=Writer(),
        approval=manifest.approval(),
        credential="a-secure-f3-credential",
        clock=lambda: T0,
        approval_is_current=lambda: True,
    )
    prepared = service.prepare(command, credential="a-secure-f3-credential")
    assert prepared.command_sha256
    assert service.receipt(command, credential="a-secure-f3-credential") == expected
