"""Optional quality may be denied without denying independently authorized teaching."""

from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.composition.school_fact_education_quality import EducationQualityBridge
from app.composition.school_fact_education_reader import EducationReadStale
from app.composition.school_fact_f2_query_repository import F2FinalAuthorizationGuard
from app.composition.school_fact_teaching_gateway import MODULE, StaffTeachingGateway
from app.interfaces.http.school_fact_teaching import create_teaching_read_router
from app.platform.assistant.tool_loop import ModelToolCall, ToolCallContext
from app.platform.authorization.secure_reader_contracts import SecureReadDenied
from app.platform.school_facts.application import SchoolFactChangeRejected
from app.platform.school_facts.read_policy import SchoolFactAuthorizationUnavailable
from tests.school_facts.test_f4_education_query import AT
from tests.school_facts.test_f4_education_reader import actor
from tests.school_facts.test_f5_staff_teaching_query import service


def assembled_missing_quality():
    query, repo, external, _ = service()
    # Exercise the real F2 guard and the real quality bridge. No exact quality scope exists.
    guard = F2FinalAuthorizationGuard(
        thin_service=object(), scope_id_resolver=lambda *_: None, clock=lambda: AT
    )
    fetched = []

    class Projection:
        def read(self, **kwargs):
            kwargs["before_read"]()
            fetched.append(True)
            raise AssertionError("A denied quality read must not reach data fetch")

    bridge = EducationQualityBridge(
        f2_guard=guard, education_authority=object(), projection=Projection(), clock=lambda: AT
    )
    external.quality_for = bridge.quality_for
    return query, repo, fetched


def test_real_f2_guard_missing_quality_keeps_authorized_teaching_and_marks_unknown():
    query, _, fetched = assembled_missing_quality()
    result = query.query(actor(), view="my_teaching")
    item = result.public_result["items"][0]
    assert item["teachers"][0]["name"] == "陈老师"
    assert item["quality_status"] == "not_authorized"
    assert "完整性和争议状态尚未核实" not in str(item.get("notices", []))
    assert "学校明确未安排" not in str(item)
    assert not fetched
    result.private_proof.revalidate(actor=actor())


def http_client(query):
    identity = SimpleNamespace(actor=actor(), actor_context_ref="staff")
    app = FastAPI()
    app.include_router(
        create_teaching_read_router(
            identity_dependency=lambda: identity,
            resolve_identity=lambda _: identity,
            family_teaching=None,
            leadership=None,
            staff_teaching=query,
        )
    )
    return TestClient(app)


def test_http_and_model_share_safe_missing_quality_result_and_history_proof():
    query, _, fetched = assembled_missing_quality()
    response = http_client(query).post(
        "/api/v1/school-fact-product/teaching/query", json={"view": "my_teaching"}
    )
    assert response.status_code == 200
    assert response.json()["items"][0]["quality_status"] == "not_authorized"
    identity = SimpleNamespace(actor=actor(), session_binding="session")
    gateway = StaffTeachingGateway(resolve_actor=lambda _: identity, query=query)
    result = gateway.execute(
        ModelToolCall(call_id="one", name=MODULE + ".query", arguments={"view": "my_teaching"}),
        ToolCallContext("staff", "conversation", "turn", "run", 1, uuid4()),
    )
    assert result.model_content["status"] == "available"
    assert "not_authorized" in str(result.model_content)
    assert result.history_check.valid() and not fetched


@pytest.mark.parametrize(
    "error",
    [
        RuntimeError("unexpected failure"),
        PermissionError("quality evidence changed"),
        SchoolFactAuthorizationUnavailable("authority service failed"),
    ],
)
def test_unknown_quality_service_failures_do_not_become_unverified_teaching(error):
    query, _, external, _ = service()

    def failed(*args, **kwargs):
        raise error

    external.quality_for = failed
    with pytest.raises(type(error)):
        query.query(actor(), view="my_teaching")


@pytest.mark.parametrize("view", ["my_teaching", "my_organizations"])
@pytest.mark.parametrize("error", [SchoolFactChangeRejected, SecureReadDenied, EducationReadStale])
def test_known_f2_dependency_denial_maps_to_http_403_and_model_denied(view, error):
    class Denied:
        def query(self, *args, **kwargs):
            raise error("Current exact read authorization missing")

    response = http_client(Denied()).post(
        "/api/v1/school-fact-product/teaching/query", json={"view": view}
    )
    assert response.status_code == 403 and "items" not in response.json()
    gateway = StaffTeachingGateway(
        resolve_actor=lambda _: SimpleNamespace(actor=actor(), session_binding="session"),
        query=Denied(),
    )
    result = gateway.execute(
        ModelToolCall(call_id="one", name=MODULE + ".query", arguments={"view": view}),
        ToolCallContext("staff", "conversation", "turn", "run", 1, uuid4()),
    )
    assert result.model_content["status"] == ("stale" if error is EducationReadStale else "denied")
