"""Exact F5 education authority scopes; no implicit descendants or wildcards."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, cast
from uuid import UUID

from app.domains.students.teaching_contracts import (
    TEACHING_FIELDS,
)
from app.domains.students.teaching_contracts import (
    TEACHING_KINDS as F5_FACT_KINDS,
)
from app.platform.authorization.thin_contracts import (
    ModuleAuthorizationRequirement,
    ThinAuthorizationRequest,
)
from app.platform.contracts import StaffActorContext
from app.platform.governance.contracts import ScopeResolutionRequest, ScopeResolutionSnapshot

F5_FACT_FIELDS = {kind: allowed for kind, (allowed, _) in TEACHING_FIELDS.items()}

F5_SCOPE_OWNER = "school_facts.f5"
F5_SCOPE_SCHEMA = "exact_teaching_fact_set"

F5_OPERATIONS = frozenset(
    f"school_facts.f5.{kind}.{purpose}"
    for kind in F5_FACT_KINDS
    for purpose in ("read", "maintain")
)


@dataclass(frozen=True, slots=True)
class F5FactScope:
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
            or self.fact_kind not in F5_FACT_KINDS
            or type(self.object_keys) is not tuple
            or type(self.creation_keys) is not tuple
            or type(self.field_names) is not tuple
            or self.coverage == "exact"
            and not (self.object_keys or self.creation_keys)
            or not self.field_names
            or len(self.object_keys) + len(self.creation_keys) > 100
            or set(self.object_keys) & set(self.creation_keys)
            or not set(self.field_names) <= F5_FACT_FIELDS[self.fact_kind]
        ):
            raise ValueError("Exact nonempty F5 object and field scope required.")
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
                raise ValueError("Finite unique F5 scope values required.")
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
    def from_document(cls, document: dict[str, object]) -> F5FactScope:
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
            raise ValueError("Invalid exact F5 scope document.")
        for name in ("object_keys", "creation_keys", "field_names"):
            if type(document[name]) is not list:
                raise ValueError("Invalid exact F5 scope document.")
        return cls(
            UUID(str(document["school_id"])),
            str(document["fact_kind"]),
            tuple(cast(list[str], document["object_keys"])),
            tuple(cast(list[str], document["creation_keys"])),
            tuple(cast(list[str], document["field_names"])),
            coverage=coverage,
        )

    @classmethod
    def from_reference(cls, reference: str) -> F5FactScope:
        if type(reference) is not str or len(reference) > 32000:
            raise ValueError("Invalid F5 scope reference.")
        return cls.from_document(json.loads(reference))


class F5FactPolicy:
    module_name = F5_SCOPE_OWNER
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
            or request.action_code not in F5_OPERATIONS
        ):
            return None
        _, _, kind, action = request.action_code.split(".")
        duty = f"f5_{kind}_{'reader' if action == 'read' else 'maintainer'}"
        return ModuleAuthorizationRequirement(duty, f"f5_{kind}_{action}", 1)


class F5ScopeResolver:
    """Resolve one exact owner document after the fact owner validates its current keys."""

    def __init__(
        self,
        *,
        lookup_scope: Callable[[F5FactScope, datetime], bool],
        lookup_scope_many: Callable[
            [tuple[F5FactScope, ...], tuple[datetime, ...]], tuple[bool, ...]
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
        valid: list[tuple[int, F5FactScope, ScopeResolutionRequest]] = []
        results: list[ScopeResolutionSnapshot | None] = [None] * len(requests)
        for index, request in enumerate(requests):
            try:
                if (
                    request.schema_owner != F5_SCOPE_OWNER
                    or request.schema_name != F5_SCOPE_SCHEMA
                    or request.schema_version != 1
                    or request.as_of.utcoffset() is None
                ):
                    continue
                scope = F5FactScope.from_document(request.reference_document)
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
                        request.school_id, F5_SCOPE_OWNER, scope.reference, 1, True, False, None
                    )
        return tuple(results)

    def resolve(self, request: ScopeResolutionRequest) -> ScopeResolutionSnapshot | None:
        try:
            if (
                request.schema_owner != F5_SCOPE_OWNER
                or request.schema_name != F5_SCOPE_SCHEMA
                or request.schema_version != 1
                or request.as_of.utcoffset() is None
            ):
                return None
            scope = F5FactScope.from_document(request.reference_document)
            if (
                scope.school_id != request.school_id
                or scope.document_sha256 != request.reference_sha256
                or not self._lookup_scope(scope, request.as_of)
            ):
                return None
            return ScopeResolutionSnapshot(
                request.school_id, F5_SCOPE_OWNER, scope.reference, 1, True, False, None
            )
        except (KeyError, TypeError, ValueError):
            return None


__all__ = [
    "F5_FACT_FIELDS",
    "F5_FACT_KINDS",
    "F5_OPERATIONS",
    "F5_SCOPE_OWNER",
    "F5_SCOPE_SCHEMA",
    "F5FactPolicy",
    "F5FactScope",
    "F5ScopeResolver",
]
