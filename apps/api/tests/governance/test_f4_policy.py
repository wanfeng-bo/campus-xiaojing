from datetime import UTC, datetime
from importlib import import_module
from uuid import UUID, uuid4

import pytest

import app.platform.contracts as contexts
from app.platform.authorization.thin_contracts import ThinAuthorizationRequest
from app.platform.governance.contracts import ScopeResolutionRequest


def module():
    try:
        return import_module("app.platform.school_facts.f4_policy")
    except ModuleNotFoundError:
        pytest.fail("F4 exact education authorization contract is not implemented")


def test_f4_scope_rejects_implicit_objects_fields_and_foreign_fact_kinds():
    scope = module().F4FactScope
    for keys, fields, kind in (
        (("*",), ("display_name",), "education_class"),
        (("class:a",), ("private_phone",), "education_class"),
        (("class:a",), (), "education_class"),
        (("class:a",), ("display_name",), "guardian_relationship"),
    ):
        with pytest.raises(ValueError):
            scope(UUID(int=1), kind, object_keys=keys, field_names=fields)


def test_f4_scope_round_trip_keeps_creation_separate_from_read_objects():
    api = module()
    scope = api.F4FactScope(
        UUID(int=1), "education_class", object_keys=("class:a",),
        creation_keys=("class:b",), field_names=("display_name",),
    )
    assert api.F4FactScope.from_reference(scope.reference) == scope
    assert scope.object_keys == ("class:a",)
    assert scope.creation_keys == ("class:b",)
    with pytest.raises(ValueError):
        api.F4FactScope(UUID(int=1), "education_class", object_keys=("class:a",),
                        creation_keys=("class:a",), field_names=("display_name",))


def test_f4_policy_separates_read_maintenance_and_refuses_business_role_as_authority():
    api = module()
    actor = contexts._issue_staff_actor_context(
        account_user_id=1, school_id=contexts.SchoolId(UUID(int=1)),
        person_id=contexts.PersonId(UUID(int=2)), assignment_id=contexts.AssignmentId(UUID(int=3)),
        assignment_version=1, authorization_version=2,
    )
    def request(action):
        return ThinAuthorizationRequest(
            actor, api.F4_SCOPE_OWNER, action, uuid4(), action, actor.school_id,
            actor.authorization_version, uuid4(), datetime(2026, 9, 13, tzinfo=UTC),
        )
    policy = api.F4FactPolicy()
    read = policy.requirement_for(request("school_facts.f4.education_class.read"))
    maintain = policy.requirement_for(request("school_facts.f4.education_class.maintain"))
    assert read.duty_code == "f4_education_class_reader"
    assert maintain.duty_code == "f4_education_class_maintainer"
    assert policy.requirement_for(request("school_facts.f4.student_class_position.authorize")) is None
    assert policy.requirement_for(request("school_facts.f3.education_class.read")) is None


def test_f4_resolver_requires_same_school_digest_and_current_formal_coordinate():
    api = module()
    scope = api.F4FactScope(UUID(int=1), "education_class", object_keys=("class:a",),
                            field_names=("display_name",))
    request = ScopeResolutionRequest(
        UUID(int=1), uuid4(), api.F4_SCOPE_OWNER, api.F4_SCOPE_SCHEMA, 1,
        scope.document, scope.document_sha256, datetime(2026, 9, 13, tzinfo=UTC),
    )
    assert api.F4ScopeResolver(lookup_scope=lambda *_: True).resolve(request) is not None
    assert api.F4ScopeResolver(lookup_scope=lambda *_: False).resolve(request) is None
    foreign = ScopeResolutionRequest(
        UUID(int=9), request.scope_id, request.schema_owner, request.schema_name,
        request.schema_version, request.reference_document, request.reference_sha256, request.as_of,
    )
    assert api.F4ScopeResolver(lookup_scope=lambda *_: True).resolve(foreign) is None
