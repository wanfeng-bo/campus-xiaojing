"""F2 short authority passes share a connection, never decisions or ORM objects."""

from collections.abc import Callable, Generator
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime
from threading import get_ident
from typing import Protocol

from sqlalchemy.engine import Connection, Transaction
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.platform.contracts import StaffActorContext
from app.platform.governance.contracts import GovernanceAuthorityQuery, GovernanceAuthorizationBasis


class AuthorityBasis(Protocol):
    def issue(self, query: GovernanceAuthorityQuery) -> GovernanceAuthorizationBasis: ...
    def validate(self, basis: GovernanceAuthorizationBasis, *, as_of: datetime) -> bool: ...
    def issue_many(
        self, queries: tuple[GovernanceAuthorityQuery, ...]
    ) -> tuple[GovernanceAuthorizationBasis | Exception, ...]: ...
    def validate_many(
        self, bases: tuple[GovernanceAuthorizationBasis, ...], *, as_of: datetime
    ) -> tuple[bool | Exception, ...]: ...


@dataclass
class _ReadScope:
    actor: StaffActorContext | None
    thread: int
    connection: Connection
    transaction: Transaction
    sessions: sessionmaker[Session]
    active: bool = True

    def require_current(self) -> None:
        if not self.active:
            raise SQLAlchemyError("Authority read scope is closed")
        if self.thread != get_ident():
            raise SQLAlchemyError("Authority read scope thread changed")
        if (
            not self.transaction.is_active
            or self.connection.get_transaction() is not self.transaction
        ):
            raise SQLAlchemyError("Authority transaction changed during read")


@dataclass
class _ReadPass:
    actor: StaffActorContext
    thread: int
    stack: ExitStack
    reader: _ReadScope | None = None
    active: bool = True

    def require_current(self) -> None:
        if not self.active:
            raise SQLAlchemyError("Authority read scope is closed")
        if self.thread != get_ident():
            raise SQLAlchemyError("Authority read scope thread changed")
        if self.reader is not None:
            self.reader.require_current()


