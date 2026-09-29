"""Formal family relations and current selected-child Reader boundary.

Old relationship rows retain authority_version=NULL and cannot become login authority.
"""

import sqlalchemy as sa

from alembic import op

revision = "20260913_26"
down_revision = "20260906_25"
branch_labels = None
depends_on = None

FAMILY_AUTHORITY_SQL = r"""
CREATE FUNCTION app_private.family_relationship_is_authoritative(
    p_school uuid, p_adult uuid, p_relation uuid, p_version bigint, p_at timestamptz
) RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog AS $f$
SELECT EXISTS (
    SELECT 1 FROM public.guardian_relationships AS r
    WHERE r.school_id=p_school AND r.guardian_person_id=p_adult
      AND r.id=p_relation AND r.version=p_version
      AND r.authority_version = 1 AND r.status = 'confirmed'
      AND r.revoked_at IS NULL AND r.verified_by_person_id IS NOT NULL
      AND r.verified_by_assignment_id IS NOT NULL AND r.verified_at IS NOT NULL
      AND r.source_id IS NOT NULL
      AND ((r.relationship_kind='verified_guardian' AND
    r.access_purpose='guardian_student_profile')
        OR (r.relationship_kind='recognized_caregiver' AND
    r.access_purpose='caregiver_student_summary'))
      AND r.valid_from<=p_at AND (r.valid_to IS NULL OR p_at<r.valid_to)
      AND app_private.school_fact_identity_root_is_effective_at(p_school,'school',p_school,p_at)
      AND app_private.school_fact_identity_root_is_effective_at(p_school,'person',p_adult,p_at)
      AND app_private.school_fact_identity_root_is_effective_at(p_school,'student',r.student_id,
    p_at)
);
$f$;
REVOKE ALL ON FUNCTION app_private.family_relationship_is_authoritative(uuid,uuid,uuid,bigint,
    timestamptz)
 FROM PUBLIC,campus_ai_runtime,campus_ai_school_fact_reader,campus_ai_school_fact_writer,
 campus_ai_migration,campus_ai_db_owner,campus_ai_identity_definer;

CREATE FUNCTION app.school_fact_family_authority(
    p_school uuid, p_adult uuid, p_relation uuid, p_version bigint
) RETURNS TABLE(school_id uuid, adult_person_id uuid, relationship_id uuid,
    relationship_version bigint, student_id uuid, student_version bigint,
    relationship_kind text, valid_from timestamptz, valid_to timestamptz)
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog AS $f$
BEGIN
    IF SESSION_USER <> 'campus_ai_school_fact_reader'
       OR p_school IS DISTINCT FROM app.require_school_database_anchor() THEN
        RAISE EXCEPTION 'family Reader denied' USING ERRCODE='28000';
    END IF;
    RETURN QUERY
    SELECT r.school_id,r.guardian_person_id,r.id,r.version,s.id,s.version,
           r.relationship_kind::text,r.valid_from,r.valid_to
    FROM public.guardian_relationships r
    JOIN public.students s ON s.school_id=r.school_id AND s.id=r.student_id
    WHERE app_private.family_relationship_is_authoritative(
        p_school,p_adult,p_relation,p_version,statement_timestamp())
      AND r.school_id=p_school AND r.id=p_relation AND r.guardian_person_id=p_adult;
END;
$f$;
REVOKE ALL ON FUNCTION app.school_fact_family_authority(uuid,uuid,uuid,bigint) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app.school_fact_family_authority(uuid,uuid,uuid,bigint)
 TO campus_ai_school_fact_reader;
"""

