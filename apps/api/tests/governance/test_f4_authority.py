from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

import app.platform.contracts as contexts
from app.platform.authorization.thin_service import ThinAuthorizationService
from app.platform.governance.contracts import (
    GovernanceAuthorizationBasis,
    GovernanceDependencyVersion,
)
from app.platform.school_facts.f4_policy import F4FactPolicy, F4FactScope

NOW = datetime(2026, 9, 13, tzinfo=UTC)


def test_single_receipt_authorizes_only_its_exact_family_and_objects():
    from app.composition.school_fact_education_execution import EducationOwnResultAuthorizer

    auth, governance = authority()
    context = contexts._issue_command_execution_context(
        actor=actor(), request_id=uuid4(), idempotency_key="exact-own-receipt"
    )
    auth.clock = lambda: NOW
    requested = frozenset({("education_class", "class:a")})

    class Never:
        def __call__(self, context):
            pytest.fail("unrelated retained scopes must not be read")

        def for_targets(self, context, targets):
            pytest.fail("unrelated retained scopes must not be read")

    composite = EducationOwnResultAuthorizer(education=auth.own_result, retained=Never())
    proof = composite.for_targets(context, requested)
    assert proof.allowed_targets == requested
    # Receipt metadata admits current read targets and creation-only maintenance
    # targets separately; the latter never grants current content access.
    assert governance.calls == 2
    assert {permit.action for permit in proof.authorizations[0].permits} == {"read", "maintain"}
    assert proof.creation_only_targets == frozenset()
    proof.revalidate()
    governance.valid = False
    with pytest.raises(PermissionError):
        proof.revalidate()


@pytest.mark.parametrize(
    "targets",
    [
        frozenset({("education_class", "class:outside")}),
        frozenset({("education_class", "class:a"), ("education_class", "class:outside")}),
        frozenset({("student_class_position", "class:new")}),
    ],
)
def test_single_receipt_metadata_never_expands_read_or_creation_scope(targets):
    auth, _ = authority()
    auth.clock = lambda: NOW
    context = contexts._issue_command_execution_context(
        actor=actor(), request_id=uuid4(), idempotency_key="exact-own-receipt"
    )
    with pytest.raises(PermissionError):
        auth.own_result(context, targets=targets)


def test_single_receipt_creation_metadata_requires_current_exact_maintenance_scope():
    auth, governance = authority()
    context = contexts._issue_command_execution_context(
        actor=actor(), request_id=uuid4(), idempotency_key="exact-own-creation-receipt"
    )
    targets = frozenset({("education_class", "class:new")})
    proof = auth.own_result(context, targets=targets)
    assert proof.allowed_targets == targets and proof.creation_only_targets == targets
    assert {permit.action for permit in proof.permits} == {"read", "maintain"}
    assert governance.calls == 2
    read = next(permit for permit in proof.permits if permit.action == "read")
    with pytest.raises(PermissionError):
        auth.require_object(read, "class:new", is_creation=False)
    proof.revalidate()
    governance.valid = False
    with pytest.raises(PermissionError):
        proof.revalidate()
    governance.valid = True
    del auth.bindings[actor().assignment_id, "school_facts.f4.education_class.maintain"]
    with pytest.raises(PermissionError):
        auth.own_result(context, targets=targets)


def test_school_receipt_requires_selected_target_and_keeps_creation_separate():
    auth, governance = authority()
    governance.scope = F4FactScope(
        UUID(int=1),
        "education_class",
        field_names=("display_name",),
        coverage="school",
    )
    context = contexts._issue_command_execution_context(
        actor=actor(), request_id=uuid4(), idempotency_key="school-own-receipt"
    )
    with pytest.raises(PermissionError):
        auth.own_result(context)
    selected = frozenset({("education_class", "class:new-later")})
    proof = auth.own_result(context, targets=selected)
    assert proof.allowed_targets == selected
    assert proof.creation_only_targets == frozenset()


def actor():
    return contexts._issue_staff_actor_context(
        account_user_id=1,
        school_id=contexts.SchoolId(UUID(int=1)),
        person_id=contexts.PersonId(UUID(int=2)),
        assignment_id=contexts.AssignmentId(UUID(int=3)),
        assignment_version=1,
        authorization_version=2,
    )


class Governance:
    def __init__(self, scope, sid):
        self.scope, self.sid, self.valid, self.calls = scope, sid, True, 0

    def issue(self, query):
        self.calls += 1
        a = actor()
        return GovernanceAuthorizationBasis(
            a.school_id,
            a.assignment_id,
            query.duty_code,
            self.sid,
            uuid4(),
            1,
            a.authorization_version,
            NOW,
            NOW + timedelta(days=7),
            (
                GovernanceDependencyVersion("school_facts.assignment", str(a.assignment_id), 1),
                GovernanceDependencyVersion("school_facts.person", str(a.person_id), 1),
                GovernanceDependencyVersion(
                    "school_facts.f4.scope-target", self.scope.reference, 1
                ),
            ),
        )

    def validate(self, basis, *, as_of):
        return self.valid and NOW <= as_of < NOW + timedelta(days=7)


