"""No-provider diagnostics of settled material at the actual next model boundary.

These are synthetic boundary cases, not reconstructions of unsaved past requests.
The transport retains only lengths, counts and presence flags, never message bodies.
"""

from __future__ import annotations

import json
from uuid import uuid4

import pytest

from app.platform.assistant.runtime import AssistantRuntime, HistoryCheck
from app.platform.assistant.tool_loop import (
    AssistantToolLoop,
    ModelContinuation,
    ModelReply,
    ModelToolCall,
    ToolExecutionResult,
)
from tests.assistant.test_tool_loop import ScriptModel, gateway, load_api, setup_run

REFERENCE = "f2:organization:00000000-0000-0000-0000-000000000001"


class MetadataTransport:
    checks_transmission_before_send = True

    def __init__(self, *, reference=REFERENCE):
        self.reference = reference
        self.observations = []

    def respond(self, request):
        request.require_current_transmission()
        history = next(
            (
                json.loads(message.content)
                for message in request.messages
                if message.role == "system"
                and message.content.startswith('{"kind":"quoted_historical_user_intents"')
            ),
            {"turns": []},
        )
        turns = history["turns"]
        serialized = request.model_dump_json()
        self.observations.append(
            {
                "reference_sent": self.reference in serialized,
                "turn_count": len(turns),
                "execution_notes": sum("past_tool_executions" in turn for turn in turns),
                "quoted_result_turns": sum("quoted_tool_results" in turn for turn in turns),
                "unavailable_turns": sum(
                    bool(turn.get("history_context_unavailable")) for turn in turns
                ),
                "native_messages": sum(message.role == "assistant" for message in request.messages),
                "older_user_sent": "OLDER_SMALL_TURN" in serialized,
                "latest_user_sent": "LATEST_USER_TURN" in serialized,
            }
        )
        return ModelReply(text="当前答复")


def _follow(runtime, conversation_ref, tools, *, actor="actor-private", reference=REFERENCE):
    accepted = runtime.accept_turn(conversation_ref, actor, uuid4(), "请引用刚才的对象")
    transport = MetadataTransport(reference=reference)
    AssistantToolLoop(model=transport, gateway_factory=lambda _: tools).run_accepted(
        runtime=runtime, accepted=accepted, actor_context_ref=actor, request_id=uuid4()
    )
    return transport.observations[0]


def test_legacy_sync_loop_does_not_elevate_owner_note_to_factual_proof():
    runtime, accepted = setup_run()
    tools = gateway(load_api())
    tools.execute = lambda *_: ToolExecutionResult(
        model_content={"status": "denied", "secret": REFERENCE},
        history_execution_note="read_returned_no_values",
    )
    model = ScriptModel(
        [
            ModelReply(
                calls=(ModelToolCall(call_id="read", name="directory.lookup", arguments={}),)
            ),
            ModelReply(text="UNVERIFIED_ANSWER"),
        ]
    )
    snapshot = AssistantToolLoop(model=model, gateway_factory=lambda _: tools).run_accepted(
        runtime=runtime,
        accepted=accepted,
        actor_context_ref="actor-private",
        request_id=uuid4(),
    )
    assert snapshot.model_safe_ledger[-1].model_history_checks is None
    assert isinstance(json.loads(snapshot.model_safe_ledger[-1].model_safe_note), list)
    metadata = _follow(runtime, accepted.conversation_ref, tools)
    assert not metadata["reference_sent"]
    assert metadata["quoted_result_turns"] == 0 and metadata["execution_notes"] == 0


def test_legacy_sync_loop_does_not_release_independent_tools_or_unproved_mixed_answer():
    from tests.assistant.test_f5_native_history_refresh import (
        CandidateGateway,
        _history_turn,
        _object_answer,
        _object_call,
    )

    runtime = AssistantRuntime()
    conversation = runtime.create_conversation("owner", uuid4()).conversation_ref
    tools, seen = CandidateGateway(), []

    def fail(messages):
        tools.failure = "denied"
        return _object_call("object-b")

    first = _history_turn(
        runtime,
        conversation,
        tools,
        "read",
        [
            _object_call(),
            fail,
            _object_answer("UNVERIFIED_MIXED"),
        ],
        seen,
    )
    assert first.model_safe_ledger[-1].tool_history
    assert first.model_safe_ledger[-1].model_history_checks is None
    metadata = _follow(runtime, conversation, tools, actor="owner", reference="object-a")
    assert not metadata["reference_sent"] and metadata["quoted_result_turns"] == 0


def _material(characters, *, symbol="x"):
    value = {"reference": REFERENCE, "approved_text": ""}
    fixed = len(json.dumps([value], ensure_ascii=False, separators=(",", ":")))
    value["approved_text"] = symbol * (characters - fixed)
    assert len(json.dumps([value], ensure_ascii=False, separators=(",", ":"))) == characters
    return value


