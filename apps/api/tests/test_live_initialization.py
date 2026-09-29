"""Formal initialization contracts, using public synthetic test inputs only."""

from __future__ import annotations

import importlib
import importlib.util
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from pydantic import ValidationError

from tests.school_facts.test_f1_initialization_contract import manifest as first_facts


def module():
    name = "app.composition.school_fact_live_initialization_contracts"
    assert importlib.util.find_spec(name), "formal initialization composition is missing"
    return importlib.import_module(name)


def manifest_payload():
    now = datetime(2026, 9, 27, tzinfo=UTC)
    facts = first_facts()["change_set"]
    for change in facts["changes"]:
        change["effective_at"] = now
        if change["fact_kind"] == "login_mapping":
            change["content"].update(
                provider_code="dingtalk",
                provider_tenant_key="test-corp",
                external_subject="test-first-operator",
            )
    return dict(
        purpose="school-facts-live-initialization",
        school_id=UUID(int=1),
        anchor_id=UUID(int=2),
        expected_system_identifier="1234567890123",
        database_name="campus_ai_school_facts_live_test",
        request_id=UUID(int=3),
        approval_reference="school-reviewed:test-approval",
        issuer_reference="school:test-issuer",
        issuer_qualification_reference="school:test-appointment",
        operator_reference="operator:test",
        source_authority_reference="school:test-authoritative-roster",
        corp_id="test-corp",
        external_subject="test-first-operator",
        approved_at=now,
        expires_at=now + timedelta(minutes=15),
        grant_valid_from=now,
        grant_valid_to=now + timedelta(days=90),
        scope_school_id=UUID(int=1),
        duties=("official_contact_reader", "official_contact_maintainer"),
        fields=(
            "owner_kind",
            "organization_source_key",
            "location_source_key",
            "contact_kind",
            "contact_value",
            "service_hours",
            "lifecycle_status",
            "visibility_scope",
            "audience_code",
        ),
        first_facts=facts,
    )


def test_formal_manifest_binds_identity_scope_and_finite_approval_to_original_request():
    m = module().LiveInitializationManifest.model_validate(manifest_payload())
    request = m.initialization_request()
    assert request.approval_reference == "live-init:" + m.manifest_sha256
    assert request.change_set == m.first_facts
    assert m.grant_valid_to - m.grant_valid_from == timedelta(days=90)
    m.require_current(m.approved_at)
    with pytest.raises(ValueError):
        m.require_current(m.expires_at)


@pytest.mark.parametrize(
    "field,value",
    [
        ("database_name", "campus_ai_school_facts_f1"),
        ("database_name", "campus_ai_identity_test"),
        ("issuer_qualification_reference", ""),
        ("scope_school_id", UUID(int=4)),
        ("duties", ("school_admin",)),
        ("fields", ("contact_value",)),
        ("corp_id", "other-corp"),
        ("external_subject", "someone-else"),
        ("grant_valid_to", datetime(2027, 1, 1, tzinfo=UTC)),
        ("expires_at", datetime(2026, 9, 28, tzinfo=UTC)),
    ],
)
def test_formal_manifest_rejects_unreviewed_or_expanded_inputs(field, value):
    raw = manifest_payload()
    raw[field] = value
    with pytest.raises(ValidationError):
        module().LiveInitializationManifest.model_validate(raw)


def test_formal_manifest_never_accepts_engineering_mapping():
    raw = manifest_payload()
    raw["first_facts"]["changes"][2]["content"]["provider_code"] = "local_test"
    with pytest.raises(ValidationError):
        module().LiveInitializationManifest.model_validate(raw)


def test_formal_root_rejects_nil_school_before_database_creation():
    raw = manifest_payload()
    root = {
        k: raw[k]
        for k in (
            "approval_reference",
            "issuer_reference",
            "issuer_qualification_reference",
            "operator_reference",
            "source_authority_reference",
            "approved_at",
            "expires_at",
        )
    }
    root.update(
        purpose="school-facts-live-root",
        db="campus_ai_school_facts_live_test",
        system_identifier="1234567890123",
        school_id=UUID(int=0),
        source_key="school:test",
        school_code="TEST",
        display_name="测试",
        timezone_name="Asia/Shanghai",
    )
    with pytest.raises(ValidationError):
        module().LiveRootManifest.model_validate(root)


def test_changed_approval_cannot_reuse_original_initialization():
    m = module().LiveInitializationManifest.model_validate(manifest_payload())
    raw = manifest_payload()
    raw["issuer_reference"] = "school:another-issuer"
    other = module().LiveInitializationManifest.model_validate(raw)
    assert (
        other.initialization_request().manifest_sha256()
        != m.initialization_request().manifest_sha256()
    )


