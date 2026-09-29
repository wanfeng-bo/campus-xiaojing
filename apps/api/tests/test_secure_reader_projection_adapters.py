from __future__ import annotations

import importlib
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import pytest

from app.platform import contracts as platform_contracts
from app.platform.authorization.thin_contracts import (
    NamedProjection,
    ScopePlan,
    StaffActorBasis,
    ThinAuthorizationBasis,
)
from app.platform.contracts import AssignmentId, PersonId, SchoolId
from app.platform.governance.contracts import (
    GovernanceAuthorizationBasis,
    GovernanceDependencyVersion,
)

AS_OF = datetime(2026, 8, 24, 8, 0, tzinfo=UTC)
SCHOOL_ID = uuid4()
PERSON_ID = uuid4()
ASSIGNMENT_ID = uuid4()
DUTY_ID = uuid4()
SCOPE_ID = uuid4()
GRANT_ID = uuid4()


def _module(name: str) -> Any:
    return importlib.import_module(f"app.platform.authorization.{name}")


def _actor(*, account_user_id: int = 7) -> Any:
    return platform_contracts._issue_staff_actor_context(  # pyright: ignore[reportPrivateUsage]
        account_user_id=account_user_id,
        school_id=SchoolId(SCHOOL_ID),
        person_id=PersonId(PERSON_ID),
        assignment_id=AssignmentId(ASSIGNMENT_ID),
        assignment_version=3,
        authorization_version=5,
    )


def _governance_dependencies() -> tuple[GovernanceDependencyVersion, ...]:
    dependencies = (
        GovernanceDependencyVersion(
            authority="school_facts.assignment",
            reference=str(ASSIGNMENT_ID),
            version=3,
        ),
        GovernanceDependencyVersion(
            authority="school_facts.person",
            reference=str(PERSON_ID),
            version=4,
        ),
        GovernanceDependencyVersion(
            authority="school_facts.organization",
            reference=str(SCHOOL_ID),
            version=6,
        ),
        GovernanceDependencyVersion(
            authority="test-only.sf5a.scope-target",
            reference=f"school:{SCHOOL_ID}",
            version=11,
        ),
        GovernanceDependencyVersion(
            authority="governance.duty",
            reference=str(DUTY_ID),
            version=7,
        ),
        GovernanceDependencyVersion(
            authority="governance.scope",
            reference=str(SCOPE_ID),
            version=11,
        ),
        GovernanceDependencyVersion(
            authority="governance.grant",
            reference=str(GRANT_ID),
            version=13,
        ),
        GovernanceDependencyVersion(
            authority="governance.assignment_epoch",
            reference=str(ASSIGNMENT_ID),
            version=17,
        ),
    )
    return tuple(sorted(dependencies, key=lambda item: (item.authority, item.reference)))


def _thin_basis(
    *,
    request_id: UUID | None = None,
    decision_id: UUID | None = None,
    issued_as_of: datetime = AS_OF,
    dependencies: tuple[GovernanceDependencyVersion, ...] | None = None,
) -> ThinAuthorizationBasis:
    return ThinAuthorizationBasis(
        governance=GovernanceAuthorizationBasis(
            school_id=SCHOOL_ID,
            assignment_id=ASSIGNMENT_ID,
            duty_code="test_only.r1_reader.duty",
            scope_id=SCOPE_ID,
            grant_id=GRANT_ID,
            grant_version=13,
            assignment_epoch=17,
            issued_as_of=issued_as_of,
            valid_until=AS_OF + timedelta(hours=1),
            dependency_versions=dependencies or _governance_dependencies(),
        ),
        actor=StaffActorBasis(
            account_user_id=7,
            school_id=SchoolId(SCHOOL_ID),
            person_id=PersonId(PERSON_ID),
            assignment_id=AssignmentId(ASSIGNMENT_ID),
            assignment_version=3,
            authorization_version=5,
        ),
        module_name="test_only.r1.secure_reader",
        action_code="test_only.r1.public_location.read",
        module_policy_version=19,
        resource_type="test_only.r1.public_location",
        resource_id=SCHOOL_ID,
        resource_revision=5,
        request_id=request_id or uuid4(),
        decision_id=decision_id or uuid4(),
    )


def _replace_governance(
    basis: ThinAuthorizationBasis,
    **changes: object,
) -> ThinAuthorizationBasis:
    return replace(basis, governance=replace(basis.governance, **changes))


