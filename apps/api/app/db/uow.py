# pyright: strict

from __future__ import annotations

from contextlib import AbstractContextManager
from types import TracebackType
from typing import Self

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, SessionTransaction, sessionmaker

from app.db.rls_context import DatabaseExecutionContextEnvelope, set_local_execution_context


class CommitOutcomeUnknown(RuntimeError):
    """表示连接在 COMMIT 应答前失效，调用方必须用新只读事务探测。"""


class SqlAlchemyUnitOfWork(AbstractContextManager["SqlAlchemyUnitOfWork"]):
    """拥有一个 SQLAlchemy 会话和事务，并在进入时绑定可信数据库上下文。"""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        database_context_envelope: DatabaseExecutionContextEnvelope,
        *,
        read_only: bool = False,
    ) -> None:
        self._session_factory = session_factory
        self._database_context_envelope = database_context_envelope
        self._read_only = read_only
        self._session: Session | None = None
        self._transaction: SessionTransaction | None = None
        self._committed = False

    @property
    def session(self) -> Session:
        """只在已进入的 UoW 中返回唯一 Session。"""

        if self._session is None:
            raise RuntimeError("UnitOfWork 尚未进入或已经关闭。")
        return self._session

    @property
    def committed(self) -> bool:
        """说明本 UoW 是否已由 Handler 明确提交。"""

        return self._committed

    def __enter__(self) -> Self:
        if self._session is not None or self._transaction is not None:
            raise RuntimeError("同一个 UnitOfWork 实例不能重复进入。")
        session = self._session_factory()
        transaction = session.begin()
        self._session = session
        self._transaction = transaction
        try:
            if self._read_only:
                session.execute(text("SET TRANSACTION READ ONLY"))
            set_local_execution_context(session, self._database_context_envelope)
        except BaseException:
            if transaction.is_active:
                transaction.rollback()
            session.close()
            self._session = None
            self._transaction = None
            raise
        return self

    def commit(self) -> None:
        """只提交本 UoW 自己创建的活动事务；重复提交明确失败。"""

        transaction = self._transaction
        if self._session is None or transaction is None:
            raise RuntimeError("UnitOfWork 尚未进入或已经关闭。")
        if self._committed or not transaction.is_active:
            raise RuntimeError("UnitOfWork 事务已经结束。")
        try:
            transaction.commit()
        except DBAPIError as exc:
            sqlstate = getattr(exc.orig, "sqlstate", None)
            if exc.connection_invalidated or (
                isinstance(sqlstate, str) and sqlstate.startswith("08")
            ):
                raise CommitOutcomeUnknown("数据库提交结果未知，必须重新探测。") from exc
            raise
        else:
            self._committed = True

    def rollback(self) -> None:
        """显式回滚本 UoW 的活动事务，不接管任何外部会话。"""

        transaction = self._transaction
        if self._session is None or transaction is None:
            raise RuntimeError("UnitOfWork 尚未进入或已经关闭。")
        if transaction.is_active:
            transaction.rollback()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, traceback
        session = self._session
        transaction = self._transaction
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
