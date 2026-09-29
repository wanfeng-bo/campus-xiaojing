from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from importlib import import_module
from pathlib import Path
from threading import Event, Thread
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import URL, Engine
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

from app.composition.governance_dependencies import SqlAlchemyGovernanceDependencyAdapter
from tests.governance.test_writer_lifecycle import (
    AS_OF,
    _document_sha256,
    _grant_command,
    _scope_document,
    _seed_formal_owner_facts,
    _TestOnlyScopeResolver,
)
from tests.master_data.conftest import PostgresRoles
from tests.school_facts.test_sf2_transaction import make_owner_factory, sf2_environment

pytestmark = pytest.mark.integration

API_ROOT = Path(__file__).resolve().parents[2]
AUTHORITY_REFERENCE = "deployment:campus_ai_migration"
TEST_MARKER = "bootstrap"
_BOOTSTRAP_PROCESS_PAYLOAD = "SF5A_BOOTSTRAP_PROCESS_PAYLOAD"
_BOOTSTRAP_PROCESS_ENTRYPOINT = (
    "from tests.governance.test_external_bootstrap import "
    "_closed_bootstrap_process_probe; _closed_bootstrap_process_probe()"
)


@dataclass(frozen=True, slots=True)
class _BootstrapApi:
    GovernanceWriter: type[Any]
    GovernanceActorContext: type[Any]
    CreateDutyCommand: type[Any]
    OpenGovernanceBootstrapCommand: type[Any]
    CloseGovernanceBootstrapCommand: type[Any]
    GovernanceBootstrapDenied: type[Exception]
    GovernanceBootstrapClosed: type[Exception]
    build_migration_bootstrap_writer: Any


def _load_bootstrap_api() -> _BootstrapApi:
    """Load lazily so collection succeeds and the missing Slice 4 module is the RED."""

    application = import_module("app.platform.governance.application")
    contracts = import_module("app.platform.governance.contracts")
    bootstrap = import_module("app.platform.governance.bootstrap")
    return _BootstrapApi(
        GovernanceWriter=application.GovernanceWriter,
        GovernanceActorContext=contracts.GovernanceActorContext,
        CreateDutyCommand=contracts.CreateDutyCommand,
        OpenGovernanceBootstrapCommand=bootstrap.OpenGovernanceBootstrapCommand,
        CloseGovernanceBootstrapCommand=bootstrap.CloseGovernanceBootstrapCommand,
        GovernanceBootstrapDenied=bootstrap.GovernanceBootstrapDenied,
        GovernanceBootstrapClosed=bootstrap.GovernanceBootstrapClosed,
        build_migration_bootstrap_writer=bootstrap.build_migration_bootstrap_writer,
    )


@dataclass(frozen=True, slots=True)
class _Facts:
    school_id: UUID
    user_id: int
    person_id: UUID
    assignment_id: UUID


@dataclass(slots=True)
class _BootstrapHarness:
    api: _BootstrapApi
    writer_api: Any
    factory: sessionmaker[Session]
    second_factory: sessionmaker[Session]
    engine: Engine
    second_engine: Engine
    writer: Any
    facts: _Facts
    dependency_port: Any
    scope_resolver: _TestOnlyScopeResolver


