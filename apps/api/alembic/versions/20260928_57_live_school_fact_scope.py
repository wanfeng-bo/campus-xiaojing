"""Bounded source metadata for explicitly authorized school fact coverage.

Existing content projections, exact grants and their temporal guards are retained.
The new reader functions expose only source coordinates, never business values.
"""

from __future__ import annotations

import runpy
from pathlib import Path

from sqlalchemy import text

from alembic import op

revision = "20260928_57"
down_revision = "20260924_56"
branch_labels = None
depends_on = None


SCOPE_KEYS_SQL = r"""
CREATE FUNCTION app.school_fact_scope_keys(
 p_school uuid,p_family text,p_kind text,p_after_key text,p_limit integer
) RETURNS jsonb LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path=pg_catalog AS $f$
DECLARE allowed text[]; selected text[]; more boolean;
BEGIN
 IF SESSION_USER <> 'campus_ai_school_fact_reader'
    OR p_school IS DISTINCT FROM app.require_school_database_anchor() THEN
   RAISE EXCEPTION 'School source enumeration denied' USING ERRCODE='28000';
 END IF;
 allowed:=CASE p_family
 WHEN 'f2' THEN ARRAY['assignment','fact_quality','location','location_alias',
 'official_contact','organization','organization_campus_relationship',
 'organization_location','organization_membership','organization_profile','person',
 'person_contact','person_workplace','policy','policy_scope','policy_version','school',
 'school_service']
 WHEN 'f3' THEN ARRAY['assignment','guardian_relationship','login_mapping','person',
 'person_region','student','student_contact','student_school_relationship']
 WHEN 'f4' THEN ARRAY['class_lineage','class_location','class_placement',
 'class_position_definition','class_teacher_assignment','education_calendar_rule',
 'education_class','education_cohort','education_grade','education_grade_group',
 'education_offering','education_stage','education_term','student_class_membership',
 'student_class_position','student_cohort_membership','student_grade_placement']
 WHEN 'f5' THEN ARRAY['course_subject','offering_member_source','teaching_assignment',
 'teaching_course','teaching_group','teaching_group_member','teaching_offering',
 'teaching_organization_scope','teaching_subject']
 WHEN 'f6' THEN ARRAY['dorm_bed','dorm_room','dormitory_building_profile',
 'student_bed_residence'] END;
 IF allowed IS NULL OR p_kind IS NULL OR NOT p_kind=ANY(allowed)
    OR p_limit IS NULL OR p_limit<1 OR p_limit>100
    OR p_after_key IS NOT NULL AND (p_after_key='' OR length(p_after_key)>255
      OR p_after_key<>btrim(p_after_key) OR p_after_key LIKE '%*%'
      OR p_after_key LIKE '%?%') THEN
   RAISE EXCEPTION 'Invalid school source selection' USING ERRCODE='22023';
 END IF;
 IF NOT app_private.school_fact_identity_root_is_effective_at(
    p_school,'school',p_school,statement_timestamp()) THEN
   RAISE EXCEPTION 'School source enumeration denied' USING ERRCODE='28000';
 END IF;
 SELECT COALESCE(array_agg(k ORDER BY k COLLATE "C"),ARRAY[]::text[]) INTO selected
 FROM (
   SELECT DISTINCT l.fact_source_key COLLATE "C" AS k
   FROM public.school_fact_change_lineage l
   JOIN public.master_data_import_batches b ON b.school_id=l.school_id AND b.id=l.batch_id
   WHERE l.school_id=p_school AND l.fact_kind=p_kind AND b.status='applied'
     AND (p_after_key IS NULL OR l.fact_source_key COLLATE "C">p_after_key COLLATE "C")
   ORDER BY k LIMIT p_limit+1
 ) candidates;
 more:=cardinality(selected)>p_limit;
 selected:=selected[1:p_limit];
 RETURN jsonb_build_object('keys',to_jsonb(selected),'has_more',more,
   'next_after',CASE WHEN more THEN selected[cardinality(selected)] ELSE NULL END,
   'school_fact_version',app.school_fact_current_version(p_school));
END;
$f$;
REVOKE ALL ON FUNCTION app.school_fact_scope_keys(uuid,text,text,text,integer) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app.school_fact_scope_keys(uuid,text,text,text,integer)
 TO campus_ai_school_fact_reader;
"""


STUDENT_IMPACT_KEYS_SQL = r"""
CREATE FUNCTION app.school_fact_f6_student_impact_keys(
 p_school uuid,p_student text,p_at timestamptz
) RETURNS jsonb LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path=pg_catalog AS $f$
DECLARE student uuid; keys text[];
BEGIN
 IF SESSION_USER <> 'campus_ai_school_fact_reader'
    OR p_school IS DISTINCT FROM app.require_school_database_anchor() THEN
   RAISE EXCEPTION 'Student residence source enumeration denied' USING ERRCODE='28000';
 END IF;
 IF p_student IS NULL OR p_student='' OR length(p_student)>255
    OR p_student<>btrim(p_student) OR p_student LIKE '%*%' OR p_student LIKE '%?%'
    OR p_at IS NULL THEN
   RAISE EXCEPTION 'Invalid student source selection' USING ERRCODE='22023';
 END IF;
 IF NOT app_private.school_fact_identity_root_is_effective_at(
    p_school,'school',p_school,statement_timestamp()) THEN
   RAISE EXCEPTION 'Student residence source enumeration denied' USING ERRCODE='28000';
 END IF;
 SELECT s.id INTO student FROM public.students s
 WHERE s.school_id=p_school AND s.source_key=p_student;
 IF student IS NULL THEN
   RAISE EXCEPTION 'Student source unavailable' USING ERRCODE='22023';
 END IF;
 SELECT COALESCE(array_agg(k ORDER BY k COLLATE "C"),ARRAY[]::text[]) INTO keys
 FROM (
   SELECT DISTINCT app_private.f6_source_key(p_school,'student_bed_residence',r.id) AS k
   FROM public.student_bed_residences r
   WHERE r.school_id=p_school AND r.student_id=student AND r.revoked_at IS NULL
     AND (r.valid_to IS NULL OR r.valid_to>p_at)
   ORDER BY k LIMIT 101
 ) impacted;
 IF cardinality(keys)>100 OR array_position(keys,NULL) IS NOT NULL THEN
   RAISE EXCEPTION 'Student residence impact set exceeds fixed projection'
    USING ERRCODE='54000';
 END IF;
 RETURN jsonb_build_object('keys',to_jsonb(keys),
   'school_fact_version',app.school_fact_current_version(p_school));
END;
$f$;
REVOKE ALL ON FUNCTION app.school_fact_f6_student_impact_keys(uuid,text,timestamptz) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app.school_fact_f6_student_impact_keys(uuid,text,timestamptz)
 TO campus_ai_school_fact_reader;
"""


