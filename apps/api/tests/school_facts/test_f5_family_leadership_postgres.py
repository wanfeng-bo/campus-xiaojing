"""Migration contract and opt-in owned PostgreSQL capability checks."""

import importlib.util
import os
from pathlib import Path

import pytest


def migration():
    path = Path(__file__).parents[2] / "alembic/versions/20260914_33_f5_family_leadership.py"
    assert path.exists(), "F2 leadership narrow SQL migration is required"
    spec = importlib.util.spec_from_file_location("f5_leadership_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_is_append_only_and_differences_use_f2_fields():
    module = migration()
    assert module.down_revision == "20260914_32"
    assert "appointment_source" not in module.LEADERSHIP_SQL
    assert "approval_reference" not in module.LEADERSHIP_SQL
    assert "person_contact_points" not in module.LEADERSHIP_SQL
    for field in (
        "display_name",
        "position_title",
        "person_source_key",
        "organization_source_key",
        "contact_value",
        "contact_kind",
        "service_hours",
    ):
        assert field in module.DIFFERENCE_SQL
    assert "school_fact_f3_field_changed" not in module.DIFFERENCE_SQL


@pytest.mark.skipif(not os.environ.get("F5_LEADERSHIP_PG"), reason="explicit owned F5 PG required")
def test_real_pg_runtime_cannot_call_leadership_projection():
    import json

    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError

    from tests.school_facts.test_f3_family_postgres import safe_test_engine

    metadata = json.loads(Path(os.environ["F3_TEST_TARGET_METADATA"]).read_text())
    assert metadata["owner"] == "F5 persistence and reader controlled tests"
    engine = safe_test_engine(Path(os.environ["F3_TEST_DATABASE_CONFIG"]), "runtime")
    try:
        with engine.connect() as connection:
            with pytest.raises(DBAPIError):
                connection.execute(
                    text(
                        "SELECT app.school_fact_family_leadership("
                        "NULL,NULL,NULL,1,ARRAY['principal'],ARRAY[]::text[],false)"
                    )
                )
    finally:
        engine.dispose()


@pytest.mark.skipif(not os.environ.get("F5_LEADERSHIP_PG"), reason="explicit owned F5 PG required")
def test_real_pg_current_published_leader_without_contact_and_stale_guardian_denied():
    import json

    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError

    from tests.school_facts.test_f3_family_postgres import safe_test_engine

    metadata = json.loads(Path(os.environ["F3_TEST_TARGET_METADATA"]).read_text())
    assert metadata["owner"] == "F5 persistence and reader controlled tests"
    config = Path(os.environ["F3_TEST_DATABASE_CONFIG"])
    owner, reader = safe_test_engine(config, "migration"), safe_test_engine(config, "reader")
    try:
        with owner.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) in {
                "20260914_33",
                "20260914_34",
                "20260914_35",
                "20260914_36",
                "20260914_37",
                "20260914_38",
                "20260914_39",
                "20260914_40",
            }
            guardian = connection.execute(
                text("""SELECT school_id,guardian_person_id,id,version
                FROM guardian_relationships WHERE relationship_kind='verified_guardian'
                AND status='confirmed' AND authority_version=1 AND revoked_at IS NULL
                AND valid_from<=now() AND (valid_to IS NULL OR now()<valid_to)
                ORDER BY id LIMIT 1""")
            ).one()
            code = connection.scalar(
                text("""SELECT a.semantic_role_code
                FROM staff_assignment_versions a
                JOIN school_fact_change_lineage l ON l.school_id=a.school_id
                AND l.fact_kind='assignment' AND l.target_id=a.assignment_id
                AND l.target_version=a.target_version
                JOIN master_data_import_batches b ON b.id=l.batch_id AND b.school_id=l.school_id
                WHERE b.status='applied' AND a.status='confirmed' AND a.revoked_at IS NULL
                AND a.effective_from<=now() AND (a.effective_to IS NULL OR now()<a.effective_to)
                AND (a.planned_valid_to IS NULL OR now()<a.planned_valid_to)
                ORDER BY a.assignment_id LIMIT 1""")
            )
            assert code, "Fixture requires an actually published current assignment"
        params = dict(
            s=guardian.school_id,
            a=guardian.guardian_person_id,
            r=guardian.id,
            v=guardian.version,
            codes=[code],
        )
        sql = text("""SELECT app.school_fact_family_leadership(:s,:a,:r,:v,
                     CAST(:codes AS text[]),ARRAY[]::text[],false)""")
        with reader.connect() as connection:
            result = connection.scalar(sql, params)
            assert result["status"] == "available"
            assert result["leaders"]
            assert all(not leader["official_contacts"] for leader in result["leaders"])
            assert not {"approval_reference", "appointment_source"} & set(result["leaders"][0])
            with pytest.raises(DBAPIError):
                connection.scalar(sql, params | {"v": guardian.version + 1})
    finally:
        owner.dispose()
        reader.dispose()
