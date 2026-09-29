from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID

import pytest

from app.platform.assistant.contracts import PublicControlV1
from app.platform.assistant.runtime import (
    AcceptedRun,
    AssistantRuntime,
    ConversationBusyError,
    RunSettlement,
    RuntimeInvariantError,
    StaleRunError,
)

_ISSUE_REQUEST = UUID("00000000-0000-0000-0000-000000000101")
_CLICK_REQUEST = UUID("00000000-0000-0000-0000-000000000102")


class _Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 8, 18, 1, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.value


class _Refs:
    def __init__(self) -> None:
        self.count = 0

    def __call__(self, kind: str) -> str:
        self.count += 1
        return f"{kind}-{self.count}"


def _settled_runtime(
    *,
    clock: _Clock | None = None,
    refs: _Refs | None = None,
    control_kind: Literal[
        "action_offer", "recovery_control", "result_check_handle"
    ] = "action_offer",
) -> tuple[AssistantRuntime, str, AcceptedRun, PublicControlV1]:
    runtime = AssistantRuntime(clock=clock, ref_factory=refs or _Refs())
    opened = runtime.create_conversation("actor-1", UUID(int=1))
    accepted = runtime.accept_turn(
        opened.conversation_ref,
        "actor-1",
        _ISSUE_REQUEST,
        "形成一个机械控制",
    )
    control = PublicControlV1(
        kind=control_kind,
        handle="signed-handle-1",
        safe_label="核验结果" if control_kind == "result_check_handle" else "确认办理",
        expires_at=int((clock.value if clock else datetime.now(UTC)).timestamp()) + 300,
        manual_review_on_expiry=control_kind == "result_check_handle",
        module_ref="fixture.control",
    )
    runtime.settle_run(
        accepted.conversation_ref,
        "actor-1",
        accepted.run_ref,
        accepted.generation,
        _ISSUE_REQUEST,
        RunSettlement(
            display_payload={"text": "待处理"},
            module_ref="fixture.control",
            module_state_ref="state-1",
            state_revision=1,
            issuance_request_id=_ISSUE_REQUEST,
            controls=(control,),
        ),
    )
    return runtime, accepted.conversation_ref, accepted, control


def test_runtime_reservation_closes_revalidate_consume_toctou_and_binds_both_request_ids() -> None:
    runtime, conversation_ref, accepted, hidden = _settled_runtime()
    reservation_ref = runtime.reserve_control(
        conversation_ref=conversation_ref,
        actor_context_ref="actor-1",
        turn_ref=accepted.turn_ref,
        run_ref=accepted.run_ref,
        generation=accepted.generation,
        module_ref="fixture.control",
        module_state_ref="state-1",
        state_revision=1,
        issuance_request_id=_ISSUE_REQUEST,
        click_request_id=_CLICK_REQUEST,
        kind="action_offer",
        expires_at=hidden.expires_at,
    )
    runtime.attach_control_reconciliation(
        conversation_ref=conversation_ref,
        actor_context_ref="actor-1",
        reservation_ref=reservation_ref,
        control=hidden.model_copy(
            update={
                "kind": "result_check_handle",
                "safe_label": "核验结果",
                "manual_review_on_expiry": True,
            }
        ),
    )

    with pytest.raises(ConversationBusyError):
        runtime.accept_turn(conversation_ref, "actor-1", UUID(int=3), "并发的新一句")
    with pytest.raises(StaleRunError):
        runtime.reserve_control(
            conversation_ref=conversation_ref,
            actor_context_ref="actor-1",
            turn_ref=accepted.turn_ref,
            run_ref=accepted.run_ref,
            generation=accepted.generation,
            module_ref="fixture.control",
            module_state_ref="state-1",
            state_revision=1,
            issuance_request_id=_ISSUE_REQUEST,
            click_request_id=_CLICK_REQUEST,
            kind="action_offer",
            expires_at=hidden.expires_at,
        )

    snapshot = runtime.snapshot(conversation_ref, "actor-1")
    click_entries = [
        entry for entry in snapshot.causal_ledger if entry.request_id == _CLICK_REQUEST
    ]
    assert [entry.state for entry in click_entries] == ["control_active"]
    assert "signed-handle-1" not in repr(click_entries)
    assert "state-1" not in repr(click_entries)


