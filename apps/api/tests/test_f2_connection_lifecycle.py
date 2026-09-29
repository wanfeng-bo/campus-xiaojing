"""Bounded pool ownership checks; SQL fixtures contain no school data."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime
from threading import Barrier, Event, Lock
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest
from sqlalchemy import URL, create_engine, event, false, literal, select, text
from sqlalchemy.exc import TimeoutError as PoolTimeout
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import QueuePool

from app.composition.school_fact_authority_connection import ConnectionBoundAuthority
from app.composition.school_fact_read_connection import F2ConnectionReadExecutor
from app.platform.authorization.f2_reader_contracts import F2_ROW_SPECS
from app.platform.authorization.secure_reader_compiler import CompiledReadRelation
from app.platform.authorization.secure_reader_contracts import (
    SecureReadAuthorizationDrift,
    SecureReadDenied,
    SecureReadRequest,
)
from app.platform.authorization.secure_reader_registry import f2_operation_registry
from app.platform.authorization.secure_reader_service import SecureCursorCodec, SecureReader
from app.platform.authorization.thin_contracts import NamedProjection, ScopePlan
from tests.test_f2_directory_product import actor


@contextmanager
def lifecycle_engine(monkeypatch=None, *, url: str | URL = "sqlite://", size=1, timeout=0.2):
    options: dict[str, Any] = dict(
        poolclass=QueuePool, pool_size=size, max_overflow=0, pool_timeout=timeout
    )
    if url == "sqlite://":
        options["connect_args"] = {"check_same_thread": False}
    engine = create_engine(url, **options)
    if monkeypatch is not None:
        # Only SQLite lacks the PG isolation levels. Ownership still uses real
        # SQLAlchemy Sessions, transactions and a bounded QueuePool.
        monkeypatch.setattr(engine.dialect, "get_isolation_level", lambda _: "READ COMMITTED")
        monkeypatch.setattr(
            engine.dialect,
            "get_isolation_level_values",
            lambda _: ("READ COMMITTED", "REPEATABLE READ", "SERIALIZABLE"),
        )
        monkeypatch.setattr(engine.dialect, "set_isolation_level", lambda *_: None)
    try:
        yield cast(Any, engine)
    finally:
        engine.dispose()


def lifecycle_reader(
    engine, *, decisions=None, before_count=lambda: None, source=None, before_reserve=lambda: None
):
    sessions = sessionmaker(bind=engine)
    who = actor()
    query = SimpleNamespace(school_id=who.school_id, assignment_id=who.assignment_id)
    checks = []
    decisions = iter(decisions or (True, True))

    def build(bound):
        def issue(query):
            with bound() as session:
                return source(session) if source else session.scalar(text("SELECT 1")) == 1

        return SimpleNamespace(issue=issue)

    authority = cast(Any, ConnectionBoundAuthority(sessions=sessions, build_basis=cast(Any, build)))

    class Authorizer:
        def decide(self, *, operation, **kwargs):
            if not authority.issue(query):
                return False
            return SimpleNamespace(
                allowed=True,
                scope_plan=ScopePlan(uuid4(), "school_facts.f2", "synthetic", 1),
                projection=NamedProjection(operation.projection_name, 1),
                basis_fingerprint="synthetic-authority",
            )

        def revalidate(self, *args, **kwargs):
            # Record ownership before attempting acquisition, so the original
            # nested checkout defect is observable even when the pool times out.
            checks.append(engine.pool.checkedout())
            if not authority.issue(query):
                return False
            result = next(decisions)
            if isinstance(result, BaseException):
                raise result
            return result

    class Compiler:
        def compile(self, *, operation, as_of, page_size, **kwargs):
            before_reserve()
            return CompiledReadRelation(
                operation.code,
                as_of,
                page_size,
                tuple(name for name, _ in F2_ROW_SPECS),
                select(literal(0)),
                select(literal(0)).where(false()),
            )

    class Executor(F2ConnectionReadExecutor):
        @contextmanager
        def validated_read(self, before_read, after_read):
            with super().validated_read(before_read, after_read) as transaction:
                before_count()
                yield transaction

    reader = SecureReader(
        registry=f2_operation_registry(),
        compiler=cast(Any, Compiler()),
        authorizer=Authorizer(),
        executor=cast(Any, Executor(sessions, authority)),
        cursor_codec=SecureCursorCodec(b"synthetic-lifecycle-key"),
        clock=lambda: datetime.now(UTC),
    )
    request = SecureReadRequest(actor=who, operation_code="school_facts.f2.school.read")
    return reader, request, checks, authority


def test_single_connection_read_does_not_recursively_borrow_its_own_pool(monkeypatch):
    with lifecycle_engine(monkeypatch) as engine:
        reader, request, checks, _ = lifecycle_reader(engine)
        page = reader.read(request)
        assert page.total_count == 0 and page.rows == ()
        assert checks == [1, 1]  # Same reserved physical connection in fresh RC phases.
        assert engine.pool.checkedout() == 0


@pytest.fixture
def postgres_lifecycle(isolated_migration_schema):
    roles = isolated_migration_schema
    params = roles.runtime_connection_parameters
    url = URL.create(
        "postgresql+psycopg",
        username=params["user"],
        password=params["password"],
        host=params["host"],
        port=params["port"],
        database=params["dbname"],
    )
    with lifecycle_engine(url=url, size=1, timeout=5) as engine:
        yield engine, roles


@pytest.mark.integration
@pytest.mark.parametrize("concurrent", [1, 4, 8])
def test_postgres_one_connection_concurrent_reader_phases(postgres_lifecycle, concurrent):
    engine, _ = postgres_lifecycle
    start = Barrier(concurrent)
    observed = []
    lock = Lock()

    def source(session):
        isolation, pid = session.execute(
            text("SELECT current_setting('transaction_isolation'), pg_backend_pid()")
        ).one()
        with lock:
            observed.append(("authority", isolation, pid))
        return True

    def query(connection, cursor, statement, parameters, context, executemany):
        if statement.startswith("SELECT %("):
            with lock:
                observed.append(("data", connection.get_isolation_level().lower(), None))

    event.listen(engine, "before_cursor_execute", query)

    def read():
        reader, request, _, _ = lifecycle_reader(engine, source=source)
        start.wait(timeout=10)
        return reader.read(request)

    with ThreadPoolExecutor(max_workers=concurrent) as workers:
        pages = list(workers.map(lambda _: read(), range(concurrent)))
    assert all(page.rows == () for page in pages)
    assert len([item for item in observed if item[0] == "authority"]) == concurrent * 3
    assert {item[1] for item in observed if item[0] == "authority"} == {"read committed"}
    assert {item[1] for item in observed if item[0] == "data"} == {"repeatable read"}
    assert len({item[2] for item in observed if item[0] == "authority"}) == 1
    assert engine.pool.checkedout() == 0


def _authority_table(roles):
    with roles.admin_connection() as admin:
        admin.execute("CREATE TABLE public.f2_lifecycle_authority (allowed boolean NOT NULL)")
        admin.execute("INSERT INTO public.f2_lifecycle_authority VALUES (true)")
        admin.execute("GRANT SELECT ON public.f2_lifecycle_authority TO campus_ai_runtime")


def _stored_authority(session):
    return session.scalar(text("SELECT allowed FROM public.f2_lifecycle_authority"))


@pytest.mark.integration
def test_postgres_revocation_during_pool_wait_is_checked_before_data(postgres_lifecycle):
    engine, roles = postgres_lifecycle
    _authority_table(roles)
    compiled, resume, queued = Event(), Event(), Event()
    data_reads = []

    def before_reserve():
        compiled.set()
        assert resume.wait(5)
        queued.set()

    def query(conn, cursor, statement, parameters, context, many):
        if statement.startswith("SELECT %("):
            data_reads.append(1)

    event.listen(engine, "before_cursor_execute", query)
    reader, request, _, _ = lifecycle_reader(
        engine, source=_stored_authority, before_reserve=before_reserve
    )
    with ThreadPoolExecutor(max_workers=1) as workers:
        future = workers.submit(reader.read, request)
        try:
            assert compiled.wait(5)
            with engine.connect():
                resume.set()
                assert queued.wait(5)
                with roles.admin_connection() as admin:
                    admin.execute("UPDATE public.f2_lifecycle_authority SET allowed=false")
            with pytest.raises(SecureReadDenied):
                future.result(timeout=10)
        finally:
            resume.set()
    assert data_reads == []
    assert engine.pool.checkedout() == 0


@pytest.mark.integration
def test_postgres_post_read_authority_uses_new_committed_state(postgres_lifecycle):
    engine, roles = postgres_lifecycle
    _authority_table(roles)

    def revoke():
        with roles.admin_connection() as admin:
            admin.execute("UPDATE public.f2_lifecycle_authority SET allowed=false")

    reader, request, _, _ = lifecycle_reader(engine, source=_stored_authority, before_count=revoke)
    with pytest.raises(SecureReadAuthorizationDrift):
        reader.read(request)
    assert engine.pool.checkedout() == 0


@pytest.mark.integration
@pytest.mark.parametrize("phase", ["precheck", "data", "postcheck", "cancel"])
def test_postgres_failed_phase_returns_connection_and_restores_read_committed(
    postgres_lifecycle, phase
):
    engine, _ = postgres_lifecycle
    error = asyncio.CancelledError() if phase == "cancel" else ValueError("synthetic phase failure")

    def fail():
        raise error

    reader, request, _, authority = lifecycle_reader(
        engine,
        decisions=[error]
        if phase == "precheck"
        else [True, error]
        if phase == "postcheck"
        else None,
        before_count=fail if phase in {"data", "cancel"} else lambda: None,
    )
    with pytest.raises(type(error)):
        reader.read(request)
    assert engine.pool.checkedout() == 0
    assert authority.current_sessions().kw["bind"] is engine
    with engine.connect() as recovered:
        assert recovered.get_isolation_level() == "READ COMMITTED"
        assert not recovered.in_transaction()
    fresh, request, _, _ = lifecycle_reader(engine)
    assert fresh.read(request).rows == ()
    assert engine.pool.checkedout() == 0


@pytest.mark.integration
def test_postgres_pool_timeout_leaves_only_holder_then_recovers(postgres_lifecycle):
    original, _ = postgres_lifecycle
    with lifecycle_engine(url=original.url, timeout=0.05) as engine:
        reader, request, _, authority = lifecycle_reader(engine)
        with engine.connect():
            with pytest.raises(PoolTimeout):
                reader.read(request)
            assert engine.pool.checkedout() == 1
            assert authority.current_sessions().kw["bind"] is engine
        assert engine.pool.checkedout() == 0
        assert reader.read(request).rows == ()
        assert engine.pool.checkedout() == 0


@pytest.mark.integration
def test_postgres_statement_timeout_rolls_back_data_phase_and_restores_connection(
    postgres_lifecycle,
):
    from psycopg.errors import QueryCanceled
    from psycopg.pq import TransactionStatus
    from sqlalchemy.exc import DBAPIError

    engine, _ = postgres_lifecycle
    with engine.connect() as connection:
        original_timeout = connection.scalar(text("SHOW statement_timeout"))
    injected = False
    aborted = []

    def time_out_data(connection, cursor, statement, parameters, context, executemany):
        nonlocal injected
        if injected or not statement.startswith("SELECT %("):
            return
        injected = True
        assert connection.get_isolation_level() == "REPEATABLE READ"
        # A test-only server deadline inside the actual data transaction. This
        # exercises psycopg's aborted-transaction path, not a Python exception.
        connection.exec_driver_sql("SET LOCAL statement_timeout = '25ms'")
        connection.exec_driver_sql("SELECT pg_sleep(1)")

    def observe_abort(context):
        if isinstance(context.original_exception, QueryCanceled):
            aborted.append(context.connection.connection.driver_connection.info.transaction_status)

    event.listen(engine, "before_cursor_execute", time_out_data)
    event.listen(engine, "handle_error", observe_abort)
    reader, request, checks, authority = lifecycle_reader(engine)
    try:
        with pytest.raises(DBAPIError) as failure:
            reader.read(request)
        assert isinstance(failure.value.orig, QueryCanceled)
        assert failure.value.orig.sqlstate == "57014"
    finally:
        event.remove(engine, "before_cursor_execute", time_out_data)
        event.remove(engine, "handle_error", observe_abort)
    assert injected and aborted and set(aborted) == {TransactionStatus.INERROR}
    assert checks == [1]  # The aborted data transaction cannot authorize a result.
    assert engine.pool.checkedout() == 0
    assert authority.current_sessions().kw["bind"] is engine
    with engine.connect() as recovered:
        assert recovered.get_isolation_level() == "READ COMMITTED"
        assert not recovered.in_transaction()
        assert (
            recovered.connection.driver_connection.info.transaction_status == TransactionStatus.IDLE
        )
        assert recovered.scalar(text("SHOW statement_timeout")) == original_timeout
        assert recovered.scalar(text("SELECT 1")) == 1
    fresh, request, _, _ = lifecycle_reader(engine)
    assert fresh.read(request).rows == ()
    assert engine.pool.checkedout() == 0


def test_borrowed_authority_rejects_thread_snapshot_transaction_and_nested_ownership(monkeypatch):
    from contextvars import copy_context

    from sqlalchemy.exc import SQLAlchemyError

    with lifecycle_engine(monkeypatch) as engine:
        _, request, _, authority = lifecycle_reader(engine)
        with engine.connect() as connection:
            with authority.borrow_connection(connection):
                assert authority.issue(request.actor)
                assert authority.current_sessions().kw["bind"] is connection
                copied = copy_context()
                with ThreadPoolExecutor(max_workers=1) as workers:
                    future = workers.submit(copied.run, authority.issue, request.actor)
                    with pytest.raises(SQLAlchemyError, match="thread"):
                        future.result(timeout=3)
                with pytest.raises(SQLAlchemyError, match="already active"):
                    with authority.borrow_connection(connection):
                        pytest.fail("nested borrowing must not reopen a phase")
            with pytest.raises(SQLAlchemyError, match="closed"):
                copied.run(authority.issue, request.actor)
            assert not connection.in_transaction()
            with connection.begin():
                with pytest.raises(SQLAlchemyError, match="fresh"):
                    with authority.borrow_connection(connection):
                        pytest.fail("must not borrow a preexisting transaction")
        assert authority.current_sessions().kw["bind"] is engine


def test_borrowed_authority_transaction_loss_fails_closed_and_clears_context(monkeypatch):
    from sqlalchemy.exc import SQLAlchemyError

    with lifecycle_engine(monkeypatch) as engine:
        _, _, _, authority = lifecycle_reader(engine)
        with engine.connect() as connection:
            with pytest.raises(SQLAlchemyError, match="transaction changed"):
                with authority.borrow_connection(connection):
                    connection.rollback()
            assert authority.current_sessions().kw["bind"] is engine
            assert not connection.in_transaction()


@pytest.mark.parametrize("outcome", ["deny_before", "deny_after", "failure", "cancel"])
def test_authority_failure_and_cancel_return_the_connection(monkeypatch, outcome):
    expected = {
        "deny_before": ([False], SecureReadDenied),
        "deny_after": ([True, False], SecureReadAuthorizationDrift),
        "failure": ([True, ValueError("source failed")], ValueError),
        "cancel": ([True, asyncio.CancelledError()], asyncio.CancelledError),
    }
    decisions, error = expected[outcome]
    with lifecycle_engine(monkeypatch) as engine:
        reader, request, _, _ = lifecycle_reader(engine, decisions=decisions)
        with pytest.raises(error):
            reader.read(request)
        assert engine.pool.checkedout() == 0
        recovered, request, _, _ = lifecycle_reader(engine)
        assert recovered.read(request).rows == ()
        assert engine.pool.checkedout() == 0


def test_exhausted_pool_timeout_releases_waiter_and_next_read_recovers(monkeypatch):
    with lifecycle_engine(monkeypatch, timeout=0.05) as engine:
        reader, request, _, _ = lifecycle_reader(engine)
        with engine.connect():
            with pytest.raises(PoolTimeout):
                reader.read(request)
            assert engine.pool.checkedout() == 1  # The intentional holder only.
        recovered, request, _, _ = lifecycle_reader(engine)
        assert recovered.read(request).rows == ()
        assert engine.pool.checkedout() == 0


@pytest.mark.parametrize("concurrent", [1, 4, 8])
def test_small_pool_parallel_reads_complete_without_nested_acquisition(monkeypatch, concurrent):
    with lifecycle_engine(monkeypatch, size=1, timeout=5) as engine:
        start = Barrier(concurrent)
        counts = {"borrowed": 0, "peak": 0, "checkouts": 0}
        lock = Lock()

        def checkout(*_):
            with lock:
                counts["borrowed"] += 1
                counts["checkouts"] += 1
                counts["peak"] = max(counts["peak"], counts["borrowed"])

        def checkin(*_):
            with lock:
                counts["borrowed"] -= 1

        event.listen(engine, "checkout", checkout)
        event.listen(engine, "checkin", checkin)

        def read():
            reader, request, _, _ = lifecycle_reader(engine)
            start.wait(timeout=5)
            return reader.read(request)

        with ThreadPoolExecutor(max_workers=concurrent) as workers:
            pages = list(workers.map(lambda _: read(), range(concurrent)))
        assert all(page.rows == () for page in pages)
        assert counts == {"borrowed": 0, "peak": 1, "checkouts": 2 * concurrent}
        assert engine.pool.checkedout() == 0
