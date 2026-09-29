"""F1 external first-maintainer initialization; existing root/anchor remain unchanged."""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260905_21"
down_revision = "20260824_20"
branch_labels = None
depends_on = None

_INITIALIZATION_DDL = r"""

CREATE TABLE school_fact_initializations (
	id UUID NOT NULL,
	school_id UUID NOT NULL,
	request_id UUID NOT NULL,
	authority_reference VARCHAR(80) NOT NULL,
	operator_reference VARCHAR(1000) NOT NULL,
	approval_reference VARCHAR(1000) NOT NULL,
	manifest_sha256 VARCHAR(64) NOT NULL,
	manifest_payload JSONB NOT NULL,
	confirmation_token_sha256 VARCHAR(64) NOT NULL,
	expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
	state VARCHAR(20) NOT NULL,
	result_payload JSONB,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	applied_at TIMESTAMP WITH TIME ZONE,
	closed_at TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (id),
	CONSTRAINT uq_school_fact_initializations_school UNIQUE (school_id),
	CONSTRAINT uq_school_fact_initializations_school_id UNIQUE (school_id, id),
	CONSTRAINT uq_school_fact_initializations_request UNIQUE (request_id),
	CONSTRAINT ck_school_fact_initializations_state CHECK (state IN ('prepared', 'applied',
     'closed')),
	CONSTRAINT ck_school_fact_initializations_digest CHECK (manifest_sha256 ~ '^[0-9a-f]{64}$'),
	CONSTRAINT ck_school_fact_initializations_token CHECK (confirmation_token_sha256 ~
     '^[0-9a-f]{64}$'),
	CONSTRAINT ck_school_fact_initializations_authority CHECK (authority_reference =
     'deployment:campus_ai_migration'),
	CONSTRAINT ck_school_fact_initializations_result CHECK ((state = 'prepared' AND applied_at IS
     NULL AND closed_at IS NULL AND result_payload IS NULL) OR (state = 'applied' AND applied_at
     IS NOT NULL AND closed_at IS NULL AND result_payload IS NOT NULL) OR (state = 'closed' AND
     applied_at IS NOT NULL AND closed_at IS NOT NULL AND result_payload IS NOT NULL)),
	FOREIGN KEY(school_id) REFERENCES schools (id) ON DELETE RESTRICT
)


"""

