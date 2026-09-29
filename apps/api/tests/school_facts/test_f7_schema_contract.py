"""F7 new code never runs against the F6 database revision by tag alone."""

from app.db.schema_contract import schema_contract_head


def test_f7_schema_contract_points_to_both_append_only_migrations():
    assert schema_contract_head("school-facts-f7-v47") == "20260924_47"
    assert schema_contract_head("school-facts-f6-v45") == "20260922_45"


def test_f7_writer_identity_lookup_is_rebuildable_without_expanding_identity_owner_acl():
    from pathlib import Path

    migration = Path("alembic/versions/20260924_48_f7_writer_identity_lookup.py").read_text(
        encoding="utf-8"
    )
    assert schema_contract_head("school-facts-f7-v48") == "20260924_48"
    assert 'down_revision = "20260924_47"' in migration
    assert "SESSION_USER <> 'campus_ai_school_fact_writer'" in migration
    assert "app.context_purpose" in migration and "app.context_kind" in migration
    assert "GRANT EXECUTE ON FUNCTION app.school_fact_lock_existing_identity_user_id" in migration
    assert "campus_ai_school_fact_writer" in migration
    assert "GRANT EXECUTE ON FUNCTION app.lock_existing_identity_user_id" not in migration
    assert "app.school_fact_lock_existing_identity_user_id" in Path(
        "app/platform/identity/tuple_validator.py"
    ).read_text(encoding="utf-8")


def test_f7_coverage_basis_schema_requires_its_append_only_migration():
    from pathlib import Path

    migration = Path("alembic/versions/20260924_49_f7_quality_coverage_basis.py").read_text(
        encoding="utf-8"
    )
    assert schema_contract_head("school-facts-f7-v49") == "20260924_49"
    assert 'down_revision = "20260924_48"' in migration


def test_f7_education_and_residence_quality_projections_require_current_schema():
    from pathlib import Path

    education = Path(
        "alembic/versions/20260924_50_f7_education_quality_coverage_basis.py"
    ).read_text(encoding="utf-8")
    residence = Path("alembic/versions/20260924_51_f7_residence_exact_coverage.py").read_text(
        encoding="utf-8"
    )
    assert schema_contract_head("school-facts-f7-v50") == "20260924_50"
    assert schema_contract_head("school-facts-f7-v51") == "20260924_51"
    assert 'down_revision = "20260924_49"' in education
    assert 'down_revision = "20260924_50"' in residence


def test_f7_reader_owner_repair_is_append_only():
    from pathlib import Path

    repair = Path("alembic/versions/20260924_52_f7_residence_reader_owner.py").read_text(
        encoding="utf-8"
    )
    assert schema_contract_head("school-facts-f7-v52") == "20260924_52"
    assert 'down_revision = "20260924_51"' in repair
    assert "campus_ai_context_definer" in repair


def test_f7_family_education_exact_quality_projection_is_append_only():
    from pathlib import Path

    migration = Path(
        "alembic/versions/20260924_53_f7_family_education_exact_coverage.py"
    ).read_text(encoding="utf-8")
    assert schema_contract_head("school-facts-f7-v53") == "20260924_53"
    assert 'down_revision = "20260924_52"' in migration
    current = Path("alembic/versions/20260924_54_f7_exact_read_contracts.py").read_text(
        encoding="utf-8"
    )
    assert schema_contract_head("school-facts-f7-v54") == "20260924_54"
    assert 'down_revision = "20260924_53"' in current
    assert "school_fact_f7_package_quality_kind" in current


def test_f7_family_profile_quality_has_new_head_without_rewriting_old_contract():
    from pathlib import Path

    assert schema_contract_head("school-facts-f7-v54") == "20260924_54"
    assert schema_contract_head("school-facts-f7-v55") == "20260924_55"
    migration = Path("alembic/versions/20260924_55_f7_family_profile_quality.py").read_text(
        encoding="utf-8"
    )
    assert 'down_revision = "20260924_54"' in migration


def test_f7_identity_lookup_selects_only_fixed_wrapper_and_legacy_keeps_its_path():
    from app.platform.identity.tuple_validator import SqlAlchemyIdentityTupleValidator

    class Result:
        def scalar_one_or_none(self):
            return 17

    class Session:
        def __init__(self):
            self.sql = ""

        def execute(self, statement, params):
            self.sql = str(statement)
            assert params == {
                "provider_code": "local_test",
                "provider_tenant_key": "synthetic",
                "external_subject": "subject",
            }
            return Result()

    args = dict(
        provider_code="local_test",
        provider_tenant_key="synthetic",
        external_subject="subject",
    )
    current = Session()
    assert (
        SqlAlchemyIdentityTupleValidator(school_fact_command_wrapper=True).lock_existing_user_id(
            current, **args
        )
        == 17
    )
    assert "app.school_fact_lock_existing_identity_user_id" in current.sql

    legacy = Session()
    assert SqlAlchemyIdentityTupleValidator().lock_existing_user_id(legacy, **args) == 17
    assert "app.lock_existing_identity_user_id" in legacy.sql
    assert "app.school_fact_lock_existing_identity_user_id" not in legacy.sql
