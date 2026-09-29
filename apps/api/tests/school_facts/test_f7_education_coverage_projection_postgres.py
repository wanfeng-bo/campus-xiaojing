"""Owned F7 PostgreSQL validates retained F4/F5 quality projection contracts."""

import json
import os
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session
from sqlalchemy.exc import DBAPIError

from app.composition.school_fact_persistence import SqlAlchemySchoolFactPersistence
from app.platform.school_facts.application import SchoolFactChangeRejected
from app.platform.school_facts.contracts import (
    CanonicalFactChangeSetV3, CanonicalFactChangeSetV4, CanonicalFactChangeV1,
)
from app.platform.school_facts.f7_coverage_basis import mother_set_digest
from tests.school_facts import test_f3_family_postgres, test_f3_writer_postgres
from tests.school_facts.test_f3_family_postgres import existing_fixture_writer, safe_test_engine
from tests.school_facts.test_f4_writer_postgres import apply as formal_apply_v3


def _grant_f4_reader(scenario, actor, kind, keys):
    """Publish one exact test-only grant, never an original-instance permission."""
    from app.composition.school_fact_education_settings import EducationScopeBinding
    from app.domains.students.education_contracts import EDUCATION_CONTENT_FIELDS
    from app.platform.school_facts.f4_policy import F4_SCOPE_OWNER, F4_SCOPE_SCHEMA, F4FactScope

    scope = F4FactScope(
        scenario.school, kind, object_keys=tuple(keys),
        field_names=tuple(sorted(EDUCATION_CONTENT_FIELDS[kind])),
    )
    at = datetime.now(UTC) - timedelta(seconds=1)
    sid, gid = uuid4(), uuid4()
    duty_code = f"f4_{kind}_reader"
    with scenario.owner.begin() as session:
        duty_id = session.scalar(text(
            "SELECT id FROM governance_duties WHERE school_id=:school AND duty_code=:code"
        ), {"school": scenario.school, "code": duty_code})
        if duty_id is None:
            duty_id = uuid4()
            scenario._add(
                session, "governance_duties", id=duty_id, school_id=scenario.school,
                duty_code=duty_code, current_version=1,
            )
            scenario._add(
                session, "governance_duty_versions", school_id=scenario.school,
                duty_id=duty_id, display_name="F7隔离年级组读取职责",
                description="TEST FIXTURE ONLY", is_active=True,
                effective_from=at, version=1,
            )
        scenario._add(
            session, "governance_scopes", id=sid, school_id=scenario.school,
            scope_key="f7-isolated-grade-set:" + sid.hex, current_version=1,
        )
        scenario._add(
            session, "governance_scope_versions", school_id=scenario.school,
            scope_id=sid, schema_owner=F4_SCOPE_OWNER, schema_name=F4_SCOPE_SCHEMA,
            schema_version=1, reference_document=scope.document,
            reference_sha256=scope.document_sha256, is_active=True,
            effective_from=at, version=1,
        )
        scenario._add(
            session, "governance_grants", id=gid, school_id=scenario.school,
            assignment_id=actor.assignment_id, duty_id=duty_id, current_version=1,
        )
        scenario._add(
            session, "governance_grant_versions", school_id=scenario.school,
            grant_id=gid, duty_id=duty_id, duty_version=1,
            scope_id=sid, scope_version=1, basis_authority="isolated_test_fixture",
            basis_reference="f7-grade-set-owned", valid_from=at,
            effective_from=at, planned_valid_to=at + timedelta(hours=1),
            transition_kind="create", version=1,
        )
        session.execute(text(
            "INSERT INTO governance_assignment_epochs(school_id,assignment_id,epoch) "
            "VALUES(:school,:assignment,1) ON CONFLICT(school_id,assignment_id) "
            "DO UPDATE SET epoch=governance_assignment_epochs.epoch+1"
        ), {"school": scenario.school, "assignment": actor.assignment_id})
    return EducationScopeBinding(
        assignment_id=actor.assignment_id,
        operation_code=f"school_facts.f4.{kind}.read", scope_id=sid,
    ), gid

pytestmark = pytest.mark.skipif(
    not os.environ.get("F7_TEST_DATABASE_CONFIG"), reason="owned F7 PG config required"
)