def _settle_material(runtime, accepted, *, material, native_size=None, valid=lambda: True):
    tools = gateway(load_api())
    tools.execute = lambda *_: ToolExecutionResult(
        model_content=material,
        history_content=material,
        history_check=HistoryCheck(valid),
    )
    answer = ModelReply(text="已读取，可按引用继续。")
    if native_size is not None:
        answer = answer.with_private_continuation(
            ModelContinuation({"synthetic": True}, retention_bytes=native_size)
        )
    model = ScriptModel(
        [
            ModelReply(
                calls=(ModelToolCall(call_id="read", name="directory.lookup", arguments={}),)
            ),
            answer,
        ]
    )
    snapshot = AssistantToolLoop(model=model, gateway_factory=lambda _: tools).run_accepted(
        runtime=runtime, accepted=accepted, actor_context_ref="actor-private", request_id=uuid4()
    )
    return snapshot.model_safe_ledger[-1], tools


@pytest.mark.parametrize("characters", [8000, 8001])
def test_settlement_character_limit_drops_whole_tool_material(characters):
    runtime, accepted = setup_run()
    entry, tools = _settle_material(runtime, accepted, material=_material(characters))
    retained = characters <= 8000
    assert (entry.model_safe_note is not None) is retained
    metadata = _follow(runtime, accepted.conversation_ref, tools)
    assert metadata["reference_sent"] is retained
    assert metadata["quoted_result_turns"] == int(retained)
    assert metadata["turn_count"] == 1


@pytest.mark.parametrize("native_size", [0, 6500, 12001])
def test_native_history_falls_back_without_losing_fitting_tool_material(native_size):
    runtime, accepted = setup_run()
    entry, tools = _settle_material(
        runtime, accepted, material=_material(6000), native_size=native_size
    )
    assert entry.model_safe_note is not None
    metadata = _follow(runtime, accepted.conversation_ref, tools)
    assert metadata["reference_sent"]
    assert metadata["quoted_result_turns"] == 1
    assert metadata["native_messages"] == int(native_size == 0)


def test_native_assistant_message_does_not_restore_dropped_tool_references():
    runtime, accepted = setup_run()
    entry, tools = _settle_material(runtime, accepted, material=_material(8001), native_size=100)
    assert entry.model_safe_note is None
    metadata = _follow(runtime, accepted.conversation_ref, tools)
    assert metadata["native_messages"] == 1
    assert not metadata["reference_sent"]


def test_utf8_oversized_recent_material_also_excludes_earlier_small_turn():
    runtime = AssistantRuntime()
    conversation = runtime.create_conversation("actor-private", uuid4())
    tools = gateway(load_api())
    older = runtime.accept_turn(
        conversation.conversation_ref, "actor-private", uuid4(), "OLDER_SMALL_TURN"
    )
    AssistantToolLoop(
        model=ScriptModel([ModelReply(text="可继续")]), gateway_factory=lambda _: tools
    ).run_accepted(
        runtime=runtime, accepted=older, actor_context_ref="actor-private", request_id=uuid4()
    )
    latest = runtime.accept_turn(
        conversation.conversation_ref, "actor-private", uuid4(), "LATEST_USER_TURN"
    )
    entry, tools = _settle_material(runtime, latest, material=_material(6000, symbol="字"))
    note = entry.model_safe_note
    assert note is not None and len(note) <= 8000 and len(note.encode("utf-8")) > 12000
    metadata = _follow(runtime, conversation.conversation_ref, tools)
    assert metadata["turn_count"] == 0
    assert not metadata["reference_sent"]
    assert not metadata["older_user_sent"] and not metadata["latest_user_sent"]


@pytest.mark.parametrize("valid_after_read", [True, False])
def test_retained_material_is_revalidated_before_actual_transport(valid_after_read):
    live = [True]
    runtime, accepted = setup_run()
    entry, tools = _settle_material(
        runtime, accepted, material=_material(400), native_size=100, valid=lambda: live[0]
    )
    assert entry.model_safe_note is not None
    live[0] = valid_after_read
    metadata = _follow(runtime, accepted.conversation_ref, tools)
    assert metadata["reference_sent"] is valid_after_read
    assert metadata["native_messages"] == int(valid_after_read)
    assert metadata["unavailable_turns"] == int(not valid_after_read)


def test_history_last_eight_turn_window_excludes_older_user_context():
    runtime = AssistantRuntime()
    conversation = runtime.create_conversation("actor-private", uuid4())
    tools = gateway(load_api())
    for index in range(9):
        accepted = runtime.accept_turn(
            conversation.conversation_ref,
            "actor-private",
            uuid4(),
            "OLDER_SMALL_TURN" if index == 0 else f"follow-{index}",
        )
        AssistantToolLoop(
            model=ScriptModel([ModelReply(text="可继续")]), gateway_factory=lambda _: tools
        ).run_accepted(
            runtime=runtime,
            accepted=accepted,
            actor_context_ref="actor-private",
            request_id=uuid4(),
        )
    metadata = _follow(runtime, conversation.conversation_ref, tools)
    assert metadata["turn_count"] == 8
    assert not metadata["older_user_sent"]


