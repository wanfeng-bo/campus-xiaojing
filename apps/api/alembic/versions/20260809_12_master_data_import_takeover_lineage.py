"""Allow fully proven takeover-to-takeover lineage and fixed recovery reads."""

from __future__ import annotations

from collections.abc import Iterable

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "20260809_12"
down_revision = "20260809_11"
branch_labels = None
depends_on = None

_AUDIT_ARGUMENTS = (
    "integer,uuid,uuid,uuid,bigint,text,text,text,uuid,uuid,uuid,jsonb,jsonb,uuid,text"
)
_SUPERSEDE_ARGUMENTS = "uuid,uuid,uuid,text,bigint"
_ASSERT_ARGUMENTS = "uuid,uuid,uuid"
_LOCK_SOURCE_ARGUMENTS = "uuid,uuid,uuid"
_LOCK_ASSIGNMENT_ARGUMENTS = "uuid,uuid,uuid,uuid"
_CHAIN_ARGUMENTS = "uuid,uuid,uuid,bigint"
_LINEAGE_ARGUMENTS = "uuid,uuid"
_SUCCEEDED_ATTEMPT_ARGUMENTS = "uuid,uuid,uuid"
_LOCK_SOURCE_SIGNATURE = f"app.lock_master_data_import_takeover_source({_LOCK_SOURCE_ARGUMENTS})"
_ASSERT_SIGNATURE = f"app.assert_master_data_import_takeover_integrity({_ASSERT_ARGUMENTS})"
_SUPERSEDE_SIGNATURE = f"app.supersede_master_data_import_takeover_source({_SUPERSEDE_ARGUMENTS})"
_LINEAGE_SIGNATURE = f"app.read_master_data_import_attempt_lineage({_LINEAGE_ARGUMENTS})"
_SUCCEEDED_ATTEMPT_SIGNATURE = (
    f"app.read_master_data_import_succeeded_attempt({_SUCCEEDED_ATTEMPT_ARGUMENTS})"
)
_AUDIT_CHAIN_SIGNATURE = (
    f"app_private.master_data_import_takeover_chain_is_valid_for_audit({_CHAIN_ARGUMENTS})"
)
_SNAPSHOT_POPULATOR_SIGNATURE = "app_private.populate_master_data_import_unit_outcome_snapshot()"
_OUTCOME_MUTATION_REJECTOR_SIGNATURE = "app_private.reject_master_data_import_outcome_mutation()"


def _replace_saved_function(
    *,
    signature: str,
    save_statements: Iterable[str],
    replacements: Iterable[tuple[str, str, int]],
) -> None:
    """Save the exact revision-11 blob, then install a narrowly transformed copy."""

    definition = (
        op.get_bind()
        .exec_driver_sql(
            "SELECT pg_catalog.pg_get_functiondef(%s::pg_catalog.regprocedure)",
            (signature,),
        )
        .scalar_one()
    )
    if not isinstance(definition, str):
        raise RuntimeError(f"cannot read revision-11 function definition: {signature}")
    transformed = definition
    for source, replacement, expected_count in replacements:
        actual_count = transformed.count(source)
        if actual_count != expected_count:
            raise RuntimeError(
                f"revision-11 function shape changed for {signature}: "
                f"expected {expected_count}, found {actual_count}"
            )
        transformed = transformed.replace(source, replacement)
    for statement in save_statements:
        op.execute(statement)
    op.execute(transformed)