def test_f7_grade_group_complete_writer_uses_whole_real_campus_year_set():
    """Owned test DB only; every inserted row rolls back with this transaction."""
    config = Path(os.environ["F7_TEST_DATABASE_CONFIG"])
    engine = safe_test_engine(config, "migration")
    try:
        with engine.connect() as connection:
            transaction = connection.begin()
            try:
                school = connection.scalar(text("SELECT school_id FROM school_database_anchor"))
                assert school is not None
                source_id = connection.scalar(text(
                    "SELECT id FROM school_fact_sources WHERE school_id=:school LIMIT 1"
                ), {"school": school})
                batch_id = connection.scalar(text(
                    "SELECT id FROM master_data_import_batches WHERE school_id=:school "
                    "AND status='applied' LIMIT 1"
                ), {"school": school})
                assert source_id is not None and batch_id is not None
                at = datetime.now(UTC) - timedelta(days=1)
                token = uuid4().hex
                campus = f"campus:f7-set-{token}"
                keys = (f"group:f7-set-a-{token}", f"group:f7-set-b-{token}")

                def insert_group(session, key, *, year=2026, campus_key=campus, start=at):
                    fact_id = uuid4()
                    session.execute(text(
                        "INSERT INTO school_education_facts "
                        "(id,school_id,kind,source_key,version) "
                        "VALUES (:id,:school,'education_grade_group',:key,1)"
                    ), {"id": fact_id, "school": school, "key": key})
                    session.execute(text(
                        "INSERT INTO school_education_versions "
                        "(id,school_id,fact_id,revision,valid_from,payload,source_id,import_batch_id) "
                        "VALUES (:id,:school,:fact_id,1,:start,CAST(:payload AS jsonb),:source,:batch)"
                    ), {
                        "id": uuid4(), "school": school, "fact_id": fact_id,
                        "start": start,
                        "payload": json.dumps({
                            "calendar_source_key": f"calendar:f7-set-{token}",
                            "academic_year": year, "grade_source_key": f"grade:{key}",
                            "campus_source_key": campus_key,
                        }),
                        "source": source_id, "batch": batch_id,
                    })

                with Session(bind=connection, join_transaction_mode="create_savepoint") as session:
                    for key in keys:
                        insert_group(session, key)
                    version = SqlAlchemySchoolFactPersistence._current_global_fact_version(
                        session, school
                    )
                    members = tuple((key, 1) for key in keys)
                    count, digest = mother_set_digest(members)
                    claim = CanonicalFactChangeV1.model_validate({
                        "schema_version": 4, "fact_kind": "fact_quality",
                        "operation": "upsert", "source_key": f"quality:f7-set-{token}",
                        "expected_version": 0, "effective_at": at,
                        "origin": {"sheet_code": "quality", "row_number": 1},
                        "content": {
                            "quality_kind": "coverage", "coverage_status": "complete",
                            "owner_kind": "education",
                            "education_target_kind": "education_calendar_rule",
                            "education_target_source_key": f"calendar:f7-set-{token}",
                            "target_fact_kind": "education_grade_group",
                            "target_source_key": None, "field_name": "grade_source_key",
                            "campus_source_key": campus, "academic_year": 2026,
                            "responsibility": "隔离核查员", "basis_reference": "核查",
                            "verified_at": at,
                            "coverage_basis": {
                                "schema_version": 2, "school_id": school,
                                "owner_kind": "education",
                                "owner_source_key": f"calendar:f7-set-{token}",
                                "target_fact_kind": "education_grade_group",
                                "target_source_key": None, "field_name": "grade_source_key",
                                "purpose": None, "contact_kind": None,
                                "campus_source_key": campus, "academic_year": 2026,
                                "authorized_source_keys": keys,
                                "valid_from": at, "valid_to": None,
                                "school_fact_version": version,
                                "member_count": count, "member_sha256": digest,
                            },
                        },
                    })
                    SqlAlchemySchoolFactPersistence._verify_complete_coverage_basis(
                        session, school, claim, {}
                    )
                    with session.begin_nested() as nested:
                        insert_group(session, f"group:f7-set-extra-{token}")
                        with pytest.raises(SchoolFactChangeRejected, match="母集已变化"):
                            SqlAlchemySchoolFactPersistence._verify_complete_coverage_basis(
                                session, school, claim, {}
                            )
                        nested.rollback()
                    with session.begin_nested() as nested:
                        insert_group(
                            session, f"group:f7-set-future-{token}",
                            start=at + timedelta(days=2),
                        )
                        with pytest.raises(SchoolFactChangeRejected, match="母集已变化"):
                            SqlAlchemySchoolFactPersistence._verify_complete_coverage_basis(
                                session, school, claim, {}
                            )
                        nested.rollback()
            finally:
                transaction.rollback()
    finally:
        engine.dispose()


