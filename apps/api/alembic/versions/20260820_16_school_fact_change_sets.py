"""Add SF2 canonical change staging and row lineage.

Revision ID: 20260820_16
Revises: 20260819_15
Create Date: 2026-08-20
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "20260820_16"
down_revision = "20260819_15"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint(
        "ck_master_data_import_command_outcomes_action",
        "master_data_import_command_outcomes",
        type_="check",
    )
    op.create_check_constraint(
        "ck_master_data_import_command_outcomes_action",
        "master_data_import_command_outcomes",
        "action_code IN ('master_data.import.manual_confirm', "
        "'master_data.import.ordinary_confirm', 'master_data.import.ordinary_resume', "
        "'master_data.import.ordinary_takeover', "
        "'master_data.import.calendar_publish', 'school_facts.confirm')",
    )
    op.drop_constraint(
        "ck_master_data_import_command_outcomes_unit_limits",
        "master_data_import_command_outcomes",
        type_="check",
    )
    op.create_check_constraint(
        "ck_master_data_import_command_outcomes_unit_limits",
        "master_data_import_command_outcomes",
        "cardinality(succeeded_units) <= 10000 "
        "AND cardinality(failed_units) <= 10000 "
        "AND cardinality(executed_units) <= 10000",
    )
    op.drop_constraint(
        "ck_master_data_import_batches_mode",
        "master_data_import_batches",
        type_="check",
    )
    op.create_check_constraint(
        "ck_master_data_import_batches_mode",
        "master_data_import_batches",
        "mode IN ('manual_unit', 'ordinary_atomic_units', "
        "'authoritative_set_publish', 'canonical_atomic_changes')",
    )
    op.drop_constraint(
        "ck_master_data_import_units_kind",
        "master_data_import_units",
        type_="check",
    )
    op.create_check_constraint(
        "ck_master_data_import_units_kind",
        "master_data_import_units",
        "unit_kind IN ('person', 'calendar_set', 'school_fact')",
    )

    for column in (
        sa.Column("plan_sha256", sa.String(length=64), nullable=True),
        sa.Column("base_fact_version", sa.BigInteger(), nullable=True),
        sa.Column("target_fact_version", sa.BigInteger(), nullable=True),
        sa.Column("confirmation_token_sha256", sa.String(length=64), nullable=True),
        sa.Column("confirmation_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("confirmation_consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("confirmation_session_sha256", sa.String(length=64), nullable=True),
        sa.Column("staged_by_user_id", sa.Integer(), nullable=True),
        sa.Column("staged_by_person_id", sa.Uuid(), nullable=True),
        sa.Column("staged_by_assignment_id", sa.Uuid(), nullable=True),
        sa.Column("staged_assignment_version", sa.BigInteger(), nullable=True),
        sa.Column("staged_authorization_version", sa.BigInteger(), nullable=True),
        sa.Column("result_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    ):
        op.add_column("master_data_import_batches", column)

    op.create_unique_constraint(
        "uq_master_data_import_batches_confirmation_token",
        "master_data_import_batches",
        ["confirmation_token_sha256"],
    )
    op.create_foreign_key(
        "fk_master_data_import_batches_staged_user",
        "master_data_import_batches",
        "users",
        ["staged_by_user_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_master_data_import_batches_staged_person",
        "master_data_import_batches",
        "school_people",
        ["school_id", "staged_by_person_id"],
        ["school_id", "id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_master_data_import_batches_staged_assignment",
        "master_data_import_batches",
        "staff_assignments",
        ["school_id", "staged_by_assignment_id"],
        ["school_id", "id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        "ck_master_data_import_batches_canonical_state",
        "master_data_import_batches",
        "(mode <> 'canonical_atomic_changes' AND plan_sha256 IS NULL "
        "AND base_fact_version IS NULL AND target_fact_version IS NULL "
        "AND confirmation_token_sha256 IS NULL AND confirmation_expires_at IS NULL "
        "AND confirmation_consumed_at IS NULL AND confirmation_session_sha256 IS NULL "
        "AND staged_by_user_id IS NULL AND staged_by_person_id IS NULL "
        "AND staged_by_assignment_id IS NULL AND staged_assignment_version IS NULL "
        "AND staged_authorization_version IS NULL AND result_payload IS NULL) OR "
        "(mode = 'canonical_atomic_changes' AND plan_sha256 ~ '^[0-9a-f]{64}$' "
        "AND base_fact_version >= 1 AND target_fact_version >= base_fact_version "
        "AND confirmation_token_sha256 ~ '^[0-9a-f]{64}$' "
        "AND confirmation_expires_at IS NOT NULL "
        "AND confirmation_session_sha256 ~ '^[0-9a-f]{64}$' "
        "AND staged_by_user_id IS NOT NULL AND staged_by_person_id IS NOT NULL "
        "AND staged_by_assignment_id IS NOT NULL AND staged_assignment_version >= 1 "
        "AND staged_authorization_version >= 1)",
    )

    op.create_table(
        "school_fact_change_lineage",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("school_id", sa.Uuid(), nullable=False),
        sa.Column("batch_id", sa.Uuid(), nullable=False),
        sa.Column("unit_id", sa.Uuid(), nullable=False),
        sa.Column("fact_kind", sa.String(length=80), nullable=False),
        sa.Column("fact_source_key", sa.String(length=255), nullable=False),
        sa.Column("operation", sa.String(length=20), nullable=False),
        sa.Column("target_id", sa.Uuid(), nullable=False),
        sa.Column("target_version", sa.BigInteger(), nullable=False),
        sa.Column("source_sha256", sa.String(length=64), nullable=False),
        sa.Column("sheet_code", sa.String(length=120), nullable=False),
        sa.Column("row_number", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "operation IN ('upsert', 'end', 'revoke')",
            name="ck_school_fact_change_lineage_operation",
        ),
        sa.CheckConstraint(
            "source_sha256 ~ '^[0-9a-f]{64}$' AND target_version >= 1 AND row_number >= 1",
            name="ck_school_fact_change_lineage_values",
        ),
        sa.ForeignKeyConstraint(
            ["school_id", "batch_id"],
            ["master_data_import_batches.school_id", "master_data_import_batches.id"],
            name="fk_school_fact_change_lineage_batch",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["school_id", "unit_id"],
            ["master_data_import_units.school_id", "master_data_import_units.id"],
            name="fk_school_fact_change_lineage_unit",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "school_id",
            "id",
            name="uq_school_fact_change_lineage_school_id",
        ),
        sa.UniqueConstraint(
            "school_id",
            "batch_id",
            "unit_id",
            name="uq_school_fact_change_lineage_unit",
        ),
        sa.UniqueConstraint(
            "school_id",
            "fact_kind",
            "fact_source_key",
            "target_version",
            name="uq_school_fact_change_lineage_fact_version",
        ),
    )
    op.create_index(
        "ix_school_fact_change_lineage_target",
        "school_fact_change_lineage",
        ["school_id", "fact_kind", "fact_source_key", "target_version"],
    )
    op.execute("ALTER TABLE public.school_fact_change_lineage ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE public.school_fact_change_lineage FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY tenant_select ON public.school_fact_change_lineage FOR SELECT "
        "USING (CASE WHEN CURRENT_USER = 'campus_ai_db_owner' THEN true "
        "ELSE school_id = (SELECT app.require_school_context()) END)"
    )
    op.execute(
        "CREATE POLICY owner_insert ON public.school_fact_change_lineage FOR INSERT "
        "WITH CHECK (CURRENT_USER = 'campus_ai_db_owner')"
    )

    op.execute(
        sa.text(
            r"""
            CREATE FUNCTION public.sf2_reject_lineage_mutation()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $function$
            BEGIN
                RAISE EXCEPTION USING
                    ERRCODE = '55000',
                    MESSAGE = 'school fact change lineage is append-only';
            END
            $function$;

            CREATE TRIGGER trg_sf2_reject_lineage_update
            BEFORE UPDATE OR DELETE ON public.school_fact_change_lineage
            FOR EACH ROW EXECUTE FUNCTION public.sf2_reject_lineage_mutation();
            """
        )
    )
    op.execute("SET LOCAL ROLE campus_ai_context_definer")
    op.execute(
        "GRANT EXECUTE ON FUNCTION app.bind_execution_context"
        "(uuid,integer,uuid,text,uuid,bigint,uuid,bigint,bigint,uuid,text,text,text,"
        "timestamptz,text) TO campus_ai_db_owner"
    )
    op.execute("SET LOCAL ROLE campus_ai_db_owner")


def downgrade() -> None:
    op.execute(
        sa.text(
            r"""
            DO $downgrade$
            BEGIN
                IF EXISTS (SELECT 1 FROM public.school_fact_change_lineage)
                   OR EXISTS (
                       SELECT 1 FROM public.master_data_import_batches
                       WHERE mode = 'canonical_atomic_changes'
                   ) THEN
                    RAISE EXCEPTION USING
                        ERRCODE = '55000',
                        MESSAGE = 'revision 16 contains school fact changes '
                                  'not representable by revision 15';
                END IF;
            END
            $downgrade$;
            """
        )
    )
    op.execute("SET LOCAL ROLE campus_ai_context_definer")
    op.execute(
        "REVOKE EXECUTE ON FUNCTION app.bind_execution_context"
        "(uuid,integer,uuid,text,uuid,bigint,uuid,bigint,bigint,uuid,text,text,text,"
        "timestamptz,text) FROM campus_ai_db_owner"
    )
    op.execute("SET LOCAL ROLE campus_ai_db_owner")
    op.drop_table("school_fact_change_lineage")
    op.execute(sa.text("DROP FUNCTION public.sf2_reject_lineage_mutation()"))
    op.drop_constraint(
        "ck_master_data_import_batches_canonical_state",
        "master_data_import_batches",
        type_="check",
    )
    op.drop_constraint(
        "uq_master_data_import_batches_confirmation_token",
        "master_data_import_batches",
        type_="unique",
    )
    for constraint_name in (
        "fk_master_data_import_batches_staged_assignment",
        "fk_master_data_import_batches_staged_person",
        "fk_master_data_import_batches_staged_user",
    ):
        op.drop_constraint(
            constraint_name,
            "master_data_import_batches",
            type_="foreignkey",
        )
    for name in (
        "result_payload",
        "staged_authorization_version",
        "staged_assignment_version",
        "staged_by_assignment_id",
        "staged_by_person_id",
        "staged_by_user_id",
        "confirmation_session_sha256",
        "confirmation_consumed_at",
        "confirmation_expires_at",
        "confirmation_token_sha256",
        "target_fact_version",
        "base_fact_version",
        "plan_sha256",
    ):
        op.drop_column("master_data_import_batches", name)
    op.drop_constraint(
        "ck_master_data_import_units_kind",
        "master_data_import_units",
        type_="check",
    )
    op.create_check_constraint(
        "ck_master_data_import_units_kind",
        "master_data_import_units",
        "unit_kind IN ('person', 'calendar_set')",
    )
    op.drop_constraint(
        "ck_master_data_import_batches_mode",
        "master_data_import_batches",
        type_="check",
    )
    op.create_check_constraint(
        "ck_master_data_import_batches_mode",
        "master_data_import_batches",
        "mode IN ('manual_unit', 'ordinary_atomic_units', 'authoritative_set_publish')",
    )
    op.drop_constraint(
        "ck_master_data_import_command_outcomes_unit_limits",
        "master_data_import_command_outcomes",
        type_="check",
    )
    op.create_check_constraint(
        "ck_master_data_import_command_outcomes_unit_limits",
        "master_data_import_command_outcomes",
        "cardinality(succeeded_units) <= 500 "
        "AND cardinality(failed_units) <= 500 "
        "AND cardinality(executed_units) <= 500",
    )
    op.drop_constraint(
        "ck_master_data_import_command_outcomes_action",
        "master_data_import_command_outcomes",
        type_="check",
    )
    op.create_check_constraint(
        "ck_master_data_import_command_outcomes_action",
        "master_data_import_command_outcomes",
        "action_code IN ('master_data.import.manual_confirm', "
        "'master_data.import.ordinary_confirm', 'master_data.import.ordinary_resume', "
        "'master_data.import.ordinary_takeover', 'master_data.import.calendar_publish')",
    )
