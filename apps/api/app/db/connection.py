from __future__ import annotations

from collections.abc import Generator
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, cast
from uuid import UUID

from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.engine.interfaces import DBAPIConnection
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import ConnectionPoolEntry, PoolProxiedConnection

from app.core.settings import Settings, get_settings
from app.db.schema_contract import schema_contract_head

EXPECTED_RUNTIME_DATABASE_ROLE = "campus_ai_runtime"
REQUIRED_RUNTIME_ALEMBIC_HEAD = "20260824_20"
_PRIVILEGED_RUNTIME_MEMBERSHIP_ROLES = (
    "campus_ai_db_owner",
    "campus_ai_migration",
    "campus_ai_context_definer",
    "campus_ai_audit_definer",
    "campus_ai_outbox_probe_definer",
    "campus_ai_takeover_definer",
    "campus_ai_school_fact_writer",
    "campus_ai_school_fact_reader",
    "campus_ai_identity_definer",
)
_RUNTIME_CONTEXT_SETTING_NAMES = (
    "app.context_key_version",
    "app.school_id",
    "app.account_user_id",
    "app.person_id",
    "app.context_kind",
    "app.assignment_id",
    "app.assignment_version",
    "app.guardian_relationship_id",
    "app.guardian_relationship_version",
    "app.authorization_version",
    "app.request_id",
    "app.context_purpose",
    "app.idempotency_key_hash",
    "app.session_nonce_hash",
    "app.proof_expires_at",
    "app.server_proof",
    "app.binding_database",
    "app.binding_backend_pid",
    "app.binding_xact_id",
    "app.binding_proof",
)
_RUNTIME_CONTEXT_IS_CLEAN_SQL = " AND ".join(
    f"COALESCE(current_setting('{name}', true), '') = ''" for name in _RUNTIME_CONTEXT_SETTING_NAMES
)
_RUNTIME_PRIVILEGED_ROLES_SQL = ", ".join(
    f"'{role_name}'" for role_name in _PRIVILEGED_RUNTIME_MEMBERSHIP_ROLES
)
_RUNTIME_CHECKOUT_FACTS_SQL = f"""
SELECT current_user::text,
       session_user::text,
       role.rolsuper,
       role.rolbypassrls,
       EXISTS (
           SELECT 1
             FROM pg_catalog.pg_roles AS granted_role
            WHERE granted_role.rolname IN ({_RUNTIME_PRIVILEGED_ROLES_SQL})
              AND pg_catalog.pg_has_role(current_user, granted_role.oid, 'MEMBER')
       ) AS has_privileged_membership,
       (
           SELECT CASE WHEN count(*) = 1 THEN min(version_num) END
             FROM public.alembic_version
       ) AS alembic_head,
       CASE
           WHEN %s::text IS NULL THEN app.require_school_database_anchor()
           ELSE app.assert_school_database_anchor(%s::uuid, %s::uuid, %s::text)
       END::text AS anchored_school_id,
       ({_RUNTIME_CONTEXT_IS_CLEAN_SQL}) AS execution_context_is_clean
  FROM pg_catalog.pg_roles AS role
 WHERE role.rolname = current_user
"""


@dataclass(frozen=True, slots=True, kw_only=True)
class RuntimeDatabaseConnectionFacts:
    current_user: str
    session_user: str
    is_superuser: bool
    bypasses_rls: bool
    has_privileged_membership: bool
    alembic_head: str
    anchored_school_id: UUID
    execution_context_is_clean: bool


class RuntimeDatabaseStartupRejected(RuntimeError):
    """Runtime role, migration head, anchor, or pooled state failed closed."""


def require_runtime_database_connection(
    facts: RuntimeDatabaseConnectionFacts,
    *,
    settings: Settings,
) -> None:
    expected_school_id = settings.postgres_expected_school_id
    if (
        settings.postgres_user != EXPECTED_RUNTIME_DATABASE_ROLE
        or facts.current_user != EXPECTED_RUNTIME_DATABASE_ROLE
        or facts.session_user != EXPECTED_RUNTIME_DATABASE_ROLE
        or facts.is_superuser
        or facts.bypasses_rls
        or facts.has_privileged_membership
        or facts.alembic_head != schema_contract_head(settings.postgres_schema_contract)
        or not facts.execution_context_is_clean
        or (expected_school_id is not None and facts.anchored_school_id != expected_school_id)
    ):
        raise RuntimeDatabaseStartupRejected("Runtime database boundary rejected the connection.")


