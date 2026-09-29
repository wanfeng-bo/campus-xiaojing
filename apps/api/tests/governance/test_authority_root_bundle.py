from contextlib import contextmanager, nullcontext
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session as SASession
from sqlalchemy.orm import sessionmaker

from app.platform.governance.contracts import (
    AssignmentAuthoritySnapshot,
    GovernanceAuthorityQuery,
    RootAuthoritySnapshot,
)
from app.platform.governance.reader import GovernanceAuthorityReader, GovernanceAuthorityUnavailable
from tests.school_facts.test_f5_family_teaching_postgres import owned_family  # noqa: F401

NOW = datetime(2026, 9, 14, tzinfo=UTC)


def fixture():
    school, assignment_id, person, organization = (uuid4() for _ in range(4))
    query = GovernanceAuthorityQuery(school, assignment_id, "test_only.read", uuid4(), NOW)
    assignment = AssignmentAuthoritySnapshot(
        school, assignment_id, person, organization, 2, True, None
    )
    roots = tuple(
        RootAuthoritySnapshot(school, kind, key, 3, True, None)
        for kind, key in (("person", person), ("organization", organization))
    )
    return query, (assignment, *roots)


class BundlePort:
    def __init__(self, values):
        self.values, self.calls, self.legacy = values, [], []
        self.fallback: Any = None

    def read_authority_roots(self, **kw):
        self.calls.append(kw)
        if isinstance(self.values, Exception):
            raise self.values
        return self.values

    def read_assignment(self, **kw):
        self.legacy.append("assignment")
        return self.fallback[0]

    def read_person_root(self, **kw):
        self.legacy.append("person")
        return self.fallback[1]

    def read_organization_root(self, **kw):
        self.legacy.append("organization")
        return self.fallback[2]


def reader(port):
    return GovernanceAuthorityReader(
        session_factory=cast(sessionmaker[SASession], None),
        dependency_port=port,
        scope_owner_resolvers={},
    )


def test_bundle_is_fresh_each_decision_and_legacy_is_not_called():
    query, values = fixture()
    port = BundlePort(values)
    instance = reader(port)
    assert instance._read_dependencies(query) == values
    changed = (replace(values[0], version=4), *values[1:])
    port.values = changed
    assert instance._read_dependencies(query) == changed
    assert len(port.calls) == 2
    assert port.legacy == []


@pytest.mark.parametrize("index", [0, 1, 2])
@pytest.mark.parametrize(
    "change", ["missing", "school", "id", "version", "inactive", "ambiguous", "boundary"]
)
def test_bundle_preserves_each_root_validation(index, change):
    query, original = fixture()
    values: list[Any] = list(original)
    value = values[index]
    if change == "missing":
        value = None
    else:
        fields = {
            "school": {"school_id": uuid4()},
            "id": {"assignment_id" if index == 0 else "root_id": uuid4()},
            "version": {"version": 0},
            "inactive": {"is_effective": False},
            "ambiguous": {"is_ambiguous": True},
            "boundary": {"valid_until": NOW},
        }
        value = replace(value, **fields[change])
    values[index] = value
    port = BundlePort(tuple(values))
    with pytest.raises(GovernanceAuthorityUnavailable):
        reader(port)._read_dependencies(query)
    assert len(port.calls) == 1 and port.legacy == []


def test_bundle_error_does_not_retry_using_legacy_reads():
    query, _ = fixture()
    port = BundlePort(RuntimeError("synthetic owner failure"))
    with pytest.raises(GovernanceAuthorityUnavailable, match="owner failed") as error:
        reader(port)._read_dependencies(query)
    assert error.value.reason == "dependency_error"
    assert len(port.calls) == 1 and port.legacy == []


def test_explicit_unsupported_bundle_uses_original_three_reads():
    query, values = fixture()
    port = BundlePort(None)
    port.fallback = values
    assert reader(port)._read_dependencies(query) == values
    assert port.legacy == ["assignment", "person", "organization"]


def test_active_root_round_does_not_acquire_bundle(monkeypatch):
    import app.composition.school_fact_read_capture as capture
    from app.composition.school_fact_product_services import ReadOnlyGovernanceDependencies

    calls = []
    monkeypatch.setattr(
        ReadOnlyGovernanceDependencies, "read_authority_roots", lambda *a, **kw: calls.append(kw)
    )
    dep = capture.EvidenceGovernanceDependencies(
        cast(sessionmaker[SASession], None), owner=object()
    )
    token = capture._root_round.set((object(),))
    try:
        assert dep.read_authority_roots(school_id=uuid4(), assignment_id=uuid4(), as_of=NOW) is None
    finally:
        capture._root_round.reset(token)
    assert calls == []


