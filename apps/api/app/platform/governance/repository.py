# pyright: strict

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import and_, func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.platform.governance.models import (
    GovernanceAssignmentEpoch,
    GovernanceAuditEvent,
    GovernanceBootstrapState,
    GovernanceCommandReceipt,
    GovernanceDuty,
    GovernanceDutyVersion,
    GovernanceGrant,
    GovernanceGrantVersion,
    GovernanceScope,
    GovernanceScopeVersion,
)


class GovernanceRepository:
    """All Governance persistence used by the single Writer transaction."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, value: object) -> None:
        self._session.add(value)

    def flush(self) -> None:
        self._session.flush()

    def session_user(self) -> str:
        value = self._session.scalar(text("SELECT session_user"))
        if not isinstance(value, str) or not value:
            raise RuntimeError("PostgreSQL did not expose a trusted session_user.")
        return value

    def lock_bootstrap_state(self, school_id: UUID) -> GovernanceBootstrapState | None:
        return self._session.scalar(
            select(GovernanceBootstrapState)
            .where(GovernanceBootstrapState.school_id == school_id)
            .with_for_update()
        )

    def lock_receipt(
        self, school_id: UUID, idempotency_key_hash: str
    ) -> GovernanceCommandReceipt | None:
        return self._session.scalar(
            select(GovernanceCommandReceipt)
            .where(
                GovernanceCommandReceipt.school_id == school_id,
                GovernanceCommandReceipt.idempotency_key_hash == idempotency_key_hash,
            )
            .with_for_update()
        )

    def acquire_receipt(
        self, candidate: GovernanceCommandReceipt
    ) -> tuple[GovernanceCommandReceipt, bool]:
        """Atomically win a command key or lock the already committed winner."""

        statement = (
            pg_insert(GovernanceCommandReceipt)
            .values(
                id=candidate.id,
                school_id=candidate.school_id,
                idempotency_key_hash=candidate.idempotency_key_hash,
                request_sha256=candidate.request_sha256,
                command_name=candidate.command_name,
                actor_kind=candidate.actor_kind,
                actor_reference=candidate.actor_reference,
                actor_assignment_id=candidate.actor_assignment_id,
                actor_assignment_version=candidate.actor_assignment_version,
                request_id=candidate.request_id,
                status=candidate.status,
                target_kind=candidate.target_kind,
                target_id=candidate.target_id,
                target_version=candidate.target_version,
                result_payload=candidate.result_payload,
            )
            .on_conflict_do_nothing(
                index_elements=[
                    GovernanceCommandReceipt.school_id,
                    GovernanceCommandReceipt.idempotency_key_hash,
                ]
            )
            .returning(GovernanceCommandReceipt.id)
        )
        inserted_id = self._session.scalar(statement)
        receipt = self.lock_receipt(
            candidate.school_id,
            candidate.idempotency_key_hash,
        )
        if receipt is None:
            raise RuntimeError("Governance receipt acquisition produced no authoritative row.")
        return receipt, inserted_id is not None

    def lock_grant(self, school_id: UUID, grant_id: UUID) -> GovernanceGrant | None:
        return self._session.scalar(
            select(GovernanceGrant)
            .where(
                GovernanceGrant.school_id == school_id,
                GovernanceGrant.id == grant_id,
            )
            .with_for_update()
        )

    def grant_version(
        self, school_id: UUID, grant_id: UUID, version: int
    ) -> GovernanceGrantVersion | None:
        return self._session.scalar(
            select(GovernanceGrantVersion).where(
                GovernanceGrantVersion.school_id == school_id,
                GovernanceGrantVersion.grant_id == grant_id,
                GovernanceGrantVersion.version == version,
            )
        )

    def latest_grant_effective_from(self, school_id: UUID, grant_id: UUID) -> datetime | None:
        return self._session.scalar(
            select(func.max(GovernanceGrantVersion.effective_from)).where(
                GovernanceGrantVersion.school_id == school_id,
                GovernanceGrantVersion.grant_id == grant_id,
            )
        )

    def effective_grant_version(
        self,
        school_id: UUID,
        grant_id: UUID,
        *,
        as_of: datetime,
    ) -> GovernanceGrantVersion | None:
        return self._session.scalar(
            select(GovernanceGrantVersion)
            .where(
                GovernanceGrantVersion.school_id == school_id,
                GovernanceGrantVersion.grant_id == grant_id,
                GovernanceGrantVersion.transition_kind.in_(("create", "update")),
                GovernanceGrantVersion.effective_from <= as_of,
            )
            .order_by(
                GovernanceGrantVersion.effective_from.desc(),
                GovernanceGrantVersion.version.desc(),
            )
            .limit(1)
        )

    def scope_version(
        self, school_id: UUID, scope_id: UUID, version: int
    ) -> GovernanceScopeVersion | None:
        return self._session.scalar(
            select(GovernanceScopeVersion).where(
                GovernanceScopeVersion.school_id == school_id,
                GovernanceScopeVersion.scope_id == scope_id,
                GovernanceScopeVersion.version == version,
            )
        )

    def advance_assignment_epoch(self, school_id: UUID, assignment_id: UUID) -> int:
        statement = (
            pg_insert(GovernanceAssignmentEpoch)
            .values(
                school_id=school_id,
                assignment_id=assignment_id,
                epoch=1,
            )
            .on_conflict_do_update(
                index_elements=[
                    GovernanceAssignmentEpoch.school_id,
                    GovernanceAssignmentEpoch.assignment_id,
                ],
                set_={
                    "epoch": GovernanceAssignmentEpoch.epoch + 1,
                    "updated_at": func.now(),
                },
            )
            .returning(GovernanceAssignmentEpoch.epoch)
        )
        epoch = self._session.scalar(statement)
        if not isinstance(epoch, int) or epoch < 1:
            raise RuntimeError("Governance assignment epoch did not advance atomically.")
        return epoch


class GovernanceOutcomeProbeRepository:
    """Read-only receipt and authority projection used after an uncertain commit."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def receipt(
        self, school_id: UUID, idempotency_key_hash: str
    ) -> GovernanceCommandReceipt | None:
        return self._session.scalar(
            select(GovernanceCommandReceipt).where(
                GovernanceCommandReceipt.school_id == school_id,
                GovernanceCommandReceipt.idempotency_key_hash == idempotency_key_hash,
            )
        )

    def authoritative_target_matches(self, receipt: GovernanceCommandReceipt) -> bool:
        target_id = receipt.target_id
        target_version = receipt.target_version
        if target_id is None or target_version is None:
            return False
        if receipt.result_payload.get("target_id") != str(target_id):
            return False
        if receipt.result_payload.get("target_version") != target_version:
            return False

        if receipt.target_kind == "grant":
            statement = (
                select(GovernanceGrant.id)
                .join(
                    GovernanceGrantVersion,
                    and_(
                        GovernanceGrantVersion.school_id == GovernanceGrant.school_id,
                        GovernanceGrantVersion.grant_id == GovernanceGrant.id,
                    ),
                )
                .where(
                    GovernanceGrant.school_id == receipt.school_id,
                    GovernanceGrant.id == target_id,
                    GovernanceGrant.current_version >= target_version,
                    GovernanceGrantVersion.version == target_version,
                )
            )
        elif receipt.target_kind == "duty":
            statement = (
                select(GovernanceDuty.id)
                .join(
                    GovernanceDutyVersion,
                    and_(
                        GovernanceDutyVersion.school_id == GovernanceDuty.school_id,
                        GovernanceDutyVersion.duty_id == GovernanceDuty.id,
                    ),
                )
                .where(
                    GovernanceDuty.school_id == receipt.school_id,
                    GovernanceDuty.id == target_id,
                    GovernanceDuty.current_version >= target_version,
                    GovernanceDutyVersion.version == target_version,
                )
            )
        elif receipt.target_kind == "scope":
            statement = (
                select(GovernanceScope.id)
                .join(
                    GovernanceScopeVersion,
                    and_(
                        GovernanceScopeVersion.school_id == GovernanceScope.school_id,
                        GovernanceScopeVersion.scope_id == GovernanceScope.id,
                    ),
                )
                .where(
                    GovernanceScope.school_id == receipt.school_id,
                    GovernanceScope.id == target_id,
                    GovernanceScope.current_version >= target_version,
                    GovernanceScopeVersion.version == target_version,
                )
            )
        elif receipt.target_kind == "bootstrap_state":
            statement = select(GovernanceBootstrapState.school_id).where(
                GovernanceBootstrapState.school_id == receipt.school_id,
                GovernanceBootstrapState.school_id == target_id,
                GovernanceBootstrapState.version >= target_version,
            )
        else:
            return False
        return self._session.scalar(statement) is not None


__all__ = [
    "GovernanceAuditEvent",
    "GovernanceCommandReceipt",
    "GovernanceDuty",
    "GovernanceDutyVersion",
    "GovernanceGrant",
    "GovernanceGrantVersion",
    "GovernanceOutcomeProbeRepository",
    "GovernanceRepository",
    "GovernanceScope",
    "GovernanceScopeVersion",
]
