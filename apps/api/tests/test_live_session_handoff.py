"""Synthetic identity handoff evidence; no real credential or business grant."""

from __future__ import annotations

import importlib
import importlib.util
import json
import os
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.composition.school_fact_live_initialization_contracts import LiveInitializationManifest
from app.integrations.dingtalk.client import DingTalkIdentity
from app.platform.identity.models import User
from app.platform.identity.router import get_dingtalk_identity_verifier, get_dingtalk_login_guard
from tests.test_f1_product_identity import MemoryRedis
from tests.test_live_initialization import configuration_payload, manifest_payload

BASE = "/api/v1/school-fact-initialization"
ORIGIN = "https://school.example.org"


def api():
    name = "app.composition.school_fact_live_session_handoff"
    assert importlib.util.find_spec(name), "bounded formal identity handoff is missing"
    return importlib.import_module(name)


def payload(tmp_path, *, approval=False):
    if approval:
        from app.composition.school_fact_live_initialization_contracts import (
            LiveSessionHandoffApproval,
        )

        m = LiveSessionHandoffApproval.model_validate(handoff_approval_payload())
    else:
        m = LiveInitializationManifest.model_validate(manifest_payload())
    return dict(
        purpose="school-facts-live-session-handoff",
        runtime=configuration_payload()["runtime"],
        manifest=m,
        approved_sha256=m.manifest_sha256,
        allowed_origin=ORIGIN,
        trusted_proxy_cidrs=(),
        output=tmp_path / "session.json",
    )


def handoff_approval_payload():
    source = manifest_payload()
    keys = (
        "school_id",
        "anchor_id",
        "expected_system_identifier",
        "database_name",
        "corp_id",
        "external_subject",
        "approval_reference",
        "issuer_reference",
        "issuer_qualification_reference",
        "operator_reference",
        "source_authority_reference",
        "approved_at",
        "expires_at",
    )
    return {"purpose": "school-facts-live-session-handoff"} | {key: source[key] for key in keys}


@pytest.fixture(params=[False, True], ids=["legacy-manifest", "handoff-approval"])
def setup(tmp_path, monkeypatch, request):
    mod = api()
    config = mod.SessionHandoffConfiguration.model_validate(
        payload(tmp_path, approval=request.param)
    )
    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    User.__table__.create(engine)
    sessions = sessionmaker(engine)
    with sessions.begin() as s:
        s.add(
            User(
                id=1,
                provider_code="dingtalk",
                provider_tenant_key="test-corp",
                external_subject="test-first-operator",
                display_name="synthetic",
                is_active=True,
                security_version=1,
            )
        )
    redis = MemoryRedis()
    state = SimpleNamespace(
        now=config.manifest.approved_at,
        calls=0,
        identity=DingTalkIdentity("test-corp", "test-first-operator", "synthetic"),
    )

    class Verifier:
        def verify(self, code):
            state.calls += 1
            assert code == "synthetic-code"
            return state.identity

        def fetch_optional_mobile(self, subject):
            return None

    class Guard:
        @contextmanager
        def admit(self, request_id):
            assert request_id
            yield

    # Windows does not provide POSIX directory ownership/mode semantics. HTTP
    # tests substitute only the file primitive; Linux tests below use it directly.
    if os.name != "posix":

        @contextmanager
        def reserve(path, *, before_release=lambda: None):
            with path.open("x+b") as stream:
                try:
                    yield stream
                    before_release()
                except BaseException:
                    stream.seek(0)
                    stream.truncate()
                    raise

        monkeypatch.setattr(mod, "reserve_protected_output", reserve)
    else:
        tmp_path.chmod(0o700)
    app = mod.create_session_handoff_app(
        config, sessions=sessions, redis=redis, clock=lambda: state.now
    )
    app.dependency_overrides[get_dingtalk_identity_verifier] = Verifier
    app.dependency_overrides[get_dingtalk_login_guard] = Guard
    with TestClient(app, base_url=ORIGIN) as client:
        yield SimpleNamespace(
            mod=mod,
            config=config,
            client=client,
            state=state,
            redis=redis,
            sessions=sessions,
            app=app,
        )
    engine.dispose()


def exchange(s, **kwargs):
    return s.client.post(
        BASE + "/dingtalk/exchange",
        json={"auth_code": "synthetic-code"},
        headers={"origin": ORIGIN},
        **kwargs,
    )


