"""F5 is wired through the normal product contracts, with explicit leadership policy."""

import json
from importlib import import_module
from importlib.util import find_spec

import pytest
from pydantic import ValidationError


def test_f5_explicit_six_yuan_stage_extension_is_accepted(tmp_path):
    from app.composition.school_fact_product_settings import SchoolFactProductConfiguration
    from tests.school_facts.test_f5_startup_contract import f5_document

    data = f5_document(tmp_path)
    stage = data["model"]["stage_budget"]
    stage.update(
        previous_cap_microyuan=3_000_000,
        previous_approval_reference=stage["approval_reference"],
        cap_microyuan=6_000_000,
        approval_reference="approved-f5-extension",
    )
    result = SchoolFactProductConfiguration.model_validate_json(json.dumps(data))
    assert result.model.stage_budget.cap_microyuan == 6_000_000


@pytest.mark.parametrize("version", ["f3", "f4"])
def test_f5_budget_extension_does_not_expand_prior_stage_approval(tmp_path, version):
    from app.composition.school_fact_product_settings import SchoolFactProductConfiguration
    from tests.assistant.test_f3_product_configuration import f3_document
    from tests.assistant.test_f4_product_configuration import f4_document

    data = (f3_document if version == "f3" else f4_document)(tmp_path)
    stage = data["model"]["stage_budget"]
    stage.update(
        previous_cap_microyuan=3_000_000,
        previous_approval_reference=stage["approval_reference"],
        cap_microyuan=6_000_000,
        approval_reference="unapproved-prior-stage-extension",
    )
    with pytest.raises(ValidationError):
        SchoolFactProductConfiguration.model_validate_json(json.dumps(data))


@pytest.mark.parametrize(
    "cap,previous",
    [(6_000_001, 3_000_000), (6_000_000, None), (6_000_000, 2_000_000), (3_000_000, 2_000_000)],
)
def test_f5_rejects_unapproved_budget_extensions(tmp_path, cap, previous):
    from app.composition.school_fact_product_settings import SchoolFactProductConfiguration
    from tests.school_facts.test_f5_startup_contract import f5_document

    data = f5_document(tmp_path)
    stage = data["model"]["stage_budget"]
    stage.update(cap_microyuan=cap, approval_reference="new")
    if previous is not None:
        stage.update(previous_cap_microyuan=previous, previous_approval_reference="old")
    with pytest.raises(ValidationError):
        SchoolFactProductConfiguration.model_validate_json(json.dumps(data))


def test_f5_common_maintenance_contract_accepts_teaching_without_new_write_path():
    from app.composition.school_fact_directory_product import FactChangeSummary
    from app.domains.students.teaching_contracts import TEACHING_KINDS

    allowed = str(FactChangeSummary.model_json_schema())
    assert all(kind in allowed for kind in TEACHING_KINDS)


def test_leadership_configuration_is_school_bound_not_a_model_selector(tmp_path):
    from app.composition.school_fact_product_settings import SchoolFactProductConfiguration
    from tests.school_facts.test_f5_startup_contract import f5_document

    data = f5_document(tmp_path)
    policy = {
        "school_id": data["reader"]["expected_school_id"],
        "publication_reference": "F5-approved-engineering",
        "version": 1,
        "school_role_codes": ["principal"],
        "campus_role_codes": ["campus_principal"],
    }
    data["family_leadership"] = policy
    assert SchoolFactProductConfiguration.model_validate_json(json.dumps(data)).family_leadership
    policy["school_id"] = "00000000-0000-0000-0000-000000000999"
    with pytest.raises(ValidationError):
        SchoolFactProductConfiguration.model_validate_json(json.dumps(data))


def test_parent_teaching_tool_cannot_select_another_child_or_actor():
    name = "app.composition.school_fact_family_teaching_gateway"
    assert find_spec(name), "Teaching tool is not assembled"
    m = import_module(name)
    for key in ("student_id", "person_id", "assignment_id", "school_id"):
        with pytest.raises(ValidationError):
            m.FamilyTeachingInput.model_validate({key: "arbitrary"})


def test_direct_teaching_http_revalidates_before_returning_answer():
    from types import SimpleNamespace

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from tests.school_facts.test_f5_family_teaching import fixture

    name = "app.interfaces.http.school_fact_teaching"
    assert find_spec(name), "F5 direct reading route missing"
    m = import_module(name)
    service, actor, state = fixture()
    identity = SimpleNamespace(actor=actor, actor_context_ref="current-child")
    app = FastAPI()
    app.include_router(
        m.create_teaching_read_router(
            identity_dependency=lambda: identity,
            resolve_identity=lambda _: identity,
            family_teaching=service,
            leadership=None,
            staff_teaching=None,
        )
    )
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/school-fact-product/family/teaching", json={"subject_name": "数学"}
        )
        assert response.status_code == 200 and response.json()["status"] == "no_current_records"
        state.active = False
        denied = client.post("/api/v1/school-fact-product/family/teaching", json={})
        assert denied.status_code == 403 and "items" not in denied.json()
