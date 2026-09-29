"""Actual F7 OFF Product/Writer assembly against the explicitly selected DB contract.

The isolated Redis and test-only model key are never connected to the original
Product, Writer, database, ledger or external model service. Formal old fact and
own-receipt positives are separate real-PG cases in the bounded OFF probe.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from uuid import UUID

import psycopg
import pytest
from fastapi.testclient import TestClient

from app.platform.school_facts.application import SchoolFactChangeRejected
from tests.school_facts.test_f7_startup_contract import (
    F7_PROBE_CONTRACTS, f7_document, f7_probe_contract,
)

pytestmark = pytest.mark.skipif(
    not os.environ.get("F7_TEST_DATABASE_CONFIG")
    or not os.environ.get("F7_TEST_REDIS_HOST"),
    reason="owned F7 PostgreSQL and Redis are required",
)


def _test_configuration(tmp_path: Path) -> tuple[dict, dict]:
    config = json.loads(Path(os.environ["F7_TEST_DATABASE_CONFIG"]).read_text(encoding="utf-8"))
    assert config["owner"] == "F7 isolated quality migration and reader tests"
    assert config["container"] == "campus-f7-tests-20260924"
    assert config["database"] == "f7_fixture_test"
    with psycopg.connect(
        host=config["host"], port=config["port"], dbname=config["database"],
        user="campus_ai_migration", password=config["migration"],
        options="-c role=campus_ai_db_owner", connect_timeout=3,
    ) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT version_num FROM alembic_version")
            assert cursor.fetchone()[0] == F7_PROBE_CONTRACTS[f7_probe_contract()]
            cursor.execute("SELECT anchor_id,school_id FROM school_database_anchor")
            anchor = cursor.fetchone()
            assert anchor is not None
            cursor.execute("SELECT system_identifier FROM pg_control_system()")
            assert str(cursor.fetchone()[0]) == str(config["system_identifier"])
    document = f7_document(tmp_path, contract=f7_probe_contract())
    runtime, reader, model = document["runtime"], document["reader"], document["model"]
    runtime.update(
        postgres_host=config["host"], postgres_port=config["port"],
        postgres_db=config["database"], postgres_user="campus_ai_runtime",
        postgres_password=config["runtime"],
        postgres_expected_anchor_id=str(anchor[0]),
        postgres_expected_school_id=str(anchor[1]),
        postgres_expected_system_identifier=str(config["system_identifier"]),
        rls_context_signing_key=config["context"],
        redis_host=os.environ["F7_TEST_REDIS_HOST"],
        redis_port=6379,
        redis_password=os.environ["F7_TEST_REDIS_PASSWORD"],
    )
    reader.update(
        database_host=config["host"], database_port=config["port"],
        database_name=config["database"], database_password=config["reader"],
        expected_anchor_id=str(anchor[0]), expected_school_id=str(anchor[1]),
        expected_system_identifier=str(config["system_identifier"]),
    )
    # This key has no provider validity and no model request is made in the test.
    model_key = tmp_path / "isolated-non-provider-key"
    model_key.write_text("isolated-off-test-only", encoding="ascii")
    model["api_key_file"] = str(model_key)
    model["state_directory"] = str(tmp_path)
    from app.composition.school_fact_model_budget import ApprovedModelBudget, SqliteModelCallPermit

    SqliteModelCallPermit(
        path=tmp_path / "model-budget.sqlite3",
        policy=ApprovedModelBudget.model_validate_json(json.dumps(model["budget"])),
    )
    document["f7_packages_enabled"] = False
    writer = {
        key: document[key]
        for key in ("reader", "scope_id", "directory", "family", "education", "residence")
    }
    writer["writer"] = {
        key: value for key, value in reader.items() if key != "cursor_hmac_key"
    } | {
        "database_user": "campus_ai_school_fact_writer",
        "database_password": config["writer"],
        "confirmation_hmac_key": "isolated-confirmation-only-" * 2,
        "caller_auth_key": document["executor"]["caller_auth_key"],
        "context_transport_hmac_key": document["executor"]["context_transport_hmac_key"],
        "rls_context_signing_key": config["context"],
    }
    writer["f7_packages_enabled"] = False
    return document, writer


def test_actual_off_product_writer_http_and_final_guard(tmp_path, monkeypatch):
    from app.composition import school_fact_product as product
    from app.composition import school_fact_product_writer as writer

    product_data, writer_data = _test_configuration(tmp_path)
    result_path = Path(os.environ["F7_OFF_HTTP_RESULT_PATH"])
    assert result_path.is_absolute() and result_path.parent.is_dir()
    completed: list[str] = []

    def mark(label: str) -> None:
        completed.append(label)
        result_path.write_text(json.dumps({"passed": completed}), encoding="utf-8")

    product_path, writer_path = tmp_path / "product.json", tmp_path / "writer.json"
    product_path.write_text(json.dumps(product_data), encoding="utf-8")
    writer_path.write_text(json.dumps(writer_data), encoding="utf-8")
    monkeypatch.setenv("SCHOOL_FACT_PRODUCT_CONFIG", str(product_path))
    monkeypatch.setenv("SCHOOL_FACT_F1_WRITER_CONFIG_FILE", str(writer_path))
    observed_kernels = []
    real_kernel = writer.SqlAlchemySchoolFactWriterExecutorKernel

    def observe_kernel(**kwargs):
        observed_kernels.append(kwargs)
        return real_kernel(**kwargs)

    monkeypatch.setattr(writer, "SqlAlchemySchoolFactWriterExecutorKernel", observe_kernel)
    with TestClient(product.create_school_fact_product_app()) as client:
        features = client.get("/api/v1/school-fact-product/features")
        assert features.status_code == 200
        assert features.json()["f7_packages_enabled"] is False
        mark("product_off_features_200")
        legacy_path = "/api/v1/school-fact-product/education-facts/lookup"
        assert legacy_path in client.app.openapi()["paths"]
        assert client.post(legacy_path, json={}).status_code in {401, 403, 422}
        mark("product_legacy_http_route_auth_denied")
        assert (
            client.post("/api/v1/school-fact-product/imports/sources", json={}).status_code
            == 404
        )
        mark("package_sources_http_404")
        plan = UUID(int=1)
        path = f"/api/v1/school-fact-product/imports/plans/{plan}"
        assert client.post(path + "/confirmation", json={}).status_code == 404
        mark("package_prepare_http_404")
        assert client.post(path + "/confirm", json={}).status_code == 404
        mark("package_confirm_http_404")
    with TestClient(writer.create_school_fact_product_writer_app()) as client:
        features = client.get("/internal/v1/school-facts/features")
        assert features.status_code == 200
        assert features.json() == {
            "schema_contract": f7_probe_contract(), "f7_packages_enabled": False,
        }
        mark("writer_off_features_200")
        assert "/internal/v1/school-facts/preflight" in client.app.openapi()["paths"]
        assert client.post("/internal/v1/school-facts/preflight", json={}).status_code == 401
        mark("writer_legacy_http_route_auth_denied")
    assert len(observed_kernels) == 1
    with pytest.raises(SchoolFactChangeRejected):
        observed_kernels[0]["package_final_authorization_guard"](None, None, None)
    mark("writer_package_final_guard_rejected")