def _create_unit_outcome_snapshot_boundary() -> None:
    """冻结新 ordinary outcome 的逐单元事实；旧行保持无证据并失败关闭。"""

    op.execute("SET LOCAL ROLE campus_ai_db_owner")
    op.add_column(
        "master_data_import_command_outcomes",
        sa.Column("unit_outcome_snapshots", postgresql.JSONB(), nullable=True),
        schema="public",
    )
    op.create_check_constraint(
        "ck_master_data_import_command_outcomes_unit_snapshots_array",
        "master_data_import_command_outcomes",
        "unit_outcome_snapshots IS NULL OR jsonb_typeof(unit_outcome_snapshots) = 'array'",
        schema="public",
    )
    op.execute("GRANT USAGE, CREATE ON SCHEMA app_private TO campus_ai_takeover_definer")
    op.execute(
        "GRANT SELECT (school_id, batch_id, sequence, status, version, target_id, "
        "target_version, error_code, error_field_code, last_attempt_completed) "
        "ON TABLE public.master_data_import_units TO campus_ai_takeover_definer"
    )
    op.execute("SET LOCAL ROLE campus_ai_takeover_definer")
    op.execute(
        """
        CREATE FUNCTION app_private.populate_master_data_import_unit_outcome_snapshot()
        RETURNS trigger
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog
        AS $function$
        DECLARE
            v_snapshot_count bigint;
            v_snapshots jsonb;
        BEGIN
            IF NEW.unit_outcome_snapshots IS NOT NULL THEN
                RAISE EXCEPTION 'unit outcome snapshot is database generated'
                    USING ERRCODE = '22023';
            END IF;
            IF NEW.action_code NOT IN (
                'master_data.import.ordinary_confirm',
                'master_data.import.ordinary_resume',
                'master_data.import.ordinary_takeover'
            ) THEN
                RETURN NEW;
            END IF;
            IF pg_catalog.cardinality(NEW.executed_units) <>
               pg_catalog.cardinality(ARRAY(
                      SELECT DISTINCT sequence
                      FROM pg_catalog.unnest(NEW.executed_units) AS item(sequence)
               )) THEN
                RAISE EXCEPTION 'ordinary outcome executed sequence set is invalid'
                    USING ERRCODE = '23514';
            END IF;
            SELECT pg_catalog.count(*),
                   COALESCE(
                       pg_catalog.jsonb_agg(
                           pg_catalog.jsonb_build_object(
                               'sequence', unit.sequence,
                               'status', unit.status,
                               'version', unit.version,
                               'target_id', unit.target_id::text,
                               'target_version', unit.target_version,
                               'error_code', unit.error_code,
                               'error_field_code', unit.error_field_code
                           ) ORDER BY unit.sequence
                       ),
                       '[]'::jsonb
                   )
            INTO v_snapshot_count, v_snapshots
            FROM public.master_data_import_units AS unit
            WHERE unit.school_id = NEW.school_id
              AND unit.batch_id = NEW.batch_id
              AND unit.sequence = ANY(NEW.executed_units)
              AND unit.last_attempt_completed IS TRUE;
            IF v_snapshot_count <> pg_catalog.cardinality(NEW.executed_units)
               OR EXISTS (
                    SELECT 1
                    FROM public.master_data_import_units AS unit
                    WHERE unit.school_id = NEW.school_id
                      AND unit.batch_id = NEW.batch_id
                      AND unit.sequence = ANY(NEW.executed_units)
                      AND (
                          (unit.status = 'applied'
                           AND NOT unit.sequence = ANY(NEW.succeeded_units))
                          OR (unit.status = 'failed'
                              AND NOT unit.sequence = ANY(NEW.failed_units))
                          OR unit.status NOT IN ('applied', 'failed')
                      )
               ) THEN
                RAISE EXCEPTION 'ordinary outcome unit snapshot is incomplete'
                    USING ERRCODE = '23514';
            END IF;
            NEW.unit_outcome_snapshots := v_snapshots;
            RETURN NEW;
        END
        $function$
        """
    )
    op.execute(
        """
        CREATE FUNCTION app_private.reject_master_data_import_outcome_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog
        AS $function$
        BEGIN
            RAISE EXCEPTION 'master data import outcome is immutable'
                USING ERRCODE = '55000';
        END
        $function$
        """
    )
    for signature in (
        _SNAPSHOT_POPULATOR_SIGNATURE,
        _OUTCOME_MUTATION_REJECTOR_SIGNATURE,
    ):
        op.execute(
            f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC, campus_ai_runtime, "
            "campus_ai_db_owner, campus_ai_context_definer, campus_ai_audit_definer, "
            "campus_ai_outbox_probe_definer, campus_ai_migration"
        )
        op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO campus_ai_takeover_definer")
        op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO campus_ai_db_owner")
    op.execute("SET LOCAL ROLE campus_ai_db_owner")
    op.execute(
        "CREATE TRIGGER populate_master_data_import_unit_outcome_snapshot "
        "BEFORE INSERT ON public.master_data_import_command_outcomes FOR EACH ROW "
        "EXECUTE FUNCTION app_private.populate_master_data_import_unit_outcome_snapshot()"
    )
    op.execute(
        "CREATE TRIGGER reject_master_data_import_outcome_row_mutation "
        "BEFORE UPDATE OR DELETE ON public.master_data_import_command_outcomes FOR EACH ROW "
        "EXECUTE FUNCTION app_private.reject_master_data_import_outcome_mutation()"
    )
    op.execute(
        "CREATE TRIGGER reject_master_data_import_outcome_truncate "
        "BEFORE TRUNCATE ON public.master_data_import_command_outcomes FOR EACH STATEMENT "
        "EXECUTE FUNCTION app_private.reject_master_data_import_outcome_mutation()"
    )
    op.execute("SET LOCAL ROLE campus_ai_takeover_definer")
    for signature in (
        _SNAPSHOT_POPULATOR_SIGNATURE,
        _OUTCOME_MUTATION_REJECTOR_SIGNATURE,
    ):
        op.execute(f"REVOKE EXECUTE ON FUNCTION {signature} FROM campus_ai_db_owner")
    op.execute("SET LOCAL ROLE campus_ai_db_owner")
    op.execute("REVOKE CREATE ON SCHEMA app_private FROM campus_ai_takeover_definer")


