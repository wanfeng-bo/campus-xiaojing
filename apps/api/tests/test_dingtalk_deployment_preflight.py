from __future__ import annotations

# ruff: noqa: E501
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path, PurePath, PureWindowsPath

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[3]
GIT_BASH = Path("C:/Program Files/Git/bin/bash.exe")


def read_project_script(relative_path: str) -> str:
    """读取仓库脚本，并将缺失脚本显示为可观察的测试失败。"""

    script_path = PROJECT_ROOT / relative_path
    assert script_path.is_file(), f"部署脚本不存在：{relative_path}"
    return script_path.read_text(encoding="utf-8")


def write_fake_command(directory: Path, name: str, content: str) -> None:
    """写入仅供 Git Bash 执行的命令替身，隔离真实系统与 Docker。"""

    command_path = directory / name
    command_path.write_text(content, encoding="utf-8", newline="\n")
    command_path.chmod(command_path.stat().st_mode | stat.S_IXUSR)


def resolve_bash() -> str:
    """优先解析 PATH 中的 Bash，Windows 才回退 Git Bash；缺失即失败。"""

    if os.name == "nt" and GIT_BASH.is_file():
        return str(GIT_BASH)
    bash_from_path = shutil.which("bash")
    if bash_from_path is not None:
        return bash_from_path
    pytest.fail("未找到 Bash，Task 5 部署脚本验证未满足")


def to_bash_path(path: PurePath, *, windows: bool | None = None) -> str:
    """将路径转换为 Bash 参数；仅 Windows 使用盘符到 /c 的转换。"""

    use_windows = os.name == "nt" if windows is None else windows
    if not use_windows:
        return path.as_posix()
    assert path.drive, f"Windows 路径缺少盘符：{path}"
    return f"/{path.drive[0].lower()}{path.as_posix()[2:]}"


