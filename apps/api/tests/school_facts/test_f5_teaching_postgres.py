"""Only explicitly owned isolated F5 databases; never bootstrap or restore a database."""

import importlib.util
import json
import os
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError


def migration():
    path = Path(__file__).parents[2] / "alembic/versions/20260914_32_f5_teaching_facts.py"
    spec = importlib.util.spec_from_file_location("f5_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_append_only_migration_has_independent_reader_and_cross_root_constraints():
    module = migration()
    assert module.down_revision == "20260913_31"
    assert "f5_target_records" in module.STAFF_SQL
    assert "f4_allowed_fields" not in module.STAFF_SQL
    assert "BEFORE INSERT" in module.STORAGE_SQL
    assert "fact_kind" in module.STORAGE_SQL
    assert "education_kind" in module.STORAGE_SQL
    assert "EXCLUDE USING gist" in module.STORAGE_SQL


@pytest.fixture
def connection():
    if not os.environ.get("F5_TEACHING_PG"):
        pytest.skip("explicit owned F5 database required")
    assert os.environ.get("PYTEST_DISABLE_PLUGIN_AUTOLOAD") == "1"
    metadata = json.loads(Path(os.environ["F3_TEST_TARGET_METADATA"]).read_text(encoding="utf-8"))
    assert metadata["owner"] == "F5 persistence and reader controlled tests"
    from tests.school_facts.test_f3_family_postgres import safe_test_engine

    engine = safe_test_engine(Path(os.environ["F3_TEST_DATABASE_CONFIG"]), "migration")
    try:
        with engine.connect() as conn:
            transaction = conn.begin()
            assert conn.scalar(text("SELECT version_num FROM alembic_version")) in {
                "20260914_32",
                "20260914_33",
                "20260914_34",
                "20260914_35",
                "20260914_36",
                "20260914_37",
                "20260914_38",
                "20260914_39",
                "20260914_40",
            }
            assert conn.scalar(text("SELECT source_key FROM schools")) == "school:f3-pg-fixture"
            yield conn
            transaction.rollback()
    finally:
        engine.dispose()


def insert_version(connection, kind, payload, *, supplied_kind=None):
    root, version = uuid4(), uuid4()
    school = connection.scalar(text("SELECT id FROM schools"))
    source = connection.scalar(text("SELECT id FROM school_fact_sources LIMIT 1"))
    batch = connection.scalar(
        text("SELECT id FROM master_data_import_batches WHERE status='applied' LIMIT 1")
    )
    connection.execute(
        text(
            "INSERT INTO school_education_facts(id,school_id,kind,source_key,version) "
            "VALUES (:id,:school,:kind,:key,1)"
        ),
        dict(id=root, school=school, kind=kind, key="f5:" + uuid4().hex),
    )
    connection.execute(
        text(
            "INSERT INTO school_education_versions(id,school_id,fact_id,revision,valid_from,"
            "payload,source_id,import_batch_id,fact_kind) VALUES "
            "(:id,:school,:root,1,'2026-09-01',CAST(:payload AS jsonb),:source,:batch,:kind)"
        ),
        dict(
            id=version,
            school=school,
            root=root,
            payload=json.dumps(payload),
            source=source,
            batch=batch,
            kind=supplied_kind,
        ),
    )
    return version


def test_old_insert_derives_type_but_explicit_wrong_kind_is_rejected(connection):
    version = insert_version(connection, "education_class", {"display_name": "合成班"})
    assert (
        connection.scalar(
            text("SELECT fact_kind FROM school_education_versions WHERE id=:id"), {"id": version}
        )
        == "education_class"
    )
    with pytest.raises(DBAPIError), connection.begin_nested():
        insert_version(
            connection,
            "education_class",
            {"display_name": "合成班"},
            supplied_kind="teaching_course",
        )


def test_cross_source_duplicate_source_and_null_payload_are_rejected(connection):
    payload = {
        "offering_source_key": "offer:one",
        "source_kind": "administrative_class",
        "class_source_key": "class:one",
    }
    insert_version(connection, "offering_member_source", payload)
    with pytest.raises(DBAPIError), connection.begin_nested():
        insert_version(connection, "offering_member_source", payload)
    with pytest.raises(DBAPIError), connection.begin_nested():
        insert_version(
            connection, "offering_member_source", {**payload, "offering_source_key": None}
        )


def test_runtime_and_reader_have_no_direct_teaching_table_grants(connection):
    for role in ("campus_ai_runtime", "campus_ai_school_fact_reader"):
        assert not connection.scalar(
            text("SELECT has_table_privilege(:role,'public.school_education_versions','SELECT')"),
            {"role": role},
        )
        assert not connection.scalar(
            text(
                "SELECT has_function_privilege(:role,'app_private.f5_target_records"
                "(uuid,text,text[],text[],timestamptz,timestamptz,jsonb)','EXECUTE')"
            ),
            {"role": role},
        )


def test_wrong_education_reference_type_is_rejected_even_with_same_source_text(connection):
    target_version = insert_version(connection, "teaching_course", {"display_name": "合成课程"})
    target_id = connection.scalar(
        text("SELECT fact_id FROM school_education_versions WHERE id=:id"), {"id": target_version}
    )
    connection.execute(
        text("UPDATE school_education_facts SET source_key='class:wrong-type' WHERE id=:id"),
        {"id": target_id},
    )
    source_version = insert_version(
        connection,
        "offering_member_source",
        {
            "offering_source_key": "offer:wrong",
            "source_kind": "administrative_class",
            "class_source_key": "class:wrong-type",
        },
    )
    with pytest.raises(DBAPIError), connection.begin_nested():
        connection.execute(
            text(
                "INSERT INTO school_education_references(school_id,version_id,field_name,"
                "ordinal,education_id) SELECT school_id,:version,'class_source_key',0,:target "
                "FROM school_education_versions WHERE id=:version"
            ),
            {"version": source_version, "target": target_id},
        )


def test_concurrent_cross_root_source_insert_is_serialized_by_gist(connection):
    payload = {
        "offering_source_key": "offer:concurrent",
        "source_kind": "administrative_class",
        "class_source_key": "class:one",
    }
    insert_version(connection, "offering_member_source", payload)
    with connection.engine.connect() as other:
        transaction = other.begin()
        try:
            other.execute(text("SET LOCAL lock_timeout='200ms'"))
            with pytest.raises(DBAPIError) as caught:
                insert_version(other, "offering_member_source", payload)
            assert getattr(caught.value.orig, "sqlstate", None) == "55P03"
        finally:
            transaction.rollback()


@pytest.mark.parametrize(
    "kind,field,value",
    [
        ("offering_member_source", "source_kind", "administrative_class"),
        ("teaching_offering", "offering_status", "suspended"),
        ("teaching_assignment", "teaching_role", "co_teacher"),
        ("teaching_group", "group_mode", "shared"),
        ("teaching_organization_scope", "scope_basis", "academic_grade"),
    ],
)
def test_reader_accepts_only_registered_scalar_teaching_filters(connection, kind, field, value):
    from tests.school_facts.test_f3_family_postgres import safe_test_engine

    school = connection.scalar(text("SELECT id FROM schools"))
    engine = safe_test_engine(Path(os.environ["F3_TEST_DATABASE_CONFIG"]), "reader")
    statement = text(
        "SELECT app.school_fact_f5_targets(:school,:kind,CAST(:keys AS text[]),"
        "CAST(:fields AS text[]),CURRENT_TIMESTAMP,NULL,CAST(:filter AS jsonb))"
    )
    try:
        with engine.connect() as reader:
            result = reader.scalar(
                statement,
                dict(
                    school=school,
                    kind=kind,
                    keys=[],
                    fields=[field],
                    filter=json.dumps({field: value}),
                ),
            )
            assert result["records"] == []
            reader.rollback()
            for invalid in ([value], "unregistered_value"):
                with pytest.raises(DBAPIError):
                    reader.scalar(
                        statement,
                        dict(
                            school=school,
                            kind=kind,
                            keys=[],
                            fields=[field],
                            filter=json.dumps({field: invalid}),
                        ),
                    )
                reader.rollback()
    finally:
        engine.dispose()
