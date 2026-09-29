"""Independent contract regressions; no model calls or persistent school mutations."""

from copy import deepcopy
from types import SimpleNamespace
from typing import cast
from uuid import uuid4

import pytest

from app.composition.school_fact_directory_gateway import (
    MODULE,
    DirectoryFactToolGateway,
    DirectoryQueryPort,
    ProductActor,
    directory_history_material,
    directory_model_material,
    directory_model_projection,
)
from app.platform.assistant.runtime import AssistantRuntimeError
from app.platform.assistant.tool_loop import ModelToolCall, ToolCallContext
from tests.test_f2_directory_gateway import _relationship_record
from tests.test_f2_directory_product import actor, setup


def _assert_process_codes_absent(value):
    if isinstance(value, dict):
        assert "query_completion" not in value
        for item in value.values():
            _assert_process_codes_absent(item)
    elif isinstance(value, list):
        for item in value:
            _assert_process_codes_absent(item)


def test_separate_edges_have_explicit_endpoints_not_a_relation_between_neighbors():
    service = _relationship_record(
        "school_service",
        "different-service",
        display_name="合成取件服务",
        official_contact_ref="f2:official_contact:channel-two",
        policy_ref="f2:policy:rule-two",
    )
    policy = _relationship_record("policy", "rule-two", display_name="合成取件规则")
    channel = _relationship_record(
        "official_contact", "channel-two", contact_value="desk@example.invalid"
    )
    view = {"records": [channel, policy, service]}
    before = deepcopy(view)
    for material in (directory_model_material(view), directory_history_material(view)):
        edges = material["records"][2]["relationship_facts"]
        assert {e["subject_ref"] for e in edges} == {service["reference"]}
        assert {e["subject_name"] for e in edges} == {"合成取件服务"}
        by_type = {e["target_type"]: e for e in edges}
        assert by_type["制度目录"]["target_name"] == "合成取件规则"
        assert (
            by_type["制度目录"]["meaning"]
            == "仅证明本服务关联此制度，不证明渠道与制度之间存在直接关系。"
        )
        assert "target_name" not in by_type["官方联系方式"]
        assert material["records"][2]["fact_type"] == "学校服务目录"
    assert view == before


def test_relationship_name_cannot_be_invented_or_merged_by_same_name():
    one = _relationship_record(
        "school_service", "one", display_name="同名服务", policy_ref="f2:policy:p"
    )
    two = _relationship_record(
        "school_service",
        "two",
        display_name="同名服务",
        official_contact_ref="f2:official_contact:c",
    )
    unnamed = _relationship_record(
        "person_workplace", "desk", person_ref="f2:person:p", location_ref="f2:location:l"
    )
    unnamed["label"] = "路径/不能当名称"
    material = directory_model_material({"records": [one, two, unnamed]})
    assert (
        material["records"][0]["relationship_facts"][0]["subject_ref"]
        != material["records"][1]["relationship_facts"][0]["subject_ref"]
    )
    assert all(
        "subject_name" not in e and "target_name" not in e
        for e in material["records"][2]["relationship_facts"]
    )


def test_quality_scope_owner_is_not_publisher_and_actual_source_survives():
    organization = _relationship_record(
        "organization", "owner-independent", display_name="合成服务组"
    )
    service = _relationship_record(
        "school_service",
        "q",
        display_name="合成查阅服务",
        organization_ref=organization["reference"],
    )
    view = {
        "records": [service, organization],
        "quality": {
            "evidence_status": "known",
            "source_availability": "unavailable",
            "statements": [
                {
                    "kind": "source",
                    "owner_kind": "organization",
                    "owner_ref": organization["reference"],
                    "target_fact_kind": "school_service",
                    "target_ref": service["reference"],
                    "applies_to_query": False,
                    "responsibility": "合成资料发布岗位",
                    "source_status": "unavailable",
                }
            ],
        },
    }
    before = deepcopy(view)
    material = directory_model_material(view)
    scope = material["readable_quality"]["statements"][0]["scope"]
    assert "owner_filter_name" not in scope
    assert (
        material["readable_quality"]["statements"][0]["tool_context"]["owner_ref"]
        == organization["reference"]
    )
    assert scope["owner_filter_type"] == "组织"
    assert scope["owner_role"] == "归属筛选，非受评事实或发布者"
    assert "owner_name" not in scope and "publisher" not in scope
    assert material["records"][0]["source"]["label"] == "正式发布"
    assert (
        material["readable_quality"]["statements"][0]["details"]["发布与核实责任"]
        == "合成资料发布岗位"
    )
    assert view == before


def _gateway():
    service, boundary, allowed, payload = setup()
    gateway = DirectoryFactToolGateway(
        resolve_actor=lambda _: cast(
            ProductActor, SimpleNamespace(actor=actor(), session_binding="session-one")
        ),
        query=cast(DirectoryQueryPort, SimpleNamespace()),
        product=service,
        retention_seconds=60,
    )
    context = ToolCallContext("private-actor", "dialogue", "turn", "run", 1, uuid4())
    return gateway, boundary, allowed, payload, context


def test_preparation_readable_material_preserves_canonical_proposal_and_revocation():
    gateway, boundary, allowed, payload, context = _gateway()
    turn = gateway.for_turn(context.actor_context_ref)
    result = turn.execute(
        ModelToolCall(
            call_id="prepare", name=MODULE + ".prepare", arguments=payload.model_dump(mode="json")
        ),
        context,
    )
    material = result.model_content
    assert material["outcome"] == "候选已准备，尚未正式执行"
    assert material["changes"] == [
        {"field": "正式名称", "before": "合成人员甲", "after": "合成人员乙"}
    ]
    assert "summary" not in material and "proposal_ref" not in material
    assert isinstance(material["tool_context"]["action_number"], int)
    assert result.fact_card["proposal_ref"] not in str(material)
    assert result.fact_card["summary"]["after"] == payload.fields
    assert result.module_result.action_offer is None
    assert not any(name == "confirm" for name, _ in boundary.calls)
    turn.revalidate(context.actor_context_ref)
    allowed[0] = False
    with pytest.raises((AssistantRuntimeError, PermissionError)):
        turn.revalidate(context.actor_context_ref)


@pytest.mark.parametrize(
    "status,expected",
    [
        ("succeeded", "正式修改已完成"),
        ("rejected", "正式修改被拒绝"),
        ("unknown", "原操作结果尚未确定"),
    ],
)
def test_receipt_projection_keeps_unknown_distinct_and_rechecks_raw_receipt(status, expected):
    from app.composition.school_fact_product_contacts import ContactReceipt

    gateway, _, allowed, _, context = _gateway()
    raw = ContactReceipt(
        reference="sf3plan_bounded_receipt_reference",
        status=status,
        message="正式回执说明",
        changed_count=1 if status == "succeeded" else None,
        target_version=8 if status == "succeeded" else None,
    )

    def receipt(**kwargs):
        if not allowed[0]:
            raise PermissionError("revoked")
        assert kwargs["proposal_ref"] == raw.reference
        return raw

    gateway.product.receipt = receipt
    turn = gateway.for_turn(context.actor_context_ref)
    result = turn.execute(
        ModelToolCall(
            call_id="receipt", name=MODULE + ".receipt", arguments={"proposal_ref": raw.reference}
        ),
        context,
    )
    assert result.model_content["outcome"] == expected
    assert "reference" not in result.model_content and "status" not in result.model_content
    assert isinstance(result.model_content["tool_context"]["action_number"], int)
    assert raw.reference not in str(result.model_content)
    assert result.fact_card["status"] == status
    if status == "succeeded":
        assert result.model_content["changed_count"] == 1
    else:
        assert "changed_count" not in result.model_content
    turn.revalidate(context.actor_context_ref)
    assert result.history_check.valid()
    allowed[0] = False
    with pytest.raises((AssistantRuntimeError, PermissionError)):
        turn.revalidate(context.actor_context_ref)


