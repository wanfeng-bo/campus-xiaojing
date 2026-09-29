from __future__ import annotations

import importlib
import json
from copy import deepcopy
from datetime import UTC, datetime
from importlib.util import find_spec
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.platform.assistant.runtime import AssistantRuntimeError
from app.platform.authorization.secure_reader_contracts import OfficialContactMaintenanceTargetRow
from app.platform.school_facts.application import SchoolFactChangeRejected
from app.platform.school_facts.read_policy import (
    OfficialContactFact,
    SchoolFactAuthorizationUnavailable,
)


def api(name="school_fact_product_settings"):
    module = "app.composition." + name
    assert find_spec(module) is not None, "missing isolated product composition"
    return importlib.import_module(module)


@pytest.mark.parametrize("stage", [1, 2, 3, 4, 5, 6, 7])
def test_all_fact_product_stages_share_evidence_based_expression_without_changing_boundaries(
    document, monkeypatch, tmp_path, stage
):
    import hashlib

    from app.composition.school_fact_product_identity import ProductFamilyActor
    from tests.assistant.test_f3_product_configuration import f3_document
    from tests.assistant.test_f4_product_configuration import f4_document
    from tests.school_facts.test_f5_startup_contract import f5_document
    from tests.school_facts.test_f6_startup_contract import f6_document
    from tests.school_facts.test_f7_startup_contract import f7_document
    from tests.test_f2_directory_product import actor
    from tests.test_f3_product_identity import guardian

    module = api("school_fact_product")
    if stage >= 3:
        document = {
            3: f3_document, 4: f4_document, 5: f5_document, 6: f6_document, 7: f7_document,
        }[stage](tmp_path)
    elif stage == 2:
        document["purpose"] = "school-facts-f2-synthetic"
        document["runtime"]["postgres_schema_contract"] = "school-facts-f2-v25"
        document["reader"]["schema_contract"] = "school-facts-f2-v25"
        document["directory"] = {"approval_reference": "unit-f2", "scope_bindings": []}
    configuration = api().SchoolFactProductConfiguration.model_validate_json(json.dumps(document))
    monkeypatch.setattr(module, "load_school_fact_product_configuration", lambda: configuration)
    identity = MagicMock()
    for name in (
        "build_engine",
        "build_session_factory",
        "Redis",
        "create_school_fact_product_services",
        "create_directory_read_components",
        "create_family_query_service",
        "create_family_staff_components",
        "SchoolFactEphemeralStoreJanitor",
    ):
        monkeypatch.setattr(module, name, MagicMock())
    monkeypatch.setattr(module, "verify_runtime_context_signing_key", lambda *_: None)
    monkeypatch.setattr(module, "build_school_fact_product_identity_service", lambda **_: identity)
    make_loop = MagicMock()
    app = module.create_school_fact_product_app(assistant_loop_factory=make_loop)
    if stage >= 3:
        assert module.create_family_query_service.call_args.kwargs["enable_quality"] is (stage == 7)
    if stage == 7:
        routes = set(app.openapi()["paths"])
        assert "/api/v1/school-fact-product/imports/plans/package" in routes
        assert "/api/v1/school-fact-product/imports/plans/admin" not in routes
        assert "/api/v1/school-facts/plans/admin" not in routes
        off_document = deepcopy(document)
        off_document["f7_packages_enabled"] = False
        off_configuration = api().SchoolFactProductConfiguration.model_validate_json(
            json.dumps(off_document)
        )
        monkeypatch.setattr(
            module, "load_school_fact_product_configuration", lambda: off_configuration
        )
        module.create_school_fact_product_app(assistant_loop_factory=make_loop)
        assert module.create_family_query_service.call_args.kwargs["enable_quality"] is False
    selected_instruction = make_loop.call_args.kwargs["instruction"]
    shared = module.F1_ASSISTANT_INSTRUCTION
    suffix = shared[shared.index("永久排除边界") :]
    assert hashlib.sha256(suffix.encode()).hexdigest() == (
        "2f0f2a25b4c3adebf13a71d0f6095492cb79315e99a000251e8c8609ed31d039"
    )
    parent = guardian()
    family_actor = ProductFamilyActor(
        account_user_id=parent.account_user_id, school_id=parent.school_id,
        person_id=parent.person_id, authorization_version=parent.authorization_version,
    )
    for subject in [actor(), *([family_actor] if stage >= 3 else [])]:
        identity.resolve_assistant_identity.return_value = SimpleNamespace(
            actor=subject, session_binding="unit-session"
        )
        actual = (
            selected_instruction("unit-context")
            if callable(selected_instruction)
            else selected_instruction
        )
        assert actual.count(shared) == 1
        assert actual.endswith(shared)
        if type(subject) is ProductFamilyActor:
            assert module.FAMILY_CHILDREN_INSTRUCTION in actual
        assert "不是答复草稿" in actual
        assert "必要事实" in actual
        # This verifies every real composition receives the shared contract once;
        # it does not claim that a model always follows it in natural language.
        assert "对象、字段和适用时点" in actual
        assert "来源只支持对应记录实际记载的内容" in actual
        assert "不是来源的正式记载或明确否定" in actual
        assert "原结论实际采用的出处" in actual
        assert "每句话应回答用户所问" in actual
        assert "会改变结论或下一步的必要信息" in actual
        assert "办事方式" in actual
        assert "由用户使用正式控件确认" in actual
        assert "selected_child_display_label" not in actual
        assert "不能从先前选择推断当前对象" not in actual


