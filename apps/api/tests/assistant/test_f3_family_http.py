from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import Mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.domains.students.family_read import FamilyQueryResult
from app.interfaces.http.school_fact_family import create_family_profile_router
from tests.test_f3_product_identity import guardian


def test_profile_http_uses_selected_identity_and_refuses_unrecognized_targets():
    actor = guardian()
    query = Mock()
    now = datetime.now(UTC)
    query.query.return_value = FamilyQueryResult(
        "available", {"name": "合成孩子"}, (), (), now, now
    )
    app = FastAPI()
    app.include_router(
        create_family_profile_router(
            identity_dependency=lambda: SimpleNamespace(actor=actor, actor_context_ref="unit"),
            query=query,
        )
    )
    with TestClient(app) as client:
        for key in ["student_id", "fields", "school_id"]:
            response = client.post(
                "/api/v1/school-fact-product/family/profile", json={key: "untrusted"}
            )
            assert response.status_code == 422
        query.query.assert_not_called()
        response = client.post("/api/v1/school-fact-product/family/profile", json={})
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert response.json()["profile"] == {"name": "合成孩子"}
        query.query.assert_called_once_with(actor=actor, as_of=None)
