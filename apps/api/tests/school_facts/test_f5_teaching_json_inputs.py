"""HTTP/model JSON dates keep timezone validation and strict non-date fields."""

from datetime import datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.composition.school_fact_family_teaching_gateway import NAME, FamilyTeachingToolGateway
from app.interfaces.http.school_fact_teaching import create_teaching_read_router
from app.platform.assistant.tool_loop import ModelToolCall, ToolCallContext
from tests.school_facts.test_f4_education_reader import actor
from tests.test_f3_product_identity import guardian


def environment():
    seen = []

    class Query:
        def query(self, *args, **kwargs):
            seen.append(kwargs)
            return SimpleNamespace(
                payload={"status": "available"},
                public=lambda: {"status": "available", "items": []},
                public_result={"status": "available", "items": []},
                evidence=SimpleNamespace(valid=lambda *_: True),
                private_proof=SimpleNamespace(revalidate=lambda **_: None),
            )

    identity = SimpleNamespace(actor=guardian(), actor_context_ref="selected")
    app = FastAPI()
    query = Query()
    app.include_router(
        create_teaching_read_router(
            identity_dependency=lambda: identity,
            resolve_identity=lambda _: identity,
            family_teaching=query,
            leadership=query,
            staff_teaching=query,
        )
    )
    return TestClient(app), query, identity, seen


@pytest.mark.parametrize("path,is_staff", [("family/teaching", False), ("teaching/query", True)])
def test_http_accepts_only_timezone_aware_iso_date(path, is_staff):
    client, query, identity, seen = environment()
    if is_staff:
        identity.actor = actor()
        # Staff returns an iterable evidence list rather than a parent evidence object.
        original = query.query

        def staff(*args, **kwargs):
            value = original(*args, **kwargs)
            value.evidence = ()
            return value

        query.query = staff
    selection = {"view": "my_teaching"} if is_staff else {}
    response = client.post(
        "/api/v1/school-fact-product/" + path,
        json=selection | {"as_of": "2026-10-01T08:00:00+08:00"},
    )
    assert response.status_code == 200
    assert isinstance(seen[-1]["as_of"], datetime) and seen[-1]["as_of"].utcoffset() is not None
    calls = len(seen)
    response = client.post(
        "/api/v1/school-fact-product/" + path, json=selection | {"as_of": "2026-10-01T08:00:00"}
    )
    assert response.status_code == 422 and len(seen) == calls


@pytest.mark.parametrize(
    "path,key,is_staff",
    [
        ("family/teaching", "include_official_contacts", False),
        ("family/leadership", "include_official_contacts", False),
        ("teaching/query", "include_contacts", True),
    ],
)
@pytest.mark.parametrize("bad", ["true", 1])
def test_http_json_adapter_preserves_strict_boolean(path, key, is_staff, bad):
    client, _, identity, seen = environment()
    if is_staff:
        identity.actor = actor()
    payload = ({"view": "my_teaching"} if is_staff else {}) | {key: bad}
    assert client.post("/api/v1/school-fact-product/" + path, json=payload).status_code == 422
    assert not seen


def test_family_model_tool_parses_iso_date_and_rejects_naive_date_and_string_bool():
    _, query, identity, seen = environment()
    turn = FamilyTeachingToolGateway(resolve_actor=lambda _: identity, query=query).for_turn(
        "selected"
    )
    context = ToolCallContext("selected", "conversation", "turn", "run", 1, uuid4())

    def call(arguments):
        return ModelToolCall(call_id="one", name=NAME, arguments=arguments)

    result = turn.execute(call({"as_of": "2026-10-01T00:00:00Z"}), context)
    assert result.history_check.valid() and isinstance(seen[-1]["as_of"], datetime)
    for arguments in ({"as_of": "2026-10-01T00:00:00"}, {"include_official_contacts": "true"}):
        with pytest.raises(ValidationError):
            turn.execute(call(arguments), context)
