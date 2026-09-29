from __future__ import annotations

import importlib
import importlib.util
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, Literal
from uuid import UUID

import pytest
from sqlalchemy.dialects import postgresql

from app.platform import contracts as actor_contracts
from app.platform.authorization.secure_reader_contracts import SecureReadPage
from app.platform.authorization.thin_contracts import ScopePlan, ThinAuthorizationRequest
from app.platform.contracts import AssignmentId, PersonId, SchoolId

NOW = datetime(2026, 9, 5, 8, tzinfo=UTC)
SCHOOL = UUID(int=11)
CAMPUS = UUID(int=12)


def _module(path: str):
    assert importlib.util.find_spec(path) is not None, f"missing F1 capability: {path}"
    return importlib.import_module(path)


def _actor():
    return actor_contracts._issue_staff_actor_context(
        account_user_id=7,
        school_id=SchoolId(SCHOOL),
        person_id=PersonId(UUID(int=13)),
        assignment_id=AssignmentId(UUID(int=14)),
        assignment_version=1,
        authorization_version=1,
    )


def _request(action: str, **changes):
    values: dict[str, Any] = dict(
        actor=_actor(),
        module_name="school_facts.official_contacts",
        action_code=action,
        scope_id=UUID(int=16),
        resource_type="school_facts.official_contacts",
        resource_id=SCHOOL,
        resource_revision=1,
        request_id=UUID(int=17),
        as_of=NOW,
    )
    values.update(changes)
    return ThinAuthorizationRequest(**values)


def test_formal_policy_owns_two_exact_actions_without_role_or_scope_fallback():
    policy_api = _module("app.platform.school_facts.read_policy")
    policy = policy_api.OfficialContactPolicy()
    reading = policy.requirement_for(_request("school_facts.official_contacts.read"))
    writing = policy.requirement_for(_request("school_facts.official_contacts.maintain"))
    assert reading.duty_code == "official_contact_reader"
    assert writing.duty_code == "official_contact_maintainer"
    assert policy.requirement_for(_request("school_facts.manage")) is None
    assert (
        policy.requirement_for(
            _request("school_facts.official_contacts.read", resource_id=UUID(int=99))
        )
        is None
    )


def test_school_scope_accepts_only_canonical_same_school_document():
    api = _module("app.platform.school_facts.read_policy")
    from app.platform.governance.contracts import ScopeResolutionRequest

    document: dict[str, object] = {"school_id": str(SCHOOL)}
    request = ScopeResolutionRequest(
        school_id=SCHOOL,
        scope_id=UUID(int=16),
        schema_owner="school_facts.school",
        schema_name="single_school",
        schema_version=1,
        reference_document=document,
        reference_sha256=api.school_scope_document_hash(SCHOOL),
        as_of=NOW,
    )
    resolver = api.SingleSchoolScopeResolver()
    result = resolver.resolve(request)
    assert result.reference == f"school:{SCHOOL}" and result.is_effective
    from dataclasses import replace

    assert (
        resolver.resolve(replace(request, reference_document={"school_id": str(UUID(int=99))}))
        is None
    )
    assert (
        resolver.resolve(
            replace(request, reference_document={**document, "include_descendants": True})
        )
        is None
    )
    assert resolver.resolve(replace(request, reference_sha256="0" * 64)) is None


def test_formal_registry_is_separate_from_empty_default_and_test_only_operations():
    api = _module("app.platform.authorization.secure_reader_registry")
    assert hasattr(api, "official_contact_operation_registry"), "formal reader registry is missing"
    registry = api.official_contact_operation_registry()
    assert registry.operation_codes == (
        "school_facts.official_contact_campuses",
        "school_facts.official_contacts",
        "school_facts.official_contact_maintenance_target",
    )
    assert api.production_operation_registry().operation_codes == ()
    with pytest.raises(KeyError):
        registry.require("test_only.r1.official_office_contact")


def test_formal_contact_sql_limits_school_campus_visibility_and_safe_columns_before_fetch():
    api = _module("app.platform.authorization.secure_reader_compiler")
    registry = _module("app.platform.authorization.secure_reader_registry")
    contracts = _module("app.platform.authorization.secure_reader_contracts")
    assert hasattr(contracts, "SchoolFactReadSelection"), (
        "fixed official contact selector is missing"
    )
    relation = api.ServerOwnedProjectionCompiler().compile(
        operation=registry.official_contact_operation_registry().require(
            "school_facts.official_contacts"
        ),
        actor=_actor(),
        scope_plan=ScopePlan(
            scope_id=UUID(int=16),
            owner="school_facts.school",
            reference=f"school:{SCHOOL}",
            version=1,
        ),
        as_of=NOW,
        after_id=None,
        page_size=20,
        selection=contracts.SchoolFactReadSelection(campus_id=CAMPUS),
    )
    sql = str(relation.data_statement.compile(dialect=postgresql.dialect()))
    assert "official_contact_points" in sql and "visibility_scope" in sql
    assert "location_id =" in sql and "school_id =" in sql
    assert "school_fact_change_lineage" in sql
    assert "source_ref" not in sql and "source_key" not in relation.projected_fields
    assert "contact_value" in relation.projected_fields
    assert "valid_from" in relation.projected_fields and "source_kind" in relation.projected_fields
    assert "lineage_id" in relation.projected_fields
    assert not any(
        name in sql for name in ("person_contact_points", "student_contact_relationships", "users")
    )


