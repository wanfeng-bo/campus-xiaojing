# pyright: strict

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import cast
from uuid import UUID

from sqlalchemy import func, literal, or_, select, union_all
from sqlalchemy.orm import Session, sessionmaker

from app.platform.governance.contracts import (
    AssignmentAuthoritySnapshot,
    GovernanceAuthorityQuery,
    GovernanceAuthoritySnapshot,
    GovernanceDependencyVersion,
    RootAuthoritySnapshot,
    ScopeResolutionRequest,
    ScopeResolutionSnapshot,
)
from app.platform.governance.models import (
    GovernanceAssignmentEpoch,
    GovernanceDuty,
    GovernanceDutyVersion,
    GovernanceGrant,
    GovernanceGrantVersion,
    GovernanceScope,
    GovernanceScopeVersion,
)
from app.platform.governance.ports import (
    GovernanceBundledDependencyPort,
    GovernanceDependencyPort,
    GovernanceScopeOwnerResolver,
)


class GovernanceAuthorityUnavailable(RuntimeError):
    """A requested authority cannot be interpreted uniquely and safely."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class _StoredAuthority:
    school_id: UUID
    assignment_id: UUID
    duty_code: str
    scope_id: UUID
    grant_id: UUID
    grant_version: int
    duty_id: UUID
    duty_version: int
    scope_version: int
    assignment_epoch: int
    valid_from: datetime
    effective_from: datetime
    planned_valid_to: datetime | None
    basis_authority: str
    basis_reference: str
    schema_owner: str
    schema_name: str
    schema_version: int
    reference_document: dict[str, object]
    reference_sha256: str
    next_grant_effective_from: datetime | None


class GovernanceAuthorityReader:
    """Reads Governance authority and validates every owner-domain dependency."""

    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        dependency_port: GovernanceDependencyPort,
        scope_owner_resolvers: Mapping[str, GovernanceScopeOwnerResolver],
    ) -> None:
        self._session_factory = session_factory
        self._dependency_port = dependency_port
        self._scope_owner_resolvers = dict(scope_owner_resolvers)

    def resolve(self, query: GovernanceAuthorityQuery) -> GovernanceAuthoritySnapshot:
        stored = self._read_stored_authority(query)
        assignment, person, organization = self._read_dependencies(query)
        scope = self._resolve_scope(query, stored)
        return self._snapshot(stored, assignment, person, organization, scope)

    def resolve_many(
        self, queries: tuple[GovernanceAuthorityQuery, ...]
    ) -> tuple[GovernanceAuthoritySnapshot | GovernanceAuthorityUnavailable, ...]:
        stored_values = self._read_stored_authorities(queries)
        results: dict[int, GovernanceAuthoritySnapshot | GovernanceAuthorityUnavailable] = {}
        prepared: dict[
            int,
            tuple[
                _StoredAuthority,
                AssignmentAuthoritySnapshot,
                RootAuthoritySnapshot,
                RootAuthoritySnapshot,
            ],
        ] = {}
        owner_groups: dict[str, list[int]] = {}
        for index, (query, stored) in enumerate(zip(queries, stored_values, strict=True)):
            if isinstance(stored, GovernanceAuthorityUnavailable):
                results[index] = stored
                continue
            try:
                assignment, person, organization = self._read_dependencies(query)
            except GovernanceAuthorityUnavailable as exc:
                results[index] = exc
                continue
            prepared[index] = (stored, assignment, person, organization)
            owner_groups.setdefault(stored.schema_owner, []).append(index)
        for owner, indices in owner_groups.items():
            resolver = self._scope_owner_resolvers.get(owner)
            batch_resolve = getattr(resolver, "resolve_many", None)
            if callable(batch_resolve):
                requests = tuple(
                    self._scope_request(queries[index], prepared[index][0]) for index in indices
                )
                try:
                    values = cast(
                        Callable[
                            [tuple[ScopeResolutionRequest, ...]],
                            tuple[ScopeResolutionSnapshot | None | Exception, ...],
                        ],
                        batch_resolve,
                    )(requests)
                    if len(values) != len(indices):
                        raise ValueError("Scope owner batch cardinality changed.")
                except Exception:
                    values = tuple(
                        GovernanceAuthorityUnavailable("dependency_error", "Scope owner failed.")
                        for _ in indices
                    )
                for index, value in zip(indices, values, strict=True):
                    try:
                        if isinstance(value, Exception):
                            raise GovernanceAuthorityUnavailable(
                                "dependency_error", "Scope owner failed."
                            ) from value
                        scope = self._require_scope(value, queries[index], prepared[index][0])
                        results[index] = self._snapshot(*prepared[index], scope)
                    except GovernanceAuthorityUnavailable as exc:
                        results[index] = exc
            else:
                for index in indices:
                    try:
                        scope = self._resolve_scope(queries[index], prepared[index][0])
                        results[index] = self._snapshot(*prepared[index], scope)
                    except GovernanceAuthorityUnavailable as exc:
                        results[index] = exc
        return tuple(results[index] for index in range(len(queries)))

    @staticmethod
    def _snapshot(
        stored: _StoredAuthority,
        assignment: AssignmentAuthoritySnapshot,
        person: RootAuthoritySnapshot,
        organization: RootAuthoritySnapshot,
        scope: ScopeResolutionSnapshot,
    ) -> GovernanceAuthoritySnapshot:
        valid_until = _minimum_boundary(
            stored.next_grant_effective_from,
            stored.planned_valid_to,
            assignment.valid_until,
            person.valid_until,
            organization.valid_until,
            scope.valid_until,
        )
        dependencies = tuple(
            sorted(
                (
                    GovernanceDependencyVersion(
                        "school_facts.assignment", str(assignment.assignment_id), assignment.version
                    ),
                    GovernanceDependencyVersion(
                        "school_facts.person", str(person.root_id), person.version
                    ),
                    GovernanceDependencyVersion(
                        "school_facts.organization",
                        str(organization.root_id),
                        organization.version,
                    ),
                    GovernanceDependencyVersion(
                        f"{scope.owner}.scope-target", scope.reference, scope.version
                    ),
                    GovernanceDependencyVersion(
                        "governance.duty", str(stored.duty_id), stored.duty_version
                    ),
                    GovernanceDependencyVersion(
                        "governance.scope", str(stored.scope_id), stored.scope_version
                    ),
                    GovernanceDependencyVersion(
                        "governance.grant", str(stored.grant_id), stored.grant_version
                    ),
                    GovernanceDependencyVersion(
                        "governance.assignment_epoch",
                        str(stored.assignment_id),
                        stored.assignment_epoch,
                    ),
                ),
                key=lambda item: (item.authority, item.reference),
            )
        )
        return GovernanceAuthoritySnapshot(
            school_id=stored.school_id,
            assignment_id=stored.assignment_id,
            duty_code=stored.duty_code,
            scope_id=stored.scope_id,
            grant_id=stored.grant_id,
            grant_version=stored.grant_version,
            duty_id=stored.duty_id,
            duty_version=stored.duty_version,
            scope_version=stored.scope_version,
            assignment_epoch=stored.assignment_epoch,
            valid_from=stored.valid_from,
            effective_from=stored.effective_from,
            planned_valid_to=stored.planned_valid_to,
            basis_authority=stored.basis_authority,
            basis_reference=stored.basis_reference,
            valid_until=valid_until,
            dependency_versions=dependencies,
        )

    def _read_stored_authority(self, query: GovernanceAuthorityQuery) -> _StoredAuthority:
        value = self._read_stored_authorities((query,))[0]
        if isinstance(value, GovernanceAuthorityUnavailable):
            raise value
        return value

    def _read_stored_authorities(
        self, queries: tuple[GovernanceAuthorityQuery, ...]
    ) -> tuple[_StoredAuthority | GovernanceAuthorityUnavailable, ...]:
        if len(queries) > 128:
            raise ValueError("Governance authority batches are limited to 128 queries.")
        if not queries:
            return ()
        # Preserve every exact query, including its clock, in one committed snapshot.
        requests = union_all(
            *(
                select(
                    literal(index).label("query_index"),
                    literal(query.school_id).label("school_id"),
                    literal(query.assignment_id).label("assignment_id"),
                    literal(query.duty_code).label("duty_code"),
                    literal(query.scope_id).label("scope_id"),
                    literal(query.as_of).label("as_of"),
                )
                for index, query in enumerate(queries)
            )
        ).cte("authority_requests")
        ranked_duties = (
            select(
                requests.c.query_index,
                GovernanceDuty.id,
                func.row_number()
                .over(partition_by=requests.c.query_index, order_by=GovernanceDuty.id)
                .label("rank"),
            )
            .select_from(requests)
            .join(
                GovernanceDuty,
                (GovernanceDuty.school_id == requests.c.school_id)
                & (GovernanceDuty.duty_code == requests.c.duty_code),
            )
            .cte("authority_ranked_duties")
        )
        duties = (
            select(ranked_duties.c.query_index, ranked_duties.c.id)
            .where(ranked_duties.c.rank <= 2)
            .cte("authority_duties")
        )
        selected_version_id = (
            select(GovernanceGrantVersion.id)
            .where(
                GovernanceGrantVersion.school_id == GovernanceGrant.school_id,
                GovernanceGrantVersion.grant_id == GovernanceGrant.id,
                GovernanceGrantVersion.effective_from <= requests.c.as_of,
            )
            .order_by(
                GovernanceGrantVersion.transition_kind.in_(("end", "revoke")).desc(),
                GovernanceGrantVersion.effective_from.desc(),
                GovernanceGrantVersion.version.desc(),
            )
            .limit(1)
            .correlate(GovernanceGrant, requests)
            .scalar_subquery()
        )
        ranked_candidates = (
            select(
                requests.c.query_index,
                GovernanceGrant.id.label("grant_id"),
                GovernanceGrant.duty_id,
                GovernanceGrantVersion.id.label("version_id"),
                func.row_number()
                .over(partition_by=requests.c.query_index, order_by=GovernanceGrant.id)
                .label("rank"),
            )
            .select_from(requests)
            .join(duties, duties.c.query_index == requests.c.query_index)
            .join(GovernanceGrant, GovernanceGrant.duty_id == duties.c.id)
            .join(GovernanceGrantVersion, GovernanceGrantVersion.id == selected_version_id)
            .where(
                GovernanceGrant.school_id == requests.c.school_id,
                GovernanceGrant.assignment_id == requests.c.assignment_id,
                GovernanceGrantVersion.scope_id == requests.c.scope_id,
                GovernanceGrantVersion.transition_kind.in_(("create", "update")),
                GovernanceGrantVersion.valid_from <= requests.c.as_of,
                or_(
                    GovernanceGrantVersion.planned_valid_to.is_(None),
                    GovernanceGrantVersion.planned_valid_to > requests.c.as_of,
                ),
                GovernanceGrantVersion.formal_ended_at.is_(None),
                GovernanceGrantVersion.revoked_at.is_(None),
            )
            .cte("authority_ranked_candidates")
        )
        candidates = (
            select(ranked_candidates)
            .where(ranked_candidates.c.rank <= 2)
            .cte("authority_candidates")
        )
        epoch_query = (
            select(GovernanceAssignmentEpoch.epoch)
            .where(
                GovernanceAssignmentEpoch.school_id == requests.c.school_id,
                GovernanceAssignmentEpoch.assignment_id == requests.c.assignment_id,
            )
            .limit(1)
            .scalar_subquery()
        )
        next_effective_query = (
            select(func.min(GovernanceGrantVersion.effective_from))
            .where(
                GovernanceGrantVersion.school_id == requests.c.school_id,
                GovernanceGrantVersion.grant_id == candidates.c.grant_id,
                GovernanceGrantVersion.effective_from > requests.c.as_of,
            )
            .correlate(candidates, requests)
            .scalar_subquery()
        )
        statement = (
            select(
                requests.c.query_index,
                select(func.count())
                .select_from(duties)
                .where(duties.c.query_index == requests.c.query_index)
                .correlate(requests)
                .scalar_subquery(),
                select(func.count())
                .select_from(candidates)
                .where(candidates.c.query_index == requests.c.query_index)
                .correlate(requests)
                .scalar_subquery(),
                candidates.c.grant_id,
                candidates.c.duty_id,
                GovernanceGrantVersion,
                GovernanceDutyVersion,
                GovernanceScope,
                GovernanceScopeVersion,
                epoch_query,
                next_effective_query,
            )
            .select_from(requests)
            .outerjoin(candidates, candidates.c.query_index == requests.c.query_index)
            .outerjoin(GovernanceGrantVersion, GovernanceGrantVersion.id == candidates.c.version_id)
            .outerjoin(
                GovernanceDutyVersion,
                (GovernanceDutyVersion.school_id == requests.c.school_id)
                & (GovernanceDutyVersion.duty_id == candidates.c.duty_id)
                & (GovernanceDutyVersion.version == GovernanceGrantVersion.duty_version)
                & (GovernanceDutyVersion.effective_from <= requests.c.as_of)
                & GovernanceDutyVersion.is_active.is_(True),
            )
            .outerjoin(
                GovernanceScope,
                (GovernanceScope.school_id == requests.c.school_id)
                & (GovernanceScope.id == requests.c.scope_id),
            )
            .outerjoin(
                GovernanceScopeVersion,
                (GovernanceScopeVersion.school_id == requests.c.school_id)
                & (GovernanceScopeVersion.scope_id == GovernanceScope.id)
                & (GovernanceScopeVersion.version == GovernanceGrantVersion.scope_version)
                & (GovernanceScopeVersion.effective_from <= requests.c.as_of)
                & GovernanceScopeVersion.is_active.is_(True),
            )
        )
        # Cardinality is decided before optional references; a broken competing
        # Grant remains visible. At most two candidates are returned per query.
        results: dict[int, _StoredAuthority | GovernanceAuthorityUnavailable] = {}
        with self._session_factory() as session:
            rows = session.execute(statement).all()
        for row in rows:
            (
                index,
                duty_count,
                candidate_count,
                grant_id,
                duty_id,
                grant_version,
                duty_version,
                scope_root,
                scope_version,
                epoch,
                next_effective,
            ) = row
            if index in results:
                continue
            query = queries[cast(int, index)]
            if duty_count != 1:
                reason = "grant_ambiguous" if duty_count else "grant_inactive"
                results[index] = GovernanceAuthorityUnavailable(
                    reason, "Governance duty is unavailable."
                )
                continue
            if candidate_count != 1:
                reason = "grant_ambiguous" if candidate_count else "grant_inactive"
                results[index] = GovernanceAuthorityUnavailable(
                    reason, "Governance Grant is unavailable."
                )
                continue
            if any(
                value is None
                for value in (grant_version, duty_version, scope_root, scope_version, epoch)
            ):
                results[index] = GovernanceAuthorityUnavailable(
                    "grant_inactive", "Governance Grant references are unavailable."
                )
                continue
            results[index] = _StoredAuthority(
                school_id=query.school_id,
                assignment_id=query.assignment_id,
                duty_code=query.duty_code,
                scope_id=query.scope_id,
                grant_id=grant_id,
                grant_version=grant_version.version,
                duty_id=duty_id,
                duty_version=duty_version.version,
                scope_version=scope_version.version,
                assignment_epoch=epoch,
                valid_from=grant_version.valid_from,
                effective_from=grant_version.effective_from,
                planned_valid_to=grant_version.planned_valid_to,
                basis_authority=grant_version.basis_authority,
                basis_reference=grant_version.basis_reference,
                schema_owner=scope_version.schema_owner,
                schema_name=scope_version.schema_name,
                schema_version=scope_version.schema_version,
                reference_document=scope_version.reference_document,
                reference_sha256=scope_version.reference_sha256,
                next_grant_effective_from=next_effective,
            )
        return tuple(results[index] for index in range(len(queries)))

    def _read_dependencies(
        self, query: GovernanceAuthorityQuery
    ) -> tuple[AssignmentAuthoritySnapshot, RootAuthoritySnapshot, RootAuthoritySnapshot]:
        if callable(getattr(self._dependency_port, "read_authority_roots", None)):
            assignment_value = person_value = organization_value = None
            try:
                values = cast(
                    GovernanceBundledDependencyPort, self._dependency_port
                ).read_authority_roots(
                    school_id=query.school_id, assignment_id=query.assignment_id, as_of=query.as_of
                )
                if values is not None:
                    assignment_value, person_value, organization_value = values
            except Exception as exc:
                # A failed combined read is a failed owner, never a retry signal.
                raise GovernanceAuthorityUnavailable(
                    "dependency_error", "Root owner failed."
                ) from exc
            if values is not None:
                assignment = self._require_assignment(assignment_value, query)
                person = self._require_root(
                    person_value, query=query, root_kind="person", root_id=assignment.person_id
                )
                organization = self._require_root(
                    organization_value,
                    query=query,
                    root_kind="organization",
                    root_id=assignment.organization_id,
                )
                return assignment, person, organization
        assignment = self._read_assignment(query)
        return (
            assignment,
            self._read_person(query, assignment),
            self._read_organization(query, assignment),
        )

    def _read_assignment(self, query: GovernanceAuthorityQuery) -> AssignmentAuthoritySnapshot:
        try:
            value = self._dependency_port.read_assignment(
                school_id=query.school_id,
                assignment_id=query.assignment_id,
                as_of=query.as_of,
            )
        except Exception as exc:
            raise GovernanceAuthorityUnavailable(
                "dependency_error", "Assignment owner failed."
            ) from exc
        return self._require_assignment(value, query)

    @staticmethod
    def _require_assignment(
        value: AssignmentAuthoritySnapshot | None, query: GovernanceAuthorityQuery
    ) -> AssignmentAuthoritySnapshot:
        if value is None:
            raise GovernanceAuthorityUnavailable("dependency_missing", "Assignment is missing.")
        if value.is_ambiguous:
            raise GovernanceAuthorityUnavailable("dependency_ambiguous", "Assignment is ambiguous.")
        if (
            value.school_id != query.school_id
            or value.assignment_id != query.assignment_id
            or value.version < 1
            or not value.is_effective
            or _at_or_after_boundary(query.as_of, value.valid_until)
        ):
            raise GovernanceAuthorityUnavailable("assignment_invalid", "Assignment is invalid.")
        return value

    def _read_person(
        self, query: GovernanceAuthorityQuery, assignment: AssignmentAuthoritySnapshot
    ) -> RootAuthoritySnapshot:
        try:
            value = self._dependency_port.read_person_root(
                school_id=query.school_id, person_id=assignment.person_id, as_of=query.as_of
            )
        except Exception as exc:
            raise GovernanceAuthorityUnavailable(
                "dependency_error", "Person owner failed."
            ) from exc
        return self._require_root(
            value, query=query, root_kind="person", root_id=assignment.person_id
        )

    def _read_organization(
        self, query: GovernanceAuthorityQuery, assignment: AssignmentAuthoritySnapshot
    ) -> RootAuthoritySnapshot:
        try:
            value = self._dependency_port.read_organization_root(
                school_id=query.school_id,
                organization_id=assignment.organization_id,
                as_of=query.as_of,
            )
        except Exception as exc:
            raise GovernanceAuthorityUnavailable(
                "dependency_error", "Organization owner failed."
            ) from exc
        return self._require_root(
            value,
            query=query,
            root_kind="organization",
            root_id=assignment.organization_id,
        )

    @staticmethod
    def _require_root(
        value: RootAuthoritySnapshot | None,
        *,
        query: GovernanceAuthorityQuery,
        root_kind: str,
        root_id: UUID,
    ) -> RootAuthoritySnapshot:
        if value is None:
            raise GovernanceAuthorityUnavailable("dependency_missing", f"{root_kind} is missing.")
        if value.is_ambiguous:
            raise GovernanceAuthorityUnavailable(
                "dependency_ambiguous", f"{root_kind} is ambiguous."
            )
        if (
            value.school_id != query.school_id
            or value.root_kind != root_kind
            or value.root_id != root_id
            or value.version < 1
            or not value.is_effective
            or _at_or_after_boundary(query.as_of, value.valid_until)
        ):
            raise GovernanceAuthorityUnavailable(f"{root_kind}_invalid", f"{root_kind} is invalid.")
        return value

    def _resolve_scope(
        self, query: GovernanceAuthorityQuery, stored: _StoredAuthority
    ) -> ScopeResolutionSnapshot:
        resolver = self._scope_owner_resolvers.get(stored.schema_owner)
        if resolver is None:
            raise GovernanceAuthorityUnavailable("dependency_missing", "Scope owner is missing.")
        request = self._scope_request(query, stored)
        try:
            value = resolver.resolve(request)
        except Exception as exc:
            raise GovernanceAuthorityUnavailable("dependency_error", "Scope owner failed.") from exc
        return self._require_scope(value, query, stored)

    @staticmethod
    def _scope_request(
        query: GovernanceAuthorityQuery, stored: _StoredAuthority
    ) -> ScopeResolutionRequest:
        return ScopeResolutionRequest(
            school_id=query.school_id,
            scope_id=query.scope_id,
            schema_owner=stored.schema_owner,
            schema_name=stored.schema_name,
            schema_version=stored.schema_version,
            reference_document=stored.reference_document,
            reference_sha256=stored.reference_sha256,
            as_of=query.as_of,
        )

    @staticmethod
    def _require_scope(
        value: ScopeResolutionSnapshot | None,
        query: GovernanceAuthorityQuery,
        stored: _StoredAuthority,
    ) -> ScopeResolutionSnapshot:
        if value is None:
            raise GovernanceAuthorityUnavailable("dependency_missing", "Scope target is missing.")
        if value.is_ambiguous:
            raise GovernanceAuthorityUnavailable(
                "dependency_ambiguous", "Scope target is ambiguous."
            )
        if (
            value.school_id != query.school_id
            or value.owner != stored.schema_owner
            or not value.reference.strip()
            or value.version < 1
            or not value.is_effective
            or _at_or_after_boundary(query.as_of, value.valid_until)
        ):
            raise GovernanceAuthorityUnavailable("scope_invalid", "Scope target is invalid.")
        return value


def _at_or_after_boundary(as_of: datetime, boundary: datetime | None) -> bool:
    return boundary is not None and as_of >= boundary


def _minimum_boundary(*values: datetime | None) -> datetime | None:
    present = [value for value in values if value is not None]
    return min(present) if present else None


__all__ = ["GovernanceAuthorityReader", "GovernanceAuthorityUnavailable"]
