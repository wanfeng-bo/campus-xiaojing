# ruff: noqa: E501
"""Frozen F5 typed teaching storage and finite Reader; preserve F4 SQL and all RLS."""

from alembic import op

revision = "20260914_32"
down_revision = "20260913_31"
branch_labels = None
depends_on = None

STORAGE_SQL = r"""
ALTER TABLE public.school_fact_qualities ADD COLUMN teaching_role varchar(20);
ALTER TABLE public.school_fact_qualities DROP CONSTRAINT ck_school_fact_qualities_negative;
ALTER TABLE public.school_fact_qualities ADD CONSTRAINT ck_school_fact_qualities_negative CHECK (negative_code IS NULL OR ((negative_code = 'no_office' AND target_fact_kind IN ('organization_location','person_workplace') AND purpose = 'office') OR (negative_code = 'no_reception' AND target_fact_kind IN ('organization_location','person_workplace') AND purpose = 'reception') OR (negative_code = 'no_official_channel' AND target_fact_kind = 'official_contact' AND contact_kind IN ('phone','email')) OR (negative_code = 'no_service' AND target_fact_kind = 'school_service') OR (negative_code = 'vacant_position' AND target_fact_kind = 'student_class_position' AND education_position_id IS NOT NULL) OR (negative_code = 'no_class_teacher' AND target_fact_kind = 'class_teacher_assignment') OR (negative_code = 'no_fixed_location' AND target_fact_kind = 'class_location') OR (negative_code='no_teaching_assignment' AND target_fact_kind='teaching_assignment' AND education_target_id IS NOT NULL AND teaching_role IN ('lead','co_teacher') AND education_position_id IS NULL AND target_source_key IS NULL AND replacement_source_key IS NULL)) IS TRUE);
ALTER TABLE public.school_fact_qualities DROP CONSTRAINT ck_school_fact_qualities_target;
ALTER TABLE public.school_fact_qualities ADD CONSTRAINT ck_school_fact_qualities_target CHECK (target_fact_kind IN ('school','person','organization','assignment','organization_campus_relationship','location','location_alias','person_contact','official_contact','policy','policy_scope','policy_version','organization_profile','organization_membership','organization_location','person_workplace','school_service','class_lineage','class_location','class_placement','class_position_definition','class_teacher_assignment','course_subject','education_calendar_rule','education_class','education_cohort','education_grade','education_grade_group','education_offering','education_stage','education_term','offering_member_source','student_class_membership','student_class_position','student_cohort_membership','student_grade_placement','teaching_assignment','teaching_course','teaching_group','teaching_group_member','teaching_offering','teaching_organization_scope','teaching_subject'));
ALTER TABLE public.school_fact_qualities ADD CONSTRAINT ck_f5_quality_teaching_role CHECK (teaching_role IS NULL OR (target_fact_kind='teaching_assignment' AND teaching_role IN ('lead','co_teacher')) IS TRUE);
CREATE FUNCTION app_private.f5_quality_owner() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog AS $f$
BEGIN
 IF NEW.negative_code='no_teaching_assignment' AND NOT EXISTS(SELECT 1 FROM public.school_education_facts f WHERE f.school_id=NEW.school_id AND f.id=NEW.education_target_id AND f.kind='teaching_offering') THEN RAISE EXCEPTION 'teaching vacancy owner mismatch' USING ERRCODE='23514'; END IF;
 RETURN NEW;
END; $f$;
REVOKE ALL ON FUNCTION app_private.f5_quality_owner() FROM PUBLIC;
CREATE TRIGGER f5_quality_owner BEFORE INSERT OR UPDATE ON public.school_fact_qualities FOR EACH ROW EXECUTE FUNCTION app_private.f5_quality_owner();
GRANT SELECT(teaching_role) ON public.school_fact_qualities TO campus_ai_context_definer;
ALTER TABLE public.school_education_facts DROP CONSTRAINT ck_education_fact_kind;
ALTER TABLE public.school_education_facts ADD CONSTRAINT ck_education_fact_kind CHECK (kind IN ('class_lineage','class_location','class_placement','class_position_definition','class_teacher_assignment','course_subject','education_calendar_rule','education_class','education_cohort','education_grade','education_grade_group','education_offering','education_stage','education_term','offering_member_source','student_class_membership','student_class_position','student_cohort_membership','student_grade_placement','teaching_assignment','teaching_course','teaching_group','teaching_group_member','teaching_offering','teaching_organization_scope','teaching_subject'));
ALTER TABLE public.school_education_facts ADD CONSTRAINT uq_education_fact_school_id_kind UNIQUE(school_id,id,kind);
ALTER TABLE public.school_education_versions ADD COLUMN fact_kind varchar(80);
UPDATE public.school_education_versions v SET fact_kind=f.kind FROM public.school_education_facts f WHERE f.school_id=v.school_id AND f.id=v.fact_id;
ALTER TABLE public.school_education_versions ALTER COLUMN fact_kind SET NOT NULL;
ALTER TABLE public.school_education_versions ADD CONSTRAINT fk_education_version_kind FOREIGN KEY(school_id,fact_id,fact_kind) REFERENCES public.school_education_facts(school_id,id,kind) ON DELETE RESTRICT;
ALTER TABLE public.school_education_references ADD COLUMN education_kind varchar(80);
UPDATE public.school_education_references r SET education_kind=f.kind FROM public.school_education_facts f WHERE f.school_id=r.school_id AND f.id=r.education_id;
ALTER TABLE public.school_education_references ADD CONSTRAINT fk_education_reference_kind FOREIGN KEY(school_id,education_id,education_kind) REFERENCES public.school_education_facts(school_id,id,kind) ON DELETE RESTRICT;
ALTER TABLE public.school_education_references ADD CONSTRAINT ck_education_reference_kind_pair CHECK ((education_id IS NULL)=(education_kind IS NULL));
CREATE FUNCTION app_private.f5_derive_version_kind() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog AS $f$
DECLARE actual text;
BEGIN
 SELECT kind INTO actual FROM public.school_education_facts WHERE school_id=NEW.school_id AND id=NEW.fact_id;
 IF actual IS NULL OR (NEW.fact_kind IS NOT NULL AND NEW.fact_kind<>actual) THEN RAISE EXCEPTION 'education kind mismatch' USING ERRCODE='23514'; END IF;
 NEW.fact_kind:=actual;
 RETURN NEW;
END; $f$;
REVOKE ALL ON FUNCTION app_private.f5_derive_version_kind() FROM PUBLIC;
CREATE TRIGGER f5_derive_version_kind BEFORE INSERT OR UPDATE ON public.school_education_versions FOR EACH ROW EXECUTE FUNCTION app_private.f5_derive_version_kind();
CREATE FUNCTION app_private.f5_derive_reference_kind() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog AS $f$
DECLARE actual text; expected text; parent_kind text; body jsonb; expected_key text; actual_key text; target_column text;
BEGIN
 SELECT fact_kind,payload INTO parent_kind,body FROM public.school_education_versions WHERE school_id=NEW.school_id AND id=NEW.version_id;
 IF parent_kind IN ('teaching_subject','teaching_course','course_subject','teaching_offering','offering_member_source','teaching_group','teaching_group_member','teaching_assignment','teaching_organization_scope') THEN
  IF NEW.ordinal < 0 OR NEW.ordinal > 2147483647 THEN RAISE EXCEPTION 'reference ordinal out of bounds' USING ERRCODE='23514'; END IF;
  expected_key:=CASE WHEN NEW.field_name IN ('stage_source_keys','subject_source_keys','grade_source_keys') THEN body->NEW.field_name->>(NEW.ordinal::integer) ELSE body->>NEW.field_name END;
  IF expected_key IS NULL OR (NEW.field_name NOT IN ('stage_source_keys','subject_source_keys','grade_source_keys') AND NEW.ordinal<>0) THEN RAISE EXCEPTION 'reference absent from payload' USING ERRCODE='23514'; END IF;
  IF NEW.education_id IS NOT NULL THEN
   SELECT source_key INTO actual_key FROM public.school_education_facts WHERE school_id=NEW.school_id AND id=NEW.education_id;
  ELSIF NEW.student_id IS NOT NULL AND NEW.field_name='student_source_key' THEN
   SELECT source_key INTO actual_key FROM public.students WHERE school_id=NEW.school_id AND id=NEW.student_id;
  ELSIF NEW.assignment_id IS NOT NULL AND NEW.field_name='assignment_source_key' THEN
   SELECT source_key INTO actual_key FROM public.staff_assignments WHERE school_id=NEW.school_id AND id=NEW.assignment_id;
  ELSIF NEW.organization_id IS NOT NULL AND NEW.field_name='organization_source_key' THEN
   SELECT source_key INTO actual_key FROM public.organization_units WHERE school_id=NEW.school_id AND id=NEW.organization_id;
  ELSIF NEW.location_id IS NOT NULL AND NEW.field_name='campus_source_key' THEN
   SELECT source_key INTO actual_key FROM public.campus_locations WHERE school_id=NEW.school_id AND id=NEW.location_id;
  ELSIF NEW.relationship_id IS NOT NULL AND NEW.field_name='school_relationship_source_key' THEN
   SELECT COALESCE(r.source_key,l.fact_source_key) INTO actual_key FROM public.student_school_relationships r LEFT JOIN public.school_fact_change_lineage l ON l.school_id=r.school_id AND l.target_id=r.id AND l.fact_kind='student_school_relationship' WHERE r.school_id=NEW.school_id AND r.id=NEW.relationship_id ORDER BY l.target_version DESC LIMIT 1;
  END IF;
  IF actual_key IS DISTINCT FROM expected_key THEN RAISE EXCEPTION 'reference does not match payload' USING ERRCODE='23514'; END IF;
 END IF;
 IF NEW.education_id IS NOT NULL THEN
  SELECT kind INTO actual FROM public.school_education_facts WHERE school_id=NEW.school_id AND id=NEW.education_id;
  expected:=CASE NEW.field_name WHEN 'subject_source_key' THEN 'teaching_subject' WHEN 'course_source_key' THEN 'teaching_course' WHEN 'offering_source_key' THEN 'teaching_offering' WHEN 'group_source_key' THEN 'teaching_group' WHEN 'term_source_key' THEN 'education_term' WHEN 'grade_group_source_key' THEN 'education_grade_group' WHEN 'stage_source_key' THEN 'education_stage' WHEN 'grade_source_key' THEN 'education_grade' WHEN 'calendar_source_key' THEN 'education_calendar_rule' WHEN 'cohort_source_key' THEN 'education_cohort' WHEN 'class_source_key' THEN 'education_class' WHEN 'predecessor_class_source_key' THEN 'education_class' WHEN 'position_source_key' THEN 'class_position_definition' WHEN 'stage_source_keys' THEN 'education_stage' WHEN 'subject_source_keys' THEN 'teaching_subject' WHEN 'grade_source_keys' THEN 'education_grade' ELSE NULL END;
  IF actual IS NULL OR (NEW.education_kind IS NOT NULL AND NEW.education_kind<>actual) OR (expected IS NOT NULL AND actual<>expected) OR (parent_kind IN ('teaching_subject','teaching_course','course_subject','teaching_offering','offering_member_source','teaching_group','teaching_group_member','teaching_assignment','teaching_organization_scope') AND expected IS NULL) THEN RAISE EXCEPTION 'education reference type mismatch' USING ERRCODE='23514'; END IF;
  NEW.education_kind:=actual;
 END IF;
 RETURN NEW;
END; $f$;
REVOKE ALL ON FUNCTION app_private.f5_derive_reference_kind() FROM PUBLIC;
CREATE TRIGGER f5_derive_reference_kind BEFORE INSERT OR UPDATE ON public.school_education_references FOR EACH ROW EXECUTE FUNCTION app_private.f5_derive_reference_kind();
ALTER TABLE public.school_education_versions ADD CONSTRAINT ck_f5_teaching_subject_required CHECK (fact_kind<>'teaching_subject' OR ((jsonb_typeof(payload->'display_name')='string' AND btrim(payload->>'display_name')<>'') AND (jsonb_typeof(payload->'stage_source_keys')='array' AND jsonb_array_length(payload->'stage_source_keys')>0)) IS TRUE);
ALTER TABLE public.school_education_versions ADD CONSTRAINT ck_f5_teaching_course_required CHECK (fact_kind<>'teaching_course' OR ((jsonb_typeof(payload->'display_name')='string' AND btrim(payload->>'display_name')<>'')) IS TRUE);
ALTER TABLE public.school_education_versions ADD CONSTRAINT ck_f5_course_subject_required CHECK (fact_kind<>'course_subject' OR ((jsonb_typeof(payload->'course_source_key')='string' AND btrim(payload->>'course_source_key')<>'') AND (jsonb_typeof(payload->'subject_source_key')='string' AND btrim(payload->>'subject_source_key')<>'')) IS TRUE);
ALTER TABLE public.school_education_versions ADD CONSTRAINT ck_f5_teaching_offering_required CHECK (fact_kind<>'teaching_offering' OR ((jsonb_typeof(payload->'course_source_key')='string' AND btrim(payload->>'course_source_key')<>'') AND (jsonb_typeof(payload->'offering_status')='string' AND btrim(payload->>'offering_status')<>'')) IS TRUE);
ALTER TABLE public.school_education_versions ADD CONSTRAINT ck_f5_offering_member_source_required CHECK (fact_kind<>'offering_member_source' OR ((jsonb_typeof(payload->'offering_source_key')='string' AND btrim(payload->>'offering_source_key')<>'') AND (jsonb_typeof(payload->'source_kind')='string' AND btrim(payload->>'source_kind')<>'')) IS TRUE);
ALTER TABLE public.school_education_versions ADD CONSTRAINT ck_f5_teaching_group_required CHECK (fact_kind<>'teaching_group' OR ((jsonb_typeof(payload->'group_mode')='string' AND btrim(payload->>'group_mode')<>'') AND (jsonb_typeof(payload->'organization_source_key')='string' AND btrim(payload->>'organization_source_key')<>'')) IS TRUE);
ALTER TABLE public.school_education_versions ADD CONSTRAINT ck_f5_teaching_group_member_required CHECK (fact_kind<>'teaching_group_member' OR ((jsonb_typeof(payload->'group_source_key')='string' AND btrim(payload->>'group_source_key')<>'') AND (jsonb_typeof(payload->'school_relationship_source_key')='string' AND btrim(payload->>'school_relationship_source_key')<>'') AND (jsonb_typeof(payload->'student_source_key')='string' AND btrim(payload->>'student_source_key')<>'')) IS TRUE);
ALTER TABLE public.school_education_versions ADD CONSTRAINT ck_f5_teaching_assignment_required CHECK (fact_kind<>'teaching_assignment' OR ((jsonb_typeof(payload->'assignment_source_key')='string' AND btrim(payload->>'assignment_source_key')<>'') AND (jsonb_typeof(payload->'offering_source_key')='string' AND btrim(payload->>'offering_source_key')<>'') AND (jsonb_typeof(payload->'teaching_role')='string' AND btrim(payload->>'teaching_role')<>'')) IS TRUE);
ALTER TABLE public.school_education_versions ADD CONSTRAINT ck_f5_teaching_organization_scope_required CHECK (fact_kind<>'teaching_organization_scope' OR ((jsonb_typeof(payload->'organization_source_key')='string' AND btrim(payload->>'organization_source_key')<>'') AND (jsonb_typeof(payload->'scope_basis')='string' AND btrim(payload->>'scope_basis')<>'') AND (jsonb_typeof(payload->'subject_source_key')='string' AND btrim(payload->>'subject_source_key')<>'')) IS TRUE);
ALTER TABLE public.school_education_versions ADD CONSTRAINT ck_f5_member_source CHECK (fact_kind<>'offering_member_source' OR ((payload->>'source_kind'='administrative_class' AND NULLIF(payload->>'class_source_key','') IS NOT NULL AND payload->>'group_source_key' IS NULL) OR (payload->>'source_kind'='independent_group' AND NULLIF(payload->>'group_source_key','') IS NOT NULL AND payload->>'class_source_key' IS NULL)) IS TRUE);
ALTER TABLE public.school_education_versions ADD CONSTRAINT ck_f5_offering_status CHECK (fact_kind<>'teaching_offering' OR (payload->>'offering_status' IN ('active','suspended')) IS TRUE);
ALTER TABLE public.school_education_versions ADD CONSTRAINT ck_f5_group_mode CHECK (fact_kind<>'teaching_group' OR (payload->>'group_mode' IN ('exclusive','shared')) IS TRUE);
ALTER TABLE public.school_education_versions ADD CONSTRAINT ck_f5_teaching_role CHECK (fact_kind<>'teaching_assignment' OR (payload->>'teaching_role' IN ('lead','co_teacher')) IS TRUE);
ALTER TABLE public.school_education_versions ADD CONSTRAINT ck_f5_scope_basis CHECK (fact_kind<>'teaching_organization_scope' OR (CASE payload->>'scope_basis' WHEN 'stage' THEN NULLIF(payload->>'stage_source_key','') IS NOT NULL AND payload->>'grade_source_key' IS NULL AND payload->>'grade_group_source_key' IS NULL WHEN 'fixed_grade' THEN NULLIF(payload->>'grade_source_key','') IS NOT NULL AND payload->>'stage_source_key' IS NULL AND payload->>'grade_group_source_key' IS NULL WHEN 'academic_grade' THEN NULLIF(payload->>'grade_group_source_key','') IS NOT NULL AND payload->>'stage_source_key' IS NULL AND payload->>'grade_source_key' IS NULL ELSE false END) IS TRUE);
ALTER TABLE public.school_education_versions ADD CONSTRAINT excl_f5_course_subject EXCLUDE USING gist (school_id WITH =, (payload->>'course_source_key') WITH =, (payload->>'subject_source_key') WITH =, tstzrange(valid_from,valid_to,'[)') WITH &&) WHERE (superseded_at IS NULL AND fact_kind='course_subject');
ALTER TABLE public.school_education_versions ADD CONSTRAINT excl_f5_offering_member_source EXCLUDE USING gist (school_id WITH =, (payload->>'offering_source_key') WITH =, tstzrange(valid_from,valid_to,'[)') WITH &&) WHERE (superseded_at IS NULL AND fact_kind='offering_member_source');
ALTER TABLE public.school_education_versions ADD CONSTRAINT excl_f5_group EXCLUDE USING gist (school_id WITH =, (payload->>'organization_source_key') WITH =, tstzrange(valid_from,valid_to,'[)') WITH &&) WHERE (superseded_at IS NULL AND fact_kind='teaching_group');
ALTER TABLE public.school_education_versions ADD CONSTRAINT excl_f5_group_member EXCLUDE USING gist (school_id WITH =, (payload->>'group_source_key') WITH =, (payload->>'student_source_key') WITH =, tstzrange(valid_from,valid_to,'[)') WITH &&) WHERE (superseded_at IS NULL AND fact_kind='teaching_group_member');
ALTER TABLE public.school_education_versions ADD CONSTRAINT excl_f5_assignment EXCLUDE USING gist (school_id WITH =, (payload->>'offering_source_key') WITH =, (payload->>'assignment_source_key') WITH =, (payload->>'teaching_role') WITH =, tstzrange(valid_from,valid_to,'[)') WITH &&) WHERE (superseded_at IS NULL AND fact_kind='teaching_assignment');
ALTER TABLE public.school_education_versions ADD CONSTRAINT excl_f5_scope_stage EXCLUDE USING gist (school_id WITH =, (payload->>'organization_source_key') WITH =, (payload->>'subject_source_key') WITH =, (payload->>'stage_source_key') WITH =, (COALESCE(payload->>'term_source_key','')) WITH =, tstzrange(valid_from,valid_to,'[)') WITH &&) WHERE (superseded_at IS NULL AND fact_kind='teaching_organization_scope' AND payload->>'scope_basis'='stage');
ALTER TABLE public.school_education_versions ADD CONSTRAINT excl_f5_scope_fixed_grade EXCLUDE USING gist (school_id WITH =, (payload->>'organization_source_key') WITH =, (payload->>'subject_source_key') WITH =, (payload->>'grade_source_key') WITH =, (COALESCE(payload->>'term_source_key','')) WITH =, tstzrange(valid_from,valid_to,'[)') WITH &&) WHERE (superseded_at IS NULL AND fact_kind='teaching_organization_scope' AND payload->>'scope_basis'='fixed_grade');
ALTER TABLE public.school_education_versions ADD CONSTRAINT excl_f5_scope_academic_grade EXCLUDE USING gist (school_id WITH =, (payload->>'organization_source_key') WITH =, (payload->>'subject_source_key') WITH =, (payload->>'grade_group_source_key') WITH =, (COALESCE(payload->>'term_source_key','')) WITH =, tstzrange(valid_from,valid_to,'[)') WITH &&) WHERE (superseded_at IS NULL AND fact_kind='teaching_organization_scope' AND payload->>'scope_basis'='academic_grade');
"""

