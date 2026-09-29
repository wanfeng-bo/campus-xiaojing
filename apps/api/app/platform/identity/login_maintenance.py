from __future__ import annotations

import json
import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final, Protocol

from redis.exceptions import RedisError

LOGIN_MAINTENANCE_TTL_SECONDS: Final = 1_800
LOGIN_MAINTENANCE_TTL_TOLERANCE_SECONDS: Final = 1
OPERATION_ID_BYTES: Final = 24
OPERATION_ID_PATTERN: Final = re.compile(r"[A-Za-z0-9_-]{32}\Z")

_RENEW_LOGIN_MAINTENANCE_SCRIPT: Final = """
local raw_status = redis.call('get', KEYS[1])
if not raw_status then
    return 0
end
local current_status = cjson.decode(raw_status)
if current_status['operation_id'] ~= ARGV[1] then
    return 0
end
local new_status = cjson.decode(ARGV[2])
new_status['paused_at'] = current_status['paused_at']
redis.call('set', KEYS[1], cjson.encode(new_status), 'XX', 'EX', ARGV[3])
return 1
"""

_READ_LOGIN_MAINTENANCE_SCRIPT: Final = """
local raw_status = redis.call('get', KEYS[1])
if not raw_status then
    return {false, -2}
end
return {raw_status, redis.call('ttl', KEYS[1])}
"""

_RESUME_LOGIN_MAINTENANCE_SCRIPT: Final = """
local raw_status = redis.call('get', KEYS[1])
if not raw_status then
    return 0
end
local current_status = cjson.decode(raw_status)
if current_status['operation_id'] ~= ARGV[1] then
    return 0
end
return redis.call('del', KEYS[1])
"""


class LoginMaintenanceConflictError(Exception):
    """表示操作标识不匹配或租约已结束，禁止接管他人的维护窗口。"""


class LoginMaintenanceStateError(Exception):
    """表示 Redis 中的维护租约损坏或过期，调用方不得误报为未暂停。"""


class LoginMaintenanceRedisClient(Protocol):
    """定义登录维护租约所需的最小 Redis 原子读写接口。"""

    def set(
        self,
        key: str,
        value: str,
        *,
        nx: bool = False,
        xx: bool = False,
        ex: int,
    ) -> bool | None:
        """以 NX 或 XX 条件写入带正数 TTL 的文本值。"""

        ...

    def get(self, key: str) -> str | None:
        """读取文本维护状态。"""

        ...

    def ttl(self, key: str) -> int:
        """读取键的 Redis 剩余有效秒数。"""

        ...

    def eval(self, script: str, numkeys: int, *keys_and_args: object) -> object:
        """运行操作标识比较后的固定 Lua 脚本。"""

        ...


@dataclass(frozen=True)
class LoginMaintenanceStatus:
    """表示不含秘密的登录维护租约。"""

    paused_at: str
    expires_at: str
    reason: str
    operation_id: str
    ttl_seconds: int


def build_maintenance_key(app_env: str) -> str:
    """生成按环境隔离的钉钉登录维护键。"""

    return f"identity:dingtalk:login:maintenance:{app_env}"


def is_valid_operation_id(operation_id: object) -> bool:
    """校验服务器生成的固定 Base64URL 操作标识格式，拒绝控制字符和日志注入。"""

    return (
        isinstance(operation_id, str) and OPERATION_ID_PATTERN.fullmatch(operation_id) is not None
    )


def normalize_maintenance_reason(reason: object) -> str:
    """规范化维护原因并拒绝空白、超长或含换行的非安全审计文本。"""

    if not isinstance(reason, str):
        raise ValueError("维护原因必须是文本。")
    if "\r" in reason or "\n" in reason:
        raise ValueError("维护原因必须为 1 至 200 个字符，且不能包含换行。")
    normalized_reason = reason.strip()
    if not 1 <= len(normalized_reason) <= 200:
        raise ValueError("维护原因必须为 1 至 200 个字符，且不能包含换行。")
    return normalized_reason


def _generate_operation_id() -> str:
    """生成固定 24 字节、无填充的 Base64URL 维护操作标识。"""

    return secrets.token_urlsafe(OPERATION_ID_BYTES)