def test_shared_root_preserves_checked_deployment_identity_before_database_access():
    name = "app.composition.school_fact_deployment_root"
    assert importlib.util.find_spec(name), "checked root helper is missing"
    helper = importlib.import_module(name)
    from types import SimpleNamespace

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def scalar(self, statement):
            assert str(statement) == "SELECT session_user"
            return "campus_ai_runtime"

        def execute(self, *args):
            pytest.fail("runtime must never set role or write a root")

    with pytest.raises(ValueError):
        helper.controlled_school_root(
            SimpleNamespace(begin=lambda: Connection()),
            None,
            expected_database="campus_ai_school_facts_live_test",
        )


def configuration_payload():
    runtime = dict(
        app_env="production",
        identity_provider="dingtalk",
        postgres_host="127.0.0.1",
        postgres_port=5432,
        postgres_db="campus_ai_school_facts_live_test",
        postgres_user="campus_ai_runtime",
        postgres_password="runtime-" * 8,
        postgres_schema_contract="school-facts-f1-v22",
        postgres_expected_anchor_id=UUID(int=2),
        postgres_expected_school_id=UUID(int=1),
        postgres_expected_system_identifier="1234567890123",
        redis_port=6379,
        redis_password="redis-" * 8,
        rls_context_signing_key="context-" * 8,
        dingtalk_corp_id="test-corp",
        dingtalk_app_key="public-app",
        dingtalk_app_secret="app-secret-" * 8,
        identity_session_cookie_name="school_fact_live_session",
    )
    component = dict(
        database_host="127.0.0.1",
        database_port=5432,
        database_name=runtime["postgres_db"],
        schema_contract="school-facts-f1-v22",
        expected_anchor_id=UUID(int=2),
        expected_school_id=UUID(int=1),
        expected_system_identifier="1234567890123",
    )
    return dict(
        purpose="school-facts-live-initialization",
        runtime=runtime,
        migration=dict(
            host="127.0.0.1",
            port=5432,
            db=runtime["postgres_db"],
            user="campus_ai_migration",
            password="migration-" * 8,
            context_signing_key="context-" * 8,
        ),
        reader=dict(component, database_password="reader-" * 8, cursor_hmac_key="cursor-" * 8),
        writer=dict(
            component,
            database_password="writer-" * 8,
            confirmation_hmac_key="confirm-" * 8,
            caller_auth_key="caller-" * 8,
            context_transport_hmac_key="transport-" * 8,
            rls_context_signing_key="context-" * 8,
        ),
    )


def test_formal_configuration_requires_one_isolated_schema22_database():
    config = module().LiveInitializationConfiguration.model_validate(configuration_payload())
    config.require_manifest(module().LiveInitializationManifest.model_validate(manifest_payload()))
    raw = configuration_payload()
    raw["reader"]["database_name"] = "campus_ai_identity_test"
    with pytest.raises(ValidationError):
        module().LiveInitializationConfiguration.model_validate(raw)


@pytest.mark.parametrize(
    "provider,tenant,subject",
    [
        ("local_test", "test-corp", "test-first-operator"),
        ("dingtalk", "other-corp", "test-first-operator"),
        ("dingtalk", "test-corp", "other-operator"),
        ("dingtalk", "test-corp", "test-first-operator"),
    ],
)
def test_formal_account_rechecks_current_provider_corp_and_exact_subject(provider, tenant, subject):
    name = "app.composition.school_fact_live_initialization_identity"
    assert importlib.util.find_spec(name), "formal current account adapter is missing"
    helper = importlib.import_module(name)
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.platform.identity.context import CompleteIdentitySessionError, CurrentUser
    from app.platform.identity.models import User
    from app.platform.identity.session import IdentitySessionStore
    from tests.test_f1_product_identity import MemoryRedis

    redis = MemoryRedis()
    engine = create_engine("sqlite://")
    User.__table__.create(engine)
    sessions = sessionmaker(engine)
    with sessions.begin() as session:
        session.add(
            User(
                id=1,
                provider_code=provider,
                provider_tenant_key=tenant,
                external_subject=subject,
                display_name="synthetic",
                is_active=True,
                security_version=1,
            )
        )
    token = IdentitySessionStore(redis=redis, ttl_seconds=28800).create(
        CurrentUser(1, subject, "synthetic", 1)
    )
    supplied = module().LiveInitializationSession(
        token=token, corp_id="test-corp", external_subject="test-first-operator"
    )
    config = module().LiveInitializationConfiguration.model_validate(configuration_payload())
    try:
        if (provider, tenant, subject) != ("dingtalk", "test-corp", "test-first-operator"):
            with pytest.raises(CompleteIdentitySessionError):
                helper.require_live_initialization_account(
                    config.runtime, sessions, redis, supplied
                )
        else:
            assert (
                helper.require_live_initialization_account(
                    config.runtime, sessions, redis, supplied
                ).current_user.id
                == 1
            )
            with sessions.begin() as session:
                session.get(User, 1).security_version += 1
            with pytest.raises(CompleteIdentitySessionError):
                helper.require_live_initialization_account(
                    config.runtime, sessions, redis, supplied
                )
    finally:
        engine.dispose()


