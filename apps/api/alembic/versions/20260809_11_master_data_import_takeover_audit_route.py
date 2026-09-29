"""Converge existing revision 10 takeover state onto durable facts."""

from __future__ import annotations

from alembic import op

revision = "20260809_11"
down_revision = "20260809_10"
branch_labels = None
depends_on = None

_AUDIT_ARGUMENTS = (
    "integer,uuid,uuid,uuid,bigint,text,text,text,uuid,uuid,uuid,jsonb,jsonb,uuid,text"
)
_PUBLIC_AUDIT = f"app.append_master_data_import_audit({_AUDIT_ARGUMENTS})"
_PRIVATE_XID_AUDIT = f"app_private.append_master_data_import_audit_xid({_AUDIT_ARGUMENTS})"
_SUPERSEDE_ARGUMENTS = "uuid,uuid,uuid,text,bigint"
_PUBLIC_SUPERSEDE = f"app.supersede_master_data_import_takeover_source({_SUPERSEDE_ARGUMENTS})"
_PRIVATE_XID_SUPERSEDE = (
    f"app_private.supersede_master_data_import_takeover_source_xid({_SUPERSEDE_ARGUMENTS})"
)
_PUBLIC_MANIFEST = "app_private.populate_master_data_import_takeover_manifest()"
_PRIVATE_XID_MANIFEST = "app_private.populate_master_data_import_takeover_manifest_xid()"
_PUBLIC_UNIT_GUARD = "app_private.enforce_master_data_import_unit_immutability()"
_PRIVATE_REVISION_10_UNIT_GUARD = (
    "app_private.enforce_master_data_import_unit_immutability_revision_10()"
)
_ASSERT_TAKEOVER = "app.assert_master_data_import_takeover_integrity(uuid,uuid,uuid)"
_ATTEMPT_TRANSITION = "app_private.enforce_master_data_import_attempt_transition()"
_TAKEOVER_INTEGRITY_TRIGGER = "app_private.enforce_master_data_import_takeover_integrity()"


