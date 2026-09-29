from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from importlib import import_module
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine import URL
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from tests.governance.test_writer_lifecycle import (
    AS_OF,
    FUTURE,
    PLANNED_END,
    UPDATED_PLANNED_END,
    _actor,
    _create_duty_and_scope,
    _grant_command,
    _writer_environment,
)
from tests.master_data.conftest import PostgresRoles

pytestmark = pytest.mark.integration


@dataclass(frozen=True, slots=True)
class _ReaderApi:
    GovernanceAuthorityReader: type[Any]
    GovernanceAuthorizationBasisService: type[Any]
    GovernanceAuthorityQuery: type[Any]
    AssignmentAuthoritySnapshot: type[Any]
    RootAuthoritySnapshot: type[Any]
    ScopeResolutionSnapshot: type[Any]
    GovernanceDependencyPort: type[Any]
    GovernanceScopeOwnerResolver: type[Any]
    GovernanceAuthorityUnavailable: type[Exception]
    GovernanceBasisInvalid: type[Exception]


def _load_reader_api() -> _ReaderApi:
    """Load lazily so every intended RED node remains collectable."""

    reader = import_module("app.platform.governance.reader")
    basis = import_module("app.platform.governance.basis")
    contracts = import_module("app.platform.governance.contracts")
    ports = import_module("app.platform.governance.ports")
    return _ReaderApi(
        GovernanceAuthorityReader=reader.GovernanceAuthorityReader,
        GovernanceAuthorizationBasisService=basis.GovernanceAuthorizationBasisService,
        GovernanceAuthorityQuery=contracts.GovernanceAuthorityQuery,
        AssignmentAuthoritySnapshot=contracts.AssignmentAuthoritySnapshot,
        RootAuthoritySnapshot=contracts.RootAuthoritySnapshot,
        ScopeResolutionSnapshot=contracts.ScopeResolutionSnapshot,
        GovernanceDependencyPort=ports.GovernanceDependencyPort,
        GovernanceScopeOwnerResolver=ports.GovernanceScopeOwnerResolver,
        GovernanceAuthorityUnavailable=reader.GovernanceAuthorityUnavailable,
        GovernanceBasisInvalid=basis.GovernanceBasisInvalid,
    )


@dataclass(slots=True)
class _DependencyState:
    assignment_version: int = 1
    person_version: int = 11
    organization_version: int = 21
    scope_target_version: int = 31
    valid_until: datetime = datetime(2028, 1, 1, tzinfo=UTC)
    organization_id: UUID = field(default_factory=uuid4)
    invalid: set[str] = field(default_factory=set)
    missing: set[str] = field(default_factory=set)
    errors: set[str] = field(default_factory=set)
    ambiguous: set[str] = field(default_factory=set)


@dataclass(slots=True)
class _TestOnlyFactsOwnerPort:
    api: _ReaderApi
    state: _DependencyState
    school_id: UUID
    assignment_id: UUID
    organization_id: UUID
    person_id: UUID = field(default_factory=uuid4)
    calls: list[tuple[str, datetime]] = field(default_factory=list)

    def _before_read(self, kind: str, as_of: datetime) -> bool:
        self.calls.append((kind, as_of))
        if kind in self.state.errors:
            raise RuntimeError(f"test-only {kind} owner Port failure")
        return kind not in self.state.missing

    def read_assignment(
        self,
        *,
        school_id: UUID,
        assignment_id: UUID,
        as_of: datetime,
    ) -> Any | None:
        assert (school_id, assignment_id) == (self.school_id, self.assignment_id)
        if not self._before_read("assignment", as_of):
            return None
        return self.api.AssignmentAuthoritySnapshot(
            school_id=school_id,
            assignment_id=assignment_id,
            person_id=self.person_id,
            organization_id=self.organization_id,
            version=self.state.assignment_version,
            is_effective="assignment" not in self.state.invalid,
            valid_until=self.state.valid_until,
        )

    def read_person_root(
        self,
        *,
        school_id: UUID,
        person_id: UUID,
        as_of: datetime,
    ) -> Any | None:
        assert (school_id, person_id) == (self.school_id, self.person_id)
        if not self._before_read("person", as_of):
            return None
        return self.api.RootAuthoritySnapshot(
            school_id=school_id,
            root_kind="person",
            root_id=person_id,
            version=self.state.person_version,
            is_effective="person" not in self.state.invalid,
            valid_until=self.state.valid_until,
        )

    def read_organization_root(
        self,
        *,
        school_id: UUID,
        organization_id: UUID,
        as_of: datetime,
    ) -> Any | None:
        assert (school_id, organization_id) == (self.school_id, self.organization_id)
        if not self._before_read("organization", as_of):
            return None
        return self.api.RootAuthoritySnapshot(
            school_id=school_id,
            root_kind="organization",
            root_id=organization_id,
            version=self.state.organization_version,
            is_effective="organization" not in self.state.invalid,
            valid_until=self.state.valid_until,
        )


