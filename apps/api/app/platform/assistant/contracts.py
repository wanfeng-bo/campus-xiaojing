from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, JsonValue, StringConstraints, model_validator


class AssistantContract(BaseModel):
    """所有内核协议共用的严格、不可变外壳。"""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        allow_inf_nan=False,
    )


ProtocolVersionV1 = Literal["1"]
OpaqueRef = Annotated[
    str,
    StringConstraints(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9_.:-]+$"),
]
ModuleRef = Annotated[
    str,
    StringConstraints(min_length=1, max_length=120, pattern=r"^[a-z][a-z0-9_.-]*$"),
]
FieldName = Annotated[
    str,
    StringConstraints(min_length=1, max_length=120, pattern=r"^[A-Za-z][A-Za-z0-9_.-]*$"),
]
NeutralSlotName = Annotated[
    str,
    StringConstraints(min_length=2, max_length=12, pattern=r"^S[1-9][0-9]*$"),
]
NonBlankText = Annotated[
    str,
    StringConstraints(min_length=1, max_length=8_000, pattern=r"\S"),
]
ModelSafeText = Annotated[
    str,
    StringConstraints(min_length=1, max_length=8_000, pattern=r"\S"),
]
JsonObject = dict[str, JsonValue]
type NonNullJsonValue = str | int | float | bool | list[JsonValue] | dict[str, JsonValue]


class TextSpanEvidenceV1(AssistantContract):
    """指向本轮老师原话的半开字符区间；内容判断留给模块。"""

    kind: Literal["text_span"]
    start: int = Field(strict=True, ge=0)
    end: int = Field(strict=True, ge=1)

    @model_validator(mode="after")
    def require_non_empty_span(self) -> Self:
        if self.end <= self.start:
            raise ValueError("evidence span must be non-empty")
        return self


class VerbatimEvidenceV1(AssistantContract):
    """携带模块仍须对照当前原话验证的逐字依据。"""

    kind: Literal["verbatim"]
    text: NonBlankText


EvidenceRefV1 = Annotated[
    TextSpanEvidenceV1 | VerbatimEvidenceV1,
    Field(discriminator="kind"),
]


class PrecompositionCandidateV1(AssistantContract):
    """只承载受限模板和字段句柄，不承载字段真实值。"""

    template_version: OpaqueRef
    display_template: NonBlankText
    field_names: tuple[FieldName, ...]

    @model_validator(mode="after")
    def require_unique_field_names(self) -> Self:
        if len(set(self.field_names)) != len(self.field_names):
            raise ValueError("precomposition field names must be unique")
        return self


class MeaningV1(AssistantContract):
    """超级大脑适配器返回的候选含义信封；因果引用由服务端本地补入。"""

    version: ProtocolVersionV1
    conversation_ref: OpaqueRef
    turn_ref: OpaqueRef
    run_ref: OpaqueRef
    mode: Literal["dispatch", "clarify", "none"]
    module_ref: ModuleRef | None
    meaning_payload: JsonObject
    evidence_refs: tuple[EvidenceRefV1, ...]
    candidate_steps: tuple[JsonObject, ...]
    precomposition_candidate: PrecompositionCandidateV1 | None
    model_safe_expression_input: ModelSafeText | None

    @model_validator(mode="after")
    def require_mode_envelope(self) -> Self:
        if self.mode == "dispatch":
            if self.module_ref is None:
                raise ValueError("dispatch requires exactly one module_ref")
            return self
        if self.module_ref is not None:
            raise ValueError("clarify and none must not carry module_ref")
        if self.model_safe_expression_input is None:
            raise ValueError("clarify and none require model_safe_expression_input")
        return self


class MeaningModelCorrelationV1(AssistantContract):
    """只供模型适配器本地关联并补封 MeaningV1，不序列化给 provider。"""

    conversation_ref: OpaqueRef
    turn_ref: OpaqueRef
    run_ref: OpaqueRef


class MeaningModelRequestV1(AssistantContract):
    """第一次模型调用唯一可见的输入边界。"""

    version: ProtocolVersionV1
    current_teacher_utterance: NonBlankText
    model_safe_context: tuple[ModelSafeText, ...]


class ExpressionModelRequestV1(AssistantContract):
    """第二次模型调用只见安全说明和中性槽名，不见槽值。"""

    version: ProtocolVersionV1
    model_safe_expression_input: ModelSafeText
    neutral_slot_names: tuple[NeutralSlotName, ...]

    @model_validator(mode="after")
    def require_unique_neutral_slots(self) -> Self:
        if len(set(self.neutral_slot_names)) != len(self.neutral_slot_names):
            raise ValueError("neutral slot names must be unique")
        return self


class ExpressionModelResultV1(AssistantContract):
    """第二次模型返回的待机械校验模板。"""

    version: ProtocolVersionV1
    rendered_template: NonBlankText


class ModuleCallV1(AssistantContract):
    """内核机械封装并投递给唯一模块的调用信封。"""

    version: ProtocolVersionV1
    module_ref: ModuleRef
    conversation_ref: OpaqueRef
    turn_ref: OpaqueRef
    run_ref: OpaqueRef
    actor_context_ref: OpaqueRef
    current_teacher_utterance: NonBlankText
    meaning_payload: JsonObject
    evidence_refs: tuple[EvidenceRefV1, ...]
    candidate_steps: tuple[JsonObject, ...]
    module_state_ref: OpaqueRef | None
    state_revision: int | None = Field(default=None, strict=True, ge=1)
    precomposition_candidate: PrecompositionCandidateV1 | None

    @model_validator(mode="after")
    def require_state_pair(self) -> Self:
        if (self.module_state_ref is None) != (self.state_revision is None):
            raise ValueError("module state ref and revision must appear together")
        return self


