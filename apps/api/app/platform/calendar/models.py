from __future__ import annotations

from datetime import date, datetime, time
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
    Time,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ExcludeConstraint
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql.elements import TextClause

from app.db.base import Base

_VALID_INTERVAL = "valid_to IS NULL OR valid_to > valid_from"


def _effective_range() -> TextClause:
    """构造 PostgreSQL 半开发布有效区间表达式。"""

    return text("tstzrange(valid_from, COALESCE(valid_to, 'infinity'::timestamptz), '[)')")


class SchoolCalendarVersion(Base):
    """保存学校日历的草稿、发布或废弃版本。"""

    __tablename__ = "school_calendar_versions"
    __table_args__ = (
        UniqueConstraint("school_id", "id", name="uq_school_calendar_versions_school_id_id"),
        CheckConstraint(
            "status IN ('draft', 'published', 'superseded')",
            name="ck_school_calendar_versions_status",
        ),
        CheckConstraint(_VALID_INTERVAL, name="ck_school_calendar_versions_valid_interval"),
        CheckConstraint(
            "version >= 1",
            name="ck_school_calendar_versions_version_positive",
        ),
        ExcludeConstraint(
            ("school_id", "="),
            (_effective_range(), "&&"),
            where=text("status = 'published'"),
            using="gist",
            name="excl_school_calendar_versions_published_period",
        ),
        Index(
            "ix_school_calendar_versions_published_as_of",
            "school_id",
            "status",
            "valid_from",
            "valid_to",
        ),
        {"info": {"school_facts_authority": "legacy_disabled"}},
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    school_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("schools.id", ondelete="RESTRICT"),
        nullable=False,
    )
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    timezone_name: Mapped[str] = mapped_column(String(64), nullable=False)
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    source_kind: Mapped[str] = mapped_column(String(40), nullable=False)
    source_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    version: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1, server_default="1")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class SchoolCalendarDay(Base):
    """保存发布版本内某个实际日期的工作或休息事实。"""

    __tablename__ = "school_calendar_days"
    __table_args__ = (
        ForeignKeyConstraint(
            ["school_id", "calendar_version_id"],
            ["school_calendar_versions.school_id", "school_calendar_versions.id"],
            name="fk_school_calendar_days_school_version",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "school_id",
            "calendar_version_id",
            "local_date",
            name="uq_school_calendar_days_version_date",
        ),
        CheckConstraint(
            "day_kind IN ('working', 'rest')",
            name="ck_school_calendar_days_kind",
        ),
        CheckConstraint(
            "(actual_start_time IS NULL AND actual_end_time IS NULL) OR "
            "(actual_start_time IS NOT NULL AND actual_end_time IS NOT NULL "
            "AND actual_end_time > actual_start_time)",
            name="ck_school_calendar_days_actual_time",
        ),
        CheckConstraint("version >= 1", name="ck_school_calendar_days_version_positive"),
        Index(
            "ix_school_calendar_days_version_date",
            "school_id",
            "calendar_version_id",
            "local_date",
        ),
        {"info": {"school_facts_authority": "legacy_disabled"}},
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    school_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    calendar_version_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    local_date: Mapped[date] = mapped_column(Date, nullable=False)
    day_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    actual_start_time: Mapped[time | None] = mapped_column(Time, nullable=True)
    actual_end_time: Mapped[time | None] = mapped_column(Time, nullable=True)
    source_kind: Mapped[str] = mapped_column(String(40), nullable=False)
    note: Mapped[str | None] = mapped_column(String(500), nullable=True)
    version: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1, server_default="1")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
