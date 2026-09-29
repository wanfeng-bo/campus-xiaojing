from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[3]
FAKE_DINGTALK_TEST_ENVIRONMENT = {
    "CAMPUS_AI_PUBLIC_HOST": "dingtalk-test.invalid",
    "CAMPUS_AI_TLS_DIR": "/tmp/campus-ai-dingtalk-test.invalid/tls",
    "APP_ENV": "test",
    "IDENTITY_PROVIDER": "dingtalk",
    "POSTGRES_HOST": "postgres",
    "POSTGRES_PORT": "5432",
    "POSTGRES_DB": "campus_ai_dingtalk_test",
    "POSTGRES_USER": "campus_ai_dingtalk_test",
    "POSTGRES_PASSWORD": "fake-postgres-secret",
    "REDIS_HOST": "redis",
    "REDIS_PORT": "6379",
    "REDIS_PASSWORD": "fake-redis-secret",
    "MINIO_ROOT_USER": "campus-ai-dingtalk-test",
    "MINIO_ROOT_PASSWORD": "fake-minio-secret",
    "MINIO_BUCKET": "uploads-dingtalk-test",
    "DINGTALK_CORP_ID": "fake-corp-id",
    "DINGTALK_APP_KEY": "fake-app-key",
    "DINGTALK_APP_SECRET": "fake-app-secret",
    "DINGTALK_IDENTITY_TIMEOUT_SECONDS": "5",
    "CAMPUS_AI_DINGTALK_LOGIN_IP_RATE": "5r/s",
    "CAMPUS_AI_DINGTALK_LOGIN_IP_BURST": "30",
    "IDENTITY_SESSION_COOKIE_NAME": "campus_ai_dingtalk_test_session",
    "CAMPUS_AI_ICP_FILING_REQUIRED": "true",
    "VITE_ICP_FILING_NUMBER": "fake-icp-filing-number",
}
COMPOSE_ENVIRONMENT_KEYS_TO_CLEAR = frozenset(
    (
        *FAKE_DINGTALK_TEST_ENVIRONMENT,
        "CAMPUS_AI_DINGTALK_TEST_ENV_FILE",
        "COMPOSE_PROJECT_NAME",
        "COMPOSE_FILE",
    )
)


def read_project_file(relative_path: str) -> str:
    """读取部署资产文本，避免测试依赖 YAML 或 Nginx 额外解析库。"""

    return (PROJECT_ROOT / relative_path).read_text(encoding="utf-8")


def extract_nginx_block(nginx: str, opening_line: str) -> str:
    """按花括号深度提取指定 Nginx 块，避免相邻 location 造成误匹配。"""

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
            continue
        if character == "{":
            depth += 1
        elif character == "}":
            depth -= 1
            if depth == 0:
                return nginx[opening_offset : offset + 1]

    pytest.fail(f"Nginx 块缺少结束花括号：{opening_line}")


def extract_all_nginx_server_blocks(nginx: str) -> list[str]:
    """按活动 `server {` 起始行提取所有完整 server 块。"""

    server_openings = list(re.finditer(r"^\s*server\s*\{", nginx, flags=re.MULTILINE))
    blocks: list[str] = []
    for opening in server_openings:
        opening_brace_offset = nginx.find("{", opening.start(), opening.end())
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
                continue
            if character == "{":
                depth += 1
            elif character == "}":
                depth -= 1
                if depth == 0:
                    blocks.append(nginx[opening.start() : offset + 1])
                    break
        else:
            pytest.fail("Nginx server 块缺少结束花括号")
    return blocks


def collect_active_nginx_directives(nginx: str, directive: str) -> list[str]:
    """收集指定活动 directive 的完整参数，支持跨行且不返回块头。"""

    return [
        arguments
        for parsed_directive, arguments in parse_active_nginx_directives(nginx)
        if parsed_directive == directive
    ]


