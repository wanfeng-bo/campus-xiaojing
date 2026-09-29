"""The append-only SQL must retain the exact quality gate and expose keys only."""

import runpy
from pathlib import Path


def migration():
    path = (
        Path(__file__).resolve().parents[2]
        / "alembic/versions/20260928_57_live_school_fact_scope.py"
    )
    assert path.exists(), "Live school metadata migration is missing"
    return runpy.run_path(str(path))


def test_scope_metadata_sql_is_fixed_bounded_and_anchor_guarded():
    sql = migration()["SCOPE_KEYS_SQL"]
    assert "SESSION_USER <> 'campus_ai_school_fact_reader'" in sql
    assert "app.require_school_database_anchor()" in sql
    assert "p_limit>100" in sql
    assert 'COLLATE "C"' in sql
    assert "b.status='applied'" in sql
    assert "school_fact_current_version" in sql
    assert " EXECUTE " not in sql.split("$f$;", 1)[0]
    assert "payload" not in sql and "content" not in sql


def test_quality_gate_keeps_exact_objects_and_all_current_grant_guards():
    sql = migration()["quality_kind_sql"]()
    assert "sv.schema_name='exact_fact_set' AND sv.schema_version=1" in sql
    assert "(sv.reference_document->'object_keys') ? p_key" in sql
    assert "sv.reference_document->>'coverage'='school'" in sql
    assert "gv.revoked_at IS NULL" in sql and "gv.formal_ended_at IS NULL" in sql
    assert "checked_at<gv.planned_valid_to" in sql
    assert "SESSION_USER <> 'campus_ai_school_fact_reader'" in sql
    assert "SECURITY INVOKER" in sql


def test_student_relationship_metadata_preserves_complete_current_root_set():
    sql = migration()["STUDENT_RELATIONSHIP_KEYS_SQL"]
    assert "SESSION_USER <> 'campus_ai_school_fact_reader'" in sql
    assert "app.require_school_database_anchor()" in sql
    assert "school_fact_identity_root_is_effective_at" in sql
    assert "r.superseded_at IS NULL" in sql
    assert "LIMIT 101" in sql and "cardinality(keys)>100" in sql
    assert "array_position(keys,NULL) IS NOT NULL" in sql
    assert "school_fact_current_version" in sql
    assert "valid_from" not in sql and "valid_to" not in sql
