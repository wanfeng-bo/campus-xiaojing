from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.composition.school_fact_family_correction_product import FamilyCorrectionProduct
from app.core.request_id import assign_request_id
from app.interfaces.http.school_fact_family_corrections import create_family_correction_router
from tests.test_f3_product_identity import guardian


def setup():
    actor = guardian()
    identity = SimpleNamespace(
        actor=actor, session_binding="current-session", actor_context_ref="unit"
    )
    executor = Mock()
    now = datetime.now(UTC).isoformat()
    record = {
        "report_id": str(uuid4()),
        "status": "submitted",
        "field_name": "campus",
        "submitted_at": now,
    }
    executor.submit.return_value.model_dump.return_value = record
    executor.own_list.return_value.model_dump.return_value = {"items": [record]}
    executor.by_key.return_value.model_dump.return_value = record
    current = Mock(return_value=True)
    service = FamilyCorrectionProduct(executor=executor, current_actor=current, staff_source=Mock())
    app = FastAPI()
    app.middleware("http")(assign_request_id)
    app.include_router(
        create_family_correction_router(identity_dependency=lambda: identity, product=service)
    )
    return app, executor, current, actor


def test_current_identity_controls_submission_and_original_key_lookup():
    app, executor, current, actor = setup()
    key = str(uuid4())
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/school-fact-product/family/corrections",
            json={
                "reference": "current-signed-reference",
                "field_name": "campus",
                "statement": "校区资料有误",
                "idempotency_key": key,
            },
        )
        assert response.status_code == 200
        assert response.json()["kind"] == "family_fact_correction_receipt"
        assert response.headers["cache-control"] == "no-store"
        submitted = executor.submit.call_args.kwargs
        assert submitted["context"].actor == actor
        assert submitted["session_binding"] == "current-session"
        assert submitted["context"].idempotency_key.endswith(key)
        response = client.get("/api/v1/school-fact-product/family/corrections/by-key/" + key)
        assert response.status_code == 200
        assert (
            executor.by_key.call_args.kwargs["idempotency_key"]
            == submitted["context"].idempotency_key
        )
        current.return_value = False
        assert client.get("/api/v1/school-fact-product/family/corrections").status_code == 403
        executor.own_list.assert_not_called()


def test_does_not_accept_identity_fields_or_dispatch_without_explicit_payload():
    app, executor, _, _ = setup()
    with TestClient(app) as client:
        for extra in [
            {"school_id": str(uuid4())},
            {"student_id": str(uuid4())},
            {"confirmed": True},
        ]:
            response = client.post(
                "/api/v1/school-fact-product/family/corrections",
                json={
                    "reference": "current-signed-reference",
                    "field_name": "campus",
                    "statement": "核对这项资料",
                    "idempotency_key": str(uuid4()),
                    **extra,
                },
            )
            assert response.status_code == 422
        assert (
            client.post("/api/v1/school-fact-product/family/corrections", json={}).status_code
            == 422
        )
    executor.submit.assert_not_called()


def test_lost_result_is_looked_up_not_resubmitted_and_metadata_is_projected():
    app, executor, _, _ = setup()
    executor.by_key.return_value = None
    with TestClient(app) as client:
        response = client.get(
            "/api/v1/school-fact-product/family/corrections/by-key/" + str(uuid4())
        )
        assert response.status_code == 404
        executor.submit.assert_not_called()
        executor.own_list.return_value.model_dump.return_value["items"][0]["private_statement"] = (
            "private"
        )
        response = client.get("/api/v1/school-fact-product/family/corrections")
        assert response.status_code == 200
        assert "private_statement" not in response.text


def test_options_use_current_identity_and_never_accept_arbitrary_private_fields():
    app, executor, current, actor = setup()
    executor.options.return_value.model_dump.return_value = {
        "items": [
            {
                "field_name": "relationship",
                "label": "孩子关联",
                "reference": "signed-relation-source",
            }
        ]
    }
    with TestClient(app) as client:
        response = client.get("/api/v1/school-fact-product/family/corrections/options")
        assert response.status_code == 200
        assert response.json()["items"][0]["field_name"] == "relationship"
        assert executor.options.call_args.kwargs["context"].actor == actor
        executor.options.return_value.model_dump.return_value["items"][0]["field_name"] = (
            "full_address"
        )
        assert (
            client.get("/api/v1/school-fact-product/family/corrections/options").status_code == 503
        )
        current.return_value = False
        assert (
            client.get("/api/v1/school-fact-product/family/corrections/options").status_code == 403
        )


def test_post_write_identity_expiry_is_unknown_outcome_not_a_rejected_submission():
    app, executor, current, _ = setup()
    current.side_effect = [True, False]
    key = str(uuid4())
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/school-fact-product/family/corrections",
            json={
                "reference": "signed-current-target",
                "field_name": "campus",
                "statement": "请核对校区",
                "idempotency_key": key,
            },
        )
        assert response.status_code == 503
        assert "原回执" in response.json()["detail"]
        executor.submit.assert_called_once()
        assert "report_id" not in response.text
