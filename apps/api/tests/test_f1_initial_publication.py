from __future__ import annotations

import importlib
import importlib.util
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest

from app.platform.governance.contracts import ScopeResolutionRequest
from app.platform.school_facts.application import SchoolFactChangeRejected
from app.platform.school_facts.contracts import CanonicalFactChangeSetV1

NOW = datetime(2026, 9, 5, 8, tzinfo=UTC)
SCHOOL = UUID(int=11)
INIT = UUID(int=18)
ASSIGNMENT = UUID(int=14)
GRANT = UUID(int=21)


def api():
    path = "app.composition.school_fact_initial_publication"
    assert importlib.util.find_spec(path) is not None, (
        "missing fixed initial publication composition"
    )
    return importlib.import_module(path)


def change_set(**updates):
    value: dict[str, Any] = dict(
        source_sha256="a" * 64,
        base_version=2,
        changes=[
            dict(
                operation="upsert",
                fact_kind="location",
                source_key="f1:campus",
                expected_version=0,
                effective_at=NOW,
                origin=dict(sheet_code="f1", row_number=1),
                content=dict(
                    kind="campus",
                    display_name="合成主校区",
                    display_path="合成主校区",
                    status="active",
                ),
            ),
            dict(
                operation="upsert",
                fact_kind="official_contact",
                source_key="f1:contact",
                expected_version=0,
                effective_at=NOW,
                origin=dict(sheet_code="f1", row_number=2),
                content=dict(
                    owner_kind="location",
                    location_source_key="f1:campus",
                    contact_kind="phone",
                    contact_value="0571-00000000",
                    lifecycle_status="active",
                    visibility_scope="public",
                ),
            ),
        ],
    )
    value.update(updates)
    value["changes"] = tuple(value["changes"])
    return CanonicalFactChangeSetV1.model_validate(value)


def manifest(**updates):
    values = dict(
        school_id=SCHOOL,
        initialization_id=INIT,
        assignment_id=ASSIGNMENT,
        request_id=UUID(int=19),
        approved_at=NOW,
        expires_at=NOW + timedelta(minutes=10),
        approval_reference="合成工程最小样本已确认",
        change_set=change_set(),
    )
    values.update(updates)
    return api().InitialPublicationManifest(**values)


def test_manifest_accepts_only_exact_two_facts_and_binds_approval_and_deadline():
    first = manifest()
    assert first.scope_key == f"f1:initial-publication:{INIT}"
    assert manifest(approval_reference="另一批准").manifest_sha256 != first.manifest_sha256
    assert manifest(expires_at=NOW + timedelta(minutes=11)).manifest_sha256 != first.manifest_sha256
    assert first.scope_document["canonical_plan_sha256"] == first.plan_sha256
    assert first.scope_document["publication_manifest_sha256"] == first.manifest_sha256


@pytest.mark.parametrize(
    "alteration", ["extra", "update", "other_campus", "expired", "long", "other_time"]
)
def test_manifest_rejects_unapproved_expansion(alteration):
    values = change_set().model_dump(mode="python", exclude_none=True)
    kwargs = {}
    if alteration == "extra":
        values["changes"] = (*values["changes"], {**values["changes"][0], "source_key": "f1:extra"})
    elif alteration == "update":
        values["changes"][0]["expected_version"] = 1
    elif alteration == "other_campus":
        values["changes"][1]["content"]["location_source_key"] = "f1:other"
    elif alteration == "expired":
        kwargs["expires_at"] = NOW
    elif alteration == "long":
        kwargs["expires_at"] = NOW + timedelta(hours=1)
    else:
        values["changes"][0]["effective_at"] = NOW - timedelta(seconds=1)
    with pytest.raises(ValueError):
        manifest(change_set=CanonicalFactChangeSetV1.model_validate(values), **kwargs)


