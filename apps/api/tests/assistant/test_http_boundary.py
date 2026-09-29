from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterator
from contextlib import contextmanager
from typing import cast
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

import app.interfaces.http.assistant as assistant_http
from app.core.request_id import assign_request_id
from app.interfaces.http.assistant import (
    AssistantRequestIdentity,
    create_assistant_router,
)
from app.interfaces.http.errors import ApiError, HttpErrorResponse
from app.interfaces.http.framework_request import (
    api_error_handler,
    request_validation_error_handler,
)
from app.main import app as production_app
from app.platform.assistant.orchestrator import AssistantTurnOrchestrator
from app.platform.assistant.runtime import AcceptedRun, AssistantRuntime, ConversationSnapshot


class _AcceptOnlyOrchestrator:
    """只供 K3-A HTTP 机械边界测试保留 active run；完整纵切另有专门探针。"""

    def run_accepted(
        self,
        *,
        runtime: AssistantRuntime,
        accepted: AcceptedRun,
        actor_context_ref: str,
        request_id: UUID,
    ) -> ConversationSnapshot:
        del request_id
        return runtime.snapshot(accepted.conversation_ref, actor_context_ref)


class _Refs:
    def __init__(self) -> None:
        self._counts: dict[str, int] = defaultdict(int)

    def __call__(self, kind: str) -> str:
        self._counts[kind] += 1
        return f"{kind}-{self._counts[kind]}"


@contextmanager
def _client(
    *,
    actor_context: dict[str, str] | None = None,
    on_conversation_created=None,
    on_user_input=None,
) -> Iterator[tuple[TestClient, AssistantRuntime, dict[str, str]]]:
    current_actor = actor_context or {"value": "actor-context-1"}
    runtime = AssistantRuntime(ref_factory=_Refs())

    def resolve_identity() -> AssistantRequestIdentity:
        return AssistantRequestIdentity(actor_context_ref=current_actor["value"])

    app = FastAPI(responses={422: {"model": HttpErrorResponse}})
    app.middleware("http")(assign_request_id)
    app.add_exception_handler(ApiError, api_error_handler)
    app.add_exception_handler(RequestValidationError, request_validation_error_handler)
    app.include_router(
        create_assistant_router(
            runtime=runtime,
            identity_dependency=resolve_identity,
            turn_orchestrator=cast(AssistantTurnOrchestrator, _AcceptOnlyOrchestrator()),
            on_conversation_created=on_conversation_created,
            on_user_input=on_user_input,
        )
    )
    with TestClient(app) as client:
        yield client, runtime, current_actor


def _create_conversation(client: TestClient) -> str:
    response = client.post("/api/v1/assistant/conversations")
    assert response.status_code == 201
    return cast(str, response.json()["conversation_ref"])


def test_input_policy_rejects_before_conversation_retention_or_model_work():
    from app.platform.assistant.runtime import AssistantServiceError

    def require_input(_text):
        raise AssistantServiceError("synthetic policy rejection", reason="data_policy")

    with _client(on_user_input=require_input) as (client, runtime, current_actor):
        reference = _create_conversation(client)
        response = client.post(
            f"/api/v1/assistant/conversations/{reference}/turns",
            json={"teacher_utterance": "synthetic blocked value"},
        )
        assert response.status_code == 422
        assert "assistant.data_policy" in response.text
        assert "synthetic blocked value" not in response.text
        snapshot = runtime.snapshot(reference, current_actor["value"])
        assert not snapshot.model_safe_ledger
        assert snapshot.active_run is None


def test_opaque_context_claim_reaches_only_new_conversation_hook() -> None:
    bound: list[tuple[str, str, str]] = []
    with _client(
        on_conversation_created=lambda actor, conversation, claim: bound.append(
            (actor, conversation, claim)
        )
    ) as (client, _, _):
        plain = _create_conversation(client)
        assert bound == []
        response = client.post(
            "/api/v1/assistant/conversations",
            headers={"X-Conversation-Context-Claim": "C" * 43},
        )
        assert response.status_code == 201
        claimed = response.json()["conversation_ref"]
        assert claimed != plain
        assert bound == [("actor-context-1", claimed, "C" * 43)]
        client.get(
            f"/api/v1/assistant/conversations/{claimed}",
            headers={"X-Conversation-Context-Claim": "C" * 43},
        )
        assert bound == [("actor-context-1", claimed, "C" * 43)]