@dataclass(slots=True)
class _TestOnlyScopeOwnerResolver:
    api: _ReaderApi
    state: _DependencyState
    school_id: UUID
    requests: list[object] = field(default_factory=list)

    def resolve(self, request: object) -> Any | None:
        self.requests.append(request)
        if "scope" in self.state.errors:
            raise RuntimeError("test-only Scope owner Port failure")
        if "scope" in self.state.missing:
            return None
        typed_request = cast(Any, request)
        assert typed_request.school_id == self.school_id
        assert typed_request.schema_owner == "test-only.sf5a"
        assert typed_request.schema_name == "fictitious-school-scope"
        assert typed_request.schema_version == 1
        document = typed_request.reference_document
        assert document["fixture"] == "sf5a-test-only"
        return self.api.ScopeResolutionSnapshot(
            school_id=self.school_id,
            owner="test-only.sf5a",
            reference=f"school:{self.school_id}",
            version=self.state.scope_target_version,
            is_effective="scope" not in self.state.invalid,
            is_ambiguous="scope" in self.state.ambiguous,
            valid_until=self.state.valid_until,
        )


@dataclass(frozen=True, slots=True)
class _ReaderHarness:
    reader: Any
    basis_service: Any
    facts_port: _TestOnlyFactsOwnerPort
    scope_resolver: _TestOnlyScopeOwnerResolver


def _reader_harness(
    api: _ReaderApi,
    writer_harness: Any,
    state: _DependencyState,
) -> _ReaderHarness:
    facts_port = _TestOnlyFactsOwnerPort(
        api=api,
        state=state,
        school_id=writer_harness.facts.school_id,
        assignment_id=writer_harness.facts.assignment_id,
        person_id=writer_harness.facts.person_id,
        organization_id=state.organization_id,
    )
    scope_resolver = _TestOnlyScopeOwnerResolver(
        api=api,
        state=state,
        school_id=writer_harness.facts.school_id,
    )
    reader = api.GovernanceAuthorityReader(
        session_factory=writer_harness.factory,
        dependency_port=facts_port,
        scope_owner_resolvers={"test-only.sf5a": scope_resolver},
    )
    basis_service = api.GovernanceAuthorizationBasisService(
        session_factory=writer_harness.factory,
        authority_reader=reader,
    )
    return _ReaderHarness(reader, basis_service, facts_port, scope_resolver)


def _query(
    api: _ReaderApi,
    writer_harness: Any,
    *,
    marker: str,
    scope_id: UUID,
    as_of: datetime,
) -> Any:
    return api.GovernanceAuthorityQuery(
        school_id=writer_harness.facts.school_id,
        assignment_id=writer_harness.facts.assignment_id,
        duty_code=f"test_only.{marker}.duty",
        scope_id=scope_id,
        as_of=as_of,
    )


def _reason(error: pytest.ExceptionInfo[Exception]) -> str:
    reason = cast(Any, error.value).reason
    return str(getattr(reason, "value", reason))


