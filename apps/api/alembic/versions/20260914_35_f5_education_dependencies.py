"""Finite F5 quality and policy dependency projections; original F4 contract is unchanged."""

# ruff: noqa: E501
from alembic import op

revision = "20260914_35"
down_revision = "20260914_34"
branch_labels = None
depends_on = None

HELPER_SQL = r"""
CREATE FUNCTION app_private.f5_dependency_allowed_fields(p_kind text) RETURNS text[]
LANGUAGE sql IMMUTABLE SET search_path=pg_catalog AS $f$
 SELECT COALESCE(app_private.f5_allowed_fields(p_kind),app_private.f4_allowed_fields(p_kind));
$f$;
REVOKE ALL ON FUNCTION app_private.f5_dependency_allowed_fields(text) FROM PUBLIC;
CREATE FUNCTION app_private.f5_dependency_scope_is_current(p_school uuid,p_kind text,p_keys text[],p_at timestamptz)
RETURNS boolean LANGUAGE sql STABLE SET search_path=pg_catalog AS $f$
 SELECT CASE WHEN app_private.f5_allowed_fields(p_kind) IS NOT NULL
 THEN app.school_fact_f5_scope_is_current(p_school,p_kind,p_keys,p_at)
 ELSE app.school_fact_f4_scope_is_current(p_school,p_kind,p_keys,p_at) END;
$f$;
REVOKE ALL ON FUNCTION app_private.f5_dependency_scope_is_current(uuid,text,text[],timestamptz) FROM PUBLIC;
"""

