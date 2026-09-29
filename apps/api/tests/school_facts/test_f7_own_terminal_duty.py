"""A batch terminal needs its own live governance duty, not a fact-read fallback."""

from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.platform import contracts
from app.platform.authorization.secure_reader_contracts import ReadAuthorization, SecureReadDenied
from app.platform.authorization.thin_contracts import (
    NamedProjection,
    ScopePlan,
    ThinAuthorizationBasis,
)
from app.platform.identity.execution_context import compose_command_context
from app.platform.school_facts import f7_own_terminal_authorization as terminal_auth
from app.platform.school_facts.f7_own_terminal_authorization import (
    F7_OWN_TERMINAL_DUTY,
    F7_OWN_TERMINAL_MODULE,
    F7_OWN_TERMINAL_OPERATION,
    F7OwnTerminalPolicy,
)


def test_own_terminal_policy_never_maps_fact_read_to_terminal_duty():
    school_id = uuid4()
    actor = contracts._issue_staff_actor_context(
        account_user_id=1,
        school_id=school_id,
        person_id=uuid4(),
        assignment_id=uuid4(),
        assignment_version=2,
        authorization_version=3,
    )
    policy = F7OwnTerminalPolicy()
    request = SimpleNamespace(
        actor=actor,
        module_name=F7_OWN_TERMINAL_MODULE,
        action_code=F7_OWN_TERMINAL_OPERATION,
        resource_type=F7_OWN_TERMINAL_MODULE,
        resource_id=school_id,
        resource_revision=3,
    )
    requirement = policy.requirement_for(request)
    assert requirement is not None
    assert requirement.duty_code == F7_OWN_TERMINAL_DUTY
    assert requirement.projection_name == "f7_own_terminal_status"
    assert policy.requirement_for(
        SimpleNamespace(**{**vars(request), "action_code": "school_facts.f2.person.read"})
    ) is None
    assert policy.requirement_for(
        SimpleNamespace(**{**vars(request), "resource_id": uuid4()})
    ) is None
    # An untrusted pseudo-actor must not match even with identical coordinates.
    assert policy.requirement_for(
        SimpleNamespace(
            **{
                **vars(request),
                "actor": SimpleNamespace(
                    school_id=actor.school_id,
                    authorization_version=actor.authorization_version,
                ),
            }
        )
    ) is None


def test_own_terminal_requires_exact_duty_and_revalidates_before_release(monkeypatch):
    school_id, scope_id = uuid4(), uuid4()
    actor = contracts._issue_staff_actor_context(
        account_user_id=1,
        school_id=school_id,
        person_id=uuid4(),
        assignment_id=uuid4(),
        assignment_version=2,
        authorization_version=3,
    )
    context = compose_command_context(actor, uuid4(), "f7-own-terminal-test")
    state = {"duty": F7_OWN_TERMINAL_DUTY, "current": True}

    class Adapter:
        def __init__(self, **_kwargs):
            pass

        def decide(self, **_kwargs):
            basis = ThinAuthorizationBasis(
                governance=SimpleNamespace(duty_code=state["duty"]),
                actor=SimpleNamespace(),
                module_name=F7_OWN_TERMINAL_MODULE,
                action_code=F7_OWN_TERMINAL_OPERATION,
                module_policy_version=1,
                resource_type=F7_OWN_TERMINAL_MODULE,
                resource_id=school_id,
                resource_revision=3,
                request_id=uuid4(),
                decision_id=uuid4(),
            )
            return ReadAuthorization(
                True,
                basis,
                ScopePlan(scope_id, "school_facts.school", f"school:{school_id}", 1),
                NamedProjection("f7_own_terminal_status", 1),
                "test-only",
            )

        def revalidate(self, *_args, **_kwargs):
            return state["current"]

    monkeypatch.setattr(terminal_auth, "ThinAuthorizationReadAdapter", Adapter)
    authorize = terminal_auth.F7OwnTerminalAuthorizer(
        thin_service=object(), school_scope_id=scope_id
    )
    proof = authorize(context)
    proof.revalidate()
    state["current"] = False
    with pytest.raises(SecureReadDenied):
        proof.revalidate()
    state["duty"] = "f2_person_reader"
    with pytest.raises(SecureReadDenied):
        authorize(context)
