from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID

import httpx
import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError
from sqlalchemy import create_engine
from sqlalchemy.pool import QueuePool

from app.composition.school_fact_executor_client_settings import SchoolFactExecutorClientSettings
from app.composition.school_fact_writer_client import (
    SchoolFactWriterExecutorClient,
    SchoolFactWriterExecutorUnavailable,
)
from app.composition.school_fact_writer_executor import (
    SchoolFactWriterExecutorKernel,
    SqlAlchemySchoolFactWriterExecutorKernel,
    create_school_fact_writer_executor_app,
)
from app.composition.school_fact_writer_settings import (
    EXPECTED_SCHOOL_FACT_WRITER_ROLE,
    REQUIRED_SF5B_ALEMBIC_HEAD,
    SchoolFactWriterConnectionFacts,
    SchoolFactWriterSettings,
    WriterStartupRejected,
    build_school_fact_writer_engine,
    require_school_fact_writer_connection,
)
from app.platform import contracts as platform_contracts
from app.platform.contracts import CommandExecutionContext, SchoolId
from app.platform.identity.executor_context_codec import (
    CommandContextTransportCodec,
    CommandContextTransportRejected,
)
from app.platform.school_facts.application import SchoolFactChangeRejected
from app.platform.school_facts.contracts import (
    CanonicalFactChangeSetV1,
    ConfirmationReceiptV1,
    PreflightReportV1,
    canonical_plan_sha256,
)
from app.platform.school_facts.executor_contracts import (
    SchoolFactExecutorConfirmRequest,
    SchoolFactExecutorDigestResultRequest,
    SchoolFactExecutorLocatorResultRequest,
    SchoolFactExecutorPreflightRequest,
    SchoolFactExecutorPrepareRequest,
    SchoolFactExecutorProvenance,
)
from app.platform.school_facts.ports import SchoolFactApplyResult, SchoolFactResultLookup

NOW = datetime(2026, 8, 24, 8, tzinfo=UTC)
SCHOOL_ID = UUID("00000000-0000-0000-0000-000000000001")
PERSON_ID = UUID("00000000-0000-0000-0000-000000000002")
ASSIGNMENT_ID = UUID("00000000-0000-0000-0000-000000000003")
REQUEST_ID = UUID("00000000-0000-0000-0000-000000000004")
ANCHOR_ID = UUID("00000000-0000-0000-0000-000000000005")
CALLER_KEY = b"synthetic-executor-caller-key-32-bytes"
CONTEXT_KEY = b"synthetic-context-transport-key-32-bytes"


def _context() -> CommandExecutionContext:
    actor = platform_contracts._issue_staff_actor_context(  # pyright: ignore[reportPrivateUsage]
        account_user_id=19,
        school_id=SchoolId(SCHOOL_ID),
        person_id=platform_contracts.PersonId(PERSON_ID),
        assignment_id=platform_contracts.AssignmentId(ASSIGNMENT_ID),
        assignment_version=7,
        authorization_version=11,
    )
    return platform_contracts._issue_command_execution_context(  # pyright: ignore[reportPrivateUsage]
        actor=actor,
        request_id=REQUEST_ID,
        idempotency_key="sf5b-writer-executor-test",
    )


def _change_set() -> CanonicalFactChangeSetV1:
    return CanonicalFactChangeSetV1.model_validate(
        {
            "schema_version": 1,
            "source_sha256": "a" * 64,
            "base_version": 4,
            "changes": (
                {
                    "schema_version": 1,
                    "operation": "upsert",
                    "fact_kind": "school",
                    "source_key": "school:synthetic",
                    "expected_version": 4,
                    "effective_at": NOW,
                    "origin": {"sheet_code": "schools", "row_number": 2},
                    "content": {
                        "business_code": "SYNTHETIC-001",
                        "display_name": "虚构学校",
                        "timezone_name": "Asia/Shanghai",
                        "lifecycle_status": "active",
                    },
                },
            ),
        }
    )


@pytest.mark.parametrize("forbidden", ["sql", "where", "table", "command"])
def test_executor_protocol_rejects_generic_database_or_command_fields(forbidden: str) -> None:
    payload: dict[str, object] = {
        "school_id": str(SCHOOL_ID),
        "change_set": _change_set().model_dump(mode="json", exclude_none=True),
        forbidden: "SELECT * FROM schools",
    }

    with pytest.raises(ValidationError):
        SchoolFactExecutorPreflightRequest.model_validate(payload)


def test_preflight_request_accepts_its_json_wire_school_id() -> None:
    token = CommandContextTransportCodec(CONTEXT_KEY, clock=lambda: NOW).encode(_context())
    request = SchoolFactExecutorPreflightRequest(
        school_id=SCHOOL_ID,
        change_set=_change_set(),
        context_token=token,
        session_binding="server-session-A",
    )

    restored = SchoolFactExecutorPreflightRequest.model_validate(
        request.model_dump(mode="json", exclude_none=True)
    )

    assert restored.school_id == SCHOOL_ID


