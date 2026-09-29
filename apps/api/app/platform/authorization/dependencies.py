from __future__ import annotations

from collections.abc import Callable
from typing import Annotated

from fastapi import Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.db.connection import get_session
from app.platform.authorization.service import authorize
from app.platform.identity.context import CurrentUser, get_current_user


def require_permission(
    action_code: str, scope_type: str, scope_value: str | None
) -> Callable[..., CurrentUser]:
    """创建路由可复用的服务端权限依赖，拒绝时返回统一中文提示。"""

    def check_permission(
        current_user: Annotated[CurrentUser, Depends(get_current_user)],
        session: Annotated[Session, Depends(get_session)],
    ) -> CurrentUser:
        """在实际请求会话中执行集中授权判断并返回已验证用户。"""

        decision = authorize(
            session=session,
            current_user=current_user,
            action_code=action_code,
            scope_type=scope_type,
            scope_value=scope_value,
        )
        if not decision.allowed:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="你暂无执行该操作的权限。",
            )
        return current_user

    return check_permission
