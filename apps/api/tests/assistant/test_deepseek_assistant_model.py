from __future__ import annotations

import importlib
import json
from datetime import UTC
from importlib.util import find_spec
from typing import cast

import httpx
import pytest
from pydantic import SecretStr

from app.platform.assistant.tool_loop import (
    ModelMessage,
    ModelRequest,
    ToolDescriptor,
)


def load_api():
    assert find_spec("app.composition.deepseek_assistant_model") is not None, (
        "missing paid-gated DeepSeek ToolModel adapter"
    )
    return importlib.import_module("app.composition.deepseek_assistant_model")


class Reservation:
    def __init__(self):
        self.usages = []
        self.unknown_count = 0

    def settle(self, usage):
        self.usages.append(usage)

    def unknown(self):
        self.unknown_count += 1


class Permit:
    def __init__(self):
        self.reservations = []
        self.limits = []

    def reserve(self, max_input_tokens, max_output_tokens):
        self.limits.append((max_input_tokens, max_output_tokens))
        reservation = Reservation()
        self.reservations.append(reservation)
        return reservation


def request(*, messages=None, names=("school.contacts.query",)):
    return ModelRequest(
        messages=messages
        or (
            ModelMessage(role="system", content="只使用获准事实。"),
            ModelMessage(role="user", content="查询主校区联系。"),
        ),
        tools=tuple(
            ToolDescriptor(
                name=name,
                owner="school.facts",
                kind="read",
                description="读取正式官方联系。",
                parameters={
                    "type": "object",
                    "properties": {"campus_name": {"type": "string"}},
                    "required": ["campus_name"],
                    "additionalProperties": False,
                },
            )
            for name in names
        ),
    )


def completion(*, calls=None, content="这是已发布的官方联系。"):
    return {
        "id": "completion.synthetic.1",
        "object": "chat.completion",
        "created": 1788580000,
        "model": "deepseek-v4-flash",
        "choices": [
            {
                "index": 0,
                "finish_reason": "tool_calls" if calls else "stop",
                "message": {
                    "role": "assistant",
                    "content": None if calls else content,
                    "reasoning_content": "  PRIVATE_SYNTHETIC_REASONING\n逐字续传🙂  ",
                    **({"tool_calls": calls} if calls else {}),
                },
            }
        ],
        "usage": {
            "prompt_tokens": 90,
            "completion_tokens": 20,
            "total_tokens": 110,
            "prompt_cache_hit_tokens": 10,
            "prompt_cache_miss_tokens": 80,
            "completion_tokens_details": {"reasoning_tokens": 15},
        },
    }


def wire_call(name, arguments='{"campus_name":"主校区"}'):
    return {
        "id": "call.synthetic.1",
        "type": "function",
        "function": {"name": name, "arguments": arguments},
    }


def model_with(handler, **kwargs):
    api = load_api()
    permit = Permit()
    model = api.DeepSeekAssistantModel(
        api_key=SecretStr("test-only-fake-deepseek-key"),
        model="deepseek-v4-flash",
        permit=permit,
        transport=httpx.MockTransport(handler),
        **kwargs,
    )
    return model, permit


def test_no_permit_never_makes_a_network_call():
    api = load_api()
    calls = []
    model = api.DeepSeekAssistantModel(
        api_key=SecretStr("test-only-fake-deepseek-key"),
        model="deepseek-v4-flash",
        transport=httpx.MockTransport(lambda req: calls.append(req) or httpx.Response(500)),
    )
    with pytest.raises(api.DeepSeekModelError, match="permit"):
        model.respond(request())
    assert calls == []


