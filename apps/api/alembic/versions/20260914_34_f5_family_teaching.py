# ruff: noqa: E501
"""F5 selected-child teaching projection, conditional reuse and exact correction proof."""

from alembic import op

revision = "20260914_34"
down_revision = "20260914_33"
branch_labels = None
depends_on = None

BOUNDARY_GRANTS_SQL = """
GRANT SELECT(import_batch_id) ON public.student_school_relationships TO campus_ai_context_definer;
"""

FAMILY_SQL = r"""
CREATE FUNCTION app_private.f5_child_keys(p_school uuid,p_student uuid,p_at timestamptz,p_until timestamptz DEFAULT NULL,p_subject text DEFAULT NULL,p_course text DEFAULT NULL)
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
 JOIN public.student_school_relationships referenced ON referenced.school_id=p_school
 AND referenced.id=sr.relationship_id AND referenced.student_id=p_student
 JOIN public.student_school_relationships enrolled ON enrolled.school_id=p_school
 AND enrolled.student_id=p_student AND enrolled.source_key=referenced.source_key AND enrolled.superseded_at IS NULL
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

CREATE FUNCTION app_private.f5_child_projection(p_school uuid,p_student uuid,p_at timestamptz,p_subject text,p_course text,p_contacts boolean)
RETURNS jsonb LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog AS $f$
DECLARE selected jsonb; part record; records jsonb:='[]'; staff jsonb:='[]'; qualities jsonb:='[]'; groups jsonb:='[]'; teacher_ids uuid[];
BEGIN
 SELECT COALESCE(jsonb_agg(jsonb_build_object('kind',k.kind,'source_key',k.source_key)),'[]')
 INTO selected FROM app_private.f5_child_keys(p_school,p_student,p_at,NULL,p_subject,p_course) k
 WHERE k.kind<>'teaching_group_member';
 IF jsonb_array_length(selected)>300 THEN RAISE EXCEPTION 'teaching result bound exceeded' USING ERRCODE='54000'; END IF;
 FOR part IN SELECT x.kind,array_agg(x.source_key ORDER BY x.source_key) AS keys
 FROM jsonb_to_recordset(selected) x(kind text,source_key text) GROUP BY x.kind LOOP
  IF app_private.f5_allowed_fields(part.kind) IS NOT NULL THEN
   records:=records||app_private.f5_target_records(p_school,part.kind,part.keys,
    array_remove(array_remove(app_private.f5_allowed_fields(part.kind),'basis_reference'),'teaching_role_limits'),p_at,NULL);
  ELSE
   records:=records||app_private.f4_target_records(p_school,part.kind,part.keys,
    ARRAY['display_name'],p_at,NULL);
  END IF;
 END LOOP;
 -- Expose one factual instant, not the selected child's or teacher's full history.
 SELECT COALESCE(jsonb_agg((r-'prior_segments'-'latest_operation')||jsonb_build_object('segments',
  COALESCE((SELECT jsonb_agg(s-'cause_source_key'-'cause_version'-'causal_prior')
   FROM jsonb_array_elements(r->'segments') s WHERE (s->>'effective_at')::timestamptz<=p_at
   AND (s->>'valid_to' IS NULL OR p_at<(s->>'valid_to')::timestamptz)),'[]'))),'[]')
 INTO records FROM jsonb_array_elements(records) r;
 SELECT array_agg(DISTINCT r.assignment_id) INTO teacher_ids
 FROM jsonb_to_recordset(selected) k(kind text,source_key text)
 JOIN public.school_education_facts f ON f.school_id=p_school AND f.kind=k.kind AND f.source_key=k.source_key
 JOIN public.school_education_versions v ON v.school_id=f.school_id AND v.fact_id=f.id
 JOIN public.school_education_references r ON r.school_id=v.school_id AND r.version_id=v.id AND r.field_name='assignment_source_key'
 WHERE k.kind='teaching_assignment' AND v.superseded_at IS NULL AND v.valid_from<=p_at
 AND (v.valid_to IS NULL OR p_at<v.valid_to);
 WITH people AS MATERIALIZED (
 SELECT a.source_key,av.person_id,av.organization_unit_id,n.display_name,av.source_id,av.import_batch_id
 FROM public.staff_assignments a JOIN public.staff_assignment_versions av ON av.school_id=a.school_id AND av.assignment_id=a.id
 JOIN public.school_person_names n ON n.school_id=av.school_id AND n.person_id=av.person_id
 WHERE a.school_id=p_school AND a.id=ANY(COALESCE(teacher_ids,'{}'))
 AND av.status='confirmed' AND av.revoked_at IS NULL AND av.effective_from<=p_at
 AND (av.effective_to IS NULL OR p_at<av.effective_to) AND (av.planned_valid_to IS NULL OR p_at<av.planned_valid_to)
 AND n.status='confirmed' AND n.valid_from<=p_at AND (n.valid_to IS NULL OR p_at<n.valid_to)
 AND app_private.school_fact_identity_root_is_effective_at(p_school,'person',av.person_id,p_at)
 AND app_private.school_fact_identity_root_is_effective_at(p_school,'organization',av.organization_unit_id,p_at)
 AND EXISTS(SELECT 1 FROM public.school_fact_change_lineage l
 JOIN public.master_data_import_batches b ON b.school_id=l.school_id AND b.id=l.batch_id
 WHERE l.school_id=a.school_id AND l.fact_kind='assignment' AND l.fact_source_key=a.source_key
 AND l.target_id=a.id AND l.target_version=av.target_version AND b.status='applied')
 ), channels AS MATERIALIZED (
 SELECT p.source_key AS teacher,c.owner_kind,c.contact_kind,c.contact_value,c.service_hours,ov.display_name AS owner
 FROM people p JOIN public.official_contact_points c ON c.school_id=p_school AND c.owner_kind='organization' AND c.organization_unit_id=p.organization_unit_id
 JOIN public.organization_unit_versions ov ON ov.school_id=c.school_id AND ov.organization_unit_id=c.organization_unit_id
 WHERE p_contacts AND c.visibility_scope='public' AND c.lifecycle_status='active'
 AND c.valid_from<=statement_timestamp() AND (c.valid_to IS NULL OR statement_timestamp()<c.valid_to)
 AND ov.status='confirmed' AND ov.valid_from<=statement_timestamp() AND (ov.valid_to IS NULL OR statement_timestamp()<ov.valid_to)
 AND app_private.school_fact_identity_root_is_effective_at(p_school,'organization',c.organization_unit_id,statement_timestamp())
 AND EXISTS(SELECT 1 FROM public.school_fact_change_lineage l
 JOIN public.master_data_import_batches b ON b.school_id=l.school_id AND b.id=l.batch_id
 WHERE l.school_id=c.school_id AND l.fact_kind='official_contact' AND l.target_id=c.id AND l.target_version=c.version
 AND l.operation IN ('upsert','reactivate') AND b.status='applied')
 AND NOT EXISTS(SELECT 1 FROM public.school_fact_change_lineage l
 JOIN public.master_data_import_batches b ON b.school_id=l.school_id AND b.id=l.batch_id
 JOIN public.master_data_import_units u ON u.school_id=l.school_id AND u.id=l.unit_id
 WHERE l.school_id=c.school_id AND l.fact_kind='official_contact' AND l.target_id=c.id AND l.operation IN ('end','revoke')
 AND b.status='applied' AND (u.payload->>'effective_at')::timestamptz<=statement_timestamp())
 ) SELECT COALESCE(jsonb_agg(jsonb_build_object('assignment_source_key',p.source_key,'name',p.display_name,
 'official_contacts',COALESCE((SELECT jsonb_agg(to_jsonb(c)-'teacher') FROM channels c WHERE c.teacher=p.source_key),'[]'))),'[]') INTO staff FROM people p;
 SELECT COALESCE(jsonb_agg(DISTINCT jsonb_build_object('group_source_key',f.source_key,'name',ov.display_name)),'[]') INTO groups
 FROM jsonb_to_recordset(selected) k(kind text,source_key text)
 JOIN public.school_education_facts f ON f.school_id=p_school AND f.kind='teaching_group' AND k.kind=f.kind AND k.source_key=f.source_key
 JOIN public.school_education_versions v ON v.school_id=p_school AND v.fact_id=f.id AND v.superseded_at IS NULL
 JOIN public.school_education_references r ON r.school_id=p_school AND r.version_id=v.id AND r.field_name='organization_source_key'
 JOIN public.organization_unit_versions ov ON ov.school_id=p_school AND ov.organization_unit_id=r.organization_id
 WHERE v.valid_from<=p_at AND (v.valid_to IS NULL OR p_at<v.valid_to) AND ov.status='confirmed'
 AND ov.valid_from<=p_at AND (ov.valid_to IS NULL OR p_at<ov.valid_to)
 AND app_private.school_fact_identity_root_is_effective_at(p_school,'organization',ov.organization_unit_id,p_at);
 WITH chosen AS MATERIALIZED (SELECT * FROM jsonb_to_recordset(selected) k(kind text,source_key text)), eligible AS (
 SELECT q.source_key,q.version,q.quality_kind,q.teaching_role,q.negative_code,q.coverage_status,q.dispute_status,q.source_status,
 q.field_name,q.target_source_key,q.replacement_source_key,q.verified_at,q.review_due_at,q.valid_from,q.valid_to,q.revoked_at,e.source_key AS offering_key,l.created_at AS published_at,l.batch_id,src.source_kind,src.source_ref
 FROM chosen own JOIN public.school_education_facts e ON e.school_id=p_school AND e.kind='teaching_offering'
 AND own.kind=e.kind AND own.source_key=e.source_key
 JOIN public.school_fact_qualities q ON q.school_id=p_school AND q.education_target_id=e.id
 JOIN public.school_fact_change_lineage l ON l.school_id=q.school_id AND l.fact_kind='fact_quality'
 AND l.fact_source_key=q.source_key AND l.target_id=q.id AND l.target_version=q.version
 JOIN public.master_data_import_batches b ON b.school_id=l.school_id AND b.id=l.batch_id AND b.status='applied'
 JOIN public.school_fact_sources src ON src.school_id=q.school_id AND src.id=q.source_id
 WHERE q.owner_kind='education' AND q.target_fact_kind='teaching_assignment'
 AND q.teaching_role IN ('lead','co_teacher') AND q.verified_at<=p_at
 AND q.valid_from<=p_at AND (q.valid_to IS NULL OR p_at<q.valid_to) AND (q.revoked_at IS NULL OR p_at<q.revoked_at)
 AND (q.field_name IS NULL OR q.field_name=ANY(app_private.f5_allowed_fields('teaching_assignment')))
 AND q.purpose IS NULL AND q.contact_kind IS NULL AND q.education_position_id IS NULL
 AND (q.academic_year IS NULL OR EXISTS(
 SELECT 1 FROM public.school_education_versions ov JOIN public.school_education_references tr
 ON tr.school_id=p_school AND tr.version_id=ov.id AND tr.field_name='term_source_key'
 JOIN public.school_education_versions tv ON tv.school_id=p_school AND tv.fact_id=tr.education_id
 JOIN public.master_data_import_batches tb ON tb.school_id=p_school AND tb.id=tv.import_batch_id AND tb.status='applied'
 WHERE ov.school_id=p_school AND ov.fact_id=e.id AND ov.superseded_at IS NULL AND ov.valid_from<=p_at
 AND (ov.valid_to IS NULL OR p_at<ov.valid_to) AND tv.superseded_at IS NULL AND tv.valid_from<=p_at
 AND (tv.valid_to IS NULL OR p_at<tv.valid_to) AND tv.payload->>'academic_year'=q.academic_year::text))
 AND (q.target_source_key IS NULL OR EXISTS(SELECT 1 FROM chosen c WHERE c.kind='teaching_assignment' AND c.source_key=q.target_source_key))
 AND (q.replacement_source_key IS NULL OR EXISTS(SELECT 1 FROM chosen c WHERE c.kind='teaching_assignment' AND c.source_key=q.replacement_source_key))
 ) SELECT COALESCE(jsonb_agg(jsonb_build_object(
 'offering_source_key',offering_key,'teaching_role',teaching_role,'quality_kind',quality_kind,
 'negative_code',negative_code,'coverage_status',coverage_status,'dispute_status',dispute_status,'source_status',source_status,
 'field_name',field_name,'target_source_key',target_source_key,'replacement_source_key',replacement_source_key,
 'verified_at',verified_at,'review_due_at',review_due_at,'effective_at',valid_from,'valid_to',LEAST(valid_to,revoked_at),
 'version',version,'source',jsonb_build_object('source_kind',source_kind,'source_ref',source_ref,'batch_id',batch_id,
 'publication_reference','publication:'||batch_id::text,'published_at',published_at)) ORDER BY source_key),'[]') INTO qualities FROM eligible;
 IF jsonb_array_length(qualities)>100 THEN RAISE EXCEPTION 'family teaching quality bound exceeded' USING ERRCODE='54000'; END IF;
 RETURN jsonb_build_object('records',records,'related_teachers',staff,'as_of',p_at,'quality',qualities,'related_groups',groups,
 'enrollment_status',(SELECT r.enrollment_status FROM public.student_school_relationships r
 WHERE r.school_id=p_school AND r.student_id=p_student AND r.superseded_at IS NULL
 AND r.valid_from<=p_at AND (r.valid_to IS NULL OR p_at<r.valid_to)));
END; $f$;
REVOKE ALL ON FUNCTION app_private.f5_child_projection(uuid,uuid,timestamptz,text,text,boolean) FROM PUBLIC;

CREATE FUNCTION app_private.f5_child_boundary(p_school uuid,p_student uuid,p_at timestamptz,p_until timestamptz,p_subject text,p_course text,p_contacts boolean)
RETURNS TABLE(boundary_at timestamptz) LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog AS $f$
WITH related AS MATERIALIZED (
 SELECT f.id FROM app_private.f5_child_keys(p_school,p_student,p_at,p_until,p_subject,p_course) k
 JOIN public.school_education_facts f ON f.school_id=p_school AND f.kind=k.kind AND f.source_key=k.source_key
 UNION SELECT v.fact_id FROM public.school_education_references r
 JOIN public.school_education_versions v ON v.school_id=r.school_id AND v.id=r.version_id
 WHERE r.school_id=p_school AND r.student_id=p_student AND v.superseded_at IS NULL
), teacher_ids AS MATERIALIZED (
 SELECT DISTINCT r.assignment_id AS id FROM related f JOIN public.school_education_versions v ON v.school_id=p_school AND v.fact_id=f.id
 JOIN public.school_education_references r ON r.school_id=p_school AND r.version_id=v.id AND r.field_name='assignment_source_key'
 WHERE v.superseded_at IS NULL
), people_ids AS MATERIALIZED (
 SELECT DISTINCT v.person_id AS id FROM public.staff_assignment_versions v JOIN teacher_ids t ON t.id=v.assignment_id WHERE v.school_id=p_school
), organization_ids AS MATERIALIZED (
 SELECT DISTINCT v.organization_unit_id AS id FROM public.staff_assignment_versions v JOIN teacher_ids t ON t.id=v.assignment_id WHERE v.school_id=p_school
 UNION SELECT ref.organization_id FROM related f JOIN public.school_education_versions v ON v.school_id=p_school AND v.fact_id=f.id
 JOIN public.school_education_references ref ON ref.school_id=p_school AND ref.version_id=v.id AND ref.field_name='organization_source_key' WHERE v.superseded_at IS NULL
), values_at AS (
 SELECT v.valid_from AS t FROM public.school_education_versions v JOIN related r ON r.id=v.fact_id WHERE v.school_id=p_school AND v.superseded_at IS NULL
 UNION ALL SELECT v.valid_to FROM public.school_education_versions v JOIN related r ON r.id=v.fact_id WHERE v.school_id=p_school AND v.superseded_at IS NULL
 UNION ALL SELECT r.valid_from FROM public.student_school_relationships r WHERE r.school_id=p_school AND r.student_id=p_student AND r.superseded_at IS NULL
 UNION ALL SELECT r.valid_to FROM public.student_school_relationships r WHERE r.school_id=p_school AND r.student_id=p_student AND r.superseded_at IS NULL
 UNION ALL SELECT v.effective_from FROM public.staff_assignment_versions v JOIN teacher_ids t ON t.id=v.assignment_id WHERE v.school_id=p_school
 UNION ALL SELECT v.effective_to FROM public.staff_assignment_versions v JOIN teacher_ids t ON t.id=v.assignment_id WHERE v.school_id=p_school
 UNION ALL SELECT v.planned_valid_to FROM public.staff_assignment_versions v JOIN teacher_ids t ON t.id=v.assignment_id WHERE v.school_id=p_school
 UNION ALL SELECT v.valid_from FROM public.school_person_names v JOIN people_ids p ON p.id=v.person_id WHERE v.school_id=p_school
 UNION ALL SELECT v.valid_to FROM public.school_person_names v JOIN people_ids p ON p.id=v.person_id WHERE v.school_id=p_school
 UNION ALL SELECT v.valid_from FROM public.school_person_profile_versions v JOIN people_ids p ON p.id=v.person_id WHERE v.school_id=p_school
 UNION ALL SELECT v.valid_to FROM public.school_person_profile_versions v JOIN people_ids p ON p.id=v.person_id WHERE v.school_id=p_school
 UNION ALL SELECT v.valid_from FROM public.organization_unit_versions v JOIN organization_ids o ON o.id=v.organization_unit_id WHERE v.school_id=p_school
 UNION ALL SELECT v.valid_to FROM public.organization_unit_versions v JOIN organization_ids o ON o.id=v.organization_unit_id WHERE v.school_id=p_school
 UNION ALL SELECT c.valid_from FROM public.official_contact_points c JOIN organization_ids o ON o.id=c.organization_unit_id WHERE c.school_id=p_school AND p_contacts
 UNION ALL SELECT c.valid_to FROM public.official_contact_points c JOIN organization_ids o ON o.id=c.organization_unit_id WHERE c.school_id=p_school AND p_contacts
 UNION ALL SELECT unnest(ARRAY[q.valid_from,q.valid_to,q.revoked_at,q.verified_at,q.review_due_at])
 FROM public.school_fact_qualities q JOIN related r ON r.id=q.education_target_id
 WHERE q.school_id=p_school AND q.owner_kind='education' AND q.target_fact_kind='teaching_assignment'
 AND q.teaching_role IN ('lead','co_teacher')
 UNION ALL SELECT v.valid_from FROM public.school_versions v WHERE v.school_id=p_school
 UNION ALL SELECT v.valid_to FROM public.school_versions v WHERE v.school_id=p_school
) SELECT DISTINCT t FROM values_at WHERE t>p_at AND t<p_until;
$f$;
REVOKE ALL ON FUNCTION app_private.f5_child_boundary(uuid,uuid,timestamptz,timestamptz,text,text,boolean) FROM PUBLIC;

CREATE FUNCTION app_private.f5_child_calendars(p_school uuid,p_student uuid,p_at timestamptz)
RETURNS TABLE(id uuid) LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog AS $f$
WITH active AS NOT MATERIALIZED (
 SELECT f.id,f.kind,f.source_key,v.id AS version_id,v.payload
 FROM public.school_education_facts f JOIN public.school_education_versions v
 ON v.school_id=f.school_id AND v.fact_id=f.id
 JOIN public.master_data_import_batches b ON b.school_id=v.school_id AND b.id=v.import_batch_id
 WHERE f.school_id=p_school AND v.superseded_at IS NULL
 AND v.valid_from<=p_at
 AND (v.valid_to IS NULL OR p_at<v.valid_to) AND b.status='applied'
 AND (SELECT count(*) FROM public.school_fact_change_lineage l
 JOIN public.master_data_import_batches lb ON lb.school_id=l.school_id AND lb.id=l.batch_id
 WHERE l.school_id=f.school_id AND l.target_id=f.id AND l.fact_kind=f.kind
 AND l.fact_source_key=f.source_key AND l.target_version=f.version AND lb.status='applied')=1
 AND NOT EXISTS(SELECT 1 FROM public.school_fact_change_lineage l
 JOIN public.master_data_import_batches lb ON lb.school_id=l.school_id AND lb.id=l.batch_id
 WHERE l.school_id=f.school_id AND l.fact_kind=f.kind AND l.fact_source_key=f.source_key
 AND l.target_version>f.version AND lb.status='applied')
), personal AS MATERIALIZED (
 SELECT a.version_id FROM public.school_education_references r JOIN active a ON a.version_id=r.version_id
 WHERE r.school_id=p_school AND r.student_id=p_student AND a.kind='student_grade_placement'
), classes AS MATERIALIZED (
 SELECT cr.education_id AS id FROM public.school_education_references r JOIN active a ON a.version_id=r.version_id
 JOIN public.school_education_references cr ON cr.school_id=p_school AND cr.version_id=a.version_id AND cr.field_name='class_source_key'
 WHERE r.school_id=p_school AND r.student_id=p_student AND a.kind='student_class_membership' AND a.payload->>'membership_status'='assigned'
), origins AS (
 SELECT p.version_id FROM personal p
 UNION SELECT a.version_id FROM classes c JOIN public.school_education_references r ON r.school_id=p_school AND r.education_id=c.id AND r.field_name='class_source_key'
 JOIN active a ON a.version_id=r.version_id AND a.kind='class_placement' WHERE NOT EXISTS(SELECT 1 FROM personal)
)
SELECT DISTINCT a.id FROM origins o JOIN public.school_education_references r ON r.school_id=p_school AND r.version_id=o.version_id AND r.field_name='calendar_source_key'
JOIN active a ON a.id=r.education_id AND a.kind='education_calendar_rule';
$f$;
REVOKE ALL ON FUNCTION app_private.f5_child_calendars(uuid,uuid,timestamptz) FROM PUBLIC;

CREATE FUNCTION app.school_fact_family_teaching_stamp(p_school uuid,p_adult uuid,p_relation uuid,p_version bigint)
RETURNS bigint LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog AS $f$
BEGIN
 IF SESSION_USER<>'campus_ai_school_fact_reader' OR p_school IS DISTINCT FROM app.require_school_database_anchor()
 OR NOT app_private.family_relationship_is_authoritative(p_school,p_adult,p_relation,p_version,statement_timestamp())
 OR NOT EXISTS(SELECT 1 FROM public.guardian_relationships r WHERE r.school_id=p_school AND r.id=p_relation
 AND r.relationship_kind='verified_guardian') THEN RAISE EXCEPTION 'current guardian denied' USING ERRCODE='28000'; END IF;
 RETURN app.school_fact_current_version(p_school);
END; $f$;
REVOKE ALL ON FUNCTION app.school_fact_family_teaching_stamp(uuid,uuid,uuid,bigint) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app.school_fact_family_teaching_stamp(uuid,uuid,uuid,bigint) TO campus_ai_school_fact_reader;

CREATE FUNCTION app.school_fact_family_teaching(p_school uuid,p_adult uuid,p_relation uuid,p_version bigint,
 p_factual_at timestamptz,p_period text,p_subject text,p_course text,p_contacts boolean)
RETURNS jsonb LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog AS $f$
DECLARE basis record; at_time timestamptz:=COALESCE(p_factual_at,statement_timestamp());
 result jsonb; status text:='available'; boundary timestamptz; term_end timestamptz; term_start timestamptz;
 term_count bigint; calendars uuid[]; enrollment text; period_limited boolean:=false; slices jsonb:='[]'; point timestamptz; times timestamptz[];
BEGIN
 PERFORM app.school_fact_family_teaching_stamp(p_school,p_adult,p_relation,p_version);
 IF p_period IS NULL OR p_period NOT IN ('current','current_term','next_term') OR p_contacts IS NULL
 OR (p_factual_at IS NOT NULL AND p_period<>'current') OR length(p_subject)>120 OR length(p_course)>120
 OR p_subject='' OR p_course='' THEN RAISE EXCEPTION 'invalid teaching selection' USING ERRCODE='22023'; END IF;
 SELECT * INTO basis FROM app.school_fact_family_authority(p_school,p_adult,p_relation,p_version);
 IF NOT FOUND OR basis.relationship_kind<>'verified_guardian' THEN RAISE EXCEPTION 'verified guardian required' USING ERRCODE='28000'; END IF;
 IF p_period<>'current' THEN
  SELECT array_agg(c.id) INTO calendars FROM (SELECT 1) one JOIN app_private.f5_child_calendars(p_school,basis.student_id,at_time) c ON true;
  SELECT count(*),min(v.valid_from),max(v.valid_to) INTO term_count,term_start,term_end
  FROM public.school_education_facts f JOIN public.school_education_versions v ON v.school_id=f.school_id AND v.fact_id=f.id
  JOIN public.master_data_import_batches b ON b.school_id=v.school_id AND b.id=v.import_batch_id
  JOIN public.school_education_references cr ON cr.school_id=p_school AND cr.version_id=v.id AND cr.field_name='calendar_source_key' AND cr.education_id=ANY(COALESCE(calendars,'{}'))
  WHERE f.school_id=p_school AND f.kind='education_term' AND v.superseded_at IS NULL AND b.status='applied'
  AND v.valid_from<=at_time AND at_time<v.valid_to;
  IF term_count=0 THEN status:='period_not_recorded';
  ELSIF term_count>1 THEN status:='ambiguous';
  ELSIF p_period='next_term' THEN
   SELECT min(v.valid_from) INTO term_start FROM public.school_education_facts f
   JOIN public.school_education_versions v ON v.school_id=f.school_id AND v.fact_id=f.id
   JOIN public.master_data_import_batches b ON b.school_id=v.school_id AND b.id=v.import_batch_id
  JOIN public.school_education_references cr ON cr.school_id=p_school AND cr.version_id=v.id AND cr.field_name='calendar_source_key' AND cr.education_id=ANY(COALESCE(calendars,'{}'))
   WHERE f.school_id=p_school AND f.kind='education_term' AND v.superseded_at IS NULL AND b.status='applied' AND v.valid_from>=term_end;
   IF term_start IS NULL THEN status:='period_not_recorded'; term_end:=NULL;
   ELSE
    SELECT count(*),max(v.valid_to) INTO term_count,term_end FROM public.school_education_facts f
    JOIN public.school_education_versions v ON v.school_id=f.school_id AND v.fact_id=f.id
    JOIN public.master_data_import_batches b ON b.school_id=v.school_id AND b.id=v.import_batch_id
  JOIN public.school_education_references cr ON cr.school_id=p_school AND cr.version_id=v.id AND cr.field_name='calendar_source_key' AND cr.education_id=ANY(COALESCE(calendars,'{}'))
    WHERE f.school_id=p_school AND f.kind='education_term' AND v.superseded_at IS NULL AND b.status='applied' AND v.valid_from=term_start;
    IF term_count<>1 THEN status:='ambiguous'; ELSE at_time:=term_start; END IF;
   END IF;
  ELSE at_time:=term_start;
  END IF;
 END IF;
 IF p_period='current_term' AND status='available' THEN
  period_limited:=(at_time<basis.valid_from OR (basis.valid_to IS NOT NULL AND term_end>basis.valid_to));
  at_time:=GREATEST(at_time,basis.valid_from);
  term_end:=LEAST(term_end,basis.valid_to);
 END IF;
 IF at_time<basis.valid_from OR (basis.valid_to IS NOT NULL AND at_time>=basis.valid_to)
 OR (term_end IS NOT NULL AND basis.valid_to IS NOT NULL AND term_end>basis.valid_to) THEN
 RAISE EXCEPTION 'teaching period outside guardian authority' USING ERRCODE='28000'; END IF;
 SELECT r.enrollment_status INTO enrollment FROM public.student_school_relationships r WHERE r.school_id=p_school
 AND r.student_id=basis.student_id AND r.superseded_at IS NULL AND r.valid_from<=at_time AND (r.valid_to IS NULL OR at_time<r.valid_to);
 IF status IN ('ambiguous','period_not_recorded') THEN
 result:=jsonb_build_object('records','[]'::jsonb,'related_teachers','[]'::jsonb,'as_of',at_time);
 ELSE result:=app_private.f5_child_projection(p_school,basis.student_id,at_time,p_subject,p_course,p_contacts); END IF;
 SELECT min(b.boundary_at) INTO boundary FROM app_private.f5_child_boundary(
 p_school,basis.student_id,LEAST(at_time,statement_timestamp()),'infinity',p_subject,p_course,p_contacts) b
 WHERE b.boundary_at>statement_timestamp();
 IF status='available' AND jsonb_array_length(result->'records')=0 THEN status:='no_current_records'; END IF;
 IF term_end IS NOT NULL AND status NOT IN ('ambiguous','period_not_recorded') THEN
  SELECT array_agg(t ORDER BY t) INTO times FROM (
   SELECT at_time AS t UNION SELECT b.boundary_at FROM app_private.f5_child_boundary(
    p_school,basis.student_id,at_time,term_end,p_subject,p_course,p_contacts) b
  ) boundaries;
  IF cardinality(times)>40 THEN RAISE EXCEPTION 'term contains too many changes; select a narrower period' USING ERRCODE='54000'; END IF;
  FOREACH point IN ARRAY times LOOP
   slices:=slices||jsonb_build_array(app_private.f5_child_projection(p_school,basis.student_id,point,p_subject,p_course,p_contacts));
  END LOOP;
 END IF;
 IF status='no_current_records' AND EXISTS(SELECT 1 FROM jsonb_array_elements(slices) s WHERE jsonb_array_length(s->'records')>0) THEN status:='available'; END IF;
 RETURN result||jsonb_build_object('status',status,'student_id',basis.student_id,'student_version',basis.student_version,
 'relationship_version',basis.relationship_version,'school_fact_version',app.school_fact_current_version(p_school),
 'enrollment_status',enrollment,'next_boundary',boundary,'period_start',at_time,'period_end',term_end,
 'period_limited_to_authorized',period_limited,'period_slices',slices);
END; $f$;
REVOKE ALL ON FUNCTION app.school_fact_family_teaching(uuid,uuid,uuid,bigint,timestamptz,text,text,text,boolean) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app.school_fact_family_teaching(uuid,uuid,uuid,bigint,timestamptz,text,text,text,boolean) TO campus_ai_school_fact_reader;
"""

