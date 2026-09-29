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
    SmallInteger,
    String,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ExcludeConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class School(Base):
    """保存学校稳定主体及当前最小显示配置。"""

    __tablename__ = "schools"
    __table_args__ = (
        UniqueConstraint("source_key", name="uq_schools_source_key"),
        CheckConstraint("status IN ('active', 'inactive')", name="ck_schools_status"),
        CheckConstraint("version >= 1", name="ck_schools_version_positive"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    source_key: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        server_default=text("('local:'::text || gen_random_uuid()::text)"),
    )
    school_code: Mapped[str] = mapped_column(String(80), nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    timezone_name: Mapped[str] = mapped_column(String(64), nullable=False)
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


class SchoolDatabaseAnchor(Base):
    """Declare the immutable one-database/one-school anchor for Alembic metadata."""

    __tablename__ = "school_database_anchor"
    __table_args__ = (
        CheckConstraint(
            "singleton_id = 1",
            name="ck_school_database_anchor_singleton",
        ),
        UniqueConstraint(
            "anchor_id",
            name="uq_school_database_anchor_anchor_id",
        ),
        UniqueConstraint(
            "school_id",
            name="uq_school_database_anchor_school_id",
        ),
    )

    singleton_id: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    anchor_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    school_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey(
            "schools.id",
            name="fk_school_database_anchor_school",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    cluster_system_identifier: Mapped[str] = mapped_column(String(32), nullable=False)
    database_oid: Mapped[int] = mapped_column(BigInteger, nullable=False)
    database_name: Mapped[str] = mapped_column(String(63), nullable=False)
    provisioned_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


class SchoolVersion(Base):
    """保存学校可见编号、名称和时区的半开有效期版本。"""

    __tablename__ = "school_versions"
    __table_args__ = (
        UniqueConstraint("school_id", "id", name="uq_school_versions_school_id_id"),
        ForeignKeyConstraint(
            ["school_id", "source_id"],
            ["school_fact_sources.school_id", "school_fact_sources.id"],
            name="fk_school_versions_school_source",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["school_id", "import_batch_id"],
            ["master_data_import_batches.school_id", "master_data_import_batches.id"],
            name="fk_school_versions_school_import_batch",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "lifecycle_status IN ('active', 'inactive')",
            name="ck_school_versions_lifecycle_status",
        ),
        CheckConstraint(
            "valid_to IS NULL OR valid_to > valid_from",
            name="ck_school_versions_valid_interval",
        ),
        CheckConstraint("version >= 1", name="ck_school_versions_version_positive"),
        ExcludeConstraint(
            ("school_id", "="),
            (
                text("tstzrange(valid_from, COALESCE(valid_to, 'infinity'::timestamptz), '[)')"),
                "&&",
            ),
            using="gist",
            name="excl_school_versions_period",
        ),
        Index(
            "ix_school_versions_as_of",
            "school_id",
            "valid_from",
            "valid_to",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    school_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("schools.id", ondelete="RESTRICT"),
        nullable=False,
    )
    business_code: Mapped[str] = mapped_column(String(80), nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    short_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    timezone_name: Mapped[str] = mapped_column(String(64), nullable=False)
    lifecycle_status: Mapped[str] = mapped_column(String(20), nullable=False)
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    version: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1, server_default="1")
    source_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    import_batch_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
