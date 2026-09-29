"""Exact F3 student/family authority scopes; no implicit descendants or wildcards."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, cast
from uuid import UUID

from app.platform.authorization.thin_contracts import (
    ModuleAuthorizationRequirement,
    ThinAuthorizationRequest,
)
from app.platform.contracts import StaffActorContext
from app.platform.governance.contracts import ScopeResolutionRequest, ScopeResolutionSnapshot

F3_SCOPE_OWNER = "school_facts.f3"
F3_SCOPE_SCHEMA = "exact_family_fact_set"
F3_FACT_KINDS = frozenset(
    {
        "student",
        "student_school_relationship",
        "student_contact",
        "guardian_relationship",
        "person_region",
        "person",
        "login_mapping",
        "assignment",
    }
)

F3_FACT_FIELDS: dict[str, frozenset[str]] = {
    "student": frozenset({"person_source_key", "status"}),
    "student_school_relationship": frozenset(
        {
            "valid_to",
            "student_source_key",
            "student_number",
            "enrollment_status",
            "residence_mode",
            "primary_campus_source_key",
            "administrative_campus_source_key",
        }
    ),
    "student_contact": frozenset(
        {
            "student_source_key",
            "contact_person_source_key",
            "relationship_kind",
            "priority",
            "is_emergency",
        }
    ),
    "guardian_relationship": frozenset(
        {
            "guardian_person_source_key",
            "student_source_key",
            "relationship_kind",
            "access_purpose",
            "verification_source",
            "verification_reference",
            "review_of_relationship_source_key",
            "status",
        }
    ),
    "person_region": frozenset(
        {"person_source_key", "district_code", "district_name", "region_purpose"}
    ),
    "person": frozenset({"business_code", "display_name", "person_kind", "lifecycle_status"}),
    "login_mapping": frozenset(
        {"provider_code", "provider_tenant_key", "external_subject", "person_source_key", "status"}
    ),
    "assignment": frozenset(
        {
            "person_source_key",
            "organization_source_key",
            "position_title",
            "semantic_role_code",
            "assignment_kind",
            "appointment_source",
            "approval_reference",
            "status",
            "valid_to",
        }
    ),
}

F3_OPERATIONS = frozenset(
    f"school_facts.f3.{kind}.{purpose}"
    for kind in F3_FACT_KINDS
    for purpose in ("read", "maintain")
) | frozenset(
    f"school_facts.f3.guardian_relationship.{action}"
    for action in ("request", "verify", "end", "revoke")
)


@dataclass(frozen=True, slots=True)
class F3FactScope:
    school_id: UUID
    fact_kind: str
    object_keys: tuple[str, ...] = ()
    creation_keys: tuple[str, ...] = ()
    field_names: tuple[str, ...] = ()
    coverage: Literal["exact", "school"] = "exact"

    def __post_init__(self) -> None:
        if (
            self.coverage not in {"exact", "school"}
            or self.coverage == "school"
            and bool(self.object_keys or self.creation_keys)
            or type(self.school_id) is not UUID
            or self.fact_kind not in F3_FACT_KINDS
            or type(self.object_keys) is not tuple
            or type(self.creation_keys) is not tuple
            or type(self.field_names) is not tuple
            or self.coverage == "exact"
            and not (self.object_keys or self.creation_keys)
            or not self.field_names
            or len(self.object_keys) + len(self.creation_keys) > 100
            or set(self.object_keys) & set(self.creation_keys)
            or not set(self.field_names) <= F3_FACT_FIELDS[self.fact_kind]
        ):
            raise ValueError("Exact nonempty F3 object and field scope required.")
        for values in (self.object_keys, self.creation_keys, self.field_names):
            if len(values) != len(set(values)) or any(
                type(value) is not str
                or value != value.strip()
                or not value
                or len(value) > 255
                or "*" in value
                or "?" in value
                for value in values
            ):
                raise ValueError("Finite unique F3 scope values required.")
        object.__setattr__(self, "object_keys", tuple(sorted(self.object_keys)))
        object.__setattr__(self, "creation_keys", tuple(sorted(self.creation_keys)))
        object.__setattr__(self, "field_names", tuple(sorted(self.field_names)))

    @property
    def is_school_wide(self) -> bool:
        return self.coverage == "school"

    def allows_key(self, source_key: str, *, is_creation: bool = False) -> bool:
        if (
            type(source_key) is not str
            or not source_key
            or source_key != source_key.strip()
            or len(source_key) > 255
            or "*" in source_key
            or "?" in source_key
        ):
            return False
        return self.is_school_wide or source_key in (
            self.creation_keys if is_creation else self.object_keys
        )

    @property
    def document(self) -> dict[str, object]:
        result: dict[str, object] = {
            "school_id": str(self.school_id),
            "fact_kind": self.fact_kind,
            "object_keys": list(self.object_keys),
            "creation_keys": list(self.creation_keys),
            "field_names": list(self.field_names),
        }

        if self.is_school_wide:
            result["coverage"] = "school"
        return result

    @property
    def reference(self) -> str:
        return json.dumps(self.document, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @property
    def document_sha256(self) -> str:
        return hashlib.sha256(self.reference.encode()).hexdigest()

    @classmethod
    def from_document(cls, document: dict[str, object]) -> F3FactScope:
        coverage: Literal["exact", "school"] = "exact"
        if type(document) is dict and "coverage" in document:
            if document["coverage"] != "school":
                raise ValueError("Only explicit school coverage may extend the exact document.")
            coverage = "school"
            document = {key: value for key, value in document.items() if key != "coverage"}

        if type(document) is not dict or set(document) != {
            "school_id",
            "fact_kind",
            "object_keys",
            "creation_keys",
            "field_names",
        }:
            raise ValueError("Invalid exact F3 scope document.")
        for name in ("object_keys", "creation_keys", "field_names"):
            if type(document[name]) is not list:
                raise ValueError("Invalid exact F3 scope document.")
        return cls(
            UUID(str(document["school_id"])),
            str(document["fact_kind"]),
            tuple(cast(list[str], document["object_keys"])),
            tuple(cast(list[str], document["creation_keys"])),
            tuple(cast(list[str], document["field_names"])),
            coverage=coverage,
        )

    @classmethod
    def from_reference(cls, reference: str) -> F3FactScope:
        if type(reference) is not str or len(reference) > 32000:
            raise ValueError("Invalid F3 scope reference.")
        return cls.from_document(json.loads(reference))


class F3FactPolicy:
    module_name = F3_SCOPE_OWNER
    version = 1

    def requirement_for(
        self, request: ThinAuthorizationRequest
    ) -> ModuleAuthorizationRequirement | None:
        if (
            type(request.actor) is not StaffActorContext
            or request.module_name != self.module_name
            or request.resource_id != request.actor.school_id
            or request.resource_revision != request.actor.authorization_version
            or request.action_code != request.resource_type
            or request.action_code not in F3_OPERATIONS
        ):
            return None
        _, _, kind, action = request.action_code.split(".")
        if kind == "guardian_relationship" and action == "request":
            duty = "f3_guardian_relationship_recorder"
        elif kind == "guardian_relationship" and action in {"verify", "end", "revoke"}:
            duty = "f3_guardian_relationship_verifier"
        else:
            duty = f"f3_{kind}_{'reader' if action == 'read' else 'maintainer'}"
        return ModuleAuthorizationRequirement(duty, f"f3_{kind}_{action}", 1)


class F3ScopeResolver:
    """Resolve one exact owner document after the fact owner validates its current keys."""

    def __init__(self, *, lookup_scope: Callable[[F3FactScope, datetime], bool]) -> None:
        self._lookup_scope = lookup_scope

    def resolve(self, request: ScopeResolutionRequest) -> ScopeResolutionSnapshot | None:
        try:
            if (
                request.schema_owner != F3_SCOPE_OWNER
                or request.schema_name != F3_SCOPE_SCHEMA
                or request.schema_version != 1
                or request.as_of.utcoffset() is None
            ):
                return None
            scope = F3FactScope.from_document(request.reference_document)
            if (
                scope.school_id != request.school_id
                or scope.document_sha256 != request.reference_sha256
                or not self._lookup_scope(scope, request.as_of)
            ):
                return None
            return ScopeResolutionSnapshot(
                request.school_id, F3_SCOPE_OWNER, scope.reference, 1, True, False, None
            )
        except (KeyError, TypeError, ValueError):
            return None


__all__ = [
    "F3_FACT_FIELDS",
    "F3_FACT_KINDS",
    "F3_OPERATIONS",
    "F3_SCOPE_OWNER",
    "F3_SCOPE_SCHEMA",
    "F3FactPolicy",
    "F3FactScope",
    "F3ScopeResolver",
]