def test_reader_and_basis_expose_owner_port_protocols_without_scope_product_enum() -> None:
    api = _load_reader_api()
    assert getattr(api.GovernanceDependencyPort, "_is_protocol", False) is True
    assert getattr(api.GovernanceScopeOwnerResolver, "_is_protocol", False) is True


def test_reader_restores_current_as_of_future_planned_end_and_terminal_history(
    isolated_migration_schema: PostgresRoles,
) -> None:
    api = _load_reader_api()
    with _writer_environment(isolated_migration_schema) as writer_harness:
        state = _DependencyState()
        harness = _reader_harness(api, writer_harness, state)

        future_duty, future_scope = _create_duty_and_scope(writer_harness, marker="reader-future")
        future_grant = writer_harness.writer.create_grant(
            _grant_command(
                writer_harness,
                duty=future_duty,
                scope=future_scope,
                marker="reader-future",
            )
        )
        writer_harness.writer.update_grant(
            writer_harness.api.UpdateGrantCommand(
                school_id=writer_harness.facts.school_id,
                grant_id=future_grant.target_id,
                expected_version=1,
                effective_from=FUTURE,
                planned_valid_to=UPDATED_PLANNED_END,
                basis_authority="test-only.sf5a",
                basis_reference="fixture://sf5a/reader-future/amendment",
                actor=_actor(writer_harness, reason="schedule Reader history"),
                idempotency_key="sf5a:reader-future:update",
            )
        )

        before_future = harness.reader.resolve(
            _query(
                api,
                writer_harness,
                marker="reader-future",
                scope_id=future_scope.target_id,
                as_of=FUTURE - timedelta(microseconds=1),
            )
        )
        at_future = harness.reader.resolve(
            _query(
                api,
                writer_harness,
                marker="reader-future",
                scope_id=future_scope.target_id,
                as_of=FUTURE,
            )
        )
        assert (before_future.grant_version, at_future.grant_version) == (1, 2)
        assert before_future.valid_until == FUTURE
        assert at_future.planned_valid_to == UPDATED_PLANNED_END

        for transition, marker in (("end", "reader-end"), ("revoke", "reader-revoke")):
            duty, scope = _create_duty_and_scope(writer_harness, marker=marker)
            grant = writer_harness.writer.create_grant(
                _grant_command(writer_harness, duty=duty, scope=scope, marker=marker)
            )
            terminal_at = AS_OF + timedelta(days=10)
            command_type = (
                writer_harness.api.EndGrantCommand
                if transition == "end"
                else writer_harness.api.RevokeGrantCommand
            )
            command = command_type(
                school_id=writer_harness.facts.school_id,
                grant_id=grant.target_id,
                expected_version=1,
                effective_at=terminal_at,
                actor=_actor(writer_harness, reason=f"Reader {transition} history"),
                idempotency_key=f"sf5a:{marker}:{transition}",
            )
            getattr(writer_harness.writer, f"{transition}_grant")(command)

            historical = harness.reader.resolve(
                _query(
                    api,
                    writer_harness,
                    marker=marker,
                    scope_id=scope.target_id,
                    as_of=terminal_at - timedelta(microseconds=1),
                )
            )
            assert historical.grant_version == 1
            with pytest.raises(api.GovernanceAuthorityUnavailable) as terminal:
                harness.reader.resolve(
                    _query(
                        api,
                        writer_harness,
                        marker=marker,
                        scope_id=scope.target_id,
                        as_of=terminal_at,
                    )
                )
            assert _reason(terminal) == "grant_inactive"

        planned_duty, planned_scope = _create_duty_and_scope(
            writer_harness, marker="reader-planned"
        )
        writer_harness.writer.create_grant(
            _grant_command(
                writer_harness,
                duty=planned_duty,
                scope=planned_scope,
                marker="reader-planned",
            )
        )
        planned_current = harness.reader.resolve(
            _query(
                api,
                writer_harness,
                marker="reader-planned",
                scope_id=planned_scope.target_id,
                as_of=PLANNED_END - timedelta(microseconds=1),
            )
        )
        assert planned_current.valid_until == PLANNED_END
        with pytest.raises(api.GovernanceAuthorityUnavailable) as expired:
            harness.reader.resolve(
                _query(
                    api,
                    writer_harness,
                    marker="reader-planned",
                    scope_id=planned_scope.target_id,
                    as_of=PLANNED_END,
                )
            )
        assert _reason(expired) == "grant_inactive"
        assert {kind for kind, _as_of in harness.facts_port.calls} == {
            "assignment",
            "person",
            "organization",
        }
        assert harness.scope_resolver.requests


