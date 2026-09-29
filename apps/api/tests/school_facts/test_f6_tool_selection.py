"""Scripted owner gateway tests; no model, database, services or paid calls."""

import json
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.composition import school_fact_residence_gateway as residence_contract
from app.composition.school_fact_residence_gateway import (
    FAMILY_NAME,
    MODULE,
    RESIDENCE_INSTRUCTION,
    FamilyResidenceToolGateway,
    ResidenceQueryInput,
    ResidenceToolGateway,
)
from app.domains.students.residence_read import ResidenceQueryResult
from app.platform.assistant.catalog import StaticAssistantCatalog
from app.platform.assistant.controls import AssistantControlService, ControlIssueContext
from app.platform.assistant.pydanticai_runner import CandidateToolRunner, _tool_schema_presentation
from app.platform.assistant.runtime import AssistantRuntime, HistoryCheck
from app.platform.assistant.tool_loop import ModelToolCall, ToolExecutionResult
from tests.assistant.test_control_service import _MemoryControlStore
from tests.test_f3_family_staff_gateway import Product, context, gateway

STUDENT = "f3:student:known-student"
ROOM = "f6:dorm_room:known-room"
BED = "f6:dorm_bed:known-bed"
OLD = "f6:student_bed_residence:known-old"
NEW = "f6create:student_bed_residence:reserved-new"
AT = "2026-09-29T08:00:00+08:00"


def owner(query=None, eligibility=None, product=None, describe=None):
    base = gateway()
    return ResidenceToolGateway(
        resolve_actor=base.resolve_actor,
        query=query
        or SimpleNamespace(query=lambda **_: pytest.fail("invalid selection reached reader")),
        product=product or base.product,
        describe_adapter=describe or SimpleNamespace(
            describe=lambda **_: {
                "fact_kind": "student_bed_residence",
                "fact_timezone_name": "Asia/Shanghai",
                "fields": [],
                "creation_choices": [{"creation_ref": NEW, "label": "获准新增"}],
            }
        ),
        eligibility_adapter=eligibility
        or SimpleNamespace(query=lambda **_: pytest.fail("invalid selection reached SSR")),
    )


def call(selection, *, action="query", ident="one"):
    return ModelToolCall(call_id=ident, name=MODULE + "." + action, arguments=selection)


def assert_no_direct_chat_confirmation(result: ToolExecutionResult) -> None:
    module = result.module_result
    assert module is not None
    service = AssistantControlService(
        catalog=StaticAssistantCatalog(()),
        store=_MemoryControlStore(),
        signing_keys={"test": b"t" * 32},
        active_key_id="test",
    )
    assert service.issue_from_result(
        context=ControlIssueContext(
            actor_context_ref="identity",
            conversation_ref="conversation",
            turn_ref="turn",
            run_ref="run",
            generation=1,
            module_ref=MODULE,
            module_state_ref=module.staged_state_ref,
            state_revision=module.state_revision,
            issuance_request_id=uuid4(),
        ),
        result=module,
    ) == ()


def test_owner_contract_explains_exact_names_intersection_and_single_preparation():
    properties = ResidenceQueryInput.model_json_schema()["properties"]
    name = properties["name"]["description"]
    assert all(word in name for word in ("学生姓名", "房间", "床位", "名称", "路径"))
    assert "交集" in properties["related_ref"]["description"]
    assert "describe" in RESIDENCE_INSTRUCTION and "直接调用" in RESIDENCE_INSTRUCTION
    assert "按能力标识检索" in RESIDENCE_INSTRUCTION
    assert "prepare" in RESIDENCE_INSTRUCTION and "重复" in RESIDENCE_INSTRUCTION
    assert "locate_change" in RESIDENCE_INSTRUCTION and "可选择" in RESIDENCE_INSTRUCTION
    assert "同一事实时点" in RESIDENCE_INSTRUCTION
    assert "在校关系" in RESIDENCE_INSTRUCTION and "不能推断走读" in RESIDENCE_INSTRUCTION


def test_change_contracts_keep_common_actions_eager_and_detailed_actions_discoverable():
    selected = {
        descriptor.name.rsplit(".", 1)[1]: descriptor
        for descriptor in ResidenceToolGateway.descriptors
        if descriptor.name.rsplit(".", 1)[1]
        in {"query", "describe", "locate_change", "prepare_transfer", "prepare"}
    }
    assert set(selected) == {"query", "describe", "locate_change", "prepare_transfer", "prepare"}
    assert {name for name, descriptor in selected.items() if not descriptor.load_on_demand} == {
        "query", "describe", "prepare_transfer"
    }
    assert {name for name, descriptor in selected.items() if descriptor.load_on_demand} == {
        "locate_change", "prepare"
    }
    assert all(descriptor.owner == MODULE for descriptor in selected.values())
    assert [selected[name].kind for name in (
        "query", "describe", "locate_change", "prepare_transfer", "prepare"
    )] == [
        "read", "read", "read", "prepare", "prepare"
    ]


def test_transfer_preparation_is_a_visible_domain_prepare_without_replacing_generic_prepare():
    selected = {item.name: item for item in ResidenceToolGateway.descriptors}
    transfer = selected[MODULE + ".prepare_transfer"]
    assert transfer.kind == "prepare" and transfer.owner == MODULE
    assert transfer.load_on_demand is False
    assert MODULE + ".prepare" in selected
    assert "prepare_transfer" in RESIDENCE_INSTRUCTION


@pytest.mark.parametrize("action", ["locate_change", "prepare"])
def test_detailed_residence_actions_remain_discoverable_and_callable_by_exact_capability(action):
    from tests.assistant.test_pydanticai_tool_loading import (
        _FakePermit,
        _run_turn,
        _wire_factory,
        _wire_response,
    )

    descriptors = ResidenceToolGateway.descriptors
    target = next(
        descriptor for descriptor in descriptors if descriptor.name == MODULE + "." + action
    )
    alias = f"tool_{descriptors.index(target)}"
    assert target.load_on_demand
    calls, sends = [], []

    class Gateway:
        descriptors = ResidenceToolGateway.descriptors

        def revalidate(self, _):
            pass

        def execute(self, candidate, _):
            calls.append(candidate.name)
            return ToolExecutionResult(
                model_content={"status": "synthetic_lookup"},
                history_check=HistoryCheck(lambda: True),
            )

    def handler(request):
        tools = {
            item["function"]["name"]: item["function"]
            for item in json.loads(request.content)["tools"]
        }
        sends.append(len(request.content))
        if len(sends) == 1:
            assert alias not in tools
            assert f"{alias}: {target.description}" in tools["search_tools"]["description"]
            assert target.name in tools["search_tools"]["description"]
            return _wire_response(name="search_tools", arguments={"queries": [alias]})
        assert tools[alias]["parameters"] == _tool_schema_presentation(target.parameters)
        if len(sends) == 2:
            return _wire_response(name=alias, call_id="specific-read")
        return _wire_response()

    runtime = AssistantRuntime()
    conversation = runtime.create_conversation("actor", uuid4())
    _run_turn(
        CandidateToolRunner(
            model_factory=_wire_factory(_FakePermit([]), handler),
            gateway_factory=lambda _: Gateway(),
            instruction=RESIDENCE_INSTRUCTION,
        ),
        runtime,
        conversation,
    )
    assert calls == [target.name]
    assert len(sends) == 3


