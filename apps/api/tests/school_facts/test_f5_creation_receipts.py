import json
from uuid import UUID, uuid4

import pytest
from sqlalchemy import JSON, Column, MetaData, String, Table, Uuid

from tests.test_f2_own_receipts import context, store  # noqa: F401

TARGET = frozenset({("teaching_offering", "new:offering")})


@pytest.mark.parametrize("expected", [0, 1, "0", None, False])
def test_creation_only_receipt_requires_formal_numeric_zero_command(store, expected):  # noqa: F811
    service, seed, engine = store
    with engine.connect() as connection:
        raw = connection.connection.driver_connection
        raw.create_function(
            "jsonb_extract_path", 2, lambda value, key: json.dumps(json.loads(value).get(key))
        )
        raw.create_function(
            "jsonb_typeof",
            1,
            lambda value: (
                "boolean"
                if type(json.loads(value)) is bool
                else "number"
                if type(json.loads(value)) in {int, float}
                else "other"
            ),
        )
    unit = Table(
        "master_data_import_units",
        MetaData(),
        Column("id", Uuid, primary_key=True),
        Column("school_id", Uuid),
        Column("batch_id", Uuid),
        Column("status", String),
        Column("unit_kind", String),
        Column("payload", JSON),
    )
    unit.create(engine)
    locator = seed(0, targets=TARGET)
    with engine.begin() as conn:
        conn.execute(
            unit.insert().values(
                id=uuid4(),
                school_id=context().actor.school_id,
                batch_id=UUID(int=100),
                status="applied",
                unit_kind="school_fact",
                payload={
                    "fact_kind": "teaching_offering",
                    "source_key": "new:offering",
                    "operation": "upsert",
                    "expected_version": expected,
                },
            )
        )
    result = service.check_own_result_by_locator(
        context=context(),
        locator_sha256=locator,
        allowed_targets=TARGET,
        creation_only_targets=TARGET,
    )
    assert (result is not None) is (type(expected) is int and expected == 0)
    page = service.list_own_results(
        context=context(), allowed_targets=TARGET, creation_only_targets=TARGET
    )
    assert len(page.items) == int(type(expected) is int and expected == 0)


def test_current_creation_authority_is_only_a_receipt_restriction_not_read_scope():
    from types import SimpleNamespace
    from unittest.mock import Mock

    from app.composition.school_fact_education_authority import EducationAuthority

    authority = object.__new__(EducationAuthority)
    actor = context().actor
    authority.bindings = {
        (actor.assignment_id, "school_facts.f5.teaching_offering.maintain"): UUID(int=55)
    }
    authority.clock = lambda: None
    permit = SimpleNamespace(
        actor=actor,
        action="maintain",
        scope=SimpleNamespace(
            fact_kind="teaching_offering",
            object_keys=("existing",),
            creation_keys=("new:offering",),
        ),
        operation_code="school_facts.f5.teaching_offering.maintain",
    )
    authority.require_scope = Mock(return_value=permit)
    authority.revalidate_many = Mock()
    proof = authority.own_result(context())
    assert proof.allowed_targets == TARGET
    assert proof.creation_only_targets == TARGET
    proof.revalidate()
    authority.revalidate_many.assert_called_once()
    authority.revalidate_many.side_effect = PermissionError("revoked or expired")
    with pytest.raises(PermissionError):
        proof.revalidate()


def test_creation_receipt_reference_is_not_a_fact_or_creation_selector():
    from types import SimpleNamespace
    from unittest.mock import Mock

    from app.composition.school_fact_education_reader import EducationRepository

    repository = object.__new__(EducationRepository)
    repository._key = b"unit-test-key-not-production"
    repository.clock = lambda: None
    repository.authority = SimpleNamespace(
        require_scope=Mock(
            return_value=SimpleNamespace(
                scope=SimpleNamespace(creation_keys=("new:offering",), object_keys=())
            )
        ),
        require_object=Mock(),
    )
    actor = context().actor
    reference = repository.creation_receipt_reference_for(
        actor=actor, fact_kind="teaching_offering", source_key="new:offering"
    )
    assert reference.startswith("f4audit:teaching_offering:")
    for creation in (False, True):
        with pytest.raises(PermissionError):
            repository.source_for_reference(
                actor, "teaching_offering", reference, creation=creation
            )


