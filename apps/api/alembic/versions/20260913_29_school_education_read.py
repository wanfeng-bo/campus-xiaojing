# ruff: noqa: E501
"""Finite education read projections, with original family proof and Writer correction evidence."""

from alembic import op

revision = "20260913_29"
down_revision = "20260913_28"
branch_labels = None
depends_on = None

STAFF_SQL = r"""
CREATE FUNCTION app_private.f4_allowed_fields(p_kind text) RETURNS text[]
LANGUAGE sql IMMUTABLE SET search_path=pg_catalog AS $f$ SELECT CASE p_kind
WHEN 'class_lineage' THEN ARRAY['basis_reference','class_source_key','lineage_kind','predecessor_class_source_key','valid_to']::text[]
WHEN 'class_location' THEN ARRAY['basis_reference','class_source_key','location_purpose','location_source_key','valid_to']::text[]
WHEN 'class_placement' THEN ARRAY['basis_reference','calendar_source_key','campus_source_key','class_number','class_source_key','grade_source_key','valid_to']::text[]
WHEN 'class_position_definition' THEN ARRAY['allow_concurrent','allow_multiple','basis_reference','business_code','class_source_key','display_name','max_holders','valid_to']::text[]
WHEN 'class_teacher_assignment' THEN ARRAY['acting_for_role','assignment_source_key','basis_reference','class_source_key','duty_scope','teacher_role','valid_to']::text[]
WHEN 'education_calendar_rule' THEN ARRAY['basis_reference','campus_source_key','day','display_name','end_cycle','grade_source_keys','month','stage_source_key','start_cycle','timezone_name','valid_to']::text[]
WHEN 'education_class' THEN ARRAY['basis_reference','business_code','display_name','organization_source_key','primary_classroom_limit','short_name','teacher_mode','teacher_role_limits','valid_to']::text[]
WHEN 'education_cohort' THEN ARRAY['academic_year','basis_reference','business_code','cohort_kind','display_name','stage_source_key','valid_to']::text[]
WHEN 'education_grade' THEN ARRAY['basis_reference','business_code','display_name','grade_order','stage_source_key','valid_to']::text[]
WHEN 'education_grade_group' THEN ARRAY['academic_year','basis_reference','calendar_source_key','campus_source_key','display_name','grade_source_key','organization_source_key','valid_to']::text[]
WHEN 'education_offering' THEN ARRAY['basis_reference','campus_source_key','stage_source_key','valid_to']::text[]
WHEN 'education_stage' THEN ARRAY['basis_reference','business_code','display_name','valid_to']::text[]
WHEN 'education_term' THEN ARRAY['academic_year','basis_reference','calendar_source_key','display_name','valid_to']::text[]
WHEN 'student_class_membership' THEN ARRAY['basis_reference','class_source_key','membership_status','school_relationship_source_key','student_source_key','valid_to']::text[]
WHEN 'student_class_position' THEN ARRAY['basis_reference','class_source_key','position_source_key','student_source_key','valid_to']::text[]
WHEN 'student_cohort_membership' THEN ARRAY['basis_reference','cohort_source_key','student_source_key','valid_to']::text[]
WHEN 'student_grade_placement' THEN ARRAY['basis_reference','calendar_source_key','grade_source_key','student_source_key','valid_to']::text[]
ELSE NULL::text[] END; $f$;
REVOKE ALL ON FUNCTION app_private.f4_allowed_fields(text) FROM PUBLIC,
 campus_ai_runtime,campus_ai_school_fact_reader,campus_ai_school_fact_writer,campus_ai_migration,campus_ai_db_owner;

CREATE FUNCTION app.school_fact_f4_scope_is_current(
 p_school uuid,p_kind text,p_objects text[],p_as_of timestamptz)
RETURNS boolean LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog AS $f$
DECLARE matched bigint;
BEGIN
 IF SESSION_USER<>'campus_ai_school_fact_reader'
    OR p_school IS DISTINCT FROM app.require_school_database_anchor() THEN
  RAISE EXCEPTION 'education Reader denied' USING ERRCODE='28000';
 END IF;
 IF p_as_of IS NULL OR p_objects IS NULL OR cardinality(p_objects)>100
    OR array_position(p_objects,NULL) IS NOT NULL
    OR app_private.f4_allowed_fields(p_kind) IS NULL
    OR NOT app_private.school_fact_identity_root_is_effective_at(p_school,'school',p_school,p_as_of)
    OR EXISTS(SELECT 1 FROM unnest(p_objects) k WHERE btrim(k)='' OR length(k)>255)
    OR cardinality(p_objects)<>(SELECT count(DISTINCT k) FROM unnest(p_objects) k)
 THEN RETURN false; END IF;
 SELECT count(*) INTO matched FROM public.school_education_facts f
 WHERE f.school_id=p_school AND f.kind=p_kind AND f.source_key=ANY(p_objects)
 AND (SELECT count(*) FROM public.school_fact_change_lineage l
      JOIN public.master_data_import_batches b ON b.school_id=l.school_id AND b.id=l.batch_id
      WHERE l.school_id=f.school_id AND l.fact_kind=f.kind AND l.fact_source_key=f.source_key
      AND l.target_id=f.id AND l.target_version=f.version AND b.status='applied'
      AND l.operation IN ('upsert','correct','cancel','end','revoke'))=1
 AND NOT EXISTS(SELECT 1 FROM public.school_fact_change_lineage l
      JOIN public.master_data_import_batches b ON b.school_id=l.school_id AND b.id=l.batch_id
      WHERE l.school_id=f.school_id AND l.fact_kind=f.kind AND l.fact_source_key=f.source_key
      AND l.target_version>f.version AND b.status='applied');
 RETURN matched=cardinality(p_objects);
END;
$f$;
REVOKE ALL ON FUNCTION app.school_fact_f4_scope_is_current(uuid,text,text[],timestamptz) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app.school_fact_f4_scope_is_current(uuid,text,text[],timestamptz)
 TO campus_ai_school_fact_reader;

CREATE FUNCTION app_private.f4_target_records(
 p_school uuid,p_kind text,p_objects text[],p_fields text[],p_as_of timestamptz,p_known_at timestamptz,p_filter jsonb DEFAULT '{}')
RETURNS jsonb LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog AS $f$
WITH requested AS (SELECT unnest(p_objects) AS source_key), roots AS MATERIALIZED (
 SELECT f.id,f.school_id,f.kind,f.source_key,f.version
 FROM public.school_education_facts f
 WHERE f.school_id=p_school AND f.kind=p_kind AND f.source_key=ANY(p_objects)
), publication AS MATERIALIZED (
 SELECT l.target_id,l.target_version,l.operation,l.batch_id,l.created_at,
 row_number() OVER(PARTITION BY l.target_id ORDER BY l.target_version DESC) AS rn
 FROM public.school_fact_change_lineage l
 JOIN roots f ON f.school_id=l.school_id AND f.id=l.target_id AND f.kind=l.fact_kind
 AND f.source_key=l.fact_source_key
 JOIN public.master_data_import_batches b ON b.school_id=l.school_id AND b.id=l.batch_id
 WHERE b.status='applied' AND (p_known_at IS NULL OR l.created_at<=p_known_at)
), view_rows AS MATERIALIZED (
 SELECT v.fact_id,v.revision,v.valid_from,v.valid_to,v.recorded_at,v.superseded_at,
 v.source_id,v.import_batch_id,v.cause_source_key,v.cause_version,v.payload AS raw_payload,
 COALESCE((SELECT jsonb_object_agg(e.key,e.value) FROM jsonb_each(v.payload || jsonb_build_object('valid_to',v.valid_to)) e
           WHERE e.key=ANY(p_fields)),'{}'::jsonb) AS projected
 FROM public.school_education_versions v JOIN roots f ON f.school_id=v.school_id AND f.id=v.fact_id
 JOIN public.master_data_import_batches b ON b.school_id=v.school_id AND b.id=v.import_batch_id
 WHERE b.status='applied' AND NOT EXISTS(SELECT 1 FROM jsonb_each(p_filter) fl
 WHERE NOT (CASE WHEN jsonb_typeof(fl.value)='array' THEN fl.value ? (v.payload->>fl.key)
 ELSE fl.value = v.payload->fl.key END) OR v.payload->fl.key IS NULL) AND
 ((p_known_at IS NULL AND v.superseded_at IS NULL) OR
  (p_known_at IS NOT NULL AND v.recorded_at<=p_known_at AND
   (v.superseded_at IS NULL OR p_known_at<v.superseded_at)))
), aggregated AS (
 SELECT v.fact_id,
 count(*) FILTER(WHERE v.valid_from<=p_as_of AND (v.valid_to IS NULL OR p_as_of<v.valid_to)) AS active_count,
 min(boundary.value) FILTER(WHERE boundary.value>p_as_of) AS next_boundary,
 jsonb_agg(jsonb_build_object('revision',v.revision,'effective_at',v.valid_from,
 'valid_to',v.valid_to,'recorded_at',v.recorded_at,'superseded_at',v.superseded_at,
 'cause_source_key',v.cause_source_key,'cause_version',v.cause_version,
 'causal_prior',causal.proof,
 'content',v.projected,'source',jsonb_build_object('source_key',s.source_key,
 'source_kind',s.source_kind,'source_ref',s.source_ref,'batch_id',v.import_batch_id,
 'publication_reference','publication:'||v.import_batch_id::text)) ORDER BY v.valid_from) AS segments
 FROM view_rows v JOIN public.school_fact_sources s ON s.school_id=p_school AND s.id=v.source_id
 LEFT JOIN LATERAL (
  SELECT jsonb_build_object('effective_at',old.valid_from,'valid_to',old.valid_to,
    'revision',old.revision,'ended_target_version',(SELECT f.version FROM roots f WHERE f.id=v.fact_id),
    'content',COALESCE((SELECT jsonb_object_agg(e.key,e.value) FROM jsonb_each(old.payload||jsonb_build_object('valid_to',old.valid_to)) e WHERE e.key=ANY(p_fields)),'{}')) proof
  FROM public.school_education_versions ended JOIN public.school_education_versions old
   ON old.school_id=ended.school_id AND old.fact_id=ended.fact_id AND old.revision=ended.revision-1 AND old.valid_from=ended.valid_from
  JOIN public.master_data_import_batches eb ON eb.school_id=ended.school_id AND eb.id=ended.import_batch_id AND eb.status='applied'
  JOIN public.master_data_import_batches ob ON ob.school_id=old.school_id AND ob.id=old.import_batch_id AND ob.status='applied'
  WHERE ended.school_id=p_school AND ended.fact_id=v.fact_id AND v.valid_to IS NOT NULL
   AND ended.cause_source_key=v.cause_source_key AND ended.cause_version=v.cause_version
   AND ended.valid_from=v.valid_from AND ended.valid_to=v.valid_to
   AND ended.payload-'valid_to'=v.raw_payload-'valid_to'
   AND old.payload-'valid_to'=v.raw_payload-'valid_to'
   AND (old.valid_to IS NULL OR old.valid_to>ended.valid_to)
  ORDER BY ended.revision LIMIT 1
 ) causal ON true
 LEFT JOIN LATERAL (SELECT min(x) AS value FROM unnest(ARRAY[v.valid_from,v.valid_to]) x
                    WHERE x>p_as_of) boundary ON true
 GROUP BY v.fact_id
), prior AS (
 SELECT v.fact_id,jsonb_agg(jsonb_build_object('revision',v.revision,
 'effective_at',v.valid_from,'valid_to',v.valid_to,'recorded_at',v.recorded_at,
 'content',COALESCE((SELECT jsonb_object_agg(e.key,e.value)
 FROM jsonb_each(v.payload || jsonb_build_object('valid_to',v.valid_to)) e
 WHERE e.key=ANY(p_fields)),'{}')) ORDER BY v.valid_from) AS segments
 FROM public.school_education_versions v JOIN roots f ON f.school_id=v.school_id AND f.id=v.fact_id
 JOIN publication pub ON pub.target_id=f.id AND pub.rn=1
 JOIN public.master_data_import_batches b ON b.school_id=v.school_id AND b.id=v.import_batch_id
 WHERE b.status='applied' AND v.revision=(SELECT max(l.target_version)
 FROM public.school_fact_change_lineage l JOIN public.master_data_import_batches lb
 ON lb.school_id=l.school_id AND lb.id=l.batch_id
 WHERE l.school_id=f.school_id AND l.target_id=f.id AND l.fact_kind=f.kind
 AND l.fact_source_key=f.source_key AND l.target_version<pub.target_version AND lb.status='applied')
 GROUP BY v.fact_id
)
SELECT COALESCE(jsonb_agg(jsonb_build_object('fact_kind',p_kind,'source_key',r.source_key,
 'version',COALESCE(f.version,0),'status',CASE WHEN f.id IS NULL OR pub.target_id IS NULL
 THEN 'not_recorded' WHEN pub.operation='cancel' AND COALESCE(jsonb_array_length(a.segments),0)=0 THEN 'cancelled'
 WHEN COALESCE(a.active_count,0)>1 THEN 'conflict'
 WHEN COALESCE(a.active_count,0)=1 THEN 'available' ELSE 'no_current_records' END,
 'segments',COALESCE(a.segments,'[]'::jsonb),'prior_segments',COALESCE(prior.segments,'[]'::jsonb),'latest_operation',pub.operation,'next_valid_boundary',a.next_boundary,
 'source_published_at',pub.created_at,'publication_reference',
 CASE WHEN pub.batch_id IS NULL THEN NULL ELSE 'publication:'||pub.batch_id::text END)
 ORDER BY r.source_key),'[]'::jsonb)
FROM requested r LEFT JOIN roots f ON f.source_key=r.source_key
LEFT JOIN publication pub ON pub.target_id=f.id AND pub.rn=1
LEFT JOIN aggregated a ON a.fact_id=f.id
LEFT JOIN prior ON prior.fact_id=f.id
WHERE p_filter='{}'::jsonb OR a.fact_id IS NOT NULL;
$f$;
REVOKE ALL ON FUNCTION app_private.f4_target_records(uuid,text,text[],text[],timestamptz,timestamptz,jsonb)
 FROM PUBLIC,campus_ai_runtime,campus_ai_school_fact_reader,campus_ai_school_fact_writer,campus_ai_migration,campus_ai_db_owner;

CREATE FUNCTION app.school_fact_f4_targets(
 p_school uuid,p_kind text,p_objects text[],p_fields text[],p_as_of timestamptz,
 p_known_at timestamptz DEFAULT NULL,p_filter jsonb DEFAULT '{}')
RETURNS jsonb LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog AS $f$
DECLARE records jsonb;
BEGIN
 IF SESSION_USER<>'campus_ai_school_fact_reader'
    OR p_school IS DISTINCT FROM app.require_school_database_anchor() THEN
  RAISE EXCEPTION 'education Reader denied' USING ERRCODE='28000';
 END IF;
 IF p_as_of IS NULL OR p_objects IS NULL OR cardinality(p_objects)>100
    OR array_position(p_objects,NULL) IS NOT NULL OR p_fields IS NULL
    OR cardinality(p_fields)=0 OR array_position(p_fields,NULL) IS NOT NULL
    OR app_private.f4_allowed_fields(p_kind) IS NULL
    OR NOT (p_fields <@ app_private.f4_allowed_fields(p_kind))
    OR p_known_at>statement_timestamp()
    OR cardinality(p_objects)<>(SELECT count(DISTINCT k) FROM unnest(p_objects) k)
    OR EXISTS(SELECT 1 FROM unnest(p_objects) k WHERE btrim(k)='' OR length(k)>255) THEN
  RAISE EXCEPTION 'invalid finite education projection' USING ERRCODE='22023';
 END IF;
 IF p_filter IS NULL OR jsonb_typeof(p_filter)<>'object' OR
 EXISTS(SELECT 1 FROM jsonb_each(p_filter) fl WHERE
 NOT fl.key=ANY(ARRAY['class_source_key','student_source_key','campus_source_key','stage_source_key',
 'calendar_source_key','cohort_source_key','position_source_key','assignment_source_key','display_name',
 'predecessor_class_source_key','school_relationship_source_key','organization_source_key',
 'location_source_key','grade_source_key'])
 OR NOT fl.key=ANY(p_fields) OR jsonb_typeof(fl.value) NOT IN ('string','array')) THEN
  RAISE EXCEPTION 'invalid education filter' USING ERRCODE='22023';
 END IF;
 IF EXISTS(SELECT 1 FROM jsonb_each(p_filter) fl CROSS JOIN LATERAL
 jsonb_array_elements(CASE WHEN jsonb_typeof(fl.value)='array' THEN fl.value ELSE jsonb_build_array(fl.value) END) val
 WHERE jsonb_typeof(val)<>'string'
 OR (fl.key='display_name' AND (length(val#>>'{}') NOT BETWEEN 1 AND 200 OR btrim(val#>>'{}')=''))
 OR (fl.key<>'display_name' AND (length(val#>>'{}') NOT BETWEEN 3 AND 255
 OR (val#>>'{}') !~ '^[a-z0-9][a-z0-9._:-]{2,254}$')))
 OR EXISTS(SELECT 1 FROM jsonb_each(p_filter) fl WHERE jsonb_typeof(fl.value)='array'
 AND jsonb_array_length(fl.value) NOT BETWEEN 1 AND 100) THEN
  RAISE EXCEPTION 'invalid education filter' USING ERRCODE='22023';
 END IF;
 IF NOT app_private.school_fact_identity_root_is_effective_at(
   p_school,'school',p_school,statement_timestamp()) THEN
  RAISE EXCEPTION 'inactive school' USING ERRCODE='28000';
 END IF;
 -- Missing keys may be explicit creation candidates; existing coordinates must be formal.
 IF NOT app.school_fact_f4_scope_is_current(p_school,p_kind,
  ARRAY(SELECT f.source_key FROM public.school_education_facts f WHERE f.school_id=p_school
        AND f.kind=p_kind AND f.source_key=ANY(p_objects)),statement_timestamp()) THEN
  RAISE EXCEPTION 'education publication evidence unavailable' USING ERRCODE='55000';
 END IF;
 records := app_private.f4_target_records(p_school,p_kind,p_objects,p_fields,p_as_of,p_known_at,p_filter);
 RETURN jsonb_build_object('status',CASE WHEN jsonb_array_length(records)=0 THEN 'no_current_records' ELSE 'available' END,'school_fact_version',
 app.school_fact_current_version(p_school),'records',records,'as_of',p_as_of,'known_at',p_known_at);
END;
$f$;
REVOKE ALL ON FUNCTION app.school_fact_f4_targets(uuid,text,text[],text[],timestamptz,timestamptz,jsonb)
 FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app.school_fact_f4_targets(uuid,text,text[],text[],timestamptz,timestamptz,jsonb)
 TO campus_ai_school_fact_reader;
"""

