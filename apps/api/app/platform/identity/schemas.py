from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.platform.identity.execution_context import SelectedContextRef


class DingTalkExchangeRequest(BaseModel):
    """定义前端提交一次性钉钉免登码的最小请求，不接受任何角色或身份字段。"""

    model_config = ConfigDict(extra="forbid")

    auth_code: str = Field(min_length=1, max_length=4096)


class IdentityAccessStatus(BaseModel):
    """定义前端可见的最小访问状态，不泄露角色名称或完整权限集合。"""

    access_state: Literal["ready", "pending_configuration"]
    can_manage_system: bool


class LogoutResponse(BaseModel):
    """定义退出本地身份会话后的安全响应。"""

    status: Literal["logged_out"]


class IdentityContextsResponse(BaseModel):
    """只返回不透明引用及人可读标签，不暴露内部身份与权限集合。"""

    model_config = ConfigDict(extra="forbid")

    items: list[SelectedContextRef]
