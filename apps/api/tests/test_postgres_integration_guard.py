from __future__ import annotations

import ast
import importlib
import os
import subprocess
import sys
from pathlib import Path

import pytest

API_ROOT = Path(__file__).resolve().parents[1]
PROBE_PATH = API_ROOT / "tests" / "postgres_guard_probe.py"


def run_guard_probe(
    *,
    app_env: str,
    database_name: str,
    probe_name: str = "test_postgres_guard_probe",
) -> subprocess.CompletedProcess[str]:
    """在隔离子进程中运行安全门探针，不连接或修改 PostgreSQL。"""

    environment = {
        **os.environ,
        "APP_ENV": app_env,
        "POSTGRES_HOST": "127.0.0.1",
        "POSTGRES_PORT": "5432",
        "POSTGRES_DB": database_name,
        "POSTGRES_USER": "guard-test",
        "POSTGRES_PASSWORD": "not-a-real-password",
        "REDIS_HOST": "127.0.0.1",
        "REDIS_PORT": "6379",
        "REDIS_PASSWORD": "not-a-real-password",
        "IDENTITY_PROVIDER": "local_test",
        "RUN_POSTGRES_INTEGRATION": "1",
    }
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", f"{PROBE_PATH}::{probe_name}"],
        cwd=API_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize(
    ("app_env", "database_name"),
    [
        ("development", "campus_ai_guard_test"),
        ("test", "campus_ai"),
    ],
)
def test_integration_guard_rejects_unsafe_database(
    app_env: str,
    database_name: str,
) -> None:
    result = run_guard_probe(app_env=app_env, database_name=database_name)

    assert result.returncode != 0
    assert "PostgreSQL 集成测试拒绝使用非隔离数据库" in result.stdout + result.stderr


def test_integration_guard_accepts_test_environment_and_database() -> None:
    result = run_guard_probe(app_env="test", database_name="campus_ai_guard_test")

    assert result.returncode == 0, result.stdout + result.stderr


def test_integration_guard_rejects_unsafe_database_for_unmarked_probe() -> None:
    result = run_guard_probe(
        app_env="development",
        database_name="campus_ai_guard_test",
        probe_name="test_postgres_guard_unmarked_probe",
    )

    assert result.returncode != 0
    assert "PostgreSQL 集成测试拒绝使用非隔离数据库" in result.stdout + result.stderr


@pytest.mark.parametrize(
    ("module_name", "helper_name"),
    [
        ("tests.test_authorization_admin_api", "prepare_admin_api_database"),
        ("tests.test_authorization_service", "prepare_access_test_database"),
    ],
)
def test_authorization_setup_uses_only_the_central_isolated_database(
    module_name: str,
    helper_name: str,
) -> None:
    """授权测试不得恢复私有 schema reset，只能消费中央隔离库。"""

    module = importlib.import_module(module_name)
    module_path = Path(str(module.__file__))
    tree = ast.parse(module_path.read_text(encoding="utf-8"), filename=str(module_path))
    helper = next(
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == helper_name
    )
    assert [argument.arg for argument in helper.args.args] == ["postgres_roles"]

    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == helper_name
    ]
    assert calls
    assert all(
        len(call.args) == 1
        and not call.keywords
        and isinstance(call.args[0], ast.Name)
        and call.args[0].id == "isolated_migration_schema"
        for call in calls
    )

    imported_modules = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imported_symbols = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }
    assert "subprocess" not in imported_modules
    assert "alembic" not in imported_modules
    assert {"get_settings", "get_migration_settings"}.isdisjoint(imported_symbols)


def test_f1_schema_fixture_is_explicit_and_reuses_disposable_cluster_guards() -> None:
    import inspect

    from tests.master_data import conftest as fixtures

    assert hasattr(fixtures, "school_facts_f1_template")
    assert hasattr(fixtures, "school_facts_f1_schema")
    old = inspect.signature(fixtures.current_master_data_template)
    assert old.parameters["schema_revision"].default == "20260824_20"
    current = inspect.signature(fixtures.current_master_data_schema)
    assert current.parameters["schema_contract"].default == "identity-v20"
    assert "20260905_22" in inspect.getsource(fixtures.school_facts_f1_template)
    assert "school-facts-f1-v22" in inspect.getsource(fixtures.school_facts_f1_schema)
    assert "verify_disposable_cluster()" in inspect.getsource(fixtures.current_master_data_template)
    assert "verify_disposable_cluster()" in inspect.getsource(fixtures.current_master_data_schema)
    root = (API_ROOT / "tests" / "conftest.py").read_text(encoding="utf-8")
    assert "school_facts_f1_template = pytest.fixture" in root
    assert "school_facts_f1_schema = pytest.fixture" in root
