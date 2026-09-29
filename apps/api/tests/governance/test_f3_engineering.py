from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from app.composition.school_fact_engineering_governance import EngineeringGovernanceService
from app.composition.school_fact_f3_engineering_governance import (
    F3EngineeringApproval,
    F3EngineeringCommandAuthorizer,
    f3_governance_command_digest,
)
from app.platform.governance.contracts import (
    CreateDutyCommand,
    CreateGrantCommand,
    GovernanceActorContext,
)

NOW = datetime(2026, 9, 13, 5, tzinfo=UTC)


def actor(school_id=None):
    school_id = UUID(int=1) if school_id is None else school_id
    return GovernanceActorContext(
        school_id, "f3_engineering", "issuer:fixed", None, None, uuid4(), "approved F3"
    )


def test_f3_engineering_approval_binds_exact_digest_actor_school_and_duty():
    a = actor()
    command = CreateDutyCommand(a.school_id, "f3_student_reader", "reader", None, NOW, a, "f3:one")
    approval = F3EngineeringApproval(
        a.school_id,
        a.actor_reference,
        NOW,
        NOW + timedelta(minutes=10),
        NOW + timedelta(days=7),
        frozenset({f3_governance_command_digest(command)}),
        frozenset({"f3_student_reader"}),
        frozenset({UUID(int=9)}),
    )
    auth = F3EngineeringCommandAuthorizer(
        approval=approval, clock=lambda: NOW, approval_is_current=lambda: True
    )
    assert auth.authorize_command(command)
    assert not auth.authorize_command(replace(command, duty_code="f3_student_maintainer"))
    assert not auth.authorize_command(replace(command, actor=actor(UUID(int=2))))


def test_f3_grant_is_limited_to_approved_assignment_and_seven_day_cap():
    a = actor()
    command = CreateGrantCommand(
        a.school_id,
        UUID(int=9),
        UUID(int=10),
        1,
        UUID(int=11),
        1,
        "f3_engineering_policy",
        "approval:f3",
        NOW,
        NOW,
        NOW + timedelta(days=7),
        a,
        "f3:grant",
    )
    approval = F3EngineeringApproval(
        a.school_id,
        a.actor_reference,
        NOW,
        NOW + timedelta(minutes=10),
        NOW + timedelta(days=7),
        frozenset({f3_governance_command_digest(command)}),
        frozenset({"f3_student_reader"}),
        frozenset({UUID(int=9)}),
    )
    auth = F3EngineeringCommandAuthorizer(
        approval=approval, clock=lambda: NOW, approval_is_current=lambda: True
    )
    assert auth.authorize_command(command)
    assert not auth.authorize_command(replace(command, assignment_id=UUID(int=8)))
    assert not auth.authorize_command(
        replace(command, planned_valid_to=NOW + timedelta(days=7, seconds=1))
    )
    assert not F3EngineeringCommandAuthorizer(
        approval=approval,
        clock=lambda: NOW + timedelta(minutes=10),
        approval_is_current=lambda: True,
    ).authorize_command(command)


def test_closed_execution_approval_denies_new_command():
    a = actor()
    command = CreateDutyCommand(a.school_id, "f3_student_reader", "reader", None, NOW, a, "f3:one")
    approval = F3EngineeringApproval(
        a.school_id,
        a.actor_reference,
        NOW,
        NOW + timedelta(minutes=10),
        NOW + timedelta(days=7),
        frozenset({f3_governance_command_digest(command)}),
        frozenset({"f3_student_reader"}),
        frozenset({UUID(int=9)}),
    )
    auth = F3EngineeringCommandAuthorizer(
        approval=approval, clock=lambda: NOW, approval_is_current=lambda: False
    )
    assert not auth.authorize_command(command)


def test_f3_approval_rejects_long_execution_or_grant_window():
    with pytest.raises(ValueError):
        F3EngineeringApproval(
            UUID(int=1),
            "issuer",
            NOW,
            NOW + timedelta(minutes=16),
            NOW + timedelta(days=7),
            frozenset({"0" * 64}),
            frozenset({"f3_student_reader"}),
            frozenset({UUID(int=9)}),
        )


def test_f3_reuses_short_confirm_service_with_injected_authorizer():
    a = actor()
    command = CreateDutyCommand(
        a.school_id, "f3_student_reader", "reader", None, NOW, a, "f3:service"
    )
    approval = F3EngineeringApproval(
        a.school_id,
        a.actor_reference,
        NOW,
        NOW + timedelta(minutes=10),
        NOW + timedelta(days=7),
        frozenset({f3_governance_command_digest(command)}),
        frozenset({"f3_student_reader"}),
        frozenset({UUID(int=9)}),
    )
    current = [True]
    authorizer = F3EngineeringCommandAuthorizer(
        approval=approval, clock=lambda: NOW, approval_is_current=lambda: current[0]
    )

    class Writer:
        def __init__(self):
            self.commands = []

        def create_duty(self, c):
            self.commands.append(c)
            return "receipt"

        def lookup_command_result(self, *_a, **_k):
            return "receipt"

    writer = Writer()
    service = EngineeringGovernanceService(
        writer=writer,
        approval=approval,
        credential="independent-f3-credential",
        clock=lambda: NOW,
        command_authorizer=authorizer,
    )
    prepared = service.prepare(command, credential="independent-f3-credential")
    assert (
        service.confirm(prepared.confirmation_token, credential="independent-f3-credential")
        == "receipt"
    )
    current[0] = False
    with pytest.raises(Exception, match="no longer approved"):
        service.confirm(prepared.confirmation_token, credential="independent-f3-credential")