def test_business_free_http_path_uses_real_runtime_for_create_accept_snapshot_and_stop() -> None:
    teacher_utterance = "  老师原话自带号码 13800000000  "
    with _client() as (client, runtime, _actor):
        conversation_ref = _create_conversation(client)

        accepted = client.post(
            f"/api/v1/assistant/conversations/{conversation_ref}/turns",
            json={"teacher_utterance": teacher_utterance},
        )
        assert accepted.status_code == 200
        accepted_body = accepted.json()
        assert accepted_body == {
            "version": "1",
            "conversation_ref": conversation_ref,
            "generation": 1,
            "active_run": {
                "turn_ref": "turn-1",
                "run_ref": "run-1",
                "generation": 1,
            },
            "turns": [
                {
                    "turn_ref": "turn-1",
                    "run_ref": "run-1",
                    "generation": 1,
                    "teacher_utterance": teacher_utterance,
                    "visible_state": "active",
                    "display_kind": None,
                    "display_payload": None,
                    "controls": [],
                }
            ],
        }

        active = client.get(f"/api/v1/assistant/conversations/{conversation_ref}")
        assert active.status_code == 200
        assert active.json() == {
            "version": "1",
            "conversation_ref": conversation_ref,
            "generation": 1,
            "active_run": {
                "turn_ref": "turn-1",
                "run_ref": "run-1",
                "generation": 1,
            },
            "turns": [
                {
                    "turn_ref": "turn-1",
                    "run_ref": "run-1",
                    "generation": 1,
                    "teacher_utterance": teacher_utterance,
                    "visible_state": "active",
                    "display_kind": None,
                    "display_payload": None,
                    "controls": [],
                }
            ],
        }

        stopped = client.post(
            f"/api/v1/assistant/conversations/{conversation_ref}/runs/run-1/stop",
            json={"generation": 1},
        )
        assert stopped.status_code == 200
        assert stopped.json()["generation"] == 2
        assert stopped.json()["active_run"] is None
        assert stopped.json()["turns"][0]["visible_state"] == "stopped"

        internal = runtime.snapshot(conversation_ref, "actor-context-1")
        assert internal.model_safe_ledger[0].teacher_utterance == teacher_utterance
        assert internal.model_safe_ledger[0].technical_marker == "stopped"


def test_busy_is_a_stable_error_and_rejected_text_never_enters_a_ledger() -> None:
    rejected_canary = "REJECTED-SENSITIVE-13911112222"
    with _client() as (client, runtime, _actor):
        conversation_ref = _create_conversation(client)
        runtime.accept_turn(
            conversation_ref,
            "actor-context-1",
            UUID("00000000-0000-0000-0000-000000000001"),
            "第一句",
        )

        busy = client.post(
            f"/api/v1/assistant/conversations/{conversation_ref}/turns",
            json={"teacher_utterance": rejected_canary},
        )

        assert busy.status_code == 409
        assert busy.json()["code"] == "assistant.conversation_busy"
        assert rejected_canary not in busy.text
        snapshot = runtime.snapshot(conversation_ref, "actor-context-1")
        assert len(snapshot.display_ledger) == 1
        assert rejected_canary not in repr(snapshot)


def test_identity_is_server_injected_and_client_authority_fields_are_rejected() -> None:
    authority_canary = "FORGED-ACTOR-CONTEXT-SECRET"
    with _client() as (client, _runtime, actor):
        conversation_ref = _create_conversation(client)
        forged = client.post(
            f"/api/v1/assistant/conversations/{conversation_ref}/turns",
            headers={"X-Actor-Context-Ref": authority_canary},
            json={
                "teacher_utterance": "第一句",
                "actor_context_ref": authority_canary,
                "school_id": 7,
            },
        )

        assert forged.status_code == 422
        assert forged.json()["code"] == "http.request_invalid"
        assert authority_canary not in forged.text

        accepted = client.post(
            f"/api/v1/assistant/conversations/{conversation_ref}/turns",
            headers={"X-Actor-Context-Ref": authority_canary},
            json={"teacher_utterance": "合法正文"},
        )
        assert accepted.status_code == 200

        actor["value"] = "actor-context-2"
        unavailable = client.get(f"/api/v1/assistant/conversations/{conversation_ref}")
        assert unavailable.status_code == 404
        assert unavailable.json()["code"] == "assistant.conversation_unavailable"