STAFF_SQL = r"""

CREATE FUNCTION app_private.f5_allowed_fields(p_kind text) RETURNS text[]
LANGUAGE sql IMMUTABLE SET search_path=pg_catalog AS $f$ SELECT CASE p_kind
WHEN 'teaching_subject' THEN ARRAY['basis_reference','business_code','display_name','stage_source_keys','valid_to']::text[]
WHEN 'teaching_course' THEN ARRAY['basis_reference','business_code','display_name','grade_source_keys','stage_source_keys','valid_to']::text[]
WHEN 'course_subject' THEN ARRAY['basis_reference','course_source_key','subject_source_key','valid_to']::text[]
WHEN 'teaching_offering' THEN ARRAY['basis_reference','campus_source_key','course_source_key','display_name','offering_status','teaching_role_limits','term_source_key','valid_to']::text[]
WHEN 'offering_member_source' THEN ARRAY['basis_reference','class_source_key','group_source_key','offering_source_key','source_kind','valid_to']::text[]
WHEN 'teaching_group' THEN ARRAY['basis_reference','group_mode','organization_source_key','valid_to']::text[]
WHEN 'teaching_group_member' THEN ARRAY['basis_reference','group_source_key','school_relationship_source_key','student_source_key','valid_to']::text[]
WHEN 'teaching_assignment' THEN ARRAY['assignment_source_key','basis_reference','duty_scope','offering_source_key','subject_source_keys','teaching_role','valid_to']::text[]
WHEN 'teaching_organization_scope' THEN ARRAY['basis_reference','grade_group_source_key','grade_source_key','organization_source_key','scope_basis','stage_source_key','subject_source_key','term_source_key','valid_to']::text[]
ELSE NULL::text[] END; $f$;
REVOKE ALL ON FUNCTION app_private.f5_allowed_fields(text) FROM PUBLIC,
 campus_ai_runtime,campus_ai_school_fact_reader,campus_ai_school_fact_writer,campus_ai_migration,campus_ai_db_owner;

CREATE FUNCTION app.school_fact_f5_scope_is_current(
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
    OR app_private.f5_allowed_fields(p_kind) IS NULL
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
REVOKE ALL ON FUNCTION app.school_fact_f5_scope_is_current(uuid,text,text[],timestamptz) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app.school_fact_f5_scope_is_current(uuid,text,text[],timestamptz)
 TO campus_ai_school_fact_reader;

CREATE FUNCTION app_private.f5_target_records(
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
REVOKE ALL ON FUNCTION app_private.f5_target_records(uuid,text,text[],text[],timestamptz,timestamptz,jsonb)
 FROM PUBLIC,campus_ai_runtime,campus_ai_school_fact_reader,campus_ai_school_fact_writer,campus_ai_migration,campus_ai_db_owner;

CREATE FUNCTION app.school_fact_f5_targets(
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
    OR app_private.f5_allowed_fields(p_kind) IS NULL
    OR NOT (p_fields <@ app_private.f5_allowed_fields(p_kind))
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
 'location_source_key','grade_source_key','subject_source_key','course_source_key','offering_source_key','group_source_key','term_source_key','grade_group_source_key','source_kind','offering_status','teaching_role','group_mode','scope_basis'])
 OR NOT fl.key=ANY(p_fields) OR jsonb_typeof(fl.value)<>'string') THEN
  RAISE EXCEPTION 'invalid education filter' USING ERRCODE='22023';
 END IF;
 IF EXISTS(SELECT 1 FROM jsonb_each(p_filter) fl WHERE
  (fl.key='source_kind' AND fl.value#>>'{}' NOT IN ('administrative_class','independent_group')) OR
  (fl.key='offering_status' AND fl.value#>>'{}' NOT IN ('active','suspended')) OR
  (fl.key='teaching_role' AND fl.value#>>'{}' NOT IN ('lead','co_teacher')) OR
  (fl.key='group_mode' AND fl.value#>>'{}' NOT IN ('exclusive','shared')) OR
  (fl.key='scope_basis' AND fl.value#>>'{}' NOT IN ('stage','fixed_grade','academic_grade'))) THEN
  RAISE EXCEPTION 'unregistered teaching filter value' USING ERRCODE='22023';
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
 IF NOT app.school_fact_f5_scope_is_current(p_school,p_kind,
  ARRAY(SELECT f.source_key FROM public.school_education_facts f WHERE f.school_id=p_school
        AND f.kind=p_kind AND f.source_key=ANY(p_objects)),statement_timestamp()) THEN
  RAISE EXCEPTION 'education publication evidence unavailable' USING ERRCODE='55000';
 END IF;
 records := app_private.f5_target_records(p_school,p_kind,p_objects,p_fields,p_as_of,p_known_at,p_filter);
 RETURN jsonb_build_object('status',CASE WHEN jsonb_array_length(records)=0 THEN 'no_current_records' ELSE 'available' END,'school_fact_version',
 app.school_fact_current_version(p_school),'records',records,'as_of',p_as_of,'known_at',p_known_at);
END;
$f$;
REVOKE ALL ON FUNCTION app.school_fact_f5_targets(uuid,text,text[],text[],timestamptz,timestamptz,jsonb)
 FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app.school_fact_f5_targets(uuid,text,text[],text[],timestamptz,timestamptz,jsonb)
 TO campus_ai_school_fact_reader;

CREATE FUNCTION app.school_fact_f5_coordinates(p_school uuid,p_kind text,p_objects text[])
RETURNS jsonb LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog AS $f$
BEGIN
 IF SESSION_USER<>'campus_ai_school_fact_reader'
 OR p_school IS DISTINCT FROM app.require_school_database_anchor() THEN
  RAISE EXCEPTION 'education coordinate Reader denied' USING ERRCODE='28000';
 END IF;
 IF app_private.f5_allowed_fields(p_kind) IS NULL OR p_objects IS NULL
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
REVOKE ALL ON FUNCTION app.school_fact_f5_coordinates(uuid,text,text[]) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app.school_fact_f5_coordinates(uuid,text,text[])
 TO campus_ai_school_fact_reader;
"""


def upgrade():
    op.execute("SET LOCAL ROLE campus_ai_db_owner")
    op.execute(STORAGE_SQL)
    op.execute("GRANT CREATE ON SCHEMA app,app_private TO campus_ai_context_definer")
    op.execute("SET LOCAL ROLE campus_ai_context_definer")
    op.execute(STAFF_SQL)
    op.execute("SET LOCAL ROLE campus_ai_db_owner")
    op.execute("REVOKE CREATE ON SCHEMA app,app_private FROM campus_ai_context_definer")


def downgrade():
    raise RuntimeError("F5 teaching facts require a data-preserving forward repair")
