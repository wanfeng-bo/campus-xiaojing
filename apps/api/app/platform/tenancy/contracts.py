from __future__ import annotations

from datetime import datetime
from typing import Protocol

from app.platform.contracts import (
    DisplayName,
    HistoricalSnapshotContract,
    SchoolId,
    TimezoneName,
)


class SchoolSnapshot(HistoricalSnapshotContract):
    """保存某一查询时点使用的学校显示信息，不回查未来配置。"""

    display_name: DisplayName
    timezone_name: TimezoneName


class CurrentSchoolReader(Protocol):
    """只读取指定学校在统一查询时点的当前有效最小快照。"""

    def get_current_school(
        self,
        school_id: SchoolId,
        *,
        as_of: datetime,
    ) -> SchoolSnapshot | None:
        """返回指定时点的学校快照；无有效学校时返回 ``None``。"""

        ...
