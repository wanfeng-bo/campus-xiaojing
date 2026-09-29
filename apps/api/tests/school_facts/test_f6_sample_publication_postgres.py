"""Owned schema44/45 Writer-kernel checks; fixtures do not prove user authority."""

import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from threading import Event
from time import monotonic, sleep
from types import SimpleNamespace
from uuid import uuid4

import psycopg
import pytest
from sqlalchemy import select, text

from app.composition.school_fact_persistence import SqlAlchemySchoolFactPersistence as Writer
from app.platform.master_data_imports.models import MasterDataImportBatch, MasterDataImportUnit
from app.platform.school_facts.contracts import CanonicalFactChangeSetV1, canonical_plan_sha256
from tests.school_facts.test_f6_residence_postgres import (
    current_owner_connection as current_owner_connection,
)
from tests.school_facts.test_f6_residence_postgres import (
    current_owner_session as current_owner_session,
)
from tests.school_facts.test_f6_residence_postgres import (
    seed,
)
from tests.school_facts.test_sf1_postgres_constraints import (
    _at,
    _insert_bed,
    _insert_confirmed_location_slices,
    _insert_person_and_student,
    _insert_residence,
    _insert_room,
    _insert_school,
    _insert_student_school_relationship,
    _LocationSliceSeed,
)

pytestmark = pytest.mark.integration


def entry(kind, key, content, *, at=None, version=0, schema=4):
    return dict(
        schema_version=schema,
        fact_kind=kind,
        source_key=key,
        operation="upsert",
        expected_version=version,
        effective_at=at or _at(1),
        content=content,
        origin=dict(sheet_code="f6_kernel_fixture", row_number=1),
    )


