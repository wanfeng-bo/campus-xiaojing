"""F6 fixed selected-child and exact staff Reader projections."""

from alembic import op

revision = "20260922_43"
down_revision = "20260922_42"
branch_labels = None
depends_on = None

SQL = r"""
CREATE FUNCTION app_private.f6_allowed_fields(p_kind text) RETURNS text[]
LANGUAGE sql IMMUTABLE SET search_path=pg_catalog AS $f$
 SELECT CASE p_kind
 WHEN 'dormitory_building_profile' THEN ARRAY['location_source_key','usage_kind','display_name',
 'description','is_available','valid_to']
 WHEN 'dorm_room' THEN ARRAY['room_source_key','authorized_bed_capacity','accommodation_zone',
 'is_available','accessibility_tags','valid_to','room_name','building_name']
 WHEN 'dorm_bed' THEN ARRAY['room_source_key','business_code','display_name','availability',
 'valid_to','room_name','building_name']
 WHEN 'student_bed_residence' THEN ARRAY['student_source_key','bed_source_key','valid_to',
 'effective_at','student_name','bed_name','room_name','building_name','residence_mode',
 'enrollment_status'] END;
$f$;

CREATE FUNCTION app_private.f6_publication(p_school uuid,p_kind text,p_target uuid,p_version
 bigint)
RETURNS jsonb LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog AS $f$
 SELECT jsonb_build_object('kind','formal_publication','label',b.source_ref,'published_at',
 l.created_at)
 FROM public.school_fact_change_lineage l JOIN public.master_data_import_batches b
 ON b.school_id=l.school_id AND b.id=l.batch_id AND b.status='applied'
 JOIN public.master_data_import_units u ON u.school_id=l.school_id AND u.id=l.unit_id
 WHERE l.school_id=p_school AND l.fact_kind=p_kind AND l.target_id=p_target AND
 l.target_version<=p_version
 ORDER BY l.target_version DESC,l.created_at DESC LIMIT 1;
$f$;

CREATE FUNCTION app_private.f6_source_key(p_school uuid,p_kind text,p_target uuid)
RETURNS text LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog AS $f$
 SELECT l.fact_source_key FROM public.school_fact_change_lineage l JOIN
 public.master_data_import_batches b
 ON b.school_id=l.school_id AND b.id=l.batch_id AND b.status='applied'
 WHERE l.school_id=p_school AND l.fact_kind=p_kind AND l.target_id=p_target
 ORDER BY l.target_version DESC LIMIT 1;
$f$;

CREATE FUNCTION app_private.f6_location(p_school uuid,p_location uuid,p_at timestamptz)
RETURNS jsonb LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog AS $f$
 WITH RECURSIVE path AS (
 SELECT v.location_id,v.parent_location_id,v.display_name,0 AS depth,ARRAY[v.location_id] AS
 visited
 FROM public.campus_location_versions v WHERE v.school_id=p_school AND v.location_id=p_location
 AND v.status='confirmed' AND v.valid_from<=p_at AND (v.valid_to IS NULL OR p_at<v.valid_to)
 UNION ALL
 SELECT v.location_id,v.parent_location_id,v.display_name,p.depth+1,p.visited||v.location_id
 FROM path p JOIN public.campus_location_versions v ON v.school_id=p_school AND
 v.location_id=p.parent_location_id
 WHERE p.depth<32 AND NOT v.location_id=ANY(p.visited) AND v.status='confirmed'
 AND v.valid_from<=p_at AND (v.valid_to IS NULL OR p_at<v.valid_to)
 ), uses AS (
 SELECT p.depth,d.display_name,d.usage_kind FROM path p JOIN
 public.dormitory_building_profile_versions d
 ON d.school_id=p_school AND d.location_id=p.location_id AND d.superseded_at IS NULL
 AND d.valid_from<=p_at AND (d.valid_to IS NULL OR p_at<d.valid_to) AND d.is_available
 ) SELECT jsonb_build_object('room_name',(SELECT display_name FROM path WHERE depth=0),
 'building_name',(SELECT string_agg(display_name,' / ' ORDER BY depth DESC) FROM uses));
$f$;

CREATE FUNCTION app_private.f6_quality(p_school uuid,p_kind text,p_source text,p_person uuid,
 p_location uuid,p_at timestamptz,p_fields text[])
RETURNS jsonb LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog AS $f$
 SELECT COALESCE(jsonb_agg(jsonb_build_object('quality_kind',q.quality_kind,'target_fact_kind',
 q.target_fact_kind,
 'field_name',q.field_name,'coverage_status',q.coverage_status,'negative_code',q.negative_code,
 'dispute_status',q.dispute_status,'source_status',q.source_status,'verified_at',q.verified_at,
 'review_due_at',q.review_due_at,'valid_from',q.valid_from,'valid_to',q.valid_to,'version',
 q.version,
 'source',app_private.f6_publication(p_school,'fact_quality',q.id,q.version)) ORDER BY
 q.source_key),'[]'::jsonb)
 FROM public.school_fact_qualities q WHERE q.school_id=p_school AND q.target_fact_kind=p_kind
 AND (q.target_source_key=p_source OR q.target_source_key IS NULL AND
 ((q.owner_kind='person' AND q.person_id=p_person) OR (q.owner_kind='location' AND
 q.location_id=p_location)))
 AND ((q.owner_kind='person' AND q.person_id=p_person) OR (q.owner_kind='location' AND
 q.location_id=p_location)
 OR q.owner_kind='school' AND q.target_source_key=p_source)
 AND (q.field_name IS NULL OR q.field_name=ANY(p_fields)) AND q.valid_from<=p_at
 AND (q.valid_to IS NULL OR p_at<q.valid_to) AND (q.revoked_at IS NULL OR p_at<q.revoked_at)
 AND q.verified_at<=statement_timestamp() AND EXISTS(SELECT 1 FROM
 public.master_data_import_batches b
 WHERE b.school_id=p_school AND b.id=q.import_batch_id AND b.status='applied');
$f$;

-- Only temporal metadata is aggregated here, never student values. Together
-- with the formal publication version this detects future matches and renames
-- even when the current exact selection returned no row.
CREATE FUNCTION app_private.f6_next_boundary(p_school uuid,p_after timestamptz)
RETURNS timestamptz LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog AS $f$
 SELECT min(t) FROM (
 SELECT unnest(ARRAY[valid_from,valid_to]) t FROM public.student_bed_residences WHERE
 school_id=p_school AND revoked_at IS NULL
 UNION ALL SELECT unnest(ARRAY[valid_from,valid_to]) FROM public.dorm_bed_versions WHERE
 school_id=p_school AND superseded_at IS NULL
 UNION ALL SELECT unnest(ARRAY[valid_from,valid_to]) FROM public.dorm_room_profile_versions
 WHERE school_id=p_school AND superseded_at IS NULL
 UNION ALL SELECT unnest(ARRAY[valid_from,valid_to]) FROM
 public.dormitory_building_profile_versions WHERE school_id=p_school AND superseded_at IS NULL
 UNION ALL SELECT unnest(ARRAY[valid_from,valid_to]) FROM public.campus_location_versions WHERE
 school_id=p_school AND status='confirmed'
 UNION ALL SELECT unnest(ARRAY[valid_from,valid_to]) FROM public.school_person_names WHERE
 school_id=p_school AND status='confirmed'
 UNION ALL SELECT unnest(ARRAY[valid_from,valid_to]) FROM public.student_school_relationships
 WHERE school_id=p_school AND superseded_at IS NULL
 UNION ALL SELECT unnest(ARRAY[valid_from,valid_to,revoked_at,review_due_at]) FROM
 public.school_fact_qualities WHERE school_id=p_school
 AND target_fact_kind=ANY(ARRAY['dormitory_building_profile','dorm_room','dorm_bed',
 'student_bed_residence'])
 ) b WHERE t>p_after;
$f$;

CREATE FUNCTION app_private.f6_target(p_school uuid,p_kind text,p_key text,p_at timestamptz,
 p_fields text[])
RETURNS jsonb LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog AS $f$
DECLARE r record; result jsonb; content jsonb; place jsonb; target uuid; ver bigint; starts
 timestamptz; ends timestamptz;
 person uuid; location uuid; quality jsonb; dependency record; dependency_key text;
BEGIN
 IF p_kind='dormitory_building_profile' THEN
  SELECT d.* INTO r FROM public.dormitory_building_profile_versions d WHERE
 d.school_id=p_school AND d.source_key=p_key
  AND d.superseded_at IS NULL AND d.valid_from<=p_at AND (d.valid_to IS NULL OR
 p_at<d.valid_to);
  IF NOT FOUND THEN RETURN NULL; END IF;
  target:=r.id; ver:=r.version; starts:=r.valid_from; ends:=r.valid_to; location:=r.location_id;
  content:=jsonb_build_object('location_source_key',CASE WHEN
 'location_source_key'=ANY(p_fields) THEN (SELECT source_key FROM public.campus_locations WHERE
 school_id=p_school AND id=r.location_id) END,
  'usage_kind',CASE WHEN 'usage_kind'=ANY(p_fields) THEN r.usage_kind END,'display_name',CASE
 WHEN 'display_name'=ANY(p_fields) THEN r.display_name END,
  'description',CASE WHEN 'description'=ANY(p_fields) THEN r.description END,'is_available',
 CASE WHEN 'is_available'=ANY(p_fields) THEN r.is_available END);
 ELSIF p_kind='dorm_room' THEN
  SELECT d.* INTO r FROM public.dorm_room_profile_versions d WHERE d.school_id=p_school AND
 COALESCE(d.source_key,app_private.f6_source_key(p_school,p_kind,d.id))=p_key
  AND d.superseded_at IS NULL AND d.valid_from<=p_at AND (d.valid_to IS NULL OR
 p_at<d.valid_to);
  IF NOT FOUND THEN RETURN NULL; END IF;
  target:=r.id; ver:=r.version; starts:=r.valid_from; ends:=r.valid_to;
 location:=r.room_location_id;
  content:=jsonb_build_object('room_source_key',CASE WHEN 'room_source_key'=ANY(p_fields) THEN
 (SELECT source_key FROM public.campus_locations WHERE school_id=p_school AND
 id=r.room_location_id) END,
  'authorized_bed_capacity',CASE WHEN 'authorized_bed_capacity'=ANY(p_fields) THEN
 r.authorized_bed_capacity END,
  'accommodation_zone',CASE WHEN 'accommodation_zone'=ANY(p_fields) THEN r.accommodation_zone
 END,
  'is_available',CASE WHEN 'is_available'=ANY(p_fields) THEN r.is_available END,
 'accessibility_tags',CASE WHEN 'accessibility_tags'=ANY(p_fields) THEN r.accessibility_tags
 END);
 ELSIF p_kind='dorm_bed' THEN
  SELECT v.*,b.room_location_id FROM public.dorm_beds b JOIN public.dorm_bed_versions v
  ON v.school_id=b.school_id AND v.bed_id=b.id INTO r WHERE b.school_id=p_school AND
 b.source_key=p_key
  AND v.superseded_at IS NULL AND v.valid_from<=p_at AND (v.valid_to IS NULL OR
 p_at<v.valid_to);
  IF NOT FOUND THEN RETURN NULL; END IF;
  target:=r.bed_id; ver:=r.version; starts:=r.valid_from; ends:=r.valid_to;
 location:=r.room_location_id;
  content:=jsonb_build_object('room_source_key',CASE WHEN 'room_source_key'=ANY(p_fields) THEN
 (SELECT source_key FROM public.campus_locations WHERE school_id=p_school AND
 id=r.room_location_id) END,
  'business_code',CASE WHEN 'business_code'=ANY(p_fields) THEN r.business_code END,
 'display_name',CASE WHEN 'display_name'=ANY(p_fields) THEN r.display_name END,
  'availability',CASE WHEN 'availability'=ANY(p_fields) THEN r.availability END);
 ELSIF p_kind='student_bed_residence' THEN
  SELECT d.*,s.person_id,s.source_key AS student_key,b.source_key AS bed_key,b.room_location_id
 INTO r
  FROM public.student_bed_residences d JOIN public.students s ON s.school_id=d.school_id AND
 s.id=d.student_id
  JOIN public.dorm_beds b ON b.school_id=d.school_id AND b.id=d.bed_id
  WHERE d.school_id=p_school AND d.revoked_at IS NULL AND d.valid_from<=p_at AND (d.valid_to IS
 NULL OR p_at<d.valid_to)
  AND EXISTS(SELECT 1 FROM public.school_fact_change_lineage l JOIN
 public.master_data_import_batches mb ON mb.school_id=l.school_id AND mb.id=l.batch_id AND
 mb.status='applied'
   WHERE l.school_id=p_school AND l.fact_kind=p_kind AND l.fact_source_key=p_key AND
 l.target_id=d.id);
  IF NOT FOUND THEN RETURN NULL; END IF;
  target:=r.id; ver:=r.version; starts:=r.valid_from; ends:=r.valid_to; person:=r.person_id;
 location:=r.room_location_id;
  content:=jsonb_build_object('student_source_key',CASE WHEN 'student_source_key'=ANY(p_fields)
 THEN r.student_key END,
  'bed_source_key',CASE WHEN 'bed_source_key'=ANY(p_fields) THEN r.bed_key END,
  'student_name',CASE WHEN 'student_name'=ANY(p_fields) THEN (SELECT display_name FROM
 public.school_person_names n WHERE n.school_id=p_school AND n.person_id=r.person_id AND
 n.status='confirmed' AND n.valid_from<=p_at AND (n.valid_to IS NULL OR p_at<n.valid_to)) END,
  'bed_name',CASE WHEN 'bed_name'=ANY(p_fields) THEN (SELECT display_name FROM
 public.dorm_bed_versions v WHERE v.school_id=p_school AND v.bed_id=r.bed_id AND
 v.superseded_at IS NULL AND v.valid_from<=p_at AND (v.valid_to IS NULL OR p_at<v.valid_to))
 END,
  'residence_mode',CASE WHEN 'residence_mode'=ANY(p_fields) THEN (SELECT residence_mode FROM
 public.student_school_relationships e WHERE e.school_id=p_school AND e.student_id=r.student_id
 AND e.superseded_at IS NULL AND e.valid_from<=p_at AND (e.valid_to IS NULL OR
 p_at<e.valid_to)) END,
  'enrollment_status',CASE WHEN 'enrollment_status'=ANY(p_fields) THEN (SELECT
 enrollment_status FROM public.student_school_relationships e WHERE e.school_id=p_school AND
 e.student_id=r.student_id AND e.superseded_at IS NULL AND e.valid_from<=p_at AND (e.valid_to
 IS NULL OR p_at<e.valid_to)) END);
 ELSE RAISE EXCEPTION 'invalid F6 family' USING ERRCODE='22023'; END IF;
 IF 'room_name'=ANY(p_fields) OR 'building_name'=ANY(p_fields) THEN
  place:=app_private.f6_location(p_school,location,p_at);
  content:=content||jsonb_build_object('room_name',place->'room_name','building_name',
 place->'building_name');
 END IF;
 content:=content||jsonb_build_object('valid_to',ends,'effective_at',starts);
 SELECT COALESCE(jsonb_object_agg(k,v),'{}') INTO content FROM jsonb_each(content) fields(k,v)
 WHERE k=ANY(p_fields);
 quality:=app_private.f6_quality(p_school,p_kind,p_key,person,location,p_at,p_fields);
 IF p_kind='student_bed_residence' THEN
  IF 'bed_name'=ANY(p_fields) THEN
   quality:=quality||app_private.f6_quality(p_school,'dorm_bed',r.bed_key,NULL,location,p_at,
 ARRAY['display_name']);
  END IF;
  IF 'room_name'=ANY(p_fields) THEN
   SELECT COALESCE(d.source_key,app_private.f6_source_key(p_school,'dorm_room',d.id)) INTO
 dependency_key
   FROM public.dorm_room_profile_versions d WHERE d.school_id=p_school AND
 d.room_location_id=location AND d.superseded_at IS NULL
   AND d.valid_from<=p_at AND (d.valid_to IS NULL OR p_at<d.valid_to);
   quality:=quality||app_private.f6_quality(p_school,'dorm_room',dependency_key,NULL,location,
 p_at,ARRAY['room_source_key']);
  END IF;
  IF 'building_name'=ANY(p_fields) THEN
   FOR dependency IN SELECT n.source_key,n.location_id FROM
 public.dormitory_building_profile_versions n
   JOIN public.campus_locations l ON l.school_id=n.school_id AND l.id=n.location_id
   WHERE n.school_id=p_school AND n.superseded_at IS NULL AND n.valid_from<=p_at AND
 (n.valid_to IS NULL OR p_at<n.valid_to)
   AND app_private.f6_related_location(p_school,p_kind,p_key,l.source_key,p_at) LOOP
    quality:=quality||app_private.f6_quality(p_school,'dormitory_building_profile',
 dependency.source_key,NULL,dependency.location_id,p_at,ARRAY['display_name','usage_kind']);
   END LOOP;
  END IF;
 END IF;
 RETURN jsonb_build_object('source_key',p_key,'fact_kind',p_kind,'version',ver,'valid_from',
 starts,'valid_to',ends,
  'content',content,'source',app_private.f6_publication(p_school,p_kind,target,ver),'quality',
 quality);
END; $f$;

CREATE FUNCTION app_private.f6_related_location(p_school uuid,p_kind text,p_key text,p_parent
 text,p_at timestamptz)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog AS $f$
 WITH RECURSIVE seed(location_id) AS (
 SELECT d.location_id FROM public.dormitory_building_profile_versions d WHERE
 p_kind='dormitory_building_profile'
 AND d.school_id=p_school AND d.source_key=p_key AND d.superseded_at IS NULL AND
 d.valid_from<=p_at AND (d.valid_to IS NULL OR p_at<d.valid_to)
 UNION SELECT d.room_location_id FROM public.dorm_room_profile_versions d WHERE
 p_kind='dorm_room'
 AND d.school_id=p_school AND COALESCE(d.source_key,app_private.f6_source_key(p_school,p_kind,
 d.id))=p_key AND d.superseded_at IS NULL AND d.valid_from<=p_at AND (d.valid_to IS NULL OR
 p_at<d.valid_to)
 UNION SELECT b.room_location_id FROM public.dorm_beds b WHERE p_kind='dorm_bed' AND
 b.school_id=p_school AND b.source_key=p_key
 UNION SELECT b.room_location_id FROM public.student_bed_residences d JOIN public.dorm_beds b
 ON b.school_id=d.school_id AND b.id=d.bed_id
 WHERE p_kind='student_bed_residence' AND d.school_id=p_school AND d.revoked_at IS NULL AND
 d.valid_from<=p_at AND (d.valid_to IS NULL OR p_at<d.valid_to)
 AND app_private.f6_source_key(p_school,p_kind,d.id)=p_key
 ), path(location_id,depth,visited) AS (
 SELECT location_id,0,ARRAY[location_id] FROM seed
 UNION ALL SELECT v.parent_location_id,p.depth+1,p.visited||v.parent_location_id FROM path p
 JOIN public.campus_location_versions v
 ON v.school_id=p_school AND v.location_id=p.location_id AND v.status='confirmed' AND
 v.valid_from<=p_at AND (v.valid_to IS NULL OR p_at<v.valid_to)
 WHERE v.parent_location_id IS NOT NULL AND p.depth<32 AND NOT
 v.parent_location_id=ANY(p.visited)
 ) SELECT EXISTS(SELECT 1 FROM path p JOIN public.campus_locations l ON l.school_id=p_school
 AND l.id=p.location_id WHERE l.source_key=p_parent);
$f$;

CREATE FUNCTION app.school_fact_f6_targets(p_school uuid,p_kind text,p_objects text[],p_fields
 text[],p_at timestamptz,p_name text,p_related_kind text,p_related_key text,p_offset integer,
 p_limit integer)
RETURNS jsonb LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog AS $f$
DECLARE rows jsonb:='[]'; item jsonb; k text; name_field text; related_field text; more
 boolean; ambiguous boolean:=false;
BEGIN
 IF SESSION_USER<>'campus_ai_school_fact_reader' OR p_school IS DISTINCT FROM
 app.require_school_database_anchor() THEN RAISE EXCEPTION 'F6 Reader denied' USING
 ERRCODE='28000'; END IF;
 IF app_private.f6_allowed_fields(p_kind) IS NULL OR p_objects IS NULL OR
 cardinality(p_objects)>100 OR array_position(p_objects,NULL) IS NOT NULL
 OR p_fields IS NULL OR cardinality(p_fields)=0 OR NOT
 p_fields<@app_private.f6_allowed_fields(p_kind) OR p_at IS NULL
 OR p_offset IS NULL OR p_offset<0 OR p_offset>10000 OR p_limit IS NULL OR p_limit<1 OR
 p_limit>100 THEN RAISE EXCEPTION 'invalid fixed F6 selection' USING ERRCODE='22023'; END IF;
 name_field:=CASE p_kind WHEN 'student_bed_residence' THEN 'student_name' WHEN 'dorm_room' THEN
 'room_name' ELSE 'display_name' END;
 IF p_name IS NOT NULL AND NOT name_field=ANY(p_fields) THEN RAISE EXCEPTION
 'F6 name filter denied' USING ERRCODE='28000'; END IF;
 related_field:=CASE p_related_kind WHEN 'student' THEN 'student_source_key' WHEN 'dorm_bed'
 THEN 'bed_source_key' WHEN 'location' THEN CASE WHEN p_kind='dormitory_building_profile' THEN
 'location_source_key' WHEN p_kind='student_bed_residence' THEN 'room_name' ELSE
 'room_source_key' END END;
 IF p_related_kind IS NOT NULL AND (related_field IS NULL OR NOT related_field=ANY(p_fields) OR
 p_related_key IS NULL) THEN RAISE EXCEPTION 'F6 relation filter denied' USING ERRCODE='28000';
 END IF;
 FOR k IN SELECT DISTINCT key FROM unnest(p_objects) key ORDER BY key LOOP
  item:=app_private.f6_target(p_school,p_kind,k,p_at,p_fields);
  IF item IS NOT NULL AND (p_name IS NULL OR item->'content'->>name_field=p_name)
  AND (p_related_kind IS NULL OR CASE WHEN p_related_kind='location' THEN
 app_private.f6_related_location(p_school,p_kind,k,p_related_key,p_at) ELSE
 item->'content'->>related_field=p_related_key END) THEN rows:=rows||jsonb_build_array(item);
 END IF;
 END LOOP;
 ambiguous:=p_name IS NOT NULL AND jsonb_array_length(rows)>1;
 more:=jsonb_array_length(rows)>p_offset+p_limit;
 SELECT COALESCE(jsonb_agg(value ORDER BY n),'[]') INTO rows FROM jsonb_array_elements(rows)
 WITH ORDINALITY r(value,n) WHERE n>p_offset AND n<=p_offset+p_limit;
 RETURN jsonb_build_object('status',CASE WHEN ambiguous THEN 'ambiguous' WHEN
 jsonb_array_length(rows)=0 THEN 'no_current_records' ELSE 'available' END,
 'records',rows,'has_more',more,'as_of',p_at,'school_fact_version',
 app.school_fact_current_version(p_school),
 'next_boundary',app_private.f6_next_boundary(p_school,statement_timestamp()));
END; $f$;
REVOKE ALL ON FUNCTION app.school_fact_f6_targets(uuid,text,text[],text[],timestamptz,text,text,
 text,integer,integer) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app.school_fact_f6_targets(uuid,text,text[],text[],timestamptz,text,
 text,text,integer,integer) TO campus_ai_school_fact_reader;

CREATE FUNCTION app.school_fact_f6_maintenance_target(p_school uuid,p_kind text,p_key text,
 p_fields text[],p_creation boolean)
RETURNS jsonb LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog AS $f$
DECLARE ver bigint; target uuid; segments jsonb:='[]'; item jsonb; row record; ends timestamptz;
BEGIN
 IF SESSION_USER<>'campus_ai_school_fact_reader' OR p_school IS DISTINCT FROM
 app.require_school_database_anchor() THEN RAISE EXCEPTION 'F6 maintenance Reader denied' USING
 ERRCODE='28000'; END IF;
 IF app_private.f6_allowed_fields(p_kind) IS NULL OR p_key IS NULL OR length(p_key)=0 OR
 length(p_key)>255
 OR p_fields IS NULL OR cardinality(p_fields)=0 OR NOT
 p_fields<@app_private.f6_allowed_fields(p_kind) OR p_creation IS NULL
 THEN RAISE EXCEPTION 'invalid F6 maintenance selection' USING ERRCODE='22023'; END IF;
 SELECT l.target_version,l.target_id INTO ver,target FROM public.school_fact_change_lineage l
 JOIN public.master_data_import_batches b ON b.school_id=l.school_id AND b.id=l.batch_id AND
 b.status='applied'
 WHERE l.school_id=p_school AND l.fact_kind=p_kind AND l.fact_source_key=p_key ORDER BY
 l.target_version DESC LIMIT 1;
 ver:=COALESCE(ver,0);
 IF p_creation AND ver<>0 OR NOT p_creation AND ver=0 THEN RAISE EXCEPTION 'F6 target changed'
 USING ERRCODE='40001'; END IF;
 IF NOT p_creation THEN
 FOR row IN
  SELECT d.valid_from,d.valid_to FROM public.dormitory_building_profile_versions d WHERE
 p_kind='dormitory_building_profile'
   AND d.school_id=p_school AND d.source_key=p_key AND d.superseded_at IS NULL AND d.version=ver
  UNION ALL SELECT d.valid_from,d.valid_to FROM public.dorm_room_profile_versions d WHERE
 p_kind='dorm_room'
   AND d.school_id=p_school AND COALESCE(d.source_key,app_private.f6_source_key(p_school,p_kind,
 d.id))=p_key AND d.superseded_at IS NULL AND d.version=ver
  UNION ALL SELECT v.valid_from,v.valid_to FROM public.dorm_bed_versions v JOIN
 public.dorm_beds b ON b.school_id=v.school_id AND b.id=v.bed_id
   WHERE p_kind='dorm_bed' AND b.school_id=p_school AND b.source_key=p_key AND v.superseded_at
 IS NULL AND v.version=ver
  UNION ALL SELECT d.valid_from,d.valid_to FROM public.student_bed_residences d WHERE
 p_kind='student_bed_residence'
   AND d.school_id=p_school AND d.id=target AND d.revoked_at IS NULL
  ORDER BY valid_from
 LOOP
  IF jsonb_array_length(segments)>=1000 THEN RAISE EXCEPTION
 'F6 timeline exceeds bounded projection' USING ERRCODE='54000'; END IF;
  item:=app_private.f6_target(p_school,p_kind,p_key,row.valid_from,p_fields);
  IF item IS NULL THEN RAISE EXCEPTION 'F6 timeline cannot be verified' USING ERRCODE='40001';
 END IF;
  segments:=segments||jsonb_build_array(jsonb_build_object('effective_at',row.valid_from,
 'valid_to',row.valid_to,'content',item->'content','source',item->'source'));
 END LOOP;
 END IF;
 RETURN jsonb_build_object('school_fact_version',app.school_fact_current_version(p_school),
 'records',jsonb_build_array(
  jsonb_build_object('source_key',p_key,'fact_kind',p_kind,'version',ver,'segments',segments)));
END; $f$;
REVOKE ALL ON FUNCTION app.school_fact_f6_maintenance_target(uuid,text,text,text[],boolean)
 FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app.school_fact_f6_maintenance_target(uuid,text,text,text[],boolean)
 TO campus_ai_school_fact_reader;

CREATE FUNCTION app.school_fact_f6_student_impacts(p_school uuid,p_student text,p_objects
 text[],p_at timestamptz)
RETURNS jsonb LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog AS $f$
DECLARE student uuid; d record; key text; rows jsonb:='[]';
BEGIN
 IF SESSION_USER<>'campus_ai_school_fact_reader' OR p_school IS DISTINCT FROM
 app.require_school_database_anchor() THEN RAISE EXCEPTION 'F6 impact Reader denied' USING
 ERRCODE='28000'; END IF;
 IF p_student IS NULL OR p_objects IS NULL OR cardinality(p_objects)>100 OR p_at IS NULL THEN
 RAISE EXCEPTION 'invalid F6 impact selection' USING ERRCODE='22023'; END IF;
 SELECT s.id INTO student FROM public.students s WHERE s.school_id=p_school AND
 s.source_key=p_student;
 IF student IS NULL THEN RAISE EXCEPTION 'F6 impact student unavailable' USING ERRCODE='22023';
 END IF;
 -- Verify the complete impacted set before projecting any student or bed value.
 IF EXISTS(SELECT 1 FROM public.student_bed_residences candidate WHERE
 candidate.school_id=p_school AND candidate.student_id=student
  AND candidate.revoked_at IS NULL AND (candidate.valid_to IS NULL OR candidate.valid_to>p_at)
  AND COALESCE(app_private.f6_source_key(p_school,'student_bed_residence',
 candidate.id)=ANY(p_objects),false)=false)
 THEN RAISE EXCEPTION 'incomplete F6 impact authority' USING ERRCODE='28000'; END IF;
 FOR d IN SELECT r.*,b.source_key AS bed_key FROM public.student_bed_residences r
 JOIN public.dorm_beds b ON b.school_id=r.school_id AND b.id=r.bed_id WHERE
 r.school_id=p_school AND r.student_id=student
 AND r.revoked_at IS NULL AND (r.valid_to IS NULL OR r.valid_to>p_at) ORDER BY r.valid_from,
 r.id LOOP
  key:=app_private.f6_source_key(p_school,'student_bed_residence',d.id);
  rows:=rows||jsonb_build_array(jsonb_build_object('source_key',key,'fact_kind',
 'student_bed_residence','version',(SELECT max(l.target_version) FROM
 public.school_fact_change_lineage l JOIN public.master_data_import_batches b ON
 b.school_id=l.school_id AND b.id=l.batch_id AND b.status='applied' WHERE l.school_id=p_school
 AND l.fact_kind='student_bed_residence' AND l.fact_source_key=key),
   'student_source_key',p_student,'bed_source_key',d.bed_key,'valid_from',d.valid_from,
 'valid_to',d.valid_to,'revoked_at',d.revoked_at));
 END LOOP;
 RETURN jsonb_build_object('school_fact_version',app.school_fact_current_version(p_school),
 'records',rows,
  'next_boundary',app_private.f6_next_boundary(p_school,statement_timestamp()));
END; $f$;
REVOKE ALL ON FUNCTION app.school_fact_f6_student_impacts(uuid,text,text[],timestamptz) FROM
 PUBLIC;
GRANT EXECUTE ON FUNCTION app.school_fact_f6_student_impacts(uuid,text,text[],timestamptz) TO
 campus_ai_school_fact_reader;

-- The caller obtains these keys from the F3 Reader scope, never an F6 scope.
-- This function proves completeness without exposing an unscoped relation.
CREATE FUNCTION app.school_fact_f6_student_relationship_keys(p_school uuid,p_student text,
 p_objects text[])
RETURNS jsonb LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog AS $f$
DECLARE student uuid; keys text[];
BEGIN
 IF SESSION_USER<>'campus_ai_school_fact_reader' OR p_school IS DISTINCT FROM
 app.require_school_database_anchor() THEN RAISE EXCEPTION 'F3 eligibility Reader denied' USING
 ERRCODE='28000'; END IF;
 IF p_student IS NULL OR p_objects IS NULL OR cardinality(p_objects)>100 OR
 array_position(p_objects,NULL) IS NOT NULL THEN RAISE EXCEPTION
 'invalid F3 eligibility selection' USING ERRCODE='22023'; END IF;
 SELECT s.id INTO student FROM public.students s WHERE s.school_id=p_school AND
 s.source_key=p_student;
 IF student IS NULL THEN RAISE EXCEPTION 'student unavailable' USING ERRCODE='28000'; END IF;
 SELECT array_agg(DISTINCT r.source_key ORDER BY r.source_key) INTO keys FROM
 public.student_school_relationships r
 WHERE r.school_id=p_school AND r.student_id=student AND r.superseded_at IS NULL;
 IF EXISTS(SELECT 1 FROM public.student_school_relationships r WHERE r.school_id=p_school AND
 r.student_id=student
 AND r.superseded_at IS NULL AND COALESCE(r.source_key=ANY(p_objects),false)=false)
 THEN RAISE EXCEPTION 'incomplete F3 eligibility authority' USING ERRCODE='28000'; END IF;
 RETURN jsonb_build_object('school_fact_version',app.school_fact_current_version(p_school),
 'source_keys',COALESCE(keys,ARRAY[]::text[]));
END; $f$;
REVOKE ALL ON FUNCTION app.school_fact_f6_student_relationship_keys(uuid,text,text[]) FROM
 PUBLIC;
GRANT EXECUTE ON FUNCTION app.school_fact_f6_student_relationship_keys(uuid,text,text[]) TO
 campus_ai_school_fact_reader;

CREATE FUNCTION app.school_fact_f6_quality_coordinates(p_school uuid,p_key text)
RETURNS jsonb LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog AS $f$
DECLARE result jsonb;
BEGIN
 IF SESSION_USER<>'campus_ai_school_fact_reader' OR p_school IS DISTINCT FROM
 app.require_school_database_anchor() THEN RAISE EXCEPTION 'F6 quality Reader denied' USING
 ERRCODE='28000'; END IF;
 IF p_key IS NULL OR length(p_key)=0 OR length(p_key)>255 THEN RAISE EXCEPTION
 'invalid quality key' USING ERRCODE='22023'; END IF;
 SELECT jsonb_build_object('version',q.version,'content',jsonb_build_object('target_fact_kind',
 q.target_fact_kind,
 'target_source_key',q.target_source_key,'replacement_source_key',q.replacement_source_key,
 'field_name',q.field_name,
 'owner_kind',q.owner_kind,'negative_code',q.negative_code,'owner_source_key',CASE q.owner_kind
 WHEN 'person' THEN (SELECT source_key FROM public.school_people p WHERE p.school_id=p_school
 AND p.id=q.person_id)
 WHEN 'location' THEN (SELECT source_key FROM public.campus_locations l WHERE
 l.school_id=p_school AND l.id=q.location_id)
 WHEN 'school' THEN (SELECT source_key FROM public.schools s WHERE s.id=p_school) END)) INTO
 result
 FROM public.school_fact_qualities q WHERE q.school_id=p_school AND q.source_key=p_key
 AND EXISTS(SELECT 1 FROM public.master_data_import_batches b WHERE b.school_id=p_school AND
 b.id=q.import_batch_id AND b.status='applied')
 ORDER BY q.version DESC LIMIT 1;
 RETURN result;
END; $f$;
REVOKE ALL ON FUNCTION app.school_fact_f6_quality_coordinates(uuid,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app.school_fact_f6_quality_coordinates(uuid,text) TO
 campus_ai_school_fact_reader;

CREATE FUNCTION app.school_fact_f6_quality_detail(p_school uuid,p_key text)
RETURNS jsonb LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog AS $f$
DECLARE result jsonb;
BEGIN
 IF SESSION_USER<>'campus_ai_school_fact_reader' OR p_school IS DISTINCT FROM
 app.require_school_database_anchor() THEN RAISE EXCEPTION 'F6 quality Reader denied' USING
 ERRCODE='28000'; END IF;
 IF p_key IS NULL OR length(p_key)=0 OR length(p_key)>255 THEN RAISE EXCEPTION
 'invalid quality key' USING ERRCODE='22023'; END IF;
 SELECT jsonb_build_object('version',q.version,'valid_from',q.valid_from,'valid_to',q.valid_to,
 'source',app_private.f6_publication(p_school,'fact_quality',q.id,q.version),
 'content',jsonb_strip_nulls(jsonb_build_object('quality_kind',q.quality_kind,
 'target_fact_kind',q.target_fact_kind,
 'owner_kind',q.owner_kind,'person_source_key',CASE WHEN q.owner_kind='person' THEN (SELECT
 p.source_key FROM public.school_people p WHERE p.school_id=p_school AND p.id=q.person_id) END,
 'location_source_key',CASE WHEN q.owner_kind='location' THEN (SELECT l.source_key FROM
 public.campus_locations l WHERE l.school_id=p_school AND l.id=q.location_id) END,
 'basis_reference',q.basis_reference,'responsibility',q.responsibility,'verified_at',
 q.verified_at,
 'target_source_key',q.target_source_key,'replacement_source_key',q.replacement_source_key,
 'field_name',q.field_name,
 'evidence_reference',q.evidence_reference,'coverage_status',q.coverage_status,'negative_code',
 q.negative_code,
 'dispute_status',q.dispute_status,'source_status',q.source_status,'last_success_at',
 q.last_success_at,
 'review_due_at',q.review_due_at,'valid_to',q.valid_to))) INTO result
 FROM public.school_fact_qualities q WHERE q.school_id=p_school AND q.source_key=p_key
 AND q.target_fact_kind=ANY(ARRAY['dormitory_building_profile','dorm_room','dorm_bed',
 'student_bed_residence'])
 AND EXISTS(SELECT 1 FROM public.master_data_import_batches b WHERE b.school_id=p_school AND
 b.id=q.import_batch_id AND b.status='applied')
 ORDER BY q.version DESC LIMIT 1;
 RETURN result;
END; $f$;
REVOKE ALL ON FUNCTION app.school_fact_f6_quality_detail(uuid,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app.school_fact_f6_quality_detail(uuid,text) TO
 campus_ai_school_fact_reader;

CREATE FUNCTION app.school_fact_f6_quality_owners_current(p_school uuid,p_kind text,p_owners
 text[],p_keys text[],p_at timestamptz)
RETURNS boolean LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog AS $f$
DECLARE i integer; owner text; target uuid;
BEGIN
 IF SESSION_USER<>'campus_ai_school_fact_reader' OR p_school IS DISTINCT FROM
 app.require_school_database_anchor() THEN RAISE EXCEPTION 'F6 quality owner Reader denied'
 USING ERRCODE='28000'; END IF;
 IF p_at IS NULL OR p_keys IS NULL OR p_owners IS NULL OR
 cardinality(p_keys)<>cardinality(p_owners) OR cardinality(p_keys)>100 OR array_position(p_keys,
 NULL) IS NOT NULL OR array_position(p_owners,NULL) IS NOT NULL THEN RETURN false; END IF;
 IF cardinality(p_keys)=0 THEN RETURN true; END IF;
 FOR i IN 1..cardinality(p_keys) LOOP
  owner:=p_owners[i]; target:=NULL;
  IF p_kind='student_bed_residence' AND owner='person' THEN
   SELECT p.id INTO target FROM public.school_people p WHERE p.school_id=p_school AND
 p.source_key=p_keys[i]
   AND EXISTS(SELECT 1 FROM public.students s WHERE s.school_id=p_school AND s.person_id=p.id
 AND app_private.school_fact_identity_root_is_effective_at(p_school,'student',s.id,p_at));
  ELSIF p_kind=ANY(ARRAY['dormitory_building_profile','dorm_room','dorm_bed']) AND
 owner='location' THEN
   SELECT l.id INTO target FROM public.campus_locations l WHERE l.school_id=p_school AND
 l.source_key=p_keys[i];
  ELSIF p_kind='dormitory_building_profile' AND owner='school' THEN
   SELECT s.id INTO target FROM public.schools s WHERE s.id=p_school AND s.source_key=p_keys[i];
  ELSE RETURN false; END IF;
  IF target IS NULL THEN RETURN false; END IF;
  IF owner='location' THEN
   IF NOT app.school_fact_location_history_is_resolvable(p_school,target,p_at) THEN RETURN
 false; END IF;
  ELSIF NOT app_private.school_fact_identity_root_is_effective_at(p_school,owner,target,p_at)
 THEN RETURN false; END IF;
 END LOOP;
 RETURN true;
END; $f$;
REVOKE ALL ON FUNCTION app.school_fact_f6_quality_owners_current(uuid,text,text[],text[],
 timestamptz) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app.school_fact_f6_quality_owners_current(uuid,text,text[],text[],
 timestamptz) TO campus_ai_school_fact_reader;

CREATE FUNCTION app.school_fact_f6_scope_is_current(p_school uuid,p_kind text,p_objects text[],
 p_at timestamptz)
RETURNS boolean LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog AS $f$
BEGIN
 IF SESSION_USER<>'campus_ai_school_fact_reader' OR p_school IS DISTINCT FROM
 app.require_school_database_anchor() THEN RAISE EXCEPTION 'F6 scope Reader denied' USING
 ERRCODE='28000'; END IF;
 IF app_private.f6_allowed_fields(p_kind) IS NULL OR p_objects IS NULL OR
 cardinality(p_objects)>100 OR array_position(p_objects,NULL) IS NOT NULL OR p_at IS NULL THEN
 RETURN false; END IF;
 RETURN app_private.school_fact_identity_root_is_effective_at(p_school,'school',p_school,p_at)
 AND NOT EXISTS(SELECT 1 FROM unnest(p_objects) k WHERE NOT EXISTS(SELECT 1 FROM
 public.school_fact_change_lineage l
 JOIN public.master_data_import_batches b ON b.school_id=l.school_id AND b.id=l.batch_id AND
 b.status='applied'
 WHERE l.school_id=p_school AND l.fact_kind=p_kind AND l.fact_source_key=k));
END; $f$;
REVOKE ALL ON FUNCTION app.school_fact_f6_scope_is_current(uuid,text,text[],timestamptz) FROM
 PUBLIC;
GRANT EXECUTE ON FUNCTION app.school_fact_f6_scope_is_current(uuid,text,text[],timestamptz) TO
 campus_ai_school_fact_reader;

CREATE FUNCTION app.school_fact_family_residence_stamp(p_school uuid,p_adult uuid,p_relation
 uuid,p_version bigint)
RETURNS bigint LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog AS $f$
BEGIN
 IF SESSION_USER<>'campus_ai_school_fact_reader' OR p_school IS DISTINCT FROM
 app.require_school_database_anchor()
 OR NOT app_private.family_relationship_is_authoritative(p_school,p_adult,p_relation,p_version,
 statement_timestamp())
 OR NOT EXISTS(SELECT 1 FROM public.guardian_relationships r WHERE r.school_id=p_school AND
 r.id=p_relation AND r.relationship_kind='verified_guardian')
 THEN RAISE EXCEPTION 'F6 family Reader denied' USING ERRCODE='28000'; END IF;
 RETURN app.school_fact_current_version(p_school);
END; $f$;
REVOKE ALL ON FUNCTION app.school_fact_family_residence_stamp(uuid,uuid,uuid,bigint) FROM
 PUBLIC;
GRANT EXECUTE ON FUNCTION app.school_fact_family_residence_stamp(uuid,uuid,uuid,bigint) TO
 campus_ai_school_fact_reader;

CREATE FUNCTION app.school_fact_family_residence(p_school uuid,p_adult uuid,p_relation uuid,
 p_version bigint,p_at timestamptz)
RETURNS jsonb LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog AS $f$
DECLARE basis record; at_time timestamptz:=COALESCE(p_at,statement_timestamp()); person uuid;
 mode text; enrollment text;
 rows jsonb:='[]'; d record; item jsonb; fields text[]:=ARRAY['student_name','bed_name',
 'room_name','building_name']; status text; qualities jsonb; legacy boolean:=false; total
 integer:=0;
BEGIN
 PERFORM app.school_fact_family_residence_stamp(p_school,p_adult,p_relation,p_version);
 SELECT * INTO basis FROM app.school_fact_family_authority(p_school,p_adult,p_relation,
 p_version);
 IF NOT FOUND OR at_time<basis.valid_from OR basis.valid_to IS NOT NULL AND
 at_time>=basis.valid_to THEN RAISE EXCEPTION 'F6 family time denied' USING ERRCODE='28000';
 END IF;
 SELECT s.person_id INTO person FROM public.students s WHERE s.school_id=p_school AND
 s.id=basis.student_id;
 SELECT e.residence_mode,e.enrollment_status INTO mode,enrollment FROM
 public.student_school_relationships e
 WHERE e.school_id=p_school AND e.student_id=basis.student_id AND e.superseded_at IS NULL AND
 e.valid_from<=at_time AND (e.valid_to IS NULL OR at_time<e.valid_to);
 FOR d IN SELECT r.id FROM public.student_bed_residences r WHERE r.school_id=p_school AND
 r.student_id=basis.student_id
 AND r.revoked_at IS NULL AND r.valid_from<=at_time AND (r.valid_to IS NULL OR
 at_time<r.valid_to) ORDER BY r.id LIMIT 2 LOOP
  total:=total+1;
  item:=app_private.f6_target(p_school,'student_bed_residence',
 app_private.f6_source_key(p_school,'student_bed_residence',d.id),at_time,fields);
  IF item IS NOT NULL THEN rows:=rows||jsonb_build_array((item-'content')||(item->'content'));
 ELSE legacy:=true; END IF;
 END LOOP;
 status:=CASE WHEN total>1 THEN 'conflict' WHEN legacy THEN 'partial' WHEN
 jsonb_array_length(rows)=0 THEN 'no_current_records'
 WHEN mode IS DISTINCT FROM 'boarding' OR enrollment NOT IN ('enrolled','suspended') THEN
 'conflict' ELSE 'available' END;
 IF total>1 THEN rows:='[]'; END IF;
 qualities:=app_private.f6_quality(p_school,'student_bed_residence',NULL,person,NULL,at_time,
 ARRAY['student_source_key','bed_source_key','valid_to','effective_at']);
 RETURN jsonb_build_object('status',status,'records',rows,'student_id',basis.student_id,
 'student_version',basis.student_version,
 'relationship_version',basis.relationship_version,'residence_mode',mode,'enrollment_status',
 enrollment,'quality',qualities,
 'as_of',at_time,'school_fact_version',app.school_fact_current_version(p_school),
 'next_boundary',app_private.f6_next_boundary(p_school,statement_timestamp()));
END; $f$;
REVOKE ALL ON FUNCTION app.school_fact_family_residence(uuid,uuid,uuid,bigint,timestamptz) FROM
 PUBLIC;
GRANT EXECUTE ON FUNCTION app.school_fact_family_residence(uuid,uuid,uuid,bigint,timestamptz)
 TO campus_ai_school_fact_reader;

REVOKE ALL ON FUNCTION app_private.f6_allowed_fields(text),app_private.f6_publication(uuid,text,
 uuid,bigint),app_private.f6_source_key(uuid,text,uuid),
app_private.f6_location(uuid,uuid,timestamptz),app_private.f6_quality(uuid,text,text,uuid,uuid,
 timestamptz,text[]),
app_private.f6_next_boundary(uuid,timestamptz),app_private.f6_target(uuid,text,text,timestamptz,
 text[]),app_private.f6_related_location(uuid,text,text,text,timestamptz) FROM PUBLIC;
"""


