"""Isolated PostgreSQL transaction proof; fixed test permits are NOT governance proof.

The HTTP/owner tests cover current authorization. This file independently checks
the real Writer, school lock, stored package selection and cross-owner rollback.
"""

import json
import os
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import uuid4

import pytest
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from app.composition.school_fact_directory_services import create_directory_read_components
from app.composition.school_fact_directory_settings import (
    DirectoryConfiguration,
    DirectoryScopeBinding,
)
from app.composition.school_fact_persistence import SqlAlchemySchoolFactPersistence
from app.composition.school_fact_reader import SchoolFactReaderSettings
from app.composition.school_facts import HmacSchoolFactConfirmationCredentialIssuer
from app.platform.identity.context_proof import ContextProofSigner
from app.platform.identity.tuple_validator import SqlAlchemyIdentityTupleValidator
from app.platform.master_data_imports.school_facts_package import (
    FULL_SCOPE_JSON_MAPPING_SHA256,
    FullScopeInputManifestV5,
    FullScopePackageBuilder,
    FullScopeSourcePart,
    FullScopeSourceV5,
    parse_full_scope_fact_json_source,
)
from app.platform.master_data_imports.school_facts_replacement import ReplacementFactSnapshot
from app.platform.school_facts.application import SchoolFactChangeRejected, SchoolFactChangeService
from app.platform.school_facts.f2_policy import F2FactScope
from app.platform.school_facts.ports import (
    SchoolFactChangeProvenance,
    SchoolFactPolicyTextAttachment,
)
from tests.school_facts import test_f3_family_postgres, test_f3_writer_postgres
from tests.school_facts.test_f3_writer_postgres import WriterScenario
from tests.school_facts.test_f7_large_own_receipt_postgres import _owned_engines
from tests.school_facts.test_sf2_transaction import context_for

pytestmark = pytest.mark.skipif(
    not os.environ.get("F7_TEST_DATABASE_CONFIG"), reason="owned F7 PG config required"
)


def _grant_isolated_policy_version_reader(engines, school, actor, source_key):
    """A one-object *test* grant; never evidence of original-instance authority."""
    scope = F2FactScope(school, "policy_version", (source_key,), ())
    scope_id, grant_id = uuid4(), uuid4()
    start = datetime.now(UTC) - timedelta(seconds=1)
    with engines["migration"].begin() as conn:
        duty = conn.execute(text(
            "SELECT id,current_version FROM governance_duties WHERE school_id=:school AND "
            "duty_code='f2_policy_version_reader'"
        ), {"school": school}).one_or_none()
        if duty is None:
            duty_id, duty_version = uuid4(), 1
            conn.execute(text(
                "INSERT INTO governance_duties (id,school_id,duty_code,current_version) "
                "VALUES (:id,:school,'f2_policy_version_reader',1)"
            ), {"id": duty_id, "school": school})
            conn.execute(text(
                "INSERT INTO governance_duty_versions "
                "(id,school_id,duty_id,display_name,description,is_active,effective_from,version) "
                "VALUES (:version_id,:school,:id,'隔离制度读取','TEST FIXTURE ONLY',true,:at,1)"
            ), {"version_id": uuid4(), "school": school, "id": duty_id, "at": start})
        else:
            duty_id, duty_version = duty
            assert conn.scalar(text(
                "SELECT is_active FROM governance_duty_versions "
                "WHERE school_id=:school AND duty_id=:duty AND version=:version "
                "AND effective_from<=:at"
            ), {
                "school": school, "duty": duty_id,
                "version": duty_version, "at": datetime.now(UTC),
            }) is True, "Isolated policy-reader duty is not currently active"
        conn.execute(text(
            "INSERT INTO governance_scopes (id,school_id,scope_key,current_version) "
            "VALUES (:id,:school,:key,1)"
        ), {"id": scope_id, "school": school, "key": "f7-policy-test:" + scope_id.hex})
        conn.execute(text(
            "INSERT INTO governance_scope_versions "
            "(id,school_id,scope_id,schema_owner,schema_name,schema_version,"
            "reference_document,reference_sha256,is_active,effective_from,version) "
            "VALUES (:version_id,:school,:id,'school_facts.f2','exact_fact_set',1,"
            "CAST(:document AS jsonb),:digest,true,:at,1)"
        ), {
            "version_id": uuid4(), "school": school, "id": scope_id,
            "document": json.dumps(scope.document),
            "digest": scope.document_sha256, "at": start,
        })
        conn.execute(text(
            "INSERT INTO governance_grants "
            "(id,school_id,assignment_id,duty_id,current_version) "
            "VALUES (:id,:school,:assignment,:duty,1)"
        ), {
            "id": grant_id, "school": school,
            "assignment": actor.assignment_id, "duty": duty_id,
        })
        conn.execute(text(
            "INSERT INTO governance_grant_versions "
            "(id,school_id,grant_id,duty_id,duty_version,scope_id,scope_version,"
            "basis_authority,basis_reference,valid_from,effective_from,"
            "planned_valid_to,transition_kind,version) "
            "VALUES (:version_id,:school,:grant,:duty,:duty_version,:scope,1,"
            "'isolated_test_fixture',"
            "'f7-policy-source-roundtrip',:at,:at,:until,'create',1)"
        ), {
            "version_id": uuid4(),
            "school": school, "grant": grant_id, "duty": duty_id,
            "duty_version": duty_version,
            "scope": scope_id, "at": start, "until": start + timedelta(hours=1),
        })
        conn.execute(text(
            "INSERT INTO governance_assignment_epochs(school_id,assignment_id,epoch) "
            "VALUES (:school,:assignment,1) ON CONFLICT(school_id,assignment_id) "
            "DO UPDATE SET epoch=governance_assignment_epochs.epoch+1"
        ), {"school": school, "assignment": actor.assignment_id})
    return scope_id


