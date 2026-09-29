"""Real product assembly must keep its identity, resources and data boundary together."""

import json
from unittest.mock import MagicMock

from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.composition.school_fact_live_settings import LiveSchoolFactProductConfiguration
from tests.assistant.test_pydanticai_product_adapter import _product_factory_environment
from tests.assistant.test_school_fact_live_settings import live_document


def test_disabled_formal_factory_never_loads_model_secrets_or_constructs_any_loop(
    monkeypatch, tmp_path,
):
    from app.composition import school_fact_live_settings as live_settings
    from app.composition import school_fact_pydanticai as supported
    from tests.assistant.test_school_fact_live_settings import direct_only_live_document

    product, _, assistant_router, _, _, _ = _product_factory_environment(monkeypatch, tmp_path)
    config = LiveSchoolFactProductConfiguration.model_validate_json(
        json.dumps(direct_only_live_document(tmp_path))
    )
    monkeypatch.setattr(
        product, "build_school_fact_dingtalk_identity_service", lambda **_: MagicMock()
    )
    monkeypatch.setattr(product, "create_family_query_service", lambda **_: MagicMock())
    monkeypatch.setattr(product, "create_family_staff_components", lambda **_: MagicMock())
    forbidden = MagicMock(side_effect=AssertionError("disabled model construction"))
    monkeypatch.setattr(product, "load_product_model_api_key", forbidden)
    monkeypatch.setattr(product, "build_product_model", forbidden)
    monkeypatch.setattr(product, "AssistantToolLoop", forbidden)
    monkeypatch.setattr(supported, "load_product_model_api_key", forbidden)
    monkeypatch.setattr(supported, "build_pydanticai_loop", forbidden)
    monkeypatch.setattr(supported, "build_live_model_call_permit", forbidden)
    monkeypatch.setattr(live_settings, "SqliteModelCallPermit", forbidden)
    monkeypatch.setattr(live_settings, "SqliteCurrentModelCallPermit", forbidden)
    app = product.create_school_fact_product_app(
        configuration=config, assistant_loop_factory=forbidden,
    )
    with TestClient(app, base_url=config.allowed_origin) as client:
        features = client.get("/api/v1/school-fact-product/features").json()
        assert features == {
            "assistant_enabled": False,
            "f5_features_enabled": True,
            "f6_features_enabled": True,
            "f7_packages_enabled": True,
        }
    forbidden.assert_not_called()
    assistant_router.assert_not_called()
    assert list(tmp_path.rglob("*.sqlite3")) == []
    paths = app.openapi()["paths"]
    assert "/api/v1/school-fact-product/dingtalk/exchange" in paths
    assert "/api/v1/school-fact-product/login" not in paths
    assert "/api/v1/school-fact-product/facts/query" in paths
    assert "/api/v1/school-fact-product/facts/proposals/{proposal_ref}/confirm" in paths
    assert "/api/v1/school-fact-product/facts/receipts/{proposal_ref}" in paths


def test_disabled_formal_http_keeps_identity_direct_reads_and_confirmation_receipts(
    monkeypatch, tmp_path,
):
    from tests.assistant.test_school_fact_live_settings import direct_only_live_document
    from tests.test_dingtalk_product_identity import formal_fixture
    from tests.test_f1_product_identity import _service
    from tests.test_f2_directory_product import setup

    product, _, _, make_directory, _, _ = _product_factory_environment(monkeypatch, tmp_path)
    config = LiveSchoolFactProductConfiguration.model_validate_json(
        json.dumps(direct_only_live_document(tmp_path))
    )
    identity, token, _ = formal_fixture()
    identity._resolver_factory = _service()[0]._resolver_factory
    monkeypatch.setattr(
        product, "build_school_fact_dingtalk_identity_service", lambda **_: identity
    )
    monkeypatch.setattr(product, "create_family_query_service", lambda **_: MagicMock())
    monkeypatch.setattr(product, "create_family_staff_components", lambda **_: MagicMock())
    # Real HTTP/session and domain service; synthetic reader/Writer boundary, no cloud proof.
    fact_service, boundary, allowed, change = setup()
    first = [True]

    def make_product(**kwargs):
        if first[0]:
            first[0] = False
            return fact_service
        return make_directory(**kwargs)

    monkeypatch.setattr(product, "DirectoryFactProductService", make_product)
    reader = MagicMock()
    reader.query.return_value = {"records": [{"label": "已授权合成资料"}]}
    monkeypatch.setattr(product, "DirectoryProductAdapter", lambda *_a, **_kw: reader)
    app = product.create_school_fact_product_app(configuration=config)
    base = "/api/v1/school-fact-product"
    assistant = "/api/v1/assistant/conversations"
    with TestClient(app, base_url=config.allowed_origin) as client:
        headers = {"Origin": config.allowed_origin}
        assert client.post(assistant, headers=headers).status_code == 401
        client.cookies.set(config.runtime.identity_session_cookie_name, token)
        assert client.get(base + "/session").json()["status"] == "signed_in"
        selected = client.post(base + "/context", headers=headers, json={"context_ref": "A" * 43})
        assert selected.status_code == 200
        assert client.post(assistant).status_code == 403
        unavailable = client.post(assistant, headers=headers)
        assert unavailable.status_code == 503
        assert unavailable.json()["code"] == "assistant.not_enabled"
        assert "尚未启用" in unavailable.json()["message"]
        query = client.post(base + "/facts/query", headers=headers, json={"fact_kind": "person"})
        assert query.status_code == 200
        assert query.json() == reader.query.return_value
        assert reader.query.call_args.kwargs["actor"] == identity.resolve_request(token).actor
        prepared = client.post(
            base + "/facts/prepare", headers=headers, json=change.model_dump(mode="json")
        )
        assert prepared.status_code == 200
        proposal_ref = prepared.json()["proposal_ref"]
        confirm = base + f"/facts/proposals/{proposal_ref}/confirm"
        confirmed = client.post(confirm, headers=headers)
        assert confirmed.status_code == 200
        assert confirmed.json()["status"] == "succeeded"
        original = client.get(base + f"/facts/receipts/{proposal_ref}")
        assert original.status_code == 200
        assert original.json()["status"] == "succeeded"
        assert sum(name == "confirm" for name, _ in boundary.calls) == 1
        allowed[0] = False
        assert client.post(confirm, headers=headers).status_code == 403
        assert sum(name == "confirm" for name, _ in boundary.calls) == 1
        assert client.post(base + "/logout", headers=headers).status_code == 200
        # Replaying the previously valid cookie cannot recover either entry.
        client.cookies.set(config.runtime.identity_session_cookie_name, token)
        assert client.post(assistant, headers=headers).status_code == 401
        assert client.post(
            base + "/facts/query", headers=headers, json={"fact_kind": "person"}
        ).status_code == 401
        assert reader.query.call_count == 1


