from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.composition.school_fact_family_correction_executor import (
    CorrectionOption,
    FamilyCorrectionExecutorClient,
    FamilyCorrectionExecutorKernel,
    create_family_correction_executor_app,
)
from app.composition.school_fact_writer_executor import create_school_fact_writer_executor_app
from app.domains.students.family_corrections import (
    FamilyCorrectionDenied,
    FamilyCorrectionNotFound,
    FamilyCorrectionReport,
)
from app.platform.identity.executor_context_codec import CommandContextTransportCodec
from tests.school_facts.test_sf5b_writer_executor import (
    CALLER_KEY,
    CONTEXT_KEY,
    NOW,
    _change_set,
    _context,
    _RecordingKernel,
)

KEY = "k" * 32


class Codec:
    def encode(self, _context):
        return "token"

    def decode(self, token):
        assert token == "token"
        return SimpleNamespace(idempotency_key="idem", actor=object())


def report():
    return FamilyCorrectionReport(
        uuid4(),
        uuid4(),
        uuid4(),
        uuid4(),
        uuid4(),
        "student",
        "student:1",
        "status",
        "wrong",
        "submitted",
        datetime.now(UTC),
    )


def test_fixed_http_requires_caller_and_forwards_only_contract():
    kernel = MagicMock()
    kernel.submit.return_value = report()
    app = create_family_correction_executor_app(
        kernel=kernel, context_codec=Codec(), caller_auth_key=KEY
    )
    client = TestClient(app)
    body = {
        "context_token": "token",
        "session_binding": "s",
        "target_reference": "opaque",
        "field_name": "status",
        "statement": "wrong",
    }
    assert (
        client.post("/internal/v1/school-facts/family-corrections/submit", json=body).status_code
        == 401
    )
    response = client.post(
        "/internal/v1/school-facts/family-corrections/submit",
        json=body,
        headers={"X-School-Fact-Executor-Key": KEY},
    )
    assert response.status_code == 200 and response.json()["status"] == "submitted"
    kernel.submit.assert_called_once()


def test_client_uses_fixed_path_and_parses_receipt():
    item = report()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/internal/v1/school-facts/family-corrections/submit"
        assert request.headers["X-School-Fact-Executor-Key"] == KEY
        return httpx.Response(
            200,
            json={
                "report_id": str(item.report_id),
                "status": "submitted",
                "field_name": "status",
                "submitted_at": item.submitted_at.isoformat(),
                "resolved_at": None,
            },
        )

    settings = SimpleNamespace(
        base_url="http://writer",
        caller_auth_key=SimpleNamespace(get_secret_value=lambda: KEY),
        context_transport_hmac_key=SimpleNamespace(),
        request_timeout_seconds=2.0,
    )
    client = FamilyCorrectionExecutorClient(
        settings=settings, context_codec=Codec(), transport=httpx.MockTransport(handler)
    )
    receipt = client.submit(
        context=object(),
        session_binding="s",
        target_reference="opaque",
        field_name="status",
        statement="wrong",
    )
    assert receipt.report_id == item.report_id
    client.close()


@pytest.mark.parametrize("method", ["own", "resolve"])
def test_real_client_uuid_wire_contract_reaches_shared_writer_kernel(method):
    item = report()
    correction = MagicMock(spec=FamilyCorrectionExecutorKernel)
    getattr(correction, method).return_value = item
    app = create_school_fact_writer_executor_app(
        kernel=_RecordingKernel(),
        correction_kernel=correction,
        context_codec=Codec(),
        caller_auth_key=KEY,
    )
    wire_status = []
    with TestClient(app) as server:

        def handler(request):
            response = server.post(
                request.url.path,
                content=request.content,
                headers={"Content-Type": "application/json", "X-School-Fact-Executor-Key": KEY},
            )
            wire_status.append(response.status_code)
            return httpx.Response(response.status_code, content=response.content)

        client = FamilyCorrectionExecutorClient(
            settings=SimpleNamespace(
                base_url="http://writer",
                request_timeout_seconds=2.0,
                caller_auth_key=SimpleNamespace(get_secret_value=lambda: KEY),
            ),
            context_codec=Codec(),
            transport=httpx.MockTransport(handler),
        )
        try:
            kwargs = {
                "context": object(),
                "session_binding": "server-session",
                "report_id": item.report_id,
            }
            if method == "resolve":
                kwargs["writer_receipt_ref"] = "a" * 64
            result = getattr(client, method)(**kwargs)
        finally:
            client.close()
    assert wire_status == [200]
    assert result.report_id == item.report_id
    passed = getattr(correction, method).call_args.kwargs
    assert passed["report_id"] == item.report_id
    assert passed["session_binding"] == "server-session"


