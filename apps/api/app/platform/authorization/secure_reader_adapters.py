# pyright: strict

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Generator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, cast
from uuid import UUID, uuid4

from sqlalchemy.orm import Session

from app.platform.contracts import StaffActorContext
from app.platform.governance.contracts import GovernanceDependencyVersion

from .secure_reader_compiler import CompiledReadRelation
from .secure_reader_contracts import ReadAuthorization
from .secure_reader_registry import ReadOperation
from .thin_contracts import (
    ThinAuthorizationBasis,
    ThinAuthorizationDecision,
    ThinAuthorizationRequest,
    ThinDecisionKind,
    ThinSelfAuthorizationBasis,
)


class _SqlAlchemyReadTransaction:
    def __init__(self, session: Session) -> None:
        self._session = session

    def count(self, relation: CompiledReadRelation) -> int:
        value = self._session.scalar(relation.count_statement)
        return 0 if value is None else int(value)

    def fetch(self, relation: CompiledReadRelation) -> Sequence[Mapping[str, object]]:
        rows = self._session.execute(relation.data_statement).mappings().all()
        return tuple(dict(row) for row in rows)

    def metadata(self, relation: CompiledReadRelation) -> tuple[tuple[object, ...], ...]:
        if relation.metadata_statement is None:
            raise ValueError("Read metadata projection unavailable")
        return tuple(tuple(row) for row in self._session.execute(relation.metadata_statement))

    def count_metadata(
        self, relation: CompiledReadRelation
    ) -> tuple[int, tuple[tuple[object, ...], ...]]:
        if relation.counted_metadata_statement is None:
            return self.count(relation), self.metadata(relation)
        rows = self._session.execute(relation.counted_metadata_statement).all()
        # The aggregate always supplies one row, including an empty page. The
        # presence marker distinguishes that row from nullable metadata fields.
        return int(rows[0][0]), tuple(tuple(row[2:]) for row in rows if row[1])


class SqlAlchemyRepeatableReadExecutor:
    """Execute one bounded count/data pair in one real REPEATABLE READ transaction."""

    def __init__(self, session_factory: Callable[[], Session]) -> None:
        self._session_factory = session_factory

    @contextmanager
    def repeatable_read(self) -> Generator[_SqlAlchemyReadTransaction]:
        session = self._session_factory()
        try:
            session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
            yield _SqlAlchemyReadTransaction(session)
            session.commit()
        except BaseException:
            session.rollback()
            raise
        finally:
            session.close()


@dataclass(frozen=True, slots=True)
class _IssuedReadAuthorization(ReadAuthorization):
    operation_code: str
    projection_name: str
    resource_revision: int


