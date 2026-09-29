from __future__ import annotations

from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier
from uuid import UUID

import pytest
from pydantic import JsonValue

from app.platform.assistant.runtime import (
    AssistantRuntime,
    ConversationAccessError,
    ConversationBusyError,
    InvalidRuntimeInputError,
    RunSettlement,
    StaleRunError,
)


class _Clock:
    def __init__(self) -> None:
        self._next = datetime(2026, 8, 18, 8, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        current = self._next
        self._next += timedelta(seconds=1)
        return current


class _Refs:
    def __init__(self) -> None:
        self._counts: dict[str, int] = defaultdict(int)

    def __call__(self, kind: str) -> str:
        self._counts[kind] += 1
        return f"{kind}-{self._counts[kind]}"


class _StickyDict(dict[str, JsonValue]):
    def __deepcopy__(self, memo: object) -> _StickyDict:
        return self


@pytest.fixture
def runtime() -> AssistantRuntime:
    return AssistantRuntime(clock=_Clock(), ref_factory=_Refs())


def _request(number: int) -> UUID:
    return UUID(int=number)


@pytest.mark.parametrize("extra", [0, 1])
def test_independent_tool_history_shares_existing_material_budget(
    runtime: AssistantRuntime, extra: int
) -> None:
    from app.platform.assistant.runtime import HistoryCheck, ToolHistoryProjection

    conversation = runtime.create_conversation("owner", _request(1))
    accepted = runtime.accept_turn(conversation.conversation_ref, "owner", _request(2), "read")
    material = '{"value":"' + "x" * 3988 + '"}'
    assert len(material) == 4000
    settlement = RunSettlement(
        display_payload="done",
        model_safe_note="n" * (4000 + extra),
        tool_history=(ToolHistoryProjection(material, HistoryCheck(lambda: True)),),
    )

    def settle():
        return runtime.settle_run(
            conversation.conversation_ref,
            "owner",
            accepted.run_ref,
            accepted.generation,
            _request(3),
            settlement,
        )

    if extra:
        with pytest.raises(InvalidRuntimeInputError, match="material budget"):
            settle()
        assert runtime.snapshot(conversation.conversation_ref, "owner").active_run is not None
    else:
        entry = settle().model_safe_ledger[-1]
        assert entry.model_history_checks is None
        assert entry.tool_history[0].material_json == material


def test_tool_history_container_is_private_nonpersistent_and_copy_safe():
    import copy
    import pickle

    from app.platform.assistant.runtime import HistoryCheck, ToolHistoryProjection

    item = ToolHistoryProjection('{"value":"PRIVATE_CANARY"}', HistoryCheck(lambda: True))
    assert "PRIVATE_CANARY" not in repr(item)
    assert copy.copy(item) is item and copy.deepcopy(item) is item
    with pytest.raises(TypeError, match="cannot be persisted"):
        pickle.dumps(item)


def test_create_conversation_starts_with_only_a_generation_zero_causal_record(
    runtime: AssistantRuntime,
) -> None:
    snapshot = runtime.create_conversation(
        actor_context_ref="actor-context-1",
        request_id=_request(1),
    )

    assert snapshot.conversation_ref == "conversation-1"
    assert snapshot.actor_context_ref == "actor-context-1"
    assert snapshot.generation == 0
    assert snapshot.active_run is None
    assert [entry.state for entry in snapshot.causal_ledger] == ["conversation_opened"]
    assert snapshot.causal_ledger[0].request_id == _request(1)
    assert snapshot.display_ledger == ()
    assert snapshot.model_safe_ledger == ()


def test_accept_turn_creates_one_active_run_and_keeps_teacher_text_exact(
    runtime: AssistantRuntime,
) -> None:
    conversation = runtime.create_conversation("actor-context-1", _request(1))
    teacher_utterance = "  请看一下 13800000000 这个号码。  "

    accepted = runtime.accept_turn(
        conversation.conversation_ref,
        "actor-context-1",
        _request(2),
        teacher_utterance,
    )
    snapshot = runtime.snapshot(conversation.conversation_ref, "actor-context-1")

    assert accepted.turn_ref == "turn-1"
    assert accepted.run_ref == "run-1"
    assert accepted.generation == 1
    assert snapshot.active_run == accepted
    assert snapshot.generation == 1
    assert [entry.state for entry in snapshot.causal_ledger] == [
        "conversation_opened",
        "run_active",
    ]
    assert snapshot.display_ledger[0].teacher_utterance == teacher_utterance
    assert snapshot.display_ledger[0].visible_state == "active"
    assert snapshot.model_safe_ledger == ()


def test_busy_rejection_is_atomic_and_writes_nothing(runtime: AssistantRuntime) -> None:
    conversation = runtime.create_conversation("actor-context-1", _request(1))
    runtime.accept_turn(
        conversation.conversation_ref,
        "actor-context-1",
        _request(2),
        "第一句",
    )
    before = runtime.snapshot(conversation.conversation_ref, "actor-context-1")

    with pytest.raises(ConversationBusyError, match="conversation is busy"):
        runtime.accept_turn(
            conversation.conversation_ref,
            "actor-context-1",
            _request(3),
            "未被接纳的第二句",
        )

    assert runtime.snapshot(conversation.conversation_ref, "actor-context-1") == before


def test_concurrent_accept_has_one_winner_and_no_hidden_queue(
    runtime: AssistantRuntime,
) -> None:
    conversation = runtime.create_conversation("actor-context-1", _request(1))
    barrier = Barrier(2)

    def send(number: int) -> str:
        barrier.wait()
        try:
            return runtime.accept_turn(
                conversation.conversation_ref,
                "actor-context-1",
                _request(number),
                f"第 {number} 句",
            ).run_ref
        except ConversationBusyError:
            return "busy"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(send, (2, 3)))

    snapshot = runtime.snapshot(conversation.conversation_ref, "actor-context-1")
    assert sorted(outcomes) == ["busy", "run-1"]
    assert len(snapshot.display_ledger) == 1
    assert len(snapshot.causal_ledger) == 2
    assert snapshot.active_run is not None


