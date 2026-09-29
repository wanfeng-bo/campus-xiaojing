from types import SimpleNamespace
from datetime import UTC, datetime, timedelta

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.interfaces.http.school_fact_education import create_education_product_router
from app.interfaces.http.framework_request import FrameworkRequestContext, get_framework_request_context
from app.interfaces.http.errors import ApiError, render_api_error
from app.composition.school_fact_directory_product import FactChangeSummary, FactProposal
from tests.test_f2_directory_product import actor
from uuid import uuid4


def test_real_http_json_array_reaches_fixed_lookup_and_no_store_boundary():
    identity = SimpleNamespace(actor=actor(), session_binding="synthetic-session")
    calls = []

    class Query:
        def query(self, current, **selection):
            calls.append((current, selection))
            return SimpleNamespace(public_result={"status": "known", "name": "合成班"},
                private_proof=SimpleNamespace(revalidate=lambda **kwargs: None))

    app = FastAPI()
    app.include_router(create_education_product_router(identity_dependency=lambda: identity,
        query=SimpleNamespace(), product=SimpleNamespace(), education_query=Query()))
    with TestClient(app) as client:
        response = client.post("/api/v1/school-fact-product/education-facts/lookup",
                               json={"name": "合成班", "fields": ["teachers"]})
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert calls[0][1]["fields"] == ("teachers",)


def test_lookup_never_accepts_model_identity_or_school_parameters():
    app = FastAPI()
    app.include_router(create_education_product_router(
        identity_dependency=lambda: SimpleNamespace(actor=actor()),
        query=SimpleNamespace(), product=SimpleNamespace(), education_query=SimpleNamespace()))
    with TestClient(app) as client:
        result = client.post("/api/v1/school-fact-product/education-facts/lookup",
            json={"name": "合成班", "fields": ["teachers"], "school_id": "other"})
    assert result.status_code == 422


def test_dependency_query_preserves_explicit_year_campus_and_position():
    calls = []
    class Views:
        def query(self, **kwargs):
            calls.append(kwargs)
            return {"status": "known", "records": []}
    app = FastAPI()
    app.include_router(create_education_product_router(
        identity_dependency=lambda: SimpleNamespace(actor=actor()),
        query=Views(), product=SimpleNamespace(), education_query=SimpleNamespace()))
    with TestClient(app) as client:
        result = client.post("/api/v1/school-fact-product/education-facts/query", json={
            "fact_kind": "fact_quality", "academic_year": 2026,
            "campus_ref": "authorized-campus", "position_ref": "authorized-position"})
    assert result.status_code == 200
    assert calls[0]["academic_year"] == 2026
    assert calls[0]["campus_ref"] == "authorized-campus"
    assert calls[0]["position_ref"] == "authorized-position"


def test_f7_off_education_quality_uses_existing_prepare_and_describe_boundary():
    """F7 package ingress OFF must not block the owner's narrow quality maintenance."""
    calls = []
    now = datetime.now(UTC)

    class Views:
        def describe(self, **kwargs):
            calls.append(("describe", kwargs["fact_kind"]))
            return {"fact_kind": "fact_quality", "creation_choices": [
                {"creation_ref": "f4create:fact_quality:opaque", "label": "合成质量"}
            ]}

    class Product:
        def prepare(self, **kwargs):
            calls.append(("prepare", kwargs["payload"].fact_kind))
            return FactProposal(
                proposal_ref="f7-quality-proposal",
                summary=FactChangeSummary(
                    fact_kind="fact_quality", label="合成质量",
                    reference="f4create:fact_quality:opaque", operation="upsert",
                    before=None, after={"quality_kind": "source"},
                    effective_at=now, reason="正式来源核对", source_label="正式维护",
                ),
                expires_at=now + timedelta(minutes=5),
            )

    app = FastAPI()
    context = FrameworkRequestContext(uuid4())
    app.dependency_overrides[get_framework_request_context] = lambda: context
    app.add_exception_handler(ApiError, lambda _request, error: render_api_error(context.request_id, error))
    app.include_router(create_education_product_router(
        identity_dependency=lambda: SimpleNamespace(actor=actor(), session_binding="session"),
        query=Views(), product=Product(), education_query=SimpleNamespace(),
    ))
    with TestClient(app) as client:
        described = client.get("/api/v1/school-fact-product/education-facts/describe/fact_quality")
        prepared = client.post("/api/v1/school-fact-product/education-facts/prepare", json={
            "fact_kind": "fact_quality", "creation_ref": "f4create:fact_quality:opaque",
            "fields": {"quality_kind": "source"}, "reason": "正式来源核对", "purpose": "record",
        })
    assert described.status_code == 200
    assert prepared.status_code == 200
    assert prepared.json()["proposal_ref"] == "f7-quality-proposal"
    assert calls == [("describe", "fact_quality"), ("prepare", "fact_quality")]


def test_f7_off_education_quality_route_does_not_open_policy_scope():
    calls = []

    class Product:
        def prepare(self, **kwargs):
            calls.append(kwargs)
            raise AssertionError("unsupported owner reached product")

    app = FastAPI()
    context = FrameworkRequestContext(uuid4())
    app.dependency_overrides[get_framework_request_context] = lambda: context
    app.add_exception_handler(ApiError, lambda _request, error: render_api_error(context.request_id, error))
    app.include_router(create_education_product_router(
        identity_dependency=lambda: SimpleNamespace(actor=actor(), session_binding="session"),
        query=SimpleNamespace(), product=Product(), education_query=SimpleNamespace(),
    ))
    with TestClient(app) as client:
        result = client.post("/api/v1/school-fact-product/education-facts/prepare", json={
            "fact_kind": "policy_scope", "creation_ref": "f4create:policy_scope:opaque",
            "fields": {}, "reason": "未经本入口授权", "purpose": "record",
        })
    assert result.status_code != 200
    assert calls == []