@pytest.fixture
def document():
    anchor, school = str(uuid4()), str(uuid4())
    return {
        "purpose": "school-facts-f1-synthetic",
        "runtime": {
            "app_env": "test",
            "identity_provider": "local_test",
            "postgres_schema_contract": "school-facts-f1-v22",
            "postgres_host": "postgres",
            "postgres_port": 5432,
            "postgres_db": "campus_ai_school_facts_f1",
            "postgres_user": "campus_ai_runtime",
            "postgres_password": "runtime-password-" * 3,
            "postgres_expected_anchor_id": anchor,
            "postgres_expected_school_id": school,
            "postgres_expected_system_identifier": "1234567890123",
            "rls_context_signing_key": "runtime-context-" * 3,
            "redis_host": "redis",
            "redis_port": 6379,
            "redis_password": "redis-password-" * 3,
        },
        "reader": {
            "schema_contract": "school-facts-f1-v22",
            "database_host": "postgres",
            "database_port": 5432,
            "database_name": "campus_ai_school_facts_f1",
            "database_password": "reader-password-" * 3,
            "expected_anchor_id": anchor,
            "expected_school_id": school,
            "expected_system_identifier": "1234567890123",
            "cursor_hmac_key": "reader-cursor-" * 3,
        },
        "executor": {
            "base_url": "http://writer:8010",
            "caller_auth_key": "caller-key-" * 4,
            "context_transport_hmac_key": "transport-key-" * 4,
        },
        "scope_id": str(uuid4()),
        "allowed_origin": "http://127.0.0.1:8001",
        "trusted_proxy_cidrs": [],
        "control_signing_key": "assistant-control-" * 3,
        "model": None,
    }


def approved_model_document(tmp_path):
    key_file = tmp_path / "unit-provider-secret"
    key_file.write_text("unit-only-api-key\n", encoding="utf-8")
    return {
        "api_key_file": str(key_file),
        "model": "deepseek-v4-flash",
        "thinking": "enabled",
        "reasoning_effort": "high",
        "approval_reference": "unit-model-approval",
        "budget": {
            "approval_reference": "unit-budget-approval",
            "cap_fen": 5000,
            "input_cny_per_million": "3",
            "output_cny_per_million": "9",
        },
        "retention_approval_reference": "unit-retention-approval",
        "chat_retention_hours": 24,
        "state_directory": str(tmp_path),
        "max_input_tokens": 32768,
        "max_output_tokens": 16384,
    }


def test_configuration_reads_only_explicit_json_not_dotenv_or_ambient(
    document, monkeypatch, tmp_path
):
    module = api()
    monkeypatch.setenv("POSTGRES_PASSWORD", "ambient-secret")
    monkeypatch.setenv("SCHOOL_FACT_READER_DATABASE_PASSWORD", "ambient-reader-" * 3)
    monkeypatch.setenv("SCHOOL_FACT_EXECUTOR_BASE_URL", "https://unexpected.invalid")
    monkeypatch.setenv("REPAIR_DRAFT_MODEL_ENABLED", "true")
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("POSTGRES_PASSWORD=dotenv-secret", encoding="utf-8")
    path = tmp_path / "product.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    monkeypatch.setenv("SCHOOL_FACT_PRODUCT_CONFIG", str(path))
    config = module.load_school_fact_product_configuration()
    assert config.runtime.postgres_password == document["runtime"]["postgres_password"]
    assert (
        config.reader.database_password.get_secret_value()
        == document["reader"]["database_password"]
    )
    assert config.executor.base_url == "http://writer:8010"
    assert config.model is None
    assert not config.runtime.repair_draft_model_enabled
    assert "runtime-password" not in repr(config)