@contextmanager
def _bootstrap_environment(roles: PostgresRoles) -> Iterator[_BootstrapHarness]:
    api = _load_bootstrap_api()
    writer_api = import_module("tests.governance.test_writer_lifecycle")._load_writer_api()
    with sf2_environment(roles) as (factory, persistence, actor_facts):
        seeded = _seed_formal_owner_facts(factory, persistence, actor_facts)
        second_factory, raw_second_engine = make_owner_factory(roles)
        engine = cast(Engine, factory.kw["bind"])
        second_engine = cast(Engine, raw_second_engine)
        dependency_port = SqlAlchemyGovernanceDependencyAdapter(factory)
        scope_resolver = _TestOnlyScopeResolver()
        writer = api.build_migration_bootstrap_writer(
            session_factory=factory,
            clock=lambda: AS_OF,
            dependency_port=dependency_port,
            scope_owner_resolvers={"test-only.sf5a": scope_resolver},
        )
        assert type(writer) is api.GovernanceWriter
        try:
            yield _BootstrapHarness(
                api=api,
                writer_api=writer_api,
                factory=factory,
                second_factory=second_factory,
                engine=engine,
                second_engine=second_engine,
                writer=writer,
                facts=_Facts(
                    seeded.school_id,
                    seeded.user_id,
                    seeded.person_id,
                    seeded.assignment_id,
                ),
                dependency_port=dependency_port,
                scope_resolver=scope_resolver,
            )
        finally:
            second_engine.dispose()


def _actor(harness: _BootstrapHarness, *, reason: str) -> Any:
    return harness.api.GovernanceActorContext(
        school_id=harness.facts.school_id,
        actor_kind="platform_bootstrap",
        actor_reference=AUTHORITY_REFERENCE,
        actor_assignment_id=None,
        actor_assignment_version=None,
        request_id=uuid4(),
        reason=reason,
    )


def _create_bootstrap_duty_and_scope(harness: _BootstrapHarness) -> tuple[Any, Any]:
    duty = harness.writer.create_duty(
        harness.api.CreateDutyCommand(
            school_id=harness.facts.school_id,
            duty_code=f"test_only.{TEST_MARKER}.duty",
            display_name="虚构 bootstrap 测试职责",
            description="仅用于 SF5A 外部 bootstrap 机制验证，不是生产职责词典。",
            effective_from=AS_OF - timedelta(days=1),
            actor=_actor(harness, reason="bootstrap creates test-only duty"),
            idempotency_key=f"sf5a:{TEST_MARKER}:duty",
        )
    )
    document = _scope_document(harness.facts.school_id)
    scope = harness.writer.create_scope(
        harness.writer_api.CreateScopeCommand(
            school_id=harness.facts.school_id,
            scope_key=f"test-only:{TEST_MARKER}:scope",
            schema_owner="test-only.sf5a",
            schema_name="fictitious-school-scope",
            schema_version=1,
            reference_document=document,
            reference_sha256=_document_sha256(document),
            effective_from=AS_OF - timedelta(days=1),
            actor=_actor(harness, reason="bootstrap creates typed test-only scope"),
            idempotency_key=f"sf5a:{TEST_MARKER}:scope",
        )
    )
    return duty, scope


def _open_command(
    harness: _BootstrapHarness,
    *,
    idempotency_key: str = "sf5a:bootstrap:open",
) -> Any:
    return harness.api.OpenGovernanceBootstrapCommand(
        school_id=harness.facts.school_id,
        authority_reference=AUTHORITY_REFERENCE,
        actor=_actor(harness, reason="open external Governance bootstrap"),
        idempotency_key=idempotency_key,
    )


def _close_command(
    harness: _BootstrapHarness,
    *,
    idempotency_key: str = "sf5a:bootstrap:close",
) -> Any:
    return harness.api.CloseGovernanceBootstrapCommand(
        school_id=harness.facts.school_id,
        authority_reference=AUTHORITY_REFERENCE,
        expected_version=1,
        actor=_actor(harness, reason="permanently close external Governance bootstrap"),
        idempotency_key=idempotency_key,
    )


def _scalar(
    factory: sessionmaker[Session],
    statement: str,
    parameters: dict[str, object] | None = None,
) -> Any:
    with factory() as session:
        return session.execute(text(statement), parameters or {}).scalar_one()


def _rows(
    factory: sessionmaker[Session],
    statement: str,
    parameters: dict[str, object] | None = None,
) -> list[dict[str, Any]]:
    with factory() as session:
        return [
            dict(row) for row in session.execute(text(statement), parameters or {}).mappings().all()
        ]


