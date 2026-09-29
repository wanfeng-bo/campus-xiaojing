from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
API_ROOT = PROJECT_ROOT / "apps/api"
APPROVED_LOGGERS = (
    "app.platform.identity.router",
    "app.integrations.dingtalk.token_provider",
    "app.platform.identity.login_guard",
    "app.platform.identity.manage_dingtalk_login",
)


def run_isolated_python(source: str) -> subprocess.CompletedProcess[str]:
    """在当前工作树启动无网络 Python 子进程，验证真实 stderr 日志行为。"""

    environment = os.environ.copy()
    inherited_pythonpath = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = os.pathsep.join(
        path for path in (str(API_ROOT), inherited_pythonpath) if path
    )
    environment["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run(
        [sys.executable, "-c", source],
        cwd=API_ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=20,
    )


def test_safe_logging_enables_only_approved_info_without_duplicate() -> None:
    """默认 WARNING 且无 handler 时只输出四个批准 logger，幂等配置不重复。"""

    logger_names = json.dumps(APPROVED_LOGGERS)
    result = run_isolated_python(
        f"""
import json
import logging
import sys

root = logging.getLogger()
root.handlers.clear()
root.setLevel(logging.WARNING)

from app.core.logging import configure_safe_dingtalk_logging

logger_names = {logger_names}
configure_safe_dingtalk_logging()
safe_handler = logging.getLogger(logger_names[0]).handlers[0]
safe_handler.setLevel(logging.CRITICAL)
safe_handler.setFormatter(logging.Formatter("unsafe:%(message)s"))
safe_handler.filters.clear()
configure_safe_dingtalk_logging()
for logger_name in logger_names:
    logging.getLogger(logger_name).info("operational-probe")
for index, logger_name in enumerate(logger_names):
    unapproved_names = (
        f"{{logger_name}}.child",
        f"{{logger_name}}_similar",
        f"{{logger_name}}.cross.{{logger_names[(index + 1) % len(logger_names)]}}",
    )
    for unapproved_name in unapproved_names:
        unapproved_logger = logging.getLogger(unapproved_name)
        unapproved_logger.info(f"unapproved-info-{{index}}")
        if unapproved_name.startswith(f"{{logger_name}}."):
            try:
                raise RuntimeError(f"unapproved-error-{{index}}")
            except RuntimeError:
                unapproved_logger.exception(f"unapproved-exception-{{index}}")

print(json.dumps({{
    "root_level": root.level,
    "app_level": logging.getLogger("app").level,
    "handler_counts": [
        len(logging.getLogger(logger_name).handlers) for logger_name in logger_names
    ],
    "filter_counts": [
        len(logging.getLogger(logger_name).handlers[0].filters)
        for logger_name in logger_names
    ],
}}))
"""
    )

    assert result.returncode == 0, result.stderr
    assert result.stderr.splitlines() == [
        f"INFO:{logger_name}:operational-probe" for logger_name in APPROVED_LOGGERS
    ]
    assert "unapproved" not in result.stderr
    metadata = json.loads(result.stdout)
    assert metadata == {
        "root_level": 30,
        "app_level": 0,
        "handler_counts": [1, 1, 1, 1],
        "filter_counts": [1, 1, 1, 1],
    }


def test_safe_logging_does_not_inherit_existing_root_handler_context() -> None:
    """已有 root handler 的合成上下文字段不得附加到批准 logger 输出。"""

    result = run_isolated_python(
        """
import logging
import sys

root = logging.getLogger()
root.handlers.clear()
root.setLevel(logging.WARNING)
unsafe_handler = logging.StreamHandler(sys.stderr)
unsafe_handler.setFormatter(
    logging.Formatter("root-secret=synthetic-secret-marker client=198.51.100.24 %(message)s")
)
root.addHandler(unsafe_handler)

from app.core.logging import configure_safe_dingtalk_logging

configure_safe_dingtalk_logging()
logging.getLogger("app.platform.identity.router").info("safe-context-probe")
"""
    )

    assert result.returncode == 0, result.stderr
    assert result.stderr == "INFO:app.platform.identity.router:safe-context-probe\n"
    assert "synthetic-secret-marker" not in result.stderr
    assert "198.51.100.24" not in result.stderr


