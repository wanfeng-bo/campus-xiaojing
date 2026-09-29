from __future__ import annotations

import asyncio
import importlib.util
import io
import json
import sys
from collections.abc import Coroutine
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any

import httpx
import pytest


@pytest.fixture
def capacity_runner() -> ModuleType:
    """从测试负载目录加载发流器，避免安装到生产应用包。"""

    module_path = (
        Path(__file__).resolve().parents[3] / "tests" / "load" / "dingtalk_capacity_runner.py"
    )
    specification = importlib.util.spec_from_file_location("dingtalk_capacity_runner", module_path)
    assert specification is not None
    assert specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    try:
        specification.loader.exec_module(module)
    finally:
        sys.modules.pop(specification.name, None)
    return module


class FakeClock:
    """以独立 wall/monotonic 值驱动发流时序测试。"""

    def __init__(self, wall: datetime) -> None:
        self.wall = wall
        self.monotonic_value = 0.0

    def utc_now(self) -> datetime:
        return self.wall

    def monotonic(self) -> float:
        return self.monotonic_value

    async def sleep(self, seconds: float) -> None:
        self.monotonic_value += seconds
        self.wall += timedelta(seconds=seconds)
        await asyncio.sleep(0)


class FakeSender:
    """记录固定控制调用与请求，不包含真实网络行为。"""

    def __init__(self, runner: ModuleType, *, statuses: list[int] | None = None) -> None:
        self.runner = runner
        self.statuses = statuses or []
        self.reset_calls = 0
        self.reset_modes: list[str] = []
        self.metric_calls = 0
        self.auto_metrics = True
        self.codes: list[str] = []
        self.targets: list[str] = []
        self.metrics_payload: dict[str, object] = {
            "exchange_results": {"success": 1},
            "exchange_duration_ms": [401.0],
            "token_events": {},
            "guard_results": {"allowed": 1},
            "max_concurrency": 1,
            "external_token_fetches": 0,
        }

    async def reset(self, mode: str) -> dict[str, object]:
        self.reset_calls += 1
        self.reset_modes.append(mode)
        return {"reset": True, "mode": mode, "deleted": 0}

    async def metrics(self) -> dict[str, object]:
        self.metric_calls += 1
        payload = dict(self.metrics_payload)
        if self.auto_metrics:
            payload["exchange_results"] = {"success": len(self.codes)}
            payload["exchange_duration_ms"] = [401.0] * len(self.codes)
            payload["guard_results"] = {"allowed": len(self.codes)}
        return payload

    async def __call__(self, target: str, code: str) -> object:
        self.targets.append(target)
        self.codes.append(code)
        status = self.statuses.pop(0) if self.statuses else 200
        return self.runner.RequestResult(status, 401.0, 0.0)


def successful_results(runner: ModuleType, count: int, *, start_skew_ms: float = 0) -> list[object]:
    """构造不含敏感内容的成功请求样本。"""

    return [
        runner.RequestResult(200, 400.0, index * start_skew_ms / max(count - 1, 1) / 1000)
        for index in range(count)
    ]


def test_profile_specs_are_exactly_frozen(capacity_runner: ModuleType) -> None:
    """仅允许设计确认的十二个容量场景，且不可变更其关键字段。"""

    assert tuple(capacity_runner.PROFILE_SPECS) == (
        "rate-warmup-10",
        "rate-warmup-30",
        "rate-warmup-60",
        "concurrency-warmup-5",
        "concurrency-warmup-10",
        "concurrency-warmup-20",
        "rate-100",
        "concurrency-30",
        "rate-overload-121",
        "concurrency-overload-31",
        "gateway-overload-40",
        "cold-cache-30",
    )
    assert tuple(
        (spec.mode, spec.target_requests, spec.duration_seconds, spec.target)
        for spec in capacity_runner.PROFILE_SPECS.values()
    ) == (
        ("rate", 10, 5.0, "gateway"),
        ("rate", 30, 15.0, "gateway"),
        ("rate", 60, 30.0, "gateway"),
        ("barrier", 5, 0.0, "gateway"),
        ("barrier", 10, 0.0, "gateway"),
        ("barrier", 20, 0.0, "gateway"),
        ("rate", 100, 50.0, "gateway"),
        ("barrier", 30, 0.0, "gateway"),
        ("rate", 121, 30.25, "api"),
        ("barrier", 31, 0.0, "api"),
        ("barrier", 40, 0.0, "gateway"),
        ("barrier", 30, 0.0, "api"),
    )
    with pytest.raises((AttributeError, TypeError)):
        capacity_runner.PROFILE_SPECS["rate-100"].target_requests = 101


def test_rate_100_schedule_is_inside_fifty_seconds(capacity_runner: ModuleType) -> None:
    """一分钟一百次发流必须在五十秒窗口内均匀启动。"""

    offsets = capacity_runner.build_rate_offsets(capacity_runner.PROFILE_SPECS["rate-100"])

    assert len(offsets) == 100
    assert offsets[0] == 0
    assert offsets[-1] < 50


def test_nearest_rank_uses_documented_boundaries(capacity_runner: ModuleType) -> None:
    """百分位严格使用 nearest-rank 并拒绝空样本和非法分位。"""

    assert capacity_runner.nearest_rank((8.0, 1.0, 3.0, 5.0), 0.5) == 3.0
    assert capacity_runner.nearest_rank((8.0, 1.0, 3.0, 5.0), 0.95) == 8.0
    with pytest.raises(ValueError):
        capacity_runner.nearest_rank((), 0.95)
    with pytest.raises(ValueError):
        capacity_runner.nearest_rank((1.0,), 1.01)


def test_target_and_protection_results_are_not_conflated(capacity_runner: ModuleType) -> None:
    """目标容量与受控过载的 429 必须用不同契约判定。"""

    target = capacity_runner.summarize_profile(
        capacity_runner.PROFILE_SPECS["concurrency-30"],
        successful_results(capacity_runner, 30, start_skew_ms=80),
        {
            "max_guard_concurrency": 30,
            "exchange_results": {"success": 30},
            "guard_events": {"allowed": 30},
        },
    )
    overload = capacity_runner.summarize_profile(
        capacity_runner.PROFILE_SPECS["concurrency-overload-31"],
        successful_results(capacity_runner, 30) + [capacity_runner.RequestResult(429, 10.0, 0.0)],
        {
            "max_guard_concurrency": 30,
            "exchange_results": {"success": 30, "rate_limited": 1},
            "guard_events": {"allowed": 30, "concurrency_limited": 1},
        },
    )

    assert target["passed"] is True
    assert overload["passed"] is True
    assert overload["expected_rejections"] == 1
    assert target["unexpected_failures"] == 0


