from __future__ import annotations

import importlib
import importlib.util
import json
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app.platform.identity.context import CurrentUser
from app.platform.identity.execution_context import ContextNoLongerValid, SelectedContextRef
from tests.test_f1_official_contact_query import _actor

ORIGIN = "http://127.0.0.1:8181"


def _api():
    name = "app.composition.school_fact_product_identity"
    assert importlib.util.find_spec(name) is not None, "missing controlled engineering identity"
    return importlib.import_module(name)


class MemoryRedis:
    def __init__(self):
        self.values = {}

    def setex(self, name, time, value):
        self.values[name] = value

    def get(self, name):
        return self.values.get(name)

    def eval(self, script, numkeys, key, session_key, expected, ttl, value):
        assert numkeys == 2
        if self.values.get(key, "") != expected or session_key not in self.values:
            return 0
        self.setex(key, ttl, value)
        return 1

    def delete(self, *names):
        for name in names:
            self.values.pop(name, None)


def _service():
    api = _api()
    redis = MemoryRedis()
    account = api.ENGINEERING_ACCOUNTS[0]
    state = SimpleNamespace(version=1, active=True, resolves=0)

    def load_account(selected):
        assert selected == account
        if not state.active:
            return None
        return CurrentUser(
            id=7,
            external_subject="f1-maintainer",
            display_name="合成工程维护员",
            security_version=state.version,
        )

    class Resolver:
        def list_options(self, *, exclude_selected=None):
            return tuple(
                SelectedContextRef(
                    opaque_ref=letter * 43,
                    kind="staff_assignment",
                    display_label=f"合成履职{letter}",
                    school_label="合成工程学校",
                    effective_until=datetime.now(UTC) + timedelta(minutes=15),
                )
                for letter in ("A", "B")
            )

        def resolve(self, ref):
            state.resolves += 1
            if ref not in ("A" * 43, "B" * 43):
                raise ContextNoLongerValid("invalid")
            return _actor()

        def resolve_selected(self, selected):
            state.resolves += 1
            return _actor()

    service = api.SchoolFactProductIdentityService(
        app_env="test",
        redis=redis,
        accounts=(account,),
        load_account=load_account,
        resolver_factory=lambda _user, _session: Resolver(),
        can_maintain=lambda _actor: True,
    )
    return service, redis, state


def test_engineering_identity_rejects_production_and_arbitrary_registration():
    api = _api()
    service, redis, _ = _service()
    with pytest.raises(api.ProductIdentityRejected):
        service.login("arbitrary-user")
    assert redis.values == {}
    with pytest.raises(ValueError, match="production"):
        api.SchoolFactProductIdentityService(
            app_env="production",
            redis=redis,
            accounts=api.ENGINEERING_ACCOUNTS,
            load_account=lambda _: None,
            resolver_factory=lambda _u, _s: None,
            can_maintain=lambda _: False,
        )


def test_real_session_store_explicit_context_and_private_refs_revalidate_and_invalidate():
    api = _api()
    service, redis, state = _service()
    token = service.login("maintainer")
    from app.platform.identity.session import IdentitySessionStore

    stored = IdentitySessionStore(redis=redis, ttl_seconds=28800).read(token)
    assert stored and stored.user_id == 7
    assert service.session_view(token).selected_context_ref is None
    with pytest.raises(api.ProductIdentityRejected):
        service.resolve_request(token)
    service.select_context(token, "A" * 43)
    first = service.resolve_request(token)
    assert first.actor == _actor()
    assert first.session_binding == stored.session_nonce_hash
    assert first.session_binding != token
    assert token not in repr(first)
    before = state.resolves
    assert service.resolve_private_actor(first.actor_context_ref).actor == _actor()
    assert state.resolves == before + 1
    service.select_context(token, "B" * 43)
    with pytest.raises(api.ProductIdentityRejected):
        service.resolve_private_actor(first.actor_context_ref)
    second = service.resolve_request(token)
    assert second.actor_context_ref != first.actor_context_ref
    service.logout(token)
    with pytest.raises(api.ProductIdentityRejected):
        service.resolve_private_actor(second.actor_context_ref)


