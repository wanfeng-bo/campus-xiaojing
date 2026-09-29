from uuid import uuid4

import pytest

from app.platform.assistant.runtime import AssistantRuntimeError
from tests.assistant.test_tool_loop import ScriptModel, gateway, load_api, setup_run


@pytest.mark.parametrize("tool_count,budget", [(0, 4.5), (2, 8.5)])
def test_internal_buffering_does_not_spend_the_deadline_on_duplicate_full_reads(
    monkeypatch, tool_count, budget
):
    api = load_api()
    runtime, accepted = setup_run()
    elapsed = [0.0]
    monkeypatch.setattr(api, "monotonic", lambda: elapsed[0])

    def validate(_):
        elapsed[0] += 1.0  # Simulate measured cost of an unchanged complete fact read.

    tools = gateway(api, validate=validate)
    replies = []
    if tool_count:

        def selected(request):
            request.require_current_transmission()
            return api.ModelReply(
                calls=tuple(
                    api.ModelToolCall(call_id=str(i), name="directory.lookup", arguments={})
                    for i in range(tool_count)
                )
            )

        replies.append(selected)

    def final(request):
        request.require_current_transmission()
        return api.ModelReply(text="The authorized result.")

    replies.append(final)
    result = api.AssistantToolLoop(
        model=ScriptModel(replies), gateway_factory=lambda _: tools, timeout_seconds=budget
    ).run_accepted(
        runtime=runtime, accepted=accepted, actor_context_ref="actor-private", request_id=uuid4()
    )
    assert result.display_ledger[-1].display_payload["text"] == "The authorized result."
    assert len(tools.calls) == tool_count


@pytest.mark.parametrize("boundary", ["next_tool", "next_model", "final_display", "transmission"])
def test_revoked_result_never_crosses_the_next_release_boundary(boundary):
    api = load_api()
    runtime, accepted = setup_run()
    live = [True]
    releases = []

    def validate(_):
        if not live[0]:
            raise AssistantRuntimeError("authorization changed")

    tools = gateway(api, validate=validate)
    original = tools.execute

    def execute(call, context):
        result = original(call, context)
        live[0] = False
        return result

    tools.execute = execute

    def first(request):
        if boundary == "transmission":
            live[0] = False
        request.require_current_transmission()
        releases.append("model")
        if boundary == "final_display":
            live[0] = False
            return api.ModelReply(text="Must not be displayed.")
        return api.ModelReply(
            calls=tuple(
                api.ModelToolCall(call_id=str(i), name="directory.lookup", arguments={})
                for i in range(2 if boundary == "next_tool" else 1)
            )
        )

    def forbidden_next(request):
        request.require_current_transmission()
        releases.append("forbidden-model")
        return api.ModelReply(text="Must not be displayed.")

    model = ScriptModel([first, forbidden_next])
    with pytest.raises(AssistantRuntimeError):
        api.AssistantToolLoop(model=model, gateway_factory=lambda _: tools).run_accepted(
            runtime=runtime,
            accepted=accepted,
            actor_context_ref="actor-private",
            request_id=uuid4(),
        )
    assert "forbidden-model" not in releases
    assert len(model.requests) == 1
    assert len(tools.calls) == (1 if boundary in {"next_tool", "next_model"} else 0)
    assert all(
        entry.display_payload is None
        for entry in runtime.snapshot(accepted.conversation_ref, "actor-private").display_ledger
    )


@pytest.mark.parametrize("outcome", ["allowed", "revoked", "stopped"])
def test_pending_control_is_revalidated_before_any_credential_store_write(outcome):
    from app.platform.assistant.contracts import ActionOfferV1
    from tests.assistant.test_control_service import _direct_result, _service_and_context

    api = load_api()
    runtime, accepted = setup_run()
    control, _, store, module, _ = _service_and_context()
    live = [True]

    def validate(_):
        if not live[0]:
            raise AssistantRuntimeError("authorization changed")

    tools = gateway(api, validate=validate)
    tools.descriptors = (
        api.ToolDescriptor(
            name="fixture.prepare",
            owner="fixture.control",
            kind="prepare",
            description="Prepare a reviewable candidate.",
            parameters={"type": "object", "properties": {}, "additionalProperties": False},
        ),
    )
    candidate = _direct_result(
        state_revision=1,
        staged_state_ref="state-private-1",
        action_offer=ActionOfferV1(
            action_offer_ref="offer-private-1", safe_label="Confirm candidate"
        ),
    )

    def prepare(call, context):
        tools.calls.append((call, context))
        return api.ToolExecutionResult(
            model_content={"status": "prepared"}, module_result=candidate
        )

    tools.execute = prepare

    def final(request):
        request.require_current_transmission()
        if outcome == "revoked":
            live[0] = False
        elif outcome == "stopped":
            runtime.stop_run(
                accepted.conversation_ref,
                "actor-private",
                accepted.run_ref,
                accepted.generation,
                uuid4(),
            )
        return api.ModelReply(text="Review the candidate before confirming.")

    model = ScriptModel(
        [
            api.ModelReply(
                calls=(api.ModelToolCall(call_id="prepare", name="fixture.prepare", arguments={}),)
            ),
            final,
        ]
    )
    loop = api.AssistantToolLoop(
        model=model, gateway_factory=lambda _: tools, control_service=control
    )
    arguments = dict(
        runtime=runtime, accepted=accepted, actor_context_ref="actor-private", request_id=uuid4()
    )
    if outcome == "allowed":
        snapshot = loop.run_accepted(**arguments)
        assert len(store.records) == 1 and len(store.ttls) == 1
        assert len(snapshot.display_ledger[-1].controls) == 1
    else:
        with pytest.raises(AssistantRuntimeError):
            loop.run_accepted(**arguments)
        assert store.records == {} and store.ttls == []
        assert all(
            entry.display_payload is None
            for entry in runtime.snapshot(accepted.conversation_ref, "actor-private").display_ledger
        )
    assert len(tools.calls) == 1 and len(model.requests) == 2
    assert module.confirm_calls == module.recover_calls == module.check_calls == []


