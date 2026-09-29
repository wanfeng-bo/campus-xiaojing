"""F3 is an explicit supported product, never a name that selects the old loop."""

import copy
import json
from typing import Any, cast

import pytest
from pydantic import ValidationError

from app.composition.school_fact_product_settings import SchoolFactProductConfiguration
from app.db.schema_contract import schema_contract_head
from tests.assistant.test_school_fact_product_factory import current_model_document, document


def f3_document(tmp_path):
    data = cast(Any, document).__wrapped__()
    data["purpose"] = "school-facts-f3-synthetic"
    data["runtime"]["postgres_schema_contract"] = "school-facts-f3-v27"
    data["reader"]["schema_contract"] = "school-facts-f3-v27"
    data["directory"] = {"approval_reference": "prior-f2", "scope_bindings": []}
    data["family"] = {"approval_reference": "approved-f3", "scope_bindings": []}
    data["model"] = current_model_document(tmp_path)
    data["model"]["stage_budget"] = {
        "stage_id": "school-facts-f3",
        "approval_reference": "approved-f3-model-budget",
        "cap_microyuan": 3000000,
    }
    return data


def test_f3_configuration_requires_exact_same_new_schema_and_explicit_budget(tmp_path):
    config = SchoolFactProductConfiguration.model_validate_json(json.dumps(f3_document(tmp_path)))
    assert schema_contract_head(config.runtime.postgres_schema_contract) == "20260913_27"
    assert config.family is not None
    assert config.model is not None and config.model.stage_budget is not None
    assert config.family.approval_reference == "approved-f3"
    assert config.model.stage_budget.cap_microyuan == 3000000


@pytest.mark.parametrize(
    "path,value",
    [
        (("reader", "schema_contract"), "school-facts-f2-v25"),
        (("runtime", "postgres_schema_contract"), "school-facts-f2-v25"),
        (("model", "stage_budget"), None),
        (("model", "stage_budget", "cap_microyuan"), 3000001),
        (("model", "stage_budget", "stage_id"), "new-f3-reset"),
        (("model", "thinking"), "disabled"),
        (("family",), None),
        (("directory",), None),
        (("purpose",), "school-facts-f2-synthetic"),
    ],
)
def test_f3_configuration_rejects_incomplete_or_expanded_approval(tmp_path, path, value):
    data = copy.deepcopy(f3_document(tmp_path))
    item = data
    for key in path[:-1]:
        item = item[key]
    item[path[-1]] = value
    with pytest.raises(ValidationError):
        SchoolFactProductConfiguration.model_validate_json(json.dumps(data))


@pytest.mark.parametrize(
    "contract,head",
    [
        ("school-facts-f2-v23", "20260906_23"),
        ("school-facts-f2-v25", "20260906_25"),
        ("school-facts-f3-v26", "20260913_26"),
        ("school-facts-f3-v27", "20260913_27"),
    ],
)
def test_schema_contract_maps_exact_history_and_current_without_fallback(contract, head):
    assert schema_contract_head(contract) == head


def test_current_f3_rejects_old26_even_when_runtime_and_reader_agree(tmp_path):
    data = f3_document(tmp_path)
    data["runtime"]["postgres_schema_contract"] = "school-facts-f3-v26"
    data["reader"]["schema_contract"] = "school-facts-f3-v26"
    with pytest.raises(ValidationError):
        SchoolFactProductConfiguration.model_validate_json(json.dumps(data))
