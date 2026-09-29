"""Memory-only, complete-turn history for the PydanticAI adapter.

Only the caller's domain-owned history projections may replace tool material in
this retained copy. Current model messages and required thinking are unchanged.
Runtime remains responsible for identity binding, leases, and the 24-hour TTL.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from contextlib import AbstractContextManager
from copy import deepcopy
from dataclasses import dataclass
from typing import cast

from pydantic_ai.messages import (
    ModelMessage,
    ModelMessagesTypeAdapter,
    ModelRequest,
    ToolReturnPart,
)

from app.platform.assistant.contracts import JsonObject
from app.platform.assistant.runtime import (
    AssistantRuntimeError,
    HistoryCheck,
    ModelSafeLedgerEntry,
    PrivateHistoryRecoveryMarker,
    PrivateNativeHistory,
)


@dataclass(slots=True, repr=False)
class _TurnGroup:
    messages: list[ModelMessage]
    checks: tuple[HistoryCheck, ...]


def _unique_checks(checks: Iterable[HistoryCheck]) -> tuple[HistoryCheck, ...]:
    # Keys may name different snapshots. Replacing a check solely by key could
    # make fresh evidence appear to validate an older, now-invalid answer.
    return tuple({id(check): check for check in checks}.values())


class CandidateHistory(PrivateNativeHistory):
    """Opaque retained groups; consumers receive independent message snapshots."""

    __slots__ = ("_aliases", "_discarded", "_groups", "_trimmed")

    def __init__(
        self,
        groups: Iterable[_TurnGroup],
        aliases: tuple[tuple[str, str], ...],
        *,
        trimmed: bool = False,
    ) -> None:
        self._groups = [
            _TurnGroup(deepcopy(group.messages), tuple(group.checks)) for group in groups
        ]
        self._aliases = tuple(aliases)
        self._trimmed = trimmed
        self._discarded = False

    @property
    def messages(self) -> list[ModelMessage]:
        return deepcopy([message for group in self._groups for message in group.messages])

    @property
    def checks(self) -> tuple[HistoryCheck, ...]:
        return _unique_checks(check for group in self._groups for check in group.checks)

    @property
    def aliases(self) -> tuple[tuple[str, str], ...]:
        return self._aliases

    def fork(self) -> CandidateHistory:
        if self._discarded:
            raise AssistantRuntimeError("private history was discarded")
        return type(self)(self._groups, self._aliases, trimmed=self._trimmed)

    def discard(self) -> None:
        for group in self._groups:
            group.messages.clear()
            group.checks = ()
        self._groups.clear()
        self._aliases = ()
        self._trimmed = False
        self._discarded = True


class QuotedHistory(PrivateHistoryRecoveryMarker):
    """Private contract boundary only; quoted material stays in the runtime ledger."""

    __slots__ = ("_discarded", "aliases", "material_since_generation")

    def __init__(
        self, aliases: Iterable[tuple[str, str]], material_since_generation: int = 0
    ) -> None:
        self.aliases = tuple(aliases)
        self.material_since_generation = material_since_generation
        self._discarded = False

    def fork(self) -> QuotedHistory:
        if self._discarded:
            raise AssistantRuntimeError("private history was discarded")
        return type(self)(self.aliases, self.material_since_generation)

    def discard(self) -> None:
        self.aliases = ()
        self.material_since_generation = 0
        self._discarded = True


def retain_history(
    previous: CandidateHistory | None,
    *,
    new_messages: list[ModelMessage],
    checks: tuple[HistoryCheck, ...],
    aliases: tuple[tuple[str, str], ...],
    projections: dict[str, JsonObject],
    replayable: bool,
    max_bytes: int = 12_000,
    max_turns: int = 8,
    max_checks: int = 16,
) -> tuple[CandidateHistory | None, str]:
    """Keep the newest complete suffix and all dependencies of each kept turn.

    ``checks`` must contain the complete transitive evidence set for the new
    answer, including no-tool answers that rely on previous turns. Checks are
    carried by reference, never executed, refreshed, persisted, or summarized
    here. A capacity failure describes future reuse, not current answer validity.
    """
    if not replayable or not new_messages:
        return None, "unavailable_evidence"
    # This module owns the private groups; retaining them does not expose a public API.
    if previous is not None and (
        previous._discarded  # pyright: ignore[reportPrivateUsage]
        or previous.aliases != aliases
    ):
        return None, "unavailable_evidence"

    messages = deepcopy(new_messages)
    for message in messages:
        if isinstance(message, ModelRequest):
            # Every current run rebuilds trusted instructions. Retaining one full
            # copy per provider request wastes the history budget and is not
            # conversational evidence. Only this private retained copy changes.
            message.instructions = None
        for part in message.parts:
            if isinstance(part, ToolReturnPart) and part.tool_call_id in projections:
                # The runner registers function tools; use their base return contract.
                cast(ToolReturnPart, part).content = deepcopy(projections[part.tool_call_id])

    groups = (
        [] if previous is None else list(previous._groups)  # pyright: ignore[reportPrivateUsage]
    )
    groups.append(_TurnGroup(messages, _unique_checks(checks)))
    trimmed = previous is not None and previous._trimmed  # pyright: ignore[reportPrivateUsage]
    while groups:
        retained_checks = _unique_checks(check for group in groups for check in group.checks)
        retained_messages = [message for group in groups for message in group.messages]
        measured = ModelMessagesTypeAdapter.dump_json(
            retained_messages,
            exclude_defaults=True,
            exclude_none=True,
        )
        if (
            len(groups) <= max_turns
            and len(retained_checks) <= max_checks
            and len(measured) <= max_bytes
        ):
            return CandidateHistory(groups, aliases, trimmed=trimmed), (
                "trimmed" if trimmed else "retained"
            )
        # Never split a turn or drop its checks independently of its messages.
        # Newer answers retain their complete dependencies even after this pop.
        groups.pop(0)
        trimmed = True
    return None, "unavailable_capacity"


def pack_history_note(results: list[JsonObject], events: list[JsonObject]) -> str | None:
    """Keep the existing ledger slot; bounded owner events survive oversized fact material."""
    import json

    value: object = results
    if events:
        value = {"history_note_version": 1, "results": results, "past_tool_executions": events}
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if len(encoded) <= 8000:
        return encoded if results or events else None
    if events:
        encoded = json.dumps(
            {"history_note_version": 1, "results": None, "past_tool_executions": events},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        if len(encoded) <= 8000:
            return encoded
    return None


def unpack_history_note(note: str | None) -> tuple[list[JsonObject] | None, list[JsonObject]]:
    """Only runner envelopes can carry events; ordinary tool JSON stays inside the old list."""
    import json

    if note is None:
        return None, []
    try:
        value = json.loads(note)
    except (ValueError, TypeError):
        return None, []
    if isinstance(value, list):
        return value, []
    if not isinstance(value, dict) or set(value) != {
        "history_note_version",
        "results",
        "past_tool_executions",
    }:
        return None, []
    if type(value["history_note_version"]) is not int or value["history_note_version"] != 1:
        return None, []
    events = value["past_tool_executions"]
    if not isinstance(events, list) or not 1 <= len(events) <= 64:
        return None, []
    for event in events:
        if (
            not isinstance(event, dict)
            or set(event) != {"owner", "tool", "call_index", "outcome"}
            or event["outcome"] != "read_returned_no_values"
            or type(event["call_index"]) is not int
            or not 1 <= event["call_index"] <= 128
            or any(
                not isinstance(event[key], str) or not 1 <= len(event[key]) <= 200
                for key in ("owner", "tool")
            )
        ):
            return None, []
    results = value["results"]
    if results is not None and not isinstance(results, list):
        return None, []
    return results, events


def recover_quoted_history(
    ledger: Sequence[ModelSafeLedgerEntry],
    *,
    checkpoint: Callable[[], None],
    validation_scope: Callable[[], AbstractContextManager[object]] | None = None,
    max_bytes: int = 12_000,
    max_turns: int = 8,
    max_checks: int = 16,
    material_since_generation: int = 0,
) -> tuple[str | None, tuple[HistoryCheck, ...], bool]:
    """Rebuild the existing quoted-material contract, never a partial native transcript.

    The quoted user text is untrusted context. Answers and owner projections are
    released only with their complete, current evidence. This private result is
    consumed during the current run and is never put into a native turn group.
    """
    import json
    from contextlib import nullcontext

    def encode(turns: Sequence[JsonObject]) -> str:
        return json.dumps(
            {
                "kind": "quoted_historical_user_intents",
                "turns": [dict(turn, position=n) for n, turn in enumerate(turns, 1)],
            },
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )

    def mark_omitted_output(turn: JsonObject) -> None:
        turn["history_context_unavailable"] = True
        turn["omitted_output_context"] = {
            "retained_input": "user_request_only",
            "assistant_output": "not_released",
            "tool_output": "not_released",
            "prior_specific_values": "unknown",
            "prior_completion": "unknown",
            "meaning": (
                "保留的用户原话仅说明请求；原助手答复和工具事实正文未提供。"
                "若附过去工具执行摘要，仅按其逐项范围理解。"
                "当前材料无法核实该轮是否给过具体值或办成事项；"
                "用户后续指代和本标记均不能证明曾给出、成功或失败。"
            ),
        }
        if "verified_tool_results" in turn:
            context = cast(JsonObject, turn["omitted_output_context"])
            context["tool_output"] = "independently_verified_subset"
            context["meaning"] = (
                "原助手答复未提供；所列逐工具结果已分别通过当前核验，可按各自调用范围使用。"
                "未列出的输出仍不可推断；用户指代和恢复标记不证明曾给过具体值或办成事项。"
            )

    scope: Callable[[], AbstractContextManager[object]] = validation_scope or nullcontext
    turns: list[JsonObject]
    released: list[HistoryCheck]
    checked: dict[int, bool]
    turns, released, checked = [], [], {}
    unavailable = len(ledger) > max_turns
    for entry in reversed(ledger[-max_turns:]):
        checkpoint()
        turn: JsonObject = {
            "dialogue_state": "stopped" if entry.technical_marker == "stopped" else "ended",
            "quoted_user_text": entry.teacher_utterance,
        }
        material, events = unpack_history_note(entry.model_safe_note)
        if entry.generation >= material_since_generation and events:
            turn["past_tool_executions"] = events
            turn["past_tool_execution_meaning"] = (
                "仅证明所列工具调用当时未返回事实值，不证明其他调用或助手答复没有给值；"
                "不代表当前事实、当前权限或业务执行结果。"
            )
        proofs = (
            entry.model_history_checks if entry.generation >= material_since_generation else None
        )
        allowed = proofs is not None
        if proofs is not None:
            pending = {id(check): check for check in proofs if id(check) not in checked}
            if len(checked) + len(pending) > max_checks:
                allowed = False
            else:
                stable: dict[int, bool] = {}
                reusable = {
                    key: check
                    for key, check in pending.items()
                    if check.revalidation_covers_content
                }
                # Only completed owner boundaries may share content-version probes.
                # A failed proof is refreshed outside that boundary, just as in
                # the original loop; no stale transaction spans a model wait.
                try:
                    with scope():
                        for key, check in reusable.items():
                            checkpoint()
                            stable[key] = check.valid()
                            checkpoint()
                except Exception:
                    stable.clear()
                    checkpoint()  # Cancellation/deadline must never start recovery.
                for key, check in pending.items():
                    checkpoint()
                    checked[key] = stable.get(key, False) or check.refresh()
                    checkpoint()
                allowed = all(checked[id(check)] for check in proofs)
        if proofs and material is None:
            # A valid check cannot replace the omitted structured factual context.
            # Keep the user's intent available for a fresh authorized read.
            allowed = False
            turn["history_context_unavailable"] = True
            unavailable = True
        if allowed and entry.model_safe_template is not None:
            turn["quoted_assistant_text"] = entry.model_safe_template
            if material is not None:
                turn["quoted_tool_results"] = material
        elif entry.model_safe_template is not None:
            turn["history_context_unavailable"] = True
            unavailable = True

        individual_checks: list[HistoryCheck] = []
        if not allowed and entry.generation >= material_since_generation:
            individual_results: list[JsonObject] = []
            for item in entry.tool_history:
                checkpoint()
                check = item.history_check
                key = id(check)
                if key not in checked:
                    if len(checked) >= max_checks:
                        continue
                    valid = False
                    try:
                        with scope():
                            if check.revalidation_covers_content:
                                valid = check.valid()
                    except Exception:
                        valid = False
                        checkpoint()
                    checked[key] = valid or check.refresh()
                    checkpoint()
                if not checked[key]:
                    continue
                try:
                    projection = json.loads(item.material_json)
                except (ValueError, TypeError):
                    continue
                if not isinstance(projection, dict):
                    continue
                proposed = {**turn, "verified_tool_results": [*individual_results, projection]}
                # Reserve omission metadata before accepting any whole tool result.
                mark_omitted_output(proposed)
                if len(encode([proposed]).encode("utf-8")) > max_bytes:
                    continue
                individual_results.append(projection)
                individual_checks.append(check)
            if individual_results:
                turn["verified_tool_results"] = individual_results

        if "quoted_assistant_text" not in turn:
            mark_omitted_output(turn)
            unavailable = True
        if len(encode([turn]).encode("utf-8")) > max_bytes:
            # Do not clip the middle of an answer, source, or tool projection.
            turn = {
                "dialogue_state": turn["dialogue_state"],
                "quoted_user_text": entry.teacher_utterance,
                **{
                    key: turn[key]
                    for key in ("past_tool_executions", "past_tool_execution_meaning")
                    if key in turn
                },
            }
            mark_omitted_output(turn)
            allowed = False
            individual_checks = []
            unavailable = True
        prospective = [turn, *turns]
        if len(encode(prospective).encode("utf-8")) > max_bytes:
            unavailable = True
            break
        turns = prospective
        released.extend(individual_checks)
        if allowed and proofs is not None and "quoted_assistant_text" in turn:
            released.extend(proofs)
    checkpoint()
    return encode(turns) if turns else None, _unique_checks(released), unavailable