def test_preparation_projection_localizes_enums_but_never_interprets_unknown_codes():
    from app.composition.school_fact_directory_action_material import proposal_model_material

    raw = {
        "proposal_ref": "sf3plan_raw",
        "expires_at": "2026-09-12T10:00:00Z",
        "summary": {
            "fact_kind": "official_contact",
            "reference": "f2:official_contact:raw",
            "operation": "upsert",
            "label": "desk@example.invalid",
            "before": {
                "contact_kind": "email",
                "contact_value": "desk@example.invalid",
                "owner_kind": "location",
            },
            "after": {
                "contact_kind": "phone",
                "contact_value": "000-123",
                "owner_kind": "location",
                "owner_ref": "f2:location:raw",
            },
            "reason": "合成资料校核",
            "effective_at": "2026-09-12T09:00:00Z",
            "source_label": "正式资料",
        },
    }
    material = proposal_model_material(raw)
    assert material["changes"][:2] == [
        {"field": "联系类型", "before": "邮箱", "after": "电话"},
        {"field": "联系方式", "before": "desk@example.invalid", "after": "000-123"},
    ]
    assert material["tool_context"]["reference"] == raw["summary"]["reference"]
    assert material["tool_context"]["relation_changes"]["owner_ref"]["after"] == "f2:location:raw"
    assert "formal_name" not in material
    unknown = deepcopy(raw)
    unknown["summary"]["after"]["contact_kind"] = "not_a_registered_kind"
    projected = proposal_model_material(unknown)
    assert projected["changes"][0]["after"] == {
        "meaning": "尚无领域释义的原始值",
        "raw": "not_a_registered_kind",
    }


def test_proposal_policy_original_is_explicit_excerpt_not_silent_full_text():
    from app.composition.school_fact_directory_action_material import proposal_model_material

    text = "合成条款" * 1000
    raw = {
        "proposal_ref": "sf3plan_policy",
        "summary": {
            "fact_kind": "policy_version",
            "operation": "upsert",
            "reference": None,
            "label": "合成规章版本",
            "before": None,
            "after": {"content_text": text},
            "reason": "初次发布",
            "source_label": "合成制度",
        },
    }
    material = proposal_model_material(directory_model_projection(raw))
    original = material["changes"][0]["after"]
    assert original["length"] == len(text) and original["complete"] is False
    assert original["excerpt"] == text[:1200]
    assert raw["summary"]["after"]["content_text"] == text


@pytest.mark.parametrize(
    "field",
    [
        "semantic_role_code",
        "audience_code",
        "category_code",
        "kind",
        "alias_kind",
        "basis_reference",
        "approval_reference",
        "consent_reference",
        "evidence_reference",
    ],
)
def test_proposal_does_not_promote_uninterpreted_codes_or_evidence_into_main_facts(field):
    from app.composition.school_fact_directory_action_material import proposal_model_material

    raw = {
        "proposal_ref": "sf3plan_scoped_evidence",
        "summary": {
            "fact_kind": "school_service",
            "reference": "f2:school_service:scoped",
            "operation": "upsert",
            "before": {},
            "after": {field: "task-internal-reference"},
            "source_label": "正式资料",
        },
    }
    material = proposal_model_material(raw)
    assert "task-internal-reference" not in str(material["changes"])
    assert "task-internal-reference" in str(material["tool_context"])
    assert material["source_label"] == "正式资料"
    assert raw["summary"]["after"][field] == "task-internal-reference"


@pytest.mark.parametrize(
    "operation,label",
    [
        ("upsert", "发布或更新"),
        ("end", "结束"),
        ("revoke", "撤销"),
        ("reactivate", "重新启用原对象"),
    ],
)
def test_proposal_uses_legal_lifecycle_operations_without_claiming_field_deletion(operation, label):
    from app.composition.school_fact_directory_action_material import proposal_model_material

    raw = {
        "proposal_ref": "sf3plan_lifecycle",
        "summary": {
            "fact_kind": "person",
            "reference": "f2:person:lifecycle",
            "operation": operation,
            "before": {"display_name": "合成成员", "person_kind": "staff"},
            "after": None
            if operation in {"end", "revoke"}
            else {"display_name": "合成成员", "person_kind": "staff"},
            "effective_at": "2026-09-12T09:00:00Z",
        },
    }
    material = proposal_model_material(raw)
    assert material["operation"] == label
    assert material["changes"] == []
    assert material["outcome"] == "候选已准备，尚未正式执行"
    assert material["effective_at"] == raw["summary"]["effective_at"]
    assert material["tool_context"]["operation"] == operation


def test_service_dossiers_bind_same_name_facts_quality_and_relation_coverage():
    from app.composition.school_fact_directory_lookup import FactLookupInput, lookup
    from tests.test_f2_directory_gateway import _history_service_facts

    facts = _history_service_facts()
    rows = facts.rows[("school_service", None)]["records"]
    rows[1]["fields"]["display_name"] = rows[0]["fields"]["display_name"]
    quality = facts.rows[("school_service", None)]["quality"]
    for claim in quality["statements"]:
        claim["target_ref"] = rows[0]["reference"]
        claim["applies_to_records"] = [rows[0]["reference"]]
        claim["applies_to_query"] = False
    raw = lookup(
        facts,
        actor=actor(),
        payload=FactLookupInput(
            subject_kind="school_service",
            scope="all_authorized",
            include=("official_contact", "organization_campus_relationship"),
        ),
    )
    original = deepcopy(raw)
    material = directory_model_material(directory_model_projection(raw))
    one, two = material["service_results"]
    assert one["service_name"] == two["service_name"]
    assert one["facts"]["readable_facts"]["服务说明"] == "资料登记与问询"
    assert two["facts"]["readable_facts"]["服务说明"] == "当面登记与资料核对"
    assert one["facts"]["tool_context"]["reference"] != two["facts"]["tool_context"]["reference"]
    assert len(one["facts"]["quality_statements"]) == 4
    assert two["facts"]["quality_statements"] == []
    channel = one["official_contact"]["facts"]
    assert channel["readable_facts"]["联系方式"] == "advice@example.invalid"
    assert channel["quality_statements"] == []
    assert channel["source_in"] == "results"
    assert material["results"][1]["records"][0]["source"]["label"] == "学校正式发布资料"
    campuses = one["provider_campuses"]
    assert campuses["reality_coverage"]["meaning"] == "覆盖未确认"
    assert len(campuses["relations"]) == 1
    assert campuses["relations"][0]["relationship_facts"]
    assert "不声明关联唯一" in campuses["scope"]
    assert raw == original


def test_receipt_model_material_does_not_repeat_unused_machine_status():
    from app.composition.school_fact_directory_action_material import receipt_model_material

    raw = {
        "status": "unknown",
        "reference": "sf3plan_original_receipt_handle",
        "message": "结果尚未确定",
        "request_id": "original-request",
    }
    material = receipt_model_material(raw)
    assert material["outcome"] == "原操作结果尚未确定"
    assert "status" not in material["tool_context"]
    assert material["tool_context"]["reference"] == raw["reference"]
    assert raw["status"] == "unknown"


def test_action_numbers_select_original_operation_and_do_not_reveal_raw_handles():
    import json
    from dataclasses import replace

    gateway, _, _, payload, context = _gateway()
    first = gateway.execute(
        ModelToolCall(
            call_id="first", name=MODULE + ".prepare", arguments=payload.model_dump(mode="json")
        ),
        context,
    )
    second_context = replace(context, request_id=uuid4())
    second = gateway.execute(
        ModelToolCall(
            call_id="second", name=MODULE + ".prepare", arguments=payload.model_dump(mode="json")
        ),
        second_context,
    )
    one = first.model_content["tool_context"]["action_number"]
    two = second.model_content["tool_context"]["action_number"]
    assert one != two
    assert first.fact_card["proposal_ref"] not in json.dumps(first.model_content)
    queried = gateway.for_turn(context.actor_context_ref).execute(
        ModelToolCall(call_id="old", name=MODULE + ".receipt", arguments={"action_number": one}),
        context,
    )
    assert queried.fact_card["reference"] == first.fact_card["proposal_ref"]
    assert queried.model_content["tool_context"] == {"action_number": one}
    assert queried.history_check.valid()
    assert "request_id" not in json.dumps(queried.model_content)
    assert queried.fact_card["reference"] not in json.dumps(queried.model_content)
    for changed in (
        replace(context, conversation_ref="another"),
        replace(context, actor_context_ref="other-actor"),
    ):
        denied = gateway.execute(
            ModelToolCall(
                call_id="wrong", name=MODULE + ".receipt", arguments={"action_number": one}
            ),
            changed,
        )
        assert denied.model_content["status"] == "clarification_required"
        assert denied.fact_card is None
    invalid = gateway.execute(
        ModelToolCall(
            call_id="missing", name=MODULE + ".receipt", arguments={"action_number": two + 1}
        ),
        context,
    )
    assert invalid.model_content["status"] == "clarification_required"
    explicit = gateway.execute(
        ModelToolCall(
            call_id="explicit",
            name=MODULE + ".receipt",
            arguments={"proposal_ref": first.fact_card["proposal_ref"]},
        ),
        context,
    )
    assert explicit.fact_card["reference"] == first.fact_card["proposal_ref"]


