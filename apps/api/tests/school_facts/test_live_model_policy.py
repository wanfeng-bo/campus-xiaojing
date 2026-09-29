import asyncio
import json
from contextvars import ContextVar
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx2
import pytest

from app.composition import school_fact_pydanticai as pydanticai_assembly
from app.composition.school_fact_answer_evidence import answer_evidence_material
from app.composition.school_fact_directory_action_material import (
    own_receipt_current_model_material,
    proposal_model_material,
    receipt_model_material,
)
from app.composition.school_fact_directory_gateway import directory_model_material
from app.composition.school_fact_education_gateway import education_model_material
from app.composition.school_fact_family_staff_gateway import _safe_material
from app.composition.school_fact_live_model_policy import (
    LiveModelDataPolicyError,
    guard_live_wire,
    project_live_material,
    wrap_live_gateway,
)
from app.composition.school_fact_live_settings import ApprovedLiveModelDataPolicy
from app.composition.school_fact_product_contacts import (
    ContactCard,
    ContactCoverage,
    ContactListing,
    ContactQuerySelection,
    ContactQueryView,
    ContactSource,
    model_contact_query_view,
)
from app.composition.school_fact_pydanticai_model import _GuardedTransport
from app.composition.school_fact_residence_gateway import residence_model_material
from app.interfaces.http.assistant import _service_failure_payload
from app.platform.assistant.runtime import (
    AssistantServiceError,
    ContinuationEvidence,
    HistoryCheck,
    ResultContinuation,
)
from app.platform.assistant.tool_loop import ToolExecutionResult


def test_projection_keeps_task_facts_and_replaces_unknown_structured_data():
    value = {
        "status": "known",
        "records": [
            {
                "name": "王老师",
                "class_name": "七年级一班",
                "subject": "数学",
                "valid_from": "2026-09-01",
                "official_contact": "校办公室",
                "private_phone": "13900000001",
                "bed_name": "1号床",
                "future_unclassified": {"payload": "do-not-send"},
            }
        ],
        "query_scope": {"scope": "selected", "as_of": "2026-09-27"},
    }
    result = project_live_material(value)
    encoded = json.dumps(result, ensure_ascii=False)
    assert all(s in encoded for s in ("王老师", "七年级一班", "数学", "校办公室", "2026-09-27"))
    assert all(s not in encoded for s in ("13900000001", "1号床", "do-not-send"))
    assert result["records"][0]["field_states"]["bed_name"] == "trusted_view_only"


def test_residence_bed_name_alias_is_held_even_when_generic_name_is_safe():
    value = {
        "records": [
            {"fact_kind": "dorm_bed", "name": "秘密床位A", "status": "known"},
            {"fact_kind": "administrative_class", "name": "七年级一班", "status": "known"},
        ]
    }
    projected = project_live_material(value)
    assert "秘密床位A" not in json.dumps(projected, ensure_ascii=False)
    assert "七年级一班" in json.dumps(projected, ensure_ascii=False)
    assert projected["records"][0]["field_states"]["name"] == "trusted_view_only"


def test_actual_official_contact_model_view_keeps_published_phone():
    view = ContactQueryView(
        status="known",
        items=(
            ContactCard(
                reference="contact-ref",
                campus_ref="campus-ref",
                campus_name="主校区",
                contact_kind="phone",
                contact_value="13800000000",
                service_hours="工作日",
                valid_from=datetime.fromisoformat("2026-09-01T00:00:00+08:00"),
                valid_to=None,
                version=1,
                source=ContactSource(
                    kind="official",
                    label="学校发布",
                    published_at=datetime.fromisoformat("2026-09-01T00:00:00+08:00"),
                    reference="source-ref",
                ),
            ),
        ),
        query=ContactQuerySelection(
            subject="official_contacts", scope="selected", campus_name="主校区", campus_ref=None
        ),
        listing=ContactListing(
            entity="contacts", returned_count=1, matched_count=1, truncated=False
        ),
        coverage=ContactCoverage(status="partial", description="仅发布来源"),
        queried_at=datetime.fromisoformat("2026-09-27T00:00:00+08:00"),
    )
    source = model_contact_query_view(view).model_dump(mode="json")
    projected = project_live_material(source)
    assert projected["items"][0]["contact_value"] == "13800000000"
    guard_live_wire(
        json.dumps({"messages": [{"role": "tool", "content": json.dumps(projected)}]}).encode()
    )


