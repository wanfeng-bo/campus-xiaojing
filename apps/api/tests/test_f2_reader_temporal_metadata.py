"""Exercise projected time expressions without opening any external database."""

import sqlite3
from datetime import UTC, datetime
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.dialects import sqlite

from app.platform import contracts as actors
from app.platform.authorization.f2_reader_compiler import compile_f2
from app.platform.authorization.secure_reader_registry import f2_operation_registry
from app.platform.authorization.thin_contracts import ScopePlan
from app.platform.contracts import AssignmentId, PersonId, SchoolId
from app.platform.school_facts.f2_policy import F2FactScope


def relation(kind, purpose="read"):
    school = UUID(int=1)
    actor = actors._issue_staff_actor_context(
        account_user_id=7,
        school_id=SchoolId(school),
        person_id=PersonId(UUID(int=2)),
        assignment_id=AssignmentId(UUID(int=3)),
        assignment_version=1,
        authorization_version=1,
    )
    scope = F2FactScope(school, kind, ("allowed.object",))
    return compile_f2(
        operation=f2_operation_registry().require(f"school_facts.f2.{kind}.{purpose}"),
        actor=actor,
        scope_plan=ScopePlan(UUID(int=4), "school_facts.f2", scope.reference, 1),
        as_of=datetime(2026, 9, 6, tzinfo=UTC),
        after_id=None,
        page_size=2,
        selection=None,
    )


@pytest.mark.parametrize(
    "row_count,after,page_size", [(0, None, 2), (1, None, 2), (4, None, 1), (4, 2, 1), (4, 9, 2)]
)
def test_counted_metadata_reads_one_relation_and_keeps_total_cursor_empty_and_lookahead(
    row_count, after, page_size
):
    from sqlalchemy import Column, Integer, MetaData, Table, Uuid, create_engine, event, func
    from sqlalchemy.orm import Session

    from app.platform.authorization import f2_reader_compiler as compiler
    from app.platform.authorization.secure_reader_adapters import _SqlAlchemyReadTransaction
    from app.platform.authorization.secure_reader_compiler import CompiledReadRelation

    builder = getattr(compiler, "_counted_metadata", None)
    assert callable(builder), "Exact metadata count and page still scan separately"
    metadata = MetaData()
    facts = Table(
        "visible_facts", metadata,
        Column("row_id", Uuid), Column("version", Integer), Column("allowed", Integer),
    )
    engine = create_engine("sqlite://")
    evaluations = []

    @event.listens_for(engine, "connect")
    def count_evaluations(connection, _):
        connection.create_function(
            "check_allowed", 1, lambda value: (evaluations.append(value), value)[1]
        )

    metadata.create_all(engine)
    after_id = None if after is None else UUID(int=after)
    base = select(facts.c.row_id, facts.c.version).where(func.check_allowed(facts.c.allowed) == 1)
    page = base if after_id is None else base.where(facts.c.row_id > after_id)
    page = page.order_by(facts.c.row_id).limit(page_size + 1)
    compiled = CompiledReadRelation(
        "test", datetime(2026, 9, 6, tzinfo=UTC), page_size, (),
        select(func.count()).select_from(base.subquery()), page, page,
        builder(base, after_id=after_id, page_size=page_size),
    )
    calls = []
    with Session(engine) as session:
        session.execute(facts.insert(), [
            {"row_id": UUID(int=i), "version": None if i == 1 else i, "allowed": 1}
            for i in range(1, row_count + 1)
        ] + [{"row_id": UUID(int=99), "version": 99, "allowed": 0}])
        transaction = _SqlAlchemyReadTransaction(session)
        expected = transaction.count(compiled), transaction.metadata(compiled)
        evaluations.clear()
        event.listen(engine, "before_cursor_execute", lambda *args: calls.append(args[2]))
        assert transaction.count_metadata(compiled) == expected
    assert expected[0] == row_count
    assert len(calls) == 1
    assert calls[0].count("FROM visible_facts") == 1
    assert "MATERIALIZED" in calls[0]
    assert len(evaluations) == row_count + 1  # Authorized relation evaluated only once.