def test_preflight_and_result_requests_require_trusted_context_and_session_binding() -> None:
    change_set = _change_set()
    token = CommandContextTransportCodec(CONTEXT_KEY, clock=lambda: NOW).encode(_context())
    preflight_payload = {
        "school_id": str(SCHOOL_ID),
        "change_set": change_set.model_dump(mode="json", exclude_none=True),
    }
    digest_payload = {
        "lookup_kind": "digest",
        "school_id": str(SCHOOL_ID),
        "source_sha256": "a" * 64,
        "plan_sha256": "b" * 64,
    }
    locator_payload = {
        "lookup_kind": "locator",
        "school_id": str(SCHOOL_ID),
        "actor_user_id": 19,
        "locator_sha256": "c" * 64,
    }

    for model, payload in (
        (SchoolFactExecutorPreflightRequest, preflight_payload),
        (SchoolFactExecutorDigestResultRequest, digest_payload),
        (SchoolFactExecutorLocatorResultRequest, locator_payload),
    ):
        with pytest.raises(ValidationError):
            model.model_validate(payload)
        restored = model.model_validate(
            {
                **payload,
                "context_token": token,
                "session_binding": "server-session-A",
            }
        )
        assert restored.context_token == token
        assert restored.session_binding == "server-session-A"


def test_prepare_request_accepts_its_json_wire_provenance_sequences() -> None:
    change_set = _change_set()
    request = SchoolFactExecutorPrepareRequest(
        plan_sha256=canonical_plan_sha256(change_set),
        change_set=change_set,
        context_token=CommandContextTransportCodec(CONTEXT_KEY, clock=lambda: NOW).encode(
            _context()
        ),
        session_binding="server-session-A",
        provenance=SchoolFactExecutorProvenance(
            channel="admin",
            raw_source_sha256=change_set.source_sha256,
            template_version="school_facts_v2",
            mapping_version="admin_v2",
            mapping_config_sha256="c" * 64,
            result_locator_sha256="d" * 64,
        ),
    )

    restored = SchoolFactExecutorPrepareRequest.model_validate(
        request.model_dump(mode="json", exclude_none=True)
    )

    assert restored.provenance == request.provenance


def test_command_context_transport_round_trip_and_tamper_fail_closed() -> None:
    codec = CommandContextTransportCodec(CONTEXT_KEY, clock=lambda: NOW)
    token = codec.encode(_context())

    restored = codec.decode(token)
    assert restored == _context()

    replacement = "A" if token[-1] != "A" else "B"
    with pytest.raises(CommandContextTransportRejected):
        codec.decode(f"{token[:-1]}{replacement}")


@dataclass
class _RecordingKernel(SchoolFactWriterExecutorKernel):
    contexts: list[CommandExecutionContext] = field(default_factory=list)
    operations: list[str] = field(default_factory=list)

    def preflight(
        self,
        *,
        school_id: SchoolId,
        change_set: CanonicalFactChangeSetV1,
        context: CommandExecutionContext,
        session_binding: str,
    ) -> PreflightReportV1:
        assert UUID(str(school_id)) == SCHOOL_ID
        assert session_binding == "server-session-A"
        self.contexts.append(context)
        self.operations.append("preflight")
        return PreflightReportV1(
            plan_sha256=canonical_plan_sha256(change_set),
            is_clean=True,
            issues=(),
        )

    def prepare(
        self,
        *,
        change_set: CanonicalFactChangeSetV1,
        context: CommandExecutionContext,
        session_binding: str,
        provenance: object | None,
    ) -> ConfirmationReceiptV1:
        del change_set, provenance
        assert session_binding == "server-session-A"
        self.contexts.append(context)
        return ConfirmationReceiptV1(
            plan_sha256="b" * 64,
            credential="C" * 43,
            expires_at=datetime(2026, 8, 24, 9, tzinfo=UTC),
        )

    def confirm(
        self,
        *,
        credential: str,
        change_set: CanonicalFactChangeSetV1,
        context: CommandExecutionContext,
        session_binding: str,
    ) -> SchoolFactApplyResult:
        del credential, change_set, session_binding
        self.contexts.append(context)
        return SchoolFactApplyResult(
            batch_id=UUID("00000000-0000-0000-0000-000000000006"),
            target_version=5,
            changed_count=1,
            replayed=False,
        )

    def result_by_digest(
        self,
        *,
        school_id: SchoolId,
        source_sha256: str,
        plan_sha256: str,
        context: CommandExecutionContext,
        session_binding: str,
    ) -> SchoolFactResultLookup:
        del school_id, source_sha256, plan_sha256
        assert session_binding == "server-session-A"
        self.contexts.append(context)
        self.operations.append("digest")
        return SchoolFactResultLookup(found=False, result=None)

    def result_by_locator(
        self,
        *,
        school_id: SchoolId,
        actor_user_id: int,
        session_binding: str,
        locator_sha256: str,
        context: CommandExecutionContext,
    ) -> SchoolFactResultLookup:
        del school_id, actor_user_id, session_binding, locator_sha256
        self.contexts.append(context)
        self.operations.append("locator")
        return SchoolFactResultLookup(found=False, result=None)


def _app_client() -> tuple[TestClient, _RecordingKernel, CommandContextTransportCodec]:
    kernel = _RecordingKernel()
    codec = CommandContextTransportCodec(CONTEXT_KEY, clock=lambda: NOW)
    app = create_school_fact_writer_executor_app(
        kernel=kernel,
        context_codec=codec,
        caller_auth_key=CALLER_KEY,
    )
    return TestClient(app), kernel, codec


