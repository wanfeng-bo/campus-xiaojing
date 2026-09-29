"""Bind one database to one school and close the SF5B capability boundary.

Revision ID: 20260824_20
Revises: 20260823_19
Create Date: 2026-08-24
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260824_20"
down_revision = "20260823_19"
branch_labels = None
depends_on = None


# Every relation below carries a school identity directly.  Keep this explicit:
# adding a table is a security review event, not an ordering trick.
_SF5B_APP_ACCESS_TABLES = (
    "schools",
    "school_versions",
    "school_people",
    "school_person_names",
    "school_person_profile_versions",
    "students",
    "student_school_relationships",
    "account_person_bindings",
    "account_person_binding_versions",
    "organization_units",
    "organization_unit_versions",
    "organization_campus_relationships",
    "staff_assignments",
    "staff_assignment_versions",
    "campus_locations",
    "campus_location_versions",
    "campus_location_aliases",
    "dorm_room_profile_versions",
    "dorm_beds",
    "dorm_bed_versions",
    "student_bed_residences",
    "person_contact_points",
    "student_contact_relationships",
    "official_contact_points",
    "school_policies",
    "school_policy_scopes",
    "school_policy_versions",
    "school_fact_sources",
    "master_data_import_batches",
    "master_data_import_units",
    "master_data_import_command_outcomes",
    "master_data_import_takeovers",
    "school_fact_change_lineage",
    "command_idempotency_records",
    "audit_logs",
    "outbox_messages",
    "governance_duties",
    "governance_duty_versions",
    "governance_scopes",
    "governance_scope_versions",
    "governance_grants",
    "governance_grant_versions",
    "governance_assignment_epochs",
    "governance_bootstrap_states",
    "governance_command_receipts",
    "governance_audit_events",
)

_ANCHOR_GUARD_ONLY_TABLES = (
    "administrative_classes",
    "administrative_class_versions",
    "guardian_relationships",
    "repair_responsibility_grants",
    "repair_scope_bindings",
    "repair_tickets",
    "responsibility_group_memberships",
    "responsibility_group_versions",
    "responsibility_groups",
    "responsibility_types",
    "school_calendar_days",
    "school_calendar_versions",
    "student_enrollments",
)

_SCHOOL_BEARING_TABLES = _SF5B_APP_ACCESS_TABLES + _ANCHOR_GUARD_ONLY_TABLES

_RETIRED_REPAIRS_RUNTIME_ACCESS_TABLES = (
    "repair_responsibility_grants",
    "repair_scope_bindings",
    "repair_tickets",
    "responsibility_group_memberships",
    "responsibility_group_versions",
    "responsibility_groups",
    "responsibility_types",
)

_SCHOOL_FACT_WRITER_TABLES = (
    "schools",
    "school_versions",
    "school_people",
    "school_person_names",
    "school_person_profile_versions",
    "students",
    "student_school_relationships",
    "account_person_bindings",
    "account_person_binding_versions",
    "organization_units",
    "organization_unit_versions",
    "organization_campus_relationships",
    "staff_assignments",
    "staff_assignment_versions",
    "campus_locations",
    "campus_location_versions",
    "campus_location_aliases",
    "dorm_room_profile_versions",
    "dorm_beds",
    "dorm_bed_versions",
    "student_bed_residences",
    "person_contact_points",
    "student_contact_relationships",
    "official_contact_points",
    "school_policies",
    "school_policy_scopes",
    "school_policy_versions",
    "school_fact_sources",
    "master_data_import_batches",
    "master_data_import_units",
    "master_data_import_command_outcomes",
    "school_fact_change_lineage",
    "command_idempotency_records",
    "audit_logs",
)

_SCHOOL_FACT_RUNTIME_DML_REVOKE_TABLES = _SCHOOL_FACT_WRITER_TABLES + (
    "master_data_import_takeovers",
)

_R1_READER_TABLES = (
    "school_people",
    "organization_units",
    "organization_unit_versions",
    "staff_assignments",
    "staff_assignment_versions",
    "campus_locations",
    "campus_location_versions",
    "official_contact_points",
)

_REV18_CONTENT_SLICE_POLICY_TABLES = (
    "staff_assignment_versions",
    "account_person_binding_versions",
)

_LEGACY_CONTEXT_POLICY_DROP_TABLES = frozenset(
    _R1_READER_TABLES + _REV18_CONTENT_SLICE_POLICY_TABLES + ("school_versions",)
)

_LEGACY_IMPORT_POLICY_DROP_TABLES = (
    "master_data_import_batches",
    "master_data_import_units",
)

_REVOKED_RUNTIME_WRITER_FUNCTIONS = (
    "app.lock_master_data_import_takeover_source(uuid,uuid,uuid)",
    "app.lock_master_data_import_takeover_assignment(uuid,uuid,uuid,uuid)",
    "app.supersede_master_data_import_takeover_source(uuid,uuid,uuid,text,bigint)",
)


_GOVERNANCE_READER_TABLES = (
    "governance_duties",
    "governance_duty_versions",
    "governance_scopes",
    "governance_scope_versions",
    "governance_grants",
    "governance_grant_versions",
    "governance_assignment_epochs",
)

_SHARED_SUPPORT_TABLES = (
    "command_idempotency_records",
    "audit_logs",
    "outbox_messages",
)

_WRITER_FUNCTIONS = (
    "app.require_school_context()",
    "app.purge_expired_school_fact_preparations()",
    (
        "app.bind_execution_context(uuid,integer,uuid,text,uuid,bigint,uuid,bigint,"
        "bigint,uuid,text,text,text,timestamptz,text)"
    ),
    (
        "app.bind_school_reactivation_context(uuid,integer,uuid,text,uuid,bigint,uuid,"
        "bigint,bigint,uuid,text,text,text,timestamptz,text,text,text,text,bigint,text,"
        "text,text,bigint)"
    ),
    "app.require_school_reactivation_context()",
    "app.school_reactivation_replay_result()",
    "app.bind_root_reactivation_context(text,text,text,text,bigint)",
    "app.require_root_reactivation_context()",
    "app.school_fact_content_version_terminal(uuid,text,uuid,bigint)",
    "app.school_fact_root_is_effective_at(uuid,text,uuid,timestamptz)",
    "app.school_fact_location_history_is_resolvable(uuid,uuid,timestamptz)",
    ("app.school_fact_location_history_interval_is_resolvable(uuid,uuid,timestamptz,timestamptz)"),
    (
        "app.school_fact_school_reactivation_preflight(uuid,integer,uuid,uuid,bigint,"
        "bigint,uuid,text,text,timestamptz,text,text,bigint,timestamptz,bigint,text,text)"
    ),
    (
        "app.school_fact_school_reactivation_prepare(uuid,integer,uuid,uuid,bigint,bigint,"
        "uuid,text,text,timestamptz,text,text,bigint,timestamptz,bigint,text,text,uuid,uuid,"
        "text,text,timestamptz,text,text,text,text,text,text,integer)"
    ),
)

_R1_READER_FUNCTIONS = ("app.school_fact_location_history_is_resolvable(uuid,uuid,timestamptz)",)


def _create_anchor() -> None:
    op.create_table(
        "school_database_anchor",
        sa.Column("singleton_id", sa.SmallInteger(), nullable=False),
        sa.Column("anchor_id", sa.Uuid(), nullable=False),
        sa.Column("school_id", sa.Uuid(), nullable=False),
        sa.Column("cluster_system_identifier", sa.String(length=32), nullable=False),
        sa.Column("database_oid", sa.BigInteger(), nullable=False),
        sa.Column("database_name", sa.String(length=63), nullable=False),
        sa.Column(
            "provisioned_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint("singleton_id = 1", name="ck_school_database_anchor_singleton"),
        sa.ForeignKeyConstraint(
            ["school_id"],
            ["schools.id"],
            name="fk_school_database_anchor_school",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("singleton_id"),
        sa.UniqueConstraint("anchor_id", name="uq_school_database_anchor_anchor_id"),
        sa.UniqueConstraint("school_id", name="uq_school_database_anchor_school_id"),
    )
    op.execute("REVOKE ALL ON TABLE public.school_database_anchor FROM PUBLIC CASCADE")
    for role_name in (
        "campus_ai_runtime",
        "campus_ai_school_fact_writer",
        "campus_ai_school_fact_reader",
        "campus_ai_identity_definer",
    ):
        op.execute(f"REVOKE ALL ON TABLE public.school_database_anchor FROM {role_name} CASCADE")


def _create_anchor_functions() -> None:
    op.execute(
        sa.text(
            """
            CREATE FUNCTION app.require_school_database_anchor()
            RETURNS uuid
            LANGUAGE plpgsql
            SECURITY DEFINER
            SET search_path = pg_catalog, pg_temp
            AS $function$
            DECLARE
                bound record;
                actual_system_identifier text;
            BEGIN
                SELECT anchor.anchor_id,
                       anchor.school_id,
                       anchor.cluster_system_identifier,
                       anchor.database_oid,
                       anchor.database_name
                  INTO bound
                  FROM public.school_database_anchor AS anchor
                 WHERE anchor.singleton_id = 1;
                IF NOT FOUND THEN
                    RAISE EXCEPTION USING
                        ERRCODE = '55000',
                        MESSAGE = 'school database is not provisioned';
                END IF;

                SELECT control.system_identifier::text
                  INTO actual_system_identifier
                  FROM pg_catalog.pg_control_system() AS control;
                IF bound.cluster_system_identifier <> actual_system_identifier
                   OR bound.database_oid <>
                      (SELECT database.oid::bigint
                         FROM pg_catalog.pg_database AS database
                        WHERE database.datname = pg_catalog.current_database())
                   OR bound.database_name <> pg_catalog.current_database() THEN
                    RAISE EXCEPTION USING
                        ERRCODE = '55000',
                        MESSAGE = 'school database anchor does not match this database instance';
                END IF;
                RETURN bound.school_id;
            END
            $function$;

            CREATE FUNCTION app.assert_school_database_anchor(
                p_anchor_id uuid,
                p_school_id uuid,
                p_expected_system_identifier text
            )
            RETURNS uuid
            LANGUAGE plpgsql
            SECURITY DEFINER
            SET search_path = pg_catalog, pg_temp
            AS $function$
            DECLARE
                bound record;
                required_school_id uuid;
            BEGIN
                IF p_anchor_id IS NULL OR p_school_id IS NULL
                   OR p_expected_system_identifier IS NULL
                   OR p_expected_system_identifier = '' THEN
                    RAISE EXCEPTION USING
                        ERRCODE = '22023',
                        MESSAGE = 'complete expected school database identity is required';
                END IF;
                required_school_id := app.require_school_database_anchor();
                SELECT anchor.anchor_id,
                       anchor.school_id,
                       anchor.cluster_system_identifier
                  INTO bound
                  FROM public.school_database_anchor AS anchor
                 WHERE anchor.singleton_id = 1;
                IF bound.anchor_id <> p_anchor_id
                   OR bound.school_id <> p_school_id
                   OR required_school_id <> p_school_id
                   OR bound.cluster_system_identifier <> p_expected_system_identifier THEN
                    RAISE EXCEPTION USING
                        ERRCODE = '55000',
                        MESSAGE = 'expected school database identity does not match';
                END IF;
                RETURN required_school_id;
            END
            $function$;

            CREATE FUNCTION app.provision_school_database_anchor(
                p_anchor_id uuid,
                p_school_id uuid,
                p_expected_system_identifier text
            )
            RETURNS uuid
            LANGUAGE plpgsql
            SECURITY DEFINER
            SET search_path = pg_catalog, pg_temp
            AS $function$
            DECLARE
                table_name text;
                conflicting_rows bigint;
                actual_system_identifier text;
                actual_database_oid bigint;
            BEGIN
                IF p_anchor_id IS NULL OR p_school_id IS NULL
                   OR p_expected_system_identifier IS NULL
                   OR p_expected_system_identifier = '' THEN
                    RAISE EXCEPTION USING
                        ERRCODE = '22023',
                        MESSAGE = 'complete school database identity is required';
                END IF;
                PERFORM pg_catalog.pg_advisory_xact_lock(
                    pg_catalog.hashtextextended('sf5b.school-root-anchor:v1', 0)
                );

                IF EXISTS (SELECT 1 FROM public.school_database_anchor) THEN
                    RAISE EXCEPTION USING
                        ERRCODE = '55000',
                        MESSAGE = 'school database anchor is immutable once provisioned';
                END IF;
                IF (SELECT pg_catalog.count(*) FROM public.schools) <> 1
                   OR NOT EXISTS (SELECT 1 FROM public.schools WHERE id = p_school_id) THEN
                    RAISE EXCEPTION USING
                        ERRCODE = '55000',
                        MESSAGE = 'provisioning requires exactly one matching school root';
                END IF;

                FOREACH table_name IN ARRAY ARRAY[
                    'school_versions', 'school_people', 'school_person_names',
                    'school_person_profile_versions', 'students',
                    'student_school_relationships', 'account_person_bindings',
                    'account_person_binding_versions', 'organization_units',
                    'organization_unit_versions', 'organization_campus_relationships',
                    'staff_assignments', 'staff_assignment_versions', 'campus_locations',
                    'campus_location_versions', 'campus_location_aliases',
                    'dorm_room_profile_versions', 'dorm_beds', 'dorm_bed_versions',
                    'student_bed_residences', 'person_contact_points',
                    'student_contact_relationships', 'official_contact_points',
                    'school_policies', 'school_policy_scopes', 'school_policy_versions',
                    'school_fact_sources', 'master_data_import_batches',
                    'master_data_import_units', 'master_data_import_command_outcomes',
                    'master_data_import_takeovers', 'school_fact_change_lineage',
                    'command_idempotency_records', 'audit_logs', 'outbox_messages',
                    'governance_duties', 'governance_duty_versions', 'governance_scopes',
                    'governance_scope_versions', 'governance_grants',
                    'governance_grant_versions', 'governance_assignment_epochs',
                    'governance_bootstrap_states', 'governance_command_receipts',
                    'governance_audit_events', 'administrative_classes',
                    'administrative_class_versions', 'guardian_relationships',
                    'repair_responsibility_grants', 'repair_scope_bindings',
                    'repair_tickets', 'responsibility_group_memberships',
                    'responsibility_group_versions', 'responsibility_groups',
                    'responsibility_types', 'school_calendar_days',
                    'school_calendar_versions', 'student_enrollments'
                ]
                LOOP
                    EXECUTE pg_catalog.format(
                        'SELECT count(*) FROM public.%I '
                        'WHERE school_id IS NOT NULL AND school_id <> $1',
                        table_name
                    )
                    INTO conflicting_rows
                    USING p_school_id;
                    IF conflicting_rows <> 0 THEN
                        RAISE EXCEPTION USING
                            ERRCODE = '55000',
                            MESSAGE = pg_catalog.format(
                                'conflicting tenant rows exist in %s', table_name
                            );
                    END IF;
                END LOOP;

                SELECT control.system_identifier::text
                  INTO actual_system_identifier
                  FROM pg_catalog.pg_control_system() AS control;
                IF actual_system_identifier <> p_expected_system_identifier THEN
                    RAISE EXCEPTION USING
                        ERRCODE = '55000',
                        MESSAGE = 'expected cluster system identifier does not match';
                END IF;
                SELECT database.oid::bigint
                  INTO actual_database_oid
                  FROM pg_catalog.pg_database AS database
                 WHERE database.datname = pg_catalog.current_database();

                INSERT INTO public.school_database_anchor (
                    singleton_id,
                    anchor_id,
                    school_id,
                    cluster_system_identifier,
                    database_oid,
                    database_name
                )
                VALUES (
                    1,
                    p_anchor_id,
                    p_school_id,
                    actual_system_identifier,
                    actual_database_oid,
                    pg_catalog.current_database()
                );
                RETURN p_school_id;
            END
            $function$;
            """
        )
    )
    for signature in (
        "app.require_school_database_anchor()",
        "app.assert_school_database_anchor(uuid,uuid,text)",
        "app.provision_school_database_anchor(uuid,uuid,text)",
    ):
        op.execute(f"ALTER FUNCTION {signature} OWNER TO campus_ai_db_owner")
        op.execute(
            f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC, campus_ai_runtime, "
            "campus_ai_school_fact_writer, campus_ai_school_fact_reader CASCADE"
        )
    op.execute(
        "GRANT EXECUTE ON FUNCTION app.require_school_database_anchor() TO "
        "campus_ai_runtime, campus_ai_school_fact_writer, campus_ai_school_fact_reader, "
        "campus_ai_migration, campus_ai_context_definer, campus_ai_audit_definer, "
        "campus_ai_outbox_probe_definer, campus_ai_takeover_definer"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION app.assert_school_database_anchor(uuid,uuid,text) TO "
        "campus_ai_runtime, campus_ai_school_fact_writer, campus_ai_school_fact_reader, "
        "campus_ai_migration"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION app.provision_school_database_anchor(uuid,uuid,text) "
        "TO campus_ai_migration"
    )


def _create_anchor_guards() -> None:
    op.execute(
        sa.text(
            """
            CREATE FUNCTION app.enforce_school_database_anchor()
            RETURNS trigger
            LANGUAGE plpgsql
            SECURITY DEFINER
            SET search_path = pg_catalog, pg_temp
            AS $function$
            DECLARE
                anchored_school_id uuid;
            BEGIN
                PERFORM pg_catalog.pg_advisory_xact_lock(
                    pg_catalog.hashtextextended('sf5b.school-root-anchor:v1', 0)
                );
                SELECT anchor.school_id
                  INTO anchored_school_id
                  FROM public.school_database_anchor AS anchor
                 WHERE anchor.singleton_id = 1;
                IF NOT FOUND THEN
                    IF SESSION_USER <> 'campus_ai_migration' THEN
                        RAISE EXCEPTION USING
                            ERRCODE = '55000',
                            MESSAGE = 'first school root requires controlled provisioning';
                    END IF;
                    IF EXISTS (SELECT 1 FROM public.schools) THEN
                        RAISE EXCEPTION USING
                            ERRCODE = '23514',
                            MESSAGE = 'second school root cannot precede controlled provisioning';
                    END IF;
                    RETURN NEW;
                END IF;
                IF NEW.id <> anchored_school_id THEN
                    RAISE EXCEPTION USING
                        ERRCODE = '23514',
                        MESSAGE = 'second school root violates the database anchor';
                END IF;
                RETURN NEW;
            END
            $function$;

            CREATE FUNCTION app.provision_first_school_database_anchor()
            RETURNS trigger
            LANGUAGE plpgsql
            SECURITY DEFINER
            SET search_path = pg_catalog, pg_temp
            AS $function$
            DECLARE
                system_identifier text;
            BEGIN
                IF SESSION_USER <> 'campus_ai_migration' THEN
                    RAISE EXCEPTION USING
                        ERRCODE = '55000',
                        MESSAGE = 'only controlled migration may provision the first school root';
                END IF;
                SELECT control.system_identifier::text
                  INTO system_identifier
                  FROM pg_catalog.pg_control_system() AS control;
                PERFORM app.provision_school_database_anchor(
                    pg_catalog.gen_random_uuid(), NEW.id, system_identifier
                );
                RETURN NEW;
            END
            $function$;

            CREATE FUNCTION app.reject_school_database_anchor_mutation()
            RETURNS trigger
            LANGUAGE plpgsql
            SECURITY DEFINER
            SET search_path = pg_catalog, pg_temp
            AS $function$
            BEGIN
                RAISE EXCEPTION USING
                    ERRCODE = '55000',
                    MESSAGE = 'school database anchor is immutable';
            END
            $function$;

            CREATE TRIGGER trg_sf5b_school_database_anchor
            BEFORE INSERT OR UPDATE OF id ON public.schools
            FOR EACH ROW EXECUTE FUNCTION app.enforce_school_database_anchor();

            CREATE TRIGGER trg_sf5b_provision_first_school_database_anchor
            AFTER INSERT ON public.schools
            FOR EACH ROW EXECUTE FUNCTION app.provision_first_school_database_anchor();

            CREATE TRIGGER trg_sf5b_school_database_anchor_immutable
            BEFORE UPDATE OR DELETE ON public.school_database_anchor
            FOR EACH ROW EXECUTE FUNCTION app.reject_school_database_anchor_mutation();

            CREATE TRIGGER trg_sf5b_school_database_anchor_truncate
            BEFORE TRUNCATE ON public.school_database_anchor
            FOR EACH STATEMENT EXECUTE FUNCTION app.reject_school_database_anchor_mutation();
            """
        )
    )
    for signature in (
        "app.enforce_school_database_anchor()",
        "app.provision_first_school_database_anchor()",
        "app.reject_school_database_anchor_mutation()",
    ):
        op.execute(f"ALTER FUNCTION {signature} OWNER TO campus_ai_db_owner")
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC CASCADE")


def _bind_provable_existing_database() -> None:
    op.execute(
        sa.text(
            """
            DO $block$
            DECLARE
                school_count bigint;
                only_school_id uuid;
                system_identifier text;
            BEGIN
                SELECT pg_catalog.count(*) INTO school_count FROM public.schools;
                IF school_count = 0 THEN
                    RETURN;
                END IF;
                IF school_count <> 1 THEN
                    RAISE EXCEPTION USING
                        ERRCODE = '55000',
                        MESSAGE = 'revision 20 refuses to guess a multi-school database anchor';
                END IF;
                SELECT id INTO only_school_id FROM public.schools LIMIT 1;
                SELECT control.system_identifier::text
                  INTO system_identifier
                  FROM pg_catalog.pg_control_system() AS control;
                PERFORM app.provision_school_database_anchor(
                    pg_catalog.gen_random_uuid(), only_school_id, system_identifier
                );
            END
            $block$;
            """
        )
    )


def _create_identity_capabilities() -> None:
    op.execute(
        sa.text(
            """
            CREATE FUNCTION app.lock_existing_identity_user_id(
                p_provider_code text,
                p_provider_tenant_key text,
                p_external_subject text
            )
            RETURNS bigint
            LANGUAGE plpgsql
            SECURITY DEFINER
            SET search_path = pg_catalog, pg_temp
            AS $function$
            DECLARE
                canonical_key text;
                locked_user_id bigint;
            BEGIN
                IF p_provider_code IS NULL OR p_provider_code = ''
                   OR p_provider_tenant_key IS NULL OR p_provider_tenant_key = ''
                   OR p_external_subject IS NULL OR p_external_subject = '' THEN
                    RAISE EXCEPTION USING
                        ERRCODE = '22023',
                        MESSAGE = 'identity tuple components must be non-empty';
                END IF;
                canonical_key := pg_catalog.format(
                    'v1|%s:%s|%s:%s|%s:%s',
                    pg_catalog.octet_length(p_provider_code), p_provider_code,
                    pg_catalog.octet_length(p_provider_tenant_key), p_provider_tenant_key,
                    pg_catalog.octet_length(p_external_subject), p_external_subject
                );
                PERFORM pg_catalog.pg_advisory_xact_lock(
                    pg_catalog.hashtextextended(canonical_key, 0)
                );
                SELECT users.id
                  INTO locked_user_id
                  FROM public.users AS users
                 WHERE users.provider_code = p_provider_code
                   AND users.provider_tenant_key = p_provider_tenant_key
                   AND users.external_subject = p_external_subject
                 FOR UPDATE;
                RETURN locked_user_id;
            END
            $function$;

            CREATE FUNCTION app.claim_legacy_identity_user_id(
                p_legacy_external_subject text,
                p_provider_code text,
                p_provider_tenant_key text,
                p_external_subject text
            )
            RETURNS bigint
            LANGUAGE plpgsql
            SECURITY DEFINER
            SET search_path = pg_catalog, pg_temp
            AS $function$
            DECLARE
                claimed_user_id bigint;
                existing_target_id bigint;
            BEGIN
                existing_target_id := app.lock_existing_identity_user_id(
                    p_provider_code, p_provider_tenant_key, p_external_subject
                );
                IF existing_target_id IS NOT NULL THEN
                    RAISE EXCEPTION USING
                        ERRCODE = '23505',
                        MESSAGE = 'target identity tuple already exists';
                END IF;
                UPDATE public.users AS users
                   SET provider_code = p_provider_code,
                       provider_tenant_key = p_provider_tenant_key,
                       external_subject = p_external_subject,
                       updated_at = CURRENT_TIMESTAMP
                 WHERE users.id = (
                     SELECT legacy.id
                       FROM public.users AS legacy
                      WHERE legacy.provider_code = 'legacy'
                        AND legacy.provider_tenant_key = 'legacy'
                        AND legacy.external_subject = p_legacy_external_subject
                      FOR UPDATE
                 )
                RETURNING users.id INTO claimed_user_id;
                RETURN claimed_user_id;
            END
            $function$;
            """
        )
    )
    for signature in (
        "app.lock_existing_identity_user_id(text,text,text)",
        "app.claim_legacy_identity_user_id(text,text,text,text)",
    ):
        op.execute(
            f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC, campus_ai_runtime, "
            "campus_ai_school_fact_writer, campus_ai_school_fact_reader CASCADE"
        )


def _create_school_reactivation_writer_capabilities() -> None:
    preflight_signature = (
        "app.school_fact_school_reactivation_preflight(uuid,integer,uuid,uuid,bigint,"
        "bigint,uuid,text,text,timestamptz,text,text,bigint,timestamptz,bigint,text,text)"
    )
    prepare_signature = (
        "app.school_fact_school_reactivation_prepare(uuid,integer,uuid,uuid,bigint,bigint,"
        "uuid,text,text,timestamptz,text,text,bigint,timestamptz,bigint,text,text,uuid,uuid,"
        "text,text,timestamptz,text,text,text,text,text,text,integer)"
    )
    op.execute("GRANT CREATE ON SCHEMA app TO campus_ai_context_definer")
    op.execute("SET LOCAL ROLE campus_ai_context_definer")
    op.execute(
        sa.text(
            r"""
            CREATE FUNCTION app.school_fact_school_reactivation_preflight(
                p_school_id uuid,
                p_account_user_id integer,
                p_person_id uuid,
                p_assignment_id uuid,
                p_assignment_version bigint,
                p_authorization_version bigint,
                p_request_id uuid,
                p_idempotency_key_hash text,
                p_session_nonce_hash text,
                p_proof_expires_at timestamptz,
                p_server_proof text,
                p_fact_source_key text,
                p_expected_version bigint,
                p_effective_at timestamptz,
                p_base_version bigint,
                p_source_sha256 text,
                p_plan_sha256 text
            )
            RETURNS TABLE(
                replay_batch_id uuid,
                replay_target_version bigint,
                replay_changed_count integer,
                current_fact_version bigint,
                current_target_version bigint
            )
            LANGUAGE plpgsql
            STABLE
            SECURITY DEFINER
            SET search_path = pg_catalog, pg_temp
            AS $function$
            DECLARE
                v_as_of timestamptz := statement_timestamp();
                v_signing_key bytea;
                v_canonical text;
                v_expected_proof text;
                v_replay_batch_id uuid;
                v_replay_target_version text;
                v_replay_changed_count text;
                v_current_fact_version bigint;
                v_current_target_version bigint;
            BEGIN
                IF SESSION_USER IS DISTINCT FROM 'campus_ai_school_fact_writer'
                   OR p_school_id IS NULL
                   OR p_account_user_id IS NULL OR p_account_user_id < 1
                   OR p_person_id IS NULL
                   OR p_assignment_id IS NULL
                   OR p_assignment_version IS NULL OR p_assignment_version < 1
                   OR p_authorization_version IS NULL OR p_authorization_version < 1
                   OR p_request_id IS NULL
                   OR p_idempotency_key_hash !~ '^[0-9a-f]{64}$'
                   OR p_session_nonce_hash !~ '^[0-9a-f]{64}$'
                   OR p_proof_expires_at IS NULL OR p_proof_expires_at <= v_as_of
                   OR p_server_proof !~ '^[0-9a-f]{64}$'
                   OR p_fact_source_key IS NULL OR p_fact_source_key = ''
                   OR octet_length(convert_to(p_fact_source_key, 'UTF8')) > 255
                   OR p_expected_version IS NULL OR p_expected_version < 1
                   OR p_effective_at IS NULL
                   OR p_base_version IS NULL OR p_base_version < 1
                   OR p_source_sha256 !~ '^[0-9a-f]{64}$'
                   OR p_plan_sha256 !~ '^[0-9a-f]{64}$'
                   OR app.require_school_database_anchor() IS DISTINCT FROM p_school_id THEN
                    RAISE EXCEPTION 'school reactivation preflight rejected'
                        USING ERRCODE = '28000';
                END IF;

                SELECT keys.signing_key
                  INTO STRICT v_signing_key
                  FROM app_private.context_proof_keys AS keys
                 WHERE keys.is_current;
                v_canonical := array_to_string(ARRAY[
                    'context-v1',
                    p_account_user_id::text,
                    p_authorization_version::text,
                    p_school_id::text,
                    p_person_id::text,
                    'assignment',
                    p_assignment_id::text,
                    p_assignment_version::text,
                    '-',
                    '-',
                    p_request_id::text,
                    'command',
                    p_idempotency_key_hash,
                    p_session_nonce_hash,
                    to_char(
                        p_proof_expires_at AT TIME ZONE 'UTC',
                        'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'
                    )
                ], chr(31));
                v_expected_proof := encode(
                    app_private.hmac(
                        convert_to(v_canonical, 'UTF8'),
                        v_signing_key,
                        'sha256'
                    ),
                    'hex'
                );
                IF v_expected_proof IS DISTINCT FROM p_server_proof THEN
                    RAISE EXCEPTION 'school reactivation preflight rejected'
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
                       AND (
                           binding_version.effective_to IS NULL
                           OR binding_version.effective_to > v_as_of
                       )
                       AND app_private.school_fact_relationship_slice_is_effective_at(
                           binding_version.school_id,
                           'login_mapping',
                           binding_version.binding_id,
                           binding_version.target_version,
                           v_as_of
                       )
                      JOIN public.schools AS school
                        ON school.id = binding_version.school_id
                       AND school.status = 'inactive'
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
                       AND (
                           assignment_version.effective_to IS NULL
                           OR assignment_version.effective_to > v_as_of
                       )
                       AND (
                           assignment_version.planned_valid_to IS NULL
                           OR assignment_version.planned_valid_to > v_as_of
                       )
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
                       AND NOT app_private.school_fact_identity_root_is_effective_at(
                           binding_version.school_id,
                           'school',
                           binding_version.school_id,
                           v_as_of
                       )
                       AND EXISTS (
                           SELECT 1
                             FROM public.school_fact_change_lineage AS terminal_lineage
                            WHERE terminal_lineage.school_id = p_school_id
                              AND terminal_lineage.fact_kind = 'school'
                              AND terminal_lineage.fact_source_key = p_fact_source_key
                              AND terminal_lineage.target_id = p_school_id
                              AND terminal_lineage.target_version = p_expected_version
                              AND terminal_lineage.operation = 'revoke'
                       )
                ) THEN
                    RAISE EXCEPTION 'school reactivation preflight rejected'
                        USING ERRCODE = '28000';
                END IF;

                SELECT COALESCE(MAX(batch.target_fact_version), 1)
                  INTO v_current_fact_version
                  FROM public.master_data_import_batches AS batch
                 WHERE batch.school_id = p_school_id
                   AND batch.mode = 'canonical_atomic_changes'
                   AND batch.status = 'applied';
                SELECT school.version
                  INTO v_current_target_version
                  FROM public.schools AS school
                 WHERE school.id = p_school_id
                   AND school.source_key = p_fact_source_key;
                SELECT batch.id,
                       batch.result_payload ->> 'target_version',
                       batch.result_payload ->> 'changed_count'
                  INTO v_replay_batch_id,
                       v_replay_target_version,
                       v_replay_changed_count
                  FROM public.master_data_import_batches AS batch
                 WHERE batch.school_id = p_school_id
                   AND batch.source_sha256 = p_source_sha256
                   AND batch.plan_sha256 = p_plan_sha256
                   AND batch.status = 'applied'
                   AND batch.result_payload IS NOT NULL
                 ORDER BY batch.confirmed_at ASC NULLS LAST,
                          batch.created_at ASC,
                          batch.id ASC
                 LIMIT 1;
                IF v_replay_batch_id IS NOT NULL AND (
                    v_replay_target_version !~ '^[1-9][0-9]*$'
                    OR v_replay_changed_count !~ '^[0-9]+$'
                ) THEN
                    RAISE EXCEPTION 'school reactivation replay rejected'
                        USING ERRCODE = '28000';
                END IF;

                RETURN QUERY
                SELECT v_replay_batch_id,
                       v_replay_target_version::bigint,
                       v_replay_changed_count::integer,
                       v_current_fact_version,
                       v_current_target_version;
            EXCEPTION
                WHEN invalid_text_representation OR numeric_value_out_of_range THEN
                    RAISE EXCEPTION 'school reactivation preflight rejected'
                        USING ERRCODE = '28000';
            END
            $function$;
            """
        )
    )
    op.execute(
        sa.text(
            r"""
            CREATE FUNCTION app.school_fact_school_reactivation_prepare(
                p_school_id uuid,
                p_account_user_id integer,
                p_person_id uuid,
                p_assignment_id uuid,
                p_assignment_version bigint,
                p_authorization_version bigint,
                p_request_id uuid,
                p_idempotency_key_hash text,
                p_session_nonce_hash text,
                p_proof_expires_at timestamptz,
                p_server_proof text,
                p_fact_source_key text,
                p_expected_version bigint,
                p_effective_at timestamptz,
                p_base_version bigint,
                p_source_sha256 text,
                p_plan_sha256 text,
                p_proposed_batch_id uuid,
                p_proposed_unit_id uuid,
                p_confirmation_token_sha256 text,
                p_confirmation_session_sha256 text,
                p_confirmation_expires_at timestamptz,
                p_unit_payload_sha256 text,
                p_result_locator_sha256 text,
                p_template_version text,
                p_mapping_version text,
                p_mapping_config_sha256 text,
                p_origin_sheet_code text,
                p_origin_row_number integer
            )
            RETURNS TABLE(
                batch_id uuid,
                confirmation_token_sha256 text,
                confirmation_expires_at timestamptz,
                reused boolean
            )
            LANGUAGE plpgsql
            VOLATILE
            SECURITY DEFINER
            SET search_path = pg_catalog, pg_temp
            AS $function$
            DECLARE
                v_as_of timestamptz := statement_timestamp();
                v_preflight record;
                v_mapping_payload jsonb;
                v_unit_payload jsonb;
                v_existing_batch_id uuid;
                v_existing_token_sha256 text;
                v_existing_expires_at timestamptz;
                v_existing_exact boolean;
            BEGIN
                IF SESSION_USER IS DISTINCT FROM 'campus_ai_school_fact_writer'
                   OR p_proposed_batch_id IS NULL
                   OR p_proposed_unit_id IS NULL
                   OR p_proposed_batch_id = p_proposed_unit_id
                   OR p_confirmation_token_sha256 !~ '^[0-9a-f]{64}$'
                   OR p_confirmation_session_sha256 !~ '^[0-9a-f]{64}$'
                   OR p_confirmation_expires_at IS NULL
                   OR p_confirmation_expires_at <= v_as_of
                   OR p_unit_payload_sha256 !~ '^[0-9a-f]{64}$'
                   OR (
                       p_result_locator_sha256 IS NOT NULL
                       AND p_result_locator_sha256 !~ '^[0-9a-f]{64}$'
                   )
                   OR NOT (
                       (
                           p_template_version IS NOT DISTINCT FROM 'canonical_v1'
                           AND p_mapping_version IS NOT DISTINCT FROM 'manual_v1'
                           AND p_mapping_config_sha256 IS NOT DISTINCT FROM
                               'da372f43d87e905e1c0b608e8e1596c0dce172e67f661359e484ee092177f958'
                       )
                       OR (
                           p_template_version IS NOT DISTINCT FROM 'school_facts_v2'
                           AND p_mapping_version IS NOT DISTINCT FROM 'admin_v2'
                           AND p_mapping_config_sha256 IS NOT DISTINCT FROM
                               '93d942d3170fbd0f0191fbff48c2bda8cef60a1c63840f96c44e07870216fd28'
                       )
                   )
                   OR p_origin_sheet_code !~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,119}$'
                   OR p_origin_row_number IS NULL OR p_origin_row_number < 1 THEN
                    RAISE EXCEPTION 'school reactivation prepare rejected'
                        USING ERRCODE = '28000';
                END IF;

                SELECT *
                  INTO STRICT v_preflight
                  FROM app.school_fact_school_reactivation_preflight(
                      p_school_id,
                      p_account_user_id,
                      p_person_id,
                      p_assignment_id,
                      p_assignment_version,
                      p_authorization_version,
                      p_request_id,
                      p_idempotency_key_hash,
                      p_session_nonce_hash,
                      p_proof_expires_at,
                      p_server_proof,
                      p_fact_source_key,
                      p_expected_version,
                      p_effective_at,
                      p_base_version,
                      p_source_sha256,
                      p_plan_sha256
                  );
                IF v_preflight.replay_batch_id IS NOT NULL
                   OR v_preflight.current_fact_version IS DISTINCT FROM p_base_version
                   OR v_preflight.current_target_version IS DISTINCT FROM p_expected_version
                   OR NOT EXISTS (
                       SELECT 1
                         FROM public.schools AS school
                        WHERE school.id = p_school_id
                          AND school.source_key = p_fact_source_key
                          AND school.status = 'inactive'
                          AND school.version = p_expected_version
                   )
                   OR app_private.school_fact_identity_root_is_effective_at(
                       p_school_id,
                       'school',
                       p_school_id,
                       p_effective_at
                   ) THEN
                    RAISE EXCEPTION 'school reactivation prepare rejected'
                        USING ERRCODE = '28000';
                END IF;

                v_mapping_payload := jsonb_build_object(
                    'schema_version', 1,
                    'raw_source_sha256', p_source_sha256,
                    'template_version', p_template_version,
                    'mapping_version', p_mapping_version,
                    'mapping_config_sha256', p_mapping_config_sha256,
                    'result_locator_sha256', p_result_locator_sha256,
                    'source_manifest', '[]'::jsonb,
                    'row_lineage', '[]'::jsonb
                );
                v_unit_payload := jsonb_build_object(
                    'schema_version', 1,
                    'operation', 'reactivate',
                    'fact_kind', 'school',
                    'source_key', p_fact_source_key,
                    'expected_version', p_expected_version,
                    'effective_at', to_jsonb(p_effective_at),
                    'origin', jsonb_build_object(
                        'sheet_code', p_origin_sheet_code,
                        'row_number', p_origin_row_number
                    )
                );

                IF p_result_locator_sha256 IS NOT NULL THEN
                    PERFORM pg_advisory_xact_lock(
                        hashtextextended(
                            array_to_string(ARRAY[
                                'sf5b-school-reactivation-locator-v1',
                                p_school_id::text,
                                p_account_user_id::text,
                                p_session_nonce_hash,
                                p_result_locator_sha256
                            ], chr(31)),
                            0
                        )
                    );
                    SELECT candidate.id,
                           candidate.confirmation_token_sha256
                      INTO v_existing_batch_id,
                           v_existing_token_sha256
                      FROM public.master_data_import_batches AS candidate
                     WHERE candidate.school_id = p_school_id
                       AND candidate.staged_by_user_id = p_account_user_id
                       AND candidate.confirmation_session_sha256 =
                           p_confirmation_session_sha256
                       AND candidate.mapping_payload ->> 'result_locator_sha256' =
                           p_result_locator_sha256
                     ORDER BY CASE WHEN candidate.status = 'applied' THEN 0 ELSE 1 END,
                              candidate.id ASC
                     LIMIT 1;
                    IF v_existing_batch_id IS NOT NULL THEN
                        IF v_existing_token_sha256 !~ '^[0-9a-f]{64}$' THEN
                            RAISE EXCEPTION 'school reactivation prepare conflict'
                                USING ERRCODE = '28000';
                        END IF;
                        PERFORM pg_advisory_xact_lock(
                            hashtextextended(v_existing_token_sha256, 0)
                        );
                        SELECT batch.confirmation_token_sha256,
                               batch.confirmation_expires_at,
                               (
                                   batch.mode = 'canonical_atomic_changes'
                                   AND batch.source_kind = 'manual'
                                   AND batch.source_ref = 'sf2:' || batch.id::text
                                   AND batch.source_sha256 = p_source_sha256
                                   AND batch.filename_summary IS NULL
                                   AND batch.source_size_bytes IS NULL
                                   AND batch.worksheet_names =
                                       jsonb_build_array(p_origin_sheet_code)
                                   AND batch.mapping_version = 1
                                   AND batch.mapping_payload = v_mapping_payload
                                   AND batch.status = 'ready_for_confirmation'
                                   AND batch.planned_units = 1
                                   AND batch.succeeded_units = 0
                                   AND batch.failed_units = 0
                                   AND batch.pending_units = 1
                                   AND batch.confirmation_card_sha256 = p_plan_sha256
                                   AND batch.request_id = p_request_id
                                   AND batch.idempotency_key_hash = p_idempotency_key_hash
                                   AND batch.plan_sha256 = p_plan_sha256
                                   AND batch.base_fact_version = p_base_version
                                   AND batch.target_fact_version = p_base_version + 1
                                   AND batch.confirmation_consumed_at IS NULL
                                   AND batch.confirmation_expires_at > v_as_of
                                   AND batch.confirmation_session_sha256 = p_session_nonce_hash
                                   AND batch.staged_by_user_id = p_account_user_id
                                   AND batch.staged_by_person_id = p_person_id
                                   AND batch.staged_by_assignment_id = p_assignment_id
                                   AND batch.staged_assignment_version =
                                       p_assignment_version
                                   AND batch.staged_authorization_version =
                                       p_authorization_version
                                   AND batch.result_payload IS NULL
                                   AND batch.version = 1
                                   AND EXISTS (
                                       SELECT 1
                                         FROM public.master_data_import_units AS unit
                                        WHERE unit.school_id = batch.school_id
                                          AND unit.batch_id = batch.id
                                          AND unit.sequence = 1
                                          AND unit.row_number = p_origin_row_number
                                          AND unit.unit_kind = 'school_fact'
                                          AND unit.status = 'ready'
                                          AND unit.payload = v_unit_payload
                                          AND unit.payload_sha256 = p_unit_payload_sha256
                                          AND unit.soft_anomalies = '[]'::jsonb
                                          AND unit.initial_soft_anomalies = '[]'::jsonb
                                          AND unit.decision_reason IS NULL
                                          AND unit.target_id IS NULL
                                          AND unit.target_version IS NULL
                                          AND unit.error_code IS NULL
                                          AND unit.error_field_code IS NULL
                                          AND unit.last_attempt_idempotency_record_id IS NULL
                                          AND unit.last_attempt_completed IS NULL
                                          AND unit.version = 1
                                   )
                                   AND NOT EXISTS (
                                       SELECT 1
                                         FROM public.master_data_import_units AS other
                                        WHERE other.school_id = batch.school_id
                                          AND other.batch_id = batch.id
                                          AND other.sequence <> 1
                                   )
                               )
                          INTO v_existing_token_sha256,
                               v_existing_expires_at,
                               v_existing_exact
                          FROM public.master_data_import_batches AS batch
                         WHERE batch.id = v_existing_batch_id
                           AND batch.school_id = p_school_id;
                        IF NOT COALESCE(v_existing_exact, false) THEN
                            RAISE EXCEPTION 'school reactivation prepare conflict'
                                USING ERRCODE = '28000';
                        END IF;
                        RETURN QUERY
                        SELECT v_existing_batch_id,
                               v_existing_token_sha256,
                               v_existing_expires_at,
                               true;
                        RETURN;
                    END IF;
                END IF;

                INSERT INTO public.master_data_import_batches (
                    id,
                    school_id,
                    mode,
                    source_kind,
                    source_ref,
                    source_sha256,
                    filename_summary,
                    source_size_bytes,
                    worksheet_names,
                    mapping_version,
                    mapping_payload,
                    status,
                    planned_units,
                    succeeded_units,
                    failed_units,
                    pending_units,
                    confirmation_card_sha256,
                    request_id,
                    idempotency_key_hash,
                    plan_sha256,
                    base_fact_version,
                    target_fact_version,
                    confirmation_token_sha256,
                    confirmation_expires_at,
                    confirmation_session_sha256,
                    staged_by_user_id,
                    staged_by_person_id,
                    staged_by_assignment_id,
                    staged_assignment_version,
                    staged_authorization_version
                ) VALUES (
                    p_proposed_batch_id,
                    p_school_id,
                    'canonical_atomic_changes',
                    'manual',
                    'sf2:' || p_proposed_batch_id::text,
                    p_source_sha256,
                    NULL,
                    NULL,
                    jsonb_build_array(p_origin_sheet_code),
                    1,
                    v_mapping_payload,
                    'ready_for_confirmation',
                    1,
                    0,
                    0,
                    1,
                    p_plan_sha256,
                    p_request_id,
                    p_idempotency_key_hash,
                    p_plan_sha256,
                    p_base_version,
                    p_base_version + 1,
                    p_confirmation_token_sha256,
                    p_confirmation_expires_at,
                    p_confirmation_session_sha256,
                    p_account_user_id,
                    p_person_id,
                    p_assignment_id,
                    p_assignment_version,
                    p_authorization_version
                );
                INSERT INTO public.master_data_import_units (
                    id,
                    school_id,
                    batch_id,
                    sequence,
                    row_number,
                    unit_kind,
                    status,
                    payload,
                    payload_sha256,
                    soft_anomalies,
                    initial_soft_anomalies
                ) VALUES (
                    p_proposed_unit_id,
                    p_school_id,
                    p_proposed_batch_id,
                    1,
                    p_origin_row_number,
                    'school_fact',
                    'ready',
                    v_unit_payload,
                    p_unit_payload_sha256,
                    '[]'::jsonb,
                    '[]'::jsonb
                );
                RETURN QUERY
                SELECT p_proposed_batch_id,
                       p_confirmation_token_sha256,
                       p_confirmation_expires_at,
                       false;
            EXCEPTION
                WHEN integrity_constraint_violation
                     OR invalid_text_representation
                     OR numeric_value_out_of_range THEN
                    RAISE EXCEPTION 'school reactivation prepare rejected'
                        USING ERRCODE = '28000';
            END
            $function$;
            """
        )
    )
    for signature in (preflight_signature, prepare_signature):
        op.execute(
            f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC, campus_ai_runtime, "
            "campus_ai_school_fact_writer, campus_ai_school_fact_reader, "
            "campus_ai_migration, campus_ai_db_owner, campus_ai_identity_definer CASCADE"
        )
    op.execute("SET LOCAL ROLE campus_ai_db_owner")
    op.execute("REVOKE CREATE ON SCHEMA app FROM campus_ai_context_definer")


def _create_expired_preparation_cleanup_capability() -> None:
    signature = "app.purge_expired_school_fact_preparations()"
    op.execute("GRANT CREATE ON SCHEMA app TO campus_ai_context_definer")
    op.execute("SET LOCAL ROLE campus_ai_context_definer")
    op.execute(
        sa.text(
            r"""
            CREATE FUNCTION app.purge_expired_school_fact_preparations()
            RETURNS integer
            LANGUAGE plpgsql
            VOLATILE
            SECURITY DEFINER
            SET search_path = pg_catalog, pg_temp
            AS $function$
            DECLARE
                v_school_id uuid;
                v_batch_ids uuid[];
                v_removed integer := 0;
                v_now timestamptz := pg_catalog.statement_timestamp();
            BEGIN
                IF SESSION_USER IS DISTINCT FROM 'campus_ai_school_fact_writer'
                   OR pg_catalog.current_setting('app.context_purpose', true)
                       IS DISTINCT FROM 'command'
                   OR pg_catalog.current_setting('app.context_kind', true)
                       IS DISTINCT FROM 'assignment'
                   OR pg_catalog.current_setting('app.guardian_relationship_id', true)
                       IS DISTINCT FROM '-' THEN
                    RAISE EXCEPTION 'school fact preparation cleanup context rejected'
                        USING ERRCODE = '28000';
                END IF;
                v_school_id := app.require_school_context();

                SELECT pg_catalog.array_agg(
                           candidate.id
                           ORDER BY candidate.confirmation_expires_at, candidate.id
                       )
                  INTO v_batch_ids
                  FROM (
                      SELECT batch.id, batch.confirmation_expires_at
                        FROM public.master_data_import_batches AS batch
                       WHERE batch.school_id = v_school_id
                         AND batch.mode = 'canonical_atomic_changes'
                         AND batch.status = 'ready_for_confirmation'
                         AND batch.confirmation_consumed_at IS NULL
                         AND batch.confirmation_expires_at <= v_now
                  ) AS candidate;
                IF v_batch_ids IS NULL THEN
                    RETURN 0;
                END IF;

                DELETE FROM public.master_data_import_units AS unit
                 WHERE unit.school_id = v_school_id
                   AND unit.batch_id = ANY(v_batch_ids)
                   AND unit.status = 'ready'
                   AND unit.target_id IS NULL
                   AND unit.target_version IS NULL
                   AND unit.last_attempt_idempotency_record_id IS NULL
                   AND unit.last_attempt_completed IS NULL
                   AND EXISTS (
                       SELECT 1
                         FROM public.master_data_import_batches AS batch
                        WHERE batch.id = unit.batch_id
                          AND batch.school_id = unit.school_id
                          AND batch.mode = 'canonical_atomic_changes'
                          AND batch.status = 'ready_for_confirmation'
                          AND batch.confirmation_consumed_at IS NULL
                          AND batch.confirmation_expires_at <= v_now
                   );

                DELETE FROM public.master_data_import_batches AS batch
                 WHERE batch.id = ANY(v_batch_ids)
                   AND batch.school_id = v_school_id
                   AND batch.mode = 'canonical_atomic_changes'
                   AND batch.status = 'ready_for_confirmation'
                   AND batch.confirmation_consumed_at IS NULL
                   AND batch.confirmation_expires_at <= v_now;
                GET DIAGNOSTICS v_removed = ROW_COUNT;
                RETURN v_removed;
            EXCEPTION
                WHEN integrity_constraint_violation
                     OR invalid_text_representation
                     OR numeric_value_out_of_range THEN
                    RAISE EXCEPTION 'school fact preparation cleanup rejected'
                        USING ERRCODE = '28000';
            END
            $function$;
            """
        )
    )
    op.execute(
        f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC, campus_ai_runtime, "
        "campus_ai_school_fact_writer, campus_ai_school_fact_reader, "
        "campus_ai_migration, campus_ai_db_owner, campus_ai_identity_definer CASCADE"
    )
    op.execute("SET LOCAL ROLE campus_ai_db_owner")
    op.execute("REVOKE CREATE ON SCHEMA app FROM campus_ai_context_definer")


def _install_tenant_rls() -> None:
    reader_tables = frozenset(_GOVERNANCE_READER_TABLES + _R1_READER_TABLES)
    for table_name in _SCHOOL_BEARING_TABLES:
        tenant_column = "id" if table_name == "schools" else "school_id"
        nullable_guard = table_name in _SHARED_SUPPORT_TABLES
        tenant_predicate = (
            f"({tenant_column} IS NULL OR {tenant_column} = app.require_school_database_anchor())"
            if nullable_guard
            else f"{tenant_column} = app.require_school_database_anchor()"
        )
        owner_or_tenant = (
            f"CURRENT_USER IN ('campus_ai_db_owner', 'campus_ai_migration') OR ({tenant_predicate})"
        )
        context_predicate = f"{tenant_column} = app.require_school_context()"
        reader_predicate = f"{tenant_column} = app.require_school_database_anchor()"
        op.execute(f"ALTER TABLE public.{table_name} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE public.{table_name} FORCE ROW LEVEL SECURITY")
        if table_name in _RETIRED_REPAIRS_RUNTIME_ACCESS_TABLES:
            op.execute(
                sa.text(
                    f"""
                    DO $sf5b_drop_retired_repair_policies$
                    DECLARE
                        policy_name text;
                    BEGIN
                        FOR policy_name IN
                            SELECT policy.polname
                            FROM pg_catalog.pg_policy AS policy
                            WHERE policy.polrelid = 'public.{table_name}'::regclass
                        LOOP
                            EXECUTE pg_catalog.format(
                                'DROP POLICY %I ON public.{table_name}',
                                policy_name
                            );
                        END LOOP;
                    END
                    $sf5b_drop_retired_repair_policies$;
                    """
                )
            )
        if table_name in _LEGACY_CONTEXT_POLICY_DROP_TABLES:
            for legacy_policy_name in ("tenant_select", "owner_write"):
                op.execute(f"DROP POLICY IF EXISTS {legacy_policy_name} ON public.{table_name}")
        if table_name in _LEGACY_IMPORT_POLICY_DROP_TABLES:
            for legacy_policy_name in (
                "tenant_select",
                "tenant_insert",
                "tenant_update",
                "owner_delete",
            ):
                op.execute(f"DROP POLICY IF EXISTS {legacy_policy_name} ON public.{table_name}")
        for policy_name in (
            "sf5b_anchor_app_access",
            "sf5b_owner_migration_access",
            "sf5b_writer_context_access",
            "sf5b_reader_anchor_access",
            "sf5b_context_definer_anchor_access",
            "sf5b_audit_definer_append",
            "sf5b_audit_definer_returning",
            "sf5b_anchor_guard",
            "sf5b_writer_school_required",
            "sf5b_school_reactivation_prepare_insert",
            "sf5b_expired_preparation_cleanup_read",
            "sf5b_expired_preparation_cleanup_delete",
        ):
            op.execute(f"DROP POLICY IF EXISTS {policy_name} ON public.{table_name}")
        if table_name in _SF5B_APP_ACCESS_TABLES:
            op.execute(
                f"CREATE POLICY sf5b_owner_migration_access ON public.{table_name} "
                "AS PERMISSIVE FOR ALL TO campus_ai_db_owner, campus_ai_migration "
                "USING (CURRENT_USER IN ('campus_ai_db_owner', 'campus_ai_migration')) "
                "WITH CHECK (CURRENT_USER IN ('campus_ai_db_owner', 'campus_ai_migration'))"
            )
        if table_name in _SCHOOL_FACT_WRITER_TABLES:
            op.execute(
                f"CREATE POLICY sf5b_writer_context_access ON public.{table_name} "
                "AS PERMISSIVE FOR ALL TO campus_ai_school_fact_writer "
                f"USING ({context_predicate}) WITH CHECK ({context_predicate})"
            )
        if table_name in {"master_data_import_batches", "master_data_import_units"}:
            op.execute(
                f"CREATE POLICY sf5b_school_reactivation_prepare_insert "
                f"ON public.{table_name} AS PERMISSIVE FOR INSERT "
                "TO campus_ai_context_definer "
                "WITH CHECK (school_id = app.require_school_database_anchor())"
            )
        if table_name == "master_data_import_batches":
            expired_preparation_predicate = (
                "school_id = app.require_school_context() "
                "AND mode = 'canonical_atomic_changes' "
                "AND status = 'ready_for_confirmation' "
                "AND confirmation_consumed_at IS NULL "
                "AND confirmation_expires_at <= pg_catalog.statement_timestamp()"
            )
            op.execute(
                "CREATE POLICY sf5b_expired_preparation_cleanup_read "
                "ON public.master_data_import_batches AS PERMISSIVE FOR SELECT "
                "TO campus_ai_context_definer "
                f"USING ({expired_preparation_predicate})"
            )
            op.execute(
                "CREATE POLICY sf5b_expired_preparation_cleanup_delete "
                "ON public.master_data_import_batches AS PERMISSIVE FOR DELETE "
                "TO campus_ai_context_definer "
                f"USING ({expired_preparation_predicate})"
            )
        if table_name == "master_data_import_units":
            expired_preparation_predicate = (
                "school_id = app.require_school_context() "
                "AND status = 'ready' "
                "AND target_id IS NULL "
                "AND target_version IS NULL "
                "AND last_attempt_idempotency_record_id IS NULL "
                "AND last_attempt_completed IS NULL "
                "AND EXISTS ("
                "SELECT 1 FROM public.master_data_import_batches AS batch "
                "WHERE batch.id = master_data_import_units.batch_id "
                "AND batch.school_id = master_data_import_units.school_id "
                "AND batch.mode = 'canonical_atomic_changes' "
                "AND batch.status = 'ready_for_confirmation' "
                "AND batch.confirmation_consumed_at IS NULL "
                "AND batch.confirmation_expires_at <= pg_catalog.statement_timestamp()"
                ")"
            )
            op.execute(
                "CREATE POLICY sf5b_expired_preparation_cleanup_read "
                "ON public.master_data_import_units AS PERMISSIVE FOR SELECT "
                "TO campus_ai_context_definer "
                f"USING ({expired_preparation_predicate})"
            )
            op.execute(
                "CREATE POLICY sf5b_expired_preparation_cleanup_delete "
                "ON public.master_data_import_units AS PERMISSIVE FOR DELETE "
                "TO campus_ai_context_definer "
                f"USING ({expired_preparation_predicate})"
            )
        if table_name in reader_tables:
            op.execute(
                f"CREATE POLICY sf5b_reader_anchor_access ON public.{table_name} "
                "AS PERMISSIVE FOR SELECT TO campus_ai_school_fact_reader "
                f"USING ({reader_predicate})"
            )
        if table_name in _LEGACY_CONTEXT_POLICY_DROP_TABLES:
            op.execute(
                f"CREATE POLICY sf5b_context_definer_anchor_access ON public.{table_name} "
                "AS PERMISSIVE FOR SELECT TO campus_ai_context_definer "
                f"USING ({reader_predicate})"
            )
        if table_name == "audit_logs":
            op.execute(
                "CREATE POLICY sf5b_audit_definer_append ON public.audit_logs "
                "AS PERMISSIVE FOR INSERT TO campus_ai_audit_definer "
                "WITH CHECK (school_id IS NULL OR school_id = app.require_school_database_anchor())"
            )
            op.execute(
                "CREATE POLICY sf5b_audit_definer_returning ON public.audit_logs "
                "AS PERMISSIVE FOR SELECT TO campus_ai_audit_definer "
                "USING (school_id IS NULL OR school_id = app.require_school_database_anchor())"
            )
        op.execute(
            f"CREATE POLICY sf5b_anchor_guard ON public.{table_name} "
            "AS RESTRICTIVE FOR ALL TO PUBLIC "
            f"USING ({owner_or_tenant}) WITH CHECK ({owner_or_tenant})"
        )
        if table_name in _SHARED_SUPPORT_TABLES:
            op.execute(
                f"CREATE POLICY sf5b_writer_school_required ON public.{table_name} "
                "AS RESTRICTIVE FOR ALL TO campus_ai_school_fact_writer "
                "USING (school_id = app.require_school_database_anchor()) "
                "WITH CHECK (school_id = app.require_school_database_anchor())"
            )
        if table_name == "command_idempotency_records":
            op.execute(
                "DROP POLICY IF EXISTS sf5b_runtime_no_school_fact_idempotency "
                "ON public.command_idempotency_records"
            )
            op.execute(
                "CREATE POLICY sf5b_runtime_no_school_fact_idempotency "
                "ON public.command_idempotency_records AS RESTRICTIVE FOR ALL "
                "TO campus_ai_runtime USING (action_code <> 'school_facts.confirm') "
                "WITH CHECK (action_code <> 'school_facts.confirm')"
            )


def _revoke_all_column_privileges(
    table_name: str,
    *,
    grantees: tuple[str, ...],
) -> None:
    grantee_sql = ", ".join(grantees)
    op.execute(
        sa.text(
            f"""
            DO $sf5b_revoke_all_columns$
            DECLARE
                column_names text;
                privilege_name text;
            BEGIN
                SELECT pg_catalog.string_agg(
                           pg_catalog.format('%I', attribute.attname),
                           ', ' ORDER BY attribute.attnum
                       )
                  INTO column_names
                  FROM pg_catalog.pg_attribute AS attribute
                 WHERE attribute.attrelid = 'public.{table_name}'::regclass
                   AND attribute.attnum > 0
                   AND NOT attribute.attisdropped;
                FOREACH privilege_name IN ARRAY
                    ARRAY['SELECT', 'INSERT', 'UPDATE', 'REFERENCES']
                LOOP
                    EXECUTE pg_catalog.format(
                        'REVOKE %s (%s) ON TABLE public.{table_name} '
                        'FROM {grantee_sql} CASCADE',
                        privilege_name,
                        column_names
                    );
                END LOOP;
            END
            $sf5b_revoke_all_columns$;
            """
        )
    )


def _close_table_acl() -> None:
    isolated_roles = (
        "campus_ai_school_fact_writer",
        "campus_ai_school_fact_reader",
        "campus_ai_identity_definer",
    )
    for role_name in isolated_roles:
        op.execute(f"REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {role_name} CASCADE")
        op.execute(f"REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM {role_name} CASCADE")
    for table_name in _SCHOOL_BEARING_TABLES:
        op.execute(f"REVOKE ALL ON TABLE public.{table_name} FROM PUBLIC CASCADE")
        _revoke_all_column_privileges(table_name, grantees=("PUBLIC",))

    append_only_writer_tables = frozenset(
        {
            "audit_logs",
            "command_idempotency_records",
            "master_data_import_command_outcomes",
            "school_fact_change_lineage",
        }
    )
    for table_name in _SCHOOL_FACT_RUNTIME_DML_REVOKE_TABLES:
        if table_name not in _SHARED_SUPPORT_TABLES:
            op.execute(
                f"REVOKE ALL ON TABLE public.{table_name} FROM "
                "campus_ai_runtime, campus_ai_school_fact_reader CASCADE"
            )
            _revoke_all_column_privileges(
                table_name,
                grantees=("campus_ai_runtime", "campus_ai_school_fact_reader"),
            )

    for table_name in _RETIRED_REPAIRS_RUNTIME_ACCESS_TABLES:
        op.execute(
            f"REVOKE ALL ON TABLE public.{table_name} "
            "FROM campus_ai_runtime, campus_ai_school_fact_reader CASCADE"
        )
        _revoke_all_column_privileges(
            table_name,
            grantees=("campus_ai_runtime", "campus_ai_school_fact_reader"),
        )

    for table_name in _SCHOOL_FACT_WRITER_TABLES:
        if table_name in append_only_writer_tables:
            continue
        op.execute(
            f"GRANT SELECT, INSERT, UPDATE ON TABLE public.{table_name} "
            "TO campus_ai_school_fact_writer"
        )

    op.execute(
        "GRANT SELECT (source_kind, source_ref, filename_summary, source_size_bytes, "
        "worksheet_names, mapping_version, mapping_payload, confirmation_card_sha256, "
        "confirmed_at, created_at, version) "
        "ON TABLE public.master_data_import_batches TO campus_ai_context_definer"
    )
    op.execute(
        "GRANT SELECT (row_number, soft_anomalies, initial_soft_anomalies, "
        "decision_reason, error_code, error_field_code, "
        "last_attempt_idempotency_record_id, last_attempt_completed, version) "
        "ON TABLE public.master_data_import_units TO campus_ai_context_definer"
    )
    op.execute(
        "GRANT INSERT (id, school_id, mode, source_kind, source_ref, source_sha256, "
        "filename_summary, source_size_bytes, worksheet_names, mapping_version, "
        "mapping_payload, status, planned_units, succeeded_units, failed_units, "
        "pending_units, confirmation_card_sha256, request_id, idempotency_key_hash, "
        "plan_sha256, base_fact_version, target_fact_version, confirmation_token_sha256, "
        "confirmation_expires_at, confirmation_session_sha256, staged_by_user_id, "
        "staged_by_person_id, staged_by_assignment_id, staged_assignment_version, "
        "staged_authorization_version) ON TABLE public.master_data_import_batches "
        "TO campus_ai_context_definer"
    )
    op.execute(
        "GRANT INSERT (id, school_id, batch_id, sequence, row_number, unit_kind, status, "
        "payload, payload_sha256, soft_anomalies, initial_soft_anomalies) "
        "ON TABLE public.master_data_import_units TO campus_ai_context_definer"
    )
    for table_name in ("master_data_import_batches", "master_data_import_units"):
        op.execute(
            f"REVOKE DELETE ON TABLE public.{table_name} "
            "FROM campus_ai_context_definer CASCADE"
        )
        op.execute(
            f"GRANT DELETE ON TABLE public.{table_name} TO campus_ai_context_definer"
        )
    op.execute(
        "GRANT INSERT (id, school_id, action_code, actor_user_id, actor_person_id, "
        "actor_context_type, actor_context_key, actor_assignment_id, "
        "actor_assignment_version, resource_type, resource_id, idempotency_key_hash, "
        "request_sha256, status, result_type, result_payload) "
        "ON TABLE public.command_idempotency_records TO campus_ai_school_fact_writer"
    )
    op.execute(
        "REVOKE ALL ON TABLE public.audit_logs FROM campus_ai_runtime, "
        "campus_ai_school_fact_reader CASCADE"
    )
    op.execute(
        "GRANT INSERT (actor_user_id, school_id, actor_person_id, actor_assignment_id, "
        "actor_assignment_version, actor_context_type, idempotency_key_hash, event_type, "
        "target_type, target_id, before_summary, after_summary, request_id, reason) "
        "ON TABLE public.audit_logs TO campus_ai_school_fact_writer"
    )
    op.execute(
        "GRANT INSERT ON TABLE public.master_data_import_command_outcomes "
        "TO campus_ai_school_fact_writer"
    )
    op.execute(
        "GRANT SELECT, INSERT ON TABLE public.school_fact_change_lineage "
        "TO campus_ai_school_fact_writer"
    )
    op.execute(
        "GRANT SELECT ON TABLE public.alembic_version TO "
        "campus_ai_runtime, campus_ai_school_fact_writer, campus_ai_school_fact_reader"
    )

    for table_name in _GOVERNANCE_READER_TABLES:
        op.execute(
            f"REVOKE ALL ON TABLE public.{table_name} FROM campus_ai_school_fact_reader CASCADE"
        )
        op.execute(f"GRANT SELECT ON TABLE public.{table_name} TO campus_ai_school_fact_reader")

    # The fixed R1 compiler reads only these relations.  SQL projection, not this
    # role, remains the field-level enforcement point.
    for table_name in _R1_READER_TABLES:
        op.execute(f"GRANT SELECT ON TABLE public.{table_name} TO campus_ai_school_fact_reader")
    op.execute("REVOKE UPDATE ON TABLE public.users FROM campus_ai_runtime CASCADE")
    op.execute("GRANT UPDATE (display_name, updated_at) ON TABLE public.users TO campus_ai_runtime")
    op.execute("REVOKE ALL ON TABLE public.users FROM campus_ai_identity_definer CASCADE")
    op.execute(
        "GRANT SELECT (id, provider_code, provider_tenant_key, external_subject), "
        "UPDATE (provider_code, provider_tenant_key, external_subject, updated_at) "
        "ON TABLE public.users TO campus_ai_identity_definer"
    )
    op.execute(
        "REVOKE ALL ON SEQUENCE public.audit_logs_id_seq FROM campus_ai_school_fact_writer CASCADE"
    )
    op.execute("GRANT USAGE ON SEQUENCE public.audit_logs_id_seq TO campus_ai_school_fact_writer")


def _close_function_acl() -> None:
    op.execute(
        "GRANT USAGE ON SCHEMA app TO campus_ai_school_fact_writer, campus_ai_school_fact_reader"
    )

    op.execute("SET LOCAL ROLE campus_ai_context_definer")
    for signature in _WRITER_FUNCTIONS:
        op.execute(f"REVOKE EXECUTE ON FUNCTION {signature} FROM campus_ai_school_fact_writer")
        op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO campus_ai_school_fact_writer")
    for signature in _R1_READER_FUNCTIONS:
        op.execute(f"REVOKE EXECUTE ON FUNCTION {signature} FROM campus_ai_school_fact_reader")
        op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO campus_ai_school_fact_reader")
    op.execute("SET LOCAL ROLE campus_ai_db_owner")
    op.execute("SET LOCAL ROLE campus_ai_takeover_definer")
    for signature in _REVOKED_RUNTIME_WRITER_FUNCTIONS:
        op.execute(f"REVOKE EXECUTE ON FUNCTION {signature} FROM campus_ai_runtime")
    op.execute("SET LOCAL ROLE campus_ai_db_owner")


def upgrade() -> None:
    _create_anchor()
    _create_anchor_functions()
    _create_anchor_guards()
    _bind_provable_existing_database()
    _create_identity_capabilities()
    _create_school_reactivation_writer_capabilities()
    _create_expired_preparation_cleanup_capability()
    _install_tenant_rls()
    _close_table_acl()
    _close_function_acl()


def downgrade() -> None:
    raise RuntimeError(
        "revision 20 is an irreversible security boundary; downgrade must not restore "
        "dangerous runtime or importer privileges"
    )