def test_api_import_path_configures_safe_operational_logging() -> None:
    """正常导入 FastAPI 启动入口时必须真实启用批准的 INFO stderr 输出。"""

    result = run_isolated_python(
        """
import logging

root = logging.getLogger()
root.handlers.clear()
root.setLevel(logging.WARNING)

import app.main

logging.getLogger("app.platform.identity.router").info("api-startup-probe")
"""
    )

    assert result.returncode == 0, result.stderr
    assert result.stderr == "INFO:app.platform.identity.router:api-startup-probe\n"


def test_optional_mobile_supplier_failure_does_not_log_response_body() -> None:
    """电话查询降级时不得把供应商正文或完整号码写入 stdout/stderr。"""

    result = run_isolated_python(
        """
import httpx

from app.core.settings import Settings
from app.integrations.dingtalk.client import DingTalkIdentityVerifier

class CachedTokenProvider:
    def get(self, _fetcher):
        return "temporary-token"

settings = Settings.model_validate({
    "app_env": "test",
    "postgres_host": "127.0.0.1",
    "postgres_port": 5432,
    "postgres_db": "campus_ai_test",
    "postgres_user": "tester",
    "postgres_password": "not-a-real-password",
    "rls_context_signing_key": "test-runtime-context-signing-key-32-bytes",
    "redis_host": "127.0.0.1",
    "redis_port": 6379,
    "redis_password": "not-a-real-redis-password",
    "identity_provider": "dingtalk",
    "dingtalk_corp_id": "ding-test-corp",
    "dingtalk_app_key": "test-app-key",
    "dingtalk_app_secret": "test-app-secret",
})

def handler(_request):
    return httpx.Response(
        200,
        json={
            "errcode": 60011,
            "errmsg": "no permission for 13800000000",
            "result": {"mobile": "13800000000"},
        },
    )

verifier = DingTalkIdentityVerifier(
    settings=settings,
    token_provider=CachedTokenProvider(),
    http_transport=httpx.MockTransport(handler),
)
assert verifier.fetch_optional_mobile("teacher-001") is None
"""
    )

    assert result.returncode == 0, result.stderr
    assert "13800000000" not in result.stdout
    assert "13800000000" not in result.stderr