FAMILY_READER_SQL = r"""
CREATE FUNCTION app.school_fact_family_profile(
 p_school uuid,p_adult uuid,p_relation uuid,p_version bigint,p_factual_at timestamptz
) RETURNS jsonb LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog AS $f$
DECLARE
 basis record;
 at_time timestamptz := COALESCE(p_factual_at,statement_timestamp());
 child_person uuid; campus_id uuid;
 n_count bigint; e_count bigint; r_count bigint := 0; c_count bigint := 0;
 n_version bigint; campus_version bigint;
 child_name text; campus_name text; enrolled jsonb; district jsonb;
 profile jsonb; sources jsonb; missing jsonb; state text;
 conflicts jsonb := '[]'::jsonb;
 enrollment_id uuid; enrollment_version bigint; region_id uuid; region_version bigint;
BEGIN
 IF SESSION_USER <> 'campus_ai_school_fact_reader'
    OR p_school IS DISTINCT FROM app.require_school_database_anchor() THEN
   RAISE EXCEPTION 'family Reader denied' USING ERRCODE='28000';
 END IF;
 SELECT a.school_id,a.adult_person_id,a.relationship_id,a.relationship_version,
        a.student_id,a.student_version,a.relationship_kind,a.valid_from,a.valid_to
 INTO basis FROM app.school_fact_family_authority(p_school,p_adult,p_relation,p_version) a;
 IF NOT FOUND OR at_time<basis.valid_from OR at_time>statement_timestamp()
    OR (basis.valid_to IS NOT NULL AND at_time>=basis.valid_to)
    OR (basis.relationship_kind='recognized_caregiver' AND p_factual_at IS NOT NULL) THEN
   RAISE EXCEPTION 'family relation does not permit this time' USING ERRCODE='28000';
 END IF;
 SELECT s.person_id INTO child_person FROM public.students s
 WHERE s.school_id=p_school AND s.id=basis.student_id;
 SELECT count(*),min(n.display_name),min(n.version) INTO n_count,child_name,n_version
 FROM public.school_person_names n WHERE n.school_id=p_school AND n.person_id=child_person
 AND n.status='confirmed' AND n.valid_from<=at_time
 AND (n.valid_to IS NULL OR at_time<n.valid_to);
 SELECT count(*) INTO e_count FROM public.student_school_relationships e
 WHERE e.school_id=p_school AND e.student_id=basis.student_id
 AND e.valid_from<=at_time AND (e.valid_to IS NULL OR at_time<e.valid_to);
 IF e_count=1 THEN
   IF basis.relationship_kind='verified_guardian' THEN
     SELECT e.id,e.version,e.primary_campus_location_id,jsonb_build_object(
       'enrollment_status',e.enrollment_status,'student_number',e.student_number,
       'residence_mode',e.residence_mode)
     INTO enrollment_id,enrollment_version,campus_id,enrolled
     FROM public.student_school_relationships e
     WHERE e.school_id=p_school AND e.student_id=basis.student_id
     AND e.valid_from<=at_time AND (e.valid_to IS NULL OR at_time<e.valid_to);
   ELSE
     -- The caregiver statement never selects student_number or residence_mode.
     SELECT e.id,e.version,e.primary_campus_location_id,
       jsonb_build_object('enrollment_status',e.enrollment_status)
     INTO enrollment_id,enrollment_version,campus_id,enrolled
     FROM public.student_school_relationships e
     WHERE e.school_id=p_school AND e.student_id=basis.student_id
     AND e.valid_from<=at_time AND (e.valid_to IS NULL OR at_time<e.valid_to);
   END IF;
   SELECT count(*),min(c.display_name),min(c.version)
   INTO c_count,campus_name,campus_version FROM public.campus_location_versions c
   WHERE c.school_id=p_school AND c.location_id=campus_id AND c.status='confirmed'
   AND c.valid_from<=at_time AND (c.valid_to IS NULL OR at_time<c.valid_to);
 END IF;
 profile := jsonb_build_object('name',CASE WHEN n_count=1 THEN child_name END,
   'enrollment_status',NULL,'campus',CASE WHEN c_count=1 THEN campus_name END)
   || COALESCE(enrolled,'{}'::jsonb);
 IF basis.relationship_kind='verified_guardian' THEN
   SELECT count(*) INTO r_count FROM public.person_regions r
   WHERE r.school_id=p_school AND r.person_id=child_person
   AND r.region_purpose='student_profile'
   AND (r.revoked_at IS NULL OR at_time<r.revoked_at)
   AND r.valid_from<=at_time AND (r.valid_to IS NULL OR at_time<r.valid_to);
   IF r_count=1 THEN
     SELECT r.id,r.version,jsonb_build_object('district_code',r.district_code,
       'district_name',r.district_name) INTO region_id,region_version,district
     FROM public.person_regions r WHERE r.school_id=p_school AND r.person_id=child_person
     AND r.region_purpose='student_profile'
   AND (r.revoked_at IS NULL OR at_time<r.revoked_at)
     AND r.valid_from<=at_time AND (r.valid_to IS NULL OR at_time<r.valid_to);
   END IF;
   profile := jsonb_build_object('student_number',NULL,'residence_mode',NULL)
     || profile || jsonb_build_object('region',district);
 END IF;
 state := CASE WHEN n_count>1 OR e_count>1 OR r_count>1 OR c_count>1 THEN 'conflict'
   WHEN n_count=0 AND e_count=0 AND r_count=0 THEN 'no_current_records' ELSE 'available' END;
 IF n_count>1 THEN conflicts := conflicts||jsonb_build_array('name'); END IF;
 IF e_count>1 THEN
   conflicts := conflicts||jsonb_build_array('enrollment_status','campus');
   IF basis.relationship_kind='verified_guardian' THEN
     conflicts := conflicts||jsonb_build_array('student_number','residence_mode');
   END IF;
 END IF;
 IF c_count>1 THEN conflicts := conflicts||jsonb_build_array('campus'); END IF;
 IF r_count>1 THEN conflicts := conflicts||jsonb_build_array('region'); END IF;
 SELECT COALESCE(jsonb_agg(k),'[]'::jsonb) INTO missing
 FROM jsonb_each(profile) AS fields(k,v) WHERE v='null'::jsonb AND NOT (conflicts ? k);
 SELECT COALESCE(jsonb_agg(jsonb_build_object(
   'fact',q.fact_kind,'reference','publication:'||q.batch_id::text,
   'published_at',q.created_at::text,'source_name',NULL,
   'source_trace_reference',q.source_trace_reference,
   'channel',CASE q.source_kind WHEN 'manual' THEN '人工正式发布'
     WHEN 'excel' THEN '表格正式导入' WHEN 'system' THEN '系统正式发布'
     WHEN 'legacy' THEN '历史迁入' ELSE '来源渠道未知' END)),'[]'::jsonb)
 INTO sources FROM (
   SELECT DISTINCT l.fact_kind,l.batch_id,l.created_at,s.source_kind,
     s.source_ref AS source_trace_reference
   FROM public.school_fact_change_lineage l
   JOIN public.master_data_import_batches b ON b.school_id=l.school_id AND b.id=l.batch_id
   LEFT JOIN public.school_fact_sources s ON s.school_id=l.school_id
     AND s.source_key='change-set:'||l.batch_id::text
   WHERE l.school_id=p_school AND b.status='applied'
     AND l.operation IN ('upsert','reactivate') AND (
     (n_count=1 AND l.fact_kind='person' AND l.target_id=child_person
       AND l.target_version=n_version)
     OR (l.fact_kind='student_school_relationship' AND l.target_id=enrollment_id
       AND l.target_version=enrollment_version)
     OR (l.fact_kind='person_region' AND l.target_id=region_id
       AND l.target_version=region_version)
     OR (c_count=1 AND l.fact_kind='location' AND l.target_id=campus_id
       AND l.target_version=campus_version))
   ORDER BY l.created_at,l.fact_kind
 ) q;
 RETURN jsonb_build_object('status',state,'profile',profile,'sources',sources,'missing',missing,
   'conflicting_fields',conflicts);
END;
$f$;
REVOKE ALL ON FUNCTION app.school_fact_family_profile(uuid,uuid,uuid,bigint,timestamptz)
 FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app.school_fact_family_profile(uuid,uuid,uuid,bigint,timestamptz)
 TO campus_ai_school_fact_reader;
"""

