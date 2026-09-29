from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from app.domains.students.family_corrections import (
    FamilyCorrectionDenied,
    FamilyCorrectionReport,
    FamilyCorrectionService,
    GuardianCorrectionPermit,
)
from app.platform.contracts import _issue_guardian_actor_context, _issue_staff_actor_context


def guardian():
    return _issue_guardian_actor_context(
        account_user_id=1,
        school_id=uuid4(),
        person_id=uuid4(),
        guardian_relationship_id=uuid4(),
        relationship_version=1,
        authorization_version=1,
    )


def test_submit_revalidates_non_authoritative_repository_command():
    a = guardian()
    auth = MagicMock()
    p = GuardianCorrectionPermit(
        a, uuid4(), a.guardian_relationship_id, "student", "student:1", "status", datetime.now(UTC)
    )
    auth.require_submit.return_value = p
    row = MagicMock()
    repo = MagicMock()
    repo.submit.return_value = row
    s = FamilyCorrectionService(
        repository=repo,
        guardian_authority=auth,
        staff_authority=MagicMock(),
        receipt_verifier=MagicMock(),
        clock=lambda: datetime.now(UTC),
    )
    assert (
        s.submit(
            actor=a,
            target_reference="opaque",
            field_name="status",
            statement=" wrong ",
            idempotency_key="k",
        )
        is row
    )
    auth.revalidate.assert_called_once()


def test_resolve_requires_same_target_field_receipt_and_fresh_authority():
    a = guardian()
    staff = _issue_staff_actor_context(
        account_user_id=2,
        school_id=a.school_id,
        person_id=uuid4(),
        assignment_id=uuid4(),
        assignment_version=1,
        authorization_version=1,
    )
    row = FamilyCorrectionReport(
        uuid4(),
        a.school_id,
        a.person_id,
        a.guardian_relationship_id,
        uuid4(),
        "student",
        "student:1",
        "status",
        "wrong",
        "submitted",
        datetime.now(UTC),
    )
    repo = MagicMock()
    repo.get_target.return_value = row
    repo.resolve.return_value = SimpleNamespace(status="resolved")
    authority = MagicMock()
    receipt = MagicMock()
    receipt.require_applied_target.return_value = "maintain"
    s = FamilyCorrectionService(
        repository=repo,
        guardian_authority=MagicMock(),
        staff_authority=authority,
        receipt_verifier=receipt,
        clock=lambda: datetime.now(UTC),
    )
    assert (
        s.resolve(actor=staff, report_id=row.report_id, locator_sha256="a" * 64).status
        == "resolved"
    )
    assert authority.require_action.call_count == 2
    receipt.require_applied_target.assert_called_once()


def test_invalid_and_missing_fail_closed():
    a = guardian()
    repo = MagicMock()
    repo.own.return_value = None
    s = FamilyCorrectionService(
        repository=repo,
        guardian_authority=MagicMock(),
        staff_authority=MagicMock(),
        receipt_verifier=MagicMock(),
        clock=lambda: datetime.now(UTC),
    )
    with pytest.raises(FamilyCorrectionDenied):
        s.submit(
            actor=a, target_reference="x", field_name="status", statement=" ", idempotency_key="k"
        )
    with pytest.raises(FamilyCorrectionDenied):
        s.own(actor=a, report_id=uuid4())


def test_relationship_resolution_rejects_generic_maintain():
    a = guardian()
    staff = _issue_staff_actor_context(
        account_user_id=2,
        school_id=a.school_id,
        person_id=uuid4(),
        assignment_id=uuid4(),
        assignment_version=1,
        authorization_version=1,
    )
    row = FamilyCorrectionReport(
        uuid4(),
        a.school_id,
        a.person_id,
        a.guardian_relationship_id,
        uuid4(),
        "guardian_relationship",
        "relationship:1",
        "relationship",
        "wrong child",
        "submitted",
        datetime.now(UTC),
    )
    repo = MagicMock()
    repo.get_target.return_value = row
    receipt = MagicMock()
    receipt.require_applied_target.return_value = "maintain"
    service = FamilyCorrectionService(
        repository=repo,
        guardian_authority=MagicMock(),
        staff_authority=MagicMock(),
        receipt_verifier=receipt,
        clock=lambda: datetime.now(UTC),
    )
    with pytest.raises(FamilyCorrectionDenied):
        service.resolve(actor=staff, report_id=row.report_id, locator_sha256="a" * 64)
    repo.resolve.assert_not_called()
