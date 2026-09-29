"""F2 reserves one connection across fresh authority and bounded data phases."""

from collections.abc import Callable, Generator
from contextlib import contextmanager

from sqlalchemy import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.composition.school_fact_authority_connection import ConnectionBoundAuthority
from app.platform.authorization.secure_reader_adapters import SqlAlchemyRepeatableReadExecutor
from app.platform.authorization.secure_reader_service import ReadTransaction


class F2ConnectionReadExecutor:
    def __init__(
        self, sessions: sessionmaker[Session], authority: ConnectionBoundAuthority
    ) -> None:
        self.sessions = sessions
        self.authority = authority

    @contextmanager
    def validated_read(
        self, before_read: Callable[[], None], after_read: Callable[[], None]
    ) -> Generator[ReadTransaction]:
        engine = self.sessions.kw.get("bind")
        if not isinstance(engine, Engine):
            raise SQLAlchemyError("F2 read requires an owned Reader engine")
        # Pool waiting happens before fresh authority checks. Each RC phase
        # ends before switching isolation; no RR snapshot authorizes release.
        with engine.connect() as connection:
            with self.authority.borrow_connection(connection):
                before_read()
            bound = sessionmaker(bind=connection, expire_on_commit=False)
            with SqlAlchemyRepeatableReadExecutor(bound).repeatable_read() as transaction:
                yield transaction
            connection.execution_options(isolation_level="READ COMMITTED")
            with self.authority.borrow_connection(connection):
                after_read()
