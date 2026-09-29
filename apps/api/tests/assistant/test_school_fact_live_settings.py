"""Formal configuration must never inherit synthetic authority or spend."""

from __future__ import annotations

import asyncio
import copy
import json
import sqlite3
from datetime import UTC, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.composition.school_fact_live_settings import (
    LiveSchoolFactProductConfiguration,
    build_live_model_call_permit,
)
from app.composition.school_fact_product_settings import SchoolFactProductConfiguration
from tests.school_facts.test_f7_startup_contract import f7_document


def live_document(tmp_path):
    data = f7_document(tmp_path, contract="school-facts-f7-v56")
    data["purpose"] = "school-facts-live"
    data["runtime"].update(
        app_env="production",
        identity_provider="dingtalk",
        dingtalk_corp_id="approved-corp",
        dingtalk_app_key="approved-app",
        dingtalk_app_secret="dingtalk-secret-for-test-only-1234567890",
        identity_session_cookie_name="school_fact_live_session",
    )
    data["target_corp_id"] = "approved-corp"
    data["target_school_id"] = data["runtime"]["postgres_expected_school_id"]
    data["family_leadership"] = {
        "school_id": data["target_school_id"],
        "publication_reference": "approved-school-leadership",
        "version": 1,
        "school_role_codes": ["principal"],
        "campus_role_codes": [],
    }
    data["allowed_origin"] = "https://school.example.test"
    data["trusted_proxy_cidrs"] = ["10.11.12.13/32"]
    data["model"].pop("stage_budget")
    data["model"]["budget"]["cap_fen"] = 10000
    data["model"]["budget"]["approval_reference"] = "approved-live-100-yuan"
    data["model"].update(
        ledger_purpose="school-facts-live",
        budget_window_start="2026-09-27T00:00:00+08:00",
        budget_window_end="2026-10-27T00:00:00+08:00",
    )
    data["model_data_policy"] = {
        "approval_reference": "approved-live-data",
        "operator_notice_reference": "school-notice-1",
        "supplier_policy_reference": "supplier-contract-1",
        "projection_version": "school-facts-live-v1",
        "allow_normal_names": True,
        "chat_retention_hours": 24,
    }
    return data


def direct_only_live_document(tmp_path):
    data = live_document(tmp_path)
    data["assistant_enabled"] = False
    data.pop("model")
    data.pop("model_data_policy")
    return data


def test_formal_direct_mode_needs_explicit_disable_and_no_model_approvals(tmp_path):
    data = direct_only_live_document(tmp_path)
    config = LiveSchoolFactProductConfiguration.model_validate_json(json.dumps(data))
    assert config.assistant_enabled is False
    assert config.model is None
    assert config.model_data_policy is None
    assert config.runtime.app_env == "production"
    assert config.runtime.identity_provider == "dingtalk"
    data.pop("assistant_enabled")
    with pytest.raises(ValidationError):
        LiveSchoolFactProductConfiguration.model_validate_json(json.dumps(data))


@pytest.mark.parametrize("missing", ["model", "model_data_policy"])
def test_enabled_formal_assistant_still_requires_every_model_approval(tmp_path, missing):
    data = live_document(tmp_path)
    data["assistant_enabled"] = True
    data.pop(missing)
    with pytest.raises(ValidationError):
        LiveSchoolFactProductConfiguration.model_validate_json(json.dumps(data))


@pytest.mark.parametrize("retained", ["model", "model_data_policy"])
def test_disabled_formal_assistant_rejects_leftover_model_configuration(tmp_path, retained):
    data = direct_only_live_document(tmp_path)
    data[retained] = live_document(tmp_path)[retained]
    with pytest.raises(ValidationError):
        LiveSchoolFactProductConfiguration.model_validate_json(json.dumps(data))


@pytest.mark.parametrize("value", ["false", "true", 0, 1, None])
def test_formal_assistant_switch_requires_an_actual_boolean(tmp_path, value):
    data = direct_only_live_document(tmp_path)
    data["assistant_enabled"] = value
    with pytest.raises(ValidationError):
        LiveSchoolFactProductConfiguration.model_validate_json(json.dumps(data))


