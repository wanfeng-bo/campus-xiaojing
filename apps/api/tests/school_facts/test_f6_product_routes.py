"""Installed HTTP and common receipt continuation preserve F6 identity boundaries."""

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.composition.school_fact_directory_context import current_bound_actor_matches
from app.interfaces.http.school_fact_residence import create_family_residence_router
from tests.school_facts.test_f6_residence_read import fixture, row


def client_fixture():
    query, actor, state = fixture()
    identity = SimpleNamespace(actor=actor, actor_context_ref="selected-child")

    def resolve(_):
        return identity

    query.current_actor = lambda selected: (
        state.active and current_bound_actor_matches(selected, resolve)
    )
    app = FastAPI()
    app.include_router(
        create_family_residence_router(
            identity_dependency=lambda: identity,
            query=query,
            resolve_identity=resolve,
        )
    )
    return TestClient(app), state


def test_selected_child_http_binds_identity_and_preserves_no_store():
    client, state = client_fixture()
    state.payload.update(status="available", records=[row()], residence_mode="boarding")
    with client:
        result = client.get("/api/v1/school-fact-product/family/residence")
    assert result.status_code == 200
    assert result.json()["records"][0]["room_name"] == "301室"
    assert "no-store" in result.headers["cache-control"]


@pytest.mark.parametrize("failure,status", [("revoked", 403), ("source", 503)])
def test_family_http_does_not_release_denied_or_unavailable_as_success(failure, status):
    client, state = client_fixture()
    if failure == "revoked":
        state.active = False
    else:
        state.payload["student_id"] = "wrong-child"
    with client:
        result = client.get("/api/v1/school-fact-product/family/residence")
    assert result.status_code == status
    assert "records" not in result.json()


@pytest.mark.parametrize("school_wide", [False, True])
def test_shared_receipt_current_dispatch_reads_exact_f6_target_and_preserves_retained_owner(school_wide):
    from app.composition.school_fact_product import read_receipt_current_residence
    from app.platform.assistant.tool_loop import ToolExecutionResult

    calls = []
    retained = []
    result = ToolExecutionResult(model_content={"status": "available"})
    gateway = SimpleNamespace(execute=lambda call, context: calls.append(call) or result)

    def fallback(target, context):
        retained.append(target)
        return result

    target = SimpleNamespace(
        fact_kind="student_bed_residence", reference="f6:student_bed_residence:" + "a" * 64
    )
    if school_wide:
        target.reference = "schoolv1:f6:student_bed_residence:o:YQ:" + "a" * 64
    assert read_receipt_current_residence(gateway, fallback, target, None) is result
    assert calls[0].arguments == {"fact_kind": target.fact_kind, "reference": target.reference}
    assert not retained
    old = SimpleNamespace(
        fact_kind="teaching_assignment", reference="f4:teaching_assignment:" + "b" * 64
    )
    assert read_receipt_current_residence(gateway, fallback, old, None) is result
    assert retained == [old]
    bad = SimpleNamespace(
        fact_kind="student_bed_residence", reference="f4:teaching_assignment:" + "b" * 64
    )
    assert (
        read_receipt_current_residence(gateway, fallback, bad, None).model_content["status"]
        == "unsupported"
    )
    assert len(calls) == 1


@pytest.mark.parametrize(
    "kind,prefix", [("fact_quality", "f2"), ("student_school_relationship", "f3")]
)
def test_receipt_current_uses_f6_dependency_owner_for_quality_and_eligibility(kind, prefix):
    from app.composition.school_fact_product import read_receipt_current_residence
    from app.platform.assistant.tool_loop import ToolExecutionResult

    calls = []
    result = ToolExecutionResult(model_content={"status": "available"})
    gateway = SimpleNamespace(execute=lambda call, context: calls.append(call) or result)
    target = SimpleNamespace(fact_kind=kind, reference=f"{prefix}:{kind}:" + "a" * 64)
    assert read_receipt_current_residence(gateway, None, target, None) is result
    assert calls[0].name == "school_facts.residence.query"
    assert calls[0].arguments == {"fact_kind": kind, "reference": target.reference}
