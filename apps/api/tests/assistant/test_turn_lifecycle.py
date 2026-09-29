from __future__ import annotations

import json
from threading import Event
from uuid import UUID

import anyio
import pytest
from anyio import to_thread
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient

from app.core.request_id import assign_request_id
from app.interfaces.http.assistant import AssistantRequestIdentity, create_assistant_router
from app.interfaces.http.errors import ApiError
from app.interfaces.http.framework_request import (
    api_error_handler,
    request_validation_error_handler,
)
from app.platform.assistant.runtime import (
    AssistantRuntime,
    AssistantServiceError,
    RunSettlement,
    StaleRunError,
)


class _Runner:
    def __init__(self, *, block=False, fail=False):
        self.entered, self.release, self.finished = Event(), Event(), Event()
        self.block, self.fail, self.stale = block, fail, False

    def run_accepted(self, *, runtime, accepted, actor_context_ref, request_id):
        self.entered.set()
        try:
            if self.block:
                assert self.release.wait(3)
            runtime.require_current_run(
                accepted.conversation_ref, actor_context_ref, accepted.run_ref, accepted.generation
            )
            if isinstance(self.fail, str):
                raise AssistantServiceError("PRIVATE-ERROR-TOKEN", reason=self.fail)
            if self.fail:
                raise RuntimeError("PRIVATE-ERROR-TOKEN")
            return runtime.settle_run(
                accepted.conversation_ref,
                actor_context_ref,
                accepted.run_ref,
                accepted.generation,
                request_id,
                RunSettlement(display_payload={"text": "verified answer"}),
            )
        except StaleRunError:
            self.stale = True
            raise
        finally:
            self.finished.set()


def _app(runtime, runner, actor=None):
    actor = actor or {"value": "actor-1"}
    app = FastAPI()
    app.middleware("http")(assign_request_id)
    app.add_exception_handler(ApiError, api_error_handler)
    app.add_exception_handler(RequestValidationError, request_validation_error_handler)
    app.include_router(
        create_assistant_router(
            runtime=runtime,
            identity_dependency=lambda: AssistantRequestIdentity(actor["value"]),
            turn_orchestrator=runner,
        )
    )
    return app


def test_close_seals_delayed_accept_is_idempotent_and_history_stays_readable():
    runtime = AssistantRuntime(retention_seconds=86400)
    actor = {"value": "actor-1"}
    with TestClient(_app(runtime, _Runner(), actor)) as client:
        ref = client.post("/api/v1/assistant/conversations").json()["conversation_ref"]
        base = f"/api/v1/assistant/conversations/{ref}"
        actor["value"] = "other"
        assert client.post(base + "/close").status_code == 404
        actor["value"] = "actor-1"
        assert client.post(base + "/close", json={}).status_code == 422
        closed = client.post(base + "/close")
        assert closed.status_code == 200
        assert client.post(base + "/close").json() == closed.json()
        rejected = client.post(base + "/turns", json={"teacher_utterance": "late text"})
        assert rejected.status_code == 409
        assert rejected.json()["code"] == "assistant.conversation_closed"
        assert client.get(base).json() == closed.json()


def test_close_stops_original_run_and_rejects_late_settlement():
    runtime = AssistantRuntime()
    ref = runtime.create_conversation("actor-1", UUID(int=1)).conversation_ref
    accepted = runtime.accept_turn(ref, "actor-1", UUID(int=2), "question")
    with TestClient(_app(runtime, _Runner())) as client:
        closed = client.post(f"/api/v1/assistant/conversations/{ref}/close")
        assert closed.status_code == 200
        assert closed.json()["turns"][0]["visible_state"] == "stopped"
        with pytest.raises(StaleRunError):
            runtime.settle_run(
                ref,
                "actor-1",
                accepted.run_ref,
                accepted.generation,
                UUID(int=3),
                RunSettlement(display_payload={"text": "late"}),
            )


def test_close_does_not_cancel_reserved_formal_control():
    runtime = AssistantRuntime()
    ref = runtime.create_conversation("actor-1", UUID(int=1)).conversation_ref
    accepted = runtime.accept_turn(ref, "actor-1", UUID(int=2), "prepare")
    runtime.settle_run(
        ref,
        "actor-1",
        accepted.run_ref,
        accepted.generation,
        UUID(int=3),
        RunSettlement(
            display_payload={"text": "confirm"},
            module_ref="module",
            module_state_ref="state",
            state_revision=1,
            issuance_request_id=UUID(int=3),
        ),
    )
    runtime.reserve_control(
        conversation_ref=ref,
        actor_context_ref="actor-1",
        turn_ref=accepted.turn_ref,
        run_ref=accepted.run_ref,
        generation=accepted.generation,
        module_ref="module",
        module_state_ref="state",
        state_revision=1,
        issuance_request_id=UUID(int=3),
        click_request_id=UUID(int=4),
        kind="action_offer",
        expires_at=4102444800,
    )
    before = runtime.snapshot(ref, "actor-1")
    with TestClient(_app(runtime, _Runner())) as client:
        rejected = client.post(f"/api/v1/assistant/conversations/{ref}/close")
        assert rejected.status_code == 409
        assert rejected.json()["code"] == "assistant.conversation_busy"
    assert runtime.snapshot(ref, "actor-1") == before