@pytest.mark.parametrize(
    "path,value",
    [
        (("runtime", "app_env"), "test"),
        (("runtime", "identity_provider"), "local_test"),
        (("runtime", "dingtalk_corp_id"), "other-corp"),
        (("runtime", "identity_session_cookie_name"), "campus_ai_session"),
        (("reader", "schema_contract"), "school-facts-f7-v55"),
        (("allowed_origin",), "http://school.example.test"),
        (("family_leadership",), None),
        (("residence", "dependency_education"), None),
    ],
)
def test_disabled_formal_assistant_keeps_non_model_requirements(tmp_path, path, value):
    data = direct_only_live_document(tmp_path)
    node = data
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    with pytest.raises(ValidationError):
        LiveSchoolFactProductConfiguration.model_validate_json(json.dumps(data))


def test_integration_synthetic_has_own_six_yuan_stage_on_original_ledger(tmp_path):
    data = f7_document(tmp_path, contract="school-facts-f7-v56")
    data["purpose"] = "school-facts-live-integration-synthetic"
    data["model"]["stage_budget"] = {
        "stage_id": "school-facts-live-integration",
        "approval_reference": "approved-live-integration-six-yuan",
        "cap_microyuan": 6_000_000,
    }
    config = SchoolFactProductConfiguration.model_validate_json(json.dumps(data))
    assert config.model.budget.cap_fen == 5000
    assert config.model.stage_budget.stage_id == "school-facts-live-integration"
    assert config.model.stage_budget.cap_microyuan == 6_000_000
    for path, value in [
        (("model", "stage_budget", "cap_microyuan"), 6_000_001),
        (("model", "stage_budget", "stage_id"), "school-facts-f7"),
        (("runtime", "postgres_schema_contract"), "school-facts-f7-v55"),
        (("reader", "schema_contract"), "school-facts-f7-v55"),
    ]:
        wrong = copy.deepcopy(data)
        node = wrong
        for key in path[:-1]:
            node = node[key]
        node[path[-1]] = value
        with pytest.raises(ValidationError):
            SchoolFactProductConfiguration.model_validate_json(json.dumps(wrong))


def test_formal_configuration_has_separate_purpose_and_complete_dependencies(tmp_path):
    data = live_document(tmp_path)
    config = LiveSchoolFactProductConfiguration.model_validate_json(json.dumps(data))
    assert config.assistant_enabled is True
    assert config.runtime.app_env == "production"
    assert config.runtime.postgres_schema_contract == "school-facts-f7-v56"
    assert config.model is not None
    assert config.model.budget.cap_fen == 10000
    with pytest.raises(ValidationError):
        SchoolFactProductConfiguration.model_validate_json(json.dumps(data))


@pytest.mark.parametrize(
    "path,value",
    [
        (("purpose",), "school-facts-f7-synthetic"),
        (("runtime", "app_env"), "test"),
        (("runtime", "identity_provider"), "local_test"),
        (("runtime", "postgres_schema_contract"), "school-facts-f7-v55"),
        (("reader", "schema_contract"), "school-facts-f7-v55"),
        (("runtime", "dingtalk_corp_id"), "other-corp"),
        (("runtime", "identity_session_cookie_name"), "campus_ai_session"),
        (("allowed_origin",), "http://school.example.test"),
        (("allowed_origin",), "https://school.example.test/path"),
        (("trusted_proxy_cidrs",), ["0.0.0.0/0"]),
        (("directory",), None),
        (("family",), None),
        (("education",), None),
        (("family_leadership",), None),
        (("residence",), None),
        (("residence", "dependency_education"), None),
        (("model", "budget", "cap_fen"), 5000),
        (("model", "budget_window_end"), "2026-10-28T00:00:00+08:00"),
        (("model", "ledger_purpose"), "school-facts-f7-synthetic"),
        (("model_data_policy", "allow_normal_names"), False),
    ],
)
def test_formal_configuration_rejects_missing_or_mismatched_authority(tmp_path, path, value):
    data = copy.deepcopy(live_document(tmp_path))
    node = data
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    with pytest.raises(ValidationError):
        LiveSchoolFactProductConfiguration.model_validate_json(json.dumps(data))


def test_formal_configuration_does_not_read_environment_into_nested_settings(tmp_path, monkeypatch):
    data = live_document(tmp_path)
    data["runtime"].pop("dingtalk_app_key")
    monkeypatch.setenv("DINGTALK_APP_KEY", "ambient-key")
    with pytest.raises(ValidationError):
        LiveSchoolFactProductConfiguration.model_validate_json(json.dumps(data))


def test_formal_dingtalk_secret_cannot_reuse_runtime_signing_key(tmp_path):
    data = live_document(tmp_path)
    data["runtime"]["dingtalk_app_secret"] = data["runtime"]["rls_context_signing_key"]
    with pytest.raises(ValidationError):
        LiveSchoolFactProductConfiguration.model_validate_json(json.dumps(data))