def test_actual_education_material_keeps_teacher_role_class_and_subject():
    source = education_model_material(
        {
            "status": "known",
            "fact_kind": "class_teacher_assignment",
            "fields": {
                "teacher_role": "primary",
                "class_name": "七年级一班",
                "teacher_name": "王老师",
                "subject": "数学",
                "private_phone": "13900000009",
            },
            "source": {"label": "学校登记", "published_at": "2026-09-01"},
        }
    )
    projected = project_live_material(source)
    encoded = json.dumps(projected, ensure_ascii=False)
    assert all(value in encoded for value in ("班主任", "七年级一班", "王老师", "数学", "学校登记"))
    assert "13900000009" not in encoded


def test_actual_teaching_material_keeps_conflict_and_validity():
    source = education_model_material(
        {
            "status": "known",
            "items": [
                {
                    "course": "数学",
                    "offering": "正式开课",
                    "offering_status": "持续开设",
                    "teachers": [{"name": "王老师", "role": "主讲", "subjects": ["数学"]}],
                    "teacher_status": "conflict",
                    "notices": ["教师安排存在争议"],
                    "quality": [{"dispute_status": "open"}],
                    "effective_from": "2026-09-01T00:00:00+08:00",
                    "effective_to": "2027-01-01T00:00:00+08:00",
                }
            ],
            "reality_coverage": {"status": "partial"},
        },
        teaching_answer=True,
        query_answer=True,
    )
    projected = project_live_material(source)
    encoded = json.dumps(projected, ensure_ascii=False)
    assert all(
        value in encoded
        for value in ("数学", "王老师", "conflict", "教师安排存在争议", "2027-01-01")
    )


def test_actual_family_profile_material_keeps_child_school_state():
    source = _safe_material(
        {
            "status": "known",
            "profile": {
                "name": "孩子甲",
                "class_name": "七年级一班",
                "enrollment_status": "enrolled",
                "private_phone": "13900000007",
            },
            "relationship_kind": "verified_guardian",
        }
    )
    projected = project_live_material(source)
    encoded = json.dumps(projected, ensure_ascii=False)
    assert all(
        value in encoded for value in ("孩子甲", "七年级一班", "verified_guardian", "enrolled")
    )
    assert "13900000007" not in encoded


def test_actual_residence_material_keeps_state_but_not_bed_details():
    source = residence_model_material(
        {
            "status": "known",
            "fact_kind": "student_residence",
            "records": [
                {
                    "fact_kind": "dorm_bed",
                    "name": "床A",
                    "bed_name": "床A",
                    "valid_from": "2026-09-01",
                }
            ],
        }
    )
    projected = project_live_material(source)
    encoded = json.dumps(projected, ensure_ascii=False)
    assert "known" in encoded and "2026-09-01" in encoded
    assert "床A" not in encoded
    guard_live_wire(json.dumps({"messages": [{"role": "tool", "content": encoded}]}).encode())


def test_actual_directory_records_keep_public_school_org_person_and_policy_fields():
    raw = {
        "kind": "directory_facts",
        "status": "known",
        "records": [
            {
                "fact_kind": "school",
                "reference": "school-ref",
                "fields": {"display_name": "开源示例中学", "timezone_name": "Asia/Shanghai"},
            },
            {
                "fact_kind": "person",
                "reference": "person-ref",
                "fields": {
                    "display_name": "王老师",
                    "person_kind": "staff",
                    "contact_value": "13900000006",
                    "id_number": "123456789012345678",
                },
            },
            {
                "fact_kind": "organization_profile",
                "reference": "org-ref",
                "fields": {"display_name": "教务处", "function_summary": "教学安排"},
            },
            {
                "fact_kind": "policy_version",
                "reference": "policy-ref",
                "fields": {"classification": "public", "content_text": "公开作息制度"},
            },
        ],
    }
    projected = project_live_material(directory_model_material(raw))
    encoded = json.dumps(projected, ensure_ascii=False)
    assert all(
        v in encoded for v in ("开源示例中学", "王老师", "教务处", "教学安排", "公开作息制度")
    )
    assert "13900000006" not in encoded and "123456789012345678" not in encoded
    assert projected["fact_reading"]["outcome"]
    guard_live_wire(json.dumps({"messages": [{"role": "tool", "content": encoded}]}).encode())