def test_invalid_body_and_stale_stop_fail_closed_without_echoing_input() -> None:
    invalid_canary = "INVALID-GENERATION-SECRET"
    with _client() as (client, _runtime, _actor):
        invalid_path = client.get("/api/v1/assistant/conversations/INVALID!REFERENCE!SECRET")
        assert invalid_path.status_code == 422
        assert "INVALID!REFERENCE!SECRET" not in invalid_path.text

        conversation_ref = _create_conversation(client)
        runtime = _runtime
        runtime.accept_turn(
            conversation_ref,
            "actor-context-1",
            UUID("00000000-0000-0000-0000-000000000002"),
            "第一句",
        )

        invalid = client.post(
            f"/api/v1/assistant/conversations/{conversation_ref}/runs/run-1/stop",
            json={"generation": invalid_canary},
        )
        assert invalid.status_code == 422
        assert invalid_canary not in invalid.text

        stale = client.post(
            f"/api/v1/assistant/conversations/{conversation_ref}/runs/run-1/stop",
            json={"generation": 9},
        )
        assert stale.status_code == 409
        assert stale.json()["code"] == "assistant.run_stale"


def test_no_body_routes_reject_hidden_body_and_query_values() -> None:
    hidden_canary = "HIDDEN-REQUEST-SECRET"
    with _client() as (client, _runtime, _actor):
        create = client.request(
            "POST",
            "/api/v1/assistant/conversations?school_id=7",
            content=hidden_canary,
            headers={"Content-Type": "text/plain"},
        )
        assert create.status_code == 422
        assert hidden_canary not in create.text

        conversation_ref = _create_conversation(client)
        snapshot = client.request(
            "GET",
            f"/api/v1/assistant/conversations/{conversation_ref}?actor_context_ref=forged",
            content=hidden_canary,
        )
        assert snapshot.status_code == 422
        assert hidden_canary not in snapshot.text


def test_formal_router_has_strict_versioned_contract_and_no_probe_route() -> None:
    with _client() as (client, _runtime, _actor):
        document = client.get("/openapi.json").json()

    paths = cast(dict[str, object], document["paths"])
    assert set(paths) == {
        "/api/v1/assistant/conversations",
        "/api/v1/assistant/conversations/{conversation_ref}",
        "/api/v1/assistant/conversations/{conversation_ref}/close",
        "/api/v1/assistant/conversations/{conversation_ref}/turns",
        "/api/v1/assistant/conversations/{conversation_ref}/continuations/{handle}",
        "/api/v1/assistant/conversations/{conversation_ref}/runs/{run_ref}/stop",
        "/api/v1/assistant/conversations/{conversation_ref}/controls/action-offers/{control_handle}",
        "/api/v1/assistant/conversations/{conversation_ref}/controls/recovery-controls/{control_handle}",
        "/api/v1/assistant/conversations/{conversation_ref}/controls/result-checks/{control_handle}",
    }
    assert all("probe" not in path for path in paths)
    for path in (
        "/api/v1/assistant/conversations/{conversation_ref}/controls/"
        "action-offers/{control_handle}",
        "/api/v1/assistant/conversations/{conversation_ref}/controls/"
        "recovery-controls/{control_handle}",
        "/api/v1/assistant/conversations/{conversation_ref}/controls/"
        "result-checks/{control_handle}",
    ):
        post = cast(dict[str, object], cast(dict[str, object], paths[path])["post"])
        assert "requestBody" not in post
        parameters = cast(list[dict[str, object]], post["parameters"])
        assert {parameter["name"] for parameter in parameters} == {
            "conversation_ref",
            "control_handle",
        }
        assert all(parameter["in"] == "path" for parameter in parameters)
    schemas = cast(dict[str, dict[str, object]], document["components"]["schemas"])
    assert schemas["AssistantTurnRequest"]["additionalProperties"] is False
    assert schemas["AssistantStopRequest"]["additionalProperties"] is False
    assert set(cast(dict[str, object], schemas["AssistantTurnRequest"]["properties"])) == {
        "teacher_utterance"
    }
    assert set(cast(dict[str, object], schemas["AssistantStopRequest"]["properties"])) == {
        "generation"
    }


