from __future__ import annotations

import posixpath
import re
from collections import Counter
from collections.abc import Iterator
from copy import deepcopy
from pathlib import Path

import pytest
import yaml

from tests import audited_skip_guard

from . import assistant_contract_guard, assistant_http_guard

API_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = API_ROOT.parents[1]
WORKFLOW = PROJECT_ROOT / ".github" / "workflows" / "assistant-gates.yml"
BACKEND_MAIN = API_ROOT / "app" / "main.py"
ASSISTANT_HTTP = API_ROOT / "app" / "interfaces" / "http" / "assistant.py"
ASSISTANT_CATALOG = API_ROOT / "app" / "platform" / "assistant" / "catalog.py"
FRONTEND_BOUNDARY = (
    PROJECT_ROOT / "apps" / "web" / "tests" / ("assistant_frontend_boundary.spec.ts")
)
FRONTEND_SOURCE_ROOT = PROJECT_ROOT / "apps" / "web" / "src"
FORBIDDEN_PRODUCTION_ASSISTANT_MODULES = frozenset(
    {
        "features/assistant/AssistantConversation.vue",
        "features/assistant/assistant_api.ts",
        "features/assistant/assistant_contracts.ts",
        "features/assistant/assistant_session.ts",
    }
)
_FRONTEND_IMPORT = re.compile(
    r"(?:\bfrom\s+|\bimport\s*\(\s*|\bimport\s+)[\"'](?P<specifier>[^\"']+)[\"']"
)

EXPECTED_STEP_NAMES = [
    "Checkout",
    "Set up Python",
    "Set up Node",
    "Install PostgreSQL 16 client",
    "Install API development and supported assistant dependencies",
    "Install web dependencies",
    "Create isolated pytest configuration",
    "Verify pytest collection boundary",
    "Verify disposable PostgreSQL system identifier",
    "Bootstrap isolated database roles",
    "Apply Alembic migrations",
    "Reconcile isolated database roles after migrations",
    "Authenticate Redis service",
    "Run migration transaction and role tests",
    "Run real Redis Lua test",
    "Run all assistant and architecture tests",
    "Verify supported framework control boundaries without provider calls",
    "Run full backend pytest",
    "Run Ruff lint",
    "Run Ruff formatting check",
    "Run Pyright",
    "Build fresh wheel",
    "Verify wheel contains assistant boundaries",
    "Run frontend Vitest",
    "Build frontend",
    "Build F1 frontend",
    "Build formal DingTalk school facts frontend",
    "Verify production frontend excludes assistant shell",
]

