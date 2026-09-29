from __future__ import annotations

import ast
from pathlib import Path

API_ROOT = Path(__file__).resolve().parents[2]
MIGRATION_PATH = API_ROOT / "alembic/versions/20260824_20_school_database_boundary.py"
PERSISTENCE_PATH = API_ROOT / "app/composition/school_fact_persistence.py"
PROJECT_ROOT = API_ROOT.parents[1]
ROLE_BOOTSTRAP_PATH = PROJECT_ROOT / "infra/postgres/001-campus-ai-roles.sh"
COMPATIBILITY_ADAPTER_PATH = API_ROOT / "app/platform/school_facts/compatibility_repository.py"


def _source() -> str:
    assert MIGRATION_PATH.is_file(), MIGRATION_PATH
    return MIGRATION_PATH.read_text(encoding="utf-8")


def test_revision20_declares_anchor_identity_rls_and_irreversible_security_boundary() -> None:
    source = _source()

    assert "school_database_anchor" in source
    assert "anchor_id" in source
    assert "app.assert_school_database_anchor" in source
    assert "app.provision_school_database_anchor" in source
    assert "app.lock_existing_identity_user_id" in source
    assert "app.claim_legacy_identity_user_id" in source
    assert "ENABLE ROW LEVEL SECURITY" in source
    assert "FORCE ROW LEVEL SECURITY" in source
    assert "campus_ai_school_fact_writer" in source
    assert "campus_ai_school_fact_reader" in source
    assert "campus_ai_identity_definer" in source
    assert "irreversible security boundary" in source
    assert "GRANT USAGE ON SCHEMA app TO" in source
    assert "SET LOCAL ROLE campus_ai_context_definer" in source
    assert "second school root cannot precede controlled provisioning" in source
    assert source.count("sf5b.school-root-anchor:v1") >= 2


def test_revision20_declares_writer_only_school_reactivation_preflight_and_prepare() -> None:
    source = _source()
    persistence = PERSISTENCE_PATH.read_text(encoding="utf-8")
    role_bootstrap = ROLE_BOOTSTRAP_PATH.read_text(encoding="utf-8")

    assert "CREATE FUNCTION app.school_fact_school_reactivation_preflight(" in source
    assert "CREATE FUNCTION app.school_fact_school_reactivation_prepare(" in source
    assert source.count("SESSION_USER IS DISTINCT FROM 'campus_ai_school_fact_writer'") >= 2
    assert source.count("SECURITY DEFINER") >= 2
    assert "sf5b_school_reactivation_prepare_insert" in source
    assert "TO campus_ai_context_definer" in source
    assert "school_fact_school_reactivation_preflight" in role_bootstrap
    assert "school_fact_school_reactivation_prepare" in role_bootstrap
    assert "terminal_lineage.operation = 'revoke'" in source
    assert '!= "revoke"' in persistence
    assert "学校只能在正式撤销后重新启用。" in persistence

    forbidden = (
        "school_fact_change_lineage",
        "audit_logs",
        "command_idempotency_records",
        "master_data_import_command_outcomes",
    )
    prepare_start = source.index("CREATE FUNCTION app.school_fact_school_reactivation_prepare(")
    prepare_end = source.index("$function$;", prepare_start)
    prepare_body = source[prepare_start:prepare_end]
    assert "INSERT INTO public.master_data_import_batches" in prepare_body
    assert "INSERT INTO public.master_data_import_units" in prepare_body
    assert "set_config(" not in prepare_body
    assert "UPDATE public.schools" not in prepare_body
    for relation_name in forbidden:
        assert relation_name not in prepare_body