def test_new_module_revision_removes_old_recovery_but_keeps_current_revision_recovery() -> None:
    runtime, conversation_ref, _accepted, _control = _settled_runtime(
        control_kind="recovery_control"
    )

    newer = runtime.accept_turn(conversation_ref, "actor-1", UUID(int=4), "形成更新 revision")
    new_recovery = PublicControlV1(
        kind="recovery_control",
        handle="signed-handle-new",
        safe_label="放弃新版",
        expires_at=int(datetime.now(UTC).timestamp()) + 300,
        module_ref="fixture.control",
    )
    snapshot = runtime.settle_run(
        conversation_ref,
        "actor-1",
        newer.run_ref,
        newer.generation,
        UUID(int=4),
        RunSettlement(
            display_payload={"text": "新版"},
            module_ref="fixture.control",
            module_state_ref="state-2",
            state_revision=2,
            issuance_request_id=UUID(int=4),
            controls=(new_recovery,),
        ),
    )

    assert snapshot.display_ledger[0].controls == ()
    assert snapshot.display_ledger[1].controls == (new_recovery,)


def test_reservation_at_exact_expiry_rejects_without_consuming_the_click_request() -> None:
    clock = _Clock()
    runtime, conversation_ref, accepted, action = _settled_runtime(clock=clock)
    clock.value += timedelta(seconds=300)

    with pytest.raises(StaleRunError):
        runtime.reserve_control(
            conversation_ref=conversation_ref,
            actor_context_ref="actor-1",
            turn_ref=accepted.turn_ref,
            run_ref=accepted.run_ref,
            generation=accepted.generation,
            module_ref="fixture.control",
            module_state_ref="state-1",
            state_revision=1,
            issuance_request_id=_ISSUE_REQUEST,
            click_request_id=_CLICK_REQUEST,
            kind="action_offer",
            expires_at=action.expires_at,
        )

    snapshot = runtime.snapshot(conversation_ref, "actor-1")
    assert snapshot.display_ledger[0].controls == ()
    assert all(entry.request_id != _CLICK_REQUEST for entry in snapshot.causal_ledger)


def test_unresolved_check_expires_at_300_seconds_into_manual_review() -> None:
    clock = _Clock()
    runtime, conversation_ref, _accepted, control = _settled_runtime(
        clock=clock,
        control_kind="result_check_handle",
    )

    clock.value += timedelta(seconds=299)
    before = runtime.snapshot(conversation_ref, "actor-1")
    assert before.display_ledger[0].controls == (control,)

    clock.value += timedelta(seconds=1)
    expired = runtime.snapshot(conversation_ref, "actor-1")
    assert expired.display_ledger[0].controls == ()
    assert expired.display_ledger[0].display_payload == {"text": "待人工核对"}

    later = runtime.snapshot(conversation_ref, "actor-1")
    assert later.display_ledger[0].controls == ()
    assert later.display_ledger[0].display_payload == {"text": "待人工核对"}


def test_inflight_check_expiry_is_visible_and_late_result_cannot_overwrite() -> None:
    clock = _Clock()
    runtime, conversation_ref, accepted, _action = _settled_runtime(clock=clock)
    reservation_ref = runtime.reserve_control(
        conversation_ref=conversation_ref,
        actor_context_ref="actor-1",
        turn_ref=accepted.turn_ref,
        run_ref=accepted.run_ref,
        generation=accepted.generation,
        module_ref="fixture.control",
        module_state_ref="state-1",
        state_revision=1,
        issuance_request_id=_ISSUE_REQUEST,
        click_request_id=_CLICK_REQUEST,
        kind="action_offer",
        expires_at=_action.expires_at,
    )
    hidden = PublicControlV1(
        kind="result_check_handle",
        handle="prebuilt-check-handle",
        safe_label="核验结果",
        expires_at=int(clock.value.timestamp()) + 300,
        manual_review_on_expiry=True,
        module_ref="fixture.control",
    )
    runtime.attach_control_reconciliation(
        conversation_ref=conversation_ref,
        actor_context_ref="actor-1",
        reservation_ref=reservation_ref,
        control=hidden,
    )
    runtime.mark_control_invoked(
        conversation_ref=conversation_ref,
        actor_context_ref="actor-1",
        reservation_ref=reservation_ref,
    )
    clock.value += timedelta(seconds=300)

    while_module_is_in_flight = runtime.snapshot(conversation_ref, "actor-1")
    assert while_module_is_in_flight.display_ledger[0].controls == ()
    assert while_module_is_in_flight.display_ledger[0].display_payload == {"text": "待人工核对"}

    late_result = runtime.apply_control_result(
        conversation_ref=conversation_ref,
        actor_context_ref="actor-1",
        turn_ref=accepted.turn_ref,
        run_ref=accepted.run_ref,
        generation=accepted.generation,
        previous_module_ref="fixture.control",
        previous_module_state_ref="state-1",
        previous_state_revision=1,
        reservation_ref=reservation_ref,
        next_module_state_ref="state-2",
        next_state_revision=2,
        display_kind="text",
        display_payload={"text": "迟到成功，不得覆盖人工核对"},
        controls=(),
    )

    assert late_result.display_ledger[0].controls == ()
    assert late_result.display_ledger[0].display_payload == {"text": "待人工核对"}