def _replace_dependency(
    basis: ThinAuthorizationBasis,
    authority: str,
    *,
    reference: str | None = None,
    version: int | None = None,
) -> ThinAuthorizationBasis:
    dependencies = tuple(
        replace(
            dependency,
            reference=dependency.reference if reference is None else reference,
            version=dependency.version if version is None else version,
        )
        if dependency.authority == authority
        else dependency
        for dependency in basis.governance.dependency_versions
    )
    return _replace_governance(basis, dependency_versions=dependencies)


def _relation(operation_code: str, *, after_id: UUID | None = None) -> Any:
    registry = _module("secure_reader_registry").test_operation_registry()
    return (
        _module("secure_reader_compiler")
        .ServerOwnedProjectionCompiler()
        .compile(
            operation=registry.require(operation_code),
            actor=_actor(),
            scope_plan=ScopePlan(
                scope_id=SCHOOL_ID,
                owner="test-only.r1.school",
                reference=f"school:{SCHOOL_ID}",
                version=1,
            ),
            as_of=AS_OF,
            after_id=after_id,
            page_size=25,
        )
    )


def test_self_assignment_uses_current_version_slice_not_legacy_root_content() -> None:
    sql = str(_relation("test_only.r1.self_current_assignment").data_statement).lower()

    assert "staff_assignment_versions" in sql
    assert "effective_from" in sql
    assert "effective_to" in sql
    assert "planned_valid_to" in sql
    assert "target_version" in sql


def test_public_location_reuses_canonical_epoch_terminal_and_ancestry_guard() -> None:
    sql = str(_relation("test_only.r1.public_location").data_statement).lower()

    assert "app.school_fact_location_history_is_resolvable" in sql
    assert "campus_location_versions.school_id" in sql
    assert "campus_location_versions.location_id" in sql


def test_cursor_count_and_data_share_the_same_remaining_filter() -> None:
    relation = _relation("test_only.r1.public_location", after_id=UUID(int=19))
    count_sql = str(relation.count_statement).lower()
    data_sql = str(relation.data_statement).lower()

    for predicate in ("campus_locations.id >", "valid_from <=", "valid_to >"):
        assert predicate in count_sql
        assert predicate in data_sql


def test_compiler_denies_unknown_or_cross_school_scope_plan() -> None:
    compiler = _module("secure_reader_compiler").ServerOwnedProjectionCompiler()
    operation = (
        _module("secure_reader_registry")
        .test_operation_registry()
        .require("test_only.r1.public_location")
    )
    for scope_plan in (
        ScopePlan(
            scope_id=SCHOOL_ID,
            owner="invented.production.scope",
            reference=f"school:{SCHOOL_ID}",
            version=1,
        ),
        ScopePlan(
            scope_id=uuid4(),
            owner="test-only.r1.school",
            reference=f"school:{SCHOOL_ID}",
            version=1,
        ),
    ):
        with pytest.raises(ValueError, match="scope"):
            compiler.compile(
                operation=operation,
                actor=_actor(),
                scope_plan=scope_plan,
                as_of=AS_OF,
                after_id=None,
                page_size=25,
            )


def test_compiler_accepts_existing_sf5a_test_only_typed_scope() -> None:
    compiler = _module("secure_reader_compiler").ServerOwnedProjectionCompiler()
    operation = (
        _module("secure_reader_registry")
        .test_operation_registry()
        .require("test_only.r1.public_location")
    )
    typed_scope_id = uuid4()

    relation = compiler.compile(
        operation=operation,
        actor=_actor(),
        scope_plan=ScopePlan(
            scope_id=typed_scope_id,
            owner="test-only.sf5a",
            reference=f"school:{SCHOOL_ID}",
            version=31,
        ),
        as_of=AS_OF,
        after_id=None,
        page_size=25,
    )

    assert relation.operation_code == "test_only.r1.public_location"


def test_counterfeit_actor_is_denied_before_authorization_or_sql() -> None:
    registry_api = _module("secure_reader_registry")
    service_api = _module("secure_reader_service")
    calls: list[str] = []
    counterfeit = SimpleNamespace(
        account_user_id=7,
        school_id=SchoolId(SCHOOL_ID),
        person_id=PersonId(PERSON_ID),
        assignment_id=AssignmentId(ASSIGNMENT_ID),
        assignment_version=3,
        authorization_version=5,
    )
    request = _module("secure_reader_contracts").SecureReadRequest(
        actor=counterfeit,
        operation_code="test_only.r1.public_location",
        page_size=10,
    )
    authorizer = SimpleNamespace(decide=lambda **_: calls.append("decide"))
    executor = SimpleNamespace(repeatable_read=lambda: calls.append("sql"))
    reader = service_api.SecureReader(
        registry=registry_api.test_operation_registry(),
        compiler=_module("secure_reader_compiler").ServerOwnedProjectionCompiler(),
        authorizer=authorizer,
        executor=executor,
        cursor_codec=service_api.SecureCursorCodec(b"test-secret"),
        clock=lambda: AS_OF,
    )

    with pytest.raises(service_api.SecureReadDenied):
        reader.read(request)
    assert calls == []


