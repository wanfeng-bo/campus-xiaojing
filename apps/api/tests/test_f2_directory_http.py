from types import SimpleNamespace
from typing import cast

import pytest
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient

from app.composition.school_fact_assistant_gateway import ProductActor
from app.composition.school_fact_directory_adapter import DirectoryProductAdapter
from app.composition.school_fact_directory_context import bind_directory_identity
from app.core.request_id import assign_request_id
from app.interfaces.http.errors import ApiError
from app.interfaces.http.framework_request import (
    api_error_handler,
    request_validation_error_handler,
)
from app.interfaces.http.school_fact_directory import create_directory_product_router
from tests.test_f2_directory_product import actor, setup

BASE = "/api/v1/school-fact-product/facts"


def client_for(service, query=None):
    app = FastAPI()
    app.middleware("http")(assign_request_id)
    app.add_exception_handler(ApiError, api_error_handler)
    app.add_exception_handler(RequestValidationError, request_validation_error_handler)
    state = {"session": "session-one"}
    calls = []

    def original(**kwargs):
        calls.append(kwargs)
        return {
            "kind": "policy_original",
            "content_text": "正式制度原文。" * 12000,
            "complete": True,
            "length": 84000,
        }

    async def identity_dependency():
        # Match the real F2 app's async dependency and synchronous context manager.
        with bind_directory_identity("synthetic-private-context"):
            yield cast(
                ProductActor, SimpleNamespace(actor=actor(), session_binding=state["session"])
            )

    app.include_router(
        create_directory_product_router(
            identity_dependency=identity_dependency,
            query=cast(DirectoryProductAdapter, query or SimpleNamespace(original=original)),
            product=service,
        )
    )
    return TestClient(app), state, calls


def test_full_diff_confirmation_receipt_and_session_binding():
    service, boundary, _, payload = setup()
    client, state, _ = client_for(service)
    proposal = client.post(BASE + "/prepare", json=payload.model_dump(mode="json"))
    assert proposal.status_code == 200, proposal.text
    ref = proposal.json()["proposal_ref"]
    review = client.get(BASE + "/proposals/" + ref)
    assert (
        review.status_code == 200
        and review.json()["summary"]["before"] != review.json()["summary"]["after"]
    )
    assert not any(name == "confirm" for name, _ in boundary.calls)
    state["session"] = "different-session"
    assert client.post(BASE + "/proposals/" + ref + "/confirm").status_code == 409
    assert not any(name == "confirm" for name, _ in boundary.calls)
    state["session"] = "session-one"
    confirmed = client.post(BASE + "/proposals/" + ref + "/confirm")
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["status"] == "succeeded"
    assert client.get(BASE + "/receipts/" + ref).json()["status"] == "succeeded"
    assert review.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("extra", [{"json": {"confirm": True}}, {"params": {"actor": "other"}}])
def test_confirmation_cannot_change_body_or_identity(extra):
    service, boundary, _, payload = setup()
    client, _, _ = client_for(service)
    proposal = client.post(BASE + "/prepare", json=payload.model_dump(mode="json")).json()
    result = client.post(BASE + "/proposals/" + proposal["proposal_ref"] + "/confirm", **extra)
    assert result.status_code == 422
    assert not any(name == "confirm" for name, _ in boundary.calls)


def test_original_preserves_long_text_and_requested_fact_time():
    service, _, _, _ = setup()
    client, _, calls = client_for(service)
    result = client.get(
        BASE + "/original/f2:policy_version:test", params={"as_of": "2026-10-01T00:00:00Z"}
    )
    assert result.status_code == 200, result.text
    assert result.json()["content_text"] == "正式制度原文。" * 12000
    assert calls[0]["limit"] == 100000 and calls[0]["as_of"].month == 10
    assert result.headers["cache-control"] == "no-store"


