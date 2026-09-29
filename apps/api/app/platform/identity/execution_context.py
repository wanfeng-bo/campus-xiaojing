# pyright: strict

from __future__ import annotations

import json
import math
import re
import secrets
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal, NoReturn, Protocol, cast
from uuid import UUID

from pydantic import AwareDatetime, Field, StringConstraints
from redis.exceptions import RedisError
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.sql.elements import TextClause

from app.db.rls_context import (
    set_local_execution_context,
    validate_school_reactivation_candidate,
)
from app.platform.contracts import (
    ActorContext,
    AssignmentId,
    CommandExecutionContext,
    FrozenContractModel,
    GuardianActorContext,
    GuardianRelationshipId,
    PersonId,
    RequestExecutionContext,
    SchoolId,
    StaffActorContext,
    _issue_command_execution_context,  # pyright: ignore[reportPrivateUsage]
    _issue_guardian_actor_context,  # pyright: ignore[reportPrivateUsage]
    _issue_request_execution_context,  # pyright: ignore[reportPrivateUsage]
    _issue_staff_actor_context,  # pyright: ignore[reportPrivateUsage]
)
from app.platform.identity.context import CurrentUser
from app.platform.identity.context_proof import ContextProofSigner
from app.platform.identity.session import IdentitySession

OpaqueContextReference = Annotated[
    str,
    StringConstraints(pattern=r"^[A-Za-z0-9_-]{32,128}$"),
]
_OPAQUE_CONTEXT_REFERENCE_PATTERN = re.compile(r"^[A-Za-z0-9_-]{32,128}$")
_SHA256_HEX_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_LIST_EXECUTION_CONTEXT_OPTIONS = text(
    """
    SELECT context_kind, context_id, context_version,
           school_id, person_id, display_label
    FROM app.list_execution_context_options(
        :account_user_id, :authorization_version,
        :session_nonce_hash, :proof_expires_at, :server_proof
    )
    """
)
_LIST_SCHOOL_REACTIVATION_CANDIDATES = text(
    """
    SELECT context_kind, context_id, context_version,
           school_id, person_id, display_label
    FROM app.list_school_reactivation_candidates(
        :account_user_id, :authorization_version,
        :session_nonce_hash, :proof_expires_at, :server_proof
    )
    """
)


class ContextOptionRedisClient(Protocol):
    """定义不透明上下文引用存储所需的最小 Redis 操作。"""

    def setex(self, name: str, time: int, value: str) -> object: ...

    def get(self, name: str) -> object: ...


class ContextNoLongerValid(RuntimeError):
    """表示所选引用不存在、已过期或权威关系已经变化。"""


class ContextResolutionUnavailable(RuntimeError):
    """表示 Redis 或 PostgreSQL 暂时不能完成失败关闭的上下文解析。"""


class SelectedContextRef(FrozenContractModel):
    """客户端可见的最小身份选择项，不暴露任何内部稳定 ID 或版本。"""

    opaque_ref: OpaqueContextReference
    kind: Literal["staff_assignment", "guardian_relationship"]
    display_label: Annotated[str, StringConstraints(min_length=1, max_length=400)]
    school_label: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    effective_until: AwareDatetime


