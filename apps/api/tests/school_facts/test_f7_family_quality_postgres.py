"""Owned F7 PostgreSQL verifies the narrow family-quality Reader and storage ACL.

This test does not manufacture a published batch. Positive publication needs
the unchanged formal confirmation/Writer path and is validated separately.
"""

import json
import os
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import sessionmaker

from app.composition.school_fact_directory_adapter import DirectoryProductAdapter
from app.composition.school_fact_directory_product import FactQueryInput
from app.composition.school_fact_directory_services import create_directory_read_components
from app.composition.school_fact_directory_settings import (
    DirectoryConfiguration,
    DirectoryScopeBinding,
)
from app.composition.school_fact_family_authority import create_family_authority_components
from app.composition.school_fact_family_quality import (
    FamilyQualityAuthorizer,
    FamilyQualityChangeGuard,
    FamilyQualityCurrentContent,
    FamilyQualityExistingContent,
    FamilyQualityFinalGuard,
    FamilyQualityReader,
    SqlFamilyQualityProjection,
)
from app.composition.school_fact_family_settings import FamilyConfiguration
from app.composition.school_fact_persistence import SqlAlchemySchoolFactPersistence
from app.composition.school_fact_read_connection import F2ConnectionReadExecutor
from app.composition.school_fact_reader import SchoolFactReaderSettings
from app.composition.school_fact_residence_reader import SqlResidenceProjection
from app.composition.school_facts import HmacSchoolFactConfirmationCredentialIssuer
from app.platform.identity.execution_context import compose_command_context
from app.platform.school_facts.application import SchoolFactChangeService
from app.platform.school_facts.contracts import CanonicalFactChangeSetV4
from app.platform.school_facts.f2_policy import F2FactScope
from app.platform.school_facts.f7_coverage_basis import (
    mother_set_digest,
    verified_exact_student_basis,
    verify_coverage_basis,
)
from tests.school_facts import test_f3_family_postgres, test_f3_writer_postgres
from tests.school_facts.test_f3_family_postgres import safe_test_engine
from tests.school_facts.test_f3_writer_postgres import WriterScenario
from tests.school_facts.test_sf2_transaction import seed_staff

pytestmark = pytest.mark.skipif(
    not os.environ.get("F7_TEST_DATABASE_CONFIG"), reason="owned F7 PG config required"
)

F3_TARGETS = (
    "student", "student_school_relationship", "login_mapping",
    "student_contact", "guardian_relationship", "person_region",
)


def test_f7_formal_f3_quality_target_prerequisite():
    config_path = Path(os.environ["F7_TEST_DATABASE_CONFIG"])
    config = json.loads(config_path.read_text(encoding="utf-8"))
    engines = {
        role: safe_test_engine(config_path, role)
        for role in ("migration", "runtime", "reader", "writer")
    }
    try:
        with _connection(config, "migration") as connection:
            connection.execute("SET ROLE campus_ai_db_owner")
            school = connection.execute(
                "SELECT school_id FROM school_database_anchor"
            ).fetchone()[0]
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(test_f3_family_postgres, "KEY", config["context"].encode())
            patch.setattr(test_f3_writer_postgres, "KEY", config["context"].encode())
            scenario = WriterScenario(engines, school)
            scenario.complete_seed_staff_context()
        with _connection(config, "migration") as connection:
            connection.execute("SET ROLE campus_ai_db_owner")
            assert connection.execute(
                "SELECT count(*) FROM students WHERE school_id=%s", (school,)
            ).fetchone()[0] >= 1
    finally:
        for engine in engines.values():
            engine.dispose()


def _grant_test_quality(
    scenario, actor, quality_key, *, purpose, creation, fact_kind="fact_quality",
    grant_seconds=3600,
):
    """Isolated Governance fixture, never evidence of an original-school grant."""
    scope = F2FactScope(
        scenario.school,
        fact_kind,
        () if creation else (quality_key,),
        (quality_key,) if creation else (),
    )
    scope_id, grant_id = uuid4(), uuid4()
    duty = f"f2_{fact_kind}_{'reader' if purpose == 'read' else 'maintainer'}"
    at = datetime.now(UTC) - timedelta(seconds=1)
    with scenario.owner.begin() as session:
        duty_id = session.scalar(
            text("SELECT id FROM governance_duties WHERE school_id=:school AND duty_code=:duty"),
            {"school": scenario.school, "duty": duty},
        )
        if duty_id is None:
            duty_id = uuid4()
            scenario._add(
                session, "governance_duties", id=duty_id, school_id=scenario.school,
                duty_code=duty, current_version=1,
            )
            scenario._add(
                session, "governance_duty_versions", school_id=scenario.school,
                duty_id=duty_id, display_name="F7隔离质量验收职责",
                description="TEST FIXTURE ONLY", is_active=True,
                effective_from=at, version=1,
            )
        scenario._add(
            session, "governance_scopes", id=scope_id, school_id=scenario.school,
            scope_key="f7-isolated-quality:" + scope_id.hex, current_version=1,
        )
        scenario._add(
            session, "governance_scope_versions", school_id=scenario.school,
            scope_id=scope_id, schema_owner="school_facts.f2", schema_name="exact_fact_set",
            schema_version=1, reference_document=scope.document,
            reference_sha256=scope.document_sha256, is_active=True,
            effective_from=at, version=1,
        )
        scenario._add(
            session, "governance_grants", id=grant_id, school_id=scenario.school,
            assignment_id=actor.assignment_id, duty_id=duty_id, current_version=1,
        )
        scenario._add(
            session, "governance_grant_versions", school_id=scenario.school,
            grant_id=grant_id, duty_id=duty_id, duty_version=1,
            scope_id=scope_id, scope_version=1, basis_authority="isolated_test_fixture",
            basis_reference="f7-family-quality-owned", valid_from=at,
            effective_from=at, planned_valid_to=at + timedelta(seconds=grant_seconds),
            transition_kind="create", version=1,
        )
        session.execute(
            text(
                "INSERT INTO governance_assignment_epochs(school_id,assignment_id,epoch) "
                "VALUES(:school,:assignment,1) ON CONFLICT(school_id,assignment_id) "
                "DO UPDATE SET epoch=governance_assignment_epochs.epoch+1"
            ),
            {"school": scenario.school, "assignment": actor.assignment_id},
        )
    return DirectoryScopeBinding(
        assignment_id=actor.assignment_id,
        operation_code=f"school_facts.f2.{fact_kind}.{purpose}",
        scope_id=scope_id,
    )