def parse_active_nginx_directives(nginx: str) -> list[tuple[str, str]]:
    """解析未注释的 Nginx directive，保留引号/转义内容并在未引用分号处结束。"""

    directives: list[tuple[str, str]] = []
    statement: list[str] = []
    quote: str | None = None
    escaped = False
    in_comment = False
    in_braced_variable = False

    def append_separator() -> None:
        if statement and statement[-1] != " ":
            statement.append(" ")

    def finish_statement() -> None:
        raw_statement = "".join(statement).strip()
        statement.clear()
        if not raw_statement:
            return
        name, separator, arguments = raw_statement.partition(" ")
        directives.append((name, arguments.strip() if separator else ""))

    for character in nginx:
        if in_comment:
            if character in "\r\n":
                in_comment = False
                append_separator()
            continue
        if escaped:
            statement.append(character)
            escaped = False
            continue
        if character == "\\":
            statement.append(character)
            escaped = True
            continue
        if quote is not None:
            statement.append(character)
            if character == quote:
                quote = None
            continue
        if in_braced_variable:
            statement.append(character)
            if character == "}":
                in_braced_variable = False
            continue
        if character in {"'", '"'}:
            statement.append(character)
            quote = character
        elif character == "#":
            in_comment = True
        elif character == "{" and statement and statement[-1] == "$":
            statement.append(character)
            in_braced_variable = True
        elif character == ";":
            finish_statement()
        elif character in "{}":
            statement.clear()
        elif character.isspace():
            append_separator()
        else:
            statement.append(character)

    return directives


def extract_nginx_variables(value: str) -> list[str]:
    """按出现顺序解析 `$name` 与 `${name}`，避免花括号变量绕过敏感字段检查。"""

    variables: list[str] = []
    for braced_name, plain_name in re.findall(
        r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)",
        value,
    ):
        variables.append(f"${braced_name or plain_name}")
    return variables


def test_nginx_variable_parser_recognizes_plain_and_braced_forms() -> None:
    """安全格式变量解析必须同时识别普通与花括号写法并保留重复和顺序。"""

    assert extract_nginx_variables("$status ${remote_addr} $request_method ${args} $status") == [
        "$status",
        "$remote_addr",
        "$request_method",
        "$args",
        "$status",
    ]


def test_nginx_directive_collector_parses_multiline_quotes_escapes_and_comments() -> None:
    """日志 directive 可跨行，且注释、引号内分号、转义和块头不能污染解析结果。"""

    nginx = (
        "# access_log /tmp/commented.log combined;\n"
        "log_format campus_multiline\n"
        "    'status=$status literal=semi;quoted escaped=\\\"value\\\"';\n"
        "access_log\n"
        "    /var/log/nginx/access.log # 参数间注释\n"
        "    campus_multiline;\n"
        "error_log\n"
        "    /var/log/nginx/error.log\n"
        "    crit;\n"
        "limit_req_log_level\n"
        "    error;\n"
        "server {\n"
        "    location /api/ {\n"
        "        access_log\n"
        "            /var/log/nginx/api.log\n"
        "            campus_multiline;\n"
        "    }\n"
        "}\n"
    )

    assert collect_active_nginx_directives(nginx, "log_format") == [
        "campus_multiline 'status=$status literal=semi;quoted escaped=\\\"value\\\"'"
    ]
    assert collect_active_nginx_directives(nginx, "access_log") == [
        "/var/log/nginx/access.log campus_multiline",
        "/var/log/nginx/api.log campus_multiline",
    ]
    assert collect_active_nginx_directives(nginx, "error_log") == ["/var/log/nginx/error.log crit"]
    assert collect_active_nginx_directives(nginx, "limit_req_log_level") == ["error"]
    assert collect_active_nginx_directives(nginx, "server") == []
    assert collect_active_nginx_directives(nginx, "location") == []


def resolve_docker_cli(*, require_integration: bool = False) -> str:
    """解析只用于 Compose 配置展开的 Docker CLI；不可用时安全跳过。"""

    configured_cli = os.environ.get("CAMPUS_AI_DOCKER_CLI")
    if configured_cli:
        if not Path(configured_cli).is_file():
            if require_integration:
                pytest.fail("显式网页构建要求可用的 Docker CLI")
            pytest.skip("CAMPUS_AI_DOCKER_CLI 指向的 Docker CLI 不可用")
        return configured_cli

    discovered_cli = shutil.which("docker")
    if discovered_cli is None:
        if require_integration:
            pytest.fail("显式网页构建要求可用的 Docker CLI")
        pytest.skip("当前环境没有可用的 Docker CLI")
    return discovered_cli


def assert_linux_docker_daemon(docker_cli: str) -> None:
    """显式构建矩阵必须连接可用的 Linux daemon，不能把环境失败算作反例通过。"""

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
        pytest.fail("显式网页构建要求可用的 Docker daemon")
    if daemon_probe.returncode != 0:
        pytest.fail("显式网页构建要求可用的 Docker daemon")
    if daemon_probe.stdout.strip() != "linux":
        pytest.fail("显式网页构建要求 Linux Docker daemon")


