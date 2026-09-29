from importlib import import_module

import pytest

AT = "2027-09-01T00:00:00+08:00"


def member(key, student, old, new, status="assigned"):
    return dict(
        fact_kind="student_class_membership",
        reference=key,
        operation="upsert",
        effective_at=AT,
        before=dict(student_ref=student, class_ref=old, membership_status="assigned"),
        after=dict(student_ref=student, class_ref=new, membership_status=status),
    )


def build(items):
    return import_module("app.composition.school_fact_education_regroup").summarize_regroup(items)


def test_two_old_two_new_keeps_explicit_unassigned_and_only_counts_batch():
    value = build(
        [
            member("m1", "s1", "old1", "new1"),
            member("m2", "s2", "old1", "new2"),
            member("m3", "s3", "old2", None, "unassigned"),
        ]
    )
    assert value["scope"] == "batch_only"
    point = value["periods"][0]
    assert point["student_count"] == 3
    assert point["unassigned"] == ["s3"]
    assert {r["class_ref"]: r["incoming_count"] for r in point["classes"]} == {
        "old1": 0,
        "old2": 0,
        "new1": 1,
        "new2": 1,
    }
    assert point["departures"] == [
        {
            "student_ref": "s1",
            "from_class_ref": "old1",
            "to_class_ref": "new1",
            "status": "assigned",
        },
        {
            "student_ref": "s2",
            "from_class_ref": "old1",
            "to_class_ref": "new2",
            "status": "assigned",
        },
        {
            "student_ref": "s3",
            "from_class_ref": "old2",
            "to_class_ref": None,
            "status": "unassigned",
        },
    ]
    assert all(c["class_state"] == "unchanged" for c in point["classes"])


def test_duplicate_student_and_missing_destination_are_rejected_not_auto_assigned():
    with pytest.raises(ValueError, match="重复"):
        build([member("m1", "s1", "old1", "new1"), member("m2", "s1", "old1", "new2")])
    with pytest.raises(ValueError, match="去向"):
        build([member("m1", "s1", "old1", None)])


def test_end_then_new_membership_is_one_student_and_class_end_is_explicit():
    old = member("m1", "s1", "old1", None)
    old.update(operation="end", after=None)
    new = member("m2", "s1", None, "new1")
    new["before"] = None
    end = dict(
        fact_kind="education_class",
        reference="old1",
        operation="end",
        effective_at=AT,
        before={"display_name": "原甲班"},
        after=None,
    )
    value = build([old, new, end])["periods"][0]
    assert value["student_count"] == 1
    assert value["departures"][0]["from_class_ref"] == "old1"
    assert next(c for c in value["classes"] if c["class_ref"] == "old1")["class_state"] == "ended"


def test_separate_effective_dates_are_not_merged_and_non_member_batch_has_no_summary():
    later = member("m2", "s1", "new1", "new2")
    later["effective_at"] = "2028-09-01T00:00:00+08:00"
    assert len(build([member("m1", "s1", "old1", "new1"), later])["periods"]) == 2
    assert build([]) is None


def test_history_replacement_uses_full_timeline_preview_not_flat_roster_summary():
    correction = member("m1", "s1", "old1", "new1")
    correction["operation"] = "correct"
    assert build([correction]) is None
