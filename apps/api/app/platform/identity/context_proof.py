# pyright: strict

from __future__ import annotations

import hmac
from dataclasses import dataclass, replace
from datetime import datetime
from hashlib import sha256

from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.settings import Settings
from app.db.rls_context import (
    DatabaseExecutionContextEnvelope,
    account_options_canonical_text,
    execution_context_canonical_text,
)
from app.platform.contracts import (
    CommandExecutionContext,
    GuardianActorContext,
    RequestExecutionContext,
    StaffActorContext,
)


class ContextSigningKeyMismatch(RuntimeError):
    """表示应用签名密钥与数据库当前密钥指纹不一致。"""


@dataclass(frozen=True, slots=True, kw_only=True)
class AccountOptionsProof:
    """承载调用未选学校上下文枚举函数所需的最小服务端证明。"""

    account_user_id: int
    authorization_version: int
    session_nonce_hash: str
    proof_expires_at: datetime
    server_proof: str

    def parameters(self) -> dict[str, object]:
        """返回固定命名参数，禁止任意字典透传到数据库函数。"""

        return {
            "account_user_id": self.account_user_id,
            "authorization_version": self.authorization_version,
            "session_nonce_hash": self.session_nonce_hash,
            "proof_expires_at": self.proof_expires_at,
            "server_proof": self.server_proof,
        }


class ContextProofSigner:
    """只为服务端已解析上下文生成与 PostgreSQL 一致的 HMAC 证明。"""

    def __init__(self, signing_key: bytes | SecretStr) -> None:
        raw_key = (
            signing_key.get_secret_value().encode("utf-8")
            if isinstance(signing_key, SecretStr)
            else signing_key
        )
        if len(raw_key) < 32:
            raise ValueError("运行时数据库上下文签名密钥必须至少包含 32 个字节。")
        self._signing_key = bytes(raw_key)

    @classmethod
    def from_settings(cls, settings: Settings) -> ContextProofSigner:
        """从集中运行配置建立签名器，不向调用方暴露 Secret 明文。"""

        return cls(settings.rls_context_signing_key)

    @property
    def fingerprint(self) -> str:
        """返回可安全与数据库公开值比较的 SHA-256 指纹。"""

        return sha256(self._signing_key).hexdigest()

    def sign_account_options(
        self,
        *,
        account_user_id: int,
        authorization_version: int,
        session_nonce_hash: str,
        proof_expires_at: datetime,
    ) -> AccountOptionsProof:
        """签发未选择学校前的窄上下文枚举证明。"""

        canonical = account_options_canonical_text(
            account_user_id=account_user_id,
            authorization_version=authorization_version,
            session_nonce_hash=session_nonce_hash,
            proof_expires_at=proof_expires_at,
        )
        return AccountOptionsProof(
            account_user_id=account_user_id,
            authorization_version=authorization_version,
            session_nonce_hash=session_nonce_hash,
            proof_expires_at=proof_expires_at,
            server_proof=self._sign(canonical),
        )

    def sign_execution_context(
        self,
        context: RequestExecutionContext | CommandExecutionContext,
        *,
        session_nonce_hash: str,
        proof_expires_at: datetime,
    ) -> DatabaseExecutionContextEnvelope:
        """把一个精确的可信请求上下文绑定到当前会话和证明到期点。"""

        if type(context) not in (RequestExecutionContext, CommandExecutionContext):
            raise TypeError("只能签发身份边界生成的精确执行上下文。")
        actor = context.actor
        if type(actor) is StaffActorContext:
            context_kind = "assignment"
            assignment_id = actor.assignment_id
            assignment_version = actor.assignment_version
            guardian_relationship_id = None
            guardian_relationship_version = None
        elif type(actor) is GuardianActorContext:
            context_kind = "guardian"
            assignment_id = None
            assignment_version = None
            guardian_relationship_id = actor.guardian_relationship_id
            guardian_relationship_version = actor.relationship_version
        else:
            raise TypeError("执行上下文包含未经签发的操作者类型。")

        idempotency_key_hash = (
            sha256(context.idempotency_key.encode("utf-8")).hexdigest()
            if type(context) is CommandExecutionContext
            else None
        )
        unsigned = DatabaseExecutionContextEnvelope(
            school_id=actor.school_id,
            account_user_id=actor.account_user_id,
            person_id=actor.person_id,
            context_kind=context_kind,
            assignment_id=assignment_id,
            assignment_version=assignment_version,
            guardian_relationship_id=guardian_relationship_id,
            guardian_relationship_version=guardian_relationship_version,
            authorization_version=actor.authorization_version,
            request_id=context.request_id,
            context_purpose=("command" if type(context) is CommandExecutionContext else "read"),
            idempotency_key_hash=idempotency_key_hash,
            session_nonce_hash=session_nonce_hash,
            proof_expires_at=proof_expires_at,
            server_proof="0" * 64,
        )
        return replace(
            unsigned,
            server_proof=self._sign(execution_context_canonical_text(unsigned)),
        )

    def verify_database_fingerprint(self, session: Session) -> None:
        """常量时间核验数据库当前密钥指纹；缺失、多行或不一致均失败关闭。"""

        rows = session.execute(
            text(
                "SELECT key_version, fingerprint_sha256 FROM app.context_signing_key_fingerprint()"
            )
        ).all()
        if (
            len(rows) != 1
            or type(rows[0][0]) is not int
            or rows[0][0] < 1
            or not isinstance(rows[0][1], str)
            or not hmac.compare_digest(rows[0][1], self.fingerprint)
        ):
            raise ContextSigningKeyMismatch("运行时上下文签名密钥与数据库不一致。")

    def _sign(self, canonical_text: str) -> str:
        return hmac.new(
            self._signing_key,
            canonical_text.encode("utf-8"),
            sha256,
        ).hexdigest()