def test_actual_directory_contact_and_policy_release_requires_public_scope():
    raw = {
        "kind": "directory_facts",
        "status": "known",
        "records": [
            {
                "fact_kind": "official_contact",
                "fields": {
                    "visibility_scope": "public",
                    "contact_kind": "phone",
                    "contact_value": "13800000008",
                    "service_hours": "工作日",
                },
            },
            {
                "fact_kind": "official_contact",
                "fields": {
                    "visibility_scope": "private",
                    "contact_kind": "mobile",
                    "contact_value": "13900000008",
                },
            },
            {
                "fact_kind": "policy_version",
                "fields": {"classification": "restricted", "content_text": "内部材料正文"},
            },
        ],
    }
    released = project_live_material(directory_model_material(raw))
    encoded = json.dumps(released, ensure_ascii=False)
    assert "13800000008" in encoded and "工作日" in encoded
    assert "13900000008" not in encoded and "内部材料正文" not in encoded
    guard_live_wire(
        json.dumps({"messages": [{"role": "tool", "content": json.dumps(released)}]}).encode()
    )


def test_formal_directory_model_readable_facts_keep_task_values():
    raw = {
        "kind": "directory_facts",
        "status": "known",
        "records": [
            {
                "fact_kind": "school",
                "reference": "f2:school:abc",
                "fields": {"display_name": "开源示例中学", "timezone_name": "Asia/Shanghai"},
                "source": {"kind": "formal_publication", "label": "学校正式发布"},
            },
            {
                "fact_kind": "assignment",
                "reference": "f2:assignment:def",
                "fields": {"position_title": "数学老师", "assignment_kind": "primary"},
                "source": {"kind": "formal_publication", "label": "任职正式发布"},
            },
        ],
    }
    material = directory_model_material(raw)
    assert "readable_facts" in material["records"][0]
    released = project_live_material(material)
    encoded = json.dumps(released, ensure_ascii=False)
    assert all(v in encoded for v in ("开源示例中学", "Asia/Shanghai", "数学老师", "学校正式发布"))
    assert released["records"][0]["tool_context"]["reference"] == "f2:school:abc"


def test_formal_directory_readable_contact_public_only():
    raw = {
        "kind": "directory_facts",
        "status": "known",
        "records": [
            {
                "fact_kind": "official_contact",
                "reference": "f2:official_contact:abc",
                "fields": {
                    "contact_kind": "phone",
                    "contact_value": "13800000003",
                    "visibility_scope": "public",
                    "service_hours": "工作日",
                },
                "source": {"kind": "formal_publication", "label": "学校发布"},
            },
            {
                "fact_kind": "official_contact",
                "reference": "f2:official_contact:def",
                "fields": {
                    "contact_kind": "phone",
                    "contact_value": "13900000003",
                    "visibility_scope": "private",
                },
                "source": {"kind": "formal_publication", "label": "受限来源"},
            },
        ],
    }
    released = project_live_material(directory_model_material(raw))
    encoded = json.dumps(released, ensure_ascii=False)
    assert "13800000003" in encoded and "工作日" in encoded
    assert "13900000003" not in encoded


def test_actual_preview_and_receipt_keep_state_and_safe_locator_without_sensitive_diff():
    proposal = proposal_model_material(
        {
            "proposal_ref": "proposal-1",
            "summary": {
                "fact_kind": "person",
                "operation": "upsert",
                "before": {"display_name": "旧姓名", "contact_value": "13900000005"},
                "after": {"display_name": "新姓名", "contact_value": "13900000004"},
            },
            "expires_at": "2026-09-27T10:00:00+08:00",
        }
    )
    projected = project_live_material(proposal)
    encoded = json.dumps(projected, ensure_ascii=False)
    assert projected["tool_context"]["proposal_ref"] == "proposal-1"
    assert projected["outcome"] and projected["confirmation_requirement"]
    assert "正式名称" in encoded
    assert all(v not in encoded for v in ("13900000005", "13900000004"))

    receipt = receipt_model_material(
        {
            "status": "succeeded",
            "message": "正式完成",
            "reference": "receipt-1",
            "changed_count": 1,
            "target_version": 2,
        }
    )
    current = own_receipt_current_model_material(receipt, [{"status": "known", "name": "王老师"}])
    released = project_live_material(current)
    assert released["receipt"]["outcome"] == "正式修改已完成"
    assert released["current_results"][0]["name"] == "王老师"
    assert released["evidence_scope"]["current"]["reading"] == "independent_per_target"