def test_formal_projection_rejects_test_scope_and_unbound_contact_selection():
    compiler = _module(
        "app.platform.authorization.secure_reader_compiler"
    ).ServerOwnedProjectionCompiler()
    registry = _module("app.platform.authorization.secure_reader_registry")
    assert hasattr(registry, "official_contact_operation_registry"), "formal registry is missing"
    operation = registry.official_contact_operation_registry().require(
        "school_facts.official_contacts"
    )
    with pytest.raises(ValueError):
        compiler.compile(
            operation=operation,
            actor=_actor(),
            scope_plan=ScopePlan(SCHOOL, "test-only.r1.school", f"school:{SCHOOL}", 1),
            as_of=NOW,
            after_id=None,
            page_size=10,
        )
    with pytest.raises(ValueError):
        compiler.compile(
            operation=operation,
            actor=_actor(),
            scope_plan=ScopePlan(SCHOOL, "school_facts.school", f"school:{SCHOOL}", 1),
            as_of=NOW,
            after_id=None,
            page_size=10,
        )


class _Reader:
    def __init__(self, campuses, contacts=()):
        self.campuses, self.contacts, self.calls = campuses, contacts, []

    def read(self, request):
        self.calls.append(request)
        rows = self.campuses if request.operation_code.endswith("_campuses") else self.contacts
        return SimpleNamespace(rows=rows, total_count=len(rows), as_of=NOW, next_cursor=None)


def _campus(name="主校区", row_id=CAMPUS):
    contracts = _module("app.platform.authorization.secure_reader_contracts")
    assert hasattr(contracts, "OfficialContactCampusRow"), "formal campus DTO is missing"
    return contracts.OfficialContactCampusRow(
        row_id=row_id,
        display_name=name,
        display_path=f"/{name}",
        valid_from=NOW,
        valid_to=None,
        version=1,
    )


def test_same_named_campuses_are_ambiguous_before_any_contact_value_read():
    api = _module("app.composition.school_fact_query_repository")
    reader = _Reader((_campus(), _campus(row_id=UUID(int=88))))
    result = api.OfficialContactQueryRepository(reader=reader).read_official_contacts(
        actor=_actor(), campus_name="主校区"
    )
    assert result.status == "ambiguous"
    assert len(result.campuses) == 2 and result.contacts == ()
    assert len(reader.calls) == 1


def test_empty_current_contact_result_does_not_claim_unrecorded_or_official_absence():
    api = _module("app.composition.school_fact_query_repository")
    reader = _Reader((_campus(),))
    result = api.OfficialContactQueryRepository(reader=reader).read_official_contacts(
        actor=_actor(), campus_name="主校区"
    )
    assert result.status == "no_current_records"
    assert result.coverage == "unknown"
    assert result.contacts == ()
    assert len(reader.calls) == 2
    assert reader.calls[1].selection.campus_id == CAMPUS


def test_denied_and_authorization_drift_never_become_empty_school_facts():
    api = _module("app.composition.school_fact_query_repository")
    from app.platform.authorization.secure_reader_contracts import (
        SecureReadAuthorizationDrift,
        SecureReadDenied,
    )

    for error_type in (SecureReadDenied, SecureReadAuthorizationDrift):

        class DenyingReader:
            def read(self, request, error_class=error_type):
                raise error_class("internal authorization diagnostic")

        result = api.OfficialContactQueryRepository(reader=DenyingReader()).read_official_contacts(
            actor=_actor(), campus_name="主校区"
        )
        assert result.status == "forbidden"
        assert result.contacts == () and result.campuses == ()
        assert "internal authorization" not in repr(result)