class ThinAuthorizationReadAdapter:
    """Mechanically adapt the existing Thin Authorization service to secure reads."""

    def __init__(
        self,
        *,
        thin_service: object,
        scope_id_resolver: Callable[[ReadOperation, StaffActorContext], UUID] | None = None,
    ) -> None:
        self._thin_service = thin_service
        self._scope_id_resolver = scope_id_resolver

    @property
    def supports_batch(self) -> bool:
        return callable(getattr(self._thin_service, "decide_many", None)) and callable(
            getattr(self._thin_service, "revalidate_many", None)
        )

    def decide_many(
        self, *, operations: tuple[ReadOperation, ...], actor: StaffActorContext, as_of: datetime
    ) -> tuple[ReadAuthorization, ...]:
        if len(operations) > 128:
            raise ValueError("Read authorization batch exceeds the fixed bound")
        if not self.supports_batch:
            return tuple(self.decide(operation=op, actor=actor, as_of=as_of) for op in operations)
        pending: list[tuple[int, ReadOperation, ThinAuthorizationRequest]] = []
        results = [ReadAuthorization(False, None, None, None, None) for _ in operations]
        if type(actor) is not StaffActorContext or self._scope_id_resolver is None:
            return tuple(results)
        for index, operation in enumerate(operations):
            try:
                scope_id = self._scope_id_resolver(operation, actor)
            except Exception:
                continue
            if type(scope_id) is not UUID:
                continue
            request = ThinAuthorizationRequest(
                actor=actor,
                module_name=operation.module_name,
                action_code=operation.action_code,
                scope_id=scope_id,
                resource_type=operation.code,
                resource_id=UUID(str(actor.school_id)),
                resource_revision=actor.authorization_version,
                request_id=uuid4(),
                as_of=as_of,
            )
            pending.append((index, operation, request))
        decisions = cast(Any, self._thin_service).decide_many(tuple(r for _, _, r in pending))
        if len(decisions) != len(pending):
            raise ValueError("Incomplete read authorization batch")
        for (index, operation, request), decision in zip(pending, decisions, strict=True):
            if not _matches_operation(
                decision, operation, request=request, scope_id=request.scope_id
            ):
                continue
            fingerprint = _fingerprint_authorization_basis(decision.basis)
            if fingerprint is not None:
                results[index] = _IssuedReadAuthorization(
                    True,
                    decision.basis,
                    decision.scope_plan,
                    decision.projection,
                    fingerprint,
                    operation.code,
                    operation.projection_name,
                    actor.authorization_version,
                )
        return tuple(results)

    def revalidate_many(
        self,
        authorizations: tuple[ReadAuthorization, ...],
        *,
        actor: StaffActorContext,
        as_of: datetime,
    ) -> tuple[bool | Exception, ...]:
        if len(authorizations) > 128:
            raise ValueError("Read authorization batch exceeds the fixed bound")
        if not self.supports_batch:
            return tuple(self.revalidate(auth, actor=actor, as_of=as_of) for auth in authorizations)
        pending: list[tuple[int, _IssuedReadAuthorization]] = []
        results: list[bool | Exception] = [False] * len(authorizations)
        if type(actor) is not StaffActorContext:
            return tuple(results)
        for index, authorization in enumerate(authorizations):
            if (
                isinstance(authorization, _IssuedReadAuthorization)
                and isinstance(
                    authorization.basis, (ThinAuthorizationBasis, ThinSelfAuthorizationBasis)
                )
                and _basis_matches_actor_context(
                    authorization.basis, actor, resource_revision=authorization.resource_revision
                )
            ):
                pending.append((index, authorization))
        decisions = cast(Any, self._thin_service).revalidate_many(
            tuple(auth.basis for _, auth in pending),
            actor=actor,
            as_of=as_of,
            resource_revision=actor.authorization_version,
        )
        if len(decisions) != len(pending):
            raise ValueError("Incomplete read revalidation batch")
        for (index, authorization), decision in zip(pending, decisions, strict=True):
            if isinstance(decision, Exception):
                results[index] = decision
                continue
            results[index] = (
                _is_allow(decision)
                and decision.projection == authorization.projection
                and decision.scope_plan == authorization.scope_plan
                and _fingerprint_authorization_basis(decision.basis)
                == authorization.basis_fingerprint
            )
        return tuple(results)

    def decide(
        self,
        *,
        operation: ReadOperation,
        actor: StaffActorContext,
        as_of: datetime,
    ) -> ReadAuthorization:
        if type(actor) is not StaffActorContext:
            return ReadAuthorization(
                allowed=False,
                basis=None,
                scope_plan=None,
                projection=None,
                basis_fingerprint=None,
            )
        if self._scope_id_resolver is None:
            return ReadAuthorization(
                allowed=False,
                basis=None,
                scope_plan=None,
                projection=None,
                basis_fingerprint=None,
            )
        try:
            scope_id = self._scope_id_resolver(operation, actor)
        except Exception:
            return ReadAuthorization(
                allowed=False,
                basis=None,
                scope_plan=None,
                projection=None,
                basis_fingerprint=None,
            )
        if type(scope_id) is not UUID:
            return ReadAuthorization(
                allowed=False,
                basis=None,
                scope_plan=None,
                projection=None,
                basis_fingerprint=None,
            )
        school_id = UUID(str(actor.school_id))
        request = ThinAuthorizationRequest(
            actor=actor,
            module_name=operation.module_name,
            action_code=operation.action_code,
            scope_id=scope_id,
            resource_type=operation.code,
            resource_id=school_id,
            resource_revision=actor.authorization_version,
            request_id=uuid4(),
            as_of=as_of,
        )
        decision = cast(Any, self._thin_service).decide(request)
        if not _matches_operation(
            decision,
            operation,
            request=request,
            scope_id=scope_id,
        ):
            return ReadAuthorization(
                allowed=False,
                basis=None,
                scope_plan=None,
                projection=None,
                basis_fingerprint=None,
            )
        basis_fingerprint = _fingerprint_authorization_basis(decision.basis)
        if basis_fingerprint is None:
            return ReadAuthorization(
                allowed=False,
                basis=None,
                scope_plan=None,
                projection=None,
                basis_fingerprint=None,
            )
        return _IssuedReadAuthorization(
            allowed=True,
            basis=decision.basis,
            scope_plan=decision.scope_plan,
            projection=decision.projection,
            basis_fingerprint=basis_fingerprint,
            operation_code=operation.code,
            projection_name=operation.projection_name,
            resource_revision=actor.authorization_version,
        )

    def revalidate(
        self,
        authorization: object,
        *,
        actor: StaffActorContext,
        as_of: datetime,
    ) -> bool:
        if type(actor) is not StaffActorContext or not isinstance(
            authorization, _IssuedReadAuthorization
        ):
            return False
        basis = authorization.basis
        if not isinstance(
            basis, (ThinAuthorizationBasis, ThinSelfAuthorizationBasis)
        ) or not _basis_matches_actor_context(
            basis,
            actor,
            resource_revision=authorization.resource_revision,
        ):
            return False
        decision = cast(Any, self._thin_service).revalidate(
            basis,
            actor=actor,
            as_of=as_of,
            resource_revision=authorization.resource_revision,
        )
        return (
            _is_allow(decision)
            and decision.projection == authorization.projection
            and decision.scope_plan == authorization.scope_plan
            and _fingerprint_authorization_basis(decision.basis) == authorization.basis_fingerprint
        )


