from __future__ import annotations

from datetime import UTC, datetime
from importlib import import_module
from importlib.util import find_spec


def api():
    name = "app.domains.students.education_change_planner"
    assert find_spec(name) is not None, "F4 explicit impact planner is missing"
    return import_module(name)


def at(day):
    return datetime(2026, 10, day, tzinfo=UTC)


def fact(kind, key, content, **kw):
    return api().EducationFactSnapshot(
        fact_kind=kind,
        source_key=key,
        version=kw.pop("version", 1),
        effective_at=kw.pop("effective_at", at(1)),
        content=content,
        **kw,
    )


def mutation(kind, key, content=None, operation="upsert", version=0, effective_at=None):
    return dict(
        schema_version=3,
        fact_kind=kind,
        source_key=key,
        expected_version=version,
        effective_at=effective_at or at(10),
        operation=operation,
        content=content,
        origin={"sheet_code": "EDUCATION", "row_number": 1},
    )


def test_transfer_expands_current_membership_and_its_roles_in_one_batch():
    a = api()
    snapshots = (
        fact(
            "student_class_membership",
            "member-old",
            {"student_source_key": "student-a", "class_source_key": "class-a", "valid_to": None},
        ),
        fact(
            "student_class_position",
            "role-old",
            {
                "student_source_key": "student-a",
                "class_source_key": "class-a",
                "position_source_key": "monitor",
                "valid_to": None,
            },
        ),
    )
    primary = mutation(
        "student_class_membership",
        "member-new",
        {"student_source_key": "student-a", "class_source_key": "class-b", "valid_to": None},
    )
    command = a.EducationChangeCommand(
        purpose="transfer",
        effective_at=at(10),
        cause_source_key="member-new",
        cause_version=1,
        student_source_key="student-a",
        class_source_key="class-a",
        primary_changes=(primary,),
    )
    result = a.plan_education_change(
        command,
        snapshots,
        authorized_keys=frozenset({"member-old", "member-new", "role-old"}),
        complete_snapshot=True,
    )
    assert result.issues == ()
    assert {c["source_key"] for c in result.changes} == {"member-old", "member-new", "role-old"}
    ended = {c["source_key"]: c for c in result.changes if c["operation"] == "end"}
    assert ended["role-old"]["cause_source_key"] == "member-new"
    assert ended["member-old"]["expected_version"] == 1


def test_missing_authority_reports_whole_impact_without_partial_changes():
    a = api()
    records = (
        fact(
            "student_class_membership",
            "m",
            {"student_source_key": "s", "class_source_key": "c", "valid_to": None},
        ),
        fact(
            "student_class_position",
            "r",
            {
                "student_source_key": "s",
                "class_source_key": "c",
                "position_source_key": "monitor",
                "valid_to": None,
            },
        ),
    )
    cmd = a.EducationChangeCommand(
        purpose="graduation",
        effective_at=at(10),
        cause_source_key="graduation",
        cause_version=1,
        student_source_key="s",
        primary_changes=(
            mutation(
                "student_status", "graduation", {"student_source_key": "s", "status": "graduated"}
            ),
        ),
    )
    result = a.plan_education_change(
        cmd, records, authorized_keys=frozenset({"graduation"}), complete_snapshot=True
    )
    assert not result.changes
    assert {i.source_key for i in result.issues if i.code == "missing_authority"} == {"m", "r"}
    assert set(result.impact_keys) == {"m", "r", "graduation"}


