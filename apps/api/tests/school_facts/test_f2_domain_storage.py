from importlib import import_module


def test_f2_models_have_temporal_identity_same_school_references_and_closed_types():
    from app.composition import school_fact_persistence
    from app.db.base import Base

    module = import_module("app.platform.school_facts.models")
    for kind, model in module.F2_MODELS.items():
        table = model.__table__
        assert {
            "source_key",
            "version",
            "valid_from",
            "valid_to",
            "revoked_at",
            "source_id",
            "import_batch_id",
        } <= set(table.columns.keys())
        assert any(
            set(c.columns.keys()) == {"school_id", "source_key", "version"}
            for c in table.constraints
        )
        for fk in table.foreign_key_constraints:
            if len(fk.column_keys) > 1:
                assert "school_id" in fk.column_keys
        assert kind in school_fact_persistence.F2_MODELS
        assert table.name in Base.metadata.tables


def test_f2_schema_has_explicit_head():
    from app.db.schema_contract import schema_contract_head

    assert schema_contract_head("school-facts-f2-v24") == "20260906_24"


def test_f2_migration_is_append_only_and_closes_new_table_acl():
    from pathlib import Path

    path = Path("alembic/versions/20260906_23_school_fact_f2.py")
    assert path.exists(), "F2 needs its own append-only revision"
    source = path.read_text(encoding="utf-8")
    assert 'down_revision = "20260905_22"' in source
    assert "FORCE ROW LEVEL SECURITY" in source
    assert "sf5b_anchor_guard" in source
    assert "UPDATE (valid_to, revoked_at)" in source
    assert "TO campus_ai_school_fact_reader" in source


def test_f2_atomic_correction_orders_terminal_before_replacement_and_quality():
    from datetime import UTC, datetime

    from app.composition.school_fact_persistence import SqlAlchemySchoolFactPersistence
    from app.platform.master_data_imports.models import MasterDataImportUnit
    from app.platform.school_facts.contracts import CanonicalFactChangeSetV2, CanonicalFactChangeV2

    at = datetime(2026, 9, 1, tzinfo=UTC)

    def unit(kind, key, operation, content, expected=0):
        return CanonicalFactChangeV2.model_validate(
            dict(
                fact_kind=kind,
                source_key=key,
                operation=operation,
                content=content,
                expected_version=expected,
                effective_at=at,
                origin=dict(sheet_code="f2", row_number=2),
            )
        )

    replacement = unit(
        "organization_location",
        "office:new",
        "upsert",
        dict(
            organization_source_key="org:one",
            location_source_key="loc:one",
            purpose="office",
            is_primary=True,
            basis_reference="basis",
        ),
    )
    terminal = unit("organization_location", "office:old", "end", None, 1)
    quality = unit(
        "fact_quality",
        "quality:correction",
        "upsert",
        dict(
            quality_kind="correction",
            target_fact_kind="organization_location",
            owner_kind="organization",
            organization_source_key="org:one",
            target_source_key="office:old",
            replacement_source_key="office:new",
            basis_reference="basis",
            responsibility="office",
            verified_at=at,
            evidence_reference="review",
        ),
    )
    plan = CanonicalFactChangeSetV2(
        source_sha256="a" * 64, base_version=1, changes=(quality, replacement, terminal)
    )
    result = SqlAlchemySchoolFactPersistence._ordered_change_pairs(
        tuple(MasterDataImportUnit() for _ in range(3)), plan, {}
    )
    assert [change.source_key for _, change in result] == [
        "office:old",
        "office:new",
        "quality:correction",
    ]


