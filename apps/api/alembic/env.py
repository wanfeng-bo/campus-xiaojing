from __future__ import annotations

from sqlalchemy import create_engine, pool, text

from alembic import context
from app.db.base import Base
from app.db.migration_settings import get_migration_settings
from app.domains.campus import models as campus_models  # noqa: F401
from app.domains.repairs import models as repair_models  # noqa: F401
from app.domains.students import education_models as education_models  # noqa: F401
from app.domains.students import models as student_models  # noqa: F401
from app.platform.audit import models as audit_models  # noqa: F401
from app.platform.authorization import models as authorization_models  # noqa: F401
from app.platform.calendar import models as calendar_models  # noqa: F401
from app.platform.directory import models as directory_models  # noqa: F401
from app.platform.governance import models as governance_models  # noqa: F401
from app.platform.idempotency import models as idempotency_models  # noqa: F401
from app.platform.identity import models as identity_models  # noqa: F401
from app.platform.master_data_imports import models as master_data_import_models  # noqa: F401
from app.platform.messaging import models as messaging_models  # noqa: F401
from app.platform.organization import models as organization_models  # noqa: F401
from app.platform.policies import models as policy_models  # noqa: F401
from app.platform.responsibility import models as responsibility_models  # noqa: F401
from app.platform.school_facts import models as school_fact_models  # noqa: F401
from app.platform.tenancy import models as tenancy_models  # noqa: F401

config = context.config
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """生成不连接数据库的迁移 SQL，且始终隐藏连接字符串中的密码。"""

    migration_settings = get_migration_settings()
    context.configure(
        url=migration_settings.database_url.render_as_string(hide_password=True),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """使用独立的无连接池引擎执行迁移，避免复用应用运行时连接池。"""

    migration_settings = get_migration_settings()
    connectable = create_engine(
        migration_settings.database_url,
        poolclass=pool.NullPool,
        hide_parameters=True,
    )

    try:
        with connectable.connect() as connection:
            context.configure(connection=connection, target_metadata=target_metadata)

            with context.begin_transaction():
                connection.execute(text("SET LOCAL ROLE campus_ai_db_owner"))
                connection.execute(
                    text(
                        "SELECT set_config('app.context_signing_key', :context_signing_key, true)"
                    ),
                    {
                        "context_signing_key": (
                            migration_settings.context_signing_key.get_secret_value()
                        )
                    },
                )
                context.run_migrations()
    finally:
        connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