def _directory(scenario, config, bindings, *, source_catalog=None, creation_catalog=None):
    with _connection(config, "migration") as connection:
        connection.execute("SET ROLE campus_ai_db_owner")
        anchor_id = connection.execute(
            "SELECT anchor_id FROM school_database_anchor"
        ).fetchone()[0]
    settings = SchoolFactReaderSettings(
        schema_contract="school-facts-f7-v47",
        database_host=config["host"],
        database_port=config["port"],
        database_name=config["database"],
        database_user="campus_ai_school_fact_reader",
        database_password=config["reader"],
        expected_anchor_id=anchor_id,
        expected_school_id=scenario.school,
        expected_system_identifier=config["system_identifier"],
        cursor_hmac_key=config["context"],
    )
    return create_directory_read_components(
        sessions=scenario.sessions["reader"],
        settings=settings,
        configuration=DirectoryConfiguration(
            approval_reference="F7 isolated fixture only", scope_bindings=tuple(bindings),
            source_catalog=source_catalog or {},
            creation_catalog=creation_catalog or {},
        ),
        legacy_scope_id=uuid4(),
        reference_key=config["context"].encode(),
    )


def test_f7_f2_exact_complete_formal_writer_and_authorized_reader():
    config_path = Path(os.environ["F7_TEST_DATABASE_CONFIG"])
    config = json.loads(config_path.read_text(encoding="utf-8"))
    engines = {
        role: safe_test_engine(config_path, role)
        for role in ("migration", "runtime", "reader", "writer")
    }
    try:
        with _connection(config, "migration") as connection:
            connection.execute("SET ROLE campus_ai_db_owner")
            school = connection.execute(
                "SELECT school_id FROM school_database_anchor"
            ).fetchone()[0]
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(test_f3_family_postgres, "KEY", config["context"].encode())
            patch.setattr(test_f3_writer_postgres, "KEY", config["context"].encode())
            scenario = WriterScenario(engines, school)
            scenario.complete_seed_staff_context()
            identity = scenario.identity()
            actor = identity.staff()
            owner_key = "organization:f3-writer-safety"
            quality_key = "quality:f7:f2-exact-" + uuid4().hex
            maintain = _grant_test_quality(
                scenario, actor, quality_key, purpose="maintain", creation=True
            )
            owner_read = _grant_test_quality(
                scenario, actor, owner_key, purpose="read", creation=False,
                fact_kind="organization",
            )
            owner_maintain = _grant_test_quality(
                scenario, actor, owner_key, purpose="maintain", creation=False,
                fact_kind="organization",
            )
            quality_read = _grant_test_quality(
                scenario, actor, quality_key, purpose="read", creation=False
            )
            directory = _directory(
                scenario, config, (maintain, owner_read, owner_maintain, quality_read)
            )
            context = compose_command_context(actor, uuid4(), quality_key)
            persistence = SqlAlchemySchoolFactPersistence(
                scenario.sessions["writer"], identity.envelope,
                confirmation_credential_issuer=HmacSchoolFactConfirmationCredentialIssuer(
                    config["context"].encode()
                ),
                final_authorization_guard=directory.guard,
            )
            service = SchoolFactChangeService(reader=persistence, writer=persistence)
            current = persistence.for_request(context).current_school_fact_version(school)
            at = datetime.now(UTC)
            owner_ref = directory.query.reference_for(
                actor=actor, fact_kind="organization", source_key=owner_key
            )
            selected = directory.query.query(
                actor=actor, fact_kind="organization", reference=owner_ref,
                factual_at=at,
            )
            assert selected.status == "known" and len(selected.records) == 1
            assert selected.read_version == current
            owner_version = selected.records[0].version
            count, digest = mother_set_digest(((owner_key, owner_version),))
            content = {
                "quality_kind": "coverage", "coverage_status": "complete",
                "target_fact_kind": "organization", "target_source_key": owner_key,
                "owner_kind": "organization", "organization_source_key": owner_key,
                "basis_reference": "F7隔离合成核查依据",
                "responsibility": "隔离测试维护职责", "verified_at": at,
                "coverage_basis": {
                    "schema_version": 1, "school_id": school,
                    "owner_kind": "organization", "owner_source_key": owner_key,
                    "target_fact_kind": "organization", "target_source_key": owner_key,
                    "field_name": None, "purpose": None, "contact_kind": None,
                    "valid_from": at, "valid_to": None, "school_fact_version": current,
                    "member_count": count, "member_sha256": digest,
                },
            }
            changes = CanonicalFactChangeSetV4.model_validate({
                "schema_version": 4,
                "source_sha256": sha256(context.idempotency_key.encode()).hexdigest(),
                "base_version": current,
                "changes": ({
                    "schema_version": 4, "operation": "upsert", "fact_kind": "fact_quality",
                    "source_key": quality_key, "expected_version": 0, "effective_at": at,
                    "origin": {"sheet_code": "f7-f2-exact", "row_number": 1},
                    "content": content,
                },),
            })
            report = service.preflight(
                school_id=school, change_set=changes, context=context,
                session_binding=identity.session.session_nonce_hash,
            )
            assert report.is_clean
            prepared = service.prepare_confirmation(
                change_set=changes, context=context,
                session_binding=identity.session.session_nonce_hash,
            )
            receipt = service.confirm(
                credential=prepared.credential, change_set=changes, context=context,
                session_binding=identity.session.session_nonce_hash,
            )
            assert receipt.changed_count == 1
            # Package routing may inspect only this exact existing quality's
            # target kind, after a fresh object-maintenance grant.
            existing_maintain = _grant_test_quality(
                scenario, actor, quality_key, purpose="maintain", creation=False
            )
            with scenario.sessions["reader"]() as session:
                query = text(
                    "SELECT app.school_fact_f7_package_quality_kind("
                    ":school,:assignment,:scope,:key)"
                )
                assert session.scalar(query, {
                    "school": school, "assignment": actor.assignment_id,
                    "scope": existing_maintain.scope_id, "key": quality_key,
                }) == "organization"
                with pytest.raises(DBAPIError):
                    session.scalar(query, {
                        "school": school, "assignment": actor.assignment_id,
                        "scope": existing_maintain.scope_id, "key": owner_key,
                    })
            expired = _grant_test_quality(
                scenario, actor, quality_key, purpose="maintain", creation=False,
                grant_seconds=0.1,
            )
            for denied in (
                {"school": school, "assignment": uuid4(),
                 "scope": existing_maintain.scope_id, "key": quality_key},
                {"school": school, "assignment": actor.assignment_id,
                 "scope": expired.scope_id, "key": quality_key},
                {"school": uuid4(), "assignment": actor.assignment_id,
                 "scope": existing_maintain.scope_id, "key": quality_key},
            ):
                with scenario.sessions["reader"]() as session:
                    with pytest.raises(DBAPIError):
                        session.scalar(query, denied)
            with scenario.sessions["runtime"]() as session:
                with pytest.raises(DBAPIError):
                    session.scalar(query, {
                        "school": school, "assignment": actor.assignment_id,
                        "scope": existing_maintain.scope_id, "key": quality_key,
                    })
            quality_ref = directory.query.reference_for(
                actor=actor, fact_kind="fact_quality", source_key=quality_key
            )
            formal = directory.query.query(
                actor=actor, fact_kind="fact_quality", reference=quality_ref,
                factual_at=datetime.now(UTC),
            )
            assert formal.status == "known" and len(formal.records) == 1
            proof = formal.records[0].content["coverage_basis"]
            fresh = directory.query.query(
                actor=actor, fact_kind="organization", reference=owner_ref,
                factual_at=datetime.now(UTC),
            )
            assert fresh.status == "known" and len(fresh.records) == 1
            assert proof["member_sha256"] == mother_set_digest(
                ((owner_key, fresh.records[0].version),)
            )[1]
            assert proof["school_fact_version"] + 1 == fresh.read_version
            assert proof["owner_source_key"] == owner_key
            assert proof["target_source_key"] == owner_key
            assert proof.get("field_name") is None
            assert proof.get("purpose") is None
            assert proof.get("contact_kind") is None
            assert proof.get("valid_to") is None
            assert verify_coverage_basis(
                proof, school_id=school, owner_kind="organization",
                owner_source_key=owner_key, target_fact_kind="organization",
                target_source_key=owner_key, field_name=None, purpose=None,
                contact_kind=None, factual_at=fresh.factual_at,
                school_fact_version=fresh.read_version,
                members=((owner_key, fresh.records[0].version),),
            )
            answer = DirectoryProductAdapter(directory.query).query(
                actor=actor,
                payload=FactQueryInput(fact_kind="organization", reference=owner_ref),
            )
            assert answer["quality"]["coverage"] == "complete"
    finally:
        for engine in engines.values():
            engine.dispose()


