from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from app.composition.school_fact_product_identity import (
    ENGINEERING_ACCOUNTS,
    ProductIdentityRejected,
    SchoolFactProductIdentityService,
)
from app.platform.contracts import (
    GuardianRelationshipId,
    _issue_guardian_actor_context,
)
from app.platform.identity.context import CurrentUser
from app.platform.identity.execution_context import ContextNoLongerValid, SelectedContextRef
from tests.test_f1_official_contact_query import _actor
from tests.test_f1_product_identity import MemoryRedis


def guardian(number=1):
    staff = _actor()
    return _issue_guardian_actor_context(
        account_user_id=staff.account_user_id,
        school_id=staff.school_id,
        person_id=staff.person_id,
        guardian_relationship_id=GuardianRelationshipId(UUID(int=number)),
        relationship_version=1,
        authorization_version=staff.authorization_version,
    )


def service(*, enabled=True):
    state = SimpleNamespace(ended=False, maintain_calls=[], resolves=0)
    actors = {"A" * 43: _actor(), "B" * 43: guardian(), "C" * 43: guardian(2)}

    class Resolver:
        def list_options(self, *, exclude_selected=None):
            return tuple(
                SelectedContextRef(
                    opaque_ref=ref,
                    kind="staff_assignment" if ref.startswith("A") else "guardian_relationship",
                    display_label="教师" if ref.startswith("A") else "监护人 · 合成孩子",
                    school_label="合成学校",
                    effective_until=datetime.now(UTC) + timedelta(minutes=15),
                )
                for ref in actors
            )

        def resolve(self, opaque_ref: str):
            state.resolves += 1
            ref = opaque_ref
            if state.ended and ref.startswith("B"):
                raise ContextNoLongerValid("relationship ended")
            return actors[ref]

        def resolve_selected(self, selected):
            for ref, actor in actors.items():
                if str(selected.context_id) == str(
                    actor.assignment_id if ref.startswith("A") else actor.guardian_relationship_id
                ) and selected.context_kind == (
                    "assignment" if ref.startswith("A") else "guardian"
                ):
                    return self.resolve(ref)
            raise ContextNoLongerValid("no current matching relationship")

    def maintain(actor):
        state.maintain_calls.append(actor)
        assert type(actor) is type(_actor()), "parent must not enter staff duty evaluation"
        return True

    result = SchoolFactProductIdentityService(
        app_env="test",
        redis=MemoryRedis(),
        accounts=(ENGINEERING_ACCOUNTS[0],),
        load_account=lambda _: CurrentUser(
            id=7,
            external_subject="f1-maintainer",
            display_name="合成工程维护员",
            security_version=1,
        ),
        resolver_factory=lambda _user, _session: Resolver(),
        can_maintain=maintain,
        family_enabled=enabled,
    )
    return result, state


def test_f3_teacher_parent_and_two_children_are_explicit_separate_contexts():
    identity, state = service()
    token = identity.login("maintainer")
    assert len(identity.session_view(token).contexts) == 3
    assert identity.session_view(token).family_enabled is True
    assert identity.session_view(token).selected_context_kind is None
    identity.select_context(token, "B" * 43)
    first = identity.resolve_request(token)
    assert first.actor == guardian()
    assert identity.session_view(token).selected_context_kind == "guardian_relationship"
    assert identity.session_view(token).can_maintain is False
    assert state.maintain_calls == []
    identity.select_context(token, "C" * 43)
    with pytest.raises(ProductIdentityRejected):
        identity.resolve_private_actor(first.actor_context_ref)
    second = identity.resolve_request(token)
    assert second.actor == guardian(2)
    identity.select_context(token, "A" * 43)
    with pytest.raises(ProductIdentityRejected):
        identity.resolve_private_actor(second.actor_context_ref)
    assert identity.session_view(token).can_maintain is True
    assert identity.session_view(token).selected_context_kind == "staff_assignment"


