"""Append-only ownership fix preserves the existing selected-child boundary."""

import runpy
from pathlib import Path


def test_relation_quality_migration_keeps_exact_child_and_owner_selection():
    migration = runpy.run_path(
        str(
            Path(__file__).parents[2]
            / "alembic/versions"
            / "20260924_56_f7_family_teaching_quality_owner.py"
        )
    )
    assert migration["revision"] == "20260924_56"
    assert migration["down_revision"] == "20260924_55"
    teaching = migration["projection_sql"]()
    assert "app_private.f5_child_keys(p_school,p_student,p_at" in teaching
    assert (
        "JOIN chosen offered ON offered.kind=o.kind AND offered.source_key=o.source_key" in teaching
    )
    assert "v.superseded_at IS NULL" in teaching
    assert "v.payload->>'teaching_role' IN ('lead','co_teacher')" in teaching
    assert "OR q.target_source_key=e.source_key" in teaching
    assert "q.replacement_source_key IS NULL OR EXISTS" in teaching
    assert "'quality_owner_source_key',quality_owner_source_key" in teaching
    assert "q.verified_at<=p_at" in teaching
    assert "q.revoked_at IS NULL OR p_at<q.revoked_at" in teaching
    assert "q.purpose IS NULL AND q.contact_kind IS NULL" in teaching
    assert "q.field_name=ANY(app_private.f5_allowed_fields('teaching_assignment'))" in teaching
    education = migration["education_projection_sql"]()
    assert "app.school_fact_family_education_pre_v31" in education
    assert "JOIN selected own ON own.kind=e.kind AND own.source_key=e.source_key" in education
    assert "CASE WHEN e.kind=q.target_fact_kind" in education
    assert "q.target_source_key IS NULL OR EXISTS" in education
    assert "q.coverage_basis" in education
    assert "THEN 'item' ELSE 'field'" in education
    boundary = migration["teaching_boundary_sql"]()
    assert "app_private.f5_child_keys(p_school,p_student,p_at,p_until" in boundary
    assert "own.id=q.education_target_id" in boundary
    assert "q.target_source_key=own.source_key" in boundary
    assert "ov.payload->>'teaching_role' IN ('lead','co_teacher')" in boundary
    assert "q.valid_from,q.valid_to,q.revoked_at,q.verified_at,q.review_due_at" in boundary
