from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol
from uuid import UUID

from app.platform.contracts import GuardianActorContext, StaffActorContext

CorrectionStatus = Literal["submitted", "resolved", "reviewed"]


class FamilyCorrectionDenied(RuntimeError):
    pass


class FamilyCorrectionNotFound(FamilyCorrectionDenied):
    pass


@dataclass(frozen=True, slots=True)
class GuardianCorrectionPermit:
    actor: GuardianActorContext
    student_id: UUID
    relationship_id: UUID
    fact_kind: str
    source_key: str
    field_name: str
    checked_at: datetime
    target_version: int = 1


@dataclass(frozen=True, slots=True)
class FamilyCorrectionReport:
    report_id: UUID
    school_id: UUID
    reporter_person_id: UUID
    relationship_id: UUID
    student_id: UUID
    fact_kind: str
    source_key: str
    field_name: str
    statement: str
    status: CorrectionStatus
    submitted_at: datetime
    resolution_locator_sha256: str | None = None
    resolved_at: datetime | None = None
    target_version: int = 1
    review_explanation: str | None = None


@dataclass(frozen=True, slots=True)
class FamilyCorrectionResolutionTarget:
    report_id: UUID
    school_id: UUID
    fact_kind: str
    source_key: str
    field_name: str
    status: CorrectionStatus
    submitted_at: datetime
    target_version: int


class GuardianCorrectionAuthority(Protocol):
    def require_submit(
        self,
        *,
        actor: GuardianActorContext,
        target_reference: str,
        field_name: str,
        checked_at: datetime,
    ) -> GuardianCorrectionPermit: ...
    def revalidate(self, permit: GuardianCorrectionPermit, *, checked_at: datetime) -> None: ...
    def require_current(self, actor: GuardianActorContext, *, checked_at: datetime) -> None: ...


class StaffCorrectionAuthority(Protocol):
    def require_action(
        self,
        *,
        actor: StaffActorContext,
        fact_kind: str,
        source_key: str,
        field_name: str,
        action: Literal["maintain", "end", "revoke"],
        checked_at: datetime,
    ) -> object: ...


class FamilyCorrectionRepository(Protocol):
    def submit(
        self, *, permit: GuardianCorrectionPermit, statement: str, idempotency_key: str
    ) -> FamilyCorrectionReport: ...
    def own(
        self, *, actor: GuardianActorContext, report_id: UUID
    ) -> FamilyCorrectionReport | None: ...
    def own_by_key(
        self, *, actor: GuardianActorContext, idempotency_key: str
    ) -> FamilyCorrectionReport | None: ...
    def list_own(
        self, *, actor: GuardianActorContext, limit: int
    ) -> tuple[FamilyCorrectionReport, ...]: ...
    def get_target(
        self, *, school_id: UUID, report_id: UUID
    ) -> FamilyCorrectionResolutionTarget | None: ...
    def list_target(
        self, *, school_id: UUID, fact_kind: str, source_key: str, field_name: str, limit: int
    ) -> tuple[FamilyCorrectionReport, ...]: ...
    def resolve(
        self, *, report_id: UUID, locator_sha256: str, resolved_at: datetime
    ) -> FamilyCorrectionReport: ...
    def current_target_version(
        self, *, school_id: UUID, fact_kind: str, source_key: str
    ) -> int: ...
    def review_unchanged(
        self,
        *,
        actor: StaffActorContext,
        report_id: UUID,
        explanation: str,
        target_version: int,
        command_sha256: str,
        reviewed_at: datetime,
    ) -> FamilyCorrectionReport: ...
    def review_result(
        self, *, actor: StaffActorContext, report_id: UUID, command_sha256: str
    ) -> FamilyCorrectionReport | None: ...


class FormalReceiptVerifier(Protocol):
    def require_applied_target(
        self,
        *,
        actor: StaffActorContext,
        locator_sha256: str,
        fact_kind: str,
        source_key: str,
        field_name: str,
        minimum_target_version: int,
        submitted_at: datetime,
    ) -> Literal["maintain", "end", "revoke"]: ...


