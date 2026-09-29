"""Isolated owner-quality publication with F7 package ingress disabled.

All grants and facts in this file belong to the F7 test database.  This is not
evidence of a grant, publication or session in the original local instance.
"""

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from app.composition.school_fact_directory_product import (
    DirectoryFactProductService,
    FactChangeInput,
)
from app.composition.school_fact_directory_adapter import DirectoryProductAdapter
from app.composition.school_fact_family_quality import (
    FamilyQualityAuthorizer,
    FamilyQualityChangeGuard,
    FamilyQualityFinalGuard,
    FamilyQualityReader,
)
from app.composition.school_fact_family_quality_product import FamilyQualityProductAdapter
from app.composition.school_fact_family_authority import FamilyFactAuthorityDenied
from app.composition.school_fact_family_services import create_family_staff_components
from app.composition.school_fact_family_settings import FamilyConfiguration
from app.composition.school_fact_persistence import SqlAlchemySchoolFactPersistence
from app.composition.school_fact_read_connection import F2ConnectionReadExecutor
from app.composition.school_fact_residence_reader import SqlResidenceProjection
from app.composition.school_facts import (
    HmacSchoolFactConfirmationCredentialIssuer,
    InMemorySchoolFactPlanStore,
)
from app.platform.school_facts.application import SchoolFactChangeService
from app.platform.school_facts.application import SchoolFactChangeRejected
from tests.school_facts import test_f3_family_postgres, test_f3_writer_postgres
from tests.school_facts.test_f3_family_postgres import safe_test_engine
from tests.school_facts.test_f3_writer_postgres import WriterScenario
from tests.school_facts.test_f7_family_quality_postgres import (
    _connection,
    _directory,
    _grant_test_quality,
)
from tests.school_facts.test_f7_startup_contract import F7_PROBE_CONTRACTS, f7_probe_contract


pytestmark = pytest.mark.skipif(
    not os.environ.get("F7_TEST_DATABASE_CONFIG"), reason="owned F7 PG config required"
)


