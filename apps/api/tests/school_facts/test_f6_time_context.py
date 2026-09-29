"""F6 uses verified school time, independent of browser locale and capture time."""

from datetime import UTC, datetime, timedelta
from importlib import import_module, util
from types import SimpleNamespace

import pytest

from app.composition.school_fact_residence_gateway import residence_model_material
from tests.test_f1_official_contact_query import _actor

NOW = datetime(2026, 9, 21, 16, tzinfo=UTC)


def module():
    name = "app.composition.school_fact_residence_time"
    assert util.find_spec(name), "F6 authoritative school time callback is missing"
    return import_module(name)


def setup_time(zone="Asia/Shanghai"):
    actor = _actor()
    state = SimpleNamespace(now=NOW, active=True, zone=zone, version=3, end=None, reads=[])

    def load(who, at):
        assert who == actor
        state.reads.append(at)
        return SimpleNamespace(
            school_id=actor.school_id,
            timezone_name=state.zone,
            source_version=state.version,
            captured_at=state.now,
            effective_interval=SimpleNamespace(
                valid_from=NOW - timedelta(days=1), valid_to=state.end
            ),
        )

    service = module().ResidenceTimeService(
        load=load,
        current_actor=lambda who: state.active and who == actor,
        clock=lambda: state.now,
    )
    return service, actor, state


def test_model_time_labels_use_authoritative_school_zone_and_preserve_iso():
    raw = {
        "fact_timezone_name": "Asia/Shanghai",
        "records": [{"valid_from": "2026-09-19T16:00:00+00:00", "valid_to": None}],
    }
    result = residence_model_material(raw)
    assert result["records"][0]["valid_from"] == raw["records"][0]["valid_from"]
    assert "2026-09-20 00:00:00" in result["records"][0]["time_labels"]["valid_from"]
    assert "Asia/Shanghai" in result["records"][0]["time_labels"]["valid_from"]


def test_each_preview_impact_uses_its_own_verified_time_zone():
    value = "2026-09-19T16:00:00+00:00"
    result = residence_model_material(
        {
            "summary": {
                "fact_timezone_name": "Asia/Shanghai",
                "effective_at": value,
                "impacts": [
                    {"fact_timezone_name": "America/New_York", "effective_at": value},
                    {"fact_timezone_name": None, "effective_at": value},
                ],
            }
        }
    )["summary"]
    assert "2026-09-20 00:00:00" in result["time_labels"]["effective_at"]
    assert "2026-09-19 12:00:00" in result["impacts"][0]["time_labels"]["effective_at"]
    assert "time_labels" not in result["impacts"][1]


def test_explicit_unknown_parent_does_not_hide_known_child_time_basis():
    result = residence_model_material(
        {
            "summary": {
                "fact_timezone_name": None,
                "impacts": [
                    {"fact_timezone_name": "Asia/Shanghai", "effective_at": "2026-09-19T16:00:00Z"}
                ],
            }
        }
    )
    assert "2026-09-20 00:00:00" in result["summary"]["impacts"][0]["time_labels"]["effective_at"]


@pytest.mark.parametrize("zone", [None, "invalid/zone"])
def test_unknown_school_zone_never_assumes_china(zone):
    service, actor, _ = setup_time(zone)
    evidence = service.capture(actor)
    assert evidence.document["fact_timezone_name"] is None
    assert evidence.document["time_basis"]["status"] == "unavailable"
    result = residence_model_material(
        {**evidence.document, "records": [{"valid_from": "2026-09-19T16:00:00+00:00"}]}
    )
    assert "2026-09-20" not in str(result["records"])
    assert "school_timezone_unavailable" in result["limitations"]


def test_capture_time_is_not_a_fact_and_changed_fact_or_identity_is_stale():
    service, actor, state = setup_time()
    evidence = service.capture(actor)
    document = evidence.document
    state.now += timedelta(seconds=1)
    evidence.revalidate(actor=actor)
    assert service.capture(actor).document == document
    state.version += 1
    with pytest.raises(RuntimeError):
        evidence.revalidate(actor=actor)
    state.version -= 1
    state.active = False
    with pytest.raises(PermissionError):
        evidence.revalidate(actor=actor)


def test_current_school_natural_boundary_is_part_of_evidence():
    service, actor, state = setup_time()
    state.end = NOW + timedelta(seconds=2)
    evidence = service.capture(actor)
    assert evidence.next_boundary == state.end
    state.now = state.end
    with pytest.raises(RuntimeError):
        evidence.revalidate(actor=actor)


def test_family_result_uses_same_callback_and_rechecks_school_change():
    from tests.school_facts.test_f6_residence_read import fixture

    query, actor, state = fixture()
    zone_state = {"version": 3}
    query.time_context = module().ResidenceTimeService(
        current_actor=lambda who: who == actor,
        clock=lambda: state.clock,
        load=lambda who, at: SimpleNamespace(
            school_id=actor.school_id,
            timezone_name="Asia/Shanghai",
            source_version=zone_state["version"],
            captured_at=state.clock,
            effective_interval=SimpleNamespace(
                valid_from=state.clock - timedelta(days=1), valid_to=None
            ),
        ),
    )
    result = query.query(actor=actor)
    assert result.public()["fact_timezone_name"] == "Asia/Shanghai"
    assert result.evidence.valid(actor)
    zone_state["version"] += 1
    assert not result.evidence.valid(actor)


