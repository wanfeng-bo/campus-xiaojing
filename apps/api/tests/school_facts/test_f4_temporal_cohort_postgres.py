"""Three bounded temporal/cohort scenarios, only in the explicitly owned F4 DB."""

# ruff: noqa: F811
from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import text

from app.composition.school_fact_education_query import active_segment, resolve_grade_from_records
from app.domains.students.education_contracts import EDUCATION_CONTENT_FIELDS
from app.platform.school_facts.application import SchoolFactChangeRejected
from tests.school_facts.test_f4_reader_postgres import query
from tests.school_facts.test_f4_writer_postgres import apply, writer  # noqa: F401

pytestmark = pytest.mark.skipif(
    not os.environ.get("F4_WRITER_PG"), reason="explicit owned F4 PG target required"
)


def fact(kind, key, content, at, *, version=0, operation="upsert"):
    return dict(
        fact_kind=kind,
        source_key=key,
        content=content,
        effective_at=at,
        expected_version=version,
        operation=operation,
    )


def setup_school(w):
    token = uuid4().hex
    k = {
        n: "temporal:" + n + ":" + token
        for n in (
            "stage",
            "calendar",
            "placement",
            "class",
            "person",
            "student",
            "ssr",
            "member",
            "teacher",
            "room",
            "post",
            "holder",
            "g1",
            "g2",
            "g3",
            "g4",
        )
    }
    now = datetime.now(UTC)
    start = now - timedelta(days=1)
    # Anchor in this year's April cycle, after original fixture relations began.
    assert now.month >= 4, "This September fixture must be re-bound outside its fixed window"
    with w[0]["migration"].connect() as c:
        campus = c.scalar(
            text(
                "SELECT l.source_key FROM campus_locations l JOIN "
                "campus_location_versions v ON v.location_id=l.id "
                "WHERE v.kind='campus' AND v.valid_to IS NULL LIMIT 1"
            )
        )
        assignment = c.scalar(
            text(
                "SELECT source_key FROM staff_assignments WHERE school_id=:s "
                "AND valid_from<=:at AND valid_to IS NULL LIMIT 1"
            ),
            dict(s=w[1].school_id, at=start),
        )
    assert campus and assignment
    rule = dict(
        display_name="Four-year April cycle",
        stage_source_key=k["stage"],
        start_cycle=now.year,
        month=4,
        day=1,
        timezone_name="Asia/Shanghai",
        grade_source_keys=[k[f"g{i}"] for i in range(1, 5)],
    )
    rows = [
        fact(
            "person",
            k["person"],
            dict(
                display_name="Temporal cohort pupil", person_kind="other", lifecycle_status="active"
            ),
            start,
        ),
        fact("student", k["student"], dict(person_source_key=k["person"], status="active"), start),
        fact(
            "student_school_relationship",
            k["ssr"],
            dict(
                student_source_key=k["student"],
                student_number=token[:12],
                enrollment_status="enrolled",
                residence_mode="day",
                primary_campus_source_key=campus,
                administrative_campus_source_key=campus,
            ),
            start,
        ),
        fact("education_stage", k["stage"], dict(display_name="Configured four-year stage"), start),
        *[
            fact(
                "education_grade",
                k[f"g{i}"],
                dict(display_name=f"Level {i}", stage_source_key=k["stage"], grade_order=i),
                start,
            )
            for i in range(1, 5)
        ],
        fact("education_calendar_rule", k["calendar"], rule, start),
        fact(
            "education_class",
            k["class"],
            dict(
                display_name="Fixed 909 class",
                teacher_mode="single_primary",
                teacher_role_limits={"primary": 1},
                primary_classroom_limit=1,
            ),
            start,
        ),
        fact(
            "class_placement",
            k["placement"],
            dict(
                class_source_key=k["class"],
                calendar_source_key=k["calendar"],
                grade_source_key=k["g1"],
                campus_source_key=campus,
                class_number=91,
            ),
            start,
        ),
        fact(
            "student_class_membership",
            k["member"],
            dict(
                student_source_key=k["student"],
                school_relationship_source_key=k["ssr"],
                membership_status="assigned",
                class_source_key=k["class"],
            ),
            start,
        ),
        fact(
            "class_teacher_assignment",
            k["teacher"],
            dict(
                class_source_key=k["class"],
                assignment_source_key=assignment,
                teacher_role="primary",
            ),
            start,
        ),
        fact(
            "class_location",
            k["room"],
            dict(
                class_source_key=k["class"],
                location_source_key=campus,
                location_purpose="other_fixed",
            ),
            start,
        ),
        fact(
            "class_position_definition",
            k["post"],
            dict(
                class_source_key=k["class"],
                display_name="Officer",
                allow_multiple=False,
                allow_concurrent=True,
                max_holders=1,
            ),
            start,
        ),
        fact(
            "student_class_position",
            k["holder"],
            dict(
                class_source_key=k["class"],
                student_source_key=k["student"],
                position_source_key=k["post"],
            ),
            start,
        ),
    ]
    apply(w, rows)
    return k, rule, now


