from __future__ import annotations

from typing import Annotated, Literal, Protocol, Self
from uuid import UUID

from pydantic import Field, StringConstraints, model_validator

from app.platform.contracts import FrozenContractModel, PositiveVersion

BusinessAuditStateCode = Literal[
    "assignment.absent",
    "assignment.confirmed",
    "repair.absent",
    "repair.submitted",
]
BusinessAuditFactCode = Literal["assignment.updated", "repair.submitted"]
BusinessAuditEventType = Literal[
    "master_data.assignment.changed",
    "repair.ticket.submitted",
]
BusinessAuditTargetType = Literal["staff_assignment", "repair_ticket"]
BusinessAuditReasonCode = Literal[
    "manual.assignment-confirmed",
    "manual.repair-submitted",
]


class BusinessAuditSummary(FrozenContractModel):
    """限制业务审计摘要只保存稳定状态、版本、引用和事实代码。"""

    state_code: BusinessAuditStateCode
    resource_version: PositiveVersion | None = None
    stable_reference_ids: tuple[UUID, ...] = Field(default=(), max_length=16)
    fact_codes: tuple[BusinessAuditFactCode, ...] = Field(default=(), max_length=32)

    @model_validator(mode="after")
    def validate_absent_state(self) -> Self:
        """不存在状态不能伪造资源版本或已经发生的事实。"""

        if self.state_code in ("assignment.absent", "repair.absent") and (
            self.resource_version is not None or self.fact_codes
        ):
            raise ValueError("尚不存在的资源不能带资源版本或完成事实。")
        return self


class BusinessAuditRecord(FrozenContractModel):
    """定义不承载姓名、电话、自由文本或外链的新业务审计记录。"""

    event_type: BusinessAuditEventType
    target_type: BusinessAuditTargetType
    target_id: UUID
    before_summary: BusinessAuditSummary
    after_summary: BusinessAuditSummary
    reason_code: BusinessAuditReasonCode

    @model_validator(mode="after")
    def validate_transition(self) -> Self:
        """当前类型化事件不允许把不存在状态写成操作后事实。"""

        transition = (
            self.event_type,
            self.target_type,
            self.before_summary.state_code,
            self.after_summary.state_code,
            self.after_summary.fact_codes,
            self.reason_code,
        )
        allowed = {
            (
                "master_data.assignment.changed",
                "staff_assignment",
                "assignment.absent",
                "assignment.confirmed",
                ("assignment.updated",),
                "manual.assignment-confirmed",
            ),
            (
                "repair.ticket.submitted",
                "repair_ticket",
                "repair.absent",
                "repair.submitted",
                ("repair.submitted",),
                "manual.repair-submitted",
            ),
        }
        if transition not in allowed:
            raise ValueError("业务审计事件、对象、状态、事实和原因必须属于同一完整组合。")
        if self.event_type == "repair.ticket.submitted" and (
            self.before_summary.resource_version is not None
            or self.before_summary.stable_reference_ids != (self.target_id,)
            or self.before_summary.fact_codes
            or self.after_summary.resource_version != 1
            or self.after_summary.stable_reference_ids != (self.target_id,)
        ):
            raise ValueError("报修提交审计必须完整绑定目标、版本和稳定引用。")
        return self


AuditId = Annotated[int, Field(strict=True, ge=1)]
IdempotencyKeyHash = Annotated[
    str,
    StringConstraints(strict=True, pattern=r"^[0-9a-f]{64}$"),
]


class BusinessAuditReceipt(FrozenContractModel):
    """返回数据库已接受的审计主键和幂等摘要，不返回可变 ORM 实体。"""

    audit_id: AuditId
    idempotency_key_hash: IdempotencyKeyHash


class TypedAuditWriter(Protocol):
    """定义已绑定可信命令上下文的窄业务审计写口。"""

    def append(self, record: BusinessAuditRecord) -> BusinessAuditReceipt: ...
