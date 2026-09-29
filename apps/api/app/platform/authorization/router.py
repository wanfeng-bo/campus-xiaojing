from __future__ import annotations

import logging
from typing import Annotated, cast

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.db.connection import get_session
from app.platform.audit.service import append_audit_log
from app.platform.authorization.dependencies import require_permission
from app.platform.authorization.models import ResponsibilityScope, Role, RolePermission, UserRole
from app.platform.authorization.schemas import (
    PermissionGrantRequest,
    PermissionGrantResponse,
    RoleAssignmentRequest,
    RoleAssignmentResponse,
    RoleRevocationRequest,
    RoleRevocationResponse,
    ScopeAssignmentRequest,
    ScopeAssignmentResponse,
)
from app.platform.identity.context import CurrentUser
from app.platform.identity.models import User

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/admin", tags=["authorization"])
SystemAccessWriter = Annotated[
    CurrentUser,
    Depends(require_permission("system.access.write", "all", None)),
]
DatabaseSession = Annotated[Session, Depends(get_session)]


def get_server_request_id(request: Request) -> str:
    """读取请求中间件生成的服务端追踪标识，禁止使用客户端传入值。"""

    request_id = getattr(request.state, "request_id", None)
    if not isinstance(request_id, str):
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="请求追踪初始化失败，请稍后重试。",
        )
    return request_id


ServerRequestId = Annotated[str, Depends(get_server_request_id)]


def raise_database_write_error(session: Session, message: str) -> None:
    """回滚当前配置事务、记录服务端异常并返回安全中文错误提示。"""

    session.rollback()
    logger.exception("权限配置写入失败")
    raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=message) from None


@router.post(
    "/users/{user_id}/roles",
    response_model=RoleAssignmentResponse,
    status_code=status.HTTP_201_CREATED,
)
def assign_user_role(
    user_id: int,
    payload: RoleAssignmentRequest,
    request_id: ServerRequestId,
    current_user: SystemAccessWriter,
    session: DatabaseSession,
) -> RoleAssignmentResponse:
    """向目标用户授予既有角色，并在同一事务内记录不可变审计。"""

    user = session.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="目标用户不存在。")
    role = session.scalar(select(Role).where(Role.code == payload.role_code))
    if role is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="目标角色不存在。")
    existing_assignment = session.scalar(
        select(UserRole).where(UserRole.user_id == user.id, UserRole.role_id == role.id)
    )
    if existing_assignment is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="该用户已拥有此角色。")

    assignment = UserRole(user_id=user.id, role_id=role.id, assigned_by_user_id=current_user.id)
    try:
        session.add(assignment)
        session.flush()
        append_audit_log(
            session=session,
            actor_user_id=current_user.id,
            event_type="authorization.role.assigned",
            target_type="user_role",
            target_id=str(assignment.id),
            before_summary={},
            after_summary={"user_id": user.id, "role_code": role.code},
            request_id=request_id,
            reason=payload.reason,
        )
        session.commit()
    except IntegrityError:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="该用户已拥有此角色。",
        ) from None
    except SQLAlchemyError:
        raise_database_write_error(session, "角色配置保存失败，请稍后重试。")

    return RoleAssignmentResponse(
        id=assignment.id,
        user_id=user.id,
        role_code=role.code,
        status="created",
    )


@router.delete(
    "/users/{user_id}/roles/{role_code}",
    response_model=RoleRevocationResponse,
)
def revoke_user_role(
    user_id: int,
    role_code: str,
    payload: RoleRevocationRequest,
    request_id: ServerRequestId,
    current_user: SystemAccessWriter,
    session: DatabaseSession,
) -> RoleRevocationResponse:
    """撤销目标用户的普通角色，并在同一事务记录不可变审计。"""

    user = session.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="目标用户不存在。")

    role = session.scalar(select(Role).where(Role.code == role_code))
    if role is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="目标角色不存在。")
    if role.code == "system_admin":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="系统管理员角色不能通过此接口撤销。",
        )

    try:
        assignment_id_result = session.scalar(
            delete(UserRole)
            .where(UserRole.user_id == user.id, UserRole.role_id == role.id)
            .returning(UserRole.id)
            .execution_options(synchronize_session=False)
        )
        if assignment_id_result is None:
            session.rollback()
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="目标用户未拥有此角色。",
            )
        assignment_id = cast(int, assignment_id_result)
        append_audit_log(
            session=session,
            actor_user_id=current_user.id,
            event_type="authorization.role.revoked",
            target_type="user_role",
            target_id=str(assignment_id),
            before_summary={"user_id": user.id, "role_code": role.code},
            after_summary={},
            request_id=request_id,
            reason=payload.reason,
        )
        session.commit()
    except SQLAlchemyError:
        raise_database_write_error(session, "角色撤销保存失败，请稍后重试。")

    return RoleRevocationResponse(
        id=assignment_id,
        user_id=user.id,
        role_code=role.code,
        status="deleted",
    )