def test_f3_relation_revocation_is_rechecked_for_private_model_reference():
    identity, state = service()
    token = identity.login("maintainer")
    identity.select_context(token, "B" * 43)
    actor = identity.resolve_request(token)
    state.ended = True
    with pytest.raises(ProductIdentityRejected):
        identity.resolve_private_actor(actor.actor_context_ref)


def test_f1_f2_do_not_enable_guardian_contexts_implicitly():
    identity, _ = service(enabled=False)
    token = identity.login("maintainer")
    assert len(identity.session_view(token).contexts) == 1
    assert identity.session_view(token).family_enabled is False
    with pytest.raises(ProductIdentityRejected):
        identity.select_context(token, "B" * 43)


@pytest.mark.parametrize("change", ["child", "revoke", "logout"])
def test_guardian_identity_cannot_change_during_a_synchronous_release(change):
    from app.composition.school_fact_read_evidence import validation_batch
    from app.platform.assistant.runtime import AssistantRuntimeError

    identity, state = service()
    token = identity.login("maintainer")
    identity.select_context(token, "B" * 43)
    original = identity.resolve_request(token)
    with pytest.raises(AssistantRuntimeError):
        with validation_batch():
            assert identity.resolve_private_actor(original.actor_context_ref) == original
            if change == "child":
                identity.select_context(token, "C" * 43)
            elif change == "revoke":
                state.ended = True
            else:
                identity.logout(token)


@pytest.mark.parametrize(
    "field",
    [
        "session_binding",
        "display_label",
        "account_user_id",
        "school_id",
        "person_id",
        "guardian_relationship_id",
        "relationship_version",
        "authorization_version",
    ],
)
def test_release_compares_every_guardian_identity_field(field, monkeypatch):
    from app.composition.school_fact_read_evidence import validation_batch
    from app.platform.assistant.runtime import AssistantRuntimeError

    identity, _ = service()
    token = identity.login("maintainer")
    identity.select_context(token, "B" * 43)
    original = identity.resolve_request(token)
    if field in {"session_binding", "display_label"}:
        changed = replace(original, **{field: "different-value"})
    else:
        value = getattr(original.actor, field)
        value = UUID(int=value.int + 10) if isinstance(value, UUID) else value + 1
        actor_fields = asdict(original.actor) | {field: value}
        changed = replace(original, actor=_issue_guardian_actor_context(**actor_fields))
    with pytest.raises(AssistantRuntimeError):
        with validation_batch():
            assert identity.resolve_private_actor(original.actor_context_ref) == original
            # Simulate the trusted resolver returning another complete identity.
            monkeypatch.setattr(identity, "resolve_request", lambda _: changed)


def test_family_gateways_and_bound_evidence_share_identity_without_skipping_content():
    from app.composition.school_fact_directory_context import current_bound_actor_matches
    from app.composition.school_fact_directory_gateway import CombinedFactTurnGateway
    from app.composition.school_fact_family_leadership_gateway import FamilyLeadershipToolGateway
    from app.composition.school_fact_family_teaching_gateway import FamilyTeachingToolGateway
    from app.domains.students.family_read import FamilyReadEvidence
    from app.platform.assistant.tool_loop import ModelToolCall, ToolCallContext

    identity, state = service()
    token = identity.login("maintainer")
    identity.select_context(token, "B" * 43)
    original = identity.resolve_request(token)
    reference = original.actor_context_ref
    checked = []

    def valid(actor):
        checked.append(actor)
        return all(
            current_bound_actor_matches(actor, identity.resolve_private_actor) for _ in range(2)
        )

    query = SimpleNamespace(
        query=lambda **_: SimpleNamespace(
            public=lambda: {"status": "available"}, evidence=FamilyReadEvidence(valid)
        )
    )
    gateways = [
        gateway(resolve_actor=identity.resolve_private_actor, query=query).for_turn(reference)
        for gateway in (FamilyTeachingToolGateway, FamilyLeadershipToolGateway)
    ]
    combined = CombinedFactTurnGateway(*gateways)
    context = ToolCallContext(reference, "conversation", "turn", "run", 1, uuid4())
    checks = []
    for index, gateway in enumerate(gateways):
        with combined.validation_scope():
            output = combined.execute(
                ModelToolCall(call_id=str(index), name=gateway.descriptors[0].name, arguments={}),
                context,
            )
        checks.append(output.history_check)
    state.resolves = 0
    checked.clear()
    with combined.validation_scope():
        combined.revalidate(reference)
        for check in checks:
            assert check.valid() and check.refresh()
        assert state.resolves == 1
    assert state.resolves == 2
    assert len(checked) == 2
    state.ended = True
    with pytest.raises(ProductIdentityRejected):
        with combined.validation_scope():
            combined.revalidate(reference)