def _create_chain_validator_and_reader() -> None:
    """Create one private full-chain validator and one fixed same-school read boundary."""

    op.execute("SET LOCAL ROLE campus_ai_db_owner")
    op.execute("GRANT USAGE, CREATE ON SCHEMA app_private TO campus_ai_takeover_definer")
    op.execute(
        "GRANT SELECT (school_id, idempotency_record_id, batch_id, action_code, status, "
        "batch_version, succeeded_units, failed_units, executed_units, "
        "unit_outcome_snapshots) ON TABLE "
        "public.master_data_import_command_outcomes TO campus_ai_takeover_definer"
    )
    op.execute("SET LOCAL ROLE campus_ai_takeover_definer")
    op.execute(
        """
        CREATE FUNCTION app_private.master_data_import_takeover_chain_is_valid(
            p_school_id uuid,
            p_batch_id uuid,
            p_new_record_id uuid,
            p_next_batch_version_before bigint DEFAULT NULL
        )
        RETURNS boolean
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog
        AS $function$
        DECLARE
            v_current_new_record_id uuid := p_new_record_id;
            v_next_batch_version_before bigint := p_next_batch_version_before;
            v_seen uuid[] := ARRAY[]::uuid[];
            v_edge record;
            v_old record;
            v_new record;
            v_completed_units bigint;
            v_audit_count bigint;
        BEGIN
            IF p_school_id IS NULL OR p_batch_id IS NULL OR p_new_record_id IS NULL THEN
                RETURN false;
            END IF;
            LOOP
                IF v_current_new_record_id = ANY(v_seen) THEN
                    RETURN false;
                END IF;
                v_seen := pg_catalog.array_append(v_seen, v_current_new_record_id);

                SELECT takeover.id,
                       takeover.previous_idempotency_record_id,
                       takeover.new_idempotency_record_id,
                       takeover.original_actor_user_id,
                       takeover.original_actor_person_id,
                       takeover.original_actor_assignment_id,
                       takeover.original_actor_assignment_version,
                       takeover.new_actor_user_id,
                       takeover.new_actor_person_id,
                       takeover.new_actor_assignment_id,
                       takeover.new_actor_assignment_version,
                       takeover.request_id,
                       takeover.batch_version_before,
                       takeover.batch_version_after,
                       takeover.transferred_sequences,
                       takeover.transferred_unit_versions
                INTO v_edge
                FROM public.master_data_import_takeovers AS takeover
                WHERE takeover.school_id = p_school_id
                  AND takeover.batch_id = p_batch_id
                  AND takeover.new_idempotency_record_id = v_current_new_record_id;
                IF NOT FOUND THEN
                    RETURN false;
                END IF;
                SELECT record.id,
                       record.action_code,
                       record.actor_user_id,
                       record.actor_person_id,
                       record.actor_context_type,
                       record.actor_assignment_id,
                       record.actor_assignment_version,
                       record.resource_type,
                       record.resource_id,
                       record.status,
                       record.result_type,
                       record.result_payload
                INTO v_old
                FROM public.command_idempotency_records AS record
                WHERE record.school_id = p_school_id
                  AND record.id = v_edge.previous_idempotency_record_id;
                IF NOT FOUND THEN
                    RETURN false;
                END IF;
                SELECT record.id,
                       record.action_code,
                       record.actor_user_id,
                       record.actor_person_id,
                       record.actor_context_type,
                       record.actor_assignment_id,
                       record.actor_assignment_version,
                       record.resource_type,
                       record.resource_id,
                       record.idempotency_key_hash,
                       record.status
                INTO v_new
                FROM public.command_idempotency_records AS record
                WHERE record.school_id = p_school_id
                  AND record.id = v_edge.new_idempotency_record_id;
                IF NOT FOUND THEN
                    RETURN false;
                END IF;

                IF v_edge.previous_idempotency_record_id = v_edge.new_idempotency_record_id
                   OR v_edge.batch_version_after <> v_edge.batch_version_before + 1
                   OR v_edge.original_actor_user_id <> v_old.actor_user_id
                   OR v_edge.original_actor_person_id <> v_old.actor_person_id
                   OR v_edge.original_actor_assignment_id
                        IS DISTINCT FROM v_old.actor_assignment_id
                   OR v_edge.original_actor_assignment_version
                        IS DISTINCT FROM v_old.actor_assignment_version
                   OR v_edge.new_actor_user_id <> v_new.actor_user_id
                   OR v_edge.new_actor_person_id <> v_new.actor_person_id
                   OR v_edge.new_actor_assignment_id
                        IS DISTINCT FROM v_new.actor_assignment_id
                   OR v_edge.new_actor_assignment_version
                        IS DISTINCT FROM v_new.actor_assignment_version
                   OR v_old.actor_context_type <> 'assignment'
                   OR v_old.resource_type <> 'master_data.import_batch'
                   OR v_old.resource_id <> p_batch_id::text
                   OR v_old.status <> 'terminal_failure'
                   OR v_old.result_type <> 'master_data.import_outcome'
                   OR v_old.result_payload <> pg_catalog.jsonb_build_object(
                        'resource_type', 'master_data.import_batch',
                        'resource_id', p_batch_id::text,
                        'resource_version', v_edge.batch_version_before,
                        'outcome_code', 'master_data.import.command_superseded'
                   )
                   OR v_new.action_code <> 'master_data.import.ordinary_takeover'
                   OR v_new.actor_context_type <> 'assignment'
                   OR v_new.resource_type <> 'master_data.import_batch'
                   OR v_new.resource_id <> p_batch_id::text
                   OR v_new.status NOT IN ('in_progress', 'succeeded', 'terminal_failure')
                   OR pg_catalog.cardinality(v_edge.transferred_sequences) < 1
                   OR pg_catalog.cardinality(v_edge.transferred_sequences)
                        <> pg_catalog.cardinality(v_edge.transferred_unit_versions)
                   OR v_edge.transferred_sequences <> ARRAY(
                        SELECT DISTINCT sequence
                        FROM pg_catalog.unnest(v_edge.transferred_sequences)
                            AS item(sequence)
                        ORDER BY sequence
                   )
                   OR EXISTS (
                        SELECT 1
                        FROM pg_catalog.unnest(v_edge.transferred_unit_versions)
                            AS item(version)
                        WHERE version < 1
                   ) THEN
                    RETURN false;
                END IF;

                SELECT pg_catalog.count(*) INTO v_audit_count
                FROM public.audit_logs AS audit
                WHERE audit.school_id = p_school_id
                  AND audit.event_type = 'master_data.import.taken_over'
                  AND audit.target_type = 'master_data_import_takeover'
                  AND audit.target_id = v_edge.id::text
                  AND audit.request_id = v_edge.request_id::text
                  AND audit.actor_user_id = v_edge.new_actor_user_id
                  AND audit.actor_person_id = v_edge.new_actor_person_id
                  AND audit.actor_assignment_id = v_edge.new_actor_assignment_id
                  AND audit.actor_assignment_version = v_edge.new_actor_assignment_version
                  AND audit.actor_context_type = 'assignment'
                  AND audit.idempotency_key_hash = v_new.idempotency_key_hash
                  AND audit.reason = 'manual.ordinary-import-taken-over';
                IF v_audit_count <> 1 THEN
                    RETURN false;
                END IF;

                IF v_next_batch_version_before IS NOT NULL THEN
                    SELECT pg_catalog.count(*) INTO v_completed_units
                    FROM public.master_data_import_units AS unit
                    WHERE unit.school_id = p_school_id
                      AND unit.batch_id = p_batch_id
                      AND unit.last_attempt_idempotency_record_id =
                          v_current_new_record_id
                      AND unit.last_attempt_completed IS TRUE
                      AND unit.status IN ('applied', 'failed');
                    IF v_next_batch_version_before
                       <> v_edge.batch_version_after + v_completed_units THEN
                        RETURN false;
                    END IF;
                END IF;

                IF v_old.action_code IN (
                    'master_data.import.ordinary_confirm',
                    'master_data.import.ordinary_resume'
                ) THEN
                    RETURN true;
                END IF;
                IF v_old.action_code <> 'master_data.import.ordinary_takeover' THEN
                    RETURN false;
                END IF;
                v_next_batch_version_before := v_edge.batch_version_before;
                v_current_new_record_id := v_edge.previous_idempotency_record_id;
            END LOOP;
        END
        $function$
        """
    )
    op.execute(
        "REVOKE ALL ON FUNCTION "
        f"app_private.master_data_import_takeover_chain_is_valid({_CHAIN_ARGUMENTS}) "
        "FROM PUBLIC, campus_ai_runtime, campus_ai_db_owner, campus_ai_context_definer, "
        "campus_ai_audit_definer, campus_ai_outbox_probe_definer, campus_ai_migration"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION "
        f"app_private.master_data_import_takeover_chain_is_valid({_CHAIN_ARGUMENTS}) "
        "TO campus_ai_takeover_definer"
    )
    op.execute(
        """
        CREATE FUNCTION app_private.master_data_import_takeover_chain_is_valid_for_audit(
            p_school_id uuid,
            p_batch_id uuid,
            p_new_record_id uuid,
            p_next_batch_version_before bigint
        )
        RETURNS boolean
        LANGUAGE sql
        SECURITY DEFINER
        SET search_path = pg_catalog
        AS $function$
            SELECT app_private.master_data_import_takeover_chain_is_valid(
                p_school_id,
                p_batch_id,
                p_new_record_id,
                p_next_batch_version_before
            )
        $function$
        """
    )
    op.execute(
        f"REVOKE ALL ON FUNCTION {_AUDIT_CHAIN_SIGNATURE} "
        "FROM PUBLIC, campus_ai_runtime, campus_ai_db_owner, campus_ai_context_definer, "
        "campus_ai_audit_definer, campus_ai_outbox_probe_definer, campus_ai_migration"
    )
    op.execute(f"GRANT EXECUTE ON FUNCTION {_AUDIT_CHAIN_SIGNATURE} TO campus_ai_audit_definer")

    op.execute("SET LOCAL ROLE campus_ai_db_owner")
    op.execute("GRANT CREATE ON SCHEMA app TO campus_ai_takeover_definer")
    op.execute("SET LOCAL ROLE campus_ai_takeover_definer")
    op.execute(
        """
        CREATE FUNCTION app.read_master_data_import_attempt_lineage(
            p_school_id uuid,
            p_new_record_id uuid
        )
        RETURNS TABLE(record_id uuid)
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog
        AS $function$
        DECLARE
            v_school_id uuid := app.require_school_context();
            v_batch_id uuid;
        BEGIN
            IF p_school_id IS DISTINCT FROM v_school_id THEN
                RAISE EXCEPTION 'takeover lineage school does not match signed context'
                    USING ERRCODE = '28000';
            END IF;
            SELECT takeover.batch_id INTO v_batch_id
            FROM public.master_data_import_takeovers AS takeover
            WHERE takeover.school_id = v_school_id
              AND takeover.new_idempotency_record_id = p_new_record_id;
            IF v_batch_id IS NULL OR NOT app_private.master_data_import_takeover_chain_is_valid(
                v_school_id, v_batch_id, p_new_record_id, NULL
            ) THEN
                RAISE EXCEPTION 'takeover lineage durable provenance is incomplete'
                    USING ERRCODE = '23514';
            END IF;
            RETURN QUERY
            WITH RECURSIVE lineage AS (
                SELECT takeover.previous_idempotency_record_id AS previous_record_id,
                       1::bigint AS depth,
                       ARRAY[p_new_record_id, takeover.previous_idempotency_record_id]
                           AS path
                FROM public.master_data_import_takeovers AS takeover
                WHERE takeover.school_id = v_school_id
                  AND takeover.batch_id = v_batch_id
                  AND takeover.new_idempotency_record_id = p_new_record_id
                UNION ALL
                SELECT takeover.previous_idempotency_record_id,
                       lineage.depth + 1,
                       pg_catalog.array_append(
                           lineage.path, takeover.previous_idempotency_record_id
                       )
                FROM lineage
                JOIN public.master_data_import_takeovers AS takeover
                  ON takeover.school_id = v_school_id
                 AND takeover.batch_id = v_batch_id
                 AND takeover.new_idempotency_record_id = lineage.previous_record_id
                WHERE NOT takeover.previous_idempotency_record_id = ANY(lineage.path)
            )
            SELECT lineage.previous_record_id
            FROM lineage
            ORDER BY lineage.depth DESC;
        END
        $function$
        """
    )
    op.execute(f"REVOKE ALL ON FUNCTION {_LINEAGE_SIGNATURE} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {_LINEAGE_SIGNATURE} TO campus_ai_runtime")
    op.execute(
        """
        CREATE FUNCTION app.read_master_data_import_succeeded_attempt(
            p_school_id uuid,
            p_batch_id uuid,
            p_idempotency_record_id uuid
        )
        RETURNS TABLE(
            idempotency_record_id uuid,
            batch_id uuid,
            action_code text,
            status text,
            batch_version bigint,
            succeeded_units integer[],
            failed_units integer[],
            executed_units integer[],
            unit_outcome_snapshots jsonb
        )
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog
        AS $function$
        DECLARE
            v_school_id uuid := app.require_school_context();
        BEGIN
            IF p_school_id IS DISTINCT FROM v_school_id THEN
                RAISE EXCEPTION 'historical import attempt school does not match signed context'
                    USING ERRCODE = '28000';
            END IF;
            RETURN QUERY
            SELECT outcome.idempotency_record_id,
                   outcome.batch_id,
                   outcome.action_code::text,
                   outcome.status::text,
                   outcome.batch_version,
                   outcome.succeeded_units,
                   outcome.failed_units,
                   outcome.executed_units,
                   outcome.unit_outcome_snapshots
            FROM public.command_idempotency_records AS record
            JOIN public.master_data_import_command_outcomes AS outcome
              ON outcome.school_id = record.school_id
             AND outcome.idempotency_record_id = record.id
            WHERE record.school_id = v_school_id
              AND record.id = p_idempotency_record_id
              AND outcome.batch_id = p_batch_id
              AND record.status = 'succeeded'
              AND record.action_code = outcome.action_code
              AND record.action_code IN (
                  'master_data.import.ordinary_confirm',
                  'master_data.import.ordinary_resume',
                  'master_data.import.ordinary_takeover'
              )
              AND record.resource_type = 'master_data.import_batch'
              AND record.resource_id = p_batch_id::text
              AND record.result_type = 'master_data.import_outcome'
              AND record.result_payload = pg_catalog.jsonb_build_object(
                  'resource_type', 'master_data.import_batch',
                  'resource_id', p_batch_id::text,
                  'resource_version', outcome.batch_version,
                  'outcome_code', outcome.status
              )
              AND outcome.unit_outcome_snapshots IS NOT NULL
              AND pg_catalog.jsonb_typeof(outcome.unit_outcome_snapshots) = 'array'
              AND pg_catalog.jsonb_array_length(outcome.unit_outcome_snapshots) =
                  pg_catalog.cardinality(outcome.executed_units)
              AND (
                  record.action_code <> 'master_data.import.ordinary_takeover'
                  OR app_private.master_data_import_takeover_chain_is_valid(
                      v_school_id,
                      p_batch_id,
                      record.id,
                      NULL
                  )
              );
        END
        $function$
        """
    )
    op.execute(f"REVOKE ALL ON FUNCTION {_SUCCEEDED_ATTEMPT_SIGNATURE} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {_SUCCEEDED_ATTEMPT_SIGNATURE} TO campus_ai_runtime")
    op.execute("SET LOCAL ROLE campus_ai_db_owner")
    op.execute("REVOKE CREATE ON SCHEMA app, app_private FROM campus_ai_takeover_definer")