def test_scope_is_exact_approved_document_and_expires():
    m = manifest()
    resolver = api().InitialPublicationScopeResolver()
    query = ScopeResolutionRequest(
        school_id=SCHOOL,
        scope_id=UUID(int=16),
        schema_owner=api().PUBLICATION_MODULE,
        schema_name="approved_manifest",
        schema_version=1,
        reference_document=m.scope_document,
        reference_sha256=api().document_sha256(m.scope_document),
        as_of=NOW,
    )
    result = resolver.resolve(query)
    assert result.reference == f"manifest:{m.manifest_sha256}"
    assert result.valid_until is None
    assert resolver.resolve(replace(query, as_of=m.expires_at)).is_effective
    assert (
        resolver.resolve(replace(query, reference_document={**m.scope_document, "extra": True}))
        is None
    )
    assert resolver.resolve(replace(query, reference_sha256="b" * 64)) is None


def actor():
    from app.platform import contracts

    return contracts._issue_staff_actor_context(
        account_user_id=7,
        school_id=contracts.SchoolId(SCHOOL),
        person_id=contracts.PersonId(UUID(int=13)),
        assignment_id=contracts.AssignmentId(ASSIGNMENT),
        assignment_version=1,
        authorization_version=1,
    )


def environment(*, revoked=False, at=NOW, current_grant=GRANT, expected_grant=GRANT):
    from app.composition.school_fact_product_services import ProductThinAuthorizationService
    from app.platform.contracts import _issue_command_execution_context
    from app.platform.governance.contracts import (
        GovernanceAuthorizationBasis,
        GovernanceDependencyVersion,
    )

    m = manifest()
    events = []

    class Basis:
        def issue(self, query):
            events.append("issue")
            return GovernanceAuthorizationBasis(
                school_id=SCHOOL,
                assignment_id=ASSIGNMENT,
                duty_code=api().PUBLICATION_DUTY,
                scope_id=query.scope_id,
                grant_id=current_grant,
                grant_version=1,
                assignment_epoch=3,
                issued_as_of=query.as_of,
                valid_until=at + timedelta(minutes=10),
                dependency_versions=(
                    GovernanceDependencyVersion(
                        api().PUBLICATION_MODULE + ".scope-target",
                        f"manifest:{m.manifest_sha256}",
                        1,
                    ),
                    GovernanceDependencyVersion("school_facts.assignment", str(ASSIGNMENT), 1),
                    GovernanceDependencyVersion("school_facts.person", str(UUID(int=13)), 1),
                    GovernanceDependencyVersion("governance.duty", str(UUID(int=25)), 1),
                    GovernanceDependencyVersion("governance.scope", str(query.scope_id), 1),
                    GovernanceDependencyVersion("governance.grant", str(current_grant), 1),
                    GovernanceDependencyVersion("governance.assignment_epoch", str(ASSIGNMENT), 3),
                ),
            )

        def validate(self, basis, *, as_of):
            events.append("validate")
            return not revoked

    class Session:
        def execute(self, statement, params):
            assert "require_initial_publication_window" in str(statement)
            assert params["manifest_sha"] == m.manifest_sha256
            events.append("window")

        def scalar(self, statement, params):
            events.append("epoch")
            assert params == {"school_id": SCHOOL, "assignment_id": ASSIGNMENT}
            return 3

    thin = ProductThinAuthorizationService(
        governance_basis_service=Basis(),
        module_policies={api().PUBLICATION_MODULE: api().InitialPublicationPolicy(m)},
    )
    guard = api().InitialPublicationFinalGuard(
        manifest=m,
        scope_id=UUID(int=16),
        grant_id=expected_grant,
        thin_service=thin,
        clock=lambda: at,
    )
    context = _issue_command_execution_context(
        actor=actor(), request_id=m.request_id, idempotency_key="f1-publication"
    )
    return m, guard, context, Session(), events


def test_guard_locks_epoch_and_uses_real_thin_for_exact_manifest():
    m, guard, context, session, events = environment()
    guard(session, context, m.change_set)
    assert events[:2] == ["epoch", "window"] and "issue" in events and "validate" in events


def test_guard_rejects_other_plan_before_db_and_revocation_inside_final_boundary():
    m, guard, context, session, events = environment()
    with pytest.raises(SchoolFactChangeRejected):
        guard(session, context, change_set(source_sha256="b" * 64))
    assert events == []
    m, guard, context, session, _ = environment(revoked=True)
    with pytest.raises(SchoolFactChangeRejected):
        guard(session, context, m.change_set)