def test_readiness_requires_explicit_formal_session_and_preserves_engineering_gate():
    from app.composition.school_fact_initialization_readiness import FirstMaintainerReadiness
    from app.platform.school_facts.initialization_contracts import SchoolFactInitializationRejected

    config = module().LiveInitializationConfiguration.model_validate(configuration_payload())
    kwargs = dict(
        runtime_settings=config.runtime, reader_settings=config.reader, scope_id=UUID(int=4)
    )
    with pytest.raises(SchoolFactInitializationRejected):
        FirstMaintainerReadiness(**kwargs)
    formal = module().LiveInitializationSession(
        token="x" * 48, corp_id="test-corp", external_subject="test-first-operator"
    )
    readiness = FirstMaintainerReadiness(**kwargs, formal_identity=formal)
    assert readiness._formal_identity is formal


@pytest.mark.parametrize(
    "action",
    [
        "prepare",
        "confirm",
        "governance",
        "publication-grant",
        "publication-prepare",
        "publication-confirm",
    ],
)
def test_formal_dispatch_cannot_write_after_window(action):
    name = "app.composition.school_fact_live_initialization"
    assert importlib.util.find_spec(name), "formal initialization dispatcher missing"
    dispatch = importlib.import_module(name)
    m = module().LiveInitializationManifest.model_validate(manifest_payload())
    with pytest.raises(ValueError):
        dispatch.require_action_window(m, action, m.expires_at)


@pytest.mark.parametrize("action", ["result", "publication-result", "publication-end", "close"])
def test_formal_dispatch_keeps_original_result_and_closure_available_after_window(action):
    name = "app.composition.school_fact_live_initialization"
    assert importlib.util.find_spec(name), "formal initialization dispatcher missing"
    dispatch = importlib.import_module(name)
    m = module().LiveInitializationManifest.model_validate(manifest_payload())
    dispatch.require_action_window(m, action, m.expires_at)


@pytest.mark.parametrize("kind", ["root", "initialization", "publication", "confirmation"])
def test_cli_reads_its_strict_json_contracts_without_relaxing_python_validation(tmp_path, kind):
    import json

    from app.composition import school_fact_live_initialization as dispatch
    from app.platform.school_facts.contracts import ConfirmationReceiptV1
    from tests.test_f1_initial_publication import manifest as publication_manifest

    m = module()
    if kind == "initialization":
        value = m.LiveInitializationManifest.model_validate(manifest_payload())
    elif kind == "publication":
        value = publication_manifest()
    elif kind == "confirmation":
        value = ConfirmationReceiptV1(
            credential="a" * 48, plan_sha256="b" * 64, expires_at=datetime(2026, 9, 27, tzinfo=UTC)
        )
    else:
        payload = manifest_payload()
        values = {
            k: payload[k]
            for k in (
                "approval_reference",
                "issuer_reference",
                "issuer_qualification_reference",
                "operator_reference",
                "source_authority_reference",
                "approved_at",
                "expires_at",
            )
        }
        value = m.LiveRootManifest(
            **values,
            purpose="school-facts-live-root",
            db="campus_ai_school_facts_live_test",
            system_identifier="1234567890123",
            school_id=UUID(int=1),
            source_key="school:test",
            school_code="TEST",
            display_name="测试",
            timezone_name="Asia/Shanghai",
        )
    path = tmp_path / (kind + ".json")
    path.write_text(value.model_dump_json(exclude_none=True), encoding="utf-8")
    assert hasattr(dispatch, "load_reviewed_contract"), "strict JSON CLI contract loader missing"
    loaded = dispatch.load_reviewed_contract(type(value), path)
    assert loaded == value
    if kind in {"root", "initialization", "publication"}:
        assert loaded.manifest_sha256 == value.manifest_sha256
    if kind in {"initialization", "publication", "confirmation"}:
        with pytest.raises(ValidationError):
            type(value).model_validate(json.loads(path.read_text(encoding="utf-8")))


@pytest.mark.parametrize(
    "action", ["publication-grant", "publication-prepare", "publication-confirm"]
)
def test_deferred_manifest_rejects_publication_mutations(action):
    from app.composition.school_fact_live_initialization import require_action_window

    manifest = module().LiveInitializationManifest.model_validate(manifest_payload())
    manifest = manifest.model_copy(update={"publication_policy": "deferred"})
    with pytest.raises(ValueError, match="deferred"):
        require_action_window(manifest, action, manifest.approved_at)


