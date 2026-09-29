from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Barrier
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import QueuePool

from app.composition.school_fact_authority_connection import ConnectionBoundAuthority


@pytest.fixture
def setup(monkeypatch):
    engine = create_engine("sqlite://")
    # SQLite checks lifecycle only; fresh READ COMMITTED visibility needs real PG.
    monkeypatch.setattr(engine.dialect, "get_isolation_level", lambda _: "READ COMMITTED")
    counts = {"checkout": 0, "queries": 0}
    event.listen(
        engine, "checkout", lambda *_: counts.__setitem__("checkout", counts["checkout"] + 1)
    )
    event.listen(
        engine,
        "before_cursor_execute",
        lambda *_: counts.__setitem__("queries", counts["queries"] + 1),
    )
    yield engine, sessionmaker(bind=engine), counts
    engine.dispose()


def test_call_reuses_connection_not_results_and_next_call_checks_out_again(setup):
    _, sessions, counts = setup
    seen = []

    def build(bound):
        def issue(_):
            for _index in range(4):
                with bound.begin() as session:
                    seen.append(session)
                    assert session.scalar(text("select 1")) == 1
            return "result"

        return SimpleNamespace(issue=issue)

    service = ConnectionBoundAuthority(sessions=sessions, build_basis=build)
    assert service.issue(None) == "result"
    assert counts == {"checkout": 1, "queries": 4}
    assert len({id(s) for s in seen}) == 4
    assert service.issue(None) == "result"
    assert counts == {"checkout": 2, "queries": 8}


def test_rollback_and_exception_fail_closed_and_next_call_recovers(setup):
    _, sessions, counts = setup
    state = ["rollback"]

    def build(bound):
        def issue(_):
            with bound() as session:
                session.scalar(text("select 1"))
                if state[0] == "rollback":
                    session.rollback()
                if state[0] == "error":
                    raise ValueError("expected failure")
            return "result"

        return SimpleNamespace(issue=issue)

    service = ConnectionBoundAuthority(sessions=sessions, build_basis=build)
    with pytest.raises(Exception, match="transaction"):
        service.issue(None)
    state[0] = "error"
    with pytest.raises(ValueError, match="expected failure"):
        service.issue(None)
    state[0] = "ok"
    assert service.issue(None) == "result"
    assert counts["checkout"] == 3


def test_non_read_committed_is_rejected(setup, monkeypatch):
    engine, sessions, _ = setup
    monkeypatch.setattr(engine.dialect, "get_isolation_level", lambda _: "REPEATABLE READ")
    service = ConnectionBoundAuthority(
        sessions=sessions, build_basis=lambda _: pytest.fail("must not build")
    )
    with pytest.raises(Exception, match="READ COMMITTED"):
        service.issue(None)


def test_validate_releases_connection_on_success_denial_and_error(setup):
    engine, sessions, counts = setup
    checkins = []
    event.listen(engine, "checkin", lambda *_: checkins.append(True))
    expected_basis = object()
    expected_time = datetime.now(UTC)
    outcome = [True]

    def build(bound):
        def validate(basis, *, as_of):
            assert basis is expected_basis
            assert as_of is expected_time
            with bound.begin() as session:
                assert session.scalar(text("select 1")) == 1
            if isinstance(outcome[0], Exception):
                raise outcome[0]
            return outcome[0]

        return SimpleNamespace(validate=validate)

    service = ConnectionBoundAuthority(sessions=sessions, build_basis=build)
    assert service.validate(expected_basis, as_of=expected_time) is True
    assert len(checkins) == 1
    outcome[0] = False
    assert service.validate(expected_basis, as_of=expected_time) is False
    assert len(checkins) == 2
    outcome[0] = ValueError("validation failed")
    with pytest.raises(ValueError, match="validation failed"):
        service.validate(expected_basis, as_of=expected_time)
    assert len(checkins) == 3
    assert counts == {"checkout": 3, "queries": 3}


def test_concurrent_calls_use_distinct_connections_and_release_pool(monkeypatch):
    engine = create_engine(
        "sqlite://",
        poolclass=QueuePool,
        pool_size=2,
        max_overflow=0,
        connect_args={"check_same_thread": False},
    )
    monkeypatch.setattr(engine.dialect, "get_isolation_level", lambda _: "READ COMMITTED")
    overlap = Barrier(2, timeout=5)

    def build(bound):
        def issue(value):
            connection_ids = []
            results = []
            for _ in range(2):
                with bound.begin() as session:
                    connection_ids.append(id(session.connection().connection.dbapi_connection))
                    results.append(session.scalar(text("select :value"), {"value": value}))
                    overlap.wait()
            return connection_ids, results

        return SimpleNamespace(issue=issue)

    service = ConnectionBoundAuthority(sessions=sessionmaker(bind=engine), build_basis=build)
    try:
        with ThreadPoolExecutor(max_workers=2) as workers:
            futures = [workers.submit(service.issue, value) for value in (11, 22)]
            first, second = [future.result(timeout=10) for future in futures]
        assert first[1] == [11, 11]
        assert second[1] == [22, 22]
        assert len(set(first[0])) == len(set(second[0])) == 1
        assert first[0][0] != second[0][0]
        assert engine.pool.checkedout() == 0
    finally:
        engine.dispose()


