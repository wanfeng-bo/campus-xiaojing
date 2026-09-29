"""Presentation boundary for already-authorized school-fact query materials.

This does not evaluate permissions or facts, choose an answer, or inspect user text.
Only known result envelopes are visited; formal field values and arbitrary payloads
are opaque. Cards, history proofs and maintenance commands never use this projection.
"""

import json
from copy import deepcopy
from typing import Any

_RESULT_CONTAINERS = frozenset(
    {
        "records",
        "items",
        "results",
        "teachers",
        "members",
        "courses",
        "contacts",
        "channels",
        "audiences",
        "children",
        "headteachers",
        "class_teachers",
    }
)
_EVIDENCE_FIELDS = (
    "source",
    "sources",
    "quality",
    "readable_quality",
    "quality_basis",
    "publication_evidence",
    "登记来源",
    "来源时间",
)
_QUALITY_FIELDS = ("quality", "readable_quality", "quality_basis", "独立质量声明")
_CLAIM_BINDINGS = frozenset(
    {
        "scope",
        "conditions",
        "field_name",
        "field",
        "item_index",
        "academic_year",
        "target_ref",
        "target_fact_kind",
        "owner_ref",
        "owner_kind",
        "applies_to_records",
        "applies_to_query",
        "effective_at",
        "valid_from",
        "valid_to",
        "tool_context",
    }
)
_RISK_VALUES = {
    "dispute_status": {"open", "conflicting_statements"},
    "source_status": {"unavailable", "unreachable", "stale", "conflicting_statements"},
    "evidence_status": {"denied", "forbidden", "source_unavailable"},
    "conflict": {
        "open",
        "conflicting_statements",
        "contradictory_evidence",
        "正式资料存在待核实争议",
        "质量声明相互不一致",
        "正式事实与否定声明相互矛盾",
    },
    "freshness": {
        "review_overdue",
        "conflicting_statements",
        "已到声明规定的核实期限",
        "核实时效声明相互不一致",
    },
    "source_statement": {"声明记载原来源不可用", "来源可用性的声明相互不一致"},
    "reading": {"本次未能读取质量声明", "当前身份无权读取质量声明"},
}
_CLAIM_KEYS = frozenset(
    {
        "quality_kind",
        "negative_code",
        "kind",
        "scope",
        "details",
        "statements",
        "statement_groups",
        "coverage",
        "coverage_status",
        "existence",
        "reading_limit",
        "evidence_truncated",
        "risks",
        *_RISK_VALUES,
    }
)


def _constraints(quality: Any) -> dict[str, list[Any]]:
    """Surface only supplied explicit constraints, retaining local claim scope.

    Unknown quality is not evidence of an unsafe or false positive fact. No dates
    are evaluated here: freshness comes from the authoritative query outcome.
    """
    absence: list[Any] = []
    issues: list[Any] = []

    def inspect(
        value: Any, field: str | None = None, inherited_scope: dict[str, Any] | None = None
    ) -> None:
        if isinstance(value, list):
            for item in value:
                inspect(item, field, inherited_scope)
            return
        if not isinstance(value, dict):
            return
        if not value.keys() & _CLAIM_KEYS:
            # F3 maps a quality summary to each requested, authorized field.
            for name, item in value.items():
                if isinstance(item, (dict, list)):
                    inspect(item, name, inherited_scope)
            return
        local = {
            **(inherited_scope or {}),
            **{
                key: item
                for key, item in value.items()
                if key not in {"statements", "statement_groups"}
            },
        }
        parent_context = (inherited_scope or {}).get("tool_context")
        own_context = value.get("tool_context")
        if isinstance(parent_context, dict) and isinstance(own_context, dict):
            local["tool_context"] = {**parent_context, **own_context}
        bound = deepcopy(local) if field is None else {"field": field, "evidence": deepcopy(local)}
        details = value.get("details")
        details = details if isinstance(details, dict) else {}
        if (
            value.get("negative_code") is not None
            or details.get("negative_code") is not None
            or details.get("正式否定事项") is not None
        ):
            absence.append(bound)
        risk = (
            value.get("evidence_truncated") is True
            or bool(value.get("reading_limit"))
            or (isinstance(value.get("risks"), list) and bool(value["risks"]))
        )
        risk = risk or any(
            isinstance(value.get(key), str) and value[key] in choices
            for key, choices in _RISK_VALUES.items()
        )
        # Directory claims keep their own risk in translated details when the
        # aggregate is unknown; never promote that claim to unrelated objects.
        risk = risk or any(
            isinstance(details.get(label), str) and details[label] in _RISK_VALUES[category]
            for label, category in (("争议状态", "conflict"), ("来源可用状态", "source_statement"))
        )
        if risk:
            # Copy only the actual problem plus its binding, not all normal
            # quality metadata or full evidence again. This also keeps the
            # existing bounded history capacity usable for continuous queries.
            issue = {
                key: deepcopy(item)
                for key, item in local.items()
                if key in _CLAIM_BINDINGS
                or key
                in {
                    "status",
                    "risks",
                    "quality_kind",
                    "kind",
                }
                or (key in _RISK_VALUES and isinstance(item, str) and item in _RISK_VALUES[key])
                or (key == "evidence_truncated" and item is True)
                or (key == "reading_limit" and bool(item))
            }
            risk_details = {
                label: details[label]
                for label, category in (
                    ("争议状态", "conflict"),
                    ("来源可用状态", "source_statement"),
                )
                if isinstance(details.get(label), str) and details[label] in _RISK_VALUES[category]
            }
            if risk_details:
                issue["details"] = risk_details
            issues.append(issue if field is None else {"field": field, "evidence": issue})
        for name in ("statements", "statement_groups"):
            children = value.get(name)
            if isinstance(children, (list, dict)):
                scope = {key: item for key, item in local.items() if key in _CLAIM_BINDINGS}
                inspect(children, field, scope)

    inspect(quality)
    result = {}
    if absence:
        result["established_absences"] = absence
    if issues:
        result["issues_affecting_answer"] = _share_identical_issue_scope(issues)
    return result


