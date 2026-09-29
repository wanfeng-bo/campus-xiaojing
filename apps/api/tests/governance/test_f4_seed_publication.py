"""Offline seed input immutability and recovery contracts."""

import importlib.util
import json
import sys
from datetime import timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[4]
sys.path.insert(0, str(ROOT / "scripts/f4_engineering"))
sys.path.insert(0, str(Path(__file__).parent))
from test_f3_formal_writer_adapter import NOW, Boundary, Guard, actor  # noqa: E402

from app.composition.school_fact_product_identity import ProductIdentity  # noqa: E402


def setup(tmp_path):
    import seed_publication as m

    spec = importlib.util.spec_from_file_location(
        "seed_prepare_test", ROOT / "scripts/f4_engineering/prepare_runtime.py"
    )
    prep = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(prep)
    raw = (
        (
            json.dumps(
                prep.build_plan(member_at="2026-09-13T13:00:00+00:00"), ensure_ascii=False, indent=2
            )
            + "\n"
        )
        .replace("\n", "\r\n")
        .encode()
    )
    b = Boundary()
    identity = ProductIdentity(actor(), "a" * 64, "test-ref")
    driver = m.SeedPublication(
        raw=raw,
        journal_path=tmp_path / "journal.json",
        identity=lambda: identity,
        expected_school=identity.actor.school_id,
        expected_assignment=identity.actor.assignment_id,
        boundary=b,
        guard=Guard(),
        fixed_deadline=NOW + timedelta(hours=1),
        clock=lambda: NOW,
    )
    return m, driver, b


def test_seed_preserves_dates_and_unknown_is_receipt_only(tmp_path):
    m, driver, b = setup(tmp_path)
    prepared = driver.prepare("foundation", base_version=22)
    assert prepared.change_set.base_version == 22
    assert prepared.change_set.changes[0].effective_at.year == 2023
    b.unknown = True
    with pytest.raises(m.SeedRecoveryPending):
        driver.confirm(prepared)
    assert driver.receipt("foundation").target_version == 23
    with pytest.raises(m.SeedRecoveryPending):
        driver.prepare("foundation", base_version=23)
    assert len([x for x in b.calls if x[0] == "confirm"]) == 1


def test_seed_rejects_altered_input_and_unpublished_predecessor(tmp_path):
    m, driver, b = setup(tmp_path)
    with pytest.raises(m.SeedRecoveryPending):
        driver.prepare("future_registration", base_version=22)
    with pytest.raises(ValueError):
        m.validate_input(driver.raw.replace(b"2023", b"2024"))
    assert b.calls == []


def test_seed_factory_review_never_opens_configuration(tmp_path):
    from seed_factory import create_formal_f4_port

    _, driver, _ = setup(tmp_path)
    result = create_formal_f4_port(
        configuration=None,
        raw=driver.raw,
        journal_path=tmp_path / "unused",
        fixed_deadline=None,
        expected_assignment=None,
        session_tokens=None,
    )
    assert result["mutations"] == 0
    assert result["stages"][0] == "foundation"


def test_seed_roundtrip_preserves_explicit_fields(tmp_path):
    _, driver, _ = setup(tmp_path)
    from app.platform.school_facts.contracts import CanonicalFactChangeSetV1

    for stage in driver.stages.values():
        reparsed = CanonicalFactChangeSetV1.model_validate_json(
            stage.model_dump_json(exclude_unset=True)
        )
        assert reparsed == stage
        assert [c.content.model_fields_set if c.content else None for c in reparsed.changes] == [
            c.content.model_fields_set if c.content else None for c in stage.changes
        ]


def test_seed_owner_guard_rejects_mixed_epochs(tmp_path):
    from types import SimpleNamespace as N

    from seed_factory import SeedOwnerGuard

    _, driver, _ = setup(tmp_path)

    def proof(epoch):
        return N(basis=N(governance=N(assignment_epoch=epoch)))

    components = N(
        family=N(authority=N(require_change_set=lambda *a, **kw: N(item_permits=(proof(2),)))),
        dependency_directory=N(guard=N(require_change_set=lambda *a, **kw: (proof(1),))),
    )
    guard = SeedOwnerGuard(components)
    with pytest.raises(PermissionError, match="epochs differ"):
        guard.require_change_set(actor(), driver.stages["foundation"], checked_at=NOW)
