"""F6 is an explicit, bounded successor; old purposes cannot enable boarding."""

import copy
import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.composition.school_fact_product_settings import SchoolFactProductConfiguration
from app.composition.school_fact_writer_executor import SqlAlchemySchoolFactWriterExecutorKernel
from app.db.schema_contract import schema_contract_head
from tests.school_facts.test_f5_startup_contract import f5_document


def f6_document(tmp_path, *, contract="school-facts-f6-v44"):
    data = f5_document(tmp_path)
    data["purpose"] = "school-facts-f6-synthetic"
    data["runtime"]["postgres_schema_contract"] = contract
    data["reader"]["schema_contract"] = contract
    data["model"]["stage_budget"]["stage_id"] = "school-facts-f6"
    data["residence"] = {
        "approval_reference": "F6-approved-synthetic",
        "scope_bindings": [],
        "dependency_education": copy.deepcopy(data["education"]),
    }
    return data


@pytest.mark.parametrize("contract", ["school-facts-f6-v44", "school-facts-f6-v45"])
def test_f6_product_retains_dependencies_and_own_budget(tmp_path, contract):
    data = f6_document(tmp_path, contract=contract)
    result = SchoolFactProductConfiguration.model_validate_json(json.dumps(data))
    assert result.residence.enabled
    assert result.model.stage_budget.cap_microyuan == 3_000_000
    assert schema_contract_head(contract) == "20260922_" + contract.rsplit("v", 1)[1]
    for path, value in [
        (("family",), None),
        (("education",), None),
        (("directory",), None),
        (("residence",), None),
        (("residence", "dependency_education"), None),
        (("model", "stage_budget", "stage_id"), "school-facts-f5"),
        (("model", "stage_budget", "cap_microyuan"), 3_000_001),
        (("reader", "schema_contract"), "school-facts-f5-v41"),
    ]:
        changed = copy.deepcopy(data)
        node = changed
        for key in path[:-1]:
            node = node[key]
        node[path[-1]] = value
        with pytest.raises(ValidationError):
            SchoolFactProductConfiguration.model_validate_json(json.dumps(changed))


def test_old_product_cannot_enable_f6_or_reuse_extended_f5_budget(tmp_path):
    old = f5_document(tmp_path)
    old["residence"] = f6_document(tmp_path)["residence"]
    with pytest.raises(ValidationError):
        SchoolFactProductConfiguration.model_validate_json(json.dumps(old))
    data = f6_document(tmp_path)
    data["model"]["stage_budget"].update(
        cap_microyuan=6_000_000,
        previous_cap_microyuan=3_000_000,
        previous_approval_reference="old-f5",
    )
    with pytest.raises(ValidationError):
        SchoolFactProductConfiguration.model_validate_json(json.dumps(data))


@pytest.mark.parametrize("contract", ["school-facts-f6-v44", "school-facts-f6-v45"])
@pytest.mark.parametrize("guard", [None, lambda *_: None])
def test_f6_writer_requires_final_authority_and_family_validation(guard, contract):
    with pytest.raises(ValueError, match="final authorization|family actor"):
        SqlAlchemySchoolFactWriterExecutorKernel(
            engine=None,
            settings=SimpleNamespace(schema_contract=contract),
            final_authorization_guard=guard,
        )


def test_f6_compatibility_disables_only_f6_new_changes(tmp_path):
    from app.composition.school_fact_product_settings import require_product_fact_features

    data = f6_document(tmp_path)
    data["f6_features_enabled"] = False
    config = SchoolFactProductConfiguration.model_validate_json(json.dumps(data))
    assert config.f5_features_enabled
    for kind, target in [("student_bed_residence", None), ("fact_quality", "dorm_bed")]:
        change = SimpleNamespace(
            fact_kind=kind,
            content=SimpleNamespace(target_fact_kind=target),
            replacement_segments=None,
        )
        with pytest.raises(PermissionError):
            require_product_fact_features(config, SimpleNamespace(changes=(change,)))
    change = SimpleNamespace(fact_kind="teaching_course", content=None, replacement_segments=None)
    require_product_fact_features(config, SimpleNamespace(changes=(change,)))


def test_f6_changes_are_accepted_by_shared_maintenance_envelope():
    from app.composition.school_fact_directory_product import FactChangeInput

    change = FactChangeInput(
        fact_kind="dorm_bed",
        target_ref="f6:bed:opaque",
        operation="correct",
        fields={"display_name": "3号床"},
        reason="正式纠错",
    )
    assert change.fact_kind == "dorm_bed"


@pytest.mark.parametrize("creation_keys,permitted", [(("new-bed",), True), ((), False)])
def test_creation_receipt_entry_does_not_require_unrelated_read_grant(creation_keys, permitted):
    from app.composition.school_fact_residence_services import ResidenceComponents

    class Authority:
        def require_scope(self, actor, *, fact_kind, action, checked_at):
            if action == "maintain" and fact_kind == "dorm_bed":
                return SimpleNamespace(scope=SimpleNamespace(creation_keys=creation_keys))
            raise PermissionError("no current scope")

    components = ResidenceComponents(Authority(), None, None, None)
    if permitted:
        # This is only the entry gate. Writer still checks ownership and every
        # actual target; creation-only results contain no original/current body.
        components.require_receipt(object())
    else:
        with pytest.raises(PermissionError):
            components.require_receipt(object())
