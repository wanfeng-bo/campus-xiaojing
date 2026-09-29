# pyright: strict

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import cast
from uuid import UUID

import pytest
from pydantic import SecretStr

from app.composition.school_fact_executor_client_settings import SchoolFactExecutorClientSettings
from app.composition.school_fact_writer_client import SchoolFactWriterExecutorClient
from app.platform.contracts import CommandExecutionContext, SchoolId
from app.platform.school_facts.contracts import (
    CanonicalFactChangeSetV1,
    canonical_plan_sha256,
)
from app.platform.school_facts.ports import SchoolFactApplyResult
from tests.master_data.conftest import PostgresRoles
from tests.school_facts.test_sf2_transaction import (
    NOW,
    context_for,
    make_owner_factory,
    seed_staff,
)
from tests.school_facts.test_sf5b_writer_executor_postgres import (
    API_ROOT,
    CALLER_AUTH_KEY,
    CAPACITY_IDEMPOTENCY_KEY,
    CAPACITY_SESSION_BINDING,
    CAPACITY_SOURCE_SHA256,
    CAPACITY_UNIT_COUNT,
    CONTEXT_TRANSPORT_KEY,
    WriterActivityMetrics,
    build_api_environment,
    build_writer_environment,
    running_writer_executor,
)

# Bounded Level B harness budget only; this is not a production SLO.
CAPACITY_RESULT_TIMEOUT_SECONDS = 1_800
CAPACITY_RESULT_POLL_SECONDS = 0.5


def _capacity_client(base_url: str) -> SchoolFactWriterExecutorClient:
    return SchoolFactWriterExecutorClient(
        settings=SchoolFactExecutorClientSettings(
            base_url=base_url,
            caller_auth_key=SecretStr(CALLER_AUTH_KEY),
            context_transport_hmac_key=SecretStr(CONTEXT_TRANSPORT_KEY),
            request_timeout_seconds=60,
        )
    )


def _capacity_change_set() -> CanonicalFactChangeSetV1:
    changes = tuple(
        {
            "schema_version": 1,
            "operation": "upsert",
            "fact_kind": "person",
            "source_key": f"person:sf5b-capacity:{index:05d}",
            "expected_version": 0,
            "effective_at": NOW,
            "origin": {
                "sheet_code": "sf5b_capacity",
                "row_number": index + 2,
            },
            "content": {
                "display_name": f"SF5B 容量虚构人员 {index:05d}",
                "person_kind": "other",
                "lifecycle_status": "active",
            },
        }
        for index in range(CAPACITY_UNIT_COUNT)
    )
    return CanonicalFactChangeSetV1.model_validate(
        {
            "schema_version": 1,
            "source_sha256": CAPACITY_SOURCE_SHA256,
            "base_version": 1,
            "changes": changes,
        }
    )


def _observe_writer_activity(
    roles: PostgresRoles,
    stop: threading.Event,
    metrics: WriterActivityMetrics,
) -> None:
    try:
        with roles.admin_connection() as connection:
            while not stop.is_set():
                row = connection.execute(
                    """
                    SELECT count(*)::integer,
                           count(*) FILTER (WHERE state = 'active')::integer,
                           count(*) FILTER (WHERE xact_start IS NOT NULL)::integer,
                           coalesce(array_agg(pid), ARRAY[]::integer[])
                    FROM pg_catalog.pg_stat_activity
                    WHERE datname = %s
                      AND application_name = 'campus-ai-school-fact-writer'
                    """,
                    (roles.admin_connection_parameters["dbname"],),
                ).fetchone()
                if row is None:
                    raise AssertionError("pg_stat_activity aggregate returned no row.")
                connections = int(row[0])
                active = int(row[1])
                transactional = int(row[2])
                metrics.samples += 1
                metrics.max_connections = max(metrics.max_connections, connections)
                metrics.max_active_connections = max(metrics.max_active_connections, active)
                metrics.max_transactional_connections = max(
                    metrics.max_transactional_connections,
                    transactional,
                )
                metrics.observed_pids.update(int(pid) for pid in row[3])
                if transactional:
                    metrics.phases_with_transaction.add(metrics.phase)
                    if metrics.phase == "confirm":
                        metrics.confirm_transaction_seen.set()
                stop.wait(0.02)
    except Exception as exc:  # pragma: no cover - asserted by the owning test
        metrics.errors.append(f"{type(exc).__name__}: {exc}")
        metrics.confirm_transaction_seen.set()


