from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID, uuid4

import pytest

import app.platform.contracts as platform_contracts
from app.composition.school_fact_family_authority import (
    CurrentFamilyFactActorValidatorAdapter,
    F3OwnResultAuthorizer,
    FamilyAuthorityComponents,
    FamilyFactAuthorityDenied,
    FamilyFactTarget,
    StaffFamilyFactAuthorization,
    StaffFamilyFactChangeSetGuard,
)
from app.composition.school_fact_family_settings import FamilyConfiguration, FamilyScopeBinding
from app.platform.authorization.thin_service import ThinAuthorizationService
from app.platform.contracts import AssignmentId, PersonId, SchoolId, StaffActorContext
from app.platform.governance.contracts import (
    GovernanceAuthorizationBasis,
    GovernanceDependencyVersion,
)
from app.platform.school_facts.f3_policy import F3FactPolicy, F3FactScope

NOW = datetime(2026, 9, 13, 4, tzinfo=UTC)


def actor() -> StaffActorContext:
    issuer = cast(object, platform_contracts._issue_staff_actor_context)
    return issuer(  # type: ignore[operator]
        account_user_id=9,
        school_id=SchoolId(UUID(int=1)),
        person_id=PersonId(UUID(int=2)),
        assignment_id=AssignmentId(UUID(int=3)),
        assignment_version=4,
        authorization_version=5,
    )


class Governance:
    def __init__(self, scope: F3FactScope, scope_id: UUID) -> None:
        self.scope, self.scope_id, self.valid = scope, scope_id, True

    def issue(self, query):
        a = actor()
        return GovernanceAuthorizationBasis(
            a.school_id,
            a.assignment_id,
            query.duty_code,
            self.scope_id,
            uuid4(),
            1,
            a.authorization_version,
            NOW,
            NOW + timedelta(days=7),
            (
                GovernanceDependencyVersion("school_facts.assignment", str(a.assignment_id), 4),
                GovernanceDependencyVersion("school_facts.person", str(a.person_id), 1),
                GovernanceDependencyVersion(
                    "school_facts.f3.scope-target", self.scope.reference, 1
                ),
            ),
        )

    def validate(self, basis, *, as_of):
        return self.valid and as_of < NOW + timedelta(days=7)


def authority(scope: F3FactScope):
    scope_id = uuid4()
    governance = Governance(scope, scope_id)
    thin = ThinAuthorizationService(
        governance_basis_service=governance,
        module_policies={"school_facts.f3": F3FactPolicy()},
    )
    return StaffFamilyFactAuthorization(thin=thin, scope_id=scope_id), governance


def test_staff_family_authority_walks_thin_scope_and_final_revalidation():
    scope = F3FactScope(
        UUID(int=1),
        "guardian_relationship",
        object_keys=("guardian:f3-a",),
        field_names=("status", "student_source_key", "guardian_person_source_key"),
    )
    auth, governance = authority(scope)
    target = FamilyFactTarget(
        "guardian_relationship",
        "guardian:f3-a",
        ("status", "student_source_key"),
        resource_revision=5,
    )
    permit = auth.require_current(actor(), action="verify", target=target, checked_at=NOW)
    auth.final_guard(permit, actor=actor(), target=target, checked_at=NOW)
    governance.valid = False
    with pytest.raises(FamilyFactAuthorityDenied):
        auth.final_guard(permit, actor=actor(), target=target, checked_at=NOW)


def test_staff_family_school_scope_accepts_future_key_but_retains_field_boundary():
    scope = F3FactScope(
        UUID(int=1),
        "student",
        field_names=("status",),
        coverage="school",
    )
    auth, _ = authority(scope)
    target = FamilyFactTarget("student", "student:new-later", ("status",), 5, True)
    permit = auth.require_current(actor(), action="maintain", target=target, checked_at=NOW)
    auth.final_guard(permit, actor=actor(), target=target, checked_at=NOW)
    with pytest.raises(FamilyFactAuthorityDenied):
        auth.require_current(
            actor(),
            action="maintain",
            target=FamilyFactTarget("student", "student:new-later", ("person_source_key",), 5, True),
            checked_at=NOW,
        )


