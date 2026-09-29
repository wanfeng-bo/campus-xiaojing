from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MIGRATION = ROOT / "alembic/versions/20260913_26_school_fact_family.py"


def module():
    assert MIGRATION.is_file(), "F3 formal family migration is missing"
    spec = spec_from_file_location("f3_family_migration", MIGRATION)
    assert spec is not None and spec.loader is not None
    value = module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def test_f3_appends_after_25_and_formal_relationship_is_not_legacy_confirmation():
    mod = module()
    assert mod.revision == "20260913_26" and mod.down_revision == "20260906_25"
    sql = mod.FAMILY_AUTHORITY_SQL
    for required in (
        "authority_version = 1",
        "status = 'confirmed'",
        "revoked_at IS NULL",
        "verified_by_person_id IS NOT NULL",
        "verified_by_assignment_id IS NOT NULL",
        "guardian_student_profile",
        "caregiver_student_summary",
        "student",
    ):
        assert required in sql


def test_f3_reader_sql_keeps_explicit_role_and_both_current_and_factual_time():
    sql = module().FAMILY_READER_SQL
    assert "SESSION_USER" in sql and "campus_ai_school_fact_reader" in sql
    assert "app.require_school_database_anchor()" in sql
    assert "statement_timestamp()" in sql and "p_factual_at" in sql
    assert "school_fact_family_authority" in sql
    assert "28000" in sql
    assert "SELECT *" not in sql.upper()
    assert "verification_reference" not in sql and "person_contact_points" not in sql


def test_f3_identity_reuses_original_signed_algorithm_and_revokes_bypass():
    sql = module().FAMILY_IDENTITY_SQL
    assert "list_execution_context_options_pre_f3" in sql
    assert "bind_execution_context_pre_f3" in sql
    assert "family_relationship_is_authoritative" in sql
    assert "REVOKE ALL" in sql and "campus_ai_runtime" in sql


def test_f3_migration_renders_all_statements_without_a_database():
    from io import StringIO

    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    output = StringIO()
    context = MigrationContext.configure(
        dialect_name="postgresql", opts={"as_sql": True, "output_buffer": output}
    )
    with Operations.context(context):
        module().upgrade()
    sql = output.getvalue()
    assert "CREATE TABLE person_regions" in sql
    assert "authority_version INTEGER" in sql
    assert "FORCE ROW LEVEL SECURITY" in sql
    assert "REVOKE CREATE ON SCHEMA app,app_private" in sql


def test_family_projection_preserves_conflict_and_human_source_evidence():
    mod = module()
    sql = mod.FAMILY_READER_SQL
    assert "LIMIT 1" not in sql and "c_count>1" in sql
    assert "no_current_records" in sql
    assert "source_name" in sql and "school_fact_sources" in sql
    assert "campus_version" in sql and "l.fact_kind='location'" in sql
    assert "LEFT JOIN LATERAL" in mod.FAMILY_IDENTITY_SQL


def test_staff_target_is_fixed_eight_kind_projection_not_dynamic_sql():
    sql = module().FAMILY_STAFF_SQL
    assert "school_fact_f3_staff_target" in sql and "SESSION_USER" in sql
    assert "EXECUTE" not in sql.split("REVOKE")[0]
    for kind in (
        "student",
        "student_school_relationship",
        "student_contact",
        "guardian_relationship",
        "person_region",
        "person",
        "login_mapping",
        "assignment",
    ):
        assert "p_kind='" + kind + "'" in sql
    assert "p_creation" in sql and "fact_source_key=p_source" in sql
    assert "expected_version" in sql and "school_fact_version" in sql


def test_f3_acl_is_fixed_in_the_append_only_migration_without_global_reconcile():
    sql = MIGRATION.read_text(encoding="utf-8")
    assert "pg_attribute" not in sql and "pg_class" not in sql
    assert 'for table in ("guardian_relationships", "person_regions")' in sql
    assert "school_fact_f3_scope_is_current" in sql
    assert "REVOKE CREATE ON SCHEMA app,app_private" in sql
    role_script = (ROOT.parents[1] / "infra/postgres/001-campus-ai-roles.sh").read_text(
        encoding="utf-8"
    )
    assert "$f3_family_capabilities$" not in role_script


def test_staff_temporal_and_source_metadata_are_real_not_query_time():
    sql = module().FAMILY_STAFF_SQL
    assert "'valid_from',chosen.valid_from" in sql
    assert "'valid_to',ends_at" in sql
    assert "'source_published_at',chosen.published_at" in sql
    assert "'source_trace_reference',chosen.source_trace_reference" in sql


def test_pg_test_connection_failure_has_no_credential_or_exception_chain(tmp_path, monkeypatch):
    import json
    import traceback

    import psycopg
    import pytest

    from tests.school_facts.test_f3_family_postgres import safe_test_engine

    sentinel = "synthetic-credential-never-used-for-a-real-connection"
    path = tmp_path / "fake-config.json"
    path.write_text(
        json.dumps({"database": "f3_fake_test", "migration": sentinel}), encoding="utf-8"
    )

    def fail_connect(*args, **kwargs):
        raise RuntimeError("simulated driver failure " + sentinel)

    monkeypatch.setattr(psycopg, "connect", fail_connect)
    engine = safe_test_engine(path, "migration")
    assert engine.url.password is None and sentinel not in repr(engine.url)
    with pytest.raises(RuntimeError, match="F3 test database connection failed") as caught:
        engine.connect()
    error = caught.value
    assert error.__cause__ is None and error.__context__ is None
    assert sentinel not in "".join(traceback.format_exception(error))