class SelectedContextIdentity(FrozenContractModel):
    """Server-private exact selection locator; every use still requires database validation."""

    account_user_id: Annotated[int, Field(strict=True, ge=1)]
    authorization_version: Annotated[int, Field(strict=True, ge=1)]
    session_nonce_hash: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
    session_expires_at: AwareDatetime
    school_id: UUID
    person_id: UUID
    context_kind: Literal["assignment", "guardian"]
    context_id: UUID
    context_version: Annotated[int, Field(strict=True, ge=1)]

    @classmethod
    def from_actor(cls, actor: ActorContext, session: IdentitySession) -> SelectedContextIdentity:
        if type(actor) is StaffActorContext:
            kind, context_id, version = "assignment", actor.assignment_id, actor.assignment_version
        elif type(actor) is GuardianActorContext:
            kind = "guardian"
            context_id, version = actor.guardian_relationship_id, actor.relationship_version
        else:
            raise ContextNoLongerValid("Selected actor was not issued by the identity boundary.")
        return cls(
            account_user_id=actor.account_user_id,
            authorization_version=actor.authorization_version,
            session_nonce_hash=session.session_nonce_hash,
            session_expires_at=datetime.fromisoformat(session.expires_at),
            school_id=actor.school_id,
            person_id=actor.person_id,
            context_kind=kind,
            context_id=context_id,
            context_version=version,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class _StoredContextOption:
    account_user_id: int
    authorization_version: int
    session_nonce_hash: str
    school_id: str
    person_id: str
    context_kind: Literal["assignment", "guardian"]
    context_id: str
    context_version: int
    effective_until: str


class ActorContextResolver:
    """按当前身份会话枚举并逐请求重验一个显式履职上下文。"""

    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        redis: ContextOptionRedisClient,
        proof_signer: ContextProofSigner,
        current_user: CurrentUser,
        identity_session: IdentitySession,
        option_ttl_seconds: int = 900,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not 1 <= option_ttl_seconds <= 3600:
            raise ValueError("履职上下文引用有效期必须介于 1 秒至 1 小时。")
        self._session_factory = session_factory
        self._redis = redis
        self._proof_signer = proof_signer
        self._current_user = current_user
        self._identity_session = identity_session
        self._option_ttl_seconds = option_ttl_seconds
        self._clock = clock or (lambda: datetime.now(UTC))
        self._require_matching_identity_session()

    def list_options(
        self, *, exclude_selected: SelectedContextIdentity | None = None
    ) -> tuple[SelectedContextRef, ...]:
        """通过数据库窄函数枚举当前账号可显式选择的最小上下文。"""

        return self._list_options_from(
            statement=_LIST_EXECUTION_CONTEXT_OPTIONS,
            assignment_only=False,
            exclude_selected=exclude_selected,
        )

    def list_school_reactivation_candidates(self) -> tuple[SelectedContextRef, ...]:
        """重新签发停用学校的教职工候选，不依赖此前缓存的普通上下文。"""

        return self._list_options_from(
            statement=_LIST_SCHOOL_REACTIVATION_CANDIDATES,
            assignment_only=True,
        )

    def _list_options_from(
        self,
        *,
        statement: TextClause,
        assignment_only: bool,
        exclude_selected: SelectedContextIdentity | None = None,
    ) -> tuple[SelectedContextRef, ...]:

        now = self._now()
        session_expires_at = self._session_expires_at()
        effective_until = min(
            session_expires_at,
            now + timedelta(seconds=self._option_ttl_seconds),
        )
        if effective_until <= now:
            raise ContextNoLongerValid("当前身份会话已经失效。")
        proof = self._proof_signer.sign_account_options(
            account_user_id=self._current_user.id,
            authorization_version=self._current_user.security_version,
            session_nonce_hash=self._identity_session.session_nonce_hash,
            proof_expires_at=effective_until,
        )
        try:
            with self._session_factory() as session, session.begin():
                rows = session.execute(statement, proof.parameters()).mappings().all()
        except DBAPIError as exc:
            self._raise_database_resolution_error(exc)

        options: list[SelectedContextRef] = []
        for row in rows:
            context_kind = row["context_kind"]
            if context_kind not in ("assignment", "guardian") or (
                assignment_only and context_kind != "assignment"
            ):
                raise ContextResolutionUnavailable("数据库返回了未知履职上下文类型。")
            context_id = row["context_id"]
            school_id = row["school_id"]
            person_id = row["person_id"]
            context_version = row["context_version"]
            display_label = row["display_label"]
            if (
                not isinstance(context_id, UUID)
                or not isinstance(school_id, UUID)
                or not isinstance(person_id, UUID)
                or type(context_version) is not int
                or context_version < 1
                or not isinstance(display_label, str)
                or not display_label.strip()
            ):
                raise ContextResolutionUnavailable("数据库返回了损坏的履职上下文。")
            if exclude_selected is not None and (
                exclude_selected.account_user_id == self._current_user.id
                and exclude_selected.authorization_version == self._current_user.security_version
                and exclude_selected.session_nonce_hash == self._identity_session.session_nonce_hash
                and exclude_selected.session_expires_at == session_expires_at
                and exclude_selected.school_id == school_id
                and exclude_selected.person_id == person_id
                and exclude_selected.context_kind == context_kind
                and exclude_selected.context_id == context_id
                and exclude_selected.context_version == context_version
            ):
                continue
            opaque_ref = secrets.token_urlsafe(32)
            stored = _StoredContextOption(
                account_user_id=self._current_user.id,
                authorization_version=self._current_user.security_version,
                session_nonce_hash=self._identity_session.session_nonce_hash,
                school_id=str(school_id),
                person_id=str(person_id),
                context_kind=context_kind,
                context_id=str(context_id),
                context_version=context_version,
                effective_until=effective_until.isoformat(),
            )
            ttl_seconds = max(1, math.ceil((effective_until - now).total_seconds()))
            try:
                self._redis.setex(
                    self._option_key(opaque_ref),
                    ttl_seconds,
                    json.dumps(asdict(stored), ensure_ascii=True, separators=(",", ":")),
                )
            except RedisError as exc:
                raise ContextResolutionUnavailable("履职上下文存储暂时不可用。") from exc
            school_label, separator, actor_label = display_label.partition(" · ")
            options.append(
                SelectedContextRef(
                    opaque_ref=opaque_ref,
                    kind=(
                        "staff_assignment"
                        if context_kind == "assignment"
                        else "guardian_relationship"
                    ),
                    display_label=actor_label if separator and actor_label else display_label,
                    school_label=school_label if separator and school_label else display_label,
                    effective_until=effective_until,
                )
            )
        return tuple(options)

    def resolve(self, opaque_ref: str) -> ActorContext:
        """只解析调用方明确选择的引用；失败后绝不尝试另一教师或家长身份。"""

        stored, effective_until = self._load_current_option(opaque_ref)
        actor = self._issue_actor(stored)
        verification_context = compose_request_context(actor, UUID(bytes=secrets.token_bytes(16)))
        envelope = self._proof_signer.sign_execution_context(
            verification_context,
            session_nonce_hash=self._identity_session.session_nonce_hash,
            proof_expires_at=effective_until,
        )
        try:
            with self._session_factory() as session, session.begin():
                set_local_execution_context(session, envelope)
        except DBAPIError as exc:
            self._raise_database_resolution_error(exc)
        return actor

    def resolve_selected(self, selected: SelectedContextIdentity) -> ActorContext:
        """Revalidate only the server-retained selection, without renewing login or duties."""
        self._require_matching_identity_session()
        if (
            selected.account_user_id != self._current_user.id
            or selected.authorization_version != self._current_user.security_version
            or selected.session_nonce_hash != self._identity_session.session_nonce_hash
            or selected.session_expires_at != self._session_expires_at()
        ):
            raise ContextNoLongerValid(
                "The retained selection belongs to another identity session."
            )
        effective_until = min(
            self._session_expires_at(),
            self._now() + timedelta(seconds=self._option_ttl_seconds),
        )
        stored = _StoredContextOption(
            account_user_id=selected.account_user_id,
            authorization_version=selected.authorization_version,
            session_nonce_hash=selected.session_nonce_hash,
            school_id=str(selected.school_id),
            person_id=str(selected.person_id),
            context_kind=selected.context_kind,
            context_id=str(selected.context_id),
            context_version=selected.context_version,
            effective_until=effective_until.isoformat(),
        )
        actor = self._issue_actor(stored)
        context = compose_request_context(actor, UUID(bytes=secrets.token_bytes(16)))
        envelope = self._proof_signer.sign_execution_context(
            context,
            session_nonce_hash=self._identity_session.session_nonce_hash,
            proof_expires_at=effective_until,
        )
        try:
            with self._session_factory() as session, session.begin():
                set_local_execution_context(session, envelope)
        except DBAPIError as exc:
            self._raise_database_resolution_error(exc)
        if effective_until <= self._now():
            raise ContextNoLongerValid("The identity proof expired during validation.")
        return actor

    def resolve_school_reactivation_candidate(self, opaque_ref: str) -> StaffActorContext:
        """仅为停用学校重验一个教师候选；不建立普通数据库执行上下文。"""

        stored, effective_until = self._load_current_option(opaque_ref)
        if stored.context_kind != "assignment":
            raise ContextNoLongerValid("学校重新启用只接受当前教职工履职身份。")
        actor = self._issue_actor(stored)
        if type(actor) is not StaffActorContext:
            raise ContextNoLongerValid("学校重新启用只接受当前教职工履职身份。")
        verification_context = compose_request_context(actor, UUID(bytes=secrets.token_bytes(16)))
        envelope = self._proof_signer.sign_execution_context(
            verification_context,
            session_nonce_hash=self._identity_session.session_nonce_hash,
            proof_expires_at=effective_until,
        )
        try:
            with self._session_factory() as session, session.begin():
                validate_school_reactivation_candidate(session, envelope)
        except DBAPIError as exc:
            self._raise_database_resolution_error(exc)
        return actor

    def _load_current_option(self, opaque_ref: str) -> tuple[_StoredContextOption, datetime]:
        if _OPAQUE_CONTEXT_REFERENCE_PATTERN.fullmatch(opaque_ref) is None:
            raise ContextNoLongerValid("所选履职身份已经失效，请重新选择。")
        try:
            raw_option = self._redis.get(self._option_key(opaque_ref))
        except RedisError as exc:
            raise ContextResolutionUnavailable("履职上下文存储暂时不可用。") from exc
        stored = self._parse_stored_option(raw_option)
        if stored is None:
            raise ContextNoLongerValid("所选履职身份已经失效，请重新选择。")
        effective_until = datetime.fromisoformat(stored.effective_until)
        if (
            stored.account_user_id != self._current_user.id
            or stored.authorization_version != self._current_user.security_version
            or stored.session_nonce_hash != self._identity_session.session_nonce_hash
            or effective_until.tzinfo is None
            or effective_until <= self._now()
            or effective_until > self._session_expires_at()
        ):
            raise ContextNoLongerValid("所选履职身份已经失效，请重新选择。")
        return stored, effective_until

    @staticmethod
    def _issue_actor(stored: _StoredContextOption) -> ActorContext:
        try:
            if stored.context_kind == "assignment":
                actor = _issue_staff_actor_context(
                    account_user_id=stored.account_user_id,
                    school_id=SchoolId(UUID(stored.school_id)),
                    person_id=PersonId(UUID(stored.person_id)),
                    assignment_id=AssignmentId(UUID(stored.context_id)),
                    assignment_version=stored.context_version,
                    authorization_version=stored.authorization_version,
                )
            else:
                actor = _issue_guardian_actor_context(
                    account_user_id=stored.account_user_id,
                    school_id=SchoolId(UUID(stored.school_id)),
                    person_id=PersonId(UUID(stored.person_id)),
                    guardian_relationship_id=GuardianRelationshipId(UUID(stored.context_id)),
                    relationship_version=stored.context_version,
                    authorization_version=stored.authorization_version,
                )
        except (TypeError, ValueError) as exc:
            raise ContextNoLongerValid("所选履职身份已经失效，请重新选择。") from exc
        return actor

    def _require_matching_identity_session(self) -> None:
        if (
            self._identity_session.user_id != self._current_user.id
            or self._identity_session.external_subject != self._current_user.external_subject
            or self._identity_session.security_version != self._current_user.security_version
            or self._session_expires_at() <= self._now()
        ):
            raise ContextNoLongerValid("当前身份会话已经失效。")

    def _parse_stored_option(self, raw_option: object) -> _StoredContextOption | None:
        if not isinstance(raw_option, str):
            return None
        try:
            decoded = cast(object, json.loads(raw_option))
            expected_fields = {
                "account_user_id",
                "authorization_version",
                "session_nonce_hash",
                "school_id",
                "person_id",
                "context_kind",
                "context_id",
                "context_version",
                "effective_until",
            }
            if not isinstance(decoded, dict):
                return None
            payload = cast(dict[object, object], decoded)
            if set(payload) != expected_fields:
                return None
            account_user_id = payload["account_user_id"]
            authorization_version = payload["authorization_version"]
            session_nonce_hash = payload["session_nonce_hash"]
            school_id = payload["school_id"]
            person_id = payload["person_id"]
            context_kind = payload["context_kind"]
            context_id = payload["context_id"]
            context_version = payload["context_version"]
            effective_until_text = payload["effective_until"]
            if (
                type(account_user_id) is not int
                or type(authorization_version) is not int
                or type(context_version) is not int
                or account_user_id < 1
                or authorization_version < 1
                or context_version < 1
                or not isinstance(session_nonce_hash, str)
                or not isinstance(school_id, str)
                or not isinstance(person_id, str)
                or not isinstance(context_id, str)
                or not isinstance(effective_until_text, str)
            ):
                return None
            if context_kind == "assignment":
                normalized_kind: Literal["assignment", "guardian"] = "assignment"
            elif context_kind == "guardian":
                normalized_kind = "guardian"
            else:
                return None
            UUID(school_id)
            UUID(person_id)
            UUID(context_id)
            effective_until = datetime.fromisoformat(effective_until_text)
            if (
                effective_until.tzinfo is None
                or _SHA256_HEX_PATTERN.fullmatch(session_nonce_hash) is None
            ):
                return None
            return _StoredContextOption(
                account_user_id=account_user_id,
                authorization_version=authorization_version,
                session_nonce_hash=session_nonce_hash,
                school_id=school_id,
                person_id=person_id,
                context_kind=normalized_kind,
                context_id=context_id,
                context_version=context_version,
                effective_until=effective_until_text,
            )
        except (TypeError, ValueError, json.JSONDecodeError):
            return None

    def _option_key(self, opaque_ref: str) -> str:
        return f"identity:context:{self._identity_session.session_nonce_hash}:{opaque_ref}"

    def _session_expires_at(self) -> datetime:
        expires_at = datetime.fromisoformat(self._identity_session.expires_at)
        if expires_at.tzinfo is None:
            raise ContextNoLongerValid("当前身份会话已经失效。")
        return expires_at

    def _now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None:
            raise RuntimeError("上下文解析时钟必须带时区。")
        return now

    @staticmethod
    def _raise_database_resolution_error(error: DBAPIError) -> NoReturn:
        sqlstate = getattr(error.orig, "sqlstate", None)
        if sqlstate == "28000":
            raise ContextNoLongerValid("所选履职身份已经失效，请重新选择。") from error
        raise ContextResolutionUnavailable("履职上下文数据库校验暂时不可用。") from error


def compose_request_context(actor: ActorContext, request_id: UUID) -> RequestExecutionContext:
    """把已核验 actor 与服务端请求 ID 组合为只读执行上下文。"""

    if request_id.int == 0:
        raise ValueError("服务端请求标识必须是非零 UUID。")
    return _issue_request_execution_context(actor=actor, request_id=request_id)


def compose_command_context(
    actor: ActorContext,
    request_id: UUID,
    idempotency_key: str,
) -> CommandExecutionContext:
    """为单个写命令组合独立幂等键，不把幂等身份写入登录会话。"""

    if request_id.int == 0:
        raise ValueError("服务端请求标识必须是非零 UUID。")
    if not idempotency_key.strip() or len(idempotency_key.encode("utf-8")) > 255:
        raise ValueError("命令幂等键必须是 1 至 255 字节的非空文本。")
    return _issue_command_execution_context(
        actor=actor,
        request_id=request_id,
        idempotency_key=idempotency_key,
    )
