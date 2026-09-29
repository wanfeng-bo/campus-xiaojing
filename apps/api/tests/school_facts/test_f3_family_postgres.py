"""Opt-in real PostgreSQL F3 reader/identity fixture; no bootstrap or global ACLs.

F3_TEST_DATABASE_CONFIG plus public target metadata identify the owned26 test database.
Owner-created identity/family prerequisites are explicit test fixtures. Fact batches
are published through the real isolated Canonical Writer and audited separately.
"""

from __future__ import annotations

import json
import os
import re
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from sqlalchemy import URL, create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from app.composition.school_fact_family_query import create_family_query_service
from app.composition.school_fact_persistence import SqlAlchemySchoolFactPersistence
from app.composition.school_fact_product_identity import (
    FAMILY_ENGINEERING_ACCOUNTS,
    ProductIdentityRejected,
    SchoolFactProductIdentityService,
)
from app.composition.school_facts import HmacSchoolFactConfirmationCredentialIssuer
from app.db.rls_context import set_local_execution_context
from app.domains.campus import models as campus_models  # noqa: F401
from app.domains.students import models as student_models  # noqa: F401
from app.platform.contracts import GuardianActorContext, StaffActorContext
from app.platform.identity.context import CurrentUser
from app.platform.identity.context_proof import ContextProofSigner
from app.platform.identity.execution_context import ActorContextResolver, compose_command_context
from app.platform.identity.session import IdentitySessionStore
from app.platform.school_facts.application import SchoolFactChangeService
from app.platform.school_facts.contracts import CanonicalFactChangeSetV1
from tests.school_facts.test_sf2_transaction import seed_staff
from tests.test_f1_product_identity import MemoryRedis

pytestmark = pytest.mark.skipif(
    not os.environ.get("F3_TEST_DATABASE_CONFIG"), reason="owned F3 PG config required"
)
KEY = b"f3-isolated-reader-test-context-key-32-bytes"


def _test_target_endpoint(values: dict) -> tuple[str, int, str]:
    """Optional public target metadata avoids another private credential copy."""
    metadata_path = os.environ.get("F3_TEST_TARGET_METADATA")
    if metadata_path:
        target = json.loads(Path(metadata_path).read_text(encoding="utf-8"))
        database = target["database"]
        if (
            target["container"] != "pydanticai-stage2-controls-postgres"
            or target["system_identifier"] != "7684478285531328547"
            or not isinstance(database, str)
            or re.fullmatch(r"f3_[0-9a-f]{32}_test", database) is None
        ):
            raise ValueError("fixed owned test target mismatch")
        if target.get("transport") == "control_private_network_tcp":
            return "192.168.96.2", 5432, database
        return "127.0.0.1", 55439, database
    return values.get("host", "postgres"), values.get("port", 5432), values["database"]


def safe_test_engine(config_path: Path, role: str) -> Engine:
    """Contain driver/config errors before they can expose private connection args."""
    names = {
        "migration": "campus_ai_migration",
        "runtime": "campus_ai_runtime",
        "reader": "campus_ai_school_fact_reader",
        "writer": "campus_ai_school_fact_writer",
    }
    name = names[role]

    def connect():
        __tracebackhide__ = True
        try:
            cfg = json.loads(config_path.read_text(encoding="utf-8"))
            host, port, database = _test_target_endpoint(cfg)
            connection = psycopg.connect(
                user=name,
                password=cfg[role],
                host=host,
                port=port,
                dbname=database,
                options="-c role=campus_ai_db_owner" if role == "migration" else "",
            )
        except Exception:
            pass
        else:
            return connection
        # Outside the except block: neither cause nor context retains driver args.
        raise RuntimeError("F3 test database connection failed")

    return create_engine(
        URL.create("postgresql+psycopg", username=name),
        creator=connect,
        poolclass=NullPool,
        hide_parameters=True,
    )


def public_test_database_settings(config_path: Path) -> dict[str, str]:
    __tracebackhide__ = True
    try:
        values = json.loads(config_path.read_text(encoding="utf-8"))
        database = _test_target_endpoint(values)[2]
        if not isinstance(database, str):
            raise ValueError("invalid database")
        return {"database": database}
    except Exception:
        pass
    raise RuntimeError("F3 test database configuration is invalid")