def test_guardian_residence_instruction_matches_only_the_guardian_read_tool():
    assert {item.name for item in FamilyResidenceToolGateway.descriptors} == {FAMILY_NAME}
    instruction = getattr(residence_contract, "FAMILY_RESIDENCE_INSTRUCTION", "")
    assert FAMILY_NAME in instruction
    assert all(name not in instruction for name in ("locate_change", "describe", "prepare"))
    assert "在校关系" in instruction


@pytest.mark.parametrize(
    "selection",
    [
        {"fact_kind": "dorm_bed", "query_text": "PRIVATE_INPUT"},
        {"fact_kind": "dorm_bed", "student_number": "PRIVATE_INPUT"},
        {"fact_kind": "dorm_room", "related_field": "student_ref"},
        {"fact_kind": "student_school_relationship", "related_field": "student_ref"},
        {"fact_kind": "student_school_relationship", "name": "PRIVATE_INPUT", "reference": OLD},
        {"fact_kind": "student_school_relationship", "related_ref": "f2:location:PRIVATE_INPUT"},
        {"fact_kind": "fact_quality", "name": "PRIVATE_INPUT"},
        {"fact_kind": "dorm_bed", "as_of": "2026-09-29T08:00:00"},
    ],
)
def test_invalid_selection_has_fixed_actionable_contract_without_echo_or_read(selection):
    result = owner().execute(call(selection), context())
    content = result.model_content
    assert content["status"] == "unsupported_selection"
    assert content["code"] == "invalid_residence_selection"
    assert content["allowed_fields"] and content["selection_help"]
    assert "PRIVATE_INPUT" not in json.dumps(content)
    assert OLD not in json.dumps(content)
    assert result.history_check is None and result.fact_card is None
    assert result.continuation is None


def test_dependency_value_error_uses_fixed_help_and_permission_stays_denied():
    def fail(**_):
        raise ValueError("PRIVATE_EXCEPTION")

    result = owner(eligibility=SimpleNamespace(query=fail)).execute(
        call({"fact_kind": "student_school_relationship", "student_number": "PRIVATE_INPUT"}),
        context(),
    )
    assert result.model_content["selection_help"]
    assert "PRIVATE" not in str(result.model_content)

    def deny(**_):
        raise PermissionError("PRIVATE_PERMISSION")

    result = owner(eligibility=SimpleNamespace(query=deny)).execute(
        call({"fact_kind": "student_school_relationship", "student_number": "PRIVATE_INPUT"}),
        context(),
    )
    assert result.model_content == {"status": "denied"}


def test_six_owner_calls_reuse_refs_prepare_one_batch_and_keep_original_receipt_guard():
    class PreparedProduct(Product):
        invoked = False
        allowed = True
        prepares = 0

        def prepare(self, **kwargs):
            self.prepares += 1
            payload = kwargs["payload"]
            assert payload.target_ref == OLD and payload.operation == "end"
            assert len(payload.additional_changes) == 1
            new = payload.additional_changes[0]
            assert new.creation_ref == NEW and new.fields == {
                "student_ref": STUDENT,
                "bed_ref": BED,
            }
            return SimpleNamespace(
                proposal_ref="sf6plan_" + "a" * 32,
                model_dump=lambda **_: {
                    "proposal_ref": "sf6plan_" + "a" * 32,
                    "summary": {
                        "fact_kind": "student_bed_residence",
                        "label": "完整换床差异",
                        "reference": OLD,
                        "before": {"bed_ref": "f6:dorm_bed:old-bed"},
                        "after": {"valid_to": AT},
                    },
                    "expires_at": "2026-09-29T00:10:00Z",
                },
            )

        def require_invoked(self, **_):
            if not self.invoked or not self.allowed:
                raise PermissionError("Original operation not available")

        def receipt(self, **kwargs):
            from app.composition.school_fact_product_contacts import ContactReceipt

            self.require_invoked(**kwargs)
            return ContactReceipt(
                reference=kwargs["proposal_ref"],
                status="succeeded",
                message="完成",
                changed_count=2,
                target_version=2,
            )

    def query(**args):
        assert args["as_of"].isoformat() == AT
        kind = args["fact_kind"]
        if kind == "student_bed_residence":
            assert args["related_ref"] == STUDENT
            row = {
                "reference": OLD,
                "student_ref": STUDENT,
                "bed_ref": "f6:dorm_bed:old-bed",
                "bed_name": "2号床",
                "room_name": "301",
                "building_name": "住宿楼",
            }
        elif kind == "dorm_room":
            assert args["name"] == "302"
            row = {"reference": ROOM, "room_name": "302"}
        else:
            assert kind == "dorm_bed" and args["name"] == "1号床" and args["related_ref"] == ROOM
            row = {"reference": BED, "display_name": "1号床", "room_name": "302"}
        return ResidenceQueryResult(
            {"status": "available", "records": [row]}, SimpleNamespace(valid=lambda _: True)
        )

    eligibility = SimpleNamespace(
        query=lambda **_: {
            "status": "known",
            "records": [
                {
                    "fields": {"student_ref": STUDENT},
                    "reference": "f3:student_school_relationship:known",
                }
            ],
        }
    )
    product = PreparedProduct()
    turn = owner(
        query=SimpleNamespace(query=query), eligibility=eligibility, product=product
    ).for_turn("identity")
    ctx = context()
    described = turn.execute(
        call({"fact_kind": "student_bed_residence"}, action="describe", ident="describe"), ctx
    )
    assert described.model_content["creation_choices"][0]["creation_ref"] == NEW
    selected = turn.execute(
        call(
            {"fact_kind": "student_school_relationship", "student_number": "A001", "as_of": AT},
            ident="student",
        ),
        ctx,
    )
    assert selected.model_content["records"][0]["fields"]["student_ref"] == STUDENT
    for ident, selection, expected in (
        ("old", {"fact_kind": "student_bed_residence", "related_ref": STUDENT}, OLD),
        ("room", {"fact_kind": "dorm_room", "name": "302"}, ROOM),
        ("bed", {"fact_kind": "dorm_bed", "name": "1号床", "related_ref": ROOM}, BED),
    ):
        result = turn.execute(call({**selection, "as_of": AT}, ident=ident), ctx)
        assert result.model_content["tool_context"]["targets"][0]["reference"] == expected
    prepared = turn.execute(
        call(
            {
                "fact_kind": "student_bed_residence",
                "target_ref": OLD,
                "operation": "end",
                "fields": {},
                "effective_at": AT,
                "reason": "明确换床",
                "additional_changes": [
                    {
                        "fact_kind": "student_bed_residence",
                        "creation_ref": NEW,
                        "operation": "upsert",
                        "effective_at": AT,
                        "reason": "明确换床",
                        "fields": {"student_ref": STUDENT, "bed_ref": BED},
                    }
                ],
            },
            action="prepare",
            ident="prepare",
        ),
        ctx,
    )
    assert product.prepares == 1 and not product.invoked
    assert prepared.module_result is not None
    assert prepared.module_result.action_offer is None
    assert_no_direct_chat_confirmation(prepared)
    assert prepared.module_result.display_payload == prepared.fact_card
    assert prepared.continuation is not None
    assert prepared.continuation.reference == prepared.fact_card["proposal_ref"]
    assert prepared.history_check is not None
    assert prepared.model_content["tool_context"]["action_owner"] == MODULE
    assert "proposal_ref" not in prepared.model_content["tool_context"]
    assert "sf6plan_" not in str(prepared.model_content)
    with pytest.raises(PermissionError):
        prepared.continuation.load()
    product.invoked = True  # Stub models a separate already-confirmed result; no write here.
    receipt = prepared.continuation.load()
    assert receipt.fact_card["changed_count"] == 2 and receipt.history_check.valid()
    product.allowed = False
    assert not receipt.history_check.valid()


