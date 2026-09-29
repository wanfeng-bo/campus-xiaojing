"""Readable correction targets within the existing selected-child projection."""
from alembic import op

revision = '20260914_38'
down_revision = '20260914_37'
branch_labels = None
depends_on = None

LABEL_SQL = r"""
CREATE OR REPLACE FUNCTION app.school_fact_family_teaching_targets(p_school uuid,p_adult uuid,p_relation uuid,p_version bigint)
RETURNS TABLE(field_name text,label text,fact_kind text,fact_source_key text,target_version bigint,student_id uuid)
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog AS $f$
DECLARE child uuid; projection jsonb;
BEGIN
 IF SESSION_USER<>'campus_ai_school_fact_writer' OR p_school IS DISTINCT FROM app.require_school_database_anchor()
 OR NOT app_private.family_relationship_is_authoritative(p_school,p_adult,p_relation,p_version,statement_timestamp()) THEN
 RAISE EXCEPTION 'teaching correction denied' USING ERRCODE='28000'; END IF;
 SELECT r.student_id INTO child FROM public.guardian_relationships r WHERE r.school_id=p_school AND r.id=p_relation
 AND r.guardian_person_id=p_adult AND r.relationship_kind='verified_guardian';
 IF child IS NULL THEN RAISE EXCEPTION 'verified guardian required' USING ERRCODE='28000'; END IF;
 -- Names come from exactly the same authorized child closure as the parent query.
 -- No contacts, whole-school enumeration, private person fields, or new target kinds.
 projection:=app_private.f5_child_projection(p_school,child,statement_timestamp(),NULL,NULL,false);
 RETURN QUERY WITH records AS MATERIALIZED (
  SELECT r->>'fact_kind' AS kind,r->>'source_key' AS source_key,
   s->'content' AS content FROM jsonb_array_elements(projection->'records') r
  CROSS JOIN LATERAL jsonb_array_elements(r->'segments') s
 ), own AS MATERIALIZED (
  SELECT k.kind,k.source_key,f.version,v.payload
  FROM app_private.f5_child_keys(p_school,child,statement_timestamp()) k
  JOIN public.school_education_facts f ON f.school_id=p_school AND f.kind=k.kind AND f.source_key=k.source_key
  JOIN public.school_education_versions v ON v.school_id=p_school AND v.fact_id=f.id AND v.superseded_at IS NULL
  WHERE v.valid_from<=statement_timestamp() AND (v.valid_to IS NULL OR statement_timestamp()<v.valid_to)
 ), names AS (
  SELECT own.*,
   COALESCE(offer.content->>'display_name',own.payload->>'display_name','未提供开课名称') AS offering_name,
   COALESCE(course.content->>'display_name','未提供课程名称') AS course_name,
   COALESCE(subject.content->>'display_name','未提供学科名称') AS subject_name,
   COALESCE(teacher.value->>'name','未提供教师姓名') AS teacher_name,
   COALESCE(grp.value->>'name','未提供群体名称') AS group_name
  FROM own
  LEFT JOIN records offer ON offer.kind='teaching_offering' AND offer.source_key=own.payload->>'offering_source_key'
  LEFT JOIN records course ON course.kind='teaching_course' AND course.source_key=COALESCE(own.payload->>'course_source_key',offer.content->>'course_source_key')
  LEFT JOIN records subject ON subject.kind='teaching_subject' AND subject.source_key=own.payload->>'subject_source_key'
  LEFT JOIN LATERAL jsonb_array_elements(projection->'related_teachers') teacher(value)
   ON teacher.value->>'assignment_source_key'=own.payload->>'assignment_source_key'
  LEFT JOIN LATERAL jsonb_array_elements(projection->'related_groups') grp(value)
   ON grp.value->>'group_source_key'=own.payload->>'group_source_key'
 ) SELECT fields.name,
  CASE names.kind
   WHEN 'teaching_assignment' THEN concat_ws(' · ',names.course_name,names.offering_name,names.teacher_name,
    CASE names.payload->>'teaching_role' WHEN 'lead' THEN '主讲任教' WHEN 'co_teacher' THEN '共同任教' ELSE '任教职责待核' END)
   WHEN 'teaching_offering' THEN concat_ws(' · ',names.course_name,names.offering_name,'开课')
   WHEN 'offering_member_source' THEN concat_ws(' · ',names.course_name,names.offering_name,'名单来源')
   WHEN 'course_subject' THEN concat_ws(' · ',names.course_name,names.subject_name,'课程学科')
   WHEN 'teaching_group_member' THEN concat_ws(' · ',names.group_name,'本孩子成员关系')
   WHEN 'teaching_subject' THEN concat_ws(' · ',names.payload->>'display_name','学科')
   WHEN 'teaching_course' THEN concat_ws(' · ',names.payload->>'display_name','课程')
  END,names.kind,names.source_key,names.version,child
 FROM names CROSS JOIN LATERAL unnest(CASE names.kind
 WHEN 'teaching_subject' THEN ARRAY['display_name'] WHEN 'teaching_course' THEN ARRAY['display_name']
 WHEN 'course_subject' THEN ARRAY['subject_source_key']
 WHEN 'teaching_offering' THEN ARRAY['course_source_key','offering_status']
 WHEN 'offering_member_source' THEN ARRAY['source_kind','class_source_key','group_source_key']
 WHEN 'teaching_assignment' THEN ARRAY['assignment_source_key','teaching_role','subject_source_keys']
 WHEN 'teaching_group_member' THEN ARRAY['group_source_key','valid_to']
 ELSE '{}'::text[] END) fields(name);
END; $f$;
"""


def upgrade():
    op.execute('SET LOCAL ROLE campus_ai_db_owner')
    op.execute('GRANT CREATE ON SCHEMA app TO campus_ai_context_definer')
    op.execute('SET LOCAL ROLE campus_ai_context_definer')
    op.execute(LABEL_SQL)
    op.execute('SET LOCAL ROLE campus_ai_db_owner')
    op.execute('REVOKE CREATE ON SCHEMA app FROM campus_ai_context_definer')


def downgrade():
    raise RuntimeError('Use a compatible guarded artifact; do not downgrade formal facts')
