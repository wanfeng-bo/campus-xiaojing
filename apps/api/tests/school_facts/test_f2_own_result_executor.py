"""Own receipt transport stays distinct from confirmation and current scope is server-owned."""

from __future__ import annotations

import importlib
import importlib.util
import inspect
import json
from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError
from sqlalchemy import create_engine, event
from sqlalchemy.pool import QueuePool

from app.composition.school_fact_writer_executor import SqlAlchemySchoolFactWriterExecutorKernel
from app.platform.authorization.secure_reader_contracts import ReadAuthorization, SecureReadDenied
from app.platform.authorization.thin_contracts import NamedProjection, ScopePlan
from app.platform.school_facts.application import SchoolFactChangeRejected
from app.platform.school_facts.f2_policy import F2FactScope
from app.platform.school_facts.read_policy import SchoolFactAuthorizationUnavailable
from tests.school_facts.test_sf5b_writer_executor import (
    CALLER_KEY,
    CONTEXT_KEY,
    NOW,
    _context,
    _RecordingKernel,
    _writer_settings,
)

ORIGINAL_FIELDS = {
    "teaching_assignment": ("assignment_source_key", "offering_source_key", "teaching_role")
}


def original_result_kernel(monkeypatch, *, count=1, creation=False):
    from app.platform.school_facts.ports import (
        SchoolFactOwnOriginalResult,
        SchoolFactOwnOriginalSegment,
        SchoolFactOwnResultTarget,
    )

    calls = []
    state = {"denied": False, "revoked": False, "fail": False}
    receipt = replace(
        _receipt(),
        result=replace(_receipt().result, changed_count=count),
        targets=tuple(
            SchoolFactOwnResultTarget(
                "teaching_assignment", f"teaching:{i}", "upsert", 1 if creation else 2, creation
            )
            for i in range(count)
        ),
    )

    class Proof:
        allowed_targets = frozenset((t.fact_kind, t.source_key) for t in receipt.targets)
        creation_only_targets = allowed_targets if creation else frozenset()

        def revalidate(self):
            calls.append("metadata-post")

    class Authorizer:
        def for_targets(self, context, targets):
            return Proof()

        def authorize_original(self, context, target, fields):
            calls.append(("fields", target.source_key, fields))
            if state["denied"]:
                raise PermissionError("private denial")

            def post():
                calls.append("original-post")
                if state["revoked"]:
                    raise SecureReadDenied("revoked")

            return SimpleNamespace(revalidate=post)

    class Service:
        def locate_own_result_targets(self, **kwargs):
            return Proof.allowed_targets

        def check_own_result_by_locator(self, **kwargs):
            return receipt

        def read_own_original_target(self, *, context, result, target, fields):
            calls.append(("read", target.source_key))
            assert result == receipt
            if state["fail"]:
                raise RuntimeError("private source detail")
            return SchoolFactOwnOriginalResult(
                "available",
                target.target_version,
                target.operation,
                fields,
                (
                    SchoolFactOwnOriginalSegment(
                        NOW,
                        None,
                        {
                            "assignment_source_key": "assignment:chen",
                            "offering_source_key": "offering:original",
                            "teaching_role": "lead",
                        },
                    ),
                ),
                field_changes=True,
            )

    @contextmanager
    def connect():
        yield object()

    monkeypatch.setattr(
        "app.composition.school_fact_writer_executor.sessionmaker", lambda **_: None
    )
    kernel = SqlAlchemySchoolFactWriterExecutorKernel(
        engine=SimpleNamespace(connect=connect),
        settings=_writer_settings(),
        own_result_authorizer=Authorizer(),
    )
    monkeypatch.setattr(kernel, "_service", lambda **_: Service())
    return kernel, receipt, state, calls


def read_original(kernel, **kwargs):
    return kernel.check_own_result_by_locator(
        context=_context(),
        session_binding="new-session",
        locator_sha256="d" * 64,
        original_fields=ORIGINAL_FIELDS,
        **kwargs,
    )


def test_own_original_requires_fields_before_read_and_revalidates_afterward(monkeypatch):
    kernel, receipt, _, calls = original_result_kernel(monkeypatch)
    result = read_original(kernel)
    assert replace(result.targets[0], original_result=None) == receipt.targets[0]
    original = result.targets[0].original_result
    assert original.status == "available" and original.target_version == 2
    assert original.segments[0].content["assignment_source_key"] == "assignment:chen"
    assert calls.index(
        ("fields", "teaching:0", ORIGINAL_FIELDS["teaching_assignment"])
    ) < calls.index(("read", "teaching:0"))
    assert calls[-1] == "original-post"


