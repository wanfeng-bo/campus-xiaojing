"""F6 exact fact capabilities; facilities never imply student roster access."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from app.platform.authorization.thin_contracts import (
    ModuleAuthorizationRequirement,
    ThinAuthorizationRequest,
)
from app.platform.contracts import StaffActorContext
from app.platform.governance.contracts import ScopeResolutionRequest, ScopeResolutionSnapshot

F6_SCOPE_OWNER = "school_facts.f6"
F6_SCOPE_SCHEMA = "exact_residence_fact_set"
F6_FACT_FIELDS: dict[str, Any] = {
    "dormitory_building_profile": frozenset(
        {
            "location_source_key",
            "usage_kind",
            "display_name",
            "description",
            "is_available",
            "valid_to",
        }
    ),
    "dorm_room": frozenset(
        {
            "room_source_key",
            "authorized_bed_capacity",
            "accommodation_zone",
            "is_available",
            "accessibility_tags",
            "valid_to",
            "room_name",
            "building_name",
        }
    ),
    "dorm_bed": frozenset(
        {
            "room_source_key",
            "business_code",
            "display_name",
            "availability",
            "valid_to",
            "room_name",
            "building_name",
        }
    ),
    "student_bed_residence": frozenset(
        {
            "student_source_key",
            "bed_source_key",
            "effective_at",
            "valid_to",
            "student_name",
            "bed_name",
            "room_name",
            "building_name",
            "residence_mode",
            "enrollment_status",
        }
    ),
}
F6_FACT_KINDS = frozenset(F6_FACT_FIELDS)
F6_OPERATIONS = frozenset(
    f"{F6_SCOPE_OWNER}.{kind}.{action}" for kind in F6_FACT_KINDS for action in ("read", "maintain")
)


@dataclass(frozen=True, slots=True)
class F6FactScope:
    school_id: UUID
    fact_kind: str
    object_keys: tuple[str, ...] = ()
    creation_keys: tuple[str, ...] = ()
    field_names: tuple[str, ...] = ()
    quality_owners: tuple[tuple[str, str], ...] = ()
    reserved_read_keys: tuple[str, ...] = ()
    coverage: Literal["exact", "school"] = "exact"

    def __post_init__(self) -> Any:
        if (
            self.coverage not in {"exact", "school"}
            or self.coverage == "school"
            and bool(
                self.object_keys
                or self.creation_keys
                or self.quality_owners
                or self.reserved_read_keys
            )
            or type(self.school_id) is not UUID
            or self.fact_kind not in F6_FACT_KINDS
            or self.coverage == "exact"
            and not (
                self.object_keys
                or self.creation_keys
                or self.quality_owners
                or self.reserved_read_keys
            )
            or not self.field_names
            or len(self.object_keys)
            + len(self.creation_keys)
            + len(self.quality_owners)
            + len(self.reserved_read_keys)
            > 100
            or set(self.object_keys) & set(self.creation_keys)
            or set(self.reserved_read_keys) & (set(self.object_keys) | set(self.creation_keys))
            or not set(self.field_names) <= F6_FACT_FIELDS[self.fact_kind]
        ):
            raise ValueError("Exact nonempty F6 objects and owned fields required")
        for name in ("object_keys", "creation_keys", "field_names", "reserved_read_keys"):
            values = getattr(self, name)
            if (
                type(values) is not tuple
                or len(set(values)) != len(values)
                or any(
                    type(v) is not str
                    or not v
                    or v != v.strip()
                    or len(v) > 255
                    or "*" in v
                    or "?" in v
                    for v in values
                )
            ):
                raise ValueError("Finite unique F6 scope values required")
            object.__setattr__(self, name, tuple(sorted(values)))
        owner_kinds = (
            {"person"}
            if self.fact_kind == "student_bed_residence"
            else {"location", "school"}
            if self.fact_kind == "dormitory_building_profile"
            else {"location"}
        )
        if (
            type(self.quality_owners) is not tuple
            or any(
                type(p) is not tuple
                or len(p) != 2
                or p[0] not in owner_kinds
                or type(p[1]) is not str
                or not p[1]
                or p[1] != p[1].strip()
                or len(p[1]) > 255
                or "*" in p[1]
                or "?" in p[1]
                for p in self.quality_owners
            )
            or len(set(self.quality_owners)) != len(self.quality_owners)
        ):
            raise ValueError("Finite exact F6 quality owner coordinates required")
        object.__setattr__(self, "quality_owners", tuple(sorted(self.quality_owners)))

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
    def readable_keys(self) -> tuple[str, ...]:
        return tuple(sorted((*self.object_keys, *self.reserved_read_keys)))

    @property
    def document(self) -> Any:
        result: dict[str, Any] = {
            "school_id": str(self.school_id),
            "fact_kind": self.fact_kind,
            "object_keys": list(self.object_keys),
            "creation_keys": list(self.creation_keys),
            "field_names": list(self.field_names),
        }
        if self.is_school_wide:
            result["coverage"] = "school"
        if self.quality_owners:
            result["quality_owners"] = [list(pair) for pair in self.quality_owners]
        if self.reserved_read_keys:
            result["reserved_read_keys"] = list(self.reserved_read_keys)
        return result

    @property
    def reference(self) -> Any:
        return json.dumps(self.document, sort_keys=True, ensure_ascii=False, separators=(",", ":"))

    @property
    def document_sha256(self) -> Any:
        return hashlib.sha256(self.reference.encode()).hexdigest()

    @classmethod
    def from_document(cls, document: Any) -> Any:
        coverage: Literal["exact", "school"] = "exact"
        if type(document) is dict and "coverage" in document:
            if document["coverage"] != "school":
                raise ValueError("Only explicit school coverage may extend the exact document.")
            coverage = "school"
            document = {key: value for key, value in document.items() if key != "coverage"}

        required: set[Any] = {
            "school_id",
            "fact_kind",
            "object_keys",
            "creation_keys",
            "field_names",
        }
        if (
            type(document) is not dict
            or not required <= set(document)
            or set(document) - required - {"quality_owners", "reserved_read_keys"}
        ):
            raise ValueError("Invalid exact F6 scope")
        if any(
            type(document[key]) is not list
            for key in ("object_keys", "creation_keys", "field_names")
        ):
            raise ValueError("Invalid finite scope lists")
        owners = document.get("quality_owners", [])
        reserved = document.get("reserved_read_keys", [])
        if type(reserved) is not list:
            raise ValueError("Invalid finite reserved read keys")
        if type(owners) is not list or any(
            type(pair) is not list or len(pair) != 2 for pair in owners
        ):
            raise ValueError("Invalid F6 quality owners")
        return cls(
            UUID(document["school_id"]),
            document["fact_kind"],
            tuple(document["object_keys"]),
            tuple(document["creation_keys"]),
            tuple(document["field_names"]),
            tuple(tuple(pair) for pair in owners),
            tuple(reserved),
            coverage=coverage,
        )

    @classmethod
    def from_reference(cls, reference: Any) -> Any:
        if type(reference) is not str or len(reference) > 32000:
            raise ValueError("Invalid F6 scope reference")
        return cls.from_document(json.loads(reference))


class F6FactPolicy:
    module_name = F6_SCOPE_OWNER
    version = 1

    def requirement_for(self, request: ThinAuthorizationRequest) -> Any:
        if (
            type(request.actor) is not StaffActorContext
            or request.module_name != self.module_name
            or request.resource_id != request.actor.school_id
            or request.resource_revision != request.actor.authorization_version
            or request.action_code != request.resource_type
            or request.action_code not in F6_OPERATIONS
        ):
            return None
        _, _, kind, action = request.action_code.split(".")
        return ModuleAuthorizationRequirement(
            f"f6_{kind}_{'reader' if action == 'read' else 'maintainer'}", f"f6_{kind}_{action}", 1
        )


class F6ScopeResolver:
    def __init__(self, *, lookup_scope: Callable[[F6FactScope, datetime], bool]) -> None:
        self.lookup_scope = lookup_scope

    def resolve(self, request: ScopeResolutionRequest) -> Any:
        try:
            if (
                request.schema_owner != F6_SCOPE_OWNER
                or request.schema_name != F6_SCOPE_SCHEMA
                or request.schema_version != 1
                or request.as_of.utcoffset() is None
            ):
                return None
            scope = F6FactScope.from_document(request.reference_document)
            if (
                scope.school_id != request.school_id
                or scope.document_sha256 != request.reference_sha256
                or not self.lookup_scope(scope, request.as_of)
            ):
                return None
            return ScopeResolutionSnapshot(
                request.school_id, F6_SCOPE_OWNER, scope.reference, 1, True, False, None
            )
        except (KeyError, ValueError, TypeError):
            return None
