from __future__ import annotations

import tomllib
from collections.abc import Iterator
from copy import deepcopy
from pathlib import Path
from shlex import split as shlex_split

import pytest
import yaml

API_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = API_ROOT.parents[1]
WORKFLOW = PROJECT_ROOT / ".github" / "workflows" / "master-data-gates.yml"
CONFTST = API_ROOT / "tests" / "master_data" / "conftest.py"
PYPROJECT = API_ROOT / "pyproject.toml"
EXPECTED_STEP_NAMES = [
    "Checkout",
    "Set up Python",
    "Install PostgreSQL 16 client",
    "Install API development dependencies",
    "Verify disposable PostgreSQL system identifier",
    "Bootstrap isolated database roles",
    "Apply Alembic migrations",
    "Reconcile isolated database roles after migrations",
    "Run master-data and architecture tests",
    "Upload sanitized capacity report",
    "Run Ruff lint",
    "Run Ruff formatting check",
    "Run Pyright",
    "Build wheel",
]
EXPECTED_RUN_TOKENS = {
    "Install PostgreSQL 16 client": [
        ["sudo", "apt-get", "update"],
        ["sudo", "apt-get", "install", "--yes", "postgresql-client-16"],
        ["psql", "--version", "|", "grep", "-F", "PostgreSQL) 16"],
    ],
    "Install API development dependencies": [["python", "-m", "pip", "install", "-e", ".[dev]"]],
    "Verify disposable PostgreSQL system identifier": [
        [
            "system_identifier=$(PGUSER=$POSTGRES_BOOTSTRAP_USER "
            "PGPASSWORD=$POSTGRES_BOOTSTRAP_PASSWORD psql -X -Atqc "
            "'SELECT system_identifier FROM pg_control_system()')"
        ],
        ["test", "-n", "$system_identifier"],
        [
            "echo",
            "MASTER_DATA_DISPOSABLE_CLUSTER_SYSTEM_IDENTIFIER=$system_identifier",
            ">>",
            "$GITHUB_ENV",
        ],
        [
            "echo",
            "MASTER_DATA_POSTGRES_BIN=$(dirname $(command",
            "-v",
            "psql))",
            ">>",
            "$GITHUB_ENV",
        ],
    ],
    "Bootstrap isolated database roles": [
        [
            "POSTGRES_EXPECTED_SYSTEM_IDENTIFIER=$MASTER_DATA_DISPOSABLE_CLUSTER_SYSTEM_IDENTIFIER",
            "sh",
            "infra/postgres/001-campus-ai-roles.sh",
        ]
    ],
    "Apply Alembic migrations": [["python", "-m", "alembic", "upgrade", "20260824_20"]],
    "Reconcile isolated database roles after migrations": [
        [
            "POSTGRES_EXPECTED_SYSTEM_IDENTIFIER=$MASTER_DATA_DISPOSABLE_CLUSTER_SYSTEM_IDENTIFIER",
            "sh",
            "infra/postgres/001-campus-ai-roles.sh",
        ]
    ],
    "Run master-data and architecture tests": [
        ["set", "-o", "pipefail"],
        [
            "python",
            "-m",
            "pytest",
            "tests/master_data",
            "tests/school_facts/test_sf5b_writer_executor_postgres.py",
            "tests/test_identity_legacy_claim_postgres.py",
            "tests/test_secure_reader_postgres.py",
            "tests/architecture",
            "-q",
            "-rs",
            "|",
            "tee",
            "master-data-pytest.txt",
        ],
        ["if", "grep", "-Eq", "[0-9]+ skipped", "master-data-pytest.txt;", "then"],
        ["echo", "master_data/architecture tests must not skip in CI", ">&2"],
        ["exit", "1"],
        ["fi"],
    ],
    "Run Ruff lint": [["python", "-m", "ruff", "check", "app", "tests"]],
    "Run Ruff formatting check": [["python", "-m", "ruff", "format", "--check", "app", "tests"]],
    "Run Pyright": [["pyright"]],
    "Build wheel": [
        ["python", "-m", "build", "--wheel", "--outdir", "$RUNNER_TEMP/campus-ai-master-wheel"]
    ],
}


