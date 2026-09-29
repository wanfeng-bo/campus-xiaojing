"""Include exact teaching-relation quality in the existing own-child projection.

The selected child, authorized fact closure and temporal guards stay with the
existing Reader. No teaching relation or quality source is read by broad scope.
"""

from __future__ import annotations

import runpy
from pathlib import Path

from sqlalchemy import text

from alembic import op

revision = "20260924_56"
down_revision = "20260924_55"
branch_labels = None
depends_on = None


def projection_sql() -> str:
    retained = runpy.run_path(
        str(Path(__file__).with_name("20260924_50_f7_education_quality_coverage_basis.py"))
    )
    replace = retained["_replace_once"]
    sql = retained["_source"](*retained["_PRIOR"][2])
    sql = replace(
        sql,
        "q.field_name,q.target_source_key,q.replacement_source_key",
        "q.field_name,q.coverage_basis,q.target_source_key,q.replacement_source_key",
    )
    sql = replace(
        sql,
        "'negative_code',negative_code,'coverage_status',coverage_status,'dispute_status'",
        "'negative_code',negative_code,'coverage_status',coverage_status,"
        "'coverage_basis',coverage_basis,'dispute_status'",
    )
    sql = replace(
        sql,
        "WITH chosen AS MATERIALIZED (SELECT * FROM jsonb_to_recordset(selected) "
        "k(kind text,source_key text)), eligible AS (",
        """WITH chosen AS MATERIALIZED (
 SELECT * FROM jsonb_to_recordset(selected) k(kind text,source_key text)
 ), quality_owners AS MATERIALIZED (
 SELECT e.id,e.kind,e.source_key,e.id AS offering_id,e.source_key AS offering_key,
 NULL::text AS teaching_role
 FROM chosen own JOIN public.school_education_facts e
 ON e.school_id=p_school AND e.kind='teaching_offering'
 AND own.kind=e.kind AND own.source_key=e.source_key
 UNION ALL
 SELECT e.id,e.kind,e.source_key,o.id,o.source_key,v.payload->>'teaching_role'
 FROM chosen own JOIN public.school_education_facts e
 ON e.school_id=p_school AND e.kind='teaching_assignment'
 AND own.kind=e.kind AND own.source_key=e.source_key
 JOIN public.school_education_versions v
 ON v.school_id=p_school AND v.fact_id=e.id AND v.superseded_at IS NULL
 JOIN public.master_data_import_batches vb
 ON vb.school_id=p_school AND vb.id=v.import_batch_id AND vb.status='applied'
 JOIN public.school_education_references r
 ON r.school_id=p_school AND r.version_id=v.id AND r.field_name='offering_source_key'
 JOIN public.school_education_facts o
 ON o.school_id=p_school AND o.id=r.education_id AND o.kind='teaching_offering'
 JOIN chosen offered ON offered.kind=o.kind AND offered.source_key=o.source_key
 WHERE v.valid_from<=p_at AND (v.valid_to IS NULL OR p_at<v.valid_to)
 AND v.payload->>'teaching_role' IN ('lead','co_teacher')
 ), eligible AS (""",
    )
    sql = replace(
        sql,
        "q.field_name,q.coverage_basis,q.target_source_key,q.replacement_source_key",
        "q.field_name,q.coverage_basis,COALESCE(q.target_source_key,"
        "CASE WHEN e.kind='teaching_assignment' THEN e.source_key END) AS target_source_key,"
        "q.replacement_source_key",
    )
    sql = replace(
        sql,
        "e.source_key AS offering_key,l.created_at AS published_at",
        "e.offering_key,e.source_key AS quality_owner_source_key,l.created_at AS published_at",
    )
    sql = replace(
        sql,
        "FROM chosen own JOIN public.school_education_facts e ON e.school_id=p_school "
        "AND e.kind='teaching_offering'\n AND own.kind=e.kind AND own.source_key=e.source_key",
        "FROM quality_owners e",
    )
    sql = replace(
        sql,
        "AND q.teaching_role IN ('lead','co_teacher') AND q.verified_at<=p_at",
        "AND ((e.kind='teaching_offering' AND q.teaching_role IN ('lead','co_teacher')) "
        "OR (e.kind='teaching_assignment' AND "
        "(q.teaching_role IS NULL OR q.teaching_role=e.teaching_role))) "
        "AND (e.kind='teaching_offering' OR q.target_source_key IS NULL "
        "OR q.target_source_key=e.source_key) AND q.verified_at<=p_at",
    )
    sql = replace(
        sql,
        "WHERE ov.school_id=p_school AND ov.fact_id=e.id",
        "WHERE ov.school_id=p_school AND ov.fact_id=e.offering_id",
    )
    sql = replace(
        sql,
        "'offering_source_key',offering_key,'teaching_role',teaching_role",
        "'offering_source_key',offering_key,"
        "'quality_owner_source_key',quality_owner_source_key,'teaching_role',teaching_role",
    )
    return sql


