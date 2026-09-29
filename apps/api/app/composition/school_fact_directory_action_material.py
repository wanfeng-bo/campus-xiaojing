"""Readable fact-action material; canonical cards remain the execution authority."""

from __future__ import annotations

import re
from copy import deepcopy
from typing import cast

from pydantic import JsonValue

from app.composition.school_fact_directory_adapter import (
    ENUM_CONTEXT_LABELS,
    ENUM_LABELS,
    FACT_LABELS,
    FIELD_CONTEXT_LABELS,
    FIELD_LABELS,
    PROVENANCE_FIELDS,
    SCHOOL_DEFINED_CODE_FIELDS,
)
from app.platform.assistant.contracts import JsonObject
from app.platform.school_facts.contracts import fact_content_enums
from app.platform.school_facts.school_scope_reference import school_scope_reference_coordinates


def _change_value(kind: str, field: str, fields: JsonObject) -> JsonValue:
    if field not in fields:
        return {"meaning": "本侧候选未提供该字段"}
    value = fields[field]
    enums = fact_content_enums(kind)
    if field in enums and isinstance(value, str):
        label = ENUM_CONTEXT_LABELS.get((kind, field, value), ENUM_LABELS.get(value))
        if value in enums[field] and label is not None:
            return label
        return {"meaning": "尚无领域释义的原始值", "raw": value}
    return deepcopy(value)


def proposal_model_material(view: JsonObject, *, action_number: int | None = None) -> JsonObject:
    """Summarize an already-authorized proposal, never approve or alter it."""
    summary = cast(JsonObject, view["summary"])
    kind = cast(str, summary["fact_kind"])
    before = cast(JsonObject, summary.get("before") or {})
    after = cast(JsonObject, summary.get("after") or {})
    changes: list[JsonValue] = []
    relation_changes: JsonObject = {}
    uninterpreted_changes: JsonObject = {}
    operation = str(summary["operation"])
    content_fields = (
        dict.fromkeys((*before, *after)) if operation in {"upsert", "reactivate"} else {}
    )
    code_fields = SCHOOL_DEFINED_CODE_FIELDS - fact_content_enums(kind).keys()
    for field in content_fields:
        if field in before and field in after and before[field] == after[field]:
            continue
        pair: JsonObject = {
            "before": _change_value(kind, field, before),
            "after": _change_value(kind, field, after),
        }
        label = FIELD_CONTEXT_LABELS.get((kind, field), FIELD_LABELS.get(field))
        if field.endswith("_ref"):
            relation_changes[field] = pair
            changes.append(
                {
                    "field": label or "关联对象",
                    "meaning": "关联对象发生变更，须在完整确认页核对精确引用",
                }
            )
        elif (
            field in code_fields
            or field in PROVENANCE_FIELDS
            or field in {"business_code", "display_path"}
            or label is None
            and field != "original_text"
        ):
            uninterpreted_changes[field] = pair
            changes.append(
                {
                    "field": label or "原始字段",
                    "meaning": "原始记录值发生变更，完整差异在确认页核对",
                }
            )
        else:
            changes.append({"field": label or FIELD_LABELS["content_text"], **pair})
    context: JsonObject = {
        "proposal_ref": view["proposal_ref"],
        "fact_kind": kind,
        "operation": summary["operation"],
    }
    if action_number is not None:
        context.pop("proposal_ref")
        context["action_number"] = action_number
    if summary.get("reference") is not None:
        context["reference"] = summary["reference"]
    if relation_changes:
        context["relation_changes"] = relation_changes
    if uninterpreted_changes:
        context["uninterpreted_changes"] = uninterpreted_changes
    material: JsonObject = {
        "kind": "fact_proposal",
        "outcome": "候选已准备，尚未正式执行",
        "fact_type": FACT_LABELS[kind],
        "operation": {
            "upsert": "发布或更新",
            "end": "结束",
            "revoke": "撤销",
            "reactivate": "重新启用原对象",
        }[operation],
        "changes": changes,
        "tool_context": context,
        "confirmation_requirement": (
            "需用户在完整确认页核对身份、精确对象、全部差异及生效时间并明确确认；"
            "候选和聊天接受均不算正式执行"
        ),
    }
    for field in ("reason", "source_label", "effective_at"):
        if field in summary:
            material[field] = deepcopy(summary[field])
    if "expires_at" in view:
        material["expires_at"] = view["expires_at"]
    return material


