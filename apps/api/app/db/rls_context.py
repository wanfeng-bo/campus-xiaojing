from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, Protocol, cast
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session


class _PsycopgLikeConnection(Protocol):
    def execute(self, query: str, params: tuple[object, ...]) -> object: ...


@dataclass(frozen=True, slots=True, kw_only=True)
class DatabaseExecutionContextEnvelope:
    """承载服务端签名并由数据库再次核验的单事务执行上下文。"""

    school_id: UUID
    account_user_id: int
    person_id: UUID
    context_kind: Literal["assignment", "guardian"]
    assignment_id: UUID | None
    assignment_version: int | None
    guardian_relationship_id: UUID | None
    guardian_relationship_version: int | None
    authorization_version: int
    request_id: UUID
    context_purpose: Literal["read", "command"]
    idempotency_key_hash: str | None
    session_nonce_hash: str
    proof_expires_at: datetime
    server_proof: str

    def __post_init__(self) -> None:
        """拒绝结构不完整或可产生歧义规范串的 envelope。"""

        assignment_context = (
            self.assignment_id is not None
            and self.assignment_version is not None
            and self.guardian_relationship_id is None
            and self.guardian_relationship_version is None
        )
        guardian_context = (
            self.assignment_id is None
            and self.assignment_version is None
            and self.guardian_relationship_id is not None
            and self.guardian_relationship_version is not None
        )
        if not (
            (self.context_kind == "assignment" and assignment_context)
            or (self.context_kind == "guardian" and guardian_context)
        ):
            raise ValueError("数据库执行上下文必须且只能绑定一种履职关系。")
        if self.account_user_id < 1 or self.authorization_version < 1:
            raise ValueError("账号和授权版本必须为正数。")
        if self.assignment_version is not None and self.assignment_version < 1:
            raise ValueError("任职版本必须为正数。")
        if (
            self.guardian_relationship_version is not None
            and self.guardian_relationship_version < 1
        ):
            raise ValueError("家校关系版本必须为正数。")
        if self.proof_expires_at.tzinfo is None:
            raise ValueError("证明到期时间必须带时区。")
        if not _is_sha256_hex(self.session_nonce_hash) or not _is_sha256_hex(self.server_proof):
            raise ValueError("数据库上下文摘要和证明必须是小写 SHA-256。")
        if self.context_purpose == "read" and self.idempotency_key_hash is not None:
            raise ValueError("只读上下文不得携带幂等键摘要。")
        if self.context_purpose == "command" and not _is_sha256_hex(self.idempotency_key_hash):
            raise ValueError("命令上下文必须携带小写 SHA-256 幂等键摘要。")


@dataclass(frozen=True, slots=True)
class BoundSchoolReactivationContext:
    """数据库已验证且绑定到当前事务的单学校重新启用目标。"""

    school_id: UUID
    batch_id: UUID
    unit_id: UUID
    fact_source_key: str
    expected_version: int


@dataclass(frozen=True, slots=True)
class BoundRootReactivationContext:
    """数据库已验证且绑定到当前事务的非学校根重新启用目标。"""

    school_id: UUID
    batch_id: UUID
    unit_id: UUID
    fact_kind: Literal["person", "student", "organization", "location"]
    fact_source_key: str
    expected_version: int
    target_id: UUID


@dataclass(frozen=True, slots=True)
class BoundSchoolReactivationReplay:
    """停用学校上下文中只读返回的既有正式确认结果。"""

    batch_id: UUID
    target_version: int
    changed_count: int


@dataclass(frozen=True, slots=True)
class BoundSchoolFactSchoolReactivationPreflight:
    """Writer 专用函数返回的停用学校最小预检投影。"""

    replay_batch_id: UUID | None
    replay_target_version: int | None
    replay_changed_count: int | None
    current_fact_version: int
    current_target_version: int | None


@dataclass(frozen=True, slots=True)
class BoundSchoolFactSchoolReactivationPreparation:
    """Writer 专用函数返回的唯一待确认批次。"""

    batch_id: UUID
    confirmation_token_sha256: str
    confirmation_expires_at: datetime
    reused: bool


