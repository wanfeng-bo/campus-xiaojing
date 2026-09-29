"""建立普通导入显式接管的窄数据库边界。

Revision ID: 20260809_10
Revises: 20260808_09
Create Date: 2026-08-09
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20260809_10"
down_revision: str | None = "20260808_09"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_LOCK_SOURCE_SIGNATURE = "app.lock_master_data_import_takeover_source(uuid,uuid,uuid)"
_LOCK_ASSIGNMENT_SIGNATURE = "app.lock_master_data_import_takeover_assignment(uuid,uuid,uuid,uuid)"
_SUPERSEDE_SOURCE_SIGNATURE = (
    "app.supersede_master_data_import_takeover_source(uuid,uuid,uuid,text,bigint)"
)
_IMPORT_AUDIT_SIGNATURE = (
    "app.append_master_data_import_audit(integer,uuid,uuid,uuid,bigint,text,text,"
    "text,uuid,uuid,uuid,jsonb,jsonb,uuid,text)"
)
_POPULATE_MANIFEST_SIGNATURE = (
    "app_private.populate_master_data_import_takeover_manifest()"
)


def _add_takeover_manifest_columns() -> None:
    """为既有追加事实补齐可验证的 DB-owned 转移溯源。"""

    op.execute("SET LOCAL ROLE campus_ai_db_owner")
    op.execute(
        "ALTER TABLE public.master_data_import_takeovers "
        "ADD COLUMN transferred_sequences integer[], "
        "ADD COLUMN transferred_unit_versions bigint[]"
    )
    op.execute(
        "DROP TRIGGER reject_master_data_import_takeover_row_mutation "
        "ON public.master_data_import_takeovers"
    )
    op.execute(
        """
        WITH manifests AS (
            SELECT takeover.school_id,
                   takeover.id,
                   pg_catalog.array_agg(unit.sequence::integer ORDER BY unit.sequence)
                       AS transferred_sequences,
                   pg_catalog.array_agg(unit.version ORDER BY unit.sequence)
                       AS transferred_unit_versions
            FROM public.master_data_import_takeovers AS takeover
            JOIN public.master_data_import_units AS unit
              ON unit.school_id = takeover.school_id
             AND unit.batch_id = takeover.batch_id
             AND (
                 unit.last_attempt_idempotency_record_id =
                     takeover.new_idempotency_record_id
                 OR (
                     unit.last_attempt_idempotency_record_id =
                         takeover.previous_idempotency_record_id
                     AND unit.last_attempt_completed IS FALSE
                 )
             )
            GROUP BY takeover.school_id, takeover.id
        )
        UPDATE public.master_data_import_takeovers AS takeover
        SET transferred_sequences = manifest.transferred_sequences,
            transferred_unit_versions = manifest.transferred_unit_versions
        FROM manifests AS manifest
        WHERE takeover.school_id = manifest.school_id
          AND takeover.id = manifest.id
        """
    )
    op.execute(
        """
        CREATE TRIGGER reject_master_data_import_takeover_row_mutation
        BEFORE UPDATE OR DELETE ON public.master_data_import_takeovers
        FOR EACH ROW
        EXECUTE FUNCTION app_private.reject_master_data_import_takeover_mutation()
        """
    )
    op.execute(
        """
        DO $block$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM public.master_data_import_takeovers AS takeover
                WHERE takeover.transferred_sequences IS NULL
                   OR pg_catalog.cardinality(takeover.transferred_sequences) NOT BETWEEN 1 AND 500
                   OR takeover.transferred_unit_versions IS NULL
                   OR pg_catalog.cardinality(takeover.transferred_sequences)
                      <> pg_catalog.cardinality(takeover.transferred_unit_versions)
            ) THEN
                RAISE EXCEPTION 'existing takeover has no reconstructable transfer manifest'
                    USING ERRCODE = '23514';
            END IF;
        END
        $block$
        """
    )
    op.execute(
        "ALTER TABLE public.master_data_import_takeovers "
        "ALTER COLUMN transferred_sequences SET NOT NULL, "
        "ALTER COLUMN transferred_unit_versions SET NOT NULL, "
        "ADD CONSTRAINT ck_master_data_import_takeovers_manifest_cardinality CHECK ("
        "cardinality(transferred_sequences) BETWEEN 1 AND 500 "
        "AND cardinality(transferred_sequences) = cardinality(transferred_unit_versions)), "
        "ADD CONSTRAINT ck_master_data_import_takeovers_manifest_positive CHECK ("
        "array_position(transferred_sequences, NULL) IS NULL "
        "AND array_position(transferred_unit_versions, NULL) IS NULL "
        "AND 0 < ALL(transferred_sequences) "
        "AND 0 < ALL(transferred_unit_versions))"
    )


def _grant_takeover_definer_boundaries() -> None:
    """仅授予专用 definer 固定函数所需的精确列权限。"""

    op.execute("SET LOCAL ROLE campus_ai_db_owner")
    for table_name in (
        "command_idempotency_records",
        "master_data_import_batches",
        "master_data_import_units",
        "master_data_import_takeovers",
        "staff_assignments",
    ):
        op.execute(
            f"REVOKE ALL PRIVILEGES ON TABLE public.{table_name} "
            "FROM campus_ai_takeover_definer CASCADE"
        )
    op.execute(
        "GRANT SELECT (school_id, id, action_code, actor_user_id, actor_person_id, "
        "actor_context_type, actor_assignment_id, actor_assignment_version, resource_type, "
        "resource_id, idempotency_key_hash, request_sha256, status, result_type, created_at, "
        "updated_at) ON TABLE public.command_idempotency_records "
        "TO campus_ai_takeover_definer"
    )
    op.execute(
        "GRANT UPDATE (status, result_type, result_payload, updated_at) "
        "ON TABLE public.command_idempotency_records TO campus_ai_takeover_definer"
    )
    op.execute(
        "GRANT SELECT (school_id, id, mode, status, active_idempotency_record_id, "
        "active_action_code, source_kind, planned_units, version) "
        "ON TABLE public.master_data_import_batches "
        "TO campus_ai_takeover_definer"
    )
    op.execute(
        "GRANT SELECT (school_id, id, batch_id, status, sequence, "
        "last_attempt_idempotency_record_id, last_attempt_completed, version) "
        "ON TABLE public.master_data_import_units TO campus_ai_takeover_definer"
    )
    op.execute(
        "GRANT SELECT (school_id, id, batch_id, previous_idempotency_record_id, "
        "new_idempotency_record_id, original_actor_user_id, original_actor_person_id, "
        "original_actor_assignment_id, original_actor_assignment_version, "
        "new_actor_user_id, new_actor_person_id, new_actor_assignment_id, "
        "new_actor_assignment_version, request_id, batch_version_before, "
        "batch_version_after, transferred_sequences, transferred_unit_versions) "
        "ON TABLE public.master_data_import_takeovers "
        "TO campus_ai_takeover_definer"
    )
    op.execute(
        "GRANT SELECT (school_id, id, person_id, semantic_role_code, valid_from, valid_to, "
        "status, version) ON TABLE public.staff_assignments TO campus_ai_takeover_definer"
    )
    op.execute("GRANT UPDATE (id) ON TABLE public.staff_assignments TO campus_ai_takeover_definer")
    for table_name in (
        "command_idempotency_records",
        "master_data_import_batches",
        "master_data_import_units",
        "master_data_import_takeovers",
    ):
        op.execute(
            f"GRANT SELECT (xmin) ON TABLE public.{table_name} TO campus_ai_takeover_definer"
        )
    op.execute("GRANT USAGE ON SCHEMA public, app TO campus_ai_takeover_definer")
    op.execute("SET LOCAL ROLE campus_ai_context_definer")
    op.execute(
        "GRANT EXECUTE ON FUNCTION app.require_school_context() TO campus_ai_takeover_definer"
    )
    op.execute("SET LOCAL ROLE campus_ai_audit_definer")
    op.execute(f"GRANT EXECUTE ON FUNCTION {_IMPORT_AUDIT_SIGNATURE} TO campus_ai_takeover_definer")
    op.execute("SET LOCAL ROLE campus_ai_db_owner")


def _create_takeover_rls_policies() -> None:
    """在保持 FORCE RLS 时，仅允许 definer 读取签名上下文对应学校的行。"""

    op.execute("SET LOCAL ROLE campus_ai_db_owner")
    op.execute(
        "CREATE POLICY takeover_definer_tenant_select "
        "ON public.command_idempotency_records FOR SELECT "
        "TO campus_ai_takeover_definer "
        "USING (school_id = (SELECT app.require_school_context()))"
    )
    op.execute(
        "CREATE POLICY takeover_definer_tenant_update "
        "ON public.command_idempotency_records FOR UPDATE "
        "TO campus_ai_takeover_definer "
        "USING (school_id = (SELECT app.require_school_context())) "
        "WITH CHECK (school_id = (SELECT app.require_school_context()))"
    )
    for table_name in (
        "master_data_import_batches",
        "master_data_import_units",
        "master_data_import_takeovers",
        "staff_assignments",
    ):
        op.execute(
            "CREATE POLICY takeover_definer_tenant_select "
            f"ON public.{table_name} FOR SELECT "
            "TO campus_ai_takeover_definer "
            "USING (school_id = (SELECT app.require_school_context()))"
        )


def _create_lock_source_function() -> None:
    """创建第三步窄函数，只锁定批次当前活动的旧命令行。"""

    op.execute("SET LOCAL ROLE campus_ai_db_owner")
    op.execute("GRANT CREATE ON SCHEMA app TO campus_ai_takeover_definer")
    op.execute("SET LOCAL ROLE campus_ai_takeover_definer")
    op.execute(
        """
        CREATE FUNCTION app.lock_master_data_import_takeover_source(
            p_school_id uuid,
            p_batch_id uuid,
            p_record_id uuid
        )
        RETURNS TABLE (
            record_id uuid,
            status text,
            request_sha256 text,
            result_type text,
            created_at timestamp with time zone,
            updated_at timestamp with time zone,
            action_code text,
            actor_user_id integer,
            actor_person_id uuid,
            actor_context_type text,
            actor_assignment_id uuid,
            actor_assignment_version bigint,
            resource_type text,
            resource_id text,
            idempotency_key_hash text
        )
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog
        AS $function$
        DECLARE
            v_school_id uuid := app.require_school_context();
            v_now timestamp with time zone := pg_catalog.clock_timestamp();
            v_authorized boolean := false;
        BEGIN
            IF p_school_id IS DISTINCT FROM v_school_id
               OR current_setting('app.context_purpose', true) IS DISTINCT FROM 'command'
               OR current_setting('app.context_kind', true) IS DISTINCT FROM 'assignment'
               OR current_setting('app.guardian_relationship_id', true) IS DISTINCT FROM '-' THEN
                RAISE EXCEPTION 'ordinary import takeover requires staff command context'
                    USING ERRCODE = '28000';
            END IF;
            SELECT true
            INTO v_authorized
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
              );
            IF NOT COALESCE(v_authorized, false) THEN
                RAISE EXCEPTION 'ordinary import takeover requires effective manager assignment'
                    USING ERRCODE = '42501';
            END IF;

            RETURN QUERY
            SELECT old_record.id,
                   old_record.status::text,
                   old_record.request_sha256::text,
                   old_record.result_type::text,
                   old_record.created_at,
                   old_record.updated_at,
                   old_record.action_code::text,
                   old_record.actor_user_id,
                   old_record.actor_person_id,
                   old_record.actor_context_type::text,
                   old_record.actor_assignment_id,
                   old_record.actor_assignment_version,
                   old_record.resource_type::text,
                   old_record.resource_id::text,
                   old_record.idempotency_key_hash::text
            FROM public.command_idempotency_records AS old_record
            JOIN public.master_data_import_batches AS batch
              ON batch.school_id = old_record.school_id
             AND batch.id = p_batch_id
             AND batch.active_idempotency_record_id = old_record.id
             AND batch.active_action_code = old_record.action_code
            WHERE old_record.school_id = v_school_id
              AND old_record.id = p_record_id
              AND old_record.status = 'in_progress'
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
              AND batch.mode = 'ordinary_atomic_units'
              AND batch.status = 'applying'
            FOR UPDATE OF old_record;
        END
        $function$
        """
    )
    op.execute(f"REVOKE ALL ON FUNCTION {_LOCK_SOURCE_SIGNATURE} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {_LOCK_SOURCE_SIGNATURE} TO campus_ai_runtime")
    op.execute("SET LOCAL ROLE campus_ai_db_owner")
    op.execute("REVOKE CREATE ON SCHEMA app FROM campus_ai_takeover_definer")


def _create_lock_assignment_function() -> None:
    """创建第四步窄函数，只锁定旧命令引用的原任职。"""

    op.execute("SET LOCAL ROLE campus_ai_db_owner")
    op.execute("GRANT CREATE ON SCHEMA app TO campus_ai_takeover_definer")
    op.execute("SET LOCAL ROLE campus_ai_takeover_definer")
    op.execute(
        """
        CREATE FUNCTION app.lock_master_data_import_takeover_assignment(
            p_school_id uuid,
            p_batch_id uuid,
            p_record_id uuid,
            p_assignment_id uuid
        )
        RETURNS TABLE (
            assignment_id uuid,
            person_id uuid,
            version bigint,
            status text,
            valid_from timestamp with time zone,
            valid_to timestamp with time zone
        )
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog
        AS $function$
        DECLARE
            v_school_id uuid := app.require_school_context();
            v_now timestamp with time zone := pg_catalog.clock_timestamp();
            v_authorized boolean := false;
            v_source_matches boolean := false;
        BEGIN
            IF p_school_id IS DISTINCT FROM v_school_id
               OR current_setting('app.context_purpose', true) IS DISTINCT FROM 'command'
               OR current_setting('app.context_kind', true) IS DISTINCT FROM 'assignment'
               OR current_setting('app.guardian_relationship_id', true) IS DISTINCT FROM '-' THEN
                RAISE EXCEPTION 'ordinary import takeover requires staff command context'
                    USING ERRCODE = '28000';
            END IF;
            SELECT true
            INTO v_authorized
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
              );
            IF NOT COALESCE(v_authorized, false) THEN
                RAISE EXCEPTION 'ordinary import takeover requires effective manager assignment'
                    USING ERRCODE = '42501';
            END IF;

            SELECT true
            INTO v_source_matches
            FROM public.command_idempotency_records AS old_record
            JOIN public.master_data_import_batches AS batch
              ON batch.school_id = old_record.school_id
             AND batch.id = p_batch_id
             AND batch.active_idempotency_record_id = old_record.id
             AND batch.active_action_code = old_record.action_code
            WHERE old_record.school_id = v_school_id
              AND old_record.id = p_record_id
              AND old_record.actor_context_type = 'assignment'
              AND old_record.actor_assignment_id = p_assignment_id
              AND old_record.status = 'in_progress'
              AND old_record.action_code IN (
                  'master_data.import.ordinary_confirm',
                  'master_data.import.ordinary_resume'
              )
              AND old_record.resource_type = 'master_data.import_batch'
              AND old_record.resource_id = p_batch_id::text
              AND batch.mode = 'ordinary_atomic_units'
              AND batch.status = 'applying';
            IF NOT COALESCE(v_source_matches, false) THEN
                RETURN;
            END IF;

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
            FOR UPDATE OF original_assignment;
        END
        $function$
        """
    )
    op.execute(f"REVOKE ALL ON FUNCTION {_LOCK_ASSIGNMENT_SIGNATURE} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {_LOCK_ASSIGNMENT_SIGNATURE} TO campus_ai_runtime")
    op.execute("SET LOCAL ROLE campus_ai_db_owner")
    op.execute("REVOKE CREATE ON SCHEMA app FROM campus_ai_takeover_definer")


def _create_supersede_source_function() -> None:
    """创建最终固定边界，核验完整同事务 bundle 后 CAS 旧命令并写审计。"""

    op.execute("SET LOCAL ROLE campus_ai_db_owner")
    op.execute("GRANT CREATE ON SCHEMA app TO campus_ai_takeover_definer")
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
            v_current_xid bigint := (
                pg_current_xact_id()::text::bigint % 4294967296
            );
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
                        AND original_assignment.version =
                            old_record.actor_assignment_version
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
              AND takeover.xmin::text::bigint = v_current_xid
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
              AND new_record.xmin::text::bigint = v_current_xid
              AND batch.mode = 'ordinary_atomic_units'
              AND batch.source_kind = 'excel'
              AND batch.status = 'applying'
              AND batch.active_idempotency_record_id = new_record.id
              AND batch.active_action_code = 'master_data.import.ordinary_takeover'
              AND batch.version = takeover.batch_version_after
              AND batch.xmin::text::bigint = v_current_xid
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
                  ORDER BY exact_unit.sequence
              )
              AND takeover.transferred_unit_versions = ARRAY(
                  SELECT exact_unit.version
                  FROM public.master_data_import_units AS exact_unit
                  WHERE exact_unit.school_id = v_school_id
                    AND exact_unit.batch_id = p_batch_id
                    AND exact_unit.last_attempt_idempotency_record_id = new_record.id
                    AND exact_unit.last_attempt_completed IS FALSE
                  ORDER BY exact_unit.sequence
              )
              AND EXISTS (
                  SELECT 1
                  FROM public.master_data_import_units AS transferred_unit
                  WHERE transferred_unit.school_id = v_school_id
                    AND transferred_unit.batch_id = p_batch_id
                    AND transferred_unit.last_attempt_idempotency_record_id = new_record.id
                    AND transferred_unit.last_attempt_completed IS FALSE
                    AND transferred_unit.xmin::text::bigint = v_current_xid
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
                        OR incomplete_unit.xmin::text::bigint <> v_current_xid
                    )
              )
              AND NOT EXISTS (
                  SELECT 1
                  FROM public.master_data_import_units AS changed_unit
                  WHERE changed_unit.school_id = v_school_id
                    AND changed_unit.batch_id = p_batch_id
                    AND changed_unit.xmin::text::bigint = v_current_xid
                    AND (
                        changed_unit.last_attempt_idempotency_record_id
                            IS DISTINCT FROM new_record.id
                        OR changed_unit.last_attempt_completed IS DISTINCT FROM false
                        OR changed_unit.status NOT IN ('pending', 'ready', 'failed')
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
            RETURN true;
        END
        $function$
        """
    )
    op.execute(f"REVOKE ALL ON FUNCTION {_SUPERSEDE_SOURCE_SIGNATURE} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {_SUPERSEDE_SOURCE_SIGNATURE} TO campus_ai_runtime")
    op.execute("SET LOCAL ROLE campus_ai_db_owner")
    op.execute("REVOKE CREATE ON SCHEMA app FROM campus_ai_takeover_definer")


