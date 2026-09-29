"""F7 exact F6 coverage checks on the owned, rollback-only PostgreSQL fixture.

The fixture inserts synthetic rows inside one transaction. It never publishes to
the original school instance, and closing the connection rolls all rows back.
"""

from __future__ import annotations

import json
import os
import socket
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import psycopg
import pytest
from sqlalchemy import URL, create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.composition.school_fact_persistence import SqlAlchemySchoolFactPersistence
from app.platform.school_facts.application import SchoolFactChangeRejected
from app.platform.school_facts.contracts import CoverageBasisV1
from app.platform.school_facts.f7_coverage_basis import mother_set_digest
from tests.school_facts.test_sf1_postgres_constraints import (
    _at,
    _insert_bed,
    _insert_person_and_student,
    _insert_residence,
    _insert_room,
    _insert_source,
)

pytestmark = pytest.mark.skipif(
    not os.environ.get("F7_TEST_DATABASE_CONFIG"), reason="owned F7 PG config required"
)

KINDS = (
    "dormitory_building_profile",
    "dorm_room",
    "dorm_bed",
    "student_bed_residence",
)


def _connect_owned_fixture() -> psycopg.Connection:
    config = json.loads(Path(os.environ["F7_TEST_DATABASE_CONFIG"]).read_text(encoding="utf-8"))
    assert config["owner"] == "F7 isolated quality migration and reader tests"
    assert config["container"] == "campus-f7-tests-20260924"
    assert config["database"] == "f7_fixture_test"
    assert config["database"] != "campus_ai_school_facts_f1"
    try:
        host = socket.gethostbyname(config["container"])
    except OSError:
        raise RuntimeError("Owned F7 test container unavailable") from None
    try:
        connection = psycopg.connect(
            host=host, port=config["port"], dbname=config["database"],
            user="postgres", password=config["admin"], connect_timeout=3,
        )
    except Exception:
        raise RuntimeError("Owned F7 test database unavailable") from None
    try:
        actual = connection.execute(
            "SELECT current_database(),system_identifier::text FROM pg_control_system()"
        ).fetchone()
        assert actual == (config["database"], config["system_identifier"])
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone()[0] == (
            "20260924_55"
        )
    except Exception:
        connection.close()
        raise
    return connection


def _lineage(
    connection: psycopg.Connection,
    *,
    school: UUID,
    batch: UUID,
    kind: str,
    key: str,
    target: UUID,
    version: int = 1,
) -> None:
    sequence = connection.execute(
        "SELECT COALESCE(MAX(sequence),0)+1 FROM master_data_import_units "
        "WHERE school_id=%s AND batch_id=%s",
        (school, batch),
    ).fetchone()[0]
    unit = uuid4()
    digest = unit.hex * 2
    connection.execute(
        "INSERT INTO master_data_import_units "
        "(id,school_id,batch_id,sequence,row_number,unit_kind,status,payload,"
        "payload_sha256,soft_anomalies,initial_soft_anomalies,target_id,"
        "target_version,version) VALUES "
        "(%s,%s,%s,%s,%s,'school_fact','applied',"
        "jsonb_build_object('fact_kind',CAST(%s AS text),'source_key',CAST(%s AS text),"
        "'operation','upsert','expected_version',0),%s,'[]','[]',%s,%s,1)",
        (unit, school, batch, sequence, sequence, kind, key, digest, target, version),
    )
    connection.execute(
        "INSERT INTO school_fact_change_lineage "
        "(id,school_id,batch_id,unit_id,fact_kind,fact_source_key,operation,"
        "target_id,target_version,source_sha256,sheet_code,row_number) VALUES "
        "(%s,%s,%s,%s,%s,%s,'upsert',%s,%s,%s,'f7_exact_coverage',%s)",
        (uuid4(), school, batch, unit, kind, key, target, version, digest, sequence),
    )