def test_real_reader_pipeline_revalidates_and_binds_cursor_to_fixed_campus_selection():
    from dataclasses import asdict

    from app.platform.authorization.secure_reader_contracts import (
        SchoolFactReadSelection,
        SecureReadRequest,
    )
    from app.platform.authorization.thin_contracts import ScopePlan
    from tests.test_secure_reader_projection import _Authorizer, _Executor

    class Authorizer(_Authorizer):
        def decide(self, **kwargs):
            result = super().decide(**kwargs)
            result.scope_plan = ScopePlan(
                UUID(int=16), "school_facts.school", f"school:{SCHOOL}", 1
            )
            return result

    api = _module("app.platform.authorization.secure_reader_service")
    executor = _Executor(rows=[asdict(_campus()), asdict(_campus(row_id=UUID(int=19)))])
    auth = Authorizer()
    reader = api.SecureReader(
        registry=_module(
            "app.platform.authorization.secure_reader_registry"
        ).official_contact_operation_registry(),
        compiler=_module(
            "app.platform.authorization.secure_reader_compiler"
        ).ServerOwnedProjectionCompiler(),
        authorizer=auth,
        executor=executor,
        cursor_codec=api.SecureCursorCodec(b"unit-test"),
        clock=lambda: NOW,
    )
    request = SecureReadRequest(
        actor=_actor(),
        operation_code="school_facts.official_contact_campuses",
        selection=SchoolFactReadSelection(campus_name="主校区"),
        page_size=1,
    )
    page = reader.read(request)
    assert page.rows == (_campus(),)
    assert page.next_cursor and auth.events == ["decide", "revalidate", "revalidate"]
    from dataclasses import replace

    with pytest.raises(api.SecureReadDenied, match="bound"):
        reader.read(
            replace(
                request,
                cursor=page.next_cursor,
                selection=SchoolFactReadSelection(campus_name="分校区"),
            )
        )
    assert executor.events.count("data") == 1


def test_source_unavailable_is_not_missing_and_publication_remains_explainable():
    from sqlalchemy.exc import OperationalError

    from app.platform.authorization.secure_reader_contracts import PublishedOfficialContactRow

    api = _module("app.composition.school_fact_query_repository")

    class Unavailable:
        def read(self, request):
            raise OperationalError("SELECT internal", {}, RuntimeError("database offline"))

    unavailable = api.OfficialContactQueryRepository(reader=Unavailable()).read_official_contacts(
        actor=_actor(), campus_name="主校区"
    )
    assert unavailable.status == "source_unavailable"
    contact = PublishedOfficialContactRow(
        row_id=UUID(int=31),
        campus_id=CAMPUS,
        campus_name="主校区",
        contact_kind="phone",
        contact_value="0571-55550001",
        service_hours=None,
        valid_from=NOW,
        valid_to=None,
        version=2,
        source_kind="manual",
        lineage_id=UUID(int=32),
        batch_id=UUID(int=33),
        published_at=NOW,
    )
    result = api.OfficialContactQueryRepository(
        reader=_Reader((_campus(),), (contact,)),
        source_description="合成学校工程资料，经本实例正式维护通道发布",
    ).read_official_contacts(actor=_actor(), campus_name="主校区")
    assert result.status == "known" and result.coverage == "unknown"
    assert result.contacts[0].published_at == NOW
    assert result.contacts[0].publication_ref == f"publication:{UUID(int=33)}"
    assert "合成学校" in result.contacts[0].source_description
    assert "部门" in result.query_scope


def _guard_environment(*, epoch=3, locked_epoch=3, valid=True):
    from app.platform.authorization.thin_service import ThinAuthorizationService
    from app.platform.governance.contracts import (
        GovernanceAuthorizationBasis,
        GovernanceDependencyVersion,
    )

    policy_api = _module("app.platform.school_facts.read_policy")
    actor = _actor()
    events = []

    class Governance:
        def issue(self, query):
            events.append("decide")
            dependencies = (
                GovernanceDependencyVersion("school_facts.assignment", str(actor.assignment_id), 1),
                GovernanceDependencyVersion("school_facts.person", str(actor.person_id), 1),
                GovernanceDependencyVersion(
                    "school_facts.school.scope-target", f"school:{SCHOOL}", 1
                ),
                GovernanceDependencyVersion("governance.duty", str(UUID(int=21)), 1),
                GovernanceDependencyVersion("governance.scope", str(UUID(int=16)), 1),
                GovernanceDependencyVersion("governance.grant", str(UUID(int=22)), 1),
                GovernanceDependencyVersion(
                    "governance.assignment_epoch", str(actor.assignment_id), epoch
                ),
            )
            return GovernanceAuthorizationBasis(
                school_id=SCHOOL,
                assignment_id=UUID(str(actor.assignment_id)),
                duty_code=query.duty_code,
                scope_id=query.scope_id,
                grant_id=UUID(int=22),
                grant_version=1,
                assignment_epoch=epoch,
                issued_as_of=NOW,
                valid_until=None,
                dependency_versions=dependencies,
            )

        def validate(self, basis, *, as_of):
            events.append("revalidate")
            return valid

    class Session:
        def scalar(self, statement, params):
            events.append("lock")
            assert "app.lock_school_fact_governance_epoch" in str(statement)
            assert params == {"school_id": SCHOOL, "assignment_id": actor.assignment_id}
            return locked_epoch

    thin = ThinAuthorizationService(
        governance_basis_service=Governance(),
        module_policies={policy_api.OFFICIAL_CONTACT_MODULE: policy_api.OfficialContactPolicy()},
    )
    return thin, Session(), events


