"""Opt-in owned fixture35: real confirmation/Writer, planner and narrow education Reader.

Uses new isolated source keys and existing login authority; never provisions roles or DBs.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import text

from app.composition.school_fact_education_maintenance import snapshot_at
from app.composition.school_fact_education_query import active_segment
from app.composition.school_fact_teaching_query import teaching_view
from app.domains.students.education_change_planner import (
    EducationChangeCommand,
    EducationFactSnapshot,
    plan_education_change,
)
from app.domains.students.teaching_change_planner import plan_teaching_change
from app.platform.school_facts.f3_policy import F3_FACT_FIELDS
from app.platform.school_facts.f4_policy import F4_FACT_FIELDS
from app.platform.school_facts.f5_policy import F5_FACT_FIELDS
from tests.school_facts.test_f4_writer_postgres import apply
from tests.school_facts.test_f5_writer_reader_postgres import writer as owned_writer  # noqa: F401

pytestmark = pytest.mark.skipif(
    not os.environ.get("F5_TEACHING_PG"), reason="explicit owned F5 fixture35 required"
)


def change(kind, key, at, content=None, *, operation="upsert", expected=0):
    return dict(
        schema_version=3,
        origin={"sheet_code": "F5-temporal-owned", "row_number": 1},
        fact_kind=kind,
        source_key=key,
        operation=operation,
        expected_version=expected,
        effective_at=at,
        content=content,
        **(
            {"correction_reason": "隔离合成教学时间核对"}
            if operation in {"cancel", "correct"}
            else {}
        ),
    )


def read_rows(writer, kind, keys, at):
    fields = F5_FACT_FIELDS[kind] if kind in F5_FACT_FIELDS else F4_FACT_FIELDS[kind]
    family = "f5" if kind in F5_FACT_FIELDS else "f4"
    with writer[0]["reader"].connect() as conn:
        raw = conn.scalar(
            text(
                f"SELECT app.school_fact_{family}_targets"
                "(:school,:kind,:keys,:fields,:at,NULL,'{}'::jsonb)"
            ),
            dict(
                school=writer[1].school_id, kind=kind, keys=list(keys), fields=sorted(fields), at=at
            ),
        )
    return [dict(row, fact_kind=kind) for row in raw["records"]]


def only_active(writer, kind, keys, at):
    return {
        r["source_key"]: segment["content"]
        for r in read_rows(writer, kind, keys, at)
        if (segment := active_segment(r, at)) is not None
    }


def ssr_row(writer, key, at):
    with writer[0]["reader"].connect() as conn:
        row = conn.scalar(
            text(
                "SELECT app.school_fact_f3_staff_target(:school,'student_school_relationship',"
                ":key,false,:at,:fields)"
            ),
            dict(
                school=writer[1].school_id,
                key=key,
                at=at,
                fields=sorted(F3_FACT_FIELDS["student_school_relationship"]),
            ),
        )
    assert row["status"] == "available"
    assert row["source_reference"]
    return row


def ssr_content(writer, key, at):
    return ssr_row(writer, key, at)["content"]


def ssr_snapshot(writer, key, at):
    row = ssr_row(writer, key, at)
    return EducationFactSnapshot(
        fact_kind="student_school_relationship",
        source_key=key,
        version=row["expected_version"],
        effective_at=datetime.fromisoformat(row["valid_from"]),
        content=row["content"],
    )


@pytest.fixture
def graph(owned_writer, monkeypatch):  # noqa: F811
    writer = owned_writer
    from app.composition.school_fact_persistence import SqlAlchemySchoolFactPersistence

    original_validation = SqlAlchemySchoolFactPersistence._validate_education_state

    def diagnose(*args, **kwargs):
        try:
            return original_validation(*args, **kwargs)
        except Exception as exc:
            print("F5_OWNED_INVARIANT", str(exc))
            raise

    monkeypatch.setattr(
        SqlAlchemySchoolFactPersistence, "_validate_education_state", staticmethod(diagnose)
    )
    original_prepare = writer[4].prepare_confirmation

    def prepare_with_report(**kwargs):
        report = writer[4].preflight(
            school_id=writer[1].school_id,
            change_set=kwargs["change_set"],
            context=kwargs["context"],
            session_binding=kwargs["session_binding"],
        )
        if not report.is_clean:
            print("F5_OWNED_PREFLIGHT", report.model_dump_json())
        return original_prepare(**kwargs)

    monkeypatch.setattr(writer[4], "prepare_confirmation", prepare_with_report)
    now = datetime.now(UTC)
    at = now - timedelta(seconds=2)
    prefix = "f5-temporal:" + uuid4().hex + ":"

    def k(name):
        return prefix + name

    with writer[0]["migration"].connect() as conn:
        campus = conn.scalar(
            text(
                "SELECT l.source_key FROM campus_locations l JOIN campus_location_versions v "
                "ON v.location_id=l.id WHERE v.kind='campus' AND v.valid_to IS NULL LIMIT 1"
            )
        )
    org = {
        "kind": "department",
        "display_name": "隔离教学组",
        "display_path": "隔离教学组",
        "status": "active",
    }
    foundation = [change("organization", k("org"), at, org)]
    for name in ("old", "new"):
        foundation += [
            change(
                "person",
                k(name),
                at,
                {
                    "display_name": "隔离老师" + name,
                    "person_kind": "staff",
                    "lifecycle_status": "active",
                },
            ),
            change(
                "assignment",
                k(name + "-staff"),
                at,
                {
                    "person_source_key": k(name),
                    "organization_source_key": k("org"),
                    "position_title": "隔离教师",
                    "semantic_role_code": "teacher",
                    "assignment_kind": "substantive",
                    "appointment_source": "manual",
                    "approval_reference": "owned-F5-temporal",
                    "status": "confirmed",
                },
            ),
        ]
    ssr = {
        "student_source_key": k("student"),
        "student_number": uuid4().hex[:12],
        "enrollment_status": "enrolled",
        "residence_mode": "day",
        "primary_campus_source_key": campus,
        "administrative_campus_source_key": campus,
    }
    foundation += [
        change(
            "person",
            k("child"),
            at,
            {"display_name": "隔离学生", "person_kind": "other", "lifecycle_status": "active"},
        ),
        change("student", k("student"), at, {"person_source_key": k("child"), "status": "active"}),
        change("student_school_relationship", k("ssr"), at, ssr),
    ]
    apply(writer, foundation)
    records = [
        change("teaching_course", k("course"), at, {"display_name": "隔离课程"}),
        change(
            "teaching_group",
            k("group"),
            at,
            {"organization_source_key": k("org"), "group_mode": "shared"},
        ),
        change(
            "teaching_group_member",
            k("member"),
            at,
            {
                "group_source_key": k("group"),
                "student_source_key": k("student"),
                "school_relationship_source_key": k("ssr"),
            },
        ),
    ]
    for name in ("a", "b"):
        records.append(
            change("education_class", k("class-" + name), at, {"display_name": "隔离班" + name})
        )
    for name, source in (("a", "administrative_class"), ("b", "independent_group")):
        records += [
            change(
                "teaching_offering",
                k("offer-" + name),
                at,
                {
                    "course_source_key": k("course"),
                    "display_name": "隔离开课" + name,
                    "offering_status": "active",
                },
            ),
            change(
                "offering_member_source",
                k("source-" + name),
                at,
                {
                    "offering_source_key": k("offer-" + name),
                    "source_kind": source,
                    **(
                        {"class_source_key": k("class-a")}
                        if name == "a"
                        else {"group_source_key": k("group")}
                    ),
                },
            ),
        ]
    records += [
        change(
            "student_class_membership",
            k("class-member"),
            at,
            {
                "class_source_key": k("class-a"),
                "student_source_key": k("student"),
                "school_relationship_source_key": k("ssr"),
                "membership_status": "assigned",
            },
        ),
        change(
            "teaching_assignment",
            k("teacher-old"),
            at,
            {
                "offering_source_key": k("offer-a"),
                "assignment_source_key": k("old-staff"),
                "teaching_role": "lead",
                "valid_to": now + timedelta(days=4),
            },
        ),
    ]
    apply(writer, records)
    return writer, k, now, at, ssr


def test_formal_handover_and_cancel_preserves_independent_cutoff(graph):
    writer, k, now, at, _ = graph
    handover = now + timedelta(days=2)
    cutoff = now + timedelta(days=4)
    old = snapshot_at(
        read_rows(writer, "teaching_assignment", [k("teacher-old")], now)[0],
        kind="teaching_assignment",
        at=now,
    )
    new = change(
        "teaching_assignment",
        k("teacher-new"),
        handover,
        {
            "offering_source_key": k("offer-a"),
            "assignment_source_key": k("new-staff"),
            "teaching_role": "lead",
        },
    )
    cmd = EducationChangeCommand(
        purpose="replace_assignment",
        effective_at=handover,
        cause_source_key=k("teacher-new"),
        cause_version=1,
        primary_changes=(new,),
        replaced_source_keys=frozenset({k("teacher-old")}),
    )
    planned = plan_education_change(
        cmd,
        (old,),
        authorized_keys=frozenset({k("teacher-old"), k("teacher-new")}),
        complete_snapshot=True,
    )
    assert not planned.issues, planned.issues
    result = apply(writer, list(planned.changes))
    assert result.changed_count == 2
    keys = [k("teacher-old"), k("teacher-new")]
    assert set(
        only_active(writer, "teaching_assignment", keys, handover - timedelta(microseconds=1))
    ) == {k("teacher-old")}
    assert set(only_active(writer, "teaching_assignment", keys, handover)) == {k("teacher-new")}
    snapshots = tuple(
        snapshot_at(r, kind="teaching_assignment", at=now)
        for r in read_rows(writer, "teaching_assignment", keys, now)
    )
    cancellation = change(
        "teaching_assignment", k("teacher-new"), handover, operation="cancel", expected=1
    )
    cancelled = plan_teaching_change(
        (cancellation,), snapshots, now=now, authorized_keys=frozenset(keys), complete_snapshot=True
    )
    assert not cancelled.issues, cancelled.issues
    result = apply(writer, list(cancelled.changes))
    assert result.changed_count == 2
    assert set(only_active(writer, "teaching_assignment", keys, handover + timedelta(hours=1))) == {
        k("teacher-old")
    }
    assert only_active(writer, "teaching_assignment", keys, cutoff) == {}
    print(
        "F5_TEMPORAL_EVIDENCE handover_half_open_boundary=true "
        "cancellation_committed_replay_verified=true independent_cutoff_preserved=true batch="
        + str(result.batch_id)
    )


def test_formal_ssr_suspension_resumption_departure_and_class_source_linkage(graph):
    writer, k, now, at, ssr = graph
    membership = read_rows(writer, "teaching_group_member", [k("member")], now)[0]
    baseline = membership["version"]
    # A changes its class join immediately; B keeps its separately maintained membership.
    transfer = datetime.now(UTC)
    current = only_active(writer, "student_class_membership", [k("class-member")], now)[
        k("class-member")
    ]
    apply(
        writer,
        [
            change(
                "student_class_membership",
                k("class-member"),
                transfer,
                {**current, "class_source_key": k("class-b")},
                expected=1,
            )
        ],
    )
    before = only_active(writer, "student_class_membership", [k("class-member")], now)[
        k("class-member")
    ]
    after = only_active(writer, "student_class_membership", [k("class-member")], transfer)[
        k("class-member")
    ]
    a_source = only_active(writer, "offering_member_source", [k("source-a")], transfer)[
        k("source-a")
    ]
    assert (
        before["class_source_key"] == a_source["class_source_key"]
        and after["class_source_key"] != a_source["class_source_key"]
    )
    assert (
        read_rows(writer, "teaching_group_member", [k("member")], transfer)[0]["version"]
        == baseline
    )
    for version, status in ((1, "suspended"), (2, "enrolled")):
        moment = datetime.now(UTC)
        member = snapshot_at(
            read_rows(writer, "teaching_group_member", [k("member")], moment)[0],
            kind="teaching_group_member",
            at=moment,
        )
        relation = ssr_snapshot(writer, k("ssr"), moment)
        planned = plan_teaching_change(
            (
                change(
                    "student_school_relationship",
                    k("ssr"),
                    moment,
                    {**ssr, "enrollment_status": status},
                    expected=version,
                ),
            ),
            (member, relation),
            now=now,
            authorized_keys=frozenset({k("ssr"), k("member")}),
            complete_snapshot=True,
        )
        assert not planned.issues and len(planned.changes) == 1
        apply(writer, list(planned.changes))
        assert (
            read_rows(writer, "teaching_group_member", [k("member")], moment)[0]["version"]
            == baseline
        )
        view = teaching_view(
            records=read_rows(writer, "teaching_course", [k("course")], moment)
            + read_rows(writer, "teaching_offering", [k("offer-b")], moment),
            teachers={},
            at=moment,
            enrollment_status=ssr_content(writer, k("ssr"), moment)["enrollment_status"],
        )
        assert ("participation" in view) == (status == "suspended")
    leave = datetime.now(UTC)
    member = snapshot_at(
        read_rows(writer, "teaching_group_member", [k("member")], leave)[0],
        kind="teaching_group_member",
        at=leave,
    )
    relation = ssr_snapshot(writer, k("ssr"), leave)
    administrative = snapshot_at(
        read_rows(writer, "student_class_membership", [k("class-member")], leave)[0],
        kind="student_class_membership",
        at=leave,
    )
    snapshots = (member, relation, administrative)
    keys = frozenset({k("ssr"), k("member"), k("class-member")})
    primary = change(
        "student_school_relationship",
        k("ssr"),
        leave,
        {**ssr, "enrollment_status": "transferred_out"},
        expected=3,
    )
    # Match the existing coordinator: F3 leaving first closes F4 enrollment, then B membership.
    command = EducationChangeCommand(
        purpose="graduation",
        effective_at=leave,
        cause_source_key=k("ssr"),
        cause_version=4,
        primary_changes=(primary,),
        student_source_key=k("student"),
    )
    education = plan_education_change(
        command, snapshots, authorized_keys=keys, complete_snapshot=True
    )
    assert not education.issues, education.issues
    planned = plan_teaching_change(
        education.changes, snapshots, now=now, authorized_keys=keys, complete_snapshot=True
    )
    assert not planned.issues, planned.issues
    assert {c["source_key"] for c in planned.changes} == set(keys)
    result = apply(writer, list(planned.changes))
    assert result.changed_count == 3
    assert only_active(writer, "student_class_membership", [k("class-member")], leave) == {}
    assert only_active(writer, "teaching_group_member", [k("member")], leave) == {}
    assert ssr_content(writer, k("ssr"), leave)["enrollment_status"] == "transferred_out"
    print(
        "F5_LINKAGE_EVIDENCE class_source_follows=true "
        "independent_group_unchanged_on_transfer=true "
        "suspension_and_resume_keep_membership=true departure_closes_B=true batch="
        + str(result.batch_id)
    )
