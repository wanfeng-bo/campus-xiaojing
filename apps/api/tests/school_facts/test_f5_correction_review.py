from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from app.domains.students.family_corrections import (
    FamilyCorrectionDenied,
    FamilyCorrectionResolutionTarget,
    FamilyCorrectionService,
)


def fixture():
    repo, guardian, staff, receipts = (MagicMock() for _ in range(4))
    actor = SimpleNamespace(school_id=uuid4(), person_id=uuid4(), assignment_id=uuid4())
    target = FamilyCorrectionResolutionTarget(
        uuid4(),
        actor.school_id,
        "teaching_assignment",
        "teaching:1",
        "assignment_source_key",
        "submitted",
        datetime.now(UTC),
        1,
    )
    repo.get_target.return_value = target
    repo.current_target_version.return_value = 2
    service = FamilyCorrectionService(
        repository=repo,
        guardian_authority=guardian,
        staff_authority=staff,
        receipt_verifier=receipts,
        clock=lambda: datetime.now(UTC),
    )
    return service, repo, staff, receipts, actor, target


def test_unchanged_review_needs_explicit_confirmation_and_is_not_applied_receipt():
    service, repo, staff, receipts, actor, target = fixture()
    kwargs = dict(
        actor=actor,
        report_id=target.report_id,
        explanation="已核对正式任课安排，原资料无误。",
        expected_target_version=2,
        command_sha256="a" * 64,
        action="maintain",
    )
    with pytest.raises(FamilyCorrectionDenied):
        service.review_unchanged(**kwargs, confirmed=False)
    repo.review_unchanged.assert_not_called()
    service.review_unchanged(**kwargs, confirmed=True)
    repo.review_unchanged.assert_called_once()
    receipts.require_applied_target.assert_not_called()
    assert staff.require_action.call_args.kwargs["action"] == "maintain"


@pytest.mark.parametrize("change", ["version", "authorization", "empty_explanation"])
def test_review_does_not_write_after_preview_or_authority_change(change):
    service, repo, staff, receipts, actor, target = fixture()
    if change == "version":
        repo.current_target_version.return_value = 3
    if change == "authorization":
        staff.require_action.side_effect = FamilyCorrectionDenied("revoked")
    with pytest.raises(FamilyCorrectionDenied):
        service.review_unchanged(
            actor=actor,
            report_id=target.report_id,
            explanation="" if change == "empty_explanation" else "已核对，原资料无误。",
            expected_target_version=2,
            command_sha256="a" * 64,
            confirmed=True,
            action="maintain",
        )
    repo.review_unchanged.assert_not_called()


def test_old_resolved_report_cannot_be_relabelled_unchanged():
    service, repo, staff, receipts, actor, target = fixture()
    repo.get_target.return_value = replace(target, status="resolved")
    with pytest.raises(FamilyCorrectionDenied):
        service.review_unchanged(
            actor=actor,
            report_id=target.report_id,
            explanation="原资料无误",
            expected_target_version=2,
            command_sha256="a" * 64,
            confirmed=True,
            action="maintain",
        )
    repo.review_unchanged.assert_not_called()


def test_review_confirmation_token_binds_actor_session_payload_and_expiry():
    from datetime import timedelta

    from app.composition.school_fact_family_correction_writer import CorrectionReviewCodec
    from tests.school_facts.test_f4_education_reader import actor as staff_actor

    actor = staff_actor()
    now = datetime.now(UTC)
    codec = CorrectionReviewCodec(b"x" * 32)
    token = codec.encode(
        actor=actor,
        session_binding="session:1",
        report_id=uuid4(),
        target_version=2,
        explanation="原资料无误",
        action="maintain",
        expires_at=now + timedelta(minutes=5),
    )
    assert (
        codec.decode(actor=actor, session_binding="session:1", token=token, now=now)[
            "target_version"
        ]
        == 2
    )
    for binding, at in [("session:2", now), ("session:1", now + timedelta(minutes=6))]:
        with pytest.raises(FamilyCorrectionDenied):
            codec.decode(actor=actor, session_binding=binding, token=token, now=at)
    with pytest.raises(FamilyCorrectionDenied):
        codec.decode(actor=actor, session_binding="session:1", token=token + "forged", now=now)


def test_review_schema_retains_distinct_status_and_audit_columns():
    from tests.school_facts.test_f5_family_leadership_postgres import migration

    sql = migration().REVIEW_SQL
    for name in (
        "review_explanation",
        "reviewer_person_id",
        "reviewer_assignment_id",
        "review_command_sha256",
        "review_target_version",
    ):
        assert name in sql
    assert "reviewed" in sql
    assert "immutable" in sql


def test_review_result_requires_current_authority_but_not_unchanged_future_fact_version():
    service, repo, staff, receipts, actor, target = fixture()
    repo.get_target.return_value = replace(target, status="reviewed")
    result = SimpleNamespace(status="reviewed")
    repo.review_result.return_value = result
    assert (
        service.review_result(
            actor=actor, report_id=target.report_id, command_sha256="a" * 64, action="maintain"
        )
        is result
    )
    repo.current_target_version.assert_not_called()
    staff.require_action.side_effect = FamilyCorrectionDenied("revoked")
    with pytest.raises(FamilyCorrectionDenied):
        service.review_result(
            actor=actor, report_id=target.report_id, command_sha256="a" * 64, action="maintain"
        )


def test_review_result_codec_can_lookup_original_after_confirmation_expires():
    from datetime import timedelta

    from app.composition.school_fact_family_correction_writer import CorrectionReviewCodec
    from tests.school_facts.test_f4_education_reader import actor as staff_actor

    actor = staff_actor()
    now = datetime.now(UTC)
    codec = CorrectionReviewCodec(b"x" * 32)
    token = codec.encode(
        actor=actor,
        session_binding="s",
        report_id=uuid4(),
        target_version=2,
        explanation="原资料无误",
        action="maintain",
        expires_at=now,
    )
    assert (
        codec.decode(
            actor=actor,
            session_binding="s",
            token=token,
            now=now + timedelta(days=1),
            for_result=True,
        )["explanation"]
        == "原资料无误"
    )


def test_old_schema_projection_does_not_reference_new_review_column():
    from app.composition.school_fact_family_correction_writer import SqlFamilyCorrectionRepository

    repository = SqlFamilyCorrectionRepository(None, review_enabled=False)
    assert "NULL::text AS review_explanation" in repository._columns
    assert "resolved_at, review_explanation" not in repository._columns
    assert "statement," not in repository._receipt_columns
