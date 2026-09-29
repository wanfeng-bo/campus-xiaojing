from __future__ import annotations

from typing import Protocol

_PUT_CONTROL_SCRIPT = """
if redis.call('exists', KEYS[1]) == 1 then
    return 0
end
return redis.call('set', KEYS[1], ARGV[1], 'NX', 'EX', ARGV[2]) and 1 or 0
"""

_CONSUME_CONTROL_SCRIPT = """
local current = redis.call('get', KEYS[1])
local now = redis.call('TIME')
if not current or current ~= ARGV[1] or tonumber(now[1]) >= tonumber(ARGV[2]) then
    return 0
end
redis.call('del', KEYS[1])
return 1
"""

_CONSUME_AND_PUT_CONTROL_SCRIPT = """
local current = redis.call('get', KEYS[1])
local now = redis.call('TIME')
if not current
    or current ~= ARGV[1]
    or tonumber(now[1]) >= tonumber(ARGV[2])
    or redis.call('exists', KEYS[2]) == 1
then
    return 0
end
local created = redis.call('set', KEYS[2], ARGV[3], 'NX', 'EX', ARGV[4])
if not created then
    return 0
end
redis.call('del', KEYS[1])
return 1
"""

_DISCARD_CONTROL_SCRIPT = """
local current = redis.call('get', KEYS[1])
if not current or current ~= ARGV[1] then
    return 0
end
redis.call('del', KEYS[1])
return 1
"""


class ControlRedisClient(Protocol):
    def get(self, key: str) -> object: ...

    def eval(self, script: str, numkeys: int, *values: object) -> object: ...


class RedisControlCredentialStore:
    """使用已有 Redis 文本客户端保存并原子消费短寿命点击凭据。"""

    def __init__(self, *, redis: ControlRedisClient) -> None:
        self._redis = redis

    def put(
        self,
        *,
        credential_ref: str,
        payload: str,
        ttl_seconds: int,
    ) -> bool:
        response = self._redis.eval(
            _PUT_CONTROL_SCRIPT,
            1,
            credential_ref,
            payload,
            str(ttl_seconds),
        )
        return self._strict_boolean_response(response)

    def get(self, *, credential_ref: str) -> str | None:
        value = self._redis.get(credential_ref)
        if value is None or isinstance(value, str):
            return value
        raise RuntimeError("control store returned an invalid value")

    def consume(
        self,
        *,
        credential_ref: str,
        expected_payload: str,
        expires_at: int,
    ) -> bool:
        response = self._redis.eval(
            _CONSUME_CONTROL_SCRIPT,
            1,
            credential_ref,
            expected_payload,
            str(expires_at),
            "consume",
        )
        return self._strict_boolean_response(response)

    def consume_and_put(
        self,
        *,
        credential_ref: str,
        expected_payload: str,
        expires_at: int,
        replacement_credential_ref: str,
        replacement_payload: str,
        ttl_seconds: int,
    ) -> bool:
        response = self._redis.eval(
            _CONSUME_AND_PUT_CONTROL_SCRIPT,
            2,
            credential_ref,
            replacement_credential_ref,
            expected_payload,
            str(expires_at),
            replacement_payload,
            str(ttl_seconds),
        )
        return self._strict_boolean_response(response)

    def discard(self, *, credential_ref: str, expected_payload: str) -> bool:
        response = self._redis.eval(
            _DISCARD_CONTROL_SCRIPT,
            1,
            credential_ref,
            expected_payload,
            "discard",
        )
        return self._strict_boolean_response(response)

    @staticmethod
    def _strict_boolean_response(value: object) -> bool:
        if type(value) is not int or value not in {0, 1}:
            raise RuntimeError("control store returned an invalid response")
        return value == 1


__all__ = ["ControlRedisClient", "RedisControlCredentialStore"]