def _clone_applied_batch(
    connection: psycopg.Connection, school: UUID, template: UUID
) -> UUID:
    """Create a self-contained six-unit terminal test receipt; never alter history."""
    batch, record, outcome = uuid4(), uuid4(), uuid4()
    digest = batch.hex * 2
    base = connection.execute(
        "SELECT COALESCE(MAX(target_fact_version),1) FROM master_data_import_batches "
        "WHERE school_id=%s AND mode='canonical_atomic_changes' AND status='applied'",
        (school,),
    ).fetchone()[0]
    connection.execute(
        "INSERT INTO master_data_import_batches SELECT "
        "(jsonb_populate_record(NULL::master_data_import_batches,to_jsonb(b)||%s::jsonb)).* "
        "FROM master_data_import_batches b WHERE b.school_id=%s AND b.id=%s",
        (json.dumps({
            "id": str(batch), "request_id": str(uuid4()),
            "source_ref": "f7-exact-coverage:" + batch.hex,
            "source_sha256": digest, "idempotency_key_hash": digest,
            "plan_sha256": digest, "confirmation_token_sha256": digest,
            "planned_units": 6, "succeeded_units": 6,
            "base_fact_version": base, "target_fact_version": base + 1,
        }), school, template),
    )
    connection.execute(
        "INSERT INTO command_idempotency_records SELECT "
        "(jsonb_populate_record(NULL::command_idempotency_records,to_jsonb(i)||%s::jsonb)).* "
        "FROM command_idempotency_records i JOIN master_data_import_command_outcomes o "
        "ON o.school_id=i.school_id AND o.idempotency_record_id=i.id "
        "WHERE o.school_id=%s AND o.batch_id=%s",
        (json.dumps({
            "id": str(record), "resource_id": str(batch),
            "idempotency_key_hash": digest, "request_sha256": digest,
            "result_payload": {
                "resource_type": "master_data.import_batch", "resource_id": str(batch),
                "resource_version": 1, "outcome_code": "applied",
            },
        }), school, template),
    )
    connection.execute(
        "INSERT INTO master_data_import_command_outcomes SELECT "
        "(jsonb_populate_record(NULL::master_data_import_command_outcomes,"
        "to_jsonb(o)||%s::jsonb)).* "
        "FROM master_data_import_command_outcomes o "
        "WHERE o.school_id=%s AND o.batch_id=%s",
        (json.dumps({
            "id": str(outcome), "idempotency_record_id": str(record),
            "batch_id": str(batch), "succeeded_units": list(range(1, 7)),
            "failed_units": [], "executed_units": list(range(1, 7)),
            "unit_outcome_snapshots": None,
        }), school, template),
    )
    return batch


