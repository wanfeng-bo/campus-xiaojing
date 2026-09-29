import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.composition import school_fact_product_settings as settings
from app.core.request_id import assign_request_id
from app.interfaces.http.errors import ApiError
from app.interfaces.http.framework_request import api_error_handler
from app.interfaces.http.school_fact_education import create_education_product_router
from tests.school_facts.test_f5_startup_contract import f5_document
from tests.test_f1_official_contact_query import _actor


def test_rollback_keeps_f5_schema_stage_and_explicit_strict_switch(tmp_path):
    data = f5_document(tmp_path)
    data["f5_features_enabled"] = False
    config = settings.SchoolFactProductConfiguration.model_validate_json(json.dumps(data))
    assert config.f5_features_enabled is False
    assert config.purpose == "school-facts-f5-synthetic"
    assert config.reader.schema_contract == "school-facts-f5-v41"
    assert config.model.stage_budget.stage_id == "school-facts-f5"
    data["model"]["stage_budget"]["stage_id"] = "school-facts-f4"
    with pytest.raises(ValidationError):
        settings.SchoolFactProductConfiguration.model_validate_json(json.dumps(data))
    data = f5_document(tmp_path) | {"f5_features_enabled": "false"}
    with pytest.raises(ValidationError):
        settings.SchoolFactProductConfiguration.model_validate_json(json.dumps(data))


def test_rollback_rejects_new_f5_canonical_changes_but_retains_f4():
    check = getattr(settings, "require_product_fact_features", None)
    assert check is not None
    config = SimpleNamespace(f5_features_enabled=False)

    def plan(kind, **fields):
        return SimpleNamespace(
            changes=(
                SimpleNamespace(
                    fact_kind=kind, content=SimpleNamespace(**fields), replacement_segments=()
                ),
            )
        )

    check(config, plan("education_class"))
    for change in [
        plan("teaching_course"),
        plan("fact_quality", target_fact_kind="teaching_assignment"),
        plan("policy_scope", education_target_kind="teaching_offering"),
    ]:
        with pytest.raises(PermissionError):
            check(config, change)
    check(SimpleNamespace(f5_features_enabled=True), plan("teaching_course"))


def test_rollback_http_catalog_and_direct_read_are_f4_only():
    calls = []
    actor = _actor()
    views = SimpleNamespace(
        describe=lambda **kw: calls.append(kw) or {"ok": True},
        query=lambda **kw: calls.append(kw) or {"ok": True},
    )
    app = FastAPI()
    app.add_exception_handler(ApiError, api_error_handler)
    app.middleware("http")(assign_request_id)
    app.include_router(
        create_education_product_router(
            identity_dependency=lambda: SimpleNamespace(actor=actor),
            query=views,
            product=SimpleNamespace(),
            education_query=SimpleNamespace(),
            teaching_enabled=False,
        )
    )
    client = TestClient(app)
    base = "/api/v1/school-fact-product/education-facts"
    catalog = client.get(base + "/catalog").json()
    assert "education_class" in {row["value"] for row in catalog["families"]}
    assert "teaching_course" not in {row["value"] for row in catalog["families"]}
    assert client.get(base + "/describe/teaching_course").status_code >= 400
    assert client.post(base + "/query", json={"fact_kind": "teaching_course"}).status_code >= 400
    assert calls == []
    assert client.get(base + "/describe/education_class").status_code == 200


def test_product_wires_feature_switch_to_routes_tools_and_preflight():
    from pathlib import Path

    source = (Path(__file__).parents[2] / "app/composition/school_fact_product.py").read_text(
        encoding="utf-8"
    )
    assert "configuration.f5_features_enabled" in source
    assert "require_product_fact_features(configuration, change_set)" in source
    assert '"f5_features_enabled"' in source


