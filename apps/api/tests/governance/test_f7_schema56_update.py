"""The bounded 55→56 repair preserves configuration and historical transitions."""

from copy import deepcopy
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def scripts_path(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[4]))


def test_only_two_reviewed_transitions_are_supported():
    from scripts.f7_engineering import migration as m

    m._transition("20260922_45", "20260924_55")
    m._transition("20260924_55", "20260924_56")
    for pair in (
        ("20260922_45", "20260924_56"),
        ("20260924_55", "20260924_55"),
        ("20260924_54", "20260924_56"),
        ("20260924_56", "20260924_55"),
    ):
        with pytest.raises(ValueError):
            m._transition(*pair)


@pytest.mark.parametrize("product", [True, False])
def test_schema_repair_changes_only_matching_contract_fields(product):
    from scripts.f7_engineering import runtime_update as r

    before = {
        "reader": {"schema_contract": "school-facts-f7-v55"},
        "f7_packages_enabled": True,
        "f5_features_enabled": True,
        "f6_features_enabled": True,
        "scope_bindings": ["retained"],
        "credential": "synthetic-marker",
        "ledger": "retained",
    }
    owner, field = (
        ("runtime", "postgres_schema_contract") if product else ("writer", "schema_contract")
    )
    before[owner] = {field: "school-facts-f7-v55"}
    if product:
        before["purpose"] = "school-facts-f7-synthetic"
    after = deepcopy(before)
    after["reader"]["schema_contract"] = "school-facts-f7-v56"
    after[owner][field] = "school-facts-f7-v56"
    r.require_schema_repair_config_delta(before, after, product=product)
    for field_name, value in (
        ("scope_bindings", ["expanded"]),
        ("credential", "changed"),
        ("ledger", "reset"),
        ("f7_packages_enabled", False),
    ):
        with pytest.raises(ValueError):
            r.require_schema_repair_config_delta(
                before, after | {field_name: value}, product=product
            )
    with pytest.raises(ValueError):
        r.require_schema_repair_config_delta(after, before, product=product)


def test_snapshot_accepts55_only_for_reviewed56_upgrade():
    from scripts.f7_engineering import migration as m

    from tests.governance.test_f7_migration_tools import _snapshot

    snapshot = _snapshot() | {"schema": "20260924_55"}
    m.require_snapshot(snapshot, expected_schema="20260924_55")
    with pytest.raises(ValueError):
        m.require_snapshot(snapshot, expected_schema="20260922_45")


def test_new_contract_does_not_relabel_55():
    from app.db.schema_contract import schema_contract_head

    assert schema_contract_head("school-facts-f7-v56") == "20260924_56"
    assert schema_contract_head("school-facts-f7-v55") == "20260924_55"
