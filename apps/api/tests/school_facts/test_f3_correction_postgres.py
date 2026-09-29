"""Real schema27 controls on the existing owned F3 fixture; explicit opt-in only."""
# ruff: noqa: E501 - fixed, parameterized SQL kept readable as complete statements

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import sessionmaker

from app.composition.school_fact_family_correction_writer import (
    FamilyCorrectionReferenceCodec,
    SqlFamilyCorrectionRepository,
    SqlGuardianCorrectionAuthority,
)
from app.db.rls_context import set_local_execution_context
from app.domains.students.family_corrections import FamilyCorrectionDenied
from app.platform.contracts import GuardianActorContext
from app.platform.identity.context import CurrentUser
from app.platform.identity.context_proof import ContextProofSigner
from app.platform.identity.execution_context import ActorContextResolver, compose_command_context
from app.platform.identity.session import IdentitySessionStore
from tests.school_facts.test_f3_family_postgres import KEY, safe_test_engine
from tests.school_facts.test_f3_writer_postgres import scenario  # noqa: F401
from tests.test_f1_product_identity import MemoryRedis

pytestmark = pytest.mark.skipif(
    not os.environ.get("F3_TEST_DATABASE_CONFIG"), reason="owned F3 PG config required"
)


@pytest.fixture
def fixture(request):
    assert os.environ.get("PYTEST_DISABLE_PLUGIN_AUTOLOAD") == "1" and not request.config.getoption(
        "showlocals"
    )
    config = Path(os.environ["F3_TEST_DATABASE_CONFIG"])
    engines = {r: safe_test_engine(config, r) for r in ("migration", "runtime", "reader", "writer")}
    with engines["migration"].connect() as c:
        assert c.scalar(text("SELECT version_num FROM alembic_version")) == "20260913_27"
        assert c.scalar(text("SELECT source_key FROM schools")) == "school:f3-pg-fixture"
    yield engines
    for e in engines.values():
        e.dispose()


def guardian(engines, source="person:f3-teacher-parent"):
    with engines["migration"].connect() as c:
        row = c.execute(
            text(
                "SELECT u.id,u.external_subject,u.display_name,u.security_version FROM users u JOIN account_person_bindings b ON b.account_user_id=u.id JOIN school_people p ON p.id=b.person_id AND p.school_id=b.school_id WHERE p.source_key=:source"
            ),
            {"source": source},
        ).one()
    user = CurrentUser(*row)
    redis = MemoryRedis()
    store = IdentitySessionStore(redis=redis, ttl_seconds=900)
    identity = store.read(store.create(user))
    assert identity is not None
    resolver = ActorContextResolver(
        session_factory=sessionmaker(engines["runtime"]),
        redis=redis,
        proof_signer=ContextProofSigner(KEY),
        current_user=user,
        identity_session=identity,
    )
    options = [x for x in resolver.list_options() if x.kind == "guardian_relationship"]
    assert len(options) == 1
    actor = resolver.resolve(options[0].opaque_ref)
    assert type(actor) is GuardianActorContext
    return actor, identity


