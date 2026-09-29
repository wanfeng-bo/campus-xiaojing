from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from math import isfinite
from threading import RLock
from typing import Literal, Never, cast
from uuid import UUID, uuid4

from .contracts import JsonObject, NonNullJsonValue, PublicControlV1

type Clock = Callable[[], datetime]
type RefFactory = Callable[[str], str]


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _random_ref(kind: str) -> str:
    return f"{kind}-{uuid4().hex}"


class AssistantRuntimeError(RuntimeError):
    """固定、安全的机械运行时错误基类。"""


class AssistantServiceError(AssistantRuntimeError):
    """Public technical category only; never contains vendor bodies or credentials."""

    def __init__(self, message: str, *, reason: str = "dependency") -> None:
        super().__init__(message)
        self.reason = (
            reason
            if reason
            in {"dependency", "tool_limit", "deadline", "budget", "context_limit", "output_limit", "data_policy"}
            else "dependency"
        )


class ConversationAccessError(AssistantRuntimeError):
    """对话不存在或不属于当前服务端登录上下文。"""


class ConversationBusyError(AssistantRuntimeError):
    """当前对话已有一个 active run。"""


class ConversationClosedError(AssistantRuntimeError):
    """Closed conversations retain history but cannot accept another turn."""


class StaleRunError(AssistantRuntimeError):
    """run 或 generation 已不再是当前值。"""


class InvalidRuntimeInputError(AssistantRuntimeError):
    """机械输入不满足固定形状。"""


class RuntimeInvariantError(AssistantRuntimeError):
    """服务端时钟或引用生成器破坏运行时不变量。"""


@dataclass(frozen=True, slots=True)
class AcceptedRun:
    conversation_ref: str
    turn_ref: str
    run_ref: str
    generation: int
    request_id: UUID
    teacher_utterance: str
    accepted_at: datetime
    continuation_of: str | None = None
    continuation_evidence: ContinuationEvidence | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class CausalLedgerEntry:
    conversation_ref: str
    turn_ref: str | None
    run_ref: str | None
    generation: int
    request_id: UUID
    state: Literal[
        "conversation_opened",
        "conversation_closed",
        "run_active",
        "run_completed",
        "run_stopped",
        "control_active",
        "control_completed",
        "control_reconciliation",
    ]
    occurred_at: datetime
    result_ref: str | None = None


@dataclass(frozen=True, slots=True)
class DisplayLedgerEntry:
    turn_ref: str
    run_ref: str
    generation: int
    teacher_utterance: str
    visible_state: Literal["active", "completed", "stopped"]
    display_kind: Literal["text", "card"] | None = None
    display_payload: NonNullJsonValue | None = None
    controls: tuple[PublicControlV1, ...] = ()
    continuation_of: str | None = None


class HistoryCheck:
    """Owner-supplied read-only check; memory-only and never a public model contract."""

    __slots__ = ("_check", "_refresh", "_key", "_revalidation_covers_content")

    def __init__(
        self,
        refresh: Callable[[], bool],
        *,
        revalidate: Callable[[], bool] | None = None,
        key: str | None = None,
        revalidation_covers_content: bool = False,
    ) -> None:
        self._revalidation_covers_content = revalidation_covers_content is True
        self._refresh = refresh
        self._check = refresh if revalidate is None else revalidate
        self._key: str | object = self if key is None else key

    @property
    def revalidation_covers_content(self) -> bool:
        """Owner opt-in: valid checks all content dependencies, not authority alone."""
        return self._revalidation_covers_content

    @property
    def key(self) -> object:
        return self._key

    def refresh(self) -> bool:
        try:
            return self._refresh() is True
        except Exception:
            return False

    def valid(self) -> bool:
        try:
            return self._check() is True
        except Exception:
            return False

    def __repr__(self) -> str:
        return "HistoryCheck(<private>)"

    def __reduce_ex__(self, protocol: object) -> Never:
        del protocol
        raise TypeError("history authorization cannot be persisted")


@dataclass(frozen=True, slots=True, repr=False)
class ContinuationEvidence:
    """Owner-projected original execution result, never an execution credential."""

    model_content: JsonObject
    fact_card: JsonObject
    history_check: HistoryCheck


@dataclass(frozen=True, slots=True, repr=False)
class ResultContinuation:
    reference: str
    load: Callable[[], ContinuationEvidence]


@dataclass(slots=True, repr=False)
class _Continuation:
    origin: AcceptedRun
    reference: str
    load: Callable[[], ContinuationEvidence]
    claimed: bool = False


class PrivateNativeHistory(ABC):
    """One opaque, nonpersistent model-owned history value; absent from snapshots."""

    @abstractmethod
    def fork(self) -> PrivateNativeHistory: ...

    @abstractmethod
    def discard(self) -> None: ...

    def __repr__(self) -> str:
        return "PrivateNativeHistory(<private>)"

    def __reduce_ex__(self, protocol: object) -> Never:
        del protocol
        raise TypeError("private native history cannot be persisted")


class PrivateHistoryRecoveryMarker(PrivateNativeHistory):
    """Contract boundary only: contains no messages, facts, or authority to release them.

    A completed turn may retain this marker even when its own material has no
    reusable evidence. Owners must revalidate prior ledger material independently.
    Identity, stop, expiry, and private-history lease rules still apply.
    """


@dataclass(frozen=True, slots=True, repr=False)
class ToolHistoryProjection:
    """One immutable owner projection and its original proof, only in memory."""

    material_json: str
    history_check: HistoryCheck = field(repr=False)

    def __repr__(self) -> str:
        return "ToolHistoryProjection(<private>)"

    def __copy__(self) -> ToolHistoryProjection:
        return self

    def __deepcopy__(self, memo: object) -> ToolHistoryProjection:
        del memo
        return self  # Frozen text and the original proof must stay bound.

    def __reduce_ex__(self, protocol: object) -> Never:
        del protocol
        raise TypeError("private tool history cannot be persisted")