def _guard_change(kind: Literal["official_contact", "person"] = "official_contact"):
    from app.platform.school_facts.contracts import (
        CanonicalFactChangeSetV1,
        CanonicalFactChangeV1,
        FactOriginV1,
    )

    return CanonicalFactChangeSetV1(
        source_sha256="a" * 64,
        base_version=1,
        changes=(
            CanonicalFactChangeV1(
                operation="revoke",
                fact_kind=kind,
                source_key="test.contact",
                expected_version=1,
                effective_at=NOW,
                origin=FactOriginV1(sheet_code="contacts", row_number=1),
                content=None,
            ),
        ),
    )


@pytest.mark.parametrize(
    ("epoch", "locked_epoch", "valid"), ((3, 3, False), (3, 4, True), (3, None, True))
)
def test_final_guard_rejects_revocation_drift_and_missing_epoch(epoch, locked_epoch, valid):
    api = _module("app.composition.school_fact_query_repository")
    assert hasattr(api, "OfficialContactFinalAuthorizationGuard"), "final writer guard is missing"
    from app.platform.school_facts.application import SchoolFactChangeRejected

    thin, session, _ = _guard_environment(epoch=epoch, locked_epoch=locked_epoch, valid=valid)
    guard = api.OfficialContactFinalAuthorizationGuard(
        thin_service=thin, scope_id=UUID(int=16), clock=lambda: NOW
    )
    context = actor_contracts._issue_command_execution_context(
        actor=_actor(), request_id=UUID(int=27), idempotency_key="unit-test"
    )
    with pytest.raises(SchoolFactChangeRejected):
        guard(session, context, _guard_change())


def test_final_guard_locks_epoch_before_thin_and_only_accepts_official_contact_changes():
    api = _module("app.composition.school_fact_query_repository")
    assert hasattr(api, "OfficialContactFinalAuthorizationGuard"), "final writer guard is missing"
    from app.platform.school_facts.application import SchoolFactChangeRejected

    thin, session, events = _guard_environment()
    guard = api.OfficialContactFinalAuthorizationGuard(
        thin_service=thin, scope_id=UUID(int=16), clock=lambda: NOW
    )
    context = actor_contracts._issue_command_execution_context(
        actor=_actor(), request_id=UUID(int=27), idempotency_key="unit-test"
    )
    guard(session, context, _guard_change())
    assert events == ["lock", "decide", "revalidate"]
    events.clear()
    with pytest.raises(SchoolFactChangeRejected):
        guard(session, context, _guard_change("person"))
    assert events == []


def test_maintenance_target_is_a_separate_maintainer_projection_not_a_model_fact():
    api = _module("app.platform.authorization.secure_reader_registry")
    operation = api.official_contact_operation_registry().require(
        "school_facts.official_contact_maintenance_target"
    )
    assert operation.action_code == "school_facts.official_contacts.maintain"
    policy = _module("app.platform.school_facts.read_policy").OfficialContactPolicy()
    assert (
        policy.requirement_for(
            _request(operation.action_code, resource_type=operation.code)
        ).duty_code
        == "official_contact_maintainer"
    )
    assert (
        policy.requirement_for(
            _request("school_facts.official_contacts.read", resource_type=operation.code)
        )
        is None
    )
    contracts = _module("app.platform.authorization.secure_reader_contracts")
    relation = (
        _module("app.platform.authorization.secure_reader_compiler")
        .ServerOwnedProjectionCompiler()
        .compile(
            operation=operation,
            actor=_actor(),
            scope_plan=ScopePlan(UUID(int=16), "school_facts.school", f"school:{SCHOOL}", 1),
            as_of=NOW,
            after_id=None,
            page_size=1,
            selection=contracts.SchoolFactReadSelection(lineage_id=UUID(int=32)),
        )
    )
    assert relation.projected_fields[:5] == (
        "row_id",
        "source_key",
        "expected_version",
        "campus_id",
        "campus_source_key",
    )
    sql = str(relation.data_statement.compile(dialect=postgresql.dialect()))
    assert "school_fact_change_lineage.id =" in sql and "school_id =" in sql
    assert "source_ref" not in sql and "source_key" in relation.projected_fields


