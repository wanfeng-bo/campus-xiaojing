from contextlib import nullcontext
from types import SimpleNamespace
from app.composition.school_fact_directory_context import (
    bind_directory_identity,
    current_directory_actor_matches,
)
from test_f2_directory_product import actor


def test_self_basis_requires_current_private_identity_and_resets_binding():
    subject = actor()
    calls = []

    def resolve(reference):
        calls.append(reference)
        return SimpleNamespace(actor=subject)

    assert not current_directory_actor_matches(subject, resolve)
    with bind_directory_identity("private-one"):
        assert current_directory_actor_matches(subject, resolve)
        with bind_directory_identity("private-two"):
            assert current_directory_actor_matches(subject, resolve)
        assert current_directory_actor_matches(subject, resolve)
    assert not current_directory_actor_matches(subject, resolve)
    assert calls == ["private-one", "private-two", "private-one"]


def test_logout_during_read_invalidates_self_basis():
    def revoked(reference):
        raise PermissionError("expired")

    with bind_directory_identity("private-one"):
        assert not current_directory_actor_matches(actor(), revoked)
