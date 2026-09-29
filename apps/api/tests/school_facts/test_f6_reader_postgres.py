"""Actual PostgreSQL Reader/RLS under rollback-only owned F6 fixtures."""

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from tests.school_facts.test_f6_residence_postgres import (
    current_owner_connection as current_owner_connection,
)
from tests.school_facts.test_f6_residence_postgres import (
    current_owner_session as current_owner_session,
)
from tests.school_facts.test_f6_residence_postgres import relationship, seed
from tests.school_facts.test_sf1_postgres_constraints import _at, _insert_residence


@pytest.fixture
def reader_fixture():
    config_path = os.environ.get("F6_TEST_DATABASE_CONFIG")
    if not config_path:
        pytest.skip("Only the explicitly owned F6 fixture is allowed")
    config = json.loads(Path(config_path).read_text())
    meta = json.loads(Path(os.environ["F6_TEST_DATABASE_META"]).read_text())
    for key, value in {
        "owner": "F6 isolated persistence and reader tests",
        "container": "campus-f6-tests-20260922",
        "database": "f6_fixture_test",
        "system_identifier": "7688048026898145318",
    }.items():
        assert config[key] == meta[key] == value
    # Superuser is used solely to switch SESSION_USER within one transaction;
    # each tested statement executes as the real non-superuser Reader role.
    # All fixture rows and authorization switches roll back on exit.
    c = psycopg.connect(
        host=config["host"],
        port=config["port"],
        dbname=config["database"],
        user="postgres",
        password=config["admin"],
    )
    # Initialize the dialect before seeding: its initial rollback must not
    # discard fixture rows. Reader repository sessions close savepoints only;
    # the enclosing test transaction always rolls back every synthetic write.
    engine = create_engine("postgresql+psycopg://", creator=lambda: c, poolclass=StaticPool)
    db_connection = engine.connect()
    transaction = db_connection.begin()
    sessions = sessionmaker(
        bind=db_connection, join_transaction_mode="create_savepoint", expire_on_commit=False
    )
    try:
        assert c.execute(
            "SELECT current_database(),system_identifier::text FROM pg_control_system()"
        ).fetchone() == ("f6_fixture_test", "7688048026898145318")
        assert c.execute("SELECT count(*) FROM schools").fetchone()[0] == 0, (
            "Unknown persisted fixture; do not modify it"
        )
        c.execute("SET SESSION AUTHORIZATION campus_ai_migration")
        c.execute("SET ROLE campus_ai_db_owner")
        state = seed(c)
        school, source, student, room, bed = state
        assert c.execute("SELECT school_id FROM school_database_anchor").fetchone()[0] == school
        relationship(c, state, _at(1), None, "f6-reader-school")
        c.execute(
            (
                "UPDATE student_school_relationships SET source_key='f6-reade"
                "r-school' WHERE school_id=%s"
            ),
            (school,),
        )
        _insert_residence(
            c, school_id=school, source_id=source, student_id=student, bed_id=bed, valid_from=_at(1)
        )
        residence = c.execute(
            "SELECT id FROM student_bed_residences WHERE school_id=%s", (school,)
        ).fetchone()[0]
        building = c.execute(
            "SELECT parent_location_id FROM campus_locations WHERE id=%s", (room,)
        ).fetchone()[0]
        n25 = uuid4()
        c.execute(
            (
                "INSERT INTO dormitory_building_profile_versions(id,school_id"
                ",location_id,source_key,usage_kind,display_name,is_available"
                ",valid_from,version,source_id) VALUES(%s,%s,%s,'f6-building'"
                ",'building','综合楼寄宿区',true,%s,1,%s)"
            ),
            (n25, school, building, _at(1), source),
        )
        room_profile = c.execute(
            "SELECT id FROM dorm_room_profile_versions WHERE room_location_id=%s", (room,)
        ).fetchone()[0]
        old_batch, _, requester, assignment = c.execute(
            (
                "SELECT id,source_sha256,confirmed_by_person_id,confirmed_by_"
                "assignment_id FROM master_data_import_batches WHERE school_i"
                "d=%s LIMIT 1"
            ),
            (school,),
        ).fetchone()
        batch, idempotency = uuid4(), uuid4()
        digest = batch.hex * 2
        changes = {
            "id": str(batch),
            "request_id": str(uuid4()),
            "source_ref": "F6 reader synthetic publication",
            "source_sha256": digest,
            "idempotency_key_hash": digest,
            "confirmation_token_sha256": digest,
            "planned_units": 6,
            "succeeded_units": 6,
        }
        c.execute(
            (
                "INSERT INTO master_data_import_batches SELECT (jsonb_populat"
                "e_record(NULL::master_data_import_batches,to_jsonb(b)||%s::j"
                "sonb)).* FROM master_data_import_batches b WHERE id=%s"
            ),
            (json.dumps(changes), old_batch),
        )
        changes = {
            "id": str(idempotency),
            "idempotency_key_hash": digest,
            "resource_id": str(batch),
            "result_payload": {
                "resource_type": "master_data.import_batch",
                "resource_id": str(batch),
                "resource_version": 1,
                "outcome_code": "applied",
            },
        }
        c.execute(
            (
                "INSERT INTO command_idempotency_records SELECT (jsonb_popula"
                "te_record(NULL::command_idempotency_records,to_jsonb(i)||%s:"
                ":jsonb)).* FROM command_idempotency_records i JOIN master_da"
                "ta_import_command_outcomes o ON o.idempotency_record_id=i.id"
                " WHERE o.batch_id=%s"
            ),
            (json.dumps(changes), old_batch),
        )
        changes = {
            "id": str(uuid4()),
            "idempotency_record_id": str(idempotency),
            "batch_id": str(batch),
            "succeeded_units": [1, 2, 3, 4, 5, 6],
            "executed_units": [1, 2, 3, 4, 5, 6],
        }
        c.execute(
            (
                "INSERT INTO master_data_import_command_outcomes SELECT (json"
                "b_populate_record(NULL::master_data_import_command_outcomes,"
                "to_jsonb(o)||%s::jsonb)).* FROM master_data_import_command_o"
                "utcomes o WHERE batch_id=%s"
            ),
            (json.dumps(changes), old_batch),
        )
        keys = {
            "dormitory_building_profile": "f6-building",
            "dorm_room": "f6-room",
            "dorm_bed": f"bed:f6:{bed}",
            "student_bed_residence": "f6-residence",
            "student_school_relationship": "f6-reader-school",
        }
        keys["dorm_bed"] = c.execute(
            "SELECT source_key FROM dorm_beds WHERE id=%s", (bed,)
        ).fetchone()[0]
        quality = uuid4()
        keys["fact_quality"] = "f6-quality"
        c.execute(
            (
                "INSERT INTO school_fact_qualities(id,school_id,source_key,qu"
                "ality_kind,target_fact_kind,owner_kind,location_id,target_so"
                "urce_key,field_name,basis_reference,responsibility,verified_"
                "at,source_status,valid_from,version,source_id,import_batch_i"
                "d) VALUES(%s,%s,'f6-quality','source','dorm_bed','location',"
                "%s,%s,'display_name','人工核验','宿管岗位',%s,'unavailable',%s,1,%s,"
                "%s)"
            ),
            (quality, school, room, keys["dorm_bed"], _at(1), _at(1), source, batch),
        )
        for sequence, (kind, target) in enumerate(
            (
                ("dormitory_building_profile", n25),
                ("dorm_room", room_profile),
                ("dorm_bed", bed),
                ("student_bed_residence", residence),
                ("fact_quality", quality),
                (
                    "student_school_relationship",
                    c.execute(
                        "SELECT id FROM student_school_relationships WHERE school_id=%s",
                        (school,),
                    ).fetchone()[0],
                ),
            ),
            1,
        ):
            unit = uuid4()
            c.execute(
                (
                    "INSERT INTO master_data_import_units(id,school_id,batch_id,s"
                    "equence,row_number,unit_kind,status,payload,payload_sha256,s"
                    "oft_anomalies,initial_soft_anomalies,target_id,target_versio"
                    "n,version) VALUES(%s,%s,%s,%s,%s,'school_fact','applied',%s:"
                    ":jsonb,%s,'[]','[]',%s,1,1)"
                ),
                (
                    unit,
                    school,
                    batch,
                    sequence,
                    sequence,
                    json.dumps(
                        {
                            "fact_kind": kind,
                            "source_key": keys[kind],
                            "operation": "upsert",
                            "expected_version": 0,
                            "effective_at": _at(1).isoformat(),
                        }
                    ),
                    unit.hex * 2,
                    target,
                ),
            )
            c.execute(
                (
                    "INSERT INTO school_fact_change_lineage(id,school_id,batch_id"
                    ",unit_id,fact_kind,fact_source_key,operation,target_id,targe"
                    "t_version,source_sha256,sheet_code,row_number) VALUES(%s,%s,"
                    "%s,%s,%s,%s,'upsert',%s,1,%s,'f6_reader_fixture',%s)"
                ),
                (uuid4(), school, batch, unit, kind, keys[kind], target, digest, sequence),
            )
        adult, relation, verifier, verify_assignment = uuid4(), uuid4(), uuid4(), uuid4()
        c.execute(
            (
                "INSERT INTO school_people(id,school_id,source_key,status,ver"
                "sion) VALUES(%s,%s,'f6-reader-adult','active',1)"
            ),
            (adult, school),
        )
        c.execute(
            (
                "INSERT INTO school_people(id,school_id,source_key,status,ver"
                "sion) VALUES(%s,%s,'f6-reader-verifier','active',1)"
            ),
            (verifier, school),
        )
        c.execute(
            (
                "INSERT INTO staff_assignments(id,school_id,source_key,person"
                "_id,organization_unit_id,position_title,semantic_role_code,a"
                "ssignment_kind,appointment_source,approval_reference,valid_f"
                "rom,status,version) SELECT %s,school_id,'f6-verifier-assignm"
                "ent',%s,organization_unit_id,position_title,semantic_role_co"
                "de,assignment_kind,appointment_source,approval_reference,val"
                "id_from,status,version FROM staff_assignments WHERE id=%s"
            ),
            (verify_assignment, verifier, assignment),
        )
        c.execute(
            (
                "INSERT INTO guardian_relationships(id,school_id,guardian_per"
                "son_id,student_id,relationship_kind,verification_source,veri"
                "fication_reference,verified_at,valid_from,valid_to,status,ve"
                "rsion,authority_version,access_purpose,requested_by_person_i"
                "d,requested_by_assignment_id,requested_at,verified_by_person"
                "_id,verified_by_assignment_id,source_id) VALUES(%s,%s,%s,%s,"
                "'verified_guardian','manual','f6-reader-test',%s,%s,NULL,'co"
                "nfirmed',1,1,'guardian_student_profile',%s,%s,%s,%s,%s,%s)"
            ),
            (
                relation,
                school,
                adult,
                student,
                _at(1),
                _at(1),
                requester,
                assignment,
                _at(1),
                verifier,
                verify_assignment,
                source,
            ),
        )
        c.execute("SET CONSTRAINTS ALL IMMEDIATE")
        c.execute("RESET ROLE")
        c.execute("SET SESSION AUTHORIZATION campus_ai_school_fact_reader")
        assert c.execute(
            "SELECT SESSION_USER,current_user,(SELECT rolsuper FROM pg_ro"
            "les WHERE rolname=current_user)"
        ).fetchone() == ("campus_ai_school_fact_reader", "campus_ai_school_fact_reader", False)
        yield (
            c,
            dict(
                school=school,
                adult=adult,
                relation=relation,
                student=student,
                keys=keys,
                student_key=f"student:f6:{student}",
                room_key=f"room:f6:{room}",
                building_key=f"building:f6:{room}",
                sessions=sessions,
            ),
        )
    finally:
        transaction.rollback()
        db_connection.close()
        engine.dispose()


