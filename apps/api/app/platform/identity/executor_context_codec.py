# pyright: strict

from __future__ import annotations

import base64
import hmac
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, SecretStr, StringConstraints, model_validator

from app.platform import contracts as platform_contracts
from app.platform.contracts import (
    CommandExecutionContext,
    FrozenContractModel,
    GuardianActorContext,
    SchoolId,
    StaffActorContext,
)

_DOMAIN = b"identity-command-context-transport:v1\x00"
_MAX_TOKEN_BYTES = 4096
_MAX_TTL_SECONDS = 300
_BASE64URL = Annotated[
    str,
    StringConstraints(
        strict=True, min_length=16, max_length=_MAX_TOKEN_BYTES, pattern=r"^[A-Za-z0-9_-]+$"
    ),
]


class CommandContextTransportRejected(RuntimeError):
    """表示跨进程执行上下文缺少可信签名、被篡改或已经失效。"""


class _TransportActor(FrozenContractModel):
    kind: Literal["staff", "guardian"]
    account_user_id: int = Field(strict=True, ge=1)
    school_id: UUID
    person_id: UUID
    assignment_id: UUID | None = None
    assignment_version: int | None = Field(default=None, strict=True, ge=1)
    guardian_relationship_id: UUID | None = None
    relationship_version: int | None = Field(default=None, strict=True, ge=1)
    authorization_version: int = Field(strict=True, ge=1)

    @model_validator(mode="after")
    def require_exact_actor_shape(self) -> _TransportActor:
        staff = (
            self.kind == "staff"
            and self.assignment_id is not None
            and self.assignment_version is not None
            and self.guardian_relationship_id is None
            and self.relationship_version is None
        )
        guardian = (
            self.kind == "guardian"
            and self.assignment_id is None
            and self.assignment_version is None
            and self.guardian_relationship_id is not None
            and self.relationship_version is not None
        )
        if not (staff or guardian):
            raise ValueError("跨进程操作者上下文形状无效。")
        return self


class _TransportCommand(FrozenContractModel):
    schema_version: Literal[1] = 1
    actor: _TransportActor
    request_id: UUID
    idempotency_key: Annotated[
        str,
        StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=255),
    ]
    expires_at: AwareDatetime


class _TransportEnvelope(FrozenContractModel):
    payload: _BASE64URL
    signature: Annotated[
        str,
        StringConstraints(strict=True, min_length=43, max_length=43, pattern=r"^[A-Za-z0-9_-]+$"),
    ]


def _urlsafe_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _urlsafe_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.b64decode(
        f"{value}{padding}",
        altchars=b"-_",
        validate=True,
    )