class _FaultClock(_Clock):
    def __init__(self) -> None:
        super().__init__()
        self.fail_on_call: int | None = None
        self.calls = 0

    def __call__(self) -> datetime:
        self.calls += 1
        if self.fail_on_call == self.calls:
            raise RuntimeInvariantError("CLOCK-FAILURE")
        return self.value


class _FaultRefs(_Refs):
    def __init__(self) -> None:
        super().__init__()
        self.fail_control_reservation = False

    def __call__(self, kind: str) -> str:
        if kind == "control-reservation" and self.fail_control_reservation:
            raise RuntimeError("REF-FAILURE")
        return super().__call__(kind)


def test_reserve_reference_failure_is_atomic_and_same_click_can_retry() -> None:
    refs = _FaultRefs()
    runtime, conversation_ref, accepted, action = _settled_runtime(refs=refs)
    refs.fail_control_reservation = True

    with pytest.raises(RuntimeInvariantError, match="reference generation failed"):
        runtime.reserve_control(
            conversation_ref=conversation_ref,
            actor_context_ref="actor-1",
            turn_ref=accepted.turn_ref,
            run_ref=accepted.run_ref,
            generation=accepted.generation,
            module_ref="fixture.control",
            module_state_ref="state-1",
            state_revision=1,
            issuance_request_id=_ISSUE_REQUEST,
            click_request_id=_CLICK_REQUEST,
            kind="action_offer",
            expires_at=action.expires_at,
        )

    refs.fail_control_reservation = False
    reservation_ref = runtime.reserve_control(
        conversation_ref=conversation_ref,
        actor_context_ref="actor-1",
        turn_ref=accepted.turn_ref,
        run_ref=accepted.run_ref,
        generation=accepted.generation,
        module_ref="fixture.control",
        module_state_ref="state-1",
        state_revision=1,
        issuance_request_id=_ISSUE_REQUEST,
        click_request_id=_CLICK_REQUEST,
        kind="action_offer",
        expires_at=action.expires_at,
    )
    assert reservation_ref.startswith("control-reservation-")


def test_reserve_clock_failure_is_atomic_and_same_click_can_retry() -> None:
    clock = _FaultClock()
    runtime, conversation_ref, accepted, action = _settled_runtime(clock=clock)
    clock.fail_on_call = clock.calls + 1

    with pytest.raises(RuntimeInvariantError, match="clock failed"):
        runtime.reserve_control(
            conversation_ref=conversation_ref,
            actor_context_ref="actor-1",
            turn_ref=accepted.turn_ref,
            run_ref=accepted.run_ref,
            generation=accepted.generation,
            module_ref="fixture.control",
            module_state_ref="state-1",
            state_revision=1,
            issuance_request_id=_ISSUE_REQUEST,
            click_request_id=_CLICK_REQUEST,
            kind="action_offer",
            expires_at=action.expires_at,
        )

    clock.fail_on_call = None
    reservation_ref = runtime.reserve_control(
        conversation_ref=conversation_ref,
        actor_context_ref="actor-1",
        turn_ref=accepted.turn_ref,
        run_ref=accepted.run_ref,
        generation=accepted.generation,
        module_ref="fixture.control",
        module_state_ref="state-1",
        state_revision=1,
        issuance_request_id=_ISSUE_REQUEST,
        click_request_id=_CLICK_REQUEST,
        kind="action_offer",
        expires_at=action.expires_at,
    )
    assert reservation_ref.startswith("control-reservation-")


