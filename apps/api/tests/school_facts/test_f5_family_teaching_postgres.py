"""Fixed teaching SQL and owned PostgreSQL integration; never silently use the live DB."""

import importlib.util
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from tests.school_facts.test_f3_family_postgres import existing_fixture_writer, safe_test_engine
from tests.school_facts.test_f4_writer_postgres import apply


def migration():
    path = Path(__file__).parents[2] / "alembic/versions/20260914_34_f5_family_teaching.py"
    assert path.exists(), "A separate F5 selected-child SQL boundary is required"
    spec = importlib.util.spec_from_file_location("f5_family_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_boundary_does_not_reuse_the_f4_school_wide_walk_or_private_contacts():
    m = migration()
    assert m.down_revision == "20260914_33"
    assert "app_private.f4_child_keys" not in m.FAMILY_SQL
    assert "person_contact_points" not in m.FAMILY_SQL
    assert "r.student_id=p_student" in m.FAMILY_SQL
    assert "app_private.family_relationship_is_authoritative" in m.FAMILY_SQL
    assert "school_fact_family_teaching_stamp" in m.FAMILY_SQL
    assert "school_fact_family_teaching_targets" in m.CORRECTION_SQL
    assert "school_fact_f5_field_changed" in m.CORRECTION_SQL


def test_term_resolution_uses_only_selected_child_formal_calendar():
    sql = migration().FAMILY_SQL
    assert "app_private.f5_child_calendars" in sql
    assert "JOIN app_private.f5_child_calendars" in sql


def test_own_group_membership_is_private_correction_evidence():
    m = migration()
    assert "UNION SELECT o.kind,o.source_key FROM own o" in m.FAMILY_SQL
    assert "k.kind<>'teaching_group_member'" in m.FAMILY_SQL
    assert (
        "WHEN 'teaching_group_member' THEN ARRAY['group_source_key','valid_to']" in m.CORRECTION_SQL
    )


def test_temporal_boundary_is_limited_to_related_people_and_organizations():
    sql = migration().FAMILY_SQL
    assert "app_private.f5_child_boundary" in sql
    assert "JOIN teacher_ids" in sql
    assert "JOIN people_ids" in sql
    assert "JOIN organization_ids" in sql


@pytest.fixture
def owned_family():
    if not os.environ.get("F5_TEACHING_PG"):
        pytest.skip("explicit owned F5 PostgreSQL target required")
    assert os.environ.get("PYTEST_DISABLE_PLUGIN_AUTOLOAD") == "1"
    meta = json.loads(Path(os.environ["F3_TEST_TARGET_METADATA"]).read_text())
    assert meta["owner"] == "F5 persistence and reader controlled tests"
    engines = {
        role: safe_test_engine(Path(os.environ["F3_TEST_DATABASE_CONFIG"]), role)
        for role in ("migration", "reader", "writer", "runtime")
    }
    try:
        with engines["migration"].connect() as connection:
            assert (
                connection.scalar(text("SELECT source_key FROM schools")) == "school:f3-pg-fixture"
            )
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) in {
                "20260914_34",
                "20260914_35",
                "20260914_36",
                "20260914_37",
                "20260914_38",
                "20260914_39",
                "20260914_40",
                "20260914_41",
            }
            guardian = (
                connection.execute(
                    text("""SELECT school_id,guardian_person_id AS adult,id AS relation,version
                FROM guardian_relationships WHERE relationship_kind='verified_guardian'
                AND status='confirmed' AND authority_version=1 AND revoked_at IS NULL
                AND valid_from<=now() AND (valid_to IS NULL OR now()<valid_to)
                ORDER BY valid_to NULLS FIRST LIMIT 1""")
                )
                .mappings()
                .one()
            )
        yield engines, dict(guardian)
    finally:
        for engine in engines.values():
            engine.dispose()


def read_family(engine, guardian, **selection):
    params = {
        **guardian,
        "at": None,
        "period": "current",
        "subject": None,
        "course": None,
        "contacts": False,
        **selection,
    }
    with engine.connect() as connection:
        return connection.scalar(
            text(
                "SELECT app.school_fact_family_teaching("
                ":school_id,:adult,:relation,:version,:at,:period,:subject,:course,:contacts)"
            ),
            params,
        )