def test_action_number_expiry_capacity_and_nonreuse():
    from datetime import UTC, datetime, timedelta

    gateway, _, _, _, context = _gateway()
    now = datetime.now(UTC)
    identity = gateway.resolve_actor(context.actor_context_ref)
    one = gateway._remember_action(context, identity, "sf3plan_first_original_ref", now=now)
    for n in range(127):
        gateway._remember_action(context, identity, f"sf3plan_original_ref_{n:08d}", now=now)
    with pytest.raises(ValueError):
        gateway._remember_action(context, identity, "sf3plan_capacity_failure", now=now)
    later = now + timedelta(seconds=61)
    gateway.purge_expired(now=later)
    assert gateway._action_reference(context, identity, one, now=later) is None
    newer = gateway._remember_action(context, identity, "sf3plan_new_original_ref", now=later)
    assert newer > one
    assert gateway._action_reference(context, identity, one, now=later) is None


def test_shared_fact_conversation_actions_keep_recent_selection_bound_to_actor_and_session():
    from types import SimpleNamespace

    from app.composition.school_fact_directory_gateway import FactConversationActions

    gateway, _, _, payload, context = _gateway()
    assert isinstance(gateway, FactConversationActions)
    result = gateway.execute(
        ModelToolCall(
            call_id="prepare", name=MODULE + ".prepare", arguments=payload.model_dump(mode="json")
        ),
        context,
    )
    identity = gateway.resolve_actor(context.actor_context_ref)
    number, reference = gateway.recent_action(context, identity)
    assert number == result.model_content["tool_context"]["action_number"]
    assert reference == result.fact_card["proposal_ref"]
    assert (
        gateway.recent_action(
            context, SimpleNamespace(actor=identity.actor, session_binding="another-session")
        )
        is None
    )


def test_receipt_action_selection_is_exclusive_and_strict():
    from pydantic import ValidationError

    from app.composition.school_fact_directory_gateway import DirectoryReceiptInput

    for args in (
        {"action_number": True},
        {"action_number": 0},
        {"action_number": 1, "proposal_ref": "sf3plan_valid_original_ref"},
    ):
        with pytest.raises(ValidationError):
            DirectoryReceiptInput.model_validate(args)


def test_action_session_change_and_history_expiry_never_refresh_number():
    from dataclasses import replace
    from datetime import UTC, datetime, timedelta

    gateway, _, _, payload, context = _gateway()
    turn = gateway.for_turn(context.actor_context_ref)
    proposal = turn.execute(
        ModelToolCall(
            call_id="proposal", name=MODULE + ".prepare", arguments=payload.model_dump(mode="json")
        ),
        context,
    )
    number = proposal.model_content["tool_context"]["action_number"]
    identity = gateway.resolve_actor(context.actor_context_ref)
    changed_identity = SimpleNamespace(actor=identity.actor, session_binding="new-session")
    assert gateway._action_reference(context, changed_identity, number) is None
    receipt_turn = gateway.for_turn(context.actor_context_ref)
    receipt = receipt_turn.execute(
        ModelToolCall(
            call_id="receipt", name=MODULE + ".receipt", arguments={"action_number": number}
        ),
        context,
    )
    assert proposal.history_check.valid()
    assert receipt.history_check.valid()
    gateway.purge_expired(now=datetime.now(UTC) + timedelta(seconds=61))
    with pytest.raises(AssistantRuntimeError):
        turn.revalidate(context.actor_context_ref)
    with pytest.raises(AssistantRuntimeError):
        receipt_turn.revalidate(context.actor_context_ref)
    assert not receipt.history_check.refresh()
    assert not gateway._actions
    changed = gateway.execute(
        ModelToolCall(
            call_id="another", name=MODULE + ".prepare", arguments=payload.model_dump(mode="json")
        ),
        replace(context, request_id=uuid4()),
    )
    assert changed.model_content["tool_context"]["action_number"] > number
    missing = gateway.execute(
        ModelToolCall(
            call_id="expired", name=MODULE + ".receipt", arguments={"action_number": number}
        ),
        context,
    )
    assert missing.fact_card is None
    assert missing.model_content["status"] == "clarification_required"


def test_capacity_refuses_before_preparing_any_candidate():
    gateway, boundary, _, payload, context = _gateway()
    identity = gateway.resolve_actor(context.actor_context_ref)
    for n in range(128):
        gateway._remember_action(context, identity, f"sf3plan_capacity_original_{n:08d}")
    before = list(boundary.calls)
    result = gateway.execute(
        ModelToolCall(
            call_id="full", name=MODULE + ".prepare", arguments=payload.model_dump(mode="json")
        ),
        context,
    )
    assert result.model_content["status"] == "rejected"
    assert boundary.calls == before
    assert len(gateway._actions) == 128


def test_channel_quality_stays_with_channel_and_does_not_move_to_service():
    from app.composition.school_fact_directory_lookup import FactLookupInput, lookup
    from tests.test_f2_directory_gateway import _history_service_facts

    facts = _history_service_facts()
    channel_key = next(key for key in facts.rows if key[0] == "official_contact")
    channel = facts.rows[channel_key]["records"][0]
    claim = deepcopy(facts.rows[("school_service", None)]["quality"]["statements"][0])
    claim.update(
        reference="f2:fact_quality:channel-independent",
        target_fact_kind="official_contact",
        target_ref=channel["reference"],
        applies_to_records=[channel["reference"]],
        source_status="available",
    )
    facts.rows[channel_key]["quality"] = {"evidence_status": "known", "statements": [claim]}
    raw = lookup(
        facts,
        actor=actor(),
        payload=FactLookupInput(
            subject_kind="school_service",
            scope="all_authorized",
            include=("official_contact", "organization_campus_relationship"),
        ),
    )
    material = directory_model_material(directory_model_projection(raw))
    service = material["service_results"][0]
    own = service["facts"]["quality_statements"]
    contact = service["official_contact"]["facts"]["quality_statements"]
    assert len(own) == 4 and len(contact) == 1
    assert all(c["scope"]["target_fact_type"] == "学校服务目录" for c in own)
    assert contact[0]["scope"]["target_fact_type"] == "官方联系方式"
    assert service["official_contact"]["facts"]["tool_context"]["reference"] == channel["reference"]
    assert "tool_context" not in contact[0]  # Original result retains the claim's tool handles.


def test_receipt_read_crossing_expiry_does_not_allocate_another_number():
    from datetime import UTC, datetime, timedelta

    gateway, _, _, payload, context = _gateway()
    prepared = gateway.execute(
        ModelToolCall(
            call_id="prepare", name=MODULE + ".prepare", arguments=payload.model_dump(mode="json")
        ),
        context,
    )
    number = prepared.model_content["tool_context"]["action_number"]
    original_receipt = gateway.product.receipt
    before_next = gateway._next_action_number

    def crossing(**kwargs):
        result = original_receipt(**kwargs)
        gateway.purge_expired(now=datetime.now(UTC) + timedelta(seconds=61))
        return result

    gateway.product.receipt = crossing
    result = gateway.execute(
        ModelToolCall(
            call_id="crossing", name=MODULE + ".receipt", arguments={"action_number": number}
        ),
        context,
    )
    assert result.model_content["status"] == "rejected"
    assert result.fact_card is None
    assert gateway._next_action_number == before_next
    assert not gateway._actions


def test_receipt_history_checks_do_not_extend_action_expiry():
    gateway, _, _, payload, context = _gateway()
    prepared = gateway.execute(
        ModelToolCall(
            call_id="prepare", name=MODULE + ".prepare", arguments=payload.model_dump(mode="json")
        ),
        context,
    )
    number = prepared.model_content["tool_context"]["action_number"]
    expires = gateway._actions[number][2]
    receipt = gateway.for_turn(context.actor_context_ref).execute(
        ModelToolCall(
            call_id="receipt", name=MODULE + ".receipt", arguments={"action_number": number}
        ),
        context,
    )
    assert receipt.history_check.valid() and receipt.history_check.refresh()
    assert gateway._actions[number][2] == expires


