"""F2 School Facts owned temporal records; maintained only by the unique Writer."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    String,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, ExcludeConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.domains.students.education_contracts import EDUCATION_KINDS
from app.platform.school_facts.contracts import F7_FAMILY_QUALITY_TARGET_KINDS


class _TemporalFact:
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    school_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    source_key: Mapped[str] = mapped_column(String(255), nullable=False)
    version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    import_batch_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    basis_reference: Mapped[str] = mapped_column(String(1000), nullable=False)


def _fk(table: str, field: str, target: str) -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        ["school_id", field],
        [f"{target}.school_id", f"{target}.id"],
        name=f"fk_{table}_{field}",
        ondelete="RESTRICT",
    )


def _overlap(
    table: str, suffix: str, fields: tuple[str, ...], where: str = "true"
) -> ExcludeConstraint:
    return ExcludeConstraint(
        ("school_id", "="),
        *((field, "=") for field in fields),
        (
            text(
                "tstzrange(valid_from, GREATEST(valid_from, "
                "COALESCE(LEAST(valid_to, revoked_at), 'infinity'::timestamptz)), '[)')"
            ),
            "&&",
        ),
        where=text(where),
        using="gist",
        name=f"excl_{table}_{suffix}",
    )


def _common(table: str) -> tuple:
    return (
        ForeignKeyConstraint(
            ["school_id"], ["schools.id"], name=f"fk_{table}_school", ondelete="RESTRICT"
        ),
        _fk(table, "source_id", "school_fact_sources"),
        _fk(table, "import_batch_id", "master_data_import_batches"),
        UniqueConstraint("school_id", "id", name=f"uq_{table}_school_id"),
        UniqueConstraint("school_id", "source_key", "version", name=f"uq_{table}_source_version"),
        CheckConstraint("version >= 1", name=f"ck_{table}_version"),
        CheckConstraint("valid_to IS NULL OR valid_to > valid_from", name=f"ck_{table}_interval"),
        _overlap(table, "source_period", ("source_key",)),
    )


class OrganizationProfile(_TemporalFact, Base):
    __tablename__ = "organization_profiles"
    __table_args__ = (
        *_common(__tablename__),
        _fk(__tablename__, "organization_unit_id", "organization_units"),
        CheckConstraint(
            (
                "organization_type IN "
                "('department','grade_group','teaching_research_group','lesson_preparation_group','committee','other')"
            ),
            name="ck_organization_profiles_type",
        ),
        _overlap(__tablename__, "organization_period", ("organization_unit_id",)),
    )
    organization_unit_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    organization_type: Mapped[str] = mapped_column(String(80), nullable=False)
    function_summary: Mapped[str] = mapped_column(String(1000), nullable=False)


class OrganizationMembership(_TemporalFact, Base):
    __tablename__ = "organization_memberships"
    __table_args__ = (
        *_common(__tablename__),
        _fk(__tablename__, "organization_unit_id", "organization_units"),
        _fk(__tablename__, "person_id", "school_people"),
        CheckConstraint(
            "membership_kind IN ('member','participant')", name="ck_organization_memberships_kind"
        ),
        _overlap(
            __tablename__, "member_period", ("organization_unit_id", "person_id", "membership_kind")
        ),
    )
    organization_unit_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    person_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    membership_kind: Mapped[str] = mapped_column(String(20), nullable=False)


class OrganizationLocation(_TemporalFact, Base):
    __tablename__ = "organization_locations"
    __table_args__ = (
        *_common(__tablename__),
        _fk(__tablename__, "organization_unit_id", "organization_units"),
        _fk(__tablename__, "location_id", "campus_locations"),
        CheckConstraint(
            "purpose IN ('office','reception')", name="ck_organization_locations_purpose"
        ),
        _overlap(
            __tablename__, "relation_period", ("organization_unit_id", "location_id", "purpose")
        ),
        _overlap(
            __tablename__,
            "primary_period",
            ("organization_unit_id", "purpose"),
            "is_primary",
        ),
    )
    organization_unit_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    location_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    purpose: Mapped[str] = mapped_column(String(20), nullable=False)
    is_primary: Mapped[bool] = mapped_column(Boolean, nullable=False)


class PersonWorkplace(_TemporalFact, Base):
    __tablename__ = "person_workplaces"
    __table_args__ = (
        *_common(__tablename__),
        _fk(__tablename__, "person_id", "school_people"),
        _fk(__tablename__, "assignment_id", "staff_assignments"),
        _fk(__tablename__, "location_id", "campus_locations"),
        CheckConstraint(
            "(person_id IS NOT NULL)::int + (assignment_id IS NOT NULL)::int = 1",
            name="ck_person_workplaces_subject",
        ),
        CheckConstraint("purpose IN ('office','reception')", name="ck_person_workplaces_purpose"),
        _overlap(__tablename__, "person_period", ("person_id", "location_id", "purpose")),
        _overlap(__tablename__, "assignment_period", ("assignment_id", "location_id", "purpose")),
        _overlap(
            __tablename__,
            "person_primary",
            ("person_id", "purpose"),
            "is_primary",
        ),
        _overlap(
            __tablename__,
            "assignment_primary",
            ("assignment_id", "purpose"),
            "is_primary",
        ),
    )
    person_id: Mapped[UUID | None] = mapped_column(Uuid)
    assignment_id: Mapped[UUID | None] = mapped_column(Uuid)
    location_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    purpose: Mapped[str] = mapped_column(String(20), nullable=False)
    is_primary: Mapped[bool] = mapped_column(Boolean, nullable=False)


class SchoolService(_TemporalFact, Base):
    __tablename__ = "school_services"
    __table_args__ = (
        *_common(__tablename__),
        _fk(__tablename__, "organization_unit_id", "organization_units"),
        _fk(__tablename__, "official_contact_id", "official_contact_points"),
        _fk(__tablename__, "policy_id", "school_policies"),
    )
    display_name: Mapped[str] = mapped_column(String(1000), nullable=False)
    organization_unit_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    audience_description: Mapped[str] = mapped_column(String(1000), nullable=False)
    service_summary: Mapped[str] = mapped_column(String(1000), nullable=False)
    official_contact_id: Mapped[UUID | None] = mapped_column(Uuid)
    policy_id: Mapped[UUID | None] = mapped_column(Uuid)


class FactQuality(_TemporalFact, Base):
    __tablename__ = "school_fact_qualities"
    __table_args__ = (
        *_common(__tablename__),
        _fk(__tablename__, "organization_unit_id", "organization_units"),
        _fk(__tablename__, "person_id", "school_people"),
        _fk(__tablename__, "location_id", "campus_locations"),
        _fk(__tablename__, "education_target_id", "school_education_facts"),
        _fk(__tablename__, "education_position_id", "school_education_facts"),
        CheckConstraint(
            "teaching_role IS NULL OR (target_fact_kind='teaching_assignment' "
            "AND teaching_role IN ('lead','co_teacher')) IS TRUE",
            name="ck_f5_quality_teaching_role",
        ),
        CheckConstraint(
            "quality_kind IN ('source','coverage','negative','dispute','review','correction')",
            name="ck_school_fact_qualities_kind",
        ),
        CheckConstraint(
            (
                "target_fact_kind IN "
                "('school','person','organization','assignment','organization_campus_relationship','location','location_alias','person_contact','official_contact','policy','policy_scope','policy_version','organization_profile','organization_membership','organization_location','person_workplace','school_service',"
                + ",".join(
                    repr(k)
                    for k in sorted(
                        EDUCATION_KINDS
                        | F7_FAMILY_QUALITY_TARGET_KINDS
                        | {
                            "dormitory_building_profile",
                            "dorm_room",
                            "dorm_bed",
                            "student_bed_residence",
                        }
                    )
                )
                + ")"
            ),
            name="ck_school_fact_qualities_target",
        ),
        CheckConstraint(
            (
                "(owner_kind = 'school' AND organization_unit_id IS NULL AND person_id "
                "IS NULL AND location_id IS NULL) OR (owner_kind = 'organization' AND "
                "organization_unit_id IS NOT NULL AND person_id IS NULL AND location_id"
                " IS NULL) OR (owner_kind = 'person' AND person_id IS NOT NULL AND "
                "organization_unit_id IS NULL AND location_id IS NULL) OR (owner_kind ="
                " 'location' AND location_id IS NOT NULL AND organization_unit_id IS "
                "NULL AND person_id IS NULL) OR (owner_kind = 'education' AND "
                "education_target_id IS NOT NULL AND organization_unit_id IS NULL AND "
                "person_id IS NULL AND location_id IS NULL)"
            ),
            name="ck_school_fact_qualities_owner",
        ),
        CheckConstraint(
            "coverage_status IS NULL OR coverage_status IN ('partial','complete')",
            name="ck_school_fact_qualities_coverage",
        ),
        CheckConstraint(
            "source_status IS NULL OR source_status IN ('available','unavailable')",
            name="ck_school_fact_qualities_source",
        ),
        CheckConstraint(
            "dispute_status IS NULL OR dispute_status IN ('open','resolved')",
            name="ck_school_fact_qualities_dispute",
        ),
        CheckConstraint(
            "(coverage_status IS NULL OR quality_kind = 'coverage') AND "
            "(negative_code IS NULL OR quality_kind = 'negative') AND "
            "(dispute_status IS NULL OR quality_kind = 'dispute') AND "
            "(source_status IS NULL OR quality_kind = 'source') AND "
            "(replacement_source_key IS NULL OR quality_kind = 'correction')",
            name="ck_school_fact_qualities_exclusive_fields",
        ),
        CheckConstraint(
            "contact_kind IS NULL OR contact_kind IN ('phone','email')",
            name="ck_school_fact_qualities_contact_kind",
        ),
        CheckConstraint(
            "review_due_at IS NULL OR review_due_at > verified_at",
            name="ck_school_fact_qualities_review",
        ),
        CheckConstraint(
            (
                "(quality_kind = 'source' AND source_status IS NOT NULL) OR "
                "(quality_kind = 'coverage' AND coverage_status IS NOT NULL) OR "
                "(quality_kind = 'negative' AND negative_code IS NOT NULL) OR "
                "(quality_kind = 'dispute' AND dispute_status IS NOT NULL AND "
                "evidence_reference IS NOT NULL) OR (quality_kind = 'review' AND "
                "review_due_at IS NOT NULL) OR (quality_kind = 'correction' AND "
                "target_source_key IS NOT NULL AND replacement_source_key IS NOT NULL "
                "AND target_source_key <> replacement_source_key AND evidence_reference"
                " IS NOT NULL)"
            ),
            name="ck_school_fact_qualities_required",
        ),
        CheckConstraint(
            (
                "negative_code IS NULL OR ((negative_code = 'no_office' AND "
                "target_fact_kind IN ('organization_location','person_workplace') AND "
                "purpose = 'office') OR (negative_code = 'no_reception' AND "
                "target_fact_kind IN ('organization_location','person_workplace') AND "
                "purpose = 'reception') OR (negative_code = 'no_official_channel' AND "
                "target_fact_kind = 'official_contact' AND contact_kind IN "
                "('phone','email')) OR (negative_code = 'no_service' AND "
                "target_fact_kind = 'school_service') OR (negative_code = "
                "'vacant_position' AND target_fact_kind = 'student_class_position' AND "
                "education_position_id IS NOT NULL) OR (negative_code = "
                "'no_class_teacher' AND target_fact_kind = 'class_teacher_assignment') "
                "OR (negative_code = 'no_fixed_location' AND target_fact_kind = "
                "'class_location') OR (negative_code='no_teaching_assignment' AND "
                "target_fact_kind='teaching_assignment' AND education_target_id IS NOT NULL "
                "AND teaching_role IN ('lead','co_teacher') "
                "AND education_position_id IS NULL AND target_source_key IS NULL "
                "AND replacement_source_key IS NULL) OR (negative_code='no_residence' "
                "AND target_fact_kind='student_bed_residence' AND owner_kind='person' "
                "AND person_id IS NOT NULL AND purpose IS NULL) OR (negative_code='no_dormitory' "
                "AND target_fact_kind='dormitory_building_profile' "
                "AND owner_kind IN ('school','location') "
                "AND purpose IS NULL)) IS TRUE"
            ),
            name="ck_school_fact_qualities_negative",
        ),
    )
    education_target_id: Mapped[UUID | None] = mapped_column(Uuid)
    education_position_id: Mapped[UUID | None] = mapped_column(Uuid)
    teaching_role: Mapped[str | None] = mapped_column(String(20))
    field_name: Mapped[str | None] = mapped_column(String(1000))
    academic_year: Mapped[int | None] = mapped_column(BigInteger)
    quality_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    target_fact_kind: Mapped[str] = mapped_column(String(80), nullable=False)
    owner_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    organization_unit_id: Mapped[UUID | None] = mapped_column(Uuid)
    person_id: Mapped[UUID | None] = mapped_column(Uuid)
    location_id: Mapped[UUID | None] = mapped_column(Uuid)
    target_source_key: Mapped[str | None] = mapped_column(String(255))
    replacement_source_key: Mapped[str | None] = mapped_column(String(255))
    responsibility: Mapped[str] = mapped_column(String(1000), nullable=False)
    evidence_reference: Mapped[str | None] = mapped_column(String(1000))
    purpose: Mapped[str | None] = mapped_column(String(20))
    contact_kind: Mapped[str | None] = mapped_column(String(20))
    coverage_status: Mapped[str | None] = mapped_column(String(20))
    coverage_basis: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    negative_code: Mapped[str | None] = mapped_column(String(40))
    dispute_status: Mapped[str | None] = mapped_column(String(20))
    source_status: Mapped[str | None] = mapped_column(String(20))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    verified_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    review_due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


F2_MODELS: dict[str, type[Base]] = {
    "organization_profile": OrganizationProfile,
    "organization_membership": OrganizationMembership,
    "organization_location": OrganizationLocation,
    "person_workplace": PersonWorkplace,
    "school_service": SchoolService,
    "fact_quality": FactQuality,
}