@pytest.mark.parametrize("selected_model", ["deepseek-v4-flash", "deepseek-flash"])
def test_wire_mapping_is_reversible_and_preserves_tool_calls_and_results(selected_model):
    observed = []

    def handler(req):
        payload = json.loads(req.content)
        observed.append(payload)
        assert str(req.url) == "https://api.deepseek.com/chat/completions"
        assert req.headers["authorization"] == "Bearer test-only-fake-deepseek-key"
        assert payload["thinking"] == {"type": "enabled"}
        assert payload["stream"] is False
        assert payload["max_tokens"] == 512
        assert payload["reasoning_effort"] == "high"
        if len(observed) == 1:
            names = [tool["function"]["name"] for tool in payload["tools"]]
            assert len(set(names)) == 2
            assert all("." not in name and len(name) <= 64 for name in names)
            result = completion(calls=[wire_call(names[0])])
            result["model"] = selected_model
            return httpx.Response(200, json=result)
        assistant = payload["messages"][-2]
        assert assistant["role"] == "assistant"
        assert assistant["reasoning_content"] == "  PRIVATE_SYNTHETIC_REASONING\n逐字续传🙂  "
        assert (
            assistant["tool_calls"][0]["function"]["name"]
            == (observed[0]["tools"][0]["function"]["name"])
        )
        assert json.loads(assistant["tool_calls"][0]["function"]["arguments"]) == {
            "campus_name": "主校区"
        }
        assert payload["messages"][-1] == {
            "role": "tool",
            "tool_call_id": "call.synthetic.1",
            "content": '{"status":"known","contact_value":"office@example.invalid"}',
        }
        result = completion()
        result["model"] = selected_model
        return httpx.Response(
            200, json=result, headers={"x-request-id": "wire-request.synthetic.2"}
        )

    if selected_model == "deepseek-flash":
        approval = current_approval()

        class CurrentReservation(Reservation):
            def mark_transmission_started(self):
                pass

        class CurrentPermit(Permit):
            current_approval = approval

            def reserve(self, max_input_tokens, max_output_tokens):
                self.limits.append((max_input_tokens, max_output_tokens))
                self.reservations.append(CurrentReservation())
                return self.reservations[-1]

        permit = CurrentPermit()
        model = load_api().DeepSeekAssistantModel(
            api_key=SecretStr("test-only-fake-deepseek-key"),
            model=selected_model,
            permit=permit,
            current_approval=approval,
            transport=httpx.MockTransport(handler),
            max_input_tokens=16384,
            max_output_tokens=512,
        )
    else:
        model, permit = model_with(handler, max_input_tokens=16384, max_output_tokens=512)
    first_request = request(names=("school.contacts.query", "school_contacts_query"))
    first = model.respond(first_request)
    assert first.calls[0].name == "school.contacts.query"
    second_request = first_request.model_copy(
        update={
            "messages": first_request.messages
            + (
                ModelMessage.from_reply(first),
                ModelMessage(
                    role="tool",
                    tool_call_id=first.calls[0].call_id,
                    content='{"status":"known","contact_value":"office@example.invalid"}',
                ),
            )
        }
    )
    second = model.respond(second_request)
    assert second.text == "这是已发布的官方联系。"
    assert second.calls == ()
    assert permit.limits == [(16384, 512), (16384, 512)]
    usage = permit.reservations[-1].usages[0]
    assert usage.model == selected_model
    assert usage.request_id == "wire-request.synthetic.2"
    assert usage.completion_id == "completion.synthetic.1"
    assert (usage.prompt_tokens, usage.completion_tokens, usage.total_tokens) == (90, 20, 110)
    assert (usage.prompt_cache_hit_tokens, usage.prompt_cache_miss_tokens) == (10, 80)
    assert usage.reasoning_tokens == 15
    assert (
        "PRIVATE_SYNTHETIC_REASONING"
        not in repr(first)
        + first.model_dump_json()
        + repr(second_request)
        + second_request.model_dump_json()
    )
    assert all(item.unknown_count == 0 for item in permit.reservations)
    assert "reasoning_content" not in second.model_dump_json()


@pytest.mark.parametrize(
    "mutation",
    [
        "extra_envelope",
        "extra_function",
        "invalid_reasoning",
        "missing_reasoning",
        "bad_reasoning_usage",
        "bad_arguments",
        "extra_argument",
        "duplicate_arguments",
        "unknown_tool",
        "bad_usage",
        "duplicate_envelope",
    ],
)
def test_bad_wire_data_is_rejected_without_exposing_private_response(mutation, caplog):
    api = load_api()

    def handler(req):
        name = json.loads(req.content)["tools"][0]["function"]["name"]
        payload = completion(calls=[wire_call(name)])
        message = payload["choices"][0]["message"]
        if mutation == "extra_envelope":
            payload["unapproved"] = "PRIVATE_WIRE_VALUE"
        elif mutation == "extra_function":
            message["tool_calls"][0]["function"]["extra"] = "PRIVATE_WIRE_VALUE"
        elif mutation == "invalid_reasoning":
            message["reasoning_content"] = {"bad": "PRIVATE_WIRE_VALUE"}
        elif mutation == "missing_reasoning":
            message.pop("reasoning_content")
        elif mutation == "bad_reasoning_usage":
            payload["usage"]["completion_tokens_details"]["reasoning_tokens"] = 21
        elif mutation == "bad_arguments":
            message["tool_calls"][0]["function"]["arguments"] = '["PRIVATE_WIRE_VALUE"]'
        elif mutation == "extra_argument":
            message["tool_calls"][0]["function"]["arguments"] = (
                '{"campus_name":"主校区","school_id":"PRIVATE_WIRE_VALUE"}'
            )
        elif mutation == "duplicate_arguments":
            message["tool_calls"][0]["function"]["arguments"] = (
                '{"campus_name":"主校区","campus_name":"PRIVATE_WIRE_VALUE"}'
            )
        elif mutation == "unknown_tool":
            message["tool_calls"][0]["function"]["name"] = "PRIVATE_WIRE_VALUE"
        elif mutation == "bad_usage":
            payload["usage"]["total_tokens"] = 999
        elif mutation == "duplicate_envelope":
            raw = json.dumps(payload)
            return httpx.Response(200, content=raw[:-1] + ',"id":"PRIVATE_WIRE_VALUE"}')
        return httpx.Response(200, json=payload)

    model, permit = model_with(handler)
    with pytest.raises(api.DeepSeekModelError) as error:
        model.respond(request())
    assert "PRIVATE_WIRE_VALUE" not in str(error.value)
    assert "test-only-fake-deepseek-key" not in str(error.value)
    assert "PRIVATE_WIRE_VALUE" not in caplog.text
    reservation = permit.reservations[0]
    assert bool(reservation.usages) != bool(reservation.unknown_count)


def test_timeout_is_not_retried_and_unknown_keeps_the_reservation(caplog):
    api = load_api()
    calls = []

    def handler(req):
        calls.append(req)
        raise httpx.ReadTimeout("PRIVATE_TIMEOUT_AND_KEY", request=req)

    model, permit = model_with(handler)
    with pytest.raises(api.DeepSeekModelError) as error:
        model.respond(request())
    assert len(calls) == 1
    assert calls[0].extensions["timeout"]["read"] <= 60
    assert permit.reservations[0].unknown_count == 1
    assert permit.reservations[0].usages == []
    assert "PRIVATE_TIMEOUT_AND_KEY" not in str(error.value) + caplog.text


