"""F6 fixed publication contract, without live services."""

import hashlib
import json
from datetime import timedelta
from types import SimpleNamespace as N

import pytest

from app.composition.school_fact_product_identity import ProductIdentity
from app.platform.contracts import _issue_staff_actor_context
from tests.governance.test_f3_formal_writer_adapter import NOW, Boundary, Guard, actor


def envelope():
    from scripts.f5_engineering.prepare_samples import build_plan
    from scripts.f6_engineering.publication import SCHOOL

    old = build_plan()
    change = {
        "schema_version": 4,
        "operation": "upsert",
        "fact_kind": "location",
        "source_key": "f6:location:test",
        "expected_version": 0,
        "effective_at": NOW.isoformat(),
        "origin": {"sheet_code": "f6-test", "row_number": 1},
        "content": {
            "kind": "campus",
            "business_code": "F6TEST",
            "display_name": "test",
            "display_path": "/test",
            "status": "active",
        },
    }
    batch = {**old["batches"]["foundation"], "schema_version": 4, "changes": [change]}
    return {
        "format": "f6_synthetic_publication_v1",
        "status": "prepared_not_published",
        "approval_reference": "approved-f6-test",
        "school_id": str(SCHOOL),
        "batches": {"foundation": batch},
    }


def pin(data):
    raw = json.dumps(data).encode()
    return raw, hashlib.sha256(raw).hexdigest()


def test_review_is_offline_and_pinned_v4_only():
    from scripts.f6_engineering.publication import create_formal_f6_port, validate_input

    raw, digest = pin(envelope())
    result = create_formal_f6_port(raw=raw, approved_input_sha256=digest)
    assert result["mutations"] == 0 and result["stages"] == ("foundation",)
    assert result["batches"]["foundation"]["change_set"]["schema_version"] == 4
    with pytest.raises(ValueError):
        validate_input(raw + b" ", approved_input_sha256=digest)
    for key, value in (("schema_version", 3),):
        data = envelope()
        data["batches"]["foundation"][key] = value
        r, d = pin(data)
        with pytest.raises(ValueError):
            validate_input(r, approved_input_sha256=d)


def test_stage_owner_and_school_are_finite():
    from scripts.f6_engineering.publication import validate_input

    for mode in ("school", "stage", "kind"):
        data = envelope()
        if mode == "school":
            data["school_id"] = "00000000-0000-0000-0000-000000000001"
        elif mode == "stage":
            data["batches"] = {"teaching": data["batches"]["foundation"]}
        else:
            data["batches"] = {"facilities": data["batches"]["foundation"]}
        raw, digest = pin(data)
        with pytest.raises(ValueError):
            validate_input(raw, approved_input_sha256=digest)


def test_f6_unknown_confirmation_recovers_original_only(tmp_path):
    from scripts.f6_engineering.publication import (
        ASSIGNMENT,
        SCHOOL,
        F6Publication,
        PublicationRecoveryPending,
    )

    raw, digest = pin(envelope())
    staff = _issue_staff_actor_context(
        account_user_id=9,
        school_id=SCHOOL,
        person_id=actor().person_id,
        assignment_id=ASSIGNMENT,
        assignment_version=2,
        authorization_version=5,
    )
    boundary = Boundary()
    driver = F6Publication(
        raw=raw,
        approved_input_sha256=digest,
        journal_path=tmp_path / "journal.json",
        identity=lambda: ProductIdentity(staff, "a" * 64, "test"),
        expected_school=SCHOOL,
        expected_assignment=ASSIGNMENT,
        boundary=boundary,
        guard=Guard(),
        grant_epoch=NOW,
        fixed_deadline=NOW + timedelta(days=7),
        clock=lambda: NOW,
    )
    prepared = driver.prepare("foundation", base_version=3)
    provenance = next(kw["provenance"] for method, kw in boundary.calls if method == "prepare")
    assert provenance.template_version == "f6_seed_v1"
    boundary.unknown = True
    with pytest.raises(PublicationRecoveryPending):
        driver.confirm(prepared)
    assert driver.receipt("foundation").target_version == 4
    with pytest.raises(PublicationRecoveryPending):
        driver.prepare("foundation", base_version=4)
    assert sum(method == "confirm" for method, _ in boundary.calls) == 1


def test_worker_f6_stage_and_private_mount():
    from scripts.f6_engineering import runtime_seed, seed_worker

    assert seed_worker.STAGES == {"foundation", "facilities", "residences", "quality"}
    args = runtime_seed.worker_argv(
        docker="docker",
        image="sha256:" + "a" * 64,
        name="f6-test",
        owner="owner",
        folder=__import__("pathlib").Path("."),
        mounts=[],
        network=runtime_seed.NETWORK,
    )
    assert args[-1] == "/workspace/scripts/f6_engineering/seed_worker.py"
    assert any("target=/run/f6-seed" in x for x in args)
    assert "school-facts.f6.seed-owner=owner" in args
    assert "--read-only" in args and "none" in args


