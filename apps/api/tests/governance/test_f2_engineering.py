from dataclasses import replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import uuid4

import pytest

from app.composition.school_fact_engineering_governance import (
    EngineeringApproval,
    EngineeringCommandAuthorizer,
    EngineeringGovernanceDenied,
    EngineeringGovernanceService,
    governance_command_digest,
)
from app.platform.governance.contracts import CreateDutyCommand, GovernanceActorContext


def fixture():
    now = datetime(2026, 9, 6, tzinfo=UTC)
    actor = GovernanceActorContext(
        uuid4(), "f2_engineering", "f2-operator", None, None, uuid4(), "approved F2"
    )
    command = CreateDutyCommand(
        actor.school_id, "f2_person_reader", "Read approved persons", None, now, actor, "once"
    )
    approval = EngineeringApproval(
        actor.school_id,
        actor.actor_reference,
        now,
        now + timedelta(hours=2),
        frozenset({governance_command_digest(command)}),
    )
    return now, actor, command, approval


def test_confirm_exact_payload_and_authentication():
    now, actor, command, approval = fixture()

    class Writer:
        calls = []

        def create_duty(self, command):
            self.calls.append(command)
            return "receipt"

    writer = Writer()
    service = EngineeringGovernanceService(
        writer=writer, approval=approval, credential="separate credential", clock=lambda: now
    )
    with pytest.raises(EngineeringGovernanceDenied):
        service.prepare(command, credential="wrong")
    with pytest.raises(EngineeringGovernanceDenied):
        service.prepare(
            replace(command, duty_code="governance_admin"), credential="separate credential"
        )
    prepared = service.prepare(command, credential="separate credential")
    assert not writer.calls
    assert (
        service.confirm(prepared.confirmation_token, credential="separate credential") == "receipt"
    )
    assert len(writer.calls) == 1


def test_bound_authorizer_denies_tampered_payload():
    from app.composition.school_fact_engineering_governance import governance_command_payload
    from app.platform.governance.contracts import GovernanceWriteAuthorizationRequest

    now, actor, command, approval = fixture()
    auth = EngineeringCommandAuthorizer(approval=approval, clock=lambda: now)
    request = GovernanceWriteAuthorizationRequest(
        actor.school_id,
        "governance.duty.create",
        "duty",
        None,
        actor,
        command_payload_json=governance_command_payload(command),
        command_sha256=sha256(governance_command_payload(command).encode()).hexdigest(),
        idempotency_key_sha256=sha256(command.idempotency_key.encode()).hexdigest(),
    )
    assert auth.authorize(request)
    assert not auth.authorize(
        replace(request, idempotency_key_sha256=sha256(b"another key").hexdigest())
    )
    assert not auth.authorize(
        replace(
            request,
            command_payload_json=request.command_payload_json.replace(
                "f2_person_reader", "governance_admin"
            ),
        )
    )
    assert not auth.authorize(replace(request, actor=replace(actor, actor_assignment_id=uuid4())))
    assert not EngineeringCommandAuthorizer(
        approval=approval, clock=lambda: now + timedelta(days=1)
    ).authorize(request)


def test_writer_binds_full_command_before_transaction():
    from app.platform.governance.application import (
        GovernanceAuthorizationDenied,
        GovernanceWriter,
        _command_sha256,
    )

    now, actor, command, approval = fixture()
    captured = []

    class Deny:
        def authorize(self, request):
            captured.append(request)
            return False

    def never_session():
        raise AssertionError("denial must precede database transaction")

    writer = GovernanceWriter(
        session_factory=never_session,
        authorizer=Deny(),
        clock=lambda: now,
        dependency_port=object(),
        scope_owner_resolvers={},
    )
    with pytest.raises(GovernanceAuthorizationDenied):
        writer.create_duty(command)
    request = captured[0]
    assert request.command_sha256 == _command_sha256(command, "governance.duty.create")
    assert request.idempotency_key_sha256 == sha256(command.idempotency_key.encode()).hexdigest()
    assert "f2_person_reader" in request.command_payload_json