STAFF_SQL = r"""
CREATE FUNCTION app.school_fact_f5_staff_coordinates(p_school uuid,p_person uuid,p_assignment uuid,p_as_of timestamptz)
RETURNS jsonb LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog AS $f$
DECLARE assignment_key text; person_key text; at_time timestamptz:=statement_timestamp();
BEGIN
 IF SESSION_USER<>'campus_ai_school_fact_reader' OR p_school IS DISTINCT FROM app.require_school_database_anchor()
 OR p_as_of IS NULL OR p_person IS NULL OR p_assignment IS NULL THEN
 RAISE EXCEPTION 'staff coordinate denied' USING ERRCODE='28000'; END IF;
 SELECT a.source_key INTO assignment_key FROM public.staff_assignments a
 JOIN public.staff_assignment_versions v ON v.school_id=a.school_id AND v.assignment_id=a.id
 WHERE a.school_id=p_school AND a.id=p_assignment AND v.person_id=p_person
 AND v.status='confirmed' AND v.revoked_at IS NULL AND v.effective_from<=at_time
 AND (v.effective_to IS NULL OR at_time<v.effective_to) AND (v.planned_valid_to IS NULL OR at_time<v.planned_valid_to)
 AND app_private.school_fact_identity_root_is_effective_at(p_school,'person',p_person,at_time)
 AND app_private.school_fact_identity_root_is_effective_at(p_school,'organization',v.organization_unit_id,at_time)
 AND EXISTS(SELECT 1 FROM public.school_fact_change_lineage l
 JOIN public.master_data_import_batches b ON b.school_id=l.school_id AND b.id=l.batch_id
 WHERE l.school_id=p_school AND l.fact_kind='assignment' AND l.fact_source_key=a.source_key
 AND l.target_id=a.id AND l.target_version=v.target_version AND b.status='applied');
 SELECT l.fact_source_key INTO person_key FROM public.school_fact_change_lineage l
 JOIN public.master_data_import_batches b ON b.school_id=l.school_id AND b.id=l.batch_id
 WHERE l.school_id=p_school AND l.fact_kind='person' AND l.target_id=p_person AND b.status='applied'
 ORDER BY l.target_version DESC LIMIT 1;
 IF assignment_key IS NULL OR person_key IS NULL THEN RAISE EXCEPTION 'current published staff required' USING ERRCODE='28000'; END IF;
 RETURN jsonb_build_object('assignment_source_key',assignment_key,'person_source_key',person_key,
 'school_fact_version',app.school_fact_current_version(p_school));
END; $f$;
REVOKE ALL ON FUNCTION app.school_fact_f5_staff_coordinates(uuid,uuid,uuid,timestamptz) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app.school_fact_f5_staff_coordinates(uuid,uuid,uuid,timestamptz) TO campus_ai_school_fact_reader;
"""