def test_executor_app_exposes_only_four_fixed_internal_operations() -> None:
    client, _, _ = _app_client()
    routes = {
        route.path for route in cast(FastAPI, client.app).routes if isinstance(route, APIRoute)
    }
    assert routes == {
        "/internal/v1/school-facts/preflight",
        "/internal/v1/school-facts/prepare",
        "/internal/v1/school-facts/confirm",
        "/internal/v1/school-facts/result",
    }


def test_executor_app_rejects_wrong_auth_unknown_fields_and_tampered_context() -> None:
    client, kernel, codec = _app_client()
    plan = _change_set()
    prepare_payload = {
        "plan_sha256": canonical_plan_sha256(plan),
        "change_set": plan.model_dump(mode="json", exclude_none=True),
        "context_token": codec.encode(_context()),
        "session_binding": "server-session-A",
        "provenance": None,
    }

    wrong_auth = client.post(
        "/internal/v1/school-facts/prepare",
        headers={"X-School-Fact-Executor-Key": "wrong-key"},
        json=prepare_payload,
    )
    assert wrong_auth.status_code == 401

    unknown = client.post(
        "/internal/v1/school-facts/prepare",
        headers={"X-School-Fact-Executor-Key": CALLER_KEY.decode("ascii")},
        json={**prepare_payload, "sql": "DELETE FROM schools"},
    )
    assert unknown.status_code == 422

    token = str(prepare_payload["context_token"])
    tampered = client.post(
        "/internal/v1/school-facts/prepare",
        headers={"X-School-Fact-Executor-Key": CALLER_KEY.decode("ascii")},
        json={**prepare_payload, "context_token": f"{token[:-1]}A"},
    )
    assert tampered.status_code == 401
    assert kernel.contexts == []


def test_executor_app_accepts_verified_context_on_the_fixed_prepare_path() -> None:
    client, kernel, codec = _app_client()
    change_set = _change_set()
    response = client.post(
        "/internal/v1/school-facts/prepare",
        headers={"X-School-Fact-Executor-Key": CALLER_KEY.decode("ascii")},
        json={
            "plan_sha256": canonical_plan_sha256(change_set),
            "change_set": change_set.model_dump(mode="json", exclude_none=True),
            "context_token": codec.encode(_context()),
            "session_binding": "server-session-A",
            "provenance": None,
        },
    )

    assert response.status_code == 200
    assert kernel.contexts == [_context()]


def test_executor_app_verifies_context_on_preflight_and_both_result_paths() -> None:
    client, kernel, codec = _app_client()
    token = codec.encode(_context())
    headers = {"X-School-Fact-Executor-Key": CALLER_KEY.decode("ascii")}

    preflight = client.post(
        "/internal/v1/school-facts/preflight",
        headers=headers,
        json={
            "school_id": str(SCHOOL_ID),
            "change_set": _change_set().model_dump(mode="json", exclude_none=True),
            "context_token": token,
            "session_binding": "server-session-A",
        },
    )
    digest = client.post(
        "/internal/v1/school-facts/result",
        headers=headers,
        json={
            "lookup_kind": "digest",
            "school_id": str(SCHOOL_ID),
            "source_sha256": "a" * 64,
            "plan_sha256": "b" * 64,
            "context_token": token,
            "session_binding": "server-session-A",
        },
    )
    locator = client.post(
        "/internal/v1/school-facts/result",
        headers=headers,
        json={
            "lookup_kind": "locator",
            "school_id": str(SCHOOL_ID),
            "actor_user_id": 19,
            "locator_sha256": "c" * 64,
            "context_token": token,
            "session_binding": "server-session-A",
        },
    )

    assert [preflight.status_code, digest.status_code, locator.status_code] == [200, 200, 200]
    assert kernel.operations == ["preflight", "digest", "locator"]
    assert kernel.contexts == [_context(), _context(), _context()]


@pytest.mark.parametrize(
    "path,payload",
    [
        (
            "/internal/v1/school-facts/preflight",
            {
                "school_id": str(SCHOOL_ID),
                "change_set": _change_set().model_dump(mode="json", exclude_none=True),
                "session_binding": "server-session-A",
            },
        ),
        (
            "/internal/v1/school-facts/result",
            {
                "lookup_kind": "digest",
                "school_id": str(SCHOOL_ID),
                "source_sha256": "a" * 64,
                "plan_sha256": "b" * 64,
                "session_binding": "server-session-A",
            },
        ),
        (
            "/internal/v1/school-facts/result",
            {
                "lookup_kind": "locator",
                "school_id": str(SCHOOL_ID),
                "actor_user_id": 19,
                "locator_sha256": "c" * 64,
                "session_binding": "server-session-A",
            },
        ),
    ],
)
def test_executor_app_rejects_tampered_context_on_read_paths(
    path: str,
    payload: dict[str, object],
) -> None:
    client, kernel, codec = _app_client()
    token = codec.encode(_context())

    response = client.post(
        path,
        headers={"X-School-Fact-Executor-Key": CALLER_KEY.decode("ascii")},
        json={**payload, "context_token": f"{token[:-1]}A"},
    )

    assert response.status_code == 401
    assert kernel.contexts == []