FAMILY_SQL = r"""
CREATE FUNCTION app_private.f4_child_keys(p_school uuid,p_student uuid,p_at timestamptz,p_fields text[])
RETURNS TABLE(kind text,source_key text)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog AS $f$
WITH RECURSIVE active AS MATERIALIZED (
 SELECT f.id,f.kind,f.source_key,v.id AS version_id
 FROM public.school_education_facts f JOIN public.school_education_versions v
 ON v.school_id=f.school_id AND v.fact_id=f.id
 JOIN public.master_data_import_batches b ON b.school_id=v.school_id AND b.id=v.import_batch_id
 WHERE f.school_id=p_school AND v.superseded_at IS NULL AND v.valid_from<=p_at
 AND (v.valid_to IS NULL OR p_at<v.valid_to) AND b.status='applied'
 AND (SELECT count(*) FROM public.school_fact_change_lineage l
 JOIN public.master_data_import_batches lb ON lb.school_id=l.school_id AND lb.id=l.batch_id
 WHERE l.school_id=f.school_id AND l.target_id=f.id AND l.fact_kind=f.kind
 AND l.fact_source_key=f.source_key AND l.target_version=f.version AND lb.status='applied'
 AND l.operation IN ('upsert','correct','end','revoke'))=1
), own AS MATERIALIZED (
 SELECT a.id,a.kind,a.source_key,a.version_id FROM active a
 JOIN public.school_education_references r ON r.school_id=p_school AND r.version_id=a.version_id
 WHERE r.student_id=p_student AND
 (a.kind IN ('student_class_membership','student_grade_placement') OR
  (a.kind='student_class_position' AND 'positions'=ANY(p_fields)) OR
  (a.kind='student_cohort_membership' AND 'cohorts'=ANY(p_fields)))
), classes AS MATERIALIZED (
 SELECT DISTINCT r.education_id AS id FROM own o
 JOIN public.school_education_references r ON r.school_id=p_school AND r.version_id=o.version_id
 WHERE o.kind='student_class_membership' AND r.field_name='class_source_key'
), related AS (
 SELECT a.id,a.kind,a.source_key,a.version_id FROM active a
 JOIN public.school_education_references r ON r.school_id=p_school AND r.version_id=a.version_id
 JOIN classes c ON c.id=r.education_id WHERE r.field_name='class_source_key' AND
 (a.kind='class_placement' OR
  (a.kind='class_teacher_assignment' AND p_fields && ARRAY['teachers','official_contacts']) OR
  (a.kind='class_location' AND p_fields && ARRAY['classroom','official_contacts']))
), seeds AS (SELECT id,kind,source_key,version_id FROM own UNION SELECT id,kind,source_key,version_id FROM related),
 walk(id,kind,source_key,version_id) AS (
 SELECT id,kind,source_key,version_id FROM seeds
 UNION
 SELECT a.id,a.kind,a.source_key,a.version_id FROM walk w
 JOIN public.school_education_references r ON r.school_id=p_school AND r.version_id=w.version_id
 JOIN active a ON a.id=r.education_id
 WHERE r.field_name IN ('class_source_key','calendar_source_key','grade_source_key',
 'grade_source_keys','stage_source_key','cohort_source_key','position_source_key')
)
SELECT DISTINCT w.kind,w.source_key FROM walk w;
$f$;
REVOKE ALL ON FUNCTION app_private.f4_child_keys(uuid,uuid,timestamptz,text[]) FROM PUBLIC,
 campus_ai_runtime,campus_ai_school_fact_reader,campus_ai_school_fact_writer,campus_ai_migration,campus_ai_db_owner;

CREATE FUNCTION app.school_fact_family_education(
 p_school uuid,p_adult uuid,p_relation uuid,p_version bigint,p_factual_at timestamptz,p_fields text[])
RETURNS jsonb LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog AS $f$
DECLARE basis record; at_time timestamptz:=COALESCE(p_factual_at,statement_timestamp());
 selected jsonb; part record; records jsonb:='[]'::jsonb; teachers jsonb:='[]'::jsonb;
 locations jsonb:='[]'::jsonb; contacts jsonb:='[]'::jsonb;
 teacher_ids uuid[]; location_ids uuid[]; organization_ids uuid[];
BEGIN
 IF SESSION_USER<>'campus_ai_school_fact_reader'
    OR p_school IS DISTINCT FROM app.require_school_database_anchor() THEN
  RAISE EXCEPTION 'family education Reader denied' USING ERRCODE='28000';
 END IF;
 SELECT a.school_id,a.student_id,a.student_version,a.relationship_version,
 a.relationship_kind,a.valid_from,a.valid_to INTO basis
 FROM app.school_fact_family_authority(p_school,p_adult,p_relation,p_version) a;
 IF NOT FOUND OR basis.relationship_kind<>'verified_guardian' OR at_time<basis.valid_from
    OR (basis.valid_to IS NOT NULL AND at_time>=basis.valid_to)
    OR NOT app_private.family_relationship_is_authoritative(
        p_school,p_adult,p_relation,p_version,statement_timestamp()) THEN
  RAISE EXCEPTION 'current guardian relation does not permit education time' USING ERRCODE='28000';
 END IF;
 IF p_fields IS NULL OR cardinality(p_fields)=0 OR array_position(p_fields,NULL) IS NOT NULL
 OR NOT p_fields <@ ARRAY['class','grade','teachers','classroom','positions','cohorts','official_contacts'] THEN
  RAISE EXCEPTION 'invalid family education projection' USING ERRCODE='22023';
 END IF;
 SELECT COALESCE(jsonb_agg(jsonb_build_object('kind',k.kind,'source_key',k.source_key)),'[]')
 INTO selected FROM app_private.f4_child_keys(p_school,basis.student_id,at_time,p_fields) k;
 IF jsonb_array_length(selected)>100 THEN
  RAISE EXCEPTION 'family education set exceeds finite bound' USING ERRCODE='54000';
 END IF;
 -- One fixed batch per kind, never one full lineage lookup per output row.
 FOR part IN SELECT x.kind,array_agg(x.source_key ORDER BY x.source_key) AS keys
 FROM jsonb_to_recordset(selected) x(kind text,source_key text) GROUP BY x.kind LOOP
  records := records || app_private.f4_target_records(p_school,part.kind,part.keys,
    array_remove(app_private.f4_allowed_fields(part.kind),'basis_reference'),at_time,NULL);
 END LOOP;
 -- Causal restoration details are a Staff maintenance projection only.
 SELECT COALESCE(jsonb_agg((r - 'prior_segments' - 'latest_operation') || jsonb_build_object('segments',
 COALESCE((SELECT jsonb_agg(s - 'cause_source_key' - 'cause_version' - 'causal_prior')
 FROM jsonb_array_elements(r->'segments') s),'[]'))),'[]')
 INTO records FROM jsonb_array_elements(records) r;
 -- Child history is requested one factual instant at a time. Do not return
 -- complete personal timelines merely because structure anchors are needed.
 SELECT COALESCE(jsonb_agg(CASE WHEN r->>'fact_kind' LIKE 'student_%' THEN
 r || jsonb_build_object('segments',COALESCE((SELECT jsonb_agg(s) FROM jsonb_array_elements(r->'segments') s
 WHERE (s->>'effective_at')::timestamptz<=at_time
 AND (s->>'valid_to' IS NULL OR at_time<(s->>'valid_to')::timestamptz)),'[]')) ELSE r END),'[]')
 INTO records FROM jsonb_array_elements(records) r;
 WITH chosen AS (
 SELECT f.id,v.id AS version_id,f.kind FROM jsonb_to_recordset(selected) k(kind text,source_key text)
 JOIN public.school_education_facts f ON f.school_id=p_school AND f.kind=k.kind AND f.source_key=k.source_key
 JOIN public.school_education_versions v ON v.school_id=f.school_id AND v.fact_id=f.id
 WHERE v.superseded_at IS NULL AND v.valid_from<=at_time AND (v.valid_to IS NULL OR at_time<v.valid_to)
 ) SELECT array_agg(DISTINCT r.assignment_id) FILTER(WHERE r.assignment_id IS NOT NULL),
 array_agg(DISTINCT r.location_id) FILTER(WHERE r.location_id IS NOT NULL)
 INTO teacher_ids,location_ids FROM chosen c JOIN public.school_education_references r
 ON r.school_id=p_school AND r.version_id=c.version_id
 WHERE c.kind IN ('class_teacher_assignment','class_location');
 IF p_fields && ARRAY['teachers','official_contacts'] THEN
  SELECT COALESCE(jsonb_agg(jsonb_build_object('assignment_source_key',a.source_key,
   'assignment_version',a.version,'display_name',n.display_name,'person_version',n.version,
   'as_of',at_time)),'[]'),array_agg(DISTINCT av.organization_unit_id)
  INTO teachers,organization_ids
  FROM public.staff_assignments a JOIN public.staff_assignment_versions av
   ON av.school_id=a.school_id AND av.assignment_id=a.id
  JOIN public.school_person_names n ON n.school_id=av.school_id AND n.person_id=av.person_id
  WHERE a.school_id=p_school AND a.id=ANY(COALESCE(teacher_ids,'{}'::uuid[]))
  AND av.status='confirmed' AND av.revoked_at IS NULL AND av.effective_from<=at_time
  AND (av.effective_to IS NULL OR at_time<av.effective_to)
  AND (av.planned_valid_to IS NULL OR at_time<av.planned_valid_to)
  AND n.status='confirmed' AND n.valid_from<=at_time AND (n.valid_to IS NULL OR at_time<n.valid_to)
  AND app_private.school_fact_identity_root_is_effective_at(p_school,'person',av.person_id,at_time)
  AND app_private.school_fact_identity_root_is_effective_at(p_school,'organization',av.organization_unit_id,at_time);
 END IF;
 IF p_fields && ARRAY['classroom','official_contacts'] THEN
  SELECT COALESCE(jsonb_agg(jsonb_build_object('location_source_key',l.source_key,
  'display_name',v.display_name,'version',v.version,'as_of',at_time)),'[]') INTO locations
  FROM public.campus_locations l JOIN public.campus_location_versions v ON v.school_id=l.school_id AND v.location_id=l.id
  WHERE l.school_id=p_school AND l.id=ANY(COALESCE(location_ids,'{}'::uuid[]))
  AND v.status='confirmed' AND v.valid_from<=at_time AND (v.valid_to IS NULL OR at_time<v.valid_to)
  AND app_private.school_fact_identity_root_is_effective_at(p_school,'location',l.id,at_time);
 END IF;
 IF 'official_contacts'=ANY(p_fields) THEN
  SELECT COALESCE(jsonb_agg(jsonb_build_object('owner_kind',c.owner_kind,
  'organization_id',c.organization_unit_id,'location_id',c.location_id,'contact_kind',c.contact_kind,
  'contact_value',c.contact_value,'service_hours',c.service_hours,'version',c.version,
  'contact_at',statement_timestamp())),'[]') INTO contacts
  FROM public.official_contact_points c
  WHERE c.school_id=p_school AND c.visibility_scope='public' AND c.lifecycle_status='active'
  AND c.valid_from<=statement_timestamp() AND (c.valid_to IS NULL OR statement_timestamp()<c.valid_to)
  AND ((c.owner_kind='organization' AND app_private.school_fact_identity_root_is_effective_at(p_school,'organization',c.organization_unit_id,statement_timestamp()))
    OR (c.owner_kind='location' AND app_private.school_fact_identity_root_is_effective_at(p_school,'location',c.location_id,statement_timestamp())))
  AND ((c.owner_kind='organization' AND c.organization_unit_id=ANY(COALESCE(organization_ids,'{}'::uuid[])))
    OR (c.owner_kind='location' AND c.location_id=ANY(COALESCE(location_ids,'{}'::uuid[]))))
  AND EXISTS(SELECT 1 FROM public.school_fact_change_lineage l
    JOIN public.master_data_import_batches b ON b.school_id=l.school_id AND b.id=l.batch_id
    WHERE l.school_id=c.school_id AND l.fact_kind='official_contact' AND l.target_id=c.id
      AND l.target_version=c.version AND l.operation IN ('upsert','reactivate') AND b.status='applied')
  AND NOT EXISTS(SELECT 1 FROM public.school_fact_change_lineage l
    JOIN public.master_data_import_batches b ON b.school_id=l.school_id AND b.id=l.batch_id
    JOIN public.master_data_import_units u ON u.school_id=l.school_id AND u.id=l.unit_id
    WHERE l.school_id=c.school_id AND l.fact_kind='official_contact' AND l.target_id=c.id
      AND l.operation IN ('end','revoke') AND b.status='applied'
      AND (u.payload->>'effective_at')::timestamptz<=statement_timestamp());
 END IF;
 RETURN jsonb_build_object('status',CASE WHEN jsonb_array_length(records)=0 THEN 'no_current_records' ELSE 'available' END,
 'student_id',basis.student_id,'student_version',basis.student_version,'relationship_version',basis.relationship_version,
 'school_fact_version',app.school_fact_current_version(p_school),'as_of',at_time,'records',records,
 'related_teachers',teachers,'locations',locations,'official_contacts',contacts,'contact_at',statement_timestamp());
END;
$f$;
REVOKE ALL ON FUNCTION app.school_fact_family_education(uuid,uuid,uuid,bigint,timestamptz,text[]) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app.school_fact_family_education(uuid,uuid,uuid,bigint,timestamptz,text[])
 TO campus_ai_school_fact_reader;
"""