def test_sqlalchemy_executor_sets_repeatable_read_and_fetches_explicit_mappings() -> None:
    adapters = _module("secure_reader_adapters")
    relation = _relation("test_only.r1.public_location")

    class _Mappings:
        def all(self) -> list[dict[str, object]]:
            return [{"row_id": UUID(int=1)}]

    class _ExecuteResult:
        def mappings(self) -> _Mappings:
            return _Mappings()

    class _Session:
        def __init__(self) -> None:
            self.connection_options: list[dict[str, object]] = []
            self.statements: list[object] = []
            self.committed = False

        def connection(self, *, execution_options: dict[str, object]) -> None:
            self.connection_options.append(execution_options)

        def scalar(self, statement: object) -> int:
            self.statements.append(statement)
            return 1

        def execute(self, statement: object) -> _ExecuteResult:
            self.statements.append(statement)
            return _ExecuteResult()

        def commit(self) -> None:
            self.committed = True

        def rollback(self) -> None:
            pass

        def close(self) -> None:
            pass

    session = _Session()
    executor = adapters.SqlAlchemyRepeatableReadExecutor(lambda: session)
    with executor.repeatable_read() as transaction:
        assert transaction.count(relation) == 1
        assert transaction.fetch(relation) == ({"row_id": UUID(int=1)},)

    assert session.connection_options == [{"isolation_level": "REPEATABLE READ"}]
    assert session.statements == [relation.count_statement, relation.data_statement]
    assert session.committed is True


def _replace_actor_basis(
    basis: ThinAuthorizationBasis,
    **changes: object,
) -> ThinAuthorizationBasis:
    return replace(basis, actor=replace(basis.actor, **changes))


@pytest.mark.parametrize("changed", ["projection", "scope", "actor", "order", "short"])
def test_batch_adapter_checks_each_exact_response_and_fresh_projection(changed):
    from app.platform.authorization.secure_reader_adapters import ThinAuthorizationReadAdapter
    from app.platform.authorization.secure_reader_registry import ReadOperation
    from app.platform.authorization.thin_contracts import (
        ThinAuthorizationDecision,
        ThinDecisionKind,
    )

    state = {"changed": None}

    def decision(basis):
        return ThinAuthorizationDecision(
            ThinDecisionKind.ALLOW,
            None,
            ScopePlan(SCOPE_ID, "test-only.sf5a", f"school:{SCHOOL_ID}", 11),
            NamedProjection("public_location", 1),
            basis,
            basis.request_id,
            basis.decision_id,
        )

    class Thin:
        def decide_many(self, requests):
            results = [decision(_thin_basis(request_id=request.request_id)) for request in requests]
            if state["changed"] == "order":
                return tuple(reversed(results))
            if state["changed"] == "short":
                return ()
            if state["changed"] == "projection":
                results[1] = replace(results[1], projection=NamedProjection("different", 1))
            if state["changed"] == "scope":
                assert results[1].scope_plan is not None
                results[1] = replace(
                    results[1], scope_plan=replace(results[1].scope_plan, scope_id=uuid4())
                )
            if state["changed"] == "actor":
                assert isinstance(results[1].basis, ThinAuthorizationBasis)
                results[1] = replace(
                    results[1], basis=_replace_actor_basis(results[1].basis, account_user_id=8)
                )
            return tuple(results)

        def revalidate_many(self, bases, **kwargs):
            return tuple(
                replace(decision(basis), projection=NamedProjection("different", 1))
                for basis in bases
            )

    adapter = ThinAuthorizationReadAdapter(
        thin_service=Thin(), scope_id_resolver=lambda *_: SCOPE_ID
    )
    operation = ReadOperation(
        "test_only.r1.public_location",
        "public_location",
        "test_only.r1.public_location.read",
        "test_only.r1.secure_reader",
    )
    original = adapter.decide_many(operations=(operation, operation), actor=_actor(), as_of=AS_OF)
    assert all(auth.allowed for auth in original)
    assert adapter.revalidate_many(original, actor=_actor(), as_of=AS_OF) == (False, False)
    state["changed"] = changed
    if changed == "short":
        with pytest.raises(ValueError, match="Incomplete"):
            adapter.decide_many(operations=(operation, operation), actor=_actor(), as_of=AS_OF)
    else:
        checked = adapter.decide_many(
            operations=(operation, operation), actor=_actor(), as_of=AS_OF
        )
        assert tuple(auth.allowed for auth in checked) == (
            (False, False) if changed == "order" else (True, False)
        )


