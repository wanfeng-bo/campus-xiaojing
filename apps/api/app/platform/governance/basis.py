# pyright: strict

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session, sessionmaker

from app.platform.governance.contracts import (
    GovernanceAuthorityQuery,
    GovernanceAuthoritySnapshot,
    GovernanceAuthorizationBasis,
    GovernanceDependencyVersion,
)
from app.platform.governance.models import GovernanceAssignmentEpoch
from app.platform.governance.reader import (
    GovernanceAuthorityReader,
    GovernanceAuthorityUnavailable,
)


class GovernanceBasisInvalid(RuntimeError):
    """A previously issued Governance basis is no longer authoritative."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


class GovernanceAuthorizationBasisService:
    """Issues and revalidates Governance inputs without deciding module actions."""

    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        authority_reader: GovernanceAuthorityReader,
    ) -> None:
        self._session_factory = session_factory
        self._authority_reader = authority_reader

    def issue(self, query: GovernanceAuthorityQuery) -> GovernanceAuthorizationBasis:
        authority = self._authority_reader.resolve(query)
        return self._basis(authority, query.as_of)

    def issue_many(
        self, queries: tuple[GovernanceAuthorityQuery, ...]
    ) -> tuple[GovernanceAuthorizationBasis | GovernanceAuthorityUnavailable, ...]:
        authorities = self._authority_reader.resolve_many(queries)
        bases = tuple(
            authority
            if isinstance(authority, GovernanceAuthorityUnavailable)
            else self._basis(authority, query.as_of)
            for query, authority in zip(queries, authorities, strict=True)
        )
        # Stored Grant snapshots precede owner calls. Reject any epoch changed
        # during those calls before exposing a newly issued batch to consumers.
        epochs = self._read_assignment_epochs(
            tuple(basis for basis in bases if isinstance(basis, GovernanceAuthorizationBasis))
        )
        return tuple(
            GovernanceAuthorityUnavailable(
                "assignment_epoch_changed", "Governance epoch changed during basis issuance."
            )
            if isinstance(basis, GovernanceAuthorizationBasis)
            and epochs.get((basis.school_id, basis.assignment_id)) != basis.assignment_epoch
            else basis
            for basis in bases
        )

    @staticmethod
    def _basis(
        authority: GovernanceAuthoritySnapshot, as_of: datetime
    ) -> GovernanceAuthorizationBasis:
        return GovernanceAuthorizationBasis(
            school_id=authority.school_id,
            assignment_id=authority.assignment_id,
            duty_code=authority.duty_code,
            scope_id=authority.scope_id,
            grant_id=authority.grant_id,
            grant_version=authority.grant_version,
            assignment_epoch=authority.assignment_epoch,
            issued_as_of=as_of,
            valid_until=authority.valid_until,
            dependency_versions=authority.dependency_versions,
        )

    def validate(self, basis: GovernanceAuthorizationBasis, *, as_of: datetime) -> bool:
        if basis.valid_until is not None and as_of >= basis.valid_until:
            raise GovernanceBasisInvalid("basis_expired", "Governance basis has expired.")
        if (
            self._read_assignment_epoch(basis.school_id, basis.assignment_id)
            != basis.assignment_epoch
        ):
            raise GovernanceBasisInvalid(
                "assignment_epoch_changed", "Governance assignment epoch has changed."
            )
        query = GovernanceAuthorityQuery(
            school_id=basis.school_id,
            assignment_id=basis.assignment_id,
            duty_code=basis.duty_code,
            scope_id=basis.scope_id,
            as_of=as_of,
        )
        try:
            current = self._authority_reader.resolve(query)
        except GovernanceAuthorityUnavailable as exc:
            raise GovernanceBasisInvalid(
                "authority_unavailable", "Governance authority is no longer available."
            ) from exc
        if current.assignment_epoch != basis.assignment_epoch:
            raise GovernanceBasisInvalid(
                "assignment_epoch_changed", "Governance assignment epoch has changed."
            )
        if (
            current.grant_id != basis.grant_id
            or current.grant_version != basis.grant_version
            or _dependency_map(current.dependency_versions)
            != _dependency_map(basis.dependency_versions)
        ):
            raise GovernanceBasisInvalid(
                "dependency_version_changed", "Governance dependencies have changed."
            )
        if (
            self._read_assignment_epoch(basis.school_id, basis.assignment_id)
            != basis.assignment_epoch
        ):
            raise GovernanceBasisInvalid(
                "assignment_epoch_changed", "Governance assignment epoch changed during validation."
            )
        return True

    def validate_many(
        self, bases: tuple[GovernanceAuthorizationBasis, ...], *, as_of: datetime
    ) -> tuple[bool | GovernanceBasisInvalid, ...]:
        if len(bases) > 128:
            raise ValueError("Governance basis batches are limited to 128 items.")
        results: dict[int, bool | GovernanceBasisInvalid] = {}
        pending: dict[int, GovernanceAuthorizationBasis] = {}
        for index, basis in enumerate(bases):
            if basis.valid_until is not None and as_of >= basis.valid_until:
                results[index] = GovernanceBasisInvalid(
                    "basis_expired", "Governance basis has expired."
                )
            else:
                pending[index] = basis
        before = self._read_assignment_epochs(tuple(pending.values()))
        for index, basis in tuple(pending.items()):
            if before.get((basis.school_id, basis.assignment_id)) != basis.assignment_epoch:
                results[index] = GovernanceBasisInvalid(
                    "assignment_epoch_changed", "Governance assignment epoch has changed."
                )
                del pending[index]
        queries = tuple(
            GovernanceAuthorityQuery(
                school_id=basis.school_id,
                assignment_id=basis.assignment_id,
                duty_code=basis.duty_code,
                scope_id=basis.scope_id,
                as_of=as_of,
            )
            for basis in pending.values()
        )
        currents = self._authority_reader.resolve_many(queries) if queries else ()
        for (index, basis), current in zip(tuple(pending.items()), currents, strict=True):
            error: GovernanceBasisInvalid | None = None
            if isinstance(current, GovernanceAuthorityUnavailable):
                error = GovernanceBasisInvalid(
                    "authority_unavailable", "Governance authority is no longer available."
                )
                error.__cause__ = current
            elif current.assignment_epoch != basis.assignment_epoch:
                error = GovernanceBasisInvalid(
                    "assignment_epoch_changed", "Governance assignment epoch has changed."
                )
            elif (
                current.grant_id != basis.grant_id
                or current.grant_version != basis.grant_version
                or _dependency_map(current.dependency_versions)
                != _dependency_map(basis.dependency_versions)
            ):
                error = GovernanceBasisInvalid(
                    "dependency_version_changed", "Governance dependencies have changed."
                )
            if error is not None:
                results[index] = error
                del pending[index]
        # A separate fresh read protects changes committed while owner calls ran.
        after = self._read_assignment_epochs(tuple(pending.values()))
        for index, basis in pending.items():
            if after.get((basis.school_id, basis.assignment_id)) != basis.assignment_epoch:
                results[index] = GovernanceBasisInvalid(
                    "assignment_epoch_changed", "Governance epoch changed during validation."
                )
            else:
                results[index] = True
        return tuple(results[index] for index in range(len(bases)))

    def _read_assignment_epochs(
        self, bases: tuple[GovernanceAuthorizationBasis, ...]
    ) -> dict[tuple[UUID, UUID], int]:
        identities = tuple(dict.fromkeys((basis.school_id, basis.assignment_id) for basis in bases))
        if not identities:
            return {}
        with self._session_factory() as session:
            rows = session.execute(
                select(
                    GovernanceAssignmentEpoch.school_id,
                    GovernanceAssignmentEpoch.assignment_id,
                    GovernanceAssignmentEpoch.epoch,
                ).where(
                    or_(
                        *(
                            and_(
                                GovernanceAssignmentEpoch.school_id == school_id,
                                GovernanceAssignmentEpoch.assignment_id == assignment_id,
                            )
                            for school_id, assignment_id in identities
                        )
                    )
                )
            ).all()
        return {(school_id, assignment_id): epoch for school_id, assignment_id, epoch in rows}

    def _read_assignment_epoch(self, school_id: UUID, assignment_id: UUID) -> int | None:
        with self._session_factory() as session:
            return session.scalar(
                select(GovernanceAssignmentEpoch.epoch)
                .where(
                    GovernanceAssignmentEpoch.school_id == school_id,
                    GovernanceAssignmentEpoch.assignment_id == assignment_id,
                )
                .limit(1)
            )


def _dependency_map(
    values: tuple[GovernanceDependencyVersion, ...],
) -> dict[tuple[str, str], int]:
    return {(value.authority, value.reference): value.version for value in values}


__all__ = ["GovernanceAuthorizationBasisService", "GovernanceBasisInvalid"]