EXPECTED_RUN_LINES = {
    "Install PostgreSQL 16 client": [
        "sudo apt-get update",
        "sudo apt-get install --yes postgresql-client-16",
        'psql --version | grep -F "PostgreSQL) 16"',
    ],
    "Install API development and supported assistant dependencies": [
        'python -m pip install -e ".[dev,f2-assistant]"'
    ],
    "Install web dependencies": ["npm ci"],
    "Create isolated pytest configuration": [
        'python -c "from pathlib import Path; '
        "Path('$RUNNER_TEMP/assistant-pytest.ini').write_text("
        "'[pytest]\\naddopts = -ra\\nmarkers =\\n    integration: requires the local "
        "PostgreSQL integration database\\n    destructive_migration: requires a "
        "function-scoped disposable migration database\\n', encoding='utf-8')\""
    ],
    "Verify pytest collection boundary": ["python tests/audited_skip_guard.py --verify-collection"],
    "Verify disposable PostgreSQL system identifier": [
        'system_identifier="$(PGUSER=$POSTGRES_BOOTSTRAP_USER '
        "PGPASSWORD=$POSTGRES_BOOTSTRAP_PASSWORD psql -X -Atqc "
        "'SELECT system_identifier FROM pg_control_system()')\"",
        'test -n "$system_identifier"',
        'echo "ASSISTANT_DISPOSABLE_CLUSTER_SYSTEM_IDENTIFIER=$system_identifier" >> "$GITHUB_ENV"',
        'echo "MASTER_DATA_DISPOSABLE_CLUSTER_SYSTEM_IDENTIFIER=$system_identifier" '
        '>> "$GITHUB_ENV"',
        'echo "MASTER_DATA_POSTGRES_BIN=$(dirname "$(command -v psql)")" >> "$GITHUB_ENV"',
    ],
    "Bootstrap isolated database roles": [
        'POSTGRES_EXPECTED_SYSTEM_IDENTIFIER="$ASSISTANT_DISPOSABLE_CLUSTER_SYSTEM_IDENTIFIER" '
        "sh infra/postgres/001-campus-ai-roles.sh"
    ],
    "Apply Alembic migrations": ["python -m alembic upgrade 20260824_20"],
    "Reconcile isolated database roles after migrations": [
        'POSTGRES_EXPECTED_SYSTEM_IDENTIFIER="$ASSISTANT_DISPOSABLE_CLUSTER_SYSTEM_IDENTIFIER" '
        "sh infra/postgres/001-campus-ai-roles.sh"
    ],
    "Authenticate Redis service": [
        'docker exec "${{ job.services.redis.id }}" redis-cli CONFIG SET requirepass '
        '"$REDIS_PASSWORD"',
        'docker exec "${{ job.services.redis.id }}" redis-cli -a "$REDIS_PASSWORD" ping',
    ],
    "Run migration transaction and role tests": [
        "set -o pipefail",
        'python -m pytest -c "$RUNNER_TEMP/assistant-pytest.ini" --rootdir=. '
        "tests/test_migration_baseline.py "
        "tests/master_data/test_task5_transaction_records.py "
        "tests/master_data/test_role_separation.py -q -rs | tee assistant-postgres-pytest.txt",
        "if grep -Eq '[0-9]+ (skipped|xfailed|xpassed)' assistant-postgres-pytest.txt; then",
        'echo "assistant PostgreSQL tests must not skip or xfail in CI" >&2',
        "exit 1",
        "fi",
    ],
    "Run real Redis Lua test": [
        "set -o pipefail",
        'python -m pytest -c "$RUNNER_TEMP/assistant-pytest.ini" --rootdir=. '
        "tests/assistant/test_control_redis_store.py::"
        "test_real_redis_lua_persists_across_adapter_instances_is_atomic_and_auto_expires "
        "-q -rs | tee assistant-redis-pytest.txt",
        "if grep -Eq '[0-9]+ (skipped|xfailed|xpassed)' assistant-redis-pytest.txt; then",
        'echo "assistant Redis test must not skip or xfail in CI" >&2',
        "exit 1",
        "fi",
    ],
    "Run all assistant and architecture tests": [
        "set -o pipefail",
        'python -m pytest -c "$RUNNER_TEMP/assistant-pytest.ini" --rootdir=. '
        "tests/assistant tests/architecture -q -rs | tee assistant-pytest.txt",
        "if grep -Eq '[0-9]+ (skipped|xfailed|xpassed)' assistant-pytest.txt; then",
        'echo "assistant/architecture tests must not skip or xfail in CI" >&2',
        "exit 1",
        "fi",
    ],
    "Verify supported framework control boundaries without provider calls": [
        "set -o pipefail",
        'python -m pytest -c "$RUNNER_TEMP/assistant-pytest.ini" --rootdir=. '
        "candidates/pydanticai_f2/tests/test_runner_boundaries.py "
        "candidates/pydanticai_f2/tests/test_provider.py "
        "candidates/pydanticai_f2/tests/test_send_handoff.py "
        "candidates/pydanticai_f2/tests/test_history_recovery.py "
        "candidates/pydanticai_f2/tests/test_controls.py "
        "-q -rs | tee supported-framework-pytest.txt",
        "if grep -Eq '[0-9]+ (skipped|xfailed|xpassed)' supported-framework-pytest.txt; then",
        'echo "supported framework controls must not skip or xfail in CI" >&2',
        "exit 1",
        "fi",
    ],
    "Run full backend pytest": [
        "set -o pipefail",
        'python -m pytest -c "$RUNNER_TEMP/assistant-pytest.ini" --rootdir=. '
        "tests -q -rs -p tests.audited_skip_guard | "
        "tee full-backend-pytest.txt",
    ],
    "Run Ruff lint": ["python -m ruff check app tests"],
    "Run Ruff formatting check": ["python -m ruff format --check app tests"],
    "Run Pyright": ["pyright"],
    "Build fresh wheel": [
        'python -m build --wheel --outdir "$RUNNER_TEMP/campus-ai-assistant-wheel"'
    ],
    "Verify wheel contains assistant boundaries": [
        'python -c "from pathlib import Path; from zipfile import ZipFile; '
        "wheels=list(Path('$RUNNER_TEMP/campus-ai-assistant-wheel').glob('*.whl')); "
        "assert len(wheels)==1, 'expected one assistant wheel'; "
        "required={'app/composition/assistant_controls.py','app/interfaces/http/assistant.py',"
        "'app/interfaces/http/assistant_schemas.py','app/platform/assistant/__init__.py',"
        "'app/platform/assistant/catalog.py','app/platform/assistant/contracts.py',"
        "'app/platform/assistant/controls.py','app/platform/assistant/orchestrator.py',"
        "'app/platform/assistant/ports.py','app/platform/assistant/presentation.py',"
        "'app/platform/assistant/runtime.py','app/platform/assistant/pydanticai_runner.py',"
        "'app/platform/assistant/pydanticai_history.py','app/platform/assistant/pydanticai_errors.py',"
        "'app/composition/school_fact_pydanticai.py',"
        "'app/composition/school_fact_pydanticai_model.py'}; "
        "names=set(ZipFile(wheels[0]).namelist()); "
        "assert not any(name.startswith(('candidates/','experiments/')) for name in names), "
        "'historical packages must not ship in the product wheel'; "
        "missing=sorted(required-names); assert not missing, "
        "f'missing wheel files: {missing}'\""
    ],
    "Run frontend Vitest": ["npm test"],
    "Build frontend": ["npm run build"],
    "Build F1 frontend": [
        "./node_modules/.bin/vite build --config vite.school-facts.config.ts "
        "--outDir ../../runtime/f1-evidence/web-build"
    ],
    "Build formal DingTalk school facts frontend": ["npm run build:dingtalk-school-facts"],
    "Verify production frontend excludes assistant shell": [
        'python -c "from pathlib import Path; '
        "suffixes={'.css','.html','.js','.map'}; "
        "text=''.join(path.read_text(encoding='utf-8') for path in Path('dist').rglob('*') "
        "if path.is_file() and path.suffix in suffixes); "
        "forbidden=('/api/v1/assistant/conversations','action-offers','recovery-controls',"
        "'result-checks'); hits=[value for value in forbidden if value in text]; "
        "assert not hits, f'assistant shell leaked into production dist: {hits}'\""
    ],
}


def _workflow() -> dict[str, object]:
    assert WORKFLOW.is_file(), "assistant CI workflow is required"
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


def _assistant_job(workflow: dict[str, object]) -> dict[str, object]:
    jobs = workflow.get("jobs")
    assert isinstance(jobs, dict)
    assert set(jobs) == {"assistant-gates"}, "only the reachable assistant gate is accepted"
    job = jobs["assistant-gates"]
    assert isinstance(job, dict)
    return job


