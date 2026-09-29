from datetime import UTC, datetime

from app.platform.authorization.secure_reader_adapters import ThinAuthorizationReadAdapter
from app.platform.authorization.secure_reader_registry import ReadOperation
from app.platform.authorization.thin_service import ThinAuthorizationService
from app.platform.school_facts.f2_policy import F2FactPolicy
from tests.test_secure_reader_projection import _actor


def test_person_self_uses_current_identity_without_governance_grant():
    actor = _actor()
    now = datetime.now(UTC)

    class NeverGrant:
        def issue(self, *args, **kwargs):
            raise AssertionError("self must not issue fake Grant")

        def validate(self, *args, **kwargs):
            raise AssertionError("self must not validate fake Grant")

    live = [True]
    thin = ThinAuthorizationService(
        governance_basis_service=NeverGrant(),
        module_policies={"school_facts.f2": F2FactPolicy()},
        self_person_validator=lambda a, t: live[0],
    )
    op = ReadOperation(
        "school_facts.f2.person_contact.self",
        "f2_person_contact_self",
        "school_facts.f2.person_contact.self",
        "school_facts.f2",
    )
    adapter = ThinAuthorizationReadAdapter(
        thin_service=thin, scope_id_resolver=lambda op, a: a.person_id
    )
    auth = adapter.decide(operation=op, actor=actor, as_of=now)
    assert auth.allowed
    assert auth.scope_plan.reference == f"person:{actor.person_id}"
    assert not hasattr(auth.basis, "governance")
    assert adapter.revalidate(auth, actor=actor, as_of=now)
    live[0] = False
    assert not adapter.revalidate(auth, actor=actor, as_of=now)


def test_factual_time_never_replaces_current_authorization_time():
    from dataclasses import replace
    from datetime import timedelta

    from app.platform.authorization.secure_reader_compiler import ServerOwnedProjectionCompiler
    from app.platform.authorization.secure_reader_registry import test_operation_registry
    from app.platform.authorization.secure_reader_service import SecureCursorCodec, SecureReader
    from tests.test_secure_reader_projection import _Authorizer, _Executor, _request

    now = datetime.now(UTC)
    seen = []

    class Authority(_Authorizer):
        def decide(self, *, operation, actor, as_of):
            seen.append(as_of)
            return super().decide(operation=operation)

        def revalidate(self, authorization, *, actor, as_of):
            seen.append(as_of)
            return True

    executor = _Executor()
    reader = SecureReader(
        registry=test_operation_registry(),
        compiler=ServerOwnedProjectionCompiler(),
        authorizer=Authority(),
        executor=executor,
        cursor_codec=SecureCursorCodec(b"test-secret"),
        clock=lambda: now,
    )
    past = now - timedelta(days=30)
    page = reader.read(replace(_request("test_only.r1.public_location"), factual_at=past))
    assert page.as_of == past
    assert seen == [now, now, now]
    assert all(relation.as_of == past for relation in executor.relations)
    import pytest

    from app.platform.authorization.secure_reader_contracts import SecureReadDenied

    with pytest.raises(SecureReadDenied):
        reader.read(
            replace(
                _request("test_only.r1.public_location", cursor=page.next_cursor), factual_at=now
            )
        )