def test_summary_rejects_exchange_metrics_that_do_not_match_http_results(
    capacity_runner: ModuleType,
) -> None:
    """HTTP 全成功不能掩盖 Task 1 exchange 聚合缺失或额外结果。"""

    report = capacity_runner.summarize_profile(
        capacity_runner.PROFILE_SPECS["rate-100"],
        successful_results(capacity_runner, 100),
        {
            "max_guard_concurrency": 1,
            "exchange_results": {"success": 1},
            "token_events": {},
            "guard_events": {"allowed": 100},
        },
    )

    assert report["passed"] is False

    accepted = capacity_runner.summarize_profile(
        capacity_runner.PROFILE_SPECS["gateway-overload-40"],
        successful_results(capacity_runner, 39)
        + [capacity_runner.RequestResult(429, 10.0, 0.0, retry_after_seconds=2)],
        {
            "max_guard_concurrency": 30,
            "exchange_results": {"success": 39},
            "token_events": {},
            "guard_events": {"allowed": 39},
        },
    )

    assert accepted["passed"] is True


def test_application_overload_requires_matching_guard_source(capacity_runner: ModuleType) -> None:
    """应用过载的 HTTP 429 必须由对应登录门事件佐证。"""

    report = capacity_runner.summarize_profile(
        capacity_runner.PROFILE_SPECS["rate-overload-121"],
        successful_results(capacity_runner, 120) + [capacity_runner.RequestResult(429, 10.0, 0.0)],
        {
            "max_guard_concurrency": 1,
            "exchange_results": {"success": 120, "rate_limited": 1},
            "token_events": {},
            "guard_events": {"allowed": 120, "concurrency_limited": 1},
        },
    )

    assert report["passed"] is False


def test_gateway_overload_requires_fixed_retry_after(capacity_runner: ModuleType) -> None:
    """网关保护场景的每个受控拒绝都必须公开固定 Retry-After 两秒。"""

    report = capacity_runner.summarize_profile(
        capacity_runner.PROFILE_SPECS["gateway-overload-40"],
        successful_results(capacity_runner, 39) + [capacity_runner.RequestResult(429, 10.0, 0.0)],
        {
            "max_guard_concurrency": 30,
            "exchange_results": {"success": 39},
            "token_events": {},
            "guard_events": {"allowed": 39},
        },
    )

    assert report["passed"] is False


def test_gateway_overload_accepts_mixed_gateway_and_application_rejections(
    capacity_runner: ModuleType,
) -> None:
    """网关至少拒绝一次时，应用并发门可同时产生有佐证的受控拒绝。"""

    report = capacity_runner.summarize_profile(
        capacity_runner.PROFILE_SPECS["gateway-overload-40"],
        successful_results(capacity_runner, 30)
        + [capacity_runner.RequestResult(429, 10.0, 0.0, retry_after_seconds=2) for _ in range(10)],
        {
            "max_guard_concurrency": 30,
            "exchange_results": {"success": 30, "rate_limited": 1},
            "token_events": {"hit": 30},
            "guard_events": {"allowed": 30, "concurrency_limited": 1},
        },
    )

    assert report["expected_rejections"] == 10
    assert report["passed"] is True


def test_gateway_overload_rejects_application_only_protection(
    capacity_runner: ModuleType,
) -> None:
    """四十次请求必须至少有一次由网关拒绝，不能只验证应用并发门。"""

    report = capacity_runner.summarize_profile(
        capacity_runner.PROFILE_SPECS["gateway-overload-40"],
        successful_results(capacity_runner, 30)
        + [capacity_runner.RequestResult(429, 10.0, 0.0, retry_after_seconds=2) for _ in range(10)],
        {
            "max_guard_concurrency": 30,
            "exchange_results": {"success": 30, "rate_limited": 10},
            "token_events": {"hit": 30},
            "guard_events": {"allowed": 30, "concurrency_limited": 10},
        },
    )

    assert report["passed"] is False


@pytest.mark.parametrize(
    ("profile", "metrics"),
    (
        (
            "concurrency-overload-31",
            {
                "max_guard_concurrency": 30,
                "exchange_results": {"success": 30, "rate_limited": 1},
                "token_events": {},
                "guard_events": {"allowed": 30, "concurrency_limited": 1},
            },
        ),
        (
            "gateway-overload-40",
            {
                "max_guard_concurrency": 30,
                "exchange_results": {"success": 39},
                "token_events": {},
                "guard_events": {"allowed": 39},
            },
        ),
    ),
)
def test_overload_barriers_reject_start_skew_over_one_hundred_ms(
    capacity_runner: ModuleType,
    profile: str,
    metrics: dict[str, object],
) -> None:
    """应用和网关过载也必须满足全部 worker 的统一屏障启动偏差门。"""

    success_count = 30 if profile == "concurrency-overload-31" else 39
    retry_after = None if profile == "concurrency-overload-31" else 2
    results = successful_results(capacity_runner, success_count, start_skew_ms=101)
    results.append(capacity_runner.RequestResult(429, 10.0, 0.0, retry_after_seconds=retry_after))

    report = capacity_runner.summarize_profile(
        capacity_runner.PROFILE_SPECS[profile],
        results,
        metrics,
    )

    assert report["start_skew_ms"] == 101
    assert report["passed"] is False