def test_f7_grade_group_year_filter_is_database_narrow():
    config = Path(os.environ["F7_TEST_DATABASE_CONFIG"])
    reader = safe_test_engine(config, "reader")
    migration = safe_test_engine(config, "migration")
    try:
        with migration.connect() as connection:
            school = connection.scalar(text("SELECT school_id FROM school_database_anchor"))
        with reader.connect() as connection:
            statement = text(
                "SELECT app.school_fact_f4_targets(:school,:kind,ARRAY[]::text[],"
                "CAST(:fields AS text[]),now(),NULL,CAST(:filter AS jsonb))"
            )
            assert connection.scalar(statement, {
                "school": school, "kind": "education_grade_group",
                "fields": ["academic_year"],
                "filter": json.dumps({"academic_year": 2026}),
            })["records"] == []
            for kind, value in (
                ("education_grade_group", "2026"),
                ("education_grade_group", 2026.5),
                ("education_term", 2026),
            ):
                with pytest.raises(DBAPIError) as denied:
                    connection.execute(statement, {
                        "school": school, "kind": kind,
                        "fields": ["academic_year"],
                        "filter": json.dumps({"academic_year": value}),
                    })
                assert denied.value.orig.sqlstate == "22023"
                connection.rollback()
    finally:
        reader.dispose()
        migration.dispose()


