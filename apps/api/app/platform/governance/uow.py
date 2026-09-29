# pyright: strict

from __future__ import annotations

from contextlib import AbstractContextManager
from types import TracebackType
from typing import Self

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, SessionTransaction, sessionmaker

from app.db.uow import CommitOutcomeUnknown
from app.platform.governance.repository import (
    GovernanceOutcomeProbeRepository,
    GovernanceRepository,
)


class GovernanceUnitOfWork(AbstractContextManager["GovernanceUnitOfWork"]):
    """One physical SQLAlchemy transaction for one Governance command."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory
        self._session: Session | None = None
        self._transaction: SessionTransaction | None = None
        self._repository: GovernanceRepository | None = None
        self._committed = False

    @property
    def repository(self) -> GovernanceRepository:
        if self._repository is None:
            raise RuntimeError("Governance UnitOfWork is not active.")
        return self._repository

    def __enter__(self) -> Self:
        if self._session is not None:
            raise RuntimeError("Governance UnitOfWork cannot be entered twice.")
        session = self._session_factory()
        self._session = session
        self._transaction = session.begin()
        self._repository = GovernanceRepository(session)
        return self

    def commit(self) -> None:
        transaction = self._transaction
        if transaction is None or not transaction.is_active or self._committed:
            raise RuntimeError("Governance UnitOfWork has no active transaction.")
        try:
            transaction.commit()
        except DBAPIError as exc:
            sqlstate = getattr(exc.orig, "sqlstate", None)
            if exc.connection_invalidated or (
                isinstance(sqlstate, str) and sqlstate.startswith("08")
            ):
                raise CommitOutcomeUnknown(
                    "Governance commit outcome is unknown and must be probed."
                ) from exc
            raise
        else:
            self._committed = True

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, traceback
        transaction = self._transaction
        session = self._session
        try:
            if transaction is not None and transaction.is_active and not self._committed:
                try:
                    transaction.rollback()
                except DBAPIError:
                    if exc is None:
                        raise
        finally:
            if session is not None:
                session.close()
            self._session = None
            self._transaction = None
            self._repository = None


class GovernanceOutcomeProbeUnitOfWork(AbstractContextManager["GovernanceOutcomeProbeUnitOfWork"]):
    """A fresh read-only transaction used only to resolve uncertain commits."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory
        self._session: Session | None = None
        self._transaction: SessionTransaction | None = None
        self._repository: GovernanceOutcomeProbeRepository | None = None

    @property
    def repository(self) -> GovernanceOutcomeProbeRepository:
        if self._repository is None:
            raise RuntimeError("Governance outcome probe is not active.")
        return self._repository

    def __enter__(self) -> Self:
        if self._session is not None:
            raise RuntimeError("Governance outcome probe cannot be entered twice.")
        session = self._session_factory()
        transaction = session.begin()
        self._session = session
        self._transaction = transaction
        try:
            session.execute(text("SET TRANSACTION READ ONLY"))
        except BaseException:
            if transaction.is_active:
                transaction.rollback()
            session.close()
            self._session = None
            self._transaction = None
            raise
        self._repository = GovernanceOutcomeProbeRepository(session)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc, traceback
        transaction = self._transaction
        session = self._session
        try:
            if transaction is not None and transaction.is_active:
                transaction.rollback()
        finally:
            if session is not None:
                session.close()
            self._session = None
            self._transaction = None
            self._repository = None


__all__ = [
    "GovernanceOutcomeProbeUnitOfWork",
    "GovernanceUnitOfWork",
]
