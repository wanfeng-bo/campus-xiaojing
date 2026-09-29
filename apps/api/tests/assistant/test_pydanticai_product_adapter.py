"""Supported F2 adapters load without the historical candidate or experiment packages."""

from __future__ import annotations

import builtins
import importlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

pytest.importorskip("pydantic_ai", reason="requires the fixed f2-assistant optional dependencies")

SUPPORTED = (
    "app.platform.assistant.pydanticai_runner",
    "app.platform.assistant.pydanticai_history",
    "app.platform.assistant.pydanticai_errors",
    "app.composition.school_fact_pydanticai_model",
    "app.composition.school_fact_pydanticai",
)


@pytest.mark.parametrize("name", SUPPORTED)
def test_f2_adapter_has_a_supported_import_path(name: str) -> None:
    assert importlib.util.find_spec(name) is not None, f"missing supported adapter: {name}"


def test_supported_imports_do_not_load_candidate_or_experiment_packages() -> None:
    for name in SUPPORTED:
        assert importlib.util.find_spec(name) is not None, f"missing supported adapter: {name}"
    code = """
import builtins
import importlib
import json
import importlib.abc
import sys

class BlockHistoricalPackages(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'candidates', 'experiments'}:
            raise AssertionError('unsupported runtime dependency: ' + fullname)
        return None

sys.meta_path.insert(0, BlockHistoricalPackages())
for name in sys.argv[1:]:
    importlib.import_module(name)
assert not any(name.split('.')[0] in {'candidates', 'experiments'} for name in sys.modules)
"""
    dependencies = [str(Path(item).resolve()) for item in sys.path if item]
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; sys.path[:] = " + repr(dependencies) + "\n" + code,
            *SUPPORTED,
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def _product_factory_environment(monkeypatch, tmp_path, *, f2=True, model=True):
    from app.composition import school_fact_product as product
    from app.composition.school_fact_product_settings import SchoolFactProductConfiguration
    from tests.assistant.test_school_fact_product_factory import current_model_document, document

    data = document.__wrapped__()
    if f2:
        data["purpose"] = "school-facts-f2-synthetic"
        data["runtime"]["postgres_schema_contract"] = "school-facts-f2-v25"
        data["reader"]["schema_contract"] = "school-facts-f2-v25"
        data["directory"] = {"approval_reference": "unit-f2", "scope_bindings": []}
    if model:
        data["model"] = current_model_document(tmp_path)
    configuration = SchoolFactProductConfiguration.model_validate_json(json.dumps(data))
    engine, services, redis, identity, directory = (MagicMock() for _ in range(5))
    monkeypatch.setattr(product, "load_school_fact_product_configuration", lambda: configuration)
    monkeypatch.setattr(product, "build_engine", lambda _: engine)
    monkeypatch.setattr(product, "verify_runtime_context_signing_key", lambda *_: None)
    monkeypatch.setattr(product, "build_session_factory", lambda _: MagicMock())
    monkeypatch.setattr(product, "Redis", lambda **_: redis)
    monkeypatch.setattr(product, "create_school_fact_product_services", lambda **_: services)
    monkeypatch.setattr(product, "build_school_fact_product_identity_service", lambda **_: identity)
    monkeypatch.setattr(product, "create_directory_read_components", lambda **_: directory)
    monkeypatch.setattr(product, "SchoolFactEphemeralStoreJanitor", lambda *_a, **_kw: MagicMock())
    make_router = MagicMock(wraps=product.create_assistant_router)
    make_directory = MagicMock(wraps=product.DirectoryFactProductService)
    monkeypatch.setattr(product, "create_assistant_router", make_router)
    monkeypatch.setattr(product, "DirectoryFactProductService", make_directory)
    return product, configuration, make_router, make_directory, directory, (engine, services, redis)


