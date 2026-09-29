"""Policy business dates survive canonical record revisions (in-memory SQL probes)."""

from datetime import UTC, datetime
from importlib.util import module_from_spec, spec_from_file_location
from io import StringIO
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.composition.school_fact_persistence import SqlAlchemySchoolFactPersistence as Writer
from app.platform.policies.models import SchoolPolicyVersion
from app.platform.school_facts.application import SchoolFactChangeRejected
from app.platform.school_facts.contracts import CanonicalFactChangeV2

_RESOLVE_POLICY_RECORD = Writer._source_root_id

SCHOOL = uuid4()
ROOT = uuid4()
SEP = datetime(2026, 9, 1, tzinfo=UTC)
NOW = datetime(2026, 9, 6, tzinfo=UTC)
OCT = datetime(2026, 10, 1, tzinfo=UTC)
NOV = datetime(2026, 11, 1, tzinfo=UTC)


class PolicySession:
    def __init__(self):
        self.connection = create_engine("sqlite://").connect()
        self.connection.execute(
            text(
                "CREATE TABLE school_policy_versions (id TEXT, school_id TEXT, "
                "policy_id TEXT, business_code TEXT, title TEXT, category_code TEXT, "
                "classification TEXT, "
                "publishing_organization_unit_id TEXT, published_at TEXT, effective_from TEXT, "
                "effective_to TEXT, content_text TEXT, version_note TEXT, "
                "supersedes_policy_version_id TEXT, "
                "version INTEGER, source_id TEXT, import_batch_id TEXT, valid_from "
                "TEXT, valid_to TEXT, revoked_at TEXT)"
            )
        )
        self.connection.execute(
            text(
                "CREATE TABLE school_fact_change_lineage (school_id TEXT, "
                "fact_kind TEXT, fact_source_key TEXT, target_id TEXT, target_version "
                "INTEGER, operation TEXT, unit_id TEXT)"
            )
        )

        self.connection.execute(
            text("CREATE TABLE master_data_import_units (school_id TEXT, id TEXT, payload TEXT)")
        )

    def execute(self, query, parameters=None) -> Any:
        from datetime import datetime
        from uuid import UUID

        values = {
            k: v.isoformat() if isinstance(v, datetime) else str(v) if isinstance(v, UUID) else v
            for k, v in (parameters or {}).items()
        }
        result = self.connection.execute(
            text(
                str(query)
                .replace(" FOR UPDATE", "")
                .replace("::timestamptz", "")
                .replace("CAST(:until AS timestamptz)", ":until")
            ),
            values,
        )
        return result

    def revisions(self):
        return (
            self.execute("SELECT * FROM school_policy_versions ORDER BY valid_from")
            .mappings()
            .all()
        )


def change(at, operation="upsert", version=0):
    return CanonicalFactChangeV2.model_validate(
        dict(
            fact_kind="policy_version",
            source_key="policy:v1",
            operation=operation,
            expected_version=version,
            effective_at=at,
            origin=dict(sheet_code="policy", row_number=2),
            content=dict(
                policy_source_key="policy",
                business_code="policy:one",
                display_name="Policy",
                category_code="synthetic",
                classification="internal",
                published_at=SEP,
                effective_from=OCT,
                effective_to=NOV,
                content_text="正文" * 50000,
                version_note="original",
            )
            if operation == "upsert"
            else None,
        )
    )


def revise(db, at, current=None, version=0, note="revision"):
    item = change(at, version=version)
    assert item.content is not None
    content = item.content.model_dump()
    content["version_note"] = note
    target, number = Writer._upsert_policy_version(
        session=cast(Session, db),
        school_id=SCHOOL,
        batch_id=uuid4(),
        source_id=uuid4(),
        change=item,
        content=content,
        current_id=current,
        current_version=version,
    )
    import json

    unit = uuid4()
    db.execute(
        "INSERT INTO master_data_import_units VALUES (:school,:id,:payload)",
        dict(school=SCHOOL, id=unit, payload=json.dumps({"effective_at": at.isoformat()})),
    )
    db.execute(
        "INSERT INTO school_fact_change_lineage VALUES "
        "(:school,'policy_version','policy:v1',:id,:v,'upsert',:unit)",
        dict(school=SCHOOL, id=target, v=number, unit=unit),
    )
    return target, number


