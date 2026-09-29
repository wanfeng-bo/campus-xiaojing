from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.settings import Settings, get_settings
from app.db.connection import get_engine
from app.platform.audit.service import append_audit_log
from app.platform.authorization.bootstrap import (
    SYSTEM_ADMIN_ACTION_CODE,
    SYSTEM_ADMIN_SCOPE_TYPE,
    ensure_system_admin_access,
    seed_initial_roles,
)
from app.platform.authorization.models import Role, UserRole
from app.platform.identity.models import User


class InitialAdminBootstrapError(Exception):
    """表示初始管理员绑定不满足受控部署前提，调用方必须整体回滚。"""


def bind_initial_administrators(
    session: Session,
    *,
    user_ids: Sequence[int],
    request_id: str,
) -> None:
    """原子准备两名初始管理员的角色、固定维护许可和审计；本函数不自行提交事务。"""

    normalized_user_ids = tuple(user_ids)
    if len(normalized_user_ids) != 2 or len(set(normalized_user_ids)) != 2:
        raise InitialAdminBootstrapError("初始管理员必须指定两个不同的用户 ID。")

    existing_system_admin = session.scalar(
        select(UserRole.id)
        .join(Role, Role.id == UserRole.role_id)
        .where(Role.code == "system_admin")
    )
    if existing_system_admin is not None:
        raise InitialAdminBootstrapError("系统已经完成初始管理员绑定，不能重复执行。")

    seed_initial_roles(session)
    system_admin_role = session.scalar(select(Role).where(Role.code == "system_admin"))
    if system_admin_role is None:
        raise InitialAdminBootstrapError("未找到系统管理员角色，无法完成初始化。")
    ensure_system_admin_access(session, system_admin_role)

    users = session.scalars(select(User).where(User.id.in_(normalized_user_ids))).all()
    if len(users) != 2 or {user.id for user in users} != set(normalized_user_ids):
        raise InitialAdminBootstrapError("指定用户不存在或尚未完成钉钉身份映射。")
    if any(not user.is_active for user in users):
        raise InitialAdminBootstrapError("初始管理员用户必须处于启用状态。")

    for user_id in normalized_user_ids:
        session.add(
            UserRole(
                user_id=user_id,
                role_id=system_admin_role.id,
                assigned_by_user_id=None,
            )
        )
    session.flush()
    append_audit_log(
        session=session,
        actor_user_id=None,
        event_type="identity.initial_admin.bound",
        target_type="system_admin_bootstrap",
        target_id="system_admin",
        before_summary={},
        after_summary={
            "user_ids": list(normalized_user_ids),
            "role_code": "system_admin",
            "action_code": SYSTEM_ADMIN_ACTION_CODE,
            "scope_type": SYSTEM_ADMIN_SCOPE_TYPE,
        },
        request_id=request_id,
        reason="测试企业初始管理员绑定",
    )


def build_argument_parser() -> argparse.ArgumentParser:
    """创建只接受两个本地用户 ID 的受控初始化命令参数解析器。"""

    parser = argparse.ArgumentParser(description="绑定两名钉钉测试企业初始系统管理员。")
    parser.add_argument(
        "--user-id",
        action="append",
        type=int,
        required=True,
        help="已完成钉钉首次映射的本地用户 ID；必须恰好提供两次。",
    )
    return parser


def validate_bootstrap_environment(settings: Settings) -> None:
    """限制命令只能在测试或生产钉钉环境执行，禁止开发环境直接绑定管理员。"""

    if settings.app_env not in {"test", "production"}:
        raise InitialAdminBootstrapError("初始管理员命令只能在 test 或 production 环境执行。")
    if settings.identity_provider != "dingtalk":
        raise InitialAdminBootstrapError("初始管理员命令只能使用钉钉身份提供者。")


def get_expected_migration_revision() -> str:
    """读取当前 Alembic 脚本头版本，避免在代码与数据库架构不一致时写入管理员数据。"""

    api_root = Path(__file__).resolve().parents[3]
    alembic_config = Config(str(api_root / "alembic.ini"))
    expected_revision = ScriptDirectory.from_config(alembic_config).get_current_head()
    if expected_revision is None:
        raise InitialAdminBootstrapError("未找到当前 Alembic 迁移版本，禁止绑定管理员。")
    return expected_revision


def validate_database_migration(session: Session) -> None:
    """要求数据库已迁移至当前 Alembic 头版本；不满足时在任何管理员写入前停止。"""

    expected_revision = get_expected_migration_revision()
    current_revision = session.scalar(text("SELECT version_num FROM alembic_version"))
    if current_revision != expected_revision:
        raise InitialAdminBootstrapError("数据库迁移未完成，请先执行 alembic upgrade head。")


def main(argv: Sequence[str] | None = None) -> int:
    """执行一次受控双管理员绑定；失败时回滚并返回非零退出码。"""

    parser = build_argument_parser()
    arguments = parser.parse_args(argv)
    user_ids = tuple(arguments.user_id)
    if len(user_ids) != 2 or len(set(user_ids)) != 2:
        parser.error("--user-id 必须恰好提供两个不同的用户 ID。")

    try:
        settings = get_settings()
        validate_bootstrap_environment(settings)
        with Session(get_engine()) as session:
            try:
                validate_database_migration(session)
                bind_initial_administrators(
                    session,
                    user_ids=user_ids,
                    request_id="initial-admin-bootstrap",
                )
                session.commit()
            except InitialAdminBootstrapError as error:
                session.rollback()
                print(f"初始管理员绑定失败：{error}", file=sys.stderr)
                return 1
            except SQLAlchemyError:
                session.rollback()
                print(
                    "初始管理员绑定失败：数据库连接或迁移状态异常，请检查数据库服务和迁移。",
                    file=sys.stderr,
                )
                return 1
    except InitialAdminBootstrapError as error:
        print(f"初始管理员绑定失败：{error}", file=sys.stderr)
        return 1

    print("初始管理员绑定完成，请核对 audit_logs 审计记录。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