@dataclass(frozen=True, slots=True)
class ModelSafeLedgerEntry:
    turn_ref: str
    run_ref: str
    generation: int
    teacher_utterance: str
    technical_marker: Literal["completed", "stopped"]
    model_safe_note: str | None = None
    model_safe_template: str | None = None
    model_history_checks: tuple[HistoryCheck, ...] | None = field(default=None, repr=False)
    tool_history: tuple[ToolHistoryProjection, ...] = field(default=(), repr=False)


@dataclass(frozen=True, slots=True)
class RunSettlement:
    """已完成链路交给运行时一次结算的机械结果。"""

    display_payload: NonNullJsonValue
    display_kind: Literal["text", "card"] = "text"
    result_ref: str | None = None
    model_safe_note: str | None = None
    model_safe_template: str | None = None
    model_history_checks: tuple[HistoryCheck, ...] | None = field(default=None, repr=False)
    native_history: PrivateNativeHistory | None = field(default=None, repr=False)
    module_ref: str | None = None
    module_state_ref: str | None = None
    state_revision: int | None = None
    controls: tuple[PublicControlV1, ...] = ()
    issuance_request_id: UUID | None = None
    tool_history: tuple[ToolHistoryProjection, ...] = field(default=(), repr=False)


@dataclass(frozen=True, slots=True)
class ModuleStateBinding:
    module_ref: str
    module_state_ref: str
    state_revision: int
    turn_ref: str
    run_ref: str
    generation: int
    issuance_request_id: UUID


@dataclass(slots=True)
class _ActiveControl:
    reservation_ref: str
    kind: Literal["action_offer", "recovery_control", "result_check_handle"]
    binding: ModuleStateBinding
    click_request_id: UUID
    reserved_at: datetime
    source_expires_at: int
    reconciliation_control: PublicControlV1 | None = None
    invocation_started: bool = False
    reconciliation_expired: bool = False


@dataclass(frozen=True, slots=True)
class ConversationSnapshot:
    conversation_ref: str
    actor_context_ref: str
    generation: int
    active_run: AcceptedRun | None
    causal_ledger: tuple[CausalLedgerEntry, ...]
    display_ledger: tuple[DisplayLedgerEntry, ...]
    model_safe_ledger: tuple[ModelSafeLedgerEntry, ...]
    module_states: tuple[ModuleStateBinding, ...]


@dataclass(slots=True)
class _ConversationState:
    conversation_ref: str
    actor_context_ref: str
    generation: int = 0
    closed: bool = False
    active_run: AcceptedRun | None = None
    causal_ledger: list[CausalLedgerEntry] = field(
        default_factory=lambda: list[CausalLedgerEntry]()
    )
    display_ledger: list[DisplayLedgerEntry] = field(
        default_factory=lambda: list[DisplayLedgerEntry]()
    )
    model_safe_ledger: list[ModelSafeLedgerEntry] = field(
        default_factory=lambda: list[ModelSafeLedgerEntry]()
    )
    native_history: tuple[str, datetime, PrivateNativeHistory] | None = field(
        default=None, repr=False
    )
    used_turn_refs: set[str] = field(default_factory=lambda: set[str]())
    used_run_refs: set[str] = field(default_factory=lambda: set[str]())
    module_states: dict[str, ModuleStateBinding] = field(
        default_factory=lambda: dict[str, ModuleStateBinding]()
    )
    active_control: _ActiveControl | None = None
    used_control_request_ids: set[UUID] = field(default_factory=lambda: set[UUID]())
    continuations: dict[str, _Continuation] = field(default_factory=dict, repr=False)


