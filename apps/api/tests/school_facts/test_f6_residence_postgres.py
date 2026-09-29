"""F6 interval invariants, on the explicitly opted-in disposable fixture only."""

import json
import os
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest

from tests.school_facts.test_sf1_postgres_constraints import (
    _at,
    _insert_bed,
    _insert_person_and_student,
    _insert_residence,
    _insert_room,
    _insert_school,
    _insert_source,
    _insert_student_school_relationship,
)

pytestmark = pytest.mark.integration


@pytest.fixture
def current_owner_connection():
    config_path = os.environ.get("F6_TEST_DATABASE_CONFIG")
    meta_path = os.environ.get("F6_TEST_DATABASE_META")
    if not config_path or not meta_path:
        pytest.skip("Requires the explicitly owned F6 isolated fixture")
    config = json.loads(Path(config_path).read_text())
    meta = json.loads(Path(meta_path).read_text())
    for key, expected in {
        "owner": "F6 isolated persistence and reader tests",
        "container": "campus-f6-tests-20260922",
        "database": "f6_fixture_test",
        "system_identifier": "7688048026898145318",
    }.items():
        assert config[key] == meta[key] == expected
    connection = psycopg.connect(
        host=config["host"],
        port=config["port"],
        dbname=config["database"],
        user="campus_ai_migration",
        password=config["migration"],
        autocommit=False,
    )
    try:
        connection.execute("SET ROLE campus_ai_db_owner")
        actual = connection.execute(
            "SELECT current_database(), system_identifier::text FROM pg_control_system()"
        ).fetchone()
        assert actual == ("f6_fixture_test", "7688048026898145318")
        yield connection
    finally:
        connection.rollback()
        connection.close()


@pytest.fixture
def current_owner_session(current_owner_connection):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from sqlalchemy.pool import StaticPool

    engine = create_engine(
        "postgresql+psycopg://", creator=lambda: current_owner_connection, poolclass=StaticPool
    )
    with engine.connect() as connection:
        connection.exec_driver_sql("SET ROLE campus_ai_db_owner")
        with Session(bind=connection) as session:
            yield session


def seed(conn):
    school, source, person, student, room, bed = (uuid4() for _ in range(6))
    _insert_school(conn, school_id=school, source_key=f"f6:{school}")
    _insert_source(conn, school_id=school, source_id=source, source_key=f"f6:{source}")
    _insert_person_and_student(
        conn, school_id=school, person_id=person, student_id=student, key=f"f6:{student}"
    )
    _insert_room(
        conn, school_id=school, source_id=source, room_id=room, key=f"f6:{room}", capacity=1
    )
    _insert_bed(conn, school_id=school, source_id=source, room_id=room, bed_id=bed, key=f"f6:{bed}")
    return school, source, student, room, bed


def relationship(conn, state, start, end, key):
    school, source, student, _, _ = state
    _insert_student_school_relationship(
        conn,
        school_id=school,
        source_id=source,
        student_id=student,
        key=key,
        valid_from=start,
        valid_to=end,
    )


def test_continuous_boarding_slices_jointly_cover_one_residence(current_owner_connection):
    c = current_owner_connection
    state = seed(c)
    relationship(c, state, _at(1), _at(2), "first")
    relationship(c, state, _at(2), None, "second")
    _insert_residence(
        c,
        school_id=state[0],
        source_id=state[1],
        student_id=state[2],
        bed_id=state[4],
        valid_from=_at(1),
    )
    c.execute("SET CONSTRAINTS ALL IMMEDIATE")


def test_one_microsecond_enrollment_gap_still_rejects_residence(current_owner_connection):
    c = current_owner_connection
    state = seed(c)
    relationship(c, state, _at(1), _at(2), "first")
    relationship(c, state, _at(2) + timedelta(microseconds=1), None, "second")
    with pytest.raises(psycopg.errors.CheckViolation), c.transaction():
        _insert_residence(
            c,
            school_id=state[0],
            source_id=state[1],
            student_id=state[2],
            bed_id=state[4],
            valid_from=_at(1),
        )
        c.execute("SET CONSTRAINTS ALL IMMEDIATE")


def test_sequential_available_beds_do_not_exceed_one_capacity(current_owner_connection):
    c = current_owner_connection
    school, source, _, room, bed = seed(c)
    c.execute("UPDATE dorm_bed_versions SET valid_to=%s WHERE bed_id=%s", (_at(2), bed))
    c.execute(
        "INSERT INTO dorm_beds(id,school_id,room_location_id,source_key) VALUES(%s,%s,%s,%s)",
        (new_bed := uuid4(), school, room, f"f6:{uuid4()}"),
    )
    c.execute(
        "INSERT INTO "
        "dorm_bed_versions(id,school_id,bed_id,business_code,display_name,availability,valid_from,source_id)"
        " VALUES(%s,%s,%s,'new','new','available',%s,%s)",
        (uuid4(), school, new_bed, _at(2), source),
    )
    c.execute(
        "UPDATE dorm_room_profile_versions SET accommodation_zone='reviewed' WHERE "
        "room_location_id=%s",
        (room,),
    )
    c.execute("SET CONSTRAINTS ALL IMMEDIATE")


