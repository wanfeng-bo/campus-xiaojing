# pyright: strict

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any, Literal, Self, cast
from uuid import UUID

from pydantic import Field, SecretStr, StringConstraints, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import URL, Engine, create_engine, event, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.db.schema_contract import DatabaseSchemaContract, schema_contract_head
from app.platform.authorization.secure_reader_adapters import (
    SqlAlchemyRepeatableReadExecutor,
    ThinAuthorizationReadAdapter,
)
from app.platform.authorization.secure_reader_compiler import ServerOwnedProjectionCompiler
from app.platform.authorization.secure_reader_registry import production_operation_registry
from app.platform.authorization.secure_reader_service import (
    SecureCursorCodec,
    SecureReader,
)

EXPECTED_SCHOOL_FACT_READER_ROLE = "campus_ai_school_fact_reader"
REQUIRED_SF5B_ALEMBIC_HEAD = "20260824_20"
SCHOOL_DATABASE_ANCHOR_FUNCTION = "app.assert_school_database_anchor"

_PRIVILEGED_READER_MEMBERSHIP_ROLES = (
    "campus_ai_db_owner",
    "campus_ai_migration",
    "campus_ai_runtime",
    "campus_ai_context_definer",
    "campus_ai_audit_definer",
    "campus_ai_outbox_probe_definer",
    "campus_ai_takeover_definer",
    "campus_ai_school_fact_writer",
    "campus_ai_identity_definer",
)
_READER_PRIVILEGED_ROLES_SQL = ", ".join(
    f"'{role_name}'" for role_name in _PRIVILEGED_READER_MEMBERSHIP_ROLES
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
               WHERE granted_role.rolname IN ({_READER_PRIVILEGED_ROLES_SQL})
                 AND pg_catalog.pg_has_role(
                     current_user, granted_role.oid, 'MEMBER'
                 )
           ) AS has_privileged_membership,
           current_setting('default_transaction_read_only') = 'on' AS is_read_only
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

_CHECKOUT_ROLE_FACTS_SQL = f"""
SELECT current_user,
       session_user,
       role.rolsuper,
       role.rolbypassrls,
       EXISTS (
           SELECT 1
           FROM pg_catalog.pg_roles AS granted_role
           WHERE granted_role.rolname IN ({_READER_PRIVILEGED_ROLES_SQL})
             AND pg_catalog.pg_has_role(current_user, granted_role.oid, 'MEMBER')
       ),
       current_setting('default_transaction_read_only') = 'on'
  FROM pg_catalog.pg_roles AS role
 WHERE role.rolname = current_user
"""
# Read current gate facts in one round trip, never reuse startup results. CASE
# preserves the original head-uniqueness rejection before the anchor function:
# aggregate head metadata always has one row even for zero/multiple heads.
_CHECKOUT_FACTS_SQL = f"""
WITH role_facts AS MATERIALIZED (
    {_CHECKOUT_ROLE_FACTS_SQL}
), schema_head AS MATERIALIZED (
    SELECT count(*) AS head_count, min(version_num) AS alembic_head
    FROM alembic_version
)
SELECT role_facts.*,
       schema_head.head_count,
       schema_head.alembic_head,
       CASE WHEN schema_head.head_count = 1 THEN
           {SCHOOL_DATABASE_ANCHOR_FUNCTION}(
               CAST(%s AS uuid), CAST(%s AS uuid), CAST(%s AS text)
           )
       ELSE NULL END AS anchored_school_id
FROM role_facts CROSS JOIN schema_head
"""