def test_revision20_uses_explicit_table_sets_and_never_restores_runtime_fact_dml() -> None:
    source = _source()
    module = ast.parse(source)

    assignments = {
        target.id: node.value
        for node in module.body
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance((target := node.targets[0]), ast.Name)
    }
    writer_tables = ast.literal_eval(assignments["_SCHOOL_FACT_WRITER_TABLES"])
    app_access_tables = ast.literal_eval(assignments["_SF5B_APP_ACCESS_TABLES"])
    runtime_revoke_expression = assignments["_SCHOOL_FACT_RUNTIME_DML_REVOKE_TABLES"]
    assert isinstance(runtime_revoke_expression, ast.BinOp)
    assert isinstance(runtime_revoke_expression.left, ast.Name)
    assert runtime_revoke_expression.left.id == "_SCHOOL_FACT_WRITER_TABLES"
    runtime_dml_revoke_tables = writer_tables + ast.literal_eval(runtime_revoke_expression.right)
    anchor_guard_only_tables = ast.literal_eval(assignments["_ANCHOR_GUARD_ONLY_TABLES"])
    tenant_tables = app_access_tables + anchor_guard_only_tables

    assert len(writer_tables) == len(set(writer_tables))
    assert len(tenant_tables) == len(set(tenant_tables))
    assert set(writer_tables) <= set(tenant_tables)
    assert set(writer_tables) <= set(runtime_dml_revoke_tables)
    assert "master_data_import_takeovers" in runtime_dml_revoke_tables
    assert "governance_grants" in tenant_tables
    assert "school_policy_versions" in writer_tables
    assert "GRANT INSERT, UPDATE, DELETE, TRUNCATE" not in source
    assert "master_data_import_takeovers" not in writer_tables
    assert "BEFORE TRUNCATE ON public.school_database_anchor" in source
    assert "USING (true) WITH CHECK (true)" not in source
    assert "sf5b_owner_migration_access" in source
    assert "sf5b_writer_context_access" in source
    assert "sf5b_reader_anchor_access" in source
    assert "app.require_school_context()" in source
    assert "app.require_school_database_anchor()" in source
    assert "REVOKE ALL ON ALL TABLES IN SCHEMA public" in source
    assert "REVOKE ALL ON ALL SEQUENCES IN SCHEMA public" in source
    assert "CREATE FUNCTION app.purge_expired_school_fact_preparations()" in source
    assert "sf5b_expired_preparation_cleanup_read" in source
    assert "sf5b_expired_preparation_cleanup_delete" in source
    assert "pg_catalog.statement_timestamp()" in source
    assert 'f"GRANT DELETE ON TABLE public.{table_name} TO campus_ai_context_definer"' in source
    assert (
        'f"GRANT DELETE ON TABLE public.{table_name} TO campus_ai_school_fact_writer"' not in source
    )
    assert "GRANT TRUNCATE" not in source
    assert "GRANT SELECT, INSERT, UPDATE ON TABLE public.schools TO campus_ai_runtime" not in source
    assert "GRANT SELECT, INSERT ON TABLE public.school_fact_change_lineage" in source


def test_revision20_closes_r1_context_policies_and_anchors_audit_returning() -> None:
    source = _source()
    module = ast.parse(source)
    assignments = {
        target.id: node.value
        for node in module.body
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance((target := node.targets[0]), ast.Name)
    }

    r1_reader_tables = ast.literal_eval(assignments["_R1_READER_TABLES"])
    rev18_slice_tables = ast.literal_eval(assignments["_REV18_CONTENT_SLICE_POLICY_TABLES"])
    legacy_drop_expression = assignments["_LEGACY_CONTEXT_POLICY_DROP_TABLES"]
    assert isinstance(legacy_drop_expression, ast.Call)
    assert isinstance(legacy_drop_expression.func, ast.Name)
    assert legacy_drop_expression.func.id == "frozenset"
    assert len(legacy_drop_expression.args) == 1
    combined_tables = legacy_drop_expression.args[0]
    assert isinstance(combined_tables, ast.BinOp)
    assert isinstance(combined_tables.left, ast.BinOp)
    assert isinstance(combined_tables.left.left, ast.Name)
    assert combined_tables.left.left.id == "_R1_READER_TABLES"
    assert isinstance(combined_tables.left.right, ast.Name)
    assert combined_tables.left.right.id == "_REV18_CONTENT_SLICE_POLICY_TABLES"
    context_root_tables = ast.literal_eval(combined_tables.right)
    assert context_root_tables == ("school_versions",)
    assert "school_policy_versions" not in context_root_tables
    assert set(rev18_slice_tables) == {
        "staff_assignment_versions",
        "account_person_binding_versions",
    }
    assert {
        "school_people",
        "organization_units",
        "organization_unit_versions",
        "staff_assignments",
        "campus_locations",
        "campus_location_versions",
    } <= set(r1_reader_tables)
    assert "school_policy_versions" not in r1_reader_tables
    assert "DROP POLICY IF EXISTS {legacy_policy_name}" in source
    assert "sf5b_audit_definer_append" in source
    assert "FOR INSERT TO campus_ai_audit_definer" in source
    assert (
        "WITH CHECK (school_id IS NULL OR "
        "school_id = app.require_school_database_anchor())" in source
    )
    assert "sf5b_audit_definer_returning" in source
    assert "AS PERMISSIVE FOR SELECT TO campus_ai_audit_definer" in source
    assert "USING (school_id IS NULL OR school_id = app.require_school_database_anchor())" in source