def _runtime_factory(roles: PostgresRoles) -> tuple[sessionmaker[Session], Engine]:
    runtime = roles.runtime_connection_parameters
    engine = create_engine(
        URL.create(
            "postgresql+psycopg",
            username=str(runtime["user"]),
            password=str(runtime["password"]),
            host=str(runtime["host"]),
            port=int(runtime["port"]),
            database=str(runtime["dbname"]),
        ),
        poolclass=NullPool,
    )
    return sessionmaker(engine, expire_on_commit=False), engine


def _migration_process_environment(
    roles: PostgresRoles,
    *,
    school_id: UUID,
    as_of: datetime,
) -> dict[str, str]:
    migration = roles.migration_connection_parameters
    environment = os.environ.copy()
    environment[_BOOTSTRAP_PROCESS_PAYLOAD] = json.dumps(
        {
            "database": {
                "host": str(migration["host"]),
                "port": int(migration["port"]),
                "dbname": str(migration["dbname"]),
                "user": str(migration["user"]),
                "password": str(migration["password"]),
            },
            "school_id": str(school_id),
            "as_of": as_of.isoformat(),
        },
        separators=(",", ":"),
    )
    return environment


def _closed_bootstrap_process_probe() -> None:
    payload = cast(
        dict[str, Any],
        json.loads(os.environ[_BOOTSTRAP_PROCESS_PAYLOAD]),
    )
    database = cast(dict[str, Any], payload["database"])
    school_id = UUID(cast(str, payload["school_id"]))
    as_of = datetime.fromisoformat(cast(str, payload["as_of"]))
    engine = create_engine(
        URL.create(
            "postgresql+psycopg",
            username=str(database["user"]),
            password=str(database["password"]),
            host=str(database["host"]),
            port=int(database["port"]),
            database=str(database["dbname"]),
            query={"options": "-c role=campus_ai_db_owner"},
        ),
        poolclass=NullPool,
    )
    factory = sessionmaker(engine, expire_on_commit=False)
    api = _load_bootstrap_api()
    writer = api.build_migration_bootstrap_writer(
        session_factory=factory,
        clock=lambda: as_of,
        dependency_port=SqlAlchemyGovernanceDependencyAdapter(factory),
        scope_owner_resolvers={},
    )

    def actor(reason: str) -> Any:
        return api.GovernanceActorContext(
            school_id=school_id,
            actor_kind="platform_bootstrap",
            actor_reference=AUTHORITY_REFERENCE,
            actor_assignment_id=None,
            actor_assignment_version=None,
            request_id=uuid4(),
            reason=reason,
        )

    try:
        open_command = api.OpenGovernanceBootstrapCommand(
            school_id=school_id,
            authority_reference=AUTHORITY_REFERENCE,
            actor=actor("open external Governance bootstrap"),
            idempotency_key="sf5a:bootstrap:open",
        )
        try:
            writer.open_governance_bootstrap(open_command)
        except api.GovernanceBootstrapClosed:
            open_outcome = "typed_closed"
        else:
            raise AssertionError("closed bootstrap open replay was not rejected")

        close_result = writer.close_governance_bootstrap(
            api.CloseGovernanceBootstrapCommand(
                school_id=school_id,
                authority_reference=AUTHORITY_REFERENCE,
                expected_version=1,
                actor=actor("permanently close external Governance bootstrap"),
                idempotency_key="sf5a:bootstrap:close",
            )
        )
        print(
            json.dumps(
                {
                    "close_replayed": close_result.replayed,
                    "close_version": close_result.target_version,
                    "open_outcome": open_outcome,
                },
                sort_keys=True,
            )
        )
    finally:
        engine.dispose()


def _run_python_probe(
    entrypoint: str,
    *,
    environment: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", entrypoint],
        cwd=API_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )


