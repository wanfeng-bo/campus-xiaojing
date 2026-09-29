from __future__ import annotations

from typing import Protocol
from uuid import UUID

from app.platform.contracts import AssignmentId, SchoolId
from app.platform.messaging.contracts import OutboxEvent


class OutboxWriter(Protocol):
    """定义业务事务内只追加安全 Outbox 事件的窄边界。"""

    def append(self, event: OutboxEvent) -> None:
        """加入事件并 flush；不得发送外部通知或提交事务。"""

        ...


class StaffAssignmentOutboxProbe(Protocol):
    """只核验一次固定任职事件是否已权威提交。"""

    def contains_staff_assignment_event(
        self,
        *,
        event_id: UUID,
        school_id: SchoolId,
        assignment_id: AssignmentId,
        assignment_version: int,
    ) -> bool:
        """核对固定事件 ID 与任职聚合引用，不返回消息载荷。"""

        ...


class StaffAssignmentOutboxTransactionPort(
    OutboxWriter,
    StaffAssignmentOutboxProbe,
    Protocol,
):
    """命令事务可追加，并可在幂等重放时核验固定事件。"""
