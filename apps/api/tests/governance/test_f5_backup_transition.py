"""New backup transitions must still fail closed before any dump or output file."""

import importlib.util
import sys
from pathlib import Path

import pytest


@pytest.fixture
def helper(monkeypatch):
    root = Path(__file__).resolve().parents[4]
    monkeypatch.syspath_prepend(str(root / "scripts/f5_engineering"))
    spec = importlib.util.spec_from_file_location(
        "f5_backup_transition", root / "scripts/f5_engineering/backup_original.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def snapshot():
    return {
        "system_identifier": "7682019857215991846",
        "database": "campus_ai_school_facts_f1",
        "schema": "20260914_35",
        "anchor": {"school_id": "c1137be2-0734-4b69-8937-37dbd741f141"},
        "applying": 0,
        "active_reference": 0,
        "writer_active": 0,
        "prepared": 0,
        "governance_login": False,
        "governance_connections": 0,
    }


def test_explicit_35_backup_keeps_exact_identity_and_quiescence_guard(helper):
    helper.require_snapshot(snapshot(), expected_schema="20260914_35")
    with pytest.raises(ValueError):
        helper.require_snapshot(snapshot(), expected_schema="20260913_31")


@pytest.mark.parametrize(
    "field,value",
    [
        ("database", "other_project"),
        ("system_identifier", "other_cluster"),
        ("governance_login", True),
        ("governance_connections", 1),
        ("writer_active", 1),
        ("prepared", 1),
        ("applying", 1),
        ("active_reference", 1),
        ("schema", "20260914_36"),
    ],
)
def test_unsafe_backup_never_reaches_dump(helper, field, value):
    current = snapshot()
    current[field] = value
    with pytest.raises(ValueError):
        helper.require_snapshot(current, expected_schema="20260914_35")


def test_unapproved_revision_and_missing_guard_rejected(helper):
    with pytest.raises(ValueError):
        helper.require_snapshot(snapshot(), expected_schema="20260914_36")
    current = snapshot()
    del current["governance_login"]
    with pytest.raises((KeyError, ValueError)):
        helper.require_snapshot(current, expected_schema="20260914_35")


def test_explicit36_backup_preserves_old_default_and_all_guards(helper):
    current = snapshot() | {"schema": "20260914_36"}
    helper.require_snapshot(current, expected_schema="20260914_36")
    with pytest.raises(ValueError):
        helper.require_snapshot(current, expected_schema="20260913_31")
    for field in (
        "governance_login",
        "governance_connections",
        "writer_active",
        "prepared",
        "applying",
        "active_reference",
    ):
        with pytest.raises(ValueError):
            helper.require_snapshot(current | {field: 1}, expected_schema="20260914_36")
    with pytest.raises(ValueError):
        helper.require_snapshot(current, expected_schema="20260914_37")


@pytest.mark.parametrize("revision", ["20260914_38", "20260914_39", "20260914_40", "20260914_41"])
def test_explicit_cancellation_revision_backup_remains_fail_closed(helper, revision):
    current = snapshot() | {"schema": revision}
    helper.require_snapshot(current, expected_schema=revision)
    for field in (
        "governance_login",
        "governance_connections",
        "writer_active",
        "prepared",
        "applying",
        "active_reference",
    ):
        with pytest.raises(ValueError):
            helper.require_snapshot(current | {field: 1}, expected_schema=revision)
    with pytest.raises(ValueError):
        helper.require_snapshot(current, expected_schema="20260913_31")