@pytest.fixture
def db(monkeypatch):
    monkeypatch.setattr(Writer, "_source_root_id", staticmethod(lambda *a, **kw: ROOT))
    return PolicySession()


def test_future_business_note_revision_preserves_dates_fulltext_and_record_history(db):
    old, v = revise(db, SEP)
    revise(db, NOW, old, v)
    before, after = db.revisions()
    assert before["valid_from"] == SEP.isoformat()
    assert before["valid_to"] == NOW.isoformat()
    assert after["valid_from"] == NOW.isoformat() and after["valid_to"] is None
    for row in (before, after):
        assert row["effective_from"] == OCT.isoformat()
        assert row["effective_to"] == NOV.isoformat()
        assert row["published_at"] == SEP.isoformat()
        assert row["content_text"] == "正文" * 50000


def test_backdated_revision_splits_predecessor_without_overwriting_future_record(db):
    old, v = revise(db, SEP)
    future, v = revise(db, OCT, old, v)
    revise(db, NOW, future, v)
    rows = db.revisions()
    assert [(r["valid_from"], r["valid_to"]) for r in rows] == [
        (SEP.isoformat(), NOW.isoformat()),
        (NOW.isoformat(), OCT.isoformat()),
        (OCT.isoformat(), None),
    ]


def test_duplicate_record_time_is_rejected_before_closing_history(db):
    old, v = revise(db, SEP)
    with pytest.raises(SchoolFactChangeRejected):
        revise(db, SEP, old, v)
    assert len(db.revisions()) == 1


def test_policy_record_columns_are_independent_of_business_window():
    table = SchoolPolicyVersion.__table__
    assert table.c.valid_from.computed is None
    assert table.c.valid_to.computed is None


def test_revision25_offline_preserves_official_dates_and_narrows_writer_update():
    path = Path("alembic/versions/20260906_25_school_policy_revision_period.py")
    assert path.exists(), "Requires forward revision25; historical migrations must stay immutable"
    spec = spec_from_file_location("policy_revision_migration", path)
    assert spec and spec.loader
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.revision == "20260906_25" and module.down_revision == "20260906_24"
    output = StringIO()
    context = MigrationContext.configure(
        dialect_name="postgresql", opts={"as_sql": True, "output_buffer": output}
    )
    with Operations.context(context):
        module.upgrade()
    sql = output.getvalue()
    assert "DROP EXPRESSION" in sql and "payload ->> 'effective_at'" in sql
    assert "SET effective_from" not in sql and "SET effective_to" not in sql
    assert "EXCLUDE USING gist" in sql and "GREATEST(valid_from, effective_from)" in sql
    assert "LEAST(valid_to, effective_to, revoked_at)" in sql
    assert "GRANT UPDATE (valid_to, revoked_at)" in sql
    assert "REVOKE UPDATE (effective_to)" in sql
    with pytest.raises(RuntimeError, match="backup"):
        module.downgrade()


@pytest.mark.parametrize("operation", ["end", "revoke"])
def test_record_termination_preserves_business_window_even_before_business_start(db, operation):
    old, v = revise(db, SEP)
    Writer._end_or_revoke(
        cast(Session, db), change=change(NOW, operation, v), target_id=old, target_version=v
    )
    row = db.revisions()[0]
    assert row["valid_to"] == NOW.isoformat()
    assert row["effective_from"] == OCT.isoformat() and row["effective_to"] == NOV.isoformat()
    assert row["revoked_at"] == (NOW.isoformat() if operation == "revoke" else None)


@pytest.mark.parametrize("operation", ["end", "revoke"])
def test_terminal_cannot_leave_a_previously_scheduled_future_revision_active(db, operation):
    old, v = revise(db, SEP)
    future, v = revise(db, OCT, old, v)
    backdated, v = revise(db, NOW, future, v)
    at = datetime(2026, 9, 10, tzinfo=UTC)
    with pytest.raises(SchoolFactChangeRejected):
        Writer._end_or_revoke(
            cast(Session, db),
            change=change(at, operation, v),
            target_id=backdated,
            target_version=v,
        )
    assert db.revisions()[-1]["valid_to"] is None