class CommandContextTransportCodec:
    """仅在身份域内签发、验签并重建可信命令上下文。"""

    def __init__(
        self,
        signing_key: bytes | SecretStr,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        raw_key = (
            signing_key.get_secret_value().encode("utf-8")
            if isinstance(signing_key, SecretStr)
            else signing_key
        )
        if type(raw_key) is not bytes or len(raw_key) < 32:
            raise ValueError("执行上下文传输签名密钥至少需要 32 字节。")
        self._signing_key = bytes(raw_key)
        self._clock = clock or (lambda: datetime.now(UTC))

    def encode(
        self,
        context: CommandExecutionContext,
        *,
        ttl_seconds: int = 60,
    ) -> str:
        """签发短时、用途分离且不暴露 provenance 的跨进程上下文。"""

        if type(context) is not CommandExecutionContext:
            raise TypeError("只能传输身份域签发的精确 CommandExecutionContext。")
        if not 1 <= ttl_seconds <= _MAX_TTL_SECONDS:
            raise ValueError("执行上下文传输有效期无效。")
        now = self._aware_now()
        command = _TransportCommand(
            actor=self._actor_payload(context),
            request_id=context.request_id,
            idempotency_key=context.idempotency_key,
            expires_at=now + timedelta(seconds=ttl_seconds),
        )
        payload_bytes = json.dumps(
            command.model_dump(mode="json"),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
        payload = _urlsafe_encode(payload_bytes)
        signature = _urlsafe_encode(
            hmac.new(self._signing_key, _DOMAIN + payload.encode("ascii"), sha256).digest()
        )
        token = f"{payload}.{signature}"
        if len(token.encode("ascii")) > _MAX_TOKEN_BYTES:
            raise ValueError("执行上下文传输令牌超过固定边界。")
        return token

    def decode(self, token: str) -> CommandExecutionContext:
        """先验签、再严格解析，并只通过身份域私有签发器重建上下文。"""

        try:
            if len(token.encode("ascii")) > _MAX_TOKEN_BYTES:
                raise ValueError
            payload, signature = token.split(".")
            envelope = _TransportEnvelope(payload=payload, signature=signature)
            expected = _urlsafe_encode(
                hmac.new(
                    self._signing_key,
                    _DOMAIN + envelope.payload.encode("ascii"),
                    sha256,
                ).digest()
            )
            if not hmac.compare_digest(envelope.signature, expected):
                raise ValueError
            raw_payload = _urlsafe_decode(envelope.payload)
            command = _TransportCommand.model_validate_json(raw_payload)
            now = self._aware_now()
            if not now < command.expires_at <= now + timedelta(seconds=_MAX_TTL_SECONDS):
                raise ValueError
            actor = self._restore_actor(command.actor)
            return platform_contracts._issue_command_execution_context(  # pyright: ignore[reportPrivateUsage]
                actor=actor,
                request_id=command.request_id,
                idempotency_key=command.idempotency_key,
            )
        except Exception as exc:
            raise CommandContextTransportRejected("执行上下文传输证明无效。") from exc

    @staticmethod
    def _actor_payload(context: CommandExecutionContext) -> _TransportActor:
        actor = context.actor
        if type(actor) is StaffActorContext:
            return _TransportActor(
                kind="staff",
                account_user_id=actor.account_user_id,
                school_id=UUID(str(actor.school_id)),
                person_id=UUID(str(actor.person_id)),
                assignment_id=UUID(str(actor.assignment_id)),
                assignment_version=actor.assignment_version,
                authorization_version=actor.authorization_version,
            )
        if type(actor) is GuardianActorContext:
            return _TransportActor(
                kind="guardian",
                account_user_id=actor.account_user_id,
                school_id=UUID(str(actor.school_id)),
                person_id=UUID(str(actor.person_id)),
                guardian_relationship_id=UUID(str(actor.guardian_relationship_id)),
                relationship_version=actor.relationship_version,
                authorization_version=actor.authorization_version,
            )
        raise TypeError("执行上下文操作者类型无效。")

    @staticmethod
    def _restore_actor(actor: _TransportActor) -> StaffActorContext | GuardianActorContext:
        if actor.kind == "staff":
            if actor.assignment_id is None or actor.assignment_version is None:
                raise CommandContextTransportRejected("教职工执行上下文不完整。")
            return platform_contracts._issue_staff_actor_context(  # pyright: ignore[reportPrivateUsage]
                account_user_id=actor.account_user_id,
                school_id=SchoolId(actor.school_id),
                person_id=platform_contracts.PersonId(actor.person_id),
                assignment_id=platform_contracts.AssignmentId(actor.assignment_id),
                assignment_version=actor.assignment_version,
                authorization_version=actor.authorization_version,
            )
        if actor.guardian_relationship_id is None or actor.relationship_version is None:
            raise CommandContextTransportRejected("家长执行上下文不完整。")
        return platform_contracts._issue_guardian_actor_context(  # pyright: ignore[reportPrivateUsage]
            account_user_id=actor.account_user_id,
            school_id=SchoolId(actor.school_id),
            person_id=platform_contracts.PersonId(actor.person_id),
            guardian_relationship_id=platform_contracts.GuardianRelationshipId(
                actor.guardian_relationship_id
            ),
            relationship_version=actor.relationship_version,
            authorization_version=actor.authorization_version,
        )

    def _aware_now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise RuntimeError("执行上下文传输时钟必须带时区。")
        return now.astimezone(UTC)