def test_future_cancel_restores_only_own_unchanged_cutoff():
    a = api()
    prior = {
        "student_source_key": "s",
        "class_source_key": "c",
        "position_source_key": "monitor",
        "valid_to": None,
    }
    provenance = a.EndProvenance(
        cause_source_key="future",
        cause_version=1,
        ended_target_version=2,
        prior_effective_at=at(1),
        prior_content=prior,
    )
    old = fact(
        "student_class_position",
        "role",
        {**prior, "valid_to": at(20)},
        version=2,
        provenance=provenance,
    )
    future = fact(
        "student_class_membership",
        "future",
        {"student_source_key": "s", "class_source_key": "other", "valid_to": None},
        effective_at=at(20),
    )
    cmd = a.EducationChangeCommand(
        purpose="cancel_future",
        effective_at=at(10),
        cause_source_key="future",
        cause_version=1,
        primary_changes=(
            mutation(
                "student_class_membership",
                "future",
                operation="cancel",
                version=1,
                effective_at=at(20),
            ),
        ),
        correction_reason="取消尚未生效的调班",
    )
    result = a.plan_education_change(
        cmd, (old, future), authorized_keys=frozenset({"role", "future"}), complete_snapshot=True
    )
    assert not result.issues
    restored = next(c for c in result.changes if c["source_key"] == "role")
    assert restored["operation"] == "correct" and restored["content"]["valid_to"] is None
    assert restored["effective_at"] == at(1)
    assert restored["expected_version"] == 2


def test_future_cancel_never_overwrites_independent_later_edit():
    a = api()
    prior = {"student_source_key": "s", "class_source_key": "c", "valid_to": None}
    provenance = a.EndProvenance(
        cause_source_key="future",
        cause_version=1,
        ended_target_version=2,
        prior_effective_at=at(1),
        prior_content=prior,
    )
    changed = fact(
        "student_class_membership",
        "old",
        {**prior, "valid_to": at(15)},
        version=3,
        provenance=provenance,
    )
    future = fact(
        "student_class_membership",
        "future",
        {**prior, "class_source_key": "other"},
        effective_at=at(20),
    )
    cmd = a.EducationChangeCommand(
        purpose="cancel_future",
        effective_at=at(10),
        cause_source_key="future",
        cause_version=1,
        primary_changes=(
            mutation(
                "student_class_membership",
                "future",
                operation="cancel",
                version=1,
                effective_at=at(20),
            ),
        ),
        correction_reason="取消未来安排",
    )
    result = a.plan_education_change(
        cmd, (changed, future), authorized_keys=frozenset({"future", "old"}), complete_snapshot=True
    )
    assert not result.changes
    assert "independent_change" in {i.code for i in result.issues}


def test_future_dependencies_and_incomplete_snapshot_are_reported_together():
    a = api()
    future = fact(
        "student_class_position",
        "future-role",
        {
            "student_source_key": "s",
            "class_source_key": "c",
            "position_source_key": "monitor",
            "valid_to": None,
        },
        effective_at=at(20),
    )
    cmd = a.EducationChangeCommand(
        purpose="close_class",
        effective_at=at(10),
        cause_source_key="c",
        cause_version=2,
        class_source_key="c",
        primary_changes=(mutation("education_class", "c", operation="end", version=1),),
    )
    result = a.plan_education_change(
        cmd, (future,), authorized_keys=frozenset({"c", "future-role"}), complete_snapshot=False
    )
    assert not result.changes
    assert {"incomplete_snapshot", "future_dependency"} <= {i.code for i in result.issues}


def test_duplicate_same_key_merges_identical_but_rejects_conflicting_changes():
    a = api()
    primary = mutation("student_status", "x", {"student_source_key": "s", "status": "enrolled"})
    cmd = a.EducationChangeCommand(
        purpose="historical_correction",
        effective_at=at(10),
        cause_source_key="x",
        cause_version=1,
        primary_changes=(primary, primary),
        correction_reason="修正记录",
    )
    result = a.plan_education_change(
        cmd, (), authorized_keys=frozenset({"x"}), complete_snapshot=True
    )
    assert len(result.changes) == 1
    other = {**primary, "content": {"student_source_key": "s", "status": "graduated"}}
    from dataclasses import replace

    result = a.plan_education_change(
        replace(cmd, primary_changes=(primary, other)),
        (),
        authorized_keys=frozenset({"x"}),
        complete_snapshot=True,
    )
    assert not result.changes and "conflicting_changes" in {i.code for i in result.issues}


