from __future__ import annotations

from typing import Annotated, Literal, Self, cast
from uuid import UUID

from pydantic import (
    AwareDatetime,
    ConfigDict,
    Field,
    ModelWrapValidatorHandler,
    StringConstraints,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic_core import InitErrorDetails

from app.platform.contracts import (
    AssignmentId,
    DisplayName,
    EffectiveInterval,
    FrozenContractModel,
    LocationId,
    PersonId,
    PositiveVersion,
    ResponsibilityGroupId,
    SchoolId,
)

RepairLocationText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=200),
]
RepairProblemDescription = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=2_000),
]
RepairDraftLocationCandidate = Annotated[
    str,
    StringConstraints(strip_whitespace=True, max_length=200),
]
RepairDraftProblemCandidate = Annotated[
    str,
    StringConstraints(strip_whitespace=True, max_length=2_000),
]
RepairPhone = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        pattern=r"^\+?[0-9]{7,15}$",
    ),
]
RepairPublicReference = Annotated[
    str,
    StringConstraints(pattern=r"^rep_[A-Za-z0-9_-]{20,60}$"),
]
RepairScopePurpose = Literal["handler", "general_affairs"]
RepairScopeKind = Literal["school", "location"]
RepairRouteKind = Literal["ordinary", "general_affairs", "unresolved"]
RepairTicketStatus = Literal["submitted", "processing", "completed"]
RepairCardEditCandidateKind = Literal["edit_location", "edit_problem", "unclear"]
RepairCardEditDecisionKind = Literal["edit_location", "edit_problem", "unresolved"]
RepairPresentationRelation = Literal[
    "reported_by_me",
    "available_to_handle",
    "needs_general_affairs",
]
RepairResolutionBranch = Literal[
    "location_handler",
    "school_handler_after_location_zero",
    "school_handler_without_location",
    "general_affairs",
    "unresolved",
]


class _SensitiveValidationContract(FrozenContractModel):
    """只为本模块两个电话契约移除结构化校验错误中的原始输入。"""

    @model_validator(mode="wrap")
    @classmethod
    def remove_input_from_validation_errors(
        cls,
        value: object,
        handler: ModelWrapValidatorHandler[Self],
    ) -> Self:
        try:
            return handler(value)
        except ValidationError as exc:
            sanitized_errors = cast(
                list[InitErrorDetails],
                exc.errors(include_url=False, include_input=False),
            )
            raise ValidationError.from_exception_data(
                cls.__name__,
                sanitized_errors,
                hide_input=True,
            ) from None


class RepairDraftSourceText(FrozenContractModel):
    """校验本次主动发送的原话，但绝不修剪、识别或改写其中内容。"""

    raw_text: Annotated[str, StringConstraints(min_length=1, max_length=2_000)]

    @field_validator("raw_text")
    @classmethod
    def require_nonblank_text(cls, value: str) -> str:
        """拒绝全空白输入，同时原样保留空格、换行、数字和其他正文。"""

        if not value.strip():
            raise ValueError("报修原话不能为空。")
        return value


class RepairCardEditSource(FrozenContractModel):
    """承载本次改卡原话和当前唯一草稿的两个既有字段。"""

    raw_text: Annotated[str, StringConstraints(min_length=1, max_length=2_000)]
    current_location_text: RepairLocationText
    current_problem_description: RepairProblemDescription

    @field_validator("raw_text")
    @classmethod
    def require_nonblank_raw_text(cls, value: str) -> str:
        """复用原话边界：只拒绝全空白，不修剪或改写正文。"""

        return RepairDraftSourceText(raw_text=value).raw_text


class RepairCardEditCandidate(FrozenContractModel):
    """模型只可提出一个单字段替换候选或明确无法判断。"""

    kind: RepairCardEditCandidateKind
    replacement_text: Annotated[
        str,
        StringConstraints(strip_whitespace=True, max_length=2_000),
    ]
    evidence: Annotated[str, StringConstraints(max_length=2_000)]

    @model_validator(mode="after")
    def require_kind_payload_pairing(self) -> Self:
        if self.kind == "unclear":
            if self.replacement_text or self.evidence:
                raise ValueError("无法判断时不得携带替换值或依据。")
            return self
        if not self.replacement_text or not self.evidence.strip():
            raise ValueError("单字段修改必须同时包含替换值和本句依据。")
        return self


