from importlib.util import find_spec
from types import SimpleNamespace
from uuid import UUID

import pytest

from app.platform.school_facts.f4_policy import F4_FACT_KINDS, F4FactScope

_CANCELLATION_REJECTIONS = (
    "prior arrangement independently changed or lacks interval proof; complete correction required",
    "selected arrangement independently changed; review required",
    "invalid teaching cancellation cause",
    "selected slice has no original arrangement proof",
)


def cancellation_projection_failure(message, *, state="22023", selected=True, stage="fetch"):
    from contextlib import contextmanager
    from datetime import UTC, datetime

    from sqlalchemy.exc import DBAPIError

    from app.composition.school_fact_education_reader import SqlEducationProjection

    class DriverError(Exception):
        sqlstate = state
        diag = SimpleNamespace(message_primary=message)

    failure = DBAPIError("SELECT private_sql", {"key": "private-parameter"}, DriverError(message))

    class Executor:
        @contextmanager
        def validated_read(self, before, after):
            before()
            yield self
            after()

        def fetch(self, relation):
            if stage == "fetch":
                raise failure
            return [{"payload": {"records": [], "school_fact_version": 1}}]

    def boundary(where):
        if stage == where:
            raise failure

    return SqlEducationProjection(Executor(), current_sessions=lambda: None).read(
        school_id=UUID(int=1), fact_kind="teaching_course",
        as_of=datetime(2027, 1, 1, tzinfo=UTC), known_at=None, filter={}, metadata_only=False,
        source_keys=lambda: ("course:chosen",), field_names=lambda: ("display_name",),
        before_read=lambda: boundary("before"), after_read=lambda _: boundary("after"),
        cancellation_start=datetime(2027, 9, 1, tzinfo=UTC) if selected else None,
        expected_version=3,
    )


@pytest.mark.parametrize("message", _CANCELLATION_REJECTIONS)
def test_known_cancellation_proof_rejection_has_controlled_correction_reason(message):
    with pytest.raises(Exception) as caught:
        cancellation_projection_failure(message)
    assert type(caught.value).__name__ == "EducationCancellationRequiresCorrection"
    assert "完整调整" in str(caught.value)
    assert "private_sql" not in str(caught.value)
    assert message not in str(caught.value)


@pytest.mark.parametrize(
    "options",
    [
        {"state": "08006"}, {"state": "42501"}, {"state": "28000"},
        {"selected": False}, {"stage": "before"}, {"stage": "after"},
    ],
)
def test_cancellation_error_mapping_never_absorbs_other_database_or_authority_failures(options):
    from sqlalchemy.exc import DBAPIError

    with pytest.raises(DBAPIError):
        cancellation_projection_failure(_CANCELLATION_REJECTIONS[0], **options)


def test_unknown_22023_is_not_presented_as_a_proven_cancellation_conflict():
    from sqlalchemy.exc import DBAPIError

    with pytest.raises(DBAPIError):
        cancellation_projection_failure("invalid exact teaching cancellation coordinates")


def cancellation_prepare_input():
    return dict(
        fact_kind="teaching_course", target_ref="chosen-course", operation="cancel", fields={},
        cancel_from="2027-09-01T00:00:00Z", reason="取消选定未来安排", purpose="cancel_future",
    )


@pytest.mark.parametrize("entry", ["prepare", "preview"])
@pytest.mark.parametrize("known", [False, True])
def test_http_cancellation_prepare_and_preview_show_known_correction_only(entry, known):
    from uuid import uuid4

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.interfaces.http.errors import ApiError, render_api_error
    from app.interfaces.http.framework_request import (
        FrameworkRequestContext,
        FrameworkRequestPresence,
        get_framework_request_context,
        get_framework_request_presence,
    )
    from app.interfaces.http.school_fact_education import create_education_product_router
    from tests.test_f2_directory_product import actor, setup

    def read_proof(**_):
        return cancellation_projection_failure(
            _CANCELLATION_REJECTIONS[0], state="22023" if known else "08006"
        )

    app = FastAPI()
    context = FrameworkRequestContext(uuid4())
    product, boundary, _, original = setup()
    if entry == "preview":
        prepared = product.prepare(
            actor=actor(), session_binding="session", payload=original,
            request_id=context.request_id,
        )
        product._confirmations[prepared.proposal_ref].summary_revalidate = (
            lambda current: read_proof()
        )
        boundary.calls.clear()
    else:
        product._resolve = read_proof
    app.dependency_overrides[get_framework_request_context] = lambda: context
    app.dependency_overrides[get_framework_request_presence] = lambda: FrameworkRequestPresence(
        context, False, False
    )
    app.add_exception_handler(
        ApiError, lambda request, exc: render_api_error(context.request_id, exc)
    )
    app.include_router(create_education_product_router(
        identity_dependency=lambda: SimpleNamespace(actor=actor(), session_binding="session"),
        query=SimpleNamespace(), education_query=SimpleNamespace(), teaching_enabled=True,
        product=product,
    ))
    with TestClient(app) as client:
        response = (
            client.post("/api/v1/school-fact-product/education-facts/prepare",
                        json=cancellation_prepare_input())
            if entry == "prepare" else
            client.get(
                "/api/v1/school-fact-product/education-facts/proposals/" + prepared.proposal_ref
            )
        )
    assert response.status_code == (409 if known else 503)
    assert ("完整调整" in response.json()["message"]) is known
    if known:
        assert response.json()["code"] == "school_facts.teaching_cancellation_requires_correction"
        assert "未发起新的修改" in response.json()["message"]
        assert "已提交的操作仍请查原回执" in response.json()["message"]
    assert "private_sql" not in response.text and "private-parameter" not in response.text
    assert _CANCELLATION_REJECTIONS[0] not in response.text
    assert boundary.calls == []


