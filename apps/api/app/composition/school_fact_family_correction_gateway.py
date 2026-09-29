"""Bounded current-Guardian report reads, sharing the normal HTTP owner service."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from typing import cast
from uuid import uuid4

from pydantic import Field

from app.composition.school_fact_directory_context import bind_directory_identity
from app.composition.school_fact_family_correction_product import _LABELS
from app.composition.school_fact_read_evidence import validate_fallback_read
from app.platform.assistant.contracts import JsonObject
from app.platform.assistant.runtime import AssistantRuntimeError, HistoryCheck
from app.platform.assistant.tool_loop import ToolDescriptor, ToolExecutionResult
from app.platform.contracts import FrozenContractModel, GuardianActorContext

NAME = "school_facts.family.corrections"
STATUS = {
    "submitted": ("已提交", "报错已提交，尚未记录处理完成；提交不代表资料已经修改。"),
    "resolved": ("已处理", "该报错已绑定正式处理证明；这项处理状态不等同于当前任课或其他资料值。"),
    "reviewed": ("已核实原资料无误", "正式维护者已核实原资料无误，本次未修改事实。"),
}


class FamilyCorrectionInput(FrozenContractModel):
    limit: int = Field(default=5, ge=1, le=20, strict=True)


def material(page):
    items = []
    for raw in page["items"]:
        row = deepcopy(raw)
        kind = row.get("fact_kind")
        from app.composition.school_fact_education_product import EDUCATION_LABELS
        from app.composition.school_fact_family_staff_gateway import FAMILY_FACT_LABELS

        labels = {
            **FAMILY_FACT_LABELS,
            **EDUCATION_LABELS,
            "official_contact": "官方联系渠道",
            "assignment": "教职工任职",
            "person": "人员资料",
        }
        row["target_label"] = labels.get(kind, "本人所报资料")
        row["field_label"] = (
            "任课教师"
            if kind == "teaching_assignment" and row["field_name"] == "assignment_source_key"
            else _LABELS.get(row["field_name"], "所报字段")
        )
        row["status_label"], row["status_meaning"] = STATUS[row["status"]]
        items.append(row)
    return {
        "kind": "family_correction_history",
        "status": "available",
        "items": items,
        "coverage": "limited_page",
        "scope": "本次目标孩子及本人提交、当前仍获准读取的报错，按提交时间倒序返回有限页。",
        "meaning": (
            "本页不证明全部历史或只有这些报错。"
            "多条同类记录按报告定位和时间区分；处理状态与当前事实分别查询。"
        ),
    }


def model_material(public: JsonObject) -> JsonObject:
    """Keep each report's facts; interpret each observed status only once per page."""
    result = deepcopy(public)
    status_meanings: JsonObject = {}
    for row in cast(list[JsonObject], result["items"]):
        coordinates = {
            key: row.pop(key)
            for key in ("report_id", "fact_kind", "field_name", "status")
            if key in row
        }
        row["tool_context"] = coordinates
        status = cast(str, coordinates["status"])
        status_meanings[status] = {
            "submitted": "已提交，尚未处理完成；不代表资料已经修改。",
            "resolved": "已处理并绑定正式处理证明。",
            "reviewed": "已核实原资料无误，本次未修改事实。",
        }[status]
        row.pop("status_meaning", None)
    result["interpretation"] = {
        "status": status_meanings,
        "scope": result.pop("meaning"),
        "selection": (
            "按当前问题中的对象、事项和时间选择对应报错，只回答相关状态。"
            "有限页中的其他事项不自动成为本轮请求；仍有多个合理目标才追问。"
        ),
    }
    return result


class FamilyCorrectionToolGateway:
    descriptors = (
        ToolDescriptor(
            name=NAME,
            owner="school_facts.family",
            kind="read",
            description="读取本人关于本次目标孩子已提交报错的处理状态、所报字段和时间。区分已提交、已处理、原资料无误；不修改报错、不查他人。按当前问题选择对应事项，当前老师或其他事实值仍用相应查询工具。",
            parameters=FamilyCorrectionInput.model_json_schema(),
        ),
    )

    def __init__(self, *, resolve_actor, product):
        self.resolve_actor, self.product = resolve_actor, product

    def for_turn(self, actor_context_ref):
        return _Turn(self, actor_context_ref)


class _Turn:
    def __init__(self, owner, actor_context_ref):
        self.owner, self.actor_context_ref, self._checks = owner, actor_context_ref, []

    @property
    def descriptors(self):
        return self.owner.descriptors

    def identity(self):
        identity = self.owner.resolve_actor(self.actor_context_ref)
        if type(identity.actor) is not GuardianActorContext:
            raise AssistantRuntimeError("Current selected Guardian required")
        return identity

    def read(self, identity, limit, request_id):
        with bind_directory_identity(self.actor_context_ref):
            return self.owner.product.own_list(identity, request_id, limit=limit).model_dump(
                mode="json"
            )

    def revalidate(self, actor_context_ref):
        if actor_context_ref != self.actor_context_ref:
            raise AssistantRuntimeError("Family report identity changed")
        self.identity()
        for check in self._checks:
            check()

    def execute(self, call, context):
        self.revalidate(context.actor_context_ref)
        if call.name != NAME:
            raise AssistantRuntimeError("Report capability not installed")
        args = FamilyCorrectionInput.model_validate_json(json.dumps(call.arguments))
        identity = self.identity()
        page = self.read(identity, args.limit, context.request_id)
        binding = (identity.actor, identity.session_binding)
        key = hashlib.sha256((self.actor_context_ref + NAME + str(args.limit)).encode()).hexdigest()

        def current():
            latest = self.identity()
            if (latest.actor, latest.session_binding) != binding:
                return False
            return self.read(latest, args.limit, uuid4()) == page

        def validate():
            return validate_fallback_read((NAME, key, id(page)), current)

        # Every release rechecks the current Guardian and complete bounded page.
        # No database/client transaction is held while the model runs.
        validate()
        self._checks.append(validate)
        public = material(page)
        return ToolExecutionResult(
            model_content=model_material(cast(JsonObject, public)),
            fact_card=public,
            history_check=HistoryCheck(
                validate, revalidate=validate, key=key, revalidation_covers_content=True
            ),
        )
