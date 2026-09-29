from __future__ import annotations

import importlib
import importlib.util
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import pytest

from app.composition.school_fact_assistant_gateway import QueryRepository
from app.composition.school_facts import InMemorySchoolFactPlanStore
from app.platform import contracts as actors
from app.platform.contracts import AssignmentId, PersonId, SchoolId, StaffActorContext
from app.platform.school_facts.contracts import (
    CanonicalFactContentV1,
    ConfirmationReceiptV1,
    PreflightReportV1,
    canonical_plan_sha256,
)
from app.platform.school_facts.ports import SchoolFactApplyResult, SchoolFactResultLookup
from app.platform.school_facts.read_policy import OfficialContactFact

NOW = datetime(2026, 9, 5, 8, tzinfo=UTC)


def api():
    path = "app.composition.school_fact_product_contacts"
    assert importlib.util.find_spec(path) is not None, "missing narrow contact product service"
    return importlib.import_module(path)


def actor():
    return actors._issue_staff_actor_context(
        account_user_id=7,
        school_id=SchoolId(UUID(int=11)),
        person_id=PersonId(UUID(int=13)),
        assignment_id=AssignmentId(UUID(int=14)),
        assignment_version=1,
        authorization_version=1,
    )


@dataclass(frozen=True)
class ProductIdentity:
    actor: StaffActorContext
    session_binding: str = "session-one"


def target():
    return SimpleNamespace(
        source_key="engineering.contact.one",
        school_fact_version=3,
        version=1,
        content=CanonicalFactContentV1(
            owner_kind="location",
            location_source_key="engineering.campus.one",
            contact_kind="email",
            contact_value="old@example.invalid",
            service_hours="工作日",
            lifecycle_status="active",
            visibility_scope="public",
        ),
        public_fact=OfficialContactFact(
            fact_ref="official-contact:" + str(UUID(int=9)),
            campus_ref="campus:" + str(UUID(int=12)),
            campus_name="合成第一校区",
            contact_kind="email",
            contact_value="old@example.invalid",
            service_hours="工作日",
            valid_from=NOW,
            valid_to=None,
            version=1,
            source_kind="admin",
            source_description="合成工程资料经正式发布",
            publication_ref="publication:" + str(UUID(int=8)),
            published_at=NOW,
        ),
    )


class Boundary:
    def __init__(self):
        self.confirm_calls = []
        self.prepare_calls = []
        self.preflight_calls = []
        self.applied = None
        self.unknown = False
        self.clean = True

    def preflight(self, **kw):
        self.preflight_calls.append(kw)
        return PreflightReportV1(
            plan_sha256=canonical_plan_sha256(kw["change_set"]), is_clean=self.clean, issues=()
        )

    def prepare_confirmation(self, **kw):
        self.prepare_calls.append(kw)
        return ConfirmationReceiptV1(
            plan_sha256=canonical_plan_sha256(kw["change_set"]),
            credential="C" * 43,
            expires_at=NOW + timedelta(minutes=5),
        )

    def confirm(self, **kw):
        self.confirm_calls.append(kw)
        self.applied = SchoolFactApplyResult(UUID(int=99), 4, 1, False)
        if self.unknown:
            raise TimeoutError("private transport detail")
        return self.applied

    def check_result_by_locator(self, **kw):
        return SchoolFactResultLookup(found=True, result=self.applied)


def setup(*, require=None):
    module = api()
    boundary = Boundary()
    clock = [NOW]
    service = module.OfficialContactProductService(
        boundary=boundary,
        target_resolver=lambda **_: target(),
        require_maintenance=require or (lambda _: None),
        plan_store=InMemorySchoolFactPlanStore(clock=lambda: clock[0]),
        clock=lambda: clock[0],
    )
    payload = module.ContactChangeInput(
        contact_ref=target().public_fact.fact_ref,
        contact_value="new@example.invalid",
        service_hours=None,
        reason="合成工程资料更新",
    )
    return module, service, boundary, clock, payload


@pytest.mark.parametrize("family", ["contact", "directory"])
@pytest.mark.parametrize("outcome", ["succeeded", "unknown", "rejected"])
def test_result_continuation_requires_invocation_and_fresh_original_authority(family, outcome):
    from app.composition.school_fact_assistant_gateway import OfficialContactToolGateway
    from app.composition.school_fact_directory_gateway import DirectoryFactToolGateway
    from app.composition.school_fact_product_contacts import ContactProductUnavailable
    from app.platform.assistant.runtime import AssistantRuntimeError
    from app.platform.assistant.tool_loop import ModelToolCall, ToolCallContext
    from app.platform.school_facts.application import SchoolFactChangeRejected
    from tests.test_f2_directory_product import setup as directory_setup

    allowed = [True]

    def require(_):
        if not allowed[0]:
            raise PermissionError("revoked")

    identity = [ProductIdentity(actor())]
    if family == "contact":
        _, service, boundary, _, payload = setup(require=require)
        gateway = OfficialContactToolGateway(
            resolve_actor=lambda _: identity[0], require_read=require,
            query=SimpleNamespace(), contacts=service, receipt_retention_seconds=60,
        )
        module = "school_facts.official_contacts"
    else:
        service, boundary, allowed, payload = directory_setup()
        gateway = DirectoryFactToolGateway(
            resolve_actor=lambda _: identity[0], query=SimpleNamespace(),
            product=service, retention_seconds=60,
        )
        module = "school_facts.directory"
    context = ToolCallContext("identity", "conversation", "turn", "run", 1, uuid4())
    result = gateway.for_turn("identity").execute(
        ModelToolCall(call_id="prepare", name=module + ".prepare",
                      arguments=payload.model_dump(mode="json")), context,
    )
    assert result.continuation is not None
    continuation = result.continuation
    reference = result.fact_card["proposal_ref"]
    assert continuation.reference == reference
    with pytest.raises(ContactProductUnavailable):
        continuation.load()  # A prepared operation can already have an unknown receipt.
    writes = []

    def confirm(**kwargs):
        writes.append(kwargs)
        if outcome == "rejected":
            raise SchoolFactChangeRejected("private rejection detail")
        if outcome == "unknown":
            raise TimeoutError("private transport detail")
        applied = SchoolFactApplyResult(UUID(int=99), 4, 1, False)
        boundary.applied = boundary.result = applied
        return applied

    boundary.confirm = confirm
    service.confirm(actor=identity[0].actor, session_binding="session-one",
                    proposal_ref=reference, request_id=uuid4())
    if outcome == "unknown":
        with pytest.raises(AssistantRuntimeError, match="not terminal"):
            continuation.load()
        assert len(writes) == 1
        # Recovery reads the same original receipt; it must not invoke confirm again.
        boundary.applied = boundary.result = SchoolFactApplyResult(UUID(int=99), 4, 1, False)
    evidence = continuation.load()
    observed = "succeeded" if outcome == "unknown" else outcome
    assert evidence.fact_card["status"] == observed
    assert evidence.history_check.revalidation_covers_content
    assert evidence.history_check.valid()
    assert "C" * 43 not in str(evidence.model_content)
    assert "private" not in str(evidence.model_content)
    if observed == "succeeded":
        tool_context = evidence.model_content["tool_context"]
        if family == "contact":
            assert tool_context["original_contact_ref"] == payload.contact_ref
            hint = tool_context["read_current"]
            assert hint == {
                "tool": module + ".query",
                "arguments": {"campus_ref": target().public_fact.campus_ref},
            }
            assert "old@example.invalid" not in str(evidence.model_content)
            assert "new@example.invalid" not in str(evidence.model_content)
        else:
            from hashlib import sha256

            assert tool_context["read_original_targets"] == {
                "tool": module + ".own_receipt",
                "arguments": {
                    "receipt_ref": "sf2receipt_" + sha256(reference.encode()).hexdigest(),
                },
            }
    else:
        assert "read_current" not in str(evidence.model_content)
        assert "read_original_targets" not in str(evidence.model_content)
    assert len(writes) == 1
    if observed != "succeeded":
        # A fresh authoritative success takes precedence over a saved rejection.
        boundary.applied = boundary.result = SchoolFactApplyResult(UUID(int=99), 4, 1, False)
        assert not evidence.history_check.valid()
        assert continuation.load().fact_card["status"] == "succeeded"
    identity[0] = ProductIdentity(actor(), session_binding="other-session")
    with pytest.raises(AssistantRuntimeError):
        continuation.load()
    assert not evidence.history_check.valid()
    identity[0] = ProductIdentity(actor())
    allowed[0] = False
    with pytest.raises(PermissionError):
        continuation.load()
    assert not evidence.history_check.valid()
    assert len(writes) == 1