CORRECTION_SQL = r"""
CREATE FUNCTION app.school_fact_family_teaching_targets(p_school uuid,p_adult uuid,p_relation uuid,p_version bigint)
RETURNS TABLE(field_name text,label text,fact_kind text,fact_source_key text,target_version bigint,student_id uuid)
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog AS $f$
DECLARE child uuid;
BEGIN
 IF SESSION_USER<>'campus_ai_school_fact_writer' OR p_school IS DISTINCT FROM app.require_school_database_anchor()
 OR NOT app_private.family_relationship_is_authoritative(p_school,p_adult,p_relation,p_version,statement_timestamp()) THEN
 RAISE EXCEPTION 'teaching correction denied' USING ERRCODE='28000'; END IF;
 SELECT r.student_id INTO child FROM public.guardian_relationships r WHERE r.school_id=p_school AND r.id=p_relation
 AND r.guardian_person_id=p_adult AND r.relationship_kind='verified_guardian';
 IF child IS NULL THEN RAISE EXCEPTION 'verified guardian required' USING ERRCODE='28000'; END IF;
 RETURN QUERY SELECT names.field_name,k.kind,k.kind,k.source_key,f.version,child
 FROM app_private.f5_child_keys(p_school,child,statement_timestamp()) k
 JOIN public.school_education_facts f ON f.school_id=p_school AND f.kind=k.kind AND f.source_key=k.source_key
 CROSS JOIN LATERAL unnest(CASE k.kind
 WHEN 'teaching_subject' THEN ARRAY['display_name'] WHEN 'teaching_course' THEN ARRAY['display_name']
 WHEN 'course_subject' THEN ARRAY['subject_source_key']
 WHEN 'teaching_offering' THEN ARRAY['course_source_key','offering_status']
 WHEN 'offering_member_source' THEN ARRAY['source_kind','class_source_key','group_source_key']
 WHEN 'teaching_assignment' THEN ARRAY['assignment_source_key','teaching_role','subject_source_keys']
 WHEN 'teaching_group_member' THEN ARRAY['group_source_key','valid_to']
 ELSE '{}'::text[] END) names(field_name);
END; $f$;
REVOKE ALL ON FUNCTION app.school_fact_family_teaching_targets(uuid,uuid,uuid,bigint) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app.school_fact_family_teaching_targets(uuid,uuid,uuid,bigint) TO campus_ai_school_fact_writer;

CREATE FUNCTION app.school_fact_f5_field_changed(p_school uuid,p_kind text,p_source text,p_version bigint,p_field text)
RETURNS boolean LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog AS $f$
DECLARE target uuid; previous_version bigint; answer boolean;
BEGIN
 IF SESSION_USER<>'campus_ai_school_fact_writer' OR p_school IS DISTINCT FROM app.require_school_database_anchor() THEN
 RAISE EXCEPTION 'teaching difference denied' USING ERRCODE='28000'; END IF;
 IF p_version<2 OR p_field IS NULL OR app_private.f5_allowed_fields(p_kind) IS NULL
 OR NOT p_field=ANY(app_private.f5_allowed_fields(p_kind)) THEN RETURN false; END IF;
 SELECT f.id INTO target FROM public.school_education_facts f WHERE f.school_id=p_school
 AND f.kind=p_kind AND f.source_key=p_source AND f.version>=p_version;
 IF target IS NULL OR (SELECT count(*) FROM public.school_fact_change_lineage l
 JOIN public.master_data_import_batches b ON b.school_id=l.school_id AND b.id=l.batch_id
 WHERE l.school_id=p_school AND l.fact_kind=p_kind AND l.fact_source_key=p_source AND l.target_id=target
 AND l.target_version=p_version AND l.operation IN ('upsert','correct','end','cancel','revoke') AND b.status='applied')<>1 THEN RETURN false; END IF;
 SELECT max(l.target_version) INTO previous_version FROM public.school_fact_change_lineage l
 JOIN public.master_data_import_batches b ON b.school_id=l.school_id AND b.id=l.batch_id
 WHERE l.school_id=p_school AND l.fact_kind=p_kind AND l.fact_source_key=p_source AND l.target_id=target
 AND l.target_version<p_version AND b.status='applied';
 IF previous_version IS NULL THEN RETURN false; END IF;
 WITH rows AS (
 SELECT v.revision,v.valid_from,v.valid_to,CASE WHEN p_field='valid_to' THEN 'true'::jsonb ELSE v.payload->p_field END AS value
 FROM public.school_education_versions v WHERE v.school_id=p_school AND v.fact_id=target AND v.revision IN (previous_version,p_version)
 ), compared AS (
 SELECT r.*,CASE WHEN lag(r.valid_to) OVER w=r.valid_from AND lag(r.value) OVER w IS NOT DISTINCT FROM r.value
 THEN 0 ELSE 1 END AS start_group FROM rows r WINDOW w AS(PARTITION BY r.revision ORDER BY r.valid_from)
 ), grouped AS (
 SELECT c.*,sum(c.start_group) OVER(PARTITION BY c.revision ORDER BY c.valid_from) AS grp FROM compared c
 ), merged AS (
 SELECT g.revision,g.grp,min(g.valid_from) AS starts,CASE WHEN bool_or(g.valid_to IS NULL) THEN NULL ELSE max(g.valid_to) END AS ends,
 (array_agg(g.value ORDER BY g.valid_from))[1] AS value FROM grouped g GROUP BY g.revision,g.grp
 ), projections AS (
 SELECT m.revision,jsonb_agg(jsonb_build_array(m.starts,m.ends,m.value) ORDER BY m.starts) AS value FROM merged m GROUP BY m.revision
 ) SELECT (SELECT value FROM projections WHERE revision=p_version) IS DISTINCT FROM
 (SELECT value FROM projections WHERE revision=previous_version) INTO answer;
 RETURN COALESCE(answer,false);
END; $f$;
REVOKE ALL ON FUNCTION app.school_fact_f5_field_changed(uuid,text,text,bigint,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app.school_fact_f5_field_changed(uuid,text,text,bigint,text) TO campus_ai_school_fact_writer;
"""


def upgrade():
    op.execute("SET LOCAL ROLE campus_ai_db_owner")
    op.execute(BOUNDARY_GRANTS_SQL)
    op.execute("GRANT CREATE ON SCHEMA app,app_private TO campus_ai_context_definer")
    op.execute("SET LOCAL ROLE campus_ai_context_definer")
    op.execute(FAMILY_SQL)
    op.execute(STAFF_SQL)
    op.execute(CORRECTION_SQL)
    op.execute("SET LOCAL ROLE campus_ai_db_owner")
    op.execute("REVOKE CREATE ON SCHEMA app,app_private FROM campus_ai_context_definer")


def downgrade():
    raise RuntimeError("F5 rollback uses a compatible guarded artifact; no fact or ledger downgrade")