def targets(c, data, kind, fields, **kw):
    return c.execute(
        "SELECT app.school_fact_f6_targets(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (
            data["school"],
            kind,
            kw.get("keys", [data["keys"][kind]]),
            fields,
            kw.get("at", datetime.now(UTC)),
            kw.get("name"),
            None,
            None,
            0,
            25,
        ),
    ).fetchone()[0]


def number_candidates(c, data, *, number="STU-f6-reader-school", fields=None, **selection):
    return c.execute(
        "SELECT source_key,label,payload FROM app.school_fact_f3_staff_candidates("
        "%s,%s,%s,%s,%s,%s,%s,%s)",
        (
            selection.get("school", data["school"]),
            "student_school_relationship",
            selection.get("keys", [data["keys"]["student_school_relationship"]]),
            selection.get("at", _at(2)),
            fields if fields is not None else ["student_number", "student_source_key"],
            "student_number",
            number,
            selection.get("limit", 2),
        ),
    ).fetchall()


def test_real_student_number_lookup_is_exact_scoped_and_timed(reader_fixture):
    c, data = reader_fixture
    rows = number_candidates(c, data)
    assert len(rows) == 1
    assert rows[0][0] == data["keys"]["student_school_relationship"]
    assert rows[0][2]["content"] == {
        "student_number": "STU-f6-reader-school",
        "student_source_key": data["student_key"],
    }
    assert number_candidates(c, data, number="STU-f6-reader") == []
    assert number_candidates(c, data, keys=[]) == []
    assert number_candidates(c, data, at=_at(1) - timedelta(seconds=1)) == []
    with pytest.raises(psycopg.errors.InvalidAuthorizationSpecification), c.transaction():
        number_candidates(c, data, school=uuid4())
    with pytest.raises(psycopg.errors.InsufficientPrivilege), c.transaction():
        c.execute("SELECT student_number FROM student_school_relationships")


