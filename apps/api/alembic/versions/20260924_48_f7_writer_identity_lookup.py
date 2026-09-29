"""Give the fact Writer a bounded identity lookup on rebuilt F7 databases.

Revision ID: 20260924_48
Revises: 20260924_47

The identity-owned function has different historical ACLs in the running and
reconstructed databases. Keep that function's ACL intact. This wrapper is
owned by the school-fact database owner and is callable only by the Writer
inside an authenticated school-fact command context.
"""

from alembic import op

revision = "20260924_48"
down_revision = "20260924_47"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL ROLE campus_ai_db_owner")
    op.execute(
        """
        CREATE FUNCTION app.school_fact_lock_existing_identity_user_id(
            p_provider_code text,
            p_provider_tenant_key text,
            p_external_subject text
        ) RETURNS bigint
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp
        AS $function$
        DECLARE
            v_user_id bigint;
        BEGIN
            IF SESSION_USER <> 'campus_ai_school_fact_writer'
               OR current_setting('app.context_purpose', true) IS DISTINCT FROM 'command'
               OR current_setting('app.context_kind', true) IS DISTINCT FROM 'assignment'
               OR app.require_school_context() IS NULL THEN
                RAISE EXCEPTION 'school fact identity lookup authority rejected'
                    USING ERRCODE = '28000';
            END IF;
            v_user_id := app.lock_existing_identity_user_id(
                p_provider_code, p_provider_tenant_key, p_external_subject
            );
            RETURN v_user_id;
        END
        $function$;
        """
    )
    op.execute(
        "REVOKE ALL ON FUNCTION "
        "app.school_fact_lock_existing_identity_user_id(text,text,text) "
        "FROM PUBLIC, campus_ai_runtime, campus_ai_school_fact_reader, "
        "campus_ai_school_fact_writer, campus_ai_migration"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION app.school_fact_lock_existing_identity_user_id"
        "(text,text,text) TO campus_ai_school_fact_writer"
    )


def downgrade() -> None:
    raise RuntimeError("F7 identity capability is append-only; do not reopen the old ACL")