def test_prepare_confirm_receipt_uses_existing_writer_kernel_and_does_not_auto_confirm():
    from app.composition.school_fact_product_identity import ProductIdentity
    from app.platform.school_facts.contracts import ConfirmationReceiptV1
    from app.platform.school_facts.ports import SchoolFactApplyResult, SchoolFactResultLookup

    m = manifest()
    events = []

    class Kernel:
        def preflight(self, **kw):
            events.append("preflight")
            return SimpleNamespace(is_clean=True, plan_sha256=m.plan_sha256)

        def prepare(self, **kw):
            events.append("prepare")
            assert kw["provenance"].result_locator_sha256 == api().publication_locator_sha256(
                m.locator_sha256, UUID(int=21)
            )
            return ConfirmationReceiptV1(
                plan_sha256=m.plan_sha256,
                credential="a" * 43,
                expires_at=NOW + timedelta(minutes=5),
            )

        def confirm(self, **kw):
            events.append("confirm")
            return SchoolFactApplyResult(
                batch_id=UUID(int=30), target_version=3, changed_count=2, replayed=False
            )

        def result_by_locator(self, **kw):
            events.append("result")
            return SchoolFactResultLookup(
                found=True,
                result=SchoolFactApplyResult(
                    batch_id=UUID(int=30), target_version=3, changed_count=2, replayed=False
                ),
            )

    identity = ProductIdentity(actor(), "a" * 64, "private")
    service = api().InitialPublicationService(
        manifest=m,
        grant_id=UUID(int=21),
        kernel=Kernel(),
        require_current=lambda _: None,
        clock=lambda: NOW,
    )
    receipt = service.prepare(identity)
    assert events == ["preflight", "prepare"]
    assert service.confirm(identity, receipt).changed_count == 2
    assert service.result(identity).result.batch_id == UUID(int=30)
    assert events == ["preflight", "prepare", "confirm", "result"]
    with pytest.raises(SchoolFactChangeRejected):
        service.confirm(identity, receipt.model_copy(update={"plan_sha256": "b" * 64}))


def test_fixed_close_does_not_accept_caller_supplied_scope_and_requires_real_publication():
    from inspect import signature

    fn = api().verify_initial_publication_closed
    assert set(signature(fn).parameters) == {"session", "request", "initialization_id"}

    class Session:
        def execute(self, statement, params):
            assert params["scope_key"] == f"f1:initial-publication:{INIT}"
            return SimpleNamespace(mappings=lambda: SimpleNamespace(one_or_none=lambda: None))

    with pytest.raises(SchoolFactChangeRejected):
        fn(Session(), request=SimpleNamespace(school_id=SCHOOL), initialization_id=INIT)


def test_temporary_grant_creation_has_finite_exact_scope_and_uses_governance_writer():
    m = manifest()
    commands = []

    class Writer:
        def create_duty(self, c):
            commands.append(c)
            return SimpleNamespace(target_id=UUID(int=25), target_version=1)

        def create_scope(self, c):
            commands.append(c)
            return SimpleNamespace(target_id=UUID(int=16), target_version=1)

        def create_grant(self, c):
            commands.append(c)
            return SimpleNamespace(target_id=UUID(int=21), target_version=1)

    receipt = api().configure_initial_publication_grant(Writer(), manifest=m)
    assert receipt.grant_id == UUID(int=21)
    assert commands[1].reference_document == m.scope_document
    assert commands[1].scope_key == m.scope_key
    assert commands[2].assignment_id == ASSIGNMENT
    assert commands[2].planned_valid_to == m.expires_at