def test_real_student_number_filter_cannot_read_an_unauthorized_match_field(reader_fixture):
    c, data = reader_fixture
    assert number_candidates(c, data, fields=["student_source_key"]) == []
    rows = number_candidates(c, data, fields=["student_number"])
    assert len(rows) == 1 and set(rows[0][2]["content"]) == {"student_number"}
    assert number_candidates(c, data, limit=22) == []
    assert number_candidates(c, data, keys=["absent"] * 101) == []


@pytest.mark.parametrize("formal_future", [False, True])
def test_real_same_number_in_another_campus_remains_two_timed_candidates(
    reader_fixture, formal_future
):
    from tests.school_facts.test_f6_sample_publication_postgres import entry

    c, data = reader_fixture
    before = datetime.now(UTC) + timedelta(days=1) if formal_future else _at(2)
    start = datetime.now(UTC) + timedelta(days=2) if formal_future else _at(3)
    after = datetime.now(UTC) + timedelta(days=3) if formal_future else _at(4)
    _reader_publication(
        c,
        data,
        [
            entry(
                "location",
                "number-campus-two",
                dict(
                    kind="campus",
                    display_name="同号第二校区",
                    display_path="/同号第二校区",
                    status="active",
                ),
            ),
            entry(
                "person",
                "number-person-two",
                dict(
                    display_name="同号学生",
                    person_kind="other",
                    lifecycle_status="active",
                ),
            ),
            entry(
                "student",
                "number-student-two",
                dict(
                    person_source_key="number-person-two",
                    status="active",
                ),
            ),
            entry(
                "student_school_relationship",
                "number-school-two",
                dict(
                    student_source_key="number-student-two",
                    student_number="STU-f6-reader-school",
                    enrollment_status="enrolled",
                    residence_mode="day",
                    primary_campus_source_key="number-campus-two",
                    administrative_campus_source_key="number-campus-two",
                ),
                at=start,
            ),
        ],
    )
    keys = [data["keys"]["student_school_relationship"], "number-school-two"]

    def candidates(**kwargs):
        if not formal_future:
            return number_candidates(c, data, **kwargs)
        from app.composition.school_fact_family_staff_reader import SqlFamilyStaffProjection

        statement = SqlFamilyStaffProjection._future_relationship_statement(
            data["school"],
            kwargs["keys"],
            kwargs.get("fields", ["student_number"]),
            kwargs["at"],
            "student_number",
            "STU-f6-reader-school",
            2,
        )
        with data["sessions"]() as session:
            return session.execute(statement).all()

    assert len(candidates(keys=keys, at=before)) == 1
    rows = candidates(
        keys=keys,
        at=after,
        fields=[
            "student_number",
            "student_source_key",
            "primary_campus_source_key",
        ],
    )
    assert len(rows) == 2
    assert len({r[2]["content"]["student_source_key"] for r in rows}) == 2
    assert len({r[2]["content"]["primary_campus_source_key"] for r in rows}) == 2
    assert len(candidates(keys=keys[:1], at=after)) == 1