def write_fake_resource_commands(directory: Path, stop_log: Path) -> None:
    """提供完整资源快照和 Compose JSON 的假命令，不连接真实 Docker daemon。"""

    write_fake_command(
        directory,
        "free",
        "#!/usr/bin/env bash\nset -eu\n"
        '[ "$#" -eq 1 ] && [ "$1" = -m ] || exit 97\n'
        '[ "${CAMPUS_AI_FAKE_FREE_FAIL:-}" != 1 ] || exit 42\n'
        "printf 'Mem: 1000 100 100 0 0 %s\\n' \"${CAMPUS_AI_FAKE_MEM_AVAILABLE_MB:-900}\"\n",
    )
    write_fake_command(
        directory,
        "df",
        "#!/usr/bin/env bash\nset -eu\n"
        '[ "$#" -eq 2 ] && [ "$1" = -Pm ] || exit 97\n'
        'case "$2" in /) value="${CAMPUS_AI_FAKE_SYSTEM_DISK_MB:-900}" ;; /fake/docker-root) value="${CAMPUS_AI_FAKE_DOCKER_DISK_MB:-900}" ;; *) exit 97 ;; esac\n'
        '[ "${CAMPUS_AI_FAKE_DF_FAIL:-}" != 1 ] || exit 42\n'
        '[ "$2" != /fake/docker-root ] || [ "${CAMPUS_AI_FAKE_DOCKER_DF_FAIL:-}" != 1 ] || exit 42\n'
        "printf '%s\\n' 'Filesystem 1048576-blocks Used Available Capacity Mounted on'\n"
        "printf '/dev/fake 1000 100 %s 10%% /\\n' \"$value\"\n",
    )
    write_fake_command(
        directory,
        "python3",
        '#!/usr/bin/env bash\nprintf x >> "${CAMPUS_AI_FAKE_PYTHON_LOG:?}"\n[ "${CAMPUS_AI_FAKE_PYTHON3_FAIL:-}" != 1 ] || exit 42\n[ "${CAMPUS_AI_FAKE_PYTHON3_FAIL_SECOND:-}" != 1 ] || [ "$(wc -c < "${CAMPUS_AI_FAKE_PYTHON_LOG:?}")" -ne 2 ] || exit 42\nexec "${CAMPUS_AI_REAL_PYTHON_BASH:?}" "$@"\n',
    )
    write_fake_command(
        directory,
        "docker",
        "#!/usr/bin/env bash\n"
        "set -eu\n"
        'if [ "${1:-}" = info ]; then\n'
        '[ "$#" -eq 3 ] && [ "$2" = --format ] && [ "$3" = "{{.DockerRootDir}}" ] || exit 97\n'
        '  if [ "${CAMPUS_AI_FAKE_DOCKER_INFO_FAIL:-}" = 1 ]; then\n'
        "    printf '%s\\n' 'fake docker info failure' >&2\n"
        "    exit 42\n"
        "  fi\n"
        '  if [ "${CAMPUS_AI_FAKE_DOCKER_ROOT_EMPTY:-}" = 1 ]; then printf "\\n"; else printf "%s\\n" "${CAMPUS_AI_FAKE_DOCKER_ROOT:-/fake/docker-root}"; fi\n'
        'elif [ "${1:-}" = stats ]; then\n'
        '[ "$#" -eq 4 ] && [ "$2" = --no-stream ] && [ "$3" = --format ] && [ "$4" = "{{.Name}}\\t{{.MemUsage}}\\t{{.CPUPerc}}" ] || exit 97\n'
        '[ "${CAMPUS_AI_FAKE_STATS_FAIL:-}" != 1 ] || exit 42\n'
        "  printf '%s\\n' 'fake-api\\t20MiB / 900MiB\\t1.00%%'\n"
        'elif [ "${1:-}" = system ] && [ "${2:-}" = df ]; then\n'
        '[ "$#" -eq 4 ] && [ "$3" = --format ] && [ "$4" = "{{.Type}}\\t{{.TotalCount}}\\t{{.Size}}\\t{{.Reclaimable}}" ] || exit 97\n'
        '[ "${CAMPUS_AI_FAKE_SYSTEM_DF_FAIL:-}" != 1 ] || exit 42\n'
        'if [ "${CAMPUS_AI_FAKE_SYSTEM_DF_MALFORMED:-}" = 1 ]; then printf "%s\\n" malformed; exit 0; fi\n'
        "  printf 'Images\\t1\\t100MB\\t50MB\\n'\n"
        'elif [ "${1:-}" = compose ]; then\n'
        '  [ "$2" = --project-name ] && [ "$3" = campus-ai-dingtalk-test ] && [ "$4" = --env-file ] && [ "$5" = .env.dingtalk-test ] && [ "$6" = -f ] && [ "$7" = infra/docker/compose.dingtalk-test.yml ] || exit 97\n'
        '  if [ "$#" -eq 10 ] && [ "$8" = config ] && [ "$9" = --format ] && [ "${10}" = json ]; then\n'
        '    printf x >> "${CAMPUS_AI_FAKE_CONFIG_LOG:?}"\n'
        '    [ "${CAMPUS_AI_FAKE_CONFIG_FAIL_FIRST:-}" != 1 ] || [ "$(wc -c < "${CAMPUS_AI_FAKE_CONFIG_LOG:?}")" -ne 1 ] || exit 42\n'
        '    [ "${CAMPUS_AI_FAKE_CONFIG_FAIL_SECOND:-}" != 1 ] || [ "$(wc -c < "${CAMPUS_AI_FAKE_CONFIG_LOG:?}")" -ne 2 ] || exit 42\n'
        "    "
        'if [ "${CAMPUS_AI_FAKE_COMPOSE_NESTED_NAME:-}" = 1 ]; then '
        'printf \'%s\\n\' \'{"name":"wrong-project","services":{"web":{"name":"campus-ai-dingtalk-test"}}}\'; '
        "else printf '%s\\n' '{\"name\":\"campus-ai-dingtalk-test\"}'; fi\n"
        '  elif [ "$#" -eq 8 ] && [ "$8" = stop ]; then\n'
        '    printf \'%s\\n\' "$*" >> "${CAMPUS_AI_FAKE_STOP_LOG:?}"\n'
        '    [ "${CAMPUS_AI_FAKE_STOP_FAIL:-}" != 1 ] || exit 42\n'
        "  else exit 97; fi\n"
        "else\n"
        "  printf '%s\\n' 'unexpected fake docker command' >&2\n"
        "  exit 98\n"
        "fi\n",
    )
    stop_log.touch()


