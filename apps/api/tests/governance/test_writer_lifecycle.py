from __future__ import annotations

import inspect
import json
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from importlib import import_module
from threading import Barrier
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, event, text
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.orm import Session, sessionmaker

from app.composition.governance_dependencies import SqlAlchemyGovernanceDependencyAdapter
from app.db.uow import CommitOutcomeUnknown
from app.platform.governance.contracts import (
    AssignmentAuthoritySnapshot,
    RootAuthoritySnapshot,
    ScopeResolutionRequest,
    ScopeResolutionSnapshot,
)
from app.platform.governance.ports import GovernanceDependencyPort
from app.platform.school_facts.application import SchoolFactChangeService
from tests.master_data.conftest import PostgresRoles
from tests.school_facts.test_sf2_transaction import (
    all_fact_kinds_change_set,
    context_for,
    sf2_environment,
)

AS_OF = datetime(2026, 8, 23, 8, tzinfo=UTC)
FUTURE = AS_OF + timedelta(days=7)
PLANNED_END = AS_OF + timedelta(days=90)
UPDATED_PLANNED_END = AS_OF + timedelta(days=120)

pytestmark = pytest.mark.integration


@dataclass(frozen=True, slots=True)
class _WriterApi:
    GovernanceWriter: type[Any]
    GovernanceActorContext: type[Any]
    CreateDutyCommand: type[Any]
    CreateScopeCommand: type[Any]
    CreateGrantCommand: type[Any]
    UpdateGrantCommand: type[Any]
    EndGrantCommand: type[Any]
    RevokeGrantCommand: type[Any]
    GovernanceAuthorizationDenied: type[Exception]
    GovernanceIdempotencyConflict: type[Exception]
    GovernanceVersionConflict: type[Exception]
    GovernanceTerminalGrant: type[Exception]
    GovernanceReferenceRejected: type[Exception]


def _load_writer_api() -> _WriterApi:
    """Load the production API lazily so all five intended RED nodes collect."""

    contracts = import_module("app.platform.governance.contracts")
    application = import_module("app.platform.governance.application")
    return _WriterApi(
        GovernanceWriter=application.GovernanceWriter,
        GovernanceActorContext=contracts.GovernanceActorContext,
        CreateDutyCommand=contracts.CreateDutyCommand,
        CreateScopeCommand=contracts.CreateScopeCommand,
        CreateGrantCommand=contracts.CreateGrantCommand,
        UpdateGrantCommand=contracts.UpdateGrantCommand,
        EndGrantCommand=contracts.EndGrantCommand,
        RevokeGrantCommand=contracts.RevokeGrantCommand,
        GovernanceAuthorizationDenied=application.GovernanceAuthorizationDenied,
        GovernanceIdempotencyConflict=application.GovernanceIdempotencyConflict,
        GovernanceVersionConflict=application.GovernanceVersionConflict,
        GovernanceTerminalGrant=application.GovernanceTerminalGrant,
        GovernanceReferenceRejected=application.GovernanceReferenceRejected,
    )


def test_governance_writer_api_contract_is_available() -> None:
    writer_type = _load_writer_api().GovernanceWriter
    assert writer_type is not None
    assert {
        "dependency_port",
        "scope_owner_resolvers",
    } <= inspect.signature(writer_type).parameters.keys()


@dataclass(slots=True)
class _RecordingAuthorizer:
    allowed: bool = True
    requests: list[object] | None = None

    def __post_init__(self) -> None:
        self.requests = []

    def authorize(self, request: object) -> bool:
        assert self.requests is not None
        self.requests.append(request)
        return self.allowed


class _OwnerDependencyProbe:
    """Test-only wrapper around the real School Facts owner adapter."""

    def __init__(self, delegate: GovernanceDependencyPort, *, failure: str | None = None) -> None:
        self._delegate = delegate
        self._failure = failure
        self.assignment_reads: list[datetime] = []
        self.person_reads: list[datetime] = []
        self.organization_reads: list[datetime] = []

    def read_assignment(
        self, *, school_id: UUID, assignment_id: UUID, as_of: datetime
    ) -> AssignmentAuthoritySnapshot | None:
        self.assignment_reads.append(as_of)
        value = self._delegate.read_assignment(
            school_id=school_id,
            assignment_id=assignment_id,
            as_of=as_of,
        )
        if value is not None and self._failure == "ended_assignment":
            return replace(value, valid_until=as_of)
        return value

    def read_person_root(
        self, *, school_id: UUID, person_id: UUID, as_of: datetime
    ) -> RootAuthoritySnapshot | None:
        self.person_reads.append(as_of)
        value = self._delegate.read_person_root(
            school_id=school_id,
            person_id=person_id,
            as_of=as_of,
        )
        if value is not None and self._failure == "inactive_person":
            return replace(value, is_effective=False)
        return value

    def read_organization_root(
        self, *, school_id: UUID, organization_id: UUID, as_of: datetime
    ) -> RootAuthoritySnapshot | None:
        self.organization_reads.append(as_of)
        if self._failure == "missing_organization":
            return None
        return self._delegate.read_organization_root(
            school_id=school_id,
            organization_id=organization_id,
            as_of=as_of,
        )


class _TestOnlyScopeResolver:
    """Resolves only the explicit, fictitious SF5A Scope document used in tests."""

    def __init__(self, *, failure: str | None = None) -> None:
        self._failure = failure
        self.requests: list[ScopeResolutionRequest] = []

    def resolve(self, request: ScopeResolutionRequest) -> ScopeResolutionSnapshot | None:
        self.requests.append(request)
        if self._failure == "owner_error":
            raise RuntimeError("test-only Scope owner failure")
        reference = request.reference_document.get("school_id")
        version = request.reference_document.get("owner_version")
        assert isinstance(reference, str)
        assert isinstance(version, int)
        return ScopeResolutionSnapshot(
            school_id=request.school_id,
            owner=(
                "test-only.wrong-owner"
                if self._failure == "wrong_owner_target"
                else request.schema_owner
            ),
            reference=reference,
            version=version,
            is_effective=True,
            is_ambiguous=False,
            valid_until=None,
        )


@dataclass(frozen=True, slots=True)
class _Facts:
    school_id: UUID
    user_id: int
    person_id: UUID
    assignment_id: UUID


@dataclass(slots=True)
class _Harness:
    api: _WriterApi
    factory: sessionmaker[Session]
    engine: Engine
    writer: Any
    authorizer: _RecordingAuthorizer
    facts: _Facts
    owner_adapter: GovernanceDependencyPort
    dependency_probe: _OwnerDependencyProbe
    scope_resolver: _TestOnlyScopeResolver