def test_real_f6_future_student_number_uses_owner_sql_projection_and_current_authority(
    reader_fixture,
):
    from contextlib import contextmanager
    from types import SimpleNamespace

    from app.composition.school_fact_family_authority import (
        FamilyFactAuthorityDenied,
        FamilyFactTarget,
    )
    from app.composition.school_fact_family_settings import FamilyConfiguration
    from app.composition.school_fact_family_staff_reader import (
        FamilyStaffRepository,
        SqlFamilyStaffProjection,
    )
    from app.platform import contracts as pc

    c, data = reader_fixture
    now, future = datetime.now(UTC), datetime.now(UTC) + timedelta(days=1)
    actor = pc._issue_staff_actor_context(
        account_user_id=1,
        school_id=pc.SchoolId(data["school"]),
        person_id=pc.PersonId(uuid4()),
        assignment_id=pc.AssignmentId(uuid4()),
        assignment_version=1,
        authorization_version=1,
    )
    reads, unsupported = [], []
    state = {"allowed": True, "field": True, "revoke_after_read": False}

    class Executor:
        @contextmanager
        def validated_read(self, before, after):
            before()
            with data["sessions"]() as session:

                def fetch(relation):
                    reads.append(relation.operation_code)
                    result = tuple(
                        dict(row) for row in session.execute(relation.data_statement).mappings()
                    )
                    if state["revoke_after_read"]:
                        state["allowed"] = False
                    return result

                yield SimpleNamespace(fetch=fetch)
            after()

    class Evidence:
        def unsupported(self):
            unsupported.append(True)

        def record(self, *args, **kwargs):
            pytest.fail("Future selection cannot use the F3 current/history signature")

    def require(a, action, kind, source, creation, checked_at):
        if not state["allowed"]:
            raise FamilyFactAuthorityDenied("current grant revoked")
        fields = (
            ("student_source_key", "student_number") if state["field"] else ("student_source_key",)
        )
        target = FamilyFactTarget(kind, source, fields, a.authorization_version, creation)
        return SimpleNamespace(actor=a, target=target, scope=SimpleNamespace(field_names=fields))

    def revalidate(*args):
        if not state["allowed"]:
            raise FamilyFactAuthorityDenied("current grant revoked")

    repository = FamilyStaffRepository(
        projection=SqlFamilyStaffProjection(Executor(), Evidence()),
        configuration=FamilyConfiguration(
            approval_reference="owned-f6-future",
            scope_bindings=(),
            source_catalog={
                "student_school_relationship": (data["keys"]["student_school_relationship"],)
            },
        ),
        reference_key=b"f6-owned-future-reference-key",
        authorize_scope=require,
        revalidate=revalidate,
        clock=lambda: now,
    )
    arguments = dict(
        actor=actor,
        fact_kind="student_school_relationship",
        student_number="STU-f6-reader-school",
        as_of=future,
    )
    result = repository.query(**arguments, _allow_formal_future=True)
    assert result["status"] == "known" and len(result["records"]) == 1
    record = result["records"][0]
    assert record["content"] == {
        "student_source_key": data["student_key"],
        "student_number": "STU-f6-reader-school",
    }
    assert datetime.fromisoformat(record["factual_at"]) == future
    assert datetime.fromisoformat(record["valid_from"]) == _at(1)
    assert record["source_reference"].startswith("publication:") and record["source_published_at"]
    assert unsupported and len(reads) == 1
    # Actual HTTP -> views -> eligibility -> F3 product query -> narrow SQL,
    # with only the current grant fixture replaced, never the consumer objects.
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.composition.school_fact_family_product_adapter import (
        FamilyStaffProductQuery,
        SchoolFactFamilyProductAdapter,
    )
    from app.composition.school_fact_residence_dependencies import ResidenceEligibilityAdapter
    from app.composition.school_fact_residence_views import ResidenceMaintenanceViews
    from app.interfaces.http.school_fact_residence import create_residence_product_router

    query = FamilyStaffProductQuery(repository, SchoolFactFamilyProductAdapter(repository), None)
    eligibility = ResidenceEligibilityAdapter(
        education=SimpleNamespace(family=SimpleNamespace(query=query)), lifecycle=None
    )
    views = ResidenceMaintenanceViews(
        repository=repository,
        adapter=SimpleNamespace(time_context=None),
        external_choices=None,
        query=None,
        eligibility=eligibility,
    )
    identity = SimpleNamespace(
        actor=actor, actor_context_ref="owned-f6-future", session_binding="owned-future-session"
    )
    app = FastAPI()
    app.include_router(
        create_residence_product_router(
            identity_dependency=lambda: identity, views=views, product=None
        )
    )
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/school-fact-product/residence-facts/query",
            json={
                "fact_kind": "student_school_relationship",
                "student_number": "STU-f6-reader-school",
                "as_of": future.isoformat(),
                "limit": 20,
            },
        )
    assert response.status_code == 200
    public = response.json()
    assert public["status"] == "known" and len(public["records"]) == 1
    assert public["records"][0]["fields"]["student_number"] == "STU-f6-reader-school"
    assert public["records"][0]["fields"]["student_ref"].startswith("f3:student:")
    assert (
        repository.query(**{**arguments, "student_number": "unmatched"}, _allow_formal_future=True)[
            "records"
        ]
        == []
    )
    state["field"] = False
    count = len(reads)
    with pytest.raises(FamilyFactAuthorityDenied):
        repository.query(**arguments, _allow_formal_future=True)
    assert len(reads) == count
    state.update(field=True, revoke_after_read=True)
    with pytest.raises(FamilyFactAuthorityDenied):
        repository.query(**arguments, _allow_formal_future=True)
    assert not state["allowed"]
    # The old public F3 fixed SQL's future boundary remains intact.
    with pytest.raises(psycopg.errors.InvalidParameterValue), c.transaction():
        number_candidates(c, data, at=future)


def test_real_reader_facilities_are_narrow_and_raw_tables_are_denied(reader_fixture):
    c, data = reader_fixture
    for kind in ("dormitory_building_profile", "dorm_room", "dorm_bed"):
        fields = ["room_name"] if kind == "dorm_room" else ["display_name"]
        result = targets(c, data, kind, fields)
        assert result["status"] == "available"
        assert set(result["records"][0]["content"]) == set(fields)
        assert "student" not in json.dumps(result["records"])
    with pytest.raises(psycopg.errors.InsufficientPrivilege), c.transaction():
        c.execute("SELECT * FROM student_bed_residences")
    with pytest.raises(psycopg.errors.InvalidParameterValue), c.transaction():
        targets(c, data, "dorm_bed", ["student_name"])