def test_handoff_is_consumable_and_token_only_in_protected_file_cookie(setup, caplog):
    s = setup
    response = exchange(s)
    assert response.status_code == 200, response.text
    assert response.json() == {"status": "identity_delivered"}
    saved = json.loads(s.config.output.read_text())
    token = saved["token"]
    assert len(token) > 32 and token not in response.text and token not in caplog.text
    assert saved["external_subject"] == "test-first-operator"
    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie and "Secure" in cookie and "SameSite=lax" in cookie
    from app.composition.school_fact_live_initialization_contracts import LiveInitializationSession
    from app.composition.school_fact_live_initialization_identity import (
        require_live_initialization_account,
    )

    assert (
        require_live_initialization_account(
            s.config.runtime, s.sessions, s.redis, LiveInitializationSession.model_validate(saved)
        ).current_user.id
        == 1
    )
    assert s.client.get(BASE + "/session").json() == {"status": "authenticated"}
    assert exchange(s).status_code == 409
    assert s.state.calls == 1


def test_handoff_approval_accepts_identity_without_unreviewed_first_facts(tmp_path):
    from app.composition import school_fact_live_initialization_contracts as contracts

    assert hasattr(contracts, "LiveSessionHandoffApproval")
    approval = contracts.LiveSessionHandoffApproval.model_validate(handoff_approval_payload())
    assert "first_facts" not in approval.model_dump()
    assert "duties" not in approval.model_dump()
    configuration = api().SessionHandoffConfiguration.model_validate(
        payload(tmp_path, approval=True)
    )
    assert configuration.manifest == approval


def test_authenticated_name_is_only_a_protected_display_candidate(setup):
    s = setup
    s.state.identity = DingTalkIdentity("test-corp", "test-first-operator", "钉钉可信姓名")
    response = exchange(s)
    assert response.status_code == 200, response.text
    saved = json.loads(s.config.output.read_text())
    assert saved["display_name_candidate"] == "钉钉可信姓名"
    assert "钉钉可信姓名" not in response.text
    assert set(saved) == {"token", "corp_id", "external_subject", "display_name_candidate"}


def test_display_candidate_comes_from_this_login_when_same_subject_logs_in_again(
    setup, monkeypatch
):
    from fastapi import Response

    s = setup
    first_name = "首次验码姓名"
    later_name = "随后登录姓名"
    s.state.identity = DingTalkIdentity("test-corp", "test-first-operator", first_name)
    issue = s.mod.issue_dingtalk_identity_session

    def issue_with_later_login(*args, **kwargs):
        first = issue(*args, **kwargs)
        s.state.identity = DingTalkIdentity("test-corp", "test-first-operator", later_name)
        second_args = list(args)
        second_args[2] = Response()
        issue(*second_args, **kwargs)
        return first

    monkeypatch.setattr(s.mod, "issue_dingtalk_identity_session", issue_with_later_login)
    assert exchange(s).status_code == 200
    with s.sessions() as session:
        assert session.get(User, 1).display_name == later_name
    saved = json.loads(s.config.output.read_text())
    assert saved["display_name_candidate"] == first_name


@pytest.mark.parametrize(
    "identity",
    [
        DingTalkIdentity("other", "test-first-operator", "x"),
        DingTalkIdentity("test-corp", "other", "x"),
    ],
)
def test_other_identity_never_gets_session(setup, identity):
    s = setup
    s.state.identity = identity
    assert exchange(s).status_code == 401
    assert not s.redis.values
    assert not s.config.output.exists()


def test_expiry_and_origin_reject_before_authentication(setup):
    s = setup
    assert (
        s.client.post(
            BASE + "/dingtalk/exchange",
            json={"auth_code": "synthetic-code"},
            headers={"origin": "https://other.example.org"},
        ).status_code
        == 403
    )
    s.state.now = s.config.manifest.expires_at
    assert exchange(s).status_code == 403
    assert s.state.calls == 0 and not s.config.output.exists()


def test_logout_and_current_security_version_invalidate_delivered_identity(setup):
    s = setup
    assert exchange(s).status_code == 200
    with s.sessions.begin() as session:
        session.get(User, 1).security_version += 1
    assert s.client.get(BASE + "/session").json() == {"status": "authentication_required"}
    assert s.client.post(BASE + "/logout", headers={"origin": ORIGIN}).json() == {
        "status": "logged_out"
    }
    assert not s.redis.values


