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
    """构造 PostgreSQL 半开有效区间表达式。"""

    return text("tstzrange(valid_from, COALESCE(valid_to, 'infinity'::timestamptz), '[)')")


class AdministrativeClass(Base):
    """保存行政班的稳定身份、学年和年级结构。"""

    __tablename__ = "administrative_classes"
    __table_args__ = (
        UniqueConstraint("school_id", "id", name="uq_administrative_classes_school_id_id"),
        CheckConstraint(
            "status IN ('active', 'inactive')", name="ck_administrative_classes_status"
        ),
        CheckConstraint("version >= 1", name="ck_administrative_classes_version_positive"),
        Index(
            "ix_administrative_classes_current_grade",
            "school_id",
            "grade_code",
            "status",
            "id",
        ),
        {"info": {"school_facts_authority": "legacy_disabled"}},
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    school_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("schools.id", ondelete="RESTRICT"),
        nullable=False,
    )
    school_year_code: Mapped[str] = mapped_column(String(40), nullable=False)
    grade_code: Mapped[str] = mapped_column(String(40), nullable=False)
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


class AdministrativeClassVersion(Base):
    """保存行政班名称和简称的确认有效期版本。"""

    __tablename__ = "administrative_class_versions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["school_id", "administrative_class_id"],
            ["administrative_classes.school_id", "administrative_classes.id"],
            name="fk_administrative_class_versions_school_class",
            ondelete="RESTRICT",
        ),
        CheckConstraint(_CONFIRMATION_STATUSES, name="ck_administrative_class_versions_status"),
        CheckConstraint(
            _VALID_INTERVAL,
            name="ck_administrative_class_versions_valid_interval",
        ),
        CheckConstraint(
            "version >= 1",
            name="ck_administrative_class_versions_version_positive",
        ),
        ExcludeConstraint(
            ("school_id", "="),
            ("administrative_class_id", "="),
            (_effective_range(), "&&"),
            where=text("status = 'confirmed'"),
            using="gist",
            name="excl_administrative_class_versions_confirmed_period",
        ),
        Index(
            "ix_administrative_class_versions_as_of",
            "school_id",
            "administrative_class_id",
            "status",
            "valid_from",
            "valid_to",
        ),
        {"info": {"school_facts_authority": "legacy_disabled"}},
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    school_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    administrative_class_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    short_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
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