def test_thin_basis_fingerprint_excludes_only_per_evaluation_metadata() -> None:
    adapters = _module("secure_reader_adapters")
    basis = _thin_basis()
    reevaluated = _thin_basis(
        request_id=uuid4(),
        decision_id=uuid4(),
        issued_as_of=AS_OF + timedelta(seconds=1),
        dependencies=tuple(reversed(basis.governance.dependency_versions)),
    )

    fingerprint = adapters._fingerprint_authorization_basis  # pyright: ignore[reportPrivateUsage]

    assert fingerprint(basis) is not None
    assert fingerprint(reevaluated) == fingerprint(basis)


@pytest.mark.parametrize(
    "mutate",
    (
        pytest.param(
            lambda basis: _replace_actor_basis(basis, account_user_id=8),
            id="actor-account-user-id",
        ),
        pytest.param(
            lambda basis: _replace_actor_basis(basis, school_id=SchoolId(UUID(int=101))),
            id="actor-school-id",
        ),
        pytest.param(
            lambda basis: _replace_actor_basis(basis, person_id=PersonId(UUID(int=102))),
            id="actor-person-id",
        ),
        pytest.param(
            lambda basis: _replace_actor_basis(
                basis,
                assignment_id=AssignmentId(UUID(int=103)),
            ),
            id="actor-assignment-id",
        ),
        pytest.param(
            lambda basis: _replace_actor_basis(basis, assignment_version=4),
            id="actor-assignment-version",
        ),
        pytest.param(
            lambda basis: _replace_actor_basis(basis, authorization_version=6),
            id="actor-authorization-version",
        ),
        pytest.param(
            lambda basis: replace(basis, module_name="test_only.r1.changed"),
            id="module-name",
        ),
        pytest.param(
            lambda basis: replace(basis, action_code="test_only.r1.changed.read"),
            id="action-code",
        ),
        pytest.param(
            lambda basis: replace(basis, module_policy_version=20),
            id="module-policy-version",
        ),
        pytest.param(
            lambda basis: replace(basis, resource_type="test_only.r1.changed"),
            id="resource-type",
        ),
        pytest.param(
            lambda basis: replace(basis, resource_id=UUID(int=104)),
            id="resource-id",
        ),
        pytest.param(
            lambda basis: replace(basis, resource_revision=6),
            id="resource-revision",
        ),
        pytest.param(
            lambda basis: _replace_governance(basis, school_id=UUID(int=105)),
            id="governance-school-id",
        ),
        pytest.param(
            lambda basis: _replace_governance(
                _replace_dependency(
                    _replace_dependency(
                        basis,
                        "school_facts.assignment",
                        reference=str(UUID(int=106)),
                    ),
                    "governance.assignment_epoch",
                    reference=str(UUID(int=106)),
                ),
                assignment_id=UUID(int=106),
            ),
            id="governance-assignment-id",
        ),
        pytest.param(
            lambda basis: _replace_governance(basis, duty_code="test_only.changed.duty"),
            id="governance-duty-code",
        ),
        pytest.param(
            lambda basis: _replace_dependency(
                basis,
                "governance.duty",
                reference=str(UUID(int=107)),
            ),
            id="governance-duty-id",
        ),
        pytest.param(
            lambda basis: _replace_dependency(basis, "governance.duty", version=8),
            id="governance-duty-version",
        ),
        pytest.param(
            lambda basis: _replace_governance(
                _replace_dependency(
                    basis,
                    "governance.scope",
                    reference=str(UUID(int=108)),
                ),
                scope_id=UUID(int=108),
            ),
            id="governance-scope-id",
        ),
        pytest.param(
            lambda basis: _replace_dependency(basis, "governance.scope", version=12),
            id="governance-scope-version",
        ),
        pytest.param(
            lambda basis: _replace_governance(
                _replace_dependency(
                    basis,
                    "governance.grant",
                    reference=str(UUID(int=109)),
                ),
                grant_id=UUID(int=109),
            ),
            id="governance-grant-id",
        ),
        pytest.param(
            lambda basis: _replace_governance(
                _replace_dependency(basis, "governance.grant", version=14),
                grant_version=14,
            ),
            id="governance-grant-version",
        ),
        pytest.param(
            lambda basis: _replace_governance(
                _replace_dependency(basis, "governance.assignment_epoch", version=18),
                assignment_epoch=18,
            ),
            id="governance-assignment-epoch",
        ),
        pytest.param(
            lambda basis: _replace_governance(
                basis,
                valid_until=AS_OF + timedelta(hours=2),
            ),
            id="governance-valid-until",
        ),
        pytest.param(
            lambda basis: _replace_dependency(
                basis,
                "school_facts.organization",
                version=7,
            ),
            id="sorted-dependency-version",
        ),
    ),
)
def test_thin_basis_fingerprint_binds_every_stable_authorization_field(
    mutate: Callable[[ThinAuthorizationBasis], ThinAuthorizationBasis],
) -> None:
    adapters = _module("secure_reader_adapters")
    basis = _thin_basis()
    changed = mutate(basis)

    fingerprint = adapters._fingerprint_authorization_basis  # pyright: ignore[reportPrivateUsage]
    original_fingerprint = fingerprint(basis)
    changed_fingerprint = fingerprint(changed)

    assert original_fingerprint is not None
    assert changed_fingerprint is not None
    assert changed_fingerprint != original_fingerprint