def prepare(service, payload):
    return service.prepare(
        actor=actor(), session_binding="session-one", payload=payload, request_id=uuid4()
    )


def test_prepare_uses_server_target_versions_and_never_writes():
    module, service, boundary, _, payload = setup()
    proposal = prepare(service, payload)
    assert not boundary.confirm_calls
    assert len(boundary.preflight_calls) == len(boundary.prepare_calls) == 1
    change = boundary.prepare_calls[0]["change_set"]
    assert change.base_version == 3
    assert change.changes[0].expected_version == 1
    assert change.changes[0].content.location_source_key == "engineering.campus.one"
    assert change.changes[0].content.service_hours is None
    public = proposal.model_dump_json()
    assert "C" * 43 not in public and "engineering.contact.one" not in public
    assert proposal.summary.before.contact_value == "old@example.invalid"
    assert proposal.summary.after.contact_value == "new@example.invalid"


def test_client_cannot_supply_school_or_version_or_confirmation_credential():
    module, *_ = setup()
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        module.ContactChangeInput(
            contact_ref="x",
            contact_value="y",
            service_hours=None,
            reason="z",
            school_id=str(UUID(int=11)),
            expected_version=99,
        )


def test_explicit_confirm_reuses_original_idempotency_and_receipt_lookup_after_lost_response():
    _, service, boundary, _, payload = setup()
    proposal = prepare(service, payload)
    boundary.unknown = True
    result = service.confirm(
        actor=actor(),
        session_binding="session-one",
        proposal_ref=proposal.proposal_ref,
        request_id=uuid4(),
    )
    assert result.status == "unknown"
    assert "private" not in result.model_dump_json()
    assert boundary.confirm_calls[0]["context"].idempotency_key == (
        boundary.prepare_calls[0]["context"].idempotency_key
    )
    receipt = service.receipt(
        actor=actor(),
        session_binding="session-one",
        proposal_ref=proposal.proposal_ref,
        request_id=uuid4(),
    )
    assert receipt.status == "succeeded"
    assert len(boundary.confirm_calls) == 1


def test_new_service_can_query_persisted_receipt_without_restoring_a_plan():
    module, service, boundary, _, payload = setup()
    proposal = prepare(service, payload)
    service.confirm(
        actor=actor(),
        session_binding="session-one",
        proposal_ref=proposal.proposal_ref,
        request_id=uuid4(),
    )
    recovered = module.OfficialContactProductService(
        boundary=boundary,
        target_resolver=lambda **_: pytest.fail("receipt must not read target"),
        require_maintenance=lambda _: None,
        plan_store=InMemorySchoolFactPlanStore(),
    )
    assert (
        recovered.receipt(
            actor=actor(),
            session_binding="session-one",
            proposal_ref=proposal.proposal_ref,
            request_id=uuid4(),
        ).status
        == "succeeded"
    )


def test_revoke_between_prepare_and_click_blocks_writer():
    allowed = [True]

    def guard(_):
        if not allowed[0]:
            raise PermissionError("revoked")

    module, service, boundary, _, payload = setup(require=guard)
    proposal = prepare(service, payload)
    allowed[0] = False
    with pytest.raises(PermissionError):
        service.confirm(
            actor=actor(),
            session_binding="session-one",
            proposal_ref=proposal.proposal_ref,
            request_id=uuid4(),
        )
    assert not boundary.confirm_calls


def test_expired_or_other_session_proposal_cannot_confirm():
    module, service, boundary, clock, payload = setup()
    proposal = prepare(service, payload)
    with pytest.raises(module.ContactProductUnavailable):
        service.confirm(
            actor=actor(),
            session_binding="session-two",
            proposal_ref=proposal.proposal_ref,
            request_id=uuid4(),
        )
    clock[0] = NOW + timedelta(minutes=6)
    with pytest.raises(module.ContactProductUnavailable):
        service.confirm(
            actor=actor(),
            session_binding="session-one",
            proposal_ref=proposal.proposal_ref,
            request_id=uuid4(),
        )
    assert not boundary.confirm_calls