@pytest.mark.parametrize("operation", ["end", "revoke"])
@pytest.mark.parametrize("at", [SEP, NOV])
def test_terminal_rejects_nonpositive_record_or_extension_past_official_expiry(db, operation, at):
    old, v = revise(db, SEP)
    with pytest.raises(SchoolFactChangeRejected):
        Writer._end_or_revoke(
            cast(Session, db), change=change(at, operation, v), target_id=old, target_version=v
        )


def test_schema25_contract_keeps_historical_mappings_and_rejects_schema24():
    from unittest.mock import MagicMock

    from app.composition.school_fact_directory_services import create_directory_read_components
    from app.db.schema_contract import schema_contract_head

    assert schema_contract_head("school-facts-f2-v25") == "20260906_25"
    assert schema_contract_head("school-facts-f2-v24") == "20260906_24"
    with pytest.raises(ValueError, match="exact schema25"):
        create_directory_read_components(
            sessions=MagicMock(),
            settings=MagicMock(schema_contract="school-facts-f2-v24"),
            configuration=MagicMock(),
            legacy_scope_id=MagicMock(),
            reference_key=b"synthetic",
        )


@pytest.mark.parametrize(
    "at,until,accepted",
    [(SEP, NOV, False), (OCT, NOV, True), (OCT, None, False), (NOV, None, False)],
)
def test_quality_reference_requires_whole_business_and_record_window(db, at, until, accepted):
    old, v = revise(db, SEP)

    def call():
        return Writer._f2_content_at(
            cast(Session, db),
            SCHOOL,
            "policy_version",
            "policy:v1",
            at,
            until=until,
            require_whole_window=True,
        )

    if accepted:
        assert call() == (old, v)
    else:
        with pytest.raises(SchoolFactChangeRejected):
            call()


@pytest.mark.parametrize("bound", ["valid_to", "revoked_at"])
def test_quality_reference_rejects_window_crossing_record_or_revocation_boundary(db, bound):
    old, v = revise(db, SEP)
    end = datetime(2026, 10, 10, tzinfo=UTC)
    db.execute(f"UPDATE school_policy_versions SET {bound}=:end", dict(end=end))
    with pytest.raises(SchoolFactChangeRejected):
        Writer._f2_content_at(
            cast(Session, db),
            SCHOOL,
            "policy_version",
            "policy:v1",
            OCT,
            until=NOV,
            require_whole_window=True,
        )


def test_supersedes_binds_revision_at_canonical_time_not_latest_inserted(db, monkeypatch):
    original = _RESOLVE_POLICY_RECORD
    old, v = revise(db, SEP)
    future, v = revise(db, OCT, old, v)
    backdated, v = revise(db, NOW, future, v)
    assert (
        original(cast(Session, db), SCHOOL, "policy_version", "policy:v1", effective_at=SEP) == old
    )
    assert (
        original(cast(Session, db), SCHOOL, "policy_version", "policy:v1", effective_at=NOW)
        == backdated
    )
    assert (
        original(cast(Session, db), SCHOOL, "policy_version", "policy:v1", effective_at=NOV)
        == future
    )


def migration25():
    spec = spec_from_file_location(
        "policy_revision_migration",
        Path("alembic/versions/20260906_25_school_policy_revision_period.py"),
    )
    assert spec and spec.loader
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def sqlite_migration_sql(sql):
    return sql.replace("public.", "").replace("::timestamptz", "")