def test_regrant_after_revoke_uses_new_grant_identity_and_preserves_old_history(
    isolated_migration_schema: PostgresRoles,
) -> None:
    api = _load_reader_api()
    with _writer_environment(isolated_migration_schema) as writer_harness:
        duty, scope = _create_duty_and_scope(writer_harness, marker="reader-regrant")
        original = writer_harness.writer.create_grant(
            _grant_command(
                writer_harness,
                duty=duty,
                scope=scope,
                marker="reader-regrant-original",
            )
        )
        revoke_at = AS_OF + timedelta(days=10)
        writer_harness.writer.revoke_grant(
            writer_harness.api.RevokeGrantCommand(
                school_id=writer_harness.facts.school_id,
                grant_id=original.target_id,
                expected_version=1,
                effective_at=revoke_at,
                actor=_actor(writer_harness, reason="withdraw original Grant before regrant"),
                idempotency_key="sf5a:reader-regrant:revoke-original",
            )
        )
        regrant_at = revoke_at + timedelta(days=1)
        replacement = writer_harness.writer.create_grant(
            replace(
                _grant_command(
                    writer_harness,
                    duty=duty,
                    scope=scope,
                    marker="reader-regrant-replacement",
                ),
                valid_from=regrant_at,
                effective_from=regrant_at,
            )
        )
        assert replacement.target_id != original.target_id
        assert replacement.assignment_epoch == 3

        state = _DependencyState()
        harness = _reader_harness(api, writer_harness, state)
        historical = harness.reader.resolve(
            _query(
                api,
                writer_harness,
                marker="reader-regrant",
                scope_id=scope.target_id,
                as_of=revoke_at - timedelta(microseconds=1),
            )
        )
        assert historical.grant_id == original.target_id
        assert historical.planned_valid_to == PLANNED_END

        with pytest.raises(api.GovernanceAuthorityUnavailable) as withdrawn:
            harness.reader.resolve(
                _query(
                    api,
                    writer_harness,
                    marker="reader-regrant",
                    scope_id=scope.target_id,
                    as_of=revoke_at,
                )
            )
        assert _reason(withdrawn) == "grant_inactive"

        current = harness.reader.resolve(
            _query(
                api,
                writer_harness,
                marker="reader-regrant",
                scope_id=scope.target_id,
                as_of=regrant_at,
            )
        )
        assert current.grant_id == replacement.target_id
        assert current.grant_version == 1


def test_reader_role_can_read_persisted_governance_authority_without_dml_privileges(
    isolated_migration_schema: PostgresRoles,
) -> None:
    api = _load_reader_api()
    with _writer_environment(isolated_migration_schema) as writer_harness:
        duty, scope = _create_duty_and_scope(writer_harness, marker="formal-reader")
        grant = writer_harness.writer.create_grant(
            _grant_command(
                writer_harness,
                duty=duty,
                scope=scope,
                marker="formal-reader",
            )
        )
        reader = isolated_migration_schema.reader_connection_parameters
        engine = create_engine(
            URL.create(
                "postgresql+psycopg",
                username=str(reader["user"]),
                password=str(reader["password"]),
                host=str(reader["host"]),
                port=int(reader["port"]),
                database=str(reader["dbname"]),
            ),
            poolclass=NullPool,
        )
        try:
            factory = sessionmaker(engine, expire_on_commit=False)
            reader_view = SimpleNamespace(factory=factory, facts=writer_harness.facts)
            harness = _reader_harness(api, reader_view, _DependencyState())
            resolved = harness.reader.resolve(
                _query(
                    api,
                    writer_harness,
                    marker="formal-reader",
                    scope_id=scope.target_id,
                    as_of=AS_OF,
                )
            )
            assert resolved.grant_id == grant.target_id
        finally:
            engine.dispose()


