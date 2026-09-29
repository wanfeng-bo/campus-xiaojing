from dataclasses import replace
from typing import cast
from unittest.mock import MagicMock
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from pydantic_settings import SettingsConfigDict
from sqlalchemy.pool import QueuePool

from app.core.settings import PROJECT_ROOT, Settings, get_settings
from app.db import connection as database_connection
from app.db.connection import (
    EXPECTED_RUNTIME_DATABASE_ROLE,
    REQUIRED_RUNTIME_ALEMBIC_HEAD,
    RuntimeDatabaseConnectionFacts,
    RuntimeDatabaseStartupRejected,
    build_engine,
    require_runtime_database_connection,
)

BASE_SETTINGS = {
    "app_env": "test",
    "postgres_host": "127.0.0.1",
    "postgres_port": 5432,
    "postgres_db": "campus_ai_test",
    "postgres_user": "tester",
    "postgres_password": "not-a-real-password",
    "postgres_expected_anchor_id": "10000000-0000-4000-8000-000000000001",
    "postgres_expected_school_id": "10000000-0000-4000-8000-000000000002",
    "postgres_expected_system_identifier": "1234567890123456789",
    "rls_context_signing_key": "test-runtime-context-signing-key-32-bytes",
    "redis_host": "127.0.0.1",
    "redis_port": 6379,
    "redis_password": "not-a-real-redis-password",
}


class SettingsWithoutEnv(Settings):
    model_config = SettingsConfigDict(env_file=None, hide_input_in_errors=True)


def test_settings_builds_postgresql_url_from_required_values() -> None:
    settings = Settings.model_validate(BASE_SETTINGS)

    assert settings.database_url.drivername == "postgresql+psycopg"
    assert settings.database_url.host == "127.0.0.1"
    assert settings.database_url.port == 5432
    assert settings.database_url.database == "campus_ai_test"
    assert settings.database_url.username == "tester"


def test_runtime_database_pool_defaults_are_bounded_and_documented() -> None:
    settings = SettingsWithoutEnv.model_validate(BASE_SETTINGS)

    assert settings.postgres_pool_size == 4
    assert settings.postgres_max_overflow == 0
    assert settings.postgres_pool_timeout_seconds == 3
    assert settings.postgres_pool_recycle_seconds == 600

    for example_name in (".env.example", ".env.internal.example"):
        environment = (PROJECT_ROOT / example_name).read_text(encoding="utf-8")
        assert "POSTGRES_POOL_SIZE=4" in environment
        assert "POSTGRES_MAX_OVERFLOW=0" in environment
        assert "POSTGRES_POOL_TIMEOUT_SECONDS=3" in environment
        assert "POSTGRES_POOL_RECYCLE_SECONDS=600" in environment


@pytest.mark.parametrize(
    ("setting_name", "invalid_value"),
    [
        pytest.param("postgres_pool_size", 0, id="pool-size-below-bound"),
        pytest.param("postgres_pool_size", 17, id="pool-size-above-bound"),
        pytest.param("postgres_max_overflow", -1, id="overflow-below-bound"),
        pytest.param("postgres_max_overflow", 9, id="overflow-above-bound"),
        pytest.param("postgres_pool_timeout_seconds", 0, id="timeout-below-bound"),
        pytest.param("postgres_pool_timeout_seconds", 10.1, id="timeout-above-bound"),
        pytest.param("postgres_pool_timeout_seconds", float("nan"), id="timeout-not-a-number"),
        pytest.param("postgres_pool_recycle_seconds", 29, id="recycle-below-bound"),
        pytest.param("postgres_pool_recycle_seconds", 1801, id="recycle-above-bound"),
    ],
)
def test_runtime_database_pool_rejects_out_of_budget_values(
    setting_name: str,
    invalid_value: float,
) -> None:
    with pytest.raises(ValidationError):
        SettingsWithoutEnv.model_validate({**BASE_SETTINGS, setting_name: invalid_value})