class KernelFixture:
    """Seed transcript under DB owner and invoke the unchanged shared apply kernel."""

    def __init__(self, connection, session):
        self.connection, self.session = connection, session
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() in {
            ("20260922_44",),
            ("20260922_45",),
        }
        self.school = uuid4()
        _insert_school(connection, school_id=self.school, source_key=f"f6:kernel:{self.school}")
        slices = []
        for side in ("a", "b", "f1"):
            location = uuid4()
            key = "location:f1-main-campus" if side == "f1" else f"f4:location:{side}"
            connection.execute(
                "INSERT INTO campus_locations(id,school_id,source_key,kind,status) "
                "VALUES(%s,%s,%s,'campus','active')",
                (location, self.school, key),
            )
            slices.append(
                _LocationSliceSeed(
                    location, key, None, "campus", "F6-" + side, side, (), "/" + side, _at(1)
                )
            )
        _insert_confirmed_location_slices(connection, school_id=self.school, slices=tuple(slices))
        self.template = session.execute(
            select(MasterDataImportBatch).where(MasterDataImportBatch.school_id == self.school)
        ).scalar_one()
        self.writer = object.__new__(Writer)

    def apply(self, entries, *, schema=4, check_preflight=True):
        session, writer = self.session, self.writer
        plan = CanonicalFactChangeSetV1.model_validate_json(
            json.dumps(
                dict(
                    schema_version=schema,
                    base_version=writer._current_global_fact_version(session, self.school),
                    source_sha256=sha256(uuid4().bytes).hexdigest(),
                    changes=[
                        {
                            **row,
                            "schema_version": schema,
                            "origin": dict(sheet_code="f6_kernel_fixture", row_number=i),
                        }
                        for i, row in enumerate(entries, 1)
                    ],
                ),
                default=str,
            )
        )
        if check_preflight:
            assert writer._residence_candidate_codes(session, self.school, plan.changes) == ()
        now, key, template = datetime.now(UTC), uuid4().hex, self.template
        batch = MasterDataImportBatch(
            id=uuid4(),
            school_id=self.school,
            mode="canonical_atomic_changes",
            source_kind="manual",
            source_ref="f6-kernel:" + key,
            source_sha256=plan.source_sha256,
            worksheet_names=[],
            status="ready_for_confirmation",
            planned_units=len(plan.changes),
            succeeded_units=0,
            failed_units=0,
            pending_units=len(plan.changes),
            request_id=uuid4(),
            idempotency_key_hash=sha256(key.encode()).hexdigest(),
            plan_sha256=canonical_plan_sha256(plan),
            base_fact_version=plan.base_version,
            target_fact_version=plan.base_version + 1,
            confirmation_token_sha256=sha256(uuid4().bytes).hexdigest(),
            confirmation_expires_at=now + timedelta(minutes=10),
            confirmation_session_sha256="a" * 64,
            staged_by_user_id=template.staged_by_user_id,
            staged_by_person_id=template.staged_by_person_id,
            staged_by_assignment_id=template.staged_by_assignment_id,
            staged_assignment_version=1,
            staged_authorization_version=1,
        )
        session.add(batch)
        session.flush()
        for i, change in enumerate(plan.changes, 1):
            private = change.model_dump(mode="json", exclude_none=True)
            session.add(
                MasterDataImportUnit(
                    school_id=self.school,
                    batch_id=batch.id,
                    sequence=i,
                    row_number=i,
                    unit_kind="school_fact",
                    status="ready",
                    payload={
                        name: private[name]
                        for name in (
                            "schema_version",
                            "fact_kind",
                            "source_key",
                            "operation",
                            "expected_version",
                            "effective_at",
                            "origin",
                        )
                    },
                    payload_sha256=sha256(
                        json.dumps(
                            private, sort_keys=True, ensure_ascii=False, separators=(",", ":")
                        ).encode()
                    ).hexdigest(),
                    soft_anomalies=[],
                    initial_soft_anomalies=[],
                )
            )
        session.flush()
        units = writer.apply_canonical_units_in_transaction(
            session, batch=batch, change_set=plan, school_id=self.school
        )
        batch.status, batch.succeeded_units, batch.pending_units = "applied", len(units), 0
        batch.confirmed_by_user_id = template.confirmed_by_user_id
        batch.confirmed_by_person_id = template.confirmed_by_person_id
        batch.confirmed_by_assignment_id = template.confirmed_by_assignment_id
        batch.confirmed_at = batch.confirmation_consumed_at = now
        session.flush()
        writer._append_terminal_outcome(
            session=session,
            batch=batch,
            actor=SimpleNamespace(
                account_user_id=template.confirmed_by_user_id,
                person_id=template.confirmed_by_person_id,
                assignment_id=template.confirmed_by_assignment_id,
                assignment_version=1,
            ),
            context=SimpleNamespace(idempotency_key=key),
            sequences=list(range(1, len(units) + 1)),
        )
        session.flush()
        session.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
        session.execute(text("SET CONSTRAINTS ALL DEFERRED"))
        return plan

    def seed_students(self):
        rows = []
        for stage, side, mode, at, number in (
            (3, "a", "boarding", datetime(2026, 9, 13, 6, 20, 2, 232433, tzinfo=UTC), "F3-A001"),
            (3, "b", "day", datetime(2026, 9, 13, 6, 20, 2, 232433, tzinfo=UTC), "F3-B001"),
            (3, "c", "day", datetime(2026, 9, 13, 6, 20, 2, 232433, tzinfo=UTC), "F3-C001"),
            (4, "d", "day", datetime(2026, 9, 13, 13, tzinfo=UTC), "F4-D001"),
        ):
            person, student = f"f{stage}:person:student-{side}", f"f{stage}:student:{side}"
            rows.extend(
                (
                    entry(
                        "person",
                        person,
                        dict(
                            display_name="合成学生" + side,
                            person_kind="other",
                            lifecycle_status="active",
                        ),
                    ),
                    entry("student", student, dict(person_source_key=person, status="active")),
                    entry(
                        "student_school_relationship",
                        "f3:student-school:c"
                        if side == "c"
                        else f"f{stage}:student_school_relationship:{side}",
                        dict(
                            student_source_key=student,
                            student_number=number,
                            enrollment_status="enrolled",
                            residence_mode=mode,
                            primary_campus_source_key="location:f1-main-campus"
                            if side == "c"
                            else "f4:location:a",
                            administrative_campus_source_key="location:f1-main-campus"
                            if side == "c"
                            else "f4:location:a",
                        ),
                        at=at,
                    ),
                )
            )
        self.apply(rows)
        self.apply(
            [
                entry(
                    "student_school_relationship",
                    "f3:student-school:c",
                    dict(
                        student_source_key="f3:student:c",
                        student_number="F3-C001",
                        enrollment_status="suspended",
                        residence_mode="day",
                        primary_campus_source_key="location:f1-main-campus",
                        administrative_campus_source_key="location:f1-main-campus",
                    ),
                    at=datetime(2026, 9, 14, 5, 16, 1, 949614, tzinfo=UTC),
                    version=1,
                )
            ]
        )