STUDENT_RELATIONSHIP_KEYS_SQL = r"""
CREATE FUNCTION app.school_fact_f6_student_school_relationship_keys(
 p_school uuid,p_student text
) RETURNS jsonb LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path=pg_catalog AS $f$
DECLARE student uuid; keys text[];
BEGIN
 IF SESSION_USER <> 'campus_ai_school_fact_reader'
    OR p_school IS DISTINCT FROM app.require_school_database_anchor() THEN
   RAISE EXCEPTION 'Student relationship source enumeration denied' USING ERRCODE='28000';
 END IF;
 IF p_student IS NULL OR p_student='' OR length(p_student)>255
    OR p_student<>btrim(p_student) OR p_student LIKE '%*%' OR p_student LIKE '%?%' THEN
   RAISE EXCEPTION 'Invalid student source selection' USING ERRCODE='22023';
 END IF;
 IF NOT app_private.school_fact_identity_root_is_effective_at(
    p_school,'school',p_school,statement_timestamp()) THEN
   RAISE EXCEPTION 'Student relationship source enumeration denied' USING ERRCODE='28000';
 END IF;
 SELECT s.id INTO student FROM public.students s
 WHERE s.school_id=p_school AND s.source_key=p_student;
 IF student IS NULL THEN
   RAISE EXCEPTION 'Student source unavailable' USING ERRCODE='22023';
 END IF;
 SELECT COALESCE(array_agg(k ORDER BY k COLLATE "C"),ARRAY[]::text[]) INTO keys
 FROM (
   SELECT DISTINCT r.source_key COLLATE "C" AS k
   FROM public.student_school_relationships r
   WHERE r.school_id=p_school AND r.student_id=student AND r.superseded_at IS NULL
   ORDER BY k NULLS FIRST LIMIT 101
 ) relationships;
 IF cardinality(keys)>100 OR array_position(keys,NULL) IS NOT NULL THEN
   RAISE EXCEPTION 'Student relationship set exceeds fixed projection' USING ERRCODE='54000';
 END IF;
 RETURN jsonb_build_object('keys',to_jsonb(keys),
   'school_fact_version',app.school_fact_current_version(p_school));
END;
$f$;
REVOKE ALL ON FUNCTION app.school_fact_f6_student_school_relationship_keys(uuid,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app.school_fact_f6_student_school_relationship_keys(uuid,text)
 TO campus_ai_school_fact_reader;
"""


def quality_kind_sql() -> str:
    """Change only the object coverage predicate of the retained SQL grant check."""
    prior = runpy.run_path(str(Path(__file__).with_name("20260924_54_f7_exact_read_contracts.py")))
    source = prior["QUALITY_KIND_SQL"]
    original = """AND jsonb_typeof(sv.reference_document->'object_keys')='array'
     AND (sv.reference_document->'object_keys') ? p_key"""
    replacement = """AND (
       (NOT sv.reference_document ? 'coverage'
        AND jsonb_typeof(sv.reference_document->'object_keys')='array'
        AND (sv.reference_document->'object_keys') ? p_key)
       OR (sv.reference_document->>'coverage'='school'
        AND sv.reference_document->'object_keys'='[]'::jsonb
        AND sv.reference_document->'creation_keys'='[]'::jsonb
        AND (SELECT count(*) FROM jsonb_object_keys(sv.reference_document))=5)
     )"""
    if source.count(original) != 1:
        raise RuntimeError("Retained exact quality scope guard changed")
    return source.replace(
        "CREATE FUNCTION app.school_fact_f7_package_quality_kind(",
        "CREATE OR REPLACE FUNCTION app.school_fact_f7_package_quality_kind(",
        1,
    ).replace(original, replacement, 1)


def upgrade() -> None:
    op.execute("SET LOCAL ROLE campus_ai_db_owner")
    op.execute(quality_kind_sql())
    temporary_create = (
        not op.get_bind()
        .execute(text("SELECT has_schema_privilege('campus_ai_context_definer','app','CREATE')"))
        .scalar_one()
    )
    if temporary_create:
        op.execute("GRANT CREATE ON SCHEMA app TO campus_ai_context_definer")
    op.execute("SET LOCAL ROLE campus_ai_context_definer")
    op.execute(SCOPE_KEYS_SQL)
    op.execute(STUDENT_IMPACT_KEYS_SQL)
    op.execute(STUDENT_RELATIONSHIP_KEYS_SQL)
    op.execute("SET LOCAL ROLE campus_ai_db_owner")
    if temporary_create:
        op.execute("REVOKE CREATE ON SCHEMA app FROM campus_ai_context_definer")


def downgrade() -> None:
    raise RuntimeError("Published live school scope metadata is append-only")
