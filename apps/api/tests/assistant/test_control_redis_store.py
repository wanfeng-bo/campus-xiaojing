from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from importlib import import_module
from threading import Lock
from typing import Any, cast
from uuid import uuid4

import pytest

from app.core.redis import get_redis_client


class _ScriptRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.calls: list[tuple[str, int, tuple[object, ...]]] = []
        self.lock = Lock()
        self.now_epoch = 100

    def get(self, key: str) -> str | None:
        with self.lock:
            return self.values.get(key)

    def eval(self, script: str, numkeys: int, *values: object) -> int:
        self.calls.append((script, numkeys, values))
        with self.lock:
            if numkeys == 1 and len(values) == 4 and values[3] == "consume":
                key, expected_payload, expires_text, operation = cast(
                    tuple[str, str, str, str], values
                )
                assert operation == "consume"
                if self.values.get(key) != expected_payload or self.now_epoch >= int(expires_text):
                    return 0
                del self.values[key]
                return 1
            if numkeys == 1 and len(values) == 3 and values[2] == "discard":
                key, expected_payload, operation = cast(tuple[str, str, str], values)
                assert operation == "discard"
                if self.values.get(key) != expected_payload:
                    return 0
                del self.values[key]
                return 1
            if numkeys == 2:
                old_key, new_key, expected, expires_text, replacement, ttl_text = cast(
                    tuple[str, str, str, str, str, str], values
                )
                assert int(ttl_text) > 0
                if (
                    self.values.get(old_key) != expected
                    or self.now_epoch >= int(expires_text)
                    or new_key in self.values
                ):
                    return 0
                self.values[new_key] = replacement
                del self.values[old_key]
                return 1
            key, payload, ttl_text = cast(tuple[str, str, str], values)
            assert int(ttl_text) > 0
            if key in self.values:
                return 0
            self.values[key] = payload
            return 1


def _store_type() -> type[Any]:
    try:
        module = import_module("app.composition.assistant_controls")
    except ModuleNotFoundError:
        pytest.fail("K6 Redis control credential adapter is missing")
    return cast(type[Any], module.RedisControlCredentialStore)


def test_corrupt_legacy_supersession_value_cannot_delete_an_unrelated_shared_key() -> None:
    redis = _ScriptRedis()
    redis.values["legacy-supersession-pointer"] = "shared-unrelated-key"
    redis.values["shared-unrelated-key"] = "SHARED-CANARY"
    store = _store_type()(redis=redis)

    assert store.put(
        credential_ref="record-new",
        payload="payload-new",
        ttl_seconds=300,
    )

    assert redis.values["legacy-supersession-pointer"] == "shared-unrelated-key"
    assert redis.values["shared-unrelated-key"] == "SHARED-CANARY"
    assert store.get(credential_ref="record-new") == "payload-new"
    assert all(numkeys == 1 for _script, numkeys, _values in redis.calls)


def test_redis_adapter_atomically_consumes_original_and_prebuilds_reconciliation() -> None:
    redis = _ScriptRedis()
    store = _store_type()(redis=redis)
    assert store.put(
        credential_ref="record-old",
        payload="payload-old",
        ttl_seconds=300,
    )

    with ThreadPoolExecutor(max_workers=2) as executor:
        winners = list(
            executor.map(
                lambda _index: store.consume_and_put(
                    credential_ref="record-old",
                    expected_payload="payload-old",
                    expires_at=1_000,
                    replacement_credential_ref="record-check",
                    replacement_payload="payload-check",
                    ttl_seconds=300,
                ),
                range(2),
            )
        )

    assert sorted(winners) == [False, True]
    assert store.get(credential_ref="record-old") is None
    assert store.get(credential_ref="record-check") == "payload-check"
    assert all("lock" not in script.casefold() for script, _count, _values in redis.calls)


def test_atomic_consume_rejects_exact_expiry_without_deleting_or_replacing() -> None:
    redis = _ScriptRedis()
    redis.values["record-old"] = "payload-old"
    store = _store_type()(redis=redis)

    assert not store.consume(
        credential_ref="record-old",
        expected_payload="payload-old",
        expires_at=100,
    )
    assert redis.values["record-old"] == "payload-old"
    assert not store.consume_and_put(
        credential_ref="record-old",
        expected_payload="payload-old",
        expires_at=100,
        replacement_credential_ref="record-check",
        replacement_payload="payload-check",
        ttl_seconds=300,
    )
    assert redis.values == {"record-old": "payload-old"}


def test_real_redis_lua_persists_across_adapter_instances_is_atomic_and_auto_expires() -> None:
    redis = get_redis_client()
    prefix = f"assistant-control:k6-real:{uuid4().hex}"
    old_key = f"{prefix}:old"
    replacement_key = f"{prefix}:replacement"
    expiring_key = f"{prefix}:expiring"
    boundary_key = f"{prefix}:boundary"
    boundary_replacement_key = f"{prefix}:boundary-replacement"
    first = _store_type()(redis=redis)
    second = _store_type()(redis=redis)
    try:
        assert first.put(
            credential_ref=old_key,
            payload="payload-old",
            ttl_seconds=300,
        )
        assert second.get(credential_ref=old_key) == "payload-old"
        assert redis.ttl(old_key) in {299, 300}

        with ThreadPoolExecutor(max_workers=8) as executor:
            winners = list(
                executor.map(
                    lambda _index: second.consume_and_put(
                        credential_ref=old_key,
                        expected_payload="payload-old",
                        expires_at=int(cast(Any, redis.time())[0]) + 300,
                        replacement_credential_ref=replacement_key,
                        replacement_payload="payload-check",
                        ttl_seconds=300,
                    ),
                    range(8),
                )
            )
        assert winners.count(True) == 1
        assert winners.count(False) == 7
        assert first.get(credential_ref=old_key) is None
        assert first.get(credential_ref=replacement_key) == "payload-check"
        assert redis.ttl(replacement_key) in {299, 300}

        assert first.put(
            credential_ref=boundary_key,
            payload="payload-boundary",
            ttl_seconds=300,
        )
        boundary_epoch = int(cast(Any, redis.time())[0])
        assert not second.consume(
            credential_ref=boundary_key,
            expected_payload="payload-boundary",
            expires_at=boundary_epoch,
        )
        assert second.get(credential_ref=boundary_key) == "payload-boundary"
        assert not second.consume_and_put(
            credential_ref=boundary_key,
            expected_payload="payload-boundary",
            expires_at=boundary_epoch,
            replacement_credential_ref=boundary_replacement_key,
            replacement_payload="payload-boundary-check",
            ttl_seconds=300,
        )
        assert second.get(credential_ref=boundary_key) == "payload-boundary"
        assert second.get(credential_ref=boundary_replacement_key) is None

        assert first.put(
            credential_ref=expiring_key,
            payload="payload-expiring",
            ttl_seconds=1,
        )
        deadline = time.monotonic() + 3
        while redis.exists(expiring_key) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert redis.exists(expiring_key) == 0
    finally:
        redis.delete(
            old_key,
            replacement_key,
            expiring_key,
            boundary_key,
            boundary_replacement_key,
        )
