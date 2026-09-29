"""Framework-native contract loading must never become business or permission routing."""

import asyncio
import hashlib
import json
from uuid import uuid4

import pytest

pytest.importorskip("pydantic_ai")

import httpx2
from pydantic import SecretStr
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import FunctionModel

from app.composition.deepseek_assistant_model import CurrentModelApproval
from app.composition.school_fact_pydanticai_model import build_guarded_model
from app.composition.school_fact_residence_gateway import MODULE as RESIDENCE
from app.composition.school_fact_residence_gateway import ResidenceToolGateway
from app.platform.assistant.pydanticai_runner import CandidateToolRunner
from app.platform.assistant.runtime import (
    AssistantRuntime,
    AssistantRuntimeError,
    AssistantServiceError,
    ContinuationEvidence,
    HistoryCheck,
    ResultContinuation,
)
from app.platform.assistant.tool_loop import ToolDescriptor, ToolExecutionResult
from tests.assistant.test_control_service import _direct_result


def test_model_loads_exact_contract_then_uses_existing_gateway_and_continues(caplog):
    import logging

    from app.platform.assistant.diagnostics import request_trace

    calls, visible = [], []

    class Gateway:
        descriptors = (
            ToolDescriptor(
                name="example.read",
                owner="example",
                kind="read",
                description="Read a value",
                parameters={"type": "object", "properties": {}},
            ),
            ToolDescriptor(
                name="example.details",
                owner="example",
                kind="read",
                description="Additional details",
                parameters={
                    "type": "object",
                    "properties": {"value": {"type": "integer"}},
                    "required": ["value"],
                },
                load_on_demand=True,
            ),
        )

        def revalidate(self, identity):
            assert identity == "actor"

        def execute(self, call, context):
            calls.append((call.name, call.arguments, context.actor_context_ref))
            return ToolExecutionResult(
                model_content={
                    "value": 7,
                    "status": "object_mismatch",
                    "stage": "original_residence",
                },
                history_check=HistoryCheck(lambda: True),
            )

    async def answer(messages, info):
        names = {tool.name for tool in info.function_tools}
        visible.append(names)
        if len(visible) == 1:
            assert names == {"tool_0", "search_tools"}
            assert "tool_1: Additional details" in next(
                t.description for t in info.function_tools if t.name == "search_tools"
            )
            return ModelResponse(
                parts=[ToolCallPart("search_tools", {"queries": ["tool_1"]}, "load")]
            )
        if len(visible) == 2:
            assert "tool_1" in names
            return ModelResponse(parts=[ToolCallPart("tool_1", {"value": 7}, "read")])
        return ModelResponse(parts=[TextPart("Seven")])

    runtime = AssistantRuntime()
    conversation = runtime.create_conversation("actor", uuid4())
    accepted = runtime.accept_turn(conversation.conversation_ref, "actor", uuid4(), "Read more")
    with caplog.at_level(logging.INFO, logger="uvicorn.error"), request_trace("aliases-test"):
        result = asyncio.run(
            CandidateToolRunner(
                model_factory=lambda _: FunctionModel(answer), gateway_factory=lambda _: Gateway()
            ).run_accepted(
                runtime=runtime, accepted=accepted, actor_context_ref="actor", request_id=uuid4()
            )
        )
    assert result.display_ledger[-1].display_payload["text"] == "Seven"
    assert calls == [("example.details", {"value": 7}, "actor")]
    assert len(visible) == 3
    trace = json.loads(
        next(r.message for r in caplog.records if r.message.startswith("assistant_timing ")).split(
            " ", 1
        )[1]
    )
    assert trace["tool_sequence"] == ["search_tools", "tool_1"]
    assert trace["tool_outcomes"] == [
        {
            "tool_alias": "tool_1",
            "status": "object_mismatch",
            "code": "unspecified",
            "stage": "original_residence",
        }
    ]


def test_owner_supplied_turn_context_reaches_model_only_for_first_bound_turn():
    observed: list[str] = []

    class Gateway:
        descriptors = ()

        def revalidate(self, identity):
            assert identity == "actor"

    async def answer(messages, info):
        observed.append(str(messages))
        return ModelResponse(parts=[TextPart("完成")])

    runner = CandidateToolRunner(
        model_factory=lambda _: FunctionModel(answer),
        gateway_factory=lambda _: Gateway(),
        instruction="基础指令",
        turn_instruction=lambda actor, run: (
            "可信首轮切换语境" if actor == "actor" and run.generation == 1 else ""
        ),
    )
    runtime = AssistantRuntime()
    conversation = runtime.create_conversation("actor", uuid4())
    first = runtime.accept_turn(conversation.conversation_ref, "actor", uuid4(), "问现在的孩子")
    asyncio.run(
        runner.run_accepted(
            runtime=runtime, accepted=first, actor_context_ref="actor", request_id=uuid4()
        )
    )
    second = runtime.accept_turn(conversation.conversation_ref, "actor", uuid4(), "再问一次")
    asyncio.run(
        runner.run_accepted(
            runtime=runtime, accepted=second, actor_context_ref="actor", request_id=uuid4()
        )
    )
    assert "可信首轮切换语境" in observed[0]
    assert "可信首轮切换语境" not in observed[1]


@pytest.mark.parametrize(
    "fault",
    [
        "none",
        "no_candidate",
        "missing_card",
        "missing_continuation",
        "mismatched_card",
        "direct_action",
        "revoked",
        "expired",
        "stopped",
        "deadline",
        "later_tool",
    ],
)
def test_context_limit_releases_only_current_reviewable_prepared_proposal(fault):
    """A capacity failure may expose a reviewed candidate, never a completed task."""
    from app.platform.assistant.contracts import ActionOfferV1

    allowed = [True]
    fresh = [True]
    calls: list[str] = []
    loaded: list[str] = []
    preview = {"kind": "fact_proposal", "proposal_ref": "proposal-one", "summary": "review"}
    proposal = _direct_result(
        state_revision=1,
        staged_state_ref="state-proposal-one",
        action_offer=ActionOfferV1(
            action_offer_ref="action-proposal-one", safe_label="正式确认"
        )
        if fault == "direct_action"
        else None,
    ).model_copy(
        update={"display_kind": "card", "display_payload": preview}
    )

    def original_result() -> ContinuationEvidence:
        loaded.append("original-result")
        return ContinuationEvidence(
            model_content={"status": "succeeded", "receipt_ref": "original-receipt"},
            fact_card={"kind": "receipt", "reference": "original-receipt"},
            history_check=HistoryCheck(lambda: fresh[0]),
        )

    class Gateway:
        descriptors = (
            ToolDescriptor(
                name="fixture.prepare",
                owner="fixture",
                kind="prepare",
                description="Prepare a candidate for full review",
                parameters={"type": "object", "properties": {}},
            ),
            ToolDescriptor(
                name="fixture.read",
                owner="fixture",
                kind="read",
                description="Read another required part",
                parameters={"type": "object", "properties": {}},
            ),
        )

        def revalidate(self, actor):
            assert actor == "actor"
            if not allowed[0]:
                raise AssistantRuntimeError("actor no longer authorized")

        def execute(self, call, _context):
            calls.append(call.name)
            if call.name == "fixture.read":
                return ToolExecutionResult(
                    model_content={"status": "not_finished"},
                    history_check=HistoryCheck(lambda: fresh[0]),
                )
            return ToolExecutionResult(
                model_content={"status": "prepared"},
                fact_card=None if fault == "missing_card" else preview,
                module_result=None if fault == "no_candidate" else proposal.model_copy(
                    update={"display_payload": {"different": True}}
                    if fault == "mismatched_card" else {}
                ),
                history_check=HistoryCheck(lambda: fresh[0]),
                continuation=None if fault == "missing_continuation" else ResultContinuation(
                    "proposal-one",
                    original_result,
                ),
            )

    runtime = AssistantRuntime()
    conversation = runtime.create_conversation("actor", uuid4())
    accepted = runtime.accept_turn(
        conversation.conversation_ref,
        "actor",
        uuid4(),
        "先给我看修改方案，确认后再核对另一项；不要执行",
    )
    phases = []

    async def answer(_messages, _info):
        phases.append(len(phases))
        if len(phases) == 1:
            return ModelResponse(parts=[ToolCallPart("tool_0", {}, "prepare")])
        if fault == "later_tool" and len(phases) == 2:
            return ModelResponse(parts=[ToolCallPart("tool_1", {}, "read")])
        if fault == "revoked":
            allowed[0] = False
        if fault == "expired":
            fresh[0] = False
        if fault == "stopped":
            runtime.stop_run(
                accepted.conversation_ref,
                "actor",
                accepted.run_ref,
                accepted.generation,
                uuid4(),
            )
        if fault == "deadline":
            runner.timeout_seconds = 0
        raise AssistantServiceError("synthetic capacity boundary", reason="context_limit")

    runner = CandidateToolRunner(
        model_factory=lambda _: FunctionModel(answer),
        gateway_factory=lambda _: Gateway(),
    )
    def run():
        return asyncio.run(
            runner.run_accepted(
                runtime=runtime,
                accepted=accepted,
                actor_context_ref="actor",
                request_id=accepted.request_id,
            )
        )
    if fault == "none":
        snapshot = run()
        display = snapshot.display_ledger[-1]
        assert display.display_payload["answer_incomplete"] is True
        assert "未完成" in display.display_payload["text"]
        assert "没有执行" in display.display_payload["text"]
        assert display.display_payload["facts"] == [preview]
        assert display.controls == ()
        assert snapshot.module_states[0].module_state_ref == "state-proposal-one"
        link = display.display_payload["continuations"][0]
        assert link["proposal_ref"] == "proposal-one"
        loader = runtime.continuation_loader(
            accepted.conversation_ref, "actor", link["handle"]
        )
        assert loader is not None
        evidence = loader()
        resumed = runtime.accept_continuation(
            accepted.conversation_ref,
            "actor",
            link["handle"],
            uuid4(),
            evidence=evidence,
        )
        assert resumed is not None
        assert resumed.teacher_utterance == accepted.teacher_utterance
        assert resumed.continuation_evidence == evidence
        assert snapshot.model_safe_ledger[-1].model_safe_template == display.display_payload["text"]
        assert "synthetic capacity boundary" not in str(snapshot.model_safe_ledger)
    else:
        with pytest.raises(AssistantRuntimeError):
            run()
        snapshot = runtime.snapshot(accepted.conversation_ref, "actor")
        assert all(entry.display_payload is None for entry in snapshot.display_ledger)
        assert snapshot.module_states == ()
    assert loaded == (["original-result"] if fault == "none" else [])
    expected_calls = (
        ["fixture.prepare", "fixture.read"]
        if fault == "later_tool"
        else ["fixture.prepare"]
    )
    assert calls == expected_calls