def test_input_bounds_and_invalid_history_fail_before_reserving_or_sending():
    api = load_api()
    calls = []
    model, permit = model_with(
        lambda req: calls.append(req) or httpx.Response(500), max_input_tokens=1024
    )
    oversized = request(messages=(ModelMessage(role="user", content="大" * 1000),))
    with pytest.raises(api.DeepSeekModelError):
        model.respond(oversized)
    orphaned = request(
        messages=(ModelMessage(role="tool", content="{}", tool_call_id="never-issued"),)
    )
    with pytest.raises(api.DeepSeekModelError):
        model.respond(orphaned)
    assert permit.reservations == []
    assert calls == []


def test_oversized_or_http_error_response_does_not_release_unknown_spend():
    api = load_api()
    for status, body in [(200, b"x" * 200_000), (503, b"PRIVATE_SERVICE_RESPONSE")]:
        model, permit = model_with(
            lambda _, status=status, body=body: httpx.Response(status, content=body)
        )
        with pytest.raises(api.DeepSeekModelError):
            model.respond(request())
        assert permit.reservations[0].unknown_count == 1
        assert permit.reservations[0].usages == []


def test_truncated_reply_settles_reported_usage_but_is_not_accepted():
    api = load_api()
    payload = completion()
    payload["choices"][0]["finish_reason"] = "length"
    model, permit = model_with(lambda _: httpx.Response(200, json=payload))
    with pytest.raises(api.DeepSeekModelError):
        model.respond(request())
    assert len(permit.reservations[0].usages) == 1
    assert permit.reservations[0].unknown_count == 0


def test_zero_argument_closed_tool_schema_needs_no_properties_dictionary():
    api = load_api()
    descriptor = ToolDescriptor(
        name="school.status",
        owner="school.facts",
        kind="read",
        description="读取当前工程状态。",
        parameters={"type": "object", "additionalProperties": False},
    )

    def handler(req):
        name = json.loads(req.content)["tools"][0]["function"]["name"]
        return httpx.Response(200, json=completion(calls=[wire_call(name, "{}")]))

    model, _ = model_with(handler)
    result = model.respond(
        ModelRequest(
            messages=(ModelMessage(role="user", content="当前状态是什么？"),),
            tools=(descriptor,),
        )
    )
    assert isinstance(result, api.ModelReply)
    assert result.calls[0].arguments == {}


@pytest.mark.parametrize("mutation", ["wrong_model", "over_input", "over_output"])
def test_unapproved_model_or_usage_keeps_unknown_reservation(mutation):
    api = load_api()
    payload = completion()
    if mutation == "wrong_model":
        payload["model"] = "deepseek-v4-pro"
    elif mutation == "over_input":
        payload["usage"].update(
            prompt_tokens=40000,
            total_tokens=40020,
            prompt_cache_hit_tokens=0,
            prompt_cache_miss_tokens=40000,
        )
    else:
        payload["usage"].update(completion_tokens=40000, total_tokens=40090)
    model, permit = model_with(lambda _: httpx.Response(200, json=payload))
    with pytest.raises(api.DeepSeekModelError):
        model.respond(request())
    assert permit.reservations[0].usages == []
    assert permit.reservations[0].unknown_count == 1


def test_assistant_without_complete_private_continuation_fails_before_spending():
    api = load_api()
    calls = []
    model, permit = model_with(lambda req: calls.append(req) or httpx.Response(500))
    history = request(
        messages=(
            ModelMessage(role="user", content="hello"),
            ModelMessage(role="assistant", content="public reply"),
            ModelMessage(role="user", content="next"),
        )
    )
    with pytest.raises(api.DeepSeekModelError):
        model.respond(history)
    assert calls == [] and permit.reservations == []


def test_continuation_cannot_be_attached_to_a_changed_assistant_message():
    api = load_api()
    model, permit = model_with(
        lambda req: httpx.Response(
            200,
            json=completion(
                calls=[wire_call(json.loads(req.content)["tools"][0]["function"]["name"])]
            ),
        )
    )
    original = request()
    reply = model.respond(original)
    changed = ModelMessage.from_reply(reply).model_copy(
        update={"content": "changed public content"}
    )
    history = original.model_copy(
        update={
            "messages": original.messages
            + (
                changed,
                ModelMessage(role="tool", tool_call_id=reply.calls[0].call_id, content="{}"),
            )
        }
    )
    with pytest.raises(api.DeepSeekModelError):
        model.respond(history)
    assert len(permit.reservations) == 1


def test_wire_reasoning_validation_and_public_error_have_no_raw_exception_chain(caplog):
    api = load_api()
    from pydantic import ValidationError

    payload = completion()
    payload["choices"][0]["message"]["reasoning_content"] = {
        "private": "PRIVATE_VALIDATION_REASONING"
    }
    with pytest.raises(ValidationError) as raw:
        api._Completion.model_validate(payload)
    assert "PRIVATE_VALIDATION_REASONING" not in str(raw.value) + repr(raw.value)
    model, _ = model_with(lambda _: httpx.Response(200, json=payload))
    with pytest.raises(api.DeepSeekModelError) as sanitized:
        model.respond(request())
    assert sanitized.value.__context__ is None
    assert "PRIVATE_VALIDATION_REASONING" not in str(sanitized.value) + caplog.text


