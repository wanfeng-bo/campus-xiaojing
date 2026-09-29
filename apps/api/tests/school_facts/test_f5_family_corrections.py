from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.composition.school_fact_family_correction_product import PublicCorrectionOption
from app.composition.school_fact_family_correction_writer import (
    ExactCorrectionReceiptVerifier,
    SqlAppliedFieldDifferenceReader,
    StaffCorrectionAuthorityAdapter,
)
from app.domains.students.family_corrections import FamilyCorrectionDenied


@pytest.mark.parametrize(
    "field",
    [
        "display_name",
        "position_title",
        "person_source_key",
        "organization_source_key",
        "contact_value",
        "contact_kind",
        "service_hours",
        "course_source_key",
        "subject_source_key",
        "offering_source_key",
        "group_source_key",
        "teaching_role",
        "offering_status",
        "source_kind",
        "valid_to",
    ],
)
def test_f5_public_correction_fields_are_supported(field):
    assert (
        PublicCorrectionOption(
            field_name=field, label="已展示字段", reference="signed:reference"
        ).field_name
        == field
    )


def test_own_group_member_expiry_target_is_a_finite_f5_correction_field():
    from app.composition.school_fact_family_correction_writer import F5_CORRECTION_FIELDS
    assert ("teaching_group_member", "valid_to") in F5_CORRECTION_FIELDS
    assert ("person", "valid_to") not in F5_CORRECTION_FIELDS


@pytest.mark.parametrize(
    "kind,field,function",
    [
        ("person", "name", "school_fact_f3_field_changed"),
        ("person", "display_name", "school_fact_f2_family_field_changed"),
        ("assignment", "position_title", "school_fact_f2_family_field_changed"),
        ("official_contact", "contact_value", "school_fact_f2_family_field_changed"),
        ("teaching_assignment", "assignment_source_key", "school_fact_f5_field_changed"),
    ],
)
def test_difference_sql_stays_with_fact_owner(kind, field, function):
    session = MagicMock()
    session.scalar.return_value = True
    from uuid import uuid4

    SqlAppliedFieldDifferenceReader(session=session, school_id=uuid4()).changed(
        fact_kind=kind, source_key="source:1", target_version=2, field_name=field
    )
    assert function in str(session.scalar.call_args.args[0])


@pytest.mark.parametrize("operation", ["correct", "cancel", "end"])
def test_teaching_resolution_needs_exact_real_field_difference(operation):
    own, differences = MagicMock(), MagicMock()
    own.check.return_value = SimpleNamespace(
        targets=(
            SimpleNamespace(
                fact_kind="teaching_assignment",
                source_key="teach:1",
                operation=operation,
                target_version=2,
            ),
        )
    )
    differences.changed.return_value = True
    verifier = ExactCorrectionReceiptVerifier(own_results=own, differences=differences)
    kwargs = dict(
        actor=MagicMock(),
        locator_sha256="a" * 64,
        fact_kind="teaching_assignment",
        source_key="teach:1",
        field_name="assignment_source_key",
        minimum_target_version=1,
        submitted_at=datetime.now(UTC),
    )
    assert verifier.require_applied_target(**kwargs) == "maintain"
    differences.changed.return_value = False
    with pytest.raises(FamilyCorrectionDenied):
        verifier.require_applied_target(**kwargs)


def test_teaching_authority_never_falls_through_to_f3():
    old, education = MagicMock(), MagicMock()
    adapter = StaffCorrectionAuthorityAdapter(old, education)
    adapter.require_action(
        actor=MagicMock(),
        fact_kind="teaching_assignment",
        source_key="teach:1",
        field_name="assignment_source_key",
        action="maintain",
        checked_at=datetime.now(UTC),
    )
    old.require_scope.assert_not_called()
    assert education.require_scope.call_args.kwargs["action"] == "maintain"


def test_f2_leader_authority_is_not_f3_person_and_fails_closed_without_f2():
    old = MagicMock()
    adapter = StaffCorrectionAuthorityAdapter(old)
    with pytest.raises(FamilyCorrectionDenied):
        adapter.require_action(
            actor=MagicMock(),
            fact_kind="person",
            source_key="leader:1",
            field_name="display_name",
            action="maintain",
            checked_at=datetime.now(UTC),
        )
    old.require_scope.assert_not_called()


def test_fixed_f5_and_leadership_options_revalidate_signed_target_before_submit():
    from app.composition.school_fact_family_correction_writer import (
        FamilyCorrectionReferenceCodec,
        SqlGuardianCorrectionAuthority,
    )
    from app.platform.school_facts.family_leadership_policy import FamilyLeadershipPolicy
    from tests.test_f3_family_read import setup

    _, actor, state = setup()
    policy = FamilyLeadershipPolicy(
        school_id=actor.school_id,
        publication_reference="formal:role",
        version=1,
        school_role_codes=("formal_001",),
        campus_role_codes=(),
    )
    target = {
        "field_name": "display_name",
        "label": "校长姓名",
        "fact_kind": "person",
        "fact_source_key": "leader:1",
        "target_version": 1,
        "student_id": state.basis.student_id,
    }

    class Session:
        rows = [target]

        def execute(self, sql, params):
            if "leadership_targets" in str(sql):
                assert params["school_codes"] == ["formal_001"]
                self.result = list(self.rows)
            else:
                self.result = []
            return self

        def mappings(self):
            return self

        def all(self):
            return self.result

    session = Session()
    authority = SqlGuardianCorrectionAuthority(
        session=session,
        codec=FamilyCorrectionReferenceCodec(b"x" * 32),
        leadership_policy=lambda: policy,
        f5_enabled=True,
    )
    (option,) = authority.options(actor)
    permit = authority.require_submit(
        actor=actor,
        target_reference=option.reference,
        field_name="display_name",
        checked_at=datetime.now(UTC),
    )
    assert permit.fact_kind == "person"
    assert permit.student_id == state.basis.student_id
    session.rows = [target | {"target_version": 2}]
    with pytest.raises(FamilyCorrectionDenied):
        authority.require_submit(
            actor=actor,
            target_reference=option.reference,
            field_name="display_name",
            checked_at=datetime.now(UTC),
        )
    session.rows = [target | {"field_name": "approval_reference"}]
    with pytest.raises(FamilyCorrectionDenied):
        authority.options(actor)


def test_f2_maintenance_callback_is_not_used_for_read_action():
    f2 = MagicMock()
    adapter = StaffCorrectionAuthorityAdapter(MagicMock(), f2_authority=f2)
    with pytest.raises(FamilyCorrectionDenied):
        adapter.require_action(
            actor=MagicMock(),
            fact_kind="assignment",
            source_key="leader:1",
            field_name="position_title",
            action="read",
            checked_at=datetime.now(UTC),
        )
    f2.require_action.assert_not_called()
