from __future__ import annotations

import importlib
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import cast
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.orm import Session

from app.core.settings import Settings
from app.main import app
from app.platform.contracts import (
    AssignmentId,
    GuardianRelationshipId,
    PersonId,
    SchoolId,
    _issue_guardian_actor_context,
    _issue_staff_actor_context,
)
from app.platform.identity.context import (
    CompleteIdentitySession,
    CurrentUser,
    resolve_complete_identity_session,
)
from app.platform.identity.execution_context import (
    ContextResolutionUnavailable,
    SelectedContextRef,
)
from app.platform.identity.models import User
from app.platform.identity.router import (
    get_actor_context_resolver,
)
from app.platform.identity.router import router as identity_router
from app.platform.identity.session import IdentitySession


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class FakeResolver:
    def __init__(self, result: object) -> None:
        self.result = result
        self.resolve_calls = 0

    def list_options(self) -> tuple[SelectedContextRef, ...]:
        if isinstance(self.result, Exception):
            raise self.result
        return cast(tuple[SelectedContextRef, ...], self.result)

    def list_school_reactivation_candidates(self) -> tuple[SelectedContextRef, ...]:
        if isinstance(self.result, Exception):
            raise self.result
        return cast(tuple[SelectedContextRef, ...], self.result)

    def resolve(self, _opaque_ref: str) -> object:
        self.resolve_calls += 1
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class ClosingSession:
    def __init__(self, user: User) -> None:
        self.user = user
        self.closed = False

    def __enter__(self) -> ClosingSession:
        return self

    def __exit__(self, *_args: object) -> None:
        self.closed = True

    def scalar(self, _statement: object) -> User:
        return self.user

    def get_bind(self) -> object:
        return SimpleNamespace(dialect=SimpleNamespace(name="sqlite"))


def _settings() -> Settings:
    return Settings.model_validate(
        {
            "app_env": "test",
            "postgres_host": "127.0.0.1",
            "postgres_port": 5432,
            "postgres_db": "campus_ai_test",
            "postgres_user": "tester",
            "postgres_password": "not-secret",
            "rls_context_signing_key": "identity-context-api-test-key-32-bytes",
            "redis_port": 6379,
            "redis_password": "not-secret",
            "identity_provider": "local_test",
        }
    )


def _complete_session() -> CompleteIdentitySession:
    now = datetime.now(UTC)
    current_user = CurrentUser(
        id=17,
        external_subject="task3-http-user",
        display_name="Task 3 教师",
        security_version=1,
    )
    return CompleteIdentitySession(
        current_user=current_user,
        identity_session=IdentitySession(
            user_id=17,
            external_subject=current_user.external_subject,
            display_name=current_user.display_name,
            security_version=1,
            session_nonce_hash="b" * 64,
            issued_at=now.isoformat(),
            expires_at=(now + timedelta(hours=1)).isoformat(),
        ),
    )


def _staff_actor() -> object:
    return _issue_staff_actor_context(
        account_user_id=17,
        school_id=SchoolId(UUID("10000000-0000-0000-0000-000000000001")),
        person_id=PersonId(UUID("20000000-0000-0000-0000-000000000001")),
        assignment_id=AssignmentId(UUID("30000000-0000-0000-0000-000000000001")),
        assignment_version=1,
        authorization_version=1,
    )


def _guardian_actor() -> object:
    return _issue_guardian_actor_context(
        account_user_id=17,
        school_id=SchoolId(UUID("10000000-0000-0000-0000-000000000001")),
        person_id=PersonId(UUID("20000000-0000-0000-0000-000000000001")),
        guardian_relationship_id=GuardianRelationshipId(
            UUID("40000000-0000-0000-0000-000000000001")
        ),
        relationship_version=1,
        authorization_version=1,
    )


def test_identity_context_api_and_complete_session_boundary_exist() -> None:
    """上下文枚举必须有自己的完整短会话边界，而不是复用请求级数据库 Session。"""

    context_module = importlib.import_module("app.platform.identity.context")
    matching_routes = [
        route
        for route in identity_router.routes
        if getattr(route, "path", None) == "/api/v1/auth/contexts"
        and "GET" in getattr(route, "methods", set())
    ]
    reactivation_routes = [
        route
        for route in identity_router.routes
        if getattr(route, "path", None) == "/api/v1/auth/school-reactivation-contexts"
        and "GET" in getattr(route, "methods", set())
    ]

    assert hasattr(context_module, "CompleteIdentitySession")
    assert hasattr(context_module, "resolve_complete_identity_session")
    assert len(matching_routes) == 1
    assert len(reactivation_routes) == 1


