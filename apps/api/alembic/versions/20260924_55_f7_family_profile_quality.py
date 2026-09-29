"""Add the narrow family-profile quality risk projection.

Only the existing verified family Reader can call this function. It derives
the child, field and formal target from the current family profile; callers
cannot supply a quality key, target kind, target key, or another child.
"""

from alembic import op

revision = "20260924_55"
down_revision = "20260924_54"
branch_labels = None
depends_on = None


FAMILY_PROFILE_QUALITY_SQL = r"""
CREATE FUNCTION app.school_fact_f7_family_profile_quality(
 p_school uuid,p_adult uuid,p_relation uuid,p_version bigint,
 p_factual_at timestamptz,p_fields text[]
) RETURNS jsonb LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path=pg_catalog AS $f$
DECLARE
 checked_at timestamptz := statement_timestamp();
 at_time timestamptz := COALESCE(p_factual_at,statement_timestamp());
 profile_doc jsonb;
 child_person uuid;
 claims jsonb;
BEGIN
 IF SESSION_USER<>'campus_ai_school_fact_reader'
    OR p_school IS DISTINCT FROM app.require_school_database_anchor() THEN
   RAISE EXCEPTION 'Family quality Reader denied' USING ERRCODE='28000';
 END IF;
 IF p_fields IS NULL OR cardinality(p_fields)=0 OR cardinality(p_fields)>6
    OR array_position(p_fields,NULL) IS NOT NULL
    OR EXISTS (SELECT 1 FROM unnest(p_fields) AS f(name)
      WHERE f.name NOT IN ('name','enrollment_status','campus',
        'student_number','residence_mode','region')) THEN
   RAISE EXCEPTION 'Invalid family quality fields' USING ERRCODE='22023';
 END IF;
 -- Existing F3 Reader owns relationship, factual-time and field projection.
 -- It checks the current family authority even for a historical fact time.
 profile_doc := app.school_fact_family_profile(
   p_school,p_adult,p_relation,p_version,p_factual_at);
 IF EXISTS (SELECT 1 FROM unnest(p_fields) AS f(name)
   WHERE NOT ((profile_doc->'profile') ? f.name)) THEN
   RAISE EXCEPTION 'Family quality field denied' USING ERRCODE='28000';
 END IF;
 SELECT s.person_id INTO child_person
 FROM app.school_fact_family_authority(p_school,p_adult,p_relation,p_version) a
 JOIN public.students s ON s.school_id=a.school_id AND s.id=a.student_id;
 IF child_person IS NULL THEN
   RAISE EXCEPTION 'Family quality Reader denied' USING ERRCODE='28000';
 END IF;

 WITH requested AS (
   SELECT DISTINCT f.name AS field_name FROM unnest(p_fields) AS f(name)
   WHERE (profile_doc->'profile') ? f.name
 ), current_targets AS (
   SELECT 'name'::text AS field_name,'person'::text AS target_kind,
     l.fact_source_key AS target_key,'display_name'::text AS target_field,
     'person'::text AS owner_kind,child_person AS owner_id,
     b.target_fact_version AS target_fact_version
   FROM requested r
   JOIN public.school_person_names n ON r.field_name='name'
     AND n.school_id=p_school AND n.person_id=child_person
     AND n.status='confirmed' AND n.valid_from<=at_time
     AND (n.valid_to IS NULL OR at_time<n.valid_to)
   JOIN public.school_fact_change_lineage l ON l.school_id=n.school_id
     AND l.fact_kind='person' AND l.target_id=n.person_id
     AND l.target_version=n.version AND l.operation IN ('upsert','reactivate','correct')
   JOIN public.master_data_import_batches b ON b.school_id=l.school_id
     AND b.id=l.batch_id AND b.mode='canonical_atomic_changes'
     AND b.status='applied'
   UNION ALL
   SELECT r.field_name,'student_school_relationship',l.fact_source_key,
     CASE r.field_name WHEN 'campus' THEN 'primary_campus_source_key'
       ELSE r.field_name END,
     'person',child_person,b.target_fact_version
   FROM requested r
   JOIN public.students s ON s.school_id=p_school AND s.person_id=child_person
   JOIN public.student_school_relationships e ON e.school_id=s.school_id
     AND e.student_id=s.id AND e.valid_from<=at_time
     AND (e.valid_to IS NULL OR at_time<e.valid_to)
     AND e.superseded_at IS NULL
   JOIN public.school_fact_change_lineage l ON l.school_id=e.school_id
     AND l.fact_kind='student_school_relationship' AND l.target_id=e.id
     AND l.target_version=e.version AND l.operation IN ('upsert','reactivate','correct')
   JOIN public.master_data_import_batches b ON b.school_id=l.school_id
     AND b.id=l.batch_id AND b.mode='canonical_atomic_changes'
     AND b.status='applied'
   WHERE r.field_name IN ('enrollment_status','campus','student_number','residence_mode')
   UNION ALL
   SELECT 'region','person_region',l.fact_source_key,f.target_field,
     'person',child_person,b.target_fact_version
   FROM requested r
   CROSS JOIN (VALUES ('district_code'),('district_name')) f(target_field)
   JOIN public.person_regions pr ON r.field_name='region'
     AND pr.school_id=p_school AND pr.person_id=child_person
     AND pr.region_purpose='student_profile'
     AND pr.valid_from<=at_time AND (pr.valid_to IS NULL OR at_time<pr.valid_to)
     AND (pr.revoked_at IS NULL OR at_time<pr.revoked_at)
   JOIN public.school_fact_change_lineage l ON l.school_id=pr.school_id
     AND l.fact_kind='person_region' AND l.target_id=pr.id
     AND l.target_version=pr.version AND l.operation IN ('upsert','reactivate','correct')
   JOIN public.master_data_import_batches b ON b.school_id=l.school_id
     AND b.id=l.batch_id AND b.mode='canonical_atomic_changes'
     AND b.status='applied'
   UNION ALL
   SELECT 'campus','location',l.fact_source_key,'display_name',
     'location',e.primary_campus_location_id,b.target_fact_version
   FROM requested r
   JOIN public.students s ON s.school_id=p_school AND s.person_id=child_person
   JOIN public.student_school_relationships e ON e.school_id=s.school_id
     AND e.student_id=s.id AND e.valid_from<=at_time
     AND (e.valid_to IS NULL OR at_time<e.valid_to)
     AND e.superseded_at IS NULL
   JOIN public.campus_location_versions c ON c.school_id=e.school_id
     AND c.location_id=e.primary_campus_location_id AND c.status='confirmed'
     AND c.valid_from<=at_time AND (c.valid_to IS NULL OR at_time<c.valid_to)
   JOIN public.school_fact_change_lineage l ON l.school_id=c.school_id
     AND l.fact_kind='location' AND l.target_id=c.location_id
     AND l.target_version=c.version AND l.operation IN ('upsert','reactivate','correct')
   JOIN public.master_data_import_batches b ON b.school_id=l.school_id
     AND b.id=l.batch_id AND b.mode='canonical_atomic_changes'
     AND b.status='applied'
   WHERE r.field_name='campus'
 ), permitted_risks AS (
   SELECT DISTINCT t.field_name,q.quality_kind,q.source_status,
     q.coverage_status,q.dispute_status,q.review_due_at
   FROM current_targets t
   JOIN public.school_fact_qualities q ON q.school_id=p_school
     AND q.target_fact_kind=t.target_kind AND q.target_source_key=t.target_key
     AND ((t.owner_kind='person' AND q.owner_kind='person'
       AND q.person_id=t.owner_id)
       OR (t.owner_kind='location' AND q.owner_kind='location'
       AND q.location_id=t.owner_id))
     AND (q.field_name=t.target_field OR q.field_name IS NULL)
     AND q.valid_from<=at_time AND (q.valid_to IS NULL OR at_time<q.valid_to)
     AND (q.revoked_at IS NULL OR at_time<q.revoked_at)
     AND q.verified_at<=checked_at
   JOIN public.master_data_import_batches qb ON qb.school_id=q.school_id
     AND qb.id=q.import_batch_id AND qb.mode='canonical_atomic_changes'
     AND qb.status='applied'
     -- Both values are school-wide publication versions, not the independent
     -- q.version and target row version. A target republished later invalidates
     -- an older risk; a newly published version-1 risk still applies to a
     -- target already at row version 3 or higher.
     AND qb.target_fact_version>=t.target_fact_version
   WHERE (q.quality_kind='source' AND q.source_status='unavailable')
      OR (q.quality_kind='coverage' AND q.coverage_status='partial')
      OR (q.quality_kind='dispute' AND q.dispute_status='open')
      OR (q.quality_kind='review' AND q.review_due_at<=at_time)
 )
 SELECT COALESCE(jsonb_agg(jsonb_strip_nulls(jsonb_build_object(
   'field_name',r.field_name,'quality_kind',r.quality_kind,
   'source_status',r.source_status,'coverage_status',r.coverage_status,
   'dispute_status',r.dispute_status,'review_due_at',r.review_due_at
 )) ORDER BY r.field_name,r.quality_kind),'[]'::jsonb) INTO claims
 FROM permitted_risks r;
 RETURN jsonb_build_object('school_fact_version',
   app.school_fact_current_version(p_school),'claims',claims);
END;
$f$;
"""


