"""Expand complete SF3 import outcomes from 10,000 to 50,000 units.

Revision ID: 20260820_17
Revises: 20260820_16
Create Date: 2026-08-20
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260820_17"
down_revision = "20260820_16"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL ROLE campus_ai_db_owner")
    op.execute(
        "DROP TRIGGER verify_master_data_import_units_terminal_consistency "
        "ON public.master_data_import_units"
    )
    op.execute(
        "CREATE CONSTRAINT TRIGGER "
        "verify_master_data_import_units_insert_terminal_consistency "
        "AFTER INSERT ON public.master_data_import_units "
        "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW "
        "EXECUTE FUNCTION app_private.enforce_master_data_import_terminal_consistency()"
    )
    op.execute(
        """
        CREATE FUNCTION
            app_private.enforce_school_fact_unit_update_terminal_consistency()
        RETURNS trigger
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog
        AS $function$
        DECLARE
            v_batch_status text;
        BEGIN
            IF OLD.unit_kind = 'school_fact'
               AND NEW.unit_kind = 'school_fact'
               AND OLD.status = 'ready'
               AND NEW.status = 'applied'
               AND OLD.target_id IS NULL
               AND OLD.target_version IS NULL
               AND (
                   (NEW.target_id IS NULL AND NEW.target_version IS NULL)
                   OR (NEW.target_id IS NOT NULL AND NEW.target_version >= 1)
               )
               AND NEW.version = OLD.version + 1
               AND OLD.id IS NOT DISTINCT FROM NEW.id
               AND OLD.school_id IS NOT DISTINCT FROM NEW.school_id
               AND OLD.batch_id IS NOT DISTINCT FROM NEW.batch_id
               AND OLD.sequence IS NOT DISTINCT FROM NEW.sequence
               AND OLD.row_number IS NOT DISTINCT FROM NEW.row_number
               AND OLD.payload IS NOT DISTINCT FROM NEW.payload
               AND OLD.payload_sha256 IS NOT DISTINCT FROM NEW.payload_sha256
               AND OLD.soft_anomalies IS NOT DISTINCT FROM NEW.soft_anomalies
               AND OLD.initial_soft_anomalies
                   IS NOT DISTINCT FROM NEW.initial_soft_anomalies
               AND OLD.decision_reason IS NOT DISTINCT FROM NEW.decision_reason
               AND OLD.error_code IS NOT DISTINCT FROM NEW.error_code
               AND OLD.error_field_code IS NOT DISTINCT FROM NEW.error_field_code
               AND OLD.last_attempt_idempotency_record_id
                   IS NOT DISTINCT FROM NEW.last_attempt_idempotency_record_id
               AND OLD.last_attempt_completed
                   IS NOT DISTINCT FROM NEW.last_attempt_completed
               AND OLD.created_at IS NOT DISTINCT FROM NEW.created_at THEN
                SELECT batch.status
                INTO v_batch_status
                FROM public.master_data_import_batches AS batch
                WHERE batch.school_id = NEW.school_id
                  AND batch.id = NEW.batch_id;
                IF v_batch_status IN ('applied', 'published') THEN
                    RETURN NEW;
                END IF;
                RAISE EXCEPTION 'school fact unit transition requires terminal batch'
                    USING ERRCODE = '23514';
            END IF;

            PERFORM app_private.verify_master_data_import_terminal_consistency(
                NEW.school_id,
                NEW.batch_id
            );
            IF (OLD.school_id, OLD.batch_id)
               IS DISTINCT FROM (NEW.school_id, NEW.batch_id) THEN
                PERFORM app_private.verify_master_data_import_terminal_consistency(
                    OLD.school_id,
                    OLD.batch_id
                );
            END IF;
            RETURN NEW;
        END
        $function$
        """
    )
    op.execute(
        "REVOKE ALL ON FUNCTION "
        "app_private.enforce_school_fact_unit_update_terminal_consistency() FROM PUBLIC"
    )
    op.execute(
        """
        CREATE CONSTRAINT TRIGGER
            verify_master_data_import_units_update_terminal_consistency
        AFTER UPDATE ON public.master_data_import_units
        DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
        EXECUTE FUNCTION
            app_private.enforce_school_fact_unit_update_terminal_consistency()
        """
    )
    op.drop_constraint(
        "ck_master_data_import_command_outcomes_unit_limits",
        "master_data_import_command_outcomes",
        type_="check",
    )
    op.create_check_constraint(
        "ck_master_data_import_command_outcomes_unit_limits",
        "master_data_import_command_outcomes",
        "cardinality(succeeded_units) <= 50000 "
        "AND cardinality(failed_units) <= 50000 "
        "AND cardinality(executed_units) <= 50000",
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            r"""
            DO $downgrade$
            BEGIN
                IF EXISTS (
                    SELECT 1
                    FROM public.master_data_import_command_outcomes
                    WHERE pg_catalog.cardinality(succeeded_units) > 10000
                       OR pg_catalog.cardinality(failed_units) > 10000
                       OR pg_catalog.cardinality(executed_units) > 10000
                ) THEN
                    RAISE EXCEPTION USING
                        ERRCODE = '55000',
                        MESSAGE = 'revision 17 contains outcomes larger '
                            || 'than revision 16 can represent';
                END IF;
            END
            $downgrade$;
            """
        )
    )
    op.execute("SET LOCAL ROLE campus_ai_db_owner")
    op.execute(
        "DROP TRIGGER verify_master_data_import_units_insert_terminal_consistency "
        "ON public.master_data_import_units"
    )
    op.execute(
        "DROP TRIGGER verify_master_data_import_units_update_terminal_consistency "
        "ON public.master_data_import_units"
    )
    op.execute("DROP FUNCTION app_private.enforce_school_fact_unit_update_terminal_consistency()")
    op.execute(
        "CREATE CONSTRAINT TRIGGER verify_master_data_import_units_terminal_consistency "
        "AFTER INSERT OR UPDATE ON public.master_data_import_units "
        "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW "
        "EXECUTE FUNCTION app_private.enforce_master_data_import_terminal_consistency()"
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