def test_family_components_school_scope_checks_thin_before_key_enumeration():
    from app.composition.school_fact_family_authority import FamilyAuthorityComponents

    scope = F3FactScope(UUID(int=1), "student", field_names=("status",), coverage="school")
    staff, governance = authority(scope)
    components = FamilyAuthorityComponents(
        thin=staff._thin,
        connection_authority=None,
        change_set_guard=type("Guard", (), {"scope_id": lambda _, a, op: staff._scope_id})(),
        actor_validator=cast(object, None),
    )
    permit = components.require_school_scope(actor(), "read", "student", NOW)
    assert permit.scope.is_school_wide
    components.revalidate_school_scope(permit, actor(), NOW)
    governance.valid = False
    with pytest.raises(FamilyFactAuthorityDenied):
        components.revalidate_school_scope(permit, actor(), NOW)


@pytest.mark.parametrize(
    "target",
    [
        FamilyFactTarget("guardian_relationship", "guardian:other", ("status",), 5),
        FamilyFactTarget("guardian_relationship", "guardian:f3-a", ("verification_reference",), 5),
    ],
)
def test_staff_family_authority_rejects_out_of_scope_object_or_field(target):
    scope = F3FactScope(
        UUID(int=1),
        "guardian_relationship",
        object_keys=("guardian:f3-a",),
        field_names=("status",),
    )
    auth, _ = authority(scope)
    with pytest.raises(FamilyFactAuthorityDenied):
        auth.require_current(actor(), action="verify", target=target, checked_at=NOW)


def test_family_validator_adapter_returns_domain_validation_and_never_self_authorizes():
    scope = F3FactScope(
        UUID(int=1),
        "guardian_relationship",
        creation_keys=("guardian:new",),
        field_names=(
            "guardian_person_source_key",
            "student_source_key",
            "relationship_kind",
            "access_purpose",
            "verification_source",
            "verification_reference",
            "status",
        ),
    )
    auth, _ = authority(scope)
    adapter = CurrentFamilyFactActorValidatorAdapter(auth)
    result = adapter.validate(
        actor=actor(), action="request", target="guardian:new", checked_at=NOW
    )
    assert result.actor_person_id == UUID(int=2)
    assert result.actor_assignment_id == UUID(int=3)
    assert result.scope_kind == "school"
    assert result.action == "request"


def test_staff_maintain_can_use_creation_key_but_cannot_treat_existing_object_as_creation():
    scope = F3FactScope(
        UUID(int=1),
        "person",
        object_keys=("person:old",),
        creation_keys=("person:new",),
        field_names=("display_name", "person_kind", "lifecycle_status"),
    )
    auth, _ = authority(scope)
    auth.require_current(
        actor(),
        action="maintain",
        target=FamilyFactTarget("person", "person:new", ("display_name",), 5, True),
        checked_at=NOW,
    )
    with pytest.raises(FamilyFactAuthorityDenied):
        auth.require_current(
            actor(),
            action="maintain",
            target=FamilyFactTarget("person", "person:old", ("display_name",), 5, True),
            checked_at=NOW,
        )


def test_f3_own_result_uses_only_current_read_objects_and_revalidates_revocation():
    scope = F3FactScope(
        UUID(int=1),
        "student",
        object_keys=("student:a",),
        creation_keys=("student:new",),
        field_names=("status",),
    )
    staff, governance = authority(scope)
    a = actor()
    sid = staff._scope_id
    cfg = FamilyConfiguration(
        approval_reference="F3",
        scope_bindings=(
            FamilyScopeBinding(
                assignment_id=a.assignment_id,
                operation_code="school_facts.f3.student.read",
                scope_id=sid,
            ),
        ),
        source_catalog={"student": ("student:a",)},
        creation_catalog={"student": {"student:new": "new"}},
    )
    issuer = platform_contracts._issue_command_execution_context
    context = issuer(actor=a, request_id=uuid4(), idempotency_key="own:f3")
    result = F3OwnResultAuthorizer(thin=staff._thin, configuration=cfg, clock=lambda: NOW)(context)
    assert result.allowed_targets == frozenset({("student", "student:a")})
    assert ("student", "student:new") not in result.allowed_targets
    governance.valid = False
    with pytest.raises(FamilyFactAuthorityDenied):
        result.revalidate()


def test_f3_school_own_result_requires_explicit_requested_targets():
    scope = F3FactScope(
        UUID(int=1), "student", field_names=("status",), coverage="school"
    )
    staff, _ = authority(scope)
    a = actor()
    cfg = FamilyConfiguration(
        approval_reference="F3",
        scope_bindings=(
            FamilyScopeBinding(
                assignment_id=a.assignment_id,
                operation_code="school_facts.f3.student.read",
                scope_id=staff._scope_id,
            ),
        ),
    )
    context = platform_contracts._issue_command_execution_context(
        actor=a, request_id=uuid4(), idempotency_key="own:f3-school"
    )
    guard = F3OwnResultAuthorizer(thin=staff._thin, configuration=cfg, clock=lambda: NOW)
    with pytest.raises(FamilyFactAuthorityDenied):
        guard(context)
    requested = frozenset({("student", "student:created-later")})
    result = guard(context, targets=requested)
    assert result.allowed_targets == requested