def _writer_settings() -> SchoolFactWriterSettings:
    return SchoolFactWriterSettings(
        database_host="127.0.0.1",
        database_port=5432,
        database_name="campus_ai",
        database_user=EXPECTED_SCHOOL_FACT_WRITER_ROLE,
        database_password=SecretStr("synthetic-writer-database-password"),
        expected_anchor_id=ANCHOR_ID,
        expected_school_id=SCHOOL_ID,
        expected_system_identifier="synthetic-school-database",
        confirmation_hmac_key=SecretStr("synthetic-confirmation-key-32-bytes"),
        caller_auth_key=SecretStr(CALLER_KEY.decode("ascii")),
        context_transport_hmac_key=SecretStr(CONTEXT_KEY.decode("ascii")),
        rls_context_signing_key=SecretStr("synthetic-rls-context-key-32-bytes"),
        pool_size=3,
        max_overflow=1,
        pool_timeout_seconds=2,
        pool_recycle_seconds=300,
        connect_timeout_seconds=2,
        statement_timeout_ms=15_000,
        lock_timeout_ms=2_000,
    )


def test_writer_database_context_proof_covers_the_formal_confirmation_window() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    try:
        kernel = SqlAlchemySchoolFactWriterExecutorKernel(
            engine=engine,
            settings=_writer_settings(),
        )
        issued_after = datetime.now(UTC)
        service = kernel._service(  # pyright: ignore[reportPrivateUsage]
            context=_context(),
            session_binding="server-session-A",
        )
        persistence = cast(Any, service._writer)  # pyright: ignore[reportPrivateUsage]
        envelope = persistence._envelope_factory(  # pyright: ignore[reportPrivateUsage]
            _context()
        )
        issued_before = datetime.now(UTC)

        assert issued_after + timedelta(minutes=30) <= envelope.proof_expires_at
        assert envelope.proof_expires_at <= issued_before + timedelta(minutes=30)
    finally:
        engine.dispose()


def test_writer_and_api_client_settings_keep_database_secret_on_writer_side_only() -> None:
    writer_fields = set(SchoolFactWriterSettings.model_fields)
    client_fields = set(SchoolFactExecutorClientSettings.model_fields)

    assert "database_password" in writer_fields
    assert "confirmation_hmac_key" in writer_fields
    assert "migration_database_url" not in writer_fields
    assert "owner_database_url" not in writer_fields
    assert client_fields == {
        "base_url",
        "caller_auth_key",
        "context_transport_hmac_key",
        "request_timeout_seconds",
    }


@pytest.mark.parametrize(
    ("left_field", "right_field"),
    [
        ("confirmation_hmac_key", "caller_auth_key"),
        ("confirmation_hmac_key", "context_transport_hmac_key"),
        ("confirmation_hmac_key", "rls_context_signing_key"),
        ("caller_auth_key", "context_transport_hmac_key"),
        ("caller_auth_key", "rls_context_signing_key"),
        ("context_transport_hmac_key", "rls_context_signing_key"),
    ],
)
def test_writer_rejects_cross_purpose_hmac_key_reuse_without_echoing_secret(
    left_field: str,
    right_field: str,
) -> None:
    reused = "never-log-cross-purpose-secret-0000001"
    values = _writer_settings().model_dump()
    values[left_field] = SecretStr(reused)
    values[right_field] = SecretStr(reused)

    with pytest.raises(ValidationError) as raised:
        SchoolFactWriterSettings.model_validate(values)

    assert reused not in str(raised.value)
    assert reused not in repr(raised.value)


@pytest.mark.parametrize(
    "other_field",
    [
        "confirmation_hmac_key",
        "caller_auth_key",
        "context_transport_hmac_key",
        "rls_context_signing_key",
    ],
)
def test_writer_rejects_database_password_reuse_without_echoing_secret(
    other_field: str,
) -> None:
    reused = "never-log-database-cross-purpose-secret-0001"
    values = _writer_settings().model_dump()
    values["database_password"] = SecretStr(reused)
    values[other_field] = SecretStr(reused)

    with pytest.raises(ValidationError) as raised:
        SchoolFactWriterSettings.model_validate(values)

    assert reused not in str(raised.value)
    assert reused not in repr(raised.value)


def test_api_client_rejects_caller_context_key_reuse_without_echoing_secret() -> None:
    reused = "never-log-api-cross-purpose-secret-0001"

    with pytest.raises(ValidationError) as raised:
        SchoolFactExecutorClientSettings(
            base_url="http://school-fact-writer:8010",
            caller_auth_key=SecretStr(reused),
            context_transport_hmac_key=SecretStr(reused),
        )

    assert reused not in str(raised.value)
    assert reused not in repr(raised.value)


def test_api_and_writer_accept_matching_keys_for_the_same_transport_purposes() -> None:
    writer = _writer_settings()
    client = SchoolFactExecutorClientSettings(
        base_url="http://school-fact-writer:8010",
        caller_auth_key=writer.caller_auth_key,
        context_transport_hmac_key=writer.context_transport_hmac_key,
    )

    assert client.caller_auth_key.get_secret_value() == (writer.caller_auth_key.get_secret_value())
    assert client.context_transport_hmac_key.get_secret_value() == (
        writer.context_transport_hmac_key.get_secret_value()
    )


