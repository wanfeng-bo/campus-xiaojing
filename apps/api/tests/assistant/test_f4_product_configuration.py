import copy
import json

import pytest
from pydantic import ValidationError

from app.composition.school_fact_product_settings import SchoolFactProductConfiguration
from tests.assistant.test_f3_product_configuration import f3_document


def f4_document(tmp_path):
    data = f3_document(tmp_path)
    data["purpose"] = "school-facts-f4-synthetic"
    data["runtime"]["postgres_schema_contract"] = "school-facts-f4-v29"
    data["reader"]["schema_contract"] = "school-facts-f4-v29"
    data["education"] = {"approval_reference": "approved-f4", "scope_bindings": []}
    data["model"]["stage_budget"] = {
        "stage_id": "school-facts-f4",
        "approval_reference": "approved-f4-budget",
        "cap_microyuan": 3_000_000,
    }
    return data


def test_f4_explicit_assembly_keeps_family_directory_and_its_own_stage_gate(tmp_path):
    result = SchoolFactProductConfiguration.model_validate_json(json.dumps(f4_document(tmp_path)))
    assert result.education.enabled
    assert result.family.enabled and result.directory is not None
    assert result.model.stage_budget.stage_id == "school-facts-f4"
    assert result.model.thinking == "enabled"


@pytest.mark.parametrize(
    "path,value",
    [
        (("family",), None),
        (("education",), None),
        (("directory",), None),
        (("model", "stage_budget", "stage_id"), "school-facts-f3"),
        (("model", "stage_budget", "cap_microyuan"), 3_000_001),
        (("reader", "schema_contract"), "school-facts-f3-v27"),
        (("purpose",), "school-facts-f3-synthetic"),
    ],
)
def test_f4_rejects_incomplete_dependencies_and_budget_reset(tmp_path, path, value):
    data = copy.deepcopy(f4_document(tmp_path))
    target = data
    for item in path[:-1]:
        target = target[item]
    target[path[-1]] = value
    with pytest.raises(ValidationError):
        SchoolFactProductConfiguration.model_validate_json(json.dumps(data))


def test_f4_assembly_uses_official_runner_and_expires_education_stores(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from unittest.mock import MagicMock, patch

    from pydantic import SecretStr

    from app.composition import school_fact_pydanticai as supported
    from app.platform.assistant.pydanticai_runner import SynchronousCandidateRunner
    from tests.assistant.test_pydanticai_product_adapter import _product_factory_environment
    from tests.test_f3_product_identity import guardian

    product, _, router, *_ = _product_factory_environment(monkeypatch, tmp_path)
    config = SchoolFactProductConfiguration.model_validate_json(json.dumps(f4_document(tmp_path)))
    monkeypatch.setattr(product, "load_school_fact_product_configuration", lambda: config)
    identity = MagicMock()
    identity.resolve_private_actor.return_value = SimpleNamespace(actor=guardian())
    monkeypatch.setattr(
        product, "build_school_fact_product_identity_service", lambda **kw: identity
    )
    monkeypatch.setattr(product, "create_family_query_service", lambda **kw: MagicMock())
    monkeypatch.setattr(product, "create_family_staff_components", lambda **kw: MagicMock())
    monkeypatch.setattr(supported, "load_product_model_api_key", lambda _: SecretStr("unit"))
    permit = MagicMock()
    monkeypatch.setattr(supported, "SqliteCurrentModelCallPermit", lambda **kw: permit)
    monkeypatch.setattr(
        product, "build_product_model", MagicMock(side_effect=AssertionError("no legacy fallback"))
    )
    janitor = MagicMock()
    monkeypatch.setattr(product, "SchoolFactEphemeralStoreJanitor", janitor)
    correction_factory = MagicMock(wraps=product.FamilyCorrectionProduct)
    education_factory = MagicMock(wraps=product.create_education_components)
    monkeypatch.setattr(product, "FamilyCorrectionProduct", correction_factory)
    monkeypatch.setattr(product, "create_education_components", education_factory)
    app = product.create_school_fact_product_app()
    education_lookup = correction_factory.call_args.kwargs["education_source"]
    # The production closure is bound after education assembly, and resolves the
    # current signed F4 reference; the F3 resolver cannot stand in for this port.
    from app.composition.school_fact_education_reader import EducationRepository

    with patch.object(
        EducationRepository, "source_for_reference", return_value="exact-education-source"
    ) as exact:
        assert (
            education_lookup("actor", "class_placement", "f4:class_placement:signed")
            == "exact-education-source"
        )
        exact.assert_called_once_with("actor", "class_placement", "f4:class_placement:signed")
    runner = router.call_args.kwargs["turn_orchestrator"]
    assert isinstance(runner, SynchronousCandidateRunner)
    assert app.version == "f4"
    assert "/api/v1/school-fact-product/education-facts/lookup" in app.openapi()["paths"]
    assert "school_facts.family.education" in [
        d.name for d in runner.runner.gateway_factory("parent").descriptors
    ]
    from tests.test_f2_directory_product import actor

    identity.resolve_private_actor.return_value = SimpleNamespace(
        actor=actor(), session_binding="staff"
    )
    assert "school_facts.education.query" in [
        d.name for d in runner.runner.gateway_factory("staff").descriptors
    ]
    assert any(isinstance(store, product.EducationToolGateway) for store in janitor.call_args.args)
    from app.composition.school_fact_directory_product import DirectoryFactProductService

    assert (
        len(
            [
                store
                for store in janitor.call_args.args
                if isinstance(store, DirectoryFactProductService)
            ]
        )
        == 3
    )
    permit.reserve.assert_not_called()


def test_f4_dependency_configuration_is_separate_from_original_scopes(tmp_path):
    data = f4_document(tmp_path)
    original = copy.deepcopy((data["directory"], data["family"]))
    data["education"]["dependency_directory"] = {
        "approval_reference": "f4-only",
        "scope_bindings": [],
    }
    data["education"]["dependency_family"] = {"approval_reference": "f4-only", "scope_bindings": []}
    result = SchoolFactProductConfiguration.model_validate_json(json.dumps(data))
    assert result.education.dependency_directory.approval_reference == "f4-only"
    assert result.education.dependency_family.approval_reference == "f4-only"
    assert (data["directory"], data["family"]) == original
    assert result.directory.approval_reference != "f4-only"
