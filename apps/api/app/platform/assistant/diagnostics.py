"""Bounded request metadata, no conversation or provider content."""

from __future__ import annotations

import json
import logging
from asyncio import current_task
from collections import Counter, defaultdict
from collections.abc import Callable, Generator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import wraps
from threading import Lock, current_thread
from time import monotonic


@dataclass
class _Trace:
    request_id: str
    started: float = field(default_factory=monotonic)
    started_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    counts: Counter[str] = field(default_factory=lambda: Counter[str]())
    seconds: dict[str, float] = field(default_factory=lambda: defaultdict[str, float](float))
    self_seconds: dict[str, float] = field(default_factory=lambda: defaultdict[str, float](float))
    usage: list[dict[str, int]] = field(default_factory=lambda: list[dict[str, int]]())
    model_inputs: list[dict[str, int]] = field(default_factory=list)
    provider_io: list[dict[str, object]] = field(default_factory=list)
    protocol_responses: list[dict[str, object]] = field(
        default_factory=lambda: list[dict[str, object]]()
    )
    final_review: dict[str, bool] | None = None
    tool_sequence: list[str] = field(default_factory=list)
    tool_outcomes: list[dict[str, str]] = field(default_factory=list)
    lock: Lock = field(default_factory=Lock, repr=False)
    cross_context_spans: int = 0
    failure: dict[str, str] | None = None
    last_tool_alias: str | None = None


@dataclass
class _Frame:
    trace: _Trace
    execution: tuple[object, object | None]
    child_seconds: float = 0.0


_active: ContextVar[_Trace | None] = ContextVar("assistant_request_timing", default=None)
_frame: ContextVar[_Frame | None] = ContextVar("assistant_timing_frame", default=None)


def _safe_exception_kind(error: BaseException) -> str:
    name = type(error).__name__
    return (
        name
        if name
        in {
            "AssistantRuntimeError",
            "AssistantServiceError",
            "ValueError",
            "TypeError",
            "PermissionError",
            "TimeoutError",
            "RuntimeError",
            "ExceptionGroup",
            "ValidationError",
            "UsageLimitExceeded",
            "UnexpectedModelBehavior",
            "ModelHTTPError",
            "ModelAPIError",
        }
        else "other"
    )


@contextmanager
def failure_boundary(
    stage: str, *, tool_alias: str | None = None, reason: str = "exception"
) -> Generator[None]:
    """Record fixed metadata only; preserve a more specific nested failure."""
    trace = _active.get()
    if trace is None:
        yield
        return
    with trace.lock:
        prior = trace.failure
        if isinstance(tool_alias, str) and tool_alias in {
            f"tool_{index}" for index in range(64)
        } | {"search_tools"}:
            trace.last_tool_alias = tool_alias
    try:
        yield
    except Exception as error:
        safe_stage = (
            stage
            if stage
            in {
                "scope_enter",
                "scope_exit",
                "current",
                "consume_tool_call",
                "gateway_execute",
                "tool_arguments",
                "tool_result",
                "sdk_protocol",
                "model_guard",
                "checkpoint",
                "tool_loading",
                "settlement",
            }
            else "other"
        )
        safe_reason = (
            reason
            if reason
            in {
                "exception",
                "duplicate_id",
                "tool_limit",
                "arguments_size",
                "result_type",
                "result_size",
                "result_encoding",
                "confirmation_candidate",
                "transmission_check",
                "tool_batch_limit",
                "expired_boundary",
                "deadline",
                "history_lease",
                "history_evidence",
                "capability_selection",
            }
            else "other"
        )
        metadata = {
            "stage": safe_stage,
            "reason": safe_reason,
            "exception": _safe_exception_kind(error),
        }
        if safe_stage == "sdk_protocol" and type(error).__name__ == "UnexpectedModelBehavior":
            # Classify only known framework code locations. Never inspect an
            # exception message, cause, frame locals, or provider response.
            reason_by_site = {
                ("pydantic_ai._agent_graph", "consume_output_retry"): "output_retry_exhausted",
                ("pydantic_ai.tool_manager", "_check_max_retries"): "tool_retry_exhausted",
                ("pydantic_ai.models.openai", "_process_response"): "response_validation",
            }
            metadata["sdk_reason"] = "other"
            frame = error.__traceback__
            for _ in range(64):
                if frame is None:
                    break
                module = frame.tb_frame.f_globals.get("__name__")
                if isinstance(module, str):
                    site = (module, frame.tb_frame.f_code.co_name)
                    if site in reason_by_site:
                        metadata["sdk_reason"] = reason_by_site[site]
                frame = frame.tb_next
        if isinstance(tool_alias, str) and tool_alias in {
            f"tool_{index}" for index in range(64)
        } | {"search_tools"}:
            metadata["tool_alias"] = tool_alias
        with trace.lock:
            if trace.failure is prior:
                trace.failure = metadata
        raise


def _execution() -> tuple[object, object | None]:
    try:
        task = current_task()
    except RuntimeError:
        task = None  # Synchronous readers have no running event loop.
    return current_thread(), task


