import ast
import importlib.util
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from tests.test_dingtalk_test_deployment_assets import extract_nginx_block

ROOT = Path(__file__).resolve().parents[3]
COMPOSE = ROOT / "infra/docker/compose.identity-test.yml"


def test_identity_compose_has_only_four_default_services():
    assert COMPOSE.is_file(), "阶段1四服务编排尚未实现"
    document = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    services = document["services"]
    assert {name for name, spec in services.items() if not spec.get("profiles")} == {
        "postgres",
        "redis",
        "api",
        "web",
    }
    assert set(services) == {
        "postgres",
        "redis",
        "api",
        "web",
        "role-bootstrap",
        "migration",
        "role-reconcile",
    }
    for name in ("postgres", "redis", "api"):
        assert not services[name].get("ports")
    for name in ("role-bootstrap", "migration", "role-reconcile"):
        assert services[name]["profiles"] == ["init"]
        assert services[name]["restart"] == "no"
    for name in ("postgres", "redis", "api", "web"):
        assert services[name]["restart"] == "unless-stopped"
        assert services[name]["pull_policy"] == "never"
        assert services[name]["logging"]["options"] == {"max-size": "10m", "max-file": "4"}


def test_identity_migration_profile_targets_only_reviewed_revision20():
    services = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))["services"]
    migration = services["migration"]
    assert migration["profiles"] == ["init"]
    assert migration["command"] == [
        "python", "-m", "alembic", "upgrade", "20260824_20"
    ]


def test_school_root_rehearsal_targets_only_reviewed_revision20():
    source = (ROOT / "scripts/identity_school_root_rehearsal.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    upgrade_calls = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "subprocess"
        and node.func.attr == "run"
        and node.args
        and isinstance(node.args[0], ast.List)
        and any(isinstance(arg, ast.Constant) and arg.value == "alembic" for arg in node.args[0].elts)
    ]
    assert len(upgrade_calls) == 1
    assert [arg.value for arg in upgrade_calls[0].args[0].elts[1:]] == [
        "-m", "alembic", "upgrade", "20260824_20"
    ]


def test_identity_network_and_runtime_boundaries():
    assert COMPOSE.is_file()
    document = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    services = document["services"]
    assert set(services["postgres"]["networks"]) == {"runtime-db"}
    assert set(services["redis"]["networks"]) == {"identity-cache"}
    assert set(services["web"]["networks"]) == {"web-api", "edge"}
    assert set(services["api"]["networks"]) == {
        "runtime-db",
        "identity-cache",
        "web-api",
        "api-outbound",
    }
    for name in ("runtime-db", "identity-cache", "web-api"):
        assert document["networks"][name]["internal"] is True
    api = services["api"]
    assert api["user"] == "10001:10001"
    assert api["read_only"] is True
    assert api["cap_drop"] == ["ALL"]
    assert api["security_opt"] == ["no-new-privileges:true"]
    assert not api.get("volumes")
    for spec in services.values():
        assert not spec.get("privileged")
        assert spec.get("network_mode") != "host"
        assert "SYS_ADMIN" not in spec.get("cap_add", [])
        assert "docker.sock" not in str(spec)


def test_identity_public_examples_and_service_secret_ownership():
    def fields(name):
        path = ROOT / name
        assert path.is_file(), name
        return dict(
            line.split("=", 1)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line and not line.startswith("#")
        )

    api = fields(".env.identity-test.example")
    assert api["APP_ENV"] == "production"
    assert api["IDENTITY_PROVIDER"] == "dingtalk"
    assert api["POSTGRES_USER"] == "campus_ai_runtime"
    assert api["POSTGRES_HOST"] == "postgres"
    assert api["REPAIR_DRAFT_MODEL_ENABLED"] == "false"
    assert api["IDENTITY_SESSION_COOKIE_NAME"] == "__Host-campus_ai_hangzhou_identity_test_session"
    assert not any("MIGRATION" in key or "BOOTSTRAP" in key or "WRITER" in key for key in api)
    assert set(fields(".env.identity-test-postgres.example")) == {
        "POSTGRES_DB",
        "POSTGRES_USER",
        "POSTGRES_PASSWORD",
    }
    assert set(fields(".env.identity-test-redis.example")) == {"REDIS_PASSWORD"}
    control = fields(".env.identity-test-control.example")
    assert control["CAMPUS_AI_BIND_IP"] == "127.0.0.1"
    assert not any("PASSWORD" in key or "SECRET" in key for key in control)
    services = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))["services"]
    for name, variable in (
        ("api", "RUNTIME"),
        ("postgres", "POSTGRES"),
        ("redis", "REDIS"),
        ("migration", "MIGRATION"),
        ("role-bootstrap", "ROLE"),
        ("role-reconcile", "ROLE"),
    ):
        assert len(services[name]["env_file"]) == 1
        assert f"CAMPUS_AI_{variable}_ENV_FILE" in services[name]["env_file"][0]
    assert not services["web"].get("env_file")