def test_f6_common_change_contracts_are_eager_and_detailed_actions_remain_discoverable():
    seen: list[dict[str, str]] = []

    class Gateway:
        descriptors = ResidenceToolGateway.descriptors

        def revalidate(self, _):
            return None

        def execute(self, *_):
            pytest.fail("Tool visibility must not execute a fact read or prepare")

    async def answer(_messages, info):
        visible = {item.name: item.description for item in info.function_tools}
        seen.append(visible)
        return ModelResponse(parts=[TextPart("继续按正式权限核对")])

    runtime = AssistantRuntime()
    conversation = runtime.create_conversation("actor", uuid4())
    accepted = runtime.accept_turn(conversation.conversation_ref, "actor", uuid4(), "准备换床")
    snapshot = asyncio.run(
        CandidateToolRunner(
            model_factory=lambda _: FunctionModel(answer),
            gateway_factory=lambda _: Gateway(),
        ).run_accepted(
            runtime=runtime,
            accepted=accepted,
            actor_context_ref="actor",
            request_id=uuid4(),
        )
    )
    assert snapshot.display_ledger[-1].display_payload["text"] == "继续按正式权限核对"
    assert len(seen) == 1
    for action in ("query", "describe", "prepare_transfer"):
        assert any(
            f"[能力标识：{RESIDENCE}.{action}]" in description
            for name, description in seen[0].items()
            if name != "search_tools"
        )
    assert "search_tools" in seen[0]
    for action in ("locate_change", "prepare"):
        descriptor = next(
            item for item in ResidenceToolGateway.descriptors
            if item.name == f"{RESIDENCE}.{action}"
        )
        assert descriptor.load_on_demand
        assert not any(
            f"[能力标识：{descriptor.name}]" in description
            for name, description in seen[0].items()
            if name != "search_tools"
        )
        alias = f"tool_{ResidenceToolGateway.descriptors.index(descriptor)}"
        assert f"{alias}: {descriptor.description}" in seen[0]["search_tools"]
        assert descriptor.name in seen[0]["search_tools"]


@pytest.mark.parametrize("owners", [("arbitrary_alpha", "unrelated_beta"), ("one", "two")])
@pytest.mark.parametrize("reverse_order", [False, True])
@pytest.mark.parametrize("reference_field", ["current_read", "read_next", "read_detail"])
def test_eager_tool_identity_maps_returned_domain_names_without_rewriting_results(
    owners, reverse_order, reference_field
):
    original_names = [f"{owner}.read" for owner in owners]
    descriptors = tuple(
        ToolDescriptor(
            name=name,
            owner=owner,
            kind="read",
            description="Read the selected value",
            parameters={
                "type": "object",
                "properties": {"reference": {"type": "string"}},
                "required": ["reference"],
                "additionalProperties": False,
            },
        )
        for owner, name in zip(owners, original_names, strict=True)
    )
    continuation = {
        reference_field: {
            "tool": original_names[1],
            "arguments": {"reference": "original-target"},
        }
    }
    calls, visible, missing_identity = [], [], []

    class Gateway:
        def __init__(self):
            self.descriptors = tuple(reversed(descriptors)) if reverse_order else descriptors

        def revalidate(self, actor):
            assert actor == "actor"

        def execute(self, call, context):
            calls.append((call.name, call.arguments))
            return ToolExecutionResult(
                model_content=continuation if call.name == original_names[0] else {"value": 7},
                history_check=HistoryCheck(lambda: True),
            )

    async def answer(messages, info):
        tools = {tool.name: tool for tool in info.function_tools if tool.name != "search_tools"}
        visible.append(tools)
        mapping = {}
        for descriptor in descriptors:
            matches = [
                tool
                for tool in tools.values()
                if tool.description == f"{descriptor.description} [能力标识：{descriptor.name}]"
            ]
            if len(matches) != 1:
                missing_identity.append((descriptor.name, len(matches)))
                continue
            mapping[descriptor.name] = matches[0].name
            assert descriptor.name not in tools
        if missing_identity:
            return ModelResponse(parts=[TextPart("Capability identity missing")])
        if len(visible) == 1:
            return ModelResponse(
                parts=[ToolCallPart(mapping[original_names[0]], {"reference": "receipt"}, "first")]
            )
        returned = [
            part
            for message in messages
            for part in message.parts
            if part.part_kind == "tool-return"
        ]
        if len(visible) == 2:
            assert returned[-1].content == continuation
            read = returned[-1].content[reference_field]
            return ModelResponse(
                parts=[ToolCallPart(mapping[read["tool"]], read["arguments"], "second")]
            )
        assert any(part.content == continuation for part in returned)
        return ModelResponse(parts=[TextPart("Seven" if len(visible) == 3 else "Continued")])

    runtime = AssistantRuntime()
    conversation = runtime.create_conversation("actor", uuid4())
    gateway = Gateway()
    runner = CandidateToolRunner(
        model_factory=lambda _: FunctionModel(answer), gateway_factory=lambda _: gateway
    )
    result = _run_turn(runner, runtime, conversation)
    assert missing_identity == []
    assert result.display_ledger[-1].display_payload["text"] == "Seven"
    assert calls == [
        (original_names[0], {"reference": "receipt"}),
        (original_names[1], {"reference": "original-target"}),
    ]
    result = _run_turn(runner, runtime, conversation)
    assert result.display_ledger[-1].display_payload["text"] == "Continued"
    assert len(calls) == 2 and len(visible) == 4
    assert continuation[reference_field]["tool"] == original_names[1]


@pytest.mark.parametrize("query", ["example.details", "details", "tool_99"])
def test_loading_does_not_guess_names_or_expose_unregistered_tools(query):
    sent = []

    class Gateway:
        descriptors = (
            ToolDescriptor(
                name="example.details",
                owner="example",
                kind="read",
                description="Details",
                parameters={"type": "object", "properties": {}},
                load_on_demand=True,
            ),
        )

        def revalidate(self, _):
            pass

        def execute(self, *_):
            raise AssertionError("Discovery must not read business data")

    async def answer(messages, info):
        sent.append({tool.name for tool in info.function_tools})
        if len(sent) == 1:
            return ModelResponse(parts=[ToolCallPart("search_tools", {"queries": [query]}, "load")])
        assert "tool_0" not in sent[-1]
        return ModelResponse(parts=[TextPart("No matching tool")])

    runtime = AssistantRuntime()
    c = runtime.create_conversation("actor", uuid4())
    accepted = runtime.accept_turn(c.conversation_ref, "actor", uuid4(), "Read")
    asyncio.run(
        CandidateToolRunner(
            model_factory=lambda _: FunctionModel(answer), gateway_factory=lambda _: Gateway()
        ).run_accepted(
            runtime=runtime, accepted=accepted, actor_context_ref="actor", request_id=uuid4()
        )
    )
    assert len(sent) == 2