def test_bundle_sql_is_one_execute_and_preserves_owner_relation():
    from sqlalchemy.dialects import postgresql

    from app.composition.school_fact_product_services import ReadOnlyGovernanceDependencies

    query, _ = fixture()
    statements = []

    class Session:
        def execute(self, statement):
            statements.append(statement)
            return SimpleNamespace(one_or_none=lambda: None)

    sessions = SimpleNamespace(begin=lambda: nullcontext(Session()))
    dependencies = ReadOnlyGovernanceDependencies(cast(sessionmaker[SASession], sessions))
    assert dependencies.read_authority_roots(
        school_id=query.school_id, assignment_id=query.assignment_id, as_of=NOW
    ) == (None, None, None)
    assert len(statements) == 1
    sql = str(statements[0].compile(dialect=postgresql.dialect()))
    for token in (
        "school_fact_reader_assignment_terminal",
        "revoked_at IS NULL",
        "effective_from <=",
        "effective_to >",
        "planned_valid_to >",
        "school_fact_reader_identity_root_is_effective_at",
        "school_people",
        "organization_units",
    ):
        assert token in sql
    assert "LIMIT" not in sql and "FOR UPDATE" not in sql


@pytest.mark.parametrize("batch", [False, True])
def test_public_resolve_routes_preserve_bundle_and_scope_checks(monkeypatch, batch):
    query, values = fixture()
    port = BundlePort(values)
    instance = reader(port)
    stored = SimpleNamespace(schema_owner="test_only")
    scope_calls = []
    monkeypatch.setattr(instance, "_read_stored_authority", lambda _: stored)
    monkeypatch.setattr(
        instance, "_read_stored_authorities", lambda queries: (stored,) * len(queries)
    )
    monkeypatch.setattr(instance, "_resolve_scope", lambda *args: scope_calls.append(args))
    monkeypatch.setattr(instance, "_snapshot", lambda stored, a, p, o, scope: (a, p, o))
    assert (instance.resolve_many((query,))[0] if batch else instance.resolve(query)) == values
    assert len(scope_calls) == 1 and len(port.calls) == 1 and port.legacy == []


@pytest.mark.parametrize("root_index", [1, 2])
def test_wrong_root_kind_is_rejected(root_index):
    query, original = fixture()
    values = list(original)
    values[root_index] = replace(values[root_index], root_kind="wrong")
    with pytest.raises(GovernanceAuthorityUnavailable):
        reader(BundlePort(tuple(values)))._read_dependencies(query)


def assignment_row(query, values):
    assignment = values[0]
    version = SimpleNamespace(
        person_id=assignment.person_id,
        organization_unit_id=assignment.organization_id,
        target_version=assignment.version,
        position_title="synthetic",
        semantic_role_code="teacher",
        assignment_kind="formal",
        appointment_source="manual",
        approval_reference="test_only",
        planned_valid_to=None,
        status="confirmed",
        effective_from=NOW,
        effective_to=None,
    )
    return (
        SimpleNamespace(school_id=query.school_id, id=query.assignment_id),
        version,
        SimpleNamespace(display_name="synthetic"),
        NOW,
        [3, True],
        [3, True],
    )


def test_adapter_reads_new_root_versions_each_time_in_one_sql():
    from app.composition.school_fact_product_services import ReadOnlyGovernanceDependencies

    query, values = fixture()
    state = {"row": assignment_row(query, values), "executes": 0}

    class Session:
        def execute(self, statement):
            state["executes"] += 1
            return SimpleNamespace(one_or_none=lambda: state["row"])

    dep = ReadOnlyGovernanceDependencies(
        cast(Any, SimpleNamespace(begin=lambda: nullcontext(Session())))
    )
    kwargs: dict[str, Any] = dict(
        school_id=query.school_id, assignment_id=query.assignment_id, as_of=NOW
    )
    assert dep.read_authority_roots(**kwargs) == values
    state["row"] = (*state["row"][:4], [4, True], [3, False])
    changed = dep.read_authority_roots(**kwargs)
    assert changed is not None and changed[1] is not None
    assert changed[1].version == 4 and changed[2] is None
    assert state["executes"] == 2


def test_ambiguous_assignment_sql_has_no_retry():
    from sqlalchemy.exc import MultipleResultsFound

    from app.composition.school_fact_product_services import ReadOnlyGovernanceDependencies

    query, _ = fixture()
    calls = []

    def ambiguous():
        raise MultipleResultsFound("synthetic duplicate assignment")

    class Session:
        def execute(self, statement):
            calls.append(statement)
            return SimpleNamespace(one_or_none=ambiguous)

    dep = ReadOnlyGovernanceDependencies(
        cast(Any, SimpleNamespace(begin=lambda: nullcontext(Session())))
    )
    with pytest.raises(GovernanceAuthorityUnavailable) as error:
        reader(dep)._read_dependencies(query)
    assert error.value.reason == "dependency_error" and len(calls) == 1