def test_long_policy_package_writer_and_authorized_reader_keep_entire_original(monkeypatch):
    with _owned_engines() as (config, engines, anchor, staff):
        monkeypatch.setattr(test_f3_family_postgres, "KEY", config["context"].encode())
        monkeypatch.setattr(test_f3_writer_postgres, "KEY", config["context"].encode())
        scenario = WriterScenario(engines, anchor.school_id)
        scenario.complete_seed_staff_context()
        with engines["migration"].connect() as conn:
            formal_staff = conn.execute(text(
                "SELECT a.id,a.person_id,b.account_user_id FROM users u "
                "JOIN account_person_bindings b ON b.account_user_id=u.id "
                "AND b.school_id=:school JOIN staff_assignments a "
                "ON a.school_id=b.school_id AND a.person_id=b.person_id "
                "WHERE u.provider_code='local_test' "
                "AND u.provider_tenant_key='development' "
                "AND u.external_subject='f3-writer-safety-requester' "
                "ORDER BY a.created_at DESC LIMIT 1"
            ), {"school": anchor.school_id}).one()
        execution = context_for(
            school_id=anchor.school_id, user_id=formal_staff.account_user_id,
            person_id=formal_staff.person_id, assignment_id=formal_staff.id,
            key="f7-policy:" + uuid4().hex,
        )
        actor = execution.actor
        marker, at = uuid4().hex, datetime.now(UTC)
        policy_key = "f7:policy:" + marker
        version_key = "f7:policy-version:" + marker
        original = "合成制度原文。\n" * 10_000
        assert 32_767 < len(original) <= 100_000
        attachment = original.encode("utf-8")
        changes = [
            {
                "fact_kind": "policy", "source_key": policy_key,
                "operation": "upsert", "expected_version": 0,
                "effective_at": at.isoformat(), "content": {},
            },
            {
                "fact_kind": "policy_version", "source_key": version_key,
                "operation": "upsert", "expected_version": 0,
                "effective_at": at.isoformat(),
                "content": {
                    "policy_source_key": policy_key, "business_code": "F7-LONG-" + marker[:8],
                    "display_name": "隔离长制度", "category_code": "general",
                    "classification": "public", "published_at": at.isoformat(),
                    "effective_from": at.isoformat(), "content_text": original,
                },
            },
        ]
        raw = json.dumps(
            {"input_version": "school_facts_source_v5", "changes": changes},
            ensure_ascii=False,
        ).encode("utf-8")
        with engines["reader"].connect() as conn:
            base = conn.scalar(text("SELECT app.school_fact_current_version(:school)"), {
                "school": anchor.school_id,
            })
        manifest = FullScopeInputManifestV5(
            input_version="school_facts_v5", school_id=anchor.school_id,
            base_version=base, mode="incremental",
            sources=(
                FullScopeSourceV5(
                    logical_source_code="policy_rows", source_sha256=sha256(raw).hexdigest(),
                    size_bytes=len(raw), mapping_version="school_facts_source_v5",
                    mapping_config_sha256=FULL_SCOPE_JSON_MAPPING_SHA256,
                    responsibility_reference="isolated formal package fixture",
                    fact_kinds=("policy", "policy_version"),
                ),
                FullScopeSourceV5(
                    logical_source_code="policy_original",
                    source_sha256=sha256(attachment).hexdigest(),
                    size_bytes=len(attachment), mapping_version="f7_text",
                    mapping_config_sha256=sha256(b"utf8-text").hexdigest(),
                    responsibility_reference="isolated original text fixture",
                    purpose="policy_text", attached_policy_source_key=version_key,
                    fact_kinds=(),
                ),
            ),
        )
        parsed = parse_full_scope_fact_json_source(manifest.sources[0], raw)
        plan = FullScopePackageBuilder().build(
            manifest=manifest,
            parts=(parsed, FullScopeSourcePart("policy_original", attachment, (), ())),
        )
        assert plan.change_set.changes[1].content.content_text == original
        provenance = SchoolFactChangeProvenance(
            channel="package", raw_source_sha256=plan.raw_source_sha256,
            template_version=plan.input_version, mapping_version="f7_v1",
            mapping_config_sha256=FULL_SCOPE_JSON_MAPPING_SHA256,
            source_size_bytes=sum(s.size_bytes for s in plan.source_manifest),
            worksheet_names=(), source_manifest=plan.source_manifest,
            row_lineage=plan.row_lineage,
            policy_attachments=tuple(
                SchoolFactPolicyTextAttachment(
                    source_slot=item.source_slot,
                    source_sha256=item.source_sha256,
                    fact_source_key=item.fact_source_key,
                    field_code=item.field_code,
                )
                for item in plan.attachment_bindings
            ),
        )

        class ExactFixturePermit:
            def __call__(self, session, context, change_set):
                assert context.actor.school_id == anchor.school_id
                assert {(c.fact_kind, c.source_key) for c in change_set.changes} == {
                    ("policy", policy_key), ("policy_version", version_key),
                }

        signer = ContextProofSigner(SecretStr(config["context"]))
        persistence = SqlAlchemySchoolFactPersistence(
            sessionmaker(engines["writer"], expire_on_commit=False),
            lambda ctx: signer.sign_execution_context(
                ctx, session_nonce_hash=sha256(b"f7-policy-fixture").hexdigest(),
                proof_expires_at=datetime.now(UTC) + timedelta(minutes=5),
            ),
            confirmation_credential_issuer=HmacSchoolFactConfirmationCredentialIssuer(
                b"f7-policy-confirmation-test-key-32"
            ),
            identity_tuple_validator=SqlAlchemyIdentityTupleValidator(
                school_fact_command_wrapper=True
            ),
            package_final_authorization_guard=ExactFixturePermit(),
        )
        service = SchoolFactChangeService(reader=persistence, writer=persistence)
        ready = service.prepare_confirmation(
            change_set=plan.change_set, context=execution,
            session_binding="f7-policy-fixture", provenance=provenance,
        )
        receipt = service.confirm(
            credential=ready.credential, change_set=plan.change_set,
            context=execution, session_binding="f7-policy-fixture",
        )
        assert receipt.changed_count == 2

        scope_id = _grant_isolated_policy_version_reader(
            engines, anchor.school_id, actor, version_key
        )
        settings = SchoolFactReaderSettings(
            schema_contract=config["_schema_contract"],
            database_host=config["container"], database_port=config["port"],
            database_name=config["database"],
            database_user="campus_ai_school_fact_reader",
            database_password=config["reader"], expected_anchor_id=anchor.anchor_id,
            expected_school_id=anchor.school_id,
            expected_system_identifier=config["system_identifier"],
            cursor_hmac_key=config["context"],
        )
        components = create_directory_read_components(
            sessions=sessionmaker(engines["reader"], expire_on_commit=False),
            settings=settings,
            configuration=DirectoryConfiguration(
                approval_reference="isolated long-policy read only",
                scope_bindings=(DirectoryScopeBinding(
                    assignment_id=actor.assignment_id,
                    operation_code="school_facts.f2.policy_version.read",
                    scope_id=scope_id,
                ),),
                source_catalog={"policy_version": (version_key,)},
            ),
            legacy_scope_id=uuid4(), reference_key=config["context"].encode(),
        )
        reader = components.query
        reference = reader.reference_for(
            actor=actor, fact_kind="policy_version", source_key=version_key
        )
        result = reader.query(
            actor=actor, fact_kind="policy_version", reference=reference,
            content_fields=("content_text",),
        )
        assert result.status == "known" and len(result.records) == 1
        assert result.records[0].content["content_text"] == original