def test_runtime_engine_uses_bounded_rollback_reset_pool() -> None:
    settings = SettingsWithoutEnv.model_validate(
        {
            **BASE_SETTINGS,
            "postgres_pool_size": 3,
            "postgres_max_overflow": 1,
            "postgres_pool_timeout_seconds": 2,
            "postgres_pool_recycle_seconds": 300,
        }
    )

    engine = build_engine(settings)
    try:
        pool = cast(QueuePool, engine.pool)
        assert pool.size() == 3  # pyright: ignore[reportUnknownMemberType]
        assert pool._max_overflow == 1  # pyright: ignore[reportPrivateUsage]
        assert pool._timeout == 2  # pyright: ignore[reportPrivateUsage]
        assert pool._recycle == 300  # pyright: ignore[reportPrivateUsage]
        assert pool._pre_ping is True  # pyright: ignore[reportPrivateUsage]
        assert str(pool._reset_on_return).endswith(  # pyright: ignore[reportPrivateUsage]
            "reset_rollback"
        )
        checkout_listeners = list(pool.dispatch.checkout)
        assert len(checkout_listeners) == 1
        assert checkout_listeners[0].__qualname__.startswith("_runtime_checkout_listener.")

    finally:
        engine.dispose()


def test_runtime_database_gate_rejects_wrong_role_head_anchor_or_dirty_context() -> None:
    settings = SettingsWithoutEnv.model_validate(
        {**BASE_SETTINGS, "postgres_user": EXPECTED_RUNTIME_DATABASE_ROLE}
    )
    expected_school_id = cast(UUID, settings.postgres_expected_school_id)
    good = RuntimeDatabaseConnectionFacts(
        current_user=EXPECTED_RUNTIME_DATABASE_ROLE,
        session_user=EXPECTED_RUNTIME_DATABASE_ROLE,
        is_superuser=False,
        bypasses_rls=False,
        has_privileged_membership=False,
        alembic_head=REQUIRED_RUNTIME_ALEMBIC_HEAD,
        anchored_school_id=expected_school_id,
        execution_context_is_clean=True,
    )
    require_runtime_database_connection(good, settings=settings)

    invalid = (
        replace(good, current_user="campus_ai_school_fact_writer"),
        replace(good, session_user="campus_ai_migration"),
        replace(good, is_superuser=True),
        replace(good, bypasses_rls=True),
        replace(good, has_privileged_membership=True),
        replace(good, alembic_head="20260823_19"),
        replace(good, anchored_school_id=uuid4()),
        replace(good, execution_context_is_clean=False),
    )
    for facts in invalid:
        with pytest.raises(RuntimeDatabaseStartupRejected):
            require_runtime_database_connection(facts, settings=settings)

    wrong_user_settings = SettingsWithoutEnv.model_validate(
        {**BASE_SETTINGS, "postgres_user": "campus_ai_school_fact_writer"}
    )
    with pytest.raises(RuntimeDatabaseStartupRejected):
        require_runtime_database_connection(good, settings=wrong_user_settings)


def test_runtime_checkout_membership_query_covers_all_privileged_roles_transitively() -> None:
    expected_privileged_roles = frozenset(
        {
            "campus_ai_db_owner",
            "campus_ai_migration",
            "campus_ai_context_definer",
            "campus_ai_audit_definer",
            "campus_ai_outbox_probe_definer",
            "campus_ai_takeover_definer",
            "campus_ai_school_fact_writer",
            "campus_ai_school_fact_reader",
            "campus_ai_identity_definer",
        }
    )

    assert (
        frozenset(database_connection._PRIVILEGED_RUNTIME_MEMBERSHIP_ROLES)
        == expected_privileged_roles
    )
    assert (
        "pg_has_role(current_user, granted_role.oid, 'MEMBER')"
        in database_connection._RUNTIME_CHECKOUT_FACTS_SQL
    )
    for role_name in expected_privileged_roles:
        assert f"'{role_name}'" in database_connection._RUNTIME_CHECKOUT_FACTS_SQL