def run_bash_script(
    relative_path: str,
    fake_bin: Path,
    stop_log: Path,
    *arguments: str,
    environment: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    """在临时 PATH 下运行目标脚本，确保测试不调用真实系统命令。"""

    bash = resolve_bash()
    script_path = PROJECT_ROOT / relative_path
    command_environment = os.environ.copy()
    command_environment.update(environment)
    command_environment["CAMPUS_AI_FAKE_STOP_LOG"] = str(stop_log)
    command_environment["CAMPUS_AI_FAKE_CONFIG_LOG"] = str(stop_log.with_name("config.log"))
    command_environment["CAMPUS_AI_FAKE_PYTHON_LOG"] = str(stop_log.with_name("python.log"))
    fake_bin_bash_path = to_bash_path(fake_bin)
    script_bash_path = to_bash_path(script_path)
    python_path = Path(sys.executable)
    command_environment["CAMPUS_AI_REAL_PYTHON_BASH"] = to_bash_path(python_path)
    return subprocess.run(
        (
            bash,
            "-c",
            'PATH="$1:$PATH"; export PATH; exec "$2" "${@:3}"',
            "bash",
            fake_bin_bash_path,
            script_bash_path,
            *arguments,
        ),
        cwd=PROJECT_ROOT,
        env=command_environment,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=10,
    )


def resource_thresholds(memory_mb: str = "800") -> dict[str, str]:
    """构造只用于测试的资源门槛，避免从环境文件读取任何值。"""

    return {
        "CAMPUS_AI_REQUIRED_MEM_AVAILABLE_MB": memory_mb,
        "CAMPUS_AI_REQUIRED_SYSTEM_DISK_MB": "800",
        "CAMPUS_AI_REQUIRED_DOCKER_DISK_MB": "800",
    }


def test_resource_check_is_read_only_and_checks_required_sources() -> None:
    script = read_project_script("scripts/check_dingtalk_deployment_resources.sh")

    assert "MemAvailable" in script
    assert "docker info --format" in script
    assert "docker stats --no-stream" in script
    assert "docker system df --format" in script
    assert "df -Pm" in script
    assert "eval" not in script
    assert "MAX_RESOURCE_MB" in script
    assert "prune" not in script
    assert "docker compose" not in script
    assert " down" not in script
    assert " stop" not in script


def test_to_bash_path_keeps_posix_paths_without_windows_drive_access() -> None:
    assert to_bash_path(Path("/tmp/campus-ai/fake-bin"), windows=False) == "/tmp/campus-ai/fake-bin"
    assert (
        to_bash_path(PureWindowsPath("C:/workspace/fake-bin"), windows=True)
        == "/c/workspace/fake-bin"
    )


def test_cutover_guard_can_only_stop_the_fixed_project() -> None:
    script = read_project_script("scripts/guard_dingtalk_test_cutover.sh")

    assert "campus-ai-dingtalk-test" in script
    assert "--confirm-started-project" in script
    assert '"${compose_command[@]}" stop' in script
    assert " down" not in script
    assert "down -v" not in script
    assert "volume rm" not in script


def test_resource_check_returns_zero_for_sufficient_fake_resources(tmp_path: Path) -> None:
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    stop_log = tmp_path / "compose-stop.log"
    write_fake_resource_commands(fake_bin, stop_log)

    result = run_bash_script(
        "scripts/check_dingtalk_deployment_resources.sh",
        fake_bin,
        stop_log,
        environment=resource_thresholds(),
    )

    assert result.returncode == 0, result.stderr
    assert "内存可用" in result.stdout
    assert "Docker 根目录" in result.stdout
    assert stop_log.read_text(encoding="utf-8") == ""


def test_resource_check_rejects_missing_or_non_positive_thresholds(tmp_path: Path) -> None:
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    stop_log = tmp_path / "compose-stop.log"
    write_fake_resource_commands(fake_bin, stop_log)

    result = run_bash_script(
        "scripts/check_dingtalk_deployment_resources.sh",
        fake_bin,
        stop_log,
        environment={
            "CAMPUS_AI_REQUIRED_MEM_AVAILABLE_MB": "0",
            "CAMPUS_AI_REQUIRED_SYSTEM_DISK_MB": "800",
        },
    )

    assert result.returncode == 2
    assert "资源预检配置错误" in result.stderr
    assert stop_log.read_text(encoding="utf-8") == ""


def test_resource_check_reports_docker_info_command_failure_in_chinese(tmp_path: Path) -> None:
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    stop_log = tmp_path / "compose-stop.log"
    write_fake_resource_commands(fake_bin, stop_log)

    result = run_bash_script(
        "scripts/check_dingtalk_deployment_resources.sh",
        fake_bin,
        stop_log,
        environment={
            **resource_thresholds(),
            "CAMPUS_AI_FAKE_DOCKER_INFO_FAIL": "1",
        },
    )

    assert result.returncode == 1
    assert "资源预检失败：无法读取 Docker 根目录。" in result.stderr
    assert stop_log.read_text(encoding="utf-8") == ""


@pytest.mark.parametrize(
    ("environment_key", "threshold_key"),
    (
        ("CAMPUS_AI_FAKE_MEM_AVAILABLE_MB", "CAMPUS_AI_REQUIRED_MEM_AVAILABLE_MB"),
        ("CAMPUS_AI_FAKE_SYSTEM_DISK_MB", "CAMPUS_AI_REQUIRED_SYSTEM_DISK_MB"),
        ("CAMPUS_AI_FAKE_DOCKER_DISK_MB", "CAMPUS_AI_REQUIRED_DOCKER_DISK_MB"),
    ),
)
def test_resource_check_rejects_each_insufficient_resource(
    tmp_path: Path, environment_key: str, threshold_key: str
) -> None:
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    stop_log = tmp_path / "compose-stop.log"
    write_fake_resource_commands(fake_bin, stop_log)
    environment = resource_thresholds()
    environment[environment_key] = "799"
    environment[threshold_key] = "800"

    result = run_bash_script(
        "scripts/check_dingtalk_deployment_resources.sh",
        fake_bin,
        stop_log,
        environment=environment,
    )

    assert result.returncode == 1
    assert "低于已确认门槛" in result.stderr


@pytest.mark.parametrize(
    "environment_key",
    (
        "CAMPUS_AI_FAKE_MEM_AVAILABLE_MB",
        "CAMPUS_AI_FAKE_SYSTEM_DISK_MB",
        "CAMPUS_AI_FAKE_DOCKER_DISK_MB",
    ),
)
def test_resource_check_treats_zero_measurement_as_insufficient(
    tmp_path: Path, environment_key: str
) -> None:
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    stop_log = tmp_path / "compose-stop.log"
    write_fake_resource_commands(fake_bin, stop_log)
    result = run_bash_script(
        "scripts/check_dingtalk_deployment_resources.sh",
        fake_bin,
        stop_log,
        environment={**resource_thresholds(), environment_key: "0"},
    )
    assert result.returncode == 1
    assert "0 MB" in result.stdout
    assert "低于已确认门槛" in result.stderr


@pytest.mark.parametrize(
    ("failure_key", "expected_message"),
    (
        ("CAMPUS_AI_FAKE_FREE_FAIL", "无法读取可用内存"),
        ("CAMPUS_AI_FAKE_DF_FAIL", "无法读取系统盘可用空间"),
        ("CAMPUS_AI_FAKE_STATS_FAIL", "无法读取 Docker 当前容器资源"),
        ("CAMPUS_AI_FAKE_SYSTEM_DF_FAIL", "无法读取 Docker 存储快照"),
    ),
)
def test_resource_check_reports_each_source_failure(
    tmp_path: Path, failure_key: str, expected_message: str
) -> None:
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    stop_log = tmp_path / "compose-stop.log"
    write_fake_resource_commands(fake_bin, stop_log)

    result = run_bash_script(
        "scripts/check_dingtalk_deployment_resources.sh",
        fake_bin,
        stop_log,
        environment={**resource_thresholds(), failure_key: "1"},
    )

    assert result.returncode == 1
    assert expected_message in result.stderr


def test_cutover_guard_stops_only_once_when_second_gate_fails(tmp_path: Path) -> None:
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    stop_log = tmp_path / "compose-stop.log"
    write_fake_resource_commands(fake_bin, stop_log)

    result = run_bash_script(
        "scripts/guard_dingtalk_test_cutover.sh",
        fake_bin,
        stop_log,
        "--confirm-started-project",
        "campus-ai-dingtalk-test",
        environment=resource_thresholds(memory_mb="901"),
    )

    assert result.returncode == 1
    recorded_stops = stop_log.read_text(encoding="utf-8").splitlines()
    assert recorded_stops == [
        "compose --project-name campus-ai-dingtalk-test "
        "--env-file .env.dingtalk-test "
        "-f infra/docker/compose.dingtalk-test.yml stop"
    ]


def test_cutover_guard_refuses_a_different_project_without_stopping(tmp_path: Path) -> None:
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    stop_log = tmp_path / "compose-stop.log"
    write_fake_resource_commands(fake_bin, stop_log)

    result = run_bash_script(
        "scripts/guard_dingtalk_test_cutover.sh",
        fake_bin,
        stop_log,
        "--confirm-started-project",
        "different-project",
        environment=resource_thresholds(memory_mb="901"),
    )

    assert result.returncode == 2
    assert "campus-ai-dingtalk-test" in result.stderr
    assert stop_log.read_text(encoding="utf-8") == ""


@pytest.mark.parametrize("invalid_threshold", ("00", "001", "99999999999999999999"))
def test_resource_check_rejects_noncanonical_or_unbounded_thresholds(
    tmp_path: Path, invalid_threshold: str
) -> None:
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    stop_log = tmp_path / "compose-stop.log"
    write_fake_resource_commands(fake_bin, stop_log)

    result = run_bash_script(
        "scripts/check_dingtalk_deployment_resources.sh",
        fake_bin,
        stop_log,
        environment=resource_thresholds(memory_mb=invalid_threshold),
    )

    assert result.returncode == 2
    assert "资源预检配置错误" in result.stderr


def test_resource_check_reads_only_docker_storage_snapshot_without_pruning() -> None:
    script = read_project_script("scripts/check_dingtalk_deployment_resources.sh")

    assert "docker system df --format" in script
    assert "prune" not in script


def test_cutover_guard_uses_bash_and_semantic_top_level_project_validation() -> None:
    script = read_project_script("scripts/guard_dingtalk_test_cutover.sh")

    assert 'bash "$script_directory/check_dingtalk_deployment_resources.sh"' in script
    assert "--project-name campus-ai-dingtalk-test" in script
    assert "json.load" in script


def test_cutover_guard_succeeds_without_stop_for_sufficient_resources(tmp_path: Path) -> None:
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    stop_log = tmp_path / "compose-stop.log"
    write_fake_resource_commands(fake_bin, stop_log)

    result = run_bash_script(
        "scripts/guard_dingtalk_test_cutover.sh",
        fake_bin,
        stop_log,
        "--confirm-started-project",
        "campus-ai-dingtalk-test",
        environment=resource_thresholds(),
    )

    assert result.returncode == 0, result.stderr
    assert stop_log.read_text(encoding="utf-8") == ""


def test_cutover_guard_rejects_nested_project_name_without_stopping(tmp_path: Path) -> None:
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    stop_log = tmp_path / "compose-stop.log"
    write_fake_resource_commands(fake_bin, stop_log)

    result = run_bash_script(
        "scripts/guard_dingtalk_test_cutover.sh",
        fake_bin,
        stop_log,
        "--confirm-started-project",
        "campus-ai-dingtalk-test",
        environment={
            **resource_thresholds(),
            "CAMPUS_AI_FAKE_COMPOSE_NESTED_NAME": "1",
        },
    )

    assert result.returncode == 2
    assert "项目名不是" in result.stderr
    assert stop_log.read_text(encoding="utf-8") == ""


def test_cutover_guard_never_stops_when_json_parser_fails(tmp_path: Path) -> None:
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    stop_log = tmp_path / "compose-stop.log"
    write_fake_resource_commands(fake_bin, stop_log)
    result = run_bash_script(
        "scripts/guard_dingtalk_test_cutover.sh",
        fake_bin,
        stop_log,
        "--confirm-started-project",
        "campus-ai-dingtalk-test",
        environment={**resource_thresholds(), "CAMPUS_AI_FAKE_PYTHON3_FAIL": "1"},
    )
    assert result.returncode == 2
    assert "项目名不是" in result.stderr
    assert stop_log.read_text(encoding="utf-8") == ""


@pytest.mark.parametrize(
    "failure_key",
    (
        "CAMPUS_AI_FAKE_CONFIG_FAIL_FIRST",
        "CAMPUS_AI_FAKE_CONFIG_FAIL_SECOND",
        "CAMPUS_AI_FAKE_PYTHON3_FAIL_SECOND",
    ),
)
def test_cutover_guard_never_stops_when_a_confirmation_fails(
    tmp_path: Path, failure_key: str
) -> None:
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    stop_log = tmp_path / "compose-stop.log"
    write_fake_resource_commands(fake_bin, stop_log)
    environment = {**resource_thresholds(), failure_key: "1"}
    if failure_key.endswith("SECOND"):
        environment["CAMPUS_AI_REQUIRED_MEM_AVAILABLE_MB"] = "901"
    result = run_bash_script(
        "scripts/guard_dingtalk_test_cutover.sh",
        fake_bin,
        stop_log,
        "--confirm-started-project",
        "campus-ai-dingtalk-test",
        environment=environment,
    )
    assert result.returncode == 2
    assert "切换守卫失败" in result.stderr
    assert stop_log.read_text(encoding="utf-8") == ""


@pytest.mark.parametrize(
    ("environment_key", "message"),
    (
        ("CAMPUS_AI_FAKE_DOCKER_ROOT_EMPTY", "Docker 根目录"),
        ("CAMPUS_AI_FAKE_DOCKER_DF_FAIL", "Docker 盘可用空间"),
        ("CAMPUS_AI_FAKE_SYSTEM_DF_MALFORMED", "存储快照为空或格式异常"),
    ),
)
def test_resource_check_fails_closed_for_remaining_docker_boundaries(
    tmp_path: Path, environment_key: str, message: str
) -> None:
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    stop_log = tmp_path / "compose-stop.log"
    write_fake_resource_commands(fake_bin, stop_log)
    result = run_bash_script(
        "scripts/check_dingtalk_deployment_resources.sh",
        fake_bin,
        stop_log,
        environment={**resource_thresholds(), environment_key: "1"},
    )
    assert result.returncode == 1
    assert message in result.stderr


def test_cutover_guard_attempts_one_fixed_stop_when_stop_fails(tmp_path: Path) -> None:
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    stop_log = tmp_path / "compose-stop.log"
    write_fake_resource_commands(fake_bin, stop_log)
    result = run_bash_script(
        "scripts/guard_dingtalk_test_cutover.sh",
        fake_bin,
        stop_log,
        "--confirm-started-project",
        "campus-ai-dingtalk-test",
        environment={**resource_thresholds(memory_mb="901"), "CAMPUS_AI_FAKE_STOP_FAIL": "1"},
    )
    assert result.returncode == 1
    assert "无法停止" in result.stderr
    assert stop_log.read_text(encoding="utf-8").splitlines() == [
        "compose --project-name campus-ai-dingtalk-test --env-file .env.dingtalk-test -f infra/docker/compose.dingtalk-test.yml stop"
    ]
