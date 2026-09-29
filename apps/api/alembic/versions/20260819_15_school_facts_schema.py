"""Add the SF1 authoritative school facts schema.

Revision ID: 20260819_15
Revises: 20260816_14
Create Date: 2026-08-19
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "20260819_15"
down_revision = "20260816_14"
branch_labels = None
depends_on = None

_LOCAL_SOURCE_DEFAULT = sa.text("('local:'::text || gen_random_uuid()::text)")
_VALID_RANGE = sa.text("tstzrange(valid_from, COALESCE(valid_to, 'infinity'::timestamptz), '[)')")


def _uuid(name: str, *, nullable: bool = False) -> sa.Column[object]:
    return sa.Column(name, sa.Uuid(), nullable=nullable)


def _created_at() -> sa.Column[object]:
    return sa.Column(
        "created_at",
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.text("now()"),
    )


def _validity_columns() -> tuple[sa.Column[object], ...]:
    return (
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_to", sa.DateTime(timezone=True), nullable=True),
        sa.Column("version", sa.BigInteger(), nullable=False, server_default=sa.text("1")),
        _uuid("source_id"),
        _uuid("import_batch_id", nullable=True),
        _created_at(),
    )


def _add_stable_source_key(table_name: str, legacy_prefix: str) -> None:
    op.add_column(
        table_name,
        sa.Column(
            "source_key",
            sa.String(length=255),
            nullable=True,
            server_default=_LOCAL_SOURCE_DEFAULT,
        ),
    )
    op.execute(
        sa.text(f"UPDATE public.{table_name} SET source_key = '{legacy_prefix}' || id::text")
    )
    op.alter_column(
        table_name,
        "source_key",
        existing_type=sa.String(length=255),
        nullable=False,
        existing_server_default=_LOCAL_SOURCE_DEFAULT,
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            r"""
            DO $downgrade$
            BEGIN
                IF EXISTS (
                    SELECT 1 FROM public.school_fact_sources
                    UNION ALL SELECT 1 FROM public.school_versions
                    UNION ALL SELECT 1 FROM public.school_person_profile_versions
                    UNION ALL SELECT 1 FROM public.person_contact_points
                    UNION ALL SELECT 1 FROM public.official_contact_points
                    UNION ALL SELECT 1 FROM public.campus_location_aliases
                    UNION ALL SELECT 1 FROM public.dorm_room_profile_versions
                    UNION ALL SELECT 1 FROM public.dorm_beds
                    UNION ALL SELECT 1 FROM public.dorm_bed_versions
                    UNION ALL SELECT 1 FROM public.student_school_relationships
                    UNION ALL SELECT 1 FROM public.student_bed_residences
                    UNION ALL SELECT 1 FROM public.student_contact_relationships
                    UNION ALL SELECT 1 FROM public.school_policies
                    UNION ALL SELECT 1 FROM public.school_policy_versions
                ) OR EXISTS (
                    SELECT external_subject
                    FROM public.users
                    GROUP BY external_subject
                    HAVING count(*) > 1
                ) OR EXISTS (
                    SELECT 1 FROM public.schools
                    WHERE source_key <> 'legacy:schools:' || id::text
                ) OR EXISTS (
                    SELECT 1 FROM public.school_people
                    WHERE source_key <> 'legacy:school_people:' || id::text
                ) OR EXISTS (
                    SELECT 1 FROM public.account_person_bindings
                    WHERE source_key <> 'legacy:account_person_bindings:' || id::text
                ) OR EXISTS (
                    SELECT 1 FROM public.organization_units
                    WHERE source_key <> 'legacy:organization_units:' || id::text
                ) OR EXISTS (
                    SELECT 1 FROM public.staff_assignments
                    WHERE source_key <> 'legacy:staff_assignments:' || id::text
                ) OR EXISTS (
                    SELECT 1 FROM public.campus_locations
                    WHERE source_key <> 'legacy:campus_locations:' || id::text
                ) OR EXISTS (
                    SELECT 1 FROM public.students
                    WHERE source_key <> 'legacy:students:' || id::text
                ) OR EXISTS (
                    SELECT 1 FROM public.organization_unit_versions
                    WHERE business_code IS NOT NULL
                ) OR EXISTS (
                    SELECT 1 FROM public.campus_location_versions
                    WHERE business_code IS NOT NULL
                ) OR EXISTS (
                    SELECT 1 FROM public.staff_assignments
                    WHERE assignment_kind <> 'substantive' OR revoked_at IS NOT NULL
                ) OR EXISTS (
                    SELECT 1 FROM public.users
                    WHERE provider_code <> 'legacy' OR provider_tenant_key <> 'legacy'
                ) THEN
                    RAISE EXCEPTION USING
                        ERRCODE = '55000',
                        MESSAGE = 'SF1 authoritative school facts exist; downgrade is unsafe';
                END IF;
            END
            $downgrade$;
            """
        )
    )

    op.execute(
        sa.text(
            r"""
            DO $block$
            DECLARE
                table_name text;
            BEGIN
                FOREACH table_name IN ARRAY ARRAY[
                    'schools',
                    'school_people',
                    'account_person_bindings',
                    'organization_units',
                    'staff_assignments',
                    'campus_locations',
                    'dorm_beds',
                    'students',
                    'school_policies',
                    'school_fact_sources'
                ] LOOP
                    EXECUTE format(
                        'DROP TRIGGER IF EXISTS %I ON public.%I',
                        'trg_sf1_immutable_source_key_' || table_name,
                        table_name
                    );
                END LOOP;
            END
            $block$;
            """
        )
    )
    op.execute("DROP FUNCTION IF EXISTS public.sf1_reject_source_key_update()")

    op.drop_table("school_policy_versions")
    op.drop_table("school_policies")
    op.drop_table("student_contact_relationships")
    op.drop_table("student_bed_residences")
    op.drop_table("student_school_relationships")
    op.drop_table("dorm_bed_versions")
    op.drop_table("dorm_beds")
    op.drop_table("dorm_room_profile_versions")
    op.drop_table("campus_location_aliases")
    op.drop_table("official_contact_points")
    op.drop_table("person_contact_points")
    op.drop_table("school_person_profile_versions")
    op.drop_table("school_versions")
    op.drop_table("school_fact_sources")

    op.execute(
        sa.text(
            r"""
            DO $block$
            DECLARE
                table_name text;
            BEGIN
                FOREACH table_name IN ARRAY ARRAY[
                    'school_person_names',
                    'account_person_bindings',
                    'organization_unit_versions',
                    'staff_assignments',
                    'campus_location_versions'
                ] LOOP
                    EXECUTE format(
                        'DROP TRIGGER IF EXISTS %I ON public.%I',
                        'trg_sf1_reject_delete_' || table_name,
                        table_name
                    );
                END LOOP;
            END
            $block$;
            """
        )
    )
    op.execute("DROP FUNCTION IF EXISTS public.sf1_reject_history_delete()")
    op.execute("DROP FUNCTION IF EXISTS public.sf1_validate_residence_transfer()")
    op.execute("DROP FUNCTION IF EXISTS public.sf1_validate_room_residences()")
    op.execute("DROP FUNCTION IF EXISTS public.sf1_assert_room_residences_valid(uuid, uuid)")
    op.execute("DROP FUNCTION IF EXISTS public.sf1_validate_bed_residences()")
    op.execute("DROP FUNCTION IF EXISTS public.sf1_assert_bed_residences_valid(uuid, uuid)")
    op.execute("DROP FUNCTION IF EXISTS public.sf1_validate_relationship_residences()")
    op.execute("DROP FUNCTION IF EXISTS public.sf1_assert_student_residences_valid(uuid, uuid)")
    op.execute("DROP FUNCTION IF EXISTS public.sf1_validate_student_residence()")
    op.execute("DROP FUNCTION IF EXISTS public.sf1_validate_bed_version()")
    op.execute("DROP FUNCTION IF EXISTS public.sf1_validate_room_profile()")
    op.execute("DROP FUNCTION IF EXISTS public.sf1_reject_dorm_bed_identity_update()")
    op.execute("DROP FUNCTION IF EXISTS public.sf1_lock_student_residence_roots()")
    op.execute("DROP FUNCTION IF EXISTS public.sf1_lock_student_relationship_root()")

    op.drop_constraint(
        "uq_students_school_source_key",
        "students",
        type_="unique",
    )
    op.drop_column("students", "source_key")

    op.drop_column("campus_location_versions", "business_code")
    op.drop_constraint(
        "uq_campus_locations_school_source_key",
        "campus_locations",
        type_="unique",
    )
    op.drop_column("campus_locations", "source_key")

    op.drop_constraint(
        "uq_users_provider_tenant_subject",
        "users",
        type_="unique",
    )
    op.execute(
        sa.text(
            """
            DO $block$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1
                    FROM pg_catalog.pg_constraint
                    WHERE conrelid = 'public.users'::regclass
                      AND conname = 'uq_users_external_subject'
                ) THEN
                    ALTER TABLE public.users
                    ADD CONSTRAINT uq_users_external_subject UNIQUE (external_subject);
                END IF;
            END
            $block$;
            """
        )
    )
    op.drop_column("users", "provider_tenant_key")
    op.drop_column("users", "provider_code")

    op.drop_constraint(
        "ck_staff_assignments_assignment_kind",
        "staff_assignments",
        type_="check",
    )
    op.drop_constraint(
        "uq_staff_assignments_school_source_key",
        "staff_assignments",
        type_="unique",
    )
    op.drop_column("staff_assignments", "revoked_at")
    op.drop_column("staff_assignments", "assignment_kind")
    op.drop_column("staff_assignments", "source_key")

    op.drop_column("organization_unit_versions", "business_code")
    op.drop_constraint(
        "uq_organization_units_school_source_key",
        "organization_units",
        type_="unique",
    )
    op.drop_column("organization_units", "source_key")

    op.drop_constraint(
        "uq_account_person_bindings_school_source_key",
        "account_person_bindings",
        type_="unique",
    )
    op.drop_column("account_person_bindings", "source_key")

    op.drop_constraint(
        "uq_school_people_school_source_key",
        "school_people",
        type_="unique",
    )
    op.drop_column("school_people", "source_key")

    op.drop_constraint("uq_schools_source_key", "schools", type_="unique")
    op.drop_column("schools", "source_key")


def _source_foreign_key(table_name: str, constraint_name: str) -> None:
    op.create_foreign_key(
        constraint_name,
        table_name,
        "school_fact_sources",
        ["school_id", "source_id"],
        ["school_id", "id"],
        ondelete="RESTRICT",
    )


def _batch_foreign_key(table_name: str, constraint_name: str) -> None:
    op.create_foreign_key(
        constraint_name,
        table_name,
        "master_data_import_batches",
        ["school_id", "import_batch_id"],
        ["school_id", "id"],
        ondelete="RESTRICT",
    )


def upgrade() -> None:
    _add_stable_source_key("schools", "legacy:schools:")
    op.create_unique_constraint("uq_schools_source_key", "schools", ["source_key"])

    _add_stable_source_key("school_people", "legacy:school_people:")
    op.create_unique_constraint(
        "uq_school_people_school_source_key",
        "school_people",
        ["school_id", "source_key"],
    )

    _add_stable_source_key("account_person_bindings", "legacy:account_person_bindings:")
    op.create_unique_constraint(
        "uq_account_person_bindings_school_source_key",
        "account_person_bindings",
        ["school_id", "source_key"],
    )

    _add_stable_source_key("organization_units", "legacy:organization_units:")
    op.create_unique_constraint(
        "uq_organization_units_school_source_key",
        "organization_units",
        ["school_id", "source_key"],
    )
    op.add_column(
        "organization_unit_versions",
        sa.Column("business_code", sa.String(length=80), nullable=True),
    )

    _add_stable_source_key("staff_assignments", "legacy:staff_assignments:")
    op.add_column(
        "staff_assignments",
        sa.Column(
            "assignment_kind",
            sa.String(length=20),
            nullable=False,
            server_default=sa.text("'substantive'"),
        ),
    )
    op.add_column(
        "staff_assignments",
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_unique_constraint(
        "uq_staff_assignments_school_source_key",
        "staff_assignments",
        ["school_id", "source_key"],
    )
    op.create_check_constraint(
        "ck_staff_assignments_assignment_kind",
        "staff_assignments",
        "assignment_kind IN ('substantive', 'part_time', 'acting')",
    )

    op.add_column(
        "users",
        sa.Column(
            "provider_code",
            sa.String(length=40),
            nullable=False,
            server_default=sa.text("'legacy'"),
        ),
    )
    op.add_column(
        "users",
        sa.Column(
            "provider_tenant_key",
            sa.String(length=255),
            nullable=False,
            server_default=sa.text("'legacy'"),
        ),
    )
    op.create_unique_constraint(
        "uq_users_provider_tenant_subject",
        "users",
        ["provider_code", "provider_tenant_key", "external_subject"],
    )
    op.drop_constraint(
        "uq_users_external_subject",
        "users",
        type_="unique",
    )

    _add_stable_source_key("campus_locations", "legacy:campus_locations:")
    op.create_unique_constraint(
        "uq_campus_locations_school_source_key",
        "campus_locations",
        ["school_id", "source_key"],
    )
    op.add_column(
        "campus_location_versions",
        sa.Column("business_code", sa.String(length=80), nullable=True),
    )

    _add_stable_source_key("students", "legacy:students:")
    op.create_unique_constraint(
        "uq_students_school_source_key",
        "students",
        ["school_id", "source_key"],
    )

    op.create_table(
        "school_fact_sources",
        _uuid("id"),
        _uuid("school_id"),
        sa.Column("source_key", sa.String(length=255), nullable=False),
        sa.Column("source_kind", sa.String(length=20), nullable=False),
        sa.Column("source_ref", sa.String(length=1000), nullable=False),
        sa.Column(
            "schema_version",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("1"),
        ),
        _created_at(),
        sa.PrimaryKeyConstraint("id", name="pk_school_fact_sources"),
        sa.UniqueConstraint(
            "school_id",
            "id",
            name="uq_school_fact_sources_school_id_id",
        ),
        sa.UniqueConstraint(
            "school_id",
            "source_key",
            name="uq_school_fact_sources_school_source_key",
        ),
        sa.ForeignKeyConstraint(
            ["school_id"],
            ["schools.id"],
            name="fk_school_fact_sources_school",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "source_kind IN ('manual', 'excel', 'system', 'legacy')",
            name="ck_school_fact_sources_kind",
        ),
        sa.CheckConstraint(
            "schema_version >= 1",
            name="ck_school_fact_sources_schema_version_positive",
        ),
    )
    op.create_index(
        "ix_school_fact_sources_lookup",
        "school_fact_sources",
        ["school_id", "source_kind", "source_ref"],
    )

    op.create_table(
        "school_versions",
        _uuid("id"),
        _uuid("school_id"),
        sa.Column("business_code", sa.String(length=80), nullable=False),
        sa.Column("display_name", sa.String(length=200), nullable=False),
        sa.Column("short_name", sa.String(length=120), nullable=True),
        sa.Column("timezone_name", sa.String(length=64), nullable=False),
        sa.Column("lifecycle_status", sa.String(length=20), nullable=False),
        *_validity_columns(),
        sa.PrimaryKeyConstraint("id", name="pk_school_versions"),
        sa.UniqueConstraint("school_id", "id", name="uq_school_versions_school_id_id"),
        sa.ForeignKeyConstraint(
            ["school_id"],
            ["schools.id"],
            name="fk_school_versions_school",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "lifecycle_status IN ('active', 'inactive')",
            name="ck_school_versions_lifecycle_status",
        ),
        sa.CheckConstraint(
            "valid_to IS NULL OR valid_to > valid_from",
            name="ck_school_versions_valid_interval",
        ),
        sa.CheckConstraint("version >= 1", name="ck_school_versions_version_positive"),
        postgresql.ExcludeConstraint(
            ("school_id", "="),
            (_VALID_RANGE, "&&"),
            using="gist",
            name="excl_school_versions_period",
        ),
    )
    _source_foreign_key("school_versions", "fk_school_versions_school_source")
    _batch_foreign_key("school_versions", "fk_school_versions_school_import_batch")
    op.create_index(
        "ix_school_versions_as_of",
        "school_versions",
        ["school_id", "valid_from", "valid_to"],
    )

    op.create_table(
        "school_person_profile_versions",
        _uuid("id"),
        _uuid("school_id"),
        _uuid("person_id"),
        sa.Column("business_code", sa.String(length=80), nullable=True),
        sa.Column("person_kind", sa.String(length=40), nullable=False),
        sa.Column("lifecycle_status", sa.String(length=20), nullable=False),
        *_validity_columns(),
        sa.PrimaryKeyConstraint("id", name="pk_school_person_profile_versions"),
        sa.ForeignKeyConstraint(
            ["school_id", "person_id"],
            ["school_people.school_id", "school_people.id"],
            name="fk_school_person_profile_versions_person",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "person_kind IN ('staff', 'student_contact', 'other')",
            name="ck_school_person_profile_versions_kind",
        ),
        sa.CheckConstraint(
            "lifecycle_status IN ('active', 'inactive')",
            name="ck_school_person_profile_versions_lifecycle_status",
        ),
        sa.CheckConstraint(
            "valid_to IS NULL OR valid_to > valid_from",
            name="ck_school_person_profile_versions_valid_interval",
        ),
        sa.CheckConstraint(
            "version >= 1",
            name="ck_school_person_profile_versions_version_positive",
        ),
        postgresql.ExcludeConstraint(
            ("school_id", "="),
            ("person_id", "="),
            (_VALID_RANGE, "&&"),
            using="gist",
            name="excl_school_person_profile_versions_period",
        ),
    )
    _source_foreign_key(
        "school_person_profile_versions",
        "fk_school_person_profile_versions_source",
    )
    _batch_foreign_key(
        "school_person_profile_versions",
        "fk_school_person_profile_versions_import_batch",
    )
    op.create_index(
        "ix_school_person_profile_versions_as_of",
        "school_person_profile_versions",
        ["school_id", "person_id", "valid_from", "valid_to"],
    )

    op.create_table(
        "person_contact_points",
        _uuid("id"),
        _uuid("school_id"),
        _uuid("person_id"),
        sa.Column("contact_kind", sa.String(length=20), nullable=False),
        sa.Column("contact_value", sa.String(length=320), nullable=False),
        sa.Column(
            "is_primary",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "visibility_scope",
            sa.String(length=30),
            nullable=False,
            server_default=sa.text("'school_authenticated'"),
        ),
        sa.Column("audience_code", sa.String(length=80), nullable=True),
        sa.Column("consent_reference", sa.String(length=255), nullable=True),
        *_validity_columns(),
        sa.PrimaryKeyConstraint("id", name="pk_person_contact_points"),
        sa.ForeignKeyConstraint(
            ["school_id", "person_id"],
            ["school_people.school_id", "school_people.id"],
            name="fk_person_contact_points_person",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "contact_kind IN ('mobile', 'phone', 'email')",
            name="ck_person_contact_points_kind",
        ),
        sa.CheckConstraint(
            "(visibility_scope = 'private' AND audience_code IS NULL) OR "
            "(visibility_scope = 'school_authenticated' AND audience_code IS NULL) OR "
            "(visibility_scope = 'declared_audience' AND audience_code IS NOT NULL)",
            name="ck_person_contact_points_visibility",
        ),
        sa.CheckConstraint(
            "valid_to IS NULL OR valid_to > valid_from",
            name="ck_person_contact_points_valid_interval",
        ),
        sa.CheckConstraint("version >= 1", name="ck_person_contact_points_version_positive"),
        postgresql.ExcludeConstraint(
            ("school_id", "="),
            ("person_id", "="),
            ("contact_kind", "="),
            ("contact_value", "="),
            (_VALID_RANGE, "&&"),
            using="gist",
            name="excl_person_contact_points_period",
        ),
    )
    _source_foreign_key("person_contact_points", "fk_person_contact_points_source")
    _batch_foreign_key("person_contact_points", "fk_person_contact_points_import_batch")
    op.create_index(
        "ix_person_contact_points_as_of",
        "person_contact_points",
        ["school_id", "person_id", "valid_from", "valid_to"],
    )

    op.create_table(
        "official_contact_points",
        _uuid("id"),
        _uuid("school_id"),
        sa.Column("owner_kind", sa.String(length=20), nullable=False),
        _uuid("organization_unit_id", nullable=True),
        _uuid("location_id", nullable=True),
        sa.Column("contact_kind", sa.String(length=20), nullable=False),
        sa.Column("contact_value", sa.String(length=320), nullable=False),
        sa.Column("service_hours", sa.String(length=500), nullable=True),
        sa.Column("lifecycle_status", sa.String(length=20), nullable=False),
        sa.Column(
            "visibility_scope",
            sa.String(length=30),
            nullable=False,
            server_default=sa.text("'public'"),
        ),
        sa.Column("audience_code", sa.String(length=80), nullable=True),
        *_validity_columns(),
        sa.PrimaryKeyConstraint("id", name="pk_official_contact_points"),
        sa.ForeignKeyConstraint(
            ["school_id"],
            ["schools.id"],
            name="fk_official_contact_points_school",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["school_id", "organization_unit_id"],
            ["organization_units.school_id", "organization_units.id"],
            name="fk_official_contact_points_organization",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["school_id", "location_id"],
            ["campus_locations.school_id", "campus_locations.id"],
            name="fk_official_contact_points_location",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "(owner_kind = 'school' AND organization_unit_id IS NULL "
            "AND location_id IS NULL) OR (owner_kind = 'organization' "
            "AND organization_unit_id IS NOT NULL AND location_id IS NULL) "
            "OR (owner_kind = 'location' AND organization_unit_id IS NULL "
            "AND location_id IS NOT NULL)",
            name="ck_official_contact_points_owner",
        ),
        sa.CheckConstraint(
            "contact_kind IN ('phone', 'email')",
            name="ck_official_contact_points_kind",
        ),
        sa.CheckConstraint(
            "lifecycle_status IN ('active', 'inactive')",
            name="ck_official_contact_points_lifecycle_status",
        ),
        sa.CheckConstraint(
            "(visibility_scope IN ('public', 'school_authenticated') "
            "AND audience_code IS NULL) OR "
            "(visibility_scope = 'restricted' AND audience_code IS NOT NULL)",
            name="ck_official_contact_points_visibility",
        ),
        sa.CheckConstraint(
            "valid_to IS NULL OR valid_to > valid_from",
            name="ck_official_contact_points_valid_interval",
        ),
        sa.CheckConstraint("version >= 1", name="ck_official_contact_points_version_positive"),
    )
    _source_foreign_key("official_contact_points", "fk_official_contact_points_source")
    _batch_foreign_key("official_contact_points", "fk_official_contact_points_import_batch")
    op.create_index(
        "ix_official_contact_points_as_of",
        "official_contact_points",
        ["school_id", "owner_kind", "valid_from", "valid_to"],
    )

    op.create_table(
        "campus_location_aliases",
        _uuid("id"),
        _uuid("school_id"),
        _uuid("location_id"),
        sa.Column("alias", sa.String(length=200), nullable=False),
        sa.Column("alias_kind", sa.String(length=20), nullable=False),
        *_validity_columns(),
        sa.PrimaryKeyConstraint("id", name="pk_campus_location_aliases"),
        sa.ForeignKeyConstraint(
            ["school_id", "location_id"],
            ["campus_locations.school_id", "campus_locations.id"],
            name="fk_campus_location_aliases_location",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "alias_kind IN ('former', 'common', 'short')",
            name="ck_campus_location_aliases_kind",
        ),
        sa.CheckConstraint(
            "valid_to IS NULL OR valid_to > valid_from",
            name="ck_campus_location_aliases_valid_interval",
        ),
        sa.CheckConstraint("version >= 1", name="ck_campus_location_aliases_version_positive"),
        postgresql.ExcludeConstraint(
            ("school_id", "="),
            ("location_id", "="),
            ("alias", "="),
            (_VALID_RANGE, "&&"),
            using="gist",
            name="excl_campus_location_aliases_period",
        ),
    )
    _source_foreign_key("campus_location_aliases", "fk_campus_location_aliases_source")
    _batch_foreign_key(
        "campus_location_aliases",
        "fk_campus_location_aliases_import_batch",
    )
    op.create_index(
        "ix_campus_location_aliases_lookup",
        "campus_location_aliases",
        ["school_id", "alias", "valid_from", "valid_to"],
    )

    op.create_table(
        "dorm_room_profile_versions",
        _uuid("id"),
        _uuid("school_id"),
        _uuid("room_location_id"),
        sa.Column("authorized_bed_capacity", sa.BigInteger(), nullable=False),
        sa.Column("accommodation_zone", sa.String(length=80), nullable=False),
        sa.Column("is_available", sa.Boolean(), nullable=False),
        sa.Column(
            "accessibility_tags",
            postgresql.ARRAY(sa.String(length=80)),
            nullable=False,
            server_default=sa.text("ARRAY[]::varchar[]"),
        ),
        *_validity_columns(),
        sa.PrimaryKeyConstraint("id", name="pk_dorm_room_profile_versions"),
        sa.ForeignKeyConstraint(
            ["school_id", "room_location_id"],
            ["campus_locations.school_id", "campus_locations.id"],
            name="fk_dorm_room_profile_versions_room",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "authorized_bed_capacity >= 0",
            name="ck_dorm_room_profile_versions_capacity",
        ),
        sa.CheckConstraint(
            "valid_to IS NULL OR valid_to > valid_from",
            name="ck_dorm_room_profile_versions_valid_interval",
        ),
        sa.CheckConstraint(
            "version >= 1",
            name="ck_dorm_room_profile_versions_version_positive",
        ),
        postgresql.ExcludeConstraint(
            ("school_id", "="),
            ("room_location_id", "="),
            (_VALID_RANGE, "&&"),
            using="gist",
            name="excl_dorm_room_profile_versions_period",
        ),
    )
    _source_foreign_key(
        "dorm_room_profile_versions",
        "fk_dorm_room_profile_versions_source",
    )
    _batch_foreign_key(
        "dorm_room_profile_versions",
        "fk_dorm_room_profile_versions_import_batch",
    )
    op.create_index(
        "ix_dorm_room_profile_versions_as_of",
        "dorm_room_profile_versions",
        ["school_id", "room_location_id", "valid_from", "valid_to"],
    )

    op.create_table(
        "dorm_beds",
        _uuid("id"),
        _uuid("school_id"),
        _uuid("room_location_id"),
        sa.Column(
            "source_key",
            sa.String(length=255),
            nullable=False,
            server_default=_LOCAL_SOURCE_DEFAULT,
        ),
        _created_at(),
        sa.PrimaryKeyConstraint("id", name="pk_dorm_beds"),
        sa.UniqueConstraint("school_id", "id", name="uq_dorm_beds_school_id_id"),
        sa.UniqueConstraint(
            "school_id",
            "source_key",
            name="uq_dorm_beds_school_source_key",
        ),
        sa.ForeignKeyConstraint(
            ["school_id", "room_location_id"],
            ["campus_locations.school_id", "campus_locations.id"],
            name="fk_dorm_beds_room",
            ondelete="RESTRICT",
        ),
    )
    op.create_index(
        "ix_dorm_beds_room",
        "dorm_beds",
        ["school_id", "room_location_id", "id"],
    )

    op.create_table(
        "dorm_bed_versions",
        _uuid("id"),
        _uuid("school_id"),
        _uuid("bed_id"),
        sa.Column("business_code", sa.String(length=80), nullable=False),
        sa.Column("display_name", sa.String(length=120), nullable=False),
        sa.Column("availability", sa.String(length=20), nullable=False),
        *_validity_columns(),
        sa.PrimaryKeyConstraint("id", name="pk_dorm_bed_versions"),
        sa.ForeignKeyConstraint(
            ["school_id", "bed_id"],
            ["dorm_beds.school_id", "dorm_beds.id"],
            name="fk_dorm_bed_versions_bed",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "availability IN ('available', 'unavailable', 'inactive')",
            name="ck_dorm_bed_versions_availability",
        ),
        sa.CheckConstraint(
            "valid_to IS NULL OR valid_to > valid_from",
            name="ck_dorm_bed_versions_valid_interval",
        ),
        sa.CheckConstraint("version >= 1", name="ck_dorm_bed_versions_version_positive"),
        postgresql.ExcludeConstraint(
            ("school_id", "="),
            ("bed_id", "="),
            (_VALID_RANGE, "&&"),
            using="gist",
            name="excl_dorm_bed_versions_period",
        ),
    )
    _source_foreign_key("dorm_bed_versions", "fk_dorm_bed_versions_source")
    _batch_foreign_key("dorm_bed_versions", "fk_dorm_bed_versions_import_batch")
    op.create_index(
        "ix_dorm_bed_versions_as_of",
        "dorm_bed_versions",
        ["school_id", "bed_id", "valid_from", "valid_to"],
    )

    op.create_table(
        "student_school_relationships",
        _uuid("id"),
        _uuid("school_id"),
        _uuid("student_id"),
        sa.Column("student_number", sa.String(length=80), nullable=False),
        sa.Column("enrollment_status", sa.String(length=20), nullable=False),
        sa.Column("residence_mode", sa.String(length=20), nullable=False),
        *_validity_columns(),
        sa.PrimaryKeyConstraint("id", name="pk_student_school_relationships"),
        sa.UniqueConstraint(
            "school_id",
            "id",
            name="uq_student_school_relationships_school_id_id",
        ),
        sa.ForeignKeyConstraint(
            ["school_id", "student_id"],
            ["students.school_id", "students.id"],
            name="fk_student_school_relationships_student",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "enrollment_status IN "
            "('enrolled', 'suspended', 'transferred_out', 'withdrawn', 'graduated')",
            name="ck_student_school_relationships_enrollment_status",
        ),
        sa.CheckConstraint(
            "residence_mode IN ('day', 'boarding')",
            name="ck_student_school_relationships_residence_mode",
        ),
        sa.CheckConstraint(
            "valid_to IS NULL OR valid_to > valid_from",
            name="ck_student_school_relationships_valid_interval",
        ),
        sa.CheckConstraint(
            "version >= 1",
            name="ck_student_school_relationships_version_positive",
        ),
        postgresql.ExcludeConstraint(
            ("school_id", "="),
            ("student_id", "="),
            (_VALID_RANGE, "&&"),
            using="gist",
            name="excl_student_school_relationships_period",
        ),
    )
    _source_foreign_key(
        "student_school_relationships",
        "fk_student_school_relationships_source",
    )
    _batch_foreign_key(
        "student_school_relationships",
        "fk_student_school_relationships_import_batch",
    )
    op.create_index(
        "ix_student_school_relationships_as_of",
        "student_school_relationships",
        ["school_id", "student_id", "valid_from", "valid_to"],
    )

    op.create_table(
        "student_bed_residences",
        _uuid("id"),
        _uuid("school_id"),
        _uuid("student_id"),
        _uuid("bed_id"),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_to", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("version", sa.BigInteger(), nullable=False, server_default=sa.text("1")),
        _uuid("source_id"),
        _uuid("import_batch_id", nullable=True),
        _uuid("arrived_from_residence_id", nullable=True),
        _uuid("departed_to_residence_id", nullable=True),
        _created_at(),
        sa.PrimaryKeyConstraint("id", name="pk_student_bed_residences"),
        sa.UniqueConstraint(
            "school_id",
            "id",
            name="uq_student_bed_residences_school_id_id",
        ),
        sa.UniqueConstraint(
            "school_id",
            "arrived_from_residence_id",
            name="uq_student_bed_residences_arrived_from",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.UniqueConstraint(
            "school_id",
            "departed_to_residence_id",
            name="uq_student_bed_residences_departed_to",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.ForeignKeyConstraint(
            ["school_id", "student_id"],
            ["students.school_id", "students.id"],
            name="fk_student_bed_residences_student",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["school_id", "bed_id"],
            ["dorm_beds.school_id", "dorm_beds.id"],
            name="fk_student_bed_residences_bed",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["school_id", "arrived_from_residence_id"],
            ["student_bed_residences.school_id", "student_bed_residences.id"],
            name="fk_student_bed_residences_arrived_from",
            ondelete="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.ForeignKeyConstraint(
            ["school_id", "departed_to_residence_id"],
            ["student_bed_residences.school_id", "student_bed_residences.id"],
            name="fk_student_bed_residences_departed_to",
            ondelete="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.CheckConstraint(
            "valid_to IS NULL OR valid_to > valid_from",
            name="ck_student_bed_residences_valid_interval",
        ),
        sa.CheckConstraint("version >= 1", name="ck_student_bed_residences_version_positive"),
        sa.CheckConstraint(
            "arrived_from_residence_id IS NULL OR arrived_from_residence_id <> id",
            name="ck_student_bed_residences_arrived_from_not_self",
        ),
        sa.CheckConstraint(
            "departed_to_residence_id IS NULL OR departed_to_residence_id <> id",
            name="ck_student_bed_residences_departed_to_not_self",
        ),
        postgresql.ExcludeConstraint(
            ("school_id", "="),
            ("student_id", "="),
            (_VALID_RANGE, "&&"),
            where=sa.text("revoked_at IS NULL"),
            using="gist",
            name="excl_student_bed_residences_student_period",
        ),
        postgresql.ExcludeConstraint(
            ("school_id", "="),
            ("bed_id", "="),
            (_VALID_RANGE, "&&"),
            where=sa.text("revoked_at IS NULL"),
            using="gist",
            name="excl_student_bed_residences_bed_period",
        ),
    )
    _source_foreign_key("student_bed_residences", "fk_student_bed_residences_source")
    _batch_foreign_key(
        "student_bed_residences",
        "fk_student_bed_residences_import_batch",
    )
    op.create_index(
        "ix_student_bed_residences_student_as_of",
        "student_bed_residences",
        ["school_id", "student_id", "valid_from", "valid_to"],
    )
    op.create_index(
        "ix_student_bed_residences_bed_as_of",
        "student_bed_residences",
        ["school_id", "bed_id", "valid_from", "valid_to"],
    )
    op.create_table(
        "student_contact_relationships",
        _uuid("id"),
        _uuid("school_id"),
        _uuid("student_id"),
        _uuid("contact_person_id"),
        sa.Column("relationship_kind", sa.String(length=40), nullable=False),
        sa.Column("priority", sa.BigInteger(), nullable=False),
        sa.Column(
            "is_emergency",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        *_validity_columns(),
        sa.PrimaryKeyConstraint("id", name="pk_student_contact_relationships"),
        sa.ForeignKeyConstraint(
            ["school_id", "student_id"],
            ["students.school_id", "students.id"],
            name="fk_student_contact_relationships_student",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["school_id", "contact_person_id"],
            ["school_people.school_id", "school_people.id"],
            name="fk_student_contact_relationships_person",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "relationship_kind IN ('father', 'mother', 'grandfather', 'grandmother', 'other')",
            name="ck_student_contact_relationships_kind",
        ),
        sa.CheckConstraint(
            "priority >= 1",
            name="ck_student_contact_relationships_priority",
        ),
        sa.CheckConstraint(
            "valid_to IS NULL OR valid_to > valid_from",
            name="ck_student_contact_relationships_valid_interval",
        ),
        sa.CheckConstraint(
            "version >= 1",
            name="ck_student_contact_relationships_version_positive",
        ),
        postgresql.ExcludeConstraint(
            ("school_id", "="),
            ("student_id", "="),
            ("contact_person_id", "="),
            ("relationship_kind", "="),
            (_VALID_RANGE, "&&"),
            using="gist",
            name="excl_student_contact_relationships_period",
        ),
    )
    _source_foreign_key(
        "student_contact_relationships",
        "fk_student_contact_relationships_source",
    )
    _batch_foreign_key(
        "student_contact_relationships",
        "fk_student_contact_relationships_import_batch",
    )
    op.create_index(
        "ix_student_contact_relationships_as_of",
        "student_contact_relationships",
        ["school_id", "student_id", "priority", "valid_from", "valid_to"],
    )

    op.create_table(
        "school_policies",
        _uuid("id"),
        _uuid("school_id"),
        sa.Column("source_key", sa.String(length=255), nullable=False),
        _created_at(),
        sa.PrimaryKeyConstraint("id", name="pk_school_policies"),
        sa.UniqueConstraint("school_id", "id", name="uq_school_policies_school_id_id"),
        sa.UniqueConstraint(
            "school_id",
            "source_key",
            name="uq_school_policies_school_source_key",
        ),
        sa.ForeignKeyConstraint(
            ["school_id"],
            ["schools.id"],
            name="fk_school_policies_school",
            ondelete="RESTRICT",
        ),
    )

    op.create_table(
        "school_policy_versions",
        _uuid("id"),
        _uuid("school_id"),
        _uuid("policy_id"),
        sa.Column("business_code", sa.String(length=80), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("category_code", sa.String(length=80), nullable=False),
        sa.Column("classification", sa.String(length=20), nullable=False),
        _uuid("publishing_organization_unit_id", nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("effective_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("effective_to", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "valid_from",
            sa.DateTime(timezone=True),
            sa.Computed("effective_from", persisted=True),
            nullable=False,
        ),
        sa.Column(
            "valid_to",
            sa.DateTime(timezone=True),
            sa.Computed("effective_to", persisted=True),
            nullable=True,
        ),
        sa.Column("content_text", sa.Text(), nullable=True),
        sa.Column("version_note", sa.String(length=500), nullable=True),
        _uuid("supersedes_policy_version_id", nullable=True),
        sa.Column("version", sa.BigInteger(), nullable=False, server_default=sa.text("1")),
        _uuid("source_id"),
        _uuid("import_batch_id", nullable=True),
        _created_at(),
        sa.PrimaryKeyConstraint("id", name="pk_school_policy_versions"),
        sa.UniqueConstraint(
            "school_id",
            "id",
            name="uq_school_policy_versions_school_id_id",
        ),
        sa.ForeignKeyConstraint(
            ["school_id", "policy_id"],
            ["school_policies.school_id", "school_policies.id"],
            name="fk_school_policy_versions_policy",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["school_id", "publishing_organization_unit_id"],
            ["organization_units.school_id", "organization_units.id"],
            name="fk_school_policy_versions_publishing_organization",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["school_id", "supersedes_policy_version_id"],
            ["school_policy_versions.school_id", "school_policy_versions.id"],
            name="fk_school_policy_versions_supersedes",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "classification IN ('public', 'internal', 'restricted')",
            name="ck_school_policy_versions_classification",
        ),
        sa.CheckConstraint(
            "valid_to IS NULL OR valid_to > valid_from",
            name="ck_school_policy_versions_valid_interval",
        ),
        sa.CheckConstraint("version >= 1", name="ck_school_policy_versions_version_positive"),
        postgresql.ExcludeConstraint(
            ("school_id", "="),
            ("policy_id", "="),
            (_VALID_RANGE, "&&"),
            using="gist",
            name="excl_school_policy_versions_period",
        ),
    )
    _source_foreign_key("school_policy_versions", "fk_school_policy_versions_source")
    _batch_foreign_key(
        "school_policy_versions",
        "fk_school_policy_versions_import_batch",
    )
    op.create_index(
        "ix_school_policy_versions_as_of",
        "school_policy_versions",
        ["school_id", "policy_id", "effective_from", "effective_to"],
    )

    op.execute(
        sa.text(
            r"""
            CREATE FUNCTION public.sf1_lock_student_relationship_root()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $function$
            BEGIN
                IF TG_OP = 'UPDATE'
                   AND (
                       NEW.school_id IS DISTINCT FROM OLD.school_id
                       OR NEW.student_id IS DISTINCT FROM OLD.student_id
                   ) THEN
                    RAISE EXCEPTION USING
                        ERRCODE = '55000',
                        MESSAGE = 'student relationship stable identity is immutable';
                END IF;

                PERFORM 1
                FROM public.students AS student
                WHERE student.school_id = NEW.school_id
                  AND student.id = NEW.student_id
                FOR UPDATE;
                RETURN NEW;
            END
            $function$;

            CREATE TRIGGER trg_sf1_lock_student_relationship_root
            BEFORE INSERT OR UPDATE ON public.student_school_relationships
            FOR EACH ROW
            EXECUTE FUNCTION public.sf1_lock_student_relationship_root();

            CREATE FUNCTION public.sf1_lock_student_residence_roots()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $function$
            DECLARE
                room_id uuid;
            BEGIN
                IF TG_OP = 'UPDATE'
                   AND (
                       NEW.school_id IS DISTINCT FROM OLD.school_id
                       OR NEW.student_id IS DISTINCT FROM OLD.student_id
                       OR NEW.bed_id IS DISTINCT FROM OLD.bed_id
                   ) THEN
                    RAISE EXCEPTION USING
                        ERRCODE = '55000',
                        MESSAGE = 'student residence stable identity is immutable';
                END IF;

                SELECT bed.room_location_id
                INTO room_id
                FROM public.dorm_beds AS bed
                WHERE bed.school_id = NEW.school_id
                  AND bed.id = NEW.bed_id;

                PERFORM 1
                FROM public.students AS student
                WHERE student.school_id = NEW.school_id
                  AND student.id = NEW.student_id
                FOR UPDATE;

                PERFORM 1
                FROM public.campus_locations AS location
                WHERE location.school_id = NEW.school_id
                  AND location.id = room_id
                FOR UPDATE;

                PERFORM 1
                FROM public.dorm_beds AS bed
                WHERE bed.school_id = NEW.school_id
                  AND bed.id = NEW.bed_id
                FOR UPDATE;
                RETURN NEW;
            END
            $function$;

            CREATE TRIGGER trg_sf1_lock_student_residence_roots
            BEFORE INSERT OR UPDATE ON public.student_bed_residences
            FOR EACH ROW
            EXECUTE FUNCTION public.sf1_lock_student_residence_roots();

            CREATE FUNCTION public.sf1_reject_dorm_bed_identity_update()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $function$
            BEGIN
                IF NEW.school_id IS DISTINCT FROM OLD.school_id
                   OR NEW.room_location_id IS DISTINCT FROM OLD.room_location_id THEN
                    RAISE EXCEPTION USING
                        ERRCODE = '55000',
                        MESSAGE = 'dorm bed school and room identity are immutable';
                END IF;
                RETURN NEW;
            END
            $function$;

            CREATE TRIGGER trg_sf1_immutable_dorm_bed_identity
            BEFORE UPDATE OF school_id, room_location_id ON public.dorm_beds
            FOR EACH ROW
            EXECUTE FUNCTION public.sf1_reject_dorm_bed_identity_update();
            """
        )
    )

    op.execute(
        sa.text(
            r"""
            CREATE FUNCTION public.sf1_validate_room_profile()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $function$
            DECLARE
                location_kind text;
                available_beds bigint;
            BEGIN
                IF TG_OP = 'UPDATE'
                   AND (
                       NEW.school_id IS DISTINCT FROM OLD.school_id
                       OR NEW.room_location_id IS DISTINCT FROM OLD.room_location_id
                   ) THEN
                    RAISE EXCEPTION USING
                        ERRCODE = '55000',
                        MESSAGE = 'dorm room profile stable identity is immutable';
                END IF;

                SELECT location.kind
                INTO location_kind
                FROM public.campus_locations AS location
                WHERE location.school_id = NEW.school_id
                  AND location.id = NEW.room_location_id
                FOR UPDATE;

                IF location_kind IS DISTINCT FROM 'dorm_room' THEN
                    RAISE EXCEPTION USING
                        ERRCODE = '23514',
                        CONSTRAINT = 'ck_dorm_room_profile_versions_room_kind',
                        MESSAGE = 'dorm room profile must reference a dorm_room location';
                END IF;

                SELECT count(DISTINCT version.bed_id)
                INTO available_beds
                FROM public.dorm_beds AS bed
                JOIN public.dorm_bed_versions AS version
                  ON version.school_id = bed.school_id
                 AND version.bed_id = bed.id
                WHERE bed.school_id = NEW.school_id
                  AND bed.room_location_id = NEW.room_location_id
                  AND version.availability = 'available'
                  AND tstzrange(
                          version.valid_from,
                          COALESCE(version.valid_to, 'infinity'::timestamptz),
                          '[)'
                      ) && tstzrange(
                          NEW.valid_from,
                          COALESCE(NEW.valid_to, 'infinity'::timestamptz),
                          '[)'
                      );

                IF available_beds > NEW.authorized_bed_capacity THEN
                    RAISE EXCEPTION USING
                        ERRCODE = '23514',
                        CONSTRAINT = 'ck_dorm_room_profile_versions_available_bed_capacity',
                        MESSAGE = 'available beds exceed authorized room capacity';
                END IF;
                RETURN NEW;
            END
            $function$;

            CREATE TRIGGER trg_sf1_validate_room_profile
            BEFORE INSERT OR UPDATE ON public.dorm_room_profile_versions
            FOR EACH ROW
            EXECUTE FUNCTION public.sf1_validate_room_profile();
            """
        )
    )

    op.execute(
        sa.text(
            r"""
            CREATE FUNCTION public.sf1_validate_bed_version()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $function$
            DECLARE
                room_id uuid;
                location_kind text;
                room_capacity bigint;
                other_available_beds bigint;
            BEGIN
                IF TG_OP = 'UPDATE'
                   AND (
                       NEW.school_id IS DISTINCT FROM OLD.school_id
                       OR NEW.bed_id IS DISTINCT FROM OLD.bed_id
                   ) THEN
                    RAISE EXCEPTION USING
                        ERRCODE = '55000',
                        MESSAGE = 'dorm bed version stable identity is immutable';
                END IF;

                SELECT bed.room_location_id
                INTO room_id
                FROM public.dorm_beds AS bed
                WHERE bed.school_id = NEW.school_id
                  AND bed.id = NEW.bed_id;

                SELECT location.kind
                INTO location_kind
                FROM public.campus_locations AS location
                WHERE location.school_id = NEW.school_id
                  AND location.id = room_id
                FOR UPDATE;

                PERFORM 1
                FROM public.dorm_beds AS bed
                WHERE bed.school_id = NEW.school_id
                  AND bed.id = NEW.bed_id
                FOR UPDATE;

                IF location_kind IS DISTINCT FROM 'dorm_room' THEN
                    RAISE EXCEPTION USING
                        ERRCODE = '23514',
                        CONSTRAINT = 'ck_dorm_bed_versions_room_kind',
                        MESSAGE = 'dorm bed must belong to a dorm_room location';
                END IF;

                IF NEW.availability <> 'available' THEN
                    RETURN NEW;
                END IF;

                SELECT profile.authorized_bed_capacity
                INTO room_capacity
                FROM public.dorm_room_profile_versions AS profile
                WHERE profile.school_id = NEW.school_id
                  AND profile.room_location_id = room_id
                  AND profile.is_available
                  AND profile.valid_from <= NEW.valid_from
                  AND (profile.valid_to IS NULL OR (
                          NEW.valid_to IS NOT NULL
                          AND profile.valid_to >= NEW.valid_to
                      ))
                  AND (NEW.valid_to IS NOT NULL OR profile.valid_to IS NULL)
                LIMIT 1;

                IF room_capacity IS NULL THEN
                    RAISE EXCEPTION USING
                        ERRCODE = '23514',
                        CONSTRAINT = 'ck_dorm_bed_versions_room_available',
                        MESSAGE = 'available bed requires an available room profile';
                END IF;

                SELECT count(DISTINCT version.bed_id)
                INTO other_available_beds
                FROM public.dorm_beds AS bed
                JOIN public.dorm_bed_versions AS version
                  ON version.school_id = bed.school_id
                 AND version.bed_id = bed.id
                WHERE bed.school_id = NEW.school_id
                  AND bed.room_location_id = room_id
                  AND version.availability = 'available'
                  AND version.id <> NEW.id
                  AND tstzrange(
                          version.valid_from,
                          COALESCE(version.valid_to, 'infinity'::timestamptz),
                          '[)'
                      ) && tstzrange(
                          NEW.valid_from,
                          COALESCE(NEW.valid_to, 'infinity'::timestamptz),
                          '[)'
                      );

                IF other_available_beds + 1 > room_capacity THEN
                    RAISE EXCEPTION USING
                        ERRCODE = '23514',
                        CONSTRAINT = 'ck_dorm_bed_versions_room_capacity',
                        MESSAGE = 'available beds exceed authorized room capacity';
                END IF;
                RETURN NEW;
            END
            $function$;

            CREATE TRIGGER trg_sf1_validate_bed_version
            BEFORE INSERT OR UPDATE ON public.dorm_bed_versions
            FOR EACH ROW
            EXECUTE FUNCTION public.sf1_validate_bed_version();
            """
        )
    )

    op.execute(
        sa.text(
            r"""
            CREATE FUNCTION public.sf1_validate_student_residence()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $function$
            DECLARE
                room_id uuid;
            BEGIN
                IF NEW.revoked_at IS NOT NULL THEN
                    RETURN NULL;
                END IF;

                PERFORM 1
                FROM public.students AS student
                WHERE student.school_id = NEW.school_id
                  AND student.id = NEW.student_id
                FOR UPDATE;

                SELECT bed.room_location_id
                INTO room_id
                FROM public.dorm_beds AS bed
                WHERE bed.school_id = NEW.school_id
                  AND bed.id = NEW.bed_id
                FOR UPDATE;

                IF NOT EXISTS (
                    SELECT 1
                    FROM public.student_school_relationships AS relationship
                    WHERE relationship.school_id = NEW.school_id
                      AND relationship.student_id = NEW.student_id
                      AND relationship.enrollment_status = 'enrolled'
                      AND relationship.residence_mode = 'boarding'
                      AND relationship.valid_from <= NEW.valid_from
                      AND (relationship.valid_to IS NULL OR (
                              NEW.valid_to IS NOT NULL
                              AND relationship.valid_to >= NEW.valid_to
                          ))
                      AND (NEW.valid_to IS NOT NULL OR relationship.valid_to IS NULL)
                ) THEN
                    RAISE EXCEPTION USING
                        ERRCODE = '23514',
                        CONSTRAINT = 'ck_student_bed_residences_enrolled_boarder',
                        MESSAGE = 'residence requires an enrolled boarding relationship';
                END IF;

                IF NOT EXISTS (
                    SELECT 1
                    FROM public.dorm_bed_versions AS version
                    WHERE version.school_id = NEW.school_id
                      AND version.bed_id = NEW.bed_id
                      AND version.availability = 'available'
                      AND version.valid_from <= NEW.valid_from
                      AND (version.valid_to IS NULL OR (
                              NEW.valid_to IS NOT NULL
                              AND version.valid_to >= NEW.valid_to
                          ))
                      AND (NEW.valid_to IS NOT NULL OR version.valid_to IS NULL)
                ) THEN
                    RAISE EXCEPTION USING
                        ERRCODE = '23514',
                        CONSTRAINT = 'ck_student_bed_residences_bed_available',
                        MESSAGE = 'residence requires an available bed version';
                END IF;

                IF NOT EXISTS (
                    SELECT 1
                    FROM public.dorm_room_profile_versions AS profile
                    WHERE profile.school_id = NEW.school_id
                      AND profile.room_location_id = room_id
                      AND profile.is_available
                      AND profile.valid_from <= NEW.valid_from
                      AND (profile.valid_to IS NULL OR (
                              NEW.valid_to IS NOT NULL
                              AND profile.valid_to >= NEW.valid_to
                          ))
                      AND (NEW.valid_to IS NOT NULL OR profile.valid_to IS NULL)
                ) THEN
                    RAISE EXCEPTION USING
                        ERRCODE = '23514',
                        CONSTRAINT = 'ck_student_bed_residences_room_available',
                        MESSAGE = 'residence requires an available dorm room profile';
                END IF;
                RETURN NULL;
            END
            $function$;

            CREATE CONSTRAINT TRIGGER trg_sf1_validate_student_residence
            AFTER INSERT OR UPDATE ON public.student_bed_residences
            DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW
            EXECUTE FUNCTION public.sf1_validate_student_residence();
            """
        )
    )

    op.execute(
        sa.text(
            r"""
            CREATE FUNCTION public.sf1_assert_student_residences_valid(
                checked_school_id uuid,
                checked_student_id uuid
            )
            RETURNS void
            LANGUAGE plpgsql
            AS $function$
            BEGIN
                IF checked_school_id IS NULL OR checked_student_id IS NULL THEN
                    RETURN;
                END IF;

                IF EXISTS (
                    SELECT 1
                    FROM public.student_bed_residences AS residence
                    WHERE residence.school_id = checked_school_id
                      AND residence.student_id = checked_student_id
                      AND residence.revoked_at IS NULL
                      AND NOT EXISTS (
                          SELECT 1
                          FROM public.student_school_relationships AS relationship
                          WHERE relationship.school_id = residence.school_id
                            AND relationship.student_id = residence.student_id
                            AND relationship.enrollment_status = 'enrolled'
                            AND relationship.residence_mode = 'boarding'
                            AND relationship.valid_from <= residence.valid_from
                            AND (relationship.valid_to IS NULL OR (
                                    residence.valid_to IS NOT NULL
                                    AND relationship.valid_to >= residence.valid_to
                                ))
                            AND (
                                residence.valid_to IS NOT NULL
                                OR relationship.valid_to IS NULL
                            )
                      )
                ) THEN
                    RAISE EXCEPTION USING
                        ERRCODE = '23514',
                        CONSTRAINT = 'ck_student_bed_residences_enrolled_boarder',
                        MESSAGE = 'relationship change would invalidate an active residence';
                END IF;
            END
            $function$;

            CREATE FUNCTION public.sf1_validate_relationship_residences()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $function$
            BEGIN
                IF TG_OP = 'UPDATE' THEN
                    PERFORM public.sf1_assert_student_residences_valid(
                        OLD.school_id,
                        OLD.student_id
                    );
                END IF;
                PERFORM public.sf1_assert_student_residences_valid(
                    NEW.school_id,
                    NEW.student_id
                );
                RETURN NULL;
            END
            $function$;

            CREATE CONSTRAINT TRIGGER trg_sf1_validate_relationship_residences
            AFTER INSERT OR UPDATE ON public.student_school_relationships
            DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW
            EXECUTE FUNCTION public.sf1_validate_relationship_residences();

            CREATE FUNCTION public.sf1_assert_bed_residences_valid(
                checked_school_id uuid,
                checked_bed_id uuid
            )
            RETURNS void
            LANGUAGE plpgsql
            AS $function$
            BEGIN
                IF checked_school_id IS NULL OR checked_bed_id IS NULL THEN
                    RETURN;
                END IF;

                IF EXISTS (
                    SELECT 1
                    FROM public.student_bed_residences AS residence
                    WHERE residence.school_id = checked_school_id
                      AND residence.bed_id = checked_bed_id
                      AND residence.revoked_at IS NULL
                      AND NOT EXISTS (
                          SELECT 1
                          FROM public.dorm_bed_versions AS version
                          WHERE version.school_id = residence.school_id
                            AND version.bed_id = residence.bed_id
                            AND version.availability = 'available'
                            AND version.valid_from <= residence.valid_from
                            AND (version.valid_to IS NULL OR (
                                    residence.valid_to IS NOT NULL
                                    AND version.valid_to >= residence.valid_to
                                ))
                            AND (
                                residence.valid_to IS NOT NULL
                                OR version.valid_to IS NULL
                            )
                      )
                ) THEN
                    RAISE EXCEPTION USING
                        ERRCODE = '23514',
                        CONSTRAINT = 'ck_student_bed_residences_bed_available',
                        MESSAGE = 'bed version change would invalidate an active residence';
                END IF;
            END
            $function$;

            CREATE FUNCTION public.sf1_validate_bed_residences()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $function$
            BEGIN
                IF TG_OP = 'UPDATE' THEN
                    PERFORM public.sf1_assert_bed_residences_valid(
                        OLD.school_id,
                        OLD.bed_id
                    );
                END IF;
                PERFORM public.sf1_assert_bed_residences_valid(
                    NEW.school_id,
                    NEW.bed_id
                );
                RETURN NULL;
            END
            $function$;

            CREATE CONSTRAINT TRIGGER trg_sf1_validate_bed_residences
            AFTER INSERT OR UPDATE ON public.dorm_bed_versions
            DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW
            EXECUTE FUNCTION public.sf1_validate_bed_residences();

            CREATE FUNCTION public.sf1_assert_room_residences_valid(
                checked_school_id uuid,
                checked_room_id uuid
            )
            RETURNS void
            LANGUAGE plpgsql
            AS $function$
            BEGIN
                IF checked_school_id IS NULL OR checked_room_id IS NULL THEN
                    RETURN;
                END IF;

                IF EXISTS (
                    SELECT 1
                    FROM public.student_bed_residences AS residence
                    JOIN public.dorm_beds AS bed
                      ON bed.school_id = residence.school_id
                     AND bed.id = residence.bed_id
                    WHERE residence.school_id = checked_school_id
                      AND bed.room_location_id = checked_room_id
                      AND residence.revoked_at IS NULL
                      AND NOT EXISTS (
                          SELECT 1
                          FROM public.dorm_room_profile_versions AS profile
                          WHERE profile.school_id = residence.school_id
                            AND profile.room_location_id = bed.room_location_id
                            AND profile.is_available
                            AND profile.valid_from <= residence.valid_from
                            AND (profile.valid_to IS NULL OR (
                                    residence.valid_to IS NOT NULL
                                    AND profile.valid_to >= residence.valid_to
                                ))
                            AND (
                                residence.valid_to IS NOT NULL
                                OR profile.valid_to IS NULL
                            )
                      )
                ) THEN
                    RAISE EXCEPTION USING
                        ERRCODE = '23514',
                        CONSTRAINT = 'ck_student_bed_residences_room_available',
                        MESSAGE = 'room profile change would invalidate an active residence';
                END IF;
            END
            $function$;

            CREATE FUNCTION public.sf1_validate_room_residences()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $function$
            BEGIN
                IF TG_OP = 'UPDATE' THEN
                    PERFORM public.sf1_assert_room_residences_valid(
                        OLD.school_id,
                        OLD.room_location_id
                    );
                END IF;
                PERFORM public.sf1_assert_room_residences_valid(
                    NEW.school_id,
                    NEW.room_location_id
                );
                RETURN NULL;
            END
            $function$;

            CREATE CONSTRAINT TRIGGER trg_sf1_validate_room_residences
            AFTER INSERT OR UPDATE ON public.dorm_room_profile_versions
            DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW
            EXECUTE FUNCTION public.sf1_validate_room_residences();
            """
        )
    )

    op.execute(
        sa.text(
            r"""
            CREATE FUNCTION public.sf1_validate_residence_transfer()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $function$
            BEGIN
                IF EXISTS (
                    SELECT 1
                    FROM public.student_bed_residences AS residence
                    LEFT JOIN public.student_bed_residences AS departure
                      ON departure.school_id = residence.school_id
                     AND departure.id = residence.arrived_from_residence_id
                    LEFT JOIN public.student_bed_residences AS arrival
                      ON arrival.school_id = residence.school_id
                     AND arrival.id = residence.departed_to_residence_id
                    WHERE residence.school_id = NEW.school_id
                      AND residence.student_id = NEW.student_id
                      AND (
                          (
                              residence.arrived_from_residence_id IS NOT NULL
                              AND departure.id IS NOT NULL
                              AND (
                                  departure.student_id IS DISTINCT FROM residence.student_id
                                  OR departure.bed_id = residence.bed_id
                                  OR departure.valid_to IS NULL
                                  OR departure.valid_to IS DISTINCT FROM residence.valid_from
                                  OR departure.departed_to_residence_id
                                     IS DISTINCT FROM residence.id
                                  OR (departure.revoked_at IS NULL)
                                     IS DISTINCT FROM (residence.revoked_at IS NULL)
                              )
                          )
                          OR (
                              residence.departed_to_residence_id IS NOT NULL
                              AND arrival.id IS NOT NULL
                              AND (
                                  arrival.student_id IS DISTINCT FROM residence.student_id
                                  OR arrival.bed_id = residence.bed_id
                                  OR residence.valid_to IS NULL
                                  OR residence.valid_to IS DISTINCT FROM arrival.valid_from
                                  OR arrival.arrived_from_residence_id IS DISTINCT FROM residence.id
                                  OR (arrival.revoked_at IS NULL)
                                     IS DISTINCT FROM (residence.revoked_at IS NULL)
                              )
                          )
                      )
                ) THEN
                    RAISE EXCEPTION USING
                        ERRCODE = '23514',
                        CONSTRAINT = 'ck_student_bed_residences_atomic_transfer',
                        MESSAGE = 'bed transfer requires reciprocal residence edges';
                END IF;

                IF EXISTS (
                    SELECT 1
                    FROM public.student_bed_residences AS departure
                    JOIN public.student_bed_residences AS arrival
                      ON arrival.school_id = departure.school_id
                     AND arrival.student_id = departure.student_id
                     AND arrival.id <> departure.id
                     AND arrival.valid_from = departure.valid_to
                    WHERE departure.school_id = NEW.school_id
                      AND departure.student_id = NEW.student_id
                      AND departure.revoked_at IS NULL
                      AND arrival.revoked_at IS NULL
                      AND departure.valid_to IS NOT NULL
                      AND departure.bed_id <> arrival.bed_id
                      AND (
                          departure.departed_to_residence_id IS DISTINCT FROM arrival.id
                          OR arrival.arrived_from_residence_id IS DISTINCT FROM departure.id
                      )
                ) THEN
                    RAISE EXCEPTION USING
                        ERRCODE = '23514',
                        CONSTRAINT = 'ck_student_bed_residences_atomic_transfer',
                        MESSAGE = 'adjacent different beds require one atomic reciprocal transfer';
                END IF;
                RETURN NULL;
            END
            $function$;

            CREATE CONSTRAINT TRIGGER trg_sf1_validate_residence_transfer
            AFTER INSERT OR UPDATE ON public.student_bed_residences
            DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW
            EXECUTE FUNCTION public.sf1_validate_residence_transfer();
            """
        )
    )

    op.execute(
        sa.text(
            r"""
            CREATE FUNCTION public.sf1_reject_history_delete()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $function$
            BEGIN
                RAISE EXCEPTION USING
                    ERRCODE = '55000',
                    MESSAGE = 'authoritative school fact history cannot be physically deleted';
            END
            $function$;

            DO $block$
            DECLARE
                table_name text;
            BEGIN
                FOREACH table_name IN ARRAY ARRAY[
                    'school_versions',
                    'school_person_names',
                    'school_person_profile_versions',
                    'account_person_bindings',
                    'person_contact_points',
                    'official_contact_points',
                    'organization_unit_versions',
                    'staff_assignments',
                    'campus_location_versions',
                    'campus_location_aliases',
                    'dorm_room_profile_versions',
                    'dorm_bed_versions',
                    'student_school_relationships',
                    'student_bed_residences',
                    'student_contact_relationships',
                    'school_policy_versions'
                ] LOOP
                    EXECUTE format(
                        'CREATE TRIGGER %I BEFORE DELETE ON public.%I '
                        'FOR EACH ROW EXECUTE FUNCTION public.sf1_reject_history_delete()',
                        'trg_sf1_reject_delete_' || table_name,
                        table_name
                    );
                END LOOP;
            END
            $block$;
            """
        )
    )

    op.execute(
        sa.text(
            r"""
            CREATE FUNCTION public.sf1_reject_source_key_update()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $function$
            BEGIN
                IF NEW.source_key IS DISTINCT FROM OLD.source_key THEN
                    RAISE EXCEPTION USING
                        ERRCODE = '55000',
                        MESSAGE = 'authoritative school fact source keys are immutable';
                END IF;
                RETURN NEW;
            END
            $function$;

            DO $block$
            DECLARE
                table_name text;
            BEGIN
                FOREACH table_name IN ARRAY ARRAY[
                    'schools',
                    'school_people',
                    'account_person_bindings',
                    'organization_units',
                    'staff_assignments',
                    'campus_locations',
                    'dorm_beds',
                    'students',
                    'school_policies',
                    'school_fact_sources'
                ] LOOP
                    EXECUTE format(
                        'CREATE TRIGGER %I BEFORE UPDATE OF source_key ON public.%I '
                        'FOR EACH ROW EXECUTE FUNCTION public.sf1_reject_source_key_update()',
                        'trg_sf1_immutable_source_key_' || table_name,
                        table_name
                    );
                END LOOP;
            END
            $block$;
            """
        )
    )