def _matches_operation(
    decision: ThinAuthorizationDecision,
    operation: ReadOperation,
    *,
    request: ThinAuthorizationRequest,
    scope_id: UUID,
) -> bool:
    basis = decision.basis
    return (
        _is_allow(decision)
        and isinstance(basis, (ThinAuthorizationBasis, ThinSelfAuthorizationBasis))
        and decision.scope_plan is not None
        and decision.projection is not None
        and _basis_matches_actor_context(
            basis,
            request.actor,
            resource_revision=request.resource_revision,
        )
        and basis.request_id == request.request_id
        and basis.module_name == request.module_name
        and basis.action_code == request.action_code
        and basis.resource_type == request.resource_type
        and basis.resource_id == request.resource_id
        and (
            basis.scope_id
            if isinstance(basis, ThinSelfAuthorizationBasis)
            else basis.governance.scope_id
        )
        == scope_id
        and decision.scope_plan.scope_id == scope_id
        and decision.projection.name == operation.projection_name
        and decision.projection.version == 1
    )


def _basis_matches_actor_context(
    basis: ThinAuthorizationBasis | ThinSelfAuthorizationBasis,
    actor: StaffActorContext,
    *,
    resource_revision: int,
) -> bool:
    actor_basis = basis.actor
    governance_basis = None if isinstance(basis, ThinSelfAuthorizationBasis) else basis.governance
    return (
        actor_basis.account_user_id == actor.account_user_id
        and actor_basis.school_id == actor.school_id
        and actor_basis.person_id == actor.person_id
        and actor_basis.assignment_id == actor.assignment_id
        and actor_basis.assignment_version == actor.assignment_version
        and actor_basis.authorization_version == actor.authorization_version
        and (
            governance_basis is None
            or (
                governance_basis.school_id == UUID(str(actor.school_id))
                and governance_basis.assignment_id == UUID(str(actor.assignment_id))
            )
        )
        and basis.resource_revision == resource_revision
    )