def test_external_bootstrap_api_is_missing_until_slice4() -> None:
    api = _load_bootstrap_api()
    assert api.GovernanceWriter is not None
    assert api.build_migration_bootstrap_writer is not None


def test_closed_bootstrap_and_close_replay_survive_a_new_python_process(
    isolated_migration_schema: PostgresRoles,
) -> None:
    with _bootstrap_environment(isolated_migration_schema) as harness:
        harness.writer.open_governance_bootstrap(_open_command(harness))
        harness.writer.close_governance_bootstrap(_close_command(harness))

        completed = _run_python_probe(
            _BOOTSTRAP_PROCESS_ENTRYPOINT,
            environment=_migration_process_environment(
                isolated_migration_schema,
                school_id=harness.facts.school_id,
                as_of=AS_OF + timedelta(minutes=1),
            ),
        )

        assert completed.returncode == 0, completed.stderr
        payload = json.loads(completed.stdout.strip().splitlines()[-1])
        assert payload == {
            "close_replayed": True,
            "close_version": 2,
            "open_outcome": "typed_closed",
        }


def test_migration_trust_root_opens_creates_and_closes_through_one_writer(
    isolated_migration_schema: PostgresRoles,
) -> None:
    with _bootstrap_environment(isolated_migration_schema) as harness:
        identity = _rows(
            harness.factory,
            "SELECT session_user AS session_user, current_user AS current_user",
        )
        assert identity == [
            {
                "session_user": "campus_ai_migration",
                "current_user": "campus_ai_db_owner",
            }
        ]
        assert _scalar(harness.factory, "SELECT count(*) FROM governance_bootstrap_states") == 0
        assert (
            _scalar(
                harness.factory,
                "SELECT count(*) FROM governance_duties",
            )
            == 0
        )
        assert _scalar(harness.factory, "SELECT count(*) FROM governance_scopes") == 0
        assert _scalar(harness.factory, "SELECT count(*) FROM governance_grants") == 0

        open_command = _open_command(harness)
        opened = harness.writer.open_governance_bootstrap(open_command)
        replayed_open = harness.writer.open_governance_bootstrap(open_command)
        assert opened.target_id == harness.facts.school_id
        assert opened.target_version == replayed_open.target_version == 1
        assert opened.replayed is False
        assert replayed_open.replayed is True

        writer_harness = cast(
            Any,
            type(
                "_WriterHarness",
                (),
                {
                    "api": harness.writer_api,
                    "writer": harness.writer,
                    "facts": harness.facts,
                },
            )(),
        )
        duty, scope = _create_bootstrap_duty_and_scope(harness)
        grant = harness.writer.create_grant(
            replace(
                _grant_command(
                    writer_harness,
                    duty=duty,
                    scope=scope,
                    marker=TEST_MARKER,
                ),
                actor=_actor(harness, reason="bootstrap creates test-only Governance Grant"),
            )
        )
        assert grant.assignment_epoch == 1

        close_command = _close_command(harness)
        closed = harness.writer.close_governance_bootstrap(close_command)
        replayed_close = harness.writer.close_governance_bootstrap(close_command)
        assert closed.target_version == replayed_close.target_version == 2
        assert closed.replayed is False
        assert replayed_close.replayed is True

        replay_calls = (
            lambda: harness.writer.open_governance_bootstrap(open_command),
            lambda: harness.writer.create_duty(
                harness.api.CreateDutyCommand(
                    school_id=harness.facts.school_id,
                    duty_code=f"test_only.{TEST_MARKER}.duty",
                    display_name="虚构 bootstrap 测试职责",
                    description="仅用于 SF5A 外部 bootstrap 机制验证，不是生产职责词典。",
                    effective_from=AS_OF - timedelta(days=1),
                    actor=_actor(harness, reason="bootstrap creates test-only duty"),
                    idempotency_key=f"sf5a:{TEST_MARKER}:duty",
                )
            ),
            lambda: harness.writer.create_scope(
                harness.writer_api.CreateScopeCommand(
                    school_id=harness.facts.school_id,
                    scope_key=f"test-only:{TEST_MARKER}:scope",
                    schema_owner="test-only.sf5a",
                    schema_name="fictitious-school-scope",
                    schema_version=1,
                    reference_document=_scope_document(harness.facts.school_id),
                    reference_sha256=_document_sha256(_scope_document(harness.facts.school_id)),
                    effective_from=AS_OF - timedelta(days=1),
                    actor=_actor(harness, reason="bootstrap creates typed test-only scope"),
                    idempotency_key=f"sf5a:{TEST_MARKER}:scope",
                )
            ),
            lambda: harness.writer.create_grant(
                replace(
                    _grant_command(
                        writer_harness,
                        duty=duty,
                        scope=scope,
                        marker=TEST_MARKER,
                    ),
                    actor=_actor(
                        harness,
                        reason="bootstrap creates test-only Governance Grant",
                    ),
                )
            ),
        )
        for replay_call in replay_calls:
            with pytest.raises(harness.api.GovernanceBootstrapClosed):
                replay_call()

        assert _rows(
            harness.factory,
            """
            SELECT authority_reference, state, version, closed_at IS NOT NULL AS has_closed_at
            FROM governance_bootstrap_states
            WHERE school_id = :school_id
            """,
            {"school_id": harness.facts.school_id},
        ) == [
            {
                "authority_reference": AUTHORITY_REFERENCE,
                "state": "closed",
                "version": 2,
                "has_closed_at": True,
            }
        ]
        assert _scalar(harness.factory, "SELECT count(*) FROM governance_command_receipts") == 5
        assert _scalar(harness.factory, "SELECT count(*) FROM governance_audit_events") == 5
        assert _rows(
            harness.factory,
            """
            SELECT receipt.command_name, audit.event_type, audit.target_kind,
                   audit.target_id, audit.target_version
            FROM governance_command_receipts AS receipt
            JOIN governance_audit_events AS audit
              ON audit.command_receipt_id = receipt.id
            WHERE receipt.command_name LIKE 'governance.bootstrap.%'
            ORDER BY audit.target_version
            """,
        ) == [
            {
                "command_name": "governance.bootstrap.open",
                "event_type": "governance.bootstrap.opened",
                "target_kind": "bootstrap_state",
                "target_id": harness.facts.school_id,
                "target_version": 1,
            },
            {
                "command_name": "governance.bootstrap.close",
                "event_type": "governance.bootstrap.closed",
                "target_kind": "bootstrap_state",
                "target_id": harness.facts.school_id,
                "target_version": 2,
            },
        ]
        assert (
            _scalar(
                harness.factory,
                """
                SELECT epoch FROM governance_assignment_epochs
                WHERE school_id = :school_id AND assignment_id = :assignment_id
                """,
                {
                    "school_id": harness.facts.school_id,
                    "assignment_id": harness.facts.assignment_id,
                },
            )
            == 1
        )

        fresh_writer = harness.api.build_migration_bootstrap_writer(
            session_factory=harness.second_factory,
            clock=lambda: AS_OF + timedelta(minutes=1),
            dependency_port=harness.dependency_port,
            scope_owner_resolvers={"test-only.sf5a": harness.scope_resolver},
        )
        with pytest.raises(harness.api.GovernanceBootstrapClosed):
            fresh_writer.open_governance_bootstrap(
                _open_command(harness, idempotency_key="sf5a:bootstrap:must-not-reopen")
            )
        with pytest.raises(harness.api.GovernanceBootstrapClosed):
            fresh_writer.create_duty(
                harness.api.CreateDutyCommand(
                    school_id=harness.facts.school_id,
                    duty_code="test_only.bootstrap.must_not_write",
                    display_name="关闭后不得写入",
                    description=None,
                    effective_from=AS_OF,
                    actor=_actor(harness, reason="must fail after bootstrap close"),
                    idempotency_key="sf5a:bootstrap:must-not-write",
                )
            )
        closed_document = _scope_document(harness.facts.school_id)
        with pytest.raises(harness.api.GovernanceBootstrapClosed):
            fresh_writer.create_scope(
                harness.writer_api.CreateScopeCommand(
                    school_id=harness.facts.school_id,
                    scope_key="test-only:bootstrap:must-not-write",
                    schema_owner="test-only.sf5a",
                    schema_name="fictitious-school-scope",
                    schema_version=1,
                    reference_document=closed_document,
                    reference_sha256=_document_sha256(closed_document),
                    effective_from=AS_OF,
                    actor=_actor(harness, reason="closed bootstrap must not create scope"),
                    idempotency_key="sf5a:bootstrap:must-not-write-scope",
                )
            )
        with pytest.raises(harness.api.GovernanceBootstrapClosed):
            fresh_writer.create_grant(
                replace(
                    _grant_command(
                        writer_harness,
                        duty=duty,
                        scope=scope,
                        marker="bootstrap-must-not-write",
                    ),
                    actor=_actor(harness, reason="closed bootstrap must not create Grant"),
                )
            )
        assert _scalar(harness.factory, "SELECT count(*) FROM governance_command_receipts") == 5
        assert _scalar(harness.factory, "SELECT count(*) FROM governance_audit_events") == 5
        assert _scalar(harness.factory, "SELECT count(*) FROM governance_duties") == 1
        assert _scalar(harness.factory, "SELECT count(*) FROM governance_scopes") == 1
        assert _scalar(harness.factory, "SELECT count(*) FROM governance_grants") == 1