@pytest.mark.parametrize("condition", ["success", "late_failure", "lost_scope", "stale"])
def test_five_family_single_package_is_atomic_under_real_writer(monkeypatch, condition):
    """One transaction across F2–F6; owner-specific read grants have separate PG proofs.

    The exact fixture permit only isolates the shared Writer transaction. It is not
    evidence that this test identity has five production governance duties.
    """
    with _owned_engines() as (config, engines, anchor, _staff):
        monkeypatch.setattr(test_f3_family_postgres, "KEY", config["context"].encode())
        monkeypatch.setattr(test_f3_writer_postgres, "KEY", config["context"].encode())
        scenario = WriterScenario(engines, anchor.school_id)
        scenario.complete_seed_staff_context()
        with engines["migration"].connect() as conn:
            formal_staff = conn.execute(text(
                "SELECT a.id,a.person_id,b.account_user_id FROM users u "
                "JOIN account_person_bindings b ON b.account_user_id=u.id "
                "AND b.school_id=:school JOIN staff_assignments a "
                "ON a.school_id=b.school_id AND a.person_id=b.person_id "
                "WHERE u.provider_code='local_test' "
                "AND u.provider_tenant_key='development' "
                "AND u.external_subject='f3-writer-safety-requester' "
                "ORDER BY a.created_at DESC LIMIT 1"
            ), {"school": anchor.school_id}).one()
        marker, at = uuid4().hex, datetime.now(UTC)
        keys = {
            "person": "f7:mixed-person:" + marker,
            "campus": "f7:mixed-campus:" + marker,
            "location": "f7:mixed-building:" + marker,
            "student": "f7:mixed-student:" + marker,
            "education_stage": "f7:mixed-stage:" + marker,
            "teaching_course": "f7:mixed-course:" + marker,
            "dormitory_building_profile": "f7:mixed-dorm:" + marker,
        }
        bump_key = "f7:mixed-version-bump:" + marker
        expected_targets = {(kind, key) for kind, key in keys.items() if kind != "campus"}
        expected_targets.add(("location", keys["campus"]))
        state = {"permit": True, "fail_late": False}

        class ExactFixturePermit:
            def __call__(self, session, context, change_set):
                assert context.actor.school_id == anchor.school_id
                targets = {(c.fact_kind, c.source_key) for c in change_set.changes}
                assert targets in (
                    expected_targets,
                    {("teaching_course", bump_key)},
                )
                if not state["permit"]:
                    raise SchoolFactChangeRejected("fixture scope was removed")

        class FailureWriter(SqlAlchemySchoolFactPersistence):
            @staticmethod
            def _raise_db_error(exc):
                sqlstate = getattr(exc.orig, "sqlstate", None)
                constraint = getattr(getattr(exc.orig, "diag", None), "constraint_name", None)
                raise AssertionError(f"isolated DB failure {sqlstate} {constraint}") from None

            def _apply_change(self, *args, **kwargs):
                if (state["fail_late"] and
                    kwargs["change"].fact_kind == "dormitory_building_profile"):
                    raise SchoolFactChangeRejected("fixture final owner failed")
                return super()._apply_change(*args, **kwargs)

        signer = ContextProofSigner(SecretStr(config["context"]))
        persistence = FailureWriter(
            sessionmaker(engines["writer"], expire_on_commit=False),
            lambda ctx: signer.sign_execution_context(
                ctx, session_nonce_hash=sha256(b"f7-five-family-fixture").hexdigest(),
                proof_expires_at=datetime.now(UTC) + timedelta(minutes=5),
            ),
            confirmation_credential_issuer=HmacSchoolFactConfirmationCredentialIssuer(
                b"f7-five-family-confirmation-key-32"
            ),
            identity_tuple_validator=SqlAlchemyIdentityTupleValidator(
                school_fact_command_wrapper=True
            ),
            package_final_authorization_guard=ExactFixturePermit(),
        )
        service = SchoolFactChangeService(reader=persistence, writer=persistence)

        def package(changes, key):
            raw = json.dumps({
                "input_version": "school_facts_source_v5", "changes": changes,
            }, ensure_ascii=False).encode()
            with engines["reader"].connect() as conn:
                base = conn.scalar(text("SELECT app.school_fact_current_version(:school)"), {
                    "school": anchor.school_id,
                })
            manifest = FullScopeInputManifestV5.model_validate_json(json.dumps({
                "input_version": "school_facts_v5", "school_id": str(anchor.school_id),
                "base_version": base, "sources": [{
                    "logical_source_code": "five_family_rows",
                    "source_sha256": sha256(raw).hexdigest(), "size_bytes": len(raw),
                    "mapping_version": "school_facts_source_v5",
                    "mapping_config_sha256": FULL_SCOPE_JSON_MAPPING_SHA256,
                    "responsibility_reference": "isolated five-family transaction fixture",
                    "fact_kinds": sorted({change["fact_kind"] for change in changes}),
                }],
            }))
            parsed = parse_full_scope_fact_json_source(manifest.sources[0], raw)
            plan = FullScopePackageBuilder().build(manifest=manifest, parts=(parsed,))
            provenance = SchoolFactChangeProvenance(
                channel="package", raw_source_sha256=plan.raw_source_sha256,
                template_version=plan.input_version, mapping_version="f7_v1",
                mapping_config_sha256=FULL_SCOPE_JSON_MAPPING_SHA256,
                source_size_bytes=sum(s.size_bytes for s in plan.source_manifest),
                worksheet_names=(), source_manifest=plan.source_manifest,
                row_lineage=plan.row_lineage,
            )
            execution = context_for(
                school_id=anchor.school_id, user_id=formal_staff.account_user_id,
                person_id=formal_staff.person_id, assignment_id=formal_staff.id,
                key=key,
            )
            ready = service.prepare_confirmation(
                change_set=plan.change_set, context=execution,
                session_binding="f7-five-family-fixture", provenance=provenance,
            )
            return plan, execution, ready

        def confirm(staged):
            plan, execution, ready = staged
            return service.confirm(
                credential=ready.credential, change_set=plan.change_set,
                context=execution, session_binding="f7-five-family-fixture",
            )

        def row(kind, content, *, source=None):
            return {
                "fact_kind": kind, "source_key": source or keys[kind],
                "operation": "upsert", "expected_version": 0,
                "effective_at": at.isoformat(), "content": content,
            }

        changes = [
            row("person", {
                "display_name": "隔离合成学生", "person_kind": "other",
                "lifecycle_status": "active",
            }),
            row("location", {
                "kind": "campus", "display_name": "隔离合成校区",
                "display_path": "/隔离合成校区", "status": "active",
            }, source=keys["campus"]),
            row("location", {
                "parent_source_key": keys["campus"], "kind": "building",
                "display_name": "隔离住宿楼",
                "display_path": "/隔离合成校区/隔离住宿楼", "status": "active",
            }),
            row("student", {
                "person_source_key": keys["person"], "status": "active",
            }),
            row("education_stage", {
                "display_name": "隔离学段", "business_code": "F7-STAGE-" + marker[:8],
            }),
            row("teaching_course", {
                "display_name": "隔离课程", "business_code": "F7-COURSE-" + marker[:8],
            }),
            row("dormitory_building_profile", {
                "location_source_key": keys["location"], "usage_kind": "building",
                "display_name": "隔离住宿楼", "is_available": True,
            }),
        ]
        staged = package(changes, "f7-five-family:" + marker)
        if condition == "stale":
            confirm(package([
                row("teaching_course", {"display_name": "版本变更课程"}, source=bump_key)
            ], "f7-five-family-bump:" + marker))
        state["permit"] = condition != "lost_scope"
        state["fail_late"] = condition == "late_failure"
        if condition == "success":
            receipt = confirm(staged)
            assert receipt.changed_count == len(changes)
            assert confirm(staged).replayed
        else:
            with pytest.raises(SchoolFactChangeRejected):
                confirm(staged)
        with engines["migration"].connect() as conn:
            persisted = conn.execute(text(
                "SELECT fact_kind,fact_source_key FROM school_fact_change_lineage "
                "WHERE school_id=:school AND fact_source_key=ANY(:keys)"
            ), {"school": anchor.school_id, "keys": list(keys.values())}).all()
        assert set(persisted) == (expected_targets if condition == "success" else set())