def test_actual_answer_evidence_keeps_scoped_conflict_code():
    raw = answer_evidence_material(
        {
            "status": "known",
            "name": "七年级一班",
            "quality": {"dispute_status": "open", "scope": "selected"},
        }
    )
    projected = project_live_material(raw)
    encoded = json.dumps(projected, ensure_ascii=False)
    assert "七年级一班" in encoded and "open" in encoded
    assert projected["answer_constraints"]["issues_affecting_answer"]


def test_family_child_selection_reference_and_region_remain_task_usable():
    catalog = project_live_material(
        {"status": "available", "children": [{"child_ref": "opaque-child", "label": "孩子甲"}]}
    )
    assert catalog["children"] == [{"child_ref": "opaque-child", "label": "孩子甲"}]
    profile = project_live_material(
        {
            "status": "available",
            "profile": {
                "name": "孩子甲",
                "campus": "主校区",
                "residence_mode": "住宿",
                "region": {"district_name": "获准区县", "district_code": "private-code"},
                "detailed_address": "门牌号",
            },
        }
    )
    encoded = json.dumps(profile, ensure_ascii=False)
    assert all(value in encoded for value in ("主校区", "住宿", "获准区县"))
    assert "private-code" not in encoded and "门牌号" not in encoded


def test_actual_family_leadership_gateway_model_keeps_leader_and_official_channel():
    from uuid import uuid4

    from app.composition.school_fact_family_leadership_gateway import (
        NAME,
        FamilyLeadershipToolGateway,
    )
    from app.platform.assistant.tool_loop import ModelToolCall, ToolCallContext
    from tests.school_facts.test_f5_family_leadership import fixture

    service, actor, state = fixture()
    state.payload["leaders"][0]["official_contacts"] = [
        {
            "owner_kind": "organization",
            "owner_name": "学校办公室",
            "contact_kind": "phone",
            "contact_value": "13800000007",
            "service_hours": "工作日",
            "source": {"published_at": datetime.fromisoformat("2026-09-01T00:00:00+08:00")},
        }
    ]
    turn = FamilyLeadershipToolGateway(
        resolve_actor=lambda _: SimpleNamespace(actor=actor), query=service
    ).for_turn("trusted")
    original = turn.execute(
        ModelToolCall(call_id="l", name=NAME, arguments={"include_official_contacts": True}),
        ToolCallContext("trusted", "c", "t", "r", 1, uuid4()),
    )
    released = project_live_material(original.model_content)
    encoded = json.dumps(released, ensure_ascii=False)
    assert all(
        value in encoded for value in ("正式校长", "校长", "学校办公室", "13800000007", "工作日")
    )
    assert "publication:test" not in encoded


def test_family_teaching_material_keeps_published_channel_and_teacher_basis():
    material = education_model_material(
        {
            "kind": "family_teaching",
            "items": [
                {
                    "course": "数学",
                    "teachers": [
                        {
                            "name": "陈老师",
                            "private_phone": "13900000002",
                            "basis": {"label": "正式任教登记", "published_at": "2026-09-01"},
                            "official_contacts": [
                                {
                                    "owner": "教务处",
                                    "contact_kind": "email",
                                    "contact_value": "office@example.test",
                                    "service_hours": "工作日",
                                    "source": {
                                        "kind": "formal_publication",
                                        "label": "学校发布",
                                        "published_at": "2026-09-01",
                                    },
                                }
                            ],
                        }
                    ],
                }
            ],
        },
        teaching_answer=True,
        query_answer=True,
    )
    projected = project_live_material(material)
    encoded = json.dumps(projected, ensure_ascii=False)
    assert all(v in encoded for v in ("陈老师", "数学", "office@example.test", "正式任教登记"))
    assert "13900000002" not in encoded


