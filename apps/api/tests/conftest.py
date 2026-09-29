from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

from app.core.settings import get_settings
from tests.master_data import conftest as postgres_fixtures

INTEGRATION_FLAG = "RUN_POSTGRES_INTEGRATION"

# The role/password authority remains session-scoped, but every current-schema
# test gets its own physical head database.  Revision20 intentionally rejects a
# second unbound school root, so sharing one database across independently seeded
# tests would make collection order a security-relevant input.
postgres_roles = pytest.fixture(scope="session")(postgres_fixtures.postgres_roles)
current_master_data_template = pytest.fixture(scope="session")(
    postgres_fixtures.current_master_data_template
)
isolated_migration_schema = pytest.fixture(postgres_fixtures.isolated_migration_schema)
isolated_revision19_schema = pytest.fixture(postgres_fixtures.isolated_revision19_schema)
current_master_data_schema = pytest.fixture(postgres_fixtures.current_master_data_schema)
school_facts_f1_template = pytest.fixture(scope="session")(
    postgres_fixtures.school_facts_f1_template
)
school_facts_f1_schema = pytest.fixture(postgres_fixtures.school_facts_f1_schema)
current_owner_connection = pytest.fixture(postgres_fixtures.current_owner_connection)
current_owner_session = pytest.fixture(postgres_fixtures.current_owner_session)

# 所有 pytest 进程只使用固定公开测试值；正式运行仍必须从部署 Secret 显式提供。
os.environ.setdefault(
    "RLS_CONTEXT_SIGNING_KEY",
    "pytest-only-runtime-context-signing-key-32-bytes",
)


def ensure_isolated_postgres_test_database(*, app_env: str, database_name: str) -> None:
    """拒绝集成测试连接普通开发或生产数据库，避免迁移和清表破坏数据。"""

    if app_env != "test" or not database_name.lower().endswith("_test"):
        raise RuntimeError(
            "PostgreSQL 集成测试拒绝使用非隔离数据库："
            "必须设置 APP_ENV=test，且数据库名必须以 _test 结尾。"
        )


@pytest.fixture(scope="session", autouse=True)
def protect_postgres_integration_database() -> Iterator[None]:
    """在测试会话启用 PostgreSQL 集成验证时先校验环境和数据库名称。"""

    integration_enabled = os.environ.get(INTEGRATION_FLAG) == "1"
    if integration_enabled:
        get_settings.cache_clear()
        settings = get_settings()
        ensure_isolated_postgres_test_database(
            app_env=settings.app_env,
            database_name=settings.postgres_db,
        )
    yield