def test_terminal_authorizer_only_accepts_precise_grant_end_not_daily_admin():
    from app.platform.governance.contracts import (
        GovernanceActorContext,
        GovernanceWriteAuthorizationRequest,
    )

    m = manifest()
    receipt = api().PublicationGrantReceipt(UUID(int=16), UUID(int=21), 1)
    auth = api().InitialPublicationClosureAuthorizer(manifest=m, grant=receipt)
    a = GovernanceActorContext(
        school_id=SCHOOL,
        actor_kind="platform_bootstrap",
        actor_reference="deployment:campus_ai_migration",
        actor_assignment_id=None,
        actor_assignment_version=None,
        request_id=UUID(int=50),
        reason=m.approval_reference,
    )
    request = GovernanceWriteAuthorizationRequest(
        school_id=SCHOOL,
        command_name="governance.grant.end",
        target_kind="grant",
        target_id=receipt.grant_id,
        actor=a,
    )
    assert auth.authorize(request)
    assert not auth.authorize(replace(request, target_id=UUID(int=99)))
    assert not auth.authorize(replace(request, command_name="governance.grant.create"))
    assert not auth.authorize(replace(request, actor=replace(a, actor_kind="staff")))

    class Repository:
        def session_user(self):
            return "campus_ai_runtime"

    with pytest.raises(Exception, match="migration"):
        auth.authorize_session(Repository(), request)


def close_session(
    *, active=False, duplicate=False, wrong_lineage=False, empty_receipt=False, no_receipt=False
):
    m = manifest()
    scope = dict(
        id=UUID(int=16),
        current_version=1,
        schema_owner=api().PUBLICATION_MODULE,
        schema_name="approved_manifest",
        schema_version=1,
        reference_document=m.scope_document,
        reference_sha256=api().document_sha256(m.scope_document),
    )
    batch = dict(
        id=UUID(int=30),
        status="applied",
        confirmed_by_assignment_id=ASSIGNMENT,
        confirmed_by_user_id=7,
        confirmation_consumed_at=NOW + timedelta(minutes=1),
        planned_units=2,
        succeeded_units=2,
        failed_units=0,
        pending_units=0,
        target_fact_version=3,
        confirmed_at=NOW + timedelta(minutes=1),
        locator=api().publication_locator_sha256(m.locator_sha256, UUID(int=21)),
        result_payload={} if empty_receipt else {"target_version": 3, "changed_count": 2},
    )
    lineage = [
        dict(fact_kind="location", operation="upsert", target_version=1),
        dict(fact_kind="official_contact", operation="upsert", target_version=1),
    ]
    if wrong_lineage:
        lineage[1]["fact_kind"] = "person"
    grant = dict(
        grant_id=UUID(int=21),
        current_version=2,
        duty_id=UUID(int=25),
        duty_version=1,
        scope_version=1,
        valid_from=NOW,
        planned_valid_to=m.expires_at,
        assignment_id=ASSIGNMENT,
        duty_code=api().PUBLICATION_DUTY,
        transition_kind="create" if active else "end",
        formal_ended_at=None if active else NOW,
        revoked_at=None,
        effective_from=NOW,
    )

    class Session:
        def execute(self, statement, params):
            sql = str(statement)
            if "governance_scopes s" in sql:
                return SimpleNamespace(mappings=lambda: SimpleNamespace(one_or_none=lambda: scope))
            if "SELECT id, current_version FROM public.governance_duties" in sql:
                return SimpleNamespace(
                    mappings=lambda: SimpleNamespace(
                        one_or_none=lambda: dict(id=UUID(int=25), current_version=1)
                    )
                )
            if "master_data_import_batches" in sql:
                rows = [] if no_receipt else ([batch, batch] if duplicate else [batch])
            elif "school_fact_change_lineage" in sql:
                rows = lineage
            else:
                rows = [grant]
            return SimpleNamespace(mappings=lambda: SimpleNamespace(all=lambda: rows))

    return Session()


@pytest.mark.parametrize("failure", ["active", "duplicate", "wrong_lineage", "empty_receipt"])
def test_fixed_close_rejects_active_grant_unknown_receipt_and_wrong_publication(failure):
    with pytest.raises(SchoolFactChangeRejected):
        api().verify_initial_publication_closed(
            close_session(**{failure: True}),
            request=SimpleNamespace(school_id=SCHOOL),
            initialization_id=INIT,
        )


def test_fixed_close_accepts_exact_published_scope_lineage_receipt_and_ended_grant():
    assert api().verify_initial_publication_closed(
        close_session(), request=SimpleNamespace(school_id=SCHOOL), initialization_id=INIT
    ) == UUID(int=30)