def test_real_reserved_scope_is_school_bound_and_unpublished_key_reads_empty(reader_fixture):
    c, data = reader_fixture
    sql = "SELECT app.school_fact_f6_scope_is_current(%s,'student_bed_residence',%s,now())"
    # Reserved keys are authenticated in the application scope document; only
    # ordinary objects must already have applied lineage. They grant no writes.
    assert c.execute(sql, (data["school"], [])).fetchone()[0] is True
    assert c.execute(sql, (data["school"], ["f6:reserved:unpublished"])).fetchone()[0] is False
    empty = targets(
        c, data, "student_bed_residence", ["bed_name"], keys=["f6:reserved:unpublished"]
    )
    assert empty["records"] == [] and empty["status"] == "no_current_records"
    present = targets(c, data, "student_bed_residence", ["bed_name"])
    assert len(present["records"]) == 1
    assert set(present["records"][0]["content"]) == {"bed_name"}
    with pytest.raises(psycopg.errors.InvalidAuthorizationSpecification), c.transaction():
        c.execute(sql, (uuid4(), []))


def _family_reader_turn(data, now, *, clock=None):
    """Real fixed SQL/Reader proofs; identity and optional wall clock are test controls."""
    from types import SimpleNamespace

    from app.composition.school_fact_residence_gateway import (
        FAMILY_NAME,
        FamilyResidenceToolGateway,
    )
    from app.composition.school_fact_residence_query import FamilyResidenceQueryService
    from app.composition.school_fact_residence_reader import SqlFamilyResidenceRepository
    from app.platform.contracts import (
        GuardianRelationshipId,
        PersonId,
        SchoolId,
        _issue_guardian_actor_context,
    )

    actor = _issue_guardian_actor_context(
        account_user_id=1,
        school_id=SchoolId(data["school"]),
        person_id=PersonId(data["adult"]),
        guardian_relationship_id=GuardianRelationshipId(data["relation"]),
        relationship_version=1,
        authorization_version=1,
    )
    repository = SqlFamilyResidenceRepository(data["sessions"])
    service = FamilyResidenceQueryService(
        repository=repository,
        current_actor=lambda candidate: candidate == actor,
        reference_key=b"f6-owned-pg-proof-reference-key",
        clock=clock if clock is not None else lambda: now,
    )
    identity = SimpleNamespace(actor=actor, session_binding="owned-pg-stub-session")
    turn = FamilyResidenceToolGateway(resolve_actor=lambda _: identity, query=service).for_turn(
        "owned-pg-selected-child"
    )
    result = turn.execute(
        SimpleNamespace(name=FAMILY_NAME, arguments={}),
        SimpleNamespace(actor_context_ref="owned-pg-selected-child"),
    )
    return actor, repository, service, turn, result


def _reader_publication(c, data, changes):
    """Actual shared persistence kernel, with DB-owner writes only inside rollback."""
    from app.composition.school_fact_persistence import SqlAlchemySchoolFactPersistence
    from app.platform.master_data_imports.models import MasterDataImportBatch
    from tests.school_facts.test_f6_sample_publication_postgres import KernelFixture

    c.execute("RESET SESSION AUTHORIZATION")
    c.execute("SET SESSION AUTHORIZATION campus_ai_migration")
    c.execute("SET ROLE campus_ai_db_owner")
    try:
        with data["sessions"]() as session, session.begin():
            # Reader seeding validates immediately; the real atomic writer
            # requires its normal deferred terminal-batch consistency checks.
            c.execute("SET CONSTRAINTS ALL DEFERRED")
            fixture = object.__new__(KernelFixture)
            fixture.connection, fixture.session = c, session
            fixture.school = data["school"]
            fixture.writer = object.__new__(SqlAlchemySchoolFactPersistence)
            fixture.template = session.execute(
                select(MasterDataImportBatch)
                .where(MasterDataImportBatch.school_id == data["school"])
                .limit(1)
            ).scalar_one()
            fixture.apply(changes)
    finally:
        c.execute("RESET ROLE")
        c.execute("RESET SESSION AUTHORIZATION")
        c.execute("SET SESSION AUTHORIZATION campus_ai_school_fact_reader")
    assert c.execute(
        "SELECT SESSION_USER,current_user,"
        "(SELECT rolsuper FROM pg_roles WHERE rolname=current_user)"
    ).fetchone() == ("campus_ai_school_fact_reader", "campus_ai_school_fact_reader", False)


def test_real_empty_family_result_expires_after_formal_residence_creation(reader_fixture):
    """PG publication → Reader → F6 use-time guard, without any model transport."""
    from app.platform.assistant.runtime import AssistantRuntimeError
    from tests.school_facts.test_f6_sample_publication_postgres import entry

    c, data = reader_fixture
    now = datetime.now(UTC)
    _reader_publication(
        c,
        data,
        [
            dict(
                fact_kind="student_bed_residence",
                source_key=data["keys"]["student_bed_residence"],
                operation="end",
                expected_version=1,
                effective_at=now,
                content=None,
            )
        ],
    )
    actor, repository, service, turn, old = _family_reader_turn(data, now)
    assert old.model_content["status"] == "no_current_records"
    assert old.model_content["records"] == []
    assert old.history_check.valid()
    before = repository.residence_stamp(actor)

    _reader_publication(
        c,
        data,
        [
            entry(
                "student_bed_residence",
                "f6-reader-after-empty",
                dict(
                    student_source_key=data["student_key"],
                    bed_source_key=data["keys"]["dorm_bed"],
                ),
                at=now,
            )
        ],
    )
    assert repository.residence_stamp(actor) == before + 1
    assert not old.history_check.valid()
    with pytest.raises(AssistantRuntimeError):
        turn.revalidate("owned-pg-selected-child")
    fresh = service.query(actor=actor)
    assert len(fresh.public()["records"]) == 1
    assert fresh.evidence.valid(actor)