def test_creation_receipt_marker_is_preserved_and_strict_on_writer_transport():
    from pydantic import ValidationError

    from app.platform.school_facts.executor_contracts import SchoolFactExecutorOwnResultTarget

    values = dict(
        fact_kind="teaching_offering",
        source_key="new:offering",
        operation="upsert",
        target_version=1,
        creation_receipt=True,
    )
    assert SchoolFactExecutorOwnResultTarget(**values).to_domain().creation_receipt is True
    for invalid in ({"creation_receipt": "true"}, {"operation": "correct"}, {"target_version": 2}):
        with pytest.raises(ValidationError):
            SchoolFactExecutorOwnResultTarget(**(values | invalid))


def _receipt_authority_batch(monkeypatch):
    from contextlib import contextmanager
    from datetime import UTC, datetime, timedelta
    from types import SimpleNamespace
    from typing import cast

    from app.composition.school_fact_education_authority import (
        EducationAuthority,
        EducationAuthorityDenied,
    )
    from app.composition.school_fact_education_settings import (
        EducationConfiguration,
        EducationScopeBinding,
    )
    from app.composition.school_fact_product_services import (
        ProductThinAuthorizationService,
        ReadOnlyGovernanceDependencies,
    )
    from app.composition.school_fact_read_capture import (
        EvidenceGovernanceDependencies,
        owner_root_round,
    )
    from app.platform.assistant.runtime import AssistantRuntimeError
    from app.platform.contracts import StaffActorContext
    from app.platform.governance.contracts import (
        GovernanceAuthorizationBasis,
        GovernanceDependencyVersion,
    )
    from app.platform.school_facts.f4_policy import F4FactPolicy, F4FactScope
    from app.platform.school_facts.f5_policy import F5FactPolicy, F5FactScope

    actor = cast(StaffActorContext, context().actor)
    now = datetime(2026, 9, 14, tzinfo=UTC)
    state = SimpleNamespace(now=now, root=1, valid=True, after_issue=lambda: None)
    scopes = {
        UUID(int=501): F4FactScope(
            actor.school_id, "education_class", ("class:a",), (), ("display_name",)
        ),
        UUID(int=502): F4FactScope(
            actor.school_id, "education_class", ("class:maintain",), ("class:new",), ("valid_to",)
        ),
        UUID(int=503): F5FactScope(
            actor.school_id,
            "teaching_offering",
            ("offer:a", "offer:shared"),
            (),
            ("offering_status",),
        ),
        UUID(int=504): F5FactScope(
            actor.school_id,
            "teaching_offering",
            ("offer:maintain",),
            ("offer:new", "offer:shared"),
            ("valid_to",),
        ),
    }
    state.scopes = scopes
    operations = (
        "school_facts.f4.education_class.read",
        "school_facts.f4.education_class.maintain",
        "school_facts.f5.teaching_offering.read",
        "school_facts.f5.teaching_offering.maintain",
    )
    root_reads = []

    def root(_self, **kwargs):
        root_reads.append(kwargs)
        return state.root

    monkeypatch.setattr(ReadOnlyGovernanceDependencies, "_root", root)
    owner = object()
    dependencies = EvidenceGovernanceDependencies(None, owner=owner)

    class Governance:
        def __init__(self):
            self.issued, self.batches, self.checked = [], [], []

        def _basis(self, query):
            dependencies._root(
                school_id=actor.school_id,
                root_id=actor.person_id,
                kind="person",
                as_of=query.as_of,
            )
            scope = scopes[query.scope_id]
            phase = "f5" if scope.fact_kind == "teaching_offering" else "f4"
            return GovernanceAuthorizationBasis(
                actor.school_id,
                actor.assignment_id,
                query.duty_code,
                query.scope_id,
                UUID(int=query.scope_id.int + 100),
                1,
                actor.authorization_version,
                query.as_of,
                now + timedelta(days=1),
                (
                    GovernanceDependencyVersion(
                        "school_facts.assignment",
                        str(actor.assignment_id),
                        actor.assignment_version,
                    ),
                    GovernanceDependencyVersion("school_facts.person", str(actor.person_id), 1),
                    GovernanceDependencyVersion(
                        f"school_facts.{phase}.scope-target", scope.reference, 1
                    ),
                ),
            )

        def issue(self, query):
            self.issued.append(query)
            return self._basis(query)

        def issue_many(self, queries):
            self.batches.append(queries)
            values = tuple(self._basis(query) for query in queries)
            state.after_issue()
            return values

        def validate(self, basis, *, as_of):
            self.checked.append(basis)
            return (
                state.valid
                and now <= as_of < now + timedelta(days=1)
                and any(
                    dependency.reference == scopes[basis.scope_id].reference
                    for dependency in basis.dependency_versions
                    if dependency.authority.endswith(".scope-target")
                )
            )

    governance = Governance()

    @contextmanager
    def roots(actor, at):
        try:
            with owner_root_round(
                owner, actor, at, ReadOnlyGovernanceDependencies(None), lambda: state.now
            ):
                yield
        except AssistantRuntimeError as exc:
            raise EducationAuthorityDenied("changed authority root") from exc

    config = EducationConfiguration(
        approval_reference="test-approved",
        scope_bindings=tuple(
            EducationScopeBinding(
                assignment_id=actor.assignment_id, operation_code=operation, scope_id=sid
            )
            for operation, sid in zip(operations, scopes, strict=True)
        ),
    )
    auth = EducationAuthority(
        thin=ProductThinAuthorizationService(
            governance_basis_service=governance,
            module_policies={"school_facts.f4": F4FactPolicy(), "school_facts.f5": F5FactPolicy()},
        ),
        configuration=config,
        root_validation=roots,
        clock=lambda: state.now,
    )
    return auth, governance, state, root_reads