_BATCH_GUARD = r"""
        CREATE OR REPLACE FUNCTION app_private.enforce_master_data_import_batch_guard()
        RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = pg_catalog
        AS $function$
        DECLARE
            v_validate_confirmer boolean := false;
        BEGIN
            IF TG_OP = 'UPDATE' THEN
                IF OLD.status IN ('applied', 'published')
                   AND NEW IS DISTINCT FROM OLD THEN
                    RAISE EXCEPTION 'terminal master data import batch is immutable'
                        USING ERRCODE = '55000';
                END IF;

                IF (OLD.confirmed_by_user_id, OLD.confirmed_by_person_id,
                    OLD.confirmed_by_assignment_id, OLD.confirmed_at)
                   IS DISTINCT FROM
                   (NEW.confirmed_by_user_id, NEW.confirmed_by_person_id,
                    NEW.confirmed_by_assignment_id, NEW.confirmed_at) THEN
                    IF OLD.confirmed_by_user_id IS NOT NULL
                       OR OLD.confirmed_by_person_id IS NOT NULL
                       OR OLD.confirmed_by_assignment_id IS NOT NULL
                       OR OLD.confirmed_at IS NOT NULL THEN
                        RAISE EXCEPTION 'master data import confirmer is immutable'
                            USING ERRCODE = '55000';
                    END IF;
                    v_validate_confirmer := true;
                END IF;
            ELSIF NEW.confirmed_by_user_id IS NOT NULL
               OR NEW.confirmed_by_person_id IS NOT NULL
               OR NEW.confirmed_by_assignment_id IS NOT NULL
               OR NEW.confirmed_at IS NOT NULL THEN
                v_validate_confirmer := true;
            END IF;

            IF TG_OP = 'UPDATE' AND OLD.initialization_id IS DISTINCT FROM NEW.initialization_id
                THEN
                RAISE EXCEPTION 'initialization identity is immutable' USING ERRCODE = '55000';
            END IF;
            IF NEW.initialization_id IS NOT NULL THEN
                IF SESSION_USER <> 'campus_ai_migration' OR NOT EXISTS (
                    SELECT 1 FROM public.school_fact_initializations AS initialization
                    WHERE initialization.id = NEW.initialization_id AND initialization.school_id
                        = NEW.school_id
                      AND initialization.state = 'prepared'
                      AND initialization.request_id = NEW.request_id
                      AND initialization.manifest_payload->'change_set'->>'source_sha256' =
                          NEW.source_sha256
                ) THEN
                    RAISE EXCEPTION 'initialization batch authority rejected' USING ERRCODE =
                        '28000';
                END IF;
                RETURN NEW;
            END IF;
            IF v_validate_confirmer THEN
                IF NEW.confirmed_by_user_id IS NULL
                   OR NEW.confirmed_by_person_id IS NULL
                   OR NEW.confirmed_by_assignment_id IS NULL
                   OR NEW.confirmed_at IS NULL
                   OR current_setting('app.context_purpose', true)
                      IS DISTINCT FROM 'command'
                   OR current_setting('app.context_kind', true)
                      IS DISTINCT FROM 'assignment'
                   OR NEW.confirmed_by_user_id IS DISTINCT FROM
                      current_setting('app.account_user_id', true)::integer
                   OR NEW.confirmed_by_person_id IS DISTINCT FROM
                      current_setting('app.person_id', true)::uuid
                   OR NEW.confirmed_by_assignment_id IS DISTINCT FROM
                      current_setting('app.assignment_id', true)::uuid
                   OR NOT EXISTS (
                       SELECT 1
                       FROM public.staff_assignments AS assignment
                       WHERE assignment.school_id = NEW.school_id
                         AND assignment.id = NEW.confirmed_by_assignment_id
                         AND assignment.person_id = NEW.confirmed_by_person_id
                         AND assignment.version =
                             current_setting('app.assignment_version', true)::bigint
                         AND assignment.status = 'confirmed'
                         AND assignment.valid_from <= pg_catalog.clock_timestamp()
                         AND (
                             assignment.valid_to IS NULL
                             OR assignment.valid_to > pg_catalog.clock_timestamp()
                         )
                   ) THEN
                    RAISE EXCEPTION 'master data import confirmer context mismatch'
                        USING ERRCODE = '28000';
                END IF;
            END IF;
            RETURN NEW;
        END
        $function$
"""

