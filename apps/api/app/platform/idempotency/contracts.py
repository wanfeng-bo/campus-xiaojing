from __future__ import annotations

from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, Field, StringConstraints, model_validator

from app.platform.contracts import (
    AssignmentId,
    FrozenContractModel,
    GuardianRelationshipId,
    PersonId,
    PositiveVersion,
    SchoolId,
    SemanticCode,
)

Sha256Hex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
StableResourceReference = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=255),
]


class CommandIdempotencyIdentity(FrozenContractModel):
    """绑定学校、动作、单一操作者上下文、资源和规范请求摘要。"""

    school_id: SchoolId
    action_code: SemanticCode
    actor_user_id: int = Field(strict=True, ge=1)
    actor_person_id: PersonId
    actor_context_type: Literal["assignment", "guardian"]
    actor_assignment_id: AssignmentId | None = None
    actor_assignment_version: PositiveVersion | None = None
    actor_guardian_relationship_id: GuardianRelationshipId | None = None
    actor_guardian_relationship_version: PositiveVersion | None = None
    resource_type: SemanticCode
    resource_id: StableResourceReference
    idempotency_key_hash: Sha256Hex
    request_sha256: Sha256Hex

    @model_validator(mode="after")
    def require_one_actor_context(self) -> Self:
        assignment_complete = (
            self.actor_assignment_id is not None
            and self.actor_assignment_version is not None
            and self.actor_guardian_relationship_id is None
            and self.actor_guardian_relationship_version is None
        )
        guardian_complete = (
            self.actor_assignment_id is None
            and self.actor_assignment_version is None
            and self.actor_guardian_relationship_id is not None
            and self.actor_guardian_relationship_version is not None
        )
        if self.actor_context_type == "assignment" and assignment_complete:
            return self
        if self.actor_context_type == "guardian" and guardian_complete:
            return self
        raise ValueError("幂等范围只能绑定一种操作者上下文。")

    @property
    def actor_context_key(self) -> str:
        """生成仅由已核验上下文稳定字段构成的数据库唯一键。"""

        if self.actor_context_type == "assignment":
            return f"assignment:{self.actor_assignment_id}:{self.actor_assignment_version}"
        return (
            "guardian:"
            f"{self.actor_guardian_relationship_id}:"
            f"{self.actor_guardian_relationship_version}"
        )


class SafeCommandResult(FrozenContractModel):
    """限制幂等结果只能保存稳定引用、版本和结果代码。"""

    resource_type: SemanticCode
    resource_id: UUID
    resource_version: PositiveVersion
    outcome_code: SemanticCode


class IdempotencyRecordProjection(FrozenContractModel):
    """向 Handler 返回锁定幂等记录的安全状态，不返回任意 JSON。"""

    record_id: UUID
    status: Literal["in_progress", "succeeded", "terminal_failure"]
    request_sha256: Sha256Hex
    result_type: SemanticCode | None
    result: SafeCommandResult | None
    created_at: AwareDatetime
    updated_at: AwareDatetime


class IdempotencyRecordOwnerProjection(IdempotencyRecordProjection):
    """返回旧命令归属校验所需的稳定字段，不暴露任意结果载荷。"""

    action_code: SemanticCode
    actor_user_id: int = Field(strict=True, ge=1)
    actor_person_id: PersonId
    actor_context_type: Literal["assignment", "guardian"]
    actor_assignment_id: AssignmentId | None
    actor_assignment_version: PositiveVersion | None
    resource_type: SemanticCode
    resource_id: StableResourceReference
    idempotency_key_hash: Sha256Hex


class IdempotencyAcquisition(FrozenContractModel):
    """区分本事务新建记录与同一规范命令的恢复获取。"""

    record: IdempotencyRecordProjection
    created: bool = Field(strict=True)


class IdempotencyPayloadConflict(RuntimeError):
    """表示同一幂等范围和键被不同规范请求复用。"""