def test_settle_separates_display_values_from_model_safe_history(
    runtime: AssistantRuntime,
) -> None:
    conversation = runtime.create_conversation("actor-context-1", _request(1))
    accepted = runtime.accept_turn(
        conversation.conversation_ref,
        "actor-context-1",
        _request(2),
        "老师原话自带号码 13800000000",
    )
    display_canary = "SYSTEM-HELD-SECRET-13911112222"
    display_payload: dict[str, JsonValue] = {
        "kind": "card",
        "private_phone": display_canary,
    }

    snapshot = runtime.settle_run(
        conversation.conversation_ref,
        "actor-context-1",
        accepted.run_ref,
        accepted.generation,
        _request(3),
        RunSettlement(
            display_payload=display_payload,
            result_ref="result-1",
            model_safe_note="已完成一项查询",
            model_safe_template="查询已完成",
        ),
    )
    display_payload["private_phone"] = "mutated-outside"

    assert snapshot.active_run is None
    assert snapshot.display_ledger[0].display_payload == {
        "kind": "card",
        "private_phone": display_canary,
    }
    assert snapshot.display_ledger[0].visible_state == "completed"
    assert snapshot.causal_ledger[-1].state == "run_completed"
    assert snapshot.causal_ledger[-1].result_ref == "result-1"
    assert snapshot.model_safe_ledger[0].technical_marker == "completed"
    assert snapshot.model_safe_ledger[0].model_safe_note == "已完成一项查询"
    assert snapshot.model_safe_ledger[0].model_safe_template == "查询已完成"
    assert display_canary not in repr(snapshot.model_safe_ledger)

    snapshot_payload = snapshot.display_ledger[0].display_payload
    assert isinstance(snapshot_payload, dict)
    snapshot_payload["private_phone"] = "mutated-snapshot"
    refreshed = runtime.snapshot(conversation.conversation_ref, "actor-context-1")
    refreshed_payload = refreshed.display_ledger[0].display_payload
    assert isinstance(refreshed_payload, dict)
    assert refreshed_payload["private_phone"] == display_canary