def test_revocation_between_model_and_loading_stops_without_business_read():
    live = True

    class Gateway:
        descriptors = (
            ToolDescriptor(
                name="example.details",
                owner="example",
                kind="read",
                description="Details",
                parameters={"type": "object", "properties": {}},
                load_on_demand=True,
            ),
        )

        def revalidate(self, _):
            if not live:
                raise AssistantRuntimeError("revoked")

        def execute(self, *_):
            raise AssertionError("Must not execute")

    async def answer(messages, info):
        nonlocal live
        live = False
        return ModelResponse(parts=[ToolCallPart("search_tools", {"queries": ["tool_0"]}, "load")])

    runtime = AssistantRuntime()
    c = runtime.create_conversation("actor", uuid4())
    accepted = runtime.accept_turn(c.conversation_ref, "actor", uuid4(), "Read")
    with pytest.raises(AssistantRuntimeError):
        asyncio.run(
            CandidateToolRunner(
                model_factory=lambda _: FunctionModel(answer), gateway_factory=lambda _: Gateway()
            ).run_accepted(
                runtime=runtime, accepted=accepted, actor_context_ref="actor", request_id=uuid4()
            )
        )


class _FakeReservation:
    def __init__(self, permit, number):
        self.permit, self.number = permit, number
        self.settled = self.unknown_count = 0

    def mark_transmission_started(self):
        self.permit.events.append("mark")
        self.permit.on_mark(self.number)

    def settle(self, _usage):
        self.settled += 1

    def unknown(self):
        self.unknown_count += 1


@pytest.mark.parametrize("mode", ["success", "read_timeout", "cancel"])
def test_provider_io_is_bounded_private_and_preserves_accounting(mode, caplog):
    import logging

    from app.platform.assistant.diagnostics import request_trace

    events, calls = [], []
    permit = _FakePermit(events)
    closed = []

    class Body(httpx2.AsyncByteStream):
        async def __aiter__(self):
            yield b" \n"
            if mode == "cancel":
                await asyncio.Event().wait()
            if mode == "read_timeout":
                raise httpx2.ReadTimeout("SECRET-PROVIDER-BODY")
            yield _wire_response().content

        async def aclose(self):
            closed.append(True)

    async def handler(request):
        trace = request.extensions.get("trace")
        assert callable(trace), "Need actual transport stages, not just total model time"
        for stage in (
            "connection.connect_tcp.complete",
            "connection.start_tls.complete",
            "http11.send_request_body.complete",
        ):
            await trace(stage, {"request": "SECRET-HEADER"})
        await trace("SECRET-EVENT", {"exception": "SECRET-ERROR"})
        return httpx2.Response(200, stream=Body())

    runtime = AssistantRuntime()
    conversation = runtime.create_conversation("actor", uuid4())
    runner = CandidateToolRunner(
        model_factory=_wire_factory(permit, handler),
        gateway_factory=lambda _: _small_gateway(events, {"actor": True}, calls),
        timeout_seconds=0.2 if mode == "cancel" else 10,
    )
    with caplog.at_level(logging.INFO, logger="uvicorn.error"):
        with request_trace("provider-io-test"):
            if mode == "success":
                result = _run_turn(runner, runtime, conversation)
                assert result.display_ledger[-1].display_payload["text"] == "Synthetic answer"
            else:
                with pytest.raises(AssistantServiceError):
                    _run_turn(runner, runtime, conversation)
        with request_trace("isolated-next-request"):
            pass
    traces = [json.loads(r.args[0]) for r in caplog.records if r.msg == "assistant_timing %s"]
    report = traces[0]["provider_io"][0]
    assert report["status"] == 200
    assert report["body_chunks"] >= 1 and report["whitespace_bytes"] == 2
    assert "request_sent" in report["milestones_seconds"]
    assert "headers_received" in report["milestones_seconds"]
    assert (
        report["outcome"]
        == {"success": "complete", "read_timeout": "read_timeout", "cancel": "cancelled"}[mode]
    )
    assert "provider_io" not in traces[1]
    assert all(
        secret not in json.dumps(traces)
        for secret in ("SECRET", "Synthetic answer", "test-only-not-a-real-key")
    )
    assert len(permit.reservations) == 1 and len(closed) == 1 and not calls
    reservation = permit.reservations[0]
    assert (reservation.settled, reservation.unknown_count) == (
        (1, 0) if mode == "success" else (0, 1)
    )


class _FakePermit:
    """In-memory accounting only: no real ledger, secret or billable transport."""

    current_approval = CurrentModelApproval(
        model="deepseek-flash",
        approval_reference="test-only-tool-loading",
        price_verified_on="2026-09-12",
    )

    def __init__(self, events, on_mark=lambda _: None):
        self.events, self.on_mark = events, on_mark
        self.reservations = []
        self.limits = []

    def reserve(self, max_input_tokens, max_output_tokens):
        self.events.append("reserve")
        self.limits.append((max_input_tokens, max_output_tokens))
        reservation = _FakeReservation(self, len(self.reservations) + 1)
        self.reservations.append(reservation)
        return reservation


def _wire_response(*, name=None, arguments=None, call_id="call", thought="synthetic"):
    message = {"role": "assistant", "content": None if name else "Synthetic answer"}
    message["reasoning_content"] = thought
    if name:
        message["tool_calls"] = [
            {
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments or {})},
            }
        ]
    return httpx2.Response(
        200,
        json={
            "id": "test-only-completion",
            "object": "chat.completion",
            "created": 1788580000,
            "model": "deepseek-flash",
            "choices": [
                {"index": 0, "finish_reason": "tool_calls" if name else "stop", "message": message}
            ],
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 6406,
                "total_tokens": 6506,
                "prompt_cache_hit_tokens": 0,
                "prompt_cache_miss_tokens": 100,
                "completion_tokens_details": {"reasoning_tokens": 6405},
            },
        },
    )


def _wire_factory(permit, handler):
    def build(current):
        return build_guarded_model(
            api_key=SecretStr("test-only-not-a-real-key"),
            permit=permit,
            approval=permit.current_approval,
            current=current,
            max_input_tokens=65536,
            max_output_tokens=8192,
            transport=httpx2.MockTransport(handler),
        )

    return build


def _run_turn(runner, runtime, conversation, *, actor="actor"):
    accepted = runtime.accept_turn(conversation.conversation_ref, actor, uuid4(), "Synthetic query")
    return asyncio.run(
        runner.run_accepted(
            runtime=runtime, accepted=accepted, actor_context_ref=actor, request_id=uuid4()
        )
    )


@pytest.mark.parametrize("shape", ["text", "empty", "thinking_only", "tool", "mixed"])
def test_nonstreaming_protocol_metadata_distinguishes_shapes_without_content(shape, caplog):
    import logging

    from app.platform.assistant.diagnostics import request_trace

    events, calls, sends = [], [], []
    permit = _FakePermit(events)
    secret = "PRIVATE-OUTPUT-AND-THINKING"

    def handler(_request):
        sends.append(1)
        tool = shape in {"tool", "mixed"} and len(sends) == 1
        body = _wire_response(name="tool_0" if tool else None, thought=secret).json()
        message = body["choices"][0]["message"]
        if len(sends) == 1:
            message["content"] = secret if shape in {"text", "mixed"} else None
            if shape == "empty":
                message["reasoning_content"] = ""
        return httpx2.Response(200, json=body)

    gateway = _small_gateway(events, {"actor": True}, calls)
    gateway.descriptors = tuple(
        d.model_copy(update={"load_on_demand": False}) for d in gateway.descriptors
    )
    runner = CandidateToolRunner(
        model_factory=_wire_factory(permit, handler),
        gateway_factory=lambda _: gateway,
    )
    runtime = AssistantRuntime()
    conversation = runtime.create_conversation("actor", uuid4())

    def run():
        with request_trace("shape-test"):
            return _run_turn(runner, runtime, conversation)

    with caplog.at_level(logging.INFO, logger="uvicorn.error"):
        if shape in {"empty", "thinking_only"}:
            with pytest.raises(AssistantRuntimeError):
                run()
        else:
            run()
    trace = json.loads([r for r in caplog.records if r.msg == "assistant_timing %s"][-1].args[0])
    records = trace["protocol_responses"]
    assert [r["layer"] for r in records[:2]] == ["wire", "sdk"]
    assert [r["output_shape"] for r in records[:2]] == [shape, shape]
    assert records[0]["content_state"] == ("nonempty" if shape in {"text", "mixed"} else "null")
    assert records[0]["finish_reason"] == ("tool_calls" if shape in {"tool", "mixed"} else "stop")
    assert all(secret not in json.dumps(r) for r in records)
    assert "Synthetic answer" not in json.dumps(trace)
    assert len(sends) == (2 if shape in {"tool", "mixed"} else 1)
    assert all(r.settled == 1 and r.unknown_count == 0 for r in permit.reservations)