def test_real_f3_identity_reader_temporal_field_and_revocation_boundary(request):
    if os.environ.get("PYTEST_DISABLE_PLUGIN_AUTOLOAD") != "1" or request.config.getoption(
        "showlocals"
    ):
        pytest.fail("F3 PG tests require disabled plugin autoload and no showlocals", pytrace=False)
    config_path = Path(os.environ["F3_TEST_DATABASE_CONFIG"])
    cfg = public_test_database_settings(config_path)
    assert cfg["database"].startswith("f3_") and cfg["database"].endswith("_test")
    assert cfg["database"] not in {
        "campus_ai_school_facts_f1",
        "pydanticai_stage2_controls",
        "pydanticai_stage2_controls_test2",
    }
    engines = {}
    for role in ("migration", "runtime", "reader", "writer"):
        name = {
            "migration": "campus_ai_migration",
            "runtime": "campus_ai_runtime",
            "reader": "campus_ai_school_fact_reader",
            "writer": "campus_ai_school_fact_writer",
        }[role]
        engines[role] = safe_test_engine(config_path, role)
        with engines[role].connect() as c:
            session_user, current_user, db = c.execute(
                text("SELECT session_user,current_user,current_database()")
            ).one()
            assert session_user == name and db == cfg["database"]
            assert current_user == ("campus_ai_db_owner" if role == "migration" else name)
    owner = sessionmaker(engines["migration"], expire_on_commit=False)
    reader = sessionmaker(engines["reader"], expire_on_commit=False)
    runtime = sessionmaker(engines["runtime"], expire_on_commit=False)
    with owner() as s:
        assert s.scalar(text("SELECT version_num FROM alembic_version")) in {
            "20260913_26",
            "20260913_27",
        }
        existing = s.scalar(text("SELECT count(*) FROM schools"))
        if existing:
            assert existing == 1
            assert s.scalar(text("SELECT source_key FROM schools")) == "school:f3-pg-fixture"
            assert s.scalar(text("SELECT count(*) FROM school_people")) in {1, 8}
            assert s.scalar(text("SELECT count(*) FROM master_data_import_batches")) == 0
            school, user, requester, request_assignment = s.execute(
                text(
                    "SELECT a.school_id,b.account_user_id,a.person_id,a.id FROM "
                    "staff_assignments a "
                    "JOIN account_person_bindings b ON b.school_id=a.school_id AND "
                    "b.person_id=a.person_id "
                    "WHERE a.source_key NOT IN ('assignment:verifier','assignment:teacher-parent')"
                )
            ).one()
        else:
            assert s.scalar(text("SELECT count(*) FROM school_database_anchor")) == 0
    if not existing:
        school, user, requester, request_assignment = seed_staff(
            owner, school_source_key="school:f3-pg-fixture"
        )
    now = datetime.now(UTC)
    start = now - timedelta(days=2)
    end = now + timedelta(days=2)
    people = {
        k: uuid4()
        for k in ["guardian", "caregiver", "teacher-parent", "legacy", "verifier", "a", "b"]
    }
    students = {k: uuid4() for k in ["a", "b"]}
    relations = {
        k: uuid4()
        for k in ["guardian-a", "guardian-b", "caregiver-a", "teacher-parent-a", "legacy-a"]
    }
    source = uuid4()
    batch = uuid4()
    actors = {}
    assignments = {"verifier": uuid4(), "teacher-parent": uuid4()}
    from sqlalchemy import insert

    from app.db.base import Base

    def add(s, table, **values):
        s.execute(insert(Base.metadata.tables[table]).values(**values))

    with owner.begin() as s:
        if not s.scalar(text("SELECT count(*) FROM app_private.context_proof_keys")):
            s.execute(
                text(
                    "INSERT INTO app_private.context_proof_keys(key_version,signing_key,"
                    "fingerprint_sha256,is_current) VALUES (1,:key,:digest,true)"
                ),
                {"key": KEY, "digest": sha256(KEY).hexdigest()},
            )
        assert (
            s.scalar(
                text(
                    "SELECT fingerprint_sha256 FROM app_private.context_proof_keys WHERE is_current"
                )
            )
            == sha256(KEY).hexdigest()
        )
        user_row = s.execute(
            text(
                "SELECT id,external_subject,display_name,security_version FROM users WHERE id=:id"
            ),
            {"id": user},
        ).one()
    fixture_user = CurrentUser(*user_row)
    fixture_redis = MemoryRedis()
    fixture_sessions = IdentitySessionStore(redis=fixture_redis, ttl_seconds=900)
    fixture_token = fixture_sessions.create(fixture_user)
    fixture_identity = fixture_sessions.read(fixture_token)
    assert fixture_identity is not None
    fixture_resolver = ActorContextResolver(
        session_factory=runtime,
        redis=fixture_redis,
        proof_signer=ContextProofSigner(KEY),
        current_user=fixture_user,
        identity_session=fixture_identity,
    )
    fixture_options = fixture_resolver.list_options()
    assert len(fixture_options) == 1
    fixture_actor = fixture_resolver.resolve(fixture_options[0].opaque_ref)
    assert type(fixture_actor) is StaffActorContext
    fixture_command = compose_command_context(fixture_actor, uuid4(), "f3-reader-fixture")
    fixture_envelope = ContextProofSigner(KEY).sign_execution_context(
        fixture_command,
        session_nonce_hash=fixture_identity.session_nonce_hash,
        proof_expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    with owner() as s:
        reusable_auth_fixture = s.scalar(text("SELECT count(*) FROM school_people")) == 8
    if reusable_auth_fixture:
        with owner() as s:
            for name in people:
                people[name] = s.scalar(
                    text(
                        "SELECT id FROM school_people WHERE school_id=:school AND source_key=:key"
                    ),
                    {"school": school, "key": f"person:f3-{name}"},
                )
                assert people[name] is not None
            for name in students:
                students[name] = s.scalar(
                    text("SELECT id FROM students WHERE school_id=:school AND source_key=:key"),
                    {"school": school, "key": f"student:f3-{name}"},
                )
            for key in relations:
                adult, child = key.rsplit("-", 1)
                relations[key] = s.scalar(
                    text(
                        "SELECT id FROM guardian_relationships WHERE school_id=:school AND "
                        "guardian_person_id=:adult AND student_id=:child"
                    ),
                    {"school": school, "adult": people[adult], "child": students[child]},
                )
                assert relations[key] is not None
            for name in assignments:
                assignments[name] = s.scalar(
                    text(
                        "SELECT id FROM staff_assignments WHERE school_id=:school AND "
                        "source_key=:key"
                    ),
                    {"school": school, "key": "assignment:" + name},
                )
            for name in people.keys() - students.keys():
                row = s.execute(
                    text(
                        "SELECT u.id,u.external_subject,u.display_name,u.security_version FROM "
                        "users u JOIN account_person_bindings b ON b.account_user_id=u.id "
                        "WHERE b.school_id=:school AND b.person_id=:person"
                    ),
                    {"school": school, "person": people[name]},
                ).one()
                actors[name] = CurrentUser(*row)
            start, end, source = s.execute(
                text(
                    "SELECT valid_from,valid_to,source_id FROM guardian_relationships WHERE id=:id"
                ),
                {"id": relations["guardian-a"]},
            ).one()
    else:
        with owner.begin() as s:
            set_local_execution_context(s, fixture_envelope)
            add(
                s,
                "school_fact_sources",
                id=source,
                school_id=school,
                source_key=f"change-set:{batch}",
                source_kind="manual",
                source_ref="F3独立PG测试夹具：未走正式产品发布",
                schema_version=1,
            )
            org = s.scalar(
                text("SELECT organization_unit_id FROM staff_assignments WHERE id=:id"),
                {"id": request_assignment},
            )
            for name, pid in people.items():
                add(
                    s,
                    "school_people",
                    id=pid,
                    school_id=school,
                    source_key=f"person:f3-{name}",
                    status="active",
                    version=1,
                )
                label = "同名合成孩子" if name in students else "合成成人" + name
                add(
                    s,
                    "school_person_names",
                    id=uuid4(),
                    school_id=school,
                    person_id=pid,
                    display_name=label,
                    valid_from=start,
                    source_kind="manual",
                    source_ref="f3-test",
                    status="confirmed",
                    version=1,
                )
                add(
                    s,
                    "school_person_profile_versions",
                    id=uuid4(),
                    school_id=school,
                    person_id=pid,
                    person_kind="other" if name in students else "student_contact",
                    lifecycle_status="active",
                    valid_from=start,
                    source_id=source,
                    version=1,
                )
                if name in students:
                    add(
                        s,
                        "students",
                        id=students[name],
                        school_id=school,
                        person_id=pid,
                        source_key=f"student:f3-{name}",
                        status="active",
                        version=1,
                    )
                else:
                    subject = "f3-unverified-contact" if name == "legacy" else "f3-" + name
                    uid = s.execute(
                        text(
                            "INSERT INTO users(provider_code,provider_tenant_key,external_subject,"
                            "display_name,is_active,security_version) VALUES('local_test',"
                            "'development',:subject,'F3测试账号',true,1) RETURNING id"
                        ),
                        {"subject": subject},
                    ).scalar_one()
                    actors[name] = CurrentUser(
                        id=uid,
                        external_subject=subject,
                        display_name="F3测试账号",
                        security_version=1,
                    )
                    binding = uuid4()
                    add(
                        s,
                        "account_person_bindings",
                        id=binding,
                        school_id=school,
                        source_key="login:" + name,
                        account_user_id=uid,
                        person_id=pid,
                        valid_from=start,
                        status="confirmed",
                        version=1,
                    )
                    add(
                        s,
                        "account_person_binding_versions",
                        id=uuid4(),
                        school_id=school,
                        binding_id=binding,
                        account_user_id=uid,
                        person_id=pid,
                        provider_code="local_test",
                        provider_tenant_key="development",
                        external_subject=subject,
                        effective_from=start,
                        status="confirmed",
                        target_version=1,
                        source_ref="f3-test",
                        source_id=source,
                    )
            for name, assignment in assignments.items():
                v = dict(
                    school_id=school,
                    person_id=people[name],
                    organization_unit_id=org,
                    position_title="F3测试职工",
                    semantic_role_code="f3_test",
                    assignment_kind="substantive",
                    appointment_source="manual",
                    approval_reference="isolated-test-fixture",
                    status="confirmed",
                )
                add(
                    s,
                    "staff_assignments",
                    id=assignment,
                    source_key="assignment:" + name,
                    valid_from=start,
                    version=1,
                    **v,
                )
                add(
                    s,
                    "staff_assignment_versions",
                    id=uuid4(),
                    assignment_id=assignment,
                    effective_from=start,
                    target_version=1,
                    source_ref="f3-test",
                    source_id=source,
                    **v,
                )
            for key, rid in relations.items():
                adult, child = key.rsplit("-", 1)
                common = dict(
                    id=rid,
                    school_id=school,
                    guardian_person_id=people[adult],
                    student_id=students[child],
                    relationship_kind="recognized_caregiver"
                    if adult == "caregiver"
                    else "verified_guardian",
                    verification_source="manual",
                    verification_reference="isolated-test",
                    verified_at=start,
                    valid_from=start,
                    valid_to=end,
                    status="confirmed",
                    version=1,
                )
                if adult != "legacy":
                    common.update(
                        authority_version=1,
                        access_purpose="caregiver_student_summary"
                        if adult == "caregiver"
                        else "guardian_student_profile",
                        requested_by_person_id=requester,
                        requested_by_assignment_id=request_assignment,
                        requested_at=start,
                        verified_by_person_id=people["verifier"],
                        verified_by_assignment_id=assignments["verifier"],
                        source_id=source,
                    )
                add(s, "guardian_relationships", **common)
    persistence = SqlAlchemySchoolFactPersistence(
        sessionmaker(engines["writer"], expire_on_commit=False),
        lambda context: ContextProofSigner(KEY).sign_execution_context(
            context,
            session_nonce_hash=fixture_identity.session_nonce_hash,
            proof_expires_at=datetime.now(UTC) + timedelta(minutes=5),
        ),
        confirmation_credential_issuer=HmacSchoolFactConfirmationCredentialIssuer(KEY),
    )
    writer_service = SchoolFactChangeService(reader=persistence, writer=persistence)
    canonical = [
        (
            "person",
            "person:f3-a",
            1,
            {"display_name": "同名合成孩子", "person_kind": "other", "lifecycle_status": "active"},
        ),
        ("student", "student:f3-a", 1, {"person_source_key": "person:f3-a", "status": "active"}),
        (
            "location",
            "campus:f3-test",
            0,
            {
                "kind": "campus",
                "display_name": "合成测试校区",
                "display_path": "合成测试校区",
                "status": "active",
            },
        ),
        (
            "student_school_relationship",
            "enrollment:f3-a",
            0,
            {
                "student_source_key": "student:f3-a",
                "student_number": "F30001",
                "enrollment_status": "enrolled",
                "residence_mode": "day",
                "primary_campus_source_key": "campus:f3-test",
                "administrative_campus_source_key": "campus:f3-test",
            },
        ),
        (
            "person_region",
            "region:f3-a",
            0,
            {
                "person_source_key": "person:f3-a",
                "district_code": "330100",
                "district_name": "合成区县",
                "region_purpose": "student_profile",
            },
        ),
    ]
    changes = CanonicalFactChangeSetV1.model_validate(
        {
            "source_sha256": sha256(b"f3-real-writer-reader-fixture").hexdigest(),
            "base_version": 1,
            "changes": tuple(
                {
                    "operation": "upsert",
                    "fact_kind": kind,
                    "source_key": key,
                    "expected_version": expected,
                    "effective_at": start + timedelta(seconds=1),
                    "origin": {"sheet_code": "f3-real-writer", "row_number": i},
                    "content": content,
                }
                for i, (kind, key, expected, content) in enumerate(canonical, 1)
            ),
        }
    )
    prepared = writer_service.prepare_confirmation(
        change_set=changes,
        context=fixture_command,
        session_binding=fixture_identity.session_nonce_hash,
    )
    published = writer_service.confirm(
        credential=prepared.credential,
        change_set=changes,
        context=fixture_command,
        session_binding=fixture_identity.session_nonce_hash,
    )
    assert published.changed_count == 5
    with owner() as s:
        batch = published.batch_id
        source = s.scalar(
            text("SELECT id FROM school_fact_sources WHERE school_id=:school AND source_key=:key"),
            {"school": school, "key": f"change-set:{batch}"},
        )
        expected_trace = s.scalar(
            text("SELECT source_ref FROM school_fact_sources WHERE id=:id"), {"id": source}
        )
        assert (
            s.scalar(
                text(
                    "SELECT count(*) FROM audit_logs WHERE target_id=:id AND "
                    "event_type='school_facts.change_set.applied'"
                ),
                {"id": str(batch)},
            )
            == 1
        )
    with owner() as s:
        with pytest.raises(DBAPIError) as wrong_school:
            add(
                s,
                "person_regions",
                id=uuid4(),
                school_id=uuid4(),
                person_id=people["a"],
                district_code="synthetic",
                district_name="wrong school test",
                region_purpose="student_profile",
                valid_from=start,
                version=1,
                source_id=source,
            )
        assert getattr(wrong_school.value.orig, "sqlstate", None) == "42501"
        s.rollback()
    redis = MemoryRedis()
    accounts = tuple(
        a
        for a in FAMILY_ENGINEERING_ACCOUNTS
        if a.ref in {"guardian", "caregiver", "teacher-parent", "unverified-contact"}
    )

    def load(account):
        return next(v for v in actors.values() if v.external_subject == account.external_subject)

    service = SchoolFactProductIdentityService(
        app_env="test",
        redis=redis,
        accounts=accounts,
        load_account=load,
        resolver_factory=lambda u, i: ActorContextResolver(
            session_factory=runtime,
            redis=redis,
            proof_signer=ContextProofSigner(KEY),
            current_user=u,
            identity_session=i,
        ),
        family_enabled=True,
        can_maintain=lambda actor: type(actor) is StaffActorContext,
    )
    checks = []

    def checked(name):
        checks.append(name)

    token = service.login("guardian")
    view = service.session_view(token)
    assert len(view.contexts) == 2 and all("同名合成孩子" in c.label for c in view.contexts)
    first = next(c for c in view.contexts if "0001" in c.label)
    service.select_context(token, first.ref)
    identity = service.resolve_request(token)
    assert type(identity.actor) is GuardianActorContext
    query = create_family_query_service(
        session_factory=reader,
        current_actor=lambda actor: (
            service.resolve_private_actor(identity.actor_context_ref).actor == actor
        ),
    )
    result = query.query(actor=identity.actor)
    assert result.status == "available", result.public()
    assert result.profile["student_number"] == "F30001"
    region_value = result.profile["region"]
    assert isinstance(region_value, dict) and region_value["district_name"] == "合成区县"
    assert {s["fact"] for s in result.sources} >= {
        "person",
        "location",
        "student_school_relationship",
        "person_region",
    }
    assert all(s["source_name"] is None for s in result.sources)
    assert all(s["source_trace_reference"] == expected_trace for s in result.sources)
    assert query.query(actor=identity.actor, as_of=start - timedelta(seconds=1)).status == "denied"
    assert query.query(actor=identity.actor, as_of=start + timedelta(hours=1)).status == "available"
    checked("real_guardian_identity_fields_source_and_history")
    oldref = identity.actor_context_ref
    other = next(c for c in view.contexts if c.ref != first.ref)
    service.select_context(token, other.ref)
    with pytest.raises(ProductIdentityRejected):
        service.resolve_private_actor(oldref)
    checked("child_switch_invalidates_private_actor")
    caretoken = service.login("caregiver")
    service.select_context(caretoken, service.session_view(caretoken).contexts[0].ref)
    care = service.resolve_request(caretoken)
    carequery = create_family_query_service(
        session_factory=reader,
        current_actor=lambda actor: (
            service.resolve_private_actor(care.actor_context_ref).actor == actor
        ),
    )
    reduced = carequery.query(actor=care.actor)
    assert reduced.status == "available" and set(reduced.profile) == {
        "name",
        "enrollment_status",
        "campus",
    }
    assert carequery.query(actor=care.actor, as_of=now).status == "denied"
    checked("caregiver_sql_projection_and_history_denial")
    teachtoken = service.login("teacher-parent")
    teachview = service.session_view(teachtoken)
    assert len(teachview.contexts) == 2
    kinds = set()
    for choice in teachview.contexts:
        service.select_context(teachtoken, choice.ref)
        kinds.add(type(service.resolve_request(teachtoken).actor))
    assert kinds == {StaffActorContext, GuardianActorContext}
    legacy = service.login("unverified-contact")
    assert service.session_view(legacy).contexts == ()
    checked("teacher_parent_separate_and_legacy_no_authority")
    with reader() as s:
        row = s.execute(
            text(
                "SELECT app.school_fact_f3_staff_target(:school,'student','student:f3-a',"
                "false,:at,ARRAY['status'])"
            ),
            {"school": school, "at": now},
        ).scalar_one()
        assert row["content"] == {"status": "active"}
        assert row["valid_from"] != row["factual_at"] and row["source_published_at"] is not None
        assert row["source_name"] is None
        assert row["source_trace_reference"] == expected_trace
        assert (
            s.scalar(
                text(
                    "SELECT app.school_fact_f3_scope_is_current(:school,'student',"
                    "ARRAY['student:f3-a'],:at)"
                ),
                {"school": school, "at": now},
            )
            is True
        )
    checked("staff_sql_subset_real_validity_and_scope")
    for role in ("runtime", "writer"):
        with engines[role].connect() as connection:
            for statement in (
                "SELECT app.school_fact_current_version(:school)",
                "SELECT app.school_fact_f3_staff_target(:school,'student',"
                "'student:f3-a',false,now(),ARRAY['status'])",
            ):
                with pytest.raises(DBAPIError) as forbidden:
                    connection.execute(text(statement), {"school": school})
                assert getattr(forbidden.value.orig, "sqlstate", None) == "42501"
                connection.rollback()
    checked("fixed_reader_functions_deny_runtime_and_writer")
    for role in ("runtime", "reader"):
        with engines[role].connect() as c:
            with pytest.raises(DBAPIError):
                c.execute(text("SELECT id FROM guardian_relationships"))
            c.rollback()
            old = "app.list_execution_context_options_pre_f3(integer,bigint,text,timestamptz,text)"
            assert (
                c.scalar(
                    text("SELECT has_function_privilege(current_user,:signature,'EXECUTE')"),
                    {"signature": old},
                )
                is False
            )
    checked("base_table_and_legacy_function_closed")
    service.select_context(token, first.ref)
    fresh = service.resolve_request(token)
    with owner.begin() as s:
        s.execute(
            text(
                "UPDATE guardian_relationships SET status='revoked',revoked_at=:at,valid_to=:at "
                "WHERE id=:id"
            ),
            {"at": datetime.now(UTC), "id": relations["guardian-a"]},
        )
    with pytest.raises(ProductIdentityRejected):
        service.resolve_private_actor(fresh.actor_context_ref)
    with reader() as s:
        assert (
            s.execute(
                text(
                    "SELECT relationship_id FROM app.school_fact_family_authority(:school,"
                    ":adult,:relation,1)"
                ),
                {
                    "school": school,
                    "adult": people["guardian"],
                    "relation": relations["guardian-a"],
                },
            ).all()
            == []
        )
    checked("relation_revocation_rechecks_sql_and_private_actor")
    with reader() as s:
        missing = s.scalar(
            text(
                "SELECT app.school_fact_f3_staff_target(:school,'student',"
                "'student:never-published',false,:at,ARRAY['status'])"
            ),
            {"school": school, "at": datetime.now(UTC)},
        )
        assert missing["status"] == "no_current_records"
    checked("staff_missing_formal_record_is_explicit")
    evidence = {
        "database": cfg["database"],
        "school_id": str(school),
        "checks": checks,
        "authorization_fixture_not_product_publication": True,
        "redis": "MemoryRedis only; real Redis not covered",
        "writer_publication_verified": True,
        "writer_batch_id": str(published.batch_id),
        "writer_fact_kinds": [row[0] for row in canonical],
        "governance_final_guard_verified": False,
        "guardian_writer_lifecycle_verified": False,
    }
    Path(os.environ.get("F3_PG_EVIDENCE", "/tmp/f3-pg-evidence.json")).write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2)
    )
    for engine in engines.values():
        engine.dispose()