def _is_allow(decision: ThinAuthorizationDecision) -> bool:
    return decision.kind is ThinDecisionKind.ALLOW


def authorization_basis_fingerprint(basis: object) -> str | None:
    """Stable private evidence identity; does not authorize a read."""
    return _fingerprint_authorization_basis(basis)


def _fingerprint_authorization_basis(basis: object) -> str | None:
    if not isinstance(basis, (ThinAuthorizationBasis, ThinSelfAuthorizationBasis)):
        return None
    try:
        canonical = (
            {k: v for k, v in asdict(basis).items() if k not in {"request_id", "decision_id"}}
            if isinstance(basis, ThinSelfAuthorizationBasis)
            else _stable_thin_authorization_basis(basis)
        )
        payload = json.dumps(
            canonical,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            default=str,
        ).encode("utf-8")
    except (TypeError, ValueError):
        return None
    return hashlib.sha256(payload).hexdigest()


def _stable_thin_authorization_basis(basis: ThinAuthorizationBasis) -> dict[str, object]:
    governance = basis.governance
    dependencies = tuple(
        sorted(
            governance.dependency_versions,
            key=lambda dependency: (
                dependency.authority,
                dependency.reference,
                dependency.version,
            ),
        )
    )
    duty = _required_governance_dependency(dependencies, "governance.duty")
    scope = _required_governance_dependency(dependencies, "governance.scope")
    grant = _required_governance_dependency(dependencies, "governance.grant")
    assignment_epoch = _required_governance_dependency(
        dependencies,
        "governance.assignment_epoch",
    )
    duty_id = UUID(duty.reference)
    scope_id = UUID(scope.reference)
    grant_id = UUID(grant.reference)
    epoch_assignment_id = UUID(assignment_epoch.reference)
    if (
        scope_id != governance.scope_id
        or grant_id != governance.grant_id
        or grant.version != governance.grant_version
        or epoch_assignment_id != governance.assignment_id
        or assignment_epoch.version != governance.assignment_epoch
    ):
        raise ValueError("Governance authorization basis dependencies are inconsistent.")

    return {
        "actor": {
            "account_user_id": basis.actor.account_user_id,
            "school_id": str(basis.actor.school_id),
            "person_id": str(basis.actor.person_id),
            "assignment_id": str(basis.actor.assignment_id),
            "assignment_version": basis.actor.assignment_version,
            "authorization_version": basis.actor.authorization_version,
        },
        "module": {
            "name": basis.module_name,
            "action_code": basis.action_code,
            "policy_version": basis.module_policy_version,
        },
        "resource": {
            "type": basis.resource_type,
            "id": str(basis.resource_id),
            "revision": basis.resource_revision,
        },
        "governance": {
            "school_id": str(governance.school_id),
            "assignment_id": str(governance.assignment_id),
            "duty": {
                "code": governance.duty_code,
                "id": str(duty_id),
                "version": duty.version,
            },
            "scope": {
                "id": str(scope_id),
                "version": scope.version,
            },
            "grant": {
                "id": str(grant_id),
                "version": grant.version,
            },
            "assignment_epoch": governance.assignment_epoch,
            "valid_until": (
                governance.valid_until.isoformat() if governance.valid_until is not None else None
            ),
            "dependency_versions": [
                {
                    "authority": dependency.authority,
                    "reference": dependency.reference,
                    "version": dependency.version,
                }
                for dependency in dependencies
            ],
        },
    }


def _required_governance_dependency(
    dependencies: Sequence[GovernanceDependencyVersion],
    authority: str,
) -> GovernanceDependencyVersion:
    matches = tuple(dependency for dependency in dependencies if dependency.authority == authority)
    if len(matches) != 1:
        raise ValueError("Governance authorization basis dependency is missing or ambiguous.")
    return matches[0]


__all__ = ["SqlAlchemyRepeatableReadExecutor", "ThinAuthorizationReadAdapter"]