def test_preparing_candidate_does_not_block_another_identity_action_lookup():
    from concurrent.futures import ThreadPoolExecutor
    from dataclasses import replace
    from threading import Event

    gateway, _, _, payload, context = _gateway()
    other = replace(context, actor_context_ref="other-actor", conversation_ref="other-dialogue")
    identity = gateway.resolve_actor(other.actor_context_ref)
    reference = "sf3plan_existing_other_identity_reference"
    number = gateway._remember_action(other, identity, reference)
    entered, release = Event(), Event()
    original = gateway.product.prepare

    def waiting_prepare(**kwargs):
        entered.set()
        assert release.wait(5)
        return original(**kwargs)

    gateway.product.prepare = waiting_prepare
    call = ModelToolCall(
        call_id="prepare", name=MODULE + ".prepare", arguments=payload.model_dump(mode="json")
    )
    with ThreadPoolExecutor(max_workers=2) as workers:
        pending = workers.submit(gateway.execute, call, context)
        try:
            assert entered.wait(2)
            lookup = workers.submit(gateway._action_reference, other, identity, number)
            assert lookup.result(timeout=0.5) == reference
            assert not pending.done()
        finally:
            release.set()
        assert pending.result(timeout=2).fact_card is not None


def test_pending_prepare_reserves_capacity_and_rejects_concurrent_excess():
    from concurrent.futures import ThreadPoolExecutor
    from dataclasses import replace
    from threading import Event

    gateway, _, _, payload, context = _gateway()
    identity = gateway.resolve_actor(context.actor_context_ref)
    for n in range(127):
        gateway._remember_action(context, identity, f"sf3plan_existing_capacity_{n:08d}")
    entered, release = Event(), Event()
    original = gateway.product.prepare
    calls = []

    def waiting_prepare(**kwargs):
        calls.append(kwargs["request_id"])
        entered.set()
        assert release.wait(5)
        return original(**kwargs)

    gateway.product.prepare = waiting_prepare
    call = ModelToolCall(
        call_id="prepare", name=MODULE + ".prepare", arguments=payload.model_dump(mode="json")
    )
    with ThreadPoolExecutor(max_workers=2) as workers:
        pending = workers.submit(gateway.execute, call, context)
        try:
            assert entered.wait(2)
            excess = workers.submit(gateway.execute, call, replace(context, request_id=uuid4()))
            assert excess.result(timeout=0.5).model_content["status"] == "rejected"
            assert len(calls) == 1
            with pytest.raises(ValueError):
                gateway._remember_action(context, identity, "sf3plan_excess_explicit_reference")
        finally:
            release.set()
        assert pending.result(timeout=2).fact_card is not None
    assert len(gateway._actions) == 128
    assert gateway._preparing_actions == 0


def test_failed_prepare_releases_reserved_action_capacity():
    gateway, _, _, payload, context = _gateway()
    original = gateway.product.prepare

    def failed_prepare(**kwargs):
        raise ValueError("synthetic prepare failure")

    gateway.product.prepare = failed_prepare
    call = ModelToolCall(
        call_id="prepare", name=MODULE + ".prepare", arguments=payload.model_dump(mode="json")
    )
    assert gateway.execute(call, context).model_content["status"] == "rejected"
    assert gateway._preparing_actions == 0
    assert not gateway._actions and not gateway._recent
    gateway.product.prepare = original
    assert gateway.execute(call, context).fact_card is not None
    assert gateway._preparing_actions == 0


def test_multiple_target_material_keeps_each_unresolved_selection_explicit():
    raw = {
        "kind": "directory_bundle",
        "status": "partial",
        "results": [],
        "service_groups": [],
        "target_results": [
            {
                "selection": {"query_text": "同名服务"},
                "status": "ambiguous",
                "component_status": "ambiguous",
                "query_completion": "subject_unresolved",
                "service_references": [],
                "candidate_references": ["f2:school_service:one", "f2:school_service:two"],
            },
            {
                "selection": {"query_text": "其他服务"},
                "status": "denied",
                "component_status": "denied",
                "query_completion": "incomplete",
                "service_references": [],
            },
        ],
    }
    before = deepcopy(raw)
    material = directory_model_material(raw)
    assert material["target_results"][0]["outcome"] == "名称对应多个获准对象，尚未选择"
    assert material["target_results"][1]["outcome"] == "当前身份无权读取"
    _assert_process_codes_absent(material)
    assert (
        material["target_results"][0]["tool_context"]["candidate_references"]
        == raw["target_results"][0]["candidate_references"]
    )
    assert raw == before


@pytest.mark.parametrize("evidence_status", ["forbidden", "denied", "source_unavailable"])
def test_main_fact_result_is_distinct_from_its_independent_quality_limit(evidence_status):
    record = _relationship_record("official_contact", "phone", contact_value="synthetic value")
    raw = {
        "kind": "directory_facts",
        "fact_kind": "official_contact",
        "status": "known",
        "records": [record],
        "matched_count": 1,
        "returned_count": 1,
        "truncated": False,
        "query": {"scope": "selected", "reference": record["reference"]},
        "coverage": {"status": "unknown"},
        "quality": {
            "evidence_status": evidence_status,
            "existence": "known",
            "coverage": "unknown",
            "source_availability": "unknown",
            "statements": [],
        },
    }
    before = deepcopy(raw)
    material = directory_model_material(raw)
    assert material["status"] == "known"
    assert material["fact_reading"] == {"outcome": "已取得正式记录"}
    assert material["readable_quality"]["reading"] == (
        "本次未能读取质量声明"
        if evidence_status == "source_unavailable"
        else "当前身份无权读取质量声明"
    )
    assert material["readable_quality"]["coverage"] == "覆盖未确认；不支持全部或唯一"
    assert material["records"][0]["readable_facts"]["联系方式"] == "synthetic value"
    _assert_process_codes_absent(material)
    history = directory_history_material(raw)
    assert history["fact_reading"] == material["fact_reading"]
    _assert_process_codes_absent(history)
    assert history["readable_quality"] == material["readable_quality"]
    assert raw == before


def test_readable_result_retains_truncation_and_formal_quality_risks():
    raw = {
        "kind": "directory_facts",
        "fact_kind": "policy",
        "status": "known",
        "records": [],
        "matched_count": 2,
        "returned_count": 0,
        "truncated": True,
        "coverage": {"status": "partial"},
        "quality": {
            "evidence_status": "known",
            "source_availability": "unavailable",
            "conflict": "open",
            "freshness": "review_overdue",
            "coverage": "partial",
            "evidence_truncated": True,
            "statements": [],
        },
    }
    before = deepcopy(raw)
    material = directory_model_material(raw)
    assert material["fact_reading"]["limit"] == "本次获准结果未全部返回"
    assert material["authorized_result_set"]["truncated"] is True
    _assert_process_codes_absent(material)
    quality = material["readable_quality"]
    assert quality["source_statement"] == "声明记载原来源不可用"
    assert quality["conflict"] == "正式资料存在待核实争议"
    assert quality["freshness"] == "已到声明规定的核实期限"
    assert quality["coverage"] == "部分覆盖；不支持全部或唯一"
    assert quality["reading_limit"] == "质量声明未全部返回，不能据此确定完整质量结论"
    assert raw == before


def test_bundle_keeps_each_target_outcome_without_model_process_codes():
    raw = {
        "kind": "directory_bundle",
        "status": "partial",
        "query_completion": "incomplete",
        "results": [
            {
                "kind": "directory_facts",
                "fact_kind": "school_service",
                "status": "known",
                "records": [],
                "quality": {"evidence_status": "forbidden"},
            },
            {
                "kind": "directory_facts",
                "fact_kind": "official_contact",
                "status": "no_current_records",
                "records": [],
                "quality": {"evidence_status": "no_current_records"},
            },
        ],
        "service_groups": [],
        "target_results": [
            {
                "selection": {"query_text": "one"},
                "status": "known",
                "component_status": "partial",
                "query_completion": "incomplete",
                "service_references": ["f2:school_service:one"],
            },
            {
                "selection": {"query_text": "two"},
                "status": "no_current_records",
                "component_status": "no_current_records",
                "query_completion": "completed",
                "service_references": [],
            },
        ],
    }
    before = deepcopy(raw)
    material = directory_model_material(raw)
    _assert_process_codes_absent(material)
    assert material["results"][0]["fact_reading"]["outcome"] == "已取得正式记录"
    assert material["results"][1]["fact_reading"]["outcome"] == "本次范围内无当前记录"
    assert material["results"][0]["readable_quality"]["reading"] == "当前身份无权读取质量声明"
    assert material["results"][1]["readable_quality"]["reading"] == "本次未返回有效质量声明"
    assert material["target_results"][0]["outcome"] == "已取得正式记录"
    assert material["target_results"][0]["components"] == "部分事实未知或受限，不能扩展结论"
    assert material["target_results"][1]["outcome"] == "本次范围内无当前记录"
    for target, source in zip(material["target_results"], raw["target_results"], strict=True):
        assert target["tool_context"]["service_references"] == source["service_references"]
        assert target["selection"] == source["selection"]
        assert "query_completion" not in target
    history = directory_history_material(raw)
    _assert_process_codes_absent(history)
    assert history["results"][0]["fact_reading"] == material["results"][0]["fact_reading"]
    assert "fact_reading" not in history["results"][1]
    assert history["target_results"] == material["target_results"]
    assert raw == before


