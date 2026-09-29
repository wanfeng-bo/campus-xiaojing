from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import cast
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine

from app.composition import school_fact_reader as reader_boundary

NOW = datetime(2026, 8, 24, 8, 0, tzinfo=UTC)
SCHOOL_ID = uuid4()
ANCHOR_ID = uuid4()


def _settings(**overrides: object) -> reader_boundary.SchoolFactReaderSettings:
    values: dict[str, object] = {
        "database_host": "127.0.0.1",
        "database_port": 5432,
        "database_name": "campus_ai_test",
        "database_user": "campus_ai_school_fact_reader",
        "database_password": "reader-password-is-independent-0001",
        "expected_anchor_id": ANCHOR_ID,
        "expected_school_id": SCHOOL_ID,
        "expected_system_identifier": "pg16-system-identifier",
        "cursor_hmac_key": "reader-cursor-hmac-key-independent",
        "cursor_key_version": 3,
        "cursor_ttl_seconds": 120,
    }
    values.update(overrides)
    return reader_boundary.SchoolFactReaderSettings.model_validate(values)


def _facts(**overrides: object) -> reader_boundary.SchoolFactReaderConnectionFacts:
    values: dict[str, object] = {
        "current_user": reader_boundary.EXPECTED_SCHOOL_FACT_READER_ROLE,
        "session_user": reader_boundary.EXPECTED_SCHOOL_FACT_READER_ROLE,
        "is_superuser": False,
        "default_transaction_read_only": True,
        "bypasses_rls": False,
        "has_privileged_membership": False,
        "alembic_head": reader_boundary.REQUIRED_SF5B_ALEMBIC_HEAD,
        "anchored_school_id": SCHOOL_ID,
    }
    values.update(overrides)
    return reader_boundary.SchoolFactReaderConnectionFacts(**values)  # type: ignore[arg-type]


class _CheckoutCursor:
    def __init__(self, connection: _CheckoutConnection) -> None:
        self.connection = connection
        self.closed = False

    def execute(self, statement: str, parameters: object | None = None) -> None:
        self.connection.executions.append((statement, parameters))

    def fetchone(self) -> tuple[object, ...] | None:
        heads = self.connection.alembic_heads
        return (
            self.connection.current_user,
            self.connection.session_user,
            self.connection.is_superuser,
            self.connection.bypasses_rls,
            self.connection.has_privileged_membership,
            self.connection.default_transaction_read_only,
            len(heads),
            min(heads) if heads else None,
            self.connection.anchored_school_id if len(heads) == 1 else None,
        )

    def close(self) -> None:
        self.closed = True


class _CheckoutConnection:
    def __init__(
        self,
        *,
        current_user: str = reader_boundary.EXPECTED_SCHOOL_FACT_READER_ROLE,
        session_user: str = reader_boundary.EXPECTED_SCHOOL_FACT_READER_ROLE,
        is_superuser: bool = False,
        bypasses_rls: bool = False,
        has_privileged_membership: bool = False,
        default_transaction_read_only: bool = True,
        alembic_heads: tuple[str, ...] = (reader_boundary.REQUIRED_SF5B_ALEMBIC_HEAD,),
        anchored_school_id: object = SCHOOL_ID,
    ) -> None:
        self.current_user = current_user
        self.session_user = session_user
        self.is_superuser = is_superuser
        self.bypasses_rls = bypasses_rls
        self.has_privileged_membership = has_privileged_membership
        self.default_transaction_read_only = default_transaction_read_only
        self.alembic_heads = alembic_heads
        self.anchored_school_id = anchored_school_id
        self.executions: list[tuple[str, object | None]] = []
        self.cursors: list[_CheckoutCursor] = []
        self.rollbacks = 0

    def cursor(self) -> _CheckoutCursor:
        cursor = _CheckoutCursor(self)
        self.cursors.append(cursor)
        return cursor

    def rollback(self) -> None:
        self.rollbacks += 1


class _CheckoutRecord:
    def __init__(self) -> None:
        self.invalidations: list[tuple[object, bool]] = []

    def invalidate(self, error: object, *, soft: bool) -> None:
        self.invalidations.append((error, soft))


