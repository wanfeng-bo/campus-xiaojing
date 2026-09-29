from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.platform.assistant.contracts import NonBlankText, NonNullJsonValue, OpaqueRef

type AssistantPathRef = OpaqueRef


class _StrictHttpModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class AssistantTurnRequest(_StrictHttpModel):
    teacher_utterance: NonBlankText


class AssistantStopRequest(_StrictHttpModel):
    generation: int = Field(strict=True, ge=1)


class AssistantActiveRunResponse(_StrictHttpModel):
    turn_ref: OpaqueRef
    run_ref: OpaqueRef
    generation: int
    continuation_of: OpaqueRef | None = Field(default=None, exclude_if=lambda value: value is None)


class AssistantDisplayTurnResponse(_StrictHttpModel):
    turn_ref: OpaqueRef
    run_ref: OpaqueRef
    generation: int
    teacher_utterance: NonBlankText
    visible_state: Literal["active", "completed", "stopped"]
    display_kind: Literal["text", "card"] | None
    display_payload: NonNullJsonValue | None
    controls: tuple[AssistantControlResponse, ...]
    continuation_of: OpaqueRef | None = Field(default=None, exclude_if=lambda value: value is None)


class AssistantControlResponse(_StrictHttpModel):
    kind: Literal["action_offer", "recovery_control", "result_check_handle"]
    handle: OpaqueRef
    safe_label: NonBlankText


class AssistantConversationResponse(_StrictHttpModel):
    version: Literal["1"]
    conversation_ref: OpaqueRef
    generation: int
    active_run: AssistantActiveRunResponse | None
    turns: tuple[AssistantDisplayTurnResponse, ...]


__all__ = [
    "AssistantActiveRunResponse",
    "AssistantConversationResponse",
    "AssistantControlResponse",
    "AssistantDisplayTurnResponse",
    "AssistantPathRef",
    "AssistantStopRequest",
    "AssistantTurnRequest",
    "AssistantTurnSnapshotEvent",
    "AssistantTurnErrorEvent",
]


class AssistantTurnSnapshotEvent(_StrictHttpModel):
    event: Literal["accepted", "completed"]
    snapshot: AssistantConversationResponse


class AssistantTurnErrorEvent(_StrictHttpModel):
    event: Literal["error"] = "error"
    code: Literal[
        "assistant.runtime_unavailable",
        "assistant.run_stale",
        "assistant.dependency",
        "assistant.tool_limit",
        "assistant.deadline",
        "assistant.budget",
        "assistant.context_limit",
        "assistant.output_limit",
        "assistant.data_policy",
    ]