class AssistantRuntime:
    """未挂接的最小机械内核：一次一句、generation 和三账本。"""

    def __init__(
        self,
        *,
        clock: Clock | None = None,
        ref_factory: RefFactory | None = None,
        retention_seconds: int | None = None,
        max_conversations: int | None = None,
    ) -> None:
        self._clock: Clock = clock or _utc_now
        self._ref_factory: RefFactory = ref_factory or _random_ref
        self._lock = RLock()
        self._conversations: dict[str, _ConversationState] = {}
        for value in (retention_seconds, max_conversations):
            if value is not None and (type(value) is not int or value <= 0):
                raise ValueError("runtime bounds must be explicit positive integers")
        self._retention_seconds = retention_seconds
        self._max_conversations = max_conversations

    def create_conversation(
        self,
        actor_context_ref: str,
        request_id: UUID,
    ) -> ConversationSnapshot:
        self._require_opaque_ref(actor_context_ref, "actor context")
        self._require_request_id(request_id)
        with self._lock:
            self.purge_expired()
            if (
                self._max_conversations is not None
                and len(self._conversations) >= self._max_conversations
            ):
                raise RuntimeInvariantError("runtime conversation capacity reached")
            conversation_ref = self._new_ref("conversation")
            if conversation_ref in self._conversations:
                raise RuntimeInvariantError("generated reference is not unique")
            occurred_at = self._now()
            state = _ConversationState(
                conversation_ref=conversation_ref,
                actor_context_ref=actor_context_ref,
            )
            state.causal_ledger.append(
                CausalLedgerEntry(
                    conversation_ref=conversation_ref,
                    turn_ref=None,
                    run_ref=None,
                    generation=0,
                    request_id=request_id,
                    state="conversation_opened",
                    occurred_at=occurred_at,
                )
            )
            self._conversations[conversation_ref] = state
            return self._snapshot(state)

    def accept_turn(
        self,
        conversation_ref: str,
        actor_context_ref: str,
        request_id: UUID,
        teacher_utterance: str,
    ) -> AcceptedRun:
        self._require_request_id(request_id)
        with self._lock:
            state = self._owned_state(conversation_ref, actor_context_ref)
            if state.closed:
                raise ConversationClosedError("conversation is closed")
            self._expire_controls(state)
            if state.active_run is not None or state.active_control is not None:
                raise ConversationBusyError("conversation is busy")
            self._require_teacher_utterance(teacher_utterance)
            turn_ref = self._new_ref("turn")
            run_ref = self._new_ref("run")
            if turn_ref in state.used_turn_refs or run_ref in state.used_run_refs:
                raise RuntimeInvariantError("generated reference is not unique")
            generation = state.generation + 1
            accepted_at = self._now()
            active_run = AcceptedRun(
                conversation_ref=conversation_ref,
                turn_ref=turn_ref,
                run_ref=run_ref,
                generation=generation,
                request_id=request_id,
                teacher_utterance=teacher_utterance,
                accepted_at=accepted_at,
            )
            causal_entry = CausalLedgerEntry(
                conversation_ref=conversation_ref,
                turn_ref=turn_ref,
                run_ref=run_ref,
                generation=generation,
                request_id=request_id,
                state="run_active",
                occurred_at=accepted_at,
            )
            display_entry = DisplayLedgerEntry(
                turn_ref=turn_ref,
                run_ref=run_ref,
                generation=generation,
                teacher_utterance=teacher_utterance,
                visible_state="active",
            )

            state.generation = generation
            state.active_run = active_run
            state.used_turn_refs.add(turn_ref)
            state.used_run_refs.add(run_ref)
            state.causal_ledger.append(causal_entry)
            state.display_ledger.append(display_entry)
            return active_run

    def register_continuation(
        self,
        accepted: AcceptedRun,
        actor_context_ref: str,
        reference: str,
        loader: Callable[[], ContinuationEvidence],
    ) -> str:
        """Only a trusted capability can attach a read-only result loader to its turn."""
        with self._lock:
            state = self._owned_state(accepted.conversation_ref, actor_context_ref)
            self._current_run(state, accepted.run_ref, accepted.generation)
            for handle, item in state.continuations.items():
                if item.origin.run_ref == accepted.run_ref and item.reference == reference:
                    return handle
            handle = self._ref_factory("continuation")
            if handle in state.continuations:
                raise RuntimeInvariantError("duplicate continuation reference")
            while len(state.continuations) >= 32:
                del state.continuations[next(iter(state.continuations))]
            state.continuations[handle] = _Continuation(accepted, reference, loader)
            return handle

    def continuation_loader(
        self,
        conversation_ref: str,
        actor_context_ref: str,
        handle: str,
    ) -> Callable[[], ContinuationEvidence] | None:
        with self._lock:
            state = self._owned_state(conversation_ref, actor_context_ref)
            item = state.continuations.get(handle)
            if item is None or item.claimed:
                return None
            if (
                state.closed
                or state.generation != item.origin.generation
                or state.active_run is not None
                or state.active_control is not None
                or not any(
                    entry.run_ref == item.origin.run_ref and entry.visible_state == "completed"
                    for entry in state.display_ledger
                )
            ):
                raise StaleRunError("original task is no longer current")
            return item.load

    def accept_continuation(
        self,
        conversation_ref: str,
        actor_context_ref: str,
        handle: str,
        request_id: UUID,
        *,
        evidence: ContinuationEvidence | None = None,
    ) -> AcceptedRun | None:
        # The domain read happens outside this lock. Recheck task generation when
        # claiming it: concurrent clicks can read, but only one can start a model run.
        with self._lock:
            if self.continuation_loader(conversation_ref, actor_context_ref, handle) is None:
                return None
            state = self._owned_state(conversation_ref, actor_context_ref)
            item = state.continuations[handle]
            accepted = self.accept_turn(
                conversation_ref,
                actor_context_ref,
                request_id,
                item.origin.teacher_utterance,
            )
            accepted = replace(
                accepted, continuation_of=item.origin.turn_ref, continuation_evidence=evidence
            )
            item.claimed = True
            state.active_run = accepted
            state.display_ledger[-1] = replace(
                state.display_ledger[-1],
                continuation_of=item.origin.turn_ref,
            )
            return accepted

    def settle_run(
        self,
        conversation_ref: str,
        actor_context_ref: str,
        run_ref: str,
        generation: int,
        request_id: UUID,
        settlement: RunSettlement,
    ) -> ConversationSnapshot:
        self._require_request_id(request_id)
        with self._lock:
            state = self._owned_state(conversation_ref, actor_context_ref)
            active = self._current_run(state, run_ref, generation)
            self._require_display_kind(settlement.display_kind)
            display_payload = self._copy_json(settlement.display_payload)
            self._require_optional_safe_text(settlement.model_safe_note)
            self._require_optional_safe_text(settlement.model_safe_template)
            if (
                type(settlement.tool_history) is not tuple
                or len(settlement.tool_history) > 16
                or any(
                    not isinstance(item, ToolHistoryProjection)
                    or not isinstance(item.material_json, str)
                    or not isinstance(item.history_check, HistoryCheck)
                    for item in settlement.tool_history
                )
            ):
                raise InvalidRuntimeInputError("invalid tool history")
            if settlement.tool_history and (
                len(settlement.model_safe_note or "")
                + sum(len(item.material_json) for item in settlement.tool_history)
                > 8000
            ):
                raise InvalidRuntimeInputError("tool history exceeds material budget")
            if settlement.model_history_checks is not None and (
                type(settlement.model_history_checks) is not tuple
                or len(settlement.model_history_checks) > 128
                or any(
                    not isinstance(cast(object, check), HistoryCheck)
                    for check in settlement.model_history_checks
                )
            ):
                raise InvalidRuntimeInputError("invalid history authorization")
            if settlement.native_history is not None and (
                not isinstance(cast(object, settlement.native_history), PrivateNativeHistory)
                or (
                    settlement.model_history_checks is None
                    and not isinstance(settlement.native_history, PrivateHistoryRecoveryMarker)
                )
            ):
                raise InvalidRuntimeInputError("invalid private history authorization")
            self._require_controls(settlement.controls)
            has_any_module_state = any(
                value is not None
                for value in (
                    settlement.module_ref,
                    settlement.module_state_ref,
                    settlement.state_revision,
                )
            )
            has_all_module_state = all(
                value is not None
                for value in (
                    settlement.module_ref,
                    settlement.module_state_ref,
                    settlement.state_revision,
                )
            )
            if has_any_module_state != has_all_module_state:
                raise InvalidRuntimeInputError("module state binding is invalid")
            if has_all_module_state:
                self._require_module_state(
                    settlement.module_ref,
                    settlement.module_state_ref,
                    settlement.state_revision,
                )
                if settlement.issuance_request_id is not None:
                    self._require_request_id(settlement.issuance_request_id)
                    if settlement.issuance_request_id != request_id:
                        raise InvalidRuntimeInputError("issuance request id is invalid")
            if settlement.result_ref is not None:
                self._require_opaque_ref(settlement.result_ref, "result")
            occurred_at = self._now()
            display_index = self._display_index(state, active)
            display_entry = replace(
                state.display_ledger[display_index],
                visible_state="completed",
                display_kind=settlement.display_kind,
                display_payload=display_payload,
                controls=settlement.controls,
            )
            model_entry = ModelSafeLedgerEntry(
                turn_ref=active.turn_ref,
                run_ref=active.run_ref,
                generation=active.generation,
                teacher_utterance=active.teacher_utterance,
                technical_marker="completed",
                model_safe_note=settlement.model_safe_note,
                model_safe_template=settlement.model_safe_template,
                model_history_checks=settlement.model_history_checks,
                tool_history=settlement.tool_history,
            )
            causal_entry = CausalLedgerEntry(
                conversation_ref=conversation_ref,
                turn_ref=active.turn_ref,
                run_ref=active.run_ref,
                generation=state.generation,
                request_id=request_id,
                state="run_completed",
                occurred_at=occurred_at,
                result_ref=settlement.result_ref,
            )

            if has_all_module_state:
                module_ref = cast(str, settlement.module_ref)
                previous = state.module_states.get(module_ref)
                next_revision = cast(int, settlement.state_revision)
                if previous is not None and next_revision > previous.state_revision:
                    self._remove_prior_recovery_controls(state, module_ref)
            self._discard_native_history(state)
            if settlement.native_history is not None:
                # Ownership transfers only after all settlement validation succeeded.
                state.native_history = (active.turn_ref, occurred_at, settlement.native_history)
            state.display_ledger[display_index] = display_entry
            state.model_safe_ledger.append(model_entry)
            state.causal_ledger.append(causal_entry)
            if has_all_module_state:
                module_ref = cast(str, settlement.module_ref)
                next_revision = cast(int, settlement.state_revision)
                state.module_states[module_ref] = ModuleStateBinding(
                    module_ref=module_ref,
                    module_state_ref=cast(str, settlement.module_state_ref),
                    state_revision=next_revision,
                    turn_ref=active.turn_ref,
                    run_ref=active.run_ref,
                    generation=active.generation,
                    issuance_request_id=request_id,
                )
            state.active_run = None
            return self._snapshot(state)

    def reserve_control(
        self,
        *,
        conversation_ref: str,
        actor_context_ref: str,
        turn_ref: str,
        run_ref: str,
        generation: int,
        module_ref: str,
        module_state_ref: str,
        state_revision: int,
        issuance_request_id: UUID,
        click_request_id: UUID,
        kind: Literal["action_offer", "recovery_control", "result_check_handle"],
        expires_at: int,
    ) -> str:
        """在机械验收和外部模块调用之间建立进程内短租约。"""

        self._require_request_id(issuance_request_id)
        self._require_request_id(click_request_id)
        if type(expires_at) is not int or expires_at < 0:
            raise InvalidRuntimeInputError("control expiry is invalid")
        with self._lock:
            state = self._owned_state(conversation_ref, actor_context_ref)
            if state.closed and kind != "result_check_handle":
                raise ConversationClosedError("conversation is closed")
            occurred_at = self._now()
            now_epoch = int(occurred_at.timestamp())
            self._expire_controls_at(state, now_epoch)
            if now_epoch >= expires_at:
                raise StaleRunError("run is not current")
            if (
                state.active_control is not None
                or click_request_id in state.used_control_request_ids
            ):
                raise StaleRunError("run is not current")
            self._require_current_control_binding(
                state,
                turn_ref=turn_ref,
                run_ref=run_ref,
                generation=generation,
                module_ref=module_ref,
                module_state_ref=module_state_ref,
                state_revision=state_revision,
                issuance_request_id=issuance_request_id,
            )
            reservation_ref = self._new_ref("control-reservation")
            binding = cast(ModuleStateBinding, state.module_states.get(module_ref))
            active_control = _ActiveControl(
                reservation_ref=reservation_ref,
                kind=kind,
                binding=binding,
                click_request_id=click_request_id,
                reserved_at=occurred_at,
                source_expires_at=expires_at,
            )
            causal_entry = CausalLedgerEntry(
                conversation_ref=conversation_ref,
                turn_ref=turn_ref,
                run_ref=run_ref,
                generation=generation,
                request_id=click_request_id,
                state="control_active",
                occurred_at=occurred_at,
            )
            state.active_control = active_control
            state.used_control_request_ids.add(click_request_id)
            state.causal_ledger.append(causal_entry)
            return reservation_ref

    def retire_consumed_control(
        self,
        *,
        conversation_ref: str,
        actor_context_ref: str,
        reservation_ref: str,
    ) -> None:
        """外部原子消费成功后，机械收起与该租约精确匹配的源按钮。"""

        with self._lock:
            state = self._owned_state(conversation_ref, actor_context_ref)
            active = self._active_control(state, reservation_ref)
            binding = active.binding
            display_index = self._completed_display_index(
                state,
                binding.turn_ref,
                binding.run_ref,
                binding.generation,
            )
            entry = state.display_ledger[display_index]
            matches = tuple(
                control
                for control in entry.controls
                if control.kind == active.kind
                and control.module_ref == binding.module_ref
                and control.expires_at == active.source_expires_at
            )
            if len(matches) != 1:
                raise RuntimeInvariantError("consumed control display record is missing")
            retained = tuple(control for control in entry.controls if control is not matches[0])
            state.display_ledger[display_index] = replace(entry, controls=retained)

    def attach_control_reconciliation(
        self,
        *,
        conversation_ref: str,
        actor_context_ref: str,
        reservation_ref: str,
        control: PublicControlV1,
    ) -> None:
        with self._lock:
            state = self._owned_state(conversation_ref, actor_context_ref)
            active = self._active_control(state, reservation_ref)
            if (
                control.kind != "result_check_handle"
                or not control.manual_review_on_expiry
                or control.module_ref != active.binding.module_ref
            ):
                raise InvalidRuntimeInputError("reconciliation control is invalid")
            active.reconciliation_control = control

    def mark_control_invoked(
        self,
        *,
        conversation_ref: str,
        actor_context_ref: str,
        reservation_ref: str,
    ) -> None:
        with self._lock:
            state = self._owned_state(conversation_ref, actor_context_ref)
            active = self._active_control(state, reservation_ref)
            if active.reconciliation_control is None and active.kind != "recovery_control":
                raise RuntimeInvariantError("reconciliation control is missing")
            active.invocation_started = True

    def release_control(
        self,
        *,
        conversation_ref: str,
        actor_context_ref: str,
        reservation_ref: str,
    ) -> None:
        """仅供模块调用开始前的失败路径释放租约；请求 id 仍保持已使用。"""

        with self._lock:
            state = self._owned_state(conversation_ref, actor_context_ref)
            active = self._active_control(state, reservation_ref)
            if active.invocation_started:
                raise RuntimeInvariantError("invoked control cannot be released")
            state.active_control = None

    def require_control_binding(
        self,
        *,
        conversation_ref: str,
        actor_context_ref: str,
        turn_ref: str,
        run_ref: str,
        generation: int,
        module_ref: str,
        module_state_ref: str,
        state_revision: int,
    ) -> None:
        with self._lock:
            state = self._owned_state(conversation_ref, actor_context_ref)
            self._require_current_control_binding(
                state,
                turn_ref=turn_ref,
                run_ref=run_ref,
                generation=generation,
                module_ref=module_ref,
                module_state_ref=module_state_ref,
                state_revision=state_revision,
            )

    def apply_control_result(
        self,
        *,
        conversation_ref: str,
        actor_context_ref: str,
        turn_ref: str,
        run_ref: str,
        generation: int,
        previous_module_ref: str,
        previous_module_state_ref: str,
        previous_state_revision: int,
        reservation_ref: str,
        next_module_state_ref: str,
        next_state_revision: int,
        display_kind: Literal["text", "card"],
        display_payload: NonNullJsonValue,
        controls: tuple[PublicControlV1, ...],
    ) -> ConversationSnapshot:
        with self._lock:
            state = self._owned_state(conversation_ref, actor_context_ref)
            active_control = self._active_control(state, reservation_ref)
            self._require_active_control_binding(
                active_control,
                turn_ref=turn_ref,
                run_ref=run_ref,
                generation=generation,
                module_ref=previous_module_ref,
                module_state_ref=previous_module_state_ref,
                state_revision=previous_state_revision,
            )
            if not active_control.invocation_started:
                raise StaleRunError("run is not current")
            self._require_display_kind(display_kind)
            self._require_controls(controls)
            self._require_module_state(
                previous_module_ref,
                next_module_state_ref,
                next_state_revision,
            )
            display_payload_copy = self._copy_json(display_payload)
            display_index = self._completed_display_index(state, turn_ref, run_ref, generation)
            previous_payload = state.display_ledger[display_index].display_payload
            if isinstance(previous_payload, dict) and isinstance(display_payload_copy, dict):
                links = previous_payload.get("continuations")
                if links is not None:
                    display_payload_copy["continuations"] = self._copy_json(links)
            occurred_at, _clock_available = self._control_time(active_control)
            now_epoch = int(occurred_at.timestamp())
            self._expire_controls_at(state, now_epoch)
            if active_control.reconciliation_expired:
                state.causal_ledger.append(
                    CausalLedgerEntry(
                        conversation_ref=conversation_ref,
                        turn_ref=turn_ref,
                        run_ref=run_ref,
                        generation=generation,
                        request_id=active_control.click_request_id,
                        state="control_reconciliation",
                        occurred_at=occurred_at,
                    )
                )
                state.active_control = None
                return self._snapshot(state)
            previous = state.module_states.get(previous_module_ref)
            if previous is not None and next_state_revision > previous.state_revision:
                self._remove_prior_recovery_controls(state, previous_module_ref)
            state.display_ledger[display_index] = replace(
                state.display_ledger[display_index],
                display_kind=display_kind,
                display_payload=display_payload_copy,
                controls=controls,
            )
            state.module_states[previous_module_ref] = ModuleStateBinding(
                module_ref=previous_module_ref,
                module_state_ref=next_module_state_ref,
                state_revision=next_state_revision,
                turn_ref=turn_ref,
                run_ref=run_ref,
                generation=generation,
                issuance_request_id=active_control.click_request_id,
            )
            state.causal_ledger.append(
                CausalLedgerEntry(
                    conversation_ref=conversation_ref,
                    turn_ref=turn_ref,
                    run_ref=run_ref,
                    generation=generation,
                    request_id=active_control.click_request_id,
                    state="control_completed",
                    occurred_at=occurred_at,
                )
            )
            state.active_control = None
            return self._snapshot(state)

    def apply_control_reconciliation(
        self,
        *,
        conversation_ref: str,
        actor_context_ref: str,
        reservation_ref: str,
    ) -> ConversationSnapshot:
        """模块调用已可能发生时，仅显露预建查询凭据并结束租约。"""

        with self._lock:
            state = self._owned_state(conversation_ref, actor_context_ref)
            active = self._active_control(state, reservation_ref)
            if not active.invocation_started:
                raise StaleRunError("run is not current")
            binding = active.binding
            display_index = self._completed_display_index(
                state,
                binding.turn_ref,
                binding.run_ref,
                binding.generation,
            )
            occurred_at, clock_available = self._control_time(active)
            if clock_available:
                self._expire_controls_at(state, int(occurred_at.timestamp()))
            else:
                active.reconciliation_expired = True
            controls = (
                ()
                if active.reconciliation_control is None or active.reconciliation_expired
                else (active.reconciliation_control,)
            )
            state.display_ledger[display_index] = replace(
                state.display_ledger[display_index],
                display_kind="text",
                display_payload=(
                    {"text": "待人工核对"}
                    if not controls
                    else {"text": "结果待核验，请点击核验结果。"}
                ),
                controls=controls,
            )
            state.module_states[binding.module_ref] = replace(
                binding,
                issuance_request_id=active.click_request_id,
            )
            state.causal_ledger.append(
                CausalLedgerEntry(
                    conversation_ref=conversation_ref,
                    turn_ref=binding.turn_ref,
                    run_ref=binding.run_ref,
                    generation=binding.generation,
                    request_id=active.click_request_id,
                    state="control_reconciliation",
                    occurred_at=occurred_at,
                )
            )
            state.active_control = None
            return self._snapshot(state)

    def stop_run(
        self,
        conversation_ref: str,
        actor_context_ref: str,
        run_ref: str,
        generation: int,
        request_id: UUID,
    ) -> ConversationSnapshot:
        self._require_request_id(request_id)
        with self._lock:
            state = self._owned_state(conversation_ref, actor_context_ref)
            active = self._current_run(state, run_ref, generation)
            invalidated_generation = state.generation + 1
            occurred_at = self._now()
            display_index = self._display_index(state, active)
            display_entry = replace(
                state.display_ledger[display_index],
                visible_state="stopped",
                display_kind=None,
                display_payload=None,
            )
            model_entry = ModelSafeLedgerEntry(
                turn_ref=active.turn_ref,
                run_ref=active.run_ref,
                generation=active.generation,
                teacher_utterance=active.teacher_utterance,
                technical_marker="stopped",
            )
            causal_entry = CausalLedgerEntry(
                conversation_ref=conversation_ref,
                turn_ref=active.turn_ref,
                run_ref=active.run_ref,
                generation=invalidated_generation,
                request_id=request_id,
                state="run_stopped",
                occurred_at=occurred_at,
            )

            self._discard_native_history(state)
            state.generation = invalidated_generation
            state.display_ledger[display_index] = display_entry
            state.model_safe_ledger.append(model_entry)
            state.causal_ledger.append(causal_entry)
            state.active_run = None
            return self._snapshot(state)

    def close_conversation(
        self,
        conversation_ref: str,
        actor_context_ref: str,
        request_id: UUID,
    ) -> ConversationSnapshot:
        """Seal acceptance atomically, retaining existing formal results and controls."""
        self._require_request_id(request_id)
        with self._lock:
            state = self._owned_state(conversation_ref, actor_context_ref)
            if state.active_control is not None:
                raise ConversationBusyError("conversation is busy")
            if state.closed:
                return self._snapshot(state)
            occurred_at = self._now()
            active = state.active_run
            if active is not None:
                self.stop_run(
                    conversation_ref,
                    actor_context_ref,
                    active.run_ref,
                    active.generation,
                    request_id,
                )
            self._discard_native_history(state)
            state.closed = True
            state.causal_ledger.append(
                CausalLedgerEntry(
                    conversation_ref=conversation_ref,
                    turn_ref=None,
                    run_ref=None,
                    generation=state.generation,
                    request_id=request_id,
                    state="conversation_closed",
                    occurred_at=occurred_at,
                )
            )
            return self._snapshot(state)

    def snapshot(
        self,
        conversation_ref: str,
        actor_context_ref: str,
    ) -> ConversationSnapshot:
        with self._lock:
            state = self._owned_state(conversation_ref, actor_context_ref)
            self._expire_controls(state)
            return self._snapshot(state)

    def require_current_run(
        self,
        conversation_ref: str,
        actor_context_ref: str,
        run_ref: str,
        generation: int,
    ) -> None:
        """在外部阶段切换前机械重验原 run 仍是当前活动项。"""

        with self._lock:
            state = self._owned_state(conversation_ref, actor_context_ref)
            self._current_run(state, run_ref, generation)

    @staticmethod
    def _require_request_id(request_id: object) -> None:
        if not isinstance(request_id, UUID):
            raise InvalidRuntimeInputError("request id is invalid")

    @staticmethod
    def _require_teacher_utterance(value: object) -> None:
        if (
            not isinstance(value, str)
            or len(value) > 8_000
            or not any(not char.isspace() for char in value)
        ):
            raise InvalidRuntimeInputError("teacher utterance is invalid")

    @staticmethod
    def _require_optional_safe_text(value: object | None) -> None:
        if value is None:
            return
        if (
            not isinstance(value, str)
            or len(value) > 8_000
            or not any(not char.isspace() for char in value)
        ):
            raise InvalidRuntimeInputError("model-safe text is invalid")

    @staticmethod
    def _require_display_kind(value: object) -> None:
        if not isinstance(value, str) or value not in {"text", "card"}:
            raise InvalidRuntimeInputError("display kind is invalid")

    @staticmethod
    def _require_controls(value: object) -> None:
        if not isinstance(value, tuple):
            raise InvalidRuntimeInputError("controls are invalid")
        controls = cast(tuple[object, ...], value)
        if any(not isinstance(control, PublicControlV1) for control in controls):
            raise InvalidRuntimeInputError("controls are invalid")
        typed_controls = cast(tuple[PublicControlV1, ...], controls)
        kinds = tuple(control.kind for control in typed_controls)
        if len(kinds) > 3 or len(set(kinds)) != len(kinds):
            raise InvalidRuntimeInputError("controls are invalid")

    @classmethod
    def _require_module_state(
        cls,
        module_ref: object,
        module_state_ref: object,
        state_revision: object,
    ) -> None:
        cls._require_opaque_ref(module_ref, "module")
        cls._require_opaque_ref(module_state_ref, "module state")
        if (
            not isinstance(state_revision, int)
            or isinstance(state_revision, bool)
            or state_revision < 1
        ):
            raise InvalidRuntimeInputError("module state revision is invalid")

    @staticmethod
    def _require_opaque_ref(value: object, kind: str) -> None:
        allowed = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_.:-")
        if (
            not isinstance(value, str)
            or not 1 <= len(value) <= 200
            or any(character not in allowed for character in value)
        ):
            raise InvalidRuntimeInputError(f"{kind} reference is invalid")

    def _new_ref(self, kind: str) -> str:
        try:
            value = self._ref_factory(kind)
        except Exception:
            raise RuntimeInvariantError("reference generation failed") from None
        try:
            self._require_opaque_ref(value, "generated")
        except InvalidRuntimeInputError:
            raise RuntimeInvariantError("generated reference is invalid") from None
        return value

    def _now(self) -> datetime:
        try:
            value = cast(object, self._clock())
        except Exception:
            raise RuntimeInvariantError("clock failed") from None
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise RuntimeInvariantError("clock must return an aware datetime")
        return value

    def _control_time(self, active: _ActiveControl) -> tuple[datetime, bool]:
        try:
            return self._now(), True
        except RuntimeInvariantError:
            return active.reserved_at, False

    def copy_native_history(
        self,
        conversation_ref: str,
        actor_context_ref: str,
        run_ref: str,
        generation: int,
    ) -> tuple[str, PrivateNativeHistory, HistoryCheck] | None:
        """Lease an independent copy only to the current run in its owned conversation."""
        with self._lock:
            state = self._owned_state(conversation_ref, actor_context_ref)
            self._current_run(state, run_ref, generation)
            native = state.native_history
            if native is None:
                return None
            turn_ref, _, value = native
            if not state.model_safe_ledger or state.model_safe_ledger[-1].turn_ref != turn_ref:
                self._discard_native_history(state)
                return None

            def still_retained() -> bool:
                with self._lock:
                    current = self._owned_state(conversation_ref, actor_context_ref)
                    self._current_run(current, run_ref, generation)
                    return current.native_history is native

            try:
                return turn_ref, value.fork(), HistoryCheck(still_retained)
            except Exception:
                self._discard_native_history(state)
                return None

    @staticmethod
    def _discard_native_history(state: _ConversationState) -> None:
        native = state.native_history
        state.native_history = None
        if native is not None:
            native[2].discard()

    def purge_expired(self, *, now: datetime | None = None) -> int:
        with self._lock:
            if self._retention_seconds is None and not any(
                state.native_history is not None for state in self._conversations.values()
            ):
                return 0
            observed = now or self._now()
            for state in self._conversations.values():
                if (
                    state.native_history is not None
                    and (observed - state.native_history[1]).total_seconds() >= 86_400
                ):
                    self._discard_native_history(state)
            if self._retention_seconds is None:
                return 0
            expired = [
                ref
                for ref, state in self._conversations.items()
                if (observed - state.causal_ledger[0].occurred_at).total_seconds()
                >= self._retention_seconds
            ]
            for ref in expired:
                self._discard_native_history(self._conversations[ref])
                del self._conversations[ref]
            return len(expired)

    def _owned_state(
        self,
        conversation_ref: str,
        actor_context_ref: str,
    ) -> _ConversationState:
        self.purge_expired()
        state = self._conversations.get(conversation_ref)
        if state is None or state.actor_context_ref != actor_context_ref:
            raise ConversationAccessError("conversation is unavailable")
        return state

    @staticmethod
    def _current_run(
        state: _ConversationState,
        run_ref: str,
        generation: int,
    ) -> AcceptedRun:
        active = state.active_run
        if (
            active is None
            or active.run_ref != run_ref
            or active.generation != generation
            or state.generation != generation
        ):
            raise StaleRunError("run is not current")
        return active

    @staticmethod
    def _display_index(state: _ConversationState, active: AcceptedRun) -> int:
        for index in range(len(state.display_ledger) - 1, -1, -1):
            entry = state.display_ledger[index]
            if entry.run_ref == active.run_ref and entry.visible_state == "active":
                return index
        raise RuntimeInvariantError("active display record is missing")

    @staticmethod
    def _completed_display_index(
        state: _ConversationState,
        turn_ref: str,
        run_ref: str,
        generation: int,
    ) -> int:
        for index in range(len(state.display_ledger) - 1, -1, -1):
            entry = state.display_ledger[index]
            if (
                entry.turn_ref == turn_ref
                and entry.run_ref == run_ref
                and entry.generation == generation
                and entry.visible_state == "completed"
            ):
                return index
        raise StaleRunError("run is not current")

    @classmethod
    def _require_current_control_binding(
        cls,
        state: _ConversationState,
        *,
        turn_ref: str,
        run_ref: str,
        generation: int,
        module_ref: str,
        module_state_ref: str,
        state_revision: int,
        issuance_request_id: UUID | None = None,
    ) -> None:
        current = state.module_states.get(module_ref)
        if (
            state.active_run is not None
            or state.generation != generation
            or current is None
            or current.module_state_ref != module_state_ref
            or current.state_revision != state_revision
            or current.turn_ref != turn_ref
            or current.run_ref != run_ref
            or current.generation != generation
            or (
                issuance_request_id is not None
                and current.issuance_request_id != issuance_request_id
            )
        ):
            raise StaleRunError("run is not current")
        cls._completed_display_index(state, turn_ref, run_ref, generation)

    @staticmethod
    def _require_active_control_binding(
        active: _ActiveControl,
        *,
        turn_ref: str,
        run_ref: str,
        generation: int,
        module_ref: str,
        module_state_ref: str,
        state_revision: int,
    ) -> None:
        binding = active.binding
        if (
            binding.turn_ref != turn_ref
            or binding.run_ref != run_ref
            or binding.generation != generation
            or binding.module_ref != module_ref
            or binding.module_state_ref != module_state_ref
            or binding.state_revision != state_revision
        ):
            raise StaleRunError("run is not current")

    @staticmethod
    def _active_control(state: _ConversationState, reservation_ref: str) -> _ActiveControl:
        active = state.active_control
        if active is None or active.reservation_ref != reservation_ref:
            raise StaleRunError("run is not current")
        return active

    @staticmethod
    def _remove_prior_recovery_controls(
        state: _ConversationState,
        module_ref: str,
    ) -> None:
        for index, entry in enumerate(state.display_ledger):
            retained = tuple(
                control
                for control in entry.controls
                if not (control.kind == "recovery_control" and control.module_ref == module_ref)
            )
            if retained != entry.controls:
                state.display_ledger[index] = replace(entry, controls=retained)

    def _expire_controls(self, state: _ConversationState) -> None:
        now = int(self._now().timestamp())
        self._expire_controls_at(state, now)

    @staticmethod
    def _expire_controls_at(state: _ConversationState, now: int) -> None:
        for index, entry in enumerate(state.display_ledger):
            expired = tuple(control for control in entry.controls if control.expires_at <= now)
            if not expired:
                continue
            retained = tuple(control for control in entry.controls if control.expires_at > now)
            manual_review = any(control.manual_review_on_expiry for control in expired)
            state.display_ledger[index] = replace(
                entry,
                display_kind="text" if manual_review else entry.display_kind,
                display_payload={"text": "待人工核对"} if manual_review else entry.display_payload,
                controls=retained,
            )
        active = state.active_control
        if (
            active is None
            or active.reconciliation_control is None
            or active.reconciliation_expired
            or active.reconciliation_control.expires_at > now
        ):
            return
        active.reconciliation_expired = True
        try:
            display_index = AssistantRuntime._completed_display_index(
                state,
                active.binding.turn_ref,
                active.binding.run_ref,
                active.binding.generation,
            )
        except StaleRunError:
            return
        entry = state.display_ledger[display_index]
        state.display_ledger[display_index] = replace(
            entry,
            display_kind="text",
            display_payload={"text": "待人工核对"},
            controls=(),
        )

    @classmethod
    def _copy_json(cls, value: object) -> NonNullJsonValue:
        result = cls._canonical_json(value, seen=frozenset(), depth=0)
        if result is None:
            raise InvalidRuntimeInputError("display payload is invalid")
        return result

    @classmethod
    def _canonical_json(
        cls,
        value: object,
        *,
        seen: frozenset[int],
        depth: int,
    ) -> NonNullJsonValue | None:
        if depth > 64:
            raise InvalidRuntimeInputError("display payload is invalid")
        if value is None:
            return None
        if type(value) is str:
            return value
        if type(value) is bool:
            return value
        if type(value) is int:
            return value
        if type(value) is float:
            float_value = value
            if not isfinite(float_value):
                raise InvalidRuntimeInputError("display payload is invalid")
            return float_value
        if type(value) in {list, dict}:
            identity = id(value)
            if identity in seen:
                raise InvalidRuntimeInputError("display payload is invalid")
            nested_seen = seen | {identity}
            if type(value) is list:
                sequence = cast(list[object], value)
                canonical_items = [
                    cls._canonical_json(child, seen=nested_seen, depth=depth + 1)
                    for child in sequence
                ]
                return cast(NonNullJsonValue, canonical_items)
            mapping = cast(dict[object, object], value)
            canonical_mapping: dict[str, object] = {}
            for key, child in mapping.items():
                if type(key) is not str:
                    raise InvalidRuntimeInputError("display payload is invalid")
                canonical_mapping[key] = cls._canonical_json(
                    child,
                    seen=nested_seen,
                    depth=depth + 1,
                )
            return cast(NonNullJsonValue, canonical_mapping)
        raise InvalidRuntimeInputError("display payload is invalid")

    @staticmethod
    def _snapshot(state: _ConversationState) -> ConversationSnapshot:
        return ConversationSnapshot(
            conversation_ref=state.conversation_ref,
            actor_context_ref=state.actor_context_ref,
            generation=state.generation,
            active_run=state.active_run,
            causal_ledger=tuple(state.causal_ledger),
            display_ledger=tuple(
                replace(
                    entry,
                    display_payload=(
                        None
                        if entry.display_payload is None
                        else AssistantRuntime._copy_json(entry.display_payload)
                    ),
                )
                for entry in state.display_ledger
            ),
            model_safe_ledger=tuple(state.model_safe_ledger),
            module_states=tuple(state.module_states[key] for key in sorted(state.module_states)),
        )
