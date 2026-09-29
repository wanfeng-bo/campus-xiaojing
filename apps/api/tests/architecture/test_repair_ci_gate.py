from __future__ import annotations

import tomllib
from pathlib import Path

import yaml

API_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = API_ROOT.parents[1]
PYPROJECT = API_ROOT / "pyproject.toml"
WORKFLOW = PROJECT_ROOT / ".github" / "workflows" / "repair-gates.yml"
RETIRED_REPAIRS_SECURITY_TESTS = (
    "tests/repairs/test_schema_rls_acl.py::test_retired_repair_tables_have_force_rls_and_zero_runtime_acl",
    "tests/repairs/test_schema_rls_acl.py::test_bootstrap_twice_converges_retired_repair_runtime_acl_to_zero",
    "tests/repairs/test_schema_rls_acl.py::test_runtime_cannot_read_or_write_retired_repair_tables",
)


def _workflow() -> dict[str, object]:
    assert WORKFLOW.is_file(), "repair CI workflow is required"
    parsed = yaml.load(WORKFLOW.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    assert isinstance(parsed, dict)
    return parsed


def _steps(job: dict[str, object]) -> list[dict[str, object]]:
    steps = job.get("steps")
    assert isinstance(steps, list)
    assert all(isinstance(step, dict) for step in steps)
    return steps  # type: ignore[return-value]


def _step(steps: list[dict[str, object]], name: str) -> dict[str, object]:
    matches = [step for step in steps if step.get("name") == name]
    assert len(matches) == 1
    return matches[0]


def test_repair_http_boundaries_are_strict_and_registered_in_the_wheel() -> None:
    project = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    packages = set(project["tool"]["setuptools"]["packages"])
    strict_paths = set(project["tool"]["pyright"]["strict"])

    assert {
        "app.composition",
        "app.interfaces",
        "app.interfaces.http",
    } <= packages
    assert {"app/composition", "app/interfaces"} <= strict_paths


def test_repair_workflow_keeps_sf5b_security_gates_and_retires_legacy_product_suite() -> None:
    workflow = _workflow()
    assert workflow.get("on") == {"pull_request": "", "push": ""}
    jobs = workflow.get("jobs")
    assert isinstance(jobs, dict) and set(jobs) == {"repair-postgres"}
    job = jobs["repair-postgres"]
    assert isinstance(job, dict)
    assert "if" not in job and "continue-on-error" not in job

    services = job.get("services")
    assert isinstance(services, dict) and set(services) == {"postgres"}
    postgres = services["postgres"]
    assert isinstance(postgres, dict)
    assert postgres.get("image") == "postgres:16-alpine"

    environment = job.get("env")
    assert isinstance(environment, dict)
    assert environment.get("APP_ENV") == "test"
    assert str(environment.get("POSTGRES_DB", "")).endswith("_test")
    assert environment.get("POSTGRES_DB") == environment.get("POSTGRES_MIGRATION_DB")
    assert environment.get("RUN_POSTGRES_INTEGRATION") == "1"
    bootstrap_secret_environment = {
        "POSTGRES_SCHOOL_FACT_WRITER_PASSWORD": "campus-ai-ci-school-fact-writer-password",
        "POSTGRES_SCHOOL_FACT_READER_PASSWORD": "campus-ai-ci-school-fact-reader-password",
    }
    assert not bootstrap_secret_environment.keys() & environment.keys()

    steps = _steps(job)
    for step_name in (
        "Bootstrap isolated database roles",
        "Reconcile isolated database roles after migrations",
        "Re-bootstrap isolated database roles after role tests",
    ):
        assert _step(steps, step_name).get("env") == bootstrap_secret_environment
    names = [step.get("name") for step in steps]
    first_bootstrap = names.index("Bootstrap isolated database roles")
    migration = names.index("Apply Alembic migrations")
    assert (
        _step(steps, "Apply Alembic migrations")["run"] == "python -m alembic upgrade 20260824_20"
    )
    first_tests = names.index("Run role and RLS tests")
    rebootstrap = names.index("Reconcile isolated database roles after migrations")
    between_process_rebootstrap = names.index(
        "Re-bootstrap isolated database roles after role tests"
    )
    second_tests = names.index("Run retired Repairs boundary and identity tests")
    assert (
        first_bootstrap
        < migration
        < rebootstrap
        < first_tests
        < between_process_rebootstrap
        < second_tests
    )
    for step in steps:
        assert "if" not in step and "continue-on-error" not in step

    role_run = str(_step(steps, "Run role and RLS tests").get("run", ""))
    assert "tests/master_data/test_role_separation.py" in role_run
    assert "tests/master_data/test_rls.py" in role_run
    for security_test in RETIRED_REPAIRS_SECURITY_TESTS:
        assert security_test in role_run
    assert "python -m pytest tests/repairs " not in role_run
    assert "skipped" in role_run and "exit 1" in role_run

    retirement_run = str(
        _step(steps, "Run retired Repairs boundary and identity tests").get("run", "")
    )
    assert "tests/repairs" not in retirement_run
    assert "test_repair_presentation_postgres.py" not in retirement_run
    assert "tests/test_identity_execution_context_api.py" in retirement_run
    assert "tests/architecture/test_repairs_boundaries.py" in retirement_run
    assert "tests/architecture/test_repair_ci_gate.py" in retirement_run
    assert "skipped" in retirement_run and "exit 1" in retirement_run

    identity_step = _step(steps, "Verify disposable PostgreSQL system identifier")
    identity_run = str(identity_step.get("run", ""))
    assert "MASTER_DATA_DISPOSABLE_CLUSTER_SYSTEM_IDENTIFIER" in identity_run

    assert "Build wheel" in names
    assert "Verify wheel contains HTTP boundaries" not in names
    workflow_source = WORKFLOW.read_text(encoding="utf-8")
    assert "app/interfaces/http/repairs.py" not in workflow_source
    assert "app/composition/repairs.py" not in workflow_source

    assert names.index("Run frontend Vitest") < names.index("Build frontend")
    assert "${{ secrets." not in WORKFLOW.read_text(encoding="utf-8")