def test_apply_result_clock_failure_uses_reservation_time_and_does_not_leave_busy() -> None:
    clock = _FaultClock()
    runtime, conversation_ref, accepted, action = _settled_runtime(clock=clock)
    reservation_ref = runtime.reserve_control(
        conversation_ref=conversation_ref,
        actor_context_ref="actor-1",
        turn_ref=accepted.turn_ref,
        run_ref=accepted.run_ref,
        generation=accepted.generation,
        module_ref="fixture.control",
        module_state_ref="state-1",
        state_revision=1,
        issuance_request_id=_ISSUE_REQUEST,
        click_request_id=_CLICK_REQUEST,
        kind="action_offer",
        expires_at=action.expires_at,
    )
    hidden = action.model_copy(
        update={
            "kind": "result_check_handle",
            "safe_label": "核验结果",
            "manual_review_on_expiry": True,
        }
    )
    runtime.attach_control_reconciliation(
        conversation_ref=conversation_ref,
        actor_context_ref="actor-1",
        reservation_ref=reservation_ref,
        control=hidden,
    )
    runtime.mark_control_invoked(
        conversation_ref=conversation_ref,
        actor_context_ref="actor-1",
        reservation_ref=reservation_ref,
    )
    clock.fail_on_call = clock.calls + 1

    completed = runtime.apply_control_result(
        conversation_ref=conversation_ref,
        actor_context_ref="actor-1",
        turn_ref=accepted.turn_ref,
        run_ref=accepted.run_ref,
        generation=accepted.generation,
        previous_module_ref="fixture.control",
        previous_module_state_ref="state-1",
        previous_state_revision=1,
        reservation_ref=reservation_ref,
        next_module_state_ref="state-2",
        next_state_revision=2,
        display_kind="text",
        display_payload={"text": "新结果"},
        controls=(),
    )

    clock.fail_on_call = None
    assert completed.display_ledger[0].display_payload == {"text": "新结果"}
    assert completed.module_states[0].module_state_ref == "state-2"
    future = runtime.accept_turn(conversation_ref, "actor-1", UUID(int=99), "未来一句")
    assert future.generation == accepted.generation + 1


def test_reconciliation_clock_failure_fails_safe_manual_and_does_not_leave_busy() -> None:
    clock = _FaultClock()
    runtime, conversation_ref, accepted, action = _settled_runtime(clock=clock)
    reservation_ref = runtime.reserve_control(
        conversation_ref=conversation_ref,
        actor_context_ref="actor-1",
        turn_ref=accepted.turn_ref,
        run_ref=accepted.run_ref,
        generation=accepted.generation,
        module_ref="fixture.control",
        module_state_ref="state-1",
        state_revision=1,
        issuance_request_id=_ISSUE_REQUEST,
        click_request_id=_CLICK_REQUEST,
        kind="action_offer",
        expires_at=action.expires_at,
    )
    hidden = action.model_copy(
        update={
            "kind": "result_check_handle",
            "safe_label": "核验结果",
            "manual_review_on_expiry": True,
        }
    )
    runtime.attach_control_reconciliation(
        conversation_ref=conversation_ref,
        actor_context_ref="actor-1",
        reservation_ref=reservation_ref,
        control=hidden,
    )
    runtime.mark_control_invoked(
        conversation_ref=conversation_ref,
        actor_context_ref="actor-1",
        reservation_ref=reservation_ref,
    )
    clock.fail_on_call = clock.calls + 1

    reconciled = runtime.apply_control_reconciliation(
        conversation_ref=conversation_ref,
        actor_context_ref="actor-1",
        reservation_ref=reservation_ref,
    )

    clock.fail_on_call = None
    assert reconciled.display_ledger[0].controls == ()
    assert reconciled.display_ledger[0].display_payload == {"text": "待人工核对"}
    future = runtime.accept_turn(conversation_ref, "actor-1", UUID(int=100), "未来一句")
    assert future.generation == accepted.generation + 1