@pytest.mark.parametrize("profile", ("concurrency-overload-31", "gateway-overload-40"))
def test_overload_barriers_require_every_start_sample(
    capacity_runner: ModuleType,
    profile: str,
) -> None:
    """过载 HTTP 结果齐全时也不能缺少任一 sender 前启动样本。"""

    if profile == "concurrency-overload-31":
        results = successful_results(capacity_runner, 30)
        metrics = {
            "max_guard_concurrency": 30,
            "exchange_results": {"success": 30, "rate_limited": 1},
            "token_events": {},
            "guard_events": {"allowed": 30, "concurrency_limited": 1},
        }
        retry_after = None
    else:
        results = successful_results(capacity_runner, 39)
        metrics = {
            "max_guard_concurrency": 30,
            "exchange_results": {"success": 39},
            "token_events": {},
            "guard_events": {"allowed": 39},
        }
        retry_after = 2
    results.append(
        capacity_runner.RequestResult(
            429,
            10.0,
            None,
            retry_after_seconds=retry_after,
        )
    )

    report = capacity_runner.summarize_profile(
        capacity_runner.PROFILE_SPECS[profile],
        results,
        metrics,
    )

    assert report["passed"] is False


@pytest.mark.parametrize("hit_count", (30, 45, 59))
def test_cold_cache_accepts_real_provider_hit_range(
    capacity_runner: ModuleType,
    hit_count: int,
) -> None:
    """电话详情带来三十次必然命中，首轮后到请求至多再带来二十九次。"""

    token_events = {"miss": 1, "refresh_success": 1, "hit": hit_count}

    report = capacity_runner.summarize_profile(
        capacity_runner.PROFILE_SPECS["cold-cache-30"],
        successful_results(capacity_runner, 30, start_skew_ms=90),
        {
            "max_guard_concurrency": 30,
            "exchange_results": {"success": 30},
            "token_events": token_events,
            "external_token_fetches": 1,
            "guard_events": {"allowed": 30},
        },
    )

    assert report["passed"] is True
    assert report["external_token_fetches"] == 1


def test_cold_cache_accepts_explicit_zero_anomaly_counters(
    capacity_runner: ModuleType,
) -> None:
    """失败和协调异常事件可显式存在，但计数必须全部为零。"""

    report = capacity_runner.summarize_profile(
        capacity_runner.PROFILE_SPECS["cold-cache-30"],
        successful_results(capacity_runner, 30, start_skew_ms=90),
        {
            "max_guard_concurrency": 30,
            "exchange_results": {"success": 30},
            "token_events": {
                "hit": 30,
                "miss": 1,
                "refresh_success": 1,
                "refresh_failed": 0,
                "wait_timeout": 0,
                "cache_invalid": 0,
                "lock_expired": 0,
                "invalidated": 0,
            },
            "external_token_fetches": 1,
            "guard_events": {"allowed": 30},
        },
    )

    assert report["passed"] is True


@pytest.mark.parametrize(
    "extra_event",
    ("refresh_failed", "wait_timeout", "lock_expired", "cache_invalid", "invalidated"),
)
def test_cold_cache_rejects_nonzero_anomaly_event(
    capacity_runner: ModuleType,
    extra_event: str,
) -> None:
    """任一失败或协调异常事件出现非零计数时必须失败关闭。"""

    report = capacity_runner.summarize_profile(
        capacity_runner.PROFILE_SPECS["cold-cache-30"],
        successful_results(capacity_runner, 30, start_skew_ms=90),
        {
            "max_guard_concurrency": 30,
            "exchange_results": {"success": 30},
            "token_events": {
                "hit": 30,
                "miss": 1,
                "refresh_success": 1,
                extra_event: 1,
            },
            "external_token_fetches": 1,
            "guard_events": {"allowed": 30},
        },
    )

    assert report["passed"] is False


def test_cold_cache_rejects_hit_above_concurrency_bound(
    capacity_runner: ModuleType,
) -> None:
    """两次取令牌的三十请求至多产生五十九次缓存命中。"""

    report = capacity_runner.summarize_profile(
        capacity_runner.PROFILE_SPECS["cold-cache-30"],
        successful_results(capacity_runner, 30, start_skew_ms=90),
        {
            "max_guard_concurrency": 30,
            "exchange_results": {"success": 30},
            "token_events": {"miss": 1, "refresh_success": 1, "hit": 60},
            "external_token_fetches": 1,
            "guard_events": {"allowed": 30},
        },
    )

    assert report["passed"] is False


def test_cold_cache_rejects_hit_below_second_lookup_floor(
    capacity_runner: ModuleType,
) -> None:
    """每个成功登录都必须为用户详情再取一次缓存令牌，hit 不得低于三十。"""

    report = capacity_runner.summarize_profile(
        capacity_runner.PROFILE_SPECS["cold-cache-30"],
        successful_results(capacity_runner, 30, start_skew_ms=90),
        {
            "max_guard_concurrency": 30,
            "exchange_results": {"success": 30},
            "token_events": {"miss": 1, "refresh_success": 1, "hit": 29},
            "external_token_fetches": 1,
            "guard_events": {"allowed": 30},
        },
    )

    assert report["passed"] is False


@pytest.mark.parametrize("invalid_hit", (True, -1, 1.0))
def test_cold_cache_rejects_non_integer_or_negative_hit(
    capacity_runner: ModuleType,
    invalid_hit: object,
) -> None:
    """hit 必须是非 bool 的非负整数。"""

    with pytest.raises(ValueError):
        capacity_runner.summarize_profile(
            capacity_runner.PROFILE_SPECS["cold-cache-30"],
            successful_results(capacity_runner, 30, start_skew_ms=90),
            {
                "max_guard_concurrency": 30,
                "exchange_results": {"success": 30},
                "token_events": {
                    "miss": 1,
                    "refresh_success": 1,
                    "hit": invalid_hit,
                },
                "external_token_fetches": 1,
                "guard_events": {"allowed": 30},
            },
        )


def test_cold_cache_rejects_unknown_token_event(capacity_runner: ModuleType) -> None:
    """指标归一化必须拒绝未声明的 Token 事件键。"""

    with pytest.raises(ValueError):
        capacity_runner.summarize_profile(
            capacity_runner.PROFILE_SPECS["cold-cache-30"],
            successful_results(capacity_runner, 30, start_skew_ms=90),
            {
                "max_guard_concurrency": 30,
                "exchange_results": {"success": 30},
                "token_events": {
                    "hit": 30,
                    "miss": 1,
                    "refresh_success": 1,
                    "unexpected": 0,
                },
                "external_token_fetches": 1,
                "guard_events": {"allowed": 30},
            },
        )


