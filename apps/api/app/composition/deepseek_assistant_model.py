from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from hashlib import sha256
from typing import Annotated, Literal, Protocol, Self, cast

import httpx
from pydantic import BaseModel, ConfigDict, Field, JsonValue, SecretStr, model_validator

from app.platform.assistant.diagnostics import measure, record_usage, timed
from app.platform.assistant.runtime import AssistantServiceError
from app.platform.assistant.tool_loop import (
    ModelContinuation,
    ModelMessage,
    ModelReply,
    ModelRequest,
    ModelToolCall,
    ToolDescriptor,
)

_ENDPOINT = "https://api.deepseek.com/chat/completions"
_MAX_RESPONSE_BYTES = 1_048_576
NonNegative = Annotated[int, Field(ge=0)]
ShortText = Annotated[str, Field(min_length=1, max_length=200)]


class DeepSeekModelError(AssistantServiceError):
    """模型边界的固定错误，不持有响应正文、原始异常或凭据。"""


@dataclass(frozen=True, slots=True)
class ModelCallUsage:
    """仅保留服务报告的计费元数据；具体价格与批准范围由 permit owner 处理。"""

    model: str
    request_id: str
    completion_id: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    prompt_cache_hit_tokens: int | None
    prompt_cache_miss_tokens: int | None
    reasoning_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class CurrentModelApproval:
    """Trusted product approval bound to the current model's own billing permit."""

    model: Literal["deepseek-flash"]
    approval_reference: str
    price_verified_on: str

    def __post_init__(self) -> None:
        if (
            self.model != "deepseek-flash"
            or type(self.approval_reference) is not str
            or not 1 <= len(self.approval_reference) <= 200
            or not self.approval_reference.strip()
            or type(self.price_verified_on) is not str
        ):
            raise ValueError("invalid current model approval")
        try:
            verified = date.fromisoformat(self.price_verified_on)
        except ValueError:
            raise ValueError("invalid current model approval") from None
        if verified.isoformat() != self.price_verified_on:
            raise ValueError("invalid current model approval")


@dataclass(frozen=True, slots=True)
class CandidateModelApproval:
    """Explicit experiment-only admission, supplied by trusted local composition.

    Not a product setting, model instruction or permission granted to an end user.
    The matching permit owns the bounded experiment and checks expiry at each send.
    """

    experiment_id: str
    approval_reference: str
    model: Literal["deepseek-v4-pro"]
    expires_at: datetime

    def __post_init__(self) -> None:
        if (
            any(
                type(value) is not str or not 1 <= len(value) <= 200
                for value in (self.experiment_id, self.approval_reference)
            )
            or self.model != "deepseek-v4-pro"
            or self.expires_at.tzinfo is None
            or self.expires_at.utcoffset() is None
        ):
            raise ValueError("invalid candidate model approval")


class ModelCallReservation(Protocol):
    """reserve 已先行保留额度；只有可靠 settle 可调整，unknown 不释放保守额度。"""

    def settle(self, usage: ModelCallUsage) -> None: ...

    def unknown(self) -> None: ...


class ModelCallPermit(Protocol):
    """由外部批准与预算 owner 提供；适配器不创建默认批准、价格或留存规则。"""

    def reserve(self, max_input_tokens: int, max_output_tokens: int) -> ModelCallReservation: ...


class _StrictWire(BaseModel):
    model_config = ConfigDict(
        extra="forbid", strict=True, frozen=True, allow_inf_nan=False, hide_input_in_errors=True
    )


class _Function(_StrictWire):
    name: Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_-]+$")]
    arguments: Annotated[str, Field(max_length=16_000)]


class _Call(_StrictWire):
    id: ShortText
    type: Literal["function"]
    function: _Function
    index: Annotated[int, Field(ge=0, le=63)] | None = None


class _Message(_StrictWire):
    role: Literal["assistant"]
    content: Annotated[str, Field(max_length=8_000)] | None = None
    tool_calls: Annotated[list[_Call], Field(max_length=64)] | None = None
    reasoning_content: SecretStr = Field(repr=False)

    @model_validator(mode="after")
    def bounded_private_reasoning(self) -> Self:
        if len(self.reasoning_content.get_secret_value()) > 131_072:
            raise ValueError("model continuation too large")
        for position, call in enumerate(self.tool_calls or []):
            if "index" in call.model_fields_set and call.index != position:
                raise ValueError("tool call index does not match array position")
        return self


