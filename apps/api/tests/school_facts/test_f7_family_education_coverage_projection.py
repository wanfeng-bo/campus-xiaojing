"""The retained parent Reader must expose only proof for already-selected rows."""

import runpy
from pathlib import Path


def test_append_only_parent_projection_preserves_selection_and_adds_exact_basis():
    migration = runpy.run_path(str(
        Path(__file__).parents[2] / "alembic" / "versions"
        / "20260924_53_f7_family_education_exact_coverage.py"
    ))
    sql = migration["_function_sql"]()
    assert "app.school_fact_family_education_pre_v31" in sql
    assert "JOIN selected own" in sql
    assert "AND (q.target_source_key IS NULL OR EXISTS" in sql
    assert "q.coverage_basis" in sql
    assert "'target_fact_kind',target_fact_kind" in sql
    assert "'target_source_key',target_source_key" in sql
    assert "'coverage_basis',coverage_basis" in sql