def authority():
    from app.composition.school_fact_education_authority import EducationAuthority
    from app.composition.school_fact_education_settings import (
        EducationConfiguration,
        EducationScopeBinding,
    )

    sid = uuid4()
    scope = F4FactScope(
        UUID(int=1),
        "education_class",
        object_keys=("class:a", "class:b"),
        creation_keys=("class:new",),
        field_names=("display_name", "valid_to"),
    )
    g = Governance(scope, sid)
    thin = ThinAuthorizationService(
        governance_basis_service=g, module_policies={"school_facts.f4": F4FactPolicy()}
    )
    config = EducationConfiguration(
        approval_reference="F4-approved",
        scope_bindings=tuple(
            EducationScopeBinding(
                assignment_id=actor().assignment_id,
                operation_code=f"school_facts.f4.education_class.{action}",
                scope_id=sid,
            )
            for action in ("read", "maintain")
        ),
    )
    return EducationAuthority(thin=thin, configuration=config, clock=lambda: NOW), g


def test_collection_gets_one_current_scope_for_many_objects_but_never_creation_keys():
    auth, g = authority()
    permit = auth.require_scope(actor(), fact_kind="education_class", action="read", checked_at=NOW)
    assert permit.scope.object_keys == ("class:a", "class:b")
    assert g.calls == 1
    auth.require_fields(permit, ("display_name",))
    with pytest.raises(PermissionError):
        auth.require_fields(permit, ("private_phone",))
    with pytest.raises(PermissionError):
        auth.require_object(permit, "class:new", is_creation=False)


def test_maintenance_does_not_convert_existing_or_read_scope_into_creation():
    auth, _ = authority()
    permit = auth.require_scope(
        actor(), fact_kind="education_class", action="maintain", checked_at=NOW
    )
    auth.require_object(permit, "class:new", is_creation=True)
    with pytest.raises(PermissionError):
        auth.require_object(permit, "class:a", is_creation=True)
    read = auth.require_scope(actor(), fact_kind="education_class", action="read", checked_at=NOW)
    with pytest.raises(PermissionError):
        auth.require_object(read, "class:new", is_creation=True)


def test_result_reuse_rechecks_current_actor_revocation_and_deadline():
    auth, g = authority()
    permit = auth.require_scope(actor(), fact_kind="education_class", action="read", checked_at=NOW)
    auth.revalidate(permit, actor=actor(), checked_at=NOW + timedelta(seconds=2))
    g.valid = False
    with pytest.raises(PermissionError):
        auth.revalidate(permit, actor=actor(), checked_at=NOW + timedelta(seconds=3))
    g.valid = True
    with pytest.raises(PermissionError):
        auth.revalidate(permit, actor=actor(), checked_at=NOW + timedelta(days=7))


def test_configuration_hints_do_not_grant_rights_for_unbound_actor_or_kind():
    auth, _ = authority()
    with pytest.raises(PermissionError):
        auth.require_scope(
            actor(), fact_kind="student_class_position", action="maintain", checked_at=NOW
        )


def test_full_timeline_replacement_requires_each_segment_field_not_just_end_time():
    import json

    from app.platform.school_facts.contracts import CanonicalFactChangeSetV1

    auth, _ = authority()
    document = {
        "schema_version": 3,
        "source_sha256": "a" * 64,
        "base_version": 1,
        "changes": [
            {
                "schema_version": 3,
                "origin": {"sheet_code": "test", "row_number": 1},
                "fact_kind": "education_class",
                "source_key": "class:a",
                "operation": "correct",
                "expected_version": 1,
                "effective_at": NOW.isoformat(),
                "content": None,
                "correction_reason": "核对完整班级历史",
                "replacement_segments": [
                    {
                        "effective_at": NOW.isoformat(),
                        "valid_to": None,
                        "content": {"display_name": "正式班名", "class_number": 3},
                    }
                ],
            }
        ],
    }
    # A class's business code stays independently protected during whole-history correction.
    document["changes"][0]["replacement_segments"][0]["content"] = {
        "display_name": "正式班名",
        "business_code": "CLASS-PROTECTED",
    }
    batch = CanonicalFactChangeSetV1.model_validate_json(json.dumps(document))
    with pytest.raises(PermissionError):
        auth.require_change_set(actor(), batch, checked_at=NOW)


def test_result_permits_use_one_fresh_batch_and_do_not_cache_across_releases():
    from app.composition.school_fact_product_services import ProductThinAuthorizationService

    auth, governance = authority()
    batches = []

    def validate_many(bases, *, as_of):
        batches.append((bases, as_of))
        return tuple(governance.validate(b, as_of=as_of) for b in bases)

    governance.validate_many = validate_many
    auth.thin = ProductThinAuthorizationService(
        governance_basis_service=governance,
        module_policies={"school_facts.f4": F4FactPolicy()},
    )
    permit = auth.require_scope(actor(), fact_kind="education_class", action="read", checked_at=NOW)
    auth.revalidate_many((permit, permit), actor=actor(), checked_at=NOW)
    assert len(batches) == 1 and len(batches[0][0]) == 1
    governance.valid = False
    with pytest.raises(PermissionError):
        auth.revalidate_many((permit,), actor=actor(), checked_at=NOW + timedelta(seconds=1))
    assert len(batches) == 2
    governance.valid = True
    with pytest.raises(PermissionError):
        auth.revalidate_many((permit,), actor=actor(), checked_at=NOW + timedelta(days=7))