def test_real_family_relationship_change_expires_existing_result(reader_fixture):
    """Actual relation-version change invalidates F6 reuse; no logout/model claim."""
    from app.platform.assistant.runtime import AssistantRuntimeError

    c, data = reader_fixture
    actor, repository, service, turn, old = _family_reader_turn(data, datetime.now(UTC))
    assert len(old.model_content["records"]) == 1
    assert old.history_check.valid()
    assert repository.authority(actor) is not None

    c.execute("RESET SESSION AUTHORIZATION")
    c.execute("SET SESSION AUTHORIZATION campus_ai_migration")
    c.execute("SET ROLE campus_ai_db_owner")
    c.execute(
        "UPDATE guardian_relationships SET version=version+1 WHERE school_id=%s AND id=%s",
        (data["school"], data["relation"]),
    )
    c.execute("SET CONSTRAINTS ALL IMMEDIATE")
    c.execute("RESET ROLE")
    c.execute("RESET SESSION AUTHORIZATION")
    c.execute("SET SESSION AUTHORIZATION campus_ai_school_fact_reader")
    assert c.execute("SELECT SESSION_USER,current_user").fetchone() == (
        "campus_ai_school_fact_reader",
        "campus_ai_school_fact_reader",
    )
    assert repository.authority(actor) is None
    assert not old.history_check.valid()
    with pytest.raises(AssistantRuntimeError):
        turn.revalidate("owned-pg-selected-child")
    assert service.query(actor=actor).public()["status"] == "denied"


@pytest.mark.parametrize("identity_change", ["logout", "select_another_child"])
def test_real_family_result_is_blocked_at_release_after_identity_changes(
    reader_fixture, identity_change
):
    """Real F6 Reader evidence, with a controlled login/child-selection boundary."""
    from types import SimpleNamespace

    from app.composition.school_fact_read_evidence import validation_batch
    from app.platform.assistant.runtime import AssistantRuntimeError
    from app.platform.contracts import GuardianRelationshipId, _issue_guardian_actor_context

    _, data = reader_fixture
    actor, _, service, turn, old = _family_reader_turn(data, datetime.now(UTC))
    assert len(old.model_content["records"]) == 1 and old.history_check.valid()
    selected = {"actor": actor, "session": "owned-pg-stub-session"}
    service.current_actor = lambda candidate: selected["actor"] == candidate
    turn.owner.resolve_actor = lambda _: SimpleNamespace(
        actor=selected["actor"], session_binding=selected["session"]
    )
    with pytest.raises(AssistantRuntimeError):
        with validation_batch():
            turn.revalidate("owned-pg-selected-child")
            assert old.history_check.valid()
            if identity_change == "logout":
                selected.update(actor=None, session="logged-out")
            else:
                selected["actor"] = _issue_guardian_actor_context(
                    account_user_id=actor.account_user_id,
                    school_id=actor.school_id,
                    person_id=actor.person_id,
                    guardian_relationship_id=GuardianRelationshipId(uuid4()),
                    relationship_version=1,
                    authorization_version=actor.authorization_version,
                )
                selected["session"] = "another-selected-child"
    assert not old.history_check.valid()
    with pytest.raises(AssistantRuntimeError):
        turn.revalidate("owned-pg-selected-child")
    assert service.query(actor=actor).public()["status"] == "denied"


def test_real_family_current_result_expires_at_formal_end_and_reads_new_state(reader_fixture):
    """One formal future end, constant version, real wall clock and F6 SQL."""
    from time import sleep

    from app.platform.assistant.runtime import AssistantRuntimeError

    c, data = reader_fixture
    boundary = datetime.now(UTC) + timedelta(seconds=8)
    _reader_publication(
        c,
        data,
        [
            dict(
                fact_kind="student_bed_residence",
                source_key=data["keys"]["student_bed_residence"],
                operation="end",
                expected_version=1,
                effective_at=boundary,
                content=None,
            )
        ],
    )
    before = datetime.now(UTC)
    assert before < boundary, "Fixture was too slow to observe before the formal boundary"
    actor, repository, service, turn, old = _family_reader_turn(
        data, before, clock=lambda: datetime.now(UTC)
    )
    assert len(old.model_content["records"]) == 1 and old.history_check.valid()
    version = repository.residence_stamp(actor)
    remaining = (boundary - datetime.now(UTC)).total_seconds()
    assert remaining > 0, "First read did not finish before the formal boundary"
    sleep(remaining + 0.1)
    assert repository.residence_stamp(actor) == version
    assert not old.history_check.valid()
    with pytest.raises(AssistantRuntimeError):
        turn.revalidate("owned-pg-selected-child")
    raw_at_boundary = repository.residence_read(actor, as_of=None)
    assert raw_at_boundary["status"] == "no_current_records"
    fresh = service.query(actor=actor)
    assert fresh.public()["status"] == "no_current_records"
    assert fresh.public()["records"] == [] and fresh.evidence.valid(actor)


