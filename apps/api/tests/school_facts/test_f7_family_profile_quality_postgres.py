"""F7 guardian quality projection against the isolated PostgreSQL school.

The quality rows below are explicitly isolated SQL test fixtures. They use an
existing applied synthetic batch to exercise the Reader publication guard;
formal quality Writer publication is tested in test_f7_family_quality_postgres.
No original-school database, grant, or production fact is touched.
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
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import sessionmaker

from app.composition.school_fact_family_authority import create_family_authority_components
from app.composition.school_fact_family_execution import FamilyFinalAuthorizationGuard
from app.composition.school_fact_family_settings import FamilyConfiguration
from app.composition.school_fact_persistence import SqlAlchemySchoolFactPersistence
from app.composition.school_facts import HmacSchoolFactConfirmationCredentialIssuer
from app.platform.identity.execution_context import compose_command_context
from app.platform.school_facts.application import SchoolFactChangeService
from app.platform.school_facts.contracts import CanonicalFactChangeSetV4
from tests.school_facts import test_f3_family_postgres, test_f3_writer_postgres
from tests.school_facts.test_f3_family_postgres import safe_test_engine
from tests.school_facts.test_f3_writer_postgres import WriterScenario

pytestmark = pytest.mark.skipif(
    not os.environ.get("F7_TEST_DATABASE_CONFIG"), reason="owned F7 PG config required"
)


READ_SQL = text(
    "SELECT app.school_fact_f7_family_profile_quality("
    ":school,:adult,:relation,:version,:at,:fields)"
)


@pytest.fixture
def case():
    config = Path(os.environ["F7_TEST_DATABASE_CONFIG"])
    metadata = json.loads(config.read_text(encoding="utf-8"))
    assert metadata["database"].startswith("f7_") and metadata["database"].endswith("_test")
    engines = {
        role: safe_test_engine(config, role)
        for role in ("migration", "reader", "runtime", "writer")
    }
    owner = sessionmaker(engines["migration"])
    reader = sessionmaker(engines["reader"])
    runtime = sessionmaker(engines["runtime"])
    try:
        with owner() as session:
            school = session.scalar(text("SELECT school_id FROM school_database_anchor"))
            assert session.scalar(text("SELECT version_num FROM alembic_version")) in {
                "20260924_54", "20260924_55"
            }
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(test_f3_family_postgres, "KEY", metadata["context"].encode())
            patch.setattr(test_f3_writer_postgres, "KEY", metadata["context"].encode())
            scenario = WriterScenario(engines, school)
            scenario.complete_seed_staff_context()
            requester = scenario.identity()
            verifier = scenario.identity("f3-teacher-parent")
            key = scenario.marker + ":quality-family"
            with owner() as session:
                existing = session.execute(text(
                    "SELECT r.id FROM guardian_relationships r "
                    "JOIN school_people p ON p.school_id=r.school_id "
                    "AND p.id=r.guardian_person_id "
                    "JOIN students s ON s.school_id=r.school_id AND s.id=r.student_id "
                    "WHERE r.school_id=:school AND "
                    "p.source_key='person:f3-writer-safety-adult' "
                    "AND s.source_key='student:f3-writer-safety-wrong-child' "
                    "AND r.status='confirmed' AND r.revoked_at IS NULL "
                    "AND r.valid_from<=now() AND (r.valid_to IS NULL OR now()<r.valid_to)"
                ), {"school": school}).scalar_one_or_none()
            if existing is None:
                scenario.grant(
                    requester.staff(), kind="guardian_relationship", action="request",
                    creations=(key,),
                )
                content = {
                    "guardian_person_source_key": "person:f3-writer-safety-adult",
                    "student_source_key": "student:f3-writer-safety-wrong-child",
                    "relationship_kind": "verified_guardian",
                    "access_purpose": "guardian_student_profile",
                    "verification_source": "school_review",
                    "verification_reference": scenario.marker,
                    "status": "pending_verification",
                }
                scenario.stage(
                    requester, kind="guardian_relationship", key=key, content=content,
                ).confirm()
                scenario.grant(
                    verifier.staff(), kind="guardian_relationship", action="verify",
                    objects=(key,),
                )
                scenario.stage(
                    verifier, kind="guardian_relationship", key=key, expected=1,
                    content=content | {"status": "confirmed"},
                ).confirm()
        with owner() as session:
            adult = session.execute(text(
                "SELECT p.id AS adult, r.id AS relation, r.version, "
                "s.person_id AS child FROM guardian_relationships r "
                "JOIN school_people p ON p.school_id=r.school_id "
                "AND p.id=r.guardian_person_id "
                "JOIN students s ON s.school_id=r.school_id AND s.id=r.student_id "
                "WHERE r.school_id=:school AND "
                "p.source_key='person:f3-writer-safety-adult' "
                "AND s.source_key='student:f3-writer-safety-wrong-child' "
                "AND r.status='confirmed' AND r.revoked_at IS NULL "
                "AND r.valid_from<=now() AND (r.valid_to IS NULL OR now()<r.valid_to) "
                "ORDER BY r.version DESC LIMIT 1"
            ), {"school": school}).mappings().one()
            relation_key = session.scalar(text(
                "SELECT l.fact_source_key FROM school_fact_change_lineage l "
                "WHERE l.school_id=:school AND l.fact_kind='guardian_relationship' "
                "AND l.target_id=:relation AND l.target_version=:version "
                "AND l.operation IN ('upsert','reactivate','correct') "
                "ORDER BY l.created_at DESC LIMIT 1"
            ), {"school": school, "relation": adult["relation"],
                "version": adult["version"]})
            assert relation_key
        yield {
            "school": school,
            "adult": adult,
            "relation_key": relation_key,
            "scenario": scenario,
            "owner": owner,
            "reader": reader,
            "runtime": runtime,
        }
    finally:
        # Published-like quality fixture rows are immutable by design. Their
        # explicit short validity ends without deleting or rewriting history.
        for engine in engines.values():
            engine.dispose()


def _params(case, *, fields=("name",), at=None):
    adult = case["adult"]
    return {
        "school": case["school"], "adult": adult["adult"],
        "relation": adult["relation"], "version": adult["version"],
        "at": at, "fields": list(fields),
    }


def _add_quality(case, *, target_kind="person",
                 target_key="person:f3-writer-safety-wrong-child",
                 field="display_name", quality="source", source="unavailable",
                 coverage=None, dispute=None, review=None, batch_status="applied",
                 valid_from=None, valid_to=None, revoked_at=None):
    """Test-only exact row; no source value or quality source key goes to Reader output."""
    now = datetime.now(UTC)
    start = valid_from or now - timedelta(minutes=1)
    key = f"quality:f7:family-profile:{uuid4().hex}"
    row_id = uuid4()
    with case["owner"].begin() as session:
        batch = session.execute(text(
            "SELECT b.id, s.id AS source_id FROM school_fact_change_lineage l "
            "JOIN master_data_import_batches b ON b.school_id=l.school_id "
            "AND b.id=l.batch_id AND b.status=:status "
            "JOIN school_fact_sources s ON s.school_id=b.school_id "
            "AND s.source_key='change-set:'||b.id::text "
            "WHERE l.school_id=:school AND l.fact_kind=:kind "
            "AND l.fact_source_key=:key AND l.operation IN ('upsert','reactivate','correct') "
            "ORDER BY l.target_version DESC LIMIT 1"
        ), {
            "school": case["school"], "kind": target_kind,
            "key": target_key, "status": batch_status,
        }).mappings().one()
        session.execute(text(
            "INSERT INTO school_fact_qualities "
            "(id,school_id,source_key,version,valid_from,valid_to,revoked_at,"
            "source_id,import_batch_id,quality_kind,target_fact_kind,owner_kind,"
            "person_id,target_source_key,field_name,responsibility,basis_reference,"
            "verified_at,source_status,coverage_status,dispute_status,review_due_at,"
            "evidence_reference) VALUES "
            "(:id,:school,:key,1,:from,:to,:revoked,:source_id,:batch,:quality,"
            ":kind,'person',:child,:target,:field,'isolated test', 'isolated test',"
            ":verified,:source,:coverage,:dispute,:review,:evidence)"
        ), {
            "id": row_id, "school": case["school"], "key": key,
            "from": start,
            "to": valid_to or max(start, now) + timedelta(minutes=2),
            "revoked": revoked_at,
            "source_id": batch["source_id"], "batch": batch["id"],
            "quality": quality, "kind": target_kind,
            "child": case["adult"]["child"],
            "target": target_key, "field": field,
            "verified": now, "source": source,
            "coverage": coverage, "dispute": dispute, "review": review,
            "evidence": "isolated test" if quality == "dispute" else None,
        })
    return row_id


def _read(case, **options):
    with case["reader"]() as session:
        return session.scalar(READ_SQL, _params(case, **options))


def _ensure_nullable_residence_relationship(case):
    """Publish one V4 F3 enrollment with a legitimately unrecorded residence mode."""
    config = json.loads(Path(os.environ["F7_TEST_DATABASE_CONFIG"]).read_text(encoding="utf-8"))
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(test_f3_family_postgres, "KEY", config["context"].encode())
        patch.setattr(test_f3_writer_postgres, "KEY", config["context"].encode())
        return _publish_nullable_residence_relationship(case)


def _publish_nullable_residence_relationship(case):
    scenario = case["scenario"]
    with case["owner"]() as session:
        campus = session.scalar(text(
            "SELECT source_key FROM campus_locations WHERE school_id=:school "
            "ORDER BY source_key LIMIT 1"
        ), {"school": case["school"]})
        assert campus is not None
        existing = session.execute(text(
            "SELECT e.source_key,e.version,e.student_number,e.enrollment_status,"
            "e.residence_mode,"
            "p.source_key AS primary_key,a.source_key AS administrative_key "
            "FROM student_school_relationships e "
            "JOIN campus_locations p ON p.school_id=e.school_id "
            "AND p.id=e.primary_campus_location_id "
            "JOIN campus_locations a ON a.school_id=e.school_id "
            "AND a.id=e.administrative_campus_location_id "
            "WHERE e.school_id=:school "
            "AND student_id=(SELECT id FROM students WHERE school_id=:school "
            "AND source_key='student:f3-writer-safety-wrong-child') "
            "AND superseded_at IS NULL AND valid_from<=now() "
            "AND (valid_to IS NULL OR now()<valid_to)"
        ), {"school": case["school"]}).mappings().one_or_none()
    if existing and existing["residence_mode"] is None:
        return existing["source_key"]
    key = existing["source_key"] if existing else f"enrollment:f7:nullable-residence:{uuid4().hex}"
    expected = existing["version"] if existing else 0
    identity = scenario.identity()
    actor = identity.staff()
    scenario.grant(
        actor, kind="student_school_relationship", action="maintain",
        objects=(key,) if existing else (), creations=() if existing else (key,),
    )
    context = compose_command_context(actor, uuid4(), key)
    configuration = FamilyConfiguration(
        approval_reference="owned F7 PG only",
        scope_bindings=tuple(scenario.bindings.values()),
    )
    authority = create_family_authority_components(
        session_factory=scenario.sessions["reader"], configuration=configuration,
    )
    persistence = SqlAlchemySchoolFactPersistence(
        scenario.sessions["writer"], identity.envelope,
        confirmation_credential_issuer=HmacSchoolFactConfirmationCredentialIssuer(
            test_f3_writer_postgres.KEY
        ),
        final_authorization_guard=FamilyFinalAuthorizationGuard(authority.change_set_guard),
        family_actor_validator=authority.actor_validator,
    )
    service = SchoolFactChangeService(
        reader=persistence, writer=persistence,
        family_actor_validator=authority.actor_validator,
    )
    current = persistence.for_request(context).current_school_fact_version(case["school"])
    at = datetime.now(UTC)
    changes = CanonicalFactChangeSetV4.model_validate({
        "schema_version": 4,
        "source_sha256": sha256(key.encode()).hexdigest(),
        "base_version": current,
        "changes": ({
            "schema_version": 4, "operation": "upsert",
            "fact_kind": "student_school_relationship", "source_key": key,
            "expected_version": expected, "effective_at": at,
            "origin": {"sheet_code": "f7-nullable-profile", "row_number": 1},
            "content": {
                "student_source_key": "student:f3-writer-safety-wrong-child",
                "student_number": existing["student_number"] if existing else "F7-Q6-TEST",
                "enrollment_status": existing["enrollment_status"] if existing else "enrolled",
                "residence_mode": None,
                "primary_campus_source_key": existing["primary_key"] if existing else campus,
                "administrative_campus_source_key": (
                    existing["administrative_key"] if existing else campus
                ),
            },
        },),
    })
    report = service.preflight(
        school_id=case["school"], change_set=changes, context=context,
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
    return key


def test_current_verified_child_sees_only_matching_risks_and_no_internal_keys(case):
    _add_quality(case)
    _add_quality(case, field=None, quality="coverage", source=None, coverage="partial")
    _add_quality(case, quality="source", source="available")
    result = _read(case, fields=("name", "enrollment_status"))
    assert set(result) == {"school_fact_version", "claims"}
    assert result["school_fact_version"] >= 1
    assert {tuple(sorted(claim.items())) for claim in result["claims"]} >= {
        (("field_name", "name"), ("quality_kind", "source"),
         ("source_status", "unavailable")),
        (("coverage_status", "partial"), ("field_name", "name"),
         ("quality_kind", "coverage")),
    }
    assert "person:f3-writer-safety-wrong-child" not in json.dumps(result)
    assert "isolated test" not in json.dumps(result)


def test_other_child_prohibited_fields_and_non_reader_are_denied(case):
    _add_quality(case)
    before = _read(case)["claims"]
    # Existing formal but unrelated targets cannot be joined to this child's
    # visible name, even if a quality record was marked as this child's owner.
    _add_quality(case, target_key="person:f3-writer-safety-verifier")
    _add_quality(
        case, target_kind="guardian_relationship",
        target_key=case["relation_key"], field="status",
    )
    assert _read(case)["claims"] == before
    wrong = _params(case)
    wrong["relation"] = uuid4()
    with case["reader"]() as session:
        with pytest.raises(DBAPIError):
            session.scalar(READ_SQL, wrong)
    with case["owner"].begin() as session:
        session.execute(text(
            "UPDATE guardian_relationships SET relationship_kind='recognized_caregiver',"
            "access_purpose='caregiver_student_summary' "
            "WHERE school_id=:school AND id=:relation"
        ), {"school": case["school"], "relation": case["adult"]["relation"]})
    try:
        assert _read(case, fields=("name",))["claims"]
        with case["reader"]() as session:
            with pytest.raises(DBAPIError):
                session.scalar(READ_SQL, _params(case, fields=("region",)))
    finally:
        with case["owner"].begin() as session:
            session.execute(text(
                "UPDATE guardian_relationships SET relationship_kind='verified_guardian',"
                "access_purpose='guardian_student_profile' "
                "WHERE school_id=:school AND id=:relation"
            ), {"school": case["school"], "relation": case["adult"]["relation"]})
    with case["runtime"]() as session:
        with pytest.raises(DBAPIError):
            session.scalar(READ_SQL, _params(case))


def test_time_expiry_source_publication_and_relationship_recheck(case):
    baseline = _read(case)["claims"]
    _add_quality(case, valid_from=datetime.now(UTC) + timedelta(days=3650))
    assert _read(case)["claims"] == baseline
    _add_quality(case, revoked_at=datetime.now(UTC) - timedelta(seconds=1))
    assert _read(case)["claims"] == baseline
    with case["reader"]() as session:
        with pytest.raises(DBAPIError):
            session.scalar(READ_SQL, _params(case, at=datetime.now(UTC) + timedelta(days=1)))
    guardian = case["adult"]
    with case["owner"].begin() as session:
        session.execute(text(
            "UPDATE guardian_relationships SET revoked_at=now(),status='revoked' "
            "WHERE school_id=:school AND id=:relation"
        ), {"school": case["school"], "relation": guardian["relation"]})
    try:
        with case["reader"]() as session:
            with pytest.raises(DBAPIError):
                session.scalar(READ_SQL, _params(case))
    finally:
        with case["owner"].begin() as session:
            session.execute(text(
                "UPDATE guardian_relationships SET revoked_at=NULL,status='confirmed' "
                "WHERE school_id=:school AND id=:relation"
            ), {"school": case["school"], "relation": guardian["relation"]})


def test_later_target_publication_invalidates_old_risk_but_new_quality_version_one_applies(case):
    """Batch school version, never per-object row version, orders dependencies."""
    key = "person:f3-writer-safety-wrong-child"
    scenario = case["scenario"]
    with pytest.MonkeyPatch.context() as patch:
        config = json.loads(Path(os.environ["F7_TEST_DATABASE_CONFIG"]).read_text(encoding="utf-8"))
        patch.setattr(test_f3_family_postgres, "KEY", config["context"].encode())
        patch.setattr(test_f3_writer_postgres, "KEY", config["context"].encode())
        identity = scenario.identity()
        actor = identity.staff()
        scenario.grant(actor, kind="person", action="maintain", objects=(key,))
        with case["owner"]() as session:
            original_version = session.scalar(text(
                "SELECT version FROM school_people WHERE school_id=:school AND source_key=:key"
            ), {"school": case["school"], "key": key})
        old_label = f"隔离历史名{uuid4().hex[:8]}"
        scenario.stage(
            identity, kind="person", key=key, expected=original_version,
            content={
                "display_name": old_label,
                "person_kind": "other", "lifecycle_status": "active",
            },
        ).confirm()
        _add_quality(case, field="display_name", quality="dispute", source=None,
                     dispute="open")
        assert any(c["quality_kind"] == "dispute" for c in _read(case)["claims"])
        scenario.stage(
            identity, kind="person", key=key, expected=original_version + 1,
            content={
                "display_name": f"隔离新名{uuid4().hex[:8]}",
                "person_kind": "other", "lifecycle_status": "active",
            },
        ).confirm()
        assert not any(c["quality_kind"] == "dispute" for c in _read(case)["claims"])
        _add_quality(case, field="display_name", quality="dispute", source=None,
                     dispute="open")
        assert any(c["quality_kind"] == "dispute" for c in _read(case)["claims"])
        with case["owner"]() as session:
            current_version = session.scalar(text(
                "SELECT version FROM school_people WHERE school_id=:school AND source_key=:key"
            ), {"school": case["school"], "key": key})
        assert current_version >= 3  # quality rows still have independent version 1


def test_authorized_null_field_can_report_its_existing_target_source_failure(case):
    key = _ensure_nullable_residence_relationship(case)
    baseline = _read(case, fields=("residence_mode",))["claims"]
    _add_quality(
        case, target_kind="student_school_relationship", target_key=key,
        field="residence_mode",
    )
    result = _read(case, fields=("residence_mode",))
    assert {"field_name": "residence_mode", "quality_kind": "source",
            "source_status": "unavailable"} in result["claims"]
    assert len(result["claims"]) >= len(baseline)
    assert _read(case, fields=("region",))["claims"] == []