CORRECTION_SQL = r"""
CREATE FUNCTION app.school_fact_family_correction_targets(
 p_school uuid,p_adult uuid,p_relation uuid,p_version bigint)
RETURNS TABLE(field_name text,label text,fact_kind text,fact_source_key text,target_version bigint,student_id uuid)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog AS $f$
WITH basis AS (
 SELECT r.student_id FROM public.guardian_relationships r
 WHERE r.school_id=p_school AND r.id=p_relation AND r.guardian_person_id=p_adult
 AND r.relationship_kind='verified_guardian'
 AND app_private.family_relationship_is_authoritative(p_school,p_adult,p_relation,p_version,statement_timestamp())
), education AS (
 SELECT k.kind,k.source_key,f.version,b.student_id
 FROM basis b CROSS JOIN LATERAL app_private.f4_child_keys(p_school,b.student_id,
 statement_timestamp(),ARRAY['class','grade','teachers','classroom','positions','cohorts']) k
 JOIN public.school_education_facts f ON f.school_id=p_school AND f.kind=k.kind AND f.source_key=k.source_key
), mapped AS (
 SELECT e.kind,e.source_key,e.version,e.student_id,
 CASE e.kind WHEN 'student_class_membership' THEN 'class_source_key'
 WHEN 'student_grade_placement' THEN 'grade_source_key'
 WHEN 'class_placement' THEN 'grade_source_key'
 WHEN 'class_teacher_assignment' THEN 'assignment_source_key'
 WHEN 'class_location' THEN 'location_source_key'
 WHEN 'student_class_position' THEN 'position_source_key'
 WHEN 'student_cohort_membership' THEN 'cohort_source_key' END AS field_name
 FROM education e
)
SELECT old.field_name,old.label,old.fact_kind,old.fact_source_key,old.target_version,old.student_id
FROM app.school_fact_family_correction_targets_pre_f4(p_school,p_adult,p_relation,p_version) old
UNION ALL
SELECT m.field_name,m.kind,m.kind,m.source_key,m.version,m.student_id FROM mapped m
WHERE m.field_name IS NOT NULL AND p_school=app.require_school_database_anchor()
AND SESSION_USER='campus_ai_school_fact_writer';
$f$;
REVOKE ALL ON FUNCTION app.school_fact_family_correction_targets(uuid,uuid,uuid,bigint) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app.school_fact_family_correction_targets(uuid,uuid,uuid,bigint)
 TO campus_ai_school_fact_writer;

CREATE FUNCTION app.school_fact_f3_field_changed(
 p_school uuid,p_kind text,p_source text,p_version bigint,p_field text)
RETURNS boolean LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog AS $f$
DECLARE target uuid; previous_version bigint; answer boolean;
BEGIN
 IF SESSION_USER<>'campus_ai_school_fact_writer'
 OR p_school IS DISTINCT FROM app.require_school_database_anchor() THEN
  RAISE EXCEPTION 'correction difference denied' USING ERRCODE='28000';
 END IF;
 IF app_private.f4_allowed_fields(p_kind) IS NULL THEN
  RETURN app.school_fact_f3_field_changed_pre_f4(p_school,p_kind,p_source,p_version,p_field);
 END IF;
 IF p_version<2 OR p_field IS NULL OR NOT p_field=ANY(app_private.f4_allowed_fields(p_kind)) THEN RETURN false; END IF;
 SELECT f.id INTO target FROM public.school_education_facts f WHERE f.school_id=p_school
 AND f.kind=p_kind AND f.source_key=p_source AND f.version>=p_version;
 IF target IS NULL OR (SELECT count(*) FROM public.school_fact_change_lineage l
 JOIN public.master_data_import_batches b ON b.school_id=l.school_id AND b.id=l.batch_id
 WHERE l.school_id=p_school AND l.fact_kind=p_kind AND l.fact_source_key=p_source
 AND l.target_id=target AND l.target_version=p_version AND l.operation IN ('upsert','correct','end','cancel','revoke')
 AND b.status='applied')<>1 THEN RETURN false; END IF;
 SELECT max(l.target_version) INTO previous_version FROM public.school_fact_change_lineage l
 JOIN public.master_data_import_batches b ON b.school_id=l.school_id AND b.id=l.batch_id
 WHERE l.school_id=p_school AND l.fact_kind=p_kind AND l.fact_source_key=p_source
 AND l.target_id=target AND l.target_version<p_version AND b.status='applied';
 IF previous_version IS NULL THEN RETURN false; END IF;
 WITH rows AS (
 SELECT v.revision,v.valid_from,v.valid_to,
 CASE WHEN p_field='valid_to' THEN 'true'::jsonb ELSE v.payload->p_field END AS value
 FROM public.school_education_versions v
 WHERE v.school_id=p_school AND v.fact_id=target AND v.revision IN (previous_version,p_version)
 ), compared AS (
 SELECT r.revision,r.valid_from,r.valid_to,r.value,
 CASE WHEN lag(r.valid_to) OVER w=r.valid_from AND lag(r.value) OVER w IS NOT DISTINCT FROM r.value
 THEN 0 ELSE 1 END AS start_group FROM rows r WINDOW w AS(PARTITION BY r.revision ORDER BY r.valid_from)
 ), grouped AS (
 SELECT c.revision,c.valid_from,c.valid_to,c.value,
 sum(c.start_group) OVER(PARTITION BY c.revision ORDER BY c.valid_from) AS grp FROM compared c
 ), merged AS (
 SELECT g.revision,g.grp,min(g.valid_from) AS starts,
 CASE WHEN bool_or(g.valid_to IS NULL) THEN NULL ELSE max(g.valid_to) END AS ends,
 (array_agg(g.value ORDER BY g.valid_from))[1] AS value FROM grouped g GROUP BY g.revision,g.grp
 ), projections AS (
 SELECT m.revision,jsonb_agg(jsonb_build_array(m.starts,m.ends,m.value) ORDER BY m.starts) AS value
 FROM merged m GROUP BY m.revision
 ) SELECT (SELECT value FROM projections WHERE revision=p_version) IS DISTINCT FROM
 (SELECT value FROM projections WHERE revision=previous_version) INTO answer;
 RETURN COALESCE(answer,false);
END;
$f$;
REVOKE ALL ON FUNCTION app.school_fact_f3_field_changed(uuid,text,text,bigint,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app.school_fact_f3_field_changed(uuid,text,text,bigint,text)
 TO campus_ai_school_fact_writer;
"""


