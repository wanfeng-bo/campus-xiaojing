from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
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


class SchoolPerson(Base):
    """保存学校内自然人的稳定主体，不保存姓名。"""

    __tablename__ = "school_people"
    __table_args__ = (
        UniqueConstraint("school_id", "id", name="uq_school_people_school_id_id"),
        UniqueConstraint(
            "school_id",
            "source_key",
            name="uq_school_people_school_source_key",
        ),
        CheckConstraint(
            "status IN ('active', 'inactive', 'merged')",
            name="ck_school_people_status",
        ),
        CheckConstraint("version >= 1", name="ck_school_people_version_positive"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    school_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("schools.id", ondelete="RESTRICT"),
        nullable=False,
    )
    source_key: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        server_default=text("('local:'::text || gen_random_uuid()::text)"),
    )
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


class SchoolPersonName(Base):
    """保存自然人姓名在半开有效区间内的确认版本。"""

    __tablename__ = "school_person_names"
    __table_args__ = (
        ForeignKeyConstraint(
            ["school_id", "person_id"],
            ["school_people.school_id", "school_people.id"],
            name="fk_school_person_names_school_person",
            ondelete="RESTRICT",
        ),
        CheckConstraint(_CONFIRMATION_STATUSES, name="ck_school_person_names_status"),
        CheckConstraint(_VALID_INTERVAL, name="ck_school_person_names_valid_interval"),
        CheckConstraint("version >= 1", name="ck_school_person_names_version_positive"),
        ExcludeConstraint(
            ("school_id", "="),
            ("person_id", "="),
            (_effective_range(), "&&"),
            where=text("status = 'confirmed'"),
            using="gist",
            name="excl_school_person_names_confirmed_period",
        ),
        Index(
            "ix_school_person_names_as_of",
            "school_id",
            "person_id",
            "status",
            "valid_from",
            "valid_to",
        ),
        Index(
            "uq_school_person_names_external_reference",
            "school_id",
            "source_kind",
            "source_ref",
            unique=True,
            postgresql_where=text(
                "status = 'confirmed' AND source_kind = 'person_external_reference'"
            ),
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    school_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    person_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
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


class SchoolPersonProfileVersion(Base):
    """保存人员类别、可变校内编号和生命周期的历史版本。"""

    __tablename__ = "school_person_profile_versions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["school_id", "person_id"],
            ["school_people.school_id", "school_people.id"],
            name="fk_school_person_profile_versions_person",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["school_id", "source_id"],
            ["school_fact_sources.school_id", "school_fact_sources.id"],
            name="fk_school_person_profile_versions_source",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["school_id", "import_batch_id"],
            ["master_data_import_batches.school_id", "master_data_import_batches.id"],
            name="fk_school_person_profile_versions_import_batch",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "person_kind IN ('staff', 'student_contact', 'other')",
            name="ck_school_person_profile_versions_kind",
        ),
        CheckConstraint(
            "lifecycle_status IN ('active', 'inactive')",
            name="ck_school_person_profile_versions_lifecycle_status",
        ),
        CheckConstraint(_VALID_INTERVAL, name="ck_school_person_profile_versions_valid_interval"),
        CheckConstraint(
            "version >= 1",
            name="ck_school_person_profile_versions_version_positive",
        ),
        ExcludeConstraint(
            ("school_id", "="),
            ("person_id", "="),
            (_effective_range(), "&&"),
            using="gist",
            name="excl_school_person_profile_versions_period",
        ),
        Index(
            "ix_school_person_profile_versions_as_of",
            "school_id",
            "person_id",
            "valid_from",
            "valid_to",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    school_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    person_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    business_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    person_kind: Mapped[str] = mapped_column(String(40), nullable=False)
    lifecycle_status: Mapped[str] = mapped_column(String(20), nullable=False)
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    version: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1, server_default="1")
    source_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    import_batch_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class AccountPersonBinding(Base):
    """在学校范围内按有效期一对一绑定登录账号和自然人。"""

    __tablename__ = "account_person_bindings"
    __table_args__ = (
        UniqueConstraint(
            "school_id",
            "id",
            name="uq_account_person_bindings_school_id_id",
        ),
        UniqueConstraint(
            "school_id",
            "source_key",
            name="uq_account_person_bindings_school_source_key",
        ),
        ForeignKeyConstraint(
            ["school_id", "person_id"],
            ["school_people.school_id", "school_people.id"],
            name="fk_account_person_bindings_school_person",
            ondelete="RESTRICT",
        ),
        CheckConstraint(_CONFIRMATION_STATUSES, name="ck_account_person_bindings_status"),
        CheckConstraint(_VALID_INTERVAL, name="ck_account_person_bindings_valid_interval"),
        CheckConstraint("version >= 1", name="ck_account_person_bindings_version_positive"),
        Index(
            "ix_account_person_bindings_account_as_of",
            "school_id",
            "account_user_id",
            "status",
            "valid_from",
            "valid_to",
        ),
        Index(
            "ix_account_person_bindings_person_as_of",
            "school_id",
            "person_id",
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
    account_user_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    person_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
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


class AccountPersonBindingVersion(Base):
    """保存一项稳定账号绑定在半开技术区间内的完整内容切片。"""

    __tablename__ = "account_person_binding_versions"
    __table_args__ = (
        UniqueConstraint(
            "school_id",
            "binding_id",
            "target_version",
            name="uq_account_person_binding_versions_target_version",
        ),
        ForeignKeyConstraint(
            ["school_id", "binding_id"],
            ["account_person_bindings.school_id", "account_person_bindings.id"],
            name="fk_account_person_binding_versions_school_binding",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["school_id", "person_id"],
            ["school_people.school_id", "school_people.id"],
            name="fk_account_person_binding_versions_school_person",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["school_id", "source_id"],
            ["school_fact_sources.school_id", "school_fact_sources.id"],
            name="fk_account_person_binding_versions_school_source",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["school_id", "import_batch_id"],
            ["master_data_import_batches.school_id", "master_data_import_batches.id"],
            name="fk_account_person_binding_versions_school_batch",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            _CONFIRMATION_STATUSES,
            name="ck_account_person_binding_versions_status",
        ),
        CheckConstraint(
            "effective_to IS NULL OR effective_to > effective_from",
            name="ck_account_person_binding_versions_effective_interval",
        ),
        CheckConstraint(
            "target_version >= 1",
            name="ck_account_person_binding_versions_target_version_positive",
        ),
        ExcludeConstraint(
            ("school_id", "="),
            ("binding_id", "="),
            (
                text(
                    "tstzrange(effective_from, "
                    "COALESCE(effective_to, 'infinity'::timestamptz), '[)')"
                ),
                "&&",
            ),
            where=text("status = 'confirmed' AND revoked_at IS NULL"),
            using="gist",
            name="excl_account_person_binding_versions_confirmed_period",
        ),
        ExcludeConstraint(
            ("school_id", "="),
            ("account_user_id", "="),
            (
                text(
                    "tstzrange(effective_from, "
                    "COALESCE(effective_to, 'infinity'::timestamptz), '[)')"
                ),
                "&&",
            ),
            where=text("status = 'confirmed' AND revoked_at IS NULL"),
            using="gist",
            name="excl_account_person_binding_versions_account_period",
        ),
        ExcludeConstraint(
            ("school_id", "="),
            ("person_id", "="),
            (
                text(
                    "tstzrange(effective_from, "
                    "COALESCE(effective_to, 'infinity'::timestamptz), '[)')"
                ),
                "&&",
            ),
            where=text("status = 'confirmed' AND revoked_at IS NULL"),
            using="gist",
            name="excl_account_person_binding_versions_person_period",
        ),
        Index(
            "ix_account_person_binding_versions_as_of",
            "school_id",
            "binding_id",
            "status",
            "effective_from",
            "effective_to",
        ),
        Index(
            "ix_account_person_binding_versions_account_as_of",
            "school_id",
            "account_user_id",
            "status",
            "effective_from",
            "effective_to",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    school_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    binding_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    account_user_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    person_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    provider_code: Mapped[str] = mapped_column(String(80), nullable=False)
    provider_tenant_key: Mapped[str] = mapped_column(String(200), nullable=False)
    external_subject: Mapped[str] = mapped_column(String(255), nullable=False)
    effective_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    effective_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
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


class PersonContactPoint(Base):
    """保存自然人的联系方式事实，不把联系能力扩张为数据权限。"""

    __tablename__ = "person_contact_points"
    __table_args__ = (
        ForeignKeyConstraint(
            ["school_id", "person_id"],
            ["school_people.school_id", "school_people.id"],
            name="fk_person_contact_points_person",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["school_id", "source_id"],
            ["school_fact_sources.school_id", "school_fact_sources.id"],
            name="fk_person_contact_points_source",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["school_id", "import_batch_id"],
            ["master_data_import_batches.school_id", "master_data_import_batches.id"],
            name="fk_person_contact_points_import_batch",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "contact_kind IN ('mobile', 'phone', 'email')",
            name="ck_person_contact_points_kind",
        ),
        CheckConstraint(
            "(visibility_scope = 'private' AND audience_code IS NULL) OR "
            "(visibility_scope = 'school_authenticated' AND audience_code IS NULL) OR "
            "(visibility_scope = 'declared_audience' AND audience_code IS NOT NULL)",
            name="ck_person_contact_points_visibility",
        ),
        CheckConstraint(_VALID_INTERVAL, name="ck_person_contact_points_valid_interval"),
        CheckConstraint("version >= 1", name="ck_person_contact_points_version_positive"),
        ExcludeConstraint(
            ("school_id", "="),
            ("person_id", "="),
            ("contact_kind", "="),
            ("contact_value", "="),
            (_effective_range(), "&&"),
            using="gist",
            name="excl_person_contact_points_period",
        ),
        Index(
            "ix_person_contact_points_as_of",
            "school_id",
            "person_id",
            "valid_from",
            "valid_to",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    school_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    person_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    contact_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    contact_value: Mapped[str] = mapped_column(String(320), nullable=False)
    is_primary: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    visibility_scope: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
        default="school_authenticated",
        server_default="school_authenticated",
    )
    audience_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    consent_reference: Mapped[str | None] = mapped_column(String(255), nullable=True)
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    version: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1, server_default="1")
    source_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    import_batch_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class OfficialContactPoint(Base):
    """保存学校、组织或地点拥有的官方联系方式。"""

    __tablename__ = "official_contact_points"
    __table_args__ = (
        UniqueConstraint("school_id", "id", name="uq_official_contact_points_school_id"),
        ForeignKeyConstraint(
            ["school_id", "organization_unit_id"],
            ["organization_units.school_id", "organization_units.id"],
            name="fk_official_contact_points_organization",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["school_id", "location_id"],
            ["campus_locations.school_id", "campus_locations.id"],
            name="fk_official_contact_points_location",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["school_id", "source_id"],
            ["school_fact_sources.school_id", "school_fact_sources.id"],
            name="fk_official_contact_points_source",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["school_id", "import_batch_id"],
            ["master_data_import_batches.school_id", "master_data_import_batches.id"],
            name="fk_official_contact_points_import_batch",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "(owner_kind = 'school' AND organization_unit_id IS NULL AND location_id IS NULL) "
            "OR (owner_kind = 'organization' AND organization_unit_id IS NOT NULL "
            "AND location_id IS NULL) OR (owner_kind = 'location' "
            "AND organization_unit_id IS NULL AND location_id IS NOT NULL)",
            name="ck_official_contact_points_owner",
        ),
        CheckConstraint(
            "contact_kind IN ('phone', 'email')",
            name="ck_official_contact_points_kind",
        ),
        CheckConstraint(
            "lifecycle_status IN ('active', 'inactive')",
            name="ck_official_contact_points_lifecycle_status",
        ),
        CheckConstraint(
            "(visibility_scope IN ('public', 'school_authenticated') "
            "AND audience_code IS NULL) OR "
            "(visibility_scope = 'restricted' AND audience_code IS NOT NULL)",
            name="ck_official_contact_points_visibility",
        ),
        CheckConstraint(_VALID_INTERVAL, name="ck_official_contact_points_valid_interval"),
        CheckConstraint("version >= 1", name="ck_official_contact_points_version_positive"),
        Index(
            "ix_official_contact_points_as_of",
            "school_id",
            "owner_kind",
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
    owner_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    organization_unit_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    location_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    contact_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    contact_value: Mapped[str] = mapped_column(String(320), nullable=False)
    service_hours: Mapped[str | None] = mapped_column(String(500), nullable=True)
    lifecycle_status: Mapped[str] = mapped_column(String(20), nullable=False)
    visibility_scope: Mapped[str] = mapped_column(
        String(30), nullable=False, default="public", server_default="public"
    )
    audience_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    version: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1, server_default="1")
    source_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    import_batch_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