def upgrade():
    op.execute("SET LOCAL ROLE campus_ai_db_owner")
    for table in (
        "dormitory_building_profile_versions",
        "dorm_room_profile_versions",
        "dorm_beds",
        "dorm_bed_versions",
        "student_bed_residences",
    ):
        op.execute(f"GRANT SELECT ON public.{table} TO campus_ai_context_definer")
        # Revision42 owns the new N25 policy; existing dorm tables acquire
        # their narrowly anchored Reader-definer policy here.
        if table != "dormitory_building_profile_versions":
            op.execute(
                f"CREATE POLICY f6_context_read ON public.{table} FOR SELECT "
                "TO campus_ai_context_definer "
                "USING (school_id=app.require_school_database_anchor())"
            )
    op.execute(
        "GRANT SELECT(source_ref) ON public.master_data_import_batches TO campus_ai_context_definer"
    )
    op.execute(
        "GRANT SELECT(person_id,location_id) ON public.school_fact_qu"
        "alities TO campus_ai_context_definer"
    )
    op.execute(
        "GRANT SELECT(parent_location_id) ON public.campus_location_v"
        "ersions TO campus_ai_context_definer"
    )
    op.execute("GRANT CREATE ON SCHEMA app,app_private TO campus_ai_context_definer")
    op.execute("SET LOCAL ROLE campus_ai_context_definer")
    op.execute(SQL)
    op.execute("SET LOCAL ROLE campus_ai_db_owner")
    op.execute("REVOKE CREATE ON SCHEMA app,app_private FROM campus_ai_context_definer")


def downgrade():
    raise RuntimeError("F6 requires a guarded compatible artifact; formal history is retained")