@pytest.mark.parametrize(
    "section,field,value",
    [
        ("runtime", "postgres_password", None),
        ("runtime", "postgres_schema_contract", "identity-v20"),
        ("runtime", "postgres_schema_contract", "school-facts-f1-v21"),
        ("runtime", "postgres_expected_anchor_id", None),
        ("runtime", "postgres_user", "postgres"),
        ("runtime", "identity_provider", "dingtalk"),
        ("runtime", "app_env", "production"),
        ("reader", "expected_school_id", "00000000-0000-0000-0000-000000000001"),
        ("reader", "database_password", "runtime-password-" * 3),
        ("executor", "unknown_credential", "must-not-be-ignored"),
    ],
)
def test_configuration_rejects_missing_or_mixed_authority(
    document, monkeypatch, section, field, value
):
    monkeypatch.setenv("POSTGRES_PASSWORD", "ambient-fallback-" * 3)
    bad = deepcopy(document)
    if value is None:
        del bad[section][field]
    else:
        bad[section][field] = value
    with pytest.raises(ValidationError):
        api().SchoolFactProductConfiguration.model_validate(bad)


def test_configuration_requires_explicit_purpose_and_rejects_unsafe_origin(document):
    module = api()
    for key, value in [
        ("purpose", "hangzhou"),
        ("allowed_origin", "https://public.example.com"),
        ("trusted_proxy_cidrs", ["10.0.0.0/8"]),
    ]:
        with pytest.raises(ValidationError):
            module.SchoolFactProductConfiguration.model_validate({**document, key: value})


def test_configuration_missing_path_or_duplicate_json_never_falls_back(
    document, monkeypatch, tmp_path
):
    module = api()
    monkeypatch.delenv("SCHOOL_FACT_PRODUCT_CONFIG", raising=False)
    with pytest.raises(module.ProductConfigurationError):
        module.load_school_fact_product_configuration()
    path = tmp_path / "bad.json"
    path.write_text('{"purpose":"x","purpose":"school-facts-f1-synthetic"}', encoding="utf-8")
    monkeypatch.setenv("SCHOOL_FACT_PRODUCT_CONFIG", str(path))
    with pytest.raises(module.ProductConfigurationError):
        module.load_school_fact_product_configuration()


def test_model_configuration_has_no_implied_approval(document, tmp_path):
    module = api()
    document["model"] = {"api_key_file": str(tmp_path / "key"), "model": "deepseek-v4-flash"}
    with pytest.raises(ValidationError):
        module.SchoolFactProductConfiguration.model_validate(document)
    document["model"] = approved_model_document(tmp_path)
    config = module.SchoolFactProductConfiguration.model_validate_json(json.dumps(document))
    assert config.model.budget.cap_fen == 5000
    assert config.model.chat_retention_hours == 24
    assert "unit-provider-secret" not in repr(config)
    assert not hasattr(config.model, "api_key")


def test_disabled_model_never_returns_a_substitute_answer():
    module = api("school_fact_product")
    with pytest.raises(AssistantRuntimeError):
        module.DisabledModel().respond(MagicMock())