class _Choice(_StrictWire):
    index: Literal[0]
    message: _Message
    finish_reason: ShortText
    logprobs: None = None


class _CompletionDetails(_StrictWire):
    reasoning_tokens: NonNegative = 0


# Observed provider metadata only; these counters are never added to billed totals.
_PromptDetailKey = Annotated[str, Field(min_length=1, max_length=64)]
_PromptDetailCount = Annotated[int, Field(ge=0, le=131_072)]


class _Usage(_StrictWire):
    prompt_tokens: NonNegative
    completion_tokens: NonNegative
    total_tokens: NonNegative
    prompt_cache_hit_tokens: NonNegative | None = None
    prompt_cache_miss_tokens: NonNegative | None = None
    completion_tokens_details: _CompletionDetails | None = None
    prompt_tokens_details: (
        Annotated[dict[_PromptDetailKey, _PromptDetailCount], Field(max_length=16)] | None
    ) = None

    @model_validator(mode="after")
    def consistent_totals(self) -> Self:
        details = self.completion_tokens_details
        if details is not None and details.reasoning_tokens > self.completion_tokens:
            raise ValueError("reasoning usage is not a completion subset")
        if self.total_tokens != self.prompt_tokens + self.completion_tokens:
            raise ValueError("inconsistent model usage")
        hit, miss = self.prompt_cache_hit_tokens, self.prompt_cache_miss_tokens
        if (hit is None) != (miss is None):
            raise ValueError("incomplete cache usage")
        if hit is not None and miss is not None and hit + miss != self.prompt_tokens:
            raise ValueError("inconsistent cache usage")
        prompt_details = self.prompt_tokens_details
        if prompt_details is not None and "cached_tokens" in prompt_details:
            cached = prompt_details["cached_tokens"]
            if hit is None or cached != hit or cached > self.prompt_tokens:
                raise ValueError("inconsistent prompt cache metadata")
        return self


class _Completion(_StrictWire):
    id: ShortText
    object: Literal["chat.completion"]
    created: NonNegative
    model: ShortText
    choices: Annotated[list[_Choice], Field(min_length=1, max_length=1)]
    usage: _Usage
    system_fingerprint: str | None = None


@dataclass(frozen=True, slots=True, repr=False)
class _DeepSeekContinuation:
    reasoning: SecretStr
    assistant_sha256: str


def _assistant_sha256(message: ModelMessage) -> str:
    return sha256(message.model_dump_json().encode("utf-8")).hexdigest()


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON member")
        result[key] = value
    return result


def _reject_constant(_: str) -> None:
    raise ValueError("invalid JSON constant")


def _strict_json(value: str | bytes) -> object:
    return cast(
        object,
        json.loads(value, object_pairs_hook=_unique_object, parse_constant=_reject_constant),
    )


