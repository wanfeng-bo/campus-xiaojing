from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from app.composition.school_fact_executor_client_settings import (
    SchoolFactExecutorClientSettings,
)
from app.composition.school_fact_writer_settings import SchoolFactWriterSettings
from app.db.migration_settings import MigrationSettings

PROJECT_ROOT = Path(__file__).resolve().parents[4]
COMPOSE_PATH = PROJECT_ROOT / "infra/docker/compose.internal-verification.yml"
API_ENV_PATH = PROJECT_ROOT / ".env.internal.example"
WRITER_ENV_PATH = PROJECT_ROOT / ".env.school-fact-writer.example"
MIGRATION_ENV_PATH = PROJECT_ROOT / ".env.school-fact-migration.example"
ROLE_BOOTSTRAP_ENV_PATH = PROJECT_ROOT / ".env.school-fact-role-bootstrap.example"
CONTAINER_VERIFIER_PATH = PROJECT_ROOT / "infra/docker/verify_sf5b_writer_container.py"
ROOT_RUNTIME_ENV_PATH = PROJECT_ROOT / ".env.example"
LOCAL_DATABASE_CONTROL_ENV_PATH = PROJECT_ROOT / ".env.local-database-control.example"


def _container_verifier() -> Any:
    module_name = "test_sf5b_writer_container_verifier"
    spec = importlib.util.spec_from_file_location(module_name, CONTAINER_VERIFIER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _secure_runtime_inspections(verifier: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    writer = {
        "Config": {
            "User": "10002:10002",
            "Cmd": ["uvicorn", verifier.EXPECTED_WRITER_FACTORY, "--factory"],
            "Env": [f"{name}=private" for name in verifier._WRITER_REQUIRED_ENV],
        },
        "HostConfig": {
            "ReadonlyRootfs": True,
            "CapDrop": ["ALL"],
            "SecurityOpt": ["no-new-privileges:true"],
            "Tmpfs": {"/tmp": "rw,noexec,nosuid,size=16777216"},
            "PortBindings": {},
        },
        "Mounts": [],
        "NetworkSettings": {
            "Networks": {
                "campus-ai-internal_school-fact-database": {"NetworkID": "school-fact-database-id"},
                "campus-ai-internal_school-fact-writer-ipc": {
                    "NetworkID": "school-fact-writer-ipc-id"
                },
            },
            "Ports": {"8010/tcp": None},
        },
    }
    api = {
        "Config": {
            "User": "10001:10001",
            "Cmd": ["uvicorn", "app.main:app"],
            "Env": [f"{name}=private" for name in verifier._API_REQUIRED_ENV],
        },
        "HostConfig": {"PortBindings": {}},
        "Mounts": [],
        "NetworkSettings": {
            "Networks": {
                "campus-ai-internal_private": {"NetworkID": "private-id"},
                "campus-ai-internal_school-fact-writer-ipc": {
                    "NetworkID": "school-fact-writer-ipc-id"
                },
            },
            "Ports": {},
        },
    }
    return writer, api


def _secure_runtime_networks() -> dict[str, dict[str, Any]]:
    project = {"com.docker.compose.project": "campus-ai-internal"}
    return {
        "campus-ai-internal_school-fact-database": {
            "Name": "campus-ai-internal_school-fact-database",
            "Id": "school-fact-database-id",
            "Internal": True,
            "Labels": {
                **project,
                "com.docker.compose.network": "school-fact-database",
            },
        },
        "campus-ai-internal_school-fact-writer-ipc": {
            "Name": "campus-ai-internal_school-fact-writer-ipc",
            "Id": "school-fact-writer-ipc-id",
            "Internal": True,
            "Labels": {
                **project,
                "com.docker.compose.network": "school-fact-writer-ipc",
            },
        },
        "campus-ai-internal_private": {
            "Name": "campus-ai-internal_private",
            "Id": "private-id",
            "Internal": True,
            "Labels": {**project, "com.docker.compose.network": "private"},
        },
    }


def _verify_synthetic_runtime(
    monkeypatch: pytest.MonkeyPatch,
    verifier: Any,
    *,
    writer: dict[str, Any],
    api: dict[str, Any],
    networks: dict[str, dict[str, Any]],
) -> tuple[set[str], set[str]]:
    containers = {verifier.WRITER_SERVICE: writer, verifier.API_SERVICE: api}
    monkeypatch.setattr(verifier, "_inspect", lambda _arguments, service: containers[service])
    monkeypatch.setattr(
        verifier,
        "_inspect_network",
        lambda _arguments, network_name: networks[network_name],
        raising=False,
    )
    return verifier._verify_container_topology(SimpleNamespace(project_name="campus-ai-internal"))


def test_manual_container_verifier_scopes_commands_to_explicit_disposable_project() -> None:
    verifier = _container_verifier()
    arguments = SimpleNamespace(
        project_name="sf5b-disposable-20260824",
        api_env=Path("api.env"),
        role_bootstrap_env=Path("bootstrap.env"),
        compose=Path("compose.yml"),
    )

    assert verifier._compose_prefix(arguments) == [
        "docker",
        "compose",
        "--project-name",
        "sf5b-disposable-20260824",
        "--env-file",
        "api.env",
        "--env-file",
        "bootstrap.env",
        "-f",
        "compose.yml",
    ]


def test_manual_container_verifier_decodes_subprocess_output_as_utf8(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    verifier = _container_verifier()
    observed: dict[str, object] = {}

    def fake_run(*_args: object, **kwargs: object) -> SimpleNamespace:
        observed.update(kwargs)
        return SimpleNamespace(returncode=0, stdout="虚构路径", stderr="")

    monkeypatch.setattr(verifier.subprocess, "run", fake_run)

    assert verifier._run(["docker", "inspect"], environment={}) == "虚构路径"
    assert observed["encoding"] == "utf-8"
    assert observed["errors"] == "strict"


def _environment(path: Path) -> dict[str, str]:
    assert path.is_file(), f"missing tracked environment example: {path.name}"
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        assert separator == "=", f"invalid environment line in {path.name}: {raw_line}"
        assert key and key not in values
        values[key] = value
    return values


def _compose() -> dict[str, object]:
    parsed = yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))
    assert isinstance(parsed, dict)
    return parsed


def test_sf5b_environment_examples_separate_runtime_writer_and_root_secrets() -> None:
    api_values = _environment(API_ENV_PATH)
    writer_values = _environment(WRITER_ENV_PATH)
    migration_values = _environment(MIGRATION_ENV_PATH)
    role_bootstrap_values = _environment(ROLE_BOOTSTRAP_ENV_PATH)
    api = set(api_values)
    writer = set(writer_values)
    migration = set(migration_values)
    role_bootstrap = set(role_bootstrap_values)

    assert {
        "POSTGRES_EXPECTED_ANCHOR_ID",
        "POSTGRES_EXPECTED_SCHOOL_ID",
        "POSTGRES_EXPECTED_SYSTEM_IDENTIFIER",
        "SCHOOL_FACT_EXECUTOR_BASE_URL",
        "SCHOOL_FACT_EXECUTOR_CALLER_AUTH_KEY",
        "SCHOOL_FACT_EXECUTOR_CONTEXT_TRANSPORT_HMAC_KEY",
    } <= api
    assert {
        "SCHOOL_FACT_WRITER_DATABASE_USER",
        "SCHOOL_FACT_WRITER_DATABASE_PASSWORD",
        "SCHOOL_FACT_WRITER_EXPECTED_ANCHOR_ID",
        "SCHOOL_FACT_WRITER_EXPECTED_SCHOOL_ID",
        "SCHOOL_FACT_WRITER_EXPECTED_SYSTEM_IDENTIFIER",
        "SCHOOL_FACT_WRITER_CONFIRMATION_HMAC_KEY",
        "SCHOOL_FACT_WRITER_CALLER_AUTH_KEY",
        "SCHOOL_FACT_WRITER_CONTEXT_TRANSPORT_HMAC_KEY",
        "SCHOOL_FACT_WRITER_RLS_CONTEXT_SIGNING_KEY",
    } <= writer
    assert {
        "POSTGRES_MIGRATION_USER",
        "POSTGRES_MIGRATION_PASSWORD",
        "POSTGRES_MIGRATION_CONTEXT_SIGNING_KEY",
    } <= migration
    assert {
        "POSTGRES_BOOTSTRAP_USER",
        "POSTGRES_BOOTSTRAP_PASSWORD",
        "POSTGRES_RUNTIME_PASSWORD",
        "POSTGRES_MIGRATION_PASSWORD",
        "POSTGRES_SCHOOL_FACT_WRITER_PASSWORD",
        "POSTGRES_SCHOOL_FACT_READER_PASSWORD",
    } <= role_bootstrap

    root_only = {
        "POSTGRES_BOOTSTRAP_USER",
        "POSTGRES_BOOTSTRAP_PASSWORD",
        "POSTGRES_RUNTIME_PASSWORD",
        "POSTGRES_MIGRATION_PASSWORD",
        "POSTGRES_MIGRATION_CONTEXT_SIGNING_KEY",
        "POSTGRES_SCHOOL_FACT_WRITER_PASSWORD",
        "POSTGRES_SCHOOL_FACT_READER_PASSWORD",
    }
    assert not api.intersection(root_only)
    assert not writer.intersection(root_only)
    assert "SCHOOL_FACT_WRITER_DATABASE_PASSWORD" not in api
    assert not migration.intersection(
        {
            "POSTGRES_BOOTSTRAP_USER",
            "POSTGRES_BOOTSTRAP_PASSWORD",
            "POSTGRES_SCHOOL_FACT_WRITER_PASSWORD",
            "SCHOOL_FACT_WRITER_DATABASE_PASSWORD",
        }
    )

    shared_transport = {
        "CALLER_AUTH_KEY",
        "CONTEXT_TRANSPORT_HMAC_KEY",
    }
    api_purposes = {
        key.removeprefix("SCHOOL_FACT_EXECUTOR_")
        for key in api
        if key.startswith("SCHOOL_FACT_EXECUTOR_")
    }
    writer_purposes = {
        key.removeprefix("SCHOOL_FACT_WRITER_")
        for key in writer
        if key.startswith("SCHOOL_FACT_WRITER_")
    }
    assert shared_transport <= api_purposes.intersection(writer_purposes)
    assert (
        api_values["SCHOOL_FACT_EXECUTOR_CALLER_AUTH_KEY"]
        == (writer_values["SCHOOL_FACT_WRITER_CALLER_AUTH_KEY"])
    )
    assert (
        api_values["SCHOOL_FACT_EXECUTOR_CONTEXT_TRANSPORT_HMAC_KEY"]
        == (writer_values["SCHOOL_FACT_WRITER_CONTEXT_TRANSPORT_HMAC_KEY"])
    )
    assert (
        api_values["POSTGRES_EXPECTED_ANCHOR_ID"]
        == (writer_values["SCHOOL_FACT_WRITER_EXPECTED_ANCHOR_ID"])
    )
    assert (
        api_values["POSTGRES_EXPECTED_SCHOOL_ID"]
        == (writer_values["SCHOOL_FACT_WRITER_EXPECTED_SCHOOL_ID"])
    )
    assert (
        api_values["POSTGRES_EXPECTED_SYSTEM_IDENTIFIER"]
        == (writer_values["SCHOOL_FACT_WRITER_EXPECTED_SYSTEM_IDENTIFIER"])
    )
    writer_hmac_values = {
        writer_values["SCHOOL_FACT_WRITER_CONFIRMATION_HMAC_KEY"],
        writer_values["SCHOOL_FACT_WRITER_CALLER_AUTH_KEY"],
        writer_values["SCHOOL_FACT_WRITER_CONTEXT_TRANSPORT_HMAC_KEY"],
        writer_values["SCHOOL_FACT_WRITER_RLS_CONTEXT_SIGNING_KEY"],
    }
    assert len(writer_hmac_values) == 4
    assert (
        api_values["RLS_CONTEXT_SIGNING_KEY"]
        == (writer_values["SCHOOL_FACT_WRITER_RLS_CONTEXT_SIGNING_KEY"])
    )
    assert (
        migration_values["POSTGRES_MIGRATION_CONTEXT_SIGNING_KEY"]
        == (writer_values["SCHOOL_FACT_WRITER_RLS_CONTEXT_SIGNING_KEY"])
    )

    writer_settings = SchoolFactWriterSettings.model_validate(
        {
            key.removeprefix("SCHOOL_FACT_WRITER_").lower(): value
            for key, value in writer_values.items()
        }
    )
    executor_settings = SchoolFactExecutorClientSettings.model_validate(
        {
            key.removeprefix("SCHOOL_FACT_EXECUTOR_").lower(): value
            for key, value in api_values.items()
            if key.startswith("SCHOOL_FACT_EXECUTOR_")
        }
    )
    migration_settings = MigrationSettings.model_validate(
        {
            key.removeprefix("POSTGRES_MIGRATION_").lower(): value
            for key, value in migration_values.items()
        }
    )
    assert writer_settings.database_user == "campus_ai_school_fact_writer"
    assert executor_settings.base_url == "http://school-fact-writer:8010"
    assert migration_settings.user == "campus_ai_migration"


def test_local_runtime_env_does_not_co_locate_database_control_secrets() -> None:
    runtime = set(_environment(ROOT_RUNTIME_ENV_PATH))
    database_control = set(_environment(LOCAL_DATABASE_CONTROL_ENV_PATH))

    privileged = {
        "POSTGRES_BOOTSTRAP_USER",
        "POSTGRES_BOOTSTRAP_PASSWORD",
        "POSTGRES_MIGRATION_USER",
        "POSTGRES_MIGRATION_PASSWORD",
        "POSTGRES_MIGRATION_CONTEXT_SIGNING_KEY",
        "POSTGRES_SCHOOL_FACT_WRITER_PASSWORD",
        "POSTGRES_SCHOOL_FACT_READER_PASSWORD",
    }
    assert not runtime.intersection(privileged)
    assert {
        "POSTGRES_BOOTSTRAP_USER",
        "POSTGRES_BOOTSTRAP_PASSWORD",
        "POSTGRES_RUNTIME_PASSWORD",
        "POSTGRES_MIGRATION_PASSWORD",
        "POSTGRES_SCHOOL_FACT_WRITER_PASSWORD",
        "POSTGRES_SCHOOL_FACT_READER_PASSWORD",
    } <= database_control


def test_internal_compose_orders_control_plane_before_hardened_writer() -> None:
    compose = _compose()
    services = compose["services"]
    networks = compose["networks"]
    assert isinstance(services, dict)
    assert isinstance(networks, dict)

    bootstrap = services["school-fact-role-bootstrap"]
    migration = services["school-fact-migration"]
    reconcile = services["school-fact-role-reconcile"]
    writer = services["school-fact-writer"]
    api = services["api"]
    postgres = services["postgres"]
    assert isinstance(bootstrap, dict)
    assert isinstance(migration, dict)
    assert isinstance(reconcile, dict)
    assert isinstance(writer, dict)
    assert isinstance(api, dict)
    assert isinstance(postgres, dict)
    assert postgres["environment"] == {
        "POSTGRES_DB": "${POSTGRES_DB}",
        "POSTGRES_USER": "${POSTGRES_BOOTSTRAP_USER}",
        "POSTGRES_PASSWORD": "${POSTGRES_BOOTSTRAP_PASSWORD}",
    }
    postgres_health = postgres["healthcheck"]
    assert isinstance(postgres_health, dict)
    assert "$${POSTGRES_USER}" in postgres_health["test"][1]
    assert "$${POSTGRES_BOOTSTRAP_USER}" not in postgres_health["test"][1]

    assert bootstrap["depends_on"] == {"postgres": {"condition": "service_healthy"}}
    assert migration["depends_on"] == {
        "school-fact-role-bootstrap": {"condition": "service_completed_successfully"}
    }
    assert reconcile["depends_on"] == {
        "school-fact-migration": {"condition": "service_completed_successfully"}
    }
    assert writer["depends_on"] == {
        "school-fact-role-reconcile": {"condition": "service_completed_successfully"}
    }
    assert migration["command"] == ["python", "-m", "alembic", "upgrade", "head"]
    assert bootstrap["command"] == ["/bin/sh", "/bootstrap/001-campus-ai-roles.sh"]
    assert reconcile["command"] == bootstrap["command"]

    assert writer["user"] == "10002:10002"
    assert writer["command"][:3] == [
        "uvicorn",
        "app.composition.school_fact_writer_executor:production_school_fact_writer_executor_app",
        "--factory",
    ]
    assert "ports" not in writer
    assert writer["env_file"] == [
        "${CAMPUS_AI_SCHOOL_FACT_WRITER_ENV_FILE:-../../.env.school-fact-writer}"
    ]
    assert api["env_file"] == ["${CAMPUS_AI_INTERNAL_ENV_FILE:-../../.env.internal}"]
    assert migration["env_file"] == [
        "${CAMPUS_AI_SCHOOL_FACT_MIGRATION_ENV_FILE:-../../.env.school-fact-migration}"
    ]
    expected_bootstrap_env = [
        "${CAMPUS_AI_SCHOOL_FACT_ROLE_BOOTSTRAP_ENV_FILE:-../../.env.school-fact-role-bootstrap}"
    ]
    assert bootstrap["env_file"] == expected_bootstrap_env
    assert reconcile["env_file"] == expected_bootstrap_env

    assert writer["networks"] == ["school-fact-database", "school-fact-writer-ipc"]
    assert api["networks"] == ["private", "school-fact-writer-ipc"]
    assert networks["school-fact-database"] == {"internal": True}
    assert networks["school-fact-writer-ipc"] == {"internal": True}
    assert {
        name
        for name, service in services.items()
        if "school-fact-writer-ipc" in service.get("networks", [])
    } == {"api", "school-fact-writer"}
    assert {
        name
        for name, service in services.items()
        if "school-fact-database" in service.get("networks", [])
    } == {
        "postgres",
        "school-fact-role-bootstrap",
        "school-fact-migration",
        "school-fact-role-reconcile",
        "school-fact-writer",
    }


def test_manual_container_verifier_never_directly_provisions_school_or_anchor() -> None:
    source = CONTAINER_VERIFIER_PATH.read_text(encoding="utf-8")

    assert "production_school_fact_writer_executor_app" in source
    assert "10001:10001" in source
    assert "10002:10002" in source
    assert "X-School-Fact-Executor-Key" in source
    assert "/internal/v1/school-facts/preflight" in source
    assert "/internal/v1/school-facts/prepare" in source
    assert "/internal/v1/school-facts/confirm" in source
    assert "/internal/v1/school-facts/result" in source
    assert "school-fact-writer" in source and "restart" in source
    assert "SCHOOL_FACT_WRITER_DATABASE_PASSWORD" in source
    assert "POSTGRES_MIGRATION_" in source
    assert "INSERT INTO" not in source.upper()
    assert "alembic upgrade" not in source
    provisioning = source[
        source.index("def _provision_control_plane(") : source.index("def verify(")
    ]
    assert (
        provisioning.index('"school-fact-role-bootstrap"')
        < provisioning.index('"school-fact-migration"')
        < provisioning.index('"school-fact-role-reconcile"')
        < provisioning.index("WRITER_SERVICE, API_SERVICE")
    )


def test_manual_container_verifier_binds_result_lookup_to_trusted_context() -> None:
    verifier = _container_verifier()
    program = verifier._CONTAINER_IPC_PROGRAM
    result_request_start = program.index("        result_request = {")
    result_post_start = program.index(
        "        result = client.post(RESULT, headers=headers, json=result_request)",
        result_request_start,
    )
    result_request = program[result_request_start:result_post_start]

    assert '"context_token": prepare_request["context_token"]' in result_request
    assert '"session_binding": prepare_request["session_binding"]' in result_request


def test_manual_container_verifier_binds_preflight_to_trusted_context() -> None:
    verifier = _container_verifier()
    program = verifier._CONTAINER_IPC_PROGRAM
    preflight_request_start = program.index("        preflight_request = {")
    unauthorized_start = program.index(
        "        unauthorized = client.post(PREFLIGHT, json=preflight_request)",
        preflight_request_start,
    )
    preflight_request = program[preflight_request_start:unauthorized_start]

    assert '"context_token": prepare_request["context_token"]' in preflight_request
    assert '"session_binding": prepare_request["session_binding"]' in preflight_request


def test_manual_container_verifier_accepts_only_the_frozen_runtime_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    verifier = _container_verifier()
    writer, api = _secure_runtime_inspections(verifier)

    writer_names, api_names = _verify_synthetic_runtime(
        monkeypatch,
        verifier,
        writer=writer,
        api=api,
        networks=_secure_runtime_networks(),
    )

    assert verifier._WRITER_REQUIRED_ENV <= writer_names
    assert verifier._API_REQUIRED_ENV <= api_names


@pytest.mark.parametrize(
    ("invalid_case", "error_fragment"),
    (
        ("writer_extra_network", "Writer Docker networks"),
        ("api_extra_network", "API Docker networks"),
        ("external_network", "must be internal"),
        ("writer_writable_root", "read-only root filesystem"),
        ("writer_capability", "drop all Linux capabilities"),
        ("writer_privilege_escalation", "no-new-privileges"),
        ("writer_executable_tmp", "noexec and nosuid"),
        ("writer_mount", "Writer has unexpected mounts"),
        ("api_writer_secret_mount", "API has unexpected mounts"),
        ("writer_host_port", "Writer has host port bindings"),
        ("api_reader_db_secret_env", "API contains forbidden environment names"),
        ("api_reader_settings_secret_env", "API contains forbidden environment names"),
    ),
)
def test_manual_container_verifier_rejects_runtime_topology_drift(
    monkeypatch: pytest.MonkeyPatch,
    invalid_case: str,
    error_fragment: str,
) -> None:
    verifier = _container_verifier()
    writer, api = _secure_runtime_inspections(verifier)
    networks = _secure_runtime_networks()

    if invalid_case == "writer_extra_network":
        writer["NetworkSettings"]["Networks"]["rogue"] = {"NetworkID": "rogue-id"}
        networks["rogue"] = {
            "Name": "rogue",
            "Id": "rogue-id",
            "Internal": True,
            "Labels": {
                "com.docker.compose.project": "campus-ai-internal",
                "com.docker.compose.network": "rogue",
            },
        }
    elif invalid_case == "api_extra_network":
        api["NetworkSettings"]["Networks"]["rogue"] = {"NetworkID": "rogue-id"}
        networks["rogue"] = {
            "Name": "rogue",
            "Id": "rogue-id",
            "Internal": True,
            "Labels": {
                "com.docker.compose.project": "campus-ai-internal",
                "com.docker.compose.network": "rogue",
            },
        }
    elif invalid_case == "external_network":
        networks["campus-ai-internal_school-fact-writer-ipc"]["Internal"] = False
    elif invalid_case == "writer_writable_root":
        writer["HostConfig"]["ReadonlyRootfs"] = False
    elif invalid_case == "writer_capability":
        writer["HostConfig"]["CapDrop"] = []
    elif invalid_case == "writer_privilege_escalation":
        writer["HostConfig"]["SecurityOpt"] = []
    elif invalid_case == "writer_executable_tmp":
        writer["HostConfig"]["Tmpfs"] = {"/tmp": "rw,size=16777216"}
    elif invalid_case == "writer_mount":
        writer["Mounts"] = [{"Type": "bind", "Destination": "/workspace"}]
    elif invalid_case == "api_writer_secret_mount":
        api["Mounts"] = [{"Type": "bind", "Destination": "/run/secrets/writer"}]
    elif invalid_case == "writer_host_port":
        writer["HostConfig"]["PortBindings"] = {"8010/tcp": [{"HostPort": "8010"}]}
    elif invalid_case == "api_reader_db_secret_env":
        api["Config"]["Env"].append("POSTGRES_SCHOOL_FACT_READER_PASSWORD=private")
    elif invalid_case == "api_reader_settings_secret_env":
        api["Config"]["Env"].append("SCHOOL_FACT_READER_DATABASE_PASSWORD=private")
    else:
        raise AssertionError(f"unhandled test case: {invalid_case}")

    with pytest.raises(verifier.VerificationFailed, match=error_fragment):
        _verify_synthetic_runtime(
            monkeypatch,
            verifier,
            writer=writer,
            api=api,
            networks=networks,
        )
