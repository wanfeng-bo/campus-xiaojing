"""F6 time metadata from the existing, exact current-school tenancy reader."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.composition.school_fact_directory_context import resolve_bound_identity
from app.db.rls_context import set_local_execution_context
from app.platform.identity.execution_context import compose_request_context
from app.platform.tenancy.repository import SqlAlchemySchoolRepository


def unknown_time_document() -> dict[str, Any]:
    return {
        "fact_timezone_name": None,
        "time_basis": {
            "kind": "school_timezone",
            "status": "unavailable",
            "source_version": None,
            "valid_from": None,
            "valid_to": None,
        },
    }


class SqlResidenceSchoolTime:
    def __init__(
        self,
        *,
        sessions: Any,
        resolve_identity: Any,
        proof_signer: Any,
        clock: Any = lambda: datetime.now(UTC),
    ) -> None:
        self.sessions, self.resolve_identity, self.proof_signer, self.clock = (
            sessions,
            resolve_identity,
            proof_signer,
            clock,
        )

    def __call__(self, actor: Any, at: datetime) -> Any:
        identity = resolve_bound_identity(actor, self.resolve_identity)
        envelope = self.proof_signer.sign_execution_context(
            compose_request_context(actor, uuid4()),
            session_nonce_hash=identity.session_binding,
            proof_expires_at=self.clock() + timedelta(seconds=30),
        )
        with self.sessions() as session, session.begin():
            set_local_execution_context(session, envelope)
            snapshot = SqlAlchemySchoolRepository(session).get_current_school(
                actor.school_id, as_of=at
            )
        if resolve_bound_identity(actor, self.resolve_identity) != identity:
            raise PermissionError("学校时间依据的当前身份已变化。")
        return snapshot


@dataclass(frozen=True)
class ResidenceTimeEvidence:
    service: Any
    actor: Any
    requested_at: datetime | None
    observed_at: datetime
    basis: tuple[Any, ...] | None
    next_boundary: datetime | None

    @property
    def document(self) -> dict[str, Any]:
        if self.basis is None:
            return unknown_time_document()
        zone, version, start, end = self.basis
        return {
            "fact_timezone_name": zone,
            "time_basis": {
                "kind": "school_timezone",
                "status": "available" if zone else "unavailable",
                "source_version": version,
                "valid_from": start.isoformat(),
                "valid_to": end.isoformat() if end else None,
            },
        }

    def revalidate(self, *, actor: Any) -> None:
        if actor != self.actor or not self.service.current_actor(actor):
            raise PermissionError("学校时间依据的当前身份已变化。")
        now = self.service.clock()
        if now < self.observed_at or self.next_boundary is not None and now >= self.next_boundary:
            raise RuntimeError("学校时间依据已到适用边界，请重新查询。")
        latest = self.service.capture(actor, as_of=self.requested_at)
        if latest.basis != self.basis:
            raise RuntimeError("学校时间依据已变化，请重新查询。")


class ResidenceTimeService:
    def __init__(
        self, *, load: Any, current_actor: Any, clock: Any = lambda: datetime.now(UTC)
    ) -> None:
        self.load, self.current_actor, self.clock = load, current_actor, clock

    def capture(
        self, actor: Any, *, as_of: datetime | None = None, current_at: datetime | None = None
    ) -> ResidenceTimeEvidence:
        if not self.current_actor(actor):
            raise PermissionError("未绑定当前获准身份。")
        now = self.clock()
        at = as_of or current_at or now
        snapshot = self.load(actor, at)
        if not self.current_actor(actor):
            raise PermissionError("当前身份已变化。")
        basis = None
        boundary = None
        if snapshot is not None:
            interval = snapshot.effective_interval
            if snapshot.school_id != actor.school_id or not (
                interval.valid_from <= at and (interval.valid_to is None or at < interval.valid_to)
            ):
                raise ValueError("学校时间快照不属于该学校或时点。")
            zone = snapshot.timezone_name
            try:
                if not isinstance(zone, str):
                    raise ValueError("Missing time zone")
                ZoneInfo(zone)
            except (ZoneInfoNotFoundError, ValueError):
                zone = None
            basis = (zone, snapshot.source_version, interval.valid_from, interval.valid_to)
            boundary = interval.valid_to if as_of is None else None
        return ResidenceTimeEvidence(self, actor, as_of, now, basis, boundary)