def _validate_revision_10_takeover_history() -> None:
    """Fail closed when revision 10 already contains a partial takeover state."""

    op.execute("SET LOCAL ROLE campus_ai_db_owner")
    for table_name in (
        "audit_logs",
        "command_idempotency_records",
        "master_data_import_batches",
        "master_data_import_takeovers",
        "master_data_import_units",
    ):
        op.execute(f"ALTER TABLE public.{table_name} NO FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        DO $preflight$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM public.master_data_import_takeovers AS takeover
                WHERE NOT EXISTS (
                    SELECT 1
                    FROM public.command_idempotency_records AS old_record
                    JOIN public.command_idempotency_records AS new_record
                      ON new_record.school_id = takeover.school_id
                     AND new_record.id = takeover.new_idempotency_record_id
                    JOIN public.master_data_import_batches AS batch
                      ON batch.school_id = takeover.school_id
                     AND batch.id = takeover.batch_id
                    WHERE old_record.school_id = takeover.school_id
                      AND old_record.id = takeover.previous_idempotency_record_id
                      AND takeover.original_actor_user_id = old_record.actor_user_id
                      AND takeover.original_actor_person_id = old_record.actor_person_id
                      AND takeover.original_actor_assignment_id = old_record.actor_assignment_id
                      AND takeover.original_actor_assignment_version =
                          old_record.actor_assignment_version
                      AND takeover.new_actor_user_id = new_record.actor_user_id
                      AND takeover.new_actor_person_id = new_record.actor_person_id
                      AND takeover.new_actor_assignment_id = new_record.actor_assignment_id
                      AND takeover.new_actor_assignment_version =
                          new_record.actor_assignment_version
                      AND takeover.batch_version_after = takeover.batch_version_before + 1
                      AND old_record.action_code IN (
                          'master_data.import.ordinary_confirm',
                          'master_data.import.ordinary_resume'
                      )
                      AND old_record.actor_context_type = 'assignment'
                      AND old_record.resource_type = 'master_data.import_batch'
                      AND old_record.resource_id = takeover.batch_id::text
                      AND old_record.status = 'terminal_failure'
                      AND old_record.result_type = 'master_data.import_outcome'
                      AND old_record.result_payload = pg_catalog.jsonb_build_object(
                          'resource_type', 'master_data.import_batch',
                          'resource_id', takeover.batch_id::text,
                          'resource_version', takeover.batch_version_before,
                          'outcome_code', 'master_data.import.command_superseded'
                      )
                      AND new_record.action_code = 'master_data.import.ordinary_takeover'
                      AND new_record.actor_context_type = 'assignment'
                      AND new_record.resource_type = 'master_data.import_batch'
                      AND new_record.resource_id = takeover.batch_id::text
                      AND new_record.status = 'in_progress'
                      AND new_record.result_type IS NULL
                      AND batch.mode = 'ordinary_atomic_units'
                      AND batch.source_kind = 'excel'
                      AND batch.status = 'applying'
                      AND batch.active_idempotency_record_id = new_record.id
                      AND batch.active_action_code = new_record.action_code
                      AND batch.version = takeover.batch_version_after
                      AND batch.planned_units = (
                          SELECT pg_catalog.count(*)
                          FROM public.master_data_import_units AS counted_unit
                          WHERE counted_unit.school_id = takeover.school_id
                            AND counted_unit.batch_id = takeover.batch_id
                      )
                      AND takeover.transferred_sequences = ARRAY(
                          SELECT exact_unit.sequence::integer
                          FROM public.master_data_import_units AS exact_unit
                          WHERE exact_unit.school_id = takeover.school_id
                            AND exact_unit.batch_id = takeover.batch_id
                            AND exact_unit.last_attempt_idempotency_record_id = new_record.id
                            AND exact_unit.last_attempt_completed IS FALSE
                            AND exact_unit.status IN ('pending', 'ready', 'failed')
                          ORDER BY exact_unit.sequence
                      )
                      AND takeover.transferred_unit_versions = ARRAY(
                          SELECT exact_unit.version
                          FROM public.master_data_import_units AS exact_unit
                          WHERE exact_unit.school_id = takeover.school_id
                            AND exact_unit.batch_id = takeover.batch_id
                            AND exact_unit.last_attempt_idempotency_record_id = new_record.id
                            AND exact_unit.last_attempt_completed IS FALSE
                            AND exact_unit.status IN ('pending', 'ready', 'failed')
                          ORDER BY exact_unit.sequence
                      )
                      AND NOT EXISTS (
                          SELECT 1
                          FROM public.master_data_import_units AS invalid_unit
                          WHERE invalid_unit.school_id = takeover.school_id
                            AND invalid_unit.batch_id = takeover.batch_id
                            AND invalid_unit.last_attempt_completed IS FALSE
                            AND (
                                invalid_unit.last_attempt_idempotency_record_id
                                    IS DISTINCT FROM new_record.id
                                OR invalid_unit.status NOT IN ('pending', 'ready', 'failed')
                            )
                      )
                      AND 1 = (
                          SELECT pg_catalog.count(*)
                          FROM public.audit_logs AS audit
                          WHERE audit.school_id = takeover.school_id
                            AND audit.event_type = 'master_data.import.taken_over'
                            AND audit.target_type = 'master_data_import_takeover'
                            AND audit.target_id = takeover.id::text
                            AND audit.request_id = takeover.request_id::text
                            AND audit.actor_user_id = takeover.new_actor_user_id
                            AND audit.actor_person_id = takeover.new_actor_person_id
                            AND audit.actor_assignment_id = takeover.new_actor_assignment_id
                            AND audit.actor_assignment_version =
                                takeover.new_actor_assignment_version
                            AND audit.actor_context_type = 'assignment'
                            AND audit.idempotency_key_hash = new_record.idempotency_key_hash
                            AND audit.reason = 'manual.ordinary-import-taken-over'
                      )
                )
            ) THEN
                RAISE EXCEPTION
                    'master_data_import_takeover_upgrade_preflight:incomplete_takeover_bundle'
                    USING ERRCODE = '23514';
            END IF;

            IF EXISTS (
                SELECT 1
                FROM public.master_data_import_batches AS batch
                WHERE batch.active_action_code = 'master_data.import.ordinary_takeover'
                  AND NOT EXISTS (
                      SELECT 1
                      FROM public.master_data_import_takeovers AS takeover
                      WHERE takeover.school_id = batch.school_id
                        AND takeover.batch_id = batch.id
                        AND takeover.new_idempotency_record_id =
                            batch.active_idempotency_record_id
                  )
            ) OR EXISTS (
                SELECT 1
                FROM public.command_idempotency_records AS new_record
                WHERE new_record.action_code = 'master_data.import.ordinary_takeover'
                  AND NOT EXISTS (
                      SELECT 1
                      FROM public.master_data_import_takeovers AS takeover
                      WHERE takeover.school_id = new_record.school_id
                        AND takeover.new_idempotency_record_id = new_record.id
                  )
            ) OR EXISTS (
                SELECT 1
                FROM public.master_data_import_units AS unit
                JOIN public.command_idempotency_records AS new_record
                  ON new_record.school_id = unit.school_id
                 AND new_record.id = unit.last_attempt_idempotency_record_id
                 AND new_record.action_code = 'master_data.import.ordinary_takeover'
                WHERE unit.last_attempt_completed IS FALSE
                  AND NOT EXISTS (
                      SELECT 1
                      FROM public.master_data_import_takeovers AS takeover
                      WHERE takeover.school_id = unit.school_id
                        AND takeover.batch_id = unit.batch_id
                        AND takeover.new_idempotency_record_id = new_record.id
                  )
            ) THEN
                RAISE EXCEPTION
                    'master_data_import_takeover_upgrade_preflight:active_takeover_without_provenance'
                    USING ERRCODE = '23514';
            END IF;

            IF EXISTS (
                SELECT 1
                FROM public.command_idempotency_records AS old_record
                WHERE old_record.action_code IN (
                          'master_data.import.ordinary_confirm',
                          'master_data.import.ordinary_resume'
                      )
                  AND old_record.status = 'terminal_failure'
                  AND old_record.result_type = 'master_data.import_outcome'
                  AND old_record.result_payload ->> 'outcome_code' =
                      'master_data.import.command_superseded'
                  AND NOT EXISTS (
                      SELECT 1
                      FROM public.master_data_import_takeovers AS takeover
                      WHERE takeover.school_id = old_record.school_id
                        AND takeover.previous_idempotency_record_id = old_record.id
                  )
            ) THEN
                RAISE EXCEPTION
                    'master_data_import_takeover_upgrade_preflight:'
                    'superseded_source_without_complete_takeover'
                    USING ERRCODE = '23514';
            END IF;
        END
        $preflight$
        """
    )
    for table_name in (
        "audit_logs",
        "command_idempotency_records",
        "master_data_import_batches",
        "master_data_import_takeovers",
        "master_data_import_units",
    ):
        op.execute(f"ALTER TABLE public.{table_name} FORCE ROW LEVEL SECURITY")


def _upgrade_takeover_acl_and_preserve_revision_10() -> None:
    """保留已安装 revision 10 定义，并建立新边界所需精确读权。"""

    op.execute("SET LOCAL ROLE campus_ai_db_owner")
    op.execute("GRANT CREATE ON SCHEMA app TO campus_ai_takeover_definer")
    op.execute("GRANT USAGE, CREATE ON SCHEMA app_private TO campus_ai_takeover_definer")
    op.execute(
        "GRANT SELECT (result_payload) ON TABLE public.command_idempotency_records "
        "TO campus_ai_takeover_definer"
    )
    op.execute(
        "GRANT SELECT (id, actor_user_id, school_id, actor_person_id, "
        "actor_assignment_id, actor_assignment_version, actor_context_type, "
        "idempotency_key_hash, event_type, target_type, target_id, request_id, reason) "
        "ON TABLE public.audit_logs TO campus_ai_takeover_definer"
    )
    op.execute(
        "CREATE POLICY takeover_definer_tenant_select ON public.audit_logs "
        "FOR SELECT TO campus_ai_takeover_definer "
        "USING (school_id = (SELECT app.require_school_context()))"
    )
    for table_name in (
        "command_idempotency_records",
        "master_data_import_batches",
        "master_data_import_takeovers",
        "master_data_import_units",
    ):
        op.execute(
            f"REVOKE SELECT (xmin) ON TABLE public.{table_name} FROM campus_ai_takeover_definer"
        )

    op.execute("SET LOCAL ROLE campus_ai_takeover_definer")
    op.execute(
        f"ALTER FUNCTION {_PUBLIC_SUPERSEDE} "
        "RENAME TO supersede_master_data_import_takeover_source_xid"
    )
    op.execute(
        "ALTER FUNCTION app.supersede_master_data_import_takeover_source_xid"
        f"({_SUPERSEDE_ARGUMENTS}) SET SCHEMA app_private"
    )
    op.execute(
        f"REVOKE ALL ON FUNCTION {_PRIVATE_XID_SUPERSEDE} "
        "FROM PUBLIC, campus_ai_runtime, campus_ai_db_owner"
    )
    op.execute(
        f"ALTER FUNCTION {_PUBLIC_MANIFEST} "
        "RENAME TO populate_master_data_import_takeover_manifest_xid"
    )
    op.execute(
        f"REVOKE ALL ON FUNCTION {_PRIVATE_XID_MANIFEST} "
        "FROM PUBLIC, campus_ai_runtime, campus_ai_db_owner"
    )

    op.execute("SET LOCAL ROLE campus_ai_db_owner")
    op.execute(
        "DROP TRIGGER populate_master_data_import_takeover_manifest "
        "ON public.master_data_import_takeovers"
    )
    op.execute(
        "DROP TRIGGER enforce_master_data_import_unit_immutability "
        "ON public.master_data_import_units"
    )
    op.execute(
        f"ALTER FUNCTION {_PUBLIC_UNIT_GUARD} "
        "RENAME TO enforce_master_data_import_unit_immutability_revision_10"
    )
    op.execute(
        f"REVOKE ALL ON FUNCTION {_PRIVATE_REVISION_10_UNIT_GUARD} "
        "FROM PUBLIC, campus_ai_runtime, campus_ai_takeover_definer"
    )


def _create_attempt_state_and_manifest_boundaries() -> None:
    """显式守卫 attempt 状态，并从可信 incomplete prestate 生成清单。"""

    op.execute("SET LOCAL ROLE campus_ai_db_owner")
    op.execute(
        """
        CREATE FUNCTION app_private.enforce_master_data_import_unit_immutability()
        RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = pg_catalog
        AS $function$
        DECLARE
            v_old_stable jsonb := pg_catalog.to_jsonb(OLD)
                - ARRAY[
                    'last_attempt_idempotency_record_id',
                    'last_attempt_completed',
                    'updated_at'
                ]::text[];
            v_new_stable jsonb := pg_catalog.to_jsonb(NEW)
                - ARRAY[
                    'last_attempt_idempotency_record_id',
                    'last_attempt_completed',
                    'updated_at'
                ]::text[];
        BEGIN
            IF OLD.status IN ('applied', 'published') AND NEW IS DISTINCT FROM OLD THEN
                RAISE EXCEPTION 'terminal master data import decision is immutable'
                    USING ERRCODE = '55000';
            END IF;

            IF OLD.last_attempt_completed IS TRUE THEN
                IF NEW.last_attempt_idempotency_record_id
                    IS NOT DISTINCT FROM OLD.last_attempt_idempotency_record_id THEN
                    IF NEW IS DISTINCT FROM OLD THEN
                        RAISE EXCEPTION 'completed master data import attempt is immutable'
                            USING ERRCODE = '55000';
                    END IF;
                ELSIF NEW.last_attempt_completed IS DISTINCT FROM false
                      OR v_new_stable IS DISTINCT FROM v_old_stable THEN
                    RAISE EXCEPTION 'completed attempt may only activate a new fixed command'
                        USING ERRCODE = '55000';
                END IF;
            ELSIF OLD.last_attempt_completed IS FALSE THEN
                IF NEW.last_attempt_idempotency_record_id
                    IS NOT DISTINCT FROM OLD.last_attempt_idempotency_record_id THEN
                    IF NEW.last_attempt_completed IS FALSE
                       AND v_new_stable IS DISTINCT FROM v_old_stable THEN
                        RAISE EXCEPTION 'incomplete attempt may only complete or transfer'
                            USING ERRCODE = '55000';
                    ELSIF NEW.last_attempt_completed IS TRUE
                          AND (
                              NEW.status NOT IN ('applied', 'failed')
                              OR NEW.version <> OLD.version + 1
                          ) THEN
                        RAISE EXCEPTION 'completed attempt result is not a fixed unit transition'
                            USING ERRCODE = '55000';
                    ELSIF NEW.last_attempt_completed IS NULL THEN
                        RAISE EXCEPTION 'attempt completion cannot become unknown'
                            USING ERRCODE = '55000';
                    END IF;
                ELSIF NEW.last_attempt_completed IS DISTINCT FROM false
                      OR v_new_stable IS DISTINCT FROM v_old_stable THEN
                    RAISE EXCEPTION 'incomplete attempt transfer may only change command ownership'
                        USING ERRCODE = '55000';
                END IF;
            ELSIF OLD.last_attempt_idempotency_record_id IS NULL
                  AND NEW.last_attempt_idempotency_record_id IS NOT NULL
                  AND (
                      NEW.last_attempt_completed IS DISTINCT FROM false
                      OR v_new_stable IS DISTINCT FROM v_old_stable
                  ) THEN
                RAISE EXCEPTION 'initial attempt activation may only bind command ownership'
                    USING ERRCODE = '55000';
            END IF;
            RETURN NEW;
        END
        $function$
        """
    )
    op.execute(f"REVOKE ALL ON FUNCTION {_PUBLIC_UNIT_GUARD} FROM PUBLIC")
    op.execute(
        "CREATE TRIGGER enforce_master_data_import_unit_immutability "
        "BEFORE UPDATE ON public.master_data_import_units FOR EACH ROW "
        f"EXECUTE FUNCTION {_PUBLIC_UNIT_GUARD}"
    )

    op.execute("SET LOCAL ROLE campus_ai_takeover_definer")
    op.execute(
        """
        CREATE FUNCTION app_private.populate_master_data_import_takeover_manifest()
        RETURNS trigger
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog
        AS $function$
        DECLARE
            v_school_id uuid := app.require_school_context();
            v_sequences integer[];
            v_versions bigint[];
        BEGIN
            IF NEW.school_id IS DISTINCT FROM v_school_id THEN
                RAISE EXCEPTION 'takeover manifest school does not match signed context'
                    USING ERRCODE = '28000';
            END IF;
            IF EXISTS (
                SELECT 1
                FROM public.master_data_import_units AS source_unit
                WHERE source_unit.school_id = v_school_id
                  AND source_unit.batch_id = NEW.batch_id
                  AND source_unit.last_attempt_idempotency_record_id =
                      NEW.previous_idempotency_record_id
                  AND source_unit.last_attempt_completed IS FALSE
                  AND (
                      source_unit.status NOT IN ('pending', 'ready', 'failed')
                      OR source_unit.sequence < 1
                      OR source_unit.sequence > 2147483647
                  )
            ) THEN
                RAISE EXCEPTION 'takeover manifest source unit is not eligible'
                    USING ERRCODE = '23514';
            END IF;
            SELECT pg_catalog.array_agg(
                       source_unit.sequence::integer ORDER BY source_unit.sequence
                   ),
                   pg_catalog.array_agg(source_unit.version ORDER BY source_unit.sequence)
            INTO v_sequences, v_versions
            FROM public.master_data_import_units AS source_unit
            WHERE source_unit.school_id = v_school_id
              AND source_unit.batch_id = NEW.batch_id
              AND source_unit.last_attempt_idempotency_record_id =
                  NEW.previous_idempotency_record_id
              AND source_unit.last_attempt_completed IS FALSE;
            IF v_sequences IS NULL
               OR pg_catalog.cardinality(v_sequences) NOT BETWEEN 1 AND 500
               OR pg_catalog.cardinality(v_sequences)
                  <> pg_catalog.cardinality(v_versions) THEN
                RAISE EXCEPTION 'takeover manifest requires incomplete source units'
                    USING ERRCODE = '23514';
            END IF;
            NEW.transferred_sequences := v_sequences;
            NEW.transferred_unit_versions := v_versions;
            RETURN NEW;
        END
        $function$
        """
    )
    op.execute(f"REVOKE ALL ON FUNCTION {_PUBLIC_MANIFEST} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {_PUBLIC_MANIFEST} TO campus_ai_db_owner")
    op.execute("SET LOCAL ROLE campus_ai_db_owner")
    op.execute(
        "CREATE TRIGGER populate_master_data_import_takeover_manifest "
        "BEFORE INSERT ON public.master_data_import_takeovers FOR EACH ROW "
        f"EXECUTE FUNCTION {_PUBLIC_MANIFEST}"
    )
    op.execute("SET LOCAL ROLE campus_ai_takeover_definer")
    op.execute(f"REVOKE EXECUTE ON FUNCTION {_PUBLIC_MANIFEST} FROM campus_ai_db_owner")
    op.execute("SET LOCAL ROLE campus_ai_db_owner")


def _create_deferred_takeover_integrity_boundaries() -> None:
    """事务结束时只接受一个完整且精确的 takeover durable bundle。"""

    op.execute("SET LOCAL ROLE campus_ai_takeover_definer")
    op.execute(
        """
        CREATE FUNCTION app.assert_master_data_import_takeover_integrity(
            p_school_id uuid,
            p_batch_id uuid,
            p_new_record_id uuid
        )
        RETURNS void
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog
        AS $function$
        DECLARE
            v_school_id uuid := app.require_school_context();
            v_matching_bundles bigint := 0;
        BEGIN
            IF p_school_id IS DISTINCT FROM v_school_id THEN
                RAISE EXCEPTION 'takeover integrity school does not match signed context'
                    USING ERRCODE = '28000';
            END IF;

            SELECT pg_catalog.count(*)
            INTO v_matching_bundles
            FROM public.master_data_import_takeovers AS takeover
            JOIN public.command_idempotency_records AS old_record
              ON old_record.school_id = takeover.school_id
             AND old_record.id = takeover.previous_idempotency_record_id
            JOIN public.command_idempotency_records AS new_record
              ON new_record.school_id = takeover.school_id
             AND new_record.id = takeover.new_idempotency_record_id
            JOIN public.master_data_import_batches AS batch
              ON batch.school_id = takeover.school_id
             AND batch.id = takeover.batch_id
            WHERE takeover.school_id = v_school_id
              AND takeover.batch_id = p_batch_id
              AND takeover.new_idempotency_record_id = p_new_record_id
              AND takeover.original_actor_user_id = old_record.actor_user_id
              AND takeover.original_actor_person_id = old_record.actor_person_id
              AND takeover.original_actor_assignment_id = old_record.actor_assignment_id
              AND takeover.original_actor_assignment_version =
                  old_record.actor_assignment_version
              AND takeover.new_actor_user_id = new_record.actor_user_id
              AND takeover.new_actor_person_id = new_record.actor_person_id
              AND takeover.new_actor_assignment_id = new_record.actor_assignment_id
              AND takeover.new_actor_assignment_version = new_record.actor_assignment_version
              AND takeover.batch_version_after = takeover.batch_version_before + 1
              AND old_record.action_code IN (
                  'master_data.import.ordinary_confirm',
                  'master_data.import.ordinary_resume'
              )
              AND old_record.actor_context_type = 'assignment'
              AND old_record.resource_type = 'master_data.import_batch'
              AND old_record.resource_id = p_batch_id::text
              AND old_record.status = 'terminal_failure'
              AND old_record.result_type = 'master_data.import_outcome'
              AND old_record.result_payload = pg_catalog.jsonb_build_object(
                  'resource_type', 'master_data.import_batch',
                  'resource_id', p_batch_id::text,
                  'resource_version', takeover.batch_version_before,
                  'outcome_code', 'master_data.import.command_superseded'
              )
              AND new_record.action_code = 'master_data.import.ordinary_takeover'
              AND new_record.actor_context_type = 'assignment'
              AND new_record.resource_type = 'master_data.import_batch'
              AND new_record.resource_id = p_batch_id::text
              AND new_record.status = 'in_progress'
              AND new_record.result_type IS NULL
              AND batch.mode = 'ordinary_atomic_units'
              AND batch.source_kind = 'excel'
              AND batch.status = 'applying'
              AND batch.active_idempotency_record_id = new_record.id
              AND batch.active_action_code = new_record.action_code
              AND batch.version = takeover.batch_version_after
              AND batch.planned_units = (
                  SELECT pg_catalog.count(*)
                  FROM public.master_data_import_units AS counted_unit
                  WHERE counted_unit.school_id = v_school_id
                    AND counted_unit.batch_id = p_batch_id
              )
              AND takeover.transferred_sequences = ARRAY(
                  SELECT exact_unit.sequence::integer
                  FROM public.master_data_import_units AS exact_unit
                  WHERE exact_unit.school_id = v_school_id
                    AND exact_unit.batch_id = p_batch_id
                    AND exact_unit.last_attempt_idempotency_record_id = new_record.id
                    AND exact_unit.last_attempt_completed IS FALSE
                    AND exact_unit.status IN ('pending', 'ready', 'failed')
                  ORDER BY exact_unit.sequence
              )
              AND takeover.transferred_unit_versions = ARRAY(
                  SELECT exact_unit.version
                  FROM public.master_data_import_units AS exact_unit
                  WHERE exact_unit.school_id = v_school_id
                    AND exact_unit.batch_id = p_batch_id
                    AND exact_unit.last_attempt_idempotency_record_id = new_record.id
                    AND exact_unit.last_attempt_completed IS FALSE
                    AND exact_unit.status IN ('pending', 'ready', 'failed')
                  ORDER BY exact_unit.sequence
              )
              AND NOT EXISTS (
                  SELECT 1
                  FROM public.master_data_import_units AS invalid_unit
                  WHERE invalid_unit.school_id = v_school_id
                    AND invalid_unit.batch_id = p_batch_id
                    AND invalid_unit.last_attempt_completed IS FALSE
                    AND (
                        invalid_unit.last_attempt_idempotency_record_id
                            IS DISTINCT FROM new_record.id
                        OR invalid_unit.status NOT IN ('pending', 'ready', 'failed')
                    )
              )
              AND 1 = (
                  SELECT pg_catalog.count(*)
                  FROM public.audit_logs AS audit
                  WHERE audit.school_id = v_school_id
                    AND audit.event_type = 'master_data.import.taken_over'
                    AND audit.target_type = 'master_data_import_takeover'
                    AND audit.target_id = takeover.id::text
                    AND audit.request_id = takeover.request_id::text
                    AND audit.actor_user_id = takeover.new_actor_user_id
                    AND audit.actor_person_id = takeover.new_actor_person_id
                    AND audit.actor_assignment_id = takeover.new_actor_assignment_id
                    AND audit.actor_assignment_version =
                        takeover.new_actor_assignment_version
                    AND audit.actor_context_type = 'assignment'
                    AND audit.idempotency_key_hash = new_record.idempotency_key_hash
                    AND audit.reason = 'manual.ordinary-import-taken-over'
              );

            IF v_matching_bundles <> 1 THEN
                RAISE EXCEPTION 'ordinary import takeover did not reach one complete durable state'
                    USING ERRCODE = '23514';
            END IF;
        END
        $function$
        """
    )
    op.execute(f"REVOKE ALL ON FUNCTION {_ASSERT_TAKEOVER} FROM PUBLIC")

    op.execute(
        """
        CREATE FUNCTION app_private.enforce_master_data_import_attempt_transition()
        RETURNS trigger
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog
        AS $function$
        DECLARE
            v_school_id uuid;
            v_now timestamp with time zone := pg_catalog.clock_timestamp();
            v_action_code text;
        BEGIN
            IF NEW.last_attempt_idempotency_record_id
                IS NOT DISTINCT FROM OLD.last_attempt_idempotency_record_id THEN
                RETURN NULL;
            END IF;
            v_school_id := app.require_school_context();
            IF NEW.school_id IS DISTINCT FROM v_school_id
               OR NEW.last_attempt_completed IS DISTINCT FROM false
               OR current_setting('app.context_purpose', true) IS DISTINCT FROM 'command'
               OR current_setting('app.context_kind', true) IS DISTINCT FROM 'assignment'
               OR current_setting('app.guardian_relationship_id', true) IS DISTINCT FROM '-' THEN
                RAISE EXCEPTION 'attempt ownership transition requires signed staff command context'
                    USING ERRCODE = '28000';
            END IF;

            SELECT new_record.action_code
            INTO v_action_code
            FROM public.command_idempotency_records AS new_record
            JOIN public.master_data_import_batches AS batch
              ON batch.school_id = new_record.school_id
             AND batch.id = NEW.batch_id
             AND batch.status = 'applying'
             AND batch.active_idempotency_record_id = new_record.id
             AND batch.active_action_code = new_record.action_code
            JOIN public.master_data_import_units AS current_unit
              ON current_unit.school_id = NEW.school_id
             AND current_unit.id = NEW.id
             AND current_unit.last_attempt_idempotency_record_id = new_record.id
             AND current_unit.last_attempt_completed IS FALSE
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
             )
            WHERE new_record.school_id = v_school_id
              AND new_record.id = NEW.last_attempt_idempotency_record_id
              AND new_record.actor_user_id =
                  current_setting('app.account_user_id', true)::integer
              AND new_record.actor_person_id = current_setting('app.person_id', true)::uuid
              AND new_record.actor_context_type = 'assignment'
              AND new_record.actor_assignment_id =
                  current_setting('app.assignment_id', true)::uuid
              AND new_record.actor_assignment_version =
                  current_setting('app.assignment_version', true)::bigint
              AND new_record.idempotency_key_hash =
                  current_setting('app.idempotency_key_hash', true)
              AND new_record.resource_type = 'master_data.import_batch'
              AND new_record.resource_id = NEW.batch_id::text
              AND new_record.status = 'in_progress'
              AND new_record.result_type IS NULL
              AND new_record.action_code IN (
                  'master_data.import.ordinary_confirm',
                  'master_data.import.ordinary_resume',
                  'master_data.import.ordinary_takeover'
              );
            IF v_action_code IS NULL THEN
                RAISE EXCEPTION 'attempt ownership transition has no current fixed command'
                    USING ERRCODE = '23514';
            END IF;

            IF OLD.last_attempt_idempotency_record_id IS NULL THEN
                IF OLD.last_attempt_completed IS NOT NULL
                   OR v_action_code NOT IN (
                       'master_data.import.ordinary_confirm',
                       'master_data.import.ordinary_resume'
                   ) THEN
                    RAISE EXCEPTION 'initial attempt ownership requires confirm or recovery resume'
                        USING ERRCODE = '23514';
                END IF;
            ELSIF OLD.last_attempt_completed IS TRUE THEN
                IF v_action_code <> 'master_data.import.ordinary_resume' THEN
                    RAISE EXCEPTION 'completed attempt may only activate ordinary resume'
                        USING ERRCODE = '23514';
                END IF;
            ELSIF OLD.last_attempt_completed IS FALSE THEN
                IF v_action_code <> 'master_data.import.ordinary_takeover' THEN
                    RAISE EXCEPTION 'incomplete attempt transfer requires explicit takeover'
                        USING ERRCODE = '23514';
                END IF;
                PERFORM app.assert_master_data_import_takeover_integrity(
                    v_school_id,
                    NEW.batch_id,
                    NEW.last_attempt_idempotency_record_id
                );
            ELSE
                RAISE EXCEPTION 'attempt ownership transition has unknown source state'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NULL;
        END
        $function$
        """
    )
    op.execute(f"REVOKE ALL ON FUNCTION {_ATTEMPT_TRANSITION} FROM PUBLIC")

    op.execute(
        """
        CREATE FUNCTION app_private.enforce_master_data_import_takeover_integrity()
        RETURNS trigger
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog
        AS $function$
        DECLARE
            v_school_id uuid;
            v_batch_id uuid;
            v_new_record_id uuid;
        BEGIN
            IF TG_TABLE_NAME = 'master_data_import_takeovers' THEN
                v_school_id := NEW.school_id;
                v_batch_id := NEW.batch_id;
                v_new_record_id := NEW.new_idempotency_record_id;
            ELSIF TG_TABLE_NAME = 'master_data_import_batches' THEN
                IF NEW.active_action_code IS DISTINCT FROM
                       'master_data.import.ordinary_takeover'
                   OR (
                       TG_OP = 'UPDATE'
                       AND NEW.active_idempotency_record_id IS NOT DISTINCT FROM
                           OLD.active_idempotency_record_id
                       AND NEW.active_action_code IS NOT DISTINCT FROM
                           OLD.active_action_code
                   ) THEN
                    RETURN NULL;
                END IF;
                SELECT takeover.school_id,
                       takeover.batch_id,
                       takeover.new_idempotency_record_id
                INTO v_school_id, v_batch_id, v_new_record_id
                FROM public.master_data_import_takeovers AS takeover
                WHERE takeover.school_id = NEW.school_id
                  AND takeover.batch_id = NEW.id
                  AND takeover.new_idempotency_record_id =
                      NEW.active_idempotency_record_id;
                IF v_school_id IS NULL THEN
                    RAISE EXCEPTION 'active takeover command has no durable provenance'
                        USING ERRCODE = '23514';
                END IF;
            ELSIF TG_TABLE_NAME = 'command_idempotency_records' THEN
                IF TG_OP = 'INSERT' THEN
                    IF NEW.action_code IS DISTINCT FROM
                           'master_data.import.ordinary_takeover' THEN
                        RETURN NULL;
                    END IF;
                ELSIF NEW.action_code NOT IN (
                          'master_data.import.ordinary_confirm',
                          'master_data.import.ordinary_resume'
                      )
                      OR NEW.status IS DISTINCT FROM 'terminal_failure'
                      OR NEW.result_type IS DISTINCT FROM 'master_data.import_outcome'
                      OR NEW.result_payload ->> 'outcome_code' IS DISTINCT FROM
                          'master_data.import.command_superseded' THEN
                    RETURN NULL;
                END IF;
                SELECT takeover.school_id,
                       takeover.batch_id,
                       takeover.new_idempotency_record_id
                INTO v_school_id, v_batch_id, v_new_record_id
                FROM public.master_data_import_takeovers AS takeover
                WHERE takeover.school_id = NEW.school_id
                  AND (
                      takeover.previous_idempotency_record_id = NEW.id
                      OR takeover.new_idempotency_record_id = NEW.id
                  );
                IF v_school_id IS NULL THEN
                    RAISE EXCEPTION 'takeover command state has no durable provenance'
                        USING ERRCODE = '23514';
                END IF;
            END IF;
            IF v_school_id IS NOT NULL
               AND v_batch_id IS NOT NULL
               AND v_new_record_id IS NOT NULL THEN
                PERFORM app.assert_master_data_import_takeover_integrity(
                    v_school_id,
                    v_batch_id,
                    v_new_record_id
                );
            END IF;
            RETURN NULL;
        END
        $function$
        """
    )
    op.execute(f"REVOKE ALL ON FUNCTION {_TAKEOVER_INTEGRITY_TRIGGER} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {_ATTEMPT_TRANSITION} TO campus_ai_db_owner")
    op.execute(f"GRANT EXECUTE ON FUNCTION {_TAKEOVER_INTEGRITY_TRIGGER} TO campus_ai_db_owner")

    op.execute("SET LOCAL ROLE campus_ai_db_owner")
    op.execute(
        "CREATE CONSTRAINT TRIGGER enforce_master_data_import_attempt_transition "
        "AFTER UPDATE OF last_attempt_idempotency_record_id, last_attempt_completed "
        "ON public.master_data_import_units DEFERRABLE INITIALLY DEFERRED "
        f"FOR EACH ROW EXECUTE FUNCTION {_ATTEMPT_TRANSITION}"
    )
    op.execute(
        "CREATE CONSTRAINT TRIGGER enforce_master_data_import_takeover_insert_integrity "
        "AFTER INSERT ON public.master_data_import_takeovers "
        "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW "
        f"EXECUTE FUNCTION {_TAKEOVER_INTEGRITY_TRIGGER}"
    )
    op.execute(
        "CREATE CONSTRAINT TRIGGER enforce_master_data_import_takeover_batch_integrity "
        "AFTER INSERT OR UPDATE ON public.master_data_import_batches "
        "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW "
        f"EXECUTE FUNCTION {_TAKEOVER_INTEGRITY_TRIGGER}"
    )
    op.execute(
        "CREATE CONSTRAINT TRIGGER enforce_master_data_import_takeover_command_integrity "
        "AFTER INSERT OR UPDATE ON public.command_idempotency_records "
        "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW "
        f"EXECUTE FUNCTION {_TAKEOVER_INTEGRITY_TRIGGER}"
    )
    op.execute("SET LOCAL ROLE campus_ai_takeover_definer")
    op.execute(f"REVOKE EXECUTE ON FUNCTION {_ATTEMPT_TRANSITION} FROM campus_ai_db_owner")
    op.execute(f"REVOKE EXECUTE ON FUNCTION {_TAKEOVER_INTEGRITY_TRIGGER} FROM campus_ai_db_owner")


def _create_durable_supersede_boundary() -> None:
    """在固定 CAS 前核对完整 durable bundle，并在同一函数内追加审计。"""

    op.execute("SET LOCAL ROLE campus_ai_takeover_definer")
    op.execute(
        """
        CREATE FUNCTION app.supersede_master_data_import_takeover_source(
            p_school_id uuid,
            p_batch_id uuid,
            p_record_id uuid,
            p_expected_request_sha256 text,
            p_batch_version_before bigint
        )
        RETURNS boolean
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog
        AS $function$
        DECLARE
            v_school_id uuid := app.require_school_context();
            v_now timestamp with time zone := pg_catalog.clock_timestamp();
            v_authorized boolean := false;
            v_actor_user_id integer := current_setting('app.account_user_id', true)::integer;
            v_actor_person_id uuid := current_setting('app.person_id', true)::uuid;
            v_actor_assignment_id uuid := current_setting('app.assignment_id', true)::uuid;
            v_actor_assignment_version bigint :=
                current_setting('app.assignment_version', true)::bigint;
            v_idempotency_key_hash text :=
                current_setting('app.idempotency_key_hash', true);
            v_request_id uuid := current_setting('app.request_id', true)::uuid;
            v_source_is_invalid boolean := false;
            v_bundle_matches boolean := false;
            v_takeover_id uuid;
            v_new_record_id uuid;
        BEGIN
            IF p_school_id IS DISTINCT FROM v_school_id
               OR current_setting('app.context_purpose', true) IS DISTINCT FROM 'command'
               OR current_setting('app.context_kind', true) IS DISTINCT FROM 'assignment'
               OR current_setting('app.guardian_relationship_id', true) IS DISTINCT FROM '-'
               OR p_batch_version_before < 1 THEN
                RAISE EXCEPTION 'ordinary import takeover requires staff command context'
                    USING ERRCODE = '28000';
            END IF;
            SELECT true
            INTO v_authorized
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
              );
            IF NOT COALESCE(v_authorized, false) THEN
                RAISE EXCEPTION 'ordinary import takeover requires effective manager assignment'
                    USING ERRCODE = '42501';
            END IF;

            SELECT EXISTS (
                SELECT 1
                FROM public.command_idempotency_records AS old_record
                WHERE old_record.school_id = v_school_id
                  AND old_record.id = p_record_id
                  AND old_record.status = 'in_progress'
                  AND old_record.request_sha256 = p_expected_request_sha256
                  AND old_record.result_type IS NULL
                  AND old_record.actor_context_type = 'assignment'
                  AND old_record.actor_assignment_id IS NOT NULL
                  AND old_record.actor_assignment_version IS NOT NULL
                  AND old_record.action_code IN (
                      'master_data.import.ordinary_confirm',
                      'master_data.import.ordinary_resume'
                  )
                  AND old_record.resource_type = 'master_data.import_batch'
                  AND old_record.resource_id = p_batch_id::text
                  AND NOT EXISTS (
                      SELECT 1
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
                        )
                  )
            ) INTO v_source_is_invalid;
            IF v_source_is_invalid IS DISTINCT FROM true THEN
                RAISE EXCEPTION 'ordinary import takeover source is not invalid'
                    USING ERRCODE = '23514';
            END IF;

            SELECT takeover.id, takeover.new_idempotency_record_id
            INTO v_takeover_id, v_new_record_id
            FROM public.master_data_import_takeovers AS takeover
            JOIN public.command_idempotency_records AS old_record
              ON old_record.school_id = takeover.school_id
             AND old_record.id = takeover.previous_idempotency_record_id
            JOIN public.command_idempotency_records AS new_record
              ON new_record.school_id = takeover.school_id
             AND new_record.id = takeover.new_idempotency_record_id
            JOIN public.master_data_import_batches AS batch
              ON batch.school_id = takeover.school_id
             AND batch.id = takeover.batch_id
            WHERE takeover.school_id = v_school_id
              AND takeover.batch_id = p_batch_id
              AND takeover.previous_idempotency_record_id = p_record_id
              AND takeover.original_actor_user_id = old_record.actor_user_id
              AND takeover.original_actor_person_id = old_record.actor_person_id
              AND takeover.original_actor_assignment_id = old_record.actor_assignment_id
              AND takeover.original_actor_assignment_version =
                  old_record.actor_assignment_version
              AND takeover.new_actor_user_id = v_actor_user_id
              AND takeover.new_actor_person_id = v_actor_person_id
              AND takeover.new_actor_assignment_id = v_actor_assignment_id
              AND takeover.new_actor_assignment_version = v_actor_assignment_version
              AND takeover.request_id = v_request_id
              AND takeover.batch_version_before = p_batch_version_before
              AND takeover.batch_version_after = p_batch_version_before + 1
              AND new_record.action_code = 'master_data.import.ordinary_takeover'
              AND new_record.actor_user_id = v_actor_user_id
              AND new_record.actor_person_id = v_actor_person_id
              AND new_record.actor_context_type = 'assignment'
              AND new_record.actor_assignment_id = v_actor_assignment_id
              AND new_record.actor_assignment_version = v_actor_assignment_version
              AND new_record.resource_type = 'master_data.import_batch'
              AND new_record.resource_id = p_batch_id::text
              AND new_record.idempotency_key_hash = v_idempotency_key_hash
              AND new_record.status = 'in_progress'
              AND new_record.result_type IS NULL
              AND batch.mode = 'ordinary_atomic_units'
              AND batch.source_kind = 'excel'
              AND batch.status = 'applying'
              AND batch.active_idempotency_record_id = new_record.id
              AND batch.active_action_code = 'master_data.import.ordinary_takeover'
              AND batch.version = takeover.batch_version_after
              AND batch.planned_units = (
                  SELECT pg_catalog.count(*)
                  FROM public.master_data_import_units AS counted_unit
                  WHERE counted_unit.school_id = v_school_id
                    AND counted_unit.batch_id = p_batch_id
              )
              AND takeover.transferred_sequences = ARRAY(
                  SELECT exact_unit.sequence::integer
                  FROM public.master_data_import_units AS exact_unit
                  WHERE exact_unit.school_id = v_school_id
                    AND exact_unit.batch_id = p_batch_id
                    AND exact_unit.last_attempt_idempotency_record_id = new_record.id
                    AND exact_unit.last_attempt_completed IS FALSE
                    AND exact_unit.status IN ('pending', 'ready', 'failed')
                  ORDER BY exact_unit.sequence
              )
              AND takeover.transferred_unit_versions = ARRAY(
                  SELECT exact_unit.version
                  FROM public.master_data_import_units AS exact_unit
                  WHERE exact_unit.school_id = v_school_id
                    AND exact_unit.batch_id = p_batch_id
                    AND exact_unit.last_attempt_idempotency_record_id = new_record.id
                    AND exact_unit.last_attempt_completed IS FALSE
                    AND exact_unit.status IN ('pending', 'ready', 'failed')
                  ORDER BY exact_unit.sequence
              )
              AND EXISTS (
                  SELECT 1
                  FROM public.master_data_import_units AS transferred_unit
                  WHERE transferred_unit.school_id = v_school_id
                    AND transferred_unit.batch_id = p_batch_id
                    AND transferred_unit.last_attempt_idempotency_record_id = new_record.id
                    AND transferred_unit.last_attempt_completed IS FALSE
                    AND transferred_unit.status IN ('pending', 'ready', 'failed')
              )
              AND NOT EXISTS (
                  SELECT 1
                  FROM public.master_data_import_units AS incomplete_unit
                  WHERE incomplete_unit.school_id = v_school_id
                    AND incomplete_unit.batch_id = p_batch_id
                    AND incomplete_unit.last_attempt_completed IS FALSE
                    AND (
                        incomplete_unit.last_attempt_idempotency_record_id
                            IS DISTINCT FROM new_record.id
                        OR incomplete_unit.status NOT IN ('pending', 'ready', 'failed')
                    )
              );
            v_bundle_matches := v_takeover_id IS NOT NULL AND v_new_record_id IS NOT NULL;
            IF v_bundle_matches IS DISTINCT FROM true THEN
                RAISE EXCEPTION 'ordinary import takeover bundle is incomplete or stale'
                    USING ERRCODE = '23514';
            END IF;

            UPDATE public.command_idempotency_records AS old_record
            SET status = 'terminal_failure',
                result_type = 'master_data.import_outcome',
                result_payload = pg_catalog.jsonb_build_object(
                    'resource_type', 'master_data.import_batch',
                    'resource_id', p_batch_id::text,
                    'resource_version', p_batch_version_before,
                    'outcome_code', 'master_data.import.command_superseded'
                ),
                updated_at = v_now
            WHERE old_record.school_id = v_school_id
              AND old_record.id = p_record_id
              AND old_record.status = 'in_progress'
              AND old_record.request_sha256 = p_expected_request_sha256
              AND old_record.result_type IS NULL
              AND old_record.actor_context_type = 'assignment'
              AND old_record.actor_assignment_id IS NOT NULL
              AND old_record.actor_assignment_version IS NOT NULL
              AND old_record.action_code IN (
                  'master_data.import.ordinary_confirm',
                  'master_data.import.ordinary_resume'
              )
              AND old_record.resource_type = 'master_data.import_batch'
              AND old_record.resource_id = p_batch_id::text;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'ordinary import takeover source CAS failed'
                    USING ERRCODE = '40001';
            END IF;

            PERFORM app.append_master_data_import_audit(
                v_actor_user_id,
                v_school_id,
                v_actor_person_id,
                v_actor_assignment_id,
                v_actor_assignment_version,
                v_idempotency_key_hash,
                'master_data.import.taken_over',
                'master_data_import_takeover',
                v_takeover_id,
                p_batch_id,
                NULL,
                pg_catalog.jsonb_build_object(
                    'state_code', 'ordinary_import.active_command',
                    'resource_version', NULL,
                    'stable_reference_ids', pg_catalog.jsonb_build_array(p_batch_id::text),
                    'fact_codes', '[]'::jsonb
                ),
                pg_catalog.jsonb_build_object(
                    'state_code', 'ordinary_import.taken_over',
                    'resource_version', NULL,
                    'stable_reference_ids', pg_catalog.jsonb_build_array(
                        p_batch_id::text, v_takeover_id::text
                    ),
                    'fact_codes', '["ordinary_import.taken_over"]'::jsonb
                ),
                v_request_id,
                'manual.ordinary-import-taken-over'
            );
            PERFORM app.assert_master_data_import_takeover_integrity(
                v_school_id,
                p_batch_id,
                v_new_record_id
            );
            RETURN true;
        END
        $function$
        """
    )
    op.execute(f"REVOKE ALL ON FUNCTION {_PUBLIC_SUPERSEDE} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {_PUBLIC_SUPERSEDE} TO campus_ai_runtime")
    op.execute("SET LOCAL ROLE campus_ai_db_owner")
    op.execute("REVOKE CREATE ON SCHEMA app, app_private FROM campus_ai_takeover_definer")


def upgrade() -> None:
    """Install durable attempt, manifest, integrity, CAS, and audit boundaries."""

    _validate_revision_10_takeover_history()
    op.execute("SET LOCAL ROLE campus_ai_db_owner")
    op.execute("GRANT CREATE ON SCHEMA app, app_private TO campus_ai_audit_definer")
    op.execute("SET LOCAL ROLE campus_ai_audit_definer")
    op.execute(f"ALTER FUNCTION {_PUBLIC_AUDIT} RENAME TO append_master_data_import_audit_xid")
    op.execute(
        f"ALTER FUNCTION app.append_master_data_import_audit_xid({_AUDIT_ARGUMENTS}) "
        "SET SCHEMA app_private"
    )
    op.execute(
        f"REVOKE ALL ON FUNCTION {_PRIVATE_XID_AUDIT} FROM PUBLIC, campus_ai_runtime, "
        "campus_ai_db_owner, campus_ai_takeover_definer"
    )
    op.execute(
        """
        CREATE FUNCTION app.append_master_data_import_audit(
            p_actor_user_id integer,
            p_school_id uuid,
            p_actor_person_id uuid,
            p_actor_assignment_id uuid,
            p_actor_assignment_version bigint,
            p_idempotency_key_hash text,
            p_event_type text,
            p_target_type text,
            p_target_id uuid,
            p_batch_id uuid,
            p_unit_id uuid,
            p_before_summary jsonb,
            p_after_summary jsonb,
            p_request_id uuid,
            p_reason_code text
        )
        RETURNS bigint
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog
        AS $function$
        DECLARE
            v_audit_id bigint;
            v_school_id uuid;
            v_actor_user_id integer;
            v_actor_person_id uuid;
            v_actor_assignment_id uuid;
            v_actor_assignment_version bigint;
            v_idempotency_key_hash text;
            v_request_id uuid;
            v_now timestamp with time zone := pg_catalog.clock_timestamp();
            v_authorized boolean := false;
            v_fact_matches boolean := false;
        BEGIN
            IF p_event_type IS DISTINCT FROM 'master_data.import.taken_over' THEN
                RETURN app_private.append_master_data_import_audit_xid(
                    p_actor_user_id,
                    p_school_id,
                    p_actor_person_id,
                    p_actor_assignment_id,
                    p_actor_assignment_version,
                    p_idempotency_key_hash,
                    p_event_type,
                    p_target_type,
                    p_target_id,
                    p_batch_id,
                    p_unit_id,
                    p_before_summary,
                    p_after_summary,
                    p_request_id,
                    p_reason_code
                );
            END IF;

            v_school_id := app.require_school_context();
            IF current_setting('app.context_purpose', true) IS DISTINCT FROM 'command'
               OR current_setting('app.context_kind', true) IS DISTINCT FROM 'assignment'
               OR current_setting('app.guardian_relationship_id', true) IS DISTINCT FROM '-' THEN
                RAISE EXCEPTION 'master data import audit requires staff command context'
                    USING ERRCODE = '28000';
            END IF;
            v_actor_user_id := current_setting('app.account_user_id', true)::integer;
            v_actor_person_id := current_setting('app.person_id', true)::uuid;
            v_actor_assignment_id := current_setting('app.assignment_id', true)::uuid;
            v_actor_assignment_version :=
                current_setting('app.assignment_version', true)::bigint;
            v_idempotency_key_hash := current_setting('app.idempotency_key_hash', true);
            v_request_id := current_setting('app.request_id', true)::uuid;
            IF p_actor_user_id IS DISTINCT FROM v_actor_user_id
               OR p_school_id IS DISTINCT FROM v_school_id
               OR p_actor_person_id IS DISTINCT FROM v_actor_person_id
               OR p_actor_assignment_id IS DISTINCT FROM v_actor_assignment_id
               OR p_actor_assignment_version IS DISTINCT FROM v_actor_assignment_version
               OR p_idempotency_key_hash IS DISTINCT FROM v_idempotency_key_hash
               OR p_request_id IS DISTINCT FROM v_request_id THEN
                RAISE EXCEPTION 'master data import audit context mismatch'
                    USING ERRCODE = '28000';
            END IF;

            SELECT true
            INTO v_authorized
            FROM public.staff_assignments AS assignment
            WHERE assignment.school_id = v_school_id
              AND assignment.id = v_actor_assignment_id
              AND assignment.person_id = v_actor_person_id
              AND assignment.version = v_actor_assignment_version
              AND assignment.status = 'confirmed'
              AND assignment.semantic_role_code = 'school_master_data_manager'
              AND assignment.valid_from <= v_now
              AND (assignment.valid_to IS NULL OR assignment.valid_to > v_now);
            IF NOT COALESCE(v_authorized, false) THEN
                RAISE EXCEPTION 'master data import audit assignment is not authorized'
                    USING ERRCODE = '28000';
            END IF;

            IF p_target_type IS DISTINCT FROM 'master_data_import_takeover'
               OR p_unit_id IS NOT NULL
               OR p_reason_code IS DISTINCT FROM 'manual.ordinary-import-taken-over'
               OR p_before_summary IS DISTINCT FROM pg_catalog.jsonb_build_object(
                    'state_code', 'ordinary_import.active_command',
                    'resource_version', NULL,
                    'stable_reference_ids', pg_catalog.jsonb_build_array(p_batch_id::text),
                    'fact_codes', '[]'::jsonb
               )
               OR p_after_summary IS DISTINCT FROM pg_catalog.jsonb_build_object(
                    'state_code', 'ordinary_import.taken_over',
                    'resource_version', NULL,
                    'stable_reference_ids', pg_catalog.jsonb_build_array(
                        p_batch_id::text, p_target_id::text
                    ),
                    'fact_codes', '["ordinary_import.taken_over"]'::jsonb
               ) THEN
                RAISE EXCEPTION 'unsafe master data import audit record'
                    USING ERRCODE = '22023';
            END IF;

            SELECT EXISTS (
                SELECT 1
                FROM public.master_data_import_takeovers AS takeover
                JOIN public.master_data_import_batches AS batch
                  ON batch.school_id = takeover.school_id
                 AND batch.id = takeover.batch_id
                JOIN public.command_idempotency_records AS previous_record
                  ON previous_record.school_id = takeover.school_id
                 AND previous_record.id = takeover.previous_idempotency_record_id
                JOIN public.command_idempotency_records AS new_record
                  ON new_record.school_id = takeover.school_id
                 AND new_record.id = takeover.new_idempotency_record_id
                WHERE takeover.school_id = v_school_id
                  AND takeover.id = p_target_id
                  AND takeover.batch_id = p_batch_id
                  AND takeover.original_actor_user_id = previous_record.actor_user_id
                  AND takeover.original_actor_person_id = previous_record.actor_person_id
                  AND takeover.original_actor_assignment_id =
                      previous_record.actor_assignment_id
                  AND takeover.original_actor_assignment_version =
                      previous_record.actor_assignment_version
                  AND takeover.new_actor_user_id = v_actor_user_id
                  AND takeover.new_actor_person_id = v_actor_person_id
                  AND takeover.new_actor_assignment_id = v_actor_assignment_id
                  AND takeover.new_actor_assignment_version = v_actor_assignment_version
                  AND takeover.request_id = v_request_id
                  AND takeover.batch_version_before + 1 = takeover.batch_version_after
                  AND batch.mode = 'ordinary_atomic_units'
                  AND batch.source_kind = 'excel'
                  AND batch.status = 'applying'
                  AND batch.version = takeover.batch_version_after
                  AND batch.active_idempotency_record_id = new_record.id
                  AND batch.active_action_code = 'master_data.import.ordinary_takeover'
                  AND previous_record.status = 'terminal_failure'
                  AND previous_record.action_code IN (
                      'master_data.import.ordinary_confirm',
                      'master_data.import.ordinary_resume'
                  )
                  AND previous_record.resource_type = 'master_data.import_batch'
                  AND previous_record.resource_id = batch.id::text
                  AND previous_record.result_type = 'master_data.import_outcome'
                  AND previous_record.result_payload = pg_catalog.jsonb_build_object(
                      'resource_type', 'master_data.import_batch',
                      'resource_id', batch.id::text,
                      'resource_version', takeover.batch_version_before,
                      'outcome_code', 'master_data.import.command_superseded'
                  )
                  AND new_record.action_code = 'master_data.import.ordinary_takeover'
                  AND new_record.actor_user_id = v_actor_user_id
                  AND new_record.actor_person_id = v_actor_person_id
                  AND new_record.actor_assignment_id = v_actor_assignment_id
                  AND new_record.actor_assignment_version = v_actor_assignment_version
                  AND new_record.resource_type = 'master_data.import_batch'
                  AND new_record.resource_id = batch.id::text
                  AND new_record.idempotency_key_hash = v_idempotency_key_hash
                  AND new_record.status = 'in_progress'
                  AND new_record.result_type IS NULL
            ) INTO v_fact_matches;
            IF v_fact_matches IS DISTINCT FROM true THEN
                RAISE EXCEPTION 'master data import audit fact mismatch'
                    USING ERRCODE = '23514';
            END IF;

            INSERT INTO public.audit_logs (
                actor_user_id, school_id, actor_person_id, actor_assignment_id,
                actor_assignment_version, actor_context_type,
                idempotency_key_hash, event_type, target_type, target_id,
                before_summary, after_summary, request_id, reason
            ) VALUES (
                v_actor_user_id, v_school_id, v_actor_person_id, v_actor_assignment_id,
                v_actor_assignment_version, 'assignment',
                v_idempotency_key_hash, p_event_type, p_target_type, p_target_id::text,
                p_before_summary, p_after_summary, v_request_id::text, p_reason_code
            )
            RETURNING id INTO v_audit_id;
            RETURN v_audit_id;
        END
        $function$
        """
    )
    op.execute(f"REVOKE ALL ON FUNCTION {_PUBLIC_AUDIT} FROM PUBLIC")
    op.execute(
        f"GRANT EXECUTE ON FUNCTION {_PUBLIC_AUDIT} "
        "TO campus_ai_runtime, campus_ai_db_owner, campus_ai_takeover_definer"
    )
    op.execute("SET LOCAL ROLE campus_ai_db_owner")
    op.execute(
        "REVOKE SELECT (xmin) ON TABLE public.master_data_import_takeovers "
        "FROM campus_ai_audit_definer"
    )
    op.execute("REVOKE CREATE ON SCHEMA app, app_private FROM campus_ai_audit_definer")
    _upgrade_takeover_acl_and_preserve_revision_10()
    _create_attempt_state_and_manifest_boundaries()
    _create_deferred_takeover_integrity_boundaries()
    _create_durable_supersede_boundary()


def _downgrade_takeover_boundary() -> None:
    """移除 revision 11 边界并精确恢复已保存的 revision 10 定义。"""

    op.execute("SET LOCAL ROLE campus_ai_db_owner")
    for trigger_name, table_name in (
        ("enforce_master_data_import_attempt_transition", "master_data_import_units"),
        (
            "enforce_master_data_import_takeover_insert_integrity",
            "master_data_import_takeovers",
        ),
        (
            "enforce_master_data_import_takeover_batch_integrity",
            "master_data_import_batches",
        ),
        (
            "enforce_master_data_import_takeover_command_integrity",
            "command_idempotency_records",
        ),
        ("populate_master_data_import_takeover_manifest", "master_data_import_takeovers"),
        ("enforce_master_data_import_unit_immutability", "master_data_import_units"),
    ):
        op.execute(f"DROP TRIGGER {trigger_name} ON public.{table_name}")

    op.execute("GRANT CREATE ON SCHEMA app TO campus_ai_takeover_definer")
    op.execute("GRANT USAGE, CREATE ON SCHEMA app_private TO campus_ai_takeover_definer")
    op.execute("SET LOCAL ROLE campus_ai_takeover_definer")
    op.execute(f"DROP FUNCTION {_PUBLIC_SUPERSEDE}")
    op.execute(f"DROP FUNCTION {_PUBLIC_MANIFEST}")
    op.execute(f"DROP FUNCTION {_ATTEMPT_TRANSITION}")
    op.execute(f"DROP FUNCTION {_TAKEOVER_INTEGRITY_TRIGGER}")
    op.execute(f"DROP FUNCTION {_ASSERT_TAKEOVER}")
    op.execute(f"ALTER FUNCTION {_PRIVATE_XID_SUPERSEDE} SET SCHEMA app")
    op.execute(
        "ALTER FUNCTION app.supersede_master_data_import_takeover_source_xid"
        f"({_SUPERSEDE_ARGUMENTS}) RENAME TO supersede_master_data_import_takeover_source"
    )
    op.execute(
        f"ALTER FUNCTION {_PRIVATE_XID_MANIFEST} "
        "RENAME TO populate_master_data_import_takeover_manifest"
    )
    op.execute(f"GRANT EXECUTE ON FUNCTION {_PUBLIC_MANIFEST} TO campus_ai_db_owner")
    op.execute(f"REVOKE ALL ON FUNCTION {_PUBLIC_SUPERSEDE} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {_PUBLIC_SUPERSEDE} TO campus_ai_runtime")

    op.execute("SET LOCAL ROLE campus_ai_db_owner")
    op.execute(f"DROP FUNCTION {_PUBLIC_UNIT_GUARD}")
    op.execute(
        f"ALTER FUNCTION {_PRIVATE_REVISION_10_UNIT_GUARD} "
        "RENAME TO enforce_master_data_import_unit_immutability"
    )
    op.execute(
        "CREATE TRIGGER enforce_master_data_import_unit_immutability "
        "BEFORE UPDATE ON public.master_data_import_units FOR EACH ROW "
        f"EXECUTE FUNCTION {_PUBLIC_UNIT_GUARD}"
    )
    op.execute(
        "CREATE TRIGGER populate_master_data_import_takeover_manifest "
        "BEFORE INSERT ON public.master_data_import_takeovers FOR EACH ROW "
        f"EXECUTE FUNCTION {_PUBLIC_MANIFEST}"
    )
    op.execute("SET LOCAL ROLE campus_ai_takeover_definer")
    op.execute(f"REVOKE EXECUTE ON FUNCTION {_PUBLIC_MANIFEST} FROM campus_ai_db_owner")
    op.execute("SET LOCAL ROLE campus_ai_db_owner")
    op.execute("DROP POLICY takeover_definer_tenant_select ON public.audit_logs")
    op.execute(
        "REVOKE SELECT (id, actor_user_id, school_id, actor_person_id, "
        "actor_assignment_id, actor_assignment_version, actor_context_type, "
        "idempotency_key_hash, event_type, target_type, target_id, request_id, reason) "
        "ON TABLE public.audit_logs FROM campus_ai_takeover_definer"
    )
    op.execute(
        "REVOKE SELECT (result_payload) ON TABLE public.command_idempotency_records "
        "FROM campus_ai_takeover_definer"
    )
    for table_name in (
        "command_idempotency_records",
        "master_data_import_batches",
        "master_data_import_takeovers",
        "master_data_import_units",
    ):
        op.execute(
            f"GRANT SELECT (xmin) ON TABLE public.{table_name} TO campus_ai_takeover_definer"
        )
    op.execute("REVOKE CREATE ON SCHEMA app, app_private FROM campus_ai_takeover_definer")


def downgrade() -> None:
    """Restore the exact revision-10 takeover and audit boundaries."""

    _downgrade_takeover_boundary()
    op.execute("SET LOCAL ROLE campus_ai_db_owner")
    op.execute("GRANT CREATE ON SCHEMA app, app_private TO campus_ai_audit_definer")
    op.execute("SET LOCAL ROLE campus_ai_audit_definer")
    op.execute(f"DROP FUNCTION {_PUBLIC_AUDIT}")
    op.execute(f"ALTER FUNCTION {_PRIVATE_XID_AUDIT} SET SCHEMA app")
    op.execute(
        f"ALTER FUNCTION app.append_master_data_import_audit_xid({_AUDIT_ARGUMENTS}) "
        "RENAME TO append_master_data_import_audit"
    )
    op.execute(f"REVOKE ALL ON FUNCTION {_PUBLIC_AUDIT} FROM PUBLIC")
    op.execute(
        f"GRANT EXECUTE ON FUNCTION {_PUBLIC_AUDIT} "
        "TO campus_ai_runtime, campus_ai_db_owner, campus_ai_takeover_definer"
    )
    op.execute("SET LOCAL ROLE campus_ai_db_owner")
    op.execute(
        "GRANT SELECT (xmin) ON TABLE public.master_data_import_takeovers "
        "TO campus_ai_audit_definer"
    )
    op.execute("REVOKE CREATE ON SCHEMA app, app_private FROM campus_ai_audit_definer")
