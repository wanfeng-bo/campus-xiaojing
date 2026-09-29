from __future__ import annotations

import logging
import re
from time import perf_counter

from app.platform.identity.router import _record_exchange_result


def test_identity_exchange_log_exposes_safe_duration_fields(caplog) -> None:
    """身份交换日志必须可从容器文本日志解析，且不包含凭证字段。"""

    with caplog.at_level(logging.INFO, logger="app.platform.identity.router"):
        _record_exchange_result(
            request_id="request-safe-001",
            started_at=perf_counter() - 0.05,
            result="success",
        )

    message = caplog.records[-1].getMessage()
    assert message.startswith(
        "identity_exchange_completed "
        "request_id=request-safe-001 "
        "identity_provider=dingtalk "
        "result=success "
    )
    assert re.search(r"duration_ms=\d+\.\d{3}$", message)
    assert "auth_code" not in message
    assert "token" not in message
