"""Execute checkout SQL in memory; PostgreSQL catalog/functions are explicit substitutes.

This proves SQL control flow and per-checkout rejection, not PostgreSQL ACL behavior.
"""

import re
import sqlite3
from uuid import UUID

import pytest
from pydantic import SecretStr

from app.composition import school_fact_reader as boundary

SCHOOL = UUID(int=125)
ANCHOR = UUID(int=225)
ROLE = boundary.EXPECTED_SCHOOL_FACT_READER_ROLE
HEAD = "20260906_25"


def settings():
    return boundary.SchoolFactReaderSettings(
        schema_contract="school-facts-f2-v25",
        database_host="127.0.0.1",
        database_name="synthetic_checkout",
        database_password=SecretStr("independent-synthetic-reader-password"),
        expected_anchor_id=ANCHOR,
        expected_school_id=SCHOOL,
        expected_system_identifier="synthetic-cluster-identifier",
        cursor_hmac_key=SecretStr("independent-synthetic-reader-cursor-key"),
    )


class Record:
    def __init__(self, connection):
        self.connection = connection
        self.invalidations = []
        self.fail = False

    def invalidate(self, error, *, soft):
        self.connection.events.append("invalidate")
        self.invalidations.append((error, soft))
        if self.fail:
            raise RuntimeError("synthetic invalidation transport failure")


class Cursor:
    def __init__(self, owner):
        self.owner = owner
        self.raw = owner.raw.cursor()

    def execute(self, statement, parameters=None):
        self.owner.executions.append((statement, parameters))
        if self.owner.execute_failure:
            raise RuntimeError("synthetic SQL transport failure")
        # Substitute PostgreSQL scalar syntax only; retain CTE/aggregate/CASE SQL.
        sql = statement.replace("pg_catalog.pg_has_role", "pg_has_role")
        sql = sql.replace("app.assert_school_database_anchor", "assert_school_database_anchor")
        sql = re.sub(r"\bcurrent_user\b", "current_reader_user()", sql)
        sql = re.sub(r"\bsession_user\b", "session_reader_user()", sql)
        sql = re.sub(r"CAST\(%s AS (uuid|text)\)", "?", sql, flags=re.I)
        self.raw.execute(sql, tuple(str(v) for v in (parameters or ())))

    def fetchone(self):
        value = self.raw.fetchone()
        return value[:2] if value is not None and self.owner.malformed_result else value

    def fetchall(self):
        return self.raw.fetchall()

    def close(self):
        self.owner.events.append("close")
        self.raw.close()
        if self.owner.close_failure:
            raise RuntimeError("synthetic cursor close failure")


class Connection:
    def __init__(self):
        self.raw = sqlite3.connect(":memory:")
        self.raw.execute("ATTACH DATABASE ':memory:' AS pg_catalog")
        self.raw.execute(
            "CREATE TABLE pg_catalog.pg_roles "
            "(oid INTEGER,rolname TEXT,rolsuper INTEGER,rolbypassrls INTEGER)"
        )
        self.raw.execute("CREATE TABLE alembic_version (version_num TEXT)")
        names = [ROLE, *boundary._PRIVILEGED_READER_MEMBERSHIP_ROLES]
        self.raw.executemany(
            "INSERT INTO pg_catalog.pg_roles VALUES (?,?,0,0)", enumerate(names, 1)
        )
        self.raw.execute("INSERT INTO alembic_version VALUES (?)", (HEAD,))
        self.raw.commit()
        self.current_user = ROLE
        self.session_user = ROLE
        self.privileged_membership = False
        self.read_only = True
        self.anchor = str(SCHOOL)
        self.anchor_failure = False
        self.cursor_failure = False
        self.execute_failure = False
        self.close_failure = False
        self.rollback_failure = False
        self.malformed_result = False
        self.executions = []
        self.events = []
        self.anchor_calls = []
        self.raw.create_function("current_reader_user", 0, lambda: self.current_user)
        self.raw.create_function("session_reader_user", 0, lambda: self.session_user)
        self.raw.create_function("current_setting", 1, lambda _: "on" if self.read_only else "off")
        self.raw.create_function("pg_has_role", 3, lambda *args: self.privileged_membership)
        self.raw.create_function("assert_school_database_anchor", 3, self.assert_anchor)

    def assert_anchor(self, anchor, school, cluster):
        self.anchor_calls.append((anchor, school, cluster))
        if self.anchor_failure:
            raise RuntimeError("synthetic anchor mismatch")
        return self.anchor

    def cursor(self):
        self.events.append("cursor")
        if self.cursor_failure:
            raise RuntimeError("synthetic cursor creation failure")
        return Cursor(self)

    def rollback(self):
        self.events.append("rollback")
        self.raw.rollback()
        if self.rollback_failure:
            raise RuntimeError("synthetic rollback failure")

    def heads(self, values):
        self.raw.execute("DELETE FROM alembic_version")
        self.raw.executemany("INSERT INTO alembic_version VALUES (?)", [(v,) for v in values])
        self.raw.commit()


@pytest.fixture
def connection():
    db = Connection()
    yield db
    db.raw.close()