@pytest.mark.parametrize("creation", [False, True])
def test_own_original_denied_fields_or_creation_only_keeps_metadata_without_values(
    monkeypatch, creation
):
    kernel, receipt, state, calls = original_result_kernel(monkeypatch, creation=creation)
    state["denied"] = True
    result = read_original(kernel)
    assert replace(result.targets[0], original_result=None) == receipt.targets[0]
    assert result.targets[0].original_result.status == "denied"
    assert result.targets[0].original_result.segments == ()
    assert not any(isinstance(c, tuple) and c[0] == "read" for c in calls)


def test_own_original_selected_page_never_reads_unselected_targets(monkeypatch):
    kernel, _, _, calls = original_result_kernel(monkeypatch, count=5)
    result = read_original(kernel, target_offset=1, target_limit=2)
    assert [t.original_result is not None for t in result.targets] == [
        False,
        True,
        True,
        False,
        False,
    ]
    assert [c[1] for c in calls if isinstance(c, tuple) and c[0] == "read"] == [
        "teaching:1",
        "teaching:2",
    ]


def test_own_original_final_revocation_cannot_release_values(monkeypatch):
    kernel, _, state, _ = original_result_kernel(monkeypatch)
    state["revoked"] = True
    with pytest.raises(SecureReadDenied):
        read_original(kernel)


def test_own_original_final_later_target_io_cannot_cross_earlier_permit_deadline(monkeypatch):
    from datetime import timedelta

    from app.composition.school_fact_education_authority import EducationOwnResultProof
    from app.composition.school_fact_read_evidence import register_release_time_window

    kernel, _, _, _ = original_result_kernel(monkeypatch, count=2)
    clock = [NOW]
    calls = [0, 0]

    def authorize(context, target, fields):
        index = int(target.source_key.rsplit(":", 1)[1])
        deadline = NOW + timedelta(seconds=index + 1)

        def revalidate_many(*args, **kwargs):
            calls[index] += 1
            if index == 1 and calls[index] == 2:
                clock[0] = NOW + timedelta(seconds=1)

        def register(permits, selected_clock):
            register_release_time_window(selected_clock, observed_at=NOW, valid_before=deadline)

        authority = SimpleNamespace(revalidate_many=revalidate_many, register_permit_times=register)
        return EducationOwnResultProof(
            frozenset({(target.fact_kind, target.source_key)}),
            (SimpleNamespace(actor=context.actor),),
            authority,
            lambda: clock[0],
        )

    monkeypatch.setattr(kernel._own_result_authorizer, "authorize_original", authorize)
    with pytest.raises(SecureReadDenied):
        read_original(kernel)


def test_own_original_source_failure_is_fixed_and_retains_original_receipt(monkeypatch):
    kernel, receipt, state, _ = original_result_kernel(monkeypatch)
    state["fail"] = True
    result = read_original(kernel)
    assert result.result == receipt.result
    assert result.targets[0].original_result.status == "unavailable"
    assert result.targets[0].original_result.segments == ()
    assert "private" not in repr(result)


@pytest.mark.parametrize(
    "extra",
    [
        {"target_limit": 4},
        {"target_limit": True},
        {"target_offset": -1},
        {"original_fields": {"teaching_assignment": ["payload; DROP TABLE x; --"]}},
    ],
)
def test_own_original_transport_rejects_unbounded_or_injected_selection(extra):
    from app.platform.school_facts.executor_contracts import (
        SchoolFactExecutorOwnResultLocatorRequest,
    )

    with pytest.raises(ValidationError):
        SchoolFactExecutorOwnResultLocatorRequest.model_validate(
            {
                "context_token": "synthetic-token",
                "session_binding": "new-session",
                "locator_sha256": "d" * 64,
                "original_fields": ORIGINAL_FIELDS,
                **extra,
            }
        )