def test_bounded_lodging_locator_returns_only_authorized_change_coordinates():
    """One domain read joins exact selectors; the model still calls prepare itself."""
    building = "f6:dormitory_building_profile:known-building"
    calls = []
    permitted = {"current": True}

    def query(**selection):
        calls.append(selection)
        assert selection["as_of"].isoformat() == AT
        match selection["fact_kind"]:
            case "student_bed_residence":
                assert selection["related_ref"] == STUDENT
                row = {
                    "reference": OLD,
                    "student_ref": STUDENT,
                    "bed_ref": "f6:dorm_bed:known-old-bed",
                    "room_name": "301室",
                    "bed_name": "2号床",
                    "building_name": "甲住宿楼",
                }
            case "dormitory_building_profile":
                assert selection["name"] == "甲住宿楼"
                row = {"reference": building, "display_name": "甲住宿楼"}
            case "dorm_room":
                assert selection["related_ref"] == building
                if selection["name"] == "301室":
                    row = {"reference": "f6:dorm_room:known-old-room", "room_name": "301室"}
                else:
                    assert selection["name"] == "302室"
                    row = {"reference": ROOM, "room_name": "302室"}
            case "dorm_bed":
                if selection.get("reference"):
                    assert selection["reference"] == "f6:dorm_bed:known-old-bed"
                    assert selection["related_ref"] == "f6:dorm_room:known-old-room"
                    row = {"reference": selection["reference"], "display_name": "2号床"}
                else:
                    assert selection["name"] == "1号床" and selection["related_ref"] == ROOM
                    row = {"reference": BED, "display_name": "1号床"}
            case other:
                pytest.fail(f"Unexpected kind {other}")
        return ResidenceQueryResult(
            {
                "status": "available",
                "records": [row],
                "has_more": False,
                "fact_timezone_name": "Asia/Shanghai",
                "time_basis": {
                    "kind": "school_timezone",
                    "status": "available",
                    "source_version": 3,
                    "valid_from": "2026-09-01T00:00:00+08:00",
                    "valid_to": None,
                },
                "limitations": ["formal_arrangement_not_actual_presence"]
                if selection["fact_kind"] == "student_bed_residence"
                else [],
            },
            SimpleNamespace(valid=lambda _: permitted["current"]),
        )

    eligibility = SimpleNamespace(
        query=lambda **selection: {
            "status": "known",
            "records": [{"fields": {"student_ref": STUDENT}}],
            "truncated": False,
            "fact_timezone_name": "Asia/Shanghai",
        }
    )
    turn = owner(query=SimpleNamespace(query=query), eligibility=eligibility).for_turn("identity")
    result = turn.execute(
        call(
            {
                "student_number": "F3-A001",
                "as_of": AT,
                "to_room_name": "302室",
                "to_bed_name": "1号床",
            },
            action="locate_change",
        ),
        context(),
    )
    assert result.model_content["status"] == "located"
    assert result.model_content["tool_context"] == {
        "student_ref": STUDENT,
        "original_residence_ref": OLD,
        "original_building_ref": building,
        "original_room_ref": "f6:dorm_room:known-old-room",
        "original_bed_ref": "f6:dorm_bed:known-old-bed",
        "target_bed_ref": BED,
    }
    assert result.model_content["fact_timezone_name"] == "Asia/Shanghai"
    assert result.model_content["time_basis"]["source_version"] == 3
    assert "Asia/Shanghai" in result.model_content["time_labels"]["as_of"]
    assert "formal_arrangement_not_actual_presence" in result.model_content["limitations"]
    assert result.fact_card is None and result.module_result is None
    assert [item["fact_kind"] for item in calls] == [
        "student_bed_residence",
        "dormitory_building_profile",
        "dorm_room",
        "dorm_bed",
        "dorm_room",
        "dorm_bed",
    ]
    assert result.history_check.valid()
    permitted["current"] = False
    assert not result.history_check.valid()


@pytest.mark.parametrize(
    ("quality", "expected"),
    [
        ({"dispute_status": "open"}, "conflict"),
        ({"source_status": "stale"}, "partial"),
    ],
)
def test_lodging_locator_does_not_label_weak_old_evidence_as_located(quality, expected):
    rows = {
        "student_bed_residence": {
            "reference": OLD,
            "bed_ref": "f6:dorm_bed:old-bed",
            "building_name": "甲住宿楼",
            "room_name": "301室",
            "bed_name": "2号床",
            "quality": [quality],
        },
        "dormitory_building_profile": {
            "reference": "f6:dormitory_building_profile:building",
            "display_name": "甲住宿楼",
        },
        "dorm_room": {"reference": ROOM, "room_name": "301室"},
        "dorm_bed": {"reference": BED, "display_name": "2号床"},
    }

    def query(**selection):
        return ResidenceQueryResult(
            {
                "status": "available",
                "records": [rows[selection["fact_kind"]]],
                "has_more": False,
                "fact_timezone_name": "Asia/Shanghai",
            },
            SimpleNamespace(valid=lambda _: True),
        )

    eligibility = SimpleNamespace(
        query=lambda **_: {
            "status": "known",
            "records": [{"fields": {"student_ref": STUDENT}}],
            "truncated": False,
        }
    )
    turn = owner(query=SimpleNamespace(query=query), eligibility=eligibility).for_turn("identity")
    result = turn.execute(
        call(
            {
                "student_number": "F3-A001",
                "as_of": AT,
                "to_room_name": "302室",
                "to_bed_name": "1号床",
            },
            action="locate_change",
        ),
        context(),
    )
    assert result.model_content["status"] == expected
    assert result.model_content["stage"] == "original_residence"
    assert result.model_content["limitations"]
    assert "target_bed_ref" not in str(result.model_content)


