import copy
import importlib.util
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    "output,allowed",
    [
        (b"c1137be2-0734-4b69-8937-37dbd741f141\nready\n", True),
        (b"c1137be2-0734-4b69-8937-37dbd741f141\nblocked\n", False),
        (b"ready\n", False),
        (b"other-school\nready\n", False),
    ],
)
def test_actual_anchor_function_output_and_quiescence_are_both_required(output, allowed):
    from types import SimpleNamespace

    h = SimpleNamespace(DOCKER=[], safe_run=lambda *a, **kw: output)
    if allowed:
        helper().require_quiescent(h)
    else:
        with pytest.raises(ValueError):
            helper().require_quiescent(h)


def helper():
    path = Path(__file__).resolve().parents[4] / "scripts/f4_engineering/runtime_update.py"
    spec = importlib.util.spec_from_file_location("f4_runtime_update_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_docker_environment_order_may_change_but_values_and_unique_keys_cannot():
    old = {
        "Config": {"Env": ["A=one", "B=two"]},
        "HostConfig": {},
        "Mounts": [],
        "NetworkSettings": {"Networks": {"private": {}}},
    }
    new = copy.deepcopy(old)
    new["Config"]["Env"].reverse()
    helper().require_preserved_shape(old, new)
    for entries in (["A=changed", "B=two"], ["A=one", "B=two", "A=one"], ["A=one"]):
        new["Config"]["Env"] = entries
        with pytest.raises(ValueError):
            helper().require_preserved_shape(old, new)


def configs():
    base = {
        "purpose": "school-facts-f3-synthetic",
        "reader": {"schema_contract": "school-facts-f3-v27"},
        "runtime": {"postgres_schema_contract": "school-facts-f3-v27"},
        "family": {"scope_bindings": ["retained"]},
        "directory": {"scope_bindings": ["retained"]},
        "model": {
            "model": "deepseek-flash",
            "thinking": "high",
            "budget": {"limit": 50},
            "stage_budget": {"stage_id": "school-facts-f3", "cap_microyuan": 3_000_000},
        },
    }
    new = copy.deepcopy(base)
    new.update(
        purpose="school-facts-f4-synthetic", education={"enabled": False, "scope_bindings": []}
    )
    new["reader"]["schema_contract"] = new["runtime"]["postgres_schema_contract"] = (
        "school-facts-f4-v29"
    )
    new["model"]["stage_budget"]["stage_id"] = "school-facts-f4"
    return base, new


def test_scope29_update_preserves_all_retained_family_and_directory_configuration():
    old, new = configs()
    helper().require_config_delta(old, new, product=True)
    assert new["family"] == old["family"] and new["directory"] == old["directory"]


@pytest.mark.parametrize("change", ["family", "budget", "model", "runtime"])
def test_update_rejects_unrelated_authority_model_budget_and_database_changes(change):
    old, new = configs()
    if change == "family":
        new["family"]["scope_bindings"] = ["broader"]
    elif change == "budget":
        new["model"]["budget"]["limit"] = 51
    elif change == "model":
        new["model"]["model"] = "other"
    else:
        new["runtime"]["postgres_host"] = "other"
    with pytest.raises(ValueError, match="outside F4"):
        helper().require_config_delta(old, new, product=True)


@pytest.mark.parametrize("revision", [30, 31])
def test_incremental_update_checks_exact_live_revision_before_stopping(revision):
    from types import SimpleNamespace

    captured = []

    def run(*a, **kw):
        captured.append(kw["data"].decode())
        return b"c1137be2-0734-4b69-8937-37dbd741f141\nready\n"

    helper().require_quiescent(
        SimpleNamespace(DOCKER=[], safe_run=run), f"school-facts-f4-v{revision}"
    )
    assert f"='20260913_{revision}'" in captured[0]
    assert "20260913_29" not in captured[0]


@pytest.mark.parametrize("old_revision,new_revision", [(29, 31), (30, 31), (31, 31)])
def test_incremental31_preserves_other_scopes_and_allows_only_matching_contract(
    old_revision, new_revision
):
    _, old = configs()
    old["reader"]["schema_contract"] = old["runtime"]["postgres_schema_contract"] = (
        f"school-facts-f4-v{old_revision}"
    )
    new = copy.deepcopy(old)
    new["reader"]["schema_contract"] = new["runtime"]["postgres_schema_contract"] = (
        f"school-facts-f4-v{new_revision}"
    )
    helper().require_config_delta(old, new, product=True)


@pytest.mark.parametrize("old_revision,new_revision", [(30, 29), (31, 30), (31, 29), (31, 32)])
def test_applied_revision_cannot_be_downgraded_or_advanced_to_unknown_schema(
    old_revision, new_revision
):
    _, old = configs()
    old["reader"]["schema_contract"] = old["runtime"]["postgres_schema_contract"] = (
        f"school-facts-f4-v{old_revision}"
    )
    new = copy.deepcopy(old)
    new["reader"]["schema_contract"] = new["runtime"]["postgres_schema_contract"] = (
        f"school-facts-f4-v{new_revision}"
    )
    with pytest.raises(ValueError):
        helper().require_config_delta(old, new, product=True)
