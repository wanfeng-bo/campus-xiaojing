from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import httpx
import psycopg
import pytest
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient
from psycopg.pq import TransactionStatus
from pydantic import SecretStr
from sqlalchemy import URL, Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.composition.school_fact_executor_client_settings import SchoolFactExecutorClientSettings
from app.composition.school_fact_persistence import SchoolFactPersistenceError
from app.composition.school_fact_writer_change_boundary import (
    SchoolFactExecutorChangeBoundary,
)
from app.composition.school_fact_writer_client import (
    SchoolFactWriterExecutorClient,
)
from app.composition.school_fact_writer_executor import (
    SqlAlchemySchoolFactWriterExecutorKernel,
)
from app.composition.school_fact_writer_settings import (
    SchoolFactWriterSettings,
    WriterStartupRejected,
    build_school_fact_writer_engine,
    verify_school_fact_writer_database,
)
from app.composition.school_facts import (
    InMemoryPrivateSchoolFactSourceStore,
    InMemorySchoolFactPlanStore,
)
from app.core.request_id import assign_request_id
from app.db.rls_context import set_local_execution_context
from app.integrations.dingtalk.client import DingTalkIdentity
from app.interfaces.http.errors import ApiError
from app.interfaces.http.framework_request import (
    api_error_handler,
    request_validation_error_handler,
)
from app.interfaces.http.school_facts import (
    SchoolFactRequestIdentity,
    create_school_fact_router,
)
from app.platform.contracts import CommandExecutionContext, SchoolId, StaffActorContext
from app.platform.identity.context_proof import ContextProofSigner
from app.platform.identity.service import find_or_create_dingtalk_user
from app.platform.school_facts.application import SchoolFactChangeRejected
from app.platform.school_facts.contracts import CanonicalFactChangeSetV1, canonical_plan_sha256
from app.platform.school_facts.ports import SchoolFactApplyResult
from tests.master_data.conftest import PostgresRoles
from tests.school_facts.test_sf2_transaction import (
    NOW,
    change_set,
    context_for,
    make_owner_factory,
    seed_staff,
)

API_ROOT = Path(__file__).resolve().parents[2]
CALLER_AUTH_KEY = "sf5b-level-b-caller-auth-key-test-only-0001"
CONTEXT_TRANSPORT_KEY = "sf5b-level-b-context-transport-key-test-only-0001"
CONFIRMATION_HMAC_KEY = "sf5b-level-b-confirmation-hmac-key-test-only-0001"
RLS_CONTEXT_SIGNING_KEY = "master-data-schema-context-signing-key-32-bytes"
SESSION_BINDING = "sf5b-level-b-formal-session"
IDEMPOTENCY_KEY = "sf5b-level-b-formal-confirmation"
CAPACITY_UNIT_COUNT = 50_000
CAPACITY_IDEMPOTENCY_KEY = "sf5b-level-b-capacity-50000"
CAPACITY_SESSION_BINDING = "sf5b-level-b-capacity-session"
CAPACITY_SOURCE_SHA256 = sha256(b"sf5b:writer-executor:capacity:50000").hexdigest()

_SAFE_PROCESS_ENVIRONMENT = (
    "COMSPEC",
    "LOCALAPPDATA",
    "PATH",
    "PATHEXT",
    "SYSTEMDRIVE",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "USERPROFILE",
    "WINDIR",
)

_EXECUTION_CONTEXT_SETTING_NAMES = (
    "app.context_key_version",
    "app.school_id",
    "app.account_user_id",
    "app.person_id",
    "app.context_kind",
    "app.assignment_id",
    "app.assignment_version",
    "app.guardian_relationship_id",
    "app.guardian_relationship_version",
    "app.authorization_version",
    "app.request_id",
    "app.context_purpose",
    "app.idempotency_key_hash",
    "app.session_nonce_hash",
    "app.proof_expires_at",
    "app.server_proof",
    "app.binding_database",
    "app.binding_backend_pid",
    "app.binding_xact_id",
    "app.binding_proof",
)