def test_actual_reader_fixed_child_and_current_guardian(owned_family):
    engines, guardian = owned_family
    value = read_family(engines["reader"], guardian)
    assert value["status"] in {"available", "no_current_records"}
    assert isinstance(value["records"], list)
    for role in ("runtime", "writer"):
        with pytest.raises(DBAPIError):
            read_family(engines[role], guardian)
    with pytest.raises(DBAPIError):
        read_family(engines["reader"], guardian | {"adult": uuid4()})
    with pytest.raises(DBAPIError):
        read_family(engines["reader"], guardian | {"version": guardian["version"] + 1})


def test_actual_staff_coordinates_use_current_person_assignment(owned_family):
    engines, _ = owned_family
    with engines["migration"].connect() as connection:
        published = (
            connection.execute(
                text(
                    "SELECT a.school_id AS school,a.id AS assignment,v.person_id AS person "
                    "FROM staff_assignments a JOIN staff_assignment_versions v ON "
                    "v.assignment_id=a.id "
                    "WHERE a.source_key='assignment:f3-writer-safety-requester' AND "
                    "v.effective_to IS NULL"
                )
            )
            .mappings()
            .one()
        )
    params = {**dict(published), "at": datetime.now(UTC)}
    sql = text("SELECT app.school_fact_f5_staff_coordinates(:school,:person,:assignment,:at)")
    with engines["reader"].connect() as connection:
        value = connection.scalar(sql, params)
        assert set(value) == {"assignment_source_key", "person_source_key", "school_fact_version"}
    for role in ("runtime", "writer"):
        with engines[role].connect() as connection, pytest.raises(DBAPIError):
            connection.scalar(sql, params)
    with engines["reader"].connect() as connection, pytest.raises(DBAPIError):
        connection.scalar(sql, params | {"person": uuid4()})


def test_actual_term_without_child_calendar_is_not_school_wide(owned_family):
    engines, guardian = owned_family
    value = read_family(engines["reader"], guardian, period="current_term")
    assert value["status"] in {
        "available",
        "no_current_records",
        "period_not_recorded",
        "ambiguous",
    }
    if value["status"] in {"ambiguous", "period_not_recorded"}:
        assert value["records"] == []