def test_runtime_callback_rejects_unbound_and_mismatched_actor_before_session():
    from app.composition.school_fact_directory_context import bind_directory_identity

    actor = _actor()
    touched = []
    callback = module().SqlResidenceSchoolTime(
        sessions=lambda: touched.append(True),
        resolve_identity=lambda ref: SimpleNamespace(actor=None, session_binding="a" * 64),
        proof_signer=None,
    )
    with pytest.raises(PermissionError):
        callback(actor, NOW)
    with bind_directory_identity("private-context"), pytest.raises(PermissionError):
        callback(actor, NOW)
    assert not touched


def test_current_capture_keeps_fact_observation_time_and_expiry():
    service, actor, state = setup_time()
    state.end = NOW + timedelta(seconds=1)
    state.now = NOW + timedelta(seconds=2)
    evidence = service.capture(actor, current_at=NOW)
    assert state.reads[-1] == NOW
    assert evidence.next_boundary == NOW + timedelta(seconds=1)
    with pytest.raises(RuntimeError):
        evidence.revalidate(actor=actor)


def test_staff_http_describe_and_query_bind_existing_identity():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.composition.school_fact_directory_context import resolve_bound_identity
    from app.interfaces.http.school_fact_residence import create_residence_product_router

    actor = _actor()
    identity = SimpleNamespace(actor=actor, actor_context_ref="selected-staff")
    calls = []

    def view(**kwargs):
        calls.append(resolve_bound_identity(kwargs["actor"], lambda ref: identity))
        return {"status": "available"}

    app = FastAPI()
    app.include_router(
        create_residence_product_router(
            identity_dependency=lambda: identity,
            views=SimpleNamespace(describe=view, query=view),
            product=None,
        )
    )
    with TestClient(app) as client:
        assert (
            client.get("/api/v1/school-fact-product/residence-facts/describe/dorm_bed").status_code
            == 200
        )
        assert (
            client.post(
                "/api/v1/school-fact-product/residence-facts/query", json={"fact_kind": "dorm_bed"}
            ).status_code
            == 200
        )
    assert calls == [identity, identity]


@pytest.mark.parametrize("dependency_wrapper", [False, True])
def test_prepare_summary_and_confirmation_share_formal_time_proof(dependency_wrapper):
    from uuid import uuid4

    from tests.school_facts.test_f6_product_maintenance import (
        AT,
        replacement_environment,
        replacement_payload,
    )

    adapter, _ = replacement_environment()
    service, actor, state = setup_time()
    adapter.time_context = service if not dependency_wrapper else None
    resolved = adapter.resolve_change(
        actor=actor, payload=replacement_payload(), request_id=uuid4(), effective_at=AT
    )
    if dependency_wrapper:
        adapter.time_context = service
        resolved = adapter.attach_time_basis(actor=actor, resolved=resolved)
    assert resolved.summary.fact_timezone_name == "Asia/Shanghai"
    assert all(x["fact_timezone_name"] == "Asia/Shanghai" for x in resolved.summary.impacts)
    resolved.summary_revalidate(actor)
    state.version += 1
    with pytest.raises(RuntimeError):
        resolved.summary_revalidate(actor)