def test_runtime_session_user_cannot_impersonate_external_bootstrap(
    isolated_migration_schema: PostgresRoles,
) -> None:
    with _bootstrap_environment(isolated_migration_schema) as harness:
        harness.writer.open_governance_bootstrap(_open_command(harness))
        runtime_factory, runtime_engine = _runtime_factory(isolated_migration_schema)
        try:
            runtime_writer = harness.api.build_migration_bootstrap_writer(
                session_factory=runtime_factory,
                clock=lambda: AS_OF,
                dependency_port=harness.dependency_port,
                scope_owner_resolvers={"test-only.sf5a": harness.scope_resolver},
            )
            with pytest.raises(harness.api.GovernanceBootstrapDenied):
                runtime_writer.create_duty(
                    harness.api.CreateDutyCommand(
                        school_id=harness.facts.school_id,
                        duty_code="test_only.bootstrap.runtime_impersonation",
                        display_name="runtime 不得冒用 bootstrap",
                        description=None,
                        effective_from=AS_OF,
                        actor=_actor(harness, reason="runtime must not impersonate migration"),
                        idempotency_key="sf5a:bootstrap:runtime-impersonation",
                    )
                )
        finally:
            runtime_engine.dispose()

        assert _rows(
            harness.factory,
            """
            SELECT state, version FROM governance_bootstrap_states
            WHERE school_id = :school_id
            """,
            {"school_id": harness.facts.school_id},
        ) == [{"state": "open", "version": 1}]
        assert _scalar(harness.factory, "SELECT count(*) FROM governance_duties") == 0
        assert _scalar(harness.factory, "SELECT count(*) FROM governance_command_receipts") == 1
        assert _scalar(harness.factory, "SELECT count(*) FROM governance_audit_events") == 1