class SchoolFactReaderSettings(BaseSettings):
    """Reader process configuration with its own DSN and cursor-signing secret."""

    model_config = SettingsConfigDict(
        env_prefix="SCHOOL_FACT_READER_",
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
    database_user: Literal["campus_ai_school_fact_reader"] = EXPECTED_SCHOOL_FACT_READER_ROLE
    database_password: SecretStr
    expected_anchor_id: UUID
    expected_school_id: UUID
    expected_system_identifier: Annotated[
        str,
        StringConstraints(strict=True, strip_whitespace=True, min_length=8, max_length=128),
    ]
    cursor_hmac_key: SecretStr
    cursor_key_version: int = Field(default=1, ge=1, le=2_147_483_647)
    cursor_ttl_seconds: int = Field(default=120, ge=30, le=3_600)
    pool_size: int = Field(default=4, ge=1, le=16)
    max_overflow: int = Field(default=0, ge=0, le=8)
    pool_timeout_seconds: float = Field(default=3, ge=0.1, le=10)
    pool_recycle_seconds: int = Field(default=600, ge=30, le=1_800)
    connect_timeout_seconds: int = Field(default=3, ge=1, le=10)
    statement_timeout_ms: int = Field(default=10_000, ge=100, le=30_000)

    @field_validator("database_password", "cursor_hmac_key")
    @classmethod
    def require_independent_secret(cls, value: SecretStr, info: object) -> SecretStr:
        if len(value.get_secret_value().encode("utf-8")) < 32:
            field_name = str(getattr(info, "field_name", "Reader secret"))
            raise ValueError(f"{field_name}至少需要 32 字节。")
        return value

    @model_validator(mode="after")
    def require_distinct_secret_purposes(self) -> Self:
        if self.database_password.get_secret_value() == (self.cursor_hmac_key.get_secret_value()):
            raise ValueError("Reader database and cursor secrets must be distinct.")
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
class SchoolFactReaderConnectionFacts:
    current_user: str
    session_user: str
    is_superuser: bool
    bypasses_rls: bool
    default_transaction_read_only: bool
    alembic_head: str
    anchored_school_id: UUID
    has_privileged_membership: bool = False


class ReaderStartupRejected(RuntimeError):
    """The Reader role, migration head, or school anchor failed closed."""


def require_school_fact_reader_connection(
    facts: SchoolFactReaderConnectionFacts,
    *,
    settings: SchoolFactReaderSettings,
) -> None:
    if (
        facts.current_user != EXPECTED_SCHOOL_FACT_READER_ROLE
        or facts.session_user != EXPECTED_SCHOOL_FACT_READER_ROLE
        or facts.is_superuser
        or facts.bypasses_rls
        or facts.has_privileged_membership
        or not facts.default_transaction_read_only
        or facts.alembic_head != schema_contract_head(settings.schema_contract)
        or facts.anchored_school_id != settings.expected_school_id
    ):
        raise ReaderStartupRejected("School Facts Reader database startup gate rejected.")


def build_school_fact_reader_engine(settings: SchoolFactReaderSettings) -> Engine:
    """Build the Reader-only bounded pool; no runtime or Writer engine is reused."""

    options = " ".join(
        (
            "-c default_transaction_read_only=on",
            f"-c statement_timeout={settings.statement_timeout_ms}",
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
            "application_name": "campus-ai-school-fact-reader",
            "options": options,
        },
        hide_parameters=True,
    )
    event.listen(engine, "checkout", _reader_checkout_listener(settings), insert=True)
    return engine


def _reader_checkout_listener(
    settings: SchoolFactReaderSettings,
) -> Callable[[object, object, object], None]:
    def _validate_checkout(
        dbapi_connection: object,
        connection_record: object,
        _connection_proxy: object,
    ) -> None:
        try:
            facts = _read_school_fact_reader_checkout(dbapi_connection, settings=settings)
            require_school_fact_reader_connection(facts, settings=settings)
        except ReaderStartupRejected as error:
            _hard_invalidate_checkout(connection_record, error)
            raise
        except Exception as error:
            rejected = ReaderStartupRejected(
                "Unable to verify a checked-out School Facts Reader connection."
            )
            _hard_invalidate_checkout(connection_record, rejected)
            raise rejected from error

    return _validate_checkout


def _read_school_fact_reader_checkout(
    dbapi_connection: object,
    *,
    settings: SchoolFactReaderSettings,
) -> SchoolFactReaderConnectionFacts:
    connection = cast(Any, dbapi_connection)
    cursor: Any = None
    try:
        cursor = connection.cursor()
        cursor.execute(
            _CHECKOUT_FACTS_SQL,
            (
                settings.expected_anchor_id,
                settings.expected_school_id,
                settings.expected_system_identifier,
            ),
        )
        row = cursor.fetchone()
        if row is None or len(row) != 9:
            raise LookupError("Reader checkout facts are incomplete.")
        if row[6] != 1:
            raise ReaderStartupRejected("Database migration head is not unique.")

        return SchoolFactReaderConnectionFacts(
            current_user=str(row[0]),
            session_user=str(row[1]),
            is_superuser=bool(row[2]),
            bypasses_rls=bool(row[3]),
            has_privileged_membership=bool(row[4]),
            default_transaction_read_only=bool(row[5]),
            alembic_head=str(row[7]),
            anchored_school_id=UUID(str(row[8])),
        )
    finally:
        try:
            if cursor is not None:
                cursor.close()
        finally:
            connection.rollback()


def _hard_invalidate_checkout(connection_record: object, error: BaseException) -> None:
    try:
        cast(Any, connection_record).invalidate(error, soft=False)
    except Exception:
        pass


def verify_school_fact_reader_database(
    engine: Engine,
    *,
    settings: SchoolFactReaderSettings,
) -> SchoolFactReaderConnectionFacts:
    """Read and validate role/head/anchor from one real Reader connection."""

    try:
        with engine.connect() as connection:
            role = connection.execute(_ROLE_FACTS_SQL).mappings().one()
            heads = tuple(connection.execute(_ALEMBIC_HEAD_SQL).scalars())
            if len(heads) != 1:
                raise ReaderStartupRejected("Database migration head is not unique.")
            anchored_school_id = connection.execute(
                _SCHOOL_ANCHOR_SQL,
                {
                    "anchor_id": settings.expected_anchor_id,
                    "school_id": settings.expected_school_id,
                    "expected_system_identifier": settings.expected_system_identifier,
                },
            ).scalar_one()
            facts = SchoolFactReaderConnectionFacts(
                current_user=str(role["current_user"]),
                session_user=str(role["session_user"]),
                is_superuser=bool(role["is_superuser"]),
                bypasses_rls=bool(role["bypasses_rls"]),
                has_privileged_membership=bool(role["has_privileged_membership"]),
                default_transaction_read_only=bool(role["is_read_only"]),
                alembic_head=str(heads[0]),
                anchored_school_id=UUID(str(anchored_school_id)),
            )
            require_school_fact_reader_connection(facts, settings=settings)
            return facts
    except ReaderStartupRejected:
        raise
    except (SQLAlchemyError, LookupError, TypeError, ValueError) as exc:
        raise ReaderStartupRejected(
            "Unable to verify the School Facts Reader database startup gate."
        ) from exc


@dataclass(frozen=True, slots=True)
class ProductionSecureReaderRuntime:
    engine: Engine
    reader: SecureReader


def build_production_secure_reader(
    *,
    settings: SchoolFactReaderSettings,
    thin_service: object,
    clock: Callable[[], datetime],
) -> ProductionSecureReaderRuntime:
    """Compose a validated Reader with an empty production registry and no route."""

    engine = build_school_fact_reader_engine(settings)
    try:
        verify_school_fact_reader_database(engine, settings=settings)
        factory = sessionmaker[Session](
            bind=engine,
            autoflush=False,
            autocommit=False,
            expire_on_commit=False,
        )
        reader = SecureReader(
            registry=production_operation_registry(),
            compiler=ServerOwnedProjectionCompiler(),
            authorizer=ThinAuthorizationReadAdapter(thin_service=thin_service),
            executor=SqlAlchemyRepeatableReadExecutor(factory),
            cursor_codec=SecureCursorCodec(
                settings.cursor_hmac_key.get_secret_value().encode("utf-8"),
                key_version=settings.cursor_key_version,
                ttl_seconds=settings.cursor_ttl_seconds,
            ),
            clock=clock,
        )
        return ProductionSecureReaderRuntime(engine=engine, reader=reader)
    except BaseException:
        engine.dispose()
        raise


__all__ = [
    "EXPECTED_SCHOOL_FACT_READER_ROLE",
    "ProductionSecureReaderRuntime",
    "REQUIRED_SF5B_ALEMBIC_HEAD",
    "ReaderStartupRejected",
    "SCHOOL_DATABASE_ANCHOR_FUNCTION",
    "SchoolFactReaderConnectionFacts",
    "SchoolFactReaderSettings",
    "build_production_secure_reader",
    "build_school_fact_reader_engine",
    "require_school_fact_reader_connection",
    "verify_school_fact_reader_database",
]
