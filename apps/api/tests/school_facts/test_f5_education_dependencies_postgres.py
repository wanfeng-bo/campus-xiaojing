"""Read-only checks in the explicitly owned F5 PostgreSQL fixture."""

import json
import os
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from tests.school_facts.test_f3_family_postgres import safe_test_engine

pytestmark = pytest.mark.skipif(
    not os.environ.get("F5_TEACHING_PG"), reason="explicit owned F5 PG required"
)


@pytest.fixture
def engines():
    metadata = json.loads(Path(os.environ["F3_TEST_TARGET_METADATA"]).read_text(encoding="utf-8"))
    assert metadata["owner"] == "F5 persistence and reader controlled tests"
    assert os.environ.get("PYTEST_DISABLE_PLUGIN_AUTOLOAD") == "1"
    engines = {
        role: safe_test_engine(Path(os.environ["F3_TEST_DATABASE_CONFIG"]), role)
        for role in ("migration", "reader", "writer", "runtime")
    }
    try:
        with engines["migration"].connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) in {
                "20260914_35",
                "20260914_36",
                "20260914_37",
                "20260914_38",
                "20260914_39",
                "20260914_40",
            }
            school = connection.scalar(
                text("SELECT id FROM schools WHERE source_key='school:f3-pg-fixture'")
            )
            assert school is not None
        yield engines, school
    finally:
        for engine in engines.values():
            engine.dispose()


def test_external_metadata_compiles_every_column_with_reader_and_stays_payload_free(engines):
    db, school = engines
    with db["reader"].connect() as connection:
        for kind in ("person", "assignment", "organization", "official_contact"):
            result = connection.scalar(
                text(
                    "SELECT app.school_fact_f5_external_boundary(:school,:kind,ARRAY[]::text[],CAST(:orgs AS text[]),now())"
                ),
                {
                    "school": school,
                    "kind": kind,
                    "orgs": ["organization:nonexistent"] if kind == "official_contact" else [],
                },
            )
            assert set(result) == {"school_fact_version", "next_valid_boundary"}
            assert result["next_valid_boundary"] is None


def test_f5_dependencies_execute_without_broad_projection(engines):
    db, school = engines
    with db["migration"].connect() as connection:
        key = connection.scalar(
            text(
                "SELECT source_key FROM school_education_facts WHERE school_id=:school AND kind='teaching_course' ORDER BY source_key LIMIT 1"
            ),
            {"school": school},
        )
    assert key is not None, "canonical F5 publication fixture required"
    with db["reader"].connect() as connection:
        result = connection.scalar(
            text("""SELECT app.school_fact_f5_dependencies(:school,'fact_quality',ARRAY[]::text[],
            'teaching_course',CAST(:keys AS text[]),'teaching_course',CAST(:keys AS text[]),ARRAY['display_name'],now(),NULL,
            ARRAY[]::text[],ARRAY[]::text[],ARRAY[]::text[],ARRAY[]::text[])"""),
            {"school": school, "keys": [key]},
        )
        assert result["records"] == []
        assert set(result) == {"school_fact_version", "records", "next_valid_boundary"}
        selected = connection.scalar(
            text(
                "SELECT app.school_fact_f5_dependency_selection(:school,'fact_quality','quality:nonexistent',now())"
            ),
            {"school": school},
        )
        assert selected["records"] == []


@pytest.mark.parametrize("role", ["writer", "runtime"])
def test_non_reader_cannot_execute_external_boundary(engines, role):
    db, school = engines
    with db[role].connect() as connection, pytest.raises(DBAPIError):
        connection.execute(
            text(
                "SELECT app.school_fact_f5_external_boundary(:school,'person',ARRAY[]::text[],ARRAY[]::text[],now())"
            ),
            {"school": school},
        )