def test_result_permit_batch_rejects_actor_or_incomplete_decisions_before_release():
    from dataclasses import replace
    from types import SimpleNamespace

    auth, _ = authority()
    permit = auth.require_scope(actor(), fact_kind="education_class", action="read", checked_at=NOW)
    foreign = replace(
        permit,
        actor=contexts._issue_staff_actor_context(
            account_user_id=2,
            school_id=actor().school_id,
            person_id=actor().person_id,
            assignment_id=actor().assignment_id,
            assignment_version=1,
            authorization_version=2,
        ),
    )
    calls = []
    auth.thin = SimpleNamespace(revalidate_many=lambda *a, **k: calls.append(1) or ())
    with pytest.raises(PermissionError):
        auth.revalidate_many((permit, foreign), actor=actor(), checked_at=NOW)
    assert calls == []
    with pytest.raises(PermissionError):
        auth.revalidate_many((permit,), actor=actor(), checked_at=NOW)
    assert calls == [1]


def test_release_batch_deduplicates_exact_permit_but_rechecks_fresh_on_exit_and_next_batch():
    from unittest.mock import Mock

    from app.composition.school_fact_read_evidence import validation_batch

    auth, g = authority()
    auth.clock = lambda: NOW
    permit = auth.require_scope(actor(), fact_kind="education_class", action="read", checked_at=NOW)
    original = auth.revalidate
    auth.revalidate = Mock(wraps=original)
    with validation_batch():
        auth.revalidate_many((permit,), actor=actor(), checked_at=NOW)
        auth.revalidate_many((permit,), actor=actor(), checked_at=NOW)
        assert auth.revalidate.call_count == 1
    assert auth.revalidate.call_count == 2
    with validation_batch():
        auth.revalidate_many((permit,), actor=actor(), checked_at=NOW)
    assert auth.revalidate.call_count == 4


@pytest.mark.parametrize("change", ["revocation", "expiry"])
def test_release_batch_rejects_changes_after_first_valid_check(change):
    from app.composition.school_fact_read_evidence import validation_batch
    from app.platform.assistant.runtime import AssistantRuntimeError

    auth, g = authority()
    now = [NOW]
    auth.clock = lambda: now[0]
    permit = auth.require_scope(actor(), fact_kind="education_class", action="read", checked_at=NOW)
    with pytest.raises(AssistantRuntimeError):
        with validation_batch():
            auth.revalidate_many((permit,), actor=actor(), checked_at=NOW)
            if change == "revocation":
                g.valid = False
            else:
                now[0] = NOW + timedelta(days=7)
    g.valid = False
    with pytest.raises(PermissionError):
        with validation_batch():
            auth.revalidate_many((permit,), actor=actor(), checked_at=NOW)


def test_production_root_round_shares_within_release_and_checks_fresh_roots_and_decision_time(
    monkeypatch,
):
    from contextlib import nullcontext
    from types import SimpleNamespace

    import app.composition.school_fact_authority_connection as connection_module
    import app.composition.school_fact_directory_services as directory_module
    from app.composition.school_fact_education_authority import create_education_authority
    from app.composition.school_fact_product_services import ReadOnlyGovernanceDependencies
    from app.composition.school_fact_read_capture import EvidenceGovernanceDependencies
    from app.composition.school_fact_read_evidence import validation_batch
    from app.platform.assistant.runtime import AssistantRuntimeError
    from app.platform.authorization.thin_contracts import ThinDecisionKind

    original, _ = authority()
    permits = tuple(
        original.require_scope(actor(), fact_kind="education_class", action=a, checked_at=NOW)
        for a in ("read", "maintain")
    )
    owner = object()
    now = [NOW]
    state = ["one"]
    roots = []
    times = []

    class Connection:
        def __init__(self, *args, **kwargs):
            pass

        def read_scope(self, actor):
            return nullcontext()

    monkeypatch.setattr(connection_module, "ConnectionBoundAuthority", Connection)
    monkeypatch.setattr(
        directory_module,
        "_fresh_root_scope",
        lambda _: nullcontext(ReadOnlyGovernanceDependencies(None)),
    )
    monkeypatch.setattr(
        ReadOnlyGovernanceDependencies,
        "_root",
        lambda self, **kwargs: (roots.append(kwargs), state[0])[1],
    )
    deps = EvidenceGovernanceDependencies(None, owner=owner)
    auth = create_education_authority(
        sessions=None, configuration=original.configuration, evidence_owner=owner
    )
    auth.clock = lambda: now[0]

    def check(bases, *, as_of, **kwargs):
        times.append(as_of)
        deps._root(school_id=actor().school_id, root_id="person", kind="person", as_of=as_of)
        return tuple(SimpleNamespace(kind=ThinDecisionKind.ALLOW) for _ in bases)

    auth.thin = SimpleNamespace(revalidate_many=check)
    with validation_batch():
        auth.revalidate_many((permits[0],), actor=actor(), checked_at=NOW)
        now[0] = NOW + timedelta(seconds=1)
        auth.revalidate_many(permits, actor=actor(), checked_at=now[0])
        auth.revalidate_many(permits, actor=actor(), checked_at=now[0])
        assert len(roots) == 3 and len(times) == 2
    assert len(roots) == 4 and times[-1] == now[0]
    with pytest.raises(AssistantRuntimeError):
        with validation_batch():
            auth.revalidate_many(permits, actor=actor(), checked_at=now[0])
            state[0] = "changed"
    assert len(roots) == 7