def _steps(job: dict[str, object]) -> list[dict[str, object]]:
    value = job.get("steps")
    assert isinstance(value, list), "assistant job must contain ordered steps"
    assert all(isinstance(step, dict) for step in value), "steps must be mappings"
    return value  # type: ignore[return-value]


def _step(steps: list[dict[str, object]], name: str) -> dict[str, object]:
    matches = [step for step in steps if step.get("name") == name]
    assert len(matches) == 1, f"required step must appear exactly once: {name}"
    return matches[0]


def _active_run_lines(step: dict[str, object]) -> list[str]:
    run = step.get("run")
    assert isinstance(run, str), f"{step.get('name')} must execute a command"
    return [
        line.strip()
        for line in run.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def _assert_triggers(workflow: dict[str, object]) -> None:
    assert workflow.get("on") == {"pull_request": "", "push": ""}


def _assert_services(job: dict[str, object]) -> None:
    services = job.get("services")
    assert isinstance(services, dict) and set(services) == {"postgres", "redis"}

    postgres = services["postgres"]
    assert isinstance(postgres, dict)
    assert postgres.get("image") == "postgres:16-alpine"
    assert postgres.get("env") == {
        "POSTGRES_DB": "campus_ai_assistant_test",
        "POSTGRES_USER": "campus_ai_ci_bootstrap",
        "POSTGRES_PASSWORD": "campus-ai-ci-bootstrap-password",
    }
    assert postgres.get("ports") == ["5432:5432"]
    assert "pg_isready -U campus_ai_ci_bootstrap -d campus_ai_assistant_test" in str(
        postgres.get("options")
    )

    redis = services["redis"]
    assert isinstance(redis, dict)
    assert redis.get("image") == "redis:7-alpine"
    assert redis.get("ports") == ["6379:6379"]
    assert "redis-cli ping || redis-cli -a campus-ai-ci-redis-password ping" in str(
        redis.get("options")
    )
    assert "env" not in redis, "official Redis service must be authenticated explicitly"


def _assert_environment(job: dict[str, object]) -> None:
    environment = job.get("env")
    assert isinstance(environment, dict)
    expected = {
        "APP_ENV": "test",
        "IDENTITY_PROVIDER": "local_test",
        "POSTGRES_HOST": "127.0.0.1",
        "POSTGRES_PORT": "5432",
        "POSTGRES_DB": "campus_ai_assistant_test",
        "POSTGRES_DATABASE": "campus_ai_assistant_test",
        "POSTGRES_USER": "campus_ai_runtime",
        "POSTGRES_PASSWORD": "campus-ai-ci-runtime-password",
        "PGHOST": "127.0.0.1",
        "PGPORT": "5432",
        "PGDATABASE": "campus_ai_assistant_test",
        "POSTGRES_BOOTSTRAP_USER": "campus_ai_ci_bootstrap",
        "POSTGRES_BOOTSTRAP_PASSWORD": "campus-ai-ci-bootstrap-password",
        "POSTGRES_RUNTIME_PASSWORD": "campus-ai-ci-runtime-password",
        "POSTGRES_MIGRATION_HOST": "127.0.0.1",
        "POSTGRES_MIGRATION_PORT": "5432",
        "POSTGRES_MIGRATION_DB": "campus_ai_assistant_test",
        "POSTGRES_MIGRATION_USER": "campus_ai_migration",
        "POSTGRES_MIGRATION_PASSWORD": "campus-ai-ci-migration-password",
        "RLS_CONTEXT_SIGNING_KEY": "master-data-schema-context-signing-key-32-bytes",
        "POSTGRES_MIGRATION_CONTEXT_SIGNING_KEY": (
            "master-data-schema-context-signing-key-32-bytes"
        ),
        "REDIS_HOST": "127.0.0.1",
        "REDIS_PORT": "6379",
        "REDIS_PASSWORD": "campus-ai-ci-redis-password",
        "RUN_POSTGRES_INTEGRATION": "1",
    }
    assert environment == expected
    assert not any("secrets." in str(value) for value in environment.values())


def _assert_setup_actions(steps: list[dict[str, object]]) -> None:
    assert _step(steps, "Checkout") == {
        "name": "Checkout",
        "uses": "actions/checkout@v4",
    }
    assert _step(steps, "Set up Python") == {
        "name": "Set up Python",
        "uses": "actions/setup-python@v5",
        "with": {"python-version": "3.13"},
    }
    assert _step(steps, "Set up Node") == {
        "name": "Set up Node",
        "uses": "actions/setup-node@v4",
        "with": {
            "node-version": "22",
            "cache": "npm",
            "cache-dependency-path": "apps/web/package-lock.json",
        },
    }


def _assert_steps(job: dict[str, object]) -> None:
    steps = _steps(job)
    bootstrap_secret_environment = {
        "POSTGRES_SCHOOL_FACT_WRITER_PASSWORD": "campus-ai-ci-school-fact-writer-password",
        "POSTGRES_SCHOOL_FACT_READER_PASSWORD": "campus-ai-ci-school-fact-reader-password",
    }
    for step_name in (
        "Bootstrap isolated database roles",
        "Reconcile isolated database roles after migrations",
    ):
        assert _step(steps, step_name).get("env") == bootstrap_secret_environment
    assert [step.get("name") for step in steps] == EXPECTED_STEP_NAMES
    _assert_setup_actions(steps)
    for name, expected_lines in EXPECTED_RUN_LINES.items():
        assert _active_run_lines(_step(steps, name)) == expected_lines

    for name in (
        "Install API development and supported assistant dependencies",
        "Verify pytest collection boundary",
        "Apply Alembic migrations",
        "Run migration transaction and role tests",
        "Run real Redis Lua test",
        "Run all assistant and architecture tests",
        "Run full backend pytest",
        "Run Ruff lint",
        "Run Ruff formatting check",
        "Run Pyright",
        "Build fresh wheel",
        "Verify wheel contains assistant boundaries",
    ):
        assert _step(steps, name).get("working-directory") == "apps/api"
    for name in (
        "Install web dependencies",
        "Run frontend Vitest",
        "Build frontend",
        "Build F1 frontend",
        "Build formal DingTalk school facts frontend",
        "Verify production frontend excludes assistant shell",
    ):
        assert _step(steps, name).get("working-directory") == "apps/web"
    assert "working-directory" not in _step(
        steps, "Verify supported framework control boundaries without provider calls"
    )
    for step in steps:
        if step.get("name") not in {
            "Bootstrap isolated database roles",
            "Reconcile isolated database roles after migrations",
        }:
            assert "env" not in step, "ordinary steps may not receive bootstrap-only secrets"


def _assert_unconditional_fail_closed(workflow: dict[str, object]) -> None:
    job = _assistant_job(workflow)
    assert "defaults" not in workflow
    assert "defaults" not in job
    assert "if" not in job
    for step in _steps(job):
        assert "if" not in step
        assert "shell" not in step
    for mapping in _mappings(workflow):
        assert "continue-on-error" not in mapping
    source = WORKFLOW.read_text(encoding="utf-8")
    for escape in (
        "pytest.skip",
        "@pytest.mark.skip",
        "@pytest.mark.xfail",
        "--deselect",
        "--ignore",
        "--maxfail",
        "|| true",
    ):
        assert escape not in source


def _assert_complete_gate(workflow: dict[str, object]) -> None:
    _assert_triggers(workflow)
    job = _assistant_job(workflow)
    assert job.get("runs-on") == "ubuntu-latest"
    _assert_services(job)
    _assert_environment(job)
    _assert_steps(job)
    _assert_unconditional_fail_closed(workflow)


def _frontend_sources(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): path.read_text(encoding="utf-8")
        for path in root.rglob("*")
        if path.is_file() and path.suffix in {".ts", ".vue"}
    }


