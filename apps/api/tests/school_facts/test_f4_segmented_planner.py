from dataclasses import replace

from tests.school_facts.test_f4_education_change_planner import api, at, fact, mutation


def segment(start, end, content, **kw):
    return api().EducationTimelineSegment(
        effective_at=at(start), valid_to=None if end is None else at(end), content=content, **kw
    )


def test_end_current_assignment_keeps_independent_later_assignment():
    a = api()
    content = {
        "class_source_key": "class-a",
        "assignment_source_key": "teacher-old",
        "teacher_role": "primary",
    }
    old = fact("class_teacher_assignment", "teacher-link", content)
    old = replace(
        old,
        segments=(
            segment(1, 15, content),
            segment(25, None, {**content, "assignment_source_key": "teacher-later"}),
        ),
    )
    primary = mutation(
        "class_teacher_assignment",
        "teacher-new",
        {**content, "assignment_source_key": "teacher-new", "valid_to": at(25)},
    )
    command = a.EducationChangeCommand(
        purpose="replace_assignment",
        effective_at=at(10),
        cause_source_key="teacher-new",
        cause_version=1,
        primary_changes=(primary,),
        replaced_source_keys=frozenset({"teacher-link"}),
        correction_reason="Approved appointment",
    )
    plan = a.plan_education_change(
        command,
        (old,),
        authorized_keys=frozenset({"teacher-link", "teacher-new"}),
        complete_snapshot=True,
    )
    assert not plan.issues
    changed = {c["source_key"]: c for c in plan.changes}["teacher-link"]
    assert changed["operation"] == "correct" and changed["content"] is None
    assert [(s["effective_at"], s["valid_to"]) for s in changed["replacement_segments"]] == [
        (at(1), at(10)),
        (at(25), None),
    ]
    assert changed["replacement_segments"][1]["content"]["assignment_source_key"] == "teacher-later"
    assert changed["replacement_segments"][1]["cause_source_key"] is None


def test_historical_transfer_keeps_later_membership_and_role_and_restores_after_bounded_window():
    a = api()
    content = {"student_source_key": "student-a", "class_source_key": "class-a"}
    member = replace(
        fact("student_class_membership", "member", content),
        segments=(
            segment(1, 20, content),
            segment(25, None, {**content, "class_source_key": "class-c"}),
        ),
    )
    role_content = {**content, "position_source_key": "monitor"}
    role = replace(
        fact("student_class_position", "role", role_content),
        segments=(
            segment(1, 20, role_content),
            segment(25, None, {**role_content, "class_source_key": "class-c"}),
        ),
    )
    primary = mutation(
        "student_class_membership",
        "member",
        {**content, "class_source_key": "class-b", "valid_to": at(15)},
        operation="correct",
        version=1,
    )
    command = a.EducationChangeCommand(
        purpose="historical_correction",
        corrected_purpose="transfer",
        effective_at=at(10),
        cause_source_key="member",
        cause_version=2,
        primary_changes=(primary,),
        student_source_key="student-a",
        class_source_key="class-a",
        correction_reason="Approved bounded transfer",
    )
    plan = a.plan_education_change(
        command,
        (member, role),
        authorized_keys=frozenset({"member", "role"}),
        complete_snapshot=True,
    )
    assert not plan.issues
    changed = {c["source_key"]: c for c in plan.changes}["role"]
    assert [(s["effective_at"], s["valid_to"]) for s in changed["replacement_segments"]] == [
        (at(1), at(10)),
        (at(15), at(20)),
        (at(25), None),
    ]


def test_segmented_version_conflict_returns_no_partial_plan():
    a = api()
    old = replace(
        fact("education_class", "class", {"display_name": "Class"}),
        segments=(segment(1, None, {"display_name": "Class"}),),
    )
    primary = mutation("education_class", "class", operation="end", version=2)
    command = a.EducationChangeCommand(
        purpose="close_class",
        effective_at=at(10),
        cause_source_key="class",
        cause_version=3,
        primary_changes=(primary,),
        class_source_key="class",
    )
    plan = a.plan_education_change(
        command, (old,), authorized_keys=frozenset({"class"}), complete_snapshot=True
    )
    assert not plan.changes
    assert any(i.code == "stale_version" for i in plan.issues)


def test_cancel_does_not_restore_a_cutoff_caused_by_an_independent_arrangement():
    a = api()
    content = {"student_source_key": "student-a", "class_source_key": "class-a"}
    future = replace(
        fact("student_class_membership", "future", content, effective_at=at(20)),
        segments=(segment(20, None, {**content, "class_source_key": "class-b"}),),
    )
    original = replace(
        fact("student_class_membership", "original", content),
        segments=(segment(1, 15, content, cause_source_key="other-arrangement", cause_version=4),),
    )
    primary = mutation("student_class_membership", "future", operation="cancel", version=1)
    primary["effective_at"] = at(20)
    command = a.EducationChangeCommand(
        purpose="cancel_future",
        effective_at=at(10),
        cause_source_key="future",
        cause_version=1,
        primary_changes=(primary,),
        student_source_key="student-a",
        correction_reason="Cancel only own arrangement",
    )
    plan = a.plan_education_change(
        command,
        (future, original),
        authorized_keys=frozenset({"future", "original"}),
        complete_snapshot=True,
    )
    assert not plan.issues
    assert [c["source_key"] for c in plan.changes] == ["future"]


def test_same_root_future_replacement_cancel_restores_exact_prior_revision():
    a = api()
    payload = {"display_name": "A"}
    old = replace(
        fact("education_class", "class", payload, version=2),
        segments=(segment(1, 20, payload), segment(20, None, {"display_name": "B"})),
        prior_segments=(segment(1, None, payload),),
        latest_operation="upsert",
    )
    primary = mutation("education_class", "class", operation="cancel", version=2)
    primary["effective_at"] = at(20)
    command = a.EducationChangeCommand(
        purpose="cancel_future",
        effective_at=at(10),
        cause_source_key="class",
        cause_version=2,
        primary_changes=(primary,),
        correction_reason="Cancel future replacement",
    )
    plan = a.plan_education_change(
        command, (old,), authorized_keys=frozenset({"class"}), complete_snapshot=True
    )
    assert not plan.issues
    assert plan.changes[0]["operation"] == "cancel"
