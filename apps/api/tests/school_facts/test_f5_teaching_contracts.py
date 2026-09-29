from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.platform.school_facts.contracts import CanonicalFactChangeV1


def change(kind, **content):
    return CanonicalFactChangeV1.model_validate(
        dict(
            schema_version=3,
            fact_kind=kind,
            operation="upsert",
            source_key="test:one",
            expected_version=0,
            effective_at=datetime(2026, 9, 1, tzinfo=UTC),
            origin={"sheet_code": "teaching", "row_number": 1},
            content=content,
        )
    )


def test_open_teaching_assignment_has_explicit_subjects_and_role():
    item = change(
        "teaching_assignment",
        offering_source_key="offer:one",
        assignment_source_key="staff:one",
        teaching_role="lead",
        subject_source_keys=("subject:math",),
    )
    assert item.content.valid_to is None
    assert item.content.subject_source_keys == ("subject:math",)


@pytest.mark.parametrize(
    "source_kind,field",
    [("administrative_class", "class_source_key"), ("independent_group", "group_source_key")],
)
def test_source_requires_exactly_its_declared_authority(source_kind, field):
    base = dict(offering_source_key="offer:one", source_kind=source_kind)
    assert change("offering_member_source", **base, **{field: "source:one"})
    with pytest.raises(ValidationError):
        change("offering_member_source", **base)
    with pytest.raises(ValidationError):
        change(
            "offering_member_source",
            **base,
            class_source_key="class:one",
            group_source_key="group:one",
        )


def test_teaching_vocabulary_is_separate_from_f4():
    from app.domains.students.education_contracts import EDUCATION_KINDS, F4_EDUCATION_KINDS
    from app.domains.students.teaching_contracts import TEACHING_KINDS

    assert len(F4_EDUCATION_KINDS) == 17
    assert len(TEACHING_KINDS) == 9
    assert not F4_EDUCATION_KINDS & TEACHING_KINDS
    assert EDUCATION_KINDS == F4_EDUCATION_KINDS | TEACHING_KINDS


def test_group_uses_organization_name_and_rejects_copied_name():
    assert change("teaching_group", organization_source_key="org:one", group_mode="shared")
    with pytest.raises(ValidationError):
        change(
            "teaching_group",
            organization_source_key="org:one",
            group_mode="shared",
            display_name="copied",
        )


def test_scope_has_one_explicit_dimension():
    assert change(
        "teaching_organization_scope",
        organization_source_key="org:one",
        subject_source_key="subject:math",
        scope_basis="stage",
        stage_source_key="stage:junior",
    )
    with pytest.raises(ValidationError):
        change(
            "teaching_organization_scope",
            organization_source_key="org:one",
            subject_source_key="subject:math",
            scope_basis="stage",
            grade_source_key="grade:one",
        )


def test_duplicate_subjects_and_unregistered_fields_are_rejected():
    with pytest.raises(ValidationError):
        change(
            "teaching_subject", display_name="数学", stage_source_keys=("stage:one", "stage:one")
        )
    with pytest.raises(ValidationError):
        change("teaching_course", display_name="数学", automatic_promotion=True)


def test_cross_source_duplicates_and_role_limits_are_batch_postconditions():
    from app.domains.students.education_validation import EducationRecord
    from app.domains.students.teaching_contracts import validate_teaching_rules

    at = datetime(2026, 9, 1, tzinfo=UTC)

    def row(kind, key, **content):
        return EducationRecord(kind, key, at, None, content)

    offering = row(
        "teaching_offering", "offer:one", course_source_key="course:one", offering_status="active"
    )
    source = row(
        "offering_member_source",
        "source:one",
        offering_source_key="offer:one",
        source_kind="administrative_class",
        class_source_key="class:one",
    )
    a = row(
        "teaching_assignment",
        "assign:one",
        offering_source_key="offer:one",
        assignment_source_key="staff:one",
        teaching_role="lead",
    )
    b = row(
        "teaching_assignment",
        "assign:two",
        offering_source_key="offer:one",
        assignment_source_key="staff:two",
        teaching_role="lead",
    )
    with pytest.raises(ValueError):
        validate_teaching_rules((offering, source, a, b))
    configured = row(
        "teaching_offering",
        "offer:one",
        course_source_key="course:one",
        offering_status="active",
        teaching_role_limits={"lead": 2},
    )
    validate_teaching_rules((configured, source, a, b))
    with pytest.raises(ValueError):
        validate_teaching_rules(
            (
                configured,
                source,
                source.__class__(source.kind, "source:two", at, None, source.content),
            )
        )


def test_missing_version_type_and_reference_type_are_not_orm_metadata_gaps():
    from app.domains.students.education_models import EducationReference, EducationVersion

    assert "fact_kind" in EducationVersion.__table__.columns
    assert "education_kind" in EducationReference.__table__.columns