def test_staff_empty_conflict_and_missing_published_content_remain_distinct():
    sql = module().FAMILY_STAFF_SQL
    assert "IF latest IS NULL THEN" in sql and "'status','no_current_records'" in sql
    assert "'status','conflict'" in sql
    assert "F3 published content is unavailable" in sql and "ERRCODE='55000'" in sql


def test_historical_student_and_family_status_do_not_use_current_terminal_values():
    sql = module().FAMILY_STAFF_SQL
    student = sql.split("IF p_kind='student' THEN", 1)[1].split("ELSIF p_kind=", 1)[0]
    assert "f.version=chosen.target_version" not in student
    assert "THEN 'active'" in student
    relation = sql.split("ELSIF p_kind='guardian_relationship' THEN", 1)[1].split(
        "ELSIF p_kind=", 1
    )[0]
    assert "WHEN f.verified_at IS NULL THEN 'pending_verification'" in relation
    assert "ELSE 'confirmed'" in relation
    assert "f.revoked_at IS NULL" not in relation


def test_technical_source_reference_is_not_labeled_as_natural_source_name():
    mod = module()
    assert "'source_name',NULL" in mod.FAMILY_READER_SQL
    assert "'source_trace_reference',q.source_trace_reference" in mod.FAMILY_READER_SQL
    assert "'source_name',NULL" in mod.FAMILY_STAFF_SQL
    assert "AS source_name" not in mod.FAMILY_READER_SQL


def test_guardian_empty_profile_includes_region_and_parent_basis_is_checked_now():
    sql = module().FAMILY_READER_SQL
    assert "WHEN n_count=0 AND e_count=0 AND r_count=0" in sql
    assert "FROM app.school_fact_family_authority" in sql
    assert "at_time<basis.valid_from" in sql and "at_time>statement_timestamp()" in sql


def test_staff_scope_checks_formal_coordinates_without_business_time_availability():
    sql = module().FAMILY_STAFF_SQL.split("CREATE FUNCTION app.school_fact_f3_scope_is_current", 1)[
        1
    ]
    assert "app.school_fact_f3_staff_target(" not in sql
    assert "'school',p_school,p_as_of" in sql
    assert "b.status='applied'" in sql and "l.fact_source_key=object_key" in sql
    assert "content_version" in sql and "coordinate_count" in sql
    assert "IF coordinate_count<>1 THEN RETURN false" in sql
    assert "f.status=" not in sql and "f.revoked_at IS NULL" not in sql


def test_migration_and_model_preserve_unverified_terminal_shape_without_fake_verifier():
    import re
    from io import StringIO

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import CheckConstraint, Table

    from app.domains.students.models import GuardianRelationship

    assert isinstance(GuardianRelationship.__table__, Table)
    shape = next(
        c
        for c in GuardianRelationship.__table__.constraints
        if c.name == "ck_guardian_relationships_formal_shape"
    )
    output = StringIO()
    context = MigrationContext.configure(
        dialect_name="postgresql", opts={"as_sql": True, "output_buffer": output}
    )
    with Operations.context(context):
        module().upgrade()
    assert isinstance(shape, CheckConstraint)
    expected = re.sub(r"\s+", "", str(shape.sqltext))
    assert expected in re.sub(r"\s+", "", output.getvalue())


def test_staff_history_selects_effective_time_before_correction_version():
    sql = module().FAMILY_STAFF_SQL.split("CREATE FUNCTION app.school_fact_f3_scope_is_current", 1)[
        0
    ]
    assert "INTO latest_at" in sql
    assert "(u.payload->>'effective_at')::timestamptz=latest_at" in sql
    assert sql.index("INTO latest_at") < sql.index("INTO latest FROM")


def test_staff_candidates_are_fixed_authorized_source_and_field_projection():
    sql = module().FAMILY_STAFF_SQL
    start = sql.index("CREATE FUNCTION app.school_fact_f3_staff_candidates")
    end = sql.index("CREATE FUNCTION app.school_fact_f3_scope_is_current", start)
    candidates = sql[start:end]
    assert "p_sources text[]" in candidates
    assert "cardinality(p_sources)>100" in candidates
    assert "FOREACH candidate IN ARRAY p_sources" in candidates
    assert "p_fields text[]" in candidates
    assert "p_factual_at,p_fields" in candidates
    assert "payload:=target" in candidates
    assert "p_limit>21" in candidates
    assert "RETURNS TABLE(source_key text,label text,payload jsonb)" in candidates
    assert "display_name" in candidates
    assert "person_source_key" in candidates
    assert "GRANT EXECUTE" in candidates
    assert "campus_ai_school_fact_reader" in candidates
    assert "EXECUTE format" not in candidates


def test_person_region_uses_school_id_rls_not_the_school_root_id_trigger():
    sql = MIGRATION.read_text(encoding="utf-8")
    assert "FOR EACH ROW EXECUTE FUNCTION app.enforce_school_database_anchor()" not in sql
    assert "CREATE POLICY f3_family_anchor ON public.person_regions AS RESTRICTIVE" in sql
    assert "school_id=app.require_school_database_anchor()" in sql
    assert "FORCE ROW LEVEL SECURITY" in sql


def test_staff_definer_can_call_only_the_existing_reader_version_function():
    sql = MIGRATION.read_text(encoding="utf-8")
    expected = (
        "GRANT EXECUTE ON FUNCTION app.school_fact_current_version(uuid) "
        "TO campus_ai_context_definer"
    )
    assert expected in sql.replace('"\n        "', "")
    assert "school_fact_current_version(uuid) TO campus_ai_runtime" not in sql
    assert "school_fact_current_version(uuid) TO campus_ai_school_fact_writer" not in sql