def test_f7_family_quality_formal_writer_then_exact_reader():
    config_path = Path(os.environ["F7_TEST_DATABASE_CONFIG"])
    config = json.loads(config_path.read_text(encoding="utf-8"))
    engines = {
        role: safe_test_engine(config_path, role)
        for role in ("migration", "runtime", "reader", "writer")
    }
    try:
        with _connection(config, "migration") as connection:
            connection.execute("SET ROLE campus_ai_db_owner")
            school = connection.execute(
                "SELECT school_id FROM school_database_anchor"
            ).fetchone()[0]
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(test_f3_family_postgres, "KEY", config["context"].encode())
            patch.setattr(test_f3_writer_postgres, "KEY", config["context"].encode())
            scenario = WriterScenario(engines, school)
            scenario.complete_seed_staff_context()
            identity = scenario.identity()
            actor = identity.staff()
            quality_key = "quality:f7:student-status-" + uuid4().hex
            target_key = "student:f3-writer-safety-wrong-child"
            scenario.grant(actor, kind="student", action="maintain", objects=(target_key,))
            f2_maintain = _grant_test_quality(
                scenario, actor, quality_key, purpose="maintain", creation=True
            )
            directory = _directory(scenario, config, (f2_maintain,))
            family = create_family_authority_components(
                session_factory=scenario.sessions["reader"],
                configuration=FamilyConfiguration(
                    approval_reference="F7 isolated fixture only",
                    scope_bindings=tuple(scenario.bindings.values()),
                ),
            )
            quality_authorizer = FamilyQualityAuthorizer(directory.guard, family)
            quality_guard = FamilyQualityChangeGuard(
                quality_authorizer, clock=lambda: datetime.now(UTC)
            )
            context = compose_command_context(actor, uuid4(), "f7-quality-formal-create")
            persistence = SqlAlchemySchoolFactPersistence(
                scenario.sessions["writer"],
                identity.envelope,
                confirmation_credential_issuer=HmacSchoolFactConfirmationCredentialIssuer(
                    config["context"].encode()
                ),
                final_authorization_guard=FamilyQualityFinalGuard(quality_guard),
                family_actor_validator=family.actor_validator,
            )
            service = SchoolFactChangeService(
                reader=persistence, writer=persistence,
                family_actor_validator=family.actor_validator,
            )
            version = persistence.for_request(context).current_school_fact_version(school)
            at = datetime.now(UTC)
            changes = CanonicalFactChangeSetV4.model_validate(
                {
                    "schema_version": 4,
                    "source_sha256": sha256(context.idempotency_key.encode()).hexdigest(),
                    "base_version": version,
                    "changes": ({
                        "schema_version": 4,
                        "operation": "upsert",
                        "fact_kind": "fact_quality",
                        "source_key": quality_key,
                        "expected_version": 0,
                        "effective_at": at,
                        "origin": {"sheet_code": "f7-quality-isolated", "row_number": 1},
                        "content": {
                            "quality_kind": "source", "target_fact_kind": "student",
                            "owner_kind": "person",
                            "person_source_key": "person:f3-writer-safety-wrong-child",
                            "target_source_key": target_key,
                            "field_name": "status",
                            "basis_reference": "F7隔离合成正式依据",
                            "responsibility": "隔离测试维护职责",
                            "verified_at": at,
                            "source_status": "available",
                        },
                    },),
                }
            )
            quality_guard.require_change_set(actor, changes).revalidate()
            report = service.preflight(
                school_id=school, change_set=changes, context=context,
                session_binding=identity.session.session_nonce_hash,
            )
            assert report.is_clean
            prepared = service.prepare_confirmation(
                change_set=changes, context=context,
                session_binding=identity.session.session_nonce_hash,
            )
            receipt = service.confirm(
                credential=prepared.credential, change_set=changes,
                context=context, session_binding=identity.session.session_nonce_hash,
            )
            assert receipt.changed_count == 1
            with _connection(config, "migration") as connection:
                connection.execute("SET ROLE campus_ai_db_owner")
                row = connection.execute(
                    "SELECT import_batch_id FROM school_fact_qualities "
                    "WHERE school_id=%s AND source_key=%s", (school, quality_key),
                ).fetchone()
                assert row is not None and row[0] == receipt.batch_id

            scenario.grant(actor, kind="student", action="read", objects=(target_key,))
            f2_read = _grant_test_quality(
                scenario, actor, quality_key, purpose="read", creation=False
            )
            directory = _directory(scenario, config, (f2_maintain, f2_read))
            family = create_family_authority_components(
                session_factory=scenario.sessions["reader"],
                configuration=FamilyConfiguration(
                    approval_reference="F7 isolated fixture only",
                    scope_bindings=tuple(scenario.bindings.values()),
                ),
            )
            projection = SqlResidenceProjection(scenario.sessions["reader"])
            reader = FamilyQualityReader(
                authorizer=FamilyQualityAuthorizer(directory.guard, family),
                executor=F2ConnectionReadExecutor(
                    scenario.sessions["reader"], family.connection_authority
                ),
                current_version=projection.current_version,
                clock=lambda: datetime.now(UTC),
            )
            result = reader.query_quality_for_target(
                actor=actor, target_kind="student", target_key=target_key,
                field_names=("status",), as_of=datetime.now(UTC),
            )
            result.revalidate(actor)
            assert len(result.records) == 1
            assert result.records[0]["source_status"] == "available"
            current_snapshot = FamilyQualityCurrentContent(
                f2_guard=directory.guard,
                authorizer=FamilyQualityAuthorizer(directory.guard, family),
                projection=SqlFamilyQualityProjection(scenario.sessions["reader"]),
                reader=reader,
                clock=lambda: datetime.now(UTC),
            )(actor, quality_key)
            assert current_snapshot["version"] == 1
            assert current_snapshot["content"]["source_status"] == "available"
            assert current_snapshot["valid_from"] is not None
            current_snapshot["proof"].revalidate()
            unrelated_field = reader.query_quality_for_target(
                actor=actor, target_kind="student", target_key=target_key,
                field_names=("person_source_key",), as_of=datetime.now(UTC),
            )
            unrelated_field.revalidate(actor)
            assert unrelated_field.records == ()

            f2_maintain_object = _grant_test_quality(
                scenario, actor, quality_key, purpose="maintain", creation=False
            )
            directory = _directory(scenario, config, (f2_maintain_object, f2_read))
            authorizer = FamilyQualityAuthorizer(directory.guard, family)
            existing = FamilyQualityExistingContent(
                f2_guard=directory.guard,
                authorizer=authorizer,
                projection=SqlFamilyQualityProjection(scenario.sessions["reader"]),
                clock=lambda: datetime.now(UTC),
            )
            guard = FamilyQualityChangeGuard(
                authorizer, clock=lambda: datetime.now(UTC), existing_content=existing
            )

            def revise(operation: str, expected: int, content: dict | None):
                revision_context = compose_command_context(
                    actor, uuid4(), f"f7-quality-{operation}-{expected}"
                )
                formal = SqlAlchemySchoolFactPersistence(
                    scenario.sessions["writer"],
                    identity.envelope,
                    confirmation_credential_issuer=HmacSchoolFactConfirmationCredentialIssuer(
                        config["context"].encode()
                    ),
                    final_authorization_guard=FamilyQualityFinalGuard(guard),
                    family_actor_validator=family.actor_validator,
                )
                service = SchoolFactChangeService(
                    reader=formal, writer=formal,
                    family_actor_validator=family.actor_validator,
                )
                version = formal.for_request(revision_context).current_school_fact_version(school)
                change_set = CanonicalFactChangeSetV4.model_validate({
                    "schema_version": 4,
                    "source_sha256": sha256(revision_context.idempotency_key.encode()).hexdigest(),
                    "base_version": version,
                    "changes": ({
                        "schema_version": 4,
                        "operation": operation,
                        "fact_kind": "fact_quality",
                        "source_key": quality_key,
                        "expected_version": expected,
                        "effective_at": datetime.now(UTC),
                        "origin": {"sheet_code": "f7-quality-isolated", "row_number": expected + 1},
                        "content": content,
                    },),
                })
                guard.require_change_set(actor, change_set).revalidate()
                report = service.preflight(
                    school_id=school, change_set=change_set, context=revision_context,
                    session_binding=identity.session.session_nonce_hash,
                )
                assert report.is_clean
                prepared = service.prepare_confirmation(
                    change_set=change_set, context=revision_context,
                    session_binding=identity.session.session_nonce_hash,
                )
                receipt = service.confirm(
                    credential=prepared.credential,
                    change_set=change_set,
                    context=revision_context,
                    session_binding=identity.session.session_nonce_hash,
                )
                assert receipt.changed_count == 1

            revise("upsert", 1, {
                "quality_kind": "source", "target_fact_kind": "student",
                "owner_kind": "person",
                "person_source_key": "person:f3-writer-safety-wrong-child",
                "target_source_key": target_key, "field_name": "status",
                "basis_reference": "F7隔离合成正式依据（更新）",
                "responsibility": "隔离测试维护职责",
                "verified_at": datetime.now(UTC), "source_status": "unavailable",
            })
            result = reader.query_quality_for_target(
                actor=actor, target_kind="student", target_key=target_key,
                field_names=("status",), as_of=datetime.now(UTC),
            )
            result.revalidate(actor)
            assert len(result.records) == 1
            assert result.records[0]["source_status"] == "unavailable"
            revise("end", 2, None)
            ended = reader.query_quality_for_target(
                actor=actor, target_kind="student", target_key=target_key,
                field_names=("status",), as_of=datetime.now(UTC),
            )
            ended.revalidate(actor)
            assert ended.records == ()
    finally:
        for engine in engines.values():
            engine.dispose()


