"""Narrow exact Reader contracts for F7 package and education grade groups.

The function is deliberately SECURITY INVOKER: the school-fact Reader's
existing anchored RLS still applies. Only the published-batch status columns
are added to that role, so a prepared input cannot appear as a formal fact.
"""

import hashlib
import runpy
from pathlib import Path

from sqlalchemy import text

from alembic import op

revision = "20260924_54"
down_revision = "20260924_53"
branch_labels = None
depends_on = None


QUALITY_KIND_SQL = r"""
CREATE FUNCTION app.school_fact_f7_package_quality_kind(
 p_school uuid, p_assignment uuid, p_scope uuid, p_key text
) RETURNS text LANGUAGE plpgsql STABLE SECURITY INVOKER
SET search_path=pg_catalog AS $f$
DECLARE
 checked_at timestamptz := statement_timestamp();
 result text;
BEGIN
 IF SESSION_USER <> 'campus_ai_school_fact_reader'
    OR p_school IS DISTINCT FROM app.require_school_database_anchor() THEN
   RAISE EXCEPTION 'Exact quality classification denied' USING ERRCODE='28000';
 END IF;
 IF p_assignment IS NULL OR p_scope IS NULL OR p_key IS NULL
    OR btrim(p_key)='' OR length(p_key)>255 OR p_key<>btrim(p_key)
    OR p_key LIKE '%*%' OR p_key LIKE '%?%' THEN
   RAISE EXCEPTION 'Invalid exact quality coordinate' USING ERRCODE='22023';
 END IF;
 IF NOT EXISTS (
   SELECT 1 FROM public.staff_assignments a
   WHERE a.school_id=p_school AND a.id=p_assignment
     AND a.status='confirmed' AND a.valid_from<=checked_at
     AND (a.valid_to IS NULL OR checked_at<a.valid_to)
     AND (a.revoked_at IS NULL OR checked_at<a.revoked_at)
 ) THEN
   RAISE EXCEPTION 'Exact quality classification denied' USING ERRCODE='28000';
 END IF;
 IF NOT EXISTS (
   SELECT 1 FROM public.governance_grants g
   JOIN public.governance_duties d ON d.school_id=g.school_id
     AND d.id=g.duty_id AND d.duty_code='f2_fact_quality_maintainer'
   JOIN LATERAL (
     SELECT v.* FROM public.governance_grant_versions v
     WHERE v.school_id=g.school_id AND v.grant_id=g.id
       AND v.effective_from<=checked_at
     ORDER BY (v.transition_kind IN ('end','revoke')) DESC,
       v.effective_from DESC,v.version DESC LIMIT 1
   ) gv ON true
   JOIN public.governance_duty_versions dv ON dv.school_id=d.school_id
     AND dv.duty_id=d.id AND dv.version=gv.duty_version
     AND dv.is_active AND dv.effective_from<=checked_at
   JOIN public.governance_scopes s ON s.school_id=g.school_id
     AND s.id=gv.scope_id
   JOIN public.governance_scope_versions sv ON sv.school_id=s.school_id
     AND sv.scope_id=s.id AND sv.version=gv.scope_version
     AND sv.is_active AND sv.effective_from<=checked_at
   WHERE g.school_id=p_school AND g.assignment_id=p_assignment
     AND gv.scope_id=p_scope
     AND gv.transition_kind IN ('create','update')
     AND gv.valid_from<=checked_at
     AND (gv.planned_valid_to IS NULL OR checked_at<gv.planned_valid_to)
     AND gv.formal_ended_at IS NULL AND gv.revoked_at IS NULL
     AND sv.schema_owner='school_facts.f2'
     AND sv.schema_name='exact_fact_set' AND sv.schema_version=1
     AND sv.reference_document->>'school_id'=p_school::text
     AND sv.reference_document->>'fact_kind'='fact_quality'
     AND jsonb_typeof(sv.reference_document->'object_keys')='array'
     AND (sv.reference_document->'object_keys') ? p_key
 ) THEN
   RAISE EXCEPTION 'Exact quality classification denied' USING ERRCODE='28000';
 END IF;
 SELECT q.target_fact_kind INTO result
 FROM public.school_fact_qualities q
 JOIN public.master_data_import_batches b ON b.school_id=q.school_id
   AND b.id=q.import_batch_id AND b.mode='canonical_atomic_changes'
   AND b.status='applied'
 WHERE q.school_id=p_school AND q.source_key=p_key;
 IF result IS NULL THEN
   RAISE EXCEPTION 'Exact quality classification unavailable' USING ERRCODE='28000';
 END IF;
 RETURN result;
END;
$f$;
"""


