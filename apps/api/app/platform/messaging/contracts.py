from __future__ import annotations

from typing import Annotated
from uuid import UUID

from pydantic import AwareDatetime, StringConstraints

from app.platform.contracts import FrozenContractModel, PositiveVersion, SchoolId, SemanticCode

OutboxEventKey = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=255),
]


class OutboxEvent(FrozenContractModel):
    """限制 Outbox 只携带稳定标识、事件类型、版本和发生时间。"""

    event_id: UUID
    school_id: SchoolId
    event_key: OutboxEventKey
    event_type: SemanticCode
    aggregate_type: SemanticCode
    aggregate_id: UUID
    aggregate_version: PositiveVersion
    occurred_at: AwareDatetime

    def safe_payload(self) -> dict[str, str | int]:
        """生成字段集合固定、不会混入姓名或照片的 JSON 载荷。"""

        return {
            "event_type": self.event_type,
            "aggregate_type": self.aggregate_type,
            "aggregate_id": str(self.aggregate_id),
            "aggregate_version": self.aggregate_version,
            "occurred_at": self.occurred_at.isoformat(),
        }