def test_closed_bootstrap_delete_reinsert_bypass_is_rejected_by_database_guard(
    isolated_migration_schema: PostgresRoles,
) -> None:
    with _bootstrap_environment(isolated_migration_schema) as harness:
        harness.writer.open_governance_bootstrap(_open_command(harness))
        harness.writer.close_governance_bootstrap(_close_command(harness))

        with harness.factory() as session:
            transaction = session.begin()
            try:
                with pytest.raises(DBAPIError) as rejected:
                    session.execute(
                        text(
                            """
                            WITH deleted AS (
                                DELETE FROM governance_bootstrap_states
                                WHERE school_id = :school_id
                                RETURNING school_id, authority_reference, opened_at
                            )
                            INSERT INTO governance_bootstrap_states (
                                school_id, authority_reference, state, version,
                                opened_at, closed_at
                            )
                            SELECT school_id, authority_reference, 'open', 1,
                                   opened_at, NULL
                            FROM deleted
                            """
                        ),
                        {"school_id": harness.facts.school_id},
                    )
                assert getattr(rejected.value.orig, "sqlstate", None) == "55000"
            finally:
                if transaction.is_active:
                    transaction.rollback()

        assert _rows(
            harness.second_factory,
            """
            SELECT state, version FROM governance_bootstrap_states
            WHERE school_id = :school_id
            """,
            {"school_id": harness.facts.school_id},
        ) == [{"state": "closed", "version": 2}]