def test_reviewed_26_samples_apply_through_kernel_and_keep_original_day_history(
    current_owner_connection,
    current_owner_session,
):
    from scripts.f6_engineering.prepare_samples import CURRENT, build_plan

    fixture = KernelFixture(current_owner_connection, current_owner_session)
    fixture.seed_students()
    samples = build_plan()
    assert [len(batch["changes"]) for batch in samples["batches"].values()] == [5, 11, 6, 4]
    for batch in samples["batches"].values():
        fixture.apply(batch["changes"])
    day, boarding = current_owner_connection.execute(
        "SELECT student_number,residence_mode,valid_from,valid_to,version FROM "
        "student_school_relationships WHERE school_id=%s AND "
        "source_key='f4:student_school_relationship:d' "
        "AND superseded_at IS NULL ORDER BY valid_from",
        (fixture.school,),
    ).fetchall()
    current = datetime.fromisoformat(CURRENT)
    assert day == ("F4-D001", "day", datetime(2026, 9, 13, 13, tzinfo=UTC), current, 2)
    assert boarding == ("F4-D001", "boarding", current, None, 2)
    c_history = current_owner_connection.execute(
        "SELECT student_number,enrollment_status,residence_mode,valid_from,valid_to,version "
        "FROM student_school_relationships WHERE school_id=%s "
        "AND source_key='f3:student-school:c' AND superseded_at IS NULL ORDER BY valid_from",
        (fixture.school,),
    ).fetchall()
    c_cut = datetime(2026, 9, 14, 5, 16, 1, 949614, tzinfo=UTC)
    assert c_history == [
        (
            "F3-C001",
            "enrolled",
            "day",
            datetime(2026, 9, 13, 6, 20, 2, 232433, tzinfo=UTC),
            c_cut,
            3,
        ),
        ("F3-C001", "suspended", "day", c_cut, current, 3),
        ("F3-C001", "suspended", None, current, None, 3),
    ]
    assert (
        current_owner_connection.execute(
            "SELECT count(*) FROM student_bed_residences WHERE school_id=%s", (fixture.school,)
        ).fetchone()[0]
        == 4
    )
    assert (
        current_owner_connection.execute(
            "SELECT count(*) FROM school_fact_qualities WHERE school_id=%s", (fixture.school,)
        ).fetchone()[0]
        == 4
    )