def test_real_loop_resends_all_private_subcall_reasoning_then_discards_it_for_next_turn():
    from types import SimpleNamespace
    from uuid import uuid4

    from app.platform.assistant.runtime import AssistantRuntime
    from app.platform.assistant.tool_loop import (
        AssistantToolLoop,
        ToolExecutionResult,
        ToolGateway,
        ToolModel,
    )

    observed = []
    replies = []

    def handler(req):
        wire = json.loads(req.content)
        observed.append(wire)
        index = len(observed)
        assistant = [m for m in wire["messages"] if m["role"] == "assistant"]
        if index in (1, 4):
            assert assistant == []
            assert all(m["role"] != "tool" for m in wire["messages"])
        else:
            assert [m["reasoning_content"] for m in assistant] == [
                f"PRIVATE_SUBCALL_{n}\n原样保留" for n in range(1, index)
            ]
        if index <= 2:
            call = wire_call(wire["tools"][0]["function"]["name"])
            call["id"] = f"subcall.{index}"
            payload = completion(calls=[call])
        else:
            payload = completion(content="已核对当前事实")
        payload["choices"][0]["message"]["reasoning_content"] = f"PRIVATE_SUBCALL_{index}\n原样保留"
        return httpx.Response(200, json=payload)

    adapter, permit = model_with(handler)

    def respond(req):
        reply = adapter.respond(req)
        replies.append(reply)
        return reply

    gateway = SimpleNamespace(
        descriptors=request().tools,
        revalidate=lambda _: None,
        execute=lambda *_: ToolExecutionResult(
            model_content={"status": "known", "value": "CURRENT_AUTHORIZED_VALUE"}
        ),
    )
    runtime = AssistantRuntime()
    conversation = runtime.create_conversation("private-actor", uuid4())
    accepted = runtime.accept_turn(
        conversation.conversation_ref, "private-actor", uuid4(), "读取事实"
    )
    loop = AssistantToolLoop(
        model=cast(ToolModel, SimpleNamespace(respond=respond)),
        gateway_factory=lambda _: cast(ToolGateway, gateway),
    )
    result = loop.run_accepted(
        runtime=runtime, accepted=accepted, actor_context_ref="private-actor", request_id=uuid4()
    )
    assert all(reply.private_continuation.get_payload() is None for reply in replies)
    assert "PRIVATE_SUBCALL" not in str(result)
    followup = runtime.accept_turn(
        conversation.conversation_ref, "private-actor", uuid4(), "再核对"
    )
    loop.run_accepted(
        runtime=runtime, accepted=followup, actor_context_ref="private-actor", request_id=uuid4()
    )
    assert len(observed) == 4
    assert "CURRENT_AUTHORIZED_VALUE" not in json.dumps(observed[3])
    assert "PRIVATE_SUBCALL" not in json.dumps(observed[3])
    assert all(reply.private_continuation.get_payload() is None for reply in replies)
    assert len(permit.reservations) == 4 and all(
        len(r.usages) == 1 and r.unknown_count == 0 for r in permit.reservations
    )


@pytest.mark.parametrize("metadata", ["index", "prompt_details", "both"])
def test_observed_tool_index_and_prompt_details_are_accepted_without_duplicate_usage(metadata):
    def handler(req):
        name = json.loads(req.content)["tools"][0]["function"]["name"]
        calls = [wire_call(name), {**wire_call(name), "id": "call.synthetic.2"}]
        if metadata in ("index", "both"):
            for index, call in enumerate(calls):
                call["index"] = index
        payload = completion(calls=calls)
        if metadata in ("prompt_details", "both"):
            payload["usage"]["prompt_tokens_details"] = {"cached_tokens": 10, "text_tokens": 90}
        return httpx.Response(200, json=payload)

    model, permit = model_with(handler)
    result = model.respond(request())
    assert [call.call_id for call in result.calls] == ["call.synthetic.1", "call.synthetic.2"]
    assert "index" not in result.model_dump_json()
    reservation = permit.reservations[0]
    assert len(reservation.usages) == 1 and reservation.unknown_count == 0
    usage = reservation.usages[0]
    assert (usage.prompt_tokens, usage.completion_tokens, usage.total_tokens) == (90, 20, 110)
    assert (usage.prompt_cache_hit_tokens, usage.prompt_cache_miss_tokens) == (10, 80)
    assert usage.reasoning_tokens == 15


@pytest.mark.parametrize("index", [-1, True, "0", 0.0, None, 1, 8])
def test_invalid_tool_index_preserves_unknown_reservation(index):
    api = load_api()
    payload = completion(calls=[{**wire_call("tool_0"), "index": index}])
    model, permit = model_with(lambda _: httpx.Response(200, json=payload))
    with pytest.raises(api.DeepSeekModelError) as error:
        model.respond(request())
    assert error.value.__context__ is None
    assert permit.reservations[0].usages == []
    assert permit.reservations[0].unknown_count == 1


@pytest.mark.parametrize(
    "details",
    [
        {"cached_tokens": 11},
        {"cached_tokens": 91},
        {"cached_tokens": True},
        {"cached_tokens": "10"},
        {"cached_tokens": 10.0},
        {"cached_tokens": None},
        {"cached_tokens": -1},
        {"nested": {"cached_tokens": 10}},
        {"value": 131073},
        {"x" * 65: 1},
        {f"counter_{n}": n for n in range(17)},
        [],
    ],
)
def test_invalid_prompt_details_preserve_unknown_reservation(details):
    api = load_api()
    payload = completion()
    payload["usage"]["prompt_tokens_details"] = details
    model, permit = model_with(lambda _: httpx.Response(200, json=payload))
    with pytest.raises(api.DeepSeekModelError) as error:
        model.respond(request())
    assert error.value.__context__ is None
    assert permit.reservations[0].usages == []
    assert permit.reservations[0].unknown_count == 1


