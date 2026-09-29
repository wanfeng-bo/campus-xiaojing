from __future__ import annotations

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID

import pytest
from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.engine import URL
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from tests.governance.test_reader_basis import (
    _DependencyState,
    _load_reader_api,
    _query,
    _reader_harness,
    _reason,
)
from tests.governance.test_writer_lifecycle import (
    AS_OF,
    FUTURE,
    UPDATED_PLANNED_END,
    _actor,
    _create_duty_and_scope,
    _grant_command,
    _race_writer_calls,
    _scalar,
    _writer_environment,
)
from tests.master_data.conftest import PostgresRoles

pytestmark = pytest.mark.integration

API_ROOT = Path(__file__).resolve().parents[2]
_AUTHORITY_PROCESS_PAYLOAD = "SF5A_AUTHORITY_PROCESS_PAYLOAD"
_AUTHORITY_PROCESS_ENTRYPOINT = (
    "from tests.governance.test_concurrency_persistence_performance import "
    "_revoked_basis_process_probe; _revoked_basis_process_probe()"
)


def test_concurrent_distinct_grants_for_one_assignment_serialize_epoch_increment(
    isolated_migration_schema: PostgresRoles,
) -> None:
    with _writer_environment(isolated_migration_schema) as harness:
        duty, scope = _create_duty_and_scope(harness, marker="epoch-race")
        first = _grant_command(
            harness,
            duty=duty,
            scope=scope,
            marker="epoch-race-first",
        )
        second = replace(
            first,
            basis_reference="fixture://sf5a/epoch-race/second-independent-grant",
            idempotency_key="sf5a:epoch-race:second-grant",
        )
        barrier = Barrier(2)
        epoch_inserts: list[str] = []

        def synchronize_first_epoch_insert(
            _connection: object,
            _cursor: object,
            statement: str,
            _parameters: object,
            _context: object,
            _executemany: bool,
        ) -> None:
            normalized = statement.lstrip().lower()
            if normalized.startswith("insert into governance_assignment_epochs"):
                epoch_inserts.append(statement)
                barrier.wait(timeout=10)

        event.listen(harness.engine, "before_cursor_execute", synchronize_first_epoch_insert)
        try:
            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = [
                    executor.submit(harness.writer.create_grant, first),
                    executor.submit(harness.writer.create_grant, second),
                ]
                results: list[Any] = []
                errors: list[Exception] = []
                for future in futures:
                    try:
                        results.append(future.result(timeout=20))
                    except Exception as exc:
                        errors.append(exc)
        finally:
            event.remove(harness.engine, "before_cursor_execute", synchronize_first_epoch_insert)

        assert errors == []
        assert len(results) == 2
        assert len({result.target_id for result in results}) == 2
        assert sorted(result.assignment_epoch for result in results) == [1, 2]
        assert len(epoch_inserts) == 2
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
            == 2
        )
        assert _scalar(harness.factory, "SELECT count(*) FROM governance_grants") == 2
        assert (
            _scalar(
                harness.factory,
                "SELECT count(*) FROM governance_audit_events WHERE target_kind = 'grant'",
            )
            == 2
        )


@pytest.mark.parametrize("race_kind", ["update_revoke", "revoke_revoke"])
def test_distinct_keys_competing_for_one_grant_have_one_cas_winner(
    race_kind: str,
    isolated_migration_schema: PostgresRoles,
) -> None:
    with _writer_environment(isolated_migration_schema) as harness:
        duty, scope = _create_duty_and_scope(harness, marker=f"cas-{race_kind}")
        grant = harness.writer.create_grant(
            _grant_command(
                harness,
                duty=duty,
                scope=scope,
                marker=f"cas-{race_kind}",
            )
        )
        revoke = harness.api.RevokeGrantCommand(
            school_id=harness.facts.school_id,
            grant_id=grant.target_id,
            expected_version=1,
            effective_at=FUTURE,
            actor=_actor(harness, reason=f"{race_kind} revoke contender"),
            idempotency_key=f"sf5a:cas-{race_kind}:revoke-a",
        )
        if race_kind == "update_revoke":

            def first_call() -> Any:
                return harness.writer.update_grant(
                    harness.api.UpdateGrantCommand(
                        school_id=harness.facts.school_id,
                        grant_id=grant.target_id,
                        expected_version=1,
                        effective_from=FUTURE,
                        planned_valid_to=UPDATED_PLANNED_END,
                        basis_authority="test-only.sf5a",
                        basis_reference=f"fixture://sf5a/cas-{race_kind}/update",
                        actor=_actor(harness, reason="concurrent update contender"),
                        idempotency_key=f"sf5a:cas-{race_kind}:update",
                    )
                )

            def second_call() -> Any:
                return harness.writer.revoke_grant(revoke)
        else:

            def first_call() -> Any:
                return harness.writer.revoke_grant(revoke)

            def second_call() -> Any:
                return harness.writer.revoke_grant(
                    replace(revoke, idempotency_key=f"sf5a:cas-{race_kind}:revoke-b")
                )

        results, errors, _receipt_inserts = _race_writer_calls(
            harness,
            first_call,
            second_call,
        )

        assert len(results) == 1
        assert results[0].target_version == 2
        assert len(errors) == 1
        assert isinstance(errors[0], harness.api.GovernanceVersionConflict)
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
                "SELECT epoch FROM governance_assignment_epochs "
                "WHERE school_id = :school_id AND assignment_id = :assignment_id",
                {
                    "school_id": harness.facts.school_id,
                    "assignment_id": harness.facts.assignment_id,
                },
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