def _minimal_owner_change_set(*, school_source_key: str):
    """C4 needs only these owner roots; the base school already exists."""
    original = all_fact_kinds_change_set(school_source_key=school_source_key)
    keys = {"person:parent", "organization:dorm", "assignment:parent"}
    changes = tuple(item for item in original.changes if item.source_key in keys)
    assert {item.source_key for item in changes} == keys
    return original.model_copy(update={"changes": changes})


def _seed_formal_owner_facts(
    factory: sessionmaker[Session],
    persistence: Any,
    actor_facts: tuple[UUID, int, UUID, UUID],
    *,
    minimal_owner_facts: bool = False,
) -> _Facts:
    school_id, user_id, actor_person_id, actor_assignment_id = actor_facts
    with factory() as session:
        school_source_key = str(
            session.execute(
                text("SELECT source_key FROM schools WHERE id = :school_id"),
                {"school_id": school_id},
            ).scalar_one()
        )
    service = SchoolFactChangeService(reader=persistence, writer=persistence, clock=lambda: AS_OF)
    change_set_factory = (
        _minimal_owner_change_set if minimal_owner_facts else all_fact_kinds_change_set
    )
    change_set = change_set_factory(school_source_key=school_source_key)
    context = context_for(
        school_id=school_id,
        user_id=user_id,
        person_id=actor_person_id,
        assignment_id=actor_assignment_id,
        key=f"sf5a-owner-seed:{uuid4().hex}",
    )
    session_binding = f"sf5a-owner-seed-{uuid4().hex}"
    receipt = service.prepare_confirmation(
        change_set=change_set,
        context=context,
        session_binding=session_binding,
    )
    confirmed = service.confirm(
        credential=receipt.credential,
        change_set=change_set,
        context=context,
        session_binding=session_binding,
    )
    assert confirmed.changed_count == len(change_set.changes)
    with factory() as session:
        target = (
            session.execute(
                text(
                    "SELECT assignment.id, assignment.person_id "
                    "FROM staff_assignments AS assignment "
                    "WHERE assignment.school_id = :school_id "
                    "AND assignment.source_key = 'assignment:parent'"
                ),
                {"school_id": school_id},
            )
            .mappings()
            .one()
        )
    return _Facts(
        school_id=school_id,
        user_id=user_id,
        person_id=UUID(str(target["person_id"])),
        assignment_id=UUID(str(target["id"])),
    )


@contextmanager
def _writer_environment(
    roles: PostgresRoles, *, minimal_owner_facts: bool = False
) -> Iterator[_Harness]:
    api = _load_writer_api()
    with sf2_environment(roles) as (factory, persistence, actor_facts):
        facts = _seed_formal_owner_facts(
            factory, persistence, actor_facts, minimal_owner_facts=minimal_owner_facts
        )
        engine = cast(Engine, factory.kw["bind"])
        authorizer = _RecordingAuthorizer()
        owner_adapter = SqlAlchemyGovernanceDependencyAdapter(factory)
        dependency_probe = _OwnerDependencyProbe(owner_adapter)
        scope_resolver = _TestOnlyScopeResolver()
        writer = api.GovernanceWriter(
            session_factory=factory,
            authorizer=authorizer,
            clock=lambda: AS_OF,
            dependency_port=dependency_probe,
            scope_owner_resolvers={"test-only.sf5a": scope_resolver},
        )
        yield _Harness(
            api=api,
            factory=factory,
            engine=engine,
            writer=writer,
            authorizer=authorizer,
            facts=facts,
            owner_adapter=owner_adapter,
            dependency_probe=dependency_probe,
            scope_resolver=scope_resolver,
        )


def _actor(harness: _Harness, *, reason: str) -> Any:
    return harness.api.GovernanceActorContext(
        school_id=harness.facts.school_id,
        actor_kind="assignment",
        actor_reference=f"test-only:assignment:{harness.facts.assignment_id}",
        actor_assignment_id=harness.facts.assignment_id,
        actor_assignment_version=1,
        request_id=uuid4(),
        reason=reason,
    )


def _scope_document(school_id: UUID) -> dict[str, object]:
    return {
        "fixture": "sf5a-test-only",
        "owner_kind": "school",
        "school_id": str(school_id),
        "owner_version": 1,
    }