def owned_assignment(engines):
    from sqlalchemy import text

    with engines["migration"].connect() as connection:
        return (
            connection.execute(
                text(
                    "SELECT a.school_id,a.id AS assignment_id,v.person_id,v.organization_unit_id "
                    "FROM staff_assignments a JOIN staff_assignment_versions v "
                    "ON v.school_id=a.school_id AND v.assignment_id=a.id "
                    "WHERE a.source_key='assignment:f3-writer-safety-requester' "
                    "AND v.effective_to IS NULL"
                )
            )
            .mappings()
            .one()
        )


@contextmanager
def observe_pool_returns(engine):
    """Count actual borrows/returns without assuming a QueuePool implementation."""
    from sqlalchemy import event

    counts = {"checkout": 0, "checkin": 0}

    def checkout(*args):
        counts["checkout"] += 1

    def checkin(*args):
        counts["checkin"] += 1

    event.listen(engine, "checkout", checkout)
    event.listen(engine, "checkin", checkin)
    try:
        yield
    finally:
        event.remove(engine, "checkout", checkout)
        event.remove(engine, "checkin", checkin)
        assert counts["checkout"] > 0
        assert counts["checkout"] == counts["checkin"]


def test_pool_return_observer_supports_null_pool_and_unbinds_after_exception():
    from sqlalchemy import create_engine
    from sqlalchemy.pool import NullPool

    engine = create_engine("sqlite://", poolclass=NullPool)
    try:
        with observe_pool_returns(engine):
            with engine.connect():
                pass
        with pytest.raises(RuntimeError, match="synthetic"):
            with observe_pool_returns(engine):
                with engine.connect():
                    raise RuntimeError("synthetic")
        assert not tuple(engine.pool.dispatch.checkout)
        assert not tuple(engine.pool.dispatch.checkin)
    finally:
        engine.dispose()


def test_owned_pg_bundle_matches_three_owner_reads_in_one_statement(owned_family):  # noqa: F811
    from sqlalchemy import event, text

    from app.composition.school_fact_product_services import ReadOnlyGovernanceDependencies

    engines, _ = owned_family
    selected = owned_assignment(engines)
    engine = engines["reader"]
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT current_user")) == "campus_ai_school_fact_reader"
    dependency = ReadOnlyGovernanceDependencies(sessionmaker(engine))
    arguments: dict[str, Any] = dict(
        school_id=selected["school_id"],
        assignment_id=selected["assignment_id"],
        as_of=datetime.now(UTC),
    )
    calls = []

    def count(*args):
        calls.append(1)

    with observe_pool_returns(engine):
        event.listen(engine, "before_cursor_execute", count)
        try:
            assignment = dependency.read_assignment(**arguments)
            assert assignment is not None
            person = dependency.read_person_root(
                school_id=assignment.school_id,
                person_id=assignment.person_id,
                as_of=arguments["as_of"],
            )
            organization = dependency.read_organization_root(
                school_id=assignment.school_id,
                organization_id=assignment.organization_id,
                as_of=arguments["as_of"],
            )
            assert person is not None and organization is not None
            assert len(calls) == 3
            calls.clear()
            assert dependency.read_authority_roots(**arguments) == (
                assignment,
                person,
                organization,
            )
            assert len(calls) == 1
        finally:
            event.remove(engine, "before_cursor_execute", count)
    print("Owned Reader roots: identical snapshots; legacy SQL=3, bundled SQL=1; held=0")


def test_owned_pg_missing_and_past_roots_fail_closed_without_writes(owned_family):  # noqa: F811
    from app.composition.school_fact_product_services import ReadOnlyGovernanceDependencies

    engines, _ = owned_family
    selected = owned_assignment(engines)
    dependency = ReadOnlyGovernanceDependencies(sessionmaker(engines["reader"]))
    with observe_pool_returns(engines["reader"]):
        for school, assignment_id, at in (
            (selected["school_id"], uuid4(), datetime.now(UTC)),
            (selected["school_id"], selected["assignment_id"], datetime(1900, 1, 1, tzinfo=UTC)),
        ):
            arguments: dict[str, Any] = dict(
                school_id=school, assignment_id=assignment_id, as_of=at
            )
            assert dependency.read_assignment(**arguments) is None
            assert dependency.read_authority_roots(**arguments) == (None, None, None)
            query = GovernanceAuthorityQuery(school, assignment_id, "test_only.read", uuid4(), at)
            with pytest.raises(GovernanceAuthorityUnavailable) as failure:
                reader(dependency)._read_dependencies(query)
            assert failure.value.reason == "dependency_missing"
        past = datetime(1900, 1, 1, tzinfo=UTC)
        assert (
            dependency.read_person_root(
                school_id=selected["school_id"], person_id=selected["person_id"], as_of=past
            )
            is None
        )
        assert (
            dependency.read_organization_root(
                school_id=selected["school_id"],
                organization_id=selected["organization_unit_id"],
                as_of=past,
            )
            is None
        )