@pytest.mark.parametrize(
    "condition",
    ["success", "stale", "revoked", "late_failure", "http_success", "http_late_failure"],
)
def test_package_mixed_owner_atomic_replace_uses_real_writer_and_original_receipt(condition):
    with _owned_engines() as (config, engines, anchor, staff):
        marker, now = uuid4().hex, datetime.now(UTC)
        keys = {
            "person": "f7-package-person:" + marker,
            "teaching_course": "f7-package-course:" + marker,
        }
        state = {"read": True, "fail": False}
        checked = []

        class ExactFixturePermit:
            def __call__(self, session, context, changes):
                assert context.actor.school_id == anchor.school_id
                assert all(keys.get(c.fact_kind) == c.source_key for c in changes.changes)

            def require_replacement(self, context, targets):
                assert context.actor.school_id == anchor.school_id
                assert {(t.fact_kind, t.source_key) for t in targets} == set(keys.items())
                checked.append("selected-current-read")
                if not state["read"]:
                    raise SchoolFactChangeRejected("selected read revoked")

        class FailureWriter(SqlAlchemySchoolFactPersistence):
            def _apply_change(self, *args, **kwargs):
                change = kwargs["change"]
                if state["fail"] and change.fact_kind == "teaching_course":
                    raise SchoolFactChangeRejected("deliberate second-owner failure")
                return super()._apply_change(*args, **kwargs)

        signer = ContextProofSigner(SecretStr(config["context"]))
        persistence = FailureWriter(
            sessionmaker(engines["writer"], expire_on_commit=False),
            lambda ctx: signer.sign_execution_context(
                ctx,
                session_nonce_hash=sha256(b"f7-package-fixture").hexdigest(),
                proof_expires_at=datetime.now(UTC) + timedelta(minutes=5),
            ),
            confirmation_credential_issuer=HmacSchoolFactConfirmationCredentialIssuer(
                b"f7-package-replacement-test-key-32"
            ),
            identity_tuple_validator=SqlAlchemyIdentityTupleValidator(
                school_fact_command_wrapper=True
            ),
            package_final_authorization_guard=ExactFixturePermit(),
        )
        service = SchoolFactChangeService(reader=persistence, writer=persistence)

        def base():
            with engines["reader"].connect() as conn:
                return conn.scalar(
                    text("SELECT app.school_fact_current_version(:school)"),
                    {"school": anchor.school_id},
                )

        def versions():
            with engines["migration"].connect() as conn:
                return dict(
                    conn.execute(
                        text(
                            "SELECT fact_source_key,max(target_version) "
                            "FROM school_fact_change_lineage WHERE school_id=:school "
                            "AND fact_source_key=ANY(:keys) GROUP BY fact_source_key"
                        ),
                        {"school": anchor.school_id, "keys": list(keys.values())},
                    ).all()
                )

        def stage(changes, *, targets=()):
            raw = json.dumps(
                {"input_version": "school_facts_source_v5", "changes": changes}, default=str
            ).encode()
            spec = {
                "input_version": "school_facts_v5",
                "school_id": str(anchor.school_id),
                "base_version": base(),
                "mode": "replace" if targets else "incremental",
                "replacement_scopes": [
                    {
                        "fact_kind": t.fact_kind,
                        "source_keys": [t.source_key],
                        "effective_at": t.effective_at.isoformat(),
                    }
                    for t in targets
                ],
                "sources": [
                    {
                        "logical_source_code": "synthetic_input",
                        "source_sha256": sha256(raw).hexdigest(),
                        "size_bytes": len(raw),
                        "mapping_version": "school_facts_source_v5",
                        "mapping_config_sha256": FULL_SCOPE_JSON_MAPPING_SHA256,
                        "responsibility_reference": "F7 owned transaction fixture",
                        "fact_kinds": list(keys),
                    }
                ],
            }
            manifest = FullScopeInputManifestV5.model_validate_json(json.dumps(spec))
            part = parse_full_scope_fact_json_source(manifest.sources[0], raw)
            plan = FullScopePackageBuilder().build(
                manifest=manifest, parts=(part,), replacement_targets=targets
            )
            provenance = SchoolFactChangeProvenance(
                channel="package",
                raw_source_sha256=plan.raw_source_sha256,
                template_version=plan.input_version,
                mapping_version="f7_v1",
                mapping_config_sha256=FULL_SCOPE_JSON_MAPPING_SHA256,
                source_size_bytes=sum(s.size_bytes for s in plan.source_manifest),
                worksheet_names=(),
                source_manifest=plan.source_manifest,
                row_lineage=plan.row_lineage,
                replacement_targets=targets,
            )
            context = context_for(
                school_id=anchor.school_id,
                user_id=staff.account_user_id,
                person_id=staff.person_id,
                assignment_id=staff.id,
                key="f7-package:" + uuid4().hex,
            )
            confirmation = service.prepare_confirmation(
                change_set=plan.change_set,
                context=context,
                session_binding="f7-package-fixture",
                provenance=provenance,
            )
            return plan, context, confirmation

        def confirm(staged):
            plan, context, confirmation = staged
            return service.confirm(
                credential=confirmation.credential,
                change_set=plan.change_set,
                context=context,
                session_binding="f7-package-fixture",
            )

        def row(kind, operation, version, content, at=now):
            return {
                "fact_kind": kind,
                "source_key": keys[kind],
                "operation": operation,
                "expected_version": version,
                "effective_at": at.isoformat(),
                "content": content,
            }

        initial = [
            row(
                "person",
                "upsert",
                0,
                {
                    "display_name": "合成包人员",
                    "person_kind": "other",
                    "lifecycle_status": "active",
                },
            ),
            row(
                "teaching_course",
                "upsert",
                0,
                {"display_name": "合成包课程", "basis_reference": "固定事务样本"},
            ),
        ]
        seeded = confirm(stage(initial))
        assert seeded.changed_count == 2
        if condition.startswith("http_"):
            _selected_http_round_trip(
                engines, anchor, staff, keys, state, service, condition, versions, base
            )
            return
        selected_at = datetime.now(UTC)
        old = versions()
        assert set(old) == set(keys.values())
        targets = tuple(
            ReplacementFactSnapshot(
                kind, key, selected_at, old[key], base(), "active", {}, lambda: None
            ).target()
            for kind, key in keys.items()
        )
        update = [
            row(
                "person",
                "upsert",
                old[keys["person"]],
                {
                    "display_name": "合成包修改",
                    "person_kind": "other",
                    "lifecycle_status": "active",
                },
                selected_at,
            ),
            row("teaching_course", "end", old[keys["teaching_course"]], None, selected_at),
        ]
        staged = stage(update, targets=targets)
        if condition == "stale":
            confirm(
                stage(
                    [
                        row(
                            "person",
                            "upsert",
                            old[keys["person"]],
                            initial[0]["content"],
                            datetime.now(UTC),
                        )
                    ]
                )
            )
        before = versions()
        state["read"] = condition != "revoked"
        state["fail"] = condition == "late_failure"
        if condition != "success":
            with pytest.raises(SchoolFactChangeRejected):
                confirm(staged)
            assert versions() == before
        else:
            applied = confirm(staged)
            assert applied.changed_count == 2
            assert versions() == {k: v + 1 for k, v in before.items()}
            replay = confirm(staged)
            assert replay.batch_id == applied.batch_id and replay.replayed
            assert versions() == {k: v + 1 for k, v in before.items()}
        if condition != "stale":
            assert checked == ["selected-current-read"]