def test_prompt_cached_details_require_corresponding_top_level_cache_count():
    api = load_api()
    payload = completion()
    payload["usage"].pop("prompt_cache_hit_tokens")
    payload["usage"].pop("prompt_cache_miss_tokens")
    payload["usage"]["prompt_tokens_details"] = {"cached_tokens": 10}
    model, permit = model_with(lambda _: httpx.Response(200, json=payload))
    with pytest.raises(api.DeepSeekModelError):
        model.respond(request())
    assert permit.reservations[0].usages == []
    assert permit.reservations[0].unknown_count == 1


def test_provider_reports_private_wire_bytes_and_native_history_preserves_actual_reasoning():
    from types import SimpleNamespace
    from uuid import uuid4

    from app.platform.assistant.runtime import AssistantRuntime
    from app.platform.assistant.tool_loop import AssistantToolLoop, ToolGateway

    observed = []
    text = "可以继续读取获准目录。"
    reasoning = '  实际思考\n原始引号"与反斜杠\\🙂  '

    def handler(req):
        payload = json.loads(req.content)
        observed.append(payload)
        response = completion(content=text if len(observed) == 1 else "已理解当前请求。")
        response["choices"][0]["message"]["reasoning_content"] = reasoning
        if len(observed) == 2:
            native = payload["messages"][-2]
            assert native["role"] == "assistant" and native["content"] == text
            assert native["reasoning_content"] == reasoning
            assert "tool_calls" not in native
            assert payload["messages"][-1] == {"role": "user", "content": "继续"}
        return httpx.Response(200, json=response)

    model, permit = model_with(handler, max_input_tokens=65536)
    replies = []

    class ObservedModel:
        def respond(self, request):
            reply = model.respond(request)
            replies.append(reply)
            continuation = reply.private_continuation
            assert continuation.retention_bytes >= len(
                json.dumps(reasoning, ensure_ascii=False).encode("utf-8")
            )
            return reply

    runtime = AssistantRuntime()
    conversation = runtime.create_conversation("actor-private", uuid4())
    tools = SimpleNamespace(descriptors=request().tools, revalidate=lambda _: None)
    loop = AssistantToolLoop(
        model=ObservedModel(), gateway_factory=lambda _: cast(ToolGateway, tools)
    )
    for user in ["原始问题", "继续"]:
        accepted = runtime.accept_turn(
            conversation.conversation_ref, "actor-private", uuid4(), user
        )
        loop.run_accepted(
            runtime=runtime,
            accepted=accepted,
            actor_context_ref="actor-private",
            request_id=uuid4(),
        )
    assert len(observed) == 2 and len(permit.reservations) == 2
    assert all(reply.private_continuation.get_payload() is None for reply in replies)


@pytest.mark.parametrize("invalidated", ["expiry", "stop", "authorization", "deadline"])
def test_reservation_delay_cannot_transmit_invalidated_native_context(monkeypatch, invalidated):
    from datetime import UTC, datetime, timedelta
    from types import SimpleNamespace
    from uuid import uuid4

    from app.platform.assistant import tool_loop
    from app.platform.assistant.runtime import AssistantRuntime, AssistantRuntimeError
    from app.platform.assistant.tool_loop import AssistantToolLoop, ToolGateway

    observed, reservations = [], []
    now = [datetime(2026, 9, 6, tzinfo=UTC)]
    elapsed = [0.0]
    monkeypatch.setattr(tool_loop, "monotonic", lambda: elapsed[0])
    runtime = AssistantRuntime(clock=lambda: now[0])
    conversation = runtime.create_conversation("actor-private", uuid4())
    current = []
    allowed = [True]

    def authorize(_):
        if not allowed[0]:
            raise PermissionError("private authority detail")

    class DelayedPermit:
        def reserve(self, max_input_tokens, max_output_tokens):
            reservation = Reservation()
            reservations.append(reservation)
            if observed:
                if invalidated == "expiry":
                    now[0] += timedelta(seconds=2)
                    runtime.purge_expired()
                elif invalidated == "stop":
                    accepted = current[0]
                    runtime.stop_run(
                        accepted.conversation_ref,
                        "actor-private",
                        accepted.run_ref,
                        accepted.generation,
                        uuid4(),
                    )
                elif invalidated == "authorization":
                    allowed[0] = False
                else:
                    elapsed[0] += 181
            return reservation

    def handler(req):
        observed.append(json.loads(req.content))
        return httpx.Response(200, json=completion(content="可以继续读取获准目录。"))

    api = load_api()
    model = api.DeepSeekAssistantModel(
        api_key=SecretStr("fake-test-only"),
        model="deepseek-v4-flash",
        permit=DelayedPermit(),
        transport=httpx.MockTransport(handler),
        max_input_tokens=65536,
    )
    tools = SimpleNamespace(descriptors=request().tools, revalidate=authorize)
    loop = AssistantToolLoop(model=model, gateway_factory=lambda _: cast(ToolGateway, tools))
    first = runtime.accept_turn(conversation.conversation_ref, "actor-private", uuid4(), "原问题")
    loop.run_accepted(
        runtime=runtime, accepted=first, actor_context_ref="actor-private", request_id=uuid4()
    )
    now[0] += timedelta(hours=24) - timedelta(seconds=1)
    second = runtime.accept_turn(conversation.conversation_ref, "actor-private", uuid4(), "继续")
    current.append(second)
    with pytest.raises(AssistantRuntimeError):
        loop.run_accepted(
            runtime=runtime, accepted=second, actor_context_ref="actor-private", request_id=uuid4()
        )
    assert len(observed) == 1, "invalidated already-encoded private history reached HTTP"
    assert len(reservations) == 2
    assert reservations[1].usages == [] and reservations[1].unknown_count == 1