def _upgrade_takeover_functions() -> None:
    old_action = """old_record.action_code IN (
                  'master_data.import.ordinary_confirm',
                  'master_data.import.ordinary_resume'
              )"""
    expanded_old_action = """(
                  old_record.action_code IN (
                      'master_data.import.ordinary_confirm',
                      'master_data.import.ordinary_resume'
                  )
                  OR (
                      old_record.action_code = 'master_data.import.ordinary_takeover'
                      AND app_private.master_data_import_takeover_chain_is_valid(
                          takeover.school_id,
                          takeover.batch_id,
                          old_record.id,
                          takeover.batch_version_before
                      )
                  )
              )"""
    op.execute("SET LOCAL ROLE campus_ai_db_owner")
    op.execute("GRANT CREATE ON SCHEMA app TO campus_ai_takeover_definer")
    op.execute("GRANT USAGE, CREATE ON SCHEMA app_private TO campus_ai_takeover_definer")
    op.execute("SET LOCAL ROLE campus_ai_takeover_definer")
    lock_action = """old_record.action_code IN (
                  'master_data.import.ordinary_confirm',
                  'master_data.import.ordinary_resume'
              )"""
    expanded_lock_action = """(
                  old_record.action_code IN (
                      'master_data.import.ordinary_confirm',
                      'master_data.import.ordinary_resume'
                  )
                  OR (
                      old_record.action_code = 'master_data.import.ordinary_takeover'
                      AND app_private.master_data_import_takeover_chain_is_valid(
                          v_school_id,
                          p_batch_id,
                          old_record.id,
                          batch.version
                      )
                  )
              )"""
    _replace_saved_function(
        signature=_LOCK_SOURCE_SIGNATURE,
        save_statements=(
            "ALTER FUNCTION app.lock_master_data_import_takeover_source"
            f"({_LOCK_SOURCE_ARGUMENTS}) RENAME TO "
            "lock_master_data_import_takeover_source_revision_11",
            "ALTER FUNCTION app.lock_master_data_import_takeover_source_revision_11"
            f"({_LOCK_SOURCE_ARGUMENTS}) SET SCHEMA app_private",
        ),
        replacements=((lock_action, expanded_lock_action, 1),),
    )
    op.execute(f"REVOKE ALL ON FUNCTION {_LOCK_SOURCE_SIGNATURE} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {_LOCK_SOURCE_SIGNATURE} TO campus_ai_runtime")
    _replace_saved_function(
        signature=(
            f"app.lock_master_data_import_takeover_assignment({_LOCK_ASSIGNMENT_ARGUMENTS})"
        ),
        save_statements=(
            "ALTER FUNCTION app.lock_master_data_import_takeover_assignment"
            f"({_LOCK_ASSIGNMENT_ARGUMENTS}) RENAME TO "
            "lock_master_data_import_takeover_assignment_revision_11",
            "ALTER FUNCTION app.lock_master_data_import_takeover_assignment_revision_11"
            f"({_LOCK_ASSIGNMENT_ARGUMENTS}) SET SCHEMA app_private",
        ),
        replacements=((lock_action, expanded_lock_action, 1),),
    )
    op.execute(
        "REVOKE ALL ON FUNCTION app.lock_master_data_import_takeover_assignment"
        f"({_LOCK_ASSIGNMENT_ARGUMENTS}) FROM PUBLIC"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION app.lock_master_data_import_takeover_assignment"
        f"({_LOCK_ASSIGNMENT_ARGUMENTS}) TO campus_ai_runtime"
    )
    _replace_saved_function(
        signature=_ASSERT_SIGNATURE,
        save_statements=(
            "ALTER FUNCTION app.assert_master_data_import_takeover_integrity"
            f"({_ASSERT_ARGUMENTS}) RENAME TO "
            "assert_master_data_import_takeover_integrity_revision_11",
            "ALTER FUNCTION app.assert_master_data_import_takeover_integrity_revision_11"
            f"({_ASSERT_ARGUMENTS}) SET SCHEMA app_private",
        ),
        replacements=((old_action, expanded_old_action, 1),),
    )
    op.execute(f"REVOKE ALL ON FUNCTION {_ASSERT_SIGNATURE} FROM PUBLIC")

    source_action = """old_record.action_code IN (
                      'master_data.import.ordinary_confirm',
                      'master_data.import.ordinary_resume'
                  )"""
    expanded_source_action = """(
                      old_record.action_code IN (
                          'master_data.import.ordinary_confirm',
                          'master_data.import.ordinary_resume'
                      )
                      OR (
                          old_record.action_code = 'master_data.import.ordinary_takeover'
                          AND app_private.master_data_import_takeover_chain_is_valid(
                              v_school_id,
                              p_batch_id,
                              old_record.id,
                              p_batch_version_before
                          )
                      )
                  )"""
    update_action = """old_record.action_code IN (
                  'master_data.import.ordinary_confirm',
                  'master_data.import.ordinary_resume'
              )"""
    expanded_update_action = """(
                  old_record.action_code IN (
                      'master_data.import.ordinary_confirm',
                      'master_data.import.ordinary_resume'
                  )
                  OR (
                      old_record.action_code = 'master_data.import.ordinary_takeover'
                      AND app_private.master_data_import_takeover_chain_is_valid(
                          v_school_id,
                          p_batch_id,
                          old_record.id,
                          p_batch_version_before
                      )
                  )
              )"""
    _replace_saved_function(
        signature=_SUPERSEDE_SIGNATURE,
        save_statements=(
            "ALTER FUNCTION app.supersede_master_data_import_takeover_source"
            f"({_SUPERSEDE_ARGUMENTS}) RENAME TO "
            "supersede_master_data_import_takeover_source_revision_11",
            "ALTER FUNCTION app.supersede_master_data_import_takeover_source_revision_11"
            f"({_SUPERSEDE_ARGUMENTS}) SET SCHEMA app_private",
        ),
        replacements=(
            (source_action, expanded_source_action, 1),
            (update_action, expanded_update_action, 1),
        ),
    )
    op.execute(f"REVOKE ALL ON FUNCTION {_SUPERSEDE_SIGNATURE} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {_SUPERSEDE_SIGNATURE} TO campus_ai_runtime")

    op.execute("SET LOCAL ROLE campus_ai_db_owner")
    for trigger_name, table_name in (
        ("enforce_master_data_import_takeover_insert_integrity", "master_data_import_takeovers"),
        ("enforce_master_data_import_takeover_batch_integrity", "master_data_import_batches"),
        ("enforce_master_data_import_takeover_command_integrity", "command_idempotency_records"),
    ):
        op.execute(f"DROP TRIGGER {trigger_name} ON public.{table_name}")
    trigger_actions = """NEW.action_code NOT IN (
                          'master_data.import.ordinary_confirm',
                          'master_data.import.ordinary_resume'
                      )"""
    expanded_trigger_actions = """NEW.action_code NOT IN (
                          'master_data.import.ordinary_confirm',
                          'master_data.import.ordinary_resume',
                          'master_data.import.ordinary_takeover'
                      )"""
    trigger_edge = """(
                      takeover.previous_idempotency_record_id = NEW.id
                      OR takeover.new_idempotency_record_id = NEW.id
                  )"""
    exact_trigger_edge = """(
                      (TG_OP = 'INSERT' AND takeover.new_idempotency_record_id = NEW.id)
                      OR (
                          TG_OP = 'UPDATE'
                          AND takeover.previous_idempotency_record_id = NEW.id
                      )
                  )"""
    op.execute("SET LOCAL ROLE campus_ai_takeover_definer")
    _replace_saved_function(
        signature="app_private.enforce_master_data_import_takeover_integrity()",
        save_statements=(
            "ALTER FUNCTION app_private.enforce_master_data_import_takeover_integrity() "
            "RENAME TO enforce_master_data_import_takeover_integrity_revision_11",
        ),
        replacements=(
            (trigger_actions, expanded_trigger_actions, 1),
            (trigger_edge, exact_trigger_edge, 1),
        ),
    )
    op.execute(
        "REVOKE ALL ON FUNCTION app_private.enforce_master_data_import_takeover_integrity() "
        "FROM PUBLIC"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION app_private.enforce_master_data_import_takeover_integrity() "
        "TO campus_ai_db_owner"
    )

    op.execute("SET LOCAL ROLE campus_ai_db_owner")
    op.execute(
        "CREATE CONSTRAINT TRIGGER enforce_master_data_import_takeover_insert_integrity "
        "AFTER INSERT ON public.master_data_import_takeovers "
        "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW "
        "EXECUTE FUNCTION app_private.enforce_master_data_import_takeover_integrity()"
    )
    op.execute(
        "CREATE CONSTRAINT TRIGGER enforce_master_data_import_takeover_batch_integrity "
        "AFTER INSERT OR UPDATE ON public.master_data_import_batches "
        "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW "
        "EXECUTE FUNCTION app_private.enforce_master_data_import_takeover_integrity()"
    )
    op.execute(
        "CREATE CONSTRAINT TRIGGER enforce_master_data_import_takeover_command_integrity "
        "AFTER INSERT OR UPDATE ON public.command_idempotency_records "
        "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW "
        "EXECUTE FUNCTION app_private.enforce_master_data_import_takeover_integrity()"
    )
    op.execute("SET LOCAL ROLE campus_ai_takeover_definer")
    op.execute(
        "REVOKE EXECUTE ON FUNCTION app_private.enforce_master_data_import_takeover_integrity() "
        "FROM campus_ai_db_owner"
    )
    op.execute("SET LOCAL ROLE campus_ai_db_owner")
    op.execute("REVOKE CREATE ON SCHEMA app, app_private FROM campus_ai_takeover_definer")
    op.execute("GRANT USAGE ON SCHEMA app_private TO campus_ai_takeover_definer")


