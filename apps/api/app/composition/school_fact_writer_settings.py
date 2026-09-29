# pyright: strict

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Literal, Self, cast
from uuid import UUID

from pydantic import Field, SecretStr, StringConstraints, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import URL, Engine, create_engine, event, text
from sqlalchemy.engine.interfaces import DBAPIConnection
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.pool import ConnectionPoolEntry, PoolProxiedConnection

from app.db.schema_contract import DatabaseSchemaContract, schema_contract_head

EXPECTED_SCHOOL_FACT_WRITER_ROLE = "campus_ai_school_fact_writer"
REQUIRED_SF5B_ALEMBIC_HEAD = "20260824_20"
SCHOOL_DATABASE_ANCHOR_FUNCTION = "app.assert_school_database_anchor"

_PRIVILEGED_WRITER_MEMBERSHIP_ROLES = (
    "campus_ai_db_owner",
    "campus_ai_migration",
    "campus_ai_runtime",
    "campus_ai_context_definer",
    "campus_ai_audit_definer",
    "campus_ai_outbox_probe_definer",
    "campus_ai_takeover_definer",
    "campus_ai_school_fact_reader",
    "campus_ai_identity_definer",
)
_WRITER_PRIVILEGED_ROLES_SQL = ", ".join(
    f"'{role_name}'" for role_name in _PRIVILEGED_WRITER_MEMBERSHIP_ROLES
)

