from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr

from app.composition.deepseek_assistant_model import DeepSeekAssistantModel
from app.platform.assistant.runtime import AssistantRuntimeError
from tests.assistant.test_deepseek_assistant_model import Permit, completion, wire_call
from tests.assistant.test_tool_loop import ScriptModel, gateway, load_api, setup_run


@pytest.mark.parametrize("rounds,expected", [(1, 3), (2, 5)])
def test_real_adapter_rechecks_once_at_each_send_and_again_at_result_use(rounds, expected):
    api = load_api()
    runtime, accepted = setup_run()
    checks, sent = [], []
    tools = gateway(api, validate=lambda _: checks.append(len(sent)))

    def transport(request):
        sent.append(request)
        if rounds == 2 and len(sent) == 1:
            return httpx.Response(200, json=completion(calls=[wire_call("tool_0", "{}")]))
        return httpx.Response(200, json=completion(content="Authorized answer"))

    model = DeepSeekAssistantModel(
        api_key=SecretStr("fake-test-only"), model="deepseek-v4-flash",
        permit=Permit(), transport=httpx.MockTransport(transport),
    )
    snapshot = api.AssistantToolLoop(model=model, gateway_factory=lambda _: tools).run_accepted(
        runtime=runtime, accepted=accepted,
            actor_context_ref="actor-private", request_id=uuid4()
    )
    assert len(sent) == rounds
    assert len(checks) == expected
    assert len(tools.calls) == rounds - 1
    assert snapshot.display_ledger[-1].display_payload["text"] == "Authorized answer"


@pytest.mark.parametrize("marker", [None, False, 1, "true"])
def test_ordinary_models_keep_entry_validation_without_exact_trusted_contract(marker):
    api = load_api()
    runtime, accepted = setup_run()
    checks = []
    tools = gateway(api, validate=lambda _: checks.append("current"))

    def respond(request):
        assert len(checks) == 2  # initial plus private model handoff
        request.require_current_transmission()
        return api.ModelReply(text="Answer")

    model = ScriptModel([respond])
    model.checks_transmission_before_send = marker
    api.AssistantToolLoop(model=model, gateway_factory=lambda _: tools).run_accepted(
        runtime=runtime, accepted=accepted,
            actor_context_ref="actor-private", request_id=uuid4()
    )
    assert len(checks) == 4


def test_declared_adapter_cannot_settle_without_using_private_send_guard():
    api = load_api()
    runtime, accepted = setup_run()
    from tests.assistant.test_tool_loop import _native_answer

    reply = _native_answer(api, "Unguarded")
    model = ScriptModel([reply])
    model.checks_transmission_before_send = True
    with pytest.raises(AssistantRuntimeError, match="transmission"):
        api.AssistantToolLoop(model=model, gateway_factory=lambda _: gateway(api)).run_accepted(
            runtime=runtime, accepted=accepted,
            actor_context_ref="actor-private", request_id=uuid4()
        )
    assert reply.private_continuation.get_payload() is None
    assert all(x.display_payload is None for x in runtime.snapshot(
        accepted.conversation_ref, "actor-private"
    ).display_ledger)


@pytest.mark.parametrize("phase", ["encode", "reserve", "client_setup"])
def test_trusted_real_adapter_revocation_before_send_blocks_network_and_retains_hold(
    monkeypatch, phase
):
    import app.composition.deepseek_assistant_model as dm

    api = load_api()
    runtime, accepted = setup_run()
    allowed, sent = [True], []

    def current(_):
        if not allowed[0]:
            raise AssistantRuntimeError("revoked")

    tools = gateway(api, validate=current)
    original_body = dm._request_body

    def encoded(*args):
        result = original_body(*args)
        if phase == "encode":
            allowed[0] = False
        return result

    class DelayedPermit(Permit):
        def reserve(self, *args):
            result = super().reserve(*args)
            if phase == "reserve":
                allowed[0] = False
            return result

    class DelayedClient(httpx.Client):
        def __enter__(self):
            result = super().__enter__()
            if phase == "client_setup":
                allowed[0] = False
            return result

    monkeypatch.setattr(dm, "_request_body", encoded)
    monkeypatch.setattr(dm.httpx, "Client", DelayedClient)
    permit = DelayedPermit()
    model = DeepSeekAssistantModel(
        api_key=SecretStr("fake-test-only"), model="deepseek-v4-flash", permit=permit,
        transport=httpx.MockTransport(
            lambda request: sent.append(request) or httpx.Response(200, json=completion())
        ),
    )
    with pytest.raises(AssistantRuntimeError):
        api.AssistantToolLoop(model=model, gateway_factory=lambda _: tools).run_accepted(
            runtime=runtime, accepted=accepted,
            actor_context_ref="actor-private", request_id=uuid4()
        )
    assert sent == [] and tools.calls == []
    assert len(permit.reservations) == 1
    assert permit.reservations[0].usages == []
    assert permit.reservations[0].unknown_count == 1