def _workflow() -> dict[str, object]:
    assert WORKFLOW.is_file(), "master-data CI workflow is required"
    parsed = yaml.load(WORKFLOW.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    assert isinstance(parsed, dict), "workflow must parse into a YAML mapping"
    return parsed


def _mappings(value: object) -> Iterator[dict[str, object]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _mappings(child)
    elif isinstance(value, list):
        for child in value:
            yield from _mappings(child)


def _steps(job: dict[str, object]) -> list[dict[str, object]]:
    steps = job.get("steps")
    assert isinstance(steps, list), "master-data job must contain ordered steps"
    assert all(isinstance(step, dict) for step in steps), "steps must be mappings"
    return steps  # type: ignore[return-value]


def _step_index(steps: list[dict[str, object]], name: str) -> int:
    matches = [index for index, step in enumerate(steps) if step.get("name") == name]
    assert len(matches) == 1, f"required step must appear exactly once: {name}"
    return matches[0]


def _run(steps: list[dict[str, object]], name: str) -> str:
    step = steps[_step_index(steps, name)]
    value = step.get("run")
    assert isinstance(value, str), f"{name} must execute a command"
    return value


def _active_run_tokens(run: str) -> list[list[str]]:
    return [tokens for line in run.splitlines() if (tokens := shlex_split(line, comments=True))]


def _assert_setup_actions(steps: list[dict[str, object]]) -> None:
    checkout = steps[_step_index(steps, "Checkout")]
    setup = steps[_step_index(steps, "Set up Python")]
    assert checkout.get("uses") == "actions/checkout@v4"
    assert setup.get("uses") == "actions/setup-python@v5"
    assert "run" not in checkout and "run" not in setup
    setup_with = setup.get("with")
    assert isinstance(setup_with, dict) and setup_with.get("python-version") == "3.13"


def _assert_capacity_artifact(steps: list[dict[str, object]]) -> None:
    upload = steps[_step_index(steps, "Upload sanitized capacity report")]
    assert upload.get("uses") == "actions/upload-artifact@v4"
    assert "run" not in upload
    upload_with = upload.get("with")
    assert upload_with == {
        "name": "master-data-capacity-report",
        "path": "${{ runner.temp }}/master-data-capacity.json",
        "if-no-files-found": "error",
        "retention-days": "14",
    }


def _assert_capacity_report_context(job: dict[str, object], steps: list[dict[str, object]]) -> None:
    environment = job.get("env")
    assert isinstance(environment, dict)
    assert "MASTER_DATA_CAPACITY_REPORT_PATH" not in environment
    test_step = steps[_step_index(steps, "Run master-data and architecture tests")]
    assert test_step.get("env") == {
        "MASTER_DATA_CAPACITY_REPORT_PATH": "${{ runner.temp }}/master-data-capacity.json"
    }


def _assert_required_run_semantics(steps: list[dict[str, object]]) -> None:
    assert [step.get("name") for step in steps] == EXPECTED_STEP_NAMES
    for name, expected_tokens in EXPECTED_RUN_TOKENS.items():
        assert _active_run_tokens(_run(steps, name)) == expected_tokens


def _assert_triggers(workflow: dict[str, object]) -> None:
    assert workflow.get("on") == {"pull_request": "", "push": ""}


def _assert_reachable(job: dict[str, object], steps: list[dict[str, object]]) -> None:
    assert "if" not in job, "the required gate job must not be conditionally skipped"
    for step in steps:
        assert "if" not in step, "the required gate must not conditionally skip a step"


def test_master_data_workflow_runs_the_complete_postgres_quality_gate() -> None:
    workflow = _workflow()
    _assert_triggers(workflow)
    jobs = workflow.get("jobs")
    assert isinstance(jobs, dict)
    assert set(jobs) == {"master-data-postgres"}, "only the reachable gate job is accepted"
    job = jobs["master-data-postgres"]
    assert isinstance(job, dict)
    services = job.get("services")
    assert isinstance(services, dict) and set(services) == {"postgres"}
    postgres = services["postgres"]
    assert isinstance(postgres, dict)
    assert postgres.get("image") == "postgres:16-alpine"
    assert "pg_isready" in str(postgres.get("options"))

    environment = job.get("env")
    assert isinstance(environment, dict)
    assert environment["POSTGRES_HOST"] == "127.0.0.1"
    assert environment["POSTGRES_PORT"] == "5432"
    assert environment["POSTGRES_DATABASE"] == "campus_ai_master_test"
    assert environment["PGHOST"] == environment["POSTGRES_HOST"]
    assert environment["PGPORT"] == environment["POSTGRES_PORT"]
    assert environment["PGDATABASE"] == environment["POSTGRES_DATABASE"]
    assert (
        environment["RLS_CONTEXT_SIGNING_KEY"]
        == environment["POSTGRES_MIGRATION_CONTEXT_SIGNING_KEY"]
    )
    assert (
        environment["RLS_CONTEXT_SIGNING_KEY"] == "master-data-schema-context-signing-key-32-bytes"
    )
    assert environment["RUN_POSTGRES_INTEGRATION"] == "1"
    assert environment["RUN_MASTER_DATA_CAPACITY"] == "1"
    for required in (
        "POSTGRES_BOOTSTRAP_USER",
        "POSTGRES_RUNTIME_PASSWORD",
        "POSTGRES_MIGRATION_PASSWORD",
    ):
        assert required in environment

    bootstrap_secret_environment = {
        "POSTGRES_SCHOOL_FACT_WRITER_PASSWORD": "campus-ai-ci-school-fact-writer-password",
        "POSTGRES_SCHOOL_FACT_READER_PASSWORD": "campus-ai-ci-school-fact-reader-password",
    }
    assert not bootstrap_secret_environment.keys() & environment.keys()

    steps = _steps(job)
    for step_name in (
        "Bootstrap isolated database roles",
        "Reconcile isolated database roles after migrations",
    ):
        step = steps[_step_index(steps, step_name)]
        assert step.get("env") == bootstrap_secret_environment
    _assert_reachable(job, steps)
    _assert_setup_actions(steps)
    _assert_capacity_artifact(steps)
    _assert_capacity_report_context(job, steps)
    _assert_required_run_semantics(steps)
    assert not any("continue-on-error" in mapping for mapping in _mappings(workflow))


def test_master_data_workflow_declares_yaml_parser_for_the_static_gate() -> None:
    development_dependencies = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"][
        "optional-dependencies"
    ]
    assert any(dependency.startswith("PyYAML>=6") for dependency in development_dependencies["dev"])


def test_job_environment_cannot_use_runner_context_before_runner_assignment() -> None:
    workflow = deepcopy(_workflow())
    jobs = workflow["jobs"]
    assert isinstance(jobs, dict)
    job = jobs["master-data-postgres"]
    assert isinstance(job, dict)
    steps = _steps(job)
    environment = job.get("env")
    assert isinstance(environment, dict)
    environment["MASTER_DATA_CAPACITY_REPORT_PATH"] = "${{ runner.temp }}/master-data-capacity.json"
    with pytest.raises(AssertionError):
        _assert_capacity_report_context(job, steps)


def test_capacity_report_path_is_required_on_the_test_step() -> None:
    workflow = deepcopy(_workflow())
    jobs = workflow["jobs"]
    assert isinstance(jobs, dict)
    job = jobs["master-data-postgres"]
    assert isinstance(job, dict)
    steps = _steps(job)
    test_step = steps[_step_index(steps, "Run master-data and architecture tests")]
    test_step.pop("env", None)
    with pytest.raises(AssertionError):
        _assert_capacity_report_context(job, steps)


def test_review_two_workflow_fixture_rejects_an_unreachable_job() -> None:
    workflow = deepcopy(_workflow())
    jobs = workflow["jobs"]
    assert isinstance(jobs, dict)
    job = jobs["master-data-postgres"]
    assert isinstance(job, dict)
    job["if"] = False
    with pytest.raises(AssertionError):
        _assert_reachable(job, _steps(job))


def test_review_two_workflow_fixture_rejects_a_comment_only_required_command() -> None:
    workflow = deepcopy(_workflow())
    jobs = workflow["jobs"]
    assert isinstance(jobs, dict)
    job = jobs["master-data-postgres"]
    assert isinstance(job, dict)
    steps = _steps(job)
    step = steps[_step_index(steps, "Run Ruff lint")]
    step["run"] = "# python -m ruff check app tests\n"
    with pytest.raises(AssertionError):
        _assert_required_run_semantics(steps)


@pytest.mark.parametrize(
    ("name", "commented_command"),
    [
        ("Bootstrap isolated database roles", "infra/postgres/001-campus-ai-roles.sh"),
        (
            "Run master-data and architecture tests",
            "python -m pytest tests/master_data tests/architecture -q",
        ),
        (
            "Run master-data and architecture tests",
            "grep -Eq '[0-9]+ skipped' master-data-pytest.txt",
        ),
        ("Build wheel", "python -m build --wheel"),
    ],
)
def test_review_three_inline_comment_short_circuits_are_not_active(
    commented_command: str,
    name: str,
) -> None:
    workflow = deepcopy(_workflow())
    jobs = workflow["jobs"]
    assert isinstance(jobs, dict)
    job = jobs["master-data-postgres"]
    assert isinstance(job, dict)
    steps = _steps(job)
    step = steps[_step_index(steps, name)]
    step["run"] = f"true # {commented_command}\n"
    with pytest.raises(AssertionError):
        _assert_required_run_semantics(steps)


@pytest.mark.parametrize(
    ("name", "uses", "expected_uses"),
    [
        ("Checkout", "actions/checkout@v3", "actions/checkout@v4"),
        ("Set up Python", "actions/setup-python@v4", "actions/setup-python@v5"),
    ],
)
def test_review_three_required_actions_cannot_be_replaced(
    expected_uses: str,
    name: str,
    uses: str,
) -> None:
    workflow = deepcopy(_workflow())
    jobs = workflow["jobs"]
    assert isinstance(jobs, dict)
    job = jobs["master-data-postgres"]
    assert isinstance(job, dict)
    steps = _steps(job)
    step = steps[_step_index(steps, name)]
    step["uses"] = uses
    with pytest.raises(AssertionError):
        _assert_setup_actions(steps)


@pytest.mark.parametrize(
    "mutation",
    [
        "skip_exit",
        "pytest_or_true",
        "ruff_heredoc",
        "trigger_dispatch",
    ],
)
def test_review_four_production_validators_reject_control_flow_and_trigger_escapes(
    mutation: str,
) -> None:
    workflow = deepcopy(_workflow())
    jobs = workflow["jobs"]
    assert isinstance(jobs, dict)
    job = jobs["master-data-postgres"]
    assert isinstance(job, dict)
    steps = _steps(job)
    if mutation == "skip_exit":
        step = steps[_step_index(steps, "Run master-data and architecture tests")]
        assert isinstance(step["run"], str)
        step["run"] = step["run"].replace("exit 1", "true")
        with pytest.raises(AssertionError):
            _assert_required_run_semantics(steps)
    elif mutation == "pytest_or_true":
        step = steps[_step_index(steps, "Run master-data and architecture tests")]
        assert isinstance(step["run"], str)
        original = step["run"]
        step["run"] = original.replace(
            " | tee master-data-pytest.txt",
            " | tee master-data-pytest.txt || true",
        )
        assert step["run"] != original
        with pytest.raises(AssertionError):
            _assert_required_run_semantics(steps)
    elif mutation == "ruff_heredoc":
        step = steps[_step_index(steps, "Run Ruff lint")]
        step["run"] = "cat <<'COMMAND'\npython -m ruff check app tests\nCOMMAND\n"
        with pytest.raises(AssertionError):
            _assert_required_run_semantics(steps)
    else:
        workflow["on"] = {"workflow_dispatch": None}
        with pytest.raises(AssertionError):
            _assert_triggers(workflow)


def test_master_data_fixture_has_a_posix_bootstrap_path_without_weakening_windows_checks() -> None:
    source = CONFTST.read_text(encoding="utf-8")
    assert 'sys.platform == "win32"' in source
    assert '"psql.exe"' in source
    assert '"psql"' in source
    assert 'shutil.which("sh")' in source
    assert "MASTER_DATA_POSTGRES_BIN" in source