def test_explicit_web_build_requires_docker_cli(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """显式构建矩阵不能因缺少 Docker CLI 而跳过。"""

    monkeypatch.setenv("CAMPUS_AI_DOCKER_CLI", str(tmp_path / "missing-docker"))
    with pytest.raises(pytest.fail.Exception, match="Docker CLI"):
        resolve_docker_cli(require_integration=True)


def test_explicit_web_build_requires_linux_daemon(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """显式构建矩阵必须先确认可用的 Linux daemon。"""

    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            args=("docker", "info"),
            returncode=0,
            stdout="windows\n",
            stderr="",
        ),
    )
    with pytest.raises(pytest.fail.Exception, match="Linux"):
        assert_linux_docker_daemon("docker")


def run_web_build_contract(
    *,
    corp_id: str,
    client_id: str,
    filing_required: str,
    filing_number: str,
) -> subprocess.CompletedProcess[str]:
    """按显式集成开关运行真实 build stage，默认测试不意外触发慢速镜像构建。"""

    if os.environ.get("RUN_DOCKER_BUILD_INTEGRATION") != "1":
        pytest.skip("未启用 RUN_DOCKER_BUILD_INTEGRATION=1")
    docker_cli = resolve_docker_cli(require_integration=True)
    assert_linux_docker_daemon(docker_cli)
    try:
        return subprocess.run(
            [
                docker_cli,
                "build",
                "--target",
                "build",
                "--file",
                str(PROJECT_ROOT / "infra/docker/web.dingtalk-test.Dockerfile"),
                "--build-arg",
                f"VITE_DINGTALK_CORP_ID={corp_id}",
                "--build-arg",
                f"VITE_DINGTALK_CLIENT_ID={client_id}",
                "--build-arg",
                f"CAMPUS_AI_ICP_FILING_REQUIRED={filing_required}",
                "--build-arg",
                f"VITE_ICP_FILING_NUMBER={filing_number}",
                str(PROJECT_ROOT),
            ],
            cwd=PROJECT_ROOT,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=600,
        )
    except subprocess.TimeoutExpired:
        pytest.fail("网页镜像构建契约验证超过 600 秒")


def write_fake_dingtalk_test_environment(target: Path) -> None:
    """写入仅供 Compose 展开的假配置，不使用真实域名、凭证或公网参数。"""

    target.write_text(
        "".join(f"{key}={value}\n" for key, value in FAKE_DINGTALK_TEST_ENVIRONMENT.items()),
        encoding="utf-8",
    )