def test_f7_formal_grade_group_complete_reader_invalidates_on_new_member_and_revoke():
    """Formal Writer publication, exact Reader grants and conservative release."""
    from sqlalchemy.orm import sessionmaker

    from app.composition.school_fact_education_authority import (
        EducationAuthorityDenied, create_education_authority,
    )
    from app.composition.school_fact_education_product import EducationProductAdapter
    from app.composition.school_fact_education_external import EducationExternalReader
    from app.composition.school_fact_education_quality import (
        EducationQualityBridge, SqlEducationDependencyProjection,
    )
    from app.composition.school_fact_education_reader import (
        EducationRepository, SqlEducationProjection,
    )
    from app.composition.school_fact_education_settings import EducationConfiguration
    from app.composition.school_fact_education_views import EducationMaintenanceViews
    from app.composition.school_fact_product_services import ReadOnlyGovernanceDependencies
    from app.composition.school_fact_read_connection import F2ConnectionReadExecutor
    from app.platform.governance.application import GovernanceWriter
    from app.platform.governance.contracts import GovernanceActorContext, RevokeGrantCommand
    from tests.governance.test_writer_lifecycle import _RecordingAuthorizer
    from tests.school_facts.test_f3_writer_postgres import WriterScenario
    from tests.school_facts.test_f7_family_quality_postgres import (
        _directory, _grant_test_quality,
    )

    config_path = Path(os.environ["F7_TEST_DATABASE_CONFIG"])
    config = json.loads(config_path.read_text(encoding="utf-8"))
    engines = {
        role: safe_test_engine(config_path, role)
        for role in ("migration", "runtime", "reader", "writer")
    }
    try:
        with engines["migration"].connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
                "20260924_55"
            )
            school = connection.scalar(text("SELECT school_id FROM school_database_anchor"))
        assert school is not None
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(test_f3_family_postgres, "KEY", config["context"].encode())
            patch.setattr(test_f3_writer_postgres, "KEY", config["context"].encode())
            scenario = WriterScenario(engines, school)
            scenario.complete_seed_staff_context()
            identity = scenario.identity()
            actor = identity.staff()
            writer = (engines, *existing_fixture_writer(engines))
            token = uuid4().hex
            campus = f"campus:f7-grade-set-{token}"
            stage, grades = f"stage:f7-{token}", tuple(
                f"grade:f7-{n}-{token}" for n in range(3)
            )
            calendar, groups = f"calendar:f7-{token}", tuple(
                f"group:f7-{n}-{token}" for n in range(3)
            )
            at = datetime.now(UTC) - timedelta(seconds=3)
            year = at.year

            def change(kind, key, content):
                return {
                    "fact_kind": kind, "source_key": key, "operation": "upsert",
                    "expected_version": 0, "effective_at": at, "content": content,
                }

            formal_apply_v3(writer, [change("location", campus, {
                "kind": "campus", "display_name": "隔离校区",
                "display_path": "隔离校区", "status": "active",
            })])
            formal_apply_v3(writer, [
                change("education_stage", stage, {"display_name": "隔离学段"}),
                *(change("education_grade", grade, {
                    "display_name": f"隔离年级{n}", "stage_source_key": stage,
                    "grade_order": n + 1,
                }) for n, grade in enumerate(grades)),
            ])
            formal_apply_v3(writer, [change("education_calendar_rule", calendar, {
                "display_name": "隔离学年规则", "stage_source_key": stage,
                "start_cycle": year, "month": 1, "day": 1,
                "timezone_name": "Asia/Shanghai", "grade_source_keys": grades,
                "campus_source_key": campus,
            })])
            formal_apply_v3(writer, [
                change("education_grade_group", group, {
                    "calendar_source_key": calendar, "academic_year": year,
                    "grade_source_key": grades[n], "campus_source_key": campus,
                }) for n, group in enumerate(groups[:2])
            ])
            quality_key = f"quality:f7-grade-set-{token}"
            q_read = _grant_test_quality(
                scenario, actor, quality_key, purpose="read", creation=False
            )
            campus_read = _grant_test_quality(
                scenario, actor, campus, purpose="read", creation=False,
                fact_kind="location",
            )
            f4_bindings = []
            grant_ids = {}
            for kind, keys in (
                ("education_calendar_rule", (calendar,)),
                ("education_grade", grades),
                ("education_grade_group", groups[:2]),
            ):
                binding, grant_id = _grant_f4_reader(scenario, actor, kind, keys)
                f4_bindings.append(binding)
                grant_ids[kind] = grant_id
            directory = _directory(scenario, config, (q_read, campus_read))
            authority = create_education_authority(
                sessions=scenario.sessions["reader"],
                configuration=EducationConfiguration(
                    approval_reference="F7 isolated grade-set fixture",
                    scope_bindings=tuple(f4_bindings),
                ),
            )
            projection = SqlEducationProjection(
                F2ConnectionReadExecutor(
                    scenario.sessions["reader"], authority.connection_authority,
                ),
                current_sessions=authority.connection_authority.current_sessions,
            )
            repository = EducationRepository(
                authority=authority, reference_key=config["context"].encode(),
                projection=projection,
            )
            quality = EducationQualityBridge(
                f2_guard=directory.guard, education_authority=authority,
                projection=SqlEducationDependencyProjection(projection),
            )
            external = EducationExternalReader(
                directory=directory.query, evidence=directory.evidence,
                quality_bridge=quality,
            )
            view = EducationMaintenanceViews(
                EducationProductAdapter(repository=repository, external=external),
                directory=None, family=None,
            )
            actor = identity.staff()
            with engines["reader"].connect() as connection:
                before = connection.scalar(text(
                    "SELECT app.school_fact_current_version(:school)"
                ), {"school": school})
            basis_count, basis_digest = mother_set_digest(((key, 1) for key in groups[:2]))
            basis = {
                "schema_version": 2, "school_id": school,
                "owner_kind": "education", "owner_source_key": calendar,
                "target_fact_kind": "education_grade_group", "target_source_key": None,
                "field_name": "grade_source_key", "purpose": None, "contact_kind": None,
                "campus_source_key": campus, "academic_year": year,
                "authorized_source_keys": groups[:2],
                "valid_from": at, "valid_to": None,
                "school_fact_version": before,
                "member_count": basis_count, "member_sha256": basis_digest,
            }
            quality_plan = CanonicalFactChangeSetV4.model_validate({
                "schema_version": 4, "base_version": before,
                "source_sha256": sha256(uuid4().bytes).hexdigest(),
                "changes": ({
                    "schema_version": 4, "fact_kind": "fact_quality",
                    "source_key": quality_key, "operation": "upsert",
                    "expected_version": 0, "effective_at": at,
                    "origin": {"sheet_code": "f7-grade-set", "row_number": 1},
                    "content": {
                        "quality_kind": "coverage", "coverage_status": "complete",
                        "owner_kind": "education",
                        "education_target_kind": "education_calendar_rule",
                        "education_target_source_key": calendar,
                        "target_fact_kind": "education_grade_group",
                        "target_source_key": None, "field_name": "grade_source_key",
                        "campus_source_key": campus, "academic_year": year,
                        "responsibility": "隔离核查员",
                        "basis_reference": "隔离正式核查记录", "verified_at": at,
                        "coverage_basis": basis,
                    },
                },),
            })
            service = writer[4]
            context = writer[3]
            report = service.preflight(
                school_id=school, change_set=quality_plan, context=context,
                session_binding=writer[2].session_nonce_hash,
            )
            assert report.is_clean
            prepared = service.prepare_confirmation(
                change_set=quality_plan, context=context,
                session_binding=writer[2].session_nonce_hash,
            )
            assert service.confirm(
                credential=prepared.credential, change_set=quality_plan,
                context=context, session_binding=writer[2].session_nonce_hash,
            ).changed_count == 1
            for kind in ("fact_quality", "location"):
                permit = directory.guard.require_current(actor, kind, purpose="read")
                try:
                    directory.guard.revalidate(permit, actor)
                except PermissionError:
                    pytest.fail(f"Fresh isolated {kind} read permit did not revalidate")
            campus_ref = directory.query.reference_for(
                actor=actor, fact_kind="location", source_key=campus,
            )
            request = {
                "actor": actor, "fact_kind": "education_grade_group",
                "campus_ref": campus_ref, "academic_year": year, "limit": 20,
            }
            complete = view.query_with_proof(**request)
            assert complete.public_result["coverage"]["status"] == "complete"
            assert len(complete.public_result["records"]) == 2
            complete.private_proof.revalidate(actor=actor)

            # A new third member is not granted to this reader. The old complete
            # assertion must become unknown without disclosing that member.
            formal_apply_v3(writer, [
                change("education_grade_group", groups[2], {
                    "calendar_source_key": calendar, "academic_year": year,
                    "grade_source_key": grades[2], "campus_source_key": campus,
                })
            ])
            changed = view.query_with_proof(**request)
            assert changed.public_result["coverage"]["status"] == "unknown"
            assert len(changed.public_result["records"]) == 2
            assert groups[2] not in str(changed.public_result)

            governance = GovernanceWriter(
                session_factory=scenario.owner, authorizer=_RecordingAuthorizer(),
                clock=lambda: datetime.now(UTC),
                dependency_port=ReadOnlyGovernanceDependencies(
                    sessionmaker(engines["reader"], expire_on_commit=False)
                ),
                scope_owner_resolvers={},
            )

            def revoke(grant_id, suffix):
                return governance.revoke_grant(RevokeGrantCommand(
                    school, grant_id, 1, datetime.now(UTC),
                    GovernanceActorContext(
                        school_id=school, actor_kind="assignment",
                        actor_reference="test-only:f7-grade-set",
                        actor_assignment_id=actor.assignment_id,
                        actor_assignment_version=actor.assignment_version,
                        request_id=uuid4(), reason="F7 isolated revoke check",
                    ),
                    f"f7-grade-set:{token}:{suffix}",
                ))

            with engines["migration"].connect() as connection:
                quality_grant = connection.scalar(text(
                    "SELECT g.id FROM governance_grants g JOIN governance_grant_versions v "
                    "ON v.school_id=g.school_id AND v.grant_id=g.id "
                    "WHERE g.school_id=:school AND v.scope_id=:scope LIMIT 1"
                ), {"school": school, "scope": q_read.scope_id})
            assert quality_grant is not None
            revoke(quality_grant, "quality")
            with pytest.raises(PermissionError):
                complete.private_proof.revalidate(actor=actor)
            actor = identity.staff()
            request["actor"] = actor
            no_quality = view.query_with_proof(**request)
            assert no_quality.public_result["coverage"]["status"] == "unknown"
            revoke(grant_ids["education_grade_group"], "grade")
            actor = identity.staff()
            request["actor"] = actor
            with pytest.raises(EducationAuthorityDenied):
                view.query_with_proof(**request)
    finally:
        for engine in engines.values():
            engine.dispose()