def test_f7_complete_student_basis_survives_formal_writer_and_exact_reader():
    config_path = Path(os.environ["F7_TEST_DATABASE_CONFIG"])
    config = json.loads(config_path.read_text(encoding="utf-8"))
    engines = {
        role: safe_test_engine(config_path, role)
        for role in ("migration", "runtime", "reader", "writer")
    }
    try:
        with _connection(config, "migration") as connection:
            connection.execute("SET ROLE campus_ai_db_owner")
            school = connection.execute(
                "SELECT school_id FROM school_database_anchor"
            ).fetchone()[0]
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(test_f3_family_postgres, "KEY", config["context"].encode())
            patch.setattr(test_f3_writer_postgres, "KEY", config["context"].encode())
            scenario = WriterScenario(engines, school)
            scenario.complete_seed_staff_context()
            identity = scenario.identity()
            actor = identity.staff()
            target_key = "student:f3-writer-safety-wrong-child"
            person_key = "person:f3-writer-safety-wrong-child"
            quality_key = "quality:f7:verified-complete-" + uuid4().hex
            scenario.grant(actor, kind="student", action="maintain", objects=(target_key,))
            maintain = _grant_test_quality(
                scenario, actor, quality_key, purpose="maintain", creation=True
            )
            directory = _directory(scenario, config, (maintain,))
            family = create_family_authority_components(
                session_factory=scenario.sessions["reader"],
                configuration=FamilyConfiguration(
                    approval_reference="F7 isolated fixture only",
                    scope_bindings=tuple(scenario.bindings.values()),
                ),
            )
            guard = FamilyQualityChangeGuard(
                FamilyQualityAuthorizer(directory.guard, family),
                clock=lambda: datetime.now(UTC),
            )
            context = compose_command_context(actor, uuid4(), "f7-complete-formal")
            persistence = SqlAlchemySchoolFactPersistence(
                scenario.sessions["writer"],
                identity.envelope,
                confirmation_credential_issuer=HmacSchoolFactConfirmationCredentialIssuer(
                    config["context"].encode()
                ),
                final_authorization_guard=FamilyQualityFinalGuard(guard),
                family_actor_validator=family.actor_validator,
            )
            version = persistence.for_request(context).current_school_fact_version(school)
            with _connection(config, "migration") as connection:
                connection.execute("SET ROLE campus_ai_db_owner")
                student_version = connection.execute(
                    "SELECT version FROM students WHERE school_id=%s AND source_key=%s",
                    (school, target_key),
                ).fetchone()[0]
            count, digest = mother_set_digest(((target_key, student_version),))
            at = datetime.now(UTC)
            change_set = CanonicalFactChangeSetV4.model_validate_json(json.dumps({
                "schema_version": 4,
                "source_sha256": sha256(context.idempotency_key.encode()).hexdigest(),
                "base_version": version,
                "changes": ({
                    "schema_version": 4,
                    "operation": "upsert",
                    "fact_kind": "fact_quality",
                    "source_key": quality_key,
                    "expected_version": 0,
                    "effective_at": at.isoformat(),
                    "origin": {"sheet_code": "f7-complete-isolated", "row_number": 1},
                    "content": {
                        "quality_kind": "coverage", "target_fact_kind": "student",
                        "owner_kind": "person", "person_source_key": person_key,
                        "target_source_key": target_key, "field_name": "status",
                        "basis_reference": "隔离学校正式核查依据",
                        "responsibility": "隔离测试维护职责", "verified_at": at.isoformat(),
                        "coverage_status": "complete",
                        "coverage_basis": {
                            "schema_version": 1, "school_id": str(school),
                            "owner_kind": "person", "owner_source_key": person_key,
                            "target_fact_kind": "student", "target_source_key": target_key,
                            "field_name": "status", "purpose": None, "contact_kind": None,
                            "valid_from": at.isoformat(), "valid_to": None,
                            "school_fact_version": version, "member_count": count,
                            "member_sha256": digest,
                        },
                    },
                },),
            }, ensure_ascii=False))
            guard.require_change_set(actor, change_set).revalidate()
            service = SchoolFactChangeService(
                reader=persistence, writer=persistence,
                family_actor_validator=family.actor_validator,
            )
            assert service.preflight(
                school_id=school, change_set=change_set, context=context,
                session_binding=identity.session.session_nonce_hash,
            ).is_clean
            prepared = service.prepare_confirmation(
                change_set=change_set, context=context,
                session_binding=identity.session.session_nonce_hash,
            )
            receipt = service.confirm(
                credential=prepared.credential, change_set=change_set,
                context=context, session_binding=identity.session.session_nonce_hash,
            )
            assert receipt.changed_count == 1
            scenario.grant(actor, kind="student", action="read", objects=(target_key,))
            read = _grant_test_quality(
                scenario, actor, quality_key, purpose="read", creation=False
            )
            directory = _directory(scenario, config, (maintain, read))
            family = create_family_authority_components(
                session_factory=scenario.sessions["reader"],
                configuration=FamilyConfiguration(
                    approval_reference="F7 isolated fixture only",
                    scope_bindings=tuple(scenario.bindings.values()),
                ),
            )
            reader = FamilyQualityReader(
                authorizer=FamilyQualityAuthorizer(directory.guard, family),
                executor=F2ConnectionReadExecutor(
                    scenario.sessions["reader"], family.connection_authority
                ),
                current_version=SqlResidenceProjection(scenario.sessions["reader"]).current_version,
                clock=lambda: datetime.now(UTC),
            )
            result = reader.query_quality_for_target(
                actor=actor, target_kind="student", target_key=target_key,
                field_names=("status",), as_of=datetime.now(UTC),
            )
            result.revalidate(actor)
            assert len(result.records) == 1
            claim = result.records[0]
            assert claim["source_key"] == quality_key
            assert verified_exact_student_basis(
                claim["coverage_basis"], school_id=school,
                owner_source_key=person_key, target_source_key=target_key,
                target_version=student_version, field_name="status",
                factual_at=datetime.now(UTC), school_fact_version=result.school_fact_version,
            )
    finally:
        for engine in engines.values():
            engine.dispose()


