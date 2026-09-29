"""Expose only F1 Reader assignment/person/organization authority judgments."""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260905_22"
down_revision = "20260905_21"
branch_labels = None
depends_on = None

_READER_FUNCTIONS = (
    "app.school_fact_reader_assignment_terminal(uuid,uuid,bigint)",
    "app.school_fact_reader_identity_root_is_effective_at(uuid,text,uuid,timestamptz)",
)


def upgrade() -> None:
    # The lineage policy predates the separate Reader role. Its signed runtime
    # context must not run for Reader queries, which already have anchor-only RLS.
    # Writer, owner/migration and context definer retain their dedicated policies.
    op.execute(
        "ALTER POLICY tenant_select ON public.school_fact_change_lineage "
        "TO campus_ai_runtime"
    )
    op.execute("GRANT CREATE ON SCHEMA app TO campus_ai_context_definer")
    op.execute("SET LOCAL ROLE campus_ai_context_definer")
    op.execute(
        sa.text(
            """
            CREATE FUNCTION app.school_fact_reader_assignment_terminal(
                p_school_id uuid,
                p_target_id uuid,
                p_content_version bigint
            )
            RETURNS TABLE(operation text, effective_at timestamptz)
            LANGUAGE plpgsql
            STABLE
            SECURITY DEFINER
            SET search_path = pg_catalog
            AS $function$
            BEGIN
                IF SESSION_USER <> 'campus_ai_school_fact_reader' THEN
                    RAISE EXCEPTION 'reader assignment authority rejected'
                        USING ERRCODE = '28000';
                END IF;
                IF p_school_id IS NULL OR p_target_id IS NULL
                   OR p_content_version IS NULL OR p_content_version < 1 THEN
                    RAISE EXCEPTION 'reader assignment arguments rejected'
                        USING ERRCODE = '22023';
                END IF;
                IF app.require_school_database_anchor() IS DISTINCT FROM p_school_id THEN
                    RAISE EXCEPTION 'reader assignment school rejected'
                        USING ERRCODE = '28000';
                END IF;
                RETURN QUERY
                SELECT terminal.operation, terminal.effective_at
                FROM app.school_fact_content_version_terminal(
                    p_school_id, 'assignment', p_target_id, p_content_version
                ) AS terminal;
            END
            $function$;

            CREATE FUNCTION app.school_fact_reader_identity_root_is_effective_at(
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
            BEGIN
                IF SESSION_USER <> 'campus_ai_school_fact_reader' THEN
                    RAISE EXCEPTION 'reader identity root authority rejected'
                        USING ERRCODE = '28000';
                END IF;
                IF p_school_id IS NULL OR p_target_id IS NULL OR p_as_of IS NULL
                   OR p_fact_kind IS NULL OR p_fact_kind NOT IN ('person', 'organization') THEN
                    RAISE EXCEPTION 'reader identity root arguments rejected'
                        USING ERRCODE = '22023';
                END IF;
                IF app.require_school_database_anchor() IS DISTINCT FROM p_school_id THEN
                    RAISE EXCEPTION 'reader identity root school rejected'
                        USING ERRCODE = '28000';
                END IF;
                RETURN app.school_fact_root_is_effective_at(
                    p_school_id, p_fact_kind, p_target_id, p_as_of
                );
            END
            $function$;
            """
        )
    )
    for signature in _READER_FUNCTIONS:
        op.execute(
            f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC, campus_ai_runtime, "
            "campus_ai_school_fact_writer, campus_ai_migration, campus_ai_db_owner, "
            "campus_ai_identity_definer, campus_ai_school_fact_reader"
        )
        op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO campus_ai_school_fact_reader")
    op.execute("SET LOCAL ROLE campus_ai_db_owner")
    op.execute("REVOKE CREATE ON SCHEMA app FROM campus_ai_context_definer")


def downgrade() -> None:
    op.execute("SET LOCAL ROLE campus_ai_context_definer")
    for signature in reversed(_READER_FUNCTIONS):
        op.execute(f"DROP FUNCTION {signature}")
    op.execute("SET LOCAL ROLE campus_ai_db_owner")
    op.execute("ALTER POLICY tenant_select ON public.school_fact_change_lineage TO PUBLIC")