def test_dirty_preflight_never_issues_confirmation():
    module, service, boundary, _, payload = setup()
    boundary.clean = False
    with pytest.raises(module.ContactProductUnavailable):
        prepare(service, payload)
    assert not boundary.prepare_calls


def http_client(service, query=None):
    from fastapi import FastAPI
    from fastapi.exceptions import RequestValidationError
    from fastapi.testclient import TestClient

    from app.core.request_id import assign_request_id
    from app.interfaces.http.errors import ApiError
    from app.interfaces.http.framework_request import (
        api_error_handler,
        request_validation_error_handler,
    )
    from app.interfaces.http.school_fact_product_contacts import (
        create_school_fact_product_contact_router,
    )
    from app.platform.school_facts.read_policy import OfficialContactQueryResult

    app = FastAPI()
    app.middleware("http")(assign_request_id)
    app.add_exception_handler(ApiError, api_error_handler)
    app.add_exception_handler(RequestValidationError, request_validation_error_handler)
    repository = SimpleNamespace(
        read_official_contacts=lambda **_: OfficialContactQueryResult(
            status="known",
            queried_at=NOW,
            coverage="unknown",
            query_scope="仅合成校区，未声明完整覆盖",
            contacts=(target().public_fact,),
        )
    )
    app.include_router(
        create_school_fact_product_contact_router(
            identity_dependency=lambda: ProductIdentity(actor()),
            query=cast(QueryRepository, query or repository),
            contacts=service,
        )
    )
    return TestClient(app)


def test_http_real_routes_query_prepare_empty_confirm_and_receipt():
    _, service, boundary, _, payload = setup()
    client = http_client(service)
    queried = client.post(
        "/api/v1/school-fact-product/contacts/query", json={"campus_name": "合成第一校区"}
    )
    assert queried.status_code == 200, queried.text
    assert queried.json()["items"][0]["contact_value"] == "old@example.invalid"
    proposed = client.post(
        "/api/v1/school-fact-product/contacts/prepare", json=payload.model_dump()
    )
    assert proposed.status_code == 200, proposed.text
    ref = proposed.json()["proposal_ref"]
    assert not boundary.confirm_calls and proposed.headers["cache-control"] == "no-store"
    confirmed = client.post(f"/api/v1/school-fact-product/contacts/proposals/{ref}/confirm")
    assert confirmed.status_code == 200 and confirmed.json()["status"] == "succeeded"
    receipt = client.get(f"/api/v1/school-fact-product/receipts/{ref}")
    assert receipt.status_code == 200 and receipt.json()["status"] == "succeeded"


def test_http_confirmation_rejects_extra_payload_before_writer():
    _, service, boundary, _, payload = setup()
    client = http_client(service)
    proposed = client.post(
        "/api/v1/school-fact-product/contacts/prepare", json=payload.model_dump()
    ).json()
    ref = proposed["proposal_ref"]
    result = client.post(
        f"/api/v1/school-fact-product/contacts/proposals/{ref}/confirm",
        json={"school_id": "forged"},
    )
    assert result.status_code == 422
    assert not boundary.confirm_calls


def test_assistant_tool_prepares_public_summary_with_private_control_binding():
    from app.composition.school_fact_assistant_gateway import OfficialContactToolGateway
    from app.platform.assistant.tool_loop import ModelToolCall, ToolCallContext
    from app.platform.school_facts.read_policy import OFFICIAL_CONTACT_MODULE

    _, service, boundary, _, payload = setup()
    gateway = OfficialContactToolGateway(
        resolve_actor=lambda _: ProductIdentity(actor()),
        require_read=lambda _: None,
        query=cast(QueryRepository, SimpleNamespace()),
        contacts=service,
        receipt_retention_seconds=60,
    )
    result = gateway.execute(
        ModelToolCall(
            call_id="call-one",
            name=OFFICIAL_CONTACT_MODULE + ".prepare",
            arguments=payload.model_dump(mode="json"),
        ),
        ToolCallContext("actor-private", "conversation-test", "turn-test", "run-test", 1, uuid4()),
    )
    assert result.model_content["status"] == "awaiting_explicit_confirmation"
    assert result.module_result is not None
    assert result.module_result.action_offer is not None
    assert result.module_result.staged_state_ref not in str(result.fact_card)
    assert result.module_result.action_offer.action_offer_ref not in str(result.fact_card)
    assert "C" * 43 not in str(result.model_content)
    assert not boundary.confirm_calls


def gateway_setup(*, require=None, retention=60, capacity=128):
    from app.composition.school_fact_assistant_gateway import OfficialContactToolGateway

    _, service, boundary, clock, payload = setup(require=require)
    gateway = OfficialContactToolGateway(
        resolve_actor=lambda _: ProductIdentity(actor()),
        require_read=lambda _: None,
        query=cast(QueryRepository, SimpleNamespace()),
        contacts=service,
        receipt_retention_seconds=retention,
        max_recent_receipts=capacity,
        clock=lambda: clock[0],
    )
    return gateway, service, boundary, clock, payload


def tool_context(*, actor_ref="actor-private", conversation="conversation-test", generation=1):
    from app.platform.assistant.tool_loop import ToolCallContext

    return ToolCallContext(
        actor_ref, conversation, f"turn-{generation}", f"run-{generation}", generation, uuid4()
    )


def gateway_prepare(gateway, payload, context=None):
    from app.platform.assistant.tool_loop import ModelToolCall

    return gateway.execute(
        ModelToolCall(
            call_id="prepare",
            name="school_facts.official_contacts.prepare",
            arguments=payload.model_dump(mode="json"),
        ),
        context or tool_context(),
    )


def gateway_receipt(gateway, context=None):
    from app.platform.assistant.tool_loop import ModelToolCall

    return gateway.execute(
        ModelToolCall(call_id="check", name="school_facts.official_contacts.receipt", arguments={}),
        context or tool_context(generation=2),
    )


def test_next_round_receipt_needs_no_model_memory_and_unknown_never_reexecutes():
    gateway, service, boundary, _, payload = gateway_setup()
    candidate = gateway_prepare(gateway, payload)
    ref = candidate.model_content["proposal_ref"]
    boundary.unknown = True
    assert (
        service.confirm(
            actor=actor(), session_binding="session-one", proposal_ref=ref, request_id=uuid4()
        ).status
        == "unknown"
    )
    boundary.applied = None
    first = gateway_receipt(gateway)
    assert first.model_content["status"] == "unknown"
    boundary.applied = SchoolFactApplyResult(UUID(int=99), 4, 1, False)
    second = gateway_receipt(gateway, tool_context(generation=3))
    assert second.model_content["status"] == "succeeded"
    assert len(boundary.prepare_calls) == len(boundary.confirm_calls) == 1
    assert "old@example.invalid" not in str(second.model_content)