def existing_fixture_writer(engines):
    """Resolve the existing test Staff through real Identity; do not seed another root."""
    owner = sessionmaker(engines["migration"], expire_on_commit=False)
    runtime = sessionmaker(engines["runtime"], expire_on_commit=False)
    with owner() as session:
        row = session.execute(
            text(
                "SELECT u.id,u.external_subject,u.display_name,u.security_version "
                "FROM staff_assignments a JOIN account_person_bindings b "
                "ON b.school_id=a.school_id AND b.person_id=a.person_id "
                "JOIN users u ON u.id=b.account_user_id WHERE "
                "a.source_key NOT IN ('assignment:verifier','assignment:teacher-parent') "
                "AND a.source_key NOT LIKE 'assignment:f3-writer-safety-%'"
            )
        ).one()
    user = CurrentUser(*row)
    redis = MemoryRedis()
    sessions = IdentitySessionStore(redis=redis, ttl_seconds=900)
    identity = sessions.read(sessions.create(user))
    assert identity is not None
    resolver = ActorContextResolver(
        session_factory=runtime,
        redis=redis,
        proof_signer=ContextProofSigner(KEY),
        current_user=user,
        identity_session=identity,
    )
    options = resolver.list_options()
    assert len(options) == 1
    actor = resolver.resolve(options[0].opaque_ref)
    assert type(actor) is StaffActorContext
    context = compose_command_context(actor, uuid4(), "f3-candidates-fixture")
    persistence = SqlAlchemySchoolFactPersistence(
        sessionmaker(engines["writer"], expire_on_commit=False),
        lambda current: ContextProofSigner(KEY).sign_execution_context(
            current,
            session_nonce_hash=identity.session_nonce_hash,
            proof_expires_at=datetime.now(UTC) + timedelta(minutes=5),
        ),
        confirmation_credential_issuer=HmacSchoolFactConfirmationCredentialIssuer(KEY),
    )
    return actor, identity, context, SchoolFactChangeService(reader=persistence, writer=persistence)