def test_live_budget_window_rejects_reserve_outside_approved_interval(tmp_path):
    data = live_document(tmp_path)
    config = LiveSchoolFactProductConfiguration.model_validate_json(json.dumps(data))
    assert config.model is not None
    start = config.model.budget_window_start
    permit = build_live_model_call_permit(config.model, clock=lambda: start - timedelta(seconds=1))
    with pytest.raises(RuntimeError, match="window"):
        permit.reserve(1024, 1024)
    permit = build_live_model_call_permit(config.model, clock=lambda: start + timedelta(days=30))
    with pytest.raises(RuntimeError, match="window"):
        permit.reserve(1024, 1024)
    assert config.model.budget_window_end.astimezone(UTC) == start.astimezone(UTC) + timedelta(
        days=30
    )


def test_live_ledger_records_own_purpose_and_cannot_be_reopened_as_engineering(tmp_path):
    from app.composition.school_fact_current_model_budget import SqliteCurrentModelCallPermit
    from app.composition.school_fact_model_budget import ApprovedModelBudget

    config = LiveSchoolFactProductConfiguration.model_validate_json(
        json.dumps(live_document(tmp_path))
    )
    model = config.model
    assert model is not None
    permit = build_live_model_call_permit(model, clock=lambda: model.budget_window_start)
    permit.reserve(1024, 1024)
    path = model.state_directory / "school-facts-live-model-budget.sqlite3"
    with sqlite3.connect(path) as db:
        metadata = json.loads(db.execute("SELECT usage_json FROM model_calls").fetchone()[0])
    assert metadata["current_model_budget"]["policy"]["ledger_purpose"] == "school-facts-live"
    old_budget = ApprovedModelBudget(
        approval_reference="approved-engineering",
        cap_fen=5000,
        input_cny_per_million=Decimal("3"),
        output_cny_per_million=Decimal("9"),
    )
    with pytest.raises(RuntimeError):
        SqliteCurrentModelCallPermit(
            path=path,
            ledger_policy=old_budget,
            current_approval=model.current_approval,
        )


def test_live_window_is_rechecked_before_transmission(tmp_path):
    config = LiveSchoolFactProductConfiguration.model_validate_json(
        json.dumps(live_document(tmp_path))
    )
    model = config.model
    assert model is not None
    now = [model.budget_window_start]
    permit = build_live_model_call_permit(model, clock=lambda: now[0])
    reservation = permit.reserve(1024, 1024)
    now[0] = model.budget_window_end
    with pytest.raises(RuntimeError, match="window"):
        reservation.mark_transmission_started()
    summary = permit.summary()
    assert summary["global_occupied_microyuan"] > 0


def test_used_live_ledger_rejects_rolling_the_approved_window(tmp_path):
    first = LiveSchoolFactProductConfiguration.model_validate_json(
        json.dumps(live_document(tmp_path))
    ).model
    assert first is not None
    build_live_model_call_permit(first, clock=lambda: first.budget_window_start).reserve(1024, 1024)
    changed = live_document(tmp_path)
    changed["model"]["budget_window_start"] = "2026-09-28T00:00:00+08:00"
    changed["model"]["budget_window_end"] = "2026-10-28T00:00:00+08:00"
    revised = LiveSchoolFactProductConfiguration.model_validate_json(json.dumps(changed)).model
    assert revised is not None
    with pytest.raises(RuntimeError, match="policy drift"):
        build_live_model_call_permit(revised, clock=lambda: revised.budget_window_start)


def test_live_permit_is_accepted_by_formal_pydanticai_model_builder(tmp_path):
    import httpx as httpx2
    from pydantic import SecretStr

    from app.composition.school_fact_pydanticai_model import build_guarded_model

    approved = LiveSchoolFactProductConfiguration.model_validate_json(
        json.dumps(live_document(tmp_path))
    ).model
    assert approved is not None
    permit = build_live_model_call_permit(approved, clock=lambda: approved.budget_window_start)
    model = build_guarded_model(
        api_key=SecretStr("test-only-not-a-real-key"),
        permit=permit,
        approval=approved.current_approval,
        current=lambda: None,
        max_input_tokens=approved.max_input_tokens,
        max_output_tokens=approved.max_output_tokens,
        transport=httpx2.MockTransport(lambda _: httpx2.Response(500)),
    )
    assert model.checks_transmission_before_send
    asyncio.run(model.aclose())