def test_cancel_restoration_rejects_overlap_with_later_published_membership():
    a = api()
    prior = {"student_source_key": "s", "class_source_key": "c", "valid_to": None}
    evidence = a.EndProvenance(
        cause_source_key="future",
        cause_version=1,
        ended_target_version=2,
        prior_effective_at=at(1),
        prior_content=prior,
    )
    old = fact(
        "student_class_membership",
        "old",
        {**prior, "valid_to": at(20)},
        version=2,
        provenance=evidence,
    )
    future = fact(
        "student_class_membership",
        "future",
        {**prior, "class_source_key": "other"},
        effective_at=at(20),
    )
    later = fact(
        "student_class_membership",
        "later",
        {**prior, "class_source_key": "third"},
        effective_at=at(25),
    )
    cmd = a.EducationChangeCommand(
        purpose="cancel_future",
        effective_at=at(10),
        cause_source_key="future",
        cause_version=1,
        primary_changes=(
            mutation(
                "student_class_membership",
                "future",
                operation="cancel",
                version=1,
                effective_at=at(20),
            ),
        ),
        correction_reason="撤回未来安排",
    )
    result = a.plan_education_change(
        cmd,
        (old, future, later),
        authorized_keys=frozenset({"old", "future", "later"}),
        complete_snapshot=True,
    )
    assert not result.changes
    assert {i.source_key for i in result.issues if i.code == "overlapping_tenure"} == {
        "old",
        "later",
    }


def test_negative_interval_is_reported_before_writer():
    a = api()
    bad = mutation(
        "student_class_position",
        "role",
        {
            "student_source_key": "s",
            "class_source_key": "c",
            "position_source_key": "p",
            "valid_to": at(5),
        },
    )
    cmd = a.EducationChangeCommand(
        purpose="historical_correction",
        effective_at=at(10),
        cause_source_key="role",
        cause_version=1,
        primary_changes=(bad,),
        correction_reason="历史补正",
    )
    result = a.plan_education_change(
        cmd, (), authorized_keys=frozenset({"role"}), complete_snapshot=True
    )
    assert not result.changes and "negative_tenure" in {i.code for i in result.issues}


def test_replacing_one_position_does_not_end_another_position():
    a = api()
    one = fact(
        "student_class_position",
        "old-monitor",
        {
            "student_source_key": "s",
            "class_source_key": "c",
            "position_source_key": "monitor",
            "valid_to": None,
        },
    )
    other = fact(
        "student_class_position",
        "sports",
        {
            "student_source_key": "s",
            "class_source_key": "c",
            "position_source_key": "sports",
            "valid_to": None,
        },
    )
    new = mutation(
        "student_class_position",
        "new-monitor",
        {
            "student_source_key": "another",
            "class_source_key": "c",
            "position_source_key": "monitor",
            "valid_to": None,
        },
    )
    cmd = a.EducationChangeCommand(
        purpose="replace_assignment",
        effective_at=at(10),
        cause_source_key="new-monitor",
        cause_version=1,
        primary_changes=(new,),
        replaced_source_keys=frozenset({"old-monitor"}),
    )
    result = a.plan_education_change(
        cmd,
        (one, other),
        authorized_keys=frozenset({"old-monitor", "new-monitor"}),
        complete_snapshot=True,
    )
    assert not result.issues
    assert set(result.impact_keys) == {"old-monitor", "new-monitor"}


def test_close_class_includes_teachers_locations_positions_and_placement():
    a = api()
    records = tuple(
        fact(kind, kind, {"class_source_key": "class-a", "valid_to": None})
        for kind in (
            "class_teacher_assignment",
            "class_location",
            "class_placement",
            "class_position_definition",
        )
    )
    root = fact("education_class", "class-a", {"display_name": "2023班", "valid_to": None})
    cmd = a.EducationChangeCommand(
        purpose="close_class",
        effective_at=at(10),
        cause_source_key="class-a",
        cause_version=2,
        class_source_key="class-a",
        primary_changes=(mutation("education_class", "class-a", operation="end", version=1),),
    )
    keys = frozenset(r.source_key for r in records + (root,))
    result = a.plan_education_change(
        cmd, records + (root,), authorized_keys=keys, complete_snapshot=True
    )
    assert not result.issues and set(result.impact_keys) == keys