@pytest.mark.parametrize("known", [False, True])
def test_model_prepare_uses_inherited_actual_path_and_explains_cancellation_rejection(known):
    from app.composition.school_fact_education_gateway import EducationToolGateway
    from app.platform.assistant.tool_loop import ModelToolCall
    from tests.school_facts.test_f4_education_gateway import fixture
    from tests.test_f2_directory_product import setup

    _, previous, context, _ = fixture()
    called = []

    def prepare(**kwargs):
        called.append(kwargs)
        return cancellation_projection_failure(
            _CANCELLATION_REJECTIONS[1], state="22023" if known else "08006"
        )

    product, boundary, _, _ = setup()
    product._resolve = prepare
    gateway = EducationToolGateway(
        resolve_actor=previous.resolve_actor, query=previous.education_query,
        product=product, describe_adapter=SimpleNamespace(),
        teaching_enabled=True,
    )
    result = gateway.for_turn("actor").execute(ModelToolCall(
        call_id="prepare-cancel", name="school_facts.education.prepare",
        arguments=cancellation_prepare_input(),
    ), context)
    assert len(called) == 1 and called[0]["payload"].operation == "cancel"
    assert result.model_content["status"] == ("rejected" if known else "source_unavailable")
    if known:
        assert result.model_content["reason_code"] == (
            "school_facts.teaching_cancellation_requires_correction"
        )
        assert "完整调整" in result.model_content["message"]
        assert "未发起新的修改" in result.model_content["message"]
        assert "已提交的操作仍请查原回执" in result.model_content["message"]
    assert result.module_result is None and result.fact_card is None
    assert gateway.recent_action(context, previous.identity("actor")) is None
    assert not gateway._actions and gateway._preparing_actions == 0
    assert "private_sql" not in str(result.model_content)
    assert boundary.calls == []

KINDS = (
    "teaching_subject",
    "teaching_course",
    "course_subject",
    "teaching_offering",
    "offering_member_source",
    "teaching_group",
    "teaching_group_member",
    "teaching_assignment",
    "teaching_organization_scope",
)


def test_f5_policy_exists_and_f4_remains_frozen():
    assert find_spec("app.platform.school_facts.f5_policy") is not None
    assert len(F4_FACT_KINDS) == 17
    for kind in KINDS:
        with pytest.raises(ValueError):
            F4FactScope(UUID(int=1), kind, ("x",), (), ("display_name",))


def test_f5_configuration_has_separate_operation():
    from app.composition.school_fact_education_settings import EducationScopeBinding

    binding = EducationScopeBinding(
        assignment_id=UUID(int=1),
        scope_id=UUID(int=2),
        operation_code="school_facts.f5.teaching_subject.read",
    )
    assert binding.operation_code.startswith("school_facts.f5.")


def test_current_authority_selects_f5_owner_and_rejects_f4_scope():
    from datetime import UTC, datetime

    from app.composition.school_fact_education_authority import (
        EducationAuthority,
        EducationAuthorityDenied,
    )
    from app.composition.school_fact_education_settings import EducationConfiguration
    from app.platform import contracts as ctx
    from app.platform.authorization.thin_contracts import ThinDecisionKind

    actor = ctx._issue_staff_actor_context(
        account_user_id=1,
        school_id=ctx.SchoolId(UUID(int=1)),
        person_id=ctx.PersonId(UUID(int=2)),
        assignment_id=ctx.AssignmentId(UUID(int=3)),
        assignment_version=1,
        authorization_version=2,
    )
    calls = []

    class Thin:
        def decide(self, request):
            calls.append(request)
            return SimpleNamespace(
                kind=ThinDecisionKind.ALLOW, scope_plan=SimpleNamespace(owner="school_facts.f4")
            )

    config = EducationConfiguration(
        approval_reference="test",
        scope_bindings=(
            {
                "assignment_id": actor.assignment_id,
                "scope_id": UUID(int=4),
                "operation_code": "school_facts.f5.teaching_subject.read",
            },
        ),
    )
    with pytest.raises(EducationAuthorityDenied):
        EducationAuthority(thin=Thin(), configuration=config).require_scope(
            actor, fact_kind="teaching_subject", action="read", checked_at=datetime.now(UTC)
        )
    assert len(calls) == 1
    assert calls[0].module_name == "school_facts.f5"


