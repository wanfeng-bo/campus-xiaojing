from contextlib import nullcontext
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy.orm import Session, sessionmaker

from app.composition.school_fact_directory_services import F2ScopeOwnerLookup
from app.composition.school_fact_product_services import ReadOnlyGovernanceDependencies


class _ScopeSession:
    def __init__(
        self,
        *,
        current_schools: set[UUID],
        lineage: dict[tuple[UUID, str, str], UUID],
    ) -> None:
        self.current_schools = current_schools
        self.lineage = lineage
        self.calls: list[tuple[str, dict[str, object]]] = []

    def scalar(self, statement: object, parameters: dict[str, object]) -> object:
        sql = " ".join(str(statement).split())
        self.calls.append((sql, parameters))
        if "school_fact_f2_reader_root_is_effective_at" in sql:
            if "'school'" in sql:
                return parameters["school_id"] in self.current_schools
            return False
        if "school_fact_change_lineage" in sql:
            school_id = parameters["school_id"]
            assert isinstance(school_id, UUID)
            return self.lineage.get(
                (
                    school_id,
                    str(parameters["kind"]),
                    str(parameters["source_key"]),
                )
            )
        raise AssertionError(f"unexpected lookup SQL: {sql}")


class _ScopeSessions:
    def __init__(self, session: _ScopeSession) -> None:
        self.session = session

    def begin(self) -> nullcontext[_ScopeSession]:
        return nullcontext(self.session)


@pytest.mark.parametrize("fact_kind", ["person", "organization", "location"])
@pytest.mark.parametrize("temporal_state", ["future", "ended"])
def test_scope_owner_accepts_existing_formal_root_independent_of_target_current_state(
    fact_kind: str, temporal_state: str
) -> None:
    school_id, target_id = uuid4(), uuid4()
    source_key = f"{fact_kind}-{temporal_state}"
    session = _ScopeSession(
        current_schools={school_id},
        lineage={(school_id, fact_kind, source_key): target_id},
    )

    sessions = cast(sessionmaker[Session], _ScopeSessions(session))

    assert F2ScopeOwnerLookup(sessions)(school_id, fact_kind, source_key, datetime.now(UTC))
    root_checks = [call for call in session.calls if "root_is_effective_at" in call[0]]
    assert len(root_checks) == 1
    assert "'school'" in root_checks[0][0]


def test_scope_owner_rejects_cross_school_or_missing_source_coordinate() -> None:
    school_id, other_school_id, target_id = uuid4(), uuid4(), uuid4()
    session = _ScopeSession(
        current_schools={school_id, other_school_id},
        lineage={(school_id, "person", "formal-person"): target_id},
    )
    sessions = cast(sessionmaker[Session], _ScopeSessions(session))
    lookup = F2ScopeOwnerLookup(sessions)
    now = datetime.now(UTC)

    assert not lookup(other_school_id, "person", "formal-person", now)
    assert not lookup(school_id, "person", "missing-person", now)
    lineage_queries = [call[0] for call in session.calls if "school_fact_change_lineage" in call[0]]
    assert lineage_queries
    assert all("school_id=:school_id" in sql for sql in lineage_queries)


class _DependencySession:
    def __init__(self, *, is_effective: bool) -> None:
        self.is_effective = is_effective
        self.sql = ""

    def execute(self, statement: object, parameters: dict[str, object]) -> SimpleNamespace:
        self.sql = " ".join(str(statement).split())
        row = SimpleNamespace(version=7, is_effective=self.is_effective)
        return SimpleNamespace(one_or_none=lambda: row)


class _DependencySessions:
    def __init__(self, session: _DependencySession) -> None:
        self.session = session

    def begin(self) -> nullcontext[_DependencySession]:
        return nullcontext(self.session)


@pytest.mark.parametrize("kind", ["person", "organization"])
def test_governance_identity_dependencies_still_require_current_roots(kind: str) -> None:
    session = _DependencySession(is_effective=False)
    dependency = object.__new__(ReadOnlyGovernanceDependencies)
    dependency._read_sessions = cast(sessionmaker[Session], _DependencySessions(session))
    root_id = uuid4()

    if kind == "person":
        result = dependency.read_person_root(
            school_id=uuid4(), person_id=root_id, as_of=datetime.now(UTC)
        )
    else:
        result = dependency.read_organization_root(
            school_id=uuid4(), organization_id=root_id, as_of=datetime.now(UTC)
        )

    assert result is None
    assert "school_fact_reader_identity_root_is_effective_at" in session.sql


@pytest.mark.parametrize("valid_school,missing", [(True, False), (False, False), (True, True)])
def test_batch_scope_lookup_checks_school_and_every_exact_key_in_one_query(valid_school, missing):
    from app.platform.school_facts.f2_policy import F2FactScope

    school = uuid4()
    calls = []

    class BatchSession:
        def scalar(self, statement, parameters):
            calls.append((str(statement), parameters))
            assert parameters["school_id"] == school
            assert parameters["kind"] == "person"
            assert parameters["keys"] == ["a", "b"]
            return valid_school and not missing

    sessions = cast(sessionmaker[Session], _ScopeSessions(cast(_ScopeSession, BatchSession())))
    scope = F2FactScope(school, "person", ("a", "b"), ("creation-only",))
    assert F2ScopeOwnerLookup(sessions).scope_exists(scope, datetime.now(UTC)) is (
        valid_school and not missing
    )
    assert len(calls) == 1
    sql = calls[0][0]
    assert "school_fact_f2_reader_root_is_effective_at" in sql
    assert "school_id=:school_id" in sql and "fact_kind=:kind" in sql
    assert "NOT EXISTS" in sql and "unnest" in sql
    assert "ORDER BY created_at DESC,id DESC LIMIT 1" in sql


def test_scope_set_uses_one_statement_with_exact_school_time_and_coordinates():
    import json

    from app.platform.school_facts.f2_policy import F2FactScope

    school = uuid4()
    at = datetime.now(UTC)
    scopes = (
        F2FactScope(school, "person", ("shared", "missing")),
        F2FactScope(school, "person", ("shared",), ("new-only",)),
    )
    calls = []

    class BatchSession:
        def execute(self, statement, parameters):
            calls.append(str(statement))
            rows = json.loads(parameters["requests"])
            assert [row["keys"] for row in rows] == [["missing", "shared"], ["shared"]]
            assert all(
                row["school_id"] == str(school) and row["as_of"] == at.isoformat() for row in rows
            )
            return SimpleNamespace(all=lambda: [(0, False), (1, True)])

    sessions = cast(sessionmaker[Session], _ScopeSessions(cast(_ScopeSession, BatchSession())))
    assert F2ScopeOwnerLookup(sessions).scope_exists_many(scopes, (at, at)) == (False, True)
    assert len(calls) == 1
    assert "root_checks AS MATERIALIZED" in calls[0]
    assert "coordinates AS MATERIALIZED" in calls[0]
    assert "ORDER BY created_at DESC,id DESC LIMIT 1" in calls[0]


@pytest.mark.parametrize("active", [True, False])
def test_school_coverage_owner_validates_current_school_without_object_enumeration(active):
    from app.platform.school_facts.f2_policy import F2FactScope

    school = uuid4()
    session = _ScopeSession(current_schools={school} if active else set(), lineage={})
    lookup = F2ScopeOwnerLookup(cast(sessionmaker[Session], _ScopeSessions(session)))
    assert (
        lookup.scope_exists(F2FactScope(school, "person", coverage="school"), datetime.now(UTC))
        is active
    )
    assert len(session.calls) == 1 and "root_is_effective_at" in session.calls[0][0]
