"""Selected-child HTTP education is independent of models and Staff scopes."""

from types import SimpleNamespace as N
from unittest.mock import Mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.composition.school_fact_family_education_query import FamilyEducationResult
from app.domains.students.family_read import FamilyReadEvidence
from app.interfaces.http.school_fact_family import create_family_profile_router
from tests.test_f1_official_contact_query import _actor
from tests.test_f3_product_identity import guardian


def application():
    state = N(actor=guardian())

    def identity():
        return N(actor=state.actor, actor_context_ref="fixed-context")

    service = Mock()

    def query(**kwargs):
        captured = kwargs["actor"]
        return FamilyEducationResult(
            {
                "status": "available",
                "profile": {"teachers": [{"display_name": "Scoped teacher"}]},
                "authorized_fields": ["teachers"],
            },
            FamilyReadEvidence(lambda current: current == captured),
        )

    service.query.side_effect = query
    app = FastAPI()
    app.include_router(
        create_family_profile_router(
            identity_dependency=identity,
            query=Mock(),
            education=service,
            resolve_identity=lambda ref: identity(),
        )
    )
    return app, state, service


def test_education_http_rejects_identity_override_private_fields_and_staff():
    app, state, service = application()
    with TestClient(app) as client:
        for payload in (
            {"fields": ["teachers"], "student_id": "other"},
            {"fields": ["phone"]},
            {"fields": ["teachers"], "school_id": "other"},
        ):
            assert (
                client.post(
                    "/api/v1/school-fact-product/family/education", json=payload
                ).status_code
                == 422
            )
        service.query.assert_not_called()
        state.actor = _actor()
        response = client.post(
            "/api/v1/school-fact-product/family/education", json={"fields": ["teachers"]}
        )
        assert response.status_code == 403
        service.query.assert_not_called()


def test_education_http_uses_current_child_and_rechecks_before_response():
    app, state, service = application()
    with TestClient(app) as client:
        first = state.actor
        response = client.post(
            "/api/v1/school-fact-product/family/education", json={"fields": ["teachers"]}
        )
        assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
        service.query.assert_called_with(actor=first, fields=("teachers",), as_of=None)
        state.actor = guardian(2)
        response = client.post(
            "/api/v1/school-fact-product/family/education", json={"fields": ["teachers"]}
        )
        assert response.status_code == 200
        service.query.assert_called_with(actor=state.actor, fields=("teachers",), as_of=None)

        def switch(**kwargs):
            original = kwargs["actor"]
            state.actor = guardian(3)
            return FamilyEducationResult(
                {"status": "available", "profile": {"teachers": ["must not escape"]}},
                FamilyReadEvidence(lambda current: current == original),
            )

        service.query.side_effect = switch
        response = client.post(
            "/api/v1/school-fact-product/family/education", json={"fields": ["teachers"]}
        )
        assert response.status_code == 403
        assert "must not escape" not in response.text
