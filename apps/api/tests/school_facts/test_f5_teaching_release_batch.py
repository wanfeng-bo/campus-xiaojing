"""One teaching release shares exact proofs, never a later response boundary."""

from datetime import timedelta
from types import SimpleNamespace

import pytest

from app.composition.school_fact_education_external import ExternalReadProof
from app.composition.school_fact_education_query import EducationQueryProof
from app.platform.assistant.runtime import AssistantRuntimeError
from tests.test_f2_read_evidence import NOW, Backend, actor, evidence


def composite(backend, trailing=(), *, deadline=None):
    education = SimpleNamespace(school_fact_version=4, revalidate=lambda **_: None)
    exact = ExternalReadProof(actor(), (evidence(backend, deadline=deadline),))
    return EducationQueryProof(education, (exact, exact, *trailing))


def test_shared_current_teaching_proof_batches_only_one_release():
    backend = Backend()
    proof = composite(backend)
    proof.revalidate(actor=actor())
    assert backend.calls.count("version") == 2
    assert backend.calls.count("decision") == 2
    assert backend.calls.count("metadata") == 2
    proof.revalidate(actor=actor())
    assert backend.calls.count("version") == 4
    assert backend.calls.count("decision") == 4


@pytest.mark.parametrize("change", ["authority", "revision", "members"])
def test_change_after_related_proofs_is_rechecked_at_release(change):
    backend = Backend()
    values = {"authority": "denied", "revision": 5, "members": ()}
    trailing = SimpleNamespace(
        school_fact_version=4, revalidate=lambda: setattr(backend, change, values[change])
    )
    proof = composite(backend, (trailing,))
    with pytest.raises(AssistantRuntimeError):
        proof.revalidate(actor=actor())


def test_natural_deadline_crossed_during_combined_release_is_rejected():
    backend = Backend()
    deadline = NOW + timedelta(seconds=1)
    trailing = SimpleNamespace(
        school_fact_version=4,
        revalidate=lambda: setattr(backend, "now", deadline),
    )
    with pytest.raises(AssistantRuntimeError):
        composite(backend, (trailing,), deadline=deadline).revalidate(actor=actor())


def test_staff_coordinate_clock_is_checked_after_final_identity_without_repeating_io():
    from app.composition.school_fact_read_evidence import validation_batch
    from app.composition.school_fact_staff_teaching_query import StaffCoordinateProof

    now = [NOW]
    deadline = NOW + timedelta(seconds=1)
    checks = []

    def check():
        checks.append("coordinate")
        return now[0] < deadline

    proof = StaffCoordinateProof(4, check, (lambda: now[0], NOW, deadline))
    with pytest.raises(AssistantRuntimeError, match="time boundary"):
        with validation_batch() as batch:
            batch.identity_checks["synthetic"] = SimpleNamespace(
                revalidate=lambda: now.__setitem__(0, deadline)
            )
            proof.revalidate()
    assert checks == ["coordinate"]


@pytest.mark.parametrize("quality", [False, True])
def test_history_content_version_is_read_twice_per_exact_release_dependency(quality):
    from app.composition.school_fact_education_quality import EducationDependencyProof
    from app.composition.school_fact_education_reader import EducationReadProof
    from app.composition.school_fact_read_evidence import validation_batch

    reads, permits = [], []
    authority = SimpleNamespace(revalidate_many=lambda values, **_: permits.append(values))
    owner = SimpleNamespace(
        authority=authority, education_authority=authority,
        projection=SimpleNamespace(current_version=lambda school: reads.append(school) or 4),
        clock=lambda: NOW,
    )
    if quality:
        proofs = [EducationDependencyProof(owner, actor(), (), (n,), 4) for n in range(4)]
    else:
        proofs = [EducationReadProof(owner, actor(), (n,), 4, None) for n in range(4)]
    for release_number in (1, 2):
        with validation_batch():
            for proof in proofs:
                # The turn gateway and runtime both depend on this same material.
                proof.revalidate()
                proof.revalidate()
        assert len(reads) == 2 * release_number
        # Only content version IO is shared; exact authority requests still run.
        assert len(permits) == 8 * release_number


def test_exact_external_boundary_history_rereads_once_at_each_release():
    from app.composition.school_fact_education_external import ArrangementReadProof
    from app.composition.school_fact_read_evidence import validation_batch

    calls = []

    def reread():
        calls.extend(("authorize", "sql", "revalidate"))
        return {"school_fact_version": 4}

    proof = ArrangementReadProof(4, {"school_fact_version": 4}, reread)
    for release_number in (1, 2):
        with validation_batch():
            proof.revalidate()
            proof.revalidate()
        assert calls == ["authorize", "sql", "revalidate"] * release_number


@pytest.mark.parametrize("change", ["revoked", "version"])
def test_external_boundary_history_reread_is_fresh_after_registration(change):
    from app.composition.school_fact_education_external import ArrangementReadProof
    from app.composition.school_fact_education_reader import EducationReadStale
    from app.composition.school_fact_read_evidence import validation_batch

    state = {"revoked": False, "version": 4}

    def reread():
        if state["revoked"]:
            raise PermissionError("revoked")
        return {"school_fact_version": state["version"]}

    proof = ArrangementReadProof(4, {"school_fact_version": 4}, reread)
    with pytest.raises((PermissionError, EducationReadStale)):
        with validation_batch():
            proof.revalidate()
            state[change] = True if change == "revoked" else 5


def test_distinct_external_boundary_proofs_do_not_share_actor_or_field_checks():
    from app.composition.school_fact_education_external import ArrangementReadProof
    from app.composition.school_fact_read_evidence import validation_batch

    calls = []
    proofs = [
        ArrangementReadProof(4, {}, lambda index=index: calls.append(index) or {})
        for index in range(3)
    ]
    with validation_batch():
        for proof in proofs:
            proof.revalidate()
            proof.revalidate()
    assert calls == [0, 1, 2]
