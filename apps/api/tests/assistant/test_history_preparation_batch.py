"""Owner exit checks precede release; failed batches never refresh in shared state."""

from contextlib import contextmanager
from uuid import uuid4

import pytest

from app.platform.assistant.runtime import HistoryCheck, RunSettlement, StaleRunError
from tests.assistant.test_tool_loop import ScriptModel, gateway, load_api, setup_run


def prepare(checks, scope, checkpoint=lambda: None):
    api = load_api()
    runtime, accepted = setup_run()
    runtime.settle_run(
        accepted.conversation_ref,
        "actor-private",
        accepted.run_ref,
        accepted.generation,
        uuid4(),
        RunSettlement(
            display_payload={"text": "old"},
            model_safe_template="APPROVED_FACT",
            model_history_checks=checks,
        ),
    )
    loop = api.AssistantToolLoop(model=ScriptModel([]), gateway_factory=lambda _: gateway(api))
    return loop._messages(
        runtime.snapshot(accepted.conversation_ref, "actor-private"),
        "source?",
        checkpoint=checkpoint,
        history_validation_scope=scope,
    )


@pytest.mark.parametrize("outcome", ["stable", "drift", "equivalent_refresh", "cancelled"])
def test_history_only_reuses_complete_owner_boundaries(outcome):
    active, live, cancelled = [False], [True], [False]
    refreshes, exits = [], []

    @contextmanager
    def scope():
        active[0] = True
        try:
            yield
            exits.append("fresh_exit")
            if outcome in {"drift", "cancelled"}:
                live[0] = False
                cancelled[0] = outcome == "cancelled"
                raise RuntimeError("owner state changed at exit")
        finally:
            active[0] = False

    def refresh():
        assert not active[0] and not cancelled[0]
        refreshes.append(1)
        return live[0]

    def valid(first):
        assert active[0]
        return not (first and outcome == "equivalent_refresh")

    def checkpoint():
        if cancelled[0]:
            raise StaleRunError("cancelled")

    checks = (
        HistoryCheck(
            refresh, revalidate=lambda: valid(True), key="one", revalidation_covers_content=True
        ),
        HistoryCheck(
            refresh, revalidate=lambda: valid(False), key="two", revalidation_covers_content=True
        ),
    )
    if outcome == "cancelled":
        with pytest.raises(StaleRunError):
            prepare(checks, scope, checkpoint)
        assert not refreshes
    else:
        messages, released = prepare(checks, scope, checkpoint)
        assert ("APPROVED_FACT" in str(messages)) == (outcome != "drift")
        assert released == (() if outcome == "drift" else checks)
        assert len(refreshes) == {"stable": 0, "drift": 2, "equivalent_refresh": 1}[outcome]
    assert exits == ["fresh_exit"] and not active[0]


def test_sibling_revocation_during_individual_refresh_blocks_the_actual_model_send():
    from app.platform.assistant.runtime import AssistantRuntimeError

    api = load_api()
    runtime, prior = setup_run()
    first_live, second_current = [True], [False]

    def second_refresh():
        second_current[0] = True
        first_live[0] = False
        return True

    checks = (
        HistoryCheck(
            lambda: first_live[0],
            revalidate=lambda: first_live[0],
            key="first",
            revalidation_covers_content=True,
        ),
        HistoryCheck(
            second_refresh,
            revalidate=lambda: second_current[0],
            key="second",
            revalidation_covers_content=True,
        ),
    )
    runtime.settle_run(
        prior.conversation_ref,
        "actor-private",
        prior.run_ref,
        prior.generation,
        uuid4(),
        RunSettlement(
            display_payload={"text": "old"},
            model_safe_template="REVOKED_PRIVATE_FACT",
            model_history_checks=checks,
        ),
    )
    current = runtime.accept_turn(prior.conversation_ref, "actor-private", uuid4(), "source?")
    tools = gateway(api)

    @contextmanager
    def scope():
        yield

    tools.validation_scope = scope
    model = ScriptModel([api.ModelReply(text="must not receive revoked fact")])
    with pytest.raises(AssistantRuntimeError, match="history"):
        api.AssistantToolLoop(model=model, gateway_factory=lambda _: tools).run_accepted(
            runtime=runtime, accepted=current, actor_context_ref="actor-private", request_id=uuid4()
        )
    assert not model.requests and not tools.calls


def test_legacy_authorization_only_validation_cannot_replace_content_refresh():
    @contextmanager
    def scope():
        yield

    checks = tuple(HistoryCheck(lambda: False, revalidate=lambda: True) for _ in range(2))
    messages, released = prepare(checks, scope)
    assert "APPROVED_FACT" not in str(messages)
    assert not released