def test_runtime_checkout_reads_full_expected_anchor_and_exact_clean_context() -> None:
    settings = SettingsWithoutEnv.model_validate(
        {**BASE_SETTINGS, "postgres_user": EXPECTED_RUNTIME_DATABASE_ROLE}
    )
    expected_school_id = cast(UUID, settings.postgres_expected_school_id)
    connection = MagicMock()
    cursor = connection.cursor.return_value
    cursor.fetchone.return_value = (
        EXPECTED_RUNTIME_DATABASE_ROLE,
        EXPECTED_RUNTIME_DATABASE_ROLE,
        False,
        False,
        False,
        REQUIRED_RUNTIME_ALEMBIC_HEAD,
        str(expected_school_id),
        True,
    )

    facts = database_connection._read_runtime_checkout_facts(connection, settings=settings)

    statement, parameters = cursor.execute.call_args.args
    expected_anchor_id = str(settings.postgres_expected_anchor_id)
    assert parameters == (
        expected_anchor_id,
        expected_anchor_id,
        str(expected_school_id),
        settings.postgres_expected_system_identifier,
    )
    assert "app.assert_school_database_anchor(%s::uuid, %s::uuid, %s::text)" in statement
    assert facts.anchored_school_id == expected_school_id
    assert facts.execution_context_is_clean is True
    cursor.close.assert_called_once_with()

    expected_context_settings = (
        "app.context_key_version",
        "app.school_id",
        "app.account_user_id",
        "app.person_id",
        "app.context_kind",
        "app.assignment_id",
        "app.assignment_version",
        "app.guardian_relationship_id",
        "app.guardian_relationship_version",
        "app.authorization_version",
        "app.request_id",
        "app.context_purpose",
        "app.idempotency_key_hash",
        "app.session_nonce_hash",
        "app.proof_expires_at",
        "app.server_proof",
        "app.binding_database",
        "app.binding_backend_pid",
        "app.binding_xact_id",
        "app.binding_proof",
    )
    assert database_connection._RUNTIME_CONTEXT_SETTING_NAMES == expected_context_settings
    for setting_name in expected_context_settings:
        assert f"COALESCE(current_setting('{setting_name}', true), '') = ''" in statement


def test_runtime_checkout_rejection_hard_invalidates_connection() -> None:
    settings = SettingsWithoutEnv.model_validate(
        {**BASE_SETTINGS, "postgres_user": EXPECTED_RUNTIME_DATABASE_ROLE}
    )
    connection = MagicMock()
    cursor = connection.cursor.return_value
    cursor.fetchone.return_value = (
        "campus_ai_school_fact_writer",
        EXPECTED_RUNTIME_DATABASE_ROLE,
        False,
        False,
        False,
        REQUIRED_RUNTIME_ALEMBIC_HEAD,
        str(settings.postgres_expected_school_id),
        True,
    )
    connection_record = MagicMock()
    listener = database_connection._runtime_checkout_listener(settings)

    with pytest.raises(RuntimeDatabaseStartupRejected) as rejected:
        listener(connection, connection_record, MagicMock())

    connection.rollback.assert_called_once_with()
    connection_record.invalidate.assert_called_once_with(rejected.value, soft=False)


@pytest.mark.parametrize(
    "anchor_overrides",
    [
        pytest.param({"postgres_expected_anchor_id": None}, id="missing-anchor-id"),
        pytest.param({"postgres_expected_school_id": None}, id="missing-school-id"),
        pytest.param(
            {"postgres_expected_system_identifier": None},
            id="missing-system-identifier",
        ),
    ],
)
def test_runtime_rejects_partial_anchor_configuration(
    anchor_overrides: dict[str, object | None],
) -> None:
    with pytest.raises(ValidationError, match="anchor configuration must be complete"):
        SettingsWithoutEnv.model_validate({**BASE_SETTINGS, **anchor_overrides})


@pytest.mark.parametrize(
    "invalid_identifier",
    ["123456789", "not-decimal", "１２３４５６７８９０"],
)
def test_runtime_rejects_invalid_database_system_identifier(
    invalid_identifier: str,
) -> None:
    with pytest.raises(ValidationError, match="system identifier is invalid"):
        SettingsWithoutEnv.model_validate(
            {**BASE_SETTINGS, "postgres_expected_system_identifier": invalid_identifier}
        )


def test_production_runtime_rejects_missing_anchor_configuration() -> None:
    with pytest.raises(ValidationError):
        SettingsWithoutEnv.model_validate(
            {
                **BASE_SETTINGS,
                "app_env": "production",
                "postgres_user": EXPECTED_RUNTIME_DATABASE_ROLE,
                "postgres_expected_anchor_id": None,
                "postgres_expected_school_id": None,
                "postgres_expected_system_identifier": None,
            }
        )