def write_fake_compose_override(target: Path, env_file: Path) -> None:
    """用临时 override 替换 API env_file，避免生产 Compose 暴露测试后门。"""

    target.write_text(
        "\n".join(
            (
                "services:",
                "  api:",
                f"    env_file: !override [{json.dumps(str(env_file))}]",
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
    resolve_env_files: bool = True,
) -> dict[str, object]:
    """展开 Compose JSON；失败信息不回显可能包含假秘密的配置内容。"""

    subprocess_environment = os.environ.copy()
    for key in COMPOSE_ENVIRONMENT_KEYS_TO_CLEAR:
        subprocess_environment.pop(key, None)
    subprocess_environment["CAMPUS_AI_DINGTALK_TEST_ENV_FILE"] = "conflicting-parent.invalid"

    config_arguments = [
        docker_cli,
        "compose",
        "--env-file",
        str(env_file),
        "-f",
        str(PROJECT_ROOT / "infra/docker/compose.dingtalk-test.yml"),
    ]
    if override_file is not None:
        config_arguments.extend(("-f", str(override_file)))
    config_arguments.append("config")
    if not resolve_env_files:
        config_arguments.append("--no-env-resolution")
    config_arguments.extend(("--format", "json"))

    try:
        result = subprocess.run(
            config_arguments,
            cwd=PROJECT_ROOT,
            env=subprocess_environment,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        pytest.fail("Docker Compose 配置展开超过 30 秒")
    if result.returncode != 0:
        pytest.fail(f"Docker Compose 配置展开失败，退出码 {result.returncode}")

    try:
        config = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        pytest.fail(f"Docker Compose 未返回有效 JSON：{error.msg}，位置 {error.pos}")
    if not isinstance(config, dict):
        pytest.fail("Docker Compose JSON 顶层必须是对象")
    return config


def test_private_environment_example_enforces_dingtalk_test_boundary() -> None:
    env_example = read_project_file(".env.dingtalk-test.example")

    assert "APP_ENV=test" in env_example
    assert "IDENTITY_PROVIDER=dingtalk" in env_example
    assert "POSTGRES_DB=campus_ai_dingtalk_test" in env_example
    assert "MINIO_BUCKET=uploads-dingtalk-test" in env_example
    assert "IDENTITY_SESSION_COOKIE_NAME=campus_ai_dingtalk_test_session" in env_example
    assert "DINGTALK_CORP_ID=" in env_example
    assert "DINGTALK_APP_KEY=" in env_example
    assert "DINGTALK_APP_SECRET=" in env_example
    assert "CAMPUS_AI_ICP_FILING_REQUIRED=true" in env_example
    assert "VITE_ICP_FILING_NUMBER=" in env_example
    assert "CAMPUS_AI_DINGTALK_LOGIN_IP_RATE=5r/s" in env_example
    assert "CAMPUS_AI_DINGTALK_LOGIN_IP_BURST=30" in env_example


def test_compose_only_publishes_https_gateway() -> None:
    compose = read_project_file("infra/docker/compose.dingtalk-test.yml")

    assert compose.count("ports:") == 1
    assert 'ports: ["80:80", "443:443"]' in compose
    assert "${CAMPUS_AI_TLS_DIR}:/etc/nginx/tls:ro" in compose
    assert 'extra_hosts: ["${CAMPUS_AI_PUBLIC_HOST}:127.0.0.1"]' in compose
    assert "https://$${CAMPUS_AI_PUBLIC_HOST}/" in compose
    assert 'env_file: ["../../.env.dingtalk-test"]' in compose
    assert "VITE_DINGTALK_CORP_ID: ${DINGTALK_CORP_ID}" in compose
    assert "VITE_DINGTALK_CLIENT_ID: ${DINGTALK_APP_KEY}" in compose
    assert "VITE_DINGTALK_APP_SECRET" not in compose
    assert "CAMPUS_AI_ICP_FILING_REQUIRED: ${CAMPUS_AI_ICP_FILING_REQUIRED}" in compose
    assert "CAMPUS_AI_DINGTALK_TEST_ENV_FILE" not in compose
    assert "networks: [private, egress]" not in compose
    assert (
        "    networks:\n"
        "      private:\n"
        "        gw_priority: 0\n"
        "      egress:\n"
        "        gw_priority: 1"
    ) in compose
    assert "  egress:\n" in compose
    assert "  private:\n    internal: true" in compose
    assert "latest" not in compose


def test_compose_json_preserves_https_deployment_boundaries(tmp_path: Path) -> None:
    docker_cli = resolve_docker_cli()
    env_file = tmp_path / "dingtalk-test.invalid.env"
    override_file = tmp_path / "compose.dingtalk-test.invalid.yml"
    write_fake_dingtalk_test_environment(env_file)
    write_fake_compose_override(override_file, env_file)

    production_config = load_compose_config(
        docker_cli,
        env_file,
        None,
        resolve_env_files=False,
    )
    production_services = production_config["services"]
    assert isinstance(production_services, dict)
    production_api = production_services["api"]
    assert isinstance(production_api, dict)
    production_env_files = production_api["env_file"]
    assert isinstance(production_env_files, list)
    assert len(production_env_files) == 1
    production_env_file = production_env_files[0]
    if isinstance(production_env_file, dict):
        production_env_file = production_env_file["path"]
    assert (
        Path(str(production_env_file)).resolve() == (PROJECT_ROOT / ".env.dingtalk-test").resolve()
    )

    unresolved_config = load_compose_config(
        docker_cli,
        env_file,
        override_file,
        resolve_env_files=False,
    )
    unresolved_services = unresolved_config["services"]
    assert isinstance(unresolved_services, dict)
    unresolved_api = unresolved_services["api"]
    assert isinstance(unresolved_api, dict)
    unresolved_env_files = unresolved_api["env_file"]
    assert isinstance(unresolved_env_files, list)
    assert len(unresolved_env_files) == 1
    unresolved_env_file = unresolved_env_files[0]
    if isinstance(unresolved_env_file, dict):
        unresolved_env_file = unresolved_env_file["path"]
    assert Path(str(unresolved_env_file)).resolve() == env_file.resolve()

    config = load_compose_config(docker_cli, env_file, override_file)
    if config.get("name") != "campus-ai-dingtalk-test":
        pytest.fail("Compose 项目名未使用受控项目配置")

    services = config["services"]
    assert isinstance(services, dict)
    assert set(services) == {"postgres", "redis", "minio", "minio-init", "api", "web"}

    published_services = {
        service_name
        for service_name, service in services.items()
        if isinstance(service, dict)
        and any(port.get("published") is not None for port in service.get("ports", ()))
    }
    assert published_services == {"web"}

    web = services["web"]
    assert isinstance(web, dict)
    web_build = web["build"]
    assert isinstance(web_build, dict)
    web_build_args = web_build["args"]
    assert isinstance(web_build_args, dict)
    assert (
        web_build_args["VITE_DINGTALK_CORP_ID"]
        == FAKE_DINGTALK_TEST_ENVIRONMENT["DINGTALK_CORP_ID"]
    )
    assert (
        web_build_args["VITE_DINGTALK_CLIENT_ID"]
        == FAKE_DINGTALK_TEST_ENVIRONMENT["DINGTALK_APP_KEY"]
    )
    assert "DINGTALK_APP_SECRET" not in web_build_args
    assert FAKE_DINGTALK_TEST_ENVIRONMENT["DINGTALK_APP_SECRET"] not in web_build_args.values()
    assert web_build_args["CAMPUS_AI_ICP_FILING_REQUIRED"] == "true"
    assert web_build_args["VITE_ICP_FILING_NUMBER"] == "fake-icp-filing-number"
    web_environment = web["environment"]
    assert isinstance(web_environment, dict)
    if (
        web_environment.get("CAMPUS_AI_PUBLIC_HOST")
        != FAKE_DINGTALK_TEST_ENVIRONMENT["CAMPUS_AI_PUBLIC_HOST"]
    ):
        pytest.fail("web 公开域名未绑定临时假配置")
    if (
        web_environment.get("CAMPUS_AI_DINGTALK_LOGIN_IP_RATE")
        != FAKE_DINGTALK_TEST_ENVIRONMENT["CAMPUS_AI_DINGTALK_LOGIN_IP_RATE"]
    ):
        pytest.fail("web 单 IP 登录速率未绑定临时假配置")
    if (
        web_environment.get("CAMPUS_AI_DINGTALK_LOGIN_IP_BURST")
        != FAKE_DINGTALK_TEST_ENVIRONMENT["CAMPUS_AI_DINGTALK_LOGIN_IP_BURST"]
    ):
        pytest.fail("web 单 IP 登录突发容量未绑定临时假配置")
    assert web_environment.get("NGINX_ENVSUBST_FILTER") == (
        "^CAMPUS_AI_(PUBLIC_HOST|DINGTALK_LOGIN_IP_RATE|DINGTALK_LOGIN_IP_BURST)$$"
    ), "Nginx envsubst 过滤范围必须只允许公开登录保护变量"

    api = services["api"]
    assert isinstance(api, dict)
    api_networks = api["networks"]
    assert isinstance(api_networks, dict)
    assert set(api_networks) == {"private", "egress"}
    private_attachment = api_networks["private"]
    egress_attachment = api_networks["egress"]
    assert isinstance(private_attachment, dict)
    assert isinstance(egress_attachment, dict)
    assert egress_attachment["gw_priority"] > private_attachment.get("gw_priority", 0)
    api_environment = api["environment"]
    assert isinstance(api_environment, dict)
    expected_private_markers = {
        "DINGTALK_APP_SECRET": FAKE_DINGTALK_TEST_ENVIRONMENT["DINGTALK_APP_SECRET"],
        "POSTGRES_PASSWORD": FAKE_DINGTALK_TEST_ENVIRONMENT["POSTGRES_PASSWORD"],
        "REDIS_PASSWORD": FAKE_DINGTALK_TEST_ENVIRONMENT["REDIS_PASSWORD"],
        "MINIO_ROOT_PASSWORD": FAKE_DINGTALK_TEST_ENVIRONMENT["MINIO_ROOT_PASSWORD"],
    }
    if any(
        api_environment.get(key) != expected for key, expected in expected_private_markers.items()
    ):
        pytest.fail("API 环境未完全来自临时假配置")

    published_ports = {
        (int(port["target"]), int(port["published"]))
        for port in web.get("ports", ())
        if port.get("published") is not None
    }
    assert published_ports == {(80, 80), (443, 443)}

    for service_name, service in services.items():
        if service_name == "web":
            continue
        assert isinstance(service, dict)
        assert not any(port.get("published") is not None for port in service.get("ports", ()))
    for service_name in ("postgres", "redis", "minio", "minio-init"):
        service = services[service_name]
        assert isinstance(service, dict)
        assert set(service["networks"]) == {"private"}
    assert set(web["networks"]) == {"edge", "private"}

    networks = config["networks"]
    assert isinstance(networks, dict)
    private_network = networks["private"]
    assert isinstance(private_network, dict)
    assert private_network["internal"] is True
    egress_network = networks["egress"]
    assert isinstance(egress_network, dict)
    assert egress_network.get("internal", False) is False

    volumes = config["volumes"]
    assert isinstance(volumes, dict)
    assert set(volumes) == {"postgres_data", "redis_data", "minio_data"}

    tls_mounts = [
        mount for mount in web.get("volumes", ()) if mount.get("target") == "/etc/nginx/tls"
    ]
    assert len(tls_mounts) == 1
    assert tls_mounts[0]["type"] == "bind"
    assert tls_mounts[0]["read_only"] is True

    healthcheck = web["healthcheck"]
    assert isinstance(healthcheck, dict)
    healthcheck_command = " ".join(str(part) for part in healthcheck["test"])
    assert "https://" in healthcheck_command
    assert "CAMPUS_AI_PUBLIC_HOST" in healthcheck_command


def test_nginx_forces_fixed_host_https_and_safe_proxy_headers() -> None:
    nginx = read_project_file("infra/nginx/dingtalk-test.conf.template")

    assert "listen 80 default_server;" in nginx
    assert "listen 443 ssl default_server;" in nginx
    assert nginx.count("server_name _;") == 2
    assert "ssl_reject_handshake on;" in nginx
    assert nginx.count("return 444;") == 2
    assert "server_name ${CAMPUS_AI_PUBLIC_HOST};" in nginx
    assert "return 308 https://${CAMPUS_AI_PUBLIC_HOST}$request_uri;" in nginx
    assert "ssl_certificate /etc/nginx/tls/tls.crt;" in nginx
    assert "ssl_certificate_key /etc/nginx/tls/tls.key;" in nginx
    assert "Strict-Transport-Security" not in nginx
    assert "includeSubDomains" not in nginx
    assert "preload" not in nginx
    assert "proxy_pass http://api:8000;" in nginx
    assert "proxy_set_header X-Forwarded-Proto https;" in nginx


def test_nginx_coarsely_limits_only_dingtalk_exchange() -> None:
    nginx = read_project_file("infra/nginx/dingtalk-test.conf.template")
    exchange_location = extract_nginx_block(
        nginx,
        "location = /api/v1/auth/dingtalk/exchange",
    )
    generic_api_location = extract_nginx_block(nginx, "location /api/")
    rate_limited_location = extract_nginx_block(
        nginx,
        "location @dingtalk_login_rate_limited",
    )

    assert (
        "limit_req_zone $binary_remote_addr "
        "zone=dingtalk_login_per_ip:10m "
        "rate=${CAMPUS_AI_DINGTALK_LOGIN_IP_RATE};"
    ) in nginx
    assert (
        "limit_req zone=dingtalk_login_per_ip burst=${CAMPUS_AI_DINGTALK_LOGIN_IP_BURST} nodelay;"
    ) in exchange_location
    assert "limit_req_status 429;" in exchange_location
    assert "limit_req zone=" not in generic_api_location
    assert "error_page 429 = @dingtalk_login_rate_limited;" in nginx
    assert "default_type application/json;" in rate_limited_location
    assert 'add_header X-Content-Type-Options "nosniff" always;' in rate_limited_location
    assert 'add_header X-Frame-Options "DENY" always;' in rate_limited_location
    assert 'add_header Referrer-Policy "no-referrer" always;' in rate_limited_location
    assert "add_header Retry-After 2 always;" in rate_limited_location
    assert (
        'return 429 \'{"detail":"当前登录人数较多，请稍后重新进入。"}\';' in rate_limited_location
    )
    assert nginx.count("limit_req zone=dingtalk_login_per_ip") == 1


def test_api_and_nginx_access_logs_use_safe_aggregate_fields() -> None:
    """关闭 Uvicorn 默认访问日志，并只允许 Nginx 输出无客户端标识的聚合字段。"""

    api_dockerfile = read_project_file("infra/docker/api.Dockerfile")
    nginx = read_project_file("infra/nginx/dingtalk-test.conf.template")
    log_formats = collect_active_nginx_directives(nginx, "log_format")
    access_logs = collect_active_nginx_directives(nginx, "access_log")
    error_logs = collect_active_nginx_directives(nginx, "error_log")
    limit_req_log_levels = collect_active_nginx_directives(nginx, "limit_req_log_level")
    server_blocks = extract_all_nginx_server_blocks(nginx)
    command_line = next(
        line.removeprefix("CMD ") for line in api_dockerfile.splitlines() if line.startswith("CMD ")
    )
    api_command = json.loads(command_line)

    assert api_command.count("--no-access-log") == 1
    assert api_command.count("--proxy-headers") == 1
    assert log_formats == [
        "campus_ai_safe 'status=$status method=$request_method uri=$uri request_time=$request_time'"
    ]
    log_variables = extract_nginx_variables(log_formats[0])
    assert log_variables == ["$status", "$request_method", "$uri", "$request_time"]
    assert set(log_variables).isdisjoint(
        {
            "$remote_addr",
            "$proxy_add_x_forwarded_for",
            "$http_x_forwarded_for",
            "$request",
            "$request_uri",
            "$args",
            "$query_string",
            "$http_cookie",
            "$http_authorization",
            "$http_referer",
            "$http_user_agent",
        }
    )
    assert len(server_blocks) == 4
    for server_block in server_blocks:
        assert collect_active_nginx_directives(server_block, "access_log") == [
            "/var/log/nginx/access.log campus_ai_safe"
        ]
    assert [directive.split() for directive in access_logs] == [
        ["/var/log/nginx/access.log", "campus_ai_safe"]
    ] * 4
    assert error_logs == ["/var/log/nginx/error.log crit"]
    assert limit_req_log_levels == ["error"]
    error_log_target, error_log_threshold = error_logs[0].split()
    assert error_log_target == "/var/log/nginx/error.log"
    limit_level = limit_req_log_levels[0]
    nginx_log_levels = ("debug", "info", "notice", "warn", "error", "crit", "alert", "emerg")
    assert nginx_log_levels.index(limit_level) < nginx_log_levels.index(error_log_threshold)
    assert "$status" in log_variables
    assert 'return 429 \'{"detail":"当前登录人数较多，请稍后重新进入。"}\';' in nginx


def test_web_build_requires_public_dingtalk_identifiers_and_consistent_filing() -> None:
    """网页镜像必须在依赖安装前拒绝非法备案模式，且继续使用 HTTPS 模板。"""

    dockerfile = read_project_file("infra/docker/web.dingtalk-test.Dockerfile")

    assert "ARG VITE_DINGTALK_CORP_ID" in dockerfile
    assert "ARG VITE_DINGTALK_CLIENT_ID" in dockerfile
    assert "ARG CAMPUS_AI_ICP_FILING_REQUIRED" in dockerfile
    assert "ARG VITE_ICP_FILING_NUMBER" in dockerfile
    assert "ENV VITE_DINGTALK_CLIENT_ID=${VITE_DINGTALK_CLIENT_ID}" in dockerfile
    assert 'case "$VITE_DINGTALK_CORP_ID" in' in dockerfile
    assert 'case "$VITE_DINGTALK_CLIENT_ID" in' in dockerfile
    assert '""|*[[:space:]]*) exit 1' in dockerfile
    assert 'case "$CAMPUS_AI_ICP_FILING_REQUIRED" in' in dockerfile
    assert "*[[:space:]]*) exit 1" in dockerfile
    assert 'false) test -z "$VITE_ICP_FILING_NUMBER"' in dockerfile
    assert "*) exit 1" in dockerfile
    assert dockerfile.index('RUN case "$VITE_DINGTALK_CORP_ID" in') < dockerfile.index("RUN npm ci")
    assert dockerfile.index('case "$VITE_DINGTALK_CLIENT_ID" in') < dockerfile.index("RUN npm ci")
    assert "/etc/nginx/templates/default.conf.template" in dockerfile
    assert "EXPOSE 80 443" in dockerfile


@pytest.mark.parametrize(
    ("corp_id", "client_id", "filing_required", "filing_number", "expected_success"),
    (
        ("fake-corp-id", "fake-client-id", "true", "fake-icp-number", True),
        ("fake-corp-id", "fake-client-id", "true", "", False),
        ("fake-corp-id", "fake-client-id", "true", " ", False),
        ("fake-corp-id", "fake-client-id", "false", "", True),
        ("fake-corp-id", "fake-client-id", "false", "fake-icp-number", False),
        ("fake-corp-id", "fake-client-id", "unexpected", "", False),
        ("fake-corp-id", "fake-client-id", "", "", False),
        ("fake-corp-id", "fake-client-id", "TRUE", "fake-icp-number", False),
        ("", "fake-client-id", "false", "", False),
        (" ", "fake-client-id", "false", "", False),
        ("fake-corp-id", "", "false", "", False),
        ("fake-corp-id", " ", "false", "", False),
    ),
)
def test_web_build_enforces_public_identity_and_filing_matrix(
    corp_id: str,
    client_id: str,
    filing_required: str,
    filing_number: str,
    expected_success: bool,
) -> None:
    """真实 build stage 必须按备案模式矩阵成功或失败，不能依赖占位值绕过。"""

    result = run_web_build_contract(
        corp_id=corp_id,
        client_id=client_id,
        filing_required=filing_required,
        filing_number=filing_number,
    )
    assert (result.returncode == 0) is expected_success


def test_private_environment_example_is_explicitly_tracked() -> None:
    gitignore = read_project_file(".gitignore")

    assert "!.env.dingtalk-test.example" in gitignore


def test_operator_docs_explain_public_client_id_and_secret_boundary() -> None:
    readme = read_project_file("README.md")
    runbook = read_project_file("docs/runbooks/钉钉测试企业HTTPS部署.md")

    assert "VITE_DINGTALK_CLIENT_ID" in readme
    assert "原 AppKey" in readme
    assert "App Secret 只进入 API 运行环境" in readme
    assert (
        "AppKey 作为网页应用 Client ID 映射到 `VITE_DINGTALK_CLIENT_ID`，同时保留它供 API 使用。"
    ) in runbook
    assert "App Secret 只进入 API 运行环境" in runbook
    assert "App Secret 不进入网页镜像" in runbook
    assert "Android/iOS 不低于 `7.0.45`" in runbook
    assert "Mac/Windows 不低于 `7.0.50`" in runbook
    assert "-u DINGTALK_APP_KEY" in runbook
    assert "公开 Corp ID 只进入 `.env.dingtalk-test` 的构建公开参数" not in runbook
    assert "App Key、App Secret 和其他凭证始终留在私有环境" not in runbook


def test_runbook_documents_compose_environment_boundaries() -> None:
    runbook = read_project_file("docs/runbooks/钉钉测试企业HTTPS部署.md")
    compose = read_project_file("infra/docker/compose.dingtalk-test.yml")

    assert "CAMPUS_AI_ICP_FILING_REQUIRED=true" in runbook
    assert "CAMPUS_AI_ICP_FILING_REQUIRED=false" in runbook
    assert "腾讯云中国香港新服务器首次启用" in runbook
    assert "不得在腾讯云执行任何 `.env.internal`" in runbook
    assert "停止腾讯云测试企业 `web` 后不启动任何 `.env.internal` 服务" in runbook
    assert "campus_ai_dingtalk_compose()" in runbook
    assert "-u DINGTALK_CORP_ID" in runbook
    assert "-u CAMPUS_AI_ICP_FILING_REQUIRED" in runbook
    assert "-u VITE_ICP_FILING_NUMBER" in runbook
    assert "--project-name campus-ai-dingtalk-test" in runbook
    assert '"$@"' in runbook
    assert "父 Shell 同名变量的优先级高于 `--env-file`" in runbook
    compose_interpolation_variables = set(re.findall(r"\$\{([A-Z_][A-Z0-9_]*)", compose))
    for variable_name in compose_interpolation_variables:
        assert f"-u {variable_name}" in runbook
    for compose_control_variable in (
        "COMPOSE_FILE",
        "COMPOSE_PROJECT_NAME",
        "COMPOSE_PROFILES",
    ):
        assert f"-u {compose_control_variable}" in runbook
    assert (
        re.search(
            r"(?m)^[ \t]*docker compose --env-file \.env\.dingtalk-test",
            runbook,
        )
        is None
    )
    assert "docker compose \\\n  --env-file .env.dingtalk-test" not in runbook
    assert "API 专用出站网络不发布任何宿主机端口" in runbook
    assert (
        "API 的生产 `env_file` 固定为 `../../.env.dingtalk-test`，不接受父 Shell 环境变量覆盖"
    ) in runbook
    assert (
        "Nginx `envsubst` 白名单仅包含三项："
        "`CAMPUS_AI_PUBLIC_HOST` 用于固定 Host 与证书域名，"
        "`CAMPUS_AI_DINGTALK_LOGIN_IP_RATE` 用于单 IP 登录速率，"
        "`CAMPUS_AI_DINGTALK_LOGIN_IP_BURST` 用于允许的突发请求数"
    ) in runbook