def test_f4_class_end_must_include_dependent_teaching_source_and_offering():
    from types import SimpleNamespace
    from uuid import uuid4

    from app.composition.school_fact_persistence import SqlAlchemySchoolFactPersistence
    from app.platform.school_facts.application import SchoolFactChangeRejected

    at = datetime(2026, 9, 1, tzinfo=UTC)
    until = datetime(2026, 10, 1, tzinfo=UTC)

    def row(kind, key, **payload):
        return kind, key, SimpleNamespace(valid_from=at, valid_to=None, payload=payload)

    rows = [
        row("teaching_course", "course:one", display_name="课程"),
        row("education_class", "class:one", display_name="班"),
        row(
            "teaching_offering",
            "offer:one",
            course_source_key="course:one",
            offering_status="active",
        ),
        row(
            "offering_member_source",
            "source:one",
            offering_source_key="offer:one",
            source_kind="administrative_class",
            class_source_key="class:one",
        ),
    ]

    class ReadOnly:
        def execute(self, statement):
            assert statement.is_select
            return SimpleNamespace(all=lambda: rows)

    def end(kind, key):
        return CanonicalFactChangeV1.model_validate(
            dict(
                schema_version=3,
                fact_kind=kind,
                source_key=key,
                operation="end",
                expected_version=1,
                effective_at=until,
                content=None,
                origin={"sheet_code": "teaching", "row_number": 1},
            )
        )

    with pytest.raises(SchoolFactChangeRejected):
        SqlAlchemySchoolFactPersistence._validate_education_state(
            ReadOnly(), uuid4(), proposed_changes=(end("education_class", "class:one"),)
        )
    SqlAlchemySchoolFactPersistence._validate_education_state(
        ReadOnly(),
        uuid4(),
        proposed_changes=(
            end("education_class", "class:one"),
            end("teaching_offering", "offer:one"),
            end("offering_member_source", "source:one"),
        ),
    )


def test_array_references_participate_in_original_batch_topological_order():
    from types import SimpleNamespace

    from app.composition.school_fact_persistence import SqlAlchemySchoolFactPersistence

    subject = change("teaching_subject", display_name="数学", stage_source_keys=("test:one",))
    stage = change("education_stage", display_name="初中")
    pairs = SqlAlchemySchoolFactPersistence._ordered_change_pairs(
        (SimpleNamespace(), SimpleNamespace()), SimpleNamespace(changes=(subject, stage)), {}
    )
    assert [c.fact_kind for _, c in pairs] == ["education_stage", "teaching_subject"]


def test_teaching_subject_restrictions_must_exist_for_whole_assignment_period():
    from app.domains.students.education_validation import EducationRecord
    from app.domains.students.teaching_contracts import validate_teaching_rules

    at = datetime(2026, 9, 1, tzinfo=UTC)
    until = datetime(2026, 10, 1, tzinfo=UTC)

    def row(kind, key, end=None, **content):
        return EducationRecord(kind, key, at, end, content)

    rows = (
        row(
            "teaching_offering",
            "offer:one",
            course_source_key="course:one",
            offering_status="active",
        ),
        row(
            "offering_member_source",
            "source:one",
            offering_source_key="offer:one",
            class_source_key="class:one",
            source_kind="administrative_class",
        ),
        row(
            "course_subject",
            "link:one",
            end=until,
            course_source_key="course:one",
            subject_source_key="subject:math",
        ),
        row(
            "teaching_assignment",
            "teacher:one",
            offering_source_key="offer:one",
            assignment_source_key="staff:one",
            teaching_role="lead",
            subject_source_keys=("subject:math",),
        ),
    )
    with pytest.raises(ValueError, match="学科"):
        validate_teaching_rules(rows)


def test_teaching_handoff_closes_old_cross_root_relation_before_new_insert():
    from types import SimpleNamespace

    from app.composition.school_fact_persistence import SqlAlchemySchoolFactPersistence

    new = change(
        "teaching_assignment",
        offering_source_key="offer:one",
        assignment_source_key="staff:one",
        teaching_role="lead",
    )
    end = CanonicalFactChangeV1.model_validate(
        dict(
            schema_version=3,
            fact_kind="teaching_assignment",
            source_key="old:one",
            operation="end",
            expected_version=1,
            effective_at=datetime(2026, 9, 1, tzinfo=UTC),
            content=None,
            origin={"sheet_code": "teaching", "row_number": 2},
        )
    )
    pairs = SqlAlchemySchoolFactPersistence._ordered_change_pairs(
        (SimpleNamespace(), SimpleNamespace()), SimpleNamespace(changes=(new, end)), {}
    )
    assert pairs[0][1].operation == "end"


def test_formal_teaching_vacancy_is_bound_to_offering_and_role():
    content = dict(
        quality_kind="negative",
        negative_code="no_teaching_assignment",
        target_fact_kind="teaching_assignment",
        owner_kind="education",
        education_target_kind="teaching_offering",
        education_target_source_key="offer:one",
        teaching_role="lead",
        responsibility="学校正式登记",
        basis_reference="正式确认",
        verified_at=datetime(2026, 9, 1, tzinfo=UTC),
    )
    assert change("fact_quality", **content).content.teaching_role == "lead"
    for invalid in (
        {"teaching_role": None},
        {"education_target_kind": "teaching_group"},
        {"position_source_key": "position:one"},
        {"target_source_key": "teaching:one"},
        {"replacement_source_key": "teaching:two"},
    ):
        with pytest.raises(ValidationError):
            change("fact_quality", **(content | invalid))


def test_teaching_enum_catalog_covers_the_new_vocabulary():
    from app.platform.school_facts.contracts import fact_content_enums

    assert fact_content_enums("teaching_assignment")["teaching_role"] == {"lead", "co_teacher"}


def test_f2_interval_overlay_accepts_new_assignment_without_reading_unwritten_root():
    from types import SimpleNamespace

    from app.composition.school_fact_persistence import SqlAlchemySchoolFactPersistence

    class NoDatabase:
        def execute(self, *args, **kwargs):
            raise AssertionError("new same-batch root is not persisted during preview")

    candidate = SimpleNamespace(
        operation="upsert",
        effective_at=datetime(2026, 1, 1, tzinfo=UTC),
        content=SimpleNamespace(valid_to=None),
    )
    SqlAlchemySchoolFactPersistence._require_f2_reference_interval(
        NoDatabase(),
        None,
        "assignment",
        None,
        datetime(2026, 9, 1, tzinfo=UTC),
        None,
        overlays=(candidate,),
    )
