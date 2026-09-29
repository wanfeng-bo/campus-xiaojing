"""45→55 on a new owned disposable database, preserving facts and formal receipts.

Never changes the original database or the shared fixture's schema/data/roles.
Only a uniquely named database in the positively identified F7 test cluster is
created and dropped. The real Alembic chain is used, not a revision-number edit.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from scripts.f7_engineering.migration import migration_hashes
from sqlalchemy import URL, create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from tests.school_facts.test_f7_large_own_receipt_postgres import _publish_formal_receipt
from tests.school_facts.test_sf2_transaction import seed_staff

API_ROOT = Path(__file__).resolve().parents[2]


def _upgrade(config, target, *, fail_after=None):
    env = os.environ.copy()
    env.update(
        {
            "POSTGRES_MIGRATION_HOST": config["host"],
            "POSTGRES_MIGRATION_PORT": str(config["port"]),
            "POSTGRES_MIGRATION_DB": config["database"],
            "POSTGRES_MIGRATION_USER": "campus_ai_migration",
            "POSTGRES_MIGRATION_PASSWORD": config["migration"],
            "POSTGRES_MIGRATION_CONTEXT_SIGNING_KEY": config["context"],
        }
    )
    argv = [sys.executable, "-m", "alembic", "upgrade", target]
    if fail_after is not None:
        # Failure injection is confined to this child process and its dedicated
        # test database; no released migration file or module in another process
        # is changed. Exercise rollback after real intermediate DDL has run.
        code = (
            "from alembic import command\nfrom alembic.config import Config\n"
            "from alembic.script import ScriptDirectory\n"
            "c=Config('alembic.ini');s=ScriptDirectory.from_config(c)\n"
            f"m=s.get_revision({fail_after!r}).module;original=m.upgrade\n"
            "def failing():\n original()\n raise RuntimeError('owned-migration-failure')\n"
            "m.upgrade=failing\n"
            "ScriptDirectory.from_config=classmethod(lambda cls,config:s)\n"
            f"command.upgrade(c,{target!r})\n"
        )
        argv = [sys.executable, "-c", code]
    result = subprocess.run(
        argv,
        cwd=API_ROOT,
        env=env,
        capture_output=True,
        timeout=100,
        check=False,
    )
    safe = result.stderr.decode(errors="replace")
    for name in ("admin", "runtime", "migration", "writer", "reader", "context"):
        safe = safe.replace(config[name], "[REDACTED]")
    if fail_after is None:
        assert result.returncode == 0, safe[-3500:]
    else:
        assert result.returncode != 0 and "owned-migration-failure" in safe, safe[-3500:]


def _tables_snapshot(connection, columns=None):
    if columns is None:
        columns = {}
        for table, column in connection.execute(
            "SELECT table_name,column_name FROM information_schema.columns "
            "WHERE table_schema='public' AND table_name<>'alembic_version' "
            "ORDER BY table_name,ordinal_position"
        ):
            columns.setdefault(table, []).append(column)
    fingerprints = {}
    for table, fields in columns.items():
        fingerprints[table] = connection.execute(
            sql.SQL(
                "SELECT count(*),md5(COALESCE(string_agg(row_to_json(t)::text, E'\\n' "
                "ORDER BY row_to_json(t)::text),'')) FROM (SELECT {} FROM {}) t"
            ).format(sql.SQL(",").join(map(sql.Identifier, fields)), sql.Identifier(table))
        ).fetchone()
    return columns, fingerprints


def test_upgrade_child_uses_owned_config_without_inheriting_missing_connection_fields(monkeypatch):
    for name in (
        "POSTGRES_MIGRATION_HOST", "POSTGRES_MIGRATION_PORT",
        "POSTGRES_MIGRATION_USER", "POSTGRES_MIGRATION_PASSWORD",
    ):
        monkeypatch.delenv(name, raising=False)
    observed = {}

    def capture(argv, **kwargs):
        observed["argv"] = argv
        observed["env"] = kwargs["env"]
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setattr(subprocess, "run", capture)
    config = {
        "host": "owned-isolated-db", "port": 5432, "database": "owned_disposable",
        "migration": "synthetic-migration-password", "context": "synthetic-context-key-32-bytes-long",
        "admin": "synthetic-admin", "runtime": "synthetic-runtime",
        "writer": "synthetic-writer", "reader": "synthetic-reader",
    }
    _upgrade(config, "20260922_45")
    assert {name: observed["env"][name] for name in (
        "POSTGRES_MIGRATION_HOST", "POSTGRES_MIGRATION_PORT",
        "POSTGRES_MIGRATION_USER", "POSTGRES_MIGRATION_PASSWORD",
    )} == {
        "POSTGRES_MIGRATION_HOST": config["host"],
        "POSTGRES_MIGRATION_PORT": str(config["port"]),
        "POSTGRES_MIGRATION_USER": "campus_ai_migration",
        "POSTGRES_MIGRATION_PASSWORD": config["migration"],
    }
    assert config["migration"] not in " ".join(observed["argv"])


@pytest.mark.skipif(
    not os.environ.get("F7_TEST_DATABASE_CONFIG"), reason="owned F7 PG config required"
)
def test_full_forward_chain_retains_all_preexisting_rows_and_original_receipt():
    tested_hashes = migration_hashes()
    original = json.loads(Path(os.environ["F7_TEST_DATABASE_CONFIG"]).read_text())
    assert original["owner"] == "F7 isolated quality migration and reader tests"
    assert original["container"] == "campus-f7-tests-20260924"
    assert original["database"] == "f7_fixture_test"
    database = "f7_migration_" + uuid4().hex
    params = {
        "host": original["host"],
        "port": original["port"],
        "dbname": original["database"],
        "user": "postgres",
        "password": original["admin"],
    }
    engines = {}
    with psycopg.connect(**params, autocommit=True) as admin:
        assert (
            admin.execute("SELECT system_identifier::text FROM pg_control_system()").fetchone()[0]
            == original["system_identifier"]
        )
        assert original["system_identifier"] != "7682019857215991846"
        roles_before = admin.execute(
            "SELECT rolname,rolcanlogin,rolsuper,rolcreatedb,rolcreaterole,rolbypassrls "
            "FROM pg_roles WHERE rolname LIKE 'campus_ai_%' ORDER BY rolname"
        ).fetchall()
        admin.execute(
            sql.SQL("CREATE DATABASE {} OWNER campus_ai_db_owner").format(sql.Identifier(database))
        )
        try:
            config = original | {"database": database}
            _upgrade(config, "20260922_45")
            for role in ("migration", "reader", "writer"):
                engines[role] = create_engine(
                    URL.create(
                        "postgresql+psycopg",
                        username="campus_ai_migration"
                        if role == "migration"
                        else f"campus_ai_school_fact_{role}",
                        password=config[role],
                        host=config["host"],
                        port=config["port"],
                        database=database,
                        query={"options": "-c role=campus_ai_db_owner"}
                        if role == "migration"
                        else {},
                    ),
                    poolclass=NullPool,
                    hide_parameters=True,
                )
            factory = sessionmaker(engines["migration"], expire_on_commit=False)
            school, user, person, assignment = seed_staff(
                factory, school_source_key="school:f7-migration-only"
            )
            locator, key, batch = _publish_formal_receipt(
                config,
                engines,
                school,
                SimpleNamespace(id=assignment, person_id=person, account_user_id=user),
            )
            with psycopg.connect(**(params | {"dbname": database}), autocommit=True) as inspect:
                columns, before = _tables_snapshot(inspect)
                assert before["master_data_import_batches"][0] > 0
                assert before["school_people"][0] > 0
                _upgrade(config, "20260924_55", fail_after="20260924_50")
                assert (
                    inspect.execute("SELECT version_num FROM alembic_version").fetchone()[0]
                    == "20260922_45"
                )
                assert _tables_snapshot(inspect, columns)[1] == before
                _upgrade(config, "20260924_55", fail_after="20260924_55")
                assert (
                    inspect.execute("SELECT version_num FROM alembic_version").fetchone()[0]
                    == "20260922_45"
                )
                assert _tables_snapshot(inspect, columns)[1] == before
                _upgrade(config, "20260924_55")
                assert (
                    inspect.execute("SELECT version_num FROM alembic_version").fetchone()[0]
                    == "20260924_55"
                )
                _, after = _tables_snapshot(inspect, columns)
                assert before == after
                row = inspect.execute(
                    "SELECT status,mapping_payload->>'result_locator_sha256' "
                    "FROM master_data_import_batches WHERE id=%s",
                    (batch,),
                ).fetchone()
                assert row[0] == "applied" and row[1] == locator
                assert (
                    inspect.execute(
                        "SELECT source_key FROM school_people WHERE source_key=%s", (key,)
                    ).fetchone()[0]
                    == key
                )
            assert (
                admin.execute(
                    "SELECT rolname,rolcanlogin,rolsuper,rolcreatedb,rolcreaterole,rolbypassrls "
                    "FROM pg_roles WHERE rolname LIKE 'campus_ai_%' ORDER BY rolname"
                ).fetchall()
                == roles_before
            )
            assert migration_hashes() == tested_hashes
        finally:
            for engine in engines.values():
                engine.dispose()
            # Identified dedicated test database only; no FORCE/no session kills.
            assert database.startswith("f7_migration_") and len(database) == 45
            admin.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(database)))
    print(
        json.dumps(
            {
                "kind": "f7-forward-preservation",
                "from": "20260922_45",
                "to": "20260924_55",
                "migration_hashes": tested_hashes,
                "tables_checked": len(before),
                "original_rows_preserved": True,
                "failure_rolls_back": True,
                "roles_unchanged": True,
                "original_database_used": False,
            },
            sort_keys=True,
        )
    )