@dataclass(slots=True)
class _WriterProcess:
    process: subprocess.Popen[str]
    base_url: str
    secrets: tuple[str, ...]

    def stop(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
        try:
            self.process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.communicate(timeout=10)


@dataclass(slots=True)
class WriterActivityMetrics:
    samples: int = 0
    max_connections: int = 0
    max_active_connections: int = 0
    max_transactional_connections: int = 0
    observed_pids: set[int] = field(default_factory=set)
    phases_with_transaction: set[str] = field(default_factory=set)
    errors: list[str] = field(default_factory=list)
    phase: str = "idle"
    confirm_transaction_seen: threading.Event = field(default_factory=threading.Event)


@dataclass(frozen=True, slots=True)
class _WriterConnectionObservation:
    backend_pid: int
    current_user: str
    session_user: str
    was_idle_before_query: bool
    context_values: tuple[str | None, ...]


def _minimal_process_environment() -> dict[str, str]:
    environment = {
        name: value
        for name in _SAFE_PROCESS_ENVIRONMENT
        if (value := os.environ.get(name)) is not None
    }
    environment.update(
        {
            "APP_ENV": "test",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONPATH": str(API_ROOT),
            "PYTHONUNBUFFERED": "1",
            "PYTHONUTF8": "1",
        }
    )
    return environment


def _writer_settings(
    roles: PostgresRoles,
    *,
    anchor_id: UUID,
    school_id: UUID,
    pool_size: int = 1,
    max_overflow: int = 0,
) -> SchoolFactWriterSettings:
    connection = roles.writer_connection_parameters
    return SchoolFactWriterSettings(
        database_host=str(connection["host"]),
        database_port=int(connection["port"]),
        database_name=str(connection["dbname"]),
        database_password=SecretStr(str(connection["password"])),
        expected_anchor_id=anchor_id,
        expected_school_id=school_id,
        expected_system_identifier=roles.disposable_cluster_system_identifier,
        confirmation_hmac_key=SecretStr(CONFIRMATION_HMAC_KEY),
        caller_auth_key=SecretStr(CALLER_AUTH_KEY),
        context_transport_hmac_key=SecretStr(CONTEXT_TRANSPORT_KEY),
        rls_context_signing_key=SecretStr(RLS_CONTEXT_SIGNING_KEY),
        pool_size=pool_size,
        max_overflow=max_overflow,
        pool_timeout_seconds=1,
        connect_timeout_seconds=2,
    )


def _existing_anchor_id(roles: PostgresRoles, *, school_id: UUID) -> UUID:
    with roles.admin_connection() as connection:
        anchor_row = connection.execute(
            "SELECT anchor_id, school_id FROM public.school_database_anchor"
        ).fetchone()
    assert anchor_row is not None
    anchor_id = UUID(str(anchor_row[0]))
    assert UUID(str(anchor_row[1])) == school_id
    return anchor_id


def _observe_writer_connection(engine: Engine) -> _WriterConnectionObservation:
    settings_projection = ", ".join(
        f"current_setting('{name}', true)" for name in _EXECUTION_CONTEXT_SETTING_NAMES
    )
    with engine.connect() as connection:
        driver_connection = connection.connection.driver_connection
        assert isinstance(driver_connection, psycopg.Connection)
        was_idle = driver_connection.info.transaction_status is TransactionStatus.IDLE
        row = connection.execute(
            text("SELECT pg_backend_pid(), current_user, session_user, " + settings_projection)
        ).one()
    return _WriterConnectionObservation(
        backend_pid=int(row[0]),
        current_user=str(row[1]),
        session_user=str(row[2]),
        was_idle_before_query=was_idle,
        context_values=tuple(None if value is None else str(value) for value in row[3:]),
    )


def _assert_clean_writer_observation(
    observation: _WriterConnectionObservation,
) -> None:
    assert observation.current_user == "campus_ai_school_fact_writer"
    assert observation.session_user == "campus_ai_school_fact_writer"
    assert observation.was_idle_before_query is True
    assert len(observation.context_values) == len(_EXECUTION_CONTEXT_SETTING_NAMES)
    assert all(value in {None, ""} for value in observation.context_values)


def build_writer_environment(
    roles: PostgresRoles,
    *,
    anchor_id: UUID,
    school_id: UUID,
) -> dict[str, str]:
    connection = roles.writer_connection_parameters
    environment = _minimal_process_environment()
    environment.update(
        {
            "SCHOOL_FACT_WRITER_DATABASE_HOST": str(connection["host"]),
            "SCHOOL_FACT_WRITER_DATABASE_PORT": str(connection["port"]),
            "SCHOOL_FACT_WRITER_DATABASE_NAME": str(connection["dbname"]),
            "SCHOOL_FACT_WRITER_DATABASE_USER": str(connection["user"]),
            "SCHOOL_FACT_WRITER_DATABASE_PASSWORD": str(connection["password"]),
            "SCHOOL_FACT_WRITER_EXPECTED_ANCHOR_ID": str(anchor_id),
            "SCHOOL_FACT_WRITER_EXPECTED_SCHOOL_ID": str(school_id),
            "SCHOOL_FACT_WRITER_EXPECTED_SYSTEM_IDENTIFIER": (
                roles.disposable_cluster_system_identifier
            ),
            "SCHOOL_FACT_WRITER_CONFIRMATION_HMAC_KEY": CONFIRMATION_HMAC_KEY,
            "SCHOOL_FACT_WRITER_CALLER_AUTH_KEY": CALLER_AUTH_KEY,
            "SCHOOL_FACT_WRITER_CONTEXT_TRANSPORT_HMAC_KEY": CONTEXT_TRANSPORT_KEY,
            "SCHOOL_FACT_WRITER_RLS_CONTEXT_SIGNING_KEY": RLS_CONTEXT_SIGNING_KEY,
            "SCHOOL_FACT_WRITER_POOL_SIZE": "2",
            "SCHOOL_FACT_WRITER_MAX_OVERFLOW": "0",
            "SCHOOL_FACT_WRITER_POOL_TIMEOUT_SECONDS": "1",
            "SCHOOL_FACT_WRITER_CONNECT_TIMEOUT_SECONDS": "2",
        }
    )
    return environment


def build_api_environment(*, base_url: str) -> dict[str, str]:
    environment = _minimal_process_environment()
    environment.update(
        {
            "SCHOOL_FACT_EXECUTOR_BASE_URL": base_url,
            "SCHOOL_FACT_EXECUTOR_CALLER_AUTH_KEY": CALLER_AUTH_KEY,
            "SCHOOL_FACT_EXECUTOR_CONTEXT_TRANSPORT_HMAC_KEY": CONTEXT_TRANSPORT_KEY,
            "SCHOOL_FACT_EXECUTOR_REQUEST_TIMEOUT_SECONDS": "10",
        }
    )
    return environment


def _redact(value: str, secrets: tuple[str, ...]) -> str:
    result = value
    for secret in secrets:
        if secret:
            result = result.replace(secret, "<redacted>")
    return result


def _free_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _writer_command(port: int) -> tuple[str, ...]:
    return (
        sys.executable,
        "-m",
        "uvicorn",
        "app.composition.school_fact_writer_executor:production_school_fact_writer_executor_app",
        "--factory",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--log-level",
        "warning",
        "--no-access-log",
    )


def _wait_for_listener(process: subprocess.Popen[str], port: int, *, timeout: float = 12) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        return_code = process.poll()
        if return_code is not None:
            output, _ = process.communicate(timeout=1)
            raise AssertionError(
                f"Writer Executor exited before listening (code={return_code}): {output}"
            )
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.05)
    raise AssertionError("Writer Executor did not listen within the bounded startup budget.")


@contextmanager
def running_writer_executor(environment: Mapping[str, str]) -> Iterator[_WriterProcess]:
    port = _free_tcp_port()
    process = subprocess.Popen(
        _writer_command(port),
        cwd=API_ROOT,
        env=dict(environment),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
    )
    secrets = tuple(
        value
        for name, value in environment.items()
        if any(marker in name for marker in ("PASSWORD", "KEY", "SECRET", "TOKEN"))
    )
    writer = _WriterProcess(
        process=process,
        base_url=f"http://127.0.0.1:{port}",
        secrets=secrets,
    )
    try:
        try:
            _wait_for_listener(process, port)
        except AssertionError as exc:
            raise AssertionError(_redact(str(exc), secrets)) from None
        yield writer
    finally:
        writer.stop()


def _assert_startup_rejected(environment: Mapping[str, str]) -> None:
    port = _free_tcp_port()
    process = subprocess.Popen(
        _writer_command(port),
        cwd=API_ROOT,
        env=dict(environment),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
    )
    deadline = time.monotonic() + 12
    listening = False
    try:
        while time.monotonic() < deadline and process.poll() is None:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    listening = True
                    break
            except OSError:
                time.sleep(0.05)
        if listening:
            raise AssertionError("Rejected Writer configuration opened a TCP listener.")
        assert process.poll() not in {None, 0}
    finally:
        if process.poll() is None:
            process.terminate()
        try:
            process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate(timeout=10)


def _executor_client(base_url: str) -> SchoolFactWriterExecutorClient:
    settings = SchoolFactExecutorClientSettings(
        base_url=base_url,
        caller_auth_key=SecretStr(CALLER_AUTH_KEY),
        context_transport_hmac_key=SecretStr(CONTEXT_TRANSPORT_KEY),
        request_timeout_seconds=10,
    )
    return SchoolFactWriterExecutorClient(settings=settings)


def _candidate_app(
    *,
    boundary: SchoolFactExecutorChangeBoundary,
    actor: StaffActorContext,
) -> FastAPI:
    def identity() -> SchoolFactRequestIdentity:
        return SchoolFactRequestIdentity(
            actor=actor,
            session_binding=SESSION_BINDING,
            authorized_actions=frozenset({"school_facts.manage"}),
        )

    app = FastAPI()
    app.middleware("http")(assign_request_id)
    app.add_exception_handler(ApiError, api_error_handler)
    app.add_exception_handler(RequestValidationError, request_validation_error_handler)
    app.include_router(
        create_school_fact_router(
            change_service=boundary,
            source_store=InMemoryPrivateSchoolFactSourceStore(clock=lambda: NOW),
            plan_store=InMemorySchoolFactPlanStore(clock=lambda: NOW),
            identity_dependency=identity,
            clock=lambda: NOW,
        )
    )
    return app