SSR_TEMPORAL_SQL = "ALTER TABLE public.student_school_relationships ADD COLUMN source_key varchar(255);\nALTER TABLE public.student_school_relationships ADD COLUMN superseded_at timestamptz;\nUPDATE public.student_school_relationships r SET source_key=l.fact_source_key FROM (SELECT DISTINCT ON(target_id) target_id,fact_source_key FROM public.school_fact_change_lineage WHERE fact_kind='student_school_relationship' ORDER BY target_id,target_version DESC) l WHERE r.id=l.target_id;\nSET CONSTRAINTS ALL IMMEDIATE;\nALTER TABLE public.student_school_relationships DROP CONSTRAINT excl_student_school_relationships_period;\nALTER TABLE public.student_school_relationships ADD CONSTRAINT excl_student_school_relationships_period EXCLUDE USING gist (school_id WITH =,student_id WITH =,tstzrange(valid_from,COALESCE(valid_to,'infinity'::timestamptz),'[)') WITH &&) WHERE (superseded_at IS NULL);\nCREATE INDEX ix_student_school_relationships_source_revision ON public.student_school_relationships(school_id,source_key,version) WHERE superseded_at IS NULL;\nCREATE POLICY f4_school_relationship_current_read ON public.student_school_relationships AS RESTRICTIVE FOR SELECT TO campus_ai_school_fact_reader,campus_ai_context_definer,campus_ai_identity_definer USING (superseded_at IS NULL);\n"