@contextmanager
def request_trace(request_id: str) -> Generator[None]:
    trace = _Trace(request_id)
    token = _active.set(trace)
    frame_token = _frame.set(None)
    outcome = "completed"
    try:
        yield
    except Exception as exc:
        outcome = _safe_exception_kind(exc)
        raise
    finally:
        with trace.lock:
            payload = {
                "request_id": trace.request_id,
                "started_at": trace.started_at,
                "ended_at": datetime.now(UTC).isoformat(),
                "seconds": round(monotonic() - trace.started, 4),
                "outcome": outcome,
                "counts": dict(trace.counts),
                "cross_context_spans": trace.cross_context_spans,
                "exclusive_scope": "same_execution_context",
                "usage": list(trace.usage),
                "model_inputs": list(trace.model_inputs),
                **({"provider_io": list(trace.provider_io)} if trace.provider_io else {}),
                **(
                    {"protocol_responses": list(trace.protocol_responses)}
                    if trace.protocol_responses
                    else {}
                ),
                **({"final_review": dict(trace.final_review)} if trace.final_review else {}),
                **({"tool_sequence": list(trace.tool_sequence)} if trace.tool_sequence else {}),
                **({"tool_outcomes": list(trace.tool_outcomes)} if trace.tool_outcomes else {}),
                "inclusive_seconds": {k: round(v, 4) for k, v in trace.seconds.items()},
                "exclusive_seconds": {k: round(v, 4) for k, v in trace.self_seconds.items()},
                **(
                    {"failure": dict(trace.failure)}
                    if outcome != "completed" and trace.failure
                    else {}
                ),
                **(
                    {"last_tool_alias": trace.last_tool_alias}
                    if outcome != "completed" and trace.last_tool_alias
                    else {}
                ),
            }
        _frame.reset(frame_token)
        _active.reset(token)
        logging.getLogger("uvicorn.error").info("assistant_timing %s", json.dumps(payload))


def record_protocol_response(
    *,
    layer: str,
    finish_reason: object,
    content_state: str,
    tool_call_count: int,
    thinking_present: bool,
    other_parts: bool = False,
) -> None:
    """Record finite response structure only; callers retain all content privately."""
    trace = _active.get()
    if trace is None:
        return
    content = (
        content_state
        if type(content_state) is str and content_state in {"missing", "null", "empty", "nonempty"}
        else "other"
    )
    count = tool_call_count if type(tool_call_count) is int and 0 <= tool_call_count <= 64 else None
    thinking = thinking_present is True
    shape = (
        "other"
        if other_parts is not False or content == "other" or count is None
        else "mixed"
        if content == "nonempty" and count
        else "tool"
        if count
        else "text"
        if content == "nonempty"
        else "thinking_only"
        if thinking
        else "empty"
    )
    record: dict[str, object] = {
        "layer": layer if type(layer) is str and layer in {"wire", "sdk"} else "other",
        "finish_reason": finish_reason
        if type(finish_reason) is str
        and finish_reason
        in {"stop", "tool_calls", "tool_call", "length", "content_filter", "function_call"}
        else "other",
        "content_state": content,
        "tool_call_count": count,
        "thinking_present": thinking,
        "output_shape": shape,
    }
    with trace.lock:
        if len(trace.protocol_responses) < 64:
            trace.protocol_responses.append(record)


def record_final_review(*, accepted: bool = False) -> None:
    """A validator acknowledgement is metadata, never permission to publish a draft."""
    trace = _active.get()
    if trace is None:
        return
    with trace.lock:
        trace.final_review = {"started": True, "accepted": accepted is True}


def record_tool_outcome(
    alias: str, *, status: object = None, code: object = None, stage: object = None
) -> None:
    """Count fixed owner outcomes; never accept argument, error or response text."""
    trace = _active.get()
    if trace is None or alias not in {f"tool_{i}" for i in range(64)}:
        return

    def fixed(value: object, allowed: frozenset[str]) -> str:
        if value is None:
            return "unspecified"
        return value if isinstance(value, str) and value in allowed else "other"

    result = {
        "tool_alias": alias,
        "status": fixed(
            status,
            frozenset(
                {
                    "known",
                    "available",
                    "not_recorded",
                    "no_current_records",
                    "ambiguous",
                    "denied",
                    "rejected",
                    "source_unavailable",
                    "clarification_required",
                    "unsupported_selection",
                    "confirmation_pending",
                    "conflict",
                    "incomplete",
                    "object_mismatch",
                    "located",
                    "target_room_candidates",
                    "target_bed_candidates",
                    "partial",
                    "unavailable",
                }
            ),
        ),
        "code": fixed(
            code,
            frozenset({"invalid_arguments", "owner_rejected", "invalid_residence_selection"}),
        ),
    }
    if stage is not None:
        result["stage"] = fixed(
            stage,
            frozenset(
                {
                    "student",
                    "original_residence",
                    "building",
                    "original_bed",
                    "target_room",
                    "target_bed",
                    "change_coordinates",
                    "creation_slot",
                    "time_basis",
                }
            ),
        )
    with trace.lock:
        if len(trace.tool_outcomes) < 32:
            trace.tool_outcomes.append(result)