def test_lodging_locator_never_picks_first_ambiguous_bed_or_mismatched_old_bed():
    building = "f6:dormitory_building_profile:known-building"
    old_bed_name = {"value": "2号床"}

    def query(**selection):
        kind = selection["fact_kind"]
        rows = {
            "student_bed_residence": [
                {
                    "reference": OLD,
                    "bed_ref": "f6:dorm_bed:known-old-bed",
                    "room_name": "301室",
                    "bed_name": old_bed_name["value"],
                    "building_name": "甲住宿楼",
                }
            ],
            "dormitory_building_profile": [{"reference": building, "display_name": "甲住宿楼"}],
            "dorm_room": [{"reference": ROOM, "room_name": selection["name"]}],
            "dorm_bed": [
                {"reference": selection["reference"], "display_name": "2号床"}
            ] if selection.get("reference") else [
                {"reference": BED, "display_name": "1号床"},
                {"reference": "f6:dorm_bed:second-bed", "display_name": "1号床"},
            ],
        }[kind]
        return ResidenceQueryResult(
            {"status": "available", "records": rows, "has_more": False},
            SimpleNamespace(valid=lambda _: True),
        )

    eligibility = SimpleNamespace(
        query=lambda **_: {
            "status": "known",
            "records": [{"fields": {"student_ref": STUDENT}}],
            "truncated": False,
        }
    )
    turn = owner(query=SimpleNamespace(query=query), eligibility=eligibility).for_turn("identity")
    selection = {
        "student_number": "F3-A001",
        "as_of": AT,
        "to_room_name": "302室",
        "to_bed_name": "1号床",
    }
    ambiguous = turn.execute(call(selection, action="locate_change", ident="ambiguous"), context())
    assert ambiguous.model_content["status"] == "ambiguous"
    assert "target_bed_ref" not in str(ambiguous.model_content)

    old_bed_name["value"] = "3号床"
    wrong_old = turn.execute(call(selection, action="locate_change", ident="wrong"), context())
    assert wrong_old.model_content["status"] == "object_mismatch"
    assert "target_bed_ref" not in str(wrong_old.model_content)


def test_lodging_locator_does_not_assume_same_named_old_room_is_same_object():
    building = "f6:dormitory_building_profile:known-building"
    calls = []

    def query(**selection):
        calls.append(selection)
        kind = selection["fact_kind"]
        if kind == "student_bed_residence":
            row = {
                "reference": OLD,
                "bed_ref": "f6:dorm_bed:known-old-bed",
                "room_name": "301室",
                "bed_name": "2号床",
                "building_name": "甲住宿楼",
            }
        elif kind == "dormitory_building_profile":
            row = {"reference": building, "display_name": "甲住宿楼"}
        elif kind == "dorm_room":
            assert selection["related_ref"] == building
            row = {"reference": "f6:dorm_room:different-room", "room_name": "301室"}
        else:
            assert kind == "dorm_bed"
            # The old bed is not related to the room found under this building.
            assert selection["related_ref"] == "f6:dorm_room:different-room"
            return ResidenceQueryResult(
                {
                    "status": "no_current_records",
                    "records": [],
                    "has_more": False,
                    "coverage": {"status": "authorized_selection_only"},
                    "limitations": ["absence_is_not_proof_of_no_lodging"],
                    "fact_timezone_name": "Asia/Shanghai",
                },
                SimpleNamespace(valid=lambda _: True),
            )
        return ResidenceQueryResult(
            {
                "status": "available",
                "records": [row],
                "has_more": False,
                "fact_timezone_name": "Asia/Shanghai",
            },
            SimpleNamespace(valid=lambda _: True),
        )

    eligibility = SimpleNamespace(
        query=lambda **_: {
            "status": "known",
            "records": [{"fields": {"student_ref": STUDENT}}],
            "truncated": False,
        }
    )
    turn = owner(query=SimpleNamespace(query=query), eligibility=eligibility).for_turn("identity")
    result = turn.execute(
        call(
            {
                "student_number": "F3-A001",
                "as_of": AT,
                "to_room_name": "302室",
                "to_bed_name": "1号床",
            },
            action="locate_change",
        ),
        context(),
    )
    assert result.model_content["status"] == "no_current_records"
    assert result.model_content["stage"] == "original_bed"
    assert result.model_content["as_of"] == AT
    assert result.model_content["coverage"] == {"status": "authorized_selection_only"}
    assert result.model_content["limitations"] == ["absence_is_not_proof_of_no_lodging"]
    assert result.model_content["fact_timezone_name"] == "Asia/Shanghai"
    assert "target_bed_ref" not in str(result.model_content)
    assert len(calls) == 4  # No target search after the old linkage fails.


@pytest.mark.parametrize("bed_name", [None, "missing"])
def test_lodging_locator_reports_missing_projected_field_without_claiming_mismatch(bed_name):
    def query(**selection):
        assert selection["fact_kind"] == "student_bed_residence"
        return ResidenceQueryResult(
            {
                "status": "available",
                "records": [{
                    "reference": OLD, "room_name": "301室", "building_name": "甲住宿楼",
                    **({} if bed_name == "missing" else {"bed_name": bed_name}),
                }],
                "has_more": False,
                "fact_timezone_name": "Asia/Shanghai",
            },
            SimpleNamespace(valid=lambda _: True),
        )

    eligibility = SimpleNamespace(
        query=lambda **_: {
            "status": "known",
            "records": [{"fields": {"student_ref": STUDENT}}],
            "truncated": False,
        }
    )
    turn = owner(query=SimpleNamespace(query=query), eligibility=eligibility).for_turn("identity")
    result = turn.execute(
        call(
            {
                "student_number": "F3-A001",
                "as_of": AT,
                "to_room_name": "302室",
                "to_bed_name": "1号床",
            },
            action="locate_change",
        ),
        context(),
    )
    assert result.model_content["status"] == "incomplete"
    assert result.model_content["stage"] == "original_residence"
    assert result.model_content["fact_timezone_name"] == "Asia/Shanghai"
    assert "target_bed_ref" not in str(result.model_content)


def test_lodging_locator_denial_does_not_expose_stage_or_private_selector():
    def deny(**_):
        raise PermissionError("PRIVATE_PERMISSION_REASON")

    turn = owner(eligibility=SimpleNamespace(query=deny)).for_turn("identity")
    result = turn.execute(
        call(
            {
                "student_number": "PRIVATE_STUDENT_NUMBER",
                "as_of": AT,
                "to_room_name": "302室",
                "to_bed_name": "1号床",
            },
            action="locate_change",
        ),
        context(),
    )
    assert result.model_content == {"status": "denied"}
    assert result.history_execution_note == "read_returned_no_values"


def test_lodging_locator_uses_formal_old_names_and_never_guesses_spoken_target_alias():
    building = "f6:dormitory_building_profile:formal-building"
    old_room = "f6:dorm_room:formal-old-room"

    def query(**selection):
        kind = selection["fact_kind"]
        if kind == "student_bed_residence":
            rows = [{
                "reference": OLD, "bed_ref": "f6:dorm_bed:old-bed",
                "building_name": "合成甲住宿楼", "room_name": "301", "bed_name": "2号床",
            }]
        elif kind == "dormitory_building_profile":
            assert selection["name"] == "合成甲住宿楼"
            rows = [{"reference": building, "display_name": "合成甲住宿楼"}]
        elif kind == "dorm_room" and selection.get("name") == "301":
            assert selection["related_ref"] == building
            rows = [{"reference": old_room, "room_name": "301"}]
        elif kind == "dorm_bed":
            assert selection["reference"] == "f6:dorm_bed:old-bed"
            assert selection["related_ref"] == old_room
            rows = [{"reference": selection["reference"], "display_name": "2号床"}]
        elif kind == "dorm_room":
            assert selection["related_ref"] == building
            rows = (
                [] if selection.get("name") == "302室" else [
                    {"reference": old_room, "room_name": "301"},
                    {"reference": ROOM, "room_name": "302"},
                ]
            )
        else:
            pytest.fail(f"Unexpected kind {kind}")
        return ResidenceQueryResult(
            {
                "status": "available" if rows else "no_current_records",
                "records": rows,
                "has_more": False,
                "fact_timezone_name": "Asia/Shanghai",
                "coverage": {"status": "authorized_selection_only"},
            },
            SimpleNamespace(valid=lambda _: True),
        )

    eligibility = SimpleNamespace(
        query=lambda **_: {
            "status": "known", "records": [{"fields": {"student_ref": STUDENT}}],
            "truncated": False, "fact_timezone_name": "Asia/Shanghai",
        }
    )
    turn = owner(query=SimpleNamespace(query=query), eligibility=eligibility).for_turn("identity")
    result = turn.execute(
        call(
            {
                "student_number": "F3-A001",
                "as_of": AT,
                "to_room_name": "302室",
                "to_bed_name": "1号床",
            },
            action="locate_change",
        ),
        context(),
    )
    assert result.model_content["status"] == "target_room_candidates"
    assert result.model_content["original"] == {
        "building_name": "合成甲住宿楼", "room_name": "301", "bed_name": "2号床"
    }
    assert [item["room_name"] for item in result.model_content["candidates"]] == ["301", "302"]
    assert "target_bed_ref" not in str(result.model_content)
    assert result.module_result is None and result.history_check.valid()