def test_maintenance_resolver_refuses_stale_missing_or_wrong_shaped_targets():
    api = _module("app.composition.school_fact_query_repository")
    assert hasattr(api.OfficialContactQueryRepository, "resolve_maintenance_target"), (
        "missing narrow maintainer resolver"
    )
    from app.platform.school_facts.application import SchoolFactChangeRejected

    reader = _Reader(())
    repo = api.OfficialContactQueryRepository(reader=reader)
    with pytest.raises(SchoolFactChangeRejected):
        repo.resolve_maintenance_target(actor=_actor(), fact_ref=f"official-contact:{UUID(int=32)}")
    assert reader.calls[0].operation_code == "school_facts.official_contact_maintenance_target"
    assert reader.calls[0].selection.lineage_id == UUID(int=32)
    with pytest.raises(ValueError):
        repo.resolve_maintenance_target(actor=_actor(), fact_ref="arbitrary-source-key")


def test_authorization_authority_unavailable_never_becomes_forbidden():
    policy_api = _module("app.platform.school_facts.read_policy")
    assert hasattr(policy_api, "SchoolFactAuthorizationUnavailable"), (
        "missing typed authority unavailability"
    )
    api = _module("app.composition.school_fact_query_repository")

    class UnavailableReader:
        def read(self, request):
            raise policy_api.SchoolFactAuthorizationUnavailable("private authority diagnostic")

    result = api.OfficialContactQueryRepository(reader=UnavailableReader()).read_official_contacts(
        actor=_actor()
    )
    assert result.status == "source_unavailable" and "diagnostic" not in repr(result)

    class UnavailableThin:
        def decide(self, request):
            raise policy_api.SchoolFactAuthorizationUnavailable("private authority diagnostic")

    guard = api.OfficialContactFinalAuthorizationGuard(
        thin_service=UnavailableThin(), scope_id=UUID(int=16)
    )
    with pytest.raises(policy_api.SchoolFactAuthorizationUnavailable):
        guard.require_current(_actor())


def test_final_guard_database_fault_stays_unavailable_and_never_authorizes():
    policy_api = _module("app.platform.school_facts.read_policy")
    assert hasattr(policy_api, "SchoolFactAuthorizationUnavailable"), (
        "missing typed authority unavailability"
    )
    api = _module("app.composition.school_fact_query_repository")
    from sqlalchemy.exc import OperationalError

    thin, _, events = _guard_environment()

    class OfflineSession:
        def scalar(self, statement, params):
            raise OperationalError("private SQL", {}, RuntimeError("offline"))

    guard = api.OfficialContactFinalAuthorizationGuard(thin_service=thin, scope_id=UUID(int=16))
    context = actor_contracts._issue_command_execution_context(
        actor=_actor(), request_id=UUID(int=27), idempotency_key="unit-test"
    )
    with pytest.raises(policy_api.SchoolFactAuthorizationUnavailable):
        guard(OfflineSession(), context, _guard_change())
    assert events == []


@pytest.mark.parametrize(
    ("failure", "status", "fetched"),
    (
        ("no_grant", "forbidden", False),
        ("database_issue", "source_unavailable", False),
        ("database_before", "source_unavailable", False),
        ("database_after", "source_unavailable", True),
        ("revoked_after", "forbidden", True),
    ),
)
def test_real_secure_reader_pipeline_distinguishes_authority_failure_and_drops_rows(
    failure, status, fetched
):
    from dataclasses import asdict

    from sqlalchemy.exc import OperationalError

    from app.composition.school_fact_product_services import ProductThinAuthorizationService
    from app.platform.authorization.secure_reader_adapters import ThinAuthorizationReadAdapter
    from app.platform.authorization.secure_reader_service import SecureCursorCodec, SecureReader
    from app.platform.governance.reader import GovernanceAuthorityUnavailable
    from tests.test_secure_reader_projection import _Executor

    base, _, _ = _guard_environment()
    original_basis = base._governance_basis_service

    class Basis:
        calls = 0

        def issue(self, query):
            if failure == "no_grant":
                raise GovernanceAuthorityUnavailable("grant_inactive", "no grant")
            if failure == "database_issue":
                raise OperationalError("internal authority", {}, RuntimeError("offline"))
            return original_basis.issue(query)

        def validate(self, basis, *, as_of):
            self.calls += 1
            if (failure == "database_before" and self.calls == 1) or (
                failure == "database_after" and self.calls == 2
            ):
                raise OperationalError("internal authority", {}, RuntimeError("offline"))
            return not (failure == "revoked_after" and self.calls == 2)

    executor = _Executor(rows=[asdict(_campus())])
    reader = SecureReader(
        registry=_module(
            "app.platform.authorization.secure_reader_registry"
        ).official_contact_operation_registry(),
        compiler=_module(
            "app.platform.authorization.secure_reader_compiler"
        ).ServerOwnedProjectionCompiler(),
        authorizer=ThinAuthorizationReadAdapter(
            thin_service=ProductThinAuthorizationService(governance_basis_service=Basis()),
            scope_id_resolver=lambda _operation, _actor: UUID(int=16),
        ),
        executor=executor,
        cursor_codec=SecureCursorCodec(b"unit-test"),
        clock=lambda: NOW,
    )
    result = (
        _module("app.composition.school_fact_query_repository")
        .OfficialContactQueryRepository(reader=reader)
        .read_official_contacts(actor=_actor(), campus_name="主校区")
    )
    assert result.status == status and result.campuses == () and result.contacts == ()
    assert ("data" in executor.events) == fetched


