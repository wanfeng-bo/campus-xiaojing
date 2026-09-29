from __future__ import annotations

from collections.abc import Mapping
from hashlib import sha256
from typing import Any

from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session

from app.platform.audit.contracts import (
    BusinessAuditReceipt,
    BusinessAuditRecord,
)
from app.platform.audit.models import AuditLog
from app.platform.contracts import (
    CommandExecutionContext,
    GuardianActorContext,
    StaffActorContext,
)


def _uses_postgresql(session: Session) -> bool:
    """只在真实 SQLAlchemy PostgreSQL Session 上启用数据库窄函数。"""

    get_bind = getattr(session, "get_bind", None)
    if get_bind is None:
        return False
    return get_bind().dialect.name == "postgresql"


def append_audit_log(
    *,
    session: Session,
    actor_user_id: int | None,
    event_type: str,
    target_type: str,
    target_id: str,
    before_summary: Mapping[str, Any],
    after_summary: Mapping[str, Any],
    request_id: str,
    reason: str,
) -> AuditLog:
    """向审计表追加一条完整变更记录，不提交、更新或删除任何既有审计数据。"""

    if _uses_postgresql(session):
        audit_id = session.execute(
            text(
                """
                SELECT app.append_legacy_audit(
                    :actor_user_id, :event_type, :target_type, :target_id,
                    :before_summary, :after_summary, :request_id, :reason
                )
                """
            ).bindparams(
                bindparam("before_summary", type_=JSONB),
                bindparam("after_summary", type_=JSONB),
            ),
            {
                "actor_user_id": actor_user_id,
                "event_type": event_type,
                "target_type": target_type,
                "target_id": target_id,
                "before_summary": dict(before_summary),
                "after_summary": dict(after_summary),
                "request_id": request_id,
                "reason": reason,
            },
        ).scalar_one()
        return AuditLog(
            id=audit_id,
            actor_user_id=actor_user_id,
            event_type=event_type,
            target_type=target_type,
            target_id=target_id,
            before_summary=dict(before_summary),
            after_summary=dict(after_summary),
            request_id=request_id,
            reason=reason,
        )

    audit_log = AuditLog(
        actor_user_id=actor_user_id,
        event_type=event_type,
        target_type=target_type,
        target_id=target_id,
        before_summary=dict(before_summary),
        after_summary=dict(after_summary),
        request_id=request_id,
        reason=reason,
    )
    session.add(audit_log)
    session.flush()
    return audit_log


def append_business_audit_log(
    *,
    session: Session,
    context: CommandExecutionContext,
    record: BusinessAuditRecord,
) -> BusinessAuditReceipt:
    """按单一可信人工上下文追加新业务审计，仍由外层 UoW 提交。"""

    actor = context.actor
    assignment_id = None
    assignment_version = None
    guardian_relationship_id = None
    guardian_relationship_version = None
    if isinstance(actor, StaffActorContext):
        context_type = "assignment"
        assignment_id = actor.assignment_id
        assignment_version = actor.assignment_version
    elif isinstance(actor, GuardianActorContext):
        context_type = "guardian"
        guardian_relationship_id = actor.guardian_relationship_id
        guardian_relationship_version = actor.relationship_version
    else:
        raise TypeError("业务审计只接受服务端签发的人工操作者上下文。")

    idempotency_key_hash = sha256(context.idempotency_key.encode("utf-8")).hexdigest()
    values = {
        "actor_user_id": actor.account_user_id,
        "school_id": actor.school_id,
        "actor_person_id": actor.person_id,
        "actor_assignment_id": assignment_id,
        "actor_assignment_version": assignment_version,
        "actor_guardian_relationship_id": guardian_relationship_id,
        "actor_guardian_relationship_version": guardian_relationship_version,
        "actor_context_type": context_type,
        "idempotency_key_hash": idempotency_key_hash,
        "event_type": record.event_type,
        "target_type": record.target_type,
        "target_id": record.target_id,
        "before_summary": record.before_summary.model_dump(mode="json"),
        "after_summary": record.after_summary.model_dump(mode="json"),
        "request_id": context.request_id,
        "reason": record.reason_code,
    }
    if _uses_postgresql(session):
        audit_id = session.execute(
            text(
                """
                SELECT app.append_business_audit(
                    :actor_user_id, :school_id, :actor_person_id,
                    :actor_assignment_id, :actor_assignment_version,
                    :actor_guardian_relationship_id,
                    :actor_guardian_relationship_version, :actor_context_type,
                    :idempotency_key_hash, :event_type, :target_type,
                    :target_id, :before_summary, :after_summary,
                    :request_id, :reason_code
                )
                """
            ).bindparams(
                bindparam("before_summary", type_=JSONB),
                bindparam("after_summary", type_=JSONB),
            ),
            {**values, "reason_code": record.reason_code},
        ).scalar_one()
    else:
        audit_log = AuditLog(
            **{
                **values,
                "target_id": str(record.target_id),
                "request_id": str(context.request_id),
            }
        )
        session.add(audit_log)
        session.flush()
        audit_id = audit_log.id
    return BusinessAuditReceipt(
        audit_id=audit_id,
        idempotency_key_hash=idempotency_key_hash,
    )


class BoundTypedAuditWriter:
    """把 Session 与可信命令上下文绑定成 Handler 可用的窄审计写口。"""

    def __init__(self, session: Session, context: CommandExecutionContext) -> None:
        self._session = session
        self._context = context

    def append(self, record: BusinessAuditRecord) -> BusinessAuditReceipt:
        """追加一条类型化业务审计，不提交当前事务。"""

        return append_business_audit_log(
            session=self._session,
            context=self._context,
            record=record,
        )
