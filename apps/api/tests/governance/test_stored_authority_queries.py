"""Execute real ORM SELECTs in isolated SQLite; production constraints/ACL need PostgreSQL."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest
from sqlalchemy import JSON, Column, MetaData, Table, create_engine, event
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker

from app.platform.governance.basis import (
    GovernanceAuthorizationBasisService,
    GovernanceBasisInvalid,
)
from app.platform.governance.contracts import (
    AssignmentAuthoritySnapshot,
    GovernanceAuthorityQuery,
    GovernanceAuthorizationBasis,
    RootAuthoritySnapshot,
    ScopeResolutionSnapshot,
)
from app.platform.governance.models import (
    GovernanceAssignmentEpoch,
    GovernanceDuty,
    GovernanceDutyVersion,
    GovernanceGrant,
    GovernanceGrantVersion,
    GovernanceScope,
    GovernanceScopeVersion,
)
from app.platform.governance.reader import GovernanceAuthorityReader, GovernanceAuthorityUnavailable

AT = datetime(2026, 9, 6, tzinfo=UTC)
MODELS = (
    GovernanceDuty,
    GovernanceDutyVersion,
    GovernanceScope,
    GovernanceScopeVersion,
    GovernanceGrant,
    GovernanceGrantVersion,
    GovernanceAssignmentEpoch,
)


@pytest.fixture
def stored():
    engine = create_engine("sqlite://")
    metadata = MetaData()
    tables = {}
    # Omit production FKs/checks to represent individually unavailable joined references.
    # Execute actual filtering/join/terminal SQL, rather than mocking Session responses.
    for model in MODELS:
        tables[model] = Table(
            model.__tablename__,
            metadata,
            *[
                Column(
                    c.name,
                    JSON() if isinstance(c.type, JSONB) else c.type,
                    primary_key=c.primary_key,
                    nullable=True,
                )
                for c in model.__table__.columns
            ],
        )
    metadata.create_all(engine)
    school, assignment, duty, scope, grant = (uuid4() for _ in range(5))

    def insert(model, **values):
        row: dict[str, Any] = {c.name: None for c in tables[model].columns}
        row.update(
            id=uuid4(),
            school_id=school,
            created_at=AT,
            updated_at=AT,
            current_version=1,
            version=1,
            effective_from=AT,
            valid_from=AT,
        )
        row.update(values)
        row = {k: v for k, v in row.items() if k in tables[model].c}
        with engine.begin() as connection:
            connection.execute(tables[model].insert().values(**row))

    def add_grant(grant_id=grant, **values):
        insert(GovernanceGrant, id=grant_id, assignment_id=assignment, duty_id=duty)
        add_version(grant_id=grant_id, **values)

    def add_version(grant_id=grant, **values):
        row = dict(
            grant_id=grant_id,
            duty_id=duty,
            duty_version=1,
            scope_id=scope,
            scope_version=1,
            basis_authority="fixture",
            basis_reference="fixture://grant",
            transition_kind="create",
            planned_valid_to=AT + timedelta(days=20),
        )
        row.update(values)
        insert(GovernanceGrantVersion, **row)

    insert(GovernanceDuty, id=duty, duty_code="fixture.read")
    insert(GovernanceDutyVersion, duty_id=duty, is_active=True)
    insert(GovernanceScope, id=scope, scope_key="fixture")
    insert(
        GovernanceScopeVersion,
        scope_id=scope,
        is_active=True,
        schema_owner="fixture",
        schema_name="school",
        schema_version=1,
        reference_document={"fixture": True},
        reference_sha256="a" * 64,
    )
    insert(GovernanceAssignmentEpoch, assignment_id=assignment, epoch=7)
    add_grant()
    statements = []
    event.listen(engine, "before_cursor_execute", lambda _c, _u, sql, *_: statements.append(sql))
    reader = GovernanceAuthorityReader(
        session_factory=sessionmaker(engine),
        dependency_port=cast(Any, None),
        scope_owner_resolvers={},
    )
    query = GovernanceAuthorityQuery(
        school_id=school,
        assignment_id=assignment,
        duty_code="fixture.read",
        scope_id=scope,
        as_of=AT,
    )
    yield SimpleNamespace(
        engine=engine,
        tables=tables,
        reader=reader,
        query=query,
        statements=statements,
        grant=grant,
        scope=scope,
        duty=duty,
        add_grant=add_grant,
        add_version=add_version,
    )
    engine.dispose()


def test_stored_authority_keeps_cardinality_gates_in_one_select(stored):
    value = stored.reader._read_stored_authority(stored.query)
    assert value.grant_id == stored.grant and value.assignment_epoch == 7
    assert value.scope_id == stored.scope and value.reference_document == {"fixture": True}
    assert value.next_grant_effective_from is None
    assert len(stored.statements) == 1  # Cardinality gates precede reference resolution inside SQL.


@pytest.mark.parametrize(
    "model",
    [GovernanceDutyVersion, GovernanceScope, GovernanceScopeVersion, GovernanceAssignmentEpoch],
)
def test_missing_reference_remains_grant_inactive(stored, model):
    with stored.engine.begin() as connection:
        connection.execute(stored.tables[model].delete())
    with pytest.raises(GovernanceAuthorityUnavailable) as error:
        stored.reader._read_stored_authority(stored.query)
    assert error.value.reason == "grant_inactive"


@pytest.mark.parametrize("model", [GovernanceDutyVersion, GovernanceScopeVersion])
@pytest.mark.parametrize(
    "change",
    [
        {"is_active": False},
        {"effective_from": AT + timedelta(seconds=1)},
        {"version": 2},
        {"school_id": uuid4()},
    ],
)
def test_inactive_future_or_wrong_reference_remains_denied(stored, model, change):
    with stored.engine.begin() as connection:
        connection.execute(stored.tables[model].update().values(**change))
    with pytest.raises(GovernanceAuthorityUnavailable) as error:
        stored.reader._read_stored_authority(stored.query)
    assert error.value.reason == "grant_inactive"


def test_duplicate_grant_is_ambiguous_before_missing_reference_filter(stored):
    stored.add_grant(grant_id=uuid4())
    with stored.engine.begin() as connection:
        connection.execute(stored.tables[GovernanceScopeVersion].delete())
    with pytest.raises(GovernanceAuthorityUnavailable) as error:
        stored.reader._read_stored_authority(stored.query)
    assert error.value.reason == "grant_ambiguous"


@pytest.mark.parametrize("transition", ["update", "end", "revoke"])
def test_future_transition_boundary_and_terminal_history(stored, transition):
    boundary = AT + timedelta(days=2)
    values = dict(version=2, transition_kind=transition, effective_from=boundary)
    if transition == "end":
        values["formal_ended_at"] = boundary
    if transition == "revoke":
        values["revoked_at"] = boundary
    stored.add_version(**values)
    before = stored.reader._read_stored_authority(
        replace(stored.query, as_of=boundary - timedelta(microseconds=1))
    )
    assert before.grant_version == 1
    assert before.next_grant_effective_from == boundary.replace(tzinfo=None)
    if transition == "update":
        after = stored.reader._read_stored_authority(replace(stored.query, as_of=boundary))
        assert after.grant_version == 2 and after.next_grant_effective_from is None
    else:
        with pytest.raises(GovernanceAuthorityUnavailable) as error:
            stored.reader._read_stored_authority(replace(stored.query, as_of=boundary))
        assert error.value.reason == "grant_inactive"


def test_epoch_is_fresh_and_planned_end_is_exclusive(stored):
    assert stored.reader._read_stored_authority(stored.query).assignment_epoch == 7
    with stored.engine.begin() as connection:
        connection.execute(stored.tables[GovernanceAssignmentEpoch].update().values(epoch=8))
    assert stored.reader._read_stored_authority(stored.query).assignment_epoch == 8
    with pytest.raises(GovernanceAuthorityUnavailable) as error:
        stored.reader._read_stored_authority(replace(stored.query, as_of=AT + timedelta(days=20)))
    assert error.value.reason == "grant_inactive"


def test_duplicate_duty_is_ambiguous_even_when_no_grant_matches(stored):
    with stored.engine.begin() as connection:
        original = connection.execute(stored.tables[GovernanceDuty].select()).mappings().one()
        connection.execute(
            stored.tables[GovernanceDuty].insert().values(**{**original, "id": uuid4()})
        )
        connection.execute(stored.tables[GovernanceGrantVersion].delete())
    with pytest.raises(GovernanceAuthorityUnavailable) as error:
        stored.reader._read_stored_authority(stored.query)
    assert error.value.reason == "grant_ambiguous"


def test_missing_duty_is_inactive_even_with_orphan_grant(stored):
    with stored.engine.begin() as connection:
        connection.execute(stored.tables[GovernanceDuty].delete())
    with pytest.raises(GovernanceAuthorityUnavailable) as error:
        stored.reader._read_stored_authority(stored.query)
    assert error.value.reason == "grant_inactive"


def test_other_school_same_duty_and_grant_cannot_authorize(stored):
    with pytest.raises(GovernanceAuthorityUnavailable) as error:
        stored.reader._read_stored_authority(replace(stored.query, school_id=uuid4()))
    assert error.value.reason == "grant_inactive"


def test_second_grant_with_unavailable_reference_still_causes_ambiguity(stored):
    stored.add_grant(grant_id=uuid4(), duty_version=99)
    with pytest.raises(GovernanceAuthorityUnavailable) as error:
        stored.reader._read_stored_authority(stored.query)
    assert error.value.reason == "grant_ambiguous"


def test_terminal_history_wins_over_later_update(stored):
    stored.add_version(
        version=2,
        transition_kind="revoke",
        effective_from=AT + timedelta(days=1),
        revoked_at=AT + timedelta(days=1),
    )
    stored.add_version(version=3, transition_kind="update", effective_from=AT + timedelta(days=2))
    with pytest.raises(GovernanceAuthorityUnavailable) as error:
        stored.reader._read_stored_authority(replace(stored.query, as_of=AT + timedelta(days=3)))
    assert error.value.reason == "grant_inactive"


def test_stored_batch_preserves_exact_query_time_identity_and_order(stored):
    queries = (
        stored.query,
        replace(stored.query, assignment_id=uuid4()),
        replace(stored.query, school_id=uuid4()),
        replace(stored.query, scope_id=uuid4()),
        replace(stored.query, duty_code="fixture.other"),
        replace(stored.query, as_of=AT + timedelta(days=20)),
        stored.query,
    )
    values = stored.reader._read_stored_authorities(queries)
    assert values[0].grant_id == values[-1].grant_id == stored.grant
    assert all(isinstance(value, GovernanceAuthorityUnavailable) for value in values[1:-1])
    assert all(value.reason == "grant_inactive" for value in values[1:-1])
    assert len(stored.statements) == 1


def test_stored_batch_keeps_ambiguity_before_reference_filter(stored):
    stored.add_grant(grant_id=uuid4(), duty_version=99)
    stored.statements.clear()
    values = stored.reader._read_stored_authorities(
        (stored.query, replace(stored.query, scope_id=uuid4()))
    )
    assert tuple(value.reason for value in values) == ("grant_ambiguous", "grant_inactive")
    assert len(stored.statements) == 1


def test_stored_batch_terminal_and_future_boundaries_are_per_query(stored):
    boundary = AT + timedelta(days=1)
    stored.add_version(
        version=2, transition_kind="revoke", effective_from=boundary, revoked_at=boundary
    )
    stored.add_version(version=3, transition_kind="update", effective_from=AT + timedelta(days=2))
    stored.statements.clear()
    values = stored.reader._read_stored_authorities(
        (
            stored.query,
            replace(stored.query, as_of=boundary),
            replace(stored.query, as_of=AT + timedelta(days=3)),
        )
    )
    assert values[0].next_grant_effective_from == boundary.replace(tzinfo=None)
    assert tuple(value.reason for value in values[1:]) == ("grant_inactive", "grant_inactive")
    assert len(stored.statements) == 1


def test_stored_batch_rejects_oversize_before_opening_session(stored):
    assert stored.reader._read_stored_authorities(()) == ()
    with pytest.raises(ValueError):
        stored.reader._read_stored_authorities((stored.query,) * 129)
    assert stored.statements == []


def test_stored_batch_accepts_full_bound_without_extra_statements(stored):
    values = stored.reader._read_stored_authorities((stored.query,) * 128)
    assert len(values) == 128
    assert all(value.grant_id == stored.grant for value in values)
    assert len(stored.statements) == 1


def _batch_reader(stored):
    person, organization = uuid4(), uuid4()
    state = SimpleNamespace(scope_calls=[], person_version=1, after_scope=None)

    def resolve_many(requests):
        state.scope_calls.append(requests)
        if state.after_scope is not None:
            state.after_scope()
        return tuple(
            ScopeResolutionSnapshot(
                school_id=request.school_id,
                owner="fixture",
                reference=str(request.scope_id),
                version=1,
                is_effective=True,
                valid_until=None,
                is_ambiguous=False,
            )
            for request in requests
        )

    dependencies = SimpleNamespace(
        read_assignment=lambda **kw: AssignmentAuthoritySnapshot(
            school_id=kw["school_id"],
            assignment_id=kw["assignment_id"],
            person_id=person,
            organization_id=organization,
            version=1,
            is_effective=True,
            valid_until=None,
        ),
        read_person_root=lambda **kw: RootAuthoritySnapshot(
            school_id=kw["school_id"],
            root_kind="person",
            root_id=person,
            version=state.person_version,
            is_effective=True,
            valid_until=None,
        ),
        read_organization_root=lambda **kw: RootAuthoritySnapshot(
            school_id=kw["school_id"],
            root_kind="organization",
            root_id=organization,
            version=1,
            is_effective=True,
            valid_until=None,
        ),
    )
    reader = GovernanceAuthorityReader(
        session_factory=sessionmaker(stored.engine),
        dependency_port=cast(Any, dependencies),
        scope_owner_resolvers={"fixture": cast(Any, SimpleNamespace(resolve_many=resolve_many))},
    )
    # SQLite drops timezone information; use a consistent clock for owner-boundary checks.
    query = replace(stored.query, as_of=AT.replace(tzinfo=None))
    return reader, query, state


def test_resolve_many_batches_scope_owner_and_keeps_per_query_denial(stored):
    reader, query, state = _batch_reader(stored)
    values = reader.resolve_many((query, replace(query, assignment_id=uuid4()), query))
    assert not isinstance(values[0], GovernanceAuthorityUnavailable)
    assert not isinstance(values[2], GovernanceAuthorityUnavailable)
    assert isinstance(values[1], GovernanceAuthorityUnavailable)
    assert values[0].grant_id == values[2].grant_id == stored.grant
    assert values[1].reason == "grant_inactive"
    assert len(state.scope_calls) == 1 and len(state.scope_calls[0]) == 2
    assert len(stored.statements) == 1


def test_resolve_many_fails_closed_when_scope_batch_cardinality_is_wrong(stored):
    reader, query, _ = _batch_reader(stored)
    reader._scope_owner_resolvers = {
        "fixture": cast(Any, SimpleNamespace(resolve_many=lambda _: ()))
    }
    values = reader.resolve_many((query, query))
    assert all(
        isinstance(value, GovernanceAuthorityUnavailable) and value.reason == "dependency_error"
        for value in values
    )


def _require_bases(values) -> tuple[GovernanceAuthorizationBasis, ...]:
    assert all(isinstance(value, GovernanceAuthorizationBasis) for value in values)
    return tuple(values)


def test_basis_batch_issues_then_revalidates_fresh_with_bounded_epoch_reads(stored):
    reader, query, _ = _batch_reader(stored)
    service = GovernanceAuthorizationBasisService(
        session_factory=sessionmaker(stored.engine), authority_reader=reader
    )
    bases = service.issue_many((query, query, replace(query, duty_code="missing")))
    assert isinstance(bases[0], GovernanceAuthorizationBasis)
    assert isinstance(bases[1], GovernanceAuthorizationBasis)
    assert bases[0].grant_id == bases[1].grant_id == stored.grant
    assert isinstance(bases[2], GovernanceAuthorityUnavailable)
    stored.statements.clear()
    valid_bases = _require_bases(bases[:2])
    assert service.validate_many(valid_bases, as_of=query.as_of) == (True, True)
    assert len(stored.statements) == 3  # Fresh epoch before + stored batch + fresh epoch after.
    with stored.engine.begin() as connection:
        connection.execute(stored.tables[GovernanceAssignmentEpoch].update().values(epoch=8))
    stored.statements.clear()
    values = service.validate_many(valid_bases, as_of=query.as_of)
    assert all(isinstance(value, GovernanceBasisInvalid) for value in values)
    assert all(
        isinstance(value, GovernanceBasisInvalid) and value.reason == "assignment_epoch_changed"
        for value in values
    )
    assert len(stored.statements) == 1


def test_basis_batch_rejects_epoch_change_during_owner_resolution(stored):
    reader, query, state = _batch_reader(stored)
    service = GovernanceAuthorizationBasisService(
        session_factory=sessionmaker(stored.engine), authority_reader=reader
    )
    bases = _require_bases(service.issue_many((query, query)))

    def revoke_during_scope():
        with stored.engine.begin() as connection:
            connection.execute(stored.tables[GovernanceAssignmentEpoch].update().values(epoch=8))

    state.after_scope = revoke_during_scope
    values = service.validate_many(bases, as_of=query.as_of)
    assert all(
        isinstance(value, GovernanceBasisInvalid) and value.reason == "assignment_epoch_changed"
        for value in values
    )


def test_issue_batch_rejects_only_epochs_changed_during_scope_resolution(stored):
    reader, query, state = _batch_reader(stored)
    other_assignment, other_grant = uuid4(), uuid4()
    with stored.engine.begin() as connection:
        for model, changes in (
            (GovernanceGrant, {"id": other_grant, "assignment_id": other_assignment}),
            (GovernanceGrantVersion, {"id": uuid4(), "grant_id": other_grant}),
            (GovernanceAssignmentEpoch, {"assignment_id": other_assignment}),
        ):
            original = connection.execute(stored.tables[model].select()).mappings().one()
            connection.execute(stored.tables[model].insert().values(**{**original, **changes}))
    service = GovernanceAuthorizationBasisService(
        session_factory=sessionmaker(stored.engine), authority_reader=reader
    )

    def revoke_during_scope():
        with stored.engine.begin() as connection:
            connection.execute(
                stored.tables[GovernanceAssignmentEpoch]
                .update()
                .where(
                    stored.tables[GovernanceAssignmentEpoch].c.assignment_id == query.assignment_id
                )
                .values(epoch=8)
            )

    state.after_scope = revoke_during_scope
    stored.statements.clear()
    values = service.issue_many((query, replace(query, assignment_id=other_assignment)))
    assert isinstance(values[0], GovernanceAuthorityUnavailable)
    assert values[0].reason == "assignment_epoch_changed"
    assert isinstance(values[1], GovernanceAuthorizationBasis)
    assert values[1].assignment_id == other_assignment and values[1].assignment_epoch == 7
    assert (
        sum(sql.lstrip().upper().startswith(("SELECT", "WITH")) for sql in stored.statements) == 2
    )
    state.after_scope = None
    fresh = service.issue_many((query,))[0]
    assert isinstance(fresh, GovernanceAuthorizationBasis)
    assert fresh.assignment_epoch == 8


def test_basis_batch_keeps_dependency_versions_and_expiry_per_basis(stored):
    reader, query, state = _batch_reader(stored)
    service = GovernanceAuthorizationBasisService(
        session_factory=sessionmaker(stored.engine), authority_reader=reader
    )
    basis = service.issue_many((query,))[0]
    assert isinstance(basis, GovernanceAuthorizationBasis)
    expired = replace(basis, valid_until=query.as_of)
    state.person_version = 2
    fresh_basis = service.issue_many((query,))[0]
    assert isinstance(fresh_basis, GovernanceAuthorizationBasis)
    values = service.validate_many((expired, basis, fresh_basis), as_of=query.as_of)
    assert isinstance(values[0], GovernanceBasisInvalid)
    assert isinstance(values[1], GovernanceBasisInvalid)
    assert values[0].reason == "basis_expired"
    assert values[1].reason == "dependency_version_changed"
    assert values[2] is True


def test_basis_batches_are_bounded_and_empty_batches_do_not_acquire_connections(stored):
    reader, query, _ = _batch_reader(stored)
    service = GovernanceAuthorizationBasisService(
        session_factory=sessionmaker(stored.engine), authority_reader=reader
    )
    assert reader.resolve_many(()) == ()
    assert service.issue_many(()) == ()
    assert service.validate_many((), as_of=query.as_of) == ()
    with pytest.raises(ValueError):
        reader.resolve_many((query,) * 129)
    with pytest.raises(ValueError):
        service.issue_many((query,) * 129)
    with pytest.raises(ValueError):
        service.validate_many((cast(Any, None),) * 129, as_of=query.as_of)
    assert stored.statements == []


def test_basis_batch_preserves_owner_failure_cause_for_source_fault_classification(stored):
    reader, query, _ = _batch_reader(stored)
    service = GovernanceAuthorizationBasisService(
        session_factory=sessionmaker(stored.engine), authority_reader=reader
    )
    bases = _require_bases(service.issue_many((query,)))

    def owner_failed(_requests):
        raise RuntimeError("test-only owner reader failure")

    reader._scope_owner_resolvers = {
        "fixture": cast(Any, SimpleNamespace(resolve_many=owner_failed))
    }
    result = service.validate_many(bases, as_of=query.as_of)[0]
    assert isinstance(result, GovernanceBasisInvalid)
    assert result.reason == "authority_unavailable"
    assert isinstance(result.__cause__, GovernanceAuthorityUnavailable)
    assert result.__cause__.reason == "dependency_error"
