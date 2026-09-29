"""F4/F5 representative quality uses the domain final guard, Writer and Reader.

Targets are previously published synthetic facts in the owned F7 fixture. The
test fails if those prerequisites disappear; it does not mint fake lineage.
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

from app.composition.school_fact_education_authority import create_education_authority
from app.composition.school_fact_education_execution import EducationDependencyFinalGuard
from app.composition.school_fact_education_quality import (
    EducationQualityBridge,
    SqlEducationDependencyProjection,
)
from app.composition.school_fact_education_reader import SqlEducationProjection
from app.composition.school_fact_education_settings import (
    EducationConfiguration,
    EducationScopeBinding,
)
from app.composition.school_fact_persistence import SqlAlchemySchoolFactPersistence
from app.composition.school_fact_read_connection import F2ConnectionReadExecutor
from app.composition.school_facts import HmacSchoolFactConfirmationCredentialIssuer
from app.platform.identity.execution_context import compose_command_context
from app.platform.school_facts.application import SchoolFactChangeService
from app.platform.school_facts.contracts import CanonicalFactChangeSetV4
from app.platform.school_facts.f4_policy import (
    F4_FACT_FIELDS,
    F4_SCOPE_OWNER,
    F4_SCOPE_SCHEMA,
    F4FactScope,
)
from app.platform.school_facts.f5_policy import (
    F5_FACT_FIELDS,
    F5_SCOPE_OWNER,
    F5_SCOPE_SCHEMA,
    F5FactScope,
)
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

CASES = (
    ("education_grade_group", "education_grade_group", "grade_source_key", "F4"),
    ("class_teacher_assignment", "education_class", "teacher_role", "F4"),
    ("teaching_assignment", "teaching_offering", "teaching_role", "F5"),
)


def _grant_education(scenario, actor, *, kind, key, field, action):
    owner = "F5" if kind in F5_FACT_FIELDS else "F4"
    scope_cls, schema_owner, schema, fields = (
        (F5FactScope, F5_SCOPE_OWNER, F5_SCOPE_SCHEMA, F5_FACT_FIELDS)
        if owner == "F5" else
        (F4FactScope, F4_SCOPE_OWNER, F4_SCOPE_SCHEMA, F4_FACT_FIELDS)
    )
    selected_fields = (field,) if isinstance(field, str) else tuple(field)
    selected_keys = (key,) if isinstance(key, str) else tuple(key)
    assert selected_fields and set(selected_fields) <= fields[kind]
    scope = scope_cls(scenario.school, kind, selected_keys, (), selected_fields)
    now = datetime.now(UTC) - timedelta(seconds=1)
    duty = f"{owner.lower()}_{kind}_{'reader' if action == 'read' else 'maintainer'}"
    with scenario.owner.begin() as session:
        duty_id = session.scalar(text(
            "SELECT id FROM governance_duties WHERE school_id=:school AND duty_code=:duty"
        ), {"school": scenario.school, "duty": duty})
        if duty_id is None:
            duty_id = uuid4()
            scenario._add(session, "governance_duties", id=duty_id,
                          school_id=scenario.school, duty_code=duty, current_version=1)
            scenario._add(session, "governance_duty_versions", school_id=scenario.school,
                          duty_id=duty_id, display_name="F7隔离教育质量职责",
                          description="TEST FIXTURE ONLY", is_active=True,
                          effective_from=now, version=1)
        scope_id, grant_id = uuid4(), uuid4()
        scenario._add(session, "governance_scopes", id=scope_id,
                      school_id=scenario.school, scope_key=f"f7-edu-quality:{scope_id.hex}",
                      current_version=1)
        scenario._add(session, "governance_scope_versions", school_id=scenario.school,
                      scope_id=scope_id, schema_owner=schema_owner,
                      schema_name=schema, schema_version=1,
                      reference_document=scope.document,
                      reference_sha256=scope.document_sha256,
                      is_active=True, effective_from=now, version=1)
        scenario._add(session, "governance_grants", id=grant_id,
                      school_id=scenario.school, assignment_id=actor.assignment_id,
                      duty_id=duty_id, current_version=1)
        scenario._add(session, "governance_grant_versions", school_id=scenario.school,
                      grant_id=grant_id, duty_id=duty_id, duty_version=1,
                      scope_id=scope_id, scope_version=1,
                      basis_authority="isolated_test_fixture", basis_reference="F7-owned-Education",
                      valid_from=now, effective_from=now,
                      planned_valid_to=now + timedelta(hours=1),
                      transition_kind="create", version=1)
        session.execute(text(
            "INSERT INTO governance_assignment_epochs(school_id,assignment_id,epoch) "
            "VALUES(:school,:assignment,1) ON CONFLICT(school_id,assignment_id) "
            "DO UPDATE SET epoch=governance_assignment_epochs.epoch+1"
        ), {"school": scenario.school, "assignment": actor.assignment_id})
    return EducationScopeBinding(
        assignment_id=actor.assignment_id,
        operation_code=f"{schema_owner}.{kind}.{action}", scope_id=scope_id,
    )


def _published_pair(config, school, target_kind, owner_kind):
    owner_expr = (
        "f.source_key" if target_kind == owner_kind else
        "v.payload->>'class_source_key'" if owner_kind == "education_class" else
        "v.payload->>'offering_source_key'"
    )
    with _connection(config, "migration") as connection:
        connection.execute("SET ROLE campus_ai_db_owner")
        row = connection.execute(
            "SELECT f.source_key, " + owner_expr + " AS owner_key "
            "FROM school_education_facts f JOIN school_education_versions v "
            "ON v.school_id=f.school_id AND v.fact_id=f.id "
            "WHERE f.school_id=%s AND f.kind=%s AND v.superseded_at IS NULL "
            "AND v.valid_from<=now() AND (v.valid_to IS NULL OR now()<v.valid_to) "
            "AND EXISTS (SELECT 1 FROM school_fact_change_lineage l "
            "JOIN master_data_import_batches b ON b.school_id=l.school_id "
            "AND b.id=l.batch_id AND b.status='applied' "
            "WHERE l.school_id=f.school_id AND l.fact_kind=f.kind "
            "AND l.fact_source_key=f.source_key) "
            "ORDER BY f.source_key LIMIT 1",
            (school, target_kind),
        ).fetchone()
    assert row is not None and row[0] and row[1], (
        "F7 isolated formal education target prerequisite missing", target_kind
    )
    return row[0], row[1]


@pytest.mark.parametrize("target_kind,owner_kind,field,owner", CASES)
def test_education_quality_uses_domain_final_guard_formal_writer_and_reader(
    target_kind, owner_kind, field, owner
):
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
            target_key, owner_key = _published_pair(config, school, target_kind, owner_kind)
            identity = scenario.identity()
            actor = identity.staff()
            quality_key = f"quality:f7:{target_kind}-guard-{uuid4().hex}"
            f2_create = _grant_test_quality(
                scenario, actor, quality_key, purpose="maintain", creation=True
            )
            f2_read = _grant_test_quality(
                scenario, actor, quality_key, purpose="read", creation=False
            )
            directory = _directory(scenario, config, (f2_create, f2_read))
            owner_field = field if owner_kind == target_kind else (
                sorted((F4_FACT_FIELDS | F5_FACT_FIELDS)[owner_kind])[0]
            )
            coordinates = [(owner_kind, owner_key, owner_field)]
            if target_kind != owner_kind:
                coordinates.append((target_kind, target_key, field))
            bindings = tuple(
                _grant_education(scenario, actor, kind=kind, key=key,
                                 field=selected_field, action=action)
                for kind, key, selected_field in coordinates
                for action in ("maintain", "read")
            )
            authority = create_education_authority(
                sessions=scenario.sessions["reader"],
                configuration=EducationConfiguration(
                    approval_reference="F7 isolated fixture only", scope_bindings=bindings,
                ),
            )
            projection = SqlEducationProjection(
                F2ConnectionReadExecutor(
                    scenario.sessions["reader"], authority.connection_authority,
                ),
                current_sessions=authority.connection_authority.current_sessions,
            )
            quality = EducationQualityBridge(
                f2_guard=directory.guard,
                education_authority=authority,
                projection=SqlEducationDependencyProjection(
                    projection, f5_enabled=owner == "F5",
                ),
            )
            context = compose_command_context(actor, uuid4(), quality_key)
            persistence = SqlAlchemySchoolFactPersistence(
                scenario.sessions["writer"], identity.envelope,
                confirmation_credential_issuer=HmacSchoolFactConfirmationCredentialIssuer(
                    config["context"].encode()
                ),
                final_authorization_guard=EducationDependencyFinalGuard(
                    quality=quality,
                    retained=lambda *_args: pytest.fail("Unexpected noneducation partition"),
                ),
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
                    "origin": {"sheet_code": "f7-owned-education-source", "row_number": 1},
                    "content": {
                        "quality_kind": "source", "source_status": "unavailable",
                        "target_fact_kind": target_kind, "target_source_key": target_key,
                        "owner_kind": "education", "education_target_kind": owner_kind,
                        "education_target_source_key": owner_key, "field_name": field,
                        "basis_reference": "F7隔离合成来源核验",
                        "responsibility": "隔离测试维护职责", "verified_at": at,
                    },
                },),
            })
            quality.require_change_set(actor, changes).revalidate()
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
            result = quality.read_for_source(actor, "fact_quality", quality_key, at)
            result["proof"].revalidate()
            assert result["content"]["source_status"] == "unavailable"
            assert result["content"]["target_fact_kind"] == target_kind
            assert result["content"]["field_name"] == field
    finally:
        for engine in engines.values():
            engine.dispose()
