from __future__ import annotations

import ast
import inspect
from dataclasses import asdict, dataclass
from datetime import datetime
from importlib import import_module
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import pytest

from app.platform import contracts as platform_contracts
from app.platform.contracts import AssignmentId, PersonId, SchoolId, StaffActorContext
from tests.governance.test_reader_basis import (
    _DependencyState,
    _load_reader_api,
    _reader_harness,
)
from tests.governance.test_writer_lifecycle import (
    AS_OF,
    FUTURE,
    _actor,
    _create_duty_and_scope,
    _grant_command,
    _writer_environment,
)
from tests.master_data.conftest import PostgresRoles

pytestmark = pytest.mark.integration

API_ROOT = Path(__file__).resolve().parents[2]
TEST_MODULE = "test_only_sf5a_fictitious_register"
TEST_ACTION = "test_only.sf5a.fictitious_register.read"
TEST_PROJECTION = "test_only.sf5a.fictitious_register.summary"


@dataclass(frozen=True, slots=True)
class _ThinApi:
    ThinAuthorizationRequest: type[Any]
    ModuleAuthorizationRequirement: type[Any]
    ScopePlan: type[Any]
    NamedProjection: type[Any]
    ThinAuthorizationBasis: type[Any]
    ThinAuthorizationDecision: type[Any]
    ThinDecisionKind: type[Any]
    ThinDenyReason: type[Any]
    ModulePolicy: type[Any]
    ThinAuthorizationService: type[Any]


def _load_thin_api() -> _ThinApi:
    """Load lazily so collection succeeds and the missing Slice 4 module is the RED."""

    contracts = import_module("app.platform.authorization.thin_contracts")
    service = import_module("app.platform.authorization.thin_service")
    return _ThinApi(
        ThinAuthorizationRequest=contracts.ThinAuthorizationRequest,
        ModuleAuthorizationRequirement=contracts.ModuleAuthorizationRequirement,
        ScopePlan=contracts.ScopePlan,
        NamedProjection=contracts.NamedProjection,
        ThinAuthorizationBasis=contracts.ThinAuthorizationBasis,
        ThinAuthorizationDecision=contracts.ThinAuthorizationDecision,
        ThinDecisionKind=contracts.ThinDecisionKind,
        ThinDenyReason=contracts.ThinDenyReason,
        ModulePolicy=contracts.ModulePolicy,
        ThinAuthorizationService=service.ThinAuthorizationService,
    )


@dataclass(slots=True)
class _TestOnlyModulePolicy:
    api: _ThinApi
    version: int = 7
    required_duty_code: str = "test_only.thin.duty"

    @property
    def module_name(self) -> str:
        return TEST_MODULE

    def requirement_for(self, request: object) -> Any | None:
        if getattr(request, "action_code", None) != TEST_ACTION:
            return None
        return self.api.ModuleAuthorizationRequirement(
            duty_code=self.required_duty_code,
            projection_name=TEST_PROJECTION,
            projection_version=1,
        )


def _request(
    api: _ThinApi,
    writer_harness: Any,
    *,
    scope_id: UUID,
    action_code: str = TEST_ACTION,
    resource_revision: int = 41,
    as_of: datetime = AS_OF,
    actor: StaffActorContext | object | None = None,
) -> Any:
    trusted_actor = actor or _staff_actor(writer_harness)
    return api.ThinAuthorizationRequest(
        actor=trusted_actor,
        module_name=TEST_MODULE,
        action_code=action_code,
        scope_id=scope_id,
        resource_type="test_only.fictitious_register",
        resource_id=uuid4(),
        resource_revision=resource_revision,
        request_id=uuid4(),
        as_of=as_of,
    )


def _staff_actor(
    writer_harness: Any,
    *,
    assignment_version: int = 1,
    authorization_version: int = 1,
) -> StaffActorContext:
    return platform_contracts._issue_staff_actor_context(  # pyright: ignore[reportPrivateUsage]
        account_user_id=writer_harness.facts.user_id,
        school_id=SchoolId(writer_harness.facts.school_id),
        person_id=PersonId(writer_harness.facts.person_id),
        assignment_id=AssignmentId(writer_harness.facts.assignment_id),
        assignment_version=assignment_version,
        authorization_version=authorization_version,
    )


def _service(api: _ThinApi, reader_harness: Any, policy: _TestOnlyModulePolicy) -> Any:
    return api.ThinAuthorizationService(
        governance_basis_service=reader_harness.basis_service,
        module_policies={TEST_MODULE: policy},
    )


def _assert_denied(api: _ThinApi, decision: Any) -> None:
    assert decision.kind is api.ThinDecisionKind.DENY
    assert isinstance(decision.reason, api.ThinDenyReason)
    assert decision.scope_plan is None
    assert decision.projection is None
    assert decision.basis is None


