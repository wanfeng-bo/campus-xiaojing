from __future__ import annotations

import logging
from typing import Final

SAFE_DINGTALK_LOGGERS: Final = (
    "app.platform.identity.router",
    "app.integrations.dingtalk.token_provider",
    "app.platform.identity.login_guard",
    "app.platform.identity.manage_dingtalk_login",
)
SAFE_DINGTALK_LOG_FORMAT: Final = "%(levelname)s:%(name)s:%(message)s"


class _SafeDingTalkLogHandler(logging.StreamHandler):
    """标记由本模块管理的安全 stderr handler，供幂等配置复用。"""


class _ExactDingTalkLoggerFilter(logging.Filter):
    """只允许四个批准 logger 的精确名称，拒绝后代或相似名称传播。"""

    def filter(self, record: logging.LogRecord) -> bool:
        """按 LogRecord 原始 logger 名称执行精确白名单判断。"""

        return record.name in SAFE_DINGTALK_LOGGERS


def configure_safe_dingtalk_logging() -> None:
    """仅为批准的钉钉运行日志启用 INFO，并以固定无上下文格式写入 stderr。

    调用会替换四个精确 logger 的既有 handler、关闭向 root 传播并保持 root/app
    命名空间级别不变，避免 Uvicorn 或调用方已有 handler 造成重复或附加敏感上下文。
    """

    handler = _find_existing_handler()
    if handler is None:
        handler = _SafeDingTalkLogHandler()
    handler.setLevel(logging.INFO)
    handler.setFormatter(logging.Formatter(SAFE_DINGTALK_LOG_FORMAT))
    handler.filters.clear()
    handler.addFilter(_ExactDingTalkLoggerFilter())

    for logger_name in SAFE_DINGTALK_LOGGERS:
        logger = logging.getLogger(logger_name)
        logger.handlers = [handler]
        logger.setLevel(logging.INFO)
        logger.propagate = False
        logger.disabled = False


def _find_existing_handler() -> _SafeDingTalkLogHandler | None:
    """查找任一批准 logger 已安装的安全 handler，避免重复创建。"""

    for logger_name in SAFE_DINGTALK_LOGGERS:
        for handler in logging.getLogger(logger_name).handlers:
            if isinstance(handler, _SafeDingTalkLogHandler):
                return handler
    return None