def test_own_authority_batches_independent_read_and_creation_scopes_with_fresh_roots(monkeypatch):
    auth, governance, _, root_reads = _receipt_authority_batch(monkeypatch)
    proof = auth.own_result(context())
    assert proof.allowed_targets == frozenset(
        {
            ("education_class", "class:a"),
            ("education_class", "class:new"),
            ("teaching_offering", "offer:a"),
            ("teaching_offering", "offer:shared"),
            ("teaching_offering", "offer:new"),
        }
    )
    assert proof.creation_only_targets == frozenset(
        {("education_class", "class:new"), ("teaching_offering", "offer:new")}
    )
    assert len(proof.permits) == 4
    for permit in proof.permits:
        auth.require_fields(permit, permit.scope.field_names)
        with pytest.raises(PermissionError):
            auth.require_fields(permit, ("private_phone",))
    assert governance.issued == []
    assert len(governance.batches) == 1 and len(governance.batches[0]) == 4
    assert len(root_reads) == 2
    # A later call, including after a model wait, starts a new current pass.
    assert auth.own_result(context()).allowed_targets == proof.allowed_targets
    assert len(governance.batches) == 2 and len(root_reads) == 4


@pytest.mark.parametrize("action", ["read", "maintain"])
def test_own_authority_keeps_reading_and_creation_permissions_independent(monkeypatch, action):
    auth, _, _, _ = _receipt_authority_batch(monkeypatch)
    auth.bindings = {key: value for key, value in auth.bindings.items() if key[1].endswith(action)}
    proof = auth.own_result(context())
    expected = (
        {
            ("education_class", "class:a"),
            ("teaching_offering", "offer:a"),
            ("teaching_offering", "offer:shared"),
        }
        if action == "read"
        else {
            ("education_class", "class:new"),
            ("teaching_offering", "offer:new"),
            ("teaching_offering", "offer:shared"),
        }
    )
    assert proof.allowed_targets == frozenset(expected)
    assert proof.creation_only_targets == (
        frozenset(expected) if action == "maintain" else frozenset()
    )


@pytest.mark.parametrize("key", ["class:maintain", "class:outside"])
def test_own_authority_exact_selection_needs_every_requested_object(monkeypatch, key):
    auth, _, _, _ = _receipt_authority_batch(monkeypatch)
    with pytest.raises(PermissionError):
        auth.own_result(
            context(), targets=frozenset({("education_class", "class:a"), ("education_class", key)})
        )


@pytest.mark.parametrize("change", ["revocation", "expiry", "fields", "objects"])
def test_own_authority_proof_rechecks_after_read_and_wait(monkeypatch, change):
    from dataclasses import replace
    from datetime import timedelta

    auth, governance, state, _ = _receipt_authority_batch(monkeypatch)
    proof = auth.own_result(context())
    proof.revalidate()
    assert len(governance.checked) == 4
    if change == "revocation":
        state.valid = False
    elif change == "expiry":
        state.now += timedelta(days=1)
    else:
        sid = UUID(int=501)
        changed = (
            {"field_names": ("valid_to",)} if change == "fields" else {"object_keys": ("class:b",)}
        )
        state.scopes[sid] = replace(state.scopes[sid], **changed)
    with pytest.raises(PermissionError):
        proof.revalidate()