@pytest.mark.parametrize("method", ["own", "resolve"])
@pytest.mark.parametrize("invalid", [None, True, 42, [], {}, "not-a-uuid"])
def test_uuid_wire_parser_keeps_malformed_report_ids_out_of_kernel(method, invalid):
    correction = MagicMock(spec=FamilyCorrectionExecutorKernel)
    app = create_school_fact_writer_executor_app(
        kernel=_RecordingKernel(),
        correction_kernel=correction,
        context_codec=Codec(),
        caller_auth_key=KEY,
    )
    body = {"context_token": "token", "session_binding": "s", "report_id": invalid}
    if method == "resolve":
        body["writer_receipt_ref"] = "a" * 64
    with TestClient(app) as client:
        response = client.post(
            "/internal/v1/school-facts/family-corrections/" + method,
            json=body,
            headers={"X-School-Fact-Executor-Key": KEY},
        )
    assert response.status_code == 422
    getattr(correction, method).assert_not_called()


def test_resolve_uuid_parser_does_not_accept_extra_actor_or_authority_fields():
    correction = MagicMock(spec=FamilyCorrectionExecutorKernel)
    app = create_school_fact_writer_executor_app(
        kernel=_RecordingKernel(),
        correction_kernel=correction,
        context_codec=Codec(),
        caller_auth_key=KEY,
    )
    body = {
        "context_token": "token",
        "session_binding": "s",
        "report_id": str(uuid4()),
        "writer_receipt_ref": "a" * 64,
        "actor": {"school_id": str(uuid4())},
    }
    with TestClient(app) as client:
        response = client.post(
            "/internal/v1/school-facts/family-corrections/resolve",
            json=body,
            headers={"X-School-Fact-Executor-Key": KEY},
        )
    assert response.status_code == 422
    correction.resolve.assert_not_called()


def test_public_product_resolve_roundtrip_preserves_original_report_receipt_and_actor():
    from app.composition.school_fact_family_correction_product import FamilyCorrectionProduct
    from app.core.request_id import assign_request_id
    from app.interfaces.http.school_fact_family_corrections import create_family_correction_router

    original = replace(
        report(),
        field_name="region",
        fact_kind="person_region",
        status="resolved",
        resolved_at=datetime.now(UTC),
        resolution_locator_sha256="a" * 64,
    )
    correction = MagicMock(spec=FamilyCorrectionExecutorKernel)
    correction.resolve.return_value = original
    codec = CommandContextTransportCodec(CONTEXT_KEY, clock=lambda: NOW)
    writer = create_school_fact_writer_executor_app(
        kernel=_RecordingKernel(),
        correction_kernel=correction,
        context_codec=codec,
        caller_auth_key=KEY,
    )
    current = _context().actor
    identity = SimpleNamespace(
        actor=current,
        session_binding="server-session",
        actor_context_ref="selected-staff",
    )
    with TestClient(writer) as server:

        def handler(request):
            response = server.post(
                request.url.path,
                content=request.content,
                headers={"Content-Type": "application/json", "X-School-Fact-Executor-Key": KEY},
            )
            return httpx.Response(response.status_code, content=response.content)

        executor = FamilyCorrectionExecutorClient(
            settings=SimpleNamespace(
                base_url="http://writer",
                request_timeout_seconds=2.0,
                caller_auth_key=SimpleNamespace(get_secret_value=lambda: KEY),
            ),
            context_codec=codec,
            transport=httpx.MockTransport(handler),
        )
        product = FamilyCorrectionProduct(
            executor=executor,
            current_actor=lambda actor: actor == current,
            staff_source=MagicMock(),
        )
        app = FastAPI()
        app.middleware("http")(assign_request_id)
        app.include_router(
            create_family_correction_router(
                identity_dependency=lambda: identity,
                product=product,
            )
        )
        try:
            with TestClient(app) as browser:
                response = browser.post(
                    f"/api/v1/school-fact-product/family-facts/corrections/{original.report_id}/resolve",
                    json={"writer_receipt_ref": "sf2receipt_" + "a" * 64},
                )
        finally:
            executor.close()
    assert response.status_code == 200
    assert response.json()["status"] == "resolved"
    assert response.json()["report_id"] == str(original.report_id)
    correction.resolve.assert_called_once()
    args = correction.resolve.call_args.kwargs
    assert args["context"].actor == current
    assert args["report_id"] == original.report_id and args["writer_receipt_ref"] == "a" * 64
    assert args["session_binding"] == identity.session_binding
    correction.submit.assert_not_called()


