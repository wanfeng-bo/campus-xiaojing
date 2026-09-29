# ruff: noqa: E501
"""Preserve each already-authorized public channel's own publication basis."""
from alembic import op

revision = "20260914_36"
down_revision = "20260914_35"
branch_labels = None
depends_on = None

CHANNEL_SOURCE_SQL = r"""
CREATE OR REPLACE FUNCTION app_private.f5_child_projection(p_school uuid,p_student uuid,p_at timestamptz,p_subject text,p_course text,p_contacts boolean)
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
 SELECT p.source_key AS teacher,c.owner_kind,c.contact_kind,c.contact_value,c.service_hours,ov.display_name AS owner,
 jsonb_build_object('kind','formal_publication','label','学校正式发布的官方渠道登记',
 'published_at',channel_publication.published_at) AS source
 FROM people p JOIN public.official_contact_points c ON c.school_id=p_school AND c.owner_kind='organization' AND c.organization_unit_id=p.organization_unit_id
 JOIN public.organization_unit_versions ov ON ov.school_id=c.school_id AND ov.organization_unit_id=c.organization_unit_id
 JOIN LATERAL (SELECT max(l.created_at) AS published_at
 FROM public.school_fact_change_lineage l JOIN public.master_data_import_batches b
 ON b.school_id=l.school_id AND b.id=l.batch_id
 WHERE l.school_id=c.school_id AND l.fact_kind='official_contact'
 AND l.target_id=c.id AND l.target_version=c.version
 AND l.operation IN ('upsert','reactivate') AND b.status='applied'
 HAVING count(*)=1) channel_publication ON true
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

"""


def upgrade():
    op.execute("SET LOCAL ROLE campus_ai_db_owner")
    op.execute("GRANT CREATE ON SCHEMA app_private TO campus_ai_context_definer")
    op.execute("SET LOCAL ROLE campus_ai_context_definer")
    op.execute(CHANNEL_SOURCE_SQL)
    op.execute("SET LOCAL ROLE campus_ai_db_owner")
    op.execute("REVOKE CREATE ON SCHEMA app_private FROM campus_ai_context_definer")


def downgrade():
    raise RuntimeError("Formal channel publication evidence is append-only")
