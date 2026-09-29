"""Three bounded owned40 checks; no facts, roles, or grants are written."""

# ruff: noqa: F811 -- imported pytest fixtures are intentional

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Event

import pytest
from psycopg.errors import QueryCanceled
from sqlalchemy import create_engine, event, text
from sqlalchemy.exc import TimeoutError
from sqlalchemy.orm import sessionmaker

from app.composition.school_fact_education_query import active_segment
from app.composition.school_fact_family_teaching_query import FamilyTeachingRepository
from tests.school_facts.test_f5_family_teaching_assembly_postgres import assembled  # noqa: F401
from tests.school_facts.test_f5_family_teaching_postgres import owned_family  # noqa: F401


def test_open_teaching_identity_survives_two_semesters_and_reconnect(owned_family):
    engines, guardian = owned_family
    keys = {
        "teaching_offering": "f5-pg-seed:math-offering",
        "teaching_assignment": "f5-pg-seed:math-teacher",
    }
    fields = {
        "teaching_offering": ["course_source_key", "offering_status"],
        "teaching_assignment": ["offering_source_key", "assignment_source_key", "teaching_role"],
    }
    snapshot = text(
        "SELECT f.kind,f.source_key,f.id,f.version,count(v.id) FROM "
        "school_education_facts f JOIN school_education_versions v ON v.fact_id=f.id "
        "WHERE ((f.kind='teaching_offering' AND f.source_key='f5-pg-seed:math-offering') "
        "OR (f.kind='teaching_assignment' AND f.source_key='f5-pg-seed:math-teacher')) "
        "GROUP BY f.kind,f.source_key,f.id,f.version "
        "ORDER BY f.kind"
    )
    with engines["migration"].connect() as c:
        before = c.execute(snapshot, {"keys": list(keys.values())}).all()
    assert len(before) == 2
    baseline = None
    school_version = None
    for at in (
        datetime(2026, 9, 14, tzinfo=UTC),
        datetime(2027, 3, 1, tzinfo=UTC),
        datetime(2027, 9, 2, tzinfo=UTC),
    ):
        # A new physical connection, not a cached prior response or annual publication.
        engines["reader"].dispose()
        values = {}
        with engines["reader"].connect() as c:
            for kind, key in keys.items():
                value = c.scalar(
                    text(
                        "SELECT app.school_fact_f5_targets(:s,:k,:keys,"
                        ":fields,:at,NULL,'{}'::jsonb)"
                    ),
                    {
                        "s": guardian["school_id"],
                        "k": kind,
                        "keys": [key],
                        "fields": fields[kind],
                        "at": at,
                    },
                )
                (row,) = value["records"]
                segment = active_segment(row, at)
                assert segment is not None
                assert segment["valid_to"] is None
                values[kind] = (row["source_key"], row["version"], segment["content"])
                if school_version is None:
                    school_version = value["school_fact_version"]
                assert value["school_fact_version"] == school_version
        if baseline is None:
            baseline = values
        assert values == baseline
    with engines["migration"].connect() as c:
        assert c.execute(snapshot, {"keys": list(keys.values())}).all() == before
    print(
        "T01: exact seeded offering/teacher unchanged across 2026-09, 2027-03, 2027-09; "
        "root ids, versions, physical version-row counts unchanged"
    )


def test_real_f5_repository_pool_wait_timeout_and_recovery(assembled):
    _, actor, _, engines = assembled
    engine = create_engine(
        engines["reader"].url,
        creator=engines["reader"].pool._creator,
        hide_parameters=True,
        pool_size=1,
        max_overflow=0,
        pool_timeout=0.05,
    )
    repository = FamilyTeachingRepository(sessionmaker(engine))
    try:
        with engine.connect():
            with pytest.raises(TimeoutError):
                repository.teaching_stamp(actor)
            assert engine.pool.checkedout() == 1
        assert engine.pool.checkedout() == 0
        assert repository.teaching_stamp(actor)
        assert engine.pool.checkedout() == 0
        # Also prove a bounded waiting reader succeeds after the holder returns.
        started = Event()

        def waiting():
            started.set()
            return repository.teaching_stamp(actor)

        with ThreadPoolExecutor(max_workers=1) as executor:
            holder = engine.connect()
            try:
                future = executor.submit(waiting)
                assert started.wait(1)
            finally:
                holder.close()
            assert future.result(timeout=2)
        assert engine.pool.checkedout() == 0
    finally:
        engine.dispose()


def test_real_f5_sql_timeout_rolls_back_and_next_read_recovers(assembled):
    _, actor, _, engines = assembled
    engine = create_engine(
        engines["reader"].url,
        creator=engines["reader"].pool._creator,
        hide_parameters=True,
        pool_size=1,
        max_overflow=0,
        pool_timeout=0.2,
    )
    repository = FamilyTeachingRepository(sessionmaker(engine))
    injected = False

    def time_out(connection, cursor, statement, parameters, context, executemany):
        nonlocal injected
        if not injected and "school_fact_family_teaching_stamp" in statement:
            injected = True
            cursor.execute("SET LOCAL statement_timeout='20ms'")
            cursor.execute("SELECT pg_sleep(0.2)")

    event.listen(engine, "before_cursor_execute", time_out)
    try:
        with pytest.raises(QueryCanceled) as failure:
            repository.teaching_stamp(actor)
        assert failure.value.sqlstate == "57014"
        assert injected and engine.pool.checkedout() == 0
        assert repository.teaching_stamp(actor)
        assert engine.pool.checkedout() == 0
        with engine.connect() as c:
            assert not c.in_transaction()
            assert c.scalar(text("SHOW statement_timeout")) != "20ms"
    finally:
        event.remove(engine, "before_cursor_execute", time_out)
        engine.dispose()
