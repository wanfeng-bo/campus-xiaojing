# pyright: strict

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType


@dataclass(frozen=True, slots=True)
class ReadOperation:
    code: str
    projection_name: str
    action_code: str
    module_name: str


class ReadOperationRegistry:
    def __init__(self, operations: Mapping[str, ReadOperation]) -> None:
        self._operations = MappingProxyType(dict(operations))

    @property
    def operation_codes(self) -> tuple[str, ...]:
        return tuple(self._operations)

    def require(self, operation_code: str) -> ReadOperation:
        return self._operations[operation_code]


_TEST_OPERATIONS = (
    ReadOperation(
        code="test_only.r1.self_current_assignment",
        projection_name="self_current_assignment",
        action_code="test_only.r1.self_current_assignment.read",
        module_name="test_only.r1.secure_reader",
    ),
    ReadOperation(
        code="test_only.r1.public_location",
        projection_name="public_location",
        action_code="test_only.r1.public_location.read",
        module_name="test_only.r1.secure_reader",
    ),
    ReadOperation(
        code="test_only.r1.official_office_contact",
        projection_name="official_office_contact",
        action_code="test_only.r1.official_office_contact.read",
        module_name="test_only.r1.secure_reader",
    ),
    ReadOperation(
        code="test_only.r1.restricted_policy_body",
        projection_name="restricted_policy_body",
        action_code="test_only.r1.restricted_policy_body.read",
        module_name="test_only.r1.secure_reader",
    ),
)


def production_operation_registry() -> ReadOperationRegistry:
    """Production starts empty; an unregistered read always fails before SQL."""

    return ReadOperationRegistry({})


def official_contact_operation_registry() -> ReadOperationRegistry:
    """Explicit F1 product composition; default/identity-only composition stays closed."""

    operations = (
        ReadOperation(
            code="school_facts.official_contact_campuses",
            projection_name="official_contact_campuses",
            action_code="school_facts.official_contacts.read",
            module_name="school_facts.official_contacts",
        ),
        ReadOperation(
            code="school_facts.official_contacts",
            projection_name="published_official_contacts",
            action_code="school_facts.official_contacts.read",
            module_name="school_facts.official_contacts",
        ),
        ReadOperation(
            code="school_facts.official_contact_maintenance_target",
            projection_name="official_contact_maintenance_target",
            action_code="school_facts.official_contacts.maintain",
            module_name="school_facts.official_contacts",
        ),
    )
    return ReadOperationRegistry({operation.code: operation for operation in operations})


def test_operation_registry() -> ReadOperationRegistry:
    """Return three readable test mechanisms plus one fixed pre-read denial."""

    return ReadOperationRegistry({operation.code: operation for operation in _TEST_OPERATIONS})


__all__ = [
    "official_contact_operation_registry",
    "ReadOperation",
    "ReadOperationRegistry",
    "production_operation_registry",
    "test_operation_registry",
]


def f2_operation_registry() -> ReadOperationRegistry:
    """Explicit F2 composition preserves all registered F1 operations."""
    from app.platform.school_facts.f2_policy import F2_FACT_KINDS

    f1 = official_contact_operation_registry()
    codes = (
        "school_facts.official_contact_campuses",
        "school_facts.official_contacts",
        "school_facts.official_contact_maintenance_target",
    )
    operations = {code: f1.require(code) for code in codes}
    for kind in sorted(F2_FACT_KINDS):
        for purpose in ("read", "maintain"):
            code = f"school_facts.f2.{kind}.{purpose}"
            operations[code] = ReadOperation(
                code=code,
                projection_name=f"f2_{kind}_{purpose}",
                action_code=code,
                module_name="school_facts.f2",
            )
    code = "school_facts.f2.person_contact.self"
    operations[code] = ReadOperation(
        code=code,
        projection_name="f2_person_contact_self",
        action_code=code,
        module_name="school_facts.f2",
    )
    return ReadOperationRegistry(operations)