def _locator_with_quality(
    *, weak_kind=None, quality=None, quality_name=None, clock=None,
    product=None, describe=None, read_allowed=None,
):
    building = "f6:dormitory_building_profile:known-building"
    old_room = "f6:dorm_room:known-old-room"

    def query(**selection):
        kind = selection["fact_kind"]
        if kind == "student_bed_residence":
            rows = [{
                "reference": OLD,
                "bed_ref": "f6:dorm_bed:known-old-bed",
                "building_name": "甲住宿楼",
                "room_name": "301室",
                "bed_name": "2号床",
            }]
        elif kind == "dormitory_building_profile":
            rows = [{"reference": building, "display_name": "甲住宿楼"}]
        elif kind == "dorm_room":
            if selection.get("name") == "301室":
                rows = [{"reference": old_room, "room_name": "301室"}]
            elif selection.get("name") == "302室":
                rows = [{"reference": ROOM, "room_name": "302室"}]
            else:
                rows = [
                    {"reference": old_room, "room_name": "301室"},
                    {"reference": ROOM, "room_name": "302室"},
                ]
        elif selection.get("reference"):
            rows = [{"reference": selection["reference"], "display_name": "2号床"}]
        elif selection.get("name") == "1号床":
            rows = [{"reference": BED, "display_name": "1号床"}]
        else:
            rows = [
                {"reference": BED, "display_name": "1号床"},
                {"reference": "f6:dorm_bed:second-bed", "display_name": "2号床"},
            ]
        if (
            quality is not None
            and kind == weak_kind
            and selection.get("name") == quality_name
            and selection.get("reference") is None
        ):
            rows[-1]["quality"] = [quality]
        return ResidenceQueryResult(
            {
                "status": "available",
                "records": rows,
                "has_more": False,
                "fact_timezone_name": "Asia/Shanghai",
                "time_basis": {
                    "kind": "school_timezone",
                    "status": "available",
                    "source_version": 3,
                },
            },
            SimpleNamespace(valid=lambda _: read_allowed() if read_allowed else True),
        )

    eligibility = SimpleNamespace(
        query=lambda **_: {
            "status": "known",
            "records": [{"fields": {"student_ref": STUDENT}}],
            "truncated": False,
        }
    )
    residence_query = SimpleNamespace(query=query)
    if clock is not None:
        residence_query.repository = SimpleNamespace(clock=clock)
    return owner(
        query=residence_query, eligibility=eligibility, product=product, describe=describe
    ).for_turn("identity")


def test_transfer_preparation_uses_owner_location_and_one_existing_batch_preview():
    class PreviewOnlyProduct(Product):
        def __init__(self):
            self.prepares = []

        def prepare(self, **kwargs):
            self.prepares.append(kwargs)
            return super().prepare(**kwargs)

        def proposal(self, **_):
            return Product.prepare(self)

        def confirm(self, **_):
            pytest.fail("A preview must not execute a formal change")

    product = PreviewOnlyProduct()
    turn = _locator_with_quality(product=product)
    result = turn.execute(
        call(
            {
                "student_number": "F3-A001",
                "effective_at": AT,
                "from_building_name": "甲住宿楼",
                "from_room_name": "301室",
                "from_bed_name": "2号床",
                "to_room_name": "302室",
                "to_bed_name": "1号床",
            },
            action="prepare_transfer",
        ),
        context(),
    )
    assert len(product.prepares) == 1
    payload = product.prepares[0]["payload"]
    assert payload.fact_kind == "student_bed_residence"
    assert payload.target_ref == OLD and payload.operation == "end"
    assert payload.effective_at.isoformat() == AT and payload.fields == {}
    assert len(payload.additional_changes) == 1
    created = payload.additional_changes[0]
    assert created.creation_ref == NEW and created.operation == "upsert"
    assert created.effective_at.isoformat() == AT
    assert created.fields == {"student_ref": STUDENT, "bed_ref": BED, "valid_to": None}
    assert payload.reason == created.reason == "按本次明确请求调整床位安排"
    assert result.module_result is not None and result.continuation is not None
    assert result.module_result.action_offer is None
    assert_no_direct_chat_confirmation(result)
    assert result.module_result.display_payload == result.fact_card
    assert result.continuation.reference == result.fact_card["proposal_ref"]
    assert result.fact_card is not None and result.history_check is not None
    assert result.history_check.valid()


def test_transfer_preparation_pending_evidence_fails_closed_after_read_revocation():
    allowed = {"current": True}
    turn = _locator_with_quality(read_allowed=lambda: allowed["current"])
    result = turn.execute(
        call(
            {
                "student_number": "F3-A001",
                "effective_at": AT,
                "to_room_name": "302室",
                "to_bed_name": "1号床",
            },
            action="prepare_transfer",
        ),
        context(),
    )
    assert result.module_result is not None and result.history_check.valid()
    allowed["current"] = False
    assert not result.history_check.valid()


def _transfer_with_original_receipt(status="succeeded"):
    from app.composition.school_fact_product_contacts import ContactReceipt

    class ReceiptProduct(Product):
        invoked = False
        allowed = True
        receipt_reads = 0
        prepares = 0

        def prepare(self, **kwargs):
            self.prepares += 1
            return super().prepare(**kwargs)

        def proposal(self, **_):
            return Product.prepare(self)

        def require_invoked(self, **_):
            if not self.invoked or not self.allowed:
                raise PermissionError("Original operation unavailable")

        def receipt(self, **kwargs):
            self.require_invoked(**kwargs)
            self.receipt_reads += 1
            return ContactReceipt(
                reference=kwargs["proposal_ref"], status=status, message="原操作结果",
                changed_count=2 if status == "succeeded" else 0, target_version=44,
            )

        def confirm(self, **_):
            pytest.fail("Result continuation must not repeat the formal action")

    product = ReceiptProduct()
    preview_read = {"valid": True}
    turn = _locator_with_quality(
        product=product, read_allowed=lambda: preview_read["valid"]
    )
    prepared = turn.execute(
        call(
            {
                "student_number": "F3-A001", "effective_at": AT,
                "to_room_name": "302室", "to_bed_name": "1号床",
            },
            action="prepare_transfer",
        ),
        context(),
    )
    assert prepared.continuation is not None and product.prepares == 1
    return product, preview_read, prepared