def test_reader_fails_closed_for_invalid_missing_error_and_ambiguous_authority(
    isolated_migration_schema: PostgresRoles,
) -> None:
    api = _load_reader_api()
    with _writer_environment(isolated_migration_schema) as writer_harness:
        duty, scope = _create_duty_and_scope(writer_harness, marker="reader-deny")
        writer_harness.writer.create_grant(
            _grant_command(writer_harness, duty=duty, scope=scope, marker="reader-deny")
        )
        state = _DependencyState()
        harness = _reader_harness(api, writer_harness, state)
        query = _query(
            api,
            writer_harness,
            marker="reader-deny",
            scope_id=scope.target_id,
            as_of=AS_OF,
        )

        cases = (
            (state.invalid, "assignment", "assignment_invalid"),
            (state.invalid, "person", "person_invalid"),
            (state.invalid, "organization", "organization_invalid"),
            (state.invalid, "scope", "scope_invalid"),
            (state.missing, "person", "dependency_missing"),
            (state.errors, "organization", "dependency_error"),
            (state.ambiguous, "scope", "dependency_ambiguous"),
        )
        for failure_set, kind, expected_reason in cases:
            failure_set.add(kind)
            try:
                with pytest.raises(api.GovernanceAuthorityUnavailable) as denied:
                    harness.reader.resolve(query)
                assert _reason(denied) == expected_reason
            finally:
                failure_set.remove(kind)

        invalid_versions = (
            ("assignment_version", "assignment_invalid"),
            ("person_version", "person_invalid"),
            ("organization_version", "organization_invalid"),
            ("scope_target_version", "scope_invalid"),
        )
        for field_name, expected_reason in invalid_versions:
            original_version = cast(int, getattr(state, field_name))
            setattr(state, field_name, 0)
            try:
                with pytest.raises(api.GovernanceAuthorityUnavailable) as denied:
                    harness.reader.resolve(query)
                assert _reason(denied) == expected_reason
            finally:
                setattr(state, field_name, original_version)

        writer_harness.writer.create_grant(
            _grant_command(
                writer_harness,
                duty=duty,
                scope=scope,
                marker="reader-deny-parallel",
            )
        )
        with pytest.raises(api.GovernanceAuthorityUnavailable) as ambiguous_grant:
            harness.reader.resolve(query)
        assert _reason(ambiguous_grant) == "grant_ambiguous"


