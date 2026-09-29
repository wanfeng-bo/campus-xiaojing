"""Allow the six F3 fact kinds as exact F7 quality targets.

The prior constraint is replaced in place; existing rows, history and all other
quality checks remain untouched. No family grant or Reader visibility follows
from adding a target kind to this storage constraint.
"""

from alembic import op

revision = "20260924_46"
down_revision = "20260922_45"
branch_labels = None
depends_on = None


# Frozen copy of revision 42's allowed targets, plus the six F3 kinds. Do not
# import mutable application enums into an append-only migration.
QUALITY_TARGETS = (
    "school", "person", "organization", "assignment",
    "organization_campus_relationship", "location", "location_alias",
    "person_contact", "official_contact", "policy", "policy_scope",
    "policy_version", "organization_profile", "organization_membership",
    "organization_location", "person_workplace", "school_service",
    "class_lineage", "class_location", "class_placement",
    "class_position_definition", "class_teacher_assignment", "course_subject",
    "dorm_bed", "dorm_room", "dormitory_building_profile",
    "education_calendar_rule", "education_class", "education_cohort",
    "education_grade", "education_grade_group", "education_offering",
    "education_stage", "education_term", "offering_member_source",
    "student_bed_residence", "student_class_membership",
    "student_class_position", "student_cohort_membership",
    "student_grade_placement", "teaching_assignment", "teaching_course",
    "teaching_group", "teaching_group_member", "teaching_offering",
    "teaching_organization_scope", "teaching_subject",
    "student", "student_school_relationship", "login_mapping", "student_contact",
    "guardian_relationship", "person_region",
)

FAMILY_READ_SQL = r"""
CREATE FUNCTION app.school_fact_f7_family_quality(
 p_school uuid,p_quality_sources text[],p_target_kind text,p_target_source text,
 p_fields text[],p_at timestamptz
) RETURNS jsonb LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path=pg_catalog AS $f$
DECLARE evidence jsonb;
BEGIN
 IF SESSION_USER<>'campus_ai_school_fact_reader'
    OR p_school IS DISTINCT FROM app.require_school_database_anchor() THEN
   RAISE EXCEPTION 'Family quality Reader denied' USING ERRCODE='28000';
 END IF;
 IF p_quality_sources IS NULL OR cardinality(p_quality_sources)=0
    OR cardinality(p_quality_sources)>100
    OR array_position(p_quality_sources,NULL) IS NOT NULL
    OR p_target_kind NOT IN (
      'student','student_school_relationship','login_mapping',
      'student_contact','guardian_relationship','person_region')
    OR p_target_source IS NULL OR btrim(p_target_source)=''
    OR length(p_target_source)>255 OR p_fields IS NULL
    OR cardinality(p_fields)=0 OR cardinality(p_fields)>20
    OR array_position(p_fields,NULL) IS NOT NULL OR p_at IS NULL THEN
   RAISE EXCEPTION 'Invalid exact family quality selection' USING ERRCODE='22023';
 END IF;
 SELECT coalesce(jsonb_agg(jsonb_build_object(
   'quality_kind',q.quality_kind,
   'field_name',q.field_name,
   'coverage_status',q.coverage_status,
   'dispute_status',q.dispute_status,
   'source_status',q.source_status,
   'review_due_at',q.review_due_at,
   'verified_at',q.verified_at,
   'valid_from',q.valid_from,
   'valid_to',q.valid_to,
   'version',q.version
 ) ORDER BY q.source_key,q.version),'[]'::jsonb) INTO evidence
 FROM public.school_fact_qualities q
 JOIN public.master_data_import_batches b ON b.school_id=q.school_id
   AND b.id=q.import_batch_id AND b.mode='canonical_atomic_changes'
   AND b.status='applied'
 WHERE q.school_id=p_school AND q.source_key=ANY(p_quality_sources)
   AND q.target_fact_kind=p_target_kind
   AND q.target_source_key=p_target_source
   AND q.owner_kind='person'
   AND q.field_name=ANY(p_fields)
   AND q.valid_from<=p_at AND (q.valid_to IS NULL OR p_at<q.valid_to)
   AND (q.revoked_at IS NULL OR p_at<q.revoked_at)
   AND q.verified_at<=statement_timestamp();
 RETURN jsonb_build_object('records',evidence,
   'school_fact_version',app.school_fact_current_version(p_school));
END;
$f$;
"""