COORDINATES_SQL = r"""
CREATE FUNCTION app.school_fact_f4_coordinates(p_school uuid,p_kind text,p_objects text[])
RETURNS jsonb LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog AS $f$
BEGIN
 IF SESSION_USER<>'campus_ai_school_fact_reader'
 OR p_school IS DISTINCT FROM app.require_school_database_anchor() THEN
  RAISE EXCEPTION 'education coordinate Reader denied' USING ERRCODE='28000';
 END IF;
 IF app_private.f4_allowed_fields(p_kind) IS NULL OR p_objects IS NULL
 OR cardinality(p_objects)>100 OR array_position(p_objects,NULL) IS NOT NULL
 OR cardinality(p_objects)<>(SELECT count(DISTINCT k) FROM unnest(p_objects) k)
 OR EXISTS(SELECT 1 FROM unnest(p_objects) k WHERE btrim(k)='' OR length(k)>255) THEN
  RAISE EXCEPTION 'invalid education coordinate set' USING ERRCODE='22023';
 END IF;
 RETURN jsonb_build_object('school_fact_version',app.school_fact_current_version(p_school),
 'records',(SELECT COALESCE(jsonb_agg(jsonb_build_object('source_key',k,
 'version',COALESCE(f.version,0),'status',CASE WHEN f.id IS NULL THEN 'not_recorded' ELSE 'exists' END)
 ORDER BY k),'[]') FROM unnest(p_objects) k LEFT JOIN public.school_education_facts f
 ON f.school_id=p_school AND f.kind=p_kind AND f.source_key=k));
END;
$f$;
REVOKE ALL ON FUNCTION app.school_fact_f4_coordinates(uuid,text,text[]) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app.school_fact_f4_coordinates(uuid,text,text[])
 TO campus_ai_school_fact_reader;
"""


SSR_COORDINATE_SQL = r"""
CREATE FUNCTION app_private.f4_ssr_coordinate_exists(p_school uuid,p_source text,p_version bigint)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog AS $f$
 SELECT SESSION_USER='campus_ai_school_fact_reader'
 AND p_school=app.require_school_database_anchor() AND p_version>0
 AND EXISTS(SELECT 1 FROM public.student_school_relationships r
 WHERE r.school_id=p_school AND r.source_key=p_source AND r.version=p_version);
$f$;
REVOKE ALL ON FUNCTION app_private.f4_ssr_coordinate_exists(uuid,text,bigint)
 FROM PUBLIC,campus_ai_runtime,campus_ai_school_fact_reader,campus_ai_school_fact_writer,campus_ai_migration;
GRANT EXECUTE ON FUNCTION app_private.f4_ssr_coordinate_exists(uuid,text,bigint) TO campus_ai_context_definer;
"""


SSR_READ_SQL = r"""
CREATE FUNCTION app.school_fact_f3_scope_is_current(p_school uuid,p_kind text,p_objects text[],p_as_of timestamptz)
RETURNS boolean LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog AS $f$
BEGIN
 IF p_kind IS DISTINCT FROM 'student_school_relationship' THEN
  RETURN app.school_fact_f3_scope_is_current_pre_f4(p_school,p_kind,p_objects,p_as_of);
 END IF;
 IF SESSION_USER<>'campus_ai_school_fact_reader' OR p_school IS DISTINCT FROM app.require_school_database_anchor() THEN
  RAISE EXCEPTION 'F3 Reader denied' USING ERRCODE='28000';
 END IF;
 IF p_objects IS NULL OR cardinality(p_objects)>100 OR array_position(p_objects,NULL) IS NOT NULL
 OR p_as_of IS NULL OR EXISTS(SELECT 1 FROM unnest(p_objects) k WHERE btrim(k)='' OR length(k)>255)
 OR NOT app_private.school_fact_identity_root_is_effective_at(p_school,'school',p_school,p_as_of) THEN RETURN false; END IF;
 RETURN NOT EXISTS(SELECT 1 FROM unnest(p_objects) k WHERE NOT EXISTS(
  SELECT 1 FROM public.school_fact_change_lineage l JOIN public.master_data_import_batches b
  ON b.school_id=l.school_id AND b.id=l.batch_id JOIN public.master_data_import_units u
  ON u.school_id=l.school_id AND u.id=l.unit_id
  WHERE l.school_id=p_school AND l.fact_kind=p_kind AND l.fact_source_key=k AND b.status='applied'
  AND l.operation IN ('upsert','correct','end','revoke') AND u.payload->>'effective_at' IS NOT NULL
  AND l.target_version=(SELECT max(ll.target_version) FROM public.school_fact_change_lineage ll
    JOIN public.master_data_import_batches bb ON bb.school_id=ll.school_id AND bb.id=ll.batch_id
    WHERE ll.school_id=p_school AND ll.fact_kind=p_kind AND ll.fact_source_key=k AND bb.status='applied')
  AND app_private.f4_ssr_coordinate_exists(p_school,k,l.target_version)));
END;
$f$;
REVOKE ALL ON FUNCTION app.school_fact_f3_scope_is_current(uuid,text,text[],timestamptz)
 FROM PUBLIC,campus_ai_runtime,campus_ai_school_fact_writer,campus_ai_migration,campus_ai_db_owner;
GRANT EXECUTE ON FUNCTION app.school_fact_f3_scope_is_current(uuid,text,text[],timestamptz)
 TO campus_ai_school_fact_reader;

CREATE FUNCTION app.school_fact_f3_staff_target(
 p_school uuid,p_kind text,p_source text,p_creation boolean,p_factual_at timestamptz,p_fields text[]
) RETURNS jsonb LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog AS $f$
DECLARE publication record; row_data record; result jsonb; content jsonb; total bigint;
BEGIN
 IF p_kind IS DISTINCT FROM 'student_school_relationship' THEN
  RETURN app.school_fact_f3_staff_target_pre_f4(p_school,p_kind,p_source,p_creation,p_factual_at,p_fields);
 END IF;
 IF SESSION_USER<>'campus_ai_school_fact_reader' OR p_school IS DISTINCT FROM app.require_school_database_anchor() THEN
  RAISE EXCEPTION 'F3 Reader denied' USING ERRCODE='28000';
 END IF;
 IF p_source IS NULL OR btrim(p_source)='' OR length(p_source)>255 OR p_creation IS NULL
 OR p_factual_at IS NULL OR p_factual_at>statement_timestamp()
 OR p_fields IS NULL OR cardinality(p_fields)=0 OR array_position(p_fields,NULL) IS NOT NULL
 OR cardinality(p_fields)<>(SELECT count(DISTINCT f) FROM unnest(p_fields) f)
 OR NOT p_fields <@ ARRAY['student_source_key','student_number','enrollment_status','residence_mode',
 'primary_campus_source_key','administrative_campus_source_key','valid_to'] THEN
  RAISE EXCEPTION 'invalid fixed SSR target' USING ERRCODE='22023';
 END IF;
 SELECT l.target_version,l.batch_id,l.created_at AS published_at,l.operation,
 (SELECT s.source_ref FROM public.school_fact_sources s WHERE s.school_id=p_school AND s.source_key='change-set:'||l.batch_id::text) AS source_trace_reference
 INTO publication FROM public.school_fact_change_lineage l JOIN public.master_data_import_batches b
 ON b.school_id=l.school_id AND b.id=l.batch_id
 WHERE l.school_id=p_school AND l.fact_kind=p_kind AND l.fact_source_key=p_source AND b.status='applied'
 ORDER BY l.target_version DESC LIMIT 1;
 result:=jsonb_build_object('status','no_current_records','fact_kind',p_kind,'source_key',p_source,
 'content','{}'::jsonb,'expected_version',COALESCE(publication.target_version,0),
 'school_fact_version',app.school_fact_current_version(p_school),'label','学生在校关系',
 'source_reference',CASE WHEN publication.batch_id IS NOT NULL THEN 'publication:'||publication.batch_id::text END,
 'source_published_at',publication.published_at,'source_trace_reference',publication.source_trace_reference,
 'source_name',NULL,'factual_at',p_factual_at,'valid_from',NULL,'valid_to',NULL);
 IF p_creation THEN
  IF publication.target_version IS NOT NULL THEN RAISE EXCEPTION 'SSR already published' USING ERRCODE='22023'; END IF;
  RETURN result||jsonb_build_object('status','available_creation');
 END IF;
 IF publication.target_version IS NULL THEN RETURN result; END IF;
 SELECT count(*) INTO total FROM public.student_school_relationships r
 WHERE r.school_id=p_school AND r.source_key=p_source AND r.superseded_at IS NULL
 AND r.valid_from<=p_factual_at AND (r.valid_to IS NULL OR p_factual_at<r.valid_to);
 IF total>1 THEN RETURN result||jsonb_build_object('status','conflict'); END IF;
 IF total=0 THEN RETURN result; END IF;
 SELECT r.student_id,r.student_number,r.enrollment_status,r.residence_mode,
 r.primary_campus_location_id,r.administrative_campus_location_id,r.valid_from,r.valid_to,r.version
 INTO STRICT row_data FROM public.student_school_relationships r
 WHERE r.school_id=p_school AND r.source_key=p_source AND r.superseded_at IS NULL
 AND r.valid_from<=p_factual_at AND (r.valid_to IS NULL OR p_factual_at<r.valid_to);
 IF row_data.version>publication.target_version THEN RAISE EXCEPTION 'SSR publication mismatch' USING ERRCODE='55000'; END IF;
 SELECT COALESCE(jsonb_object_agg(k,v),'{}'::jsonb) INTO content FROM jsonb_each(jsonb_build_object(
 'student_source_key',CASE WHEN 'student_source_key'=ANY(p_fields) THEN (SELECT s.source_key FROM public.students s WHERE s.school_id=p_school AND s.id=row_data.student_id) END,
 'student_number',CASE WHEN 'student_number'=ANY(p_fields) THEN row_data.student_number END,
 'enrollment_status',CASE WHEN 'enrollment_status'=ANY(p_fields) THEN row_data.enrollment_status END,
 'residence_mode',CASE WHEN 'residence_mode'=ANY(p_fields) THEN row_data.residence_mode END,
 'primary_campus_source_key',CASE WHEN 'primary_campus_source_key'=ANY(p_fields) THEN (SELECT c.source_key FROM public.campus_locations c WHERE c.school_id=p_school AND c.id=row_data.primary_campus_location_id) END,
 'administrative_campus_source_key',CASE WHEN 'administrative_campus_source_key'=ANY(p_fields) THEN (SELECT c.source_key FROM public.campus_locations c WHERE c.school_id=p_school AND c.id=row_data.administrative_campus_location_id) END,
 'valid_to',CASE WHEN 'valid_to'=ANY(p_fields) THEN row_data.valid_to END)) fields(k,v) WHERE k=ANY(p_fields);
 RETURN result||jsonb_build_object('status','available','content',content,'valid_from',row_data.valid_from,'valid_to',row_data.valid_to);
END;
$f$;
REVOKE ALL ON FUNCTION app.school_fact_f3_staff_target(uuid,text,text,boolean,timestamptz,text[])
 FROM PUBLIC,campus_ai_runtime,campus_ai_school_fact_writer,campus_ai_migration,campus_ai_db_owner;
GRANT EXECUTE ON FUNCTION app.school_fact_f3_staff_target(uuid,text,text,boolean,timestamptz,text[])
 TO campus_ai_school_fact_reader;
"""