def test_original_receipt_url_uses_current_directory_guard_and_original_locator():
    from app.interfaces.http.school_fact_product_contacts import (
        create_school_fact_product_receipt_router,
    )

    service, boundary, _, payload = setup()
    client, state, _ = client_for(service)
    proposal = client.post(BASE + "/prepare", json=payload.model_dump(mode="json")).json()
    ref = proposal["proposal_ref"]
    assert client.post(BASE + "/proposals/" + ref + "/confirm").status_code == 200
    cast(FastAPI, client.app).include_router(
        create_school_fact_product_receipt_router(
            identity_dependency=lambda: cast(
                ProductActor, SimpleNamespace(actor=actor(), session_binding=state["session"])
            ),
            contacts=service,
        )
    )
    result = client.get("/api/v1/school-fact-product/receipts/" + ref)
    assert result.status_code == 200 and result.json()["reference"] == ref
    assert result.json()["status"] == "succeeded"
    assert result.headers["cache-control"] == "no-store"

    def denied(_actor):
        raise PermissionError("revoked")

    service._require_receipt = denied
    before = len(boundary.calls)
    assert client.get("/api/v1/school-fact-product/receipts/" + ref).status_code == 403
    assert len(boundary.calls) == before


def test_reader_denial_is_forbidden_not_source_failure():
    from app.platform.authorization.secure_reader_contracts import SecureReadDenied

    def denied(**_kwargs):
        raise SecureReadDenied("Related target is outside scope")

    service, _, _, _ = setup()
    client, _, _ = client_for(service, query=SimpleNamespace(query=denied))
    result = client.post(
        BASE + "/query", json={"fact_kind": "official_contact", "scope": "all_authorized"}
    )
    assert result.status_code == 403
    assert result.json()["code"] == "school_facts.forbidden"


@pytest.mark.parametrize("route", ["query", "prepare"])
def test_json_body_accepts_timezone_aware_fact_dates(route):
    from tests.test_f2_directory_product import NOW

    service, boundary, _, payload = setup()
    reads = []

    def read(**kwargs):
        reads.append(kwargs["payload"].as_of)
        return {"status": "known", "records": []}

    client, _, _ = client_for(service, query=SimpleNamespace(query=read))
    data = (
        {"fact_kind": "school", "as_of": NOW.isoformat()}
        if route == "query"
        else {**payload.model_dump(mode="json"), "effective_at": NOW.isoformat()}
    )
    response = client.post(BASE + "/" + route, json=data)
    assert response.status_code == 200, response.text
    if route == "query":
        assert reads == [NOW]
    else:
        assert boundary.calls[0][1]["change_set"].changes[0].effective_at == NOW
        assert not any(name == "confirm" for name, _ in boundary.calls)


@pytest.mark.parametrize("date", ["not-a-date", "2026-09-06T00:00:00", 1788652800])
@pytest.mark.parametrize("route", ["query", "prepare"])
def test_json_body_rejects_invalid_naive_and_numeric_dates(route, date):
    service, boundary, _, payload = setup()
    client, _, _ = client_for(service)
    data = (
        {"fact_kind": "school", "as_of": date}
        if route == "query"
        else {**payload.model_dump(mode="json"), "effective_at": date}
    )
    assert client.post(BASE + "/" + route, json=data).status_code == 422
    assert boundary.calls == []


@pytest.mark.parametrize("date", ["not-a-date", "2026-09-06T00:00:00"])
def test_original_query_requires_timezone_aware_fact_date(date):
    service, _, _, _ = setup()
    client, _, calls = client_for(service)
    result = client.get(BASE + "/original/f2:policy_version:test", params={"as_of": date})
    assert result.status_code == 422
    assert calls == []


def test_mistyped_opaque_target_returns_422_without_reaching_preflight():
    from tests.test_f2_directory_product import real_directory_adapter

    service, boundary, _, _ = setup()
    adapter, reference = real_directory_adapter("policy_version", {})
    service._resolve = adapter.resolve_change
    client, _, _ = client_for(service)
    response = client.post(
        BASE + "/prepare",
        json={
            "fact_kind": "policy_version",
            "target_ref": reference + "c",
            "fields": {"version_note": "synthetic diagnostic"},
            "reason": "approved check",
        },
    )
    assert response.status_code == 422
    assert response.json()["code"] == "school_facts.request_invalid"
    assert reference not in response.text
    assert boundary.calls == []