def test_f7_f4_f5_quality_projection_keeps_reader_acl_and_basis_column():
    config = Path(os.environ["F7_TEST_DATABASE_CONFIG"])
    engines = {
        role: safe_test_engine(config, role)
        for role in ("migration", "reader", "runtime")
    }
    try:
        with engines["migration"].connect() as connection:
            revision = connection.scalar(text("SELECT version_num FROM alembic_version"))
            assert revision == "20260924_55"
            school = connection.scalar(text("SELECT school_id FROM school_database_anchor"))
            assert school is not None
            for name in ("school_fact_f4_dependencies", "school_fact_f5_dependencies"):
                definition = connection.scalar(text("""
                    SELECT pg_get_functiondef(p.oid) FROM pg_proc p
                    JOIN pg_namespace n ON n.oid=p.pronamespace
                    WHERE n.nspname='app' AND p.proname=:name
                """), {"name": name})
                assert definition is not None and "q.coverage_basis" in definition

        f4 = text("""SELECT app.school_fact_f4_dependencies(
            :school,'fact_quality',ARRAY[]::text[],
            'education_class',CAST(:owner AS text[]),'class_teacher_assignment',
            ARRAY[]::text[],ARRAY['teacher_role'],now(),NULL,
            ARRAY[]::text[],ARRAY[]::text[],ARRAY[]::text[])""")
        f5 = text("""SELECT app.school_fact_f5_dependencies(
            :school,'fact_quality',ARRAY[]::text[],
            'teaching_offering',CAST(:owner AS text[]),'teaching_assignment',
            ARRAY[]::text[],ARRAY['teaching_role'],now(),NULL,
            ARRAY[]::text[],ARRAY[]::text[],ARRAY[]::text[],ARRAY[]::text[])""")
        with engines["reader"].connect() as connection:
            for statement, kind in ((f4, "education_class"), (f5, "teaching_offering")):
                with pytest.raises(DBAPIError) as denied:
                    connection.execute(statement, {"school": school, "owner": ["unpublished"]})
                assert denied.value.orig.sqlstate == "55000", kind
                connection.rollback()
        with engines["runtime"].connect() as connection:
            for statement, kind in ((f4, "education_class"), (f5, "teaching_offering")):
                with pytest.raises(DBAPIError) as denied:
                    connection.execute(
                        statement, {"school": school, "owner": ["unpublished"]}
                    )
                assert denied.value.orig.sqlstate in {"42501", "28000"}, kind
                connection.rollback()
    finally:
        for engine in engines.values():
            engine.dispose()