def test_own_authority_batch_rejects_root_change_before_return_and_discards_round(monkeypatch):
    auth, _, state, _ = _receipt_authority_batch(monkeypatch)
    state.after_issue = lambda: setattr(state, "root", 2)
    with pytest.raises(PermissionError):
        auth.own_result(context())
    state.after_issue = lambda: None
    assert auth.own_result(context()).allowed_targets


@pytest.mark.parametrize("fault", ["missing", "owner", "scope_id", "projection", "fields", "actor"])
def test_own_authority_batch_never_accepts_incomplete_or_mismatched_decisions(monkeypatch, fault):
    from dataclasses import replace

    from app.platform.authorization.thin_contracts import NamedProjection

    auth, _, _, _ = _receipt_authority_batch(monkeypatch)
    original = auth.thin.decide_many

    def invalid(requests):
        decisions = original(requests)
        if fault == "missing":
            return decisions[:-1]
        first = decisions[0]
        if fault == "owner":
            first = replace(first, scope_plan=replace(first.scope_plan, owner="school_facts.f5"))
        elif fault == "scope_id":
            first = replace(first, scope_plan=replace(first.scope_plan, scope_id=UUID(int=999)))
        elif fault == "projection":
            first = replace(first, projection=NamedProjection("f4_education_class_maintain", 1))
        elif fault == "fields":
            document = json.loads(first.scope_plan.reference)
            document["field_names"] = ["private_phone"]
            first = replace(
                first, scope_plan=replace(first.scope_plan, reference=json.dumps(document))
            )
        else:
            first = replace(
                first,
                basis=replace(first.basis, actor=replace(first.basis.actor, account_user_id=999)),
            )
        return (first, *decisions[1:])

    auth.thin.decide_many = invalid
    with pytest.raises((PermissionError, ValueError)):
        auth.own_result(context(), targets=frozenset({("education_class", "class:a")}))


@pytest.mark.parametrize("fault", ["untrusted_context", "unbound_actor", "disabled"])
def test_own_authority_rejects_ineligible_identity_or_configuration_before_io(monkeypatch, fault):
    from types import SimpleNamespace
    from typing import Any, cast

    auth, governance, _, root_reads = _receipt_authority_batch(monkeypatch)
    trusted = context()
    if fault == "untrusted_context":
        trusted = SimpleNamespace(actor=trusted.actor)
    elif fault == "unbound_actor":
        auth.bindings = {
            (UUID(int=999), operation): sid for (_, operation), sid in auth.bindings.items()
        }
    else:
        auth.configuration = auth.configuration.model_copy(update={"enabled": False})
    with pytest.raises(PermissionError):
        auth.own_result(cast(Any, trusted))
    assert governance.batches == governance.issued == root_reads == []


def test_own_authority_source_failure_does_not_turn_into_partial_allow(monkeypatch):
    from app.platform.school_facts.read_policy import SchoolFactAuthorizationUnavailable

    auth, _, _, _ = _receipt_authority_batch(monkeypatch)

    def unavailable(_requests):
        raise SchoolFactAuthorizationUnavailable("controlled unavailable authority")

    auth.thin.decide_many = unavailable
    with pytest.raises(SchoolFactAuthorizationUnavailable):
        auth.own_result(context())


def _education_scope_batch_case(phase):
    from datetime import UTC, datetime
    from importlib import import_module

    from app.platform.governance.contracts import ScopeResolutionRequest

    module = import_module(f"app.platform.school_facts.f{phase}_policy")
    scope_type = getattr(module, f"F{phase}FactScope")
    resolver_type = getattr(module, f"F{phase}ScopeResolver")
    kind = "education_class" if phase == 4 else "teaching_offering"
    scopes = (
        scope_type(UUID(int=11), kind, ("existing",), ("creation-only",), ("display_name",)),
        scope_type(UUID(int=11), kind, ("missing",), (), ("valid_to",)),
    )
    at = datetime(2026, 9, 14, tzinfo=UTC)
    requests = tuple(
        ScopeResolutionRequest(
            scope.school_id,
            UUID(int=501 + index),
            f"school_facts.f{phase}",
            getattr(module, f"F{phase}_SCOPE_SCHEMA"),
            1,
            scope.document,
            scope.document_sha256,
            at,
        )
        for index, scope in enumerate(scopes)
    )
    return resolver_type, scopes, requests