_TERMINAL_GUARD = r"""
        CREATE OR REPLACE FUNCTION app_private.verify_master_data_import_terminal_consistency(
            p_school_id uuid,
            p_batch_id uuid
        )
        RETURNS void
        LANGUAGE plpgsql
        SET search_path = pg_catalog
        AS $function$
        DECLARE
            v_batch record;
            v_outcome record;
            v_idempotency record;
            v_outcome_count bigint;
            v_succeeded bigint;
            v_failed bigint;
            v_pending bigint;
            v_normalized integer[];
            v_union integer[];
        BEGIN
            SELECT batch.status,
                   batch.version,
                   batch.succeeded_units,
                   batch.failed_units,
                   batch.pending_units
            INTO v_batch
            FROM public.master_data_import_batches AS batch
            WHERE batch.school_id = p_school_id
              AND batch.id = p_batch_id;
            IF NOT FOUND OR v_batch.status NOT IN ('applied', 'published') THEN
                RETURN;
            END IF;

            SELECT count(*) FILTER (WHERE unit.status IN ('applied', 'published')),
                   count(*) FILTER (WHERE unit.status = 'failed'),
                   count(*) FILTER (
                       WHERE unit.status NOT IN ('applied', 'published', 'failed')
                   )
            INTO v_succeeded, v_failed, v_pending
            FROM public.master_data_import_units AS unit
            WHERE unit.school_id = p_school_id
              AND unit.batch_id = p_batch_id;
            IF (v_succeeded, v_failed, v_pending) IS DISTINCT FROM
               (v_batch.succeeded_units, v_batch.failed_units, v_batch.pending_units) THEN
                RAISE EXCEPTION 'master data import terminal counts are inconsistent'
                    USING ERRCODE = '23514';
            END IF;

            IF EXISTS (SELECT 1 FROM public.master_data_import_batches
                       WHERE id = p_batch_id AND school_id = p_school_id AND initialization_id
                           IS NOT NULL) THEN
                IF NOT EXISTS (
                    SELECT 1 FROM public.master_data_import_batches AS batch
                    JOIN public.school_fact_initializations AS initialization
                      ON initialization.id = batch.initialization_id AND
                          initialization.school_id = batch.school_id
                    WHERE batch.id = p_batch_id AND batch.school_id = p_school_id
                      AND initialization.state IN ('applied', 'closed')
                      AND initialization.result_payload->>'batch_id' = batch.id::text
                      AND (initialization.result_payload->>'changed_count')::bigint =
                          batch.succeeded_units
                      AND batch.failed_units = 0 AND batch.pending_units = 0
                ) THEN
                    RAISE EXCEPTION 'initialization terminal receipt mismatch' USING ERRCODE =
                        '23514';
                END IF;
                RETURN;
            END IF;
            SELECT count(*)
            INTO v_outcome_count
            FROM public.master_data_import_command_outcomes AS outcome
            WHERE outcome.school_id = p_school_id
              AND outcome.batch_id = p_batch_id
              AND outcome.batch_version = v_batch.version
              AND outcome.status = v_batch.status;
            IF v_outcome_count <> 1 THEN
                RAISE EXCEPTION 'master data import terminal outcome is missing or ambiguous'
                    USING ERRCODE = '23514';
            END IF;

            SELECT outcome.idempotency_record_id,
                   outcome.status,
                   outcome.batch_version,
                   outcome.succeeded_units,
                   outcome.failed_units,
                   outcome.executed_units
            INTO STRICT v_outcome
            FROM public.master_data_import_command_outcomes AS outcome
            WHERE outcome.school_id = p_school_id
              AND outcome.batch_id = p_batch_id
              AND outcome.batch_version = v_batch.version
              AND outcome.status = v_batch.status;

            SELECT COALESCE(array_agg(value ORDER BY value), '{}'::integer[])
            INTO v_normalized
            FROM (
                SELECT DISTINCT value
                FROM unnest(v_outcome.succeeded_units) AS item(value)
                WHERE value IS NOT NULL
            ) AS normalized;
            IF v_normalized IS DISTINCT FROM v_outcome.succeeded_units THEN
                RAISE EXCEPTION 'master data import succeeded units are not canonical'
                    USING ERRCODE = '23514';
            END IF;

            SELECT COALESCE(array_agg(value ORDER BY value), '{}'::integer[])
            INTO v_normalized
            FROM (
                SELECT DISTINCT value
                FROM unnest(v_outcome.failed_units) AS item(value)
                WHERE value IS NOT NULL
            ) AS normalized;
            IF v_normalized IS DISTINCT FROM v_outcome.failed_units THEN
                RAISE EXCEPTION 'master data import failed units are not canonical'
                    USING ERRCODE = '23514';
            END IF;

            SELECT COALESCE(array_agg(value ORDER BY value), '{}'::integer[])
            INTO v_normalized
            FROM (
                SELECT DISTINCT value
                FROM unnest(v_outcome.executed_units) AS item(value)
                WHERE value IS NOT NULL
            ) AS normalized;
            IF v_normalized IS DISTINCT FROM v_outcome.executed_units THEN
                RAISE EXCEPTION 'master data import executed units are not canonical'
                    USING ERRCODE = '23514';
            END IF;

            IF EXISTS (
                SELECT 1
                FROM unnest(v_outcome.succeeded_units) AS succeeded(sequence)
                JOIN unnest(v_outcome.failed_units) AS failed(sequence) USING (sequence)
            ) THEN
                RAISE EXCEPTION 'master data import outcome unit sets overlap'
                    USING ERRCODE = '23514';
            END IF;
            SELECT COALESCE(array_agg(sequence ORDER BY sequence), '{}'::integer[])
            INTO v_union
            FROM (
                SELECT sequence
                FROM unnest(v_outcome.succeeded_units) AS succeeded(sequence)
                UNION
                SELECT sequence
                FROM unnest(v_outcome.failed_units) AS failed(sequence)
            ) AS combined;
            IF v_union IS DISTINCT FROM v_outcome.executed_units THEN
                RAISE EXCEPTION 'master data import executed units do not match outcome sets'
                    USING ERRCODE = '23514';
            END IF;

            IF EXISTS (
                SELECT 1
                FROM unnest(v_outcome.succeeded_units) AS expected(sequence)
                LEFT JOIN public.master_data_import_units AS unit
                  ON unit.school_id = p_school_id
                 AND unit.batch_id = p_batch_id
                 AND unit.sequence = expected.sequence
                WHERE unit.id IS NULL OR unit.status NOT IN ('applied', 'published')
            ) OR EXISTS (
                SELECT 1
                FROM unnest(v_outcome.failed_units) AS expected(sequence)
                LEFT JOIN public.master_data_import_units AS unit
                  ON unit.school_id = p_school_id
                 AND unit.batch_id = p_batch_id
                 AND unit.sequence = expected.sequence
                WHERE unit.id IS NULL OR unit.status <> 'failed'
            ) THEN
                RAISE EXCEPTION 'master data import outcome units do not match unit states'
                    USING ERRCODE = '23514';
            END IF;

            SELECT record.status, record.result_type, record.result_payload
            INTO v_idempotency
            FROM public.command_idempotency_records AS record
            WHERE record.school_id = p_school_id
              AND record.id = v_outcome.idempotency_record_id;
            IF NOT FOUND
               OR v_idempotency.status IS DISTINCT FROM 'succeeded'
               OR v_idempotency.result_type IS DISTINCT FROM 'master_data.import_outcome'
               OR v_idempotency.result_payload ->> 'resource_type'
                  IS DISTINCT FROM 'master_data.import_batch'
               OR v_idempotency.result_payload ->> 'resource_id'
                  IS DISTINCT FROM p_batch_id::text
               OR v_idempotency.result_payload ->> 'resource_version'
                  IS DISTINCT FROM v_outcome.batch_version::text
               OR v_idempotency.result_payload ->> 'outcome_code'
                  IS DISTINCT FROM v_outcome.status THEN
                RAISE EXCEPTION 'master data import idempotency result is inconsistent'
                    USING ERRCODE = '23514';
            END IF;
        END
        $function$
"""


