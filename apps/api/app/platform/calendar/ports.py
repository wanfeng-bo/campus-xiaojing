from __future__ import annotations

from typing import Protocol
from uuid import UUID

from app.platform.calendar.contracts import WorkCalendarReader
from app.platform.calendar.models import SchoolCalendarDay, SchoolCalendarVersion
from app.platform.contracts import SchoolId


class CalendarRepository(WorkCalendarReader, Protocol):
    """定义学校日历版本、具体日期和查询时读取边界。"""

    def add_version(self, version: SchoolCalendarVersion) -> None:
        """加入校历版本并触发发布区间约束。"""

        ...

    def add_day(self, day: SchoolCalendarDay) -> None:
        """加入版本内具体日期事实并 flush。"""

        ...

    def lock_versions_for_publish(
        self,
        school_id: SchoolId,
        draft_version_id: UUID,
    ) -> tuple[SchoolCalendarVersion, ...]:
        """按 UUID 固定顺序锁定目标草稿和该校全部已发布版本。"""

        ...

    def lock_days_for_publish(
        self,
        school_id: SchoolId,
        calendar_version_id: UUID,
    ) -> tuple[SchoolCalendarDay, ...]:
        """按日期和 UUID 固定顺序锁定待发布版本的全部日期事实。"""

        ...

    def save_version(self, version: SchoolCalendarVersion) -> None:
        """保存调用方已经校验的校历版本变化并 flush，不提交。"""

        ...