@pytest.mark.parametrize("target_kind,field", [
    ("login_mapping", "status"),
    ("person_region", "district_name"),
    ("student_contact", "priority"),
    ("student_school_relationship", "student_number"),
    ("guardian_relationship", "status"),
])
def test_f7_complete_f3_target_formal_writer_and_exact_reader(target_kind, field):
    """Each non-student F3 target uses the bounded formal quality path."""
    config_path = Path(os.environ["F7_TEST_DATABASE_CONFIG"])
    config = json.loads(config_path.read_text(encoding="utf-8"))
    engines = {
        role: safe_test_engine(config_path, role)
        for role in ("migration", "runtime", "reader", "writer")
    }
    try:
        with _connection(config, "migration") as connection:
            connection.execute("SET ROLE campus_ai_db_owner")
            school = connection.execute(
                "SELECT school_id FROM school_database_anchor"
            ).fetchone()[0]
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(test_f3_family_postgres, "KEY", config["context"].encode())
            patch.setattr(test_f3_writer_postgres, "KEY", config["context"].encode())
            scenario = WriterScenario(engines, school)
            scenario.complete_seed_staff_context()
            identity = scenario.identity()
            actor = identity.staff()
            if target_kind == "login_mapping":
                target_key = "login:f3-writer-safety-requester"
                person_key = "person:f3-writer-safety-requester"
            else:
                target_key = f"{target_kind}:f7-complete-" + uuid4().hex
                person_key = "person:f7-complete-" + uuid4().hex
                student_key = "student:f7-complete-" + uuid4().hex
                scenario.grant(actor, kind="person", action="maintain", creations=(person_key,))
                assert scenario.stage(identity, kind="person", key=person_key, content={
                    "display_name": "隔离核查合成学生", "person_kind": "other",
                    "lifecycle_status": "active",
                }).confirm().changed_count == 1
                scenario.grant(actor, kind="student", action="maintain", creations=(student_key,))
                assert scenario.stage(identity, kind="student", key=student_key, content={
                    "person_source_key": person_key, "status": "active",
                }).confirm().changed_count == 1
                if target_kind == "student_school_relationship":
                    campus_key = "campus:f7-family-quality-" + uuid4().hex
                    f2_actor, f2_identity, f2_context, f2_service = (
                        test_f3_family_postgres.existing_fixture_writer(engines)
                    )
                    with engines["reader"].connect() as connection:
                        campus_base = connection.scalar(
                            text("SELECT app.school_fact_current_version(:school)"),
                            {"school": school},
                        )
                    campus_change = test_f3_writer_postgres.canonical_changes(
                        idempotency_key=f2_context.idempotency_key,
                        base_version=campus_base, kind="location", key=campus_key,
                        content={"kind": "campus", "display_name": "隔离校区",
                                 "display_path": "隔离校区", "status": "active"},
                    )
                    assert f2_service.preflight(
                        school_id=school, change_set=campus_change, context=f2_context,
                        session_binding=f2_identity.session_nonce_hash,
                    ).is_clean
                    campus_prepared = f2_service.prepare_confirmation(
                        change_set=campus_change, context=f2_context,
                        session_binding=f2_identity.session_nonce_hash,
                    )
                    assert f2_service.confirm(
                        credential=campus_prepared.credential,
                        change_set=campus_change, context=f2_context,
                        session_binding=f2_identity.session_nonce_hash,
                    ).changed_count == 1
                    content = {
                        "student_source_key": student_key,
                        "student_number": "F7-SYNTH", "enrollment_status": "enrolled",
                        "residence_mode": "day",
                        "primary_campus_source_key": campus_key,
                        "administrative_campus_source_key": campus_key,
                    }
                elif target_kind == "guardian_relationship":
                    content = {
                        "guardian_person_source_key": "person:f3-writer-safety-adult",
                        "student_source_key": student_key,
                        "relationship_kind": "verified_guardian",
                        "access_purpose": "guardian_student_profile",
                        "verification_source": "school_review",
                        "verification_reference": "F7-isolated-quality",
                        "status": "pending_verification",
                    }
                elif target_kind == "person_region":
                    content = {"person_source_key": person_key, "district_code": "330100",
                               "district_name": "合成区县", "region_purpose": "student_profile"}
                else:
                    content = {
                        "student_source_key": student_key,
                        "contact_person_source_key": "person:f3-writer-safety-adult",
                        "relationship_kind": "father", "priority": 1, "is_emergency": True,
                    }
                scenario.grant(actor, kind=target_kind, action=(
                    "request" if target_kind == "guardian_relationship" else "maintain"
                ), creations=(target_key,))
                original_error = SqlAlchemySchoolFactPersistence._raise_db_error

                def safe_error(error):
                    original = getattr(error, "orig", None)
                    diagnostic = getattr(original, "diag", None)
                    print("F7 F3 target publication diagnostic", target_kind,
                          getattr(original, "sqlstate", None),
                          getattr(diagnostic, "constraint_name", None),
                          getattr(diagnostic, "table_name", None))
                    original_error(error)

                with pytest.MonkeyPatch.context() as diagnostics:
                    diagnostics.setattr(
                        SqlAlchemySchoolFactPersistence, "_raise_db_error",
                        staticmethod(safe_error),
                    )
                    assert scenario.stage(
                        identity, kind=target_kind, key=target_key, content=content
                    ).confirm().changed_count == 1
                if target_kind == "guardian_relationship":
                    identity = scenario.identity("f3-teacher-parent")
                    actor = identity.staff()
                    scenario.grant(actor, kind=target_kind, action="verify", objects=(target_key,))
                    assert scenario.stage(
                        identity, kind=target_kind, key=target_key,
                        content=content | {"status": "confirmed"}, expected=1,
                    ).confirm().changed_count == 1
            quality_key = f"quality:f7:{target_kind}-complete-" + uuid4().hex
            scenario.grant(actor, kind=target_kind, action=(
                "verify" if target_kind == "guardian_relationship" else "maintain"
            ), objects=(target_key,))
            maintain = _grant_test_quality(
                scenario, actor, quality_key, purpose="maintain", creation=True
            )
            directory = _directory(scenario, config, (maintain,))
            family = create_family_authority_components(
                session_factory=scenario.sessions["reader"],
                configuration=FamilyConfiguration(
                    approval_reference="F7 isolated fixture only",
                    scope_bindings=tuple(scenario.bindings.values()),
                ),
            )
            guard = FamilyQualityChangeGuard(
                FamilyQualityAuthorizer(directory.guard, family),
                clock=lambda: datetime.now(UTC),
            )
            context = compose_command_context(
                actor, uuid4(), f"f7-{target_kind}-complete-" + uuid4().hex
            )
            persistence = SqlAlchemySchoolFactPersistence(
                scenario.sessions["writer"], identity.envelope,
                confirmation_credential_issuer=HmacSchoolFactConfirmationCredentialIssuer(
                    config["context"].encode()
                ),
                final_authorization_guard=FamilyQualityFinalGuard(guard),
                family_actor_validator=family.actor_validator,
            )
            version = persistence.for_request(context).current_school_fact_version(school)
            with _connection(config, "migration") as connection:
                connection.execute("SET ROLE campus_ai_db_owner")
                target_version = connection.execute(
                    "SELECT l.target_version FROM school_fact_change_lineage l "
                    "JOIN master_data_import_batches b ON b.school_id=l.school_id "
                    "AND b.id=l.batch_id AND b.status='applied' "
                    "WHERE l.school_id=%s AND l.fact_kind=%s AND l.fact_source_key=%s "
                    "ORDER BY l.target_version DESC LIMIT 1",
                    (school, target_kind, target_key),
                ).fetchone()[0]
            count, digest = mother_set_digest(((target_key, target_version),))
            at = datetime.now(UTC)
            changes = CanonicalFactChangeSetV4.model_validate_json(json.dumps({
                "schema_version": 4,
                "source_sha256": sha256(context.idempotency_key.encode()).hexdigest(),
                "base_version": version,
                "changes": ({
                    "schema_version": 4, "operation": "upsert", "fact_kind": "fact_quality",
                    "source_key": quality_key, "expected_version": 0,
                    "effective_at": at.isoformat(),
                    "origin": {"sheet_code": "f7-login-complete", "row_number": 1},
                    "content": {
                        "quality_kind": "coverage", "coverage_status": "complete",
                        "target_fact_kind": target_kind, "owner_kind": "person",
                        "person_source_key": person_key, "target_source_key": target_key,
                        "field_name": field, "basis_reference": "隔离正式核查",
                        "responsibility": "隔离测试维护职责", "verified_at": at.isoformat(),
                        "coverage_basis": {
                            "schema_version": 1, "school_id": str(school),
                            "owner_kind": "person", "owner_source_key": person_key,
                            "target_fact_kind": target_kind, "target_source_key": target_key,
                            "field_name": field, "purpose": None, "contact_kind": None,
                            "valid_from": at.isoformat(), "valid_to": None,
                            "school_fact_version": version, "member_count": count,
                            "member_sha256": digest,
                        },
                    },
                },),
            }, ensure_ascii=False))
            guard.require_change_set(actor, changes).revalidate()
            service = SchoolFactChangeService(
                reader=persistence, writer=persistence,
                family_actor_validator=family.actor_validator,
            )
            assert service.preflight(
                school_id=school, change_set=changes, context=context,
                session_binding=identity.session.session_nonce_hash,
            ).is_clean
            prepared = service.prepare_confirmation(
                change_set=changes, context=context,
                session_binding=identity.session.session_nonce_hash,
            )
            receipt = service.confirm(
                credential=prepared.credential, change_set=changes,
                context=context, session_binding=identity.session.session_nonce_hash,
            )
            assert receipt.changed_count == 1
            scenario.grant(actor, kind=target_kind, action="read", objects=(target_key,))
            read = _grant_test_quality(
                scenario, actor, quality_key, purpose="read", creation=False
            )
            directory = _directory(scenario, config, (maintain, read))
            family = create_family_authority_components(
                session_factory=scenario.sessions["reader"],
                configuration=FamilyConfiguration(
                    approval_reference="F7 isolated fixture only",
                    scope_bindings=tuple(scenario.bindings.values()),
                ),
            )
            reader = FamilyQualityReader(
                authorizer=FamilyQualityAuthorizer(directory.guard, family),
                executor=F2ConnectionReadExecutor(
                    scenario.sessions["reader"], family.connection_authority
                ),
                current_version=SqlResidenceProjection(scenario.sessions["reader"]).current_version,
                clock=lambda: datetime.now(UTC),
            )
            result = reader.query_quality_for_target(
                actor=actor, target_kind=target_kind, target_key=target_key,
                field_names=(field,), as_of=datetime.now(UTC),
            )
            result.revalidate(actor)
            assert len(result.records) == 1
            assert verify_coverage_basis(
                result.records[0]["coverage_basis"], school_id=school,
                owner_kind="person", owner_source_key=person_key,
                target_fact_kind=target_kind, target_source_key=target_key,
                field_name=field, purpose=None, contact_kind=None,
                factual_at=datetime.now(UTC),
                school_fact_version=result.school_fact_version,
                members=((target_key, target_version),),
            )
    finally:
        for engine in engines.values():
            engine.dispose()


