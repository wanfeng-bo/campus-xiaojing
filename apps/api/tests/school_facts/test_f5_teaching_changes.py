from datetime import UTC, datetime
from importlib import import_module

import pytest

from app.domains.students.education_change_planner import EducationFactSnapshot

JAN = datetime(2026, 1, 1, tzinfo=UTC)
SEP = datetime(2026, 9, 1, tzinfo=UTC)
OCT = datetime(2026, 10, 1, tzinfo=UTC)


def snap(kind, key, **content):
    return EducationFactSnapshot(
        fact_kind=kind, source_key=key, version=1, effective_at=JAN, content=content
    )


def change(kind, key, operation="end", content=None, expected=1, at=SEP):
    return dict(
        schema_version=3,
        fact_kind=kind,
        source_key=key,
        operation=operation,
        expected_version=expected,
        effective_at=at,
        content=content,
        origin={"sheet_code": "teaching", "row_number": 1},
        correction_reason="正式调整" if operation in {"correct", "cancel"} else None,
    )


def plan(changes, rows, **kwargs):
    module = import_module("app.domains.students.teaching_change_planner")
    return module.plan_teaching_change(
        tuple(changes),
        tuple(rows),
        now=SEP,
        authorized_keys=frozenset(r.source_key for r in rows)
        | frozenset(c["source_key"] for c in changes),
        complete_snapshot=True,
        **kwargs,
    )


def test_shared_group_end_expands_all_sources_offerings_assignments_and_members():
    rows = [
        snap("teaching_group", "group:one", organization_source_key="org:one", group_mode="shared"),
        snap(
            "teaching_group_member",
            "member:one",
            group_source_key="group:one",
            student_source_key="student:one",
            school_relationship_source_key="school:one",
        ),
    ]
    for suffix in ("one", "two"):
        rows += [
            snap(
                "offering_member_source",
                "source:" + suffix,
                group_source_key="group:one",
                offering_source_key="offer:" + suffix,
                source_kind="independent_group",
            ),
            snap(
                "teaching_offering",
                "offer:" + suffix,
                course_source_key="course:one",
                offering_status="active",
            ),
            snap(
                "teaching_assignment",
                "teacher:" + suffix,
                offering_source_key="offer:" + suffix,
                assignment_source_key="staff:one",
                teaching_role="lead",
            ),
        ]
    result = plan([change("teaching_group", "group:one")], rows)
    assert not result.issues
    assert {c["source_key"] for c in result.changes} == {r.source_key for r in rows}


def test_leaving_ends_B_membership_but_suspension_preserves_it():
    member = snap(
        "teaching_group_member",
        "member:one",
        group_source_key="group:one",
        student_source_key="student:one",
        school_relationship_source_key="school:one",
    )
    relation = snap(
        "student_school_relationship",
        "school:one",
        student_source_key="student:one",
        enrollment_status="enrolled",
    )
    for status, count in (("transferred_out", 2), ("suspended", 1)):
        result = plan(
            [
                change(
                    "student_school_relationship",
                    "school:one",
                    "upsert",
                    {"student_source_key": "student:one", "enrollment_status": status},
                )
            ],
            [member, relation],
        )
        assert not result.issues
        assert len(result.changes) == count


def test_pause_does_not_end_sources_or_teachers_and_resume_does_not_revive_ended():
    offering = snap(
        "teaching_offering", "offer:one", course_source_key="course:one", offering_status="active"
    )
    result = plan(
        [
            change(
                "teaching_offering",
                "offer:one",
                "upsert",
                {"course_source_key": "course:one", "offering_status": "suspended"},
            )
        ],
        [offering],
    )
    assert not result.issues and len(result.changes) == 1
    ended = snap(
        "teaching_offering",
        "offer:one",
        course_source_key="course:one",
        offering_status="suspended",
        valid_to=SEP,
    )
    invalid = plan(
        [
            change(
                "teaching_offering",
                "offer:one",
                "upsert",
                {"course_source_key": "course:one", "offering_status": "active"},
                at=OCT,
            )
        ],
        [ended],
    )
    assert invalid.issues and not invalid.changes