def _upgrade_audit_function() -> None:
    audit_action = """previous_record.action_code IN (
                      'master_data.import.ordinary_confirm',
                      'master_data.import.ordinary_resume'
                  )"""
    expanded_audit_action = """(
                      previous_record.action_code IN (
                          'master_data.import.ordinary_confirm',
                          'master_data.import.ordinary_resume'
                      )
                      OR (
                          previous_record.action_code =
                              'master_data.import.ordinary_takeover'
                          AND app_private.master_data_import_takeover_chain_is_valid_for_audit(
                              takeover.school_id,
                              takeover.batch_id,
                              previous_record.id,
                              takeover.batch_version_before
                          )
                      )
                  )"""
    op.execute("SET LOCAL ROLE campus_ai_db_owner")
    op.execute("GRANT CREATE ON SCHEMA app, app_private TO campus_ai_audit_definer")
    op.execute("SET LOCAL ROLE campus_ai_audit_definer")
    _replace_saved_function(
        signature=f"app.append_master_data_import_audit({_AUDIT_ARGUMENTS})",
        save_statements=(
            f"ALTER FUNCTION app.append_master_data_import_audit({_AUDIT_ARGUMENTS}) "
            "RENAME TO append_master_data_import_audit_revision_11",
            "ALTER FUNCTION app.append_master_data_import_audit_revision_11"
            f"({_AUDIT_ARGUMENTS}) SET SCHEMA app_private",
        ),
        replacements=((audit_action, expanded_audit_action, 1),),
    )
    op.execute(
        f"REVOKE ALL ON FUNCTION app.append_master_data_import_audit({_AUDIT_ARGUMENTS}) "
        "FROM PUBLIC"
    )
    op.execute(
        f"GRANT EXECUTE ON FUNCTION app.append_master_data_import_audit({_AUDIT_ARGUMENTS}) "
        "TO campus_ai_runtime, campus_ai_db_owner, campus_ai_takeover_definer"
    )
    op.execute("SET LOCAL ROLE campus_ai_db_owner")
    op.execute("REVOKE CREATE ON SCHEMA app, app_private FROM campus_ai_audit_definer")