@pytest.mark.parametrize("review_shape", ["text", "thinking_only", "empty"])
def test_review_empty_response_gets_one_bounded_retry_without_publishing_draft(
    review_shape, caplog
):
    import logging

    from app.platform.assistant.diagnostics import request_trace

    events, calls, sends = [], [], []
    permit = _FakePermit(events)

    def handler(_request):
        sends.append(1)
        body = _wire_response(thought="PRIVATE-THOUGHT").json()
        message = body["choices"][0]["message"]
        message["content"] = "PRIVATE-DRAFT" if len(sends) == 1 else "PRIVATE-FINAL"
        if len(sends) == 2 and review_shape != "text":
            message["content"] = None
            if review_shape == "empty":
                message["reasoning_content"] = ""
        return httpx2.Response(200, json=body)

    runner = CandidateToolRunner(
        model_factory=_wire_factory(permit, handler),
        gateway_factory=lambda _: _small_gateway(events, {"actor": True}, calls),
        final_review_instruction="PRIVATE-REVIEW-INSTRUCTION",
    )
    runtime = AssistantRuntime()
    conversation = runtime.create_conversation("actor", uuid4())

    def run():
        with request_trace("review-shape"):
            return _run_turn(runner, runtime, conversation)

    with caplog.at_level(logging.INFO, logger="uvicorn.error"):
        run()
    trace = json.loads([r for r in caplog.records if r.msg == "assistant_timing %s"][-1].args[0])
    assert trace["final_review"] == {"started": True, "accepted": True}
    if review_shape != "text":
        assert trace["protocol_responses"][2]["output_shape"] == review_shape
    assert trace["protocol_responses"][-1]["output_shape"] == "text"
    assert len(sends) == (2 if review_shape == "text" else 3) and not calls
    assert "PRIVATE" not in json.dumps(trace)
    snapshot = runtime.snapshot(conversation.conversation_ref, "actor")
    assert snapshot.model_safe_ledger[-1].model_safe_template == "PRIVATE-FINAL"
    assert "PRIVATE-DRAFT" not in str(snapshot.display_ledger)
    assert permit.limits == [(65536, 8192)] * len(sends)
    assert all(r.settled == 1 and r.unknown_count == 0 for r in permit.reservations)


@pytest.mark.parametrize("request_limit, expected_sends", [(8, 3), (2, 2)])
def test_review_empty_retry_still_fails_closed_at_output_or_request_limit(
    request_limit, expected_sends, caplog
):
    import logging

    from app.platform.assistant.diagnostics import request_trace

    events, calls, sends = [], [], []
    permit = _FakePermit(events)

    def handler(_request):
        sends.append(1)
        body = _wire_response(thought="PRIVATE-THOUGHT").json()
        body["choices"][0]["message"]["content"] = (
            "PRIVATE-DRAFT" if len(sends) == 1 else None
        )
        return httpx2.Response(200, json=body)

    runner = CandidateToolRunner(
        model_factory=_wire_factory(permit, handler),
        gateway_factory=lambda _: _small_gateway(events, {"actor": True}, calls),
        final_review_instruction="PRIVATE-REVIEW-INSTRUCTION",
        max_model_calls=request_limit,
    )
    runtime = AssistantRuntime()
    conversation = runtime.create_conversation("actor", uuid4())
    with caplog.at_level(logging.INFO, logger="uvicorn.error"):
        with request_trace("review-empty-bounded"):
            with pytest.raises(AssistantRuntimeError):
                _run_turn(runner, runtime, conversation)
    trace = json.loads([r for r in caplog.records if r.msg == "assistant_timing %s"][-1].args[0])
    assert len(sends) == expected_sends == len(permit.reservations)
    assert trace["final_review"] == {"started": True, "accepted": False}
    snapshot = runtime.snapshot(conversation.conversation_ref, "actor")
    assert snapshot.model_safe_ledger == ()
    assert all(entry.display_kind is None for entry in snapshot.display_ledger)
    assert calls == []
    assert permit.limits == [(65536, 8192)] * len(sends)
    assert all(r.settled == 1 and r.unknown_count == 0 for r in permit.reservations)


@pytest.mark.parametrize("mode", ["success", "reserve_failure", "output_limit", "revoked"])
def test_final_review_reuses_guarded_wire_budget_and_settles_each_attempt(mode):
    events, calls, sends = [], [], []
    live = {"actor": True}

    class Permit(_FakePermit):
        def reserve(self, max_input_tokens, max_output_tokens):
            if mode == "reserve_failure" and self.reservations:
                raise OSError("synthetic review reservation failure")
            return super().reserve(max_input_tokens, max_output_tokens)

    def mark(number):
        if mode == "revoked" and number == 2:
            live["actor"] = False

    permit = Permit(events, on_mark=mark)

    def handler(request):
        sends.append(1)
        response = _wire_response()
        if mode == "output_limit" and len(sends) == 2:
            body = response.json()
            body["choices"][0]["finish_reason"] = "length"
            return httpx2.Response(200, json=body)
        return response

    runner = CandidateToolRunner(
        model_factory=_wire_factory(permit, handler),
        gateway_factory=lambda _: _small_gateway(events, live, calls),
        final_review_instruction="Review the final answer using only evidence already obtained.",
    )
    runtime = AssistantRuntime()
    conversation = runtime.create_conversation("actor", uuid4())
    if mode == "success":
        result = _run_turn(runner, runtime, conversation)
        assert result.model_safe_ledger[-1].model_safe_template == "Synthetic answer"
    else:
        with pytest.raises(AssistantRuntimeError):
            _run_turn(runner, runtime, conversation)
        assert runtime.snapshot(conversation.conversation_ref, "actor").model_safe_ledger == ()
    assert sends == [1] * (2 if mode in {"success", "output_limit"} else 1)
    assert calls == []
    assert permit.limits == [(65536, 8192)] * len(permit.reservations)
    assert permit.reservations[0].settled == 1
    if mode != "reserve_failure":
        assert len(permit.reservations) == 2
        assert permit.reservations[1].settled == (0 if mode == "revoked" else 1)
        assert permit.reservations[1].unknown_count == (1 if mode == "revoked" else 0)


def test_real_sdk_checks_full_authority_once_after_marker_before_each_send():
    events, calls = [], []
    permit = _FakePermit(events)

    def handler(_request):
        events.append("send")
        return _wire_response()

    runner = CandidateToolRunner(
        model_factory=_wire_factory(permit, handler),
        gateway_factory=lambda _: _small_gateway(events, {"actor": True}, calls),
    )
    runtime = AssistantRuntime()
    conversation = runtime.create_conversation("actor", uuid4())
    _run_turn(runner, runtime, conversation)

    assert events == ["current", "reserve", "mark", "current", "send", "current"]
    assert calls == []
    assert permit.limits == [(65536, 8192)]
    assert permit.reservations[0].settled == 1
    assert permit.reservations[0].unknown_count == 0