@pytest.mark.parametrize("phase", [4, 5])
def test_education_scope_batch_preserves_order_duplicates_and_each_denied_item(phase):
    resolver_type, scopes, requests = _education_scope_batch_case(phase)
    calls = []

    def lookup(values, times):
        calls.append((values, times))
        return True, False, True

    resolver = resolver_type(
        lookup_scope=lambda *_: pytest.fail("single scope fallback used"), lookup_scope_many=lookup
    )
    result = resolver.resolve_many((requests[0], requests[1], requests[0]))
    assert len(result) == 3 and result[1] is None
    assert result[0].reference == result[2].reference == scopes[0].reference
    assert calls == [((scopes[0], scopes[1], scopes[0]), (requests[0].as_of,) * 3)]


@pytest.mark.parametrize("phase", [4, 5])
@pytest.mark.parametrize(
    "fault", ["owner", "schema", "version", "hash", "school", "fields", "time"]
)
def test_education_scope_batch_rejects_invalid_documents_without_owner_io(phase, fault):
    from dataclasses import replace

    resolver_type, _, requests = _education_scope_batch_case(phase)
    request = requests[0]
    changes = {
        "owner": {"schema_owner": "school_facts.f3"},
        "schema": {"schema_name": "other_schema"},
        "version": {"schema_version": 2},
        "hash": {"reference_sha256": "bad"},
        "school": {"school_id": UUID(int=99)},
        "fields": {"reference_document": request.reference_document | {"field_names": ["secret"]}},
        "time": {"as_of": request.as_of.replace(tzinfo=None)},
    }
    resolver = resolver_type(
        lookup_scope=lambda *_: pytest.fail("single scope fallback used"),
        lookup_scope_many=lambda *_: pytest.fail("invalid scope reached owner"),
    )
    assert resolver.resolve_many((replace(request, **changes[fault]),)) == (None,)


@pytest.mark.parametrize("phase", [4, 5])
@pytest.mark.parametrize("answer", [(), (True, True), (1,), (None,)])
def test_education_scope_batch_rejects_missing_extra_and_nonboolean_owner_results(phase, answer):
    resolver_type, _, requests = _education_scope_batch_case(phase)
    resolver = resolver_type(lookup_scope=lambda *_: True, lookup_scope_many=lambda *_: answer)
    with pytest.raises(ValueError):
        resolver.resolve_many(requests[:1])


@pytest.mark.parametrize("phase", [4, 5])
def test_education_scope_batch_is_bounded_and_has_equivalent_single_fallback(phase):
    resolver_type, _, requests = _education_scope_batch_case(phase)
    calls = []
    resolver = resolver_type(lookup_scope=lambda scope, at: calls.append(scope) or True)
    assert resolver.resolve_many(()) == ()
    assert len(resolver.resolve_many((requests[0],) * 128)) == 128
    assert len(calls) == 128
    with pytest.raises(ValueError):
        resolver.resolve_many((requests[0],) * 129)
    assert len(calls) == 128


def _education_scope_sql_fixture():
    from contextlib import contextmanager
    from types import SimpleNamespace

    from app.composition.school_fact_education_authority import EducationScopeOwnerLookup

    calls, state = [], SimpleNamespace(rows=None)

    class Sessions:
        @contextmanager
        def begin(self):
            yield self

        def execute(self, sql, parameters):
            calls.append((str(sql), json.loads(parameters["requests"])))
            rows = state.rows
            if rows is None:
                rows = [(row["ordinal"], True) for row in calls[-1][1]]
            return SimpleNamespace(all=lambda: rows)

    return EducationScopeOwnerLookup(Sessions()), calls, state