SSR_TIMELINE_SQL = r"""
CREATE FUNCTION app.school_fact_f4_ssr_timeline(p_school uuid,p_source text,p_fields text[])
RETURNS jsonb LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog AS $f$
DECLARE publication record; segments jsonb; total bigint;
BEGIN
 IF SESSION_USER<>'campus_ai_school_fact_reader' OR p_school IS DISTINCT FROM app.require_school_database_anchor() THEN
  RAISE EXCEPTION 'F4 Reader denied' USING ERRCODE='28000'; END IF;
 IF p_source IS NULL OR btrim(p_source)='' OR length(p_source)>255 OR p_fields IS NULL
 OR cardinality(p_fields)=0 OR array_position(p_fields,NULL) IS NOT NULL
 OR cardinality(p_fields)<>(SELECT count(DISTINCT f) FROM unnest(p_fields) f)
 OR NOT p_fields <@ ARRAY['student_source_key','student_number','enrollment_status','residence_mode',
 'primary_campus_source_key','administrative_campus_source_key','valid_to'] THEN
  RAISE EXCEPTION 'invalid SSR timeline projection' USING ERRCODE='22023'; END IF;
 SELECT l.target_version,l.batch_id,l.created_at AS published_at INTO publication
 FROM public.school_fact_change_lineage l JOIN public.master_data_import_batches b
 ON b.school_id=l.school_id AND b.id=l.batch_id
 WHERE l.school_id=p_school AND l.fact_kind='student_school_relationship'
 AND l.fact_source_key=p_source AND b.status='applied' ORDER BY l.target_version DESC LIMIT 1;
 SELECT count(*) INTO total FROM public.student_school_relationships r
 WHERE r.school_id=p_school AND r.source_key=p_source AND r.superseded_at IS NULL;
 IF total>1000 THEN RAISE EXCEPTION 'SSR timeline exceeds limit' USING ERRCODE='22023'; END IF;
 IF EXISTS(SELECT 1 FROM public.student_school_relationships r WHERE r.school_id=p_school
 AND r.source_key=p_source AND r.superseded_at IS NULL
 AND (publication.target_version IS NULL OR r.version>publication.target_version)) THEN
 RAISE EXCEPTION 'SSR publication mismatch' USING ERRCODE='55000'; END IF;
 SELECT COALESCE(jsonb_agg(jsonb_build_object('effective_at',r.valid_from,'valid_to',r.valid_to,
 'revision',r.version,'content',(SELECT jsonb_object_agg(k,v) FROM jsonb_each(jsonb_build_object(
 'student_source_key',s.source_key,'student_number',r.student_number,'enrollment_status',r.enrollment_status,
 'residence_mode',r.residence_mode,'primary_campus_source_key',pc.source_key,
 'administrative_campus_source_key',ac.source_key,'valid_to',r.valid_to)) x(k,v) WHERE k=ANY(p_fields)),
 'source',jsonb_build_object('source_kind',src.source_kind,'source_ref',src.source_ref,
 'batch_id',lin.batch_id,'published_at',lin.created_at,'publication_reference','publication:'||lin.batch_id::text))
 ORDER BY r.valid_from),'[]'::jsonb) INTO segments
 FROM public.student_school_relationships r
 JOIN public.students s ON s.school_id=r.school_id AND s.id=r.student_id
 LEFT JOIN public.campus_locations pc ON pc.school_id=r.school_id AND pc.id=r.primary_campus_location_id
 LEFT JOIN public.campus_locations ac ON ac.school_id=r.school_id AND ac.id=r.administrative_campus_location_id
 LEFT JOIN LATERAL(SELECT l.created_at,l.batch_id FROM public.school_fact_change_lineage l
 JOIN public.master_data_import_batches b ON b.school_id=l.school_id AND b.id=l.batch_id
 WHERE l.school_id=r.school_id AND l.fact_kind='student_school_relationship' AND l.fact_source_key=p_source
 AND l.target_version=r.version AND b.status='applied' ORDER BY l.created_at DESC LIMIT 1) lin ON true
 LEFT JOIN public.school_fact_sources src ON src.school_id=r.school_id AND src.source_key='change-set:'||lin.batch_id::text
 WHERE r.school_id=p_school AND r.source_key=p_source AND r.superseded_at IS NULL;
 RETURN jsonb_build_object('fact_kind','student_school_relationship','source_key',p_source,
 'version',COALESCE(publication.target_version,0),'school_fact_version',app.school_fact_current_version(p_school),
 'status',CASE WHEN publication.target_version IS NULL THEN 'not_recorded' WHEN total=0 THEN 'no_current_records' ELSE 'available' END,
 'segments',segments);
END;
$f$;
REVOKE ALL ON FUNCTION app.school_fact_f4_ssr_timeline(uuid,text,text[])
 FROM PUBLIC,campus_ai_runtime,campus_ai_school_fact_writer,campus_ai_migration,campus_ai_db_owner;
GRANT EXECUTE ON FUNCTION app.school_fact_f4_ssr_timeline(uuid,text,text[]) TO campus_ai_school_fact_reader;
"""


FAMILY_SSR_SOURCE_SQL = r"""
DO $patch$
DECLARE definition text;
 old_clause text := 'l.fact_kind=''student_school_relationship'' AND l.target_id=enrollment_id';
 new_clause text := 'l.fact_kind=''student_school_relationship'' AND l.fact_source_key=(SELECT r.source_key FROM public.student_school_relationships r WHERE r.school_id=p_school AND r.id=enrollment_id)';
BEGIN
 SELECT pg_get_functiondef('app.school_fact_family_profile(uuid,uuid,uuid,bigint,timestamptz)'::regprocedure) INTO definition;
 IF position(old_clause IN definition)=0 THEN
  RAISE EXCEPTION 'Expected F3 profile source boundary not found';
 END IF;
 definition:=replace(definition,old_clause,new_clause);
 definition:=replace(definition,'l.operation IN (''upsert'',''reactivate'')','l.operation IN (''upsert'',''reactivate'',''correct'')');
 EXECUTE definition;
END;
$patch$;
"""