def _document_sha256(document: dict[str, object]) -> str:
    canonical = json.dumps(
        document,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return sha256(canonical).hexdigest()


def _create_duty_and_scope(harness: _Harness, *, marker: str) -> tuple[Any, Any]:
    duty = harness.writer.create_duty(
        harness.api.CreateDutyCommand(
            school_id=harness.facts.school_id,
            duty_code=f"test_only.{marker}.duty",
            display_name=f"虚构测试职责 {marker}",
            description="仅用于 SF5A Writer 机制验证，不是生产职责词典。",
            effective_from=AS_OF - timedelta(days=1),
            actor=_actor(harness, reason="create test-only duty"),
            idempotency_key=f"sf5a:{marker}:duty",
        )
    )
    document = _scope_document(harness.facts.school_id)
    scope = harness.writer.create_scope(
        harness.api.CreateScopeCommand(
            school_id=harness.facts.school_id,
            scope_key=f"test-only:{marker}:scope",
            schema_owner="test-only.sf5a",
            schema_name="fictitious-school-scope",
            schema_version=1,
            reference_document=document,
            reference_sha256=_document_sha256(document),
            effective_from=AS_OF - timedelta(days=1),
            actor=_actor(harness, reason="create typed test-only scope"),
            idempotency_key=f"sf5a:{marker}:scope",
        )
    )
    return duty, scope


def _grant_command(
    harness: _Harness,
    *,
    duty: Any,
    scope: Any,
    marker: str,
    idempotency_key: str | None = None,
    basis_reference: str | None = None,
) -> Any:
    return harness.api.CreateGrantCommand(
        school_id=harness.facts.school_id,
        assignment_id=harness.facts.assignment_id,
        duty_id=duty.target_id,
        duty_version=duty.target_version,
        scope_id=scope.target_id,
        scope_version=scope.target_version,
        basis_authority="test-only.sf5a",
        basis_reference=basis_reference or f"fixture://sf5a/{marker}/appointment",
        valid_from=AS_OF,
        effective_from=AS_OF,
        planned_valid_to=PLANNED_END,
        actor=_actor(harness, reason="create test-only Governance Grant"),
        idempotency_key=idempotency_key or f"sf5a:{marker}:grant",
    )


def _writer_with_owner_dependencies(
    harness: _Harness,
    *,
    dependency_port: GovernanceDependencyPort,
    scope_resolver: _TestOnlyScopeResolver,
) -> Any:
    return harness.api.GovernanceWriter(
        session_factory=harness.factory,
        authorizer=harness.authorizer,
        clock=lambda: AS_OF,
        dependency_port=dependency_port,
        scope_owner_resolvers={"test-only.sf5a": scope_resolver},
    )


def _rows(
    factory: sessionmaker[Session],
    statement: str,
    parameters: dict[str, object] | None = None,
) -> list[dict[str, Any]]:
    with factory() as session:
        return [
            dict(row) for row in session.execute(text(statement), parameters or {}).mappings().all()
        ]


def _scalar(
    factory: sessionmaker[Session],
    statement: str,
    parameters: dict[str, object] | None = None,
) -> Any:
    with factory() as session:
        return session.execute(text(statement), parameters or {}).scalar_one()


def _governance_counts(factory: sessionmaker[Session]) -> tuple[int, ...]:
    names = (
        "governance_duties",
        "governance_duty_versions",
        "governance_scopes",
        "governance_scope_versions",
        "governance_grants",
        "governance_grant_versions",
        "governance_assignment_epochs",
        "governance_command_receipts",
        "governance_audit_events",
    )
    return tuple(int(_scalar(factory, f"SELECT count(*) FROM {name}")) for name in names)


def _race_writer_calls(
    harness: _Harness,
    first: Callable[[], Any],
    second: Callable[[], Any],
) -> tuple[list[Any], list[Exception], list[str]]:
    barrier = Barrier(2)
    receipt_inserts: list[str] = []

    def synchronize_receipt_insert(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        normalized = statement.lstrip().lower()
        if normalized.startswith("insert") and "governance_command_receipts" in normalized:
            receipt_inserts.append(statement)
            barrier.wait(timeout=10)

    event.listen(harness.engine, "before_cursor_execute", synchronize_receipt_insert)
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(first), executor.submit(second)]
            results: list[Any] = []
            errors: list[Exception] = []
            for future in futures:
                try:
                    results.append(future.result(timeout=20))
                except Exception as exc:
                    errors.append(exc)
    finally:
        event.remove(harness.engine, "before_cursor_execute", synchronize_receipt_insert)
    return results, errors, receipt_inserts


@pytest.mark.parametrize(
    "effective_from",
    (AS_OF, FUTURE),
    ids=("current", "future-effective"),
)
def test_grant_creation_uses_owner_interfaces_at_the_grant_effective_instant(
    isolated_migration_schema: PostgresRoles,
    effective_from: datetime,
) -> None:
    with _writer_environment(isolated_migration_schema) as harness:
        duty, scope = _create_duty_and_scope(harness, marker=f"owner-{effective_from.date()}")
        result = harness.writer.create_grant(
            replace(
                _grant_command(
                    harness,
                    duty=duty,
                    scope=scope,
                    marker=f"owner-{effective_from.date()}",
                ),
                valid_from=effective_from,
                effective_from=effective_from,
            )
        )

        assert result.assignment_epoch == 1
        assert harness.dependency_probe.assignment_reads == [effective_from]
        assert harness.dependency_probe.person_reads == [effective_from]
        assert harness.dependency_probe.organization_reads == [effective_from]
        assert [request.as_of for request in harness.scope_resolver.requests] == [effective_from]


@pytest.mark.parametrize(
    ("dependency_failure", "scope_failure"),
    (
        ("ended_assignment", None),
        ("inactive_person", None),
        ("missing_organization", None),
        (None, "wrong_owner_target"),
        (None, "owner_error"),
    ),
    ids=(
        "ended-assignment",
        "inactive-person",
        "missing-organization",
        "wrong-scope-owner-target",
        "scope-owner-error",
    ),
)
def test_grant_creation_rejects_invalid_owner_dependencies_without_authority_writes(
    isolated_migration_schema: PostgresRoles,
    dependency_failure: str | None,
    scope_failure: str | None,
) -> None:
    with _writer_environment(isolated_migration_schema) as harness:
        marker = f"reject-{dependency_failure or scope_failure}"
        duty, scope = _create_duty_and_scope(harness, marker=marker)
        writer = _writer_with_owner_dependencies(
            harness,
            dependency_port=_OwnerDependencyProbe(
                harness.owner_adapter,
                failure=dependency_failure,
            ),
            scope_resolver=_TestOnlyScopeResolver(failure=scope_failure),
        )
        before = _governance_counts(harness.factory)

        with pytest.raises(harness.api.GovernanceReferenceRejected):
            writer.create_grant(
                _grant_command(
                    harness,
                    duty=duty,
                    scope=scope,
                    marker=marker,
                )
            )

        assert _governance_counts(harness.factory) == before
        assert _scalar(harness.factory, "SELECT count(*) FROM governance_grants") == 0
        assert _scalar(harness.factory, "SELECT count(*) FROM governance_assignment_epochs") == 0
        assert _scalar(harness.factory, "SELECT count(*) FROM governance_command_receipts") == 2
        assert _scalar(harness.factory, "SELECT count(*) FROM governance_audit_events") == 2


def test_writer_creates_test_only_duty_typed_scope_and_assignment_bound_grant_atomically(
    isolated_migration_schema: PostgresRoles,
) -> None:
    with _writer_environment(isolated_migration_schema) as harness:
        duty, scope = _create_duty_and_scope(harness, marker="create")
        grant = harness.writer.create_grant(
            _grant_command(harness, duty=duty, scope=scope, marker="create")
        )

        assert (duty.target_version, scope.target_version, grant.target_version) == (1, 1, 1)
        assert grant.assignment_epoch == 1
        assert grant.replayed is False
        assert len(harness.authorizer.requests or []) == 3

        duty_row = _rows(
            harness.factory,
            "SELECT root.duty_code, root.current_version, version.display_name, "
            "version.is_active FROM governance_duties AS root "
            "JOIN governance_duty_versions AS version "
            "ON version.school_id = root.school_id AND version.duty_id = root.id "
            "WHERE root.id = :id",
            {"id": duty.target_id},
        )
        assert duty_row == [
            {
                "duty_code": "test_only.create.duty",
                "current_version": 1,
                "display_name": "虚构测试职责 create",
                "is_active": True,
            }
        ]

        scope_row = _rows(
            harness.factory,
            "SELECT schema_owner, schema_name, schema_version, reference_document, "
            "reference_sha256 FROM governance_scope_versions WHERE scope_id = :id",
            {"id": scope.target_id},
        )
        document = _scope_document(harness.facts.school_id)
        assert scope_row == [
            {
                "schema_owner": "test-only.sf5a",
                "schema_name": "fictitious-school-scope",
                "schema_version": 1,
                "reference_document": document,
                "reference_sha256": _document_sha256(document),
            }
        ]

        grant_row = _rows(
            harness.factory,
            "SELECT root.assignment_id, root.duty_id, root.current_version, "
            "version.scope_id, version.scope_version, version.basis_authority, "
            "version.basis_reference, version.transition_kind "
            "FROM governance_grants AS root JOIN governance_grant_versions AS version "
            "ON version.school_id = root.school_id AND version.grant_id = root.id "
            "WHERE root.id = :id",
            {"id": grant.target_id},
        )
        assert grant_row == [
            {
                "assignment_id": harness.facts.assignment_id,
                "duty_id": duty.target_id,
                "current_version": 1,
                "scope_id": scope.target_id,
                "scope_version": 1,
                "basis_authority": "test-only.sf5a",
                "basis_reference": "fixture://sf5a/create/appointment",
                "transition_kind": "create",
            }
        ]
        assert (
            _scalar(
                harness.factory,
                "SELECT epoch FROM governance_assignment_epochs "
                "WHERE school_id = :school_id AND assignment_id = :assignment_id",
                {
                    "school_id": harness.facts.school_id,
                    "assignment_id": harness.facts.assignment_id,
                },
            )
            == 1
        )
        assert _scalar(harness.factory, "SELECT count(*) FROM governance_command_receipts") == 3
        assert _scalar(harness.factory, "SELECT count(*) FROM governance_audit_events") == 3

        columns = {
            column["name"]
            for column in sa_inspect(harness.engine).get_columns("governance_grant_versions")
        }
        assert {"basis_authority", "basis_reference"} <= columns
        assert not {"basis_body", "document_body", "attachment", "content_text"} & columns


def test_future_effective_update_preserves_current_slice_and_immutable_bindings(
    isolated_migration_schema: PostgresRoles,
) -> None:
    with _writer_environment(isolated_migration_schema) as harness:
        duty, scope = _create_duty_and_scope(harness, marker="future-update")
        grant = harness.writer.create_grant(
            _grant_command(harness, duty=duty, scope=scope, marker="future-update")
        )

        forbidden_update_fields = {"assignment_id", "duty_id", "scope_id", "scope_version"}
        assert forbidden_update_fields.isdisjoint(
            inspect.signature(harness.api.UpdateGrantCommand).parameters
        )
        updated = harness.writer.update_grant(
            harness.api.UpdateGrantCommand(
                school_id=harness.facts.school_id,
                grant_id=grant.target_id,
                expected_version=1,
                effective_from=FUTURE,
                planned_valid_to=UPDATED_PLANNED_END,
                basis_authority="test-only.sf5a",
                basis_reference="fixture://sf5a/future-update/amendment",
                actor=_actor(harness, reason="schedule future Grant content"),
                idempotency_key="sf5a:future-update:update",
            )
        )

        assert (updated.target_version, updated.assignment_epoch) == (2, 2)
        assert (
            _scalar(
                harness.factory,
                "SELECT current_version FROM governance_grants WHERE id = :id",
                {"id": grant.target_id},
            )
            == 2
        )
        current = _rows(
            harness.factory,
            "SELECT version, planned_valid_to, basis_reference "
            "FROM governance_grant_versions "
            "WHERE grant_id = :id AND effective_from <= :as_of "
            "ORDER BY version DESC LIMIT 1",
            {"id": grant.target_id, "as_of": AS_OF},
        )
        assert current == [
            {
                "version": 1,
                "planned_valid_to": PLANNED_END,
                "basis_reference": "fixture://sf5a/future-update/appointment",
            }
        ]
        future = _rows(
            harness.factory,
            "SELECT version, effective_from, planned_valid_to, basis_reference, "
            "duty_id, scope_id FROM governance_grant_versions "
            "WHERE grant_id = :id ORDER BY version",
            {"id": grant.target_id},
        )
        assert future[1] == {
            "version": 2,
            "effective_from": FUTURE,
            "planned_valid_to": UPDATED_PLANNED_END,
            "basis_reference": "fixture://sf5a/future-update/amendment",
            "duty_id": duty.target_id,
            "scope_id": scope.target_id,
        }


def test_formal_end_and_prospective_revoke_are_distinct_terminal_histories(
    isolated_migration_schema: PostgresRoles,
) -> None:
    with _writer_environment(isolated_migration_schema) as harness:
        duty, scope = _create_duty_and_scope(harness, marker="terminal")
        ended_grant = harness.writer.create_grant(
            _grant_command(harness, duty=duty, scope=scope, marker="formal-end")
        )
        revoked_grant = harness.writer.create_grant(
            _grant_command(harness, duty=duty, scope=scope, marker="prospective-revoke")
        )
        terminal_at = AS_OF + timedelta(days=10)

        ended = harness.writer.end_grant(
            harness.api.EndGrantCommand(
                school_id=harness.facts.school_id,
                grant_id=ended_grant.target_id,
                expected_version=1,
                effective_at=terminal_at,
                actor=_actor(harness, reason="formal natural end"),
                idempotency_key="sf5a:formal-end:end",
            )
        )
        revoked = harness.writer.revoke_grant(
            harness.api.RevokeGrantCommand(
                school_id=harness.facts.school_id,
                grant_id=revoked_grant.target_id,
                expected_version=1,
                effective_at=terminal_at,
                actor=_actor(harness, reason="prospective formal withdrawal"),
                idempotency_key="sf5a:prospective-revoke:revoke",
            )
        )

        assert (ended.target_version, revoked.target_version) == (2, 2)
        assert (ended.assignment_epoch, revoked.assignment_epoch) == (3, 4)
        terminal_rows = _rows(
            harness.factory,
            "SELECT grant_id, version, transition_kind, planned_valid_to, "
            "formal_ended_at, revoked_at FROM governance_grant_versions "
            "WHERE grant_id IN (:ended_id, :revoked_id) ORDER BY grant_id, version",
            {"ended_id": ended_grant.target_id, "revoked_id": revoked_grant.target_id},
        )
        assert len(terminal_rows) == 4
        ended_terminal = next(
            row
            for row in terminal_rows
            if row["grant_id"] == ended_grant.target_id and row["version"] == 2
        )
        revoked_terminal = next(
            row
            for row in terminal_rows
            if row["grant_id"] == revoked_grant.target_id and row["version"] == 2
        )
        assert ended_terminal == {
            "grant_id": ended_grant.target_id,
            "version": 2,
            "transition_kind": "end",
            "planned_valid_to": PLANNED_END,
            "formal_ended_at": terminal_at,
            "revoked_at": None,
        }
        assert revoked_terminal == {
            "grant_id": revoked_grant.target_id,
            "version": 2,
            "transition_kind": "revoke",
            "planned_valid_to": PLANNED_END,
            "formal_ended_at": None,
            "revoked_at": terminal_at,
        }
        assert not hasattr(harness.writer, "reactivate_grant")

        with pytest.raises(harness.api.GovernanceTerminalGrant):
            harness.writer.update_grant(
                harness.api.UpdateGrantCommand(
                    school_id=harness.facts.school_id,
                    grant_id=revoked_grant.target_id,
                    expected_version=2,
                    effective_from=terminal_at + timedelta(days=1),
                    planned_valid_to=UPDATED_PLANNED_END,
                    basis_authority="test-only.sf5a",
                    basis_reference="fixture://must-not-reopen",
                    actor=_actor(harness, reason="must fail after revoke"),
                    idempotency_key="sf5a:revoke:must-not-reopen",
                )
            )
        with pytest.raises(harness.api.GovernanceTerminalGrant):
            harness.writer.revoke_grant(
                harness.api.RevokeGrantCommand(
                    school_id=harness.facts.school_id,
                    grant_id=ended_grant.target_id,
                    expected_version=2,
                    effective_at=terminal_at + timedelta(days=1),
                    actor=_actor(harness, reason="must fail after end"),
                    idempotency_key="sf5a:end:must-not-continue",
                )
            )


def test_idempotency_version_conflict_and_authorizer_denial_are_deterministic(
    isolated_migration_schema: PostgresRoles,
) -> None:
    with _writer_environment(isolated_migration_schema) as harness:
        duty, scope = _create_duty_and_scope(harness, marker="idempotency")
        command = _grant_command(
            harness,
            duty=duty,
            scope=scope,
            marker="idempotency",
            idempotency_key="sf5a:idempotency:grant",
        )
        created = harness.writer.create_grant(command)
        replay = harness.writer.create_grant(command)
        assert replay.target_id == created.target_id
        assert replay.target_version == created.target_version == 1
        assert replay.assignment_epoch == created.assignment_epoch == 1
        assert replay.replayed is True
        assert (
            _scalar(
                harness.factory,
                "SELECT count(*) FROM governance_grant_versions WHERE grant_id = :id",
                {"id": created.target_id},
            )
            == 1
        )

        conflicting = _grant_command(
            harness,
            duty=duty,
            scope=scope,
            marker="idempotency",
            idempotency_key="sf5a:idempotency:grant",
            basis_reference="fixture://different-payload",
        )
        with pytest.raises(harness.api.GovernanceIdempotencyConflict):
            harness.writer.create_grant(conflicting)

        harness.writer.update_grant(
            harness.api.UpdateGrantCommand(
                school_id=harness.facts.school_id,
                grant_id=created.target_id,
                expected_version=1,
                effective_from=FUTURE,
                planned_valid_to=UPDATED_PLANNED_END,
                basis_authority="test-only.sf5a",
                basis_reference="fixture://sf5a/idempotency/update",
                actor=_actor(harness, reason="first CAS update"),
                idempotency_key="sf5a:idempotency:update-1",
            )
        )
        with pytest.raises(harness.api.GovernanceVersionConflict):
            harness.writer.update_grant(
                harness.api.UpdateGrantCommand(
                    school_id=harness.facts.school_id,
                    grant_id=created.target_id,
                    expected_version=1,
                    effective_from=FUTURE + timedelta(days=1),
                    planned_valid_to=UPDATED_PLANNED_END,
                    basis_authority="test-only.sf5a",
                    basis_reference="fixture://sf5a/idempotency/stale",
                    actor=_actor(harness, reason="stale CAS must fail"),
                    idempotency_key="sf5a:idempotency:update-stale",
                )
            )

        before = _governance_counts(harness.factory)
        harness.authorizer.allowed = False
        with pytest.raises(harness.api.GovernanceAuthorizationDenied):
            harness.writer.create_duty(
                harness.api.CreateDutyCommand(
                    school_id=harness.facts.school_id,
                    duty_code="test_only.denied",
                    display_name="不得写入的测试职责",
                    description=None,
                    effective_from=AS_OF,
                    actor=_actor(harness, reason="fake authorizer denies"),
                    idempotency_key="sf5a:denied:duty",
                )
            )
        assert _governance_counts(harness.factory) == before


def test_receipt_epoch_or_audit_failure_rolls_back_the_entire_command(
    isolated_migration_schema: PostgresRoles,
) -> None:
    with _writer_environment(isolated_migration_schema) as harness:
        duty, scope = _create_duty_and_scope(harness, marker="rollback")

        for failure_table in (
            "governance_command_receipts",
            "governance_assignment_epochs",
            "governance_audit_events",
        ):
            before = _governance_counts(harness.factory)

            def fail_target_write(
                _connection: object,
                _cursor: object,
                statement: str,
                _parameters: object,
                _context: object,
                _executemany: bool,
                *,
                target: str = failure_table,
            ) -> None:
                normalized = statement.lstrip().lower()
                if normalized.startswith(("insert", "update")) and target in normalized:
                    raise RuntimeError(f"injected Governance write failure: {target}")

            event.listen(harness.engine, "before_cursor_execute", fail_target_write)
            try:
                with pytest.raises(RuntimeError, match=failure_table):
                    harness.writer.create_grant(
                        _grant_command(
                            harness,
                            duty=duty,
                            scope=scope,
                            marker=f"rollback-{failure_table}",
                        )
                    )
            finally:
                event.remove(harness.engine, "before_cursor_execute", fail_target_write)

            assert _governance_counts(harness.factory) == before


@pytest.mark.parametrize(
    "failure_point",
    ("audit", "assignment_epoch", "final_receipt_flush"),
)
def test_revoke_failure_rolls_back_grant_epoch_audit_and_receipt_atomically(
    failure_point: str,
    isolated_migration_schema: PostgresRoles,
) -> None:
    with _writer_environment(isolated_migration_schema) as harness:
        duty, scope = _create_duty_and_scope(
            harness,
            marker=f"revoke-rollback-{failure_point}",
        )
        grant = harness.writer.create_grant(
            _grant_command(
                harness,
                duty=duty,
                scope=scope,
                marker=f"revoke-rollback-{failure_point}",
            )
        )
        command = harness.api.RevokeGrantCommand(
            school_id=harness.facts.school_id,
            grant_id=grant.target_id,
            expected_version=1,
            effective_at=AS_OF + timedelta(days=1),
            actor=_actor(harness, reason=f"inject revoke {failure_point} failure"),
            idempotency_key=f"sf5a:revoke-rollback:{failure_point}",
        )
        before_audits = int(
            _scalar(harness.factory, "SELECT count(*) FROM governance_audit_events")
        )
        before_receipts = int(
            _scalar(harness.factory, "SELECT count(*) FROM governance_command_receipts")
        )
        before_epoch = int(
            _scalar(
                harness.factory,
                "SELECT epoch FROM governance_assignment_epochs "
                "WHERE school_id = :school_id AND assignment_id = :assignment_id",
                {
                    "school_id": harness.facts.school_id,
                    "assignment_id": harness.facts.assignment_id,
                },
            )
        )
        grant_mutation_flushed = False
        failure_triggered = False

        def fail_after_grant_mutation(
            _connection: object,
            _cursor: object,
            statement: str,
            _parameters: object,
            _context: object,
            _executemany: bool,
        ) -> None:
            nonlocal failure_triggered, grant_mutation_flushed
            normalized = " ".join(statement.lstrip().lower().split())
            if normalized.startswith("update governance_grants") or (
                normalized.startswith("insert into governance_grant_versions")
                and "transition_kind" in normalized
            ):
                grant_mutation_flushed = True
            is_target = (
                (
                    failure_point == "audit"
                    and normalized.startswith("insert into governance_audit_events")
                )
                or (
                    failure_point == "assignment_epoch"
                    and normalized.startswith("insert into governance_assignment_epochs")
                    and "on conflict" in normalized
                    and "do update" in normalized
                )
                or (
                    failure_point == "final_receipt_flush"
                    and normalized.startswith("update governance_command_receipts")
                )
            )
            if is_target:
                assert grant_mutation_flushed is True
                failure_triggered = True
                raise RuntimeError(f"injected revoke {failure_point} failure")

        event.listen(harness.engine, "before_cursor_execute", fail_after_grant_mutation)
        try:
            with pytest.raises(RuntimeError, match=f"revoke {failure_point}"):
                harness.writer.revoke_grant(command)
        finally:
            event.remove(
                harness.engine,
                "before_cursor_execute",
                fail_after_grant_mutation,
            )

        assert failure_triggered is True
        assert (
            _scalar(
                harness.factory,
                "SELECT current_version FROM governance_grants WHERE id = :grant_id",
                {"grant_id": grant.target_id},
            )
            == 1
        )
        assert _rows(
            harness.factory,
            "SELECT version, transition_kind, formal_ended_at, revoked_at "
            "FROM governance_grant_versions WHERE grant_id = :grant_id ORDER BY version",
            {"grant_id": grant.target_id},
        ) == [
            {
                "version": 1,
                "transition_kind": "create",
                "formal_ended_at": None,
                "revoked_at": None,
            }
        ]
        assert (
            _scalar(
                harness.factory,
                "SELECT epoch FROM governance_assignment_epochs "
                "WHERE school_id = :school_id AND assignment_id = :assignment_id",
                {
                    "school_id": harness.facts.school_id,
                    "assignment_id": harness.facts.assignment_id,
                },
            )
            == before_epoch
            == 1
        )
        assert (
            _scalar(harness.factory, "SELECT count(*) FROM governance_audit_events")
            == before_audits
        )
        assert (
            _scalar(harness.factory, "SELECT count(*) FROM governance_command_receipts")
            == before_receipts
        )
        assert (
            _rows(
                harness.factory,
                "SELECT status, target_id, target_version FROM governance_command_receipts "
                "WHERE command_name = 'governance.grant.revoke'",
            )
            == []
        )

        succeeded = harness.writer.revoke_grant(command)
        assert (succeeded.target_version, succeeded.assignment_epoch) == (2, 2)
        assert succeeded.replayed is False
        assert (
            _scalar(harness.factory, "SELECT count(*) FROM governance_audit_events")
            == before_audits + 1
        )
        assert (
            _scalar(harness.factory, "SELECT count(*) FROM governance_command_receipts")
            == before_receipts + 1
        )


def test_commit_response_loss_uses_new_read_only_transaction_and_verifies_grant(
    monkeypatch: pytest.MonkeyPatch,
    isolated_migration_schema: PostgresRoles,
) -> None:
    with _writer_environment(isolated_migration_schema) as harness:
        duty, scope = _create_duty_and_scope(harness, marker="commit-unknown")
        unit_of_work_module = import_module("app.platform.governance.uow")
        unit_of_work = unit_of_work_module.GovernanceUnitOfWork
        original_commit = unit_of_work.commit
        statements: list[str] = []
        checkouts = 0
        lost_once = False

        def record_checkout(_dbapi_connection: object, *_args: object) -> None:
            nonlocal checkouts
            checkouts += 1

        def record_statement(
            _connection: object,
            _cursor: object,
            statement: str,
            _parameters: object,
            _context: object,
            _executemany: bool,
        ) -> None:
            statements.append(statement)

        def commit_then_lose_response(self: object) -> None:
            nonlocal lost_once
            original_commit(self)
            if not lost_once:
                lost_once = True
                raise CommitOutcomeUnknown("simulate lost Governance commit acknowledgement")

        event.listen(harness.engine, "checkout", record_checkout)
        event.listen(harness.engine, "before_cursor_execute", record_statement)
        monkeypatch.setattr(unit_of_work, "commit", commit_then_lose_response)
        try:
            result = harness.writer.create_grant(
                _grant_command(
                    harness,
                    duty=duty,
                    scope=scope,
                    marker="commit-unknown",
                )
            )
        finally:
            event.remove(harness.engine, "before_cursor_execute", record_statement)
            event.remove(harness.engine, "checkout", record_checkout)

        assert lost_once is True
        assert result.replayed is True
        assert result.target_version == 1
        assert result.assignment_epoch == 1
        assert checkouts >= 2
        assert sum("SET TRANSACTION READ ONLY" in statement for statement in statements) == 1
        rendered_statements = "\n".join(statements).lower()
        assert "governance_command_receipts" in rendered_statements
        assert "governance_grants" in rendered_statements
        assert "governance_grant_versions" in rendered_statements
        assert (
            _scalar(
                harness.factory,
                "SELECT count(*) FROM governance_grant_versions WHERE grant_id = :id",
                {"id": result.target_id},
            )
            == 1
        )


def test_end_and_revoke_with_identical_fields_are_distinct_idempotent_commands(
    isolated_migration_schema: PostgresRoles,
) -> None:
    with _writer_environment(isolated_migration_schema) as harness:
        duty, scope = _create_duty_and_scope(harness, marker="command-discriminator")
        grant = harness.writer.create_grant(
            _grant_command(
                harness,
                duty=duty,
                scope=scope,
                marker="command-discriminator",
            )
        )
        actor = _actor(harness, reason="same fields, different Governance operation")
        terminal_at = AS_OF + timedelta(days=1)
        shared_key = "sf5a:command-discriminator:terminal"

        harness.writer.end_grant(
            harness.api.EndGrantCommand(
                school_id=harness.facts.school_id,
                grant_id=grant.target_id,
                expected_version=1,
                effective_at=terminal_at,
                actor=actor,
                idempotency_key=shared_key,
            )
        )
        with pytest.raises(harness.api.GovernanceIdempotencyConflict):
            harness.writer.revoke_grant(
                harness.api.RevokeGrantCommand(
                    school_id=harness.facts.school_id,
                    grant_id=grant.target_id,
                    expected_version=1,
                    effective_at=terminal_at,
                    actor=actor,
                    idempotency_key=shared_key,
                )
            )

        assert _rows(
            harness.factory,
            "SELECT transition_kind FROM governance_grant_versions "
            "WHERE grant_id = :id ORDER BY version",
            {"id": grant.target_id},
        ) == [{"transition_kind": "create"}, {"transition_kind": "end"}]


def test_transport_request_id_is_not_part_of_idempotent_business_payload(
    isolated_migration_schema: PostgresRoles,
) -> None:
    with _writer_environment(isolated_migration_schema) as harness:
        duty, scope = _create_duty_and_scope(harness, marker="transport-request")
        command = _grant_command(
            harness,
            duty=duty,
            scope=scope,
            marker="transport-request",
        )
        original_request_id = command.actor.request_id
        replay_request_id = uuid4()
        replay_command = replace(
            command,
            actor=replace(command.actor, request_id=replay_request_id),
        )

        created = harness.writer.create_grant(command)
        replayed = harness.writer.create_grant(replay_command)

        assert replay_request_id != original_request_id
        assert replayed.target_id == created.target_id
        assert replayed.replayed is True
        assert _rows(
            harness.factory,
            "SELECT receipt.request_id AS receipt_request_id, "
            "audit.request_id AS audit_request_id "
            "FROM governance_command_receipts AS receipt "
            "JOIN governance_audit_events AS audit "
            "ON audit.school_id = receipt.school_id "
            "AND audit.command_receipt_id = receipt.id "
            "WHERE receipt.target_id = :id",
            {"id": created.target_id},
        ) == [
            {
                "receipt_request_id": original_request_id,
                "audit_request_id": original_request_id,
            }
        ]


def test_concurrent_same_key_and_payload_converges_to_one_authoritative_result(
    isolated_migration_schema: PostgresRoles,
) -> None:
    with _writer_environment(isolated_migration_schema) as harness:
        duty, scope = _create_duty_and_scope(harness, marker="concurrent-replay")
        command = _grant_command(
            harness,
            duty=duty,
            scope=scope,
            marker="concurrent-replay",
        )

        results, errors, receipt_inserts = _race_writer_calls(
            harness,
            lambda: harness.writer.create_grant(command),
            lambda: harness.writer.create_grant(command),
        )

        assert errors == []
        assert len(results) == 2
        assert {result.target_id for result in results} == {results[0].target_id}
        assert sorted(result.replayed for result in results) == [False, True]
        assert len(receipt_inserts) == 2
        assert all(
            "ON CONFLICT" in statement.upper() and "DO NOTHING" in statement.upper()
            for statement in receipt_inserts
        )
        assert (
            _scalar(
                harness.factory,
                "SELECT count(*) FROM governance_grant_versions WHERE grant_id = :id",
                {"id": results[0].target_id},
            )
            == 1
        )


def test_concurrent_same_key_different_payload_returns_stable_conflict(
    isolated_migration_schema: PostgresRoles,
) -> None:
    with _writer_environment(isolated_migration_schema) as harness:
        duty, scope = _create_duty_and_scope(harness, marker="concurrent-conflict")
        first = _grant_command(
            harness,
            duty=duty,
            scope=scope,
            marker="concurrent-conflict",
        )
        second = replace(
            first,
            basis_reference="fixture://sf5a/concurrent-conflict/different-basis",
        )

        results, errors, receipt_inserts = _race_writer_calls(
            harness,
            lambda: harness.writer.create_grant(first),
            lambda: harness.writer.create_grant(second),
        )

        assert len(results) == 1
        assert len(errors) == 1
        assert isinstance(errors[0], harness.api.GovernanceIdempotencyConflict)
        assert len(receipt_inserts) == 2
        assert all(
            "ON CONFLICT" in statement.upper() and "DO NOTHING" in statement.upper()
            for statement in receipt_inserts
        )
        assert _scalar(harness.factory, "SELECT count(*) FROM governance_grants") == 1


@pytest.mark.parametrize("operation", ["end", "revoke"])
def test_terminal_transition_cannot_precede_latest_future_effective_slice(
    operation: str,
    isolated_migration_schema: PostgresRoles,
) -> None:
    application = import_module("app.platform.governance.application")
    temporal_conflict = cast(type[Exception], application.GovernanceTemporalConflict)
    with _writer_environment(isolated_migration_schema) as harness:
        duty, scope = _create_duty_and_scope(harness, marker=f"non-monotonic-{operation}")
        grant = harness.writer.create_grant(
            _grant_command(
                harness,
                duty=duty,
                scope=scope,
                marker=f"non-monotonic-{operation}",
            )
        )
        harness.writer.update_grant(
            harness.api.UpdateGrantCommand(
                school_id=harness.facts.school_id,
                grant_id=grant.target_id,
                expected_version=1,
                effective_from=FUTURE,
                planned_valid_to=UPDATED_PLANNED_END,
                basis_authority="test-only.sf5a",
                basis_reference=f"fixture://sf5a/non-monotonic-{operation}/future",
                actor=_actor(harness, reason="schedule future content before terminal RED"),
                idempotency_key=f"sf5a:non-monotonic-{operation}:update",
            )
        )
        terminal_command_type = (
            harness.api.EndGrantCommand if operation == "end" else harness.api.RevokeGrantCommand
        )
        terminal_method = (
            harness.writer.end_grant if operation == "end" else harness.writer.revoke_grant
        )
        with pytest.raises(temporal_conflict):
            terminal_method(
                terminal_command_type(
                    school_id=harness.facts.school_id,
                    grant_id=grant.target_id,
                    expected_version=2,
                    effective_at=AS_OF + timedelta(days=1),
                    actor=_actor(harness, reason="must reject non-monotonic terminal"),
                    idempotency_key=f"sf5a:non-monotonic-{operation}:terminal",
                )
            )

        assert (
            _scalar(
                harness.factory,
                "SELECT current_version FROM governance_grants WHERE id = :id",
                {"id": grant.target_id},
            )
            == 2
        )
        assert (
            _scalar(
                harness.factory,
                "SELECT count(*) FROM governance_grant_versions WHERE grant_id = :id",
                {"id": grant.target_id},
            )
            == 2
        )


@pytest.mark.parametrize("operation", ["end", "revoke"])
def test_nonmonotonic_update_cannot_hide_a_later_future_slice_from_terminal_guard(
    operation: str,
    isolated_migration_schema: PostgresRoles,
) -> None:
    application = import_module("app.platform.governance.application")
    temporal_conflict = cast(type[Exception], application.GovernanceTemporalConflict)
    with _writer_environment(isolated_migration_schema) as harness:
        duty, scope = _create_duty_and_scope(harness, marker=f"hidden-future-{operation}")
        grant = harness.writer.create_grant(
            _grant_command(
                harness,
                duty=duty,
                scope=scope,
                marker=f"hidden-future-{operation}",
            )
        )
        harness.writer.update_grant(
            harness.api.UpdateGrantCommand(
                school_id=harness.facts.school_id,
                grant_id=grant.target_id,
                expected_version=1,
                effective_from=FUTURE,
                planned_valid_to=UPDATED_PLANNED_END,
                basis_authority="test-only.sf5a",
                basis_reference=f"fixture://sf5a/hidden-future-{operation}/v2",
                actor=_actor(harness, reason="schedule the later future slice"),
                idempotency_key=f"sf5a:hidden-future-{operation}:update-v2",
            )
        )
        harness.writer.update_grant(
            harness.api.UpdateGrantCommand(
                school_id=harness.facts.school_id,
                grant_id=grant.target_id,
                expected_version=2,
                effective_from=AS_OF + timedelta(days=1),
                planned_valid_to=UPDATED_PLANNED_END,
                basis_authority="test-only.sf5a",
                basis_reference=f"fixture://sf5a/hidden-future-{operation}/v3",
                actor=_actor(harness, reason="record an earlier effective content slice"),
                idempotency_key=f"sf5a:hidden-future-{operation}:update-v3",
            )
        )
        before = _governance_counts(harness.factory)
        terminal_command_type = (
            harness.api.EndGrantCommand if operation == "end" else harness.api.RevokeGrantCommand
        )
        terminal_method = (
            harness.writer.end_grant if operation == "end" else harness.writer.revoke_grant
        )

        with pytest.raises(temporal_conflict):
            terminal_method(
                terminal_command_type(
                    school_id=harness.facts.school_id,
                    grant_id=grant.target_id,
                    expected_version=3,
                    effective_at=AS_OF + timedelta(days=2),
                    actor=_actor(
                        harness,
                        reason="terminal must not be hidden by the nonmonotonic update",
                    ),
                    idempotency_key=f"sf5a:hidden-future-{operation}:terminal",
                )
            )

        assert _governance_counts(harness.factory) == before
        assert (
            _scalar(
                harness.factory,
                "SELECT current_version FROM governance_grants WHERE id = :id",
                {"id": grant.target_id},
            )
            == 3
        )
        assert (
            _scalar(
                harness.factory,
                "SELECT count(*) FROM governance_grant_versions WHERE grant_id = :id",
                {"id": grant.target_id},
            )
            == 3
        )


@pytest.mark.parametrize("operation", ["end", "revoke"])
def test_terminal_copies_the_temporally_current_slice_after_nonmonotonic_updates(
    operation: str,
    isolated_migration_schema: PostgresRoles,
) -> None:
    with _writer_environment(isolated_migration_schema) as harness:
        duty, scope = _create_duty_and_scope(harness, marker=f"terminal-current-{operation}")
        grant = harness.writer.create_grant(
            _grant_command(harness, duty=duty, scope=scope, marker=f"terminal-current-{operation}")
        )
        later_slice_at = AS_OF + timedelta(days=10)
        earlier_slice_at = AS_OF + timedelta(days=5)
        terminal_at = AS_OF + timedelta(days=11)
        current_basis = f"fixture://sf5a/terminal-current-{operation}/v2-current"
        superseded_basis = f"fixture://sf5a/terminal-current-{operation}/v3-superseded"
        harness.writer.update_grant(
            harness.api.UpdateGrantCommand(
                school_id=harness.facts.school_id,
                grant_id=grant.target_id,
                expected_version=1,
                effective_from=later_slice_at,
                planned_valid_to=PLANNED_END,
                basis_authority="test-only.sf5a",
                basis_reference=current_basis,
                actor=_actor(harness, reason="schedule the temporally current v2 slice"),
                idempotency_key=f"sf5a:terminal-current-{operation}:update-v2",
            )
        )
        harness.writer.update_grant(
            harness.api.UpdateGrantCommand(
                school_id=harness.facts.school_id,
                grant_id=grant.target_id,
                expected_version=2,
                effective_from=earlier_slice_at,
                planned_valid_to=UPDATED_PLANNED_END,
                basis_authority="test-only.sf5a",
                basis_reference=superseded_basis,
                actor=_actor(harness, reason="record the nonmonotonic v3 slice"),
                idempotency_key=f"sf5a:terminal-current-{operation}:update-v3",
            )
        )
        terminal_command_type = (
            harness.api.EndGrantCommand if operation == "end" else harness.api.RevokeGrantCommand
        )
        terminal_method = (
            harness.writer.end_grant if operation == "end" else harness.writer.revoke_grant
        )
        result = terminal_method(
            terminal_command_type(
                school_id=harness.facts.school_id,
                grant_id=grant.target_id,
                expected_version=3,
                effective_at=terminal_at,
                actor=_actor(harness, reason="terminal copies the effective v2 slice"),
                idempotency_key=f"sf5a:terminal-current-{operation}:terminal",
            )
        )
        assert (result.target_version, result.assignment_epoch) == (4, 4)
        assert _rows(
            harness.factory,
            "SELECT transition_kind, planned_valid_to, basis_reference "
            "FROM governance_grant_versions WHERE grant_id = :id AND version = 4",
            {"id": grant.target_id},
        ) == [
            {
                "transition_kind": operation,
                "planned_valid_to": PLANNED_END,
                "basis_reference": current_basis,
            }
        ]
        audit = _rows(
            harness.factory,
            "SELECT before_summary, after_summary FROM governance_audit_events "
            "WHERE target_id = :id AND target_version = 4",
            {"id": grant.target_id},
        )
        assert len(audit) == 1
        assert audit[0]["before_summary"]["transition_kind"] == "update"
        assert audit[0]["before_summary"]["planned_valid_to"] == PLANNED_END.isoformat()
        assert audit[0]["before_summary"]["basis_reference"] == current_basis
        assert audit[0]["after_summary"]["planned_valid_to"] == PLANNED_END.isoformat()
        assert audit[0]["after_summary"]["basis_reference"] == current_basis