@pytest.mark.parametrize(
    "context", [dict(conversation="another-conversation"), dict(actor_ref="another-actor")]
)
def test_recent_receipt_does_not_cross_conversation_or_private_identity(context):
    gateway, _, boundary, _, payload = gateway_setup()
    gateway_prepare(gateway, payload)
    result = gateway_receipt(gateway, tool_context(**context))
    assert result.model_content["status"] == "clarification_required"
    assert len(boundary.prepare_calls) == 1 and boundary.confirm_calls == []


def test_recent_receipt_rechecks_current_maintenance_permission():
    allowed = [True]

    def require(_):
        if not allowed[0]:
            raise PermissionError("revoked")

    gateway, _, boundary, _, payload = gateway_setup(require=require)
    gateway_prepare(gateway, payload)
    allowed[0] = False
    assert gateway_receipt(gateway).model_content["status"] == "denied"
    assert not boundary.confirm_calls


def test_recent_receipt_cache_expires_and_is_bounded_without_fact_values():
    gateway, _, _, clock, payload = gateway_setup(capacity=2)
    for name in ("one", "two", "three"):
        gateway_prepare(gateway, payload, tool_context(conversation=name))
    assert (
        gateway_receipt(gateway, tool_context(conversation="one")).model_content["status"]
        == "clarification_required"
    )
    assert (
        gateway_receipt(gateway, tool_context(conversation="three")).model_content["status"]
        == "unknown"
    )
    clock[0] += timedelta(seconds=61)
    assert gateway.purge_expired() == 2
    assert (
        gateway_receipt(gateway, tool_context(conversation="three")).model_content["status"]
        == "clarification_required"
    )


def test_real_tool_loop_followup_queries_recent_receipt_without_resending_old_values():
    from app.platform.assistant.runtime import AssistantRuntime, RunSettlement
    from app.platform.assistant.tool_loop import (
        AssistantToolLoop,
        ModelReply,
        ModelToolCall,
        ToolCallContext,
    )

    gateway, service, boundary, _, payload = gateway_setup()
    runtime = AssistantRuntime()
    snapshot = runtime.create_conversation("actor-private", uuid4())
    first = runtime.accept_turn(
        snapshot.conversation_ref, "actor-private", uuid4(), "请修改联系方式"
    )
    context = ToolCallContext(
        first_actor := "actor-private",
        first.conversation_ref,
        first.turn_ref,
        first.run_ref,
        first.generation,
        first.request_id,
    )
    proposal = gateway_prepare(gateway, payload, context)
    boundary.unknown = True
    service.confirm(
        actor=actor(),
        session_binding="session-one",
        proposal_ref=proposal.model_content["proposal_ref"],
        request_id=uuid4(),
    )
    runtime.settle_run(
        first.conversation_ref,
        first_actor,
        first.run_ref,
        first.generation,
        first.request_id,
        RunSettlement(
            display_payload="结果未知",
            model_safe_note="旧事实 old@example.invalid",
            model_safe_template="旧模型回答",
        ),
    )
    next_turn = runtime.accept_turn(first.conversation_ref, first_actor, uuid4(), "刚才是否成功")

    class Model:
        def __init__(self):
            self.requests = []

        def respond(self, request):
            self.requests.append(request)
            if len(self.requests) == 1:
                assert "old@example.invalid" not in request.model_dump_json()
                assert "旧模型回答" not in request.model_dump_json()
                return ModelReply(
                    calls=(
                        ModelToolCall(
                            call_id="lookup",
                            name="school_facts.official_contacts.receipt",
                            arguments={},
                        ),
                    )
                )
            assert '"status":"succeeded"' in request.messages[-1].content
            return ModelReply(text="权威回执显示修改已完成。")

    model = Model()
    result = AssistantToolLoop(model=model, gateway_factory=lambda _: gateway).run_accepted(
        runtime=runtime,
        accepted=next_turn,
        actor_context_ref=first_actor,
        request_id=next_turn.request_id,
    )
    display = result.display_ledger[-1].display_payload
    assert isinstance(display, dict)
    assert display["text"] == "权威回执显示修改已完成。"
    assert len(boundary.prepare_calls) == len(boundary.confirm_calls) == 1


def test_query_returns_real_repository_denial_without_blanket_read_short_circuit():
    from app.composition.school_fact_assistant_gateway import OfficialContactToolGateway
    from app.composition.school_fact_query_repository import OfficialContactQueryRepository
    from app.platform.assistant.tool_loop import ModelToolCall
    from app.platform.authorization.secure_reader_contracts import SecureReadDenied

    class DenyingReader:
        def __init__(self):
            self.calls = []

        def read(self, request):
            self.calls.append(request)
            raise SecureReadDenied("private grant decision")

    def require_read(_):
        raise PermissionError("no matching READ grant")

    reader = DenyingReader()
    _, service, boundary, _, _ = setup()
    gateway = OfficialContactToolGateway(
        resolve_actor=lambda _: ProductIdentity(actor()),
        require_read=require_read,
        query=OfficialContactQueryRepository(reader=reader, clock=lambda: NOW),
        contacts=service,
        receipt_retention_seconds=60,
    )
    result = gateway.execute(
        ModelToolCall(
            call_id="query",
            name="school_facts.official_contacts.query",
            arguments={"campus_name": "合成第一校区"},
        ),
        tool_context(),
    )
    assert result.model_content["status"] == "denied"
    assert result.model_content["items"] == result.model_content["candidates"] == []
    assert len(reader.calls) == 1
    assert "private grant decision" not in str(result.model_content)
    assert not boundary.prepare_calls and not boundary.confirm_calls


def query_gateway(query, require_read, resolve=None):
    from app.composition.school_fact_assistant_gateway import OfficialContactToolGateway

    _, service, _, _, _ = setup()
    return OfficialContactToolGateway(
        resolve_actor=resolve or (lambda _: ProductIdentity(actor())),
        require_read=require_read,
        query=query,
        contacts=service,
        receipt_retention_seconds=60,
    )