def test_formal_target_rejects_slow_percentiles(capacity_runner: ModuleType) -> None:
    """正式目标即使全部 200，也必须满足 P95/P99 延迟门槛。"""

    report = capacity_runner.summarize_profile(
        capacity_runner.PROFILE_SPECS["rate-100"],
        [capacity_runner.RequestResult(200, 10_000.0, float(index)) for index in range(100)],
        {"max_guard_concurrency": 1, "exchange_results": {"success": 100}},
    )

    assert report["passed"] is False


@pytest.mark.anyio
async def test_invalid_task_one_metrics_fail_closed(capacity_runner: ModuleType) -> None:
    """Task 1 的事件计数与耗时样本不一致时不得产生通过报告。"""

    sender = FakeSender(capacity_runner)
    sender.auto_metrics = False
    sender.metrics_payload["exchange_results"] = {"success": 2}
    sender.metrics_payload["exchange_duration_ms"] = [401.0]
    clock = FakeClock(datetime(2026, 7, 31, 12, 0, tzinfo=UTC))

    report = await capacity_runner.run_profile(
        capacity_runner.PROFILE_SPECS["concurrency-warmup-5"], sender, clock, clock.sleep
    )

    assert report["stop_reason"] == "control_error"


@pytest.mark.anyio
async def test_rate_is_open_loop_when_first_request_is_slow(capacity_runner: ModuleType) -> None:
    """慢首请求不能阻塞已到期的后续 rate worker。"""

    clock = FakeClock(datetime(2026, 7, 31, 12, 0, tzinfo=UTC))
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    sent: list[str] = []

    class SlowSender(FakeSender):
        async def __call__(self, target: str, code: str) -> object:
            sent.append(code)
            self.codes.append(code)
            if len(sent) == 1:
                first_started.set()
                await release_first.wait()
            return self.runner.RequestResult(200, 400.0, clock.monotonic())

    sender = SlowSender(capacity_runner)
    spec = capacity_runner.ProfileSpec("test-rate", "rate", 3, 0.3, "api")
    task = asyncio.create_task(capacity_runner.run_profile(spec, sender, clock, clock.sleep))
    await first_started.wait()
    for _ in range(3):
        await clock.sleep(0.2)
    assert len(sent) >= 2
    release_first.set()
    report = await task

    assert report["passed"] is True


@pytest.mark.anyio
async def test_rate_waits_for_minute_and_fails_closed_after_rollover(
    capacity_runner: ModuleType,
) -> None:
    """分钟余量不足不发流，reset 后跨桶重试耗尽也不能泄漏请求。"""

    clock = FakeClock(datetime(2026, 7, 31, 12, 0, 59, tzinfo=UTC))
    sender = FakeSender(capacity_runner)
    spec = capacity_runner.ProfileSpec("test-rate", "rate", 1, 59.0, "api")

    report = await capacity_runner.run_profile(spec, sender, clock, clock.sleep)

    assert sender.codes == []
    assert report["passed"] is False
    assert report["stop_reason"] == "minute_rollover"


@pytest.mark.anyio
async def test_rate_fails_closed_when_the_minute_rolls_after_dispatch(
    capacity_runner: ModuleType,
) -> None:
    """rate 场景结束时跨入下一分钟，不能把两分钟结果汇总为一次通过。"""

    class RolloverSender(FakeSender):
        async def __call__(self, target: str, code: str) -> object:
            result = await super().__call__(target, code)
            clock.wall += timedelta(minutes=1)
            return result

    clock = FakeClock(datetime(2026, 7, 31, 12, 0, tzinfo=UTC))
    sender = RolloverSender(capacity_runner)
    report = await capacity_runner.run_profile(
        capacity_runner.PROFILE_SPECS["rate-warmup-10"], sender, clock, clock.sleep
    )

    assert report["passed"] is False
    assert report["stop_reason"] == "minute_rollover"
    assert report["attempted"] == 1
    assert report["succeeded"] == 1
    assert report["status_counts"] == {"200": 1}


@pytest.mark.anyio
async def test_rate_rollover_after_all_workers_start_keeps_all_results(
    capacity_runner: ModuleType,
) -> None:
    """结束检查发现跨桶时，必须保留已全部启动并完成的真实 HTTP 证据。"""

    clock = FakeClock(datetime(2026, 7, 31, 12, 0, tzinfo=UTC))
    all_started = asyncio.Event()
    release = asyncio.Event()

    class AllStartedRolloverSender(FakeSender):
        async def __call__(self, target: str, code: str) -> object:
            self.targets.append(target)
            self.codes.append(code)
            if len(self.codes) == 3:
                clock.wall += timedelta(minutes=1)
                all_started.set()
                release.set()
            await release.wait()
            return self.runner.RequestResult(200, 401.0, 0.0)

    sender = AllStartedRolloverSender(capacity_runner)
    report = await capacity_runner.run_profile(
        capacity_runner.ProfileSpec("test-rate", "rate", 3, 0.03, "api"),
        sender,
        clock,
        clock.sleep,
    )

    assert all_started.is_set()
    assert report["passed"] is False
    assert report["stop_reason"] == "minute_rollover"
    assert report["attempted"] == 3
    assert report["succeeded"] == 3
    assert report["status_counts"] == {"200": 3}


