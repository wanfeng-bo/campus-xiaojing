# pyright: strict

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from app.platform.authorization.thin_contracts import NamedProjection, ScopePlan
from app.platform.contracts import StaffActorContext

from .f2_reader_contracts import F2FactReadSelection, F2FactRow


class SecureReadDenied(RuntimeError):
    """A read operation or its current authorization is not available."""


class SecureReadAuthorizationDrift(SecureReadDenied):
    """Authorization changed after rows were read; the rows must be discarded."""


@dataclass(frozen=True, slots=True)
class SchoolFactReadSelection:
    """Fixed value-only F1 selector; never carries school, scope or SQL."""

    campus_name: str | None = None
    campus_id: UUID | None = None
    lineage_id: UUID | None = None

    def __post_init__(self) -> None:
        if self.lineage_id is not None and (
            type(self.lineage_id) is not UUID
            or self.campus_name is not None
            or self.campus_id is not None
        ):
            raise ValueError("invalid fixed lineage selection")
        if self.campus_name is not None:
            if (
                type(self.campus_name) is not str
                or not self.campus_name.strip()
                or len(self.campus_name) > 200
                or self.campus_id is not None
            ):
                raise ValueError("invalid fixed campus selection")
            object.__setattr__(self, "campus_name", self.campus_name.strip())
        if self.campus_id is not None and type(self.campus_id) is not UUID:
            raise ValueError("invalid fixed campus selection")

    @property
    def binding_key(self) -> str:
        if self.lineage_id is not None:
            return f"lineage:{self.lineage_id}"
        if self.campus_id is not None:
            return f"id:{self.campus_id}"
        if self.campus_name is not None:
            return f"name:{self.campus_name}"
        return "all-campuses"


@dataclass(frozen=True, slots=True)
class SecureReadRequest:
    actor: StaffActorContext
    operation_code: str
    cursor: str | None = None
    page_size: int = 50
    selection: SchoolFactReadSelection | F2FactReadSelection | None = None
    factual_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class ReadAuthorization:
    allowed: bool
    basis: object | None
    scope_plan: ScopePlan | None
    projection: NamedProjection | None
    basis_fingerprint: str | None


@dataclass(frozen=True, slots=True)
class SelfCurrentAssignmentRow:
    row_id: UUID
    organization_unit_id: UUID
    position_title: str
    semantic_role_code: str
    assignment_kind: str
    effective_from: datetime
    effective_to: datetime | None
    planned_valid_to: datetime | None
    version: int


@dataclass(frozen=True, slots=True)
class PublicLocationRow:
    row_id: UUID
    kind: str
    business_code: str | None
    display_name: str
    display_path: str
    valid_from: datetime
    valid_to: datetime | None
    version: int


@dataclass(frozen=True, slots=True)
class OfficialOfficeContactRow:
    row_id: UUID
    owner_kind: str
    organization_unit_id: UUID | None
    location_id: UUID | None
    contact_kind: str
    contact_value: str
    service_hours: str | None
    version: int


@dataclass(frozen=True, slots=True)
class OfficialContactCampusRow:
    row_id: UUID
    display_name: str
    display_path: str
    valid_from: datetime
    valid_to: datetime | None
    version: int


@dataclass(frozen=True, slots=True)
class PublishedOfficialContactRow:
    row_id: UUID
    campus_id: UUID
    campus_name: str
    contact_kind: str
    contact_value: str
    service_hours: str | None
    valid_from: datetime
    valid_to: datetime | None
    version: int
    source_kind: str
    lineage_id: UUID
    batch_id: UUID
    published_at: datetime


@dataclass(frozen=True, slots=True)
class OfficialContactMaintenanceTargetRow:
    """Server-only canonical coordinates; never serialize this DTO into model context."""

    row_id: UUID
    source_key: str
    expected_version: int
    campus_id: UUID
    campus_source_key: str
    school_fact_version: int
    owner_kind: str
    contact_kind: str
    contact_value: str
    service_hours: str | None
    lifecycle_status: str
    visibility_scope: str
    audience_code: str | None
    valid_from: datetime
    valid_to: datetime | None


type SecureReadRow = (
    SelfCurrentAssignmentRow
    | PublicLocationRow
    | OfficialOfficeContactRow
    | OfficialContactCampusRow
    | PublishedOfficialContactRow
    | OfficialContactMaintenanceTargetRow
    | F2FactRow
)


@dataclass(frozen=True, slots=True)
class SecureReadPage:
    rows: tuple[SecureReadRow, ...]
    total_count: int
    as_of: datetime
    next_cursor: str | None


class ReadAuthorizer(Protocol):
    def decide(self, *, operation: object, actor: StaffActorContext, as_of: datetime) -> object: ...

    def revalidate(
        self,
        authorization: object,
        *,
        actor: StaffActorContext,
        as_of: datetime,
    ) -> bool: ...


__all__ = [
    "OfficialContactMaintenanceTargetRow",
    "SchoolFactReadSelection",
    "OfficialContactCampusRow",
    "PublishedOfficialContactRow",
    "OfficialOfficeContactRow",
    "PublicLocationRow",
    "ReadAuthorization",
    "ReadAuthorizer",
    "SecureReadAuthorizationDrift",
    "SecureReadDenied",
    "SecureReadPage",
    "SecureReadRow",
    "SelfCurrentAssignmentRow",
    "SecureReadRequest",
]