def test_real_product_factory_disables_f5_but_retains_f3_f4_and_receipts(monkeypatch, tmp_path):
    from unittest.mock import MagicMock

    from pydantic import SecretStr

    from app.composition import school_fact_pydanticai as supported
    from tests.assistant.test_pydanticai_product_adapter import _product_factory_environment
    from tests.test_f3_product_identity import guardian

    product, _, router, products, _, _ = _product_factory_environment(monkeypatch, tmp_path)
    config = settings.SchoolFactProductConfiguration.model_validate_json(
        json.dumps(f5_document(tmp_path) | {"f5_features_enabled": False})
    )
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
    app = product.create_school_fact_product_app()
    paths = app.openapi()["paths"]
    assert not any("/teaching" in path or "/leadership" in path for path in paths)
    assert "/api/v1/school-fact-product/education-facts/lookup" in paths
    assert any("history-receipts" in path for path in paths)
    runner = router.call_args.kwargs["turn_orchestrator"]
    parent_names = {d.name for d in runner.runner.gateway_factory("parent").descriptors}
    assert "school_facts.family.education" in parent_names
    assert not any("teaching" in name or "leadership" in name for name in parent_names)
    identity.resolve_private_actor.return_value = SimpleNamespace(
        actor=_actor(), session_binding="staff"
    )
    staff = runner.runner.gateway_factory("staff").descriptors
    assert "school_facts.education.query" in {d.name for d in staff}
    assert not any("teaching" in d.name for d in staff)
    description = next(d for d in staff if d.name == "school_facts.education.describe")
    assert "teaching_course" not in json.dumps(description.parameters)
    for factory_call in products.call_args_list:
        guard = factory_call.kwargs["require_maintenance"]
        with pytest.raises(PermissionError):
            guard(
                _actor(), SimpleNamespace(changes=(SimpleNamespace(fact_kind="teaching_course"),))
            )
    assert TestClient(app).get("/api/v1/school-fact-product/features").json() == {
        "f5_features_enabled": False
    }
    permit.reserve.assert_not_called()


def test_disabled_review_commands_keep_existing_result_lookup():
    from contextlib import contextmanager
    from datetime import UTC, datetime
    from unittest.mock import MagicMock
    from uuid import uuid4

    from app.composition.school_fact_family_correction_writer import (
        SqlFamilyCorrectionExecutorKernel,
    )
    from app.domains.students.family_corrections import FamilyCorrectionDenied
    from app.platform.identity.execution_context import compose_command_context

    visits = []

    @contextmanager
    def transaction(*_):
        visits.append("transaction")
        yield MagicMock()

    report = uuid4()
    service = MagicMock()
    codec = MagicMock()
    codec.decode.return_value = {"report_id": str(report), "action": "maintain"}
    kernel = SqlFamilyCorrectionExecutorKernel(
        transaction=transaction,
        services=lambda *_: service,
        guardian_authority=MagicMock(),
        review_codec=codec,
        review_enabled=True,
        review_commands_enabled=False,
        clock=lambda: datetime.now(UTC),
    )
    context = compose_command_context(_actor(), uuid4(), "rollback-review")
    with pytest.raises(FamilyCorrectionDenied):
        kernel.prepare_review(
            context=context,
            session_binding="session",
            report_id=report,
            explanation="Review",
            required_action="maintain",
        )
    with pytest.raises(FamilyCorrectionDenied):
        kernel.confirm_review(
            context=context,
            session_binding="session",
            report_id=report,
            confirmation_token="token",
            confirmed=True,
        )
    assert visits == []
    kernel.review_result(
        context=context, session_binding="session", report_id=report, confirmation_token="token"
    )
    assert visits == ["transaction"]
    service.review_result.assert_called_once()


def test_writer_feature_disable_requires_schema35_and_keeps_guards():
    from app.composition.school_fact_product_writer import require_f5_writer_schema

    configuration = SimpleNamespace(
        education=None,
        f5_features_enabled=False,
        reader=SimpleNamespace(schema_contract="school-facts-f4-v31"),
        writer=SimpleNamespace(schema_contract="school-facts-f4-v31"),
    )
    with pytest.raises(ValueError):
        require_f5_writer_schema(configuration)
    configuration.reader.schema_contract = configuration.writer.schema_contract = (
        "school-facts-f5-v35"
    )
    require_f5_writer_schema(configuration)


def test_disabled_guardian_authority_rejects_prior_f5_reference_without_f5_sql():
    from datetime import UTC, datetime
    from unittest.mock import MagicMock

    from app.composition.school_fact_family_correction_writer import SqlGuardianCorrectionAuthority
    from app.domains.students.family_corrections import FamilyCorrectionDenied
    from tests.test_f3_product_identity import guardian

    session, codec = MagicMock(), MagicMock()
    session.execute.return_value.mappings.return_value.all.return_value = []
    codec.decode.return_value = ("teaching_assignment", "owned:assignment", "role", 1)
    authority = SqlGuardianCorrectionAuthority(session=session, codec=codec, f5_enabled=False)
    actor = guardian()
    assert authority._extra_rows(actor) == []
    session.execute.assert_not_called()
    with pytest.raises(FamilyCorrectionDenied, match="no longer current"):
        authority.require_submit(
            actor=actor,
            target_reference="prior-f5-reference",
            field_name="role",
            checked_at=datetime.now(UTC),
        )
    assert session.execute.call_count == 1
    assert "family_correction_targets" in str(session.execute.call_args.args[0])