DEPENDENCY_SQL = r"""
-- Freeze into migration 29 after review; this file is not executed by the application.
-- Installation role: campus_ai_db_owner for column grants/policies, then campus_ai_context_definer for function.
GRANT SELECT(id,school_id,source_key,version,valid_from,valid_to,revoked_at,source_id,import_batch_id,basis_reference,education_target_id,education_position_id,field_name,academic_year,quality_kind,target_fact_kind,owner_kind,target_source_key,replacement_source_key,responsibility,evidence_reference,purpose,contact_kind,coverage_status,negative_code,dispute_status,source_status,last_success_at,verified_at,review_due_at) ON public.school_fact_qualities TO campus_ai_context_definer;
GRANT SELECT(id,school_id,source_key,policy_id,education_target_id,academic_year,campus_location_id,valid_from,valid_to,version,source_id,import_batch_id) ON public.school_policy_scopes TO campus_ai_context_definer;
GRANT SELECT(id,school_id,source_key) ON public.campus_locations TO campus_ai_context_definer;
GRANT SELECT(id,school_id,source_key) ON public.school_policies TO campus_ai_context_definer;
CREATE POLICY f4_quality_context_read ON public.school_fact_qualities FOR SELECT TO campus_ai_context_definer USING(school_id=app.require_school_database_anchor());
CREATE POLICY f4_policy_scope_context_read ON public.school_policy_scopes FOR SELECT TO campus_ai_context_definer USING(school_id=app.require_school_database_anchor());
CREATE POLICY f4_policy_context_read ON public.school_policies FOR SELECT TO campus_ai_context_definer USING(school_id=app.require_school_database_anchor());
SET LOCAL ROLE campus_ai_context_definer;
CREATE FUNCTION app.school_fact_f4_dependencies(
 p_school uuid,p_kind text,p_keys text[],p_owner_kind text,p_owner_keys text[],
 p_target_kind text,p_target_keys text[],p_fields text[],p_at timestamptz,p_academic_year bigint,p_policy_keys text[],p_campus_keys text[],p_position_keys text[])
RETURNS jsonb LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog AS $f$
DECLARE result jsonb; boundary timestamptz;
BEGIN
 IF SESSION_USER<>'campus_ai_school_fact_reader' OR p_school IS DISTINCT FROM app.require_school_database_anchor() THEN
  RAISE EXCEPTION 'education dependency Reader denied' USING ERRCODE='28000';
 END IF;
 IF p_kind IS NULL OR p_kind NOT IN ('fact_quality','policy_scope') OR p_at IS NULL
  OR p_keys IS NULL OR p_owner_keys IS NULL OR p_target_keys IS NULL OR p_fields IS NULL OR p_policy_keys IS NULL OR p_campus_keys IS NULL OR p_position_keys IS NULL
  OR cardinality(p_owner_keys) NOT BETWEEN 1 AND 100 OR cardinality(p_fields) NOT BETWEEN 1 AND 100
  OR cardinality(p_keys)>100 OR cardinality(p_target_keys)>100 OR cardinality(p_policy_keys)>100 OR cardinality(p_campus_keys)>100 OR cardinality(p_position_keys)>100
  OR app_private.f4_allowed_fields(p_owner_kind) IS NULL OR app_private.f4_allowed_fields(p_target_kind) IS NULL
  OR NOT p_fields<@app_private.f4_allowed_fields(p_target_kind)
  OR EXISTS(SELECT 1 FROM unnest(p_keys||p_owner_keys||p_target_keys||p_fields||p_policy_keys||p_campus_keys||p_position_keys) k WHERE k IS NULL OR btrim(k)='' OR length(k)>255)
  OR (p_kind='policy_scope' AND cardinality(p_policy_keys)=0)
 THEN RAISE EXCEPTION 'invalid finite dependency selection' USING ERRCODE='22023'; END IF;
 IF NOT app.school_fact_f4_scope_is_current(p_school,p_owner_kind,p_owner_keys,statement_timestamp())
  OR NOT app.school_fact_f4_scope_is_current(p_school,p_target_kind,p_target_keys,statement_timestamp()) THEN
  RAISE EXCEPTION 'education publication evidence unavailable' USING ERRCODE='55000'; END IF;
 IF p_kind='fact_quality' THEN
  WITH selected AS MATERIALIZED (
   SELECT q.id,q.source_key,q.version,q.valid_from,LEAST(q.valid_to,q.revoked_at) valid_to,q.source_id,
    q.basis_reference,q.quality_kind,q.target_fact_kind,q.owner_kind,q.target_source_key,q.replacement_source_key,
    q.responsibility,q.evidence_reference,q.purpose,q.contact_kind,q.coverage_status,q.negative_code,
    ov.valid_to owner_boundary,pv.valid_to position_boundary,
    q.dispute_status,q.source_status,q.last_success_at,q.verified_at,q.review_due_at,q.field_name,q.academic_year,
    e.kind education_target_kind,e.source_key education_target_source_key,pos.source_key position_source_key,l.created_at published_at,l.batch_id
   FROM public.school_fact_qualities q
   JOIN public.school_education_facts e ON e.school_id=q.school_id AND e.id=q.education_target_id
   LEFT JOIN public.school_education_facts pos ON pos.school_id=q.school_id AND pos.id=q.education_position_id
   JOIN LATERAL (SELECT count(*) n,min(v.valid_to) valid_to FROM public.school_education_versions v
    WHERE v.school_id=e.school_id AND v.fact_id=e.id AND v.superseded_at IS NULL
    AND v.valid_from<=p_at AND (v.valid_to IS NULL OR p_at<v.valid_to)) ov ON ov.n=1
   LEFT JOIN LATERAL (SELECT count(*) n,min(v.valid_to) valid_to FROM public.school_education_versions v
    WHERE v.school_id=pos.school_id AND v.fact_id=pos.id AND v.superseded_at IS NULL
    AND v.valid_from<=p_at AND (v.valid_to IS NULL OR p_at<v.valid_to)) pv ON true
   JOIN public.school_fact_change_lineage l ON l.school_id=q.school_id AND l.fact_kind='fact_quality'
    AND l.fact_source_key=q.source_key AND l.target_id=q.id AND l.target_version=q.version
   JOIN public.master_data_import_batches b ON b.school_id=l.school_id AND b.id=l.batch_id AND b.status='applied'
   WHERE q.school_id=p_school AND q.source_key=ANY(p_keys) AND q.owner_kind='education'
    AND e.kind=p_owner_kind AND e.source_key=ANY(p_owner_keys) AND q.target_fact_kind=p_target_kind
    AND (q.target_source_key IS NULL OR q.target_source_key=ANY(p_target_keys))
    AND (q.replacement_source_key IS NULL OR q.replacement_source_key=ANY(p_target_keys))
    AND (q.field_name IS NULL OR q.field_name=ANY(p_fields))
    AND (q.academic_year IS NULL OR q.academic_year=p_academic_year)
    AND (pos.id IS NULL OR (pv.n=1 AND pos.kind='class_position_definition' AND pos.source_key=ANY(p_position_keys)))
    AND EXISTS(SELECT 1 FROM public.school_education_versions v WHERE v.school_id=e.school_id AND v.fact_id=e.id
       AND v.superseded_at IS NULL AND v.valid_from<=p_at AND (v.valid_to IS NULL OR p_at<v.valid_to))
  ), current_rows AS (
   SELECT s.*,src.source_kind,src.source_ref FROM selected s JOIN public.school_fact_sources src ON src.school_id=p_school AND src.id=s.source_id
   WHERE s.valid_from<=p_at AND (s.valid_to IS NULL OR p_at<s.valid_to)
  ) SELECT COALESCE(jsonb_agg(jsonb_build_object('source_key',source_key,'version',version,'effective_at',valid_from,'valid_to',valid_to,
    'content',to_jsonb(c)-ARRAY['id','source_id','source_key','version','valid_from','published_at','batch_id','source_kind','source_ref','owner_boundary','position_boundary'],
    'source',jsonb_build_object('label','学校明确登记的质量依据','source_kind',source_kind,'source_ref',source_ref,'published_at',published_at,'batch_id',batch_id,'publication_reference','publication:'||batch_id::text))) ,'[]'::jsonb),
    (SELECT min(t) FROM (SELECT valid_from t FROM selected UNION ALL SELECT valid_to FROM selected UNION ALL SELECT review_due_at FROM selected UNION ALL SELECT owner_boundary FROM selected UNION ALL SELECT position_boundary FROM selected) times WHERE t>p_at)
   INTO result,boundary FROM current_rows c;
 ELSE
  WITH selected AS MATERIALIZED (
   SELECT s.id,s.source_key,s.version,s.valid_from,s.valid_to,s.source_id,s.academic_year,ov.valid_to owner_boundary,
    e.kind education_target_kind,e.source_key education_target_source_key,p.source_key policy_source_key,
    loc.source_key campus_source_key,
    l.created_at published_at,l.batch_id
   FROM public.school_policy_scopes s JOIN public.school_education_facts e ON e.school_id=s.school_id AND e.id=s.education_target_id
   LEFT JOIN public.campus_locations loc ON loc.school_id=s.school_id AND loc.id=s.campus_location_id
   JOIN LATERAL (SELECT count(*) n,min(v.valid_to) valid_to FROM public.school_education_versions v
    WHERE v.school_id=e.school_id AND v.fact_id=e.id AND v.superseded_at IS NULL
    AND v.valid_from<=p_at AND (v.valid_to IS NULL OR p_at<v.valid_to)) ov ON ov.n=1
   JOIN public.school_policies p ON p.school_id=s.school_id AND p.id=s.policy_id AND p.source_key=ANY(p_policy_keys)
   JOIN public.school_fact_change_lineage l ON l.school_id=s.school_id AND l.fact_kind='policy_scope'
    AND l.fact_source_key=s.source_key AND l.target_id=s.id AND l.target_version=s.version
   JOIN public.master_data_import_batches b ON b.school_id=l.school_id AND b.id=l.batch_id AND b.status='applied'
   WHERE s.school_id=p_school AND s.source_key=ANY(p_keys) AND e.kind=p_owner_kind AND e.source_key=ANY(p_owner_keys)
    AND (s.academic_year IS NULL OR s.academic_year=p_academic_year)
    AND (s.campus_location_id IS NULL OR loc.source_key=ANY(p_campus_keys))
    AND EXISTS(SELECT 1 FROM public.school_education_versions v WHERE v.school_id=e.school_id AND v.fact_id=e.id
      AND v.superseded_at IS NULL AND v.valid_from<=p_at AND (v.valid_to IS NULL OR p_at<v.valid_to))
  ), current_rows AS (
   SELECT s.*,src.source_kind,src.source_ref FROM selected s JOIN public.school_fact_sources src ON src.school_id=p_school AND src.id=s.source_id
   WHERE s.valid_from<=p_at AND (s.valid_to IS NULL OR p_at<s.valid_to)
  ) SELECT COALESCE(jsonb_agg(jsonb_build_object('source_key',source_key,'version',version,'effective_at',valid_from,'valid_to',valid_to,
   'content',to_jsonb(c)-ARRAY['id','source_id','source_key','version','valid_from','published_at','batch_id','source_kind','source_ref','owner_boundary','position_boundary'],
   'source',jsonb_build_object('label','学校明确登记的制度适用范围','source_kind',source_kind,'source_ref',source_ref,'published_at',published_at,'batch_id',batch_id,'publication_reference','publication:'||batch_id::text))),'[]'::jsonb),
   (SELECT min(t) FROM (SELECT valid_from t FROM selected UNION ALL SELECT valid_to FROM selected UNION ALL SELECT owner_boundary FROM selected) times WHERE t>p_at)
   INTO result,boundary FROM current_rows c;
 END IF;
 RETURN jsonb_build_object('school_fact_version',app.school_fact_current_version(p_school),'records',result,'next_valid_boundary',boundary);
END;
$f$;
REVOKE ALL ON FUNCTION app.school_fact_f4_dependencies(uuid,text,text[],text,text[],text,text[],text[],timestamptz,bigint,text[],text[],text[]) FROM PUBLIC,campus_ai_runtime,campus_ai_school_fact_writer,campus_ai_migration,campus_ai_db_owner;
GRANT EXECUTE ON FUNCTION app.school_fact_f4_dependencies(uuid,text,text[],text,text[],text,text[],text[],timestamptz,bigint,text[],text[],text[]) TO campus_ai_school_fact_reader;
SET LOCAL ROLE campus_ai_db_owner;

"""


DEPENDENCY_COORDINATES_SQL = r"""
-- Freeze into migration 29 as campus_ai_context_definer; no new payload grants.
CREATE FUNCTION app.school_fact_f4_dependency_coordinates(p_school uuid,p_kind text,p_keys text[])
RETURNS jsonb LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog AS $f$
DECLARE records jsonb;
BEGIN
 IF SESSION_USER<>'campus_ai_school_fact_reader' OR p_school IS DISTINCT FROM app.require_school_database_anchor() THEN
  RAISE EXCEPTION 'dependency coordinates denied' USING ERRCODE='28000'; END IF;
 IF p_kind IS NULL OR p_kind NOT IN ('fact_quality','policy_scope') OR p_keys IS NULL OR cardinality(p_keys)>100
  OR array_position(p_keys,NULL) IS NOT NULL OR cardinality(p_keys)<>(SELECT count(DISTINCT k) FROM unnest(p_keys) k)
  OR EXISTS(SELECT 1 FROM unnest(p_keys) k WHERE btrim(k)='' OR length(k)>255)
 THEN RAISE EXCEPTION 'invalid dependency coordinates' USING ERRCODE='22023'; END IF;
 WITH physical AS (
  SELECT q.source_key,q.version FROM public.school_fact_qualities q WHERE p_kind='fact_quality' AND q.school_id=p_school AND q.source_key=ANY(p_keys)
  UNION ALL SELECT s.source_key,s.version FROM public.school_policy_scopes s WHERE p_kind='policy_scope' AND s.school_id=p_school AND s.source_key=ANY(p_keys)
 ), all_versions AS (
  SELECT source_key,version FROM physical
  UNION ALL SELECT l.fact_source_key,l.target_version FROM public.school_fact_change_lineage l
    WHERE l.school_id=p_school AND l.fact_kind=p_kind AND l.fact_source_key=ANY(p_keys)
 ) SELECT COALESCE(jsonb_agg(jsonb_build_object('source_key',k,'version',COALESCE(v.version,0),
    'status',CASE WHEN v.version IS NULL THEN 'not_recorded' ELSE 'exists' END) ORDER BY k),'[]'::jsonb)
 INTO records FROM unnest(p_keys) k LEFT JOIN LATERAL (SELECT max(version) version FROM all_versions a WHERE a.source_key=k) v ON true;
 RETURN jsonb_build_object('school_fact_version',app.school_fact_current_version(p_school),'records',records);
END;
$f$;
REVOKE ALL ON FUNCTION app.school_fact_f4_dependency_coordinates(uuid,text,text[]) FROM PUBLIC,campus_ai_runtime,campus_ai_school_fact_writer,campus_ai_migration,campus_ai_db_owner;
GRANT EXECUTE ON FUNCTION app.school_fact_f4_dependency_coordinates(uuid,text,text[]) TO campus_ai_school_fact_reader;

"""