def test_normal_f2_uses_supported_runner_and_original_budget_without_model_send(
    monkeypatch, tmp_path
):
    from fastapi.testclient import TestClient
    from pydantic import SecretStr

    from app.composition import school_fact_pydanticai as supported
    from app.platform.assistant.pydanticai_runner import SynchronousCandidateRunner

    product, config, router, make_directory, directory, resources = _product_factory_environment(
        monkeypatch, tmp_path
    )
    permit = MagicMock()
    make_permit = MagicMock(return_value=permit)
    monkeypatch.setattr(supported, "load_product_model_api_key", lambda _: SecretStr("unit-only"))
    monkeypatch.setattr(supported, "SqliteCurrentModelCallPermit", make_permit)
    old_model = MagicMock(side_effect=AssertionError("legacy model must not initialize"))
    monkeypatch.setattr(product, "build_product_model", old_model)
    app = product.create_school_fact_product_app()
    runner = router.call_args.kwargs["turn_orchestrator"]
    assert isinstance(runner, SynchronousCandidateRunner)
    assert runner.runner.final_review_instruction == supported.FINAL_REVIEW_INSTRUCTION
    assert runner.runner.instruction == (
        product.DIRECTORY_ASSISTANT_INSTRUCTION + product.F1_ASSISTANT_INSTRUCTION
    )
    assert runner.runner.control_service is router.call_args.kwargs["control_service"]
    gateway = runner.runner.gateway_factory("unit-session")
    assert {descriptor.owner for descriptor in gateway.descriptors} == {"school_facts.directory"}
    assert make_directory.call_args.kwargs["receipt_reference_for"] is directory.query.reference_for
    assert make_directory.call_args.kwargs["receipt_fact_labels"] is product.FACT_LABELS
    make_permit.assert_called_once_with(
        path=tmp_path / "model-budget.sqlite3",
        ledger_policy=config.model.budget,
        current_approval=config.model.current_approval,
    )
    permit.reserve.assert_not_called()
    old_model.assert_not_called()
    paths = app.openapi()["paths"]
    assert "/api/v1/school-fact-product/facts/history-receipts" in paths
    assert "/api/v1/school-fact-product/facts/proposals/{proposal_ref}/confirm" in paths
    with TestClient(app):
        pass
    resources[0].dispose.assert_called_once()
    resources[1].close.assert_called_once()
    resources[2].close.assert_called_once()


def _block_supported_factory_import(monkeypatch):
    original = builtins.__import__

    def unavailable(name, *args, **kwargs):
        if name == "app.composition.school_fact_pydanticai":
            raise ModuleNotFoundError("pydantic_ai unavailable", name="pydantic_ai")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", unavailable)


@pytest.mark.parametrize("f2,model", [(False, True), (False, False), (True, False)])
def test_f1_and_no_model_workbench_keep_legacy_runner_without_optional_import(
    monkeypatch, tmp_path, f2, model
):
    from fastapi.testclient import TestClient

    product, config, router, *_ = _product_factory_environment(
        monkeypatch, tmp_path, f2=f2, model=model
    )
    old_model = MagicMock(return_value=product.DisabledModel())
    monkeypatch.setattr(product, "build_product_model", old_model)
    _block_supported_factory_import(monkeypatch)
    app = product.create_school_fact_product_app()
    assert isinstance(router.call_args.kwargs["turn_orchestrator"], product.AssistantToolLoop)
    old_model.assert_called_once_with(config.model)
    with TestClient(app):
        pass


def test_explicit_comparison_factory_precedes_normal_f2_even_without_optional_import(
    monkeypatch, tmp_path
):
    from fastapi.testclient import TestClient

    product, config, router, *_ = _product_factory_environment(monkeypatch, tmp_path)
    factory = MagicMock(return_value=MagicMock())
    old_model = MagicMock(side_effect=AssertionError("legacy model must not initialize"))
    monkeypatch.setattr(product, "build_product_model", old_model)
    _block_supported_factory_import(monkeypatch)
    app = product.create_school_fact_product_app(assistant_loop_factory=factory)
    assert router.call_args.kwargs["turn_orchestrator"] is factory.return_value
    assert factory.call_args.kwargs["configuration"] is config
    old_model.assert_not_called()
    with TestClient(app):
        pass


