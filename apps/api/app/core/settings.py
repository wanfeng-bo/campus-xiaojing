from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import quote, urlsplit
from uuid import UUID

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import URL

from app.db.schema_contract import DatabaseSchemaContract

PROJECT_ROOT = Path(__file__).resolve().parents[4]


class Settings(BaseSettings):
    """集中读取应用、数据库和有界 I/O 配置，避免调用方直接接触环境变量。"""

    app_env: Literal["development", "test", "production"] = "development"
    postgres_host: str
    postgres_port: int
    postgres_db: str
    postgres_user: str
    postgres_password: str
    postgres_schema_contract: DatabaseSchemaContract = "identity-v20"
    postgres_expected_anchor_id: UUID | None = None
    postgres_expected_school_id: UUID | None = None
    postgres_expected_system_identifier: str | None = None
    postgres_pool_size: int = Field(default=4, ge=1, le=16)
    postgres_max_overflow: int = Field(default=0, ge=0, le=8)
    postgres_pool_timeout_seconds: float = Field(
        default=3.0,
        ge=0.1,
        le=10,
        allow_inf_nan=False,
    )
    postgres_pool_recycle_seconds: int = Field(default=600, ge=30, le=1_800)
    rls_context_signing_key: SecretStr
    redis_host: str = "127.0.0.1"
    redis_port: int
    redis_password: str
    redis_connect_timeout_seconds: float = Field(
        default=0.5,
        gt=0,
        le=0.5,
        allow_inf_nan=False,
    )
    redis_read_timeout_seconds: float = Field(
        default=0.5,
        gt=0,
        le=0.5,
        allow_inf_nan=False,
    )
    repair_draft_model_enabled: bool = False
    repair_draft_model_service_name: str = "DeepSeek"
    repair_draft_model_base_url: str | None = None
    repair_draft_model_model: str | None = None
    repair_draft_model_api_key: SecretStr | None = None
    repair_draft_model_timeout_seconds: float = Field(
        default=10.0,
        gt=0,
        le=30,
        allow_inf_nan=False,
    )
    identity_provider: Literal["local_test", "dingtalk"] = "local_test"
    dingtalk_corp_id: str | None = None
    dingtalk_app_key: str | None = None
    dingtalk_app_secret: str | None = None
    dingtalk_identity_timeout_seconds: int = 5
    dingtalk_login_global_rate_per_minute: int = 120
    dingtalk_login_concurrency_limit: int = 30
    identity_session_ttl_seconds: Literal[28_800] = 28_800
    identity_session_cookie_name: str = "campus_ai_session"
    master_data_import_max_bytes: Literal[10_485_760] = 10_485_760

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        hide_input_in_errors=True,
    )

    @property
    def database_url(self) -> URL:
        """生成受 SQLAlchemy 保护的连接 URL，避免调用方手工拼接敏感密码。"""

        return URL.create(
            drivername="postgresql+psycopg",
            username=self.postgres_user,
            password=self.postgres_password,
            host=self.postgres_host,
            port=self.postgres_port,
            database=self.postgres_db,
        )

    @property
    def redis_url(self) -> str:
        """生成仅供 Redis 客户端使用的连接地址，并对密码中的保留字符编码。"""

        encoded_password = quote(self.redis_password, safe="")
        return f"redis://:{encoded_password}@{self.redis_host}:{self.redis_port}/0"

    @field_validator("rls_context_signing_key")
    @classmethod
    def validate_rls_context_signing_key(cls, value: SecretStr) -> SecretStr:
        """拒绝低熵的运行时上下文证明密钥，且不在错误中回显内容。"""

        if len(value.get_secret_value().encode("utf-8")) < 32:
            raise ValueError("运行时数据库上下文签名密钥必须至少包含 32 个字节。")
        return value

    @model_validator(mode="after")
    def validate_runtime_database_anchor_configuration(self) -> Settings:
        """生产 runtime 必须固定到完整 anchor；其他环境也拒绝半套配置。"""

        values = (
            self.postgres_expected_anchor_id,
            self.postgres_expected_school_id,
            self.postgres_expected_system_identifier,
        )
        configured = tuple(value is not None for value in values)
        if any(configured) and not all(configured):
            raise ValueError("Runtime database anchor configuration must be complete.")
        if self.app_env == "production" and not all(configured):
            raise ValueError("Production runtime requires the complete database anchor.")
        identifier = self.postgres_expected_system_identifier
        if identifier is not None and (
            not identifier.isascii() or not identifier.isdecimal() or len(identifier) < 10
        ):
            raise ValueError("Runtime database system identifier is invalid.")
        return self

    @model_validator(mode="after")
    def validate_repair_draft_model_configuration(self) -> Settings:
        """关闭时不索取凭据；启用时要求唯一适配器所需配置完整。"""

        if not self.repair_draft_model_enabled:
            return self

        api_key = self.repair_draft_model_api_key
        missing_configuration = [
            setting_name
            for setting_name, setting_value in (
                ("REPAIR_DRAFT_MODEL_SERVICE_NAME", self.repair_draft_model_service_name),
                ("REPAIR_DRAFT_MODEL_BASE_URL", self.repair_draft_model_base_url),
                ("REPAIR_DRAFT_MODEL_MODEL", self.repair_draft_model_model),
                (
                    "REPAIR_DRAFT_MODEL_API_KEY",
                    api_key.get_secret_value() if api_key is not None else None,
                ),
            )
            if setting_value is None or not setting_value.strip()
        ]
        if missing_configuration:
            missing_names = "、".join(missing_configuration)
            raise ValueError(f"报修文字整理模型缺少配置：{missing_names}。")

        base_url = self.repair_draft_model_base_url
        assert base_url is not None
        normalized_base_url = base_url.strip()
        try:
            parsed_base_url = urlsplit(normalized_base_url)
            _ = parsed_base_url.port
        except ValueError:
            raise ValueError(
                "REPAIR_DRAFT_MODEL_BASE_URL 必须是明确的 HTTP(S) 基础地址。"
            ) from None
        if (
            parsed_base_url.scheme not in {"http", "https"}
            or parsed_base_url.hostname is None
            or parsed_base_url.username is not None
            or parsed_base_url.password is not None
            or parsed_base_url.query
            or parsed_base_url.fragment
            or any(character.isspace() for character in normalized_base_url)
        ):
            raise ValueError("REPAIR_DRAFT_MODEL_BASE_URL 必须是明确的 HTTP(S) 基础地址。")
        self.repair_draft_model_base_url = normalized_base_url
        return self

    @model_validator(mode="after")
    def validate_identity_configuration(self) -> Settings:
        """限制正式环境身份提供者，并要求钉钉模式具备最小应用配置。"""

        if self.app_env == "production" and self.identity_provider != "dingtalk":
            raise ValueError("生产环境只能使用钉钉身份提供者。")
        if not 1 <= self.dingtalk_login_global_rate_per_minute <= 2_000:
            raise ValueError("钉钉每分钟登录上限必须介于 1 至 2000。")
        if not 1 <= self.dingtalk_login_concurrency_limit <= 200:
            raise ValueError("钉钉同时登录上限必须介于 1 至 200。")
        if self.identity_provider != "dingtalk":
            return self

        missing_configuration = [
            setting_name
            for setting_name, setting_value in (
                ("DINGTALK_CORP_ID", self.dingtalk_corp_id),
                ("DINGTALK_APP_KEY", self.dingtalk_app_key),
                ("DINGTALK_APP_SECRET", self.dingtalk_app_secret),
            )
            if not setting_value
        ]
        if missing_configuration:
            missing_names = "、".join(missing_configuration)
            raise ValueError(f"钉钉身份提供者缺少配置：{missing_names}。")
        if not 1 <= self.dingtalk_identity_timeout_seconds <= 5:
            raise ValueError("钉钉身份验证超时必须介于 1 至 5 秒。")
        return self


@lru_cache
def get_settings() -> Settings:
    """返回进程内缓存的配置，确保同一次运行使用一致的环境读取结果。"""

    return Settings()  # pyright: ignore[reportCallIssue]  # Pydantic 从 .env 读取必填字段。