@pytest.mark.parametrize("model_enabled", [False, True])
def test_factory_wires_real_contracts_and_disposes_without_network(
    document, monkeypatch, tmp_path, model_enabled
):
    module = api("school_fact_product")
    if model_enabled:
        document["model"] = approved_model_document(tmp_path)
    monkeypatch.setattr(module, "build_product_model", lambda _: module.DisabledModel())
    make_runtime = MagicMock(wraps=module.AssistantRuntime)
    monkeypatch.setattr(module, "AssistantRuntime", make_runtime)
    configuration = api().SchoolFactProductConfiguration.model_validate_json(json.dumps(document))
    monkeypatch.setattr(module, "load_school_fact_product_configuration", lambda: configuration)
    engine, services, redis, identity = MagicMock(), MagicMock(), MagicMock(), MagicMock()
    monkeypatch.setattr(module, "build_engine", lambda value: engine)
    verify = MagicMock()
    monkeypatch.setattr(module, "verify_runtime_context_signing_key", verify)
    sessions = MagicMock()
    monkeypatch.setattr(module, "build_session_factory", lambda value: sessions)
    make_services, make_identity, make_redis = (
        MagicMock(return_value=services),
        MagicMock(return_value=identity),
        MagicMock(return_value=redis),
    )
    monkeypatch.setattr(module, "create_school_fact_product_services", make_services)
    monkeypatch.setattr(module, "build_school_fact_product_identity_service", make_identity)
    monkeypatch.setattr(module, "Redis", make_redis)
    janitor = MagicMock()
    make_janitor = MagicMock(return_value=janitor)
    monkeypatch.setattr(module, "SchoolFactEphemeralStoreJanitor", make_janitor)
    gateway = MagicMock(wraps=module.OfficialContactToolGateway)
    monkeypatch.setattr(module, "OfficialContactToolGateway", gateway)
    app = module.create_school_fact_product_app()
    paths = set(app.openapi()["paths"])
    assert "/api/v1/school-fact-product/contacts/prepare" in paths
    assert "/api/v1/school-fact-product/login" in paths
    assert "/api/v1/assistant/conversations" in paths
    assert not any("repair" in path or "dingtalk" in path for path in paths)
    assert api_error_handler_present(app)
    assert make_redis.call_args.kwargs["decode_responses"] is True
    from app.platform import contracts as actors
    from app.platform.authorization.secure_reader_compiler import ServerOwnedProjectionCompiler
    from app.platform.authorization.secure_reader_contracts import SchoolFactReadSelection
    from app.platform.authorization.secure_reader_registry import (
        official_contact_operation_registry,
    )
    from app.platform.authorization.thin_contracts import ScopePlan

    require_read = gateway.call_args.kwargs["require_read"]
    actor = actors._issue_staff_actor_context(
        account_user_id=7,
        school_id=actors.SchoolId(uuid4()),
        person_id=actors.PersonId(uuid4()),
        assignment_id=actors.AssignmentId(uuid4()),
        assignment_version=1,
        authorization_version=1,
    )
    registry = official_contact_operation_registry()

    def compile_actual_revalidation(request):
        # Exercise the actual registered projection contract; a broad MagicMock would
        # accept a missing selector and hide a failure at every post-query revalidation.
        return ServerOwnedProjectionCompiler().compile(
            operation=registry.require(request.operation_code),
            actor=request.actor,
            scope_plan=ScopePlan(uuid4(), "school_facts.school", f"school:{actor.school_id}", 1),
            as_of=datetime.now(UTC),
            after_id=None,
            page_size=request.page_size,
            selection=request.selection,
        )

    services.secure_reader.read.side_effect = compile_actual_revalidation
    require_read(actor)
    request = services.secure_reader.read.call_args.args[0]
    assert request.actor is actor and request.page_size == 1
    assert request.operation_code == "school_facts.official_contact_campuses"
    assert request.selection == SchoolFactReadSelection()
    services.secure_reader.read.side_effect = SchoolFactAuthorizationUnavailable("source failure")
    with pytest.raises(SchoolFactAuthorizationUnavailable):
        require_read(actor)
    verify.assert_called_once_with(engine, configuration.runtime)
    maintenance = make_identity.call_args.kwargs["can_maintain"]
    services.maintenance_guard.require_current.side_effect = SchoolFactChangeRejected("denied")
    assert maintenance(MagicMock()) is False
    services.maintenance_guard.require_current.side_effect = SchoolFactAuthorizationUnavailable(
        "unavailable"
    )
    # Exercise the actual product callback through the real identity HTTP error mapping.
    identity.session_view.side_effect = lambda _token: maintenance(actor)
    with TestClient(
        app,
        base_url=document["allowed_origin"],
        client=("127.0.0.1", 5000),
        raise_server_exceptions=False,
    ) as client:
        response = client.get("/api/v1/school-fact-product/session")
        assert response.status_code == 503
        assert response.json() == {"detail": "当前身份暂时无法核验。"}
        assert "unavailable" not in response.text
    engine.dispose.assert_called_once()
    services.close.assert_called_once()
    redis.close.assert_called_once()
    janitor.start.assert_called_once()
    janitor.stop.assert_called_once()
    assert len(make_janitor.call_args.args) == 4
    assert type(make_janitor.call_args.args[3]).__name__ == "OfficialContactToolGateway"
    expected_chat_ttl = 86400 if model_enabled else 60
    assert gateway.call_args.kwargs["receipt_retention_seconds"] == expected_chat_ttl
    assert make_runtime.call_args.kwargs["retention_seconds"] == expected_chat_ttl
    assert set(make_identity.call_args.kwargs) == {
        "app_env",
        "session_factory",
        "redis",
        "proof_signer",
        "can_maintain",
        "family_enabled",
    }
    assert make_identity.call_args.kwargs["family_enabled"] is False
    # Janitor owns only in-memory chat, candidate, confirmation and recent-reference stores.
    assert services.writer_boundary not in make_janitor.call_args.args
    assert services.query_repository not in make_janitor.call_args.args


def api_error_handler_present(app):
    from fastapi.exceptions import RequestValidationError

    from app.interfaces.http.errors import ApiError

    return ApiError in app.exception_handlers and RequestValidationError in app.exception_handlers


def test_factory_releases_runtime_engine_when_reader_startup_fails(document, monkeypatch):
    module = api("school_fact_product")
    monkeypatch.setattr(
        module,
        "load_school_fact_product_configuration",
        lambda: api().SchoolFactProductConfiguration.model_validate(document),
    )
    engine = MagicMock()
    monkeypatch.setattr(module, "build_engine", lambda _: engine)
    monkeypatch.setattr(module, "verify_runtime_context_signing_key", lambda *_: None)
    monkeypatch.setattr(module, "build_session_factory", lambda _: MagicMock())
    monkeypatch.setattr(
        module,
        "create_school_fact_product_services",
        MagicMock(side_effect=RuntimeError("source unavailable")),
    )
    with pytest.raises(RuntimeError):
        module.create_school_fact_product_app()
    engine.dispose.assert_called_once()