@contextmanager
def bound(engines, actor, identity):
    ctx = compose_command_context(actor, uuid4(), "f3-correction-real-" + uuid4().hex)
    envelope = ContextProofSigner(KEY).sign_execution_context(
        ctx,
        session_nonce_hash=identity.session_nonce_hash,
        proof_expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    with sessionmaker(engines["writer"])() as s:
        with s.begin():
            set_local_execution_context(s, envelope)
            yield s


def evidence(name, values):
    path = Path(os.environ["F3_PG_EVIDENCE"]).with_name("correction-controls-result.json")
    previous = json.loads(path.read_text()) if path.exists() else {}
    previous[name] = values
    path.write_text(json.dumps(previous, ensure_ascii=False, indent=2), encoding="utf-8")


def test_real_correction_acl_denies_nonwriter(fixture):
    engines = fixture
    functions = [
        "app.school_fact_family_correction_targets(uuid,uuid,uuid,bigint)",
        "app.school_fact_f3_field_changed(uuid,text,text,bigint,text)",
    ]
    for role in ("runtime", "reader", "migration"):
        with engines[role].connect() as c:
            for signature in functions:
                # Migration is explicitly checked as SESSION_USER, not inherited owner context.
                assert not c.scalar(
                    text("SELECT has_function_privilege(session_user,:f,'EXECUTE')"),
                    {"f": signature},
                )
            for verb in ("SELECT", "INSERT", "UPDATE"):
                assert not c.scalar(
                    text(
                        "SELECT has_table_privilege(session_user,'family_fact_correction_reports',:p)"
                    ),
                    {"p": verb},
                )
        if role != "migration":
            with engines[role].connect() as c:
                with pytest.raises(DBAPIError) as failure:
                    c.execute(
                        text("SELECT * FROM app.school_fact_family_correction_targets(:s,:a,:r,1)"),
                        {"s": uuid4(), "a": uuid4(), "r": uuid4()},
                    )
                assert failure.value.orig.sqlstate == "42501"
                c.rollback()
    with engines["writer"].connect() as c:
        assert all(
            c.scalar(text("SELECT has_function_privilege(session_user,:f,'EXECUTE')"), {"f": f})
            for f in functions
        )
        assert c.scalar(
            text(
                "SELECT has_table_privilege(session_user,'family_fact_correction_reports','SELECT,INSERT,UPDATE')"
            )
        )
    evidence(
        "acl",
        {"nonwriter_execute_and_table_privileges_denied": True, "writer_fixed_api_granted": True},
    )


def test_real_guardian_correction_options_submit_idempotency_and_rollback(fixture):
    actor, identity = guardian(fixture)
    codec = FamilyCorrectionReferenceCodec(KEY)
    with bound(fixture, actor, identity) as s:
        authority = SqlGuardianCorrectionAuthority(session=s, codec=codec)
        options = authority.options(actor)
        assert {x.field_name for x in options} == {
            "name",
            "enrollment_status",
            "campus",
            "student_number",
            "residence_mode",
            "region",
        }
        assert options == authority.options(actor)
        for adult, relation, version in [
            (uuid4(), actor.guardian_relationship_id, actor.relationship_version),
            (actor.person_id, uuid4(), actor.relationship_version),
            (actor.person_id, actor.guardian_relationship_id, actor.relationship_version + 1),
        ]:
            assert not s.execute(
                text("SELECT * FROM app.school_fact_family_correction_targets(:s,:a,:r,:v)"),
                {"s": actor.school_id, "a": adult, "r": relation, "v": version},
            ).all()
        target = next(x for x in options if x.field_name == "name")
        permit = authority.require_submit(
            actor=actor,
            target_reference=target.reference,
            field_name="name",
            checked_at=datetime.now(UTC),
        )
        assert permit.target_version >= 2
        repo = SqlFamilyCorrectionRepository(s)
        key = "f3-correction-" + uuid4().hex
        report = repo.submit(permit=permit, statement="合成姓名需要核对", idempotency_key=key)
        same = repo.submit(permit=permit, statement="合成姓名需要核对", idempotency_key=key)
        assert report.report_id == same.report_id
        with pytest.raises(FamilyCorrectionDenied):
            repo.submit(permit=permit, statement="另一项说明", idempotency_key=key)
        authority.revalidate(permit, checked_at=datetime.now(UTC))
        own = repo.own_by_key(actor=actor, idempotency_key=key)
        assert own and own.report_id == report.report_id and own.statement == ""
        assert any(
            x.report_id == report.report_id and x.statement == ""
            for x in repo.list_own(actor=actor, limit=20)
        )
    # Repository conditional update rollback is a transaction proof, not formal resolution authorization.
    with pytest.raises(RuntimeError, match="intentional_test_rollback"):
        with bound(fixture, actor, identity) as s:
            changed = SqlFamilyCorrectionRepository(s).resolve(
                report_id=report.report_id, locator_sha256="c" * 64, resolved_at=datetime.now(UTC)
            )
            assert changed.status == "resolved"
            with pytest.raises(FamilyCorrectionDenied):
                SqlFamilyCorrectionRepository(s).resolve(
                    report_id=report.report_id,
                    locator_sha256="c" * 64,
                    resolved_at=datetime.now(UTC),
                )
            raise RuntimeError("intentional_test_rollback")
    with bound(fixture, actor, identity) as s:
        current = SqlFamilyCorrectionRepository(s).own(actor=actor, report_id=report.report_id)
        assert current and current.status == "submitted"
    with fixture["writer"].connect() as c:
        assert c.scalar(text("SELECT current_setting('app.context_purpose',true)")) in (None, "")
    evidence(
        "guardian_submit",
        {
            "report_id": str(report.report_id),
            "target_version": permit.target_version,
            "same_key_same_report": True,
            "different_statement_denied": True,
            "resolution_rollback_preserved_submitted": True,
            "resolution_authority_not_claimed": True,
        },
    )


def test_real_existing_formal_person_difference_uses_prior_version(fixture):
    from hashlib import sha256

    from app.platform.school_facts.contracts import CanonicalFactChangeSetV1
    from tests.school_facts.test_f3_family_postgres import existing_fixture_writer

    actor, identity, context, service = existing_fixture_writer(fixture)
    with fixture["migration"].connect() as c:
        previous = c.scalar(
            text(
                "SELECT max(target_version) FROM school_fact_change_lineage WHERE fact_kind='person' AND fact_source_key='person:f3-a'"
            )
        )
    with fixture["reader"].connect() as c:
        base = c.scalar(text("SELECT app.school_fact_current_version(:s)"), {"s": actor.school_id})
    name = "正式差异更名" + uuid4().hex[:8]
    changes = CanonicalFactChangeSetV1.model_validate(
        {
            "source_sha256": sha256(name.encode()).hexdigest(),
            "base_version": base,
            "changes": (
                {
                    "operation": "upsert",
                    "fact_kind": "person",
                    "source_key": "person:f3-a",
                    "expected_version": previous,
                    "effective_at": datetime.now(UTC),
                    "origin": {"sheet_code": "f3-difference", "row_number": 1},
                    "content": {
                        "display_name": name,
                        "person_kind": "other",
                        "lifecycle_status": "active",
                    },
                },
            ),
        }
    )
    prepared = service.prepare_confirmation(
        change_set=changes, context=context, session_binding=identity.session_nonce_hash
    )
    published = service.confirm(
        credential=prepared.credential,
        change_set=changes,
        context=context,
        session_binding=identity.session_nonce_hash,
    )
    guardian_actor, guardian_session = guardian(fixture)
    with bound(fixture, guardian_actor, guardian_session) as s:
        assert (
            s.scalar(
                text(
                    "SELECT app.school_fact_f3_field_changed(:school,'person','person:f3-a',:v,'name')"
                ),
                {"school": actor.school_id, "v": previous + 1},
            )
            is True
        )
        assert (
            s.scalar(
                text(
                    "SELECT app.school_fact_f3_field_changed(:school,'person','person:f3-a',:v,'region')"
                ),
                {"school": actor.school_id, "v": previous + 1},
            )
            is False
        )
        assert (
            s.scalar(
                text(
                    "SELECT app.school_fact_f3_field_changed(:school,'student_school_relationship','enrollment:f3-a',1,'enrollment_status')"
                ),
                {"school": actor.school_id},
            )
            is False
        )
    evidence(
        "prior_person_difference",
        {
            "formal_person_prior_version": previous,
            "target_version": previous + 1,
            "batch_id": str(published.batch_id),
            "name_changed": True,
            "unrelated_field_false": True,
            "creation_not_change": True,
        },
    )


def test_real_correction_formal_resolution_and_current_revocation(scenario):  # noqa: F811
    """Real kernel, scoped authority, formal confirmation and original own receipt."""
    from hashlib import sha256

    from pydantic import SecretStr

    from app.composition.school_fact_family_authority import (
        F3OwnResultAuthorizer,
        create_family_authority_components,
    )
    from app.composition.school_fact_family_correction_writer import create_family_correction_kernel
    from app.composition.school_fact_family_execution import FamilyFinalAuthorizationGuard
    from app.composition.school_fact_family_settings import FamilyConfiguration
    from app.composition.school_fact_writer_executor import SqlAlchemySchoolFactWriterExecutorKernel
    from app.platform.school_facts.ports import SchoolFactChangeProvenance
    from tests.school_facts.test_f3_writer_postgres import canonical_changes
    from tests.school_facts.test_sf5b_writer_executor import _writer_settings

    staff_identity = scenario.identity()
    staff = staff_identity.staff()
    source = "person:f3-a"
    for action in ("maintain", "read"):
        scenario.grant(staff, kind="person", action=action, objects=(source,))
    configuration = FamilyConfiguration(
        approval_reference="owned-correction-real-fixture",
        scope_bindings=tuple(scenario.bindings.values()),
    )
    authority = create_family_authority_components(
        session_factory=scenario.sessions["reader"],
        configuration=configuration,
    )
    settings = _writer_settings().model_copy(
        update={
            "schema_contract": "school-facts-f3-v27",
            "expected_school_id": staff.school_id,
            "rls_context_signing_key": SecretStr(KEY.decode()),
            "confirmation_hmac_key": SecretStr(KEY.decode()),
        }
    )
    kernel = SqlAlchemySchoolFactWriterExecutorKernel(
        engine=scenario.engines["writer"],
        settings=settings,
        final_authorization_guard=FamilyFinalAuthorizationGuard(authority.change_set_guard),
        family_actor_validator=authority.actor_validator,
        own_result_authorizer=F3OwnResultAuthorizer(
            thin=authority.thin,
            configuration=configuration,
            clock=lambda: datetime.now(UTC),
        ),
    )
    corrections = create_family_correction_kernel(
        writer_kernel=kernel,
        family_authority=authority,
        reference_key=KEY,
        clock=lambda: datetime.now(UTC),
    )
    parent, parent_session = guardian(scenario.engines)
    pc = compose_command_context(parent, uuid4(), "real-correction-" + uuid4().hex)
    pkw = dict(context=pc, session_binding=parent_session.session_nonce_hash)
    option = next(x for x in corrections.options(**pkw) if x.field_name == "name")
    report = corrections.submit(
        **pkw, target_reference=option.reference, field_name="name", statement="请核对合成学生姓名"
    )
    assert (
        corrections.submit(
            **pkw,
            target_reference=option.reference,
            field_name="name",
            statement="请核对合成学生姓名",
        ).report_id
        == report.report_id
    )
    sc = compose_command_context(staff, uuid4(), "real-correction-process-" + uuid4().hex)
    skw = dict(context=sc, session_binding=staff_identity.session.session_nonce_hash)
    listed = corrections.staff_list(
        **skw,
        fact_kind="person",
        source_key=source,
        field_name="name",
        required_action="maintain",
        limit=20,
    )
    assert any(x.report_id == report.report_id for x in listed)

    def publish(name):
        with scenario.owner() as s:
            v = s.scalar(
                text(
                    "SELECT max(target_version) FROM school_fact_change_lineage WHERE fact_kind='person' AND fact_source_key=:source"
                ),
                {"source": source},
            )
        with scenario.engines["reader"].connect() as c:
            base = c.scalar(
                text("SELECT app.school_fact_current_version(:s)"), {"s": staff.school_id}
            )
        context = compose_command_context(staff, uuid4(), "real-correction-write-" + uuid4().hex)
        changes = canonical_changes(
            idempotency_key=context.idempotency_key,
            base_version=base,
            kind="person",
            key=source,
            expected=v,
            content={"display_name": name, "person_kind": "other", "lifecycle_status": "active"},
        )
        locator = sha256(context.idempotency_key.encode()).hexdigest()
        provenance = SchoolFactChangeProvenance(
            channel="admin",
            raw_source_sha256=changes.source_sha256,
            template_version="school_facts_v1",
            mapping_version="admin_v1",
            mapping_config_sha256="c" * 64,
            source_size_bytes=None,
            worksheet_names=(),
            result_locator_sha256=locator,
        )
        kw = dict(context=context, session_binding=staff_identity.session.session_nonce_hash)
        assert kernel.preflight(school_id=staff.school_id, change_set=changes, **kw).is_clean
        preview = kernel.prepare(change_set=changes, provenance=provenance, **kw)
        result = kernel.confirm(credential=preview.credential, change_set=changes, **kw)
        assert kernel.confirm(credential=preview.credential, change_set=changes, **kw).replayed
        own = kernel.check_own_result_by_locator(**skw, locator_sha256=locator)
        assert own is not None and own.locator_sha256 == locator
        assert len(own.targets) == 1 and own.targets[0].source_key == source
        return locator, result

    with scenario.owner() as s:
        name = s.scalar(
            text(
                "SELECT display_name FROM school_person_names WHERE person_id=(SELECT id FROM school_people WHERE source_key=:source) AND valid_to IS NULL ORDER BY valid_from DESC LIMIT 1"
            ),
            {"source": source},
        )
    unchanged, _ = publish(name)
    with pytest.raises(FamilyCorrectionDenied):
        corrections.resolve(**skw, report_id=report.report_id, writer_receipt_ref=unchanged)
    locator, published = publish("正式纠错结果" + uuid4().hex[:8])
    resolved = corrections.resolve(**skw, report_id=report.report_id, writer_receipt_ref=locator)
    assert resolved.status == "resolved"
    assert corrections.own(**pkw, report_id=report.report_id).status == "resolved"
    # Current Guardian loss must fail the SQL bind even with a formerly signed actor.
    with scenario.owner.begin() as s:
        s.execute(
            text(
                "UPDATE guardian_relationships SET status='revoked',revoked_at=:at,valid_to=:at WHERE id=:id"
            ),
            {"at": datetime.now(UTC), "id": parent.guardian_relationship_id},
        )
    with pytest.raises((FamilyCorrectionDenied, DBAPIError)):
        corrections.own(**pkw, report_id=report.report_id)
    evidence(
        "formal_resolution",
        {
            "report_id": str(report.report_id),
            "batch_id": str(published.batch_id),
            "real_scoped_kernel_confirmation": True,
            "duplicate_confirmation_same_receipt": True,
            "unchanged_field_receipt_rejected": True,
            "resolved_parent_receipt": True,
            "current_guardian_revocation_rejected": True,
            "fixture_revocation_not_product_publication": True,
        },
    )