def test_off_package_f3_quality_prepares_confirms_receipt_and_reads_back():
    config_path = Path(os.environ["F7_TEST_DATABASE_CONFIG"])
    config = json.loads(config_path.read_text(encoding="utf-8"))
    assert config["owner"] == "F7 isolated quality migration and reader tests"
    assert config["container"] == "campus-f7-tests-20260924"
    engines = {role: safe_test_engine(config_path, role)
               for role in ("migration", "runtime", "reader", "writer")}
    try:
        with _connection(config, "migration") as connection:
            connection.execute("SET ROLE campus_ai_db_owner")
            school = connection.execute("SELECT school_id FROM school_database_anchor").fetchone()[0]
            assert connection.execute("SELECT version_num FROM alembic_version").fetchone()[0] == F7_PROBE_CONTRACTS[f7_probe_contract()]
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(test_f3_family_postgres, "KEY", config["context"].encode())
            patch.setattr(test_f3_writer_postgres, "KEY", config["context"].encode())
            scenario = WriterScenario(engines, school)
            scenario.complete_seed_staff_context()
            identity = scenario.identity()
            actor = identity.staff()
            student_key = "student:f3-writer-safety-wrong-child"
            person_key = "person:f3-writer-safety-wrong-child"
            unrelated_person = "person:f3-writer-safety-adult"
            quality_key = "quality:f7-off-student-" + uuid4().hex
            read_grant = scenario.grant(
                actor, kind="student", action="read", objects=(student_key,)
            )
            maintain_grant = scenario.grant(
                actor, kind="student", action="maintain", objects=(student_key,)
            )
            q_create = _grant_test_quality(
                scenario, actor, quality_key, purpose="maintain", creation=True
            )
            actor = identity.staff()
            directory = _directory(
                scenario, config, (q_create,),
                source_catalog={"person": [person_key, unrelated_person],
                                "fact_quality": [quality_key]},
                creation_catalog={"fact_quality": {quality_key: "隔离学生来源声明"}},
            )
            family = create_family_staff_components(
                sessions=scenario.sessions["reader"],
                configuration=FamilyConfiguration(
                    approval_reference="F7 isolated fixture only",
                    scope_bindings=tuple(scenario.bindings.values()),
                    source_catalog={"student": [student_key]},
                ),
                reference_key=config["context"].encode(), directory=directory,
            )
            quality_authorizer = FamilyQualityAuthorizer(directory.guard, family.authority)
            quality_guard = FamilyQualityChangeGuard(
                quality_authorizer, clock=lambda: datetime.now(UTC)
            )
            adapter = FamilyQualityProductAdapter(
                directory=DirectoryProductAdapter(directory.query, evidence=directory.evidence),
                family=family, guard=quality_guard,
            )
            persistence = SqlAlchemySchoolFactPersistence(
                scenario.sessions["writer"], identity.envelope,
                confirmation_credential_issuer=HmacSchoolFactConfirmationCredentialIssuer(
                    config["context"].encode()
                ),
                final_authorization_guard=FamilyQualityFinalGuard(quality_guard),
                family_actor_validator=family.authority.actor_validator,
            )
            boundary = SchoolFactChangeService(
                reader=persistence, writer=persistence,
                family_actor_validator=family.authority.actor_validator,
            )
            product = DirectoryFactProductService(
                boundary=boundary, resolve_change=adapter.resolve_change,
                require_maintenance=lambda who, batch: quality_guard.require_change_set(
                    who, batch
                ).revalidate(),
                require_receipt=lambda who: directory.guard.require_current(
                    who, "fact_quality", purpose="maintain"
                ),
                plan_store=InMemorySchoolFactPlanStore(),
            )
            creation = directory.query.creation_choices(actor=actor, fact_kind="fact_quality")
            assert len(creation) == 1
            directory.guard.require_current(actor, "fact_quality", purpose="maintain")
            assert directory.query.source_for(
                actor=actor, fact_kind="fact_quality",
                reference=creation[0]["creation_ref"], creation=True,
            ) == quality_key
            payload = FactChangeInput(
                fact_kind="fact_quality", creation_ref=creation[0]["creation_ref"],
                fields={
                    "quality_kind": "source", "source_status": "available",
                    "target_fact_kind": "student",
                    "target_ref": family.repository.reference_for(
                        actor=actor, fact_kind="student", source_key=student_key
                    ),
                    "field_name": "status", "basis_reference": "隔离正式核查",
                    "responsibility": "隔离维护者", "verified_at": datetime.now(UTC).isoformat(),
                },
                reason="登记学生状态来源",
            )
            wrong_person = payload.model_copy(update={"fields": {
                **payload.fields,
                "person_ref": directory.query.reference_for(
                    actor=actor, fact_kind="person", source_key=unrelated_person
                ),
            }})
            with pytest.raises(PermissionError, match="prepare_resolution_denied"):
                product.prepare(
                    actor=actor, session_binding=identity.session.session_nonce_hash,
                    payload=wrong_person, request_id=uuid4(),
                )
            proposal = product.prepare(
                actor=actor, session_binding=identity.session.session_nonce_hash,
                payload=payload, request_id=uuid4(),
            )
            stale_proposal = product.prepare(
                actor=actor, session_binding=identity.session.session_nonce_hash,
                payload=payload, request_id=uuid4(),
            )
            receipt = product.confirm(
                actor=actor, session_binding=identity.session.session_nonce_hash,
                proposal_ref=proposal.proposal_ref, request_id=uuid4(),
            )
            assert receipt.status == "succeeded"
            replay = product.receipt(
                actor=actor, session_binding=identity.session.session_nonce_hash,
                proposal_ref=proposal.proposal_ref, request_id=uuid4(),
            )
            assert replay.status == "succeeded"
            stale = product.confirm(
                actor=actor, session_binding=identity.session.session_nonce_hash,
                proposal_ref=stale_proposal.proposal_ref, request_id=uuid4(),
            )
            assert stale.status != "succeeded"
            with pytest.raises(SchoolFactChangeRejected):
                directory.guard.require_current(actor, "fact_quality", purpose="read")
            q_read = _grant_test_quality(
                scenario, actor, quality_key, purpose="read", creation=False
            )
            post_directory = _directory(
                scenario, config, (q_create, q_read),
                source_catalog={"person": [person_key], "fact_quality": [quality_key]},
            )
            reader = FamilyQualityReader(
                authorizer=FamilyQualityAuthorizer(post_directory.guard, family.authority),
                executor=F2ConnectionReadExecutor(
                    scenario.sessions["reader"], family.authority.connection_authority,
                ),
                current_version=SqlResidenceProjection(
                    scenario.sessions["reader"]
                ).current_version,
                clock=lambda: datetime.now(UTC),
            )
            result = reader.query_quality_for_target(
                actor=actor, target_kind="student", target_key=student_key,
                field_names=("status",), as_of=datetime.now(UTC),
            )
            result.revalidate(actor)
            assert result.records[0]["source_key"] == quality_key
            scenario.revoke_fixture_grant(actor, maintain_grant)
            with pytest.raises(FamilyFactAuthorityDenied):
                product.prepare(
                    actor=actor, session_binding=identity.session.session_nonce_hash,
                    payload=payload, request_id=uuid4(),
                )
            scenario.revoke_fixture_grant(actor, read_grant)
            with pytest.raises(FamilyFactAuthorityDenied):
                product.prepare(
                    actor=actor, session_binding=identity.session.session_nonce_hash,
                    payload=payload, request_id=uuid4(),
                )
    finally:
        for engine in engines.values():
            engine.dispose()