def test_gateway_projects_current_history_and_continuation_but_preserves_card_and_proof():
    card = {"private_phone": "13900000002"}
    check = HistoryCheck(lambda: True)
    evidence = ContinuationEvidence(
        model_content={"name": "王老师", "bed_name": "2号床"}, fact_card=card, history_check=check
    )
    original = ToolExecutionResult(
        model_content={"name": "王老师", "private_phone": "13900000002"},
        fact_card=card,
        history_check=check,
        history_content={"name": "王老师", "id_number": "123456789012345678"},
        continuation=ResultContinuation(reference="opaque", load=lambda: evidence),
    )

    class Gateway:
        descriptors = ()

        def revalidate(self, actor_context_ref):
            assert actor_context_ref == "actor"

        def execute(self, call, context):
            return original

    policy = ApprovedLiveModelDataPolicy(
        approval_reference="approved-A-F",
        operator_notice_reference="synthetic-notice",
        supplier_policy_reference="synthetic-supplier",
        projection_version="school-facts-live-v1",
        allow_normal_names=True,
        chat_retention_hours=24,
    )
    gateway = wrap_live_gateway(Gateway(), policy)
    gateway.revalidate("actor")
    result = gateway.execute(None, None)
    assert result.fact_card is card and result.history_check is check
    assert "13900000002" not in json.dumps(result.model_content)
    assert "123456789012345678" not in json.dumps(result.history_content)
    loaded = result.continuation.load()
    assert loaded.fact_card is card and loaded.history_check is check
    assert "2号床" not in json.dumps(loaded.model_content, ensure_ascii=False)


def test_withheld_field_state_is_safe_on_actual_tool_wire_but_raw_private_field_is_not():
    projected = project_live_material(
        {"status": "known", "name": "王老师", "private_phone": "13900000011", "bed_name": "床A"}
    )
    guard_live_wire(
        json.dumps({"messages": [{"role": "tool", "content": json.dumps(projected)}]}).encode()
    )
    with pytest.raises(LiveModelDataPolicyError):
        guard_live_wire(
            json.dumps(
                {
                    "messages": [
                        {
                            "role": "tool",
                            "content": json.dumps(
                                {"field_states": {"private_phone": "13900000011"}}
                            ),
                        }
                    ]
                }
            ).encode()
        )


@pytest.mark.parametrize(
    "message",
    [
        {"role": "user", "content": "我的私人电话13900000003"},
        {"role": "tool", "content": json.dumps({"private_phone": "13900000003"})},
        {"role": "assistant", "reasoning_content": "证件号123456789012345678"},
        {
            "role": "assistant",
            "tool_calls": [
                {"function": {"arguments": json.dumps({"id_number": "123456789012345678"})}}
            ],
        },
    ],
)
def test_final_wire_guard_blocks_forbidden_material_in_every_role(message):
    with pytest.raises(LiveModelDataPolicyError) as raised:
        guard_live_wire(json.dumps({"messages": [message]}).encode())
    assert raised.value.reason == "data_policy"


def test_final_wire_guard_blocks_known_secret_and_allows_safe_task_values():
    with pytest.raises(LiveModelDataPolicyError):
        guard_live_wire(
            b'{"messages":[{"role":"user","content":"credential-sentinel"}]}',
            known_credentials=("credential-sentinel",),
        )
    guard_live_wire(
        json.dumps({"messages": [{"role": "tool", "content": "王老师教七年级一班数学"}]}).encode()
    )


def test_final_wire_guard_checks_payload_values_outside_messages_without_rejecting_schema_keys():
    tool_schema = {
        "type": "function",
        "function": {
            "name": "look_up_contact",
            "description": "查询已授权的学校联系人",
            "parameters": {
                "type": "object",
                "properties": {
                    "private_phone": {"type": "string", "description": "私人电话字段不可公开"}
                },
            },
        },
    }
    payload = {
        "messages": [{"role": "user", "content": "查询学校联系方式"}],
        "tools": [tool_schema],
    }
    guard_live_wire(json.dumps(payload).encode(), known_credentials=("credential-sentinel",))

    payload["tools"][0]["function"]["description"] = "credential-sentinel"
    with pytest.raises(LiveModelDataPolicyError):
        guard_live_wire(json.dumps(payload).encode(), known_credentials=("credential-sentinel",))

    payload["tools"][0]["function"]["description"] = "-----BEGIN PRIVATE KEY-----"
    with pytest.raises(LiveModelDataPolicyError):
        guard_live_wire(json.dumps(payload).encode())