FAMILY_STAFF_SQL = r"""
CREATE FUNCTION app.school_fact_f3_staff_target(
 p_school uuid,p_kind text,p_source text,p_creation boolean,p_factual_at timestamptz,
 p_fields text[]
) RETURNS jsonb LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog AS $f$
DECLARE
 chosen record; latest bigint; latest_at timestamptz; matches bigint; payloads jsonb; content jsonb;
 display_label text;
 global_version bigint; allowed_fields text[]; ends_at timestamptz; empty_result jsonb;
BEGIN
 IF SESSION_USER <> 'campus_ai_school_fact_reader'
    OR p_school IS DISTINCT FROM app.require_school_database_anchor() THEN
   RAISE EXCEPTION 'F3 Reader denied' USING ERRCODE='28000';
 END IF;
 IF p_source IS NULL OR btrim(p_source)='' OR length(p_source)>255
    OR p_creation IS NULL OR p_factual_at IS NULL OR p_kind IS NULL
    OR p_kind NOT IN ('student','student_school_relationship','student_contact',
       'guardian_relationship','person_region','person','login_mapping','assignment') THEN
   RAISE EXCEPTION 'invalid fixed F3 target' USING ERRCODE='22023';
 END IF;
 allowed_fields := CASE p_kind
 WHEN 'student' THEN ARRAY['person_source_key','status']
 WHEN 'student_school_relationship' THEN
    ARRAY['administrative_campus_source_key','enrollment_status','primary_campus_source_key','residence_mode','student_number','student_source_key']
 WHEN 'student_contact' THEN
    ARRAY['contact_person_source_key','is_emergency','priority','relationship_kind','student_source_key']
 WHEN 'guardian_relationship' THEN
    ARRAY['access_purpose','guardian_person_source_key','relationship_kind','review_of_relationship_source_key','status','student_source_key','verification_reference','verification_source']
 WHEN 'person_region' THEN
    ARRAY['district_code','district_name','person_source_key','region_purpose']
 WHEN 'person' THEN ARRAY['business_code','display_name','lifecycle_status','person_kind']
 WHEN 'login_mapping' THEN
    ARRAY['external_subject','person_source_key','provider_code','provider_tenant_key','status']
 WHEN 'assignment' THEN
    ARRAY['appointment_source','approval_reference','assignment_kind','organization_source_key','person_source_key','position_title','semantic_role_code','status','valid_to']
    END;
 IF p_fields IS NULL OR cardinality(p_fields)=0 OR NOT p_fields<@allowed_fields
    OR array_position(p_fields,NULL) IS NOT NULL
    OR cardinality(p_fields)<>(SELECT count(DISTINCT f) FROM unnest(p_fields) f) THEN
   RAISE EXCEPTION 'invalid fixed F3 fields' USING ERRCODE='22023';
 END IF;
 global_version := app.school_fact_current_version(p_school);
 empty_result := jsonb_build_object('status','no_current_records','fact_kind',p_kind,
   'source_key',p_source,'content','{}'::jsonb,'expected_version',0,
   'school_fact_version',global_version,'label','当前时点无有效记录',
   'source_reference',NULL,'factual_at',p_factual_at,'valid_from',NULL,'valid_to',NULL,
   'source_published_at',NULL,'source_name',NULL,'source_trace_reference',NULL);
 IF p_creation THEN
   IF EXISTS (SELECT 1 FROM public.school_fact_change_lineage l
     WHERE l.school_id=p_school AND l.fact_kind=p_kind AND l.fact_source_key=p_source) THEN
     RAISE EXCEPTION 'F3 creation target already published' USING ERRCODE='22023';
   END IF;
   RETURN jsonb_build_object('status','available_creation','fact_kind',p_kind,
     'source_key',p_source,'content','{}'::jsonb,'expected_version',0,
     'school_fact_version',global_version,'label','已授权新事实候选',
     'source_reference','尚未正式发布','factual_at',p_factual_at,
     'valid_from',NULL,'valid_to',NULL,'source_published_at',NULL,'source_name',NULL,
     'source_trace_reference',NULL);
 END IF;
 -- Recorded corrections can be entered after a future version; factual time wins first.
 SELECT max((u.payload->>'effective_at')::timestamptz) INTO latest_at
 FROM public.school_fact_change_lineage l
 JOIN public.master_data_import_units u ON u.school_id=l.school_id AND u.id=l.unit_id
 JOIN public.master_data_import_batches b ON b.school_id=l.school_id AND b.id=l.batch_id
 WHERE l.school_id=p_school AND l.fact_kind=p_kind AND l.fact_source_key=p_source
 AND b.status='applied' AND (u.payload->>'effective_at')::timestamptz<=p_factual_at;
 SELECT max(l.target_version) INTO latest FROM public.school_fact_change_lineage l
 JOIN public.master_data_import_units u ON u.school_id=l.school_id AND u.id=l.unit_id
 JOIN public.master_data_import_batches b ON b.school_id=l.school_id AND b.id=l.batch_id
 WHERE l.school_id=p_school AND l.fact_kind=p_kind AND l.fact_source_key=p_source
 AND b.status='applied' AND (u.payload->>'effective_at')::timestamptz=latest_at;
 IF latest IS NULL THEN RETURN empty_result; END IF;
 SELECT count(*) INTO matches FROM public.school_fact_change_lineage l
 JOIN public.master_data_import_units u ON u.school_id=l.school_id AND u.id=l.unit_id
 JOIN public.master_data_import_batches b ON b.school_id=l.school_id AND b.id=l.batch_id
 WHERE l.school_id=p_school AND l.fact_kind=p_kind AND l.fact_source_key=p_source
 AND l.target_version=latest AND b.status='applied'
 AND (u.payload->>'effective_at')::timestamptz<=p_factual_at;
 IF matches>1 THEN
   RETURN empty_result||jsonb_build_object('status','conflict','expected_version',latest);
 ELSIF matches=0 THEN
   RAISE EXCEPTION 'F3 published content is unavailable' USING ERRCODE='55000';
 END IF;
 SELECT l.target_id,l.target_version,l.operation,l.batch_id,l.created_at AS published_at,
   (u.payload->>'effective_at')::timestamptz AS valid_from,
   (SELECT sr.source_ref FROM public.school_fact_sources sr WHERE sr.school_id=p_school
     AND sr.source_key='change-set:'||l.batch_id::text) AS source_trace_reference INTO STRICT chosen
 FROM public.school_fact_change_lineage l
 JOIN public.master_data_import_units u ON u.school_id=l.school_id AND u.id=l.unit_id
 JOIN public.master_data_import_batches b ON b.school_id=l.school_id AND b.id=l.batch_id
 WHERE l.school_id=p_school AND l.fact_kind=p_kind AND l.fact_source_key=p_source
 AND l.target_version=latest AND b.status='applied'
 AND (u.payload->>'effective_at')::timestamptz<=p_factual_at;
 empty_result := empty_result||jsonb_build_object('expected_version',chosen.target_version,
   'source_reference','publication:'||chosen.batch_id::text,
   'source_published_at',chosen.published_at,
   'source_trace_reference',chosen.source_trace_reference);
 IF chosen.operation IN ('end','revoke') THEN RETURN empty_result; END IF;
 IF chosen.operation NOT IN ('upsert','reactivate') THEN
   RAISE EXCEPTION 'F3 published operation is unavailable' USING ERRCODE='55000';
 END IF;
 IF p_kind='student' THEN
   SELECT count(*),jsonb_agg(jsonb_build_object('person_source_key',CASE WHEN
    'person_source_key'=ANY(p_fields) THEN (SELECT k.source_key FROM public.school_people k
    WHERE
    k.school_id=p_school AND k.id=f.person_id) END,
       'status',CASE WHEN 'status'=ANY(p_fields) THEN 'active' END))
   , NULL::timestamptz
   INTO matches,payloads,ends_at FROM public.students f
   WHERE f.school_id=p_school AND f.id=chosen.target_id AND f.version>=chosen.target_version;
   display_label := '学生';
 ELSIF p_kind='student_school_relationship' THEN
   SELECT count(*),jsonb_agg(jsonb_build_object('student_source_key',CASE WHEN
    'student_source_key'=ANY(p_fields) THEN (SELECT k.source_key FROM public.students k WHERE
    k.school_id=p_school AND k.id=f.student_id) END,
       'student_number',CASE WHEN 'student_number'=ANY(p_fields) THEN f.student_number END,
       'enrollment_status',CASE WHEN 'enrollment_status'=ANY(p_fields) THEN
    f.enrollment_status END,
       'residence_mode',CASE WHEN 'residence_mode'=ANY(p_fields) THEN f.residence_mode END,
       'primary_campus_source_key',CASE WHEN 'primary_campus_source_key'=ANY(p_fields) THEN
    (SELECT k.source_key FROM public.campus_locations k WHERE
    k.school_id=p_school AND k.id=f.primary_campus_location_id) END,
       'administrative_campus_source_key',CASE WHEN
    'administrative_campus_source_key'=ANY(p_fields) THEN (SELECT k.source_key FROM
    public.campus_locations k
    WHERE k.school_id=p_school AND
    k.id=f.administrative_campus_location_id) END))
   , min(f.valid_to)
   INTO matches,payloads,ends_at FROM public.student_school_relationships f
   WHERE f.school_id=p_school AND f.id=chosen.target_id AND f.version=chosen.target_version;
   display_label := '学生在校关系';
 ELSIF p_kind='student_contact' THEN
   SELECT count(*),jsonb_agg(jsonb_build_object('student_source_key',CASE WHEN
    'student_source_key'=ANY(p_fields) THEN (SELECT k.source_key FROM public.students k WHERE
    k.school_id=p_school AND k.id=f.student_id) END,
       'contact_person_source_key',CASE WHEN 'contact_person_source_key'=ANY(p_fields) THEN
    (SELECT k.source_key FROM public.school_people k WHERE
    k.school_id=p_school AND k.id=f.contact_person_id) END,
       'relationship_kind',CASE WHEN 'relationship_kind'=ANY(p_fields) THEN
    f.relationship_kind END,
       'priority',CASE WHEN 'priority'=ANY(p_fields) THEN f.priority END,
       'is_emergency',CASE WHEN 'is_emergency'=ANY(p_fields) THEN f.is_emergency
    END))
   , min(f.valid_to)
   INTO matches,payloads,ends_at FROM public.student_contact_relationships f
   WHERE f.school_id=p_school AND f.id=chosen.target_id AND f.version=chosen.target_version;
   display_label := '学生联系人';
 ELSIF p_kind='guardian_relationship' THEN
   SELECT count(*),jsonb_agg(jsonb_build_object('guardian_person_source_key',CASE WHEN
    'guardian_person_source_key'=ANY(p_fields) THEN (SELECT k.source_key FROM
    public.school_people k WHERE
    k.school_id=p_school AND k.id=f.guardian_person_id) END,
       'student_source_key',CASE WHEN 'student_source_key'=ANY(p_fields) THEN (SELECT
    k.source_key FROM public.students k WHERE
    k.school_id=p_school AND k.id=f.student_id) END,
       'relationship_kind',CASE WHEN 'relationship_kind'=ANY(p_fields) THEN
    f.relationship_kind END,
       'access_purpose',CASE WHEN 'access_purpose'=ANY(p_fields) THEN f.access_purpose END,
       'verification_source',CASE WHEN 'verification_source'=ANY(p_fields) THEN
    f.verification_source END,
       'verification_reference',CASE WHEN 'verification_reference'=ANY(p_fields) THEN
    f.verification_reference END,
       'review_of_relationship_source_key',CASE WHEN
    'review_of_relationship_source_key'=ANY(p_fields) THEN (SELECT DISTINCT k.fact_source_key
    FROM
    public.school_fact_change_lineage k WHERE k.school_id=p_school AND
    k.fact_kind='guardian_relationship' AND k.target_id=f.review_of_relationship_id) END,
       'status',CASE WHEN 'status'=ANY(p_fields) THEN CASE
    WHEN f.verified_at IS NULL THEN 'pending_verification' ELSE 'confirmed' END END))
   , min(f.valid_to)
   INTO matches,payloads,ends_at FROM public.guardian_relationships f
   WHERE f.school_id=p_school AND f.id=chosen.target_id AND f.version=chosen.target_version
    AND f.authority_version=1;
   display_label := '已发布家庭关系';
 ELSIF p_kind='person_region' THEN
   SELECT count(*),jsonb_agg(jsonb_build_object('person_source_key',CASE WHEN
    'person_source_key'=ANY(p_fields) THEN (SELECT k.source_key FROM public.school_people k
    WHERE
    k.school_id=p_school AND k.id=f.person_id) END,
       'district_code',CASE WHEN 'district_code'=ANY(p_fields) THEN f.district_code END,
       'district_name',CASE WHEN 'district_name'=ANY(p_fields) THEN f.district_name END,
       'region_purpose',CASE WHEN 'region_purpose'=ANY(p_fields) THEN f.region_purpose
    END))
   , min(f.valid_to)
   INTO matches,payloads,ends_at FROM public.person_regions f
   WHERE f.school_id=p_school AND f.id=chosen.target_id AND f.version=chosen.target_version;
   display_label := '区县资料';
 ELSIF p_kind='person' THEN
   SELECT count(*),jsonb_agg(jsonb_build_object('business_code',CASE WHEN
    'business_code'=ANY(p_fields) THEN f.business_code END,
       'display_name',CASE WHEN 'display_name'=ANY(p_fields) THEN n.display_name END,
       'person_kind',CASE WHEN 'person_kind'=ANY(p_fields) THEN f.person_kind END,
       'lifecycle_status',CASE WHEN 'lifecycle_status'=ANY(p_fields) THEN f.lifecycle_status
    END))
   , min(f.valid_to)
   INTO matches,payloads,ends_at FROM public.school_person_profile_versions f JOIN
    public.school_person_names n ON n.school_id=f.school_id AND n.person_id=f.person_id AND
    n.version=f.version AND n.status='confirmed'
   WHERE f.school_id=p_school AND f.person_id=chosen.target_id AND
    f.version=chosen.target_version;
   display_label := '人员';
 ELSIF p_kind='login_mapping' THEN
   SELECT count(*),jsonb_agg(jsonb_build_object('provider_code',CASE WHEN
    'provider_code'=ANY(p_fields) THEN f.provider_code END,
       'provider_tenant_key',CASE WHEN 'provider_tenant_key'=ANY(p_fields) THEN
    f.provider_tenant_key END,
       'external_subject',CASE WHEN 'external_subject'=ANY(p_fields) THEN f.external_subject
    END,
       'person_source_key',CASE WHEN 'person_source_key'=ANY(p_fields) THEN (SELECT
    k.source_key FROM public.school_people k WHERE
    k.school_id=p_school AND k.id=f.person_id) END,
       'status',CASE WHEN 'status'=ANY(p_fields) THEN f.status END))
   , min(f.effective_to)
   INTO matches,payloads,ends_at FROM public.account_person_binding_versions f
   WHERE f.school_id=p_school AND f.binding_id=chosen.target_id AND
    f.target_version=chosen.target_version;
   display_label := '登录映射';
 ELSIF p_kind='assignment' THEN
   SELECT count(*),jsonb_agg(jsonb_build_object('person_source_key',CASE WHEN
    'person_source_key'=ANY(p_fields) THEN (SELECT k.source_key FROM public.school_people k
    WHERE
    k.school_id=p_school AND k.id=f.person_id) END,
       'organization_source_key',CASE WHEN 'organization_source_key'=ANY(p_fields) THEN
    (SELECT k.source_key FROM public.organization_units k WHERE
    k.school_id=p_school AND k.id=f.organization_unit_id) END,
       'position_title',CASE WHEN 'position_title'=ANY(p_fields) THEN f.position_title END,
       'semantic_role_code',CASE WHEN 'semantic_role_code'=ANY(p_fields) THEN
    f.semantic_role_code END,
       'assignment_kind',CASE WHEN 'assignment_kind'=ANY(p_fields) THEN f.assignment_kind END,
       'appointment_source',CASE WHEN 'appointment_source'=ANY(p_fields) THEN
    f.appointment_source END,
       'approval_reference',CASE WHEN 'approval_reference'=ANY(p_fields) THEN
    f.approval_reference END,
       'status',CASE WHEN 'status'=ANY(p_fields) THEN f.status END,
       'valid_to',CASE WHEN 'valid_to'=ANY(p_fields) THEN f.planned_valid_to
    END))
   , min(LEAST(f.effective_to,f.planned_valid_to))
   INTO matches,payloads,ends_at FROM public.staff_assignment_versions f
   WHERE f.school_id=p_school AND f.assignment_id=chosen.target_id AND
    f.target_version=chosen.target_version;
   display_label := '任职';
 END IF;
 IF matches>1 THEN
   RETURN empty_result||jsonb_build_object('status','conflict');
 ELSIF matches=0 THEN
   -- An applied lineage without its preserved version is a source failure, not absence.
   RAISE EXCEPTION 'F3 published content is unavailable' USING ERRCODE='55000';
 END IF;
 IF ends_at IS NOT NULL AND p_factual_at>=ends_at THEN RETURN empty_result; END IF;
 SELECT jsonb_object_agg(k,v) INTO content
 FROM jsonb_each(payloads->0) AS authorized_fields(k,v) WHERE k=ANY(p_fields);
 IF p_kind='person' AND 'display_name'=ANY(p_fields) THEN
   display_label := display_label||' · '||COALESCE(content->>'display_name','姓名未知');
 END IF;
 RETURN jsonb_build_object('status','available','fact_kind',p_kind,'source_key',p_source,
   'content',content,'expected_version',chosen.target_version,
   'school_fact_version',global_version,'label',display_label,
   'source_reference','publication:'||chosen.batch_id::text,'factual_at',p_factual_at,
   'valid_from',chosen.valid_from,'valid_to',ends_at,
   'source_published_at',chosen.published_at,'source_name',NULL,
   'source_trace_reference',chosen.source_trace_reference);
END;
$f$;
REVOKE ALL ON FUNCTION
    app.school_fact_f3_staff_target(uuid,text,text,boolean,timestamptz,text[])
 FROM PUBLIC,campus_ai_runtime,campus_ai_school_fact_writer,campus_ai_migration,
 campus_ai_db_owner;
GRANT EXECUTE ON FUNCTION
    app.school_fact_f3_staff_target(uuid,text,text,boolean,timestamptz,text[])
 TO campus_ai_school_fact_reader;

CREATE FUNCTION app.school_fact_f3_staff_candidates(
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
         'administrative_campus_source_key'))
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
REVOKE ALL ON FUNCTION
 app.school_fact_f3_staff_candidates(uuid,text,text[],timestamptz,text[],text,text,integer)
 FROM PUBLIC,campus_ai_runtime,campus_ai_school_fact_writer,campus_ai_migration,
 campus_ai_db_owner;
GRANT EXECUTE ON FUNCTION
 app.school_fact_f3_staff_candidates(uuid,text,text[],timestamptz,text[],text,text,integer)
 TO campus_ai_school_fact_reader;
CREATE FUNCTION app.school_fact_f3_scope_is_current(
 p_school uuid,p_kind text,p_objects text[],p_as_of timestamptz
) RETURNS boolean LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog AS $f$
DECLARE
 object_key text; latest_version bigint; content_version bigint; metadata_count bigint;
 content_id uuid; coordinate_count bigint;
BEGIN
 IF SESSION_USER<>'campus_ai_school_fact_reader'
    OR p_school IS DISTINCT FROM app.require_school_database_anchor() THEN
   RAISE EXCEPTION 'F3 Reader denied' USING ERRCODE='28000';
 END IF;
 IF p_kind IS NULL OR p_kind NOT IN ('student','student_school_relationship','student_contact',
   'guardian_relationship','person_region','person','login_mapping','assignment')
    OR p_as_of IS NULL OR p_objects IS NULL OR cardinality(p_objects)>100
    OR array_position(p_objects,NULL) IS NOT NULL THEN RETURN false; END IF;
 IF NOT app_private.school_fact_identity_root_is_effective_at(
   p_school,'school',p_school,p_as_of) THEN RETURN false; END IF;
 FOREACH object_key IN ARRAY p_objects LOOP
   IF btrim(object_key)='' OR length(object_key)>255 THEN RETURN false; END IF;
   SELECT max(l.target_version) INTO latest_version
   FROM public.school_fact_change_lineage l
   JOIN public.master_data_import_batches b ON b.school_id=l.school_id AND b.id=l.batch_id
   WHERE l.school_id=p_school AND l.fact_kind=p_kind AND l.fact_source_key=object_key
     AND b.status='applied';
   IF latest_version IS NULL THEN RETURN false; END IF;
   SELECT count(*) INTO metadata_count
   FROM public.school_fact_change_lineage l
   JOIN public.master_data_import_batches b ON b.school_id=l.school_id AND b.id=l.batch_id
   JOIN public.master_data_import_units u ON u.school_id=l.school_id AND u.id=l.unit_id
   WHERE l.school_id=p_school AND l.fact_kind=p_kind AND l.fact_source_key=object_key
     AND b.status='applied' AND l.target_version=latest_version
     AND l.operation IN ('upsert','reactivate','end','revoke')
     AND u.payload->>'effective_at' IS NOT NULL;
   IF metadata_count<>1 THEN RETURN false; END IF;
   SELECT max(l.target_version) INTO content_version
   FROM public.school_fact_change_lineage l
   JOIN public.master_data_import_batches b ON b.school_id=l.school_id AND b.id=l.batch_id
   WHERE l.school_id=p_school AND l.fact_kind=p_kind AND l.fact_source_key=object_key
     AND b.status='applied' AND l.operation IN ('upsert','reactivate');
   IF content_version IS NULL THEN RETURN false; END IF;
   SELECT count(*) INTO metadata_count
   FROM public.school_fact_change_lineage l
   JOIN public.master_data_import_batches b ON b.school_id=l.school_id AND b.id=l.batch_id
   JOIN public.master_data_import_units u ON u.school_id=l.school_id AND u.id=l.unit_id
   WHERE l.school_id=p_school AND l.fact_kind=p_kind AND l.fact_source_key=object_key
     AND b.status='applied' AND l.target_version=content_version
     AND l.operation IN ('upsert','reactivate') AND u.payload->>'effective_at' IS NOT NULL;
   IF metadata_count<>1 THEN RETURN false; END IF;
   SELECT l.target_id INTO STRICT content_id FROM public.school_fact_change_lineage l
   JOIN public.master_data_import_batches b ON b.school_id=l.school_id AND b.id=l.batch_id
   WHERE l.school_id=p_school AND l.fact_kind=p_kind AND l.fact_source_key=object_key
     AND b.status='applied' AND l.target_version=content_version;
   -- A current duty names a formal object, including its ended/revoked historical versions.
   -- Business time remains in staff_target; current Staff/Grant/epoch checks stay in Thin.
   IF p_kind='student' THEN
     SELECT count(*) INTO coordinate_count FROM
       public.students f WHERE f.school_id=p_school AND f.id=content_id AND
       f.version>=content_version;
   ELSIF p_kind='student_school_relationship' THEN
     SELECT count(*) INTO coordinate_count FROM
       public.student_school_relationships f WHERE f.school_id=p_school AND f.id=content_id
       AND f.version=content_version;
   ELSIF p_kind='student_contact' THEN
     SELECT count(*) INTO coordinate_count FROM
       public.student_contact_relationships f WHERE f.school_id=p_school AND f.id=content_id
       AND f.version=content_version;
   ELSIF p_kind='guardian_relationship' THEN
     SELECT count(*) INTO coordinate_count FROM
       public.guardian_relationships f WHERE f.school_id=p_school AND f.id=content_id AND
       f.version=content_version AND f.authority_version=1;
   ELSIF p_kind='person_region' THEN
     SELECT count(*) INTO coordinate_count FROM
       public.person_regions f WHERE f.school_id=p_school AND f.id=content_id AND
       f.version=content_version;
   ELSIF p_kind='person' THEN
     SELECT count(*) INTO coordinate_count FROM
       public.school_person_profile_versions f JOIN public.school_person_names n ON
       n.school_id=f.school_id AND n.person_id=f.person_id AND n.version=f.version WHERE
       f.school_id=p_school AND f.person_id=content_id AND f.version=content_version;
   ELSIF p_kind='login_mapping' THEN
     SELECT count(*) INTO coordinate_count FROM
       public.account_person_binding_versions f WHERE f.school_id=p_school AND
       f.binding_id=content_id AND f.target_version=content_version;
   ELSIF p_kind='assignment' THEN
     SELECT count(*) INTO coordinate_count FROM
       public.staff_assignment_versions f WHERE f.school_id=p_school AND
       f.assignment_id=content_id AND f.target_version=content_version;
   END IF;
   IF coordinate_count<>1 THEN RETURN false; END IF;
 END LOOP;
 RETURN true;
END;
$f$;
REVOKE ALL ON FUNCTION app.school_fact_f3_scope_is_current(uuid,text,text[],timestamptz)
 FROM PUBLIC,campus_ai_runtime,campus_ai_school_fact_writer,campus_ai_migration,
 campus_ai_db_owner;
GRANT EXECUTE ON FUNCTION app.school_fact_f3_scope_is_current(uuid,text,text[],timestamptz)
 TO campus_ai_school_fact_reader;
"""