@pytest.mark.parametrize("details", [False, True])
@pytest.mark.parametrize(
    ("fact_kind", "field"), [("official_contact", "contact_value"), ("policy", "content_text")]
)
def test_process_code_omission_preserves_formal_text_and_all_tool_references(
    details, fact_kind, field
):
    text = "正式字段原文 query_completion=incomplete，不完整一词也保留"
    record = _relationship_record(fact_kind, "one", **{field: text})
    raw = {
        "kind": "directory_facts",
        "status": "known",
        "fact_kind": fact_kind,
        "query_completion": "incomplete",
        "records": [record],
        "query": {"scope": "selected", "reference": record["reference"]},
        "quality": {"evidence_status": "forbidden", "statements": []},
    }
    before = deepcopy(raw)
    projected = directory_model_projection(raw)
    projected_before = deepcopy(projected)
    current = directory_model_material(
        projected, include_supporting_evidence=details, include_routing_details=details
    )
    history = directory_history_material(projected)
    for material in (current, history):
        _assert_process_codes_absent(material)
        if fact_kind == "policy":
            assert material["records"][0]["original_text"]["excerpt"] == text
            assert material["records"][0]["original_text"]["complete"] is True
        else:
            assert material["records"][0]["readable_facts"]["联系方式"] == text
        assert material["records"][0]["tool_context"]["reference"] == record["reference"]
        assert material["query_scope"]["reference"] == record["reference"]
        assert material["readable_quality"]["reading"] == "当前身份无权读取质量声明"
    assert raw == before
    assert projected == projected_before


def test_service_dossier_facts_are_not_duplicated_in_results_and_history_restores_them():
    from app.composition.school_fact_directory_lookup import FactLookupInput, lookup
    from tests.test_f2_directory_gateway import _history_service_facts

    facts = _history_service_facts()
    raw = lookup(
        facts,
        actor=actor(),
        payload=FactLookupInput(
            subject_kind="school_service",
            scope="all_authorized",
            include=("official_contact", "organization_campus_relationship"),
        ),
    )
    projected = directory_model_projection(raw)
    material = directory_model_material(projected)
    rows = [row for view in material["results"] for row in view["records"]]
    assert all("readable_facts" not in row for row in rows)
    assert all(row["readable_facts_in"] == "service_results" for row in rows)
    assert all(row["source"] for row in rows)
    history = directory_history_material(projected)
    history_rows = [row for view in history["results"] for row in view["records"]]
    assert all("readable_facts" in row for row in history_rows)
    assert all("readable_facts_in" not in row for row in history_rows)


@pytest.mark.parametrize("case", ["different_ref", "different_value", "absent"])
def test_dossier_fact_dedup_requires_matching_reference_and_complete_fields(case):
    from app.composition.school_fact_directory_gateway import _deduplicate_service_facts

    row = {
        "tool_context": {"reference": "f2:organization:one"},
        "readable_facts": {"名称": "同名", "说明": "完整"},
        "source": {"label": "原来源"},
    }
    dossier = deepcopy(row)
    if case == "different_ref":
        dossier["tool_context"]["reference"] = "f2:organization:two"
    elif case == "different_value":
        dossier["readable_facts"]["说明"] = "另一个值"
    else:
        dossier = {}
    bundle = {"results": [{"records": [row]}], "service_results": [{"facts": dossier}]}
    before = deepcopy(bundle)
    _deduplicate_service_facts(bundle)
    assert bundle == before


def _independent_service_target_material():
    from uuid import UUID

    from app.composition.school_fact_directory_lookup import FactLookupInput, lookup
    from tests.test_f2_directory_gateway import _history_service_facts

    facts = _history_service_facts()
    original = facts.rows[("school_service", None)]
    targets = []
    for index, row in enumerate(original["records"]):
        selected = deepcopy(original)
        selected["records"] = [deepcopy(row)]
        selected["matched_count"] = 1
        for number, claim in enumerate(selected["quality"]["statements"]):
            claim["applies_to_records"] = [row["reference"]]
            claim["reference"] = "f2:fact_quality:" + str(UUID(int=1000 + index * 10 + number))
        name = row["fields"]["display_name"]
        facts.rows[("school_service", name)] = selected
        targets.append({"query_text": name})
    raw = lookup(
        facts,
        actor=actor(),
        payload=FactLookupInput(
            subject_kind="school_service",
            targets=tuple(targets),
            include=("official_contact", "organization_campus_relationship"),
        ),
    )
    return directory_model_projection(raw)


def test_independent_long_reference_service_quality_fits_current_and_history():
    import json

    projected = _independent_service_target_material()
    assert len(projected["results"]) == 6
    current = directory_model_material(projected)
    history = directory_history_material(projected)
    assert len(json.dumps(current, ensure_ascii=False)) <= 24_000
    assert len(json.dumps([history], ensure_ascii=False, separators=(",", ":"))) <= 8_000
    service_views = [
        view for view in history["results"] if view["tool_context"]["fact_kind"] == "school_service"
    ]
    for view in service_views:
        group = view["readable_quality"]["statement_groups"][0]
        assert group["tool_context"]["applies_to_records"] == [
            view["records"][0]["tool_context"]["reference"]
        ]
        assert len(group["statements"]) == 4
    assert service_views[0]["readable_quality"] != service_views[1]["readable_quality"]


@pytest.mark.parametrize("case", ["absent", "conflict", "existing_conflict"])
def test_history_dossier_restore_rejects_missing_or_conflicting_mapping(case):
    from app.composition.school_fact_directory_gateway import _deduplicate_service_facts
    from app.composition.school_fact_directory_product import ContactProductUnavailable

    row = {
        "tool_context": {"reference": "f2:organization:one"},
        "readable_facts_in": "service_results",
    }
    dossier = {
        "tool_context": {"reference": "f2:organization:one"},
        "readable_facts": {"名称": "原值"},
    }
    services = [] if case == "absent" else [{"facts": dossier}]
    if case == "conflict":
        other = deepcopy(dossier)
        other["readable_facts"]["名称"] = "不同值"
        services.append({"facts": other})
    if case == "existing_conflict":
        row["readable_facts"] = {"名称": "不能覆盖的不同值"}
    with pytest.raises(ContactProductUnavailable):
        _deduplicate_service_facts(
            {"results": [{"records": [row]}], "service_results": services}, restore=True
        )


@pytest.mark.parametrize("state", ["denied", "source_unavailable", "different_count"])
def test_selected_history_never_inherits_counts_for_missing_or_different_results(state):
    raw = _independent_service_target_material()
    if state == "different_count":
        raw["results"][-1]["matched_count"] = 2
    else:
        raw["results"].append(
            {"kind": "directory_facts", "fact_kind": "policy", "status": state, "records": []}
        )
    history = directory_history_material(raw)
    shared = history.get("result_set_semantics", {})
    assert "matched_count" not in shared
    if state == "different_count":
        assert history["results"][-1]["authorized_result_set"]["matched_count"] == 2
    else:
        assert "authorized_result_set" not in history["results"][-1]
        assert "result_set_semantics" not in history


def test_dossier_source_uses_only_same_record_complete_result_source():
    material = directory_model_material(_independent_service_target_material())
    for service in material["service_results"]:
        local = service["facts"]
        assert local["source_in"] == "results"
        assert "source" not in local
        reference = local["tool_context"]["reference"]
        result_row = next(
            row
            for view in material["results"]
            for row in view["records"]
            if row["tool_context"]["reference"] == reference
        )
        assert result_row["source"]["label"] == "学校正式发布资料"
        assert result_row["source"]["published_at"] == "2026-09-01T09:30:00+00:00"