def test_future_cancellation_keeps_gap_until_explicit_new_writer_registration(
    current_owner_connection, current_owner_session
):
    """R08: cancellation never extends history; only a new formal write restores a stay."""
    from scripts.f6_engineering.prepare_samples import CURRENT, FUTURE, build_plan

    c, session = current_owner_connection, current_owner_session
    fixture = KernelFixture(c, session)
    fixture.seed_students()
    for batch in build_plan()["batches"].values():
        fixture.apply(batch["changes"])
    cutoff = datetime.fromisoformat(FUTURE)
    resume_at = cutoff + timedelta(days=2)

    def earlier_rows():
        return c.execute(
            "SELECT r.* FROM student_bed_residences r JOIN students s "
            "ON s.school_id=r.school_id AND s.id=r.student_id "
            "WHERE r.school_id=%s AND s.source_key='f3:student:a' AND r.valid_from<%s "
            "ORDER BY r.valid_from,r.id",
            (fixture.school, cutoff),
        ).fetchall()

    def at(instant):
        return c.execute(
            "SELECT b.source_key FROM student_bed_residences r JOIN students s "
            "ON s.school_id=r.school_id AND s.id=r.student_id JOIN dorm_beds b "
            "ON b.school_id=r.school_id AND b.id=r.bed_id "
            "WHERE r.school_id=%s AND s.source_key='f3:student:a' AND r.revoked_at IS NULL "
            "AND r.valid_from<=%s AND (r.valid_to IS NULL OR %s<r.valid_to)",
            (fixture.school, instant, instant),
        ).fetchall()

    earlier = earlier_rows()
    assert len(earlier) == 2 and at(cutoff) == [("f6:bed:a-301-2",)]
    fixture.apply(
        [
            entry(
                "student_bed_residence",
                "f6:residence:a-future",
                None,
                at=datetime.fromisoformat(CURRENT),
                version=1,
            )
            | {"operation": "revoke"}
        ]
    )
    assert earlier_rows() == earlier
    assert at(cutoff - timedelta(microseconds=1)) == [("f6:bed:a-302-1",)]
    assert at(cutoff) == at(resume_at) == []
    before_reads = fixture.writer._current_global_fact_version(session, fixture.school)
    for _ in range(2):
        assert at(cutoff) == at(resume_at) == []
    assert fixture.writer._current_global_fact_version(session, fixture.school) == before_reads

    fixture.apply(
        [
            entry(
                "student_bed_residence",
                "f6:residence:explicit-restoration",
                dict(student_source_key="f3:student:a", bed_source_key="f6:bed:a-302-1"),
                at=resume_at,
            )
        ]
    )
    assert earlier_rows() == earlier
    assert at(cutoff) == at(resume_at - timedelta(microseconds=1)) == []
    assert at(resume_at) == [("f6:bed:a-302-1",)]
    assert fixture.writer._current_global_fact_version(session, fixture.school) == before_reads + 1
    assert c.execute(
        "SELECT r.revoked_at IS NOT NULL,l.target_version FROM student_bed_residences r "
        "JOIN school_fact_change_lineage l ON l.school_id=r.school_id AND l.target_id=r.id "
        "WHERE l.school_id=%s AND l.fact_kind='student_bed_residence' "
        "AND l.fact_source_key='f6:residence:a-future' ORDER BY l.target_version DESC LIMIT 1",
        (fixture.school,),
    ).fetchone() == (True, 2)


def seeded_facilities(connection, session):
    from scripts.f6_engineering.prepare_samples import build_plan

    fixture = KernelFixture(connection, session)
    fixture.seed_students()
    for name in ("foundation", "facilities"):
        fixture.apply(build_plan()["batches"][name]["changes"])
    return fixture


def test_failing_member_rolls_back_every_unit_batch_and_lineage(
    current_owner_connection,
    current_owner_session,
):
    from sqlalchemy.exc import IntegrityError

    fixture = seeded_facilities(current_owner_connection, current_owner_session)
    school, session = fixture.school, fixture.session

    def counts():
        return tuple(
            session.scalar(text(f"SELECT count(*) FROM {table} WHERE school_id=:s"), dict(s=school))
            for table in (
                "student_bed_residences",
                "school_fact_change_lineage",
                "master_data_import_batches",
            )
        )

    before = counts()
    with pytest.raises(IntegrityError) as error, session.begin_nested():
        fixture.apply(
            [
                entry(
                    "student_bed_residence",
                    "f6:atomic:valid",
                    dict(student_source_key="f3:student:a", bed_source_key="f6:bed:a-301-1"),
                    at=_at(10),
                ),
                entry(
                    "student_bed_residence",
                    "f6:atomic:invalid",
                    dict(student_source_key="f4:student:d", bed_source_key="f6:bed:a-301-2"),
                    at=_at(10),
                ),
            ],
            check_preflight=False,
        )
    assert error.value.orig.sqlstate == "23514"
    assert counts() == before