DEPENDENCY_SQL = r"""CREATE FUNCTION app.school_fact_f5_dependencies(
 p_school uuid,p_kind text,p_keys text[],p_owner_kind text,p_owner_keys text[],
 p_target_kind text,p_target_keys text[],p_fields text[],p_at timestamptz,p_academic_year bigint,p_policy_keys text[],p_campus_keys text[],p_position_keys text[],p_teaching_roles text[])
RETURNS jsonb LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog AS $f$
DECLARE result jsonb; boundary timestamptz;
BEGIN
 IF SESSION_USER<>'campus_ai_school_fact_reader' OR p_school IS DISTINCT FROM app.require_school_database_anchor() THEN
  RAISE EXCEPTION 'education dependency Reader denied' USING ERRCODE='28000';
 END IF;
 IF p_kind IS NULL OR p_kind NOT IN ('fact_quality','policy_scope') OR p_at IS NULL
  OR p_teaching_roles IS NULL OR NOT p_teaching_roles<@ARRAY['lead','co_teacher']::text[]
  OR array_position(p_teaching_roles,NULL) IS NOT NULL OR cardinality(p_teaching_roles)<>(SELECT count(DISTINCT r) FROM unnest(p_teaching_roles) r)
  OR (cardinality(p_teaching_roles)>0 AND p_target_kind<>'teaching_assignment')
  OR (app_private.f5_allowed_fields(p_owner_kind) IS NULL AND app_private.f5_allowed_fields(p_target_kind) IS NULL)
  OR p_keys IS NULL OR p_owner_keys IS NULL OR p_target_keys IS NULL OR p_fields IS NULL OR p_policy_keys IS NULL OR p_campus_keys IS NULL OR p_position_keys IS NULL
  OR cardinality(p_owner_keys) NOT BETWEEN 1 AND 100 OR cardinality(p_fields) NOT BETWEEN 1 AND 100
  OR cardinality(p_keys)>100 OR cardinality(p_target_keys)>100 OR cardinality(p_policy_keys)>100 OR cardinality(p_campus_keys)>100 OR cardinality(p_position_keys)>100
  OR app_private.f5_dependency_allowed_fields(p_owner_kind) IS NULL OR app_private.f5_dependency_allowed_fields(p_target_kind) IS NULL
  OR NOT p_fields<@app_private.f5_dependency_allowed_fields(p_target_kind)
  OR EXISTS(SELECT 1 FROM unnest(p_keys||p_owner_keys||p_target_keys||p_fields||p_policy_keys||p_campus_keys||p_position_keys) k WHERE k IS NULL OR btrim(k)='' OR length(k)>255)
  OR (p_kind='policy_scope' AND cardinality(p_policy_keys)=0)
 THEN RAISE EXCEPTION 'invalid finite dependency selection' USING ERRCODE='22023'; END IF;
 IF NOT app_private.f5_dependency_scope_is_current(p_school,p_owner_kind,p_owner_keys,statement_timestamp())
  OR NOT app_private.f5_dependency_scope_is_current(p_school,p_target_kind,p_target_keys,statement_timestamp()) THEN
  RAISE EXCEPTION 'education publication evidence unavailable' USING ERRCODE='55000'; END IF;
 IF p_kind='fact_quality' THEN
  WITH selected AS MATERIALIZED (
   SELECT q.id,q.source_key,q.version,q.valid_from,LEAST(q.valid_to,q.revoked_at) valid_to,q.source_id,
    q.basis_reference,q.quality_kind,q.target_fact_kind,q.owner_kind,q.target_source_key,q.replacement_source_key,
    q.responsibility,q.evidence_reference,q.purpose,q.contact_kind,q.coverage_status,q.negative_code,q.teaching_role,
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
    AND (q.teaching_role IS NULL OR q.teaching_role=ANY(p_teaching_roles))
    AND (q.negative_code IS DISTINCT FROM 'no_teaching_assignment' OR (e.kind='teaching_offering' AND q.target_fact_kind='teaching_assignment' AND q.teaching_role=ANY(p_teaching_roles)))
    AND (q.field_name IS NULL OR q.field_name=ANY(p_fields))
    AND (q.academic_year IS NULL OR q.academic_year=p_academic_year)
    AND (pos.id IS NULL OR (pv.n=1 AND pos.kind='class_position_definition' AND pos.source_key=ANY(p_position_keys)))
    AND EXISTS(SELECT 1 FROM public.school_education_versions v WHERE v.school_id=e.school_id AND v.fact_id=e.id
       AND v.superseded_at IS NULL AND v.valid_from<=p_at AND (v.valid_to IS NULL OR p_at<v.valid_to))
  ), current_rows AS (
   SELECT s.*,src.source_kind,src.source_ref FROM selected s JOIN public.school_fact_sources src ON src.school_id=p_school AND src.id=s.source_id
   WHERE s.valid_from<=p_at AND (s.valid_to IS NULL OR p_at<s.valid_to) AND s.verified_at<=p_at
  ) SELECT COALESCE(jsonb_agg(jsonb_build_object('source_key',source_key,'version',version,'effective_at',valid_from,'valid_to',valid_to,
    'content',to_jsonb(c)-ARRAY['id','source_id','source_key','version','valid_from','published_at','batch_id','source_kind','source_ref','owner_boundary','position_boundary'],
    'source',jsonb_build_object('label','学校明确登记的质量依据','source_kind',source_kind,'source_ref',source_ref,'published_at',published_at,'batch_id',batch_id,'publication_reference','publication:'||batch_id::text))) ,'[]'::jsonb),
    (SELECT min(t) FROM (SELECT valid_from t FROM selected UNION ALL SELECT valid_to FROM selected UNION ALL SELECT review_due_at FROM selected UNION ALL SELECT verified_at FROM selected UNION ALL SELECT owner_boundary FROM selected UNION ALL SELECT position_boundary FROM selected) times WHERE t>p_at)
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
 SELECT LEAST(boundary,min(t)) INTO boundary FROM (
  SELECT v.valid_from t FROM public.school_education_versions v JOIN public.school_education_facts e ON e.school_id=v.school_id AND e.id=v.fact_id
  WHERE e.school_id=p_school AND v.superseded_at IS NULL AND ((e.kind=p_owner_kind AND e.source_key=ANY(p_owner_keys)) OR (e.kind=p_target_kind AND e.source_key=ANY(p_target_keys)))
  UNION ALL SELECT v.valid_to FROM public.school_education_versions v JOIN public.school_education_facts e ON e.school_id=v.school_id AND e.id=v.fact_id
  WHERE e.school_id=p_school AND v.superseded_at IS NULL AND ((e.kind=p_owner_kind AND e.source_key=ANY(p_owner_keys)) OR (e.kind=p_target_kind AND e.source_key=ANY(p_target_keys)))
 ) bounds WHERE t>p_at;
 RETURN jsonb_build_object('school_fact_version',app.school_fact_current_version(p_school),'records',result,'next_valid_boundary',boundary);
END;
$f$;
REVOKE ALL ON FUNCTION app.school_fact_f5_dependencies(uuid,text,text[],text,text[],text,text[],text[],timestamptz,bigint,text[],text[],text[],text[]) FROM PUBLIC,campus_ai_runtime,campus_ai_school_fact_writer,campus_ai_migration,campus_ai_db_owner;
GRANT EXECUTE ON FUNCTION app.school_fact_f5_dependencies(uuid,text,text[],text,text[],text,text[],text[],timestamptz,bigint,text[],text[],text[],text[]) TO campus_ai_school_fact_reader;
"""

DEPENDENCY_COORDINATES_SQL = r"""
-- Exact existence coordinates only; F2 source authorization occurs before this call.
CREATE FUNCTION app.school_fact_f5_dependency_coordinates(p_school uuid,p_kind text,p_keys text[])
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
REVOKE ALL ON FUNCTION app.school_fact_f5_dependency_coordinates(uuid,text,text[]) FROM PUBLIC,campus_ai_runtime,campus_ai_school_fact_writer,campus_ai_migration,campus_ai_db_owner;
GRANT EXECUTE ON FUNCTION app.school_fact_f5_dependency_coordinates(uuid,text,text[]) TO campus_ai_school_fact_reader;

"""