def test_shared_writer_mounts_correction_routes_with_original_auth_and_base_routes():
    kernel = _RecordingKernel()
    correction = MagicMock(spec=FamilyCorrectionExecutorKernel)
    correction.options.return_value = (
        CorrectionOption(field_name="campus", label="当前校区", reference="opaque-campus"),
    )
    codec = CommandContextTransportCodec(CONTEXT_KEY, clock=lambda: NOW)
    app = create_school_fact_writer_executor_app(
        kernel=kernel,
        correction_kernel=correction,
        context_codec=codec,
        caller_auth_key=CALLER_KEY,
    )
    client = TestClient(app)
    path = "/internal/v1/school-facts/family-corrections/options"
    body = {"context_token": codec.encode(_context()), "session_binding": "server-session-A"}
    headers = {"X-School-Fact-Executor-Key": CALLER_KEY.decode()}
    assert client.post(path, json=body).status_code == 401
    assert (
        client.post(
            path, json=body, headers={**headers, "X-School-Fact-Executor-Key": "wrong"}
        ).status_code
        == 401
    )
    assert (
        client.post(path, json={**body, "context_token": "invalid"}, headers=headers).status_code
        == 401
    )
    correction.options.assert_not_called()

    response = client.post(path, json=body, headers=headers)
    assert response.status_code == 200
    assert response.json() == {
        "items": [{"field_name": "campus", "label": "当前校区", "reference": "opaque-campus"}]
    }
    correction.options.assert_called_once_with(
        context=_context(), session_binding="server-session-A"
    )
    assert kernel.operations == []

    response = client.post(
        "/internal/v1/school-facts/preflight",
        json={
            **body,
            "school_id": str(_context().actor.school_id),
            "change_set": _change_set().model_dump(mode="json", exclude_none=True),
        },
        headers=headers,
    )
    assert response.status_code == 200 and kernel.contexts == [_context()]
    expected_paths = {
        "/internal/v1/school-facts/" + name
        for name in ("preflight", "prepare", "confirm", "result")
    } | {
        "/internal/v1/school-facts/family-corrections/" + name
        for name in ("options", "own-list", "by-key", "submit", "own", "staff-list", "resolve")
    }
    for path in expected_paths:
        assert client.post(path, json={}).status_code == 401
    assert client.get("/docs").status_code == 404


def test_shared_writer_does_not_expose_correction_routes_without_kernel():
    app = create_school_fact_writer_executor_app(
        kernel=_RecordingKernel(), context_codec=Codec(), caller_auth_key=KEY
    )
    client = TestClient(app)
    for name in ("options", "own-list", "by-key", "submit", "own", "staff-list", "resolve"):
        response = client.post(
            "/internal/v1/school-facts/family-corrections/" + name,
            json={"context_token": "token", "session_binding": "s"},
            headers={"X-School-Fact-Executor-Key": KEY},
        )
        assert response.status_code == 404


