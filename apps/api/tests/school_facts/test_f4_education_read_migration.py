from importlib.util import module_from_spec, spec_from_file_location
from io import StringIO
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MIGRATION = ROOT / "alembic/versions/20260913_29_school_education_read.py"


def migration():
    assert MIGRATION.is_file(), "F4 official education Reader migration is missing"
    spec = spec_from_file_location("f4_education_read", MIGRATION)
    mod = module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_ssr_timeline_is_a_separate_f4_bounded_projection():
    sql = migration().SSR_TIMELINE_SQL
    assert "school_fact_f4_ssr_timeline" in sql
    assert "superseded_at IS NULL" in sql and "1000" in sql
    assert "p_fields <@" in sql and "SESSION_USER" in sql
    assert "school_fact_f3_staff_target(" not in sql


def test_migration29_follows28_and_renders_finite_security_definers():
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    mod = migration()
    assert mod.revision == "20260913_29" and mod.down_revision == "20260913_28"
    output = StringIO()
    with Operations.context(
        MigrationContext.configure(
            dialect_name="postgresql", opts={"as_sql": True, "output_buffer": output}
        )
    ):
        mod.upgrade()
    sql = output.getvalue()
    assert "school_fact_f4_scope_is_current" in sql
    assert "school_fact_f4_targets" in sql and "school_fact_family_education" in sql
    assert "REVOKE CREATE ON SCHEMA app,app_private" in sql
    assert (
        "GRANT SELECT" not in sql
        or "campus_ai_school_fact_reader" not in sql.split("GRANT SELECT")[1].split(";")[0]
    )


def test_staff_batches_validate_all_keys_fields_before_projecting():
    sql = migration().STAFF_SQL
    assert "cardinality(p_objects)>100" in sql
    assert "p_fields <@" in sql
    assert "SESSION_USER" in sql and "app.require_school_database_anchor()" in sql
    assert "p_known_at>statement_timestamp()" in sql
    assert "FOREACH" not in sql
    assert "superseded_at" in sql and "not_recorded" in sql and "cancelled" in sql


def test_guardian_uses_real_current_proof_and_never_returns_other_child_positions():
    sql = migration().FAMILY_SQL
    assert "family_relationship_is_authoritative" in sql
    assert "verified_guardian" in sql
    assert "r.student_id=p_student" in sql and "f4_child_keys(p_school,basis.student_id" in sql
    assert "visibility_scope='public'" in sql
    assert "person_contact_points" not in sql
    assert "contact_at" in sql
    assert "CREATE OR REPLACE FUNCTION app.school_fact_family_profile" not in sql


def test_correction_preserves_old_contract_and_compares_exact_education_revision():
    sql = migration().CORRECTION_SQL
    assert "school_fact_family_correction_targets_pre_f4" in sql
    assert "school_fact_f3_field_changed_pre_f4" in sql
    assert "p_version" in sql and "p_source" in sql
    assert "applied" in sql


def test_filter_is_bounded_exact_reference_filter_before_projection():
    sql = migration().STAFF_SQL
    assert "p_filter jsonb" in sql
    assert "jsonb_typeof" in sql and "p_filter" in sql
    assert "assignment_source_key" in sql
    assert "p_fields" in sql and "invalid education filter" in sql


def test_creation_coordinates_never_read_payload_or_versions():
    sql = migration().COORDINATES_SQL
    assert "school_fact_f4_coordinates" in sql
    assert "school_education_versions" not in sql and "payload" not in sql
    assert "cardinality(p_objects)>100" in sql


def test_causal_prior_revision_is_staff_only_and_still_field_projected():
    mod = migration()
    assert "prior_segments" in mod.STAFF_SQL
    assert "cause_source_key" in mod.STAFF_SQL
    assert "r - 'prior_segments'" in mod.FAMILY_SQL
    assert "s - 'cause_source_key' - 'cause_version'" in mod.FAMILY_SQL