@pytest.mark.parametrize(
    "field,value",
    [
        ("approved_sha256", "f" * 64),
        ("allowed_origin", "http://school.example.org"),
        ("trusted_proxy_cidrs", ("0.0.0.0/0",)),
    ],
)
@pytest.mark.parametrize("approval", [False, True], ids=["legacy", "handoff"])
def test_configuration_rejects_unreviewed_inputs(tmp_path, field, value, approval):
    p = payload(tmp_path, approval=approval)
    p[field] = value
    with pytest.raises(ValueError):
        api().SessionHandoffConfiguration.model_validate(p)


@pytest.mark.parametrize(
    "field,value",
    [
        ("postgres_expected_school_id", "00000000-0000-0000-0000-000000000009"),
        ("postgres_expected_anchor_id", "00000000-0000-0000-0000-000000000009"),
        ("dingtalk_corp_id", "other"),
    ],
)
@pytest.mark.parametrize("approval", [False, True], ids=["legacy", "handoff"])
def test_configuration_rejects_mismatched_target(tmp_path, field, value, approval):
    p = payload(tmp_path, approval=approval)
    p["runtime"][field] = value
    with pytest.raises(ValueError):
        api().SessionHandoffConfiguration.model_validate(p)


def test_protected_output_permissions_symlinks_and_exclusive_creation(tmp_path):
    mod = api()
    if os.name != "posix":
        with pytest.raises(ValueError, match="private Linux directory"):
            with mod.reserve_protected_output(tmp_path / "session.json"):
                raise AssertionError("non-POSIX output must not be created")
        assert not (tmp_path / "session.json").exists()
        return
    tmp_path.chmod(0o700)
    path = tmp_path / "session.json"
    with mod.reserve_protected_output(path) as stream:
        assert path.stat().st_mode & 0o777 == 0o600
        with pytest.raises(FileExistsError):
            with mod.reserve_protected_output(path):
                pass
        stream.write(b"synthetic")
    link = tmp_path / "link"
    link.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises((ValueError, OSError)):
        with mod.reserve_protected_output(link / "other.json"):
            pass
    tmp_path.chmod(0o755)
    with pytest.raises(ValueError):
        with mod.reserve_protected_output(tmp_path / "other.json"):
            pass


def test_window_expiring_during_commit_revokes_just_created_token(setup):
    from sqlalchemy import event

    s = setup

    def expire(_session):
        s.state.now = s.config.manifest.expires_at

    event.listen(s.sessions.class_, "before_commit", expire)
    try:
        response = exchange(s)
    finally:
        event.remove(s.sessions.class_, "before_commit", expire)
    assert response.status_code == 403
    assert "set-cookie" not in response.headers
    assert not s.redis.values and not s.config.output.exists()


def test_commit_failure_revokes_session_and_emits_no_secret(setup, caplog):
    from sqlalchemy import event
    from sqlalchemy.exc import SQLAlchemyError

    s = setup

    def fail(_session):
        raise SQLAlchemyError("synthetic-sensitive-diagnostic")

    event.listen(s.sessions.class_, "before_commit", fail)
    try:
        response = exchange(s)
    finally:
        event.remove(s.sessions.class_, "before_commit", fail)
    assert response.status_code == 503
    assert "set-cookie" not in response.headers
    assert not s.redis.values and not s.config.output.exists()
    assert "synthetic-sensitive-diagnostic" not in response.text + caplog.text


def test_output_write_failure_revokes_token_and_sanitizes_diagnostics(setup, monkeypatch, caplog):
    s = setup

    def fail(_fd):
        raise OSError("synthetic-sensitive-output-path")

    monkeypatch.setattr(s.mod.os, "fsync", fail)
    response = exchange(s)
    assert response.status_code == 503 and "set-cookie" not in response.headers
    assert not s.redis.values and s.config.output.read_bytes() == b""
    assert "synthetic-sensitive-output-path" not in response.text + caplog.text


