"""Finite, independently authenticated F2 governance execution; never bootstrap."""

from __future__ import annotations

import copy
import hmac
import json
import secrets
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from hashlib import sha256
from typing import Any, Protocol
from uuid import UUID

from app.platform.governance.contracts import (
    CreateDutyCommand,
    CreateGrantCommand,
    CreateScopeCommand,
    GovernanceCommand,
    GovernanceWriteAuthorizationRequest,
    GovernanceWriteResult,
    RevokeGrantCommand,
)

_COMMANDS: dict[type[object], tuple[str, str]] = {
    CreateDutyCommand: ("governance.duty.create", "create_duty"),
    CreateScopeCommand: ("governance.scope.create", "create_scope"),
    CreateGrantCommand: ("governance.grant.create", "create_grant"),
    RevokeGrantCommand: ("governance.grant.revoke", "revoke_grant"),
}


class EngineeringGovernanceDenied(RuntimeError):
    pass


def governance_command_payload(command: GovernanceCommand) -> str:
    if type(command) not in _COMMANDS:
        raise EngineeringGovernanceDenied("Command is outside approved F2 governance.")
    payload = asdict(command)
    payload.pop("idempotency_key")
    payload["actor"].pop("request_id")
    return json.dumps(
        {"command_name": _COMMANDS[type(command)][0], "payload": payload},
        default=lambda v: v.isoformat() if isinstance(v, datetime) else str(v),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def governance_command_digest(command: GovernanceCommand) -> str:
    return _approval_digest(
        governance_command_payload(command),
        sha256(command.idempotency_key.encode("utf-8")).hexdigest(),
    )


def _approval_digest(payload: str, key_hash: str) -> str:
    return sha256((payload + "|" + key_hash).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class EngineeringApproval:
    school_id: UUID
    actor_reference: str
    valid_from: datetime
    valid_to: datetime
    command_digests: frozenset[str]
    grant_valid_to: datetime | None = None

    def __post_init__(self):
        if (
            type(self.school_id) is not UUID
            or type(self.command_digests) is not frozenset
            or not self.actor_reference.strip()
            or self.valid_from.utcoffset() is None
            or self.valid_to.utcoffset() is None
            or self.valid_to <= self.valid_from
            or (
                self.grant_valid_to is not None
                and (
                    self.grant_valid_to.utcoffset() is None
                    or self.grant_valid_to <= self.valid_from
                )
            )
            or not self.command_digests
            or len(self.command_digests) > 1024
            or any(
                len(d) != 64 or any(c not in "0123456789abcdef" for c in d)
                for d in self.command_digests
            )
        ):
            raise ValueError("Finite explicit F2 engineering approval required.")


class EngineeringCommandAuthorizer:
    def __init__(
        self,
        *,
        approval: EngineeringApproval,
        clock: Callable[[], datetime],
        expected_database_role: str = "campus_ai_governance_writer",
        approval_is_current: Callable[[], bool] | None = None,
        command_authorizer: EngineeringCommandAuthorization | None = None,
    ):
        self.approval = approval
        self.clock = clock
        self.expected_database_role = expected_database_role
        self.approval_is_current = approval_is_current

    def authorize(self, request: GovernanceWriteAuthorizationRequest) -> bool:
        a = self.approval
        actor = request.actor
        if self.approval_is_current is not None:
            try:
                if not self.approval_is_current():
                    return False
            except Exception:
                return False
        if not (
            a.valid_from <= self.clock() < a.valid_to
            and request.school_id == a.school_id == actor.school_id
            and actor.actor_kind == "f2_engineering"
            and actor.actor_reference == a.actor_reference
            and actor.actor_assignment_id is None
            and actor.actor_assignment_version is None
            and _approval_digest(request.command_payload_json, request.idempotency_key_sha256)
            in a.command_digests
            and sha256(request.command_payload_json.encode("utf-8")).hexdigest()
            == request.command_sha256
        ):
            return False
        try:
            bound = json.loads(request.command_payload_json)
            payload = bound["payload"]
            expected_actor = asdict(actor)
            expected_actor.pop("request_id")
            expected_actor = json.loads(json.dumps(expected_actor, default=str))
            if (
                bound["command_name"] != request.command_name
                or payload["actor"] != expected_actor
                or payload["school_id"] != str(a.school_id)
            ):
                return False
            if request.target_kind != request.command_name.split(".")[1]:
                return False
            effective_at = payload.get("effective_from", payload.get("effective_at"))
            if (
                effective_at is None
                or not a.valid_from <= datetime.fromisoformat(effective_at) < a.valid_to
            ):
                return False
            if request.command_name == "governance.duty.create":
                from app.platform.school_facts.f2_policy import F2_FACT_KINDS

                return payload["duty_code"] in {
                    f"f2_{k}_{p}" for k in F2_FACT_KINDS for p in ("reader", "maintainer")
                }
            if request.command_name == "governance.scope.create":
                from app.platform.school_facts.f2_policy import F2FactScope

                if payload["reference_document"].get("coverage") is not None:
                    return False
                scope = F2FactScope.from_document(payload["reference_document"])
                return (
                    scope.school_id == a.school_id
                    and payload["schema_owner"] == "school_facts.f2"
                    and payload["schema_name"] == "exact_fact_set"
                    and payload["schema_version"] == 1
                    and scope.document_sha256 == payload["reference_sha256"]
                )
            if request.command_name == "governance.grant.create":
                return (
                    payload["basis_authority"] == "f2_engineering_policy"
                    and payload["planned_valid_to"] is not None
                    and a.valid_from
                    <= datetime.fromisoformat(payload["valid_from"])
                    < datetime.fromisoformat(payload["planned_valid_to"])
                    <= (a.grant_valid_to or a.valid_to)
                )
            if request.command_name == "governance.grant.revoke":
                return request.target_id == UUID(payload["grant_id"])
        except (KeyError, TypeError, ValueError):
            return False
        return False

    def authorize_session(
        self, repository: Any, request: GovernanceWriteAuthorizationRequest
    ) -> None:
        if repository.session_user() != self.expected_database_role:
            raise EngineeringGovernanceDenied("Wrong isolated Governance execution role.")
        if not self.authorize(request):
            raise EngineeringGovernanceDenied("Engineering command no longer approved.")

    authorize_mutation = authorize_session
    authorize_replay = authorize_session


class EngineeringCommandAuthorization(Protocol):
    def authorize(self, request: GovernanceWriteAuthorizationRequest) -> bool: ...

@dataclass(frozen=True, slots=True)
class EngineeringPreparedCommand:
    confirmation_token: str
    command_sha256: str
    command_payload_json: str
    expires_at: datetime


class EngineeringGovernanceService:
    """Server-created finite commands, explicit confirmation, original Writer receipts.

    Composition supplies a separately authenticated credential and an immutable
    approval manifest. Product staff sessions/DB role names are never credentials.
    Prepared payloads are short-lived; committed results live in Governance receipts.
    """

    def __init__(
        self,
        *,
        writer: Any,
        approval: EngineeringApproval,
        credential: str,
        clock: Callable[[], datetime],
        approval_is_current: Callable[[], bool] | None = None,
        command_authorizer: EngineeringCommandAuthorization | None = None,
    ):
        if len(credential) < 16:
            raise ValueError("Independent engineering credential required.")
        self._writer = writer
        self._approval = approval
        self._credential_digest = sha256(credential.encode()).digest()
        self._clock = clock
        self._authorizer = command_authorizer or EngineeringCommandAuthorizer(
            approval=approval, clock=clock, approval_is_current=approval_is_current
        )
        self._prepared: dict[str, tuple[GovernanceCommand, datetime]] = {}

    def _authenticate(self, credential: object) -> None:
        if not isinstance(credential, str) or not hmac.compare_digest(
            sha256(credential.encode()).digest(), self._credential_digest
        ):
            raise EngineeringGovernanceDenied("Independent engineering authentication failed.")

    def _request(self, command: GovernanceCommand) -> GovernanceWriteAuthorizationRequest:
        name = _COMMANDS.get(type(command))
        if name is None:
            raise EngineeringGovernanceDenied("Unsupported engineering command.")
        return GovernanceWriteAuthorizationRequest(
            command.school_id,
            name[0],
            name[0].split(".")[1],
            getattr(command, "grant_id", None),
            command.actor,
            command_payload_json=governance_command_payload(command),
            command_sha256=sha256(governance_command_payload(command).encode("utf-8")).hexdigest(),
            idempotency_key_sha256=sha256(command.idempotency_key.encode("utf-8")).hexdigest(),
        )

    def prepare(self, command: GovernanceCommand, *, credential: str) -> EngineeringPreparedCommand:
        self._authenticate(credential)
        command = copy.deepcopy(command)
        request = self._request(command)
        if not self._authorizer.authorize(request):
            raise EngineeringGovernanceDenied("Exact command is not approved.")
        now = self._clock()
        self._prepared = {k: v for k, v in self._prepared.items() if now < v[1]}
        if len(self._prepared) >= 1024:
            raise EngineeringGovernanceDenied("Too many pending engineering confirmations.")
        expires = min(now + timedelta(minutes=15), self._approval.valid_to)
        token = secrets.token_urlsafe(32)
        self._prepared[token] = (command, expires)
        return EngineeringPreparedCommand(
            token, request.command_sha256, request.command_payload_json, expires
        )

    def confirm(self, confirmation_token: str, *, credential: str) -> GovernanceWriteResult:
        self._authenticate(credential)
        candidate = self._prepared.get(confirmation_token)
        if candidate is None or self._clock() >= candidate[1]:
            raise EngineeringGovernanceDenied("Confirmation expired or unknown; prepare again.")
        command = candidate[0]
        if not self._authorizer.authorize(self._request(command)):
            raise EngineeringGovernanceDenied("Confirmed command is no longer approved.")
        # Same idempotency key deliberately retained on repeats and uncertain commits.
        return getattr(self._writer, _COMMANDS[type(command)][1])(command)

    def receipt(
        self, command: GovernanceCommand, *, credential: str
    ) -> GovernanceWriteResult | None:
        self._authenticate(credential)
        request = self._request(command)
        if (
            command.school_id != self._approval.school_id
            or command.actor.actor_reference != self._approval.actor_reference
            or governance_command_digest(command) not in self._approval.command_digests
        ):
            raise EngineeringGovernanceDenied("Original command is outside this approval.")
        return self._writer.lookup_command_result(command, command_name=request.command_name)
