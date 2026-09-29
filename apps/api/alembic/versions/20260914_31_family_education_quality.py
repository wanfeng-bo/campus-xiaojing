# ruff: noqa: E501
"""Own-child education quality through the existing finite Guardian projection.

Revision ID: 20260913_31
Revises: 20260913_30
"""

from alembic import op

revision = "20260913_31"
down_revision = "20260913_30"
branch_labels = None
depends_on = None

FAMILY_QUALITY_SQL = r"""
CREATE FUNCTION app.school_fact_family_education(
 p_school uuid,p_adult uuid,p_relation uuid,p_version bigint,p_factual_at timestamptz,p_fields text[])
RETURNS jsonb LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog AS $f$
DECLARE result jsonb; qualities jsonb; at_time timestamptz:=COALESCE(p_factual_at,statement_timestamp());
BEGIN
 -- This call retains session role, school anchor, current Guardian, selected child,
 -- requested time and allowed-field checks, including the v30 SSR grade evidence.
 result:=app.school_fact_family_education_pre_v31(p_school,p_adult,p_relation,p_version,p_factual_at,p_fields);
 WITH selected AS MATERIALIZED (
  SELECT r->>'fact_kind' kind,r->>'source_key' source_key
  FROM jsonb_array_elements(result->'records') r
  WHERE EXISTS(SELECT 1 FROM jsonb_array_elements(r->'segments') s
   WHERE (s->>'effective_at')::timestamptz<=at_time
   AND (s->>'valid_to' IS NULL OR at_time<(s->>'valid_to')::timestamptz))
 ), mapped(target_kind,field) AS (VALUES
  ('student_class_membership','class'),('class_placement','grade'),
  ('student_grade_placement','grade'),('class_teacher_assignment','teachers'),
  ('class_location','classroom'),('student_class_position','positions'),
  ('class_position_definition','positions'),('student_cohort_membership','cohorts')),
 eligible AS (
  SELECT q.source_key,q.version,q.valid_from,q.valid_to,q.revoked_at,q.field_name,
   q.academic_year,q.target_source_key,q.education_position_id,q.purpose,q.contact_kind,q.quality_kind,q.negative_code,
   q.coverage_status,q.dispute_status,q.source_status,q.verified_at,q.last_success_at,q.review_due_at,m.field,e.kind education_target_kind,e.source_key education_target_source_key,
   l.created_at published_at,l.batch_id,src.source_kind,src.source_ref
  FROM public.school_fact_qualities q
  JOIN mapped m ON m.target_kind=q.target_fact_kind AND m.field=ANY(p_fields)
  JOIN public.school_education_facts e ON e.school_id=q.school_id AND e.id=q.education_target_id
  JOIN selected own ON own.kind=e.kind AND own.source_key=e.source_key
  JOIN public.school_fact_change_lineage l ON l.school_id=q.school_id AND l.fact_kind='fact_quality'
   AND l.fact_source_key=q.source_key AND l.target_id=q.id AND l.target_version=q.version
  JOIN public.master_data_import_batches b ON b.school_id=l.school_id AND b.id=l.batch_id AND b.status='applied'
  JOIN public.school_fact_sources src ON src.school_id=q.school_id AND src.id=q.source_id
  WHERE q.school_id=p_school AND q.owner_kind='education'
   AND q.quality_kind IN ('negative','coverage','dispute','source','review','correction')
   AND q.valid_from<=at_time AND (q.valid_to IS NULL OR at_time<q.valid_to)
   AND (q.revoked_at IS NULL OR at_time<q.revoked_at)
   AND q.verified_at IS NOT NULL AND q.verified_at<=at_time
   AND (q.field_name IS NULL OR q.field_name=ANY(app_private.f4_allowed_fields(q.target_fact_kind)))
   AND (q.target_source_key IS NULL OR EXISTS(SELECT 1 FROM selected s
        WHERE s.kind=q.target_fact_kind AND s.source_key=q.target_source_key))
   AND (q.replacement_source_key IS NULL OR EXISTS(SELECT 1 FROM selected s
        WHERE s.kind=q.target_fact_kind AND s.source_key=q.replacement_source_key))
   AND (q.education_position_id IS NULL OR EXISTS(
    SELECT 1 FROM public.school_education_facts pos
    JOIN public.school_education_versions pv ON pv.school_id=pos.school_id AND pv.fact_id=pos.id
    JOIN public.school_education_references pr ON pr.school_id=pv.school_id AND pr.version_id=pv.id
     AND pr.field_name='class_source_key'
    JOIN public.school_education_facts c ON c.school_id=pr.school_id AND c.id=pr.education_id
    JOIN selected cs ON cs.kind='education_class' AND cs.source_key=c.source_key
    JOIN public.master_data_import_batches pb ON pb.school_id=pv.school_id AND pb.id=pv.import_batch_id AND pb.status='applied'
    WHERE pos.school_id=p_school AND pos.id=q.education_position_id AND pos.kind='class_position_definition'
     AND pv.superseded_at IS NULL AND pv.valid_from<=at_time AND (pv.valid_to IS NULL OR at_time<pv.valid_to)))
 ) SELECT COALESCE(jsonb_agg(jsonb_build_object(
  'field',field,'field_name',field_name,
  'scope',CASE WHEN target_source_key IS NOT NULL OR education_position_id IS NOT NULL OR purpose IS NOT NULL OR contact_kind IS NOT NULL THEN 'item' ELSE 'field' END,
  'academic_year',academic_year,'education_target_kind',education_target_kind,
  'education_target_source_key',education_target_source_key,
  'quality_kind',quality_kind,'negative_code',negative_code,'coverage_status',coverage_status,
  'dispute_status',dispute_status,'source_status',source_status,'verified_at',verified_at,
  'last_success_at',last_success_at,'review_due_at',review_due_at,
  'effective_at',valid_from,'valid_to',LEAST(valid_to,revoked_at),'version',version,
  'source',jsonb_build_object('source_kind',source_kind,'source_ref',source_ref,
   'batch_id',batch_id,'publication_reference','publication:'||batch_id::text,'published_at',published_at)
 ) ORDER BY source_key),'[]'::jsonb) INTO qualities FROM eligible;
 IF jsonb_array_length(qualities)>100 THEN RAISE EXCEPTION 'family quality set exceeds finite bound' USING ERRCODE='54000'; END IF;
 RETURN result||jsonb_build_object('quality',qualities);
END;
$f$;
REVOKE ALL ON FUNCTION app.school_fact_family_education(uuid,uuid,uuid,bigint,timestamptz,text[])
 FROM PUBLIC,campus_ai_runtime,campus_ai_school_fact_writer,campus_ai_migration,campus_ai_db_owner;
GRANT EXECUTE ON FUNCTION app.school_fact_family_education(uuid,uuid,uuid,bigint,timestamptz,text[])
 TO campus_ai_school_fact_reader;
"""


def upgrade():
    op.execute("SET LOCAL ROLE campus_ai_db_owner")
    op.execute("GRANT CREATE ON SCHEMA app TO campus_ai_context_definer")
    op.execute("SET LOCAL ROLE campus_ai_context_definer")
    op.execute(
        "ALTER FUNCTION app.school_fact_family_education(uuid,uuid,uuid,bigint,timestamptz,text[]) RENAME TO school_fact_family_education_pre_v31"
    )
    op.execute(
        "REVOKE ALL ON FUNCTION app.school_fact_family_education_pre_v31(uuid,uuid,uuid,bigint,timestamptz,text[]) FROM PUBLIC,campus_ai_school_fact_reader"
    )
    op.execute(FAMILY_QUALITY_SQL)
    op.execute("SET LOCAL ROLE campus_ai_db_owner")
    op.execute("REVOKE CREATE ON SCHEMA app FROM campus_ai_context_definer")


def downgrade():
    raise RuntimeError("Family quality downgrade requires an explicit compatibility plan")