def test_f3_own_result_rejects_different_current_actor_binding():
    scope = F3FactScope(UUID(int=1), "student", object_keys=("student:a",), field_names=("status",))
    staff, _ = authority(scope)
    a = actor()
    cfg = FamilyConfiguration(
        approval_reference="F3", scope_bindings=(), source_catalog={"student": ("student:a",)}
    )
    context = platform_contracts._issue_command_execution_context(
        actor=a, request_id=uuid4(), idempotency_key="own:none"
    )
    with pytest.raises(FamilyFactAuthorityDenied):
        F3OwnResultAuthorizer(thin=staff._thin, configuration=cfg, clock=lambda: NOW)(context)


def dependency_components(*, dependency_keys=("person:old-student",)):
    primary_id, inherited_id = UUID(int=81), UUID(int=82)
    primary = Governance(
        F3FactScope(
            UUID(int=1),
            "person",
            object_keys=("person:new-student",),
            field_names=("business_code", "display_name", "lifecycle_status", "person_kind"),
        ),
        primary_id,
    )
    inherited = Governance(
        F3FactScope(
            UUID(int=1),
            "person",
            object_keys=("person:old-student", "person:unrelated"),
            field_names=("display_name", "lifecycle_status", "person_kind"),
        ),
        inherited_id,
    )

    class RoutedGovernance:
        def __init__(self):
            self.calls = []
            self.scopes = {primary_id: primary, inherited_id: inherited}

        def issue(self, query):
            self.calls.append(query.scope_id)
            source = self.scopes[query.scope_id]
            if not source.validate(None, as_of=query.as_of):
                raise RuntimeError("Current grant unavailable")
            return source.issue(query)

        def validate(self, basis, *, as_of):
            return self.scopes[basis.scope_id].validate(basis, as_of=as_of)

    configuration = FamilyConfiguration.model_validate(
        {
            "approval_reference": "F5 inherited dependency",
            "scope_bindings": [
                {
                    "assignment_id": actor().assignment_id,
                    "scope_id": primary_id,
                    "operation_code": "school_facts.f3.person.read",
                }
            ],
            "read_dependency_bindings": [
                {
                    "assignment_id": actor().assignment_id,
                    "scope_id": inherited_id,
                    "operation_code": "school_facts.f3.person.read",
                    "object_keys": dependency_keys,
                }
            ],
        }
    )
    governance = RoutedGovernance()
    thin = ThinAuthorizationService(
        governance_basis_service=governance,
        module_policies={"school_facts.f3": F3FactPolicy()},
    )
    components = FamilyAuthorityComponents(
        thin,
        None,
        StaffFamilyFactChangeSetGuard(thin=thin, configuration=configuration),
        None,
        read_dependency_bindings=configuration.read_dependency_bindings,
    )
    return components, governance, inherited


def test_inherited_person_read_uses_original_fields_without_widening_primary():
    components, governance, _ = dependency_components()
    old = components.require_scope(actor(), "read", "person", "person:old-student", False, NOW)
    new = components.require_scope(actor(), "read", "person", "person:new-student", False, NOW)
    assert old.scope_id == UUID(int=82)
    assert set(old.scope.field_names) == {"display_name", "lifecycle_status", "person_kind"}
    assert new.scope_id == UUID(int=81)
    assert "business_code" in new.scope.field_names
    assert governance.calls == [UUID(int=82), UUID(int=81)]
    with pytest.raises(FamilyFactAuthorityDenied):
        components.require_scope(actor(), "read", "person", "person:unrelated", False, NOW)
    with pytest.raises(FamilyFactAuthorityDenied):
        components.require_scope(actor(), "maintain", "person", "person:old-student", False, NOW)


@pytest.mark.parametrize("failure", ("revoked", "expired"))
def test_inherited_read_rechecks_current_grant_and_never_falls_back(failure):
    components, governance, inherited = dependency_components()
    permit = components.require_scope(actor(), "read", "person", "person:old-student", False, NOW)
    checked_at = NOW
    if failure == "revoked":
        inherited.valid = False
    else:
        checked_at += timedelta(days=7)
    with pytest.raises(FamilyFactAuthorityDenied):
        components.revalidate_permit(permit, actor(), permit.target, checked_at)
    with pytest.raises(FamilyFactAuthorityDenied):
        components.require_scope(actor(), "read", "person", "person:old-student", False, checked_at)
    assert governance.calls == [UUID(int=82), UUID(int=82)]


