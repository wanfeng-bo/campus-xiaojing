from datetime import UTC, datetime
from typing import cast
from uuid import UUID, uuid4

import pytest

import app.platform.contracts as platform_contracts
from app.platform.authorization.thin_contracts import ThinAuthorizationRequest
from app.platform.contracts import AssignmentId, PersonId, SchoolId, StaffActorContext
from app.platform.governance.contracts import ScopeResolutionRequest
from app.platform.school_facts.f3_policy import F3FactPolicy, F3FactScope, F3ScopeResolver


def actor() -> StaffActorContext:
    issuer = cast(object, platform_contracts._issue_staff_actor_context)
    return issuer(  # type: ignore[operator]
        account_user_id=1,
        school_id=SchoolId(UUID(int=1)),
        person_id=PersonId(UUID(int=2)),
        assignment_id=AssignmentId(UUID(int=3)),
        assignment_version=1,
        authorization_version=2,
    )


def request(action: str) -> ThinAuthorizationRequest:
    a = actor()
    return ThinAuthorizationRequest(
        a,
        "school_facts.f3",
        action,
        uuid4(),
        action,
        a.school_id,
        a.authorization_version,
        uuid4(),
        datetime(2026, 9, 13, tzinfo=UTC),
    )


def test_f3_policy_separates_guardian_recording_and_verification_duties():
    policy = F3FactPolicy()
    assert (
        policy.requirement_for(request("school_facts.f3.guardian_relationship.request")).duty_code
        == "f3_guardian_relationship_recorder"
    )
    assert (
        policy.requirement_for(request("school_facts.f3.guardian_relationship.verify")).duty_code
        == "f3_guardian_relationship_verifier"
    )


def test_f3_scope_requires_explicit_known_fields_and_finite_keys():
    with pytest.raises(ValueError):
        F3FactScope(UUID(int=1), "student", object_keys=("student:a",), field_names=())
    with pytest.raises(ValueError):
        F3FactScope(UUID(int=1), "student", object_keys=("*",), field_names=("status",))
    with pytest.raises(ValueError):
        F3FactScope(UUID(int=1), "student", object_keys=("student:a",), field_names=("private",))


def test_f2_action_is_not_accepted_by_f3_policy():
    assert F3FactPolicy().requirement_for(request("school_facts.f2.student.read")) is None


def test_f3_scope_resolver_preserves_formal_terminal_object_coordinate():
    school = UUID(int=1)
    scope = F3FactScope(
        school,
        "guardian_relationship",
        object_keys=("guardian:ended-but-formal",),
        field_names=("status",),
    )
    seen = []

    def formal_coordinate_is_current(value, checked_at):
        seen.append((value, checked_at))
        # The owner lookup has verified a formal coordinate. Business status is
        # deliberately absent from the governance scope contract.
        return True

    resolved = F3ScopeResolver(lookup_scope=formal_coordinate_is_current).resolve(
        ScopeResolutionRequest(
            school,
            uuid4(),
            "school_facts.f3",
            "exact_family_fact_set",
            1,
            scope.document,
            scope.document_sha256,
            datetime(2026, 9, 13, tzinfo=UTC),
        )
    )
    assert resolved is not None and seen[0][0] == scope


@pytest.mark.parametrize("reason", ["missing", "wrong_school", "damaged"])
def test_f3_scope_resolver_rejects_nonformal_or_invalid_coordinate(reason):
    school = UUID(int=1)
    scope = F3FactScope(
        school,
        "student",
        object_keys=("student:invalid",),
        field_names=("status",),
    )
    request = ScopeResolutionRequest(
        school,
        uuid4(),
        "school_facts.f3",
        "exact_family_fact_set",
        1,
        scope.document,
        scope.document_sha256,
        datetime(2026, 9, 13, tzinfo=UTC),
    )
    if reason == "wrong_school":
        request = ScopeResolutionRequest(
            UUID(int=9),
            request.scope_id,
            request.schema_owner,
            request.schema_name,
            request.schema_version,
            request.reference_document,
            request.reference_sha256,
            request.as_of,
        )
    elif reason == "damaged":
        request = ScopeResolutionRequest(
            request.school_id,
            request.scope_id,
            request.schema_owner,
            request.schema_name,
            request.schema_version,
            request.reference_document,
            "0" * 64,
            request.as_of,
        )
    assert F3ScopeResolver(lookup_scope=lambda *_args: False).resolve(request) is None