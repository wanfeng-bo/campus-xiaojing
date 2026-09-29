from pathlib import Path

ROOT = Path(__file__).parents[2]
M = ROOT / "alembic/versions/20260913_27_family_fact_corrections.py"


def test_revision27_is_additive_writer_only_non_authoritative_intake():
    s = M.read_text(encoding="utf-8")
    assert 'revision = "20260913_27"' in s and 'down_revision = "20260913_26"' in s
    assert "family_fact_correction_reports" in s
    assert "campus_ai_school_fact_writer" in s
    assert "GRANT SELECT,INSERT,UPDATE" in s
    assert "campus_ai_runtime" in s and "campus_ai_school_fact_reader" in s
    assert "canonical" not in s.lower()
    assert "submitted','resolved" in s
