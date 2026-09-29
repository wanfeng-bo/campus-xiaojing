from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.platform.authorization.models import ResponsibilityScope, Role, RolePermission, UserRole
from app.platform.identity.context import CurrentUser
from app.platform.identity.models import User


@dataclass(frozen=True)
class AuthorizationDecision:
    """描述一次服务端授权判断的结果及内部拒绝原因。"""

    allowed: bool
    reason: str


def authorize(
    *,
    session: Session,
    current_user: CurrentUser,
    action_code: str,
    scope_type: str,
    scope_value: str | None,
) -> AuthorizationDecision:
    """根据用户角色、动作许可和责任范围集中判断一次服务端授权。"""

    is_active = session.scalar(select(User.is_active).where(User.id == current_user.id))
    if is_active is not True:
        return AuthorizationDecision(allowed=False, reason="当前用户已停用或不存在。")

    permitted_scope_types = set(
        session.scalars(
            select(RolePermission.scope_type)
            .join(Role, Role.id == RolePermission.role_id)
            .join(UserRole, UserRole.role_id == Role.id)
            .where(
                UserRole.user_id == current_user.id,
                RolePermission.action_code == action_code,
            )
        ).all()
    )
    if not permitted_scope_types:
        return AuthorizationDecision(allowed=False, reason="当前用户缺少该操作权限。")
    if "all" in permitted_scope_types:
        return AuthorizationDecision(allowed=True, reason="当前角色拥有全范围权限。")
    if scope_type not in permitted_scope_types:
        return AuthorizationDecision(allowed=False, reason="当前用户缺少该范围类型权限。")
    if scope_type == "self":
        matched_self_scope = scope_value == str(current_user.id)
        return AuthorizationDecision(
            allowed=matched_self_scope,
            reason="自身范围匹配。" if matched_self_scope else "自身范围不匹配。",
        )
    if scope_value is None:
        return AuthorizationDecision(allowed=False, reason="缺少目标范围值。")

    assigned_scope_id = session.scalar(
        select(ResponsibilityScope.id).where(
            ResponsibilityScope.user_id == current_user.id,
            ResponsibilityScope.scope_type == scope_type,
            ResponsibilityScope.scope_value == scope_value,
        )
    )
    return AuthorizationDecision(
        allowed=assigned_scope_id is not None,
        reason="责任范围匹配。" if assigned_scope_id is not None else "责任范围不匹配。",
    )