def test_release_permits_never_cross_authority_owners_for_same_actor():
    from unittest.mock import Mock

    from app.composition.school_fact_read_evidence import validation_batch

    left, _ = authority()
    right, _ = authority()
    left.clock = right.clock = lambda: NOW
    permit = left.require_scope(actor(), fact_kind="education_class", action="read", checked_at=NOW)
    left.revalidate = Mock(wraps=left.revalidate)
    right.revalidate = Mock(wraps=right.revalidate)
    with validation_batch():
        left.revalidate_many((permit,), actor=actor(), checked_at=NOW)
        right.revalidate_many((permit,), actor=actor(), checked_at=NOW)
        assert left.revalidate.call_count == right.revalidate.call_count == 1
    assert left.revalidate.call_count == right.revalidate.call_count == 2


def test_release_fresh_recheck_includes_final_partial_chunk_and_denies_its_failure():
    from dataclasses import replace
    from types import SimpleNamespace

    from app.composition.school_fact_read_evidence import validation_batch
    from app.platform.assistant.runtime import AssistantRuntimeError
    from app.platform.authorization.thin_contracts import ThinDecisionKind

    auth, _ = authority()
    auth.clock = lambda: NOW
    permit = auth.require_scope(actor(), fact_kind="education_class", action="read", checked_at=NOW)
    permits = tuple(replace(permit, scope_id=UUID(int=100 + i)) for i in range(129))
    calls = []
    finishing = [False]

    def check(bases, **kwargs):
        calls.append(len(bases))
        if finishing[0] and len(bases) == 1:
            return ()
        return tuple(SimpleNamespace(kind=ThinDecisionKind.ALLOW) for _ in bases)

    auth.thin = SimpleNamespace(revalidate_many=check)
    with pytest.raises(AssistantRuntimeError):
        with validation_batch():
            auth.revalidate_many(permits[:128], actor=actor(), checked_at=NOW)
            auth.revalidate_many(permits[128:], actor=actor(), checked_at=NOW)
            finishing[0] = True
    assert calls == [128, 1, 128, 1]


def scope_batch_authority():
    from contextlib import contextmanager
    from types import SimpleNamespace

    auth, governance = authority()
    auth.clock = lambda: NOW
    original = auth.thin
    calls = []
    roots = []

    @contextmanager
    def root_validation(current_actor, checked_at):
        roots.append(("enter", current_actor, checked_at))
        try:
            yield
        finally:
            roots.append(("exit", current_actor, checked_at))

    def decide_many(requests):
        assert roots[-1][0] == "enter"
        calls.append(requests)
        return tuple(original.decide(request) for request in requests)

    auth.thin = SimpleNamespace(decide_many=decide_many, decide=original.decide)
    auth.root_validation = root_validation
    return auth, governance, calls, roots


def test_scope_batch_deduplicates_only_exact_requests_and_restores_order():
    auth, governance, calls, roots = scope_batch_authority()
    requests = (
        ("education_class", "read"),
        ("education_class", "maintain"),
        ("education_class", "read"),
    )
    permits = auth.require_scopes(actor(), requests=requests, checked_at=NOW)
    assert len(calls) == 1 and len(calls[0]) == 2 and governance.calls == 2
    assert tuple(p.action for p in permits) == ("read", "maintain", "read")
    assert permits[0] is permits[2]
    assert [entry[0] for entry in roots] == ["enter", "exit"]
    assert all(request.as_of == NOW and request.actor == actor() for request in calls[0])
    auth.require_fields(permits[0], ("display_name",))
    with pytest.raises(PermissionError):
        auth.require_fields(permits[0], ("private_phone",))
    with pytest.raises(PermissionError):
        auth.require_object(permits[0], "class:outside", is_creation=False)
    auth.require_scopes(actor(), requests=requests, checked_at=NOW + timedelta(seconds=1))
    assert len(calls) == 2 and governance.calls == 4
    assert calls[0][0].request_id != calls[1][0].request_id