def _admin_payload(*, source_key: str) -> dict[str, object]:
    return {
        "base_version": 1,
        "changes": [
            {
                "operation": "upsert",
                "fact_kind": "school",
                "source_key": source_key,
                "expected_version": 1,
                "effective_at": datetime(2026, 8, 21, 8, tzinfo=UTC).isoformat(),
                "content": {
                    "business_code": "SF5B-LEVEL-B",
                    "display_name": "SF5B Level B 虚构学校",
                    "timezone_name": "Asia/Shanghai",
                    "lifecycle_status": "active",
                },
            }
        ],
    }


def test_formal_router_preserves_executor_domain_conflict_as_public_409() -> None:
    school_id = uuid4()
    source_key = f"sf5b-router-conflict-{uuid4().hex}"
    context: CommandExecutionContext = context_for(
        school_id=school_id,
        user_id=19,
        person_id=uuid4(),
        assignment_id=uuid4(),
        key="sf5b-router-conflict",
    )
    actor = context.actor
    assert isinstance(actor, StaffActorContext)

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        parsed_change_set = CanonicalFactChangeSetV1.model_validate_json(
            json.dumps(payload["change_set"])
        )
        plan_sha256 = canonical_plan_sha256(parsed_change_set)
        if request.url.path.endswith("/preflight"):
            return httpx.Response(
                200,
                json={
                    "plan_sha256": plan_sha256,
                    "is_clean": True,
                    "issues": [],
                    "confirmation_credential": None,
                },
                request=request,
            )
        if request.url.path.endswith("/prepare"):
            return httpx.Response(
                200,
                json={
                    "plan_sha256": plan_sha256,
                    "credential": "C" * 43,
                    "expires_at": (NOW + timedelta(minutes=5)).isoformat(),
                },
                request=request,
            )
        assert request.url.path.endswith("/confirm")
        return httpx.Response(
            409,
            json={"detail": "must-not-become-a-public-503"},
            request=request,
        )

    settings = SchoolFactExecutorClientSettings(
        base_url="http://writer-executor.invalid",
        caller_auth_key=SecretStr(CALLER_AUTH_KEY),
        context_transport_hmac_key=SecretStr(CONTEXT_TRANSPORT_KEY),
        request_timeout_seconds=2,
    )
    with SchoolFactWriterExecutorClient(
        settings=settings,
        transport=httpx.MockTransport(handler),
    ) as executor_client:
        boundary = SchoolFactExecutorChangeBoundary(executor_client)
        with TestClient(_candidate_app(boundary=boundary, actor=actor)) as api:
            preflight = api.post(
                "/api/v1/school-facts/plans/admin",
                json=_admin_payload(source_key=source_key),
            )
            assert preflight.status_code == 200, preflight.text
            plan_ref = str(preflight.json()["plan_ref"])
            prepared = api.post(
                f"/api/v1/school-facts/plans/{plan_ref}/confirmation",
                headers={"Idempotency-Key": "sf5b-router-conflict"},
            )
            assert prepared.status_code == 200, prepared.text
            rejected = api.post(
                f"/api/v1/school-facts/plans/{plan_ref}/confirm",
                headers={"Idempotency-Key": "sf5b-router-conflict"},
                json={"credential": prepared.json()["credential"]},
            )

    assert rejected.status_code == 409
    assert rejected.json()["code"] == "school_facts.change_rejected"