def test_product_explicit_replacement_is_consumed_by_real_atomic_writer(
    current_owner_connection, current_owner_session
):
    """Product planner → canonical → real shared kernel; authority is covered separately."""
    from types import SimpleNamespace

    from scripts.f6_engineering.prepare_samples import CURRENT, build_plan
    from sqlalchemy.exc import IntegrityError

    from app.composition.school_fact_residence_product import (
        ResidenceBatchInput,
        ResidenceProductAdapter,
    )
    from tests.school_facts.test_f6_sample_publication_postgres import KernelFixture, entry

    c, session = current_owner_connection, current_owner_session
    assert c.execute("SELECT count(*) FROM schools").fetchone()[0] == 0
    fixture = KernelFixture(c, session)
    fixture.seed_students()
    for batch in build_plan()["batches"].values():
        fixture.apply(batch["changes"])
    at = datetime.fromisoformat(CURRENT)
    keys = ("f6:residence:a-current", "f6:residence:a-future")
    base = fixture.writer._current_global_fact_version(session, fixture.school)

    def bound_proof(observed_version):
        def revalidate(**kwargs):
            current = fixture.writer._current_global_fact_version(session, fixture.school)
            if current != observed_version:
                raise RuntimeError("formal set changed")

        return SimpleNamespace(revalidate=revalidate)

    proof = bound_proof(base)
    ids = {}

    def read(**kwargs):
        assert kwargs["source_key"] in keys and not kwargs.get("creation")
        row = c.execute(
            "SELECT r.id,r.valid_from,r.valid_to,l.target_version,s.source_key,b.source_key "
            "FROM school_fact_change_lineage l JOIN student_bed_residences r "
            "ON r.school_id=l.school_id AND r.id=l.target_id "
            "JOIN students s ON s.school_id=r.school_id AND s.id=r.student_id "
            "JOIN dorm_beds b ON b.school_id=r.school_id AND b.id=r.bed_id "
            "WHERE l.school_id=%s AND l.fact_kind='student_bed_residence' AND l.fact_source_key=%s "
            "ORDER BY l.target_version DESC LIMIT 1",
            (fixture.school, kwargs["source_key"]),
        ).fetchone()
        ids[kwargs["source_key"]] = row[0]
        return SimpleNamespace(
            records=(
                {
                    "source_key": kwargs["source_key"],
                    "version": row[3],
                    "segments": [
                        {
                            "effective_at": row[1].isoformat(),
                            "valid_to": row[2].isoformat() if row[2] else None,
                            "content": {"student_source_key": row[4], "bed_source_key": row[5]},
                        }
                    ],
                },
            ),
            school_fact_version=base,
            proof=proof,
        )

    repository = SimpleNamespace(
        source_for_reference=lambda **kw: kw["reference"].split(":", 2)[2],
        reference_for=lambda **kw: "f6:" + kw["fact_kind"] + ":" + kw["source_key"],
        read_for_maintenance=read,
        read_page_context=lambda *a: SimpleNamespace(proof=proof),
    )
    external = SimpleNamespace(reference_for=lambda **kw: "f3:student:" + kw["source_key"])
    adapter = ResidenceProductAdapter(repository=repository, external=external, clock=lambda: at)
    refs = ["f6:student_bed_residence:" + key for key in keys]
    request = ResidenceBatchInput.model_validate_json(
        json.dumps(
            dict(
                fact_kind="student_bed_residence",
                target_ref=refs[0],
                operation="end",
                fields={},
                reason="合成明确集合替换",
                effective_at=CURRENT,
                import_mode="replace",
                import_rows=[],
                import_replacement_refs=refs,
            )
        )
    )
    result = adapter.resolve_change(
        actor=SimpleNamespace(school_id=fixture.school),
        payload=request,
        request_id=uuid4(),
        effective_at=at,
    )
    assert [(x.source_key, x.operation) for x in result.change_set.changes] == [
        (keys[0], "end"),
        (keys[1], "revoke"),
    ]
    # A different approved formal source changes the real publication version.
    # The existing preview is then stale even though its selected keys still exist.
    changed_bed = next(
        row
        for row in build_plan()["batches"]["facilities"]["changes"]
        if row["fact_kind"] == "dorm_bed" and row["source_key"] == "f6:bed:b-301-1"
    )
    fixture.apply(
        [
            {
                **changed_bed,
                "expected_version": 1,
                "effective_at": CURRENT,
                "content": {**changed_bed["content"], "display_name": "已核对的1号床"},
            }
        ]
    )
    with pytest.raises(RuntimeError, match="formal set changed"):
        result.summary_revalidate(SimpleNamespace(school_id=fixture.school))
    base = fixture.writer._current_global_fact_version(session, fixture.school)
    proof = bound_proof(base)
    result = adapter.resolve_change(
        actor=SimpleNamespace(school_id=fixture.school),
        payload=request,
        request_id=uuid4(),
        effective_at=at,
    )
    changes = [
        change.model_dump(mode="json", exclude_unset=True) for change in result.change_set.changes
    ]

    def formal_snapshot():
        return (
            tuple(
                c.execute(
                    "SELECT id,valid_from,valid_to,revoked_at FROM student_bed_residences "
                    "WHERE school_id=%s ORDER BY id",
                    (fixture.school,),
                ).fetchall()
            ),
            *(
                c.execute(
                    f"SELECT count(*) FROM {table} WHERE school_id=%s", (fixture.school,)
                ).fetchone()[0]
                for table in ("school_fact_change_lineage", "master_data_import_batches")
            ),
            fixture.writer._current_global_fact_version(session, fixture.school),
        )

    before = formal_snapshot()
    # Failure in a later unit must also undo both earlier replacement units.
    with pytest.raises(IntegrityError) as error, session.begin_nested():
        fixture.apply(
            [
                *changes,
                entry(
                    "student_bed_residence",
                    "f6:replacement:invalid-day-student",
                    dict(student_source_key="f3:student:b", bed_source_key="f6:bed:b-301-1"),
                    at=CURRENT,
                ),
            ],
            check_preflight=False,
        )
    assert error.value.orig.sqlstate == "23514"
    assert formal_snapshot() == before
    result.summary_revalidate(SimpleNamespace(school_id=fixture.school))
    count = c.execute(
        "SELECT count(*) FROM master_data_import_batches WHERE school_id=%s", (fixture.school,)
    ).fetchone()[0]
    fixture.apply(changes)
    assert (
        c.execute(
            "SELECT count(*) FROM master_data_import_batches WHERE school_id=%s", (fixture.school,)
        ).fetchone()[0]
        == count + 1
    )
    current = c.execute(
        "SELECT valid_from,valid_to,revoked_at FROM student_bed_residences WHERE id=%s",
        (ids[keys[0]],),
    ).fetchone()
    future = c.execute(
        "SELECT valid_from,valid_to,revoked_at FROM student_bed_residences WHERE id=%s",
        (ids[keys[1]],),
    ).fetchone()
    assert current[0] < at and current[1] == at and current[2] is None
    assert future[0] > at and future[1] is None and future[2] is not None
    with pytest.raises(RuntimeError):
        result.summary_revalidate(SimpleNamespace(school_id=fixture.school))