def test_k3_router_is_not_registered_in_the_production_application() -> None:
    paths = {route.path for route in production_app.routes if isinstance(route, APIRoute)}
    openapi_paths = set(cast(dict[str, object], production_app.openapi()["paths"]))

    assert not hasattr(assistant_http, "router")
    assert not any(path.startswith("/api/v1/assistant") for path in paths)
    assert not any(path.startswith("/api/v1/assistant") for path in openapi_paths)


def test_known_service_failure_is_settled_with_specific_safe_message():
    from app.interfaces.http.assistant import _service_failure_payload
    from app.platform.assistant.runtime import AssistantServiceError

    payload = _service_failure_payload(
        AssistantServiceError("private internal", reason="tool_limit")
    )
    assert payload["error_code"] == "assistant.tool_limit"
    assert "private internal" not in str(payload)
    assert "运行次数上限" in payload["text"]


@pytest.mark.parametrize(
    "reason", ["tool_limit", "deadline", "budget", "dependency", "context_limit", "output_limit"]
)
def test_known_failure_settles_turn_without_false_success_or_unknown_delivery(monkeypatch, reason):
    from app.platform.assistant.runtime import AssistantServiceError

    def fail(self, **kwargs):
        raise AssistantServiceError("private-body", reason=reason)

    monkeypatch.setattr(_AcceptOnlyOrchestrator, "run_accepted", fail)
    with _client() as (client, runtime, actor):
        ref = _create_conversation(client)
        response = client.post(
            f"/api/v1/assistant/conversations/{ref}/turns", json={"teacher_utterance": "合成输入"}
        )
        assert response.status_code == 200
        value = response.json()
        assert value["active_run"] is None
        assert value["turns"][-1]["display_payload"]["error_code"] == "assistant." + reason
        assert "private-body" not in response.text
        if reason == "output_limit":
            text = value["turns"][-1]["display_payload"]["text"]
            assert "未形成完整回答" in text
            assert "服务" not in text and "网络" not in text
        assert (
            client.get(f"/api/v1/assistant/conversations/{ref}").json()["turns"] == value["turns"]
        )


def test_output_limit_is_a_known_terminal_stream_event():
    from app.interfaces.http.assistant_schemas import AssistantTurnErrorEvent

    event = AssistantTurnErrorEvent(code="assistant.output_limit")
    assert event.code == "assistant.output_limit"


def test_request_timing_contains_only_fixed_metadata_and_cleans_context(caplog):
    import json
    import logging

    from app.platform.assistant.diagnostics import (
        measure,
        record_model_input,
        record_usage,
        request_trace,
    )

    with caplog.at_level(logging.INFO, logger="uvicorn.error"):
        with request_trace("synthetic-id"):
            with measure("model"):
                with measure("provider"):
                    record_usage(prompt=17, completion=3, reasoning=2, cache_hit=10, cache_miss=7)
                    record_model_input(wire_bytes=70000)
                    for _ in range(40):
                        record_model_input(wire_bytes=1000, tool_count=3, message_count=2)
        with measure("scope"):
            pass
    payload = json.loads([r for r in caplog.records if r.msg == "assistant_timing %s"][-1].args[0])
    assert payload["counts"] == {"model": 1, "provider": 1}
    assert payload["usage"][0]["completion_tokens"] == 3
    assert set(payload) == {
        "request_id",
        "started_at",
        "ended_at",
        "seconds",
        "outcome",
        "counts",
        "usage",
        "model_inputs",
        "cross_context_spans",
        "exclusive_scope",
        "inclusive_seconds",
        "exclusive_seconds",
    }
    assert payload["exclusive_seconds"]["model"] <= payload["inclusive_seconds"]["model"]
    assert len(payload["model_inputs"]) == 32
    assert payload["model_inputs"][0] == {"wire_bytes": 70000}
    assert payload["model_inputs"][1] == {
        "wire_bytes": 1000,
        "tool_count": 3,
        "message_count": 2,
    }