class ModuleConfirmationCallV1(AssistantContract):
    """点击后交给模块重新鉴权的服务端引用信封，不携带业务数据。"""

    version: ProtocolVersionV1
    module_ref: ModuleRef
    conversation_ref: OpaqueRef
    turn_ref: OpaqueRef
    run_ref: OpaqueRef
    actor_context_ref: OpaqueRef
    action_offer_ref: OpaqueRef
    module_state_ref: OpaqueRef
    state_revision: int = Field(strict=True, ge=1)
    confirmation_ref: OpaqueRef


class ModuleRecoveryCallV1(AssistantContract):
    """恢复点击交给模块重验当前 revision 的服务端引用信封。"""

    version: ProtocolVersionV1
    module_ref: ModuleRef
    conversation_ref: OpaqueRef
    turn_ref: OpaqueRef
    run_ref: OpaqueRef
    actor_context_ref: OpaqueRef
    recovery_control_ref: OpaqueRef
    module_state_ref: OpaqueRef
    state_revision: int = Field(strict=True, ge=1)


class ModuleResultCheckCallV1(AssistantContract):
    """结果核验点击只携带原动作的不透明句柄和当前服务端上下文引用。"""

    version: ProtocolVersionV1
    module_ref: ModuleRef
    conversation_ref: OpaqueRef
    turn_ref: OpaqueRef
    run_ref: OpaqueRef
    actor_context_ref: OpaqueRef
    result_check_ref: OpaqueRef


class ValidationReceiptV1(AssistantContract):
    """模块可公开的候选验证结论，不携带内部依据。"""

    status: Literal["accepted", "needs_input", "rejected"]
    safe_result_family: ModuleRef


class CapsuleUnchangedV1(AssistantContract):
    kind: Literal["unchanged"]


class CapsuleClearV1(AssistantContract):
    kind: Literal["clear"]


class CapsuleReplaceV1(AssistantContract):
    kind: Literal["replace"]
    model_safe_capsule: ModelSafeText


CapsuleUpdateV1 = Annotated[
    CapsuleUnchangedV1 | CapsuleClearV1 | CapsuleReplaceV1,
    Field(discriminator="kind"),
]


class FallbackPresentationV1(AssistantContract):
    """由模块确定性生成、无需模型即可显示的兜底。"""

    display_kind: Literal["text", "card"]
    display_payload: NonNullJsonValue


class ActionOfferV1(AssistantContract):
    """只暴露确定性确认界面所需的服务端句柄与安全标签。"""

    action_offer_ref: OpaqueRef
    safe_label: NonBlankText


class RecoveryControlV1(AssistantContract):
    """只暴露恢复控制句柄与安全标签。"""

    recovery_control_ref: OpaqueRef
    safe_label: NonBlankText


class ResultCheckHandleV1(AssistantContract):
    """正式结果探测的服务端不透明句柄。"""

    result_check_ref: OpaqueRef


class PublicControlV1(AssistantContract):
    """展示层唯一可见的签名点击句柄；不携带模块私有引用。"""

    kind: Literal["action_offer", "recovery_control", "result_check_handle"]
    handle: OpaqueRef
    safe_label: NonBlankText
    expires_at: int = Field(default=9_223_372_036_854_775_807, strict=True, ge=1, exclude=True)
    manual_review_on_expiry: bool = Field(default=False, exclude=True)
    module_ref: ModuleRef = Field(default="assistant.unknown", exclude=True)


class ModuleResultV1(AssistantContract):
    """模块结果的固定外壳；三档呈现字段严格互斥。"""

    version: ProtocolVersionV1
    validation_receipt: ValidationReceiptV1
    locked_notice: NonBlankText | None
    staged_state_ref: OpaqueRef
    state_revision: int = Field(strict=True, ge=1)
    capsule_update: CapsuleUpdateV1
    presentation_mode: Literal["direct", "precomposed", "compose"]
    display_kind: Literal["text", "card"]
    display_payload: JsonValue | None
    display_template: NonBlankText | None
    field_map: dict[FieldName, JsonValue] | None
    model_safe_narration: ModelSafeText | None
    slot_map: dict[NeutralSlotName, JsonValue] | None
    fallback_presentation: FallbackPresentationV1 | None
    model_safe_note: ModelSafeText | None
    action_offer: ActionOfferV1 | None
    recovery_control: RecoveryControlV1 | None
    result_check_handle: ResultCheckHandleV1 | None
    safe_error_projection: NonBlankText | None
    diagnostic_ref: OpaqueRef | None

    @model_validator(mode="after")
    def require_exact_presentation_shape(self) -> Self:
        direct = self.presentation_mode == "direct"
        precomposed = self.presentation_mode == "precomposed"
        compose = self.presentation_mode == "compose"

        valid = (
            (
                direct
                and self.display_payload is not None
                and self.display_template is None
                and self.field_map is None
                and self.model_safe_narration is None
                and self.slot_map is None
                and self.fallback_presentation is None
            )
            or (
                precomposed
                and self.display_payload is None
                and self.display_template is not None
                and self.field_map is not None
                and self.model_safe_narration is None
                and self.slot_map is None
                and self.fallback_presentation is not None
            )
            or (
                compose
                and self.display_payload is None
                and self.display_template is None
                and self.field_map is None
                and self.model_safe_narration is not None
                and self.slot_map is not None
                and self.fallback_presentation is not None
            )
        )
        if not valid:
            raise ValueError("presentation fields do not match presentation_mode")
        return self