@pytest.mark.parametrize("change", ["none", "revoke", "child"])
def test_expired_guardian_choice_never_changes_child_or_revives_relationship(change):
    from tests.test_f1_product_identity import _expire_choices

    identity, state = service()
    token = identity.login("maintainer")
    identity.select_context(token, "B" * 43)
    original = identity.resolve_request(token)
    _expire_choices(identity, identity._redis, token)
    if change == "revoke":
        state.ended = True
    elif change == "child":
        identity.select_context(token, "C" * 43)
    if change == "none":
        assert identity.resolve_private_actor(original.actor_context_ref) == original
        assert identity.session_view(token).selected_context_ref == "B" * 43
    else:
        with pytest.raises(ProductIdentityRejected):
            identity.resolve_private_actor(original.actor_context_ref)


def test_selected_label_is_server_owned_and_invalid_selection_never_returns_it():
    identity, _ = service()
    token = identity.login("maintainer")
    with pytest.raises(ProductIdentityRejected):
        identity.resolve_request(token)
    for ref in ("B" * 43, "C" * 43):
        identity.select_context(token, ref)
        selected = identity.resolve_request(token)
        assert selected.display_label == next(
            c.label for c in identity.session_view(token).contexts if c.ref == ref
        )
        assert "display_label" not in repr(selected)
    identity.logout(token)
    with pytest.raises(ProductIdentityRejected):
        identity.resolve_private_actor(selected.actor_context_ref)


def test_actual_child_selection_change_has_only_server_dated_current_event():
    identity, _ = service()
    token = identity.login("maintainer")
    identity.select_context(token, "B" * 43)
    assert identity.resolve_request(token).selection_event is None
    before = datetime.now(UTC)
    identity.select_context(token, "C" * 43)
    selected = identity.resolve_request(token)
    event = selected.selection_event
    assert event is not None
    assert event.kind == "guardian_selection_changed"
    assert before <= event.occurred_at <= datetime.now(UTC)
    assert set(event.model_dump()) == {"kind", "occurred_at"}
    assert "selection_event" not in repr(selected)
    identity.select_context(token, "A" * 43)
    assert identity.resolve_request(token).selection_event is None


def test_child_switch_context_claim_is_bound_once_to_new_conversation_first_turn():
    identity, _ = service()
    token = identity.login("maintainer")
    assert identity.select_context(token, "B" * 43) is None
    claim = identity.select_context(token, "C" * 43)
    assert isinstance(claim, str) and len(claim) >= 32
    selected = identity.resolve_request(token)
    assert identity.session_view(token).selection_transition_ref is None
    assert identity.bind_selection_context(selected.actor_context_ref, claim, "D" * 43)
    assert identity.recent_selection_for_turn(selected.actor_context_ref, "D" * 43, 1)
    assert not identity.recent_selection_for_turn(selected.actor_context_ref, "D" * 43, 2)
    assert not identity.bind_selection_context(selected.actor_context_ref, claim, "E" * 43)
    assert not identity.recent_selection_for_turn(selected.actor_context_ref, "E" * 43, 1)


