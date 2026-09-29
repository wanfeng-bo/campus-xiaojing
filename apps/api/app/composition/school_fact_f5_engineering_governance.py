"""Finite F5 deployment approval over the existing Governance confirmation/receipt flow."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from hashlib import sha256
from typing import Any, ClassVar
from uuid import UUID

from app.composition.school_fact_engineering_governance import (
    EngineeringGovernanceService,
)
from app.composition.school_fact_engineering_governance import (
    governance_command_digest as f5_governance_command_digest,
)
from app.composition.school_fact_engineering_governance import (
    governance_command_payload as f5_governance_command_payload,
)
from app.domains.students.education_contracts import F4_EDUCATION_KINDS
from app.domains.students.teaching_contracts import TEACHING_KINDS
from app.platform.governance.contracts import GovernanceCommand, GovernanceWriteAuthorizationRequest
from app.platform.school_facts.f2_policy import F2_FACT_KINDS, F2FactScope
from app.platform.school_facts.f3_policy import F3_FACT_KINDS, F3FactScope

__all__ = [
    "F5_ALLOWED_DUTIES",
    "F5EngineeringApproval",
    "F5EngineeringCommandAuthorizer",
    "F5EngineeringGovernanceService",
    "GrantReferenceProof",
    "parse_scope",
    "f5_governance_command_digest",
    "f5_governance_command_payload",
    "dependency_maintenance_allowed",
]

F5_ALLOWED_DUTIES = (
    frozenset(
        f"f5_{kind}_{purpose}" for kind in TEACHING_KINDS for purpose in ("reader", "maintainer")
    )
    | frozenset(f"f2_{kind}_reader" for kind in F2_FACT_KINDS)
    | frozenset(f"f3_{kind}_reader" for kind in F3_FACT_KINDS)
    | frozenset(
        f"f4_{kind}_{purpose}"
        for kind in F4_EDUCATION_KINDS
        for purpose in ("reader", "maintainer")
    )
    | {
        "f3_student_school_relationship_maintainer",
        "f2_policy_scope_maintainer",
        "f2_fact_quality_maintainer",
        "f2_location_maintainer",
        "f2_organization_maintainer",
        "f2_organization_membership_maintainer",
        "f2_organization_campus_relationship_maintainer",
        "f2_official_contact_maintainer",
        "f2_person_maintainer",
        "f2_assignment_maintainer",
        "f2_policy_maintainer",
        "f2_policy_version_maintainer",
        "f3_person_maintainer",
        "f3_student_maintainer",
        "f3_assignment_maintainer",
    }
)


def dependency_maintenance_allowed(
    family: str,
    kind: str,
    object_keys: tuple[str, ...],
    creation_keys: tuple[str, ...],
    *,
    verified_scope_sha256: str | None = None,
    reviewed_object_keys: tuple[str, ...] = (),
) -> bool:
    """Only the existing owners needed by approved F5 lifecycle/policy/quality changes."""
    if family == "f4" and kind in F4_EDUCATION_KINDS:
        keys = object_keys + creation_keys
        return bool(keys) and (
            all(key.startswith(f"f5:{kind}:") for key in keys)
            or (
                bool(object_keys)
                and not creation_keys
                and (
                    verified_scope_sha256 is not None
                    or (
                        bool(reviewed_object_keys) and set(reviewed_object_keys) == set(object_keys)
                    )
                )
            )
        )
    if family == "f3" and kind == "student_school_relationship":
        return bool(object_keys or creation_keys) and all(
            key.startswith(f"f5:{kind}:") for key in creation_keys
        )
    if family == "f2" and kind in {
        "policy",
        "policy_version",
        "policy_scope",
        "fact_quality",
        "location",
        "organization",
        "organization_membership",
        "organization_campus_relationship",
        "official_contact",
        "person",
        "assignment",
    }:
        keys = object_keys + creation_keys
        return bool(keys) and all(key.startswith(f"f5:{kind}:") for key in keys)
    if family == "f3" and kind in {"person", "student", "assignment"}:
        keys = object_keys + creation_keys
        return bool(keys) and all(key.startswith(f"f5:{kind}:") for key in keys)
    return False


def parse_scope(owner: str, document: dict[str, object]) -> Any:
    if document.get("coverage") is not None:
        raise ValueError("Engineering approval cannot grant live school coverage")
    if owner == "school_facts.f2":
        return F2FactScope.from_document(document)
    if owner == "school_facts.f3":
        return F3FactScope.from_document(document)
    if owner == "school_facts.f4":
        from app.platform.school_facts.f4_policy import F4FactScope

        return F4FactScope.from_document(document)
    if owner == "school_facts.f5":
        from app.platform.school_facts.f5_policy import F5FactScope

        return F5FactScope.from_document(document)
    raise ValueError("Unapproved scope owner")


@dataclass(frozen=True, slots=True)
class GrantReferenceProof:
    duty_id: UUID
    duty_version: int
    duty_code: str
    scope_id: UUID
    scope_version: int
    scope_owner: str
    scope_sha256: str


@dataclass(frozen=True, slots=True)
class F5EngineeringApproval:
    allowed_duties: ClassVar[frozenset[str]] = F5_ALLOWED_DUTIES
    school_id: UUID
    actor_reference: str
    approval_reference: str
    valid_from: datetime
    valid_to: datetime
    grant_epoch: datetime
    grant_valid_to: datetime
    command_digests: frozenset[str]
    duty_codes: frozenset[str]
    assignment_ids: frozenset[UUID]
    grant_references: tuple[GrantReferenceProof, ...] = ()

    def __post_init__(self) -> None:
        times = (self.valid_from, self.valid_to, self.grant_epoch, self.grant_valid_to)
        if (
            type(self.school_id) is not UUID
            or not self.actor_reference.strip()
            or not self.approval_reference.strip()
            or any(value.utcoffset() is None for value in times)
            or not timedelta(0) < self.valid_to - self.valid_from <= timedelta(minutes=15)
            or self.grant_valid_to != self.grant_epoch + timedelta(days=7)
            or not self.grant_epoch <= self.valid_from < self.valid_to <= self.grant_valid_to
            or not self.command_digests
            or not self.assignment_ids
            or not self.duty_codes
            or not self.duty_codes <= self.allowed_duties
            or any(
                len(d) != 64 or any(c not in "0123456789abcdef" for c in d)
                for d in self.command_digests
            )
        ):
            raise ValueError("Exact F5 approval, short window and fixed seven-day epoch required")


class F5EngineeringCommandAuthorizer:
    actor_kind = "f5_engineering"
    basis_authority = "f5_engineering_policy"
    own_scope_owner = "school_facts.f5"
    parse_scope = staticmethod(parse_scope)
    dependency_maintenance_allowed = staticmethod(dependency_maintenance_allowed)
    scope_schemas = {
        "school_facts.f2": "exact_fact_set",
        "school_facts.f3": "exact_family_fact_set",
        "school_facts.f4": "exact_education_fact_set",
        "school_facts.f5": "exact_teaching_fact_set",
    }

    def __init__(
        self,
        *,
        approval: F5EngineeringApproval,
        clock: Callable[[], datetime],
        approval_is_current: Callable[[], bool],
        grant_reference_is_current: Callable[[GrantReferenceProof], bool] | None = None,
        expected_database_role: str = "campus_ai_governance_writer",
    ) -> None:
        self.approval = approval
        self.clock = clock
        self.approval_is_current = approval_is_current
        self.grant_reference_is_current = grant_reference_is_current
        self.expected_database_role = expected_database_role

    def authorize_command(self, command: GovernanceCommand) -> bool:
        try:
            payload = f5_governance_command_payload(command)
            name = json.loads(payload)["command_name"]
            return self.authorize(
                GovernanceWriteAuthorizationRequest(
                    command.school_id,
                    name,
                    name.split(".")[1],
                    getattr(command, "grant_id", None),
                    command.actor,
                    payload,
                    sha256(payload.encode()).hexdigest(),
                    sha256(command.idempotency_key.encode()).hexdigest(),
                )
            )
        except (KeyError, TypeError, ValueError, RuntimeError):
            return False

    def _proof(self, payload: dict[str, Any]) -> GrantReferenceProof | None:
        matches = [
            p
            for p in self.approval.grant_references
            if str(p.duty_id) == payload["duty_id"]
            and p.duty_version == payload["duty_version"]
            and str(p.scope_id) == payload["scope_id"]
            and p.scope_version == payload["scope_version"]
        ]
        return matches[0] if len(matches) == 1 else None

    def authorize(self, request: GovernanceWriteAuthorizationRequest) -> bool:
        a, actor = self.approval, request.actor
        try:
            if not self.approval_is_current():
                return False
            digest = sha256(
                (request.command_payload_json + "|" + request.idempotency_key_sha256).encode()
            ).hexdigest()
            if not (
                a.valid_from <= self.clock() < a.valid_to
                and request.school_id == actor.school_id == a.school_id
                and actor.actor_kind == self.actor_kind
                and actor.actor_reference == a.actor_reference
                and actor.actor_assignment_id is None
                and actor.actor_assignment_version is None
                and digest in a.command_digests
                and sha256(request.command_payload_json.encode()).hexdigest()
                == request.command_sha256
            ):
                return False
            bound = json.loads(request.command_payload_json)
            payload = bound["payload"]
            if bound["command_name"] != request.command_name:
                return False
            effective = payload.get("effective_from", payload.get("effective_at"))
            if not a.valid_from <= datetime.fromisoformat(effective) < a.valid_to:
                return False
            if request.command_name == "governance.duty.create":
                return payload["duty_code"] in a.duty_codes
            if request.command_name == "governance.scope.create":
                owner = payload["schema_owner"]
                scope = self.parse_scope(owner, payload["reference_document"])
                schemas = self.scope_schemas
                return (
                    scope.school_id == a.school_id
                    and payload["schema_name"] == schemas[owner]
                    and payload["schema_version"] == 1
                    and scope.document_sha256 == payload["reference_sha256"]
                    and (
                        owner == self.own_scope_owner
                        or not scope.creation_keys
                        or (
                            f"{owner.rsplit('.', 1)[1]}_{scope.fact_kind}_maintainer"
                            in a.duty_codes
                            and self.dependency_maintenance_allowed(
                                owner.rsplit(".", 1)[1],
                                scope.fact_kind,
                                scope.object_keys,
                                scope.creation_keys,
                            )
                        )
                    )
                )
            if request.command_name == "governance.grant.create":
                proof = self._proof(payload)
                return (
                    proof is not None
                    and proof.duty_code in a.duty_codes
                    and UUID(payload["assignment_id"]) in a.assignment_ids
                    and payload["basis_authority"] == self.basis_authority
                    and payload["basis_reference"] == a.approval_reference
                    and a.valid_from <= datetime.fromisoformat(payload["valid_from"]) < a.valid_to
                    and datetime.fromisoformat(payload["planned_valid_to"]) == a.grant_valid_to
                )
            return request.command_name == "governance.grant.revoke" and request.target_id == UUID(
                payload["grant_id"]
            )
        except (KeyError, TypeError, ValueError):
            return False

    def authorize_session(
        self, repository: Any, request: GovernanceWriteAuthorizationRequest
    ) -> None:
        allowed = repository.session_user() == self.expected_database_role and self.authorize(
            request
        )
        if allowed and request.command_name == "governance.grant.create":
            proof = self._proof(json.loads(request.command_payload_json)["payload"])
            allowed = (
                proof is not None
                and self.grant_reference_is_current is not None
                and self.grant_reference_is_current(proof)
            )
        if not allowed:
            raise PermissionError(
                "F5 execution or authoritative duty/scope binding is not approved"
            )

    authorize_mutation = authorize_session
    authorize_replay = authorize_session


class F5EngineeringGovernanceService(EngineeringGovernanceService):
    command_authorizer_type = F5EngineeringCommandAuthorizer

    def __init__(
        self,
        *,
        writer: Any,
        approval: F5EngineeringApproval,
        credential: str,
        clock: Callable[[], datetime],
        approval_is_current: Callable[[], bool],
    ) -> None:
        super().__init__(
            writer=writer,
            approval=approval,  # type: ignore[arg-type]
            credential=credential,
            clock=clock,
            approval_is_current=approval_is_current,
            command_authorizer=self.command_authorizer_type(
                approval=approval, clock=clock, approval_is_current=approval_is_current
            ),
        )