@pytest.mark.anyio
async def test_rate_scenario_timeout_keeps_started_and_completed_results(
    capacity_runner: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """rate 总场景超时是门失败，并保留已完成与仍在途的真实尝试数。"""

    monkeypatch.setattr(capacity_runner, "EXCHANGE_TIMEOUT_SECONDS", 1.0)
    monkeypatch.setattr(capacity_runner, "SCENARIO_GRACE_SECONDS", 0.5)
    never = asyncio.Event()
    all_started = asyncio.Event()

    class PartlyHangingSender(FakeSender):
        async def __call__(self, target: str, code: str) -> object:
            self.targets.append(target)
            self.codes.append(code)
            if len(self.codes) == 2:
                all_started.set()
            if len(self.codes) == 1:
                return self.runner.RequestResult(200, 401.0, 0.0)
            await never.wait()
            raise AssertionError("unreachable")

    sender = PartlyHangingSender(capacity_runner)
    clock = FakeClock(datetime(2026, 7, 31, 12, 0, tzinfo=UTC))

    report = await capacity_runner.run_profile(
        capacity_runner.ProfileSpec("test-rate", "rate", 2, 0.001, "api"),
        sender,
        clock,
        clock.sleep,
    )

    assert all_started.is_set()
    assert report["stop_reason"] == "rate_timeout"
    assert report["passed"] is False
    assert report["attempted"] == 2
    assert report["succeeded"] == 1
    assert report["status_counts"] == {"200": 1}


@pytest.mark.anyio
async def test_barrier_timeout_and_cancellation_send_no_partial_requests(
    capacity_runner: ModuleType,
) -> None:
    """屏障超时或取消前绝不允许任何 worker 抵达 sender。"""

    clock = FakeClock(datetime(2026, 7, 31, 12, 0, tzinfo=UTC))
    sender = FakeSender(capacity_runner)
    spec = capacity_runner.ProfileSpec("test-barrier", "barrier", 3, 0.0, "api")

    task = asyncio.create_task(capacity_runner.run_profile(spec, sender, clock, clock.sleep))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert sender.codes == []


@pytest.mark.anyio
async def test_barrier_ready_timeout_sends_no_partial_requests(
    capacity_runner: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """固定 ready timeout 在统一释放前取消全部 worker，不能泄漏请求。"""

    async def timeout(awaitable: object, *, timeout: float) -> None:
        assert timeout == capacity_runner.READY_TIMEOUT_SECONDS
        await asyncio.sleep(0)
        close = getattr(awaitable, "close", None)
        if callable(close):
            close()
        raise TimeoutError

    monkeypatch.setattr(capacity_runner.asyncio, "wait_for", timeout)
    sender = FakeSender(capacity_runner)
    clock = FakeClock(datetime(2026, 7, 31, 12, 0, tzinfo=UTC))
    report = await capacity_runner.run_profile(
        capacity_runner.ProfileSpec("test-barrier", "barrier", 3, 0.0, "api"),
        sender,
        clock,
        clock.sleep,
    )

    assert sender.codes == []
    assert report["stop_reason"] == "barrier_timeout"


@pytest.mark.anyio
async def test_barrier_scenario_timeout_keeps_all_started_attempts(
    capacity_runner: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """统一释放后的屏障总超时必须保留所有已进入 sender 的尝试。"""

    monkeypatch.setattr(capacity_runner, "READY_TIMEOUT_SECONDS", 0.25)
    monkeypatch.setattr(capacity_runner, "SCENARIO_GRACE_SECONDS", 0.5)
    monkeypatch.setattr(capacity_runner, "EXCHANGE_TIMEOUT_SECONDS", 1.0)
    never = asyncio.Event()
    all_started = asyncio.Event()

    class HangingSender(FakeSender):
        async def __call__(self, target: str, code: str) -> object:
            self.targets.append(target)
            self.codes.append(code)
            if len(self.codes) == 3:
                all_started.set()
            await never.wait()
            raise AssertionError("unreachable")

    sender = HangingSender(capacity_runner)
    clock = FakeClock(datetime(2026, 7, 31, 12, 0, tzinfo=UTC))

    report = await capacity_runner.run_profile(
        capacity_runner.ProfileSpec("test-barrier", "barrier", 3, 0.0, "api"),
        sender,
        clock,
        clock.sleep,
    )

    assert all_started.is_set()
    assert report["stop_reason"] == "barrier_timeout"
    assert report["passed"] is False
    assert report["attempted"] == 3
    assert report["succeeded"] == 0
    assert report["status_counts"] == {}


@pytest.mark.anyio
async def test_barrier_samples_every_start_before_sender_setup(
    capacity_runner: ModuleType,
) -> None:
    """单个 sender 的同步准备不能串行放大统一屏障的启动偏差。"""

    clock = FakeClock(datetime(2026, 7, 31, 12, 0, tzinfo=UTC))

    class SenderWithSynchronousSetup(FakeSender):
        async def __call__(self, target: str, code: str) -> object:
            self.targets.append(target)
            self.codes.append(code)
            clock.monotonic_value += 0.05
            return self.runner.RequestResult(200, 401.0, 0.0)

    sender = SenderWithSynchronousSetup(capacity_runner)
    sender.metrics_payload["max_concurrency"] = 5

    report = await capacity_runner.run_profile(
        capacity_runner.PROFILE_SPECS["concurrency-warmup-5"],
        sender,
        clock,
        clock.sleep,
    )

    assert len(sender.codes) == 5
    assert report["start_skew_ms"] == 0
    assert report["passed"] is True


@pytest.mark.anyio
async def test_reset_timeout_is_control_error_before_any_request(
    capacity_runner: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """reset 超时属于控制面错误，不能误报为屏障场景超时。"""

    monkeypatch.setattr(capacity_runner, "EXCHANGE_TIMEOUT_SECONDS", 0.01)
    never = asyncio.Event()

    class HangingResetSender(FakeSender):
        async def reset(self, mode: str) -> dict[str, object]:
            self.reset_modes.append(mode)
            await never.wait()
            raise AssertionError("unreachable")

    sender = HangingResetSender(capacity_runner)
    clock = FakeClock(datetime(2026, 7, 31, 12, 0, tzinfo=UTC))

    report = await capacity_runner.run_profile(
        capacity_runner.ProfileSpec("test-barrier", "barrier", 1, 0.0, "api"),
        sender,
        clock,
        clock.sleep,
    )

    assert report["stop_reason"] == "control_error"
    assert report["attempted"] == 0
    assert sender.codes == []


@pytest.mark.anyio
async def test_metrics_timeout_is_control_error_after_requests(
    capacity_runner: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """metrics 超时属于控制面错误，并保留已经完成的请求证据。"""

    monkeypatch.setattr(capacity_runner, "EXCHANGE_TIMEOUT_SECONDS", 0.01)
    never = asyncio.Event()

    class HangingMetricsSender(FakeSender):
        async def metrics(self) -> dict[str, object]:
            self.metric_calls += 1
            await never.wait()
            raise AssertionError("unreachable")

    sender = HangingMetricsSender(capacity_runner)
    clock = FakeClock(datetime(2026, 7, 31, 12, 0, tzinfo=UTC))

    report = await capacity_runner.run_profile(
        capacity_runner.ProfileSpec("test-barrier", "barrier", 2, 0.0, "api"),
        sender,
        clock,
        clock.sleep,
    )

    assert report["stop_reason"] == "control_error"
    assert report["attempted"] == 2
    assert report["succeeded"] == 2
    assert report["status_counts"] == {"200": 2}


@pytest.mark.anyio
async def test_run_profile_uses_reset_and_normalizes_task_one_metrics(
    capacity_runner: ModuleType,
) -> None:
    """场景先验证 reset，并把 Task 1 的 max_concurrency 归一化到报告。"""

    sender = FakeSender(capacity_runner)
    sender.metrics_payload.update(
        {
            "exchange_results": {"success": 5},
            "exchange_duration_ms": [401.0] * 5,
            "max_concurrency": 5,
        }
    )
    clock = FakeClock(datetime(2026, 7, 31, 12, 0, tzinfo=UTC))

    report = await capacity_runner.run_profile(
        capacity_runner.PROFILE_SPECS["concurrency-warmup-5"], sender, clock, clock.sleep
    )

    assert sender.reset_calls == 1
    assert sender.reset_modes == ["profile"]
    assert sender.metric_calls == 1
    assert report["peak_in_flight"] == 5
    assert report["passed"] is True


def test_reset_mode_is_fixed_for_all_profiles(capacity_runner: ModuleType) -> None:
    """仅冷缓存场景可清 Token，其余十一个 profile 必须保留 Token 缓存。"""

    modes = {
        name: capacity_runner._reset_mode_for_profile(spec)
        for name, spec in capacity_runner.PROFILE_SPECS.items()
    }

    assert modes["cold-cache-30"] == "cold_cache"
    assert set(modes.values()) == {"profile", "cold_cache"}
    assert all(mode == "profile" for name, mode in modes.items() if name != "cold-cache-30")


@pytest.mark.anyio
async def test_cold_cache_profile_sends_cold_cache_reset_mode(
    capacity_runner: ModuleType,
) -> None:
    """cold-cache-30 必须把 cold_cache 模式实际传给控制端点。"""

    sender = FakeSender(capacity_runner)
    sender.auto_metrics = False
    sender.metrics_payload = {
        "exchange_results": {"success": 30},
        "exchange_duration_ms": [401.0] * 30,
        "token_events": {"hit": 30, "miss": 1, "refresh_success": 1},
        "guard_results": {"allowed": 30},
        "max_concurrency": 30,
        "external_token_fetches": 1,
    }
    clock = FakeClock(datetime(2026, 7, 31, 12, 0, tzinfo=UTC))

    report = await capacity_runner.run_profile(
        capacity_runner.PROFILE_SPECS["cold-cache-30"], sender, clock, clock.sleep
    )

    assert sender.reset_modes == ["cold_cache"]
    assert report["passed"] is True


@pytest.mark.anyio
async def test_bad_reset_response_is_control_error_without_requests(
    capacity_runner: ModuleType,
) -> None:
    """reset 的 deleted 必须是对应模式范围内的非 bool 整数。"""

    class BadResetSender(FakeSender):
        async def reset(self, mode: str) -> dict[str, object]:
            return {"reset": True, "mode": mode, "deleted": True}

    sender = BadResetSender(capacity_runner)
    clock = FakeClock(datetime(2026, 7, 31, 12, 0, tzinfo=UTC))
    report = await capacity_runner.run_profile(
        capacity_runner.PROFILE_SPECS["concurrency-warmup-5"], sender, clock, clock.sleep
    )

    assert sender.codes == []
    assert report["stop_reason"] == "control_error"


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("mode", "response"),
    (
        ("profile", {"reset": True, "mode": "profile", "deleted": 3}),
        ("cold_cache", {"reset": True, "mode": "cold_cache", "deleted": 5}),
        ("profile", {"reset": True, "mode": "cold_cache", "deleted": 0}),
        ("profile", {"reset": True, "mode": "profile", "deleted": 0, "extra": 1}),
    ),
)
async def test_reset_response_requires_exact_mode_and_deleted_range(
    capacity_runner: ModuleType,
    mode: str,
    response: dict[str, object],
) -> None:
    """reset 回应必须精确回显请求模式，且删除数不能越过该模式固定键数。"""

    class ResetSender(FakeSender):
        async def reset(self, mode: str) -> dict[str, object]:
            assert mode == expected_mode
            return response

    expected_mode = mode
    assert await capacity_runner._reset(ResetSender(capacity_runner), expected_mode) is False


@pytest.mark.anyio
async def test_http_reset_sends_fixed_json_mode(
    capacity_runner: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HTTP reset 必须把模式放入固定 JSON 请求体。"""

    calls: list[tuple[str, str, dict[str, object]]] = []

    class FakeResponse:
        status_code = 200

        @staticmethod
        def json() -> dict[str, object]:
            return {"reset": True, "mode": "cold_cache", "deleted": 0}

    class FakeAsyncClient:
        def __init__(self, *, timeout: float, trust_env: bool) -> None:
            assert timeout == 5.0
            assert trust_env is False

        async def __aenter__(self) -> FakeAsyncClient:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def request(
            self,
            method: str,
            url: str,
            **kwargs: object,
        ) -> FakeResponse:
            calls.append((method, url, kwargs))
            return FakeResponse()

    monkeypatch.setattr(capacity_runner.httpx, "AsyncClient", FakeAsyncClient)

    response = await capacity_runner.HttpCapacitySender().reset("cold_cache")

    assert response == {"reset": True, "mode": "cold_cache", "deleted": 0}
    assert calls == [
        (
            "POST",
            f"{capacity_runner.API_BASE_URL}/__capacity__/reset",
            {
                "headers": {
                    capacity_runner.CAPACITY_CONTROL_HEADER_NAME: (
                        capacity_runner.CAPACITY_CONTROL_HEADER
                    )
                },
                "json": {"mode": "cold_cache"},
            },
        )
    ]


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("status_code", "exception", "expected_event"),
    (
        (503, None, "server_5xx"),
        (401, None, "identity_4xx"),
        (None, httpx.ReadTimeout("token=private"), "transport_timeout"),
    ),
)
async def test_http_sender_emits_only_safe_realtime_event(
    capacity_runner: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    status_code: int | None,
    exception: Exception | None,
    expected_event: str,
) -> None:
    """真实 sender 路径把 5xx/身份4xx/超时写入安全通道且不回显异常。"""

    stream = io.StringIO()
    reporter = capacity_runner.ProgressReporter("rate-warmup-10", stream=stream)

    class FakeResponse:
        headers: dict[str, str] = {}

        def __init__(self, status: int) -> None:
            self.status_code = status

    class FakeAsyncClient:
        def __init__(self, *, timeout: float, trust_env: bool) -> None:
            assert timeout == capacity_runner.HTTP_EXCHANGE_TIMEOUT_SECONDS
            assert trust_env is False

        async def __aenter__(self) -> FakeAsyncClient:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def post(self, _url: str, *, json: dict[str, str]) -> FakeResponse:
            assert json["auth_code"] == "CAPACITY-private"
            if exception is not None:
                raise exception
            assert status_code is not None
            return FakeResponse(status_code)

    monkeypatch.setattr(capacity_runner.httpx, "AsyncClient", FakeAsyncClient)

    await capacity_runner.HttpCapacitySender(reporter)("api", "CAPACITY-private")

    payload = json.loads(
        stream.getvalue().strip().removeprefix(capacity_runner.PROGRESS_LINE_PREFIX)
    )
    assert payload["event"] == expected_event
    assert "private" not in stream.getvalue()


@pytest.mark.anyio
async def test_http_sender_enforces_inner_timeout_before_profile_timeout(
    capacity_runner: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """即使测试客户端忽略 httpx 配置，sender 自身也须先发出传输超时事件。"""

    monkeypatch.setattr(capacity_runner, "HTTP_EXCHANGE_TIMEOUT_SECONDS", 0.01)
    never = asyncio.Event()
    stream = io.StringIO()
    reporter = capacity_runner.ProgressReporter("rate-warmup-10", stream=stream)

    class HangingAsyncClient:
        def __init__(self, *, timeout: float, trust_env: bool) -> None:
            assert timeout == 0.01
            assert trust_env is False

        async def __aenter__(self) -> HangingAsyncClient:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def post(self, _url: str, *, json: dict[str, str]) -> object:
            await never.wait()
            raise AssertionError(json)

    monkeypatch.setattr(capacity_runner.httpx, "AsyncClient", HangingAsyncClient)

    result = await asyncio.wait_for(
        capacity_runner.HttpCapacitySender(reporter)("api", "CAPACITY-private"),
        timeout=0.2,
    )

    assert result.failure_kind == "transport_timeout"
    assert '"event":"transport_timeout"' in stream.getvalue()
    assert "private" not in stream.getvalue()


@pytest.mark.anyio
async def test_token_metrics_timeout_emits_fail_closed_progress(
    capacity_runner: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Token 指标读取阻塞必须在两秒内失败关闭，不能被轮询协程吞掉。"""

    monkeypatch.setattr(
        capacity_runner,
        "PROGRESS_METRICS_TIMEOUT_SECONDS",
        0.01,
        raising=False,
    )
    never = asyncio.Event()
    stream = io.StringIO()
    reporter = capacity_runner.ProgressReporter("rate-warmup-10", stream=stream)

    class HangingMetricsSender(FakeSender):
        async def metrics(self) -> dict[str, object]:
            await never.wait()
            raise AssertionError("unreachable")

    stop = asyncio.Event()
    task = asyncio.create_task(
        capacity_runner._watch_token_progress(
            HangingMetricsSender(capacity_runner),
            reporter,
            stop,
        )
    )
    try:
        await asyncio.sleep(0.05)
        assert '"detail":"metrics_unavailable"' in stream.getvalue()
    finally:
        stop.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.anyio
async def test_run_profile_emits_token_anomaly_from_live_metrics(
    capacity_runner: ModuleType,
) -> None:
    """profile 运行期间的脱敏 Token 异常须进入实时通道，最终报告仍保持固定 JSON。"""

    sender = FakeSender(capacity_runner)
    sender.auto_metrics = False
    sender.metrics_payload = {
        "exchange_results": {"success": 5},
        "exchange_duration_ms": [401.0] * 5,
        "token_events": {"refresh_failed": 1},
        "guard_results": {"allowed": 5},
        "max_concurrency": 5,
        "external_token_fetches": 0,
    }
    stream = io.StringIO()
    reporter = capacity_runner.ProgressReporter("concurrency-warmup-5", stream=stream)
    clock = FakeClock(datetime(2026, 7, 31, 12, 0, tzinfo=UTC))

    report = await capacity_runner.run_profile(
        capacity_runner.PROFILE_SPECS["concurrency-warmup-5"],
        sender,
        clock,
        clock.sleep,
        reporter,
    )

    progress = [
        json.loads(line.removeprefix(capacity_runner.PROGRESS_LINE_PREFIX))
        for line in stream.getvalue().splitlines()
    ]
    assert progress == [
        {
            "detail": "refresh_failed",
            "event": "token_anomaly",
            "profile": "concurrency-warmup-5",
            "schema_version": "dingtalk-capacity-progress-v1",
        }
    ]
    assert report["token_events"] == {"refresh_failed": 1}
    assert set(report) == capacity_runner.REPORT_KEYS


def test_reports_are_whitelisted_and_sensitive_key_scan_is_exact(
    capacity_runner: ModuleType,
) -> None:
    """报告仅含白名单；token_events 合法而精确敏感键必须被拒绝。"""

    report = capacity_runner.summarize_profile(
        capacity_runner.PROFILE_SPECS["rate-warmup-10"],
        successful_results(capacity_runner, 10),
        {
            "max_guard_concurrency": 1,
            "exchange_results": {"success": 10},
            "token_events": {"hit": 1},
            "guard_events": {"allowed": 10},
        },
    )

    assert frozenset(report) == capacity_runner.REPORT_KEYS
    assert capacity_runner.contains_forbidden_report_key(report) is False
    assert capacity_runner.contains_forbidden_report_key({"token": "private"}) is True
    assert capacity_runner.contains_forbidden_report_key({"token_events": {"hit": 1}}) is False
    assert "private" not in json.dumps(report)


def test_cli_exit_codes_are_fixed(
    capacity_runner: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CLI 只区分通过、门失败和参数/控制错误三类退出码。"""

    passed_report = capacity_runner.summarize_profile(
        capacity_runner.PROFILE_SPECS["rate-warmup-10"],
        successful_results(capacity_runner, 10),
        {
            "max_guard_concurrency": 1,
            "exchange_results": {"success": 10},
            "guard_events": {"allowed": 10},
        },
    )
    failed_report = dict(passed_report, passed=False)

    def passing_run(awaitable: Coroutine[Any, Any, object]) -> dict[str, object]:
        awaitable.close()
        return passed_report

    def failing_run(awaitable: Coroutine[Any, Any, object]) -> dict[str, object]:
        awaitable.close()
        return failed_report

    monkeypatch.setattr(capacity_runner.asyncio, "run", passing_run)
    assert capacity_runner.main(["--profile", "rate-warmup-10"]) == 0
    monkeypatch.setattr(capacity_runner.asyncio, "run", failing_run)
    assert capacity_runner.main(["--profile", "rate-warmup-10"]) == 1
    rate_timeout_report = dict(failed_report, stop_reason="rate_timeout")

    def rate_timeout_run(awaitable: Coroutine[Any, Any, object]) -> dict[str, object]:
        awaitable.close()
        return rate_timeout_report

    monkeypatch.setattr(capacity_runner.asyncio, "run", rate_timeout_run)
    assert capacity_runner.main(["--profile", "rate-warmup-10"]) == 1
    control_report = dict(failed_report, stop_reason="control_error")

    def control_run(awaitable: Coroutine[Any, Any, object]) -> dict[str, object]:
        awaitable.close()
        return control_report

    monkeypatch.setattr(capacity_runner.asyncio, "run", control_run)
    assert capacity_runner.main(["--profile", "rate-warmup-10"]) == 2
    assert capacity_runner.main(["--profile", "unknown"]) == 2


def test_cli_argument_error_is_silent_and_does_not_echo_original_value(
    capacity_runner: ModuleType,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """非法参数只返回 2，不得把原始敏感值写入 stdout 或 stderr。"""

    private_value = "token=private-value"

    assert capacity_runner.run_cli(["--profile", private_value]) == 2

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""
    assert private_value not in captured.out
    assert private_value not in captured.err


def test_progress_reporter_emits_only_strict_deduplicated_jsonl(
    capacity_runner: ModuleType,
) -> None:
    """实时通道只写固定四键 JSONL，同一安全事件不得刷屏。"""

    stream = io.StringIO()
    reporter = capacity_runner.ProgressReporter("rate-warmup-10", stream=stream)

    reporter.emit("server_5xx")
    reporter.emit("server_5xx")
    reporter.emit("token_anomaly", detail="refresh_failed")

    lines = stream.getvalue().splitlines()
    assert len(lines) == 2
    assert all(line.startswith(capacity_runner.PROGRESS_LINE_PREFIX) for line in lines)
    payloads = [
        json.loads(line.removeprefix(capacity_runner.PROGRESS_LINE_PREFIX)) for line in lines
    ]
    assert all(
        set(payload) == {"schema_version", "profile", "event", "detail"} for payload in payloads
    )
    assert payloads == [
        {
            "detail": None,
            "event": "server_5xx",
            "profile": "rate-warmup-10",
            "schema_version": "dingtalk-capacity-progress-v1",
        },
        {
            "detail": "refresh_failed",
            "event": "token_anomaly",
            "profile": "rate-warmup-10",
            "schema_version": "dingtalk-capacity-progress-v1",
        },
    ]
    assert all(len(line.encode("ascii")) <= 512 for line in lines)
    assert "token=" not in stream.getvalue()
    assert "request_id=" not in stream.getvalue()


@pytest.mark.parametrize(
    ("result", "expected"),
    (
        ((500, None), ("server_5xx", None)),
        ((503, None), ("server_5xx", None)),
        ((401, None), ("identity_4xx", None)),
        ((422, None), ("identity_4xx", None)),
        ((429, None), None),
        ((200, None), None),
        ((None, "transport_timeout"), ("transport_timeout", None)),
        ((None, "transport_error"), None),
    ),
)
def test_request_progress_classification_is_fixed_and_safe(
    capacity_runner: ModuleType,
    result: tuple[int | None, str | None],
    expected: tuple[str, None] | None,
) -> None:
    """只把 5xx、传输超时和身份 4xx 暴露为白名单即时停止事件。"""

    status_code, failure_kind = result
    request = capacity_runner.RequestResult(
        status_code,
        None,
        None,
        failure_kind=failure_kind,
    )

    assert capacity_runner._request_progress_event(request) == expected


@pytest.mark.parametrize(
    ("events", "expected"),
    (
        ({"hit": 1}, ()),
        ({"miss": 1, "refresh_success": 1}, (("token_refresh", None),)),
        (
            {"miss": 2, "refresh_success": 2},
            (("token_refresh", None), ("token_anomaly", "second_refresh")),
        ),
        ({"refresh_failed": 1}, (("token_anomaly", "refresh_failed"),)),
        ({"wait_timeout": 1}, (("token_anomaly", "wait_timeout"),)),
        ({"cache_invalid": 1}, (("token_anomaly", "cache_invalid"),)),
        ({"lock_expired": 1}, (("token_anomaly", "lock_expired"),)),
        ({"invalidated": 1}, (("token_anomaly", "invalidated"),)),
    ),
)
def test_token_progress_classification_covers_refresh_and_all_anomalies(
    capacity_runner: ModuleType,
    events: dict[str, int],
    expected: tuple[tuple[str, str | None], ...],
) -> None:
    """Token 进度只认固定计数，第二次刷新和五类协调异常立即发出。"""

    assert capacity_runner._token_progress_events(events) == expected
