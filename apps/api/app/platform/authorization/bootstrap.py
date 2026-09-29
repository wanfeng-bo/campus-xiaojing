from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.platform.authorization.models import Role, RolePermission

INITIAL_ROLE_DEFINITIONS = (
    ("teacher", "教师"),
    ("handler", "责任处理人"),
    ("inspector", "值周/检查人员"),
    ("leader", "学校领导"),
    ("system_admin", "系统管理员"),
)
INITIAL_ROLE_CODES = tuple(role_code for role_code, _ in INITIAL_ROLE_DEFINITIONS)
SYSTEM_ADMIN_ACTION_CODE = "system.access.write"
SYSTEM_ADMIN_SCOPE_TYPE = "all"


def seed_initial_roles(session: Session) -> list[str]:
    """补齐五类通用预置角色，保留学校后续配置自定义权限的空间。"""

    existing_codes = set(session.scalars(select(Role.code)).all())
    created_codes: list[str] = []
    for role_code, role_name in INITIAL_ROLE_DEFINITIONS:
        if role_code in existing_codes:
            continue
        session.add(Role(code=role_code, name=role_name, is_system=True))
        created_codes.append(role_code)

    if created_codes:
        session.flush()
    return created_codes


def ensure_system_admin_access(session: Session, role: Role) -> bool:
    """确保系统管理员具备维护权限；只补齐固定系统许可，不授予任何业务模块权限。"""

    existing_permission = session.scalar(
        select(RolePermission).where(
            RolePermission.role_id == role.id,
            RolePermission.action_code == SYSTEM_ADMIN_ACTION_CODE,
            RolePermission.scope_type == SYSTEM_ADMIN_SCOPE_TYPE,
        )
    )
    if existing_permission is not None:
        return False
    session.add(
        RolePermission(
            role_id=role.id,
            action_code=SYSTEM_ADMIN_ACTION_CODE,
            scope_type=SYSTEM_ADMIN_SCOPE_TYPE,
        )
    )
    session.flush()
    return True