def test_basis_carries_epoch_dependency_vector_and_expires_across_new_service_instances(
    isolated_migration_schema: PostgresRoles,
) -> None:
    api = _load_reader_api()
    with _writer_environment(isolated_migration_schema) as writer_harness:
        duty, scope = _create_duty_and_scope(writer_harness, marker="basis")
        grant = writer_harness.writer.create_grant(
            _grant_command(writer_harness, duty=duty, scope=scope, marker="basis")
        )
        boundary = AS_OF + timedelta(hours=2)
        state = _DependencyState(valid_until=boundary)
        first = _reader_harness(api, writer_harness, state)
        query = _query(
            api,
            writer_harness,
            marker="basis",
            scope_id=scope.target_id,
            as_of=AS_OF,
        )
        basis = first.basis_service.issue(query)
        dependency_versions = {
            entry.authority: entry.version for entry in basis.dependency_versions
        }
        assert basis.assignment_epoch == 1
        assert basis.grant_id == grant.target_id
        assert basis.grant_version == 1
        assert basis.valid_until == boundary
        assert {
            "school_facts.assignment",
            "school_facts.person",
            "school_facts.organization",
            "test-only.sf5a.scope-target",
            "governance.duty",
            "governance.scope",
            "governance.grant",
            "governance.assignment_epoch",
        } <= dependency_versions.keys()
        assert dependency_versions["school_facts.assignment"] == 1
        assert dependency_versions["school_facts.person"] == 11
        assert dependency_versions["school_facts.organization"] == 21
        assert dependency_versions["test-only.sf5a.scope-target"] == 31

        restarted = _reader_harness(api, writer_harness, state)
        assert restarted.basis_service.validate(
            basis,
            as_of=boundary - timedelta(microseconds=1),
        )
        with pytest.raises(api.GovernanceBasisInvalid) as time_expired:
            restarted.basis_service.validate(basis, as_of=boundary)
        assert _reason(time_expired) == "basis_expired"

        state.valid_until = datetime(2028, 1, 1, tzinfo=UTC)
        for field_name in (
            "assignment_version",
            "person_version",
            "organization_version",
            "scope_target_version",
        ):
            dependency_basis = restarted.basis_service.issue(query)
            original_version = cast(int, getattr(state, field_name))
            setattr(state, field_name, original_version + 1)
            dependency_changed = _reader_harness(api, writer_harness, state)
            with pytest.raises(api.GovernanceBasisInvalid) as stale_dependency:
                dependency_changed.basis_service.validate(dependency_basis, as_of=AS_OF)
            assert _reason(stale_dependency) == "dependency_version_changed"
            setattr(state, field_name, original_version)

        dependency_basis = restarted.basis_service.issue(query)

        writer_harness.writer.update_grant(
            writer_harness.api.UpdateGrantCommand(
                school_id=writer_harness.facts.school_id,
                grant_id=grant.target_id,
                expected_version=1,
                effective_from=FUTURE,
                planned_valid_to=UPDATED_PLANNED_END,
                basis_authority="test-only.sf5a",
                basis_reference="fixture://sf5a/basis/future-update",
                actor=_actor(writer_harness, reason="invalidate old basis by epoch"),
                idempotency_key="sf5a:basis:update",
            )
        )
        epoch_changed = _reader_harness(api, writer_harness, state)
        with pytest.raises(api.GovernanceBasisInvalid) as stale_epoch:
            epoch_changed.basis_service.validate(dependency_basis, as_of=AS_OF)
        assert _reason(stale_epoch) == "assignment_epoch_changed"

        future_query = _query(
            api,
            writer_harness,
            marker="basis",
            scope_id=scope.target_id,
            as_of=FUTURE,
        )
        future_basis = epoch_changed.basis_service.issue(future_query)
        revoke_at = FUTURE + timedelta(days=1)
        writer_harness.writer.revoke_grant(
            writer_harness.api.RevokeGrantCommand(
                school_id=writer_harness.facts.school_id,
                grant_id=grant.target_id,
                expected_version=2,
                effective_at=revoke_at,
                actor=_actor(writer_harness, reason="prospective revoke invalidates basis"),
                idempotency_key="sf5a:basis:revoke",
            )
        )
        after_revoke = _reader_harness(api, writer_harness, state)
        with pytest.raises(api.GovernanceBasisInvalid) as revoked:
            after_revoke.basis_service.validate(future_basis, as_of=FUTURE)
        assert _reason(revoked) == "assignment_epoch_changed"