def test_thin_authorization_adapter_builds_server_owned_request_and_revalidates() -> None:
    adapters = _module("secure_reader_adapters")
    registry_api = _module("secure_reader_registry")
    thin_contracts = _module("thin_contracts")
    operation = registry_api.test_operation_registry().require("test_only.r1.public_location")
    basis = _thin_basis()
    resolved_scope_id = SCOPE_ID

    class _ThinService:
        def __init__(self) -> None:
            self.requests: list[object] = []
            self.revalidations: list[object] = []

        def decide(self, request: object) -> object:
            self.requests.append(request)
            bound_basis = replace(
                basis,
                request_id=cast(Any, request).request_id,
            )
            return SimpleNamespace(
                kind=thin_contracts.ThinDecisionKind.ALLOW,
                basis=bound_basis,
                scope_plan=ScopePlan(
                    scope_id=resolved_scope_id,
                    owner="test-only.r1.school",
                    reference=f"school:{SCHOOL_ID}",
                    version=1,
                ),
                projection=NamedProjection(name=operation.projection_name, version=1),
            )

        def revalidate(self, basis: object, **kwargs: object) -> object:
            self.revalidations.append((basis, kwargs))
            return SimpleNamespace(
                kind=thin_contracts.ThinDecisionKind.ALLOW,
                basis=basis,
                scope_plan=ScopePlan(
                    scope_id=resolved_scope_id,
                    owner="test-only.r1.school",
                    reference=f"school:{SCHOOL_ID}",
                    version=1,
                ),
                projection=NamedProjection(name=operation.projection_name, version=1),
            )

    thin = _ThinService()
    adapter = adapters.ThinAuthorizationReadAdapter(
        thin_service=thin,
        scope_id_resolver=lambda _operation, _actor: resolved_scope_id,
    )
    authorization = adapter.decide(operation=operation, actor=_actor(), as_of=AS_OF)

    assert authorization.allowed is True
    assert cast(Any, thin.requests[0]).action_code == operation.action_code
    assert cast(Any, thin.requests[0]).module_name == operation.module_name
    assert cast(Any, thin.requests[0]).scope_id == resolved_scope_id
    assert adapter.revalidate(authorization, actor=_actor(), as_of=AS_OF) is True
    assert cast(Any, thin.revalidations[0])[0] == authorization.basis
    assert (
        adapter.revalidate(
            authorization,
            actor=_actor(account_user_id=8),
            as_of=AS_OF,
        )
        is False
    )
    assert len(thin.revalidations) == 1


