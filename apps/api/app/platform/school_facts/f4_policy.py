"""Exact F4 education authority scopes; no implicit descendants or wildcards."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, cast
from uuid import UUID

from app.domains.students.education_contracts import (
    F4_EDUCATION_CONTENT_FIELDS as F4_FACT_FIELDS,
)
from app.domains.students.education_contracts import (
    F4_EDUCATION_KINDS as F4_FACT_KINDS,
)
from app.platform.authorization.thin_contracts import (
    ModuleAuthorizationRequirement,
    ThinAuthorizationRequest,
)
from app.platform.contracts import StaffActorContext
from app.platform.governance.contracts import ScopeResolutionRequest, ScopeResolutionSnapshot

F4_SCOPE_OWNER = "school_facts.f4"
F4_SCOPE_SCHEMA = "exact_education_fact_set"

F4_OPERATIONS = frozenset(
    f"school_facts.f4.{kind}.{purpose}"
    for kind in F4_FACT_KINDS
    for purpose in ("read", "maintain")
)


@dataclass(frozen=True, slots=True)
class F4FactScope:
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
            or self.fact_kind not in F4_FACT_KINDS
            or type(self.object_keys) is not tuple
            or type(self.creation_keys) is not tuple
            or type(self.field_names) is not tuple
            or self.coverage == "exact"
            and not (self.object_keys or self.creation_keys)
            or not self.field_names
            or len(self.object_keys) + len(self.creation_keys) > 100
            or set(self.object_keys) & set(self.creation_keys)
            or not set(self.field_names) <= F4_FACT_FIELDS[self.fact_kind]
        ):
            raise ValueError("Exact nonempty F4 object and field scope required.")
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
                raise ValueError("Finite unique F4 scope values required.")
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
    def from_document(cls, document: dict[str, object]) -> F4FactScope:
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
            raise ValueError("Invalid exact F4 scope document.")
        for name in ("object_keys", "creation_keys", "field_names"):
            if type(document[name]) is not list:
                raise ValueError("Invalid exact F4 scope document.")
        return cls(
            UUID(str(document["school_id"])),
            str(document["fact_kind"]),
            tuple(cast(list[str], document["object_keys"])),
            tuple(cast(list[str], document["creation_keys"])),
            tuple(cast(list[str], document["field_names"])),
            coverage=coverage,
        )

    @classmethod
    def from_reference(cls, reference: str) -> F4FactScope:
        if type(reference) is not str or len(reference) > 32000:
            raise ValueError("Invalid F4 scope reference.")
        return cls.from_document(json.loads(reference))


class F4FactPolicy:
    module_name = F4_SCOPE_OWNER
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
            or request.action_code not in F4_OPERATIONS
        ):
            return None
        _, _, kind, action = request.action_code.split(".")
        duty = f"f4_{kind}_{'reader' if action == 'read' else 'maintainer'}"
        return ModuleAuthorizationRequirement(duty, f"f4_{kind}_{action}", 1)


class F4ScopeResolver:
    """Resolve one exact owner document after the fact owner validates its current keys."""

    def __init__(
        self,
        *,
        lookup_scope: Callable[[F4FactScope, datetime], bool],
        lookup_scope_many: Callable[
            [tuple[F4FactScope, ...], tuple[datetime, ...]], tuple[bool, ...]
        ]
        | None = None,
    ) -> None:
        self._lookup_scope = lookup_scope
        self._lookup_scope_many = lookup_scope_many

    def resolve_many(
        self, requests: tuple[ScopeResolutionRequest, ...]
    ) -> tuple[ScopeResolutionSnapshot | None, ...]:
        """Batch only current owner IO; validate every complete scope document."""
        if len(requests) > 128:
            raise ValueError("Scope batch exceeds the fixed bound.")
        if self._lookup_scope_many is None:
            return tuple(self.resolve(request) for request in requests)
        valid: list[tuple[int, F4FactScope, ScopeResolutionRequest]] = []
        results: list[ScopeResolutionSnapshot | None] = [None] * len(requests)
        for index, request in enumerate(requests):
            try:
                if (
                    request.schema_owner != F4_SCOPE_OWNER
                    or request.schema_name != F4_SCOPE_SCHEMA
                    or request.schema_version != 1
                    or request.as_of.utcoffset() is None
                ):
                    continue
                scope = F4FactScope.from_document(request.reference_document)
                if (
                    scope.school_id == request.school_id
                    and scope.document_sha256 == request.reference_sha256
                ):
                    valid.append((index, scope, request))
            except (KeyError, TypeError, ValueError):
                continue
        if valid:
            checks = self._lookup_scope_many(
                tuple(scope for _, scope, _ in valid),
                tuple(request.as_of for _, _, request in valid),
            )
            if len(checks) != len(valid) or any(type(check) is not bool for check in checks):
                raise ValueError("Scope owner returned an invalid batch.")
            for (index, scope, request), allowed in zip(valid, checks, strict=True):
                if allowed:
                    results[index] = ScopeResolutionSnapshot(
                        request.school_id, F4_SCOPE_OWNER, scope.reference, 1, True, False, None
                    )
        return tuple(results)

    def resolve(self, request: ScopeResolutionRequest) -> ScopeResolutionSnapshot | None:
        try:
            if (
                request.schema_owner != F4_SCOPE_OWNER
                or request.schema_name != F4_SCOPE_SCHEMA
                or request.schema_version != 1
                or request.as_of.utcoffset() is None
            ):
                return None
            scope = F4FactScope.from_document(request.reference_document)
            if (
                scope.school_id != request.school_id
                or scope.document_sha256 != request.reference_sha256
                or not self._lookup_scope(scope, request.as_of)
            ):
                return None
            return ScopeResolutionSnapshot(
                request.school_id, F4_SCOPE_OWNER, scope.reference, 1, True, False, None
            )
        except (KeyError, TypeError, ValueError):
            return None


__all__ = [
    "F4_FACT_FIELDS",
    "F4_FACT_KINDS",
    "F4_OPERATIONS",
    "F4_SCOPE_OWNER",
    "F4_SCOPE_SCHEMA",
    "F4FactPolicy",
    "F4FactScope",
    "F4ScopeResolver",
]