def resolve(w, k, at, known=None):
    calendars = query(
        w,
        "education_calendar_rule",
        [k["calendar"]],
        list(EDUCATION_CONTENT_FIELDS["education_calendar_rule"]),
        at,
        known,
    )
    placements = query(
        w,
        "class_placement",
        [k["placement"]],
        list(EDUCATION_CONTENT_FIELDS["class_placement"]),
        at,
        known,
    )
    record = placements["records"][0]
    segment = active_segment(record, at)
    assert segment is not None
    return resolve_grade_from_records(
        at=at,
        calendars=tuple(calendars["records"]),
        placement_record=record,
        placement=segment,
        known_at=known,
    )


def batch_count(w):
    with w[0]["migration"].connect() as c:
        return c.scalar(
            text("SELECT COUNT(*) FROM master_data_import_batches WHERE status='applied'")
        )


def test_configured_boundaries_reconnect_without_annual_writes_or_role_changes(writer):
    k, _rule, now = setup_school(writer)
    boundary = datetime(now.year + 1, 3, 31, 16, tzinfo=UTC)
    before = batch_count(writer)
    expected = [
        (boundary - timedelta(microseconds=1), "g1"),
        (boundary, "g2"),
        (boundary + timedelta(microseconds=1), "g2"),
        (datetime(now.year + 2, 3, 31, 16, tzinfo=UTC), "g3"),
    ]
    baseline = {}
    kinds = dict(
        education_class="class",
        class_teacher_assignment="teacher",
        class_location="room",
        student_class_position="holder",
    )
    for at, grade in expected:
        writer[0]["reader"].dispose()  # New physical checkout, no cached resolver state.
        assert resolve(writer, k, at).grade_id == k[grade]
        assert resolve(writer, k, at).grade_id == k[grade]
        for kind, name in kinds.items():
            raw = query(writer, kind, [k[name]], list(EDUCATION_CONTENT_FIELDS[kind]), at)
            item = raw["records"][0]
            assert item["status"] == "available" and len(item["segments"]) == 1
            signature = (
                raw["school_fact_version"],
                item["version"],
                item["segments"][0]["content"],
            )
            baseline.setdefault(kind, signature)
            assert signature == baseline[kind]
    last = resolve(writer, k, datetime(now.year + 4, 3, 31, 16, tzinfo=UTC))
    assert last.status == "unknown" and last.grade_id is None
    assert batch_count(writer) == before