@pytest.mark.parametrize("kind", ["organization", "person", "policy_version", "location"])
def test_counted_metadata_preserves_authorized_relation_without_business_columns(kind):
    compiled = relation(kind)
    counted = getattr(compiled, "counted_metadata_statement", None)
    assert counted is not None, "F2 evidence still repeats its authorized metadata relation"
    from sqlalchemy.dialects import postgresql

    sql = str(counted.compile(dialect=postgresql.dialect()))
    assert "MATERIALIZED" in sql
    assert "jsonb_build_object" not in sql
    assert "contact_value" not in sql and "content_text" not in sql
    base = counted.get_final_froms()[0].left.element.get_final_froms()[0].element
    assert base.get_final_froms() == compiled.metadata_statement.get_final_froms()
    assert base._where_criteria == compiled.metadata_statement._where_criteria
    assert list(base.selected_columns.keys()) == list(
        compiled.metadata_statement.selected_columns.keys()
    )


@pytest.mark.parametrize("current_time", [False, True])
def test_f3_evidence_inherited_backend_keeps_legacy_projection_and_fresh_rr(current_time):
    from datetime import timedelta
    from types import SimpleNamespace

    from app.composition.school_fact_family_staff_reader import F3EvidenceBackend

    at = datetime(2026, 9, 6, tzinfo=UTC)
    now = at + timedelta(seconds=1)
    expected = (("person:a", "digest", "available", None, 1),)
    calls = []

    class Session:
        def connection(self, *, execution_options):
            calls.append(("begin", execution_options))

        def scalar(self, statement):
            calls.append(("count", statement))
            return 1

        def execute(self, statement):
            calls.append(("metadata", statement))
            return expected

        def commit(self):
            calls.append(("commit",))

        def rollback(self):
            pytest.fail("Valid inherited F3 metadata must remain supported")

        def close(self):
            calls.append(("close",))

    backend = F3EvidenceBackend(sessions=Session, thin=None)
    backend.clock = lambda: now
    probe = SimpleNamespace(
        current_time=current_time,
        arguments=dict(
            family_signature=True, school_id=UUID(int=1), fact_kind="person",
            sources=("person:a",), fields=("display_name",), as_of=at,
        ),
    )
    assert backend.metadata(probe) == (1, expected)
    assert [call[0] for call in calls] == ["begin", "count", "metadata", "commit", "close"]
    assert calls[0][1] == {"isolation_level": "REPEATABLE READ"}
    assert (now if current_time else at) in calls[2][1].compile().params.values()


@pytest.mark.parametrize("purpose", ["read", "maintain"])
@pytest.mark.parametrize(
    "version_end,planned_end,revocation,expected",
    [
        (None, None, None, None),
        (None, "2027-01-01", None, "2027-01-01"),
        ("2026-10-01", "2027-01-01", None, "2026-10-01"),
        ("2027-01-01", "2026-10-01", None, "2026-10-01"),
        (None, None, "2026-10-01", "2026-10-01"),
        ("2027-01-01", "2026-12-01", "2026-10-01", "2026-10-01"),
    ],
)
def test_assignment_envelope_ends_at_first_actual_boundary(
    purpose, version_end, planned_end, revocation, expected
):
    expression = relation("assignment", purpose).data_statement.selected_columns.valid_to
    statement = select(expression).compile(dialect=sqlite.dialect())
    with sqlite3.connect(":memory:") as connection:
        # PostgreSQL LEAST ignores NULL; SQLite's built-in min does not.
        connection.create_function(
            "least", -1, lambda *values: min((v for v in values if v is not None), default=None)
        )
        connection.execute(
            "CREATE TABLE staff_assignment_versions "
            "(effective_to TEXT, planned_valid_to TEXT, revoked_at TEXT)"
        )
        connection.execute(
            "INSERT INTO staff_assignment_versions VALUES (?, ?, ?)",
            (version_end, planned_end, revocation),
        )
        assert connection.execute(str(statement)).fetchone()[0] == expected