def test_product_thin_failure_recorder_is_independent_for_overlapping_requests():
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from sqlalchemy.exc import OperationalError

    from app.composition.school_fact_product_services import ProductThinAuthorizationService
    from app.platform.authorization.thin_contracts import ThinDecisionKind
    from app.platform.school_facts.read_policy import SchoolFactAuthorizationUnavailable

    base, _, _ = _guard_environment()
    original_basis = base._governance_basis_service
    first_entered, second_done = Event(), Event()

    class Basis:
        def issue(self, query):
            if query.scope_id == UUID(int=99):
                first_entered.set()
                assert second_done.wait(3)
                raise OperationalError("internal", {}, RuntimeError("offline"))
            assert first_entered.wait(3)
            result = original_basis.issue(query)
            second_done.set()
            return result

        def validate(self, basis, *, as_of):
            return True

    thin = ProductThinAuthorizationService(governance_basis_service=Basis())
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(
            thin.decide, _request("school_facts.official_contacts.read", scope_id=UUID(int=99))
        )
        second = pool.submit(thin.decide, _request("school_facts.official_contacts.read"))
        assert second.result(timeout=5).kind == ThinDecisionKind.ALLOW
        with pytest.raises(SchoolFactAuthorizationUnavailable):
            first.result(timeout=5)


def test_f1_governance_reader_uses_fixed_identity_wrappers_and_default_reader_is_unchanged():
    from contextlib import nullcontext
    from typing import cast

    from sqlalchemy.orm import Session as DatabaseSession
    from sqlalchemy.orm import sessionmaker

    from app.composition.governance_dependencies import SqlAlchemyGovernanceDependencyAdapter
    from app.composition.school_fact_product_services import ReadOnlyGovernanceDependencies

    calls = []

    class Session:
        def execute(self, statement, *args):
            calls.append(str(statement.compile(dialect=postgresql.dialect())))
            return SimpleNamespace(one_or_none=lambda: None)

    sessions = cast(
        sessionmaker[DatabaseSession], SimpleNamespace(begin=lambda: nullcontext(Session()))
    )
    dependencies = ReadOnlyGovernanceDependencies(sessions)
    assert (
        dependencies.read_assignment(school_id=SCHOOL, assignment_id=UUID(int=14), as_of=NOW)
        is None
    )
    assert "app.school_fact_reader_assignment_terminal(" in calls[-1]
    assert "app.school_fact_reader_identity_root_is_effective_at(" in calls[-1]
    assert "app.school_fact_content_version_terminal(" not in calls[-1]
    assert "app.school_fact_root_is_effective_at(" not in calls[-1]
    for _kind, method, arguments in (
        ("person", dependencies.read_person_root, {"person_id": UUID(int=13)}),
        ("organization", dependencies.read_organization_root, {"organization_id": UUID(int=15)}),
    ):
        assert method(school_id=SCHOOL, as_of=NOW, **arguments) is None
        assert "app.school_fact_reader_identity_root_is_effective_at(" in calls[-1]
        assert "app.school_fact_root_is_effective_at(" not in calls[-1]
        assert "FOR UPDATE" not in calls[-1]
    original = SqlAlchemyGovernanceDependencyAdapter(sessions)
    assert original.read_assignment(school_id=SCHOOL, assignment_id=UUID(int=14), as_of=NOW) is None
    assert "app.school_fact_content_version_terminal(" in calls[-1]
    assert "app.school_fact_root_is_effective_at(" in calls[-1]
    assert "app.school_fact_reader_" not in calls[-1]


def test_explicit_unmatched_campus_is_not_an_empty_contact_collection():
    api = _module("app.composition.school_fact_query_repository")
    reader = _Reader(())
    result = api.OfficialContactQueryRepository(reader=reader).read_official_contacts(
        actor=_actor(), campus_name="未匹配校区"
    )
    assert result.status == "unmatched"
    assert len(reader.calls) == 1