def test_real_reader_selected_child_future_and_wrong_relation(reader_fixture):
    c, data = reader_fixture
    sql = "SELECT app.school_fact_family_residence(%s,%s,%s,1,%s)"
    for at in (datetime.now(UTC), datetime.now(UTC) + timedelta(days=7)):
        result = c.execute(sql, (data["school"], data["adult"], data["relation"], at)).fetchone()[0]
        assert result["status"] == "available"
        assert result["student_id"] == str(data["student"])
        assert len(result["records"]) == 1
        assert result["records"][0]["building_name"] == "综合楼寄宿区"
        assert "bed_source_key" not in result["records"][0]
        assert any(
            q.get("source_status") == "unavailable" and q["target_fact_kind"] == "dorm_bed"
            for q in result["records"][0]["quality"]
        )
    with pytest.raises(psycopg.Error), c.transaction():
        c.execute(sql, (data["school"], uuid4(), data["relation"], None))


def test_real_maintenance_timeline_legacy_room_and_complete_impacts(reader_fixture):
    c, data = reader_fixture
    for kind, fields in (
        ("dorm_room", ["room_source_key", "authorized_bed_capacity"]),
        ("student_bed_residence", ["student_source_key", "bed_source_key"]),
    ):
        result = c.execute(
            "SELECT app.school_fact_f6_maintenance_target(%s,%s,%s,%s,false)",
            (data["school"], kind, data["keys"][kind], fields),
        ).fetchone()[0]
        assert result["records"][0]["version"] == 1
        assert len(result["records"][0]["segments"]) == 1
        assert set(result["records"][0]["segments"][0]["content"]) == set(fields)
    sql = "SELECT app.school_fact_f6_student_impacts(%s,%s,%s,now())"
    with pytest.raises(psycopg.errors.InvalidAuthorizationSpecification), c.transaction():
        c.execute(sql, (data["school"], data["student_key"], []))
    result = c.execute(
        sql, (data["school"], data["student_key"], [data["keys"]["student_bed_residence"]])
    ).fetchone()[0]
    assert len(result["records"]) == 1


def test_real_writer_and_runtime_cannot_execute_reader_api(reader_fixture):
    c, data = reader_fixture
    for role in ("campus_ai_school_fact_writer", "campus_ai_runtime"):
        c.execute("RESET SESSION AUTHORIZATION")
        c.execute("SET SESSION AUTHORIZATION " + role)
        with pytest.raises(psycopg.errors.InsufficientPrivilege), c.transaction():
            targets(c, data, "dorm_bed", ["display_name"])


def test_real_related_room_building_and_f3_eligibility_are_exact(reader_fixture):
    c, data = reader_fixture
    sql = (
        "SELECT app.school_fact_f6_targets(%s,'student_bed_residence'"
        ",%s,ARRAY['room_name','bed_name'],now(),NULL,'location',%s,0"
        ",25)"
    )
    for parent in (data["room_key"], data["building_key"]):
        value = c.execute(
            sql, (data["school"], [data["keys"]["student_bed_residence"]], parent)
        ).fetchone()[0]
        assert len(value["records"]) == 1
    value = c.execute(
        sql, (data["school"], [data["keys"]["student_bed_residence"]], "location:other")
    ).fetchone()[0]
    assert value["records"] == []
    sql = "SELECT app.school_fact_f6_student_relationship_keys(%s,%s,%s)"
    with pytest.raises(psycopg.errors.InvalidAuthorizationSpecification), c.transaction():
        c.execute(sql, (data["school"], data["student_key"], []))
    value = c.execute(sql, (data["school"], data["student_key"], ["f6-reader-school"])).fetchone()[
        0
    ]
    assert value["source_keys"] == ["f6-reader-school"]


def test_real_quality_coordinates_body_and_owner_root_boundaries(reader_fixture):
    c, data = reader_fixture
    coordinate = c.execute(
        "SELECT app.school_fact_f6_quality_coordinates(%s,'f6-quality')", (data["school"],)
    ).fetchone()[0]
    assert set(coordinate["content"]) == {
        "target_fact_kind",
        "target_source_key",
        "replacement_source_key",
        "field_name",
        "owner_kind",
        "negative_code",
        "owner_source_key",
    }
    assert "responsibility" not in coordinate["content"]
    detail = c.execute(
        "SELECT app.school_fact_f6_quality_detail(%s,'f6-quality')", (data["school"],)
    ).fetchone()[0]
    assert detail["content"]["responsibility"] == "宿管岗位"
    assert detail["content"]["source_status"] == "unavailable"
    sql = "SELECT app.school_fact_f6_quality_owners_current(%s,%s,%s,%s,now())"
    assert (
        c.execute(sql, (data["school"], "dorm_bed", ["location"], [data["room_key"]])).fetchone()[0]
        is True
    )
    assert (
        c.execute(sql, (data["school"], "dorm_bed", ["location"], ["unrecorded"])).fetchone()[0]
        is False
    )
    assert (
        c.execute(sql, (data["school"], "dorm_bed", ["person"], ["unknown"])).fetchone()[0] is False
    )