@pytest.mark.parametrize("operation", ["end", "revoke"])
def test_backfill_formal_terminal_row_uses_upsert_origin_even_if_row_version_advanced(
    db, operation
):
    import json

    old, v = revise(db, SEP)
    terminal = datetime(2026, 10, 15, tzinfo=UTC)
    unit = uuid4()
    db.execute(
        "INSERT INTO master_data_import_units VALUES (:school,:id,:payload)",
        dict(school=SCHOOL, id=unit, payload=json.dumps({"effective_at": terminal.isoformat()})),
    )
    db.execute(
        "INSERT INTO school_fact_change_lineage VALUES "
        "(:school,'policy_version','policy:v1',:id,2,:op,:unit)",
        dict(school=SCHOOL, id=old, op=operation, unit=unit),
    )
    # Legacy schema24 stored generated valid_from/to and may have terminal CAS version.
    db.execute(
        "UPDATE school_policy_versions SET version=2,valid_from=effective_from, "
        "valid_to=:terminal,effective_to=:end,revoked_at=:revoked",
        dict(
            terminal=terminal,
            end=terminal if operation == "end" else NOV,
            revoked=terminal if operation == "revoke" else None,
        ),
    )
    connection = db.connection.connection.driver_connection
    connection.create_function(
        "least", -1, lambda *v: min((x for x in v if x is not None), default=None)
    )
    module = migration25()
    assert not db.execute(sqlite_migration_sql(module._AMBIGUOUS_ORIGIN)).all()
    db.execute(sqlite_migration_sql(module._BACKFILL))
    row = db.revisions()[0]
    assert row["valid_from"] == SEP.isoformat() and row["valid_to"] == terminal.isoformat()
    assert row["effective_from"] == OCT.isoformat()
    assert row["effective_to"] == (terminal if operation == "end" else NOV).isoformat()
    assert row["content_text"] == "正文" * 50000
    assert (
        _RESOLVE_POLICY_RECORD(
            cast(Session, db), SCHOOL, "policy_version", "policy:v1", effective_at=OCT
        )
        == old
    )
    assert Writer._f2_content_at(
        cast(Session, db),
        SCHOOL,
        "policy_version",
        "policy:v1",
        OCT,
        until=terminal,
        require_whole_window=True,
    ) == (old, 1)


def test_backfill_rejects_missing_or_ambiguous_formal_origin(db):
    old, v = revise(db, SEP)
    module = migration25()
    db.execute("INSERT INTO school_fact_change_lineage SELECT * FROM school_fact_change_lineage")
    assert len(db.execute(sqlite_migration_sql(module._AMBIGUOUS_ORIGIN)).all()) == 1
    db.execute("UPDATE school_fact_change_lineage SET operation='end'")
    assert len(db.execute(sqlite_migration_sql(module._AMBIGUOUS_ORIGIN)).all()) == 1


def test_backfill_reconstructs_prebusiness_and_backdated_periods_by_canonical_time(db):
    old, v = revise(db, SEP)
    future, v = revise(db, OCT, old, v)
    revise(db, NOW, future, v)
    db.execute("UPDATE school_policy_versions SET valid_from=effective_from,valid_to=effective_to")
    connection = db.connection.connection.driver_connection
    connection.create_function(
        "least", -1, lambda *v: min((x for x in v if x is not None), default=None)
    )
    db.execute(sqlite_migration_sql(migration25()._BACKFILL))
    assert [(r["valid_from"], r["valid_to"]) for r in db.revisions()] == [
        (SEP.isoformat(), NOW.isoformat()),
        (NOW.isoformat(), OCT.isoformat()),
        (OCT.isoformat(), None),
    ]


@pytest.mark.parametrize(
    "record_from,record_to,business_from,business_to,revoked,expected",
    [
        ("2026-09-01", "2026-09-06", "2026-10-01", None, None, ("2026-10-01", "2026-10-01")),
        ("2026-09-06", None, "2026-10-01", "2026-11-01", None, ("2026-10-01", "2026-11-01")),
        ("2026-09-01", None, "2026-09-01", "2026-10-01", None, ("2026-09-01", "2026-10-01")),
        ("2026-09-01", None, "2026-09-01", None, "2026-09-15", ("2026-09-01", "2026-09-15")),
    ],
)
def test_exact_exclusion_expression_represents_intersection_and_empty_or_adjacent_ranges(
    db, record_from, record_to, business_from, business_to, revoked, expected
):
    import json

    connection = db.connection.connection.driver_connection
    connection.create_function(
        "least", -1, lambda *v: min((x for x in v if x is not None), default=None)
    )
    connection.create_function(
        "greatest", -1, lambda *v: max((x for x in v if x is not None), default=None)
    )
    connection.create_function("tstzrange", 3, lambda start, end, bound: json.dumps([start, end]))
    db.execute(
        "INSERT INTO school_policy_versions "
        "(valid_from,valid_to,effective_from,effective_to,revoked_at) "
        "VALUES (:rf,:rt,:bf,:bt,:rev)",
        dict(rf=record_from, rt=record_to, bf=business_from, bt=business_to, rev=revoked),
    )
    actual = db.execute(
        "SELECT "
        + sqlite_migration_sql(migration25()._APPLICABILITY_RANGE)
        + " FROM school_policy_versions"
    ).scalar_one()
    assert tuple(json.loads(actual)) == expected