def test_transmission_guard_is_private_nonpersistent_and_discarded_after_model_return():
    import pickle

    from app.platform.assistant.tool_loop import ModelRequest

    checked = []
    guarded = request().with_transmission_check(lambda: checked.append("PRIVATE_AUTHORITY"))
    assert "PRIVATE_AUTHORITY" not in repr(guarded) + guarded.model_dump_json()
    assert "transmission" not in guarded.model_dump()
    with pytest.raises(TypeError, match="cannot be persisted"):
        pickle.dumps(guarded)
    guarded.require_current_transmission()
    assert checked == ["PRIVATE_AUTHORITY"]
    guarded.discard_transmission_check()
    with pytest.raises(Exception, match="transmission"):
        guarded.require_current_transmission()
    # A freshly decoded public envelope never acquires the private callback.
    public = ModelRequest.model_validate_json(guarded.model_dump_json())
    public.require_current_transmission()
    assert checked == ["PRIVATE_AUTHORITY"]


def test_http_client_setup_delay_is_rechecked_before_stream_and_keeps_only_reservation(monkeypatch):
    api = load_api()
    calls = []
    allowed = [True]

    def current():
        if not allowed[0]:
            raise RuntimeError("private authorization changed")

    class DelayedClient(httpx.Client):
        def __enter__(self):
            result = super().__enter__()
            allowed[0] = False
            return result

    monkeypatch.setattr(api.httpx, "Client", DelayedClient)
    model, permit = model_with(
        lambda req: calls.append(req) or httpx.Response(200, json=completion())
    )
    with pytest.raises(api.DeepSeekModelError, match="not sent; budget reservation retained"):
        model.respond(request().with_transmission_check(current))
    assert calls == []
    assert len(permit.reservations) == 1
    assert permit.reservations[0].usages == [] and permit.reservations[0].unknown_count == 1


@pytest.mark.parametrize("change", ["deadline", "stop"])
def test_transmission_authorization_delay_rechecks_run_and_deadline_after_return(
    monkeypatch, change
):
    from types import SimpleNamespace
    from uuid import uuid4

    from app.platform.assistant import tool_loop
    from app.platform.assistant.runtime import AssistantRuntime, AssistantRuntimeError
    from app.platform.assistant.tool_loop import AssistantToolLoop, ToolGateway

    elapsed, client_open = [0.0], [False]
    monkeypatch.setattr(tool_loop, "monotonic", lambda: elapsed[0])
    runtime = AssistantRuntime()
    conversation = runtime.create_conversation("actor-private", uuid4())
    accepted = runtime.accept_turn(
        conversation.conversation_ref, "actor-private", uuid4(), "当前请求"
    )

    class DelayedClient(httpx.Client):
        def __enter__(self):
            result = super().__enter__()
            client_open[0] = True
            return result

    api = load_api()
    monkeypatch.setattr(api.httpx, "Client", DelayedClient)

    def authorize(_):
        if client_open[0]:
            if change == "deadline":
                elapsed[0] += 181
            else:
                runtime.stop_run(
                    accepted.conversation_ref,
                    "actor-private",
                    accepted.run_ref,
                    accepted.generation,
                    uuid4(),
                )

    calls = []
    model, permit = model_with(
        lambda req: calls.append(req) or httpx.Response(200, json=completion()),
        max_input_tokens=65536,
    )
    tools = SimpleNamespace(descriptors=request().tools, revalidate=authorize)
    loop = AssistantToolLoop(model=model, gateway_factory=lambda _: cast(ToolGateway, tools))
    with pytest.raises(AssistantRuntimeError):
        loop.run_accepted(
            runtime=runtime,
            accepted=accepted,
            actor_context_ref="actor-private",
            request_id=uuid4(),
        )
    assert calls == []
    assert permit.reservations[0].usages == [] and permit.reservations[0].unknown_count == 1


def test_private_encoding_does_not_repeat_ready_transport_authority_check(monkeypatch):
    api = load_api()
    stages, sent = [], []
    phase = ["before_reservation"]
    elapsed = [0.0]

    class ObservedPermit(Permit):
        def reserve(self, *args):
            phase[0] = "reserved_transport_not_ready"
            return super().reserve(*args)

    class ObservedClient(httpx.Client):
        def __enter__(self):
            result = super().__enter__()
            phase[0] = "transport_ready"
            return result

    def current():
        stages.append(phase[0])
        elapsed[0] += 4.0
        if elapsed[0] > 10:
            raise RuntimeError("bounded authority IO deadline")

    monkeypatch.setattr(api.httpx, "Client", ObservedClient)
    permit = ObservedPermit()
    model = api.DeepSeekAssistantModel(
        api_key=SecretStr("test-only-fake"),
        model="deepseek-v4-flash",
        permit=permit,
        transport=httpx.MockTransport(
            lambda req: sent.append(req) or httpx.Response(200, json=completion())
        ),
    )
    result = model.respond(request().with_transmission_check(current))
    assert result.text
    assert stages == ["transport_ready"]
    assert len(sent) == 1
    assert len(permit.reservations[0].usages) == 1
    assert permit.reservations[0].unknown_count == 0