def test_formal_factory_uses_dingtalk_and_keeps_complete_fact_product(monkeypatch, tmp_path):
    product, _, _, _, _, resources = _product_factory_environment(monkeypatch, tmp_path)
    config = LiveSchoolFactProductConfiguration.model_validate_json(
        json.dumps(live_document(tmp_path))
    )
    identity = MagicMock()
    engineering = MagicMock(side_effect=AssertionError("engineering identity on public entry"))
    formal = MagicMock(return_value=identity)
    monkeypatch.setattr(product, "build_school_fact_product_identity_service", engineering)
    monkeypatch.setattr(
        product, "build_school_fact_dingtalk_identity_service", formal, raising=False
    )
    monkeypatch.setattr(product, "create_family_query_service", lambda **_: MagicMock())
    monkeypatch.setattr(product, "create_family_staff_components", lambda **_: MagicMock())
    make_loop = MagicMock()
    monkeypatch.setattr(product, "load_product_model_api_key", lambda _: SecretStr("fake-key"))
    app = product.create_school_fact_product_app(
        configuration=config, assistant_loop_factory=make_loop,
    )
    paths = app.openapi()["paths"]
    assert "/api/v1/school-fact-product/dingtalk/exchange" in paths
    assert "/api/v1/school-fact-product/login" not in paths
    assert "/api/v1/school-fact-product/imports/plans/package" in paths
    assert "/api/v1/school-fact-product/family/profile" in paths
    assert app.version == "live-v1"
    engineering.assert_not_called()
    assert formal.call_args.kwargs["settings"] is config.runtime
    assert formal.call_args.kwargs["redis"] is resources[2]
    gateway = make_loop.call_args.kwargs["gateway_factory"]("formal-session")
    assert type(gateway).__module__ == "app.composition.school_fact_live_model_policy"
    from app.core.settings import get_settings
    from app.platform.identity.router import get_identity_redis
    assert app.dependency_overrides[get_settings]() is config.runtime
    assert app.dependency_overrides[get_identity_redis]() is resources[2]
    with TestClient(app, base_url=config.allowed_origin) as client:
        assert client.get("/api/v1/school-fact-product/features").json() == {
            "assistant_enabled": True,
            "f5_features_enabled": True,
            "f6_features_enabled": True,
            "f7_packages_enabled": True,
        }
    resources[0].dispose.assert_called_once()
    resources[1].close.assert_called_once()
    resources[2].close.assert_called_once()


def test_public_factory_never_loads_engineering_configuration(monkeypatch):
    from app.composition import school_fact_live_product as live

    configuration = object()
    sentinel = object()
    monkeypatch.setattr(live, "load_school_fact_live_configuration", lambda: configuration)
    build = MagicMock(return_value=sentinel)
    monkeypatch.setattr(live, "create_school_fact_product_app", build)
    assert live.create_app() is sentinel
    build.assert_called_once_with(configuration=configuration)


def test_formal_and_engineering_instructions_keep_distinct_material_policies():
    from app.composition import school_fact_product as product

    assert "当前仅使用获批合成资料" in product.F1_ASSISTANT_INSTRUCTION
    assert "当前仅使用获批合成资料" not in product.LIVE_ASSISTANT_INSTRUCTION
    assert "当前学校" in product.LIVE_ASSISTANT_INSTRUCTION