_EXECUTION_CONTEXT_SETTING_NAMES = (
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
_EXECUTION_CONTEXT_IS_CLEAN_SQL = " AND ".join(
    f"COALESCE(current_setting('{name}', true), '') = ''"
    for name in _EXECUTION_CONTEXT_SETTING_NAMES
)

_ROLE_FACTS_SQL = text(
    f"""
    SELECT current_user AS current_user,
           session_user AS session_user,
           role.rolsuper AS is_superuser,
           role.rolbypassrls AS bypasses_rls,
           EXISTS (
               SELECT 1
               FROM pg_catalog.pg_roles AS granted_role
               WHERE granted_role.rolname IN ({_WRITER_PRIVILEGED_ROLES_SQL})
                 AND pg_catalog.pg_has_role(
                     current_user, granted_role.oid, 'MEMBER'
                 )
           ) AS has_privileged_membership
      FROM pg_catalog.pg_roles AS role
     WHERE role.rolname = current_user
    """
)
_ALEMBIC_HEAD_SQL = text("SELECT version_num FROM alembic_version")
_SCHOOL_ANCHOR_SQL = text(
    f"""
    SELECT {SCHOOL_DATABASE_ANCHOR_FUNCTION}(
        CAST(:anchor_id AS uuid),
        CAST(:school_id AS uuid),
        CAST(:expected_system_identifier AS text)
    ) AS school_id
    """
)

_CHECKOUT_CONNECTION_FACTS_SQL = f"""
    SELECT current_user::text AS current_user,
           session_user::text AS session_user,
           role.rolsuper AS is_superuser,
           role.rolbypassrls AS bypasses_rls,
           EXISTS (
               SELECT 1
               FROM pg_catalog.pg_roles AS granted_role
               WHERE granted_role.rolname IN ({_WRITER_PRIVILEGED_ROLES_SQL})
                 AND pg_catalog.pg_has_role(
                     current_user, granted_role.oid, 'MEMBER'
                 )
           ) AS has_privileged_membership,
           (
               SELECT CASE WHEN count(*) = 1 THEN min(version_num) END
                 FROM public.alembic_version
           ) AS alembic_head,
           app.assert_school_database_anchor(
               %s::uuid, %s::uuid, %s::text
           )::text AS anchored_school_id,
           ({_EXECUTION_CONTEXT_IS_CLEAN_SQL}) AS execution_context_is_clean
      FROM pg_catalog.pg_roles AS role
     WHERE role.rolname = current_user
"""


def _secret_bytes(value: SecretStr, *, label: str) -> bytes:
    encoded = value.get_secret_value().encode("utf-8")
    if len(encoded) < 32:
        raise ValueError(f"{label}至少需要 32 字节。")
    return encoded


class SchoolFactWriterSettings(BaseSettings):
    """Writer 进程独占配置；不接受 migration/owner 凭据。"""

    model_config = SettingsConfigDict(
        env_prefix="SCHOOL_FACT_WRITER_",
        env_file=None,
        extra="ignore",
        frozen=True,
        hide_input_in_errors=True,
    )

    schema_contract: DatabaseSchemaContract = "identity-v20"

    database_host: Annotated[
        str,
        StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=253),
    ]
    database_port: int = Field(default=5432, ge=1, le=65_535)
    database_name: Annotated[
        str,
        StringConstraints(strict=True, min_length=1, max_length=63, pattern=r"^[A-Za-z0-9_-]+$"),
    ]
    database_user: Literal["campus_ai_school_fact_writer"] = EXPECTED_SCHOOL_FACT_WRITER_ROLE
    database_password: SecretStr
    expected_anchor_id: UUID
    expected_school_id: UUID
    expected_system_identifier: Annotated[
        str,
        StringConstraints(strict=True, strip_whitespace=True, min_length=8, max_length=128),
    ]
    confirmation_hmac_key: SecretStr
    caller_auth_key: SecretStr
    context_transport_hmac_key: SecretStr
    rls_context_signing_key: SecretStr
    pool_size: int = Field(default=4, ge=1, le=16)
    max_overflow: int = Field(default=0, ge=0, le=8)
    pool_timeout_seconds: float = Field(default=3, ge=0.1, le=10)
    pool_recycle_seconds: int = Field(default=600, ge=30, le=1800)
    connect_timeout_seconds: int = Field(default=3, ge=1, le=10)
    statement_timeout_ms: int = Field(default=30_000, ge=1_000, le=60_000)
    lock_timeout_ms: int = Field(default=3_000, ge=100, le=10_000)

    @field_validator(
        "database_password",
        "confirmation_hmac_key",
        "caller_auth_key",
        "context_transport_hmac_key",
        "rls_context_signing_key",
    )
    @classmethod
    def require_nontrivial_secret(cls, value: SecretStr, info: object) -> SecretStr:
        field_name = getattr(info, "field_name", "Writer secret")
        _secret_bytes(value, label=str(field_name))
        return value

    @model_validator(mode="after")
    def require_distinct_secret_purposes(self) -> Self:
        purpose_secrets = (
            self.database_password,
            self.confirmation_hmac_key,
            self.caller_auth_key,
            self.context_transport_hmac_key,
            self.rls_context_signing_key,
        )
        if len({secret.get_secret_value() for secret in purpose_secrets}) != len(purpose_secrets):
            raise ValueError("Writer secrets must be distinct by purpose.")
        return self

    @property
    def database_url(self) -> URL:
        return URL.create(
            drivername="postgresql+psycopg",
            username=self.database_user,
            password=self.database_password.get_secret_value(),
            host=self.database_host,
            port=self.database_port,
            database=self.database_name,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class SchoolFactWriterConnectionFacts:
    current_user: str
    session_user: str
    is_superuser: bool
    bypasses_rls: bool
    alembic_head: str
    anchored_school_id: UUID
    has_privileged_membership: bool = False


class WriterStartupRejected(RuntimeError):
    """表示 Writer 数据库身份、版本或学校锚点不满足固定启动门。"""


def require_school_fact_writer_connection(
    facts: SchoolFactWriterConnectionFacts,
    *,
    settings: SchoolFactWriterSettings,
) -> None:
    """对已读取的连接事实执行单一、失败关闭的启动判定。"""

    if (
        facts.current_user != EXPECTED_SCHOOL_FACT_WRITER_ROLE
        or facts.session_user != EXPECTED_SCHOOL_FACT_WRITER_ROLE
        or facts.is_superuser
        or facts.bypasses_rls
        or facts.has_privileged_membership
        or facts.alembic_head != schema_contract_head(settings.schema_contract)
        or facts.anchored_school_id != settings.expected_school_id
    ):
        raise WriterStartupRejected("School Facts Writer 数据库启动门拒绝连接。")


def _read_checkout_connection_facts(
    dbapi_connection: DBAPIConnection,
    *,
    settings: SchoolFactWriterSettings,
) -> SchoolFactWriterConnectionFacts:
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute(
            _CHECKOUT_CONNECTION_FACTS_SQL,
            (
                str(settings.expected_anchor_id),
                str(settings.expected_school_id),
                settings.expected_system_identifier,
            ),
        )
        row = cast(tuple[object, ...] | None, cursor.fetchone())
    finally:
        cursor.close()
    if row is None or len(row) != 8:
        raise WriterStartupRejected("Writer checkout facts are incomplete.")
    if row[7] is not True:
        raise WriterStartupRejected("Writer checkout context is not clean.")
    return SchoolFactWriterConnectionFacts(
        current_user=str(row[0]),
        session_user=str(row[1]),
        is_superuser=bool(row[2]),
        bypasses_rls=bool(row[3]),
        has_privileged_membership=bool(row[4]),
        alembic_head=str(row[5]),
        anchored_school_id=UUID(str(row[6])),
    )


def _invalidate_rejected_checkout(
    dbapi_connection: DBAPIConnection,
    connection_record: ConnectionPoolEntry,
    *,
    rejection: WriterStartupRejected,
) -> None:
    try:
        dbapi_connection.rollback()
    except Exception:
        pass
    connection_record.invalidate(rejection, soft=False)


def _validate_school_fact_writer_checkout(
    dbapi_connection: DBAPIConnection,
    connection_record: ConnectionPoolEntry,
    _connection_proxy: PoolProxiedConnection,
    *,
    settings: SchoolFactWriterSettings,
) -> None:
    try:
        facts = _read_checkout_connection_facts(dbapi_connection, settings=settings)
        require_school_fact_writer_connection(facts, settings=settings)
        dbapi_connection.rollback()
    except WriterStartupRejected as exc:
        _invalidate_rejected_checkout(
            dbapi_connection,
            connection_record,
            rejection=exc,
        )
        raise
    except Exception as exc:
        rejection = WriterStartupRejected("Unable to validate School Facts Writer checkout.")
        _invalidate_rejected_checkout(
            dbapi_connection,
            connection_record,
            rejection=rejection,
        )
        raise rejection from exc


def build_school_fact_writer_engine(settings: SchoolFactWriterSettings) -> Engine:
    """建立独立、短预算、回滚复位且带失活探测的 Writer 连接池。"""

    options = " ".join(
        (
            f"-c statement_timeout={settings.statement_timeout_ms}",
            f"-c lock_timeout={settings.lock_timeout_ms}",
            f"-c idle_in_transaction_session_timeout={settings.statement_timeout_ms}",
        )
    )
    engine = create_engine(
        settings.database_url,
        pool_pre_ping=True,
        pool_reset_on_return="rollback",
        pool_recycle=settings.pool_recycle_seconds,
        pool_size=settings.pool_size,
        max_overflow=settings.max_overflow,
        pool_timeout=settings.pool_timeout_seconds,
        pool_use_lifo=True,
        connect_args={
            "connect_timeout": settings.connect_timeout_seconds,
            "application_name": "campus-ai-school-fact-writer",
            "options": options,
        },
        hide_parameters=True,
    )

    def validate_checkout(
        dbapi_connection: DBAPIConnection,
        connection_record: ConnectionPoolEntry,
        connection_proxy: PoolProxiedConnection,
    ) -> None:
        _validate_school_fact_writer_checkout(
            dbapi_connection,
            connection_record,
            connection_proxy,
            settings=settings,
        )

    event.listen(
        engine.pool,
        "checkout",
        validate_checkout,
    )
    return engine


def verify_school_fact_writer_database(
    engine: Engine,
    *,
    settings: SchoolFactWriterSettings,
) -> SchoolFactWriterConnectionFacts:
    """从真实连接读取 role/head/anchor；任一查询或形状异常都失败关闭。"""

    try:
        with engine.connect() as connection:
            role = connection.execute(_ROLE_FACTS_SQL).mappings().one()
            heads = tuple(connection.execute(_ALEMBIC_HEAD_SQL).scalars())
            if len(heads) != 1:
                raise WriterStartupRejected("数据库迁移头不是唯一值。")
            anchored_school_id = connection.execute(
                _SCHOOL_ANCHOR_SQL,
                {
                    "anchor_id": settings.expected_anchor_id,
                    "school_id": settings.expected_school_id,
                    "expected_system_identifier": settings.expected_system_identifier,
                },
            ).scalar_one()
            facts = SchoolFactWriterConnectionFacts(
                current_user=str(role["current_user"]),
                session_user=str(role["session_user"]),
                is_superuser=bool(role["is_superuser"]),
                bypasses_rls=bool(role["bypasses_rls"]),
                has_privileged_membership=bool(role["has_privileged_membership"]),
                alembic_head=str(heads[0]),
                anchored_school_id=UUID(str(anchored_school_id)),
            )
            require_school_fact_writer_connection(facts, settings=settings)
            return facts
    except WriterStartupRejected:
        raise
    except (SQLAlchemyError, LookupError, TypeError, ValueError) as exc:
        raise WriterStartupRejected("无法验证 School Facts Writer 数据库启动门。") from exc