def _exchange(*, disconnect=False, fail=False, prior=False, asgi_version="2.3"):
    runtime = AssistantRuntime()
    runner = _Runner(block=disconnect, fail=fail)
    ref = runtime.create_conversation("actor-1", UUID(int=1)).conversation_ref
    if prior:
        old = runtime.accept_turn(ref, "actor-1", UUID(int=10), "prior")
        runtime.settle_run(
            ref,
            "actor-1",
            old.run_ref,
            old.generation,
            UUID(int=11),
            RunSettlement(display_payload={"text": "OLD-PRIVATE-FACT"}),
        )
    app = _app(runtime, runner)
    events, accepted_before_model = [], []

    async def run():
        disconnected = anyio.Event()
        supplied = False

        async def receive():
            nonlocal supplied
            if not supplied:
                supplied = True
                return {
                    "type": "http.request",
                    "body": b'{"teacher_utterance":"question"}',
                    "more_body": False,
                }
            await disconnected.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            if message["type"] == "http.response.body" and message.get("body"):
                for line in message["body"].splitlines():
                    event = json.loads(line)
                    events.append(event)
                    if event.get("event") == "accepted":
                        accepted_before_model.append(not runner.entered.is_set())

        async def cut_connection():
            assert await to_thread.run_sync(runner.entered.wait, 2)
            disconnected.set()

        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": asgi_version},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": f"/api/v1/assistant/conversations/{ref}/turns",
            "query_string": b"",
            "headers": [
                (b"content-type", b"application/json"),
                (b"accept", b"application/x-ndjson"),
            ],
            "server": ("test", 80),
            "client": ("test", 1),
            "root_path": "",
        }
        async with anyio.create_task_group() as group:
            if disconnect:
                group.start_soon(cut_connection)
            with anyio.fail_after(3):
                await app(scope, receive, send)
            if disconnect:
                assert runtime.snapshot(ref, "actor-1").active_run is None
                runner.release.set()
                assert await to_thread.run_sync(runner.finished.wait, 2)

    try:
        anyio.run(run)
    finally:
        runner.release.set()
    return runtime.snapshot(ref, "actor-1"), events, accepted_before_model, runner


def test_stream_emits_acceptance_before_model_and_only_verified_final_snapshot():
    snapshot, events, before, _ = _exchange()
    assert before == [True]
    assert [event["event"] for event in events] == ["accepted", "completed"]
    assert events[0]["snapshot"]["active_run"]["run_ref"] == snapshot.display_ledger[0].run_ref
    assert events[0]["snapshot"]["turns"][0]["display_payload"] is None
    assert events[1]["snapshot"]["turns"][0]["display_payload"] == {"text": "verified answer"}


@pytest.mark.parametrize("asgi_version", ["2.3", "2.4"])
def test_stream_disconnect_stops_before_blocked_call_returns_and_rejects_late_result(asgi_version):
    snapshot, events, before, runner = _exchange(disconnect=True, asgi_version=asgi_version)
    assert before == [True]
    assert [event["event"] for event in events] == ["accepted"]
    assert snapshot.display_ledger[0].visible_state == "stopped"
    assert runner.stale


def test_stream_unknown_failure_is_safely_classified_and_releases_run():
    snapshot, events, before, _ = _exchange(fail=True)
    assert before == [True]
    assert events[-1] == {"event": "error", "code": "assistant.runtime_unavailable"}
    assert "PRIVATE-ERROR-TOKEN" not in json.dumps(events)
    assert snapshot.active_run is None


@pytest.mark.parametrize(
    "reason", ["tool_limit", "deadline", "budget", "context_limit", "dependency", "output_limit"]
)
def test_stream_service_failure_keeps_safe_category_and_readable_failure_snapshot(reason):
    snapshot, events, _, _ = _exchange(fail=reason, prior=True)
    assert events[-1]["event"] == "completed"
    assert "OLD-PRIVATE-FACT" not in json.dumps(events[-1])
    assert len(events[-1]["snapshot"]["turns"]) == 1
    assert events[-1]["snapshot"]["active_run"] is None
    assert (
        events[-1]["snapshot"]["turns"][0]["display_payload"]["error_code"] == "assistant." + reason
    )
    assert snapshot.active_run is None
    payload = snapshot.display_ledger[-1].display_payload
    assert isinstance(payload, dict)
    assert payload["error_code"] == "assistant." + reason


def test_accepted_frame_does_not_retransmit_old_fact_bodies_before_revalidation():
    _, events, _, _ = _exchange(prior=True)
    assert "OLD-PRIVATE-FACT" not in json.dumps(events[0])
    assert len(events[0]["snapshot"]["turns"]) == 1
    assert events[0]["snapshot"]["turns"][0]["teacher_utterance"] == "question"