@contextmanager
def _f6_targets():
    raw = _connect_owned_fixture()
    engine = create_engine(
        URL.create("postgresql+psycopg", username="postgres"),
        creator=lambda: raw, poolclass=StaticPool, hide_parameters=True,
    )
    connection = engine.connect()  # Dialect startup precedes all fixture writes.
    transaction = connection.begin()
    try:
        raw.execute("SET ROLE campus_ai_db_owner")
        school = raw.execute("SELECT school_id FROM school_database_anchor").fetchone()[0]
        marker = uuid4().hex
        source, person, student, room, bed = (uuid4() for _ in range(5))
        _insert_source(
            raw, school_id=school, source_id=source, source_key=f"f7:quality:{marker}"
        )
        _insert_person_and_student(
            raw, school_id=school, person_id=person, student_id=student, key=marker
        )
        _insert_room(
            raw, school_id=school, source_id=source, room_id=room,
            key=marker, capacity=1,
        )
        building = raw.execute(
            "SELECT parent_location_id FROM campus_locations WHERE school_id=%s AND id=%s",
            (school, room),
        ).fetchone()[0]
        profile = uuid4()
        raw.execute(
            "INSERT INTO dormitory_building_profile_versions "
            "(id,school_id,source_key,location_id,usage_kind,display_name,"
            "is_available,valid_from,valid_to,version,source_id) VALUES "
            "(%s,%s,%s,%s,'building','合成宿舍楼',true,%s,%s,1,%s)",
            (profile, school, f"f7:building:{marker}", building, _at(1), _at(3), source),
        )
        _insert_bed(
            raw, school_id=school, source_id=source, room_id=room, bed_id=bed,
            key=marker,
        )
        campus = raw.execute(
            "SELECT parent_location_id FROM campus_locations "
            "WHERE school_id=%s AND id=%s",
            (school, building),
        ).fetchone()[0]
        raw.execute(
            "INSERT INTO student_school_relationships "
            "(id,school_id,student_id,student_number,enrollment_status,residence_mode,"
            "primary_campus_location_id,administrative_campus_location_id,valid_from,"
            "version,source_id) VALUES "
            "(%s,%s,%s,%s,'enrolled','boarding',%s,%s,%s,1,%s)",
            (uuid4(), school, student, f"F7-{marker}", campus, campus, _at(1), source),
        )
        residence = _insert_residence(
            raw, school_id=school, source_id=source, student_id=student,
            bed_id=bed, valid_from=_at(1),
        )
        room_profile = raw.execute(
            "SELECT id FROM dorm_room_profile_versions WHERE school_id=%s "
            "AND room_location_id=%s AND source_key IS NULL",
            (school, room),
        ).fetchone()[0]
        template = raw.execute(
            "SELECT l.batch_id FROM school_fact_change_lineage l "
            "JOIN master_data_import_batches b ON b.school_id=l.school_id "
            "AND b.id=l.batch_id AND b.status='applied' "
            "WHERE l.school_id=%s AND l.fact_kind='location' AND l.target_id=%s "
            "ORDER BY l.created_at DESC LIMIT 1",
            (school, room),
        ).fetchone()[0]
        batch = _clone_applied_batch(raw, school, template)
        targets = {
            "dormitory_building_profile": (f"f7:building:{marker}", profile, building),
            "dorm_room": (f"f7:room:{marker}", room_profile, room),
            "dorm_bed": (f"bed:{marker}", bed, room),
            "student_bed_residence": (f"f7:residence:{marker}", residence, person),
        }
        # Legacy room profiles have no direct source_key. The latest applied
        # lineage is the only stable key used by both F6 Reader and F7 Writer.
        assert raw.execute(
            "SELECT source_key FROM dorm_room_profile_versions WHERE id=%s",
            (room_profile,),
        ).fetchone()[0] is None
        _lineage(
            raw, school=school, batch=batch, kind="dorm_room",
            key=targets["dorm_room"][0], target=room_profile,
        )
        _lineage(
            raw, school=school, batch=batch, kind="student_bed_residence",
            key=targets["student_bed_residence"][0], target=residence,
        )
        version = raw.execute(
            "SELECT COALESCE(MAX(target_fact_version),1) FROM master_data_import_batches "
            "WHERE school_id=%s AND mode='canonical_atomic_changes' AND status='applied'",
            (school,),
        ).fetchone()[0]
        keys = {}
        for kind, (target, _, owner_id) in targets.items():
            owner_kind = "person" if kind == "student_bed_residence" else "location"
            owner_key = raw.execute(
                "SELECT source_key FROM school_people WHERE school_id=%s AND id=%s"
                if owner_kind == "person" else
                "SELECT source_key FROM campus_locations WHERE school_id=%s AND id=%s",
                (school, owner_id),
            ).fetchone()[0]
            key, row_id = f"f7:quality:{uuid4().hex}", uuid4()
            keys[kind] = key
            basis = _claim(
                school, kind, target, owner_kind, owner_key, version, at=_at(1)
            ).content.coverage_basis.model_dump(mode="json")
            raw.execute(
                "INSERT INTO school_fact_qualities "
                "(id,school_id,source_key,quality_kind,target_fact_kind,owner_kind,"
                "person_id,location_id,target_source_key,basis_reference,responsibility,"
                "verified_at,coverage_status,coverage_basis,valid_from,valid_to,"
                "version,source_id,import_batch_id) VALUES "
                "(%s,%s,%s,'coverage',%s,%s,%s,%s,%s,'隔离核查','隔离测试',"
                "%s,'complete',%s,%s,%s,1,%s,%s)",
                (row_id, school, key, kind, owner_kind,
                 owner_id if owner_kind == "person" else None,
                 owner_id if owner_kind == "location" else None,
                 target, _at(1), json.dumps(basis), _at(1), _at(3), source, batch),
            )
            _lineage(
                raw, school=school, batch=batch, kind="fact_quality",
                key=key, target=row_id,
            )
        raw.execute("SET CONSTRAINTS ALL IMMEDIATE")
        raw.execute("SET CONSTRAINTS ALL DEFERRED")
        yield raw, connection, school, targets, keys
    finally:
        transaction.rollback()
        connection.close()
        engine.dispose()
        raw.close()


def _claim(school, kind, target, owner_kind, owner_key, school_version, *,
           target_version=1, at=None):
    at = at or _at(2)
    return SimpleNamespace(
        effective_at=at,
        content=SimpleNamespace(
            quality_kind="coverage", coverage_status="complete",
            target_fact_kind=kind, target_source_key=target, owner_kind=owner_kind,
            coverage_basis=CoverageBasisV1(
                school_id=school, owner_kind=owner_kind,
                owner_source_key=owner_key, target_fact_kind=kind,
                target_source_key=target, field_name=None, purpose=None,
                contact_kind=None, valid_from=at, valid_to=None,
                school_fact_version=school_version, member_count=1,
                member_sha256=mother_set_digest(((target, target_version),))[1],
            ),
        ),
    )


