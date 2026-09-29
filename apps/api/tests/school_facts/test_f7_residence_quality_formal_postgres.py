"""One F6-owned quality claim through formal Writer and current dual-scope Reader.

The claim concerns source availability for one synthetic student's residence
field. It does not assert that a particular residence exists, nor that the
four F6 targets each have a published quality claim.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import text

from app.composition.school_fact_family_authority import create_family_authority_components
from app.composition.school_fact_family_settings import FamilyConfiguration
from app.composition.school_fact_persistence import SqlAlchemySchoolFactPersistence
from app.composition.school_fact_residence_authority import create_residence_authority
from app.composition.school_fact_residence_execution import ResidenceQualityFinalGuard
from app.composition.school_fact_residence_quality import (
    ResidenceQualityBridge,
    ResidenceQualityOwnerAuthorizer,
)
from app.composition.school_fact_residence_reader import SqlResidenceProjection
from app.composition.school_fact_residence_settings import (
    ResidenceConfiguration,
    ResidenceScopeBinding,
)
from app.composition.school_facts import HmacSchoolFactConfirmationCredentialIssuer
from app.platform.identity.execution_context import compose_command_context
from app.platform.school_facts.application import (
    SchoolFactChangeRejected,
    SchoolFactChangeService,
)
from app.platform.school_facts.contracts import CanonicalFactChangeSetV4
from app.platform.school_facts.f6_policy import F6FactScope
from tests.school_facts import test_f3_family_postgres, test_f3_writer_postgres
from tests.school_facts.test_f3_family_postgres import safe_test_engine
from tests.school_facts.test_f3_writer_postgres import WriterScenario
from tests.school_facts.test_f7_family_quality_postgres import (
    _connection,
    _directory,
    _grant_test_quality,
)

pytestmark = pytest.mark.skipif(
    not os.environ.get("F7_TEST_DATABASE_CONFIG"), reason="owned F7 PG config required"
)


def _grant_f6_person_quality_owner(scenario, actor, person_key, *, action):
    """Isolated test grant: exact person owner and one F6 field."""
    scope = F6FactScope(
        scenario.school, "student_bed_residence", (), (),
        ("bed_source_key",),
        (("person", person_key),),
    )
    now = datetime.now(UTC) - timedelta(seconds=1)
    duty = f"f6_student_bed_residence_{'reader' if action == 'read' else 'maintainer'}"
    with scenario.owner.begin() as session:
        duty_id = session.scalar(text(
            "SELECT id FROM governance_duties WHERE school_id=:school AND duty_code=:duty"
        ), {"school": scenario.school, "duty": duty})
        if duty_id is None:
            duty_id = uuid4()
            scenario._add(session, "governance_duties", id=duty_id,
                          school_id=scenario.school, duty_code=duty, current_version=1)
            scenario._add(session, "governance_duty_versions", school_id=scenario.school,
                          duty_id=duty_id, display_name="F7隔离寄宿质量职责",
                          description="TEST FIXTURE ONLY", is_active=True,
                          effective_from=now, version=1)
        scope_id, grant_id = uuid4(), uuid4()
        scenario._add(session, "governance_scopes", id=scope_id,
                      school_id=scenario.school, scope_key=f"f7-f6-quality:{scope_id.hex}",
                      current_version=1)
        scenario._add(session, "governance_scope_versions", school_id=scenario.school,
                      scope_id=scope_id, schema_owner="school_facts.f6",
                      schema_name="exact_residence_fact_set", schema_version=1,
                      reference_document=scope.document,
                      reference_sha256=scope.document_sha256,
                      is_active=True, effective_from=now, version=1)
        scenario._add(session, "governance_grants", id=grant_id,
                      school_id=scenario.school, assignment_id=actor.assignment_id,
                      duty_id=duty_id, current_version=1)
        scenario._add(session, "governance_grant_versions", school_id=scenario.school,
                      grant_id=grant_id, duty_id=duty_id, duty_version=1,
                      scope_id=scope_id, scope_version=1,
                      basis_authority="isolated_test_fixture", basis_reference="F7-owned-F6",
                      valid_from=now, effective_from=now,
                      planned_valid_to=now + timedelta(hours=1),
                      transition_kind="create", version=1)
        session.execute(text(
            "INSERT INTO governance_assignment_epochs(school_id,assignment_id,epoch) "
            "VALUES(:school,:assignment,1) ON CONFLICT(school_id,assignment_id) "
            "DO UPDATE SET epoch=governance_assignment_epochs.epoch+1"
        ), {"school": scenario.school, "assignment": actor.assignment_id})
    return ResidenceScopeBinding(
        assignment_id=actor.assignment_id,
        operation_code=f"school_facts.f6.student_bed_residence.{action}",
        scope_id=scope_id,
    )


def test_f6_student_source_risk_uses_formal_writer_and_current_authorized_reader():
    config_path = Path(os.environ["F7_TEST_DATABASE_CONFIG"])
    config = json.loads(config_path.read_text(encoding="utf-8"))
    assert config["owner"] == "F7 isolated quality migration and reader tests"
    assert config["container"] == "campus-f7-tests-20260924"
    assert config["database"] == "f7_fixture_test"
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
            assert connection.execute(
                "SELECT version_num FROM alembic_version"
            ).fetchone()[0] == "20260924_55"
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(test_f3_family_postgres, "KEY", config["context"].encode())
            patch.setattr(test_f3_writer_postgres, "KEY", config["context"].encode())
            scenario = WriterScenario(engines, school)
            scenario.complete_seed_staff_context()
            identity = scenario.identity()
            actor = identity.staff()
            person_key = "person:f3-writer-safety-wrong-child"
            scenario.grant(actor, kind="person", action="read", objects=(person_key,))
            family = create_family_authority_components(
                session_factory=scenario.sessions["reader"],
                configuration=FamilyConfiguration(
                    approval_reference="F7 isolated fixture only",
                    scope_bindings=tuple(scenario.bindings.values()),
                ),
            )
            quality_key = f"quality:f7:f6-school-source-{uuid4().hex}"
            f2_create = _grant_test_quality(
                scenario, actor, quality_key, purpose="maintain", creation=True
            )
            f2_read = _grant_test_quality(
                scenario, actor, quality_key, purpose="read", creation=False
            )
            directory = _directory(scenario, config, (f2_create, f2_read))
            bindings = (
                _grant_f6_person_quality_owner(scenario, actor, person_key, action="maintain"),
                _grant_f6_person_quality_owner(scenario, actor, person_key, action="read"),
            )
            residence = create_residence_authority(
                sessions=scenario.sessions["reader"],
                configuration=ResidenceConfiguration(
                    approval_reference="F7 isolated fixture only", scope_bindings=bindings,
                ),
            )
            bridge = ResidenceQualityBridge(
                f2_guard=directory.guard,
                residence_authority=residence,
                projection=SqlResidenceProjection(scenario.sessions["reader"]),
                owner_authorizer=ResidenceQualityOwnerAuthorizer(
                    f2_guard=directory.guard, family_authority=family,
                    clock=lambda: datetime.now(UTC),
                ),
            )
            context = compose_command_context(actor, uuid4(), quality_key)
            persistence = SqlAlchemySchoolFactPersistence(
                scenario.sessions["writer"], identity.envelope,
                confirmation_credential_issuer=HmacSchoolFactConfirmationCredentialIssuer(
                    config["context"].encode()
                ),
                final_authorization_guard=ResidenceQualityFinalGuard(bridge),
            )
            service = SchoolFactChangeService(reader=persistence, writer=persistence)
            version = persistence.for_request(context).current_school_fact_version(school)
            at = datetime.now(UTC)
            changes = CanonicalFactChangeSetV4.model_validate({
                "schema_version": 4,
                "source_sha256": sha256(context.idempotency_key.encode()).hexdigest(),
                "base_version": version,
                "changes": ({
                    "schema_version": 4, "operation": "upsert",
                    "fact_kind": "fact_quality", "source_key": quality_key,
                    "expected_version": 0, "effective_at": at,
                    "origin": {"sheet_code": "f7-owned-f6-source", "row_number": 1},
                    "content": {
                        "quality_kind": "source", "source_status": "unavailable",
                        "target_fact_kind": "student_bed_residence",
                        "owner_kind": "person", "person_source_key": person_key,
                        "field_name": "bed_source_key",
                        "basis_reference": "F7隔离合成来源核验",
                        "responsibility": "隔离测试维护职责", "verified_at": at,
                    },
                },),
            })
            bridge.require_change_set(actor, changes).revalidate()
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
                batch = connection.execute(
                    "SELECT import_batch_id FROM school_fact_qualities "
                    "WHERE school_id=%s AND source_key=%s", (school, quality_key),
                ).fetchone()[0]
                assert batch == receipt.batch_id
            value = bridge.read_content(actor, quality_key)
            value["proof"].revalidate()
            assert value["content"]["source_status"] == "unavailable"
            assert value["content"]["target_fact_kind"] == "student_bed_residence"
            assert value["content"]["field_name"] == "bed_source_key"
            no_quality_read = _directory(scenario, config, (f2_create,))
            with pytest.raises(SchoolFactChangeRejected):
                ResidenceQualityBridge(
                    f2_guard=no_quality_read.guard,
                    residence_authority=residence,
                    projection=bridge.projection,
                ).read_content(actor, quality_key)
            no_target_read = create_residence_authority(
                sessions=scenario.sessions["reader"],
                configuration=ResidenceConfiguration(
                    approval_reference="F7 isolated denial check",
                    scope_bindings=(bindings[0],),
                ),
            )
            with pytest.raises(PermissionError):
                ResidenceQualityBridge(
                    f2_guard=directory.guard,
                    residence_authority=no_target_read,
                    projection=bridge.projection,
                ).read_content(actor, quality_key)
    finally:
        for engine in engines.values():
            engine.dispose()