def test_scope_batch_uses_production_decide_many_and_owner_issue_many():
    from app.composition.school_fact_product_services import ProductThinAuthorizationService

    auth, governance = authority()
    auth.clock = lambda: NOW
    batches = []

    def issue_many(queries):
        batches.append(queries)
        return tuple(governance.issue(query) for query in queries)

    governance.issue_many = issue_many
    auth.thin = ProductThinAuthorizationService(
        governance_basis_service=governance,
        module_policies={"school_facts.f4": F4FactPolicy()},
    )
    permits = auth.require_scopes(
        actor(),
        requests=(("education_class", "read"), ("education_class", "maintain")),
        checked_at=NOW,
    )
    assert len(batches) == 1 and len(batches[0]) == 2
    assert [permit.action for permit in permits] == ["read", "maintain"]


def test_scope_batch_keeps_f4_and_f5_owners_projections_and_scopes_separate():
    from dataclasses import replace
    from types import SimpleNamespace

    from app.composition.school_fact_education_settings import EducationScopeBinding
    from app.composition.school_fact_product_services import ProductThinAuthorizationService
    from app.platform.school_facts.f5_policy import F5FactPolicy, F5FactScope

    auth, f4 = authority()
    f5_scope = F5FactScope(
        actor().school_id,
        "teaching_course",
        object_keys=("course:a",),
        field_names=("display_name",),
    )
    f5_id = uuid4()
    auth.bindings[actor().assignment_id, "school_facts.f5.teaching_course.read"] = f5_id
    auth.configuration = auth.configuration.model_copy(
        update={
            "scope_bindings": (
                *auth.configuration.scope_bindings,
                EducationScopeBinding(
                    assignment_id=actor().assignment_id,
                    operation_code="school_facts.f5.teaching_course.read",
                    scope_id=f5_id,
                ),
            ),
        }
    )

    def issue_many(queries):
        results = []
        for query in queries:
            basis = f4.issue(query)
            if query.scope_id == f5_id:
                basis = replace(
                    basis,
                    scope_id=f5_id,
                    dependency_versions=(
                        *basis.dependency_versions[:2],
                        GovernanceDependencyVersion(
                            "school_facts.f5.scope-target", f5_scope.reference, 1
                        ),
                    ),
                )
            results.append(basis)
        return tuple(results)

    auth.thin = ProductThinAuthorizationService(
        governance_basis_service=SimpleNamespace(issue_many=issue_many),
        module_policies={"school_facts.f4": F4FactPolicy(), "school_facts.f5": F5FactPolicy()},
    )
    permits = auth.require_scopes(
        actor(),
        requests=(("education_class", "read"), ("teaching_course", "read")),
        checked_at=NOW,
    )
    assert tuple(p.scope.fact_kind for p in permits) == ("education_class", "teaching_course")
    assert permits[1].scope == f5_scope and permits[1].basis.module_name == "school_facts.f5"
    with pytest.raises(PermissionError):
        auth.require_object(permits[0], "course:a", is_creation=False)


@pytest.mark.parametrize("invalid", ["kind", "action", "binding", "actor", "time", "size"])
def test_scope_batch_preflights_entire_request_before_any_authority_io(invalid):
    auth, governance, calls, roots = scope_batch_authority()
    current_actor, checked_at = actor(), NOW
    requests = (("education_class", "read"),)
    if invalid == "kind":
        requests += (("unknown_kind", "read"),)
    elif invalid == "action":
        requests += (("education_class", "unbound-action"),)
    elif invalid == "binding":
        requests += (("student_class_position", "read"),)
    elif invalid == "actor":
        current_actor = object()
    elif invalid == "time":
        checked_at = NOW.replace(tzinfo=None)
    else:
        requests *= 129
    with pytest.raises(PermissionError):
        auth.require_scopes(current_actor, requests=requests, checked_at=checked_at)
    assert governance.calls == 0 and calls == [] and roots == []


def test_scope_batch_empty_and_legacy_thin_fallback_remain_bounded():
    auth, governance = authority()
    auth.clock = lambda: NOW
    assert auth.require_scopes(actor(), requests=(), checked_at=NOW) == ()
    assert governance.calls == 0
    permits = auth.require_scopes(
        actor(), requests=(("education_class", "read"),) * 128, checked_at=NOW
    )
    assert len(permits) == 128 and governance.calls == 1
    assert all(permit is permits[0] for permit in permits)