def test_revision20_closes_legacy_runtime_takeover_writer_entrypoints() -> None:
    source = _source()
    module = ast.parse(source)
    assignments = {
        target.id: node.value
        for node in module.body
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance((target := node.targets[0]), ast.Name)
    }
    revoked_functions = ast.literal_eval(assignments["_REVOKED_RUNTIME_WRITER_FUNCTIONS"])
    role_bootstrap = ROLE_BOOTSTRAP_PATH.read_text(encoding="utf-8")

    signature = "app.supersede_master_data_import_takeover_source(uuid,uuid,uuid,text,bigint)"
    assert signature in revoked_functions
    assert "SET LOCAL ROLE campus_ai_takeover_definer" in source
    runtime_allowlist = role_bootstrap[
        role_bootstrap.rfind(
            "WITH expected(function_name, argument_types)",
            0,
            role_bootstrap.index("-- Revision 18 adds narrow School Facts"),
        ) : role_bootstrap.index("-- Revision 18 adds narrow School Facts")
    ]
    assert "supersede_master_data_import_takeover_source" not in runtime_allowlist
    assert "sf5b_runtime_writer_close" in role_bootstrap
    assert signature in role_bootstrap
    assert "REVOKE ALL PRIVILEGES ON TABLE public.master_data_import_takeovers" in role_bootstrap


def test_identity_advisory_material_is_byte_length_prefixed_and_versioned() -> None:
    source = _source()

    assert "v1|%s:%s|%s:%s|%s:%s" in source
    assert source.count("octet_length") >= 3
    assert "pg_advisory_xact_lock" in source
    assert "hashtextextended" in source


def test_sf5b_has_no_legacy_repairs_school_fact_compatibility_boundary() -> None:
    production_sources = {
        "revision20": _source(),
        "role_bootstrap": ROLE_BOOTSTRAP_PATH.read_text(encoding="utf-8"),
        "connection": (API_ROOT / "app/db/connection.py").read_text(encoding="utf-8"),
        "responsibility": (API_ROOT / "app/platform/responsibility/repository.py").read_text(
            encoding="utf-8"
        ),
    }
    forbidden_symbols = {
        "campus_ai_school_fact_compat_definer",
        "SqlAlchemySchoolFactCompatibilityAdapter",
        "school_fact_current_assignment_identity_relation",
        "lock_school_fact_assignment",
        "school_fact_current_assignment_snapshot",
        "school_fact_current_assignment_identity",
        "school_fact_person_name_snapshot",
        "school_fact_find_exact_locations",
        "school_fact_location_is_current",
    }

    assert not COMPATIBILITY_ADAPTER_PATH.exists()
    for source_name, source in production_sources.items():
        found = sorted(symbol for symbol in forbidden_symbols if symbol in source)
        assert found == [], f"{source_name} retains legacy Repairs compatibility: {found}"

    revision20 = production_sources["revision20"]
    role_bootstrap = production_sources["role_bootstrap"]
    assert "_RETIRED_REPAIRS_RUNTIME_ACCESS_TABLES" in revision20
    assert "GRANT SELECT, UPDATE (id) ON TABLE public.repair_scope_bindings" not in role_bootstrap
    assert (
        "GRANT SELECT, INSERT ON TABLE public.repair_tickets TO campus_ai_runtime"
        not in role_bootstrap
    )
