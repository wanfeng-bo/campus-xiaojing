from uuid import UUID, uuid4

from app.composition.school_fact_family_execution import (
    FamilyWriterDispatch,
    PartitionedOwnResultAuthorizer,
)
from app.composition.school_fact_family_settings import FamilyConfiguration
from app.platform import contracts as pc


def context():
    a = pc._issue_staff_actor_context(
        account_user_id=1,
        school_id=pc.SchoolId(UUID(int=1)),
        person_id=pc.PersonId(UUID(int=2)),
        assignment_id=pc.AssignmentId(UUID(int=3)),
        assignment_version=1,
        authorization_version=1,
    )
    return pc._issue_command_execution_context(
        actor=a, request_id=uuid4(), idempotency_key="receipt"
    )


class Proof:
    def __init__(self, targets):
        self.allowed_targets = frozenset(targets)
        self.valid = 0

    def revalidate(self):
        self.valid += 1


def test_configured_overlap_has_one_owner_and_version_input_cannot_change_owner():
    cfg = FamilyConfiguration(
        approval_reference="F3", scope_bindings=(), source_catalog={"person": ("person:f3",)}
    )
    dispatch = FamilyWriterDispatch(
        family=lambda *_: None, directory=lambda *_: None, configuration=cfg
    )
    assert dispatch.is_family("person", "person:f3") and not dispatch.is_family(
        "person", "person:f2"
    )
    for _fake_version in (0, 1, 999):
        assert dispatch.is_family("person", "person:f3")


def test_partitioned_receipt_cannot_use_directory_proof_for_family_coordinate():
    cfg = FamilyConfiguration(
        approval_reference="F3", scope_bindings=(), source_catalog={"person": ("person:f3",)}
    )
    dispatch = FamilyWriterDispatch(
        family=lambda *_: None, directory=lambda *_: None, configuration=cfg
    )
    directory = Proof({("person", "person:f3"), ("person", "person:f2")})
    family = Proof({("person", "person:f3")})
    result = PartitionedOwnResultAuthorizer(
        dispatch=dispatch, directory=lambda _: directory, family=lambda _: family
    )(context())
    assert result.allowed_targets == frozenset({("person", "person:f2"), ("person", "person:f3")})
    result.revalidate()
    assert directory.valid == 1 and family.valid == 1
    family_empty = Proof(set())
    result2 = PartitionedOwnResultAuthorizer(
        dispatch=dispatch, directory=lambda _: directory, family=lambda _: family_empty
    )(context())
    assert ("person", "person:f3") not in result2.allowed_targets