def _published_contact(campus, number):
    contracts = _module("app.platform.authorization.secure_reader_contracts")
    return contracts.PublishedOfficialContactRow(
        row_id=UUID(int=number),
        campus_id=campus.row_id,
        campus_name=campus.display_name,
        contact_kind="email",
        contact_value=f"contact{number}@example.invalid",
        service_hours=None,
        valid_from=NOW,
        valid_to=None,
        version=1,
        source_kind="admin",
        lineage_id=UUID(int=10000 + number),
        batch_id=UUID(int=20000 + number),
        published_at=NOW,
    )


class _SelectedPages:
    def __init__(self, campuses, contacts=None):
        self.campuses = campuses
        self.contacts = contacts or {}
        self.calls = []

    def read(self, request) -> SecureReadPage:
        self.calls.append(request)
        selection = request.selection
        if request.operation_code.endswith("_campuses"):
            rows = tuple(
                row
                for row in self.campuses
                if (selection.campus_id is None or row.row_id == selection.campus_id)
                and (selection.campus_name is None or row.display_name == selection.campus_name)
            )
        else:
            rows = self.contacts.get(selection.campus_id, ())
        return SecureReadPage(
            rows=rows[: request.page_size],
            total_count=len(rows),
            as_of=NOW,
            next_cursor="bounded-more" if len(rows) > request.page_size else None,
        )


def test_listing_campuses_is_distinct_from_resolving_one_contact_owner():
    api = _module("app.composition.school_fact_query_repository")
    view = _module("app.composition.school_fact_product_contacts").contact_query_view
    reader = _SelectedPages((_campus(), _campus("另一校区", UUID(int=88))))
    result = api.OfficialContactQueryRepository(reader=reader).read_official_contacts(
        actor=_actor(), subject="campuses", scope="all_authorized"
    )
    public = view(result).model_dump(mode="json")
    assert public["status"] == "known" and public["items"] == []
    assert len(public["resolved_campuses"]) == 2 and public["candidates"] == []
    assert public["query"] == {
        "subject": "campuses",
        "scope": "all_authorized",
        "campus_name": None,
        "campus_ref": None,
    }
    assert public["listing"] == {
        "entity": "campuses",
        "returned_count": 2,
        "matched_count": 2,
        "truncated": False,
    }
    assert public["coverage"]["status"] == "unknown" and len(reader.calls) == 1


@pytest.mark.parametrize("size", [0, 1, 100, 101])
def test_selected_contact_counts_and_truncation_do_not_claim_source_coverage(size):
    api = _module("app.composition.school_fact_query_repository")
    view = _module("app.composition.school_fact_product_contacts").contact_query_view
    campus = _campus()
    reader = _SelectedPages(
        (campus,),
        {campus.row_id: tuple(_published_contact(campus, number + 1) for number in range(size))},
    )
    result = api.OfficialContactQueryRepository(reader=reader).read_official_contacts(
        actor=_actor(), campus_ref=f"campus:{campus.row_id}"
    )
    public = view(result).model_dump(mode="json")
    assert public["status"] == ("known" if size else "no_current_records")
    assert public["resolved_campuses"][0]["reference"] == f"campus:{campus.row_id}"
    assert public["listing"] == {
        "entity": "contacts",
        "returned_count": min(size, 100),
        "matched_count": size,
        "truncated": size > 100,
    }
    assert public["coverage"]["status"] == "unknown"


def test_all_authorized_contacts_aggregate_only_resolved_campuses_and_remain_bounded():
    api = _module("app.composition.school_fact_query_repository")
    view = _module("app.composition.school_fact_product_contacts").contact_query_view
    campuses = (_campus(), _campus("另一校区", UUID(int=88)))
    reader = _SelectedPages(
        campuses,
        {
            campus.row_id: tuple(_published_contact(campus, index * 100 + n + 1) for n in range(60))
            for index, campus in enumerate(campuses)
        },
    )
    result = api.OfficialContactQueryRepository(reader=reader).read_official_contacts(
        actor=_actor(), scope="all_authorized"
    )
    public = view(result).model_dump(mode="json")
    assert public["status"] == "known" and len(public["items"]) == 100
    assert public["listing"]["matched_count"] == 120
    assert public["listing"]["truncated"] is True
    assert [call.selection.campus_id for call in reader.calls[1:]] == [
        row.row_id for row in campuses
    ]


def test_incomplete_campus_enumeration_does_not_invent_contact_total():
    api = _module("app.composition.school_fact_query_repository")
    view = _module("app.composition.school_fact_product_contacts").contact_query_view
    reader = _SelectedPages(tuple(_campus(f"校区{n}", UUID(int=n + 1)) for n in range(101)))
    public = view(
        api.OfficialContactQueryRepository(reader=reader).read_official_contacts(
            actor=_actor(), scope="all_authorized"
        )
    ).model_dump(mode="json")
    assert public["listing"]["matched_count"] is None
    assert public["listing"]["truncated"] is True
    assert len(public["resolved_campuses"]) == 100 and len(reader.calls) == 101