def test_source_switch_notice_is_explicit_without_copying_A_students():
    module = import_module("app.domains.students.teaching_change_planner")
    source = snap(
        "offering_member_source",
        "source:one",
        offering_source_key="offer:one",
        source_kind="administrative_class",
        class_source_key="class:one",
    )
    changes = [
        change(
            "offering_member_source",
            "source:one",
            "upsert",
            {
                "offering_source_key": "offer:one",
                "source_kind": "independent_group",
                "group_source_key": "group:one",
            },
        )
    ]
    notes = module.teaching_change_notices(tuple(changes), (source,))
    assert any("独立维护" in note for note in notes)
    assert len(plan(changes, [source]).changes) == 1


def test_batch_limit_rejects_whole_expansion():
    rows = [
        snap("teaching_group", "group:one", organization_source_key="org:one", group_mode="shared"),
        snap(
            "teaching_group_member",
            "member:one",
            group_source_key="group:one",
            student_source_key="student:one",
        ),
    ]
    result = plan([change("teaching_group", "group:one")], rows, max_changes=1)
    assert not result.changes and any(i.code == "teaching_capacity" for i in result.issues)


def test_ending_only_source_requires_offering_and_assignment_ends():
    rows = [
        snap(
            "offering_member_source",
            "source:one",
            offering_source_key="offer:one",
            source_kind="administrative_class",
            class_source_key="class:one",
        ),
        snap(
            "teaching_offering",
            "offer:one",
            course_source_key="course:one",
            offering_status="active",
        ),
        snap(
            "teaching_assignment",
            "teacher:one",
            offering_source_key="offer:one",
            assignment_source_key="staff:one",
            teaching_role="lead",
        ),
    ]
    result = plan([change("offering_member_source", "source:one")], rows)
    assert not result.issues
    assert {c["source_key"] for c in result.changes} == {r.source_key for r in rows}


def test_maintenance_bridge_switches_source_and_presents_independent_list_notice():
    from uuid import uuid4

    from app.composition.school_fact_education_product import EducationBatchInput
    from tests.school_facts.test_f4_education_maintenance import environment, record

    rows = [
        record(
            "offering_member_source",
            "source:one",
            offering_source_key="offer:one",
            source_kind="administrative_class",
            class_source_key="class:one",
        ),
        record(
            "teaching_group", "group:one", organization_source_key="org:one", group_mode="exclusive"
        ),
        record(
            "teaching_offering",
            "offer:one",
            course_source_key="course:one",
            offering_status="active",
        ),
    ]
    coordinator, actor = environment(rows)
    payload = EducationBatchInput(
        fact_kind="offering_member_source",
        target_ref="f4:offering_member_source:source:one",
        fields={"source_kind": "independent_group", "group_ref": "f4:teaching_group:group:one"},
        reason="正式独立教学名单",
    )
    result = coordinator.resolve_change(
        actor=actor, payload=payload, request_id=uuid4(), effective_at=SEP
    )
    assert result.change_set.changes[0].content.class_source_key is None
    assert "独立维护" in result.summary.source_label


def test_maintenance_teaching_array_fields_are_public_references():
    from app.composition.school_fact_education_product import (
        education_field_descriptor,
        public_name,
    )

    assert public_name("subject_source_keys") == "subject_refs"
    assert education_field_descriptor("subject_refs", "teaching_assignment")["type"] == "references"


def test_family_entry_bridge_keeps_the_exact_coordinated_preview_and_guard():
    from types import SimpleNamespace
    from unittest.mock import Mock
    from uuid import uuid4

    from app.composition.school_fact_directory_product import FactChangeInput
    from app.composition.school_fact_education_maintenance import resolve_family_with_education

    coordinated = SimpleNamespace(summary=object(), final_guard=object())
    maintenance = SimpleNamespace(resolve_change=Mock(return_value=coordinated))
    fallback = Mock()
    payload = FactChangeInput(
        fact_kind="student_school_relationship",
        target_ref="f3:signed",
        fields={"enrollment_status": "transferred_out"},
        reason="正式离校",
    )
    result = resolve_family_with_education(
        maintenance=maintenance,
        fallback=fallback,
        actor=object(),
        payload=payload,
        request_id=uuid4(),
        effective_at=SEP,
    )
    assert result is coordinated and not fallback.called
    assert maintenance.resolve_change.call_args.kwargs["payload"].fields == payload.fields