def test_f6_guards_route_real_owner_and_revalidate_without_epoch_synthesis():
    from scripts.f6_engineering.publication import F6OwnerGuard

    called = []
    epoch = [4]

    def permit():
        return N(basis=N(governance=N(assignment_epoch=epoch[0])))

    residence = N(
        require_change_set=lambda *a, **k: called.append("require-f6") or (permit(),),
        revalidate=lambda p, *, actor, checked_at: called.append("revalidate-f6"),
    )
    quality = N(
        require_change_set=lambda *a: N(
            f2_permits=(permit(),),
            residence_permits=(permit(),),
            revalidate=lambda: called.append("revalidate-quality"),
        )
    )
    guard = F6OwnerGuard(components=N(), residence=residence, quality=quality)

    class Batch:
        def __init__(self, changes):
            self.changes = changes

        def model_copy(self, *, update):
            return Batch(update["changes"])

    cs = Batch((N(fact_kind="dorm_bed"), N(fact_kind="fact_quality")))
    proof = guard.require_change_set(actor(), cs, checked_at=NOW)
    assert proof[3] == 4
    guard.final_guard(proof, actor(), cs, checked_at=NOW)
    assert "revalidate-f6" in called and "revalidate-quality" in called
    epoch[0] = 5
    with pytest.raises(PermissionError, match="changed"):
        guard.final_guard(proof, actor(), cs, checked_at=NOW)


def test_f6_execute_rejects_other_cluster_before_opening_resources():
    from scripts.f6_engineering.publication import ASSIGNMENT, SCHOOL, create_formal_f6_port

    raw, digest = pin(envelope())
    with pytest.raises(PermissionError, match="cluster"):
        create_formal_f6_port(
            raw=raw,
            approved_input_sha256=digest,
            execute=True,
            expected_assignment=ASSIGNMENT,
            configuration=N(
                residence=N(dependency_education=object()),
                reader=N(expected_school_id=SCHOOL, expected_system_identifier="other"),
            ),
        )


def test_f6_explicit_unknown_survives_canonical_pin_and_review():
    from scripts.f6_engineering.publication import create_formal_f6_port

    data = envelope()
    batch = data["batches"].pop("foundation")
    change = batch["changes"][0]
    change.update(
        fact_kind="student_school_relationship",
        source_key="f6:ssr:test",
        content={
            "student_source_key": "person:test",
            "student_number": "F6TEST",
            "enrollment_status": "enrolled",
            "residence_mode": None,
            "primary_campus_source_key": "location:campus",
            "administrative_campus_source_key": "location:campus",
        },
    )
    data["batches"]["residences"] = batch
    raw, digest = pin(data)
    result = create_formal_f6_port(raw=raw, approved_input_sha256=digest)
    assert (
        result["batches"]["residences"]["change_set"]["changes"][0]["content"]["residence_mode"]
        is None
    )


def test_later_stage_requires_authoritative_predecessor_and_unchanged_deadline(tmp_path):
    from scripts.f6_engineering.publication import (
        ASSIGNMENT,
        SCHOOL,
        F6Publication,
        PublicationRecoveryPending,
    )

    data = envelope()
    facility = json.loads(json.dumps(data["batches"]["foundation"]))
    facility["changes"][0].update(
        fact_kind="dormitory_building_profile",
        source_key="f6:building:test",
        content={
            "location_source_key": "f6:location:test",
            "usage_kind": "building",
            "display_name": "test",
            "is_available": True,
        },
    )
    data["batches"]["facilities"] = facility
    raw, digest = pin(data)
    staff = _issue_staff_actor_context(
        account_user_id=9,
        school_id=SCHOOL,
        person_id=actor().person_id,
        assignment_id=ASSIGNMENT,
        assignment_version=2,
        authorization_version=5,
    )
    boundary = Boundary()
    guard = Guard()
    driver = F6Publication(
        raw=raw,
        approved_input_sha256=digest,
        journal_path=tmp_path / "journal.json",
        identity=lambda: ProductIdentity(staff, "a" * 64, "test"),
        expected_school=SCHOOL,
        expected_assignment=ASSIGNMENT,
        boundary=boundary,
        guard=guard,
        grant_epoch=NOW,
        fixed_deadline=NOW + timedelta(days=7),
        clock=lambda: NOW,
    )
    with pytest.raises(PublicationRecoveryPending):
        driver.prepare("facilities", base_version=3)
    assert not boundary.calls
    first = driver.prepare("foundation", base_version=3)
    driver.confirm(first)
    second = driver.prepare("facilities", base_version=4)
    guard.denied = True
    with pytest.raises(PermissionError):
        driver.confirm(second)
    assert sum(method == "confirm" for method, _ in boundary.calls) == 1
    driver.clock = lambda: driver.deadline
    with pytest.raises(PermissionError):
        driver.confirm(second)
    assert driver.receipt("foundation").target_version == 4


def test_f6_job_contract_accepts_new_stage_not_legacy_teaching():
    from scripts.f6_engineering.seed_worker import check_job

    job = {
        "stage": "facilities",
        "source_manifest_sha256": "a" * 64,
        "approved_input_sha256": "b" * 64,
        "issued_at": NOW.isoformat(),
        "fixed_deadline": (NOW + timedelta(days=7)).isoformat(),
        "assignment_epoch": 3,
        "receipt_only": False,
    }
    assert not check_job(job, {"maintainer_token": "x" * 32}, now=NOW)
    assert "stage" in check_job(
        job | {"stage": "teaching"}, {"maintainer_token": "x" * 32}, now=NOW
    )
    assert "fixed_seven_day_window" in check_job(
        job, {"maintainer_token": "x" * 32}, now=NOW + timedelta(days=7)
    )