def contact_reply(campus_name="合成第一校区"):
    from app.platform.assistant.tool_loop import ModelReply, ModelToolCall

    return ModelReply(
        calls=(
            ModelToolCall(
                call_id="query",
                name="school_facts.official_contacts.query",
                arguments={"campus_name": campus_name},
            ),
        )
    )


def run_contact_loop(gateway, model):
    from app.platform.assistant.tool_loop import AssistantToolLoop
    from tests.assistant.test_tool_loop import setup_run

    runtime, accepted = setup_run()
    return AssistantToolLoop(model=model, gateway_factory=gateway.for_turn).run_accepted(
        runtime=runtime,
        accepted=accepted,
        actor_context_ref="actor-private",
        request_id=accepted.request_id,
    )


@pytest.mark.parametrize("failure", ["denied", "source_unavailable"])
def test_turn_loop_passes_real_repository_safe_failure_to_next_model(failure):
    from app.composition.school_fact_query_repository import OfficialContactQueryRepository
    from app.platform.assistant.tool_loop import ModelReply
    from app.platform.authorization.secure_reader_contracts import SecureReadDenied
    from app.platform.school_facts.read_policy import SchoolFactAuthorizationUnavailable
    from tests.assistant.test_tool_loop import ScriptModel

    class Reader:
        def read(self, request):
            error = SecureReadDenied if failure == "denied" else SchoolFactAuthorizationUnavailable
            raise error("private authority detail")

    def require_read(_):
        pytest.fail("pure rejected query must come from its real reader")

    gateway = query_gateway(
        OfficialContactQueryRepository(reader=Reader(), clock=lambda: NOW), require_read
    )
    model = ScriptModel([contact_reply(), ModelReply(text="无法取得该项正式信息。")])
    result = run_contact_loop(gateway, model)
    assert len(model.requests) == 2
    assert f'"status":"{failure}"' in model.requests[1].messages[-1].content
    assert "private authority detail" not in model.requests[1].model_dump_json()
    display = result.display_ledger[-1].display_payload
    assert isinstance(display, dict)
    assert display["text"] == "无法取得该项正式信息。"


@pytest.mark.parametrize(
    "status",
    ["known", "ambiguous", "not_recorded", "no_current_records", "selection_required", "unmatched"],
)
def test_turn_loop_rechecks_all_authorized_query_cards_before_releasing_to_model(status):
    from app.platform.assistant.runtime import AssistantRuntimeError
    from app.platform.school_facts.read_policy import (
        OfficialContactCampus,
        OfficialContactQueryResult,
    )
    from tests.assistant.test_tool_loop import ScriptModel

    allowed = [True]

    def require_read(_):
        if not allowed[0]:
            raise PermissionError("revoked")

    def query(**_):
        allowed[0] = False
        return OfficialContactQueryResult(
            status=status,
            queried_at=NOW,
            coverage="unknown",
            query_scope="仅合成校区",
            contacts=(target().public_fact,) if status == "known" else (),
            campuses=(
                OfficialContactCampus(
                    campus_ref=target().public_fact.campus_ref,
                    display_name="受保护校区名",
                    display_path="受保护校区名",
                    version=1,
                ),
            ),
        )

    gateway = query_gateway(SimpleNamespace(read_official_contacts=query), require_read)
    model = ScriptModel([contact_reply()])
    with pytest.raises(AssistantRuntimeError, match="current context unavailable"):
        run_contact_loop(gateway, model)
    assert len(model.requests) == 1
    assert "old@example.invalid" not in model.requests[0].model_dump_json()
    assert "受保护校区名" not in model.requests[0].model_dump_json()


def test_turn_gateway_revalidates_identity_even_before_any_fact():
    from app.platform.assistant.runtime import AssistantRuntimeError
    from tests.assistant.test_tool_loop import ScriptModel

    def resolve(_):
        raise PermissionError("signed out")

    gateway = query_gateway(SimpleNamespace(), lambda _: None, resolve)
    model = ScriptModel([])
    with pytest.raises(AssistantRuntimeError, match="current context unavailable"):
        run_contact_loop(gateway, model)
    assert not model.requests


@pytest.mark.parametrize("capability", ["prepare", "receipt"])
def test_turn_gateway_preserves_read_gate_before_maintenance_service(capability):
    from app.platform.assistant.tool_loop import ModelToolCall

    def require_read(_):
        raise PermissionError("no READ permission")

    gateway = query_gateway(SimpleNamespace(), require_read)
    arguments = setup()[-1].model_dump(mode="json") if capability == "prepare" else {}
    result = gateway.for_turn("actor-private").execute(
        ModelToolCall(
            call_id="call", name="school_facts.official_contacts." + capability, arguments=arguments
        ),
        tool_context(),
    )
    assert result.model_content["status"] == "denied"
    assert result.fact_card is None and result.module_result is None


@pytest.mark.parametrize("capability", ["prepare", "receipt"])
def test_turn_gateway_marks_candidate_and_authoritative_receipt_protected(capability):
    from app.platform.assistant.runtime import AssistantRuntimeError

    allowed = [True]

    def require_read(_):
        if not allowed[0]:
            raise PermissionError("revoked")

    gateway, _, _, _, payload = gateway_setup()
    gateway._require_read = require_read
    if capability == "receipt":
        gateway_prepare(gateway, payload)
    turn = gateway.for_turn("actor-private")
    result = gateway_prepare(turn, payload) if capability == "prepare" else gateway_receipt(turn)
    assert result.fact_card is not None
    allowed[0] = False
    with pytest.raises(AssistantRuntimeError):
        turn.revalidate("actor-private")