@router.post(
    "/users/{user_id}/scopes",
    response_model=ScopeAssignmentResponse,
    status_code=status.HTTP_201_CREATED,
)
def assign_responsibility_scope(
    user_id: int,
    payload: ScopeAssignmentRequest,
    request_id: ServerRequestId,
    current_user: SystemAccessWriter,
    session: DatabaseSession,
) -> ScopeAssignmentResponse:
    """向目标用户授予责任范围，并在同一事务内记录不可变审计。"""

    user = session.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="目标用户不存在。")
    existing_scope = session.scalar(
        select(ResponsibilityScope).where(
            ResponsibilityScope.user_id == user.id,
            ResponsibilityScope.scope_type == payload.scope_type,
            ResponsibilityScope.scope_value == payload.scope_value,
        )
    )
    if existing_scope is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="该用户已拥有此责任范围。")

    scope = ResponsibilityScope(
        user_id=user.id,
        scope_type=payload.scope_type,
        scope_value=payload.scope_value,
        assigned_by_user_id=current_user.id,
    )
    try:
        session.add(scope)
        session.flush()
        append_audit_log(
            session=session,
            actor_user_id=current_user.id,
            event_type="authorization.scope.assigned",
            target_type="responsibility_scope",
            target_id=str(scope.id),
            before_summary={},
            after_summary={
                "user_id": user.id,
                "scope_type": scope.scope_type,
                "scope_value": scope.scope_value,
            },
            request_id=request_id,
            reason=payload.reason,
        )
        session.commit()
    except IntegrityError:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="该用户已拥有此责任范围。",
        ) from None
    except SQLAlchemyError:
        raise_database_write_error(session, "责任范围配置保存失败，请稍后重试。")

    return ScopeAssignmentResponse(
        id=scope.id,
        user_id=user.id,
        scope_type=payload.scope_type,
        scope_value=scope.scope_value,
        status="created",
    )


@router.post(
    "/roles/{role_code}/permissions",
    response_model=PermissionGrantResponse,
    status_code=status.HTTP_201_CREATED,
)
def grant_role_permission(
    role_code: str,
    payload: PermissionGrantRequest,
    request_id: ServerRequestId,
    current_user: SystemAccessWriter,
    session: DatabaseSession,
) -> PermissionGrantResponse:
    """向既有角色授予动作与范围类型许可，并在同一事务内记录审计。"""

    role = session.scalar(select(Role).where(Role.code == role_code))
    if role is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="目标角色不存在。")
    existing_permission = session.scalar(
        select(RolePermission).where(
            RolePermission.role_id == role.id,
            RolePermission.action_code == payload.action_code,
            RolePermission.scope_type == payload.scope_type,
        )
    )
    if existing_permission is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="该角色已拥有此操作权限。")

    permission = RolePermission(
        role_id=role.id,
        action_code=payload.action_code,
        scope_type=payload.scope_type,
    )
    try:
        session.add(permission)
        session.flush()
        append_audit_log(
            session=session,
            actor_user_id=current_user.id,
            event_type="authorization.permission.granted",
            target_type="role_permission",
            target_id=str(permission.id),
            before_summary={},
            after_summary={
                "role_code": role.code,
                "action_code": permission.action_code,
                "scope_type": permission.scope_type,
            },
            request_id=request_id,
            reason=payload.reason,
        )
        session.commit()
    except IntegrityError:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="该角色已拥有此操作权限。",
        ) from None
    except SQLAlchemyError:
        raise_database_write_error(session, "操作权限配置保存失败，请稍后重试。")

    return PermissionGrantResponse(
        id=permission.id,
        role_code=role.code,
        action_code=permission.action_code,
        scope_type=payload.scope_type,
        status="created",
    )