def test_unknown_attribute_does_not_establish_residence_eligibility(current_owner_connection):
    c = current_owner_connection
    state = seed(c)
    relationship(c, state, _at(1), None, "unknown")
    c.execute(
        "UPDATE student_school_relationships SET residence_mode=NULL WHERE student_id=%s",
        (state[2],),
    )
    c.execute("SET CONSTRAINTS ALL IMMEDIATE")
    with pytest.raises(psycopg.errors.CheckViolation), c.transaction():
        _insert_residence(
            c,
            school_id=state[0],
            source_id=state[1],
            student_id=state[2],
            bed_id=state[4],
            valid_from=_at(1),
        )
        c.execute("SET CONSTRAINTS ALL IMMEDIATE")


def test_suspension_preserves_existing_formal_boarding_without_new_rows(current_owner_connection):
    c = current_owner_connection
    state = seed(c)
    relationship(c, state, _at(1), None, "suspended")
    _insert_residence(
        c,
        school_id=state[0],
        source_id=state[1],
        student_id=state[2],
        bed_id=state[4],
        valid_from=_at(1),
    )
    c.execute("SET CONSTRAINTS ALL IMMEDIATE")
    c.execute(
        "UPDATE student_school_relationships SET enrollment_status='suspended' WHERE student_id=%s",
        (state[2],),
    )
    assert (
        c.execute(
            "SELECT count(*) FROM student_bed_residences WHERE student_id=%s", (state[2],)
        ).fetchone()[0]
        == 1
    )


def test_continuous_room_and_bed_versions_cover_the_full_arrangement(current_owner_connection):
    c = current_owner_connection
    school, source, student, room, bed = state = seed(c)
    relationship(c, state, _at(1), None, "continuous")
    c.execute(
        "UPDATE dorm_room_profile_versions SET valid_to=%s WHERE room_location_id=%s",
        (_at(2), room),
    )
    c.execute(
        "INSERT INTO "
        "dorm_room_profile_versions(id,school_id,room_location_id,authorized_bed_capacity,accommodation_zone,is_available,accessibility_tags,valid_from,source_id)"
        " VALUES(%s,%s,%s,1,'formal',true,'{}',%s,%s)",
        (uuid4(), school, room, _at(2), source),
    )
    c.execute("UPDATE dorm_bed_versions SET valid_to=%s WHERE bed_id=%s", (_at(2), bed))
    c.execute(
        "INSERT INTO "
        "dorm_bed_versions(id,school_id,bed_id,business_code,display_name,availability,valid_from,source_id)"
        " VALUES(%s,%s,%s,'next','next','available',%s,%s)",
        (uuid4(), school, bed, _at(2), source),
    )
    _insert_residence(
        c, school_id=school, source_id=source, student_id=student, bed_id=bed, valid_from=_at(1)
    )
    c.execute("SET CONSTRAINTS ALL IMMEDIATE")


@pytest.mark.parametrize(
    "table,identity",
    [("dorm_bed_versions", "bed_id"), ("dorm_room_profile_versions", "room_location_id")],
)
def test_superseded_facility_rows_cannot_supply_missing_coverage(
    current_owner_connection, table, identity
):
    c = current_owner_connection
    school, source, student, room, bed = state = seed(c)
    relationship(c, state, _at(1), None, "boarding")
    _insert_residence(
        c, school_id=school, source_id=source, student_id=student, bed_id=bed, valid_from=_at(1)
    )
    c.execute("SET CONSTRAINTS ALL IMMEDIATE")
    with pytest.raises(psycopg.errors.CheckViolation), c.transaction():
        c.execute(
            f"UPDATE {table} SET superseded_at=now() WHERE {identity}=%s",
            (bed if identity == "bed_id" else room,),
        )