def test_concurrent_turn_gateways_do_not_share_protected_fact_state():
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from app.platform.assistant.runtime import AssistantRuntimeError
    from app.platform.assistant.tool_loop import ModelReply
    from app.platform.school_facts.read_policy import OfficialContactQueryResult
    from tests.assistant.test_tool_loop import ScriptModel

    known_reached_model = Event()
    denied_finished = Event()
    allowed = [True]

    def require_read(_):
        if not allowed[0]:
            raise PermissionError("revoked")

    def query(**kw):
        known = kw["campus_name"] == "known"
        return OfficialContactQueryResult(
            status="known" if known else "forbidden",
            queried_at=NOW,
            coverage="unknown",
            query_scope="仅合成校区",
            contacts=(target().public_fact,) if known else (),
        )

    gateway = query_gateway(SimpleNamespace(read_official_contacts=query), require_read)

    def pause_known(request):
        assert "old@example.invalid" in request.messages[-1].content
        known_reached_model.set()
        assert denied_finished.wait(5)
        return ModelReply(text="旧事实不得在撤权后结算")

    known_model = ScriptModel([contact_reply("known"), pause_known])
    denied_model = ScriptModel([contact_reply("denied"), ModelReply(text="无权查询")])

    def run_denied():
        assert known_reached_model.wait(5)
        allowed[0] = False
        try:
            return run_contact_loop(gateway, denied_model)
        finally:
            denied_finished.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        known_future = pool.submit(run_contact_loop, gateway, known_model)
        denied_future = pool.submit(run_denied)
        result = denied_future.result(timeout=8)
        with pytest.raises(AssistantRuntimeError, match="current context unavailable"):
            known_future.result(timeout=8)
    display = result.display_ledger[-1].display_payload
    assert isinstance(display, dict)
    assert display["text"] == "无权查询"
    assert len(known_model.requests) == len(denied_model.requests) == 2
    assert "old@example.invalid" not in denied_model.requests[1].model_dump_json()


def test_turn_gateway_rejects_another_identity_binding():
    from app.platform.assistant.runtime import AssistantRuntimeError

    gateway, _, _, _, payload = gateway_setup()
    turn = gateway.for_turn("actor-private")
    with pytest.raises(AssistantRuntimeError):
        turn.revalidate("other-actor")
    with pytest.raises(AssistantRuntimeError):
        gateway_prepare(turn, payload, tool_context(actor_ref="other-actor"))


def test_http_and_tool_share_explicit_query_subject_scope_and_resolved_result():
    from app.composition.school_fact_query_repository import OfficialContactQueryRepository
    from app.platform.assistant.contracts import JsonObject
    from app.platform.assistant.tool_loop import ModelToolCall
    from tests.test_f1_official_contact_query import _campus, _SelectedPages

    reader = _SelectedPages((_campus(), _campus("另一校区", UUID(int=88))))
    repository = OfficialContactQueryRepository(reader=reader, clock=lambda: NOW)
    _, service, boundary, _, _ = setup()
    client = http_client(service, repository)
    selection: JsonObject = {"subject": "campuses", "scope": "all_authorized"}
    response = client.post("/api/v1/school-fact-product/contacts/query", json=selection)
    assert response.status_code == 200, response.text
    public = response.json()
    assert public["query"]["subject"] == "campuses"
    assert public["query"]["scope"] == "all_authorized"
    assert public["status"] == "known" and len(public["resolved_campuses"]) == 2
    gateway = query_gateway(repository, lambda _: None)
    result = gateway.execute(
        ModelToolCall(
            call_id="query",
            name="school_facts.official_contacts.query",
            arguments=selection,
        ),
        tool_context(),
    )
    assert result.model_content == public and result.fact_card == public
    assert not boundary.preflight_calls and not boundary.confirm_calls

    rejected = client.post(
        "/api/v1/school-fact-product/contacts/query",
        json={**selection, "campus_name": "旧校区"},
    )
    assert rejected.status_code == 422


@pytest.mark.parametrize("change", ["value", "expiry", "authorization", "service"])
def test_history_guard_rereads_owner_facts_without_repeating_writes(change):
    from dataclasses import replace
    from types import SimpleNamespace

    from app.platform.assistant.tool_loop import ModelToolCall, ToolCallContext
    from app.platform.school_facts.read_policy import OfficialContactQueryResult

    allowed = [True]
    result = [
        OfficialContactQueryResult(
            status="known",
            queried_at=NOW,
            coverage="unknown",
            query_scope="合成测试",
            contacts=(target().public_fact,),
        )
    ]
    queries = []

    def query(**kwargs):
        queries.append(kwargs)
        if change == "service" and len(queries) > 2:
            raise RuntimeError("source down")
        return result[0]

    def require_read(_):
        if not allowed[0]:
            raise PermissionError("revoked")

    owner = query_gateway(SimpleNamespace(read_official_contacts=query), require_read)
    context = ToolCallContext("actor-private", "conversation", "turn", "run", 1, uuid4())
    output = owner.for_turn("actor-private").execute(
        ModelToolCall(
            call_id="query",
            name="school_facts.official_contacts.query",
            arguments={"campus_name": "合成第一校区"},
        ),
        context,
    )
    assert output.history_check is not None and output.history_check.refresh()
    if change == "value":
        result[0] = replace(
            result[0],
            contacts=(replace(target().public_fact, contact_value="changed@example.invalid"),),
        )
    elif change == "expiry":
        result[0] = replace(result[0], status="no_current_records", contacts=())
    elif change == "authorization":
        allowed[0] = False
    assert not output.history_check.refresh()
    assert all(query["campus_name"] == "合成第一校区" for query in queries)


def test_history_guard_does_not_serialize_callbacks_or_fact_values():
    import pickle

    from app.platform.assistant.runtime import HistoryCheck

    check = HistoryCheck(lambda: True)
    assert repr(check) == "HistoryCheck(<private>)"
    with pytest.raises(TypeError, match="cannot be persisted"):
        pickle.dumps(check)


def test_contact_card_keeps_the_stable_campus_owner():
    card = api().contact_card(target().public_fact)
    assert card.model_dump()["campus_ref"] == target().public_fact.campus_ref