_LIST_SIGNATURE = "integer,bigint,text,timestamptz,text"
_BIND_SIGNATURE = (
    "uuid,integer,uuid,text,uuid,bigint,uuid,bigint,bigint,uuid,text,text,text,timestamptz,text"
)

FAMILY_IDENTITY_SQL = r"""
ALTER FUNCTION app.list_execution_context_options(integer,bigint,text,timestamptz,text)
 RENAME TO list_execution_context_options_pre_f3;
ALTER FUNCTION app.bind_execution_context(uuid,integer,uuid,text,uuid,bigint,uuid,bigint,bigint,
    uuid,text,text,text,timestamptz,text)
 RENAME TO bind_execution_context_pre_f3;
REVOKE ALL ON FUNCTION app.list_execution_context_options_pre_f3(integer,bigint,text,timestamptz,
    text)
 FROM PUBLIC,campus_ai_runtime,campus_ai_school_fact_reader,campus_ai_school_fact_writer,
    campus_ai_db_owner,campus_ai_migration,campus_ai_identity_definer;
REVOKE ALL ON FUNCTION app.bind_execution_context_pre_f3(uuid,integer,uuid,text,uuid,bigint,uuid,
    bigint,bigint,uuid,text,text,text,timestamptz,text)
 FROM PUBLIC,campus_ai_runtime,campus_ai_school_fact_reader,campus_ai_school_fact_writer,
    campus_ai_db_owner,campus_ai_migration,campus_ai_identity_definer;

CREATE FUNCTION app.list_execution_context_options(
 p_account_user_id integer,p_authorization_version bigint,p_session_nonce_hash text,
 p_proof_expires_at timestamptz,p_server_proof text)
RETURNS TABLE(context_kind text,context_id uuid,context_version bigint,school_id uuid,person_id
    uuid,display_label text)
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog AS $f$
BEGIN
 RETURN QUERY
 SELECT o.context_kind,o.context_id,o.context_version,o.school_id,o.person_id,
   CASE WHEN o.context_kind='guardian' THEN split_part(o.display_label,' · ',1)||' · '||
     CASE WHEN r.relationship_kind='verified_guardian' THEN '监护人' ELSE '认可照护人' END||' · '||
     CASE WHEN n.matches=1 THEN n.display_name WHEN n.matches>1 THEN '孩子姓名有冲突'
       ELSE '当前范围未查得孩子姓名' END||' · '||
     CASE WHEN e.matches>1 OR c.matches>1 THEN '校区资料有冲突'
       WHEN c.matches=1 THEN c.display_name ELSE '当前范围未查得校区' END||
     CASE WHEN r.relationship_kind='verified_guardian' AND e.matches=1
       AND e.student_number IS NOT NULL THEN ' · 学号末尾'||right(e.student_number,4) ELSE '' END
   ELSE o.display_label END
 FROM app.list_execution_context_options_pre_f3(p_account_user_id,p_authorization_version,
      p_session_nonce_hash,p_proof_expires_at,p_server_proof) o
 LEFT JOIN public.guardian_relationships r ON o.context_kind='guardian'
   AND r.school_id=o.school_id AND r.id=o.context_id
 LEFT JOIN public.students s ON s.school_id=r.school_id AND s.id=r.student_id
 LEFT JOIN LATERAL (
   SELECT count(*) AS matches,min(nm.display_name) AS display_name
   FROM public.school_person_names nm WHERE nm.school_id=s.school_id AND nm.person_id=s.person_id
   AND nm.status='confirmed' AND nm.valid_from<=statement_timestamp()
   AND (nm.valid_to IS NULL OR statement_timestamp()<nm.valid_to)
 ) n ON true
 LEFT JOIN LATERAL (
   SELECT count(*) AS matches,
     CASE WHEN count(*)=1 THEN (array_agg(sr.primary_campus_location_id))[1] END AS campus_id,
     CASE WHEN r.relationship_kind='verified_guardian' AND count(*)=1
       THEN min(sr.student_number) END AS student_number
   FROM public.student_school_relationships sr WHERE sr.school_id=s.school_id
   AND sr.student_id=s.id AND sr.valid_from<=statement_timestamp()
   AND (sr.valid_to IS NULL OR statement_timestamp()<sr.valid_to)
 ) e ON true
 LEFT JOIN LATERAL (
   SELECT count(*) AS matches,min(cv.display_name) AS display_name
   FROM public.campus_location_versions cv WHERE cv.school_id=s.school_id
   AND cv.location_id=e.campus_id AND cv.status='confirmed'
   AND cv.valid_from<=statement_timestamp()
   AND (cv.valid_to IS NULL OR statement_timestamp()<cv.valid_to)
 ) c ON true
 WHERE o.context_kind='assignment' OR (o.context_kind='guardian' AND
   app_private.family_relationship_is_authoritative(o.school_id,o.person_id,o.context_id,
    o.context_version,statement_timestamp()));
END;
$f$;

CREATE FUNCTION app.bind_execution_context(
 p_school_id uuid,p_account_user_id integer,p_person_id uuid,p_context_kind text,
 p_assignment_id uuid,p_assignment_version bigint,p_guardian_relationship_id uuid,
 p_guardian_relationship_version bigint,p_authorization_version bigint,p_request_id uuid,
 p_context_purpose text,p_idempotency_key_hash text,p_session_nonce_hash text,
 p_proof_expires_at timestamptz,p_server_proof text)
RETURNS void LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path=pg_catalog AS $f$
BEGIN
 PERFORM app.bind_execution_context_pre_f3(p_school_id,p_account_user_id,p_person_id,
    p_context_kind,
  p_assignment_id,p_assignment_version,p_guardian_relationship_id,p_guardian_relationship_version,
  p_authorization_version,p_request_id,p_context_purpose,p_idempotency_key_hash,
    p_session_nonce_hash,
  p_proof_expires_at,p_server_proof);
 IF p_context_kind='guardian' AND NOT app_private.family_relationship_is_authoritative(
  p_school_id,p_person_id,p_guardian_relationship_id,p_guardian_relationship_version,
    statement_timestamp()) THEN
   RAISE EXCEPTION 'formal guardian context is not current' USING ERRCODE='28000';
 END IF;
END;
$f$;
REVOKE ALL ON FUNCTION app.list_execution_context_options(integer,bigint,text,timestamptz,text)
    FROM PUBLIC;
REVOKE ALL ON FUNCTION app.bind_execution_context(uuid,integer,uuid,text,uuid,bigint,uuid,bigint,
    bigint,uuid,text,text,text,timestamptz,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app.list_execution_context_options(integer,bigint,text,timestamptz,
    text) TO campus_ai_runtime;
GRANT EXECUTE ON FUNCTION app.bind_execution_context(uuid,integer,uuid,text,uuid,bigint,uuid,
    bigint,bigint,uuid,text,text,text,timestamptz,text)
 TO campus_ai_runtime,campus_ai_school_fact_writer,campus_ai_db_owner;
"""


