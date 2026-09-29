"""Owned F5 canonical Writer persistence/Reader SQL, separately from product duty tests."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from tests.school_facts.test_f3_family_postgres import existing_fixture_writer, safe_test_engine
from tests.school_facts.test_f4_writer_postgres import apply

pytestmark = pytest.mark.skipif(
    not os.environ.get("F5_TEACHING_PG"), reason="explicit owned F5 PG target required"
)


@pytest.fixture
def writer():
    assert os.environ.get("PYTEST_DISABLE_PLUGIN_AUTOLOAD") == "1"
    metadata = json.loads(Path(os.environ["F3_TEST_TARGET_METADATA"]).read_text(encoding="utf-8"))
    assert metadata["owner"] == "F5 persistence and reader controlled tests"
    engines = {
        role: safe_test_engine(Path(os.environ["F3_TEST_DATABASE_CONFIG"]), role)
        for role in ("migration", "runtime", "reader", "writer")
    }
    try:
        with engines["migration"].connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) in {
                "20260914_34",
                "20260914_35",
                "20260914_36",
                "20260914_37",
                "20260914_38",
                "20260914_39",
                "20260914_40",
                "20260914_41",
            }
            assert (
                connection.scalar(text("SELECT source_key FROM schools")) == "school:f3-pg-fixture"
            )
        actor, identity, context, service = existing_fixture_writer(engines)
        yield engines, actor, identity, context, service
    finally:
        for engine in engines.values():
            engine.dispose()


def test_canonical_writer_publishes_f5_and_reader_only_sees_requested_fields(writer):
    engines, actor, *_ = writer
    key = "course:f5-pg-" + uuid4().hex
    now = datetime.now(UTC)
    result = apply(
        writer,
        [
            {
                "fact_kind": "teaching_course",
                "source_key": key,
                "operation": "upsert",
                "expected_version": 0,
                "effective_at": now - timedelta(seconds=1),
                "content": {"display_name": "F5合成课程"},
            }
        ],
    )
    assert result.batch_id is not None
    with engines["migration"].connect() as connection:
        assert (
            connection.scalar(
                text(
                    "SELECT v.fact_kind FROM school_education_versions "
                    "v JOIN school_education_facts f ON f.id=v.fact_id WHERE f.source_key=:key"
                ),
                {"key": key},
            )
            == "teaching_course"
        )
        assert (
            connection.scalar(
                text("SELECT status FROM master_data_import_batches WHERE id=:batch"),
                {"batch": result.batch_id},
            )
            == "applied"
        )
    params = {
        "school": actor.school_id,
        "kind": "teaching_course",
        "keys": [key],
        "fields": ["display_name"],
        "at": now,
        "filter": "{}",
    }
    sql = text(
        "SELECT app.school_fact_f5_targets(:school,:kind,"
        ":keys,:fields,:at,NULL,CAST(:filter "
        "AS jsonb))"
    )
    with engines["reader"].connect() as connection:
        response = connection.scalar(sql, params)
        assert len(response["records"]) == 1
        assert response["records"][0]["source_key"] == key
        assert response["records"][0]["segments"][0]["content"] == {"display_name": "F5合成课程"}
        assert response["records"][0]["segments"][0]["source"]
        assert (
            connection.scalar(
                text("SELECT app.school_fact_f5_scope_is_current(:s,:kind,:keys,:at)"),
                {"s": actor.school_id, "kind": "teaching_course", "keys": [key], "at": now},
            )
            is True
        )
    for role in ("runtime", "writer"):
        with engines[role].connect() as connection, pytest.raises(DBAPIError):
            connection.scalar(sql, params)
    with engines["reader"].connect() as connection, pytest.raises(DBAPIError):
        connection.scalar(sql, params | {"fields": ["private_phone"]})
    with engines["reader"].connect() as connection, pytest.raises(DBAPIError):
        connection.scalar(
            text(
                "SELECT app.school_fact_f4_targets(:school,:kind,"
                ":keys,:fields,:at,NULL,CAST(:filter "
                "AS jsonb))"
            ),
            params,
        )


def test_reader_and_runtime_cannot_write_new_teaching_roots(writer):
    engines, actor, *_ = writer
    for role in ("runtime", "reader"):
        with engines[role].connect() as connection, pytest.raises(DBAPIError):
            connection.execute(
                text(
                    "INSERT INTO school_education_facts(id,school_id,kind,source_key,version) "
                    "VALUES (:id,:school,'teaching_course',:key,1)"
                ),
                {"id": uuid4(), "school": actor.school_id, "key": "course:blocked-" + uuid4().hex},
            )


def test_f5_creation_coordinate_returns_metadata_without_formal_content(writer):
    engines, actor, *_ = writer
    key = "course:coordinate-" + uuid4().hex
    with engines["reader"].connect() as connection:
        result = connection.scalar(
            text("SELECT app.school_fact_f5_coordinates(:school,:kind,:keys)"),
            {"school": actor.school_id, "kind": "teaching_course", "keys": [key]},
        )
        assert result["records"] == [{"source_key": key, "version": 0, "status": "not_recorded"}]
        with pytest.raises(DBAPIError), connection.begin_nested():
            connection.scalar(
                text("SELECT app.school_fact_f5_coordinates(:school,:kind,:keys)"),
                {"school": actor.school_id, "kind": "education_class", "keys": [key]},
            )