class DingTalkLoginMaintenanceStore:
    """以固定 30 分钟 Redis 租约协调钉钉登录维护，不保存凭证或会话。"""

    def __init__(
        self,
        *,
        redis: LoginMaintenanceRedisClient,
        app_env: str,
        now: Callable[[], datetime] | None = None,
        operation_id_factory: Callable[[], str] = _generate_operation_id,
    ) -> None:
        """绑定文本 Redis、环境隔离键和可注入时钟，租约不涉及身份会话。"""

        self._redis = redis
        self.key = build_maintenance_key(app_env)
        self._now = now or (lambda: datetime.now(UTC))
        self._operation_id_factory = operation_id_factory

    def pause(self, *, reason: str, operation_id: str | None = None) -> str:
        """创建或续期 30 分钟租约；续期保留最初开始时间并更新当前操作原因。"""

        active_operation_id = (
            operation_id if operation_id is not None else self._operation_id_factory()
        )
        if not is_valid_operation_id(active_operation_id):
            raise ValueError("维护操作标识格式无效。")
        normalized_reason = normalize_maintenance_reason(reason)
        payload = self._build_payload(
            reason=normalized_reason,
            operation_id=active_operation_id,
        )
        try:
            if operation_id is None:
                created = self._redis.set(
                    self.key,
                    payload,
                    nx=True,
                    ex=LOGIN_MAINTENANCE_TTL_SECONDS,
                )
                if created:
                    return active_operation_id
            else:
                renewed = self._redis.eval(
                    _RENEW_LOGIN_MAINTENANCE_SCRIPT,
                    1,
                    self.key,
                    active_operation_id,
                    payload,
                    LOGIN_MAINTENANCE_TTL_SECONDS,
                )
                if renewed == 1:
                    return active_operation_id
        except RedisError:
            raise LoginMaintenanceStateError("登录维护租约无法安全写入。") from None
        raise LoginMaintenanceConflictError("当前登录维护租约属于其他操作，不能续期或覆盖。")

    def status(self) -> LoginMaintenanceStatus | None:
        """从单个 Lua 快照读取有效租约；缺失以外的异常状态均明确失败。"""

        try:
            snapshot = self._redis.eval(_READ_LOGIN_MAINTENANCE_SCRIPT, 1, self.key)
        except RedisError:
            raise LoginMaintenanceStateError("登录维护租约无法读取。") from None
        if not isinstance(snapshot, (list, tuple)) or len(snapshot) != 2:
            raise LoginMaintenanceStateError("登录维护租约快照已损坏。")
        raw_status, ttl_seconds = snapshot
        if raw_status is None and ttl_seconds == -2:
            return None
        if not isinstance(raw_status, str) or not isinstance(ttl_seconds, int):
            raise LoginMaintenanceStateError("登录维护租约快照已损坏。")
        if ttl_seconds < 1:
            raise LoginMaintenanceStateError("登录维护租约已损坏或失去有效期。")
        return self._decode_status(
            raw_status,
            ttl_seconds=ttl_seconds,
            now=self._now().astimezone(UTC),
        )

    def resume(self, operation_id: str) -> bool:
        """仅在操作标识匹配时原子删除维护租约，绝不预读后无条件删除。"""

        if not is_valid_operation_id(operation_id):
            raise ValueError("维护操作标识格式无效。")
        try:
            resumed = self._redis.eval(
                _RESUME_LOGIN_MAINTENANCE_SCRIPT,
                1,
                self.key,
                operation_id,
            )
        except RedisError:
            raise LoginMaintenanceStateError("登录维护租约无法安全解除。") from None
        return resumed == 1

    def _build_payload(self, *, reason: str, operation_id: str) -> str:
        """构造仅含时间、原因和操作标识的 JSON，明确排除应用凭证与 Token。"""

        paused_at = self._now().astimezone(UTC)
        expires_at = paused_at + timedelta(seconds=LOGIN_MAINTENANCE_TTL_SECONDS)
        payload = {
            "paused_at": paused_at.isoformat(),
            "expires_at": expires_at.isoformat(),
            "reason": reason,
            "operation_id": operation_id,
        }
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def _decode_status(
        raw_status: str,
        *,
        ttl_seconds: int,
        now: datetime,
    ) -> LoginMaintenanceStatus:
        """严格校验 Redis JSON 字段，避免损坏值被解释为未暂停或可继续操作。"""

        try:
            payload = json.loads(raw_status)
            if not isinstance(payload, dict) or set(payload) != {
                "paused_at",
                "expires_at",
                "reason",
                "operation_id",
            }:
                raise ValueError
            status = LoginMaintenanceStatus(
                paused_at=DingTalkLoginMaintenanceStore._required_text(payload, "paused_at"),
                expires_at=DingTalkLoginMaintenanceStore._required_text(payload, "expires_at"),
                reason=normalize_maintenance_reason(payload["reason"]),
                operation_id=DingTalkLoginMaintenanceStore._required_text(payload, "operation_id"),
                ttl_seconds=ttl_seconds,
            )
            if not is_valid_operation_id(status.operation_id):
                raise ValueError
            paused_at = datetime.fromisoformat(status.paused_at)
            expires_at = datetime.fromisoformat(status.expires_at)
            if (
                paused_at.tzinfo is None
                or expires_at.tzinfo is None
                or paused_at.utcoffset() != timedelta(0)
                or expires_at.utcoffset() != timedelta(0)
                or expires_at <= paused_at
                or abs((expires_at - now).total_seconds() - ttl_seconds)
                > LOGIN_MAINTENANCE_TTL_TOLERANCE_SECONDS
            ):
                raise ValueError
        except (TypeError, ValueError, json.JSONDecodeError):
            raise LoginMaintenanceStateError("登录维护租约内容已损坏。") from None
        return status

    @staticmethod
    def _required_text(payload: dict[str, object], field: str) -> str:
        """读取不可为空的 JSON 文本字段，拒绝类型伪造和空维护元数据。"""

        value = payload[field]
        if not isinstance(value, str) or not value:
            raise ValueError
        return value