def test_successful_transfer_continuation_keeps_current_and_effective_readback_targets():
    product, preview_read, prepared = _transfer_with_original_receipt()
    with pytest.raises(PermissionError):
        prepared.continuation.load()
    product.invoked = True  # Separate confirmation is simulated; no formal write.
    preview_read["valid"] = False  # A write invalidates its old preview, not user intent.
    assert not prepared.history_check.valid()
    result = prepared.continuation.load()
    assert result.fact_card["status"] == "succeeded"
    assert result.fact_card["changed_count"] == 2
    scope = result.model_content["readback_scope"]
    assert scope["effective_at"] == AT and scope["queried"] is False
    assert "不是当前安排或届时安排的查询结果" in scope["meaning"]
    assert "读取受限或失败" in scope["meaning"] and "不重做原操作" in scope["meaning"]
    hints = result.model_content["tool_context"]["read_after_confirmation"]
    assert hints == [
        {
            "scope": "current", "tool": MODULE + ".query",
            "arguments": {"fact_kind": "student_bed_residence", "related_ref": STUDENT},
        },
        {
            "scope": "effective_time", "tool": MODULE + ".query",
            "arguments": {
                "fact_kind": "student_bed_residence", "related_ref": STUDENT, "as_of": AT,
            },
        },
    ]
    for hint in hints:
        ResidenceQueryInput.model_validate_json(json.dumps(hint["arguments"]))
    assert result.model_content["tool_context"]["read_original_targets"]["tool"] == (
        MODULE + ".own_receipt"
    )
    assert "sf3plan_" not in str(result.model_content)
    assert "302室" not in str(result.model_content)  # No stale preview values masquerade as facts.
    assert product.prepares == 1 and result.history_check.valid()
    product.allowed = False
    assert not result.history_check.valid()  # The exact original receipt still controls release.


def test_transfer_continuation_does_not_cache_or_mutate_previous_readback_material():
    product, _, prepared = _transfer_with_original_receipt()
    product.invoked = True
    first = prepared.continuation.load()
    first.model_content["tool_context"]["read_after_confirmation"][0]["arguments"][
        "related_ref"
    ] = "another-child"
    second = prepared.continuation.load()
    assert second.model_content["tool_context"]["read_after_confirmation"][0]["arguments"][
        "related_ref"
    ] == STUDENT
    assert product.receipt_reads == 2 and product.prepares == 1


@pytest.mark.parametrize("status", ["unknown", "rejected"])
def test_unsuccessful_transfer_continuation_never_instructs_successful_readback(status):
    product, _, prepared = _transfer_with_original_receipt(status)
    product.invoked = True
    if status == "unknown":
        from app.platform.assistant.runtime import AssistantRuntimeError

        with pytest.raises(AssistantRuntimeError, match="not terminal"):
            prepared.continuation.load()
    else:
        result = prepared.continuation.load()
        assert result.fact_card["status"] == "rejected"
        assert "readback_scope" not in result.model_content
        assert "read_after_confirmation" not in result.model_content["tool_context"]
        assert result.history_check.valid()
    assert product.prepares == 1


def test_transfer_preparation_keeps_an_existing_prepare_history_dependency():
    extra = {"valid": True}
    turn = _locator_with_quality()
    underlying = turn.retained.execute

    def with_prepare_proof(call, context):
        result = underlying(call, context)
        if call.name == MODULE + ".prepare":
            natural = result.history_check
            assert natural is not None
            return replace(
                result,
                history_check=HistoryCheck(lambda: extra["valid"] and natural.valid()),
            )
        return result

    turn.retained.execute = with_prepare_proof
    result = turn.execute(
        call(
            {
                "student_number": "F3-A001", "effective_at": AT,
                "to_room_name": "302室", "to_bed_name": "1号床",
            },
            action="prepare_transfer",
        ),
        context(),
    )
    assert result.module_result is not None and result.history_check.valid()
    extra["valid"] = False
    assert not result.history_check.valid()


def test_transfer_preparation_naturally_invalidates_when_its_action_number_is_retired():
    turn = _locator_with_quality()
    result = turn.execute(
        call(
            {
                "student_number": "F3-A001", "effective_at": AT,
                "to_room_name": "302室", "to_bed_name": "1号床",
            },
            action="prepare_transfer",
        ),
        context(),
    )
    assert result.module_result is not None and result.history_check.valid()
    turn.owner._actions.clear()
    assert not result.history_check.valid()


def test_transfer_preparation_naturally_invalidates_when_original_proposal_changes():
    class MutableProposalProduct(Product):
        label = "学生甲"

        def proposal(self, **kwargs):
            original = super().proposal(**kwargs)

            def dump(**_):
                view = original.model_dump(mode="json")
                view["summary"]["label"] = self.label
                return view

            return SimpleNamespace(model_dump=dump)

    product = MutableProposalProduct()
    turn = _locator_with_quality(product=product)
    result = turn.execute(
        call(
            {
                "student_number": "F3-A001", "effective_at": AT,
                "to_room_name": "302室", "to_bed_name": "1号床",
            },
            action="prepare_transfer",
        ),
        context(),
    )
    assert result.module_result is not None and result.history_check.valid()
    product.label = "另一份预检"
    assert not result.history_check.valid()


def test_transfer_preparation_does_not_treat_failed_description_as_a_valid_creation_catalog():
    class PreviewOnlyProduct(Product):
        def prepare(self, **_):
            pytest.fail("A failed creation catalog cannot create a proposal")

    describe = SimpleNamespace(describe=lambda **_: {
        "status": "source_unavailable", "fact_kind": "student_bed_residence",
        "fact_timezone_name": "Asia/Shanghai",
        "creation_choices": [{"creation_ref": NEW, "label": "获准新增"}],
    })
    turn = _locator_with_quality(product=PreviewOnlyProduct(), describe=describe)
    result = turn.execute(
        call(
            {
                "student_number": "F3-A001", "effective_at": AT,
                "to_room_name": "302室", "to_bed_name": "1号床",
            },
            action="prepare_transfer",
        ),
        context(),
    )
    assert result.model_content["status"] == "source_unavailable"
    assert result.module_result is None


@pytest.mark.parametrize(
    "declared",
    [
        {"from_building_name": "另一栋楼"},
        {"from_room_name": "另一间房"},
        {"from_bed_name": "另一张床"},
    ],
)
def test_transfer_preparation_never_overrides_an_explicitly_different_old_arrangement(declared):
    class PreviewOnlyProduct(Product):
        def prepare(self, **_):
            pytest.fail("A mismatched old arrangement cannot be prepared")

    turn = _locator_with_quality(product=PreviewOnlyProduct())
    result = turn.execute(
        call(
            {
                "student_number": "F3-A001",
                "effective_at": AT,
                "to_room_name": "302室",
                "to_bed_name": "1号床",
                **declared,
            },
            action="prepare_transfer",
        ),
        context(),
    )
    assert result.model_content["status"] == "object_mismatch"
    assert result.model_content["stage"] == "original_residence"
    assert result.model_content["original"] == {
        "building_name": "甲住宿楼", "room_name": "301室", "bed_name": "2号床"
    }
    assert result.module_result is None and result.history_check is not None