def _new_engine_reader(
    api: Any,
    writer_harness: Any,
    state: _DependencyState,
) -> tuple[Engine, Any]:
    engine = create_engine(writer_harness.engine.url, poolclass=NullPool)
    factory = sessionmaker(engine, expire_on_commit=False)
    view = SimpleNamespace(factory=factory, facts=writer_harness.facts)
    return engine, _reader_harness(api, view, state)


def _basis_payload(basis: Any) -> dict[str, object]:
    return {
        "school_id": str(basis.school_id),
        "assignment_id": str(basis.assignment_id),
        "duty_code": basis.duty_code,
        "scope_id": str(basis.scope_id),
        "grant_id": str(basis.grant_id),
        "grant_version": basis.grant_version,
        "assignment_epoch": basis.assignment_epoch,
        "issued_as_of": basis.issued_as_of.isoformat(),
        "valid_until": (basis.valid_until.isoformat() if basis.valid_until is not None else None),
        "dependency_versions": [
            {
                "authority": dependency.authority,
                "reference": dependency.reference,
                "version": dependency.version,
            }
            for dependency in basis.dependency_versions
        ],
    }


def _reader_process_environment(
    roles: PostgresRoles,
    *,
    payload: dict[str, object],
) -> dict[str, str]:
    reader = roles.reader_connection_parameters
    environment = os.environ.copy()
    environment[_AUTHORITY_PROCESS_PAYLOAD] = json.dumps(
        {
            **payload,
            "database": {
                "host": str(reader["host"]),
                "port": int(reader["port"]),
                "dbname": str(reader["dbname"]),
                "user": str(reader["user"]),
                "password": str(reader["password"]),
            },
        },
        separators=(",", ":"),
    )
    return environment


def _exception_reason(error: Exception) -> str:
    reason = getattr(error, "reason", "")
    return str(getattr(reason, "value", reason))