@pytest.mark.parametrize("change", ["new_match", "future_start", "period_end", "revocation"])
def test_real_f3_reader_evidence_live_invalidation(request, change):
    """Real Reader reuse notices new matches/time/revocation without a version bump.

    Owner writes below create explicitly test-only prerequisites in the owned test DB.
    They are not product publications. Generated IDs are recorded and retained because
    deleting a relation requires an FK check against the Writer-owned correction table.
    No production or existing fixture row, Governance record, or schema is changed.
    """
    import time

    from sqlalchemy import insert, select

    from app.db.base import Base

    assert os.environ.get("PYTEST_DISABLE_PLUGIN_AUTOLOAD") == "1"
    assert not request.config.getoption("showlocals")
    assert os.environ.get("F3_TEST_TARGET_METADATA"), "fixed owned target metadata required"
    config = Path(os.environ["F3_TEST_DATABASE_CONFIG"])
    evidence_path = Path(os.environ["F3_PG_EVIDENCE"])
    expected_db = public_test_database_settings(config)["database"]
    engines = {
        role: safe_test_engine(config, role)
        for role in ("migration", "runtime", "reader", "writer")
    }
    owner = sessionmaker(engines["migration"], expire_on_commit=False)
    reader = sessionmaker(engines["reader"], expire_on_commit=False)
    person_id, student_id, relationship_id, name_id = (uuid4() for _ in range(4))
    tables = Base.metadata.tables
    relationship = tables["guardian_relationships"]
    school = None
    envelope = None
    fixture_committed = False
    assertions_passed = False
    try:
        with owner() as session:
            assert session.scalar(text("SELECT current_database()")) == expected_db
            assert session.scalar(text("SELECT version_num FROM alembic_version")) == "20260913_27"
            assert session.scalar(text("SELECT source_key FROM schools")) == "school:f3-pg-fixture"
            # Clone only the shape of an active synthetic prerequisite, not its authority ID.
            template = dict(
                session.execute(
                    select(relationship)
                    .join(
                        tables["school_people"],
                        tables["school_people"].c.id == relationship.c.guardian_person_id,
                    )
                    .where(
                        tables["school_people"].c.source_key == "person:f3-guardian",
                        relationship.c.status == "confirmed",
                        relationship.c.authority_version == 1,
                        relationship.c.revoked_at.is_(None),
                    )
                    .limit(1)
                )
                .mappings()
                .one()
            )
        staff, staff_identity, context, _ = existing_fixture_writer(engines)
        school = staff.school_id
        assert template["school_id"] == school
        envelope = ContextProofSigner(KEY).sign_execution_context(
            context,
            session_nonce_hash=staff_identity.session_nonce_hash,
            proof_expires_at=datetime.now(UTC) + timedelta(minutes=5),
        )
        now = datetime.now(UTC)
        with owner.begin() as session:
            set_local_execution_context(session, envelope)
            session.execute(
                insert(tables["school_people"]).values(
                    id=person_id,
                    school_id=school,
                    source_key=f"person:f3-temporal-{person_id}",
                    status="active",
                    version=1,
                )
            )
            session.execute(
                insert(tables["students"]).values(
                    id=student_id,
                    school_id=school,
                    person_id=person_id,
                    source_key=f"student:f3-temporal-{student_id}",
                    status="active",
                    version=1,
                )
            )
            session.execute(
                insert(relationship).values(
                    **(
                        template
                        | {
                            "id": relationship_id,
                            "student_id": student_id,
                            "valid_from": now - timedelta(hours=1),
                            "valid_to": now + timedelta(hours=1),
                            "version": 1,
                            "verification_reference": "F3 test-only fixture; not publication",
                            "review_of_relationship_id": None,
                            "import_batch_id": None,
                        }
                    )
                )
            )
            row = session.execute(
                text(
                    "SELECT u.id,u.external_subject,u.display_name,u.security_version "
                    "FROM users u JOIN account_person_bindings b ON b.account_user_id=u.id "
                    "WHERE b.school_id=:school AND b.person_id=:adult"
                ),
                {"school": school, "adult": template["guardian_person_id"]},
            ).one()
        fixture_committed = True
        user = CurrentUser(*row)
        redis = MemoryRedis()
        sessions = IdentitySessionStore(redis=redis, ttl_seconds=900)
        identity = sessions.read(sessions.create(user))
        assert identity is not None
        resolver = ActorContextResolver(
            session_factory=sessionmaker(engines["runtime"]),
            redis=redis,
            proof_signer=ContextProofSigner(KEY),
            current_user=user,
            identity_session=identity,
        )
        candidates = [
            resolver.resolve(option.opaque_ref)
            for option in resolver.list_options()
            if option.kind == "guardian_relationship"
        ]
        actor = next(
            candidate
            for candidate in candidates
            if type(candidate) is GuardianActorContext
            and candidate.guardian_relationship_id == relationship_id
        )
        # Current relationship is always revalidated by the real Reader authority SQL.
        service = create_family_query_service(
            session_factory=reader,
            current_actor=lambda candidate: candidate == actor,
        )

        def versions():
            with reader() as session:
                fact_version = session.scalar(
                    text("SELECT app.school_fact_current_version(:school)"), {"school": school}
                )
            with owner() as session:
                return fact_version, session.execute(
                    text(
                        "SELECT s.version,r.version FROM students s JOIN guardian_relationships r "
                        "ON r.school_id=s.school_id AND r.student_id=s.id WHERE r.id=:id"
                    ),
                    {"id": relationship_id},
                ).one()

        def add_name(valid_from, valid_to=None):
            with owner.begin() as session:
                set_local_execution_context(session, envelope)
                session.execute(
                    insert(tables["school_person_names"]).values(
                        id=name_id,
                        school_id=school,
                        person_id=person_id,
                        display_name="时效性合成孩子",
                        valid_from=valid_from,
                        valid_to=valid_to,
                        source_kind="manual",
                        source_ref="F3 test-only temporal fixture",
                        status="confirmed",
                        version=1,
                    )
                )

        unchanged_versions = versions()
        boundary = datetime.now(UTC) + timedelta(seconds=3)
        if change == "future_start":
            add_name(boundary)
        elif change == "period_end":
            add_name(datetime.now(UTC) - timedelta(minutes=1), boundary)
        elif change == "revocation":
            add_name(datetime.now(UTC) - timedelta(minutes=1))
        before = service.query(actor=actor)
        assert before.status == (
            "no_current_records" if change in {"new_match", "future_start"} else "available"
        )
        assert before.evidence is not None and before.evidence.valid(actor)
        if change == "new_match":
            add_name(datetime.now(UTC) - timedelta(seconds=1))
        elif change in {"future_start", "period_end"}:
            delay = (boundary - datetime.now(UTC)).total_seconds()
            assert 0 < delay <= 3, "initial read must precede the natural time boundary"
            time.sleep(delay + 0.05)
        else:
            with owner.begin() as session:
                set_local_execution_context(session, envelope)
                session.execute(
                    relationship.update()
                    .where(relationship.c.id == relationship_id)
                    .values(
                        status="revoked",
                        revoked_at=datetime.now(UTC),
                    )
                )
        assert versions() == unchanged_versions
        assert not before.evidence.valid(actor)
        after = service.query(actor=actor)
        assert (
            after.status
            == {
                "new_match": "available",
                "future_start": "available",
                "period_end": "no_current_records",
                "revocation": "denied",
            }[change]
        )
        if after.status == "available":
            assert after.profile["name"] == "时效性合成孩子"
        elif after.status == "denied":
            assert after.profile == {} and after.evidence is None
        else:
            assert all(value is None for value in after.profile.values())
        assertions_passed = True
    finally:
        try:
            evidence_path.with_name(f"reader-live-{change}-{person_id}.json").write_text(
                json.dumps(
                    {
                        "case": change,
                        "database": expected_db,
                        "school_id": str(school) if school is not None else None,
                        "generated_ids": {
                            "person": str(person_id),
                            "student": str(student_id),
                            "guardian_relationship": str(relationship_id),
                            "school_person_name": str(name_id),
                        },
                        "fixture_not_product_publication": True,
                        "retained": fixture_committed,
                        "assertions_passed": assertions_passed,
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
        finally:
            for engine in engines.values():
                engine.dispose()


def test_real_f3_formal_names_candidates_subset_and_bounded_batch(request):
    """Continue on the owned published fixture; no owner-made publication ledger."""
    from time import perf_counter

    if os.environ.get("PYTEST_DISABLE_PLUGIN_AUTOLOAD") != "1" or request.config.getoption(
        "showlocals"
    ):
        pytest.fail("F3 PG tests require disabled plugin autoload and no showlocals", pytrace=False)
    config = Path(os.environ["F3_TEST_DATABASE_CONFIG"])
    engines = {
        role: safe_test_engine(config, role)
        for role in ("migration", "runtime", "reader", "writer")
    }
    actor, identity, context, service = existing_fixture_writer(engines)
    before = datetime.now(UTC)
    marker = uuid4().hex[:10]
    # A failed assertion may follow a committed real publication. Resume its read checks.
    with engines["migration"].connect() as connection:
        prior = connection.execute(
            text(
                "SELECT l.fact_source_key,l.batch_id,n.valid_from FROM "
                "school_fact_change_lineage l "
                "JOIN master_data_import_batches b ON b.id=l.batch_id AND b.school_id=l.school_id "
                "JOIN school_person_names n ON n.person_id=l.target_id AND n.school_id=l.school_id "
                "AND n.version=l.target_version WHERE l.fact_kind='person' "
                "AND l.fact_source_key LIKE 'person:f3-candidate-%' AND b.status='applied' "
                "ORDER BY l.created_at DESC LIMIT 1"
            )
        ).first()
    if prior is not None:
        marker = prior.fact_source_key.split("-")[-2]
        before = prior.valid_from
    keys = [f"person:f3-candidate-{marker}-{index:03d}" for index in range(100)]
    name = "正式同名测试" + marker
    with engines["reader"].connect() as connection:
        base = connection.scalar(
            text("SELECT app.school_fact_current_version(:school)"), {"school": actor.school_id}
        )
    content = {"display_name": name, "person_kind": "other", "lifecycle_status": "active"}
    change_set = CanonicalFactChangeSetV1.model_validate(
        {
            "source_sha256": sha256(("candidates-" + marker).encode()).hexdigest(),
            "base_version": base,
            "changes": tuple(
                {
                    "operation": "upsert",
                    "fact_kind": "person",
                    "source_key": key,
                    "expected_version": 0,
                    "effective_at": before,
                    "origin": {"sheet_code": "f3-candidates", "row_number": index + 1},
                    "content": content,
                }
                for index, key in enumerate(keys)
            )
            + (
                {
                    "operation": "upsert",
                    "fact_kind": "person",
                    "source_key": "person:f3-a",
                    "expected_version": 2,
                    "effective_at": before,
                    "origin": {"sheet_code": "f3-candidates", "row_number": 101},
                    "content": {**content, "display_name": "正式更名测试" + marker},
                },
            ),
        }
    )
    if prior is None:
        prepared = service.prepare_confirmation(
            change_set=change_set, context=context, session_binding=identity.session_nonce_hash
        )
        published = service.confirm(
            credential=prepared.credential,
            change_set=change_set,
            context=context,
            session_binding=identity.session_nonce_hash,
        )
        batch_id = published.batch_id
        assert published.changed_count == 101
    else:
        batch_id = prior.batch_id
    with engines["migration"].connect() as connection:
        assert (
            connection.scalar(
                text("SELECT count(*) FROM school_fact_change_lineage WHERE batch_id=:batch"),
                {"batch": batch_id},
            )
            == 101
        )
    query = text(
        "SELECT source_key,label,payload FROM app.school_fact_f3_staff_candidates("
        ":school,:kind,CAST(:sources AS text[]),:at,CAST(:fields AS text[]),:field,:value,:limit)"
    )
    params = {
        "school": actor.school_id,
        "kind": "person",
        "sources": keys,
        "at": datetime.now(UTC),
        "fields": ["display_name"],
        "field": "display_name",
        "value": name,
        "limit": 21,
    }
    with engines["reader"].connect() as connection:
        started = perf_counter()
        rows = connection.execute(query, params).all()
        elapsed = perf_counter() - started
        assert len(rows) == 21 and {r.source_key for r in rows} <= set(keys)
        assert all(
            r.label == "人员 · " + name and r.payload["content"] == {"display_name": name}
            for r in rows
        )
        assert all(r.payload["source_reference"] == f"publication:{batch_id}" for r in rows)
        assert all(r.payload["source_name"] is None for r in rows)
        selected = connection.execute(query, {**params, "sources": keys[:2]}).all()
        assert {r.source_key for r in selected} == set(keys[:2])
        assert connection.execute(query, {**params, "fields": ["lifecycle_status"]}).all() == []
        assert connection.execute(query, {**params, "sources": keys + ["person:f3-a"]}).all() == []
        changed = connection.execute(
            query, {**params, "sources": ["person:f3-a"], "value": "正式更名测试" + marker}
        ).all()
        assert len(changed) == 1 and changed[0].payload["expected_version"] == 3
        assert (
            connection.execute(
                query, {**params, "sources": ["person:f3-a"], "value": "同名合成孩子"}
            ).all()
            == []
        )
        historical = connection.execute(
            query,
            {
                **params,
                "sources": ["person:f3-a"],
                "value": "同名合成孩子",
                "at": before - timedelta(seconds=1),
            },
        ).all()
        assert len(historical) == 1 and historical[0].payload["expected_version"] == 2
        related = connection.execute(
            query,
            {
                **params,
                "kind": "student",
                "sources": ["student:f3-a"],
                "fields": ["person_source_key"],
                "field": "person_source_key",
                "value": "person:f3-a",
            },
        ).all()
        assert len(related) == 1 and related[0].payload["content"] == {
            "person_source_key": "person:f3-a"
        }
        campus = connection.execute(
            query,
            {
                **params,
                "kind": "student_school_relationship",
                "sources": ["enrollment:f3-a"],
                "fields": ["primary_campus_source_key"],
                "field": "primary_campus_source_key",
                "value": "campus:f3-test",
            },
        ).all()
        assert len(campus) == 1 and campus[0].payload["content"] == {
            "primary_campus_source_key": "campus:f3-test"
        }
    for role in ("runtime", "writer"):
        with engines[role].connect() as connection:
            with pytest.raises(DBAPIError) as forbidden:
                connection.execute(query, params)
            assert getattr(forbidden.value.orig, "sqlstate", None) == "42501"
    evidence = {
        "database": public_test_database_settings(config)["database"],
        "writer_batch_id": str(batch_id),
        "writer_changed_count": 101,
        "source_count": 100,
        "returned_limit_plus_one": 21,
        "elapsed_seconds": elapsed,
        "checks": [
            "formal_rename_current_and_history",
            "same_name_all_candidates_bounded",
            "source_and_field_subset",
            "person_to_student_relation",
            "campus_relation",
            "no_internal_trace_as_source_name",
            "native_role_denials",
        ],
        "timing_is_observation_not_product_gate": True,
        "governance_guard_not_covered_here": True,
    }
    output = Path(os.environ.get("F3_PG_EVIDENCE", "/tmp/f3-pg-evidence.json")).with_name(
        "staff-candidates-result.json"
    )
    output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2))
    for engine in engines.values():
        engine.dispose()
