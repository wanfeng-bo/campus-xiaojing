from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, ExcludeConstraint
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql.elements import TextClause

from app.db.base import Base

_CONFIRMATION_STATUSES = "status IN ('draft', 'confirmed', 'rejected')"
_VALID_INTERVAL = "valid_to IS NULL OR valid_to > valid_from"


def _effective_range() -> TextClause:
    """构造 PostgreSQL 半开有效区间表达式。"""

    return text("tstzrange(valid_from, COALESCE(valid_to, 'infinity'::timestamptz), '[)')")


class CampusLocation(Base):
    """保存校园地点稳定树，不混入人员、责任或资产关系。"""

    __tablename__ = "campus_locations"
    __table_args__ = (
        UniqueConstraint("school_id", "id", name="uq_campus_locations_school_id_id"),
        UniqueConstraint(
            "school_id",
            "source_key",
            name="uq_campus_locations_school_source_key",
        ),
        ForeignKeyConstraint(
            ["school_id", "parent_location_id"],
            ["campus_locations.school_id", "campus_locations.id"],
            name="fk_campus_locations_school_parent",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "parent_location_id IS NULL OR parent_location_id <> id",
            name="ck_campus_locations_not_self_parent",
        ),
        CheckConstraint(
            "(kind = 'campus' AND parent_location_id IS NULL) OR "
            "(kind <> 'campus' AND parent_location_id IS NOT NULL)",
            name="ck_campus_locations_top_level_parent",
        ),
        CheckConstraint("status IN ('active', 'inactive')", name="ck_campus_locations_status"),
        CheckConstraint("version >= 1", name="ck_campus_locations_version_positive"),
        Index(
            "ix_campus_locations_current_parent",
            "school_id",
            "parent_location_id",
            "status",
            "id",
        ),
    )
    source_key: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        server_default=text("('local:'::text || gen_random_uuid()::text)"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    school_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("schools.id", ondelete="RESTRICT"),
        nullable=False,
    )
    parent_location_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
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