def _resolve_frontend_import(
    *, importer: str, specifier: str, sources: dict[str, str]
) -> str | None:
    if not specifier.startswith("."):
        return None
    if posixpath.splitext(specifier)[1] not in {"", ".js", ".ts", ".vue"}:
        return None
    unresolved = posixpath.normpath(posixpath.join(posixpath.dirname(importer), specifier))
    for candidate in (
        unresolved,
        f"{unresolved}.ts",
        f"{unresolved}.vue",
        f"{unresolved}/index.ts",
        f"{unresolved}/index.vue",
    ):
        if candidate in sources:
            return candidate
    raise AssertionError(f"unresolved production frontend import: {importer} -> {specifier}")


def _reachable_frontend_modules(*, sources: dict[str, str], entries: tuple[str, ...]) -> set[str]:
    pending = list(entries)
    reachable: set[str] = set()
    while pending:
        current = pending.pop()
        assert current in sources, f"missing production frontend entry: {current}"
        if current in reachable:
            continue
        reachable.add(current)
        for match in _FRONTEND_IMPORT.finditer(sources[current]):
            resolved = _resolve_frontend_import(
                importer=current,
                specifier=match.group("specifier"),
                sources=sources,
            )
            if resolved is not None:
                pending.append(resolved)
    return reachable


def _assert_production_frontend_graph_isolated(sources: dict[str, str]) -> None:
    reachable = _reachable_frontend_modules(sources=sources, entries=("main.ts",))
    leaked = sorted(reachable & FORBIDDEN_PRODUCTION_ASSISTANT_MODULES)
    assert not leaked, f"assistant shell is reachable from production: {leaked}"


def test_assistant_workflow_is_a_complete_unconditional_integration_gate() -> None:
    _assert_complete_gate(_workflow())


def test_full_backend_gate_consumes_f1_without_new_skip_allowances() -> None:
    steps = _steps(_assistant_job(_workflow()))
    assert _active_run_lines(_step(steps, "Run full backend pytest")) == [
        "set -o pipefail",
        'python -m pytest -c "$RUNNER_TEMP/assistant-pytest.ini" --rootdir=. '
        "tests -q -rs -p tests.audited_skip_guard | tee full-backend-pytest.txt",
    ]
    assert not any(step.get("name") == "Run F1 product gates" for step in steps)
    required = (
        "tests/test_f1_contact_product.py",
        "tests/test_f1_product_identity.py",
        "tests/test_f1_official_contact_query.py",
        "tests/test_f1_initial_publication.py",
        "tests/test_f1_model_budget.py",
        "tests/school_facts/test_f1_initialization_contract.py",
        "tests/school_facts/test_f1_product_postgres.py",
        "tests/assistant/test_school_fact_product_factory.py",
        "tests/assistant/test_deepseek_assistant_model.py",
        "tests/assistant/test_tool_loop.py",
    )
    inventory = audited_skip_guard.documented_skip_inventory(
        audited_skip_guard.SKIP_AUDIT.read_text(encoding="utf-8"),
        windows_symlink_unavailable=True,
        postgres_integration_enabled=True,
    )
    assert sum(inventory.values()) == 31
    for relative in required:
        assert (API_ROOT / relative).is_file()
        assert not any(node.startswith(f"{relative}::") for node in inventory)
    node = (
        "tests/school_facts/test_f1_product_postgres.py::"
        "test_f1_real_initialization_identity_publication_reader_writer_round_trip"
    )
    source = (API_ROOT / required[6]).read_text(encoding="utf-8")
    assert f"def {node.split('::')[1]}(" in source
    observed = inventory.copy()
    observed[node] += 1
    assert audited_skip_guard.inventory_issues(
        expected=inventory, observed=observed, xfailed=[], deselected=[]
    ) == [f"unexpected skips: [('{node}', 1)]"]
    assert audited_skip_guard.inventory_issues(
        expected=inventory, observed=inventory, xfailed=[node], deselected=[]
    ) == [f"xfail/xpass is forbidden: ['{node}']"]