def test_writer_checks_all_four_real_targets_and_legacy_room_lineage():
    with _f6_targets() as (_, connection, school, targets, _):
        with Session(bind=connection, join_transaction_mode="create_savepoint") as session:
            version = SqlAlchemySchoolFactPersistence._current_global_fact_version(
                session, school
            )
            for kind, (key, _, owner) in targets.items():
                owner_kind = "person" if kind == "student_bed_residence" else "location"
                SqlAlchemySchoolFactPersistence._verify_complete_coverage_basis(
                    session, school, _claim(
                        school, kind, key, owner_kind, str(owner), version
                    ), {},
                )
                with pytest.raises(SchoolFactChangeRejected, match="母集已变化"):
                    SqlAlchemySchoolFactPersistence._verify_complete_coverage_basis(
                        session, school, _claim(
                            school, kind, key, owner_kind, str(owner), version,
                            target_version=2,
                        ), {},
                    )
                with pytest.raises(SchoolFactChangeRejected, match="时点不可核实"):
                    SqlAlchemySchoolFactPersistence._verify_complete_coverage_basis(
                        session, school, _claim(
                            school, kind, key, owner_kind, str(owner), version,
                            at=_at(1).replace(year=2025),
                        ), {},
                    )
            kind, (key, _, owner) = next(iter(targets.items()))
            with pytest.raises(SchoolFactChangeRejected, match="时点不可核实"):
                SqlAlchemySchoolFactPersistence._verify_complete_coverage_basis(
                    session, school, _claim(
                        school, kind, key, "location", str(owner), version,
                        at=_at(3),
                    ), {},
                )
            with pytest.raises(SchoolFactChangeRejected, match="学校事实版本已变化"):
                SqlAlchemySchoolFactPersistence._verify_complete_coverage_basis(
                    session, school, _claim(
                        school, kind, key, "location", str(owner), version - 1
                    ), {},
                )


def test_reader_projection_is_exact_time_bounded_and_reader_role_only():
    with _f6_targets() as (raw, _, school, targets, keys):
        function = "app.school_fact_f7_residence_exact_coverage(uuid,text,text,timestamptz)"
        assert raw.execute(
            "SELECT has_function_privilege('campus_ai_school_fact_reader',%s,'EXECUTE'),"
            "has_function_privilege('campus_ai_runtime',%s,'EXECUTE')",
            (function, function),
        ).fetchone() == (True, False)
        raw.execute("RESET ROLE")
        raw.execute("SET SESSION AUTHORIZATION campus_ai_school_fact_reader")
        assert raw.execute(
            "SELECT session_user,current_user,"
            "(SELECT rolsuper FROM pg_roles WHERE rolname=current_user),"
            "current_setting('row_security')"
        ).fetchone() == (
            "campus_ai_school_fact_reader", "campus_ai_school_fact_reader", False, "on"
        )
        for kind, (target, _, _) in targets.items():
            result = raw.execute(
                "SELECT app.school_fact_f7_residence_exact_coverage(%s,%s,%s,%s)",
                (school, kind, target, _at(2)),
            ).fetchone()[0]
            assert len(result) == 1
            assert result[0]["quality_source_key"] == keys[kind]
            assert result[0]["target_source_key"] == target
            assert result[0]["coverage_basis"]["target_source_key"] == target
            assert result[0]["source"]["kind"] == "formal_publication"
            assert raw.execute(
                "SELECT app.school_fact_f7_residence_exact_coverage(%s,%s,%s,%s)",
                (school, kind, target + ":other", _at(2)),
            ).fetchone()[0] == []
            assert raw.execute(
                "SELECT app.school_fact_f7_residence_exact_coverage(%s,%s,%s,%s)",
                (school, kind, target, _at(3)),
            ).fetchone()[0] == []
        with pytest.raises(psycopg.errors.InvalidAuthorizationSpecification), raw.transaction():
            raw.execute(
                "SELECT app.school_fact_f7_residence_exact_coverage(%s,%s,%s,%s)",
                (uuid4(), KINDS[0], targets[KINDS[0]][0], _at(2)),
            )