@pytest.mark.parametrize("difference", ["reference", "source", "absent", "conflicting_result"])
def test_dossier_source_never_merges_other_objects_or_incomplete_sources(difference):
    from app.composition.school_fact_directory_gateway import _deduplicate_service_sources

    row = {
        "tool_context": {"reference": "f2:organization:one"},
        "source": {"label": "同名来源", "published_at": "2026-09-01"},
    }
    dossier = deepcopy(row)
    rows = [row]
    if difference == "reference":
        dossier["tool_context"]["reference"] = "f2:organization:two"
    elif difference == "source":
        dossier["source"]["published_at"] = "2026-09-02"
    elif difference == "absent":
        rows = []
    else:
        other = deepcopy(row)
        other["source"]["published_at"] = "2026-09-03"
        rows.append(other)
    bundle = {"results": [{"records": rows}], "service_results": [{"facts": dossier}]}
    original = deepcopy(bundle)
    _deduplicate_service_sources(bundle)
    assert bundle == original


def test_quality_owner_filter_is_not_the_fact_under_review_when_target_is_null():
    raw = _independent_service_target_material()
    original = deepcopy(raw)
    current = directory_model_material(raw)
    first = current["service_results"][0]
    claim = first["facts"]["quality_statements"][0]
    assert claim["scope"]["applies_to_names"] == [first["service_name"]]
    assert claim["scope"]["owner_role"] == "归属筛选，非受评事实或发布者"
    assert claim["scope"]["owner_filter_type"] == "组织"
    assert "scope_owner_name" not in claim["scope"]
    assert raw == original


def test_default_record_version_has_no_model_operand_and_raw_evidence_is_preserved():
    raw = _independent_service_target_material()
    original = deepcopy(raw)
    material = directory_model_material(raw)
    assert all("version" not in row for view in material["results"] for row in view["records"])
    assert all("version" in row for view in raw["results"] for row in view["records"])
    detailed = directory_model_material(raw, include_supporting_evidence=True)
    assert all("version" in row for view in detailed["results"] for row in view["records"])
    assert raw == original


def test_real_quality_evaluator_projects_owner_filter_to_only_matching_service():
    from datetime import UTC, datetime

    from app.platform.school_facts.f2_quality import QualityFact, evaluate_quality

    at = datetime(2026, 9, 12, tzinfo=UTC)
    start = datetime(2026, 8, 1, tzinfo=UTC)
    owner = _relationship_record("organization", "independent-a", display_name="资料事务组")
    other = _relationship_record("organization", "independent-b", display_name="研究事务组")
    service = _relationship_record(
        "school_service",
        "independent-s",
        display_name="样本查验",
        organization_ref=owner["reference"],
    )
    unrelated = _relationship_record(
        "school_service",
        "independent-t",
        display_name="器材登记",
        organization_ref=other["reference"],
    )
    statement = QualityFact(
        "f2:fact_quality:independent-q",
        "fact_quality",
        {
            "owner_kind": "organization",
            "organization_ref": owner["reference"],
            "target_fact_kind": "school_service",
            "target_ref": None,
            "quality_kind": "source",
            "source_status": "unavailable",
            "verified_at": "2026-09-01T00:00:00+00:00",
        },
        start,
        None,
    )
    quality = evaluate_quality(
        fact_kind="school_service",
        reference=None,
        related_ref=None,
        name=None,
        factual_at=at,
        records=[
            QualityFact(row["reference"], "school_service", row["fields"], start, None)
            for row in (service, unrelated)
        ],
        statements=[statement],
        query_status="known",
        evidence_status="known",
        evidence_truncated=False,
    )
    assert quality["statements"][0]["applies_to_records"] == [service["reference"]]
    assert quality["statements"][0]["owner_ref"] == owner["reference"]
    raw = {"records": [service, unrelated, owner, other], "quality": quality}
    current = directory_model_material(raw)
    claim = current["readable_quality"]["statements"][0]
    assert claim["scope"]["applies_to_names"] == ["样本查验"]
    assert "owner_filter_name" not in claim["scope"]
    assert claim["tool_context"]["owner_ref"] == owner["reference"]
    assert claim["scope"]["target_fact_type"] == "学校服务目录"
    assert claim["tool_context"]["applies_to_records"] == [service["reference"]]
    assert claim["details"]["来源可用状态"] == "声明记载原来源不可用"


def test_default_quality_keeps_applicable_fact_names_and_owner_reference_without_owner_name_copy():
    raw = _independent_service_target_material()
    before = deepcopy(raw)
    current = directory_model_material(raw)
    claims = current["results"][0]["readable_quality"]["statements"]
    for claim in claims:
        assert "owner_filter_name" not in claim["scope"]
        assert claim["scope"]["applies_to_names"] == [current["service_results"][0]["service_name"]]
        assert claim["scope"]["owner_filter_type"] == "组织"
        assert (
            claim["tool_context"]["owner_ref"]
            == raw["results"][0]["quality"]["statements"][0]["owner_ref"]
        )
    assert raw == before


def test_receipt_page_shares_boundary_and_preserves_complete_tool_references():
    from app.composition.school_fact_directory_action_material import own_receipts_model_material
    from tests.test_f2_own_receipt_gateway import receipt

    raw = {
        "items": [receipt(), receipt("sf2receipt_" + "b" * 64)],
        "has_more": True,
        "notice": "仅当前身份可读的本人最近正式回执，不代表全部历史。",
    }
    original = deepcopy(raw)
    material = own_receipts_model_material(raw)
    assert raw == original
    assert material["has_more"] is True
    assert len(material["receipts"]) == 2
    assert "只读回执" in material["meaning"]
    assert "仅核对原操作结果" in material["meaning"]
    for item, source in zip(material["receipts"], raw["items"], strict=True):
        assert "meaning" not in item
        assert "target_version" not in item
        assert "school_fact_version" not in item
        assert item["tool_context"]["school_fact_version"] == source["target_version"]
        assert item["tool_context"]["receipt_ref"] == source["reference"]
        assert "targets" not in item["tool_context"]
        assert item["status"] == source["status"]
        assert item["target_count"] == len(source["targets"])
        assert material["read_detail"] == {
            "tool": "school_facts.directory.own_receipt",
            "argument": "receipt_ref",
        }
        assert item["confirmed_at"] == source["confirmed_at"]
        assert item["outcome"] == "正式修改已完成"


def test_receipt_material_separates_school_and_each_historical_record_version():
    from app.composition.school_fact_directory_action_material import (
        own_receipt_model_material,
        own_receipts_model_material,
    )
    from tests.test_f2_own_receipt_gateway import receipt

    raw = receipt()
    raw["target_version"] = 36
    raw["targets"][0]["target_version"] = 2
    other = deepcopy(raw["targets"][0])
    other.update(reference="f4:teaching_assignment:" + "b" * 64, target_version=7)
    raw["targets"].append(other)
    original = deepcopy(raw)
    material = own_receipt_model_material(raw, include_audit=True)
    assert "school_fact_version" not in material
    assert material["tool_context"]["school_fact_version"] == 36
    assert "target_version" not in material
    assert [(x["reference"], x["record_version"]) for x in material["tool_context"]["targets"]] == [
        (x["reference"], x["target_version"]) for x in raw["targets"]
    ]
    assert "meaning" not in material
    assert "原操作" in material["tool_context"]["meaning"]
    assert "school_fact_version" not in material["tool_context"]["meaning"]
    assert "record_version" not in material["tool_context"]["meaning"]
    assert "不是当前记录版本" in material["tool_context"]["version_meaning"]
    page = own_receipts_model_material({"items": [raw], "has_more": False, "notice": "只读"})
    assert "school_fact_version" not in page["receipts"][0]
    assert "school_fact_version" not in page["meaning"]
    assert page["receipts"][0]["tool_context"] == {
        "receipt_ref": raw["reference"], "school_fact_version": 36,
    }
    assert "原操作后" in page["tool_context"]["version_meaning"]
    assert raw == original


