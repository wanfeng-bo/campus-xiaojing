from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import URL


class MigrationSettings(BaseSettings):
    """只供 Alembic 使用的迁移连接与上下文证明配置。"""

    host: str
    port: int = 5432
    db: str
    user: str
    password: SecretStr
    owner_role: Literal["campus_ai_db_owner"] = "campus_ai_db_owner"
    context_signing_key: SecretStr

    model_config = SettingsConfigDict(
        env_prefix="POSTGRES_MIGRATION_",
        extra="ignore",
        hide_input_in_errors=True,
    )

    @field_validator("context_signing_key")
    @classmethod
    def validate_context_signing_key(cls, value: SecretStr) -> SecretStr:
        if len(value.get_secret_value().encode("utf-8")) < 32:
            raise ValueError("数据库上下文签名密钥必须至少包含 32 个字节。")
        return value

    @property
    def database_url(self) -> URL:
        """生成不会在默认字符串表示中暴露密码的迁移连接 URL。"""

        return URL.create(
            "postgresql+psycopg",
            username=self.user,
            password=self.password.get_secret_value(),
            host=self.host,
            port=self.port,
            database=self.db,
        )


@lru_cache
def get_migration_settings() -> MigrationSettings:
    """返回进程内固定的迁移配置，不回退读取应用 runtime 凭据。"""

    return MigrationSettings()  # pyright: ignore[reportCallIssue]