_F4_TARGETS_SHA256 = "8942fe2c2e5bf705ea6165db2e798a5522b0fba8e2dac06c9764f82095d6b190"


def _f4_grade_group_filter_sql() -> str:
    original = runpy.run_path(str(
        Path(__file__).with_name("20260913_29_school_education_read.py")
    ))["STAFF_SQL"]
    start = original.index("CREATE FUNCTION app.school_fact_f4_targets(")
    end = original.index("$f$;", start) + len("$f$;")
    function = original[start:end]
    if hashlib.sha256(function.encode()).hexdigest() != _F4_TARGETS_SHA256:
        raise RuntimeError("Retained exact education Reader changed")
    patches = (
        (
            "CREATE FUNCTION app.school_fact_f4_targets(",
            "CREATE OR REPLACE FUNCTION app.school_fact_f4_targets(",
        ),
        (
            "OR NOT fl.key=ANY(p_fields) OR jsonb_typeof(fl.value) NOT IN ('string','array')",
            "OR NOT fl.key=ANY(p_fields) OR "
            "(fl.key='academic_year' AND (p_kind<>'education_grade_group' OR "
            "jsonb_typeof(fl.value) NOT IN ('number','array'))) OR "
            "(fl.key<>'academic_year' AND jsonb_typeof(fl.value) NOT IN ('string','array'))",
        ),
        (
            "'location_source_key','grade_source_key'])",
            "'location_source_key','grade_source_key']) "
            "AND NOT (p_kind='education_grade_group' AND fl.key='academic_year')",
        ),
        (
            "WHERE jsonb_typeof(val)<>'string'\n OR (fl.key='display_name'",
            "WHERE (fl.key='academic_year' AND "
            "(jsonb_typeof(val)<>'number' OR val::text !~ '^[0-9]{4}$' "
            "OR val::text NOT BETWEEN '1900' AND '9998'))\n"
            "  OR (fl.key<>'academic_year' AND (jsonb_typeof(val)<>'string'\n"
            "  OR (fl.key='display_name'",
        ),
        (
            "OR (val#>>'{}') !~ '^[a-z0-9][a-z0-9._:-]{2,254}$')))\n",
            "OR (val#>>'{}') !~ '^[a-z0-9][a-z0-9._:-]{2,254}$')))))\n",
        ),
    )
    for before, after in patches:
        if function.count(before) != 1:
            raise RuntimeError("Retained exact education Reader guard changed")
        function = function.replace(before, after, 1)
    return function


def upgrade() -> None:
    op.execute("SET LOCAL ROLE campus_ai_db_owner")
    # Existing anchor-guard RLS still applies; expose only publication status.
    op.execute(
        "GRANT SELECT(id,school_id,mode,status) ON public.master_data_import_batches "
        "TO campus_ai_school_fact_reader"
    )
    op.execute(
        "CREATE POLICY f7_package_quality_batch_read ON public.master_data_import_batches "
        "AS PERMISSIVE FOR SELECT TO campus_ai_school_fact_reader "
        "USING(school_id=app.require_school_database_anchor())"
    )
    op.execute(QUALITY_KIND_SQL)
    temporary_create = not op.get_bind().execute(text(
        "SELECT has_schema_privilege('campus_ai_context_definer','app','CREATE')"
    )).scalar_one()
    if temporary_create:
        op.execute("GRANT CREATE ON SCHEMA app TO campus_ai_context_definer")
    op.execute("SET LOCAL ROLE campus_ai_context_definer")
    op.execute(_f4_grade_group_filter_sql())
    op.execute("SET LOCAL ROLE campus_ai_db_owner")
    if temporary_create:
        op.execute("REVOKE CREATE ON SCHEMA app FROM campus_ai_context_definer")
    op.execute(
        "REVOKE ALL ON FUNCTION app.school_fact_f7_package_quality_kind"
        "(uuid,uuid,uuid,text) FROM PUBLIC"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION app.school_fact_f7_package_quality_kind"
        "(uuid,uuid,uuid,text) TO campus_ai_school_fact_reader"
    )


def downgrade() -> None:
    raise RuntimeError("Published F7 exact quality classification is append-only")
