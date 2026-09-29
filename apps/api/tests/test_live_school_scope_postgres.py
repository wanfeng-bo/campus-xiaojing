"""New isolated synthetic cluster: metadata enumeration never projects fact contents."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import psycopg
import pytest
from psycopg.types.json import Jsonb

from app.platform.school_facts.f2_policy import F2FactScope
from tests import test_live_initialization_postgres as initialization
from tests.school_facts.test_sf1_postgres_constraints import (
    _insert_bed,
    _insert_person_and_student,
    _insert_residence,
    _insert_room,
    _insert_source,
)
from tests.school_facts.test_sf2_migration import _run_alembic
from tests.test_dingtalk_product_identity_postgres import identity_template  # noqa: F401

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_DINGTALK_POSTGRES_INTEGRATION") != "1",
    reason="explicit new owned disposable PostgreSQL cluster required",
)


def test_live_school_scope_metadata_guards_and_complete_keyset(
    identity_template,  # noqa: F811
    monkeypatch,
    tmp_path,
):
    def exercise(roles):
        __tracebackhide__ = True
        roles.verify_disposable_cluster()
        upgrade = _run_alembic(roles, "upgrade", "20260928_57")
        assert upgrade.returncode == 0, "Migration 57 did not apply to the disposable fixture"
        with roles.admin_connection() as connection:
            connection.autocommit = False
            school = connection.execute(
                "SELECT school_id FROM public.school_database_anchor"
            ).fetchone()[0]
            original = connection.execute(
                "SELECT id,unit_id FROM public.school_fact_change_lineage ORDER BY id LIMIT 1"
            ).fetchone()
            for index in range(205):
                unit, lineage, target = uuid4(), uuid4(), uuid4()
                connection.execute(
                    "INSERT INTO public.master_data_import_units SELECT (jsonb_populate_record("
                    "NULL::public.master_data_import_units,to_jsonb(u)||jsonb_build_object("
                    "'id',%s::text,'sequence',%s::bigint))).* "
                    "FROM public.master_data_import_units u WHERE id=%s",
                    (str(unit), index + 1000, original[1]),
                )
                connection.execute(
                    "INSERT INTO public.school_fact_change_lineage SELECT (jsonb_populate_record("
                    "NULL::public.school_fact_change_lineage,to_jsonb(l)||jsonb_build_object("
                    "'id',%s::text,'unit_id',%s::text,'target_id',%s::text,"
                    "'fact_kind','dorm_bed','fact_source_key',%s::text))).* "
                    "FROM public.school_fact_change_lineage l WHERE id=%s",
                    (
                        str(lineage),
                        str(unit),
                        str(target),
                        f"synthetic:bed:{index:04}",
                        original[0],
                    ),
                )
            source = _seed_student_impact(connection, school, original)
            assignment, scopes, grants = _seed_quality_scopes(connection, school, original, source)
            query = "SELECT app.school_fact_scope_keys(%s,%s,%s,%s,%s)"
            connection.execute("SET SESSION AUTHORIZATION campus_ai_school_fact_reader")
            reader = connection
            keys, after, versions = [], None, set()
            while True:
                page = reader.execute(query, (school, "f6", "dorm_bed", after, 100)).fetchone()[0]
                assert set(page) == {"keys", "has_more", "next_after", "school_fact_version"}
                assert len(page["keys"]) <= 100
                keys.extend(page["keys"])
                versions.add(page["school_fact_version"])
                if not page["has_more"]:
                    assert page["next_after"] is None
                    break
                after = page["next_after"]
            assert keys == [f"synthetic:bed:{index:04}" for index in range(205)]
            assert len(versions) == 1
            impact = reader.execute(
                "SELECT app.school_fact_f6_student_impact_keys(%s,%s,%s)",
                (school, "student:synthetic-impact", datetime.now(UTC)),
            ).fetchone()[0]
            assert impact == {
                "keys": ["synthetic:impact"],
                "school_fact_version": next(iter(versions)),
            }
            complete = reader.execute(
                "SELECT app.school_fact_f6_student_impacts(%s,%s,%s,%s)",
                (school, "student:synthetic-impact", impact["keys"], datetime.now(UTC)),
            ).fetchone()[0]
            assert len(complete["records"]) == 1
            relationship_query = "SELECT app.school_fact_f6_student_school_relationship_keys(%s,%s)"
            relationships = reader.execute(
                relationship_query, (school, "student:synthetic-impact")
            ).fetchone()[0]
            assert relationships == {
                "keys": ["synthetic:relationship"],
                "school_fact_version": next(iter(versions)),
            }
            assert (
                reader.execute(
                    "SELECT app.school_fact_f6_student_relationship_keys(%s,%s,%s)",
                    (school, "student:synthetic-impact", relationships["keys"]),
                ).fetchone()[0]["source_keys"]
                == relationships["keys"]
            )
            with pytest.raises(psycopg.Error), connection.transaction():
                reader.execute(
                    "SELECT app.school_fact_f6_student_relationship_keys(%s,%s,%s)",
                    (school, "student:synthetic-impact", []),
                )
            for selected in ((uuid4(), "student:synthetic-impact"), (school, "*")):
                with pytest.raises(psycopg.Error), connection.transaction():
                    reader.execute(relationship_query, selected)
            with pytest.raises(psycopg.Error), connection.transaction():
                reader.execute(
                    "SELECT app.school_fact_f6_student_impacts(%s,%s,%s,%s)",
                    (school, "student:synthetic-impact", [], datetime.now(UTC)),
                )
            empty = reader.execute(query, (school, "f4", "education_class", None, 100)).fetchone()[
                0
            ]
            assert empty["keys"] == [] and not empty["has_more"]
            quality_query = "SELECT app.school_fact_f7_package_quality_kind(%s,%s,%s,%s)"
            for label in ("school", "exact"):
                assert reader.execute(
                    quality_query, (school, assignment, scopes[label], "synthetic:quality")
                ).fetchone() == ("official_contact",)
            for scope in (scopes["other"], scopes["expired"], uuid4()):
                with pytest.raises(psycopg.Error), connection.transaction():
                    reader.execute(quality_query, (school, assignment, scope, "synthetic:quality"))
            with pytest.raises(psycopg.Error), connection.transaction():
                reader.execute(
                    quality_query, (school, uuid4(), scopes["school"], "synthetic:quality")
                )
            connection.execute("RESET SESSION AUTHORIZATION")
            connection.execute(
                "INSERT INTO public.governance_grant_versions SELECT (jsonb_populate_record("
                "NULL::public.governance_grant_versions,to_jsonb(v)||jsonb_build_object("
                "'id',%s::text,'version',2,'transition_kind','revoke',"
                "'effective_from',statement_timestamp(),'revoked_at',statement_timestamp()))).* "
                "FROM public.governance_grant_versions v WHERE grant_id=%s AND version=1",
                (str(uuid4()), grants["school"]),
            )
            connection.execute("SET SESSION AUTHORIZATION campus_ai_school_fact_reader")
            with pytest.raises(psycopg.Error), connection.transaction():
                reader.execute(
                    quality_query, (school, assignment, scopes["school"], "synthetic:quality")
                )
            for args in (
                (uuid4(), "f6", "dorm_bed", None, 100),
                (school, "f3", "dorm_bed", None, 100),
                (school, "f6", "dorm_bed", None, 101),
                (school, "f6", "dorm_bed", "*", 100),
                (school, "f6", None, None, 100),
            ):
                with pytest.raises(psycopg.Error), connection.transaction():
                    reader.execute(query, args)
            connection.rollback()
        with psycopg.connect(**roles.runtime_connection_parameters, autocommit=True) as runtime:
            with pytest.raises(psycopg.Error):
                runtime.execute(query, (school, "f6", "dorm_bed", None, 100))
        with roles.admin_connection() as connection:
            with pytest.raises(psycopg.Error), connection.transaction():
                connection.execute(query, (school, "f6", "dorm_bed", None, 100))

    initialization.test_formal_initialization_closes_and_preserves_upgrade(
        identity_template, monkeypatch, tmp_path, on_upgraded=exercise
    )


def test_live_school_education_repository_reads_beyond_one_key_page(
    identity_template,  # noqa: F811
    monkeypatch,
    tmp_path,
):
    """Real Reader SQL and repository keep a school read complete across 100-key pages."""
    from contextlib import contextmanager
    from types import SimpleNamespace

    from sqlalchemy import create_engine
    from sqlalchemy.engine import URL
    from sqlalchemy.orm import sessionmaker

    from app.composition.school_fact_education_reader import (
        EducationRepository,
        SqlEducationProjection,
    )
    from app.platform.authorization.secure_reader_adapters import _SqlAlchemyReadTransaction
    from app.platform.contracts import (
        AssignmentId,
        PersonId,
        SchoolId,
        _issue_staff_actor_context,
    )
    from app.platform.school_facts.f4_policy import F4FactScope

    def exercise(roles):
        __tracebackhide__ = True
        roles.verify_disposable_cluster()
        assert _run_alembic(roles, "upgrade", "20260928_57").returncode == 0
        values = roles.admin_connection_parameters
        engine = create_engine(
            URL.create(
                "postgresql+psycopg", username=values["user"], password=values["password"],
                host=values["host"], port=values["port"], database=values["dbname"],
            ), hide_parameters=True,
        )
        connection = engine.connect()
        transaction = connection.begin()

        def seed():
            school = connection.exec_driver_sql(
                "SELECT school_id FROM public.school_database_anchor"
            ).fetchone()[0]
            original = connection.exec_driver_sql(
                "SELECT id,unit_id FROM public.school_fact_change_lineage ORDER BY id LIMIT 1"
            ).fetchone()
            for index in range(105):
                unit, lineage, target = uuid4(), uuid4(), uuid4()
                connection.exec_driver_sql(
                    "INSERT INTO public.master_data_import_units SELECT (jsonb_populate_record("
                    "NULL::public.master_data_import_units,to_jsonb(u)||jsonb_build_object("
                    "'id',%s::text,'sequence',%s::bigint))).* "
                    "FROM public.master_data_import_units u WHERE id=%s",
                    (str(unit), index + 3000, original[1]),
                )
                connection.exec_driver_sql(
                    "INSERT INTO public.school_fact_change_lineage SELECT (jsonb_populate_record("
                    "NULL::public.school_fact_change_lineage,to_jsonb(l)||jsonb_build_object("
                    "'id',%s::text,'unit_id',%s::text,'target_id',%s::text,"
                    "'fact_kind','education_class','fact_source_key',%s::text))).* "
                    "FROM public.school_fact_change_lineage l WHERE id=%s",
                    (
                        str(lineage), str(unit), str(target),
                        f"synthetic:class:{index:04}", original[0],
                    ),
                )
            unit, lineage, target = uuid4(), uuid4(), uuid4()
            connection.exec_driver_sql(
                "INSERT INTO public.master_data_import_units SELECT (jsonb_populate_record("
                "NULL::public.master_data_import_units,to_jsonb(u)||jsonb_build_object("
                "'id',%s::text,'sequence',4000))).* "
                "FROM public.master_data_import_units u WHERE id=%s",
                (str(unit), original[1]),
            )
            connection.exec_driver_sql(
                "INSERT INTO public.school_fact_change_lineage SELECT (jsonb_populate_record("
                "NULL::public.school_fact_change_lineage,to_jsonb(l)||jsonb_build_object("
                "'id',%s::text,'unit_id',%s::text,'target_id',%s::text,"
                "'fact_kind','teaching_course','fact_source_key','synthetic:course:0001'))).* "
                "FROM public.school_fact_change_lineage l WHERE id=%s",
                (str(lineage), str(unit), str(target), original[0]),
            )
            return school

        school = seed()
        connection.exec_driver_sql("SET SESSION AUTHORIZATION campus_ai_school_fact_reader")
        sessions = sessionmaker(
            bind=connection, expire_on_commit=False, join_transaction_mode="create_savepoint"
        )

        class Executor:
            @contextmanager
            def validated_read(self, before_read, after_read):
                before_read()
                with sessions() as session:
                    yield _SqlAlchemyReadTransaction(session)
                    session.commit()
                after_read()

        from app.platform.school_facts.f5_policy import F5FactScope

        scopes = {
            "education_class": F4FactScope(
                school, "education_class", field_names=("display_name",), coverage="school"
            ),
            "teaching_course": F5FactScope(
                school, "teaching_course", field_names=("display_name",), coverage="school"
            ),
        }
        selected_actor = _issue_staff_actor_context(
            account_user_id=1, school_id=SchoolId(school), person_id=PersonId(uuid4()),
            assignment_id=AssignmentId(uuid4()), assignment_version=1,
            authorization_version=1,
        )
        state = SimpleNamespace(allowed=True, pages=0)

        class Authority:
            def require_scope(self, actor, *, fact_kind, action, checked_at):
                assert actor == selected_actor and fact_kind in scopes and action == "read"
                assert state.allowed
                return SimpleNamespace(actor=actor, action=action, scope=scopes[fact_kind])

            def require_fields(self, permit, fields):
                assert state.allowed and set(fields) <= set(permit.scope.field_names)

            def require_object(self, permit, key, *, is_creation):
                assert state.allowed and not is_creation and permit.scope.allows_key(key)

            def revalidate(self, permit, *, actor, checked_at):
                assert state.allowed and permit.actor == actor

        projection = SqlEducationProjection(Executor(), current_sessions=lambda: sessions)
        original_scope_keys = projection.scope_keys

        def counted_keys(**kwargs):
            state.pages += 1
            return original_scope_keys(**kwargs)

        projection.scope_keys = counted_keys
        repository = EducationRepository(
            projection=projection, authority=Authority(), reference_key=b"synthetic-f4-scope-key",
            clock=lambda: datetime.now(UTC),
        )
        try:
            result = repository.read_records(
                selected_actor, "education_class", fields=("display_name",)
            )
            assert state.pages == 2
            assert result.school_fact_version == projection.current_version(school)
            result.proof.revalidate(actor=selected_actor)
            teaching = repository.read_records(
                selected_actor, "teaching_course", fields=("display_name",)
            )
            assert state.pages == 3
            assert teaching.school_fact_version == result.school_fact_version
            teaching.proof.revalidate(actor=selected_actor)
            state.allowed = False
            with pytest.raises(AssertionError):
                result.proof.revalidate(actor=selected_actor)
            with pytest.raises(AssertionError):
                teaching.proof.revalidate(actor=selected_actor)
        finally:
            transaction.rollback()
            connection.close()
            engine.dispose()

    initialization.test_formal_initialization_closes_and_preserves_upgrade(
        identity_template, monkeypatch, tmp_path, on_upgraded=exercise
    )


def _seed_student_impact(connection, school, original):
    """Synthetic rows stay uncommitted and are discarded with the metadata fixture."""
    source, person, student, room, bed = (uuid4() for _ in range(5))
    connection.execute("SET LOCAL search_path=public,pg_catalog")
    _insert_source(connection, school_id=school, source_id=source, source_key="synthetic-impact")
    _insert_person_and_student(
        connection, school_id=school, person_id=person, student_id=student, key="synthetic-impact"
    )
    _insert_room(
        connection,
        school_id=school,
        source_id=source,
        room_id=room,
        key="synthetic-impact",
        capacity=1,
    )
    _insert_bed(
        connection,
        school_id=school,
        source_id=source,
        room_id=room,
        bed_id=bed,
        key="synthetic-impact",
    )
    residence = _insert_residence(
        connection,
        school_id=school,
        source_id=source,
        student_id=student,
        bed_id=bed,
        valid_from=datetime.now(UTC) - timedelta(days=1),
    )
    campus = connection.execute(
        "SELECT id FROM public.campus_locations WHERE school_id=%s AND kind='campus' LIMIT 1",
        (school,),
    ).fetchone()[0]
    connection.execute(
        "INSERT INTO public.student_school_relationships "
        "(id,school_id,student_id,source_key,student_number,enrollment_status,residence_mode,"
        "primary_campus_location_id,administrative_campus_location_id,"
        "valid_from,version,source_id) "
        "VALUES(%s,%s,%s,'synthetic:relationship','SYNTHETIC-RELATIONSHIP','enrolled',"
        "'boarding',%s,%s,%s,1,%s)",
        (uuid4(), school, student, campus, campus, datetime.now(UTC), source),
    )
    unit = uuid4()
    connection.execute(
        "INSERT INTO public.master_data_import_units SELECT (jsonb_populate_record("
        "NULL::public.master_data_import_units,to_jsonb(u)||jsonb_build_object("
        "'id',%s::text,'sequence',2000))).* FROM public.master_data_import_units u WHERE id=%s",
        (str(unit), original[1]),
    )
    connection.execute(
        "INSERT INTO public.school_fact_change_lineage SELECT (jsonb_populate_record("
        "NULL::public.school_fact_change_lineage,to_jsonb(l)||jsonb_build_object("
        "'id',%s::text,'unit_id',%s::text,'target_id',%s::text,"
        "'fact_kind','student_bed_residence','fact_source_key','synthetic:impact'))).* "
        "FROM public.school_fact_change_lineage l WHERE id=%s",
        (str(uuid4()), str(unit), str(residence), original[0]),
    )
    return source


def _seed_quality_scopes(connection, school, original, source):
    assignment = connection.execute("SELECT id FROM public.staff_assignments LIMIT 1").fetchone()[0]
    batch = connection.execute(
        "SELECT batch_id FROM public.school_fact_change_lineage WHERE id=%s", (original[0],)
    ).fetchone()[0]
    at = datetime.now(UTC) - timedelta(minutes=2)
    duty = uuid4()
    connection.execute(
        "INSERT INTO public.governance_duties(id,school_id,duty_code) "
        "VALUES(%s,%s,'f2_fact_quality_maintainer')",
        (duty, school),
    )
    connection.execute(
        "INSERT INTO public.governance_duty_versions(id,school_id,duty_id,display_name,"
        "is_active,effective_from,version) VALUES(%s,%s,%s,'synthetic',true,%s,1)",
        (uuid4(), school, duty, at),
    )
    scopes, grants = {}, {}
    for label in ("school", "exact", "other", "expired"):
        scope = (
            F2FactScope(school, "fact_quality", (), coverage="school")
            if label in {"school", "expired"}
            else F2FactScope(
                school, "fact_quality", ("synthetic:quality" if label == "exact" else "other",)
            )
        )
        scopes[label], grants[label] = uuid4(), uuid4()
        connection.execute(
            "INSERT INTO public.governance_scopes(id,school_id,scope_key) VALUES(%s,%s,%s)",
            (scopes[label], school, "synthetic:" + label),
        )
        connection.execute(
            "INSERT INTO public.governance_scope_versions(id,school_id,scope_id,schema_owner,"
            "schema_name,schema_version,reference_document,reference_sha256,is_active,"
            "effective_from,version) "
            "VALUES(%s,%s,%s,'school_facts.f2','exact_fact_set',1,%s,%s,true,%s,1)",
            (uuid4(), school, scopes[label], Jsonb(scope.document), scope.document_sha256, at),
        )
        connection.execute(
            "INSERT INTO public.governance_grants(id,school_id,assignment_id,duty_id) "
            "VALUES(%s,%s,%s,%s)",
            (grants[label], school, assignment, duty),
        )
        connection.execute(
            "INSERT INTO public.governance_grant_versions(id,school_id,grant_id,duty_id,"
            "duty_version,scope_id,scope_version,basis_authority,basis_reference,valid_from,"
            "effective_from,planned_valid_to,transition_kind,version) "
            "VALUES(%s,%s,%s,%s,1,%s,1,'synthetic','synthetic',%s,%s,%s,'create',1)",
            (
                uuid4(),
                school,
                grants[label],
                duty,
                scopes[label],
                at,
                at,
                at + timedelta(minutes=1) if label == "expired" else at + timedelta(hours=1),
            ),
        )
    connection.execute(
        "INSERT INTO public.school_fact_qualities(id,school_id,source_key,quality_kind,"
        "target_fact_kind,owner_kind,basis_reference,responsibility,verified_at,source_status,"
        "valid_from,version,source_id,import_batch_id) "
        "VALUES(%s,%s,'synthetic:quality','source','official_contact','school',"
        "'synthetic','synthetic',%s,'unavailable',%s,1,%s,%s)",
        (uuid4(), school, at, at, source, batch),
    )
    return assignment, scopes, grants