def test_future_rule_cancel_restores_calendar_and_retains_as_known_revision(writer):
    from app.composition.school_fact_education_product import EducationBatchInput
    from tests.school_facts.test_f4_education_maintenance import environment

    k, rule, now = setup_school(writer)
    future = datetime(now.year + 1, 3, 31, 16, tzinfo=UTC)
    changed = dict(rule, start_cycle=now.year + 1, month=5)
    apply(writer, [fact("education_calendar_rule", k["calendar"], changed, future, version=1)])
    known = datetime.now(UTC)
    april = datetime(now.year + 1, 4, 15, tzinfo=UTC)
    may = datetime(now.year + 1, 4, 30, 16, tzinfo=UTC)
    assert resolve(writer, k, april).grade_id == k["g1"]
    assert resolve(writer, k, may).grade_id == k["g2"]
    raw = query(
        writer,
        "education_calendar_rule",
        [k["calendar"]],
        list(EDUCATION_CONTENT_FIELDS["education_calendar_rule"]),
        now,
    )
    coordinator, _ = environment(raw["records"], raw["school_fact_version"])
    payload = EducationBatchInput.model_validate_json(
        json.dumps(
            dict(
                fact_kind="education_calendar_rule",
                target_ref="f4:education_calendar_rule:" + k["calendar"],
                operation="cancel",
                fields={},
                reason="Withdraw exact future calendar replacement",
            )
        )
    )
    plan = coordinator.resolve_change(
        actor=writer[1], payload=payload, request_id=uuid4(), effective_at=datetime.now(UTC)
    )
    apply(writer, [c.model_dump(mode="json", exclude_unset=True) for c in plan.change_set.changes])
    writer[0]["reader"].dispose()
    assert resolve(writer, k, april).grade_id == k["g2"]
    assert resolve(writer, k, may).grade_id == k["g2"]
    assert resolve(writer, k, april, known).grade_id == k["g1"]
    assert resolve(writer, k, now).grade_id == k["g1"]


def test_three_cohort_meanings_are_independent_and_bad_batch_is_atomic(writer):
    k, _rule, now = setup_school(writer)
    kinds = ("original_intake", "school_transfer_intake", "expected_graduation")
    cohorts = [k["stage"] + ":" + kind for kind in kinds]
    members = [key + ":member" for key in cohorts]
    rows = []
    for index, (kind, key, member) in enumerate(zip(kinds, cohorts, members, strict=True)):
        rows += [
            fact(
                "education_cohort",
                key,
                dict(
                    display_name=kind,
                    stage_source_key=k["stage"],
                    cohort_kind=kind,
                    academic_year=now.year + index,
                ),
                now,
            ),
            fact(
                "student_cohort_membership",
                member,
                dict(student_source_key=k["student"], cohort_source_key=key),
                now,
            ),
        ]
    apply(writer, rows)
    raw = query(writer, "education_cohort", cohorts, ["cohort_kind", "academic_year"], now)
    assert {r["segments"][0]["content"]["cohort_kind"] for r in raw["records"]} == set(kinds)
    before = batch_count(writer)
    first = cohorts[0] + ":rollback"
    with pytest.raises(SchoolFactChangeRejected):
        apply(
            writer,
            [
                fact(
                    "education_cohort",
                    first,
                    dict(
                        display_name="Must roll back",
                        stage_source_key=k["stage"],
                        cohort_kind="original_intake",
                    ),
                    now,
                ),
                fact(
                    "student_cohort_membership",
                    first + ":bad",
                    dict(student_source_key=k["student"], cohort_source_key=k["class"]),
                    now,
                ),
            ],
        )
    assert batch_count(writer) == before
    assert (
        query(writer, "education_cohort", [first], ["display_name"], now)["records"][0]["status"]
        == "not_recorded"
    )
    stopped = datetime.now(UTC)
    apply(
        writer,
        [fact("student_cohort_membership", members[1], None, stopped, version=1, operation="end")],
    )
    current = query(
        writer,
        "student_cohort_membership",
        members,
        ["student_source_key", "cohort_source_key"],
        stopped,
    )
    assert {r["source_key"]: r["status"] for r in current["records"]} == dict(
        zip(members, ("available", "no_current_records", "available"), strict=True)
    )
    assert (
        query(writer, "student_cohort_membership", [members[1]], ["cohort_source_key"], now)[
            "records"
        ][0]["status"]
        == "available"
    )
    assert (
        query(writer, "student_class_membership", [k["member"]], ["class_source_key"], stopped)[
            "records"
        ][0]["status"]
        == "available"
    )