def test_f7_f4_f5_exact_complete_formal_writer_then_reader_projection(monkeypatch):
    config = Path(os.environ["F7_TEST_DATABASE_CONFIG"])
    config_data = json.loads(config.read_text(encoding="utf-8"))
    engines = {
        role: safe_test_engine(config, role)
        for role in ("migration", "reader", "runtime", "writer")
    }
    try:
        with engines["migration"].connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
                "20260924_55"
            )
            school = connection.scalar(text("SELECT school_id FROM school_database_anchor"))
            assignment = connection.scalar(text("""
                SELECT source_key FROM staff_assignments
                WHERE school_id=:school AND source_key LIKE 'assignment:f3-writer-safety-%'
                ORDER BY source_key LIMIT 1
            """), {"school": school})
            assert assignment is not None
        monkeypatch.setattr(
            test_f3_family_postgres, "KEY", config_data["context"].encode()
        )
        monkeypatch.setattr(
            test_f3_writer_postgres, "KEY", config_data["context"].encode()
        )
        actor, identity, context, service = existing_fixture_writer(engines)
        writer = engines, actor, identity, context, service
        now = datetime.now(UTC)
        token = uuid4().hex
        class_key, teacher_key = f"class:f7-coverage-{token}", f"teacher:f7-coverage-{token}"
        formal_apply_v3(writer, [
            {"fact_kind": "education_class", "source_key": class_key,
             "operation": "upsert", "expected_version": 0, "effective_at": now,
             "content": {"display_name": "隔离测试班",
                         "teacher_mode": "single_primary",
                         "teacher_role_limits": {"primary": 1}}},
        ])
        assignment_entry = {"fact_kind": "class_teacher_assignment", "source_key": teacher_key,
             "operation": "upsert", "expected_version": 0, "effective_at": now,
             "content": {"class_source_key": class_key,
                         "assignment_source_key": assignment,
                         "teacher_role": "primary"}}
        with engines["reader"].connect() as connection:
            assignment_base = connection.scalar(
                text("SELECT app.school_fact_current_version(:school)"), {"school": school}
            )
        assignment_plan = CanonicalFactChangeSetV3.model_validate_json(json.dumps({
            "schema_version": 3, "base_version": assignment_base,
            "source_sha256": sha256(uuid4().bytes).hexdigest(),
            "changes": [{"schema_version": 3,
                         "origin": {"sheet_code": "education", "row_number": 1},
                         **assignment_entry}],
        }, default=str))
        report = service.preflight(
            school_id=school, change_set=assignment_plan, context=context,
            session_binding=identity.session_nonce_hash,
        )
        assert report.is_clean, [(i.error_code, i.column_code) for i in report.issues]
        formal_apply_v3(writer, [assignment_entry])
        with engines["reader"].connect() as connection:
            base = connection.scalar(
                text("SELECT app.school_fact_current_version(:school)"), {"school": school}
            )
        quality_key = f"quality:f7-coverage-{token}"
        basis = {
            "schema_version": 1, "school_id": str(school),
            "owner_kind": "education", "owner_source_key": class_key,
            "target_fact_kind": "class_teacher_assignment",
            "target_source_key": teacher_key, "field_name": "teacher_role",
            "purpose": None, "contact_kind": None,
            "valid_from": now.isoformat(), "valid_to": None,
            "school_fact_version": base, "member_count": 1,
            "member_sha256": mother_set_digest(((teacher_key, 1),))[1],
        }
        change = CanonicalFactChangeSetV4.model_validate_json(json.dumps({
            "schema_version": 4,
            "base_version": base,
            "source_sha256": sha256(uuid4().bytes).hexdigest(),
            "changes": [{
                "schema_version": 4,
                "origin": {"sheet_code": "quality", "row_number": 1},
                "fact_kind": "fact_quality", "source_key": quality_key,
                "operation": "upsert", "expected_version": 0,
                "effective_at": now,
                "content": {
                    "quality_kind": "coverage", "coverage_status": "complete",
                    "owner_kind": "education", "education_target_kind": "education_class",
                    "education_target_source_key": class_key,
                    "target_fact_kind": "class_teacher_assignment",
                    "target_source_key": teacher_key, "field_name": "teacher_role",
                    "responsibility": "隔离工程核查员", "basis_reference": "合成记录核查",
                    "verified_at": now, "coverage_basis": basis,
                },
            }],
        }, default=str))
        prepared = service.prepare_confirmation(
            change_set=change, context=context,
            session_binding=identity.session_nonce_hash,
        )
        receipt = service.confirm(
            credential=prepared.credential, change_set=change, context=context,
            session_binding=identity.session_nonce_hash,
        )
        assert receipt.changed_count == 1
        with engines["reader"].connect() as connection:
            result = connection.scalar(text("""
                SELECT app.school_fact_f4_dependencies(
                    :school,'fact_quality',CAST(:qualities AS text[]),
                    'education_class',CAST(:classes AS text[]),
                    'class_teacher_assignment',CAST(:teachers AS text[]),
                    ARRAY['teacher_role'],:at,NULL,
                    ARRAY[]::text[],ARRAY[]::text[],ARRAY[]::text[])
            """), {"school": school, "qualities": [quality_key],
                   "classes": [class_key], "teachers": [teacher_key], "at": now})
        assert len(result["records"]) == 1
        assert result["records"][0]["content"]["coverage_basis"]["member_sha256"] == (
            basis["member_sha256"]
        )
        assert result["school_fact_version"] == base + 1

        course_key = f"course:f7-coverage-{token}"
        offering_key = f"offering:f7-coverage-{token}"
        assignment_key = f"assignment:f7-coverage-{token}"
        formal_apply_v3(writer, [
            {"fact_kind": "teaching_course", "source_key": course_key,
             "operation": "upsert", "expected_version": 0,
             "effective_at": now, "content": {"display_name": "隔离数学课"}},
            {"fact_kind": "teaching_offering", "source_key": offering_key,
             "operation": "upsert", "expected_version": 0,
             "effective_at": now,
             "content": {"display_name": "隔离任课", "course_source_key": course_key,
                         "offering_status": "active"}},
            {"fact_kind": "offering_member_source", "source_key": f"source:f7-{token}",
             "operation": "upsert", "expected_version": 0,
             "effective_at": now,
             "content": {"offering_source_key": offering_key,
                         "source_kind": "administrative_class",
                         "class_source_key": class_key}},
            {"fact_kind": "teaching_assignment", "source_key": assignment_key,
             "operation": "upsert", "expected_version": 0,
             "effective_at": now,
             "content": {"offering_source_key": offering_key,
                         "assignment_source_key": assignment,
                         "teaching_role": "lead"}},
        ])
        with engines["reader"].connect() as connection:
            teaching_base = connection.scalar(
                text("SELECT app.school_fact_current_version(:school)"), {"school": school}
            )
        teaching_quality_key = f"quality:f7-teaching-{token}"
        teaching_basis = {
            "schema_version": 1, "school_id": str(school),
            "owner_kind": "education", "owner_source_key": offering_key,
            "target_fact_kind": "teaching_assignment",
            "target_source_key": assignment_key, "field_name": "teaching_role",
            "purpose": None, "contact_kind": None,
            "valid_from": now.isoformat(), "valid_to": None,
            "school_fact_version": teaching_base, "member_count": 1,
            "member_sha256": mother_set_digest(((assignment_key, 1),))[1],
        }
        teaching_change = CanonicalFactChangeSetV4.model_validate_json(json.dumps({
            "schema_version": 4, "base_version": teaching_base,
            "source_sha256": sha256(uuid4().bytes).hexdigest(),
            "changes": [{
                "schema_version": 4,
                "origin": {"sheet_code": "quality", "row_number": 1},
                "fact_kind": "fact_quality", "source_key": teaching_quality_key,
                "operation": "upsert", "expected_version": 0,
                "effective_at": now,
                "content": {
                    "quality_kind": "coverage", "coverage_status": "complete",
                    "owner_kind": "education",
                    "education_target_kind": "teaching_offering",
                    "education_target_source_key": offering_key,
                    "target_fact_kind": "teaching_assignment",
                    "target_source_key": assignment_key,
                    "field_name": "teaching_role", "teaching_role": "lead",
                    "responsibility": "隔离工程核查员",
                    "basis_reference": "合成记录核查", "verified_at": now,
                    "coverage_basis": teaching_basis,
                },
            }],
        }, default=str))
        teaching_prepared = service.prepare_confirmation(
            change_set=teaching_change, context=context,
            session_binding=identity.session_nonce_hash,
        )
        teaching_receipt = service.confirm(
            credential=teaching_prepared.credential,
            change_set=teaching_change, context=context,
            session_binding=identity.session_nonce_hash,
        )
        assert teaching_receipt.changed_count == 1
        with engines["reader"].connect() as connection:
            teaching_result = connection.scalar(text("""
                SELECT app.school_fact_f5_dependencies(
                    :school,'fact_quality',CAST(:qualities AS text[]),
                    'teaching_offering',CAST(:offerings AS text[]),
                    'teaching_assignment',CAST(:assignments AS text[]),
                    ARRAY['teaching_role'],:at,NULL,
                    ARRAY[]::text[],ARRAY[]::text[],ARRAY[]::text[],ARRAY['lead'])
            """), {"school": school, "qualities": [teaching_quality_key],
                   "offerings": [offering_key], "assignments": [assignment_key], "at": now})
        assert len(teaching_result["records"]) == 1
        assert teaching_result["records"][0]["content"]["coverage_basis"]["member_sha256"] == (
            teaching_basis["member_sha256"]
        )
        assert teaching_result["school_fact_version"] == teaching_base + 1
    finally:
        for engine in engines.values():
            engine.dispose()