@pytest.mark.parametrize("competing", ["student", "bed"])
def test_future_residences_keep_both_exclusive_constraints(current_owner_connection, competing):
    c = current_owner_connection
    school, source, student, room, bed = state = seed(c)
    relationship(c, state, _at(1), None, "boarding")
    _insert_residence(
        c, school_id=school, source_id=source, student_id=student, bed_id=bed, valid_from=_at(10)
    )
    if competing == "student":
        c.execute(
            "UPDATE dorm_room_profile_versions SET authorized_bed_capacity=2 WHERE "
            "room_location_id=%s",
            (room,),
        )
        other_bed = uuid4()
        _insert_bed(
            c,
            school_id=school,
            source_id=source,
            room_id=room,
            bed_id=other_bed,
            key=f"other:{other_bed}",
        )
        other_student = student
    else:
        other_student = uuid4()
        _insert_person_and_student(
            c,
            school_id=school,
            person_id=uuid4(),
            student_id=other_student,
            key=f"other:{other_student}",
        )
        _insert_student_school_relationship(
            c,
            school_id=school,
            source_id=source,
            student_id=other_student,
            key="other",
            valid_from=_at(1),
            valid_to=None,
        )
        other_bed = bed
    with pytest.raises(psycopg.errors.ExclusionViolation), c.transaction():
        _insert_residence(
            c,
            school_id=school,
            source_id=source,
            student_id=other_student,
            bed_id=other_bed,
            valid_from=_at(10, 2),
        )
        c.execute("SET CONSTRAINTS ALL IMMEDIATE")


def test_unknown_change_cannot_invalidate_an_existing_arrangement(current_owner_connection):
    c = current_owner_connection
    school, source, student, _, bed = state = seed(c)
    relationship(c, state, _at(1), None, "boarding")
    _insert_residence(
        c, school_id=school, source_id=source, student_id=student, bed_id=bed, valid_from=_at(1)
    )
    c.execute("SET CONSTRAINTS ALL IMMEDIATE")
    with pytest.raises(psycopg.errors.CheckViolation), c.transaction():
        c.execute(
            "UPDATE student_school_relationships SET residence_mode=NULL WHERE student_id=%s",
            (student,),
        )


def test_profile_reuses_the_building_and_preserves_legacy_rooms(current_owner_connection):
    c = current_owner_connection
    school, source, _, room, _ = seed(c)
    assert (
        c.execute(
            "SELECT count(*) FROM dormitory_building_profile_versions WHERE school_id=%s", (school,)
        ).fetchone()[0]
        == 0
    )
    building = c.execute(
        "SELECT parent_location_id FROM campus_locations WHERE id=%s", (room,)
    ).fetchone()[0]
    c.execute(
        "INSERT INTO "
        "dormitory_building_profile_versions(id,school_id,source_key,location_id,usage_kind,display_name,is_available,valid_from,source_id)"
        " VALUES(%s,%s,'dormitory:one',%s,'building','正式用途',true,%s,%s)",
        (uuid4(), school, building, _at(1), source),
    )
    c.execute("SET CONSTRAINTS ALL IMMEDIATE")
    with pytest.raises(psycopg.errors.CheckViolation), c.transaction():
        c.execute(
            "INSERT INTO "
            "dormitory_building_profile_versions(id,school_id,source_key,location_id,usage_kind,display_name,is_available,valid_from,source_id)"
            " VALUES(%s,%s,'dormitory:bad',%s,'building','错误楼栋用途',true,%s,%s)",
            (uuid4(), school, room, _at(1), source),
        )


def test_new_profile_has_no_runtime_table_access(current_owner_connection):
    c = current_owner_connection
    assert c.execute(
        "SELECT relrowsecurity AND relforcerowsecurity FROM pg_class WHERE "
        "oid='public.dormitory_building_profile_versions'::regclass"
    ).fetchone()[0]
    for role in ("campus_ai_runtime", "campus_ai_school_fact_reader"):
        assert not c.execute(
            "SELECT "
            "has_table_privilege(%s,'public.dormitory_building_profile_versions','SELECT,INSERT,UPDATE,DELETE')",
            (role,),
        ).fetchone()[0]