class FamilyCorrectionService:
    def __init__(
        self,
        *,
        repository: FamilyCorrectionRepository,
        guardian_authority: GuardianCorrectionAuthority,
        staff_authority: StaffCorrectionAuthority,
        receipt_verifier: FormalReceiptVerifier,
        clock: Callable[[], datetime],
    ):
        self._repo = repository
        self._guardian = guardian_authority
        self._staff = staff_authority
        self._receipts = receipt_verifier
        self._clock = clock

    def submit(
        self,
        *,
        actor: GuardianActorContext,
        target_reference: str,
        field_name: str,
        statement: str,
        idempotency_key: str,
    ) -> FamilyCorrectionReport:
        clean = " ".join(statement.split())
        if not clean or len(clean) > 1000 or not idempotency_key or len(idempotency_key) > 255:
            raise FamilyCorrectionDenied("Invalid correction report.")
        permit = self._guardian.require_submit(
            actor=actor,
            target_reference=target_reference,
            field_name=field_name,
            checked_at=self._clock(),
        )
        report = self._repo.submit(permit=permit, statement=clean, idempotency_key=idempotency_key)
        self._guardian.revalidate(permit, checked_at=self._clock())
        return report

    def own(self, *, actor: GuardianActorContext, report_id: UUID) -> FamilyCorrectionReport:
        self._guardian.require_current(actor, checked_at=self._clock())
        report = self._repo.own(actor=actor, report_id=report_id)
        self._guardian.require_current(actor, checked_at=self._clock())
        if report is None:
            raise FamilyCorrectionNotFound("Correction report unavailable.")
        return report

    def own_by_key(
        self, *, actor: GuardianActorContext, idempotency_key: str
    ) -> FamilyCorrectionReport:
        self._guardian.require_current(actor, checked_at=self._clock())
        report = self._repo.own_by_key(actor=actor, idempotency_key=idempotency_key)
        self._guardian.require_current(actor, checked_at=self._clock())
        if report is None:
            raise FamilyCorrectionNotFound("Correction report unavailable.")
        return report

    def list_own(
        self, *, actor: GuardianActorContext, limit: int = 20
    ) -> tuple[FamilyCorrectionReport, ...]:
        if not 1 <= limit <= 20:
            raise FamilyCorrectionDenied("Invalid correction report limit.")
        self._guardian.require_current(actor, checked_at=self._clock())
        reports = self._repo.list_own(actor=actor, limit=limit)
        self._guardian.require_current(actor, checked_at=self._clock())
        return reports

    def staff_list(
        self,
        *,
        actor: StaffActorContext,
        fact_kind: str,
        source_key: str,
        field_name: str,
        action: Literal["maintain", "end", "revoke"] = "maintain",
        limit: int = 20,
    ) -> tuple[FamilyCorrectionReport, ...]:
        if not 1 <= limit <= 20:
            raise FamilyCorrectionDenied("Invalid correction report limit.")
        self._require_matching_action(field_name=field_name, action=action)
        self._staff.require_action(
            actor=actor,
            fact_kind=fact_kind,
            source_key=source_key,
            field_name=field_name,
            action=action,
            checked_at=self._clock(),
        )
        rows = self._repo.list_target(
            school_id=actor.school_id,
            fact_kind=fact_kind,
            source_key=source_key,
            field_name=field_name,
            limit=limit,
        )
        self._staff.require_action(
            actor=actor,
            fact_kind=fact_kind,
            source_key=source_key,
            field_name=field_name,
            action=action,
            checked_at=self._clock(),
        )
        return rows

    def resolve(
        self, *, actor: StaffActorContext, report_id: UUID, locator_sha256: str
    ) -> FamilyCorrectionReport:
        report = self._repo.get_target(school_id=actor.school_id, report_id=report_id)
        if report is None or report.status != "submitted":
            raise FamilyCorrectionNotFound("Correction report unavailable.")
        receipt_action = self._receipts.require_applied_target(
            actor=actor,
            locator_sha256=locator_sha256,
            fact_kind=report.fact_kind,
            source_key=report.source_key,
            field_name=report.field_name,
            minimum_target_version=report.target_version,
            submitted_at=report.submitted_at,
        )
        self._require_matching_action(field_name=report.field_name, action=receipt_action)
        self._staff.require_action(
            actor=actor,
            fact_kind=report.fact_kind,
            source_key=report.source_key,
            field_name=report.field_name,
            action=receipt_action,
            checked_at=self._clock(),
        )
        self._staff.require_action(
            actor=actor,
            fact_kind=report.fact_kind,
            source_key=report.source_key,
            field_name=report.field_name,
            action=receipt_action,
            checked_at=self._clock(),
        )
        return self._repo.resolve(
            report_id=report_id, locator_sha256=locator_sha256, resolved_at=self._clock()
        )

    def prepare_review(
        self,
        *,
        actor: StaffActorContext,
        report_id: UUID,
        explanation: str,
        action: Literal["maintain", "end", "revoke"] = "maintain",
    ) -> tuple[FamilyCorrectionResolutionTarget, int, str]:
        clean = " ".join(explanation.split())
        if not clean or len(clean) > 1000:
            raise FamilyCorrectionDenied("A readable review explanation is required.")
        report = self._repo.get_target(school_id=actor.school_id, report_id=report_id)
        if report is None or report.status not in {"submitted", "reviewed"}:
            raise FamilyCorrectionNotFound("Correction report unavailable for review.")
        self._require_matching_action(field_name=report.field_name, action=action)
        self._staff.require_action(
            actor=actor,
            fact_kind=report.fact_kind,
            source_key=report.source_key,
            field_name=report.field_name,
            action=action,
            checked_at=self._clock(),
        )
        version = self._repo.current_target_version(
            school_id=actor.school_id, fact_kind=report.fact_kind, source_key=report.source_key
        )
        if version < report.target_version:
            raise FamilyCorrectionDenied("Current formal target is unavailable.")
        return report, version, clean

    def review_unchanged(
        self,
        *,
        actor: StaffActorContext,
        report_id: UUID,
        explanation: str,
        expected_target_version: int,
        command_sha256: str,
        confirmed: bool,
        action: Literal["maintain", "end", "revoke"] = "maintain",
    ) -> FamilyCorrectionReport:
        if confirmed is not True or len(command_sha256) != 64:
            raise FamilyCorrectionDenied("Explicit prepared review confirmation required.")
        report, version, clean = self.prepare_review(
            actor=actor, report_id=report_id, explanation=explanation, action=action
        )
        if version != expected_target_version:
            raise FamilyCorrectionDenied("Formal target changed after review preparation.")
        self._staff.require_action(
            actor=actor,
            fact_kind=report.fact_kind,
            source_key=report.source_key,
            field_name=report.field_name,
            action=action,
            checked_at=self._clock(),
        )
        return self._repo.review_unchanged(
            actor=actor,
            report_id=report_id,
            explanation=clean,
            target_version=version,
            command_sha256=command_sha256,
            reviewed_at=self._clock(),
        )

    def review_result(
        self,
        *,
        actor: StaffActorContext,
        report_id: UUID,
        command_sha256: str,
        action: Literal["maintain", "end", "revoke"],
    ) -> FamilyCorrectionReport:
        target = self._repo.get_target(school_id=actor.school_id, report_id=report_id)
        if target is None:
            raise FamilyCorrectionNotFound("Review result unavailable.")
        self._require_matching_action(field_name=target.field_name, action=action)
        self._staff.require_action(
            actor=actor,
            fact_kind=target.fact_kind,
            source_key=target.source_key,
            field_name=target.field_name,
            action=action,
            checked_at=self._clock(),
        )
        result = self._repo.review_result(
            actor=actor, report_id=report_id, command_sha256=command_sha256
        )
        self._staff.require_action(
            actor=actor,
            fact_kind=target.fact_kind,
            source_key=target.source_key,
            field_name=target.field_name,
            action=action,
            checked_at=self._clock(),
        )
        if result is None:
            raise FamilyCorrectionNotFound("Review result unavailable.")
        return result

    @staticmethod
    def _require_matching_action(
        *, field_name: str, action: Literal["maintain", "end", "revoke"]
    ) -> None:
        allowed = {"end", "revoke"} if field_name == "relationship" else {"maintain"}
        if action not in allowed:
            raise FamilyCorrectionDenied("Correction action does not match its target.")
