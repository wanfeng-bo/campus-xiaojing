# ruff: noqa: E501
"""Follow an already-published member's stable SSR source through current-only RLS."""
from alembic import op

revision = "20260914_40"
down_revision = "20260914_39"
branch_labels = None
depends_on = None

CURRENT_RELATIONSHIP_SQL = r"""
CREATE OR REPLACE FUNCTION app_private.f5_child_keys(p_school uuid,p_student uuid,p_at timestamptz,p_until timestamptz DEFAULT NULL,p_subject text DEFAULT NULL,p_course text DEFAULT NULL)
RETURNS TABLE(kind text,source_key text)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog AS $f$
WITH RECURSIVE active AS NOT MATERIALIZED (
 SELECT f.id,f.kind,f.source_key,v.id AS version_id,v.payload
 FROM public.school_education_facts f JOIN public.school_education_versions v
 ON v.school_id=f.school_id AND v.fact_id=f.id
 JOIN public.master_data_import_batches b ON b.school_id=v.school_id AND b.id=v.import_batch_id
 WHERE f.school_id=p_school AND v.superseded_at IS NULL
 AND ((p_until IS NULL AND v.valid_from<=p_at) OR (p_until IS NOT NULL AND v.valid_from<p_until))
 AND (v.valid_to IS NULL OR p_at<v.valid_to) AND b.status='applied'
 AND (SELECT count(*) FROM public.school_fact_change_lineage l
 JOIN public.master_data_import_batches lb ON lb.school_id=l.school_id AND lb.id=l.batch_id
 WHERE l.school_id=f.school_id AND l.target_id=f.id AND l.fact_kind=f.kind
 AND l.fact_source_key=f.source_key AND l.target_version=f.version AND lb.status='applied')=1
 AND NOT EXISTS(SELECT 1 FROM public.school_fact_change_lineage l
 JOIN public.master_data_import_batches lb ON lb.school_id=l.school_id AND lb.id=l.batch_id
 WHERE l.school_id=f.school_id AND l.fact_kind=f.kind AND l.fact_source_key=f.source_key
 AND l.target_version>f.version AND lb.status='applied')
), own AS MATERIALIZED (
 SELECT DISTINCT a.id,a.kind,a.source_key,a.version_id,a.payload FROM public.school_education_references r
 JOIN active a ON a.version_id=r.version_id
 JOIN public.school_education_references sr ON sr.school_id=p_school AND sr.version_id=a.version_id
 AND sr.field_name='school_relationship_source_key'
 JOIN public.student_school_relationships enrolled ON enrolled.school_id=p_school
 AND enrolled.student_id=p_student AND enrolled.source_key=a.payload->>'school_relationship_source_key' AND enrolled.superseded_at IS NULL
 WHERE r.school_id=p_school AND r.student_id=p_student
 AND a.kind IN ('student_class_membership','teaching_group_member')
 AND enrolled.enrollment_status IN ('enrolled','suspended')
 AND ((p_until IS NULL AND enrolled.valid_from<=p_at) OR (p_until IS NOT NULL AND enrolled.valid_from<p_until))
 AND (enrolled.valid_to IS NULL OR p_at<enrolled.valid_to)
 AND EXISTS(SELECT 1 FROM public.school_fact_change_lineage l
 JOIN public.master_data_import_batches b ON b.school_id=l.school_id AND b.id=l.batch_id
 WHERE l.school_id=enrolled.school_id AND l.fact_kind='student_school_relationship'
 AND l.fact_source_key=enrolled.source_key AND l.batch_id=enrolled.import_batch_id
 AND l.target_version=enrolled.version AND b.status='applied')
), source_owners AS MATERIALIZED (
 SELECT DISTINCT ref.education_id AS id, o.kind FROM own o
 JOIN public.school_education_references ref ON ref.school_id=p_school AND ref.version_id=o.version_id
 WHERE (o.kind='student_class_membership' AND o.payload->>'membership_status'='assigned' AND ref.field_name='class_source_key')
 OR (o.kind='teaching_group_member' AND ref.field_name='group_source_key')
), source_candidates AS MATERIALIZED (
 SELECT DISTINCT a.id,a.kind,a.source_key,a.version_id,a.payload FROM source_owners o
 JOIN public.school_education_references r ON r.school_id=p_school AND r.education_id=o.id
 JOIN active a ON a.version_id=r.version_id AND a.kind='offering_member_source'
 WHERE (o.kind='student_class_membership' AND r.field_name='class_source_key' AND a.payload->>'source_kind'='administrative_class')
 OR (o.kind='teaching_group_member' AND r.field_name='group_source_key' AND a.payload->>'source_kind'='independent_group')
), offering_candidates AS MATERIALIZED (
 SELECT DISTINCT a.id,a.kind,a.source_key,a.version_id,a.payload FROM source_candidates s
 JOIN public.school_education_references r ON r.school_id=p_school AND r.version_id=s.version_id AND r.field_name='offering_source_key'
 JOIN active a ON a.id=r.education_id AND a.kind='teaching_offering'
), courses AS MATERIALIZED (
 SELECT DISTINCT a.id,a.kind,a.source_key,a.version_id,a.payload FROM offering_candidates o
 JOIN public.school_education_references r ON r.school_id=p_school AND r.version_id=o.version_id AND r.field_name='course_source_key'
 JOIN active a ON a.id=r.education_id AND a.kind='teaching_course'
 WHERE (p_course IS NULL OR a.payload->>'display_name'=p_course)
 AND (p_subject IS NULL OR EXISTS(
  SELECT 1 FROM public.school_education_references cr JOIN active cs ON cs.version_id=cr.version_id AND cs.kind='course_subject'
  JOIN public.school_education_references sr ON sr.school_id=p_school AND sr.version_id=cs.version_id AND sr.field_name='subject_source_key'
  JOIN active subject ON subject.id=sr.education_id AND subject.kind='teaching_subject'
  WHERE cr.school_id=p_school AND cr.education_id=a.id AND cr.field_name='course_source_key' AND subject.payload->>'display_name'=p_subject))
), offerings AS MATERIALIZED (
 SELECT o.* FROM offering_candidates o JOIN public.school_education_references r ON r.school_id=p_school AND r.version_id=o.version_id AND r.field_name='course_source_key'
 JOIN courses c ON c.id=r.education_id
), sources AS MATERIALIZED (
 SELECT s.* FROM source_candidates s JOIN public.school_education_references r ON r.school_id=p_school AND r.version_id=s.version_id AND r.field_name='offering_source_key'
 JOIN offerings o ON o.id=r.education_id
), subjects AS MATERIALIZED (
 SELECT DISTINCT a.id,a.kind,a.source_key,a.version_id,a.payload FROM courses c
 JOIN public.school_education_references r ON r.school_id=p_school AND r.education_id=c.id AND r.field_name='course_source_key'
 JOIN active a ON a.version_id=r.version_id AND a.kind='course_subject'
), teachers AS MATERIALIZED (
 SELECT DISTINCT a.id,a.kind,a.source_key,a.version_id,a.payload FROM offerings o
 JOIN public.school_education_references r ON r.school_id=p_school AND r.education_id=o.id AND r.field_name='offering_source_key'
 JOIN active a ON a.version_id=r.version_id AND a.kind='teaching_assignment'
 WHERE p_subject IS NULL OR a.payload->'subject_source_keys' IS NULL
 OR EXISTS(SELECT 1 FROM public.school_education_references sr JOIN active subject ON subject.id=sr.education_id
 WHERE sr.school_id=p_school AND sr.version_id=a.version_id AND sr.field_name='subject_source_keys'
 AND subject.kind='teaching_subject' AND subject.payload->>'display_name'=p_subject)
), seeds AS (
 SELECT * FROM sources UNION SELECT * FROM offerings UNION SELECT * FROM courses
 UNION SELECT * FROM subjects UNION SELECT * FROM teachers
), walk(id,kind,source_key,version_id,payload) AS (
 SELECT * FROM seeds
 UNION
 SELECT a.id,a.kind,a.source_key,a.version_id,a.payload FROM walk w
 JOIN public.school_education_references r ON r.school_id=p_school AND r.version_id=w.version_id
 JOIN active a ON a.id=r.education_id
 WHERE r.field_name IN ('subject_source_key','subject_source_keys','group_source_key','class_source_key','stage_source_keys','grade_source_keys','term_source_key','calendar_source_key')
)
SELECT DISTINCT w.kind,w.source_key FROM walk w
UNION SELECT o.kind,o.source_key FROM own o WHERE o.kind='teaching_group_member';
$f$;
REVOKE ALL ON FUNCTION app_private.f5_child_keys(uuid,uuid,timestamptz,timestamptz,text,text) FROM PUBLIC;

"""


def upgrade():
    op.execute("SET LOCAL ROLE campus_ai_db_owner")
    op.execute("GRANT CREATE ON SCHEMA app_private TO campus_ai_context_definer")
    op.execute("SET LOCAL ROLE campus_ai_context_definer")
    op.execute(CURRENT_RELATIONSHIP_SQL)
    op.execute("SET LOCAL ROLE campus_ai_db_owner")
    op.execute("REVOKE CREATE ON SCHEMA app_private FROM campus_ai_context_definer")


def downgrade():
    raise RuntimeError("Current relationship reads use append-only schema evolution")