def test_writer_engine_has_fixed_role_and_bounded_fail_closed_pool() -> None:
    engine = build_school_fact_writer_engine(_writer_settings())
    try:
        assert engine.url.username == EXPECTED_SCHOOL_FACT_WRITER_ROLE
        pool = cast(QueuePool, engine.pool)
        assert pool.size() == 3  # pyright: ignore[reportUnknownMemberType]
        assert pool._max_overflow == 1  # pyright: ignore[reportPrivateUsage]
        assert pool._timeout == 2  # pyright: ignore[reportPrivateUsage]
        assert pool._recycle == 300  # pyright: ignore[reportPrivateUsage]
        assert pool._pre_ping is True  # pyright: ignore[reportPrivateUsage]
        assert str(pool._reset_on_return).endswith("reset_rollback")  # pyright: ignore[reportPrivateUsage]
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "facts",
    [
        SchoolFactWriterConnectionFacts(
            current_user="other-role",
            session_user=EXPECTED_SCHOOL_FACT_WRITER_ROLE,
            is_superuser=False,
            bypasses_rls=False,
            alembic_head=REQUIRED_SF5B_ALEMBIC_HEAD,
            anchored_school_id=SCHOOL_ID,
        ),
        SchoolFactWriterConnectionFacts(
            current_user=EXPECTED_SCHOOL_FACT_WRITER_ROLE,
            session_user=EXPECTED_SCHOOL_FACT_WRITER_ROLE,
            is_superuser=True,
            bypasses_rls=False,
            alembic_head=REQUIRED_SF5B_ALEMBIC_HEAD,
            anchored_school_id=SCHOOL_ID,
        ),
        SchoolFactWriterConnectionFacts(
            current_user=EXPECTED_SCHOOL_FACT_WRITER_ROLE,
            session_user=EXPECTED_SCHOOL_FACT_WRITER_ROLE,
            is_superuser=False,
            bypasses_rls=False,
            has_privileged_membership=True,
            alembic_head=REQUIRED_SF5B_ALEMBIC_HEAD,
            anchored_school_id=SCHOOL_ID,
        ),
        SchoolFactWriterConnectionFacts(
            current_user=EXPECTED_SCHOOL_FACT_WRITER_ROLE,
            session_user=EXPECTED_SCHOOL_FACT_WRITER_ROLE,
            is_superuser=False,
            bypasses_rls=False,
            alembic_head="stale-head",
            anchored_school_id=SCHOOL_ID,
        ),
        SchoolFactWriterConnectionFacts(
            current_user=EXPECTED_SCHOOL_FACT_WRITER_ROLE,
            session_user=EXPECTED_SCHOOL_FACT_WRITER_ROLE,
            is_superuser=False,
            bypasses_rls=False,
            alembic_head=REQUIRED_SF5B_ALEMBIC_HEAD,
            anchored_school_id=UUID("00000000-0000-0000-0000-000000000099"),
        ),
    ],
)
def test_writer_startup_connection_gate_fails_closed(
    facts: SchoolFactWriterConnectionFacts,
) -> None:
    with pytest.raises(WriterStartupRejected):
        require_school_fact_writer_connection(facts, settings=_writer_settings())


def test_fixed_client_proxy_uses_only_typed_paths_and_auth_key() -> None:
    requests: list[httpx.Request] = []
    change_set = _change_set()

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "plan_sha256": canonical_plan_sha256(change_set),
                "is_clean": True,
                "issues": [],
                "confirmation_credential": None,
            },
        )

    settings = SchoolFactExecutorClientSettings(
        base_url="http://writer-executor.invalid",
        caller_auth_key=SecretStr(CALLER_KEY.decode("ascii")),
        context_transport_hmac_key=SecretStr(CONTEXT_KEY.decode("ascii")),
        request_timeout_seconds=2,
    )
    client = SchoolFactWriterExecutorClient(
        settings=settings,
        context_codec=CommandContextTransportCodec(CONTEXT_KEY, clock=lambda: NOW),
        transport=httpx.MockTransport(handler),
    )

    report = client.preflight(
        school_id=SchoolId(SCHOOL_ID),
        change_set=change_set,
        context=_context(),
        session_binding="server-session-A",
    )

    assert report.is_clean is True
    assert [request.url.path for request in requests] == ["/internal/v1/school-facts/preflight"]
    assert requests[0].headers["X-School-Fact-Executor-Key"] == CALLER_KEY.decode("ascii")
    forwarded = SchoolFactExecutorPreflightRequest.model_validate_json(requests[0].content)
    assert forwarded.session_binding == "server-session-A"
    assert (
        CommandContextTransportCodec(CONTEXT_KEY, clock=lambda: NOW).decode(forwarded.context_token)
        == _context()
    )


def test_fixed_client_ignores_hostile_process_proxy_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[httpx.Request] = []
    change_set = _change_set()

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "plan_sha256": canonical_plan_sha256(change_set),
                "is_clean": True,
                "issues": [],
                "confirmation_credential": None,
            },
            request=request,
        )

    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "")

    with _mock_executor_client(httpx.MockTransport(handler)) as client:
        assert client._client._trust_env is False  # pyright: ignore[reportPrivateUsage]
        report = client.preflight(
            school_id=SchoolId(SCHOOL_ID),
            change_set=change_set,
            context=_context(),
            session_binding="server-session-A",
        )

    assert report.is_clean is True
    assert [request.url.path for request in requests] == ["/internal/v1/school-facts/preflight"]


