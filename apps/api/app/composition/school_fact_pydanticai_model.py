"""Opt-in PydanticAI wire boundary using the original approved budget owner."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from time import monotonic
from typing import Any, Never, cast

import httpx2
from openai import AsyncOpenAI
from pydantic import SecretStr
from pydantic_ai import RunContext
from pydantic_ai.messages import ModelMessage, ModelResponse
from pydantic_ai.models import Model, ModelRequestParameters, StreamedResponse
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIChatModelSettings
from pydantic_ai.models.wrapper import WrapperModel
from pydantic_ai.profiles import merge_profile
from pydantic_ai.profiles.openai import OpenAIModelProfile
from pydantic_ai.providers.deepseek import DeepSeekProvider
from pydantic_ai.settings import ModelSettings

from app.composition.deepseek_assistant_model import (
    CurrentModelApproval,
    DeepSeekModelError,
    ModelCallPermit,
    ModelCallReservation,
    parse_model_call_usage,
)
from app.platform.assistant.diagnostics import (
    measure,
    record_model_input,
    record_protocol_response,
    record_provider_io,
    record_usage,
)
from app.platform.assistant.pydanticai_errors import rebuild_runtime_error, runtime_error_category
from app.platform.assistant.runtime import AssistantRuntimeError, AssistantServiceError

_ENDPOINT = "https://api.deepseek.com/chat/completions"
_MAX_RESPONSE_BYTES = 1_048_576


@dataclass
class _Failure:
    reason: str = "dependency"
    runtime: type[AssistantRuntimeError] | None = None


def _unique_arguments(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate tool argument")
        result[key] = value
    return result


def _reject_constant(value: str) -> Never:
    raise ValueError("invalid tool argument number")


def _record_wire_components(encoded: bytes) -> None:
    """Count the actual SDK envelope in memory; retain neither values nor names.

    Message totals include JSON framing. Reasoning is its UTF-8 text subset of
    assistant messages, not an additional component to add to their byte total.
    Diagnostic parsing cannot turn malformed or oversized packets into a send.
    """
    try:
        body = json.loads(encoded)
        messages = body.get("messages", [])
        tools = body.get("tools", [])

        def size(value: object) -> int:
            return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode())

        def role_size(role: str) -> int:
            return sum(size(message) for message in messages if message.get("role") == role)

        record_model_input(
            wire_bytes=len(encoded),
            tool_count=len(tools),
            message_count=len(messages),
            tool_schema_bytes=size(tools),
            system_message_bytes=role_size("system"),
            user_message_bytes=role_size("user"),
            assistant_message_bytes=role_size("assistant"),
            tool_message_bytes=role_size("tool"),
            reasoning_bytes=sum(
                len(message.get("reasoning_content", "").encode())
                for message in messages
                if message.get("role") == "assistant"
            ),
        )
    except (TypeError, ValueError, AttributeError):
        record_model_input(wire_bytes=len(encoded))


class _GuardedTransport(httpx2.AsyncBaseTransport):
    def __init__(
        self,
        inner: httpx2.AsyncBaseTransport,
        permit: ModelCallPermit,
        current: Callable[[], None],
        max_input_tokens: int,
        max_output_tokens: int,
        failure: ContextVar[_Failure | None],
        wire_guard: Callable[[bytes], None] | None = None,
    ) -> None:
        self.inner, self.permit, self.current = inner, permit, current
        self.max_input_tokens, self.max_output_tokens = max_input_tokens, max_output_tokens
        self.failure = failure
        self.wire_guard = wire_guard

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        reservation: ModelCallReservation | None = None
        response: httpx2.Response | None = None
        settled = False
        io_started: float | None = None
        milestones: dict[str, float] = {}
        body_chunks = body_bytes = whitespace_bytes = 0
        io_outcome = "other"

        async def trace_io(event: str, _info: object) -> None:
            # HTTP core info may contain credentials/body/exception objects.
            # Deliberately inspect only fixed event names, never _info.
            stage = {
                "connection.connect_tcp.complete": "tcp_connected",
                "connection.start_tls.complete": "tls_ready",
                "http11.send_request_body.complete": "request_sent",
                "http2.send_request_body.complete": "request_sent",
            }.get(event)
            if stage is not None and io_started is not None:
                milestones.setdefault(stage, monotonic() - io_started)

        try:
            # Bound the SDK's actual UTF-8 envelope after its setup and encoding,
            # including tools and all retained private thinking.
            encoded = request.content
            _record_wire_components(encoded)
            if len(encoded) > self.max_input_tokens:
                raise DeepSeekModelError(
                    "model input exceeds approved bound", reason="context_limit"
                )
            body = json.loads(encoded)
            if self.wire_guard is not None:
                self.wire_guard(encoded)
            if (
                request.method != "POST"
                or str(request.url) != _ENDPOINT
                or body.get("model") != "deepseek-flash"
                or body.get("thinking") != {"type": "enabled"}
                or body.get("reasoning_effort") != "high"
                or body.get("max_tokens") != self.max_output_tokens
                or "max_completion_tokens" in body
                or body.get("stream") is not False
                or body.get("parallel_tool_calls") is not False
            ):
                raise DeepSeekModelError("unapproved model request")
            reservation = self.permit.reserve(self.max_input_tokens, self.max_output_tokens)
            marker = getattr(reservation, "mark_transmission_started", None)
            if not callable(marker):
                raise DeepSeekModelError("model transmission accounting unavailable")
            marker()
            # The durable marker can wait on the original ledger. This last
            # authority/deadline check must remain adjacent to HTTP release.
            self.current()
            with measure("provider"):
                io_started = monotonic()
                request.extensions["trace"] = trace_io
                response = await self.inner.handle_async_request(request)
                milestones["headers_received"] = monotonic() - io_started
                if response.status_code != 200:
                    raise DeepSeekModelError("model service unavailable")
                chunks = bytearray()
                async for chunk in response.aiter_bytes():
                    if chunk:
                        body_chunks += 1
                        body_bytes += len(chunk)
                        if not chunk.strip(b" \r\n\t"):
                            whitespace_bytes += len(chunk)
                        milestones.setdefault("first_body", monotonic() - io_started)
                        milestones["last_body"] = monotonic() - io_started
                    if len(chunks) + len(chunk) > _MAX_RESPONSE_BYTES:
                        raise DeepSeekModelError("model response too large")
                    chunks.extend(chunk)
                milestones["body_complete"] = monotonic() - io_started
                io_outcome = "complete"
            raw = bytes(chunks)
            usage = parse_model_call_usage(
                raw,
                request_id=response.headers.get("x-request-id"),
                expected_model="deepseek-flash",
                max_input_tokens=self.max_input_tokens,
                max_output_tokens=self.max_output_tokens,
            )
            reservation.settle(usage)
            settled = True
            record_usage(
                prompt=usage.prompt_tokens,
                completion=usage.completion_tokens,
                reasoning=usage.reasoning_tokens,
                cache_hit=usage.prompt_cache_hit_tokens,
                cache_miss=usage.prompt_cache_miss_tokens,
            )
            # Accounting is already reliable. Unusable replies must not execute
            # tools, but they do not turn settled provider usage into unknown.
            choice = json.loads(raw)["choices"][0]
            message = choice["message"]
            content = message.get("content")
            record_protocol_response(
                layer="wire",
                finish_reason=choice["finish_reason"],
                content_state="missing"
                if "content" not in message
                else "null"
                if content is None
                else "empty"
                if content == ""
                else "nonempty",
                tool_call_count=len(message.get("tool_calls") or []),
                thinking_present=bool(message.get("reasoning_content")),
            )
            if choice["finish_reason"] == "length":
                raise DeepSeekModelError("model output was truncated", reason="output_limit")
            calls: list[dict[str, Any]] = choice["message"].get("tool_calls") or []
            if len(calls) > 8:
                raise DeepSeekModelError("tool batch limit exceeded", reason="tool_limit")
            if choice["finish_reason"] != ("tool_calls" if calls else "stop"):
                raise DeepSeekModelError("incomplete model response")
            names = {tool["function"]["name"] for tool in body.get("tools", [])}
            ids: set[str] = set()
            for call in calls:
                function = call["function"]
                if function["name"] not in names or call["id"] in ids:
                    raise DeepSeekModelError("unregistered or repeated model tool")
                ids.add(call["id"])
                arguments = json.loads(
                    function["arguments"],
                    object_pairs_hook=_unique_arguments,
                    parse_constant=_reject_constant,
                )
                if not isinstance(arguments, dict):
                    raise DeepSeekModelError("invalid model tool arguments")
            # aiter_bytes decoded content encodings already; do not decode again
            # when returning the validated in-memory payload to the SDK.
            headers = [
                (key, value)
                for key, value in response.headers.multi_items()
                if key.lower() not in {"content-encoding", "content-length", "transfer-encoding"}
            ]
            return httpx2.Response(200, headers=headers, content=raw, request=request)
        except AssistantServiceError as error:
            state = self.failure.get()
            if state is not None:
                state.reason = error.reason
            raise
        except AssistantRuntimeError as error:
            state = self.failure.get()
            if state is not None:
                state.runtime = runtime_error_category(error)
            raise
        except BaseException as error:
            io_outcome = next(
                (
                    label
                    for kind, label in (
                        (asyncio.CancelledError, "cancelled"),
                        (httpx2.ConnectTimeout, "connect_timeout"),
                        (httpx2.ReadTimeout, "read_timeout"),
                        (httpx2.WriteTimeout, "write_timeout"),
                        (httpx2.PoolTimeout, "pool_timeout"),
                        (httpx2.TransportError, "transport_error"),
                    )
                    if isinstance(error, kind)
                ),
                "other",
            )
            raise
        finally:
            # Also runs for CancelledError (BaseException). The original owner
            # intentionally has no abort/refund operation for held reservations.
            if reservation is not None and not settled:
                try:
                    reservation.unknown()
                except Exception:
                    pass  # Durable reservation stays occupied if recording fails.
            try:
                if response is not None:
                    await response.aclose()
            finally:
                if io_started is not None:
                    milestones["finished"] = monotonic() - io_started
                    record_provider_io(
                        milestones_seconds=milestones,
                        status=response.status_code if response is not None else None,
                        body_chunks=body_chunks,
                        body_bytes=body_bytes,
                        whitespace_bytes=whitespace_bytes,
                        outcome=io_outcome,
                    )

    async def aclose(self) -> None:
        await self.inner.aclose()


class _GuardedModel(WrapperModel):
    # _GuardedTransport checks once after SDK setup/reservation and the
    # durable transmission marker, immediately before the actual HTTP release.
    checks_transmission_before_send = True

    def __init__(
        self, wrapped: Model, client: httpx2.AsyncClient, failure: ContextVar[_Failure | None]
    ) -> None:
        super().__init__(wrapped)
        self._client, self._failure, self._closed = client, failure, False

    async def request(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
    ) -> ModelResponse:
        if self._closed:
            raise DeepSeekModelError("candidate model has been closed")
        if model_settings is not None and (
            model_settings.get("thinking", "high") != "high"
            or model_settings.get("openai_reasoning_effort", "high") != "high"
            or model_settings.get("parallel_tool_calls", False) is not False
        ):
            raise DeepSeekModelError("unapproved candidate model settings")
        state = _Failure()
        token = self._failure.set(state)
        try:
            return await super().request(messages, model_settings, model_request_parameters)
        except Exception:
            pass  # Never export SDK exceptions, response bodies or request objects.
        finally:
            self._failure.reset(token)
        # Outside the caught exception context, including any SDK cause chain.
        if state.runtime is not None:
            raise rebuild_runtime_error(state.runtime)
        raise DeepSeekModelError(
            "candidate model response unavailable or invalid", reason=state.reason
        )

    @asynccontextmanager
    async def request_stream(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
        run_context: RunContext[Any] | None = None,
    ) -> AsyncGenerator[StreamedResponse]:
        # This candidate meters complete responses only. Do not delegate to the
        # SDK streaming API, which has a different accounting/error lifecycle.
        raise DeepSeekModelError("candidate streaming is not supported")
        yield cast(StreamedResponse, None)  # pragma: no cover - unreachable context manager yield

    async def aclose(self) -> None:
        self._closed = True
        await self._client.aclose()


def build_guarded_model(
    *,
    api_key: SecretStr,
    permit: ModelCallPermit,
    approval: CurrentModelApproval,
    current: Callable[[], None],
    max_input_tokens: int,
    max_output_tokens: int,
    transport: httpx2.AsyncBaseTransport | None = None,
    live_wire_guard: Callable[[bytes], None] | None = None,
) -> Model:
    """Build an unenabled candidate; caller must await its aclose in finally."""
    if (
        type(approval) is not CurrentModelApproval
        or getattr(permit, "current_approval", None) != approval
        or getattr(permit, "candidate_approval", None) is not None
        or not isinstance(cast(object, api_key), SecretStr)
        or not api_key.get_secret_value().strip()
        or not callable(current)
        or type(max_input_tokens) is not int
        or not 1024 <= max_input_tokens <= 131072
        or type(max_output_tokens) is not int
        or not 1 <= max_output_tokens <= 32768
    ):
        raise DeepSeekModelError("approved candidate model configuration required")
    failure: ContextVar[_Failure | None] = ContextVar("candidate_model_failure", default=None)
    timeout = httpx2.Timeout(connect=10, read=60, write=30, pool=10)
    client = httpx2.AsyncClient(
        transport=_GuardedTransport(
            transport
            if transport is not None
            else httpx2.AsyncHTTPTransport(retries=0, trust_env=False),
            permit,
            current,
            max_input_tokens,
            max_output_tokens,
            failure,
            live_wire_guard,
        ),
        trust_env=False,
        follow_redirects=False,
        timeout=timeout,
    )
    sdk = AsyncOpenAI(
        api_key=api_key.get_secret_value(),
        base_url="https://api.deepseek.com",
        http_client=client,
        max_retries=0,
        timeout=timeout,
    )
    provider = DeepSeekProvider(openai_client=sdk)
    profile = merge_profile(
        provider.model_profile("deepseek-flash"),
        OpenAIModelProfile(
            # Preserve the owner-provided schema exactly as in the existing wire
            # contract; the framework must not infer stricter tool requirements.
            json_schema_transformer=None,
            openai_supports_strict_tool_definition=False,
            supports_thinking=True,
            openai_reasoning_enabled_by_default=True,
            openai_supports_forced_tool_choice_with_thinking=False,
            openai_chat_supports_max_completion_tokens=False,
        ),
    )
    wrapped = OpenAIChatModel(
        "deepseek-flash",
        provider=provider,
        profile=profile,
        settings=OpenAIChatModelSettings(
            thinking="high",
            openai_reasoning_effort="high",
            extra_body={"thinking": {"type": "enabled"}, "parallel_tool_calls": False},
            max_tokens=max_output_tokens,
            parallel_tool_calls=False,
        ),
    )
    return _GuardedModel(wrapped, client, failure)
