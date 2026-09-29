"""Correct pre-SF4 school fact schema semantics.

Revision ID: 20260821_18
Revises: 20260820_17
Create Date: 2026-08-21
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "20260821_18"
down_revision = "20260820_17"
branch_labels = None
depends_on = None


def _guard_upgrade_without_unmappable_rows() -> None:
    op.execute(
        sa.text(
            r"""
            DO $upgrade_guard$
            BEGIN
                IF EXISTS (
                    SELECT 1
                    FROM public.student_bed_residences
                    WHERE arrived_from_residence_id IS NOT NULL
                       OR departed_to_residence_id IS NOT NULL
                ) THEN
                    RAISE EXCEPTION USING
                        ERRCODE = '55000',
                        MESSAGE = 'revision 18 cannot discard populated Residence self-FK data';
                END IF;

                IF EXISTS (SELECT 1 FROM public.student_school_relationships) THEN
                    RAISE EXCEPTION USING
                        ERRCODE = '55000',
                        MESSAGE = 'revision 18 requires an explicit student campus backfill';
                END IF;

                IF EXISTS (
                    SELECT 1
                    FROM public.staff_assignments
                    WHERE revoked_at IS NOT NULL
                ) OR EXISTS (
                    SELECT 1
                    FROM public.school_fact_change_lineage AS lineage
                    JOIN public.school_policy_versions AS version
                      ON version.school_id = lineage.school_id
                     AND version.id = lineage.target_id
                    WHERE lineage.fact_kind = 'policy_version'
                      AND lineage.operation = 'revoke'
                ) THEN
                    RAISE EXCEPTION USING
                        ERRCODE = '55000',
                        MESSAGE = 'revision 18 cannot reconstruct the original planned '
                                  'terminal date after a rev17 revoke';
                END IF;

                IF EXISTS (
                    SELECT 1
                    FROM public.school_fact_change_lineage AS terminal
                    JOIN public.school_fact_change_lineage AS later
                      ON later.school_id = terminal.school_id
                     AND later.fact_kind = terminal.fact_kind
                     AND later.fact_source_key = terminal.fact_source_key
                     AND later.target_version > terminal.target_version
                     AND later.operation = 'upsert'
                    WHERE terminal.fact_kind IN (
                        'school', 'person', 'student', 'organization', 'location',
                        'assignment', 'login_mapping'
                    )
                      AND terminal.operation IN ('end', 'revoke')
                ) THEN
                    RAISE EXCEPTION USING
                        ERRCODE = '55000',
                        MESSAGE = 'revision 18 cannot grandfather a rev17 terminal '
                                  'fact reopened by ordinary upsert';
                END IF;

                IF EXISTS (
                    SELECT 1
                    FROM public.schools AS stable
                    WHERE NOT EXISTS (
                              SELECT 1
                              FROM public.school_versions AS version
                              WHERE version.school_id = stable.id
                          )
                      AND (
                          stable.status <> 'active'
                          OR stable.version <> 1
                          OR EXISTS (
                              SELECT 1
                              FROM public.school_fact_change_lineage AS lineage
                              WHERE lineage.school_id = stable.id
                                AND lineage.fact_kind = 'school'
                          )
                          OR EXISTS (
                              SELECT 1
                              FROM public.school_fact_sources AS source
                              WHERE source.school_id = stable.id
                                AND (
                                    source.source_key =
                                        'revision18:legacy-school-v1:' || stable.id::text
                                    OR source.source_ref =
                                        'revision18:legacy-school-v1:' || stable.id::text
                                )
                          )
                      )
                ) THEN
                    RAISE EXCEPTION USING
                        ERRCODE = '55000',
                        MESSAGE = 'revision 18 cannot reconstruct an ambiguous legacy '
                                  'school content history';
                END IF;

            END
            $upgrade_guard$;
            """
        )
    )


def _backfill_unambiguous_legacy_school_versions() -> None:
    """Map a stable-only active v1 school to one identifiable migration content slice."""

    op.execute(
        sa.text(
            r"""
            WITH candidates AS (
                SELECT stable.id AS school_id,
                       gen_random_uuid() AS source_id,
                       'revision18:legacy-school-v1:' || stable.id::text AS marker
                FROM public.schools AS stable
                WHERE stable.status = 'active'
                  AND stable.version = 1
                  AND NOT EXISTS (
                      SELECT 1
                      FROM public.school_versions AS version
                      WHERE version.school_id = stable.id
                  )
                  AND NOT EXISTS (
                      SELECT 1
                      FROM public.school_fact_change_lineage AS lineage
                      WHERE lineage.school_id = stable.id
                        AND lineage.fact_kind = 'school'
                  )
            ), inserted_sources AS (
                INSERT INTO public.school_fact_sources (
                    id, school_id, source_key, source_kind, source_ref,
                    schema_version, created_at
                )
                SELECT source_id, school_id, marker, 'legacy', marker, 1,
                       statement_timestamp()
                FROM candidates
                RETURNING id, school_id
            )
            INSERT INTO public.school_versions (
                id, school_id, business_code, display_name, short_name,
                timezone_name, lifecycle_status, valid_from, valid_to, version,
                source_id, import_batch_id, created_at
            )
            SELECT gen_random_uuid(), stable.id, stable.school_code,
                   stable.display_name, NULL, stable.timezone_name, 'active',
                   stable.created_at, NULL, 1, source.id, NULL, stable.created_at
            FROM public.schools AS stable
            JOIN inserted_sources AS source ON source.school_id = stable.id;
            """
        )
    )


def _guard_backfilled_structure_topology() -> None:
    """Backscan topology because constraint triggers only validate future writes."""

    op.execute(
        sa.text(
            r"""
            DO $backfilled_structure_guard$
            BEGIN
                IF EXISTS (
                    WITH RECURSIVE ancestry(
                        school_id, node_id, parent_id, covered, path
                    ) AS (
                        SELECT version.school_id,
                               version.organization_unit_id,
                               version.parent_organization_unit_id,
                               tstzrange(
                                   version.valid_from,
                                   COALESCE(
                                       version.valid_to,
                                       'infinity'::timestamptz
                                   ),
                                   '[)'
                               ),
                               ARRAY[version.organization_unit_id]::uuid[]
                        FROM public.organization_unit_versions AS version
                        WHERE version.status = 'confirmed'
                          AND version.parent_organization_unit_id IS NOT NULL
                        UNION ALL
                        SELECT child.school_id,
                               parent.organization_unit_id,
                               parent.parent_organization_unit_id,
                               child.covered * tstzrange(
                                   parent.valid_from,
                                   COALESCE(
                                       parent.valid_to,
                                       'infinity'::timestamptz
                                   ),
                                   '[)'
                               ),
                               child.path || parent.organization_unit_id
                        FROM ancestry AS child
                        JOIN public.organization_unit_versions AS parent
                          ON parent.school_id = child.school_id
                         AND parent.organization_unit_id = child.parent_id
                         AND parent.status = 'confirmed'
                         AND tstzrange(
                                 parent.valid_from,
                                 COALESCE(
                                     parent.valid_to,
                                     'infinity'::timestamptz
                                 ),
                                 '[)'
                             ) && child.covered
                        WHERE NOT child.parent_id = ANY(child.path)
                    )
                    SELECT 1
                    FROM ancestry
                    WHERE parent_id = ANY(path)
                      AND NOT isempty(covered)
                ) OR EXISTS (
                    WITH RECURSIVE ancestry(
                        school_id, node_id, parent_id, covered, path
                    ) AS (
                        SELECT version.school_id,
                               version.location_id,
                               version.parent_location_id,
                               tstzrange(
                                   version.valid_from,
                                   COALESCE(
                                       version.valid_to,
                                       'infinity'::timestamptz
                                   ),
                                   '[)'
                               ),
                               ARRAY[version.location_id]::uuid[]
                        FROM public.campus_location_versions AS version
                        WHERE version.status = 'confirmed'
                          AND version.parent_location_id IS NOT NULL
                        UNION ALL
                        SELECT child.school_id,
                               parent.location_id,
                               parent.parent_location_id,
                               child.covered * tstzrange(
                                   parent.valid_from,
                                   COALESCE(
                                       parent.valid_to,
                                       'infinity'::timestamptz
                                   ),
                                   '[)'
                               ),
                               child.path || parent.location_id
                        FROM ancestry AS child
                        JOIN public.campus_location_versions AS parent
                          ON parent.school_id = child.school_id
                         AND parent.location_id = child.parent_id
                         AND parent.status = 'confirmed'
                         AND tstzrange(
                                 parent.valid_from,
                                 COALESCE(
                                     parent.valid_to,
                                     'infinity'::timestamptz
                                 ),
                                 '[)'
                             ) && child.covered
                        WHERE NOT child.parent_id = ANY(child.path)
                    )
                    SELECT 1
                    FROM ancestry
                    WHERE parent_id = ANY(path)
                      AND NOT isempty(covered)
                ) OR EXISTS (
                    SELECT 1
                    FROM public.organization_unit_versions AS child
                    WHERE child.status = 'confirmed'
                      AND child.parent_organization_unit_id IS NOT NULL
                      AND NOT COALESCE(
                          (
                              SELECT range_agg(
                                  tstzrange(
                                      parent.valid_from,
                                      COALESCE(
                                          parent.valid_to,
                                          'infinity'::timestamptz
                                      ),
                                      '[)'
                                  )
                              ) @> tstzrange(
                                  child.valid_from,
                                  COALESCE(
                                      child.valid_to,
                                      'infinity'::timestamptz
                                  ),
                                  '[)'
                              )
                              FROM public.organization_unit_versions AS parent
                              WHERE parent.school_id = child.school_id
                                AND parent.organization_unit_id =
                                    child.parent_organization_unit_id
                                AND parent.status = 'confirmed'
                          ),
                          false
                      )
                ) OR EXISTS (
                    SELECT 1
                    FROM public.campus_location_versions AS child
                    WHERE child.status = 'confirmed'
                      AND child.parent_location_id IS NOT NULL
                      AND NOT COALESCE(
                          (
                              SELECT range_agg(
                                  tstzrange(
                                      parent.valid_from,
                                      COALESCE(
                                          parent.valid_to,
                                          'infinity'::timestamptz
                                      ),
                                      '[)'
                                  )
                              ) @> tstzrange(
                                  child.valid_from,
                                  COALESCE(
                                      child.valid_to,
                                      'infinity'::timestamptz
                                  ),
                                  '[)'
                              )
                              FROM public.campus_location_versions AS parent
                              WHERE parent.school_id = child.school_id
                                AND parent.location_id = child.parent_location_id
                                AND parent.status = 'confirmed'
                          ),
                          false
                      )
                ) THEN
                    RAISE EXCEPTION USING
                        ERRCODE = '55000',
                        MESSAGE = 'revision 18 cannot reconstruct ambiguous location or '
                                  'organization structure history';
                END IF;
            END
            $backfilled_structure_guard$;
            """
        )
    )


def _add_historical_structure_slices() -> None:
    """Backfill only mechanically unambiguous rev17 structural history."""

    op.execute(
        sa.text(
            r"""
            DO $structure_history_guard$
            BEGIN
                IF EXISTS (
                    SELECT 1
                    FROM public.campus_locations AS stable
                    JOIN LATERAL (
                        SELECT count(*) AS slice_count,
                               count(*) FILTER (WHERE version.status = 'confirmed')
                                   AS confirmed_count,
                               min(version.version) AS only_version
                        FROM public.campus_location_versions AS version
                        WHERE version.school_id = stable.school_id
                          AND version.location_id = stable.id
                    ) AS history ON true
                    WHERE history.slice_count <> 1
                       OR history.confirmed_count <> 1
                       OR NOT (
                           (stable.status = 'active'
                            AND history.only_version = stable.version)
                           OR
                           (stable.status = 'inactive'
                            AND stable.version = history.only_version + 1
                            AND EXISTS (
                                SELECT 1
                                FROM public.school_fact_change_lineage AS lineage
                                WHERE lineage.school_id = stable.school_id
                                  AND lineage.fact_kind = 'location'
                                  AND lineage.fact_source_key = stable.source_key
                                  AND lineage.target_id = stable.id
                                  AND lineage.target_version = stable.version
                                  AND lineage.operation IN ('end', 'revoke')
                            ))
                       )
                ) OR EXISTS (
                    SELECT 1
                    FROM public.organization_units AS stable
                    JOIN LATERAL (
                        SELECT count(*) AS slice_count,
                               count(*) FILTER (WHERE version.status = 'confirmed')
                                   AS confirmed_count,
                               min(version.version) AS only_version
                        FROM public.organization_unit_versions AS version
                        WHERE version.school_id = stable.school_id
                          AND version.organization_unit_id = stable.id
                    ) AS history ON true
                    WHERE history.slice_count <> 1
                       OR history.confirmed_count <> 1
                       OR NOT (
                           (stable.status = 'active'
                            AND history.only_version = stable.version)
                           OR
                           (stable.status = 'inactive'
                            AND stable.version = history.only_version + 1
                            AND EXISTS (
                                SELECT 1
                                FROM public.school_fact_change_lineage AS lineage
                                WHERE lineage.school_id = stable.school_id
                                  AND lineage.fact_kind = 'organization'
                                  AND lineage.fact_source_key = stable.source_key
                                  AND lineage.target_id = stable.id
                                  AND lineage.target_version = stable.version
                                  AND lineage.operation IN ('end', 'revoke')
                            ))
                       )
                ) THEN
                    RAISE EXCEPTION USING
                        ERRCODE = '55000',
                        MESSAGE = 'revision 18 cannot reconstruct ambiguous location or '
                                  'organization structure history';
                END IF;
            END
            $structure_history_guard$;
            """
        )
    )
    op.add_column(
        "campus_location_versions",
        sa.Column("parent_location_id", sa.Uuid(), nullable=True),
    )
    op.add_column(
        "campus_location_versions",
        sa.Column("kind", sa.String(length=40), nullable=True),
    )
    op.add_column(
        "organization_unit_versions",
        sa.Column("parent_organization_unit_id", sa.Uuid(), nullable=True),
    )
    op.execute(
        "UPDATE public.campus_location_versions AS version "
        "SET parent_location_id = stable.parent_location_id, kind = stable.kind "
        "FROM public.campus_locations AS stable "
        "WHERE stable.school_id = version.school_id AND stable.id = version.location_id"
    )
    op.execute(
        "UPDATE public.organization_unit_versions AS version "
        "SET parent_organization_unit_id = stable.parent_organization_unit_id "
        "FROM public.organization_units AS stable "
        "WHERE stable.school_id = version.school_id "
        "AND stable.id = version.organization_unit_id"
    )
    _guard_backfilled_structure_topology()
    op.alter_column("campus_location_versions", "kind", nullable=False)
    op.create_foreign_key(
        "fk_campus_location_versions_school_parent",
        "campus_location_versions",
        "campus_locations",
        ["school_id", "parent_location_id"],
        ["school_id", "id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        "ck_campus_location_versions_not_self_parent",
        "campus_location_versions",
        "parent_location_id IS NULL OR parent_location_id <> location_id",
    )
    op.create_check_constraint(
        "ck_campus_location_versions_top_level_parent",
        "campus_location_versions",
        "(kind = 'campus' AND parent_location_id IS NULL) OR "
        "(kind <> 'campus' AND parent_location_id IS NOT NULL)",
    )
    op.create_foreign_key(
        "fk_organization_unit_versions_school_parent",
        "organization_unit_versions",
        "organization_units",
        ["school_id", "parent_organization_unit_id"],
        ["school_id", "id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        "ck_organization_unit_versions_not_self_parent",
        "organization_unit_versions",
        "parent_organization_unit_id IS NULL "
        "OR parent_organization_unit_id <> organization_unit_id",
    )


def _drop_historical_structure_slices() -> None:
    op.drop_constraint(
        "ck_organization_unit_versions_not_self_parent",
        "organization_unit_versions",
        type_="check",
    )
    op.drop_constraint(
        "fk_organization_unit_versions_school_parent",
        "organization_unit_versions",
        type_="foreignkey",
    )
    op.drop_column("organization_unit_versions", "parent_organization_unit_id")
    op.drop_constraint(
        "ck_campus_location_versions_top_level_parent",
        "campus_location_versions",
        type_="check",
    )
    op.drop_constraint(
        "ck_campus_location_versions_not_self_parent",
        "campus_location_versions",
        type_="check",
    )
    op.drop_constraint(
        "fk_campus_location_versions_school_parent",
        "campus_location_versions",
        type_="foreignkey",
    )
    op.drop_column("campus_location_versions", "kind")
    op.drop_column("campus_location_versions", "parent_location_id")


def _create_relationship_content_slices() -> None:
    """Create and mechanically backfill assignment/login authoritative content slices."""

    op.execute(
        sa.text(
            r"""
            DO $relationship_content_guard$
            BEGIN
                IF EXISTS (
                    SELECT 1
                    FROM public.school_fact_change_lineage
                    WHERE fact_kind IN ('assignment', 'login_mapping')
                      AND operation = 'upsert'
                    GROUP BY school_id, fact_kind, fact_source_key
                    HAVING count(*) > 1
                ) OR EXISTS (
                    SELECT 1
                    FROM public.staff_assignments AS stable
                    WHERE EXISTS (
                              SELECT 1
                              FROM public.school_fact_change_lineage AS terminal
                              WHERE terminal.school_id = stable.school_id
                                AND terminal.fact_kind = 'assignment'
                                AND terminal.fact_source_key = stable.source_key
                                AND terminal.target_id = stable.id
                                AND terminal.operation IN ('end', 'revoke')
                          )
                ) OR EXISTS (
                    SELECT 1
                    FROM public.staff_assignments AS stable
                    WHERE NOT EXISTS (
                              SELECT 1
                              FROM public.school_fact_change_lineage AS lineage
                              WHERE lineage.school_id = stable.school_id
                                AND lineage.fact_kind = 'assignment'
                                AND lineage.fact_source_key = stable.source_key
                          )
                      AND stable.version <> 1
                ) OR EXISTS (
                    SELECT 1
                    FROM public.account_person_bindings AS stable
                    WHERE NOT EXISTS (
                              SELECT 1
                              FROM public.school_fact_change_lineage AS lineage
                              WHERE lineage.school_id = stable.school_id
                                AND lineage.fact_kind = 'login_mapping'
                                AND lineage.fact_source_key = stable.source_key
                          )
                      AND stable.version <> 1
                ) OR EXISTS (
                    SELECT 1
                    FROM public.staff_assignments AS stable
                    WHERE EXISTS (
                              SELECT 1
                              FROM public.school_fact_change_lineage AS lineage
                              WHERE lineage.school_id = stable.school_id
                                AND lineage.fact_kind = 'assignment'
                                AND lineage.fact_source_key = stable.source_key
                          )
                      AND stable.version IS DISTINCT FROM (
                              SELECT max(lineage.target_version)
                              FROM public.school_fact_change_lineage AS lineage
                              WHERE lineage.school_id = stable.school_id
                                AND lineage.fact_kind = 'assignment'
                                AND lineage.fact_source_key = stable.source_key
                                AND lineage.target_id = stable.id
                          )
                ) OR EXISTS (
                    SELECT 1
                    FROM public.account_person_bindings AS stable
                    WHERE EXISTS (
                              SELECT 1
                              FROM public.school_fact_change_lineage AS lineage
                              WHERE lineage.school_id = stable.school_id
                                AND lineage.fact_kind = 'login_mapping'
                                AND lineage.fact_source_key = stable.source_key
                          )
                      AND stable.version IS DISTINCT FROM (
                              SELECT max(lineage.target_version)
                              FROM public.school_fact_change_lineage AS lineage
                              WHERE lineage.school_id = stable.school_id
                                AND lineage.fact_kind = 'login_mapping'
                                AND lineage.fact_source_key = stable.source_key
                                AND lineage.target_id = stable.id
                          )
                ) THEN
                    RAISE EXCEPTION USING
                        ERRCODE = '55000',
                        MESSAGE = 'revision 18 cannot reconstruct ambiguous assignment or '
                                  'login mapping content history';
                END IF;
            END
            $relationship_content_guard$;
            """
        )
    )
    op.create_unique_constraint(
        "uq_account_person_bindings_school_id_id",
        "account_person_bindings",
        ["school_id", "id"],
    )
    op.create_table(
        "staff_assignment_versions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("school_id", sa.Uuid(), nullable=False),
        sa.Column("assignment_id", sa.Uuid(), nullable=False),
        sa.Column("person_id", sa.Uuid(), nullable=False),
        sa.Column("organization_unit_id", sa.Uuid(), nullable=False),
        sa.Column("position_title", sa.String(length=200), nullable=False),
        sa.Column("semantic_role_code", sa.String(length=80), nullable=False),
        sa.Column("assignment_kind", sa.String(length=20), nullable=False),
        sa.Column("appointment_source", sa.String(length=40), nullable=False),
        sa.Column("approval_reference", sa.String(length=255), nullable=False),
        sa.Column("effective_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("effective_to", sa.DateTime(timezone=True), nullable=True),
        sa.Column("planned_valid_to", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("target_version", sa.BigInteger(), nullable=False),
        sa.Column("source_ref", sa.String(length=255), nullable=False),
        sa.Column("source_id", sa.Uuid(), nullable=False),
        sa.Column("import_batch_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_staff_assignment_versions"),
        sa.UniqueConstraint(
            "school_id",
            "assignment_id",
            "target_version",
            name="uq_staff_assignment_versions_target_version",
        ),
        sa.ForeignKeyConstraint(
            ["school_id", "assignment_id"],
            ["staff_assignments.school_id", "staff_assignments.id"],
            name="fk_staff_assignment_versions_school_assignment",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["school_id", "person_id"],
            ["school_people.school_id", "school_people.id"],
            name="fk_staff_assignment_versions_school_person",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["school_id", "organization_unit_id"],
            ["organization_units.school_id", "organization_units.id"],
            name="fk_staff_assignment_versions_school_unit",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["school_id", "source_id"],
            ["school_fact_sources.school_id", "school_fact_sources.id"],
            name="fk_staff_assignment_versions_school_source",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["school_id", "import_batch_id"],
            ["master_data_import_batches.school_id", "master_data_import_batches.id"],
            name="fk_staff_assignment_versions_school_batch",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "assignment_kind IN ('substantive', 'part_time', 'acting')",
            name="ck_staff_assignment_versions_assignment_kind",
        ),
        sa.CheckConstraint(
            "status IN ('draft', 'confirmed', 'rejected')",
            name="ck_staff_assignment_versions_status",
        ),
        sa.CheckConstraint(
            "effective_to IS NULL OR effective_to > effective_from",
            name="ck_staff_assignment_versions_effective_interval",
        ),
        sa.CheckConstraint(
            "planned_valid_to IS NULL OR planned_valid_to > effective_from",
            name="ck_staff_assignment_versions_planned_interval",
        ),
        sa.CheckConstraint(
            "target_version >= 1",
            name="ck_staff_assignment_versions_target_version_positive",
        ),
        postgresql.ExcludeConstraint(
            ("school_id", "="),
            ("assignment_id", "="),
            (
                sa.text(
                    "tstzrange(effective_from, "
                    "COALESCE(effective_to, 'infinity'::timestamptz), '[)')"
                ),
                "&&",
            ),
            where=sa.text("status = 'confirmed' AND revoked_at IS NULL"),
            using="gist",
            name="excl_staff_assignment_versions_confirmed_period",
        ),
        postgresql.ExcludeConstraint(
            ("school_id", "="),
            ("person_id", "="),
            ("organization_unit_id", "="),
            ("semantic_role_code", "="),
            (
                sa.text(
                    "tstzrange(effective_from, "
                    "LEAST(COALESCE(effective_to, 'infinity'::timestamptz), "
                    "COALESCE(planned_valid_to, 'infinity'::timestamptz)), '[)')"
                ),
                "&&",
            ),
            where=sa.text("status = 'confirmed' AND revoked_at IS NULL"),
            using="gist",
            name="excl_staff_assignment_versions_confirmed_slot_period",
        ),
    )
    op.create_index(
        "ix_staff_assignment_versions_as_of",
        "staff_assignment_versions",
        ["school_id", "assignment_id", "status", "effective_from", "effective_to"],
    )
    op.create_table(
        "account_person_binding_versions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("school_id", sa.Uuid(), nullable=False),
        sa.Column("binding_id", sa.Uuid(), nullable=False),
        sa.Column("account_user_id", sa.Integer(), nullable=False),
        sa.Column("person_id", sa.Uuid(), nullable=False),
        sa.Column("provider_code", sa.String(length=80), nullable=False),
        sa.Column("provider_tenant_key", sa.String(length=200), nullable=False),
        sa.Column("external_subject", sa.String(length=255), nullable=False),
        sa.Column("effective_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("effective_to", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("target_version", sa.BigInteger(), nullable=False),
        sa.Column("source_ref", sa.String(length=255), nullable=False),
        sa.Column("source_id", sa.Uuid(), nullable=False),
        sa.Column("import_batch_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_account_person_binding_versions"),
        sa.UniqueConstraint(
            "school_id",
            "binding_id",
            "target_version",
            name="uq_account_person_binding_versions_target_version",
        ),
        sa.ForeignKeyConstraint(
            ["school_id", "binding_id"],
            ["account_person_bindings.school_id", "account_person_bindings.id"],
            name="fk_account_person_binding_versions_school_binding",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["school_id", "person_id"],
            ["school_people.school_id", "school_people.id"],
            name="fk_account_person_binding_versions_school_person",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["account_user_id"],
            ["users.id"],
            name="fk_account_person_binding_versions_user",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["school_id", "source_id"],
            ["school_fact_sources.school_id", "school_fact_sources.id"],
            name="fk_account_person_binding_versions_school_source",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["school_id", "import_batch_id"],
            ["master_data_import_batches.school_id", "master_data_import_batches.id"],
            name="fk_account_person_binding_versions_school_batch",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "status IN ('draft', 'confirmed', 'rejected')",
            name="ck_account_person_binding_versions_status",
        ),
        sa.CheckConstraint(
            "effective_to IS NULL OR effective_to > effective_from",
            name="ck_account_person_binding_versions_effective_interval",
        ),
        sa.CheckConstraint(
            "target_version >= 1",
            name="ck_account_person_binding_versions_target_version_positive",
        ),
        postgresql.ExcludeConstraint(
            ("school_id", "="),
            ("binding_id", "="),
            (
                sa.text(
                    "tstzrange(effective_from, "
                    "COALESCE(effective_to, 'infinity'::timestamptz), '[)')"
                ),
                "&&",
            ),
            where=sa.text("status = 'confirmed' AND revoked_at IS NULL"),
            using="gist",
            name="excl_account_person_binding_versions_confirmed_period",
        ),
        postgresql.ExcludeConstraint(
            ("school_id", "="),
            ("account_user_id", "="),
            (
                sa.text(
                    "tstzrange(effective_from, "
                    "COALESCE(effective_to, 'infinity'::timestamptz), '[)')"
                ),
                "&&",
            ),
            where=sa.text("status = 'confirmed' AND revoked_at IS NULL"),
            using="gist",
            name="excl_account_person_binding_versions_account_period",
        ),
        postgresql.ExcludeConstraint(
            ("school_id", "="),
            ("person_id", "="),
            (
                sa.text(
                    "tstzrange(effective_from, "
                    "COALESCE(effective_to, 'infinity'::timestamptz), '[)')"
                ),
                "&&",
            ),
            where=sa.text("status = 'confirmed' AND revoked_at IS NULL"),
            using="gist",
            name="excl_account_person_binding_versions_person_period",
        ),
    )
    op.create_index(
        "ix_account_person_binding_versions_as_of",
        "account_person_binding_versions",
        ["school_id", "binding_id", "status", "effective_from", "effective_to"],
    )
    op.create_index(
        "ix_account_person_binding_versions_account_as_of",
        "account_person_binding_versions",
        ["school_id", "account_user_id", "status", "effective_from", "effective_to"],
    )
    op.execute(
        sa.text(
            r"""
            INSERT INTO public.school_fact_sources (
                id, school_id, source_key, source_kind, source_ref, schema_version
            )
            SELECT gen_random_uuid(), school.id,
                   'revision18:legacy-relationship-content:' || school.id::text,
                   'legacy',
                   'revision18:legacy-relationship-content:' || school.id::text,
                   1
            FROM public.schools AS school
            WHERE EXISTS (
                SELECT 1 FROM public.staff_assignments AS assignment
                WHERE assignment.school_id = school.id
            ) OR EXISTS (
                SELECT 1 FROM public.account_person_bindings AS binding
                WHERE binding.school_id = school.id
            )
            ON CONFLICT (school_id, source_key) DO NOTHING;

            INSERT INTO public.staff_assignment_versions (
                id, school_id, assignment_id, person_id, organization_unit_id,
                position_title, semantic_role_code, assignment_kind, appointment_source,
                approval_reference, effective_from, effective_to, planned_valid_to,
                revoked_at, status, target_version, source_ref, source_id, import_batch_id
            )
            SELECT gen_random_uuid(), stable.school_id, stable.id, stable.person_id,
                   stable.organization_unit_id, stable.position_title,
                   stable.semantic_role_code, stable.assignment_kind,
                   stable.appointment_source, stable.approval_reference, stable.valid_from,
                   NULL, stable.valid_to, stable.revoked_at, stable.status,
                   COALESCE(upsert.target_version, 1),
                   COALESCE(batch_source.source_ref, legacy.source_ref),
                   COALESCE(batch_source.id, legacy.id), upsert.batch_id
            FROM public.staff_assignments AS stable
            LEFT JOIN LATERAL (
                SELECT lineage.target_version, lineage.batch_id
                FROM public.school_fact_change_lineage AS lineage
                WHERE lineage.school_id = stable.school_id
                  AND lineage.fact_kind = 'assignment'
                  AND lineage.fact_source_key = stable.source_key
                  AND lineage.target_id = stable.id
                  AND lineage.operation = 'upsert'
                ORDER BY lineage.target_version
                LIMIT 1
            ) AS upsert ON true
            LEFT JOIN public.school_fact_sources AS batch_source
              ON batch_source.school_id = stable.school_id
             AND batch_source.source_key = 'change-set:' || upsert.batch_id::text
            JOIN public.school_fact_sources AS legacy
              ON legacy.school_id = stable.school_id
             AND legacy.source_key =
                   'revision18:legacy-relationship-content:' || stable.school_id::text;

            INSERT INTO public.account_person_binding_versions (
                id, school_id, binding_id, account_user_id, person_id, provider_code,
                provider_tenant_key, external_subject, effective_from,
                effective_to, revoked_at, status, target_version, source_ref, source_id,
                import_batch_id
            )
            SELECT gen_random_uuid(), stable.school_id, stable.id, stable.account_user_id,
                   stable.person_id, users.provider_code, users.provider_tenant_key,
                   users.external_subject, stable.valid_from,
                   stable.valid_to, revoke.effective_at, stable.status,
                   COALESCE(upsert.target_version, 1),
                   COALESCE(batch_source.source_ref, legacy.source_ref),
                   COALESCE(batch_source.id, legacy.id), upsert.batch_id
            FROM public.account_person_bindings AS stable
            JOIN public.users AS users ON users.id = stable.account_user_id
            LEFT JOIN LATERAL (
                SELECT lineage.target_version, lineage.batch_id
                FROM public.school_fact_change_lineage AS lineage
                WHERE lineage.school_id = stable.school_id
                  AND lineage.fact_kind = 'login_mapping'
                  AND lineage.fact_source_key = stable.source_key
                  AND lineage.target_id = stable.id
                  AND lineage.operation = 'upsert'
                ORDER BY lineage.target_version
                LIMIT 1
            ) AS upsert ON true
            LEFT JOIN LATERAL (
                SELECT (unit.payload ->> 'effective_at')::timestamptz AS effective_at
                FROM public.school_fact_change_lineage AS lineage
                JOIN public.master_data_import_units AS unit
                  ON unit.school_id = lineage.school_id AND unit.id = lineage.unit_id
                WHERE lineage.school_id = stable.school_id
                  AND lineage.fact_kind = 'login_mapping'
                  AND lineage.fact_source_key = stable.source_key
                  AND lineage.target_id = stable.id
                  AND lineage.operation = 'revoke'
                ORDER BY lineage.target_version
                LIMIT 1
            ) AS revoke ON true
            LEFT JOIN public.school_fact_sources AS batch_source
              ON batch_source.school_id = stable.school_id
             AND batch_source.source_key = 'change-set:' || upsert.batch_id::text
            JOIN public.school_fact_sources AS legacy
              ON legacy.school_id = stable.school_id
             AND legacy.source_key =
                   'revision18:legacy-relationship-content:' || stable.school_id::text;
            """
        )
    )
    for trigger_name, table_name in (
        ("trg_sf1_reject_delete_staff_assignment_versions", "staff_assignment_versions"),
        (
            "trg_sf1_reject_delete_account_person_binding_versions",
            "account_person_binding_versions",
        ),
    ):
        op.execute(
            f"CREATE TRIGGER {trigger_name} BEFORE DELETE ON public.{table_name} "
            "FOR EACH ROW EXECUTE FUNCTION public.sf1_reject_history_delete()"
        )
    for table_name in (
        "staff_assignment_versions",
        "account_person_binding_versions",
    ):
        op.execute(f"ALTER TABLE public.{table_name} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE public.{table_name} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant_select ON public.{table_name} FOR SELECT USING ("
            "CASE WHEN CURRENT_USER IN ('campus_ai_db_owner', 'campus_ai_context_definer') "
            "THEN true ELSE school_id = (SELECT app.require_school_context()) END)"
        )
        op.execute(
            f"CREATE POLICY owner_write ON public.{table_name} FOR ALL "
            "USING (CURRENT_USER = 'campus_ai_db_owner') "
            "WITH CHECK (CURRENT_USER = 'campus_ai_db_owner')"
        )
    op.execute(
        "GRANT SELECT ON public.staff_assignment_versions, "
        "public.account_person_binding_versions TO campus_ai_context_definer"
    )
    op.execute(
        "GRANT SELECT ON public.staff_assignment_versions, "
        "public.account_person_binding_versions TO campus_ai_runtime"
    )
    for constraint_name, table_name in (
        ("excl_staff_assignments_confirmed_slot_period", "staff_assignments"),
        ("excl_account_person_bindings_account_period", "account_person_bindings"),
        ("excl_account_person_bindings_person_period", "account_person_bindings"),
    ):
        op.drop_constraint(constraint_name, table_name)


def _drop_relationship_content_slices() -> None:
    op.execute(
        "REVOKE SELECT ON public.staff_assignment_versions, "
        "public.account_person_binding_versions FROM campus_ai_runtime"
    )
    op.execute(
        "REVOKE SELECT ON public.staff_assignment_versions, "
        "public.account_person_binding_versions FROM campus_ai_context_definer"
    )
    op.execute(
        "DROP TRIGGER trg_sf1_reject_delete_account_person_binding_versions "
        "ON public.account_person_binding_versions"
    )
    op.execute(
        "DROP TRIGGER trg_sf1_reject_delete_staff_assignment_versions "
        "ON public.staff_assignment_versions"
    )
    op.drop_table("account_person_binding_versions")
    op.drop_table("staff_assignment_versions")
    # Alembic's create_exclude_constraint cannot attach an unnamed TextClause
    # expression to its synthetic Table.  Use the exact rev17 PostgreSQL DDL so
    # the downgrade restores the original stable-projection constraints.
    op.execute(
        """
        ALTER TABLE public.staff_assignments
        ADD CONSTRAINT excl_staff_assignments_confirmed_slot_period
        EXCLUDE USING gist (
            school_id WITH =,
            person_id WITH =,
            organization_unit_id WITH =,
            semantic_role_code WITH =,
            tstzrange(
                valid_from,
                COALESCE(valid_to, 'infinity'::timestamptz),
                '[)'
            ) WITH &&
        ) WHERE (status = 'confirmed')
        """
    )
    op.execute(
        """
        ALTER TABLE public.account_person_bindings
        ADD CONSTRAINT excl_account_person_bindings_account_period
        EXCLUDE USING gist (
            school_id WITH =,
            account_user_id WITH =,
            tstzrange(
                valid_from,
                COALESCE(valid_to, 'infinity'::timestamptz),
                '[)'
            ) WITH &&
        ) WHERE (status = 'confirmed')
        """
    )
    op.execute(
        """
        ALTER TABLE public.account_person_bindings
        ADD CONSTRAINT excl_account_person_bindings_person_period
        EXCLUDE USING gist (
            school_id WITH =,
            person_id WITH =,
            tstzrange(
                valid_from,
                COALESCE(valid_to, 'infinity'::timestamptz),
                '[)'
            ) WITH &&
        ) WHERE (status = 'confirmed')
        """
    )
    op.execute(
        "DELETE FROM public.school_fact_sources WHERE source_kind = 'legacy' "
        "AND source_key = 'revision18:legacy-relationship-content:' || school_id::text "
        "AND source_ref = source_key"
    )
    op.drop_constraint(
        "uq_account_person_bindings_school_id_id",
        "account_person_bindings",
        type_="unique",
    )


def _guard_upgrade_location_history() -> None:
    """Reject rev17 Residence history that cannot be mapped to exact structural slices."""

    op.execute(
        sa.text(
            r"""
            DO $location_history_upgrade_guard$
            BEGIN
                IF EXISTS (
                    SELECT 1
                    FROM public.campus_locations AS location
                    WHERE location.status = 'inactive'
                      AND NOT EXISTS (
                          SELECT 1
                          FROM public.school_fact_change_lineage AS lineage
                          JOIN public.master_data_import_units AS unit
                            ON unit.school_id = lineage.school_id
                           AND unit.id = lineage.unit_id
                          WHERE lineage.school_id = location.school_id
                            AND lineage.fact_kind = 'location'
                            AND lineage.target_id = location.id
                            AND lineage.target_version = location.version
                            AND lineage.operation IN ('end', 'revoke')
                            AND unit.payload ->> 'fact_kind' = 'location'
                            AND unit.payload ->> 'source_key' = lineage.fact_source_key
                            AND unit.payload ->> 'operation' = lineage.operation
                            AND NULLIF(unit.payload ->> 'effective_at', '') IS NOT NULL
                      )
                ) OR EXISTS (
                    SELECT 1
                    FROM public.student_bed_residences AS residence
                    JOIN public.dorm_beds AS bed
                      ON bed.school_id = residence.school_id
                     AND bed.id = residence.bed_id
                    WHERE residence.revoked_at IS NULL
                      AND NOT app.school_fact_location_history_interval_is_resolvable(
                          residence.school_id,
                          bed.room_location_id,
                          residence.valid_from,
                          residence.valid_to
                      )
                ) THEN
                    RAISE EXCEPTION USING
                        ERRCODE = '55000',
                        MESSAGE = 'revision 18 cannot install with effective Residence '
                                  'below inactive location ancestry';
                END IF;
            END
            $location_history_upgrade_guard$;
            """
        )
    )


def _create_structure_history_guards() -> None:
    """Serialize structure changes and require acyclic, lineage-backed projections."""

    op.execute(
        sa.text(
            r"""
            CREATE FUNCTION public.sf18_lock_structure_change()
            RETURNS trigger
            LANGUAGE plpgsql
            SECURITY DEFINER
            SET search_path = pg_catalog
            AS $function$
            DECLARE
                v_scope text;
            BEGIN
                v_scope := CASE
                    WHEN TG_TABLE_NAME IN (
                        'organization_units',
                        'organization_unit_versions'
                    ) THEN 'organization-structure:'
                    ELSE 'location-structure:'
                END;
                PERFORM pg_catalog.pg_advisory_xact_lock(
                    pg_catalog.hashtextextended(
                        'campus-ai:sf18:' || v_scope || NEW.school_id::text,
                        0
                    )
                );
                RETURN NEW;
            END
            $function$;

            CREATE TRIGGER trg_sf18_lock_organization_structure
            BEFORE INSERT OR UPDATE OF parent_organization_unit_id, kind
            ON public.organization_units
            FOR EACH ROW EXECUTE FUNCTION public.sf18_lock_structure_change();

            CREATE TRIGGER trg_sf18_lock_organization_version_structure
            BEFORE INSERT OR UPDATE OF
                parent_organization_unit_id, valid_from, valid_to, status
            ON public.organization_unit_versions
            FOR EACH ROW EXECUTE FUNCTION public.sf18_lock_structure_change();

            CREATE TRIGGER trg_sf18_lock_location_structure
            BEFORE INSERT OR UPDATE OF parent_location_id, kind
            ON public.campus_locations
            FOR EACH ROW EXECUTE FUNCTION public.sf18_lock_structure_change();

            CREATE TRIGGER trg_sf18_lock_location_version_structure
            BEFORE INSERT OR UPDATE OF parent_location_id, kind, valid_from, valid_to, status
            ON public.campus_location_versions
            FOR EACH ROW EXECUTE FUNCTION public.sf18_lock_structure_change();

            CREATE FUNCTION public.sf18_validate_organization_structure()
            RETURNS trigger
            LANGUAGE plpgsql
            SECURITY DEFINER
            SET search_path = pg_catalog
            AS $function$
            BEGIN
                PERFORM pg_catalog.pg_advisory_xact_lock(
                    pg_catalog.hashtextextended(
                        'campus-ai:sf18:organization-structure:' || NEW.school_id::text,
                        0
                    )
                );
                IF EXISTS (
                    WITH RECURSIVE ancestry(id, parent_id, path, cycle) AS (
                        SELECT NEW.id,
                               NEW.parent_organization_unit_id,
                               ARRAY[NEW.id]::uuid[],
                               false
                        UNION ALL
                        SELECT parent.id,
                               parent.parent_organization_unit_id,
                               child.path || parent.id,
                               parent.id = ANY(child.path)
                        FROM ancestry AS child
                        JOIN public.organization_units AS parent
                          ON parent.school_id = NEW.school_id
                         AND parent.id = child.parent_id
                        WHERE NOT child.cycle
                    )
                    SELECT 1
                    FROM ancestry
                    WHERE cycle
                ) THEN
                    RAISE EXCEPTION USING
                        ERRCODE = '23514',
                        CONSTRAINT = 'ck_organization_units_acyclic',
                        MESSAGE = 'organization hierarchy cannot contain a cycle';
                END IF;
                RETURN NULL;
            END
            $function$;

            CREATE CONSTRAINT TRIGGER trg_sf18_a_validate_organization_structure
            AFTER INSERT OR UPDATE OF parent_organization_unit_id
            ON public.organization_units
            DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW EXECUTE FUNCTION public.sf18_validate_organization_structure();

            CREATE FUNCTION public.sf18_validate_organization_version_structure()
            RETURNS trigger
            LANGUAGE plpgsql
            SECURITY DEFINER
            SET search_path = pg_catalog
            AS $function$
            BEGIN
                PERFORM pg_catalog.pg_advisory_xact_lock(
                    pg_catalog.hashtextextended(
                        'campus-ai:sf18:organization-structure:' || NEW.school_id::text,
                        0
                    )
                );
                IF EXISTS (
                    WITH RECURSIVE ancestry(id, parent_id, covered, path, cycle) AS (
                        SELECT NEW.organization_unit_id,
                               NEW.parent_organization_unit_id,
                               tstzrange(
                                   NEW.valid_from,
                                   COALESCE(NEW.valid_to, 'infinity'::timestamptz),
                                   '[)'
                               ),
                               ARRAY[NEW.organization_unit_id]::uuid[],
                               false
                        UNION ALL
                        SELECT parent.organization_unit_id,
                               parent.parent_organization_unit_id,
                               child.covered * tstzrange(
                                   parent.valid_from,
                                   COALESCE(parent.valid_to, 'infinity'::timestamptz),
                                   '[)'
                               ),
                               child.path || parent.organization_unit_id,
                               parent.organization_unit_id = ANY(child.path)
                        FROM ancestry AS child
                        JOIN public.organization_unit_versions AS parent
                          ON parent.school_id = NEW.school_id
                         AND parent.organization_unit_id = child.parent_id
                         AND parent.status = 'confirmed'
                         AND tstzrange(
                                 parent.valid_from,
                                 COALESCE(parent.valid_to, 'infinity'::timestamptz),
                                 '[)'
                             ) && child.covered
                        WHERE NOT child.cycle
                    )
                    SELECT 1
                    FROM ancestry
                    WHERE cycle AND NOT isempty(covered)
                ) THEN
                    RAISE EXCEPTION USING
                        ERRCODE = '23514',
                        CONSTRAINT = 'ck_organization_unit_versions_acyclic',
                        MESSAGE = 'organization structure versions cannot contain a cycle';
                END IF;
                RETURN NULL;
            END
            $function$;

            CREATE CONSTRAINT TRIGGER trg_sf18_validate_organization_version_structure
            AFTER INSERT OR UPDATE OF parent_organization_unit_id, valid_from, valid_to, status
            ON public.organization_unit_versions
            DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW
            EXECUTE FUNCTION public.sf18_validate_organization_version_structure();

            CREATE FUNCTION public.sf18_validate_location_version_structure()
            RETURNS trigger
            LANGUAGE plpgsql
            SECURITY DEFINER
            SET search_path = pg_catalog
            AS $function$
            BEGIN
                PERFORM pg_catalog.pg_advisory_xact_lock(
                    pg_catalog.hashtextextended(
                        'campus-ai:sf18:location-structure:' || NEW.school_id::text,
                        0
                    )
                );
                IF EXISTS (
                    WITH RECURSIVE ancestry(id, parent_id, covered, path, cycle) AS (
                        SELECT NEW.location_id,
                               NEW.parent_location_id,
                               tstzrange(
                                   NEW.valid_from,
                                   COALESCE(NEW.valid_to, 'infinity'::timestamptz),
                                   '[)'
                               ),
                               ARRAY[NEW.location_id]::uuid[],
                               false
                        UNION ALL
                        SELECT parent.location_id,
                               parent.parent_location_id,
                               child.covered * tstzrange(
                                   parent.valid_from,
                                   COALESCE(parent.valid_to, 'infinity'::timestamptz),
                                   '[)'
                               ),
                               child.path || parent.location_id,
                               parent.location_id = ANY(child.path)
                        FROM ancestry AS child
                        JOIN public.campus_location_versions AS parent
                          ON parent.school_id = NEW.school_id
                         AND parent.location_id = child.parent_id
                         AND parent.status = 'confirmed'
                         AND tstzrange(
                                 parent.valid_from,
                                 COALESCE(parent.valid_to, 'infinity'::timestamptz),
                                 '[)'
                             ) && child.covered
                        WHERE NOT child.cycle
                    )
                    SELECT 1
                    FROM ancestry
                    WHERE cycle AND NOT isempty(covered)
                ) THEN
                    RAISE EXCEPTION USING
                        ERRCODE = '23514',
                        CONSTRAINT = 'ck_campus_location_versions_acyclic',
                        MESSAGE = 'location structure versions cannot contain a cycle';
                END IF;
                RETURN NULL;
            END
            $function$;

            CREATE CONSTRAINT TRIGGER trg_sf18_validate_location_version_structure
            AFTER INSERT OR UPDATE OF parent_location_id, valid_from, valid_to, status
            ON public.campus_location_versions
            DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW
            EXECUTE FUNCTION public.sf18_validate_location_version_structure();

            CREATE FUNCTION public.sf18_validate_structure_parent_coverage()
            RETURNS trigger
            LANGUAGE plpgsql
            SECURITY DEFINER
            SET search_path = pg_catalog
            AS $function$
            DECLARE
                v_invalid boolean;
                v_constraint text;
            BEGIN
                PERFORM pg_catalog.pg_advisory_xact_lock(
                    pg_catalog.hashtextextended(
                        'campus-ai:sf18:structure-parent-coverage:' || NEW.school_id::text,
                        0
                    )
                );
                IF TG_TABLE_NAME = 'campus_location_versions' THEN
                    SELECT EXISTS (
                        SELECT 1
                        FROM public.campus_location_versions AS child
                        WHERE child.school_id = NEW.school_id
                          AND child.status = 'confirmed'
                          AND child.parent_location_id IS NOT NULL
                          AND NOT COALESCE(
                              (
                                  SELECT range_agg(
                                      tstzrange(
                                          parent.valid_from,
                                          COALESCE(
                                              parent.valid_to,
                                              'infinity'::timestamptz
                                          ),
                                          '[)'
                                      )
                                  ) @> tstzrange(
                                      child.valid_from,
                                      COALESCE(child.valid_to, 'infinity'::timestamptz),
                                      '[)'
                                  )
                                  FROM public.campus_location_versions AS parent
                                  WHERE parent.school_id = child.school_id
                                    AND parent.location_id = child.parent_location_id
                                    AND parent.status = 'confirmed'
                              ),
                              false
                          )
                    ) INTO v_invalid;
                    v_constraint := 'ck_campus_location_versions_parent_coverage';
                ELSE
                    SELECT EXISTS (
                        SELECT 1
                        FROM public.organization_unit_versions AS child
                        WHERE child.school_id = NEW.school_id
                          AND child.status = 'confirmed'
                          AND child.parent_organization_unit_id IS NOT NULL
                          AND NOT COALESCE(
                              (
                                  SELECT range_agg(
                                      tstzrange(
                                          parent.valid_from,
                                          COALESCE(
                                              parent.valid_to,
                                              'infinity'::timestamptz
                                          ),
                                          '[)'
                                      )
                                  ) @> tstzrange(
                                      child.valid_from,
                                      COALESCE(child.valid_to, 'infinity'::timestamptz),
                                      '[)'
                                  )
                                  FROM public.organization_unit_versions AS parent
                                  WHERE parent.school_id = child.school_id
                                    AND parent.organization_unit_id
                                        = child.parent_organization_unit_id
                                    AND parent.status = 'confirmed'
                              ),
                              false
                          )
                    ) INTO v_invalid;
                    v_constraint := 'ck_organization_unit_versions_parent_coverage';
                END IF;
                IF v_invalid THEN
                    RAISE EXCEPTION USING
                        ERRCODE = '23514',
                        CONSTRAINT = v_constraint,
                        MESSAGE = 'a child structure slice requires complete parent coverage';
                END IF;
                RETURN NULL;
            END
            $function$;

            CREATE CONSTRAINT TRIGGER trg_sf18_validate_location_parent_coverage
            AFTER INSERT OR UPDATE OF parent_location_id, valid_from, valid_to, status
            ON public.campus_location_versions
            DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW
            EXECUTE FUNCTION public.sf18_validate_structure_parent_coverage();

            CREATE CONSTRAINT TRIGGER trg_sf18_validate_organization_parent_coverage
            AFTER INSERT OR UPDATE OF parent_organization_unit_id, valid_from, valid_to, status
            ON public.organization_unit_versions
            DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW
            EXECUTE FUNCTION public.sf18_validate_structure_parent_coverage();

            CREATE FUNCTION public.sf18_require_structure_lineage()
            RETURNS trigger
            LANGUAGE plpgsql
            SECURITY DEFINER
            SET search_path = pg_catalog
            AS $function$
            DECLARE
                v_fact_kind text;
                v_parent_id uuid;
                v_matches boolean;
            BEGIN
                IF TG_TABLE_NAME = 'campus_locations' THEN
                    IF NEW.parent_location_id IS NOT DISTINCT FROM OLD.parent_location_id
                       AND NEW.kind IS NOT DISTINCT FROM OLD.kind THEN
                        RETURN NULL;
                    END IF;
                    v_fact_kind := 'location';
                    v_parent_id := NEW.parent_location_id;
                    IF NEW.version IS DISTINCT FROM OLD.version + 1 THEN
                        v_matches := false;
                    ELSE
                        SELECT EXISTS (
                            SELECT 1
                            FROM public.campus_location_versions AS version
                            JOIN public.school_fact_change_lineage AS lineage
                              ON lineage.school_id = version.school_id
                             AND lineage.fact_kind = 'location'
                             AND lineage.target_id = version.location_id
                             AND lineage.target_version = version.version
                             AND lineage.operation IN ('upsert', 'reactivate')
                            JOIN public.master_data_import_units AS unit
                              ON unit.school_id = lineage.school_id
                             AND unit.id = lineage.unit_id
                             AND unit.batch_id = lineage.batch_id
                             AND unit.status IN ('applied', 'published')
                            WHERE version.school_id = NEW.school_id
                              AND version.location_id = NEW.id
                              AND version.version = NEW.version
                              AND version.parent_location_id
                                  IS NOT DISTINCT FROM v_parent_id
                              AND version.kind = NEW.kind
                        ) INTO v_matches;
                    END IF;
                ELSE
                    IF NEW.parent_organization_unit_id IS NOT DISTINCT FROM
                       OLD.parent_organization_unit_id
                       AND NEW.kind IS NOT DISTINCT FROM OLD.kind THEN
                        RETURN NULL;
                    END IF;
                    v_fact_kind := 'organization';
                    v_parent_id := NEW.parent_organization_unit_id;
                    IF NEW.version IS DISTINCT FROM OLD.version + 1 THEN
                        v_matches := false;
                    ELSE
                        SELECT EXISTS (
                            SELECT 1
                            FROM public.organization_unit_versions AS version
                            JOIN public.school_fact_change_lineage AS lineage
                              ON lineage.school_id = version.school_id
                             AND lineage.fact_kind = 'organization'
                             AND lineage.target_id = version.organization_unit_id
                             AND lineage.target_version = version.version
                             AND lineage.operation IN ('upsert', 'reactivate')
                            JOIN public.master_data_import_units AS unit
                              ON unit.school_id = lineage.school_id
                             AND unit.id = lineage.unit_id
                             AND unit.batch_id = lineage.batch_id
                             AND unit.status IN ('applied', 'published')
                            WHERE version.school_id = NEW.school_id
                              AND version.organization_unit_id = NEW.id
                              AND version.version = NEW.version
                              AND version.parent_organization_unit_id
                                  IS NOT DISTINCT FROM v_parent_id
                        ) INTO v_matches;
                    END IF;
                END IF;
                IF NOT COALESCE(v_matches, false) THEN
                    RAISE EXCEPTION USING
                        ERRCODE = '23514',
                        CONSTRAINT = CASE
                            WHEN v_fact_kind = 'location'
                                THEN 'ck_campus_location_structure_lineage'
                            ELSE 'ck_organization_unit_structure_lineage'
                        END,
                        MESSAGE = 'stable structure projection requires its formal version lineage';
                END IF;
                RETURN NULL;
            END
            $function$;

            CREATE CONSTRAINT TRIGGER trg_sf18_require_location_structure_lineage
            AFTER UPDATE OF parent_location_id, kind ON public.campus_locations
            DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW EXECUTE FUNCTION public.sf18_require_structure_lineage();

            CREATE CONSTRAINT TRIGGER trg_sf18_require_organization_structure_lineage
            AFTER UPDATE OF parent_organization_unit_id, kind ON public.organization_units
            DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW EXECUTE FUNCTION public.sf18_require_structure_lineage();

            CREATE FUNCTION public.sf18_require_structure_version_lineage()
            RETURNS trigger
            LANGUAGE plpgsql
            SECURITY DEFINER
            SET search_path = pg_catalog
            AS $function$
            DECLARE
                v_fact_kind text;
                v_target_id uuid;
                v_constraint text;
                v_matches boolean := false;
            BEGIN
                -- Every post-migration writer must prove that a structural version write
                -- belongs to a formal applied unit and lineage row. Upgrade backfill runs
                -- before these deferred triggers are installed.
                IF TG_OP = 'UPDATE' THEN
                    IF TG_TABLE_NAME = 'campus_location_versions' THEN
                        IF NEW.parent_location_id IS NOT DISTINCT FROM
                           OLD.parent_location_id
                           AND NEW.kind IS NOT DISTINCT FROM OLD.kind
                           AND NEW.valid_from IS NOT DISTINCT FROM OLD.valid_from
                           AND NEW.valid_to IS NOT DISTINCT FROM OLD.valid_to
                           AND NEW.status IS NOT DISTINCT FROM OLD.status THEN
                            RETURN NULL;
                        END IF;
                    ELSIF TG_TABLE_NAME = 'organization_unit_versions' THEN
                        IF NEW.parent_organization_unit_id IS NOT DISTINCT FROM
                           OLD.parent_organization_unit_id
                           AND NEW.valid_from IS NOT DISTINCT FROM OLD.valid_from
                           AND NEW.valid_to IS NOT DISTINCT FROM OLD.valid_to
                           AND NEW.status IS NOT DISTINCT FROM OLD.status THEN
                            RETURN NULL;
                        END IF;
                    END IF;
                END IF;
                IF TG_TABLE_NAME = 'campus_location_versions' THEN
                    v_fact_kind := 'location';
                    v_target_id := NEW.location_id;
                    v_constraint := 'ck_campus_location_version_structure_lineage';
                    IF TG_OP = 'INSERT' THEN
                        SELECT EXISTS (
                            SELECT 1
                            FROM public.campus_locations AS stable
                            JOIN public.school_fact_change_lineage AS lineage
                              ON lineage.school_id = stable.school_id
                             AND lineage.fact_kind = 'location'
                             AND lineage.target_id = stable.id
                             AND lineage.target_version = NEW.version
                             AND lineage.operation IN ('upsert', 'reactivate')
                             AND lineage.fact_source_key = stable.source_key
                            JOIN public.master_data_import_batches AS batch
                              ON batch.school_id = lineage.school_id
                             AND batch.id = lineage.batch_id
                             AND batch.mode = 'canonical_atomic_changes'
                             AND batch.status IN ('applied', 'published')
                             AND batch.source_sha256 = lineage.source_sha256
                            JOIN public.master_data_import_units AS unit
                              ON unit.school_id = lineage.school_id
                             AND unit.id = lineage.unit_id
                             AND unit.batch_id = lineage.batch_id
                             AND unit.status IN ('applied', 'published')
                             AND unit.unit_kind = 'school_fact'
                             AND unit.target_id = stable.id
                             AND unit.target_version = NEW.version
                             AND unit.payload_sha256 ~ '^[0-9a-f]{64}$'
                             AND unit.payload ->> 'fact_kind' = 'location'
                             AND unit.payload ->> 'source_key' = stable.source_key
                             AND unit.payload ->> 'operation' = lineage.operation
                             AND (unit.payload ->> 'effective_at')::timestamptz
                                 = NEW.valid_from
                             AND (
                                 (
                                     NEW.version = 1
                                     AND lineage.operation = 'upsert'
                                     AND unit.payload ? 'expected_version'
                                     AND jsonb_typeof(
                                         unit.payload -> 'expected_version'
                                     ) = 'number'
                                     AND unit.payload ->> 'expected_version' = '0'
                                 )
                                 OR (
                                     NEW.version > 1
                                     AND unit.payload ? 'expected_version'
                                     AND jsonb_typeof(
                                         unit.payload -> 'expected_version'
                                     ) = 'number'
                                     AND unit.payload ->> 'expected_version' ~ '^[0-9]+$'
                                     AND (unit.payload ->> 'expected_version')::bigint
                                         = NEW.version - 1
                                 )
                             )
                            WHERE stable.school_id = NEW.school_id
                              AND stable.id = NEW.location_id
                              AND stable.version = NEW.version
                              AND stable.parent_location_id
                                  IS NOT DISTINCT FROM NEW.parent_location_id
                              AND stable.kind = NEW.kind
                              AND NEW.status = 'confirmed'
                              AND NEW.source_ref = 'change-set:' || lineage.batch_id::text
                        ) INTO v_matches;
                    ELSE
                        IF NEW.parent_location_id IS NOT DISTINCT FROM OLD.parent_location_id
                           AND NEW.kind IS NOT DISTINCT FROM OLD.kind
                           AND NEW.valid_from IS NOT DISTINCT FROM OLD.valid_from
                           AND NEW.status IS NOT DISTINCT FROM OLD.status
                           AND OLD.valid_to IS NULL
                           AND NEW.valid_to IS NOT NULL THEN
                            SELECT EXISTS (
                                SELECT 1
                            FROM public.campus_location_versions AS successor
                            JOIN public.campus_locations AS stable
                              ON stable.school_id = successor.school_id
                             AND stable.id = successor.location_id
                             AND stable.version = successor.version
                            JOIN public.school_fact_change_lineage AS lineage
                              ON lineage.school_id = successor.school_id
                             AND lineage.fact_kind = 'location'
                             AND lineage.target_id = successor.location_id
                             AND lineage.target_version = successor.version
                             AND lineage.operation IN ('upsert', 'reactivate')
                             AND lineage.fact_source_key = stable.source_key
                            JOIN public.master_data_import_batches AS batch
                              ON batch.school_id = lineage.school_id
                             AND batch.id = lineage.batch_id
                             AND batch.mode = 'canonical_atomic_changes'
                             AND batch.status IN ('applied', 'published')
                             AND batch.source_sha256 = lineage.source_sha256
                            JOIN public.master_data_import_units AS unit
                              ON unit.school_id = lineage.school_id
                             AND unit.id = lineage.unit_id
                             AND unit.batch_id = lineage.batch_id
                             AND unit.status IN ('applied', 'published')
                             AND unit.unit_kind = 'school_fact'
                             AND unit.target_id = successor.location_id
                             AND unit.target_version = successor.version
                             AND unit.payload_sha256 ~ '^[0-9a-f]{64}$'
                             AND unit.payload ->> 'fact_kind' = 'location'
                             AND unit.payload ->> 'source_key' = stable.source_key
                             AND unit.payload ->> 'operation' = lineage.operation
                             AND (unit.payload ->> 'effective_at')::timestamptz
                                 = successor.valid_from
                             AND unit.payload ? 'expected_version'
                             AND jsonb_typeof(
                                 unit.payload -> 'expected_version'
                             ) = 'number'
                             AND unit.payload ->> 'expected_version' ~ '^[0-9]+$'
                             AND (unit.payload ->> 'expected_version')::bigint
                                 = successor.version - 1
                                WHERE successor.school_id = NEW.school_id
                                  AND successor.location_id = NEW.location_id
                                  AND successor.version > OLD.version
                                  AND successor.valid_from = NEW.valid_to
                                  AND successor.status = 'confirmed'
                                  AND successor.source_ref
                                      = 'change-set:' || lineage.batch_id::text
                            ) INTO v_matches;
                        END IF;
                    END IF;
                ELSE
                    v_fact_kind := 'organization';
                    v_target_id := NEW.organization_unit_id;
                    v_constraint := 'ck_organization_unit_version_structure_lineage';
                    IF TG_OP = 'INSERT' THEN
                        SELECT EXISTS (
                            SELECT 1
                            FROM public.organization_units AS stable
                            JOIN public.school_fact_change_lineage AS lineage
                              ON lineage.school_id = stable.school_id
                             AND lineage.fact_kind = 'organization'
                             AND lineage.target_id = stable.id
                             AND lineage.target_version = NEW.version
                             AND lineage.operation IN ('upsert', 'reactivate')
                             AND lineage.fact_source_key = stable.source_key
                            JOIN public.master_data_import_batches AS batch
                              ON batch.school_id = lineage.school_id
                             AND batch.id = lineage.batch_id
                             AND batch.mode = 'canonical_atomic_changes'
                             AND batch.status IN ('applied', 'published')
                             AND batch.source_sha256 = lineage.source_sha256
                            JOIN public.master_data_import_units AS unit
                              ON unit.school_id = lineage.school_id
                             AND unit.id = lineage.unit_id
                             AND unit.batch_id = lineage.batch_id
                             AND unit.status IN ('applied', 'published')
                             AND unit.unit_kind = 'school_fact'
                             AND unit.target_id = stable.id
                             AND unit.target_version = NEW.version
                             AND unit.payload_sha256 ~ '^[0-9a-f]{64}$'
                             AND unit.payload ->> 'fact_kind' = 'organization'
                             AND unit.payload ->> 'source_key' = stable.source_key
                             AND unit.payload ->> 'operation' = lineage.operation
                             AND (unit.payload ->> 'effective_at')::timestamptz
                                 = NEW.valid_from
                             AND (
                                 (
                                     NEW.version = 1
                                     AND lineage.operation = 'upsert'
                                     AND unit.payload ? 'expected_version'
                                     AND jsonb_typeof(
                                         unit.payload -> 'expected_version'
                                     ) = 'number'
                                     AND unit.payload ->> 'expected_version' = '0'
                                 )
                                 OR (
                                     NEW.version > 1
                                     AND unit.payload ? 'expected_version'
                                     AND jsonb_typeof(
                                         unit.payload -> 'expected_version'
                                     ) = 'number'
                                     AND unit.payload ->> 'expected_version' ~ '^[0-9]+$'
                                     AND (unit.payload ->> 'expected_version')::bigint
                                         = NEW.version - 1
                                 )
                             )
                            WHERE stable.school_id = NEW.school_id
                              AND stable.id = NEW.organization_unit_id
                              AND stable.version = NEW.version
                              AND stable.parent_organization_unit_id
                                  IS NOT DISTINCT FROM NEW.parent_organization_unit_id
                              AND NEW.status = 'confirmed'
                              AND NEW.source_ref = 'change-set:' || lineage.batch_id::text
                        ) INTO v_matches;
                    ELSE
                        IF NEW.parent_organization_unit_id IS NOT DISTINCT FROM
                           OLD.parent_organization_unit_id
                           AND NEW.valid_from IS NOT DISTINCT FROM OLD.valid_from
                           AND NEW.status IS NOT DISTINCT FROM OLD.status
                           AND OLD.valid_to IS NULL
                           AND NEW.valid_to IS NOT NULL THEN
                            SELECT EXISTS (
                                SELECT 1
                            FROM public.organization_unit_versions AS successor
                            JOIN public.organization_units AS stable
                              ON stable.school_id = successor.school_id
                             AND stable.id = successor.organization_unit_id
                             AND stable.version = successor.version
                            JOIN public.school_fact_change_lineage AS lineage
                              ON lineage.school_id = successor.school_id
                             AND lineage.fact_kind = 'organization'
                             AND lineage.target_id = successor.organization_unit_id
                             AND lineage.target_version = successor.version
                             AND lineage.operation IN ('upsert', 'reactivate')
                             AND lineage.fact_source_key = stable.source_key
                            JOIN public.master_data_import_batches AS batch
                              ON batch.school_id = lineage.school_id
                             AND batch.id = lineage.batch_id
                             AND batch.mode = 'canonical_atomic_changes'
                             AND batch.status IN ('applied', 'published')
                             AND batch.source_sha256 = lineage.source_sha256
                            JOIN public.master_data_import_units AS unit
                              ON unit.school_id = lineage.school_id
                             AND unit.id = lineage.unit_id
                             AND unit.batch_id = lineage.batch_id
                             AND unit.status IN ('applied', 'published')
                             AND unit.unit_kind = 'school_fact'
                             AND unit.target_id = successor.organization_unit_id
                             AND unit.target_version = successor.version
                             AND unit.payload_sha256 ~ '^[0-9a-f]{64}$'
                             AND unit.payload ->> 'fact_kind' = 'organization'
                             AND unit.payload ->> 'source_key' = stable.source_key
                             AND unit.payload ->> 'operation' = lineage.operation
                             AND (unit.payload ->> 'effective_at')::timestamptz
                                 = successor.valid_from
                             AND unit.payload ? 'expected_version'
                             AND jsonb_typeof(
                                 unit.payload -> 'expected_version'
                             ) = 'number'
                             AND unit.payload ->> 'expected_version' ~ '^[0-9]+$'
                             AND (unit.payload ->> 'expected_version')::bigint
                                 = successor.version - 1
                                WHERE successor.school_id = NEW.school_id
                                  AND successor.organization_unit_id
                                      = NEW.organization_unit_id
                                  AND successor.version > OLD.version
                                  AND successor.valid_from = NEW.valid_to
                                  AND successor.status = 'confirmed'
                                  AND successor.source_ref
                                      = 'change-set:' || lineage.batch_id::text
                            ) INTO v_matches;
                        END IF;
                    END IF;
                END IF;
                IF NOT COALESCE(v_matches, false) THEN
                    RAISE EXCEPTION USING
                        ERRCODE = '23514',
                        CONSTRAINT = v_constraint,
                        MESSAGE = v_fact_kind || ' structure version requires formal lineage';
                END IF;
                RETURN NULL;
            END
            $function$;

            CREATE CONSTRAINT TRIGGER trg_sf18_require_location_version_structure_lineage
            AFTER INSERT OR UPDATE OF parent_location_id, kind, valid_from, valid_to, status
            ON public.campus_location_versions
            DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW EXECUTE FUNCTION public.sf18_require_structure_version_lineage();

            CREATE CONSTRAINT TRIGGER trg_sf18_require_organization_version_structure_lineage
            AFTER INSERT OR UPDATE OF
                parent_organization_unit_id, valid_from, valid_to, status
            ON public.organization_unit_versions
            DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW EXECUTE FUNCTION public.sf18_require_structure_version_lineage();
            """
        )
    )
    for signature in (
        "public.sf18_lock_structure_change()",
        "public.sf18_validate_organization_structure()",
        "public.sf18_validate_organization_version_structure()",
        "public.sf18_validate_location_version_structure()",
        "public.sf18_validate_structure_parent_coverage()",
        "public.sf18_require_structure_lineage()",
        "public.sf18_require_structure_version_lineage()",
    ):
        op.execute(f"ALTER FUNCTION {signature} OWNER TO campus_ai_db_owner")
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")


def _drop_structure_history_guards() -> None:
    op.execute(
        "DROP TRIGGER trg_sf18_require_organization_version_structure_lineage "
        "ON public.organization_unit_versions"
    )
    op.execute(
        "DROP TRIGGER trg_sf18_require_location_version_structure_lineage "
        "ON public.campus_location_versions"
    )
    op.execute("DROP FUNCTION public.sf18_require_structure_version_lineage()")
    op.execute(
        "DROP TRIGGER trg_sf18_require_organization_structure_lineage ON public.organization_units"
    )
    op.execute(
        "DROP TRIGGER trg_sf18_require_location_structure_lineage ON public.campus_locations"
    )
    op.execute("DROP FUNCTION public.sf18_require_structure_lineage()")
    op.execute(
        "DROP TRIGGER trg_sf18_validate_organization_parent_coverage "
        "ON public.organization_unit_versions"
    )
    op.execute(
        "DROP TRIGGER trg_sf18_validate_location_parent_coverage ON public.campus_location_versions"
    )
    op.execute("DROP FUNCTION public.sf18_validate_structure_parent_coverage()")
    op.execute(
        "DROP TRIGGER trg_sf18_validate_location_version_structure "
        "ON public.campus_location_versions"
    )
    op.execute("DROP FUNCTION public.sf18_validate_location_version_structure()")
    op.execute(
        "DROP TRIGGER trg_sf18_validate_organization_version_structure "
        "ON public.organization_unit_versions"
    )
    op.execute("DROP FUNCTION public.sf18_validate_organization_version_structure()")
    op.execute(
        "DROP TRIGGER trg_sf18_a_validate_organization_structure ON public.organization_units"
    )
    op.execute("DROP FUNCTION public.sf18_validate_organization_structure()")
    op.execute(
        "DROP TRIGGER trg_sf18_lock_location_version_structure ON public.campus_location_versions"
    )
    op.execute("DROP TRIGGER trg_sf18_lock_location_structure ON public.campus_locations")
    op.execute(
        "DROP TRIGGER trg_sf18_lock_organization_version_structure "
        "ON public.organization_unit_versions"
    )
    op.execute("DROP TRIGGER trg_sf18_lock_organization_structure ON public.organization_units")
    op.execute("DROP FUNCTION public.sf18_lock_structure_change()")


def _create_student_person_identity_guard() -> None:
    op.execute(
        sa.text(
            r"""
            CREATE FUNCTION public.sf18_preserve_student_person_identity()
            RETURNS trigger
            LANGUAGE plpgsql
            SECURITY DEFINER
            SET search_path = pg_catalog
            AS $function$
            BEGIN
                IF NEW.person_id IS DISTINCT FROM OLD.person_id THEN
                    RAISE EXCEPTION USING
                        ERRCODE = '23514',
                        CONSTRAINT = 'ck_students_person_identity_immutable',
                        MESSAGE = 'student person identity cannot be rebound';
                END IF;
                RETURN NEW;
            END
            $function$;

            CREATE TRIGGER trg_sf18_preserve_student_person_identity
            BEFORE UPDATE OF person_id ON public.students
            FOR EACH ROW
            EXECUTE FUNCTION public.sf18_preserve_student_person_identity();
            """
        )
    )
    op.execute(
        "ALTER FUNCTION public.sf18_preserve_student_person_identity() OWNER TO campus_ai_db_owner"
    )
    op.execute("REVOKE ALL ON FUNCTION public.sf18_preserve_student_person_identity() FROM PUBLIC")


def _drop_student_person_identity_guard() -> None:
    op.execute("DROP TRIGGER trg_sf18_preserve_student_person_identity ON public.students")
    op.execute("DROP FUNCTION public.sf18_preserve_student_person_identity()")


def _drop_residence_edges() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS trg_sf1_validate_residence_transfer "
        "ON public.student_bed_residences"
    )
    op.execute("DROP FUNCTION IF EXISTS public.sf1_validate_residence_transfer()")
    for name, constraint_type in (
        ("fk_student_bed_residences_arrived_from", "foreignkey"),
        ("fk_student_bed_residences_departed_to", "foreignkey"),
        ("uq_student_bed_residences_arrived_from", "unique"),
        ("uq_student_bed_residences_departed_to", "unique"),
        ("ck_student_bed_residences_arrived_from_not_self", "check"),
        ("ck_student_bed_residences_departed_to_not_self", "check"),
    ):
        op.drop_constraint(
            name,
            "student_bed_residences",
            type_=constraint_type,
        )
    op.drop_column("student_bed_residences", "arrived_from_residence_id")
    op.drop_column("student_bed_residences", "departed_to_residence_id")


def _add_student_campuses() -> None:
    op.add_column(
        "student_school_relationships",
        sa.Column("primary_campus_location_id", sa.Uuid(), nullable=False),
    )
    op.add_column(
        "student_school_relationships",
        sa.Column("administrative_campus_location_id", sa.Uuid(), nullable=False),
    )
    op.create_foreign_key(
        "fk_student_school_relationships_primary_campus",
        "student_school_relationships",
        "campus_locations",
        ["school_id", "primary_campus_location_id"],
        ["school_id", "id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_student_school_relationships_administrative_campus",
        "student_school_relationships",
        "campus_locations",
        ["school_id", "administrative_campus_location_id"],
        ["school_id", "id"],
        ondelete="RESTRICT",
    )


def _create_organization_campus_relationships() -> None:
    op.create_table(
        "organization_campus_relationships",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("school_id", sa.Uuid(), nullable=False),
        sa.Column("source_key", sa.String(length=255), nullable=False),
        sa.Column("organization_unit_id", sa.Uuid(), nullable=False),
        sa.Column("campus_location_id", sa.Uuid(), nullable=False),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_to", sa.DateTime(timezone=True), nullable=True),
        sa.Column("version", sa.BigInteger(), server_default="1", nullable=False),
        sa.Column("source_id", sa.Uuid(), nullable=False),
        sa.Column("import_batch_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_organization_campus_relationships"),
        sa.UniqueConstraint(
            "school_id",
            "id",
            name="uq_organization_campus_relationships_school_id_id",
        ),
        sa.ForeignKeyConstraint(
            ["school_id", "organization_unit_id"],
            ["organization_units.school_id", "organization_units.id"],
            name="fk_organization_campus_relationships_organization",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["school_id", "campus_location_id"],
            ["campus_locations.school_id", "campus_locations.id"],
            name="fk_organization_campus_relationships_campus",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["school_id", "source_id"],
            ["school_fact_sources.school_id", "school_fact_sources.id"],
            name="fk_organization_campus_relationships_source",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["school_id", "import_batch_id"],
            ["master_data_import_batches.school_id", "master_data_import_batches.id"],
            name="fk_organization_campus_relationships_import_batch",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "valid_to IS NULL OR valid_to > valid_from",
            name="ck_organization_campus_relationships_valid_interval",
        ),
        sa.CheckConstraint(
            "version >= 1",
            name="ck_organization_campus_relationships_version_positive",
        ),
        postgresql.ExcludeConstraint(
            ("school_id", "="),
            ("organization_unit_id", "="),
            ("campus_location_id", "="),
            (
                sa.text("tstzrange(valid_from, COALESCE(valid_to, 'infinity'::timestamptz), '[)')"),
                "&&",
            ),
            using="gist",
            name="excl_organization_campus_relationships_period",
        ),
    )
    op.create_index(
        "ix_organization_campus_relationships_as_of",
        "organization_campus_relationships",
        [
            "school_id",
            "organization_unit_id",
            "campus_location_id",
            "valid_from",
            "valid_to",
        ],
    )
    op.create_index(
        "ix_organization_campus_relationships_school_source_key",
        "organization_campus_relationships",
        ["school_id", "source_key", "valid_from", "valid_to"],
    )


def _create_policy_scopes() -> None:
    op.create_table(
        "school_policy_scopes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("school_id", sa.Uuid(), nullable=False),
        sa.Column("source_key", sa.String(length=255), nullable=False),
        sa.Column("policy_id", sa.Uuid(), nullable=False),
        sa.Column("organization_unit_id", sa.Uuid(), nullable=True),
        sa.Column("campus_location_id", sa.Uuid(), nullable=True),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_to", sa.DateTime(timezone=True), nullable=True),
        sa.Column("version", sa.BigInteger(), server_default="1", nullable=False),
        sa.Column("source_id", sa.Uuid(), nullable=False),
        sa.Column("import_batch_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_school_policy_scopes"),
        sa.UniqueConstraint(
            "school_id",
            "id",
            name="uq_school_policy_scopes_school_id_id",
        ),
        sa.ForeignKeyConstraint(
            ["school_id", "policy_id"],
            ["school_policies.school_id", "school_policies.id"],
            name="fk_school_policy_scopes_policy",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["school_id", "organization_unit_id"],
            ["organization_units.school_id", "organization_units.id"],
            name="fk_school_policy_scopes_organization",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["school_id", "campus_location_id"],
            ["campus_locations.school_id", "campus_locations.id"],
            name="fk_school_policy_scopes_campus",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["school_id", "source_id"],
            ["school_fact_sources.school_id", "school_fact_sources.id"],
            name="fk_school_policy_scopes_source",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["school_id", "import_batch_id"],
            ["master_data_import_batches.school_id", "master_data_import_batches.id"],
            name="fk_school_policy_scopes_import_batch",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "num_nonnulls(organization_unit_id, campus_location_id) = 1",
            name="ck_school_policy_scopes_exactly_one_target",
        ),
        sa.CheckConstraint(
            "valid_to IS NULL OR valid_to > valid_from",
            name="ck_school_policy_scopes_valid_interval",
        ),
        sa.CheckConstraint(
            "version >= 1",
            name="ck_school_policy_scopes_version_positive",
        ),
        postgresql.ExcludeConstraint(
            ("school_id", "="),
            ("policy_id", "="),
            ("organization_unit_id", "="),
            (
                sa.text("tstzrange(valid_from, COALESCE(valid_to, 'infinity'::timestamptz), '[)')"),
                "&&",
            ),
            where=sa.text("organization_unit_id IS NOT NULL"),
            using="gist",
            name="excl_school_policy_scopes_organization_period",
        ),
        postgresql.ExcludeConstraint(
            ("school_id", "="),
            ("policy_id", "="),
            ("campus_location_id", "="),
            (
                sa.text("tstzrange(valid_from, COALESCE(valid_to, 'infinity'::timestamptz), '[)')"),
                "&&",
            ),
            where=sa.text("campus_location_id IS NOT NULL"),
            using="gist",
            name="excl_school_policy_scopes_campus_period",
        ),
    )
    op.create_index(
        "ix_school_policy_scopes_as_of",
        "school_policy_scopes",
        ["school_id", "policy_id", "valid_from", "valid_to"],
    )
    op.create_index(
        "ix_school_policy_scopes_school_source_key",
        "school_policy_scopes",
        ["school_id", "source_key", "valid_from", "valid_to"],
    )


def _create_new_fact_history_delete_guards() -> None:
    for trigger_name, table_name in (
        (
            "trg_sf1_reject_delete_organization_campus_relationships",
            "organization_campus_relationships",
        ),
        ("trg_sf1_reject_delete_school_policy_scopes", "school_policy_scopes"),
    ):
        op.execute(
            f"CREATE TRIGGER {trigger_name} "
            f"BEFORE DELETE ON public.{table_name} FOR EACH ROW "
            "EXECUTE FUNCTION public.sf1_reject_history_delete()"
        )


def _drop_new_fact_history_delete_guards() -> None:
    for trigger_name, table_name in (
        ("trg_sf1_reject_delete_school_policy_scopes", "school_policy_scopes"),
        (
            "trg_sf1_reject_delete_organization_campus_relationships",
            "organization_campus_relationships",
        ),
    ):
        op.execute(f"DROP TRIGGER {trigger_name} ON public.{table_name}")


def _create_campus_target_guards() -> None:
    op.execute(
        sa.text(
            r"""
            CREATE FUNCTION public.sf18_require_campus_target()
            RETURNS trigger
            LANGUAGE plpgsql
            SECURITY DEFINER
            SET search_path = pg_catalog
            AS $function$
            DECLARE
                v_column text;
                v_school_id uuid;
                v_target_id uuid;
                v_valid_from timestamptz;
                v_valid_to timestamptz;
            BEGIN
                v_school_id := (pg_catalog.to_jsonb(NEW) ->> 'school_id')::uuid;
                v_valid_from := (pg_catalog.to_jsonb(NEW) ->> 'valid_from')::timestamptz;
                v_valid_to := NULLIF(
                    pg_catalog.to_jsonb(NEW) ->> 'valid_to', ''
                )::timestamptz;
                PERFORM pg_catalog.pg_advisory_xact_lock(
                    pg_catalog.hashtextextended(
                        'campus-ai:sf18:school-campus:' || v_school_id::text,
                        0
                    )
                );
                FOREACH v_column IN ARRAY TG_ARGV LOOP
                    v_target_id := (pg_catalog.to_jsonb(NEW) ->> v_column)::uuid;
                    IF v_target_id IS NOT NULL AND NOT (
                        COALESCE(
                            (
                                SELECT range_agg(
                                    tstzrange(
                                        campus.valid_from,
                                        COALESCE(
                                            campus.valid_to,
                                            'infinity'::timestamptz
                                        ),
                                        '[)'
                                    )
                                ) @> tstzrange(
                                    v_valid_from,
                                    COALESCE(v_valid_to, 'infinity'::timestamptz),
                                    '[)'
                                )
                                FROM public.campus_location_versions AS campus
                                WHERE campus.school_id = v_school_id
                                  AND campus.location_id = v_target_id
                                  AND campus.kind = 'campus'
                                  AND campus.status = 'confirmed'
                            ),
                            false
                        )
                        AND app.school_fact_location_history_interval_is_resolvable(
                            v_school_id,
                            v_target_id,
                            v_valid_from,
                            v_valid_to
                        )
                    ) THEN
                        RAISE EXCEPTION USING
                            ERRCODE = '23514',
                            CONSTRAINT = 'ck_school_fact_campus_target_kind',
                            MESSAGE = 'campus target must reference a campus root';
                    END IF;
                END LOOP;
                RETURN NEW;
            END
            $function$;

            CREATE TRIGGER trg_sf18_require_organization_campus_target
            BEFORE INSERT OR UPDATE ON public.organization_campus_relationships
            FOR EACH ROW
            EXECUTE FUNCTION public.sf18_require_campus_target('campus_location_id');

            CREATE TRIGGER trg_sf18_require_student_campus_targets
            BEFORE INSERT OR UPDATE ON public.student_school_relationships
            FOR EACH ROW
            EXECUTE FUNCTION public.sf18_require_campus_target(
                'primary_campus_location_id',
                'administrative_campus_location_id'
            );

            CREATE TRIGGER trg_sf18_require_policy_scope_campus_target
            BEFORE INSERT OR UPDATE ON public.school_policy_scopes
            FOR EACH ROW
            EXECUTE FUNCTION public.sf18_require_campus_target('campus_location_id');

            CREATE FUNCTION public.sf18_preserve_referenced_campus_target()
            RETURNS trigger
            LANGUAGE plpgsql
            SECURITY DEFINER
            SET search_path = pg_catalog
            AS $function$
            BEGIN
                PERFORM pg_catalog.pg_advisory_xact_lock(
                    pg_catalog.hashtextextended(
                        'campus-ai:sf18:school-campus:' || NEW.school_id::text,
                        0
                    )
                );
                IF NEW.kind <> 'campus'
                   AND (
                       EXISTS (
                           SELECT 1
                           FROM public.organization_campus_relationships AS relation
                           WHERE relation.school_id = NEW.school_id
                             AND relation.campus_location_id = NEW.location_id
                             AND tstzrange(
                                     relation.valid_from,
                                     COALESCE(relation.valid_to, 'infinity'::timestamptz),
                                     '[)'
                                 ) && tstzrange(
                                     NEW.valid_from,
                                     COALESCE(NEW.valid_to, 'infinity'::timestamptz),
                                     '[)'
                                 )
                       )
                       OR EXISTS (
                           SELECT 1
                           FROM public.student_school_relationships AS relationship
                           WHERE relationship.school_id = NEW.school_id
                             AND (
                                 relationship.primary_campus_location_id = NEW.location_id
                                 OR relationship.administrative_campus_location_id
                                    = NEW.location_id
                             )
                             AND tstzrange(
                                     relationship.valid_from,
                                     COALESCE(
                                         relationship.valid_to,
                                         'infinity'::timestamptz
                                     ),
                                     '[)'
                                 ) && tstzrange(
                                     NEW.valid_from,
                                     COALESCE(NEW.valid_to, 'infinity'::timestamptz),
                                     '[)'
                                 )
                       )
                       OR EXISTS (
                           SELECT 1
                           FROM public.school_policy_scopes AS scope
                           WHERE scope.school_id = NEW.school_id
                             AND scope.campus_location_id = NEW.location_id
                             AND tstzrange(
                                     scope.valid_from,
                                     COALESCE(scope.valid_to, 'infinity'::timestamptz),
                                     '[)'
                                 ) && tstzrange(
                                     NEW.valid_from,
                                     COALESCE(NEW.valid_to, 'infinity'::timestamptz),
                                     '[)'
                                 )
                       )
                   ) THEN
                    RAISE EXCEPTION USING
                        ERRCODE = '23514',
                        CONSTRAINT = 'ck_school_fact_referenced_campus_target_kind',
                        MESSAGE = 'a referenced campus target cannot become a non-campus node';
                END IF;
                RETURN NEW;
            END
            $function$;

            CREATE TRIGGER trg_sf18_preserve_referenced_campus_target
            BEFORE INSERT OR UPDATE OF kind, parent_location_id, valid_from, valid_to
            ON public.campus_location_versions
            FOR EACH ROW
            EXECUTE FUNCTION public.sf18_preserve_referenced_campus_target();
            """
        )
    )
    for signature in (
        "public.sf18_require_campus_target()",
        "public.sf18_preserve_referenced_campus_target()",
    ):
        op.execute(f"ALTER FUNCTION {signature} OWNER TO campus_ai_db_owner")
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")


def _create_location_residence_final_guard() -> None:
    op.execute(
        sa.text(
            r"""
            CREATE FUNCTION public.sf18_lock_location_residence_change()
            RETURNS trigger
            LANGUAGE plpgsql
            SECURITY DEFINER
            SET search_path = pg_catalog
            AS $function$
            BEGIN
                PERFORM pg_catalog.pg_advisory_xact_lock(
                    pg_catalog.hashtextextended(
                        'campus-ai:sf18:school-campus:' || NEW.school_id::text,
                        0
                    )
                );
                RETURN NEW;
            END
            $function$;

            CREATE TRIGGER trg_sf18_lock_location_terminal_residences
            BEFORE UPDATE OF status ON public.campus_locations
            FOR EACH ROW
            EXECUTE FUNCTION public.sf18_lock_location_residence_change();

            CREATE TRIGGER trg_sf18_lock_bed_location_residences
            BEFORE INSERT OR UPDATE ON public.dorm_beds
            FOR EACH ROW
            EXECUTE FUNCTION public.sf18_lock_location_residence_change();

            CREATE TRIGGER trg_sf18_lock_residence_location_ancestry
            BEFORE INSERT OR UPDATE ON public.student_bed_residences
            FOR EACH ROW
            EXECUTE FUNCTION public.sf18_lock_location_residence_change();

            CREATE FUNCTION public.sf18_validate_inactive_campus_residences()
            RETURNS trigger
            LANGUAGE plpgsql
            SECURITY DEFINER
            SET search_path = pg_catalog
            AS $function$
            BEGIN
                PERFORM pg_catalog.pg_advisory_xact_lock(
                    pg_catalog.hashtextextended(
                        'campus-ai:sf18:school-campus:' || NEW.school_id::text,
                        0
                    )
                );
                IF EXISTS (
                    SELECT 1
                    FROM public.student_bed_residences AS residence
                    JOIN public.dorm_beds AS bed
                      ON bed.school_id = residence.school_id
                     AND bed.id = residence.bed_id
                    WHERE residence.school_id = NEW.school_id
                      AND residence.revoked_at IS NULL
                      AND NOT app.school_fact_location_history_interval_is_resolvable(
                          residence.school_id,
                          bed.room_location_id,
                          residence.valid_from,
                          residence.valid_to
                      )
                ) THEN
                    RAISE EXCEPTION USING
                        ERRCODE = '23514',
                        CONSTRAINT = 'ck_student_bed_residences_active_location_ancestry',
                        MESSAGE = 'open residence cannot remain below an inactive campus ancestor';
                END IF;
                RETURN NULL;
            END
            $function$;

            CREATE CONSTRAINT TRIGGER trg_sf18_validate_location_residences
            AFTER INSERT OR UPDATE ON public.campus_location_versions
            DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW
            EXECUTE FUNCTION public.sf18_validate_inactive_campus_residences();

            CREATE CONSTRAINT TRIGGER trg_sf18_validate_location_terminal_residences
            AFTER UPDATE OF status ON public.campus_locations
            DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW
            EXECUTE FUNCTION public.sf18_validate_inactive_campus_residences();

            CREATE CONSTRAINT TRIGGER trg_sf18_validate_location_lineage_residences
            AFTER INSERT OR UPDATE ON public.school_fact_change_lineage
            DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW
            WHEN (NEW.fact_kind = 'location' AND NEW.operation IN ('end', 'revoke'))
            EXECUTE FUNCTION public.sf18_validate_inactive_campus_residences();

            CREATE CONSTRAINT TRIGGER trg_sf18_validate_bed_location_residences
            AFTER INSERT OR UPDATE ON public.dorm_beds
            DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW
            EXECUTE FUNCTION public.sf18_validate_inactive_campus_residences();

            CREATE CONSTRAINT TRIGGER trg_sf18_validate_residence_location_ancestry
            AFTER INSERT OR UPDATE ON public.student_bed_residences
            DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW
            EXECUTE FUNCTION public.sf18_validate_inactive_campus_residences();
            """
        )
    )
    for signature in (
        "public.sf18_lock_location_residence_change()",
        "public.sf18_validate_inactive_campus_residences()",
    ):
        op.execute(f"ALTER FUNCTION {signature} OWNER TO campus_ai_db_owner")
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")


def _create_temporal_execution_context_functions() -> None:
    list_signature = "app.list_execution_context_options(integer,bigint,text,timestamptz,text)"
    bind_signature = (
        "app.bind_execution_context(uuid,integer,uuid,text,uuid,bigint,uuid,bigint,"
        "bigint,uuid,text,text,text,timestamptz,text)"
    )
    op.execute(
        "GRANT SELECT (student_id) ON TABLE public.guardian_relationships "
        "TO campus_ai_context_definer"
    )
    op.execute("GRANT CREATE ON SCHEMA app TO campus_ai_context_definer")
    op.execute("SET LOCAL ROLE campus_ai_context_definer")
    op.execute(f"ALTER FUNCTION {list_signature} RENAME TO list_execution_context_options_rev07")
    op.execute(f"ALTER FUNCTION {bind_signature} RENAME TO bind_execution_context_rev07")
    for signature in (
        "app.list_execution_context_options_rev07(integer,bigint,text,timestamptz,text)",
        (
            "app.bind_execution_context_rev07(uuid,integer,uuid,text,uuid,bigint,uuid,bigint,"
            "bigint,uuid,text,text,text,timestamptz,text)"
        ),
    ):
        op.execute(
            f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC, campus_ai_runtime, "
            "campus_ai_db_owner, campus_ai_migration, campus_ai_audit_definer, "
            "campus_ai_outbox_probe_definer, campus_ai_takeover_definer"
        )
    op.execute(
        sa.text(
            r"""
            CREATE FUNCTION app.list_execution_context_options(
                p_account_user_id integer,
                p_authorization_version bigint,
                p_session_nonce_hash text,
                p_proof_expires_at timestamptz,
                p_server_proof text
            )
            RETURNS TABLE(
                context_kind text,
                context_id uuid,
                context_version bigint,
                school_id uuid,
                person_id uuid,
                display_label text
            )
            LANGUAGE plpgsql
            STABLE
            SECURITY DEFINER
            SET search_path = pg_catalog
            AS $function$
            DECLARE
                signing_key bytea;
                canonical_text text;
                expected_proof text;
                as_of timestamptz := statement_timestamp();
            BEGIN
                IF p_account_user_id IS NULL OR p_account_user_id < 1
                   OR p_authorization_version IS NULL OR p_authorization_version < 1
                   OR p_session_nonce_hash IS NULL
                   OR p_proof_expires_at IS NULL
                   OR p_server_proof IS NULL
                   OR p_session_nonce_hash !~ '^[0-9a-f]{64}$'
                   OR p_server_proof !~ '^[0-9a-f]{64}$'
                   OR p_proof_expires_at <= as_of THEN
                    RAISE EXCEPTION 'invalid or expired account options proof'
                        USING ERRCODE = '28000';
                END IF;
                SELECT keys.signing_key INTO STRICT signing_key
                FROM app_private.context_proof_keys AS keys
                WHERE keys.is_current;
                canonical_text := array_to_string(ARRAY[
                    'account-options-v1', p_account_user_id::text,
                    p_authorization_version::text, p_session_nonce_hash,
                    to_char(p_proof_expires_at AT TIME ZONE 'UTC',
                            'YYYY-MM-DD"T"HH24:MI:SS.US"Z"')
                ], chr(31));
                expected_proof := encode(
                    app_private.hmac(
                        convert_to(canonical_text, 'UTF8'), signing_key, 'sha256'
                    ),
                    'hex'
                );
                IF expected_proof IS DISTINCT FROM p_server_proof THEN
                    RAISE EXCEPTION 'account options proof verification failed'
                        USING ERRCODE = '28000';
                END IF;
                IF NOT EXISTS (
                    SELECT 1 FROM public.users AS account
                    WHERE account.id = p_account_user_id
                      AND account.is_active
                      AND account.security_version = p_authorization_version
                ) THEN
                    RAISE EXCEPTION 'account options actor is not current'
                        USING ERRCODE = '28000';
                END IF;

                RETURN QUERY
                SELECT 'assignment'::text,
                       assignment.id,
                       assignment.version,
                       binding_version.school_id,
                       binding_version.person_id,
                       school_version.display_name || ' · ' ||
                           COALESCE(unit_version.display_name, assignment_version.position_title)
                FROM public.account_person_binding_versions AS binding_version
                JOIN public.school_versions AS school_version
                  ON school_version.school_id = binding_version.school_id
                 AND school_version.lifecycle_status = 'active'
                 AND school_version.valid_from <= as_of
                 AND (school_version.valid_to IS NULL
                      OR school_version.valid_to > as_of)
                JOIN public.staff_assignment_versions AS assignment_version
                  ON assignment_version.school_id = binding_version.school_id
                 AND assignment_version.person_id = binding_version.person_id
                JOIN public.staff_assignments AS assignment
                  ON assignment.school_id = assignment_version.school_id
                 AND assignment.id = assignment_version.assignment_id
                LEFT JOIN public.organization_unit_versions AS unit_version
                  ON unit_version.school_id = assignment_version.school_id
                 AND unit_version.organization_unit_id =
                       assignment_version.organization_unit_id
                 AND unit_version.status = 'confirmed'
                 AND unit_version.valid_from <= as_of
                 AND (unit_version.valid_to IS NULL OR unit_version.valid_to > as_of)
                WHERE binding_version.account_user_id = p_account_user_id
                  AND binding_version.status = 'confirmed'
                  AND binding_version.revoked_at IS NULL
                  AND binding_version.effective_from <= as_of
                  AND (binding_version.effective_to IS NULL
                       OR binding_version.effective_to > as_of)
                  AND app_private.school_fact_relationship_slice_is_effective_at(
                      binding_version.school_id,
                      'login_mapping',
                      binding_version.binding_id,
                      binding_version.target_version,
                      as_of
                  )
                  AND assignment_version.status = 'confirmed'
                  AND assignment_version.revoked_at IS NULL
                  AND assignment_version.effective_from <= as_of
                  AND (assignment_version.effective_to IS NULL
                       OR assignment_version.effective_to > as_of)
                  AND (assignment_version.planned_valid_to IS NULL
                       OR assignment_version.planned_valid_to > as_of)
                  AND app_private.school_fact_relationship_slice_is_effective_at(
                      assignment_version.school_id,
                      'assignment',
                      assignment_version.assignment_id,
                      assignment_version.target_version,
                      as_of
                  )
                  AND app_private.school_fact_identity_root_is_effective_at(
                      binding_version.school_id,
                      'school',
                      binding_version.school_id,
                      as_of
                  )
                  AND app_private.school_fact_identity_root_is_effective_at(
                      binding_version.school_id,
                      'person',
                      binding_version.person_id,
                      as_of
                  )
                  AND app_private.school_fact_identity_root_is_effective_at(
                      assignment_version.school_id,
                      'organization',
                      assignment_version.organization_unit_id,
                      as_of
                  )
                UNION ALL
                SELECT 'guardian'::text,
                       guardian.id,
                       guardian.version,
                       binding_version.school_id,
                       binding_version.person_id,
                       school_version.display_name || ' · 家长身份'
                FROM public.account_person_binding_versions AS binding_version
                JOIN public.school_versions AS school_version
                  ON school_version.school_id = binding_version.school_id
                 AND school_version.lifecycle_status = 'active'
                 AND school_version.valid_from <= as_of
                 AND (school_version.valid_to IS NULL
                      OR school_version.valid_to > as_of)
                JOIN public.guardian_relationships AS guardian
                  ON guardian.school_id = binding_version.school_id
                 AND guardian.guardian_person_id = binding_version.person_id
                WHERE binding_version.account_user_id = p_account_user_id
                  AND binding_version.status = 'confirmed'
                  AND binding_version.revoked_at IS NULL
                  AND binding_version.effective_from <= as_of
                  AND (binding_version.effective_to IS NULL
                       OR binding_version.effective_to > as_of)
                  AND app_private.school_fact_relationship_slice_is_effective_at(
                      binding_version.school_id,
                      'login_mapping',
                      binding_version.binding_id,
                      binding_version.target_version,
                      as_of
                  )
                  AND guardian.status = 'confirmed'
                  AND guardian.valid_from <= as_of
                  AND (guardian.valid_to IS NULL OR guardian.valid_to > as_of)
                  AND app_private.school_fact_identity_root_is_effective_at(
                      binding_version.school_id,
                      'school',
                      binding_version.school_id,
                      as_of
                  )
                  AND app_private.school_fact_identity_root_is_effective_at(
                      binding_version.school_id,
                      'person',
                      binding_version.person_id,
                      as_of
                  )
                  AND app_private.school_fact_identity_root_is_effective_at(
                      guardian.school_id, 'student', guardian.student_id, as_of
                  )
                ORDER BY 1, 4, 2;
            END
            $function$;

            CREATE FUNCTION app.bind_execution_context(
                p_school_id uuid,
                p_account_user_id integer,
                p_person_id uuid,
                p_context_kind text,
                p_assignment_id uuid,
                p_assignment_version bigint,
                p_guardian_relationship_id uuid,
                p_guardian_relationship_version bigint,
                p_authorization_version bigint,
                p_request_id uuid,
                p_context_purpose text,
                p_idempotency_key_hash text,
                p_session_nonce_hash text,
                p_proof_expires_at timestamptz,
                p_server_proof text
            )
            RETURNS void
            LANGUAGE plpgsql
            VOLATILE
            SECURITY DEFINER
            SET search_path = pg_catalog
            AS $function$
            DECLARE
                current_key_version bigint;
                signing_key bytea;
                canonical_text text;
                expected_proof text;
                secondary_text text;
                secondary_proof text;
                as_of timestamptz := statement_timestamp();
            BEGIN
                IF p_school_id IS NULL
                   OR p_account_user_id IS NULL OR p_account_user_id < 1
                   OR p_person_id IS NULL
                   OR p_context_kind IS NULL
                   OR p_authorization_version IS NULL OR p_authorization_version < 1
                   OR p_request_id IS NULL
                   OR p_context_purpose IS NULL
                   OR p_session_nonce_hash IS NULL
                   OR p_proof_expires_at IS NULL
                   OR p_server_proof IS NULL
                   OR p_context_kind NOT IN ('assignment', 'guardian')
                   OR p_context_purpose NOT IN ('read', 'command')
                   OR p_session_nonce_hash !~ '^[0-9a-f]{64}$'
                   OR p_server_proof !~ '^[0-9a-f]{64}$'
                   OR p_proof_expires_at <= as_of
                   OR (p_context_purpose = 'read' AND p_idempotency_key_hash IS NOT NULL)
                   OR (p_context_purpose = 'command'
                       AND COALESCE(p_idempotency_key_hash, '') !~ '^[0-9a-f]{64}$')
                   OR (p_context_kind = 'assignment' AND (
                        p_assignment_id IS NULL OR p_assignment_version IS NULL
                        OR p_assignment_version < 1
                        OR p_guardian_relationship_id IS NOT NULL
                        OR p_guardian_relationship_version IS NOT NULL))
                   OR (p_context_kind = 'guardian' AND (
                        p_assignment_id IS NOT NULL OR p_assignment_version IS NOT NULL
                        OR p_guardian_relationship_id IS NULL
                        OR p_guardian_relationship_version IS NULL
                        OR p_guardian_relationship_version < 1)) THEN
                    RAISE EXCEPTION 'invalid or expired execution context proof'
                        USING ERRCODE = '28000';
                END IF;
                SELECT keys.key_version, keys.signing_key
                  INTO STRICT current_key_version, signing_key
                FROM app_private.context_proof_keys AS keys
                WHERE keys.is_current;
                canonical_text := array_to_string(ARRAY[
                    'context-v1', p_account_user_id::text,
                    p_authorization_version::text, p_school_id::text,
                    p_person_id::text, p_context_kind,
                    COALESCE(p_assignment_id::text, '-'),
                    COALESCE(p_assignment_version::text, '-'),
                    COALESCE(p_guardian_relationship_id::text, '-'),
                    COALESCE(p_guardian_relationship_version::text, '-'),
                    p_request_id::text, p_context_purpose,
                    COALESCE(p_idempotency_key_hash, '-'), p_session_nonce_hash,
                    to_char(p_proof_expires_at AT TIME ZONE 'UTC',
                            'YYYY-MM-DD"T"HH24:MI:SS.US"Z"')
                ], chr(31));
                expected_proof := encode(
                    app_private.hmac(
                        convert_to(canonical_text, 'UTF8'), signing_key, 'sha256'
                    ),
                    'hex'
                );
                IF expected_proof IS DISTINCT FROM p_server_proof THEN
                    RAISE EXCEPTION 'execution context proof verification failed'
                        USING ERRCODE = '28000';
                END IF;

                PERFORM set_config('app.context_key_version', current_key_version::text, true);
                PERFORM set_config('app.school_id', p_school_id::text, true);
                PERFORM set_config('app.account_user_id', p_account_user_id::text, true);
                PERFORM set_config('app.person_id', p_person_id::text, true);
                PERFORM set_config('app.context_kind', p_context_kind, true);
                PERFORM set_config(
                    'app.assignment_id', COALESCE(p_assignment_id::text, '-'), true
                );
                PERFORM set_config(
                    'app.assignment_version', COALESCE(p_assignment_version::text, '-'), true
                );
                PERFORM set_config(
                    'app.guardian_relationship_id',
                    COALESCE(p_guardian_relationship_id::text, '-'), true
                );
                PERFORM set_config(
                    'app.guardian_relationship_version',
                    COALESCE(p_guardian_relationship_version::text, '-'), true
                );
                PERFORM set_config(
                    'app.authorization_version', p_authorization_version::text, true
                );
                PERFORM set_config('app.request_id', p_request_id::text, true);
                PERFORM set_config('app.context_purpose', p_context_purpose, true);
                PERFORM set_config(
                    'app.idempotency_key_hash',
                    COALESCE(p_idempotency_key_hash, '-'), true
                );
                PERFORM set_config('app.session_nonce_hash', p_session_nonce_hash, true);
                PERFORM set_config(
                    'app.proof_expires_at',
                    to_char(p_proof_expires_at AT TIME ZONE 'UTC',
                            'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'), true
                );
                PERFORM set_config('app.server_proof', p_server_proof, true);
                PERFORM set_config('app.binding_database', current_database(), true);
                PERFORM set_config('app.binding_backend_pid', pg_backend_pid()::text, true);
                PERFORM set_config('app.binding_xact_id', pg_current_xact_id()::text, true);
                secondary_text := array_to_string(ARRAY[
                    'bound-context-v1', current_database(), pg_backend_pid()::text,
                    pg_current_xact_id()::text, current_key_version::text,
                    canonical_text, p_server_proof
                ], chr(31));
                secondary_proof := encode(
                    app_private.hmac(
                        convert_to(secondary_text, 'UTF8'), signing_key, 'sha256'
                    ),
                    'hex'
                );
                PERFORM set_config('app.binding_proof', secondary_proof, true);

                IF NOT EXISTS (
                    SELECT 1
                    FROM public.users AS account
                    JOIN public.account_person_binding_versions AS binding_version
                      ON binding_version.account_user_id = account.id
                     AND binding_version.school_id = p_school_id
                     AND binding_version.person_id = p_person_id
                     AND binding_version.status = 'confirmed'
                     AND binding_version.revoked_at IS NULL
                     AND binding_version.effective_from <= as_of
                     AND (binding_version.effective_to IS NULL
                          OR binding_version.effective_to > as_of)
                     AND app_private.school_fact_relationship_slice_is_effective_at(
                         binding_version.school_id,
                         'login_mapping',
                         binding_version.binding_id,
                         binding_version.target_version,
                         as_of
                     )
                    WHERE account.id = p_account_user_id
                      AND account.is_active
                      AND account.security_version = p_authorization_version
                      AND app_private.school_fact_identity_root_is_effective_at(
                          binding_version.school_id,
                          'school',
                          binding_version.school_id,
                          as_of
                      )
                      AND app_private.school_fact_identity_root_is_effective_at(
                          binding_version.school_id,
                          'person',
                          binding_version.person_id,
                          as_of
                      )
                ) THEN
                    RAISE EXCEPTION 'execution context actor is not current'
                        USING ERRCODE = '28000';
                END IF;
                IF p_context_kind = 'assignment' AND NOT EXISTS (
                    SELECT 1
                    FROM public.staff_assignments AS assignment
                    JOIN public.staff_assignment_versions AS assignment_version
                      ON assignment_version.school_id = assignment.school_id
                     AND assignment_version.assignment_id = assignment.id
                    WHERE assignment.school_id = p_school_id
                      AND assignment.id = p_assignment_id
                      AND assignment_version.person_id = p_person_id
                      AND assignment.version = p_assignment_version
                      AND assignment_version.status = 'confirmed'
                      AND assignment_version.revoked_at IS NULL
                      AND assignment_version.effective_from <= as_of
                      AND (assignment_version.effective_to IS NULL
                           OR assignment_version.effective_to > as_of)
                      AND (assignment_version.planned_valid_to IS NULL
                           OR assignment_version.planned_valid_to > as_of)
                      AND app_private.school_fact_relationship_slice_is_effective_at(
                          assignment_version.school_id,
                          'assignment',
                          assignment_version.assignment_id,
                          assignment_version.target_version,
                          as_of
                      )
                      AND app_private.school_fact_identity_root_is_effective_at(
                          assignment_version.school_id,
                          'organization',
                          assignment_version.organization_unit_id,
                          as_of
                      )
                ) THEN
                    RAISE EXCEPTION 'execution context assignment is not current'
                        USING ERRCODE = '28000';
                END IF;
                IF p_context_kind = 'guardian' AND NOT EXISTS (
                    SELECT 1 FROM public.guardian_relationships AS relationship
                    WHERE relationship.school_id = p_school_id
                      AND relationship.id = p_guardian_relationship_id
                      AND relationship.guardian_person_id = p_person_id
                      AND relationship.version = p_guardian_relationship_version
                      AND relationship.status = 'confirmed'
                      AND relationship.valid_from <= as_of
                      AND (relationship.valid_to IS NULL OR relationship.valid_to > as_of)
                      AND app_private.school_fact_identity_root_is_effective_at(
                          relationship.school_id,
                          'student',
                          relationship.student_id,
                          as_of
                      )
                ) THEN
                    RAISE EXCEPTION 'execution context guardian relationship is not current'
                        USING ERRCODE = '28000';
                END IF;
            END
            $function$;
            """
        )
    )
    for signature in (list_signature, bind_signature):
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO campus_ai_runtime")
    op.execute(f"GRANT EXECUTE ON FUNCTION {bind_signature} TO campus_ai_db_owner")
    op.execute("SET LOCAL ROLE campus_ai_db_owner")
    op.execute("REVOKE CREATE ON SCHEMA app FROM campus_ai_context_definer")


def _drop_temporal_execution_context_functions() -> None:
    list_signature = "app.list_execution_context_options(integer,bigint,text,timestamptz,text)"
    bind_signature = (
        "app.bind_execution_context(uuid,integer,uuid,text,uuid,bigint,uuid,bigint,"
        "bigint,uuid,text,text,text,timestamptz,text)"
    )
    op.execute("GRANT CREATE ON SCHEMA app TO campus_ai_context_definer")
    op.execute("SET LOCAL ROLE campus_ai_context_definer")
    op.execute(f"DROP FUNCTION {bind_signature}")
    op.execute(f"DROP FUNCTION {list_signature}")
    op.execute(
        "ALTER FUNCTION app.bind_execution_context_rev07("
        "uuid,integer,uuid,text,uuid,bigint,uuid,bigint,bigint,uuid,text,text,text,"
        "timestamptz,text) RENAME TO bind_execution_context"
    )
    op.execute(
        "ALTER FUNCTION app.list_execution_context_options_rev07("
        "integer,bigint,text,timestamptz,text) RENAME TO list_execution_context_options"
    )
    op.execute(f"GRANT EXECUTE ON FUNCTION {list_signature} TO campus_ai_runtime")
    op.execute(f"GRANT EXECUTE ON FUNCTION {bind_signature} TO campus_ai_runtime")
    op.execute(f"GRANT EXECUTE ON FUNCTION {bind_signature} TO campus_ai_db_owner")
    op.execute("SET LOCAL ROLE campus_ai_db_owner")
    op.execute("REVOKE CREATE ON SCHEMA app FROM campus_ai_context_definer")
    op.execute(
        "REVOKE SELECT (student_id) ON TABLE public.guardian_relationships "
        "FROM campus_ai_context_definer"
    )


def _create_school_reactivation_context() -> None:
    list_signature = "app.list_school_reactivation_candidates(integer,bigint,text,timestamptz,text)"
    candidate_signature = (
        "app.validate_school_reactivation_candidate(uuid,integer,uuid,text,uuid,bigint,uuid,"
        "bigint,bigint,uuid,text,text,text,timestamptz,text)"
    )
    bind_signature = (
        "app.bind_school_reactivation_context(uuid,integer,uuid,text,uuid,bigint,uuid,"
        "bigint,bigint,uuid,text,text,text,timestamptz,text,text,text,text,bigint,text,"
        "text,text,bigint)"
    )
    require_signature = "app.require_school_reactivation_context()"
    replay_signature = "app.school_reactivation_replay_result()"
    op.execute(
        "GRANT SELECT (source_key, version) ON TABLE public.schools TO campus_ai_context_definer"
    )
    op.execute("GRANT UPDATE (status) ON TABLE public.schools TO campus_ai_context_definer")
    op.execute(
        "GRANT SELECT (school_id, id, mode, status, planned_units, succeeded_units, "
        "failed_units, pending_units, request_id, idempotency_key_hash, "
        "source_sha256, plan_sha256, base_fact_version, target_fact_version, "
        "confirmation_token_sha256, "
        "confirmation_expires_at, confirmation_consumed_at, confirmation_session_sha256, "
        "result_payload, "
        "staged_by_user_id, staged_by_person_id, staged_by_assignment_id, "
        "staged_assignment_version, staged_authorization_version) "
        "ON TABLE public.master_data_import_batches TO campus_ai_context_definer"
    )
    op.execute(
        "GRANT SELECT (school_id, id, batch_id, sequence, unit_kind, status, payload, "
        "payload_sha256, "
        "target_id, target_version) ON TABLE public.master_data_import_units "
        "TO campus_ai_context_definer"
    )
    op.execute(
        "GRANT SELECT (school_id, batch_id, unit_id, fact_kind, fact_source_key, "
        "operation, target_id, target_version) "
        "ON TABLE public.school_fact_change_lineage TO campus_ai_context_definer"
    )
    for table_name in (
        "master_data_import_batches",
        "master_data_import_units",
        "school_fact_change_lineage",
    ):
        op.execute(
            f"CREATE POLICY school_reactivation_context_select ON public.{table_name} "
            "FOR SELECT TO campus_ai_context_definer USING (true)"
        )

    op.execute("GRANT CREATE ON SCHEMA app TO campus_ai_context_definer")
    op.execute("SET LOCAL ROLE campus_ai_context_definer")
    op.execute(
        sa.text(
            r"""
            CREATE FUNCTION app.list_school_reactivation_candidates(
                p_account_user_id integer,
                p_authorization_version bigint,
                p_session_nonce_hash text,
                p_proof_expires_at timestamptz,
                p_server_proof text
            )
            RETURNS TABLE(
                context_kind text,
                context_id uuid,
                context_version bigint,
                school_id uuid,
                person_id uuid,
                display_label text
            )
            LANGUAGE plpgsql
            STABLE
            SECURITY DEFINER
            SET search_path = pg_catalog
            AS $function$
            DECLARE
                v_as_of timestamptz := statement_timestamp();
                v_signing_key bytea;
                v_canonical text;
                v_expected_proof text;
            BEGIN
                IF p_account_user_id IS NULL OR p_account_user_id < 1
                   OR p_authorization_version IS NULL OR p_authorization_version < 1
                   OR p_session_nonce_hash IS NULL
                   OR p_session_nonce_hash !~ '^[0-9a-f]{64}$'
                   OR p_proof_expires_at IS NULL OR p_proof_expires_at <= v_as_of
                   OR p_server_proof IS NULL
                   OR p_server_proof !~ '^[0-9a-f]{64}$' THEN
                    RAISE EXCEPTION 'school reactivation candidates rejected'
                        USING ERRCODE = '28000';
                END IF;

                SELECT keys.signing_key INTO STRICT v_signing_key
                FROM app_private.context_proof_keys AS keys
                WHERE keys.is_current;
                v_canonical := array_to_string(ARRAY[
                    'account-options-v1',
                    p_account_user_id::text,
                    p_authorization_version::text,
                    p_session_nonce_hash,
                    to_char(
                        p_proof_expires_at AT TIME ZONE 'UTC',
                        'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'
                    )
                ], chr(31));
                v_expected_proof := encode(
                    app_private.hmac(
                        convert_to(v_canonical, 'UTF8'), v_signing_key, 'sha256'
                    ),
                    'hex'
                );
                IF v_expected_proof IS DISTINCT FROM p_server_proof THEN
                    RAISE EXCEPTION 'school reactivation candidates rejected'
                        USING ERRCODE = '28000';
                END IF;
                IF NOT EXISTS (
                    SELECT 1
                    FROM public.users AS account
                    WHERE account.id = p_account_user_id
                      AND account.is_active
                      AND account.security_version = p_authorization_version
                ) THEN
                    RAISE EXCEPTION 'school reactivation candidates rejected'
                        USING ERRCODE = '28000';
                END IF;

                RETURN QUERY
                SELECT 'assignment'::text,
                       assignment.id,
                       assignment.version,
                       binding_version.school_id,
                       binding_version.person_id,
                       school_version.display_name || ' · ' ||
                           COALESCE(
                               unit_version.display_name,
                               assignment_version.position_title
                           )
                FROM public.account_person_binding_versions AS binding_version
                JOIN public.school_versions AS school_version
                  ON school_version.school_id = binding_version.school_id
                 AND school_version.lifecycle_status = 'active'
                 AND school_version.valid_from <= v_as_of
                 AND (school_version.valid_to IS NULL
                      OR school_version.valid_to > v_as_of)
                JOIN public.staff_assignment_versions AS assignment_version
                  ON assignment_version.school_id = binding_version.school_id
                 AND assignment_version.person_id = binding_version.person_id
                JOIN public.staff_assignments AS assignment
                  ON assignment.school_id = assignment_version.school_id
                 AND assignment.id = assignment_version.assignment_id
                LEFT JOIN public.organization_unit_versions AS unit_version
                  ON unit_version.school_id = assignment_version.school_id
                 AND unit_version.organization_unit_id =
                       assignment_version.organization_unit_id
                 AND unit_version.status = 'confirmed'
                 AND unit_version.valid_from <= v_as_of
                 AND (unit_version.valid_to IS NULL OR unit_version.valid_to > v_as_of)
                WHERE binding_version.account_user_id = p_account_user_id
                  AND binding_version.status = 'confirmed'
                  AND binding_version.revoked_at IS NULL
                  AND binding_version.effective_from <= v_as_of
                  AND (binding_version.effective_to IS NULL
                       OR binding_version.effective_to > v_as_of)
                  AND app_private.school_fact_relationship_slice_is_effective_at(
                      binding_version.school_id,
                      'login_mapping',
                      binding_version.binding_id,
                      binding_version.target_version,
                      v_as_of
                  )
                  AND assignment_version.status = 'confirmed'
                  AND assignment_version.revoked_at IS NULL
                  AND assignment_version.effective_from <= v_as_of
                  AND (assignment_version.effective_to IS NULL
                       OR assignment_version.effective_to > v_as_of)
                  AND (assignment_version.planned_valid_to IS NULL
                       OR assignment_version.planned_valid_to > v_as_of)
                  AND app_private.school_fact_relationship_slice_is_effective_at(
                      assignment_version.school_id,
                      'assignment',
                      assignment_version.assignment_id,
                      assignment_version.target_version,
                      v_as_of
                  )
                  AND NOT app_private.school_fact_identity_root_is_effective_at(
                      binding_version.school_id,
                      'school',
                      binding_version.school_id,
                      v_as_of
                  )
                  AND app_private.school_fact_identity_root_is_effective_at(
                      binding_version.school_id,
                      'person',
                      binding_version.person_id,
                      v_as_of
                  )
                  AND app_private.school_fact_identity_root_is_effective_at(
                      assignment_version.school_id,
                      'organization',
                      assignment_version.organization_unit_id,
                      v_as_of
                  )
                ORDER BY binding_version.school_id, assignment.id;
            END
            $function$;

            CREATE FUNCTION app.validate_school_reactivation_candidate(
                p_school_id uuid,
                p_account_user_id integer,
                p_person_id uuid,
                p_context_kind text,
                p_assignment_id uuid,
                p_assignment_version bigint,
                p_guardian_relationship_id uuid,
                p_guardian_relationship_version bigint,
                p_authorization_version bigint,
                p_request_id uuid,
                p_context_purpose text,
                p_idempotency_key_hash text,
                p_session_nonce_hash text,
                p_proof_expires_at timestamptz,
                p_server_proof text
            )
            RETURNS void
            LANGUAGE plpgsql
            STABLE
            SECURITY DEFINER
            SET search_path = pg_catalog
            AS $function$
            DECLARE
                v_as_of timestamptz := statement_timestamp();
                v_signing_key bytea;
                v_canonical text;
                v_expected_proof text;
            BEGIN
                IF p_school_id IS NULL
                   OR p_account_user_id IS NULL OR p_account_user_id < 1
                   OR p_person_id IS NULL
                   OR p_context_kind IS DISTINCT FROM 'assignment'
                   OR p_assignment_id IS NULL
                   OR p_assignment_version IS NULL OR p_assignment_version < 1
                   OR p_guardian_relationship_id IS NOT NULL
                   OR p_guardian_relationship_version IS NOT NULL
                   OR p_authorization_version IS NULL OR p_authorization_version < 1
                   OR p_request_id IS NULL
                   OR p_context_purpose IS DISTINCT FROM 'read'
                   OR p_idempotency_key_hash IS NOT NULL
                   OR p_session_nonce_hash IS NULL
                   OR p_session_nonce_hash !~ '^[0-9a-f]{64}$'
                   OR p_proof_expires_at IS NULL OR p_proof_expires_at <= v_as_of
                   OR p_server_proof IS NULL
                   OR p_server_proof !~ '^[0-9a-f]{64}$' THEN
                    RAISE EXCEPTION 'school reactivation candidate rejected'
                        USING ERRCODE = '28000';
                END IF;

                SELECT keys.signing_key INTO STRICT v_signing_key
                FROM app_private.context_proof_keys AS keys
                WHERE keys.is_current;
                v_canonical := array_to_string(ARRAY[
                    'context-v1',
                    p_account_user_id::text,
                    p_authorization_version::text,
                    p_school_id::text,
                    p_person_id::text,
                    p_context_kind,
                    p_assignment_id::text,
                    p_assignment_version::text,
                    '-',
                    '-',
                    p_request_id::text,
                    p_context_purpose,
                    '-',
                    p_session_nonce_hash,
                    to_char(
                        p_proof_expires_at AT TIME ZONE 'UTC',
                        'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'
                    )
                ], chr(31));
                v_expected_proof := encode(
                    app_private.hmac(
                        convert_to(v_canonical, 'UTF8'), v_signing_key, 'sha256'
                    ),
                    'hex'
                );
                IF v_expected_proof IS DISTINCT FROM p_server_proof THEN
                    RAISE EXCEPTION 'school reactivation candidate rejected'
                        USING ERRCODE = '28000';
                END IF;

                IF NOT EXISTS (
                    SELECT 1
                    FROM public.users AS account
                    JOIN public.account_person_binding_versions AS binding_version
                      ON binding_version.account_user_id = account.id
                     AND binding_version.school_id = p_school_id
                     AND binding_version.person_id = p_person_id
                     AND binding_version.status = 'confirmed'
                     AND binding_version.revoked_at IS NULL
                     AND binding_version.effective_from <= v_as_of
                     AND (binding_version.effective_to IS NULL
                          OR binding_version.effective_to > v_as_of)
                     AND app_private.school_fact_relationship_slice_is_effective_at(
                         binding_version.school_id,
                         'login_mapping',
                         binding_version.binding_id,
                         binding_version.target_version,
                         v_as_of
                     )
                    JOIN public.schools AS school
                      ON school.id = binding_version.school_id
                    JOIN public.staff_assignments AS assignment
                      ON assignment.school_id = binding_version.school_id
                     AND assignment.id = p_assignment_id
                     AND assignment.version = p_assignment_version
                    JOIN public.staff_assignment_versions AS assignment_version
                      ON assignment_version.school_id = assignment.school_id
                     AND assignment_version.assignment_id = assignment.id
                     AND assignment_version.person_id = p_person_id
                     AND assignment_version.status = 'confirmed'
                     AND assignment_version.revoked_at IS NULL
                     AND assignment_version.effective_from <= v_as_of
                     AND (assignment_version.effective_to IS NULL
                          OR assignment_version.effective_to > v_as_of)
                     AND (assignment_version.planned_valid_to IS NULL
                          OR assignment_version.planned_valid_to > v_as_of)
                     AND app_private.school_fact_relationship_slice_is_effective_at(
                         assignment_version.school_id,
                         'assignment',
                         assignment_version.assignment_id,
                         assignment_version.target_version,
                         v_as_of
                     )
                    WHERE account.id = p_account_user_id
                      AND account.is_active
                      AND account.security_version = p_authorization_version
                      AND NOT app_private.school_fact_identity_root_is_effective_at(
                          binding_version.school_id,
                          'school',
                          binding_version.school_id,
                          v_as_of
                      )
                      AND app_private.school_fact_identity_root_is_effective_at(
                          binding_version.school_id,
                          'person',
                          binding_version.person_id,
                          v_as_of
                      )
                      AND app_private.school_fact_identity_root_is_effective_at(
                          assignment_version.school_id,
                          'organization',
                          assignment_version.organization_unit_id,
                          v_as_of
                      )
                ) THEN
                    RAISE EXCEPTION 'school reactivation candidate rejected'
                        USING ERRCODE = '28000';
                END IF;
            END
            $function$;

            CREATE FUNCTION app.require_school_reactivation_context()
            RETURNS TABLE(
                school_id uuid,
                batch_id uuid,
                unit_id uuid,
                fact_source_key text,
                expected_version bigint
            )
            LANGUAGE plpgsql
            STABLE
            SECURITY DEFINER
            SET search_path = pg_catalog
            AS $function$
            DECLARE
                v_school_id uuid;
                v_key_version text := current_setting('app.context_key_version', true);
                v_batch_id text := current_setting(
                    'app.school_reactivation_batch_id', true
                );
                v_unit_id text := current_setting('app.school_reactivation_unit_id', true);
                v_source_key text := current_setting(
                    'app.school_reactivation_source_key', true
                );
                v_expected_version text := current_setting(
                    'app.school_reactivation_expected_version', true
                );
                v_token_sha256 text := current_setting(
                    'app.school_reactivation_confirmation_token_sha256', true
                );
                v_session_sha256 text := current_setting(
                    'app.school_reactivation_session_sha256', true
                );
                v_binding_proof text := current_setting(
                    'app.school_reactivation_binding_proof', true
                );
                v_signing_key bytea;
                v_canonical text;
                v_expected_proof text;
            BEGIN
                v_school_id := app.require_school_context();
                IF current_setting('app.context_purpose', true) IS DISTINCT FROM 'command'
                   OR v_key_version IS NULL OR v_key_version = ''
                   OR v_batch_id IS NULL OR v_batch_id = ''
                   OR v_unit_id IS NULL OR v_unit_id = ''
                   OR v_source_key IS NULL OR v_source_key = ''
                   OR v_expected_version IS NULL OR v_expected_version = ''
                   OR v_token_sha256 !~ '^[0-9a-f]{64}$'
                   OR v_session_sha256 !~ '^[0-9a-f]{64}$'
                   OR v_binding_proof !~ '^[0-9a-f]{64}$' THEN
                    RAISE EXCEPTION 'school reactivation context is unavailable'
                        USING ERRCODE = '28000';
                END IF;

                SELECT keys.signing_key INTO v_signing_key
                FROM app_private.context_proof_keys AS keys
                WHERE keys.is_current AND keys.key_version::text = v_key_version;
                IF v_signing_key IS NULL THEN
                    RAISE EXCEPTION 'school reactivation context is unavailable'
                        USING ERRCODE = '28000';
                END IF;
                v_canonical := array_to_string(ARRAY[
                    'school-reactivation-bound-v1',
                    current_database(),
                    pg_backend_pid()::text,
                    pg_current_xact_id()::text,
                    v_key_version,
                    v_school_id::text,
                    v_batch_id,
                    v_unit_id,
                    v_source_key,
                    v_expected_version,
                    v_token_sha256,
                    v_session_sha256
                ], chr(31));
                v_expected_proof := encode(
                    app_private.hmac(
                        convert_to(v_canonical, 'UTF8'), v_signing_key, 'sha256'
                    ),
                    'hex'
                );
                IF v_expected_proof IS DISTINCT FROM v_binding_proof THEN
                    RAISE EXCEPTION 'school reactivation context is unavailable'
                        USING ERRCODE = '28000';
                END IF;

                RETURN QUERY SELECT
                    v_school_id,
                    v_batch_id::uuid,
                    v_unit_id::uuid,
                    v_source_key,
                    v_expected_version::bigint;
            EXCEPTION
                WHEN invalid_text_representation OR numeric_value_out_of_range THEN
                    RAISE EXCEPTION 'school reactivation context is unavailable'
                        USING ERRCODE = '28000';
            END
            $function$;

            CREATE FUNCTION app.school_reactivation_replay_result()
            RETURNS TABLE(
                batch_id uuid,
                target_version bigint,
                changed_count integer
            )
            LANGUAGE plpgsql
            STABLE
            SECURITY DEFINER
            SET search_path = pg_catalog
            AS $function$
            DECLARE
                v_key_version text := current_setting(
                    'app.school_reactivation_replay_key_version', true
                );
                v_batch_id text := current_setting(
                    'app.school_reactivation_replay_batch_id', true
                );
                v_target_version text := current_setting(
                    'app.school_reactivation_replay_target_version', true
                );
                v_changed_count text := current_setting(
                    'app.school_reactivation_replay_changed_count', true
                );
                v_token_sha256 text := current_setting(
                    'app.school_reactivation_replay_confirmation_token_sha256', true
                );
                v_session_sha256 text := current_setting(
                    'app.school_reactivation_replay_session_sha256', true
                );
                v_binding_proof text := current_setting(
                    'app.school_reactivation_replay_binding_proof', true
                );
                v_signing_key bytea;
                v_canonical text;
                v_expected_proof text;
            BEGIN
                IF v_batch_id IS NULL OR v_batch_id = '' THEN
                    RETURN;
                END IF;
                IF v_key_version IS NULL OR v_key_version = ''
                   OR v_target_version IS NULL OR v_target_version = ''
                   OR v_changed_count IS NULL OR v_changed_count = ''
                   OR v_token_sha256 !~ '^[0-9a-f]{64}$'
                   OR v_session_sha256 !~ '^[0-9a-f]{64}$'
                   OR v_binding_proof !~ '^[0-9a-f]{64}$' THEN
                    RAISE EXCEPTION 'school reactivation replay is unavailable'
                        USING ERRCODE = '28000';
                END IF;
                SELECT keys.signing_key INTO v_signing_key
                FROM app_private.context_proof_keys AS keys
                WHERE keys.is_current AND keys.key_version::text = v_key_version;
                IF v_signing_key IS NULL THEN
                    RAISE EXCEPTION 'school reactivation replay is unavailable'
                        USING ERRCODE = '28000';
                END IF;
                v_canonical := array_to_string(ARRAY[
                    'school-reactivation-replay-v1',
                    current_database(),
                    pg_backend_pid()::text,
                    pg_current_xact_id()::text,
                    v_key_version,
                    v_batch_id,
                    v_target_version,
                    v_changed_count,
                    v_token_sha256,
                    v_session_sha256
                ], chr(31));
                v_expected_proof := encode(
                    app_private.hmac(
                        convert_to(v_canonical, 'UTF8'), v_signing_key, 'sha256'
                    ),
                    'hex'
                );
                IF v_expected_proof IS DISTINCT FROM v_binding_proof THEN
                    RAISE EXCEPTION 'school reactivation replay is unavailable'
                        USING ERRCODE = '28000';
                END IF;
                IF NOT EXISTS (
                    SELECT 1
                    FROM public.master_data_import_batches AS batch
                    WHERE batch.id = v_batch_id::uuid
                      AND batch.status = 'applied'
                      AND batch.confirmation_consumed_at IS NOT NULL
                      AND batch.result_payload ->> 'target_version' = v_target_version
                      AND batch.result_payload ->> 'changed_count' = v_changed_count
                ) THEN
                    RAISE EXCEPTION 'school reactivation replay is unavailable'
                        USING ERRCODE = '28000';
                END IF;
                RETURN QUERY SELECT
                    v_batch_id::uuid,
                    v_target_version::bigint,
                    v_changed_count::integer;
            EXCEPTION
                WHEN invalid_text_representation OR numeric_value_out_of_range THEN
                    RAISE EXCEPTION 'school reactivation replay is unavailable'
                        USING ERRCODE = '28000';
            END
            $function$;

            CREATE FUNCTION app.bind_school_reactivation_context(
                p_school_id uuid,
                p_account_user_id integer,
                p_person_id uuid,
                p_context_kind text,
                p_assignment_id uuid,
                p_assignment_version bigint,
                p_guardian_relationship_id uuid,
                p_guardian_relationship_version bigint,
                p_authorization_version bigint,
                p_request_id uuid,
                p_context_purpose text,
                p_idempotency_key_hash text,
                p_session_nonce_hash text,
                p_proof_expires_at timestamptz,
                p_server_proof text,
                p_confirmation_token_sha256 text,
                p_confirmation_session_sha256 text,
                p_fact_source_key text,
                p_expected_version bigint,
                p_plan_sha256 text,
                p_source_sha256 text,
                p_unit_payload_sha256 text,
                p_base_version bigint
            )
            RETURNS void
            LANGUAGE plpgsql
            VOLATILE
            SECURITY DEFINER
            SET search_path = pg_catalog
            AS $function$
            DECLARE
                v_as_of timestamptz := statement_timestamp();
                v_batch_id uuid;
                v_unit_id uuid;
                v_pending boolean;
                v_target_version bigint;
                v_changed_count integer;
                v_signing_key bytea;
                v_key_version bigint;
                v_context_canonical text;
                v_expected_context_proof text;
                v_marker_canonical text;
                v_marker_proof text;
            BEGIN
                IF p_school_id IS NULL
                   OR p_account_user_id IS NULL OR p_account_user_id < 1
                   OR p_person_id IS NULL
                   OR p_context_kind IS DISTINCT FROM 'assignment'
                   OR p_assignment_id IS NULL
                   OR p_assignment_version IS NULL OR p_assignment_version < 1
                   OR p_guardian_relationship_id IS NOT NULL
                   OR p_guardian_relationship_version IS NOT NULL
                   OR p_authorization_version IS NULL OR p_authorization_version < 1
                   OR p_request_id IS NULL
                   OR p_context_purpose IS DISTINCT FROM 'command'
                   OR p_idempotency_key_hash !~ '^[0-9a-f]{64}$'
                   OR p_session_nonce_hash !~ '^[0-9a-f]{64}$'
                   OR p_proof_expires_at IS NULL OR p_proof_expires_at <= v_as_of
                   OR p_server_proof !~ '^[0-9a-f]{64}$'
                   OR p_confirmation_token_sha256 !~ '^[0-9a-f]{64}$'
                   OR p_confirmation_session_sha256 !~ '^[0-9a-f]{64}$'
                   OR p_fact_source_key IS NULL OR p_fact_source_key = ''
                   OR octet_length(convert_to(p_fact_source_key, 'UTF8')) > 255
                   OR p_expected_version IS NULL OR p_expected_version < 1
                   OR p_plan_sha256 !~ '^[0-9a-f]{64}$'
                   OR p_source_sha256 !~ '^[0-9a-f]{64}$'
                   OR p_unit_payload_sha256 !~ '^[0-9a-f]{64}$'
                   OR p_base_version IS NULL OR p_base_version < 1 THEN
                    RAISE EXCEPTION 'school reactivation context rejected'
                        USING ERRCODE = '28000';
                END IF;

                SELECT keys.key_version, keys.signing_key
                  INTO STRICT v_key_version, v_signing_key
                FROM app_private.context_proof_keys AS keys
                WHERE keys.is_current;
                v_context_canonical := array_to_string(ARRAY[
                    'context-v1',
                    p_account_user_id::text,
                    p_authorization_version::text,
                    p_school_id::text,
                    p_person_id::text,
                    p_context_kind,
                    p_assignment_id::text,
                    p_assignment_version::text,
                    '-',
                    '-',
                    p_request_id::text,
                    p_context_purpose,
                    p_idempotency_key_hash,
                    p_session_nonce_hash,
                    to_char(
                        p_proof_expires_at AT TIME ZONE 'UTC',
                        'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'
                    )
                ], chr(31));
                v_expected_context_proof := encode(
                    app_private.hmac(
                        convert_to(v_context_canonical, 'UTF8'),
                        v_signing_key,
                        'sha256'
                    ),
                    'hex'
                );
                IF v_expected_context_proof IS DISTINCT FROM p_server_proof THEN
                    RAISE EXCEPTION 'school reactivation context rejected'
                        USING ERRCODE = '28000';
                END IF;

                IF NOT EXISTS (
                    SELECT 1
                    FROM public.users AS account
                    JOIN public.account_person_binding_versions AS binding_version
                      ON binding_version.account_user_id = account.id
                     AND binding_version.school_id = p_school_id
                     AND binding_version.person_id = p_person_id
                     AND binding_version.status = 'confirmed'
                     AND binding_version.revoked_at IS NULL
                     AND binding_version.effective_from <= v_as_of
                     AND (binding_version.effective_to IS NULL
                          OR binding_version.effective_to > v_as_of)
                     AND app_private.school_fact_relationship_slice_is_effective_at(
                         binding_version.school_id,
                         'login_mapping',
                         binding_version.binding_id,
                         binding_version.target_version,
                         v_as_of
                     )
                    JOIN public.staff_assignments AS assignment
                      ON assignment.school_id = binding_version.school_id
                     AND assignment.id = p_assignment_id
                     AND assignment.version = p_assignment_version
                    JOIN public.staff_assignment_versions AS assignment_version
                      ON assignment_version.school_id = assignment.school_id
                     AND assignment_version.assignment_id = assignment.id
                     AND assignment_version.person_id = p_person_id
                     AND assignment_version.status = 'confirmed'
                     AND assignment_version.revoked_at IS NULL
                     AND assignment_version.effective_from <= v_as_of
                     AND (assignment_version.effective_to IS NULL
                          OR assignment_version.effective_to > v_as_of)
                     AND (assignment_version.planned_valid_to IS NULL
                          OR assignment_version.planned_valid_to > v_as_of)
                     AND app_private.school_fact_relationship_slice_is_effective_at(
                         assignment_version.school_id,
                         'assignment',
                         assignment_version.assignment_id,
                         assignment_version.target_version,
                         v_as_of
                     )
                    WHERE account.id = p_account_user_id
                      AND account.is_active
                      AND account.security_version = p_authorization_version
                      AND app_private.school_fact_identity_root_is_effective_at(
                          binding_version.school_id,
                          'person',
                          binding_version.person_id,
                          v_as_of
                      )
                      AND app_private.school_fact_identity_root_is_effective_at(
                          assignment_version.school_id,
                          'organization',
                          assignment_version.organization_unit_id,
                          v_as_of
                      )
                ) THEN
                    RAISE EXCEPTION 'school reactivation context rejected'
                        USING ERRCODE = '28000';
                END IF;

                PERFORM pg_advisory_xact_lock(
                    hashtextextended(p_confirmation_token_sha256, 0)
                );
                SELECT batch.id, unit.id,
                       batch.status = 'ready_for_confirmation',
                       CASE WHEN batch.status = 'applied'
                            THEN (batch.result_payload ->> 'target_version')::bigint
                            ELSE NULL END,
                       CASE WHEN batch.status = 'applied'
                            THEN (batch.result_payload ->> 'changed_count')::integer
                            ELSE NULL END
                  INTO v_batch_id, v_unit_id, v_pending, v_target_version, v_changed_count
                FROM public.master_data_import_batches AS batch
                JOIN public.master_data_import_units AS unit
                  ON unit.school_id = batch.school_id
                 AND unit.batch_id = batch.id
                WHERE batch.school_id = p_school_id
                  AND batch.mode = 'canonical_atomic_changes'
                  AND batch.confirmation_token_sha256 = p_confirmation_token_sha256
                  AND batch.confirmation_session_sha256 = p_confirmation_session_sha256
                  AND batch.staged_by_user_id = p_account_user_id
                  AND batch.staged_by_person_id = p_person_id
                  AND batch.staged_by_assignment_id = p_assignment_id
                  AND batch.staged_assignment_version = p_assignment_version
                  AND batch.staged_authorization_version = p_authorization_version
                  AND batch.plan_sha256 = p_plan_sha256
                  AND batch.source_sha256 = p_source_sha256
                  AND batch.base_fact_version = p_base_version
                  AND batch.target_fact_version = batch.base_fact_version + 1
                   AND batch.confirmation_expires_at > v_as_of
                   AND batch.planned_units = 1
                  AND unit.sequence = 1
                  AND unit.unit_kind = 'school_fact'
                  AND unit.payload_sha256 = p_unit_payload_sha256
                  AND unit.payload ->> 'operation' = 'reactivate'
                  AND unit.payload ->> 'fact_kind' = 'school'
                  AND unit.payload ->> 'source_key' = p_fact_source_key
                  AND (unit.payload ->> 'expected_version')::bigint = p_expected_version
                  AND NOT EXISTS (
                      SELECT 1
                      FROM public.master_data_import_units AS other
                      WHERE other.school_id = batch.school_id
                        AND other.batch_id = batch.id
                        AND other.id <> unit.id
                  )
                  AND (
                      (
                          batch.status = 'ready_for_confirmation'
                          AND batch.confirmation_consumed_at IS NULL
                          AND batch.succeeded_units = 0
                          AND batch.failed_units = 0
                          AND batch.pending_units = 1
                          AND unit.status = 'ready'
                          AND unit.target_id IS NULL
                          AND unit.target_version IS NULL
                          AND NULLIF(unit.payload ->> 'effective_at', '') IS NOT NULL
                          AND NOT app_private.school_fact_identity_root_is_effective_at(
                              batch.school_id,
                              'school',
                              batch.school_id,
                              NULLIF(unit.payload ->> 'effective_at', '')::timestamptz
                          )
                          AND NOT EXISTS (
                              SELECT 1
                              FROM public.school_fact_change_lineage AS lineage
                              WHERE lineage.school_id = batch.school_id
                                AND lineage.batch_id = batch.id
                          )
                      )
                      OR (
                          batch.status = 'applied'
                          AND batch.confirmation_consumed_at IS NOT NULL
                          AND batch.succeeded_units = 1
                          AND batch.failed_units = 0
                          AND batch.pending_units = 0
                          AND unit.status = 'applied'
                          AND unit.target_id = p_school_id
                          AND unit.target_version = p_expected_version + 1
                          AND batch.result_payload ->> 'target_version' ~ '^[0-9]+$'
                          AND batch.result_payload ->> 'changed_count' ~ '^[0-9]+$'
                          AND EXISTS (
                              SELECT 1
                              FROM public.school_fact_change_lineage AS lineage
                              WHERE lineage.school_id = batch.school_id
                                AND lineage.batch_id = batch.id
                                AND lineage.unit_id = unit.id
                                AND lineage.fact_kind = 'school'
                                AND lineage.fact_source_key = p_fact_source_key
                                AND lineage.operation = 'reactivate'
                                AND lineage.target_id = p_school_id
                                AND lineage.target_version = p_expected_version + 1
                          )
                      )
                   );
                IF v_batch_id IS NULL OR v_unit_id IS NULL THEN
                    RAISE EXCEPTION 'school reactivation context rejected'
                        USING ERRCODE = '28000';
                END IF;

                IF NOT v_pending THEN
                    SELECT keys.key_version, keys.signing_key
                      INTO STRICT v_key_version, v_signing_key
                    FROM app_private.context_proof_keys AS keys
                    WHERE keys.is_current;
                    v_marker_canonical := array_to_string(ARRAY[
                        'school-reactivation-replay-v1',
                        current_database(),
                        pg_backend_pid()::text,
                        pg_current_xact_id()::text,
                        v_key_version::text,
                        v_batch_id::text,
                        v_target_version::text,
                        v_changed_count::text,
                        p_confirmation_token_sha256,
                        p_confirmation_session_sha256
                    ], chr(31));
                    v_marker_proof := encode(
                        app_private.hmac(
                            convert_to(v_marker_canonical, 'UTF8'),
                            v_signing_key,
                            'sha256'
                        ),
                        'hex'
                    );
                    PERFORM set_config(
                        'app.school_reactivation_replay_key_version',
                        v_key_version::text,
                        true
                    );
                    PERFORM set_config(
                        'app.school_reactivation_replay_batch_id', v_batch_id::text, true
                    );
                    PERFORM set_config(
                        'app.school_reactivation_replay_target_version',
                        v_target_version::text,
                        true
                    );
                    PERFORM set_config(
                        'app.school_reactivation_replay_changed_count',
                        v_changed_count::text,
                        true
                    );
                    PERFORM set_config(
                        'app.school_reactivation_replay_confirmation_token_sha256',
                        p_confirmation_token_sha256,
                        true
                    );
                    PERFORM set_config(
                        'app.school_reactivation_replay_session_sha256',
                        p_confirmation_session_sha256,
                        true
                    );
                    PERFORM set_config(
                        'app.school_reactivation_replay_binding_proof',
                        v_marker_proof,
                        true
                    );
                    RETURN;
                END IF;

                UPDATE public.schools AS school
                SET status = 'active'
                WHERE school.id = p_school_id
                  AND school.source_key = p_fact_source_key
                  AND school.status = 'inactive'
                  AND school.version = p_expected_version;
                IF NOT FOUND THEN
                    RAISE EXCEPTION 'school reactivation context rejected'
                        USING ERRCODE = '28000';
                END IF;

                -- The narrow reactivation binder already revalidates the active actor
                -- roots above.  The ordinary rev18 binder must continue to reject this
                -- school until the reactivation effective time, so use the preserved
                -- rev07 binder only after the transaction-local school prewrite.
                PERFORM app.bind_execution_context_rev07(
                    p_school_id,
                    p_account_user_id,
                    p_person_id,
                    p_context_kind,
                    p_assignment_id,
                    p_assignment_version,
                    p_guardian_relationship_id,
                    p_guardian_relationship_version,
                    p_authorization_version,
                    p_request_id,
                    p_context_purpose,
                    p_idempotency_key_hash,
                    p_session_nonce_hash,
                    p_proof_expires_at,
                    p_server_proof
                );

                SELECT keys.key_version, keys.signing_key
                      INTO STRICT v_key_version, v_signing_key
                    FROM app_private.context_proof_keys AS keys
                    WHERE keys.is_current;
                    v_marker_canonical := array_to_string(ARRAY[
                        'school-reactivation-bound-v1',
                        current_database(),
                        pg_backend_pid()::text,
                        pg_current_xact_id()::text,
                        v_key_version::text,
                        p_school_id::text,
                        v_batch_id::text,
                        v_unit_id::text,
                        p_fact_source_key,
                        p_expected_version::text,
                        p_confirmation_token_sha256,
                        p_confirmation_session_sha256
                    ], chr(31));
                    v_marker_proof := encode(
                        app_private.hmac(
                            convert_to(v_marker_canonical, 'UTF8'),
                            v_signing_key,
                            'sha256'
                        ),
                        'hex'
                    );
                    PERFORM set_config(
                        'app.school_reactivation_batch_id', v_batch_id::text, true
                    );
                    PERFORM set_config(
                        'app.school_reactivation_unit_id', v_unit_id::text, true
                    );
                    PERFORM set_config(
                        'app.school_reactivation_source_key', p_fact_source_key, true
                    );
                    PERFORM set_config(
                        'app.school_reactivation_expected_version',
                        p_expected_version::text,
                        true
                    );
                    PERFORM set_config(
                        'app.school_reactivation_confirmation_token_sha256',
                        p_confirmation_token_sha256,
                        true
                    );
                    PERFORM set_config(
                        'app.school_reactivation_session_sha256',
                        p_confirmation_session_sha256,
                        true
                    );
                    PERFORM set_config(
                        'app.school_reactivation_binding_proof', v_marker_proof, true
                    );
            EXCEPTION
                WHEN invalid_text_representation OR numeric_value_out_of_range THEN
                    RAISE EXCEPTION 'school reactivation context rejected'
                        USING ERRCODE = '28000';
            END
            $function$;
            """
        )
    )
    for signature in (
        list_signature,
        candidate_signature,
        bind_signature,
        require_signature,
        replay_signature,
    ):
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO campus_ai_runtime")
        op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO campus_ai_db_owner")
    op.execute("SET LOCAL ROLE campus_ai_db_owner")
    op.execute("REVOKE CREATE ON SCHEMA app FROM campus_ai_context_definer")

    op.execute(
        sa.text(
            r"""
            CREATE FUNCTION public.sf18_require_school_reactivation_lineage()
            RETURNS trigger
            LANGUAGE plpgsql
            SECURITY DEFINER
            SET search_path = pg_catalog
            AS $function$
            DECLARE
                v_context record;
            BEGIN
                IF OLD.status <> 'active' AND NEW.status = 'active' THEN
                    BEGIN
                        SELECT * INTO STRICT v_context
                        FROM app.require_school_reactivation_context();
                    EXCEPTION WHEN OTHERS THEN
                        RAISE EXCEPTION USING
                            ERRCODE = '23514',
                            CONSTRAINT = 'ck_schools_explicit_reactivation_lineage',
                            MESSAGE = 'school reactivation requires one formal lineage unit';
                    END;
                    IF v_context.school_id IS DISTINCT FROM NEW.id
                       OR v_context.fact_source_key IS DISTINCT FROM OLD.source_key
                       OR v_context.expected_version IS DISTINCT FROM OLD.version
                       OR NOT EXISTS (
                           SELECT 1
                           FROM public.schools AS school
                           JOIN public.master_data_import_batches AS batch
                             ON batch.school_id = school.id
                            AND batch.id = v_context.batch_id
                           JOIN public.master_data_import_units AS unit
                             ON unit.school_id = batch.school_id
                            AND unit.batch_id = batch.id
                            AND unit.id = v_context.unit_id
                           JOIN public.school_fact_change_lineage AS lineage
                             ON lineage.school_id = unit.school_id
                            AND lineage.batch_id = unit.batch_id
                            AND lineage.unit_id = unit.id
                           WHERE school.id = NEW.id
                             AND school.source_key = v_context.fact_source_key
                             AND school.status = 'active'
                             AND school.version = v_context.expected_version + 1
                             AND batch.status = 'applied'
                             AND batch.planned_units = 1
                             AND batch.succeeded_units = 1
                             AND batch.failed_units = 0
                             AND batch.pending_units = 0
                             AND batch.confirmation_consumed_at IS NOT NULL
                             AND unit.sequence = 1
                             AND unit.unit_kind = 'school_fact'
                             AND unit.status = 'applied'
                             AND unit.target_id = school.id
                             AND unit.target_version = v_context.expected_version + 1
                             AND unit.payload ->> 'operation' = 'reactivate'
                             AND unit.payload ->> 'fact_kind' = 'school'
                             AND unit.payload ->> 'source_key' = v_context.fact_source_key
                             AND (unit.payload ->> 'expected_version')::bigint
                                 = v_context.expected_version
                             AND lineage.fact_kind = 'school'
                             AND lineage.fact_source_key = v_context.fact_source_key
                             AND lineage.operation = 'reactivate'
                             AND lineage.target_id = school.id
                             AND lineage.target_version = v_context.expected_version + 1
                       ) THEN
                        RAISE EXCEPTION USING
                            ERRCODE = '23514',
                            CONSTRAINT = 'ck_schools_explicit_reactivation_lineage',
                            MESSAGE = 'school reactivation requires one formal lineage unit';
                    END IF;
                END IF;
                RETURN NULL;
            EXCEPTION
                WHEN invalid_text_representation OR numeric_value_out_of_range THEN
                    RAISE EXCEPTION USING
                        ERRCODE = '23514',
                        CONSTRAINT = 'ck_schools_explicit_reactivation_lineage',
                        MESSAGE = 'school reactivation requires one formal lineage unit';
            END
            $function$;

            CREATE CONSTRAINT TRIGGER trg_sf18_require_school_reactivation_lineage
            AFTER UPDATE ON public.schools
            DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW
            EXECUTE FUNCTION public.sf18_require_school_reactivation_lineage();
            """
        )
    )
    op.execute(
        "ALTER FUNCTION public.sf18_require_school_reactivation_lineage() "
        "OWNER TO campus_ai_db_owner"
    )
    op.execute(
        "REVOKE ALL ON FUNCTION public.sf18_require_school_reactivation_lineage() FROM PUBLIC"
    )


def _drop_school_reactivation_context() -> None:
    list_signature = "app.list_school_reactivation_candidates(integer,bigint,text,timestamptz,text)"
    candidate_signature = (
        "app.validate_school_reactivation_candidate(uuid,integer,uuid,text,uuid,bigint,uuid,"
        "bigint,bigint,uuid,text,text,text,timestamptz,text)"
    )
    bind_signature = (
        "app.bind_school_reactivation_context(uuid,integer,uuid,text,uuid,bigint,uuid,"
        "bigint,bigint,uuid,text,text,text,timestamptz,text,text,text,text,bigint,text,"
        "text,text,bigint)"
    )
    require_signature = "app.require_school_reactivation_context()"
    replay_signature = "app.school_reactivation_replay_result()"
    op.execute("DROP TRIGGER trg_sf18_require_school_reactivation_lineage ON public.schools")
    op.execute("DROP FUNCTION public.sf18_require_school_reactivation_lineage()")
    op.execute("SET LOCAL ROLE campus_ai_context_definer")
    for signature in (
        list_signature,
        candidate_signature,
        bind_signature,
        require_signature,
        replay_signature,
    ):
        op.execute(f"REVOKE EXECUTE ON FUNCTION {signature} FROM campus_ai_runtime")
        op.execute(f"REVOKE EXECUTE ON FUNCTION {signature} FROM campus_ai_db_owner")
        op.execute(f"DROP FUNCTION {signature}")
    op.execute("SET LOCAL ROLE campus_ai_db_owner")
    for table_name in (
        "school_fact_change_lineage",
        "master_data_import_units",
        "master_data_import_batches",
    ):
        op.execute(f"DROP POLICY school_reactivation_context_select ON public.{table_name}")
    op.execute(
        "REVOKE SELECT (school_id, batch_id, unit_id, fact_kind, fact_source_key, "
        "operation, target_id, target_version) "
        "ON TABLE public.school_fact_change_lineage FROM campus_ai_context_definer"
    )
    op.execute(
        "REVOKE SELECT (school_id, id, batch_id, sequence, unit_kind, status, payload, "
        "payload_sha256, "
        "target_id, target_version) ON TABLE public.master_data_import_units "
        "FROM campus_ai_context_definer"
    )
    op.execute(
        "REVOKE SELECT (school_id, id, mode, status, planned_units, succeeded_units, "
        "failed_units, pending_units, request_id, idempotency_key_hash, "
        "source_sha256, plan_sha256, base_fact_version, target_fact_version, "
        "confirmation_token_sha256, "
        "confirmation_expires_at, confirmation_consumed_at, confirmation_session_sha256, "
        "result_payload, "
        "staged_by_user_id, staged_by_person_id, staged_by_assignment_id, "
        "staged_assignment_version, staged_authorization_version) "
        "ON TABLE public.master_data_import_batches FROM campus_ai_context_definer"
    )
    op.execute("REVOKE UPDATE (status) ON TABLE public.schools FROM campus_ai_context_definer")
    op.execute(
        "REVOKE SELECT (source_key, version) ON TABLE public.schools FROM campus_ai_context_definer"
    )


def _create_root_reactivation_context() -> None:
    bind_signature = "app.bind_root_reactivation_context(text,text,text,text,bigint)"
    require_signature = "app.require_root_reactivation_context()"
    root_tables = (
        "school_people",
        "students",
        "organization_units",
        "campus_locations",
    )
    for table_name in root_tables:
        op.execute(
            "GRANT SELECT (id, school_id, source_key, status, version) "
            f"ON TABLE public.{table_name} TO campus_ai_context_definer"
        )

    op.execute("GRANT CREATE ON SCHEMA app TO campus_ai_context_definer")
    op.execute("SET LOCAL ROLE campus_ai_context_definer")
    op.execute(
        sa.text(
            r"""
            CREATE FUNCTION app.require_root_reactivation_context()
            RETURNS TABLE(
                school_id uuid,
                batch_id uuid,
                unit_id uuid,
                fact_kind text,
                fact_source_key text,
                expected_version bigint,
                target_id uuid
            )
            LANGUAGE plpgsql
            STABLE
            SECURITY DEFINER
            SET search_path = pg_catalog
            AS $function$
            DECLARE
                v_school_id uuid;
                v_key_version text := current_setting('app.context_key_version', true);
                v_batch_id text := current_setting('app.root_reactivation_batch_id', true);
                v_unit_id text := current_setting('app.root_reactivation_unit_id', true);
                v_fact_kind text := current_setting('app.root_reactivation_fact_kind', true);
                v_source_key text := current_setting('app.root_reactivation_source_key', true);
                v_expected_version text := current_setting(
                    'app.root_reactivation_expected_version', true
                );
                v_target_id text := current_setting('app.root_reactivation_target_id', true);
                v_token_sha256 text := current_setting(
                    'app.root_reactivation_confirmation_token_sha256', true
                );
                v_session_sha256 text := current_setting(
                    'app.root_reactivation_session_sha256', true
                );
                v_binding_proof text := current_setting(
                    'app.root_reactivation_binding_proof', true
                );
                v_signing_key bytea;
                v_canonical text;
                v_expected_proof text;
            BEGIN
                v_school_id := app.require_school_context();
                IF current_setting('app.context_purpose', true) IS DISTINCT FROM 'command'
                   OR v_key_version IS NULL OR v_key_version = ''
                   OR v_batch_id IS NULL OR v_batch_id = ''
                   OR v_unit_id IS NULL OR v_unit_id = ''
                   OR v_fact_kind NOT IN ('person', 'student', 'organization', 'location')
                   OR v_source_key IS NULL OR v_source_key = ''
                   OR v_expected_version IS NULL OR v_expected_version = ''
                   OR v_target_id IS NULL OR v_target_id = ''
                   OR v_token_sha256 !~ '^[0-9a-f]{64}$'
                   OR v_session_sha256 !~ '^[0-9a-f]{64}$'
                   OR v_binding_proof !~ '^[0-9a-f]{64}$' THEN
                    RAISE EXCEPTION 'root reactivation context is unavailable'
                        USING ERRCODE = '28000';
                END IF;

                SELECT keys.signing_key INTO v_signing_key
                FROM app_private.context_proof_keys AS keys
                WHERE keys.is_current AND keys.key_version::text = v_key_version;
                IF v_signing_key IS NULL THEN
                    RAISE EXCEPTION 'root reactivation context is unavailable'
                        USING ERRCODE = '28000';
                END IF;
                v_canonical := array_to_string(ARRAY[
                    'root-reactivation-bound-v1',
                    current_database(),
                    pg_backend_pid()::text,
                    pg_current_xact_id()::text,
                    v_key_version,
                    v_school_id::text,
                    v_batch_id,
                    v_unit_id,
                    v_fact_kind,
                    v_source_key,
                    v_expected_version,
                    v_target_id,
                    v_token_sha256,
                    v_session_sha256
                ], chr(31));
                v_expected_proof := encode(
                    app_private.hmac(
                        convert_to(v_canonical, 'UTF8'), v_signing_key, 'sha256'
                    ),
                    'hex'
                );
                IF v_expected_proof IS DISTINCT FROM v_binding_proof THEN
                    RAISE EXCEPTION 'root reactivation context is unavailable'
                        USING ERRCODE = '28000';
                END IF;

                RETURN QUERY SELECT
                    v_school_id,
                    v_batch_id::uuid,
                    v_unit_id::uuid,
                    v_fact_kind,
                    v_source_key,
                    v_expected_version::bigint,
                    v_target_id::uuid;
            EXCEPTION
                WHEN invalid_text_representation OR numeric_value_out_of_range THEN
                    RAISE EXCEPTION 'root reactivation context is unavailable'
                        USING ERRCODE = '28000';
            END
            $function$;

            CREATE FUNCTION app.bind_root_reactivation_context(
                p_confirmation_token_sha256 text,
                p_confirmation_session_sha256 text,
                p_fact_kind text,
                p_fact_source_key text,
                p_expected_version bigint
            )
            RETURNS void
            LANGUAGE plpgsql
            VOLATILE
            SECURITY DEFINER
            SET search_path = pg_catalog
            AS $function$
            DECLARE
                v_as_of timestamptz := statement_timestamp();
                v_school_id uuid;
                v_batch_id uuid;
                v_unit_id uuid;
                v_target_id uuid;
                v_pending boolean;
                v_signing_key bytea;
                v_key_version bigint;
                v_marker_canonical text;
                v_marker_proof text;
            BEGIN
                v_school_id := app.require_school_context();
                IF current_setting('app.context_purpose', true) IS DISTINCT FROM 'command'
                   OR current_setting('app.context_kind', true) IS DISTINCT FROM 'assignment'
                   OR p_confirmation_token_sha256 IS NULL
                   OR p_confirmation_token_sha256 !~ '^[0-9a-f]{64}$'
                   OR p_confirmation_session_sha256 IS NULL
                   OR p_confirmation_session_sha256 !~ '^[0-9a-f]{64}$'
                   OR p_fact_kind NOT IN ('person', 'student', 'organization', 'location')
                   OR p_fact_source_key IS NULL OR p_fact_source_key = ''
                   OR octet_length(convert_to(p_fact_source_key, 'UTF8')) > 255
                   OR p_expected_version IS NULL OR p_expected_version < 1 THEN
                    RAISE EXCEPTION 'root reactivation context rejected'
                        USING ERRCODE = '28000';
                END IF;

                PERFORM pg_advisory_xact_lock(
                    hashtextextended(p_confirmation_token_sha256, 0)
                );
                SELECT batch.id, unit.id, target.id,
                       batch.status = 'ready_for_confirmation'
                  INTO v_batch_id, v_unit_id, v_target_id, v_pending
                FROM public.master_data_import_batches AS batch
                JOIN public.master_data_import_units AS unit
                  ON unit.school_id = batch.school_id
                 AND unit.batch_id = batch.id
                JOIN LATERAL (
                    SELECT person.id, person.status, person.version
                    FROM public.school_people AS person
                    WHERE p_fact_kind = 'person'
                      AND person.school_id = batch.school_id
                      AND person.source_key = p_fact_source_key
                    UNION ALL
                    SELECT student.id, student.status, student.version
                    FROM public.students AS student
                    WHERE p_fact_kind = 'student'
                      AND student.school_id = batch.school_id
                      AND student.source_key = p_fact_source_key
                    UNION ALL
                    SELECT organization.id, organization.status, organization.version
                    FROM public.organization_units AS organization
                    WHERE p_fact_kind = 'organization'
                      AND organization.school_id = batch.school_id
                      AND organization.source_key = p_fact_source_key
                    UNION ALL
                    SELECT location.id, location.status, location.version
                    FROM public.campus_locations AS location
                    WHERE p_fact_kind = 'location'
                      AND location.school_id = batch.school_id
                      AND location.source_key = p_fact_source_key
                ) AS target ON true
                WHERE batch.school_id = v_school_id
                  AND batch.mode = 'canonical_atomic_changes'
                  AND batch.confirmation_token_sha256 = p_confirmation_token_sha256
                  AND batch.confirmation_session_sha256 = p_confirmation_session_sha256
                  AND batch.staged_by_user_id = current_setting('app.account_user_id')::integer
                  AND batch.staged_by_person_id = current_setting('app.person_id')::uuid
                  AND batch.staged_by_assignment_id = current_setting('app.assignment_id')::uuid
                  AND batch.staged_assignment_version
                      = current_setting('app.assignment_version')::bigint
                  AND batch.staged_authorization_version
                      = current_setting('app.authorization_version')::bigint
                  AND batch.target_fact_version = batch.base_fact_version + 1
                  AND batch.confirmation_expires_at > v_as_of
                  AND batch.planned_units = 1
                  AND unit.sequence = 1
                  AND unit.unit_kind = 'school_fact'
                  AND unit.payload ->> 'operation' = 'reactivate'
                  AND unit.payload ->> 'fact_kind' = p_fact_kind
                  AND unit.payload ->> 'source_key' = p_fact_source_key
                  AND (unit.payload ->> 'expected_version')::bigint = p_expected_version
                  AND NOT EXISTS (
                      SELECT 1
                      FROM public.master_data_import_units AS other
                      WHERE other.school_id = batch.school_id
                        AND other.batch_id = batch.id
                        AND other.id <> unit.id
                  )
                  AND (
                      (
                          batch.status = 'ready_for_confirmation'
                          AND batch.confirmation_consumed_at IS NULL
                          AND batch.succeeded_units = 0
                          AND batch.failed_units = 0
                          AND batch.pending_units = 1
                          AND unit.status = 'ready'
                          AND unit.target_id IS NULL
                          AND unit.target_version IS NULL
                          AND target.status = 'inactive'
                          AND target.version = p_expected_version
                          AND NULLIF(unit.payload ->> 'effective_at', '') IS NOT NULL
                          AND NOT app_private.school_fact_identity_root_is_effective_at(
                              batch.school_id,
                              p_fact_kind,
                              target.id,
                              NULLIF(unit.payload ->> 'effective_at', '')::timestamptz
                          )
                          AND NOT EXISTS (
                              SELECT 1
                              FROM public.school_fact_change_lineage AS lineage
                              WHERE lineage.school_id = batch.school_id
                                AND lineage.batch_id = batch.id
                          )
                      )
                      OR (
                          batch.status = 'applied'
                          AND batch.confirmation_consumed_at IS NOT NULL
                          AND batch.succeeded_units = 1
                          AND batch.failed_units = 0
                          AND batch.pending_units = 0
                          AND unit.status = 'applied'
                          AND unit.target_id = target.id
                          AND unit.target_version = p_expected_version + 1
                          AND EXISTS (
                              SELECT 1
                              FROM public.school_fact_change_lineage AS lineage
                              WHERE lineage.school_id = batch.school_id
                                AND lineage.batch_id = batch.id
                                AND lineage.unit_id = unit.id
                                AND lineage.fact_kind = p_fact_kind
                                AND lineage.fact_source_key = p_fact_source_key
                                AND lineage.operation = 'reactivate'
                                AND lineage.target_id = target.id
                                AND lineage.target_version = p_expected_version + 1
                          )
                      )
                  );
                IF v_batch_id IS NULL OR v_unit_id IS NULL OR v_target_id IS NULL THEN
                    RAISE EXCEPTION 'root reactivation context rejected'
                        USING ERRCODE = '28000';
                END IF;
                IF NOT v_pending THEN
                    RETURN;
                END IF;

                SELECT keys.key_version, keys.signing_key
                  INTO STRICT v_key_version, v_signing_key
                FROM app_private.context_proof_keys AS keys
                WHERE keys.is_current;
                v_marker_canonical := array_to_string(ARRAY[
                    'root-reactivation-bound-v1',
                    current_database(),
                    pg_backend_pid()::text,
                    pg_current_xact_id()::text,
                    v_key_version::text,
                    v_school_id::text,
                    v_batch_id::text,
                    v_unit_id::text,
                    p_fact_kind,
                    p_fact_source_key,
                    p_expected_version::text,
                    v_target_id::text,
                    p_confirmation_token_sha256,
                    p_confirmation_session_sha256
                ], chr(31));
                v_marker_proof := encode(
                    app_private.hmac(
                        convert_to(v_marker_canonical, 'UTF8'), v_signing_key, 'sha256'
                    ),
                    'hex'
                );
                PERFORM set_config('app.root_reactivation_batch_id', v_batch_id::text, true);
                PERFORM set_config('app.root_reactivation_unit_id', v_unit_id::text, true);
                PERFORM set_config('app.root_reactivation_fact_kind', p_fact_kind, true);
                PERFORM set_config(
                    'app.root_reactivation_source_key', p_fact_source_key, true
                );
                PERFORM set_config(
                    'app.root_reactivation_expected_version', p_expected_version::text, true
                );
                PERFORM set_config('app.root_reactivation_target_id', v_target_id::text, true);
                PERFORM set_config(
                    'app.root_reactivation_confirmation_token_sha256',
                    p_confirmation_token_sha256,
                    true
                );
                PERFORM set_config(
                    'app.root_reactivation_session_sha256',
                    p_confirmation_session_sha256,
                    true
                );
                PERFORM set_config(
                    'app.root_reactivation_binding_proof', v_marker_proof, true
                );
            EXCEPTION
                WHEN invalid_text_representation OR numeric_value_out_of_range THEN
                    RAISE EXCEPTION 'root reactivation context rejected'
                        USING ERRCODE = '28000';
            END
            $function$;
            """
        )
    )
    for signature in (bind_signature, require_signature):
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO campus_ai_runtime")
        op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO campus_ai_db_owner")
    op.execute("SET LOCAL ROLE campus_ai_db_owner")
    op.execute("REVOKE CREATE ON SCHEMA app FROM campus_ai_context_definer")

    op.execute(
        sa.text(
            r"""
            CREATE FUNCTION public.sf18_require_root_reactivation_lineage()
            RETURNS trigger
            LANGUAGE plpgsql
            SECURITY DEFINER
            SET search_path = pg_catalog
            AS $function$
            DECLARE
                v_context record;
                v_fact_kind text := CASE TG_TABLE_NAME
                    WHEN 'school_people' THEN 'person'
                    WHEN 'students' THEN 'student'
                    WHEN 'organization_units' THEN 'organization'
                    WHEN 'campus_locations' THEN 'location'
                    ELSE NULL
                END;
            BEGIN
                IF OLD.status <> 'active' AND NEW.status = 'active' THEN
                    BEGIN
                        SELECT * INTO STRICT v_context
                        FROM app.require_root_reactivation_context();
                    EXCEPTION WHEN OTHERS THEN
                        RAISE EXCEPTION USING
                            ERRCODE = '23514',
                            CONSTRAINT = 'ck_school_fact_root_explicit_reactivation_lineage',
                            MESSAGE = 'root reactivation requires one formal lineage unit';
                    END;
                    IF v_fact_kind IS NULL
                       OR v_context.school_id IS DISTINCT FROM NEW.school_id
                       OR v_context.target_id IS DISTINCT FROM NEW.id
                       OR v_context.fact_kind IS DISTINCT FROM v_fact_kind
                       OR v_context.fact_source_key IS DISTINCT FROM OLD.source_key
                       OR v_context.expected_version IS DISTINCT FROM OLD.version
                       OR NEW.version IS DISTINCT FROM OLD.version + 1
                       OR NOT EXISTS (
                           SELECT 1
                           FROM public.master_data_import_batches AS batch
                           JOIN public.master_data_import_units AS unit
                             ON unit.school_id = batch.school_id
                            AND unit.batch_id = batch.id
                            AND unit.id = v_context.unit_id
                           JOIN public.school_fact_change_lineage AS lineage
                             ON lineage.school_id = unit.school_id
                            AND lineage.batch_id = unit.batch_id
                            AND lineage.unit_id = unit.id
                           WHERE batch.school_id = NEW.school_id
                             AND batch.id = v_context.batch_id
                             AND batch.status = 'applied'
                             AND batch.planned_units = 1
                             AND batch.succeeded_units = 1
                             AND batch.failed_units = 0
                             AND batch.pending_units = 0
                             AND batch.confirmation_consumed_at IS NOT NULL
                             AND unit.sequence = 1
                             AND unit.unit_kind = 'school_fact'
                             AND unit.status = 'applied'
                             AND unit.target_id = NEW.id
                             AND unit.target_version = OLD.version + 1
                             AND unit.payload ->> 'operation' = 'reactivate'
                             AND unit.payload ->> 'fact_kind' = v_fact_kind
                             AND unit.payload ->> 'source_key' = OLD.source_key
                             AND (unit.payload ->> 'expected_version')::bigint = OLD.version
                             AND lineage.fact_kind = v_fact_kind
                             AND lineage.fact_source_key = OLD.source_key
                             AND lineage.operation = 'reactivate'
                             AND lineage.target_id = NEW.id
                             AND lineage.target_version = OLD.version + 1
                       ) THEN
                        RAISE EXCEPTION USING
                            ERRCODE = '23514',
                            CONSTRAINT = 'ck_school_fact_root_explicit_reactivation_lineage',
                            MESSAGE = 'root reactivation requires one formal lineage unit';
                    END IF;
                END IF;
                RETURN NULL;
            EXCEPTION
                WHEN invalid_text_representation OR numeric_value_out_of_range THEN
                    RAISE EXCEPTION USING
                        ERRCODE = '23514',
                        CONSTRAINT = 'ck_school_fact_root_explicit_reactivation_lineage',
                        MESSAGE = 'root reactivation requires one formal lineage unit';
            END
            $function$;
            """
        )
    )
    op.execute(
        "ALTER FUNCTION public.sf18_require_root_reactivation_lineage() OWNER TO campus_ai_db_owner"
    )
    op.execute("REVOKE ALL ON FUNCTION public.sf18_require_root_reactivation_lineage() FROM PUBLIC")
    for table_name in root_tables:
        op.execute(
            f"CREATE CONSTRAINT TRIGGER trg_sf18_require_{table_name}_reactivation_lineage "
            f"AFTER UPDATE ON public.{table_name} DEFERRABLE INITIALLY DEFERRED "
            "FOR EACH ROW EXECUTE FUNCTION public.sf18_require_root_reactivation_lineage()"
        )


def _drop_root_reactivation_context() -> None:
    bind_signature = "app.bind_root_reactivation_context(text,text,text,text,bigint)"
    require_signature = "app.require_root_reactivation_context()"
    root_tables = (
        "school_people",
        "students",
        "organization_units",
        "campus_locations",
    )
    for table_name in root_tables:
        op.execute(
            f"DROP TRIGGER trg_sf18_require_{table_name}_reactivation_lineage "
            f"ON public.{table_name}"
        )
    op.execute("DROP FUNCTION public.sf18_require_root_reactivation_lineage()")
    op.execute("SET LOCAL ROLE campus_ai_context_definer")
    for signature in (bind_signature, require_signature):
        op.execute(f"REVOKE EXECUTE ON FUNCTION {signature} FROM campus_ai_runtime")
        op.execute(f"REVOKE EXECUTE ON FUNCTION {signature} FROM campus_ai_db_owner")
        op.execute(f"DROP FUNCTION {signature}")
    op.execute("SET LOCAL ROLE campus_ai_db_owner")
    for table_name in root_tables:
        op.execute(
            "REVOKE SELECT (id, school_id, source_key, status, version) "
            f"ON TABLE public.{table_name} FROM campus_ai_context_definer"
        )


def _create_historical_content_terminal_reader() -> None:
    signature = "app.school_fact_content_version_terminal(uuid,text,uuid,bigint)"
    root_effective_signature = "app.school_fact_root_is_effective_at(uuid,text,uuid,timestamptz)"
    location_signature = "app.school_fact_location_history_is_resolvable(uuid,uuid,timestamptz)"
    location_interval_signature = (
        "app.school_fact_location_history_interval_is_resolvable(uuid,uuid,timestamptz,timestamptz)"
    )
    school_signature = "app.school_fact_school_snapshot_as_of(uuid,timestamptz)"
    op.execute(
        "GRANT SELECT (school_id, location_id, parent_location_id, kind, "
        "valid_from, valid_to, status, version) "
        "ON TABLE public.campus_location_versions TO campus_ai_context_definer"
    )
    op.execute(
        "GRANT SELECT (school_id, display_name, timezone_name, lifecycle_status, "
        "valid_from, valid_to, version) ON TABLE public.school_versions "
        "TO campus_ai_context_definer"
    )
    op.execute(
        "GRANT SELECT (school_id, person_id, lifecycle_status, valid_from, valid_to, "
        "version) ON TABLE public.school_person_profile_versions "
        "TO campus_ai_context_definer"
    )
    op.execute(
        "GRANT SELECT (school_id, organization_unit_id, valid_from, valid_to, status, "
        "version) ON TABLE public.organization_unit_versions "
        "TO campus_ai_context_definer"
    )
    op.execute(
        "GRANT SELECT (school_id, id, status, version) ON TABLE public.students "
        "TO campus_ai_context_definer"
    )
    op.execute(
        "GRANT SELECT (id, status, version) ON TABLE public.schools TO campus_ai_context_definer"
    )
    for table_name in ("school_people", "organization_units", "campus_locations"):
        op.execute(
            "GRANT SELECT (id, school_id, status, version) "
            f"ON TABLE public.{table_name} TO campus_ai_context_definer"
        )
    op.execute(
        "GRANT SELECT (school_id, id, payload) "
        "ON TABLE public.master_data_import_units TO campus_ai_context_definer"
    )
    op.execute(
        "GRANT SELECT (school_id, unit_id, fact_kind, target_id, target_version, operation) "
        "ON TABLE public.school_fact_change_lineage TO campus_ai_context_definer"
    )
    for table_name in ("master_data_import_units", "school_fact_change_lineage"):
        op.execute(
            f"CREATE POLICY historical_content_reader_select ON public.{table_name} "
            "FOR SELECT TO campus_ai_context_definer USING (true)"
        )
    op.execute("GRANT CREATE ON SCHEMA app, app_private TO campus_ai_context_definer")
    op.execute("SET LOCAL ROLE campus_ai_context_definer")
    op.execute(
        sa.text(
            r"""
            CREATE FUNCTION app.school_fact_content_version_terminal(
                p_school_id uuid,
                p_fact_kind text,
                p_target_id uuid,
                p_content_version bigint
            )
            RETURNS TABLE(operation text, effective_at timestamptz)
            LANGUAGE plpgsql
            STABLE
            SECURITY DEFINER
            SET search_path = pg_catalog
            AS $function$
            DECLARE
                v_context_school_id uuid;
                v_epoch_start bigint;
                v_epoch_end bigint;
                v_terminal_version bigint;
            BEGIN
                IF p_school_id IS NULL OR p_target_id IS NULL
                   OR p_content_version IS NULL OR p_content_version < 1
                   OR p_fact_kind IS NULL OR p_fact_kind NOT IN (
                       'school',
                       'person',
                       'organization',
                       'location',
                       'assignment',
                       'login_mapping',
                       'organization_campus_relationship'
                   ) THEN
                    RAISE EXCEPTION 'historical content terminal arguments rejected'
                        USING ERRCODE = '22023';
                END IF;
                IF session_user = 'campus_ai_runtime' THEN
                    v_context_school_id := app.require_school_context();
                    IF v_context_school_id IS DISTINCT FROM p_school_id THEN
                        RAISE EXCEPTION 'historical content terminal school rejected'
                            USING ERRCODE = '28000';
                    END IF;
                END IF;

                IF p_fact_kind IN ('school', 'person', 'organization', 'location') THEN
                    SELECT COALESCE(max(lineage.target_version), 0)
                      INTO v_epoch_start
                    FROM public.school_fact_change_lineage AS lineage
                    WHERE lineage.school_id = p_school_id
                      AND lineage.fact_kind = p_fact_kind
                      AND lineage.target_id = p_target_id
                      AND lineage.operation = 'reactivate'
                      AND lineage.target_version <= p_content_version;
                    SELECT min(lineage.target_version)
                      INTO v_epoch_end
                    FROM public.school_fact_change_lineage AS lineage
                    WHERE lineage.school_id = p_school_id
                      AND lineage.fact_kind = p_fact_kind
                      AND lineage.target_id = p_target_id
                      AND lineage.operation = 'reactivate'
                      AND lineage.target_version > v_epoch_start;

                    RETURN QUERY
                    SELECT lineage.operation::text,
                           NULLIF(unit.payload ->> 'effective_at', '')::timestamptz
                    FROM public.school_fact_change_lineage AS lineage
                    JOIN public.master_data_import_units AS unit
                      ON unit.school_id = lineage.school_id
                     AND unit.id = lineage.unit_id
                    WHERE lineage.school_id = p_school_id
                      AND lineage.fact_kind = p_fact_kind
                      AND lineage.target_id = p_target_id
                      AND lineage.target_version > v_epoch_start
                      AND (v_epoch_end IS NULL OR lineage.target_version < v_epoch_end)
                      AND lineage.operation IN ('end', 'revoke')
                    ORDER BY
                        CASE WHEN lineage.operation = 'revoke' THEN 0 ELSE 1 END,
                        NULLIF(unit.payload ->> 'effective_at', '')::timestamptz
                            ASC NULLS FIRST,
                        lineage.target_version
                    LIMIT 1;
                    RETURN;
                END IF;

                IF p_fact_kind IN ('assignment', 'login_mapping') THEN
                    RETURN QUERY
                    SELECT lineage.operation::text,
                           NULLIF(unit.payload ->> 'effective_at', '')::timestamptz
                    FROM public.school_fact_change_lineage AS lineage
                    JOIN public.master_data_import_units AS unit
                      ON unit.school_id = lineage.school_id
                     AND unit.id = lineage.unit_id
                    WHERE lineage.school_id = p_school_id
                      AND lineage.fact_kind = p_fact_kind
                      AND lineage.target_id = p_target_id
                      AND lineage.target_version > p_content_version
                      AND lineage.operation IN ('end', 'revoke')
                    ORDER BY lineage.target_version
                    LIMIT 1;
                    RETURN;
                END IF;

                v_terminal_version := p_content_version + 1;
                RETURN QUERY
                SELECT lineage.operation::text,
                       NULLIF(unit.payload ->> 'effective_at', '')::timestamptz
                FROM public.school_fact_change_lineage AS lineage
                JOIN public.master_data_import_units AS unit
                  ON unit.school_id = lineage.school_id
                 AND unit.id = lineage.unit_id
                WHERE lineage.school_id = p_school_id
                  AND lineage.fact_kind = p_fact_kind
                  AND lineage.target_id = p_target_id
                  AND lineage.target_version = v_terminal_version
                  AND lineage.operation IN ('end', 'revoke')
                LIMIT 1;
            END
            $function$;

            CREATE FUNCTION app_private.school_fact_relationship_slice_is_effective_at(
                p_school_id uuid,
                p_fact_kind text,
                p_target_id uuid,
                p_content_version bigint,
                p_as_of timestamptz
            )
            RETURNS boolean
            LANGUAGE sql
            STABLE
            SECURITY DEFINER
            SET search_path = pg_catalog
            AS $function$
                SELECT p_school_id IS NOT NULL
                   AND p_fact_kind IN ('assignment', 'login_mapping')
                   AND p_target_id IS NOT NULL
                   AND p_content_version IS NOT NULL
                   AND p_content_version >= 1
                   AND p_as_of IS NOT NULL
                   AND NOT EXISTS (
                       SELECT 1
                       FROM public.school_fact_change_lineage AS lineage
                       JOIN public.master_data_import_units AS unit
                         ON unit.school_id = lineage.school_id
                        AND unit.id = lineage.unit_id
                       WHERE lineage.school_id = p_school_id
                         AND lineage.fact_kind = p_fact_kind
                         AND lineage.target_id = p_target_id
                         AND lineage.target_version > p_content_version
                         AND lineage.operation IN ('end', 'revoke')
                         AND (
                             lineage.operation = 'revoke'
                             OR NULLIF(unit.payload ->> 'effective_at', '') IS NULL
                             OR NULLIF(
                                 unit.payload ->> 'effective_at', ''
                             )::timestamptz <= p_as_of
                         )
                   );
            $function$;

            CREATE FUNCTION app_private.school_fact_identity_root_is_effective_at(
                p_school_id uuid,
                p_fact_kind text,
                p_target_id uuid,
                p_as_of timestamptz
            )
            RETURNS boolean
            LANGUAGE sql
            STABLE
            SECURITY DEFINER
            SET search_path = pg_catalog
            AS $function$
                WITH content(version) AS (
                    SELECT version.version
                    FROM public.school_versions AS version
                    WHERE p_fact_kind = 'school'
                      AND version.school_id = p_school_id
                      AND version.school_id = p_target_id
                      AND version.lifecycle_status = 'active'
                      AND version.valid_from <= p_as_of
                      AND (version.valid_to IS NULL OR version.valid_to > p_as_of)
                    UNION ALL
                    SELECT CASE
                               WHEN terminal.target_version IS NULL THEN stable.version
                               ELSE terminal.target_version - 1
                           END
                    FROM public.schools AS stable
                    LEFT JOIN LATERAL (
                        SELECT lineage.target_version
                        FROM public.school_fact_change_lineage AS lineage
                        WHERE lineage.school_id = stable.id
                          AND lineage.fact_kind = 'school'
                          AND lineage.target_id = stable.id
                          AND lineage.operation IN ('end', 'revoke')
                        ORDER BY lineage.target_version DESC
                        LIMIT 1
                    ) AS terminal ON true
                    WHERE p_fact_kind = 'school'
                      AND stable.id = p_school_id
                      AND stable.id = p_target_id
                      AND (stable.status = 'active' OR terminal.target_version IS NOT NULL)
                      AND NOT EXISTS (
                          SELECT 1 FROM public.school_versions AS version
                          WHERE version.school_id = stable.id
                      )
                    UNION ALL
                    SELECT version.version
                    FROM public.school_person_profile_versions AS version
                    WHERE p_fact_kind = 'person'
                      AND version.school_id = p_school_id
                      AND version.person_id = p_target_id
                      AND version.lifecycle_status = 'active'
                      AND version.valid_from <= p_as_of
                      AND (version.valid_to IS NULL OR version.valid_to > p_as_of)
                    UNION ALL
                    SELECT CASE
                               WHEN terminal.target_version IS NULL THEN stable.version
                               ELSE terminal.target_version - 1
                           END
                    FROM public.school_people AS stable
                    LEFT JOIN LATERAL (
                        SELECT lineage.target_version
                        FROM public.school_fact_change_lineage AS lineage
                        WHERE lineage.school_id = stable.school_id
                          AND lineage.fact_kind = 'person'
                          AND lineage.target_id = stable.id
                          AND lineage.operation IN ('end', 'revoke')
                        ORDER BY lineage.target_version DESC
                        LIMIT 1
                    ) AS terminal ON true
                    WHERE p_fact_kind = 'person'
                      AND stable.school_id = p_school_id
                      AND stable.id = p_target_id
                      AND (stable.status = 'active' OR terminal.target_version IS NOT NULL)
                      AND NOT EXISTS (
                          SELECT 1
                          FROM public.school_person_profile_versions AS version
                          WHERE version.school_id = stable.school_id
                            AND version.person_id = stable.id
                      )
                    UNION ALL
                    SELECT version.version
                    FROM public.organization_unit_versions AS version
                    WHERE p_fact_kind = 'organization'
                      AND version.school_id = p_school_id
                      AND version.organization_unit_id = p_target_id
                      AND version.status = 'confirmed'
                      AND version.valid_from <= p_as_of
                      AND (version.valid_to IS NULL OR version.valid_to > p_as_of)
                    UNION ALL
                    SELECT CASE
                               WHEN terminal.target_version IS NULL THEN stable.version
                               ELSE terminal.target_version - 1
                           END
                    FROM public.organization_units AS stable
                    LEFT JOIN LATERAL (
                        SELECT lineage.target_version
                        FROM public.school_fact_change_lineage AS lineage
                        WHERE lineage.school_id = stable.school_id
                          AND lineage.fact_kind = 'organization'
                          AND lineage.target_id = stable.id
                          AND lineage.operation IN ('end', 'revoke')
                        ORDER BY lineage.target_version DESC
                        LIMIT 1
                    ) AS terminal ON true
                    WHERE p_fact_kind = 'organization'
                      AND stable.school_id = p_school_id
                      AND stable.id = p_target_id
                      AND (stable.status = 'active' OR terminal.target_version IS NOT NULL)
                      AND NOT EXISTS (
                          SELECT 1
                          FROM public.organization_unit_versions AS version
                          WHERE version.school_id = stable.school_id
                            AND version.organization_unit_id = stable.id
                      )
                    UNION ALL
                    SELECT selected.target_version
                    FROM LATERAL (
                        SELECT lineage.target_version
                        FROM public.school_fact_change_lineage AS lineage
                        JOIN public.master_data_import_units AS unit
                          ON unit.school_id = lineage.school_id
                         AND unit.id = lineage.unit_id
                        WHERE p_fact_kind = 'student'
                          AND lineage.school_id = p_school_id
                          AND lineage.fact_kind = 'student'
                          AND lineage.target_id = p_target_id
                          AND lineage.operation IN ('upsert', 'reactivate')
                          AND NULLIF(unit.payload ->> 'effective_at', '') IS NOT NULL
                          AND NULLIF(unit.payload ->> 'effective_at', '')::timestamptz
                              <= p_as_of
                        ORDER BY lineage.target_version DESC
                        LIMIT 1
                    ) AS selected
                    UNION ALL
                    SELECT CASE
                               WHEN first_change.target_version IS NULL THEN student.version
                               ELSE first_change.target_version - 1
                           END
                    FROM public.students AS student
                    LEFT JOIN LATERAL (
                        SELECT lineage.target_version, lineage.operation
                        FROM public.school_fact_change_lineage AS lineage
                        WHERE lineage.school_id = student.school_id
                          AND lineage.fact_kind = 'student'
                          AND lineage.target_id = student.id
                        ORDER BY lineage.target_version
                        LIMIT 1
                    ) AS first_change ON true
                    LEFT JOIN LATERAL (
                        SELECT lineage.target_version
                        FROM public.school_fact_change_lineage AS lineage
                        WHERE lineage.school_id = student.school_id
                          AND lineage.fact_kind = 'student'
                          AND lineage.target_id = student.id
                          AND lineage.operation IN ('end', 'revoke')
                        ORDER BY lineage.target_version DESC
                        LIMIT 1
                    ) AS terminal ON true
                    WHERE p_fact_kind = 'student'
                      AND student.school_id = p_school_id
                      AND student.id = p_target_id
                      AND (student.status = 'active' OR terminal.target_version IS NOT NULL)
                      AND (
                          first_change.operation IS NULL
                          OR first_change.operation IN ('end', 'revoke')
                          OR (
                              first_change.operation = 'upsert'
                              AND first_change.target_version > 1
                          )
                      )
                      AND NOT EXISTS (
                          SELECT 1
                          FROM public.school_fact_change_lineage AS lineage
                          JOIN public.master_data_import_units AS unit
                            ON unit.school_id = lineage.school_id
                           AND unit.id = lineage.unit_id
                          WHERE lineage.school_id = student.school_id
                            AND lineage.fact_kind = 'student'
                            AND lineage.target_id = student.id
                            AND lineage.operation IN ('upsert', 'reactivate')
                            AND NULLIF(unit.payload ->> 'effective_at', '') IS NOT NULL
                            AND NULLIF(unit.payload ->> 'effective_at', '')::timestamptz
                                <= p_as_of
                      )
                ), epoch_content AS (
                    SELECT content.version,
                           COALESCE((
                               SELECT max(reactivation.target_version)
                               FROM public.school_fact_change_lineage AS reactivation
                               WHERE reactivation.school_id = p_school_id
                                 AND reactivation.fact_kind = p_fact_kind
                                 AND reactivation.target_id = p_target_id
                                 AND reactivation.operation = 'reactivate'
                                 AND reactivation.target_version <= content.version
                           ), 0) AS epoch_start
                    FROM content
                ), bounded_content AS (
                    SELECT epoch_content.version,
                           epoch_content.epoch_start,
                           (
                               SELECT min(reactivation.target_version)
                               FROM public.school_fact_change_lineage AS reactivation
                               WHERE reactivation.school_id = p_school_id
                                 AND reactivation.fact_kind = p_fact_kind
                                 AND reactivation.target_id = p_target_id
                                 AND reactivation.operation = 'reactivate'
                                 AND reactivation.target_version > epoch_content.epoch_start
                           ) AS epoch_end
                    FROM epoch_content
                )
                SELECT COALESCE(
                    EXISTS (
                        SELECT 1
                        FROM bounded_content AS content
                        WHERE (
                            p_fact_kind <> 'student'
                            AND NOT EXISTS (
                                SELECT 1
                                FROM public.school_fact_change_lineage AS terminal_lineage
                                JOIN public.master_data_import_units AS terminal_unit
                                  ON terminal_unit.school_id = terminal_lineage.school_id
                                 AND terminal_unit.id = terminal_lineage.unit_id
                                WHERE terminal_lineage.school_id = p_school_id
                                  AND terminal_lineage.fact_kind = p_fact_kind
                                  AND terminal_lineage.target_id = p_target_id
                                  AND terminal_lineage.target_version > content.epoch_start
                                  AND (
                                      content.epoch_end IS NULL
                                      OR terminal_lineage.target_version < content.epoch_end
                                  )
                                  AND terminal_lineage.operation IN ('end', 'revoke')
                                  AND (
                                      terminal_lineage.operation = 'revoke'
                                      OR NULLIF(
                                          terminal_unit.payload ->> 'effective_at', ''
                                      ) IS NULL
                                      OR NULLIF(
                                          terminal_unit.payload ->> 'effective_at', ''
                                      )::timestamptz <= p_as_of
                                  )
                            )
                        ) OR (
                            p_fact_kind = 'student'
                            AND NOT EXISTS (
                            SELECT 1
                            FROM public.school_fact_change_lineage AS terminal_lineage
                            JOIN public.master_data_import_units AS terminal_unit
                              ON terminal_unit.school_id = terminal_lineage.school_id
                             AND terminal_unit.id = terminal_lineage.unit_id
                            WHERE terminal_lineage.school_id = p_school_id
                              AND terminal_lineage.fact_kind = p_fact_kind
                              AND terminal_lineage.target_id = p_target_id
                              AND terminal_lineage.target_version > content.epoch_start
                              AND (
                                  content.epoch_end IS NULL
                                  OR terminal_lineage.target_version < content.epoch_end
                              )
                              AND terminal_lineage.operation IN ('end', 'revoke')
                              AND (
                                  terminal_lineage.operation = 'revoke'
                                  OR NULLIF(
                                      terminal_unit.payload ->> 'effective_at', ''
                                  ) IS NULL
                                  OR NULLIF(
                                      terminal_unit.payload ->> 'effective_at', ''
                                  )::timestamptz <= p_as_of
                              )
                            )
                        )
                    ),
                    false
                )
            $function$;

            CREATE FUNCTION app.school_fact_root_is_effective_at(
                p_school_id uuid,
                p_fact_kind text,
                p_target_id uuid,
                p_as_of timestamptz
            )
            RETURNS boolean
            LANGUAGE plpgsql
            STABLE
            SECURITY DEFINER
            SET search_path = pg_catalog
            AS $function$
            DECLARE
                v_context_school_id uuid;
                v_effective boolean;
            BEGIN
                IF p_school_id IS NULL OR p_target_id IS NULL OR p_as_of IS NULL
                   OR p_fact_kind IS NULL OR p_fact_kind NOT IN (
                       'school', 'person', 'student', 'organization', 'location'
                   ) THEN
                    RAISE EXCEPTION 'root effectiveness arguments rejected'
                        USING ERRCODE = '22023';
                END IF;
                IF session_user = 'campus_ai_runtime' THEN
                    v_context_school_id := app.require_school_context();
                    IF v_context_school_id IS DISTINCT FROM p_school_id THEN
                        RAISE EXCEPTION 'root effectiveness school rejected'
                            USING ERRCODE = '28000';
                    END IF;
                END IF;

                IF p_fact_kind = 'location' THEN
                    SELECT app.school_fact_location_history_is_resolvable(
                        p_school_id, p_target_id, p_as_of
                    ) INTO v_effective;
                ELSE
                    SELECT app_private.school_fact_identity_root_is_effective_at(
                        p_school_id, p_fact_kind, p_target_id, p_as_of
                    ) INTO v_effective;
                END IF;
                RETURN COALESCE(v_effective, false);
            END
            $function$;

            CREATE FUNCTION app.school_fact_location_history_is_resolvable(
                p_school_id uuid,
                p_target_id uuid,
                p_as_of timestamptz
            )
            RETURNS boolean
            LANGUAGE plpgsql
            STABLE
            SECURITY DEFINER
            SET search_path = pg_catalog
            AS $function$
            DECLARE
                v_context_school_id uuid;
                v_resolvable boolean;
            BEGIN
                IF p_school_id IS NULL OR p_target_id IS NULL OR p_as_of IS NULL THEN
                    RAISE EXCEPTION 'historical location arguments rejected'
                        USING ERRCODE = '22023';
                END IF;
                IF session_user = 'campus_ai_runtime' THEN
                    v_context_school_id := app.require_school_context();
                    IF v_context_school_id IS DISTINCT FROM p_school_id THEN
                        RAISE EXCEPTION 'historical location school rejected'
                            USING ERRCODE = '28000';
                    END IF;
                END IF;

                WITH RECURSIVE ancestry(
                    id, parent_location_id, kind, version, path
                ) AS (
                    SELECT content.location_id,
                           content.parent_location_id,
                           content.kind,
                           content.version,
                           ARRAY[content.location_id]::uuid[]
                    FROM public.campus_location_versions AS content
                    WHERE content.school_id = p_school_id
                      AND content.location_id = p_target_id
                      AND content.status = 'confirmed'
                      AND content.valid_from <= p_as_of
                      AND (content.valid_to IS NULL OR content.valid_to > p_as_of)
                      AND NOT EXISTS (
                          SELECT 1
                          FROM app.school_fact_content_version_terminal(
                              p_school_id, 'location', content.location_id, content.version
                          ) AS terminal
                          WHERE terminal.operation IN ('end', 'revoke')
                            AND (
                                terminal.operation = 'revoke'
                                OR terminal.effective_at IS NULL
                                OR terminal.effective_at <= p_as_of
                            )
                      )
                    UNION ALL
                    SELECT parent.location_id,
                           parent.parent_location_id,
                           parent.kind,
                           parent.version,
                           child.path || parent.location_id
                    FROM ancestry AS child
                    JOIN public.campus_location_versions AS parent
                      ON parent.school_id = p_school_id
                     AND parent.location_id = child.parent_location_id
                     AND parent.status = 'confirmed'
                     AND parent.valid_from <= p_as_of
                     AND (parent.valid_to IS NULL OR parent.valid_to > p_as_of)
                    WHERE NOT parent.location_id = ANY(child.path)
                      AND NOT EXISTS (
                          SELECT 1
                          FROM app.school_fact_content_version_terminal(
                              p_school_id, 'location', parent.location_id, parent.version
                          ) AS terminal
                          WHERE terminal.operation IN ('end', 'revoke')
                            AND (
                                terminal.operation = 'revoke'
                                OR terminal.effective_at IS NULL
                                OR terminal.effective_at <= p_as_of
                            )
                      )
                )
                SELECT EXISTS (
                    SELECT 1
                    FROM ancestry
                    WHERE parent_location_id IS NULL AND kind = 'campus'
                )
                INTO v_resolvable;
                RETURN COALESCE(v_resolvable, false);
            END
            $function$;

            CREATE FUNCTION app.school_fact_location_history_interval_is_resolvable(
                p_school_id uuid,
                p_target_id uuid,
                p_valid_from timestamptz,
                p_valid_to timestamptz
            )
            RETURNS boolean
            LANGUAGE plpgsql
            STABLE
            SECURITY DEFINER
            SET search_path = pg_catalog
            AS $function$
            DECLARE
                v_context_school_id uuid;
                v_resolvable boolean;
                v_requested tstzrange;
            BEGIN
                IF p_school_id IS NULL OR p_target_id IS NULL OR p_valid_from IS NULL
                   OR (p_valid_to IS NOT NULL AND p_valid_to <= p_valid_from) THEN
                    RAISE EXCEPTION 'historical location interval arguments rejected'
                        USING ERRCODE = '22023';
                END IF;
                IF session_user = 'campus_ai_runtime' THEN
                    v_context_school_id := app.require_school_context();
                    IF v_context_school_id IS DISTINCT FROM p_school_id THEN
                        RAISE EXCEPTION 'historical location interval school rejected'
                            USING ERRCODE = '28000';
                    END IF;
                END IF;

                v_requested := tstzrange(
                    p_valid_from,
                    COALESCE(p_valid_to, 'infinity'::timestamptz),
                    '[)'
                );
                WITH RECURSIVE ancestry(
                    id, parent_location_id, kind, version, covered, path
                ) AS (
                    SELECT content.location_id,
                           content.parent_location_id,
                           content.kind,
                           content.version,
                            tstzrange(
                                content.valid_from,
                                GREATEST(
                                    content.valid_from,
                                    LEAST(
                                        COALESCE(content.valid_to, 'infinity'::timestamptz),
                                        COALESCE(
                                            CASE WHEN terminal.operation = 'end'
                                                 THEN terminal.effective_at END,
                                            'infinity'::timestamptz
                                        )
                                    )
                                ),
                                '[)'
                           ) * v_requested,
                           ARRAY[content.location_id]::uuid[]
                    FROM public.campus_location_versions AS content
                    JOIN public.campus_locations AS stable
                      ON stable.school_id = content.school_id
                     AND stable.id = content.location_id
                    LEFT JOIN LATERAL (
                        SELECT terminal.operation, terminal.effective_at
                        FROM app.school_fact_content_version_terminal(
                            p_school_id, 'location', content.location_id, content.version
                        ) AS terminal
                    ) AS terminal ON true
                    WHERE content.school_id = p_school_id
                      AND content.location_id = p_target_id
                      AND content.status = 'confirmed'
                      AND tstzrange(
                              content.valid_from,
                              COALESCE(content.valid_to, 'infinity'::timestamptz),
                              '[)'
                          ) && v_requested
                      AND terminal.operation IS DISTINCT FROM 'revoke'
                      AND NOT (
                          terminal.operation IS NOT DISTINCT FROM 'end'
                          AND terminal.effective_at IS NULL
                      )
                      AND NOT (
                          stable.status <> 'active'
                          AND stable.version = content.version + 1
                          AND terminal.operation IS NULL
                      )
                      AND NOT isempty(
                            tstzrange(
                                content.valid_from,
                                GREATEST(
                                    content.valid_from,
                                    LEAST(
                                        COALESCE(content.valid_to, 'infinity'::timestamptz),
                                        COALESCE(
                                            CASE WHEN terminal.operation = 'end'
                                                 THEN terminal.effective_at END,
                                            'infinity'::timestamptz
                                        )
                                    )
                                ),
                                '[)'
                          ) * v_requested
                      )
                    UNION ALL
                    SELECT parent.location_id,
                           parent.parent_location_id,
                           parent.kind,
                           parent.version,
                            tstzrange(
                                parent.valid_from,
                                GREATEST(
                                    parent.valid_from,
                                    LEAST(
                                        COALESCE(parent.valid_to, 'infinity'::timestamptz),
                                        COALESCE(
                                            CASE WHEN terminal.operation = 'end'
                                                 THEN terminal.effective_at END,
                                            'infinity'::timestamptz
                                        )
                                    )
                                ),
                                '[)'
                           ) * child.covered,
                           child.path || parent.location_id
                    FROM ancestry AS child
                    JOIN public.campus_location_versions AS parent
                      ON parent.school_id = p_school_id
                     AND parent.location_id = child.parent_location_id
                    JOIN public.campus_locations AS stable_parent
                      ON stable_parent.school_id = parent.school_id
                     AND stable_parent.id = parent.location_id
                    LEFT JOIN LATERAL (
                        SELECT terminal.operation, terminal.effective_at
                        FROM app.school_fact_content_version_terminal(
                            p_school_id, 'location', parent.location_id, parent.version
                        ) AS terminal
                    ) AS terminal ON true
                    WHERE parent.status = 'confirmed'
                      AND tstzrange(
                              parent.valid_from,
                              COALESCE(parent.valid_to, 'infinity'::timestamptz),
                              '[)'
                          ) && child.covered
                      AND terminal.operation IS DISTINCT FROM 'revoke'
                      AND NOT (
                          terminal.operation IS NOT DISTINCT FROM 'end'
                          AND terminal.effective_at IS NULL
                      )
                      AND NOT (
                          stable_parent.status <> 'active'
                          AND stable_parent.version = parent.version + 1
                          AND terminal.operation IS NULL
                      )
                      AND NOT parent.location_id = ANY(child.path)
                      AND NOT isempty(
                            tstzrange(
                                parent.valid_from,
                                GREATEST(
                                    parent.valid_from,
                                    LEAST(
                                        COALESCE(parent.valid_to, 'infinity'::timestamptz),
                                        COALESCE(
                                            CASE WHEN terminal.operation = 'end'
                                                 THEN terminal.effective_at END,
                                            'infinity'::timestamptz
                                        )
                                    )
                                ),
                                '[)'
                          ) * child.covered
                      )
                )
                SELECT COALESCE(
                    range_agg(covered) FILTER (
                        WHERE parent_location_id IS NULL AND kind = 'campus'
                    ) @> v_requested,
                    false
                )
                INTO v_resolvable
                FROM ancestry;
                RETURN v_resolvable;
            END
            $function$;

            CREATE FUNCTION app.school_fact_school_snapshot_as_of(
                p_school_id uuid,
                p_as_of timestamptz
            )
            RETURNS TABLE(
                school_id uuid,
                display_name text,
                timezone_name text,
                source_version bigint,
                valid_from timestamptz,
                valid_to timestamptz,
                captured_at timestamptz
            )
            LANGUAGE plpgsql
            STABLE
            SECURITY DEFINER
            SET search_path = pg_catalog
            AS $function$
            DECLARE
                v_context_school_id uuid;
            BEGIN
                IF p_school_id IS NULL OR p_as_of IS NULL THEN
                    RAISE EXCEPTION 'historical school arguments rejected'
                        USING ERRCODE = '22023';
                END IF;
                IF session_user = 'campus_ai_runtime' THEN
                    v_context_school_id := app.require_school_context();
                    IF v_context_school_id IS DISTINCT FROM p_school_id THEN
                        RAISE EXCEPTION 'historical school context rejected'
                            USING ERRCODE = '28000';
                    END IF;
                END IF;
                RETURN QUERY
                SELECT version.school_id,
                       version.display_name::text,
                       version.timezone_name::text,
                       version.version,
                       version.valid_from,
                       version.valid_to,
                       statement_timestamp()
                FROM public.school_versions AS version
                WHERE version.school_id = p_school_id
                  AND version.lifecycle_status = 'active'
                  AND version.valid_from <= p_as_of
                  AND (version.valid_to IS NULL OR version.valid_to > p_as_of)
                  AND NOT EXISTS (
                      SELECT 1
                      FROM app.school_fact_content_version_terminal(
                          version.school_id, 'school', version.school_id, version.version
                      ) AS terminal
                      WHERE terminal.operation IN ('end', 'revoke')
                        AND (
                            terminal.operation = 'revoke'
                            OR terminal.effective_at IS NULL
                            OR terminal.effective_at <= p_as_of
                        )
                  );
            END
            $function$;
            """
        )
    )
    for function_signature in (
        signature,
        root_effective_signature,
        location_signature,
        location_interval_signature,
        school_signature,
    ):
        op.execute(f"REVOKE ALL ON FUNCTION {function_signature} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION {function_signature} TO campus_ai_runtime")
        op.execute(f"GRANT EXECUTE ON FUNCTION {function_signature} TO campus_ai_db_owner")
    op.execute(
        "REVOKE ALL ON FUNCTION "
        "app_private.school_fact_identity_root_is_effective_at("
        "uuid,text,uuid,timestamptz) FROM PUBLIC"
    )
    op.execute(
        "REVOKE ALL ON FUNCTION "
        "app_private.school_fact_relationship_slice_is_effective_at("
        "uuid,text,uuid,bigint,timestamptz) FROM PUBLIC"
    )
    op.execute("SET LOCAL ROLE campus_ai_db_owner")
    op.execute("REVOKE CREATE ON SCHEMA app, app_private FROM campus_ai_context_definer")


def _drop_historical_content_terminal_reader() -> None:
    signature = "app.school_fact_content_version_terminal(uuid,text,uuid,bigint)"
    root_effective_signature = "app.school_fact_root_is_effective_at(uuid,text,uuid,timestamptz)"
    location_signature = "app.school_fact_location_history_is_resolvable(uuid,uuid,timestamptz)"
    location_interval_signature = (
        "app.school_fact_location_history_interval_is_resolvable(uuid,uuid,timestamptz,timestamptz)"
    )
    school_signature = "app.school_fact_school_snapshot_as_of(uuid,timestamptz)"
    identity_root_signature = (
        "app_private.school_fact_identity_root_is_effective_at(uuid,text,uuid,timestamptz)"
    )
    relationship_slice_signature = (
        "app_private.school_fact_relationship_slice_is_effective_at("
        "uuid,text,uuid,bigint,timestamptz)"
    )
    op.execute("GRANT CREATE ON SCHEMA app, app_private TO campus_ai_context_definer")
    op.execute("SET LOCAL ROLE campus_ai_context_definer")
    for function_signature in (
        school_signature,
        location_interval_signature,
        location_signature,
        root_effective_signature,
        signature,
        identity_root_signature,
        relationship_slice_signature,
    ):
        op.execute(
            f"REVOKE EXECUTE ON FUNCTION {function_signature} "
            "FROM campus_ai_runtime, campus_ai_db_owner"
        )
        op.execute(f"DROP FUNCTION {function_signature}")
    op.execute("SET LOCAL ROLE campus_ai_db_owner")
    op.execute("REVOKE CREATE ON SCHEMA app, app_private FROM campus_ai_context_definer")
    for table_name in ("school_fact_change_lineage", "master_data_import_units"):
        op.execute(f"DROP POLICY historical_content_reader_select ON public.{table_name}")
    op.execute(
        "REVOKE SELECT (school_id, unit_id, fact_kind, target_id, target_version, operation) "
        "ON TABLE public.school_fact_change_lineage FROM campus_ai_context_definer"
    )
    op.execute(
        "REVOKE SELECT (school_id, id, payload) "
        "ON TABLE public.master_data_import_units FROM campus_ai_context_definer"
    )
    op.execute(
        "REVOKE SELECT (school_id, location_id, parent_location_id, kind, "
        "valid_from, valid_to, status, version) "
        "ON TABLE public.campus_location_versions FROM campus_ai_context_definer"
    )
    op.execute(
        "REVOKE SELECT (school_id, display_name, timezone_name, lifecycle_status, "
        "valid_from, valid_to, version) ON TABLE public.school_versions "
        "FROM campus_ai_context_definer"
    )
    op.execute(
        "REVOKE SELECT (school_id, person_id, lifecycle_status, valid_from, valid_to, "
        "version) ON TABLE public.school_person_profile_versions "
        "FROM campus_ai_context_definer"
    )
    op.execute(
        "REVOKE SELECT (version) ON TABLE public.organization_unit_versions "
        "FROM campus_ai_context_definer"
    )
    op.execute(
        "REVOKE SELECT (school_id, id, status, version) ON TABLE public.students "
        "FROM campus_ai_context_definer"
    )
    op.execute("REVOKE SELECT (version) ON TABLE public.schools FROM campus_ai_context_definer")
    for table_name in ("school_people", "organization_units", "campus_locations"):
        op.execute(
            "REVOKE SELECT (id, school_id, status, version) "
            f"ON TABLE public.{table_name} FROM campus_ai_context_definer"
        )


def _create_temporal_takeover_assignment_reader() -> None:
    """Make takeover revalidation use the authoritative assignment content slice."""

    helper_signature = "app_private.master_data_import_effective_assignment(uuid,uuid,timestamptz)"
    lock_source_signature = "app.lock_master_data_import_takeover_source(uuid,uuid,uuid)"
    lock_assignment_signature = (
        "app.lock_master_data_import_takeover_assignment(uuid,uuid,uuid,uuid)"
    )
    supersede_signature = (
        "app.supersede_master_data_import_takeover_source(uuid,uuid,uuid,text,bigint)"
    )
    attempt_signature = "app_private.enforce_master_data_import_attempt_transition()"

    op.execute("GRANT CREATE ON SCHEMA app_private TO campus_ai_context_definer")
    op.execute("SET LOCAL ROLE campus_ai_context_definer")
    op.execute(
        sa.text(
            r"""
            CREATE FUNCTION app_private.master_data_import_effective_assignment(
                p_school_id uuid,
                p_assignment_id uuid,
                p_as_of timestamptz
            )
            RETURNS TABLE (
                assignment_id uuid,
                person_id uuid,
                version bigint,
                status text,
                valid_from timestamptz,
                valid_to timestamptz,
                semantic_role_code text
            )
            LANGUAGE sql
            STABLE
            SECURITY DEFINER
            SET search_path = pg_catalog
            AS $function$
                SELECT stable.id,
                       content.person_id,
                       stable.version,
                       content.status::text,
                       content.effective_from,
                       CASE
                           WHEN content.effective_to IS NULL THEN content.planned_valid_to
                           WHEN content.planned_valid_to IS NULL THEN content.effective_to
                           ELSE LEAST(content.effective_to, content.planned_valid_to)
                       END,
                       content.semantic_role_code::text
                FROM public.staff_assignments AS stable
                JOIN public.staff_assignment_versions AS content
                  ON content.school_id = stable.school_id
                 AND content.assignment_id = stable.id
                WHERE p_school_id = app.require_school_context()
                  AND stable.school_id = p_school_id
                  AND stable.id = p_assignment_id
                  AND content.status = 'confirmed'
                  AND content.revoked_at IS NULL
                  AND content.effective_from <= p_as_of
                  AND (content.effective_to IS NULL OR content.effective_to > p_as_of)
                  AND (
                      content.planned_valid_to IS NULL
                      OR content.planned_valid_to > p_as_of
                  )
                  AND app_private.school_fact_relationship_slice_is_effective_at(
                      content.school_id,
                      'assignment',
                      content.assignment_id,
                      content.target_version,
                      p_as_of
                  )
                  AND app_private.school_fact_identity_root_is_effective_at(
                      content.school_id,
                      'school',
                      content.school_id,
                      p_as_of
                  )
                  AND app_private.school_fact_identity_root_is_effective_at(
                      content.school_id,
                      'person',
                      content.person_id,
                      p_as_of
                  )
                  AND app_private.school_fact_identity_root_is_effective_at(
                      content.school_id,
                      'organization',
                      content.organization_unit_id,
                      p_as_of
                  );
            $function$;
            """
        )
    )
    op.execute(f"REVOKE ALL ON FUNCTION {helper_signature} FROM PUBLIC")
    op.execute(
        f"GRANT EXECUTE ON FUNCTION {helper_signature} "
        "TO campus_ai_takeover_definer, campus_ai_audit_definer"
    )
    op.execute("SET LOCAL ROLE campus_ai_db_owner")
    op.execute("REVOKE CREATE ON SCHEMA app_private FROM campus_ai_context_definer")

    op.execute("GRANT CREATE ON SCHEMA app, app_private TO campus_ai_takeover_definer")
    op.execute("SET LOCAL ROLE campus_ai_takeover_definer")
    op.execute(
        f"ALTER FUNCTION {lock_source_signature} "
        "RENAME TO lock_master_data_import_takeover_source_rev17"
    )
    op.execute(
        "ALTER FUNCTION app.lock_master_data_import_takeover_source_rev17(uuid,uuid,uuid) "
        "SET SCHEMA app_private"
    )
    op.execute(
        f"ALTER FUNCTION {lock_assignment_signature} "
        "RENAME TO lock_master_data_import_takeover_assignment_rev17"
    )
    op.execute(
        "ALTER FUNCTION app.lock_master_data_import_takeover_assignment_rev17("
        "uuid,uuid,uuid,uuid) SET SCHEMA app_private"
    )
    op.execute(
        f"ALTER FUNCTION {supersede_signature} "
        "RENAME TO supersede_master_data_import_takeover_source_rev17"
    )
    op.execute(
        "ALTER FUNCTION app.supersede_master_data_import_takeover_source_rev17("
        "uuid,uuid,uuid,text,bigint) SET SCHEMA app_private"
    )
    op.execute(
        f"ALTER FUNCTION {attempt_signature} "
        "RENAME TO enforce_master_data_import_attempt_transition_rev17"
    )
    for backup_signature in (
        "app_private.lock_master_data_import_takeover_source_rev17(uuid,uuid,uuid)",
        ("app_private.lock_master_data_import_takeover_assignment_rev17(uuid,uuid,uuid,uuid)"),
        (
            "app_private.supersede_master_data_import_takeover_source_rev17("
            "uuid,uuid,uuid,text,bigint)"
        ),
    ):
        op.execute(
            f"REVOKE EXECUTE ON FUNCTION {backup_signature} "
            "FROM PUBLIC, campus_ai_runtime, campus_ai_db_owner"
        )

    op.execute(
        sa.text(
            r"""
            DO $rewrite$
            DECLARE
                v_definition text;
                v_rewritten text;
                v_old text;
                v_new text;
            BEGIN
                SELECT pg_catalog.pg_get_functiondef(
                    'app_private.lock_master_data_import_takeover_source_rev17('
                    'uuid,uuid,uuid)'::regprocedure
                ) INTO v_definition;
                v_rewritten := pg_catalog.replace(
                    v_definition,
                    'app_private.lock_master_data_import_takeover_source_rev17',
                    'app.lock_master_data_import_takeover_source'
                );
                v_old := $old$
            FROM public.staff_assignments AS current_assignment
            WHERE current_assignment.school_id = v_school_id
              AND current_assignment.id = current_setting('app.assignment_id', true)::uuid
              AND current_assignment.person_id = current_setting('app.person_id', true)::uuid
              AND current_assignment.version =
                  current_setting('app.assignment_version', true)::bigint
              AND current_assignment.semantic_role_code = 'school_master_data_manager'
              AND current_assignment.status = 'confirmed'
              AND current_assignment.valid_from <= v_now
              AND (
                  current_assignment.valid_to IS NULL
                  OR current_assignment.valid_to > v_now
              );$old$;
                v_new := $new$
            FROM app_private.master_data_import_effective_assignment(
                v_school_id,
                current_setting('app.assignment_id', true)::uuid,
                v_now
            ) AS current_assignment
            WHERE current_assignment.person_id =
                  current_setting('app.person_id', true)::uuid
              AND current_assignment.version =
                  current_setting('app.assignment_version', true)::bigint
              AND current_assignment.semantic_role_code = 'school_master_data_manager';$new$;
                IF pg_catalog.strpos(v_rewritten, v_old) = 0 THEN
                    RAISE EXCEPTION 'revision 18 takeover source definition mismatch';
                END IF;
                EXECUTE pg_catalog.replace(v_rewritten, v_old, v_new);

                SELECT pg_catalog.pg_get_functiondef(
                    'app_private.lock_master_data_import_takeover_assignment_rev17('
                    'uuid,uuid,uuid,uuid)'::regprocedure
                ) INTO v_definition;
                v_rewritten := pg_catalog.replace(
                    v_definition,
                    'app_private.lock_master_data_import_takeover_assignment_rev17',
                    'app.lock_master_data_import_takeover_assignment'
                );
                IF pg_catalog.strpos(v_rewritten, v_old) = 0 THEN
                    RAISE EXCEPTION 'revision 18 takeover assignment actor definition mismatch';
                END IF;
                v_rewritten := pg_catalog.replace(v_rewritten, v_old, v_new);
                v_old := $old$
            RETURN QUERY
            SELECT original_assignment.id,
                   original_assignment.person_id,
                   original_assignment.version,
                   original_assignment.status::text,
                   original_assignment.valid_from,
                   original_assignment.valid_to
            FROM public.staff_assignments AS original_assignment
            WHERE original_assignment.school_id = v_school_id
              AND original_assignment.id = p_assignment_id
            FOR UPDATE OF original_assignment;$old$;
                v_new := $new$
            PERFORM 1
            FROM public.staff_assignments AS original_assignment
            WHERE original_assignment.school_id = v_school_id
              AND original_assignment.id = p_assignment_id
            FOR UPDATE OF original_assignment;

            RETURN QUERY
            SELECT original_assignment.assignment_id,
                   original_assignment.person_id,
                   original_assignment.version,
                   original_assignment.status,
                   original_assignment.valid_from,
                   original_assignment.valid_to
            FROM app_private.master_data_import_effective_assignment(
                v_school_id, p_assignment_id, v_now
            ) AS original_assignment;$new$;
                IF pg_catalog.strpos(v_rewritten, v_old) = 0 THEN
                    RAISE EXCEPTION 'revision 18 takeover assignment target definition mismatch';
                END IF;
                EXECUTE pg_catalog.replace(v_rewritten, v_old, v_new);

                SELECT pg_catalog.pg_get_functiondef(
                    'app_private.supersede_master_data_import_takeover_source_rev17('
                    'uuid,uuid,uuid,text,bigint)'::regprocedure
                ) INTO v_definition;
                v_rewritten := pg_catalog.replace(
                    v_definition,
                    'app_private.supersede_master_data_import_takeover_source_rev17',
                    'app.supersede_master_data_import_takeover_source'
                );
                v_old := $old$
            FROM public.staff_assignments AS current_assignment
            WHERE current_assignment.school_id = v_school_id
              AND current_assignment.id = v_actor_assignment_id
              AND current_assignment.person_id = v_actor_person_id
              AND current_assignment.version = v_actor_assignment_version
              AND current_assignment.semantic_role_code = 'school_master_data_manager'
              AND current_assignment.status = 'confirmed'
              AND current_assignment.valid_from <= v_now
              AND (
                  current_assignment.valid_to IS NULL
                  OR current_assignment.valid_to > v_now
              );$old$;
                v_new := $new$
            FROM app_private.master_data_import_effective_assignment(
                v_school_id, v_actor_assignment_id, v_now
            ) AS current_assignment
            WHERE current_assignment.person_id = v_actor_person_id
              AND current_assignment.version = v_actor_assignment_version
              AND current_assignment.semantic_role_code = 'school_master_data_manager';$new$;
                IF pg_catalog.strpos(v_rewritten, v_old) = 0 THEN
                    RAISE EXCEPTION 'revision 18 takeover supersede actor definition mismatch';
                END IF;
                v_rewritten := pg_catalog.replace(v_rewritten, v_old, v_new);
                v_old := $old$
                      FROM public.staff_assignments AS original_assignment
                      WHERE original_assignment.school_id = old_record.school_id
                        AND original_assignment.id = old_record.actor_assignment_id
                        AND original_assignment.person_id = old_record.actor_person_id
                        AND original_assignment.version = old_record.actor_assignment_version
                        AND original_assignment.status = 'confirmed'
                        AND original_assignment.valid_from <= v_now
                        AND (
                            original_assignment.valid_to IS NULL
                            OR original_assignment.valid_to > v_now
                        )$old$;
                v_new := $new$
                      FROM app_private.master_data_import_effective_assignment(
                          old_record.school_id,
                          old_record.actor_assignment_id,
                          v_now
                      ) AS original_assignment
                      WHERE original_assignment.person_id = old_record.actor_person_id
                        AND original_assignment.version =
                            old_record.actor_assignment_version$new$;
                IF pg_catalog.strpos(v_rewritten, v_old) = 0 THEN
                    RAISE EXCEPTION 'revision 18 takeover supersede source definition mismatch';
                END IF;
                EXECUTE pg_catalog.replace(v_rewritten, v_old, v_new);

                SELECT pg_catalog.pg_get_functiondef(
                    'app_private.enforce_master_data_import_attempt_transition_rev17()'
                    ::regprocedure
                ) INTO v_definition;
                v_rewritten := pg_catalog.replace(
                    v_definition,
                    'app_private.enforce_master_data_import_attempt_transition_rev17',
                    'app_private.enforce_master_data_import_attempt_transition'
                );
                v_old := $old$
            JOIN public.staff_assignments AS current_assignment
              ON current_assignment.school_id = new_record.school_id
             AND current_assignment.id = new_record.actor_assignment_id
             AND current_assignment.person_id = new_record.actor_person_id
             AND current_assignment.version = new_record.actor_assignment_version
             AND current_assignment.semantic_role_code = 'school_master_data_manager'
             AND current_assignment.status = 'confirmed'
             AND current_assignment.valid_from <= v_now
             AND (
                 current_assignment.valid_to IS NULL
                 OR current_assignment.valid_to > v_now
             )$old$;
                v_new := $new$
            JOIN LATERAL app_private.master_data_import_effective_assignment(
                new_record.school_id,
                new_record.actor_assignment_id,
                v_now
            ) AS current_assignment
              ON current_assignment.person_id = new_record.actor_person_id
             AND current_assignment.version = new_record.actor_assignment_version
             AND current_assignment.semantic_role_code = 'school_master_data_manager'$new$;
                IF pg_catalog.strpos(v_rewritten, v_old) = 0 THEN
                    RAISE EXCEPTION 'revision 18 takeover attempt definition mismatch';
                END IF;
                EXECUTE pg_catalog.replace(v_rewritten, v_old, v_new);
            END
            $rewrite$;
            """
        )
    )
    for signature in (
        lock_source_signature,
        lock_assignment_signature,
        supersede_signature,
    ):
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO campus_ai_runtime")
    op.execute(f"REVOKE ALL ON FUNCTION {attempt_signature} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {attempt_signature} TO campus_ai_db_owner")
    op.execute("SET LOCAL ROLE campus_ai_db_owner")
    op.execute(
        "DROP TRIGGER enforce_master_data_import_attempt_transition "
        "ON public.master_data_import_units"
    )
    op.execute(
        "CREATE CONSTRAINT TRIGGER enforce_master_data_import_attempt_transition "
        "AFTER UPDATE OF last_attempt_idempotency_record_id, last_attempt_completed "
        "ON public.master_data_import_units DEFERRABLE INITIALLY DEFERRED "
        f"FOR EACH ROW EXECUTE FUNCTION {attempt_signature}"
    )
    op.execute("SET LOCAL ROLE campus_ai_takeover_definer")
    op.execute(f"REVOKE EXECUTE ON FUNCTION {attempt_signature} FROM campus_ai_db_owner")
    op.execute("SET LOCAL ROLE campus_ai_db_owner")
    op.execute("REVOKE CREATE ON SCHEMA app, app_private FROM campus_ai_takeover_definer")

    audit_signature = (
        "app.append_master_data_import_audit(integer,uuid,uuid,uuid,bigint,text,text,"
        "text,uuid,uuid,uuid,jsonb,jsonb,uuid,text)"
    )
    audit_backup_signature = (
        "app_private.append_master_data_import_audit_rev17(integer,uuid,uuid,uuid,"
        "bigint,text,text,text,uuid,uuid,uuid,jsonb,jsonb,uuid,text)"
    )
    op.execute("GRANT CREATE ON SCHEMA app, app_private TO campus_ai_audit_definer")
    op.execute("SET LOCAL ROLE campus_ai_audit_definer")
    audit_xid_signature = (
        "app_private.append_master_data_import_audit_xid(integer,uuid,uuid,uuid,"
        "bigint,text,text,text,uuid,uuid,uuid,jsonb,jsonb,uuid,text)"
    )
    op.execute(
        f"ALTER FUNCTION {audit_xid_signature} RENAME TO append_master_data_import_audit_xid_rev17"
    )
    op.execute(
        sa.text(
            r"""
            DO $rewrite_audit_xid$
            DECLARE
                v_definition text;
                v_rewritten text;
                v_old text;
                v_new text;
            BEGIN
                SELECT pg_catalog.pg_get_functiondef(
                    'app_private.append_master_data_import_audit_xid_rev17('
                    'integer,uuid,uuid,uuid,bigint,text,text,text,uuid,uuid,uuid,'
                    'jsonb,jsonb,uuid,text)'::regprocedure
                ) INTO v_definition;
                v_rewritten := pg_catalog.replace(
                    v_definition,
                    'app_private.append_master_data_import_audit_xid_rev17',
                    'app_private.append_master_data_import_audit_xid'
                );
                v_old := $old$
                FROM public.staff_assignments AS assignment
                WHERE assignment.school_id = v_school_id
                  AND assignment.id = v_actor_assignment_id
                  AND assignment.person_id = v_actor_person_id
                  AND assignment.version = v_actor_assignment_version
                  AND assignment.status = 'confirmed'
                  AND assignment.semantic_role_code = 'school_master_data_manager'
                  AND assignment.valid_from <= v_now
                  AND (assignment.valid_to IS NULL OR assignment.valid_to > v_now)
            ) INTO v_authorized;$old$;
                v_new := $new$
                FROM app_private.master_data_import_effective_assignment(
                    v_school_id, v_actor_assignment_id, v_now
                ) AS assignment
                WHERE assignment.person_id = v_actor_person_id
                  AND assignment.version = v_actor_assignment_version
                  AND assignment.semantic_role_code = 'school_master_data_manager'
            ) INTO v_authorized;$new$;
                IF pg_catalog.strpos(v_rewritten, v_old) = 0 THEN
                    RAISE EXCEPTION 'revision 18 import audit xid assignment definition mismatch';
                END IF;
                EXECUTE pg_catalog.replace(v_rewritten, v_old, v_new);
            END
            $rewrite_audit_xid$;
            """
        )
    )
    op.execute(f"REVOKE ALL ON FUNCTION {audit_xid_signature} FROM PUBLIC")
    op.execute(f"ALTER FUNCTION {audit_signature} RENAME TO append_master_data_import_audit_rev17")
    op.execute(
        "ALTER FUNCTION app.append_master_data_import_audit_rev17(integer,uuid,uuid,"
        "uuid,bigint,text,text,text,uuid,uuid,uuid,jsonb,jsonb,uuid,text) "
        "SET SCHEMA app_private"
    )
    op.execute(
        f"REVOKE EXECUTE ON FUNCTION {audit_backup_signature} "
        "FROM PUBLIC, campus_ai_runtime, campus_ai_db_owner, campus_ai_takeover_definer"
    )
    op.execute(
        sa.text(
            r"""
            DO $rewrite_audit$
            DECLARE
                v_definition text;
                v_rewritten text;
                v_old text;
                v_new text;
            BEGIN
                SELECT pg_catalog.pg_get_functiondef(
                    'app_private.append_master_data_import_audit_rev17('
                    'integer,uuid,uuid,uuid,bigint,text,text,text,uuid,uuid,uuid,'
                    'jsonb,jsonb,uuid,text)'::regprocedure
                ) INTO v_definition;
                v_rewritten := pg_catalog.replace(
                    v_definition,
                    'app_private.append_master_data_import_audit_rev17',
                    'app.append_master_data_import_audit'
                );
                v_old := $old$
            FROM public.staff_assignments AS assignment
            WHERE assignment.school_id = v_school_id
              AND assignment.id = v_actor_assignment_id
              AND assignment.person_id = v_actor_person_id
              AND assignment.version = v_actor_assignment_version
              AND assignment.status = 'confirmed'
              AND assignment.semantic_role_code = 'school_master_data_manager'
              AND assignment.valid_from <= v_now
              AND (assignment.valid_to IS NULL OR assignment.valid_to > v_now);$old$;
                v_new := $new$
            FROM app_private.master_data_import_effective_assignment(
                v_school_id, v_actor_assignment_id, v_now
            ) AS assignment
            WHERE assignment.person_id = v_actor_person_id
              AND assignment.version = v_actor_assignment_version
              AND assignment.semantic_role_code = 'school_master_data_manager';$new$;
                IF pg_catalog.strpos(v_rewritten, v_old) = 0 THEN
                    RAISE EXCEPTION 'revision 18 import audit assignment definition mismatch';
                END IF;
                EXECUTE pg_catalog.replace(v_rewritten, v_old, v_new);
            END
            $rewrite_audit$;
            """
        )
    )
    op.execute(f"REVOKE ALL ON FUNCTION {audit_signature} FROM PUBLIC")
    op.execute(
        f"GRANT EXECUTE ON FUNCTION {audit_signature} "
        "TO campus_ai_runtime, campus_ai_db_owner, campus_ai_takeover_definer"
    )
    op.execute("SET LOCAL ROLE campus_ai_db_owner")
    op.execute("REVOKE CREATE ON SCHEMA app, app_private FROM campus_ai_audit_definer")


def _drop_temporal_takeover_assignment_reader() -> None:
    """Restore the exact revision 17 takeover functions and trigger target."""

    helper_signature = "app_private.master_data_import_effective_assignment(uuid,uuid,timestamptz)"
    op.execute(
        "DROP TRIGGER enforce_master_data_import_attempt_transition "
        "ON public.master_data_import_units"
    )
    op.execute("GRANT CREATE ON SCHEMA app, app_private TO campus_ai_takeover_definer")
    op.execute("SET LOCAL ROLE campus_ai_takeover_definer")
    op.execute("DROP FUNCTION app.lock_master_data_import_takeover_source(uuid,uuid,uuid)")
    op.execute("DROP FUNCTION app.lock_master_data_import_takeover_assignment(uuid,uuid,uuid,uuid)")
    op.execute(
        "DROP FUNCTION app.supersede_master_data_import_takeover_source(uuid,uuid,uuid,text,bigint)"
    )
    op.execute("DROP FUNCTION app_private.enforce_master_data_import_attempt_transition()")
    op.execute(
        "ALTER FUNCTION app_private.lock_master_data_import_takeover_source_rev17("
        "uuid,uuid,uuid) SET SCHEMA app"
    )
    op.execute(
        "ALTER FUNCTION app.lock_master_data_import_takeover_source_rev17("
        "uuid,uuid,uuid) RENAME TO lock_master_data_import_takeover_source"
    )
    op.execute(
        "ALTER FUNCTION app_private.lock_master_data_import_takeover_assignment_rev17("
        "uuid,uuid,uuid,uuid) SET SCHEMA app"
    )
    op.execute(
        "ALTER FUNCTION app.lock_master_data_import_takeover_assignment_rev17("
        "uuid,uuid,uuid,uuid) RENAME TO lock_master_data_import_takeover_assignment"
    )
    op.execute(
        "ALTER FUNCTION app_private.supersede_master_data_import_takeover_source_rev17("
        "uuid,uuid,uuid,text,bigint) SET SCHEMA app"
    )
    op.execute(
        "ALTER FUNCTION app.supersede_master_data_import_takeover_source_rev17("
        "uuid,uuid,uuid,text,bigint) RENAME TO supersede_master_data_import_takeover_source"
    )
    op.execute(
        "ALTER FUNCTION app_private.enforce_master_data_import_attempt_transition_rev17() "
        "RENAME TO enforce_master_data_import_attempt_transition"
    )
    for signature in (
        "app.lock_master_data_import_takeover_source(uuid,uuid,uuid)",
        "app.lock_master_data_import_takeover_assignment(uuid,uuid,uuid,uuid)",
        "app.supersede_master_data_import_takeover_source(uuid,uuid,uuid,text,bigint)",
    ):
        op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO campus_ai_runtime")
    op.execute(
        "GRANT EXECUTE ON FUNCTION "
        "app_private.enforce_master_data_import_attempt_transition() "
        "TO campus_ai_db_owner"
    )
    op.execute("SET LOCAL ROLE campus_ai_db_owner")
    op.execute(
        "CREATE CONSTRAINT TRIGGER enforce_master_data_import_attempt_transition "
        "AFTER UPDATE OF last_attempt_idempotency_record_id, last_attempt_completed "
        "ON public.master_data_import_units DEFERRABLE INITIALLY DEFERRED "
        "FOR EACH ROW EXECUTE FUNCTION "
        "app_private.enforce_master_data_import_attempt_transition()"
    )
    op.execute("SET LOCAL ROLE campus_ai_takeover_definer")
    op.execute(
        "REVOKE EXECUTE ON FUNCTION "
        "app_private.enforce_master_data_import_attempt_transition() "
        "FROM campus_ai_db_owner"
    )
    op.execute("SET LOCAL ROLE campus_ai_db_owner")
    op.execute("REVOKE CREATE ON SCHEMA app, app_private FROM campus_ai_takeover_definer")
    audit_signature = (
        "app.append_master_data_import_audit(integer,uuid,uuid,uuid,bigint,text,text,"
        "text,uuid,uuid,uuid,jsonb,jsonb,uuid,text)"
    )
    op.execute("GRANT CREATE ON SCHEMA app, app_private TO campus_ai_audit_definer")
    op.execute("SET LOCAL ROLE campus_ai_audit_definer")
    op.execute(f"DROP FUNCTION {audit_signature}")
    op.execute(
        "DROP FUNCTION app_private.append_master_data_import_audit_xid(integer,uuid,"
        "uuid,uuid,bigint,text,text,text,uuid,uuid,uuid,jsonb,jsonb,uuid,text)"
    )
    op.execute(
        "ALTER FUNCTION app_private.append_master_data_import_audit_xid_rev17(integer,"
        "uuid,uuid,uuid,bigint,text,text,text,uuid,uuid,uuid,jsonb,jsonb,uuid,text) "
        "RENAME TO append_master_data_import_audit_xid"
    )
    op.execute(
        "ALTER FUNCTION app_private.append_master_data_import_audit_rev17(integer,"
        "uuid,uuid,uuid,bigint,text,text,text,uuid,uuid,uuid,jsonb,jsonb,uuid,text) "
        "SET SCHEMA app"
    )
    op.execute(
        "ALTER FUNCTION app.append_master_data_import_audit_rev17(integer,uuid,uuid,"
        "uuid,bigint,text,text,text,uuid,uuid,uuid,jsonb,jsonb,uuid,text) "
        "RENAME TO append_master_data_import_audit"
    )
    op.execute(
        f"GRANT EXECUTE ON FUNCTION {audit_signature} "
        "TO campus_ai_runtime, campus_ai_db_owner, campus_ai_takeover_definer"
    )
    op.execute("SET LOCAL ROLE campus_ai_db_owner")
    op.execute("REVOKE CREATE ON SCHEMA app, app_private FROM campus_ai_audit_definer")
    op.execute("GRANT CREATE ON SCHEMA app_private TO campus_ai_context_definer")
    op.execute("SET LOCAL ROLE campus_ai_context_definer")
    op.execute(
        f"REVOKE EXECUTE ON FUNCTION {helper_signature} "
        "FROM campus_ai_takeover_definer, campus_ai_audit_definer"
    )
    op.execute(f"DROP FUNCTION {helper_signature}")
    op.execute("SET LOCAL ROLE campus_ai_db_owner")
    op.execute("REVOKE CREATE ON SCHEMA app_private FROM campus_ai_context_definer")


def upgrade() -> None:
    op.execute("SET LOCAL ROLE campus_ai_db_owner")
    _guard_upgrade_without_unmappable_rows()
    _backfill_unambiguous_legacy_school_versions()
    _add_historical_structure_slices()
    _create_relationship_content_slices()
    _create_historical_content_terminal_reader()
    _create_temporal_takeover_assignment_reader()
    _create_temporal_execution_context_functions()
    _guard_upgrade_location_history()
    _drop_residence_edges()
    _add_student_campuses()
    op.add_column(
        "school_policy_versions",
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
    )
    _create_organization_campus_relationships()
    _create_policy_scopes()
    _create_new_fact_history_delete_guards()
    op.drop_constraint(
        "ck_school_fact_change_lineage_operation",
        "school_fact_change_lineage",
        type_="check",
    )
    op.create_check_constraint(
        "ck_school_fact_change_lineage_operation",
        "school_fact_change_lineage",
        "operation IN ('upsert', 'end', 'revoke', 'reactivate')",
    )
    _create_school_reactivation_context()
    _create_root_reactivation_context()
    _create_student_person_identity_guard()
    _create_structure_history_guards()
    _create_campus_target_guards()
    _create_location_residence_final_guard()


def _guard_safe_downgrade() -> None:
    op.execute(
        sa.text(
            r"""
            DO $downgrade_guard$
            BEGIN
                IF EXISTS (SELECT 1 FROM public.organization_campus_relationships)
                   OR EXISTS (SELECT 1 FROM public.school_policy_scopes)
                   OR EXISTS (SELECT 1 FROM public.student_school_relationships)
                   OR EXISTS (
                       SELECT 1 FROM public.school_policy_versions
                       WHERE revoked_at IS NOT NULL
                   )
                   OR EXISTS (
                       SELECT 1 FROM public.school_fact_change_lineage
                       WHERE operation = 'reactivate'
                   )
                   OR EXISTS (
                       SELECT 1
                       FROM public.staff_assignments AS stable
                       LEFT JOIN public.staff_assignment_versions AS version
                         ON version.school_id = stable.school_id
                        AND version.assignment_id = stable.id
                       GROUP BY stable.id, stable.school_id, stable.person_id,
                                stable.organization_unit_id, stable.position_title,
                                stable.semantic_role_code, stable.assignment_kind,
                                stable.appointment_source, stable.approval_reference,
                                stable.valid_from, stable.valid_to, stable.revoked_at,
                                stable.status
                       HAVING count(version.id) <> 1
                          OR bool_or(
                               version.person_id IS DISTINCT FROM stable.person_id
                            OR version.organization_unit_id
                                  IS DISTINCT FROM stable.organization_unit_id
                            OR version.position_title IS DISTINCT FROM stable.position_title
                            OR version.semantic_role_code
                                  IS DISTINCT FROM stable.semantic_role_code
                            OR version.assignment_kind
                                  IS DISTINCT FROM stable.assignment_kind
                            OR version.appointment_source
                                  IS DISTINCT FROM stable.appointment_source
                            OR version.approval_reference
                                  IS DISTINCT FROM stable.approval_reference
                            OR version.effective_from IS DISTINCT FROM stable.valid_from
                            OR version.effective_to IS NOT NULL
                            OR version.planned_valid_to IS DISTINCT FROM stable.valid_to
                            OR version.revoked_at IS NOT NULL
                            OR version.status IS DISTINCT FROM stable.status
                          )
                   )
                   OR EXISTS (
                       SELECT 1
                       FROM public.account_person_bindings AS stable
                       JOIN public.users AS users ON users.id = stable.account_user_id
                       LEFT JOIN public.account_person_binding_versions AS version
                         ON version.school_id = stable.school_id
                        AND version.binding_id = stable.id
                       GROUP BY stable.id, stable.school_id, stable.account_user_id,
                                stable.person_id, stable.valid_from, stable.valid_to,
                                stable.status, users.provider_code,
                                users.provider_tenant_key, users.external_subject
                       HAVING count(version.id) <> 1
                          OR bool_or(
                               version.account_user_id
                                  IS DISTINCT FROM stable.account_user_id
                            OR version.person_id IS DISTINCT FROM stable.person_id
                            OR version.provider_code IS DISTINCT FROM users.provider_code
                            OR version.provider_tenant_key
                                  IS DISTINCT FROM users.provider_tenant_key
                            OR version.external_subject
                                  IS DISTINCT FROM users.external_subject
                            OR version.effective_from IS DISTINCT FROM stable.valid_from
                            OR version.effective_to IS DISTINCT FROM stable.valid_to
                            OR version.revoked_at IS NOT NULL
                            OR version.status IS DISTINCT FROM stable.status
                          )
                   )
                   OR EXISTS (
                       SELECT 1
                       FROM public.campus_location_versions AS version
                       JOIN public.campus_locations AS stable
                         ON stable.school_id = version.school_id
                        AND stable.id = version.location_id
                       WHERE version.parent_location_id
                                 IS DISTINCT FROM stable.parent_location_id
                          OR version.kind IS DISTINCT FROM stable.kind
                   )
                   OR EXISTS (
                       SELECT 1
                       FROM public.organization_unit_versions AS version
                       JOIN public.organization_units AS stable
                         ON stable.school_id = version.school_id
                        AND stable.id = version.organization_unit_id
                       WHERE version.parent_organization_unit_id
                                 IS DISTINCT FROM stable.parent_organization_unit_id
                   )
                   OR EXISTS (
                       SELECT 1
                       FROM public.school_fact_sources AS source
                       JOIN public.schools AS stable ON stable.id = source.school_id
                       WHERE source.source_key =
                                 'revision18:legacy-school-v1:' || source.school_id::text
                         AND (
                             source.source_kind <> 'legacy'
                             OR source.source_ref IS DISTINCT FROM source.source_key
                             OR stable.status <> 'active'
                             OR stable.version <> 1
                             OR EXISTS (
                                 SELECT 1
                                 FROM public.school_fact_change_lineage AS lineage
                                 WHERE lineage.school_id = stable.id
                                   AND lineage.fact_kind = 'school'
                             )
                             OR (
                                 SELECT count(*)
                                 FROM public.school_versions AS version
                                 WHERE version.school_id = stable.id
                             ) <> 1
                             OR NOT EXISTS (
                                 SELECT 1
                                 FROM public.school_versions AS version
                                 WHERE version.school_id = stable.id
                                   AND version.source_id = source.id
                                   AND version.business_code = stable.school_code
                                   AND version.display_name = stable.display_name
                                   AND version.short_name IS NULL
                                   AND version.timezone_name = stable.timezone_name
                                   AND version.lifecycle_status = 'active'
                                   AND version.valid_from = stable.created_at
                                   AND version.valid_to IS NULL
                                   AND version.version = 1
                                   AND version.import_batch_id IS NULL
                             )
                         )
                   )
                   OR EXISTS (SELECT 1 FROM public.student_bed_residences) THEN
                    RAISE EXCEPTION USING
                        ERRCODE = '55000',
                        MESSAGE = 'revision 18 contains facts not representable by revision 17';
                END IF;
            END
            $downgrade_guard$;
            """
        )
    )


def _remove_unmodified_legacy_school_version_backfill() -> None:
    """Restore the exact rev17 stable-only shape after the downgrade guard proves safety."""

    op.execute(
        "ALTER TABLE public.school_versions DISABLE TRIGGER trg_sf1_reject_delete_school_versions"
    )
    op.execute(
        sa.text(
            r"""
            DELETE FROM public.school_versions AS version
            USING public.school_fact_sources AS source
            WHERE source.school_id = version.school_id
              AND source.id = version.source_id
              AND source.source_key =
                    'revision18:legacy-school-v1:' || source.school_id::text
              AND source.source_kind = 'legacy'
              AND source.source_ref = source.source_key;
            """
        )
    )
    op.execute(
        "ALTER TABLE public.school_versions ENABLE TRIGGER trg_sf1_reject_delete_school_versions"
    )
    op.execute(
        sa.text(
            r"""
            DELETE FROM public.school_fact_sources AS source
            WHERE source.source_key =
                      'revision18:legacy-school-v1:' || source.school_id::text
              AND source.source_kind = 'legacy'
              AND source.source_ref = source.source_key;
            """
        )
    )


def _restore_residence_edges() -> None:
    op.add_column(
        "student_bed_residences",
        sa.Column("arrived_from_residence_id", sa.Uuid(), nullable=True),
    )
    op.add_column(
        "student_bed_residences",
        sa.Column("departed_to_residence_id", sa.Uuid(), nullable=True),
    )
    op.create_unique_constraint(
        "uq_student_bed_residences_arrived_from",
        "student_bed_residences",
        ["school_id", "arrived_from_residence_id"],
        deferrable=True,
        initially="DEFERRED",
    )
    op.create_unique_constraint(
        "uq_student_bed_residences_departed_to",
        "student_bed_residences",
        ["school_id", "departed_to_residence_id"],
        deferrable=True,
        initially="DEFERRED",
    )
    op.create_foreign_key(
        "fk_student_bed_residences_arrived_from",
        "student_bed_residences",
        "student_bed_residences",
        ["school_id", "arrived_from_residence_id"],
        ["school_id", "id"],
        ondelete="RESTRICT",
        deferrable=True,
        initially="DEFERRED",
    )
    op.create_foreign_key(
        "fk_student_bed_residences_departed_to",
        "student_bed_residences",
        "student_bed_residences",
        ["school_id", "departed_to_residence_id"],
        ["school_id", "id"],
        ondelete="RESTRICT",
        deferrable=True,
        initially="DEFERRED",
    )
    op.create_check_constraint(
        "ck_student_bed_residences_arrived_from_not_self",
        "student_bed_residences",
        "arrived_from_residence_id IS NULL OR arrived_from_residence_id <> id",
    )
    op.create_check_constraint(
        "ck_student_bed_residences_departed_to_not_self",
        "student_bed_residences",
        "departed_to_residence_id IS NULL OR departed_to_residence_id <> id",
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


def downgrade() -> None:
    op.execute("SET LOCAL ROLE campus_ai_db_owner")
    _guard_safe_downgrade()
    _remove_unmodified_legacy_school_version_backfill()
    op.execute(
        "DROP TRIGGER trg_sf18_validate_location_lineage_residences "
        "ON public.school_fact_change_lineage"
    )
    op.execute(
        "DROP TRIGGER trg_sf18_validate_location_terminal_residences ON public.campus_locations"
    )
    op.execute(
        "DROP TRIGGER trg_sf18_validate_residence_location_ancestry "
        "ON public.student_bed_residences"
    )
    op.execute("DROP TRIGGER trg_sf18_validate_bed_location_residences ON public.dorm_beds")
    op.execute(
        "DROP TRIGGER trg_sf18_validate_location_residences ON public.campus_location_versions"
    )
    op.execute("DROP FUNCTION public.sf18_validate_inactive_campus_residences()")
    op.execute(
        "DROP TRIGGER trg_sf18_lock_residence_location_ancestry ON public.student_bed_residences"
    )
    op.execute("DROP TRIGGER trg_sf18_lock_bed_location_residences ON public.dorm_beds")
    op.execute("DROP TRIGGER trg_sf18_lock_location_terminal_residences ON public.campus_locations")
    op.execute("DROP FUNCTION public.sf18_lock_location_residence_change()")
    op.execute(
        "DROP TRIGGER trg_sf18_require_policy_scope_campus_target ON public.school_policy_scopes"
    )
    op.execute(
        "DROP TRIGGER trg_sf18_preserve_referenced_campus_target ON public.campus_location_versions"
    )
    op.execute("DROP FUNCTION public.sf18_preserve_referenced_campus_target()")
    op.execute(
        "DROP TRIGGER trg_sf18_require_student_campus_targets "
        "ON public.student_school_relationships"
    )
    op.execute(
        "DROP TRIGGER trg_sf18_require_organization_campus_target "
        "ON public.organization_campus_relationships"
    )
    op.execute("DROP FUNCTION public.sf18_require_campus_target()")
    _drop_structure_history_guards()
    _drop_student_person_identity_guard()
    _drop_root_reactivation_context()
    _drop_school_reactivation_context()
    _drop_temporal_execution_context_functions()
    _drop_temporal_takeover_assignment_reader()
    _drop_historical_content_terminal_reader()
    _drop_relationship_content_slices()
    op.drop_constraint(
        "ck_school_fact_change_lineage_operation",
        "school_fact_change_lineage",
        type_="check",
    )
    op.create_check_constraint(
        "ck_school_fact_change_lineage_operation",
        "school_fact_change_lineage",
        "operation IN ('upsert', 'end', 'revoke')",
    )
    _drop_new_fact_history_delete_guards()
    op.drop_table("school_policy_scopes")
    op.drop_table("organization_campus_relationships")
    op.drop_constraint(
        "fk_student_school_relationships_administrative_campus",
        "student_school_relationships",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_student_school_relationships_primary_campus",
        "student_school_relationships",
        type_="foreignkey",
    )
    op.drop_column("student_school_relationships", "administrative_campus_location_id")
    op.drop_column("student_school_relationships", "primary_campus_location_id")
    op.drop_column("school_policy_versions", "revoked_at")
    _restore_residence_edges()
    _drop_historical_structure_slices()
