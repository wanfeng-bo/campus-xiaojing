"""Narrow F3 quality creation through the existing preflight/confirm/receipt service."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import datetime
from typing import Any
from uuid import UUID

from app.composition.school_fact_directory_product import FactChangeSummary, ResolvedFactChange
from app.platform.contracts import StaffActorContext
from app.platform.school_facts.contracts import (
    F7_FAMILY_QUALITY_TARGET_KINDS,
    CanonicalFactChangeSetV4,
    fact_content_fields,
)
from app.platform.school_facts.f3_policy import F3_FACT_FIELDS
from app.platform.school_facts.f7_coverage_basis import mother_set_digest
from app.platform.school_facts.school_scope_reference import school_scope_reference_coordinates


def _quality_preview(content: dict[str, Any]) -> dict[str, str]:
    """Complete human review material, excluding private mother-set internals."""

    def display(value: object) -> str:
        return value.isoformat() if isinstance(value, datetime) else str(value)

    result = {
        "所属资料": str(content.get("target_fact_kind") or "未明确"),
        "目标记录": str(content.get("target_source_key") or "未明确"),
        "所属人员": str(content.get("person_source_key") or "未明确"),
        "适用字段": str(content.get("field_name") or "整体资料"),
        "声明类型": str(content.get("quality_kind") or "未明确"),
        "核查依据": str(content.get("basis_reference") or "未提供"),
        "责任主体": str(content.get("responsibility") or "未提供"),
        "核查时间": display(content.get("verified_at") or "未提供"),
        "有效至": display(content.get("valid_to") or "未预设"),
    }
    for field, label, choices in (
        ("coverage_status", "覆盖声明", {"complete": "完整", "partial": "部分"}),
        (
            "source_status",
            "来源状态",
            {"available": "可用（学校登记）", "unavailable": "不可用（学校登记）"},
        ),
        ("negative_code", "正式空缺", {}),
        ("dispute_status", "争议状态", {"open": "待处理", "resolved": "已处理"}),
        ("evidence_reference", "补充证据", {}),
        ("replacement_source_key", "纠正目标", {}),
        ("review_due_at", "复核日期", {}),
    ):
        value = content.get(field)
        if value is not None:
            result[label] = choices.get(str(value), display(value))
    if content.get("coverage_basis") is not None:
        result["完整依据"] = "已准备当前正式母集核对；确认时仍须重新核验"
    return result


class FamilyQualityProductAdapter:
    def __init__(
        self,
        *,
        directory: Any,
        family: Any,
        guard: Any,
        existing: Any = None,
        existing_target_kind: Callable[[StaffActorContext, str], str] | None = None,
    ) -> None:
        self.directory = directory
        self.family = family
        self.guard = guard
        self.existing = existing
        self.existing_target_kind = existing_target_kind or (
            existing.target_kind if existing is not None else None
        )

    def recognizes(self, actor: StaffActorContext, payload: Any) -> bool:
        if payload.fact_kind != "fact_quality":
            return False
        if payload.creation_ref is not None:
            reference = payload.fields.get("target_ref")
            coordinates = school_scope_reference_coordinates(reference)
            return (isinstance(reference, str) and reference.startswith("f3:")) or (
                coordinates is not None
                and coordinates[0] == "f3"
                and coordinates[1] in F7_FAMILY_QUALITY_TARGET_KINDS
                and not coordinates[2]
            )
        if self.existing_target_kind is None or payload.target_ref is None:
            return False
        key = self.directory.repository.source_for(
            actor=actor, fact_kind="fact_quality", reference=payload.target_ref
        )
        return self.existing_target_kind(actor, key) in F7_FAMILY_QUALITY_TARGET_KINDS

    def _current_target_owner(
        self, actor: StaffActorContext, kind: str, target_ref: str
    ) -> tuple[Any, str, Any | None]:
        """Use the F3 authorized Reader, not a signed reference as existence proof."""
        selected = self.family.repository.prepare_target(
            actor=actor, fact_kind=kind, target_ref=target_ref, creation_ref=None
        )
        if kind in {"student", "login_mapping", "person_region"}:
            owner = selected.content.get("person_source_key")
            student = None
        else:
            student_key = selected.content.get("student_source_key")
            if not isinstance(student_key, str) or not student_key:
                raise PermissionError("The F3 target owner cannot be verified.")
            student_ref = self.family.repository.reference_for(
                actor=actor, fact_kind="student", source_key=student_key
            )
            student = self.family.repository.prepare_target(
                actor=actor,
                fact_kind="student",
                target_ref=student_ref,
                creation_ref=None,
            )
            if (
                student.source_key != student_key
                or student.school_fact_version != selected.school_fact_version
            ):
                raise PermissionError("The F3 student owner changed.")
            owner = student.content.get("person_source_key")
        if not isinstance(owner, str) or not owner:
            raise PermissionError("The F3 target owner cannot be verified.")
        return selected, owner, student

    def resolve_change(
        self,
        *,
        actor: StaffActorContext,
        payload: Any,
        request_id: UUID,
        effective_at: datetime,
    ) -> ResolvedFactChange:
        if payload.fact_kind != "fact_quality" or payload.operation not in {
            "upsert",
            "end",
            "revoke",
        }:
            raise ValueError("F3 quality needs one supported formal maintenance operation.")
        creation = payload.creation_ref is not None
        if not creation and self.existing is None:
            raise PermissionError("Original family quality cannot be verified.")
        original = None
        if creation:
            key = self.directory.repository.source_for(
                actor=actor,
                fact_kind="fact_quality",
                reference=payload.creation_ref,
                creation=True,
            )
            previous_version = 0
            school_version = None
        else:
            key = self.directory.repository.source_for(
                actor=actor, fact_kind="fact_quality", reference=payload.target_ref
            )
            original = self.existing(actor, key)
            previous_version = original["version"]
            school_version = self.existing.projection.current_version(actor.school_id)
        if payload.operation != "upsert" and (creation or payload.fields):
            raise ValueError("Ending or revoking a quality claim cannot replace its content.")
        fields = dict(payload.fields)
        if "coverage_basis" in fields:
            raise ValueError("Mother-set proof is generated by the authorized server read.")
        target_ref = fields.pop("target_ref", None)
        person_ref = fields.pop("person_ref", None)
        allowed, _ = fact_content_fields("fact_quality", 4)
        if set(fields) - allowed - {"target_fact_kind"}:
            raise ValueError("Unregistered F3 quality fields.")
        content = None
        selected_target = None
        selected_student = None
        owner_key = None
        if payload.operation == "upsert":
            content = dict(original["content"] if original is not None else {})
            content.update(fields)
            if target_ref is not None:
                if not isinstance(target_ref, str):
                    raise ValueError("Invalid F3 target reference.")
                target_kind, target_key = self.family.repository.source_for_reference(
                    actor, target_ref
                )
                if content.get("target_fact_kind") != target_kind:
                    raise ValueError("Quality target type does not match the selected F3 fact.")
                selected_target, owner_key, selected_student = self._current_target_owner(
                    actor, target_kind, target_ref
                )
                if selected_target.source_key != target_key:
                    raise PermissionError("The F3 target changed during selection.")
                if creation:
                    school_version = selected_target.school_fact_version
                elif selected_target.school_fact_version != school_version:
                    raise PermissionError("The F3 target changed during selection.")
                content["target_source_key"] = target_key
            elif creation:
                raise ValueError("Exact F3 target reference is required.")
            if person_ref is not None:
                if not isinstance(person_ref, str):
                    raise ValueError("Invalid person reference.")
                content["person_source_key"] = self.directory.repository.source_for(
                    actor=actor, fact_kind="person", reference=person_ref
                )
                if owner_key != content["person_source_key"]:
                    raise PermissionError("Quality owner does not match the selected F3 fact.")
            elif owner_key is not None:
                content["person_source_key"] = owner_key
            elif creation:
                raise PermissionError("The F3 target owner cannot be verified.")
            kind = content.get("target_fact_kind")
            if kind not in F7_FAMILY_QUALITY_TARGET_KINDS:
                raise ValueError("F3 quality target type is not registered.")
            field_name = content.get("field_name")
            if field_name is not None and field_name not in F3_FACT_FIELDS[kind]:
                raise ValueError("Quality field does not belong to the selected F3 fact.")
            content["owner_kind"] = "person"
            if (
                content.get("quality_kind") == "coverage"
                and content.get("coverage_status") == "complete"
            ):
                selected_ref = target_ref or self.family.repository.reference_for(
                    actor=actor, fact_kind=kind, source_key=content["target_source_key"]
                )
                selected = self.family.repository.prepare_target(
                    actor=actor,
                    fact_kind=kind,
                    target_ref=selected_ref,
                    creation_ref=None,
                )
                if selected.school_fact_version != school_version:
                    raise PermissionError("Target changed while preparing complete coverage.")
                count, digest = mother_set_digest(
                    ((selected.source_key, selected.expected_version),)
                )
                content["coverage_basis"] = {
                    "schema_version": 1,
                    "school_id": str(actor.school_id),
                    "owner_kind": "person",
                    "owner_source_key": content["person_source_key"],
                    "target_fact_kind": kind,
                    "target_source_key": selected.source_key,
                    "field_name": content.get("field_name"),
                    "purpose": content.get("purpose"),
                    "contact_kind": content.get("contact_kind"),
                    "valid_from": effective_at.isoformat(),
                    "valid_to": content.get("valid_to"),
                    "school_fact_version": school_version,
                    "member_count": count,
                    "member_sha256": digest,
                }
            else:
                content.pop("coverage_basis", None)
        else:
            kind = original["content"]["target_fact_kind"]
            field_name = original["content"].get("field_name")
        if school_version is None:
            raise PermissionError("The current school fact version cannot be verified.")
        material = {
            "schema_version": 4,
            "base_version": school_version,
            "source_sha256": hashlib.sha256(str(request_id).encode()).hexdigest(),
            "changes": [
                {
                    "schema_version": 4,
                    "fact_kind": "fact_quality",
                    "source_key": key,
                    "expected_version": previous_version,
                    "operation": payload.operation,
                    "effective_at": effective_at.isoformat(),
                    "content": content,
                    "origin": {"sheet_code": "family_quality", "row_number": 1},
                }
            ],
        }
        change_set = CanonicalFactChangeSetV4.model_validate_json(
            json.dumps(material, ensure_ascii=False, default=str)
        )
        proof = self.guard.require_change_set(actor, change_set)

        def current(selected: StaffActorContext) -> None:
            if selected != actor:
                raise PermissionError("Family quality maintenance identity changed.")
            if original is not None:
                original["proof"].revalidate()
            if selected_target is not None and target_ref is not None:
                latest, latest_owner, latest_student = self._current_target_owner(
                    selected, kind, target_ref
                )
                if (
                    latest.source_key != selected_target.source_key
                    or latest.expected_version != selected_target.expected_version
                    or latest_owner != owner_key
                    or (selected_student is None) != (latest_student is None)
                    or selected_student is not None
                    and latest_student is not None
                    and latest_student.expected_version != selected_student.expected_version
                ):
                    raise PermissionError("The F3 quality target changed before confirmation.")
            proof.revalidate()

        current(actor)
        return ResolvedFactChange(
            change_set=change_set,
            summary=FactChangeSummary(
                fact_kind="fact_quality",
                label="家校资料依据",
                reference=payload.creation_ref or payload.target_ref,
                operation=payload.operation,
                before=None if original is None else _quality_preview(original["content"]),
                after=None if content is None else _quality_preview(content),
                effective_at=effective_at,
                reason=payload.reason,
                source_label="学校正式发布的家校资料依据",
            ),
            summary_revalidate=current,
        )