def test_stop_invalidates_generation_and_late_result_writes_nothing(
    runtime: AssistantRuntime,
) -> None:
    conversation = runtime.create_conversation("actor-context-1", _request(1))
    accepted = runtime.accept_turn(
        conversation.conversation_ref,
        "actor-context-1",
        _request(2),
        "停止后仍保留的老师原话",
    )

    stopped = runtime.stop_run(
        conversation.conversation_ref,
        "actor-context-1",
        accepted.run_ref,
        accepted.generation,
        _request(3),
    )

    assert stopped.generation == accepted.generation + 1
    assert stopped.active_run is None
    assert stopped.display_ledger[0].visible_state == "stopped"
    assert stopped.display_ledger[0].teacher_utterance == "停止后仍保留的老师原话"
    assert stopped.model_safe_ledger[0].technical_marker == "stopped"
    assert stopped.model_safe_ledger[0].model_safe_note is None
    assert stopped.model_safe_ledger[0].model_safe_template is None
    before_late_result = runtime.snapshot(conversation.conversation_ref, "actor-context-1")

    with pytest.raises(StaleRunError, match="run is not current"):
        runtime.settle_run(
            conversation.conversation_ref,
            "actor-context-1",
            accepted.run_ref,
            accepted.generation,
            _request(4),
            RunSettlement(display_payload={"late": "must-not-appear"}),
        )

    assert runtime.snapshot(conversation.conversation_ref, "actor-context-1") == before_late_result


def test_invalid_settlement_is_atomic_and_keeps_run_active(runtime: AssistantRuntime) -> None:
    conversation = runtime.create_conversation("actor-context-1", _request(1))
    accepted = runtime.accept_turn(
        conversation.conversation_ref,
        "actor-context-1",
        _request(2),
        "等待合法结算",
    )
    before = runtime.snapshot(conversation.conversation_ref, "actor-context-1")

    with pytest.raises(InvalidRuntimeInputError, match="model-safe text is invalid"):
        runtime.settle_run(
            conversation.conversation_ref,
            "actor-context-1",
            accepted.run_ref,
            accepted.generation,
            _request(3),
            RunSettlement(display_payload="可显示", model_safe_note="   "),
        )

    assert runtime.snapshot(conversation.conversation_ref, "actor-context-1") == before


def test_invalid_runtime_display_kind_is_atomic_and_keeps_three_ledgers_unchanged(
    runtime: AssistantRuntime,
) -> None:
    conversation = runtime.create_conversation("actor-context-1", _request(1))
    accepted = runtime.accept_turn(
        conversation.conversation_ref,
        "actor-context-1",
        _request(2),
        "等待合法展示类型",
    )
    before = runtime.snapshot(conversation.conversation_ref, "actor-context-1")
    invalid = RunSettlement(display_payload="不可结算")
    object.__setattr__(invalid, "display_kind", "html")

    with pytest.raises(InvalidRuntimeInputError, match="display kind is invalid"):
        runtime.settle_run(
            conversation.conversation_ref,
            "actor-context-1",
            accepted.run_ref,
            accepted.generation,
            _request(3),
            invalid,
        )

    after = runtime.snapshot(conversation.conversation_ref, "actor-context-1")
    assert after.active_run == before.active_run
    assert after.causal_ledger == before.causal_ledger
    assert after.display_ledger == before.display_ledger
    assert after.model_safe_ledger == before.model_safe_ledger


