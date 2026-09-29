from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

ScopeType = Literal["all", "self", "department", "location", "inspection"]


class MutationRequest(BaseModel):
    """定义所有权限配置变更都必须携带的可追溯理由。"""

    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=500)

    @field_validator("reason")
    @classmethod
    def validate_reason(cls, value: str) -> str:
        """拒绝仅包含空白字符的配置变更理由。"""

        normalized_value = value.strip()
        if not normalized_value:
            raise ValueError("变更理由不能为空。")
        return normalized_value


class RoleAssignmentRequest(MutationRequest):
    """描述向指定用户授予一个既有角色的请求。"""

    role_code: str = Field(min_length=1, max_length=80, pattern=r"^[a-z][a-z0-9_]*$")


class RoleRevocationRequest(MutationRequest):
    """描述从指定用户撤销一个既有普通角色的请求。"""


class ScopeAssignmentRequest(MutationRequest):
    """描述向指定用户授予一个责任数据范围的请求。"""

    scope_type: ScopeType
    scope_value: str = Field(min_length=1, max_length=255)

    @field_validator("scope_value")
    @classmethod
    def validate_scope_value(cls, value: str) -> str:
        """拒绝仅包含空白字符的责任范围值。"""

        normalized_value = value.strip()
        if not normalized_value:
            raise ValueError("责任范围值不能为空。")
        return normalized_value


class PermissionGrantRequest(MutationRequest):
    """描述向指定角色授予一个动作与范围类型许可的请求。"""

    action_code: str = Field(
        min_length=3,
        max_length=120,
        pattern=r"^[a-z][a-z0-9_.-]*$",
    )
    scope_type: ScopeType


class RoleAssignmentResponse(BaseModel):
    """返回已创建的用户角色配置标识。"""

    id: int
    user_id: int
    role_code: str
    status: Literal["created"]


class RoleRevocationResponse(BaseModel):
    """返回已删除的用户角色配置标识。"""

    id: int
    user_id: int
    role_code: str
    status: Literal["deleted"]


class ScopeAssignmentResponse(BaseModel):
    """返回已创建的用户责任范围配置标识。"""

    id: int
    user_id: int
    scope_type: ScopeType
    scope_value: str
    status: Literal["created"]


class PermissionGrantResponse(BaseModel):
    """返回已创建的角色动作范围许可标识。"""

    id: int
    role_code: str
    action_code: str
    scope_type: ScopeType
    status: Literal["created"]