def upgrade() -> None:
    _structures()
    _grants()
    op.execute("GRANT CREATE ON SCHEMA app,app_private TO campus_ai_context_definer")
    op.execute("SET LOCAL ROLE campus_ai_context_definer")
    op.execute(FAMILY_AUTHORITY_SQL)
    op.execute(FAMILY_READER_SQL)
    op.execute(FAMILY_STAFF_SQL)
    op.execute(FAMILY_IDENTITY_SQL)
    op.execute("SET LOCAL ROLE campus_ai_db_owner")
    op.execute("REVOKE CREATE ON SCHEMA app,app_private FROM campus_ai_context_definer")


def _structures() -> None:
    for name, kind in (
        ("authority_version", sa.Integer()),
        ("access_purpose", sa.String(40)),
        ("requested_by_person_id", sa.Uuid()),
        ("requested_by_assignment_id", sa.Uuid()),
        ("requested_at", sa.DateTime(timezone=True)),
        ("verified_by_person_id", sa.Uuid()),
        ("verified_by_assignment_id", sa.Uuid()),
        ("review_of_relationship_id", sa.Uuid()),
        ("revoked_at", sa.DateTime(timezone=True)),
        ("source_id", sa.Uuid()),
        ("import_batch_id", sa.Uuid()),
    ):
        op.add_column("guardian_relationships", sa.Column(name, kind, nullable=True))
    op.alter_column("guardian_relationships", "verified_at", nullable=True)
    op.drop_constraint("ck_guardian_relationships_status", "guardian_relationships", type_="check")
    op.create_check_constraint(
        "ck_guardian_relationships_status",
        "guardian_relationships",
        "status IN ('draft','pending_verification','confirmed','rejected','ended','revoked')",
    )
    op.create_check_constraint(
        "ck_guardian_relationships_authority_version",
        "guardian_relationships",
        "authority_version IS NULL OR authority_version=1",
    )
    op.create_check_constraint(
        "ck_guardian_relationships_formal_shape",
        "guardian_relationships",
        "authority_version IS NULL OR (access_purpose IS NOT NULL AND "
        "requested_by_person_id IS NOT NULL AND requested_by_assignment_id IS NOT NULL AND "
        "requested_at IS NOT NULL AND source_id IS NOT NULL AND requested_by_person_id <> "
        "guardian_person_id AND ((status = 'pending_verification' AND verified_by_person_id "
        "IS NULL AND verified_by_assignment_id IS NULL AND verified_at IS NULL) OR (status "
        "= 'confirmed' AND verified_by_person_id IS NOT NULL AND verified_by_assignment_id "
        "IS NOT NULL AND verified_at IS NOT NULL AND verified_by_person_id <> "
        "guardian_person_id AND verified_by_person_id <> requested_by_person_id AND "
        "verified_by_assignment_id <> requested_by_assignment_id) OR (status IN "
        "('rejected', 'ended', 'revoked') AND ((verified_by_person_id IS NULL AND "
        "verified_by_assignment_id IS NULL AND verified_at IS NULL) OR "
        "(verified_by_person_id IS NOT NULL AND verified_by_assignment_id IS NOT NULL AND "
        "verified_at IS NOT NULL AND verified_by_person_id <> guardian_person_id AND "
        "verified_by_person_id <> requested_by_person_id AND verified_by_assignment_id <> "
        "requested_by_assignment_id)))))",
    )
    for suffix, column, target in (
        ("requester", "requested_by_person_id", "school_people"),
        ("verifier", "verified_by_person_id", "school_people"),
        ("requester_assignment", "requested_by_assignment_id", "staff_assignments"),
        ("verifier_assignment", "verified_by_assignment_id", "staff_assignments"),
        ("review_of", "review_of_relationship_id", "guardian_relationships"),
        ("source", "source_id", "school_fact_sources"),
        ("import_batch", "import_batch_id", "master_data_import_batches"),
    ):
        op.create_foreign_key(
            "fk_guardian_relationships_" + suffix,
            "guardian_relationships",
            target,
            ["school_id", column],
            ["school_id", "id"],
            ondelete="RESTRICT",
        )
    op.create_table(
        "person_regions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("school_id", sa.Uuid(), nullable=False),
        sa.Column("person_id", sa.Uuid(), nullable=False),
        sa.Column("district_code", sa.String(40), nullable=False),
        sa.Column("district_name", sa.String(120), nullable=False),
        sa.Column("region_purpose", sa.String(40), nullable=False),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_to", sa.DateTime(timezone=True)),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column("version", sa.BigInteger(), nullable=False, server_default="1"),
        sa.Column("source_id", sa.Uuid(), nullable=False),
        sa.Column("import_batch_id", sa.Uuid()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("id", name="pk_person_regions"),
        sa.UniqueConstraint("school_id", "id", name="uq_person_regions_school_id_id"),
        sa.CheckConstraint(
            "valid_to IS NULL OR valid_to > valid_from", name="ck_person_regions_valid_interval"
        ),
        sa.CheckConstraint("version>=1", name="ck_person_regions_version_positive"),
        sa.CheckConstraint(
            "btrim(district_code)<>'' AND btrim(district_name)<>''",
            name="ck_person_regions_district_nonempty",
        ),
        sa.CheckConstraint("region_purpose='student_profile'", name="ck_person_regions_purpose"),
    )
    for suffix, column, target in (
        ("person", "person_id", "school_people"),
        ("source", "source_id", "school_fact_sources"),
        ("import_batch", "import_batch_id", "master_data_import_batches"),
    ):
        op.create_foreign_key(
            "fk_person_regions_" + suffix,
            "person_regions",
            target,
            ["school_id", column],
            ["school_id", "id"],
            ondelete="RESTRICT",
        )
    op.execute(
        "ALTER TABLE public.person_regions ADD CONSTRAINT excl_person_regions_period "
        "EXCLUDE USING gist (school_id WITH =,person_id WITH =,region_purpose "
        "WITH =,tstzrange(valid_from,valid_to,'[)') WITH &&)"
    )


def _grants() -> None:
    # Nested SECURITY DEFINER calls keep the real Reader SESSION_USER guard.
    op.execute(
        "GRANT EXECUTE ON FUNCTION app.school_fact_current_version(uuid) "
        "TO campus_ai_context_definer"
    )
    for table in ("guardian_relationships", "person_regions"):
        op.execute(f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE public.{table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"REVOKE ALL ON public.{table} "
            "FROM PUBLIC,campus_ai_runtime,campus_ai_school_fact_reader"
        )
        op.execute(f"GRANT SELECT,INSERT,UPDATE ON public.{table} TO campus_ai_school_fact_writer")
        op.execute(
            f"CREATE POLICY f3_family_writer ON public.{table} FOR ALL "
            "TO campus_ai_school_fact_writer "
            "USING (school_id=app.require_school_context()) WITH CHECK "
            "(school_id=app.require_school_context())"
        )
    op.execute(
        "CREATE POLICY f3_family_owner ON public.person_regions FOR ALL TO "
        "campus_ai_db_owner,campus_ai_migration USING (true) WITH CHECK (true)"
    )
    op.execute(
        "CREATE POLICY f3_family_anchor ON public.person_regions AS RESTRICTIVE FOR ALL TO PUBLIC "
        "USING (school_id=app.require_school_database_anchor()) WITH CHECK "
        "(school_id=app.require_school_database_anchor())"
    )
    columns = {
        "guardian_relationships": (
            "id,school_id,guardian_person_id,student_id,version,authority_version,"
            "status,revoked_at,verified_by_person_id,verified_by_assignment_id,"
            "verification_source,verification_reference,review_of_relationship_id,"
            "verified_at,source_id,relationship_kind,access_purpose,valid_from,"
            "valid_to"
        ),
        "person_regions": (
            "id,school_id,person_id,district_code,district_name,region_purpose,"
            "valid_from,valid_to,revoked_at,version"
        ),
        "students": "id,school_id,person_id,version,source_key,status",
        "school_people": "id,school_id,source_key",
        "organization_units": "id,school_id,source_key",
        "campus_locations": "id,school_id,source_key",
        "school_fact_sources": "school_id,source_key,source_ref,source_kind",
        "master_data_import_units": "id,school_id,payload",
        "student_contact_relationships": (
            "id,school_id,student_id,contact_person_id,relationship_kind,priority,"
            "is_emergency,valid_from,valid_to,version"
        ),
        "school_person_profile_versions": (
            "school_id,person_id,business_code,person_kind,lifecycle_status,version,"
            "valid_from,valid_to"
        ),
        "account_person_binding_versions": (
            "school_id,binding_id,target_version,person_id,provider_code,"
            "provider_tenant_key,external_subject,status,effective_from,effective_to,"
            "revoked_at"
        ),
        "staff_assignment_versions": (
            "school_id,assignment_id,target_version,person_id,organization_unit_id,"
            "position_title,semantic_role_code,assignment_kind,appointment_source,"
            "approval_reference,status,effective_from,effective_to,planned_valid_to,"
            "revoked_at"
        ),
        "school_person_names": (
            "school_id,person_id,display_name,status,valid_from,valid_to,version"
        ),
        "student_school_relationships": (
            "id,school_id,student_id,student_number,enrollment_status,"
            "residence_mode,primary_campus_location_id,administrative_campus_location_id,valid_from,valid_to,version"
        ),
        "campus_location_versions": (
            "school_id,location_id,display_name,status,valid_from,valid_to,version"
        ),
        "school_fact_change_lineage": (
            "school_id,fact_kind,fact_source_key,batch_id,unit_id,target_id,target_version,operation,created_at"
        ),
        "master_data_import_batches": "id,school_id,source_kind,status",
    }
    for table, projection in columns.items():
        op.execute(f"GRANT SELECT ({projection}) ON public.{table} TO campus_ai_context_definer")
        op.execute(
            f"CREATE POLICY f3_family_context_read ON public.{table} FOR SELECT "
            "TO campus_ai_context_definer USING (school_id=app.require_school_database_anchor())"
        )


def downgrade() -> None:
    # Downgrade cannot silently erase formal family authority or optional region facts.
    raise RuntimeError("F3 downgrade requires an explicitly reviewed data-preserving plan")