DEPENDENCY_SELECTION_SQL = r"""
-- Frozen migration input. Exact source lookup returns reference coordinates only, never content.
CREATE FUNCTION app.school_fact_f4_dependency_selection(p_school uuid,p_kind text,p_key text,p_at timestamptz)
RETURNS jsonb LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog AS $f$
DECLARE rows jsonb;
BEGIN
 IF SESSION_USER<>'campus_ai_school_fact_reader' OR p_school IS DISTINCT FROM app.require_school_database_anchor() THEN
  RAISE EXCEPTION 'dependency selection denied' USING ERRCODE='28000'; END IF;
 IF p_kind IS NULL OR p_kind NOT IN ('fact_quality','policy_scope') OR p_key IS NULL OR btrim(p_key)='' OR length(p_key)>255 OR p_at IS NULL THEN
  RAISE EXCEPTION 'invalid exact dependency selection' USING ERRCODE='22023'; END IF;
 WITH selected AS (
  SELECT q.id,q.source_key,q.version,q.education_target_id,q.target_fact_kind,q.target_source_key,q.replacement_source_key,
   q.field_name,q.academic_year,q.education_position_id,NULL::uuid campus_location_id,NULL::uuid policy_id
  FROM public.school_fact_qualities q WHERE p_kind='fact_quality' AND q.school_id=p_school AND q.source_key=p_key
   AND q.owner_kind='education' AND q.valid_from<=p_at AND (LEAST(q.valid_to,q.revoked_at) IS NULL OR p_at<LEAST(q.valid_to,q.revoked_at))
  UNION ALL
  SELECT s.id,s.source_key,s.version,s.education_target_id,NULL::text,NULL::text,NULL::text,NULL::text,s.academic_year,NULL::uuid,s.campus_location_id,s.policy_id
  FROM public.school_policy_scopes s WHERE p_kind='policy_scope' AND s.school_id=p_school AND s.source_key=p_key
   AND s.valid_from<=p_at AND (s.valid_to IS NULL OR p_at<s.valid_to)
 ) SELECT COALESCE(jsonb_agg(jsonb_build_object('source_key',s.source_key,'version',s.version,
   'owner_kind',e.kind,'owner_key',e.source_key,'target_kind',COALESCE(s.target_fact_kind,e.kind),
   'target_key',s.target_source_key,'replacement_key',s.replacement_source_key,'field_name',s.field_name,
   'academic_year',s.academic_year,'position_key',pos.source_key,'campus_key',loc.source_key,'policy_key',pol.source_key)),'[]'::jsonb)
 INTO rows FROM selected s
 JOIN public.school_education_facts e ON e.school_id=p_school AND e.id=s.education_target_id
 LEFT JOIN public.school_education_facts pos ON pos.school_id=p_school AND pos.id=s.education_position_id AND pos.kind='class_position_definition'
 LEFT JOIN public.campus_locations loc ON loc.school_id=p_school AND loc.id=s.campus_location_id
 LEFT JOIN public.school_policies pol ON pol.school_id=p_school AND pol.id=s.policy_id
 WHERE EXISTS(SELECT 1 FROM public.school_fact_change_lineage l JOIN public.master_data_import_batches b ON b.school_id=l.school_id AND b.id=l.batch_id AND b.status='applied'
  WHERE l.school_id=p_school AND l.fact_kind=p_kind AND l.fact_source_key=p_key AND l.target_id=s.id AND l.target_version=s.version);
 RETURN jsonb_build_object('school_fact_version',app.school_fact_current_version(p_school),'records',rows);
END;
$f$;
REVOKE ALL ON FUNCTION app.school_fact_f4_dependency_selection(uuid,text,text,timestamptz) FROM PUBLIC,campus_ai_runtime,campus_ai_school_fact_writer,campus_ai_migration,campus_ai_db_owner;
GRANT EXECUTE ON FUNCTION app.school_fact_f4_dependency_selection(uuid,text,text,timestamptz) TO campus_ai_school_fact_reader;

"""


def upgrade():
    op.execute("SET LOCAL ROLE campus_ai_db_owner")
    op.execute(SSR_TEMPORAL_SQL)
    op.execute(SSR_COORDINATE_SQL)
    op.execute("GRANT SELECT(source_key,superseded_at) ON public.student_school_relationships TO campus_ai_context_definer")
    op.execute("GRANT CREATE ON SCHEMA app TO campus_ai_context_definer")
    op.execute("SET LOCAL ROLE campus_ai_context_definer")
    op.execute("ALTER FUNCTION app.school_fact_f3_scope_is_current(uuid,text,text[],timestamptz) RENAME TO school_fact_f3_scope_is_current_pre_f4")
    op.execute("REVOKE ALL ON FUNCTION app.school_fact_f3_scope_is_current_pre_f4(uuid,text,text[],timestamptz) FROM PUBLIC,campus_ai_school_fact_reader")
    op.execute("GRANT EXECUTE ON FUNCTION app.school_fact_f3_scope_is_current_pre_f4(uuid,text,text[],timestamptz) TO campus_ai_context_definer")
    op.execute("ALTER FUNCTION app.school_fact_f3_staff_target(uuid,text,text,boolean,timestamptz,text[]) RENAME TO school_fact_f3_staff_target_pre_f4")
    op.execute("REVOKE ALL ON FUNCTION app.school_fact_f3_staff_target_pre_f4(uuid,text,text,boolean,timestamptz,text[]) FROM PUBLIC,campus_ai_school_fact_reader")
    op.execute("GRANT EXECUTE ON FUNCTION app.school_fact_f3_staff_target_pre_f4(uuid,text,text,boolean,timestamptz,text[]) TO campus_ai_context_definer")
    # Preserve the exact v27 behavior for its original field family.
    op.execute("SET LOCAL ROLE campus_ai_db_owner")
    op.execute("GRANT CREATE ON SCHEMA app,app_private TO campus_ai_context_definer")
    op.execute("SET LOCAL ROLE campus_ai_context_definer")
    op.execute(
        "ALTER FUNCTION app.school_fact_family_correction_targets(uuid,uuid,uuid,bigint) RENAME TO school_fact_family_correction_targets_pre_f4"
    )
    op.execute(
        "REVOKE ALL ON FUNCTION app.school_fact_family_correction_targets_pre_f4(uuid,uuid,uuid,bigint) FROM PUBLIC,campus_ai_school_fact_writer"
    )
    op.execute("SET LOCAL ROLE campus_ai_db_owner")
    op.execute(
        "ALTER FUNCTION app.school_fact_f3_field_changed(uuid,text,text,bigint,text) RENAME TO school_fact_f3_field_changed_pre_f4"
    )
    op.execute(
        "REVOKE ALL ON FUNCTION app.school_fact_f3_field_changed_pre_f4(uuid,text,text,bigint,text) FROM PUBLIC,campus_ai_school_fact_writer"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION app.school_fact_f3_field_changed_pre_f4(uuid,text,text,bigint,text) TO campus_ai_context_definer"
    )
    op.execute("GRANT CREATE ON SCHEMA app,app_private TO campus_ai_context_definer")
    # Only definer gets the finite old directory projection needed by Guardian.
    op.execute(
        "GRANT SELECT (id,school_id,source_key,version) ON public.staff_assignments TO campus_ai_context_definer"
    )
    op.execute(
        "GRANT SELECT (id,school_id,owner_kind,organization_unit_id,location_id,contact_kind,contact_value,service_hours,lifecycle_status,visibility_scope,valid_from,valid_to,version) ON public.official_contact_points TO campus_ai_context_definer"
    )
    op.execute(
        "GRANT SELECT (id,school_id,source_key,source_kind,source_ref) ON public.school_fact_sources TO campus_ai_context_definer"
    )
    op.execute(
        "CREATE POLICY f4_contact_context_read ON public.official_contact_points FOR SELECT TO campus_ai_context_definer USING (school_id=app.require_school_database_anchor())"
    )
    op.execute("SET LOCAL ROLE campus_ai_context_definer")
    op.execute(STAFF_SQL)
    op.execute(SSR_READ_SQL)
    op.execute(SSR_TIMELINE_SQL)
    op.execute(FAMILY_SSR_SOURCE_SQL)
    op.execute(COORDINATES_SQL)
    op.execute(FAMILY_SQL)
    op.execute(CORRECTION_SQL)
    op.execute("SET LOCAL ROLE campus_ai_db_owner")
    op.execute(DEPENDENCY_SQL)
    op.execute("SET LOCAL ROLE campus_ai_context_definer")
    op.execute(DEPENDENCY_COORDINATES_SQL)
    op.execute(DEPENDENCY_SELECTION_SQL)
    op.execute("SET LOCAL ROLE campus_ai_db_owner")
    op.execute("REVOKE CREATE ON SCHEMA app,app_private FROM campus_ai_context_definer")


def downgrade():
    raise RuntimeError("Education read downgrade requires an explicit data-preserving plan")