class ConnectionBoundAuthority:
    def __init__(
        self,
        *,
        sessions: sessionmaker[Session],
        build_basis: Callable[[sessionmaker[Session]], AuthorityBasis],
    ) -> None:
        self._sessions = sessions
        self._build_basis = build_basis
        self._scope: ContextVar[_ReadPass | None] = ContextVar(
            "f2_authority_connection_scope", default=None
        )
        self._borrowed: ContextVar[_ReadScope | None] = ContextVar(
            "f2_authority_borrowed_connection", default=None
        )

    @contextmanager
    def borrow_connection(self, connection: Connection) -> Generator[None]:
        """Use one reserved Reader connection during a fresh synchronous RC phase."""
        if self._borrowed.get() is not None or self._scope.get() is not None:
            raise SQLAlchemyError("Authority connection phase is already active")
        if connection.engine is not self._sessions.kw.get("bind"):
            raise SQLAlchemyError("Authority connection belongs to another engine")
        if connection.in_transaction() or connection.get_isolation_level() != "READ COMMITTED":
            raise SQLAlchemyError("Authority borrowing requires a fresh READ COMMITTED phase")
        transaction = connection.begin()
        bound = sessionmaker(
            bind=connection, expire_on_commit=False, join_transaction_mode="rollback_only"
        )
        state = _ReadScope(None, get_ident(), connection, transaction, bound)
        token = self._borrowed.set(state)
        try:
            yield
            state.require_current()
        finally:
            state.active = False
            self._borrowed.reset(token)
            connection.rollback()

    def current_sessions(self) -> sessionmaker[Session]:
        """Fresh owner probes share only the current reserved RC connection."""
        borrowed = self._borrowed.get()
        if borrowed is None:
            return self._sessions
        borrowed.require_current()
        return borrowed.sessions

    @contextmanager
    def _connection(self, actor: StaffActorContext | None) -> Generator[_ReadScope]:
        with self._sessions() as owner:
            connection = owner.connection()
            if connection.get_isolation_level() != "READ COMMITTED":
                raise SQLAlchemyError("Authority reads require READ COMMITTED")
            transaction = connection.get_transaction()
            if transaction is None:
                raise SQLAlchemyError("Authority transaction unavailable")
            bound = sessionmaker(
                bind=connection, expire_on_commit=False, join_transaction_mode="rollback_only"
            )
            state = _ReadScope(actor, get_ident(), connection, transaction, bound)
            try:
                yield state
                state.require_current()
            finally:
                state.active = False

    @contextmanager
    def read_scope(self, actor: StaffActorContext) -> Generator[None]:
        """Only the synchronous evidence decision pass enters this private scope.

        Every SQL statement still sees current committed state. The full actor
        and thread must match; no connection survives a pass or model wait.
        """
        if type(actor) is not StaffActorContext:
            raise SQLAlchemyError("Authority scope requires trusted identity")
        existing = self._scope.get()
        if existing is not None:
            existing.require_current()
            if existing.actor != actor:
                raise SQLAlchemyError("Authority scope identity changed")
            yield
            existing.require_current()
            return
        with ExitStack() as stack:
            state = _ReadPass(actor, get_ident(), stack)
            token = self._scope.set(state)
            try:
                yield
                state.require_current()
            finally:
                state.active = False
                self._scope.reset(token)

    def _read[T](
        self,
        operation: Callable[[AuthorityBasis], T],
        key: GovernanceAuthorityQuery | GovernanceAuthorizationBasis,
    ) -> T:
        state = self._scope.get()
        borrowed = self._borrowed.get()
        if borrowed is not None:
            borrowed.require_current()
            if state is not None:
                state.require_current()
                if (
                    key.school_id != state.actor.school_id
                    or key.assignment_id != state.actor.assignment_id
                ):
                    raise SQLAlchemyError("Authority request identity changed")
            try:
                return operation(self._build_basis(borrowed.sessions))
            finally:
                borrowed.require_current()
        if state is None:
            with self._connection(None) as current:
                return operation(self._build_basis(current.sessions))
        state.require_current()
        if key.school_id != state.actor.school_id or key.assignment_id != state.actor.assignment_id:
            raise SQLAlchemyError("Authority request identity changed")
        # Deduplicated proof passes with no remaining decisions need no checkout.
        if state.reader is None:
            state.reader = state.stack.enter_context(self._connection(state.actor))
        # The evidence backend checks the complete request actor before Thin.
        # Build anew per call; dependencies also open a new ORM Session per read.
        try:
            return operation(self._build_basis(state.reader.sessions))
        finally:
            state.require_current()

    def issue(self, query: GovernanceAuthorityQuery) -> GovernanceAuthorizationBasis:
        return self._read(lambda basis: basis.issue(query), query)

    def validate(self, basis: GovernanceAuthorizationBasis, *, as_of: datetime) -> bool:
        return self._read(lambda current: current.validate(basis, as_of=as_of), basis)

    def issue_many(
        self, queries: tuple[GovernanceAuthorityQuery, ...]
    ) -> tuple[GovernanceAuthorizationBasis | Exception, ...]:
        if not queries:
            return ()
        if len(queries) > 128 or any(
            (q.school_id, q.assignment_id) != (queries[0].school_id, queries[0].assignment_id)
            for q in queries
        ):
            raise SQLAlchemyError("Authority batch identity or size changed")
        return self._read(lambda current: current.issue_many(queries), queries[0])

    def validate_many(
        self, bases: tuple[GovernanceAuthorizationBasis, ...], *, as_of: datetime
    ) -> tuple[bool | Exception, ...]:
        if not bases:
            return ()
        if len(bases) > 128 or any(
            (b.school_id, b.assignment_id) != (bases[0].school_id, bases[0].assignment_id)
            for b in bases
        ):
            raise SQLAlchemyError("Authority batch identity or size changed")
        return self._read(lambda current: current.validate_many(bases, as_of=as_of), bases[0])