@pytest.fixture
def teaching_scenario(owned_family):
    engines, guardian = owned_family
    prefix = "f5-parent-period:"
    with engines["migration"].connect() as connection:
        existing = connection.scalar(
            text("SELECT count(*) FROM school_education_facts WHERE source_key=:key"),
            {"key": prefix + "offering"},
        )
        if existing:
            return engines, guardian, prefix
        calendar = connection.scalar(
            text(
                "SELECT v.payload FROM school_education_facts f JOIN school_education_versions "
                "v ON v.fact_id=f.id WHERE f.kind='education_calendar_rule' AND "
                "v.superseded_at IS NULL AND v.valid_from<=now() AND v.valid_to IS NULL ORDER "
                "BY f.source_key LIMIT 1"
            )
        )
        other = (
            connection.execute(
                text(
                    "SELECT s.source_key AS student,r.source_key AS relation FROM "
                    "student_school_relationships r JOIN students s ON s.id=r.student_id WHERE "
                    "r.superseded_at IS NULL AND r.valid_from<=now() AND r.valid_to IS NULL "
                    "AND r.enrollment_status='enrolled' AND s.source_key<>'student:f3-a' LIMIT 1"
                )
            )
            .mappings()
            .one()
        )
    start = datetime.now(UTC) - timedelta(seconds=30)
    middle = start + timedelta(minutes=20)
    end = start + timedelta(hours=1)
    entries = []

    def add(kind, key, content, *, at=start):
        entries.append(
            {
                "fact_kind": kind,
                "source_key": prefix + key,
                "operation": "upsert",
                "expected_version": 0,
                "effective_at": at,
                "content": content,
            }
        )

    add(
        "education_calendar_rule",
        "calendar",
        dict(calendar) | {"display_name": "Own child formal calendar"},
    )
    add(
        "education_calendar_rule",
        "unrelated-calendar",
        dict(calendar) | {"display_name": "Unrelated calendar"},
    )
    add(
        "student_grade_placement",
        "placement",
        {
            "student_source_key": "student:f3-a",
            "calendar_source_key": prefix + "calendar",
            "grade_source_key": calendar["grade_source_keys"][0],
        },
    )
    add(
        "education_term",
        "term",
        {
            "display_name": "Own current term",
            "calendar_source_key": prefix + "calendar",
            "academic_year": 2026,
            "valid_to": end,
        },
    )
    add(
        "education_term",
        "unrelated-term",
        {
            "display_name": "Other current term",
            "calendar_source_key": prefix + "unrelated-calendar",
            "academic_year": 2026,
            "valid_to": end,
        },
    )
    add("teaching_course", "course", {"display_name": "Own independent course"})
    add(
        "teaching_group",
        "group",
        {"organization_source_key": "organization:f3-writer-safety", "group_mode": "shared"},
    )
    add(
        "teaching_group_member",
        "own-member",
        {
            "group_source_key": prefix + "group",
            "student_source_key": "student:f3-a",
            "school_relationship_source_key": "enrollment:f3-a",
        },
    )
    add(
        "teaching_group_member",
        "other-member",
        {
            "group_source_key": prefix + "group",
            "student_source_key": other["student"],
            "school_relationship_source_key": other["relation"],
        },
    )
    add(
        "teaching_offering",
        "offering",
        {
            "display_name": "Own changing teacher offering",
            "course_source_key": prefix + "course",
            "term_source_key": prefix + "term",
            "offering_status": "active",
            "valid_to": end,
        },
    )
    add(
        "offering_member_source",
        "source",
        {
            "offering_source_key": prefix + "offering",
            "source_kind": "independent_group",
            "group_source_key": prefix + "group",
            "valid_to": end,
        },
    )
    add(
        "teaching_assignment",
        "first-teacher",
        {
            "offering_source_key": prefix + "offering",
            "assignment_source_key": "assignment:f3-writer-safety-requester",
            "teaching_role": "lead",
            "valid_to": middle,
        },
    )
    add(
        "teaching_assignment",
        "next-teacher",
        {
            "offering_source_key": prefix + "offering",
            "assignment_source_key": "assignment:f3-writer-safety-verifier",
            "teaching_role": "lead",
            "valid_to": end,
        },
        at=middle,
    )
    actor, identity, context, service = existing_fixture_writer(engines)
    apply((engines, actor, identity, context, service), entries)
    return engines, guardian, prefix


def test_actual_child_term_ignores_other_calendar_and_contains_teacher_slices(teaching_scenario):
    engines, guardian, prefix = teaching_scenario
    value = read_family(
        engines["reader"], guardian, period="current_term", course="Own independent course"
    )
    assert value["status"] == "available"
    assert len(value["period_slices"]) >= 2
    assert all(
        part["enrollment_status"] in {"enrolled", "suspended"} for part in value["period_slices"]
    )
    snapshots = [
        {row["source_key"] for row in part["records"] if row["fact_kind"] == "teaching_assignment"}
        for part in value["period_slices"]
    ]
    assert {prefix + "first-teacher"} in snapshots
    assert {prefix + "next-teacher"} in snapshots
    assert value["next_boundary"] is not None


def test_actual_own_b_membership_is_correctable_but_other_member_hidden(teaching_scenario):
    engines, guardian, prefix = teaching_scenario
    value = read_family(engines["reader"], guardian, course="Own independent course")
    assert all(row["fact_kind"] != "teaching_group_member" for row in value["records"])
    with engines["writer"].connect() as connection:
        targets = (
            connection.execute(
                text(
                    "SELECT * FROM "
                    "app.school_fact_family_teaching_targets(:school_id,:adult,:relation,:version)"
                ),
                guardian,
            )
            .mappings()
            .all()
        )
    own_keys = {row["fact_source_key"] for row in targets}
    assert prefix + "own-member" in own_keys
    assert prefix + "other-member" not in own_keys


