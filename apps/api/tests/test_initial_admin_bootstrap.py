from __future__ import annotations

from typing import cast

import pytest
from sqlalchemy.orm import Session

from app.platform.audit.models import AuditLog
from app.platform.authorization.models import Role, RolePermission, UserRole
from app.platform.identity.bootstrap_initial_admins import (
    InitialAdminBootstrapError,
    bind_initial_administrators,
)
from app.platform.identity.models import User


class ScalarRows:
    """模拟 SQLAlchemy 标量结果的最小集合接口。"""

    def __init__(self, values: list[User]) -> None:
        """保存预设的用户查询结果。"""

        self.values = values

    def all(self) -> list[User]:
        """返回全部预设用户。"""

        return self.values


class FakeBootstrapSession:
    """模拟初始管理员绑定所需的查询、写入和刷新行为，不连接真实数据库。"""

    def __init__(self, users: list[User], scalar_results: list[object | None]) -> None:
        """预置已映射用户及按调用顺序返回的单值查询结果。"""

        self.users = users
        self.scalar_results = scalar_results
        self.added_objects: list[object] = []

    def scalar(self, _statement: object) -> object | None:
        """按预设顺序返回角色、许可或既有绑定查询结果。"""

        return self.scalar_results.pop(0)

    def scalars(self, _statement: object) -> ScalarRows:
        """返回预置的已映射用户集合。"""

        return ScalarRows(self.users)

    def add(self, model: object) -> None:
        """记录待写入的权限、角色绑定或审计对象。"""

        self.added_objects.append(model)

    def flush(self) -> None:
        """模拟 ORM 写入刷新；本测试无需生成新增对象主键。"""


def build_user(user_id: int, is_active: bool = True) -> User:
    """构造已完成钉钉首次映射的测试用户。"""

    return User(
        id=user_id,
        external_subject=f"dingtalk:ding-test-corp:user-{user_id}",
        display_name=f"测试用户{user_id}",
        is_active=is_active,
    )


def test_bind_initial_admins_grants_exactly_two_and_audits(monkeypatch) -> None:
    """首次绑定只给两名指定用户系统管理员与系统维护权限，并追加单条部署审计。"""

    from app.platform.identity import bootstrap_initial_admins as bootstrap_module

    system_admin_role = Role(id=8, code="system_admin", name="系统管理员", is_system=True)
    session = FakeBootstrapSession(
        users=[build_user(101), build_user(102)],
        scalar_results=[None, system_admin_role, None],
    )
    monkeypatch.setattr(bootstrap_module, "seed_initial_roles", lambda _session: [])

    bind_initial_administrators(
        cast(Session, session),
        user_ids=(101, 102),
        request_id="bootstrap-request",
    )

    assigned_user_ids = {
        item.user_id for item in session.added_objects if isinstance(item, UserRole)
    }
    assert assigned_user_ids == {101, 102}
    system_permissions = [
        item for item in session.added_objects if isinstance(item, RolePermission)
    ]
    assert [(item.action_code, item.scope_type) for item in system_permissions] == [
        ("system.access.write", "all")
    ]
    audit_log = next(item for item in session.added_objects if isinstance(item, AuditLog))
    assert audit_log.event_type == "identity.initial_admin.bound"
    assert audit_log.actor_user_id is None
    assert audit_log.after_summary["user_ids"] == [101, 102]


def test_invalid_or_repeated_bootstrap_does_not_write_partial_data(monkeypatch) -> None:
    """重复用户或既有系统管理员必须被拒绝，且不留下新的权限、绑定或审计对象。"""

    from app.platform.identity import bootstrap_initial_admins as bootstrap_module

    duplicate_session = FakeBootstrapSession(users=[build_user(101)], scalar_results=[])
    repeated_session = FakeBootstrapSession(
        users=[build_user(101), build_user(102)],
        scalar_results=[object()],
    )
    monkeypatch.setattr(bootstrap_module, "seed_initial_roles", lambda _session: [])

    with pytest.raises(InitialAdminBootstrapError, match="两个不同"):
        bind_initial_administrators(
            cast(Session, duplicate_session),
            user_ids=(101, 101),
            request_id="bootstrap-request",
        )
    with pytest.raises(InitialAdminBootstrapError, match="已经完成"):
        bind_initial_administrators(
            cast(Session, repeated_session),
            user_ids=(101, 102),
            request_id="bootstrap-request",
        )

    assert duplicate_session.added_objects == []
    assert repeated_session.added_objects == []


def test_validate_database_migration_rejects_outdated_revision(monkeypatch) -> None:
    """数据库迁移版本未到当前版本时，管理员命令必须在写入前拒绝执行。"""

    from app.platform.identity import bootstrap_initial_admins as bootstrap_module

    session = FakeBootstrapSession(users=[], scalar_results=["20260728_01"])
    monkeypatch.setattr(
        bootstrap_module,
        "get_expected_migration_revision",
        lambda: "20260728_02",
    )

    with pytest.raises(InitialAdminBootstrapError, match="迁移"):
        bootstrap_module.validate_database_migration(cast(Session, session))

    assert session.added_objects == []


def test_expected_migration_revision_rejects_missing_script_head(monkeypatch) -> None:
    """迁移脚本没有当前版本时，管理员命令不能把空值当作已迁移。"""

    from app.platform.identity import bootstrap_initial_admins as bootstrap_module

    class EmptyScriptDirectory:
        """模拟没有可用迁移头版本的 Alembic 脚本目录。"""

        def get_current_head(self) -> None:
            """返回空迁移头，模拟错误的部署包。"""

            return None

    monkeypatch.setattr(
        bootstrap_module.ScriptDirectory,
        "from_config",
        lambda _config: EmptyScriptDirectory(),
    )

    with pytest.raises(InitialAdminBootstrapError, match="迁移"):
        bootstrap_module.get_expected_migration_revision()