@pytest.mark.parametrize(
    "malformed", [None, "missing", "wrong_fields", "wrong_version", "extra_target"]
)
def test_own_original_signed_http_roundtrip_rejects_incomplete_projection(monkeypatch, malformed):
    from app.composition.school_fact_executor_client_settings import (
        SchoolFactExecutorClientSettings,
    )
    from app.composition.school_fact_writer_client import (
        SchoolFactWriterExecutorClient,
        SchoolFactWriterExecutorUnavailable,
    )
    from app.composition.school_fact_writer_executor import create_school_fact_writer_executor_app
    from app.platform.identity.executor_context_codec import CommandContextTransportCodec

    kernel, _, _, _ = original_result_kernel(monkeypatch, count=2)
    codec = CommandContextTransportCodec(CONTEXT_KEY, clock=lambda: NOW)
    api = TestClient(
        create_school_fact_writer_executor_app(
            kernel=kernel,
            context_codec=codec,
            caller_auth_key=CALLER_KEY,
            enable_own_results=True,
        )
    )

    def transport(request):
        body = json.loads(request.content)
        assert codec.decode(body["context_token"]) == _context()
        assert body["original_fields"] == {
            "teaching_assignment": list(ORIGINAL_FIELDS["teaching_assignment"])
        }
        response = api.post(request.url.path, headers=dict(request.headers), json=body)
        assert response.status_code == 200, response.json()
        value = response.json()
        target = value["item"]["targets"][0]
        if malformed == "missing":
            target.pop("original_result")
        elif malformed == "wrong_fields":
            target["original_result"]["field_names"] = ["teaching_role"]
        elif malformed == "wrong_version":
            target["original_result"]["target_version"] += 1
        elif malformed == "extra_target":
            value["item"]["targets"][1]["original_result"] = target["original_result"]
        return httpx.Response(response.status_code, json=value)

    settings = SchoolFactExecutorClientSettings(
        base_url="http://writer:8010",
        caller_auth_key=SecretStr(CALLER_KEY.decode()),
        context_transport_hmac_key=SecretStr(CONTEXT_KEY.decode()),
    )
    with SchoolFactWriterExecutorClient(
        settings=settings, context_codec=codec, transport=httpx.MockTransport(transport)
    ) as client:
        if malformed is not None:
            with pytest.raises((SchoolFactWriterExecutorUnavailable, ValidationError)):
                read_original(client, target_limit=1)
        else:
            result = read_original(client, target_limit=1)
            assert (
                result.targets[0].original_result.segments[0].content["assignment_source_key"]
                == "assignment:chen"
            )
            assert result.targets[1].original_result is None


@pytest.mark.parametrize("failure", [None, "field", "object", "maintain_only", "actor"])
def test_own_original_real_thin_scope_requires_read_object_fields_and_fresh_grant(failure):
    from uuid import UUID

    from app.composition.school_fact_education_authority import EducationAuthority
    from app.composition.school_fact_education_execution import EducationOwnResultAuthorizer
    from app.composition.school_fact_education_settings import EducationConfiguration
    from app.platform.authorization.thin_service import ThinAuthorizationService
    from app.platform.governance.contracts import GovernanceDependencyVersion
    from app.platform.school_facts.f5_policy import F5FactPolicy, F5FactScope
    from app.platform.school_facts.ports import SchoolFactOwnResultTarget
    from tests.governance.test_f4_authority import NOW as AUTH_NOW
    from tests.governance.test_f4_authority import Governance, actor

    fields = ORIGINAL_FIELDS["teaching_assignment"]
    scope = F5FactScope(
        actor().school_id,
        "teaching_assignment",
        ("teaching:other" if failure == "object" else "teaching:a",),
        (),
        ("teaching_role",) if failure == "field" else fields,
    )

    class TeachingGovernance(Governance):
        def issue(self, query):
            basis = super().issue(query)
            return replace(
                basis,
                dependency_versions=tuple(
                    GovernanceDependencyVersion(
                        d.authority.replace("school_facts.f4", "school_facts.f5"),
                        d.reference,
                        d.version,
                    )
                    for d in basis.dependency_versions
                ),
            )

    governance = TeachingGovernance(scope, UUID(int=4))
    config = EducationConfiguration(
        approval_reference="f5-original",
        scope_bindings=(
            {
                "assignment_id": actor().assignment_id,
                "scope_id": UUID(int=4),
                "operation_code": "school_facts.f5.teaching_assignment."
                + ("maintain" if failure == "maintain_only" else "read"),
            },
        ),
    )
    authority = EducationAuthority(
        thin=ThinAuthorizationService(
            governance_basis_service=governance,
            module_policies={"school_facts.f5": F5FactPolicy()},
        ),
        configuration=config,
        clock=lambda: AUTH_NOW,
    )
    owner = EducationOwnResultAuthorizer(
        education=authority.own_result, retained=None, original_authority=authority
    )
    current_actor = _context().actor if failure == "actor" else actor()
    context = SimpleNamespace(actor=current_actor)
    target = SchoolFactOwnResultTarget("teaching_assignment", "teaching:a", "upsert", 2)
    if failure is not None:
        with pytest.raises(PermissionError):
            owner.authorize_original(context, target, fields)
    else:
        proof = owner.authorize_original(context, target, fields)
        proof.revalidate()
        governance.valid = False
        with pytest.raises(PermissionError):
            proof.revalidate()


def _authorization_module():
    name = "app.composition.school_fact_own_result_authorization"
    assert importlib.util.find_spec(name) is not None, "own receipt scope compiler is missing"
    return importlib.import_module(name)


def _grant(kind, purpose, keys, *, creation_keys=()):
    scope = F2FactScope(_context().actor.school_id, kind, tuple(keys), tuple(creation_keys))
    return ReadAuthorization(
        True,
        object(),
        ScopePlan(uuid4(), "school_facts.f2", scope.reference, 1),
        NamedProjection(f"f2_{kind}_{purpose}", 1),
        "unit-basis",
    )