def _mock_executor_client(transport: httpx.BaseTransport) -> SchoolFactWriterExecutorClient:
    return SchoolFactWriterExecutorClient(
        settings=SchoolFactExecutorClientSettings(
            base_url="http://writer-executor.invalid",
            caller_auth_key=SecretStr(CALLER_KEY.decode("ascii")),
            context_transport_hmac_key=SecretStr(CONTEXT_KEY.decode("ascii")),
            request_timeout_seconds=2,
        ),
        context_codec=CommandContextTransportCodec(CONTEXT_KEY, clock=lambda: NOW),
        transport=transport,
    )


def test_fixed_client_preserves_only_explicit_executor_domain_conflict() -> None:
    upstream_marker = "must-not-cross-the-executor-boundary"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            409,
            json={"detail": upstream_marker},
            request=request,
        )

    with _mock_executor_client(httpx.MockTransport(handler)) as client:
        with pytest.raises(SchoolFactChangeRejected) as raised:
            client.confirm(
                credential="C" * 43,
                change_set=_change_set(),
                context=_context(),
                session_binding="server-session-A",
            )

    assert upstream_marker not in str(raised.value)


@pytest.mark.parametrize("scenario", ["401", "403", "422", "503", "network", "malformed"])
def test_fixed_client_keeps_non_conflict_failures_unavailable(scenario: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if scenario == "network":
            raise httpx.ConnectError("synthetic connection failure", request=request)
        if scenario == "malformed":
            return httpx.Response(200, content=b"not-json", request=request)
        return httpx.Response(int(scenario), json={"detail": "rejected"}, request=request)

    with _mock_executor_client(httpx.MockTransport(handler)) as client:
        with pytest.raises(SchoolFactWriterExecutorUnavailable):
            client.preflight(
                school_id=SchoolId(SCHOOL_ID),
                change_set=_change_set(),
                context=_context(),
                session_binding="server-session-A",
            )


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param({"found": True, "result": None}, id="found-without-result"),
        pytest.param(
            {
                "found": False,
                "result": {
                    "batch_id": "00000000-0000-0000-0000-000000000006",
                    "target_version": 5,
                    "changed_count": 1,
                    "replayed": False,
                },
            },
            id="result-without-found",
        ),
    ],
)
def test_fixed_client_rejects_contradictory_successful_result_response(
    payload: dict[str, object],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload, request=request)

    with _mock_executor_client(httpx.MockTransport(handler)) as client:
        with pytest.raises(SchoolFactWriterExecutorUnavailable):
            client.result_by_digest(
                school_id=SchoolId(SCHOOL_ID),
                source_sha256="a" * 64,
                plan_sha256="b" * 64,
                context=_context(),
                session_binding="server-session-A",
            )


def test_protocol_models_cover_digest_and_locator_result_without_generic_query() -> None:
    token = CommandContextTransportCodec(CONTEXT_KEY, clock=lambda: NOW).encode(_context())
    digest = SchoolFactExecutorDigestResultRequest(
        lookup_kind="digest",
        school_id=SCHOOL_ID,
        source_sha256="a" * 64,
        plan_sha256="b" * 64,
        context_token=token,
        session_binding="server-session-A",
    )
    locator = SchoolFactExecutorLocatorResultRequest(
        lookup_kind="locator",
        school_id=SCHOOL_ID,
        actor_user_id=19,
        context_token=token,
        session_binding="server-session-A",
        locator_sha256="c" * 64,
    )

    assert digest.lookup_kind == "digest"
    assert locator.lookup_kind == "locator"
    with pytest.raises(ValidationError):
        SchoolFactExecutorLocatorResultRequest.model_validate(
            {**locator.model_dump(mode="json", exclude_none=True), "where": "TRUE"}
        )


def test_prepare_and_confirm_protocol_require_bound_plan_digest() -> None:
    token = CommandContextTransportCodec(CONTEXT_KEY, clock=lambda: NOW).encode(_context())
    common: dict[str, Any] = {
        "plan_sha256": canonical_plan_sha256(_change_set()),
        "change_set": _change_set().model_dump(mode="json", exclude_none=True),
        "context_token": token,
        "session_binding": "server-session-A",
    }
    prepare = SchoolFactExecutorPrepareRequest.model_validate({**common, "provenance": None})
    confirm = SchoolFactExecutorConfirmRequest.model_validate({**common, "credential": "C" * 43})

    assert prepare.plan_sha256 == canonical_plan_sha256(_change_set())
    assert confirm.plan_sha256 == canonical_plan_sha256(_change_set())


def test_executor_recomputes_plan_digest_before_dispatch() -> None:
    client, kernel, codec = _app_client()
    change_set = _change_set()
    response = client.post(
        "/internal/v1/school-facts/prepare",
        headers={"X-School-Fact-Executor-Key": CALLER_KEY.decode("ascii")},
        json={
            "plan_sha256": "0" * 64,
            "change_set": change_set.model_dump(mode="json", exclude_none=True),
            "context_token": codec.encode(_context()),
            "session_binding": "server-session-A",
            "provenance": None,
        },
    )

    assert response.status_code == 409
    assert kernel.contexts == []


def test_caller_bearer_key_cannot_sign_identity_transport_context() -> None:
    client, kernel, _ = _app_client()
    change_set = _change_set()
    wrong_domain_codec = CommandContextTransportCodec(CALLER_KEY, clock=lambda: NOW)
    response = client.post(
        "/internal/v1/school-facts/prepare",
        headers={"X-School-Fact-Executor-Key": CALLER_KEY.decode("ascii")},
        json={
            "plan_sha256": canonical_plan_sha256(change_set),
            "change_set": change_set.model_dump(mode="json", exclude_none=True),
            "context_token": wrong_domain_codec.encode(_context()),
            "session_binding": "server-session-A",
            "provenance": None,
        },
    )

    assert response.status_code == 401
    assert kernel.contexts == []


