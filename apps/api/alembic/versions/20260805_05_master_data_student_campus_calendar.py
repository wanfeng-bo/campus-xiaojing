"""Add student, campus location, and query-time calendar schema.

Revision ID: 20260805_05
Revises: 20260805_04
Create Date: 2026-08-06
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260805_05"
down_revision: str | None = "20260805_04"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CONFIRMATION_STATUSES = "status IN ('draft', 'confirmed', 'rejected')"
_VALID_INTERVAL = "valid_to IS NULL OR valid_to > valid_from"


def _uuid_column(name: str, *, nullable: bool = False) -> sa.Column[object]:
    return sa.Column(name, postgresql.UUID(as_uuid=True), nullable=nullable)


def _version_column() -> sa.Column[object]:
    return sa.Column(
        "version",
        sa.BigInteger(),
        server_default=sa.text("1"),
        nullable=False,
    )


def _created_at_column() -> sa.Column[object]:
    return sa.Column(
        "created_at",
        sa.DateTime(timezone=True),
        server_default=sa.text("CURRENT_TIMESTAMP"),
        nullable=False,
    )


def _updated_at_column() -> sa.Column[object]:
    return sa.Column(
        "updated_at",
        sa.DateTime(timezone=True),
        server_default=sa.text("CURRENT_TIMESTAMP"),
        nullable=False,
    )


def _effective_range() -> sa.TextClause:
    return sa.text("tstzrange(valid_from, COALESCE(valid_to, 'infinity'::timestamptz), '[)')")


def upgrade() -> None:
    """创建 Task 4 学生、地点与查询时校历的数据库骨架。"""

    op.create_table(
        "administrative_classes",
        _uuid_column("id"),
        _uuid_column("school_id"),
        sa.Column("school_year_code", sa.String(length=40), nullable=False),
        sa.Column("grade_code", sa.String(length=40), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        _version_column(),
        _created_at_column(),
        _updated_at_column(),
        sa.PrimaryKeyConstraint("id", name="pk_administrative_classes"),
        sa.UniqueConstraint(
            "school_id",
            "id",
            name="uq_administrative_classes_school_id_id",
        ),
        sa.ForeignKeyConstraint(
            ["school_id"],
            ["schools.id"],
            name="fk_administrative_classes_school",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'inactive')",
            name="ck_administrative_classes_status",
        ),
        sa.CheckConstraint(
            "version >= 1",
            name="ck_administrative_classes_version_positive",
        ),
    )
    op.create_index(
        "ix_administrative_classes_current_grade",
        "administrative_classes",
        ["school_id", "grade_code", "status", "id"],
    )

    op.create_table(
        "administrative_class_versions",
        _uuid_column("id"),
        _uuid_column("school_id"),
        _uuid_column("administrative_class_id"),
        sa.Column("display_name", sa.String(length=200), nullable=False),
        sa.Column("short_name", sa.String(length=120), nullable=True),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_to", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source_kind", sa.String(length=40), nullable=False),
        sa.Column("source_ref", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        _version_column(),
        _created_at_column(),
        _updated_at_column(),
        sa.PrimaryKeyConstraint("id", name="pk_administrative_class_versions"),
        sa.ForeignKeyConstraint(
            ["school_id", "administrative_class_id"],
            ["administrative_classes.school_id", "administrative_classes.id"],
            name="fk_administrative_class_versions_school_class",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            _CONFIRMATION_STATUSES,
            name="ck_administrative_class_versions_status",
        ),
        sa.CheckConstraint(
            _VALID_INTERVAL,
            name="ck_administrative_class_versions_valid_interval",
        ),
        sa.CheckConstraint(
            "version >= 1",
            name="ck_administrative_class_versions_version_positive",
        ),
        postgresql.ExcludeConstraint(
            ("school_id", "="),
            ("administrative_class_id", "="),
            (_effective_range(), "&&"),
            where=sa.text("status = 'confirmed'"),
            using="gist",
            name="excl_administrative_class_versions_confirmed_period",
        ),
    )
    op.create_index(
        "ix_administrative_class_versions_as_of",
        "administrative_class_versions",
        ["school_id", "administrative_class_id", "status", "valid_from", "valid_to"],
    )

    op.create_table(
        "students",
        _uuid_column("id"),
        _uuid_column("school_id"),
        _uuid_column("person_id"),
        sa.Column("status", sa.String(length=20), nullable=False),
        _version_column(),
        _created_at_column(),
        _updated_at_column(),
        sa.PrimaryKeyConstraint("id", name="pk_students"),
        sa.UniqueConstraint("school_id", "id", name="uq_students_school_id_id"),
        sa.UniqueConstraint("school_id", "person_id", name="uq_students_school_person"),
        sa.ForeignKeyConstraint(
            ["school_id", "person_id"],
            ["school_people.school_id", "school_people.id"],
            name="fk_students_school_person",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'inactive', 'merged')",
            name="ck_students_status",
        ),
        sa.CheckConstraint("version >= 1", name="ck_students_version_positive"),
    )

    op.create_table(
        "student_enrollments",
        _uuid_column("id"),
        _uuid_column("school_id"),
        _uuid_column("student_id"),
        _uuid_column("administrative_class_id"),
        sa.Column("student_number", sa.String(length=80), nullable=True),
        sa.Column("seat_number", sa.String(length=20), nullable=True),
        sa.Column("source_kind", sa.String(length=40), nullable=False),
        sa.Column("source_ref", sa.String(length=255), nullable=False),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_to", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        _version_column(),
        _created_at_column(),
        _updated_at_column(),
        sa.PrimaryKeyConstraint("id", name="pk_student_enrollments"),
        sa.UniqueConstraint(
            "school_id",
            "id",
            name="uq_student_enrollments_school_id_id",
        ),
        sa.ForeignKeyConstraint(
            ["school_id", "student_id"],
            ["students.school_id", "students.id"],
            name="fk_student_enrollments_school_student",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["school_id", "administrative_class_id"],
            ["administrative_classes.school_id", "administrative_classes.id"],
            name="fk_student_enrollments_school_class",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            _CONFIRMATION_STATUSES,
            name="ck_student_enrollments_status",
        ),
        sa.CheckConstraint(
            _VALID_INTERVAL,
            name="ck_student_enrollments_valid_interval",
        ),
        sa.CheckConstraint(
            "student_number IS NULL OR btrim(student_number) <> ''",
            name="ck_student_enrollments_student_number_nonempty",
        ),
        sa.CheckConstraint(
            "seat_number IS NULL OR btrim(seat_number) <> ''",
            name="ck_student_enrollments_seat_number_nonempty",
        ),
        sa.CheckConstraint(
            "version >= 1",
            name="ck_student_enrollments_version_positive",
        ),
        postgresql.ExcludeConstraint(
            ("school_id", "="),
            ("student_id", "="),
            (_effective_range(), "&&"),
            where=sa.text("status = 'confirmed'"),
            using="gist",
            name="excl_student_enrollments_confirmed_period",
        ),
    )
    op.create_index(
        "ix_student_enrollments_current_student",
        "student_enrollments",
        ["school_id", "student_id", "status", "valid_from", "valid_to"],
    )
    op.create_index(
        "ix_student_enrollments_current_class_roster",
        "student_enrollments",
        [
            "school_id",
            "administrative_class_id",
            "status",
            "valid_from",
            "valid_to",
            "student_id",
        ],
    )

    op.create_table(
        "guardian_relationships",
        _uuid_column("id"),
        _uuid_column("school_id"),
        _uuid_column("guardian_person_id"),
        _uuid_column("student_id"),
        sa.Column("relationship_kind", sa.String(length=40), nullable=False),
        sa.Column("verification_source", sa.String(length=40), nullable=False),
        sa.Column("verification_reference", sa.String(length=255), nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_to", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        _version_column(),
        _created_at_column(),
        _updated_at_column(),
        sa.PrimaryKeyConstraint("id", name="pk_guardian_relationships"),
        sa.UniqueConstraint(
            "school_id",
            "id",
            name="uq_guardian_relationships_school_id_id",
        ),
        sa.ForeignKeyConstraint(
            ["school_id", "guardian_person_id"],
            ["school_people.school_id", "school_people.id"],
            name="fk_guardian_relationships_school_person",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["school_id", "student_id"],
            ["students.school_id", "students.id"],
            name="fk_guardian_relationships_school_student",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            _CONFIRMATION_STATUSES,
            name="ck_guardian_relationships_status",
        ),
        sa.CheckConstraint(
            _VALID_INTERVAL,
            name="ck_guardian_relationships_valid_interval",
        ),
        sa.CheckConstraint(
            "version >= 1",
            name="ck_guardian_relationships_version_positive",
        ),
        postgresql.ExcludeConstraint(
            ("school_id", "="),
            ("guardian_person_id", "="),
            ("student_id", "="),
            ("relationship_kind", "="),
            (_effective_range(), "&&"),
            where=sa.text("status = 'confirmed'"),
            using="gist",
            name="excl_guardian_relationships_confirmed_period",
        ),
    )
    op.create_index(
        "ix_guardian_relationships_current_guardian",
        "guardian_relationships",
        [
            "school_id",
            "guardian_person_id",
            "status",
            "valid_from",
            "valid_to",
            "student_id",
        ],
    )

    op.create_table(
        "campus_locations",
        _uuid_column("id"),
        _uuid_column("school_id"),
        _uuid_column("parent_location_id", nullable=True),
        sa.Column("kind", sa.String(length=40), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        _version_column(),
        _created_at_column(),
        _updated_at_column(),
        sa.PrimaryKeyConstraint("id", name="pk_campus_locations"),
        sa.UniqueConstraint("school_id", "id", name="uq_campus_locations_school_id_id"),
        sa.ForeignKeyConstraint(
            ["school_id"],
            ["schools.id"],
            name="fk_campus_locations_school",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["school_id", "parent_location_id"],
            ["campus_locations.school_id", "campus_locations.id"],
            name="fk_campus_locations_school_parent",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "parent_location_id IS NULL OR parent_location_id <> id",
            name="ck_campus_locations_not_self_parent",
        ),
        sa.CheckConstraint(
            "(kind = 'campus' AND parent_location_id IS NULL) OR "
            "(kind <> 'campus' AND parent_location_id IS NOT NULL)",
            name="ck_campus_locations_top_level_parent",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'inactive')",
            name="ck_campus_locations_status",
        ),
        sa.CheckConstraint("version >= 1", name="ck_campus_locations_version_positive"),
    )
    op.create_index(
        "ix_campus_locations_current_parent",
        "campus_locations",
        ["school_id", "parent_location_id", "status", "id"],
    )
    op.execute(
        """
        CREATE FUNCTION public.prevent_campus_location_cycle()
        RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = pg_catalog, public
        AS $$
        BEGIN
            IF NEW.parent_location_id IS NULL THEN
                RETURN NEW;
            END IF;

            PERFORM pg_catalog.pg_advisory_xact_lock(
                pg_catalog.hashtextextended(NEW.school_id::text, 0)
            );

            IF EXISTS (
                WITH RECURSIVE ancestors(id, parent_location_id) AS (
                    SELECT location.id, location.parent_location_id
                    FROM public.campus_locations AS location
                    WHERE location.school_id = NEW.school_id
                      AND location.id = NEW.parent_location_id

                    UNION ALL

                    SELECT parent.id, parent.parent_location_id
                    FROM public.campus_locations AS parent
                    JOIN ancestors AS child
                      ON parent.school_id = NEW.school_id
                     AND parent.id = child.parent_location_id
                )
                SELECT 1
                FROM ancestors
                WHERE id = NEW.id
            ) THEN
                RAISE EXCEPTION '校园地点层级禁止形成循环'
                    USING ERRCODE = '23514',
                          CONSTRAINT = 'ck_campus_locations_acyclic';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE CONSTRAINT TRIGGER trg_campus_locations_acyclic
        AFTER INSERT OR UPDATE OF school_id, parent_location_id
        ON public.campus_locations
        DEFERRABLE INITIALLY IMMEDIATE
        FOR EACH ROW
        EXECUTE FUNCTION public.prevent_campus_location_cycle()
        """
    )

    op.create_table(
        "campus_location_versions",
        _uuid_column("id"),
        _uuid_column("school_id"),
        _uuid_column("location_id"),
        sa.Column("display_name", sa.String(length=200), nullable=False),
        sa.Column(
            "aliases",
            postgresql.ARRAY(sa.String(length=200)),
            server_default=sa.text("ARRAY[]::varchar[]"),
            nullable=False,
        ),
        sa.Column("display_path", sa.String(length=1000), nullable=False),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_to", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source_kind", sa.String(length=40), nullable=False),
        sa.Column("source_ref", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        _version_column(),
        _created_at_column(),
        _updated_at_column(),
        sa.PrimaryKeyConstraint("id", name="pk_campus_location_versions"),
        sa.ForeignKeyConstraint(
            ["school_id", "location_id"],
            ["campus_locations.school_id", "campus_locations.id"],
            name="fk_campus_location_versions_school_location",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            _CONFIRMATION_STATUSES,
            name="ck_campus_location_versions_status",
        ),
        sa.CheckConstraint(
            _VALID_INTERVAL,
            name="ck_campus_location_versions_valid_interval",
        ),
        sa.CheckConstraint(
            "version >= 1",
            name="ck_campus_location_versions_version_positive",
        ),
        postgresql.ExcludeConstraint(
            ("school_id", "="),
            ("location_id", "="),
            (_effective_range(), "&&"),
            where=sa.text("status = 'confirmed'"),
            using="gist",
            name="excl_campus_location_versions_confirmed_period",
        ),
    )
    op.create_index(
        "ix_campus_location_versions_as_of",
        "campus_location_versions",
        ["school_id", "location_id", "status", "valid_from", "valid_to"],
    )

    op.create_table(
        "school_calendar_versions",
        _uuid_column("id"),
        _uuid_column("school_id"),
        sa.Column("display_name", sa.String(length=200), nullable=False),
        sa.Column("timezone_name", sa.String(length=64), nullable=False),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_to", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source_kind", sa.String(length=40), nullable=False),
        sa.Column("source_ref", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        _version_column(),
        _created_at_column(),
        _updated_at_column(),
        sa.PrimaryKeyConstraint("id", name="pk_school_calendar_versions"),
        sa.UniqueConstraint(
            "school_id",
            "id",
            name="uq_school_calendar_versions_school_id_id",
        ),
        sa.ForeignKeyConstraint(
            ["school_id"],
            ["schools.id"],
            name="fk_school_calendar_versions_school",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "status IN ('draft', 'published', 'superseded')",
            name="ck_school_calendar_versions_status",
        ),
        sa.CheckConstraint(
            _VALID_INTERVAL,
            name="ck_school_calendar_versions_valid_interval",
        ),
        sa.CheckConstraint(
            "version >= 1",
            name="ck_school_calendar_versions_version_positive",
        ),
        postgresql.ExcludeConstraint(
            ("school_id", "="),
            (_effective_range(), "&&"),
            where=sa.text("status = 'published'"),
            using="gist",
            name="excl_school_calendar_versions_published_period",
        ),
    )
    op.create_index(
        "ix_school_calendar_versions_published_as_of",
        "school_calendar_versions",
        ["school_id", "status", "valid_from", "valid_to"],
    )

    op.create_table(
        "school_calendar_days",
        _uuid_column("id"),
        _uuid_column("school_id"),
        _uuid_column("calendar_version_id"),
        sa.Column("local_date", sa.Date(), nullable=False),
        sa.Column("day_kind", sa.String(length=20), nullable=False),
        sa.Column("actual_start_time", sa.Time(), nullable=True),
        sa.Column("actual_end_time", sa.Time(), nullable=True),
        sa.Column("source_kind", sa.String(length=40), nullable=False),
        sa.Column("note", sa.String(length=500), nullable=True),
        _version_column(),
        _created_at_column(),
        _updated_at_column(),
        sa.PrimaryKeyConstraint("id", name="pk_school_calendar_days"),
        sa.ForeignKeyConstraint(
            ["school_id", "calendar_version_id"],
            ["school_calendar_versions.school_id", "school_calendar_versions.id"],
            name="fk_school_calendar_days_school_version",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "school_id",
            "calendar_version_id",
            "local_date",
            name="uq_school_calendar_days_version_date",
        ),
        sa.CheckConstraint(
            "day_kind IN ('working', 'rest')",
            name="ck_school_calendar_days_kind",
        ),
        sa.CheckConstraint(
            "(actual_start_time IS NULL AND actual_end_time IS NULL) OR "
            "(actual_start_time IS NOT NULL AND actual_end_time IS NOT NULL "
            "AND actual_end_time > actual_start_time)",
            name="ck_school_calendar_days_actual_time",
        ),
        sa.CheckConstraint(
            "version >= 1",
            name="ck_school_calendar_days_version_positive",
        ),
    )
    op.create_index(
        "ix_school_calendar_days_version_date",
        "school_calendar_days",
        ["school_id", "calendar_version_id", "local_date"],
    )


def downgrade() -> None:
    """逆序移除 Task 4 学生、地点和校历表。"""

    op.drop_table("school_calendar_days")
    op.drop_table("school_calendar_versions")
    op.drop_table("campus_location_versions")
    op.execute("DROP TRIGGER IF EXISTS trg_campus_locations_acyclic ON public.campus_locations")
    op.execute("DROP FUNCTION IF EXISTS public.prevent_campus_location_cycle()")
    op.drop_table("campus_locations")
    op.drop_table("guardian_relationships")
    op.drop_table("student_enrollments")
    op.drop_table("students")
    op.drop_table("administrative_class_versions")
    op.drop_table("administrative_classes")