def test_child_switch_claim_does_not_cross_identity_or_survive_revocation():
    identity, state = service()
    token = identity.login("maintainer")
    identity.select_context(token, "C" * 43)
    previous = identity.resolve_request(token)
    claim = identity.select_context(token, "B" * 43)
    current = identity.resolve_request(token)
    assert claim is not None
    with pytest.raises(ProductIdentityRejected):
        identity.bind_selection_context(previous.actor_context_ref, claim, "D" * 43)
    assert identity.bind_selection_context(current.actor_context_ref, claim, "D" * 43)
    state.ended = True
    with pytest.raises(ProductIdentityRejected):
        identity.recent_selection_for_turn(current.actor_context_ref, "D" * 43, 1)


def test_child_switch_claim_expires_and_same_selection_does_not_reissue():
    identity, _ = service()
    token = identity.login("maintainer")
    identity.select_context(token, "B" * 43)
    claim = identity.select_context(token, "C" * 43)
    selected = identity.resolve_request(token)
    assert claim is not None
    assert identity.select_context(token, "C" * 43) is None
    session = identity._store.read(token)
    assert session is not None
    old = identity._current_state(session)
    assert old.selection_event is not None
    expired = old.model_copy(update={
        "selection_event": old.selection_event.model_copy(update={
            "occurred_at": datetime.now(UTC) - timedelta(minutes=2)
        })
    })
    assert identity._replace_state(token, session, old, expired)
    assert not identity.bind_selection_context(selected.actor_context_ref, claim, "D" * 43)


def test_bound_selection_context_expires_from_original_switch_time_not_claim_time():
    identity, _ = service()
    token = identity.login("maintainer")
    identity.select_context(token, "B" * 43)
    claim = identity.select_context(token, "C" * 43)
    selected = identity.resolve_request(token)
    assert claim is not None
    assert identity.bind_selection_context(selected.actor_context_ref, claim, "D" * 43)
    session = identity._store.read(token)
    assert session is not None
    bound = identity._current_state(session)
    assert bound.selection_event is not None
    delayed = bound.model_copy(update={
        "selection_event": bound.selection_event.model_copy(update={
            "occurred_at": datetime.now(UTC) - timedelta(minutes=2)
        })
    })
    assert identity._replace_state(token, session, bound, delayed)
    assert not identity.recent_selection_for_turn(selected.actor_context_ref, "D" * 43, 1)


def test_child_switch_claim_reaches_the_first_assistant_turn_through_real_http_boundaries():
    """No model call: exercise the same identity and conversation-creation path as the UI."""
    from fastapi import Request

    from app.core.request_id import assign_request_id
    from app.interfaces.http.assistant import AssistantRequestIdentity, create_assistant_router
    from app.platform.assistant.runtime import AssistantRuntime
    from tests.test_f1_product_identity import ORIGIN, _client

    identity, _ = service()
    client, identity_http = _client(identity)
    client.app.middleware("http")(assign_request_id)
    runtime = AssistantRuntime()
    bound: list[tuple[str, str, bool]] = []

    def assistant_identity(request: Request) -> AssistantRequestIdentity:
        return AssistantRequestIdentity(
            actor_context_ref=identity_http.request_dependency(request).actor_context_ref
        )

    def bind(actor: str, conversation: str, claim: str) -> None:
        bound.append(
            (actor, conversation, identity.bind_selection_context(actor, claim, conversation))
        )

    client.app.include_router(
        create_assistant_router(
            runtime=runtime,
            identity_dependency=assistant_identity,
            turn_orchestrator=None,  # type: ignore[arg-type] -- no turn is executed
            on_conversation_created=bind,
        )
    )
    base = "/api/v1/school-fact-product"
    with client:
        assert client.post(
            base + "/login", json={"account_ref": "maintainer"}, headers={"Origin": ORIGIN}
        ).status_code == 200
        assert client.post(
            base + "/context", json={"context_ref": "B" * 43}, headers={"Origin": ORIGIN}
        ).status_code == 200
        switched = client.post(
            base + "/context", json={"context_ref": "C" * 43}, headers={"Origin": ORIGIN}
        )
        assert switched.status_code == 200
        claim = switched.json()["selection_transition_ref"]
        assert isinstance(claim, str)
        conversation = client.post(
            "/api/v1/assistant/conversations",
            headers={"Origin": ORIGIN, "X-Conversation-Context-Claim": claim},
        )
        assert conversation.status_code == 201
        conversation_ref = conversation.json()["conversation_ref"]
        assert len(bound) == 1 and bound[0][1:] == (conversation_ref, True)
        first = runtime.accept_turn(conversation_ref, bound[0][0], uuid4(), "本轮省略目标")
        assert identity.recent_selection_for_turn(
            bound[0][0], conversation_ref, first.generation
        )
        another = client.post(
            "/api/v1/assistant/conversations", headers={"Origin": ORIGIN}
        )
        assert another.status_code == 201
        assert not identity.recent_selection_for_turn(
            bound[0][0], another.json()["conversation_ref"], 1
        )