@pytest.mark.parametrize("schema_version", [1, 2])
@pytest.mark.parametrize("operation", ["end", "revoke"])
@pytest.mark.parametrize("phase", ["preflight", "prepare", "confirm"])
def test_fixed_client_terminal_change_survives_actual_executor_json_validation(
    schema_version: int, operation: str, phase: str
) -> None:
    import json

    raw = _change_set().model_dump(mode="json", exclude_none=True)
    raw["schema_version"] = schema_version
    raw["changes"][0].update(schema_version=schema_version, operation=operation, content=None)
    plan = CanonicalFactChangeSetV1.model_validate_json(json.dumps(raw))
    server, kernel, _ = _app_client()
    forwarded: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        forwarded.append(request)
        response = server.post(
            request.url.path, headers=dict(request.headers), content=request.content
        )
        return httpx.Response(response.status_code, content=response.content, request=request)

    with _mock_executor_client(httpx.MockTransport(handler)) as client:
        common: dict[str, Any] = dict(
            change_set=plan, context=_context(), session_binding="server-session-A"
        )
        if phase == "preflight":
            client.preflight(school_id=SchoolId(SCHOOL_ID), **common)
        elif phase == "prepare":
            client.prepare(**common)
        else:
            client.confirm(credential="C" * 43, **common)
    assert len(forwarded) == len(kernel.contexts) == 1
    wire = json.loads(forwarded[0].content)
    assert wire["change_set"]["changes"][0]["content"] is None
    assert wire["change_set"]["schema_version"] == schema_version


@pytest.mark.parametrize(
    "case", ["coverage_v1", "coverage_v2", "open_timeline", "explicit_unknown"]
)
@pytest.mark.parametrize("phase", ["preflight", "prepare", "confirm"])
def test_fixed_client_preserves_required_nullable_nested_contracts(case: str, phase: str) -> None:
    import json

    from app.platform.school_facts.contracts import CanonicalFactChangeSetV4
    from tests.school_facts.test_f7_coverage_basis import (
        _basis,
        _change,
        _grade_group_collection_basis,
        _grade_group_collection_change,
    )

    if case == "coverage_v1":
        change = _change(basis=_basis() | {"school_id": SCHOOL_ID})
    elif case == "coverage_v2":
        change = _grade_group_collection_change(
            _grade_group_collection_basis(SCHOOL_ID, (("group:a", 3),))
        )
    elif case == "explicit_unknown":
        from tests.school_facts.test_f6_residence_contracts import ENROLLMENT
        from tests.school_facts.test_f6_residence_contracts import change as residence_change

        change = residence_change("student_school_relationship", ENROLLMENT).model_dump(
            mode="json", exclude_none=True
        )
    else:
        change = {
            "schema_version": 4,
            "fact_kind": "education_stage",
            "source_key": "stage:synthetic",
            "expected_version": 1,
            "operation": "correct",
            "content": None,
            "correction_reason": "synthetic correction",
            "effective_at": NOW,
            "origin": {"sheet_code": "synthetic", "row_number": 1},
            "replacement_segments": [
                {
                    "effective_at": NOW,
                    "valid_to": None,
                    "content": {"display_name": "synthetic stage", "business_code": None},
                }
            ],
        }
    plan = CanonicalFactChangeSetV4.model_validate_json(
        json.dumps(
            {
                "schema_version": 4,
                "source_sha256": "a" * 64,
                "base_version": 12,
                "changes": [change],
            },
            default=str,
        )
    )
    server, kernel, _ = _app_client()
    forwarded: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        forwarded.append(json.loads(request.content))
        response = server.post(
            request.url.path, headers=dict(request.headers), content=request.content
        )
        return httpx.Response(response.status_code, content=response.content, request=request)

    with _mock_executor_client(httpx.MockTransport(handler)) as client:
        common = dict(change_set=plan, context=_context(), session_binding="server-session-A")
        if phase == "preflight":
            client.preflight(school_id=SchoolId(SCHOOL_ID), **common)
        elif phase == "prepare":
            client.prepare(**common)
        else:
            client.confirm(credential="C" * 43, **common)
    assert len(forwarded) == len(kernel.contexts) == 1
    wire = forwarded[0]["change_set"]["changes"][0]
    if case == "open_timeline":
        assert wire["content"] is None
        assert wire["replacement_segments"][0]["valid_to"] is None
        assert wire["replacement_segments"][0]["content"] == {
            "display_name": "synthetic stage", "business_code": None
        }
    elif case == "explicit_unknown":
        assert wire["content"]["residence_mode"] is None
        assert "business_code" not in wire["content"]
    else:
        basis = wire["content"]["coverage_basis"]
        assert basis["valid_to"] is None
        assert basis["purpose"] is None
        assert basis["contact_kind"] is None
        assert "valid_to" not in wire["content"]  # Optional unset fields stay omitted.
        assert "business_code" not in wire["content"]
    restored = CanonicalFactChangeSetV4.model_validate_json(json.dumps(forwarded[0]["change_set"]))
    assert canonical_plan_sha256(restored) == canonical_plan_sha256(plan)