def test_short_scope_reuses_connection_only_and_next_scope_is_fresh(setup):
    from tests.test_f2_directory_product import actor

    _, sessions, counts = setup
    seen = []
    outcomes = iter(["allow", "deny", "allow"])

    def build(bound):
        def issue(query):
            with bound() as session:
                seen.append(session)
                session.scalar(text("select 1"))
            return next(outcomes)

        return SimpleNamespace(issue=issue)

    service = ConnectionBoundAuthority(sessions=sessions, build_basis=build)
    who = actor()
    query = SimpleNamespace(school_id=who.school_id, assignment_id=who.assignment_id)
    with service.read_scope(who):
        assert service.issue(query) == "allow"
        assert service.issue(query) == "deny"
        assert counts == {"checkout": 1, "queries": 2}
    with service.read_scope(who):
        assert service.issue(query) == "allow"
    assert counts == {"checkout": 2, "queries": 3}
    assert len({id(s) for s in seen}) == 3


def test_short_scope_rejects_changed_actor_thread_and_scope_keys(setup):
    from contextvars import copy_context
    from dataclasses import asdict
    from uuid import uuid4

    from app.platform.contracts import _issue_staff_actor_context
    from tests.test_f2_directory_product import actor

    _, sessions, counts = setup
    service = ConnectionBoundAuthority(
        sessions=sessions, build_basis=lambda _: SimpleNamespace(issue=lambda _: True)
    )
    who = actor()
    other = _issue_staff_actor_context(
        **{**asdict(who), "account_user_id": who.account_user_id + 1}
    )
    query = SimpleNamespace(school_id=who.school_id, assignment_id=who.assignment_id)
    with service.read_scope(who):
        with pytest.raises(Exception, match="identity"):
            with service.read_scope(other):
                pytest.fail("must reject full actor mismatch")
        with pytest.raises(Exception, match="identity"):
            service.issue(SimpleNamespace(school_id=uuid4(), assignment_id=who.assignment_id))
        with ThreadPoolExecutor(max_workers=1) as workers:
            future = workers.submit(copy_context().run, service.issue, query)
            with pytest.raises(Exception, match="thread"):
                future.result(timeout=3)
        stale = copy_context()
    with pytest.raises(Exception, match="closed"):
        stale.run(service.issue, query)
    assert counts["checkout"] == 0


def test_short_scope_transaction_failure_closes_and_next_scope_recovers(setup):
    from tests.test_f2_directory_product import actor

    engine, sessions, counts = setup
    checkins = []
    event.listen(engine, "checkin", lambda *_: checkins.append(True))
    mode = ["rollback"]

    def build(bound):
        def issue(query):
            with bound() as session:
                session.scalar(text("select 1"))
                if mode[0] == "rollback":
                    session.rollback()
                if mode[0] == "error":
                    raise ValueError("expected error")
            return True

        return SimpleNamespace(issue=issue)

    service = ConnectionBoundAuthority(sessions=sessions, build_basis=build)
    who = actor()
    query = SimpleNamespace(school_id=who.school_id, assignment_id=who.assignment_id)
    for failure in ("rollback", "error"):
        mode[0] = failure
        with pytest.raises(Exception, match="transaction|expected error"):
            with service.read_scope(who):
                service.issue(query)
    mode[0] = "ok"
    with service.read_scope(who):
        assert service.issue(query)
    assert len(checkins) == counts["checkout"] == 3


def test_deduplicated_empty_pass_does_not_checkout_a_connection(setup):
    from tests.test_f2_directory_product import actor

    _, sessions, counts = setup
    service = ConnectionBoundAuthority(
        sessions=sessions, build_basis=lambda _: pytest.fail("no SQL needed")
    )
    with service.read_scope(actor()):
        pass
    assert counts == {"checkout": 0, "queries": 0}


def test_postgres_parallel_short_scopes_use_independent_connections(isolated_migration_schema):
    from tests.governance.test_thin_authorization import _staff_actor
    from tests.governance.test_writer_lifecycle import _writer_environment

    with _writer_environment(isolated_migration_schema, minimal_owner_facts=True) as harness:
        barrier = Barrier(2, timeout=5)

        def build(bound):
            def issue(query):
                with bound() as session:
                    pid = session.scalar(text("SELECT pg_backend_pid()"))
                    barrier.wait()
                    return pid

            return SimpleNamespace(issue=issue)

        service = ConnectionBoundAuthority(sessions=harness.factory, build_basis=build)
        who = _staff_actor(harness)
        query = SimpleNamespace(school_id=who.school_id, assignment_id=who.assignment_id)

        def read():
            with service.read_scope(who):
                return (service.issue(query), service.issue(query))

        with ThreadPoolExecutor(max_workers=2) as workers:
            pending = [workers.submit(read) for _ in range(2)]
            first, second = [item.result(timeout=10) for item in pending]
        assert first[0] == first[1] and second[0] == second[1] and first[0] != second[0]
