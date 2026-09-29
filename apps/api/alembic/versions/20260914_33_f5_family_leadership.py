# ruff: noqa: E501
"""F2-owned current family leadership projection and exact correction evidence."""

from alembic import op

revision = "20260914_33"
down_revision = "20260914_32"
branch_labels = None
depends_on = None

# Role arrays are supplied solely by the server-installed school policy, never
# accepted in HTTP/model schemas. This private helper is not an SQL public API.
LEADERSHIP_SQL = r"""
CREATE FUNCTION app_private.family_leadership_projection(
 p_school uuid,p_adult uuid,p_relation uuid,p_version bigint,
 p_school_codes text[],p_campus_codes text[],p_contacts boolean)
RETURNS jsonb LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog AS $f$
DECLARE basis record; result jsonb; at_time timestamptz:=statement_timestamp();
BEGIN
 IF p_school IS DISTINCT FROM app.require_school_database_anchor()
 OR NOT app_private.family_relationship_is_authoritative(p_school,p_adult,p_relation,p_version,at_time)
 OR p_school_codes IS NULL OR p_campus_codes IS NULL OR p_contacts IS NULL
 OR cardinality(p_school_codes)>16 OR cardinality(p_campus_codes)>16
 OR p_school_codes && p_campus_codes
 THEN RAISE EXCEPTION 'leadership denied' USING ERRCODE='28000'; END IF;
 SELECT r.student_id,s.version student_version,r.version relationship_version INTO basis
 FROM public.guardian_relationships r JOIN public.students s ON s.school_id=r.school_id AND s.id=r.student_id
 WHERE r.school_id=p_school AND r.id=p_relation AND r.relationship_kind='verified_guardian';
 IF NOT FOUND THEN RAISE EXCEPTION 'current guardian required' USING ERRCODE='28000'; END IF;
 WITH leaders AS MATERIALIZED (
 SELECT a.assignment_id,a.person_id,a.organization_unit_id,a.target_version,a.position_title,
 a.assignment_kind,CASE WHEN a.semantic_role_code=ANY(p_school_codes) THEN 'school_leader' ELSE 'campus_leader' END role_kind,
 n.display_name,n.version person_version,ou.display_name organization,ou.version organization_version,
 l.fact_source_key assignment_key,p.source_key person_key,o.source_key organization_key,
 jsonb_build_object('publication_reference','publication:'||l.batch_id::text,'published_at',l.created_at) source
 FROM public.staff_assignment_versions a
 JOIN public.school_fact_change_lineage l ON l.school_id=a.school_id AND l.fact_kind='assignment'
  AND l.target_id=a.assignment_id AND l.target_version=a.target_version AND l.operation IN ('upsert','reactivate')
 JOIN public.master_data_import_batches b ON b.school_id=l.school_id AND b.id=l.batch_id AND b.status='applied'
 JOIN public.school_people p ON p.school_id=a.school_id AND p.id=a.person_id
 JOIN public.school_person_names n ON n.school_id=a.school_id AND n.person_id=a.person_id
  AND n.status='confirmed' AND n.valid_from<=at_time AND (n.valid_to IS NULL OR at_time<n.valid_to)
 JOIN public.school_fact_change_lineage nl ON nl.school_id=n.school_id AND nl.fact_kind='person'
  AND nl.target_id=n.person_id AND nl.target_version=n.version AND nl.operation IN ('upsert','reactivate')
 JOIN public.master_data_import_batches nb ON nb.school_id=nl.school_id AND nb.id=nl.batch_id AND nb.status='applied'
 JOIN public.organization_units o ON o.school_id=a.school_id AND o.id=a.organization_unit_id
 JOIN public.organization_unit_versions ou ON ou.school_id=o.school_id AND ou.organization_unit_id=o.id
  AND ou.status='confirmed' AND ou.valid_from<=at_time AND (ou.valid_to IS NULL OR at_time<ou.valid_to)
 JOIN public.school_fact_change_lineage ol ON ol.school_id=ou.school_id AND ol.fact_kind='organization'
  AND ol.target_id=ou.organization_unit_id AND ol.target_version=ou.version AND ol.operation IN ('upsert','reactivate')
 JOIN public.master_data_import_batches ob ON ob.school_id=ol.school_id AND ob.id=ol.batch_id AND ob.status='applied'
 WHERE a.school_id=p_school AND a.status='confirmed' AND a.effective_from<=at_time
  AND (a.effective_to IS NULL OR at_time<a.effective_to)
  AND (a.planned_valid_to IS NULL OR at_time<a.planned_valid_to)
  AND (a.revoked_at IS NULL OR at_time<a.revoked_at)
  AND (a.semantic_role_code=ANY(p_school_codes) OR a.semantic_role_code=ANY(p_campus_codes))
  AND app_private.school_fact_identity_root_is_effective_at(p_school,'person',a.person_id,at_time)
  AND app_private.school_fact_identity_root_is_effective_at(p_school,'organization',a.organization_unit_id,at_time)
 ), campuses AS MATERIALIZED (
 SELECT DISTINCT c.organization_unit_id,v.display_name,c.source_key,c.version
 FROM public.organization_campus_relationships c
 JOIN leaders a ON a.organization_unit_id=c.organization_unit_id
 JOIN public.campus_location_versions v ON v.school_id=c.school_id AND v.location_id=c.campus_location_id
  AND v.status='confirmed' AND v.valid_from<=at_time AND (v.valid_to IS NULL OR at_time<v.valid_to)
 JOIN public.master_data_import_batches b ON b.school_id=c.school_id AND b.id=c.import_batch_id AND b.status='applied'
 WHERE c.school_id=p_school AND c.valid_from<=at_time AND (c.valid_to IS NULL OR at_time<c.valid_to)
 ), offices AS MATERIALIZED (
 SELECT DISTINCT ol.organization_unit_id,ol.location_id,v.display_name
 FROM public.organization_locations ol JOIN leaders a ON a.organization_unit_id=ol.organization_unit_id
 JOIN public.master_data_import_batches b ON b.school_id=ol.school_id AND b.id=ol.import_batch_id AND b.status='applied'
 JOIN public.campus_location_versions v ON v.school_id=ol.school_id AND v.location_id=ol.location_id
  AND v.status='confirmed' AND v.valid_from<=at_time AND (v.valid_to IS NULL OR at_time<v.valid_to)
 WHERE p_contacts AND ol.school_id=p_school AND ol.purpose='office' AND ol.valid_from<=at_time
  AND (ol.valid_to IS NULL OR at_time<ol.valid_to) AND (ol.revoked_at IS NULL OR at_time<ol.revoked_at)
 ), channels AS MATERIALIZED (
 SELECT DISTINCT a.assignment_id,c.id,c.owner_kind,c.contact_kind,c.contact_value,c.service_hours,c.version,
  cl.fact_source_key source_key,
  CASE WHEN c.owner_kind='organization' THEN a.organization ELSE office.display_name END owner_name,
  jsonb_build_object('publication_reference','publication:'||cl.batch_id::text,'published_at',cl.created_at) source
 FROM leaders a
 JOIN public.official_contact_points c ON c.school_id=p_school AND p_contacts
 LEFT JOIN offices office ON office.organization_unit_id=a.organization_unit_id AND office.location_id=c.location_id
 JOIN public.school_fact_change_lineage cl ON cl.school_id=c.school_id AND cl.fact_kind='official_contact'
  AND cl.target_id=c.id AND cl.target_version=c.version AND cl.operation IN ('upsert','reactivate')
 JOIN public.master_data_import_batches cb ON cb.school_id=cl.school_id AND cb.id=cl.batch_id AND cb.status='applied'
 WHERE c.lifecycle_status='active' AND c.visibility_scope='public' AND c.valid_from<=at_time
  AND (c.valid_to IS NULL OR at_time<c.valid_to)
  AND ((c.owner_kind='organization' AND c.organization_unit_id=a.organization_unit_id)
    OR (c.owner_kind='location' AND office.location_id IS NOT NULL))
 ), qualities AS MATERIALIZED (
 SELECT q.source_key,q.version,q.quality_kind,q.coverage_status,q.dispute_status,q.source_status,
 q.negative_code,q.verified_at,q.review_due_at,q.target_fact_kind,q.field_name,
 CASE WHEN q.target_fact_kind='assignment' THEN
  (SELECT string_agg(DISTINCT a.display_name||'：'||a.position_title,'；') FROM leaders a WHERE a.assignment_key=q.target_source_key)
 ELSE (SELECT string_agg(DISTINCT c.owner_name||'：'||c.contact_kind,'；') FROM channels c WHERE c.source_key=q.target_source_key) END applies_to,
 jsonb_build_object('publication_reference','publication:'||ql.batch_id::text,'published_at',ql.created_at) source,
 CASE WHEN q.review_due_at<=at_time THEN 'review_overdue' ELSE 'unknown' END freshness
 FROM public.school_fact_qualities q
 JOIN public.school_fact_change_lineage ql ON ql.school_id=q.school_id AND ql.fact_kind='fact_quality'
  AND ql.fact_source_key=q.source_key AND ql.target_id=q.id AND ql.target_version=q.version
 JOIN public.master_data_import_batches b ON b.school_id=ql.school_id AND b.id=ql.batch_id AND b.id=q.import_batch_id AND b.status='applied'
 WHERE q.school_id=p_school AND q.valid_from<=at_time AND (q.valid_to IS NULL OR at_time<q.valid_to)
  AND (q.revoked_at IS NULL OR at_time<q.revoked_at) AND q.verified_at<=at_time
  AND ((q.target_fact_kind='assignment' AND EXISTS(SELECT 1 FROM leaders a WHERE q.target_source_key=a.assignment_key
    AND (q.organization_unit_id=a.organization_unit_id OR q.person_id=a.person_id)))
   OR (p_contacts AND q.target_fact_kind='official_contact' AND EXISTS(SELECT 1 FROM channels c WHERE c.source_key=q.target_source_key)))
  AND (q.field_name IS NULL OR q.field_name IN ('display_name','position_title','person_source_key','organization_source_key','contact_value','contact_kind','service_hours'))
 ), targets AS (
 SELECT DISTINCT 'person'::text fact_kind,a.person_key fact_source_key,a.person_version target_version,
  'display_name'::text field_name,left(a.display_name,80)||'：姓名' label FROM leaders a
 UNION ALL SELECT 'assignment',a.assignment_key,a.target_version,f.field_name,left(a.display_name,80)||'：'||f.label FROM leaders a
 CROSS JOIN (VALUES ('position_title','职务'),('person_source_key','任职人员'),('organization_source_key','任职组织')) f(field_name,label)
 UNION ALL SELECT DISTINCT 'official_contact',c.source_key,c.version,f.field_name,left(c.owner_name,80)||'：'||f.label FROM channels c
 CROSS JOIN (VALUES ('contact_value','官方渠道'),('contact_kind','渠道类型'),('service_hours','服务时间')) f(field_name,label)
 WHERE f.field_name<>'service_hours' OR c.service_hours IS NOT NULL
 )
 SELECT jsonb_build_object('student_id',basis.student_id,'student_version',basis.student_version,
 'relationship_version',basis.relationship_version,'as_of',at_time,
 'dependencies',jsonb_build_object(
  'leaders',COALESCE((SELECT jsonb_agg(jsonb_build_array(a.assignment_key,a.target_version,a.person_key,a.person_version,a.organization_key,a.organization_version) ORDER BY a.assignment_key) FROM leaders a),'[]'::jsonb),
  'campuses',COALESCE((SELECT jsonb_agg(jsonb_build_array(c.source_key,c.version) ORDER BY c.source_key) FROM campuses c),'[]'::jsonb)),
 'status',CASE WHEN EXISTS(SELECT 1 FROM leaders) THEN 'available' ELSE 'no_current_records' END,
 'leaders',COALESCE((SELECT jsonb_agg(jsonb_build_object('display_name',a.display_name,'position_title',a.position_title,
  'assignment_kind',a.assignment_kind,'role_kind',a.role_kind,'organization',a.organization,'source',a.source,
  'campuses',COALESCE((SELECT jsonb_agg(c.display_name ORDER BY c.source_key) FROM campuses c WHERE c.organization_unit_id=a.organization_unit_id),'[]'::jsonb),
  'official_contacts',COALESCE((SELECT jsonb_agg(jsonb_build_object('owner_kind',c.owner_kind,'owner_name',c.owner_name,
   'contact_kind',c.contact_kind,'contact_value',c.contact_value,'service_hours',c.service_hours,'source',c.source) ORDER BY c.source_key)
   FROM channels c WHERE c.assignment_id=a.assignment_id),'[]'::jsonb)) ORDER BY a.assignment_key) FROM leaders a),'[]'::jsonb),
 'quality',COALESCE((SELECT jsonb_agg(to_jsonb(q) ORDER BY q.source_key) FROM qualities q),'[]'::jsonb),
 'targets',COALESCE((SELECT jsonb_agg(to_jsonb(t) ORDER BY t.fact_kind,t.fact_source_key,t.field_name) FROM targets t),'[]'::jsonb)) INTO result;
 IF jsonb_array_length(result->'leaders')>32 OR jsonb_array_length(result->'quality')>100
 OR jsonb_array_length(result->'targets')>320 THEN
 RAISE EXCEPTION 'leadership set exceeds finite bound' USING ERRCODE='54000'; END IF;
 RETURN result;
END;
$f$;
REVOKE ALL ON FUNCTION app_private.family_leadership_projection(uuid,uuid,uuid,bigint,text[],text[],boolean) FROM PUBLIC,campus_ai_school_fact_reader,campus_ai_school_fact_writer;

CREATE FUNCTION app.school_fact_family_leadership(p_school uuid,p_adult uuid,p_relation uuid,p_version bigint,p_school_codes text[],p_campus_codes text[],p_contacts boolean)
RETURNS jsonb LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog AS $f$
BEGIN
 IF SESSION_USER<>'campus_ai_school_fact_reader' THEN RAISE EXCEPTION 'leadership Reader denied' USING ERRCODE='28000'; END IF;
 RETURN app_private.family_leadership_projection(p_school,p_adult,p_relation,p_version,p_school_codes,p_campus_codes,p_contacts);
END;
$f$;
REVOKE ALL ON FUNCTION app.school_fact_family_leadership(uuid,uuid,uuid,bigint,text[],text[],boolean) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app.school_fact_family_leadership(uuid,uuid,uuid,bigint,text[],text[],boolean) TO campus_ai_school_fact_reader;

CREATE FUNCTION app.school_fact_family_leadership_targets(p_school uuid,p_adult uuid,p_relation uuid,p_version bigint,p_school_codes text[],p_campus_codes text[],p_contacts boolean)
RETURNS TABLE(field_name text,label text,fact_kind text,fact_source_key text,target_version bigint,student_id uuid)
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog AS $f$
DECLARE result jsonb;
BEGIN
 IF SESSION_USER<>'campus_ai_school_fact_writer' THEN RAISE EXCEPTION 'leadership Writer denied' USING ERRCODE='28000'; END IF;
 result:=app_private.family_leadership_projection(p_school,p_adult,p_relation,p_version,p_school_codes,p_campus_codes,p_contacts);
 RETURN QUERY SELECT t->>'field_name',t->>'label',t->>'fact_kind',t->>'fact_source_key',(t->>'target_version')::bigint,(result->>'student_id')::uuid
 FROM jsonb_array_elements(result->'targets') t;
END;
$f$;
REVOKE ALL ON FUNCTION app.school_fact_family_leadership_targets(uuid,uuid,uuid,bigint,text[],text[],boolean) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app.school_fact_family_leadership_targets(uuid,uuid,uuid,bigint,text[],text[],boolean) TO campus_ai_school_fact_writer;
"""