class _Guard:
    def __init__(self, grants):
        self.grants = grants
        self.checked = []
        self.revoked = False

    def require_current(self, actor, kind, *, purpose):
        assert actor == _context().actor
        value = self.grants.get((kind, purpose))
        if isinstance(value, Exception):
            raise value
        if value is None:
            raise SchoolFactChangeRejected("no current grant")
        return value

    def revalidate(self, authorization, actor):
        assert actor == _context().actor
        self.checked.append(authorization)
        if self.revoked:
            raise SecureReadDenied("revoked")

    def require_current_many(self, actor):
        from app.platform.school_facts.f2_policy import F2_FACT_KINDS

        if self.revoked:
            raise SecureReadDenied("revoked before issuance")
        decisions = []
        for kind in sorted(F2_FACT_KINDS):
            try:
                auth = self.require_current(actor, kind, purpose="read")
            except SchoolFactChangeRejected:
                auth = ReadAuthorization(False, None, None, None, None)
            decisions.append((kind, auth))
        return tuple(decisions)

    def revalidate_many(self, authorizations, actor):
        for authorization in authorizations:
            self.revalidate(authorization, actor)


def test_own_receipt_scope_uses_only_exact_current_read_targets():
    module = _authorization_module()
    read = _grant("official_contact", "read", ("contact-a",))
    maintain = _grant("school_service", "maintain", ("service-b",), creation_keys=("new-c",))
    guard = _Guard({("official_contact", "read"): read, ("school_service", "maintain"): maintain})
    authorization = module.F2OwnResultAuthorizer(guard)(_context())
    assert authorization.allowed_targets == frozenset(
        {
            ("official_contact", "contact-a"),
        }
    )
    assert not guard.checked
    authorization.revalidate()
    assert set(map(id, guard.checked)) == {id(read)}
    guard.revoked = True
    with pytest.raises(SecureReadDenied):
        authorization.revalidate()


def test_exact_own_receipt_compiler_does_not_enumerate_all_fact_families():
    grant = _grant("school_service", "read", ("service-b", "unrelated"))
    guard = _Guard({("school_service", "read"): grant})
    guard.require_current_many = lambda actor: pytest.fail("must not enumerate all families")
    proof = (
        _authorization_module()
        .F2OwnResultAuthorizer(guard)
        .for_targets(_context(), frozenset({("school_service", "service-b")}))
    )
    assert proof.allowed_targets == frozenset({("school_service", "service-b")})
    proof.revalidate()
    assert guard.checked == [grant]


@pytest.mark.parametrize("located", [None, frozenset({("school_service", "service-b")})])
def test_single_receipt_metadata_precedes_exact_authority_and_postcheck(monkeypatch, located):
    calls = []

    class Authorization:
        allowed_targets = frozenset({("school_service", "service-b")})

        def revalidate(self):
            calls.append("post")

    class Authorizer:
        def __call__(self, context):
            pytest.fail("single locator must not enumerate all scopes")

        def for_targets(self, context, targets):
            assert targets == located
            calls.append("authorize")
            return Authorization()

    class Service:
        def locate_own_result_targets(self, **kwargs):
            calls.append("metadata")
            return located

        def check_own_result_by_locator(self, **kwargs):
            assert kwargs["allowed_targets"] == located
            calls.append("content")
            return None

    engine = create_engine("sqlite://")
    kernel = SqlAlchemySchoolFactWriterExecutorKernel(
        engine=engine, settings=_writer_settings(), own_result_authorizer=Authorizer()
    )
    monkeypatch.setattr(kernel, "_service", lambda **_: Service())
    assert (
        kernel.check_own_result_by_locator(
            context=_context(), session_binding="synthetic-session", locator_sha256="a" * 64
        )
        is None
    )
    assert calls == (
        ["metadata"] if located is None else ["metadata", "authorize", "content", "post"]
    )


def test_maintain_only_does_not_grant_historical_receipt_reading():
    module = _authorization_module()
    guard = _Guard(
        {("school_service", "maintain"): _grant("school_service", "maintain", ("service-b",))}
    )
    with pytest.raises(SchoolFactChangeRejected):
        module.F2OwnResultAuthorizer(guard)(_context())


@pytest.mark.parametrize(
    "projection",
    [None, NamedProjection("f2_person_read", 1), NamedProjection("f2_official_contact_read", 2)],
)
def test_own_receipt_scope_rejects_missing_wrong_or_changed_field_projection(projection):
    module = _authorization_module()
    grant = replace(_grant("official_contact", "read", ("contact-a",)), projection=projection)
    with pytest.raises(SchoolFactAuthorizationUnavailable):
        module.F2OwnResultAuthorizer(_Guard({("official_contact", "read"): grant}))(_context())