def test_produced_cancellation_and_restoration_pass_strict_canonical_v3():
    a = api()
    from app.platform.school_facts.contracts import CanonicalFactChangeV3

    prior = {
        "student_source_key": "student:s",
        "school_relationship_source_key": "school:rel",
        "membership_status": "assigned",
        "class_source_key": "class:one",
        "valid_to": None,
    }
    evidence = a.EndProvenance(
        cause_source_key="member:future",
        cause_version=1,
        ended_target_version=2,
        prior_effective_at=at(1),
        prior_content=prior,
    )
    old = fact(
        "student_class_membership",
        "member:old",
        {**prior, "valid_to": at(20)},
        version=2,
        provenance=evidence,
    )
    future = fact(
        "student_class_membership",
        "member:future",
        {**prior, "class_source_key": "class:other"},
        effective_at=at(20),
    )
    cmd = a.EducationChangeCommand(
        purpose="cancel_future",
        effective_at=at(10),
        cause_source_key="member:future",
        cause_version=1,
        primary_changes=(
            mutation(
                "student_class_membership",
                "member:future",
                operation="cancel",
                version=1,
                effective_at=at(20),
            ),
        ),
        correction_reason="cancel unpublished future change",
    )
    result = a.plan_education_change(
        cmd,
        (old, future),
        authorized_keys=frozenset({"member:old", "member:future"}),
        complete_snapshot=True,
    )
    assert not result.issues
    assert all(CanonicalFactChangeV3.model_validate(c) for c in result.changes)


def test_cancel_requires_explicit_cancel_of_same_cause():
    a = api()
    future = fact(
        "education_class",
        "future",
        {"display_name": "class", "valid_to": None},
        effective_at=at(20),
    )
    cmd = a.EducationChangeCommand(
        purpose="cancel_future",
        effective_at=at(10),
        cause_source_key="future",
        cause_version=1,
        primary_changes=(
            mutation(
                "education_class",
                "future",
                {"display_name": "renamed"},
                version=1,
                effective_at=at(20),
            ),
        ),
        correction_reason="do not silently cancel",
    )
    result = a.plan_education_change(
        cmd, (future,), authorized_keys=frozenset({"future"}), complete_snapshot=True
    )
    assert not result.changes and "missing_cancellation" in {i.code for i in result.issues}


def test_historical_transfer_expands_at_actual_date_and_reports_impossible_later_role():
    a = api()
    member = fact(
        "student_class_membership",
        "old",
        {"student_source_key": "s", "class_source_key": "c", "valid_to": None},
    )
    role = fact(
        "student_class_position",
        "late-role",
        {
            "student_source_key": "s",
            "class_source_key": "c",
            "position_source_key": "monitor",
            "valid_to": None,
        },
        effective_at=at(15),
    )
    primary = mutation(
        "student_class_membership",
        "new",
        {"student_source_key": "s", "class_source_key": "other", "valid_to": None},
    )
    cmd = a.EducationChangeCommand(
        purpose="historical_correction",
        corrected_purpose="transfer",
        effective_at=at(10),
        cause_source_key="new",
        cause_version=1,
        student_source_key="s",
        class_source_key="c",
        primary_changes=(primary,),
        correction_reason="actual transfer occurred earlier",
    )
    result = a.plan_education_change(
        cmd,
        (member, role),
        authorized_keys=frozenset({"old", "new", "late-role"}),
        complete_snapshot=True,
    )
    assert not result.changes and "future_dependency" in {i.code for i in result.issues}
    assert set(result.impact_keys) == {"old", "new", "late-role"}
    from dataclasses import replace

    cancelled = mutation(
        "student_class_position", "late-role", operation="cancel", version=1, effective_at=at(15)
    )
    result = a.plan_education_change(
        replace(cmd, primary_changes=(primary, cancelled)),
        (member, role),
        authorized_keys=frozenset({"old", "new", "late-role"}),
        complete_snapshot=True,
    )
    assert not result.issues
    assert next(c for c in result.changes if c["source_key"] == "old")["effective_at"] == at(10)


