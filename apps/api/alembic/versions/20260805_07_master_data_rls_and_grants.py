"""Enforce deterministic master-data RLS and minimum runtime grants.

Revision ID: 20260805_07
Revises: 20260805_06
Create Date: 2026-08-06
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260805_07"
down_revision: str | None = "20260805_06"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_RLS_TABLES = (
    "schools",
    "school_people",
    "school_person_names",
    "account_person_bindings",
    "organization_units",
    "organization_unit_versions",
    "staff_assignments",
    "responsibility_types",
    "responsibility_groups",
    "responsibility_group_versions",
    "responsibility_group_memberships",
    "repair_responsibility_grants",
    "campus_locations",
    "campus_location_versions",
    "administrative_classes",
    "administrative_class_versions",
    "students",
    "student_enrollments",
    "guardian_relationships",
    "school_calendar_versions",
    "school_calendar_days",
    "command_idempotency_records",
    "outbox_messages",
)

_DEFINER_READ_COLUMNS = {
    "users": "id, is_active, security_version",
    "schools": "id, display_name, status",
    "account_person_bindings": (
        "school_id, account_user_id, person_id, valid_from, valid_to, status, version"
    ),
    "staff_assignments": (
        "id, school_id, person_id, organization_unit_id, position_title, "
        "valid_from, valid_to, status, version"
    ),
    "guardian_relationships": (
        "id, school_id, guardian_person_id, valid_from, valid_to, status, version"
    ),
    "organization_unit_versions": (
        "school_id, organization_unit_id, display_name, valid_from, valid_to, status"
    ),
}

_AUDIT_RUNTIME_INSERT_COLUMNS = (
    "actor_user_id, school_id, actor_person_id, actor_assignment_id, "
    "actor_assignment_version, actor_guardian_relationship_id, "
    "actor_guardian_relationship_version, actor_context_type, idempotency_key_hash, "
    "event_type, target_type, target_id, before_summary, after_summary, request_id, reason"
)


def _create_private_key_store() -> None:
    op.execute("CREATE SCHEMA app AUTHORIZATION campus_ai_db_owner")
    op.execute("CREATE SCHEMA app_private AUTHORIZATION campus_ai_db_owner")
    op.execute("REVOKE ALL ON SCHEMA app FROM PUBLIC")
    op.execute("REVOKE ALL ON SCHEMA app_private FROM PUBLIC")
    op.execute("CREATE EXTENSION pgcrypto WITH SCHEMA app_private")
    op.execute(
        """
        CREATE TABLE app_private.context_proof_keys (
            key_version bigint PRIMARY KEY,
            signing_key bytea NOT NULL,
            fingerprint_sha256 text NOT NULL
                CHECK (fingerprint_sha256 ~ '^[0-9a-f]{64}$'),
            is_current boolean NOT NULL,
            created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX uq_context_proof_keys_one_current
        ON app_private.context_proof_keys (is_current)
        WHERE is_current
        """
    )
    op.execute(
        """
        DO $body$
        DECLARE
            configured_key text := current_setting('app.context_signing_key', true);
        BEGIN
            IF configured_key IS NULL OR octet_length(convert_to(configured_key, 'UTF8')) < 32 THEN
                RAISE EXCEPTION 'migration context signing key is missing or too short'
                    USING ERRCODE = '22023';
            END IF;
            INSERT INTO app_private.context_proof_keys (
                key_version, signing_key, fingerprint_sha256, is_current
            )
            VALUES (
                1,
                convert_to(configured_key, 'UTF8'),
                encode(app_private.digest(convert_to(configured_key, 'UTF8'), 'sha256'), 'hex'),
                true
            );
        END
        $body$
        """
    )
    op.execute("REVOKE ALL ON ALL TABLES IN SCHEMA app_private FROM PUBLIC")
    op.execute("REVOKE ALL ON ALL FUNCTIONS IN SCHEMA app_private FROM PUBLIC")
    op.execute("GRANT USAGE ON SCHEMA app_private TO campus_ai_context_definer")
    op.execute(
        "GRANT SELECT (key_version, signing_key, fingerprint_sha256, is_current) "
        "ON app_private.context_proof_keys TO campus_ai_context_definer"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION app_private.hmac(bytea, bytea, text) "
        "TO campus_ai_context_definer"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION app_private.digest(bytea, text) TO campus_ai_context_definer"
    )


def _grant_definer_read_columns() -> None:
    op.execute("GRANT USAGE ON SCHEMA public, app TO campus_ai_context_definer")
    for table_name, columns in _DEFINER_READ_COLUMNS.items():
        op.execute(
            f"GRANT SELECT ({columns}) ON TABLE public.{table_name} TO campus_ai_context_definer"
        )


def _create_context_functions() -> None:
    op.execute("GRANT CREATE ON SCHEMA app TO campus_ai_context_definer")
    op.execute("SET LOCAL ROLE campus_ai_context_definer")
    op.execute("ALTER DEFAULT PRIVILEGES REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC")
    op.execute(
        """
        CREATE FUNCTION app.context_signing_key_fingerprint()
        RETURNS TABLE(key_version bigint, fingerprint_sha256 text)
        LANGUAGE sql
        STABLE
        SECURITY DEFINER
        SET search_path = pg_catalog
        AS $function$
            SELECT keys.key_version, keys.fingerprint_sha256
            FROM app_private.context_proof_keys AS keys
            WHERE keys.is_current
        $function$
        """
    )
    op.execute(
        """
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
                'account-options-v1',
                p_account_user_id::text,
                p_authorization_version::text,
                p_session_nonce_hash,
                to_char(p_proof_expires_at AT TIME ZONE 'UTC',
                        'YYYY-MM-DD"T"HH24:MI:SS.US"Z"')
            ], chr(31));
            expected_proof := encode(
                app_private.hmac(convert_to(canonical_text, 'UTF8'), signing_key, 'sha256'),
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
                   binding.school_id,
                   binding.person_id,
                   school.display_name || ' · ' ||
                       COALESCE(unit_version.display_name, assignment.position_title)
            FROM public.account_person_bindings AS binding
            JOIN public.schools AS school
              ON school.id = binding.school_id AND school.status = 'active'
            JOIN public.staff_assignments AS assignment
              ON assignment.school_id = binding.school_id
             AND assignment.person_id = binding.person_id
            LEFT JOIN public.organization_unit_versions AS unit_version
              ON unit_version.school_id = assignment.school_id
             AND unit_version.organization_unit_id = assignment.organization_unit_id
             AND unit_version.status = 'confirmed'
             AND unit_version.valid_from <= as_of
             AND (unit_version.valid_to IS NULL OR unit_version.valid_to > as_of)
            WHERE binding.account_user_id = p_account_user_id
              AND binding.status = 'confirmed'
              AND binding.valid_from <= as_of
              AND (binding.valid_to IS NULL OR binding.valid_to > as_of)
              AND assignment.status = 'confirmed'
              AND assignment.valid_from <= as_of
              AND (assignment.valid_to IS NULL OR assignment.valid_to > as_of)
            UNION ALL
            SELECT 'guardian'::text,
                   guardian.id,
                   guardian.version,
                   binding.school_id,
                   binding.person_id,
                   school.display_name || ' · 家长身份'
            FROM public.account_person_bindings AS binding
            JOIN public.schools AS school
              ON school.id = binding.school_id AND school.status = 'active'
            JOIN public.guardian_relationships AS guardian
              ON guardian.school_id = binding.school_id
             AND guardian.guardian_person_id = binding.person_id
            WHERE binding.account_user_id = p_account_user_id
              AND binding.status = 'confirmed'
              AND binding.valid_from <= as_of
              AND (binding.valid_to IS NULL OR binding.valid_to > as_of)
              AND guardian.status = 'confirmed'
              AND guardian.valid_from <= as_of
              AND (guardian.valid_to IS NULL OR guardian.valid_to > as_of)
            ORDER BY 1, 4, 2;
        END
        $function$
        """
    )
    op.execute(
        """
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
                'context-v1',
                p_account_user_id::text,
                p_authorization_version::text,
                p_school_id::text,
                p_person_id::text,
                p_context_kind,
                COALESCE(p_assignment_id::text, '-'),
                COALESCE(p_assignment_version::text, '-'),
                COALESCE(p_guardian_relationship_id::text, '-'),
                COALESCE(p_guardian_relationship_version::text, '-'),
                p_request_id::text,
                p_context_purpose,
                COALESCE(p_idempotency_key_hash, '-'),
                p_session_nonce_hash,
                to_char(p_proof_expires_at AT TIME ZONE 'UTC',
                        'YYYY-MM-DD"T"HH24:MI:SS.US"Z"')
            ], chr(31));
            expected_proof := encode(
                app_private.hmac(convert_to(canonical_text, 'UTF8'), signing_key, 'sha256'),
                'hex'
            );
            IF expected_proof IS DISTINCT FROM p_server_proof THEN
                RAISE EXCEPTION 'execution context proof verification failed'
                    USING ERRCODE = '28000';
            END IF;

            -- 证明验证成功后先绑定事务局部载体，使随后由 definer 发起的
            -- 关系重验也经过同一学校 RLS；任一重验失败会回滚全部 GUC。
            PERFORM set_config('app.context_key_version', current_key_version::text, true);
            PERFORM set_config('app.school_id', p_school_id::text, true);
            PERFORM set_config('app.account_user_id', p_account_user_id::text, true);
            PERFORM set_config('app.person_id', p_person_id::text, true);
            PERFORM set_config('app.context_kind', p_context_kind, true);
            PERFORM set_config('app.assignment_id', COALESCE(p_assignment_id::text, '-'), true);
            PERFORM set_config(
                'app.assignment_version', COALESCE(p_assignment_version::text, '-'), true
            );
            PERFORM set_config(
                'app.guardian_relationship_id',
                COALESCE(p_guardian_relationship_id::text, '-'),
                true
            );
            PERFORM set_config(
                'app.guardian_relationship_version',
                COALESCE(p_guardian_relationship_version::text, '-'),
                true
            );
            PERFORM set_config('app.authorization_version', p_authorization_version::text, true);
            PERFORM set_config('app.request_id', p_request_id::text, true);
            PERFORM set_config('app.context_purpose', p_context_purpose, true);
            PERFORM set_config(
                'app.idempotency_key_hash', COALESCE(p_idempotency_key_hash, '-'), true
            );
            PERFORM set_config('app.session_nonce_hash', p_session_nonce_hash, true);
            PERFORM set_config(
                'app.proof_expires_at',
                to_char(p_proof_expires_at AT TIME ZONE 'UTC',
                        'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),
                true
            );
            PERFORM set_config('app.server_proof', p_server_proof, true);
            PERFORM set_config('app.binding_database', current_database(), true);
            PERFORM set_config('app.binding_backend_pid', pg_backend_pid()::text, true);
            PERFORM set_config('app.binding_xact_id', pg_current_xact_id()::text, true);
            secondary_text := array_to_string(ARRAY[
                'bound-context-v1', current_database(), pg_backend_pid()::text,
                pg_current_xact_id()::text, current_key_version::text, canonical_text,
                p_server_proof
            ], chr(31));
            secondary_proof := encode(
                app_private.hmac(convert_to(secondary_text, 'UTF8'), signing_key, 'sha256'),
                'hex'
            );
            PERFORM set_config('app.binding_proof', secondary_proof, true);

            IF NOT EXISTS (
                SELECT 1
                FROM public.users AS account
                JOIN public.account_person_bindings AS binding
                  ON binding.account_user_id = account.id
                 AND binding.school_id = p_school_id
                 AND binding.person_id = p_person_id
                 AND binding.status = 'confirmed'
                 AND binding.valid_from <= as_of
                 AND (binding.valid_to IS NULL OR binding.valid_to > as_of)
                JOIN public.schools AS school
                  ON school.id = binding.school_id AND school.status = 'active'
                WHERE account.id = p_account_user_id
                  AND account.is_active
                  AND account.security_version = p_authorization_version
            ) THEN
                RAISE EXCEPTION 'execution context actor is not current'
                    USING ERRCODE = '28000';
            END IF;
            IF p_context_kind = 'assignment' AND NOT EXISTS (
                SELECT 1 FROM public.staff_assignments AS assignment
                WHERE assignment.school_id = p_school_id
                  AND assignment.id = p_assignment_id
                  AND assignment.person_id = p_person_id
                  AND assignment.version = p_assignment_version
                  AND assignment.status = 'confirmed'
                  AND assignment.valid_from <= as_of
                  AND (assignment.valid_to IS NULL OR assignment.valid_to > as_of)
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
            ) THEN
                RAISE EXCEPTION 'execution context guardian relationship is not current'
                    USING ERRCODE = '28000';
            END IF;

        END
        $function$
        """
    )
    op.execute(
        """
        CREATE FUNCTION app.require_school_context()
        RETURNS uuid
        LANGUAGE plpgsql
        STABLE
        SECURITY DEFINER
        SET search_path = pg_catalog
        AS $function$
        DECLARE
            key_version_text text := current_setting('app.context_key_version', true);
            school_text text := current_setting('app.school_id', true);
            account_text text := current_setting('app.account_user_id', true);
            person_text text := current_setting('app.person_id', true);
            kind_text text := current_setting('app.context_kind', true);
            assignment_text text := current_setting('app.assignment_id', true);
            assignment_version_text text := current_setting('app.assignment_version', true);
            guardian_text text := current_setting('app.guardian_relationship_id', true);
            guardian_version_text text := current_setting(
                'app.guardian_relationship_version', true
            );
            authorization_text text := current_setting('app.authorization_version', true);
            request_text text := current_setting('app.request_id', true);
            purpose_text text := current_setting('app.context_purpose', true);
            idempotency_text text := current_setting('app.idempotency_key_hash', true);
            nonce_text text := current_setting('app.session_nonce_hash', true);
            expires_text text := current_setting('app.proof_expires_at', true);
            server_proof_text text := current_setting('app.server_proof', true);
            binding_database_text text := current_setting('app.binding_database', true);
            binding_backend_text text := current_setting('app.binding_backend_pid', true);
            binding_xact_text text := current_setting('app.binding_xact_id', true);
            binding_proof_text text := current_setting('app.binding_proof', true);
            signing_key bytea;
            canonical_text text;
            secondary_text text;
            expected_server_proof text;
            expected_binding_proof text;
        BEGIN
            IF key_version_text IS NULL OR key_version_text = ''
               OR school_text IS NULL OR school_text = ''
               OR account_text IS NULL OR account_text = ''
               OR person_text IS NULL OR person_text = ''
               OR kind_text IS NULL OR kind_text = ''
               OR assignment_text IS NULL OR assignment_text = ''
               OR assignment_version_text IS NULL OR assignment_version_text = ''
               OR guardian_text IS NULL OR guardian_text = ''
               OR guardian_version_text IS NULL OR guardian_version_text = ''
               OR authorization_text IS NULL OR authorization_text = ''
               OR request_text IS NULL OR request_text = ''
               OR purpose_text IS NULL OR purpose_text = ''
               OR idempotency_text IS NULL OR idempotency_text = ''
               OR nonce_text IS NULL OR nonce_text = ''
               OR expires_text IS NULL OR expires_text = ''
               OR server_proof_text IS NULL OR server_proof_text = ''
               OR binding_database_text IS NULL OR binding_database_text = ''
               OR binding_backend_text IS NULL OR binding_backend_text = ''
               OR binding_xact_text IS NULL OR binding_xact_text = ''
               OR binding_proof_text IS NULL OR binding_proof_text = '' THEN
                RAISE EXCEPTION 'master data RLS context is missing'
                    USING ERRCODE = '28000';
            END IF;

            SELECT keys.signing_key INTO signing_key
            FROM app_private.context_proof_keys AS keys
            WHERE keys.is_current AND keys.key_version::text = key_version_text;
            IF signing_key IS NULL THEN
                RAISE EXCEPTION 'RLS context integrity check failed'
                    USING ERRCODE = '28000';
            END IF;
            canonical_text := array_to_string(ARRAY[
                'context-v1', account_text, authorization_text, school_text, person_text,
                kind_text, assignment_text, assignment_version_text, guardian_text,
                guardian_version_text, request_text, purpose_text, idempotency_text,
                nonce_text, expires_text
            ], chr(31));
            expected_server_proof := encode(
                app_private.hmac(convert_to(canonical_text, 'UTF8'), signing_key, 'sha256'),
                'hex'
            );
            secondary_text := array_to_string(ARRAY[
                'bound-context-v1', current_database(), pg_backend_pid()::text,
                pg_current_xact_id()::text, key_version_text, canonical_text,
                server_proof_text
            ], chr(31));
            expected_binding_proof := encode(
                app_private.hmac(convert_to(secondary_text, 'UTF8'), signing_key, 'sha256'),
                'hex'
            );
            IF expected_server_proof IS DISTINCT FROM server_proof_text
               OR expected_binding_proof IS DISTINCT FROM binding_proof_text
               OR binding_database_text IS DISTINCT FROM current_database()
               OR binding_backend_text IS DISTINCT FROM pg_backend_pid()::text
               OR binding_xact_text IS DISTINCT FROM pg_current_xact_id()::text
               OR expires_text::timestamptz <= statement_timestamp() THEN
                RAISE EXCEPTION 'RLS context integrity check failed'
                    USING ERRCODE = '28000';
            END IF;
            RETURN school_text::uuid;
        EXCEPTION
            WHEN invalid_text_representation OR datetime_field_overflow THEN
                RAISE EXCEPTION 'RLS context contains malformed typed values'
                    USING ERRCODE = '22023';
        END
        $function$
        """
    )
    signatures = (
        "app.context_signing_key_fingerprint()",
        "app.list_execution_context_options(integer,bigint,text,timestamptz,text)",
        (
            "app.bind_execution_context(uuid,integer,uuid,text,uuid,bigint,uuid,bigint,"
            "bigint,uuid,text,text,text,timestamptz,text)"
        ),
        "app.require_school_context()",
    )
    for signature in signatures:
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO campus_ai_runtime")
    op.execute("GRANT EXECUTE ON FUNCTION app.require_school_context() TO campus_ai_db_owner")
    op.execute("SET LOCAL ROLE campus_ai_db_owner")
    op.execute("REVOKE CREATE ON SCHEMA app FROM campus_ai_context_definer")
    op.execute("GRANT USAGE ON SCHEMA app TO campus_ai_runtime, campus_ai_migration")
    op.execute("REVOKE CREATE ON SCHEMA app FROM campus_ai_runtime, campus_ai_migration")


def _create_audit_append_functions() -> None:
    op.execute("SET LOCAL ROLE campus_ai_db_owner")
    op.execute("GRANT USAGE ON SCHEMA public TO campus_ai_audit_definer")
    op.execute("GRANT USAGE, CREATE ON SCHEMA app, app_private TO campus_ai_audit_definer")
    op.execute(
        "GRANT INSERT ("
        f"{_AUDIT_RUNTIME_INSERT_COLUMNS}"
        ") ON TABLE public.audit_logs TO campus_ai_audit_definer"
    )
    op.execute("GRANT SELECT (id) ON TABLE public.audit_logs TO campus_ai_audit_definer")
    op.execute("GRANT USAGE ON SEQUENCE public.audit_logs_id_seq TO campus_ai_audit_definer")
    op.execute("SET LOCAL ROLE campus_ai_context_definer")
    op.execute("GRANT EXECUTE ON FUNCTION app.require_school_context() TO campus_ai_audit_definer")
    op.execute("SET LOCAL ROLE campus_ai_audit_definer")
    op.execute("ALTER DEFAULT PRIVILEGES REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC")
    op.execute(
        """
        CREATE FUNCTION app_private.is_safe_business_audit_summary(p_summary jsonb)
        RETURNS boolean
        LANGUAGE plpgsql
        IMMUTABLE
        PARALLEL SAFE
        SET search_path = pg_catalog
        AS $function$
        BEGIN
            IF p_summary IS NULL
               OR jsonb_typeof(p_summary) IS DISTINCT FROM 'object'
               OR NOT p_summary ?& ARRAY[
                    'state_code', 'resource_version', 'stable_reference_ids', 'fact_codes'
               ]
               OR (SELECT count(*) FROM jsonb_object_keys(p_summary)) <> 4
               OR jsonb_typeof(p_summary -> 'state_code') IS DISTINCT FROM 'string'
               OR (p_summary ->> 'state_code') IS DISTINCT FROM 'assignment.confirmed'
               OR (
                    jsonb_typeof(p_summary -> 'resource_version') IS DISTINCT FROM 'null'
                    AND (
                        jsonb_typeof(p_summary -> 'resource_version') IS DISTINCT FROM 'number'
                        OR (p_summary ->> 'resource_version') !~ '^[1-9][0-9]*$'
                    )
               )
               OR jsonb_typeof(p_summary -> 'stable_reference_ids') IS DISTINCT FROM 'array'
               OR jsonb_array_length(p_summary -> 'stable_reference_ids') > 16
               OR EXISTS (
                    SELECT 1
                    FROM jsonb_array_elements(
                        p_summary -> 'stable_reference_ids'
                    ) AS value(item)
                    WHERE jsonb_typeof(value.item) IS DISTINCT FROM 'string'
                       OR (value.item #>> '{}')
                          !~* '^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$'
               )
               OR jsonb_typeof(p_summary -> 'fact_codes') IS DISTINCT FROM 'array'
               OR jsonb_array_length(p_summary -> 'fact_codes') > 32
               OR EXISTS (
                    SELECT 1
                    FROM jsonb_array_elements(p_summary -> 'fact_codes') AS value(item)
                    WHERE jsonb_typeof(value.item) IS DISTINCT FROM 'string'
                       OR (value.item #>> '{}') IS DISTINCT FROM 'assignment.updated'
               ) THEN
                RETURN false;
            END IF;
            RETURN true;
        END
        $function$
        """
    )
    op.execute(
        """
        CREATE FUNCTION app.append_business_audit(
            p_actor_user_id integer,
            p_school_id uuid,
            p_actor_person_id uuid,
            p_actor_assignment_id uuid,
            p_actor_assignment_version bigint,
            p_actor_guardian_relationship_id uuid,
            p_actor_guardian_relationship_version bigint,
            p_actor_context_type text,
            p_idempotency_key_hash text,
            p_event_type text,
            p_target_type text,
            p_target_id uuid,
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
            v_actor_user_id integer := p_actor_user_id;
            v_school_id uuid := p_school_id;
            v_actor_person_id uuid := p_actor_person_id;
            v_actor_assignment_id uuid := p_actor_assignment_id;
            v_actor_assignment_version bigint := p_actor_assignment_version;
            v_actor_guardian_relationship_id uuid := p_actor_guardian_relationship_id;
            v_actor_guardian_relationship_version bigint := p_actor_guardian_relationship_version;
            v_actor_context_type text := p_actor_context_type;
            v_idempotency_key_hash text := p_idempotency_key_hash;
            v_request_id uuid := p_request_id;
        BEGIN
            IF session_user = 'campus_ai_runtime' THEN
                v_school_id := app.require_school_context();
                IF current_setting('app.context_purpose', true) IS DISTINCT FROM 'command' THEN
                    RAISE EXCEPTION 'business audit requires command context'
                        USING ERRCODE = '28000';
                END IF;
                v_actor_user_id := current_setting('app.account_user_id', true)::integer;
                v_actor_person_id := current_setting('app.person_id', true)::uuid;
                v_actor_context_type := current_setting('app.context_kind', true);
                v_actor_assignment_id := NULLIF(
                    current_setting('app.assignment_id', true), '-'
                )::uuid;
                v_actor_assignment_version := NULLIF(
                    current_setting('app.assignment_version', true), '-'
                )::bigint;
                v_actor_guardian_relationship_id := NULLIF(
                    current_setting('app.guardian_relationship_id', true), '-'
                )::uuid;
                v_actor_guardian_relationship_version := NULLIF(
                    current_setting('app.guardian_relationship_version', true), '-'
                )::bigint;
                v_idempotency_key_hash := current_setting(
                    'app.idempotency_key_hash', true
                );
                v_request_id := current_setting('app.request_id', true)::uuid;
                IF p_actor_user_id IS DISTINCT FROM v_actor_user_id
                   OR p_school_id IS DISTINCT FROM v_school_id
                   OR p_actor_person_id IS DISTINCT FROM v_actor_person_id
                   OR p_actor_assignment_id IS DISTINCT FROM v_actor_assignment_id
                   OR p_actor_assignment_version IS DISTINCT FROM v_actor_assignment_version
                   OR p_actor_guardian_relationship_id
                      IS DISTINCT FROM v_actor_guardian_relationship_id
                   OR p_actor_guardian_relationship_version
                      IS DISTINCT FROM v_actor_guardian_relationship_version
                   OR p_actor_context_type IS DISTINCT FROM v_actor_context_type
                   OR p_idempotency_key_hash IS DISTINCT FROM v_idempotency_key_hash
                   OR p_request_id IS DISTINCT FROM v_request_id THEN
                    RAISE EXCEPTION 'business audit execution context mismatch'
                        USING ERRCODE = '28000';
                END IF;
            END IF;

            IF p_event_type !~ '^[a-z][a-z0-9_.-]*$'
               OR p_event_type IS DISTINCT FROM 'master_data.assignment.changed'
               OR p_target_type IS DISTINCT FROM 'staff_assignment'
               OR p_reason_code IS DISTINCT FROM 'manual.assignment-confirmed'
               OR NOT app_private.is_safe_business_audit_summary(p_before_summary)
               OR NOT app_private.is_safe_business_audit_summary(p_after_summary) THEN
                RAISE EXCEPTION 'unsafe business audit record'
                    USING ERRCODE = '22023';
            END IF;

            INSERT INTO public.audit_logs (
                actor_user_id, school_id, actor_person_id, actor_assignment_id,
                actor_assignment_version, actor_guardian_relationship_id,
                actor_guardian_relationship_version, actor_context_type,
                idempotency_key_hash, event_type, target_type, target_id,
                before_summary, after_summary, request_id, reason
            ) VALUES (
                v_actor_user_id, v_school_id, v_actor_person_id, v_actor_assignment_id,
                v_actor_assignment_version, v_actor_guardian_relationship_id,
                v_actor_guardian_relationship_version, v_actor_context_type,
                v_idempotency_key_hash, p_event_type, p_target_type, p_target_id::text,
                p_before_summary, p_after_summary, v_request_id::text, p_reason_code
            )
            RETURNING id INTO v_audit_id;
            RETURN v_audit_id;
        END
        $function$
        """
    )
    op.execute(
        """
        CREATE FUNCTION app.append_legacy_audit(
            p_actor_user_id integer,
            p_event_type text,
            p_target_type text,
            p_target_id text,
            p_before_summary jsonb,
            p_after_summary jsonb,
            p_request_id text,
            p_reason text
        )
        RETURNS bigint
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog
        AS $function$
        DECLARE
            v_audit_id bigint;
        BEGIN
            IF NOT (
                    (p_event_type = 'identity.mapping.created'
                     AND p_target_type = 'user'
                     AND p_target_id ~ '^[1-9][0-9]*$')
                 OR (p_event_type = 'identity.initial_admin.bound'
                     AND p_target_type = 'system_admin_bootstrap'
                     AND p_target_id = 'system_admin')
                 OR (p_event_type IN (
                        'authorization.role.assigned', 'authorization.role.revoked'
                     )
                     AND p_target_type = 'user_role'
                     AND p_target_id ~ '^[1-9][0-9]*$')
                 OR (p_event_type = 'authorization.scope.assigned'
                     AND p_target_type = 'responsibility_scope'
                     AND p_target_id ~ '^[1-9][0-9]*$')
                 OR (p_event_type = 'authorization.permission.granted'
                     AND p_target_type = 'role_permission'
                     AND p_target_id ~ '^[1-9][0-9]*$')
               )
               OR length(p_request_id) NOT BETWEEN 1 AND 36
               OR length(p_reason) NOT BETWEEN 1 AND 500
               OR jsonb_typeof(p_before_summary) IS DISTINCT FROM 'object'
               OR jsonb_typeof(p_after_summary) IS DISTINCT FROM 'object' THEN
                RAISE EXCEPTION 'invalid legacy audit record'
                    USING ERRCODE = '22023';
            END IF;
            INSERT INTO public.audit_logs (
                actor_user_id, event_type, target_type, target_id,
                before_summary, after_summary, request_id, reason
            ) VALUES (
                p_actor_user_id, p_event_type, p_target_type, p_target_id,
                p_before_summary, p_after_summary, p_request_id, p_reason
            )
            RETURNING id INTO v_audit_id;
            RETURN v_audit_id;
        END
        $function$
        """
    )
    business_signature = (
        "app.append_business_audit(integer,uuid,uuid,uuid,bigint,uuid,bigint,text,text,"
        "text,text,uuid,jsonb,jsonb,uuid,text)"
    )
    legacy_signature = "app.append_legacy_audit(integer,text,text,text,jsonb,jsonb,text,text)"
    op.execute(
        "REVOKE ALL ON FUNCTION app_private.is_safe_business_audit_summary(jsonb) FROM PUBLIC"
    )
    for signature in (business_signature, legacy_signature):
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO campus_ai_runtime")
        op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO campus_ai_db_owner")
    op.execute("SET LOCAL ROLE campus_ai_db_owner")
    op.execute("REVOKE CREATE ON SCHEMA app, app_private FROM campus_ai_audit_definer")


def _enable_rls() -> None:
    for table_name in _RLS_TABLES:
        tenant_column = "id" if table_name == "schools" else "school_id"
        owner_bypass = "CURRENT_USER IN ('campus_ai_db_owner', 'campus_ai_context_definer')"
        tenant_match = f"{tenant_column} = (SELECT app.require_school_context())"
        command_context = (
            "current_setting('app.context_purpose', true) = 'command' "
            "AND current_setting('app.idempotency_key_hash', true) "
            "~ '^[0-9a-f]{64}$'"
        )
        row_scope = tenant_match
        if table_name == "command_idempotency_records":
            row_scope = (
                f"{tenant_match} "
                "AND actor_user_id = current_setting('app.account_user_id', true)::integer "
                "AND actor_person_id = current_setting('app.person_id', true)::uuid "
                "AND actor_context_type = current_setting('app.context_kind', true) "
                "AND idempotency_key_hash = "
                "current_setting('app.idempotency_key_hash', true) "
                "AND CASE current_setting('app.context_kind', true) "
                "WHEN 'assignment' THEN "
                "actor_assignment_id = current_setting('app.assignment_id', true)::uuid "
                "AND actor_assignment_version = "
                "current_setting('app.assignment_version', true)::bigint "
                "AND actor_guardian_relationship_id IS NULL "
                "AND actor_guardian_relationship_version IS NULL "
                "WHEN 'guardian' THEN "
                "actor_assignment_id IS NULL AND actor_assignment_version IS NULL "
                "AND actor_guardian_relationship_id = "
                "current_setting('app.guardian_relationship_id', true)::uuid "
                "AND actor_guardian_relationship_version = "
                "current_setting('app.guardian_relationship_version', true)::bigint "
                "ELSE false END"
            )
        op.execute(f"ALTER TABLE public.{table_name} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE public.{table_name} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant_select ON public.{table_name} FOR SELECT "
            f"USING (CASE WHEN {owner_bypass} THEN true ELSE {row_scope} END)"
        )
        op.execute(
            f"CREATE POLICY tenant_insert ON public.{table_name} FOR INSERT "
            f"WITH CHECK (CASE WHEN {owner_bypass} THEN true "
            f"ELSE {row_scope} AND {command_context} END)"
        )
        op.execute(
            f"CREATE POLICY tenant_update ON public.{table_name} FOR UPDATE "
            f"USING (CASE WHEN {owner_bypass} THEN true "
            f"ELSE {row_scope} AND {command_context} END) "
            f"WITH CHECK (CASE WHEN {owner_bypass} THEN true "
            f"ELSE {row_scope} AND {command_context} END)"
        )
        op.execute(
            f"CREATE POLICY owner_delete ON public.{table_name} FOR DELETE USING ({owner_bypass})"
        )


def _grant_runtime_acl() -> None:
    for table_name in _RLS_TABLES:
        if table_name == "outbox_messages":
            op.execute("REVOKE ALL ON TABLE public.outbox_messages FROM campus_ai_runtime")
        elif table_name == "command_idempotency_records":
            op.execute(
                "REVOKE ALL ON TABLE public.command_idempotency_records FROM campus_ai_runtime"
            )
        else:
            op.execute(
                f"GRANT SELECT, INSERT, UPDATE ON TABLE public.{table_name} TO campus_ai_runtime"
            )
        op.execute(f"REVOKE DELETE, TRUNCATE ON TABLE public.{table_name} FROM campus_ai_runtime")

    op.execute("GRANT SELECT, INSERT, UPDATE ON TABLE public.users TO campus_ai_runtime")
    op.execute("GRANT SELECT, INSERT, UPDATE ON TABLE public.roles TO campus_ai_runtime")
    for table_name in ("user_roles", "role_permissions", "responsibility_scopes"):
        op.execute(
            f"GRANT SELECT, INSERT, DELETE ON TABLE public.{table_name} TO campus_ai_runtime"
        )
    op.execute("REVOKE ALL ON TABLE public.audit_logs FROM campus_ai_runtime")
    op.execute(
        "REVOKE INSERT ("
        f"{_AUDIT_RUNTIME_INSERT_COLUMNS}"
        ") ON TABLE public.audit_logs FROM campus_ai_runtime"
    )
    op.execute(
        "GRANT INSERT (id, school_id, event_key, event_type, aggregate_type, aggregate_id, "
        "aggregate_version, occurred_at, payload) "
        "ON TABLE public.outbox_messages TO campus_ai_runtime"
    )
    op.execute(
        "GRANT SELECT (id, school_id, action_code, actor_user_id, actor_person_id, "
        "actor_context_type, actor_context_key, actor_assignment_id, actor_assignment_version, "
        "actor_guardian_relationship_id, actor_guardian_relationship_version, resource_type, "
        "resource_id, idempotency_key_hash, request_sha256, status, result_type, result_payload, "
        "created_at, updated_at) ON TABLE public.command_idempotency_records "
        "TO campus_ai_runtime"
    )
    op.execute(
        "GRANT INSERT (id, school_id, action_code, actor_user_id, actor_person_id, "
        "actor_context_type, actor_context_key, actor_assignment_id, actor_assignment_version, "
        "actor_guardian_relationship_id, actor_guardian_relationship_version, resource_type, "
        "resource_id, idempotency_key_hash, request_sha256) "
        "ON TABLE public.command_idempotency_records TO campus_ai_runtime"
    )
    op.execute(
        "GRANT UPDATE (status, result_type, result_payload, updated_at) "
        "ON TABLE public.command_idempotency_records TO campus_ai_runtime"
    )
    op.execute(
        "GRANT USAGE ON SEQUENCE public.users_id_seq, public.roles_id_seq, "
        "public.user_roles_id_seq, public.role_permissions_id_seq, "
        "public.responsibility_scopes_id_seq "
        "TO campus_ai_runtime"
    )
    op.execute("REVOKE ALL ON ALL TABLES IN SCHEMA app_private FROM campus_ai_runtime")
    op.execute("REVOKE ALL ON ALL SEQUENCES IN SCHEMA app_private FROM campus_ai_runtime")


def upgrade() -> None:
    """创建不可伪造事务上下文，强制首批敏感表 RLS 并授予最小 DML。"""

    _create_private_key_store()
    _grant_definer_read_columns()
    _create_context_functions()
    _create_audit_append_functions()
    _enable_rls()
    _grant_runtime_acl()


def downgrade() -> None:
    """撤销 runtime ACL、RLS 和上下文函数，恢复到 06 事务事实结构。"""

    op.execute(
        "REVOKE USAGE ON SEQUENCE public.users_id_seq, public.roles_id_seq, "
        "public.user_roles_id_seq, public.role_permissions_id_seq, "
        "public.responsibility_scopes_id_seq "
        "FROM campus_ai_runtime"
    )
    op.execute("REVOKE ALL ON TABLE public.audit_logs FROM campus_ai_runtime")
    op.execute(
        "REVOKE INSERT ("
        f"{_AUDIT_RUNTIME_INSERT_COLUMNS}"
        ") ON TABLE public.audit_logs FROM campus_ai_runtime"
    )
    for table_name in (
        "users",
        "roles",
        "user_roles",
        "role_permissions",
        "responsibility_scopes",
        *_RLS_TABLES,
    ):
        op.execute(f"REVOKE ALL ON TABLE public.{table_name} FROM campus_ai_runtime")
    for table_name in reversed(_RLS_TABLES):
        for policy_name in ("owner_delete", "tenant_update", "tenant_insert", "tenant_select"):
            op.execute(f"DROP POLICY {policy_name} ON public.{table_name}")
        op.execute(f"ALTER TABLE public.{table_name} DISABLE ROW LEVEL SECURITY")

    for table_name, columns in _DEFINER_READ_COLUMNS.items():
        op.execute(
            f"REVOKE SELECT ({columns}) ON TABLE public.{table_name} FROM campus_ai_context_definer"
        )

    op.execute("SET LOCAL ROLE campus_ai_audit_definer")
    op.execute("ALTER DEFAULT PRIVILEGES GRANT EXECUTE ON FUNCTIONS TO PUBLIC")
    op.execute(
        "DROP FUNCTION app.append_business_audit(integer,uuid,uuid,uuid,bigint,uuid,bigint,"
        "text,text,text,text,uuid,jsonb,jsonb,uuid,text)"
    )
    op.execute(
        "DROP FUNCTION app.append_legacy_audit(integer,text,text,text,jsonb,jsonb,text,text)"
    )
    op.execute("DROP FUNCTION app_private.is_safe_business_audit_summary(jsonb)")
    op.execute("SET LOCAL ROLE campus_ai_context_definer")
    op.execute("ALTER DEFAULT PRIVILEGES GRANT EXECUTE ON FUNCTIONS TO PUBLIC")
    op.execute("DROP FUNCTION app.require_school_context()")
    op.execute(
        "DROP FUNCTION app.bind_execution_context(uuid,integer,uuid,text,uuid,bigint,uuid,bigint,"
        "bigint,uuid,text,text,text,timestamptz,text)"
    )
    op.execute(
        "DROP FUNCTION app.list_execution_context_options(integer,bigint,text,timestamptz,text)"
    )
    op.execute("DROP FUNCTION app.context_signing_key_fingerprint()")
    op.execute("SET LOCAL ROLE campus_ai_db_owner")
    op.execute("REVOKE ALL ON TABLE public.audit_logs FROM campus_ai_audit_definer")
    op.execute(
        "REVOKE INSERT ("
        f"{_AUDIT_RUNTIME_INSERT_COLUMNS}"
        ") ON TABLE public.audit_logs FROM campus_ai_audit_definer"
    )
    op.execute("REVOKE SELECT (id) ON TABLE public.audit_logs FROM campus_ai_audit_definer")
    op.execute("REVOKE USAGE ON SEQUENCE public.audit_logs_id_seq FROM campus_ai_audit_definer")
    op.execute("REVOKE USAGE ON SCHEMA public FROM campus_ai_audit_definer")
    op.execute("DROP TABLE app_private.context_proof_keys")
    op.execute("DROP EXTENSION pgcrypto")
    op.execute("DROP SCHEMA app_private")
    op.execute("DROP SCHEMA app")