def test_parallel_verified_exchange_has_one_file_and_only_one_live_session(setup):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    s = setup
    entered, release = Event(), Event()
    verifier_type = s.app.dependency_overrides[get_dingtalk_identity_verifier]

    class WaitingVerifier(verifier_type):
        def verify(self, code):
            if not entered.is_set():
                entered.set()
                assert release.wait(5)
            return super().verify(code)

    s.app.dependency_overrides[get_dingtalk_identity_verifier] = WaitingVerifier
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(exchange, s)
        try:
            assert entered.wait(5)
            assert pool.submit(exchange, s).result(timeout=5).status_code == 200
        finally:
            release.set()
        assert first.result(timeout=5).status_code == 409
    assert s.state.calls == 2 and len(s.redis.values) == 1


def test_disabled_account_has_no_deliverable_session(setup):
    s = setup
    with s.sessions.begin() as session:
        session.get(User, 1).is_active = False
    assert exchange(s).status_code == 403
    assert not s.redis.values and not s.config.output.exists()


def test_delivered_session_expiry_is_currently_checked(setup):
    s = setup
    assert exchange(s).status_code == 200
    for key, value in s.redis.values.items():
        row = json.loads(value)
        row["expires_at"] = "2020-01-01T00:00:00+00:00"
        s.redis.values[key] = json.dumps(row)
    assert s.client.get(BASE + "/session").json() == {"status": "authentication_required"}


def test_no_business_initialization_export_or_normal_auth_routes(setup):
    paths = set(setup.app.openapi()["paths"])
    assert paths == {BASE + "/session", BASE + "/logout", BASE + "/dingtalk/exchange"}


def test_spoofed_proxy_headers_do_not_make_untrusted_http_peer_secure(setup):
    s = setup
    with TestClient(s.app, base_url="http://internal", client=("198.51.100.2", 10000)) as client:
        response = client.post(
            BASE + "/dingtalk/exchange",
            json={"auth_code": "synthetic-code"},
            headers={
                "origin": ORIGIN,
                "x-forwarded-proto": "https",
                "x-forwarded-host": "school.example.org",
            },
        )
    assert response.status_code == 403 and s.state.calls == 0


def test_invalid_body_never_echoes_authentication_material(setup):
    s = setup
    secret = "synthetic-auth-material-" * 200
    response = s.client.post(
        BASE + "/dingtalk/exchange", json={"auth_code": secret}, headers={"origin": ORIGIN}
    )
    assert response.status_code == 422
    assert "synthetic-auth-material" not in response.text
    assert response.headers["cache-control"] == "no-store"


def test_expiry_during_file_flush_erases_new_file_and_revokes_session(setup, monkeypatch):
    s = setup
    original = s.mod.os.fsync

    def expire(fd):
        original(fd)
        s.state.now = s.config.manifest.expires_at

    monkeypatch.setattr(s.mod.os, "fsync", expire)
    response = exchange(s)
    assert response.status_code == 403 and "set-cookie" not in response.headers
    assert s.config.output.read_bytes() == b"" and not s.redis.values


def test_explicit_trusted_private_proxy_can_forward_exact_origin(setup):
    s = setup
    configuration = s.config.model_copy(update={"trusted_proxy_cidrs": ("10.1.1.2/32",)})
    app = s.mod.create_session_handoff_app(
        configuration, sessions=s.sessions, redis=s.redis, clock=lambda: s.state.now
    )
    app.dependency_overrides.update(s.app.dependency_overrides)
    with TestClient(app, base_url="http://internal", client=("10.1.1.2", 10000)) as client:
        response = client.post(
            BASE + "/dingtalk/exchange",
            json={"auth_code": "synthetic-code"},
            headers={
                "origin": ORIGIN,
                "x-forwarded-proto": "https",
                "x-forwarded-host": "school.example.org",
            },
        )
    assert response.status_code == 200


def test_existing_empty_reservation_is_not_claimed_as_success(setup):
    s = setup
    s.config.output.write_bytes(b"")
    response = exchange(s)
    assert response.status_code == 409
    assert response.json() == {"detail": "已有交付记录，请核对原结果。"}
    assert s.state.calls == 0 and not s.redis.values


def test_window_is_checked_after_protected_output_durability(setup, monkeypatch):
    s = setup
    reserve = s.mod.reserve_protected_output

    @contextmanager
    def slow_durable_output(path, **kwargs):
        with reserve(path, **kwargs) as stream:
            yield stream
            s.state.now = s.config.manifest.expires_at

    monkeypatch.setattr(s.mod, "reserve_protected_output", slow_durable_output)
    response = exchange(s)
    assert response.status_code == 403
    assert s.config.output.read_bytes() == b"" and not s.redis.values