def test_cancel_requires_explicit_disposition_of_new_class_future_position():
    a = api()
    future = fact(
        "student_class_membership",
        "future",
        {"student_source_key": "s", "class_source_key": "new-class", "valid_to": None},
        effective_at=at(20),
    )
    role = fact(
        "student_class_position",
        "future-position",
        {
            "student_source_key": "s",
            "class_source_key": "new-class",
            "position_source_key": "monitor",
            "valid_to": None,
        },
        effective_at=at(21),
    )
    cmd = a.EducationChangeCommand(
        purpose="cancel_future",
        effective_at=at(10),
        cause_source_key="future",
        cause_version=1,
        primary_changes=(
            mutation(
                "student_class_membership",
                "future",
                operation="cancel",
                version=1,
                effective_at=at(20),
            ),
        ),
        correction_reason="cancel future transfer",
    )
    result = a.plan_education_change(
        cmd,
        (future, role),
        authorized_keys=frozenset({"future", "future-position"}),
        complete_snapshot=True,
    )
    assert not result.changes and "dependent_arrangement" in {i.code for i in result.issues}
    assert "future-position" in result.impact_keys


def test_position_restoration_cannot_overlap_independently_published_successor():
    a = api()
    prior = {
        "student_source_key": "s",
        "class_source_key": "c",
        "position_source_key": "monitor",
        "valid_to": None,
    }
    evidence = a.EndProvenance(
        cause_source_key="future",
        cause_version=1,
        ended_target_version=2,
        prior_effective_at=at(1),
        prior_content=prior,
    )
    old = fact(
        "student_class_position",
        "old",
        {**prior, "valid_to": at(20)},
        version=2,
        provenance=evidence,
    )
    future = fact(
        "student_class_membership",
        "future",
        {"student_source_key": "s", "class_source_key": "other", "valid_to": None},
        effective_at=at(20),
    )
    successor = fact(
        "student_class_position",
        "successor",
        {**prior, "student_source_key": "another"},
        effective_at=at(21),
    )
    definition = fact(
        "class_position_definition",
        "monitor",
        {
            "class_source_key": "c",
            "display_name": "monitor",
            "allow_multiple": False,
            "allow_concurrent": True,
            "valid_to": None,
        },
    )
    cmd = a.EducationChangeCommand(
        purpose="cancel_future",
        effective_at=at(10),
        cause_source_key="future",
        cause_version=1,
        primary_changes=(
            mutation(
                "student_class_membership",
                "future",
                operation="cancel",
                version=1,
                effective_at=at(20),
            ),
        ),
        correction_reason="cancel future transfer",
    )
    result = a.plan_education_change(
        cmd,
        (old, future, successor, definition),
        authorized_keys=frozenset({"old", "future", "successor"}),
        complete_snapshot=True,
    )
    assert not result.changes and "overlapping_tenure" in {i.code for i in result.issues}


def test_explicit_existing_correction_and_implicit_ending_merge_once():
    a = api()
    old_content = {
        "student_source_key": "s",
        "class_source_key": "c",
        "position_source_key": "monitor",
        "valid_to": None,
    }
    role = fact("student_class_position", "role", old_content)
    correction = mutation(
        "student_class_position",
        "role",
        old_content,
        operation="correct",
        version=1,
        effective_at=at(1),
    )
    primary = mutation(
        "student_class_membership",
        "new",
        {"student_source_key": "s", "class_source_key": "other", "valid_to": None},
    )
    cmd = a.EducationChangeCommand(
        purpose="historical_correction",
        corrected_purpose="transfer",
        effective_at=at(10),
        cause_source_key="new",
        cause_version=1,
        student_source_key="s",
        class_source_key="c",
        primary_changes=(primary, correction),
        correction_reason="correct old position and transfer",
    )
    result = a.plan_education_change(
        cmd, (role,), authorized_keys=frozenset({"role", "new"}), complete_snapshot=True
    )
    assert not result.issues
    merged = next(c for c in result.changes if c["source_key"] == "role")
    assert merged["operation"] == "correct" and merged["content"]["valid_to"] == at(10)
    assert len([c for c in result.changes if c["source_key"] == "role"]) == 1