def record_tool_alias(alias: str) -> None:
    """Only catalog ordinal aliases; never arguments, request text or result content."""
    trace = _active.get()
    if trace is None or alias not in {f"tool_{i}" for i in range(64)} | {"search_tools"}:
        return
    with trace.lock:
        if len(trace.tool_sequence) < 32:
            trace.tool_sequence.append(alias)


@contextmanager
def measure(label: str) -> Generator[None]:
    trace = _active.get()
    if trace is None:
        yield
        return
    # Labels are fixed by application code; no request-derived labels accepted.
    if label not in {"authorization", "model", "provider", "tool", "facts", "scope", "database"}:
        raise ValueError("unknown timing category")
    started = monotonic()
    execution = _execution()
    inherited = _frame.get()
    parent = (
        inherited
        if inherited is not None and inherited.trace is trace and inherited.execution == execution
        else None
    )
    # copy_context() inherits references. A worker thread or child async task
    # must not mutate that inherited parent's nesting/child-time accounting.
    frame = _Frame(trace, execution)
    token = _frame.set(frame)
    with trace.lock:
        trace.counts[label] += 1
        if inherited is not None and inherited.trace is trace and parent is None:
            trace.cross_context_spans += 1
    try:
        yield
    finally:
        elapsed = monotonic() - started
        _frame.reset(token)
        if parent is not None:
            parent.child_seconds += elapsed
        with trace.lock:
            # Work in parallel branches is accumulated, not a wall-clock partition.
            trace.seconds[label] += elapsed
            trace.self_seconds[label] += max(0, elapsed - frame.child_seconds)


def timed[**P, R](label: str) -> Callable[[Callable[P, R]], Callable[P, R]]:
    def decorate(function: Callable[P, R]) -> Callable[P, R]:
        @wraps(function)
        def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
            with measure(label):
                return function(*args, **kwargs)

        return wrapped

    return decorate


def record_model_input(
    *,
    wire_bytes: int,
    tool_count: int | None = None,
    message_count: int | None = None,
    tool_schema_bytes: int | None = None,
    system_message_bytes: int | None = None,
    user_message_bytes: int | None = None,
    assistant_message_bytes: int | None = None,
    tool_message_bytes: int | None = None,
    reasoning_bytes: int | None = None,
) -> None:
    """Record counts only, including a request rejected before reservation/transmission."""
    trace = _active.get()
    if trace is not None:
        with trace.lock:
            if len(trace.model_inputs) < 32:
                trace.model_inputs.append(
                    {
                        key: value
                        for key, value in {
                            "wire_bytes": wire_bytes,
                            "tool_count": tool_count,
                            "message_count": message_count,
                            "tool_schema_bytes": tool_schema_bytes,
                            "system_message_bytes": system_message_bytes,
                            "user_message_bytes": user_message_bytes,
                            "assistant_message_bytes": assistant_message_bytes,
                            "tool_message_bytes": tool_message_bytes,
                            "reasoning_bytes": reasoning_bytes,
                        }.items()
                        if value is not None
                    }
                )


def record_provider_io(
    *,
    milestones_seconds: dict[str, float],
    status: int | None,
    body_chunks: int,
    body_bytes: int,
    whitespace_bytes: int,
    outcome: str,
) -> None:
    """Bounded transport metadata; never accept headers, exceptions or body text."""
    trace = _active.get()
    if trace is None:
        return
    stages = {
        "tcp_connected",
        "tls_ready",
        "request_sent",
        "headers_received",
        "first_body",
        "last_body",
        "body_complete",
        "finished",
    }
    outcomes = {
        "complete",
        "cancelled",
        "connect_timeout",
        "read_timeout",
        "write_timeout",
        "pool_timeout",
        "transport_error",
        "other",
    }
    data: dict[str, object] = {
        "milestones_seconds": {
            k: round(v, 4)
            for k, v in milestones_seconds.items()
            if k in stages and type(v) in (int, float) and 0 <= v <= 3600
        },
        "outcome": outcome if outcome in outcomes else "other",
        "status": status if type(status) is int and 100 <= status <= 599 else None,
    }
    for key, value in (
        ("body_chunks", body_chunks),
        ("body_bytes", body_bytes),
        ("whitespace_bytes", whitespace_bytes),
    ):
        data[key] = value if type(value) is int and 0 <= value <= 2**31 else 0
    with trace.lock:
        if len(trace.provider_io) < 32:
            trace.provider_io.append(data)


def record_usage(
    *,
    prompt: int,
    completion: int,
    reasoning: int | None,
    cache_hit: int | None,
    cache_miss: int | None,
) -> None:
    trace = _active.get()
    if trace is None:
        return
    with trace.lock:
        if len(trace.usage) >= 32:
            return
        trace.usage.append(
            {
                k: v
                for k, v in {
                    "prompt_tokens": prompt,
                    "completion_tokens": completion,
                    "reasoning_tokens": reasoning,
                    "cache_hit_tokens": cache_hit,
                    "cache_miss_tokens": cache_miss,
                }.items()
                if v is not None
            }
        )