def test_full_backend_skip_inventory_is_exact_and_rejects_drift() -> None:
    audit_text = audited_skip_guard.SKIP_AUDIT.read_text(encoding="utf-8")
    disabled_inventory = audited_skip_guard.documented_skip_inventory(
        audit_text,
        windows_symlink_unavailable=True,
        postgres_integration_enabled=False,
    )
    assert sum(disabled_inventory.values()) == 214
    assert (
        disabled_inventory[
            "tests/test_live_governance_postgres.py::test_live_batch_real_writer_after_dual_closure"
        ]
        == 1
    )
    assert (
        disabled_inventory[
            "tests/test_live_initialization_postgres.py::"
            "test_formal_initialization_closes_and_preserves_upgrade"
        ]
        == 1
    )

    inventory = audited_skip_guard.documented_skip_inventory(
        audit_text,
        windows_symlink_unavailable=True,
        postgres_integration_enabled=True,
    )
    assert sum(inventory.values()) == 31
    assert not any("school_facts" in node for node in inventory)

    with pytest.raises(AssertionError, match="exact audited groups"):
        audited_skip_guard.documented_skip_inventory(
            audit_text
            + "\n### 未登记 PostgreSQL 门（0 项）\n"
            + "- 明确条件：仅在 `RUN_POSTGRES_INTEGRATION=1` 时运行。\n",
            windows_symlink_unavailable=True,
            postgres_integration_enabled=True,
        )
    with pytest.raises(AssertionError, match="exact audited groups"):
        audited_skip_guard.documented_skip_inventory(
            audit_text.replace(
                "共享主数据 PostgreSQL 角色门（162 项）",
                "共享主数据 PostgreSQL 角色门（161 项）",
                1,
            ),
            windows_symlink_unavailable=True,
            postgres_integration_enabled=True,
        )

    extra = inventory.copy()
    extra["tests/new_test.py::test_hidden"] += 1
    assert audited_skip_guard.inventory_issues(
        expected=inventory,
        observed=extra,
        xfailed=[],
        deselected=[],
    ) == ["unexpected skips: [('tests/new_test.py::test_hidden', 1)]"]
    assert (
        len(
            audited_skip_guard.inventory_issues(
                expected=inventory,
                observed=Counter(),
                xfailed=["tests/old.py::test_xfail"],
                deselected=["tests/old.py::test_hidden"],
            )
        )
        == 3
    )


def test_production_frontend_graph_guard_rejects_two_hop_assistant_wiring() -> None:
    malicious_sources = {
        "main.ts": 'import "./app/AssistantEntry";',
        "app/AssistantEntry.ts": ('import "../features/assistant/AssistantConversation.vue";'),
        "features/assistant/AssistantConversation.vue": "<template />",
    }
    with pytest.raises(AssertionError, match="reachable from production"):
        _assert_production_frontend_graph_isolated(malicious_sources)


def test_assistant_gate_pins_production_isolation_and_responsibility_allowlists() -> None:
    main_source = BACKEND_MAIN.read_text(encoding="utf-8")
    http_source = ASSISTANT_HTTP.read_text(encoding="utf-8")
    catalog_source = ASSISTANT_CATALOG.read_text(encoding="utf-8")
    frontend_boundary_source = FRONTEND_BOUNDARY.read_text(encoding="utf-8")
    _assert_production_frontend_graph_isolated(_frontend_sources(FRONTEND_SOURCE_ROOT))

    assert "app.interfaces.http.assistant" not in main_source
    assert "create_assistant_router" not in main_source
    assert "/probe" not in http_source.casefold()
    assert "probe" not in catalog_source.casefold()
    assert "responsibility" not in catalog_source.casefold()
    for production_entry in (
        "../src/main.ts",
        "../src/app/DingTalkEntryApp.vue",
    ):
        assert production_entry in frontend_boundary_source
    assert "keeps the K7 shell business-free and outside all production entries" in (
        frontend_boundary_source
    )

    assert assistant_contract_guard._K1_ALLOWED_IMPORT_ROOTS == frozenset(  # noqa: SLF001
        {"__future__", "collections", "pydantic", "typing"}
    )
    assert assistant_contract_guard._RUNTIME_ALLOWED_IMPORT_ROOTS == frozenset(  # noqa: SLF001
        {
            "__future__",
            "collections",
            "dataclasses",
            "datetime",
            "math",
            "pydantic",
            "threading",
            "typing",
            "uuid",
        }
    )
    assert assistant_contract_guard._IMPLEMENTATION_ALLOWED_IMPORT_ROOTS == frozenset(  # noqa: SLF001
        {"__future__", "collections", "dataclasses", "pydantic", "typing", "uuid"}
    )
    assert assistant_contract_guard._PRESENTATION_ALLOWED_IMPORT_ROOTS == frozenset(  # noqa: SLF001
        {"__future__", "collections", "dataclasses", "json", "pydantic", "typing"}
    )
    assert assistant_contract_guard._CONTROL_ALLOWED_IMPORT_ROOTS == frozenset(  # noqa: SLF001
        {
            "__future__",
            "base64",
            "collections",
            "dataclasses",
            "datetime",
            "hashlib",
            "hmac",
            "json",
            "pydantic",
            "secrets",
            "typing",
            "uuid",
        }
    )
    assert assistant_contract_guard._ALLOWED_SIBLING_IMPORTS == {  # noqa: SLF001
        "catalog.py": frozenset({"ports"}),
        "controls.py": frozenset({"catalog", "contracts", "ports", "presentation"}),
        "orchestrator.py": frozenset(
            {"catalog", "contracts", "controls", "ports", "presentation", "runtime"}
        ),
        "ports.py": frozenset({"contracts"}),
        "presentation.py": frozenset({"contracts"}),
        "runtime.py": frozenset({"contracts"}),
        "tool_loop.py": frozenset({"contracts", "controls", "runtime"}),
    }
    assert assistant_http_guard._EXPECTED_FILES == frozenset(  # noqa: SLF001
        {"assistant.py", "assistant_schemas.py"}
    )
    assert assistant_http_guard._ALLOWED_IMPORTS == {  # noqa: SLF001
        "assistant.py": frozenset(
            {
                "__future__",
                "collections.abc",
                "dataclasses",
                "typing",
                "uuid",
                "fastapi",
                "fastapi.responses",
                "starlette.types",
                "anyio",
                "app.platform.assistant.contracts",
                "app.platform.assistant.diagnostics",
                "app.interfaces.http.assistant_schemas",
                "app.interfaces.http.errors",
                "app.interfaces.http.framework_request",
                "app.platform.assistant.runtime",
                "app.platform.assistant.orchestrator",
                "app.platform.assistant.controls",
            }
        ),
        "assistant_schemas.py": frozenset(
            {
                "__future__",
                "typing",
                "pydantic",
                "app.platform.assistant.contracts",
            }
        ),
    }