def test_account_security_change_invalidates_selected_context_and_private_reference():
    api = _api()
    service, _, state = _service()
    token = service.login("maintainer")
    service.select_context(token, "A" * 43)
    identity = service.resolve_request(token)
    state.version += 1
    with pytest.raises(api.ProductIdentityRejected):
        service.resolve_private_actor(identity.actor_context_ref)


def test_private_identity_is_shared_only_inside_one_synchronous_release():
    from app.composition.school_fact_read_evidence import validation_batch

    service, _, state = _service()
    token = service.login("maintainer")
    service.select_context(token, "A" * 43)
    identity = service.resolve_request(token)
    state.resolves = 0
    with validation_batch():
        for _ in range(4):
            assert service.resolve_private_actor(identity.actor_context_ref) == identity
        with validation_batch():
            assert service.resolve_private_actor(identity.actor_context_ref) == identity
        assert state.resolves == 1
    assert state.resolves == 2
    with validation_batch():
        assert service.resolve_private_actor(identity.actor_context_ref) == identity
    assert state.resolves == 4
    service.resolve_private_actor(identity.actor_context_ref)
    service.resolve_private_actor(identity.actor_context_ref)
    assert state.resolves == 6


@pytest.mark.parametrize("change", ["security", "disabled", "logout", "context", "expiry"])
def test_private_identity_fresh_check_runs_after_all_content_checks(change):
    from app.composition.school_fact_read_evidence import validation_batch
    from app.platform.assistant.runtime import AssistantRuntimeError

    service, redis, state = _service()
    token = service.login("maintainer")
    service.select_context(token, "A" * 43)
    identity = service.resolve_request(token)

    def changed():
        if change == "security":
            state.version += 1
        elif change == "disabled":
            state.active = False
        elif change == "logout":
            service.logout(token)
        elif change == "context":
            service.select_context(token, "B" * 43)
        else:
            key = service._store._key(token)
            payload = json.loads(redis.values[key])
            payload["expires_at"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
            redis.values[key] = json.dumps(payload)

    with pytest.raises(AssistantRuntimeError):
        with validation_batch() as batch:
            service.resolve_private_actor(identity.actor_context_ref)
            # The final identity check must happen after the last content recheck.
            batch.content_rechecks["content"] = changed
    assert batch.identity_checks == {}
    with pytest.raises(_api().ProductIdentityRejected):
        service.resolve_private_actor(identity.actor_context_ref)


def test_private_identity_keeps_distinct_live_sessions_separate_in_one_batch():
    from app.composition.school_fact_read_evidence import validation_batch

    service, _, state = _service()
    identities = []
    for _ in range(2):
        token = service.login("maintainer")
        service.select_context(token, "A" * 43)
        identities.append(service.resolve_request(token))
    assert identities[0].session_binding != identities[1].session_binding
    state.resolves = 0
    with validation_batch():
        for identity in identities * 2:
            assert service.resolve_private_actor(identity.actor_context_ref) == identity
        assert state.resolves == 2
    assert state.resolves == 4


def test_private_identity_other_service_cannot_reuse_an_allowed_reference():
    from app.composition.school_fact_read_evidence import validation_batch

    first, redis, _ = _service()
    second, _, denied = _service()
    second._redis, second._store = redis, first._store
    token = first.login("maintainer")
    first.select_context(token, "A" * 43)
    identity = first.resolve_request(token)
    denied.active = False
    with validation_batch():
        assert first.resolve_private_actor(identity.actor_context_ref) == identity
        with pytest.raises(_api().ProductIdentityRejected):
            second.resolve_private_actor(identity.actor_context_ref)


def test_private_identity_failure_clears_batch_without_reusing_it_next_time():
    from app.composition.school_fact_read_evidence import validation_batch

    service, _, state = _service()
    token = service.login("maintainer")
    service.select_context(token, "A" * 43)
    identity = service.resolve_request(token)
    state.resolves = 0
    with pytest.raises(RuntimeError, match="synthetic content failure"):
        with validation_batch() as batch:
            service.resolve_private_actor(identity.actor_context_ref)
            raise RuntimeError("synthetic content failure")
    assert batch.identity_checks == {}
    assert state.resolves == 1
    with validation_batch():
        service.resolve_private_actor(identity.actor_context_ref)
    assert state.resolves == 3


@pytest.mark.parametrize("ref", [None, [], {}, "short"])
def test_invalid_private_reference_still_rejects_inside_a_batch(ref):
    from app.composition.school_fact_read_evidence import validation_batch

    service, _, state = _service()
    with validation_batch():
        with pytest.raises(_api().ProductIdentityRejected):
            service.resolve_private_actor(ref)
    assert state.resolves == 0


@pytest.mark.parametrize(
    "field",
    [
        "account_user_id",
        "school_id",
        "person_id",
        "assignment_id",
        "assignment_version",
        "authorization_version",
        "actor_context_ref",
    ],
)
def test_release_compares_every_staff_identity_field(field, monkeypatch):
    from app.composition.school_fact_read_evidence import validation_batch
    from app.platform.assistant.runtime import AssistantRuntimeError
    from app.platform.contracts import _issue_staff_actor_context

    service, _, _ = _service()
    token = service.login("maintainer")
    service.select_context(token, "A" * 43)
    original = service.resolve_request(token)
    if field == "actor_context_ref":
        changed = replace(original, actor_context_ref="C" * 43)
    else:
        value = getattr(original.actor, field)
        value = UUID(int=value.int + 10) if isinstance(value, UUID) else value + 1
        changed = replace(
            original, actor=_issue_staff_actor_context(**(asdict(original.actor) | {field: value}))
        )
    with pytest.raises(AssistantRuntimeError):
        with validation_batch():
            assert service.resolve_private_actor(original.actor_context_ref) == original
            monkeypatch.setattr(service, "resolve_request", lambda _: changed)


def test_unavailable_fresh_identity_fails_closed_and_clears_evidence(monkeypatch):
    from sqlalchemy.exc import SQLAlchemyError

    from app.composition.school_fact_read_evidence import validation_batch
    from app.platform.assistant.runtime import AssistantRuntimeError

    service, _, _ = _service()
    token = service.login("maintainer")
    service.select_context(token, "A" * 43)
    original = service.resolve_request(token)

    def unavailable(_):
        raise SQLAlchemyError("synthetic private diagnostic")

    with pytest.raises(AssistantRuntimeError) as caught:
        with validation_batch() as batch:
            service.resolve_private_actor(original.actor_context_ref)
            monkeypatch.setattr(service, "_load_account", unavailable)
    assert "synthetic private diagnostic" not in str(caught.value)
    assert batch.identity_checks == {}


def test_fact_clock_boundary_is_rechecked_after_final_identity_io(monkeypatch):
    from app.composition.school_fact_read_evidence import validation_batch
    from app.platform.assistant.runtime import AssistantRuntimeError
    from tests.test_f2_read_evidence import NOW, Backend, evidence

    service, _, _ = _service()
    token = service.login("maintainer")
    service.select_context(token, "A" * 43)
    original = service.resolve_request(token)
    backend = Backend()
    deadline = NOW + timedelta(seconds=1)
    proof = evidence(backend, deadline=deadline)
    resolve = service.resolve_request

    def delayed(token):
        result = resolve(token)
        backend.now = deadline
        return result

    with pytest.raises(AssistantRuntimeError, match="time boundary"):
        with validation_batch():
            service.resolve_private_actor(original.actor_context_ref)
            assert proof.check(proof.actor)
            monkeypatch.setattr(service, "resolve_request", delayed)


def _client(service, *, client_host="127.0.0.1", trusted_proxy_cidrs=()):
    name = "app.interfaces.http.school_fact_product_identity"
    assert importlib.util.find_spec(name) is not None, "missing isolated product identity HTTP"
    http = importlib.import_module(name).SchoolFactProductIdentityHttp(
        service=service, allowed_origin=ORIGIN, trusted_proxy_cidrs=trusted_proxy_cidrs
    )
    app = FastAPI()
    app.include_router(http.router)

    @app.get("/api/v1/assistant/test-identity")
    def assistant_identity(request: Request):
        http.request_dependency(request)
        return {"authorized": True}

    return TestClient(app, base_url=ORIGIN, client=(client_host, 5000)), http


def test_identity_http_cookie_and_public_contract_do_not_expose_private_context_or_ids():
    service, _, _ = _service()
    client, http = _client(service)
    base = "/api/v1/school-fact-product"
    assert client.get(base + "/session").json()["status"] == "signed_out"
    login = client.post(
        base + "/login", json={"account_ref": "maintainer"}, headers={"Origin": ORIGIN}
    )
    assert login.status_code == 200
    cookie = login.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=strict" in cookie
    assert "Path=/api/v1;" in cookie
    assert login.json()["selected_context_ref"] is None
    result = client.post(
        base + "/context", json={"context_ref": "A" * 43}, headers={"Origin": ORIGIN}
    )
    assert result.status_code == 200 and result.json()["can_maintain"]
    assert client.get("/api/v1/assistant/test-identity").json() == {"authorized": True}
    assert set(result.json()) == {
        "status",
        "accounts",
        "contexts",
        "selected_context_ref",
        "can_maintain",
        "directory_enabled",
        "education_enabled",
        "family_enabled",
        "selected_context_kind",
        "selection_transition_ref",
        "family_conversation_available",
        "assistant_context_key",
    }
    assert not any(
        key in result.text
        for key in ("actor_context_ref", "account_user_id", "session_binding", "school_id")
    )
    assert (
        client.post(base + "/logout", headers={"Origin": ORIGIN}).json()["status"] == "signed_out"
    )


def test_identity_http_rejects_nonloopback_cross_origin_and_extra_authority_fields():
    service, redis, _ = _service()
    client, _ = _client(service)
    base = "/api/v1/school-fact-product"
    assert client.post(base + "/login", json={"account_ref": "maintainer"}).status_code == 403
    assert (
        client.post(
            base + "/login",
            json={"account_ref": "maintainer"},
            headers={"Origin": "https://attacker.invalid"},
        ).status_code
        == 403
    )
    assert (
        client.post(
            base + "/login",
            json={"account_ref": "maintainer", "school_id": str(UUID(int=11))},
            headers={"Origin": ORIGIN},
        ).status_code
        == 422
    )
    remote, _ = _client(service, client_host="192.0.2.1")
    assert remote.get(base + "/session").status_code == 403
    assert redis.values == {}


def test_proxy_allowlist_is_explicit_narrow_and_never_trusts_forwarded_headers():
    service, _, _ = _service()
    accepted, _ = _client(
        service, client_host="172.30.10.1", trusted_proxy_cidrs=("172.30.10.1/32",)
    )
    assert accepted.get("/api/v1/school-fact-product/session").status_code == 200
    denied, _ = _client(service, client_host="172.30.10.2", trusted_proxy_cidrs=("172.30.10.1/32",))
    assert (
        denied.get(
            "/api/v1/school-fact-product/session", headers={"X-Forwarded-For": "127.0.0.1"}
        ).status_code
        == 403
    )
    with pytest.raises(ValueError):
        _client(service, trusted_proxy_cidrs=("10.0.0.0/8",))


def _expire_choices(service, redis, token):
    session = service._store.read(token)
    key = service._state_key(session)
    payload = json.loads(redis.values[key])
    payload["effective_until"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    redis.values[key] = json.dumps(payload)


def test_expired_choices_keep_original_identity_private_binding_and_session():
    service, redis, _ = _service()
    token = service.login("maintainer")
    service.select_context(token, "A" * 43)
    original = service.resolve_request(token)
    original_session = redis.values[service._store._key(token)]
    _expire_choices(service, redis, token)
    assert service.resolve_private_actor(original.actor_context_ref) == original
    view = service.session_view(token)
    assert view.selected_context_ref == "A" * 43
    assert view.selected_context_ref in {item.ref for item in view.contexts}
    assert service.resolve_request(token) == original
    assert redis.values[service._store._key(token)] == original_session


def test_choice_expiry_during_content_release_keeps_identity_continuity():
    from app.composition.school_fact_read_evidence import validation_batch

    service, redis, _ = _service()
    token = service.login("maintainer")
    service.select_context(token, "A" * 43)
    original = service.resolve_request(token)
    with validation_batch() as batch:
        assert service.resolve_private_actor(original.actor_context_ref) == original
        batch.content_rechecks["content"] = lambda: _expire_choices(service, redis, token)
    assert service.resolve_private_actor(original.actor_context_ref) == original


@pytest.mark.parametrize("change", ["logout", "switch"])
def test_expired_choice_refresh_cannot_restore_previous_private_actor(change, monkeypatch):
    service, redis, _ = _service()
    token = service.login("maintainer")
    service.select_context(token, "A" * 43)
    original = service.resolve_request(token)
    _expire_choices(service, redis, token)
    original_replace = service._replace_state
    fired = False

    def race(token_arg, session, previous, replacement):
        nonlocal fired
        if not fired:
            fired = True
            if change == "logout":
                service.logout(token)
            else:
                service.select_context(token, "B" * 43)
        return original_replace(token_arg, session, previous, replacement)

    monkeypatch.setattr(service, "_replace_state", race)
    if change == "switch":
        with pytest.raises(_api().ProductIdentityUnavailable):
            service.session_view(token)
    else:
        assert service.session_view(token).selected_context_ref is None
    with pytest.raises(_api().ProductIdentityRejected):
        service.resolve_private_actor(original.actor_context_ref)
    if change == "switch":
        assert service.session_view(token).selected_context_ref == "B" * 43


def test_concurrent_expired_choice_refresh_keeps_same_private_identity(monkeypatch):
    service, redis, _ = _service()
    token = service.login("maintainer")
    service.select_context(token, "A" * 43)
    original = service.resolve_request(token)
    _expire_choices(service, redis, token)
    original_eval = redis.eval
    fired = False

    def race(*args):
        nonlocal fired
        if not fired:
            fired = True
            assert service.session_view(token).selected_context_ref == "A" * 43
        return original_eval(*args)

    monkeypatch.setattr(redis, "eval", race)
    assert service.session_view(token).selected_context_ref == "A" * 43
    assert service.resolve_private_actor(original.actor_context_ref) == original


@pytest.mark.parametrize("change", ["logout", "switch"])
def test_private_resolution_rejects_selection_change_during_database_io(change, monkeypatch):
    service, _, _ = _service()
    token = service.login("maintainer")
    service.select_context(token, "A" * 43)
    original = service.resolve_request(token)
    resolve = service._resolve_selection

    def changed(*args):
        actor = resolve(*args)
        if change == "logout":
            service.logout(token)
        else:
            service.select_context(token, "B" * 43)
        return actor

    monkeypatch.setattr(service, "_resolve_selection", changed)
    with pytest.raises(_api().ProductIdentityRejected):
        service.resolve_private_actor(original.actor_context_ref)


def test_expired_choices_fail_closed_when_selection_store_unavailable(monkeypatch):
    from redis.exceptions import RedisError

    service, redis, _ = _service()
    token = service.login("maintainer")
    service.select_context(token, "A" * 43)
    original = service.resolve_request(token)
    _expire_choices(service, redis, token)

    def unavailable(*_):
        raise RedisError("synthetic unavailability")

    monkeypatch.setattr(redis, "eval", unavailable)
    with pytest.raises(_api().ProductIdentityUnavailable):
        service.session_view(token)
    monkeypatch.setattr(redis, "get", unavailable)
    with pytest.raises(_api().ProductIdentityUnavailable):
        service.resolve_private_actor(original.actor_context_ref)


def test_reselecting_current_expired_public_choice_keeps_private_identity(monkeypatch):
    service, redis, _ = _service()
    token = service.login("maintainer")
    service.select_context(token, "A" * 43)
    original = service.resolve_request(token)
    _expire_choices(service, redis, token)
    factory = service._resolver_factory

    def expired_factory(user, session):
        resolver = factory(user, session)

        def expired(_):
            raise ContextNoLongerValid("short option expired")

        resolver.resolve = expired
        return resolver

    monkeypatch.setattr(service, "_resolver_factory", expired_factory)
    service.select_context(token, "A" * 43)
    assert service.resolve_request(token) == original


@pytest.mark.parametrize("expired", [False, True])
def test_legacy_selection_upgrade_preserves_binding_only_when_original_option_valid(
    monkeypatch, expired
):
    service, redis, _ = _service()
    token = service.login("maintainer")
    service.select_context(token, "A" * 43)
    original = service.resolve_request(token)
    key = service._state_key(service._store.read(token))
    legacy = json.loads(redis.values[key])
    legacy.pop("selected_identity")
    redis.values[key] = json.dumps(legacy)
    if expired:
        factory = service._resolver_factory

        def expired_factory(user, session):
            resolver = factory(user, session)

            def fail(_):
                raise ContextNoLongerValid("old option expired")

            resolver.resolve = fail
            return resolver

        monkeypatch.setattr(service, "_resolver_factory", expired_factory)
        view = service.session_view(token)
        assert view.status == "signed_in" and view.selected_context_ref is None
        with pytest.raises(_api().ProductIdentityRejected):
            service.resolve_private_actor(original.actor_context_ref)
    else:
        assert service.resolve_private_actor(original.actor_context_ref) == original
        _expire_choices(service, redis, token)
        assert service.resolve_private_actor(original.actor_context_ref) == original


def test_final_session_store_failure_is_reported_as_identity_unavailable(monkeypatch):
    from redis.exceptions import RedisError

    service, redis, _ = _service()
    token = service.login("maintainer")
    service.select_context(token, "A" * 43)
    original = service.resolve_request(token)
    real_get = redis.get
    session_reads = 0

    def fail_final_session_read(key):
        nonlocal session_reads
        if key == service._store._key(token):
            session_reads += 1
            if session_reads == 2:
                raise RedisError("final read unavailable")
        return real_get(key)

    monkeypatch.setattr(redis, "get", fail_final_session_read)
    with pytest.raises(_api().ProductIdentityUnavailable):
        service.resolve_private_actor(original.actor_context_ref)


def test_late_session_refresh_after_identity_switch_does_not_clear_valid_login_cookie(monkeypatch):
    service, redis, _ = _service()
    client, _ = _client(service)
    base = "/api/v1/school-fact-product"
    assert (
        client.post(
            base + "/login", json={"account_ref": "maintainer"}, headers={"Origin": ORIGIN}
        ).status_code
        == 200
    )
    client.post(base + "/context", json={"context_ref": "A" * 43}, headers={"Origin": ORIGIN})
    private = next(value for key, value in redis.values.items() if ":private_actor:" in key)
    token = json.loads(private)["session_token"]
    _expire_choices(service, redis, token)
    replace_state = service._replace_state
    fired = False

    def switch_before_refresh(*args):
        nonlocal fired
        if not fired:
            fired = True
            service.select_context(token, "B" * 43)
        return replace_state(*args)

    monkeypatch.setattr(service, "_replace_state", switch_before_refresh)
    response = client.get(base + "/session")
    assert "set-cookie" not in response.headers
    assert response.status_code in (200, 409, 503)
    assert client.get(base + "/session").json()["selected_context_ref"] == "B" * 43