def _authority(writer_harness: Any, *, marker: str = "thin") -> tuple[Any, Any, Any]:
    duty, scope = _create_duty_and_scope(writer_harness, marker=marker)
    grant = writer_harness.writer.create_grant(
        _grant_command(
            writer_harness,
            duty=duty,
            scope=scope,
            marker=marker,
        )
    )
    return duty, scope, grant


def test_thin_authorization_contract_is_independent_typed_and_unmounted() -> None:
    api = _load_thin_api()

    assert getattr(api.ModulePolicy, "_is_protocol", False) is True
    assert set(cast(Any, api.ThinDecisionKind)) == {
        api.ThinDecisionKind.ALLOW,
        api.ThinDecisionKind.DENY,
    }
    assert "legacy" not in inspect.signature(api.ThinAuthorizationService).parameters
    request_parameters = inspect.signature(api.ThinAuthorizationRequest).parameters
    assert "actor" in request_parameters
    assert {"school_id", "assignment_id"}.isdisjoint(request_parameters)

    modules = (
        import_module("app.platform.authorization.thin_contracts"),
        import_module("app.platform.authorization.thin_service"),
    )
    thin_sources = "\n".join(
        Path(str(module.__file__)).read_text(encoding="utf-8") for module in modules
    )
    imported_modules: set[str] = set()
    for module in modules:
        source = Path(str(module.__file__)).read_text(encoding="utf-8")
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                imported_modules.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                prefix = "." * node.level
                if node.module is not None:
                    imported_modules.add(f"{prefix}{node.module}")
                else:
                    imported_modules.update(f"{prefix}{alias.name}" for alias in node.names)
    assert {
        ".bootstrap",
        ".dependencies",
        ".models",
        ".router",
        ".service",
        "app.platform.authorization.bootstrap",
        "app.platform.authorization.dependencies",
        "app.platform.authorization.models",
        "app.platform.authorization.router",
        "app.platform.authorization.service",
    }.isdisjoint(imported_modules)
    assert all("responsibility" not in module for module in imported_modules)
    assert "app.platform.authorization.service" not in thin_sources
    assert "app.platform.responsibility" not in thin_sources

    mounted_sources = [API_ROOT / "app" / "main.py", *sorted((API_ROOT / "app").rglob("router.py"))]
    for path in mounted_sources:
        source = path.read_text(encoding="utf-8")
        assert "thin_service" not in source
        assert "thin_contracts" not in source

    production_sources = [
        path
        for path in (API_ROOT / "app").rglob("*.py")
        if path.name not in {"thin_contracts.py", "thin_service.py"}
    ]
    assert all(TEST_ACTION not in path.read_text(encoding="utf-8") for path in production_sources)


def test_thin_allow_returns_only_scope_plan_named_projection_and_wrapped_basis(
    isolated_migration_schema: PostgresRoles,
) -> None:
    api = _load_thin_api()
    reader_api = _load_reader_api()
    with _writer_environment(isolated_migration_schema) as writer_harness:
        _duty, scope, grant = _authority(writer_harness)
        state = _DependencyState()
        reader_harness = _reader_harness(reader_api, writer_harness, state)
        policy = _TestOnlyModulePolicy(api)
        request = _request(api, writer_harness, scope_id=scope.target_id)
        decision = _service(api, reader_harness, policy).decide(request)

        assert decision.kind is api.ThinDecisionKind.ALLOW
        assert decision.reason is None
        assert isinstance(decision.scope_plan, api.ScopePlan)
        assert asdict(decision.scope_plan) == {
            "scope_id": scope.target_id,
            "owner": "test-only.sf5a",
            "reference": f"school:{writer_harness.facts.school_id}",
            "version": state.scope_target_version,
        }
        assert isinstance(decision.projection, api.NamedProjection)
        assert asdict(decision.projection) == {
            "name": TEST_PROJECTION,
            "version": 1,
        }
        projection_text = repr(asdict(decision.projection)).lower()
        assert not {"value", "values", "body", "content", "sensitive", "sql"} & set(
            asdict(decision.projection)
        )
        assert "select " not in projection_text
        assert isinstance(decision.basis, api.ThinAuthorizationBasis)
        assert asdict(decision.basis.actor) == {
            "account_user_id": writer_harness.facts.user_id,
            "school_id": writer_harness.facts.school_id,
            "person_id": writer_harness.facts.person_id,
            "assignment_id": writer_harness.facts.assignment_id,
            "assignment_version": 1,
            "authorization_version": 1,
        }
        assert decision.basis.governance.grant_id == grant.target_id
        assert decision.basis.module_name == TEST_MODULE
        assert decision.basis.action_code == TEST_ACTION
        assert decision.basis.module_policy_version == policy.version
        assert decision.basis.resource_type == request.resource_type
        assert decision.basis.resource_id == request.resource_id
        assert decision.basis.resource_revision == request.resource_revision
        assert decision.basis.request_id == request.request_id == decision.request_id
        assert decision.basis.decision_id == decision.decision_id
        assert not hasattr(decision.scope_plan, "sql")
        assert not hasattr(decision.scope_plan, "where")
        assert not hasattr(decision.scope_plan, "predicate")