def test_identity_redis_password_is_streamed_as_escaped_stdin_config():
    redis = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))["services"]["redis"]
    command = "\n".join(redis["command"])

    assert redis["entrypoint"] == ["/bin/sh", "-ec"]
    assert "redis-server -" in command
    assert "--requirepass" not in command
    assert 'if [ -z "$${REDIS_PASSWORD+x}" ] || [ -z "$${REDIS_PASSWORD}" ]' in command
    assert "command -v od" in command
    assert "command -v awk" in command
    assert "set -o pipefail" in command
    assert "od -An -v -tx1" in command
    assert 'printf "\\\\x%s", $$field' in command
    assert 'requirepass "$${redis_password_escaped}"' in command
    assert "appendonly yes" in command
    assert "maxmemory 128mb" in command
    assert "maxmemory-policy noeviction" in command


def test_identity_gateway_admin_b_and_response_headers():
    nginx = (ROOT / "infra/nginx/dingtalk-test.conf.template").read_text(encoding="utf-8")
    for opening in ("location = /api/v1/admin", "location ^~ /api/v1/admin/"):
        assert "return 404;" in extract_nginx_block(nginx, opening)
    assert 'add_header X-Robots-Tag "noindex, nofollow" always;' in nginx
    assert 'add_header Cache-Control "no-store" always;' in nginx
    limited = extract_nginx_block(nginx, "location @dingtalk_login_rate_limited")
    assert 'add_header X-Robots-Tag "noindex, nofollow" always;' in limited
    assert 'add_header Cache-Control "no-store" always;' in limited


def test_identity_page_title_marks_personal_technical_test_site():
    html = (ROOT / "apps/web/index.html").read_text(encoding="utf-8")
    assert "个人技术展示" in html
    assert "测试站" in html


def test_school_root_rehearsal_has_an_explicit_isolated_entry():
    rehearsal = ROOT / "scripts/identity_school_root_rehearsal.py"
    assert rehearsal.is_file(), "专用学校根演练必须具有独立、显式调用的入口"
    regular = ast.parse(Path(__file__).read_text(encoding="utf-8"))
    assert "test_school_root_operation_draft_rehearsal" not in {
        node.name for node in regular.body if isinstance(node, ast.FunctionDef)
    }, "固定目标的破坏性演练不得混入普通CI测试收集"
    source = rehearsal.read_text(encoding="utf-8")
    tree = ast.parse(source)
    entry = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "test_school_root_operation_draft_rehearsal"
    )
    assert [arg.arg for arg in entry.args.args] == ["postgres_roles", "tmp_path"]
    for forbidden in ("pytest.skip", "skipif", "xfail", "collect_ignore", "pytest_plugins"):
        assert forbidden not in source
    assert "ROOT = Path(__file__).resolve().parents[1]" in source


def test_explicit_school_root_rehearsal_preserves_approved_safety_checks():
    rehearsal = ROOT / "scripts/identity_school_root_rehearsal.py"
    assert rehearsal.is_file(), "原操作稿与全部负向场景必须保留为独立演练"
    source = rehearsal.read_text(encoding="utf-8")
    for required in (
        'assert sys.platform == "linux"',
        "roles.verify_disposable_cluster()",
        'assert params["dbname"] == "campus_ai_identity_test"',
        'assert params["host"] == "postgres"',
        "assert draft == runbook.split",
        'Path("/run/school-root-input.json")',
        "assert not input_path.exists()",
        '"9999999999999999999"',
        '"wrong_database_test"',
        "version_num='20260823_19'",
        "zzzz_identity_stage1_fail_root",
        "assert counts() == (0, 0)",
        "assert counts() == (1, 1)",
        "assert run(payload).returncode == 1",
        "assert observed ==",
    ):
        assert required in source


