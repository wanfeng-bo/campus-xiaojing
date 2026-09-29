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
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ExcludeConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class SchoolPolicy(Base):
    """保存学校制度目录中的稳定身份。"""

    __tablename__ = "school_policies"
    __table_args__ = (
        UniqueConstraint("school_id", "id", name="uq_school_policies_school_id_id"),
        UniqueConstraint(
            "school_id",
            "source_key",
            name="uq_school_policies_school_source_key",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    school_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("schools.id", ondelete="RESTRICT"),
        nullable=False,
    )
    source_key: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class SchoolPolicyScope(Base):
    """保存制度对一个校区或一个组织的显式适用范围。"""

    __tablename__ = "school_policy_scopes"
    __table_args__ = (
        UniqueConstraint("school_id", "id", name="uq_school_policy_scopes_school_id_id"),
        ForeignKeyConstraint(
            ["school_id", "policy_id"],
            ["school_policies.school_id", "school_policies.id"],
            name="fk_school_policy_scopes_policy",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["school_id", "organization_unit_id"],
            ["organization_units.school_id", "organization_units.id"],
            name="fk_school_policy_scopes_organization",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["school_id", "campus_location_id"],
            ["campus_locations.school_id", "campus_locations.id"],
            name="fk_school_policy_scopes_campus",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["school_id", "source_id"],
            ["school_fact_sources.school_id", "school_fact_sources.id"],
            name="fk_school_policy_scopes_source",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["school_id", "import_batch_id"],
            ["master_data_import_batches.school_id", "master_data_import_batches.id"],
            name="fk_school_policy_scopes_import_batch",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["school_id", "education_target_id"],
            ["school_education_facts.school_id", "school_education_facts.id"],
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            '(education_target_id IS NULL AND num_nonnulls(organization_unit_id, '
            'campus_location_id) = 1 AND academic_year IS NULL) OR '
            '(education_target_id IS NOT NULL AND organization_unit_id IS NULL)',
            name="ck_school_policy_scopes_exactly_one_target",
        ),
        CheckConstraint(
            "valid_to IS NULL OR valid_to > valid_from",
            name="ck_school_policy_scopes_valid_interval",
        ),
        CheckConstraint("version >= 1", name="ck_school_policy_scopes_version_positive"),
        ExcludeConstraint(
            ("school_id", "="),
            ("policy_id", "="),
            ("organization_unit_id", "="),
            (
                text("tstzrange(valid_from, COALESCE(valid_to, 'infinity'::timestamptz), '[)')"),
                "&&",
            ),
            where=text("organization_unit_id IS NOT NULL"),
            using="gist",
            name="excl_school_policy_scopes_organization_period",
        ),
        ExcludeConstraint(
            ("school_id", "="),
            ("policy_id", "="),
            ("campus_location_id", "="),
            (
                text("tstzrange(valid_from, COALESCE(valid_to, 'infinity'::timestamptz), '[)')"),
                "&&",
            ),
            where=text("campus_location_id IS NOT NULL AND education_target_id IS NULL"),
            using="gist",
            name="excl_school_policy_scopes_campus_period",
        ),
        Index(
            "ix_school_policy_scopes_as_of",
            "school_id",
            "policy_id",
            "valid_from",
            "valid_to",
        ),
        Index(
            "ix_school_policy_scopes_school_source_key",
            "school_id",
            "source_key",
            "valid_from",
            "valid_to",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    school_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    source_key: Mapped[str] = mapped_column(String(255), nullable=False)
    policy_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    education_target_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    academic_year: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    organization_unit_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    campus_location_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    version: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1, server_default="1")
    source_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    import_batch_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class SchoolPolicyVersion(Base):
    """保存制度正文、分级和生效区间的不可物理删除版本。"""

    __tablename__ = "school_policy_versions"
    __table_args__ = (
        UniqueConstraint(
            "school_id",
            "id",
            name="uq_school_policy_versions_school_id_id",
        ),
        ForeignKeyConstraint(
            ["school_id", "policy_id"],
            ["school_policies.school_id", "school_policies.id"],
            name="fk_school_policy_versions_policy",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["school_id", "publishing_organization_unit_id"],
            ["organization_units.school_id", "organization_units.id"],
            name="fk_school_policy_versions_publishing_organization",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["school_id", "supersedes_policy_version_id"],
            ["school_policy_versions.school_id", "school_policy_versions.id"],
            name="fk_school_policy_versions_supersedes",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["school_id", "source_id"],
            ["school_fact_sources.school_id", "school_fact_sources.id"],
            name="fk_school_policy_versions_source",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["school_id", "import_batch_id"],
            ["master_data_import_batches.school_id", "master_data_import_batches.id"],
            name="fk_school_policy_versions_import_batch",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "classification IN ('public', 'internal', 'restricted')",
            name="ck_school_policy_versions_classification",
        ),
        CheckConstraint(
            "valid_to IS NULL OR valid_to > valid_from",
            name="ck_school_policy_versions_valid_interval",
        ),
        CheckConstraint(
            "effective_to IS NULL OR effective_to > effective_from",
            name="ck_school_policy_versions_business_interval",
        ),
        CheckConstraint("version >= 1", name="ck_school_policy_versions_version_positive"),
        ExcludeConstraint(
            ("school_id", "="),
            ("policy_id", "="),
            (
                text(
                    "tstzrange(GREATEST(valid_from, effective_from), "
                    "GREATEST(GREATEST(valid_from, effective_from), "
                    "COALESCE(LEAST(valid_to, effective_to, revoked_at), "
                    "'infinity'::timestamptz)), '[)')"
                ),
                "&&",
            ),
            using="gist",
            name="excl_school_policy_versions_period",
        ),
        Index(
            "ix_school_policy_versions_as_of",
            "school_id",
            "policy_id",
            "effective_from",
            "effective_to",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    school_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    policy_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    business_code: Mapped[str] = mapped_column(String(80), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    category_code: Mapped[str] = mapped_column(String(80), nullable=False)
    classification: Mapped[str] = mapped_column(String(20), nullable=False)
    publishing_organization_unit_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    effective_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    effective_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    valid_from: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    valid_to: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    content_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    version_note: Mapped[str | None] = mapped_column(String(500), nullable=True)
    supersedes_policy_version_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    version: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1, server_default="1")
    source_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    import_batch_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