def _revoked_basis_process_probe() -> None:
    from app.platform.governance.contracts import (
        GovernanceAuthorizationBasis,
        GovernanceDependencyVersion,
    )

    payload = cast(
        dict[str, Any],
        json.loads(os.environ[_AUTHORITY_PROCESS_PAYLOAD]),
    )
    database = cast(dict[str, Any], payload["database"])
    basis_payload = cast(dict[str, Any], payload["basis"])
    state_payload = cast(dict[str, Any], payload["dependency_state"])
    engine = create_engine(
        URL.create(
            "postgresql+psycopg",
            username=str(database["user"]),
            password=str(database["password"]),
            host=str(database["host"]),
            port=int(database["port"]),
            database=str(database["dbname"]),
        ),
        poolclass=NullPool,
    )
    factory = sessionmaker(engine, expire_on_commit=False)
    api = _load_reader_api()
    facts = SimpleNamespace(
        school_id=UUID(cast(str, payload["school_id"])),
        person_id=UUID(cast(str, payload["person_id"])),
        assignment_id=UUID(cast(str, payload["assignment_id"])),
    )
    state = _DependencyState(
        assignment_version=int(state_payload["assignment_version"]),
        person_version=int(state_payload["person_version"]),
        organization_version=int(state_payload["organization_version"]),
        scope_target_version=int(state_payload["scope_target_version"]),
        valid_until=datetime.fromisoformat(cast(str, state_payload["valid_until"])),
        organization_id=UUID(cast(str, state_payload["organization_id"])),
    )
    harness = _reader_harness(
        api,
        SimpleNamespace(factory=factory, facts=facts),
        state,
    )
    dependencies = cast(list[dict[str, Any]], basis_payload["dependency_versions"])
    basis = GovernanceAuthorizationBasis(
        school_id=UUID(cast(str, basis_payload["school_id"])),
        assignment_id=UUID(cast(str, basis_payload["assignment_id"])),
        duty_code=cast(str, basis_payload["duty_code"]),
        scope_id=UUID(cast(str, basis_payload["scope_id"])),
        grant_id=UUID(cast(str, basis_payload["grant_id"])),
        grant_version=int(basis_payload["grant_version"]),
        assignment_epoch=int(basis_payload["assignment_epoch"]),
        issued_as_of=datetime.fromisoformat(cast(str, basis_payload["issued_as_of"])),
        valid_until=(
            datetime.fromisoformat(cast(str, basis_payload["valid_until"]))
            if basis_payload["valid_until"] is not None
            else None
        ),
        dependency_versions=tuple(
            GovernanceDependencyVersion(
                authority=str(dependency["authority"]),
                reference=str(dependency["reference"]),
                version=int(dependency["version"]),
            )
            for dependency in dependencies
        ),
    )
    historical_as_of = datetime.fromisoformat(cast(str, payload["historical_as_of"]))
    revoked_as_of = datetime.fromisoformat(cast(str, payload["revoked_as_of"]))
    query = api.GovernanceAuthorityQuery(
        school_id=facts.school_id,
        assignment_id=facts.assignment_id,
        duty_code=cast(str, payload["duty_code"]),
        scope_id=UUID(cast(str, payload["scope_id"])),
        as_of=historical_as_of,
    )

    try:
        historical = harness.reader.resolve(query)
        try:
            harness.basis_service.validate(basis, as_of=historical_as_of)
        except api.GovernanceBasisInvalid as error:
            basis_reason = _exception_reason(error)
        else:
            raise AssertionError("pre-revoke Governance basis remained valid")
        try:
            harness.reader.resolve(replace(query, as_of=revoked_as_of))
        except api.GovernanceAuthorityUnavailable as error:
            revoked_reason = _exception_reason(error)
        else:
            raise AssertionError("revoked Governance authority remained readable")
        print(
            json.dumps(
                {
                    "basis_reason": basis_reason,
                    "historical_grant_id": str(historical.grant_id),
                    "revoked_reason": revoked_reason,
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


def test_new_engines_read_persisted_authority_and_reject_pre_revoke_basis(
    isolated_migration_schema: PostgresRoles,
) -> None:
    api = _load_reader_api()
    with _writer_environment(isolated_migration_schema) as writer_harness:
        duty, scope = _create_duty_and_scope(writer_harness, marker="new-engine")
        grant = writer_harness.writer.create_grant(
            _grant_command(
                writer_harness,
                duty=duty,
                scope=scope,
                marker="new-engine",
            )
        )
        state = _DependencyState()
        query = _query(
            api,
            writer_harness,
            marker="new-engine",
            scope_id=scope.target_id,
            as_of=AS_OF,
        )
        first_engine, first = _new_engine_reader(api, writer_harness, state)
        try:
            snapshot = first.reader.resolve(query)
            basis = first.basis_service.issue(query)
            assert snapshot.grant_id == grant.target_id
            assert snapshot.basis_reference == "fixture://sf5a/new-engine/appointment"
            assert basis.assignment_epoch == 1
        finally:
            first_engine.dispose()

        revoke_at = AS_OF + timedelta(hours=1)
        writer_harness.writer.revoke_grant(
            writer_harness.api.RevokeGrantCommand(
                school_id=writer_harness.facts.school_id,
                grant_id=grant.target_id,
                expected_version=1,
                effective_at=revoke_at,
                actor=_actor(writer_harness, reason="persist revoke across new engine"),
                idempotency_key="sf5a:new-engine:revoke",
            )
        )

        restarted_engine, restarted = _new_engine_reader(api, writer_harness, state)
        try:
            with pytest.raises(api.GovernanceBasisInvalid) as invalid:
                restarted.basis_service.validate(basis, as_of=AS_OF)
            assert _reason(invalid) == "assignment_epoch_changed"
            with pytest.raises(api.GovernanceAuthorityUnavailable) as revoked:
                restarted.reader.resolve(replace(query, as_of=revoke_at))
            assert _reason(revoked) == "grant_inactive"
        finally:
            restarted_engine.dispose()


def test_new_python_process_reads_history_and_rejects_revoked_authority_and_old_basis(
    isolated_migration_schema: PostgresRoles,
) -> None:
    api = _load_reader_api()
    with _writer_environment(isolated_migration_schema) as writer_harness:
        duty, scope = _create_duty_and_scope(writer_harness, marker="new-process")
        grant = writer_harness.writer.create_grant(
            _grant_command(
                writer_harness,
                duty=duty,
                scope=scope,
                marker="new-process",
            )
        )
        state = _DependencyState()
        query = _query(
            api,
            writer_harness,
            marker="new-process",
            scope_id=scope.target_id,
            as_of=AS_OF,
        )
        basis = _reader_harness(api, writer_harness, state).basis_service.issue(query)
        revoke_at = AS_OF + timedelta(hours=1)
        writer_harness.writer.revoke_grant(
            writer_harness.api.RevokeGrantCommand(
                school_id=writer_harness.facts.school_id,
                grant_id=grant.target_id,
                expected_version=1,
                effective_at=revoke_at,
                actor=_actor(writer_harness, reason="persist revoke across new process"),
                idempotency_key="sf5a:new-process:revoke",
            )
        )

        completed = _run_python_probe(
            _AUTHORITY_PROCESS_ENTRYPOINT,
            environment=_reader_process_environment(
                isolated_migration_schema,
                payload={
                    "school_id": str(writer_harness.facts.school_id),
                    "person_id": str(writer_harness.facts.person_id),
                    "assignment_id": str(writer_harness.facts.assignment_id),
                    "duty_code": "test_only.new-process.duty",
                    "scope_id": str(scope.target_id),
                    "historical_as_of": AS_OF.isoformat(),
                    "revoked_as_of": revoke_at.isoformat(),
                    "basis": _basis_payload(basis),
                    "dependency_state": {
                        "assignment_version": state.assignment_version,
                        "person_version": state.person_version,
                        "organization_version": state.organization_version,
                        "scope_target_version": state.scope_target_version,
                        "valid_until": state.valid_until.isoformat(),
                        "organization_id": str(state.organization_id),
                    },
                },
            ),
        )

        assert completed.returncode == 0, completed.stderr
        payload = json.loads(completed.stdout.strip().splitlines()[-1])
        assert payload == {
            "basis_reason": "assignment_epoch_changed",
            "historical_grant_id": str(grant.target_id),
            "revoked_reason": "grant_inactive",
        }


def test_reader_sql_is_bounded_and_critical_paths_use_governance_indexes(
    isolated_migration_schema: PostgresRoles,
) -> None:
    api = _load_reader_api()
    with _writer_environment(isolated_migration_schema) as writer_harness:
        duty, scope = _create_duty_and_scope(writer_harness, marker="query-shape")
        grant = writer_harness.writer.create_grant(
            _grant_command(
                writer_harness,
                duty=duty,
                scope=scope,
                marker="query-shape",
            )
        )
        state = _DependencyState()
        reader_harness = _reader_harness(api, writer_harness, state)
        query = _query(
            api,
            writer_harness,
            marker="query-shape",
            scope_id=scope.target_id,
            as_of=AS_OF,
        )
        governance_selects: list[str] = []

        def record_governance_select(
            _connection: object,
            _cursor: object,
            statement: str,
            _parameters: object,
            _context: object,
            _executemany: bool,
        ) -> None:
            normalized = statement.lstrip().lower()
            if normalized.startswith("select") and "governance_" in normalized:
                governance_selects.append(statement)

        event.listen(writer_harness.engine, "before_cursor_execute", record_governance_select)
        try:
            resolved = reader_harness.reader.resolve(query)
        finally:
            event.remove(
                writer_harness.engine,
                "before_cursor_execute",
                record_governance_select,
            )

        assert resolved.grant_id == grant.target_id
        assert 1 <= len(governance_selects) <= 7
        assert [kind for kind, _as_of in reader_harness.facts_port.calls] == [
            "assignment",
            "person",
            "organization",
        ]
        assert len(reader_harness.scope_resolver.requests) == 1

        plans: dict[str, str] = {}
        with writer_harness.factory.begin() as session:
            session.execute(text("SET LOCAL enable_seqscan = off"))
            statements = {
                "ix_governance_grants_assignment": (
                    "SELECT id, current_version FROM governance_grants "
                    "WHERE school_id = :school_id AND assignment_id = :assignment_id "
                    "ORDER BY duty_id, current_version, id LIMIT 2"
                ),
                "ix_governance_grant_versions_as_of": (
                    "SELECT id FROM governance_grant_versions "
                    "WHERE school_id = :school_id AND grant_id = :grant_id "
                    "AND effective_from <= :as_of "
                    "ORDER BY effective_from DESC, version DESC LIMIT 1"
                ),
                "governance_assignment_epochs_pkey": (
                    "SELECT epoch FROM governance_assignment_epochs "
                    "WHERE school_id = :school_id AND assignment_id = :assignment_id"
                ),
            }
            parameters = {
                "school_id": writer_harness.facts.school_id,
                "assignment_id": writer_harness.facts.assignment_id,
                "grant_id": grant.target_id,
                "as_of": AS_OF,
            }
            for index_name, statement in statements.items():
                plan = session.execute(
                    text(f"EXPLAIN (FORMAT JSON, COSTS OFF) {statement}"),
                    parameters,
                ).scalar_one()
                plans[index_name] = json.dumps(plan, ensure_ascii=False, default=str)

        for index_name, plan in plans.items():
            assert index_name in plan
            assert "Seq Scan" not in plan