def test_postgres_short_scope_sees_concurrent_formal_revoke(isolated_migration_schema):
    from concurrent.futures import ThreadPoolExecutor
    from datetime import timedelta
    from types import SimpleNamespace

    from sqlalchemy import text

    from app.composition.school_fact_authority_connection import ConnectionBoundAuthority
    from tests.governance.test_reader_basis import (
        _DependencyState,
        _load_reader_api,
        _query,
        _reader_harness,
    )
    from tests.governance.test_thin_authorization import _staff_actor
    from tests.governance.test_writer_lifecycle import (
        AS_OF,
        _actor,
        _create_duty_and_scope,
        _grant_command,
        _writer_environment,
    )

    # This fixture permits only a disposable PG16 cluster with *_test databases.
    # Owner/scope stubs retain the existing Governance harness contract; the
    # stored Grant, epoch, Writer command and READ COMMITTED connection are real.
    api = _load_reader_api()
    with _writer_environment(isolated_migration_schema, minimal_owner_facts=True) as harness:
        duty, scope = _create_duty_and_scope(harness, marker="c4-short-pass")
        grant = harness.writer.create_grant(
            _grant_command(harness, duty=duty, scope=scope, marker="c4-short-pass")
        )
        at = AS_OF + timedelta(hours=1)
        query = _query(api, harness, marker="c4-short-pass", scope_id=scope.target_id, as_of=at)
        who = _staff_actor(harness)
        reader_pids = []

        def build(bound):
            with bound() as session:
                reader_pids.append(session.scalar(text("SELECT pg_backend_pid()")))
            return _reader_harness(
                api, SimpleNamespace(factory=bound, facts=harness.facts), _DependencyState()
            ).basis_service

        service = ConnectionBoundAuthority(sessions=harness.factory, build_basis=build)
        with service.read_scope(who):
            basis = service.issue(query)

            def revoke():
                return harness.writer.revoke_grant(
                    harness.api.RevokeGrantCommand(
                        school_id=harness.facts.school_id,
                        grant_id=grant.target_id,
                        expected_version=1,
                        effective_at=at,
                        actor=_actor(harness, reason="test-only concurrent short-pass revoke"),
                        idempotency_key="sf5a:c4-short-pass:revoke",
                    )
                )

            with ThreadPoolExecutor(max_workers=1) as worker:
                receipt = worker.submit(revoke).result(timeout=15)
            assert receipt.assignment_epoch == basis.assignment_epoch + 1
            with pytest.raises(api.GovernanceBasisInvalid) as invalid:
                service.validate(basis, as_of=at)
            assert cast(Any, invalid.value).reason == "assignment_epoch_changed"
            with pytest.raises(api.GovernanceAuthorityUnavailable):
                service.issue(query)
        assert len(reader_pids) == 3 and len(set(reader_pids)) == 1


def test_c4_minimal_formal_seed_contains_only_required_owner_dependencies():
    from tests.governance.test_writer_lifecycle import _minimal_owner_change_set

    change_set = _minimal_owner_change_set(school_source_key="school:c4")
    assert [(item.fact_kind, item.source_key) for item in change_set.changes] == [
        ("person", "person:parent"),
        ("organization", "organization:dorm"),
        ("assignment", "assignment:parent"),
    ]
    assert all(
        item.operation == "upsert" and item.expected_version == 0 for item in change_set.changes
    )


def test_postgres_basis_batches_keep_current_expired_and_missing_results(
    isolated_migration_schema: PostgresRoles,
) -> None:
    from sqlalchemy import event

    api = _load_reader_api()
    with _writer_environment(isolated_migration_schema, minimal_owner_facts=True) as writer:
        duty, scope = _create_duty_and_scope(writer, marker="batch-results")
        grant = writer.writer.create_grant(
            _grant_command(writer, duty=duty, scope=scope, marker="batch-results")
        )
        reader = _reader_harness(api, writer, _DependencyState())
        query = _query(api, writer, marker="batch-results", scope_id=scope.target_id, as_of=AS_OF)
        statements: list[str] = []

        def record_sql(_connection, _cursor, statement, _parameters, _context, _executemany):
            statements.append(statement)

        event.listen(writer.engine, "before_cursor_execute", record_sql)
        try:
            issued = reader.basis_service.issue_many(
                (
                    query,
                    replace(query, as_of=PLANNED_END),
                    replace(query, duty_code="test_only.batch-results.missing"),
                )
            )
            basis = issued[0]
            assert not isinstance(basis, Exception)
            assert basis.grant_id == grant.target_id and basis.assignment_epoch == 1
            assert all(
                isinstance(value, api.GovernanceAuthorityUnavailable) for value in issued[1:]
            )
            assert [value.reason for value in issued[1:]] == ["grant_inactive", "grant_inactive"]
            assert len(statements) == 2  # Stored batch, then independent issuance epoch check.

            statements.clear()
            validated = reader.basis_service.validate_many(
                (
                    basis,
                    replace(basis, valid_until=AS_OF),
                    replace(basis, duty_code="test_only.batch-results.missing"),
                ),
                as_of=AS_OF,
            )
            assert validated[0] is True
            assert all(isinstance(value, api.GovernanceBasisInvalid) for value in validated[1:])
            assert [value.reason for value in validated[1:]] == [
                "basis_expired",
                "authority_unavailable",
            ]
            assert len(statements) == 3  # Fresh epoch before and after the stored batch.
        finally:
            event.remove(writer.engine, "before_cursor_execute", record_sql)