def test_f5_reader_uses_teaching_fields_and_bounded_scalar_filters():
    from datetime import UTC, datetime

    from app.composition.school_fact_education_reader import EducationRepository
    from app.platform import contracts as ctx
    from app.platform.school_facts.f5_policy import F5FactScope

    actor = ctx._issue_staff_actor_context(
        account_user_id=1,
        school_id=ctx.SchoolId(UUID(int=1)),
        person_id=ctx.PersonId(UUID(int=2)),
        assignment_id=ctx.AssignmentId(UUID(int=3)),
        assignment_version=1,
        authorization_version=2,
    )
    calls = []

    class Authority:
        def require_scope(self, actor, **kwargs):
            return SimpleNamespace(
                scope=F5FactScope(
                    actor.school_id,
                    "course_subject",
                    ("link:one",),
                    (),
                    ("course_source_key", "subject_source_key"),
                )
            )

        def require_fields(self, permit, fields):
            if not set(fields) <= set(permit.scope.field_names):
                raise PermissionError()

        def require_object(self, permit, key, **kwargs):
            if key not in permit.scope.object_keys:
                raise PermissionError()

        def revalidate(self, *args, **kwargs):
            pass

    class Projection:
        def current_version(self, *args):
            return 1

        def read(self, **kwargs):
            kwargs["before_read"]()
            calls.append(kwargs)
            raw = {"records": [], "school_fact_version": 1}
            kwargs["after_read"](raw)
            return raw

    repo = EducationRepository(
        projection=Projection(),
        authority=Authority(),
        reference_key=b"x" * 32,
        clock=lambda: datetime.now(UTC),
    )
    assert (
        repo.read_records(
            actor, "course_subject", filter={"course_source_key": "course:one"}
        ).records
        == ()
    )
    assert len(calls) == 1
    for value in ({"path": "arbitrary"}, ["course:one"], "", "*"):
        with pytest.raises(ValueError):
            repo.read_records(actor, "course_subject", filter={"course_source_key": value})
    assert len(calls) == 1


def test_f5_policy_and_scope_are_distinct_for_every_kind():
    from datetime import UTC, datetime

    from app.platform import contracts as ctx
    from app.platform.authorization.thin_contracts import ThinAuthorizationRequest
    from app.platform.governance.contracts import ScopeResolutionRequest
    from app.platform.school_facts import f4_policy as f4
    from app.platform.school_facts import f5_policy as f5

    actor = ctx._issue_staff_actor_context(
        account_user_id=1,
        school_id=ctx.SchoolId(UUID(int=1)),
        person_id=ctx.PersonId(UUID(int=2)),
        assignment_id=ctx.AssignmentId(UUID(int=3)),
        assignment_version=1,
        authorization_version=2,
    )
    assert not issubclass(f5.F5FactScope, f4.F4FactScope)
    assert not (f4.F4_OPERATIONS & f5.F5_OPERATIONS)
    now = datetime.now(UTC)
    for kind in KINDS:
        fields = tuple(f5.F5_FACT_FIELDS[kind])
        scope = f5.F5FactScope(actor.school_id, kind, ("object:one",), ("object:new",), fields)
        assert f5.F5FactScope.from_reference(scope.reference) == scope
        for action, duty in [("read", "reader"), ("maintain", "maintainer")]:
            code = f"school_facts.f5.{kind}.{action}"
            request = ThinAuthorizationRequest(
                actor,
                f5.F5_SCOPE_OWNER,
                code,
                UUID(int=4),
                code,
                actor.school_id,
                actor.authorization_version,
                UUID(int=5),
                now,
            )
            assert f5.F5FactPolicy().requirement_for(request).duty_code == f"f5_{kind}_{duty}"
            assert f4.F4FactPolicy().requirement_for(request) is None
        request = ScopeResolutionRequest(
            actor.school_id,
            UUID(int=4),
            f5.F5_SCOPE_OWNER,
            f5.F5_SCOPE_SCHEMA,
            1,
            scope.document,
            scope.document_sha256,
            now,
        )
        assert f5.F5ScopeResolver(lookup_scope=lambda *_: True).resolve(request) is not None
        assert f5.F5ScopeResolver(lookup_scope=lambda *_: False).resolve(request) is None
        assert f4.F4ScopeResolver(lookup_scope=lambda *_: True).resolve(request) is None
        with pytest.raises(ValueError):
            f5.F5FactScope(actor.school_id, kind, ("*",), (), fields)
        with pytest.raises(ValueError):
            f5.F5FactScope(actor.school_id, kind, ("x",), (), ("private_phone",))
    with pytest.raises(ValueError):
        f5.F5FactScope(actor.school_id, "education_class", ("class:a",), (), ("display_name",))


