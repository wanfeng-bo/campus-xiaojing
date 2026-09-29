"""Same synchronous batch shares roots and independently checks them before return."""

from contextlib import nullcontext
from dataclasses import replace
from types import SimpleNamespace

import pytest

from tests.governance.test_f4_authority import NOW, actor, authority


def fixture(monkeypatch):
    import app.composition.school_fact_directory_services as directory
    from app.composition.school_fact_education_authority import create_education_authority
    from app.composition.school_fact_product_services import ReadOnlyGovernanceDependencies
    from app.composition.school_fact_read_capture import EvidenceGovernanceDependencies

    original, _ = authority()
    permit = original.require_scope(
        actor(), fact_kind="education_class", action="read", checked_at=NOW
    )
    counts = {"assignment": 0, "roots": 0}
    state = {"version": 1, "mutate": False, "thin": 0, "changed_kind": "assignment"}

    def assignment(self, **kw):
        counts["assignment"] += 1
        return state["version"] if state["changed_kind"] == "assignment" else 1

    def root(self, **kw):
        counts["roots"] += 1
        return state["version"] if state["changed_kind"] == kw["kind"] else 1

    monkeypatch.setattr(ReadOnlyGovernanceDependencies, "read_assignment", assignment)
    monkeypatch.setattr(ReadOnlyGovernanceDependencies, "_root", root)
    monkeypatch.setattr(
        directory, "_fresh_root_scope", lambda _: nullcontext(ReadOnlyGovernanceDependencies(None))
    )
    auth = create_education_authority(sessions=None, configuration=original.configuration)
    auth.clock = lambda: NOW
    monkeypatch.setattr(auth.connection_authority, "read_scope", lambda _: nullcontext())
    deps = auth.connection_authority._build_basis(None)._authority_reader._dependency_port
    assert isinstance(deps, EvidenceGovernanceDependencies)

    def batch(bases, **kwargs):
        state["thin"] += len(bases)
        for _ in bases:
            deps.read_assignment(
                school_id=actor().school_id,
                assignment_id=actor().assignment_id,
                as_of=kwargs["as_of"],
            )
            for kind in ("person", "organization"):
                deps._root(
                    school_id=actor().school_id,
                    root_id=actor().person_id,
                    kind=kind,
                    as_of=kwargs["as_of"],
                )
        if state["mutate"]:
            state["version"] += 1
        from app.platform.authorization.thin_contracts import ThinDecisionKind

        return tuple(SimpleNamespace(kind=ThinDecisionKind.ALLOW) for _ in bases)

    auth.thin = SimpleNamespace(revalidate_many=batch)
    permits = tuple(replace(permit, action=str(i)) for i in range(5))
    return auth, permits, counts, state


def test_each_call_reads_three_roots_once_then_fresh_and_keeps_every_permit(monkeypatch):
    auth, permits, counts, state = fixture(monkeypatch)
    auth.revalidate_many(permits, actor=actor(), checked_at=NOW)
    assert counts == {"assignment": 2, "roots": 4} and state["thin"] == 5
    auth.revalidate_many(permits, actor=actor(), checked_at=NOW)
    assert counts == {"assignment": 4, "roots": 8} and state["thin"] == 10


@pytest.mark.parametrize("changed_kind", ["assignment", "person", "organization"])
def test_changed_root_fails_inside_call_and_next_call_has_no_retained_cache(
    monkeypatch, changed_kind
):
    from app.composition.school_fact_education_authority import EducationAuthorityDenied

    auth, permits, counts, state = fixture(monkeypatch)
    state["mutate"] = True
    state["changed_kind"] = changed_kind
    with pytest.raises(EducationAuthorityDenied):
        auth.revalidate_many(permits, actor=actor(), checked_at=NOW)
    state["mutate"] = False
    auth.revalidate_many(permits, actor=actor(), checked_at=NOW)
    assert state["thin"] == 10