FAMILY_DETAIL_SQL = r"""
CREATE FUNCTION app.school_fact_f7_family_quality_detail(p_school uuid,p_key text)
RETURNS jsonb LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path=pg_catalog AS $f$
DECLARE result jsonb;
BEGIN
 IF SESSION_USER<>'campus_ai_school_fact_reader'
    OR p_school IS DISTINCT FROM app.require_school_database_anchor() THEN
   RAISE EXCEPTION 'Family quality detail Reader denied' USING ERRCODE='28000';
 END IF;
 IF p_key IS NULL OR btrim(p_key)='' OR length(p_key)>255 THEN
   RAISE EXCEPTION 'Invalid exact family quality key' USING ERRCODE='22023';
 END IF;
 SELECT jsonb_build_object('version',q.version,'content',jsonb_strip_nulls(
   jsonb_build_object(
     'quality_kind',q.quality_kind,'target_fact_kind',q.target_fact_kind,
     'owner_kind',q.owner_kind,'person_source_key',p.source_key,
     'basis_reference',q.basis_reference,'responsibility',q.responsibility,
     'verified_at',q.verified_at,'target_source_key',q.target_source_key,
     'replacement_source_key',q.replacement_source_key,'field_name',q.field_name,
     'evidence_reference',q.evidence_reference,'coverage_status',q.coverage_status,
     'negative_code',q.negative_code,'dispute_status',q.dispute_status,
     'source_status',q.source_status,'last_success_at',q.last_success_at,
     'review_due_at',q.review_due_at,'valid_to',q.valid_to))) INTO result
 FROM public.school_fact_qualities q
 JOIN public.school_people p ON p.school_id=q.school_id AND p.id=q.person_id
 JOIN public.master_data_import_batches b ON b.school_id=q.school_id
   AND b.id=q.import_batch_id AND b.mode='canonical_atomic_changes'
   AND b.status='applied'
 WHERE q.school_id=p_school AND q.source_key=p_key AND q.owner_kind='person'
   AND q.target_fact_kind IN (
     'student','student_school_relationship','login_mapping',
     'student_contact','guardian_relationship','person_region')
 ORDER BY q.version DESC LIMIT 1;
 RETURN result;
END;
$f$;
"""


def upgrade() -> None:
    op.execute("SET LOCAL ROLE campus_ai_db_owner")
    op.execute(
        "ALTER TABLE public.school_fact_qualities "
        "DROP CONSTRAINT ck_school_fact_qualities_target"
    )
    values = ",".join("'" + kind + "'" for kind in QUALITY_TARGETS)
    op.execute(
        "ALTER TABLE public.school_fact_qualities "
        "ADD CONSTRAINT ck_school_fact_qualities_target "
        f"CHECK (target_fact_kind IN ({values}))"
    )
    op.execute("GRANT CREATE ON SCHEMA app TO campus_ai_context_definer")
    op.execute("SET LOCAL ROLE campus_ai_context_definer")
    op.execute(FAMILY_READ_SQL)
    op.execute(FAMILY_DETAIL_SQL)
    op.execute(
        "REVOKE ALL ON FUNCTION "
        "app.school_fact_f7_family_quality(uuid,text[],text,text,text[],timestamptz) "
        "FROM PUBLIC,campus_ai_runtime,campus_ai_school_fact_writer,campus_ai_migration,"
        "campus_ai_db_owner"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION app.school_fact_f7_family_quality"
        "(uuid,text[],text,text,text[],timestamptz) "
        "TO campus_ai_school_fact_reader"
    )
    op.execute(
        "REVOKE ALL ON FUNCTION app.school_fact_f7_family_quality_detail(uuid,text) "
        "FROM PUBLIC,campus_ai_runtime,campus_ai_school_fact_writer,campus_ai_migration,"
        "campus_ai_db_owner"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION app.school_fact_f7_family_quality_detail(uuid,text) "
        "TO campus_ai_school_fact_reader"
    )
    op.execute("SET LOCAL ROLE campus_ai_db_owner")
    op.execute("REVOKE CREATE ON SCHEMA app FROM campus_ai_context_definer")


def downgrade() -> None:
    raise RuntimeError("Retain F7 quality history; use a reviewed forward repair.")