def test_deferred_readiness_requires_valid_formal_manifest_and_exact_request(monkeypatch):
    from app.composition.school_fact_initialization_readiness import FirstMaintainerReadiness
    from app.platform.school_facts.initialization_contracts import SchoolFactInitializationRejected

    config = module().LiveInitializationConfiguration.model_validate(configuration_payload())
    formal = module().LiveInitializationSession(
        token="x" * 48, corp_id="test-corp", external_subject="test-first-operator"
    )
    manifest = module().LiveInitializationManifest.model_validate(manifest_payload())
    manifest = manifest.model_copy(update={"publication_policy": "deferred"})
    kwargs = dict(
        runtime_settings=config.runtime, reader_settings=config.reader, scope_id=UUID(int=4)
    )
    with pytest.raises(SchoolFactInitializationRejected):
        FirstMaintainerReadiness(**kwargs, formal_manifest=manifest)
    readiness = FirstMaintainerReadiness(**kwargs, formal_identity=formal, formal_manifest=manifest)
    other = manifest.initialization_request().model_copy(update={"approval_reference": "other"})
    with pytest.raises(SchoolFactInitializationRejected, match="manifest"):
        readiness.verify_publication(None, request=other, initialization_id=UUID(int=5))


def test_publication_absence_rejects_unknown_database_result():
    from app.composition import school_fact_initial_publication as publication

    class UnknownSession:
        def scalar(self, *args, **kwargs):
            return None

    with pytest.raises(publication.SchoolFactChangeRejected):
        publication.verify_initial_publication_absent(
            UnknownSession(), school_id=UUID(int=1), initialization_id=UUID(int=5)
        )


def test_initial_publication_explicitly_rejects_guardian_context():
    from app.composition.school_fact_initial_publication import InitialPublicationService
    from app.composition.school_fact_product_identity import ProductIdentity
    from app.platform.contracts import _issue_guardian_actor_context
    from app.platform.school_facts.application import SchoolFactChangeRejected
    from tests.test_f1_initial_publication import manifest as publication_manifest

    manifest = publication_manifest()
    actor = _issue_guardian_actor_context(
        account_user_id=1,
        school_id=manifest.school_id,
        person_id=UUID(int=10),
        guardian_relationship_id=UUID(int=11),
        relationship_version=1,
        authorization_version=1,
    )
    service = InitialPublicationService(
        manifest=manifest,
        grant_id=UUID(int=12),
        kernel=None,
        require_current=lambda _: None,
    )
    with pytest.raises(SchoolFactChangeRejected):
        service._context(ProductIdentity(actor, "a" * 64, "private"), writing=False)


def test_governance_close_rechecks_current_duties_in_its_transaction(monkeypatch):
    from types import SimpleNamespace

    from app.composition.school_fact_initialization_readiness import FirstMaintainerReadiness
    from app.composition.school_fact_live_initialization import _FiniteBootstrap
    from app.platform.school_facts.initialization_contracts import SchoolFactInitializationRejected

    m = module().LiveInitializationManifest.model_validate(manifest_payload())
    config = module().LiveInitializationConfiguration.model_validate(configuration_payload())
    formal = module().LiveInitializationSession(
        token="x" * 48, corp_id=m.corp_id, external_subject=m.external_subject
    )
    readiness = FirstMaintainerReadiness(
        runtime_settings=config.runtime,
        reader_settings=config.reader,
        scope_id=UUID(int=4),
        formal_identity=formal,
        formal_manifest=m,
    )
    monkeypatch.setattr(FirstMaintainerReadiness, "verify_publication", lambda *a, **k: None)

    def revoked(*args):
        raise SchoolFactInitializationRejected("READ duty revoked after precheck")

    monkeypatch.setattr(FirstMaintainerReadiness, "_verify_identity_and_duties", revoked)
    result = {"targets": [{"fact_kind": "assignment", "target_id": str(UUID(int=7))}]}
    record = SimpleNamespace(
        id=UUID(int=5),
        state="applied",
        manifest_sha256=m.initialization_request().manifest_sha256(),
        result_payload=result,
    )
    session = SimpleNamespace(
        scalar=lambda _: record, execute=lambda *a: SimpleNamespace(scalar_one=lambda: 1)
    )
    authorizer = _FiniteBootstrap(
        m, lambda: m.approved_at, readiness=readiness, initialization_id=UUID(int=5)
    )
    with pytest.raises(SchoolFactInitializationRejected, match="READ duty revoked"):
        authorizer._verify_closure(
            SimpleNamespace(_session=session),
            SimpleNamespace(command_name="governance.bootstrap.close"),
        )