def test_target_resolution_preserves_canonical_fields_and_rejects_racing_public_version():
    module = api("school_fact_product")
    now, campus, lineage = datetime.now(UTC), uuid4(), uuid4()
    row = OfficialContactMaintenanceTargetRow(
        row_id=lineage,
        source_key="private-contact",
        expected_version=4,
        campus_id=campus,
        campus_source_key="private-campus",
        school_fact_version=9,
        owner_kind="location",
        contact_kind="phone",
        contact_value="010-00000000",
        service_hours="工作日",
        lifecycle_status="active",
        visibility_scope="school",
        audience_code="staff",
        valid_from=now,
        valid_to=None,
    )
    public = OfficialContactFact(
        fact_ref=f"official-contact:{lineage}",
        campus_ref=f"campus:{campus}",
        campus_name="合成校区",
        contact_kind="phone",
        contact_value=row.contact_value,
        service_hours=row.service_hours,
        valid_from=now,
        valid_to=None,
        version=4,
        source_kind="admin",
        source_description="正式维护",
        publication_ref="publication:one",
        published_at=now,
    )
    query = MagicMock()
    query.resolve_maintenance_target.return_value = row
    query.read_official_contacts.return_value = SimpleNamespace(status="known", contacts=(public,))
    actor = MagicMock()
    target = module.resolve_contact_target(query=query, actor=actor, fact_ref=public.fact_ref)
    assert target.content.location_source_key == "private-campus"
    assert target.content.campus_source_key is None
    assert target.content.visibility_scope == "school"
    assert target.content.audience_code == "staff"
    assert target.school_fact_version == 9
    query.read_official_contacts.assert_called_once_with(actor=actor, campus_ref=f"campus:{campus}")
    query.read_official_contacts.return_value = SimpleNamespace(status="known", contacts=())
    with pytest.raises(module.ContactProductUnavailable):
        module.resolve_contact_target(query=query, actor=actor, fact_ref=public.fact_ref)


def test_resolver_output_reaches_real_prepare_without_invalid_canonical_fields():
    from datetime import timedelta

    from app.composition.school_fact_product_contacts import (
        ContactChangeInput,
        OfficialContactProductService,
    )
    from app.composition.school_facts import InMemorySchoolFactPlanStore
    from app.platform import contracts as actors
    from app.platform.school_facts.contracts import (
        ConfirmationReceiptV1,
        PreflightReportV1,
        canonical_plan_sha256,
    )

    module = api("school_fact_product")
    now, campus, lineage = datetime.now(UTC), uuid4(), uuid4()
    actor = actors._issue_staff_actor_context(
        account_user_id=7,
        school_id=actors.SchoolId(uuid4()),
        person_id=actors.PersonId(uuid4()),
        assignment_id=actors.AssignmentId(uuid4()),
        assignment_version=1,
        authorization_version=1,
    )
    row = OfficialContactMaintenanceTargetRow(
        row_id=lineage,
        source_key="private-contact",
        expected_version=1,
        campus_id=campus,
        campus_source_key="private-campus",
        school_fact_version=3,
        owner_kind="location",
        contact_kind="phone",
        contact_value="010-00000000",
        service_hours="工作日",
        lifecycle_status="active",
        visibility_scope="public",
        audience_code=None,
        valid_from=now,
        valid_to=None,
    )
    fact = OfficialContactFact(
        fact_ref=f"official-contact:{lineage}",
        campus_ref=f"campus:{campus}",
        campus_name="合成校区",
        contact_kind="phone",
        contact_value=row.contact_value,
        service_hours=row.service_hours,
        valid_from=now,
        valid_to=None,
        version=1,
        source_kind="admin",
        source_description="正式维护",
        publication_ref="publication:one",
        published_at=now,
    )
    query = MagicMock()
    query.resolve_maintenance_target.return_value = row
    query.read_official_contacts.return_value = SimpleNamespace(status="known", contacts=(fact,))
    boundary = MagicMock()
    boundary.preflight.side_effect = lambda **kw: PreflightReportV1(
        plan_sha256=canonical_plan_sha256(kw["change_set"]),
        is_clean=True,
        issues=(),
    )
    boundary.prepare_confirmation.side_effect = lambda **kw: ConfirmationReceiptV1(
        plan_sha256=canonical_plan_sha256(kw["change_set"]),
        credential="C" * 43,
        expires_at=now + timedelta(minutes=5),
    )
    service = OfficialContactProductService(
        boundary=boundary,
        target_resolver=lambda **kw: module.resolve_contact_target(query=query, **kw),
        require_maintenance=lambda _: None,
        plan_store=InMemorySchoolFactPlanStore(),
    )
    proposal = service.prepare(
        actor=actor,
        session_binding="isolated-session",
        request_id=uuid4(),
        payload=ContactChangeInput(
            contact_ref=fact.fact_ref,
            contact_value="010-00000001",
            service_hours=None,
            reason="更正合成联系",
        ),
    )
    assert proposal.summary.before.contact_value == row.contact_value
    assert proposal.summary.after.contact_value == "010-00000001"
    change = boundary.prepare_confirmation.call_args.kwargs["change_set"].changes[0]
    assert "valid_to" not in change.content.model_fields_set
    assert change.content.location_source_key == row.campus_source_key
    boundary.confirm.assert_not_called()