@pytest.mark.parametrize(
    "damage",
    [
        "deny",
        "missing",
        "extra",
        "reordered",
        "request_id",
        "decision_id",
        "actor",
        "scope",
        "projection",
        "module",
        "action",
        "resource",
        "revision",
        "school",
        "governance_scope",
        "governance_duty",
    ],
)
def test_scope_batch_never_accepts_misaligned_or_partial_decisions(damage):
    from dataclasses import replace

    from app.platform.authorization.thin_contracts import NamedProjection, ThinDecisionKind

    auth, _, _, _ = scope_batch_authority()
    original = auth.thin.decide_many

    def damaged(requests):
        values = original(requests)
        if damage == "missing":
            return values[:1]
        if damage == "extra":
            return (*values, values[0])
        if damage == "reordered":
            return tuple(reversed(values))
        value = values[1]
        if damage == "deny":
            value = replace(value, kind=ThinDecisionKind.DENY)
        elif damage == "request_id":
            value = replace(value, request_id=uuid4())
        elif damage == "decision_id":
            value = replace(value, decision_id=uuid4())
        elif damage == "actor":
            value = replace(
                value,
                basis=replace(value.basis, actor=replace(value.basis.actor, account_user_id=99)),
            )
        elif damage == "scope":
            value = replace(value, scope_plan=replace(value.scope_plan, scope_id=uuid4()))
        elif damage == "projection":
            value = replace(value, projection=NamedProjection("f4_education_class_read", 1))
        elif damage in ("module", "action", "resource", "revision"):
            fields = {
                "module": {"module_name": "other"},
                "action": {"action_code": "other"},
                "resource": {"resource_id": uuid4()},
                "revision": {"resource_revision": 999},
            }
            value = replace(value, basis=replace(value.basis, **fields[damage]))
        else:
            fields = {
                "school": {"school_id": UUID(int=99)},
                "governance_scope": {"scope_id": uuid4()},
                "governance_duty": {"duty_code": "other"},
            }
            value = replace(
                value,
                basis=replace(
                    value.basis, governance=replace(value.basis.governance, **fields[damage])
                ),
            )
        return (values[0], value)

    auth.thin.decide_many = damaged
    with pytest.raises(PermissionError):
        auth.require_scopes(
            actor(),
            requests=(("education_class", "read"), ("education_class", "maintain")),
            checked_at=NOW,
        )


def test_scope_batch_propagates_dependency_exception_and_has_no_retained_permits():
    auth, _, calls, roots = scope_batch_authority()
    original = auth.thin.decide_many

    def fail(requests):
        raise LookupError("controlled dependency failure")

    auth.thin.decide_many = fail
    with pytest.raises(LookupError, match="controlled dependency failure"):
        auth.require_scopes(actor(), requests=(("education_class", "read"),), checked_at=NOW)
    assert calls == [] and [entry[0] for entry in roots] == ["enter", "exit"]
    auth.thin.decide_many = original
    assert auth.require_scopes(actor(), requests=(("education_class", "read"),), checked_at=NOW)
    assert len(calls) == 1


def test_scope_batch_checks_expiry_after_owner_io_and_again_on_release():
    from app.composition.school_fact_read_evidence import validation_batch
    from app.platform.assistant.runtime import AssistantRuntimeError

    auth, _, _, _ = scope_batch_authority()
    now = [NOW]
    auth.clock = lambda: now[0]
    original = auth.thin.decide_many

    def crosses_expiry(requests):
        values = original(requests)
        now[0] = NOW + timedelta(days=7)
        return values

    auth.thin.decide_many = crosses_expiry
    with pytest.raises(AssistantRuntimeError):
        auth.require_scopes(actor(), requests=(("education_class", "read"),), checked_at=NOW)
    auth.thin.decide_many = original
    now[0] = NOW
    with pytest.raises(AssistantRuntimeError):
        with validation_batch():
            auth.require_scopes(actor(), requests=(("education_class", "read"),), checked_at=NOW)
            now[0] = NOW + timedelta(days=7)


def test_read_wave_revalidation_is_immediate_even_if_outer_release_checked_same_permit():
    from app.composition.school_fact_read_evidence import validation_batch

    auth, governance = authority()
    auth.clock = lambda: NOW
    permit = auth.require_scope(actor(), fact_kind="education_class", action="read", checked_at=NOW)
    with validation_batch():
        auth.revalidate_many((permit,), actor=actor(), checked_at=NOW)
        governance.valid = False
        with pytest.raises(PermissionError):
            auth.revalidate_read_wave((permit,), actor=actor(), checked_at=NOW)
        governance.valid = True


def test_read_wave_revalidation_checks_actor_time_and_bound_before_owner_io():
    from dataclasses import replace
    from types import SimpleNamespace

    auth, _ = authority()
    auth.clock = lambda: NOW
    permit = auth.require_scope(actor(), fact_kind="education_class", action="read", checked_at=NOW)
    calls = []
    auth.thin = SimpleNamespace(revalidate_many=lambda *a, **k: calls.append(1) or ())
    foreign_actor = contexts._issue_staff_actor_context(
        account_user_id=2,
        school_id=actor().school_id,
        person_id=actor().person_id,
        assignment_id=actor().assignment_id,
        assignment_version=1,
        authorization_version=2,
    )
    for values, checked_at in (
        ((replace(permit, actor=foreign_actor),), NOW),
        ((permit,), NOW.replace(tzinfo=None)),
        ((permit,) * 129, NOW),
    ):
        with pytest.raises(PermissionError):
            auth.revalidate_read_wave(values, actor=actor(), checked_at=checked_at)
    assert calls == []
    auth.revalidate_read_wave((), actor=actor(), checked_at=NOW)
    assert calls == []