def test_complete_identity_session_closes_database_before_returning() -> None:
    user = User(
        id=17,
        external_subject="alpha-reporter",
        display_name="合成报修教师",
        security_version=3,
        is_active=True,
    )
    database_session = ClosingSession(user)

    complete = resolve_complete_identity_session(
        settings=_settings(),
        session_token=None,
        test_subject="alpha-reporter",
        test_display_name="合成报修教师",
        session_factory=lambda: cast(Session, database_session),
        redis=None,
    )

    assert database_session.closed is True
    assert complete.current_user.id == 17
    assert complete.identity_session.user_id == 17
    assert complete.identity_session.trusted_mobile is None


@pytest.mark.anyio
async def test_contexts_returns_only_public_opaque_fields() -> None:
    option = SelectedContextRef(
        opaque_ref="context_ref_abcdefghijklmnopqrstuvwxyz_123456",
        kind="staff_assignment",
        display_label="高一（3）班班主任",
        school_label="合成中学",
        effective_until=datetime.now(UTC) + timedelta(minutes=10),
    )
    app.dependency_overrides[get_actor_context_resolver] = lambda: FakeResolver((option,))
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/api/v1/auth/contexts")
    finally:
        app.dependency_overrides.pop(get_actor_context_resolver, None)

    assert response.status_code == 200
    assert set(response.json()) == {"items"}
    assert set(response.json()["items"][0]) == {
        "opaque_ref",
        "kind",
        "display_label",
        "school_label",
        "effective_until",
    }
    assert "phone" not in response.text.lower()
    assert "person_id" not in response.text
    assert "assignment_id" not in response.text
    assert "permission" not in response.text.lower()


@pytest.mark.anyio
async def test_school_reactivation_contexts_use_the_same_opaque_public_shape() -> None:
    option = SelectedContextRef(
        opaque_ref="reactivation_context_ref_abcdefghijklmnopqrstuvwxyz",
        kind="staff_assignment",
        display_label="学校数据管理员",
        school_label="停用学校",
        effective_until=datetime.now(UTC) + timedelta(minutes=10),
    )
    app.dependency_overrides[get_actor_context_resolver] = lambda: FakeResolver((option,))
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/api/v1/auth/school-reactivation-contexts")
    finally:
        app.dependency_overrides.pop(get_actor_context_resolver, None)

    assert response.status_code == 200
    assert response.json() == {"items": [option.model_dump(mode="json")]}


@pytest.mark.anyio
async def test_contexts_fails_closed_with_stable_error_envelope() -> None:
    app.dependency_overrides[get_actor_context_resolver] = lambda: FakeResolver(
        ContextResolutionUnavailable("database marker should not escape")
    )
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/api/v1/auth/contexts")
    finally:
        app.dependency_overrides.pop(get_actor_context_resolver, None)

    assert response.status_code == 503
    assert response.json() == {
        "code": "identity.context_unavailable",
        "message": "履职身份暂时无法读取，请稍后重试。",
        "request_id": response.headers["X-Request-ID"],
    }
    assert "database marker" not in response.text


def _selected_resolver_fixture(monkeypatch, *, guardian=False):
    from dataclasses import replace

    from app.platform.identity import execution_context as module
    from app.platform.identity.context_proof import ContextProofSigner
    from tests.test_f1_product_identity import MemoryRedis

    complete = _complete_session()
    actor = _guardian_actor() if guardian else _staff_actor()
    clock = SimpleNamespace(now=datetime.now(UTC))
    session = replace(
        complete.identity_session,
        expires_at=(clock.now + timedelta(hours=1)).isoformat(),
    )
    row = {
        "context_kind": "guardian" if guardian else "assignment",
        "context_id": actor.guardian_relationship_id if guardian else actor.assignment_id,
        "context_version": actor.relationship_version if guardian else actor.assignment_version,
        "school_id": actor.school_id,
        "person_id": actor.person_id,
        "display_label": "合成学校 · 同名身份",
    }
    state = SimpleNamespace(rows=[row], proofs=[], deny=False, advance_during_io=0, queries=0)

    class Database:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def begin(self):
            return self

        def execute(self, *_):
            state.queries += 1
            return SimpleNamespace(mappings=lambda: SimpleNamespace(all=lambda: state.rows))

    def validate(_session, envelope):
        import psycopg
        from sqlalchemy.exc import DBAPIError

        state.proofs.append(envelope)
        state.queries += 1
        if state.deny:
            raise DBAPIError(
                "context check", {}, psycopg.errors.InvalidAuthorizationSpecification()
            )
        clock.now += timedelta(seconds=state.advance_during_io)

    monkeypatch.setattr(module, "set_local_execution_context", validate)
    redis = MemoryRedis()
    resolver = module.ActorContextResolver(
        session_factory=Database,
        redis=redis,
        proof_signer=ContextProofSigner(b"synthetic-test-context-signing-key-32-bytes"),
        current_user=complete.current_user,
        identity_session=session,
        clock=lambda: clock.now,
    )
    option = resolver.list_options()[0]
    resolved = resolver.resolve(option.opaque_ref)
    selected = module.SelectedContextIdentity.from_actor(resolved, session)
    return module, resolver, selected, resolved, session, option, redis, clock, state