@pytest.mark.parametrize("violation", ["capacity", "residence_identity", "location_terminal"])
def test_legacy_v3_cannot_bypass_new_facility_or_residence_guards(
    current_owner_connection,
    current_owner_session,
    violation,
):
    from sqlalchemy.exc import IntegrityError

    from app.platform.school_facts.application import SchoolFactChangeRejected

    fixture = seeded_facilities(current_owner_connection, current_owner_session)
    if violation == "capacity":
        changes = [
            entry(
                "dorm_room",
                "f6:room:a-301",
                dict(
                    room_source_key="f6:location:a-301",
                    authorized_bed_capacity=1,
                    accommodation_zone="general",
                    is_available=True,
                ),
                at=_at(10),
                version=1,
            )
        ]
        expected = IntegrityError
    elif violation == "residence_identity":
        original = entry(
            "student_bed_residence",
            "f6:v3:immutable",
            dict(student_source_key="f3:student:a", bed_source_key="f6:bed:a-301-1"),
            at=_at(10),
        )
        fixture.apply([original])
        changes = [
            {
                **original,
                "expected_version": 1,
                "content": {**original["content"], "bed_source_key": "f6:bed:a-301-2"},
            }
        ]
        expected = SchoolFactChangeRejected
    else:
        fixture.apply(
            [
                entry(
                    "student_bed_residence",
                    "f6:v3:location-dependent",
                    dict(student_source_key="f3:student:a", bed_source_key="f6:bed:a-301-1"),
                    at=_at(10),
                )
            ]
        )
        changes = [
            {
                **entry("location", "f6:location:a-301", None, at=_at(10), version=1),
                "operation": "end",
            }
        ]
        expected = (IntegrityError, SchoolFactChangeRejected)
    with pytest.raises(expected), fixture.session.begin_nested():
        fixture.apply(changes, schema=3, check_preflight=False)
    assert (
        current_owner_connection.execute(
            "SELECT count(*) FROM dorm_room_profile_versions WHERE school_id=%s "
            "AND source_key='f6:room:a-301' "
            "AND superseded_at IS NULL AND authorized_bed_capacity=2",
            (fixture.school,),
        ).fetchone()[0]
        == 1
    )


@pytest.fixture
def owned_concurrency_database():
    """Exact authorized disposable sibling; the original fixture is never committed."""
    config_path, meta_path = (
        os.environ.get(name)
        for name in (
            "F6_TEST_DATABASE_CONFIG",
            "F6_TEST_DATABASE_META",
        )
    )
    if not config_path or not meta_path:
        pytest.skip("Requires the explicitly owned F6 isolated fixture")
    config = json.loads(Path(config_path).read_text())
    metadata = json.loads(Path(meta_path).read_text())
    for key, expected in {
        "owner": "F6 isolated persistence and reader tests",
        "container": "campus-f6-tests-20260922",
        "database": "f6_fixture_test",
        "system_identifier": "7688048026898145318",
    }.items():
        assert config[key] == metadata[key] == expected
    sibling = "f6_fixture_concurrency_test"
    with psycopg.connect(
        host=config["host"],
        port=config["port"],
        dbname="postgres",
        user="postgres",
        password=config["admin"],
        autocommit=True,
    ) as admin:
        assert admin.execute(
            "SELECT system_identifier::text FROM pg_control_system()"
        ).fetchone() == ("7688048026898145318",)
        assert admin.execute(
            "SELECT count(*) FROM pg_database WHERE datname=%s", (sibling,)
        ).fetchone() == (0,)
        admin.execute(
            "CREATE DATABASE f6_fixture_concurrency_test "
            "OWNER campus_ai_db_owner TEMPLATE f6_fixture_test"
        )

        def connect():
            connection = psycopg.connect(
                host=config["host"],
                port=config["port"],
                dbname=sibling,
                user="campus_ai_migration",
                password=config["migration"],
            )
            try:
                connection.execute("SET ROLE campus_ai_db_owner")
                assert connection.execute(
                    "SELECT current_database(),system_identifier::text FROM pg_control_system()"
                ).fetchone() == (sibling, "7688048026898145318")
                assert connection.execute("SELECT version_num FROM alembic_version").fetchone() in {
                    ("20260922_44",),
                    ("20260922_45",),
                }
            except BaseException:
                connection.close()
                raise
            return connection

        try:
            yield connect
        finally:
            assert admin.execute(
                "SELECT system_identifier::text FROM pg_control_system()"
            ).fetchone() == ("7688048026898145318",)
            admin.execute("DROP DATABASE f6_fixture_concurrency_test")


