"""F7 package provenance binding shared by Product normalization and sole Writer."""

from __future__ import annotations

import json
from hashlib import sha256

from app.platform.school_facts.contracts import CanonicalFactChangeSetV1, CanonicalFactChangeV1
from app.platform.school_facts.ports import (
    SchoolFactChangeProvenance,
    SchoolFactPolicyTextAttachment,
    SchoolFactRowProvenance,
)
from app.platform.school_facts.replacement_contracts import PackageReplacementTarget


def full_scope_plan_sha256(
    *,
    raw_source_sha256: str,
    changes: tuple[CanonicalFactChangeV1, ...],
    row_lineage: tuple[SchoolFactRowProvenance, ...],
    policy_attachments: tuple[SchoolFactPolicyTextAttachment, ...],
    replacement_targets: tuple[PackageReplacementTarget, ...] = (),
) -> str:
    value = {
        "raw_source_sha256": raw_source_sha256,
        "changes": [item.model_dump(mode="json", exclude_unset=True) for item in changes],
        "row_lineage": [
            {
                "fact_kind": item.fact_kind,
                "fact_source_key": item.fact_source_key,
                "raw_source_sha256": item.raw_source_sha256,
                "source_slot": item.source_slot,
                "source_sheet_code": item.source_sheet_code,
                "source_row_number": item.source_row_number,
                "target_field_codes": item.target_field_codes,
                "source_field_codes": item.source_field_codes,
            }
            for item in row_lineage
        ],
        "attachment_bindings": [
            {
                "source_slot": item.source_slot,
                "source_sha256": item.source_sha256,
                "fact_source_key": item.fact_source_key,
                "field_code": item.field_code,
            }
            for item in policy_attachments
        ],
    }
    if replacement_targets:
        value["replacement_targets"] = [t.model_dump(mode="json") for t in replacement_targets]
    return sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()


def require_full_scope_provenance_integrity(
    provenance: SchoolFactChangeProvenance, change_set: CanonicalFactChangeSetV1
) -> None:
    """Reject a package whose signed canonical rows or attachments differ from its source."""

    if provenance.channel != "package":
        raise ValueError("当前来源并非全范围输入包。")
    if change_set.schema_version != 4 or not 1 <= len(change_set.changes) <= 50_000:
        raise ValueError("全范围输入包的规范变更版本或数量无效。")
    if len(provenance.row_lineage) != len(change_set.changes):
        raise ValueError("全范围输入包的规范变更与原行血缘数量不一致。")
    for change, origin in zip(change_set.changes, provenance.row_lineage, strict=True):
        if (change.fact_kind, change.source_key) != (
            origin.fact_kind,
            origin.fact_source_key,
        ):
            raise ValueError("全范围输入包的规范变更与原行血缘目标不一致。")
    for attachment in provenance.policy_attachments:
        matches = tuple(
            change
            for change in change_set.changes
            if change.fact_kind == "policy_version"
            and change.source_key == attachment.fact_source_key
        )
        if len(matches) != 1 or matches[0].content is None:
            raise ValueError("制度正文附件没有唯一的正式变更目标。")
        text = matches[0].content.content_text
        if not isinstance(text, str) or sha256(text.encode("utf-8")).hexdigest() != (
            attachment.source_sha256
        ):
            raise ValueError("制度正文附件摘要与正式正文不一致。")
    if change_set.source_sha256 != full_scope_plan_sha256(
        raw_source_sha256=provenance.raw_source_sha256,
        changes=change_set.changes,
        row_lineage=provenance.row_lineage,
        policy_attachments=provenance.policy_attachments,
        replacement_targets=provenance.replacement_targets,
    ):
        raise ValueError("全范围输入包规范变更摘要不一致。")