def test_thin_deny_is_fail_closed_for_unknown_and_ambiguous_authority(
    isolated_migration_schema: PostgresRoles,
) -> None:
    api = _load_thin_api()
    reader_api = _load_reader_api()
    with _writer_environment(isolated_migration_schema) as writer_harness:
        duty, scope, _grant = _authority(writer_harness)
        state = _DependencyState()
        reader_harness = _reader_harness(reader_api, writer_harness, state)
        policy = _TestOnlyModulePolicy(api)
        service = _service(api, reader_harness, policy)

        untrusted_actor = service.decide(
            _request(
                api,
                writer_harness,
                scope_id=scope.target_id,
                actor=object(),
            )
        )
        _assert_denied(api, untrusted_actor)
        assert untrusted_actor.reason is api.ThinDenyReason.UNTRUSTED_ACTOR

        unknown_action = service.decide(
            _request(
                api,
                writer_harness,
                scope_id=scope.target_id,
                action_code="test_only.sf5a.unknown",
            )
        )
        _assert_denied(api, unknown_action)
        assert unknown_action.reason is api.ThinDenyReason.UNKNOWN_ACTION

        policy.required_duty_code = "test_only.sf5a.unknown_duty"
        _assert_denied(
            api,
            service.decide(_request(api, writer_harness, scope_id=scope.target_id)),
        )
        policy.required_duty_code = "test_only.thin.duty"

        _assert_denied(
            api,
            service.decide(_request(api, writer_harness, scope_id=uuid4())),
        )

        state.missing.add("assignment")
        _assert_denied(
            api,
            service.decide(_request(api, writer_harness, scope_id=scope.target_id)),
        )
        state.missing.clear()

        state.ambiguous.add("scope")
        _assert_denied(
            api,
            service.decide(_request(api, writer_harness, scope_id=scope.target_id)),
        )
        state.ambiguous.clear()

        writer_harness.writer.create_grant(
            _grant_command(
                writer_harness,
                duty=duty,
                scope=scope,
                marker="thin-parallel-ambiguous",
            )
        )
        ambiguous_grant = service.decide(_request(api, writer_harness, scope_id=scope.target_id))
        _assert_denied(api, ambiguous_grant)
        assert ambiguous_grant.reason is api.ThinDenyReason.GOVERNANCE_AMBIGUOUS


@pytest.mark.parametrize(
    ("change", "expected_reason"),
    (
        ("dependency", "GOVERNANCE_CHANGED"),
        ("epoch", "GOVERNANCE_CHANGED"),
        ("time", "BASIS_EXPIRED"),
        ("policy", "POLICY_CHANGED"),
        ("resource", "RESOURCE_CHANGED"),
        ("actor", "ACTOR_CHANGED"),
    ),
)
def test_thin_basis_revalidation_denies_every_version_time_and_resource_drift(
    isolated_migration_schema: PostgresRoles,
    change: str,
    expected_reason: str,
) -> None:
    api = _load_thin_api()
    reader_api = _load_reader_api()
    with _writer_environment(isolated_migration_schema) as writer_harness:
        duty, scope, grant = _authority(writer_harness)
        state = _DependencyState()
        reader_harness = _reader_harness(reader_api, writer_harness, state)
        policy = _TestOnlyModulePolicy(api)
        service = _service(api, reader_harness, policy)
        request = _request(api, writer_harness, scope_id=scope.target_id)
        allowed = service.decide(request)
        assert allowed.kind is api.ThinDecisionKind.ALLOW
        basis = allowed.basis
        assert basis is not None

        revalidate_as_of = AS_OF
        resource_revision = request.resource_revision
        revalidation_actor = request.actor
        if change == "dependency":
            state.person_version += 1
        elif change == "epoch":
            writer_harness.writer.update_grant(
                writer_harness.api.UpdateGrantCommand(
                    school_id=writer_harness.facts.school_id,
                    grant_id=grant.target_id,
                    expected_version=1,
                    effective_from=FUTURE,
                    planned_valid_to=None,
                    basis_authority="test-only.sf5a",
                    basis_reference="fixture://sf5a/thin/revalidate",
                    actor=_actor(writer_harness, reason="invalidate Thin basis epoch"),
                    idempotency_key="sf5a:thin:invalidate-epoch",
                )
            )
        elif change == "time":
            revalidate_as_of = state.valid_until
        elif change == "policy":
            policy.version += 1
        elif change == "resource":
            resource_revision += 1
        elif change == "actor":
            revalidation_actor = _staff_actor(writer_harness, authorization_version=2)
        else:
            raise AssertionError(f"unknown test change: {change}")

        denied = service.revalidate(
            basis,
            actor=revalidation_actor,
            as_of=revalidate_as_of,
            resource_revision=resource_revision,
        )
        _assert_denied(api, denied)
        assert denied.reason is getattr(api.ThinDenyReason, expected_reason)