@pytest.mark.parametrize("guardian", [False, True])
def test_retained_selection_renews_only_database_proof_after_short_reference_expiry(
    monkeypatch, guardian
):
    module, resolver, selected, actor, session, option, redis, clock, state = (
        _selected_resolver_fixture(monkeypatch, guardian=guardian)
    )
    original_entries = dict(redis.values)
    clock.now += timedelta(seconds=901)
    with pytest.raises(module.ContextNoLongerValid):
        resolver.resolve(option.opaque_ref)
    before = state.queries
    assert resolver.resolve_selected(selected) == actor
    assert resolver.resolve_selected(selected) == actor
    assert state.queries == before + 2
    assert redis.values == original_entries
    for proof in state.proofs[-2:]:
        assert proof.proof_expires_at == clock.now + timedelta(seconds=900)
        assert proof.school_id == selected.school_id
        assert proof.person_id == selected.person_id
        assert proof.session_nonce_hash == session.session_nonce_hash
        assert proof.authorization_version == selected.authorization_version
        if guardian:
            assert proof.guardian_relationship_id == selected.context_id
            assert proof.guardian_relationship_version == selected.context_version
        else:
            assert proof.assignment_id == selected.context_id
            assert proof.assignment_version == selected.context_version
    clock.now = datetime.fromisoformat(session.expires_at) - timedelta(seconds=2)
    assert resolver.resolve_selected(selected) == actor
    assert state.proofs[-1].proof_expires_at == datetime.fromisoformat(session.expires_at)
    clock.now += timedelta(seconds=2)
    with pytest.raises(module.ContextNoLongerValid):
        resolver.resolve_selected(selected)


@pytest.mark.parametrize(
    "field,value",
    [
        ("account_user_id", 99),
        ("authorization_version", 2),
        ("session_nonce_hash", "d" * 64),
        ("session_expires_at", datetime(2099, 1, 1, tzinfo=UTC)),
    ],
)
def test_retained_selection_rejects_different_account_or_session_before_database(
    monkeypatch, field, value
):
    module, resolver, selected, _, _, _, _, clock, state = _selected_resolver_fixture(monkeypatch)
    clock.now += timedelta(seconds=901)
    before = state.queries
    with pytest.raises(module.ContextNoLongerValid):
        resolver.resolve_selected(selected.model_copy(update={field: value}))
    assert state.queries == before


@pytest.mark.parametrize("guardian", [False, True])
def test_retained_selection_still_obeys_current_database_rejection(monkeypatch, guardian):
    module, resolver, selected, _, _, _, _, clock, state = _selected_resolver_fixture(
        monkeypatch, guardian=guardian
    )
    clock.now += timedelta(seconds=901)
    state.deny = True
    with pytest.raises(module.ContextNoLongerValid):
        resolver.resolve_selected(selected)
    proof = state.proofs[-1]
    assert (proof.guardian_relationship_version if guardian else proof.assignment_version) == 1


def test_retained_selection_rejects_proof_expiry_during_database_validation(monkeypatch):
    module, resolver, selected, _, _, _, _, clock, state = _selected_resolver_fixture(monkeypatch)
    clock.now += timedelta(seconds=901)
    state.advance_during_io = 901
    with pytest.raises(module.ContextNoLongerValid):
        resolver.resolve_selected(selected)


def test_choice_refresh_deduplicates_only_exact_selected_identity_not_display_label(monkeypatch):
    _, resolver, selected, _, _, _, _, clock, state = _selected_resolver_fixture(monkeypatch)
    original = state.rows[0]
    state.rows = [
        original,
        original | {"context_id": UUID(int=22)},
        original | {"context_version": 2},
    ]
    clock.now += timedelta(seconds=901)
    choices = resolver.list_options(exclude_selected=selected)
    assert len(choices) == 2
    assert all(option.display_label == "同名身份" for option in choices)
