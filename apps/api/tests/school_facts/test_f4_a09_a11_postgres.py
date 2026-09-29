"""Prepared opt-in A09/A11 scenarios; use only the existing isolated F4 Writer fixture."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import text

from app.composition.school_fact_education_query import active_segment, resolve_grade_from_records
from app.domains.students.education_contracts import EDUCATION_CONTENT_FIELDS
from app.platform.school_facts.application import SchoolFactChangeRejected
from tests.school_facts.test_f4_reader_postgres import query
from tests.school_facts.test_f4_writer_postgres import apply
from tests.school_facts.test_f4_writer_postgres import writer as original_writer

writer = original_writer

pytestmark = pytest.mark.skipif(
    not os.environ.get("F4_WRITER_PG"), reason="explicit isolated F4 PG only"
)


def seed(writer, count):
    token = uuid4().hex
    start = datetime.now(UTC) - timedelta(seconds=90)
    keys = {
        name: "f4:a09a11:" + token + ":" + name
        for name in (
            "class1",
            "class2",
            "new1",
            "new2",
            "room",
            "building",
            "floor",
            "stage",
            "grade1",
            "grade2",
            "calendar",
            "placement",
        )
    }
    for number in range(1, count + 1):
        keys.update(
            {
                prefix + str(number): "f4:a09a11:" + token + ":" + prefix + str(number)
                for prefix in (
                    "person",
                    "student",
                    "ssr",
                    "member",
                    "teacher",
                    "roomlink",
                    "position",
                    "officer",
                )
            }
        )

    def fact(kind, key, content, at=start, expected=0, operation="upsert"):
        return dict(
            fact_kind=kind,
            source_key=keys.get(key, key),
            content=content,
            effective_at=at,
            expected_version=expected,
            operation=operation,
        )

    with writer[0]["migration"].connect() as connection:
        campus = connection.scalar(
            text(
                "SELECT l.source_key FROM campus_locations l JOIN "
                "campus_location_versions v ON v.location_id=l.id WHERE v.kind='campus' "
                "AND v.valid_to IS NULL LIMIT 1"
            )
        )
        teacher = connection.scalar(
            text(
                "SELECT source_key FROM staff_assignments WHERE status='confirmed' AND "
                "revoked_at IS NULL AND valid_from<=:at AND (valid_to IS NULL OR "
                "valid_to>:at) ORDER BY valid_from LIMIT 1"
            ),
            {"at": start},
        )
    assert campus and teacher
    entries = []
    ssrs = {}
    for number in range(1, count + 1):
        n = str(number)
        ssrs[number] = {
            "student_source_key": keys["student" + n],
            "student_number": token[:12] + n,
            "enrollment_status": "enrolled",
            "residence_mode": "day",
            "primary_campus_source_key": campus,
            "administrative_campus_source_key": campus,
        }
        entries.extend(
            [
                fact(
                    "person",
                    "person" + n,
                    {
                        "display_name": "Isolated scenario " + n,
                        "person_kind": "other",
                        "lifecycle_status": "active",
                    },
                ),
                fact(
                    "student",
                    "student" + n,
                    {"person_source_key": keys["person" + n], "status": "active"},
                ),
                fact("student_school_relationship", "ssr" + n, ssrs[number]),
            ]
        )
    apply(writer, entries)
    return keys, start, fact, campus, teacher, ssrs


def test_a09_suspension_resumption_retention_and_explicit_unassigned_are_independent(writer):
    keys, start, fact, campus, teacher, ssrs = seed(writer, 1)
    suspended, resumed = start + timedelta(seconds=20), start + timedelta(seconds=40)
    cycle = start.year if (start.month, start.day) >= (9, 1) else start.year - 1
    boundary = datetime(cycle + 1, 9, 1, tzinfo=UTC)
    next_grade = boundary + timedelta(days=1)
    apply(
        writer,
        [
            fact("education_class", "class1", {"display_name": "Original class"}),
            fact(
                "student_class_membership",
                "member1",
                {
                    "student_source_key": keys["student1"],
                    "school_relationship_source_key": keys["ssr1"],
                    "membership_status": "assigned",
                    "class_source_key": keys["class1"],
                },
            ),
            fact("education_stage", "stage", {"display_name": "Two-grade test stage"}),
            fact(
                "education_grade",
                "grade1",
                {"display_name": "Grade one", "stage_source_key": keys["stage"], "grade_order": 1},
            ),
            fact(
                "education_grade",
                "grade2",
                {"display_name": "Grade two", "stage_source_key": keys["stage"], "grade_order": 2},
            ),
            fact(
                "education_calendar_rule",
                "calendar",
                {
                    "display_name": "Synthetic Sep boundary",
                    "stage_source_key": keys["stage"],
                    "start_cycle": cycle,
                    "month": 9,
                    "day": 1,
                    "timezone_name": "UTC",
                    "grade_source_keys": [keys["grade1"], keys["grade2"]],
                },
            ),
            fact(
                "student_grade_placement",
                "placement",
                {
                    "student_source_key": keys["student1"],
                    "calendar_source_key": keys["calendar"],
                    "grade_source_key": keys["grade1"],
                },
            ),
        ],
    )
    apply(
        writer,
        [
            fact(
                "student_school_relationship",
                "ssr1",
                {**ssrs[1], "enrollment_status": "suspended"},
                suspended,
                1,
            )
        ],
    )
    apply(
        writer,
        [
            fact("student_school_relationship", "ssr1", ssrs[1], resumed, 2),
            fact(
                "student_class_membership",
                "member1",
                {
                    "student_source_key": keys["student1"],
                    "school_relationship_source_key": keys["ssr1"],
                    "membership_status": "unassigned",
                },
                resumed,
                1,
            ),
        ],
    )
    for at, status in (
        (start + timedelta(seconds=1), "enrolled"),
        (suspended + timedelta(seconds=1), "suspended"),
        (resumed + timedelta(seconds=1), "enrolled"),
    ):
        with writer[0]["reader"].connect() as connection:
            row = connection.scalar(
                text(
                    "SELECT app.school_fact_f3_staff_target(:s,'student_school_relationship"
                    "',:key,false,:at,ARRAY['enrollment_status','valid_to'])"
                ),
                {"s": writer[1].school_id, "key": keys["ssr1"], "at": at},
            )
        assert row["content"]["enrollment_status"] == status
    membership = query(
        writer,
        "student_class_membership",
        [keys["member1"]],
        list(EDUCATION_CONTENT_FIELDS["student_class_membership"]),
        resumed,
    )["records"][0]
    assert active_segment(membership, suspended)["content"]["class_source_key"] == keys["class1"]
    assert active_segment(membership, resumed)["content"]["membership_status"] == "unassigned"
    assert active_segment(membership, resumed)["content"].get("class_source_key") is None

    def grade(at):
        calendar = query(
            writer,
            "education_calendar_rule",
            [keys["calendar"]],
            list(EDUCATION_CONTENT_FIELDS["education_calendar_rule"]),
            at,
        )["records"][0]
        placement = query(
            writer,
            "student_grade_placement",
            [keys["placement"]],
            list(EDUCATION_CONTENT_FIELDS["student_grade_placement"]),
            at,
        )["records"][0]
        return resolve_grade_from_records(
            at=at,
            calendars=(calendar,),
            placement_record=placement,
            placement=active_segment(placement, at),
        )

    assert grade(next_grade).grade_id == keys["grade2"]
    apply(
        writer,
        [
            fact(
                "student_grade_placement",
                "placement",
                {
                    "student_source_key": keys["student1"],
                    "calendar_source_key": keys["calendar"],
                    "grade_source_key": keys["grade1"],
                },
                boundary,
                1,
            )
        ],
    )
    assert grade(next_grade).grade_id == keys["grade1"]
    # Exhausting the supported grades cannot write actual graduation into the SSR owner.
    after_final = datetime(cycle + 3, 9, 2, tzinfo=UTC)
    assert grade(after_final).status == "unknown"
    with writer[0]["reader"].connect() as connection:
        actual = connection.scalar(
            text(
                "SELECT app.school_fact_f3_staff_target(:s,'student_school_relationship"
                "',:key,false,:at,ARRAY['enrollment_status'])"
            ),
            {"s": writer[1].school_id, "key": keys["ssr1"], "at": datetime.now(UTC)},
        )
    assert actual["content"]["enrollment_status"] == "enrolled"


def test_a11_two_old_classes_two_new_classes_reject_missing_duplicate_and_do_not_inherit(writer):
    keys, start, fact, campus, teacher, ssrs = seed(writer, 3)
    at = start + timedelta(seconds=60)
    parent = campus
    locations = []
    for name, kind in (("building", "building"), ("floor", "floor"), ("room", "room")):
        locations.append(
            fact(
                "location",
                name,
                {
                    "kind": kind,
                    "parent_source_key": parent,
                    "display_name": "Isolated " + name,
                    "display_path": "Isolated/" + name,
                    "status": "active",
                },
            )
        )
        parent = keys[name]
    apply(writer, locations)
    classcontent = {
        "display_name": "Old class",
        "teacher_mode": "single_primary",
        "teacher_role_limits": {"primary": 1},
        "primary_classroom_limit": 1,
    }
    entries = [
        fact("education_class", name, {**classcontent, "display_name": name})
        for name in ("class1", "class2")
    ]
    for n in (1, 2):
        entries += [
            fact(
                "class_teacher_assignment",
                "teacher" + str(n),
                {
                    "class_source_key": keys["class" + str(n)],
                    "assignment_source_key": teacher,
                    "teacher_role": "primary",
                },
            ),
            fact(
                "class_location",
                "roomlink" + str(n),
                {
                    "class_source_key": keys["class" + str(n)],
                    "location_source_key": keys["room"],
                    "location_purpose": "main_classroom",
                },
            ),
            fact(
                "class_position_definition",
                "position" + str(n),
                {
                    "class_source_key": keys["class" + str(n)],
                    "display_name": "Monitor",
                    "allow_multiple": False,
                    "allow_concurrent": False,
                    "max_holders": 1,
                },
            ),
        ]
    for n in (1, 2, 3):
        old = 1 if n == 1 else 2
        entries.append(
            fact(
                "student_class_membership",
                "member" + str(n),
                {
                    "student_source_key": keys["student" + str(n)],
                    "school_relationship_source_key": keys["ssr" + str(n)],
                    "membership_status": "assigned",
                    "class_source_key": keys["class" + str(old)],
                },
            )
        )
    for n, student in ((1, 1), (2, 2)):
        entries.append(
            fact(
                "student_class_position",
                "officer" + str(n),
                {
                    "class_source_key": keys["class" + str(n)],
                    "student_source_key": keys["student" + str(student)],
                    "position_source_key": keys["position" + str(n)],
                },
            )
        )
    apply(writer, entries)
    final = [fact("education_class", name, {"display_name": name}, at) for name in ("new1", "new2")]
    for n in (1, 2):
        final += [
            fact(kind, prefix + str(n), None, at, 1, "end")
            for kind, prefix in (
                ("education_class", "class"),
                ("class_teacher_assignment", "teacher"),
                ("class_location", "roomlink"),
                ("class_position_definition", "position"),
                ("student_class_position", "officer"),
            )
        ]
    for n in (1, 2, 3):
        content = {
            "student_source_key": keys["student" + str(n)],
            "school_relationship_source_key": keys["ssr" + str(n)],
            "membership_status": "assigned" if n < 3 else "unassigned",
        }
        if n < 3:
            content["class_source_key"] = keys["new" + str(n)]
        final.append(fact("student_class_membership", "member" + str(n), content, at, 1))

    def snapshot():
        with writer[0]["migration"].connect() as connection:
            return connection.execute(
                text(
                    "SELECT (SELECT count(*) FROM school_education_facts),(SELECT count(*) "
                    "FROM school_education_versions),(SELECT count(*) FROM "
                    "school_fact_change_lineage),(SELECT max(target_fact_version) FROM "
                    "master_data_import_batches WHERE status='applied')"
                )
            ).one()

    before = snapshot()
    # Actual formal Reader material feeds the product coordinator. Reference and
    # scope resolution remain the explicit fixture, not production grant evidence.
    import json

    from app.composition.school_fact_education_product import EducationBatchInput
    from tests.school_facts.test_f4_education_maintenance import environment

    original_records = []
    school_version = None
    for kind in sorted({c["fact_kind"] for c in entries}):
        raw = query(
            writer,
            kind,
            [c["source_key"] for c in entries if c["fact_kind"] == kind],
            list(EDUCATION_CONTENT_FIELDS[kind]),
            at,
        )
        original_records.extend(raw["records"])
        school_version = raw["school_fact_version"]
    coordinator, _ = environment(original_records, school_version)
    inputs = [
        dict(
            fact_kind=c["fact_kind"],
            operation=c["operation"],
            **{
                ("target_ref" if c["expected_version"] else "creation_ref"): (
                    "f4:" if c["expected_version"] else "f4create:"
                )
                + c["fact_kind"]
                + ":"
                + c["source_key"]
            },
            fields=coordinator.adapter.public_fields(writer[1], c["content"] or {}),
            reason="Explicit two-class regroup roster",
            effective_at=at.isoformat(),
        )
        for c in final
    ]
    for item in inputs:
        if (
            item["fact_kind"] == "student_class_membership"
            and item["fields"].get("membership_status") == "unassigned"
        ):
            item["fields"]["class_ref"] = (
                None  # Partial form update explicitly clears the old class.
            )
    proposal = coordinator.resolve_change(
        actor=writer[1],
        payload=EducationBatchInput.model_validate_json(
            json.dumps({**inputs[0], "additional_changes": inputs[1:]})
        ),
        request_id=uuid4(),
        effective_at=at,
    )
    assert snapshot() == before
    roster = proposal.summary.regroup
    assert roster["scope"] == "batch_only"
    period = roster["periods"][0]
    assert period["student_count"] == 3
    assert period["unassigned"] == ["f4:student:" + keys["student3"]]
    counts = {r["class_ref"]: r for r in period["classes"]}
    assert counts["f4:education_class:" + keys["class1"]]["class_state"] == "ended"
    assert counts["f4:education_class:" + keys["class2"]]["outgoing_count"] == 2
    assert counts["f4:education_class:" + keys["new1"]]["incoming_count"] == 1
    assert counts["f4:education_class:" + keys["new2"]]["incoming_count"] == 1
    assert {
        c.source_key for c in proposal.change_set.changes if c.fact_kind == "student_class_position"
    } == {keys["officer1"], keys["officer2"]}
    final = [c.model_dump(mode="json", exclude_unset=True) for c in proposal.change_set.changes]
    with pytest.raises(SchoolFactChangeRejected):
        apply(writer, [c for c in final if c["source_key"] != keys["member3"]])
    assert snapshot() == before
    duplicate = fact(
        "student_class_membership",
        "f4:a11:duplicate:" + uuid4().hex,
        {
            "student_source_key": keys["student2"],
            "school_relationship_source_key": keys["ssr2"],
            "membership_status": "assigned",
            "class_source_key": keys["new1"],
        },
        at,
    )
    with pytest.raises(SchoolFactChangeRejected):
        apply(writer, [*final, duplicate])
    assert snapshot() == before
    result = apply(writer, final)
    assert result.changed_count == len(final)
    members = query(
        writer,
        "student_class_membership",
        [keys["member" + str(n)] for n in (1, 2, 3)],
        list(EDUCATION_CONTENT_FIELDS["student_class_membership"]),
        at,
    )["records"]
    destinations = {r["source_key"]: active_segment(r, at)["content"] for r in members}
    assert destinations[keys["member1"]]["class_source_key"] == keys["new1"]
    assert destinations[keys["member2"]]["class_source_key"] == keys["new2"]
    assert destinations[keys["member3"]]["membership_status"] == "unassigned"
    with writer[0]["migration"].connect() as connection:
        inherited = connection.scalar(
            text(
                "SELECT count(*) FROM school_education_facts f JOIN "
                "school_education_versions v ON v.fact_id=f.id AND "
                "v.school_id=f.school_id WHERE f.kind IN ('class_teacher_assignment','c"
                "lass_location','student_class_position','class_position_definition') "
                "AND v.superseded_at IS NULL AND "
                "v.payload->>'class_source_key'=ANY(CAST(:keys AS text[]))"
            ),
            {"keys": [keys["new1"], keys["new2"]]},
        )
    assert inherited == 0


def test_a18_old_class_closure_confirmation_rejects_new_officer_without_partial_ends(writer):
    """Actual prepare/confirm CAS; the explicit test Writer actor is not a grant test."""
    import json
    from hashlib import sha256

    from app.platform.school_facts.contracts import CanonicalFactChangeSetV3

    keys, start, fact, _, _, _ = seed(writer, 1)
    apply(
        writer,
        [
            fact("education_class", "class1", {"display_name": "Closure CAS class"}),
            fact(
                "student_class_membership",
                "member1",
                {
                    "student_source_key": keys["student1"],
                    "school_relationship_source_key": keys["ssr1"],
                    "membership_status": "assigned",
                    "class_source_key": keys["class1"],
                },
            ),
            fact(
                "class_position_definition",
                "position1",
                {
                    "class_source_key": keys["class1"],
                    "display_name": "Monitor",
                    "allow_multiple": False,
                    "allow_concurrent": False,
                    "max_holders": 1,
                },
            ),
        ],
    )
    at = datetime.now(UTC) + timedelta(seconds=20)
    with writer[0]["migration"].connect() as connection:
        base = connection.scalar(
            text(
                "SELECT max(target_fact_version) FROM "
                "master_data_import_batches WHERE status='applied'"
            )
        )
    changes = [
        fact(kind, name, None, at, 1, "end")
        for kind, name in (
            ("education_class", "class1"),
            ("student_class_membership", "member1"),
            ("class_position_definition", "position1"),
        )
    ]
    plan = CanonicalFactChangeSetV3.model_validate_json(
        json.dumps(
            {
                "schema_version": 3,
                "base_version": base,
                "source_sha256": sha256(uuid4().bytes).hexdigest(),
                "changes": [
                    {
                        "schema_version": 3,
                        "origin": {"sheet_code": "education", "row_number": i + 1},
                        **c,
                    }
                    for i, c in enumerate(changes)
                ],
            },
            default=str,
        )
    )
    _, _, identity, context, service = writer
    prepared = service.prepare_confirmation(
        change_set=plan, context=context, session_binding=identity.session_nonce_hash
    )
    apply(
        writer,
        [
            fact(
                "student_class_position",
                "officer1",
                {
                    "class_source_key": keys["class1"],
                    "student_source_key": keys["student1"],
                    "position_source_key": keys["position1"],
                },
                datetime.now(UTC),
            )
        ],
    )

    def snapshot():
        with writer[0]["migration"].connect() as connection:
            return connection.execute(
                text(
                    "SELECT source_key,version FROM school_education_facts "
                    "WHERE school_id=:school AND source_key=ANY(:keys) ORDER BY source_key"
                ),
                {
                    "school": writer[1].school_id,
                    "keys": [keys[n] for n in ("class1", "member1", "position1", "officer1")],
                },
            ).all()

    before = snapshot()
    assert len(before) == 4 and all(row.version == 1 for row in before)
    for _ in range(2):
        with pytest.raises(SchoolFactChangeRejected):
            service.confirm(
                credential=prepared.credential,
                change_set=plan,
                context=context,
                session_binding=identity.session_nonce_hash,
            )
        assert snapshot() == before
    # An old confirmation cannot end any original relation or the new officer.
    for kind, name in (
        ("education_class", "class1"),
        ("student_class_membership", "member1"),
        ("class_position_definition", "position1"),
        ("student_class_position", "officer1"),
    ):
        result = query(writer, kind, [keys[name]], list(EDUCATION_CONTENT_FIELDS[kind]), at)
        assert result["records"][0]["segments"][-1]["valid_to"] is None