def test_f2_migration_offline_sql_matches_registered_tables():
    from importlib.util import module_from_spec, spec_from_file_location
    from io import StringIO

    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    spec = spec_from_file_location("f2_migration", "alembic/versions/20260906_23_school_fact_f2.py")
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    output = StringIO()
    context = MigrationContext.configure(
        dialect_name="postgresql", opts={"as_sql": True, "output_buffer": output}
    )
    with Operations.context(context):
        module.upgrade()
    sql = output.getvalue()
    for table in module._F2_TABLES:
        assert f"CREATE TABLE {table}" in sql
        assert f"ALTER TABLE public.{table} FORCE ROW LEVEL SECURITY" in sql
    assert "GRANT SELECT, INSERT, UPDATE (valid_to, revoked_at)" in sql
    assert "governance_bootstrap_states" in sql
    assert "AS RESTRICTIVE FOR ALL TO PUBLIC" in sql
    assert "LEAST(valid_to, revoked_at)" in sql


def test_f2_reader_root_wrapper_is_narrow_anchored_and_reader_only():
    from importlib.util import module_from_spec, spec_from_file_location
    from io import StringIO

    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    spec = spec_from_file_location(
        "f2_root_migration", "alembic/versions/20260906_23_school_fact_f2.py"
    )
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    output = StringIO()
    context = MigrationContext.configure(
        dialect_name="postgresql", opts={"as_sql": True, "output_buffer": output}
    )
    with Operations.context(context):
        module.upgrade()
    sql = output.getvalue()
    name = "app.school_fact_f2_reader_root_is_effective_at"
    assert f"CREATE FUNCTION {name}(" in sql
    body = sql.split(f"CREATE FUNCTION {name}(", 1)[1].split("$f2_root$;", 1)[0]
    assert "SESSION_USER <> 'campus_ai_school_fact_reader'" in body
    assert "p_school_id IS NULL OR p_target_id IS NULL OR p_as_of IS NULL" in body
    assert "'school', 'person', 'organization', 'location'" in body
    assert "'student'" not in body
    assert "app.require_school_database_anchor() IS DISTINCT FROM p_school_id" in body
    assert "RETURN app.school_fact_root_is_effective_at(" in body
    assert "SECURITY DEFINER" in body and "SET search_path = pg_catalog" in body
    signature = f"{name}(uuid,text,uuid,timestamptz)"
    assert f"GRANT EXECUTE ON FUNCTION {signature} TO campus_ai_school_fact_reader" in sql
    revoke = sql.split(f"REVOKE ALL ON FUNCTION {signature} FROM ", 1)[1].split(";", 1)[0]
    assert all(
        role in revoke
        for role in (
            "PUBLIC",
            "campus_ai_runtime",
            "campus_ai_school_fact_writer",
            "campus_ai_governance_writer",
            "campus_ai_db_owner",
            "campus_ai_migration",
        )
    )
    assert sql.index("SET LOCAL ROLE campus_ai_context_definer") < sql.index(
        f"CREATE FUNCTION {name}("
    )
    assert "REVOKE CREATE ON SCHEMA app FROM campus_ai_context_definer" in sql

def test_f2_same_school_foreign_keys_have_unique_referenced_columns():
    from sqlalchemy import PrimaryKeyConstraint, UniqueConstraint

    from app.composition import school_fact_persistence  # noqa: F401
    from app.domains.campus import models as campus_models  # noqa: F401
    from app.platform.policies import models as policy_models  # noqa: F401
    from app.platform.school_facts.models import F2_MODELS

    for model in F2_MODELS.values():
        for fk in model.__table__.foreign_key_constraints:
            target = fk.referred_table
            columns = tuple(element.column.name for element in fk.elements)
            assert any(
                isinstance(constraint, (PrimaryKeyConstraint, UniqueConstraint))
                and tuple(constraint.columns.keys()) == columns
                for constraint in target.constraints
            ), (model.__tablename__, target.name, columns)

def test_f2_governance_writer_has_schema_usage_without_create():
    from pathlib import Path

    source = Path('alembic/versions/20260906_23_school_fact_f2.py').read_text(encoding='utf8')
    assert 'GRANT USAGE ON SCHEMA public, app TO campus_ai_governance_writer' in source
    assert 'GRANT CREATE ON SCHEMA public' not in source