@pytest.mark.parametrize("phase", ["issue", "validate"])
def test_postgres_basis_batch_rejects_formal_revoke_during_scope_resolution(
    isolated_migration_schema: PostgresRoles,
    phase: str,
) -> None:
    from concurrent.futures import ThreadPoolExecutor

    api = _load_reader_api()
    marker = f"batch-revoke-{phase}"
    with _writer_environment(isolated_migration_schema, minimal_owner_facts=True) as writer:
        duty, scope = _create_duty_and_scope(writer, marker=marker)
        grant = writer.writer.create_grant(
            _grant_command(writer, duty=duty, scope=scope, marker=marker)
        )
        reader = _reader_harness(api, writer, _DependencyState())
        at = AS_OF + timedelta(hours=1)
        query = _query(api, writer, marker=marker, scope_id=scope.target_id, as_of=at)
        basis = reader.basis_service.issue_many((query,))[0]
        assert not isinstance(basis, Exception)
        revoked = []

        def revoke():
            return writer.writer.revoke_grant(
                writer.api.RevokeGrantCommand(
                    school_id=writer.facts.school_id,
                    grant_id=grant.target_id,
                    expected_version=1,
                    effective_at=at,
                    actor=_actor(writer, reason="test-only revoke during batched owner read"),
                    idempotency_key=f"sf5a:{marker}:revoke",
                )
            )

        def resolve_many(requests):
            values = tuple(reader.scope_resolver.resolve(request) for request in requests)
            if not revoked:
                # NullPool plus a separate thread guarantees an independent
                # writer connection; its formal commit occurs after stored read
                # and before issuance/validation's final epoch read.
                assert isinstance(writer.engine.pool, NullPool)
                with ThreadPoolExecutor(max_workers=1) as worker:
                    revoked.append(worker.submit(revoke).result(timeout=15))
            return values

        reader.reader._scope_owner_resolvers = {
            "test-only.sf5a": SimpleNamespace(resolve_many=resolve_many)
        }
        results = (
            reader.basis_service.issue_many((query, query))
            if phase == "issue"
            else reader.basis_service.validate_many((basis, basis), as_of=at)
        )
        error_type = (
            api.GovernanceAuthorityUnavailable if phase == "issue" else api.GovernanceBasisInvalid
        )
        assert len(revoked) == 1 and revoked[0].assignment_epoch == basis.assignment_epoch + 1
        assert len(results) == 2 and all(isinstance(value, error_type) for value in results)
        assert [value.reason for value in results] == [
            "assignment_epoch_changed",
            "assignment_epoch_changed",
        ]
        subsequent = reader.basis_service.issue_many((query, query))
        assert all(isinstance(value, api.GovernanceAuthorityUnavailable) for value in subsequent)
        assert [value.reason for value in subsequent] == ["grant_inactive", "grant_inactive"]
        stale = reader.basis_service.validate_many((basis,), as_of=at)
        assert isinstance(stale[0], api.GovernanceBasisInvalid)
        assert stale[0].reason == "assignment_epoch_changed"