def _is_sha256_hex(value: str | None) -> bool:
    return (
        value is not None
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _timestamp_token(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def execution_context_canonical_text(envelope: DatabaseExecutionContextEnvelope) -> str:
    """生成与 PostgreSQL 函数逐字一致的无歧义 HMAC 规范串。"""

    return "\x1f".join(
        (
            "context-v1",
            str(envelope.account_user_id),
            str(envelope.authorization_version),
            str(envelope.school_id),
            str(envelope.person_id),
            envelope.context_kind,
            str(envelope.assignment_id) if envelope.assignment_id is not None else "-",
            str(envelope.assignment_version) if envelope.assignment_version is not None else "-",
            (
                str(envelope.guardian_relationship_id)
                if envelope.guardian_relationship_id is not None
                else "-"
            ),
            (
                str(envelope.guardian_relationship_version)
                if envelope.guardian_relationship_version is not None
                else "-"
            ),
            str(envelope.request_id),
            envelope.context_purpose,
            envelope.idempotency_key_hash or "-",
            envelope.session_nonce_hash,
            _timestamp_token(envelope.proof_expires_at),
        )
    )


def account_options_canonical_text(
    *,
    account_user_id: int,
    authorization_version: int,
    session_nonce_hash: str,
    proof_expires_at: datetime,
) -> str:
    """生成枚举当前可选履职上下文所用的独立 HMAC 规范串。"""

    if account_user_id < 1 or authorization_version < 1:
        raise ValueError("账号和授权版本必须为正数。")
    if not _is_sha256_hex(session_nonce_hash) or proof_expires_at.tzinfo is None:
        raise ValueError("账号选项证明输入无效。")
    return "\x1f".join(
        (
            "account-options-v1",
            str(account_user_id),
            str(authorization_version),
            session_nonce_hash,
            _timestamp_token(proof_expires_at),
        )
    )


_BIND_SQL = """
SELECT app.bind_execution_context(
    %(school_id)s, %(account_user_id)s, %(person_id)s,
    %(context_kind)s, %(assignment_id)s, %(assignment_version)s,
    %(guardian_relationship_id)s, %(guardian_relationship_version)s,
    %(authorization_version)s, %(request_id)s, %(context_purpose)s,
    %(idempotency_key_hash)s, %(session_nonce_hash)s,
    %(proof_expires_at)s, %(server_proof)s
)
"""

_BIND_SCHOOL_REACTIVATION_SQL = """
SELECT app.bind_school_reactivation_context(
    CAST(%(school_id)s AS uuid), CAST(%(account_user_id)s AS integer),
    CAST(%(person_id)s AS uuid), CAST(%(context_kind)s AS text),
    CAST(%(assignment_id)s AS uuid), CAST(%(assignment_version)s AS bigint),
    CAST(%(guardian_relationship_id)s AS uuid),
    CAST(%(guardian_relationship_version)s AS bigint),
    CAST(%(authorization_version)s AS bigint), CAST(%(request_id)s AS uuid),
    CAST(%(context_purpose)s AS text), CAST(%(idempotency_key_hash)s AS text),
    CAST(%(session_nonce_hash)s AS text), CAST(%(proof_expires_at)s AS timestamptz),
    CAST(%(server_proof)s AS text), CAST(%(confirmation_token_sha256)s AS text),
    CAST(%(confirmation_session_sha256)s AS text), CAST(%(fact_source_key)s AS text),
    CAST(%(expected_version)s AS bigint), CAST(%(plan_sha256)s AS text),
    CAST(%(source_sha256)s AS text), CAST(%(unit_payload_sha256)s AS text),
    CAST(%(base_version)s AS bigint)
)
"""

_VALIDATE_SCHOOL_REACTIVATION_CANDIDATE_SQL = """
SELECT app.validate_school_reactivation_candidate(
    CAST(%(school_id)s AS uuid), CAST(%(account_user_id)s AS integer),
    CAST(%(person_id)s AS uuid), CAST(%(context_kind)s AS text),
    CAST(%(assignment_id)s AS uuid), CAST(%(assignment_version)s AS bigint),
    CAST(%(guardian_relationship_id)s AS uuid),
    CAST(%(guardian_relationship_version)s AS bigint),
    CAST(%(authorization_version)s AS bigint), CAST(%(request_id)s AS uuid),
    CAST(%(context_purpose)s AS text), CAST(%(idempotency_key_hash)s AS text),
    CAST(%(session_nonce_hash)s AS text), CAST(%(proof_expires_at)s AS timestamptz),
    CAST(%(server_proof)s AS text)
)
"""

_SCHOOL_FACT_SCHOOL_REACTIVATION_PREFLIGHT_SQL = """
SELECT replay_batch_id, replay_target_version, replay_changed_count,
       current_fact_version, current_target_version
FROM app.school_fact_school_reactivation_preflight(
    CAST(%(school_id)s AS uuid), CAST(%(account_user_id)s AS integer),
    CAST(%(person_id)s AS uuid), CAST(%(assignment_id)s AS uuid),
    CAST(%(assignment_version)s AS bigint),
    CAST(%(authorization_version)s AS bigint), CAST(%(request_id)s AS uuid),
    CAST(%(idempotency_key_hash)s AS text),
    CAST(%(session_nonce_hash)s AS text),
    CAST(%(proof_expires_at)s AS timestamptz), CAST(%(server_proof)s AS text),
    CAST(%(fact_source_key)s AS text), CAST(%(expected_version)s AS bigint),
    CAST(%(effective_at)s AS timestamptz), CAST(%(base_fact_version)s AS bigint),
    CAST(%(source_sha256)s AS text), CAST(%(plan_sha256)s AS text)
)
"""

_SCHOOL_FACT_SCHOOL_REACTIVATION_PREPARE_SQL = """
SELECT batch_id, confirmation_token_sha256, confirmation_expires_at, reused
FROM app.school_fact_school_reactivation_prepare(
    CAST(%(school_id)s AS uuid), CAST(%(account_user_id)s AS integer),
    CAST(%(person_id)s AS uuid), CAST(%(assignment_id)s AS uuid),
    CAST(%(assignment_version)s AS bigint),
    CAST(%(authorization_version)s AS bigint), CAST(%(request_id)s AS uuid),
    CAST(%(idempotency_key_hash)s AS text),
    CAST(%(session_nonce_hash)s AS text),
    CAST(%(proof_expires_at)s AS timestamptz), CAST(%(server_proof)s AS text),
    CAST(%(fact_source_key)s AS text), CAST(%(expected_version)s AS bigint),
    CAST(%(effective_at)s AS timestamptz), CAST(%(base_fact_version)s AS bigint),
    CAST(%(source_sha256)s AS text), CAST(%(plan_sha256)s AS text),
    CAST(%(proposed_batch_id)s AS uuid), CAST(%(proposed_unit_id)s AS uuid),
    CAST(%(confirmation_token_sha256)s AS text),
    CAST(%(confirmation_session_sha256)s AS text),
    CAST(%(confirmation_expires_at)s AS timestamptz),
    CAST(%(unit_payload_sha256)s AS text),
    CAST(%(result_locator_sha256)s AS text), CAST(%(template_version)s AS text),
    CAST(%(mapping_version)s AS text), CAST(%(mapping_config_sha256)s AS text),
    CAST(%(origin_sheet_code)s AS text), CAST(%(origin_row_number)s AS integer)
)
"""

_PURGE_EXPIRED_SCHOOL_FACT_PREPARATIONS_SQL = """
SELECT app.purge_expired_school_fact_preparations() AS removed_count
"""

_BIND_ROOT_REACTIVATION_SQL = """
SELECT app.bind_root_reactivation_context(
    CAST(%(confirmation_token_sha256)s AS text),
    CAST(%(confirmation_session_sha256)s AS text),
    CAST(%(fact_kind)s AS text), CAST(%(fact_source_key)s AS text),
    CAST(%(expected_version)s AS bigint)
)
"""


def execution_context_parameters(
    envelope: DatabaseExecutionContextEnvelope,
) -> dict[str, object]:
    """把内部 envelope 转成固定数据库函数参数，不接受任意字典透传。"""

    return {
        "school_id": envelope.school_id,
        "account_user_id": envelope.account_user_id,
        "person_id": envelope.person_id,
        "context_kind": envelope.context_kind,
        "assignment_id": envelope.assignment_id,
        "assignment_version": envelope.assignment_version,
        "guardian_relationship_id": envelope.guardian_relationship_id,
        "guardian_relationship_version": envelope.guardian_relationship_version,
        "authorization_version": envelope.authorization_version,
        "request_id": envelope.request_id,
        "context_purpose": envelope.context_purpose,
        "idempotency_key_hash": envelope.idempotency_key_hash,
        "session_nonce_hash": envelope.session_nonce_hash,
        "proof_expires_at": envelope.proof_expires_at,
        "server_proof": envelope.server_proof,
    }


def set_local_execution_context(
    target: Session | _PsycopgLikeConnection,
    envelope: DatabaseExecutionContextEnvelope,
) -> None:
    """在当前数据库事务内绑定上下文；不创建或提交事务。"""

    parameters = execution_context_parameters(envelope)
    if isinstance(target, Session):
        target.execute(text(_BIND_SQL.replace("%(", ":").replace(")s", "")), parameters)
        return
    target.execute(_BIND_SQL, parameters)  # type: ignore[arg-type]


def purge_expired_school_fact_preparations(session: Session) -> int:
    """通过 Writer-only 窄函数清理当前学校已过期且未消费的准备态。"""

    removed_count = int(
        session.execute(text(_PURGE_EXPIRED_SCHOOL_FACT_PREPARATIONS_SQL)).scalar_one()
    )
    if removed_count < 0:
        raise RuntimeError("学校事实准备态清理返回了无效数量。")
    return removed_count


def validate_school_reactivation_candidate(
    session: Session,
    envelope: DatabaseExecutionContextEnvelope,
) -> None:
    """只重验停用学校的教师候选身份，不绑定普通读写上下文。"""

    if envelope.context_purpose != "read" or envelope.context_kind != "assignment":
        raise ValueError("学校重新启用候选只接受教职工只读证明。")
    session.execute(
        text(_VALIDATE_SCHOOL_REACTIVATION_CANDIDATE_SQL.replace("%(", ":").replace(")s", "")),
        execution_context_parameters(envelope),
    )


def set_local_school_reactivation_context(
    target: Session | _PsycopgLikeConnection,
    envelope: DatabaseExecutionContextEnvelope,
    *,
    confirmation_token_sha256: str,
    confirmation_session_sha256: str,
    fact_source_key: str,
    expected_version: int,
    plan_sha256: str,
    source_sha256: str,
    unit_payload_sha256: str,
    base_version: int,
) -> None:
    """只为单一、已准备的学校根重新启用计划绑定命令事务。"""

    if envelope.context_purpose != "command" or envelope.context_kind != "assignment":
        raise ValueError("学校重新启用只接受教职工命令上下文。")
    if not _is_sha256_hex(confirmation_token_sha256) or not _is_sha256_hex(
        confirmation_session_sha256
    ):
        raise ValueError("学校重新启用确认摘要必须是小写 SHA-256。")
    if not fact_source_key or len(fact_source_key) > 255 or expected_version < 1:
        raise ValueError("学校重新启用目标或预期版本无效。")
    if (
        not all(
            _is_sha256_hex(value) for value in (plan_sha256, source_sha256, unit_payload_sha256)
        )
        or base_version < 1
    ):
        raise ValueError("学校重新启用计划绑定无效。")

    parameters = {
        **execution_context_parameters(envelope),
        "confirmation_token_sha256": confirmation_token_sha256,
        "confirmation_session_sha256": confirmation_session_sha256,
        "fact_source_key": fact_source_key,
        "expected_version": expected_version,
        "plan_sha256": plan_sha256,
        "source_sha256": source_sha256,
        "unit_payload_sha256": unit_payload_sha256,
        "base_version": base_version,
    }
    if isinstance(target, Session):
        target.execute(
            text(_BIND_SCHOOL_REACTIVATION_SQL.replace("%(", ":").replace(")s", "")),
            parameters,
        )
        return
    target.execute(_BIND_SCHOOL_REACTIVATION_SQL, parameters)  # type: ignore[arg-type]


def _school_fact_school_reactivation_parameters(
    envelope: DatabaseExecutionContextEnvelope,
    *,
    fact_source_key: str,
    expected_version: int,
    effective_at: datetime,
    base_fact_version: int,
    source_sha256: str,
    plan_sha256: str,
) -> dict[str, object]:
    if (
        envelope.context_purpose != "command"
        or envelope.context_kind != "assignment"
        or envelope.assignment_id is None
        or envelope.assignment_version is None
    ):
        raise ValueError("学校重新启用预检与准备只接受教职工命令证明。")
    if not fact_source_key or len(fact_source_key) > 255 or expected_version < 1:
        raise ValueError("学校重新启用目标或预期版本无效。")
    if (
        effective_at.tzinfo is None
        or effective_at.utcoffset() is None
        or base_fact_version < 1
        or not _is_sha256_hex(source_sha256)
        or not _is_sha256_hex(plan_sha256)
    ):
        raise ValueError("学校重新启用计划摘要或版本无效。")
    return {
        "school_id": envelope.school_id,
        "account_user_id": envelope.account_user_id,
        "person_id": envelope.person_id,
        "assignment_id": envelope.assignment_id,
        "assignment_version": envelope.assignment_version,
        "authorization_version": envelope.authorization_version,
        "request_id": envelope.request_id,
        "idempotency_key_hash": envelope.idempotency_key_hash,
        "session_nonce_hash": envelope.session_nonce_hash,
        "proof_expires_at": envelope.proof_expires_at,
        "server_proof": envelope.server_proof,
        "fact_source_key": fact_source_key,
        "expected_version": expected_version,
        "effective_at": effective_at,
        "base_fact_version": base_fact_version,
        "source_sha256": source_sha256,
        "plan_sha256": plan_sha256,
    }


def school_fact_school_reactivation_preflight(
    session: Session,
    envelope: DatabaseExecutionContextEnvelope,
    *,
    fact_source_key: str,
    expected_version: int,
    effective_at: datetime,
    base_fact_version: int,
    source_sha256: str,
    plan_sha256: str,
) -> BoundSchoolFactSchoolReactivationPreflight:
    """通过 Writer-only definer 读取单 school/reactivate 的固定投影。"""

    parameters = _school_fact_school_reactivation_parameters(
        envelope,
        fact_source_key=fact_source_key,
        expected_version=expected_version,
        effective_at=effective_at,
        base_fact_version=base_fact_version,
        source_sha256=source_sha256,
        plan_sha256=plan_sha256,
    )
    row = session.execute(
        text(_SCHOOL_FACT_SCHOOL_REACTIVATION_PREFLIGHT_SQL.replace("%(", ":").replace(")s", "")),
        parameters,
    ).one()
    replay_values = (
        row.replay_batch_id,
        row.replay_target_version,
        row.replay_changed_count,
    )
    if any(value is None for value in replay_values) and not all(
        value is None for value in replay_values
    ):
        raise RuntimeError("学校重新启用预检返回了不完整的重放结果。")
    return BoundSchoolFactSchoolReactivationPreflight(
        replay_batch_id=(
            UUID(str(row.replay_batch_id)) if row.replay_batch_id is not None else None
        ),
        replay_target_version=(
            int(row.replay_target_version) if row.replay_target_version is not None else None
        ),
        replay_changed_count=(
            int(row.replay_changed_count) if row.replay_changed_count is not None else None
        ),
        current_fact_version=int(row.current_fact_version),
        current_target_version=(
            int(row.current_target_version) if row.current_target_version is not None else None
        ),
    )


def school_fact_school_reactivation_prepare(
    session: Session,
    envelope: DatabaseExecutionContextEnvelope,
    *,
    fact_source_key: str,
    expected_version: int,
    effective_at: datetime,
    base_fact_version: int,
    source_sha256: str,
    plan_sha256: str,
    proposed_batch_id: UUID,
    proposed_unit_id: UUID,
    confirmation_token_sha256: str,
    confirmation_session_sha256: str,
    confirmation_expires_at: datetime,
    unit_payload_sha256: str,
    result_locator_sha256: str | None,
    template_version: str,
    mapping_version: str,
    mapping_config_sha256: str,
    origin_sheet_code: str,
    origin_row_number: int,
) -> BoundSchoolFactSchoolReactivationPreparation:
    """通过 Writer-only definer 原子复用或建立单一学校重新启用准备态。"""

    parameters = _school_fact_school_reactivation_parameters(
        envelope,
        fact_source_key=fact_source_key,
        expected_version=expected_version,
        effective_at=effective_at,
        base_fact_version=base_fact_version,
        source_sha256=source_sha256,
        plan_sha256=plan_sha256,
    )
    if (
        not _is_sha256_hex(confirmation_token_sha256)
        or not _is_sha256_hex(confirmation_session_sha256)
        or confirmation_expires_at.tzinfo is None
        or confirmation_expires_at.utcoffset() is None
        or not _is_sha256_hex(unit_payload_sha256)
        or (result_locator_sha256 is not None and not _is_sha256_hex(result_locator_sha256))
        or not template_version
        or not mapping_version
        or not _is_sha256_hex(mapping_config_sha256)
        or not origin_sheet_code
        or origin_row_number < 1
    ):
        raise ValueError("学校重新启用准备参数无效。")
    parameters.update(
        {
            "proposed_batch_id": proposed_batch_id,
            "proposed_unit_id": proposed_unit_id,
            "confirmation_token_sha256": confirmation_token_sha256,
            "confirmation_session_sha256": confirmation_session_sha256,
            "confirmation_expires_at": confirmation_expires_at,
            "unit_payload_sha256": unit_payload_sha256,
            "result_locator_sha256": result_locator_sha256,
            "template_version": template_version,
            "mapping_version": mapping_version,
            "mapping_config_sha256": mapping_config_sha256,
            "origin_sheet_code": origin_sheet_code,
            "origin_row_number": origin_row_number,
        }
    )
    row = session.execute(
        text(_SCHOOL_FACT_SCHOOL_REACTIVATION_PREPARE_SQL.replace("%(", ":").replace(")s", "")),
        parameters,
    ).one()
    token_sha256 = str(row.confirmation_token_sha256)
    expires_at = cast(datetime, row.confirmation_expires_at)
    if (
        not _is_sha256_hex(token_sha256)
        or expires_at.tzinfo is None
        or expires_at.utcoffset() is None
    ):
        raise RuntimeError("学校重新启用准备返回了无效确认绑定。")
    return BoundSchoolFactSchoolReactivationPreparation(
        batch_id=UUID(str(row.batch_id)),
        confirmation_token_sha256=token_sha256,
        confirmation_expires_at=expires_at,
        reused=bool(row.reused),
    )


def require_school_reactivation_context(session: Session) -> BoundSchoolReactivationContext:
    """读取由数据库重新验签的学校重新启用目标，不信任裸 GUC。"""

    row = session.execute(
        text(
            "SELECT school_id, batch_id, unit_id, fact_source_key, expected_version "
            "FROM app.require_school_reactivation_context()"
        )
    ).one()
    return BoundSchoolReactivationContext(
        school_id=UUID(str(row.school_id)),
        batch_id=UUID(str(row.batch_id)),
        unit_id=UUID(str(row.unit_id)),
        fact_source_key=str(row.fact_source_key),
        expected_version=int(row.expected_version),
    )


def set_local_root_reactivation_context(
    target: Session | _PsycopgLikeConnection,
    *,
    confirmation_token_sha256: str,
    confirmation_session_sha256: str,
    fact_kind: Literal["person", "student", "organization", "location"],
    fact_source_key: str,
    expected_version: int,
) -> None:
    """在普通命令上下文内绑定单一非学校根重新启用目标。"""

    if not _is_sha256_hex(confirmation_token_sha256) or not _is_sha256_hex(
        confirmation_session_sha256
    ):
        raise ValueError("根身份重新启用确认摘要必须是小写 SHA-256。")
    if fact_kind not in {"person", "student", "organization", "location"}:
        raise ValueError("根身份重新启用类型无效。")
    if not fact_source_key or len(fact_source_key) > 255 or expected_version < 1:
        raise ValueError("根身份重新启用目标或预期版本无效。")
    parameters = {
        "confirmation_token_sha256": confirmation_token_sha256,
        "confirmation_session_sha256": confirmation_session_sha256,
        "fact_kind": fact_kind,
        "fact_source_key": fact_source_key,
        "expected_version": expected_version,
    }
    if isinstance(target, Session):
        target.execute(
            text(_BIND_ROOT_REACTIVATION_SQL.replace("%(", ":").replace(")s", "")),
            parameters,
        )
        return
    target.execute(_BIND_ROOT_REACTIVATION_SQL, parameters)  # type: ignore[arg-type]


def require_root_reactivation_context(session: Session) -> BoundRootReactivationContext:
    """读取由数据库验签的非学校根重新启用目标。"""

    row = session.execute(
        text(
            "SELECT school_id, batch_id, unit_id, fact_kind, fact_source_key, "
            "expected_version, target_id FROM app.require_root_reactivation_context()"
        )
    ).one()
    return BoundRootReactivationContext(
        school_id=UUID(str(row.school_id)),
        batch_id=UUID(str(row.batch_id)),
        unit_id=UUID(str(row.unit_id)),
        fact_kind=cast(
            Literal["person", "student", "organization", "location"],
            str(row.fact_kind),
        ),
        fact_source_key=str(row.fact_source_key),
        expected_version=int(row.expected_version),
        target_id=UUID(str(row.target_id)),
    )


def school_reactivation_replay_result(
    session: Session,
) -> BoundSchoolReactivationReplay | None:
    """读取窄重放 marker；没有 marker 表示本次仍是待执行确认。"""

    row = session.execute(
        text(
            "SELECT batch_id, target_version, changed_count "
            "FROM app.school_reactivation_replay_result()"
        )
    ).one_or_none()
    if row is None:
        return None
    return BoundSchoolReactivationReplay(
        batch_id=UUID(str(row.batch_id)),
        target_version=int(row.target_version),
        changed_count=int(row.changed_count),
    )