def _start_commit_unknown_confirm(
    *,
    environment: dict[str, str],
    payload_path: Path,
    school_id: UUID,
    user_id: int,
    person_id: UUID,
    assignment_id: UUID,
    credential: str,
    change_set: CanonicalFactChangeSetV1,
) -> subprocess.Popen[str]:
    payload_path.write_text(
        json.dumps(
            {
                "school_id": str(school_id),
                "user_id": user_id,
                "person_id": str(person_id),
                "assignment_id": str(assignment_id),
                "credential": credential,
                "change_set": change_set.model_dump(mode="json", exclude_none=True),
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    script = """
import json
import sys
from pathlib import Path
from uuid import UUID
from app.composition.school_fact_writer_client import SchoolFactWriterExecutorClient
from app.composition.school_fact_executor_client_settings import SchoolFactExecutorClientSettings
from app.platform.school_facts.contracts import CanonicalFactChangeSetV1
from tests.school_facts.test_sf2_transaction import context_for

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
change_set = CanonicalFactChangeSetV1.model_validate_json(
    json.dumps(payload["change_set"], ensure_ascii=False, separators=(",", ":"))
)
context = context_for(
    school_id=UUID(payload["school_id"]),
    user_id=int(payload["user_id"]),
    person_id=UUID(payload["person_id"]),
    assignment_id=UUID(payload["assignment_id"]),
    key="sf5b-level-b-capacity-50000",
)
with SchoolFactWriterExecutorClient(settings=SchoolFactExecutorClientSettings()) as client:
    client.confirm(
        credential=payload["credential"],
        change_set=change_set,
        context=context,
        session_binding="sf5b-level-b-capacity-session",
    )
"""
    return subprocess.Popen(
        [sys.executable, "-c", script, str(payload_path)],
        cwd=API_ROOT,
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
    )


def _await_durable_result(
    *,
    client: SchoolFactWriterExecutorClient,
    school_id: UUID,
    change_set: CanonicalFactChangeSetV1,
    context: CommandExecutionContext,
    session_binding: str,
    timeout_seconds: float = CAPACITY_RESULT_TIMEOUT_SECONDS,
) -> SchoolFactApplyResult:
    plan_sha256 = canonical_plan_sha256(change_set)
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        lookup = client.result_by_digest(
            school_id=SchoolId(school_id),
            source_sha256=change_set.source_sha256,
            plan_sha256=plan_sha256,
            context=context,
            session_binding=session_binding,
        )
        if lookup.result is not None:
            return lookup.result
        time.sleep(CAPACITY_RESULT_POLL_SECONDS)
    raise AssertionError("50,000-unit confirmation did not produce a durable result in time.")


def _wait_for_writer_connections_to_close(roles: PostgresRoles) -> int:
    deadline = time.monotonic() + 10
    remaining = -1
    while time.monotonic() < deadline:
        with roles.admin_connection() as connection:
            row = connection.execute(
                """
                SELECT count(*)
                FROM pg_catalog.pg_stat_activity
                WHERE datname = %s
                  AND application_name = 'campus-ai-school-fact-writer'
                """,
                (roles.admin_connection_parameters["dbname"],),
            ).fetchone()
            assert row is not None
            remaining = int(cast(int, row[0]))
        if remaining == 0:
            return remaining
        time.sleep(0.05)
    return remaining


@pytest.mark.integration
def test_level_b_writer_executor_applies_50000_units_with_real_commit_unknown(
    isolated_migration_schema: PostgresRoles,
    tmp_path: Path,
) -> None:
    roles = isolated_migration_schema
    factory, setup_engine = make_owner_factory(roles)
    build_started = time.perf_counter()
    change_set = _capacity_change_set()
    build_seconds = time.perf_counter() - build_started
    payload_bytes = len(change_set.model_dump_json(exclude_none=True).encode("utf-8"))
    metrics = WriterActivityMetrics()
    stop_observer = threading.Event()
    observer: threading.Thread | None = None
    result: SchoolFactApplyResult | None = None
    timings: dict[str, float] = {}
    try:
        school_id, user_id, person_id, assignment_id = seed_staff(factory)
        with roles.admin_connection() as connection:
            anchor_row = connection.execute(
                "SELECT anchor_id, school_id FROM public.school_database_anchor"
            ).fetchone()
        assert anchor_row is not None
        anchor_id = UUID(str(anchor_row[0]))
        assert UUID(str(anchor_row[1])) == school_id

        writer_environment = build_writer_environment(
            roles,
            anchor_id=anchor_id,
            school_id=school_id,
        )
        writer_environment["SCHOOL_FACT_WRITER_STATEMENT_TIMEOUT_MS"] = "60000"
        assert not any("MIGRATION" in name or "OWNER" in name for name in writer_environment)
        context: CommandExecutionContext = context_for(
            school_id=school_id,
            user_id=user_id,
            person_id=person_id,
            assignment_id=assignment_id,
            key=CAPACITY_IDEMPOTENCY_KEY,
        )

        total_started = time.perf_counter()
        with running_writer_executor(writer_environment) as writer:
            observer = threading.Thread(
                target=_observe_writer_activity,
                args=(roles, stop_observer, metrics),
                daemon=True,
            )
            observer.start()
            api_environment = build_api_environment(base_url=writer.base_url)
            api_environment["SCHOOL_FACT_EXECUTOR_REQUEST_TIMEOUT_SECONDS"] = "60"
            assert not any(
                name.startswith("SCHOOL_FACT_WRITER_") or "MIGRATION" in name or "OWNER" in name
                for name in api_environment
            )

            with _capacity_client(writer.base_url) as client:
                metrics.phase = "preflight"
                started = time.perf_counter()
                preflight = client.preflight(
                    school_id=SchoolId(school_id),
                    change_set=change_set,
                    context=context,
                    session_binding=CAPACITY_SESSION_BINDING,
                )
                timings["preflight_seconds"] = time.perf_counter() - started
                assert preflight.is_clean, preflight.issues[:10]

                metrics.phase = "prepare"
                started = time.perf_counter()
                prepared = client.prepare(
                    change_set=change_set,
                    context=context,
                    session_binding=CAPACITY_SESSION_BINDING,
                )
                timings["prepare_seconds"] = time.perf_counter() - started
                assert prepared.plan_sha256 == canonical_plan_sha256(change_set)

                metrics.phase = "confirm"
                confirm_started = time.perf_counter()
                caller = _start_commit_unknown_confirm(
                    environment=api_environment,
                    payload_path=tmp_path / "capacity-confirm.json",
                    school_id=school_id,
                    user_id=user_id,
                    person_id=person_id,
                    assignment_id=assignment_id,
                    credential=prepared.credential,
                    change_set=change_set,
                )
                transaction_deadline = time.monotonic() + 120
                while (
                    not metrics.confirm_transaction_seen.wait(0.05)
                    and time.monotonic() < transaction_deadline
                    and caller.poll() is None
                ):
                    pass
                assert not metrics.errors, metrics.errors
                assert metrics.confirm_transaction_seen.is_set(), (
                    "The real Writer confirmation transaction was not observed."
                )
                assert caller.poll() is None, (
                    "The caller received a result before commit-unknown could be established."
                )
                caller.terminate()
                caller.wait(timeout=10)
                assert caller.returncode != 0

                metrics.phase = "result_recovery"
                result = _await_durable_result(
                    client=client,
                    school_id=school_id,
                    change_set=change_set,
                    context=context,
                    session_binding=CAPACITY_SESSION_BINDING,
                )
                timings["confirm_to_durable_result_seconds"] = time.perf_counter() - confirm_started
                assert result.changed_count == CAPACITY_UNIT_COUNT
                assert result.target_version == 2
                assert result.replayed is True

                metrics.phase = "ipc_result"
                started = time.perf_counter()
                repeated = client.result_by_digest(
                    school_id=SchoolId(school_id),
                    source_sha256=change_set.source_sha256,
                    plan_sha256=canonical_plan_sha256(change_set),
                    context=context,
                    session_binding=CAPACITY_SESSION_BINDING,
                )
                timings["ipc_result_seconds"] = time.perf_counter() - started
                assert repeated.result == result
                timings["formal_total_seconds"] = time.perf_counter() - total_started
        stop_observer.set()
        assert observer is not None
        observer.join(timeout=10)
        assert not observer.is_alive()
        assert not metrics.errors, metrics.errors
        assert metrics.max_connections <= 2
        assert metrics.max_connections >= 1
        assert metrics.max_active_connections >= 1
        assert metrics.max_transactional_connections >= 1
        assert "confirm" in metrics.phases_with_transaction
        assert metrics.observed_pids
        assert _wait_for_writer_connections_to_close(roles) == 0
        assert result is not None

        with roles.admin_connection() as connection:
            person_shape = connection.execute(
                """
                SELECT count(*)::integer, count(DISTINCT xmin::text)::integer
                FROM public.school_people
                WHERE school_id = %s AND source_key LIKE 'person:sf5b-capacity:%%'
                """,
                (school_id,),
            ).fetchone()
            name_shape = connection.execute(
                """
                SELECT count(*)::integer, count(DISTINCT names.xmin::text)::integer
                FROM public.school_person_names AS names
                JOIN public.school_people AS people ON people.id = names.person_id
                WHERE people.school_id = %s
                  AND people.source_key LIKE 'person:sf5b-capacity:%%'
                """,
                (school_id,),
            ).fetchone()
            profile_shape = connection.execute(
                """
                SELECT count(*)::integer, count(DISTINCT profiles.xmin::text)::integer
                FROM public.school_person_profile_versions AS profiles
                JOIN public.school_people AS people ON people.id = profiles.person_id
                WHERE people.school_id = %s
                  AND people.source_key LIKE 'person:sf5b-capacity:%%'
                """,
                (school_id,),
            ).fetchone()
            unit_shape = connection.execute(
                """
                SELECT count(*)::integer,
                       count(*) FILTER (WHERE status = 'applied')::integer,
                       count(DISTINCT xmin::text)::integer
                FROM public.master_data_import_units
                WHERE school_id = %s AND batch_id = %s
                """,
                (school_id, result.batch_id),
            ).fetchone()
            lineage_shape = connection.execute(
                """
                SELECT count(*)::integer, count(DISTINCT xmin::text)::integer
                FROM public.school_fact_change_lineage
                WHERE school_id = %s AND batch_id = %s
                """,
                (school_id, result.batch_id),
            ).fetchone()
            batch_shape = connection.execute(
                """
                SELECT status, planned_units, succeeded_units, failed_units,
                       pending_units, target_fact_version
                FROM public.master_data_import_batches
                WHERE school_id = %s AND id = %s
                """,
                (school_id, result.batch_id),
            ).fetchone()

        assert person_shape == (CAPACITY_UNIT_COUNT, 1)
        assert lineage_shape is not None
        assert name_shape == (CAPACITY_UNIT_COUNT, 1)
        assert profile_shape == (CAPACITY_UNIT_COUNT, 1)
        assert unit_shape == (CAPACITY_UNIT_COUNT, CAPACITY_UNIT_COUNT, 1)
        assert lineage_shape == (CAPACITY_UNIT_COUNT, 1)
        assert batch_shape == (
            "applied",
            CAPACITY_UNIT_COUNT,
            CAPACITY_UNIT_COUNT,
            0,
            0,
            2,
        )

        measurement: dict[str, object] = {
            "unit_count": CAPACITY_UNIT_COUNT,
            "canonical_build_seconds": round(build_seconds, 6),
            "wire_payload_bytes": payload_bytes,
            **{name: round(value, 6) for name, value in timings.items()},
            "caller_outcome_unknown": True,
            "pg_activity_samples": metrics.samples,
            "max_writer_connections": metrics.max_connections,
            "max_active_writer_connections": metrics.max_active_connections,
            "max_transactional_writer_connections": metrics.max_transactional_connections,
            "observed_writer_backend_count": len(metrics.observed_pids),
            "confirm_output_xmin_transactions": int(cast(int, lineage_shape[1])),
        }
        print("SF5B_50000_MEASUREMENT=" + json.dumps(measurement, sort_keys=True))
    finally:
        stop_observer.set()
        if observer is not None:
            observer.join(timeout=10)
        setup_engine.dispose()  # type: ignore[union-attr]