def test_provider_io_metadata_drops_unregistered_values_and_bounds_samples(caplog):
    import json
    import logging

    from app.platform.assistant.diagnostics import record_provider_io, request_trace

    with caplog.at_level(logging.INFO, logger="uvicorn.error"), request_trace("io-bounds"):
        for _ in range(40):
            record_provider_io(
                milestones_seconds={
                    "request_sent": 0.2,
                    "SECRET": 1,
                    "first_body": float("nan"),
                    "last_body": -1,
                },
                status=999,
                body_chunks=1,
                body_bytes=2**40,
                whitespace_bytes=2,
                outcome="SECRET",
            )
    payload = json.loads([r for r in caplog.records if r.msg == "assistant_timing %s"][-1].args[0])
    assert len(payload["provider_io"]) == 32
    assert payload["provider_io"][0] == {
        "milestones_seconds": {"request_sent": 0.2},
        "status": None,
        "body_chunks": 1,
        "body_bytes": 0,
        "whitespace_bytes": 2,
        "outcome": "other",
    }
    assert "SECRET" not in json.dumps(payload)


def test_protocol_metadata_is_bounded_and_drops_unregistered_values(caplog):
    import json
    import logging

    from app.platform.assistant.diagnostics import record_protocol_response, request_trace

    class Secret:
        def __str__(self):
            raise AssertionError("Diagnostic must not stringify private values")

    with caplog.at_level(logging.INFO, logger="uvicorn.error"):
        with request_trace("protocol-bounds"):
            for _ in range(70):
                record_protocol_response(
                    layer="PRIVATE-LAYER",
                    finish_reason=Secret(),
                    content_state="PRIVATE-CONTENT",
                    tool_call_count=2**60,
                    thinking_present="PRIVATE-THINKING",
                    other_parts=False,
                )
        with request_trace("next-protocol-request"):
            pass
    traces = [json.loads(r.args[0]) for r in caplog.records if r.msg == "assistant_timing %s"]
    assert len(traces[0]["protocol_responses"]) == 64
    assert traces[0]["protocol_responses"][0] == {
        "layer": "other",
        "finish_reason": "other",
        "content_state": "other",
        "tool_call_count": None,
        "thinking_present": False,
        "output_shape": "other",
    }
    assert "protocol_responses" not in traces[1]
    assert "PRIVATE" not in json.dumps(traces)


def test_protocol_metadata_concurrent_request_contexts_remain_separate(caplog):
    import asyncio
    import json
    import logging

    from app.platform.assistant.diagnostics import (
        record_final_review,
        record_protocol_response,
        request_trace,
    )

    async def simultaneous():
        started = [asyncio.Event(), asyncio.Event()]

        async def request(index):
            with request_trace("parallel-" + str(index)):
                record_protocol_response(
                    layer="sdk",
                    finish_reason="stop",
                    content_state="nonempty" if index else "empty",
                    tool_call_count=0,
                    thinking_present=not index,
                )
                record_final_review()
                started[index].set()
                await started[1 - index].wait()
                if index:
                    record_final_review(accepted=True)

        await asyncio.gather(request(0), request(1))

    with caplog.at_level(logging.INFO, logger="uvicorn.error"):
        asyncio.run(simultaneous())
    traces = {
        x["request_id"]: x
        for r in caplog.records
        if r.msg == "assistant_timing %s"
        for x in [json.loads(r.args[0])]
    }
    assert traces["parallel-0"]["final_review"] == {"started": True, "accepted": False}
    assert traces["parallel-1"]["final_review"] == {"started": True, "accepted": True}
    assert traces["parallel-0"]["protocol_responses"][0]["output_shape"] == "thinking_only"
    assert traces["parallel-1"]["protocol_responses"][0]["output_shape"] == "text"
    assert all(len(x["protocol_responses"]) == 1 for x in traces.values())


def test_request_trace_retains_only_bounded_registered_tool_aliases(caplog):
    import json
    import logging

    from app.platform.assistant import diagnostics

    record = getattr(diagnostics, "record_tool_alias", None)
    assert callable(record), "Tool order is needed without storing arguments or model bodies"
    with caplog.at_level(logging.INFO, logger="uvicorn.error"):
        with diagnostics.request_trace("synthetic-tool-sequence"):
            record("search_tools")
            record("tool_7")
            record("secret-or-user-body")
            record("tool_64")
            for _ in range(40):
                record("tool_3")
        record("tool_9")
        with diagnostics.request_trace("next-isolated-request"):
            pass
    traces = [json.loads(r.args[0]) for r in caplog.records if r.msg == "assistant_timing %s"]
    assert traces[0]["tool_sequence"] == ["search_tools", "tool_7"] + ["tool_3"] * 30
    assert "tool_sequence" not in traces[1]
    assert "secret-or-user-body" not in json.dumps(traces)