def test_options_current_authority_denial_returns_safe_403():
    kernel = MagicMock(spec=FamilyCorrectionExecutorKernel)
    kernel.options.side_effect = FamilyCorrectionDenied("private authority detail")
    client = TestClient(
        create_family_correction_executor_app(
            kernel=kernel, context_codec=Codec(), caller_auth_key=KEY
        ),
        raise_server_exceptions=False,
    )
    response = client.post(
        "/internal/v1/school-facts/family-corrections/options",
        json={"context_token": "token", "session_binding": "s"},
        headers={"X-School-Fact-Executor-Key": KEY},
    )
    assert response.status_code == 403
    assert response.json() == {"detail": "Current correction authority changed."}
    assert "private" not in response.text


@pytest.mark.parametrize("staff", [False, True])
def test_list_response_contract_keeps_parent_minimal_and_staff_statement(staff):
    kernel = MagicMock(spec=FamilyCorrectionExecutorKernel)
    item = report()
    kernel.list_own.return_value = (item,)
    kernel.staff_list.return_value = (item,)
    client = TestClient(
        create_family_correction_executor_app(
            kernel=kernel, context_codec=Codec(), caller_auth_key=KEY
        ),
        raise_server_exceptions=False,
    )
    body = {"context_token": "token", "session_binding": "s"}
    if staff:
        body.update(
            fact_kind="student",
            source_key="student:1",
            field_name="status",
            required_action="maintain",
        )
    response = client.post(
        "/internal/v1/school-facts/family-corrections/" + ("staff-list" if staff else "own-list"),
        json=body,
        headers={"X-School-Fact-Executor-Key": KEY},
    )
    assert response.status_code == 200
    row = response.json()["items"][0]
    assert row["report_id"] == str(item.report_id)
    if staff:
        assert row["statement"] == item.statement
    else:
        assert "statement" not in row


def _missing_client(*, detail="Correction report unavailable."):
    settings = SimpleNamespace(
        base_url="http://writer",
        caller_auth_key=SimpleNamespace(get_secret_value=lambda: KEY),
        context_transport_hmac_key=SimpleNamespace(),
        request_timeout_seconds=2.0,
    )
    return FamilyCorrectionExecutorClient(
        settings=settings,
        context_codec=Codec(),
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(404, json={"detail": detail})
        ),
    )


@pytest.mark.parametrize("method", ["options", "own_list", "submit", "staff_list", "resolve"])
def test_client_missing_nonlookup_route_is_dependency_failure(method):
    client = _missing_client()
    args = {"context": object(), "session_binding": "s"}
    if method == "submit":
        args.update(target_reference="opaque", field_name="status", statement="wrong")
    elif method == "staff_list":
        args.update(
            fact_kind="student",
            source_key="student:1",
            field_name="status",
            required_action="maintain",
        )
    elif method == "resolve":
        args.update(report_id=uuid4(), writer_receipt_ref="a" * 64)
    try:
        with pytest.raises(RuntimeError, match="Family correction Writer unavailable") as caught:
            getattr(client, method)(**args)
        assert not isinstance(caught.value, FamilyCorrectionDenied)
    finally:
        client.close()


@pytest.mark.parametrize("method", ["own", "by_key"])
def test_client_recognizes_only_explicit_own_lookup_not_found(method):
    client = _missing_client()
    args = {"context": object(), "session_binding": "s"}
    args.update({"report_id": uuid4()} if method == "own" else {"idempotency_key": "original-key"})
    try:
        if method == "own":
            with pytest.raises(FamilyCorrectionNotFound):
                client.own(**args)
        else:
            assert client.by_key(**args) is None
    finally:
        client.close()


@pytest.mark.parametrize("method", ["own", "by_key"])
def test_client_missing_lookup_route_is_not_an_absent_report(method):
    client = _missing_client(detail="Not Found")
    args = {"context": object(), "session_binding": "s"}
    args.update({"report_id": uuid4()} if method == "own" else {"idempotency_key": "original-key"})
    try:
        with pytest.raises(RuntimeError, match="Family correction Writer unavailable") as caught:
            getattr(client, method)(**args)
        assert not isinstance(caught.value, FamilyCorrectionDenied)
    finally:
        client.close()
