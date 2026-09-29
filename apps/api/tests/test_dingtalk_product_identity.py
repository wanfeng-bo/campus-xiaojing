from importlib.util import find_spec
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.composition.school_fact_product_identity import ProductIdentityRejected
from app.interfaces.http.school_fact_dingtalk_identity import SchoolFactDingTalkIdentityHttp
from app.platform.identity.context import CurrentUser
from app.platform.identity.models import User
from app.platform.identity.session import IdentitySessionStore
from tests.test_f1_product_identity import MemoryRedis, _service


def test_formal_product_identity_is_separate_from_engineering_entry():
    assert find_spec("app.composition.school_fact_dingtalk_identity") is not None


def test_formal_product_http_is_separate_from_loopback_entry():
    assert find_spec("app.interfaces.http.school_fact_dingtalk_identity") is not None


def formal_fixture(provider="dingtalk", tenant="target"):
    from sqlalchemy.pool import StaticPool

    from app.composition.school_fact_dingtalk_identity import (
        build_school_fact_dingtalk_identity_service,
    )

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    User.__table__.create(engine)
    factory = sessionmaker(engine)
    with factory() as session:
        session.add(
            User(
                id=7,
                provider_code=provider,
                provider_tenant_key=tenant,
                external_subject="subject",
                display_name="本人",
                is_active=True,
                security_version=1,
            )
        )
        session.commit()
    redis = MemoryRedis()
    settings = SimpleNamespace(
        identity_provider="dingtalk",
        dingtalk_corp_id="target",
        identity_session_ttl_seconds=28800,
        identity_session_cookie_name="school_fact_dingtalk_session",
    )
    service = build_school_fact_dingtalk_identity_service(
        settings=settings,
        session_factory=factory,
        redis=redis,
        proof_signer=None,
        can_maintain=lambda _: False,
        family_enabled=True,
    )
    token = IdentitySessionStore(redis=redis, ttl_seconds=28800).create(
        CurrentUser(7, "subject", "本人", 1)
    )
    return service, token, factory


@pytest.mark.parametrize("provider,tenant", [("local_test", "target"), ("dingtalk", "other")])
def test_formal_session_rejects_wrong_provider_or_tenant(provider, tenant):
    service, token, _ = formal_fixture(provider, tenant)
    with pytest.raises(ProductIdentityRejected):
        service._complete(token)


def test_formal_session_checks_current_account_security_version():
    service, token, factory = formal_fixture()
    assert service._complete(token)[0].id == 7
    with factory() as session:
        session.get(User, 7).security_version += 1
        session.commit()
    with pytest.raises(ProductIdentityRejected):
        service._complete(token)


def test_formal_single_context_auto_selects_and_logout_invalidates_private_ref():
    service, token, _ = formal_fixture()
    engineering, _, _ = _service()
    service._resolver_factory = engineering._resolver_factory
    factory = service._resolver_factory

    def sole(user, identity):
        resolver = factory(user, identity)
        original = resolver.list_options
        resolver.list_options = lambda **kw: original(**kw)[:1]
        return resolver

    service._resolver_factory = sole
    view = service.session_view(token)
    assert view.accounts == ()
    assert view.selected_context_ref == "A" * 43
    private = service.resolve_request(token).actor_context_ref
    service.logout(token)
    with pytest.raises(ProductIdentityRejected):
        service.resolve_private_actor(private)


def test_formal_multiple_staff_contexts_require_explicit_selection():
    service, token, _ = formal_fixture()
    engineering, _, _ = _service()
    service._resolver_factory = engineering._resolver_factory
    assert service.session_view(token).selected_context_ref is None
    assert not hasattr(service, "login")


def http_client(*, trusted_proxy_cidrs=(), peer="203.0.113.7", base_url="https://school.example"):
    service, _, _ = formal_fixture()
    settings = SimpleNamespace(identity_session_cookie_name="school_fact_dingtalk_session")
    adapter = SchoolFactDingTalkIdentityHttp(
        service=service,
        settings=settings,
        allowed_origin="https://school.example",
        trusted_proxy_cidrs=trusted_proxy_cidrs,
    )
    app = FastAPI()
    app.include_router(adapter.router)
    return TestClient(app, base_url=base_url, client=(peer, 50000)), adapter


def test_formal_http_has_no_account_selector_or_old_auth_entry():
    client, _ = http_client()
    assert client.get("/api/v1/school-fact-product/session").json()["accounts"] == []
    assert (
        client.post(
            "/api/v1/school-fact-product/login", json={"account_ref": "maintainer"}
        ).status_code
        == 404
    )
    assert (
        client.post("/api/v1/auth/dingtalk/exchange", json={"auth_code": "code"}).status_code == 404
    )


def test_formal_http_exact_origin_and_cookie_boundary():
    client, _ = http_client()
    base = "/api/v1/school-fact-product"
    assert client.post(base + "/logout").status_code == 403
    assert (
        client.post(base + "/logout", headers={"origin": "https://evil.example"}).status_code == 403
    )
    response = client.post(base + "/logout", headers={"origin": "https://school.example"})
    assert response.status_code == 200
    cookie = response.headers["set-cookie"]
    assert "school_fact_dingtalk_session=" in cookie
    assert "Secure" in cookie and "HttpOnly" in cookie and "Path=/" in cookie
    assert response.headers["cache-control"] == "no-store"
    assert (
        client.get(base + "/session", headers={"sec-fetch-site": "cross-site"}).status_code == 403
    )


