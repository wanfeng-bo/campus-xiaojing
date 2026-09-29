# ruff: noqa: E501
"""Selected-child grade arrangement depends on authoritative school history.

Revision ID: 20260913_30
Revises: 20260913_29
"""

from alembic import op

revision = "20260913_30"
down_revision = "20260913_29"
branch_labels = None
depends_on = None

FAMILY_GRADE_SQL = r"""
CREATE FUNCTION app.school_fact_family_education(
 p_school uuid,p_adult uuid,p_relation uuid,p_version bigint,p_factual_at timestamptz,p_fields text[])
RETURNS jsonb LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog AS $f$
DECLARE result jsonb; at_time timestamptz:=COALESCE(p_factual_at,statement_timestamp());
 child uuid; keys text[]; source_key text; axis jsonb; segments jsonb:='[]'::jsonb;
 active jsonb; total bigint; suspended_until timestamptz;
BEGIN
 -- Retain the original Reader, current selected-child and temporal checks first.
 result:=app.school_fact_family_education_pre_v30(p_school,p_adult,p_relation,p_version,p_factual_at,p_fields);
 IF NOT 'grade'=ANY(p_fields) THEN RETURN result; END IF;
 child:=(result->>'student_id')::uuid;
 SELECT array_agg(DISTINCT r.source_key) INTO keys
 FROM public.student_school_relationships r
 WHERE r.school_id=p_school AND r.student_id=child AND r.superseded_at IS NULL
 AND r.source_key IS NOT NULL;
 IF cardinality(keys)>100 THEN RAISE EXCEPTION 'grade dependency limit' USING ERRCODE='54000'; END IF;
 FOREACH source_key IN ARRAY COALESCE(keys,'{}'::text[]) LOOP
  axis:=app.school_fact_f4_ssr_timeline(p_school,source_key,ARRAY['enrollment_status','valid_to']);
  IF axis->>'school_fact_version' IS DISTINCT FROM result->>'school_fact_version' THEN
   RAISE EXCEPTION 'grade dependency changed' USING ERRCODE='55000'; END IF;
  segments:=segments||(axis->'segments');
 END LOOP;
 SELECT count(*),jsonb_agg(s)->0 INTO total,active FROM jsonb_array_elements(segments) s
 WHERE (s->>'effective_at')::timestamptz<=at_time
 AND (s->>'valid_to' IS NULL OR at_time<(s->>'valid_to')::timestamptz);
 SELECT max((s->>'valid_to')::timestamptz) INTO suspended_until FROM jsonb_array_elements(segments) s
 WHERE s->'content'->>'enrollment_status'='suspended' AND (s->>'valid_to')::timestamptz<=at_time;
 RETURN result||jsonb_build_object('grade_arrangement',jsonb_build_object(
 'enrollment_status',CASE WHEN total=1 THEN active->'content'->>'enrollment_status' ELSE 'unknown' END,
 'effective_at',CASE WHEN total=1 THEN active->>'effective_at' END,
 'last_suspended_until',suspended_until));
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
        "ALTER FUNCTION app.school_fact_family_education(uuid,uuid,uuid,bigint,timestamptz,text[]) RENAME TO school_fact_family_education_pre_v30"
    )
    op.execute(
        "REVOKE ALL ON FUNCTION app.school_fact_family_education_pre_v30(uuid,uuid,uuid,bigint,timestamptz,text[]) FROM PUBLIC,campus_ai_school_fact_reader"
    )
    op.execute(FAMILY_GRADE_SQL)
    op.execute("SET LOCAL ROLE campus_ai_db_owner")
    op.execute("REVOKE CREATE ON SCHEMA app FROM campus_ai_context_definer")


def downgrade():
    raise RuntimeError("Grade arrangement downgrade requires an explicit compatibility plan")
