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
from sqlalchemy.dialects.postgresql import ExcludeConstraint
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql.elements import TextClause

from app.db.base import Base

_CONFIRMATION_STATUSES = "status IN ('draft', 'confirmed', 'rejected')"
_VALID_INTERVAL = "valid_to IS NULL OR valid_to > valid_from"


def _effective_range() -> TextClause:
    return text("tstzrange(valid_from, COALESCE(valid_to, 'infinity'::timestamptz), '[)')")


class OrganizationUnit(Base):
    """保存学校组织单位的稳定树结构，不保存可变名称。"""

    __tablename__ = "organization_units"
    __table_args__ = (
        UniqueConstraint("school_id", "id", name="uq_organization_units_school_id_id"),
        UniqueConstraint(
            "school_id",
            "source_key",
            name="uq_organization_units_school_source_key",
        ),
        ForeignKeyConstraint(
            ["school_id", "parent_organization_unit_id"],
            ["organization_units.school_id", "organization_units.id"],
            name="fk_organization_units_school_parent",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "status IN ('active', 'inactive')",
            name="ck_organization_units_status",
        ),
        CheckConstraint("version >= 1", name="ck_organization_units_version_positive"),
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
    parent_organization_unit_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
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


class OrganizationUnitVersion(Base):
    """保存组织父级、名称、简称和展示路径的有效期版本。"""

    __tablename__ = "organization_unit_versions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["school_id", "organization_unit_id"],
            ["organization_units.school_id", "organization_units.id"],
            name="fk_organization_unit_versions_school_unit",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["school_id", "parent_organization_unit_id"],
            ["organization_units.school_id", "organization_units.id"],
            name="fk_organization_unit_versions_school_parent",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "parent_organization_unit_id IS NULL "
            "OR parent_organization_unit_id <> organization_unit_id",
            name="ck_organization_unit_versions_not_self_parent",
        ),
        CheckConstraint(_CONFIRMATION_STATUSES, name="ck_organization_unit_versions_status"),
        CheckConstraint(_VALID_INTERVAL, name="ck_organization_unit_versions_valid_interval"),
        CheckConstraint("version >= 1", name="ck_organization_unit_versions_version_positive"),
        ExcludeConstraint(
            ("school_id", "="),
            ("organization_unit_id", "="),
            (_effective_range(), "&&"),
            where=text("status = 'confirmed'"),
            using="gist",
            name="excl_organization_unit_versions_confirmed_period",
        ),
        Index(
            "ix_organization_unit_versions_as_of",
            "school_id",
            "organization_unit_id",
            "status",
            "valid_from",
            "valid_to",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    school_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    organization_unit_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    parent_organization_unit_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    business_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    short_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
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


class OrganizationCampusRelationship(Base):
    """保存组织覆盖一个校区的显式有效期事实；无记录不表示全校。"""

    __tablename__ = "organization_campus_relationships"
    __table_args__ = (
        UniqueConstraint(
            "school_id",
            "id",
            name="uq_organization_campus_relationships_school_id_id",
        ),
        ForeignKeyConstraint(
            ["school_id", "organization_unit_id"],
            ["organization_units.school_id", "organization_units.id"],
            name="fk_organization_campus_relationships_organization",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["school_id", "campus_location_id"],
            ["campus_locations.school_id", "campus_locations.id"],
            name="fk_organization_campus_relationships_campus",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["school_id", "source_id"],
            ["school_fact_sources.school_id", "school_fact_sources.id"],
            name="fk_organization_campus_relationships_source",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["school_id", "import_batch_id"],
            ["master_data_import_batches.school_id", "master_data_import_batches.id"],
            name="fk_organization_campus_relationships_import_batch",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            _VALID_INTERVAL,
            name="ck_organization_campus_relationships_valid_interval",
        ),
        CheckConstraint(
            "version >= 1",
            name="ck_organization_campus_relationships_version_positive",
        ),
        ExcludeConstraint(
            ("school_id", "="),
            ("organization_unit_id", "="),
            ("campus_location_id", "="),
            (_effective_range(), "&&"),
            using="gist",
            name="excl_organization_campus_relationships_period",
        ),
        Index(
            "ix_organization_campus_relationships_as_of",
            "school_id",
            "organization_unit_id",
            "campus_location_id",
            "valid_from",
            "valid_to",
        ),
        Index(
            "ix_organization_campus_relationships_school_source_key",
            "school_id",
            "source_key",
            "valid_from",
            "valid_to",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    school_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    source_key: Mapped[str] = mapped_column(String(255), nullable=False)
    organization_unit_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    campus_location_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    version: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1, server_default="1")
    source_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    import_batch_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class StaffAssignment(Base):
    """保存学校人员在组织内的一项正式任职。"""

    __tablename__ = "staff_assignments"
    __table_args__ = (
        UniqueConstraint("school_id", "id", name="uq_staff_assignments_school_id_id"),
        UniqueConstraint(
            "school_id",
            "source_key",
            name="uq_staff_assignments_school_source_key",
        ),
        ForeignKeyConstraint(
            ["school_id", "person_id"],
            ["school_people.school_id", "school_people.id"],
            name="fk_staff_assignments_school_person",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["school_id", "organization_unit_id"],
            ["organization_units.school_id", "organization_units.id"],
            name="fk_staff_assignments_school_unit",
            ondelete="RESTRICT",
        ),
        CheckConstraint(_CONFIRMATION_STATUSES, name="ck_staff_assignments_status"),
        CheckConstraint(
            "assignment_kind IN ('substantive', 'part_time', 'acting')",
            name="ck_staff_assignments_assignment_kind",
        ),
        CheckConstraint(_VALID_INTERVAL, name="ck_staff_assignments_valid_interval"),
        CheckConstraint("version >= 1", name="ck_staff_assignments_version_positive"),
        Index(
            "ix_staff_assignments_current_person",
            "school_id",
            "person_id",
            "status",
            "valid_from",
            "valid_to",
        ),
        Index(
            "ix_staff_assignments_current_unit_role",
            "school_id",
            "organization_unit_id",
            "semantic_role_code",
            "status",
            "valid_from",
            "valid_to",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    school_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    source_key: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        server_default=text("('local:'::text || gen_random_uuid()::text)"),
    )
    person_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    organization_unit_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    position_title: Mapped[str] = mapped_column(String(200), nullable=False)
    semantic_role_code: Mapped[str] = mapped_column(String(80), nullable=False)
    assignment_kind: Mapped[str] = mapped_column(
        String(20), nullable=False, default="substantive", server_default="substantive"
    )
    appointment_source: Mapped[str] = mapped_column(String(40), nullable=False)
    approval_reference: Mapped[str] = mapped_column(String(255), nullable=False)
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
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


class StaffAssignmentVersion(Base):
    """保存一项稳定任职在半开技术区间内的完整内容切片。"""

    __tablename__ = "staff_assignment_versions"
    __table_args__ = (
        UniqueConstraint(
            "school_id",
            "assignment_id",
            "target_version",
            name="uq_staff_assignment_versions_target_version",
        ),
        ForeignKeyConstraint(
            ["school_id", "assignment_id"],
            ["staff_assignments.school_id", "staff_assignments.id"],
            name="fk_staff_assignment_versions_school_assignment",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["school_id", "person_id"],
            ["school_people.school_id", "school_people.id"],
            name="fk_staff_assignment_versions_school_person",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["school_id", "organization_unit_id"],
            ["organization_units.school_id", "organization_units.id"],
            name="fk_staff_assignment_versions_school_unit",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["school_id", "source_id"],
            ["school_fact_sources.school_id", "school_fact_sources.id"],
            name="fk_staff_assignment_versions_school_source",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["school_id", "import_batch_id"],
            ["master_data_import_batches.school_id", "master_data_import_batches.id"],
            name="fk_staff_assignment_versions_school_batch",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "assignment_kind IN ('substantive', 'part_time', 'acting')",
            name="ck_staff_assignment_versions_assignment_kind",
        ),
        CheckConstraint(_CONFIRMATION_STATUSES, name="ck_staff_assignment_versions_status"),
        CheckConstraint(
            "effective_to IS NULL OR effective_to > effective_from",
            name="ck_staff_assignment_versions_effective_interval",
        ),
        CheckConstraint(
            "planned_valid_to IS NULL OR planned_valid_to > effective_from",
            name="ck_staff_assignment_versions_planned_interval",
        ),
        CheckConstraint(
            "target_version >= 1",
            name="ck_staff_assignment_versions_target_version_positive",
        ),
        ExcludeConstraint(
            ("school_id", "="),
            ("assignment_id", "="),
            (
                text(
                    "tstzrange(effective_from, "
                    "COALESCE(effective_to, 'infinity'::timestamptz), '[)')"
                ),
                "&&",
            ),
            where=text("status = 'confirmed' AND revoked_at IS NULL"),
            using="gist",
            name="excl_staff_assignment_versions_confirmed_period",
        ),
        ExcludeConstraint(
            ("school_id", "="),
            ("person_id", "="),
            ("organization_unit_id", "="),
            ("semantic_role_code", "="),
            (
                text(
                    "tstzrange(effective_from, "
                    "LEAST(COALESCE(effective_to, 'infinity'::timestamptz), "
                    "COALESCE(planned_valid_to, 'infinity'::timestamptz)), '[)')"
                ),
                "&&",
            ),
            where=text("status = 'confirmed' AND revoked_at IS NULL"),
            using="gist",
            name="excl_staff_assignment_versions_confirmed_slot_period",
        ),
        Index(
            "ix_staff_assignment_versions_as_of",
            "school_id",
            "assignment_id",
            "status",
            "effective_from",
            "effective_to",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    school_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    assignment_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    person_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    organization_unit_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    position_title: Mapped[str] = mapped_column(String(200), nullable=False)
    semantic_role_code: Mapped[str] = mapped_column(String(80), nullable=False)
    assignment_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    appointment_source: Mapped[str] = mapped_column(String(40), nullable=False)
    approval_reference: Mapped[str] = mapped_column(String(255), nullable=False)
    effective_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    effective_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    planned_valid_to: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    target_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    source_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    import_batch_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