def _share_identical_issue_scope(issues: list[Any]) -> list[Any]:
    """Represent exactly equal applicability once; keep every issue and locator.

    This is only structural sharing inside one object's model material. It does
    not merge different objects, fields, conditions or periods, or cache results.
    """
    grouped: dict[str, tuple[dict[str, Any], list[dict[str, Any]]]] = {}
    singles: list[Any] = []
    for item in issues:
        shared = {
            key: value
            for key, value in item.items()
            if key in _CLAIM_BINDINGS and key != "tool_context"
        }
        context = item.get("tool_context")
        if isinstance(context, dict):
            binding_context = {
                key: value
                for key, value in context.items()
                if key in {"owner_ref", "target_ref", "applies_to_records"}
            }
            if binding_context:
                shared["tool_context"] = binding_context
        if not shared or "evidence" in item:
            singles.append(item)
            continue
        signature = json.dumps(shared, ensure_ascii=False, sort_keys=True)
        grouped.setdefault(signature, (shared, []))[1].append(item)
    for shared, members in grouped.values():
        if len(members) == 1:
            singles.extend(members)
            continue
        parts = []
        for member in members:
            part = {key: value for key, value in member.items() if key not in shared}
            if "tool_context" in shared:
                part["tool_context"] = {
                    key: value
                    for key, value in member["tool_context"].items()
                    if key not in shared["tool_context"]
                }
            parts.append(part)
        singles.append({**shared, "issues": parts})
    return singles


def answer_evidence_material(value: Any) -> Any:
    """Separate local evidence while keeping scope, time, controls and facts usable."""
    output = deepcopy(value)

    def separate(current: Any) -> None:
        if isinstance(current, list):
            for child in current:
                separate(child)
            return
        if not isinstance(current, dict):
            return
        # Avoid accidentally presenting discovery schemas or formal operations as
        # ordinary facts when a consumer shares its outer query plumbing.
        if "proposal_ref" in current or "confirmation_token" in current:
            return
        evidence = current.get("supporting_evidence")
        if evidence is not None and not isinstance(evidence, dict):
            return
        evidence = evidence if isinstance(evidence, dict) else {}
        constraints: dict[str, list[Any]] = {}
        for key in _QUALITY_FIELDS:
            if key in evidence:
                for category, claims in _constraints(evidence[key]).items():
                    constraints.setdefault(category, []).extend(claims)
        for key in _EVIDENCE_FIELDS:
            item = current.get(key)
            if not isinstance(item, (dict, list)) and key not in {"登记来源", "来源时间"}:
                continue
            if key not in current or key in evidence:
                continue
            if key in _QUALITY_FIELDS:
                for category, claims in _constraints(item).items():
                    constraints.setdefault(category, []).extend(claims)
            evidence[key] = current.pop(key)
        if evidence:
            current["supporting_evidence"] = evidence
        # Never overwrite an existing domain constraint contract.
        if constraints and "answer_constraints" not in current:
            current["answer_constraints"] = constraints
        for key in _RESULT_CONTAINERS:
            if key in current:
                separate(current[key])

    separate(output)
    return output