def test_late_stop_after_tool_returns_drops_buffer_without_next_model_or_display():
    api = load_api()
    runtime, accepted = setup_run()
    tools = gateway(api)

    def stopped(call, context):
        tools.calls.append((call, context))
        runtime.stop_run(
            accepted.conversation_ref,
            "actor-private",
            accepted.run_ref,
            accepted.generation,
            uuid4(),
        )
        return api.ToolExecutionResult(
            model_content={"value": "LATE_VALUE"}, fact_card={"value": "LATE_VALUE"}
        )

    tools.execute = stopped
    model = ScriptModel(
        [
            api.ModelReply(
                calls=(api.ModelToolCall(call_id="one", name="directory.lookup", arguments={}),)
            )
        ]
    )
    with pytest.raises(AssistantRuntimeError):
        api.AssistantToolLoop(model=model, gateway_factory=lambda _: tools).run_accepted(
            runtime=runtime,
            accepted=accepted,
            actor_context_ref="actor-private",
            request_id=uuid4(),
        )
    snapshot = runtime.snapshot(accepted.conversation_ref, "actor-private")
    assert len(model.requests) == 1 and len(tools.calls) == 1
    assert snapshot.active_run is None
    assert all(entry.display_payload is None for entry in snapshot.display_ledger)
    assert all(entry.model_safe_template is None for entry in snapshot.model_safe_ledger)


def test_revoked_native_history_after_model_reply_has_no_final_display_or_quoted_fallback():
    from app.platform.assistant.runtime import HistoryCheck
    from tests.assistant.test_tool_loop import _native_answer

    api = load_api()
    runtime, accepted = setup_run()
    live = [True]
    tools = gateway(api)
    tools.execute = lambda *_: api.ToolExecutionResult(
        model_content={"value": "PROTECTED_VALUE"}, history_check=HistoryCheck(lambda: live[0])
    )
    api.AssistantToolLoop(
        model=ScriptModel(
            [
                api.ModelReply(
                    calls=(api.ModelToolCall(call_id="one", name="directory.lookup", arguments={}),)
                ),
                _native_answer(api, "PROTECTED_VALUE"),
            ]
        ),
        gateway_factory=lambda _: tools,
    ).run_accepted(
        runtime=runtime, accepted=accepted, actor_context_ref="actor-private", request_id=uuid4()
    )
    follow = runtime.accept_turn(accepted.conversation_ref, "actor-private", uuid4(), "Continue")
    before = runtime.snapshot(follow.conversation_ref, "actor-private")
    leased = []
    reply = _native_answer(api, "Must not be displayed.")

    def revoked(request):
        request.require_current_transmission()
        leased.append(
            next(m.private_continuation for m in request.messages if m.role == "assistant")
        )
        live[0] = False
        return reply

    model = ScriptModel([revoked])
    with pytest.raises(AssistantRuntimeError, match="history"):
        api.AssistantToolLoop(model=model, gateway_factory=lambda _: tools).run_accepted(
            runtime=runtime, accepted=follow, actor_context_ref="actor-private", request_id=uuid4()
        )
    after = runtime.snapshot(follow.conversation_ref, "actor-private")
    assert len(model.requests) == 1
    assert after.display_ledger == before.display_ledger
    assert after.model_safe_ledger == before.model_safe_ledger
    assert leased[0].get_payload() is None
    assert reply.private_continuation.get_payload() is None


def test_owned_tool_execution_shares_the_precheck_release_scope():
    from contextlib import contextmanager

    api = load_api()
    runtime, accepted = setup_run()
    active = [False]
    seen = []
    tools = gateway(api)
    original = tools.execute

    @contextmanager
    def validation_scope():
        outer = active[0]
        active[0] = True
        try:
            yield
        finally:
            active[0] = outer

    def execute(call, context):
        seen.append(active[0])
        return original(call, context)

    tools.validation_scope = validation_scope
    tools.execute = execute
    model = ScriptModel(
        [
            api.ModelReply(
                calls=(api.ModelToolCall(call_id="a", name="directory.lookup", arguments={}),)
            ),
            api.ModelReply(text="Authorized answer"),
        ]
    )
    api.AssistantToolLoop(model=model, gateway_factory=lambda _: tools).run_accepted(
        runtime=runtime, accepted=accepted, actor_context_ref="actor-private", request_id=uuid4()
    )
    assert seen == [True]
    assert active == [False]