@pytest.mark.parametrize("tool_count", [1, 2])
def test_real_five_family_domain_history_crosses_settlement_limit_with_multiple_tools(tool_count):
    from app.composition.school_fact_directory_gateway import MODULE
    from tests.test_f2_directory_gateway import _directory_read_turn, _history_service_facts

    # Repeated all-authorized reads isolate accumulation; they do not reconstruct B1/B2.
    facts = _history_service_facts()
    directory, _, _ = _directory_read_turn(facts)
    runtime = AssistantRuntime()
    conversation = runtime.create_conversation("private-actor", uuid4())
    accepted = runtime.accept_turn(
        conversation.conversation_ref, "private-actor", uuid4(), "查询服务渠道与校区"
    )
    model = ScriptModel(
        [
            ModelReply(
                calls=tuple(
                    ModelToolCall(
                        call_id=f"services-{index}",
                        name=MODULE + ".lookup",
                        arguments={
                            "subject_kind": "school_service",
                            "scope": "all_authorized",
                            "include": ["official_contact", "organization_campus_relationship"],
                        },
                    )
                    for index in range(tool_count)
                )
            ),
            ModelReply(text="已取得服务渠道与校区关联。"),
        ]
    )
    snapshot = AssistantToolLoop(model=model, gateway_factory=directory.for_turn).run_accepted(
        runtime=runtime, accepted=accepted, actor_context_ref="private-actor", request_id=uuid4()
    )
    note = snapshot.model_safe_ledger[-1].model_safe_note
    assert (note is not None) is (tool_count == 1)
    reference = facts.rows[("school_service", None)]["records"][0]["reference"]
    follow = runtime.accept_turn(
        conversation.conversation_ref, "private-actor", uuid4(), "刚才对象的依据？"
    )
    transport = MetadataTransport(reference=reference)
    AssistantToolLoop(model=transport, gateway_factory=directory.for_turn).run_accepted(
        runtime=runtime, accepted=follow, actor_context_ref="private-actor", request_id=uuid4()
    )
    assert transport.observations[0]["reference_sent"] is (tool_count == 1)


@pytest.mark.parametrize("check_count", [16, 17])
def test_history_check_budget_suppresses_unreleased_material_before_transport(check_count):
    runtime, accepted = setup_run()
    tools = gateway(load_api())
    tools.execute = lambda call, _: ToolExecutionResult(
        model_content={"reference": REFERENCE},
        history_check=HistoryCheck(lambda: True, key=call.call_id),
    )
    replies = [
        ModelReply(
            calls=tuple(
                ModelToolCall(call_id=f"read-{index}", name="directory.lookup", arguments={})
                for index in range(start, min(start + 8, check_count))
            )
        )
        for start in range(0, check_count, 8)
    ]
    replies.append(ModelReply(text="已经读取。"))
    snapshot = AssistantToolLoop(
        model=ScriptModel(replies), gateway_factory=lambda _: tools, max_tool_calls=check_count
    ).run_accepted(
        runtime=runtime, accepted=accepted, actor_context_ref="actor-private", request_id=uuid4()
    )
    assert snapshot.model_safe_ledger[-1].model_safe_note is not None
    metadata = _follow(runtime, accepted.conversation_ref, tools)
    assert metadata["reference_sent"] is (check_count == 16)
    assert metadata["unavailable_turns"] == int(check_count > 16)


@pytest.mark.parametrize(
    "characters,symbol,expected", [(8000, "x", True), (8001, "x", False), (6000, "字", False)]
)
def test_official_deepseek_adapter_preserves_selected_history_at_mock_http_boundary(
    characters, symbol, expected
):
    import httpx

    from tests.assistant.test_deepseek_assistant_model import completion, model_with

    runtime, accepted = setup_run()
    _, tools = _settle_material(runtime, accepted, material=_material(characters, symbol=symbol))
    observations = []

    def handler(request):
        messages = json.loads(request.content)["messages"]
        observations.append(
            {
                "reference_sent": any(
                    REFERENCE in str(message.get("content")) for message in messages
                ),
                "tool_result_sent": any(
                    '"quoted_tool_results":' in str(message.get("content")) for message in messages
                ),
            }
        )
        return httpx.Response(200, json=completion(content="已收到当前材料。"))

    model, permit = model_with(handler, max_input_tokens=65536)
    follow = runtime.accept_turn(accepted.conversation_ref, "actor-private", uuid4(), "引用呢？")
    AssistantToolLoop(model=model, gateway_factory=lambda _: tools).run_accepted(
        runtime=runtime, accepted=follow, actor_context_ref="actor-private", request_id=uuid4()
    )
    assert observations == [{"reference_sent": expected, "tool_result_sent": expected}]
    assert len(permit.reservations) == 1
