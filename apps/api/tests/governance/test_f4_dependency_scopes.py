import json
from uuid import uuid4

import pytest

from app.composition.school_fact_f4_engineering_cli import ScopeMatrixRow


def row(**kw):
    return ScopeMatrixRow.model_validate_json(json.dumps(kw))


def test_approved_new_teacher_creation_preserves_original_owner_and_exact_keys():
    scope = row(
        key="teacher",
        family="f3",
        fact_kind="person",
        action="maintain",
        creation_keys=["f4:person:teacher-a"],
        field_names=["display_name"],
    ).scope(uuid4())
    assert scope.creation_keys == ("f4:person:teacher-a",)
    assert not scope.object_keys


def test_new_education_dependency_read_is_distinct_from_renewal_of_old_scope():
    scope = row(
        key="campus",
        family="f2",
        fact_kind="location",
        action="read",
        object_keys=["f4:location:campus-a"],
        field_names=["display_name"],
    ).scope(uuid4())
    assert scope.object_keys == ("f4:location:campus-a",)
    with pytest.raises(ValueError):
        row(
            key="campus",
            family="f2",
            fact_kind="location",
            action="read",
            object_keys=["f2:location:existing"],
            field_names=["display_name"],
        )


def test_f4_dependency_preparation_cannot_create_unrelated_accounts_or_modify_old_staff():
    for kind in ("login_mapping", "assignment"):
        with pytest.raises(ValueError):
            row(
                key="other",
                family="f3",
                fact_kind=kind,
                action="maintain",
                object_keys=["f3:assignment:old"],
                field_names=["status"],
            )


@pytest.mark.parametrize(
    "family,kind",
    [
        ("f3", "student"),
        ("f3", "student_school_relationship"),
        ("f2", "policy"),
        ("f2", "policy_version"),
    ],
)
def test_approved_independent_synthetic_samples_keep_exact_owner_creation_scope(family, kind):
    fields = (
        []
        if kind == "policy"
        else ["enrollment_status"]
        if kind == "student_school_relationship"
        else ["status"]
    )
    result = row(
        key="sample",
        family=family,
        fact_kind=kind,
        action="maintain",
        creation_keys=[f"f4:{kind}:independent"],
        field_names=fields,
    ).scope(uuid4())
    assert result.creation_keys == (f"f4:{kind}:independent",)


def test_empty_fields_are_only_valid_for_existing_fieldless_policy_metadata_owner():
    with pytest.raises(ValueError):
        row(
            key="sample",
            family="f3",
            fact_kind="person",
            action="maintain",
            creation_keys=["f4:person:independent"],
            field_names=[],
        )