def test_normal_f2_missing_optional_dependencies_fails_closed_and_closes_resources(
    monkeypatch, tmp_path
):
    product, _, _, _, _, resources = _product_factory_environment(monkeypatch, tmp_path)
    old_model = MagicMock(return_value=product.DisabledModel())
    monkeypatch.setattr(product, "build_product_model", old_model)
    _block_supported_factory_import(monkeypatch)
    with pytest.raises(ModuleNotFoundError, match="pydantic_ai unavailable"):
        product.create_school_fact_product_app()
    old_model.assert_not_called()
    resources[0].dispose.assert_called_once()
    resources[1].close.assert_called_once()
    resources[2].close.assert_called_once()


def test_f3_supported_assembly_binds_original_ledger_and_stage_cap_without_send(
    monkeypatch, tmp_path
):
    from pydantic import SecretStr

    from app.composition import school_fact_pydanticai as supported
    from app.composition.school_fact_product_settings import SchoolFactProductConfiguration
    from tests.assistant.test_f3_product_configuration import f3_document

    config = SchoolFactProductConfiguration.model_validate_json(json.dumps(f3_document(tmp_path)))
    permit = MagicMock()
    make_permit = MagicMock(return_value=permit)
    monkeypatch.setattr(supported, "load_product_model_api_key", lambda _: SecretStr("unit-only"))
    monkeypatch.setattr(supported, "SqliteCurrentModelCallPermit", make_permit)
    runner = supported.build_pydanticai_loop(
        configuration=config,
        gateway_factory=MagicMock(),
        control_service=MagicMock(),
        instruction="unit",
    )
    assert runner.runner.instruction == "unit"
    make_permit.assert_called_once_with(
        path=tmp_path / "model-budget.sqlite3",
        ledger_policy=config.model.budget,
        current_approval=config.model.current_approval,
        stage_budget=config.model.stage_budget,
    )
    permit.reserve.assert_not_called()