def test_formal_proxy_headers_only_work_from_explicit_trusted_peer():
    headers = {"x-forwarded-proto": "https", "x-forwarded-host": "school.example"}
    client, _ = http_client(base_url="http://internal")
    assert client.get("/api/v1/school-fact-product/session", headers=headers).status_code == 403
    client, _ = http_client(
        base_url="http://internal", peer="10.4.0.2", trusted_proxy_cidrs=("10.4.0.2/32",)
    )
    assert client.get("/api/v1/school-fact-product/session", headers=headers).status_code == 200
    headers["x-forwarded-host"] = "evil.example"
    assert client.get("/api/v1/school-fact-product/session", headers=headers).status_code == 403


def test_formal_teacher_parent_is_explicit_and_family_auto_entry_keeps_both_children():
    from tests.test_f3_product_identity import service as family_service

    formal, token, _ = formal_fixture()
    engineering, _ = family_service()
    formal._resolver_factory = engineering._resolver_factory
    assert formal.session_view(token).selected_context_ref is None
    formal.select_context(token, "B" * 43)
    family = formal.resolve_assistant_request(token)
    assert len(formal.family_children(family.actor_context_ref)) == 2
    formal.select_context(token, "A" * 43)
    with pytest.raises(ProductIdentityRejected):
        formal.resolve_assistant_identity(family.actor_context_ref)


def test_formal_family_only_auto_entry_keeps_each_relationship_current():
    from tests.test_f3_product_identity import service as family_service

    formal, token, _ = formal_fixture()
    engineering, state = family_service()
    factory = engineering._resolver_factory

    def family_only(user, identity):
        resolver = factory(user, identity)
        original = resolver.list_options
        resolver.list_options = lambda **kw: tuple(
            o for o in original(**kw) if o.kind == "guardian_relationship"
        )
        return resolver

    formal._resolver_factory = family_only
    view = formal.session_view(token)
    assert view.family_conversation_available is True
    family = formal.resolve_assistant_request(token)
    children = formal.family_children(family.actor_context_ref)
    assert len(children) == 2
    state.ended = True
    with pytest.raises(ProductIdentityRejected):
        formal.resolve_family_child(family.actor_context_ref, children[0].child_ref)
    assert formal.resolve_family_child(family.actor_context_ref, children[1].child_ref)


def test_formal_exchange_uses_instance_resources_and_formal_context_not_user_roles(monkeypatch):
    from contextlib import nullcontext

    from app.integrations.dingtalk.client import DingTalkIdentity
    from app.interfaces.http.school_fact_dingtalk_identity import (
        install_dingtalk_identity_dependencies,
    )
    from app.platform.identity import router as auth

    service, old_token, factory = formal_fixture()
    engineering, _, _ = _service()
    service._resolver_factory = engineering._resolver_factory
    settings = SimpleNamespace(
        identity_session_cookie_name="school_fact_dingtalk_session",
        identity_session_ttl_seconds=28800,
        identity_provider="dingtalk",
        dingtalk_corp_id="target",
    )
    adapter = SchoolFactDingTalkIdentityHttp(
        service=service, settings=settings, allowed_origin="https://school.example"
    )
    app = FastAPI()

    @app.middleware("http")
    async def request_id(request, call_next):
        request.state.request_id = "synthetic-request"
        return await call_next(request)

    install_dingtalk_identity_dependencies(
        app, settings=settings, sessions=factory, redis=service._redis
    )
    app.dependency_overrides[auth.get_dingtalk_login_guard] = lambda: SimpleNamespace(
        admit=lambda _: nullcontext()
    )
    calls = []

    def verify(code):
        calls.append(code)
        return DingTalkIdentity(
            corporation_id="target", personnel_id="subject", display_name="本人"
        )

    app.dependency_overrides[auth.get_dingtalk_identity_verifier] = lambda: SimpleNamespace(
        verify=verify, fetch_optional_mobile=lambda _: None
    )
    monkeypatch.setattr(
        auth, "find_or_create_dingtalk_user", lambda session, **_: session.get(User, 7)
    )

    def forbidden(*args, **kwargs):
        raise AssertionError("must not use global resources or legacy UserRole")

    monkeypatch.setattr(auth, "determine_access_status", forbidden)
    monkeypatch.setattr(auth, "get_redis_client", forbidden)
    app.include_router(adapter.router)
    client = TestClient(app, base_url="https://school.example")
    client.cookies.set(settings.identity_session_cookie_name, old_token)
    path = "/api/v1/school-fact-product/dingtalk/exchange"
    assert (
        client.post(
            path, json={"auth_code": "forbidden"}, headers={"origin": "https://evil.example"}
        ).status_code
        == 403
    )
    assert calls == []
    response = client.post(
        path, json={"auth_code": "one-time"}, headers={"origin": "https://school.example"}
    )
    assert response.status_code == 200
    assert response.json()["status"] == "signed_in"
    assert response.json()["accounts"] == []
    assert len(response.json()["contexts"]) == 2
    assert "Secure" in response.headers["set-cookie"]
    assert calls == ["one-time"]
    with pytest.raises(ProductIdentityRejected):
        service._complete(old_token)