@pytest.mark.parametrize(
    ("field", "kind", "supplied", "formal", "related"),
    [
        ("from_building_name", "dormitory_building_profile", "甲楼", "甲住宿楼", None),
        ("from_room_name", "dorm_room", "301", "301室",
         "f6:dormitory_building_profile:known-building"),
        ("from_bed_name", "dorm_bed", "原来第二床", "2号床", "f6:dorm_room:known-old-room"),
    ],
)
def test_transfer_unknown_old_wording_is_explicitly_bound_to_formal_preview(
    field, kind, supplied, formal, related,
):
    class PreviewOnlyProduct(Product):
        def __init__(self):
            self.prepares = []

        def prepare(self, **kwargs):
            self.prepares.append(kwargs)
            return super().prepare(**kwargs)

        def proposal(self, **_):
            return Product.prepare(self)

        def confirm(self, **_):
            pytest.fail("A spelling difference must never execute a change")

    product = PreviewOnlyProduct()
    turn = _locator_with_quality(product=product)
    underlying = turn.owner.residence_query.query
    checked = []
    allowed = {"value": True}

    def query(**selection):
        if selection.get("name") == supplied:
            checked.append(selection)
            assert selection["fact_kind"] == kind
            assert selection.get("related_ref") == related
            return ResidenceQueryResult(
                {"status": "no_current_records", "records": [], "has_more": False},
                SimpleNamespace(valid=lambda _: allowed["value"]),
            )
        return underlying(**selection)

    turn.owner.residence_query.query = query
    result = turn.execute(call({
        "student_number": "F3-A001", "effective_at": AT,
        "to_room_name": "302室", "to_bed_name": "1号床", field: supplied,
    }, action="prepare_transfer"), context())
    assert len(product.prepares) == 1
    payload = product.prepares[0]["payload"]
    assert payload.target_ref == OLD
    assert payload.additional_changes[0].fields["bed_ref"] == BED
    assert supplied in payload.reason and formal in payload.reason
    assert "名称不一致" in payload.reason and "确认前" in payload.reason
    assert payload.additional_changes[0].reason == payload.reason
    assert result.model_content["original_name_review"]["status"] == "explicit_review_required"
    assert result.module_result is not None and result.continuation is not None
    assert_no_direct_chat_confirmation(result)
    assert len(checked) >= 1 and result.history_check.valid()
    allowed["value"] = False
    assert not result.history_check.valid()


def test_transfer_two_old_name_differences_share_one_complete_preview_and_both_proofs():
    prepared = []

    class PreviewOnlyProduct(Product):
        def prepare(self, **kwargs):
            prepared.append(kwargs["payload"])
            return super().prepare(**kwargs)

        def proposal(self, **_):
            return Product.prepare(self)

    turn = _locator_with_quality(product=PreviewOnlyProduct())
    underlying = turn.owner.residence_query.query
    current = {"甲楼": True, "301": True}

    def query(**selection):
        name = selection.get("name")
        if name in current:
            return ResidenceQueryResult(
                {"status": "no_current_records", "records": [], "has_more": False},
                SimpleNamespace(valid=lambda _: current[name]),
            )
        return underlying(**selection)

    turn.owner.residence_query.query = query
    result = turn.execute(call({
        "student_number": "F3-A001", "effective_at": AT,
        "from_building_name": "甲楼", "from_room_name": "301",
        "to_room_name": "302室", "to_bed_name": "1号床",
    }, action="prepare_transfer"), context())
    assert len(prepared) == 1 and result.module_result is not None
    assert prepared[0].target_ref == OLD
    assert all(value in prepared[0].reason for value in ("甲楼", "甲住宿楼", "301", "301室"))
    assert prepared[0].additional_changes[0].reason == prepared[0].reason
    assert result.history_check.valid()
    current["甲楼"] = False
    assert not result.history_check.valid()
    current["甲楼"], current["301"] = True, False
    assert not result.history_check.valid()


def test_transfer_preflight_proves_proposed_time_not_present_vacancy():
    from datetime import datetime

    turn = _locator_with_quality()
    result = turn.execute(call({
        "student_number": "F3-A001", "effective_at": AT,
        "to_room_name": "302室", "to_bed_name": "1号床",
    }, action="prepare_transfer"), context())
    assert result.module_result is not None
    scope = result.model_content["preflight_scope"]
    assert datetime.fromisoformat(scope["effective_at"]) == datetime.fromisoformat(AT)
    assert scope["current_occupancy_queried"] is False
    assert scope["executed"] is False
    assert "当前空闲" in scope["meaning"]


@pytest.mark.parametrize(
    ("view", "expected"),
    [
        ({"status": "available", "records": [{"reference": "f6:dorm_room:another",
                                                "room_name": "旧房说法"}], "has_more": False},
         "object_mismatch"),
        ({"status": "denied", "records": []}, "denied"),
        ({"status": "partial", "records": []}, "partial"),
        ({"status": "source_unavailable", "records": []}, "source_unavailable"),
        ({"status": "no_current_records", "records": [], "has_more": True}, "incomplete"),
        ({"status": "no_current_records", "records": [], "truncated": True}, "incomplete"),
    ],
)
def test_transfer_old_wording_never_hides_other_object_or_incomplete_evidence(view, expected):
    class NoPrepareProduct(Product):
        def prepare(self, **_):
            pytest.fail("Known other coordinates or weak evidence require clarification")

    turn = _locator_with_quality(product=NoPrepareProduct())
    underlying = turn.owner.residence_query.query

    def query(**selection):
        if selection.get("name") == "旧房说法":
            assert selection["related_ref"] == "f6:dormitory_building_profile:known-building"
            return ResidenceQueryResult(view, SimpleNamespace(valid=lambda _: True))
        return underlying(**selection)

    turn.owner.residence_query.query = query
    result = turn.execute(call({
        "student_number": "F3-A001", "effective_at": AT,
        "from_room_name": "旧房说法", "to_room_name": "302室", "to_bed_name": "1号床",
    }, action="prepare_transfer"), context())
    assert result.model_content["status"] == expected
    assert result.module_result is None and result.continuation is None
    if expected == "denied":
        assert "original" not in result.model_content


def test_transfer_old_wording_requires_revalidatable_empty_result():
    class NoPrepareProduct(Product):
        def prepare(self, **_):
            pytest.fail("A non-revalidatable absence cannot support a discrepancy preview")

    turn = _locator_with_quality(product=NoPrepareProduct())
    underlying = turn.owner.residence_query.query

    def query(**selection):
        if selection.get("name") == "旧房说法":
            return ResidenceQueryResult(
                {"status": "no_current_records", "records": [], "has_more": False}, None,
            )
        return underlying(**selection)

    turn.owner.residence_query.query = query
    result = turn.execute(call({
        "student_number": "F3-A001", "effective_at": AT,
        "from_room_name": "旧房说法", "to_room_name": "302室", "to_bed_name": "1号床",
    }, action="prepare_transfer"), context())
    assert result.model_content["status"] == "incomplete"
    assert result.module_result is None


