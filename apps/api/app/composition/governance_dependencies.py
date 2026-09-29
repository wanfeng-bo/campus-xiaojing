# pyright: strict

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from uuid import UUID

from sqlalchemy.orm import Session, sessionmaker

from app.platform.contracts import AssignmentId, OrganizationUnitId, PersonId, SchoolId
from app.platform.directory.repository import SqlAlchemyPersonRepository
from app.platform.governance.contracts import (
    AssignmentAuthoritySnapshot,
    RootAuthoritySnapshot,
)
from app.platform.organization.contracts import CurrentAssignmentReader
from app.platform.organization.repository import SqlAlchemyOrganizationRepository


class SqlAlchemyGovernanceDependencyAdapter:
    """Composition adapter from owner-domain public Readers to Governance snapshots."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        assignment_reader_factory: Callable[[Session], CurrentAssignmentReader] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._assignment_reader_factory = (
            assignment_reader_factory or SqlAlchemyOrganizationRepository
        )

    def read_assignment(
        self,
        *,
        school_id: UUID,
        assignment_id: UUID,
        as_of: datetime,
    ) -> AssignmentAuthoritySnapshot | None:
        with self._session_factory.begin() as session:
            snapshot = self._assignment_reader_factory(session).get_current_assignment(
                SchoolId(school_id), AssignmentId(assignment_id), as_of=as_of
            )
        if snapshot is None:
            return None
        return AssignmentAuthoritySnapshot(
            school_id=school_id,
            assignment_id=assignment_id,
            person_id=snapshot.person_id,
            organization_id=snapshot.organization_unit_id,
            version=snapshot.source_version,
            is_effective=True,
            valid_until=_minimum_boundary(
                snapshot.effective_interval.valid_to,
                snapshot.planned_valid_to,
            ),
        )

    def read_person_root(
        self,
        *,
        school_id: UUID,
        person_id: UUID,
        as_of: datetime,
    ) -> RootAuthoritySnapshot | None:
        with self._session_factory.begin() as session:
            person = SqlAlchemyPersonRepository(session).lock_person(
                SchoolId(school_id), PersonId(person_id), as_of=as_of
            )
        if person is None:
            return None
        return RootAuthoritySnapshot(
            school_id=school_id,
            root_kind="person",
            root_id=person_id,
            version=person.version,
            is_effective=person.status == "active",
            valid_until=None,
        )

    def read_organization_root(
        self,
        *,
        school_id: UUID,
        organization_id: UUID,
        as_of: datetime,
    ) -> RootAuthoritySnapshot | None:
        with self._session_factory.begin() as session:
            organization = SqlAlchemyOrganizationRepository(session).lock_organization_unit(
                SchoolId(school_id), OrganizationUnitId(organization_id), as_of=as_of
            )
        if organization is None:
            return None
        return RootAuthoritySnapshot(
            school_id=school_id,
            root_kind="organization",
            root_id=organization_id,
            version=organization.version,
            is_effective=organization.status == "active",
            valid_until=None,
        )


def _minimum_boundary(*values: datetime | None) -> datetime | None:
    present = [value for value in values if value is not None]
    return min(present) if present else None


__all__ = ["SqlAlchemyGovernanceDependencyAdapter"]