@pytest.mark.parametrize("competing", ["student", "bed"])
def test_real_concurrent_residences_serialize_and_only_one_can_commit(
    owned_concurrency_database,
    competing,
):
    connect = owned_concurrency_database
    with connect() as setup:
        school, source, student, room, bed = seed(setup)
        _insert_student_school_relationship(
            setup,
            school_id=school,
            source_id=source,
            student_id=student,
            key="race:first",
            valid_from=_at(1),
            valid_to=None,
        )
        other_student, other_bed = student, bed
        if competing == "student":
            other_room, other_bed = uuid4(), uuid4()
            _insert_room(
                setup,
                school_id=school,
                source_id=source,
                room_id=other_room,
                key="race:other",
                capacity=1,
            )
            _insert_bed(
                setup,
                school_id=school,
                source_id=source,
                room_id=other_room,
                bed_id=other_bed,
                key="race:other",
            )
        else:
            other_student = uuid4()
            _insert_person_and_student(
                setup,
                school_id=school,
                person_id=uuid4(),
                student_id=other_student,
                key="race:other",
            )
            _insert_student_school_relationship(
                setup,
                school_id=school,
                source_id=source,
                student_id=other_student,
                key="race:other",
                valid_from=_at(1),
                valid_to=None,
            )
        setup.execute("SET CONSTRAINTS ALL IMMEDIATE")
    started, pid = Event(), {}

    def contender():
        with connect() as connection:
            connection.execute("SET LOCAL lock_timeout='5s'")
            pid["value"] = connection.execute("SELECT pg_backend_pid()").fetchone()[0]
            started.set()
            try:
                _insert_residence(
                    connection,
                    school_id=school,
                    source_id=source,
                    student_id=other_student,
                    bed_id=other_bed,
                    valid_from=_at(10),
                )
                connection.execute("SET CONSTRAINTS ALL IMMEDIATE")
            except psycopg.errors.ExclusionViolation as rejected:
                name = rejected.diag.constraint_name
                connection.rollback()
                return name
            raise AssertionError(
                "Both conflicting residence arrangements became eligible to commit"
            )

    with connect() as first:
        first_pid = first.execute("SELECT pg_backend_pid()").fetchone()[0]
        _insert_residence(
            first,
            school_id=school,
            source_id=source,
            student_id=student,
            bed_id=bed,
            valid_from=_at(10),
        )
        first.execute("SET CONSTRAINTS ALL IMMEDIATE")
        with ThreadPoolExecutor(max_workers=1) as workers:
            pending = workers.submit(contender)
            assert started.wait(2)
            blocked = False
            with connect() as observer:
                deadline = monotonic() + 2
                while monotonic() < deadline and not pending.done():
                    if (
                        first_pid
                        in observer.execute(
                            "SELECT pg_blocking_pids(%s)", (pid["value"],)
                        ).fetchone()[0]
                    ):
                        blocked = True
                        break
                    sleep(0.01)
            if blocked:
                first.commit()
            else:
                first.rollback()
            rejected_constraint = pending.result(timeout=6)
            assert blocked, "The contender must be observed waiting on the real first transaction"
    assert competing in rejected_constraint
    with connect() as verify:
        assert (
            verify.execute(
                "SELECT count(*) FROM student_bed_residences WHERE school_id=%s", (school,)
            ).fetchone()[0]
            == 1
        )


