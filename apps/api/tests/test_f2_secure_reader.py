from datetime import UTC, datetime
from importlib import import_module, util
from uuid import UUID

import pytest
from sqlalchemy.dialects import postgresql

from app.platform import contracts as actors
from app.platform.authorization.thin_contracts import ScopePlan
from app.platform.contracts import AssignmentId, PersonId, SchoolId

NOW = datetime(2026, 9, 6, tzinfo=UTC)
SCHOOL = UUID(int=1)


def mod(name):
    assert util.find_spec(name) is not None, f"missing F2 capability: {name}"
    return import_module(name)


def actor():
    return actors._issue_staff_actor_context(
        account_user_id=7,
        school_id=SchoolId(SCHOOL),
        person_id=PersonId(UUID(int=2)),
        assignment_id=AssignmentId(UUID(int=3)),
        assignment_version=1,
        authorization_version=1,
    )


def test_all_f2_families_have_read_and_distinct_maintenance_fixed_operations():
    api = mod("app.platform.authorization.f2_reader_compiler")
    reg = mod("app.platform.authorization.secure_reader_registry").f2_operation_registry()
    for kind in api.F2_DEFINITIONS:
        reading = reg.require(f"school_facts.f2.{kind}.read")
        maintenance = reg.require(f"school_facts.f2.{kind}.maintain")
        assert reading.action_code != maintenance.action_code
    assert reg.require("school_facts.official_contacts")
    with pytest.raises(KeyError):
        reg.require("school_facts.f2.login_mapping.read")


@pytest.mark.parametrize(
    "kind",
    [
        "school",
        "person",
        "organization",
        "assignment",
        "location",
        "location_alias",
        "organization_campus_relationship",
        "person_contact",
        "official_contact",
        "policy",
        "policy_scope",
        "policy_version",
        "organization_profile",
        "organization_membership",
        "organization_location",
        "person_workplace",
        "school_service",
        "fact_quality",
    ],
)
def test_each_relation_filters_school_sourcekeys_and_time_before_projection(kind):
    api = mod("app.platform.authorization.f2_reader_compiler")
    policy = mod("app.platform.school_facts.f2_policy")
    contracts = mod("app.platform.authorization.f2_reader_contracts")
    reg = mod("app.platform.authorization.secure_reader_registry").f2_operation_registry()
    scope = policy.F2FactScope(school_id=SCHOOL, fact_kind=kind, object_keys=("allowed.object",))
    reference = scope.reference
    if kind == "fact_quality":
        import json

        target = policy.F2FactScope(
            school_id=SCHOOL, fact_kind="official_contact", object_keys=("target.allowed",)
        )
        reference = json.dumps(
            {"quality_scope": scope.reference, "target_scopes": [target.reference]}
        )
    relation = api.compile_f2(
        operation=reg.require(f"school_facts.f2.{kind}.read"),
        actor=actor(),
        scope_plan=ScopePlan(
            scope_id=UUID(int=4), owner="school_facts.f2", reference=reference, version=1
        ),
        as_of=NOW,
        after_id=None,
        page_size=2,
        selection=contracts.F2FactReadSelection(),
    )
    for statement in [relation.count_statement, relation.data_statement]:
        sql = str(
            statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
        )
        assert "allowed.object" in sql and "school_id" in sql
        assert "2026-09-06" in sql
        assert "SELECT *" not in sql
    assert "LIMIT 3" in str(
        relation.data_statement.compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )


def test_compiler_rejects_cross_family_scope_before_any_sql():
    api = mod("app.platform.authorization.f2_reader_compiler")
    policy = mod("app.platform.school_facts.f2_policy")
    reg = mod("app.platform.authorization.secure_reader_registry").f2_operation_registry()
    scope = policy.F2FactScope(
        school_id=SCHOOL, fact_kind="person", object_keys=("person.allowed",)
    )
    with pytest.raises(ValueError):
        api.compile_f2(
            operation=reg.require("school_facts.f2.person_contact.read"),
            actor=actor(),
            scope_plan=ScopePlan(UUID(int=4), "school_facts.f2", scope.reference, 1),
            as_of=NOW,
            after_id=None,
            page_size=10,
            selection=None,
        )


def test_private_denial_does_not_open_reader_transaction():
    from types import SimpleNamespace

    from app.platform.authorization.secure_reader_compiler import ServerOwnedProjectionCompiler
    from app.platform.authorization.secure_reader_contracts import (
        SecureReadDenied,
        SecureReadRequest,
    )
    from app.platform.authorization.secure_reader_service import SecureCursorCodec, SecureReader

    reg = mod("app.platform.authorization.secure_reader_registry").f2_operation_registry()

    class Executor:
        def repeatable_read(self):
            pytest.fail("A denied private query must never reach SQL")

    authorizer = SimpleNamespace(decide=lambda **kwargs: SimpleNamespace(allowed=False))
    reader = SecureReader(
        registry=reg,
        compiler=ServerOwnedProjectionCompiler(),
        authorizer=authorizer,
        executor=Executor(),
        cursor_codec=SecureCursorCodec(b"k"),
        clock=lambda: NOW,
    )
    with pytest.raises(SecureReadDenied):
        reader.read(
            SecureReadRequest(actor=actor(), operation_code="school_facts.f2.person_contact.read")
        )


def test_reference_codec_remembers_authorized_read_and_preserves_long_original():
    from app.platform.authorization.secure_reader_contracts import SecureReadPage

    contracts = mod("app.platform.authorization.f2_reader_contracts")
    repo = mod("app.composition.school_fact_f2_query_repository")
    body = "制度全文" * 1000 + "\n末尾"
    row = contracts.F2FactRow(
        UUID(int=8),
        "policy_version",
        "policy.version",
        {"display_name": "制度", "content_text": body},
        2,
        9,
        NOW,
        None,
        "formal_publication",
        UUID(int=9),
        NOW,
    )

    class Reader:
        def read(self, request):
            return SecureReadPage((row,), 1, NOW, None)

    repository = repo.F2FactQueryRepository(
        reader=Reader(), reference_key=b"test-reference-secret", clock=lambda: NOW
    )
    result = repository.query(actor=actor(), fact_kind="policy_version", scope="all_authorized")
    assert result.records[0].content["content_text"] == body
    assert "policy.version" not in result.records[0].fact_ref
    target = repository.prepare_target(
        actor=actor(), fact_kind="policy_version", target_ref=result.records[0].fact_ref
    )
    assert target.source_key == "policy.version" and target.content["content_text"] == body
    with pytest.raises(ValueError):
        repository.prepare_target(
            actor=actor(), fact_kind="person", target_ref=result.records[0].fact_ref
        )