def _validate_revision_11_history() -> None:
    """Reject partial revision-11 facts without changing any business row."""

    op.execute("SET LOCAL ROLE campus_ai_db_owner")
    for table_name in (
        "command_idempotency_records",
        "master_data_import_takeovers",
        "master_data_import_units",
    ):
        op.execute(f"ALTER TABLE public.{table_name} DISABLE ROW LEVEL SECURITY")
    op.execute("SET LOCAL ROLE campus_ai_takeover_definer")
    op.execute(
        """
        DO $preflight$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM public.master_data_import_takeovers AS takeover
                WHERE NOT app_private.master_data_import_takeover_chain_is_valid(
                    takeover.school_id,
                    takeover.batch_id,
                    takeover.new_idempotency_record_id,
                    NULL
                )
            ) THEN
                RAISE EXCEPTION 'revision 11 contains partial takeover lineage'
                    USING ERRCODE = '23514';
            END IF;
        END
        $preflight$
        """
    )
    op.execute("SET LOCAL ROLE campus_ai_db_owner")
    for table_name in (
        "command_idempotency_records",
        "master_data_import_takeovers",
        "master_data_import_units",
    ):
        op.execute(f"ALTER TABLE public.{table_name} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE public.{table_name} FORCE ROW LEVEL SECURITY")


def upgrade() -> None:
    """安装可验证的接管链边界；仅变更定义与 ACL，不改写任何业务事实。"""

    _create_unit_outcome_snapshot_boundary()
    _create_chain_validator_and_reader()
    _validate_revision_11_history()
    _upgrade_takeover_functions()
    _upgrade_audit_function()