def test_explicit_model_uses_approved_budget_and_key_without_calling_provider(
    document, tmp_path, monkeypatch
):
    module = api("school_fact_product")
    document["model"] = approved_model_document(tmp_path)
    config = api().SchoolFactProductConfiguration.model_validate_json(json.dumps(document))
    permit, model = MagicMock(), MagicMock()
    make_permit, make_model = MagicMock(return_value=permit), MagicMock(return_value=model)
    monkeypatch.setattr(module, "SqliteModelCallPermit", make_permit)
    monkeypatch.setattr(module, "DeepSeekAssistantModel", make_model)
    assert module.build_product_model(config.model) is model
    assert make_permit.call_args.kwargs["path"] == tmp_path / "model-budget.sqlite3"
    assert make_permit.call_args.kwargs["policy"].cap_fen == 5000
    kwargs = make_model.call_args.kwargs
    assert kwargs["permit"] is permit
    assert kwargs["model"] == "deepseek-v4-flash"
    assert kwargs["api_key"].get_secret_value() == "unit-only-api-key"
    assert (kwargs["max_input_tokens"], kwargs["max_output_tokens"]) == (32768, 16384)
    permit.reserve.assert_not_called()
    model.respond.assert_not_called()


def test_model_configuration_rejects_output_above_adapter_limit(document, tmp_path):
    document["model"] = approved_model_document(tmp_path)
    document["model"]["max_output_tokens"] = 32769
    with pytest.raises(ValidationError):
        api().SchoolFactProductConfiguration.model_validate_json(json.dumps(document))


@pytest.mark.parametrize(
    "field,value",
    [
        ("model", "deepseek-v4-pro"),
        ("thinking", "disabled"),
        ("reasoning_effort", "max"),
        ("chat_retention_hours", 25),
        ("chat_retention_hours", 0),
        ("chat_retention_hours", True),
        ("api_key_file", "relative/key"),
        ("api_key", "forbidden-inline-key"),
        ("retention_days", 30),
    ],
)
def test_model_configuration_rejects_unapproved_model_or_retention(
    document, tmp_path, field, value
):
    document["model"] = approved_model_document(tmp_path)
    document["model"][field] = value
    with pytest.raises(ValidationError):
        api().SchoolFactProductConfiguration.model_validate_json(json.dumps(document))


@pytest.mark.parametrize(
    "field,value",
    [("cap_fen", 5001), ("input_cny_per_million", "1.5"), ("output_cny_per_million", "4.5")],
)
def test_product_budget_must_use_approved_cap_and_conservative_peak_rates(
    document, tmp_path, field, value
):
    document["model"] = approved_model_document(tmp_path)
    document["model"]["budget"][field] = value
    with pytest.raises(ValidationError):
        api().SchoolFactProductConfiguration.model_validate_json(json.dumps(document))


@pytest.mark.parametrize(
    "bad_key",
    [None, b"", b" " * 20, b"x" * 4097, b"secret\r\nheader", b"\xff"],
    ids=["missing", "empty", "blank", "oversized", "multiline", "invalid-utf8"],
)
def test_key_file_failures_are_safe_and_do_not_create_a_budget(
    document, tmp_path, monkeypatch, bad_key
):
    module = api("school_fact_product")
    document["model"] = approved_model_document(tmp_path)
    key_path = tmp_path / "unit-provider-secret"
    if bad_key is None:
        key_path.unlink()
    else:
        key_path.write_bytes(bad_key)
    configuration = api().SchoolFactProductConfiguration.model_validate_json(json.dumps(document))
    make_permit, make_model = MagicMock(), MagicMock()
    monkeypatch.setattr(module, "SqliteModelCallPermit", make_permit)
    monkeypatch.setattr(module, "DeepSeekAssistantModel", make_model)
    with pytest.raises(api().ProductConfigurationError) as failure:
        module.build_product_model(configuration.model)
    message = str(failure.value)
    assert str(tmp_path) not in message and "unit-provider-secret" not in message
    assert "secret" not in message and "header" not in message
    assert failure.value.__cause__ is None
    assert failure.value.__context__ is None
    make_permit.assert_not_called()
    make_model.assert_not_called()