@pytest.mark.parametrize(
    "mutation",
    [
        "drop_postgres",
        "downgrade_postgres",
        "drop_redis",
        "unauthenticated_redis",
        "skip_current_head",
        "skip_transaction",
        "skip_role",
        "skip_real_redis",
        "narrow_assistant_tests",
        "skip_full_backend",
        "omit_supported_dependency",
        "skip_supported_controls",
        "misplace_supported_controls",
        "omit_supported_wheel_runner",
        "skip_frontend_build",
        "skip_f1_frontend_build",
        "skip_frontend_isolation_scan",
        "skip_wheel_file",
        "condition_step",
        "continue_on_error",
        "step_shell_without_errexit",
        "job_default_shell_without_errexit",
        "step_env_pytest_narrowing",
        "drop_isolated_pytest_config",
    ],
)
def test_review_fixture_rejects_a_weakened_assistant_gate(mutation: str) -> None:
    workflow = deepcopy(_workflow())
    job = _assistant_job(workflow)
    steps = _steps(job)
    services = job["services"]
    assert isinstance(services, dict)

    if mutation == "drop_postgres":
        services.pop("postgres")
    elif mutation == "downgrade_postgres":
        postgres = services["postgres"]
        assert isinstance(postgres, dict)
        postgres["image"] = "postgres:15-alpine"
    elif mutation == "drop_redis":
        services.pop("redis")
    elif mutation == "unauthenticated_redis":
        _step(steps, "Authenticate Redis service")["run"] = "redis-cli ping"
    elif mutation == "skip_current_head":
        step = _step(steps, "Run migration transaction and role tests")
        assert isinstance(step["run"], str)
        step["run"] = step["run"].replace("tests/test_migration_baseline.py ", "")
    elif mutation == "skip_transaction":
        step = _step(steps, "Run migration transaction and role tests")
        assert isinstance(step["run"], str)
        step["run"] = step["run"].replace(
            "tests/master_data/test_task5_transaction_records.py ", ""
        )
    elif mutation == "skip_role":
        step = _step(steps, "Run migration transaction and role tests")
        assert isinstance(step["run"], str)
        step["run"] = step["run"].replace("tests/master_data/test_role_separation.py ", "")
    elif mutation == "skip_real_redis":
        _step(steps, "Run real Redis Lua test")["run"] = "python -m pytest -q"
    elif mutation == "narrow_assistant_tests":
        step = _step(steps, "Run all assistant and architecture tests")
        assert isinstance(step["run"], str)
        step["run"] = step["run"].replace(
            "tests/assistant tests/architecture", "tests/assistant/test_contracts.py"
        )
    elif mutation == "omit_supported_dependency":
        _step(steps, "Install API development and supported assistant dependencies")["run"] = (
            'python -m pip install -e ".[dev]"'
        )
    elif mutation == "skip_supported_controls":
        steps.remove(
            _step(steps, "Verify supported framework control boundaries without provider calls")
        )
    elif mutation == "misplace_supported_controls":
        _step(steps, "Verify supported framework control boundaries without provider calls")[
            "working-directory"
        ] = "apps/api"
    elif mutation == "omit_supported_wheel_runner":
        step = _step(steps, "Verify wheel contains assistant boundaries")
        assert isinstance(step["run"], str)
        step["run"] = step["run"].replace("'app/platform/assistant/pydanticai_runner.py',", "")
    elif mutation == "skip_full_backend":
        steps.remove(_step(steps, "Run full backend pytest"))
    elif mutation == "drop_isolated_pytest_config":
        steps.remove(_step(steps, "Create isolated pytest configuration"))
    elif mutation == "skip_frontend_build":
        steps.remove(_step(steps, "Build frontend"))
    elif mutation == "skip_f1_frontend_build":
        steps.remove(_step(steps, "Build F1 frontend"))
    elif mutation == "skip_frontend_isolation_scan":
        steps.remove(_step(steps, "Verify production frontend excludes assistant shell"))
    elif mutation == "skip_wheel_file":
        step = _step(steps, "Verify wheel contains assistant boundaries")
        assert isinstance(step["run"], str)
        step["run"] = step["run"].replace("'app/platform/assistant/controls.py',", "")
    elif mutation == "condition_step":
        _step(steps, "Run all assistant and architecture tests")["if"] = "success()"
    elif mutation == "step_shell_without_errexit":
        _step(steps, "Run all assistant and architecture tests")["shell"] = "bash {0}"
    elif mutation == "job_default_shell_without_errexit":
        job["defaults"] = {"run": {"shell": "bash {0}"}}
    elif mutation == "step_env_pytest_narrowing":
        _step(steps, "Run all assistant and architecture tests")["env"] = {
            "PYTEST_ADDOPTS": "--ignore=tests/architecture"
        }
    else:
        _step(steps, "Run real Redis Lua test")["continue-on-error"] = "true"

    with pytest.raises(AssertionError):
        _assert_complete_gate(workflow)