DEPENDENCY_SELECTION_SQL = r"""
-- Frozen migration input. Exact source lookup returns reference coordinates only, never content.
CREATE FUNCTION app.school_fact_f5_dependency_selection(p_school uuid,p_kind text,p_key text,p_at timestamptz)
RETURNS jsonb LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog AS $f$
DECLARE rows jsonb;
BEGIN
 IF SESSION_USER<>'campus_ai_school_fact_reader' OR p_school IS DISTINCT FROM app.require_school_database_anchor() THEN
  RAISE EXCEPTION 'dependency selection denied' USING ERRCODE='28000'; END IF;
 IF p_kind IS NULL OR p_kind NOT IN ('fact_quality','policy_scope') OR p_key IS NULL OR btrim(p_key)='' OR length(p_key)>255 OR p_at IS NULL THEN
  RAISE EXCEPTION 'invalid exact dependency selection' USING ERRCODE='22023'; END IF;
 WITH selected AS (
  SELECT q.id,q.source_key,q.version,q.education_target_id,q.target_fact_kind,q.target_source_key,q.replacement_source_key,
   q.field_name,q.academic_year,q.teaching_role,q.education_position_id,NULL::uuid campus_location_id,NULL::uuid policy_id
  FROM public.school_fact_qualities q WHERE p_kind='fact_quality' AND q.school_id=p_school AND q.source_key=p_key
   AND q.owner_kind='education' AND q.valid_from<=p_at AND (LEAST(q.valid_to,q.revoked_at) IS NULL OR p_at<LEAST(q.valid_to,q.revoked_at))
  UNION ALL
  SELECT s.id,s.source_key,s.version,s.education_target_id,NULL::text,NULL::text,NULL::text,NULL::text,s.academic_year,NULL::text,NULL::uuid,s.campus_location_id,s.policy_id
  FROM public.school_policy_scopes s WHERE p_kind='policy_scope' AND s.school_id=p_school AND s.source_key=p_key
   AND s.valid_from<=p_at AND (s.valid_to IS NULL OR p_at<s.valid_to)
 ) SELECT COALESCE(jsonb_agg(jsonb_build_object('source_key',s.source_key,'version',s.version,
   'owner_kind',e.kind,'owner_key',e.source_key,'target_kind',COALESCE(s.target_fact_kind,e.kind),
   'target_key',s.target_source_key,'replacement_key',s.replacement_source_key,'field_name',s.field_name,
   'academic_year',s.academic_year,'teaching_role',s.teaching_role,'position_key',pos.source_key,'campus_key',loc.source_key,'policy_key',pol.source_key)),'[]'::jsonb)
 INTO rows FROM selected s
 JOIN public.school_education_facts e ON e.school_id=p_school AND e.id=s.education_target_id
 LEFT JOIN public.school_education_facts pos ON pos.school_id=p_school AND pos.id=s.education_position_id AND pos.kind='class_position_definition'
 LEFT JOIN public.campus_locations loc ON loc.school_id=p_school AND loc.id=s.campus_location_id
 LEFT JOIN public.school_policies pol ON pol.school_id=p_school AND pol.id=s.policy_id
 WHERE app_private.f5_dependency_allowed_fields(e.kind) IS NOT NULL
 AND app_private.f5_dependency_allowed_fields(COALESCE(s.target_fact_kind,e.kind)) IS NOT NULL
 AND EXISTS(SELECT 1 FROM public.school_fact_change_lineage l JOIN public.master_data_import_batches b ON b.school_id=l.school_id AND b.id=l.batch_id AND b.status='applied'
  WHERE l.school_id=p_school AND l.fact_kind=p_kind AND l.fact_source_key=p_key AND l.target_id=s.id AND l.target_version=s.version);
 RETURN jsonb_build_object('school_fact_version',app.school_fact_current_version(p_school),'records',rows);
END;
$f$;
REVOKE ALL ON FUNCTION app.school_fact_f5_dependency_selection(uuid,text,text,timestamptz) FROM PUBLIC,campus_ai_runtime,campus_ai_school_fact_writer,campus_ai_migration,campus_ai_db_owner;
GRANT EXECUTE ON FUNCTION app.school_fact_f5_dependency_selection(uuid,text,text,timestamptz) TO campus_ai_school_fact_reader;

"""


