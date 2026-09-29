"""One family dialogue, independently authorized children, retained domain boundaries."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.composition.school_fact_product_identity import ProductIdentityRejected
from app.platform.assistant.runtime import AssistantRuntimeError
from app.platform.assistant.tool_loop import ModelToolCall, ToolCallContext
from tests.test_f3_product_identity import guardian, service


def family():
    identity, state = service()
    token = identity.login("maintainer")
    identity.select_context(token, "B" * 43)
    principal = identity.resolve_assistant_request(token)
    return identity, state, token, principal


def test_family_principal_has_two_targets_without_changing_selected_child():
    identity, _, token, principal = family()
    children = identity.family_children(principal.actor_context_ref)
    assert len(children) == 2
    assert children[0].is_default
    second = identity.resolve_family_child(principal.actor_context_ref, children[1].child_ref)
    assert second.actor == guardian(2)
    assert identity.resolve_private_actor(second.actor_context_ref) == second
    assert identity.resolve_request(token).actor == guardian(1)
    assert principal.actor_context_ref not in repr(children)


def test_revoking_default_child_does_not_revoke_other_child_or_change_old_evidence():
    identity, state, token, principal = family()
    children = identity.family_children(principal.actor_context_ref)
    first = identity.resolve_family_child(principal.actor_context_ref, children[0].child_ref)
    second = identity.resolve_family_child(principal.actor_context_ref, children[1].child_ref)
    state.ended = True
    assert identity.resolve_assistant_request(token) == principal
    assert identity.resolve_assistant_identity(principal.actor_context_ref) == principal
    with pytest.raises(ProductIdentityRejected):
        identity.resolve_private_actor(first.actor_context_ref)
    assert identity.resolve_private_actor(second.actor_context_ref) == second
    assert [item.child_ref for item in identity.family_children(principal.actor_context_ref)] == [
        children[1].child_ref
    ]
    assert identity.session_view(token).status == "signed_in"
    assert identity.resolve_assistant_request(token) == principal


@pytest.mark.parametrize("action", ["logout", "staff", "other_session"])
def test_family_target_cannot_survive_logout_or_staff_switch_or_cross_session(action):
    identity, _, token, principal = family()
    child = identity.family_children(principal.actor_context_ref)[1]
    derived = identity.resolve_family_child(principal.actor_context_ref, child.child_ref)
    if action == "logout":
        identity.logout(token)
    elif action == "staff":
        identity.select_context(token, "A" * 43)
    else:
        other_token = identity.login("maintainer")
        identity.select_context(other_token, "B" * 43)
        other = identity.resolve_assistant_request(other_token)
        with pytest.raises(ProductIdentityRejected):
            identity.resolve_family_child(other.actor_context_ref, child.child_ref)
        return
    with pytest.raises(ProductIdentityRejected):
        identity.resolve_assistant_identity(principal.actor_context_ref)
    with pytest.raises(ProductIdentityRejected):
        identity.resolve_private_actor(derived.actor_context_ref)


def test_family_ui_focus_changes_do_not_grant_or_replace_conversation_target():
    identity, _, token, principal = family()
    display_key = identity.session_view(token).assistant_context_key
    assert display_key and principal.actor_context_ref not in display_key
    children = identity.family_children(principal.actor_context_ref)
    child_a = identity.resolve_family_child(principal.actor_context_ref, children[0].child_ref)
    identity.select_context(token, "C" * 43)
    assert identity.resolve_assistant_request(token) == principal
    assert identity.session_view(token).assistant_context_key == display_key
    assert identity.resolve_private_actor(child_a.actor_context_ref) == child_a
    assert [item.child_ref for item in identity.family_children(principal.actor_context_ref)
            if item.is_default] == [children[1].child_ref]
    identity.select_context(token, "A" * 43)
    assert identity.session_view(token).assistant_context_key != display_key


def test_family_gateway_delegates_targets_and_preserves_subject_in_history():
    from app.composition.school_fact_family_children_gateway import FamilyChildrenToolGateway
    from app.composition.school_fact_family_gateway import FamilyProfileToolGateway
    from app.domains.students.family_read import FamilyQueryResult, FamilyReadEvidence

    identity, state, _, principal = family()
    queried = []

    class Query:
        def query(self, *, actor, as_of):
            queried.append(actor.guardian_relationship_id)
            name = "林同学" if actor == guardian(1) else "程同学"
            return FamilyQueryResult(
                status="available", profile={"name": name}, sources=(), missing=(),
                as_of=datetime.now(UTC), queried_at=datetime.now(UTC),
                relationship_kind="verified_guardian",
                evidence=FamilyReadEvidence(lambda current: current == actor),
            )

    gateway = FamilyChildrenToolGateway(
        identity=identity,
        gateways=(FamilyProfileToolGateway(resolve_actor=identity.resolve_private_actor,
                                           query=Query()),),
    ).for_turn(principal.actor_context_ref)
    context = ToolCallContext(
        principal.actor_context_ref, "conversation", "turn", "run", 1, uuid4()
    )
    result = gateway.execute(ModelToolCall(call_id="children", name="school_facts.family.children",
                                          arguments={}), context)
    children = result.model_content["children"]
    checks = []
    for index in (0, 1, 1):
        output = gateway.execute(ModelToolCall(
            call_id=str(index), name="school_facts.family.profile",
            arguments={"child_ref": children[index]["child_ref"], "fields": ["name"]},
        ), context)
        assert output.model_content["subject"]["child_ref"] == children[index]["child_ref"]
        checks.append(output.history_check)
    assert queried == [guardian(1).guardian_relationship_id, guardian(2).guardian_relationship_id,
                       guardian(2).guardian_relationship_id]
    state.ended = True
    assert not checks[0].valid()
    assert checks[1].valid()
    with pytest.raises(AssistantRuntimeError):
        gateway.revalidate(principal.actor_context_ref)


def test_missing_child_target_returns_candidates_not_ui_default():
    from app.composition.school_fact_family_children_gateway import FamilyChildrenToolGateway
    from app.composition.school_fact_family_gateway import FamilyProfileToolGateway

    identity, _, _, principal = family()
    gateway = FamilyChildrenToolGateway(
        identity=identity,
        gateways=(FamilyProfileToolGateway(resolve_actor=identity.resolve_private_actor,
                                           query=None),),
    ).for_turn(principal.actor_context_ref)
    context = ToolCallContext(
        principal.actor_context_ref, "conversation", "turn", "run", 1, uuid4()
    )
    output = gateway.execute(ModelToolCall(call_id="q", name="school_facts.family.profile",
                                          arguments={"fields": ["name"]}), context)
    assert output.model_content["status"] == "child_selection_required"
    assert len(output.model_content["children"]) == 2


def test_parent_only_login_enters_family_dialogue_without_manual_child_selection():
    identity, _ = service()
    factory = identity._resolver_factory

    def parent_only(user, session):
        resolver = factory(user, session)
        original = resolver.list_options
        resolver.list_options = lambda **kwargs: tuple(
            item for item in original(**kwargs) if item.kind == "guardian_relationship"
        )
        return resolver

    identity._resolver_factory = parent_only
    token = identity.login("maintainer")
    assert identity.session_view(token).family_conversation_available
    reference = identity.resolve_assistant_request(token).actor_context_ref
    assert len(identity.family_children(reference)) == 2


def test_family_catalog_three_children_keeps_all_and_excludes_other_school():
    from dataclasses import asdict

    from app.platform.contracts import _issue_guardian_actor_context
    from app.platform.identity.execution_context import SelectedContextRef

    identity, _, token, principal = family()
    factory = identity._resolver_factory

    def expanded(user, session):
        resolver = factory(user, session)
        original_list, original_resolve = resolver.list_options, resolver.resolve

        def options(**kwargs):
            base = original_list(**kwargs)
            return base + tuple(SelectedContextRef(
                opaque_ref=ref * 43, kind="guardian_relationship", display_label=label,
                school_label="合成学校", effective_until=base[0].effective_until,
            ) for ref, label in (("D", "第三个孩子"), ("E", "另一学校")))

        def resolve(ref):
            if ref == "D" * 43:
                return guardian(3)
            if ref == "E" * 43:
                return _issue_guardian_actor_context(
                    **(asdict(guardian(4)) | {"school_id": uuid4()})
                )
            return original_resolve(ref)

        resolver.list_options, resolver.resolve = options, resolve
        return resolver

    identity._resolver_factory = expanded
    choices = identity.family_children(principal.actor_context_ref)
    assert len(choices) == 3
    assert all("另一学校" not in item.label for item in choices)
    assert identity.resolve_request(token).actor == guardian(1)


def test_unrelated_actor_cannot_supply_staff_or_arbitrary_child_ref():
    identity, _, token, principal = family()
    for target in ("A" * 43, "forged" * 8, {"child": "B" * 43}):
        with pytest.raises(ProductIdentityRejected):
            identity.resolve_family_child(principal.actor_context_ref, target)
    identity.select_context(token, "A" * 43)
    staff = identity.resolve_assistant_request(token)
    with pytest.raises(ProductIdentityRejected):
        identity.family_children(staff.actor_context_ref)


def test_child_catalog_storage_fault_remains_service_unavailable(monkeypatch):
    from redis.exceptions import RedisError

    from app.composition.school_fact_product_identity import ProductIdentityUnavailable

    identity, _, _, principal = family()

    def failed(*_args):
        raise RedisError("internal storage exception")

    monkeypatch.setattr(identity._redis, "setex", failed)
    with pytest.raises(ProductIdentityUnavailable):
        identity.family_children(principal.actor_context_ref)


def test_derived_child_is_revalidated_at_synchronous_release_end():
    from app.composition.school_fact_read_evidence import validation_batch

    identity, state, _, principal = family()
    child = identity.family_children(principal.actor_context_ref)[0]
    target = identity.resolve_family_child(principal.actor_context_ref, child.child_ref)
    with pytest.raises(AssistantRuntimeError):
        with validation_batch():
            assert identity.resolve_private_actor(target.actor_context_ref) == target
            state.ended = True


@pytest.mark.parametrize("change", ["focus", "logout", "account_version", "school", "staff"])
def test_family_scope_final_check_distinguishes_focus_race_from_identity_change(
    change, monkeypatch,
):
    from dataclasses import asdict

    from app.platform.contracts import _issue_guardian_actor_context

    identity, _, token, principal = family()
    initial_state = identity._current_state
    armed = True

    def race(session):
        nonlocal armed
        before = initial_state(session)
        if not armed:
            return before
        armed = False
        if change == "focus":
            identity.select_context(token, "C" * 43)
        elif change == "logout":
            identity.logout(token)
        elif change == "account_version":
            original_load = identity._load_account

            def changed_account(account):
                from dataclasses import replace

                return replace(original_load(account), security_version=2)

            monkeypatch.setattr(identity, "_load_account", changed_account)
        elif change == "school":
            factory = identity._resolver_factory

            def another_school(user, session):
                resolver = factory(user, session)
                previous = resolver.resolve
                resolver.resolve = lambda ref: (
                    _issue_guardian_actor_context(**(asdict(guardian(2)) | {"school_id": uuid4()}))
                    if ref == "C" * 43 else previous(ref)
                )
                return resolver

            monkeypatch.setattr(identity, "_resolver_factory", another_school)
            identity.select_context(token, "C" * 43)
        else:
            identity.select_context(token, "A" * 43)
        return before

    monkeypatch.setattr(identity, "_current_state", race)
    if change == "focus":
        assert identity.resolve_assistant_identity(principal.actor_context_ref) == principal
    else:
        with pytest.raises(ProductIdentityRejected):
            identity.resolve_assistant_identity(principal.actor_context_ref)


def test_assistant_http_uses_family_scope_while_direct_http_retains_child_context():
    from fastapi import Request

    from tests.test_f1_product_identity import ORIGIN, _client

    identity, _ = service()
    client, http = _client(identity)

    @client.app.get("/api/v1/assistant/family-scope-test")
    def endpoint(request: Request):
        actor = http.assistant_request_dependency(request)
        return {"kind": type(actor.actor).__name__}

    with client:
        base = "/api/v1/school-fact-product"
        assert client.post(base + "/login", json={"account_ref": "maintainer"},
                           headers={"Origin": ORIGIN}).status_code == 200
        assert client.post(base + "/context", json={"context_ref": "B" * 43},
                           headers={"Origin": ORIGIN}).status_code == 200
        assert client.get("/api/v1/assistant/family-scope-test").json() == {
            "kind": "ProductFamilyActor"
        }


@pytest.mark.parametrize("name,args", [
    ("school_facts.family.education", {"fields": ["class"]}),
    ("school_facts.family.teaching", {}),
    ("school_facts.family.residence", {}),
    ("school_facts.family.leadership", {}),
])
def test_existing_education_teaching_residence_and_leadership_keep_domain_readers(name, args):
    from types import SimpleNamespace

    from app.composition.school_fact_family_children_gateway import FamilyChildrenToolGateway
    from app.composition.school_fact_family_gateway import FamilyProfileToolGateway
    from app.composition.school_fact_family_leadership_gateway import FamilyLeadershipToolGateway
    from app.composition.school_fact_family_teaching_gateway import FamilyTeachingToolGateway
    from app.composition.school_fact_residence_gateway import FamilyResidenceToolGateway
    from app.domains.students.family_read import FamilyReadEvidence

    identity, _, _, principal = family()
    seen = []

    def query(*, actor, **kwargs):
        seen.append(actor)
        return SimpleNamespace(public=lambda: {"status": "available"},
                               evidence=FamilyReadEvidence(lambda current: current == actor))

    reader = SimpleNamespace(query=query, current_read_eligibility=lambda _actor: "available")
    owners = (
        FamilyProfileToolGateway(resolve_actor=identity.resolve_private_actor, query=reader,
                                 education=reader),
        FamilyTeachingToolGateway(resolve_actor=identity.resolve_private_actor, query=reader),
        FamilyResidenceToolGateway(resolve_actor=identity.resolve_private_actor, query=reader),
        FamilyLeadershipToolGateway(resolve_actor=identity.resolve_private_actor, query=reader),
    )
    wrapper = FamilyChildrenToolGateway(
        identity=identity, gateways=owners,
        school_tool_names=frozenset({"school_facts.family.leadership"}),
    ).for_turn(principal.actor_context_ref)
    choice = identity.family_children(principal.actor_context_ref)[1]
    context = ToolCallContext(principal.actor_context_ref, "dialogue", "turn", "run", 1, uuid4())
    arguments = args if name.endswith("leadership") else {**args, "child_ref": choice.child_ref}
    result = wrapper.execute(ModelToolCall(call_id="q", name=name, arguments=arguments), context)
    assert result.history_check.valid()
    if name.endswith("leadership"):
        assert "child_ref" not in result.model_content["subject"]
        assert "child_ref" not in next(d for d in wrapper.descriptors if d.name == name).parameters[
            "properties"
        ]
    else:
        assert seen == [guardian(2)]
        assert result.model_content["subject"]["child_ref"] == choice.child_ref


def leadership_family(caregivers=(1,)):
    """Real leadership owner and single-child identity; only repository I/O is in memory."""
    from copy import deepcopy
    from datetime import timedelta
    from types import SimpleNamespace
    from uuid import UUID

    from app.composition.school_fact_directory_context import current_bound_actor_matches
    from app.composition.school_fact_family_children_gateway import FamilyChildrenToolGateway
    from app.composition.school_fact_family_leadership_gateway import FamilyLeadershipToolGateway
    from app.composition.school_fact_family_leadership_query import FamilyLeadershipQueryService
    from app.domains.students.family_read import ParentReadBasis

    identity, identity_state, token, principal = family()
    now = datetime.now(UTC)
    state = SimpleNamespace(bases={}, reads=[], authority_reads=[], unavailable=set(), now=now)
    for number in (1, 2):
        actor = guardian(number)
        state.bases[actor.guardian_relationship_id] = ParentReadBasis(
            school_id=actor.school_id, adult_person_id=actor.person_id,
            relationship_id=actor.guardian_relationship_id, relationship_version=1,
            student_id=UUID(int=100 + number), student_version=1,
            relationship_kind=(
                "recognized_caregiver" if number in caregivers else "verified_guardian"
            ),
            valid_from=now - timedelta(days=1), valid_to=now + timedelta(days=1),
        )

    class Repository:
        def authority(self, actor):
            from sqlalchemy.exc import SQLAlchemyError

            state.authority_reads.append(actor.guardian_relationship_id)
            if actor.guardian_relationship_id in state.unavailable:
                raise SQLAlchemyError("private diagnostic must not be disclosed")
            return state.bases.get(actor.guardian_relationship_id)

        def leadership_read(self, actor, *, role_kind, include_official_contacts):
            state.reads.append((actor, role_kind, include_official_contacts))
            basis = state.bases[actor.guardian_relationship_id]
            return deepcopy({
                "status": "available", "student_id": str(basis.student_id),
                "student_version": basis.student_version,
                "relationship_version": basis.relationship_version,
                "leaders": [{"display_name": "合成校区负责人", "position_title": "校区负责人",
                             "role_kind": "campus_leader", "organization": "合成办公室",
                             "campuses": ["合成乙校区"], "official_contacts": [], "source": {}}],
                "quality": [], "targets": [], "configuration": [{"version": 1}],
            })

    query = FamilyLeadershipQueryService(
        repository=Repository(),
        current_actor=lambda actor: current_bound_actor_matches(
            actor, identity.resolve_private_actor,
        ),
        clock=lambda: state.now,
    )
    state.query = query
    wrapper = FamilyChildrenToolGateway(
        identity=identity,
        gateways=(FamilyLeadershipToolGateway(resolve_actor=identity.resolve_private_actor,
                                             query=query),),
        school_tool_names=frozenset({"school_facts.family.leadership"}),
    )
    context = ToolCallContext(principal.actor_context_ref, "dialogue", "turn", "run", 1, uuid4())

    def execute(arguments=None):
        return wrapper.for_turn(principal.actor_context_ref).execute(ModelToolCall(
            call_id="leaders", name="school_facts.family.leadership", arguments=arguments or {},
        ), context)

    return identity, identity_state, token, principal, state, execute


@pytest.mark.parametrize("reverse", [False, True])
def test_leadership_uses_eligible_guardian_regardless_of_caregiver_candidate_order(
    reverse, monkeypatch,
):
    identity, _, _, _, state, execute = leadership_family()
    if reverse:
        original = identity.family_children
        monkeypatch.setattr(identity, "family_children", lambda ref: tuple(reversed(original(ref))))
    result = execute({"role_kind": "campus_leader"})
    assert result.model_content["status"] == "available"
    assert result.model_content["leaders"][0]["campuses"] == ["合成乙校区"]
    assert "child_ref" not in result.model_content["subject"]
    assert state.reads == [(guardian(2), "campus_leader", False)]
    assert result.history_check.valid()
    assert all(actor == guardian(2) for actor, _, _ in state.reads)


@pytest.mark.parametrize("change", ["caregiver_only", "revoked", "expired", "other_school"])
def test_leadership_no_eligible_basis_never_reads_the_school_directory(change):
    from dataclasses import replace

    _, _, _, _, state, execute = leadership_family()
    key = guardian(2).guardian_relationship_id
    if change == "revoked":
        state.bases[key] = None
    elif change == "expired":
        state.bases[key] = replace(state.bases[key], valid_to=state.now)
    elif change == "other_school":
        state.bases[key] = replace(state.bases[key], school_id=uuid4())
    else:
        state.bases[key] = replace(state.bases[key], relationship_kind="recognized_caregiver")
    assert execute().model_content["status"] == "denied"
    assert state.reads == []


def test_leadership_authority_fault_is_not_reported_as_all_relationships_denied():
    _, _, _, _, state, execute = leadership_family()
    state.unavailable.add(guardian(2).guardian_relationship_id)
    result = execute()
    assert result.model_content["status"] == "unavailable"
    assert "private diagnostic" not in str(result.model_content)
    assert state.reads == []


@pytest.mark.parametrize("change", ["revoke", "expiry", "logout"])
def test_selected_leadership_basis_remains_live_for_history_and_release(change):
    identity, _, token, _, state, execute = leadership_family()
    result = execute()
    assert result.model_content["status"] == "available"
    assert result.history_check.valid()
    key = guardian(2).guardian_relationship_id
    if change == "revoke":
        state.bases[key] = None
    elif change == "expiry":
        state.now = state.bases[key].valid_to
    else:
        identity.logout(token)
    assert not result.history_check.valid()


def test_unavailable_first_basis_does_not_hide_independently_valid_second_basis():
    _, _, _, _, state, execute = leadership_family(caregivers=())
    state.unavailable.add(guardian(1).guardian_relationship_id)
    result = execute()
    assert result.model_content["status"] == "available"
    assert state.reads == [(guardian(2), "all", False)]
    assert result.history_check.valid()


def test_same_school_leadership_does_not_use_another_schools_guardian_basis(monkeypatch):
    from dataclasses import asdict

    from app.platform.contracts import _issue_guardian_actor_context
    from app.platform.identity.execution_context import ContextNoLongerValid

    identity, _, _, _, state, execute = leadership_family()
    factory = identity._resolver_factory
    other_actor = _issue_guardian_actor_context(**(asdict(guardian(2)) | {"school_id": uuid4()}))

    def cross_school(user, session):
        resolver = factory(user, session)
        old_resolve, old_selected = resolver.resolve, resolver.resolve_selected
        resolver.resolve = lambda ref: other_actor if ref == "C" * 43 else old_resolve(ref)

        def selected(current):
            if current.context_id == other_actor.guardian_relationship_id:
                raise ContextNoLongerValid("school relationship changed")
            return old_selected(current)

        resolver.resolve_selected = selected
        return resolver

    monkeypatch.setattr(identity, "_resolver_factory", cross_school)
    assert execute().model_content["status"] == "denied"
    assert state.reads == []
    assert guardian(2).guardian_relationship_id not in state.authority_reads


@pytest.mark.parametrize("change", ["revoke", "expiry"])
def test_school_basis_selection_does_not_replace_final_result_release_check(change):
    from app.composition.school_fact_read_evidence import validation_batch

    _, _, _, _, state, execute = leadership_family()
    result = execute()
    key = guardian(2).guardian_relationship_id
    with pytest.raises(AssistantRuntimeError):
        with validation_batch():
            assert result.history_check.valid()
            if change == "revoke":
                state.bases[key] = None
            else:
                state.now = state.bases[key].valid_to


def test_school_directory_does_not_take_an_unexposed_child_selector():
    identity, _, _, principal, state, execute = leadership_family()
    child = identity.family_children(principal.actor_context_ref)[1]
    result = execute({"child_ref": child.child_ref})
    assert result.model_content["status"] == "unsupported_selection"
    assert state.reads == state.authority_reads == []


def test_school_relation_change_after_selection_is_rechecked_before_content_read(monkeypatch):
    _, _, _, _, state, execute = leadership_family()
    check = state.query.current_read_eligibility

    def changed_after_selection(actor):
        result = check(actor)
        if result == "available":
            state.bases[actor.guardian_relationship_id] = None
        return result

    monkeypatch.setattr(state.query, "current_read_eligibility", changed_after_selection)
    result = execute()
    assert result.model_content["status"] == "denied"
    assert state.reads == []