def test_same_source_public_ref_stable_across_content_uuid_replacement():
    repo = mod("app.composition.school_fact_f2_query_repository")
    repository = repo.F2FactQueryRepository(reader=object(), reference_key=b"test-reference-secret")
    first = repository.reference_for(
        actor=actor(), fact_kind="official_contact", source_key="contact.official"
    )
    second = repository.reference_for(
        actor=actor(), fact_kind="official_contact", source_key="contact.official"
    )
    assert first == second
    assert (
        repository.source_for(actor=actor(), fact_kind="official_contact", reference=first)
        == "contact.official"
    )


def _plan(kind, key, *, content=None, expected=1):
    from app.platform.school_facts.contracts import (
        CanonicalFactChangeSetV2,
        CanonicalFactChangeV2,
        FactOriginV1,
    )

    return CanonicalFactChangeSetV2(
        source_sha256="a" * 64,
        base_version=1,
        changes=(
            CanonicalFactChangeV2(
                operation="upsert" if content else "revoke",
                fact_kind=kind,
                source_key=key,
                expected_version=expected,
                effective_at=NOW,
                origin=FactOriginV1(sheet_code="facts", row_number=1),
                content=content,
            ),
        ),
    )


def _guard(scopes, *, revoked=False, locked_epoch=1):
    from app.platform.authorization.thin_service import ThinAuthorizationService
    from app.platform.governance.contracts import (
        GovernanceAuthorizationBasis,
        GovernanceDependencyVersion,
    )
    from app.platform.school_facts.f2_policy import F2FactPolicy, F2FactScope

    api = mod("app.composition.school_fact_f2_query_repository")
    events = []

    class Governance:
        def issue(self, q):
            events.append(("decide", q.duty_code))
            keys = scopes.get(q.duty_code)
            if keys is None:
                raise ValueError("No duty")
            kind = q.duty_code.removeprefix("f2_").rsplit("_", 1)[0]
            scope = (
                keys
                if isinstance(keys, F2FactScope)
                else F2FactScope(school_id=SCHOOL, fact_kind=kind, object_keys=tuple(keys))
            )
            deps = (
                GovernanceDependencyVersion(
                    "school_facts.assignment", str(actor().assignment_id), 1
                ),
                GovernanceDependencyVersion("school_facts.person", str(actor().person_id), 1),
                GovernanceDependencyVersion("school_facts.f2.scope-target", scope.reference, 1),
                GovernanceDependencyVersion("governance.duty", str(UUID(int=20)), 1),
                GovernanceDependencyVersion("governance.scope", str(q.scope_id), 1),
                GovernanceDependencyVersion("governance.grant", str(UUID(int=21)), 1),
                GovernanceDependencyVersion(
                    "governance.assignment_epoch", str(actor().assignment_id), 1
                ),
            )
            return GovernanceAuthorizationBasis(
                SCHOOL,
                actor().assignment_id,
                q.duty_code,
                q.scope_id,
                UUID(int=21),
                1,
                1,
                NOW,
                None,
                deps,
            )

        def validate(self, basis, *, as_of):
            events.append(("validate", basis.duty_code))
            return not revoked

    thin = ThinAuthorizationService(
        governance_basis_service=Governance(), module_policies={"school_facts.f2": F2FactPolicy()}
    )
    guard = api.F2FinalAuthorizationGuard(
        thin_service=thin, scope_id_resolver=lambda op, a: UUID(int=4), clock=lambda: NOW
    )

    class Session:
        def scalar(self, statement, params):
            events.append(("lock", None))
            return locked_epoch

    return guard, Session(), events


def test_read_permission_never_implies_maintenance():
    from app.platform.school_facts.application import SchoolFactChangeRejected

    guard, _, _ = _guard({"f2_person_reader": ("person.allowed",)})
    with pytest.raises(SchoolFactChangeRejected):
        guard.require_change_set(actor(), _plan("person", "person.allowed"))


def test_each_change_scope_checked_and_reference_denial_rejects_whole_plan():
    from app.platform.school_facts.application import SchoolFactChangeRejected

    guard, _, _ = _guard({"f2_person_maintainer": ("person.allowed",)})
    guard.require_change_set(actor(), _plan("person", "person.allowed"))
    with pytest.raises(SchoolFactChangeRejected):
        guard.require_change_set(actor(), _plan("person", "person.other"))
    guard, _, _ = _guard(
        {
            "f2_organization_location_maintainer": ("office.allowed",),
            "f2_organization_reader": ("org.allowed",),
        }
    )
    content = {
        "organization_source_key": "org.allowed",
        "location_source_key": "loc.private",
        "purpose": "office",
        "is_primary": False,
        "basis_reference": "formal",
    }
    with pytest.raises(SchoolFactChangeRejected):
        guard.require_change_set(
            actor(), _plan("organization_location", "office.allowed", content=content)
        )


@pytest.mark.parametrize("revoked,epoch", [(True, 1), (False, 2), (False, 0)])
def test_final_lock_and_each_basis_revalidation_prevent_revocation_race(revoked, epoch):
    from app.platform.school_facts.application import SchoolFactChangeRejected

    guard, session, events = _guard(
        {"f2_person_maintainer": ("person.allowed",)}, revoked=revoked, locked_epoch=epoch
    )
    context = actors._issue_command_execution_context(
        actor=actor(), request_id=UUID(int=77), idempotency_key="f2-test"
    )
    with pytest.raises(SchoolFactChangeRejected):
        guard(session, context, _plan("person", "person.allowed"))
    assert events[0][0] == "lock"


def test_terminal_relationship_never_skips_original_reference_authority():
    from app.platform.school_facts.application import SchoolFactChangeRejected

    guard, _, _ = _guard({"f2_organization_location_maintainer": ("office.allowed",)})
    with pytest.raises(SchoolFactChangeRejected, match="原事实引用"):
        guard.require_change_set(actor(), _plan("organization_location", "office.allowed"))