def _read_runtime_checkout_facts(
    dbapi_connection: DBAPIConnection,
    *,
    settings: Settings,
) -> RuntimeDatabaseConnectionFacts:
    expected_anchor_id = settings.postgres_expected_anchor_id
    expected_school_id = settings.postgres_expected_school_id
    expected_system_identifier = settings.postgres_expected_system_identifier
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute(
            _RUNTIME_CHECKOUT_FACTS_SQL,
            (
                str(expected_anchor_id) if expected_anchor_id is not None else None,
                str(expected_anchor_id) if expected_anchor_id is not None else None,
                str(expected_school_id) if expected_school_id is not None else None,
                expected_system_identifier,
            ),
        )
        row = cast(tuple[object, ...] | None, cursor.fetchone())
    finally:
        cursor.close()
    if row is None or len(row) != 8 or row[7] is not True:
        raise RuntimeDatabaseStartupRejected("Runtime checkout facts are incomplete or dirty.")
    return RuntimeDatabaseConnectionFacts(
        current_user=str(row[0]),
        session_user=str(row[1]),
        is_superuser=bool(row[2]),
        bypasses_rls=bool(row[3]),
        has_privileged_membership=bool(row[4]),
        alembic_head=str(row[5]),
        anchored_school_id=UUID(str(row[6])),
        execution_context_is_clean=True,
    )


def _invalidate_runtime_checkout(
    dbapi_connection: DBAPIConnection,
    connection_record: ConnectionPoolEntry,
    rejection: RuntimeDatabaseStartupRejected,
) -> None:
    try:
        dbapi_connection.rollback()
    except Exception:
        pass
    connection_record.invalidate(rejection, soft=False)


def _runtime_checkout_listener(
    settings: Settings,
) -> Any:
    def validate_checkout(
        dbapi_connection: DBAPIConnection,
        connection_record: ConnectionPoolEntry,
        _connection_proxy: PoolProxiedConnection,
    ) -> None:
        try:
            facts = _read_runtime_checkout_facts(dbapi_connection, settings=settings)
            require_runtime_database_connection(facts, settings=settings)
            dbapi_connection.rollback()
        except RuntimeDatabaseStartupRejected as exc:
            _invalidate_runtime_checkout(dbapi_connection, connection_record, exc)
            raise
        except Exception as exc:
            rejection = RuntimeDatabaseStartupRejected(
                "Unable to verify a checked-out runtime database connection."
            )
            _invalidate_runtime_checkout(dbapi_connection, connection_record, rejection)
            raise rejection from exc

    return validate_checkout


def build_engine(settings: Settings) -> Engine:
    """按受控配置创建连接引擎，并在复用连接前检查其仍然有效。"""

    engine = create_engine(
        settings.database_url,
        pool_pre_ping=True,
        pool_reset_on_return="rollback",
        pool_recycle=settings.postgres_pool_recycle_seconds,
        pool_size=settings.postgres_pool_size,
        max_overflow=settings.postgres_max_overflow,
        pool_timeout=settings.postgres_pool_timeout_seconds,
    )
    event.listen(engine.pool, "checkout", _runtime_checkout_listener(settings))
    return engine


def build_session_factory(engine: Engine) -> sessionmaker[Session]:
    """创建绑定指定引擎的会话工厂，禁止隐式自动提交事务。"""

    return sessionmaker(bind=engine, autoflush=False, autocommit=False)


def probe_database(engine: Engine) -> None:
    """以无副作用的只读查询验证数据库连接，不读取或修改业务数据。"""

    with engine.connect() as connection:
        connection.execute(text("SELECT 1"))


@lru_cache
def get_engine() -> Engine:
    """返回进程内复用的数据库引擎；启动门负责首次真实连接和密钥核验。"""

    return build_engine(get_settings())


@lru_cache
def get_session_factory() -> sessionmaker[Session]:
    """返回进程内复用的会话工厂，供请求依赖创建独立事务会话。"""

    return build_session_factory(get_engine())


def get_session() -> Generator[Session]:
    """为单个 API 请求提供会话，并在请求结束后可靠关闭连接资源。"""

    session = get_session_factory()()
    try:
        yield session
    finally:
        session.close()


def check_database_connection() -> None:
    """执行数据库探测和运行密钥指纹核验，缺任一条件都失败关闭。"""

    settings = get_settings()
    engine = get_engine()
    probe_database(engine)
    verify_runtime_context_signing_key(engine, settings)


def verify_runtime_context_signing_key(engine: Engine, settings: Settings) -> None:
    """启动连接池时核对应用和数据库密钥指纹，不读取数据库私有密钥。"""

    from app.platform.identity.context_proof import ContextProofSigner

    with Session(bind=engine) as session, session.begin():
        ContextProofSigner.from_settings(settings).verify_database_fingerprint(session)
