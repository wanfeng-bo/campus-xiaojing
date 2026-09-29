"""Permit exact authorized student-number filtering through the existing F3 Reader."""

from alembic import op

revision = "20260922_45"
down_revision = "20260922_44"
branch_labels = None
depends_on = None

SQL = r"""
CREATE OR REPLACE FUNCTION app.school_fact_f3_staff_candidates(
 p_school uuid,p_kind text,p_sources text[],p_factual_at timestamptz,
 p_fields text[],p_match_field text,p_match_value text,p_limit integer
) RETURNS TABLE(source_key text,label text,payload jsonb)
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog AS $f$
DECLARE candidate text; target jsonb; emitted integer:=0;
BEGIN
 IF SESSION_USER<>'campus_ai_school_fact_reader'
    OR p_school IS DISTINCT FROM app.require_school_database_anchor() THEN
   RAISE EXCEPTION 'F3 Reader denied' USING ERRCODE='28000';
 END IF;
 IF p_kind IS NULL OR p_fields IS NULL OR cardinality(p_fields)=0
    OR array_position(p_fields,NULL) IS NOT NULL OR NOT p_match_field=ANY(p_fields)
    OR p_match_field IS NULL OR p_limit IS NULL
    OR p_sources IS NULL OR cardinality(p_sources)>100
    OR array_position(p_sources,NULL) IS NOT NULL
    OR p_factual_at IS NULL OR p_match_value IS NULL OR btrim(p_match_value)=''
    OR length(p_match_value)>1000 OR p_limit<1 OR p_limit>21
    OR NOT (
      (p_kind='person' AND p_match_field='display_name')
      OR (p_kind='student' AND p_match_field='person_source_key')
      OR (p_kind='student_school_relationship' AND p_match_field IN
        ('student_source_key','primary_campus_source_key',
         'administrative_campus_source_key','student_number'))
      OR (p_kind='student_contact' AND p_match_field IN
        ('student_source_key','contact_person_source_key'))
      OR (p_kind='guardian_relationship' AND p_match_field IN
        ('guardian_person_source_key','student_source_key',
         'review_of_relationship_source_key'))
      OR (p_kind='person_region' AND p_match_field='person_source_key')
      OR (p_kind='login_mapping' AND p_match_field='person_source_key')
      OR (p_kind='assignment' AND p_match_field IN
        ('person_source_key','organization_source_key'))
    ) THEN RETURN;
 END IF;
 FOREACH candidate IN ARRAY p_sources LOOP
   target:=app.school_fact_f3_staff_target(
     p_school,p_kind,candidate,false,p_factual_at,p_fields);
   IF target->>'status'='available'
      AND target->'content'->>p_match_field=p_match_value THEN
     source_key:=candidate;
     label:=target->>'label';
     payload:=target;
     RETURN NEXT;
     emitted:=emitted+1;
     IF emitted>=p_limit THEN RETURN; END IF;
   END IF;
 END LOOP;
END;
$f$;
"""


def upgrade():
    # Replacing the same signature retains its existing owner and EXECUTE ACL.
    op.execute("SET LOCAL ROLE campus_ai_db_owner")
    op.execute("GRANT CREATE ON SCHEMA app TO campus_ai_context_definer")
    op.execute("SET LOCAL ROLE campus_ai_context_definer")
    op.execute(SQL)
    op.execute("SET LOCAL ROLE campus_ai_db_owner")
    op.execute("REVOKE CREATE ON SCHEMA app FROM campus_ai_context_definer")


def downgrade():
    raise RuntimeError("Use the reviewed same-schema compatible artifact; retain formal history")
