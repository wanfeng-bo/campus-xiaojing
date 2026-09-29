from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[3]
CAPACITY_PROJECT = "campus-ai-dingtalk-capacity-synthetic"
CAPACITY_COMPOSE = PROJECT_ROOT / "infra/docker/compose.capacity-synthetic.yml"
CAPACITY_DOCKERFILE = PROJECT_ROOT / "infra/docker/api.capacity-synthetic.Dockerfile"
CAPACITY_NGINX = PROJECT_ROOT / "infra/nginx/dingtalk-capacity-synthetic.conf.template"
CAPACITY_ENV_EXAMPLE = PROJECT_ROOT / ".env.capacity-synthetic.example"
PRODUCTION_API_DOCKERFILE = PROJECT_ROOT / "infra/docker/api.Dockerfile"
README = PROJECT_ROOT / "README.md"
RUNBOOK = PROJECT_ROOT / "docs/runbooks/钉钉测试企业HTTPS部署.md"
CAPACITY_SERVICES = {
    "postgres",
    "redis",
    "migrate",
    "api-synthetic",
    "gateway",
    "loadgen",
}
CAPACITY_PROFILE_ORDER = (
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
FAKE_CAPACITY_ENVIRONMENT = {
    "APP_ENV": "test",
    "IDENTITY_PROVIDER": "dingtalk",
    "CAMPUS_AI_SYNTHETIC_CAPACITY": "true",
    "POSTGRES_HOST": "postgres",
    "POSTGRES_PORT": "5432",
    "POSTGRES_DB": "campus_ai_capacity_test",
    "POSTGRES_USER": "campus_ai_capacity_test",
    "POSTGRES_PASSWORD": "capacity-postgres-invalid-secret",
    "REDIS_HOST": "redis",
    "REDIS_PORT": "6379",
    "REDIS_PASSWORD": "capacity-redis-invalid-secret",
    "REDIS_CONNECT_TIMEOUT_SECONDS": "0.5",
    "REDIS_READ_TIMEOUT_SECONDS": "0.5",
    "DINGTALK_CORP_ID": "capacity-corp.invalid",
    "DINGTALK_APP_KEY": "capacity-app.invalid",
    "DINGTALK_APP_SECRET": "capacity-secret-not-real",
    "DINGTALK_IDENTITY_TIMEOUT_SECONDS": "5",
    "DINGTALK_LOGIN_GLOBAL_RATE_PER_MINUTE": "120",
    "DINGTALK_LOGIN_CONCURRENCY_LIMIT": "30",
    "IDENTITY_SESSION_COOKIE_NAME": "campus_ai_capacity_session",
    "CAMPUS_AI_DINGTALK_LOGIN_IP_RATE": "5r/s",
    "CAMPUS_AI_DINGTALK_LOGIN_IP_BURST": "30",
}
COMPOSE_ENVIRONMENT_KEYS_TO_CLEAR = frozenset(
    {
        *FAKE_CAPACITY_ENVIRONMENT,
        "COMPOSE_FILE",
        "COMPOSE_PROJECT_NAME",
        "COMPOSE_PROFILES",
        "COMPOSE_ENV_FILES",
        "COMPOSE_DISABLE_ENV_FILE",
        "COMPOSE_PATH_SEPARATOR",
    }
)


def read_project_file(relative_path: str) -> str:
    """读取仓库部署资产，保持测试不依赖额外 YAML/Nginx 解析库。"""

    return (PROJECT_ROOT / relative_path).read_text(encoding="utf-8")


def parse_env_example(content: str) -> dict[str, str]:
    """解析不含引号和插值的公开容量样例。"""

    parsed: dict[str, str] = {}
    for line in content.splitlines():
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator or not key or key in parsed:
            pytest.fail("容量环境样例必须由唯一 KEY=VALUE 行组成")
        parsed[key] = value
    return parsed


def extract_nginx_block(nginx: str, opening_line: str) -> str:
    """按花括号深度提取精确 Nginx 块，避免相邻 location 污染断言。"""

    opening_offset = nginx.find(opening_line)
    if opening_offset < 0:
        pytest.fail(f"缺少 Nginx 块：{opening_line}")
    opening_brace_offset = nginx.find("{", opening_offset + len(opening_line))
    if opening_brace_offset < 0:
        pytest.fail(f"Nginx 块缺少起始花括号：{opening_line}")

    depth = 0
    quote: str | None = None
    escaped = False
    for offset in range(opening_brace_offset, len(nginx)):
        character = nginx[offset]
        if quote is not None:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == quote:
                quote = None
            continue
        if character in {"'", '"'}:
            quote = character
        elif character == "{":
            depth += 1
        elif character == "}":
            depth -= 1
            if depth == 0:
                return nginx[opening_offset : offset + 1]
    pytest.fail(f"Nginx 块缺少结束花括号：{opening_line}")


def resolve_docker_cli(*, require_integration: bool = False) -> str:
    """发现仅用于受控配置展开或显式集成门的 Docker CLI。"""

    configured_cli = os.environ.get("CAMPUS_AI_DOCKER_CLI")
    if configured_cli:
        if not Path(configured_cli).is_file():
            if require_integration:
                pytest.fail("显式容量集成要求可用的 Docker CLI")
            pytest.skip("CAMPUS_AI_DOCKER_CLI 指向的 Docker CLI 不可用")
        return configured_cli
    discovered_cli = shutil.which("docker")
    if discovered_cli is None:
        if require_integration:
            pytest.fail("显式容量集成要求可用的 Docker CLI")
        pytest.skip("当前环境没有 Docker CLI，Compose JSON 校验未执行")
    return discovered_cli


def assert_linux_docker_daemon(docker_cli: str) -> None:
    """显式集成门必须连接可用的 Linux daemon，不能退化为 skip。"""

    try:
        daemon_probe = subprocess.run(
            [docker_cli, "info", "--format", "{{.OSType}}"],
            cwd=PROJECT_ROOT,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        pytest.fail("显式容量集成要求可用的 Docker daemon")
    if daemon_probe.returncode != 0:
        pytest.fail("显式容量集成要求可用的 Docker daemon")
    if daemon_probe.stdout.strip() != "linux":
        pytest.fail("显式容量集成要求 Linux Docker daemon")


def test_explicit_capacity_integration_requires_docker_cli(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """显式 Linux 集成门开启后，缺少 Docker 不能再被记为 skip。"""

    monkeypatch.setenv("CAMPUS_AI_DOCKER_CLI", str(tmp_path / "missing-docker"))
    with pytest.raises(pytest.fail.Exception, match="Docker CLI"):
        resolve_docker_cli(require_integration=True)


def test_explicit_capacity_integration_requires_linux_daemon(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """显式 Linux 集成门开启后，不可用 daemon 必须失败关闭。"""

    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            args=("docker", "info"),
            returncode=1,
            stdout="",
            stderr="daemon unavailable",
        ),
    )
    with pytest.raises(pytest.fail.Exception, match="daemon"):
        assert_linux_docker_daemon("docker")


def write_fake_capacity_environment(target: Path) -> None:
    """写入仅供配置展开的固定 `.invalid` 假值。"""

    target.write_text(
        "".join(f"{key}={value}\n" for key, value in FAKE_CAPACITY_ENVIRONMENT.items()),
        encoding="utf-8",
    )


def write_fake_compose_override(target: Path, env_file: Path) -> None:
    """替换全部服务级 env_file，测试不读取仓库私有容量文件。"""

    quoted_path = json.dumps(str(env_file))
    target.write_text(
        "\n".join(
            (
                "services:",
                "  migrate:",
                f"    env_file: !override [{quoted_path}]",
                "  api-synthetic:",
                f"    env_file: !override [{quoted_path}]",
                "",
            )
        ),
        encoding="utf-8",
    )


def load_compose_config(
    docker_cli: str,
    env_file: Path,
    override_file: Path | None,
    *,
    include_loadgen: bool = True,
    resolve_env_files: bool,
) -> dict[str, object]:
    """以净化父环境展开固定 Compose；失败时不回显可能含配置值的输出。"""

    subprocess_environment = os.environ.copy()
    for key in COMPOSE_ENVIRONMENT_KEYS_TO_CLEAR:
        subprocess_environment.pop(key, None)

    arguments = [
        docker_cli,
        "compose",
        "--project-name",
        CAPACITY_PROJECT,
        "--env-file",
        str(env_file),
        "-f",
        str(CAPACITY_COMPOSE),
    ]
    if include_loadgen:
        arguments.extend(("--profile", "capacity-loadgen"))
    if override_file is not None:
        arguments.extend(("-f", str(override_file)))
    arguments.append("config")
    if not resolve_env_files:
        arguments.append("--no-env-resolution")
    arguments.extend(("--format", "json"))

    try:
        result = subprocess.run(
            arguments,
            cwd=PROJECT_ROOT,
            env=subprocess_environment,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        pytest.fail("容量 Compose 配置展开超过 30 秒")
    if result.returncode != 0:
        pytest.fail(f"容量 Compose 配置展开失败，退出码 {result.returncode}")
    try:
        config = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        pytest.fail(f"容量 Compose 未返回有效 JSON：位置 {error.pos}")
    if not isinstance(config, dict):
        pytest.fail("容量 Compose JSON 顶层必须是对象")
    return config


def service_env_file_paths(service: object) -> list[Path]:
    """把 Compose JSON 的 env_file 两种形态规范为绝对路径。"""

    if not isinstance(service, dict):
        pytest.fail("Compose 服务必须是对象")
    env_files = service.get("env_file", [])
    if not isinstance(env_files, list):
        pytest.fail("服务 env_file 必须是列表")
    paths: list[Path] = []
    for env_file in env_files:
        path = env_file.get("path") if isinstance(env_file, dict) else env_file
        if not isinstance(path, str):
            pytest.fail("服务 env_file 路径必须是字符串")
        paths.append(Path(path).resolve())
    return paths


def run_capacity_docker_command(arguments: list[str], *, timeout: int) -> None:
    """仅在显式容量集成门下执行 Docker，且不回显构建或容器输出。"""

    if os.environ.get("RUN_CAPACITY_DOCKER_INTEGRATION") != "1":
        pytest.skip("未启用 RUN_CAPACITY_DOCKER_INTEGRATION=1")
    docker_cli = resolve_docker_cli(require_integration=True)
    assert_linux_docker_daemon(docker_cli)

    try:
        result = subprocess.run(
            [docker_cli, *arguments],
            cwd=PROJECT_ROOT,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        pytest.fail("容量 Docker 集成命令超时")
    if result.returncode != 0:
        pytest.fail(f"容量 Docker 集成命令失败，退出码 {result.returncode}")


def test_capacity_environment_example_and_gitignore_are_isolated() -> None:
    """公开样例只能启用固定合成哨兵，真实容量 env 继续受忽略。"""

    environment = parse_env_example(read_project_file(".env.capacity-synthetic.example"))
    assert environment == {
        **FAKE_CAPACITY_ENVIRONMENT,
        "POSTGRES_PASSWORD": "",
        "REDIS_PASSWORD": "",
    }

    gitignore = read_project_file(".gitignore")
    assert ".env.*" in gitignore
    assert "!.env.capacity-synthetic.example" in gitignore
    assert "!.env.capacity-synthetic\n" not in gitignore
    assert ".env.dingtalk-test" not in read_project_file(
        "infra/docker/compose.capacity-synthetic.yml"
    )


def test_capacity_dockerfile_is_non_root_and_production_image_stays_clean() -> None:
    """容量镜像只额外复制负载工具，生产 API 镜像保持无合成入口。"""

    capacity = read_project_file("infra/docker/api.capacity-synthetic.Dockerfile")
    production = PRODUCTION_API_DOCKERFILE.read_text(encoding="utf-8")

    for required in (
        "COPY apps/api/app ./app",
        "COPY apps/api/alembic ./alembic",
        "COPY apps/api/alembic.ini ./alembic.ini",
        "COPY tests/load /workspace/tests/load",
        "USER 10001:10001",
        '"dingtalk_capacity_app:create_capacity_app"',
        '"--factory"',
        '"--app-dir"',
        '"/workspace/tests/load"',
        '"--no-access-log"',
    ):
        assert required in capacity
    assert ".env" not in capacity
    production_copy_sources: list[str] = []
    for instruction in production.splitlines():
        if not instruction.startswith("COPY "):
            continue
        copy_arguments = shlex.split(instruction)[1:]
        assert len(copy_arguments) >= 2
        production_copy_sources.extend(copy_arguments[:-1])
    assert production_copy_sources == [
        "apps/api/pyproject.toml",
        "apps/api/app",
        "apps/api/alembic",
        "apps/api/alembic.ini",
    ]
    assert not any(
        source in {".", "./", "tests", "tests/"}
        or source.startswith("tests/")
        or any(glob in source for glob in ("*", "?", "["))
        for source in production_copy_sources
    )


def test_capacity_nginx_only_proxies_health_and_exchange() -> None:
    """内部网关只暴露两个精确路径并保持安全粗限流响应。"""

    nginx = read_project_file("infra/nginx/dingtalk-capacity-synthetic.conf.template")
    exchange = extract_nginx_block(
        nginx,
        "location = /api/v1/auth/dingtalk/exchange",
    )
    health = extract_nginx_block(nginx, "location = /api/v1/health")
    rate_limited = extract_nginx_block(
        nginx,
        "location @dingtalk_login_rate_limited",
    )
    fallback = extract_nginx_block(nginx, "location /")

    assert nginx.count("server {") == 1
    assert "listen 8080 default_server;" in nginx
    assert (
        "limit_req_zone $binary_remote_addr "
        "zone=dingtalk_login_per_ip:10m "
        "rate=${CAMPUS_AI_DINGTALK_LOGIN_IP_RATE};"
    ) in nginx
    assert (
        "limit_req zone=dingtalk_login_per_ip burst=${CAMPUS_AI_DINGTALK_LOGIN_IP_BURST} nodelay;"
    ) in exchange
    assert "limit_req_status 429;" in exchange
    assert "proxy_pass http://api-synthetic:8000;" in exchange
    assert "proxy_pass http://api-synthetic:8000;" in health
    assert "limit_req zone=" not in health
    assert "default_type application/json;" in rate_limited
    assert "add_header Retry-After 2 always;" in rate_limited
    assert 'return 429 \'{"detail":"当前登录人数较多，请稍后重新进入。"}\';' in rate_limited
    assert 'return 404 \'{"detail":"Not Found"}\';' in fallback
    assert "/__capacity__/" not in nginx
    assert "location /api/" not in nginx
    assert "ssl_" not in nginx
    assert "Strict-Transport-Security" not in nginx
    assert "root " not in nginx
    assert "try_files" not in nginx
    assert "CAMPUS_AI_PUBLIC_HOST" not in nginx
    assert "edge" not in nginx
    assert "egress" not in nginx

    log_format = next(line for line in nginx.splitlines() if line.startswith("log_format "))
    assert "$status" in log_format
    assert "$request_method" in log_format
    assert "$uri" in log_format
    assert "$request_time" in log_format
    assert not any(
        unsafe in log_format
        for unsafe in (
            "$remote_addr",
            "$request_uri",
            "$args",
            "$query_string",
            "$http_cookie",
            "$http_authorization",
        )
    )
    assert "error_log /var/log/nginx/error.log crit;" in nginx
    assert "limit_req_log_level error;" in nginx


def test_capacity_compose_text_has_no_public_or_destructive_boundaries() -> None:
    """容量 Compose 不引用公网栈；文档不把配置目标误称为 bridge ACL。"""

    compose = read_project_file("infra/docker/compose.capacity-synthetic.yml")
    file_explanation = read_project_file("docs/engineering/学校综合智能体平台文件说明.md")

    assert f"name: {CAPACITY_PROJECT}" in compose
    assert 'env_file: ["../../.env.capacity-synthetic"]' in compose
    for forbidden in (
        ".env.dingtalk-test",
        "campus-ai-dingtalk-test",
        "minio",
        "CAMPUS_AI_PUBLIC_HOST",
        "CAMPUS_AI_TLS_DIR",
        "/var/run/docker.sock",
        "network_mode:",
        "privileged:",
        "extra_hosts:",
        "external: true",
        "down -v",
    ):
        assert forbidden not in compose
    assert re.search(r"(?m)^\s*ports\s*:", compose) is None
    assert "capacity-loadgen" in compose
    assert 'command: ["--help"]' in compose
    assert "同属唯一内部网络，不声明 Docker bridge 网络 ACL" in file_explanation


def test_readme_and_runbook_document_safe_capacity_entrypoint() -> None:
    """公开文档固定隔离容量入口、结论边界和无秘密示例。"""

    readme = README.read_text(encoding="utf-8")
    runbook = RUNBOOK.read_text(encoding="utf-8")

    for required in (
        CAPACITY_PROJECT,
        "一分钟一百次",
        "三十路并发",
        "内部合成容量不能替代真实钉钉",
        "禁止 `down -v`",
        "--preflight-only",
    ):
        assert required in readme

    for required in (
        CAPACITY_PROJECT,
        "一分钟一百次",
        "三十路并发",
        "内部合成容量不能替代真实钉钉",
        "禁止 `down -v`",
        "--preflight-only",
        "--confirm-project campus-ai-dingtalk-capacity-synthetic",
        "--env-file .env.capacity-synthetic",
        "--report /tmp/campus-ai-dingtalk-capacity-report.json",
    ):
        assert required in runbook

    capacity_section = runbook.split(
        "## 17. 钉钉隔离合成容量测试",
        maxsplit=1,
    )[1]
    assert capacity_section.count("set -euo pipefail") >= 4
    assert 'python_bin="${CAMPUS_AI_CAPACITY_PYTHON_BIN:-$(command -v python3)}"' in (
        capacity_section
    )
    assert capacity_section.count('"$python_bin"') >= 5
    assert capacity_section.count("--profile capacity-loadgen") >= 2
    assert "-f infra/docker/compose.capacity-synthetic.yml" in capacity_section
    assert "--profile capacity-loadgen config --quiet" in capacity_section
    assert "--profile capacity-loadgen build" in capacity_section
    profile_offsets = [capacity_section.index(profile) for profile in CAPACITY_PROFILE_ORDER]
    assert profile_offsets == sorted(profile_offsets)
    assert re.search(r"(?<!\d)1[3-9]\d{9}(?!\d)", capacity_section) is None
    assert re.search(r"(?<!\d)1\d{2}\*{4}\d{4}(?!\d)", capacity_section) is None
    assert (
        re.search(
            r"(?m)^(?:DINGTALK_(?:CORP_ID|APP_KEY|APP_SECRET)|"
            r"(?:POSTGRES|REDIS)_PASSWORD)=\S+",
            capacity_section,
        )
        is None
    )
    assert (
        re.search(
            r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])",
            capacity_section,
        )
        is None
    )


def test_capacity_compose_json_is_private_and_parent_environment_safe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """临时假配置证明固定边界；只校验 loadgen 配置目标，不声称网络 ACL。"""

    docker_cli = resolve_docker_cli()
    env_file = tmp_path / "capacity.invalid.env"
    override_file = tmp_path / "compose.capacity.invalid.yml"
    write_fake_capacity_environment(env_file)
    write_fake_compose_override(override_file, env_file)
    for key in COMPOSE_ENVIRONMENT_KEYS_TO_CLEAR:
        monkeypatch.setenv(key, f"conflicting-{key.lower()}.invalid")

    default_config = load_compose_config(
        docker_cli,
        env_file,
        None,
        include_loadgen=False,
        resolve_env_files=False,
    )
    default_services = default_config["services"]
    assert isinstance(default_services, dict)
    assert set(default_services) == CAPACITY_SERVICES - {"loadgen"}
    assert all(
        "profiles" not in service
        for service in default_services.values()
        if isinstance(service, dict)
    )

    unresolved = load_compose_config(
        docker_cli,
        env_file,
        None,
        resolve_env_files=False,
    )
    unresolved_services = unresolved["services"]
    assert isinstance(unresolved_services, dict)
    expected_private_env = (PROJECT_ROOT / ".env.capacity-synthetic").resolve()
    assert service_env_file_paths(unresolved_services["migrate"]) == [expected_private_env]
    assert service_env_file_paths(unresolved_services["api-synthetic"]) == [expected_private_env]

    overridden_unresolved = load_compose_config(
        docker_cli,
        env_file,
        override_file,
        resolve_env_files=False,
    )
    overridden_services = overridden_unresolved["services"]
    assert isinstance(overridden_services, dict)
    assert service_env_file_paths(overridden_services["migrate"]) == [env_file.resolve()]
    assert service_env_file_paths(overridden_services["api-synthetic"]) == [env_file.resolve()]

    config = load_compose_config(
        docker_cli,
        env_file,
        override_file,
        resolve_env_files=True,
    )
    assert config["name"] == CAPACITY_PROJECT
    services = config["services"]
    assert isinstance(services, dict)
    assert set(services) == CAPACITY_SERVICES

    for service in services.values():
        assert isinstance(service, dict)
        assert service.get("restart") == "no"
        assert not any(
            port.get("published") is not None
            for port in service.get("ports", ())
            if isinstance(port, dict)
        )
        service_networks = service["networks"]
        assert isinstance(service_networks, dict)
        assert set(service_networks) == {"capacity-private"}
        assert service.get("network_mode") != "host"
        assert service.get("privileged", False) is False
        assert "extra_hosts" not in service
        assert "devices" not in service

    networks = config["networks"]
    assert isinstance(networks, dict)
    assert set(networks) == {"capacity-private"}
    capacity_network = networks["capacity-private"]
    assert isinstance(capacity_network, dict)
    assert capacity_network["internal"] is True
    assert capacity_network.get("external", False) is False
    assert str(capacity_network["name"]).startswith(f"{CAPACITY_PROJECT}_")

    volumes = config["volumes"]
    assert isinstance(volumes, dict)
    assert set(volumes) == {"capacity-postgres-data", "capacity-redis-data"}
    for volume in volumes.values():
        assert isinstance(volume, dict)
        assert volume.get("external", False) is False
        assert str(volume["name"]).startswith(f"{CAPACITY_PROJECT}_")

    postgres = services["postgres"]
    redis = services["redis"]
    migrate = services["migrate"]
    api = services["api-synthetic"]
    gateway = services["gateway"]
    loadgen = services["loadgen"]
    assert isinstance(postgres, dict)
    assert isinstance(redis, dict)
    assert isinstance(migrate, dict)
    assert isinstance(api, dict)
    assert isinstance(gateway, dict)
    assert isinstance(loadgen, dict)

    assert postgres["image"] == "postgres:16-alpine"
    assert redis["image"] == "redis:7-alpine"
    assert gateway["image"] == "nginx:1.27-alpine"
    capacity_image = f"{CAPACITY_PROJECT}-api:local"
    assert migrate["image"] == capacity_image
    assert api["image"] == capacity_image
    assert loadgen["image"] == capacity_image

    api_build = api["build"]
    assert isinstance(api_build, dict)
    assert Path(str(api_build["context"])).resolve() == PROJECT_ROOT.resolve()
    assert (
        Path(str(api_build["dockerfile"])).as_posix()
        == "infra/docker/api.capacity-synthetic.Dockerfile"
    )
    assert "build" not in migrate
    assert "build" not in loadgen

    assert "env_file" not in loadgen
    assert loadgen.get("environment", {}) == {}
    loadgen_serialized = json.dumps(loadgen, sort_keys=True).lower()
    for forbidden in (
        "postgres",
        "redis",
        "password",
        "secret",
        "corp",
        "app_key",
    ):
        assert forbidden not in loadgen_serialized
    assert loadgen["profiles"] == ["capacity-loadgen"]
    assert loadgen["entrypoint"] == [
        "python",
        "/workspace/tests/load/dingtalk_capacity_runner.py",
    ]
    assert loadgen["command"] == ["--help"]
    loadgen_runner = read_project_file("tests/load/dingtalk_capacity_runner.py")
    assert set(re.findall(r'"(http://[^"]+)"', loadgen_runner)) == {
        "http://api-synthetic:8000",
        "http://gateway:8080",
    }
    assert not any(
        database_target in loadgen_runner.lower()
        for database_target in ("postgres", "redis", ":5432", ":6379")
    )

    assert migrate["depends_on"]["postgres"]["condition"] == "service_healthy"
    assert migrate["depends_on"]["redis"]["condition"] == "service_healthy"
    assert api["depends_on"]["postgres"]["condition"] == "service_healthy"
    assert api["depends_on"]["redis"]["condition"] == "service_healthy"
    assert api["depends_on"]["migrate"]["condition"] == "service_completed_successfully"
    assert gateway["depends_on"]["api-synthetic"]["condition"] == "service_healthy"
    assert loadgen["depends_on"]["api-synthetic"]["condition"] == "service_healthy"
    assert loadgen["depends_on"]["gateway"]["condition"] == "service_healthy"

    expected_api_environment = {
        key: value
        for key, value in FAKE_CAPACITY_ENVIRONMENT.items()
        if key
        not in {
            "CAMPUS_AI_DINGTALK_LOGIN_IP_RATE",
            "CAMPUS_AI_DINGTALK_LOGIN_IP_BURST",
        }
    }
    assert api["environment"].items() >= expected_api_environment.items()
    assert migrate["environment"].items() >= expected_api_environment.items()
    assert gateway["environment"] == {
        "CAMPUS_AI_DINGTALK_LOGIN_IP_BURST": "30",
        "CAMPUS_AI_DINGTALK_LOGIN_IP_RATE": "5r/s",
        "NGINX_ENVSUBST_FILTER": ("^CAMPUS_AI_DINGTALK_LOGIN_IP_(RATE|BURST)$$"),
    }

    postgres_mounts = {
        mount["target"]: mount for mount in postgres["volumes"] if isinstance(mount, dict)
    }
    redis_mounts = {mount["target"]: mount for mount in redis["volumes"] if isinstance(mount, dict)}
    assert postgres_mounts["/var/lib/postgresql/data"]["type"] == "volume"
    assert redis_mounts["/data"]["type"] == "volume"
    gateway_mounts = {
        mount["target"]: mount for mount in gateway["volumes"] if isinstance(mount, dict)
    }
    nginx_mount = gateway_mounts["/etc/nginx/templates/default.conf.template"]
    assert nginx_mount["type"] == "bind"
    assert nginx_mount["read_only"] is True
    assert Path(str(nginx_mount["source"])).resolve() == CAPACITY_NGINX.resolve()


def test_capacity_real_image_build_is_explicitly_gated() -> None:
    """容量 Python 镜像构建只在独立显式集成门下运行。"""

    run_capacity_docker_command(
        [
            "build",
            "--file",
            str(CAPACITY_DOCKERFILE),
            "--tag",
            f"{CAPACITY_PROJECT}-api:integration-invalid",
            str(PROJECT_ROOT),
        ],
        timeout=600,
    )


def test_capacity_nginx_container_config_is_explicitly_gated() -> None:
    """Nginx 模板只在显式门下以无网络临时容器执行配置检查。"""

    run_capacity_docker_command(
        [
            "run",
            "--rm",
            "--network",
            "none",
            "--add-host",
            "api-synthetic:127.0.0.1",
            "--env",
            "CAMPUS_AI_DINGTALK_LOGIN_IP_RATE=5r/s",
            "--env",
            "CAMPUS_AI_DINGTALK_LOGIN_IP_BURST=30",
            "--env",
            "NGINX_ENVSUBST_FILTER=^CAMPUS_AI_DINGTALK_LOGIN_IP_(RATE|BURST)$",
            "--volume",
            f"{CAPACITY_NGINX}:/etc/nginx/templates/default.conf.template:ro",
            "nginx:1.27-alpine",
            "nginx",
            "-t",
        ],
        timeout=120,
    )