def _selected_http_round_trip(
    engines, anchor, staff, keys, state, service, condition, versions, base
):
    """Production HTTP/owned builders/Writer with exact test permits, not governance grants.

    Reads below are fixture-owned, exact-coordinate SQL to supply actual persisted
    old values to production adapters. This does not replace separate formal
    Reader/RLS/duty verification or claim original-instance deployment evidence.
    """
    from types import SimpleNamespace

    from fastapi import FastAPI
    from fastapi.exceptions import RequestValidationError
    from fastapi.testclient import TestClient

    from app.composition.school_fact_directory_adapter import DirectoryProductAdapter
    from app.composition.school_fact_directory_product import FactChangeInput
    from app.composition.school_fact_education_maintenance import EducationMaintenanceCoordinator
    from app.composition.school_fact_education_product import (
        EducationBatchInput,
        EducationProductAdapter,
    )
    from app.composition.school_fact_f2_query_repository import F2MaintenanceTarget, F2PublicFact
    from app.composition.school_fact_package_replacement import PackageReplacementReader
    from app.composition.school_fact_package_sources import PackageDomainSourceAdapter
    from app.composition.school_facts import (
        InMemoryPrivateSchoolFactSourceStore,
        InMemorySchoolFactPlanStore,
    )
    from app.core.request_id import assign_request_id
    from app.interfaces.http.errors import ApiError
    from app.interfaces.http.framework_request import (
        api_error_handler,
        request_validation_error_handler,
    )
    from app.interfaces.http.school_facts import (
        SchoolFactRequestIdentity,
        create_school_fact_router,
    )

    actor = context_for(
        school_id=anchor.school_id,
        user_id=staff.account_user_id,
        person_id=staff.person_id,
        assignment_id=staff.id,
        key="f7-http",
    ).actor

    def allowed(current):
        assert current == actor
        if not state["read"]:
            raise PermissionError("exact test permission revoked")

    def snapshot(current, kind, key, at):
        allowed(current)
        assert keys.get(kind) == key
        before = base()
        with engines["migration"].connect() as conn:
            params = {"school": anchor.school_id, "key": key, "at": at}
            if kind == "person":
                row = (
                    conn.execute(
                        text(
                            "SELECT "
                            "p.version,n.display_name,n.valid_from,n.valid_to, "
                            "v.person_kind,v.lifecycle_status "
                            "FROM school_people p JOIN school_person_names n ON "
                            "n.school_id=p.school_id AND n.person_id=p.id "
                            "JOIN school_person_profile_versions v ON v.school_id=p.school_id "
                            "AND v.person_id=p.id "
                            "WHERE p.school_id=:school AND p.source_key=:key AND n.valid_from<=:at "
                            "AND (n.valid_to IS NULL OR n.valid_to>:at) AND v.valid_from<=:at "
                            "AND (v.valid_to IS NULL OR v.valid_to>:at)"
                        ),
                        params,
                    )
                    .mappings()
                    .one()
                )
                content = {k: row[k] for k in ("display_name", "person_kind", "lifecycle_status")}
                periods = (
                    {
                        "effective_at": row["valid_from"].isoformat(),
                        "valid_to": None,
                        "content": content,
                    },
                )
                version = row["version"]
            else:
                rows = (
                    conn.execute(
                        text(
                            "SELECT f.version,v.valid_from,v.valid_to,v.payload FROM "
                            "school_education_facts f "
                            "JOIN school_education_versions v ON v.school_id=f.school_id AND "
                            "v.fact_id=f.id "
                            "WHERE f.school_id=:school AND f.kind='teaching_course' AND "
                            "f.source_key=:key "
                            "AND v.superseded_at IS NULL ORDER BY v.valid_from"
                        ),
                        params,
                    )
                    .mappings()
                    .all()
                )
                periods = tuple(
                    {
                        "effective_at": r["valid_from"].isoformat(),
                        "valid_to": r["valid_to"].isoformat() if r["valid_to"] else None,
                        "content": r["payload"],
                    }
                    for r in rows
                )
                row = next(
                    r
                    for r in rows
                    if r["valid_from"] <= at and (r["valid_to"] is None or at < r["valid_to"])
                )
                content, version = row["payload"], row["version"]

        def proof():
            allowed(current)
            if base() != before:
                raise SchoolFactChangeRejected("fixture snapshot changed")

        proof()
        return ReplacementFactSnapshot(
            kind, key, at, version, before, "active", dict(content), proof, segments=periods
        )

    def target(**kw):
        allowed(kw["actor"])
        assert kw["target_ref"] == "f2:person:" + keys["person"] and kw["fact_kind"] == "person"
        old = snapshot(actor, "person", keys["person"], datetime.now(UTC))
        row = F2PublicFact(
            kw["target_ref"],
            "person",
            old.fields,
            old.expected_version,
            datetime.fromisoformat(old.segments[0]["effective_at"]),
            None,
            "manual",
            "published",
            datetime.fromisoformat(old.segments[0]["effective_at"]),
        )
        return F2MaintenanceTarget(
            keys["person"], old.expected_version, old.school_fact_version, old.fields, row
        )

    directory = DirectoryProductAdapter(
        SimpleNamespace(prepare_target=target, resolve_field_references=lambda **kw: kw["fields"])
    )

    class EducationRepository:
        def source_for_reference(self, *, reference, **kw):
            assert reference == "f4:teaching_course:" + keys["teaching_course"]
            return keys["teaching_course"]

        def reference_for(self, *, fact_kind, source_key, **kw):
            assert keys.get(fact_kind) == source_key
            return "f4:" + fact_kind + ":" + source_key

        def read_records(self, actor, kind, source_keys=None, **kw):
            assert kind == "teaching_course" and source_keys == (keys[kind],)
            old = snapshot(actor, kind, keys[kind], kw.get("as_of") or datetime.now(UTC))
            return SimpleNamespace(
                records=(
                    {
                        "source_key": old.source_key,
                        "version": old.expected_version,
                        "fact_kind": kind,
                        "segments": list(old.segments),
                        "status": "available",
                    },
                ),
                school_fact_version=old.school_fact_version,
                proof=SimpleNamespace(
                    revalidate=lambda **_: old.revalidate(),
                    school_fact_version=old.school_fact_version,
                ),
            )

    repo = EducationRepository()
    education = EducationMaintenanceCoordinator(
        EducationProductAdapter(repository=repo, external=repo), family=None
    )
    adapter = PackageDomainSourceAdapter(
        form_resolvers={
            "directory": (FactChangeInput, directory.resolve_change),
            "education": (EducationBatchInput, education.resolve_change),
        }
    )
    preview = PackageReplacementReader(
        partition=None,
        references=None,
        directory=None,
        family=None,
        education=None,
        residence=None,
        directory_adapter=None,
        family_adapter=None,
        residence_adapter=None,
    )
    preview.read = snapshot
    checked = []

    def require_package(identity, batch):
        allowed(identity.actor)
        assert {(c.fact_kind, c.source_key) for c in batch.changes} == set(keys.items())
        checked.append("exact-per-change")

    identity = SchoolFactRequestIdentity(
        actor=actor,
        session_binding="f7-http-selected",
        authorized_actions=frozenset({"school_facts.package.submit"}),
    )
    app = FastAPI()
    app.middleware("http")(assign_request_id)
    app.add_exception_handler(ApiError, api_error_handler)
    app.add_exception_handler(RequestValidationError, request_validation_error_handler)
    app.include_router(
        create_school_fact_router(
            change_service=service,
            source_store=InMemoryPrivateSchoolFactSourceStore(),
            plan_store=InMemorySchoolFactPlanStore(),
            identity_dependency=lambda: identity,
            enable_package=True,
            package_only=True,
            authorize_package=require_package,
            authorize_package_result=lambda i: lambda: allowed(i.actor),
            package_source_adapter=adapter,
            package_replacement=preview,
        )
    )
    prefix = "/api/v1/school-fact-product/imports"
    at = datetime.now(UTC).isoformat()
    import traceback
    from unittest.mock import patch

    from app.interfaces.http import school_facts as routes

    diagnostics = []
    original_error = routes._raise_channel_error

    def safe_error(exc):
        diagnostics.append(
            (
                type(exc).__name__,
                [(frame.name, frame.lineno) for frame in traceback.extract_tb(exc.__traceback__)],
            )
        )
        return original_error(exc)

    with patch.object(routes, "_raise_channel_error", safe_error), TestClient(app) as client:
        result = client.post(
            prefix + "/plans/selected",
            json={
                "items": [
                    {
                        "family": family,
                        "request": {
                            "fact_kind": kind,
                            "operation": "upsert",
                            "target_ref": ref,
                            "fields": {"display_name": "Verified " + kind},
                            "reason": "fixed selected package test",
                            "effective_at": at,
                        },
                    }
                    for family, kind, ref in (
                        ("directory", "person", "f2:person:" + keys["person"]),
                        (
                            "education",
                            "teaching_course",
                            "f4:teaching_course:" + keys["teaching_course"],
                        ),
                    )
                ]
            },
        )
        assert result.status_code == 200, (result.text, diagnostics)
        plan = result.json()
        assert plan["is_clean"] and plan["preview_status"] == "complete", plan["preview_issues"]
        assert len(plan["change_preview"]) == 2
        assert all(
            p["before"]["display_name"] != p["after"]["display_name"]
            for p in plan["change_preview"]
        )
        reference = plan["plan_ref"]
        prepared = client.post(
            prefix + f"/plans/{reference}/confirmation",
            headers={"Idempotency-Key": "f7-http-prepare"},
        )
        assert prepared.status_code == 200, prepared.text
        before = versions()
        state["fail"] = condition == "http_late_failure"
        applied = client.post(
            prefix + f"/plans/{reference}/confirm",
            json={"credential": prepared.json()["credential"]},
            headers={"Idempotency-Key": "f7-http-confirm"},
        )
        if state["fail"]:
            assert applied.status_code == 409, applied.text
            assert versions() == before
        else:
            assert applied.status_code == 200, applied.text
            assert applied.json()["changed_count"] == 2
            current = versions()
            assert current == {k: v + 1 for k, v in before.items()}
            receipt = client.get(prefix + f"/plans/{reference}/result")
            assert receipt.status_code == 200, receipt.text
            assert (
                receipt.json()["state"] == "applied"
                and receipt.json()["batch_id"] == applied.json()["batch_id"]
            )
            assert versions() == current
        assert len(checked) >= 5