def test_future_reschedule_moves_only_selected_segment_and_preserves_independent_end():
    from app.domains.students.education_change_planner import EducationTimelineSegment
    from app.domains.students.teaching_change_planner import reschedule_teaching_future

    december = datetime(2026, 12, 1, tzinfo=UTC)
    row = snap(
        "teaching_offering", "offer:one", course_source_key="course:one", offering_status="active"
    )
    from dataclasses import replace

    row = replace(
        row,
        segments=(
            EducationTimelineSegment(effective_at=JAN, valid_to=SEP, content=row.content),
            EducationTimelineSegment(effective_at=OCT, valid_to=december, content=row.content),
        ),
    )
    proposal = change(
        "teaching_offering",
        "offer:one",
        "correct",
        row.content,
        at=datetime(2026, 11, 1, tzinfo=UTC),
    )
    result = reschedule_teaching_future(proposal, row, from_at=OCT, now=SEP)
    assert result["replacement_segments"][0]["valid_to"] == SEP
    assert result["replacement_segments"][1]["effective_at"].month == 11
    assert result["replacement_segments"][1]["valid_to"] == december


def test_explicit_teaching_handoff_through_coordinator_ends_selected_original():
    from uuid import uuid4

    from app.composition.school_fact_education_product import EducationBatchInput
    from tests.school_facts.test_f4_education_maintenance import environment, record

    coordinator, actor = environment(
        [
            record(
                "teaching_assignment",
                "teacher:old",
                offering_source_key="offer:one",
                assignment_source_key="staff:old",
                teaching_role="lead",
            )
        ]
    )
    payload = EducationBatchInput(
        fact_kind="teaching_assignment",
        creation_ref="f4create:teaching_assignment:teacher:new",
        reason="正式任教交接",
        purpose="replace_assignment",
        replace_refs=("f4:teaching_assignment:teacher:old",),
        fields={
            "offering_ref": "f4:teaching_offering:offer:one",
            "assignment_ref": "f4:assignment:staff:new",
            "teaching_role": "lead",
        },
    )
    result = coordinator.resolve_change(
        actor=actor, payload=payload, request_id=uuid4(), effective_at=SEP
    )
    assert {(c.source_key, c.operation) for c in result.change_set.changes} == {
        ("teacher:new", "upsert"),
        ("teacher:old", "end"),
    }
    assert len(result.summary.impacts) == 2
    old_change = next(c for c in result.change_set.changes if c.source_key == "teacher:old")
    assert old_change.cause_source_key == "teacher:new" and old_change.cause_version == 1


def test_reschedule_moves_only_proven_causal_cut_and_rejects_changed_target():
    from dataclasses import replace

    from app.domains.students.education_change_planner import (
        EducationTimelineSegment,
        EndProvenance,
    )
    from app.domains.students.teaching_change_planner import reschedule_teaching_dependents

    content = {
        "offering_source_key": "offer:one",
        "assignment_source_key": "staff:old",
        "teaching_role": "lead",
    }
    provenance = EndProvenance(
        cause_source_key="teacher:new",
        cause_version=1,
        ended_target_version=2,
        prior_effective_at=JAN,
        prior_content={**content, "valid_to": None},
    )
    old = replace(
        snap("teaching_assignment", "teacher:old", **content),
        version=2,
        segments=(
            EducationTimelineSegment(
                effective_at=JAN, valid_to=OCT, content=content, provenance=provenance
            ),
        ),
    )
    proposal = change(
        "teaching_assignment",
        "teacher:new",
        "correct",
        content,
        at=datetime(2026, 11, 1, tzinfo=UTC),
    )
    result = reschedule_teaching_dependents(
        proposal, (old,), from_at=OCT, explicit_keys=frozenset({"teacher:new"})
    )
    assert result[0]["replacement_segments"][0]["valid_to"].month == 11
    with pytest.raises(ValueError):
        reschedule_teaching_dependents(
            proposal,
            (replace(old, version=3),),
            from_at=OCT,
            explicit_keys=frozenset({"teacher:new"}),
        )