def test_related_selector_is_fixed_sql_and_cursor_bound():
    from app.platform.authorization.f2_reader_compiler import compile_f2
    from app.platform.authorization.f2_reader_contracts import F2FactReadSelection
    from app.platform.authorization.secure_reader_registry import f2_operation_registry
    from app.platform.school_facts.f2_policy import F2FactScope

    scope = F2FactScope(SCHOOL, "organization_location", ("office.allowed",))
    selector = F2FactReadSelection(related_kind="organization", related_source_key="org.allowed")
    relation = compile_f2(
        operation=f2_operation_registry().require("school_facts.f2.organization_location.read"),
        actor=actor(),
        scope_plan=ScopePlan(UUID(int=4), "school_facts.f2", scope.reference, 1),
        as_of=NOW,
        after_id=None,
        page_size=3,
        selection=selector,
    )
    sql = str(
        relation.data_statement.compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )
    assert "org.allowed" in sql and "organization_unit_id" in sql
    assert (
        selector.binding_key
        != F2FactReadSelection(
            related_kind="organization", related_source_key="org.other"
        ).binding_key
    )


def test_explicit_reference_outside_scope_rejected_before_sql_not_reported_absent():
    from app.platform.authorization.f2_reader_compiler import compile_f2
    from app.platform.authorization.f2_reader_contracts import F2FactReadSelection
    from app.platform.authorization.secure_reader_registry import f2_operation_registry
    from app.platform.school_facts.f2_policy import F2FactScope

    scope = F2FactScope(SCHOOL, "person", ("person.allowed",))
    with pytest.raises(ValueError, match="outside"):
        compile_f2(
            operation=f2_operation_registry().require("school_facts.f2.person.read"),
            actor=actor(),
            scope_plan=ScopePlan(UUID(int=4), "school_facts.f2", scope.reference, 1),
            as_of=NOW,
            after_id=None,
            page_size=1,
            selection=F2FactReadSelection(source_key="person.other"),
        )


def test_related_object_denial_never_reads_relationship_records():
    from app.composition.school_fact_f2_query_repository import F2FactQueryRepository
    from app.platform.authorization.secure_reader_contracts import SecureReadDenied

    guard, _, _ = _guard({"f2_organization_reader": ("org.allowed",)})

    class Reader:
        def read(self, request):
            pytest.fail("Denied related object must not reach SQL")

    repository = F2FactQueryRepository(
        reader=Reader(), reference_key=b"test-reference-secret", maintenance_guard=guard
    )
    ref = repository.reference_for(actor=actor(), fact_kind="organization", source_key="org.denied")
    with pytest.raises(SecureReadDenied):
        repository.query(actor=actor(), fact_kind="organization_location", related_ref=ref)


def test_quality_statement_sql_requires_exact_target_or_authorized_owner():
    import json

    from app.platform.authorization.f2_reader_compiler import compile_f2
    from app.platform.authorization.secure_reader_registry import f2_operation_registry
    from app.platform.school_facts.f2_policy import F2FactScope

    quality = F2FactScope(SCHOOL, "fact_quality", ("quality.negative",))
    target = F2FactScope(SCHOOL, "organization_location", creation_keys=("future.office",))
    owner = F2FactScope(SCHOOL, "organization", ("org.allowed",))
    envelope = json.dumps(
        {"quality_scope": quality.reference, "target_scopes": [target.reference, owner.reference]}
    )
    relation = compile_f2(
        operation=f2_operation_registry().require("school_facts.f2.fact_quality.read"),
        actor=actor(),
        scope_plan=ScopePlan(UUID(int=4), "school_facts.f2", envelope, 1),
        as_of=NOW,
        after_id=None,
        page_size=1,
        selection=None,
    )
    sql = str(
        relation.count_statement.compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )
    assert "target_source_key IS NULL" in sql and "org.allowed" in sql and "quality.negative" in sql


def test_creation_is_finite_and_existing_query_never_uses_creation_keys():
    from app.platform.authorization.f2_reader_compiler import compile_f2
    from app.platform.authorization.f2_reader_contracts import F2FactReadSelection
    from app.platform.authorization.secure_reader_registry import f2_operation_registry
    from app.platform.school_facts.f2_policy import F2FactScope

    scope = F2FactScope(SCHOOL, "person", creation_keys=("person.future",))
    kwargs = dict(
        actor=actor(),
        scope_plan=ScopePlan(UUID(int=4), "school_facts.f2", scope.reference, 1),
        as_of=NOW,
        after_id=None,
        page_size=1,
    )
    reg = f2_operation_registry()
    relation = compile_f2(
        operation=reg.require("school_facts.f2.person.maintain"),
        selection=F2FactReadSelection(creation_key="person.future"),
        **kwargs,
    )
    sql = str(
        relation.data_statement.compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )
    assert "NOT (EXISTS" in sql and "person.future" in sql and "school_fact_current_version" in sql
    with pytest.raises(ValueError):
        compile_f2(
            operation=reg.require("school_facts.f2.person.maintain"),
            selection=F2FactReadSelection(creation_key="person.arbitrary"),
            **kwargs,
        )
    with pytest.raises(ValueError):
        compile_f2(
            operation=reg.require("school_facts.f2.person.read"),
            selection=F2FactReadSelection(source_key="person.future"),
            **kwargs,
        )