def education_projection_sql() -> str:
    retained = runpy.run_path(
        str(Path(__file__).with_name("20260924_53_f7_family_education_exact_coverage.py"))
    )
    # This Reader already selects the exact current child-visible owner. Make
    # an implicit self-owned claim explicit instead of treating it as a field.
    return retained["_replace_once"](
        retained["_function_sql"](),
        "q.academic_year,q.target_fact_kind,q.target_source_key,",
        "q.academic_year,q.target_fact_kind,COALESCE(q.target_source_key,"
        "CASE WHEN e.kind=q.target_fact_kind AND e.kind IN ("
        "'student_class_membership','class_placement','student_grade_placement',"
        "'class_teacher_assignment','class_location','student_class_position',"
        "'student_cohort_membership') THEN e.source_key END) AS target_source_key,",
    )


def teaching_boundary_sql() -> str:
    retained = runpy.run_path(
        str(Path(__file__).with_name("20260924_50_f7_education_quality_coverage_basis.py"))
    )
    source = retained["_source"](
        "20260914_34_f5_family_teaching.py",
        "FAMILY_SQL",
        "e9a555d103edd64e8f35eb6b9edf3bebc9d806e7560b272f58a2f6befd25fcb0",
    )
    marker = "CREATE FUNCTION app_private.f5_child_boundary("
    start = source.index(marker)
    sql = source[start : source.index("\n$f$;", start) + len("\n$f$;")]
    replace = retained["_replace_once"]
    sql = replace(sql, marker, "CREATE OR REPLACE FUNCTION app_private.f5_child_boundary(")
    return replace(
        sql,
        "AND q.teaching_role IN ('lead','co_teacher')",
        """AND (q.teaching_role IN ('lead','co_teacher') OR (
 q.teaching_role IS NULL AND EXISTS (
 SELECT 1 FROM public.school_education_facts own
 JOIN public.school_education_versions ov ON ov.school_id=p_school AND ov.fact_id=own.id
 JOIN public.master_data_import_batches ob ON ob.school_id=p_school
 AND ob.id=ov.import_batch_id AND ob.status='applied'
 WHERE own.school_id=p_school AND own.id=q.education_target_id
 AND own.kind='teaching_assignment' AND ov.superseded_at IS NULL
 AND ov.valid_from<p_until AND (ov.valid_to IS NULL OR p_at<ov.valid_to)
 AND ov.payload->>'teaching_role' IN ('lead','co_teacher')
 AND (q.target_source_key IS NULL OR q.target_source_key=own.source_key))))""",
    )


def upgrade() -> None:
    bind = op.get_bind()
    temporary_create = tuple(
        schema
        for schema in ("app", "app_private")
        if not bind.execute(
            text("SELECT has_schema_privilege('campus_ai_context_definer', :schema, 'CREATE')"),
            {"schema": schema},
        ).scalar_one()
    )
    for schema in temporary_create:
        op.execute(f"GRANT CREATE ON SCHEMA {schema} TO campus_ai_context_definer")
    op.execute("SET LOCAL ROLE campus_ai_context_definer")
    op.execute(projection_sql())
    op.execute(education_projection_sql())
    op.execute(teaching_boundary_sql())
    op.execute("SET LOCAL ROLE campus_ai_db_owner")
    for schema in temporary_create:
        op.execute(f"REVOKE CREATE ON SCHEMA {schema} FROM campus_ai_context_definer")


def downgrade() -> None:
    raise RuntimeError("Teaching quality ownership correction is append-only")