def _nullable_package(explicit_null: bool):
    import json

    from app.platform.school_facts.package_integrity import full_scope_plan_sha256
    from app.platform.school_facts.ports import (
        SchoolFactChangeProvenance,
        SchoolFactRowProvenance,
        SchoolFactSourceArtifact,
    )

    raw_changes = []
    for index, (kind, content) in enumerate((
        ("organization", {"kind": "department", "display_name": "synthetic organization",
                          "display_path": "synthetic", "status": "active"}),
        ("education_stage", {"display_name": "synthetic stage"}),
    ), 2):
        if explicit_null:
            content["short_name" if kind == "organization" else "valid_to"] = None
        raw_changes.append({
            "schema_version": 4, "operation": "upsert", "fact_kind": kind,
            "source_key": f"synthetic:{kind}", "expected_version": 1,
            "effective_at": NOW.isoformat(),
            "origin": {"sheet_code": "synthetic", "row_number": index}, "content": content,
            **({"correction_reason": None} if explicit_null else {}),
        })
    plan = CanonicalFactChangeSetV1.model_validate_json(json.dumps({
        "schema_version": 4, "source_sha256": "a" * 64, "base_version": 49,
        "changes": raw_changes,
    }))
    lineage = tuple(SchoolFactRowProvenance(
        fact_kind=c.fact_kind, fact_source_key=c.source_key, raw_source_sha256="b" * 64,
        source_slot="synthetic", source_sheet_code="synthetic", source_row_number=index,
        target_field_codes=("display_name",), source_field_codes=("name",),
    ) for index, c in enumerate(plan.changes, 2))
    provenance = SchoolFactChangeProvenance(
        channel="package", raw_source_sha256="c" * 64, template_version="school_facts_v5",
        mapping_version="f7_v1", mapping_config_sha256="d" * 64,
        source_size_bytes=1, worksheet_names=(),
        source_manifest=(SchoolFactSourceArtifact("synthetic", "b" * 64, 1),),
        row_lineage=lineage,
    )
    digest = full_scope_plan_sha256(
        raw_source_sha256=provenance.raw_source_sha256, changes=plan.changes,
        row_lineage=lineage, policy_attachments=(),
    )
    return plan.model_copy(update={"source_sha256": digest}), provenance


@pytest.mark.parametrize("explicit_null", [True, False])
@pytest.mark.parametrize("phase", ["preflight", "prepare", "confirm"])
def test_fixed_client_preserves_package_presence_and_integrity_across_http(
    explicit_null: bool, phase: str
) -> None:
    import json

    from app.platform.school_facts.package_integrity import require_full_scope_provenance_integrity

    plan, provenance = _nullable_package(explicit_null)
    require_full_scope_provenance_integrity(provenance, plan)
    server, kernel, _ = _app_client()
    forwarded: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        forwarded.append(json.loads(request.content))
        response = server.post(
            request.url.path, headers=dict(request.headers), content=request.content
        )
        return httpx.Response(response.status_code, content=response.content, request=request)

    with _mock_executor_client(httpx.MockTransport(handler)) as client:
        common = dict(change_set=plan, context=_context(), session_binding="server-session-A")
        if phase == "preflight":
            client.preflight(school_id=SchoolId(SCHOOL_ID), **common)
        elif phase == "prepare":
            client.prepare(provenance=provenance, **common)
        else:
            client.confirm(credential="C" * 43, **common)
    assert len(forwarded) == len(kernel.contexts) == 1
    wire = forwarded[0]
    restored = CanonicalFactChangeSetV1.model_validate_json(json.dumps(wire["change_set"]))
    if phase == "prepare":
        transported = SchoolFactExecutorPrepareRequest.model_validate_json(json.dumps(wire))
        assert transported.provenance is not None
        assert transported.provenance.to_domain() == provenance
    require_full_scope_provenance_integrity(provenance, restored)
    for change in wire["change_set"]["changes"]:
        assert ("correction_reason" in change) is explicit_null
        field_name = "short_name" if change["fact_kind"] == "organization" else "valid_to"
        assert (field_name in change["content"]) is explicit_null
        assert "business_code" not in change["content"]
        assert "cause_source_key" not in change
        assert "replacement_segments" not in change


@pytest.mark.parametrize("tamper", ["remove_explicit_null", "rename", "lineage"])
def test_package_integrity_still_rejects_changed_fields_or_provenance(tamper: str) -> None:
    import json
    from dataclasses import replace

    from app.platform.school_facts.package_integrity import require_full_scope_provenance_integrity

    plan, provenance = _nullable_package(True)
    raw = plan.model_dump(mode="json", exclude_unset=True)
    if tamper == "remove_explicit_null":
        raw["changes"][1]["content"].pop("valid_to")
    elif tamper == "rename":
        raw["changes"][1]["content"]["display_name"] = "different synthetic stage"
    else:
        provenance = replace(provenance, row_lineage=(
            replace(provenance.row_lineage[0], source_field_codes=("different_column",)),
            provenance.row_lineage[1],
        ))
    changed = CanonicalFactChangeSetV1.model_validate_json(json.dumps(raw))
    with pytest.raises(ValueError, match="摘要"):
        require_full_scope_provenance_integrity(provenance, changed)