@pytest.mark.parametrize("change", ["capacity", "location"])
@pytest.mark.parametrize("first_action", ["facility", "residence"])
def test_concurrent_facility_restriction_and_new_residence_cannot_both_commit(
    owned_concurrency_database,
    change,
    first_action,
):
    """Exercise both real commit orders; every losing transaction rolls back."""
    connect = owned_concurrency_database
    with connect() as setup:
        school, source, student, room, bed = seed(setup)
        _insert_student_school_relationship(
            setup,
            school_id=school,
            source_id=source,
            student_id=student,
            key="facility-race",
            valid_from=_at(1),
            valid_to=None,
        )
        setup.execute("SET CONSTRAINTS ALL IMMEDIATE")

    def apply(connection, action):
        if action == "residence":
            _insert_residence(
                connection,
                school_id=school,
                source_id=source,
                student_id=student,
                bed_id=bed,
                valid_from=_at(10),
            )
        elif change == "location":
            connection.execute(
                "UPDATE campus_locations SET status='inactive',version=version+1 "
                "WHERE school_id=%s AND id=%s",
                (school, room),
            )
        else:
            # A legitimate capacity reduction resolves the available bed in the
            # same transaction. The old room/bed periods remain in the database.
            connection.execute(
                "UPDATE dorm_bed_versions SET valid_to=%s WHERE school_id=%s "
                "AND bed_id=%s AND superseded_at IS NULL",
                (_at(10), school, bed),
            )
            connection.execute(
                "UPDATE dorm_room_profile_versions SET valid_to=%s WHERE school_id=%s "
                "AND room_location_id=%s AND superseded_at IS NULL",
                (_at(10), school, room),
            )
            connection.execute(
                """INSERT INTO dorm_room_profile_versions(id,school_id,room_location_id,
                authorized_bed_capacity,accommodation_zone,is_available,accessibility_tags,
                valid_from,valid_to,version,source_id) VALUES (%s,%s,%s,0,'general',true,
                ARRAY[]::varchar[],%s,NULL,2,%s)""",
                (uuid4(), school, room, _at(10), source),
            )
        connection.execute("SET CONSTRAINTS ALL IMMEDIATE")

    started, pid = Event(), {}

    def contender():
        with connect() as connection:
            connection.execute("SET LOCAL lock_timeout='5s'")
            pid["value"] = connection.execute("SELECT pg_backend_pid()").fetchone()[0]
            started.set()
            try:
                apply(connection, "residence" if first_action == "facility" else "facility")
            except psycopg.errors.CheckViolation as rejected:
                constraint = rejected.diag.constraint_name
                connection.rollback()
                return constraint
            raise AssertionError(
                "An incompatible facility and residence change both became committable"
            )

    with connect() as first:
        first_pid = first.execute("SELECT pg_backend_pid()").fetchone()[0]
        apply(first, first_action)
        with ThreadPoolExecutor(max_workers=1) as workers:
            pending = workers.submit(contender)
            assert started.wait(2)
            blocked = False
            with connect() as observer:
                deadline = monotonic() + 2
                while monotonic() < deadline and not pending.done():
                    if (
                        first_pid
                        in observer.execute(
                            "SELECT pg_blocking_pids(%s)", (pid["value"],)
                        ).fetchone()[0]
                    ):
                        blocked = True
                        break
                    sleep(0.01)
            first.commit() if blocked else first.rollback()
            rejected = pending.result(timeout=6)
            assert blocked, "The actual concurrent dependency lock must be observed"
    assert rejected in {
        "ck_student_bed_residences_bed_available",
        "ck_student_bed_residences_active_location_ancestry",
    }
    with connect() as verify:
        assert verify.execute(
            "SELECT count(*) FROM student_bed_residences WHERE school_id=%s", (school,)
        ).fetchone()[0] == (first_action == "residence")
        if change == "location":
            assert verify.execute(
                "SELECT status FROM campus_locations WHERE school_id=%s AND id=%s", (school, room)
            ).fetchone()[0] == ("active" if first_action == "residence" else "inactive")
        else:
            assert verify.execute(
                "SELECT authorized_bed_capacity FROM dorm_room_profile_versions "
                "WHERE school_id=%s AND room_location_id=%s AND valid_to IS NULL "
                "AND superseded_at IS NULL",
                (school, room),
            ).fetchone()[0] == (1 if first_action == "residence" else 0)
