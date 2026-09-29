"""Repair F2 person RLS and project authoritative policy business start time."""

import sqlalchemy as sa

from alembic import op

revision = "20260906_24"
down_revision = "20260906_23"
branch_labels = None
depends_on = None


_POLICY_TIME_SIGNATURE = "app.school_fact_f2_reader_policy_valid_from(uuid,uuid)"
_POLICY_TIME_DDL = r"""
CREATE FUNCTION app.school_fact_f2_reader_policy_valid_from(
    p_school_id uuid, p_target_id uuid
)
RETURNS timestamptz
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog
AS $policy_time$
BEGIN
    IF SESSION_USER <> 'campus_ai_school_fact_reader'
       OR CURRENT_USER <> 'campus_ai_context_definer' THEN
        RAISE EXCEPTION 'F2 policy time reader rejected' USING ERRCODE = '28000';
    END IF;
    IF p_school_id IS NULL OR p_target_id IS NULL THEN
        RAISE EXCEPTION 'F2 policy time arguments rejected' USING ERRCODE = '22023';
    END IF;
    IF app.require_school_database_anchor() IS DISTINCT FROM p_school_id THEN
        RAISE EXCEPTION 'F2 policy time school rejected' USING ERRCODE = '28000';
    END IF;
    RETURN (
        SELECT NULLIF(unit.payload ->> 'effective_at', '')::timestamptz
        FROM public.school_fact_change_lineage AS lineage
        JOIN public.master_data_import_units AS unit
          ON unit.school_id = lineage.school_id AND unit.id = lineage.unit_id
        WHERE lineage.school_id = p_school_id
          AND lineage.fact_kind = 'policy'
          AND lineage.target_id = p_target_id
          AND lineage.operation = 'upsert'
          AND lineage.target_version = (
              SELECT min(initial.target_version)
              FROM public.school_fact_change_lineage AS initial
              WHERE initial.school_id = p_school_id
                AND initial.fact_kind = 'policy'
                AND initial.target_id = p_target_id
                AND initial.operation = 'upsert'
          )
    );
END
$policy_time$;
"""


def _create_policy_time_reader() -> None:
    """仅投影制度正式来源中的生效时间，不向 Reader 开放原始载荷。"""
    op.execute("GRANT CREATE ON SCHEMA app TO campus_ai_context_definer")
    op.execute("SET LOCAL ROLE campus_ai_context_definer")
    op.execute(sa.text(_POLICY_TIME_DDL))
    op.execute(
        f"REVOKE ALL ON FUNCTION {_POLICY_TIME_SIGNATURE} FROM PUBLIC, "
        "campus_ai_runtime, campus_ai_school_fact_writer, campus_ai_governance_writer, "
        "campus_ai_migration, campus_ai_db_owner, campus_ai_identity_definer, "
        "campus_ai_school_fact_reader"
    )
    op.execute(
        f"GRANT EXECUTE ON FUNCTION {_POLICY_TIME_SIGNATURE} TO campus_ai_school_fact_reader"
    )
    op.execute("SET LOCAL ROLE campus_ai_db_owner")
    op.execute("REVOKE CREATE ON SCHEMA app FROM campus_ai_context_definer")


def upgrade() -> None:
    """修正 Reader 姓名策略和制度时点投影，保留锚点及事实写入边界。"""
    # The legacy PUBLIC policy contains a scalar context subquery (InitPlan).
    # A separate permissive Reader anchor policy does not prevent its evaluation.
    # Keep established context actors eligible, but exclude the independent
    # Reader entirely. This grants no table/function privileges to any role.
    op.execute(
        "ALTER POLICY tenant_select ON public.school_person_names "
        "TO campus_ai_runtime, campus_ai_db_owner, campus_ai_migration, "
        "campus_ai_context_definer, campus_ai_school_fact_writer"
    )

    _create_policy_time_reader()


def downgrade() -> None:
    """禁止恢复会使独立 Reader 失效的 PUBLIC 上下文策略。"""
    raise RuntimeError(
        "F2 person Reader separation cannot be downgraded; restore the matching verified backup."
    )