def stable_release_permit(action="read"):
    from dataclasses import replace
    from unittest.mock import Mock

    auth, governance = authority()
    original = auth.require_scope(
        actor(), fact_kind="education_class", action=action, checked_at=NOW
    )
    basis = original.basis.governance
    dependencies = (
        *basis.dependency_versions,
        GovernanceDependencyVersion("governance.duty", str(UUID(int=81)), 1),
        GovernanceDependencyVersion("governance.scope", str(basis.scope_id), 1),
        GovernanceDependencyVersion("governance.grant", str(basis.grant_id), basis.grant_version),
        GovernanceDependencyVersion(
            "governance.assignment_epoch", str(basis.assignment_id), basis.assignment_epoch
        ),
    )
    permit = replace(
        original,
        basis=replace(original.basis, governance=replace(basis, dependency_versions=dependencies)),
    )
    auth._revalidate_many_now = Mock(wraps=auth._revalidate_many_now)
    return auth, governance, permit


def renewed_release_permit(permit, *, observed_at=NOW):
    from dataclasses import replace

    return replace(
        permit,
        basis=replace(
            permit.basis,
            request_id=uuid4(),
            decision_id=uuid4(),
            governance=replace(permit.basis.governance, issued_as_of=observed_at),
        ),
    )


def test_release_shares_semantically_equal_authority_but_keeps_fresh_initial_and_final_checks():
    from app.composition.school_fact_read_evidence import validation_batch
    from app.platform.authorization.secure_reader_adapters import authorization_basis_fingerprint

    auth, _, first = stable_release_permit()
    now = [NOW]
    auth.clock = lambda: now[0]
    second = renewed_release_permit(first, observed_at=NOW + timedelta(seconds=1))
    assert first != second
    assert authorization_basis_fingerprint(first.basis) == authorization_basis_fingerprint(
        second.basis
    )
    with validation_batch():
        auth.revalidate_many((first,), actor=actor(), checked_at=now[0])
        now[0] += timedelta(seconds=1)
        auth.revalidate_many((second,), actor=actor(), checked_at=now[0])
        assert auth._revalidate_many_now.call_count == 1
        now[0] += timedelta(seconds=1)
    assert auth._revalidate_many_now.call_count == 2
    assert auth._revalidate_many_now.call_args.kwargs["checked_at"] == now[0]
    with validation_batch():
        auth.revalidate_many((second,), actor=actor(), checked_at=now[0])
    assert auth._revalidate_many_now.call_count == 4


@pytest.mark.parametrize(
    "change", ["scope_fields", "scope_objects", "scope_id", "action", "owner", "epoch", "deadline"]
)
def test_release_equivalence_never_merges_changed_security_semantics(change):
    from dataclasses import replace

    from app.composition.school_fact_read_evidence import validation_batch

    auth, _, first = stable_release_permit()
    second = renewed_release_permit(first)
    target = auth
    if change == "scope_fields":
        second = replace(second, scope=replace(second.scope, field_names=("display_name",)))
    elif change == "scope_objects":
        second = replace(second, scope=replace(second.scope, object_keys=("class:a",)))
    elif change == "scope_id":
        second = replace(second, scope_id=uuid4())
    elif change == "action":
        second = replace(second, action="maintain")
    elif change == "owner":
        target, _, _ = stable_release_permit()
    elif change == "epoch":
        gov = second.basis.governance
        dependencies = tuple(
            replace(d, version=d.version + 1) if d.authority == "governance.assignment_epoch" else d
            for d in gov.dependency_versions
        )
        second = replace(
            second,
            basis=replace(
                second.basis,
                governance=replace(
                    gov, assignment_epoch=gov.assignment_epoch + 1, dependency_versions=dependencies
                ),
            ),
        )
    else:
        second = replace(
            second,
            basis=replace(
                second.basis,
                governance=replace(second.basis.governance, valid_until=NOW + timedelta(days=1)),
            ),
        )
    with validation_batch():
        auth.revalidate_many((first,), actor=actor(), checked_at=NOW)
        target.revalidate_many((second,), actor=actor(), checked_at=NOW)
        if target is auth:
            assert auth._revalidate_many_now.call_count == 2
        else:
            assert (
                auth._revalidate_many_now.call_count == target._revalidate_many_now.call_count == 1
            )


def test_release_equivalence_retains_every_original_permit_time_window():
    from app.composition.school_fact_read_evidence import validation_batch
    from app.platform.assistant.runtime import AssistantRuntimeError

    auth, _, first = stable_release_permit()
    future_issued = renewed_release_permit(first, observed_at=NOW + timedelta(seconds=1))
    with validation_batch():
        auth.revalidate_many((first,), actor=actor(), checked_at=NOW)
        with pytest.raises(AssistantRuntimeError):
            auth.revalidate_many((future_issued,), actor=actor(), checked_at=NOW)
        # Same semantic grant does not make a not-yet-valid proof currently usable.
        assert auth._revalidate_many_now.call_count == 1