def upgrade() -> None:
    op.execute(_INITIALIZATION_DDL)
    op.add_column(
        "master_data_import_batches", sa.Column("initialization_id", sa.Uuid(), nullable=True)
    )
    op.create_foreign_key(
        "fk_master_data_import_batches_initialization",
        "master_data_import_batches",
        "school_fact_initializations",
        ["school_id", "initialization_id"],
        ["school_id", "id"],
        ondelete="RESTRICT",
    )
    op.drop_constraint(
        "ck_master_data_import_batches_confirmation_state",
        "master_data_import_batches",
        type_="check",
    )
    op.create_check_constraint(
        "ck_master_data_import_batches_confirmation_state",
        "master_data_import_batches",
        "(initialization_id IS NULL AND ((status IN ('source_registered', "
        "'mapped', 'preflight_failed', 'ready_for_confirmation') AND "
        "confirmed_by_user_id IS NULL AND confirmed_by_person_id IS NULL AND "
        "confirmed_by_assignment_id IS NULL AND confirmed_at IS NULL) OR "
        "(status IN ('applying', 'partially_failed', 'applied', 'published') "
        "AND confirmed_by_user_id IS NOT NULL AND confirmed_by_person_id IS "
        "NOT NULL AND confirmed_by_assignment_id IS NOT NULL AND "
        "confirmed_at IS NOT NULL))) OR (initialization_id IS NOT NULL AND "
        "confirmed_by_user_id IS NULL AND confirmed_by_person_id IS NULL AND "
        "confirmed_by_assignment_id IS NULL AND confirmed_at IS NULL)",
    )
    op.drop_constraint(
        "ck_master_data_import_batches_canonical_state", "master_data_import_batches", type_="check"
    )
    op.create_check_constraint(
        "ck_master_data_import_batches_canonical_state",
        "master_data_import_batches",
        "(mode <> 'canonical_atomic_changes' AND initialization_id IS NULL "
        "AND plan_sha256 IS NULL AND base_fact_version IS NULL AND "
        "target_fact_version IS NULL AND confirmation_token_sha256 IS NULL "
        "AND confirmation_expires_at IS NULL AND confirmation_consumed_at IS "
        "NULL AND confirmation_session_sha256 IS NULL AND staged_by_user_id "
        "IS NULL AND staged_by_person_id IS NULL AND staged_by_assignment_id "
        "IS NULL AND staged_assignment_version IS NULL AND "
        "staged_authorization_version IS NULL AND result_payload IS NULL) OR "
        "(mode = 'canonical_atomic_changes' AND plan_sha256 ~ "
        "'^[0-9a-f]{64}$' AND base_fact_version >= 1 AND target_fact_version "
        ">= base_fact_version AND confirmation_token_sha256 ~ "
        "'^[0-9a-f]{64}$' AND confirmation_expires_at IS NOT NULL AND "
        "confirmation_session_sha256 ~ '^[0-9a-f]{64}$' AND "
        "((initialization_id IS NULL AND staged_by_user_id IS NOT NULL AND "
        "staged_by_person_id IS NOT NULL AND staged_by_assignment_id IS NOT "
        "NULL AND staged_assignment_version >= 1 AND "
        "staged_authorization_version >= 1) OR (initialization_id IS NOT "
        "NULL AND staged_by_user_id IS NULL AND staged_by_person_id IS NULL "
        "AND staged_by_assignment_id IS NULL AND staged_assignment_version "
        "IS NULL AND staged_authorization_version IS NULL)))",
    )
    op.execute(_BATCH_GUARD)
    op.execute(_TERMINAL_GUARD)
    op.execute("ALTER TABLE public.school_fact_initializations ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE public.school_fact_initializations FORCE ROW LEVEL SECURITY")
    op.execute(
        "REVOKE ALL ON public.school_fact_initializations FROM PUBLIC, "
        "campus_ai_runtime, campus_ai_school_fact_reader, "
        "campus_ai_school_fact_writer"
    )
    op.execute(
        "GRANT SELECT, INSERT, UPDATE ON public.school_fact_initializations TO campus_ai_migration"
    )
    op.execute(
        "CREATE POLICY initialization_deployment ON "
        "public.school_fact_initializations FOR ALL TO campus_ai_db_owner, "
        "campus_ai_migration USING (school_id = "
        "app.require_school_database_anchor()) WITH CHECK (school_id = "
        "app.require_school_database_anchor())"
    )
    op.execute(
        sa.text("""
        CREATE FUNCTION app_private.guard_school_fact_initialization() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog AS $guard$
        BEGIN
            IF SESSION_USER <> 'campus_ai_migration' OR TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'initialization authority rejected' USING ERRCODE = '28000';
            END IF;
            IF TG_OP = 'UPDATE' THEN
                IF OLD.state = 'closed' OR
                   (OLD.id, OLD.school_id, OLD.request_id, OLD.authority_reference,
                       OLD.operator_reference,
                    OLD.approval_reference, OLD.manifest_sha256, OLD.manifest_payload,
                        OLD.created_at)
                   IS DISTINCT FROM
                   (NEW.id, NEW.school_id, NEW.request_id, NEW.authority_reference,
                       NEW.operator_reference,
                    NEW.approval_reference, NEW.manifest_sha256, NEW.manifest_payload,
                        NEW.created_at)
                   OR (OLD.state = 'applied' AND (NEW.state <> 'closed' OR
                       OLD.result_payload IS DISTINCT FROM NEW.result_payload OR
                       OLD.applied_at IS DISTINCT FROM NEW.applied_at)) THEN
                    RAISE EXCEPTION 'initialization immutable identity or closed state' USING
                        ERRCODE = '55000';
                END IF;
            END IF;
            RETURN NEW;
        END $guard$;
        ALTER FUNCTION app_private.guard_school_fact_initialization() OWNER TO campus_ai_db_owner;
        REVOKE ALL ON FUNCTION app_private.guard_school_fact_initialization() FROM PUBLIC;
        CREATE TRIGGER guard_school_fact_initialization BEFORE INSERT OR UPDATE OR DELETE
        ON public.school_fact_initializations FOR EACH ROW EXECUTE FUNCTION
            app_private.guard_school_fact_initialization();
    """)
    )
    op.execute(
        sa.text("""
        CREATE FUNCTION app.lock_school_fact_governance_epoch(p_school_id uuid, p_assignment_id
            uuid)
        RETURNS bigint LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $lock$
        DECLARE v_epoch bigint;
        BEGIN
            IF SESSION_USER <> 'campus_ai_school_fact_writer'
               OR app.require_school_context() IS DISTINCT FROM p_school_id
               OR current_setting('app.context_purpose', true) IS DISTINCT FROM 'command'
               OR current_setting('app.context_kind', true) IS DISTINCT FROM 'assignment'
               OR current_setting('app.assignment_id', true)::uuid IS DISTINCT FROM
                   p_assignment_id THEN
                RAISE EXCEPTION 'governance epoch authority rejected' USING ERRCODE = '28000';
            END IF;
            SELECT epoch INTO v_epoch FROM public.governance_assignment_epochs
             WHERE school_id = p_school_id AND assignment_id = p_assignment_id FOR SHARE;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'governance epoch is unavailable' USING ERRCODE = '28000';
            END IF;
            RETURN v_epoch;
        END $lock$;
        ALTER FUNCTION app.lock_school_fact_governance_epoch(uuid,uuid) OWNER TO
            campus_ai_db_owner;
        REVOKE ALL ON FUNCTION app.lock_school_fact_governance_epoch(uuid,uuid)
          FROM PUBLIC, campus_ai_runtime, campus_ai_school_fact_reader, campus_ai_migration;
        GRANT EXECUTE ON FUNCTION app.lock_school_fact_governance_epoch(uuid,uuid) TO
            campus_ai_school_fact_writer;
        GRANT EXECUTE ON FUNCTION app.require_school_context() TO campus_ai_db_owner;
        GRANT EXECUTE ON FUNCTION app.lock_existing_identity_user_id(text,text,text) TO
            campus_ai_migration, campus_ai_db_owner;
    """)
    )
    for table, columns in (
        ("school_fact_sources", "id,school_id,source_kind,created_at"),
        (
            "school_fact_change_lineage",
            "id,school_id,batch_id,fact_kind,fact_source_key,operation,target_id,target_version,created_at",
        ),
    ):
        op.execute(f"GRANT SELECT ({columns}) ON public.{table} TO campus_ai_school_fact_reader")
        op.execute(
            f"CREATE POLICY f1_reader_source ON public.{table} FOR SELECT "
            "TO campus_ai_school_fact_reader "
            "USING (school_id = app.require_school_database_anchor())"
        )

    op.execute(
        sa.text("""
        CREATE FUNCTION app.school_fact_current_version(p_school_id uuid) RETURNS bigint
        LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog AS $version$
        DECLARE v_version bigint;
        BEGIN
            IF SESSION_USER <> 'campus_ai_school_fact_reader'
               OR app.require_school_database_anchor() IS DISTINCT FROM p_school_id THEN
                RAISE EXCEPTION 'fact version authority rejected' USING ERRCODE = '28000';
            END IF;
            SELECT COALESCE(MAX(target_fact_version), 1) INTO v_version
              FROM public.master_data_import_batches WHERE school_id = p_school_id
               AND mode = 'canonical_atomic_changes' AND status = 'applied';
            RETURN v_version;
        END $version$;
        ALTER FUNCTION app.school_fact_current_version(uuid) OWNER TO campus_ai_db_owner;
        REVOKE ALL ON FUNCTION app.school_fact_current_version(uuid)
          FROM PUBLIC, campus_ai_runtime, campus_ai_school_fact_writer, campus_ai_migration;
        GRANT EXECUTE ON FUNCTION app.school_fact_current_version(uuid)
          TO campus_ai_school_fact_reader;
        GRANT SELECT (source_key) ON public.campus_locations TO campus_ai_school_fact_reader;
    """)
    )

    op.execute(
        sa.text("""
        CREATE FUNCTION app.require_initial_publication_window(
            p_school_id uuid, p_initialization_id uuid, p_scope_id uuid, p_manifest_sha256 text
        ) RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $window$
        DECLARE v_state text; v_version bigint; v_document jsonb; v_count bigint;
        BEGIN
            IF SESSION_USER <> 'campus_ai_school_fact_writer'
               OR app.require_school_context() IS DISTINCT FROM p_school_id
               OR current_setting('app.context_purpose', true) IS DISTINCT FROM 'command'
               OR current_setting('app.context_kind', true) IS DISTINCT FROM 'assignment' THEN
                RAISE EXCEPTION 'publication context rejected' USING ERRCODE = '28000';
            END IF;
            SELECT state INTO v_state FROM public.school_fact_initializations
             WHERE school_id=p_school_id AND id=p_initialization_id FOR SHARE;
            IF NOT FOUND OR v_state <> 'applied' THEN
                RAISE EXCEPTION 'initial identity is not open for publication'
                    USING ERRCODE='28000';
            END IF;
            SELECT state INTO v_state FROM public.governance_bootstrap_states
             WHERE school_id=p_school_id FOR SHARE;
            IF NOT FOUND OR v_state <> 'open' THEN
                RAISE EXCEPTION 'publication bootstrap is closed' USING ERRCODE='28000';
            END IF;
            SELECT s.current_version, v.reference_document INTO v_version, v_document
              FROM public.governance_scopes s JOIN public.governance_scope_versions v
                ON v.school_id=s.school_id AND v.scope_id=s.id AND v.version=1
             WHERE s.school_id=p_school_id AND s.id=p_scope_id
               AND s.scope_key='f1:initial-publication:' || p_initialization_id::text
               AND v.schema_owner='school_facts.initial_publication'
               AND v.schema_name='approved_manifest' AND v.schema_version=1
               FOR SHARE OF s, v;
            IF NOT FOUND OR v_version <> 1
               OR v_document->>'initialization_id' IS DISTINCT FROM p_initialization_id::text
               OR v_document->>'school_id' IS DISTINCT FROM p_school_id::text
               OR v_document->>'assignment_id' IS DISTINCT FROM
                   current_setting('app.assignment_id',true)
               OR v_document->>'publication_manifest_sha256' IS DISTINCT FROM p_manifest_sha256
               OR NOT COALESCE(
                    (v_document->>'approved_at')::timestamptz <= statement_timestamp(), false) THEN
                RAISE EXCEPTION 'publication manifest rejected' USING ERRCODE='28000';
            END IF;
            SELECT count(*) INTO v_count FROM public.governance_grants g
              JOIN public.governance_duties d ON d.school_id=g.school_id AND d.id=g.duty_id
              JOIN public.governance_grant_versions v ON v.school_id=g.school_id
                AND v.grant_id=g.id AND v.version=g.current_version
             WHERE g.school_id=p_school_id
               AND g.assignment_id=current_setting('app.assignment_id',true)::uuid
               AND d.duty_code='initial_school_fact_publisher'
               AND v.scope_id=p_scope_id AND v.scope_version=1
               AND v.transition_kind='create' AND v.version=1
               AND v.formal_ended_at IS NULL AND v.revoked_at IS NULL
               AND v.effective_from=v.valid_from AND v.valid_from <= statement_timestamp()
               AND statement_timestamp() < v.planned_valid_to
               AND v.planned_valid_to > v.valid_from
               AND v.planned_valid_to-v.valid_from <= interval '15 minutes';
            IF v_count <> 1 THEN
                RAISE EXCEPTION 'publication current grant window rejected' USING ERRCODE='28000';
            END IF;
        END $window$;
        ALTER FUNCTION app.require_initial_publication_window(uuid,uuid,uuid,text)
          OWNER TO campus_ai_db_owner;
        REVOKE ALL ON FUNCTION app.require_initial_publication_window(uuid,uuid,uuid,text)
          FROM PUBLIC, campus_ai_runtime, campus_ai_school_fact_reader, campus_ai_migration;
        GRANT EXECUTE ON FUNCTION app.require_initial_publication_window(uuid,uuid,uuid,text)
          TO campus_ai_school_fact_writer;
    """)
    )


def downgrade() -> None:
    raise RuntimeError(
        "F1 initialization history and revoked authority cannot be downgraded destructively."
    )