@pytest.mark.parametrize(
    "status,version", [("unknown", None), ("rejected", None), ("succeeded", 36)]
)
def test_receipt_without_targets_never_invents_record_version(status, version):
    from app.composition.school_fact_directory_action_material import receipt_model_material

    raw = {
        "status": status,
        "message": "原结果",
        "reference": "receipt-ref",
        "request_id": "request-ref",
        "target_version": version,
    }
    original = deepcopy(raw)
    result = receipt_model_material(raw)
    assert "record_version" not in str(result)
    assert "target_version" not in result
    assert "school_fact_version" not in result
    assert result["tool_context"].get("school_fact_version") == version
    assert result["tool_context"] == {
        "reference": "receipt-ref", "request_id": "request-ref",
        **({"school_fact_version": version} if version is not None else {}),
    }
    if status == "unknown":
        assert result["outcome"] == "原操作结果尚未确定" and "recovery" in result
    assert raw == original


@pytest.mark.parametrize("kind", ["teaching_assignment", "teaching_offering"])
def test_own_receipt_teaching_target_has_exact_current_read_contract(kind):
    from app.composition.school_fact_directory_action_material import own_receipt_model_material
    from tests.test_f2_own_receipt_gateway import receipt

    raw = receipt()
    reference = f"f4:{kind}:" + "a" * 64
    raw["targets"][0].update(fact_kind=kind, reference=reference)
    original = deepcopy(raw)
    target = own_receipt_model_material(raw)["tool_context"]["targets"][0]
    assert target["current_read"] == {
        "tool": "school_facts.teaching.query",
        "arguments": {"view": "teaching", "target_reference": reference},
    }
    assert raw == original


@pytest.mark.parametrize(
    "kind,reference",
    [
        ("teaching_assignment", "f4:teaching_offering:" + "a" * 64),
        ("teaching_assignment", "f4:teaching_assignment:not-a-signed-reference"),
        ("person", "f4:teaching_assignment:" + "a" * 64),
    ],
)
def test_receipt_consumer_does_not_reinterpret_unmatched_reference(kind, reference):
    from app.composition.school_fact_directory_action_material import own_receipt_model_material
    from tests.test_f2_own_receipt_gateway import receipt

    raw = receipt()
    raw["targets"][0].update(fact_kind=kind, reference=reference)
    target = own_receipt_model_material(raw)["tool_context"]["targets"][0]
    assert "current_read" not in target
    assert target["reference"] == reference


@pytest.mark.parametrize("owner", ["directory", "family_staff", "education"])
def test_receipt_directory_large_targets_are_available_through_exact_detail(owner):
    import json

    from app.composition.school_fact_directory_action_material import (
        own_receipt_model_material,
        own_receipts_model_material,
    )
    from tests.test_f2_own_receipt_gateway import receipt

    items = []
    for index in range(20):
        item = receipt("sf2receipt_" + f"{index:064x}")
        item["targets"] = [
            dict(
                item["targets"][0],
                fact_kind="teaching_assignment",
                label="任教",
                reference="f4:teaching_assignment:" + f"{number:064x}",
            )
            for number in range(100)
        ]
        item["changed_count"] = 100
        items.append(item)
    raw = {"items": items, "has_more": True, "notice": "仅当前身份本人获准回执"}
    before = deepcopy(raw)
    result = own_receipts_model_material(raw, detail_tool=f"school_facts.{owner}.own_receipt")
    assert len(json.dumps(result, ensure_ascii=False)) < 24000
    assert result["read_detail"] == {
        "tool": f"school_facts.{owner}.own_receipt",
        "argument": "receipt_ref",
    }
    for summary, source in zip(result["receipts"], items, strict=True):
        assert summary["target_count"] == 100
        assert summary["changed_count"] == 100
        assert summary["fact_types"] == ["任教"]
        assert summary["tool_context"] == {
            "receipt_ref": source["reference"], "school_fact_version": source["target_version"],
        }
        references = []
        for offset in range(0, 100, 20):
            detail = own_receipt_model_material(
                source, target_offset=offset, detail_tool=result["read_detail"]["tool"]
            )
            assert len(json.dumps(detail, ensure_ascii=False)) < 24000
            assert len(detail["tool_context"]["targets"]) == 20
            assert detail["tool_context"]["target_page"]["total_count"] == 100
            references.extend(t["reference"] for t in detail["tool_context"]["targets"])
        assert references == [t["reference"] for t in source["targets"]]
    assert raw == before


@pytest.mark.parametrize(
    "status,outcome", [("unknown", "原操作结果尚未确定"), ("rejected", "正式修改被拒绝")]
)
def test_receipt_directory_keeps_unsuccessful_outcome(status, outcome):
    from app.composition.school_fact_directory_action_material import own_receipts_model_material
    from tests.test_f2_own_receipt_gateway import receipt

    item = receipt()
    item.update(status=status, target_version=None, changed_count=0)
    result = own_receipts_model_material({"items": [item], "has_more": False, "notice": "只读"})
    summary = result["receipts"][0]
    assert summary["status"] == status
    assert summary["outcome"] == outcome
    assert "school_fact_version" not in summary
    if status == "unknown":
        assert "recovery" in summary


def test_receipt_directory_empty_page_is_not_absent_history():
    from app.composition.school_fact_directory_action_material import own_receipts_model_material

    result = own_receipts_model_material({"items": [], "has_more": False, "notice": "当前获准页"})
    assert result["receipts"] == []
    assert "不能据此断言没有操作历史" in result["meaning"]


@pytest.mark.parametrize("status", ["succeeded", "rejected", "unknown"])
@pytest.mark.parametrize("current_status", ["available", "empty", "denied", "source_unavailable"])
def test_combined_receipt_keeps_independent_evidence_and_unread_change_history(
    status, current_status
):
    from app.composition.school_fact_directory_action_material import (
        own_receipt_current_model_material,
        own_receipt_model_material,
    )
    from tests.test_f2_own_receipt_gateway import receipt

    raw = receipt()
    reference = "f4:teaching_assignment:" + "a" * 64
    raw["status"] = status
    raw["targets"][0].update(
        fact_kind="teaching_assignment", reference=reference, target_version=2
    )
    if status != "succeeded":
        raw.update(changed_count=0, target_version=None)
    original_card = deepcopy(raw)
    original = own_receipt_model_material(raw)
    current = {
        "tool_context": {"fact_kind": "teaching_assignment", "reference": reference},
        "result": {"status": current_status},
    }
    if current_status in {"available", "empty"}:
        current["result"].update(
            items=[{"任课教师": "合成教师乙", "record_version": 4}]
            if current_status == "available" else [],
            source_label="合成正式任教来源",
            effective_at="2026-09-15T00:00:00+08:00",
            recorded_at="2026-09-14T19:00:00+08:00",
        )
    before = deepcopy((original, [current]))

    material = own_receipt_current_model_material(original, [current])

    assert material["kind"] == "own_receipt_current"
    assert material["receipt"] == before[0]
    assert material["current_results"] == before[1]
    assert (original, [current]) == before
    assert raw == original_card
    context = material["tool_context"]
    assert context["relationship_basis"] == "original_receipt_target_reference"
    assert "change_history" not in context
    scope = material["evidence_scope"]
    assert scope["original"]["field_values"] == "not_returned"
    assert "执行结果" in scope["original"]["meaning"]
    assert "历史记录版本" in scope["original"]["meaning"]
    assert "不含修改前后字段正文" in scope["original"]["meaning"]
    assert scope["current"]["reading"] == "independent_per_target"
    assert "reference" in scope["current"]["meaning"]
    assert "同一原目标" in scope["current"]["meaning"]
    assert "独立当前读取" in scope["current"]["meaning"]
    assert "各项result" in scope["current"]["meaning"]
    assert scope["interval"]["events"] == "not_queried"
    assert "未查询" in scope["interval"]["meaning"]
    assert "两时点之间" in scope["interval"]["meaning"]
    assert "是否发生变更" in scope["interval"]["meaning"]
    assert "原因" in scope["interval"]["meaning"]