def test_close_serializes_against_concurrent_write_and_forces_same_transaction_recheck(
    isolated_migration_schema: PostgresRoles,
) -> None:
    with _bootstrap_environment(isolated_migration_schema) as harness:
        harness.writer.open_governance_bootstrap(_open_command(harness))
        closing_writer = harness.api.build_migration_bootstrap_writer(
            session_factory=harness.second_factory,
            clock=lambda: AS_OF + timedelta(seconds=1),
            dependency_port=harness.dependency_port,
            scope_owner_resolvers={"test-only.sf5a": harness.scope_resolver},
        )
        close_updated = Event()
        release_close = Event()
        write_finished = Event()
        close_errors: list[BaseException] = []
        write_errors: list[BaseException] = []

        def pause_after_close_update(
            _connection: object,
            _cursor: object,
            statement: str,
            _parameters: object,
            _context: object,
            _executemany: bool,
        ) -> None:
            normalized = " ".join(statement.lower().split())
            if normalized.startswith("update governance_bootstrap_states"):
                close_updated.set()
                if not release_close.wait(timeout=10):
                    raise TimeoutError("test did not release the close transaction")

        def close_bootstrap() -> None:
            try:
                closing_writer.close_governance_bootstrap(_close_command(harness))
            except BaseException as exc:
                close_errors.append(exc)

        def write_duty() -> None:
            try:
                harness.writer.create_duty(
                    harness.api.CreateDutyCommand(
                        school_id=harness.facts.school_id,
                        duty_code="test_only.bootstrap.concurrent_write",
                        display_name="关闭竞争写测试职责",
                        description=None,
                        effective_from=AS_OF,
                        actor=_actor(harness, reason="race write against bootstrap close"),
                        idempotency_key="sf5a:bootstrap:concurrent-write",
                    )
                )
            except BaseException as exc:
                write_errors.append(exc)
            finally:
                write_finished.set()

        event.listen(harness.second_engine, "after_cursor_execute", pause_after_close_update)
        close_thread = Thread(target=close_bootstrap, daemon=True)
        write_thread = Thread(target=write_duty, daemon=True)
        try:
            close_thread.start()
            assert close_updated.wait(timeout=10)
            write_thread.start()
            assert write_finished.wait(timeout=1) is False
            release_close.set()
            close_thread.join(timeout=10)
            write_thread.join(timeout=10)
        finally:
            release_close.set()
            event.remove(harness.second_engine, "after_cursor_execute", pause_after_close_update)

        assert close_thread.is_alive() is False
        assert write_thread.is_alive() is False
        assert close_errors == []
        assert len(write_errors) == 1
        assert isinstance(write_errors[0], harness.api.GovernanceBootstrapClosed)
        assert (
            _scalar(
                harness.factory,
                """
            SELECT count(*) FROM governance_duties
            WHERE duty_code = 'test_only.bootstrap.concurrent_write'
            """,
            )
            == 0
        )
        assert (
            _scalar(
                harness.factory,
                """
            SELECT count(*) FROM governance_command_receipts
            WHERE command_name = 'governance.duty.create'
            """,
            )
            == 0
        )
        assert _rows(
            harness.factory,
            """
            SELECT state, version FROM governance_bootstrap_states
            WHERE school_id = :school_id
            """,
            {"school_id": harness.facts.school_id},
        ) == [{"state": "closed", "version": 2}]