def test_custom_json_container_cannot_bypass_snapshot_isolation(
    runtime: AssistantRuntime,
) -> None:
    conversation = runtime.create_conversation("actor-context-1", _request(1))
    accepted = runtime.accept_turn(
        conversation.conversation_ref,
        "actor-context-1",
        _request(2),
        "拒绝自定义复制行为",
    )
    before = runtime.snapshot(conversation.conversation_ref, "actor-context-1")
    payload = _StickyDict({"private_phone": "SYSTEM-HELD-SECRET"})

    with pytest.raises(InvalidRuntimeInputError, match="display payload is invalid"):
        runtime.settle_run(
            conversation.conversation_ref,
            "actor-context-1",
            accepted.run_ref,
            accepted.generation,
            _request(3),
            RunSettlement(display_payload=payload),
        )

    payload["private_phone"] = "mutated-outside"
    assert runtime.snapshot(conversation.conversation_ref, "actor-context-1") == before


def test_stop_and_completion_race_has_exactly_one_terminal_result(
    runtime: AssistantRuntime,
) -> None:
    conversation = runtime.create_conversation("actor-context-1", _request(1))
    accepted = runtime.accept_turn(
        conversation.conversation_ref,
        "actor-context-1",
        _request(2),
        "并发终态",
    )
    barrier = Barrier(2)

    def complete() -> str:
        barrier.wait()
        try:
            runtime.settle_run(
                conversation.conversation_ref,
                "actor-context-1",
                accepted.run_ref,
                accepted.generation,
                _request(3),
                RunSettlement(display_payload="完成"),
            )
            return "completed"
        except StaleRunError:
            return "stale"

    def stop() -> str:
        barrier.wait()
        try:
            runtime.stop_run(
                conversation.conversation_ref,
                "actor-context-1",
                accepted.run_ref,
                accepted.generation,
                _request(4),
            )
            return "stopped"
        except StaleRunError:
            return "stale"

    with ThreadPoolExecutor(max_workers=2) as executor:
        completion = executor.submit(complete)
        stopping = executor.submit(stop)
        outcomes = {completion.result(), stopping.result()}

    snapshot = runtime.snapshot(conversation.conversation_ref, "actor-context-1")
    assert "stale" in outcomes
    assert len(outcomes) == 2
    assert snapshot.active_run is None
    assert snapshot.causal_ledger[-1].state in {"run_completed", "run_stopped"}
    assert len(snapshot.model_safe_ledger) == 1
    assert snapshot.display_ledger[0].visible_state in {"completed", "stopped"}


def test_actor_context_is_checked_before_read_or_mutation(runtime: AssistantRuntime) -> None:
    conversation = runtime.create_conversation("owner-sensitive-ref", _request(1))

    with pytest.raises(ConversationAccessError) as read_error:
        runtime.snapshot(conversation.conversation_ref, "attacker-sensitive-ref")
    with pytest.raises(ConversationAccessError) as write_error:
        runtime.accept_turn(
            conversation.conversation_ref,
            "attacker-sensitive-ref",
            _request(2),
            "伪造上下文",
        )

    assert str(read_error.value) == "conversation is unavailable"
    assert str(write_error.value) == "conversation is unavailable"
    assert "owner-sensitive-ref" not in str(read_error.value)
    assert "attacker-sensitive-ref" not in str(write_error.value)
    assert runtime.snapshot(conversation.conversation_ref, "owner-sensitive-ref") == conversation


def test_conversations_are_isolated(runtime: AssistantRuntime) -> None:
    first = runtime.create_conversation("actor-context-1", _request(1))
    second = runtime.create_conversation("actor-context-1", _request(2))
    runtime.accept_turn(first.conversation_ref, "actor-context-1", _request(3), "只在第一段对话")

    first_snapshot = runtime.snapshot(first.conversation_ref, "actor-context-1")
    second_snapshot = runtime.snapshot(second.conversation_ref, "actor-context-1")

    assert first_snapshot.conversation_ref != second_snapshot.conversation_ref
    assert len(first_snapshot.display_ledger) == 1
    assert second_snapshot.display_ledger == ()
    assert second_snapshot.active_run is None
    assert second_snapshot.generation == 0