def test_dependency_catalog_membership_cannot_create_authority():
    components, _, _ = dependency_components(dependency_keys=("person:unapproved",))
    with pytest.raises(FamilyFactAuthorityDenied):
        components.require_scope(actor(), "read", "person", "person:unapproved", False, NOW)


@pytest.mark.parametrize("revoked", (False, True))
def test_candidate_scope_sharing_honors_each_selected_dependency_binding(revoked):
    from dataclasses import replace
    from types import SimpleNamespace

    from app.composition.school_fact_family_staff_reader import FamilyStaffRepository

    components, governance, inherited = dependency_components()
    primary = governance.scopes[UUID(int=81)]
    primary.scope = replace(primary.scope, object_keys=("person:new-student", "person:old-student"))
    inherited.valid = not revoked
    captured = []

    def candidates(**kwargs):
        kwargs["before_read"]()
        captured.extend(kwargs["source_groups"]())
        kwargs["after_read"]()
        return []

    configuration = FamilyConfiguration(
        approval_reference="F5",
        scope_bindings=(
            FamilyScopeBinding(
                assignment_id=actor().assignment_id,
                operation_code="school_facts.f3.person.read",
                scope_id=UUID(int=81),
            ),
        ),
        read_dependency_bindings=components.read_dependency_bindings,
    )
    repository = FamilyStaffRepository(
        projection=SimpleNamespace(candidates=candidates),
        configuration=configuration,
        reference_key=b"x" * 32,
        authorize_scope=components.require_scope,
        revalidate=components.revalidate_permit,
        clock=lambda: NOW,
    )
    repository._candidate_facts(
        actor=actor(),
        fact_kind="person",
        sources=("person:new-student", "person:old-student", "person:unrelated"),
        factual_at=NOW,
        match_field="display_name",
        match_value="合成姓名",
        limit=10,
        related=None,
    )
    selected = {key: fields for keys, fields in captured for key in keys}
    assert "business_code" in selected["person:new-student"]
    assert "person:unrelated" not in selected
    if revoked:
        assert "person:old-student" not in selected
    else:
        assert set(selected["person:old-student"]) == {
            "display_name",
            "lifecycle_status",
            "person_kind",
        }
    assert governance.calls == [UUID(int=81), UUID(int=82), UUID(int=81)]


@pytest.mark.parametrize("mismatch", ("school", "fact_kind", "assignment"))
def test_inherited_read_rejects_wrong_current_authority_context(mismatch):
    from dataclasses import replace

    components, _, inherited = dependency_components()
    current_actor = actor()
    if mismatch == "school":
        inherited.scope = replace(inherited.scope, school_id=UUID(int=99))
    elif mismatch == "fact_kind":
        inherited.scope = F3FactScope(
            UUID(int=1), "student", object_keys=("person:old-student",), field_names=("status",)
        )
    else:
        current_actor = platform_contracts._issue_staff_actor_context(
            account_user_id=9,
            school_id=SchoolId(UUID(int=1)),
            person_id=PersonId(UUID(int=2)),
            assignment_id=AssignmentId(UUID(int=99)),
            assignment_version=4,
            authorization_version=5,
        )
    with pytest.raises(FamilyFactAuthorityDenied):
        components.require_scope(current_actor, "read", "person", "person:old-student", False, NOW)


@pytest.mark.parametrize("invalid", ("write", "wildcard", "duplicate", "overlap"))
def test_dependency_configuration_rejects_writes_and_ambiguous_object_filters(invalid):
    from pydantic import ValidationError

    dependency = {
        "assignment_id": actor().assignment_id,
        "scope_id": UUID(int=82),
        "operation_code": "school_facts.f3.person.read",
        "object_keys": ["person:old-student"],
    }
    dependencies = [dependency]
    if invalid == "write":
        dependency["operation_code"] = "school_facts.f3.person.maintain"
    elif invalid == "wildcard":
        dependency["object_keys"] = ["person:*"]
    elif invalid == "duplicate":
        dependency["object_keys"] *= 2
    else:
        dependencies.append({**dependency, "scope_id": UUID(int=83)})
    with pytest.raises(ValidationError):
        FamilyConfiguration.model_validate(
            {
                "approval_reference": "F5",
                "scope_bindings": [],
                "read_dependency_bindings": dependencies,
            }
        )
