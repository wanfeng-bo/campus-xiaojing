from __future__ import annotations

import argparse
import json
import logging
import sys
from collections.abc import Sequence
from typing import cast

from redis.exceptions import RedisError

from app.core.logging import configure_safe_dingtalk_logging
from app.core.redis import get_redis_client
from app.core.settings import Settings, get_settings
from app.integrations.dingtalk.errors import DingTalkUnavailableError
from app.integrations.dingtalk.token_provider import DingTalkAppTokenProvider, TokenRedisClient
from app.platform.identity.login_maintenance import (
    DingTalkLoginMaintenanceStore,
    LoginMaintenanceConflictError,
    LoginMaintenanceRedisClient,
    LoginMaintenanceStateError,
    LoginMaintenanceStatus,
    is_valid_operation_id,
    normalize_maintenance_reason,
)

logger = logging.getLogger(__name__)


def build_argument_parser() -> argparse.ArgumentParser:
    """创建钉钉登录受控维护命令参数，不提供 HTTP 管理入口。"""

    parser = argparse.ArgumentParser(description="管理钉钉登录维护状态和应用 Token 缓存。")
    subparsers = parser.add_subparsers(dest="command", required=True)

    invalidate = subparsers.add_parser("invalidate-token")
    invalidate.add_argument("--confirm-environment", required=True)
    invalidate.add_argument("--reason", required=True)

    pause = subparsers.add_parser("pause")
    pause.add_argument("--confirm-environment", required=True)
    pause.add_argument("--reason", required=True)
    pause.add_argument("--operation-id")

    status_parser = subparsers.add_parser("status")
    status_parser.add_argument("--confirm-environment", required=True)

    resume = subparsers.add_parser("resume")
    resume.add_argument("--confirm-environment", required=True)
    resume.add_argument("--operation-id", required=True)
    resume.add_argument("--reason", required=True)
    return parser


def build_token_provider(redis: object, settings: Settings) -> DingTalkAppTokenProvider:
    """使用 Task 1 的同一 Redis 刷新锁构造 Token 提供者，不接触教师会话键。"""

    return DingTalkAppTokenProvider.from_settings(
        redis=cast(TokenRedisClient, redis),
        settings=settings,
    )


def main(argv: Sequence[str] | None = None) -> int:
    """执行受控 Token 或维护租约命令；任何失败均返回非零退出码。"""

    configure_safe_dingtalk_logging()
    parser = build_argument_parser()
    args = parser.parse_args(argv)
    settings = get_settings()
    if settings.identity_provider != "dingtalk":
        parser.error("当前环境未启用钉钉身份提供者。")
    if args.confirm_environment != settings.app_env:
        parser.error("确认环境与当前 APP_ENV 不一致。")
    reason = _validate_reason(args, parser)
    _validate_operation_id(args, parser)

    try:
        redis = get_redis_client()
        if args.command == "invalidate-token":
            build_token_provider(redis, settings).invalidate()
            _log_result(args.command, settings.app_env, "success", reason=reason)
            print("钉钉应用 Token 缓存已失效。")
            return 0

        store = DingTalkLoginMaintenanceStore(
            redis=cast(LoginMaintenanceRedisClient, redis),
            app_env=settings.app_env,
        )
        if args.command == "pause":
            operation_id = store.pause(reason=reason, operation_id=args.operation_id)
            _log_result(args.command, settings.app_env, "success", operation_id, reason)
            print(operation_id)
            return 0
        if args.command == "status":
            maintenance_status = store.status()
            _log_result(args.command, settings.app_env, "success")
            if maintenance_status is None:
                print("未暂停")
            else:
                print(json.dumps(_build_status_output(maintenance_status), ensure_ascii=False))
            return 0
        if args.command == "resume":
            if not store.resume(args.operation_id):
                _log_result(args.command, settings.app_env, "conflict", args.operation_id, reason)
                print("登录维护租约不存在或操作标识不匹配。", file=sys.stderr)
                return 1
            _log_result(args.command, settings.app_env, "success", args.operation_id, reason)
            print("钉钉登录维护租约已解除。")
            return 0
    except (
        LoginMaintenanceConflictError,
        LoginMaintenanceStateError,
        DingTalkUnavailableError,
    ) as error:
        _log_result(
            args.command,
            settings.app_env,
            "failed",
            getattr(args, "operation_id", None),
            reason,
        )
        print(f"钉钉登录维护操作失败：{error}", file=sys.stderr)
        return 1
    except RedisError:
        _log_result(
            args.command,
            settings.app_env,
            "failed",
            getattr(args, "operation_id", None),
            reason,
        )
        print("钉钉登录维护操作失败：Redis 暂时不可用。", file=sys.stderr)
        return 1
    raise RuntimeError("未识别的钉钉登录维护命令。")


def _validate_reason(args: argparse.Namespace, parser: argparse.ArgumentParser) -> str:
    """规范化带原因命令的审计文本，并拒绝换行和超长输入。"""

    if not hasattr(args, "reason"):
        return ""
    try:
        return normalize_maintenance_reason(args.reason)
    except ValueError:
        parser.error("操作原因必须为 1 至 200 个字符，且不能包含换行。")
    raise AssertionError("argparse.error 应终止无效维护原因处理。")


def _validate_operation_id(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    """在连接 Redis 或记录日志前拒绝外部传入的异常维护操作标识。"""

    operation_id = getattr(args, "operation_id", None)
    if operation_id is not None and not is_valid_operation_id(operation_id):
        parser.error("操作标识格式无效。")


def _build_status_output(status: LoginMaintenanceStatus) -> dict[str, object]:
    """构造不含解除维护能力标识和内部到期时间的状态命令公开输出。"""

    return {
        "paused": True,
        "reason": status.reason,
        "paused_at": status.paused_at,
        "ttl_seconds": status.ttl_seconds,
    }


def _log_result(
    command: str,
    app_env: str,
    result: str,
    operation_id: str | None = None,
    reason: str = "",
) -> None:
    """记录固定脱敏字段；自由原因仅记录是否提供，正文继续只用于维护状态。"""

    logger.info(
        "dingtalk_login_maintenance "
        "command=%s app_env=%s result=%s operation_id=%s reason_provided=%s",
        command,
        app_env,
        result,
        operation_id or "-",
        str(bool(reason)).lower(),
    )


if __name__ == "__main__":
    raise SystemExit(main())