def test_read_only_preflight_detects_capacity_and_accepts_explicit_bed_resolution(
    current_owner_connection, current_owner_session
):
    from app.composition.school_fact_persistence import SqlAlchemySchoolFactPersistence
    from app.platform.school_facts.contracts import CanonicalFactChangeV4

    c = current_owner_connection
    school, _, _, room, bed = seed(c)
    room_key = c.execute("SELECT source_key FROM campus_locations WHERE id=%s", (room,)).fetchone()[
        0
    ]
    bed_key = c.execute("SELECT source_key FROM dorm_beds WHERE id=%s", (bed,)).fetchone()[0]
    c.execute(
        "UPDATE dorm_room_profile_versions SET source_key='room:formal' WHERE room_location_id=%s",
        (room,),
    )

    def change(kind, key, content):
        return CanonicalFactChangeV4.model_validate(
            dict(
                fact_kind=kind,
                operation="correct",
                source_key=key,
                expected_version=1,
                effective_at=_at(2),
                content=content,
                correction_reason="正式核对",
                origin=dict(sheet_code="f6", row_number=1),
            )
        )

    room_change = change(
        "dorm_room",
        "room:formal",
        dict(
            room_source_key=room_key,
            authorized_bed_capacity=0,
            accommodation_zone="formal",
            is_available=True,
        ),
    )
    before = c.execute(
        "SELECT count(*),sum(authorized_bed_capacity) FROM dorm_room_profile_versions WHERE"
        " school_id=%s",
        (school,),
    ).fetchone()
    check = SqlAlchemySchoolFactPersistence._residence_candidate_codes
    assert check(current_owner_session, school, (room_change,)) == (
        "residence.capacity_requires_same_batch_resolution",
    )
    bed_change = change(
        "dorm_bed",
        bed_key,
        dict(
            room_source_key=room_key,
            business_code="B",
            display_name="床",
            availability="unavailable",
        ),
    )
    assert check(current_owner_session, school, (room_change, bed_change)) == ()
    assert (
        c.execute(
            "SELECT count(*),sum(authorized_bed_capacity) FROM dorm_room_profile_versions "
            "WHERE school_id=%s",
            (school,),
        ).fetchone()
        == before
    )


def test_read_only_preflight_rejects_future_occupied_bed(
    current_owner_connection, current_owner_session
):
    from app.composition.school_fact_persistence import SqlAlchemySchoolFactPersistence
    from app.platform.school_facts.contracts import CanonicalFactChangeV4

    c = current_owner_connection
    school, source, student, _, bed = state = seed(c)
    relationship(c, state, _at(1), None, "boarding")
    _insert_residence(
        c, school_id=school, source_id=source, student_id=student, bed_id=bed, valid_from=_at(10)
    )
    student_key = c.execute("SELECT source_key FROM students WHERE id=%s", (student,)).fetchone()[0]
    bed_key = c.execute("SELECT source_key FROM dorm_beds WHERE id=%s", (bed,)).fetchone()[0]
    change = CanonicalFactChangeV4.model_validate(
        dict(
            fact_kind="student_bed_residence",
            operation="upsert",
            source_key="stay:second",
            expected_version=0,
            effective_at=_at(10, 2),
            content=dict(student_source_key=student_key, bed_source_key=bed_key),
            origin=dict(sheet_code="f6", row_number=1),
        )
    )
    issues = SqlAlchemySchoolFactPersistence._residence_candidate_codes(
        current_owner_session, school, (change,)
    )
    assert set(issues) == {
        "residence.student_overlap_requires_same_batch_resolution",
        "residence.bed_overlap_requires_same_batch_resolution",
    }


def test_real_writer_correction_retains_old_timeline_and_full_new_revision(
    current_owner_connection, current_owner_session, monkeypatch
):
    from sqlalchemy.orm import Session

    from app.composition.school_fact_persistence import SqlAlchemySchoolFactPersistence
    from app.platform.school_facts.contracts import CanonicalFactChangeV4

    c = current_owner_connection
    school, source, _, room, _ = seed(c)
    old = c.execute(
        "SELECT id,valid_from FROM dorm_room_profile_versions WHERE room_location_id=%s", (room,)
    ).fetchone()
    batch = c.execute(
        "SELECT id FROM master_data_import_batches WHERE school_id=%s LIMIT 1", (school,)
    ).fetchone()[0]
    monkeypatch.setattr(SqlAlchemySchoolFactPersistence, "_source_root_id", lambda *a, **k: room)
    with Session(bind=current_owner_session.bind) as session:
        change = CanonicalFactChangeV4.model_validate(
            dict(
                fact_kind="dorm_room",
                operation="correct",
                source_key="room:formal",
                expected_version=1,
                effective_at=_at(2),
                correction_reason="容量核对",
                origin={"sheet_code": "f6", "row_number": 1},
                content=dict(
                    room_source_key="room:one",
                    authorized_bed_capacity=2,
                    accommodation_zone="boarding",
                    is_available=True,
                    valid_to=_at(3),
                ),
            )
        )
        _, version = SqlAlchemySchoolFactPersistence._apply_residence_facility(
            session=session,
            school_id=school,
            batch_id=batch,
            source_id=source,
            change=change,
            current_id=old[0],
            current_version=1,
        )
        c.execute("SET CONSTRAINTS ALL IMMEDIATE")
        assert version == 2
        rows = c.execute(
            "SELECT version,valid_from,valid_to,superseded_at IS NOT NULL FROM "
            "dorm_room_profile_versions WHERE room_location_id=%s ORDER BY "
            "version,valid_from",
            (room,),
        ).fetchall()
        assert len(rows) == 4
        assert rows[0] == (1, old[1], None, True)
        assert [row[0] for row in rows[1:]] == [2, 2, 2]
        assert all(not row[3] for row in rows[1:])