def test_own_receipt_scope_preserves_source_failure_and_no_grant_denial():
    module = _authorization_module()
    with pytest.raises(SchoolFactChangeRejected):
        module.F2OwnResultAuthorizer(_Guard({}))(_context())
    with pytest.raises(SchoolFactAuthorizationUnavailable):
        module.F2OwnResultAuthorizer(
            _Guard(
                {
                    ("official_contact", "read"): SchoolFactAuthorizationUnavailable(
                        "owner offline"
                    ),
                }
            )
        )(_context())


def test_writer_own_receipt_requires_explicit_authorizer():
    parameters = inspect.signature(SqlAlchemySchoolFactWriterExecutorKernel).parameters
    assert "own_result_authorizer" in parameters, "Writer must compile its own current scope"
    assert parameters["own_result_authorizer"].default is None


@pytest.mark.parametrize("malformed", ["missing", "reversed"])
def test_own_receipt_rejects_incomplete_or_reordered_authorization_batch(malformed):
    class Guard(_Guard):
        def require_current_many(self, actor):
            values = super().require_current_many(actor)
            return values[:-1] if malformed == "missing" else tuple(reversed(values))

    with pytest.raises(SchoolFactAuthorizationUnavailable):
        _authorization_module().F2OwnResultAuthorizer(Guard({}))(_context())


def test_own_receipt_uses_one_eighteen_action_batch_and_fresh_batch_revalidation():
    from uuid import NAMESPACE_URL, uuid5

    from app.composition.school_fact_f2_query_repository import F2FinalAuthorizationGuard
    from app.composition.school_fact_product_services import ProductThinAuthorizationService
    from app.platform.governance.contracts import (
        GovernanceAuthorizationBasis,
        GovernanceDependencyVersion,
    )
    from app.platform.school_facts.f2_policy import F2FactPolicy

    who = _context().actor
    events = []
    live = {"valid": True, "failure": False}

    class Authority:
        def issue(self, query):
            raise AssertionError("single authority issuance is forbidden in this bounded path")

        def validate(self, basis, *, as_of):
            raise AssertionError("single revalidation is forbidden in this bounded path")

        def issue_many(self, queries):
            from sqlalchemy.exc import SQLAlchemyError

            if live["failure"]:
                raise SQLAlchemyError("owner unavailable")
            events.append(("issue", len(queries)))
            result = []
            for query in queries:
                kind = query.duty_code.removeprefix("f2_").removesuffix("_reader")
                scope = F2FactScope(who.school_id, kind, ("exact",))
                grant = uuid5(NAMESPACE_URL, query.duty_code)
                dependencies = (
                    GovernanceDependencyVersion(
                        "school_facts.assignment", str(who.assignment_id), who.assignment_version
                    ),
                    GovernanceDependencyVersion("school_facts.person", str(who.person_id), 1),
                    GovernanceDependencyVersion("school_facts.f2.scope-target", scope.reference, 1),
                    GovernanceDependencyVersion("governance.duty", str(grant), 1),
                    GovernanceDependencyVersion("governance.scope", str(query.scope_id), 1),
                    GovernanceDependencyVersion("governance.grant", str(grant), 1),
                    GovernanceDependencyVersion(
                        "governance.assignment_epoch", str(who.assignment_id), 1
                    ),
                )
                result.append(
                    GovernanceAuthorizationBasis(
                        who.school_id,
                        who.assignment_id,
                        query.duty_code,
                        query.scope_id,
                        grant,
                        1,
                        1,
                        query.as_of,
                        None,
                        dependencies,
                    )
                )
            return tuple(result)

        def validate_many(self, bases, *, as_of):
            events.append(("validate", len(bases)))
            return tuple(live["valid"] for _ in bases)

    thin = ProductThinAuthorizationService(
        governance_basis_service=Authority(), module_policies={"school_facts.f2": F2FactPolicy()}
    )
    guard = F2FinalAuthorizationGuard(
        thin_service=thin,
        scope_id_resolver=lambda operation, actor: uuid5(NAMESPACE_URL, operation.code),
    )
    authorizer = _authorization_module().F2OwnResultAuthorizer(guard)
    authorization = authorizer(_context())
    assert len(authorization.allowed_targets) == 18
    assert events == [("issue", 18)]
    authorization.revalidate()
    assert events == [("issue", 18), ("validate", 18)]
    live["valid"] = False
    with pytest.raises(SecureReadDenied):
        authorization.revalidate()
    live["failure"] = True
    with pytest.raises(SchoolFactAuthorizationUnavailable):
        authorizer(_context())