class CampusLocationVersion(Base):
    """保存地点结构、名称、别名和完整展示路径的确认版本。"""

    __tablename__ = "campus_location_versions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["school_id", "location_id"],
            ["campus_locations.school_id", "campus_locations.id"],
            name="fk_campus_location_versions_school_location",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["school_id", "parent_location_id"],
            ["campus_locations.school_id", "campus_locations.id"],
            name="fk_campus_location_versions_school_parent",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "parent_location_id IS NULL OR parent_location_id <> location_id",
            name="ck_campus_location_versions_not_self_parent",
        ),
        CheckConstraint(
            "(kind = 'campus' AND parent_location_id IS NULL) OR "
            "(kind <> 'campus' AND parent_location_id IS NOT NULL)",
            name="ck_campus_location_versions_top_level_parent",
        ),
        CheckConstraint(_CONFIRMATION_STATUSES, name="ck_campus_location_versions_status"),
        CheckConstraint(_VALID_INTERVAL, name="ck_campus_location_versions_valid_interval"),
        CheckConstraint(
            "version >= 1",
            name="ck_campus_location_versions_version_positive",
        ),
        ExcludeConstraint(
            ("school_id", "="),
            ("location_id", "="),
            (_effective_range(), "&&"),
            where=text("status = 'confirmed'"),
            using="gist",
            name="excl_campus_location_versions_confirmed_period",
        ),
        Index(
            "ix_campus_location_versions_as_of",
            "school_id",
            "location_id",
            "status",
            "valid_from",
            "valid_to",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    school_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    location_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    parent_location_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    business_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    aliases: Mapped[list[str]] = mapped_column(
        ARRAY(String(200)),
        nullable=False,
        default=list,
        server_default=text("ARRAY[]::varchar[]"),
    )
    display_path: Mapped[str] = mapped_column(String(1000), nullable=False)
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


class CampusLocationAlias(Base):
    """保存地点旧称、简称和俗称的独立有效期事实。"""

    __tablename__ = "campus_location_aliases"
    __table_args__ = (
        ForeignKeyConstraint(
            ["school_id", "location_id"],
            ["campus_locations.school_id", "campus_locations.id"],
            name="fk_campus_location_aliases_location",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["school_id", "source_id"],
            ["school_fact_sources.school_id", "school_fact_sources.id"],
            name="fk_campus_location_aliases_source",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["school_id", "import_batch_id"],
            ["master_data_import_batches.school_id", "master_data_import_batches.id"],
            name="fk_campus_location_aliases_import_batch",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "alias_kind IN ('former', 'common', 'short')",
            name="ck_campus_location_aliases_kind",
        ),
        CheckConstraint(_VALID_INTERVAL, name="ck_campus_location_aliases_valid_interval"),
        CheckConstraint("version >= 1", name="ck_campus_location_aliases_version_positive"),
        ExcludeConstraint(
            ("school_id", "="),
            ("location_id", "="),
            ("alias", "="),
            (_effective_range(), "&&"),
            using="gist",
            name="excl_campus_location_aliases_period",
        ),
        Index(
            "ix_campus_location_aliases_lookup",
            "school_id",
            "alias",
            "valid_from",
            "valid_to",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    school_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    location_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    alias: Mapped[str] = mapped_column(String(200), nullable=False)
    alias_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    version: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1, server_default="1")
    source_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    import_batch_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class DormRoomProfileVersion(Base):
    """保存宿舍房间的核定容量、住宿区域和可用性版本。"""

    __tablename__ = "dorm_room_profile_versions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["school_id", "room_location_id"],
            ["campus_locations.school_id", "campus_locations.id"],
            name="fk_dorm_room_profile_versions_room",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["school_id", "source_id"],
            ["school_fact_sources.school_id", "school_fact_sources.id"],
            name="fk_dorm_room_profile_versions_source",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["school_id", "import_batch_id"],
            ["master_data_import_batches.school_id", "master_data_import_batches.id"],
            name="fk_dorm_room_profile_versions_import_batch",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "authorized_bed_capacity >= 0",
            name="ck_dorm_room_profile_versions_capacity",
        ),
        CheckConstraint(_VALID_INTERVAL, name="ck_dorm_room_profile_versions_valid_interval"),
        CheckConstraint(
            "version >= 1",
            name="ck_dorm_room_profile_versions_version_positive",
        ),
        ExcludeConstraint(
            ("school_id", "="),
            ("room_location_id", "="),
            (_effective_range(), "&&"),
            using="gist",
            name="excl_dorm_room_profile_versions_period",
            where=text("superseded_at IS NULL"),
        ),
        Index(
            "ix_dorm_room_profile_versions_as_of",
            "school_id",
            "room_location_id",
            "valid_from",
            "valid_to",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    school_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    room_location_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    source_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    authorized_bed_capacity: Mapped[int] = mapped_column(BigInteger, nullable=False)
    accommodation_zone: Mapped[str] = mapped_column(String(80), nullable=False)
    is_available: Mapped[bool] = mapped_column(nullable=False)
    accessibility_tags: Mapped[list[str]] = mapped_column(
        ARRAY(String(80)),
        nullable=False,
        default=list,
        server_default=text("ARRAY[]::varchar[]"),
    )
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    version: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1, server_default="1")
    source_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    import_batch_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class DormBed(Base):
    """保存宿舍房间内床位的稳定身份，不存学生占用状态。"""

    __tablename__ = "dorm_beds"
    __table_args__ = (
        UniqueConstraint("school_id", "id", name="uq_dorm_beds_school_id_id"),
        UniqueConstraint(
            "school_id",
            "source_key",
            name="uq_dorm_beds_school_source_key",
        ),
        ForeignKeyConstraint(
            ["school_id", "room_location_id"],
            ["campus_locations.school_id", "campus_locations.id"],
            name="fk_dorm_beds_room",
            ondelete="RESTRICT",
        ),
        Index("ix_dorm_beds_room", "school_id", "room_location_id", "id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    school_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    room_location_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    source_key: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        server_default=text("('local:'::text || gen_random_uuid()::text)"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class DormBedVersion(Base):
    """保存床位号、显示名和可用性；入住只由学生关系表达。"""

    __tablename__ = "dorm_bed_versions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["school_id", "bed_id"],
            ["dorm_beds.school_id", "dorm_beds.id"],
            name="fk_dorm_bed_versions_bed",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["school_id", "source_id"],
            ["school_fact_sources.school_id", "school_fact_sources.id"],
            name="fk_dorm_bed_versions_source",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["school_id", "import_batch_id"],
            ["master_data_import_batches.school_id", "master_data_import_batches.id"],
            name="fk_dorm_bed_versions_import_batch",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "availability IN ('available', 'unavailable', 'inactive')",
            name="ck_dorm_bed_versions_availability",
        ),
        CheckConstraint(_VALID_INTERVAL, name="ck_dorm_bed_versions_valid_interval"),
        CheckConstraint("version >= 1", name="ck_dorm_bed_versions_version_positive"),
        ExcludeConstraint(
            ("school_id", "="),
            ("bed_id", "="),
            (_effective_range(), "&&"),
            using="gist",
            name="excl_dorm_bed_versions_period",
            where=text("superseded_at IS NULL"),
        ),
        Index(
            "ix_dorm_bed_versions_as_of",
            "school_id",
            "bed_id",
            "valid_from",
            "valid_to",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    school_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    bed_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    business_code: Mapped[str] = mapped_column(String(80), nullable=False)
    display_name: Mapped[str] = mapped_column(String(120), nullable=False)
    availability: Mapped[str] = mapped_column(String(20), nullable=False)
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    version: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1, server_default="1")
    source_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    import_batch_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class DormitoryBuildingProfileVersion(Base):
    """Version formal residential use of an existing location, without another hierarchy."""

    __tablename__ = "dormitory_building_profile_versions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["school_id", "location_id"],
            ["campus_locations.school_id", "campus_locations.id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["school_id", "source_id"],
            ["school_fact_sources.school_id", "school_fact_sources.id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["school_id", "import_batch_id"],
            ["master_data_import_batches.school_id", "master_data_import_batches.id"],
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "usage_kind IN ('building','residential_area')", name="ck_dormitory_profiles_usage"
        ),
        CheckConstraint(_VALID_INTERVAL, name="ck_dormitory_profiles_interval"),
        CheckConstraint("version >= 1", name="ck_dormitory_profiles_version"),
        ExcludeConstraint(
            ("school_id", "="),
            ("location_id", "="),
            (_effective_range(), "&&"),
            where=text("superseded_at IS NULL"),
            using="gist",
            name="excl_dormitory_profiles_period",
        ),
        Index("ix_dormitory_profiles_source", "school_id", "source_key", "version"),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    school_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    source_key: Mapped[str] = mapped_column(String(255), nullable=False)
    location_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    usage_kind: Mapped[str] = mapped_column(String(40), nullable=False)
    display_name: Mapped[str] = mapped_column(String(1000), nullable=False)
    description: Mapped[str | None] = mapped_column(String(1000))
    is_available: Mapped[bool] = mapped_column(nullable=False)
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1, server_default="1")
    source_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    import_batch_id: Mapped[UUID | None] = mapped_column(Uuid)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