def test_data_policy_failure_has_safe_public_category():
    failure = AssistantServiceError("private implementation detail", reason="data_policy")
    payload = _service_failure_payload(failure)
    assert payload["error_code"] == "assistant.data_policy"
    assert "private implementation detail" not in payload["text"]


def test_live_integration_synthetic_purpose_uses_original_engineering_ledger(monkeypatch, tmp_path):
    approval = SimpleNamespace(model="deepseek-flash")
    model = SimpleNamespace(
        model="deepseek-flash",
        current_approval=approval,
        thinking="enabled",
        reasoning_effort="high",
        stage_budget=object(),
        state_directory=tmp_path,
        budget=object(),
        max_input_tokens=4096,
        max_output_tokens=128,
    )
    config = SimpleNamespace(
        purpose="school-facts-live-integration-synthetic", directory=object(), model=model
    )
    make_engineering_permit = MagicMock(return_value=object())
    make_live_permit = MagicMock(side_effect=AssertionError("live ledger must not be used"))
    monkeypatch.setattr(pydanticai_assembly, "load_product_model_api_key", lambda _: object())
    monkeypatch.setattr(
        pydanticai_assembly, "SqliteCurrentModelCallPermit", make_engineering_permit
    )
    monkeypatch.setattr(pydanticai_assembly, "build_live_model_call_permit", make_live_permit)
    pydanticai_assembly.build_pydanticai_loop(
        configuration=config,
        gateway_factory=lambda _ref: None,
        control_service=MagicMock(),
        instruction="synthetic",
    )
    make_engineering_permit.assert_called_once()
    assert make_engineering_permit.call_args.kwargs["path"] == tmp_path / "model-budget.sqlite3"
    make_live_permit.assert_not_called()


def test_live_window_rechecked_after_reservation_before_http_send():
    clock = [29]
    sent = []

    class Reservation:
        def mark_transmission_started(self):
            clock[0] = 31

        def unknown(self):
            pass

    class Permit:
        def reserve(self, *_args):
            assert clock[0] == 29
            return Reservation()

    class Model:
        def require_current_window(self):
            if clock[0] >= 30:
                raise AssistantServiceError("outside approved window", reason="budget")

    transport = _GuardedTransport(
        httpx2.MockTransport(lambda _request: (sent.append(True), httpx2.Response(200))[1]),
        Permit(),
        pydanticai_assembly._live_transmission_check(lambda: None, Model()),
        4096,
        128,
        ContextVar("failure", default=None),
    )
    request = httpx2.Request(
        "POST",
        "https://api.deepseek.com/chat/completions",
        json={
            "model": "deepseek-flash",
            "thinking": {"type": "enabled"},
            "reasoning_effort": "high",
            "max_tokens": 128,
            "stream": False,
            "parallel_tool_calls": False,
            "messages": [{"role": "user", "content": "safe"}],
        },
    )
    with pytest.raises(AssistantServiceError) as raised:
        asyncio.run(transport.handle_async_request(request))
    assert raised.value.reason == "budget"
    assert sent == []


def test_wire_guard_runs_before_budget_reservation():
    class Permit:
        def reserve(self, *_args):
            raise AssertionError("budget reservation must not happen")

    transport = _GuardedTransport(
        httpx2.MockTransport(lambda _request: httpx2.Response(200)),
        Permit(),
        lambda: None,
        4096,
        128,
        ContextVar("failure", default=None),
        wire_guard=lambda _body: (_ for _ in ()).throw(LiveModelDataPolicyError()),
    )
    body = {"model": "deepseek-flash", "messages": [{"role": "user", "content": "private"}]}
    request = httpx2.Request("POST", "https://api.deepseek.com/chat/completions", json=body)
    with pytest.raises(LiveModelDataPolicyError):
        asyncio.run(transport.handle_async_request(request))
