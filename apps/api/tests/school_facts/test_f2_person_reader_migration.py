"""Keep the name Reader independent of the legacy signed context policy."""

from importlib.util import module_from_spec, spec_from_file_location
from io import StringIO
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations

from app.db.schema_contract import schema_contract_head


def _migration():
    path = Path("alembic/versions/20260906_24_school_fact_person_reader.py")
    assert path.exists(), "Applied revision23 needs a new forward migration"
    spec = spec_from_file_location("f2_person_reader_migration", path)
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_person_reader_fix_only_limits_legacy_select_policy_roles():
    module = _migration()
    assert module.revision == "20260906_24"
    assert module.down_revision == "20260906_23"
    output = StringIO()
    context = MigrationContext.configure(
        dialect_name="postgresql", opts={"as_sql": True, "output_buffer": output}
    )
    with Operations.context(context):
        module.upgrade()
    # The independent policy-time helper is validated separately. The name
    # policy still changes only its roles, preserving expressions and anchor.
    sql = output.getvalue()
    policy_sql, remainder = sql.split(";", 1)
    assert " ".join(policy_sql.split()) + ";" == (
        "ALTER POLICY tenant_select ON public.school_person_names "
        "TO campus_ai_runtime, campus_ai_db_owner, campus_ai_migration, "
        "campus_ai_context_definer, campus_ai_school_fact_writer;"
    )
    assert "ALTER POLICY" not in remainder
    assert "DROP POLICY" not in remainder
    assert "GRANT SELECT" not in remainder
    assert "GRANT INSERT" not in remainder
    assert "GRANT UPDATE" not in remainder
    assert "GRANT DELETE" not in remainder
    assert "app.require_school_context" not in remainder


def test_f2_person_reader_schema_is_explicit_and_f1_head_is_unchanged():
    assert schema_contract_head("school-facts-f2-v24") == "20260906_24"
    assert schema_contract_head("school-facts-f1-v22") == "20260905_22"


def test_person_reader_boundary_cannot_be_silently_downgraded():
    with pytest.raises(RuntimeError, match="backup"):
        _migration().downgrade()


def test_f2_directory_refuses_pre_fix_schema_before_building_authority():
    from unittest.mock import MagicMock

    from app.composition.school_fact_directory_services import create_directory_read_components

    with pytest.raises(ValueError, match="exact schema25"):
        create_directory_read_components(
            sessions=MagicMock(),
            settings=MagicMock(schema_contract="school-facts-f2-v23"),
            configuration=MagicMock(),
            legacy_scope_id=MagicMock(),
            reference_key=b"unused-schema-rejection",
        )