def _validate_revision_11_downgrade() -> None:
    """Reject a downgrade when revision-12-only takeover facts already exist."""

    op.execute("SET LOCAL ROLE campus_ai_db_owner")
    op.execute(
        """
        DO $preflight$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM public.master_data_import_takeovers AS takeover
                JOIN public.command_idempotency_records AS previous_record
                  ON previous_record.school_id = takeover.school_id
                 AND previous_record.id = takeover.previous_idempotency_record_id
                WHERE previous_record.action_code =
                    'master_data.import.ordinary_takeover'
            ) THEN
                RAISE EXCEPTION
                    'revision 12 contains takeover lineage not representable by revision 11'
                    USING ERRCODE = '23514';
            END IF;
            IF EXISTS (
                SELECT 1
                FROM public.master_data_import_takeovers AS takeover
                JOIN public.command_idempotency_records AS new_record
                  ON new_record.school_id = takeover.school_id
                 AND new_record.id = takeover.new_idempotency_record_id
                JOIN public.master_data_import_batches AS batch
                  ON batch.school_id = takeover.school_id
                 AND batch.id = takeover.batch_id
                WHERE new_record.status <> 'in_progress'
                   OR new_record.result_type IS NOT NULL
                   OR new_record.result_payload IS NOT NULL
                   OR batch.status <> 'applying'
                   OR batch.active_idempotency_record_id
                        IS DISTINCT FROM takeover.new_idempotency_record_id
                   OR batch.active_action_code
                        IS DISTINCT FROM 'master_data.import.ordinary_takeover'
                   OR batch.version <> takeover.batch_version_after
                   OR EXISTS (
                        SELECT 1
                        FROM public.master_data_import_command_outcomes AS outcome
                        WHERE outcome.school_id = takeover.school_id
                          AND outcome.idempotency_record_id =
                              takeover.new_idempotency_record_id
                   )
                   OR EXISTS (
                        SELECT 1
                        FROM pg_catalog.generate_subscripts(
                            takeover.transferred_sequences, 1
                        ) AS manifest(position)
                        LEFT JOIN public.master_data_import_units AS unit
                          ON unit.school_id = takeover.school_id
                         AND unit.batch_id = takeover.batch_id
                         AND unit.sequence =
                             takeover.transferred_sequences[manifest.position]
                        WHERE unit.id IS NULL
                           OR unit.last_attempt_idempotency_record_id
                                IS DISTINCT FROM takeover.new_idempotency_record_id
                           OR unit.last_attempt_completed IS DISTINCT FROM false
                           OR unit.status NOT IN ('pending', 'ready', 'failed')
                           OR unit.version <>
                                takeover.transferred_unit_versions[manifest.position]
                   )
            ) THEN
                RAISE EXCEPTION
                    'revision 12 contains terminal takeover facts not representable by revision 11'
                    USING ERRCODE = '23514';
            END IF;
            IF EXISTS (
                SELECT 1
                FROM public.master_data_import_command_outcomes AS outcome
                WHERE outcome.unit_outcome_snapshots IS NOT NULL
            ) THEN
                RAISE EXCEPTION
                    'revision 12 contains immutable unit outcomes not representable by revision 11'
                    USING ERRCODE = '23514';
            END IF;
        END
        $preflight$
        """
    )