def test_runtime_context_signing_key_is_required_and_never_echoed(monkeypatch) -> None:
    monkeypatch.delenv("RLS_CONTEXT_SIGNING_KEY", raising=False)
    missing = {
        key: value for key, value in BASE_SETTINGS.items() if key != "rls_context_signing_key"
    }
    with pytest.raises(ValidationError) as missing_error:
        SettingsWithoutEnv.model_validate(missing)
    assert "rls_context_signing_key" in str(missing_error.value)

    with pytest.raises(ValidationError) as short_error:
        SettingsWithoutEnv.model_validate(
            {**BASE_SETTINGS, "rls_context_signing_key": "too-short-secret"}
        )
    assert "至少包含 32 个字节" in str(short_error.value)
    assert "too-short-secret" not in str(short_error.value)


def test_production_rejects_local_test_identity_provider() -> None:
    with pytest.raises(ValidationError, match="生产环境只能使用钉钉身份提供者"):
        Settings.model_validate(
            {
                **BASE_SETTINGS,
                "app_env": "production",
                "identity_provider": "local_test",
            }
        )


def test_dingtalk_identity_provider_requires_application_credentials() -> None:
    with pytest.raises(ValidationError, match="钉钉身份提供者缺少配置"):
        Settings.model_validate({**BASE_SETTINGS, "identity_provider": "dingtalk"})


def test_settings_builds_percent_encoded_redis_url() -> None:
    settings = Settings.model_validate({**BASE_SETTINGS, "redis_password": "a/b@c"})

    assert settings.redis_url == "redis://:a%2Fb%40c@127.0.0.1:6379/0"


def test_settings_default_redis_io_timeouts_are_half_a_second() -> None:
    settings = Settings.model_validate(BASE_SETTINGS)

    assert settings.redis_connect_timeout_seconds == 0.5
    assert settings.redis_read_timeout_seconds == 0.5


def test_repair_draft_model_is_disabled_without_credentials_by_default() -> None:
    settings = SettingsWithoutEnv.model_validate(BASE_SETTINGS)

    assert settings.repair_draft_model_enabled is False
    assert settings.repair_draft_model_service_name == "DeepSeek"
    assert settings.repair_draft_model_base_url is None
    assert settings.repair_draft_model_model is None
    assert settings.repair_draft_model_api_key is None
    assert settings.repair_draft_model_timeout_seconds == 10.0


def test_enabled_repair_draft_model_requires_complete_config_without_echoing_key() -> None:
    with pytest.raises(ValidationError, match="报修文字整理模型缺少配置") as missing_error:
        SettingsWithoutEnv.model_validate(
            {
                **BASE_SETTINGS,
                "repair_draft_model_enabled": True,
                "repair_draft_model_api_key": "synthetic-provider-secret",
            }
        )

    assert "synthetic-provider-secret" not in str(missing_error.value)


def test_enabled_repair_draft_model_accepts_configurable_endpoint_and_model() -> None:
    settings = SettingsWithoutEnv.model_validate(
        {
            **BASE_SETTINGS,
            "repair_draft_model_enabled": True,
            "repair_draft_model_service_name": "学校选择的模型",
            "repair_draft_model_base_url": "  https://model.example/v4/  ",
            "repair_draft_model_model": "current-json-model",
            "repair_draft_model_api_key": "synthetic-provider-secret",
            "repair_draft_model_timeout_seconds": 30,
        }
    )

    assert settings.repair_draft_model_service_name == "学校选择的模型"
    assert settings.repair_draft_model_base_url == "https://model.example/v4/"
    assert settings.repair_draft_model_model == "current-json-model"
    assert settings.repair_draft_model_api_key is not None
    assert settings.repair_draft_model_api_key.get_secret_value() == "synthetic-provider-secret"
    assert settings.repair_draft_model_timeout_seconds == 30


@pytest.mark.parametrize(
    "invalid_base_url",
    [
        "model.example/v4",
        "ftp://model.example/v4",
        "https:///v4",
        "https://user:password@model.example/v4",
        "https://model.example/v4?tenant=school",
        "https://model.example/v4#fragment",
        "https://model.example:not-a-port/v4",
    ],
)
def test_enabled_repair_draft_model_rejects_ambiguous_or_invalid_base_url_safely(
    invalid_base_url: str,
) -> None:
    api_key = "synthetic-provider-secret-marker"

    with pytest.raises(ValidationError, match="REPAIR_DRAFT_MODEL_BASE_URL") as error:
        SettingsWithoutEnv.model_validate(
            {
                **BASE_SETTINGS,
                "repair_draft_model_enabled": True,
                "repair_draft_model_service_name": "学校选择的模型",
                "repair_draft_model_base_url": invalid_base_url,
                "repair_draft_model_model": "current-json-model",
                "repair_draft_model_api_key": api_key,
            }
        )

    assert api_key not in str(error.value)