class Student(Base):
    """保存学生稳定主体并同校引用自然人，不复制姓名。"""

    __tablename__ = "students"
    __table_args__ = (
        UniqueConstraint("school_id", "id", name="uq_students_school_id_id"),
        UniqueConstraint(
            "school_id",
            "source_key",
            name="uq_students_school_source_key",
        ),
        UniqueConstraint("school_id", "person_id", name="uq_students_school_person"),
        ForeignKeyConstraint(
            ["school_id", "person_id"],
            ["school_people.school_id", "school_people.id"],
            name="fk_students_school_person",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "status IN ('active', 'inactive', 'merged')",
            name="ck_students_status",
        ),
        CheckConstraint("version >= 1", name="ck_students_version_positive"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    school_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    person_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
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


class StudentEnrollment(Base):
    """保存学生与行政班之间不重叠的确认学籍周期。"""

    __tablename__ = "student_enrollments"
    __table_args__ = (
        UniqueConstraint("school_id", "id", name="uq_student_enrollments_school_id_id"),
        ForeignKeyConstraint(
            ["school_id", "student_id"],
            ["students.school_id", "students.id"],
            name="fk_student_enrollments_school_student",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["school_id", "administrative_class_id"],
            ["administrative_classes.school_id", "administrative_classes.id"],
            name="fk_student_enrollments_school_class",
            ondelete="RESTRICT",
        ),
        CheckConstraint(_CONFIRMATION_STATUSES, name="ck_student_enrollments_status"),
        CheckConstraint(_VALID_INTERVAL, name="ck_student_enrollments_valid_interval"),
        CheckConstraint(
            "student_number IS NULL OR btrim(student_number) <> ''",
            name="ck_student_enrollments_student_number_nonempty",
        ),
        CheckConstraint(
            "seat_number IS NULL OR btrim(seat_number) <> ''",
            name="ck_student_enrollments_seat_number_nonempty",
        ),
        CheckConstraint("version >= 1", name="ck_student_enrollments_version_positive"),
        ExcludeConstraint(
            ("school_id", "="),
            ("student_id", "="),
            (_effective_range(), "&&"),
            where=text("status = 'confirmed'"),
            using="gist",
            name="excl_student_enrollments_confirmed_period",
        ),
        Index(
            "ix_student_enrollments_current_student",
            "school_id",
            "student_id",
            "status",
            "valid_from",
            "valid_to",
        ),
        Index(
            "ix_student_enrollments_current_class_roster",
            "school_id",
            "administrative_class_id",
            "status",
            "valid_from",
            "valid_to",
            "student_id",
        ),
        {"info": {"school_facts_authority": "legacy_disabled"}},
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    school_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    student_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    administrative_class_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    student_number: Mapped[str | None] = mapped_column(String(80), nullable=True)
    seat_number: Mapped[str | None] = mapped_column(String(20), nullable=True)
    source_kind: Mapped[str] = mapped_column(String(40), nullable=False)
    source_ref: Mapped[str] = mapped_column(String(255), nullable=False)
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


class GuardianRelationship(Base):
    """保存监护自然人与学生之间经核实的有效期关系。"""

    __tablename__ = "guardian_relationships"
    __table_args__ = (
        UniqueConstraint("school_id", "id", name="uq_guardian_relationships_school_id_id"),
        ForeignKeyConstraint(
            ["school_id", "guardian_person_id"],
            ["school_people.school_id", "school_people.id"],
            name="fk_guardian_relationships_school_person",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["school_id", "student_id"],
            ["students.school_id", "students.id"],
            name="fk_guardian_relationships_school_student",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["school_id", "requested_by_person_id"],
            ["school_people.school_id", "school_people.id"],
            name="fk_guardian_relationships_requester",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["school_id", "verified_by_person_id"],
            ["school_people.school_id", "school_people.id"],
            name="fk_guardian_relationships_verifier",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["school_id", "requested_by_assignment_id"],
            ["staff_assignments.school_id", "staff_assignments.id"],
            name="fk_guardian_relationships_requester_assignment",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["school_id", "verified_by_assignment_id"],
            ["staff_assignments.school_id", "staff_assignments.id"],
            name="fk_guardian_relationships_verifier_assignment",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["school_id", "review_of_relationship_id"],
            ["guardian_relationships.school_id", "guardian_relationships.id"],
            name="fk_guardian_relationships_review_of",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["school_id", "source_id"],
            ["school_fact_sources.school_id", "school_fact_sources.id"],
            name="fk_guardian_relationships_source",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["school_id", "import_batch_id"],
            ["master_data_import_batches.school_id", "master_data_import_batches.id"],
            name="fk_guardian_relationships_import_batch",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "authority_version IS NULL OR authority_version = 1",
            name="ck_guardian_relationships_authority_version",
        ),
        CheckConstraint(
            "authority_version IS NULL OR (access_purpose IS NOT NULL "
            "AND requested_by_person_id IS NOT NULL "
            "AND requested_by_assignment_id IS NOT NULL "
            "AND requested_at IS NOT NULL AND source_id IS NOT NULL "
            "AND requested_by_person_id <> guardian_person_id "
            "AND ((status = 'pending_verification' "
            "AND verified_by_person_id IS NULL "
            "AND verified_by_assignment_id IS NULL AND verified_at IS NULL) "
            "OR (status = 'confirmed' "
            "AND verified_by_person_id IS NOT NULL "
            "AND verified_by_assignment_id IS NOT NULL "
            "AND verified_at IS NOT NULL "
            "AND verified_by_person_id <> guardian_person_id "
            "AND verified_by_person_id <> requested_by_person_id "
            "AND verified_by_assignment_id <> requested_by_assignment_id) "
            "OR (status IN ('rejected', 'ended', 'revoked') AND ("
            "(verified_by_person_id IS NULL "
            "AND verified_by_assignment_id IS NULL AND verified_at IS NULL) "
            "OR (verified_by_person_id IS NOT NULL "
            "AND verified_by_assignment_id IS NOT NULL "
            "AND verified_at IS NOT NULL "
            "AND verified_by_person_id <> guardian_person_id "
            "AND verified_by_person_id <> requested_by_person_id "
            "AND verified_by_assignment_id <> requested_by_assignment_id)))))",
            name="ck_guardian_relationships_formal_shape",
        ),
        CheckConstraint(
            "status IN ('draft', 'pending_verification', 'confirmed', "
            "'rejected', 'ended', 'revoked')",
            name="ck_guardian_relationships_status",
        ),
        CheckConstraint(_VALID_INTERVAL, name="ck_guardian_relationships_valid_interval"),
        CheckConstraint("version >= 1", name="ck_guardian_relationships_version_positive"),
        ExcludeConstraint(
            ("school_id", "="),
            ("guardian_person_id", "="),
            ("student_id", "="),
            ("relationship_kind", "="),
            (_effective_range(), "&&"),
            where=text("status = 'confirmed'"),
            using="gist",
            name="excl_guardian_relationships_confirmed_period",
        ),
        Index(
            "ix_guardian_relationships_current_guardian",
            "school_id",
            "guardian_person_id",
            "status",
            "valid_from",
            "valid_to",
            "student_id",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    school_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    guardian_person_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    student_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    relationship_kind: Mapped[str] = mapped_column(String(40), nullable=False)
    authority_version: Mapped[int | None] = mapped_column(nullable=True)
    access_purpose: Mapped[str | None] = mapped_column(String(40), nullable=True)
    requested_by_person_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    requested_by_assignment_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    verified_by_person_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    verified_by_assignment_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    review_of_relationship_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    verification_source: Mapped[str] = mapped_column(String(40), nullable=False)
    verification_reference: Mapped[str] = mapped_column(String(255), nullable=False)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    version: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1, server_default="1")
    source_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
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


class StudentSchoolRelationship(Base):
    """保存学生在校状态、学号和走读/寄宿方式的有效期事实。"""

    __tablename__ = "student_school_relationships"
    __table_args__ = (
        UniqueConstraint(
            "school_id",
            "id",
            name="uq_student_school_relationships_school_id_id",
        ),
        ForeignKeyConstraint(
            ["school_id", "student_id"],
            ["students.school_id", "students.id"],
            name="fk_student_school_relationships_student",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["school_id", "primary_campus_location_id"],
            ["campus_locations.school_id", "campus_locations.id"],
            name="fk_student_school_relationships_primary_campus",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["school_id", "administrative_campus_location_id"],
            ["campus_locations.school_id", "campus_locations.id"],
            name="fk_student_school_relationships_administrative_campus",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["school_id", "source_id"],
            ["school_fact_sources.school_id", "school_fact_sources.id"],
            name="fk_student_school_relationships_source",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["school_id", "import_batch_id"],
            ["master_data_import_batches.school_id", "master_data_import_batches.id"],
            name="fk_student_school_relationships_import_batch",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "enrollment_status IN "
            "('enrolled', 'suspended', 'transferred_out', 'withdrawn', 'graduated')",
            name="ck_student_school_relationships_enrollment_status",
        ),
        CheckConstraint(
            "residence_mode IN ('day', 'boarding')",
            name="ck_student_school_relationships_residence_mode",
        ),
        CheckConstraint(_VALID_INTERVAL, name="ck_student_school_relationships_valid_interval"),
        CheckConstraint(
            "version >= 1",
            name="ck_student_school_relationships_version_positive",
        ),
        ExcludeConstraint(
            ("school_id", "="),
            ("student_id", "="),
            (_effective_range(), "&&"),
            using="gist",
            where=text("superseded_at IS NULL"),
            name="excl_student_school_relationships_period",
        ),
        Index(
            "ix_student_school_relationships_as_of",
            "school_id",
            "student_id",
            "valid_from",
            "valid_to",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    school_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    source_key: Mapped[str | None] = mapped_column(String(255))
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    student_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    student_number: Mapped[str] = mapped_column(String(80), nullable=False)
    enrollment_status: Mapped[str] = mapped_column(String(20), nullable=False)
    residence_mode: Mapped[str | None] = mapped_column(String(20), nullable=True)
    primary_campus_location_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    administrative_campus_location_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    version: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1, server_default="1")
    source_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    import_batch_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class StudentBedResidence(Base):
    """保存学生正式床位安排的有效区间，不证明实际到宿。"""

    __tablename__ = "student_bed_residences"
    __table_args__ = (
        ForeignKeyConstraint(
            ["school_id", "student_id"],
            ["students.school_id", "students.id"],
            name="fk_student_bed_residences_student",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["school_id", "bed_id"],
            ["dorm_beds.school_id", "dorm_beds.id"],
            name="fk_student_bed_residences_bed",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["school_id", "source_id"],
            ["school_fact_sources.school_id", "school_fact_sources.id"],
            name="fk_student_bed_residences_source",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["school_id", "import_batch_id"],
            ["master_data_import_batches.school_id", "master_data_import_batches.id"],
            name="fk_student_bed_residences_import_batch",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "school_id",
            "id",
            name="uq_student_bed_residences_school_id_id",
        ),
        CheckConstraint(_VALID_INTERVAL, name="ck_student_bed_residences_valid_interval"),
        CheckConstraint("version >= 1", name="ck_student_bed_residences_version_positive"),
        ExcludeConstraint(
            ("school_id", "="),
            ("student_id", "="),
            (_effective_range(), "&&"),
            where=text("revoked_at IS NULL"),
            using="gist",
            name="excl_student_bed_residences_student_period",
        ),
        ExcludeConstraint(
            ("school_id", "="),
            ("bed_id", "="),
            (_effective_range(), "&&"),
            where=text("revoked_at IS NULL"),
            using="gist",
            name="excl_student_bed_residences_bed_period",
        ),
        Index(
            "ix_student_bed_residences_student_as_of",
            "school_id",
            "student_id",
            "valid_from",
            "valid_to",
        ),
        Index(
            "ix_student_bed_residences_bed_as_of",
            "school_id",
            "bed_id",
            "valid_from",
            "valid_to",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    school_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    student_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    bed_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    version: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1, server_default="1")
    source_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    import_batch_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class StudentContactRelationship(Base):
    """只表达学生与联系人的自然关系、顺序和紧急标志。"""

    __tablename__ = "student_contact_relationships"
    __table_args__ = (
        ForeignKeyConstraint(
            ["school_id", "student_id"],
            ["students.school_id", "students.id"],
            name="fk_student_contact_relationships_student",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["school_id", "contact_person_id"],
            ["school_people.school_id", "school_people.id"],
            name="fk_student_contact_relationships_person",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["school_id", "source_id"],
            ["school_fact_sources.school_id", "school_fact_sources.id"],
            name="fk_student_contact_relationships_source",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["school_id", "import_batch_id"],
            ["master_data_import_batches.school_id", "master_data_import_batches.id"],
            name="fk_student_contact_relationships_import_batch",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "relationship_kind IN ('father', 'mother', 'grandfather', 'grandmother', 'other')",
            name="ck_student_contact_relationships_kind",
        ),
        CheckConstraint("priority >= 1", name="ck_student_contact_relationships_priority"),
        CheckConstraint(_VALID_INTERVAL, name="ck_student_contact_relationships_valid_interval"),
        CheckConstraint(
            "version >= 1",
            name="ck_student_contact_relationships_version_positive",
        ),
        ExcludeConstraint(
            ("school_id", "="),
            ("student_id", "="),
            ("contact_person_id", "="),
            ("relationship_kind", "="),
            (_effective_range(), "&&"),
            using="gist",
            name="excl_student_contact_relationships_period",
        ),
        Index(
            "ix_student_contact_relationships_as_of",
            "school_id",
            "student_id",
            "priority",
            "valid_from",
            "valid_to",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    school_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    student_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    contact_person_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    relationship_kind: Mapped[str] = mapped_column(String(40), nullable=False)
    priority: Mapped[int] = mapped_column(BigInteger, nullable=False)
    is_emergency: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    version: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1, server_default="1")
    source_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    import_batch_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class PersonRegion(Base):
    """保存自愿提供且最多到区县的人员地区事实。"""

    __tablename__ = "person_regions"
    __table_args__ = (
        UniqueConstraint("school_id", "id", name="uq_person_regions_school_id_id"),
        CheckConstraint(
            "btrim(district_code) <> '' AND btrim(district_name) <> ''",
            name="ck_person_regions_district_nonempty",
        ),
        CheckConstraint(
            "region_purpose = 'student_profile'",
            name="ck_person_regions_purpose",
        ),
        ForeignKeyConstraint(
            ["school_id", "person_id"],
            ["school_people.school_id", "school_people.id"],
            name="fk_person_regions_person",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["school_id", "source_id"],
            ["school_fact_sources.school_id", "school_fact_sources.id"],
            name="fk_person_regions_source",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["school_id", "import_batch_id"],
            ["master_data_import_batches.school_id", "master_data_import_batches.id"],
            name="fk_person_regions_import_batch",
            ondelete="RESTRICT",
        ),
        CheckConstraint(_VALID_INTERVAL, name="ck_person_regions_valid_interval"),
        CheckConstraint("version >= 1", name="ck_person_regions_version_positive"),
        ExcludeConstraint(
            ("school_id", "="),
            ("person_id", "="),
            ("region_purpose", "="),
            (_effective_range(), "&&"),
            using="gist",
            name="excl_person_regions_period",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    school_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    person_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    district_code: Mapped[str] = mapped_column(String(40), nullable=False)
    district_name: Mapped[str] = mapped_column(String(120), nullable=False)
    region_purpose: Mapped[str] = mapped_column(String(40), nullable=False)
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    version: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1, server_default="1")
    source_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    import_batch_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