def test_repair_draft_model_failure_does_not_log_request_response_or_secret() -> None:
    """报修模型失败只暴露固定异常，不把原话、供应商正文或密钥写入输出。"""

    result = run_isolated_python(
        """
import httpx

from app.core.settings import Settings
from app.domains.repairs.ports import RepairDraftModelUnavailableError
from app.integrations.deepseek.client import DeepSeekRepairDraftClient

settings = Settings.model_validate({
    "app_env": "test",
    "postgres_host": "127.0.0.1",
    "postgres_port": 5432,
    "postgres_db": "campus_ai_test",
    "postgres_user": "tester",
    "postgres_password": "not-a-real-password",
    "rls_context_signing_key": "test-runtime-context-signing-key-32-bytes",
    "redis_host": "127.0.0.1",
    "redis_port": 6379,
    "redis_password": "not-a-real-redis-password",
    "repair_draft_model_enabled": True,
    "repair_draft_model_service_name": "synthetic-service",
    "repair_draft_model_base_url": "https://model.example",
    "repair_draft_model_model": "current-json-model",
    "repair_draft_model_api_key": "provider-secret-marker",
})

def handler(_request):
    return httpx.Response(503, text="provider-response-marker-13900000000")

client = DeepSeekRepairDraftClient(
    settings=settings,
    http_transport=httpx.MockTransport(handler),
)
try:
    client.interpret(
        raw_text="raw-request-marker-13800000000",
        continuation=None,
    )
except RepairDraftModelUnavailableError as error:
    print(str(error))
"""
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == "报修文字整理暂时不可用，请直接填写地点和问题。\n"
    combined_output = result.stdout + result.stderr
    assert "raw-request-marker-13800000000" not in combined_output
    assert "provider-response-marker-13900000000" not in combined_output
    assert "provider-secret-marker" not in combined_output


def test_direct_maintenance_cli_configures_and_emits_safe_audit_log() -> None:
    """直接运行受控 CLI 时必须在首条审计 INFO 前自行加载同一安全配置。"""

    result = run_isolated_python(
        """
import logging
from types import SimpleNamespace

root = logging.getLogger()
root.handlers.clear()
root.setLevel(logging.WARNING)

from app.platform.identity import manage_dingtalk_login as command

class FakeProvider:
    def invalidate(self):
        pass

command.get_settings = lambda: SimpleNamespace(
    identity_provider="dingtalk",
    app_env="test",
)
command.get_redis_client = lambda: object()
command.build_token_provider = lambda *_args: FakeProvider()
exit_code = command.main([
    "invalidate-token",
    "--confirm-environment",
    "test",
    "--reason",
    "reason-secret-marker-198.51.100.24",
])
raise SystemExit(exit_code)
"""
    )

    assert result.returncode == 0, result.stderr
    assert result.stderr == (
        "INFO:app.platform.identity.manage_dingtalk_login:"
        "dingtalk_login_maintenance command=invalidate-token app_env=test "
        "result=success operation_id=- reason_provided=true\n"
    )
    assert "reason-secret-marker-198.51.100.24" not in result.stderr
    assert result.stdout == "钉钉应用 Token 缓存已失效。\n"


def test_direct_maintenance_cli_conflict_does_not_log_reason_text() -> None:
    """CLI 冲突审计保留操作标识和结果，但不得写入自由原因正文。"""

    result = run_isolated_python(
        """
import logging
from types import SimpleNamespace

root = logging.getLogger()
root.handlers.clear()
root.setLevel(logging.WARNING)

from app.platform.identity import manage_dingtalk_login as command

class FakeStore:
    def __init__(self, *_args, **_kwargs):
        pass

    def resume(self, _operation_id):
        return False

command.get_settings = lambda: SimpleNamespace(
    identity_provider="dingtalk",
    app_env="test",
)
command.get_redis_client = lambda: object()
command.DingTalkLoginMaintenanceStore = FakeStore
exit_code = command.main([
    "resume",
    "--confirm-environment",
    "test",
    "--operation-id",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "--reason",
    "conflict-reason-secret-marker-198.51.100.25",
])
raise SystemExit(exit_code)
"""
    )

    assert result.returncode == 1
    assert result.stderr.splitlines()[0] == (
        "INFO:app.platform.identity.manage_dingtalk_login:"
        "dingtalk_login_maintenance command=resume app_env=test "
        "result=conflict operation_id=AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA "
        "reason_provided=true"
    )
    assert "conflict-reason-secret-marker-198.51.100.25" not in result.stderr


def test_direct_maintenance_cli_failure_does_not_log_reason_text() -> None:
    """CLI 失败审计保留固定结果字段，但不得写入自由原因正文。"""

    result = run_isolated_python(
        """
import logging
from types import SimpleNamespace

from redis.exceptions import RedisError

root = logging.getLogger()
root.handlers.clear()
root.setLevel(logging.WARNING)

from app.platform.identity import manage_dingtalk_login as command

class FakeProvider:
    def invalidate(self):
        raise RedisError("controlled failure")

command.get_settings = lambda: SimpleNamespace(
    identity_provider="dingtalk",
    app_env="test",
)
command.get_redis_client = lambda: object()
command.build_token_provider = lambda *_args: FakeProvider()
exit_code = command.main([
    "invalidate-token",
    "--confirm-environment",
    "test",
    "--reason",
    "failure-reason-secret-marker-198.51.100.26",
])
raise SystemExit(exit_code)
"""
    )

    assert result.returncode == 1
    assert result.stderr.splitlines()[0] == (
        "INFO:app.platform.identity.manage_dingtalk_login:"
        "dingtalk_login_maintenance command=invalidate-token app_env=test "
        "result=failed operation_id=- reason_provided=true"
    )
    assert "failure-reason-secret-marker-198.51.100.26" not in result.stderr


def test_direct_maintenance_status_logs_reason_absence_as_boolean() -> None:
    """无 reason 的 status 审计只记录固定 false，不生成自由文本占位。"""

    result = run_isolated_python(
        """
import logging
from types import SimpleNamespace

root = logging.getLogger()
root.handlers.clear()
root.setLevel(logging.WARNING)

from app.platform.identity import manage_dingtalk_login as command

class FakeStore:
    def __init__(self, *_args, **_kwargs):
        pass

    def status(self):
        return None

command.get_settings = lambda: SimpleNamespace(
    identity_provider="dingtalk",
    app_env="test",
)
command.get_redis_client = lambda: object()
command.DingTalkLoginMaintenanceStore = FakeStore
exit_code = command.main([
    "status",
    "--confirm-environment",
    "test",
])
raise SystemExit(exit_code)
"""
    )

    assert result.returncode == 0, result.stderr
    assert result.stderr == (
        "INFO:app.platform.identity.manage_dingtalk_login:"
        "dingtalk_login_maintenance command=status app_env=test "
        "result=success operation_id=- reason_provided=false\n"
    )
    assert result.stdout == "未暂停\n"