@pytest.mark.integration
def test_owned_real_runtime_school_snapshot_staff_and_guardian():
    """Real SESSION_USER/current_user runtime and signed contexts; all seeds roll back."""
    import json
    import os
    from pathlib import Path
    from uuid import uuid4

    import psycopg
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from app.composition.school_fact_directory_context import bind_directory_identity
    from app.platform import contracts
    from app.platform.identity.context_proof import ContextProofSigner
    from tests.school_facts.test_sf1_postgres_constraints import _insert_person_and_student
    from tests.school_facts.test_sf2_transaction import context_for, seed_staff

    config_path = os.environ.get("F6_TEST_DATABASE_CONFIG")
    if not config_path:
        pytest.skip("Requires the explicitly owned F6 fixture")
    cfg = json.loads(Path(config_path).read_text())
    meta = json.loads(Path(os.environ["F6_TEST_DATABASE_META"]).read_text())
    for key, expected in {
        "owner": "F6 isolated persistence and reader tests",
        "container": "campus-f6-tests-20260922",
        "database": "f6_fixture_test",
        "system_identifier": "7688048026898145318",
    }.items():
        assert cfg[key] == meta[key] == expected
    raw = psycopg.connect(
        host=cfg["host"],
        port=cfg["port"],
        dbname=cfg["database"],
        user="postgres",
        password=cfg["admin"],
    )
    engine = create_engine("postgresql+psycopg://", creator=lambda: raw, poolclass=StaticPool)
    try:
        with engine.connect() as connection, connection.begin():
            assert connection.execute(
                text("SELECT current_database(),system_identifier::text FROM pg_control_system()")
            ).one() == ("f6_fixture_test", "7688048026898145318")
            assert connection.scalar(text("SELECT count(*) FROM schools")) == 0
            connection.exec_driver_sql("SET SESSION AUTHORIZATION campus_ai_migration")
            connection.exec_driver_sql("SET ROLE campus_ai_db_owner")
            sessions = sessionmaker(
                bind=connection, join_transaction_mode="create_savepoint", expire_on_commit=False
            )
            now = datetime.now(UTC)
            school, user, person, assignment = seed_staff(
                sessions, bootstrap_valid_from=now - timedelta(days=1)
            )
            student, child_person, relation = uuid4(), uuid4(), uuid4()
            _insert_person_and_student(
                raw,
                school_id=school,
                person_id=child_person,
                student_id=student,
                key="f6-time-child",
            )
            source = connection.scalar(
                text("SELECT source_id FROM school_versions WHERE school_id=:school"),
                {"school": school},
            )
            reviewers = []
            for index in range(2):
                reviewer, duty = uuid4(), uuid4()
                raw.execute(
                    "INSERT INTO school_people(id,school_id,source_key,status,version) "
                    "VALUES(%s,%s,%s,'active',1)",
                    (reviewer, school, f"f6-time-reviewer-{index}"),
                )
                raw.execute(
                    "INSERT INTO staff_assignments SELECT (jsonb_populate_record("
                    "NULL::staff_assignments,to_jsonb(a)||%s::jsonb)).* "
                    "FROM staff_assignments a WHERE id=%s",
                    (
                        json.dumps(
                            {
                                "id": str(duty),
                                "person_id": str(reviewer),
                                "source_key": f"f6-time-review-duty-{index}",
                            }
                        ),
                        assignment,
                    ),
                )
                reviewers.append((reviewer, duty))
            raw.execute(
                "INSERT INTO guardian_relationships(id,school_id,guardian_person_id,student_id,"
                "relationship_kind,verification_source,verification_reference,verified_at,"
                "valid_from,status,version,authority_version,access_purpose,requested_by_person_id,"
                "requested_by_assignment_id,requested_at,verified_by_person_id,"
                "verified_by_assignment_id,source_id) VALUES(%s,%s,%s,%s,'verified_guardian',"
                "'manual','f6-time-test',%s,%s,'confirmed',1,1,'guardian_student_profile',"
                "%s,%s,%s,%s,%s,%s)",
                (
                    relation,
                    school,
                    person,
                    student,
                    now,
                    now - timedelta(hours=1),
                    *reviewers[0],
                    now,
                    *reviewers[1],
                    source,
                ),
            )
            staff = context_for(
                school_id=school,
                user_id=user,
                person_id=person,
                assignment_id=assignment,
                key="time-read",
            ).actor
            guardian = contracts._issue_guardian_actor_context(
                account_user_id=user,
                school_id=contracts.SchoolId(school),
                person_id=contracts.PersonId(person),
                guardian_relationship_id=contracts.GuardianRelationshipId(relation),
                relationship_version=1,
                authorization_version=1,
            )
            connection.exec_driver_sql("SET CONSTRAINTS ALL IMMEDIATE")
            connection.exec_driver_sql("RESET ROLE")
            connection.exec_driver_sql("RESET SESSION AUTHORIZATION")
            connection.exec_driver_sql("SET SESSION AUTHORIZATION campus_ai_runtime")
            assert connection.execute(
                text(
                    "SELECT session_user,current_user,"
                    "(SELECT rolsuper FROM pg_roles WHERE rolname=current_user)"
                )
            ).one() == ("campus_ai_runtime", "campus_ai_runtime", False)
            current = {"identity": None}
            callback = module().SqlResidenceSchoolTime(
                sessions=sessions,
                resolve_identity=lambda ref: current["identity"],
                proof_signer=ContextProofSigner(cfg["context"].encode()),
            )
            for actor in (staff, guardian):
                current["identity"] = SimpleNamespace(actor=actor, session_binding="a" * 64)
                with bind_directory_identity("owned-time-context"):
                    snapshot = callback(actor, now)
                    assert snapshot.school_id == actor.school_id
                    assert (
                        snapshot.timezone_name == "Asia/Shanghai" and snapshot.source_version == 1
                    )
                with pytest.raises(PermissionError):
                    callback(actor, now)
            # The existing runtime function rejects another school's coordinate,
            # even after a legitimate context was signed for the selected child.
            from sqlalchemy.exc import DBAPIError

            with pytest.raises(DBAPIError) as error, sessions() as session, session.begin():
                session.execute(
                    text("SELECT * FROM app.school_fact_school_snapshot_as_of(:school,:at)"),
                    {"school": uuid4(), "at": now},
                ).all()
            assert error.value.orig.sqlstate == "28000"
            connection.rollback()
    finally:
        raw.rollback()
        engine.dispose()
