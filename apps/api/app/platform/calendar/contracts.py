from __future__ import annotations

from datetime import datetime
from typing import Protocol

from pydantic import AwareDatetime

from app.platform.contracts import (
    CalendarVersionId,
    DisplayName,
    FrozenContractModel,
    HistoricalSnapshotContract,
    SchoolId,
    TimezoneName,
)


class WorkCalendarSnapshot(HistoricalSnapshotContract):
    """保存已发布校历版本及其学校时区的严格历史快照。"""

    calendar_version_id: CalendarVersionId
    display_name: DisplayName
    timezone_name: TimezoneName


class CalendarNotPublished(FrozenContractModel):
    """表示指定学校和时点不存在已发布校历，不把未命中伪装成空快照。"""

    school_id: SchoolId
    as_of: AwareDatetime


class WorkCalendarReader(Protocol):
    """按数据库统一查询时点读取已发布校历，不提供激活写口。"""

    def get_published_snapshot(
        self,
        school_id: SchoolId,
        *,
        as_of: datetime | None = None,
    ) -> WorkCalendarSnapshot | CalendarNotPublished:
        """返回指定时点已发布校历；未发布时返回类型化结果。"""

        ...