def test_invalid_utterance_fails_closed_without_echo_or_state_change(
    runtime: AssistantRuntime,
) -> None:
    conversation = runtime.create_conversation("actor-context-1", _request(1))

    with pytest.raises(InvalidRuntimeInputError) as error:
        runtime.accept_turn(
            conversation.conversation_ref,
            "actor-context-1",
            _request(2),
            "   \t\n",
        )

    assert str(error.value) == "teacher utterance is invalid"
    assert runtime.snapshot(conversation.conversation_ref, "actor-context-1") == conversation


def test_completed_run_allows_next_turn_with_monotonic_generation(
    runtime: AssistantRuntime,
) -> None:
    conversation = runtime.create_conversation("actor-context-1", _request(1))
    first = runtime.accept_turn(
        conversation.conversation_ref,
        "actor-context-1",
        _request(2),
        "第一轮",
    )
    runtime.settle_run(
        conversation.conversation_ref,
        "actor-context-1",
        first.run_ref,
        first.generation,
        _request(3),
        RunSettlement(display_payload="第一轮完成"),
    )

    second = runtime.accept_turn(
        conversation.conversation_ref,
        "actor-context-1",
        _request(4),
        "第二轮",
    )

    assert second.generation == first.generation + 1
    assert second.run_ref != first.run_ref
    assert (
        len(runtime.snapshot(conversation.conversation_ref, "actor-context-1").display_ledger) == 2
    )


def test_nested_json_nulls_survive_settlement_and_snapshot_isolation(
    runtime: AssistantRuntime,
) -> None:
    conversation = runtime.create_conversation("actor-context-1", _request(1))
    accepted = runtime.accept_turn(
        conversation.conversation_ref, "actor-context-1", _request(2), "核对清空字段"
    )
    payload: dict[str, JsonValue] = {
        "before": {"value": "旧值"},
        "after": {"value": None},
        "items": [None, {"ended_at": None}],
    }
    settled = runtime.settle_run(
        conversation.conversation_ref,
        "actor-context-1",
        accepted.run_ref,
        accepted.generation,
        _request(3),
        RunSettlement(display_kind="card", display_payload=payload),
    )
    expected = {
        "before": {"value": "旧值"},
        "after": {"value": None},
        "items": [None, {"ended_at": None}],
    }
    assert settled.display_ledger[-1].display_payload == expected
    payload["before"] = {"value": "外部篡改"}
    assert (
        runtime.snapshot(conversation.conversation_ref, "actor-context-1")
        .display_ledger[-1]
        .display_payload
        == expected
    )


def test_null_payload_root_is_rejected_without_settling(runtime: AssistantRuntime) -> None:
    conversation = runtime.create_conversation("actor-context-1", _request(1))
    accepted = runtime.accept_turn(
        conversation.conversation_ref, "actor-context-1", _request(2), "等待正式结果"
    )
    before = runtime.snapshot(conversation.conversation_ref, "actor-context-1")
    invalid = RunSettlement(display_payload="placeholder")
    object.__setattr__(invalid, "display_payload", None)
    with pytest.raises(InvalidRuntimeInputError, match="display payload is invalid"):
        runtime.settle_run(
            conversation.conversation_ref,
            "actor-context-1",
            accepted.run_ref,
            accepted.generation,
            _request(3),
            invalid,
        )
    assert runtime.snapshot(conversation.conversation_ref, "actor-context-1") == before