@pytest.mark.parametrize("kind,phase", [("education_class", "f4"), ("teaching_subject", "f5")])
@pytest.mark.parametrize("metadata_only", [False, True])
def test_projection_selects_only_the_fact_owner_sql(kind, phase, metadata_only):
    from contextlib import contextmanager
    from datetime import UTC, datetime

    from sqlalchemy.dialects import postgresql

    from app.composition.school_fact_education_reader import SqlEducationProjection

    statements = []
    events = []

    class Executor:
        @contextmanager
        def validated_read(self, before, after):
            before()
            events.append("begin")
            try:
                yield self
                after()
            finally:
                events.append("closed")

        def fetch(self, relation):
            statements.append(relation)
            return [{"payload": {"records": [], "school_fact_version": 1}}]

    projection = SqlEducationProjection(Executor(), current_sessions=lambda: None)
    projection.read(
        school_id=UUID(int=1),
        fact_kind=kind,
        as_of=datetime.now(UTC),
        known_at=None,
        filter={},
        metadata_only=metadata_only,
        source_keys=lambda: ("x",),
        field_names=lambda: ("display_name",),
        before_read=lambda: events.append("authorized"),
        after_read=lambda _: events.append("revalidated"),
    )
    compiled = str(statements[0].data_statement.compile(dialect=postgresql.dialect()))
    assert f"school_fact_{phase}_{'coordinates' if metadata_only else 'targets'}" in compiled
    assert events == ["authorized", "begin", "revalidated", "closed"]


def test_f5_real_thin_authority_rechecks_current_grant_and_identity():
    from dataclasses import replace
    from datetime import timedelta

    from app.composition.school_fact_education_authority import EducationAuthority
    from app.composition.school_fact_education_settings import EducationConfiguration
    from app.platform.authorization.thin_service import ThinAuthorizationService
    from app.platform.governance.contracts import GovernanceDependencyVersion
    from app.platform.school_facts.f5_policy import F5FactPolicy, F5FactScope
    from tests.governance.test_f4_authority import NOW, Governance, actor

    scope = F5FactScope(
        actor().school_id, "teaching_subject", ("subject:a",), ("subject:new",), ("display_name",)
    )

    class TeachingGovernance(Governance):
        def issue(self, query):
            basis = super().issue(query)
            deps = tuple(
                GovernanceDependencyVersion(
                    d.authority.replace("school_facts.f4", "school_facts.f5"),
                    d.reference,
                    d.version,
                )
                for d in basis.dependency_versions
            )
            return replace(basis, dependency_versions=deps)

    governance = TeachingGovernance(scope, UUID(int=4))
    config = EducationConfiguration(
        approval_reference="f5",
        scope_bindings=(
            {
                "assignment_id": actor().assignment_id,
                "scope_id": UUID(int=4),
                "operation_code": "school_facts.f5.teaching_subject.read",
            },
        ),
    )
    auth = EducationAuthority(
        thin=ThinAuthorizationService(
            governance_basis_service=governance, module_policies={"school_facts.f5": F5FactPolicy()}
        ),
        configuration=config,
    )
    permit = auth.require_scope(
        actor(), fact_kind="teaching_subject", action="read", checked_at=NOW
    )
    assert permit.scope == scope
    auth.revalidate(permit, actor=actor(), checked_at=NOW)
    with pytest.raises(PermissionError):
        auth.revalidate(permit, actor=actor(), checked_at=NOW + timedelta(days=8))
    governance.valid = False
    with pytest.raises(PermissionError):
        auth.revalidate(permit, actor=actor(), checked_at=NOW)


def test_old_f4_engineering_never_advertises_teaching_duties():
    from app.composition.school_fact_f4_engineering_governance import F4_ALLOWED_DUTIES
    from app.domains.students.teaching_contracts import TEACHING_KINDS

    assert not any(f"f4_{kind}_reader" in F4_ALLOWED_DUTIES for kind in TEACHING_KINDS)