def own_receipt_current_model_material(
    receipt: JsonObject,
    current_results: list[JsonValue],
    *,
    original_results: list[JsonValue] | None = None,
    current_requested: bool = True,
) -> JsonObject:
    """Keep receipt, original fields and current reads independent of unread events."""
    material: JsonObject = {
        "kind": "own_receipt_current",
        "receipt": deepcopy(receipt),
        "current_results": deepcopy(current_results),
        "evidence_scope": {
            "original": {
                "field_values": "not_returned",
                "meaning": "receipt只报告原操作执行结果及目标历史记录版本，不含修改前后字段正文。",
            },
            "current": {
                "reading": "independent_per_target",
                "meaning": (
                    "current_results按各项reference对应同一原目标的独立当前读取；"
                    "是否取得当前事实以各项result为准。"
                ),
            },
            "interval": {
                "events": "not_queried",
                "meaning": (
                    "原操作与当前读取两时点之间的变更事件未查询；"
                    "这两份材料不能判定期间是否发生变更或其原因。"
                ),
            },
        },
        "tool_context": {"relationship_basis": "original_receipt_target_reference"},
    }
    scope = cast(JsonObject, material["evidence_scope"])
    if original_results is not None:
        material["original_results"] = deepcopy(original_results)
        scope["original"] = {
            "field_values": "per_target_result",
            "meaning": (
                "receipt报告原操作执行结果；original_results按各项reference独立读取原目标的"
                "精确历史版本字段，是否取得原字段以各项result为准。"
            ),
        }
    if not current_requested:
        scope["current"] = {"reading": "not_requested", "meaning": "本次未请求目标当前事实读取。"}
        scope["interval"] = {
            "events": "not_queried",
            "meaning": (
                "本次未查询原操作之后的变更事件；"
                "原字段结果不能判定此后是否发生变更或其原因。"
            ),
        }
    return material


def receipt_model_material(view: JsonObject, *, action_number: int | None = None) -> JsonObject:
    """Separate receipt outcome from tool handles; unknown never becomes success."""
    status = view["status"]
    outcome = {
        "succeeded": "正式修改已完成",
        "rejected": "正式修改被拒绝",
        "unknown": "原操作结果尚未确定",
    }[str(status)]
    material: JsonObject = {
        "kind": "fact_receipt",
        "outcome": outcome,
        "message": view["message"],
        "tool_context": {
            field: view[field]
            for field in ("reference", "request_id")
            if view.get(field) is not None
        },
    }
    if action_number is not None:
        material["tool_context"] = {"action_number": action_number}
    if view.get("changed_count") is not None:
        material["changed_count"] = view["changed_count"]
    if view.get("target_version") is not None:
        cast(JsonObject, material["tool_context"])["school_fact_version"] = view["target_version"]
    if status == "unknown":
        material["recovery"] = "只按同一原回执查询，不重新提交或推断已完成"
    return material