@pytest.mark.parametrize("ending", ["replacement", "stop", "ttl", "conversation_expiry"])
def test_private_native_history_ownership_cleanup_and_snapshot_nonexposure(ending):
    import pickle
    from dataclasses import asdict

    from app.platform.assistant.runtime import PrivateNativeHistory

    class Sample(PrivateNativeHistory):
        def __init__(self):
            self.payload = "PRIVATE_NATIVE_PAYLOAD"

        def fork(self):
            return Sample()

        def discard(self):
            self.payload = None

    now = [datetime(2026, 9, 6, tzinfo=UTC)]
    runtime = AssistantRuntime(
        clock=lambda: now[0], retention_seconds=60 if ending == "conversation_expiry" else None
    )
    conversation = runtime.create_conversation("owner", _request(1))
    first = runtime.accept_turn(conversation.conversation_ref, "owner", _request(2), "原话")
    private = Sample()
    settlement = RunSettlement(
        display_payload={"text": "答复"}, model_history_checks=(), native_history=private
    )
    with pytest.raises(TypeError, match="cannot be persisted"):
        pickle.dumps(settlement)
    snapshot = runtime.settle_run(
        conversation.conversation_ref,
        "owner",
        first.run_ref,
        first.generation,
        _request(3),
        settlement,
    )
    assert "PRIVATE_NATIVE_PAYLOAD" not in repr(private) + repr(settlement) + repr(snapshot) + str(
        asdict(snapshot)
    )
    assert "native_history" not in asdict(snapshot)
    second = runtime.accept_turn(conversation.conversation_ref, "owner", _request(4), "下一句")
    leased = runtime.copy_native_history(
        conversation.conversation_ref, "owner", second.run_ref, second.generation
    )
    assert leased is not None and leased[0] == first.turn_ref and leased[1] is not private
    leased[1].discard()
    assert private.payload == "PRIVATE_NATIVE_PAYLOAD"
    if ending == "replacement":
        runtime.settle_run(
            conversation.conversation_ref,
            "owner",
            second.run_ref,
            second.generation,
            _request(5),
            RunSettlement(display_payload={"text": "新答复"}),
        )
    elif ending == "stop":
        runtime.stop_run(
            conversation.conversation_ref, "owner", second.run_ref, second.generation, _request(5)
        )
    else:
        now[0] += timedelta(hours=24) if ending == "ttl" else timedelta(seconds=60)
        runtime.purge_expired()
    assert private.payload is None


def test_native_history_cannot_be_settled_without_an_explicit_history_release_policy(runtime):
    from app.platform.assistant.runtime import PrivateNativeHistory

    class Sample(PrivateNativeHistory):
        def fork(self):
            return Sample()

        def discard(self):
            pass

    conversation = runtime.create_conversation("owner", _request(1))
    accepted = runtime.accept_turn(conversation.conversation_ref, "owner", _request(2), "原话")
    with pytest.raises(InvalidRuntimeInputError, match="history authorization"):
        runtime.settle_run(
            conversation.conversation_ref,
            "owner",
            accepted.run_ref,
            accepted.generation,
            _request(3),
            RunSettlement(display_payload={"text": "答复"}, native_history=Sample()),
        )
    assert runtime.snapshot(conversation.conversation_ref, "owner").active_run == accepted


def test_private_recovery_marker_does_not_authorize_failed_turn_material(runtime):
    from app.platform.assistant.runtime import PrivateHistoryRecoveryMarker

    class Marker(PrivateHistoryRecoveryMarker):
        def fork(self):
            return Marker()

        def discard(self):
            pass

    conversation = runtime.create_conversation("owner", _request(1))
    accepted = runtime.accept_turn(conversation.conversation_ref, "owner", _request(2), "原话")
    snapshot = runtime.settle_run(
        conversation.conversation_ref,
        "owner",
        accepted.run_ref,
        accepted.generation,
        _request(3),
        RunSettlement(
            display_payload={"text": "读取失败"},
            model_safe_note='[{"status":"source_unavailable"}]',
            model_safe_template="UNVERIFIED_ANSWER",
            native_history=Marker(),
        ),
    )
    assert snapshot.model_safe_ledger[-1].model_history_checks is None
    following = runtime.accept_turn(conversation.conversation_ref, "owner", _request(4), "重查")
    lease = runtime.copy_native_history(
        conversation.conversation_ref, "owner", following.run_ref, following.generation
    )
    assert lease is not None and isinstance(lease[1], Marker)
    lease[1].discard()
