"""F2 exact family/object authority, independent of factual applicability."""

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

F2_FACT_KINDS = frozenset(
    {
        "school",
        "person",
        "organization",
        "assignment",
        "organization_campus_relationship",
        "location",
        "location_alias",
        "person_contact",
        "official_contact",
        "policy",
        "policy_version",
        "policy_scope",
        "organization_profile",
        "organization_membership",
        "organization_location",
        "person_workplace",
        "school_service",
        "fact_quality",
    }
)
F2_SCOPE_OWNER = "school_facts.f2"
F2_SCOPE_SCHEMA = "exact_fact_set"


@dataclass(frozen=True, slots=True)
class F2FactScope:
    school_id: UUID
    fact_kind: str
    object_keys: tuple[str, ...] = ()
    creation_keys: tuple[str, ...] = ()
    coverage: Literal["exact", "school"] = "exact"

    def __post_init__(self):
        if (
            self.coverage not in {"exact", "school"}
            or self.coverage == "school"
            and bool(self.object_keys or self.creation_keys)
            or type(self.school_id) is not UUID
            or self.fact_kind not in F2_FACT_KINDS
            or type(self.object_keys) is not tuple
            or type(self.creation_keys) is not tuple
            or self.coverage == "exact"
            and not (self.object_keys or self.creation_keys)
            or len(self.object_keys) + len(self.creation_keys) > 100
            or (self.fact_kind == "school" and bool(self.creation_keys))
            or set(self.object_keys) & set(self.creation_keys)
        ):
            raise ValueError("Exact nonempty F2 scope required.")
        for keys in (self.object_keys, self.creation_keys):
            if len(set(keys)) != len(keys) or any(
                type(k) is not str
                or not k.strip()
                or k != k.strip()
                or len(k) > 255
                or "*" in k
                or "?" in k
                for k in keys
            ):
                raise ValueError("Finite unique source keys required.")
        object.__setattr__(self, "object_keys", tuple(sorted(self.object_keys)))
        object.__setattr__(self, "creation_keys", tuple(sorted(self.creation_keys)))

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
        if is_creation and self.fact_kind == "school":
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
        }

        if self.is_school_wide:
            result["coverage"] = "school"
        return result

    @property
    def reference(self) -> str:
        return json.dumps(self.document, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

    @property
    def document_sha256(self) -> str:
        return hashlib.sha256(self.reference.encode("utf-8")).hexdigest()

    @classmethod
    def from_document(cls, document: dict[str, object]) -> F2FactScope:
        coverage: Literal["exact", "school"] = "exact"
        if type(document) is dict and "coverage" in document:
            if document["coverage"] != "school":
                raise ValueError("Only explicit school coverage may extend the exact document.")
            coverage = "school"
            document = {key: value for key, value in document.items() if key != "coverage"}

        if (
            type(document) is not dict
            or set(document) != {"school_id", "fact_kind", "object_keys", "creation_keys"}
            or type(document["object_keys"]) is not list
            or type(document["creation_keys"]) is not list
        ):
            raise ValueError("Invalid exact F2 scope document.")
        return cls(
            UUID(str(document["school_id"])),
            str(document["fact_kind"]),
            tuple(cast(list[str], document["object_keys"])),
            tuple(cast(list[str], document["creation_keys"])),
            coverage=coverage,
        )

    @classmethod
    def from_reference(cls, reference: str) -> F2FactScope:
        if type(reference) is not str or len(reference) > 32000:
            raise ValueError("Invalid F2 scope reference.")
        return cls.from_document(json.loads(reference))


class F2ScopeResolver:
    """Owner lookup validates existing school and existing keys at authorization time.

    Creation keys are restrictions, never presented as existing target entities.
    No display paths, implicit descendants or multi-assignment union are accepted.
    """

    def __init__(
        self,
        *,
        lookup: Callable[[UUID, str, str, datetime], bool],
        lookup_scope: Callable[[F2FactScope, datetime], bool] | None = None,
        lookup_scope_many: Callable[
            [tuple[F2FactScope, ...], tuple[datetime, ...]], tuple[bool, ...]
        ]
        | None = None,
    ):
        self.lookup = lookup
        self.lookup_scope = lookup_scope
        self.lookup_scope_many = lookup_scope_many

    def resolve_many(
        self, requests: tuple[ScopeResolutionRequest, ...]
    ) -> tuple[ScopeResolutionSnapshot | None, ...]:
        """Resolve only this bounded exact set; no owner evidence survives the call."""
        if len(requests) > 128:
            raise ValueError("Scope batch exceeds the fixed bound.")
        if self.lookup_scope_many is None:
            return tuple(self.resolve(request) for request in requests)
        valid: list[tuple[int, F2FactScope, ScopeResolutionRequest]] = []
        results: list[ScopeResolutionSnapshot | None] = [None] * len(requests)
        for index, request in enumerate(requests):
            try:
                if (
                    request.schema_owner != F2_SCOPE_OWNER
                    or request.schema_name != F2_SCOPE_SCHEMA
                    or request.schema_version != 1
                    or request.as_of.utcoffset() is None
                ):
                    continue
                scope = F2FactScope.from_document(request.reference_document)
                if (
                    scope.school_id == request.school_id
                    and scope.document_sha256 == request.reference_sha256
                ):
                    valid.append((index, scope, request))
            except (ValueError, TypeError, KeyError):
                continue
        if valid:
            checks = self.lookup_scope_many(
                tuple(scope for _, scope, _ in valid),
                tuple(request.as_of for _, _, request in valid),
            )
            if len(checks) != len(valid) or any(type(check) is not bool for check in checks):
                raise ValueError("Scope owner returned an invalid batch.")
            for (index, scope, request), allowed in zip(valid, checks, strict=True):
                if allowed:
                    results[index] = ScopeResolutionSnapshot(
                        request.school_id, F2_SCOPE_OWNER, scope.reference, 1, True, False, None
                    )
        return tuple(results)

    def resolve(self, request: ScopeResolutionRequest) -> ScopeResolutionSnapshot | None:
        try:
            if (
                request.schema_owner != F2_SCOPE_OWNER
                or request.schema_name != F2_SCOPE_SCHEMA
                or request.schema_version != 1
                or request.as_of.utcoffset() is None
            ):
                return None
            scope = F2FactScope.from_document(request.reference_document)
            if (
                scope.school_id != request.school_id
                or scope.document_sha256 != request.reference_sha256
            ):
                return None
            if self.lookup_scope is not None:
                if not self.lookup_scope(scope, request.as_of):
                    return None
            elif not self.lookup(scope.school_id, "school", "", request.as_of) or any(
                not self.lookup(scope.school_id, scope.fact_kind, key, request.as_of)
                for key in scope.object_keys
            ):
                return None
            return ScopeResolutionSnapshot(
                request.school_id, F2_SCOPE_OWNER, scope.reference, 1, True, False, None
            )
        except (ValueError, TypeError, KeyError):
            return None


class F2FactPolicy:
    module_name = F2_SCOPE_OWNER
    version = 1

    def requirement_for(
        self, request: ThinAuthorizationRequest
    ) -> ModuleAuthorizationRequirement | None:
        if (
            type(request.actor) is not StaffActorContext
            or request.module_name != self.module_name
            or request.resource_id != UUID(str(request.actor.school_id))
            or request.resource_revision != request.actor.authorization_version
            or request.action_code != request.resource_type
        ):
            return None
        prefix = "school_facts.f2."
        if not request.action_code.startswith(prefix):
            return None
        parts = request.action_code[len(prefix) :].split(".")
        if len(parts) != 2 or parts[0] not in F2_FACT_KINDS:
            return None
        kind, purpose = parts
        if purpose == "self" and kind == "person_contact":
            return ModuleAuthorizationRequirement(
                "self_person", f"f2_{kind}_{purpose}", 1, authority_kind="self_person"
            )
        if purpose not in {"read", "maintain"}:
            return None
        duty = "reader" if purpose == "read" else "maintainer"
        return ModuleAuthorizationRequirement(f"f2_{kind}_{duty}", f"f2_{kind}_{purpose}", 1)
