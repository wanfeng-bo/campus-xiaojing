from __future__ import annotations

from uuid import UUID

from sqlalchemy import insert, select, text
from sqlalchemy.orm import Session

from app.platform.contracts import AssignmentId, SchoolId
from app.platform.messaging.contracts import OutboxEvent
from app.platform.messaging.models import OutboxMessage


class SqlAlchemyStaffAssignmentOutboxProbe:
    """只核验固定任职 Outbox 行，不暴露追加能力。"""

    def __init__(self, session: Session) -> None:
        self._session = session

    def contains_staff_assignment_event(
        self,
        *,
        event_id: UUID,
        school_id: SchoolId,
        assignment_id: AssignmentId,
        assignment_version: int,
    ) -> bool:
        """逐字段核对固定 Outbox 行，不读取或信任任意 JSON。"""

        if self._session.get_bind().dialect.name == "postgresql":
            verified = self._session.scalar(
                text(
                    "SELECT app.verify_staff_duty_binding_outbox("
                    ":event_id, :school_id, :assignment_id, :assignment_version)"
                ),
                {
                    "event_id": event_id,
                    "school_id": school_id,
                    "assignment_id": assignment_id,
                    "assignment_version": assignment_version,
                },
            )
            return verified is True
        statement = select(OutboxMessage.id).where(
            OutboxMessage.id == event_id,
            OutboxMessage.school_id == school_id,
            OutboxMessage.event_type == "master_data.assignment.changed",
            OutboxMessage.aggregate_type == "staff_assignment",
            OutboxMessage.aggregate_id == assignment_id,
            OutboxMessage.aggregate_version == assignment_version,
        )
        return self._session.scalar(statement) is not None


class SqlAlchemyOutboxWriter(SqlAlchemyStaffAssignmentOutboxProbe):
    """在调用方事务内只追加安全 Outbox 行，不接触外部渠道。"""

    def append(self, event: OutboxEvent) -> None:
        # 使用显式 Core INSERT，避免 Repository 是否可独立使用取决于其他 ORM 模块的导入顺序。
        self._session.execute(
            insert(OutboxMessage).values(
                id=event.event_id,
                school_id=event.school_id,
                event_key=event.event_key,
                event_type=event.event_type,
                aggregate_type=event.aggregate_type,
                aggregate_id=event.aggregate_id,
                aggregate_version=event.aggregate_version,
                occurred_at=event.occurred_at,
                payload=event.safe_payload(),
            )
        )
        self._session.flush()
