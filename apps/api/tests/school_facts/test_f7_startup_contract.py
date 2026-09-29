"""F7 may start only with its exact schema and retained safety dependencies."""

import copy
import json
import os
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.composition.school_fact_product_settings import SchoolFactProductConfiguration
from app.composition.school_fact_writer_executor import SqlAlchemySchoolFactWriterExecutorKernel
from app.db.schema_contract import schema_contract_head
from tests.school_facts.test_f6_startup_contract import f6_document

F7_PROBE_CONTRACTS = {
    "school-facts-f7-v55": "20260924_55",
    "school-facts-f7-v56": "20260924_56",
}


def f7_probe_contract():
    """Explicit candidate selection; never infer a contract from database state."""
    contract = os.environ.get("F7_TEST_SCHEMA_CONTRACT", "school-facts-f7-v55")
    if contract not in F7_PROBE_CONTRACTS:
        raise ValueError("Unsupported isolated F7 probe contract")
    return contract


def f7_document(tmp_path, *, contract="school-facts-f7-v55"):
    if contract not in F7_PROBE_CONTRACTS:
        raise ValueError("Unsupported isolated F7 configuration contract")
    data = f6_document(tmp_path, contract=contract)
    data["purpose"] = "school-facts-f7-synthetic"
    data["model"]["stage_budget"]["stage_id"] = "school-facts-f7"
    return data


@pytest.mark.parametrize("contract", tuple(F7_PROBE_CONTRACTS))
def test_f7_exact_product_contract_and_budget(tmp_path, contract):
    data = f7_document(tmp_path, contract=contract)
    result = SchoolFactProductConfiguration.model_validate_json(json.dumps(data))
    assert result.purpose == "school-facts-f7-synthetic"
    assert result.model.stage_budget.cap_microyuan == 3_000_000
    assert schema_contract_head(contract) == F7_PROBE_CONTRACTS[contract]
    for path, value in [
        (("family",), None),
        (("education",), None),
        (("directory",), None),
        (("residence",), None),
        (("residence", "dependency_education"), None),
        (("model", "stage_budget", "stage_id"), "school-facts-f6"),
        (("model", "stage_budget", "cap_microyuan"), 3_000_001),
        (("reader", "schema_contract"), "school-facts-f6-v45"),
    ]:
        changed = copy.deepcopy(data)
        node = changed
        for key in path[:-1]:
            node = node[key]
        node[path[-1]] = value
        with pytest.raises(ValidationError):
            SchoolFactProductConfiguration.model_validate_json(json.dumps(changed))


@pytest.mark.parametrize("guard", [None, lambda *_: None])
@pytest.mark.parametrize(
    "contract",
    [
        "school-facts-f7-v47",
        "school-facts-f7-v48",
        "school-facts-f7-v49",
        "school-facts-f7-v50",
        "school-facts-f7-v51",
        "school-facts-f7-v52",
        "school-facts-f7-v53",
        "school-facts-f7-v54",
        "school-facts-f7-v55",
        "school-facts-f7-v56",
    ],
)
def test_f7_writer_requires_final_authority_and_family_validation(guard, contract):
    with pytest.raises(ValueError, match="final authorization|family actor"):
        SqlAlchemySchoolFactWriterExecutorKernel(
            engine=None,
            settings=SimpleNamespace(schema_contract=contract),
            final_authorization_guard=guard,
        )


def test_probe_schema_choice_is_explicit_and_rejects_unknown(monkeypatch):
    monkeypatch.delenv("F7_TEST_SCHEMA_CONTRACT", raising=False)
    assert f7_probe_contract() == "school-facts-f7-v55"
    monkeypatch.setenv("F7_TEST_SCHEMA_CONTRACT", "school-facts-f7-v56")
    assert f7_probe_contract() == "school-facts-f7-v56"
    monkeypatch.setenv("F7_TEST_SCHEMA_CONTRACT", "school-facts-f7-v999")
    with pytest.raises(ValueError, match="Unsupported"):
        f7_probe_contract()


def _extend_stage(data, cap=4_000_000, previous=3_000_000):
    stage = data["model"]["stage_budget"]
    stage.update(
        previous_approval_reference=stage["approval_reference"],
        approval_reference="approved-f7-four-yuan-continuation",
        previous_cap_microyuan=previous,
        cap_microyuan=cap,
    )
    return data


@pytest.mark.parametrize("contract", tuple(F7_PROBE_CONTRACTS))
def test_f7_admits_only_explicit_three_to_four_yuan_continuation(tmp_path, contract):
    data = _extend_stage(f7_document(tmp_path, contract=contract))
    config = SchoolFactProductConfiguration.model_validate_json(json.dumps(data))
    assert config.model.stage_budget.cap_microyuan == 4_000_000
    assert config.model.stage_budget.previous_cap_microyuan == 3_000_000
    assert config.model.stage_budget.previous_approval_reference == (
        data["model"]["stage_budget"]["previous_approval_reference"]
    )
    assert config.model.budget.cap_fen == 5000
    assert config.model.thinking == "enabled"


@pytest.mark.parametrize("change", [
    {"cap_microyuan": 4_000_001},
    {"cap_microyuan": 6_000_000},
    {"previous_cap_microyuan": 2_000_000},
    {"previous_cap_microyuan": None},
    {"previous_approval_reference": None},
    {"previous_approval_reference": "approved-f7-four-yuan-continuation"},
])
def test_f7_continuation_does_not_admit_arbitrary_cap_or_missing_history(tmp_path, change):
    data = _extend_stage(f7_document(tmp_path, contract="school-facts-f7-v56"))
    data["model"]["stage_budget"].update(change)
    with pytest.raises(ValidationError):
        SchoolFactProductConfiguration.model_validate_json(json.dumps(data))


@pytest.mark.parametrize("stage_name", ["f3", "f4", "f5", "f6"])
def test_f7_four_yuan_approval_does_not_expand_other_product_stages(tmp_path, stage_name):
    from tests.assistant.test_f3_product_configuration import f3_document
    from tests.assistant.test_f4_product_configuration import f4_document
    from tests.school_facts.test_f5_startup_contract import f5_document

    factories = {
        "f3": f3_document, "f4": f4_document, "f5": f5_document, "f6": f6_document,
    }
    factory = factories[stage_name]
    data = _extend_stage(factory(tmp_path))
    with pytest.raises(ValidationError):
        SchoolFactProductConfiguration.model_validate_json(json.dumps(data))


def test_original_f5_three_to_six_yuan_approval_remains_compatible(tmp_path):
    from tests.school_facts.test_f5_startup_contract import f5_document

    data = _extend_stage(f5_document(tmp_path), cap=6_000_000)
    data["model"]["stage_budget"]["approval_reference"] = "approved-f5-six-yuan"
    config = SchoolFactProductConfiguration.model_validate_json(json.dumps(data))
    assert config.model.stage_budget.stage_id == "school-facts-f5"
    assert config.model.stage_budget.cap_microyuan == 6_000_000