def test_policy_root_uses_declared_business_start_and_keeps_publication_time_separate():
    compiled = relation("policy")
    columns = compiled.data_statement.selected_columns
    assert "school_fact_f2_reader_policy_valid_from" in str(columns.valid_from)
    assert "school_policies.created_at" not in str(compiled.count_statement)
    assert "school_fact_f2_reader_policy_valid_from" in str(compiled.count_statement)
    assert "school_fact_change_lineage.created_at" in str(columns.published_at)
    assert "master_data_import_units" not in str(compiled.data_statement)


def test_policy_time_migration_projects_first_upsert_only_with_reader_anchor_boundary():
    import importlib.util
    from pathlib import Path

    path = (
        Path(__file__).resolve().parents[1]
        / "alembic/versions/20260906_24_school_fact_person_reader.py"
    )
    spec = importlib.util.spec_from_file_location("policy_time_migration", path)
    assert spec and spec.loader
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    ddl = migration._POLICY_TIME_DDL
    assert "SESSION_USER <> 'campus_ai_school_fact_reader'" in ddl
    assert "CURRENT_USER <> 'campus_ai_context_definer'" in ddl
    assert "p_school_id IS NULL OR p_target_id IS NULL" in ddl
    assert "app.require_school_database_anchor() IS DISTINCT FROM p_school_id" in ddl
    assert "SET search_path = pg_catalog" in ddl
    assert "public.school_policies" not in ddl
    assert "lineage.created_at" not in ddl and "lineage.id" not in ddl
    assert "min(initial.target_version)" in ddl
    assert "LIMIT 1" not in ddl  # Conflicting first publications fail scalar cardinality.
    query = ddl.split("RETURN (", 1)[1].split(");", 1)[0]
    query = query.replace("public.", "").replace("::timestamptz", "")
    query = query.replace("p_school_id", ":school").replace("p_target_id", ":target")
    with sqlite3.connect(":memory:") as connection:
        connection.executescript(
            "CREATE TABLE master_data_import_units (school_id TEXT, id TEXT, payload TEXT);"
            "CREATE TABLE school_fact_change_lineage (school_id TEXT, target_id TEXT, "
            "unit_id TEXT, fact_kind TEXT, operation TEXT, target_version INTEGER, "
            "created_at TEXT, id TEXT);"
            "INSERT INTO master_data_import_units VALUES "
            "('school','first','{\"effective_at\":\"2026-09-06T03:27:31Z\"}'),"
            "('school','second','{\"effective_at\":\"2025-01-01T00:00:00Z\"}');"
            "INSERT INTO school_fact_change_lineage VALUES "
            "('school','policy','first','policy','upsert',1,'2026-09-06T03:28:08Z','1'),"
            "('school','policy','second','policy','upsert',2,'2026-10-01T00:00:00Z','2');"
        )
        assert connection.execute(query, dict(school="school", target="policy")).fetchone() == (
            "2026-09-06T03:27:31Z",
        )
        assert connection.execute(query, dict(school="other", target="policy")).fetchone() is None
        assert connection.execute(query, dict(school="school", target="missing")).fetchone() is None