def test_write_holds_bootstrap_lock_until_authority_receipt_and_audit_commit(
    isolated_migration_schema: PostgresRoles,
) -> None:
    with _bootstrap_environment(isolated_migration_schema) as harness:
        harness.writer.open_governance_bootstrap(_open_command(harness))
        closing_writer = harness.api.build_migration_bootstrap_writer(
            session_factory=harness.second_factory,
            clock=lambda: AS_OF + timedelta(seconds=1),
            dependency_port=harness.dependency_port,
            scope_owner_resolvers={"test-only.sf5a": harness.scope_resolver},
        )
        write_checked = Event()
        release_write = Event()
        close_finished = Event()
        write_errors: list[BaseException] = []
        close_errors: list[BaseException] = []

        def pause_after_write_locks_bootstrap(
            _connection: object,
            _cursor: object,
            statement: str,
            _parameters: object,
            _context: object,
            _executemany: bool,
        ) -> None:
            normalized = " ".join(statement.lower().split())
            if "from governance_bootstrap_states" in normalized and "for update" in normalized:
                write_checked.set()
                if not release_write.wait(timeout=10):
                    raise TimeoutError("test did not release the bootstrap-authorized write")

        def write_duty() -> None:
            try:
                harness.writer.create_duty(
                    harness.api.CreateDutyCommand(
                        school_id=harness.facts.school_id,
                        duty_code="test_only.bootstrap.writer_first",
                        display_name="writer-first 同事务锁测试职责",
                        description=None,
                        effective_from=AS_OF,
                        actor=_actor(
                            harness,
                            reason="hold bootstrap lock through authority commit",
                        ),
                        idempotency_key="sf5a:bootstrap:writer-first-duty",
                    )
                )
            except BaseException as exc:
                write_errors.append(exc)

        def close_bootstrap() -> None:
            try:
                closing_writer.close_governance_bootstrap(_close_command(harness))
            except BaseException as exc:
                close_errors.append(exc)
            finally:
                close_finished.set()

        event.listen(harness.engine, "after_cursor_execute", pause_after_write_locks_bootstrap)
        write_thread = Thread(target=write_duty, daemon=True)
        close_thread = Thread(target=close_bootstrap, daemon=True)
        try:
            write_thread.start()
            assert write_checked.wait(timeout=10)
            close_thread.start()
            assert close_finished.wait(timeout=1) is False
            release_write.set()
            write_thread.join(timeout=10)
            close_thread.join(timeout=10)
        finally:
            release_write.set()
            event.remove(harness.engine, "after_cursor_execute", pause_after_write_locks_bootstrap)

        assert write_thread.is_alive() is False
        assert close_thread.is_alive() is False
        assert write_errors == []
        assert close_errors == []
        assert (
            _scalar(
                harness.factory,
                "SELECT count(*) FROM governance_duties "
                "WHERE duty_code = 'test_only.bootstrap.writer_first'",
            )
            == 1
        )
        assert _scalar(harness.factory, "SELECT count(*) FROM governance_command_receipts") == 3
        assert _scalar(harness.factory, "SELECT count(*) FROM governance_audit_events") == 3
        assert _rows(
            harness.factory,
            """
            SELECT state, version FROM governance_bootstrap_states
            WHERE school_id = :school_id
            """,
            {"school_id": harness.facts.school_id},
        ) == [{"state": "closed", "version": 2}]