def test_query_contract_requires_explicit_unambiguous_scope():
    gateway = _module("app.composition.school_fact_assistant_gateway")
    query = gateway.ContactQueryInput(subject="campuses", scope="all_authorized")
    assert query.subject == "campuses"
    with pytest.raises(ValueError):
        gateway.ContactQueryInput(scope="all_authorized", campus_name="一个校区")
    with pytest.raises(ValueError):
        gateway.ContactQueryInput(campus_name="一个校区", campus_ref=f"campus:{CAMPUS}")


def test_campus_listing_reports_authoritative_count_even_when_display_is_truncated():
    api = _module("app.composition.school_fact_query_repository")
    view = _module("app.composition.school_fact_product_contacts").contact_query_view
    reader = _SelectedPages(tuple(_campus(f"校区{n}", UUID(int=n + 1)) for n in range(101)))
    result = view(
        api.OfficialContactQueryRepository(reader=reader).read_official_contacts(
            actor=_actor(), subject="campuses", scope="all_authorized"
        )
    )
    assert result.listing.entity == "campuses"
    assert result.listing.returned_count == 100 and result.listing.matched_count == 101
    assert result.listing.truncated is True
    assert result.coverage.status == "unknown" and len(reader.calls) == 1


def test_selected_reference_resolves_same_named_campus_without_scope_widening():
    api = _module("app.composition.school_fact_query_repository")
    view = _module("app.composition.school_fact_product_contacts").contact_query_view
    first, second = _campus(), _campus(row_id=UUID(int=88))
    reader = _SelectedPages((first, second), {second.row_id: (_published_contact(second, 5),)})
    repo = api.OfficialContactQueryRepository(reader=reader)
    choices = view(repo.read_official_contacts(actor=_actor(), campus_name=first.display_name))
    assert choices.status == "ambiguous"
    assert choices.resolved_campuses == () and len(choices.candidates) == 2
    resolved = view(
        repo.read_official_contacts(actor=_actor(), campus_ref=choices.candidates[1].reference)
    )
    assert resolved.status == "known"
    assert [campus.reference for campus in resolved.resolved_campuses] == [
        choices.candidates[1].reference
    ]
    assert reader.calls[-1].selection.campus_id == second.row_id


@pytest.mark.parametrize("failure", ["forbidden", "source_unavailable"])
def test_all_authorized_query_discards_prior_rows_if_later_read_fails(failure):
    from app.platform.authorization.secure_reader_contracts import SecureReadDenied
    from app.platform.school_facts.read_policy import SchoolFactAuthorizationUnavailable

    api = _module("app.composition.school_fact_query_repository")
    view = _module("app.composition.school_fact_product_contacts").contact_query_view
    first, second = _campus(), _campus("另一校区", UUID(int=88))

    class DriftingReader(_SelectedPages):
        def read(self, request):
            if request.selection.campus_id == second.row_id:
                error = (
                    SecureReadDenied
                    if failure == "forbidden"
                    else SchoolFactAuthorizationUnavailable
                )
                raise error("private authority detail")
            return super().read(request)

    reader = DriftingReader((first, second), {first.row_id: (_published_contact(first, 5),)})
    result = view(
        api.OfficialContactQueryRepository(reader=reader).read_official_contacts(
            actor=_actor(), scope="all_authorized"
        )
    )
    assert result.status == ("denied" if failure == "forbidden" else failure)
    assert result.items == result.candidates == result.resolved_campuses == ()
    assert result.listing.matched_count is None
    assert "private authority" not in result.model_dump_json()


@pytest.mark.parametrize("size", [0, 1, 2])
def test_absent_selector_never_silently_selects_a_campus_even_when_unique(size):
    api = _module("app.composition.school_fact_query_repository")
    view = _module("app.composition.school_fact_product_contacts").contact_query_view
    reader = _SelectedPages(tuple(_campus(f"校区{n}", UUID(int=n + 1)) for n in range(size)))
    result = view(
        api.OfficialContactQueryRepository(reader=reader).read_official_contacts(actor=_actor())
    )
    assert result.status == "selection_required"
    assert len(result.candidates) == size and result.resolved_campuses == ()
    assert result.items == () and result.listing.entity == "campuses"
    assert len(reader.calls) == 1


def test_empty_all_authorized_listing_is_only_no_current_records():
    api = _module("app.composition.school_fact_query_repository")
    view = _module("app.composition.school_fact_product_contacts").contact_query_view
    result = view(
        api.OfficialContactQueryRepository(reader=_SelectedPages(())).read_official_contacts(
            actor=_actor(), subject="campuses", scope="all_authorized"
        )
    )
    assert result.status == "no_current_records"
    assert result.listing.matched_count == 0
