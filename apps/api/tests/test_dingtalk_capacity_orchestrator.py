from __future__ import annotations

import importlib.util
import json
import os
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[3]
SCRIPT_PATH = PROJECT_ROOT / "scripts/run_dingtalk_capacity_synthetic.py"


@pytest.fixture
def orchestrator():
    """加载尚未安装为包的宿主编排器脚本。"""

    spec = importlib.util.spec_from_file_location("capacity_orchestrator", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def healthy_snapshot(orchestrator, **changes: object):
    """构造不会触发停止线的最小资源快照。"""

    values: dict[str, object] = {
        "mem_available_ratio": 0.8,
        "all_healthy": True,
        "restart_detected": False,
        "oom_detected": False,
        "redis_blocked_clients": 0,
        "redis_connection_ratio": 0.1,
        "postgres_connection_ratio": 0.1,
        "max_container_cpu_percent": 10.0,
        "max_container_memory_percent": 5.0,
        "container_cpu_percent": {},
        "container_memory_percent": {},
        "concurrency_leases": 0,
    }
    values.update(changes)
    return orchestrator.ResourceSnapshot(**values)


def target_report(**changes: object) -> dict[str, object]:
    """构造通过 Task 2 目标场景的最小脱敏报告。"""

    report: dict[str, object] = {
        "profile": "rate-100",
        "status_counts": {"200": 100},
        "unexpected_failures": 0,
        "p95_ms": 100.0,
        "p99_ms": 100.0,
        "token_events": {"hit": 1},
        "guard_events": {"allowed": 100},
        "passed": True,
    }
    report.update(changes)
    return report


def task2_report(profile: str = "rate-100", **changes: object) -> dict[str, object]:
    """构造严格匹配 Task 2 REPORT_KEYS 的通过报告。"""

    target = {
        "rate-warmup-10": 10,
        "rate-warmup-30": 30,
        "rate-warmup-60": 60,
        "concurrency-warmup-5": 5,
        "concurrency-warmup-10": 10,
        "concurrency-warmup-20": 20,
        "rate-100": 100,
        "concurrency-30": 30,
        "rate-overload-121": 121,
        "concurrency-overload-31": 31,
        "gateway-overload-40": 40,
        "cold-cache-30": 30,
    }[profile]
    report: dict[str, object] = {
        "schema_version": "dingtalk-capacity-v1",
        "profile": profile,
        "target_requests": target,
        "attempted": target,
        "succeeded": target,
        "expected_rejections": 0,
        "unexpected_failures": 0,
        "status_counts": {"200": target},
        "start_skew_ms": 0.0,
        "peak_in_flight": 1,
        "p50_ms": 100.0,
        "p95_ms": 100.0,
        "p99_ms": 100.0,
        "token_events": {"hit": target},
        "guard_events": {"allowed": target},
        "external_token_fetches": 0,
        "passed": True,
        "stop_reason": "completed",
    }
    if profile == "rate-overload-121":
        report.update(
            succeeded=120,
            expected_rejections=1,
            status_counts={"200": 120, "429": 1},
            token_events={"hit": 121},
            guard_events={"allowed": 120, "rate_limited": 1},
        )
    elif profile == "concurrency-overload-31":
        report.update(
            succeeded=30,
            expected_rejections=1,
            status_counts={"200": 30, "429": 1},
            peak_in_flight=30,
            token_events={"hit": 31},
            guard_events={"allowed": 30, "concurrency_limited": 1},
        )
    elif profile == "gateway-overload-40":
        report.update(
            succeeded=30,
            expected_rejections=10,
            status_counts={"200": 30, "429": 10},
            peak_in_flight=40,
            token_events={"hit": 30},
            guard_events={"allowed": 30},
        )
    elif profile == "cold-cache-30":
        report.update(
            peak_in_flight=30,
            token_events={"hit": 30, "miss": 1, "refresh_success": 1},
            guard_events={"allowed": 30},
            external_token_fetches=1,
        )
    elif profile.startswith("concurrency"):
        report["peak_in_flight"] = target
    report.update(changes)
    return report


def suite_report(exit_code: int = 0) -> dict[str, object]:
    """构造严格顶层 schema，用于 CLI/文件安全测试。"""

    passed = exit_code == 0
    return {
        "exit_code": exit_code,
        "schema_version": "dingtalk-capacity-orchestrator-v1",
        "profiles": (
            [
                task2_report(profile)
                for profile in (
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
            ]
            if passed
            else []
        ),
        "resource_peaks": {},
        "stop_reason": "completed" if passed else "preflight_resource_failed",
        "recovery_state": "completed" if passed else "not_started",
        "recovery_duration_seconds": 300.0 if passed else 0.0,
        "public_stack_comparison": "unchanged" if passed else "not_verified",
        "passed": passed,
    }


def test_rate_profile_timeouts_cover_minute_alignment_and_scenario_grace(orchestrator) -> None:
    """rate 外层上限必须覆盖整分钟对齐、场景宽限和控制请求。"""

    durations = {
        "rate-warmup-10": 5.0,
        "rate-warmup-30": 15.0,
        "rate-warmup-60": 30.0,
        "rate-100": 50.0,
        "rate-overload-121": 30.25,
    }

    for profile, duration in durations.items():
        assert orchestrator.PROFILE_TIMEOUT_SECONDS[profile] >= 2 * duration + 25


@dataclass
class FakeClock:
    """由 sleep 驱动的单调时钟。"""

    value: float = 0.0

    def monotonic(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.value += seconds


class FakeLoadgenProcess:
    """可轮询并记录终止顺序的假 loadgen 进程。"""

    def __init__(
        self,
        clock: FakeClock,
        *,
        complete_at: float,
        stdout: str,
        returncode: int = 0,
        wait_times_out_once: bool = False,
    ) -> None:
        self.clock = clock
        self.complete_at = complete_at
        self.stdout = stdout
        self.returncode = returncode
        self.wait_times_out_once = wait_times_out_once
        self.events: list[str] = []
        self.progress_events: list[object] = []
        self.progress_error = False

    def poll(self) -> int | None:
        self.events.append("poll")
        return self.returncode if self.clock.value >= self.complete_at else None

    def wait(self, timeout: float) -> int:
        self.events.append(f"wait:{timeout}")
        if self.wait_times_out_once:
            self.wait_times_out_once = False
            raise TimeoutError
        return self.returncode

    def terminate(self) -> None:
        self.events.append("terminate")

    def kill(self) -> None:
        self.events.append("kill")

    def read_stdout(self) -> str:
        self.events.append("stdout")
        return self.stdout

    def drain_progress(self, *, final: bool = False) -> tuple[object, ...]:
        self.events.append(f"progress:{final}")
        events = tuple(self.progress_events)
        self.progress_events.clear()
        return events

    def progress_protocol_error(self) -> bool:
        return self.progress_error

    def wait_for_activity(self, timeout: float) -> None:
        self.events.append(f"activity:{timeout}")
        self.clock.sleep(timeout)


class FakeSuiteRunner:
    """记录完整 suite 生命周期而不调用 Docker。"""

    def __init__(
        self,
        orchestrator,
        clock: FakeClock,
        *,
        preflight_code: int = 0,
        build_code: int = 0,
        start_code: int = 0,
        stop_code: int = 0,
        loadgen_cleanup_code: int = 0,
        loadgen_verify_code: int = 0,
        process_complete_after: float = 0.0,
        after: Mapping[str, tuple[str, str, int, bool]] | None | str = "same",
    ) -> None:
        self.orchestrator = orchestrator
        self.clock = clock
        self.preflight_code = preflight_code
        self.build_code = build_code
        self.start_code = start_code
        self.stop_code = stop_code
        self.loadgen_cleanup_code = loadgen_cleanup_code
        self.loadgen_verify_code = loadgen_verify_code
        self.process_complete_after = process_complete_after
        self.events: list[str] = []
        self.processes: list[FakeLoadgenProcess] = []
        self.repository_root = PROJECT_ROOT
        self.preflight_public_baseline = {
            name: (f"id-{name}", "healthy", 0, False)
            for name in orchestrator.PUBLIC_LONG_LIVED_SERVICES
        }
        self.after = self.preflight_public_baseline if after == "same" else after

    def preflight_only(self) -> int:
        self.events.append("preflight")
        if self.preflight_code != 0:
            self.preflight_public_baseline = None
        return self.preflight_code

    def build_capacity(self):
        self.events.append("build")
        return self.orchestrator.CommandResult(("build",), self.build_code, "")

    def start_capacity(self):
        self.events.append("up")
        return self.orchestrator.CommandResult(("up",), self.start_code, "")

    def start_loadgen(self, profile: str):
        self.events.append(f"run:{profile}")
        process = FakeLoadgenProcess(
            self.clock,
            complete_at=self.clock.value + self.process_complete_after,
            stdout=json.dumps(task2_report(profile)),
        )
        self.processes.append(process)
        return process

    def stop_capacity_project(self):
        self.events.append("stop")
        return self.orchestrator.CommandResult(("stop",), self.stop_code, "")

    def ensure_loadgen_stopped(self):
        self.events.append("loadgen_zero")
        return self.orchestrator.CommandResult(("loadgen-zero",), self.loadgen_cleanup_code, "")

    def verify_loadgen_zero(self):
        self.events.append("loadgen_verify")
        return self.orchestrator.CommandResult(("loadgen-verify",), self.loadgen_verify_code, "")

    def read_public_stack_after(self):
        self.events.append("public_after")
        return self.after


def fake_executor(orchestrator):
    """提供不触碰 Docker 的固定成功执行器。"""

    return lambda argv, _environment: orchestrator.CommandResult(tuple(argv), 0, "")


class RecordingExecutor:
    """记录全部只读/生命周期 argv 和最小子进程环境。"""

    def __init__(
        self,
        orchestrator,
        responses: Sequence[tuple[int, str]] = (),
    ) -> None:
        self._orchestrator = orchestrator
        self._responses = list(responses)
        self.calls: list[tuple[tuple[str, ...], dict[str, str]]] = []

    def __call__(self, argv: Sequence[str], environment: Mapping[str, str]):
        normalized = tuple(argv)
        self.calls.append((normalized, dict(environment)))
        returncode, stdout = self._responses.pop(0) if self._responses else (0, "")
        return self._orchestrator.CommandResult(normalized, returncode, stdout)


def capacity_repository(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    """创建不含真实凭据的最小固定路径树。"""

    root = tmp_path / "repository"
    compose = root / "infra/docker/compose.capacity-synthetic.yml"
    compose.parent.mkdir(parents=True)
    compose.write_text("name: campus-ai-dingtalk-capacity-synthetic\n", encoding="utf-8")
    (root / "infra/docker/api.capacity-synthetic.Dockerfile").write_text(
        "FROM scratch\n", encoding="utf-8"
    )
    nginx = root / "infra/nginx/dingtalk-capacity-synthetic.conf.template"
    nginx.parent.mkdir(parents=True)
    nginx.write_text("", encoding="utf-8")
    env_file = root / ".env.capacity-synthetic"
    env_file.write_text("APP_ENV=test\n", encoding="utf-8")
    docker_cli = tmp_path / ("docker.exe" if os.name == "nt" else "docker")
    docker_cli.write_text("", encoding="utf-8")
    docker_cli.chmod(0o700)
    meminfo = tmp_path / "meminfo"
    meminfo.write_text(
        "MemTotal:       1000 kB\nMemAvailable:    800 kB\n",
        encoding="ascii",
    )
    return root, env_file, docker_cli, meminfo


def valid_compose_config(orchestrator, root: Path) -> dict[str, object]:
    """构造与 Task3 Compose 展开契约一致的最小 JSON。"""

    images = {
        "postgres": "postgres:16-alpine",
        "redis": "redis:7-alpine",
        "migrate": f"{orchestrator.CAPACITY_PROJECT}-api:local",
        "api-synthetic": f"{orchestrator.CAPACITY_PROJECT}-api:local",
        "gateway": "nginx:1.27-alpine",
        "loadgen": f"{orchestrator.CAPACITY_PROJECT}-api:local",
    }
    dependencies = {
        "migrate": {
            "postgres": {"condition": "service_healthy"},
            "redis": {"condition": "service_healthy"},
        },
        "api-synthetic": {
            "postgres": {"condition": "service_healthy"},
            "redis": {"condition": "service_healthy"},
            "migrate": {"condition": "service_completed_successfully"},
        },
        "gateway": {"api-synthetic": {"condition": "service_healthy"}},
        "loadgen": {
            "api-synthetic": {"condition": "service_healthy"},
            "gateway": {"condition": "service_healthy"},
        },
    }
    services: dict[str, object] = {}
    for name in orchestrator.CAPACITY_SERVICES:
        service: dict[str, object] = {
            "image": images[name],
            "networks": {"capacity-private": {}},
            "restart": "no",
        }
        if name in dependencies:
            service["depends_on"] = dependencies[name]
        if name == "api-synthetic":
            service["build"] = {
                "context": str(root),
                "dockerfile": "infra/docker/api.capacity-synthetic.Dockerfile",
            }
        if name in {"migrate", "api-synthetic"}:
            service["env_file"] = [str(root / ".env.capacity-synthetic")]
        if name == "postgres":
            service["volumes"] = [
                {
                    "type": "volume",
                    "source": "capacity-postgres-data",
                    "target": "/var/lib/postgresql/data",
                }
            ]
        if name == "redis":
            service["volumes"] = [
                {
                    "type": "volume",
                    "source": "capacity-redis-data",
                    "target": "/data",
                }
            ]
        if name == "gateway":
            service["volumes"] = [
                {
                    "type": "bind",
                    "source": str(root / "infra/nginx/dingtalk-capacity-synthetic.conf.template"),
                    "target": "/etc/nginx/templates/default.conf.template",
                    "read_only": True,
                }
            ]
        if name == "loadgen":
            service["profiles"] = ["capacity-loadgen"]
            service["entrypoint"] = [
                "python",
                "/workspace/tests/load/dingtalk_capacity_runner.py",
            ]
            service["command"] = ["--help"]
        services[name] = service
    return {
        "name": orchestrator.CAPACITY_PROJECT,
        "services": services,
        "networks": {
            "capacity-private": {
                "internal": True,
                "driver": "bridge",
                "name": f"{orchestrator.CAPACITY_PROJECT}_capacity-private",
            }
        },
        "volumes": {
            name: {"name": f"{orchestrator.CAPACITY_PROJECT}_{name}"}
            for name in orchestrator.CAPACITY_VOLUMES
        },
    }


def public_inspect_rows(
    orchestrator,
    *,
    unhealthy_service: str | None = None,
    oom_service: str | None = None,
) -> tuple[str, str]:
    """返回固定五服务 ps ID 和安全 inspect 行，不含环境或凭据。"""

    ids: list[str] = []
    rows: list[str] = []
    for name in orchestrator.PUBLIC_LONG_LIVED_SERVICES:
        container_id = f"id-{name}"
        ids.append(container_id)
        health = "unhealthy" if name == unhealthy_service else "healthy"
        oom = "true" if name == oom_service else "false"
        rows.append(
            "\t".join(
                (
                    container_id,
                    orchestrator.PUBLIC_PROJECT,
                    name,
                    "true",
                    health,
                    "0",
                    oom,
                )
            )
        )
    return "\n".join(ids) + "\n", "\n".join(rows) + "\n"


def test_runner_fixes_env_compose_and_absolute_docker_cli(orchestrator, tmp_path: Path) -> None:
    """env/Compose 只能取仓库固定普通文件，Docker CLI 必须先固定为绝对路径。"""

    root, env_file, docker_cli, _meminfo = capacity_repository(tmp_path)
    executor = RecordingExecutor(orchestrator)
    runner = orchestrator.DockerComposeCapacityRunner(
        env_file,
        repository_root=root,
        docker_cli=docker_cli,
        executor=executor,
    )

    assert runner.env_file == env_file
    assert runner.compose_file == root / "infra/docker/compose.capacity-synthetic.yml"
    assert runner.compose_argv("stop")[0] == str(docker_cli)
    with pytest.raises(ValueError, match="固定容量环境文件"):
        orchestrator.DockerComposeCapacityRunner(
            tmp_path / "other.env",
            repository_root=root,
            docker_cli=docker_cli,
            executor=executor,
        )


def test_minimal_child_environment_drops_parent_overrides(
    orchestrator, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Compose、Docker context、代理和容量变量不能从父 Shell 渗入。"""

    root, env_file, docker_cli, _meminfo = capacity_repository(tmp_path)
    forbidden = {
        "COMPOSE_FILE": "public.yml",
        "COMPOSE_PROJECT_NAME": orchestrator.PUBLIC_PROJECT,
        "DOCKER_CONTEXT": "remote",
        "HTTP_PROXY": "http://proxy.invalid",
        "HTTPS_PROXY": "http://proxy.invalid",
        "NO_PROXY": "*",
        "POSTGRES_PASSWORD": "must-not-leak",
        "CAMPUS_AI_SYNTHETIC_CAPACITY": "false",
    }
    for key, value in forbidden.items():
        monkeypatch.setenv(key, value)
    runner = orchestrator.DockerComposeCapacityRunner(
        env_file,
        repository_root=root,
        docker_cli=docker_cli,
        executor=RecordingExecutor(orchestrator),
    )

    child = runner.minimal_environment()
    assert set(child) == {"LANG", "LC_ALL", "PATH"}
    assert all(key not in child for key in forbidden)


def test_runner_uses_fixed_lifecycle_argv_and_audits_every_command(
    orchestrator, tmp_path: Path
) -> None:
    """build/up/run/exec/stop 均复用同一固定前缀，只有 loadgen 启用 profile。"""

    root, env_file, docker_cli, _meminfo = capacity_repository(tmp_path)
    executor = RecordingExecutor(orchestrator)
    runner = orchestrator.DockerComposeCapacityRunner(
        env_file,
        repository_root=root,
        docker_cli=docker_cli,
        executor=executor,
    )
    prefix = (
        str(docker_cli),
        "compose",
        "--project-name",
        orchestrator.CAPACITY_PROJECT,
        "--env-file",
        str(env_file),
        "-f",
        str(root / "infra/docker/compose.capacity-synthetic.yml"),
    )

    runner.config()
    runner.build_capacity()
    runner.start_capacity()
    runner.read_redis_metrics()
    runner.run_loadgen("rate-warmup-10")
    runner.stop_capacity_project()

    assert [call[0] for call in executor.calls] == [
        prefix
        + (
            "--profile",
            "capacity-loadgen",
            "config",
            "--no-env-resolution",
            "--format",
            "json",
        ),
        prefix + ("build",),
        prefix
        + (
            "up",
            "--detach",
            "--no-build",
            "--wait",
            "--wait-timeout",
            "120",
            "postgres",
            "redis",
            "migrate",
            "api-synthetic",
            "gateway",
        ),
        prefix
        + (
            "exec",
            "-T",
            "redis",
            "sh",
            "-ceu",
            orchestrator.REDIS_METRICS_SCRIPT,
        ),
        prefix
        + (
            "--profile",
            "capacity-loadgen",
            "run",
            "--rm",
            "--name",
            orchestrator.LOADGEN_CONTAINER_NAME,
            "--label",
            runner.loadgen_owner_pair,
            "--no-deps",
            "loadgen",
            "--profile",
            "rate-warmup-10",
        ),
        prefix + ("stop",),
    ]
    assert runner.command_audit == [call[0] for call in executor.calls]
    assert all(orchestrator.PUBLIC_PROJECT not in " ".join(call) for call in runner.command_audit)
    assert all("down" not in call and "rm" not in call for call in runner.command_audit)
    with pytest.raises(ValueError, match="固定容量场景"):
        runner.run_loadgen("arbitrary")


def test_preflight_rejects_symlinked_fixed_file_before_docker(
    orchestrator,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """固定 env/Compose 任一为 symlink 都属于参数/路径错误，且零 Docker 命令。"""

    root, env_file, docker_cli, meminfo = capacity_repository(tmp_path)
    executor = RecordingExecutor(orchestrator)
    runner = orchestrator.DockerComposeCapacityRunner(
        env_file,
        repository_root=root,
        docker_cli=docker_cli,
        meminfo_path=meminfo,
        executor=executor,
    )
    original_is_symlink = Path.is_symlink
    monkeypatch.setattr(
        Path,
        "is_symlink",
        lambda path: path == runner.compose_file or original_is_symlink(path),
    )

    assert runner.preflight_only() == 2
    assert executor.calls == []
    assert runner.lifecycle_calls == []


def test_preflight_low_memory_fails_before_any_docker_command(orchestrator, tmp_path: Path) -> None:
    """MemAvailable 低于 30% 返回资源门失败，不能继续读取 Docker。"""

    root, env_file, docker_cli, meminfo = capacity_repository(tmp_path)
    meminfo.write_text(
        "MemTotal:       1000 kB\nMemAvailable:    299 kB\n",
        encoding="ascii",
    )
    executor = RecordingExecutor(orchestrator)
    runner = orchestrator.DockerComposeCapacityRunner(
        env_file,
        repository_root=root,
        docker_cli=docker_cli,
        meminfo_path=meminfo,
        executor=executor,
    )

    assert runner.preflight_only() == 1
    assert executor.calls == []
    assert runner.lifecycle_calls == []


def test_default_preflight_reads_strict_config_and_healthy_public_baseline(
    orchestrator, tmp_path: Path
) -> None:
    """默认预检必须按路径→内存→Compose→公网 ps/inspect 的只读顺序成功。"""

    root, env_file, docker_cli, meminfo = capacity_repository(tmp_path)
    ps_stdout, inspect_stdout = public_inspect_rows(orchestrator)
    executor = RecordingExecutor(
        orchestrator,
        (
            (0, json.dumps(valid_compose_config(orchestrator, root))),
            (0, ps_stdout),
            (0, inspect_stdout),
        ),
    )
    runner = orchestrator.DockerComposeCapacityRunner(
        env_file,
        repository_root=root,
        docker_cli=docker_cli,
        meminfo_path=meminfo,
        executor=executor,
    )

    assert runner.preflight_only() == 0
    assert runner.preflight_public_baseline == {
        name: (f"id-{name}", "healthy", 0, False)
        for name in orchestrator.PUBLIC_LONG_LIVED_SERVICES
    }
    assert [call[0] for call in executor.calls] == [
        runner.compose_argv(
            "--profile",
            "capacity-loadgen",
            "config",
            "--no-env-resolution",
            "--format",
            "json",
        ),
        (
            str(docker_cli),
            "ps",
            "--no-trunc",
            "--filter",
            f"label=com.docker.compose.project={orchestrator.PUBLIC_PROJECT}",
            "--format",
            "{{.ID}}",
        ),
        (
            str(docker_cli),
            "inspect",
            "--format",
            orchestrator.PUBLIC_INSPECT_FORMAT,
            *(f"id-{name}" for name in orchestrator.PUBLIC_LONG_LIVED_SERVICES),
        ),
    ]
    assert runner.lifecycle_calls == []


@pytest.mark.parametrize(
    ("change", "expected"),
    (
        ({"configs": {"unsafe": {}}}, False),
        ({"secrets": {"unsafe": {}}}, False),
    ),
)
def test_compose_allowlist_rejects_top_level_configs_or_secrets(
    orchestrator, tmp_path: Path, change: dict[str, object], expected: bool
) -> None:
    """容量 Compose 不接受额外 configs/secrets。"""

    root, env_file, docker_cli, _meminfo = capacity_repository(tmp_path)
    config = valid_compose_config(orchestrator, root)
    config.update(change)
    runner = orchestrator.DockerComposeCapacityRunner(
        env_file,
        repository_root=root,
        docker_cli=docker_cli,
        executor=RecordingExecutor(orchestrator),
    )

    clean = valid_compose_config(orchestrator, root)
    assert runner.validate_compose_config(clean) is True
    assert runner.validate_compose_config(config) is expected


@pytest.mark.parametrize(
    "mutation",
    (
        "extra_build",
        "host_network",
        "service_secret",
        "docker_socket",
        "unsafe_bind",
        "wrong_image",
        "wrong_profile",
        "wrong_entrypoint",
        "wrong_env_file",
        "wrong_dependency",
        "wrong_restart",
        "network_ipam",
        "network_attachable",
        "network_driver_opts",
        "network_name",
        "writable_gateway_bind",
    ),
)
def test_compose_allowlist_rejects_dangerous_service_expansion(
    orchestrator, tmp_path: Path, mutation: str
) -> None:
    """额外 build、host 网络、服务 secret 和危险 bind 必须失败关闭。"""

    root, env_file, docker_cli, _meminfo = capacity_repository(tmp_path)
    config = valid_compose_config(orchestrator, root)
    services = config["services"]
    assert isinstance(services, dict)
    if mutation == "extra_build":
        services["loadgen"]["build"] = {"context": str(root)}
    elif mutation == "host_network":
        services["gateway"]["network_mode"] = "host"
    elif mutation == "service_secret":
        services["api-synthetic"]["secrets"] = [{"source": "unsafe"}]
    elif mutation == "docker_socket":
        services["gateway"]["volumes"] = [
            {
                "type": "bind",
                "source": "/var/run/docker.sock",
                "target": "/var/run/docker.sock",
            }
        ]
    elif mutation == "unsafe_bind":
        services["api-synthetic"]["volumes"] = [
            {
                "type": "bind",
                "source": str(root),
                "target": "/workspace",
                "read_only": True,
            }
        ]
    elif mutation == "wrong_image":
        services["gateway"]["image"] = "nginx:latest"
    elif mutation == "wrong_profile":
        services["loadgen"]["profiles"] = ["default"]
    elif mutation == "wrong_entrypoint":
        services["loadgen"]["command"] = ["--profile", "rate-100"]
    elif mutation == "wrong_env_file":
        services["api-synthetic"]["env_file"] = [str(tmp_path / "other.env")]
    elif mutation == "wrong_dependency":
        services["gateway"]["depends_on"] = {}
    elif mutation == "wrong_restart":
        services["redis"]["restart"] = "always"
    elif mutation == "writable_gateway_bind":
        services["gateway"]["volumes"][0]["read_only"] = False
    else:
        networks = config["networks"]
        assert isinstance(networks, dict)
        network = networks["capacity-private"]
        if mutation == "network_ipam":
            network["ipam"] = {"driver": "default"}
        elif mutation == "network_attachable":
            network["attachable"] = True
        elif mutation == "network_driver_opts":
            network["driver_opts"] = {"com.invalid": "1"}
        else:
            network["name"] = "shared-public-network"
    runner = orchestrator.DockerComposeCapacityRunner(
        env_file,
        repository_root=root,
        docker_cli=docker_cli,
        executor=RecordingExecutor(orchestrator),
    )

    assert runner.validate_compose_config(valid_compose_config(orchestrator, root)) is True
    assert runner.validate_compose_config(config) is False


def test_compose_allowlist_accepts_compose_empty_ipam_default(orchestrator, tmp_path: Path) -> None:
    """Docker Compose 自动补出的空 IPAM 对象不改变内部网络安全边界。"""

    root, env_file, docker_cli, _meminfo = capacity_repository(tmp_path)
    config = valid_compose_config(orchestrator, root)
    networks = config["networks"]
    assert isinstance(networks, dict)
    networks["capacity-private"]["ipam"] = {}
    runner = orchestrator.DockerComposeCapacityRunner(
        env_file,
        repository_root=root,
        docker_cli=docker_cli,
        executor=RecordingExecutor(orchestrator),
    )

    assert runner.validate_compose_config(config) is True


@pytest.mark.parametrize("problem", ("missing", "duplicate", "unhealthy", "oom"))
def test_public_baseline_rejects_any_nonhealthy_or_nonunique_five_service_set(
    orchestrator, tmp_path: Path, problem: str
) -> None:
    """公网基线自身必须精确五服务、唯一、running+healthy 且非 OOM。"""

    root, env_file, docker_cli, _meminfo = capacity_repository(tmp_path)
    ps_stdout, inspect_stdout = public_inspect_rows(
        orchestrator,
        unhealthy_service="api" if problem == "unhealthy" else None,
        oom_service="api" if problem == "oom" else None,
    )
    if problem == "missing":
        ps_stdout = "\n".join(ps_stdout.splitlines()[:-1]) + "\n"
    elif problem == "duplicate":
        lines = inspect_stdout.splitlines()
        inspect_stdout = "\n".join((*lines, lines[0])) + "\n"
    executor = RecordingExecutor(
        orchestrator,
        ((0, ps_stdout), (0, inspect_stdout)),
    )
    runner = orchestrator.DockerComposeCapacityRunner(
        env_file,
        repository_root=root,
        docker_cli=docker_cli,
        executor=executor,
    )

    assert runner.read_public_stack_baseline() is None


def capacity_container_outputs(
    orchestrator,
    *,
    include_loadgen: bool = True,
    malformed_stats: bool = False,
) -> tuple[str, str, str]:
    """构造容量标签容器 ps/inspect/stats 的安全 fake 输出。"""

    services = list(orchestrator.CAPACITY_LONG_LIVED_SERVICES)
    if include_loadgen:
        services.append("loadgen")
    identifiers = [f"capacity-id-{name}" for name in services]
    inspect_rows: list[str] = []
    stats_rows: list[str] = []
    for index, (service, identifier) in enumerate(zip(services, identifiers, strict=True)):
        health = "none" if service == "loadgen" else "healthy"
        inspect_rows.append(
            "\t".join(
                (
                    identifier,
                    orchestrator.CAPACITY_PROJECT,
                    service,
                    "true",
                    health,
                    "0",
                    "false",
                )
            )
        )
        cpu = "not-a-percent" if malformed_stats and index == 0 else f"{10 + index}.5%"
        stats_rows.append(f"{identifier}\t{cpu}\t{5 + index}.25%")
    return (
        "\n".join(identifiers) + "\n",
        "\n".join(inspect_rows) + "\n",
        "\n".join(stats_rows) + "\n",
    )


def test_resource_snapshot_reader_parses_fixed_read_only_sources(
    orchestrator, tmp_path: Path
) -> None:
    """真实 reader 只读容量标签容器、容器内 Redis/PG，并保留逐容器 CPU。"""

    root, env_file, docker_cli, meminfo = capacity_repository(tmp_path)
    env_file.write_text(
        "APP_ENV=test\nPOSTGRES_PASSWORD=literal-must-not-enter-argv\n"
        "REDIS_PASSWORD=literal-must-not-enter-argv\n",
        encoding="utf-8",
    )
    ps_stdout, inspect_stdout, stats_stdout = capacity_container_outputs(orchestrator)
    executor = RecordingExecutor(
        orchestrator,
        (
            (0, ps_stdout),
            (0, inspect_stdout),
            (0, stats_stdout),
            (
                0,
                "connected_clients:40\r\nblocked_clients:0\r\n"
                "maxclients:100\r\nconcurrency_leases:3\r\n",
            ),
            (0, "20\t100\n"),
        ),
    )
    runner = orchestrator.DockerComposeCapacityRunner(
        env_file,
        repository_root=root,
        docker_cli=docker_cli,
        meminfo_path=meminfo,
        executor=executor,
    )

    snapshot = runner.read_resource_snapshot()

    assert snapshot.mem_available_ratio == 0.8
    assert snapshot.all_healthy is True
    assert snapshot.restart_detected is False
    assert snapshot.oom_detected is False
    assert snapshot.redis_blocked_clients == 0
    assert snapshot.redis_connection_ratio == 0.4
    assert snapshot.postgres_connection_ratio == 0.2
    assert snapshot.concurrency_leases == 3
    assert snapshot.container_cpu_percent == {
        name: 10.5 + index
        for index, name in enumerate((*orchestrator.CAPACITY_LONG_LIVED_SERVICES, "loadgen"))
    }
    assert snapshot.max_container_cpu_percent == 14.5
    assert snapshot.container_memory_percent == {
        name: 5.25 + index
        for index, name in enumerate((*orchestrator.CAPACITY_LONG_LIVED_SERVICES, "loadgen"))
    }
    assert snapshot.max_container_memory_percent == 9.25
    commands = [call[0] for call in executor.calls]
    assert commands[0] == (
        str(docker_cli),
        "ps",
        "--no-trunc",
        "--filter",
        f"label=com.docker.compose.project={orchestrator.CAPACITY_PROJECT}",
        "--format",
        "{{.ID}}",
    )
    assert commands[2] == (
        str(docker_cli),
        "stats",
        "--no-trunc",
        "--no-stream",
        "--format",
        "{{.ID}}\t{{.CPUPerc}}\t{{.MemPerc}}",
        *(
            f"capacity-id-{name}"
            for name in (*orchestrator.CAPACITY_LONG_LIVED_SERVICES, "loadgen")
        ),
    )
    assert commands[3] == runner.compose_argv(
        "exec",
        "-T",
        "redis",
        "sh",
        "-ceu",
        orchestrator.REDIS_METRICS_SCRIPT,
    )
    assert commands[4] == runner.compose_argv(
        "exec",
        "-T",
        "postgres",
        "sh",
        "-ceu",
        orchestrator.POSTGRES_METRICS_SCRIPT,
    )
    serialized_commands = "\n".join(" ".join(command) for command in commands)
    assert "literal-must-not-enter-argv" not in serialized_commands
    assert all(call[1] == runner.minimal_environment() for call in executor.calls)


def test_resource_snapshot_retries_once_when_oneoff_disappears_during_stats(
    orchestrator, tmp_path: Path, monkeypatch
) -> None:
    """one-off 自动删除撞上 stats 时只重读一次完整容器集合。"""

    root, env_file, docker_cli, meminfo = capacity_repository(tmp_path)
    first_ps, first_inspect, _first_stats = capacity_container_outputs(orchestrator)
    second_ps, second_inspect, second_stats = capacity_container_outputs(
        orchestrator, include_loadgen=False
    )
    executor = RecordingExecutor(
        orchestrator,
        (
            (0, first_ps),
            (0, first_inspect),
            (1, ""),
            (0, second_ps),
            (0, second_inspect),
            (0, second_stats),
            (
                0,
                "connected_clients:1\nblocked_clients:0\nmaxclients:100\nconcurrency_leases:0\n",
            ),
            (0, "1\t100\n"),
        ),
    )
    runner = orchestrator.DockerComposeCapacityRunner(
        env_file,
        repository_root=root,
        docker_cli=docker_cli,
        meminfo_path=meminfo,
        executor=executor,
    )
    retry_sleeps: list[float] = []
    monkeypatch.setattr(orchestrator, "sleep", retry_sleeps.append)

    snapshot = runner.read_resource_snapshot()

    assert snapshot.all_healthy is True
    assert set(snapshot.container_cpu_percent) == set(orchestrator.CAPACITY_LONG_LIVED_SERVICES)
    commands = [call[0] for call in executor.calls]
    assert sum(command[1] == "ps" for command in commands) == 2
    assert sum("stats" in command for command in commands) == 2
    assert retry_sleeps == [0.1]


def test_resource_snapshot_accepts_only_one_disappearing_loadgen_during_bulk_inspect(
    orchestrator, tmp_path: Path
) -> None:
    """批量 inspect 撞上自动删除时须逐个证明四个长期服务仍合法。"""

    root, env_file, docker_cli, meminfo = capacity_repository(tmp_path)
    ps_stdout, inspect_stdout, _stats_stdout = capacity_container_outputs(orchestrator)
    inspect_rows = inspect_stdout.splitlines()
    long_lived_rows = inspect_rows[:-1]
    _second_ps, _second_inspect, long_lived_stats = capacity_container_outputs(
        orchestrator, include_loadgen=False
    )
    executor = RecordingExecutor(
        orchestrator,
        (
            (0, ps_stdout),
            (1, ""),
            *((0, row + "\n") for row in long_lived_rows),
            (1, ""),
            (0, long_lived_stats),
            (
                0,
                "connected_clients:1\nblocked_clients:0\nmaxclients:100\nconcurrency_leases:0\n",
            ),
            (0, "1\t100\n"),
        ),
    )
    runner = orchestrator.DockerComposeCapacityRunner(
        env_file,
        repository_root=root,
        docker_cli=docker_cli,
        meminfo_path=meminfo,
        executor=executor,
    )

    snapshot = runner.read_resource_snapshot()

    assert snapshot.all_healthy is True
    assert set(snapshot.container_cpu_percent) == set(orchestrator.CAPACITY_LONG_LIVED_SERVICES)
    commands = [call[0] for call in executor.calls]
    inspect_commands = [command for command in commands if command[1] == "inspect"]
    assert len(inspect_commands) == 6
    assert len(inspect_commands[0]) == 9
    assert all(len(command) == 5 for command in inspect_commands[1:])


def test_resource_snapshot_fails_after_repeated_stats_command_error(
    orchestrator, tmp_path: Path
) -> None:
    """stats 连续失败时即使 one-off 可能退出也必须失败关闭。"""

    root, env_file, docker_cli, meminfo = capacity_repository(tmp_path)
    ps_stdout, inspect_stdout, _stats_stdout = capacity_container_outputs(orchestrator)
    responses: list[tuple[int, str]] = []
    for _attempt in range(4):
        responses.extend(((0, ps_stdout), (0, inspect_stdout), (1, "")))
    executor = RecordingExecutor(
        orchestrator,
        tuple(responses),
    )
    runner = orchestrator.DockerComposeCapacityRunner(
        env_file,
        repository_root=root,
        docker_cli=docker_cli,
        meminfo_path=meminfo,
        executor=executor,
    )

    with pytest.raises(ValueError, match="资源快照"):
        runner.read_resource_snapshot()
    assert sum("stats" in call[0] for call in executor.calls) == 4


def test_first_resource_snapshot_detects_any_existing_capacity_restart(
    orchestrator, tmp_path: Path
) -> None:
    """隔离容量栈刚启动就有 restart count 时，首个样本也必须触发停止证据。"""

    root, env_file, docker_cli, meminfo = capacity_repository(tmp_path)
    ps_stdout, inspect_stdout, stats_stdout = capacity_container_outputs(orchestrator)
    rows = inspect_stdout.splitlines()
    fields = rows[0].split("\t")
    fields[5] = "1"
    rows[0] = "\t".join(fields)
    executor = RecordingExecutor(
        orchestrator,
        (
            (0, ps_stdout),
            (0, "\n".join(rows) + "\n"),
            (0, stats_stdout),
            (
                0,
                "connected_clients:1\nblocked_clients:0\nmaxclients:100\nconcurrency_leases:0\n",
            ),
            (0, "1\t100\n"),
        ),
    )
    runner = orchestrator.DockerComposeCapacityRunner(
        env_file,
        repository_root=root,
        docker_cli=docker_cli,
        meminfo_path=meminfo,
        executor=executor,
    )

    assert runner.read_resource_snapshot().restart_detected is True


@pytest.mark.parametrize(
    ("failure_source", "replacement"),
    (
        ("stats", "capacity-id-postgres\tnot-a-percent\t1.0%\n"),
        (
            "redis",
            "connected_clients:1\nblocked_clients:0\nmaxclients:0\nconcurrency_leases:0\n",
        ),
        ("postgres", "1\t0\n"),
    ),
)
def test_resource_snapshot_reader_fails_closed_on_parse_error(
    orchestrator,
    tmp_path: Path,
    failure_source: str,
    replacement: str,
) -> None:
    """stats/Redis/PostgreSQL 任一输出异常均不得生成部分健康快照。"""

    root, env_file, docker_cli, meminfo = capacity_repository(tmp_path)
    ps_stdout, inspect_stdout, stats_stdout = capacity_container_outputs(orchestrator)
    redis_stdout = "connected_clients:1\nblocked_clients:0\nmaxclients:100\nconcurrency_leases:0\n"
    postgres_stdout = "1\t100\n"
    if failure_source == "stats":
        stats_stdout = replacement
    elif failure_source == "redis":
        redis_stdout = replacement
    else:
        postgres_stdout = replacement
    executor = RecordingExecutor(
        orchestrator,
        (
            (0, ps_stdout),
            (0, inspect_stdout),
            (0, stats_stdout),
            (0, redis_stdout),
            (0, postgres_stdout),
        ),
    )
    runner = orchestrator.DockerComposeCapacityRunner(
        env_file,
        repository_root=root,
        docker_cli=docker_cli,
        meminfo_path=meminfo,
        executor=executor,
    )

    with pytest.raises(ValueError, match="资源快照"):
        runner.read_resource_snapshot()


def test_stop_policy_tracks_high_cpu_per_container_not_aggregate_max(
    orchestrator,
) -> None:
    """不同容器交替超限不能拼成同一容器连续 60 秒。"""

    policy = orchestrator.StopPolicy()
    for index in range(30):
        service = "api-synthetic" if index % 2 == 0 else "gateway"
        assert (
            policy.evaluate(
                healthy_snapshot(
                    orchestrator,
                    max_container_cpu_percent=90.0,
                    container_cpu_percent={service: 90.0},
                ),
                None,
            )
            is None
        )
    for _ in range(30):
        assert (
            policy.evaluate(
                healthy_snapshot(
                    orchestrator,
                    max_container_cpu_percent=90.0,
                    container_cpu_percent={"api-synthetic": 90.0},
                ),
                None,
            )
            is None
        )
    assert (
        policy.evaluate(
            healthy_snapshot(
                orchestrator,
                max_container_cpu_percent=90.0,
                container_cpu_percent={"api-synthetic": 90.0},
            ),
            None,
        )
        == "high_cpu"
    )


def test_capacity_monitor_reports_only_fixed_resource_peaks(orchestrator) -> None:
    """监视器汇总容器内存、连接比和租约峰值，不保留原始诊断。"""

    snapshots = iter(
        (
            healthy_snapshot(
                orchestrator,
                max_container_cpu_percent=20.0,
                max_container_memory_percent=30.0,
                redis_connection_ratio=0.2,
                postgres_connection_ratio=0.3,
                concurrency_leases=4,
            ),
            healthy_snapshot(
                orchestrator,
                max_container_cpu_percent=40.0,
                max_container_memory_percent=25.0,
                redis_connection_ratio=0.6,
                postgres_connection_ratio=0.5,
                concurrency_leases=7,
            ),
        )
    )
    monitor = orchestrator.CapacityMonitor(lambda: next(snapshots))

    monitor.sample()
    monitor.sample()

    assert monitor.report_peaks() == {
        "peak_container_cpu_percent": 40.0,
        "peak_container_memory_percent": 30.0,
        "minimum_mem_available_ratio": 0.8,
        "peak_redis_connection_ratio": 0.6,
        "peak_postgres_connection_ratio": 0.5,
        "peak_concurrency_leases": 7,
        "redis_blocked_seen": False,
        "oom_seen": False,
        "restart_seen": False,
    }


def test_cli_default_preflight_prints_only_fixed_success_marker(
    orchestrator, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """真实默认 preflight 路径复用完整门，且 stdout 只有固定成功标记。"""

    root, env_file, docker_cli, meminfo = capacity_repository(tmp_path)
    ps_stdout, inspect_stdout = public_inspect_rows(orchestrator)
    executor = RecordingExecutor(
        orchestrator,
        (
            (0, json.dumps(valid_compose_config(orchestrator, root))),
            (0, ps_stdout),
            (0, inspect_stdout),
        ),
    )
    runner = orchestrator.DockerComposeCapacityRunner(
        env_file,
        repository_root=root,
        docker_cli=docker_cli,
        meminfo_path=meminfo,
        executor=executor,
    )

    code = orchestrator.run_cli(
        [
            "--confirm-project",
            orchestrator.CAPACITY_PROJECT,
            "--preflight-only",
        ],
        runner=runner,
    )

    assert code == 0
    assert capsys.readouterr().out == "DINGTALK_CAPACITY_PREFLIGHT_OK\n"
    assert runner.lifecycle_calls == []


def test_runner_rejects_any_other_project(orchestrator) -> None:
    """CLI 确认值不能误指向公网测试项目。"""

    with pytest.raises(ValueError, match="固定隔离容量项目"):
        orchestrator.validate_confirmation("campus-ai-dingtalk-test")


def test_stop_policy_stops_on_low_memory_or_unexpected_429(orchestrator) -> None:
    """硬资源线和目标容量内 429 必须立即停止。"""

    policy = orchestrator.StopPolicy()
    assert (
        policy.evaluate(healthy_snapshot(orchestrator, mem_available_ratio=0.29), None)
        == "low_memory"
    )
    assert (
        policy.evaluate(healthy_snapshot(orchestrator), target_report(status_counts={"429": 1}))
        == "unexpected_429"
    )


def test_stop_policy_fails_closed_on_noninteger_report_counts(orchestrator) -> None:
    """字符串化计数不是合法聚合证据，不能被隐式 int 转换接受。"""

    policy = orchestrator.StopPolicy()
    assert (
        policy.evaluate(
            healthy_snapshot(orchestrator),
            target_report(status_counts={"429": "1"}),
        )
        == "runner_failed"
    )


def test_stop_policy_requires_thirty_consecutive_high_cpu_samples(orchestrator) -> None:
    """单个容器 CPU 超限必须按单调时间持续完整 60 秒，不能按调用数猜测。"""

    policy = orchestrator.StopPolicy()
    for observed_at in range(0, 60, 2):
        assert (
            policy.evaluate(
                healthy_snapshot(orchestrator, max_container_cpu_percent=85.1),
                None,
                observed_at=float(observed_at),
            )
            is None
        )
    assert (
        policy.evaluate(
            healthy_snapshot(orchestrator, max_container_cpu_percent=85.1),
            None,
            observed_at=60.0,
        )
        == "high_cpu"
    )


def test_stop_policy_resets_high_cpu_window_after_observation_gap(orchestrator) -> None:
    """超过采样容差的未知区间不能被推断为 CPU 连续超限。"""

    policy = orchestrator.StopPolicy()
    high_cpu = healthy_snapshot(orchestrator, max_container_cpu_percent=90.0)

    assert policy.evaluate(high_cpu, None, observed_at=0.0) is None
    assert policy.evaluate(high_cpu, None, observed_at=61.0) is None
    for observed_at in range(63, 121, 2):
        assert policy.evaluate(high_cpu, None, observed_at=float(observed_at)) is None
    assert policy.evaluate(high_cpu, None, observed_at=121.0) == "high_cpu"


def test_monitored_loadgen_keeps_absolute_two_second_sample_schedule(
    orchestrator,
) -> None:
    """采样本身耗时不得叠加到下一次两秒间隔。"""

    clock = FakeClock()
    process = FakeLoadgenProcess(
        clock,
        complete_at=5.0,
        stdout=json.dumps(task2_report("rate-warmup-10")),
    )
    sample_starts: list[float] = []

    def slow_reader():
        sample_starts.append(clock.value)
        clock.sleep(0.5)
        return healthy_snapshot(orchestrator)

    outcome = orchestrator.run_monitored_loadgen(
        process=process,
        profile="rate-warmup-10",
        monitor=orchestrator.CapacityMonitor(slow_reader),
        policy=orchestrator.StopPolicy(),
        monotonic_fn=clock.monotonic,
        sleep_fn=clock.sleep,
    )

    assert outcome.stop_reason is None
    assert sample_starts == [0.0, 2.0, 4.0, 6.0]


def test_compose_config_rejects_ports_before_any_lifecycle_command(
    orchestrator, tmp_path: Path
) -> None:
    """结构预检失败时不允许 build/up/run/exec/stop 进入 Docker。"""

    root, env_file, docker_cli, _meminfo = capacity_repository(tmp_path)
    runner = orchestrator.DockerComposeCapacityRunner(
        env_file,
        repository_root=root,
        docker_cli=docker_cli,
        executor=fake_executor(orchestrator),
    )
    invalid = valid_compose_config(orchestrator, root)
    services = invalid["services"]
    assert isinstance(services, dict)
    services["api-synthetic"]["ports"] = ["8000:8000"]
    assert runner.validate_compose_config(invalid) is False
    assert runner.lifecycle_calls == []


def test_public_stack_comparison_is_three_state_and_detects_restart(orchestrator) -> None:
    """公网栈只能得到 unchanged、changed 或 not_verified，重启必为 changed。"""

    baseline = {
        name: (f"id-{name}", "healthy", 0, False)
        for name in orchestrator.PUBLIC_LONG_LIVED_SERVICES
    }
    assert orchestrator.compare_public_stack(baseline, baseline) == "unchanged"
    changed = dict(baseline)
    changed["api"] = ("id-api", "healthy", 1, False)
    assert orchestrator.compare_public_stack(baseline, changed) == "changed"
    assert orchestrator.compare_public_stack(None, baseline) == "not_verified"


def test_preflight_failure_uses_no_lifecycle_command(orchestrator, tmp_path: Path) -> None:
    """资源门失败后也不得为了清理而停止容量项目。"""

    root, env_file, docker_cli, _meminfo = capacity_repository(tmp_path)
    runner = orchestrator.DockerComposeCapacityRunner(
        env_file,
        repository_root=root,
        docker_cli=docker_cli,
        executor=fake_executor(orchestrator),
    )
    outcome = orchestrator.run_capacity_suite(
        runner=runner,
        resource_preflight=lambda: False,
        public_baseline=lambda: None,
        profile_runner=lambda _profile: target_report(),
        report_path=tmp_path / "outside.json",
        observe_seconds=0,
    )
    assert outcome["exit_code"] == 1
    assert runner.lifecycle_calls == []


def test_recovery_never_uses_down_v(orchestrator, tmp_path: Path) -> None:
    """紧急清理只能停止固定容量项目，不能删除卷或公网项目。"""

    root, env_file, docker_cli, _meminfo = capacity_repository(tmp_path)
    runner = orchestrator.DockerComposeCapacityRunner(
        env_file,
        repository_root=root,
        docker_cli=docker_cli,
        executor=fake_executor(orchestrator),
    )
    runner.stop_capacity_project()
    commands = [" ".join(call) for call in runner.lifecycle_calls]
    assert all("campus-ai-dingtalk-test" not in command for command in commands)
    assert all("down -v" not in command for command in commands)


def test_report_is_owner_only_outside_repo_and_rejects_sensitive_value(
    orchestrator, tmp_path: Path
) -> None:
    """报告以 O_EXCL/0600 写入，任何敏感异常文本都不落盘。"""

    report_path = tmp_path / "report.json"
    payload = suite_report(1)
    orchestrator.write_report(report_path, payload, repository_root=PROJECT_ROOT)
    assert json.loads(report_path.read_text(encoding="utf-8")) == payload
    if os.name != "nt":
        assert stat.S_IMODE(report_path.stat().st_mode) == 0o600
    with pytest.raises(ValueError):
        orchestrator.write_report(
            tmp_path / "unsafe.json", {"message": "token=secret"}, repository_root=PROJECT_ROOT
        )


def test_preflight_only_returns_success_without_lifecycle_or_report(
    orchestrator, tmp_path: Path
) -> None:
    """独立预检只允许只读检查，成功时不要求报告路径。"""

    root, env_file, docker_cli, _meminfo = capacity_repository(tmp_path)
    runner = orchestrator.DockerComposeCapacityRunner(
        env_file,
        repository_root=root,
        docker_cli=docker_cli,
        executor=fake_executor(orchestrator),
    )
    config = valid_compose_config(orchestrator, root)
    code = orchestrator.run_cli(
        ["--confirm-project", orchestrator.CAPACITY_PROJECT, "--preflight-only"],
        runner=runner,
        resource_preflight=lambda: True,
        config_loader=lambda: config,
        public_baseline=lambda: {
            name: (f"id-{name}", "healthy", 0, False)
            for name in orchestrator.PUBLIC_LONG_LIVED_SERVICES
        },
    )
    assert code == 0
    assert runner.lifecycle_calls == []


def test_preflight_default_failure_has_zero_lifecycle(orchestrator, tmp_path: Path) -> None:
    """默认预检资源读取失败时不得以 stop 作为清理。"""

    root, env_file, docker_cli, meminfo = capacity_repository(tmp_path)
    meminfo.write_text(
        "MemTotal:       1000 kB\nMemAvailable:    299 kB\n",
        encoding="ascii",
    )
    runner = orchestrator.DockerComposeCapacityRunner(
        env_file,
        repository_root=root,
        docker_cli=docker_cli,
        meminfo_path=meminfo,
        executor=fake_executor(orchestrator),
    )
    assert runner.preflight_only() == 1
    assert runner.lifecycle_calls == []


def test_loadgen_report_requires_one_exact_task2_json_object(orchestrator) -> None:
    """loadgen stdout 只能是一个精确 Task2 schema，profile 必须与调用值一致。"""

    report = task2_report()
    encoded = json.dumps(report)
    assert orchestrator.parse_loadgen_report(encoded, "rate-100") == report

    invalid_values = (
        encoded + "\n{}",
        json.dumps({**report, "extra": 1}),
        json.dumps({**report, "schema_version": "other"}),
        json.dumps({**report, "profile": "concurrency-30"}),
        "not-json",
        "[]",
    )
    for invalid in invalid_values:
        with pytest.raises(ValueError, match="loadgen report"):
            orchestrator.parse_loadgen_report(invalid, "rate-100")


def test_start_loadgen_uses_fixed_async_argv_and_minimal_environment(
    orchestrator, tmp_path: Path
) -> None:
    """异步 loadgen 只能由固定 Compose run argv 启动。"""

    root, env_file, docker_cli, _meminfo = capacity_repository(tmp_path)
    clock = FakeClock()
    process = FakeLoadgenProcess(
        clock,
        complete_at=0,
        stdout=json.dumps(task2_report("concurrency-30")),
    )
    calls: list[tuple[tuple[str, ...], dict[str, str]]] = []

    def factory(argv: Sequence[str], environment: Mapping[str, str]):
        calls.append((tuple(argv), dict(environment)))
        return process

    runner = orchestrator.DockerComposeCapacityRunner(
        env_file,
        repository_root=root,
        docker_cli=docker_cli,
        executor=fake_executor(orchestrator),
        process_factory=factory,
    )

    assert runner.start_loadgen("concurrency-30") is process
    assert calls == [
        (
            runner.compose_argv(
                "--profile",
                "capacity-loadgen",
                "run",
                "--rm",
                "--name",
                orchestrator.LOADGEN_CONTAINER_NAME,
                "--label",
                runner.loadgen_owner_pair,
                "--no-deps",
                "loadgen",
                "--profile",
                "concurrency-30",
            ),
            runner.minimal_environment(),
        )
    ]
    assert runner.lifecycle_calls == [calls[0][0]]


def test_progress_line_parser_requires_fixed_prefix_exact_schema_and_profile(
    orchestrator,
) -> None:
    """宿主只接受固定前缀和四键 schema；普通 Docker 噪声直接丢弃。"""

    prefix = orchestrator.PROGRESS_LINE_PREFIX
    valid = prefix + json.dumps(
        {
            "schema_version": orchestrator.PROGRESS_SCHEMA_VERSION,
            "profile": "rate-warmup-10",
            "event": "server_5xx",
            "detail": None,
        },
        separators=(",", ":"),
    )

    assert orchestrator.parse_progress_line(valid, "rate-warmup-10") == (
        orchestrator.ProgressEvent("rate-warmup-10", "server_5xx", None)
    )
    assert orchestrator.parse_progress_line("compose warning", "rate-warmup-10") is None
    with pytest.raises(ValueError, match="progress"):
        orchestrator.parse_progress_line(
            valid.replace("rate-warmup-10", "rate-warmup-30"),
            "rate-warmup-10",
        )
    with pytest.raises(ValueError, match="progress"):
        orchestrator.parse_progress_line(
            prefix
            + json.dumps(
                {
                    "schema_version": orchestrator.PROGRESS_SCHEMA_VERSION,
                    "profile": "rate-warmup-10",
                    "event": "server_5xx",
                    "detail": None,
                    "extra": "forbidden",
                }
            ),
            "rate-warmup-10",
        )


def test_subprocess_progress_reader_keeps_stdout_separate_and_discards_noise(
    orchestrator,
) -> None:
    """真实 stderr pipe 异步到达时只保留协议事件，stdout 最终 JSON 独立可读。"""

    progress = orchestrator.PROGRESS_LINE_PREFIX + json.dumps(
        {
            "schema_version": orchestrator.PROGRESS_SCHEMA_VERSION,
            "profile": "rate-warmup-10",
            "event": "server_5xx",
            "detail": None,
        },
        separators=(",", ":"),
    )
    child = (
        "import sys,time;"
        "sys.stderr.write('compose-noise token=private\\n');sys.stderr.flush();"
        "time.sleep(0.05);"
        f"sys.stderr.write({progress!r}+'\\n');sys.stderr.flush();"
        f"sys.stdout.write({json.dumps(task2_report('rate-warmup-10'))!r})"
    )
    raw_process = subprocess.Popen(
        (sys.executable, "-c", child),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    process = orchestrator.SubprocessLoadgenProcess(
        raw_process,
        "rate-warmup-10",
    )

    process.wait_for_activity(1.0)
    events = process.drain_progress()
    returncode = process.wait(1.0)
    events += process.drain_progress(final=True)

    assert returncode == 0
    assert events == (orchestrator.ProgressEvent("rate-warmup-10", "server_5xx", None),)
    assert process.progress_protocol_error() is False
    assert process.read_stdout() == json.dumps(task2_report("rate-warmup-10"))


@pytest.mark.parametrize(
    ("event", "detail", "expected_reason"),
    (
        ("server_5xx", None, "server_error"),
        ("transport_timeout", None, "transport_timeout"),
        ("identity_4xx", None, "identity_error"),
        ("token_anomaly", "second_refresh", "token_anomaly"),
    ),
)
def test_monitored_loadgen_stops_immediately_on_safe_progress_event(
    orchestrator,
    event: str,
    detail: str | None,
    expected_reason: str,
) -> None:
    """实时事件到达时不等待下一次两秒资源采样，立即终止 one-off。"""

    clock = FakeClock()
    process = FakeLoadgenProcess(clock, complete_at=100.0, stdout="")
    process.progress_events.append(orchestrator.ProgressEvent("rate-warmup-10", event, detail))
    samples: list[float] = []
    monitor = orchestrator.CapacityMonitor(
        lambda: samples.append(clock.value) or healthy_snapshot(orchestrator)
    )

    outcome = orchestrator.run_monitored_loadgen(
        process=process,
        profile="rate-warmup-10",
        monitor=monitor,
        policy=orchestrator.StopPolicy(),
        monotonic_fn=clock.monotonic,
        sleep_fn=clock.sleep,
    )

    assert outcome == orchestrator.LoadgenOutcome(None, expected_reason)
    assert samples == []
    assert clock.value == 0.0
    assert "terminate" in process.events


def test_monitored_loadgen_fails_closed_on_progress_protocol_error(orchestrator) -> None:
    """声称使用协议却破坏 schema 的 stderr 必须失败关闭且不进入采样。"""

    clock = FakeClock()
    process = FakeLoadgenProcess(clock, complete_at=100.0, stdout="")
    process.progress_error = True
    samples: list[float] = []

    outcome = orchestrator.run_monitored_loadgen(
        process=process,
        profile="rate-warmup-10",
        monitor=orchestrator.CapacityMonitor(
            lambda: samples.append(clock.value) or healthy_snapshot(orchestrator)
        ),
        policy=orchestrator.StopPolicy(),
        monotonic_fn=clock.monotonic,
        sleep_fn=clock.sleep,
    )

    assert outcome == orchestrator.LoadgenOutcome(None, "runner_failed")
    assert samples == []
    assert "terminate" in process.events


def test_realtime_progress_preempts_blocking_resource_sample_within_two_seconds(
    orchestrator,
) -> None:
    """5xx 在同步资源读取期间须两秒内发起终止，且返回前回收监督任务。"""

    class AsyncProgressProcess(FakeLoadgenProcess):
        def __init__(self, clock: FakeClock) -> None:
            super().__init__(clock, complete_at=100.0, stdout="")
            self.activity = threading.Event()
            self.terminated = False
            self.terminated_at: float | None = None

        def publish(self) -> None:
            time.sleep(0.05)
            self.progress_events.append(
                orchestrator.ProgressEvent("rate-warmup-10", "server_5xx", None)
            )
            self.activity.set()

        def poll(self) -> int | None:
            self.events.append("poll")
            return -15 if self.terminated else None

        def terminate(self) -> None:
            self.events.append("terminate")
            self.terminated = True
            self.terminated_at = time.perf_counter()
            self.activity.set()

        def wait_for_activity(self, timeout: float) -> None:
            self.activity.wait(timeout)
            self.activity.clear()

    process = AsyncProgressProcess(FakeClock())
    publisher = threading.Thread(target=process.publish, daemon=True)
    publisher.start()
    reader_finished = threading.Event()
    monitor = orchestrator.CapacityMonitor(
        lambda: (
            time.sleep(0.25),
            reader_finished.set(),
            healthy_snapshot(orchestrator),
        )[-1]
    )

    started = time.perf_counter()
    outcome = orchestrator.run_monitored_loadgen(
        process=process,
        profile="rate-warmup-10",
        monitor=monitor,
        policy=orchestrator.StopPolicy(),
        timeout_seconds=10.0,
    )

    assert outcome == orchestrator.LoadgenOutcome(None, "server_error")
    assert process.terminated_at is not None
    assert process.terminated_at - started < 2.0
    assert "terminate" in process.events
    assert reader_finished.is_set()


def test_token_metrics_anomaly_end_to_end_initiates_terminate_within_two_seconds(
    orchestrator,
) -> None:
    """真实 watcher→stderr pipe→宿主监督链在最坏轮询超时后仍须两秒内发起终止。"""

    task2_path = PROJECT_ROOT / "tests/load/dingtalk_capacity_runner.py"
    child = f"""
import asyncio
import importlib.util
import sys

spec = importlib.util.spec_from_file_location("capacity_task2_child", {str(task2_path)!r})
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)

class Sender:
    def __init__(self):
        self.calls = 0

    async def metrics(self):
        self.calls += 1
        if self.calls == 1:
            return {{"token_events": {{}}}}
        await asyncio.Event().wait()

async def main():
    reporter = module.ProgressReporter("rate-warmup-10")
    stop = asyncio.Event()
    await module._watch_token_progress(Sender(), reporter, stop)
    await asyncio.Event().wait()

asyncio.run(main())
"""

    class RecordingProcess(orchestrator.SubprocessLoadgenProcess):
        def __init__(self, process: subprocess.Popen[str]) -> None:
            super().__init__(process, "rate-warmup-10")
            self.terminate_at: float | None = None

        def terminate(self) -> None:
            self.terminate_at = time.perf_counter()
            super().terminate()

    raw_process = subprocess.Popen(
        (sys.executable, "-c", child),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    process = RecordingProcess(raw_process)
    started = time.perf_counter()
    try:
        outcome = orchestrator.run_monitored_loadgen(
            process=process,
            profile="rate-warmup-10",
            monitor=orchestrator.CapacityMonitor(
                lambda: time.sleep(1.6) or healthy_snapshot(orchestrator)
            ),
            policy=orchestrator.StopPolicy(),
            timeout_seconds=5.0,
        )
    finally:
        if raw_process.poll() is None:
            raw_process.kill()
        raw_process.wait(timeout=2.0)

    assert outcome.stop_reason == "token_anomaly"
    assert process.terminate_at is not None
    assert process.terminate_at - started < 2.0


def test_terminate_loadgen_reports_unconfirmed_exit(orchestrator) -> None:
    """terminate 与 kill 后仍不能 wait 成功时，调用方必须得到未确认状态。"""

    class NeverExitsProcess(FakeLoadgenProcess):
        def wait(self, timeout: float) -> int:
            self.events.append(f"wait:{timeout}")
            raise TimeoutError

    clock = FakeClock()
    process = NeverExitsProcess(clock, complete_at=100.0, stdout="")
    process.progress_events.append(orchestrator.ProgressEvent("rate-warmup-10", "server_5xx", None))

    outcome = orchestrator.run_monitored_loadgen(
        process=process,
        profile="rate-warmup-10",
        monitor=orchestrator.CapacityMonitor(lambda: healthy_snapshot(orchestrator)),
        policy=orchestrator.StopPolicy(),
        monotonic_fn=clock.monotonic,
        sleep_fn=clock.sleep,
    )

    assert outcome.stop_reason == "server_error"
    assert outcome.termination_confirmed is False
    assert process.events.count("terminate") == 1
    assert process.events.count("kill") == 1


def test_runner_stops_and_verifies_only_labeled_capacity_loadgen(
    orchestrator, tmp_path: Path
) -> None:
    """running one-off 必须先精确 inspect/stop，再删除合法残留并确认全状态归零。"""

    root, env_file, docker_cli, _meminfo = capacity_repository(tmp_path)
    runner = orchestrator.DockerComposeCapacityRunner(
        env_file,
        repository_root=root,
        docker_cli=docker_cli,
        executor=fake_executor(orchestrator),
    )
    identifier = "a" * 64
    inspected_running = "\t".join(
        (
            identifier,
            f"/{orchestrator.LOADGEN_CONTAINER_NAME}",
            orchestrator.CAPACITY_PROJECT,
            "loadgen",
            "True",
            runner.loadgen_owner_value,
            "true",
            "true",
        )
    )
    inspected_stopped = inspected_running.rsplit("\t", 1)[0] + "\tfalse"
    executor = RecordingExecutor(
        orchestrator,
        responses=(
            (0, identifier + "\n"),
            (0, inspected_running + "\n"),
            (0, ""),
            (0, identifier + "\n"),
            (0, inspected_stopped + "\n"),
            (0, ""),
            (0, ""),
        ),
    )
    runner.executor = executor

    result = runner.ensure_loadgen_stopped()

    assert result.returncode == 0
    commands = [call[0] for call in executor.calls]
    assert commands[0] == runner.loadgen_ps_argv()
    assert commands[1] == runner.loadgen_inspect_argv(identifier)
    assert commands[2] == (
        str(docker_cli),
        "container",
        "stop",
        "--time",
        "5",
        identifier,
    )
    assert commands[3] == runner.loadgen_ps_argv()
    assert commands[4] == runner.loadgen_inspect_argv(identifier)
    assert commands[5] == (
        str(docker_cli),
        "container",
        "rm",
        "--force",
        identifier,
    )
    assert commands[6] == runner.loadgen_ps_argv()
    assert "--all" in commands[0]
    assert "--no-trunc" in commands[0]
    assert f"label=com.docker.compose.project={orchestrator.CAPACITY_PROJECT}" in commands[0]
    assert "label=com.docker.compose.service=loadgen" in commands[0]
    assert orchestrator.LOADGEN_OWNER_LABEL in " ".join(commands[0])
    assert "com.docker.compose.oneoff=True" in " ".join(commands[0])
    assert "--volumes" not in commands[5] and "-v" not in commands[5]
    assert all(orchestrator.PUBLIC_PROJECT not in " ".join(command) for command in commands)


def test_runner_accepts_owned_autoremove_disappearing_before_inspect(
    orchestrator, tmp_path: Path, monkeypatch
) -> None:
    """已列出的自有 AutoRemove one-off 在 inspect 前消失时应复核为空。"""

    root, env_file, docker_cli, _meminfo = capacity_repository(tmp_path)
    identifier = "a" * 64
    executor = RecordingExecutor(
        orchestrator,
        responses=((0, identifier + "\n"), (1, ""), (0, "")),
    )
    runner = orchestrator.DockerComposeCapacityRunner(
        env_file,
        repository_root=root,
        docker_cli=docker_cli,
        executor=executor,
    )
    retry_sleeps: list[float] = []
    monkeypatch.setattr(orchestrator, "sleep", retry_sleeps.append)

    assert runner.ensure_loadgen_stopped().returncode == 0
    assert [call[0] for call in executor.calls] == [
        runner.loadgen_ps_argv(),
        runner.loadgen_inspect_argv(identifier),
        runner.loadgen_ps_argv(),
    ]
    assert runner.lifecycle_calls == []
    assert retry_sleeps == [0.1]


def test_runner_rejects_repeated_owned_loadgen_inspect_error(orchestrator, tmp_path: Path) -> None:
    """候选 one-off 连续可见但 inspect 连续失败时必须失败关闭。"""

    root, env_file, docker_cli, _meminfo = capacity_repository(tmp_path)
    identifier = "a" * 64
    executor = RecordingExecutor(
        orchestrator,
        responses=tuple(
            response for _attempt in range(4) for response in ((0, identifier + "\n"), (1, ""))
        ),
    )
    runner = orchestrator.DockerComposeCapacityRunner(
        env_file,
        repository_root=root,
        docker_cli=docker_cli,
        executor=executor,
    )

    assert runner.ensure_loadgen_stopped().returncode == 2
    assert runner.lifecycle_calls == []
    assert len(executor.calls) == 8


def test_each_runner_uses_a_unique_loadgen_owner_nonce(orchestrator, tmp_path: Path) -> None:
    """不同 suite 不能共享 owner 值，避免首轮 discover 误删另一轮 one-off。"""

    root, env_file, docker_cli, _meminfo = capacity_repository(tmp_path)
    first = orchestrator.DockerComposeCapacityRunner(
        env_file,
        repository_root=root,
        docker_cli=docker_cli,
        executor=fake_executor(orchestrator),
    )
    second = orchestrator.DockerComposeCapacityRunner(
        env_file,
        repository_root=root,
        docker_cli=docker_cli,
        executor=fake_executor(orchestrator),
    )

    assert first.loadgen_owner_value != second.loadgen_owner_value
    assert first.loadgen_owner_pair != second.loadgen_owner_pair
    assert f"label={first.loadgen_owner_pair}" in first.loadgen_ps_argv()
    assert f"label={second.loadgen_owner_pair}" in second.loadgen_ps_argv()


def test_runner_refuses_loadgen_owned_by_another_suite_nonce(orchestrator, tmp_path: Path) -> None:
    """即使名称和 Compose 标签相同，另一 suite nonce 也不得取得删除权。"""

    root, env_file, docker_cli, _meminfo = capacity_repository(tmp_path)
    identifier = "9" * 64
    runner = orchestrator.DockerComposeCapacityRunner(
        env_file,
        repository_root=root,
        docker_cli=docker_cli,
        executor=fake_executor(orchestrator),
    )
    other = orchestrator.DockerComposeCapacityRunner(
        env_file,
        repository_root=root,
        docker_cli=docker_cli,
        executor=fake_executor(orchestrator),
    )
    inspected = "\t".join(
        (
            identifier,
            f"/{orchestrator.LOADGEN_CONTAINER_NAME}",
            orchestrator.CAPACITY_PROJECT,
            "loadgen",
            "True",
            other.loadgen_owner_value,
            "true",
            "true",
        )
    )
    executor = RecordingExecutor(
        orchestrator,
        responses=((0, identifier + "\n"), (0, inspected + "\n")),
    )
    runner.executor = executor

    assert runner.ensure_loadgen_stopped().returncode == 2
    assert runner.lifecycle_calls == []


def test_runner_force_removes_validated_loadgen_when_graceful_stop_fails(
    orchestrator, tmp_path: Path
) -> None:
    """精确 stop 失败后须重查合法归属，再 rm --force 且最终确认归零。"""

    root, env_file, docker_cli, _meminfo = capacity_repository(tmp_path)
    runner = orchestrator.DockerComposeCapacityRunner(
        env_file,
        repository_root=root,
        docker_cli=docker_cli,
        executor=fake_executor(orchestrator),
    )
    identifier = "b" * 64
    inspected = "\t".join(
        (
            identifier,
            f"/{orchestrator.LOADGEN_CONTAINER_NAME}",
            orchestrator.CAPACITY_PROJECT,
            "loadgen",
            "True",
            runner.loadgen_owner_value,
            "true",
            "true",
        )
    )
    executor = RecordingExecutor(
        orchestrator,
        responses=(
            (0, identifier + "\n"),
            (0, inspected + "\n"),
            (1, ""),
            (0, identifier + "\n"),
            (0, inspected + "\n"),
            (0, ""),
            (0, ""),
        ),
    )
    runner.executor = executor

    result = runner.ensure_loadgen_stopped()

    assert result.returncode == 0
    assert executor.calls[5][0] == (
        str(docker_cli),
        "container",
        "rm",
        "--force",
        identifier,
    )
    assert executor.calls[6][0] == runner.loadgen_ps_argv()


@pytest.mark.parametrize(
    ("field_index", "invalid_value"),
    (
        (0, "d" * 64),
        (1, "/unowned"),
        (2, "other-project"),
        (3, "web"),
        (4, "False"),
        (5, "foreign"),
        (6, "false"),
        (7, "unknown"),
    ),
)
def test_runner_refuses_loadgen_with_mismatched_precise_inspection(
    orchestrator,
    tmp_path: Path,
    field_index: int,
    invalid_value: str,
) -> None:
    """候选容器任一归属/名称/AutoRemove 字段不符时绝不执行生命周期命令。"""

    root, env_file, docker_cli, _meminfo = capacity_repository(tmp_path)
    runner = orchestrator.DockerComposeCapacityRunner(
        env_file,
        repository_root=root,
        docker_cli=docker_cli,
        executor=fake_executor(orchestrator),
    )
    identifier = "c" * 64
    fields = [
        identifier,
        f"/{orchestrator.LOADGEN_CONTAINER_NAME}",
        orchestrator.CAPACITY_PROJECT,
        "loadgen",
        "True",
        runner.loadgen_owner_value,
        "true",
        "true",
    ]
    fields[field_index] = invalid_value
    mismatched = "\t".join(fields)
    executor = RecordingExecutor(
        orchestrator,
        responses=((0, identifier + "\n"), (0, mismatched + "\n")),
    )
    runner.executor = executor

    result = runner.ensure_loadgen_stopped()

    assert result.returncode == 2
    assert runner.lifecycle_calls == []
    assert len(executor.calls) == 2


def test_runner_removes_stopped_owned_loadgen_without_stop(orchestrator, tmp_path: Path) -> None:
    """exited/stopped 残留也须被全状态发现，复核后直接 rm --force。"""

    root, env_file, docker_cli, _meminfo = capacity_repository(tmp_path)
    runner = orchestrator.DockerComposeCapacityRunner(
        env_file,
        repository_root=root,
        docker_cli=docker_cli,
        executor=fake_executor(orchestrator),
    )
    identifier = "d" * 64
    inspected = "\t".join(
        (
            identifier,
            f"/{orchestrator.LOADGEN_CONTAINER_NAME}",
            orchestrator.CAPACITY_PROJECT,
            "loadgen",
            "True",
            runner.loadgen_owner_value,
            "true",
            "false",
        )
    )
    executor = RecordingExecutor(
        orchestrator,
        responses=(
            (0, identifier + "\n"),
            (0, inspected + "\n"),
            (0, identifier + "\n"),
            (0, inspected + "\n"),
            (0, ""),
            (0, ""),
        ),
    )
    runner.executor = executor

    assert runner.ensure_loadgen_stopped().returncode == 0
    lifecycle = runner.lifecycle_calls
    assert lifecycle == [
        (
            str(docker_cli),
            "container",
            "rm",
            "--force",
            identifier,
        )
    ]


def test_runner_refuses_to_remove_replaced_loadgen_after_stop(orchestrator, tmp_path: Path) -> None:
    """stop 后固定名称若指向新 ID，必须按 TOCTOU 失败关闭且不删除新容器。"""

    root, env_file, docker_cli, _meminfo = capacity_repository(tmp_path)
    runner = orchestrator.DockerComposeCapacityRunner(
        env_file,
        repository_root=root,
        docker_cli=docker_cli,
        executor=fake_executor(orchestrator),
    )
    original, replacement = "e" * 64, "f" * 64

    def inspected(identifier: str, running: str) -> str:
        return "\t".join(
            (
                identifier,
                f"/{orchestrator.LOADGEN_CONTAINER_NAME}",
                orchestrator.CAPACITY_PROJECT,
                "loadgen",
                "True",
                runner.loadgen_owner_value,
                "true",
                running,
            )
        )

    executor = RecordingExecutor(
        orchestrator,
        responses=(
            (0, original + "\n"),
            (0, inspected(original, "true") + "\n"),
            (0, ""),
            (0, replacement + "\n"),
            (0, inspected(replacement, "false") + "\n"),
        ),
    )
    runner.executor = executor

    assert runner.ensure_loadgen_stopped().returncode == 2
    assert all("rm" not in command for command in runner.lifecycle_calls)


@pytest.mark.parametrize("final_ps_fails", (False, True))
def test_runner_requires_successful_final_ps_and_zero_candidates(
    orchestrator,
    tmp_path: Path,
    final_ps_fails: bool,
) -> None:
    """rm 后最终 ps 失败或仍有合法候选都必须返回 2。"""

    root, env_file, docker_cli, _meminfo = capacity_repository(tmp_path)
    runner = orchestrator.DockerComposeCapacityRunner(
        env_file,
        repository_root=root,
        docker_cli=docker_cli,
        executor=fake_executor(orchestrator),
    )
    identifier = "a" * 64
    inspected = "\t".join(
        (
            identifier,
            f"/{orchestrator.LOADGEN_CONTAINER_NAME}",
            orchestrator.CAPACITY_PROJECT,
            "loadgen",
            "True",
            runner.loadgen_owner_value,
            "true",
            "false",
        )
    )
    responses: list[tuple[int, str]] = [
        (0, identifier + "\n"),
        (0, inspected + "\n"),
        (0, identifier + "\n"),
        (0, inspected + "\n"),
        (0, ""),
        (1, "") if final_ps_fails else (0, identifier + "\n"),
    ]
    if not final_ps_fails:
        responses.append((0, inspected + "\n"))
    runner.executor = RecordingExecutor(orchestrator, responses)

    assert runner.ensure_loadgen_stopped().returncode == 2


def test_monitored_loadgen_samples_every_two_seconds_and_parses_report(
    orchestrator,
) -> None:
    """loadgen 运行时使用单调时钟每两秒采样，结束后才读取 stdout。"""

    clock = FakeClock()
    process = FakeLoadgenProcess(
        clock,
        complete_at=5.0,
        stdout=json.dumps(task2_report("rate-warmup-10")),
    )
    samples: list[float] = []
    monitor = orchestrator.CapacityMonitor(
        lambda: samples.append(clock.value) or healthy_snapshot(orchestrator, concurrency_leases=0)
    )

    outcome = orchestrator.run_monitored_loadgen(
        process=process,
        profile="rate-warmup-10",
        monitor=monitor,
        policy=orchestrator.StopPolicy(),
        monotonic_fn=clock.monotonic,
        sleep_fn=clock.sleep,
    )

    assert outcome.stop_reason is None
    assert outcome.report == task2_report("rate-warmup-10")
    assert samples == [0.0, 2.0, 4.0, 6.0]
    assert "stdout" in process.events
    assert process.events.index("stdout") > process.events.index("wait:0.0")


def test_monitored_loadgen_uses_fresh_snapshot_after_process_exit(orchestrator) -> None:
    """进程退出后不得复用仍有租约的旧两秒采样。"""

    clock = FakeClock()
    process = FakeLoadgenProcess(
        clock,
        complete_at=2.0,
        stdout=json.dumps(task2_report("rate-warmup-10")),
    )
    leases = iter((1, 0))
    monitor = orchestrator.CapacityMonitor(
        lambda: healthy_snapshot(orchestrator, concurrency_leases=next(leases))
    )

    outcome = orchestrator.run_monitored_loadgen(
        process=process,
        profile="rate-warmup-10",
        monitor=monitor,
        policy=orchestrator.StopPolicy(),
        monotonic_fn=clock.monotonic,
        sleep_fn=clock.sleep,
    )

    assert outcome.stop_reason is None
    assert outcome.report == task2_report("rate-warmup-10")


def test_monitored_loadgen_preserves_safe_gate_report_from_exit_one(orchestrator) -> None:
    """Task2 gate 失败的 rc=1 仍须保留严格 JSON 聚合证据。"""

    clock = FakeClock()
    failed_report = task2_report(
        "rate-warmup-10",
        passed=False,
        stop_reason="rate_timeout",
    )
    process = FakeLoadgenProcess(
        clock,
        complete_at=0.0,
        stdout=json.dumps(failed_report),
        returncode=1,
    )

    outcome = orchestrator.run_monitored_loadgen(
        process=process,
        profile="rate-warmup-10",
        monitor=orchestrator.CapacityMonitor(lambda: healthy_snapshot(orchestrator)),
        policy=orchestrator.StopPolicy(),
        monotonic_fn=clock.monotonic,
        sleep_fn=clock.sleep,
    )

    assert outcome.report == failed_report
    assert outcome.stop_reason == "runner_failed"


def test_monitored_loadgen_timeout_terminates_waits_then_kills(orchestrator) -> None:
    """场景固定总墙钟到期时先 terminate/wait，必要时 kill/wait。"""

    clock = FakeClock()
    process = FakeLoadgenProcess(
        clock,
        complete_at=10_000,
        stdout="",
        wait_times_out_once=True,
    )
    monitor = orchestrator.CapacityMonitor(lambda: healthy_snapshot(orchestrator))

    outcome = orchestrator.run_monitored_loadgen(
        process=process,
        profile="concurrency-warmup-5",
        monitor=monitor,
        policy=orchestrator.StopPolicy(),
        monotonic_fn=clock.monotonic,
        sleep_fn=clock.sleep,
        timeout_seconds=4.0,
    )

    assert outcome.report is None
    assert outcome.stop_reason == "loadgen_timeout"
    wait_event = f"wait:{orchestrator.LOADGEN_TERMINATE_WAIT_SECONDS}"
    assert process.events[-4:] == ["terminate", wait_event, "kill", wait_event]


def test_stop_policy_applies_latency_only_after_thirty_attempts(orchestrator) -> None:
    """延迟停止线在 attempted 少于 30 时不得触发。"""

    policy = orchestrator.StopPolicy()
    small = task2_report(
        "rate-warmup-10",
        attempted=10,
        p95_ms=9_000.0,
        p99_ms=10_000.0,
    )
    assert policy.evaluate(healthy_snapshot(orchestrator), small) is None
    large = task2_report(
        "rate-100",
        attempted=30,
        p95_ms=3_001.0,
        p99_ms=4_000.0,
    )
    assert policy.evaluate(healthy_snapshot(orchestrator), large) == "latency_limit"


@pytest.mark.parametrize(
    ("changes", "expected"),
    (
        ({"status_counts": {"500": 1}}, "server_error"),
        ({"unexpected_failures": 1}, "runner_failed"),
        ({"stop_reason": "rate_timeout"}, "runner_failed"),
    ),
)
def test_stop_policy_stops_on_server_timeout_or_identity_failure(
    orchestrator, changes: dict[str, object], expected: str
) -> None:
    """5xx、超时及身份链路失败均属于硬停止。"""

    assert (
        orchestrator.StopPolicy().evaluate(
            healthy_snapshot(orchestrator),
            task2_report("rate-100", **changes),
        )
        == expected
    )


@pytest.mark.parametrize(
    "event",
    ("refresh_failed", "wait_timeout", "cache_invalid", "lock_expired", "invalidated"),
)
def test_stop_policy_rejects_every_token_anomaly(orchestrator, event: str) -> None:
    """五类 Token 协调异常任一非零都必须停止。"""

    report = task2_report("rate-100", token_events={"hit": 99, event: 1})
    assert (
        orchestrator.StopPolicy().evaluate(healthy_snapshot(orchestrator), report)
        == "token_anomaly"
    )


def test_stop_policy_allows_one_warm_initialization_and_one_cold_refresh(
    orchestrator,
) -> None:
    """空隔离 Redis 可先初始化一次；之后仅 cold-cache 可再刷新一次。"""

    policy = orchestrator.StopPolicy()
    initial_refresh = task2_report(
        "rate-warmup-10",
        token_events={"hit": 9, "miss": 1, "refresh_success": 1},
        external_token_fetches=1,
    )
    assert (
        policy.evaluate(
            healthy_snapshot(orchestrator),
            initial_refresh,
        )
        is None
    )
    unexpected_second_refresh = task2_report(
        "rate-warmup-30",
        token_events={"hit": 29, "miss": 1, "refresh_success": 1},
        external_token_fetches=1,
    )
    assert (
        policy.evaluate(
            healthy_snapshot(orchestrator),
            unexpected_second_refresh,
        )
        == "token_anomaly"
    )
    assert (
        policy.evaluate(
            healthy_snapshot(orchestrator),
            task2_report("cold-cache-30"),
        )
        is None
    )


def test_profile_recovery_requires_zero_concurrency_leases_before_timeout(
    orchestrator,
) -> None:
    """场景进程退出后等待三十秒仍不归零才进入紧急停止。"""

    clock = FakeClock()
    monitor = orchestrator.CapacityMonitor(
        lambda: healthy_snapshot(orchestrator, concurrency_leases=1)
    )
    assert (
        orchestrator._wait_for_zero_leases(
            monitor,
            orchestrator.StopPolicy(),
            monotonic_fn=clock.monotonic,
            sleep_fn=clock.sleep,
        )
        == "concurrency_leases"
    )
    assert clock.value == 30.0


def test_capacity_suite_runs_exact_lifecycle_order_and_writes_strict_report(
    orchestrator, tmp_path: Path
) -> None:
    """正常 suite 必须 preflight→build→up→固定12场景→恢复→stop→公网比较。"""

    clock = FakeClock()
    runner = FakeSuiteRunner(orchestrator, clock)
    sample_times: list[float] = []
    monitor = orchestrator.CapacityMonitor(
        lambda: (
            sample_times.append(clock.value) or healthy_snapshot(orchestrator, concurrency_leases=0)
        )
    )
    report_path = tmp_path / "capacity-report.json"

    result = orchestrator.run_capacity_suite(
        runner=runner,
        report_path=report_path,
        monitor=monitor,
        monotonic_fn=clock.monotonic,
        sleep_fn=clock.sleep,
        observe_seconds=300.0,
    )

    assert runner.events[:3] == ["preflight", "build", "up"]
    assert [event for event in runner.events if event.startswith("run:")] == [
        f"run:{profile}" for profile in orchestrator.PROFILE_ORDER
    ]
    assert runner.events.count("loadgen_zero") == len(orchestrator.PROFILE_ORDER)
    for profile in orchestrator.PROFILE_ORDER:
        run_index = runner.events.index(f"run:{profile}")
        assert runner.events[run_index + 1] == "loadgen_zero"
    assert runner.events[-3:] == ["loadgen_verify", "stop", "public_after"]
    assert set(result) == {
        "exit_code",
        "schema_version",
        "profiles",
        "resource_peaks",
        "stop_reason",
        "recovery_state",
        "recovery_duration_seconds",
        "public_stack_comparison",
        "passed",
    }
    assert result["exit_code"] == 0
    assert result["stop_reason"] == "completed"
    assert result["recovery_state"] == "completed"
    assert result["public_stack_comparison"] == "unchanged"
    assert result["passed"] is True
    assert [item["profile"] for item in result["profiles"]] == list(orchestrator.PROFILE_ORDER)
    assert json.loads(report_path.read_text(encoding="utf-8")) == result
    assert len(sample_times) >= 150 + 12 + 3


def test_capacity_suite_recovers_each_profile_before_starting_the_next(
    orchestrator, tmp_path: Path
) -> None:
    """每个 profile 后都须等待租约并取得三次健康，API 场景也不能跳过。"""

    clock = FakeClock()
    runner = FakeSuiteRunner(orchestrator, clock)
    calls_by_profile: dict[str, int] = {}

    def read_snapshot():
        current = next(
            event.removeprefix("run:")
            for event in reversed(runner.events)
            if event.startswith("run:")
        )
        calls_by_profile[current] = calls_by_profile.get(current, 0) + 1
        if current == "rate-overload-121" and calls_by_profile[current] == 2:
            return healthy_snapshot(orchestrator, all_healthy=False)
        return healthy_snapshot(orchestrator)

    result = orchestrator.run_capacity_suite(
        runner=runner,
        report_path=tmp_path / "per-profile-recovery.json",
        monitor=orchestrator.CapacityMonitor(read_snapshot),
        monotonic_fn=clock.monotonic,
        sleep_fn=clock.sleep,
        observe_seconds=0.0,
    )

    runs = [event for event in runner.events if event.startswith("run:")]
    assert runs[-1] == "run:rate-overload-121"
    assert "run:concurrency-overload-31" not in runs
    assert result["exit_code"] == 1
    assert result["stop_reason"] == "unhealthy"
    assert result["recovery_state"] == "emergency_stopped"


def test_capacity_suite_hard_stop_terminates_loadgen_before_capacity_stop(
    orchestrator, tmp_path: Path
) -> None:
    """发流中硬停止必须先结束 loadgen，再停止且不伪造正常恢复。"""

    clock = FakeClock()
    runner = FakeSuiteRunner(
        orchestrator,
        clock,
        process_complete_after=100.0,
    )
    monitor = orchestrator.CapacityMonitor(
        lambda: healthy_snapshot(orchestrator, mem_available_ratio=0.29)
    )

    result = orchestrator.run_capacity_suite(
        runner=runner,
        report_path=tmp_path / "emergency.json",
        monitor=monitor,
        monotonic_fn=clock.monotonic,
        sleep_fn=clock.sleep,
        observe_seconds=300.0,
    )

    assert result["exit_code"] == 1
    assert result["stop_reason"] == "low_memory"
    assert result["recovery_state"] == "emergency_stopped"
    assert result["passed"] is False
    assert len(runner.processes) == 1
    assert runner.processes[0].events[-2:] == [
        "terminate",
        f"wait:{orchestrator.LOADGEN_TERMINATE_WAIT_SECONDS}",
    ]
    assert runner.events[-3:] == ["loadgen_verify", "stop", "public_after"]
    assert all(not event.startswith("run:rate-warmup-30") for event in runner.events)
    assert clock.value == 0.0


def test_capacity_suite_recovery_duration_includes_exact_loadgen_cleanup(
    orchestrator, tmp_path: Path
) -> None:
    """硬停止后的 recovery_duration 必须覆盖 profile one-off 精确清理耗时。"""

    class SlowCleanupRunner(FakeSuiteRunner):
        def ensure_loadgen_stopped(self):
            self.events.append("loadgen_zero")
            self.clock.sleep(7.0)
            return self.orchestrator.CommandResult(("loadgen-zero",), 0, "")

    clock = FakeClock()
    runner = SlowCleanupRunner(
        orchestrator,
        clock,
        process_complete_after=100.0,
    )
    result = orchestrator.run_capacity_suite(
        runner=runner,
        report_path=tmp_path / "cleanup-duration.json",
        monitor=orchestrator.CapacityMonitor(
            lambda: healthy_snapshot(orchestrator, mem_available_ratio=0.29)
        ),
        monotonic_fn=clock.monotonic,
        sleep_fn=clock.sleep,
        observe_seconds=0.0,
    )

    assert result["stop_reason"] == "low_memory"
    assert result["recovery_duration_seconds"] >= 7.0


def test_capacity_suite_fails_closed_when_process_exit_is_unconfirmed(
    orchestrator,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """即使容器精确清理成功，宿主进程退出未确认也不得报告已恢复。"""

    clock = FakeClock()
    runner = FakeSuiteRunner(orchestrator, clock)
    monkeypatch.setattr(
        orchestrator,
        "run_monitored_loadgen",
        lambda **_kwargs: orchestrator.LoadgenOutcome(
            None,
            "server_error",
            termination_confirmed=False,
            recovery_started_at=0.0,
        ),
    )

    result = orchestrator.run_capacity_suite(
        runner=runner,
        report_path=tmp_path / "unconfirmed-process.json",
        monitor=orchestrator.CapacityMonitor(lambda: healthy_snapshot(orchestrator)),
        monotonic_fn=clock.monotonic,
        sleep_fn=clock.sleep,
        observe_seconds=0.0,
    )

    assert result["exit_code"] == 2
    assert result["stop_reason"] == "stop_failed"
    assert result["recovery_state"] == "stop_failed"
    assert runner.events[-3:] == ["loadgen_verify", "stop", "public_after"]


def test_capacity_suite_profile_exception_runs_profile_and_suite_cleanup(
    orchestrator,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """监视器抛错时须先执行 profile finally，再执行 suite 级清理和 stop。"""

    clock = FakeClock()
    runner = FakeSuiteRunner(orchestrator, clock)
    monkeypatch.setattr(
        orchestrator,
        "run_monitored_loadgen",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("private")),
    )

    result = orchestrator.run_capacity_suite(
        runner=runner,
        report_path=tmp_path / "profile-exception.json",
        monitor=orchestrator.CapacityMonitor(lambda: healthy_snapshot(orchestrator)),
        monotonic_fn=clock.monotonic,
        sleep_fn=clock.sleep,
        observe_seconds=0.0,
    )

    assert result["stop_reason"] == "runner_failed"
    assert runner.events[-4:] == [
        "loadgen_zero",
        "loadgen_verify",
        "stop",
        "public_after",
    ]


def test_capacity_suite_finish_only_verifies_loadgen_zero_read_only(
    orchestrator, tmp_path: Path
) -> None:
    """每 profile 已清理后，suite 收尾只能只读复核，不能再次取得删除权。"""

    clock = FakeClock()
    runner = FakeSuiteRunner(orchestrator, clock)

    result = orchestrator.run_capacity_suite(
        runner=runner,
        report_path=tmp_path / "readonly-finish.json",
        monitor=orchestrator.CapacityMonitor(lambda: healthy_snapshot(orchestrator)),
        monotonic_fn=clock.monotonic,
        sleep_fn=clock.sleep,
        observe_seconds=0.0,
    )

    assert result["exit_code"] == 0
    assert runner.events.count("loadgen_zero") == len(orchestrator.PROFILE_ORDER)
    assert runner.events[-3:] == ["loadgen_verify", "stop", "public_after"]


def test_cleanup_failure_is_not_retried_destructively_during_suite_finish(
    orchestrator, tmp_path: Path
) -> None:
    """TOCTOU/所有权失败后 suite 只读复核，绝不再次 ensure 删除替换容器。"""

    clock = FakeClock()
    runner = FakeSuiteRunner(
        orchestrator,
        clock,
        loadgen_cleanup_code=2,
        loadgen_verify_code=2,
    )

    result = orchestrator.run_capacity_suite(
        runner=runner,
        report_path=tmp_path / "cleanup-ownership-failed.json",
        monitor=orchestrator.CapacityMonitor(lambda: healthy_snapshot(orchestrator)),
        monotonic_fn=clock.monotonic,
        sleep_fn=clock.sleep,
        observe_seconds=0.0,
    )

    assert result["stop_reason"] == "stop_failed"
    assert runner.events.count("loadgen_zero") == 1
    assert runner.events[-3:] == ["loadgen_verify", "stop", "public_after"]


def test_profile_and_cleanup_exceptions_become_fixed_stop_failed_result(
    orchestrator,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """profile 与 finally 同时抛错时不得互相覆盖或泄露，只返回固定 stop_failed。"""

    class RaisingCleanupRunner(FakeSuiteRunner):
        def ensure_loadgen_stopped(self):
            self.events.append("loadgen_zero")
            raise RuntimeError("token=cleanup-private")

        def verify_loadgen_zero(self):
            self.events.append("loadgen_verify")
            return self.orchestrator.CommandResult(("loadgen-verify",), 2, "")

    clock = FakeClock()
    runner = RaisingCleanupRunner(orchestrator, clock)
    monkeypatch.setattr(
        orchestrator,
        "run_monitored_loadgen",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("token=profile-private")),
    )

    result = orchestrator.run_capacity_suite(
        runner=runner,
        report_path=tmp_path / "double-failure.json",
        monitor=orchestrator.CapacityMonitor(lambda: healthy_snapshot(orchestrator)),
        monotonic_fn=clock.monotonic,
        sleep_fn=clock.sleep,
        observe_seconds=0.0,
    )

    assert result["exit_code"] == 2
    assert result["stop_reason"] == "stop_failed"
    assert result["recovery_state"] == "stop_failed"
    assert "private" not in json.dumps(result)
    assert runner.events[-3:] == ["loadgen_verify", "stop", "public_after"]


def test_capacity_suite_never_reports_recovery_when_loadgen_is_not_zero(
    orchestrator, tmp_path: Path
) -> None:
    """容器级 loadgen 归零失败时仍停止容量项目，但结论必须是 stop_failed。"""

    clock = FakeClock()
    runner = FakeSuiteRunner(
        orchestrator,
        clock,
        loadgen_cleanup_code=2,
    )

    result = orchestrator.run_capacity_suite(
        runner=runner,
        report_path=tmp_path / "loadgen-still-running.json",
        monitor=orchestrator.CapacityMonitor(lambda: healthy_snapshot(orchestrator)),
        monotonic_fn=clock.monotonic,
        sleep_fn=clock.sleep,
        observe_seconds=0.0,
    )

    assert result["exit_code"] == 2
    assert result["stop_reason"] == "stop_failed"
    assert result["recovery_state"] == "stop_failed"
    assert "stop" in runner.events
    assert runner.events[-2:] == ["stop", "public_after"]


@pytest.mark.parametrize(
    ("after", "comparison", "exit_code", "reason"),
    (
        (
            {
                name: (
                    f"id-{name}",
                    "healthy",
                    1 if name == "api" else 0,
                    False,
                )
                for name in ("api", "web", "postgres", "redis", "minio")
            },
            "changed",
            1,
            "public_stack_changed",
        ),
        (None, "not_verified", 2, "public_stack_not_verified"),
    ),
)
def test_capacity_suite_reports_public_changed_or_not_verified(
    orchestrator,
    tmp_path: Path,
    after: Mapping[str, tuple[str, str, int, bool]] | None,
    comparison: str,
    exit_code: int,
    reason: str,
) -> None:
    """公网栈变化与无法复核必须使用三态且不能通过。"""

    clock = FakeClock()
    runner = FakeSuiteRunner(orchestrator, clock, after=after)
    result = orchestrator.run_capacity_suite(
        runner=runner,
        report_path=tmp_path / f"{comparison}.json",
        monitor=orchestrator.CapacityMonitor(lambda: healthy_snapshot(orchestrator)),
        monotonic_fn=clock.monotonic,
        sleep_fn=clock.sleep,
        observe_seconds=0.0,
    )

    assert result["public_stack_comparison"] == comparison
    assert result["exit_code"] == exit_code
    assert result["stop_reason"] == reason
    assert result["passed"] is False


def test_capacity_suite_stop_failure_is_not_reported_as_recovered(
    orchestrator, tmp_path: Path
) -> None:
    """stop 非零时恢复状态必须失败，不能写 completed/emergency_stopped。"""

    clock = FakeClock()
    runner = FakeSuiteRunner(orchestrator, clock, stop_code=1)
    result = orchestrator.run_capacity_suite(
        runner=runner,
        report_path=tmp_path / "stop-failed.json",
        monitor=orchestrator.CapacityMonitor(lambda: healthy_snapshot(orchestrator)),
        monotonic_fn=clock.monotonic,
        sleep_fn=clock.sleep,
        observe_seconds=0.0,
    )

    assert result["exit_code"] == 2
    assert result["stop_reason"] == "stop_failed"
    assert result["recovery_state"] == "stop_failed"
    assert result["passed"] is False


@pytest.mark.parametrize(
    ("preflight_code", "build_code", "start_code", "expected_events"),
    (
        (1, 0, 0, ["preflight"]),
        (2, 0, 0, ["preflight"]),
        (0, 1, 0, ["preflight", "build"]),
        (
            0,
            0,
            1,
            ["preflight", "build", "up", "loadgen_verify", "stop", "public_after"],
        ),
    ),
)
def test_capacity_suite_only_stops_after_owning_lifecycle(
    orchestrator,
    tmp_path: Path,
    preflight_code: int,
    build_code: int,
    start_code: int,
    expected_events: list[str],
) -> None:
    """预检/build 失败零 stop；发起 up 后才拥有并清理固定生命周期。"""

    clock = FakeClock()
    runner = FakeSuiteRunner(
        orchestrator,
        clock,
        preflight_code=preflight_code,
        build_code=build_code,
        start_code=start_code,
    )
    orchestrator.run_capacity_suite(
        runner=runner,
        report_path=tmp_path / f"ownership-{preflight_code}-{build_code}-{start_code}.json",
        monitor=orchestrator.CapacityMonitor(lambda: healthy_snapshot(orchestrator)),
        monotonic_fn=clock.monotonic,
        sleep_fn=clock.sleep,
        observe_seconds=0.0,
    )

    assert runner.events == expected_events


@pytest.mark.parametrize("exit_code", (0, 1, 2))
def test_cli_normal_mode_executes_suite_and_returns_fixed_exit_code(
    orchestrator, tmp_path: Path, exit_code: int
) -> None:
    """普通 CLI 必须实际调用 suite，并透传唯一 0/1/2 退出码。"""

    root, env_file, docker_cli, _meminfo = capacity_repository(tmp_path)
    runner = orchestrator.DockerComposeCapacityRunner(
        env_file,
        repository_root=root,
        docker_cli=docker_cli,
        executor=fake_executor(orchestrator),
    )
    report_path = tmp_path / f"cli-{exit_code}.json"
    calls: list[tuple[object, Path]] = []

    def execute_suite(*, runner, report_path: Path):
        calls.append((runner, report_path))
        return suite_report(exit_code)

    code = orchestrator.run_cli(
        [
            "--confirm-project",
            orchestrator.CAPACITY_PROJECT,
            "--env-file",
            str(env_file),
            "--report",
            str(report_path),
        ],
        runner=runner,
        suite_executor=execute_suite,
    )

    assert code == exit_code
    assert calls == [(runner, report_path)]


def test_cli_subprocess_entry_is_silent_on_invalid_arguments(tmp_path: Path) -> None:
    """真实 __main__ 参数错误固定退出 2，stdout/stderr 不回显原始参数。"""

    marker = "token=must-not-echo"
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            "--confirm-project",
            marker,
        ],
        cwd=PROJECT_ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=10,
    )

    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr == ""
    assert marker not in result.stdout + result.stderr


@pytest.mark.parametrize(
    "unsafe_kind",
    ("inside_repo", "existing", "missing_parent", "directory", "symlink"),
)
def test_report_rejects_every_unsafe_target_before_writing(
    orchestrator, tmp_path: Path, unsafe_kind: str
) -> None:
    """报告必须位于仓库外、父目录既有、目标全新且不是目录/symlink。"""

    if unsafe_kind == "inside_repo":
        target = PROJECT_ROOT / "unsafe-capacity-report.json"
    elif unsafe_kind == "existing":
        target = tmp_path / "existing.json"
        target.write_text("existing", encoding="utf-8")
    elif unsafe_kind == "missing_parent":
        target = tmp_path / "missing" / "report.json"
    elif unsafe_kind == "directory":
        target = tmp_path / "directory"
        target.mkdir()
    else:
        original = tmp_path / "original.json"
        original.write_text("existing", encoding="utf-8")
        target = tmp_path / "report-link.json"
        try:
            target.symlink_to(original)
        except OSError:
            pytest.skip("当前平台不允许创建测试符号链接")

    with pytest.raises(ValueError, match="unsafe report"):
        orchestrator.write_report(
            target,
            suite_report(1),
            repository_root=PROJECT_ROOT,
        )
    if unsafe_kind == "inside_repo":
        assert not target.exists()


@pytest.mark.parametrize(
    "payload_mutator",
    (
        lambda value: value.update(extra="access_token=unsafe"),
        lambda value: value["resource_peaks"].update(access_token="unsafe"),
        lambda value: value.update(stop_reason="arbitrary_exception_text"),
        lambda value: value.update(passed=True),
    ),
)
def test_report_rejects_sensitive_or_inconsistent_schema(
    orchestrator,
    tmp_path: Path,
    payload_mutator,
) -> None:
    """额外敏感键/值、任意字符串或通过状态不一致都不得落盘。"""

    payload = suite_report(1)
    payload_mutator(payload)
    target = tmp_path / f"unsafe-{id(payload_mutator)}.json"
    with pytest.raises(ValueError, match="unsafe report"):
        orchestrator.write_report(target, payload, repository_root=PROJECT_ROOT)
    assert not target.exists()


def test_report_rejects_top_level_pass_when_any_profile_failed(
    orchestrator, tmp_path: Path
) -> None:
    """顶层 passed=true 不得掩盖任一失败的 profile 聚合。"""

    payload = suite_report(0)
    profiles = payload["profiles"]
    assert isinstance(profiles, list)
    profile = profiles[4]
    assert isinstance(profile, dict)
    profile["passed"] = False
    target = tmp_path / "contradictory-profile.json"

    with pytest.raises(ValueError, match="unsafe report"):
        orchestrator.write_report(target, payload, repository_root=PROJECT_ROOT)
    assert not target.exists()


def test_all_recorded_lifecycle_argv_exclude_public_project(orchestrator, tmp_path: Path) -> None:
    """全部生命周期 argv 必须只含容量项目，公网项目只允许 Docker ps/inspect。"""

    root, env_file, docker_cli, _meminfo = capacity_repository(tmp_path)
    clock = FakeClock()
    processes = [
        FakeLoadgenProcess(
            clock,
            complete_at=0,
            stdout=json.dumps(task2_report(profile)),
        )
        for profile in orchestrator.PROFILE_ORDER
    ]
    runner = orchestrator.DockerComposeCapacityRunner(
        env_file,
        repository_root=root,
        docker_cli=docker_cli,
        executor=fake_executor(orchestrator),
        process_factory=lambda _argv, _env: processes.pop(0),
    )
    runner.build_capacity()
    runner.start_capacity()
    for profile in orchestrator.PROFILE_ORDER:
        runner.start_loadgen(profile)
    runner.stop_capacity_project()

    serialized = [" ".join(argv) for argv in runner.lifecycle_calls]
    assert all(orchestrator.CAPACITY_PROJECT in command for command in serialized)
    assert all(orchestrator.PUBLIC_PROJECT not in command for command in serialized)
    assert all(" down " not in f" {command} " for command in serialized)


def test_monitor_failure_still_terminates_loadgen_before_return(orchestrator) -> None:
    """采样解析异常不能越过 loadgen 终止顺序直接交给外层 stop。"""

    clock = FakeClock()
    process = FakeLoadgenProcess(
        clock,
        complete_at=100.0,
        stdout="",
    )
    monitor = orchestrator.CapacityMonitor(
        lambda: (_ for _ in ()).throw(ValueError("unsafe sample"))
    )

    outcome = orchestrator.run_monitored_loadgen(
        process=process,
        profile="rate-warmup-10",
        monitor=monitor,
        policy=orchestrator.StopPolicy(),
        monotonic_fn=clock.monotonic,
        sleep_fn=clock.sleep,
    )

    assert outcome.stop_reason == "runner_failed"
    assert process.events[-2:] == [
        "terminate",
        f"wait:{orchestrator.LOADGEN_TERMINATE_WAIT_SECONDS}",
    ]


def test_public_after_missing_service_is_changed_not_not_verified(
    orchestrator, tmp_path: Path
) -> None:
    """Docker ps 成功但公网服务缺失是已确认变化，不是采集失败。"""

    root, env_file, docker_cli, _meminfo = capacity_repository(tmp_path)
    baseline = {
        name: (f"id-{name}", "healthy", 0, False)
        for name in orchestrator.PUBLIC_LONG_LIVED_SERVICES
    }
    missing_ids = "\n".join(f"id-{name}" for name in orchestrator.PUBLIC_LONG_LIVED_SERVICES[:-1])
    runner = orchestrator.DockerComposeCapacityRunner(
        env_file,
        repository_root=root,
        docker_cli=docker_cli,
        executor=RecordingExecutor(orchestrator, ((0, missing_ids + "\n"),)),
    )

    after = runner.read_public_stack_after()
    assert after == {}
    assert orchestrator.compare_public_stack(baseline, after) == "changed"
