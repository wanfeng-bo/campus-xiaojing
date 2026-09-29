# pyright: strict

from __future__ import annotations

from typing import Annotated, Self
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, StringConstraints, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _secret_bytes(value: SecretStr) -> bytes:
    encoded = value.get_secret_value().encode("utf-8")
    if len(encoded) < 32:
        raise ValueError("Executor 调用者密钥至少需要 32 字节。")
    return encoded


class SchoolFactExecutorClientSettings(BaseSettings):
    """API 调用侧纯传输配置；不导入 Writer 数据库配置或引擎。"""

    model_config = SettingsConfigDict(
        env_prefix="SCHOOL_FACT_EXECUTOR_",
        env_file=None,
        extra="ignore",
        frozen=True,
        hide_input_in_errors=True,
    )

    base_url: Annotated[
        str,
        StringConstraints(strict=True, strip_whitespace=True, min_length=8, max_length=512),
    ]
    caller_auth_key: SecretStr
    context_transport_hmac_key: SecretStr
    request_timeout_seconds: float = Field(default=10, ge=0.1, le=60)

    @field_validator("caller_auth_key", "context_transport_hmac_key")
    @classmethod
    def require_caller_key(cls, value: SecretStr) -> SecretStr:
        _secret_bytes(value)
        return value

    @model_validator(mode="after")
    def require_distinct_transport_purposes(self) -> Self:
        if self.caller_auth_key.get_secret_value() == (
            self.context_transport_hmac_key.get_secret_value()
        ):
            raise ValueError("Executor caller and context keys must be distinct.")
        return self

    @field_validator("base_url")
    @classmethod
    def require_private_service_origin(cls, value: str) -> str:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise ValueError("Writer Executor 地址必须是无凭据、无路径的固定服务 origin。")
        return value.rstrip("/")
