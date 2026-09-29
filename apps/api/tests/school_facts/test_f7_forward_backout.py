"""Schema-compatible F7 package shutoff leaves the older fact product alive."""

import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError

from tests.school_facts.test_f7_startup_contract import f7_document


def test_f7_package_shutoff_keeps_authorized_original_result_after_plan_loss():
    from app.composition.school_facts import InMemorySchoolFactPlanStore
    from tests.school_facts.test_f7_package_product_boundary import (
        PREFIX,
        fixture_preview,
        set_identity,
        upload,
    )
    from tests.school_facts.test_sf3_http import _http_client

    options = dict(
        enable_package=True,
        package_only=True,
        authorize_package=lambda *_: None,
        authorize_package_result=lambda _: lambda: None,
        package_replacement=SimpleNamespace(preview=fixture_preview),
    )
    with _http_client(**options) as (client, identities, writer):
        set_identity(identities)
        planned = client.post(PREFIX + "/plans/package", json=upload(client))
        assert planned.status_code == 200, planned.text
        path = PREFIX + f"/plans/{planned.json()['plan_ref']}"
        prepared = client.post(path + "/confirmation", headers={"Idempotency-Key": "prepare"})
        assert prepared.status_code == 200, prepared.text
        applied = client.post(
            path + "/confirm",
            json={"credential": prepared.json()["credential"]},
            headers={"Idempotency-Key": "confirm"},
        )
        assert applied.status_code == 200, applied.text
        assert writer.confirm_calls == 1

    # Rebuild the router with no source or plan cache, as after an app restart.
    with _http_client(
        **options,
        writer=writer,
        plan_store=InMemorySchoolFactPlanStore(),
        result_only=True,
    ) as (client, identities, _):
        set_identity(identities)
        result = client.get(path + "/result")
        assert result.status_code == 200, result.text
        assert result.json()["state"] == "applied"
        assert client.post(PREFIX + "/sources", json={}).status_code == 404
        assert client.post(path + "/confirmation").status_code == 404
        assert client.post(path + "/confirm").status_code == 404
        assert writer.confirm_calls == 1
        identities["current"] = replace(identities["current"], session_binding="other-session")
        assert client.get(path + "/result").status_code == 404
        identities["current"] = replace(identities["current"], authorized_actions=frozenset())
        assert client.get(path + "/result").status_code == 403


def test_f7_product_shutoff_requires_explicit_strict_boolean(tmp_path):
    from app.composition.school_fact_product_settings import SchoolFactProductConfiguration

    data = f7_document(tmp_path)
    data["f7_packages_enabled"] = False
    config = SchoolFactProductConfiguration.model_validate_json(json.dumps(data))
    assert config.f7_packages_enabled is False
    assert config.runtime.postgres_schema_contract == "school-facts-f7-v55"
    data["runtime"]["postgres_schema_contract"] = "school-facts-f7-v52"
    data["reader"]["schema_contract"] = "school-facts-f7-v52"
    assert (
        SchoolFactProductConfiguration.model_validate_json(json.dumps(data)).f7_packages_enabled
        is False
    )
    data["f7_packages_enabled"] = "false"
    with pytest.raises(ValidationError):
        SchoolFactProductConfiguration.model_validate_json(json.dumps(data))


def test_f7_product_shutoff_excludes_packages_but_retains_earlier_routes(monkeypatch, tmp_path):
    from app.composition import school_fact_pydanticai as supported
    from app.composition.school_fact_product_settings import SchoolFactProductConfiguration
    from tests.assistant.test_pydanticai_product_adapter import _product_factory_environment

    product, _, _, _, _, _ = _product_factory_environment(monkeypatch, tmp_path)
    config = SchoolFactProductConfiguration.model_validate_json(
        json.dumps(f7_document(tmp_path) | {"f7_packages_enabled": False})
    )
    monkeypatch.setattr(product, "load_school_fact_product_configuration", lambda: config)
    monkeypatch.setattr(supported, "load_product_model_api_key", lambda _: SecretStr("unit-only"))
    monkeypatch.setattr(supported, "SqliteCurrentModelCallPermit", lambda **_: MagicMock())
    app = product.create_school_fact_product_app()
    paths = app.openapi()["paths"]
    result_path = "/api/v1/school-fact-product/imports/plans/{plan_ref}/result"
    assert {path for path in paths if "/imports/" in path} == {result_path}
    assert set(paths[result_path]) == {"get"}
    assert any("/facts/" in path for path in paths)
    assert "/api/v1/school-fact-product/family-facts/prepare" in paths
    assert "/api/v1/school-fact-product/education-facts/prepare" in paths
    assert any("/education-facts/" in path for path in paths)
    assert any("/residence-facts/" in path for path in paths)
    assert any("history-receipts" in path for path in paths)
    with TestClient(app) as client:
        assert (
            client.get("/api/v1/school-fact-product/features").json()["f7_packages_enabled"]
            is False
        )
        response = client.post("/api/v1/school-fact-product/imports/sources", json={})
        assert response.status_code == 404


def test_f7_writer_shutoff_denies_new_packages_without_disabling_old_guards(monkeypatch, tmp_path):
    from app.composition import school_fact_product_writer as writer
    from app.platform.school_facts.application import SchoolFactChangeRejected

    data = f7_document(tmp_path)
    configuration = {
        key: data[key]
        for key in ("reader", "scope_id", "directory", "family", "education", "residence")
    }
    configuration["writer"] = {
        key: value for key, value in data["reader"].items() if key != "cursor_hmac_key"
    } | {
        "database_password": "writer-unit-only-" * 3,
        "confirmation_hmac_key": "confirm-unit-only-" * 3,
        "caller_auth_key": "caller-unit-only-" * 3,
        "context_transport_hmac_key": "transport-unit-only-" * 3,
        "rls_context_signing_key": "context-unit-only-" * 3,
    }
    configuration["f7_packages_enabled"] = False
    path = tmp_path / "writer-synthetic.json"
    path.write_text(json.dumps(configuration), encoding="utf-8")
    monkeypatch.setenv("SCHOOL_FACT_F1_WRITER_CONFIG_FILE", str(path))
    reader_engine, writer_engine = MagicMock(), MagicMock()
    monkeypatch.setattr(writer, "build_school_fact_reader_engine", lambda _: reader_engine)
    monkeypatch.setattr(writer, "build_school_fact_writer_engine", lambda _: writer_engine)
    monkeypatch.setattr(writer, "verify_school_fact_reader_database", lambda *a, **kw: None)
    monkeypatch.setattr(writer, "verify_school_fact_writer_database", lambda *a, **kw: None)
    kernel_factory = writer.SqlAlchemySchoolFactWriterExecutorKernel
    captured = []

    def make_kernel(**kwargs):
        captured.append(kwargs)
        return kernel_factory(**kwargs)

    monkeypatch.setattr(writer, "SqlAlchemySchoolFactWriterExecutorKernel", make_kernel)
    app = writer.create_school_fact_product_writer_app()
    with TestClient(app) as client:
        assert client.get("/internal/v1/school-facts/features").json() == {
            "schema_contract": "school-facts-f7-v55",
            "f7_packages_enabled": False,
        }
    kwargs = captured[0]
    assert kwargs["final_authorization_guard"] is not None
    with pytest.raises(SchoolFactChangeRejected, match="全范围"):
        kwargs["package_final_authorization_guard"](None, None, None)
    reader_engine.dispose.assert_called_once()
    writer_engine.dispose.assert_called_once()