@pytest.mark.parametrize(
    "field", ["school_id", "actor_user_id", "actor_person_id", "allowed_targets"]
)
def test_own_receipt_transport_rejects_caller_selected_identity_and_scope(field):
    from app.platform.school_facts import executor_contracts as contracts

    model = getattr(contracts, "SchoolFactExecutorOwnResultListRequest", None)
    assert model is not None, "separate own receipt request is missing"
    with pytest.raises(ValidationError):
        model.model_validate(
            {
                "context_token": "a" * 80,
                "session_binding": "current-session",
                "limit": 20,
                field: "caller-choice",
            }
        )


def test_writer_checks_current_scope_after_waiting_for_its_pool(monkeypatch):
    from app.platform.school_facts.ports import SchoolFactOwnResultPage

    module = _authorization_module()
    guard = _Guard({("school_service", "read"): _grant("school_service", "read", ("service-b",))})
    engine = create_engine("sqlite://", poolclass=QueuePool, pool_size=1, max_overflow=0)
    accessed = []

    def revoke_on_checkout(*_):
        guard.revoked = True

    class Service:
        def list_own_results(self, **_):
            with engine.connect():
                accessed.append("receipt-payload")
            return SchoolFactOwnResultPage((), False)

    kernel = SqlAlchemySchoolFactWriterExecutorKernel(
        engine=engine,
        settings=_writer_settings(),
        own_result_authorizer=module.F2OwnResultAuthorizer(guard),
    )
    monkeypatch.setattr(kernel, "_service", lambda **_: Service())
    event.listen(engine, "checkout", revoke_on_checkout)
    try:
        with pytest.raises(SecureReadDenied):
            kernel.list_own_results(context=_context(), session_binding="new-session")
        assert accessed == [], "pool waiting must finish before the current scope check"
        assert engine.pool.checkedout() == 0
    finally:
        engine.dispose()


def _receipt():
    from app.platform.school_facts.ports import (
        SchoolFactApplyResult,
        SchoolFactOwnResult,
        SchoolFactOwnResultTarget,
    )

    return SchoolFactOwnResult(
        "d" * 64,
        NOW,
        SchoolFactApplyResult(uuid4(), 13, 1, True),
        (SchoolFactOwnResultTarget("school_service", "service-b", "upsert", 13),),
    )


class _OwnKernel(_RecordingKernel):
    def list_own_results(self, *, context, session_binding, limit=20):
        from app.platform.school_facts.ports import SchoolFactOwnResultPage

        assert session_binding == "new-session"
        assert 1 <= limit <= 20
        self.contexts.append(context)
        self.operations.append("own-list")
        return SchoolFactOwnResultPage((_receipt(),), False)

    def check_own_result_by_locator(self, *, context, session_binding, locator_sha256):
        assert session_binding == "new-session"
        assert locator_sha256 == "d" * 64
        self.contexts.append(context)
        self.operations.append("own-lookup")
        return _receipt()


def _own_app(*, enabled=True):
    from app.composition.school_fact_writer_executor import create_school_fact_writer_executor_app
    from app.platform.identity.executor_context_codec import CommandContextTransportCodec

    kernel = _OwnKernel()
    codec = CommandContextTransportCodec(CONTEXT_KEY, clock=lambda: NOW)
    app = create_school_fact_writer_executor_app(
        kernel=kernel,
        context_codec=codec,
        caller_auth_key=CALLER_KEY,
        enable_own_results=enabled,
    )
    return TestClient(app), kernel, codec


@pytest.mark.parametrize("name", ["list", "lookup", "target-page"])
@pytest.mark.parametrize("failure", ["denied", "unavailable", "unknown"])
def test_own_read_transport_distinguishes_known_denial_from_source_failure(name, failure):
    from app.composition.school_fact_writer_client import SchoolFactWriterExecutorUnavailable
    from app.composition.school_fact_writer_executor import create_school_fact_writer_executor_app
    from app.platform.identity.executor_context_codec import CommandContextTransportCodec
    from tests.school_facts.test_sf5b_writer_executor import _mock_executor_client

    marker = "synthetic-private-error-must-not-escape"

    class FailingKernel(_OwnKernel):
        def _fail(self, **_kwargs):
            if failure == "denied":
                raise PermissionError(marker)
            if failure == "unavailable":
                raise SchoolFactAuthorizationUnavailable(marker)
            raise RuntimeError(marker)

        list_own_results = _fail
        check_own_result_by_locator = _fail
        read_own_target_page = _fail

    codec = CommandContextTransportCodec(CONTEXT_KEY, clock=lambda: NOW)
    api = TestClient(create_school_fact_writer_executor_app(
        kernel=FailingKernel(), context_codec=codec, caller_auth_key=CALLER_KEY,
        enable_own_results=True,
    ), raise_server_exceptions=False)
    observed = []

    def transport(request):
        response = api.post(request.url.path, headers=dict(request.headers),
                            json=json.loads(request.content))
        observed.append(response)
        return httpx.Response(response.status_code, content=response.content)

    error = SchoolFactChangeRejected if failure == "denied" else SchoolFactWriterExecutorUnavailable
    with _mock_executor_client(httpx.MockTransport(transport)) as client:
        with pytest.raises(error) as raised:
            options = {"context": _context(), "session_binding": "new-login"}
            if name == "list":
                client.list_own_results(**options, limit=1)
            elif name == "lookup":
                client.check_own_result_by_locator(**options, locator_sha256="a" * 64)
            else:
                client.read_own_target_page(**options, locator_sha256="a" * 64,
                                            offset=0, limit=1)
    assert observed[0].status_code == {"denied": 403, "unavailable": 503, "unknown": 500}[failure]
    assert marker not in observed[0].text and marker not in str(raised.value)