def test_transfer_preparation_keeps_locator_failures_and_never_creates_a_preview():
    class PreviewOnlyProduct(Product):
        def prepare(self, **_):
            pytest.fail("No proposal may be created from a weak location")

    turn = _locator_with_quality(
        weak_kind="dorm_bed",
        quality={"source_status": "stale"},
        quality_name="1号床",
        product=PreviewOnlyProduct(),
    )
    result = turn.execute(
        call(
            {
                "student_number": "F3-A001",
                "effective_at": AT,
                "to_room_name": "302室",
                "to_bed_name": "1号床",
            },
            action="prepare_transfer",
        ),
        context(),
    )
    assert result.model_content["status"] == "partial"
    assert result.model_content["stage"] == "target_bed"
    assert result.module_result is None and result.continuation is None
    assert result.history_check is not None


def test_transfer_preparation_never_guesses_among_multiple_creation_slots():
    class PreviewOnlyProduct(Product):
        def prepare(self, **_):
            pytest.fail("A creation slot must first be selected")

    creation_choices = [
        {"creation_ref": NEW, "label": "甲校区新住宿安排"},
        {"creation_ref": "f6create:student_bed_residence:other", "label": "乙校区新住宿安排"},
    ]
    describe = SimpleNamespace(describe=lambda **_: {
        "fact_kind": "student_bed_residence",
        "fact_timezone_name": "Asia/Shanghai",
        "creation_choices": creation_choices,
        "fields": [],
    })
    turn = _locator_with_quality(product=PreviewOnlyProduct(), describe=describe)
    result = turn.execute(
        call(
            {
                "student_number": "F3-A001",
                "effective_at": AT,
                "to_room_name": "302室",
                "to_bed_name": "1号床",
            },
            action="prepare_transfer",
        ),
        context(),
    )
    assert result.model_content["status"] == "ambiguous"
    assert result.model_content["stage"] == "creation_slot"
    assert result.model_content["candidates"] == creation_choices
    assert result.module_result is None


def test_transfer_preparation_rejects_forged_slot_and_accepts_explicit_authorized_slot():
    class PreviewProduct(Product):
        def __init__(self):
            self.prepares = []

        def prepare(self, **kwargs):
            self.prepares.append(kwargs["payload"])
            return super().prepare(**kwargs)

        def proposal(self, **_):
            return Product.prepare(self)

    choices = [
        {"creation_ref": NEW, "label": "甲校区新住宿安排"},
        {"creation_ref": "f6create:student_bed_residence:other", "label": "另一获准新增槽位"},
    ]
    describe = SimpleNamespace(describe=lambda **_: {
        "fact_kind": "student_bed_residence", "fact_timezone_name": "Asia/Shanghai",
        "creation_choices": choices, "fields": [],
    })
    product = PreviewProduct()
    turn = _locator_with_quality(product=product, describe=describe)
    selection = {
        "student_number": "F3-A001", "effective_at": AT,
        "to_room_name": "302室", "to_bed_name": "1号床",
    }
    rejected = turn.execute(
        call({**selection, "creation_ref": "f6create:student_bed_residence:forged"},
             action="prepare_transfer", ident="forged"),
        context(),
    )
    assert rejected.model_content["status"] == "unsupported_selection"
    assert product.prepares == []
    accepted = turn.execute(
        call({**selection, "creation_ref": NEW}, action="prepare_transfer", ident="selected"),
        context(),
    )
    assert accepted.module_result is not None
    assert len(product.prepares) == 1
    assert product.prepares[0].additional_changes[0].creation_ref == NEW


def test_transfer_preparation_keeps_explicit_read_denial_without_maintenance():
    class PreviewOnlyProduct(Product):
        def prepare(self, **_):
            pytest.fail("A denied read cannot create a proposal")

    def deny(**_):
        raise PermissionError("not permitted")

    turn = owner(
        eligibility=SimpleNamespace(query=deny), product=PreviewOnlyProduct()
    ).for_turn("identity")
    result = turn.execute(
        call(
            {
                "student_number": "F3-A001", "effective_at": AT,
                "to_room_name": "302室", "to_bed_name": "1号床",
            },
            action="prepare_transfer",
        ),
        context(),
    )
    assert result.model_content["status"] == "denied"
    assert result.module_result is None


def test_transfer_preparation_rejects_missing_target_before_reading():
    result = owner().for_turn("identity").execute(
        call({"student_number": "F3-A001", "effective_at": AT}, action="prepare_transfer"),
        context(),
    )
    assert result.model_content["status"] == "unsupported_selection"
    assert result.module_result is None


@pytest.mark.parametrize("candidate_kind", ["dorm_room", "dorm_bed"])
@pytest.mark.parametrize(
    ("quality", "expected"),
    [
        ({"dispute_status": "open"}, "conflict"),
        ({"source_status": "stale"}, "partial"),
    ],
)
def test_lodging_locator_does_not_offer_weak_quality_candidates(
    candidate_kind, quality, expected
):
    turn = _locator_with_quality(weak_kind=candidate_kind, quality=quality)
    result = turn.execute(
        call(
            {
                "student_number": "F3-A001",
                "as_of": AT,
                "to_room_name": "302室" if candidate_kind == "dorm_bed" else None,
            },
            action="locate_change",
        ),
        context(),
    )
    assert result.model_content["status"] == expected
    assert result.model_content["stage"] == (
        "target_bed" if candidate_kind == "dorm_bed" else "target_room"
    )
    assert "candidates" not in result.model_content
    assert "target_bed_ref" not in str(result.model_content)


def test_lodging_locator_review_deadline_invalidates_result_during_model_wait():
    from datetime import UTC, datetime

    time = {"now": datetime(2098, 1, 1, tzinfo=UTC)}
    turn = _locator_with_quality(
        weak_kind="dorm_bed",
        quality={"review_due_at": "2099-01-01T00:00:00+00:00"},
        quality_name="1号床",
        clock=lambda: time["now"],
    )
    result = turn.execute(
        call(
            {
                "student_number": "F3-A001",
                "as_of": AT,
                "to_room_name": "302室",
                "to_bed_name": "1号床",
            },
            action="locate_change",
        ),
        context(),
    )
    assert result.model_content["status"] == "located"
    assert result.history_check.valid()
    time["now"] = datetime(2100, 1, 1, tzinfo=UTC)
    assert not result.history_check.valid()


@pytest.mark.parametrize("due", ["bad-date", "2099-01-01T00:00:00"])
def test_lodging_locator_does_not_interpret_invalid_review_time_as_current(due):
    from datetime import UTC, datetime

    turn = _locator_with_quality(
        weak_kind="dorm_bed",
        quality={"review_due_at": due},
        quality_name="1号床",
        clock=lambda: datetime(2098, 1, 1, tzinfo=UTC),
    )
    result = turn.execute(
        call(
            {
                "student_number": "F3-A001",
                "as_of": AT,
                "to_room_name": "302室",
                "to_bed_name": "1号床",
            },
            action="locate_change",
        ),
        context(),
    )
    assert result.model_content["status"] == "partial"
    assert result.model_content["stage"] == "target_bed"
    assert "source_quality_unavailable" in result.model_content["limitations"]
    assert "target_bed_ref" not in str(result.model_content)
