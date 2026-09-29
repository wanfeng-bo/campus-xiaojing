from types import SimpleNamespace

from app.composition.school_fact_family_correction_writer import SqlGuardianCorrectionAuthority
from tests.test_f3_family_read import setup


def test_actual_education_option_rows_are_labeled_by_finite_fact_ownership():
    _, actor, _ = setup()
    rows = [
        {
            "fact_kind": kind,
            "field_name": field,
            "label": kind,
            "fact_source_key": "private:" + kind,
            "target_version": 1,
        }
        for kind, field in (
            ("class_teacher_assignment", "assignment_source_key"),
            ("class_placement", "grade_source_key"),
            ("student_grade_placement", "grade_source_key"),
            ("student_class_membership", "class_source_key"),
            ("class_location", "location_source_key"),
            ("student_cohort_membership", "cohort_source_key"),
            ("student_class_position", "position_source_key"),
        )
    ]
    session = SimpleNamespace(
        execute=lambda *a, **k: SimpleNamespace(mappings=lambda: SimpleNamespace(all=lambda: rows))
    )
    codec = SimpleNamespace(encode=lambda **kw: "signed:" + kw["source_key"])
    options = SqlGuardianCorrectionAuthority(session=session, codec=codec).options(actor)
    assert [o.label for o in options] == [
        "班主任任职",
        "班级年级安排",
        "孩子个人年级安排",
        "孩子所在班级",
        "班级教室",
        "孩子届别归属",
        "孩子班内职务",
    ]
    assert [o.field_name for o in options] == [row["field_name"] for row in rows]
    assert all(o.reference.startswith("signed:") for o in options)