def test_independently_decided_end_cannot_be_overwritten_by_cascade():
    a = api()
    prior = {
        "student_source_key": "s",
        "class_source_key": "c",
        "position_source_key": "monitor",
        "valid_to": None,
    }
    role = fact("student_class_position", "role", prior)
    explicit = mutation(
        "student_class_position", "role", operation="end", version=1, effective_at=at(15)
    )
    primary = mutation(
        "student_class_membership",
        "new",
        {"student_source_key": "s", "class_source_key": "other", "valid_to": None},
    )
    cmd = a.EducationChangeCommand(
        purpose="transfer",
        effective_at=at(10),
        cause_source_key="new",
        cause_version=1,
        student_source_key="s",
        class_source_key="c",
        primary_changes=(primary, explicit),
    )
    result = a.plan_education_change(
        cmd, (role,), authorized_keys=frozenset({"role", "new"}), complete_snapshot=True
    )
    assert not result.changes and "conflicting_changes" in {i.code for i in result.issues}


def test_explicit_end_cannot_create_negative_tenure():
    a = api()
    record = fact(
        "education_class",
        "class-one",
        {"display_name": "class", "valid_to": None},
        effective_at=at(20),
    )
    cmd = a.EducationChangeCommand(
        purpose="close_class",
        effective_at=at(10),
        cause_source_key="class-one",
        cause_version=2,
        class_source_key="class-one",
        primary_changes=(mutation("education_class", "class-one", operation="end", version=1),),
    )
    result = a.plan_education_change(
        cmd, (record,), authorized_keys=frozenset({"class-one"}), complete_snapshot=True
    )
    assert not result.changes and "negative_tenure" in {i.code for i in result.issues}


def test_cascade_cause_must_bind_the_primary_result_version():
    a = api()
    primary = mutation("education_class", "class-new", {"display_name": "class"})
    cmd = a.EducationChangeCommand(
        purpose="historical_correction",
        effective_at=at(10),
        cause_source_key="class-new",
        cause_version=99,
        primary_changes=(primary,),
        correction_reason="historical publication",
    )
    result = a.plan_education_change(
        cmd, (), authorized_keys=frozenset({"class-new"}), complete_snapshot=True
    )
    assert not result.changes and "invalid_cause" in {i.code for i in result.issues}


def test_same_root_historical_transfer_does_not_end_its_replacement_content():
    a = api()
    snapshots = (
        fact(
            "student_class_membership",
            "member-old",
            {"student_source_key": "student-a", "class_source_key": "class-a", "valid_to": None},
        ),
        fact(
            "student_class_position",
            "role-old",
            {
                "student_source_key": "student-a",
                "class_source_key": "class-a",
                "position_source_key": "monitor",
                "valid_to": None,
            },
        ),
    )
    primary = mutation(
        "student_class_membership",
        "member-old",
        {"student_source_key": "student-a", "class_source_key": "class-b", "valid_to": None},
        operation="correct",
        version=1,
    )
    command = a.EducationChangeCommand(
        purpose="historical_correction",
        corrected_purpose="transfer",
        effective_at=at(10),
        cause_source_key="member-old",
        cause_version=2,
        student_source_key="student-a",
        class_source_key="class-a",
        primary_changes=(primary,),
        correction_reason="Approved correction",
    )
    result = a.plan_education_change(
        command,
        snapshots,
        authorized_keys=frozenset({"member-old", "role-old"}),
        complete_snapshot=True,
    )
    assert not result.issues
    changed = {c["source_key"]: c for c in result.changes}
    assert changed["member-old"]["content"]["valid_to"] is None
    assert changed["role-old"]["operation"] == "end"