def test_count_includes_entire_authorized_relation_after_cursor():
    from app.platform.authorization.f2_reader_compiler import compile_f2
    from app.platform.authorization.secure_reader_registry import f2_operation_registry
    from app.platform.school_facts.f2_policy import F2FactScope

    scope = F2FactScope(SCHOOL, "person", ("person.allowed",))
    after = UUID(int=12345)
    relation = compile_f2(
        operation=f2_operation_registry().require("school_facts.f2.person.read"),
        actor=actor(),
        scope_plan=ScopePlan(UUID(int=4), "school_facts.f2", scope.reference, 1),
        as_of=NOW,
        after_id=after,
        page_size=1,
        selection=None,
    )
    kwargs = dict(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    assert str(after) not in str(relation.count_statement.compile(**kwargs))
    assert str(after) in str(relation.data_statement.compile(**kwargs))


def test_fact_time_never_replaces_current_authorization_time():
    from contextlib import contextmanager
    from datetime import timedelta

    from app.platform.authorization.secure_reader_compiler import ServerOwnedProjectionCompiler
    from app.platform.authorization.secure_reader_contracts import (
        ReadAuthorization,
        SecureReadRequest,
    )
    from app.platform.authorization.secure_reader_registry import f2_operation_registry
    from app.platform.authorization.secure_reader_service import SecureCursorCodec, SecureReader
    from app.platform.authorization.thin_contracts import NamedProjection
    from app.platform.school_facts.f2_policy import F2FactScope

    historical = NOW - timedelta(days=30)
    times = []
    scope = F2FactScope(SCHOOL, "person", ("person.allowed",))

    class Authorization:
        def decide(self, *, operation, actor, as_of):
            times.append(as_of)
            return ReadAuthorization(
                True,
                "basis",
                ScopePlan(UUID(int=4), "school_facts.f2", scope.reference, 1),
                NamedProjection(operation.projection_name, 1),
                "fingerprint",
            )

        def revalidate(self, authorization, *, actor, as_of):
            times.append(as_of)
            return True

    class Transaction:
        def count(self, relation):
            assert relation.as_of == historical
            return 0

        def fetch(self, relation):
            return ()

    class Executor:
        @contextmanager
        def repeatable_read(self):
            yield Transaction()

    reader = SecureReader(
        registry=f2_operation_registry(),
        compiler=ServerOwnedProjectionCompiler(),
        authorizer=Authorization(),
        executor=Executor(),
        cursor_codec=SecureCursorCodec(b"cursor"),
        clock=lambda: NOW,
    )
    page = reader.read(
        SecureReadRequest(
            actor=actor(), operation_code="school_facts.f2.person.read", factual_at=historical
        )
    )
    assert page.as_of == historical and times == [NOW, NOW, NOW]


@pytest.mark.parametrize("bound", [False, True])
def test_targeted_quality_without_owner_read_scope_is_fail_closed(bound):
    import json

    from app.platform.authorization.f2_reader_compiler import compile_f2
    from app.platform.authorization.secure_reader_registry import f2_operation_registry
    from app.platform.school_facts.f2_policy import F2FactScope

    quality = F2FactScope(SCHOOL, "fact_quality", ("quality.targeted",))
    target = F2FactScope(SCHOOL, "organization_location", ("office.allowed",))
    envelope = json.dumps({"quality_scope": quality.reference, "target_scopes": [target.reference]})
    if bound:
        envelope = json.dumps({**json.loads(envelope), "target_fact_kind": "organization_location"})
    relation = compile_f2(
        operation=f2_operation_registry().require("school_facts.f2.fact_quality.read"),
        actor=actor(),
        scope_plan=ScopePlan(UUID(int=4), "school_facts.f2", envelope, 1),
        as_of=NOW,
        after_id=None,
        page_size=1,
        selection=None,
    )
    sql = str(
        relation.count_statement.compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )
    assert "WHERE false" in sql or "AND false" in sql


@pytest.mark.parametrize("bound", [False, True])
def test_quality_correction_filters_replacement_with_same_exact_target_scope(bound):
    import json

    from app.platform.authorization.f2_reader_compiler import compile_f2
    from app.platform.authorization.secure_reader_registry import f2_operation_registry
    from app.platform.school_facts.f2_policy import F2FactScope

    quality = F2FactScope(SCHOOL, "fact_quality", ("quality.correction",))
    target = F2FactScope(SCHOOL, "organization_location", ("office.original",))
    owner = F2FactScope(SCHOOL, "organization", ("org.allowed",))
    envelope = json.dumps(
        {"quality_scope": quality.reference, "target_scopes": [target.reference, owner.reference]}
    )
    if bound:
        envelope = json.dumps({**json.loads(envelope), "target_fact_kind": "organization_location"})
    relation = compile_f2(
        operation=f2_operation_registry().require("school_facts.f2.fact_quality.read"),
        actor=actor(),
        scope_plan=ScopePlan(UUID(int=4), "school_facts.f2", envelope, 1),
        as_of=NOW,
        after_id=None,
        page_size=1,
        selection=None,
    )
    sql = str(
        relation.count_statement.compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )
    assert "replacement_source_key IS NULL" in sql
    assert (
        "target_fact_kind = 'organization_location' AND "
        "school_fact_qualities.replacement_source_key IN ('office.original')"
    ) in sql


def test_schema1_official_contact_still_requires_exact_current_f2_target():
    from app.platform.school_facts.application import SchoolFactChangeRejected
    from app.platform.school_facts.contracts import CanonicalFactChangeSetV1

    content = {
        "owner_kind": "location",
        "location_source_key": "campus.allowed",
        "contact_kind": "phone",
        "contact_value": "010-00000000",
        "lifecycle_status": "active",
        "visibility_scope": "public",
    }
    import json

    raw = _plan("official_contact", "contact.new-campus", content=content).model_dump(
        mode="json", exclude_unset=True
    )
    raw["schema_version"] = 1
    raw["changes"][0]["schema_version"] = 1
    plan = CanonicalFactChangeSetV1.model_validate_json(json.dumps(raw))
    guard, session, events = _guard(
        {
            "f2_official_contact_maintainer": ("contact.old-campus",),
            "f2_location_reader": ("campus.allowed",),
        }
    )
    context = actors._issue_command_execution_context(
        actor=actor(), request_id=UUID(int=77), idempotency_key="f2-legacy-plan"
    )
    with pytest.raises(SchoolFactChangeRejected, match="未获权的目标"):
        guard(session, context, plan)
    assert events[0][0] == "lock"
    assert ("decide", "f2_official_contact_maintainer") in events
    raw["changes"][0]["source_key"] = "contact.old-campus"
    allowed = CanonicalFactChangeSetV1.model_validate_json(json.dumps(raw))
    guard(session, context, allowed)


def test_bound_quality_scope_adds_sql_filter_before_read_and_keeps_reference_guards():
    import json

    from app.platform.authorization.f2_reader_compiler import compile_f2
    from app.platform.authorization.secure_reader_registry import f2_operation_registry
    from app.platform.school_facts.f2_policy import F2FactScope

    quality = F2FactScope(SCHOOL, "fact_quality", ("q.allowed",))
    target = F2FactScope(SCHOOL, "organization_profile", ("profile.allowed",))
    owner = F2FactScope(SCHOOL, "organization", ("owner.allowed",))
    envelope = json.dumps(
        {
            "quality_scope": quality.reference,
            "target_scopes": [target.reference, owner.reference],
            "target_fact_kind": "organization_profile",
        }
    )
    relation = compile_f2(
        operation=f2_operation_registry().require("school_facts.f2.fact_quality.read"),
        actor=actor(),
        scope_plan=ScopePlan(UUID(int=4), "school_facts.f2", envelope, 1),
        as_of=NOW,
        after_id=None,
        page_size=10,
        selection=None,
    )
    for statement in (relation.count_statement, relation.data_statement):
        sql = str(
            statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
        )
        assert "target_fact_kind = 'organization_profile'" in sql
        assert (
            "replacement_source_key" in sql and "owner.allowed" in sql and "profile.allowed" in sql
        )


def _self_contact_query_fixture(*, own_rows=(), current_rows=(), read_allowed=False):
    from types import SimpleNamespace

    from app.composition.school_fact_f2_query_repository import F2FactQueryRepository
    from app.platform.authorization.secure_reader_contracts import SecureReadDenied, SecureReadPage

    seen = []

    class Reader:
        def read(self, request):
            seen.append(request)
            if request.operation_code.endswith(".read") and not read_allowed:
                raise SecureReadDenied("normal family access denied")
            rows = current_rows if request.factual_at is None else own_rows
            if isinstance(rows, Exception):
                raise rows
            return SecureReadPage(tuple(rows), len(rows), request.factual_at or NOW, None)

    def no_school_scope(*args, **kwargs):
        from app.platform.school_facts.application import SchoolFactChangeRejected

        raise SchoolFactChangeRejected("No school-wide duty in the self-only fixture")

    guard = SimpleNamespace(
        require_related=lambda *args: object(),
        revalidate=lambda *args: None,
        require_current=no_school_scope,
    )
    repository = F2FactQueryRepository(
        reader=Reader(),
        reference_key=b"private-self-test-key",
        maintenance_guard=guard,
        clock=lambda: NOW,
    )
    return repository, seen


def _self_contact_row():
    from app.platform.authorization.f2_reader_contracts import F2FactRow

    return F2FactRow(
        UUID(int=88),
        "person_contact",
        "contact.selected",
        {"person_source_key": "person.selected", "contact_value": "current-only-value"},
        1,
        1,
        NOW,
        None,
        "formal",
        UUID(int=89),
        NOW,
    )


def _contact_selector(repository, mode):
    if mode == "reference":
        return {
            "reference": repository.reference_for(
                actor=actor(), fact_kind="person_contact", source_key="contact.selected"
            )
        }
    if mode == "related_ref":
        return {
            "related_ref": repository.reference_for(
                actor=actor(), fact_kind="person", source_key="person.selected"
            )
        }
    return {"name": "explicit-contact-selector"}


@pytest.mark.parametrize("selection", ["reference", "related_ref"])
def test_empty_selected_self_fallback_is_denied_not_missing_contact(selection):
    repository, seen = _self_contact_query_fixture()
    result = repository.query(
        actor=actor(), fact_kind="person_contact", **_contact_selector(repository, selection)
    )
    assert result.status == "forbidden" and result.matched_count is None and result.records == ()
    assert len(seen) == 2
    assert seen[1].selection == seen[0].selection
    assert seen[1].operation_code == "school_facts.f2.person_contact.self"


@pytest.mark.parametrize("selection", ["reference", "related_ref"])
def test_matching_self_contact_stays_known_with_explicit_personal_scope(selection):
    repository, seen = _self_contact_query_fixture(current_rows=(_self_contact_row(),))
    result = repository.query(
        actor=actor(), fact_kind="person_contact", **_contact_selector(repository, selection)
    )
    assert result.status == "known" and len(result.records) == 1
    assert "本人" in result.query_scope
    assert len(seen) == 2


@pytest.mark.parametrize("has_row", [False, True])
def test_all_authorized_self_fallback_is_explicitly_only_own_collection(has_row):
    repository, seen = _self_contact_query_fixture(
        current_rows=(_self_contact_row(),) if has_row else ()
    )
    result = repository.query(actor=actor(), fact_kind="person_contact", scope="all_authorized")
    assert result.status == ("known" if has_row else "no_current_records")
    assert "本人" in result.query_scope and len(seen) == 2


@pytest.mark.parametrize("selection", ["reference", "related_ref"])
def test_own_history_absence_confirms_same_self_target_without_returning_current_value(
    selection,
):
    from datetime import timedelta

    repository, seen = _self_contact_query_fixture(current_rows=(_self_contact_row(),))
    past = NOW - timedelta(days=100)
    result = repository.query(
        actor=actor(),
        fact_kind="person_contact",
        factual_at=past,
        **_contact_selector(repository, selection),
    )
    assert result.status == "no_current_records" and result.records == ()
    assert result.factual_at == past and result.matched_count == 0
    assert "current-only-value" not in str(result)
    assert len(seen) == 3
    assert seen[2].selection == seen[1].selection == seen[0].selection
    assert seen[2].operation_code == "school_facts.f2.person_contact.self"
    assert seen[2].factual_at is None and seen[2].cursor is None


def test_unconfirmable_historical_self_selection_does_not_claim_absence():
    from datetime import timedelta

    repository, seen = _self_contact_query_fixture()
    result = repository.query(
        actor=actor(),
        fact_kind="person_contact",
        factual_at=NOW - timedelta(days=100),
        **_contact_selector(repository, "reference"),
    )
    assert result.status == "forbidden" and result.records == () and result.matched_count is None
    assert len(seen) == 3


def test_historical_empty_self_collection_needs_no_target_confirmation():
    from datetime import timedelta

    repository, seen = _self_contact_query_fixture()
    result = repository.query(
        actor=actor(),
        fact_kind="person_contact",
        factual_at=NOW - timedelta(days=100),
        scope="all_authorized",
    )
    assert result.status == "no_current_records" and "本人" in result.query_scope
    assert len(seen) == 2


def test_normal_authorized_empty_target_never_falls_back_to_self():
    repository, seen = _self_contact_query_fixture(read_allowed=True)
    result = repository.query(
        actor=actor(), fact_kind="person_contact", **_contact_selector(repository, "reference")
    )
    assert result.status == "no_current_records" and len(seen) == 1
    assert "本人" not in result.query_scope


@pytest.mark.parametrize(
    "failure, expected", [("denied", "forbidden"), ("source", "source_unavailable")]
)
def test_historical_self_confirmation_failure_does_not_claim_missing(failure, expected):
    from datetime import timedelta

    from app.platform.authorization.secure_reader_contracts import SecureReadDenied
    from app.platform.school_facts.read_policy import SchoolFactAuthorizationUnavailable

    error = (
        SecureReadDenied("current self access revoked")
        if failure == "denied"
        else SchoolFactAuthorizationUnavailable("current source unavailable")
    )
    repository, seen = _self_contact_query_fixture(current_rows=error)
    result = repository.query(
        actor=actor(),
        fact_kind="person_contact",
        factual_at=NOW - timedelta(days=100),
        **_contact_selector(repository, "reference"),
    )
    assert result.status == expected and result.records == () and result.matched_count is None
    assert len(seen) == 3


def test_denied_self_target_keeps_model_card_denial_and_no_quality_read():
    from types import SimpleNamespace
    from typing import cast

    from app.composition.school_fact_directory_adapter import DirectoryProductAdapter
    from app.composition.school_fact_directory_gateway import (
        MODULE,
        DirectoryFactToolGateway,
        ProductActor,
    )
    from app.platform.assistant.tool_loop import ModelToolCall, ToolCallContext
    from tests.test_f2_directory_product import setup

    repository, seen = _self_contact_query_fixture()
    service, _, _, _ = setup()
    gateway = DirectoryFactToolGateway(
        resolve_actor=lambda _: cast(
            ProductActor, SimpleNamespace(actor=actor(), session_binding="session")
        ),
        query=DirectoryProductAdapter(repository),
        product=service,
        retention_seconds=60,
    )
    result = gateway.for_turn("private").execute(
        ModelToolCall(
            call_id="read",
            name=MODULE + ".query",
            arguments={"fact_kind": "person_contact", **_contact_selector(repository, "reference")},
        ),
        ToolCallContext("private", "conversation", "turn", "run", 1, UUID(int=90)),
    )
    assert result.model_content["status"] == "denied"
    assert result.fact_card is not None and result.fact_card["status"] == "denied"
    assert result.fact_card["records"] == result.model_content["records"] == []
    assert result.fact_card["matched_count"] is None
    assert result.model_content["authorized_result_set"]["matched_count"] is None
    assert result.history_check is None and len(seen) == 2


# Query capability is a domain contract, not a fabricated empty SQL result.
SELECTOR_MATRIX = {
    "school": (("display_name",), ()),
    "person": (("display_name",), ()),
    "organization": (("display_name",), ("organization",)),
    "assignment": (("position_title",), ("person", "organization")),
    "location": (("display_name",), ("location",)),
    "location_alias": (("alias",), ("location",)),
    "organization_campus_relationship": ((), ("organization", "location")),
    "person_contact": ((), ("person",)),
    "official_contact": ((), ("organization", "location")),
    "policy": ((), ()),
    "policy_scope": ((), ("policy", "organization", "location")),
    "policy_version": (("display_name",), ("policy", "organization", "policy_version")),
    "organization_profile": (("function_summary",), ("organization",)),
    "organization_membership": ((), ("person", "organization")),
    "organization_location": ((), ("organization", "location")),
    "person_workplace": ((), ("person", "assignment", "location")),
    "school_service": (
        ("display_name", "service_summary"),
        ("organization", "official_contact", "policy"),
    ),
    "fact_quality": ((), None),
}


@pytest.mark.parametrize("kind", SELECTOR_MATRIX)
def test_query_selector_matrix_all_enabled_families(kind):
    from app.platform.authorization.f2_reader_compiler import (
        F2_DEFINITIONS,
        query_selector_error,
        query_selector_support,
    )

    assert set(SELECTOR_MATRIX) == set(F2_DEFINITIONS)
    support = query_selector_support(kind)
    names, related = SELECTOR_MATRIX[kind]
    assert support["name_fields"] == list(names)
    expected_related = list(F2_DEFINITIONS) if related is None else list(related)
    assert support["related_kinds"] == expected_related
    assert support["name_match"] == "exact"
    assert query_selector_error(kind, name="exact") == (None if names else "name_not_supported")
    for related_kind in F2_DEFINITIONS:
        assert query_selector_error(kind, related_kind=related_kind) == (
            None if related_kind in expected_related else "related_kind_not_supported"
        )


@pytest.mark.parametrize(
    "kind,name,related",
    [
        ("organization_campus_relationship", "organization name", None),
        ("organization", None, "f2:location:opaque"),
        ("person_contact", "person name", None),
    ],
)
def test_unsupported_query_rejected_before_reader_or_reference_lookup(kind, name, related):
    repository, seen = _self_contact_query_fixture()
    result = repository.query(actor=actor(), fact_kind=kind, name=name, related_ref=related)
    assert result.status == "unsupported_selection"
    assert result.matched_count is None and result.records == ()
    assert seen == []


def test_compiler_rejects_unsupported_selection_before_sql_projection():
    from app.platform.authorization.f2_reader_compiler import compile_f2
    from app.platform.authorization.f2_reader_contracts import F2FactReadSelection
    from app.platform.authorization.secure_reader_registry import f2_operation_registry
    from app.platform.school_facts.f2_policy import F2FactScope

    scope = F2FactScope(school_id=SCHOOL, fact_kind="organization", object_keys=("org",))
    with pytest.raises(ValueError, match="related_kind_not_supported"):
        compile_f2(
            operation=f2_operation_registry().require("school_facts.f2.organization.read"),
            actor=actor(),
            scope_plan=ScopePlan(
                scope_id=1, version=1, owner="school_facts.f2", reference=scope.reference
            ),
            as_of=NOW,
            after_id=None,
            page_size=10,
            selection=F2FactReadSelection(related_kind="location", related_source_key="campus"),
        )


@pytest.mark.parametrize("kind", SELECTOR_MATRIX)
def test_supported_name_and_every_related_kind_compile_with_fixed_authority(kind):
    import json

    from app.platform.authorization.f2_reader_compiler import compile_f2
    from app.platform.authorization.f2_reader_contracts import F2FactReadSelection
    from app.platform.authorization.secure_reader_registry import f2_operation_registry
    from app.platform.school_facts.f2_policy import F2FactScope

    names, related = SELECTOR_MATRIX[kind]
    related = tuple(SELECTOR_MATRIX) if related is None else related
    scope = F2FactScope(SCHOOL, kind, ("allowed.object",))
    reference = scope.reference
    if kind == "fact_quality":
        reference = json.dumps(
            {
                "quality_scope": reference,
                "target_scopes": [
                    F2FactScope(SCHOOL, "organization", ("allowed.owner",)).reference
                ],
            }
        )
    selections = [F2FactReadSelection(name="exact value")] if names else []
    selections += [
        F2FactReadSelection(related_kind=target, related_source_key="related.object")
        for target in related
    ]
    for selection in selections:
        relation = compile_f2(
            operation=f2_operation_registry().require(f"school_facts.f2.{kind}.read"),
            actor=actor(),
            scope_plan=ScopePlan(UUID(int=4), "school_facts.f2", reference, 1),
            as_of=NOW,
            after_id=None,
            page_size=10,
            selection=selection,
        )
        for statement in (relation.count_statement, relation.data_statement):
            sql = str(
                statement.compile(
                    dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
                )
            )
            assert "allowed.object" in sql and str(SCHOOL) in sql
            if selection.name is not None:
                assert "exact value" in sql
            else:
                assert "related.object" in sql


@pytest.mark.integration
def test_directory_pagination_real_postgres_sql_on_explicit_disposable_cluster():
    """Actual F2 SQL/keyset/DTO path; synthetic authority, no production RLS claim."""
    import os
    from types import SimpleNamespace

    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import sessionmaker

    from app.composition.school_fact_directory_adapter import (
        DirectoryPageUnavailable,
        DirectoryProductAdapter,
    )
    from app.composition.school_fact_directory_product import FactQueryInput
    from app.composition.school_fact_f2_query_repository import F2FactQueryRepository
    from app.platform.authorization.secure_reader_adapters import SqlAlchemyRepeatableReadExecutor
    from app.platform.authorization.secure_reader_compiler import ServerOwnedProjectionCompiler
    from app.platform.authorization.secure_reader_contracts import SecureReadDenied
    from app.platform.authorization.secure_reader_registry import f2_operation_registry
    from app.platform.authorization.secure_reader_service import SecureCursorCodec, SecureReader
    from app.platform.authorization.thin_contracts import NamedProjection
    from app.platform.school_facts.f2_policy import F2FactScope

    dsn = os.environ.get("F5_DIRECTORY_DISPOSABLE_TEST_DSN")
    expected = os.environ.get("F5_DIRECTORY_DISPOSABLE_SYSTEM_ID")
    if not dsn or not expected:
        pytest.skip("requires an explicitly identified dedicated disposable PostgreSQL cluster")
    engine = create_engine(dsn)
    try:
        with engine.begin() as db:
            assert (
                db.scalar(text("SELECT system_identifier::text FROM pg_control_system()"))
                == expected
            )
            assert db.scalar(text("SELECT current_database()")) == "directory_pagination_test"
            assert (
                db.scalar(
                    text(
                        "SELECT count(*) FROM pg_database WHERE NOT datistemplate AND datname "
                        "NOT IN ('postgres', 'directory_pagination_test')"
                    )
                )
                == 0
            )
            db.execute(text("DROP SCHEMA IF EXISTS app CASCADE"))
            db.execute(
                text(
                    "DROP TABLE IF EXISTS pagination_revision, campus_location_aliases, "
                    "school_fact_change_lineage"
                )
            )
            db.execute(text("CREATE SCHEMA app"))
            db.execute(text("CREATE TABLE pagination_revision (version integer NOT NULL)"))
            db.execute(text("INSERT INTO pagination_revision VALUES (1)"))
            db.execute(
                text(
                    "CREATE FUNCTION app.school_fact_current_version(uuid) RETURNS integer "
                    "LANGUAGE SQL STABLE AS 'SELECT version FROM public.pagination_revision'"
                )
            )
            db.execute(
                text(
                    "CREATE TABLE campus_location_aliases (id uuid PRIMARY KEY, school_id "
                    "uuid, alias text, alias_kind text, location_id uuid, valid_from "
                    "timestamptz, valid_to timestamptz, version integer)"
                )
            )
            db.execute(
                text(
                    "CREATE TABLE school_fact_change_lineage (id uuid PRIMARY KEY, school_id "
                    "uuid, fact_kind text, fact_source_key text, operation text, target_id "
                    "uuid, target_version integer, batch_id uuid, created_at timestamptz)"
                )
            )
            for i in range(101):
                values = {
                    "id": UUID(int=i + 1),
                    "school": SCHOOL,
                    "alias": f"alias-{i}",
                    "key": f"location.alias.{i}",
                    "at": NOW,
                }
                db.execute(
                    text(
                        "INSERT INTO campus_location_aliases VALUES "
                        "(:id,:school,:alias,'official',NULL,:at,NULL,1)"
                    ),
                    values,
                )
                db.execute(
                    text(
                        "INSERT INTO school_fact_change_lineage VALUES "
                        "(:id,:school,'location_alias',:key,'upsert',:id,1,:id,:at)"
                    ),
                    values,
                )
        live = [True]
        scope = F2FactScope(
            SCHOOL, "location_alias", tuple(f"location.alias.{i}" for i in range(100))
        )

        class Authorizer:
            def decide(self, *, operation, **kw):
                return SimpleNamespace(
                    allowed=live[0],
                    scope_plan=ScopePlan(UUID(int=77), "school_facts.f2", scope.reference, 1),
                    projection=NamedProjection(operation.projection_name, 1),
                    basis_fingerprint="synthetic-pg-authority",
                )

            def revalidate(self, *args, **kw):
                return live[0]

        class QualityDenied:
            def read(self, request):
                raise SecureReadDenied("quality not in this synthetic scope")

        reader = SecureReader(
            registry=f2_operation_registry(),
            compiler=ServerOwnedProjectionCompiler(),
            authorizer=Authorizer(),
            executor=SqlAlchemyRepeatableReadExecutor(sessionmaker(bind=engine)),
            cursor_codec=SecureCursorCodec(b"public-disposable-test-only"),
            clock=lambda: NOW,
        )
        repository = F2FactQueryRepository(
            reader=reader,
            quality_readers={"location_alias": QualityDenied()},
            reference_key=b"public-test-reference-only",
            clock=lambda: NOW,
        )
        native_refs = []
        native_cursor = None
        while True:
            native = repository.query(
                actor=actor(),
                fact_kind="location_alias",
                scope="all_authorized",
                page_size=7,
                cursor=native_cursor,
            )
            native_refs.extend(row.fact_ref for row in native.records)
            assert native.matched_count == 100
            native_cursor = native.next_cursor
            if native_cursor is None:
                break
        assert len(native_refs) == len(set(native_refs)) == 100
        adapter = DirectoryProductAdapter(repository)
        query = FactQueryInput(fact_kind="location_alias", scope="all_authorized", page_size=4)
        first = adapter.query(actor=actor(), payload=query)
        assert len(first["records"]) == 4 and first["matched_count"] == 100
        seen = list(first["records"])
        page = first
        while page["next_cursor"]:
            page = adapter.query(
                actor=actor(), payload=query.model_copy(update={"cursor": page["next_cursor"]})
            )
            assert page["matched_count"] == 100 and 0 < len(page["records"]) <= 4
            assert page["factual_at"] == first["factual_at"]
            seen.extend(page["records"])
        assert len(seen) == len({r["reference"] for r in seen}) == 100
        assert {r["label"] for r in seen} == {f"alias-{i}" for i in range(100)}
        exact = adapter.query(
            actor=actor(), payload=FactQueryInput(fact_kind="location_alias", query_text="alias-99")
        )
        assert exact["matched_count"] == 1 and exact["records"][0]["label"] == "alias-99"
        absent = adapter.query(
            actor=actor(),
            payload=FactQueryInput(fact_kind="location_alias", query_text="alias-100"),
        )
        assert absent["matched_count"] == 0 and not absent["records"]
        continuation = query.model_copy(update={"cursor": first["next_cursor"]})
        live[0] = False
        with pytest.raises(DirectoryPageUnavailable):
            adapter.query(actor=actor(), payload=continuation)
        live[0] = True
        with engine.begin() as db:
            db.execute(text("UPDATE pagination_revision SET version=2"))
            db.execute(
                text("UPDATE campus_location_aliases SET alias='changed' WHERE id=:id"),
                {"id": UUID(int=100)},
            )
        with pytest.raises(DirectoryPageUnavailable):
            adapter.query(actor=actor(), payload=continuation)
    finally:
        engine.dispose()


def test_school_scope_compiles_school_predicate_without_empty_key_filter():
    from app.platform.authorization.f2_reader_compiler import compile_f2
    from app.platform.authorization.f2_reader_contracts import F2FactReadSelection
    from app.platform.authorization.secure_reader_registry import f2_operation_registry
    from app.platform.school_facts.f2_policy import F2FactScope

    scope = F2FactScope(SCHOOL, "person", coverage="school")
    kwargs = dict(
        operation=f2_operation_registry().require("school_facts.f2.person.read"),
        actor=actor(),
        scope_plan=ScopePlan(UUID(int=4), "school_facts.f2", scope.reference, 1),
        as_of=NOW,
        after_id=None,
        page_size=20,
        selection=F2FactReadSelection(source_key="future:person"),
    )
    relation = compile_f2(**kwargs)
    sql = str(
        relation.data_statement.compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )
    assert str(SCHOOL) in sql and "school_id =" in sql
    assert "future:person" in sql
    assert "IN (NULL)" not in sql
    other = F2FactScope(UUID(int=99), "person", coverage="school")
    with pytest.raises(ValueError):
        compile_f2(
            **(
                kwargs
                | {"scope_plan": ScopePlan(UUID(int=4), "school_facts.f2", other.reference, 1)}
            )
        )


def test_school_scope_new_target_ref_survives_repository_restart_but_not_revoke():
    from app.composition.school_fact_f2_query_repository import F2FactQueryRepository
    from app.platform.school_facts.f2_policy import F2FactScope

    scopes = {"f2_person_maintainer": F2FactScope(SCHOOL, "person", coverage="school")}
    guard, _, _ = _guard(scopes)

    def repo(g):
        return F2FactQueryRepository(
            reader=None, reference_key=b"test-school-reference", maintenance_guard=g
        )

    first = repo(guard)
    choices = first.creation_choices(actor=actor(), fact_kind="person")
    assert len(choices) == 1
    reference = choices[0]["creation_ref"]
    key = repo(guard).source_for(
        actor=actor(), fact_kind="person", reference=reference, creation=True
    )
    assert key
    with pytest.raises(ValueError):
        repo(guard).source_for(
            actor=actor(), fact_kind="person", reference=reference, creation=False
        )
    revoked, _, _ = _guard(scopes, revoked=True)
    with pytest.raises((ValueError, RuntimeError)):
        repo(revoked).source_for(
            actor=actor(), fact_kind="person", reference=reference, creation=True
        )
    denied, _, _ = _guard({})
    with pytest.raises((ValueError, RuntimeError)):
        repo(denied).source_for(
            actor=actor(), fact_kind="person", reference=reference, creation=True
        )


def test_school_scope_change_guard_still_requires_each_related_family():
    from app.platform.school_facts.application import SchoolFactChangeRejected
    from app.platform.school_facts.f2_policy import F2FactScope

    scopes = {"f2_person_maintainer": F2FactScope(SCHOOL, "person", coverage="school")}
    guard, _, _ = _guard(scopes)
    assert guard.require_change_set(actor(), _plan("person", "future:person"))
    with pytest.raises(SchoolFactChangeRejected):
        guard.require_change_set(actor(), _plan("location", "future:location"))


def test_school_scope_own_receipt_authorizes_only_actual_requested_coordinates():
    from app.composition.school_fact_own_result_authorization import F2OwnResultAuthorizer
    from app.platform.identity.execution_context import compose_command_context
    from app.platform.school_facts.f2_policy import F2FactScope

    guard, _, _ = _guard({"f2_person_reader": F2FactScope(SCHOOL, "person", coverage="school")})
    authorizer = F2OwnResultAuthorizer(guard)
    context = compose_command_context(actor(), UUID(int=90), "read-own-school")
    selected = authorizer.for_targets(context, frozenset({("person", "future:person")}))
    assert selected.allowed_targets == frozenset({("person", "future:person")})
    selected.revalidate()
    with pytest.raises(RuntimeError):
        authorizer(context)