def test_education_scope_batch_sql_keeps_exact_type_time_objects_and_bound_injection_values():
    from dataclasses import replace
    from datetime import timedelta

    _, f4, requests = _education_scope_batch_case(4)
    _, f5, _ = _education_scope_batch_case(5)
    injected = "key'); SELECT pg_sleep(9); --"
    scopes = (replace(f4[0], object_keys=(injected,)), f5[0], f4[1])
    at = requests[0].as_of
    times = (at, at + timedelta(seconds=1), at)
    lookup, calls, state = _education_scope_sql_fixture()
    state.rows = [(0, True), (1, False), (2, True)]
    assert lookup.scope_exists_many(scopes, times) == (True, False, True)
    assert len(calls) == 1
    sql, rows = calls[0]
    assert injected not in sql and "creation-only" not in sql
    assert "school_fact_f4_scope_is_current" in sql and "school_fact_f5_scope_is_current" in sql
    assert [(row["phase"], row["kind"], row["objects"], row["as_of"]) for row in rows] == [
        ("f4", "education_class", [injected], at.isoformat()),
        ("f5", "teaching_offering", ["existing"], times[1].isoformat()),
        ("f4", "education_class", ["missing"], at.isoformat()),
    ]
    assert all(row["school_id"] == str(scopes[0].school_id) for row in rows)
    # Post-read calls keep a new database statement and cannot reuse the old answer.
    state.rows = [(0, False), (1, False), (2, True)]
    assert lookup.scope_exists_many(scopes, times) == (False, False, True)
    assert len(calls) == 2


@pytest.mark.parametrize("phase", [4, 5])
def test_education_scope_batch_sql_references_only_its_installed_phase_function(phase):
    _, scopes, requests = _education_scope_batch_case(phase)
    lookup, calls, _ = _education_scope_sql_fixture()
    assert lookup.scope_exists_many(scopes, (requests[0].as_of,) * 2) == (True, True)
    sql = calls[0][0]
    assert f"school_fact_f{phase}_scope_is_current" in sql
    assert f"school_fact_f{9 - phase}_scope_is_current" not in sql


@pytest.mark.parametrize("rows", [[], [(1, True)], [(0, True), (0, False)], [(0, 1)]])
def test_education_scope_batch_sql_rejects_missing_duplicate_shifted_or_untyped_rows(rows):
    _, scopes, requests = _education_scope_batch_case(4)
    lookup, _, state = _education_scope_sql_fixture()
    state.rows = rows
    with pytest.raises(ValueError):
        lookup.scope_exists_many(scopes[:1], (requests[0].as_of,))


@pytest.mark.parametrize("fault", ["length", "limit", "type", "time"])
def test_education_scope_batch_sql_rejects_invalid_input_before_sql(fault):
    _, scopes, requests = _education_scope_batch_case(4)
    lookup, calls, _ = _education_scope_sql_fixture()
    selected, times = scopes[:1], (requests[0].as_of,)
    if fault == "length":
        times = ()
    elif fault == "limit":
        selected, times = selected * 129, times * 129
    elif fault == "type":
        selected = (object(),)
    else:
        times = (times[0].replace(tzinfo=None),)
    with pytest.raises(ValueError):
        lookup.scope_exists_many(selected, times)
    assert calls == []


def test_own_authority_rejects_grant_expiry_during_initial_owner_io(monkeypatch):
    from datetime import timedelta

    from app.platform.assistant.runtime import AssistantRuntimeError

    auth, _, state, _ = _receipt_authority_batch(monkeypatch)
    state.after_issue = lambda: setattr(state, "now", state.now + timedelta(days=1))
    with pytest.raises(AssistantRuntimeError):
        auth.own_result(context())


@pytest.mark.parametrize("through_proof", [False, True])
def test_own_authority_rejects_expiry_after_all_authority_io_before_release(
    monkeypatch, through_proof
):
    from dataclasses import replace
    from datetime import timedelta

    from app.composition.school_fact_read_evidence import validation_batch
    from app.platform.assistant.runtime import AssistantRuntimeError

    auth, _, state, _ = _receipt_authority_batch(monkeypatch)
    proof = auth.own_result(context())
    proof_time = [state.now]
    proof = replace(proof, clock=lambda: proof_time[0])
    with pytest.raises(AssistantRuntimeError):
        with validation_batch() as batch:
            if through_proof:
                proof.revalidate()
                batch.content_rechecks["later-owner-io"] = lambda: proof_time.__setitem__(
                    0, proof_time[0] + timedelta(days=1)
                )
            else:
                auth.revalidate_many(
                    proof.permits, actor=proof.permits[0].actor, checked_at=state.now
                )
                batch.content_rechecks["later-owner-io"] = lambda: setattr(
                    state, "now", state.now + timedelta(days=1)
                )