def test_actual_quality_is_offering_role_scoped_and_adds_review_boundary(teaching_scenario):
    engines, guardian, prefix = teaching_scenario
    now = datetime.now(UTC)
    due = now + timedelta(minutes=5)
    actor, identity, context, service = existing_fixture_writer(engines)
    key = "f5-parent-quality:" + uuid4().hex
    apply(
        (engines, actor, identity, context, service),
        [
            {
                "fact_kind": "fact_quality",
                "source_key": key,
                "operation": "upsert",
                "expected_version": 0,
                "effective_at": now,
                "content": {
                    "quality_kind": "negative",
                    "negative_code": "no_teaching_assignment",
                    "target_fact_kind": "teaching_assignment",
                    "owner_kind": "education",
                    "education_target_kind": "teaching_offering",
                    "education_target_source_key": prefix + "offering",
                    "teaching_role": "co_teacher",
                    "academic_year": 2026,
                    "responsibility": "Owned test school",
                    "basis_reference": "Owned explicit role record",
                    "verified_at": now,
                    "review_due_at": due,
                    "valid_to": now + timedelta(minutes=10),
                },
            }
        ],
    )
    value = read_family(engines["reader"], guardian, course="Own independent course")
    qualities = value["quality"]
    assert any(
        q["negative_code"] == "no_teaching_assignment"
        and q["teaching_role"] == "co_teacher"
        and q["offering_source_key"] == prefix + "offering"
        for q in qualities
    )
    assert not any(q["teaching_role"] == "lead" for q in qualities)
    assert value["next_boundary"] is not None
    assert datetime.fromisoformat(value["next_boundary"]) <= due
    excluded = read_family(engines["reader"], guardian, course="Another unrecorded course")
    assert excluded["quality"] == []


def test_actual_current_term_clips_to_authorized_history(teaching_scenario):
    engines, guardian, prefix = teaching_scenario
    with engines["migration"].connect() as connection:
        begins = connection.scalar(
            text("SELECT valid_from FROM guardian_relationships WHERE id=:relation"), guardian
        )
        rows = (
            connection.execute(
                text(
                    "SELECT f.kind,f.source_key,f.version,v.payload,v.valid_from,v.valid_to "
                    "FROM school_education_facts f JOIN school_education_versions v ON "
                    "v.fact_id=f.id WHERE f.source_key IN (:calendar,:term,:placement) AND "
                    "v.superseded_at IS NULL"
                ),
                {
                    "calendar": prefix + "calendar",
                    "term": prefix + "term",
                    "placement": prefix + "placement",
                },
            )
            .mappings()
            .all()
        )
    needs_extension = any(
        row["kind"] == "education_term" and row["valid_from"] >= begins for row in rows
    )
    if needs_extension:
        earlier = begins - timedelta(minutes=1)
        changes = [
            {
                "fact_kind": kind,
                "source_key": prefix + key,
                "operation": "upsert",
                "expected_version": 0,
                "effective_at": earlier,
                "content": content,
            }
            for kind, key, content in [
                (
                    "education_stage",
                    "historical-stage",
                    {"display_name": "Historical fixture stage"},
                ),
                (
                    "education_grade",
                    "historical-grade",
                    {
                        "display_name": "Historical fixture grade",
                        "stage_source_key": prefix + "historical-stage",
                        "grade_order": 1,
                    },
                ),
            ]
        ]
        for row in rows:
            content = dict(row["payload"])
            if row["kind"] == "education_calendar_rule":
                content.update(
                    stage_source_key=prefix + "historical-stage",
                    grade_source_keys=[prefix + "historical-grade"],
                )
            if row["kind"] == "student_grade_placement":
                content.update(grade_source_key=prefix + "historical-grade")
            if row["valid_to"]:
                content["valid_to"] = row["valid_to"]
            changes.append(
                {
                    "fact_kind": row["kind"],
                    "source_key": row["source_key"],
                    "operation": "correct",
                    "expected_version": row["version"],
                    "effective_at": row["valid_from"]
                    if row["kind"] == "student_grade_placement"
                    else earlier,
                    "correction_reason": "Owned earlier formal term to verify authorized clipping",
                    "content": content,
                }
            )
        actor, identity, context, service = existing_fixture_writer(engines)
        apply((engines, actor, identity, context, service), changes)
    value = read_family(
        engines["reader"], guardian, period="current_term", course="Own independent course"
    )
    assert value["status"] == "available"
    assert value["period_limited_to_authorized"] is True
    assert datetime.fromisoformat(value["period_start"]) == begins
    assert all(datetime.fromisoformat(part["as_of"]) >= begins for part in value["period_slices"])
    with pytest.raises(DBAPIError):
        read_family(engines["reader"], guardian, at=begins - timedelta(seconds=1))
