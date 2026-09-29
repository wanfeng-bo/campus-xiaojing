"""Finite F3-only Governance approval; independent from the closed bootstrap."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from hashlib import sha256
from typing import Any, Protocol
from uuid import UUID

from app.composition.school_fact_engineering_governance import (
    EngineeringGovernanceDenied,
    EngineeringGovernanceService,
)
from app.platform.governance.contracts import (
    CreateDutyCommand,
    CreateGrantCommand,
    CreateScopeCommand,
    GovernanceCommand,
    GovernanceWriteAuthorizationRequest,
    GovernanceWriteResult,
    RevokeGrantCommand,
)
from app.platform.school_facts.f3_policy import (
    F3_SCOPE_OWNER,
    F3_SCOPE_SCHEMA,
    F3FactScope,
)


class _GovernanceSession(Protocol):
    def session_user(self) -> str: ...


_COMMAND_NAMES: dict[type[object], str] = {
    CreateDutyCommand: "governance.duty.create",
    CreateScopeCommand: "governance.scope.create",
    CreateGrantCommand: "governance.grant.create",
    RevokeGrantCommand: "governance.grant.revoke",
}


def _json(value: object) -> str:
    return json.dumps(
        value,
        default=lambda item: item.isoformat() if isinstance(item, datetime) else str(item),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def f3_governance_command_payload(command: GovernanceCommand) -> str:
    name = _COMMAND_NAMES.get(type(command))
    if name is None:
        raise ValueError("Command is outside the finite F3 Governance set.")
    payload = asdict(command)
    payload.pop("idempotency_key")
    payload["actor"].pop("request_id")
    return _json({"command_name": name, "payload": payload})


def f3_governance_command_digest(command: GovernanceCommand) -> str:
    payload = f3_governance_command_payload(command)
    key_hash = sha256(command.idempotency_key.encode()).hexdigest()
    return sha256((payload + "|" + key_hash).encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class F3EngineeringApproval:
    school_id: UUID
    actor_reference: str
    valid_from: datetime
    valid_to: datetime
    grant_valid_to: datetime
    command_digests: frozenset[str]
    duty_codes: frozenset[str]
    assignment_ids: frozenset[UUID]

    def __post_init__(self) -> None:
        if (
            type(self.school_id) is not UUID
            or not self.actor_reference.strip()
            or self.valid_from.utcoffset() is None
            or self.valid_to.utcoffset() is None
            or self.grant_valid_to.utcoffset() is None
            or not timedelta(0) < self.valid_to - self.valid_from <= timedelta(minutes=15)
            or not self.valid_from < self.grant_valid_to <= self.valid_from + timedelta(days=7)
            or not self.command_digests
            or not self.duty_codes
            or not self.assignment_ids
            or any(len(value) != 64 for value in self.command_digests)
            or any(not code.startswith("f3_") for code in self.duty_codes)
        ):
            raise ValueError("Finite exact F3 engineering approval required.")


class F3EngineeringCommandAuthorizer:
    def __init__(
        self,
        *,
        approval: F3EngineeringApproval,
        clock: Callable[[], datetime],
        approval_is_current: Callable[[], bool],
        expected_database_role: str = "campus_ai_governance_writer",
    ) -> None:
        self.approval = approval
        self.clock = clock
        self.approval_is_current = approval_is_current
        self.expected_database_role = expected_database_role

    def authorize_command(self, command: GovernanceCommand) -> bool:
        try:
            payload = f3_governance_command_payload(command)
            name = _COMMAND_NAMES[type(command)]
            request = GovernanceWriteAuthorizationRequest(
                command.school_id,
                name,
                name.split(".")[1],
                getattr(command, "grant_id", None),
                command.actor,
                payload,
                sha256(payload.encode()).hexdigest(),
                sha256(command.idempotency_key.encode()).hexdigest(),
            )
            return self.authorize(request)
        except (KeyError, TypeError, ValueError):
            return False

    def authorize(self, request: GovernanceWriteAuthorizationRequest) -> bool:
        a, actor = self.approval, request.actor
        try:
            if not self.approval_is_current():
                return False
            now = self.clock()
            digest = sha256(
                (request.command_payload_json + "|" + request.idempotency_key_sha256).encode()
            ).hexdigest()
            if not (
                a.valid_from <= now < a.valid_to
                and request.school_id == actor.school_id == a.school_id
                and actor.actor_kind == "f3_engineering"
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
            if request.command_name == "governance.duty.create":
                return payload["duty_code"] in a.duty_codes
            if request.command_name == "governance.scope.create":
                if payload["reference_document"].get("coverage") is not None:
                    return False
                scope = F3FactScope.from_document(payload["reference_document"])
                return (
                    scope.school_id == a.school_id
                    and payload["schema_owner"] == F3_SCOPE_OWNER
                    and payload["schema_name"] == F3_SCOPE_SCHEMA
                    and payload["schema_version"] == 1
                    and scope.document_sha256 == payload["reference_sha256"]
                )
            if request.command_name == "governance.grant.create":
                return (
                    UUID(payload["assignment_id"]) in a.assignment_ids
                    and payload["basis_authority"] == "f3_engineering_policy"
                    and a.valid_from
                    <= datetime.fromisoformat(payload["valid_from"])
                    < datetime.fromisoformat(payload["planned_valid_to"])
                    <= a.grant_valid_to
                )
            return request.command_name == "governance.grant.revoke"
        except (KeyError, TypeError, ValueError):
            return False

    def authorize_session(
        self, repository: _GovernanceSession, request: GovernanceWriteAuthorizationRequest
    ) -> None:
        if repository.session_user() != self.expected_database_role or not self.authorize(request):
            raise PermissionError("F3 Governance execution is not currently approved.")

    authorize_mutation = authorize_session
    authorize_replay = authorize_session


__all__ = [
    "F3EngineeringApproval",
    "F3EngineeringCommandAuthorizer",
    "f3_governance_command_digest",
    "f3_governance_command_payload",
]


class F3EngineeringGovernanceService(EngineeringGovernanceService):
    """Reuse confirmation/idempotency flow while binding the exact F3 payload contract."""

    def __init__(
        self,
        *,
        writer: Any,
        approval: F3EngineeringApproval,
        credential: str,
        clock: Callable[[], datetime],
        approval_is_current: Callable[[], bool],
    ) -> None:
        authorizer = F3EngineeringCommandAuthorizer(
            approval=approval,
            clock=clock,
            approval_is_current=approval_is_current,
        )
        super().__init__(
            writer=writer,
            approval=approval,  # type: ignore[arg-type]
            credential=credential,
            clock=clock,
            approval_is_current=approval_is_current,
            command_authorizer=authorizer,
        )

    def _request(self, command: GovernanceCommand) -> GovernanceWriteAuthorizationRequest:
        name = _COMMAND_NAMES.get(type(command))
        if name is None:
            raise EngineeringGovernanceDenied("Unsupported F3 engineering command.")
        payload = f3_governance_command_payload(command)
        return GovernanceWriteAuthorizationRequest(
            command.school_id,
            name,
            name.split(".")[1],
            getattr(command, "grant_id", None),
            command.actor,
            payload,
            sha256(payload.encode()).hexdigest(),
            sha256(command.idempotency_key.encode()).hexdigest(),
        )

    def receipt(
        self, command: GovernanceCommand, *, credential: str
    ) -> GovernanceWriteResult | None:
        self._authenticate(credential)
        request = self._request(command)
        approval = self._approval
        if (
            command.school_id != approval.school_id
            or command.actor.actor_reference != approval.actor_reference
            or f3_governance_command_digest(command) not in approval.command_digests
        ):
            raise EngineeringGovernanceDenied("Original command is outside this F3 approval.")
        return self._writer.lookup_command_result(command, command_name=request.command_name)