def _connection(config: dict, role: str):
    try:
        return psycopg.connect(
            host=config["host"],
            port=config["port"],
            dbname=config["database"],
            user=(
                f"campus_ai_{role}"
                if role in {"migration", "runtime"}
                else f"campus_ai_school_fact_{role}"
            ),
            password=config[role],
        )
    except Exception:
        raise RuntimeError("Owned F7 test connection unavailable") from None


def test_f7_family_quality_exact_reader_returns_no_unpublished_fact_and_runtime_is_denied():
    config = json.loads(Path(os.environ["F7_TEST_DATABASE_CONFIG"]).read_text(encoding="utf-8"))
    assert config["owner"] == "F7 isolated quality migration and reader tests"
    assert config["container"] == "campus-f7-tests-20260924"
    assert config["database"] == "f7_fixture_test"
    with _connection(config, "migration") as connection:
        connection.execute("SET ROLE campus_ai_db_owner")
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone()[0] in {
            "20260924_46", "20260924_47", "20260924_48", "20260924_49",
            "20260924_50", "20260924_51", "20260924_52", "20260924_53",
            "20260924_54",
        }
        assert connection.execute("SELECT system_identifier::text FROM pg_control_system()") \
            .fetchone()[0] == config["system_identifier"]
        anchor = connection.execute("SELECT school_id FROM school_database_anchor").fetchone()
        check = connection.execute(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conname='ck_school_fact_qualities_target'"
        ).fetchone()[0]
        for target_kind in F3_TARGETS:
            assert f"'{target_kind}'" in check
        assert connection.execute(
            "SELECT has_function_privilege('campus_ai_school_fact_reader', "
            "'app.school_fact_f7_family_quality(uuid,text[],text,text,text[],timestamptz)', "
            "'EXECUTE')"
        ).fetchone()[0]
        assert not connection.execute(
            "SELECT has_function_privilege('campus_ai_runtime', "
            "'app.school_fact_f7_family_quality(uuid,text[],text,text,text[],timestamptz)', "
            "'EXECUTE')"
        ).fetchone()[0]
        assert connection.execute(
            "SELECT has_function_privilege('campus_ai_school_fact_reader', "
            "'app.school_fact_f7_family_quality_detail(uuid,text)','EXECUTE')"
        ).fetchone()[0]
        assert not connection.execute(
            "SELECT has_function_privilege('campus_ai_runtime', "
            "'app.school_fact_f7_family_quality_detail(uuid,text)','EXECUTE')"
        ).fetchone()[0]
        runtime_can_login = connection.execute(
            "SELECT rolcanlogin FROM pg_roles WHERE rolname='campus_ai_runtime'"
        ).fetchone()[0]
    if anchor is None:
        owner_engine = safe_test_engine(Path(os.environ["F7_TEST_DATABASE_CONFIG"]), "migration")
        try:
            school, *_ = seed_staff(
                sessionmaker(owner_engine, expire_on_commit=False),
                school_source_key="school:f7-test",
            )
        finally:
            owner_engine.dispose()
    else:
        school = UUID(str(anchor[0]))
    at = datetime.now(UTC)
    with _connection(config, "reader") as connection:
        params = (school, ["quality:f7-unpublished"], "student", "student:f7-a", ["status"], at)
        row = connection.execute(
            "SELECT app.school_fact_f7_family_quality(%s,%s,%s,%s,%s,%s)", params
        ).fetchone()[0]
        assert row["records"] == []
        assert isinstance(row["school_fact_version"], int)
        assert connection.execute(
            "SELECT app.school_fact_f7_family_quality_detail(%s,%s)",
            (school, "quality:f7-unpublished"),
        ).fetchone()[0] is None
        with pytest.raises(psycopg.Error):
            connection.execute(
                "SELECT app.school_fact_f7_family_quality(%s,%s,%s,%s,%s,%s)",
                (uuid4(), *params[1:]),
            ).fetchone()
    if runtime_can_login:
        with _connection(config, "runtime") as connection:
            with pytest.raises(psycopg.Error):
                connection.execute(
                    "SELECT app.school_fact_f7_family_quality(%s,%s,%s,%s,%s,%s)", params
                ).fetchone()