def test_oversized_tool_batch_is_bounded_but_reliable_usage_is_settled():
    api = load_api()
    calls = [{**wire_call("tool_0"), "id": f"call-{n}"} for n in range(12)]
    model, permit = model_with(lambda req: httpx.Response(200, json=completion(calls=calls)))
    with pytest.raises(api.DeepSeekModelError) as exc:
        model.respond(request())
    assert exc.value.reason == "tool_limit"
    assert len(permit.reservations[0].usages) == 1
    assert permit.reservations[0].unknown_count == 0


def test_pro_requires_matching_explicit_candidate_permit_and_records_actual_send():
    from datetime import datetime, timedelta

    api = load_api()
    approval = api.CandidateModelApproval(
        experiment_id="synthetic-pro-once",
        approval_reference="user-approved-finite-candidate",
        model="deepseek-v4-pro",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    events = []

    class CandidateReservation(Reservation):
        def mark_transmission_started(self):
            events.append("send_started")

    class CandidatePermit(Permit):
        candidate_approval = approval

        def reserve(self, max_input_tokens, max_output_tokens):
            self.reservations.append(CandidateReservation())
            return self.reservations[-1]

    def handler(req):
        payload = json.loads(req.content)
        assert events == ["send_started"]
        assert payload["model"] == "deepseek-v4-pro"
        assert payload["thinking"] == {"type": "enabled"}
        assert payload["reasoning_effort"] == "high"
        result = completion()
        result["model"] = "deepseek-v4-pro"
        events.append("http")
        return httpx.Response(200, json=result)

    common = dict(api_key=SecretStr("synthetic-key"), model="deepseek-v4-pro")
    with pytest.raises(api.DeepSeekModelError, match="selection"):
        api.DeepSeekAssistantModel(**common, permit=CandidatePermit())
    with pytest.raises(api.DeepSeekModelError, match="selection"):
        api.DeepSeekAssistantModel(
            api_key=SecretStr("synthetic-key"),
            model="deepseek-v4-flash",
            permit=CandidatePermit(),
        )
    with pytest.raises(api.DeepSeekModelError, match="selection"):
        api.DeepSeekAssistantModel(**common, permit=Permit(), candidate_approval=approval)
    permit = CandidatePermit()
    model = api.DeepSeekAssistantModel(
        **common,
        permit=permit,
        candidate_approval=approval,
        transport=httpx.MockTransport(handler),
    )
    assert model.respond(request()).text
    assert events == ["send_started", "http"]
    assert permit.reservations[0].usages[0].model == "deepseek-v4-pro"


def test_candidate_requires_send_marker_and_cannot_admit_another_model():
    from datetime import datetime, timedelta

    api = load_api()
    approval = api.CandidateModelApproval(
        experiment_id="synthetic-pro-once",
        approval_reference="user-approved-finite-candidate",
        model="deepseek-v4-pro",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )

    class CandidatePermit(Permit):
        candidate_approval = approval

    called = []
    permit = CandidatePermit()
    model = api.DeepSeekAssistantModel(
        api_key=SecretStr("synthetic-key"),
        model="deepseek-v4-pro",
        permit=permit,
        candidate_approval=approval,
        transport=httpx.MockTransport(lambda req: called.append(req) or httpx.Response(500)),
    )
    with pytest.raises(api.DeepSeekModelError):
        model.respond(request())
    assert not called
    assert permit.reservations[0].unknown_count == 1
    with pytest.raises(api.DeepSeekModelError, match="selection"):
        api.DeepSeekAssistantModel(
            api_key=SecretStr("synthetic-key"),
            model="unapproved-model",
            permit=permit,
            candidate_approval=approval,
        )


def test_candidate_accounting_wait_cannot_transmit_after_authority_changes():
    from datetime import UTC, datetime, timedelta

    api = load_api()
    approval = api.CandidateModelApproval(
        experiment_id="synthetic-pro-once",
        approval_reference="approved-experiment",
        model="deepseek-v4-pro",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    allowed, called = [True], []

    class CandidateReservation(Reservation):
        def mark_transmission_started(self):
            allowed[0] = False

    class CandidatePermit(Permit):
        candidate_approval = approval

        def reserve(self, max_input_tokens, max_output_tokens):
            self.reservations.append(CandidateReservation())
            return self.reservations[-1]

    def current():
        if not allowed[0]:
            raise RuntimeError("synthetic revocation during metadata write")

    permit = CandidatePermit()
    model = api.DeepSeekAssistantModel(
        api_key=SecretStr("synthetic-key"),
        model="deepseek-v4-pro",
        permit=permit,
        candidate_approval=approval,
        transport=httpx.MockTransport(lambda req: called.append(True) or httpx.Response(500)),
    )
    with pytest.raises(api.DeepSeekModelError):
        model.respond(request().with_transmission_check(current))
    assert called == []
    assert permit.reservations[0].unknown_count == 1


def test_candidate_approval_expiring_during_accounting_prevents_http(monkeypatch):
    from datetime import datetime, timedelta

    api = load_api()
    expires = datetime.now(UTC) + timedelta(hours=1)
    approval = api.CandidateModelApproval(
        experiment_id="synthetic-pro-once",
        approval_reference="approved-experiment",
        model="deepseek-v4-pro",
        expires_at=expires,
    )

    class ExpiredClock:
        @staticmethod
        def now(_timezone):
            return expires

    class CandidateReservation(Reservation):
        def mark_transmission_started(self):
            monkeypatch.setattr(api, "datetime", ExpiredClock)

    class CandidatePermit(Permit):
        candidate_approval = approval

        def reserve(self, max_input_tokens, max_output_tokens):
            self.reservations.append(CandidateReservation())
            return self.reservations[-1]

    called, permit = [], CandidatePermit()
    model = api.DeepSeekAssistantModel(
        api_key=SecretStr("synthetic-key"),
        model="deepseek-v4-pro",
        permit=permit,
        candidate_approval=approval,
        transport=httpx.MockTransport(lambda req: called.append(True) or httpx.Response(500)),
    )
    with pytest.raises(api.DeepSeekModelError):
        model.respond(request())
    assert not called
    assert permit.reservations[0].unknown_count == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("model", "deepseek-v4-flash"),
        ("approval_reference", ""),
        ("approval_reference", "   "),
        ("approval_reference", "x" * 201),
        ("price_verified_on", "2026-02-30"),
        ("price_verified_on", "20260912"),
        ("price_verified_on", None),
    ],
)
def test_current_approval_rejects_invalid_contract(field, value):
    approval_type = getattr(load_api(), "CurrentModelApproval", None)
    assert approval_type is not None
    values = dict(
        model="deepseek-flash",
        approval_reference="approved-current",
        price_verified_on="2026-09-12",
    )
    with pytest.raises(ValueError):
        approval_type(**{**values, field: value})