@pytest.mark.parametrize("change", ["revoked", "logged_out", "history", "fact_time", "deadline"])
def test_real_sdk_full_release_rechecks_after_blocked_marker(change, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from app.platform.assistant import pydanticai_runner as runner_module

    events, calls, sends = [], [], []
    state = {"grant": True, "session": True, "history": True, "clock": 0.0}
    marker_entered, changed = Event(), Event()
    monkeypatch.setattr(runner_module, "monotonic", lambda: state["clock"])

    class Gateway:
        descriptors = tuple(
            item.model_copy(update={"load_on_demand": False})
            for item in _small_gateway([], {}, []).descriptors
        )

        def revalidate(self, _actor):
            events.append("current")
            if not state["grant"] or not state["session"]:
                raise AssistantRuntimeError("current identity unavailable")

        def execute(self, _call, _context):
            calls.append("read")

            def proof():
                events.append("history")
                return state["history"] and state["clock"] < 0.5

            return ToolExecutionResult(
                model_content={"synthetic_value": 7}, history_check=HistoryCheck(proof)
            )

    def change_while_marker_waits():
        assert marker_entered.wait(5)
        if change == "revoked":
            state["grant"] = False
        elif change == "logged_out":
            state["session"] = False
        elif change == "history":
            state["history"] = False
        elif change == "fact_time":
            state["clock"] = 0.5
        else:
            state["clock"] = 2.0
        events.append(change)
        changed.set()

    def mark(number):
        if number == 2:
            marker_entered.set()
            assert changed.wait(5)

    def handler(_request):
        assert events[-1] == "current"
        events.append("send")
        sends.append(1)
        return _wire_response(name="tool_0", call_id="first-read")

    permit = _FakePermit(events, on_mark=mark)
    runner = CandidateToolRunner(
        model_factory=_wire_factory(permit, handler),
        gateway_factory=lambda _: Gateway(),
        timeout_seconds=1,
    )
    runtime = AssistantRuntime()
    conversation = runtime.create_conversation("actor", uuid4())
    with ThreadPoolExecutor(max_workers=1) as workers:
        future = workers.submit(change_while_marker_waits)
        with pytest.raises(AssistantRuntimeError):
            _run_turn(runner, runtime, conversation)
        future.result()
    assert calls == ["read"] and sends == [1]
    assert len(permit.reservations) == 2
    assert permit.reservations[0].settled == 1
    assert permit.reservations[0].unknown_count == 0
    assert permit.reservations[1].settled == 0
    assert permit.reservations[1].unknown_count == 1
    assert runtime.snapshot(conversation.conversation_ref, "actor").model_safe_ledger == ()


@pytest.mark.parametrize("failure", ["reserve", "marker", "cancel"])
def test_real_sdk_ledger_failure_or_marker_cancellation_never_sends(failure):
    events, sends = [], []

    def mark(_number):
        if failure == "cancel":
            raise asyncio.CancelledError
        raise OSError("synthetic ledger failure")

    class Permit(_FakePermit):
        def reserve(self, max_input_tokens, max_output_tokens):
            if failure == "reserve":
                raise OSError("synthetic reservation failure")
            return super().reserve(max_input_tokens, max_output_tokens)

    def handler(_request):
        sends.append(1)
        return _wire_response()

    permit = Permit(events, on_mark=mark)
    runner = CandidateToolRunner(
        model_factory=_wire_factory(permit, handler),
        gateway_factory=lambda _: _small_gateway(events, {"actor": True}, []),
    )
    runtime = AssistantRuntime()
    conversation = runtime.create_conversation("actor", uuid4())
    expected = asyncio.CancelledError if failure == "cancel" else AssistantRuntimeError
    with pytest.raises(expected):
        _run_turn(runner, runtime, conversation)
    assert sends == []
    assert len(permit.reservations) == (0 if failure == "reserve" else 1)
    assert all(item.settled == 0 and item.unknown_count == 1 for item in permit.reservations)


@pytest.mark.parametrize(
    "finish,partial_tool,expected",
    [
        ("length", False, "output_limit"),
        ("length", True, "output_limit"),
        ("content_filter", False, "dependency"),
        ("stop", False, None),
    ],
)
def test_explicit_output_limit_settles_usage_without_executing_partial_tools(
    finish, partial_tool, expected
):
    events, calls, sends = [], [], []
    gateway = _small_gateway(events, {"actor": True}, calls)
    gateway.descriptors = tuple(
        d.model_copy(update={"load_on_demand": False}) for d in gateway.descriptors
    )
    permit = _FakePermit(events)

    def handler(request):
        sends.append(1)
        payload = json.loads(_wire_response(name="tool_0" if partial_tool else None).content)
        payload["choices"][0]["finish_reason"] = finish
        if partial_tool:
            payload["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] = '{"x":'
        # Full measured usage alone must not turn a valid stop into output_limit.
        payload["usage"]["completion_tokens"] = 8192
        payload["usage"]["total_tokens"] = 8292
        payload["usage"]["completion_tokens_details"]["reasoning_tokens"] = 8192
        return httpx2.Response(200, json=payload)

    runner = CandidateToolRunner(
        model_factory=_wire_factory(permit, handler), gateway_factory=lambda _: gateway
    )
    runtime = AssistantRuntime()
    conversation = runtime.create_conversation("actor", uuid4())
    if expected is None:
        _run_turn(runner, runtime, conversation)
    else:
        with pytest.raises(AssistantServiceError) as failure:
            _run_turn(runner, runtime, conversation)
        assert failure.value.reason == expected
    assert sends == [1] and calls == []
    assert len(permit.reservations) == 1
    assert permit.reservations[0].settled == 1
    assert permit.reservations[0].unknown_count == 0


def _small_gateway(events, live, calls):
    class Gateway:
        descriptors = (
            ToolDescriptor(
                name="example.details",
                owner="example",
                kind="read",
                description="Additional details",
                parameters={"type": "object", "properties": {}},
                load_on_demand=True,
            ),
        )

        def revalidate(self, actor):
            events.append("current")
            if not live[actor]:
                raise AssistantRuntimeError("revoked")

        def execute(self, call, context):
            assert events[-1] == "current"
            calls.append((call.name, context.actor_context_ref))
            return ToolExecutionResult(
                model_content={"synthetic_value": 7},
                history_check=HistoryCheck(lambda: live[context.actor_context_ref]),
            )

    return Gateway()


@pytest.mark.parametrize("tool_limit", [1, 2])
def test_real_sdk_loading_consumes_existing_tool_budget(tool_limit):
    events, calls, sent = [], [], []
    gateway = _small_gateway(events, {"actor": True}, calls)
    permit = _FakePermit(events)
    discovery_thought = "  SYNTHETIC_DISCOVERY\n保留🙂  "
    read_thought = "  SYNTHETIC_READ\n保留  "

    def handler(request):
        assert events[-1] == "current"
        events.append("send")
        body = json.loads(request.content)
        assert body["thinking"] == {"type": "enabled"}
        assert body["reasoning_effort"] == "high"
        sent.append({tool["function"]["name"] for tool in body["tools"]})
        if len(sent) == 1:
            assert "tool_0" not in sent[-1]
            return _wire_response(
                name="search_tools",
                arguments={"queries": ["tool_0"]},
                thought=discovery_thought,
            )
        thoughts = [
            m["reasoning_content"]
            for m in body["messages"]
            if m.get("role") == "assistant" and m.get("reasoning_content")
        ]
        returns = [m for m in body["messages"] if m.get("role") == "tool"]
        assert json.loads(returns[0]["content"])["discovered_tools"] == [{"name": "tool_0"}]
        if len(sent) == 2:
            assert thoughts == [discovery_thought]
            assert "tool_0" in sent[-1]
            return _wire_response(name="tool_0", call_id="actual-read", thought=read_thought)
        assert thoughts == [discovery_thought, read_thought]
        assert json.loads(returns[-1]["content"]) == {"synthetic_value": 7}
        return _wire_response()

    runner = CandidateToolRunner(
        model_factory=_wire_factory(permit, handler),
        gateway_factory=lambda _: gateway,
        max_tool_calls=tool_limit,
    )
    runtime = AssistantRuntime()
    conversation = runtime.create_conversation("actor", uuid4())
    if tool_limit == 1:
        with pytest.raises(AssistantServiceError) as failure:
            _run_turn(runner, runtime, conversation)
        assert failure.value.reason == "tool_limit"
        assert calls == [] and len(sent) == 2
    else:
        _run_turn(runner, runtime, conversation)
        assert calls == [("example.details", "actor")] and len(sent) == 3
    assert all(r.settled == 1 and r.unknown_count == 0 for r in permit.reservations)
    assert permit.limits == [(65536, 8192)] * len(sent)


@pytest.mark.parametrize("revoke_at", ["loaded_send_marker", "loaded_response"])
def test_real_sdk_loaded_tool_rechecks_before_send_and_before_execution(revoke_at):
    events, calls, sent = [], [], []
    live = {"actor": True}
    gateway = _small_gateway(events, live, calls)

    def mark(number):
        if revoke_at == "loaded_send_marker" and number == 2:
            live["actor"] = False

    permit = _FakePermit(events, on_mark=mark)

    def handler(request):
        assert events[-1] == "current"
        events.append("send")
        body = json.loads(request.content)
        sent.append(len(request.content))
        if len(sent) == 1:
            return _wire_response(name="search_tools", arguments={"queries": ["tool_0"]})
        assert "tool_0" in {t["function"]["name"] for t in body["tools"]}
        live["actor"] = False
        return _wire_response(name="tool_0", call_id="read-after-revocation")

    runner = CandidateToolRunner(
        model_factory=_wire_factory(permit, handler), gateway_factory=lambda _: gateway
    )
    runtime = AssistantRuntime()
    conversation = runtime.create_conversation("actor", uuid4())
    with pytest.raises(AssistantRuntimeError):
        _run_turn(runner, runtime, conversation)
    assert calls == []
    assert len(sent) == (1 if revoke_at == "loaded_send_marker" else 2)
    assert len(permit.reservations) == 2
    assert permit.reservations[-1].unknown_count == int(revoke_at == "loaded_send_marker")


def test_real_sdk_cannot_call_a_deferred_tool_before_loading_its_contract():
    events, calls, sent = [], [], []
    gateway = _small_gateway(events, {"actor": True}, calls)
    permit = _FakePermit(events)

    def handler(request):
        body = json.loads(request.content)
        assert {tool["function"]["name"] for tool in body["tools"]} == {"search_tools"}
        sent.append(len(request.content))
        return _wire_response(name="tool_0", call_id="unloaded-candidate")

    runner = CandidateToolRunner(
        model_factory=_wire_factory(permit, handler), gateway_factory=lambda _: gateway
    )
    runtime = AssistantRuntime()
    conversation = runtime.create_conversation("actor", uuid4())
    with pytest.raises(AssistantRuntimeError):
        _run_turn(runner, runtime, conversation)
    assert calls == [] and len(sent) == 1
    assert len(permit.reservations) == 1
    assert permit.reservations[0].settled == 1
    assert permit.reservations[0].unknown_count == 0


@pytest.mark.parametrize("domain_name", ["example.details", "example_details"])
def test_real_sdk_eager_capability_identity_is_metadata_not_an_additional_call_name(domain_name):
    events, calls, sent = [], [], []
    gateway = _small_gateway(events, {"actor": True}, calls)
    gateway.descriptors = tuple(
        descriptor.model_copy(update={"load_on_demand": False, "name": domain_name})
        for descriptor in gateway.descriptors
    )
    descriptor = gateway.descriptors[0]
    permit = _FakePermit(events)

    def handler(request):
        body = json.loads(request.content)
        tools = {tool["function"]["name"]: tool["function"] for tool in body["tools"]}
        assert descriptor.name not in tools
        assert tools["tool_0"]["description"] == (
            f"{descriptor.description} [能力标识：{descriptor.name}]"
        )
        suffix_bytes = len(tools["tool_0"]["description"].encode()) - len(
            descriptor.description.encode()
        )
        assert suffix_bytes == 18 + len(descriptor.name.encode())
        sent.append(suffix_bytes)
        return _wire_response(name=descriptor.name, call_id="unregistered-domain-name")

    runner = CandidateToolRunner(
        model_factory=_wire_factory(permit, handler), gateway_factory=lambda _: gateway
    )
    runtime = AssistantRuntime()
    conversation = runtime.create_conversation("actor", uuid4())
    with pytest.raises(AssistantRuntimeError):
        _run_turn(runner, runtime, conversation)
    assert calls == [] and sent == [33]
    assert len(permit.reservations) == 1
    assert permit.reservations[0].settled + permit.reservations[0].unknown_count == 1


def test_real_sdk_loaded_history_is_reusable_only_in_its_live_identity():
    events, calls, sent = [], [], []
    live = {"actor": True, "other": True}
    gateway = _small_gateway(events, live, calls)
    permit = _FakePermit(events)

    def handler(request):
        assert events[-1] == "current"
        events.append("send")
        body = json.loads(request.content)
        names = {tool["function"]["name"] for tool in body["tools"]}
        sent.append(names)
        number = len(sent)
        if number == 1:
            assert names == {"search_tools"}
            return _wire_response(name="search_tools", arguments={"queries": ["tool_0"]})
        if number in {2, 4}:
            assert "tool_0" in names
            if number == 4:
                assert any(m.get("role") == "tool" for m in body["messages"])
            return _wire_response(name="tool_0", call_id=f"read-{number}")
        if number == 6:
            assert names == {"search_tools"}
            assert not any(m.get("role") == "tool" for m in body["messages"])
        return _wire_response()

    runner = CandidateToolRunner(
        model_factory=_wire_factory(permit, handler), gateway_factory=lambda _: gateway
    )
    runtime = AssistantRuntime()
    conversation = runtime.create_conversation("actor", uuid4())
    first = _run_turn(runner, runtime, conversation)
    assert first.display_ledger[-1].display_payload["history_retention"] == "retained"
    _run_turn(runner, runtime, conversation)
    assert len(sent) == 5
    other = runtime.create_conversation("other", uuid4())
    _run_turn(runner, runtime, other, actor="other")
    assert len(sent) == 6
    live["actor"] = False
    with pytest.raises(AssistantRuntimeError):
        _run_turn(runner, runtime, conversation)
    assert len(sent) == 6
    assert calls == [("example.details", "actor"), ("example.details", "actor")]


def _staff_tools_and_instruction():
    from app.composition.school_fact_directory_gateway import (
        DIRECTORY_ASSISTANT_INSTRUCTION,
        DirectoryFactToolGateway,
    )
    from app.composition.school_fact_education_gateway import (
        EDUCATION_ASSISTANT_INSTRUCTION,
        EducationToolGateway,
    )
    from app.composition.school_fact_family_staff_gateway import (
        FamilyStaffToolGateway,
        family_staff_instruction,
    )
    from app.composition.school_fact_product import F1_ASSISTANT_INSTRUCTION
    from app.composition.school_fact_teaching_gateway import (
        TEACHING_ASSISTANT_INSTRUCTION,
        StaffTeachingGateway,
    )

    education = EducationToolGateway(
        resolve_actor=lambda _: None,
        query=None,
        product=None,
        describe_adapter=None,
        teaching_enabled=True,
    )
    return (
        (
            *DirectoryFactToolGateway.descriptors,
            *FamilyStaffToolGateway.descriptors,
            *education.descriptors,
            *StaffTeachingGateway.descriptors,
        ),
        F1_ASSISTANT_INSTRUCTION
        + DIRECTORY_ASSISTANT_INSTRUCTION
        + family_staff_instruction(education_enabled=True)
        + EDUCATION_ASSISTANT_INSTRUCTION
        + TEACHING_ASSISTANT_INSTRUCTION,
    )


def test_real_sdk_auxiliary_contract_loading_reduces_wire_without_dropping_evidence(monkeypatch):
    from app.composition import school_fact_pydanticai_model as model_module

    descriptors, instruction = _staff_tools_and_instruction()
    shared_aliases = {
        f"school_facts.{owner}.{action}"
        for owner in ("education", "family_staff")
        for action in ("own_receipts", "own_receipt")
    }
    descriptors = tuple(d for d in descriptors if d.name not in shared_aliases)
    auxiliary = {
        d.name
        for d in descriptors
        if d.name.endswith((".lookup", ".access", ".original", ".receipt"))
    }
    assert len(auxiliary) == 6
    thought = "合成协议思考" * 1500
    material = {"synthetic_evidence": "必要证据", "source": "synthetic-source"}
    metrics = {"previous": [], "current": []}
    mode = "previous"
    original = model_module._GuardedTransport.handle_async_request

    async def measured(self, request):
        body = json.loads(request.content)
        metrics[mode].append(len(request.content))
        if len(metrics[mode]) == 2:
            assert any(m.get("reasoning_content") == thought for m in body["messages"])
            assert any(
                m.get("role") == "tool" and json.loads(m["content"]) == material
                for m in body["messages"]
            )
        assert body["thinking"] == {"type": "enabled"}
        assert body["reasoning_effort"] == "high"
        return await original(self, request)

    monkeypatch.setattr(model_module._GuardedTransport, "handle_async_request", measured)
    for mode in metrics:
        sent = []
        permit = _FakePermit([])

        class Gateway:
            def __init__(self, selected_mode=mode):
                self.descriptors = tuple(
                    d.model_copy(update={"load_on_demand": False})
                    if selected_mode == "previous" and d.name in auxiliary
                    else d
                    for d in descriptors
                )

            def revalidate(self, _):
                pass

            def execute(self, call, _):
                assert call.name == "school_facts.directory.query"
                return ToolExecutionResult(
                    model_content=material, history_check=HistoryCheck(lambda: True)
                )

        def handler(request, sent=sent):
            sent.append(len(request.content))
            return (
                _wire_response(name="tool_2", thought=thought)
                if len(sent) == 1
                else _wire_response()
            )

        runner = CandidateToolRunner(
            model_factory=_wire_factory(permit, handler),
            gateway_factory=lambda _: Gateway(),
            instruction=instruction,
        )
        runtime = AssistantRuntime()
        conversation = runtime.create_conversation("actor", uuid4())
        if mode == "previous":
            with pytest.raises(AssistantServiceError) as failure:
                _run_turn(runner, runtime, conversation)
            assert failure.value.reason == "context_limit"
            assert len(sent) == len(permit.reservations) == 1
        else:
            _run_turn(runner, runtime, conversation)
            assert len(sent) == len(permit.reservations) == 2
    assert metrics["previous"][1] > 65536 > metrics["current"][1]
    assert metrics["previous"][0] - metrics["current"][0] > 5900
    print("SDK_AUXILIARY_LOADING_BYTES=" + json.dumps(metrics, sort_keys=True))


@pytest.mark.parametrize(
    "domain_name",
    [
        "school_facts.directory.lookup",
        "school_facts.directory.access",
        "school_facts.directory.original",
        "school_facts.directory.receipt",
        "school_facts.family_staff.receipt",
        "school_facts.education.receipt",
    ],
)
def test_real_sdk_auxiliary_contract_remains_discoverable_and_callable(domain_name):
    from app.platform.assistant.pydanticai_runner import _tool_schema_presentation

    descriptors, instruction = _staff_tools_and_instruction()
    index, descriptor = next((i, d) for i, d in enumerate(descriptors) if d.name == domain_name)
    alias = f"tool_{index}"
    assert descriptor.load_on_demand
    calls, sent = [], []
    permit = _FakePermit([])

    class Gateway:
        def __init__(self):
            self.descriptors = descriptors

        def revalidate(self, _):
            pass

        def execute(self, call, _):
            calls.append(call.name)
            return ToolExecutionResult(
                model_content={"synthetic": 7}, history_check=HistoryCheck(lambda: True)
            )

    def handler(request):
        body = json.loads(request.content)
        tools = {t["function"]["name"]: t["function"] for t in body["tools"]}
        sent.append(len(request.content))
        if len(sent) == 1:
            assert alias not in tools
            assert f"{alias}: {descriptor.description}" in tools["search_tools"]["description"]
            assert domain_name in tools["search_tools"]["description"]
            return _wire_response(name="search_tools", arguments={"queries": [alias]})
        assert tools[alias]["parameters"] == _tool_schema_presentation(descriptor.parameters)
        if len(sent) == 2:
            return _wire_response(name=alias, call_id="auxiliary-read")
        return _wire_response()

    runtime = AssistantRuntime()
    conversation = runtime.create_conversation("actor", uuid4())
    _run_turn(
        CandidateToolRunner(
            model_factory=_wire_factory(permit, handler),
            gateway_factory=lambda _: Gateway(),
            instruction=instruction,
        ),
        runtime,
        conversation,
    )
    assert calls == [domain_name]
    assert len(sent) == len(permit.reservations) == 3


@pytest.mark.parametrize("oversized", [False, True])
def test_wire_component_counts_include_rejected_requests_without_private_content(
    oversized, caplog, monkeypatch
):
    from app.composition import school_fact_pydanticai_model as model_module
    from app.platform.assistant.diagnostics import request_trace

    secret = "SYNTHETIC_PRIVATE_WIRE_CONTENT"
    permit = _FakePermit([])
    expected = []
    original = model_module._GuardedTransport.handle_async_request

    def size(value):
        return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode())

    async def measured(self, request):
        body = json.loads(request.content)
        expected.append(
            {
                "wire_bytes": len(request.content),
                "tool_schema_bytes": size(body["tools"]),
                "message_count": len(body["messages"]),
                "system_message_bytes": sum(
                    size(m) for m in body["messages"] if m["role"] == "system"
                ),
            }
        )
        return await original(self, request)

    monkeypatch.setattr(model_module._GuardedTransport, "handle_async_request", measured)
    runner = CandidateToolRunner(
        model_factory=_wire_factory(permit, lambda _: _wire_response()),
        gateway_factory=lambda _: _small_gateway([], {"actor": True}, []),
        instruction=secret * (2500 if oversized else 1),
    )
    runtime = AssistantRuntime()
    conversation = runtime.create_conversation("actor", uuid4())
    with caplog.at_level("INFO", logger="uvicorn.error"), request_trace("wire-count-test"):
        if oversized:
            with pytest.raises(AssistantServiceError) as failure:
                _run_turn(runner, runtime, conversation)
            assert failure.value.reason == "context_limit"
        else:
            _run_turn(runner, runtime, conversation)
    trace = next(json.loads(r.args[0]) for r in caplog.records if r.msg == "assistant_timing %s")
    observed = trace["model_inputs"][0]
    assert all(observed[key] == value for key, value in expected[0].items())
    assert observed["reasoning_bytes"] == observed["tool_message_bytes"] == 0
    assert secret not in caplog.text
    assert len(permit.reservations) == (0 if oversized else 1)


def test_real_sdk_24_tool_capacity_preserves_current_thinking_and_results(monkeypatch):
    from app.composition import school_fact_pydanticai_model as model_module

    descriptors, instruction = _staff_tools_and_instruction()
    assert len(descriptors) == 24
    assert sum(d.load_on_demand for d in descriptors) == 14
    synthetic_thought = "仅为合成容量材料。" * 800
    expected_thought_sha = hashlib.sha256(synthetic_thought.encode()).digest()
    metrics = {"deferred": [], "eager": []}
    mode = "deferred"
    original_send = model_module._GuardedTransport.handle_async_request

    async def measured_send(self, request):
        body = json.loads(request.content)
        thoughts = [
            m["reasoning_content"]
            for m in body["messages"]
            if m.get("role") == "assistant" and m.get("reasoning_content")
        ]
        returns = [m for m in body["messages"] if m.get("role") == "tool"]
        if thoughts:
            assert hashlib.sha256(thoughts[-1].encode()).digest() == expected_thought_sha
            assert len(returns) == 1
            assert json.loads(returns[0]["content"]) == {"synthetic_value": 7}
        assert body["thinking"] == {"type": "enabled"}
        assert body["reasoning_effort"] == "high"
        metrics[mode].append(
            {
                "body_bytes": len(request.content),
                "tools_bytes": len(
                    json.dumps(body["tools"], ensure_ascii=False, separators=(",", ":")).encode()
                ),
                "messages_bytes": len(
                    json.dumps(body["messages"], ensure_ascii=False, separators=(",", ":")).encode()
                ),
                "tool_count": len(body["tools"]),
                "thinking_bytes": sum(len(text.encode()) for text in thoughts),
                "tool_return_count": len(returns),
            }
        )
        return await original_send(self, request)

    monkeypatch.setattr(model_module._GuardedTransport, "handle_async_request", measured_send)

    def run_case(selected_mode):
        events, calls, sends = [], [], []
        permit = _FakePermit(events)

        class Gateway:
            def __init__(self):
                self.descriptors = tuple(
                    d
                    if selected_mode == "deferred"
                    else d.model_copy(update={"load_on_demand": False})
                    for d in descriptors
                )

            def revalidate(self, _):
                events.append("current")

            def execute(self, call, _context):
                assert events[-1] == "current"
                assert call.name == "school_facts.directory.query"
                calls.append(call.name)
                return ToolExecutionResult(
                    model_content={"synthetic_value": 7}, history_check=HistoryCheck(lambda: True)
                )

        def handler(request):
            assert events[-1] == "current"
            events.append("send")
            sends.append(len(request.content))
            if len(sends) == 1:
                return _wire_response(
                    name="tool_2",
                    arguments={"fact_kind": "organization", "query_text": "合成教务处"},
                    thought=synthetic_thought,
                )
            return _wire_response()

        runner = CandidateToolRunner(
            model_factory=_wire_factory(permit, handler),
            gateway_factory=lambda _: Gateway(),
            instruction=instruction,
        )
        runtime = AssistantRuntime()
        conversation = runtime.create_conversation("actor", uuid4())
        if selected_mode == "eager":
            with pytest.raises(AssistantServiceError) as failure:
                _run_turn(runner, runtime, conversation)
            assert failure.value.reason == "context_limit"
            assert len(sends) == len(permit.reservations) == 1
        else:
            _run_turn(runner, runtime, conversation)
            assert len(sends) == len(permit.reservations) == 2
        assert calls == ["school_facts.directory.query"]

    for mode in ("deferred", "eager"):
        run_case(mode)
    assert metrics["deferred"][1]["body_bytes"] < 65536 < metrics["eager"][1]["body_bytes"]
    assert metrics["eager"][0]["body_bytes"] - metrics["deferred"][0]["body_bytes"] > 10000
    # Only synthetic byte/count measurements leave this test; no prompt or thinking is retained.
    print("SDK_TOOL_LOADING_BYTES=" + json.dumps(metrics, sort_keys=True))


@pytest.mark.parametrize(
    "failure_at,stage,reason",
    [
        ("gateway", "gateway_execute", "exception"),
        ("scope", "scope_exit", "exception"),
        ("type", "tool_result", "result_type"),
        ("size", "tool_result", "result_size"),
        ("duplicate", "consume_tool_call", "duplicate_id"),
        ("sdk", "sdk_protocol", "exception"),
    ],
)
def test_failure_metadata_is_bounded_and_never_logs_exception_or_call_content(
    caplog, failure_at, stage, reason
):
    from contextlib import contextmanager

    from app.platform.assistant.diagnostics import request_trace

    secret = "SYNTHETIC_SECRET_NEVER_LOG_ARGUMENT_CALL_ID_OR_EXCEPTION"

    class Gateway:
        descriptors = (
            ToolDescriptor(
                name="example.read",
                owner="example",
                kind="read",
                description="Read",
                parameters={"type": "object", "properties": {}},
            ),
        )
        executed = False

        @contextmanager
        def validation_scope(self):
            yield
            if failure_at == "scope" and self.executed:
                raise ValueError(secret)

        def revalidate(self, actor):
            pass

        def execute(self, call, context):
            self.executed = True
            if failure_at == "gateway":
                raise ValueError(secret)
            if failure_at == "type":
                return object()
            return ToolExecutionResult(
                model_content={"value": secret * 500 if failure_at == "size" else 1}
            )

    calls = []

    async def answer(messages, info):
        if failure_at == "sdk":
            raise ValueError(secret)
        calls.append(1)
        if len(calls) == 1 or failure_at == "duplicate":
            return ModelResponse(parts=[ToolCallPart("tool_0", {}, secret)])
        return ModelResponse(parts=[TextPart("synthetic")])

    gateway = Gateway()
    runner = CandidateToolRunner(
        model_factory=lambda _: FunctionModel(answer), gateway_factory=lambda _: gateway
    )
    runtime = AssistantRuntime()
    conversation = runtime.create_conversation("actor", uuid4())
    with caplog.at_level("INFO", logger="uvicorn.error"):
        with pytest.raises(AssistantRuntimeError), request_trace("synthetic-request"):
            _run_turn(runner, runtime, conversation)
    assert secret not in caplog.text
    payload = json.loads(
        next(
            r.getMessage().split("assistant_timing ", 1)[1]
            for r in caplog.records
            if r.getMessage().startswith("assistant_timing ")
        )
    )
    failure = payload["failure"]
    assert failure["stage"] == stage and failure["reason"] == reason
    assert failure.get("tool_alias") == (None if failure_at == "sdk" else "tool_0")
    assert failure["exception"] in {"ValueError", "AssistantRuntimeError"}
    assert len(json.dumps(failure)) < 256


def test_failure_metadata_unknown_types_and_outside_request_leave_no_state(caplog):
    from app.platform.assistant import diagnostics

    secret = "SYNTHETIC_SECRET_DYNAMIC_EXCEPTION_NAME"
    error_type = type(secret, (Exception,), {})
    error = error_type(secret)
    with pytest.raises(error_type) as caught:
        with diagnostics.failure_boundary("gateway_execute", tool_alias="tool_0"):
            raise error
    assert caught.value is error and diagnostics._active.get() is None
    with caplog.at_level("INFO", logger="uvicorn.error"):
        with pytest.raises(error_type), diagnostics.request_trace("synthetic-request"):
            with diagnostics.failure_boundary("sdk_protocol"):
                raise error
    assert secret not in caplog.text
    assert '"exception": "other"' in caplog.text and '"outcome": "other"' in caplog.text
    assert diagnostics._active.get() is None


@pytest.mark.parametrize("catalog_change", ["delete_first", "reorder"])
def test_real_sdk_changed_aliases_discard_native_material_and_keep_later_recovery_safe(
    catalog_change,
):
    events, calls, sent = [], [], [[], [], []]
    gateway = _small_gateway(events, {"actor": True}, calls)
    old = gateway.descriptors[0].model_copy(update={"name": "example.old"})
    current = old.model_copy(update={"name": "example.current"})
    gateway.descriptors = (old, current)
    execute = gateway.execute
    old_material = "SYNTHETIC_OLD_PRIVATE_REFERENCE"
    current_material = "SYNTHETIC_CURRENT_PRIVATE_REFERENCE"

    def material(call, context):
        output = execute(call, context)
        return ToolExecutionResult(
            model_content={
                "read_detail": {
                    "tool": call.name,
                    "reference": old_material if call.name == old.name else current_material,
                }
            },
            history_check=output.history_check,
        )

    gateway.execute = material
    permit = _FakePermit(events)
    turn_index = 0

    def handler(request):
        assert events[-1] == "current"
        events.append("send")
        body = json.loads(request.content)
        messages = body["messages"]
        encoded = json.dumps(messages)
        names = {tool["function"]["name"] for tool in body["tools"]}
        phase = len(sent[turn_index])
        sent[turn_index].append(body)
        if turn_index:
            assert old_material not in encoded
            assert "SYNTHETIC_OLD_NATIVE_THOUGHT" not in encoded
            assert "history_context_unavailable" in encoded
        if phase == 0:
            # Old loading state and native calls must not bind tool_0 to its old owner.
            assert names == {"search_tools"}
            assert not any(message.get("role") == "tool" for message in messages)
            assert not any(message.get("tool_calls") for message in messages)
            if turn_index:
                assert "quoted_historical_user_intents" in encoded
                assert "Synthetic query" in encoded
            if turn_index == 2:
                # The new result is reusable through quoted history; old material stays out.
                assert current_material in encoded
            return _wire_response(
                name="search_tools",
                arguments={"queries": ["tool_0"]},
                call_id=f"load-{turn_index}",
            )
        if phase == 1:
            assert "tool_0" in names
            return _wire_response(
                name="tool_0",
                call_id=f"read-{turn_index}",
                thought="SYNTHETIC_OLD_NATIVE_THOUGHT" if turn_index == 0 else "synthetic",
            )
        assert phase == 2
        assert (old_material if turn_index == 0 else current_material) in encoded
        return _wire_response()

    runner = CandidateToolRunner(
        model_factory=_wire_factory(permit, handler), gateway_factory=lambda _: gateway
    )
    runtime = AssistantRuntime()
    conversation = runtime.create_conversation("actor", uuid4())
    first = _run_turn(runner, runtime, conversation)
    assert first.display_ledger[-1].display_payload["history_retention"] == "retained"
    gateway.descriptors = (current,) if catalog_change == "delete_first" else (current, old)
    for turn_index in (1, 2):
        result = _run_turn(runner, runtime, conversation)
        assert result.display_ledger[-1].display_payload["history_retention"] == "quoted"
        assert len(sent[turn_index]) == 3
    assert [len(turn) for turn in sent] == [3, 3, 3]
    assert calls == [(old.name, "actor"), (current.name, "actor"), (current.name, "actor")]
    assert all(r.settled == 1 and r.unknown_count == 0 for r in permit.reservations)


def test_tool_outcome_metadata_only_retains_fixed_categories(caplog):
    import logging

    from app.platform.assistant import diagnostics

    secret = "do-not-log-tool-body-or-credentials"
    with (
        caplog.at_level(logging.INFO, logger="uvicorn.error"),
        diagnostics.request_trace("safe-outcomes"),
    ):
        diagnostics.record_tool_outcome("tool_17", status="rejected", code="invalid_arguments")
        diagnostics.record_tool_outcome("tool_17", status="rejected", code="owner_rejected")
        diagnostics.record_tool_outcome(
            "tool_17", status="target_room_candidates", stage="target_room"
        )
        diagnostics.record_tool_outcome(
            "tool_17", status="ambiguous", stage="creation_slot"
        )
        diagnostics.record_tool_outcome("tool_3", status=secret, code={secret: secret})
        diagnostics.record_tool_outcome("tool_3", status="incomplete", stage=secret)
        diagnostics.record_tool_outcome(secret, status="rejected", code=secret)
        for _ in range(40):
            diagnostics.record_tool_outcome("tool_0", status="denied", code=None)
    raw = next(r.message for r in caplog.records if r.message.startswith("assistant_timing "))
    assert secret not in raw
    payload = json.loads(raw.split(" ", 1)[1])
    outcomes = payload["tool_outcomes"]
    assert len(outcomes) == 32
    assert outcomes[:6] == [
        {"tool_alias": "tool_17", "status": "rejected", "code": "invalid_arguments"},
        {"tool_alias": "tool_17", "status": "rejected", "code": "owner_rejected"},
        {
            "tool_alias": "tool_17", "status": "target_room_candidates",
            "code": "unspecified", "stage": "target_room",
        },
        {
            "tool_alias": "tool_17", "status": "ambiguous",
            "code": "unspecified", "stage": "creation_slot",
        },
        {"tool_alias": "tool_3", "status": "other", "code": "other"},
        {"tool_alias": "tool_3", "status": "incomplete", "code": "unspecified", "stage": "other"},
    ]
    assert outcomes[-1] == {"tool_alias": "tool_0", "status": "denied", "code": "unspecified"}