def upgrade() -> None:
    op.execute("SET LOCAL ROLE campus_ai_db_owner")
    # The definer already has anchored RLS SELECT on quality rows; add only
    # owner keys needed for the fixed family projection.
    op.execute(
        "GRANT SELECT(person_id,location_id) ON public.school_fact_qualities "
        "TO campus_ai_context_definer"
    )
    op.execute("GRANT CREATE ON SCHEMA app TO campus_ai_context_definer")
    op.execute("SET LOCAL ROLE campus_ai_context_definer")
    op.execute(FAMILY_PROFILE_QUALITY_SQL)
    op.execute(
        "REVOKE ALL ON FUNCTION app.school_fact_f7_family_profile_quality"
        "(uuid,uuid,uuid,bigint,timestamptz,text[]) "
        "FROM PUBLIC,campus_ai_runtime,campus_ai_school_fact_writer,"
        "campus_ai_migration,campus_ai_db_owner"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION app.school_fact_f7_family_profile_quality"
        "(uuid,uuid,uuid,bigint,timestamptz,text[]) "
        "TO campus_ai_school_fact_reader"
    )
    op.execute("SET LOCAL ROLE campus_ai_db_owner")
    op.execute("REVOKE CREATE ON SCHEMA app FROM campus_ai_context_definer")


def downgrade() -> None:
    raise RuntimeError("Published F7 family quality projection is append-only")