def downgrade() -> None:
    """仅在事实可由 revision 11 表达时恢复旧边界；预检失败即关闭且不改写业务数据。"""

    _validate_revision_11_downgrade()
    op.execute("SET LOCAL ROLE campus_ai_db_owner")
    for trigger_name, table_name in (
        ("enforce_master_data_import_takeover_insert_integrity", "master_data_import_takeovers"),
        ("enforce_master_data_import_takeover_batch_integrity", "master_data_import_batches"),
        ("enforce_master_data_import_takeover_command_integrity", "command_idempotency_records"),
    ):
        op.execute(f"DROP TRIGGER {trigger_name} ON public.{table_name}")

    op.execute("GRANT CREATE ON SCHEMA app TO campus_ai_takeover_definer")
    op.execute("GRANT USAGE, CREATE ON SCHEMA app_private TO campus_ai_takeover_definer")
    op.execute("SET LOCAL ROLE campus_ai_takeover_definer")
    op.execute(
        f"DROP FUNCTION app.lock_master_data_import_takeover_source({_LOCK_SOURCE_ARGUMENTS})"
    )
    op.execute(
        "ALTER FUNCTION app_private.lock_master_data_import_takeover_source_revision_11"
        f"({_LOCK_SOURCE_ARGUMENTS}) SET SCHEMA app"
    )
    op.execute(
        "ALTER FUNCTION app.lock_master_data_import_takeover_source_revision_11"
        f"({_LOCK_SOURCE_ARGUMENTS}) RENAME TO lock_master_data_import_takeover_source"
    )
    op.execute(f"REVOKE ALL ON FUNCTION {_LOCK_SOURCE_SIGNATURE} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {_LOCK_SOURCE_SIGNATURE} TO campus_ai_runtime")
    op.execute(
        "DROP FUNCTION app.lock_master_data_import_takeover_assignment"
        f"({_LOCK_ASSIGNMENT_ARGUMENTS})"
    )
    op.execute(
        "ALTER FUNCTION app_private.lock_master_data_import_takeover_assignment_revision_11"
        f"({_LOCK_ASSIGNMENT_ARGUMENTS}) SET SCHEMA app"
    )
    op.execute(
        "ALTER FUNCTION app.lock_master_data_import_takeover_assignment_revision_11"
        f"({_LOCK_ASSIGNMENT_ARGUMENTS}) RENAME TO lock_master_data_import_takeover_assignment"
    )
    op.execute(
        "REVOKE ALL ON FUNCTION app.lock_master_data_import_takeover_assignment"
        f"({_LOCK_ASSIGNMENT_ARGUMENTS}) FROM PUBLIC"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION app.lock_master_data_import_takeover_assignment"
        f"({_LOCK_ASSIGNMENT_ARGUMENTS}) TO campus_ai_runtime"
    )
    op.execute(
        f"DROP FUNCTION app.supersede_master_data_import_takeover_source({_SUPERSEDE_ARGUMENTS})"
    )
    op.execute(
        "ALTER FUNCTION app_private.supersede_master_data_import_takeover_source_revision_11"
        f"({_SUPERSEDE_ARGUMENTS}) SET SCHEMA app"
    )
    op.execute(
        "ALTER FUNCTION app.supersede_master_data_import_takeover_source_revision_11"
        f"({_SUPERSEDE_ARGUMENTS}) RENAME TO supersede_master_data_import_takeover_source"
    )
    op.execute(f"REVOKE ALL ON FUNCTION {_SUPERSEDE_SIGNATURE} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {_SUPERSEDE_SIGNATURE} TO campus_ai_runtime")
    op.execute(
        f"DROP FUNCTION app.assert_master_data_import_takeover_integrity({_ASSERT_ARGUMENTS})"
    )
    op.execute(
        "ALTER FUNCTION app_private.assert_master_data_import_takeover_integrity_revision_11"
        f"({_ASSERT_ARGUMENTS}) SET SCHEMA app"
    )
    op.execute(
        "ALTER FUNCTION app.assert_master_data_import_takeover_integrity_revision_11"
        f"({_ASSERT_ARGUMENTS}) RENAME TO assert_master_data_import_takeover_integrity"
    )
    op.execute("DROP FUNCTION app_private.enforce_master_data_import_takeover_integrity()")
    op.execute(
        "ALTER FUNCTION app_private.enforce_master_data_import_takeover_integrity_revision_11() "
        "RENAME TO enforce_master_data_import_takeover_integrity"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION app_private.enforce_master_data_import_takeover_integrity() "
        "TO campus_ai_db_owner"
    )
    op.execute("SET LOCAL ROLE campus_ai_db_owner")
    op.execute(
        "CREATE CONSTRAINT TRIGGER enforce_master_data_import_takeover_insert_integrity "
        "AFTER INSERT ON public.master_data_import_takeovers "
        "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW "
        "EXECUTE FUNCTION app_private.enforce_master_data_import_takeover_integrity()"
    )
    op.execute(
        "CREATE CONSTRAINT TRIGGER enforce_master_data_import_takeover_batch_integrity "
        "AFTER INSERT OR UPDATE ON public.master_data_import_batches "
        "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW "
        "EXECUTE FUNCTION app_private.enforce_master_data_import_takeover_integrity()"
    )
    op.execute(
        "CREATE CONSTRAINT TRIGGER enforce_master_data_import_takeover_command_integrity "
        "AFTER INSERT OR UPDATE ON public.command_idempotency_records "
        "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW "
        "EXECUTE FUNCTION app_private.enforce_master_data_import_takeover_integrity()"
    )
    op.execute("SET LOCAL ROLE campus_ai_takeover_definer")
    op.execute(
        "REVOKE EXECUTE ON FUNCTION app_private.enforce_master_data_import_takeover_integrity() "
        "FROM campus_ai_db_owner"
    )
    op.execute("SET LOCAL ROLE campus_ai_db_owner")
    op.execute("REVOKE CREATE ON SCHEMA app, app_private FROM campus_ai_takeover_definer")

    op.execute("GRANT CREATE ON SCHEMA app, app_private TO campus_ai_audit_definer")
    op.execute("SET LOCAL ROLE campus_ai_audit_definer")
    op.execute(f"DROP FUNCTION app.append_master_data_import_audit({_AUDIT_ARGUMENTS})")
    op.execute(
        "ALTER FUNCTION app_private.append_master_data_import_audit_revision_11"
        f"({_AUDIT_ARGUMENTS}) SET SCHEMA app"
    )
    op.execute(
        "ALTER FUNCTION app.append_master_data_import_audit_revision_11"
        f"({_AUDIT_ARGUMENTS}) RENAME TO append_master_data_import_audit"
    )
    op.execute(
        f"REVOKE ALL ON FUNCTION app.append_master_data_import_audit({_AUDIT_ARGUMENTS}) "
        "FROM PUBLIC"
    )
    op.execute(
        f"GRANT EXECUTE ON FUNCTION app.append_master_data_import_audit({_AUDIT_ARGUMENTS}) "
        "TO campus_ai_runtime, campus_ai_db_owner, campus_ai_takeover_definer"
    )
    op.execute("SET LOCAL ROLE campus_ai_db_owner")
    op.execute("REVOKE CREATE ON SCHEMA app, app_private FROM campus_ai_audit_definer")

    op.execute("SET LOCAL ROLE campus_ai_takeover_definer")
    op.execute(f"DROP FUNCTION IF EXISTS {_SUCCEEDED_ATTEMPT_SIGNATURE}")
    op.execute("SET LOCAL ROLE campus_ai_db_owner")
    op.execute(
        "DROP TRIGGER populate_master_data_import_unit_outcome_snapshot "
        "ON public.master_data_import_command_outcomes"
    )
    op.execute(
        "DROP TRIGGER reject_master_data_import_outcome_row_mutation "
        "ON public.master_data_import_command_outcomes"
    )
    op.execute(
        "DROP TRIGGER reject_master_data_import_outcome_truncate "
        "ON public.master_data_import_command_outcomes"
    )
    op.execute("SET LOCAL ROLE campus_ai_takeover_definer")
    op.execute(f"DROP FUNCTION {_SNAPSHOT_POPULATOR_SIGNATURE}")
    op.execute(f"DROP FUNCTION {_OUTCOME_MUTATION_REJECTOR_SIGNATURE}")
    op.execute("SET LOCAL ROLE campus_ai_db_owner")
    op.execute(
        "REVOKE SELECT (target_id, target_version, error_code, error_field_code) "
        "ON TABLE public.master_data_import_units FROM campus_ai_takeover_definer"
    )
    op.drop_constraint(
        "ck_master_data_import_command_outcomes_unit_snapshots_array",
        "master_data_import_command_outcomes",
        type_="check",
        schema="public",
    )
    op.drop_column(
        "master_data_import_command_outcomes",
        "unit_outcome_snapshots",
        schema="public",
    )
    op.execute("SET LOCAL ROLE campus_ai_takeover_definer")
    op.execute(f"DROP FUNCTION app.read_master_data_import_attempt_lineage({_LINEAGE_ARGUMENTS})")
    op.execute(f"DROP FUNCTION IF EXISTS {_AUDIT_CHAIN_SIGNATURE}")
    op.execute(
        f"DROP FUNCTION app_private.master_data_import_takeover_chain_is_valid({_CHAIN_ARGUMENTS})"
    )
    op.execute("SET LOCAL ROLE campus_ai_db_owner")
    op.execute(
        "REVOKE SELECT (school_id, idempotency_record_id, batch_id, action_code, status, "
        "batch_version, succeeded_units, failed_units, executed_units) ON TABLE "
        "public.master_data_import_command_outcomes FROM campus_ai_takeover_definer"
    )