@pytest.mark.parametrize(
    "fault",
    ["platform", "opt_in", "app_env", "host", "port", "dbname", "existing_input"],
)
def test_explicit_rehearsal_rejects_wrong_runner_before_database_fixture(monkeypatch, fault):
    rehearsal = _load_school_root_rehearsal()
    settings = SimpleNamespace(
        app_env="test",
        postgres_host="postgres",
        postgres_port=5432,
        postgres_db="campus_ai_identity_test",
    )
    monkeypatch.setattr(rehearsal.sys, "platform", "win32" if fault == "platform" else "linux")
    monkeypatch.setenv("RUN_POSTGRES_INTEGRATION", "0" if fault == "opt_in" else "1")
    if fault in {"app_env", "host", "port", "dbname"}:
        field, value = {
            "app_env": ("app_env", "production"),
            "host": ("postgres_host", "127.0.0.1"),
            "port": ("postgres_port", 15432),
            "dbname": ("postgres_db", "campus_ai_assistant_test"),
        }[fault]
        setattr(settings, field, value)
    monkeypatch.setattr(rehearsal, "get_settings", lambda: settings)
    monkeypatch.setattr(
        rehearsal, "Path", lambda _path: SimpleNamespace(exists=lambda: fault == "existing_input")
    )

    def reject_database_access():
        pytest.fail("错误演练环境不得先建立数据库fixture")

    monkeypatch.setattr(rehearsal, "build_postgres_roles", reject_database_access)
    with pytest.raises(AssertionError):
        rehearsal.postgres_roles.__wrapped__()


def test_explicit_rehearsal_allows_only_the_checked_runner(monkeypatch):
    rehearsal = _load_school_root_rehearsal()
    monkeypatch.setattr(rehearsal.sys, "platform", "linux")
    monkeypatch.setenv("RUN_POSTGRES_INTEGRATION", "1")
    monkeypatch.setattr(
        rehearsal,
        "get_settings",
        lambda: SimpleNamespace(
            app_env="test",
            postgres_host="postgres",
            postgres_port=5432,
            postgres_db="campus_ai_identity_test",
        ),
    )
    monkeypatch.setattr(rehearsal, "Path", lambda _path: SimpleNamespace(exists=lambda: False))
    calls = []
    sentinel = object()

    def build():
        calls.append("bootstrap")
        return sentinel

    monkeypatch.setattr(rehearsal, "build_postgres_roles", build)
    assert rehearsal.postgres_roles.__wrapped__() is sentinel
    assert calls == ["bootstrap"]


def test_school_root_draft_copies_are_identical():
    plan = (
        ROOT / "docs/superpowers/plans/2026-09-05-杭州阿里云当前版本测试部署实施计划.md"
    ).read_text(encoding="utf-8")
    runbook = (ROOT / "docs/runbooks/钉钉测试企业HTTPS部署.md").read_text(encoding="utf-8")
    assert (
        plan.split("<<'PY'\n", 1)[1].split("\nPY\n", 1)[0]
        == runbook.split("<<'PY'\n", 1)[1].split("\nPY\n", 1)[0]
    )


def _load_school_root_rehearsal():
    path = ROOT / "scripts/identity_school_root_rehearsal.py"
    assert path.is_file(), "独立演练入口尚未建立"
    spec = importlib.util.spec_from_file_location("identity_school_root_rehearsal_check", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_identity_deployment_uses_fixed_code_session_ttl(monkeypatch):
    from dotenv import dotenv_values
    from pydantic import ValidationError

    from app.core.settings import Settings

    values = dotenv_values(ROOT / ".env.identity-test.example")
    assert "IDENTITY_SESSION_TTL_SECONDS" not in values
    for key in tuple(os.environ):
        monkeypatch.delenv(key, raising=False)
    for key, value in values.items():
        assert value is not None
        monkeypatch.setenv(key, value)
    assert Settings(_env_file=None).identity_session_ttl_seconds == 28800
    with pytest.raises(ValidationError) as failure:
        Settings(_env_file=None, identity_session_ttl_seconds=3600)
    assert any(
        error["loc"] == ("identity_session_ttl_seconds",) and error["type"] == "literal_error"
        for error in failure.value.errors()
    )


def test_identity_health_uncaught_fault_does_not_disclose_details(monkeypatch):
    from fastapi.testclient import TestClient

    import app.main as application

    sentinel = "private-db-role-anchor-system-path-secret-sentinel"

    def fail():
        raise RuntimeError(sentinel)

    monkeypatch.setattr(application, "check_database_connection", fail)
    # No lifespan: this is an injected request-time failure, not a bypass of startup.
    client = TestClient(application.app, raise_server_exceptions=False)
    response = client.get("/api/v1/health")
    assert response.status_code == 500
    assert response.text == "Internal Server Error"
    assert sentinel not in response.text
