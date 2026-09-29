from __future__ import annotations

from datetime import datetime
from typing import Protocol
from uuid import UUID

from app.platform.contracts import SchoolId
from app.platform.policies.contracts import PolicyVersionSnapshot


class PolicyVersionReader(Protocol):
    """按学校、制度稳定 ID 和时点读取生效版本。"""

    def get_effective_snapshot(
        self,
        school_id: SchoolId,
        policy_id: UUID,
        *,
        as_of: datetime,
    ) -> PolicyVersionSnapshot | None:
        """返回指定时点的制度版本；未命中时返回 ``None``。"""

        ...