def _run_api_process_preflight(
    *,
    environment: Mapping[str, str],
    school_id: UUID,
    change_set: CanonicalFactChangeSetV1,
    context: CommandExecutionContext,
    session_binding: str,
) -> None:
    actor = context.actor
    assert isinstance(actor, StaffActorContext)
    payload = {
        "school_id": str(school_id),
        "change_set": change_set.model_dump(mode="json", exclude_none=True),
        "actor": {
            "account_user_id": actor.account_user_id,
            "school_id": str(actor.school_id),
            "person_id": str(actor.person_id),
            "assignment_id": str(actor.assignment_id),
            "assignment_version": actor.assignment_version,
            "authorization_version": actor.authorization_version,
        },
        "request_id": str(context.request_id),
        "idempotency_key": context.idempotency_key,
        "session_binding": session_binding,
    }
    script = """
import json
import sys
from uuid import UUID
from app.composition.school_fact_writer_client import SchoolFactWriterExecutorClient
from app.composition.school_fact_executor_client_settings import SchoolFactExecutorClientSettings
from app.platform import contracts as platform_contracts
from app.platform.contracts import SchoolId
from app.platform.school_facts.contracts import CanonicalFactChangeSetV1

payload = json.loads(sys.stdin.read())
actor_payload = payload["actor"]
actor = platform_contracts._issue_staff_actor_context(
    account_user_id=actor_payload["account_user_id"],
    school_id=SchoolId(UUID(actor_payload["school_id"])),
    person_id=platform_contracts.PersonId(UUID(actor_payload["person_id"])),
    assignment_id=platform_contracts.AssignmentId(UUID(actor_payload["assignment_id"])),
    assignment_version=actor_payload["assignment_version"],
    authorization_version=actor_payload["authorization_version"],
)
context = platform_contracts._issue_command_execution_context(
    actor=actor,
    request_id=UUID(payload["request_id"]),
    idempotency_key=payload["idempotency_key"],
)
change_set = CanonicalFactChangeSetV1.model_validate_json(
    json.dumps(payload["change_set"], separators=(",", ":"))
)
with SchoolFactWriterExecutorClient(settings=SchoolFactExecutorClientSettings()) as client:
    report = client.preflight(
        school_id=SchoolId(UUID(payload["school_id"])),
        change_set=change_set,
        context=context,
        session_binding=payload["session_binding"],
    )
assert report.is_clean
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=API_ROOT,
        env=dict(environment),
        input=json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def _seed_additional_staff_actor(
    factory: sessionmaker[Session],
    *,
    school_id: UUID,
) -> tuple[int, UUID, UUID]:
    marker = uuid4().hex
    person_id = uuid4()
    assignment_id = uuid4()
    binding_id = uuid4()
    relationship_source_id = uuid4()
    valid_from = NOW - timedelta(days=1)
    with factory.begin() as session:
        organization_id = UUID(
            str(
                session.execute(
                    text(
                        "SELECT id FROM organization_units "
                        "WHERE school_id = :school_id ORDER BY id LIMIT 1"
                    ),
                    {"school_id": school_id},
                ).scalar_one()
            )
        )
        user_id = int(
            session.execute(
                text(
                    "INSERT INTO users "
                    "(provider_code, provider_tenant_key, external_subject, display_name, "
                    "is_active, security_version) VALUES "
                    "('local-test', :tenant, :subject, 'SF5B 第二执行人', true, 1) "
                    "RETURNING id"
                ),
                {"tenant": marker, "subject": marker},
            ).scalar_one()
        )
        session.execute(
            text(
                "INSERT INTO school_people (id, school_id, source_key, status, version) "
                "VALUES (:id, :school_id, :source_key, 'active', 1)"
            ),
            {"id": person_id, "school_id": school_id, "source_key": f"person:{marker}"},
        )
        session.execute(
            text(
                "INSERT INTO school_fact_sources "
                "(id, school_id, source_key, source_kind, source_ref, schema_version) "
                "VALUES (:id, :school_id, :source_key, 'legacy', :source_key, 1)"
            ),
            {
                "id": relationship_source_id,
                "school_id": school_id,
                "source_key": f"test:pool-actor:{marker}",
            },
        )
        assignment_values = {
            "id": assignment_id,
            "school_id": school_id,
            "source_key": f"assignment:{marker}",
            "person_id": person_id,
            "organization_id": organization_id,
            "approval": f"approval:{marker}",
            "valid_from": valid_from,
            "source_id": relationship_source_id,
        }
        session.execute(
            text(
                "INSERT INTO staff_assignments "
                "(id, school_id, source_key, person_id, organization_unit_id, position_title, "
                "semantic_role_code, assignment_kind, appointment_source, approval_reference, "
                "valid_from, status, version) VALUES "
                "(:id, :school_id, :source_key, :person_id, :organization_id, "
                "'第二执行人', 'school_data_admin', 'substantive', 'manual', :approval, "
                ":valid_from, 'confirmed', 1)"
            ),
            assignment_values,
        )
        session.execute(
            text(
                "INSERT INTO staff_assignment_versions "
                "(id, school_id, assignment_id, person_id, organization_unit_id, "
                "position_title, semantic_role_code, assignment_kind, appointment_source, "
                "approval_reference, effective_from, status, target_version, source_ref, "
                "source_id) VALUES "
                "(:version_id, :school_id, :id, :person_id, :organization_id, "
                "'第二执行人', 'school_data_admin', 'substantive', 'manual', :approval, "
                ":valid_from, 'confirmed', 1, :source_key, :source_id)"
            ),
            {**assignment_values, "version_id": uuid4()},
        )
        binding_values = {
            "id": binding_id,
            "school_id": school_id,
            "source_key": f"login:{marker}",
            "user_id": user_id,
            "person_id": person_id,
            "valid_from": valid_from,
            "tenant": marker,
            "subject": marker,
            "source_id": relationship_source_id,
        }
        session.execute(
            text(
                "INSERT INTO account_person_bindings "
                "(id, school_id, source_key, account_user_id, person_id, valid_from, "
                "status, version) VALUES "
                "(:id, :school_id, :source_key, :user_id, :person_id, :valid_from, "
                "'confirmed', 1)"
            ),
            binding_values,
        )
        session.execute(
            text(
                "INSERT INTO account_person_binding_versions "
                "(id, school_id, binding_id, account_user_id, person_id, provider_code, "
                "provider_tenant_key, external_subject, effective_from, status, "
                "target_version, source_ref, source_id) VALUES "
                "(:version_id, :school_id, :id, :user_id, :person_id, 'local-test', "
                ":tenant, :subject, :valid_from, 'confirmed', 1, :source_key, :source_id)"
            ),
            {**binding_values, "version_id": uuid4()},
        )
    return user_id, person_id, assignment_id


@pytest.mark.integration
def test_level_b_writer_process_formal_chain_restart_and_startup_gates(
    isolated_migration_schema: PostgresRoles,
) -> None:
    roles = isolated_migration_schema
    factory, setup_engine = make_owner_factory(roles)
    try:
        school_id, user_id, person_id, assignment_id = seed_staff(factory)
        with factory() as session:
            source_key = str(
                session.execute(
                    text("SELECT source_key FROM schools WHERE id = :school_id"),
                    {"school_id": school_id},
                ).scalar_one()
            )
        anchor_id = _existing_anchor_id(roles, school_id=school_id)

        writer_environment = build_writer_environment(
            roles,
            anchor_id=anchor_id,
            school_id=school_id,
        )
        assert not any("MIGRATION" in name or "OWNER" in name for name in writer_environment)

        wrong_role = dict(writer_environment)
        wrong_role["SCHOOL_FACT_WRITER_DATABASE_USER"] = "campus_ai_runtime"
        _assert_startup_rejected(wrong_role)

        wrong_anchor = dict(writer_environment)
        wrong_anchor["SCHOOL_FACT_WRITER_EXPECTED_ANCHOR_ID"] = str(uuid4())
        _assert_startup_rejected(wrong_anchor)

        with roles.admin_connection() as connection:
            connection.execute(
                "UPDATE public.alembic_version SET version_num = %s",
                ("20260824_wrong",),
            )
        try:
            _assert_startup_rejected(writer_environment)
        finally:
            with roles.admin_connection() as connection:
                connection.execute(
                    "UPDATE public.alembic_version SET version_num = %s",
                    ("20260824_20",),
                )

        context: CommandExecutionContext = context_for(
            school_id=school_id,
            user_id=user_id,
            person_id=person_id,
            assignment_id=assignment_id,
            key=IDEMPOTENCY_KEY,
        )
        actor = context.actor
        assert isinstance(actor, StaffActorContext)
        payload = _admin_payload(source_key=source_key)

        with running_writer_executor(writer_environment) as writer:
            api_environment = build_api_environment(base_url=writer.base_url)
            assert not any(
                name.startswith("SCHOOL_FACT_WRITER_") or "MIGRATION" in name or "OWNER" in name
                for name in api_environment
            )

            raw_change_set = change_set(school_source_key=source_key)
            _run_api_process_preflight(
                environment=api_environment,
                school_id=school_id,
                change_set=raw_change_set,
                context=context,
                session_binding="sf5b-api-process-preflight",
            )

            with _executor_client(writer.base_url) as executor_client:
                boundary = SchoolFactExecutorChangeBoundary(executor_client)
                with TestClient(_candidate_app(boundary=boundary, actor=actor)) as api:
                    preflight = api.post(
                        "/api/v1/school-facts/plans/admin",
                        json=payload,
                    )
                    assert preflight.status_code == 200, preflight.text
                    assert preflight.json()["is_clean"] is True
                    plan_ref = str(preflight.json()["plan_ref"])
                    prepared = api.post(
                        f"/api/v1/school-facts/plans/{plan_ref}/confirmation",
                        headers={"Idempotency-Key": IDEMPOTENCY_KEY},
                    )
                    assert prepared.status_code == 200, prepared.text
                    applied = api.post(
                        f"/api/v1/school-facts/plans/{plan_ref}/confirm",
                        headers={"Idempotency-Key": IDEMPOTENCY_KEY},
                        json={"credential": prepared.json()["credential"]},
                    )
                    assert applied.status_code == 200, applied.text
                    assert applied.json()["target_version"] == 2
                    assert applied.json()["changed_count"] == 1

        with running_writer_executor(writer_environment) as restarted_writer:
            with _executor_client(restarted_writer.base_url) as restarted_client:
                restarted_boundary = SchoolFactExecutorChangeBoundary(restarted_client)
                with TestClient(_candidate_app(boundary=restarted_boundary, actor=actor)) as api:
                    recovered = api.get(f"/api/v1/school-facts/plans/{plan_ref}/result")
                    assert recovered.status_code == 200, recovered.text
                    assert recovered.json()["state"] == "applied"
                    assert recovered.json()["target_version"] == 2
                    assert recovered.json()["changed_count"] == 1

        with factory() as session:
            assert session.execute(
                text("SELECT display_name, version FROM schools WHERE id = :school_id"),
                {"school_id": school_id},
            ).one() == ("SF5B Level B 虚构学校", 2)
    finally:
        setup_engine.dispose()  # type: ignore[union-attr]


@pytest.mark.integration
@pytest.mark.parametrize("terminal_operation", ("revoke", "end"), ids=("revoke", "end"))
def test_writer_executor_only_reactivates_revoked_school_without_preprepare_restore(
    isolated_migration_schema: PostgresRoles,
    terminal_operation: str,
) -> None:
    roles = isolated_migration_schema
    factory, setup_engine = make_owner_factory(roles)
    writer_engine: Engine | None = None
    try:
        school_id, user_id, person_id, assignment_id = seed_staff(factory)
        with factory() as session:
            school_source_key = str(
                session.execute(
                    text("SELECT source_key FROM schools WHERE id = :school_id"),
                    {"school_id": school_id},
                ).scalar_one()
            )

        anchor_id = _existing_anchor_id(roles, school_id=school_id)
        settings = _writer_settings(
            roles,
            anchor_id=anchor_id,
            school_id=school_id,
            pool_size=1,
            max_overflow=0,
        )
        writer_engine = build_school_fact_writer_engine(settings)
        verify_school_fact_writer_database(writer_engine, settings=settings)
        kernel = SqlAlchemySchoolFactWriterExecutorKernel(
            engine=writer_engine,
            settings=settings,
        )

        checked_now = datetime.now(UTC).replace(microsecond=0)
        terminal_template = change_set(
            school_source_key=school_source_key,
            base_version=1,
        )
        terminal_plan = terminal_template.model_copy(
            update={
                "source_sha256": sha256(
                    f"sf5b:writer:school-{terminal_operation}".encode("ascii")
                ).hexdigest(),
                "changes": (
                    terminal_template.changes[0].model_copy(
                        update={
                            "operation": terminal_operation,
                            "effective_at": checked_now + timedelta(days=1),
                            "content": None,
                        }
                    ),
                ),
            }
        )
        terminal_context = context_for(
            school_id=school_id,
            user_id=user_id,
            person_id=person_id,
            assignment_id=assignment_id,
            key=f"sf5b-writer-school-{terminal_operation}",
        )
        terminal_report = kernel.preflight(
            school_id=SchoolId(school_id),
            change_set=terminal_plan,
            context=terminal_context,
            session_binding=f"sf5b-writer-school-{terminal_operation}-session",
        )
        assert terminal_report.is_clean, terminal_report.issues
        terminal_prepared = kernel.prepare(
            change_set=terminal_plan,
            context=terminal_context,
            session_binding=f"sf5b-writer-school-{terminal_operation}-session",
            provenance=None,
        )
        terminal_result = kernel.confirm(
            credential=terminal_prepared.credential,
            change_set=terminal_plan,
            context=terminal_context,
            session_binding=f"sf5b-writer-school-{terminal_operation}-session",
        )
        assert terminal_result.target_version == 2

        with roles.admin_connection() as connection:
            assert connection.execute(
                "SELECT status, version FROM public.schools WHERE id = %s",
                (school_id,),
            ).fetchone() == ("inactive", 2)

        reactivate_template = change_set(
            school_source_key=school_source_key,
            base_version=2,
        )
        reactivate_plan = reactivate_template.model_copy(
            update={
                "source_sha256": sha256(b"sf5b:writer:school-reactivate").hexdigest(),
                "changes": (
                    reactivate_template.changes[0].model_copy(
                        update={
                            "operation": "reactivate",
                            "effective_at": checked_now + timedelta(days=2),
                        }
                    ),
                ),
            }
        )
        reactivate_context = context_for(
            school_id=school_id,
            user_id=user_id,
            person_id=person_id,
            assignment_id=assignment_id,
            key="sf5b-writer-school-reactivate",
        )
        if terminal_operation == "end":
            with pytest.raises(SchoolFactPersistenceError):
                kernel.preflight(
                    school_id=SchoolId(school_id),
                    change_set=reactivate_plan,
                    context=reactivate_context,
                    session_binding="sf5b-writer-school-reactivate-session",
                )
            return

        reactivate_report = kernel.preflight(
            school_id=SchoolId(school_id),
            change_set=reactivate_plan,
            context=reactivate_context,
            session_binding="sf5b-writer-school-reactivate-session",
        )
        assert reactivate_report.is_clean, reactivate_report.issues
        with roles.admin_connection() as connection:
            assert connection.execute(
                "SELECT status, version FROM public.schools WHERE id = %s",
                (school_id,),
            ).fetchone() == ("inactive", 2)

        reactivate_prepared = kernel.prepare(
            change_set=reactivate_plan,
            context=reactivate_context,
            session_binding="sf5b-writer-school-reactivate-session",
            provenance=None,
        )
        with roles.admin_connection() as connection:
            assert connection.execute(
                "SELECT status, version FROM public.schools WHERE id = %s",
                (school_id,),
            ).fetchone() == ("inactive", 2)

        reactivate_result = kernel.confirm(
            credential=reactivate_prepared.credential,
            change_set=reactivate_plan,
            context=reactivate_context,
            session_binding="sf5b-writer-school-reactivate-session",
        )
        assert reactivate_result.target_version == 3
        with roles.admin_connection() as connection:
            assert connection.execute(
                "SELECT status, version FROM public.schools WHERE id = %s",
                (school_id,),
            ).fetchone() == ("active", 3)
    finally:
        if writer_engine is not None:
            writer_engine.dispose()
        setup_engine.dispose()  # type: ignore[union-attr]


@pytest.mark.integration
def test_writer_checkout_rejects_privileged_role_membership_drift(
    isolated_migration_schema: PostgresRoles,
) -> None:
    roles = isolated_migration_schema
    factory, setup_engine = make_owner_factory(roles)
    writer_engine: Engine | None = None
    try:
        school_id, _user_id, _person_id, _assignment_id = seed_staff(factory)
        anchor_id = _existing_anchor_id(roles, school_id=school_id)
        settings = _writer_settings(
            roles,
            anchor_id=anchor_id,
            school_id=school_id,
            pool_size=1,
            max_overflow=0,
        )
        with roles.admin_connection() as connection:
            connection.execute("GRANT campus_ai_db_owner TO campus_ai_school_fact_writer")
        try:
            writer_engine = build_school_fact_writer_engine(settings)
            with pytest.raises(WriterStartupRejected):
                verify_school_fact_writer_database(
                    writer_engine,
                    settings=settings,
                )
        finally:
            if writer_engine is not None:
                writer_engine.dispose()
            with roles.admin_connection() as connection:
                connection.execute("REVOKE campus_ai_db_owner FROM campus_ai_school_fact_writer")
    finally:
        setup_engine.dispose()


@pytest.mark.integration
def test_writer_pool_rolls_back_execution_context_before_different_actor_reuse(
    isolated_migration_schema: PostgresRoles,
) -> None:
    roles = isolated_migration_schema
    factory, setup_engine = make_owner_factory(roles)
    writer_engine: Engine | None = None
    try:
        school_id, first_user_id, first_person_id, first_assignment_id = seed_staff(factory)
        second_user_id, second_person_id, second_assignment_id = _seed_additional_staff_actor(
            factory,
            school_id=school_id,
        )
        with factory() as session:
            school_source_key = str(
                session.execute(
                    text("SELECT source_key FROM schools WHERE id = :school_id"),
                    {"school_id": school_id},
                ).scalar_one()
            )

        anchor_id = _existing_anchor_id(roles, school_id=school_id)

        settings = _writer_settings(
            roles,
            anchor_id=anchor_id,
            school_id=school_id,
            pool_size=1,
            max_overflow=0,
        )
        writer_engine = build_school_fact_writer_engine(settings)
        assert settings.pool_size == 1
        assert settings.max_overflow == 0
        verify_school_fact_writer_database(writer_engine, settings=settings)
        kernel = SqlAlchemySchoolFactWriterExecutorKernel(
            engine=writer_engine,
            settings=settings,
        )
        before_confirm = _observe_writer_connection(writer_engine)
        _assert_clean_writer_observation(before_confirm)

        first_change_set = change_set(
            school_source_key=school_source_key,
            base_version=1,
        ).model_copy(update={"source_sha256": sha256(b"sf5b:pool:actor-one").hexdigest()})
        first_context = context_for(
            school_id=school_id,
            user_id=first_user_id,
            person_id=first_person_id,
            assignment_id=first_assignment_id,
            key="sf5b-pool-actor-one",
        )
        first_report = kernel.preflight(
            school_id=SchoolId(school_id),
            change_set=first_change_set,
            context=first_context,
            session_binding="sf5b-pool-session-one",
        )
        assert first_report.is_clean, first_report.issues
        first_prepared = kernel.prepare(
            change_set=first_change_set,
            context=first_context,
            session_binding="sf5b-pool-session-one",
            provenance=None,
        )
        first_result = kernel.confirm(
            credential=first_prepared.credential,
            change_set=first_change_set,
            context=first_context,
            session_binding="sf5b-pool-session-one",
        )
        assert first_result.target_version == 2

        after_first_confirm = _observe_writer_connection(writer_engine)
        _assert_clean_writer_observation(after_first_confirm)
        assert after_first_confirm.backend_pid == before_confirm.backend_pid

        second_change_set_template = change_set(
            school_source_key=school_source_key,
            base_version=2,
        )
        second_effective_at = NOW + timedelta(seconds=1)
        second_change_set = second_change_set_template.model_copy(
            update={
                "source_sha256": sha256(b"sf5b:pool:actor-two").hexdigest(),
                "changes": tuple(
                    change.model_copy(update={"effective_at": second_effective_at})
                    for change in second_change_set_template.changes
                ),
            }
        )
        second_context = context_for(
            school_id=school_id,
            user_id=second_user_id,
            person_id=second_person_id,
            assignment_id=second_assignment_id,
            key="sf5b-pool-actor-two",
        )
        second_report = kernel.preflight(
            school_id=SchoolId(school_id),
            change_set=second_change_set,
            context=second_context,
            session_binding="sf5b-pool-session-two",
        )
        assert second_report.is_clean, second_report.issues
        second_prepared = kernel.prepare(
            change_set=second_change_set,
            context=second_context,
            session_binding="sf5b-pool-session-two",
            provenance=None,
        )
        second_result = kernel.confirm(
            credential=second_prepared.credential,
            change_set=second_change_set,
            context=second_context,
            session_binding="sf5b-pool-session-two",
        )
        assert second_result.target_version == 3

        after_second_confirm = _observe_writer_connection(writer_engine)
        _assert_clean_writer_observation(after_second_confirm)
        assert after_second_confirm.backend_pid == before_confirm.backend_pid

        with roles.admin_connection() as connection:
            assert connection.execute(
                "SELECT version FROM public.schools WHERE id = %s",
                (school_id,),
            ).fetchone() == (3,)
            audit_actors = connection.execute(
                """
                SELECT actor_user_id
                FROM public.audit_logs
                WHERE school_id = %s
                  AND event_type = 'school_facts.change_set.applied'
                ORDER BY id
                """,
                (school_id,),
            ).fetchall()
        assert audit_actors == [(first_user_id,), (second_user_id,)]
    finally:
        if writer_engine is not None:
            writer_engine.dispose()
        setup_engine.dispose()  # type: ignore[union-attr]


@pytest.mark.integration
def test_writer_pool_rejects_committed_session_context_and_recovers(
    isolated_migration_schema: PostgresRoles,
) -> None:
    roles = isolated_migration_schema
    factory, setup_engine = make_owner_factory(roles)
    writer_engine: Engine | None = None
    try:
        school_id, _user_id, _person_id, _assignment_id = seed_staff(factory)
        anchor_id = _existing_anchor_id(roles, school_id=school_id)
        settings = _writer_settings(
            roles,
            anchor_id=anchor_id,
            school_id=school_id,
            pool_size=1,
            max_overflow=0,
        )
        writer_engine = build_school_fact_writer_engine(settings)
        verify_school_fact_writer_database(writer_engine, settings=settings)

        first = _observe_writer_connection(writer_engine)
        _assert_clean_writer_observation(first)
        with writer_engine.connect() as connection:
            contaminated_pid = connection.execute(text("SELECT pg_backend_pid()"))
            assert int(contaminated_pid.scalar_one()) == first.backend_pid
            connection.execute(
                text("SELECT set_config('app.school_id', :school_id, false)"),
                {"school_id": str(school_id)},
            )
            connection.commit()

        with pytest.raises(WriterStartupRejected):
            _observe_writer_connection(writer_engine)

        recovered = _observe_writer_connection(writer_engine)
        _assert_clean_writer_observation(recovered)
        assert recovered.backend_pid != first.backend_pid
    finally:
        if writer_engine is not None:
            writer_engine.dispose()
        setup_engine.dispose()  # type: ignore[union-attr]


@pytest.mark.integration
def test_writer_pool_revalidates_head_on_every_checkout_and_recovers(
    isolated_migration_schema: PostgresRoles,
) -> None:
    roles = isolated_migration_schema
    factory, setup_engine = make_owner_factory(roles)
    writer_engine: Engine | None = None
    head_restored = False
    try:
        school_id, _user_id, _person_id, _assignment_id = seed_staff(factory)
        with roles.admin_connection() as connection:
            anchor_row = connection.execute(
                "SELECT anchor_id, school_id FROM public.school_database_anchor"
            ).fetchone()
        assert anchor_row is not None
        anchor_id = UUID(str(anchor_row[0]))
        assert UUID(str(anchor_row[1])) == school_id

        settings = _writer_settings(
            roles,
            anchor_id=anchor_id,
            school_id=school_id,
            pool_size=1,
            max_overflow=0,
        )
        writer_engine = build_school_fact_writer_engine(settings)
        verify_school_fact_writer_database(writer_engine, settings=settings)
        first = _observe_writer_connection(writer_engine)
        _assert_clean_writer_observation(first)

        with roles.admin_connection() as connection:
            connection.execute(
                "UPDATE public.alembic_version SET version_num = %s",
                ("20260824_wrong_after_startup",),
            )
        try:
            with pytest.raises(WriterStartupRejected):
                _observe_writer_connection(writer_engine)
        finally:
            with roles.admin_connection() as connection:
                connection.execute(
                    "UPDATE public.alembic_version SET version_num = %s",
                    ("20260824_20",),
                )
            head_restored = True

        recovered = _observe_writer_connection(writer_engine)
        _assert_clean_writer_observation(recovered)
        assert recovered.backend_pid != first.backend_pid
    finally:
        if not head_restored:
            with roles.admin_connection() as connection:
                connection.execute(
                    "UPDATE public.alembic_version SET version_num = %s",
                    ("20260824_20",),
                )
        if writer_engine is not None:
            writer_engine.dispose()
        setup_engine.dispose()  # type: ignore[union-attr]


@pytest.mark.integration
def test_writer_role_rls_rejects_missing_forged_and_cross_anchor_context(
    isolated_migration_schema: PostgresRoles,
) -> None:
    roles = isolated_migration_schema
    factory, setup_engine = make_owner_factory(roles)
    try:
        school_id, user_id, person_id, assignment_id = seed_staff(factory)
        foreign_school_id = uuid4()
        with roles.admin_connection() as connection:
            assert connection.execute(
                "SELECT count(*) FROM public.schools WHERE id = %s",
                (foreign_school_id,),
            ).fetchone() == (0,)

        signer = ContextProofSigner(SecretStr(RLS_CONTEXT_SIGNING_KEY))
        valid_context = context_for(
            school_id=school_id,
            user_id=user_id,
            person_id=person_id,
            assignment_id=assignment_id,
            key="sf5b-writer-role-rls-valid",
        )
        valid_envelope = signer.sign_execution_context(
            valid_context,
            session_nonce_hash=sha256(b"sf5b-writer-role-rls-valid").hexdigest(),
            proof_expires_at=datetime.now(UTC) + timedelta(minutes=5),
        )

        with psycopg.connect(
            **roles.writer_connection_parameters,
            autocommit=False,
        ) as connection:
            with pytest.raises(
                psycopg.DatabaseError,
                match="master data RLS context is missing",
            ):
                connection.execute("SELECT id FROM public.schools").fetchall()
            connection.rollback()

        with psycopg.connect(
            **roles.writer_connection_parameters,
            autocommit=False,
        ) as connection:
            set_local_execution_context(cast(Any, connection), valid_envelope)
            connection.execute(
                "SELECT set_config('app.school_id', %s, true)",
                (str(foreign_school_id),),
            )
            with pytest.raises(
                psycopg.DatabaseError,
                match="RLS context integrity check failed",
            ):
                connection.execute("SELECT id FROM public.schools").fetchall()
            connection.rollback()

        foreign_context = context_for(
            school_id=foreign_school_id,
            user_id=user_id,
            person_id=person_id,
            assignment_id=assignment_id,
            key="sf5b-writer-role-rls-foreign",
        )
        foreign_envelope = signer.sign_execution_context(
            foreign_context,
            session_nonce_hash=sha256(b"sf5b-writer-role-rls-foreign").hexdigest(),
            proof_expires_at=datetime.now(UTC) + timedelta(minutes=5),
        )
        with psycopg.connect(
            **roles.writer_connection_parameters,
            autocommit=False,
        ) as connection:
            with pytest.raises(
                psycopg.DatabaseError,
                match="execution context actor is not current",
            ):
                set_local_execution_context(cast(Any, connection), foreign_envelope)
            connection.rollback()

        with psycopg.connect(
            **roles.writer_connection_parameters,
            autocommit=False,
        ) as connection:
            set_local_execution_context(cast(Any, connection), valid_envelope)
            with pytest.raises(
                psycopg.errors.InsufficientPrivilege,
                match="row-level security",
            ):
                connection.execute(
                    """
                    INSERT INTO public.school_people (
                        id, school_id, source_key, status, version
                    ) VALUES (%s, %s, %s, 'active', 1)
                    """,
                    (
                        uuid4(),
                        foreign_school_id,
                        f"sf5b:foreign-school-attack:{uuid4()}",
                    ),
                )
            connection.rollback()
    finally:
        setup_engine.dispose()  # type: ignore[union-attr]


def _runtime_identity_url(roles: PostgresRoles) -> URL:
    values = roles.runtime_connection_parameters
    return URL.create(
        "postgresql+psycopg",
        username=str(values["user"]),
        password=str(values["password"]),
        host=str(values["host"]),
        port=int(values["port"]),
        database=str(values["dbname"]),
    )


@pytest.mark.integration
def test_identity_legacy_claim_and_executor_login_mapping_confirm_are_serialized(
    isolated_migration_schema: PostgresRoles,
) -> None:
    roles = isolated_migration_schema
    factory, setup_engine = make_owner_factory(roles)
    identity_engine = create_engine(
        _runtime_identity_url(roles),
        pool_size=1,
        max_overflow=0,
        pool_timeout=3,
    )
    identity_factory = sessionmaker(bind=identity_engine, expire_on_commit=False)
    try:
        school_id, actor_user_id, person_id, assignment_id = seed_staff(factory)
        token = uuid4().hex
        corporation_id = f"sf5b-corp-{token[:12]}"
        personnel_id = f"sf5b-person-{token[12:24]}"
        legacy_subject = f"dingtalk:{corporation_id}:{personnel_id}"
        binding_source_key = f"login:sf5b-race:{token}"
        target_person_source_key = f"person:sf5b-race:{token}"
        source_sha256 = sha256(f"sf5b:identity-race:{token}".encode("ascii")).hexdigest()
        with roles.admin_connection() as connection:
            runtime_identity_acl = connection.execute(
                """
                SELECT
                    has_column_privilege(
                        'campus_ai_runtime', 'public.users', 'display_name', 'UPDATE'
                    ),
                    has_column_privilege(
                        'campus_ai_runtime', 'public.users', 'updated_at', 'UPDATE'
                    ),
                    has_column_privilege(
                        'campus_ai_runtime', 'public.users', 'provider_code', 'UPDATE'
                    ),
                    has_column_privilege(
                        'campus_ai_runtime', 'public.users', 'provider_tenant_key', 'UPDATE'
                    ),
                    has_column_privilege(
                        'campus_ai_runtime', 'public.users', 'external_subject', 'UPDATE'
                    )
                """
            ).fetchone()
            assert runtime_identity_acl == (True, True, False, False, False)
            legacy_user_row = connection.execute(
                """
                INSERT INTO public.users (
                    provider_code,
                    provider_tenant_key,
                    external_subject,
                    display_name,
                    is_active,
                    security_version
                )
                VALUES ('legacy', 'legacy', %s, 'SF5B Legacy Identity', true, 1)
                RETURNING id
                """,
                (legacy_subject,),
            ).fetchone()
            assert legacy_user_row is not None
            legacy_user_id = int(legacy_user_row[0])

        anchor_id = _existing_anchor_id(roles, school_id=school_id)

        race_change_set = CanonicalFactChangeSetV1.model_validate(
            {
                "schema_version": 1,
                "source_sha256": source_sha256,
                "base_version": 1,
                "changes": (
                    {
                        "schema_version": 1,
                        "operation": "upsert",
                        "fact_kind": "person",
                        "source_key": target_person_source_key,
                        "expected_version": 0,
                        "effective_at": NOW,
                        "origin": {
                            "sheet_code": "sf5b_identity_race",
                            "row_number": 2,
                        },
                        "content": {
                            "display_name": "SF5B Race Target Person",
                            "person_kind": "staff",
                            "lifecycle_status": "active",
                        },
                    },
                    {
                        "schema_version": 1,
                        "operation": "upsert",
                        "fact_kind": "login_mapping",
                        "source_key": binding_source_key,
                        "expected_version": 0,
                        "effective_at": NOW,
                        "origin": {
                            "sheet_code": "sf5b_identity_race",
                            "row_number": 2,
                        },
                        "content": {
                            "provider_code": "dingtalk",
                            "provider_tenant_key": corporation_id,
                            "external_subject": personnel_id,
                            "person_source_key": target_person_source_key,
                            "status": "confirmed",
                        },
                    },
                ),
            }
        )
        idempotency_key = f"sf5b-identity-race-{token}"
        session_binding = f"sf5b-identity-race-session-{token}"
        context: CommandExecutionContext = context_for(
            school_id=school_id,
            user_id=actor_user_id,
            person_id=person_id,
            assignment_id=assignment_id,
            key=idempotency_key,
        )
        identity = DingTalkIdentity(
            corporation_id=corporation_id,
            personnel_id=personnel_id,
            display_name="SF5B Claimed Identity",
        )
        writer_environment = build_writer_environment(
            roles,
            anchor_id=anchor_id,
            school_id=school_id,
        )

        with running_writer_executor(writer_environment) as writer:
            with _executor_client(writer.base_url) as preparation_client:
                report = preparation_client.preflight(
                    school_id=SchoolId(school_id),
                    change_set=race_change_set,
                    context=context,
                    session_binding=session_binding,
                )
                assert report.is_clean, report.issues
                prepared = preparation_client.prepare(
                    change_set=race_change_set,
                    context=context,
                    session_binding=session_binding,
                )

            barrier = threading.Barrier(2)

            def claim_identity() -> int:
                with identity_factory() as session:
                    barrier.wait(timeout=10)
                    user = find_or_create_dingtalk_user(
                        session,
                        identity,
                        request_id=f"sf5b-identity-claim-{token}",
                    )
                    session.commit()
                    return int(user.id)

            def confirm_login_mapping() -> tuple[str, SchoolFactApplyResult | None]:
                with _executor_client(writer.base_url) as confirm_client:
                    barrier.wait(timeout=10)
                    try:
                        applied = confirm_client.confirm(
                            credential=prepared.credential,
                            change_set=race_change_set,
                            context=context,
                            session_binding=session_binding,
                        )
                    except SchoolFactChangeRejected as exc:
                        cause = exc.__cause__
                        assert isinstance(cause, httpx.HTTPStatusError)
                        assert cause.response.status_code == 409
                        return "rejected", None
                    return "applied", applied

            with ThreadPoolExecutor(max_workers=2) as executor:
                claim_future = executor.submit(claim_identity)
                confirm_future = executor.submit(confirm_login_mapping)
                claimed_user_id = claim_future.result(timeout=30)
                confirm_state, applied_result = confirm_future.result(timeout=30)

            with _executor_client(writer.base_url) as result_client:
                persisted_result = result_client.result_by_digest(
                    school_id=SchoolId(school_id),
                    source_sha256=race_change_set.source_sha256,
                    plan_sha256=canonical_plan_sha256(race_change_set),
                    context=context,
                    session_binding=session_binding,
                ).result

        assert claimed_user_id == legacy_user_id
        assert confirm_state in {"applied", "rejected"}
        with roles.admin_connection() as connection:
            target_users = connection.execute(
                """
                SELECT id, provider_code, provider_tenant_key, external_subject
                FROM public.users
                WHERE provider_code = 'dingtalk'
                  AND provider_tenant_key = %s
                  AND external_subject = %s
                """,
                (corporation_id, personnel_id),
            ).fetchall()
            legacy_count_row = connection.execute(
                """
                SELECT count(*)
                FROM public.users
                WHERE provider_code = 'legacy'
                  AND provider_tenant_key = 'legacy'
                  AND external_subject = %s
                """,
                (legacy_subject,),
            ).fetchone()
            assert legacy_count_row is not None
            legacy_count = int(legacy_count_row[0])
            binding_rows = connection.execute(
                """
                SELECT binding.account_user_id,
                       binding.person_id,
                       version.account_user_id,
                       version.person_id,
                       version.provider_code,
                       version.provider_tenant_key,
                       version.external_subject,
                       users.provider_code,
                       users.provider_tenant_key,
                       users.external_subject
                FROM public.account_person_bindings AS binding
                JOIN public.account_person_binding_versions AS version
                  ON version.school_id = binding.school_id
                 AND version.binding_id = binding.id
                JOIN public.users AS users ON users.id = binding.account_user_id
                WHERE binding.school_id = %s
                  AND binding.source_key = %s
                """,
                (school_id, binding_source_key),
            ).fetchall()
            target_person_row = connection.execute(
                """
                SELECT id
                FROM public.school_people
                WHERE school_id = %s AND source_key = %s
                """,
                (school_id, target_person_source_key),
            ).fetchone()
            target_person_id = (
                None if target_person_row is None else UUID(str(target_person_row[0]))
            )

        assert target_users == [
            (
                legacy_user_id,
                "dingtalk",
                corporation_id,
                personnel_id,
            )
        ]
        assert legacy_count == 0
        if confirm_state == "applied":
            assert applied_result is not None
            assert persisted_result is not None
            assert persisted_result.batch_id == applied_result.batch_id
            assert persisted_result.target_version == applied_result.target_version
            assert persisted_result.changed_count == applied_result.changed_count
            assert applied_result.replayed is False
            assert persisted_result.replayed is True
            assert target_person_id is not None
            assert binding_rows == [
                (
                    legacy_user_id,
                    target_person_id,
                    legacy_user_id,
                    target_person_id,
                    "dingtalk",
                    corporation_id,
                    personnel_id,
                    "dingtalk",
                    corporation_id,
                    personnel_id,
                )
            ]
        else:
            assert applied_result is None
            assert persisted_result is None
            assert target_person_id is None
            assert binding_rows == []
    finally:
        identity_engine.dispose()
        setup_engine.dispose()  # type: ignore[union-attr]