def current_approval():
    approval_type = getattr(load_api(), "CurrentModelApproval", None)
    assert approval_type is not None
    return approval_type(
        model="deepseek-flash",
        approval_reference="approved-current",
        price_verified_on="2026-09-12",
    )


@pytest.mark.parametrize("case", ["missing", "old_permit", "different", "old_model", "candidate"])
def test_current_model_requires_own_exact_approval_and_permit(case):
    from dataclasses import replace
    from datetime import datetime, timedelta

    api = load_api()
    approval = current_approval()
    permit = Permit()
    permit.current_approval = approval
    kwargs = dict(
        api_key=SecretStr("unit-key"),
        model="deepseek-flash",
        permit=permit,
        current_approval=approval,
    )
    if case == "missing":
        kwargs.pop("current_approval")
    elif case == "old_permit":
        kwargs["permit"] = Permit()
    elif case == "different":
        permit.current_approval = replace(approval, approval_reference="another")
    elif case == "old_model":
        kwargs["model"] = "deepseek-v4-flash"
    else:
        kwargs["candidate_approval"] = api.CandidateModelApproval(
            experiment_id="test",
            approval_reference="test",
            model="deepseek-v4-pro",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
    with pytest.raises(api.DeepSeekModelError, match="selection"):
        api.DeepSeekAssistantModel(**kwargs)
    assert permit.reservations == []


@pytest.mark.parametrize(
    "response_model", ["deepseek-flash", "deepseek-flash-extra", "deepseek-v4-flash"]
)
def test_current_model_sends_exact_identity_and_requires_exact_response(response_model):
    api = load_api()
    approval = current_approval()
    events = []

    class CurrentReservation(Reservation):
        def mark_transmission_started(self):
            events.append("marker")

    class CurrentPermit(Permit):
        current_approval = approval

        def reserve(self, max_input_tokens, max_output_tokens):
            self.reservations.append(CurrentReservation())
            return self.reservations[-1]

    def handler(req):
        payload = json.loads(req.content)
        assert payload["model"] == "deepseek-flash"
        assert payload["thinking"] == {"type": "enabled"}
        assert payload["reasoning_effort"] == "high"
        assert payload["tools"] and payload["tool_choice"] == "auto"
        assert events == ["marker"]
        result = completion()
        result["model"] = response_model
        return httpx.Response(200, json=result)

    permit = CurrentPermit()
    model = api.DeepSeekAssistantModel(
        api_key=SecretStr("unit-key"),
        model="deepseek-flash",
        permit=permit,
        current_approval=approval,
        transport=httpx.MockTransport(handler),
    )
    if response_model == "deepseek-flash":
        assert model.respond(request()).text
        assert permit.reservations[0].usages[0].model == response_model
    else:
        with pytest.raises(api.DeepSeekModelError):
            model.respond(request())
        assert permit.reservations[0].unknown_count == 1
        assert permit.reservations[0].usages == []


@pytest.mark.parametrize("missing_marker", [False, True])
def test_current_accounting_never_bypasses_transmission_recheck(missing_marker):
    api = load_api()
    approval = current_approval()
    allowed, called = [True], []

    class CurrentReservation(Reservation):
        def mark_transmission_started(self):
            allowed[0] = False

    class CurrentPermit(Permit):
        current_approval = approval

        def reserve(self, max_input_tokens, max_output_tokens):
            self.reservations.append(Reservation() if missing_marker else CurrentReservation())
            return self.reservations[-1]

    def check():
        if not allowed[0]:
            raise RuntimeError("revoked during accounting")

    permit = CurrentPermit()
    model = api.DeepSeekAssistantModel(
        api_key=SecretStr("unit-key"),
        model="deepseek-flash",
        permit=permit,
        current_approval=approval,
        transport=httpx.MockTransport(lambda req: called.append(req) or httpx.Response(500)),
    )
    with pytest.raises(api.DeepSeekModelError):
        model.respond(request().with_transmission_check(check))
    assert called == []
    assert permit.reservations[0].unknown_count == 1