def test_combined_receipt_preserves_distinct_target_coordinates_sources_and_pagination():
    from app.composition.school_fact_directory_action_material import (
        own_receipt_current_model_material,
        own_receipt_model_material,
    )
    from tests.test_f2_own_receipt_gateway import receipt

    raw = receipt()
    raw["targets"] = [
        dict(
            raw["targets"][0],
            fact_kind="teaching_assignment", label="任教",
            reference="f4:teaching_assignment:" + f"{number:064x}", target_version=number + 1,
        )
        for number in range(4)
    ]
    original_card = deepcopy(raw)
    original = own_receipt_model_material(raw, target_limit=3, include_audit=True)
    current_results = [
        {
            "tool_context": {"fact_kind": target["fact_kind"], "reference": target["reference"]},
            "result": {
                "items": [{"课程名称": "同名合成课程", "任课教师": f"合成教师{index}"}],
                "source_label": f"合成来源{index}",
                "tool_context": {"target_reference": target["reference"]},
            },
        }
        for index, target in enumerate(raw["targets"][:3])
    ]

    material = own_receipt_current_model_material(original, current_results)

    assert material["receipt"] == original
    assert material["current_results"] == current_results
    context = material["receipt"]["tool_context"]
    assert context["receipt_ref"] == raw["reference"]
    assert context["target_page"] == {
        "offset": 0, "end": 3, "total_count": 4, "has_more": True,
    }
    assert context["read_next"]["arguments"]["target_offset"] == 3
    assert raw == original_card
    material["receipt"]["tool_context"]["targets"][0]["record_version"] = 999
    material["current_results"][0]["result"]["source_label"] = "另一个来源"
    assert original["tool_context"]["targets"][0]["record_version"] == 1
    assert current_results[0]["result"]["source_label"] == "合成来源0"


@pytest.mark.parametrize("status", ["succeeded", "rejected", "unknown"])
def test_own_receipt_audit_is_opt_in_without_losing_result_or_exact_next_reads(status):
    from app.composition.school_fact_directory_action_material import own_receipt_model_material
    from tests.test_f2_own_receipt_gateway import receipt

    raw = receipt()
    raw["status"] = status
    raw["targets"][0].update(
        fact_kind="teaching_assignment", reference="f4:teaching_assignment:" + "a" * 64,
        target_version=3,
    )
    raw["targets"].append(dict(raw["targets"][0], reference="f4:teaching_assignment:" + "b" * 64))
    if status != "succeeded":
        raw.update(changed_count=0, target_version=None)
    before = deepcopy(raw)

    material = own_receipt_model_material(raw, target_limit=1)

    context = material["tool_context"]
    for audit_key in ("confirmed_at", "school_fact_version", "version_meaning"):
        assert audit_key not in context
    assert "record_version" not in context["targets"][0]
    assert context["receipt_ref"] == raw["reference"]
    assert context["targets"][0]["reference"] == raw["targets"][0]["reference"]
    assert context["targets"][0]["current_read"]["arguments"]["target_reference"] == (
        raw["targets"][0]["reference"]
    )
    assert context["read_next"]["arguments"] == {
        "receipt_ref": raw["reference"], "target_offset": 1, "target_limit": 1,
    }
    assert "原操作" in context["meaning"]
    assert "未返回" in context["meaning"]
    assert "维护权" not in context["meaning"]
    assert raw["confirmed_at"] not in str(material)
    assert material["outcome"] == {
        "succeeded": "正式修改已完成",
        "rejected": "正式修改被拒绝",
        "unknown": "原操作结果尚未确定",
    }[status]
    if status == "unknown":
        assert "recovery" in material
    assert raw == before


def test_own_receipt_explicit_audit_preserves_exact_times_versions_and_page_selection():
    from app.composition.school_fact_directory_action_material import own_receipt_model_material
    from tests.test_f2_own_receipt_gateway import receipt

    raw = receipt()
    raw["target_version"] = 82
    raw["confirmed_at"] = "2026-09-12T12:00:00.123456Z"
    raw["targets"][0]["target_version"] = 2
    raw["targets"].append(dict(raw["targets"][0], reference="f2:official_contact:YWJjZGVmZ2k"))
    before = deepcopy(raw)

    material = own_receipt_model_material(raw, target_limit=1, include_audit=True)

    context = material["tool_context"]
    assert context["confirmed_at"] == raw["confirmed_at"]
    assert context["school_fact_version"] == 82
    assert context["targets"][0]["record_version"] == 2
    assert "不是当前记录版本" in context["version_meaning"]
    assert "维护权" in context["meaning"]
    assert context["read_next"]["arguments"] == {
        "receipt_ref": raw["reference"], "target_offset": 1, "target_limit": 1,
        "include_audit": True,
    }
    assert raw == before


def test_combined_receipt_default_does_not_reintroduce_original_audit_values():
    from app.composition.school_fact_directory_action_material import (
        own_receipt_current_model_material,
        own_receipt_model_material,
    )
    from tests.test_f2_own_receipt_gateway import receipt

    raw = receipt()
    current = [{
        "tool_context": {"reference": raw["targets"][0]["reference"]},
        "result": {"status": "denied", "message": "当前字段不可读"},
    }]

    result = own_receipt_current_model_material(own_receipt_model_material(raw), current)

    assert "school_fact_version" not in str(result)
    assert "record_version" not in str(result)
    assert "confirmed_at" not in str(result)
    assert result["current_results"] == current
    assert result["evidence_scope"]["original"]["field_values"] == "not_returned"
    assert result["evidence_scope"]["interval"]["events"] == "not_queried"


@pytest.mark.parametrize("original_status", ["available", "denied", "source_unavailable"])
def test_combined_original_fields_keep_each_result_independent_from_current(original_status):
    from app.composition.school_fact_directory_action_material import (
        own_receipt_current_model_material,
    )

    reference = "f4:teaching_assignment:" + "a" * 64
    receipt = {"outcome": "正式修改已完成", "tool_context": {"receipt_ref": "原回执引用"}}
    original = [{
        "tool_context": {"reference": reference, "fact_kind": "teaching_assignment"},
        "result": {
            "status": original_status,
            "items": [{"教师": "合成教师甲", "来源": "原版本来源"}]
            if original_status == "available" else [],
            "scope": {"reading": "original_target_version"},
        },
    }]
    current = [{
        "tool_context": {"reference": reference, "fact_kind": "teaching_assignment"},
        "result": {"status": "available", "items": [{"教师": "合成教师乙", "来源": "当前来源"}]},
    }]
    before = deepcopy((receipt, original, current))

    result = own_receipt_current_model_material(receipt, current, original_results=original)

    assert result["receipt"] == receipt
    assert result["original_results"] == original
    assert result["current_results"] == current
    scope = result["evidence_scope"]
    assert scope["original"]["field_values"] == "per_target_result"
    assert "各项result" in scope["original"]["meaning"]
    assert scope["current"]["reading"] == "independent_per_target"
    assert scope["interval"]["events"] == "not_queried"
    assert "是否发生变更" in scope["interval"]["meaning"]
    result["original_results"][0]["result"]["scope"]["reading"] = "不能改写原读取"
    result["current_results"][0]["result"]["items"].clear()
    result["receipt"]["outcome"] = "不能改写原回执"
    assert (receipt, original, current) == before


def test_original_fields_without_current_read_does_not_imply_current_or_interval_facts():
    from app.composition.school_fact_directory_action_material import (
        own_receipt_current_model_material,
    )

    original = [{
        "tool_context": {"reference": "f4:teaching_assignment:" + "b" * 64},
        "result": {"status": "denied", "items": [], "scope": {"reading": "original"}},
    }]

    result = own_receipt_current_model_material(
        {"outcome": "正式修改已完成"}, [], original_results=original, current_requested=False
    )

    assert result["original_results"] == original
    assert result["current_results"] == []
    scope = result["evidence_scope"]
    assert scope["original"]["field_values"] == "per_target_result"
    assert scope["current"]["reading"] == "not_requested"
    assert "未请求" in scope["current"]["meaning"]
    assert scope["interval"]["events"] == "not_queried"
    assert "是否发生变更" in scope["interval"]["meaning"]
    assert "两时点" not in scope["interval"]["meaning"]


def test_combined_original_results_absent_and_empty_are_not_interchangeable():
    from app.composition.school_fact_directory_action_material import (
        own_receipt_current_model_material,
    )

    legacy = own_receipt_current_model_material({"outcome": "正式修改已完成"}, [])
    provided = own_receipt_current_model_material(
        {"outcome": "正式修改已完成"}, [], original_results=[]
    )

    assert "original_results" not in legacy
    assert legacy["evidence_scope"]["original"]["field_values"] == "not_returned"
    assert legacy["evidence_scope"]["current"]["reading"] == "independent_per_target"
    assert provided["original_results"] == []
    assert provided["evidence_scope"]["original"]["field_values"] == "per_target_result"