def test_budget_startup_failure_does_not_expose_paths_or_configuration(
    document, tmp_path, monkeypatch
):
    module = api("school_fact_product")
    document["model"] = approved_model_document(tmp_path)
    configuration = api().SchoolFactProductConfiguration.model_validate_json(json.dumps(document))
    monkeypatch.setattr(
        module,
        "SqliteModelCallPermit",
        MagicMock(side_effect=RuntimeError(f"internal {tmp_path} unit-only-api-key")),
    )
    with pytest.raises(api().ProductConfigurationError) as failure:
        module.build_product_model(configuration.model)
    assert str(tmp_path) not in str(failure.value)
    assert "unit-only-api-key" not in str(failure.value)
    assert failure.value.__cause__ is None
    assert failure.value.__context__ is None


def test_key_loader_discards_original_decode_error_and_key_bytes(document, tmp_path):
    module = api()
    document["model"] = approved_model_document(tmp_path)
    configuration = module.SchoolFactProductConfiguration.model_validate_json(json.dumps(document))
    key_path = tmp_path / "unit-provider-secret"
    key_path.write_bytes(b"unit-only-api-key-canary\xff")
    with pytest.raises(module.ProductConfigurationError) as failure:
        module.load_product_model_api_key(configuration.model)
    assert str(failure.value) == "F1 model configuration is unavailable."
    assert failure.value.__cause__ is None
    assert failure.value.__context__ is None


def test_f2_factory_publishes_only_exact_directory_consumers(document, monkeypatch):
    module = api("school_fact_product")
    document["purpose"] = "school-facts-f2-synthetic"
    document["runtime"]["postgres_schema_contract"] = "school-facts-f2-v25"
    document["reader"]["schema_contract"] = "school-facts-f2-v25"
    document["directory"] = {"approval_reference": "test-f2", "scope_bindings": []}
    configuration = api().SchoolFactProductConfiguration.model_validate_json(json.dumps(document))
    monkeypatch.setattr(module, "load_school_fact_product_configuration", lambda: configuration)
    services, identity = MagicMock(), MagicMock()
    monkeypatch.setattr(module, "build_engine", lambda _: MagicMock())
    monkeypatch.setattr(module, "verify_runtime_context_signing_key", lambda *_: None)
    monkeypatch.setattr(module, "build_session_factory", lambda _: MagicMock())
    monkeypatch.setattr(module, "Redis", lambda **_: MagicMock())
    monkeypatch.setattr(module, "create_school_fact_product_services", lambda **_: services)
    make_identity = MagicMock(return_value=identity)
    monkeypatch.setattr(module, "build_school_fact_product_identity_service", make_identity)
    monkeypatch.setattr(module, "create_directory_read_components", lambda **_: MagicMock())
    monkeypatch.setattr(module, "SchoolFactEphemeralStoreJanitor", lambda *_a, **_kw: MagicMock())
    make_loop = MagicMock(wraps=module.AssistantToolLoop)
    monkeypatch.setattr(module, "AssistantToolLoop", make_loop)
    app = module.create_school_fact_product_app()
    paths = app.openapi()["paths"]
    assert "/api/v1/school-fact-product/contacts/query" not in paths
    assert "/api/v1/school-fact-product/contacts/prepare" not in paths
    assert "/api/v1/school-fact-product/receipts/{proposal_ref}" in paths
    gateway = make_loop.call_args.kwargs["gateway_factory"]("private-test")
    assert all(item.owner == "school_facts.directory" for item in gateway.descriptors)
    assert make_identity.call_args.kwargs["can_maintain"](MagicMock()) is False
    services.maintenance_guard.require_current.assert_not_called()
    from app.composition.school_fact_product_identity import ProductSessionView

    identity.session_view.return_value = ProductSessionView(status="signed_out", accounts=())
    with TestClient(app, base_url=document["allowed_origin"], client=("127.0.0.1", 5000)) as client:
        assert client.get("/api/v1/school-fact-product/session").json()["directory_enabled"] is True
        for action in ("query", "prepare"):
            assert (
                client.post(f"/api/v1/school-fact-product/contacts/{action}", json={}).status_code
                == 404
            )
    services.query_repository.read_official_contacts.assert_not_called()


def current_model_document(tmp_path):
    doc = approved_model_document(tmp_path)
    doc["model"] = "deepseek-flash"
    doc["current_approval"] = dict(
        model="deepseek-flash",
        approval_reference=doc["approval_reference"],
        price_verified_on="2026-09-12",
    )
    return doc


def test_current_model_configuration_preserves_original_ledger_identity(tmp_path):
    old = api().ApprovedProductModel.model_validate_json(
        json.dumps(approved_model_document(tmp_path))
    )
    current = api().ApprovedProductModel.model_validate_json(
        json.dumps(current_model_document(tmp_path))
    )
    assert current.current_approval.model == "deepseek-flash"
    assert current.budget == old.budget
    assert current.budget.fingerprint == old.budget.fingerprint