@pytest.mark.parametrize(
    "mutate",
    (
        pytest.param(
            lambda basis: _replace_actor_basis(basis, account_user_id=8),
            id="actor-account-user-id",
        ),
        pytest.param(
            lambda basis: _replace_actor_basis(basis, school_id=SchoolId(UUID(int=201))),
            id="actor-school-id",
        ),
        pytest.param(
            lambda basis: _replace_actor_basis(basis, person_id=PersonId(UUID(int=202))),
            id="actor-person-id",
        ),
        pytest.param(
            lambda basis: _replace_actor_basis(basis, assignment_id=AssignmentId(UUID(int=203))),
            id="actor-assignment-id",
        ),
        pytest.param(
            lambda basis: _replace_actor_basis(basis, assignment_version=4),
            id="actor-assignment-version",
        ),
        pytest.param(
            lambda basis: _replace_actor_basis(basis, authorization_version=6),
            id="actor-authorization-version",
        ),
        pytest.param(
            lambda basis: replace(basis, module_name="test_only.r1.counterfeit"),
            id="module-name",
        ),
        pytest.param(
            lambda basis: replace(basis, action_code="test_only.r1.counterfeit.read"),
            id="action-code",
        ),
        pytest.param(
            lambda basis: replace(basis, resource_type="test_only.r1.counterfeit"),
            id="resource-type",
        ),
        pytest.param(
            lambda basis: replace(basis, resource_id=UUID(int=204)),
            id="resource-id",
        ),
        pytest.param(
            lambda basis: replace(basis, resource_revision=6),
            id="resource-revision",
        ),
        pytest.param(
            lambda basis: replace(basis, request_id=UUID(int=205)),
            id="request-id",
        ),
        pytest.param(
            lambda basis: _replace_governance(basis, school_id=UUID(int=206)),
            id="governance-school-id",
        ),
        pytest.param(
            lambda basis: _replace_governance(
                _replace_dependency(
                    _replace_dependency(
                        basis,
                        "school_facts.assignment",
                        reference=str(UUID(int=207)),
                    ),
                    "governance.assignment_epoch",
                    reference=str(UUID(int=207)),
                ),
                assignment_id=UUID(int=207),
            ),
            id="governance-assignment-id",
        ),
    ),
)
def test_thin_authorization_adapter_denies_allow_basis_not_bound_to_request(
    mutate: Callable[[ThinAuthorizationBasis], ThinAuthorizationBasis],
) -> None:
    adapters = _module("secure_reader_adapters")
    operation = (
        _module("secure_reader_registry")
        .test_operation_registry()
        .require("test_only.r1.public_location")
    )
    thin_contracts = _module("thin_contracts")

    class _ThinService:
        def decide(self, request: object) -> object:
            bound_basis = _thin_basis(request_id=cast(Any, request).request_id)
            return SimpleNamespace(
                kind=thin_contracts.ThinDecisionKind.ALLOW,
                basis=mutate(bound_basis),
                scope_plan=ScopePlan(
                    scope_id=SCOPE_ID,
                    owner="test-only.r1.school",
                    reference=f"school:{SCHOOL_ID}",
                    version=1,
                ),
                projection=NamedProjection(
                    name=operation.projection_name,
                    version=1,
                ),
            )

    adapter = adapters.ThinAuthorizationReadAdapter(
        thin_service=_ThinService(),
        scope_id_resolver=lambda _operation, _actor: SCOPE_ID,
    )

    authorization = adapter.decide(
        operation=operation,
        actor=_actor(),
        as_of=AS_OF,
    )

    assert authorization.allowed is False


@pytest.mark.parametrize("mode", ("missing", "wrong-type", "raises"))
def test_thin_authorization_adapter_denies_without_one_trusted_uuid_scope(mode: str) -> None:
    adapters = _module("secure_reader_adapters")
    operation = (
        _module("secure_reader_registry")
        .test_operation_registry()
        .require("test_only.r1.public_location")
    )
    thin_calls: list[object] = []

    class _ThinService:
        def decide(self, request: object) -> object:
            thin_calls.append(request)
            raise AssertionError("Thin service must not receive an unresolved scope")

    def _resolve_scope(_operation: object, _actor: object) -> object:
        if mode == "raises":
            raise RuntimeError("scope owner unavailable")
        return str(SCOPE_ID)

    if mode == "missing":
        adapter = adapters.ThinAuthorizationReadAdapter(thin_service=_ThinService())
    else:
        adapter = adapters.ThinAuthorizationReadAdapter(
            thin_service=_ThinService(),
            scope_id_resolver=_resolve_scope,
        )

    authorization = adapter.decide(operation=operation, actor=_actor(), as_of=AS_OF)

    assert authorization.allowed is False
    assert thin_calls == []
