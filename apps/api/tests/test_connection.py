from sqlalchemy import create_engine

from app.db.connection import build_session_factory, probe_database


def test_probe_database_executes_a_read_only_query() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")

    probe_database(engine)


def test_session_factory_is_bound_to_the_given_engine() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")

    session_factory = build_session_factory(engine)

    assert session_factory.kw["bind"] is engine