def own_receipt_model_material(
    view: JsonObject,
    *,
    target_offset: int = 0,
    target_limit: int = 20,
    detail_tool: str = "school_facts.directory.own_receipt",
    include_audit: bool = False,
) -> JsonObject:
    """Page model targets while the complete authorized receipt remains unchanged."""
    material = receipt_model_material(view)
    # The product message stays in the card; its outcome and read-only boundary
    # are represented once here, without repeating the same recovery explanation.
    material.pop("message")
    targets = cast(list[JsonObject], view["targets"])
    supplied_page = view.get("target_page")
    if (
        type(target_offset) is not int
        or target_offset < 0
        or type(target_limit) is not int
        or not 1 <= target_limit <= 20
    ):
        raise ValueError("invalid receipt target page")
    if isinstance(supplied_page, dict):
        page = cast(JsonObject, supplied_page)
        end, total = page.get("next_offset"), page.get("target_count")
        if (
            page.get("offset") != target_offset
            or type(end) is not int
            or type(total) is not int
            or not target_offset <= end <= min(total, target_offset + target_limit)
            or len(targets) > end - target_offset
            or page.get("has_more") is not (end < total)
        ):
            raise ValueError("invalid authorized receipt page")
        selected_targets = targets
    else:
        if target_offset >= len(targets):
            raise ValueError("invalid receipt target page")
        end = min(target_offset + target_limit, len(targets))
        total = len(targets)
        selected_targets = targets[target_offset:end]
    context = cast(JsonObject, material["tool_context"])
    context.pop("reference", None)
    context.pop("request_id", None)
    if include_audit:
        context["confirmed_at"] = view["confirmed_at"]
    else:
        context.pop("school_fact_version", None)
    material["fact_types"] = list(dict.fromkeys(str(target["label"]) for target in targets))
    context["target_page"] = {
        "offset": target_offset,
        "end": end,
        "total_count": total,
        "has_more": end < total,
    }
    if end < total:
        context["read_next"] = {
            "tool": detail_tool,
            "arguments": {
                "receipt_ref": view["reference"],
                "target_offset": end,
                "target_limit": target_limit,
                **({"include_audit": True} if include_audit else {}),
            },
        }

    def current_read(target: JsonObject) -> JsonObject:
        kind, reference = target.get("fact_kind"), target.get("reference")
        if (
            kind in {"teaching_assignment", "teaching_offering"}
            and isinstance(reference, str)
            and (re.fullmatch(rf"f4:{kind}:[0-9a-f]{{64}}", reference)
                 or school_scope_reference_coordinates(reference) == ("f5", kind, False))
        ):
            return {
                "current_read": {
                    "tool": "school_facts.teaching.query",
                    "arguments": {"view": "teaching", "target_reference": reference},
                }
            }
        return {}

    context.update({
        "receipt_ref": view["reference"],
        "targets": [
            {
                "fact_kind": target["fact_kind"],
                "reference": target["reference"],
                **({"record_version": target["target_version"]} if include_audit else {}),
                **current_read(target),
            }
            for target in selected_targets
        ],
    })
    if include_audit:
        context["version_meaning"] = (
            "school_fact_version是原操作后的学校事实版本；"
            "targets.record_version是该对象原操作产生的历史记录版本，不是当前记录版本。"
        )
        context["meaning"] = (
            "本人原正式操作的只读回执，不代表再次执行或仍有维护权，也不证明目标当前存在或字段可读；"
            "仅核对原操作结果可据此作答；用户同时询问当前事实时，才按对象引用另行查询。"
            "目标仅为target_page当前页，未返回不表示不存在；按需用read_next续查，完整明细保留在维护详情。"
        )
    else:
        context["meaning"] = (
            "本人原操作的只读回执，仅报告执行结果；"
            "目标仅为target_page当前页，未返回目标不表示不存在。"
        )
    return material


def own_receipts_model_material(
    view: JsonObject, *, detail_tool: str = "school_facts.directory.own_receipt"
) -> JsonObject:
    """Summarize authorized receipts; full targets remain in the controlled detail read."""
    receipts: list[JsonValue] = []
    for raw in cast(list[JsonValue], view["items"]):
        source = cast(JsonObject, raw)
        item = receipt_model_material(source)
        item.pop("message")
        targets = cast(list[JsonObject], source["targets"])
        item.update(
            status=source["status"],
            confirmed_at=source["confirmed_at"],
            fact_types=list(dict.fromkeys(str(target["label"]) for target in targets)),
            target_count=len(targets),
            tool_context={
                **{
                    key: value for key, value in cast(JsonObject, item["tool_context"]).items()
                    if key not in {"reference", "request_id"}
                },
                "receipt_ref": source["reference"],
            },
        )
        receipts.append(item)
    return {
        "kind": "own_fact_receipts",
        "receipts": receipts,
        "read_detail": {"tool": detail_tool, "argument": "receipt_ref"},
        "tool_context": {"version_meaning": "school_fact_version是原操作后的学校事实版本。"},
        "meaning": (
            "本人原正式操作的只读回执摘要；仅核对原操作结果，不证明目标当前存在、当前值或仍有维护权。"
            "需目标明细与对象引用时，将所选摘要的receipt_ref交给read_detail指定工具，按返回分页合同继续；详情读取仍重验当前权限。"
            "当前页不是全部历史，不能据此断言没有操作历史。"
        ),
        "has_more": view["has_more"],
        "notice": view["notice"],
    }