def test_f3_product_selects_official_framework_and_parent_tools_only(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from pydantic import SecretStr

    from app.composition import school_fact_pydanticai as supported
    from app.composition.school_fact_product_settings import SchoolFactProductConfiguration
    from app.platform.assistant.pydanticai_runner import SynchronousCandidateRunner
    from tests.assistant.test_f3_product_configuration import f3_document
    from tests.test_f3_product_identity import guardian

    product, _, router, *_ = _product_factory_environment(monkeypatch, tmp_path)
    config = SchoolFactProductConfiguration.model_validate_json(json.dumps(f3_document(tmp_path)))
    janitor = MagicMock()
    monkeypatch.setattr(product, "SchoolFactEphemeralStoreJanitor", janitor)
    monkeypatch.setattr(product, "load_school_fact_product_configuration", lambda: config)
    identity = MagicMock()
    from app.composition.school_fact_product_identity import ProductFamilyActor

    parent = guardian()
    identity.resolve_private_actor.return_value = SimpleNamespace(actor=parent)
    identity.resolve_assistant_identity.return_value = SimpleNamespace(
        actor=ProductFamilyActor(
            account_user_id=parent.account_user_id,
            school_id=parent.school_id,
            person_id=parent.person_id,
            authorization_version=parent.authorization_version,
        ),
        session_binding="parent-session",
    )
    make_identity = MagicMock(return_value=identity)
    monkeypatch.setattr(product, "build_school_fact_product_identity_service", make_identity)
    monkeypatch.setattr(product, "create_family_query_service", lambda **kw: MagicMock())
    monkeypatch.setattr(product, "create_family_staff_components", lambda **kw: MagicMock())
    monkeypatch.setattr(supported, "load_product_model_api_key", lambda _: SecretStr("unit"))
    monkeypatch.setattr(supported, "SqliteCurrentModelCallPermit", MagicMock())
    old = MagicMock(side_effect=AssertionError("F3 must not initialize old loop"))
    monkeypatch.setattr(product, "build_product_model", old)
    app = product.create_school_fact_product_app()
    staff_store = next(
        store
        for store in janitor.call_args.args
        if isinstance(store, product.FamilyStaffToolGateway)
    )
    assert staff_store.retention_seconds == config.model.chat_retention_hours * 3600 <= 86400
    runner = router.call_args.kwargs["turn_orchestrator"]
    assert isinstance(runner, SynchronousCandidateRunner)
    assert make_identity.call_args.kwargs["family_enabled"] is True
    gateway = runner.runner.gateway_factory("selected-parent")
    assert [d.name for d in gateway.descriptors] == [
        "school_facts.family.children",
        "school_facts.family.profile",
        "school_facts.family.corrections",
    ]
    assert callable(runner.runner.instruction)
    parent_instruction = runner.runner.instruction("selected-parent")
    assert product.FAMILY_ASSISTANT_INSTRUCTION in parent_instruction
    assert product.DIRECTORY_ASSISTANT_INSTRUCTION not in parent_instruction
    from tests.test_f2_directory_product import actor as staff_actor

    identity.resolve_assistant_identity.return_value = SimpleNamespace(
        actor=staff_actor(), session_binding="staff-session"
    )
    staff_instruction = runner.runner.instruction("selected-staff")
    assert product.FAMILY_ASSISTANT_INSTRUCTION not in staff_instruction
    assert product.DIRECTORY_ASSISTANT_INSTRUCTION in staff_instruction
    assert "同名" in staff_instruction and "返回的对象引用" in staff_instruction
    identity.resolve_assistant_identity.return_value = SimpleNamespace(actor=object())
    from app.platform.assistant.runtime import AssistantRuntimeError

    with pytest.raises(AssistantRuntimeError):
        runner.runner.instruction("unknown-owner")
    assert "/api/v1/school-fact-product/family/profile" in app.openapi()["paths"]
    assert "/api/v1/school-fact-product/family-facts/query" in app.openapi()["paths"]
    assert app.version == "f3"
    old.assert_not_called()


def test_staff_identity_adapter_rejects_parent_without_coercion():
    from app.composition.school_fact_product import require_staff_product_identity
    from app.composition.school_fact_product_identity import ProductIdentity
    from app.interfaces.http.errors import ApiError
    from tests.test_f3_product_identity import guardian

    with pytest.raises(ApiError) as denied:
        require_staff_product_identity(ProductIdentity(guardian(), "unit", "unit-ref"))
    assert denied.value.status_code == 403


def test_formal_runner_resolves_owner_instruction_for_each_bound_turn():
    import asyncio
    from types import SimpleNamespace
    from uuid import uuid4

    from pydantic_ai.messages import ModelResponse, TextPart
    from pydantic_ai.models.function import FunctionModel

    from app.platform.assistant.pydanticai_runner import CandidateToolRunner
    from app.platform.assistant.runtime import AssistantRuntime

    seen, resolved = [], []
    instructions = {"opaque-a": "first-owner-instruction", "opaque-b": "second-owner-instruction"}

    def choose(ref):
        resolved.append(ref)
        return instructions[ref]

    async def response(messages, _info):
        seen.append(str(messages))
        return ModelResponse(parts=[TextPart("已回应")])

    runner = CandidateToolRunner(
        model_factory=lambda _current: FunctionModel(response),
        gateway_factory=lambda _ref: SimpleNamespace(
            descriptors=(), revalidate=lambda _actor: None
        ),
        instruction=choose,
    )
    for actor_ref in instructions:
        runtime = AssistantRuntime()
        conversation = runtime.create_conversation(actor_ref, uuid4())
        accepted = runtime.accept_turn(
            conversation.conversation_ref, actor_ref, uuid4(), "当前问题"
        )
        asyncio.run(
            runner.run_accepted(
                runtime=runtime, accepted=accepted, actor_context_ref=actor_ref, request_id=uuid4()
            )
        )
    assert resolved == list(instructions)
    assert "first-owner-instruction" in seen[0] and "second-owner-instruction" not in seen[0]
    assert "second-owner-instruction" in seen[1] and "first-owner-instruction" not in seen[1]
    assert all("opaque-a" not in value and "opaque-b" not in value for value in seen)


@pytest.mark.parametrize("review_enabled", [False, True])
def test_f3_runner_can_query_original_receipt_after_control_confirmation_invalidates_history(
    review_enabled,
):
    import asyncio
    from types import SimpleNamespace
    from uuid import uuid4

    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
    from pydantic_ai.models.function import FunctionModel

    from app.composition.school_fact_family_staff_gateway import MODULE, FamilyStaffToolGateway
    from app.composition.school_fact_product_contacts import ContactProductUnavailable
    from app.core.request_id import assign_request_id
    from app.interfaces.http.assistant import AssistantRequestIdentity, create_assistant_router
    from app.platform.assistant.catalog import AssistantModuleCatalogEntry, StaticAssistantCatalog
    from app.platform.assistant.controls import AssistantControlService
    from app.platform.assistant.pydanticai_runner import (
        CandidateToolRunner,
        SynchronousCandidateRunner,
    )
    from app.platform.assistant.runtime import AssistantRuntime
    from tests.assistant.test_control_service import _MemoryControlStore
    from tests.test_f2_directory_product import actor
    from tests.test_f3_family_staff_gateway import Product, Query

    class FormalProduct(Product):
        def __init__(self):
            self.confirmations = 0
            self.receipts = []

        def proposal(self, **kwargs):
            if self.confirmations:
                raise ContactProductUnavailable("preview retired after confirmation")
            return super().proposal(**kwargs)

        def confirm(self, **kwargs):
            self.confirmations += 1
            return self.receipt(**kwargs)

        def receipt(self, **kwargs):
            self.receipts.append(kwargs["proposal_ref"])
            return SimpleNamespace(
                status="succeeded",
                model_dump=lambda **_: {
                    "reference": kwargs["proposal_ref"],
                    "status": "succeeded",
                    "message": "原操作完成",
                    "changed_count": 1,
                    "target_version": 22,
                    "request_id": None,
                },
            )

        def own_receipts(self, **_kwargs):
            raise AssertionError("known original operation must not require enumeration")

    product = FormalProduct()
    gateway = FamilyStaffToolGateway(
        resolve_actor=lambda _: SimpleNamespace(actor=actor(), session_binding="session"),
        query=Query(),
        product=product,
    )
    runtime = AssistantRuntime()
    controls = AssistantControlService(
        catalog=StaticAssistantCatalog(
            (
                AssistantModuleCatalogEntry(
                    module_ref=MODULE,
                    model_safe_capability="正式事实维护",
                    module=gateway,
                ),
            )
        ),
        store=_MemoryControlStore(),
        signing_keys={"test": b"k" * 32},
        active_key_id="test",
    )
    selections = {d.name: f"tool_{index}" for index, d in enumerate(gateway.descriptors)}
    responses = iter(
        [
            ModelResponse(
                parts=[
                    ToolCallPart(
                        "search_tools",
                        {"queries": [selections[MODULE + ".prepare"]]},
                        "load-prepare",
                    )
                ]
            ),
            ModelResponse(
                parts=[
                    ToolCallPart(
                        selections[MODULE + ".prepare"],
                        {
                            "fact_kind": "person_region",
                            "target_ref": "f3:person_region:private",
                            "fields": {"district_name": "余杭区", "district_code": "330110"},
                            "reason": "已核实资料更正",
                        },
                        "prepare",
                    )
                ]
            ),
            ModelResponse(parts=[TextPart("请核对并确认候选")]),
            ModelResponse(
                parts=[
                    ToolCallPart(
                        "search_tools",
                        {"queries": [selections[MODULE + ".receipt"]]},
                        "load-receipt",
                    )
                ]
            ),
            ModelResponse(parts=[ToolCallPart(selections[MODULE + ".receipt"], {}, "receipt")]),
            ModelResponse(parts=[TextPart("原回执已核对")]),
            ModelResponse(
                parts=[
                    ToolCallPart(
                        "search_tools",
                        {"queries": [selections[MODULE + ".receipt"]]},
                        "load-other-receipt",
                    )
                ]
            ),
            ModelResponse(
                parts=[ToolCallPart(selections[MODULE + ".receipt"], {}, "other-receipt")]
            ),
            ModelResponse(parts=[TextPart("当前身份会话没有可定位的原操作")]),
        ]
    )
    seen = []
    returned = {}
    pending_review = []

    async def answer(messages, info):
        seen.append(str(messages))
        returned.update(
            {
                part.tool_call_id: part.content
                for message in messages
                for part in message.parts
                if isinstance(part, ToolReturnPart)
            }
        )
        if pending_review:
            return pending_review.pop()
        response = next(responses)
        for part in response.parts:
            if isinstance(part, ToolCallPart):
                assert part.tool_name in {tool.name for tool in info.function_tools}
            elif isinstance(part, TextPart) and part.content == "原回执已核对":
                assert returned["receipt"]["kind"] == "fact_receipt"
                assert returned["receipt"]["outcome"] == "正式修改已完成"
                assert returned["receipt"]["message"] == "原操作完成"
                assert returned["receipt"]["changed_count"] == 1
                assert returned["receipt"]["tool_context"] == {
                    "action_number": 1,
                    "action_owner": MODULE,
                    "school_fact_version": 22,
                }
                assert original_ref not in str(returned["receipt"])
            elif isinstance(part, TextPart) and part.content == "当前身份会话没有可定位的原操作":
                assert returned["other-receipt"]["status"] == "clarification_required"
        if review_enabled and any(isinstance(part, TextPart) for part in response.parts):
            pending_review.append(response)
            return ModelResponse(parts=[TextPart("UNPUBLISHED_CONTINUATION_DRAFT")])
        return response

    runner = CandidateToolRunner(
        model_factory=lambda _: FunctionModel(answer),
        gateway_factory=gateway.for_turn,
        control_service=controls,
        final_review_instruction="Review only the answer" if review_enabled else None,
    )
    conversation = runtime.create_conversation("identity", uuid4())

    def turn(text):
        accepted = runtime.accept_turn(conversation.conversation_ref, "identity", uuid4(), text)
        return asyncio.run(
            runner.run_accepted(
                runtime=runtime,
                accepted=accepted,
                actor_context_ref="identity",
                request_id=uuid4(),
            )
        )

    first = turn("准备已经核实的地区更正")
    handle = first.display_ledger[-1].controls[0].handle
    assert product.confirmations == 0
    app = FastAPI()
    app.middleware("http")(assign_request_id)
    app.include_router(
        create_assistant_router(
            runtime=runtime,
            control_service=controls,
            turn_orchestrator=SynchronousCandidateRunner(runner),
            identity_dependency=lambda: AssistantRequestIdentity(actor_context_ref="identity"),
        )
    )
    with TestClient(app) as client:
        clicked = client.post(
            f"/api/v1/assistant/conversations/{conversation.conversation_ref}/controls/action-offers/{handle}"
        )
        assert clicked.status_code == 200
    assert product.confirmations == 1
    original_ref = product.receipts[0]
    second_start = len(seen)
    second = turn("核对刚才的原回执")
    payload = second.display_ledger[-1].display_payload
    assert payload["facts"][0]["reference"] == original_ref
    assert payload["facts"][0]["target_version"] == 22
    assert product.confirmations == 1 and set(product.receipts) == {original_ref}
    assert len(product.receipts) > 1
    assert "history_context_unavailable" in seen[second_start]
    assert "UNPUBLISHED_CONTINUATION_DRAFT" not in seen[second_start]
    assert original_ref not in seen[second_start]
    receipt_reads = len(product.receipts)
    other = runtime.create_conversation("other-identity", uuid4())
    accepted = runtime.accept_turn(
        other.conversation_ref, "other-identity", uuid4(), "核对刚才的原回执"
    )
    isolated = asyncio.run(
        runner.run_accepted(
            runtime=runtime,
            accepted=accepted,
            actor_context_ref="other-identity",
            request_id=uuid4(),
        )
    )
    assert isolated.display_ledger[-1].display_payload["facts"] == []
    assert "clarification_required" in seen[-1] and original_ref not in seen[-1]
    assert len(product.receipts) == receipt_reads and product.confirmations == 1


def test_tool_schema_presentation_removes_only_generated_annotations():
    from copy import deepcopy

    from app.platform.assistant.pydanticai_runner import _tool_schema_presentation

    original = {
        "type": "object",
        "title": "Business request",
        "required": ["title"],
        "additionalProperties": False,
        "properties": {
            "title": {"type": "string", "title": "Title", "minLength": 2},
            "count": {"type": "integer", "title": "Count", "minimum": 1},
            "custom": {"type": "string", "title": "Important business meaning"},
        },
        "$defs": {"Nested": {"title": "Nested", "type": "string", "enum": ["yes"]}},
        "default": {"title": "Count"},
    }
    saved = deepcopy(original)
    compact = _tool_schema_presentation(original)
    assert original == saved
    assert "title" in compact["properties"]
    assert "title" not in compact["properties"]["title"]
    assert "title" not in compact["$defs"]["Nested"]
    assert compact["title"] == "Business request"
    assert compact["properties"]["custom"]["title"] == "Important business meaning"
    expected = deepcopy(saved)
    del expected["properties"]["title"]["title"]
    del expected["properties"]["count"]["title"]
    del expected["$defs"]["Nested"]["title"]
    assert compact == expected


def test_formal_runner_compacts_schema_but_owner_validation_still_rejects(monkeypatch):
    import asyncio
    from uuid import uuid4

    from pydantic import BaseModel, ConfigDict, Field
    from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
    from pydantic_ai.models.function import FunctionModel

    from app.platform.assistant.pydanticai_runner import CandidateToolRunner
    from app.platform.assistant.runtime import AssistantRuntime
    from app.platform.assistant.tool_loop import ToolDescriptor, ToolExecutionResult

    class Input(BaseModel):
        model_config = ConfigDict(strict=True, extra="forbid")
        count: int = Field(ge=1, le=3)

    schema = Input.model_json_schema()
    calls = []

    class Gateway:
        descriptors = (
            ToolDescriptor(
                name="test.read",
                owner="test",
                kind="read",
                description="Business description unchanged",
                parameters=schema,
            ),
        )

        def revalidate(self, _):
            pass

        def execute(self, call, context):
            calls.append(call.arguments)
            try:
                Input.model_validate(call.arguments)
            except ValueError:
                return ToolExecutionResult(model_content={"status": "rejected"})
            raise AssertionError("invalid input accepted")

    async def answer(messages, info):
        wire = info.function_tools[0].parameters_json_schema
        assert "title" not in wire["properties"]["count"]
        assert wire["properties"]["count"]["minimum"] == 1
        assert wire["additionalProperties"] is False
        assert info.function_tools[0].description == (
            "Business description unchanged [能力标识：test.read]"
        )
        if not calls:
            return ModelResponse(parts=[ToolCallPart("tool_0", {"count": "2"}, "one")])
        return ModelResponse(parts=[TextPart("Rejected invalid value")])

    gateway = Gateway()
    runner = CandidateToolRunner(
        model_factory=lambda _: FunctionModel(answer), gateway_factory=lambda _: gateway
    )
    runtime = AssistantRuntime()
    conversation = runtime.create_conversation("identity", uuid4())
    accepted = runtime.accept_turn(conversation.conversation_ref, "identity", uuid4(), "Check")
    asyncio.run(
        runner.run_accepted(
            runtime=runtime, accepted=accepted, actor_context_ref="identity", request_id=uuid4()
        )
    )
    assert calls == [{"count": "2"}]
    assert schema["properties"]["count"]["title"] == "Count"