@pytest.mark.parametrize(
    "kind",
    [
        "organization_profile",
        "organization_membership",
        "organization_location",
        "person_workplace",
        "school_service",
        "fact_quality",
    ],
)
def test_f2_maintenance_content_includes_explicit_relation_end(kind):
    import json

    from sqlalchemy import create_engine, literal
    from sqlalchemy.sql import visitors
    from sqlalchemy.sql.elements import ColumnClause
    from sqlalchemy.sql.selectable import ScalarSelect

    school = UUID(int=1)
    actor = actors._issue_staff_actor_context(
        account_user_id=7,
        school_id=SchoolId(school),
        person_id=PersonId(UUID(int=2)),
        assignment_id=AssignmentId(UUID(int=3)),
        assignment_version=1,
        authorization_version=1,
    )
    scope = F2FactScope(school, kind, ("allowed.object",))
    reference = scope.reference
    if kind == "fact_quality":
        reference = json.dumps(
            {
                "quality_scope": reference,
                "target_scopes": [F2FactScope(school, "school", ("school.one",)).reference],
            }
        )
    compiled = compile_f2(
        operation=f2_operation_registry().require(f"school_facts.f2.{kind}.maintain"),
        actor=actor,
        scope_plan=ScopePlan(UUID(int=4), "school_facts.f2", reference, 1),
        as_of=datetime(2026, 9, 6, tzinfo=UTC),
        after_id=None,
        page_size=2,
        selection=None,
    )
    # Execute the actual JSON projection. Substitute scalar reference lookups only;
    # authorization/count/row predicates are covered by the secure-reader suite.
    expiry = "2026-10-01T00:00:00+00:00"

    def replace_column(element, **kwargs):
        if isinstance(element, ScalarSelect):
            return literal("source.reference")
        if isinstance(element, ColumnClause):
            return literal(expiry if element.name == "valid_to" else None)
        return None

    expression = visitors.replacement_traverse(
        compiled.data_statement.selected_columns.content, {}, replace_column
    )
    with create_engine("sqlite://").connect() as connection:
        native = connection.connection.driver_connection
        assert native is not None
        native.create_function(
            "jsonb_build_object",
            -1,
            lambda *args: json.dumps(dict(zip(args[::2], args[1::2], strict=True))),
        )
        native.create_function(
            "jsonb_strip_nulls",
            1,
            lambda value: json.dumps({k: v for k, v in json.loads(value).items() if v is not None}),
        )
        value = connection.scalar(select(expression))
        assert isinstance(value, str)
        projected = json.loads(value)
    assert projected["valid_to"] == expiry


def test_assignment_maintenance_preserves_planned_end_not_content_slice_end():
    from sqlalchemy.dialects import postgresql

    content = relation("assignment", "maintain").data_statement.selected_columns.content
    rendered = str(content.compile(dialect=postgresql.dialect()))
    assert "staff_assignment_versions.planned_valid_to" in rendered
    assert "staff_assignment_versions.effective_to" not in rendered
    assert "staff_assignment_versions.revoked_at" not in rendered


@pytest.mark.parametrize("purpose", ["read", "maintain"])
@pytest.mark.parametrize(
    "revision_start,business_start,revision_end,business_end,revocation,expected_start,expected_end",
    [
        ("2026-09-01", "2026-10-01", None, "2026-11-01", None, "2026-10-01", "2026-11-01"),
        ("2026-10-15", "2026-10-01", None, "2026-11-01", None, "2026-10-15", "2026-11-01"),
        ("2026-09-01", "2026-09-01", "2026-10-01", "2026-11-01", None, "2026-09-01", "2026-10-01"),
        ("2026-09-01", "2026-09-01", None, "2026-11-01", "2026-10-01", "2026-09-01", "2026-10-01"),
    ],
)
def test_policy_envelope_uses_intersection_while_content_keeps_official_dates(
    purpose,
    revision_start,
    business_start,
    revision_end,
    business_end,
    revocation,
    expected_start,
    expected_end,
):
    columns = relation("policy_version", purpose).data_statement.selected_columns
    statement = select(columns.valid_from, columns.valid_to).compile(dialect=sqlite.dialect())
    with sqlite3.connect(":memory:") as connection:
        connection.create_function(
            "least", -1, lambda *values: min((v for v in values if v is not None), default=None)
        )
        connection.create_function(
            "greatest", -1, lambda *values: max((v for v in values if v is not None), default=None)
        )
        connection.execute(
            "CREATE TABLE school_policy_versions (valid_from TEXT, effective_from "
            "TEXT, valid_to TEXT, effective_to TEXT, revoked_at TEXT)"
        )
        connection.execute(
            "INSERT INTO school_policy_versions VALUES (?,?,?,?,?)",
            (revision_start, business_start, revision_end, business_end, revocation),
        )
        assert connection.execute(str(statement)).fetchone() == (expected_start, expected_end)
    content = str(columns.content)
    assert "school_policy_versions.effective_from" in content
    assert "school_policy_versions.effective_to" in content