def test_one_statement_per_checkout_returns_all_current_security_facts(connection):
    facts = boundary._read_school_fact_reader_checkout(connection, settings=settings())
    boundary.require_school_fact_reader_connection(facts, settings=settings())
    assert len(connection.executions) == 1
    assert connection.executions[0][1] == (ANCHOR, SCHOOL, "synthetic-cluster-identifier")
    assert connection.anchor_calls == [(str(ANCHOR), str(SCHOOL), "synthetic-cluster-identifier")]
    assert connection.events == ["cursor", "close", "rollback"]
    assert facts.alembic_head == HEAD and facts.anchored_school_id == SCHOOL


@pytest.mark.parametrize("heads", [[], [HEAD, HEAD], [HEAD, "other-head"]])
def test_nonunique_head_rejected_before_anchor_can_mask_it(connection, heads):
    connection.heads(heads)
    connection.anchor_failure = True
    record = Record(connection)
    with pytest.raises(boundary.ReaderStartupRejected, match="head is not unique"):
        boundary._reader_checkout_listener(settings())(connection, record, object())
    assert connection.anchor_calls == []
    assert connection.events[-3:] == ["close", "rollback", "invalidate"]
    assert len(record.invalidations) == 1 and record.invalidations[0][1] is False


@pytest.mark.parametrize(
    "changed",
    [
        "current_user",
        "session_user",
        "superuser",
        "bypassrls",
        "membership",
        "read_only",
        "wrong_head",
        "zero_head",
        "multi_head",
        "anchor",
        "anchor_failure",
    ],
)
def test_same_connection_is_revalidated_after_each_successful_pool_checkout(connection, changed):
    listener = boundary._reader_checkout_listener(settings())
    record = Record(connection)
    listener(connection, record, object())
    if changed in {"current_user", "session_user"}:
        setattr(connection, changed, "campus_ai_runtime")
    elif changed in {"superuser", "bypassrls"}:
        column = "rolsuper" if changed == "superuser" else "rolbypassrls"
        connection.raw.execute(
            f"UPDATE pg_catalog.pg_roles SET {column}=1 WHERE rolname=?", (ROLE,)
        )
        connection.raw.commit()
    elif changed == "membership":
        connection.privileged_membership = True
    elif changed == "read_only":
        connection.read_only = False
    elif changed == "wrong_head":
        connection.heads(["20260906_24"])
    elif changed == "zero_head":
        connection.heads([])
    elif changed == "multi_head":
        connection.heads([HEAD, "20260906_24"])
    elif changed == "anchor":
        connection.anchor = str(UUID(int=999))
    elif changed == "anchor_failure":
        connection.anchor_failure = True
    with pytest.raises(boundary.ReaderStartupRejected):
        listener(connection, record, object())
    assert connection.events.count("rollback") == 2
    assert len(connection.executions) == 2
    assert len(record.invalidations) == 1 and record.invalidations[0][1] is False


@pytest.mark.parametrize(
    "fault",
    [
        "cursor_failure",
        "execute_failure",
        "anchor_failure",
        "close_failure",
        "rollback_failure",
        "malformed_result",
    ],
)
def test_every_operational_failure_attempts_rollback_and_hard_invalidation(connection, fault):
    setattr(connection, fault, True)
    record = Record(connection)
    with pytest.raises(boundary.ReaderStartupRejected):
        boundary._reader_checkout_listener(settings())(connection, record, object())
    assert connection.events[-2:] == ["rollback", "invalidate"]
    assert len(record.invalidations) == 1 and record.invalidations[0][1] is False


@pytest.mark.parametrize("anchor", [None, "not-a-uuid"])
def test_missing_or_invalid_anchor_never_yields_a_connection(connection, anchor):
    connection.anchor = anchor
    record = Record(connection)
    with pytest.raises(boundary.ReaderStartupRejected):
        boundary._reader_checkout_listener(settings())(connection, record, object())
    assert connection.events[-2:] == ["rollback", "invalidate"]


def test_failed_hard_invalidation_does_not_hide_original_rejection(connection):
    connection.heads([])
    record = Record(connection)
    record.fail = True
    with pytest.raises(boundary.ReaderStartupRejected, match="head is not unique"):
        boundary._reader_checkout_listener(settings())(connection, record, object())
    assert connection.events[-2:] == ["rollback", "invalidate"]


def test_missing_role_facts_skip_anchor_and_still_invalidate(connection):
    connection.current_user = "missing_catalog_role"
    connection.anchor_failure = True
    record = Record(connection)
    with pytest.raises(boundary.ReaderStartupRejected, match="Unable to verify"):
        boundary._reader_checkout_listener(settings())(connection, record, object())
    assert connection.anchor_calls == []
    assert connection.events[-3:] == ["close", "rollback", "invalidate"]


def test_single_wrong_head_still_validates_anchor_before_final_gate(connection):
    connection.heads(["20260906_24"])
    connection.anchor_failure = True
    record = Record(connection)
    with pytest.raises(boundary.ReaderStartupRejected, match="Unable to verify"):
        boundary._reader_checkout_listener(settings())(connection, record, object())
    assert len(connection.anchor_calls) == 1
    assert connection.events[-3:] == ["close", "rollback", "invalidate"]
