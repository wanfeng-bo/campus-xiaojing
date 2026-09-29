from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.platform.calendar.contracts import CalendarNotPublished, WorkCalendarSnapshot
from app.platform.calendar.models import SchoolCalendarDay, SchoolCalendarVersion
from app.platform.contracts import CalendarVersionId, EffectiveInterval, SchoolId


class SqlAlchemyWorkCalendarRepository:
    """在调用方事务内维护校历，并按数据库查询时点解析发布版本。"""

    def __init__(self, session: Session) -> None:
        """绑定调用方 Session，不接管事务或会话生命周期。"""

        self._session = session

    def add_version(self, version: SchoolCalendarVersion) -> None:
        """加入校历版本并 flush，使发布区间冲突立即暴露。"""

        self._session.add(version)
        self._session.flush()

    def add_day(self, day: SchoolCalendarDay) -> None:
        """加入版本内具体日期事实并 flush。"""

        self._session.add(day)
        self._session.flush()

    def lock_versions_for_publish(
        self,
        school_id: SchoolId,
        draft_version_id: UUID,
    ) -> tuple[SchoolCalendarVersion, ...]:
        """按 UUID 固定顺序锁定目标草稿与全部已发布版本。"""

        statement = (
            select(SchoolCalendarVersion)
            .where(
                SchoolCalendarVersion.school_id == school_id,
                or_(
                    SchoolCalendarVersion.id == draft_version_id,
                    SchoolCalendarVersion.status == "published",
                ),
            )
            .order_by(SchoolCalendarVersion.id)
            .with_for_update()
        )
        return tuple(self._session.scalars(statement).all())

    def lock_days_for_publish(
        self,
        school_id: SchoolId,
        calendar_version_id: UUID,
    ) -> tuple[SchoolCalendarDay, ...]:
        """按日期和 UUID 固定顺序锁定草稿日期，供发布前逐字段重验。"""

        statement = (
            select(SchoolCalendarDay)
            .where(
                SchoolCalendarDay.school_id == school_id,
                SchoolCalendarDay.calendar_version_id == calendar_version_id,
            )
            .order_by(SchoolCalendarDay.local_date, SchoolCalendarDay.id)
            .with_for_update()
        )
        return tuple(self._session.scalars(statement).all())

    def save_version(self, version: SchoolCalendarVersion) -> None:
        """flush 已由应用层校验的发布或旧版本结束变化，不提交。"""

        self._session.add(version)
        self._session.flush()

    def get_published_snapshot(
        self,
        school_id: SchoolId,
        *,
        as_of: datetime | None = None,
    ) -> WorkCalendarSnapshot | CalendarNotPublished:
        """仅按 published 和半开区间读取，不依赖后台激活任务。"""

        database_now = self._session.scalar(select(func.clock_timestamp()))
        if not isinstance(database_now, datetime):
            raise RuntimeError("数据库没有返回可用的权威查询时点。")
        query_time = database_now if as_of is None else as_of
        statement = select(SchoolCalendarVersion).where(
            SchoolCalendarVersion.school_id == school_id,
            SchoolCalendarVersion.status == "published",
            SchoolCalendarVersion.valid_from <= query_time,
            or_(
                SchoolCalendarVersion.valid_to.is_(None),
                SchoolCalendarVersion.valid_to > query_time,
            ),
        )
        version = self._session.scalar(statement)
        if version is None:
            return CalendarNotPublished(school_id=school_id, as_of=query_time)
        return WorkCalendarSnapshot(
            school_id=SchoolId(version.school_id),
            calendar_version_id=CalendarVersionId(version.id),
            display_name=version.display_name,
            timezone_name=version.timezone_name,
            source_version=version.version,
            captured_at=database_now,
            effective_interval=EffectiveInterval(
                valid_from=version.valid_from,
                valid_to=version.valid_to,
            ),
            snapshot_basis="calendar.published_version_as_of",
        )