class RepairCardEditDecision(FrozenContractModel):
    """程序验证后的单字段修改决定；失败统一为 unresolved。"""

    kind: RepairCardEditDecisionKind
    replacement_text: str | None

    @model_validator(mode="after")
    def require_kind_payload_pairing(self) -> Self:
        if self.kind == "unresolved":
            if self.replacement_text is not None:
                raise ValueError("无法确认修改时不得携带替换值。")
            return self
        if self.replacement_text is None or not self.replacement_text.strip():
            raise ValueError("已确认的单字段修改必须包含替换值。")
        return self


class RepairDraftCandidate(FrozenContractModel):
    """只承载模型从本次原话提取的、尚未形成业务事实的两字段候选。"""

    location_text: RepairDraftLocationCandidate
    problem_description: RepairDraftProblemCandidate


class RepairEntryInterpretationCandidate(FrozenContractModel):
    """一次模型调用给出的入口判断和本句报修字段依据。"""

    decision: Literal["repair", "unsupported", "unclear"]
    location_text: RepairDraftLocationCandidate
    location_evidence: RepairDraftLocationCandidate
    problem_description: RepairDraftProblemCandidate
    problem_evidence: RepairDraftProblemCandidate

    @model_validator(mode="after")
    def require_decision_payload_match(self) -> Self:
        pairs_match = bool(self.location_text) == bool(self.location_evidence) and (
            bool(self.problem_description) == bool(self.problem_evidence)
        )
        if not pairs_match:
            raise ValueError("入口判断字段与依据不一致。")
        has_any_pair = bool(self.location_text) or bool(self.problem_description)
        if self.decision == "repair" and not has_any_pair:
            raise ValueError("报修入口判断缺少本句字段依据。")
        if self.decision != "repair" and has_any_pair:
            raise ValueError("非报修入口判断不得携带报修字段。")
        return self


class RepairDraftDecision(FrozenContractModel):
    """由确定性程序发布是否可以出卡以及当前唯一缺口。"""

    kind: Literal["ready", "needs_location", "needs_problem", "unclear", "unsupported"]
    location_text: RepairDraftLocationCandidate
    problem_description: RepairDraftProblemCandidate
    question: Annotated[str, StringConstraints(min_length=1, max_length=500)] | None

    @model_validator(mode="after")
    def require_kind_payload_match(self) -> Self:
        ready = (
            bool(self.location_text) and bool(self.problem_description) and self.question is None
        )
        needs_location = (
            not self.location_text and bool(self.problem_description) and self.question is not None
        )
        needs_problem = (
            bool(self.location_text) and not self.problem_description and self.question is not None
        )
        unclear = (
            not self.location_text and not self.problem_description and self.question is not None
        )
        valid_by_kind = {
            "ready": ready,
            "needs_location": needs_location,
            "needs_problem": needs_problem,
            "unclear": unclear,
            "unsupported": unclear,
        }
        if not valid_by_kind[self.kind]:
            raise ValueError("报修整理判定与字段不一致。")
        return self


class RepairDraftContinuation(FrozenContractModel):
    """只携带当前页面已经接受的字段和本轮唯一缺口。"""

    missing: Literal["location", "problem", "both"]
    location_text: RepairDraftLocationCandidate
    problem_description: RepairDraftProblemCandidate

    @model_validator(mode="after")
    def require_missing_payload_match(self) -> Self:
        valid = {
            "location": not self.location_text and bool(self.problem_description),
            "problem": bool(self.location_text) and not self.problem_description,
            "both": not self.location_text and not self.problem_description,
        }
        if not valid[self.missing]:
            raise ValueError("报修追问缺口与已接受字段不一致。")
        return self


class SubmitRepair(_SensitiveValidationContract):
    """只接受报修人本次可编辑内容，不接收任何权威身份或状态字段。"""

    model_config = ConfigDict(hide_input_in_errors=True)

    location_text: RepairLocationText
    problem_description: RepairProblemDescription
    explicit_location_id: LocationId | None = None
    manual_phone: RepairPhone | None = Field(default=None, repr=False)


