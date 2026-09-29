"""F5 publication uses the canonical boundary and immutable recovery journal."""

import hashlib
import json
import sys
from datetime import timedelta
from importlib import import_module
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[4]
sys.path.insert(0, str(ROOT))
from app.composition.school_fact_product_identity import ProductIdentity  # noqa: E402
from tests.governance.test_f3_formal_writer_adapter import NOW, Boundary, Guard, actor  # noqa: E402


def setup(tmp_path):
    m = import_module("scripts.f5_engineering.publication")
    from scripts.f5_engineering.prepare_samples import build_plan

    data = build_plan()
    data["school_id"] = str(actor().school_id)
    raw = json.dumps(data, ensure_ascii=False).encode()
    digest = hashlib.sha256(raw).hexdigest()
    identity = ProductIdentity(actor(), "a" * 64, "test-ref")
    boundary = Boundary()
    guard = Guard()
    driver = m.F5Publication(
        raw=raw,
        approved_input_sha256=digest,
        journal_path=tmp_path / "journal.json",
        identity=lambda: identity,
        expected_school=identity.actor.school_id,
        expected_assignment=identity.actor.assignment_id,
        boundary=boundary,
        guard=guard,
        grant_epoch=NOW,
        fixed_deadline=NOW + timedelta(days=7),
        clock=lambda: NOW,
    )
    return m, driver, boundary, guard


def test_input_pin_and_scope_stage_names_are_f5_and_offline_review_has_no_resources(tmp_path):
    m, driver, boundary, _ = setup(tmp_path)
    result = m.create_formal_f5_port(raw=driver.raw, approved_input_sha256=driver.input_sha256)
    assert result["mode"] == "review" and result["mutations"] == 0
    assert result["stages"] == ("foundation", "teaching") and not boundary.calls
    with pytest.raises(ValueError):
        m.validate_input(driver.raw + b" ", approved_input_sha256=driver.input_sha256)


def test_unknown_confirm_is_receipt_only_and_f5_provenance_retains_exact_full_batch(tmp_path):
    m, driver, boundary, _ = setup(tmp_path)
    prepared = driver.prepare("foundation", base_version=22)
    provenance = next(kw["provenance"] for method, kw in boundary.calls if method == "prepare")
    assert (
        provenance.template_version == "f5_seed_v1"
        and provenance.mapping_config_sha256 == driver.input_sha256
    )
    assert len(prepared.change_set.changes) == len(driver.stages["foundation"].changes)
    boundary.unknown = True
    with pytest.raises(m.PublicationRecoveryPending):
        driver.confirm(prepared)
    assert driver.receipt("foundation").target_version == 23
    with pytest.raises(m.PublicationRecoveryPending):
        driver.prepare("foundation", base_version=23)
    assert sum(method == "confirm" for method, _ in boundary.calls) == 1


def test_teaching_requires_original_foundation_receipt_and_final_guard_before_confirm(tmp_path):
    m, driver, boundary, guard = setup(tmp_path)
    with pytest.raises(m.PublicationRecoveryPending):
        driver.prepare("teaching", base_version=1)
    assert not boundary.calls
    prepared = driver.prepare("foundation", base_version=1)
    guard.denied = True
    with pytest.raises(PermissionError):
        driver.confirm(prepared)
    assert not any(method == "confirm" for method, _ in boundary.calls)


def test_f5_review_rejects_f4_envelope_even_with_new_pin(tmp_path):
    m, driver, _, _ = setup(tmp_path)
    data = json.loads(driver.raw)
    data["format"] = "f4_synthetic_publication_v1"
    raw = json.dumps(data).encode()
    with pytest.raises(ValueError):
        m.validate_input(raw, approved_input_sha256=hashlib.sha256(raw).hexdigest())


def test_f5_owner_guard_routes_teaching_and_rejects_cross_owner_epoch_drift(tmp_path):
    from types import SimpleNamespace as N

    m, driver, _, _ = setup(tmp_path)

    def permit(epoch):
        return N(basis=N(governance=N(assignment_epoch=epoch)))

    called = []
    edu = N(
        require_change_set=lambda *a, **kw: (
            called.append("teaching") or N(item_permits=(permit(3),))
        )
    )
    guard = m.F5OwnerGuard(N(authority=edu))
    proof = guard.require_change_set(actor(), driver.stages["teaching"], checked_at=NOW)
    assert called == ["teaching"] and proof[3] == 3
    guard = m.F5OwnerGuard(
        N(
            family=N(authority=N(require_change_set=lambda *a, **kw: N(item_permits=(permit(2),)))),
            dependency_directory=N(guard=N(require_change_set=lambda *a, **kw: (permit(3),))),
        )
    )
    with pytest.raises(PermissionError, match="epochs differ"):
        guard.require_change_set(actor(), driver.stages["foundation"], checked_at=NOW)


def test_journal_changes_fail_before_original_receipt_lookup(tmp_path):
    m, driver, boundary, _ = setup(tmp_path)
    driver.prepare("foundation", base_version=4)
    driver.journal["batches"]["foundation"]["change_set"]["base_version"] = 5
    boundary.calls.clear()
    with pytest.raises(ValueError):
        driver.receipt("foundation")
    assert not boundary.calls


def test_expired_window_still_allows_only_original_receipt(tmp_path):
    m, driver, boundary, _ = setup(tmp_path)
    prepared = driver.prepare("foundation", base_version=4)
    driver.confirm(prepared)
    driver.clock = lambda: driver.deadline
    with pytest.raises(PermissionError):
        driver.prepare("teaching", base_version=5)
    assert driver.receipt("foundation").target_version == 5


def test_stage_dispatch_never_prepares_an_existing_attempt_or_receipt_only_stage(tmp_path):
    from types import SimpleNamespace as N

    m, driver, boundary, _ = setup(tmp_path)
    port = N(
        stages=driver.stages,
        journal={"batches": {}},
        prepare=lambda *a, **k: pytest.fail("must not prepare"),
    )
    with pytest.raises(m.PublicationRecoveryPending):
        m.publish_stage(port, stage="foundation", assignment_epoch=3, receipt_only=True)
    prepared = driver.prepare("foundation", base_version=4)
    driver.confirm(prepared)
    driver.prepare = lambda *a, **k: pytest.fail("must not prepare twice")
    assert m.publish_stage(driver, stage="foundation", assignment_epoch=3)["status"] == "succeeded"


def test_execute_factory_rejects_f4_purpose_before_opening_any_resources(tmp_path):
    from datetime import UTC, datetime
    from types import SimpleNamespace as N

    m, driver, _, _ = setup(tmp_path)
    now = datetime.now(UTC)
    with pytest.raises(ValueError, match="F5 configuration"):
        m.create_formal_f5_port(
            raw=driver.raw,
            approved_input_sha256=driver.input_sha256,
            configuration=N(purpose="school-facts-f4-synthetic", education=object()),
            grant_epoch=now,
            fixed_deadline=now + timedelta(days=7),
            execute=True,
        )