DIFFERENCE_SQL = r"""
CREATE FUNCTION app.school_fact_f2_family_field_changed(p_school uuid,p_kind text,p_source text,p_version bigint,p_field text)
RETURNS boolean LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog AS $f$
DECLARE new_id uuid; old_id uuid; old_version bigint; answer boolean:=false;
BEGIN
 IF SESSION_USER<>'campus_ai_school_fact_writer' OR p_school IS DISTINCT FROM app.require_school_database_anchor()
 THEN RAISE EXCEPTION 'F2 correction difference denied' USING ERRCODE='28000'; END IF;
 SELECT l.target_id INTO new_id FROM public.school_fact_change_lineage l
 JOIN public.master_data_import_batches b ON b.school_id=l.school_id AND b.id=l.batch_id AND b.status='applied'
 WHERE l.school_id=p_school AND l.fact_kind=p_kind AND l.fact_source_key=p_source AND l.target_version=p_version AND l.operation='upsert';
 SELECT l.target_id,l.target_version INTO old_id,old_version FROM public.school_fact_change_lineage l
 JOIN public.master_data_import_batches b ON b.school_id=l.school_id AND b.id=l.batch_id AND b.status='applied'
 WHERE l.school_id=p_school AND l.fact_kind=p_kind AND l.fact_source_key=p_source AND l.target_version<p_version
 AND l.operation IN ('upsert','reactivate') ORDER BY l.target_version DESC LIMIT 1;
 IF new_id IS NULL OR old_id IS NULL THEN RETURN false; END IF;
 IF p_kind='person' AND p_field='display_name' THEN
  SELECT n.display_name IS DISTINCT FROM o.display_name INTO answer
  FROM public.school_person_names n JOIN public.school_person_names o ON o.school_id=n.school_id
  WHERE n.school_id=p_school AND n.person_id=new_id AND n.version=p_version AND o.person_id=old_id AND o.version=old_version;
 ELSIF p_kind='assignment' THEN
  SELECT CASE p_field WHEN 'position_title' THEN n.position_title IS DISTINCT FROM o.position_title
   WHEN 'person_source_key' THEN n.person_id IS DISTINCT FROM o.person_id
   WHEN 'organization_source_key' THEN n.organization_unit_id IS DISTINCT FROM o.organization_unit_id ELSE false END INTO answer
  FROM public.staff_assignment_versions n JOIN public.staff_assignment_versions o ON o.school_id=n.school_id
  WHERE n.school_id=p_school AND n.assignment_id=new_id AND n.target_version=p_version AND o.assignment_id=old_id AND o.target_version=old_version;
 ELSIF p_kind='official_contact' THEN
  SELECT CASE p_field WHEN 'contact_value' THEN n.contact_value IS DISTINCT FROM o.contact_value
   WHEN 'contact_kind' THEN n.contact_kind IS DISTINCT FROM o.contact_kind
   WHEN 'service_hours' THEN n.service_hours IS DISTINCT FROM o.service_hours ELSE false END INTO answer
  FROM public.official_contact_points n JOIN public.official_contact_points o ON o.school_id=n.school_id
  WHERE n.school_id=p_school AND n.id=new_id AND n.version=p_version AND o.id=old_id AND o.version=old_version;
 END IF;
 RETURN COALESCE(answer,false);
END;
$f$;
REVOKE ALL ON FUNCTION app.school_fact_f2_family_field_changed(uuid,text,text,bigint,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app.school_fact_f2_family_field_changed(uuid,text,text,bigint,text) TO campus_ai_school_fact_writer;
"""