def test_release_equivalence_shorter_deadline_still_fails_at_final_boundary():
    from dataclasses import replace

    from app.composition.school_fact_read_evidence import validation_batch
    from app.platform.assistant.runtime import AssistantRuntimeError

    auth, _, first = stable_release_permit()
    now = [NOW]
    auth.clock = lambda: now[0]
    second = renewed_release_permit(first)
    second = replace(
        second,
        basis=replace(
            second.basis,
            governance=replace(second.basis.governance, valid_until=NOW + timedelta(seconds=1)),
        ),
    )
    with pytest.raises(AssistantRuntimeError):
        with validation_batch():
            auth.revalidate_many((first, second), actor=actor(), checked_at=NOW)
            now[0] += timedelta(seconds=1)


def test_release_equivalence_never_skips_final_revocation_check():
    from app.composition.school_fact_read_evidence import validation_batch
    from app.platform.assistant.runtime import AssistantRuntimeError

    auth, governance, first = stable_release_permit()
    second = renewed_release_permit(first)
    with pytest.raises(AssistantRuntimeError):
        with validation_batch():
            auth.revalidate_many((first,), actor=actor(), checked_at=NOW)
            auth.revalidate_many((second,), actor=actor(), checked_at=NOW)
            assert auth._revalidate_many_now.call_count == 1
            governance.valid = False
    assert auth._revalidate_many_now.call_count == 2


def test_release_equivalence_missing_fingerprint_keeps_exact_permit_checks():
    from unittest.mock import Mock

    from app.composition.school_fact_read_evidence import validation_batch
    from app.platform.authorization.secure_reader_adapters import authorization_basis_fingerprint

    auth, _ = authority()
    first = auth.require_scope(actor(), fact_kind="education_class", action="read", checked_at=NOW)
    second = renewed_release_permit(first)
    assert authorization_basis_fingerprint(first.basis) is None
    auth._revalidate_many_now = Mock(wraps=auth._revalidate_many_now)
    with validation_batch():
        auth.revalidate_many((first,), actor=actor(), checked_at=NOW)
        auth.revalidate_many((second,), actor=actor(), checked_at=NOW)
        assert auth._revalidate_many_now.call_count == 2
    assert auth._revalidate_many_now.call_count == 3


def test_release_equivalence_does_not_change_maintenance_or_writer_permit_checks():
    from app.composition.school_fact_read_evidence import validation_batch
    from app.platform.authorization.secure_reader_adapters import authorization_basis_fingerprint

    auth, _, first = stable_release_permit(action="maintain")
    second = renewed_release_permit(first)
    assert authorization_basis_fingerprint(first.basis) == authorization_basis_fingerprint(
        second.basis
    )
    with validation_batch():
        auth.revalidate_many((first,), actor=actor(), checked_at=NOW)
        auth.revalidate_many((second,), actor=actor(), checked_at=NOW)
        assert auth._revalidate_many_now.call_count == 2
    assert auth._revalidate_many_now.call_count == 3


def test_release_equivalence_cannot_reuse_same_basis_for_another_actor():
    from dataclasses import replace

    from app.composition.school_fact_read_evidence import validation_batch

    auth, _, first = stable_release_permit()
    other = contexts._issue_staff_actor_context(
        account_user_id=2,
        school_id=actor().school_id,
        person_id=actor().person_id,
        assignment_id=actor().assignment_id,
        assignment_version=1,
        authorization_version=2,
    )
    second = replace(renewed_release_permit(first), actor=other)
    with validation_batch():
        auth.revalidate_many((first,), actor=actor(), checked_at=NOW)
        with pytest.raises(PermissionError):
            auth.revalidate_many((second,), actor=other, checked_at=NOW)
    assert auth._revalidate_many_now.call_count == 3


@pytest.mark.parametrize("difference", ["version", "time_boundary"])
def test_release_equivalence_does_not_merge_fact_content_or_time_dependencies(difference):
    from dataclasses import replace
    from types import SimpleNamespace

    from app.composition.school_fact_education_reader import EducationReadProof, EducationReadStale
    from app.composition.school_fact_read_evidence import validation_batch
    from app.platform.assistant.runtime import AssistantRuntimeError

    auth, _, first = stable_release_permit()
    now = [NOW]
    auth.clock = lambda: now[0]
    repository = SimpleNamespace(
        authority=auth,
        clock=auth.clock,
        projection=SimpleNamespace(current_version=lambda school: 10),
    )
    first_proof = EducationReadProof(repository, actor(), (first,), 10, NOW + timedelta(seconds=2))
    second_proof = replace(first_proof, permits=(renewed_release_permit(first),))
    if difference == "version":
        second_proof = replace(second_proof, school_fact_version=9)
    else:
        second_proof = replace(second_proof, next_boundary=NOW + timedelta(seconds=1))
    with pytest.raises((EducationReadStale, AssistantRuntimeError)):
        with validation_batch():
            first_proof.revalidate(actor=actor())
            second_proof.revalidate(actor=actor())
            assert auth._revalidate_many_now.call_count == 1
            now[0] += timedelta(seconds=1)