def test_expired_confirmation_only_recovers_original_result():
    now, actor, command, approval = fixture()
    time = [now]

    class Writer:
        mutations = 0
        reads = 0

        def create_duty(self, command):
            self.mutations += 1
            return "original"

        def lookup_command_result(self, command, *, command_name):
            self.reads += 1
            return "original"

    writer = Writer()
    service = EngineeringGovernanceService(
        writer=writer, approval=approval, credential="separate credential", clock=lambda: time[0]
    )
    prepared = service.prepare(command, credential="separate credential")
    assert (
        service.confirm(prepared.confirmation_token, credential="separate credential") == "original"
    )
    time[0] = approval.valid_to + timedelta(hours=1)
    with pytest.raises(EngineeringGovernanceDenied):
        service.confirm(prepared.confirmation_token, credential="separate credential")
    assert service.receipt(command, credential="separate credential") == "original"
    assert writer.mutations == 1 and writer.reads == 1
    with pytest.raises(EngineeringGovernanceDenied):
        service.receipt(
            replace(command, idempotency_key="different"), credential="separate credential"
        )


def test_engineering_transaction_rejects_db_owner_even_with_approved_command():
    from app.composition.school_fact_engineering_governance import governance_command_payload
    from app.platform.governance.contracts import GovernanceWriteAuthorizationRequest

    now, actor, command, approval = fixture()
    auth = EngineeringCommandAuthorizer(approval=approval, clock=lambda: now)

    class Repository:
        def session_user(self):
            return "campus_ai_owner"

    request = GovernanceWriteAuthorizationRequest(
        actor.school_id,
        "governance.duty.create",
        "duty",
        None,
        actor,
        governance_command_payload(command),
        sha256(governance_command_payload(command).encode()).hexdigest(),
        sha256(command.idempotency_key.encode()).hexdigest(),
    )
    with pytest.raises(EngineeringGovernanceDenied):
        auth.authorize_session(Repository(), request)


@pytest.mark.parametrize("boundary", ["confirm", "mutation", "replay"])
@pytest.mark.parametrize("loss", ["expired", "closed", "revoked"])
def test_approval_loss_after_prepare_denies_execution_but_keeps_receipt(boundary, loss):
    now, actor, command, approval = fixture()
    time, current = [now], [True]

    class Writer:
        def create_duty(self, command):
            raise AssertionError("No write after approval loss")

        def lookup_command_result(self, command, *, command_name):
            return "original"

    service = EngineeringGovernanceService(
        writer=Writer(),
        approval=approval,
        credential="separate credential",
        clock=lambda: time[0],
        approval_is_current=lambda: current[0],
    )
    prepared = service.prepare(command, credential="separate credential")
    auth = EngineeringCommandAuthorizer(
        approval=approval, clock=lambda: time[0], approval_is_current=lambda: current[0]
    )
    request = service._request(command)

    class Repository:
        def session_user(self):
            return "campus_ai_governance_writer"

    auth.authorize_session(Repository(), request)
    if loss == "expired":
        time[0] = approval.valid_to
    else:
        current[0] = False
    with pytest.raises(EngineeringGovernanceDenied):
        if boundary == "confirm":
            service.confirm(prepared.confirmation_token, credential="separate credential")
        else:
            getattr(auth, "authorize_" + boundary)(Repository(), request)
    assert service.receipt(command, credential="separate credential") == "original"
    with pytest.raises(EngineeringGovernanceDenied):
        service.prepare(command, credential="separate credential")


@pytest.mark.parametrize("loss", ["closed", "expired"])
def test_real_writer_rechecks_authority_after_receipt_lock_before_mutation(monkeypatch, loss):
    import app.platform.governance.application as application

    now, actor, command, approval = fixture()
    time, current, mutations = [now], [True], []
    auth = EngineeringCommandAuthorizer(
        approval=approval, clock=lambda: time[0], approval_is_current=lambda: current[0]
    )

    class Repository:
        def session_user(self):
            return "campus_ai_governance_writer"

        def acquire_receipt(self, candidate):
            if loss == "closed":
                current[0] = False
            else:
                time[0] = approval.valid_to
            return candidate, True

    class Uow:
        repository = Repository()

        def __init__(self, factory):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    monkeypatch.setattr(application, "GovernanceUnitOfWork", Uow)
    writer = application.GovernanceWriter(
        session_factory=object(),
        authorizer=auth,
        transaction_authorizer=auth,
        clock=lambda: time[0],
        dependency_port=object(),
        scope_owner_resolvers={},
    )
    with pytest.raises(EngineeringGovernanceDenied):
        writer._execute(
            command,
            command_name="governance.duty.create",
            target_kind="duty",
            target_id=None,
            mutate=lambda repository: mutations.append(command),
        )
    assert not mutations