@pytest.mark.parametrize("name", ["list", "lookup"])
def test_own_receipt_routes_require_signed_current_identity_and_never_confirm(name):
    client, kernel, codec = _own_app()
    path = "/internal/v1/school-facts/own-results/" + name
    payload = {"context_token": codec.encode(_context()), "session_binding": "new-session"}
    payload.update({"limit": 1} if name == "list" else {"locator_sha256": "d" * 64})
    headers = {"X-School-Fact-Executor-Key": CALLER_KEY.decode()}
    assert client.post(path, json=payload).status_code == 401
    assert (
        client.post(path, headers=headers, json={**payload, "actor_user_id": 999}).status_code
        == 422
    )
    token = payload["context_token"]
    bad = token[:-1] + ("A" if token[-1] != "A" else "B")
    assert (
        client.post(path, headers=headers, json={**payload, "context_token": bad}).status_code
        == 401
    )
    assert kernel.operations == []
    response = client.post(path, headers=headers, json=payload)
    assert response.status_code == 200
    assert kernel.operations == ["own-list" if name == "list" else "own-lookup"]
    assert kernel.contexts == [_context()]
    item = response.json()["items"][0] if name == "list" else response.json()["item"]
    assert set(item) == {"locator_sha256", "confirmed_at", "result", "targets"}
    assert item["result"]["replayed"] is True
    assert "session" not in json.dumps(item)


def test_legacy_factory_does_not_expose_own_receipt_routes():
    client, kernel, _ = _own_app(enabled=False)
    for name in ("list", "lookup"):
        response = client.post("/internal/v1/school-facts/own-results/" + name, json={})
        assert response.status_code == 404
    assert kernel.operations == []


@pytest.mark.parametrize("name", ["list", "lookup"])
@pytest.mark.parametrize(
    "kind,operation",
    [
        ("student_class_membership", "correct"),
        ("student_class_membership", "cancel"),
        ("student_school_relationship", "correct"),
    ],
)
def test_formal_f4_receipt_operations_survive_writer_http(name, kind, operation, monkeypatch):
    from app.platform.school_facts.executor_contracts import (
        SchoolFactExecutorOwnResultLookupResponse,
        SchoolFactExecutorOwnResultPage,
    )

    original = _receipt()
    receipt = replace(
        original, targets=(replace(original.targets[0], fact_kind=kind, operation=operation),)
    )
    monkeypatch.setattr(__import__(__name__, fromlist=["_receipt"]), "_receipt", lambda: receipt)
    client, _, codec = _own_app()
    path = "/internal/v1/school-facts/own-results/" + name
    payload = {"context_token": codec.encode(_context()), "session_binding": "new-session"}
    payload.update({"limit": 1} if name == "list" else {"locator_sha256": "d" * 64})
    headers = {"X-School-Fact-Executor-Key": CALLER_KEY.decode()}
    response = client.post(path, headers=headers, json=payload)
    assert response.status_code == 200
    model = (
        SchoolFactExecutorOwnResultPage
        if name == "list"
        else SchoolFactExecutorOwnResultLookupResponse
    )
    parsed = model.model_validate_json(response.content)
    result = parsed.items[0].to_domain() if name == "list" else parsed.item.to_domain()
    assert result == receipt


@pytest.mark.parametrize(
    "kind,operation",
    [
        ("school_service", "correct"),
        ("guardian_relationship", "cancel"),
        ("student_school_relationship", "cancel"),
        ("education_class", "delete"),
    ],
)
def test_f4_receipt_transport_does_not_extend_other_fact_operations(kind, operation):
    from app.platform.school_facts.executor_contracts import SchoolFactExecutorOwnResultTarget

    with pytest.raises(ValidationError):
        SchoolFactExecutorOwnResultTarget(
            fact_kind=kind, source_key="synthetic", operation=operation, target_version=1
        )