@pytest.mark.parametrize("invalid_timeout", [0, -1, 30.001, float("nan"), float("inf")])
def test_repair_draft_model_timeout_is_positive_finite_and_at_most_thirty_seconds(
    invalid_timeout: float,
) -> None:
    with pytest.raises(ValidationError):
        SettingsWithoutEnv.model_validate(
            {
                **BASE_SETTINGS,
                "repair_draft_model_timeout_seconds": invalid_timeout,
            }
        )


@pytest.mark.parametrize(
    ("setting_name", "invalid_value"),
    [
        ("redis_connect_timeout_seconds", 0),
        ("redis_connect_timeout_seconds", -0.1),
        ("redis_connect_timeout_seconds", float("nan")),
        ("redis_connect_timeout_seconds", float("inf")),
        ("redis_connect_timeout_seconds", 0.500_001),
        ("redis_read_timeout_seconds", 0),
        ("redis_read_timeout_seconds", -0.1),
        ("redis_read_timeout_seconds", float("nan")),
        ("redis_read_timeout_seconds", float("inf")),
        ("redis_read_timeout_seconds", 0.500_001),
    ],
)
def test_settings_rejects_unbounded_redis_timeouts(
    setting_name: str,
    invalid_value: float,
) -> None:
    with pytest.raises(ValidationError):
        Settings.model_validate({**BASE_SETTINGS, setting_name: invalid_value})


@pytest.mark.parametrize(
    "example_name",
    [".env.example", ".env.internal.example", ".env.dingtalk-test.example"],
)
def test_redis_timeout_defaults_are_documented_in_api_environment_samples(
    example_name: str,
) -> None:
    environment = (PROJECT_ROOT / example_name).read_text(encoding="utf-8")

    assert "REDIS_CONNECT_TIMEOUT_SECONDS=0.5" in environment
    assert "REDIS_READ_TIMEOUT_SECONDS=0.5" in environment


@pytest.mark.parametrize(
    "example_name",
    [".env.example", ".env.internal.example", ".env.dingtalk-test.example"],
)
def test_repair_draft_model_boundary_is_documented_in_environment_samples(
    example_name: str,
) -> None:
    environment = (PROJECT_ROOT / example_name).read_text(encoding="utf-8")

    assert "REPAIR_DRAFT_MODEL_ENABLED=false" in environment
    assert "REPAIR_DRAFT_MODEL_SERVICE_NAME=DeepSeek" in environment
    assert "REPAIR_DRAFT_MODEL_BASE_URL=" in environment
    assert "REPAIR_DRAFT_MODEL_MODEL=" in environment
    assert "REPAIR_DRAFT_MODEL_API_KEY=" in environment
    assert "REPAIR_DRAFT_MODEL_TIMEOUT_SECONDS=10" in environment


def test_settings_rejects_missing_database_host_without_echoing_password(monkeypatch) -> None:
    """确认外部环境变量不会掩盖缺失主机配置，也不会在错误中泄露密码。"""

    monkeypatch.delenv("POSTGRES_HOST", raising=False)

    with pytest.raises(ValidationError) as error:
        SettingsWithoutEnv(  # pyright: ignore[reportCallIssue]  # 此测试刻意省略必填主机。
            app_env="test",
            postgres_port=5432,
            postgres_db="campus_ai_test",
            postgres_user="tester",
            postgres_password="not-a-real-password",
            rls_context_signing_key="test-runtime-context-signing-key-32-bytes",
        )

    assert "postgres_host" in str(error.value)
    assert "not-a-real-password" not in str(error.value)


def test_get_settings_reads_the_project_root_env_file(monkeypatch) -> None:
    for variable_name in (
        "POSTGRES_HOST",
        "POSTGRES_PORT",
        "POSTGRES_DB",
        "POSTGRES_USER",
        "POSTGRES_PASSWORD",
        "RLS_CONTEXT_SIGNING_KEY",
    ):
        monkeypatch.delenv(variable_name, raising=False)

    monkeypatch.setenv(
        "RLS_CONTEXT_SIGNING_KEY",
        "test-runtime-context-signing-key-32-bytes",
    )

    get_settings.cache_clear()
    try:
        assert get_settings().database_url.host == "127.0.0.1"
    finally:
        get_settings.cache_clear()