REVIEW_SQL = r"""
ALTER TABLE public.family_fact_correction_reports
 ADD COLUMN review_explanation text,
 ADD COLUMN reviewer_person_id uuid,
 ADD COLUMN reviewer_assignment_id uuid,
 ADD COLUMN reviewer_assignment_version bigint,
 ADD COLUMN reviewer_authorization_version bigint,
 ADD COLUMN review_command_sha256 varchar(64),
 ADD COLUMN review_target_version bigint;
ALTER TABLE public.family_fact_correction_reports DROP CONSTRAINT ck_family_correction_status;
ALTER TABLE public.family_fact_correction_reports DROP CONSTRAINT ck_family_correction_resolution;
ALTER TABLE public.family_fact_correction_reports
 ADD CONSTRAINT ck_family_correction_status CHECK(status IN ('submitted','resolved','reviewed')),
 ADD CONSTRAINT fk_family_review_person FOREIGN KEY(school_id,reviewer_person_id) REFERENCES public.school_people(school_id,id),
 ADD CONSTRAINT fk_family_review_assignment FOREIGN KEY(school_id,reviewer_assignment_id) REFERENCES public.staff_assignments(school_id,id),
 ADD CONSTRAINT ck_family_correction_resolution CHECK(
  (status='submitted' AND resolution_locator_sha256 IS NULL AND resolved_at IS NULL)
  OR (status='resolved' AND resolution_locator_sha256 IS NOT NULL AND resolution_locator_sha256 ~ '^[0-9a-f]{64}$' AND resolved_at IS NOT NULL)
  OR (status='reviewed' AND resolution_locator_sha256 IS NULL AND resolved_at IS NOT NULL)),
 ADD CONSTRAINT ck_family_correction_review CHECK(
  (status='reviewed' AND review_explanation IS NOT NULL AND char_length(review_explanation) BETWEEN 1 AND 1000
   AND reviewer_person_id IS NOT NULL AND reviewer_assignment_id IS NOT NULL
   AND reviewer_assignment_version IS NOT NULL AND reviewer_assignment_version>=1
   AND reviewer_authorization_version IS NOT NULL AND reviewer_authorization_version>=1
   AND review_command_sha256 IS NOT NULL AND review_command_sha256 ~ '^[0-9a-f]{64}$'
   AND review_target_version IS NOT NULL AND review_target_version>=fact_target_version)
  OR (status<>'reviewed' AND review_explanation IS NULL AND reviewer_person_id IS NULL
   AND reviewer_assignment_id IS NULL AND reviewer_assignment_version IS NULL AND reviewer_authorization_version IS NULL
   AND review_command_sha256 IS NULL AND review_target_version IS NULL));
CREATE FUNCTION app_private.family_review_immutable() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog AS $f$
BEGIN
 IF OLD.status='reviewed' THEN RAISE EXCEPTION 'review audit is immutable' USING ERRCODE='23514'; END IF;
 IF TG_OP='DELETE' THEN RETURN OLD; END IF;
 RETURN NEW;
END;
$f$;
REVOKE ALL ON FUNCTION app_private.family_review_immutable() FROM PUBLIC;
CREATE TRIGGER family_review_immutable BEFORE UPDATE OR DELETE ON public.family_fact_correction_reports
 FOR EACH ROW EXECUTE FUNCTION app_private.family_review_immutable();
"""