@pytest.mark.parametrize("name", ["list", "lookup"])
def test_writer_client_signs_identity_without_transmitting_caller_scope(name):
    from app.composition.school_fact_executor_client_settings import (
        SchoolFactExecutorClientSettings,
    )
    from app.composition.school_fact_writer_client import SchoolFactWriterExecutorClient

    api, kernel, codec = _own_app()
    seen = []

    def transport(request):
        body = json.loads(request.content)
        assert set(body) == (
            {"context_token", "session_binding", "limit"}
            if name == "list"
            else {"context_token", "session_binding", "locator_sha256"}
        )
        assert codec.decode(body["context_token"]) == _context()
        seen.append(request.url.path)
        response = api.post(request.url.path, headers=dict(request.headers), json=body)
        return httpx.Response(response.status_code, json=response.json())

    settings = SchoolFactExecutorClientSettings(
        base_url="http://writer:8010",
        caller_auth_key=SecretStr(CALLER_KEY.decode()),
        context_transport_hmac_key=SecretStr(CONTEXT_KEY.decode()),
    )
    with SchoolFactWriterExecutorClient(
        settings=settings, context_codec=codec, transport=httpx.MockTransport(transport)
    ) as client:
        value = (
            client.list_own_results(context=_context(), session_binding="new-session", limit=1)
            if name == "list"
            else client.check_own_result_by_locator(
                context=_context(), session_binding="new-session", locator_sha256="d" * 64
            )
        )
    item = value.items[0] if name == "list" else value
    assert item.locator_sha256 == "d" * 64
    assert seen == ["/internal/v1/school-facts/own-results/" + name]
    assert kernel.operations == ["own-list" if name == "list" else "own-lookup"]


@pytest.mark.parametrize("failure", ["wrong_locator", "missing_targets", "new_execution"])
def test_writer_client_rejects_incomplete_or_unrelated_receipt_responses(failure):
    from app.composition.school_fact_executor_client_settings import (
        SchoolFactExecutorClientSettings,
    )
    from app.composition.school_fact_writer_client import (
        SchoolFactWriterExecutorClient,
        SchoolFactWriterExecutorUnavailable,
    )
    from app.platform.school_facts.executor_contracts import SchoolFactExecutorOwnResult

    item = SchoolFactExecutorOwnResult.from_domain(_receipt()).model_dump(mode="json")
    if failure == "wrong_locator":
        item["locator_sha256"] = "e" * 64
    elif failure == "missing_targets":
        item["targets"] = []
    else:
        item["result"]["replayed"] = False
    settings = SchoolFactExecutorClientSettings(
        base_url="http://writer:8010",
        caller_auth_key=SecretStr(CALLER_KEY.decode()),
        context_transport_hmac_key=SecretStr(CONTEXT_KEY.decode()),
    )
    with (
        SchoolFactWriterExecutorClient(
            settings=settings,
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, json={"found": True, "item": item})
            ),
        ) as client,
        pytest.raises(SchoolFactWriterExecutorUnavailable),
    ):
        client.check_own_result_by_locator(
            context=_context(), session_binding="new-session", locator_sha256="d" * 64
        )


@pytest.mark.parametrize("lookup", [False, True])
def test_writer_releases_reserved_connection_when_post_read_authority_changes(monkeypatch, lookup):
    from app.platform.school_facts.ports import SchoolFactOwnResultPage

    module = _authorization_module()
    guard = _Guard({("school_service", "read"): _grant("school_service", "read", ("service-b",))})
    engine = create_engine("sqlite://", poolclass=QueuePool, pool_size=1, max_overflow=0)
    observed = []

    def service(**options):
        factory = options["session_factory"]

        class Service:
            def locate_own_result_targets(self, **kwargs):
                return frozenset({("school_service", "service-b")})

            def list_own_results(self, **kwargs):
                assert kwargs["allowed_targets"] == frozenset({("school_service", "service-b")})
                with factory() as session, session.begin():
                    assert session.connection().engine is engine
                    observed.append(engine.pool.checkedout())
                guard.revoked = True
                return SchoolFactOwnResultPage((), False)

            def check_own_result_by_locator(self, **kwargs):
                self.list_own_results(**kwargs)
                return None

        return Service()

    kernel = SqlAlchemySchoolFactWriterExecutorKernel(
        engine=engine,
        settings=_writer_settings(),
        own_result_authorizer=module.F2OwnResultAuthorizer(guard),
    )
    monkeypatch.setattr(kernel, "_service", service)
    try:
        with pytest.raises(SecureReadDenied):
            if lookup:
                kernel.check_own_result_by_locator(
                    context=_context(), session_binding="new-session", locator_sha256="d" * 64
                )
            else:
                kernel.list_own_results(context=_context(), session_binding="new-session")
        assert observed == [1]
        assert engine.pool.checkedout() == 0
    finally:
        engine.dispose()