def _create_takeover_manifest_trigger() -> None:
    """在任何应用可改写 unit 之前，由窄 definer 捕获旧命令未完成单元版本。"""

    op.execute("SET LOCAL ROLE campus_ai_db_owner")
    op.execute(
        "GRANT USAGE, CREATE ON SCHEMA app_private TO campus_ai_takeover_definer"
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
            v_current_xid xid8 := pg_current_xact_id();
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
            -- xmin is 32-bit while pg_xact_status consumes xid8.  Rebuild the nearest
            -- full xid around the top-level xid so released SAVEPOINT subxids retain
            -- their independent in-progress status without losing the installation epoch.
            IF EXISTS (
                SELECT 1
                FROM public.master_data_import_units AS source_unit
                WHERE source_unit.school_id = v_school_id
                  AND source_unit.batch_id = NEW.batch_id
                  AND source_unit.last_attempt_idempotency_record_id =
                      NEW.previous_idempotency_record_id
                  AND source_unit.last_attempt_completed IS FALSE
                  AND pg_catalog.pg_xact_status(
                      (
                          v_current_xid::text::bigint
                          + (
                              (
                                  source_unit.xmin::text::bigint
                                  - (v_current_xid::text::bigint % 4294967296)
                                  + 4294967296
                                  + 2147483648
                              ) % 4294967296
                              - 2147483648
                          )
                      )::text::xid8
                  ) = 'in progress'
            ) THEN
                RAISE EXCEPTION
                    'takeover manifest source was changed in current transaction or subtransaction'
                    USING ERRCODE = '23514';
            END IF;
            SELECT pg_catalog.array_agg(source_unit.sequence::integer ORDER BY source_unit.sequence),
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
    op.execute(f"REVOKE ALL ON FUNCTION {_POPULATE_MANIFEST_SIGNATURE} FROM PUBLIC")
    op.execute(
        f"GRANT EXECUTE ON FUNCTION {_POPULATE_MANIFEST_SIGNATURE} TO campus_ai_db_owner"
    )
    op.execute("SET LOCAL ROLE campus_ai_db_owner")
    op.execute(
        "CREATE TRIGGER populate_master_data_import_takeover_manifest "
        "BEFORE INSERT ON public.master_data_import_takeovers "
        f"FOR EACH ROW EXECUTE FUNCTION {_POPULATE_MANIFEST_SIGNATURE}"
    )
    op.execute("SET LOCAL ROLE campus_ai_takeover_definer")
    op.execute(
        f"REVOKE EXECUTE ON FUNCTION {_POPULATE_MANIFEST_SIGNATURE} FROM campus_ai_db_owner"
    )
    op.execute("SET LOCAL ROLE campus_ai_db_owner")
    op.execute(
        "REVOKE USAGE, CREATE ON SCHEMA app_private FROM campus_ai_takeover_definer"
    )


def upgrade() -> None:
    """安装专用角色的 RLS、列权限与三个固定函数。"""

    _add_takeover_manifest_columns()
    _create_takeover_rls_policies()
    _grant_takeover_definer_boundaries()
    _create_lock_source_function()
    _create_lock_assignment_function()
    _create_takeover_manifest_trigger()
    _create_supersede_source_function()


def downgrade() -> None:
    """移除固定函数和 takeover definer 的全部表权限。"""

    op.execute("SET LOCAL ROLE campus_ai_db_owner")
    op.execute(
        "DROP TRIGGER IF EXISTS populate_master_data_import_takeover_manifest "
        "ON public.master_data_import_takeovers"
    )
    op.execute("GRANT USAGE ON SCHEMA app_private TO campus_ai_takeover_definer")
    op.execute("SET LOCAL ROLE campus_ai_takeover_definer")
    op.execute(f"DROP FUNCTION IF EXISTS {_POPULATE_MANIFEST_SIGNATURE}")
    op.execute("SET LOCAL ROLE campus_ai_db_owner")
    op.execute("REVOKE USAGE ON SCHEMA app_private FROM campus_ai_takeover_definer")
    op.execute("SET LOCAL ROLE campus_ai_takeover_definer")
    for signature in (
        _SUPERSEDE_SOURCE_SIGNATURE,
        _LOCK_ASSIGNMENT_SIGNATURE,
        _LOCK_SOURCE_SIGNATURE,
    ):
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC, campus_ai_runtime")
        op.execute(f"DROP FUNCTION {signature}")
    op.execute("SET LOCAL ROLE campus_ai_db_owner")
    op.execute(
        "ALTER TABLE public.master_data_import_takeovers "
        "DROP CONSTRAINT IF EXISTS ck_master_data_import_takeovers_manifest_positive, "
        "DROP CONSTRAINT IF EXISTS ck_master_data_import_takeovers_manifest_cardinality, "
        "DROP COLUMN IF EXISTS transferred_unit_versions, "
        "DROP COLUMN IF EXISTS transferred_sequences"
    )
    for table_name in (
        "staff_assignments",
        "master_data_import_takeovers",
        "master_data_import_units",
        "master_data_import_batches",
        "command_idempotency_records",
    ):
        op.execute(f"DROP POLICY IF EXISTS takeover_definer_tenant_select ON public.{table_name}")
    op.execute(
        "DROP POLICY IF EXISTS takeover_definer_tenant_update ON public.command_idempotency_records"
    )
    for table_name in (
        "command_idempotency_records",
        "master_data_import_batches",
        "master_data_import_units",
        "master_data_import_takeovers",
        "staff_assignments",
    ):
        op.execute(
            f"REVOKE ALL PRIVILEGES ON TABLE public.{table_name} "
            "FROM campus_ai_takeover_definer CASCADE"
        )
    op.execute("SET LOCAL ROLE campus_ai_context_definer")
    op.execute(
        "REVOKE EXECUTE ON FUNCTION app.require_school_context() FROM campus_ai_takeover_definer"
    )
    op.execute("SET LOCAL ROLE campus_ai_audit_definer")
    op.execute(
        f"REVOKE EXECUTE ON FUNCTION {_IMPORT_AUDIT_SIGNATURE} FROM campus_ai_takeover_definer"
    )
    op.execute("SET LOCAL ROLE campus_ai_db_owner")
