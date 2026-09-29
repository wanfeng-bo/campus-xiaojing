from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.composition.school_fact_family_correction_writer import (
    ExactCorrectionReceiptVerifier,
    StaffCorrectionAuthorityAdapter,
)
from app.domains.students.family_corrections import FamilyCorrectionDenied


def verifier(operation="upsert", changed=True):
    own = MagicMock()
    own.check.return_value = SimpleNamespace(
        targets=(
            SimpleNamespace(
                fact_kind="student", source_key="student:1", operation=operation, target_version=2
            ),
        )
    )
    differences = MagicMock()
    differences.changed.return_value = changed
    return ExactCorrectionReceiptVerifier(own_results=own, differences=differences), differences


def test_exact_field_difference_is_required():
    checked, differences = verifier(changed=True)
    assert (
        checked.require_applied_target(
            actor=MagicMock(),
            locator_sha256="a" * 64,
            fact_kind="student",
            source_key="student:1",
            field_name="status",
            minimum_target_version=1,
            submitted_at=datetime.now(UTC),
        )
        == "maintain"
    )
    differences.changed.assert_called_once_with(
        fact_kind="student", source_key="student:1", target_version=2, field_name="status"
    )


@pytest.mark.parametrize("operation,changed", [("upsert", False), ("end", True)])
def test_empty_or_unrelated_update_cannot_resolve(operation, changed):
    checked, _ = verifier(operation=operation, changed=changed)
    with pytest.raises(FamilyCorrectionDenied):
        checked.require_applied_target(
            actor=MagicMock(),
            locator_sha256="a" * 64,
            fact_kind="student",
            source_key="student:1",
            field_name="status",
            minimum_target_version=1,
            submitted_at=datetime.now(UTC),
        )


def test_relationship_requires_terminal_receipt():
    checked, _ = verifier(operation="end")
    checked._own_results.check.return_value = SimpleNamespace(
        targets=(
            SimpleNamespace(
                fact_kind="guardian_relationship",
                source_key="rel:1",
                operation="end",
                target_version=2,
            ),
        )
    )
    assert (
        checked.require_applied_target(
            actor=MagicMock(),
            locator_sha256="a" * 64,
            fact_kind="guardian_relationship",
            source_key="rel:1",
            field_name="relationship",
            minimum_target_version=1,
            submitted_at=datetime.now(UTC),
        )
        == "end"
    )


def test_receipt_must_be_later_and_target_newer_than_report():
    checked, _ = verifier(changed=True)
    checked._own_results.check.return_value.confirmed_at = datetime(2026, 1, 1, tzinfo=UTC)
    with pytest.raises(FamilyCorrectionDenied):
        checked.require_applied_target(
            actor=MagicMock(),
            locator_sha256="a" * 64,
            fact_kind="student",
            source_key="student:1",
            field_name="status",
            minimum_target_version=2,
            submitted_at=datetime(2026, 1, 2, tzinfo=UTC),
        )


def test_staff_logical_fields_require_exact_physical_scope_fields():
    authority = MagicMock()
    authority.require_scope.return_value = SimpleNamespace(
        scope=SimpleNamespace(field_names=("district_code",))
    )
    adapter = StaffCorrectionAuthorityAdapter(authority)
    with pytest.raises(FamilyCorrectionDenied):
        adapter.require_action(
            actor=MagicMock(),
            fact_kind="person_region",
            source_key="region:1",
            field_name="region",
            action="maintain",
            checked_at=datetime.now(UTC),
        )
    authority.require_scope.return_value = SimpleNamespace(
        scope=SimpleNamespace(field_names=("district_code", "district_name"))
    )
    adapter.require_action(
        actor=MagicMock(),
        fact_kind="person_region",
        source_key="region:1",
        field_name="region",
        action="maintain",
        checked_at=datetime.now(UTC),
    )