def _capture_checkout_listener(
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[[object, object, object], None]:
    engine = SimpleNamespace()
    listeners: list[tuple[object, str, object, dict[str, object]]] = []

    def _listen(
        target: object,
        identifier: str,
        listener: object,
        **kwargs: object,
    ) -> None:
        listeners.append((target, identifier, listener, kwargs))

    monkeypatch.setattr(reader_boundary, "create_engine", lambda *_args, **_kwargs: engine)
    monkeypatch.setattr(
        reader_boundary,
        "event",
        SimpleNamespace(listen=_listen),
        raising=False,
    )

    assert reader_boundary.build_school_fact_reader_engine(_settings()) is engine
    assert len(listeners) == 1
    assert listeners[0][0] is engine
    assert listeners[0][1] == "checkout"
    return cast(Callable[[object, object, object], None], listeners[0][2])


def test_reader_engine_revalidates_role_read_only_head_and_anchor_on_every_checkout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    listener = _capture_checkout_listener(monkeypatch)
    connection = _CheckoutConnection()
    record = _CheckoutRecord()

    listener(connection, record, object())
    listener(connection, record, object())

    assert len(connection.executions) == 2
    assert connection.rollbacks == 2
    assert len(connection.cursors) == 2
    assert all(cursor.closed for cursor in connection.cursors)
    assert record.invalidations == []
    assert all(
        parameters == (ANCHOR_ID, SCHOOL_ID, "pg16-system-identifier")
        for _statement, parameters in connection.executions
    )


@pytest.mark.parametrize(
    ("change", "value"),
    (
        ("current_user", "campus_ai_runtime"),
        ("session_user", "campus_ai_runtime"),
        ("is_superuser", True),
        ("bypasses_rls", True),
        ("has_privileged_membership", True),
        ("default_transaction_read_only", False),
        ("alembic_heads", ("20260823_19",)),
        ("alembic_heads", ("20260823_19", "20260824_20")),
        ("anchored_school_id", uuid4()),
    ),
)
def test_reader_checkout_hard_invalidates_any_connection_that_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    change: str,
    value: object,
) -> None:
    listener = _capture_checkout_listener(monkeypatch)
    connection = _CheckoutConnection(**{change: value})  # type: ignore[arg-type]
    record = _CheckoutRecord()

    with pytest.raises(reader_boundary.ReaderStartupRejected):
        listener(connection, record, object())

    assert connection.rollbacks == 1
    assert len(record.invalidations) == 1
    assert record.invalidations[0][1] is False


def test_reader_settings_own_an_independent_secret_dsn_and_bounded_pool() -> None:
    settings = _settings()

    assert settings.database_url.username == "campus_ai_school_fact_reader"
    assert settings.database_url.password == "reader-password-is-independent-0001"
    assert settings.model_config.get("env_prefix") == "SCHOOL_FACT_READER_"
    assert {
        "writer_database_password",
        "postgres_password",
        "migration_password",
    }.isdisjoint(settings.__class__.model_fields)

    with pytest.raises(ValidationError):
        _settings(database_user="campus_ai_runtime")
    with pytest.raises(ValidationError):
        _settings(database_password="short")


def test_reader_rejects_database_cursor_secret_reuse_without_echoing_secret() -> None:
    reused = "never-log-reader-cross-purpose-secret-0001"

    with pytest.raises(ValidationError) as raised:
        _settings(
            database_password=reused,
            cursor_hmac_key=reused,
        )

    assert reused not in str(raised.value)
    assert reused not in repr(raised.value)


@pytest.mark.parametrize(
    ("change", "value"),
    (
        ("current_user", "campus_ai_runtime"),
        ("session_user", "campus_ai_runtime"),
        ("alembic_head", "20260823_19"),
        ("anchored_school_id", uuid4()),
        ("is_superuser", True),
        ("bypasses_rls", True),
        ("has_privileged_membership", True),
        ("default_transaction_read_only", False),
    ),
)
def test_reader_startup_gate_rejects_wrong_role_head_anchor_or_root_capability(
    change: str,
    value: object,
) -> None:
    with pytest.raises(reader_boundary.ReaderStartupRejected):
        reader_boundary.require_school_fact_reader_connection(
            _facts(**{change: value}),
            settings=_settings(),
        )


def test_production_composition_validates_startup_and_keeps_registry_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_engine("sqlite://")
    seen: list[object] = []
    monkeypatch.setattr(
        reader_boundary,
        "build_school_fact_reader_engine",
        lambda settings: engine,
    )
    monkeypatch.setattr(
        reader_boundary,
        "verify_school_fact_reader_database",
        lambda candidate, *, settings: seen.extend((candidate, settings)) or _facts(),
    )

    runtime = reader_boundary.build_production_secure_reader(
        settings=_settings(),
        thin_service=SimpleNamespace(),
        clock=lambda: NOW,
    )

    assert seen == [engine, _settings()]
    assert runtime.engine is engine
    assert runtime.reader._registry.operation_codes == ()  # pyright: ignore[reportPrivateUsage]
    engine.dispose()