def upgrade():
    op.execute("SET LOCAL ROLE campus_ai_db_owner")
    op.execute("GRANT SELECT,TRIGGER ON public.family_fact_correction_reports TO campus_ai_db_owner")
    op.execute(REVIEW_SQL)
    op.execute("REVOKE SELECT,TRIGGER ON public.family_fact_correction_reports FROM campus_ai_db_owner")
    # Existing definer columns are reused. Only these specific joins/projections
    # add columns; none grant private person contacts or internal appointment data.
    columns = {
        "organization_unit_versions": "school_id,organization_unit_id,display_name,status,valid_from,valid_to,version",
        "organization_campus_relationships": "school_id,organization_unit_id,campus_location_id,source_key,version,valid_from,valid_to,import_batch_id",
        "organization_locations": "school_id,organization_unit_id,location_id,purpose,valid_from,valid_to,revoked_at,import_batch_id",
        "official_contact_points": "id,school_id,owner_kind,organization_unit_id,location_id,contact_kind,contact_value,service_hours,version,lifecycle_status,visibility_scope,valid_from,valid_to",
        "school_fact_qualities": "school_id,source_key,version,quality_kind,coverage_status,dispute_status,source_status,negative_code,verified_at,review_due_at,target_fact_kind,target_source_key,field_name,organization_unit_id,person_id,valid_from,valid_to,revoked_at,import_batch_id",
    }
    for table, projection in columns.items():
        op.execute(f"GRANT SELECT ({projection}) ON public.{table} TO campus_ai_context_definer")
        op.execute(
            f"CREATE POLICY f5_leadership_context_read ON public.{table} FOR SELECT TO campus_ai_context_definer USING (school_id=app.require_school_database_anchor())"
        )
    op.execute("GRANT CREATE ON SCHEMA app,app_private TO campus_ai_context_definer")
    op.execute("SET LOCAL ROLE campus_ai_context_definer")
    op.execute(LEADERSHIP_SQL)
    op.execute(DIFFERENCE_SQL)
    op.execute("SET LOCAL ROLE campus_ai_db_owner")
    op.execute("REVOKE CREATE ON SCHEMA app,app_private FROM campus_ai_context_definer")


def downgrade():
    raise RuntimeError("Leadership downgrade requires explicit compatibility review")