def test_terminal_authorizer_locks_epoch_before_bootstrap_and_requires_publication_receipt():
    from app.platform.governance.contracts import (
        GovernanceActorContext,
        GovernanceWriteAuthorizationRequest,
    )

    m = manifest()
    g = api().PublicationGrantReceipt(UUID(int=16), UUID(int=21), 1)
    auth = api().InitialPublicationClosureAuthorizer(manifest=m, grant=g)
    a = GovernanceActorContext(
        school_id=SCHOOL,
        actor_kind="platform_bootstrap",
        actor_reference="deployment:campus_ai_migration",
        actor_assignment_id=None,
        actor_assignment_version=None,
        request_id=UUID(int=50),
        reason=m.approval_reference,
    )
    q = GovernanceWriteAuthorizationRequest(
        school_id=SCHOOL,
        command_name="governance.grant.end",
        target_kind="grant",
        target_id=g.grant_id,
        actor=a,
    )
    events = []
    base = close_session()

    class Session:
        def scalar(self, statement, params):
            if "FOR UPDATE" in str(statement):
                events.append("epoch")
                return 3
            return api().PUBLICATION_DUTY

        def execute(self, statement, params):
            events.append("read")
            return base.execute(statement, params)

    class Repository:
        _session = Session()

        def session_user(self):
            return "campus_ai_migration"

        def lock_bootstrap_state(self, school_id):
            events.append("bootstrap")
            return SimpleNamespace(state="open", authority_reference=a.actor_reference)

        def lock_grant(self, school_id, grant_id):
            return SimpleNamespace(assignment_id=ASSIGNMENT, duty_id=UUID(int=25))

        def grant_version(self, school_id, grant_id, version):
            return SimpleNamespace(
                scope_id=g.scope_id, scope_version=1, valid_from=NOW, planned_valid_to=m.expires_at
            )

    repo = Repository()
    auth.authorize_session(repo, q)
    auth.authorize_mutation(repo, q)
    assert events[:2] == ["epoch", "bootstrap"]
    auth.authorize_replay(repo, q)

    class EmptySession(Session):
        def execute(self, statement, params):
            if "master_data_import_batches" in str(statement):
                return SimpleNamespace(mappings=lambda: SimpleNamespace(all=lambda: []))
            return super().execute(statement, params)

    repo._session = EmptySession()
    with pytest.raises(SchoolFactChangeRejected):
        auth.authorize_mutation(repo, q)


def test_end_helper_uses_authoritative_confirmation_time_for_stable_retry(monkeypatch):
    m = manifest()
    g = api().PublicationGrantReceipt(UUID(int=16), UUID(int=21), 1)
    base = close_session()
    commands = []

    class Session:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, statement, params):
            return base.execute(statement, params)

        def scalar(self, statement, params):
            return NOW + timedelta(minutes=1)

    class Writer:
        def __init__(self, **kwargs):
            assert type(kwargs["authorizer"]) is api().InitialPublicationClosureAuthorizer
            assert kwargs["transaction_authorizer"] is kwargs["authorizer"]

        def end_grant(self, command):
            commands.append(command)
            return SimpleNamespace(target_id=g.grant_id, target_version=2)

    monkeypatch.setattr(api(), "GovernanceWriter", Writer)
    for _ in range(2):
        api().end_initial_publication_grant(
            session_factory=Session, manifest=m, grant=g, dependency_port=object()
        )
    assert commands[0] == commands[1]
    assert commands[0].effective_at == NOW + timedelta(minutes=1)
    assert commands[0].expected_version == 1


@pytest.mark.parametrize(
    "sqlstate,expected", [("28000", SchoolFactChangeRejected), ("08006", RuntimeError)]
)
def test_final_window_explicit_denial_differs_from_unavailable(sqlstate, expected):
    from sqlalchemy.exc import DBAPIError

    from app.platform.school_facts.read_policy import SchoolFactAuthorizationUnavailable

    m, guard, context, session, _ = environment()

    class DbFailure(Exception):
        def __init__(self, code):
            super().__init__("fixed boundary unavailable")
            self.sqlstate = code

    error = DbFailure(sqlstate)

    class Session:
        def scalar(self, *args):
            return 3

        def execute(self, *args):
            raise DBAPIError("fixed window", {}, error)

    with pytest.raises(expected) as caught:
        guard(Session(), context, m.change_set)
    assert isinstance(caught.value, SchoolFactAuthorizationUnavailable) == (sqlstate != "28000")


