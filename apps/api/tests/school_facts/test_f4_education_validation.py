from datetime import UTC, datetime

import pytest

from app.domains.students.education_validation import EducationRecord, validate_education_rules

SEP = datetime(2026, 9, 1, tzinfo=UTC)


def row(kind, key, payload, at=SEP):
    return EducationRecord(kind, key, at, None, payload)


def test_explicit_two_deputies_are_legal_but_third_is_not():
    base = [
        row(
            "education_class",
            "class:one",
            {
                "display_name": "班",
                "teacher_role_limits": {"deputy": 2},
                "teacher_mode": "single_primary",
            },
        )
    ]
    teachers = [
        row(
            "class_teacher_assignment",
            f"teacher:{i}",
            {"class_source_key": "class:one", "teacher_role": "deputy"},
        )
        for i in range(3)
    ]
    validate_education_rules(tuple(base + teachers[:2]))
    with pytest.raises(ValueError):
        validate_education_rules(tuple(base + teachers))


def test_unregistered_teacher_cardinality_is_not_assumed_one():
    with pytest.raises(ValueError):
        validate_education_rules(
            (
                row("education_class", "class:one", {}),
                row(
                    "class_teacher_assignment",
                    "teacher:one",
                    {"class_source_key": "class:one", "teacher_role": "primary"},
                ),
            )
        )


def test_multiple_capacity_does_not_allow_duplicate_same_student_same_post():
    base = (
        row("education_class", "class:one", {}),
        row(
            "class_position_definition",
            "post:one",
            {"class_source_key": "class:one", "max_holders": 2, "allow_concurrent": True},
        ),
    )
    holders = tuple(
        row(
            "student_class_position",
            "holder:" + str(i),
            {
                "class_source_key": "class:one",
                "position_source_key": "post:one",
                "student_source_key": "student:one",
            },
        )
        for i in range(2)
    )
    with pytest.raises(ValueError, match="重复"):
        validate_education_rules(base + holders)


def test_different_start_years_can_collide_at_same_derived_grade_and_number():
    rule = row(
        "education_calendar_rule",
        "calendar:one",
        {
            "start_cycle": 2025,
            "month": 9,
            "day": 1,
            "timezone_name": "UTC",
            "grade_source_keys": ["grade:one", "grade:two", "grade:three"],
        },
        datetime(2025, 9, 1, tzinfo=UTC),
    )
    earlier = row(
        "class_placement",
        "placement:one",
        {
            "class_source_key": "class:one",
            "calendar_source_key": "calendar:one",
            "campus_source_key": "campus:one",
            "grade_source_key": "grade:one",
            "class_number": 3,
        },
        datetime(2025, 9, 1, tzinfo=UTC),
    )
    later = row(
        "class_placement",
        "placement:two",
        {
            "class_source_key": "class:two",
            "calendar_source_key": "calendar:one",
            "campus_source_key": "campus:one",
            "grade_source_key": "grade:two",
            "class_number": 3,
        },
    )
    with pytest.raises(ValueError, match="班序"):
        validate_education_rules((rule, earlier, later))