class TrustedRepairPhone(_SensitiveValidationContract):
    """把登录会话电话绑定到取得它的账号与授权版本，正文不进入调试表示。"""

    model_config = ConfigDict(hide_input_in_errors=True)

    account_user_id: int = Field(strict=True, ge=1)
    authorization_version: PositiveVersion
    value: RepairPhone = Field(repr=False)


class RepairCommandResult(FrozenContractModel):
    """返回正式工单的安全结果，不携带内部 UUID、电话或自由正文。"""

    kind: Literal["created", "replayed"]
    public_ref: RepairPublicReference
    status: Literal["submitted"]
    route_kind: RepairRouteKind
    candidate_count: int = Field(strict=True, ge=0)
    revision: PositiveVersion


class RepairSubmissionProjection(FrozenContractModel):
    """供重放探测读取的不可变提交身份与初始路由投影。"""

    ticket_id: UUID
    school_id: SchoolId
    public_ref: RepairPublicReference
    reporter_person_id: PersonId
    reporter_assignment_id: AssignmentId
    reporter_assignment_version: PositiveVersion
    route_kind: RepairRouteKind
    candidate_count: int = Field(strict=True, ge=0)


class RepairPresentationAssignmentRef(FrozenContractModel):
    """表示服务端当前关系解析已经确认的一条精确任职引用。"""

    person_id: PersonId
    assignment_id: AssignmentId
    assignment_version: PositiveVersion


class RepairPresentationRelationSeed(FrozenContractModel):
    """只承载关系判定引用，不包含姓名、电话、地点或问题正文。"""

    ticket_id: UUID
    school_id: SchoolId
    reporter_person_id: PersonId
    handler_assignments: tuple[RepairPresentationAssignmentRef, ...]
    general_affairs_assignments: tuple[RepairPresentationAssignmentRef, ...]


class RepairPresentationTicketProjection(FrozenContractModel):
    """只在当前关系成立后读取一张工单的最小呈现字段。"""

    ticket_id: UUID
    school_id: SchoolId
    public_ref: RepairPublicReference
    status: RepairTicketStatus
    location_text: RepairLocationText
    problem_description: RepairProblemDescription
    reporter_name: DisplayName
    reporter_phone: RepairPhone | None


class RepairPresentationItem(FrozenContractModel):
    """只返回当前关系允许呈现的公开报修字段。"""

    relation: RepairPresentationRelation
    public_ref: RepairPublicReference
    status: RepairTicketStatus
    location_text: RepairLocationText
    problem_description: RepairProblemDescription
    reporter_name: DisplayName
    reporter_phone: RepairPhone | None


class RepairPresentationResult(FrozenContractModel):
    """返回当前精确任职可见的关系项；空结果不附带存在性提示。"""

    items: tuple[RepairPresentationItem, ...]


class RepairScopeBindingFact(FrozenContractModel):
    """表示报修模块自己拥有的一条当前责任范围绑定。"""

    binding_id: UUID
    school_id: SchoolId
    purpose: RepairScopePurpose
    scope_kind: RepairScopeKind
    location_id: LocationId | None
    responsibility_group_id: ResponsibilityGroupId
    responsibility_group_version: PositiveVersion
    status: Literal["active", "inactive"]
    version: PositiveVersion
    interval: EffectiveInterval


class RepairRouteCandidate(FrozenContractModel):
    """保存路由候选的稳定身份与版本，供提交事务锁后比较。"""

    membership_id: UUID
    membership_version: PositiveVersion
    person_id: PersonId
    assignment_id: AssignmentId
    assignment_version: PositiveVersion


class InitialRepairRoute(FrozenContractModel):
    """解释提交当时的确定性责任解析结果，但不持续授予权限。"""

    school_id: SchoolId
    route_kind: RepairRouteKind
    resolution_branch: RepairResolutionBranch
    recognized_location_id: LocationId | None
    binding_id: UUID | None
    binding_version: PositiveVersion | None
    responsibility_group_id: ResponsibilityGroupId | None
    responsibility_group_version: PositiveVersion | None
    candidates: tuple[RepairRouteCandidate, ...]
    resolved_at: AwareDatetime

    @property
    def candidate_assignment_ids(self) -> tuple[AssignmentId, ...]:
        """按稳定顺序返回候选任职引用，便于锁序和最小结果断言。"""

        return tuple(candidate.assignment_id for candidate in self.candidates)

    @property
    def candidate_count(self) -> int:
        return len(self.candidates)
