from __future__ import annotations

import json
import re
import secrets
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from hmac import compare_digest
from typing import Protocol

_TRUSTED_MOBILE_PATTERN = re.compile(r"\+?[0-9]{7,15}")


class SessionRedisClient(Protocol):
    """定义身份会话所需的最小 Redis 操作，避免会话层依赖业务模块。"""

    def setex(self, name: str, time: int, value: str) -> object:
        """按固定有效期写入文本会话值。"""

    def get(self, name: str) -> object:
        """读取文本会话值，不存在时返回空值。"""

    def delete(self, *names: str) -> object:
        """删除指定会话键。"""


class SessionUser(Protocol):
    """定义创建身份会话所需的最小当前用户字段，不耦合 FastAPI 上下文实现。"""

    @property
    def id(self) -> int:
        """返回本地用户主键。"""

        ...

    @property
    def external_subject(self) -> str:
        """返回稳定的外部身份键。"""

        ...

    @property
    def display_name(self) -> str:
        """返回可展示的本地姓名。"""

        ...

    @property
    def security_version(self) -> int:
        """返回本地账号当前安全版本。"""

        ...

    @property
    def trusted_mobile(self) -> str | None:
        """返回本次可信登录取得的可选结构化电话。"""

        ...


@dataclass(frozen=True)
class IdentitySession:
    """表示 Redis 中保存的最小本地身份会话，不包含钉钉凭证或权限。"""

    user_id: int
    external_subject: str
    display_name: str
    security_version: int
    session_nonce_hash: str
    issued_at: str
    expires_at: str
    trusted_mobile: str | None = field(default=None, repr=False)


class IdentitySessionStore:
    """以随机、不透明令牌管理 Redis 身份会话，调用方负责把令牌写入受控 Cookie。"""

    def __init__(self, *, redis: SessionRedisClient, ttl_seconds: int) -> None:
        """绑定 Redis 客户端和固定会话有效期，不创建或保存任何浏览器令牌副本。"""

        self._redis = redis
        self._ttl_seconds = ttl_seconds

    def create(self, current_user: SessionUser) -> str:
        """为已认证本地用户创建八小时会话，只保存身份映射所需的最小字段。"""

        issued_at = datetime.now(UTC)
        token = secrets.token_urlsafe(32)
        identity_session = IdentitySession(
            user_id=current_user.id,
            external_subject=current_user.external_subject,
            display_name=current_user.display_name,
            security_version=current_user.security_version,
            session_nonce_hash=self._nonce_hash(token),
            issued_at=issued_at.isoformat(),
            expires_at=(issued_at + timedelta(seconds=self._ttl_seconds)).isoformat(),
            trusted_mobile=current_user.trusted_mobile,
        )
        self._redis.setex(self._key(token), self._ttl_seconds, json.dumps(asdict(identity_session)))
        return token

    def read(self, token: str) -> IdentitySession | None:
        """读取并校验会话内容；已失效、缺失或损坏的值均按无会话处理。"""

        if not token:
            return None
        raw_session = self._redis.get(self._key(token))
        if not isinstance(raw_session, str):
            return None
        try:
            payload = json.loads(raw_session)
            legacy_fields = {
                "user_id",
                "external_subject",
                "display_name",
                "security_version",
                "session_nonce_hash",
                "issued_at",
                "expires_at",
            }
            if not isinstance(payload, dict) or set(payload) not in (
                legacy_fields,
                legacy_fields | {"trusted_mobile"},
            ):
                return None
            if type(payload["user_id"]) is not int or type(payload["security_version"]) is not int:
                return None
            if not all(
                isinstance(payload[field], str)
                for field in (
                    "external_subject",
                    "display_name",
                    "session_nonce_hash",
                    "issued_at",
                    "expires_at",
                )
            ):
                return None
            trusted_mobile = payload.get("trusted_mobile")
            if trusted_mobile is not None and (
                not isinstance(trusted_mobile, str)
                or _TRUSTED_MOBILE_PATTERN.fullmatch(trusted_mobile) is None
            ):
                return None
            identity_session = IdentitySession(
                user_id=payload["user_id"],
                external_subject=payload["external_subject"],
                display_name=payload["display_name"],
                security_version=payload["security_version"],
                session_nonce_hash=payload["session_nonce_hash"],
                issued_at=payload["issued_at"],
                expires_at=payload["expires_at"],
                trusted_mobile=trusted_mobile,
            )
            issued_at = datetime.fromisoformat(identity_session.issued_at)
            expires_at = datetime.fromisoformat(identity_session.expires_at)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None
        now = datetime.now(UTC)
        if (
            identity_session.user_id < 1
            or identity_session.security_version < 1
            or issued_at.tzinfo is None
            or expires_at.tzinfo is None
            or issued_at > now
            or expires_at <= issued_at
            or expires_at <= now
            or expires_at - issued_at > timedelta(seconds=self._ttl_seconds)
            or not compare_digest(identity_session.session_nonce_hash, self._nonce_hash(token))
        ):
            return None
        return identity_session

    def delete(self, token: str) -> None:
        """删除指定随机会话令牌，不接受外部身份键作为 Redis 键。"""

        if token:
            self._redis.delete(self._key(token))

    @staticmethod
    def _key(token: str) -> str:
        """构造会话专用 Redis 键，隔离未来其他缓存用途。"""

        return f"identity:session:{token}"

    @staticmethod
    def _nonce_hash(token: str) -> str:
        """只保存不透明会话令牌的单向摘要，供数据库证明绑定当前会话。"""

        return sha256(token.encode("utf-8")).hexdigest()