@pytest.mark.parametrize(
    ("pytest_options", "python_sources"),
    [
        (
            {"testpaths": ["tests"], "addopts": "-ra --ignore=tests/architecture"},
            {},
        ),
        (
            audited_skip_guard.EXPECTED_PYTEST_OPTIONS,
            {"tests/conftest.py": "collect_ignore = ['architecture']"},
        ),
        (
            audited_skip_guard.EXPECTED_PYTEST_OPTIONS,
            {"tests/conftest.py": "def pytest_ignore_collect(path): return True"},
        ),
        (
            audited_skip_guard.EXPECTED_PYTEST_OPTIONS,
            {"conftest.py": "pytest_ignore_collect = lambda path: True"},
        ),
        (
            audited_skip_guard.EXPECTED_PYTEST_OPTIONS,
            {"conftest.py": "from helper import pytest_ignore_collect"},
        ),
        (
            audited_skip_guard.EXPECTED_PYTEST_OPTIONS,
            {"conftest.py": "pytest_plugins = ['helper']"},
        ),
        (
            audited_skip_guard.EXPECTED_PYTEST_OPTIONS,
            {"conftest.py": "from helper import collect_ignore"},
        ),
        (
            audited_skip_guard.EXPECTED_PYTEST_OPTIONS,
            {"conftest.py": "from helper import pytest_plugins"},
        ),
    ],
)
def test_collection_boundary_rejects_config_and_hook_escapes(
    pytest_options: dict[str, object], python_sources: dict[str, str]
) -> None:
    assert audited_skip_guard.collection_boundary_issues(
        pytest_options=pytest_options,
        python_sources=python_sources,
        alternate_configs=[],
    )


def test_destructive_migration_boundary_requires_marker_and_isolated_database() -> None:
    sources = {
        "tests/test_missing_marker.py": (
            "import subprocess\n"
            "def test_round_trip(isolated_migration_schema):\n"
            "    subprocess.run(['python', '-m', 'alembic', 'downgrade', 'base'])\n"
        ),
        "tests/test_shared_database.py": (
            "import pytest\n"
            "def _reset(environment):\n"
            "    _run_alembic(environment, 'downgrade', 'base')\n"
            "@pytest.mark.destructive_migration\n"
            "def test_round_trip(current_master_data_schema):\n"
            "    _reset({})\n"
        ),
    }

    issues = audited_skip_guard.destructive_migration_boundary_issues(sources)

    assert any("missing destructive_migration marker" in issue for issue in issues)
    assert any("requires isolated_migration_schema" in issue for issue in issues)
    assert any("forbids current_master_data_schema" in issue for issue in issues)


def test_destructive_migration_boundary_accepts_marked_transitive_wrapper() -> None:
    source = (
        "import pytest\n"
        "def _run(environment, *arguments):\n"
        "    return arguments\n"
        "def _reset(environment):\n"
        "    _run(environment, 'downgrade', 'base')\n"
        "@pytest.mark.destructive_migration\n"
        "def test_round_trip(isolated_migration_schema):\n"
        "    _reset({})\n"
    )

    assert (
        audited_skip_guard.destructive_migration_boundary_issues(
            {"tests/test_safe_round_trip.py": source}
        )
        == []
    )


def test_destructive_migration_boundary_accepts_revision19_isolated_fixture() -> None:
    source = (
        "import pytest\n"
        "@pytest.mark.destructive_migration\n"
        "def test_round_trip(isolated_revision19_schema):\n"
        "    _run_alembic({}, 'downgrade', 'base')\n"
    )

    assert (
        audited_skip_guard.destructive_migration_boundary_issues(
            {"tests/test_safe_revision19_round_trip.py": source}
        )
        == []
    )


def test_destructive_migration_boundary_follows_imported_helpers_across_files() -> None:
    sources = {
        "tests/helper.py": (
            "import subprocess\n"
            "def reset_database():\n"
            "    subprocess.run(['alembic', 'downgrade', 'base'])\n"
        ),
        "tests/test_escape.py": (
            "from tests.helper import reset_database\n"
            "def test_escape(current_master_data_schema):\n"
            "    reset_database()\n"
        ),
    }

    issues = audited_skip_guard.destructive_migration_boundary_issues(sources)

    assert any("missing destructive_migration marker" in issue for issue in issues)
    assert any("requires isolated_migration_schema" in issue for issue in issues)
    assert any("forbids current_master_data_schema" in issue for issue in issues)


