from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    String,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ExcludeConstraint
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql.elements import TextClause

from app.db.base import Base

_CONFIRMATION_STATUSES = "status IN ('draft', 'confirmed', 'rejected')"
_VALID_INTERVAL = "valid_to IS NULL OR valid_to > valid_from"


def _effective_range() -> TextClause:
    """返回所有责任有效期排斥约束共用的半开区间表达式。"""

    return text("tstzrange(valid_from, COALESCE(valid_to, 'infinity'::timestamptz), '[)')")


class ResponsibilityType(Base):
    """定义一所学校发布的稳定语义职责类型。"""

    __tablename__ = "responsibility_types"
    __table_args__ = (
        UniqueConstraint("school_id", "id", name="uq_responsibility_types_school_id_id"),
        UniqueConstraint("school_id", "code", name="uq_responsibility_types_school_code"),
        ForeignKeyConstraint(
            ["school_id"],
            ["schools.id"],
            name="fk_responsibility_types_school",
            ondelete="RESTRICT",
        ),
        CheckConstraint("status IN ('active', 'inactive')", name="ck_responsibility_types_status"),
        CheckConstraint("version >= 1", name="ck_responsibility_types_version_positive"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    school_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    code: Mapped[str] = mapped_column(String(80), nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    version: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1, server_default="1")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class ResponsibilityGroup(Base):
    """保存报修等业务可引用的稳定机构责任组。"""

    __tablename__ = "responsibility_groups"
    __table_args__ = (
        UniqueConstraint("school_id", "id", name="uq_responsibility_groups_school_id_id"),
        UniqueConstraint("school_id", "code", name="uq_responsibility_groups_school_code"),
        ForeignKeyConstraint(
            ["school_id"],
            ["schools.id"],
            name="fk_responsibility_groups_school",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["school_id", "manager_assignment_id"],
            ["staff_assignments.school_id", "staff_assignments.id"],
            name="fk_responsibility_groups_manager_assignment",
            ondelete="RESTRICT",
        ),
        CheckConstraint("status IN ('active', 'inactive')", name="ck_responsibility_groups_status"),
        CheckConstraint(
            "manager_assignment_version >= 1",
            name="ck_responsibility_groups_manager_assignment_version_positive",
        ),
        CheckConstraint("version >= 1", name="ck_responsibility_groups_version_positive"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    school_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    code: Mapped[str] = mapped_column(String(80), nullable=False)
    manager_assignment_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    manager_assignment_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    version: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1, server_default="1")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class ResponsibilityGroupVersion(Base):
    """追加保存责任组显示名称的历史有效版本。"""

    __tablename__ = "responsibility_group_versions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["school_id", "responsibility_group_id"],
            ["responsibility_groups.school_id", "responsibility_groups.id"],
            name="fk_responsibility_group_versions_group",
            ondelete="RESTRICT",
        ),
        CheckConstraint(_CONFIRMATION_STATUSES, name="ck_responsibility_group_versions_status"),
        CheckConstraint(_VALID_INTERVAL, name="ck_responsibility_group_versions_valid_interval"),
        CheckConstraint("version >= 1", name="ck_responsibility_group_versions_version_positive"),
        ExcludeConstraint(
            ("school_id", "="),
            ("responsibility_group_id", "="),
            (_effective_range(), "&&"),
            where=text("status = 'confirmed'"),
            using="gist",
            name="excl_responsibility_group_versions_confirmed_period",
        ),
        Index(
            "ix_responsibility_group_versions_current",
            "school_id",
            "responsibility_group_id",
            "status",
            "valid_from",
            "valid_to",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    school_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    responsibility_group_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    version: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1, server_default="1")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


class ResponsibilityGroupMembership(Base):
    """把责任组成员绑定到正式任职，而不是旧技术角色。"""

    __tablename__ = "responsibility_group_memberships"
    __table_args__ = (
        ForeignKeyConstraint(
            ["school_id", "responsibility_group_id"],
            ["responsibility_groups.school_id", "responsibility_groups.id"],
            name="fk_responsibility_group_memberships_group",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["school_id", "assignment_id"],
            ["staff_assignments.school_id", "staff_assignments.id"],
            name="fk_responsibility_group_memberships_assignment",
            ondelete="RESTRICT",
        ),
        CheckConstraint(_CONFIRMATION_STATUSES, name="ck_responsibility_group_memberships_status"),
        CheckConstraint(_VALID_INTERVAL, name="ck_responsibility_group_memberships_valid_interval"),
        CheckConstraint(
            "version >= 1",
            name="ck_responsibility_group_memberships_version_positive",
        ),
        ExcludeConstraint(
            ("school_id", "="),
            ("responsibility_group_id", "="),
            ("assignment_id", "="),
            (_effective_range(), "&&"),
            where=text("status = 'confirmed'"),
            using="gist",
            name="excl_responsibility_group_memberships_confirmed_period",
        ),
        Index(
            "ix_responsibility_group_memberships_current_group",
            "school_id",
            "responsibility_group_id",
            "status",
            "valid_from",
            "valid_to",
        ),
        Index(
            "ix_responsibility_group_memberships_current_assignment",
            "school_id",
            "assignment_id",
            "status",
            "valid_from",
            "valid_to",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    school_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    responsibility_group_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    assignment_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    version: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1, server_default="1")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


class RepairResponsibilityGrant(Base):
    """保存报修路由所需的有限责任授权，不充当万能关系表。"""

    __tablename__ = "repair_responsibility_grants"
    __table_args__ = (
        ForeignKeyConstraint(
            ["school_id", "responsibility_type_id"],
            ["responsibility_types.school_id", "responsibility_types.id"],
            name="fk_repair_responsibility_grants_type",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["school_id", "responsibility_group_id"],
            ["responsibility_groups.school_id", "responsibility_groups.id"],
            name="fk_repair_responsibility_grants_group",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["school_id", "location_id"],
            ["campus_locations.school_id", "campus_locations.id"],
            name="fk_repair_responsibility_grants_location",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "route_mode IN ('direct', 'group_claim', 'manager_dispatch', 'create_and_handle')",
            name="ck_repair_responsibility_grants_route_mode",
        ),
        CheckConstraint(_CONFIRMATION_STATUSES, name="ck_repair_responsibility_grants_status"),
        CheckConstraint(_VALID_INTERVAL, name="ck_repair_responsibility_grants_valid_interval"),
        CheckConstraint("version >= 1", name="ck_repair_responsibility_grants_version_positive"),
        ExcludeConstraint(
            ("school_id", "="),
            ("responsibility_type_id", "="),
            (text("COALESCE(location_id, '00000000-0000-0000-0000-000000000000'::uuid)"), "="),
            (text("COALESCE(repair_subtype, '')"), "="),
            (text("COALESCE(severity, '')"), "="),
            (_effective_range(), "&&"),
            where=text("status = 'confirmed'"),
            using="gist",
            name="excl_repair_responsibility_grants_confirmed_scope_period",
        ),
        Index(
            "ix_repair_responsibility_grants_current_scope",
            "school_id",
            "responsibility_type_id",
            "status",
            "location_id",
            "repair_subtype",
            "severity",
            "valid_from",
            "valid_to",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    school_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    responsibility_type_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    responsibility_group_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    location_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    repair_subtype: Mapped[str | None] = mapped_column(String(80), nullable=True)
    severity: Mapped[str | None] = mapped_column(String(80), nullable=True)
    route_mode: Mapped[str] = mapped_column(String(30), nullable=False)
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    version: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1, server_default="1")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
