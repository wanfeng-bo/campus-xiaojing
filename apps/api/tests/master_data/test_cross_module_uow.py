from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import NoReturn
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.platform.audit.contracts import BusinessAuditRecord, BusinessAuditSummary
from app.platform.contracts import (
    AssignmentId,
    CommandExecutionContext,
    PersonId,
    SchoolId,
    StaffActorContext,
    _issue_staff_actor_context,  # pyright: ignore[reportPrivateUsage]
)
from app.platform.identity.execution_context import compose_command_context
from app.platform.organization.application import (
    ConfiguredStaffDutyBindingAuthorizer,
    ConfirmStaffDutyBindingHandler,
    StaffDutyBindingLegacyWriterDisabled,
)
from app.platform.organization.contracts import ConfirmStaffDutyBindingCommand


class _FailIfOpenedFactory:
    def __init__(self) -> None:
        self.command_open_count = 0
        self.probe_open_count = 0

    def open_command(self, context: CommandExecutionContext) -> NoReturn:
        self.command_open_count += 1
        raise AssertionError(f"disabled handler opened command UoW: {context.request_id}")

    def open_probe(self, context: CommandExecutionContext) -> NoReturn:
        self.probe_open_count += 1
        raise AssertionError(f"disabled handler opened probe UoW: {context.request_id}")


def _command() -> ConfirmStaffDutyBindingCommand:
    return ConfirmStaffDutyBindingCommand.model_validate(
        {
            "person_id": uuid4(),
            "organization_unit_id": uuid4(),
            "position_title": "水电维修工",
            "semantic_role_code": "repair_worker",
            "responsibility_group_id": uuid4(),
            "interval": {
                "valid_from": datetime.now(UTC).replace(microsecond=0) + timedelta(days=1),
                "valid_to": None,
            },
            "expected_person_version": 1,
            "expected_organization_unit_version": 1,
            "expected_group_version": 1,
            "confirmation_reason": "staff-appointment-record-001",
        }
    )


def _context() -> CommandExecutionContext:
    actor: StaffActorContext = _issue_staff_actor_context(
        account_user_id=7,
        school_id=SchoolId(uuid4()),
        person_id=PersonId(uuid4()),
        assignment_id=AssignmentId(uuid4()),
        assignment_version=1,
        authorization_version=1,
    )
    return compose_command_context(actor, uuid4(), "legacy-staff-duty-disabled")


def test_command_remains_a_strict_legacy_card_without_authority_fields() -> None:
    command = _command()
    assert set(command.model_dump()) == {
        "person_id",
        "organization_unit_id",
        "position_title",
        "semantic_role_code",
        "responsibility_group_id",
        "interval",
        "expected_person_version",
        "expected_organization_unit_version",
        "expected_group_version",
        "confirmation_reason",
    }
    assert not {
        "school_id",
        "actor_id",
        "assignment_id",
        "idempotency_key",
        "permission_granted",
        "confirmed",
    }.intersection(command.model_dump())
    with pytest.raises(ValidationError):
        ConfirmStaffDutyBindingCommand.model_validate(
            {**command.model_dump(), "school_id": uuid4()}
        )


def test_absent_assignment_audit_state_cannot_claim_a_version_or_become_after_state() -> None:
    with pytest.raises(ValidationError):
        BusinessAuditSummary(
            state_code="assignment.absent",
            resource_version=1,
        )
    absent = BusinessAuditSummary(state_code="assignment.absent")
    with pytest.raises(ValidationError):
        BusinessAuditRecord(
            event_type="master_data.assignment.changed",
            target_type="staff_assignment",
            target_id=uuid4(),
            before_summary=BusinessAuditSummary(state_code="assignment.confirmed"),
            after_summary=absent,
            reason_code="manual.assignment-confirmed",
        )


def test_disabled_legacy_handler_rejects_before_opening_any_uow() -> None:
    factory = _FailIfOpenedFactory()
    handler = ConfirmStaffDutyBindingHandler(
        uow_factory=factory,
        authorizer=ConfiguredStaffDutyBindingAuthorizer(frozenset({"school_master_data_manager"})),
    )

    with pytest.raises(StaffDutyBindingLegacyWriterDisabled, match="已机械禁用"):
        handler.handle(_command(), context=_context())

    assert factory.command_open_count == 0
    assert factory.probe_open_count == 0


def test_disabled_legacy_handler_does_not_consult_the_old_authorizer() -> None:
    class _FailIfAuthorized:
        def is_allowed(self, facts: object) -> NoReturn:
            raise AssertionError(f"disabled handler consulted old authorizer: {facts!r}")

    handler = ConfirmStaffDutyBindingHandler(
        uow_factory=_FailIfOpenedFactory(),
        authorizer=_FailIfAuthorized(),
    )

    with pytest.raises(StaffDutyBindingLegacyWriterDisabled, match="Canonical"):
        handler.handle(_command(), context=_context())


def test_disabled_legacy_private_execute_cannot_reach_the_retained_write_body() -> None:
    factory = _FailIfOpenedFactory()
    handler = ConfirmStaffDutyBindingHandler(
        uow_factory=factory,
        authorizer=ConfiguredStaffDutyBindingAuthorizer(frozenset({"school_master_data_manager"})),
    )
    command = _command()
    context = _context()
    actor = context.actor
    assert type(actor) is StaffActorContext
    identity = handler._idempotency_identity(  # pyright: ignore[reportPrivateUsage]
        command, context, actor
    )
    identifiers = handler._derive_ids(  # pyright: ignore[reportPrivateUsage]
        context, actor
    )

    with pytest.raises(StaffDutyBindingLegacyWriterDisabled, match="Canonical"):
        handler._execute(  # pyright: ignore[reportPrivateUsage]
            command,
            context,
            actor,
            identity,
            identifiers,
        )

    assert factory.command_open_count == 0
    assert factory.probe_open_count == 0