@pytest.mark.parametrize(
    "source",
    [
        (
            "import subprocess\n"
            "class TestRoundTrip:\n"
            "    def test_escape(self, current_master_data_schema):\n"
            "        subprocess.run(['alembic', 'downgrade', 'base'])\n"
        ),
        (
            "from alembic import command\n"
            "def test_escape(current_master_data_schema):\n"
            "    command.downgrade(config, 'base')\n"
        ),
    ],
    ids=("class-method", "alembic-api"),
)
def test_destructive_migration_boundary_detects_class_and_alembic_api(source: str) -> None:
    issues = audited_skip_guard.destructive_migration_boundary_issues(
        {"tests/test_escape.py": source}
    )

    assert any("missing destructive_migration marker" in issue for issue in issues)
    assert any("requires isolated_migration_schema" in issue for issue in issues)
    assert any("forbids current_master_data_schema" in issue for issue in issues)


@pytest.mark.parametrize(
    "source",
    [
        (
            "import pytest\n"
            "from tests.missing_helper import reset_database\n"
            "@pytest.mark.destructive_migration\n"
            "def test_escape(current_master_data_schema):\n"
            "    reset_database()\n"
        ),
        (
            "import pytest\n"
            "@pytest.mark.destructive_migration\n"
            "def test_escape(current_master_data_schema):\n"
            "    getattr(helper, 'reset_database')()\n"
        ),
    ],
    ids=("unknown-import", "dynamic-getattr"),
)
def test_destructive_migration_boundary_fails_closed_for_unknown_reset_helper(
    source: str,
) -> None:
    issues = audited_skip_guard.destructive_migration_boundary_issues(
        {"tests/test_escape.py": source}
    )

    assert any("unresolved destructive helper" in issue for issue in issues)
    assert any("requires isolated_migration_schema" in issue for issue in issues)
    assert any("forbids current_master_data_schema" in issue for issue in issues)


@pytest.mark.parametrize(
    "source",
    [
        (
            "import pytest\n"
            "@pytest.mark.destructive_migration\n"
            "def test_escape(current_master_data_schema):\n"
            "    helper.migrate()\n"
        ),
        (
            "import pytest\n"
            "@pytest.mark.destructive_migration\n"
            "class TestRoundTrip:\n"
            "    def test_escape(self, current_master_data_schema):\n"
            "        helper.migrate()\n"
        ),
    ],
    ids=("function-marker", "class-marker"),
)
def test_destructive_marker_always_requires_the_isolated_fixture(source: str) -> None:
    issues = audited_skip_guard.destructive_migration_boundary_issues(
        {"tests/test_escape.py": source}
    )

    assert any("requires isolated_migration_schema" in issue for issue in issues)
    assert any("forbids current_master_data_schema" in issue for issue in issues)


@pytest.mark.parametrize(
    "call_source",
    [
        "subprocess.run(['alembic', 'stamp', 'base'])",
        "subprocess.check_call(('python', '-m', 'alembic', 'stamp', 'base'))",
        "subprocess.run(['alembic', operation, 'base'])",
    ],
    ids=("list", "tuple-python-module", "dynamic-operation"),
)
def test_destructive_migration_boundary_detects_alembic_cli_base_reset(
    call_source: str,
) -> None:
    source = f"import subprocess\ndef test_escape(current_master_data_schema):\n    {call_source}\n"

    issues = audited_skip_guard.destructive_migration_boundary_issues(
        {"tests/test_escape.py": source}
    )

    assert any("missing destructive_migration marker" in issue for issue in issues)
    assert any("requires isolated_migration_schema" in issue for issue in issues)
    assert any("forbids current_master_data_schema" in issue for issue in issues)


def test_destructive_migration_boundary_ignores_plain_stamp_base_text() -> None:
    source = (
        "def test_description(current_master_data_schema):\n"
        "    logger.info('ordinary stamp/base documentation')\n"
    )

    assert (
        audited_skip_guard.destructive_migration_boundary_issues(
            {"tests/test_description.py": source}
        )
        == []
    )


def test_dingtalk_postgres_skip_inventory_is_separate_from_legacy_cluster_opt_in():
    audit = audited_skip_guard.SKIP_AUDIT.read_text(encoding="utf-8")
    default = audited_skip_guard.documented_skip_inventory(
        audit,
        windows_symlink_unavailable=True,
        dingtalk_postgres_integration_enabled=False,
    )
    enabled = audited_skip_guard.documented_skip_inventory(
        audit,
        windows_symlink_unavailable=True,
        dingtalk_postgres_integration_enabled=True,
    )
    removed = default - enabled
    assert sum(removed.values()) == 12
    assert all(
        node.startswith("tests/test_dingtalk_product_identity_postgres.py::")
        or node
        == "tests/test_live_initialization_postgres.py::"
        "test_formal_initialization_closes_and_preserves_upgrade"
        or node
        == "tests/test_live_initialization_postgres.py::"
        "test_deferred_initialization_closes_without_publication_and_preserves_upgrade"
        or node
        == "tests/test_live_initialization_postgres.py::"
        "test_deferred_initialization_rejects_publication_started_after_precheck"
        or node
        == "tests/test_live_initialization_postgres.py::"
        "test_deferred_initialization_serializes_concurrent_publication"
        or node
        == "tests/test_live_governance_postgres.py::test_live_batch_real_writer_after_dual_closure"
        for node in removed
    )
    assert sum(enabled.values()) == 202
    both = audited_skip_guard.documented_skip_inventory(
        audit,
        windows_symlink_unavailable=True,
        postgres_integration_enabled=True,
        dingtalk_postgres_integration_enabled=True,
    )
    assert sum(both.values()) == 19
    with pytest.raises(AssertionError):
        audited_skip_guard.documented_skip_inventory(
            audit.replace("[mapping_revoked]", "[invented_gate]"),
            windows_symlink_unavailable=True,
            dingtalk_postgres_integration_enabled=True,
        )