def _encoded(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def _arguments(value: object, descriptor: ToolDescriptor) -> dict[str, JsonValue]:
    if not isinstance(value, dict):
        raise ValueError("tool arguments must be an object")
    arguments = cast(dict[str, JsonValue], value)
    properties = descriptor.parameters.get("properties", {})
    if descriptor.parameters.get("additionalProperties") is False:
        if not isinstance(properties, dict) or set(arguments) - set(properties):
            raise ValueError("undeclared tool argument")
    # Full parameter semantics and domain permissions remain the tool owner's responsibility.
    return arguments


def _request_body(
    request: ModelRequest, model: str, max_output_tokens: int
) -> tuple[dict[str, object], dict[str, str]]:
    if not 1 <= len(request.messages) <= 64 or len(request.tools) > 64:
        raise ValueError("model request bounds")
    descriptors = {tool.name: tool for tool in request.tools}
    if len(descriptors) != len(request.tools):
        raise ValueError("duplicate capability")
    # A request-local bijection supports every internal name up to 120 characters
    # while keeping provider names safe and under its 64-character bound.
    names = {tool.name: f"tool_{index}" for index, tool in enumerate(request.tools)}
    reverse = {wire: internal for internal, wire in names.items()}
    messages: list[dict[str, object]] = []
    pending: set[str] = set()
    used: set[str] = set()
    for message in request.messages:
        item: dict[str, object] = {"role": message.role, "content": message.content}
        if message.role == "tool":
            if message.calls or message.content is None or message.tool_call_id not in pending:
                raise ValueError("orphaned tool response")
            pending.remove(message.tool_call_id)
            item["tool_call_id"] = message.tool_call_id
        else:
            if pending or message.tool_call_id is not None:
                raise ValueError("incomplete tool response sequence")
            if message.calls:
                if message.role != "assistant" or len(message.calls) > 8:
                    raise ValueError("invalid assistant calls")
                wire_calls: list[dict[str, object]] = []
                for call in message.calls:
                    if call.name not in names or call.call_id in used:
                        raise ValueError("unregistered or duplicate tool")
                    used.add(call.call_id)
                    pending.add(call.call_id)
                    arguments = _arguments(call.arguments, descriptors[call.name])
                    wire_calls.append(
                        {
                            "id": call.call_id,
                            "type": "function",
                            "function": {
                                "name": names[call.name],
                                "arguments": _encoded(arguments),
                            },
                        }
                    )
                item["tool_calls"] = wire_calls
            elif message.content is None or not message.content.strip():
                raise ValueError("empty message")
        if message.role == "assistant" and request.tools:
            continuation = message.private_continuation
            payload = None if continuation is None else continuation.get_payload()
            if not isinstance(
                payload, _DeepSeekContinuation
            ) or payload.assistant_sha256 != _assistant_sha256(message):
                raise ValueError("complete original assistant continuation required")
            item["reasoning_content"] = payload.reasoning.get_secret_value()
        elif message.private_continuation is not None and message.role != "assistant":
            raise ValueError("continuation belongs only to assistant messages")
        messages.append(item)
    if pending:
        raise ValueError("tool results are incomplete")
    tools = [
        {
            "type": "function",
            "function": {
                "name": names[tool.name],
                "description": tool.description,
                "parameters": tool.parameters,
            },
        }
        for tool in request.tools
    ]
    return {
        "model": model,
        "messages": messages,
        **({"tools": tools, "tool_choice": "auto"} if tools else {}),
        "thinking": {"type": "enabled"},
        "reasoning_effort": "high",
        "stream": False,
        "max_tokens": max_output_tokens,
    }, reverse


class DeepSeekAssistantModel:
    """同步 ToolModel 窄适配；无默认模型批准或付费许可，无自动重试和环境读取。"""

    # Trusted adapter capability, never a user/model-output permission. _complete
    # checks the private callback after client setup and before every real send.
    checks_transmission_before_send = True

    def __init__(
        self,
        *,
        api_key: SecretStr,
        model: str,
        permit: ModelCallPermit | None = None,
        candidate_approval: CandidateModelApproval | None = None,
        current_approval: CurrentModelApproval | None = None,
        transport: httpx.BaseTransport | None = None,
        max_input_tokens: int = 32_768,
        max_output_tokens: int = 16_384,
        timeout: httpx.Timeout | None = None,
        parallel_tool_calls: Literal[False] | None = None,
    ) -> None:
        if not api_key.get_secret_value().strip():
            raise DeepSeekModelError("model credentials unavailable")
        if (
            model == "deepseek-flash"
            or current_approval is not None
            or getattr(permit, "current_approval", None) is not None
        ):
            if (
                type(current_approval) is not CurrentModelApproval
                or model != current_approval.model
                or getattr(permit, "current_approval", None) != current_approval
                or candidate_approval is not None
                or getattr(permit, "candidate_approval", None) is not None
            ):
                raise DeepSeekModelError("approved model selection required")
        elif (
            model != "deepseek-v4-flash"
            or candidate_approval is not None
            or getattr(permit, "candidate_approval", None) is not None
        ):
            if (
                type(candidate_approval) is not CandidateModelApproval
                or model != candidate_approval.model
                or candidate_approval.expires_at <= datetime.now(UTC)
                or getattr(permit, "candidate_approval", None) != candidate_approval
            ):
                raise DeepSeekModelError("approved model selection required")
        if parallel_tool_calls is not None and parallel_tool_calls is not False:
            raise DeepSeekModelError("invalid parallel tool setting")
        self._parallel_tool_calls = parallel_tool_calls
        self._candidate_approval = candidate_approval
        self._current_approval = current_approval
        if not 1_024 <= max_input_tokens <= 131_072 or not 1 <= max_output_tokens <= 32_768:
            raise DeepSeekModelError("invalid model limits")
        self._api_key = api_key
        self._model = model
        self._permit = permit
        self._transport = transport
        self._max_input_tokens = max_input_tokens
        self._max_output_tokens = max_output_tokens
        self._timeout = timeout or httpx.Timeout(connect=10, read=60, write=30, pool=10)
        if any(
            value is None or not 0 < value <= 60
            for value in (
                self._timeout.connect,
                self._timeout.read,
                self._timeout.write,
                self._timeout.pool,
            )
        ):
            raise DeepSeekModelError("invalid model timeout")

    @timed("model")
    def respond(self, request: ModelRequest) -> ModelReply:
        """先校验最小请求并保留预算，再访问固定官方端点；未知结果永不自动重发。"""
        if self._permit is None:
            raise DeepSeekModelError("model call permit is required")
        encoded: bytes | None = None
        reverse: dict[str, str] = {}
        try:
            # Encoding stays private. The transport guard below authorizes the actual
            # external release after budget reservation and client setup.
            body, reverse = _request_body(request, self._model, self._max_output_tokens)
            if self._parallel_tool_calls is not None:
                body["parallel_tool_calls"] = self._parallel_tool_calls
            encoded = _encoded(body).encode("utf-8")
            # Conservative byte cap, including the wire envelope, avoids relying on
            # an unverified local tokenizer. Reported usage is checked separately.
            if len(encoded) > self._max_input_tokens:
                raise ValueError("model input too large")
        except Exception:
            encoded = None
        if encoded is None:
            raise DeepSeekModelError("invalid or oversized model input")
        refusal = "dependency"
        reservation: ModelCallReservation | None = None
        try:
            reservation = self._permit.reserve(self._max_input_tokens, self._max_output_tokens)
        except AssistantServiceError as exc:
            refusal = exc.reason
            reservation = None
        except Exception:
            reservation = None
        if reservation is None:
            raise DeepSeekModelError("model call permit refused", reason=refusal)
        settled = False
        failure = "model response unavailable or invalid"
        failure_reason = "dependency"

        def before_send() -> None:
            nonlocal failure
            # A withheld request has no reported usage. The existing reservation
            # contract has no release operation, so unknown conservatively retains
            # that amount; it is not a claim that a provider call was charged.
            failure = "model request not sent; budget reservation retained"
            request.require_current_transmission()
            if self._candidate_approval is not None or self._current_approval is not None:
                # Current/candidate billing requires the actual release time, after the same
                # current-identity guard used by normal calls. No prompt is recorded.
                marker = getattr(reservation, "mark_transmission_started", None)
                if not callable(marker):
                    raise ValueError("candidate transmission accounting unavailable")
                marker()
                # Durable candidate accounting can wait on the shared ledger. Keep
                # authority/deadline validation adjacent to the actual HTTP release.
                request.require_current_transmission()
                if (
                    self._candidate_approval is not None
                    and self._candidate_approval.expires_at <= datetime.now(UTC)
                ):
                    raise ValueError("candidate approval expired before transmission")
            failure = "model response unavailable or invalid"

        try:
            # _complete checks again after client setup, immediately before HTTP.
            # No external release occurs in this private handoff to the transport.
            completion, request_id = self._complete(encoded, before_send=before_send)
            usage = completion.usage
            if (
                completion.model != self._model
                or usage.prompt_tokens > self._max_input_tokens
                or usage.completion_tokens > self._max_output_tokens
            ):
                raise ValueError("unapproved model identity or usage exceeds reservation")
            reservation.settle(
                ModelCallUsage(
                    model=completion.model,
                    request_id=request_id,
                    completion_id=completion.id,
                    prompt_tokens=usage.prompt_tokens,
                    completion_tokens=usage.completion_tokens,
                    total_tokens=usage.total_tokens,
                    prompt_cache_hit_tokens=usage.prompt_cache_hit_tokens,
                    prompt_cache_miss_tokens=usage.prompt_cache_miss_tokens,
                    reasoning_tokens=None
                    if usage.completion_tokens_details is None
                    else usage.completion_tokens_details.reasoning_tokens,
                )
            )
            settled = True
            record_usage(
                prompt=usage.prompt_tokens,
                completion=usage.completion_tokens,
                reasoning=None
                if usage.completion_tokens_details is None
                else usage.completion_tokens_details.reasoning_tokens,
                cache_hit=usage.prompt_cache_hit_tokens,
                cache_miss=usage.prompt_cache_miss_tokens,
            )
            if len(completion.choices[0].message.tool_calls or []) > 8:
                failure_reason = "tool_limit"
                raise ValueError("tool batch exceeds execution limit")
            return self._reply(completion, reverse, request.tools)
        except Exception:
            if not settled:
                try:
                    reservation.unknown()
                except Exception:
                    failure = "model cost settlement unavailable"
        finally:
            encoded = None
            reverse.clear()
        # Raise outside the caught exception context: raw validation or HTTP objects are private.
        raise DeepSeekModelError(failure, reason=failure_reason)

    def _complete(self, body: bytes, *, before_send: Callable[[], None]) -> tuple[_Completion, str]:
        with httpx.Client(
            transport=self._transport,
            timeout=self._timeout,
            trust_env=False,
            follow_redirects=False,
        ) as client:
            before_send()
            with (
                measure("provider"),
                client.stream(
                    "POST",
                    _ENDPOINT,
                    content=body,
                    headers={
                        "Authorization": f"Bearer {self._api_key.get_secret_value()}",
                        "Content-Type": "application/json",
                        "Accept": "application/json",
                    },
                ) as response,
            ):
                if response.status_code != 200:
                    raise ValueError("model service unavailable")
                chunks = bytearray()
                for chunk in response.iter_bytes():
                    if len(chunks) + len(chunk) > _MAX_RESPONSE_BYTES:
                        raise ValueError("model response too large")
                    chunks.extend(chunk)
                completion = _Completion.model_validate(_strict_json(bytes(chunks)))
                request_id = response.headers.get("x-request-id", completion.id)
                if not 1 <= len(request_id) <= 200:
                    request_id = completion.id
                return completion, request_id

    def _reply(
        self,
        completion: _Completion,
        reverse: dict[str, str],
        tools: tuple[ToolDescriptor, ...],
    ) -> ModelReply:
        choice = completion.choices[0]
        calls = choice.message.tool_calls or []
        if (calls and choice.finish_reason != "tool_calls") or (
            not calls and choice.finish_reason != "stop"
        ):
            raise ValueError("incomplete model response")
        descriptors = {tool.name: tool for tool in tools}
        converted: list[ModelToolCall] = []
        for call in calls:
            name = reverse.get(call.function.name)
            if name is None:
                raise ValueError("unregistered model tool")
            arguments = _arguments(_strict_json(call.function.arguments), descriptors[name])
            converted.append(ModelToolCall(call_id=call.id, name=name, arguments=arguments))
        reply = ModelReply(text=choice.message.content or None, calls=tuple(converted))
        assistant_digest = _assistant_sha256(ModelMessage.from_reply(reply))
        private_payload = _DeepSeekContinuation(
            reasoning=choice.message.reasoning_content,
            assistant_sha256=assistant_digest,
        )
        # Count the full private envelope, including UTF-8 and JSON escaping. This
        # upper-bounds its reasoning_content wire contribution without exposing it.
        retention_bytes = len(
            _encoded(
                {
                    "reasoning_content": choice.message.reasoning_content.get_secret_value(),
                    "assistant_sha256": assistant_digest,
                }
            ).encode("utf-8")
        )
        continuation = ModelContinuation(private_payload, retention_bytes=retention_bytes)
        return reply.with_private_continuation(continuation)


def parse_model_call_usage(
    raw_body: bytes,
    *,
    request_id: str | None,
    expected_model: str,
    max_input_tokens: int,
    max_output_tokens: int,
) -> ModelCallUsage:
    """Validate original raw accounting before an SDK can coerce or discard it.

    This pure boundary reuses the original strict response contract and owns no
    price or reservation. Tool/reply acceptance remains a later decision.
    """
    measured: ModelCallUsage | None = None
    try:
        if len(raw_body) > _MAX_RESPONSE_BYTES:
            raise ValueError("model response too large")
        completion = _Completion.model_validate(_strict_json(raw_body))
        usage = completion.usage
        if (
            completion.model != expected_model
            or usage.prompt_tokens > max_input_tokens
            or usage.completion_tokens > max_output_tokens
        ):
            raise ValueError("unapproved model identity or usage exceeds reservation")
        if type(request_id) is not str or not 1 <= len(request_id) <= 200:
            request_id = completion.id
        measured = ModelCallUsage(
            model=completion.model,
            request_id=request_id,
            completion_id=completion.id,
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens,
            total_tokens=usage.total_tokens,
            prompt_cache_hit_tokens=usage.prompt_cache_hit_tokens,
            prompt_cache_miss_tokens=usage.prompt_cache_miss_tokens,
            reasoning_tokens=None
            if usage.completion_tokens_details is None
            else usage.completion_tokens_details.reasoning_tokens,
        )
    except Exception:
        pass
    if measured is None:
        # Outside the handler: no raw validation exception context is exported.
        raise DeepSeekModelError("model response accounting unavailable or invalid")
    return measured