def test_publication_inspection_distinguishes_no_execution_from_damaged_receipt():
    m = manifest()
    state = api().inspect_initial_publication(close_session(no_receipt=True), manifest=m)
    assert state.applied_batch_id is None
    assert state.scope_id == UUID(int=16)
    assert state.grants[0].grant_id == UUID(int=21)
    with pytest.raises(SchoolFactChangeRejected):
        api().inspect_initial_publication(close_session(empty_receipt=True), manifest=m)


def test_expired_initial_window_can_use_new_finite_grant_but_old_guard_cannot():
    later = NOW + timedelta(hours=1)
    m, guard, context, session, _ = environment(
        at=later, current_grant=UUID(int=22), expected_grant=UUID(int=22)
    )
    assert m.expires_at < later
    guard(session, context, m.change_set)
    m, old_guard, context, session, _ = environment(
        at=later, current_grant=UUID(int=22), expected_grant=UUID(int=21)
    )
    with pytest.raises(SchoolFactChangeRejected):
        old_guard(session, context, m.change_set)


def test_renewed_grant_rotates_locator_and_real_writer_rejects_old_unexpired_confirmation():
    from hashlib import sha256

    from app.composition.school_fact_persistence import SqlAlchemySchoolFactPersistence
    from app.composition.school_fact_product_identity import ProductIdentity
    from app.platform.school_facts.contracts import ConfirmationReceiptV1

    m = manifest()
    records = {}
    locators = []
    current = [NOW + timedelta(minutes=9)]

    class Kernel:
        def preflight(self, **kw):
            return SimpleNamespace(is_clean=True, plan_sha256=m.plan_sha256)

        def prepare(self, **kw):
            actor = kw["context"].actor
            credential = ("a" if len(records) == 0 else "b") * 43
            records[credential] = SimpleNamespace(
                school_id=actor.school_id,
                staged_by_user_id=actor.account_user_id,
                staged_by_person_id=actor.person_id,
                staged_by_assignment_id=actor.assignment_id,
                staged_assignment_version=actor.assignment_version,
                staged_authorization_version=actor.authorization_version,
                confirmation_session_sha256=sha256(kw["session_binding"].encode()).hexdigest(),
                plan_sha256=m.plan_sha256,
                source_sha256=m.change_set.source_sha256,
                base_fact_version=m.change_set.base_version,
                confirmation_expires_at=current[0] + timedelta(minutes=5),
            )
            locators.append(kw["provenance"].result_locator_sha256)
            return ConfirmationReceiptV1(
                plan_sha256=m.plan_sha256,
                credential=credential,
                expires_at=records[credential].confirmation_expires_at,
            )

        def confirm(self, **kw):
            SqlAlchemySchoolFactPersistence._validate_confirmation(
                batch=records[kw["credential"]],
                change_set=m.change_set,
                actor=kw["context"].actor,
                session_sha256=sha256(kw["session_binding"].encode()).hexdigest(),
                now=current[0],
            )
            return "validated"

    identity = ProductIdentity(actor(), "a" * 64, "private")
    old = api().InitialPublicationService(
        manifest=m,
        grant_id=UUID(int=21),
        kernel=Kernel(),
        require_current=lambda _: None,
        clock=lambda: current[0],
    )
    previous = old.prepare(identity)
    current[0] = NOW + timedelta(minutes=11)
    new = api().InitialPublicationService(
        manifest=m,
        grant_id=UUID(int=22),
        kernel=Kernel(),
        require_current=lambda _: None,
        clock=lambda: current[0],
    )
    assert current[0] < previous.expires_at
    with pytest.raises(SchoolFactChangeRejected):
        new.confirm(identity, previous)
    fresh = new.prepare(identity)
    assert locators[0] != locators[1]
    assert new.confirm(identity, fresh) == "validated"
