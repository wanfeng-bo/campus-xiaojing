"""Single education authority, with retained registration and business-time history."""

from __future__ import annotations

from datetime import datetime
from typing import Any
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
from sqlalchemy.dialects.postgresql import JSONB, ExcludeConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.domains.campus.models import CampusLocation

from .education_contracts import EDUCATION_KINDS
from .models import Student, StudentSchoolRelationship


class EducationFact(Base):
    __tablename__ = "school_education_facts"
    __table_args__ = (
        UniqueConstraint("school_id", "id", name="uq_education_fact_school_id"),
        UniqueConstraint("school_id", "id", "kind", name="uq_education_fact_school_id_kind"),
        UniqueConstraint("school_id", "kind", "source_key", name="uq_education_fact_key"),
        ForeignKeyConstraint(["school_id"], ["schools.id"], ondelete="RESTRICT"),
        CheckConstraint("version >= 1", name="ck_education_fact_version"),
        CheckConstraint(
            "kind IN (" + ",".join(repr(k) for k in sorted(EDUCATION_KINDS)) + ")",
            name="ck_education_fact_kind",
        ),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    school_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    kind: Mapped[str] = mapped_column(String(80), nullable=False)
    source_key: Mapped[str] = mapped_column(String(255), nullable=False)
    version: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1)


class EducationVersion(Base):
    __tablename__ = "school_education_versions"
    __table_args__ = (
        UniqueConstraint("school_id", "id", name="uq_education_version_school_id"),
        ForeignKeyConstraint(
            ["school_id", "fact_id", "fact_kind"],
            [
                "school_education_facts.school_id",
                "school_education_facts.id",
                "school_education_facts.kind",
            ],
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
        CheckConstraint("revision >= 1", name="ck_education_version_revision"),
        CheckConstraint(
            "valid_to IS NULL OR valid_to > valid_from", name="ck_education_version_interval"
        ),
        CheckConstraint(
            "superseded_at IS NULL OR superseded_at >= recorded_at",
            name="ck_education_registration_interval",
        ),
        CheckConstraint("jsonb_typeof(payload) = 'object'", name="ck_education_payload_object"),
        CheckConstraint(
            "(cause_source_key IS NULL) = (cause_version IS NULL)", name="ck_education_cause_pair"
        ),
        ExcludeConstraint(
            ("school_id", "="),
            ("fact_id", "="),
            (text("tstzrange(valid_from, valid_to, '[)')"), "&&"),
            where=text("superseded_at IS NULL"),
            using="gist",
            name="excl_education_current_interval",
        ),
        *(
            ExcludeConstraint(
                ("school_id", "="),
                *((text("(payload->>'" + field + "')"), "=") for field in fields),
                (text("tstzrange(valid_from, valid_to, '[)')"), "&&"),
                where=text("superseded_at IS NULL AND fact_kind='" + kind + "'"),
                using="gist",
                name="excl_f5_" + suffix,
            )
            for suffix, kind, fields in (
                ("course_subject", "course_subject", ("course_source_key", "subject_source_key")),
                ("offering_member_source", "offering_member_source", ("offering_source_key",)),
                ("group", "teaching_group", ("organization_source_key",)),
                (
                    "group_member",
                    "teaching_group_member",
                    ("group_source_key", "student_source_key"),
                ),
                (
                    "assignment",
                    "teaching_assignment",
                    ("offering_source_key", "assignment_source_key", "teaching_role"),
                ),
            )
        ),
        *(
            ExcludeConstraint(
                ("school_id", "="),
                (text("(payload->>'organization_source_key')"), "="),
                (text("(payload->>'subject_source_key')"), "="),
                (text("(payload->>'" + field + "')"), "="),
                (text("(COALESCE(payload->>'term_source_key',''))"), "="),
                (text("tstzrange(valid_from, valid_to, '[)')"), "&&"),
                where=text(
                    "superseded_at IS NULL AND fact_kind='teaching_organization_scope' "
                    "AND payload->>'scope_basis'='" + basis + "'"
                ),
                using="gist",
                name="excl_f5_scope_" + basis,
            )
            for basis, field in (
                ("stage", "stage_source_key"),
                ("fixed_grade", "grade_source_key"),
                ("academic_grade", "grade_group_source_key"),
            )
        ),
        Index(
            "ix_education_version_current",
            "school_id",
            "fact_id",
            "valid_from",
            "valid_to",
            postgresql_where=text("superseded_at IS NULL"),
        ),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    school_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    fact_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    fact_kind: Mapped[str] = mapped_column(String(80), nullable=False)
    revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    source_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    import_batch_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    cause_source_key: Mapped[str | None] = mapped_column(String(255))
    cause_version: Mapped[int | None] = mapped_column(BigInteger)
    correction_reason: Mapped[str | None] = mapped_column(String(1000))


# A reference is resolved by the Writer, never accepted as an arbitrary caller ID.
EDUCATION_REFERENCE_TABLES = {
    "education_id": "school_education_facts",
    # Resolve same-domain FK metadata even when the first request is education-only.
    "student_id": Student.__tablename__,
    "assignment_id": "staff_assignments",
    "organization_id": "organization_units",
    "location_id": CampusLocation.__tablename__,
    "relationship_id": StudentSchoolRelationship.__tablename__,
}


class EducationReference(Base):
    __tablename__ = "school_education_references"
    __table_args__ = (
        ForeignKeyConstraint(
            ["school_id", "version_id"],
            ["school_education_versions.school_id", "school_education_versions.id"],
            ondelete="RESTRICT",
        ),
        *(
            ForeignKeyConstraint(
                ["school_id", col], [f"{table}.school_id", f"{table}.id"], ondelete="RESTRICT"
            )
            for col, table in EDUCATION_REFERENCE_TABLES.items()
        ),
        ForeignKeyConstraint(
            ["school_id", "education_id", "education_kind"],
            [
                "school_education_facts.school_id",
                "school_education_facts.id",
                "school_education_facts.kind",
            ],
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "(education_id IS NULL) = (education_kind IS NULL)",
            name="ck_education_reference_kind_pair",
        ),
        CheckConstraint(
            "num_nonnulls(education_id,student_id,assignment_id,organization_id,location_id,relationship_id)=1",
            name="ck_education_reference_one_target",
        ),
        CheckConstraint("ordinal >= 0", name="ck_education_reference_ordinal"),
    )
    school_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    version_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    field_name: Mapped[str] = mapped_column(String(80), primary_key=True)
    ordinal: Mapped[int] = mapped_column(BigInteger, primary_key=True, default=0)
    education_kind: Mapped[str | None] = mapped_column(String(80))
    education_id: Mapped[UUID | None] = mapped_column(Uuid)
    student_id: Mapped[UUID | None] = mapped_column(Uuid)
    assignment_id: Mapped[UUID | None] = mapped_column(Uuid)
    organization_id: Mapped[UUID | None] = mapped_column(Uuid)
    location_id: Mapped[UUID | None] = mapped_column(Uuid)
    relationship_id: Mapped[UUID | None] = mapped_column(Uuid)