def test_context_http_exposes_one_claim_only_on_explicit_child_switch_response():
    from tests.test_f1_product_identity import ORIGIN, _client

    identity, _ = service()
    client, _ = _client(identity)
    base = "/api/v1/school-fact-product"
    with client:
        login = client.post(
            base + "/login", json={"account_ref": "maintainer"}, headers={"Origin": ORIGIN}
        )
        assert login.status_code == 200
        first = client.post(
            base + "/context", json={"context_ref": "B" * 43}, headers={"Origin": ORIGIN}
        )
        assert first.status_code == 200 and first.json()["selection_transition_ref"] is None
        switched = client.post(
            base + "/context", json={"context_ref": "C" * 43}, headers={"Origin": ORIGIN}
        )
        claim = switched.json()["selection_transition_ref"]
        assert switched.status_code == 200 and isinstance(claim, str) and len(claim) >= 32
        assert client.get(base + "/session").json()["selection_transition_ref"] is None
        same = client.post(
            base + "/context", json={"context_ref": "C" * 43}, headers={"Origin": ORIGIN}
        )
        assert same.json()["selection_transition_ref"] is None


def test_reselection_refresh_and_new_conversation_never_invent_a_new_selection_event():
    from app.platform.assistant.runtime import AssistantRuntime
    from tests.test_f1_product_identity import _expire_choices

    identity, _ = service()
    token = identity.login("maintainer")
    identity.select_context(token, "B" * 43)
    identity.select_context(token, "C" * 43)
    original = identity.resolve_request(token)
    assert original.selection_event is not None
    identity.select_context(token, "C" * 43)
    _expire_choices(identity, identity._redis, token)
    identity.session_view(token)
    assert identity.resolve_request(token) == original
    runtime = AssistantRuntime()
    for _ in range(2):
        conversation = runtime.create_conversation(original.actor_context_ref, uuid4())
        assert conversation.model_safe_ledger == ()
        assert identity.resolve_private_actor(conversation.actor_context_ref) == original


def test_selection_event_is_included_in_final_identity_revalidation(monkeypatch):
    from app.composition.school_fact_read_evidence import validation_batch
    from app.platform.assistant.runtime import AssistantRuntimeError

    identity, _ = service()
    token = identity.login("maintainer")
    identity.select_context(token, "B" * 43)
    identity.select_context(token, "C" * 43)
    original = identity.resolve_request(token)
    assert original.selection_event is not None
    changed = replace(original, selection_event=None)
    with pytest.raises(AssistantRuntimeError):
        with validation_batch():
            assert identity.resolve_private_actor(original.actor_context_ref) == original
            monkeypatch.setattr(identity, "resolve_request", lambda _: changed)


def test_selection_event_is_cleared_when_current_relationship_is_lost():
    identity, state = service()
    token = identity.login("maintainer")
    identity.select_context(token, "C" * 43)
    identity.select_context(token, "B" * 43)
    original = identity.resolve_request(token)
    assert original.selection_event is not None
    state.ended = True
    assert identity.session_view(token).selected_context_ref is None
    with pytest.raises(ProductIdentityRejected):
        identity.resolve_private_actor(original.actor_context_ref)
    identity.select_context(token, "C" * 43)
    assert identity.resolve_request(token).selection_event is None