@pytest.mark.parametrize("change", ["missing", "reference", "model", "old"])
def test_current_model_configuration_rejects_mixed_approval(tmp_path, change):
    doc = current_model_document(tmp_path)
    if change == "missing":
        del doc["current_approval"]
    elif change == "reference":
        doc["current_approval"]["approval_reference"] = "other"
    elif change == "model":
        doc["current_approval"]["model"] = "deepseek-v4-flash"
    else:
        doc["model"] = "deepseek-v4-flash"
    with pytest.raises(ValidationError):
        api().ApprovedProductModel.model_validate_json(json.dumps(doc))


def test_current_model_factory_binds_current_permit_and_original_ledger(tmp_path, monkeypatch):
    module = api("school_fact_product")
    config = api().ApprovedProductModel.model_validate_json(
        json.dumps(current_model_document(tmp_path))
    )
    permit, model = MagicMock(), MagicMock()
    make_permit = MagicMock(return_value=permit)
    make_old_permit = MagicMock()
    make_model = MagicMock(return_value=model)
    monkeypatch.setattr(module, "SqliteCurrentModelCallPermit", make_permit)
    monkeypatch.setattr(module, "SqliteModelCallPermit", make_old_permit)
    monkeypatch.setattr(module, "DeepSeekAssistantModel", make_model)
    assert module.build_product_model(config) is model
    make_permit.assert_called_once_with(
        path=tmp_path / "model-budget.sqlite3",
        ledger_policy=config.budget,
        current_approval=config.current_approval,
    )
    make_old_permit.assert_not_called()
    assert make_model.call_args.kwargs["current_approval"] == config.current_approval
    assert make_model.call_args.kwargs["permit"] is permit
    permit.reserve.assert_not_called()
    model.respond.assert_not_called()


@pytest.mark.parametrize("enabled", [False, True])
def test_f3_factory_keeps_schema27_f2_and_only_mounts_family_when_enabled(
    monkeypatch, tmp_path, enabled
):
    from tests.assistant.test_f3_product_configuration import f3_document

    module = api("school_fact_product")
    document = f3_document(tmp_path)
    document["family"]["enabled"] = enabled
    configuration = api().SchoolFactProductConfiguration.model_validate_json(json.dumps(document))
    monkeypatch.setattr(module, "load_school_fact_product_configuration", lambda: configuration)
    services, identity = MagicMock(), MagicMock()
    monkeypatch.setattr(module, "build_engine", lambda _: MagicMock())
    monkeypatch.setattr(module, "verify_runtime_context_signing_key", lambda *_: None)
    monkeypatch.setattr(module, "build_session_factory", lambda _: MagicMock())
    monkeypatch.setattr(module, "Redis", lambda **_: MagicMock())
    monkeypatch.setattr(module, "create_school_fact_product_services", lambda **_: services)
    make_identity = MagicMock(return_value=identity)
    monkeypatch.setattr(module, "build_school_fact_product_identity_service", make_identity)
    monkeypatch.setattr(module, "create_directory_read_components", lambda **_: MagicMock())
    make_family_query = MagicMock()
    make_family_staff = MagicMock()
    make_correction_client = MagicMock()
    monkeypatch.setattr(module, "FamilyCorrectionExecutorClient", make_correction_client)
    monkeypatch.setattr(module, "create_family_query_service", make_family_query)
    monkeypatch.setattr(module, "create_family_staff_components", make_family_staff)
    monkeypatch.setattr(module, "SchoolFactEphemeralStoreJanitor", lambda *_a, **_kw: MagicMock())
    make_loop = MagicMock(return_value=MagicMock())
    app = module.create_school_fact_product_app(assistant_loop_factory=make_loop)
    paths = set(app.openapi()["paths"])
    assert configuration.runtime.postgres_schema_contract == "school-facts-f3-v27"
    assert make_identity.call_args.kwargs["family_enabled"] is enabled
    assert any("/family/" in path or "/family-facts/" in path for path in paths) is enabled
    assert "/api/v1/school-fact-product/facts/query" in paths
    assert "/api/v1/school-fact-product/facts/history-receipts" in paths
    if enabled:
        assert "/api/v1/school-fact-product/family/corrections/options" in paths
        assert "/api/v1/school-fact-product/family-facts/corrections/{report_id}/resolve" in paths
        make_correction_client.assert_called_once_with(settings=configuration.executor)
    else:
        gateway = make_loop.call_args.kwargs["gateway_factory"]("staff")
        assert gateway.descriptors
        assert all(item.owner == "school_facts.directory" for item in gateway.descriptors)
        make_family_query.assert_not_called()
        make_family_staff.assert_not_called()
        make_correction_client.assert_not_called()