def test_query_tool_projects_the_same_bounded_cards_and_preserves_owner_references():
    import json
    from dataclasses import replace

    from app.composition.school_fact_query_repository import OfficialContactQueryRepository
    from app.platform.assistant.tool_loop import ModelToolCall
    from tests.test_f1_official_contact_query import _campus, _published_contact, _SelectedPages

    campuses = tuple(_campus("同名校区", UUID(int=n + 1)) for n in range(20))
    contacts = {
        campus.row_id: (
            replace(
                _published_contact(campus, n + 1),
                contact_value="v" * 1000,
                service_hours="h" * 1000,
            ),
        )
        for n, campus in enumerate(campuses)
    }
    repository = OfficialContactQueryRepository(
        reader=_SelectedPages(campuses, contacts),
        source_description="s" * 500,
        clock=lambda: NOW,
    )
    gateway = query_gateway(repository, lambda _: None)
    result = gateway.for_turn("actor-private").execute(
        ModelToolCall(
            call_id="query",
            name="school_facts.official_contacts.query",
            arguments={"scope": "all_authorized"},
        ),
        tool_context(),
    )
    public = cast(dict[str, Any], result.model_content)
    assert {key: value for key, value in public.items() if key != "items"} == {
        key: value for key, value in result.fact_card.items() if key != "items"
    }
    for actual, card in zip(public["items"], result.fact_card["items"], strict=True):
        assert {key: value for key, value in actual.items() if key != "supporting_evidence"} == {
            key: value for key, value in card.items() if key != "source"
        }
        assert actual["supporting_evidence"]["source"] == card["source"]
    assert (
        len(json.dumps(public, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) <= 12000
    )
    assert 0 < len(public["items"]) <= 10
    assert public["listing"]["returned_count"] == len(public["items"])
    assert public["listing"]["matched_count"] == 20 and public["listing"]["truncated"] is True
    owners = {campus["reference"] for campus in public["resolved_campuses"]}
    assert all(item["campus_ref"] in owners for item in public["items"])
    assert public["query"]["scope"] == "all_authorized"
    assert public["coverage"]["status"] == "unknown"
    assert result.history_check is not None and result.history_check.valid()
    assert result.history_content is not None
    history = json.dumps(result.history_content, ensure_ascii=False)
    assert (
        "contact_value" not in history
        and "service_hours" not in history
        and "source" not in history
    )
    history_content = cast(dict[str, Any], result.history_content)
    assert [item["reference"] for item in history_content["items"]] == [
        item["reference"] for item in public["items"]
    ]


def test_query_tool_with_one_oversized_record_reports_display_limit_not_absence():
    from dataclasses import replace

    from app.composition.school_fact_query_repository import OfficialContactQueryRepository
    from app.platform.assistant.tool_loop import ModelToolCall
    from tests.test_f1_official_contact_query import _campus, _published_contact, _SelectedPages

    campus = _campus()
    reader = _SelectedPages(
        (campus,),
        {campus.row_id: (replace(_published_contact(campus, 1), contact_value="v" * 30000),)},
    )
    gateway = query_gateway(OfficialContactQueryRepository(reader=reader), lambda _: None)
    result = gateway.execute(
        ModelToolCall(
            call_id="query",
            name="school_facts.official_contacts.query",
            arguments={"campus_ref": f"campus:{campus.row_id}"},
        ),
        tool_context(),
    )
    public = cast(dict[str, Any], result.model_content)
    assert public["status"] == "display_limited"
    assert public["display_notice"]
    assert public["items"] == public["resolved_campuses"] == public["candidates"] == []
    assert public["listing"] == {
        "entity": "contacts",
        "returned_count": 0,
        "matched_count": 1,
        "truncated": True,
    }
    assert public["query"]["campus_ref"] == f"campus:{campus.row_id}"


def test_query_tool_bounds_campus_candidates_without_relabeling_selection_or_source():
    from app.composition.school_fact_query_repository import OfficialContactQueryRepository
    from app.platform.assistant.tool_loop import ModelToolCall
    from tests.test_f1_official_contact_query import _campus, _SelectedPages

    reader = _SelectedPages(tuple(_campus(f"校区{n}", UUID(int=n + 1)) for n in range(101)))
    gateway = query_gateway(OfficialContactQueryRepository(reader=reader), lambda _: None)
    result = gateway.execute(
        ModelToolCall(
            call_id="query",
            name="school_facts.official_contacts.query",
            arguments={},
        ),
        tool_context(),
    )
    public = cast(dict[str, Any], result.model_content)
    assert public["status"] == "selection_required"
    assert len(public["candidates"]) == 10 and public["resolved_campuses"] == []
    assert public["listing"]["matched_count"] == 101
    assert public["listing"]["returned_count"] == 10 and public["listing"]["truncated"] is True


def test_query_projection_can_display_one_record_with_long_authorized_fields():
    import json
    from dataclasses import replace

    from app.composition.school_fact_query_repository import OfficialContactQueryRepository
    from app.platform.assistant.tool_loop import ModelToolCall
    from tests.test_f1_official_contact_query import _campus, _published_contact, _SelectedPages

    campus = _campus("校" * 200)
    reader = _SelectedPages(
        (campus,),
        {
            campus.row_id: (
                replace(
                    _published_contact(campus, 1),
                    contact_value="联" * 1000,
                    service_hours="时" * 1000,
                ),
            )
        },
    )
    gateway = query_gateway(
        OfficialContactQueryRepository(reader=reader, source_description="源" * 500),
        lambda _: None,
    )
    result = gateway.execute(
        ModelToolCall(
            call_id="query",
            name="school_facts.official_contacts.query",
            arguments={"campus_ref": f"campus:{campus.row_id}"},
        ),
        tool_context(),
    )
    public = cast(dict[str, Any], result.model_content)
    assert public["status"] == "known" and len(public["items"]) == 1
    assert public["items"][0]["contact_value"] == "联" * 1000
    assert public["items"][0]["service_hours"] == "时" * 1000
    assert (
        len(json.dumps(public, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) <= 12000
    )


def test_receipt_history_and_current_turn_recheck_maintenance_without_reading_writer(monkeypatch):
    from app.composition.school_fact_assistant_gateway import OfficialContactToolGateway
    from app.platform.assistant.runtime import AssistantRuntimeError
    from app.platform.assistant.tool_loop import ModelToolCall

    allowed = [True]

    def require_maintenance(_):
        if not allowed[0]:
            raise PermissionError("maintenance revoked; ordinary read still allowed")

    _, service, boundary, _, payload = setup(require=require_maintenance)
    proposal = prepare(service, payload)
    service.confirm(
        actor=actor(),
        session_binding="session-one",
        proposal_ref=proposal.proposal_ref,
        request_id=uuid4(),
    )
    reads = []
    original = boundary.check_result_by_locator

    def observe(**kwargs):
        reads.append(1)
        return original(**kwargs)

    monkeypatch.setattr(boundary, "check_result_by_locator", observe)

    owner = OfficialContactToolGateway(
        resolve_actor=lambda _: ProductIdentity(actor()),
        require_read=lambda _: None,
        query=cast(QueryRepository, SimpleNamespace()),
        contacts=service,
        receipt_retention_seconds=60,
    )
    gateway = owner.for_turn("actor-private")
    output = gateway.execute(
        ModelToolCall(
            call_id="receipt",
            name="school_facts.official_contacts.receipt",
            arguments={"proposal_ref": proposal.proposal_ref},
        ),
        tool_context(),
    )
    assert output.model_content["status"] == "succeeded"
    assert output.history_check is not None and output.history_check.refresh()
    observed_reads = len(reads)
    assert output.history_check.valid()
    assert len(reads) == observed_reads
    allowed[0] = False
    assert output.history_check.valid() is False
    with pytest.raises(AssistantRuntimeError):
        gateway.revalidate("actor-private")
    assert len(reads) == observed_reads and len(boundary.confirm_calls) == 1


@pytest.mark.parametrize("history", [False, True])
def test_receipt_maintenance_revocation_blocks_later_model_tools_and_settlement(history):
    from app.composition.school_fact_assistant_gateway import OfficialContactToolGateway
    from app.platform.assistant.runtime import AssistantRuntimeError
    from app.platform.assistant.tool_loop import (
        AssistantToolLoop,
        ModelReply,
        ModelToolCall,
        ToolModel,
    )
    from tests.assistant.test_tool_loop import ScriptModel, setup_run

    allowed = [True]

    def require_maintenance(_):
        if not allowed[0]:
            raise PermissionError("receipt maintenance revoked")

    _, service, boundary, _, payload = setup(require=require_maintenance)
    proposal = prepare(service, payload)
    service.confirm(
        actor=actor(),
        session_binding="session-one",
        proposal_ref=proposal.proposal_ref,
        request_id=uuid4(),
    )
    query_calls = []

    def query(**kwargs):
        query_calls.append(kwargs)
        pytest.fail("a subsequent query must not run after receipt authority changes")

    owner = OfficialContactToolGateway(
        resolve_actor=lambda _: ProductIdentity(actor()),
        require_read=lambda _: None,
        query=cast(QueryRepository, SimpleNamespace(read_official_contacts=query)),
        contacts=service,
        receipt_retention_seconds=60,
    )
    receipt_call = ModelReply(
        calls=(
            ModelToolCall(
                call_id="receipt",
                name="school_facts.official_contacts.receipt",
                arguments={"proposal_ref": proposal.proposal_ref},
            ),
        )
    )
    runtime, accepted = setup_run()
    if history:
        AssistantToolLoop(
            model=cast(ToolModel, ScriptModel([receipt_call, ModelReply(text="原回执已核对。")])),
            gateway_factory=owner.for_turn,
        ).run_accepted(
            runtime=runtime,
            accepted=accepted,
            actor_context_ref="actor-private",
            request_id=uuid4(),
        )
        accepted = runtime.accept_turn(
            accepted.conversation_ref, "actor-private", uuid4(), "继续核对"
        )

    def revoke_during_model(_):
        allowed[0] = False
        return ModelReply(
            calls=(
                ModelToolCall(
                    call_id="later",
                    name="school_facts.official_contacts.query",
                    arguments={"scope": "all_authorized"},
                ),
            )
        )

    model = ScriptModel([revoke_during_model] if history else [receipt_call, revoke_during_model])
    with pytest.raises(AssistantRuntimeError):
        AssistantToolLoop(
            model=cast(ToolModel, model), gateway_factory=owner.for_turn
        ).run_accepted(
            runtime=runtime,
            accepted=accepted,
            actor_context_ref="actor-private",
            request_id=uuid4(),
        )
    assert query_calls == [] and len(boundary.confirm_calls) == 1
    assert runtime.snapshot(accepted.conversation_ref, "actor-private").active_run is not None


@pytest.mark.parametrize(
    ("arguments", "reason_code"),
    [
        (
            {"campus_name": "PRIVATE_INVALID_INPUT", "campus_ref": "campus:" + str(UUID(int=12))},
            "query_selector_conflict",
        ),
        (
            {"scope": "all_authorized", "campus_name": "PRIVATE_INVALID_INPUT"},
            "query_scope_conflict",
        ),
        ({"subject": "PRIVATE_INVALID_INPUT"}, "invalid_query_arguments"),
    ],
)
def test_query_argument_rejection_is_specific_safe_and_never_enters_reader(arguments, reason_code):
    from app.composition.school_fact_query_repository import OfficialContactQueryRepository
    from app.platform.assistant.tool_loop import ModelToolCall

    calls = []

    class Reader:
        def read(self, request):
            calls.append(request)
            raise AssertionError("invalid query must not reach Reader")

    owner = query_gateway(OfficialContactQueryRepository(reader=Reader()), lambda _: None)
    result = owner.execute(
        ModelToolCall(
            call_id="invalid", name="school_facts.official_contacts.query", arguments=arguments
        ),
        tool_context(),
    )
    assert result.model_content["status"] == "rejected"
    assert result.model_content.get("reason_code") == reason_code
    assert set(result.model_content) == {"status", "reason_code", "reason"}
    assert isinstance(result.model_content["reason"], str) and result.model_content["reason"]
    assert "PRIVATE_INVALID_INPUT" not in str(result.model_content)
    assert "事实已变化" not in str(result.model_content)
    assert result.fact_card is None and result.module_result is None and calls == []


def test_query_diagnostic_does_not_relabel_a_stale_prepare_failure(monkeypatch):
    from app.composition.school_fact_assistant_gateway import OfficialContactToolGateway
    from app.composition.school_fact_product_contacts import ContactProductUnavailable
    from app.platform.assistant.tool_loop import ModelToolCall

    _, service, boundary, _, payload = setup()
    calls = []

    def stale(**kwargs):
        calls.append(kwargs)
        raise ContactProductUnavailable("PRIVATE_STALE_DETAIL")

    monkeypatch.setattr(service, "prepare", stale)
    owner = OfficialContactToolGateway(
        resolve_actor=lambda _: ProductIdentity(actor()),
        require_read=lambda _: None,
        query=cast(QueryRepository, SimpleNamespace()),
        contacts=service,
        receipt_retention_seconds=60,
    )
    result = owner.execute(
        ModelToolCall(
            call_id="prepare",
            name="school_facts.official_contacts.prepare",
            arguments=payload.model_dump(mode="json"),
        ),
        tool_context(),
    )
    assert result.model_content["status"] == "rejected"
    assert "reason_code" not in result.model_content
    assert isinstance(result.model_content["reason"], str)
    assert "事实已变化" in result.model_content["reason"]
    assert "PRIVATE_STALE_DETAIL" not in str(result.model_content)
    assert len(calls) == 1 and boundary.confirm_calls == []