EXTERNAL_BOUNDARY_SQL = r"""
CREATE FUNCTION app.school_fact_f5_external_boundary(p_school uuid,p_kind text,p_keys text[],p_organizations text[],p_at timestamptz)
RETURNS jsonb LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog AS $f$
DECLARE boundary timestamptz;
BEGIN
 IF SESSION_USER<>'campus_ai_school_fact_reader' OR p_school IS DISTINCT FROM app.require_school_database_anchor() THEN
  RAISE EXCEPTION 'external boundary Reader denied' USING ERRCODE='28000'; END IF;
 IF p_kind IS NULL OR p_kind NOT IN ('person','assignment','organization','official_contact') OR p_at IS NULL
  OR p_keys IS NULL OR p_organizations IS NULL OR cardinality(p_keys)>100 OR cardinality(p_organizations)>100
  OR array_position(p_keys||p_organizations,NULL) IS NOT NULL
  OR EXISTS(SELECT 1 FROM unnest(p_keys||p_organizations) k WHERE btrim(k)='' OR length(k)>255)
  OR (p_kind='official_contact' AND cardinality(p_organizations)=0)
  OR (p_kind<>'official_contact' AND cardinality(p_organizations)>0)
 THEN RAISE EXCEPTION 'invalid finite external metadata selection' USING ERRCODE='22023'; END IF;
 WITH published AS MATERIALIZED (
  SELECT l.target_id,l.target_version FROM public.school_fact_change_lineage l
  JOIN public.master_data_import_batches b ON b.school_id=l.school_id AND b.id=l.batch_id AND b.status='applied'
  WHERE l.school_id=p_school AND l.fact_kind=p_kind AND l.fact_source_key=ANY(p_keys)
 ), periods AS (
  SELECT n.valid_from start_at,n.valid_to end_at FROM public.school_person_names n
   JOIN published l ON l.target_id=n.person_id AND l.target_version=n.version
   WHERE p_kind='person' AND n.school_id=p_school
  UNION ALL SELECT p.valid_from,p.valid_to FROM public.school_person_profile_versions p
   JOIN published l ON l.target_id=p.person_id AND l.target_version=p.version
   WHERE p_kind='person' AND p.school_id=p_school
  UNION ALL SELECT a.effective_from,LEAST(a.effective_to,a.planned_valid_to,a.revoked_at)
   FROM public.staff_assignment_versions a JOIN published l ON l.target_id=a.assignment_id AND l.target_version=a.target_version
   WHERE p_kind='assignment' AND a.school_id=p_school
  UNION ALL SELECT o.valid_from,o.valid_to FROM public.organization_unit_versions o
   JOIN published l ON l.target_id=o.organization_unit_id AND l.target_version=o.version
   WHERE p_kind='organization' AND o.school_id=p_school
  UNION ALL SELECT c.valid_from,c.valid_to FROM public.official_contact_points c
   JOIN published l ON l.target_id=c.id AND l.target_version=c.version
   JOIN public.organization_units o ON o.school_id=c.school_id AND o.id=c.organization_unit_id
   WHERE p_kind='official_contact' AND c.school_id=p_school AND o.source_key=ANY(p_organizations)
   AND c.owner_kind='organization' AND c.visibility_scope='public' AND c.lifecycle_status='active'
 ) SELECT min(t) INTO boundary FROM (SELECT start_at t FROM periods UNION ALL SELECT end_at FROM periods) times WHERE t>p_at;
 RETURN jsonb_build_object('school_fact_version',app.school_fact_current_version(p_school),'next_valid_boundary',boundary);
END;
$f$;
REVOKE ALL ON FUNCTION app.school_fact_f5_external_boundary(uuid,text,text[],text[],timestamptz)
 FROM PUBLIC,campus_ai_runtime,campus_ai_school_fact_writer,campus_ai_migration,campus_ai_db_owner;
GRANT EXECUTE ON FUNCTION app.school_fact_f5_external_boundary(uuid,text,text[],text[],timestamptz)
 TO campus_ai_school_fact_reader;
"""


def upgrade():
    op.execute("SET LOCAL ROLE campus_ai_db_owner")
    # teaching_role is the sole additional payload column: exact role applicability.
    op.execute(
        "GRANT SELECT(teaching_role) ON public.school_fact_qualities TO campus_ai_context_definer"
    )
    op.execute("GRANT CREATE ON SCHEMA app,app_private TO campus_ai_context_definer")
    op.execute("SET LOCAL ROLE campus_ai_context_definer")
    op.execute(HELPER_SQL)
    op.execute(DEPENDENCY_SQL)
    op.execute(DEPENDENCY_COORDINATES_SQL)
    op.execute(DEPENDENCY_SELECTION_SQL)
    op.execute(EXTERNAL_BOUNDARY_SQL)
    op.execute("SET LOCAL ROLE campus_ai_db_owner")
    op.execute("REVOKE CREATE ON SCHEMA app,app_private FROM campus_ai_context_definer")


def downgrade():
    raise RuntimeError("F5 dependency contracts require a data-preserving forward repair")
