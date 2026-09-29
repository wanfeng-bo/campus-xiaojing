"""F6 update guards are exercised without any original runtime or private I/O."""

from copy import deepcopy
from importlib import import_module, util
from types import SimpleNamespace

import pytest


def module():
    name = "scripts.f6_engineering.runtime_update"
    assert util.find_spec(name), "F6 runtime update guard is not implemented"
    return import_module(name)


def configs():
    old = {
        "purpose": "school-facts-f5-synthetic",
        "reader": {"schema_contract": "school-facts-f5-v41", "password": "test-fixed"},
        "runtime": {"postgres_schema_contract": "school-facts-f5-v41", "host": "fixed"},
        "directory": {"scope_bindings": ["old"]},
        "family": {"scope_bindings": ["old"]},
        "education": {"scope_bindings": ["old"]},
        "f5_features_enabled": True,
        "model": {
            "state_directory": "retained-ledger",
            "max_input_tokens": 98304,
            "budget": {"cap_fen": 5000, "approval_reference": "retained"},
            "stage_budget": {
                "stage_id": "school-facts-f5",
                "cap_microyuan": 6000000,
                "approval_reference": "old-extension",
                "previous_cap_microyuan": 3000000,
                "previous_approval_reference": "original-f5",
            },
        },
    }
    new = deepcopy(old)
    new.update(purpose="school-facts-f6-synthetic", f6_features_enabled=True)
    new["residence"] = {"enabled": True, "approval_reference": "f6-approved", "scope_bindings": []}
    new["reader"]["schema_contract"] = new["runtime"]["postgres_schema_contract"] = (
        "school-facts-f6-v45"
    )
    new["model"]["stage_budget"] = {
        "stage_id": "school-facts-f6",
        "cap_microyuan": 3000000,
        "approval_reference": "f6-approved",
    }
    return old, new


def reviewed(new):
    m = module()
    return {
        "residence": m.coordinate_digest(new["residence"]),
        "model.stage_budget": m.coordinate_digest(new["model"]["stage_budget"]),
    }


def test_f6_update_and_disabled_fallback_keep_original_ledger_and_f5_configuration():
    old, new = configs()
    m = module()
    approved = reviewed(new)
    m.require_config_delta(old, new, product=True, approved_coordinates=approved)
    fallback = deepcopy(new)
    fallback["f6_features_enabled"] = False
    m.require_config_delta(new, fallback, product=True, approved_coordinates=approved)
    m.require_config_delta(fallback, new, product=True, approved_coordinates=approved)
    assert new["model"]["state_directory"] == old["model"]["state_directory"]
    assert new["education"] == old["education"] and new["f5_features_enabled"]


@pytest.mark.parametrize(
    "change",
    [
        "scope",
        "ledger",
        "global",
        "stage",
        "approval",
        "history",
        "schema",
        "purpose",
        "password",
        "input",
        "type",
    ],
)
def test_f6_update_rejects_unreviewed_or_unrelated_delta(change):
    old, new = configs()
    approved = reviewed(new)
    if change == "scope":
        new["residence"]["scope_bindings"] = ["unreviewed"]
    elif change == "ledger":
        new["model"]["state_directory"] = "fresh"
    elif change == "global":
        new["model"]["budget"]["cap_fen"] = 10000
    elif change == "stage":
        new["model"]["stage_budget"]["cap_microyuan"] = 6000000
    elif change == "approval":
        new["model"]["stage_budget"]["approval_reference"] = "different"
    elif change == "history":
        new["model"]["stage_budget"]["previous_cap_microyuan"] = 1000000
    elif change == "schema":
        new["reader"]["schema_contract"] = "school-facts-f5-v41"
    elif change == "purpose":
        new["purpose"] = "school-facts-f5-synthetic"
    elif change == "password":
        new["reader"]["password"] = "changed"
    elif change == "input":
        new["model"]["max_input_tokens"] = 65536
    else:
        new["f5_features_enabled"] = 1
    with pytest.raises(ValueError):
        module().require_config_delta(old, new, product=True, approved_coordinates=approved)


def test_f6_exact_scope_delta_needs_reviewed_digest_and_no_unknown_coordinates():
    old, new = configs()
    m = module()
    new["family"]["scope_bindings"] = ["reviewed"]
    approved = reviewed(new) | {"family.scope_bindings": m.coordinate_digest(["reviewed"])}
    m.require_config_delta(old, new, product=True, approved_coordinates=approved)
    for wrong in ({}, approved | {"reader.password": m.coordinate_digest("test-fixed")}):
        with pytest.raises(ValueError):
            m.require_config_delta(old, new, product=True, approved_coordinates=wrong)


def test_f6_pair_requires_matching_explicit_boolean_flags_and_exact_current_schema():
    m = module()
    _, config = configs()
    m.require_feature_pair({"Product": config, "Writer": deepcopy(config)})
    for flag in (False, 1, None):
        with pytest.raises(ValueError):
            m.require_feature_pair(
                {"Product": config, "Writer": dict(config, f6_features_enabled=flag)}
            )


def test_f6_quiescent_probe_is_exact_read_only_and_preserves_original_anchor():
    calls = []
    helper = SimpleNamespace(
        DOCKER=[],
        safe_run=lambda *a, **kw: (
            calls.append(kw["data"].decode()) or b"c1137be2-0734-4b69-8937-37dbd741f141\nready\n"
        ),
    )
    module().require_quiescent(helper)
    assert "='20260922_45'" in calls[0] and "BEGIN READ ONLY" in calls[0]
    assert "7682019857215991846" in calls[0]
    assert "rolcanlogin" in calls[0] and "pg_prepared_xacts" in calls[0]


def test_wrapper_uses_retained_service_flow_with_f6_guards_and_no_product_only(monkeypatch):
    m, calls = module(), []
    monkeypatch.setattr(
        m.retained, "update_services", lambda **kw: calls.append(kw) or {"status": "prepared"}
    )
    assert m.update_services(
        proof={}, configurations={}, evidence_path=None, expected_images={}
    ) == {"status": "prepared"}
    assert calls[0]["execute"] is False and calls[0]["_family"] == "f6"
    assert calls[0]["_config_check"] is m.require_config_delta
    assert calls[0]["_release_check"] is m.release.require_snapshot
    assert calls[0]["_quiescent_check"] is m.require_quiescent
    with pytest.raises(ValueError):
        m.update_services(
            proof={}, configurations={}, evidence_path=None, expected_images={}, product_only=True
        )