def test_different_actor_is_rejected_before_entering_batch(monkeypatch):
    auth, permits, counts, state = fixture(monkeypatch)
    from app.platform.contracts import _issue_staff_actor_context

    original = actor()
    other = _issue_staff_actor_context(
        account_user_id=original.account_user_id,
        school_id=original.school_id,
        person_id=original.person_id,
        assignment_id=original.assignment_id,
        assignment_version=2,
        authorization_version=original.authorization_version,
    )
    with pytest.raises(PermissionError):
        auth.revalidate_many((replace(permits[0], actor=other),), actor=original, checked_at=NOW)
    assert counts == {"assignment": 0, "roots": 0} and state["thin"] == 0


def test_thin_failure_is_not_deferred_or_reclassified_and_round_is_discarded(monkeypatch):
    auth, permits, counts, state = fixture(monkeypatch)
    original = auth.thin.revalidate_many

    def fail(*args, **kwargs):
        raise LookupError("controlled authority failure")

    auth.thin.revalidate_many = fail
    with pytest.raises(LookupError, match="controlled authority failure"):
        auth.revalidate_many(permits, actor=actor(), checked_at=NOW)
    assert counts == {"assignment": 0, "roots": 0}
    auth.thin.revalidate_many = original
    auth.revalidate_many(permits, actor=actor(), checked_at=NOW)
    assert counts == {"assignment": 2, "roots": 4}


def issue_fixture(monkeypatch):
    auth, permits, counts, state = fixture(monkeypatch)
    auth.clock = lambda: NOW
    source, governance = authority()
    governance.sid = next(iter(auth.bindings.values()))
    revalidate_many = auth.thin.revalidate_many

    def decide_many(requests):
        revalidate_many(permits[: len(requests)], as_of=requests[0].as_of)
        return tuple(source.thin.decide(request) for request in requests)

    auth.thin = SimpleNamespace(decide_many=decide_many, revalidate_many=revalidate_many)
    return auth, counts, state


def test_read_wave_issues_and_revalidates_with_fresh_roots_inside_outer_release(monkeypatch):
    from app.composition.school_fact_read_evidence import validation_batch

    auth, counts, state = issue_fixture(monkeypatch)
    requests = (("education_class", "read"), ("education_class", "maintain"))
    with validation_batch() as release:
        permits = auth.require_scopes(actor(), requests=requests, checked_at=NOW)
        assert counts == {"assignment": 2, "roots": 4}
        assert release.root_rounds == {}
        auth.revalidate_read_wave(permits, actor=actor(), checked_at=NOW)
        assert counts == {"assignment": 4, "roots": 8}
        auth.require_scopes(actor(), requests=requests, checked_at=NOW)
        assert counts == {"assignment": 6, "roots": 12}
        assert release.root_rounds == {}
    assert counts == {"assignment": 6, "roots": 12} and state["thin"] == 6


@pytest.mark.parametrize("changed_kind", ["assignment", "person", "organization"])
@pytest.mark.parametrize("phase", ["issue", "after_read"])
def test_read_wave_root_change_is_rejected_before_return_even_in_outer_release(
    monkeypatch, changed_kind, phase
):
    from app.composition.school_fact_education_authority import EducationAuthorityDenied
    from app.composition.school_fact_read_evidence import validation_batch

    auth, counts, state = issue_fixture(monkeypatch)
    requests = (("education_class", "read"), ("education_class", "maintain"))
    permits = auth.require_scopes(actor(), requests=requests, checked_at=NOW)
    state["mutate"] = True
    state["changed_kind"] = changed_kind
    with validation_batch():
        with pytest.raises(EducationAuthorityDenied):
            if phase == "issue":
                auth.require_scopes(actor(), requests=requests, checked_at=NOW)
            else:
                auth.revalidate_read_wave(permits, actor=actor(), checked_at=NOW)
    state["mutate"] = False
    auth.require_scopes(actor(), requests=requests, checked_at=NOW)
    assert state["thin"] == 6
