"""Education maintenance material and canonical adapter for the existing sole Writer."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from datetime import datetime
from types import MappingProxyType
from typing import Any, ClassVar, Literal, Self, cast
from uuid import UUID

from pydantic import Field, JsonValue, model_validator

from app.composition.school_fact_directory_product import (
    FactChangeInput,
    FactChangeSummary,
    ResolvedFactChange,
)
from app.domains.students.education_contracts import (
    EDUCATION_CONTENT_FIELDS,
    EDUCATION_FIELDS,
    EDUCATION_REFERENCES,
)
from app.platform.contracts import StaffActorContext
from app.platform.school_facts.contracts import CanonicalFactChangeSetV1, CanonicalFactContentV3
from app.platform.school_facts.school_scope_reference import school_scope_reference_coordinates


class EducationChangeInput(FactChangeInput):
    safe_constraint_messages: ClassVar[Mapping[str, str]] = MappingProxyType(
        {
            **FactChangeInput.safe_constraint_messages,
            "education_cancel_from": ("cancel_from仅用于教学资料的cancel操作，且必须包含时区。"),
            "education_relationship_correction": (
                "在校关系补正须使用原关系的target_ref，不能使用creation_ref或携带身份、版本、权限字段。"
            ),
        }
    )
    cancel_from: datetime | None = None

    @model_validator(mode="after")
    def target(self) -> Self:
        if self.cancel_from is not None:
            from app.domains.students.teaching_contracts import TEACHING_KINDS

            if (
                self.fact_kind not in TEACHING_KINDS
                or self.operation != "cancel"
                or self.cancel_from.utcoffset() is None
            ):
                raise self._constraint_error("education_cancel_from")
        if self.fact_kind == "student_school_relationship" and self.operation == "correct":
            if (
                not self.target_ref
                or self.creation_ref
                or any(
                    k in self.fields
                    for k in ("school_id", "source_key", "expected_version", "actor", "scope_id")
                )
            ):
                raise self._constraint_error("education_relationship_correction")
            return self
        return cast(Self, cast(Callable[[], FactChangeInput], super().target)())


class EducationBatchInput(EducationChangeInput):
    safe_constraint_messages: ClassVar[Mapping[str, str]] = MappingProxyType(
        {
            **EducationChangeInput.safe_constraint_messages,
            "education_cancel_reschedule": "cancel_from与reschedule_from不能同时填写。",
            "education_duplicate_target": (
                "同一批次不能对同一事实类型的同一对象重复编排；请合并该对象的修改。"
            ),
            "education_batch_family": (
                "教育批量维护只接受教育事实、在校关系、事实质量或制度适用范围。"
            ),
        }
    )
    reschedule_from: datetime | None = None
    additional_changes: tuple[EducationChangeInput, ...] = Field(
        default=(),
        max_length=299,
        description=(
            "同一次学校正式变更涉及的其他对象；每项独立提供fact_kind、目标或创建引用、fields和reason。"
            "同一对象不能重复编排。整批预检、确认和执行，不逐条提交。"
        ),
    )
    purpose: Literal[
        "record",
        "transfer",
        "graduation",
        "close_class",
        "replace_assignment",
        "cancel_future",
        "historical_correction",
    ] = "record"
    corrected_purpose: (
        Literal["transfer", "graduation", "close_class", "replace_assignment"] | None
    ) = None
    student_ref: str | None = None
    old_class_ref: str | None = None
    replace_refs: tuple[str, ...] = Field(default=(), max_length=100)

    import_rows: tuple[dict[str, JsonValue], ...] | None = Field(default=None, max_length=300)
    import_mode: Literal["incremental", "replace"] = "incremental"
    import_target_refs: tuple[str, ...] = Field(default=(), max_length=300)
    import_replacement_refs: tuple[str, ...] | None = Field(default=None, max_length=300)

    @model_validator(mode="after")
    def bounded_batch(self):
        if self.cancel_from is not None:
            from app.domains.students.teaching_contracts import TEACHING_KINDS

            if (
                self.fact_kind not in TEACHING_KINDS
                or self.operation != "cancel"
                or self.cancel_from.utcoffset() is None
            ):
                raise self._constraint_error("education_cancel_from")
            if self.reschedule_from is not None:
                raise self._constraint_error("education_cancel_reschedule")
        keys = [
            (c.fact_kind, c.target_ref or c.creation_ref) for c in (self, *self.additional_changes)
        ]
        if len(set(keys)) != len(keys):
            raise self._constraint_error("education_duplicate_target")
        if any(
            c.fact_kind
            not in EDUCATION_CONTENT_FIELDS
            | {"student_school_relationship": (), "fact_quality": (), "policy_scope": ()}
            for c in (self, *self.additional_changes)
        ):
            raise self._constraint_error("education_batch_family")
        return self


EDUCATION_LABELS = {
    "teaching_subject": "正式学科",
    "teaching_course": "课程",
    "course_subject": "课程学科",
    "teaching_offering": "持续开课",
    "offering_member_source": "开课名单来源",
    "teaching_group": "独立教学群体",
    "teaching_group_member": "独立教学成员",
    "teaching_assignment": "持续任课",
    "teaching_organization_scope": "教研组织适用范围",
    "education_stage": "学段",
    "education_offering": "学校开设学段",
    "education_grade": "年级",
    "education_calendar_rule": "学年与升年规则",
    "education_term": "学期",
    "education_grade_group": "年级组织",
    "education_cohort": "届别与入学批次",
    "student_cohort_membership": "学生届别与批次",
    "education_class": "行政班",
    "class_placement": "班级年级、校区与班序",
    "class_lineage": "重新组班来历",
    "student_grade_placement": "学生年级起点与例外",
    "student_class_membership": "学生归班",
    "class_teacher_assignment": "班主任安排",
    "class_location": "固定教室",
    "class_position_definition": "班内岗位规则",
    "student_class_position": "学生班内职务",
}
REFERENCE_KINDS = {
    **EDUCATION_REFERENCES,
    "student_source_key": "student",
    "campus_source_key": "location",
    "location_source_key": "location",
    "organization_source_key": "organization",
    "assignment_source_key": "assignment",
    "grade_source_keys": "education_grade",
    "stage_source_keys": "education_stage",
    "subject_source_keys": "teaching_subject",
}
FIELD_LABELS = {
    "subject_ref": "学科",
    "course_ref": "课程",
    "offering_ref": "开课",
    "group_ref": "教学群体",
    "term_ref": "学期",
    "grade_group_ref": "学年年级",
    "stage_refs": "适用学段",
    "subject_refs": "任教学科分工",
    "source_kind": "正式名单来源",
    "offering_status": "开课状态",
    "teaching_role": "任教角色",
    "teaching_role_limits": "任教岗位人数上限",
    "group_mode": "名单共享方式",
    "scope_basis": "组织适用口径",
    "display_name": "正式名称",
    "business_code": "学校内部编号",
    "short_name": "正式简称",
    "stage_ref": "学段",
    "grade_ref": "年级",
    "calendar_ref": "学年规则",
    "cohort_ref": "届别或入学批次",
    "class_ref": "班级",
    "predecessor_class_ref": "原班级",
    "position_ref": "班内岗位",
    "school_relationship_ref": "在校关系",
    "grade_refs": "年级递进顺序",
    "student_ref": "学生",
    "campus_ref": "校区",
    "location_ref": "地点",
    "organization_ref": "组织",
    "assignment_ref": "教职工任职",
    "grade_order": "年级顺序",
    "start_cycle": "开始学年",
    "end_cycle": "截止学年（不含）",
    "academic_year": "所属学年",
    "month": "学年切换月份",
    "day": "学年切换日期",
    "timezone_name": "学校时区",
    "class_number": "班序",
    "cohort_kind": "批次口径",
    "membership_status": "归班情况",
    "teacher_role": "班主任角色",
    "duty_scope": "负责事项",
    "location_purpose": "地点用途",
    "lineage_kind": "组班关系",
    "allow_multiple": "允许多人担任",
    "allow_concurrent": "允许兼任",
    "valid_to": "结束时间（可不填）",
    "basis_reference": "学校登记依据",
    "teacher_role_limits": "各班主任角色人数上限",
    "teacher_mode": "班主任安排方式",
    "primary_classroom_limit": "主要教室数量上限",
    "max_holders": "岗位人数上限",
    "acting_for_role": "代任的岗位角色",
}
ENUM_LABELS = {
    "administrative_class": "引用行政班名单",
    "independent_group": "独立维护正式名单",
    "active": "正常开课",
    "lead": "主讲",
    "co_teacher": "共同任教",
    "exclusive": "单课专属",
    "shared": "学校声明共享",
    "stage": "学段",
    "fixed_grade": "固定年级",
    "academic_grade": "学年年级",
    "original_intake": "原学段入学批次",
    "school_transfer_intake": "转入本校批次",
    "expected_graduation": "预计毕业分组",
    "assigned": "已分班",
    "unassigned": "尚未分班",
    "primary": "班主任",
    "deputy": "副班主任",
    "joint": "共同班主任",
    "acting": "临时代任",
    "main_classroom": "主要固定教室",
    "other_fixed": "其他固定场所",
    "regrouped_from": "重新组班",
    "split_from": "原班拆分",
    "merged_from": "原班合并",
    "single_primary": "主班主任与副班主任",
    "enrolled": "在读",
    "suspended": "休学",
    "graduated": "已毕业",
    "transferred_out": "已转出",
}


def public_name(field: str) -> str:
    if field.endswith("_source_keys"):
        return field.removesuffix("_source_keys") + "_refs"
    return field.removesuffix("_source_key") + "_ref" if field.endswith("_source_key") else field


def education_field_label(name: str, fact_kind: str | None) -> str:
    if fact_kind == "teaching_assignment" and name == "assignment_ref":
        return "任课教师"
    return FIELD_LABELS.get(name, name)


def education_field_descriptor(name: str, fact_kind: str) -> dict[str, JsonValue]:
    allowed, required = EDUCATION_FIELDS[fact_kind]
    canonical = next((field for field in allowed if public_name(field) == name), None)
    if canonical is None:
        raise ValueError("该资料不支持这个字段。")
    descriptor: dict[str, JsonValue] = {
        "name": name,
        "label": education_field_label(name, fact_kind),
        "required": canonical in required,
        "options": [],
        "reference_kind": None,
    }
    if canonical in REFERENCE_KINDS:
        descriptor.update(
            type="references" if canonical.endswith("_source_keys") else "reference",
            reference_kind=REFERENCE_KINDS[canonical],
        )
        return descriptor
    schema = CanonicalFactContentV3.model_json_schema()["properties"][canonical]
    variants = schema.get("anyOf", [schema])
    effective = next(
        (cast(dict[str, Any], item) for item in variants if item.get("type") != "null"),
        cast(dict[str, Any], {}),
    )
    kind = effective.get("type", "string")
    descriptor["type"] = {
        "integer": "number",
        "boolean": "boolean",
        "object": "object",
        "array": "list",
    }.get(kind, "datetime" if effective.get("format") == "date-time" else "text")
    if canonical in {"teacher_role_limits", "teaching_role_limits"}:
        roles = (
            ("lead", "co_teacher")
            if canonical == "teaching_role_limits"
            else ("primary", "deputy", "joint", "acting")
        )
        descriptor["options"] = [{"value": role, "label": ENUM_LABELS[role]} for role in roles]
    if "enum" in effective:
        descriptor["type"] = "select"
        descriptor["options"] = [
            {
                "value": value,
                "label": "暂停开课"
                if fact_kind == "teaching_offering" and value == "suspended"
                else ENUM_LABELS.get(value, value),
            }
            for value in effective["enum"]
        ]
    return descriptor


def _at(value: object) -> datetime | None:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")) if value else None


class EducationProductAdapter:
    def __init__(self, *, repository: Any, external: Any = None) -> None:
        self.repository, self.external = repository, external

    def _reference(
        self,
        actor: StaffActorContext,
        kind: str,
        source: str,
        *,
        creation_references: dict[tuple[str, str], str] | None = None,
    ) -> str:
        owner = self.repository if kind in EDUCATION_CONTENT_FIELDS else self.external
        if owner is None:
            raise ValueError("关联资料读取暂不可用。")
        expected = (creation_references or {}).get((kind, source))
        if expected is not None:
            reference = owner.reference_for(
                actor=actor, fact_kind=kind, source_key=source, creation=True
            )
            if reference != expected:
                raise PermissionError("本批创建引用与当前获准对象不符。")
            return reference
        return owner.reference_for(actor=actor, fact_kind=kind, source_key=source)

    def _source(self, actor: StaffActorContext, kind: str, reference: str) -> str:
        owner = self.repository if kind in EDUCATION_CONTENT_FIELDS else self.external
        if owner is None:
            raise ValueError("关联资料读取暂不可用。")
        coordinates = school_scope_reference_coordinates(reference)
        creation = reference.startswith("f4create:") or (
            coordinates is not None and coordinates[2]
        )
        return owner.source_for_reference(
            actor=actor,
            fact_kind=kind,
            reference=reference,
            creation=creation,
        )

    def resolve_reference(self, actor: StaffActorContext, kind: str, reference: str) -> str:
        return self._source(actor, kind, reference)

    def resolve_fields(
        self, actor: StaffActorContext, kind: str, fields: dict[str, JsonValue]
    ) -> dict[str, JsonValue]:
        allowed = {public_name(field): field for field in EDUCATION_CONTENT_FIELDS[kind]}
        if not set(fields) <= set(allowed):
            raise ValueError("维护字段不属于这类教育资料。")
        result: dict[str, JsonValue] = {}
        for name, value in fields.items():
            canonical = allowed[name]
            if canonical in REFERENCE_KINDS and value is not None:
                if canonical.endswith("_source_keys"):
                    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                        raise ValueError("年级顺序必须选择正式年级。")
                    value = [self._source(actor, REFERENCE_KINDS[canonical], str(v)) for v in value]
                else:
                    if not isinstance(value, str):
                        raise ValueError("请选择正式关联对象。")
                    value = self._source(actor, REFERENCE_KINDS[canonical], value)
            result[canonical] = cast(JsonValue, value)
        return result

    def public_fields(
        self,
        actor: StaffActorContext,
        fields: dict[str, JsonValue],
        *,
        creation_references: dict[tuple[str, str], str] | None = None,
    ) -> dict[str, JsonValue]:
        result: dict[str, JsonValue] = {}
        for key, value in fields.items():
            if key in REFERENCE_KINDS and value is not None:
                if key.endswith("_source_keys"):
                    value = [
                        self._reference(
                            actor,
                            REFERENCE_KINDS[key],
                            str(v),
                            creation_references=creation_references,
                        )
                        for v in cast(list[JsonValue], value)
                    ]
                else:
                    value = self._reference(
                        actor,
                        REFERENCE_KINDS[key],
                        str(value),
                        creation_references=creation_references,
                    )
            result[public_name(key)] = cast(JsonValue, value)
        return result

    def describe(self, *, actor: StaffActorContext, fact_kind: str) -> dict[str, JsonValue]:
        if fact_kind not in EDUCATION_CONTENT_FIELDS:
            raise ValueError("尚未接入这类教育资料。")
        choices: list[JsonValue] = []
        configuration = getattr(self.repository, "configuration", None) or getattr(
            getattr(self.repository, "authority", None), "configuration", None
        )
        if configuration is not None:
            catalog = configuration.creation_catalog.get(fact_kind, {})
            references = self.repository.catalog_creation_references(
                actor, fact_kind, tuple(catalog)
            )
            choices.extend(
                {
                    "label": catalog.get(key, EDUCATION_LABELS[fact_kind] + "新登记项"),
                    "creation_ref": reference,
                }
                for key, reference in references
            )
        return {
            "kind": "fact_fields",
            "fact_kind": fact_kind,
            "label": EDUCATION_LABELS[fact_kind],
            "fields": [
                education_field_descriptor(public_name(field), fact_kind)
                for field in sorted(EDUCATION_CONTENT_FIELDS[fact_kind])
            ],
            "creation_choices": choices,
            "input_semantics": {
                "required": "required约束最终完整内容，不要求已有对象每次更新重传所有必填字段。",
                "existing_upsert": (
                    "已有对象在生效时点有原资料时，upsert仅传修改字段；省略字段沿用原值。"
                ),
                "creation_or_no_original": (
                    "创建须提供完整资料；无原资料的时点不能普通upsert延续，"
                    "须明确完整补正或未来安排。"
                ),
                "null": "null是显式赋值，不等于省略；是否允许仍须通过字段及关系校验。",
            },
        }

    def resolve_change(
        self,
        *,
        actor: StaffActorContext,
        payload: FactChangeInput,
        request_id: UUID,
        effective_at: datetime,
        creation_references: dict[tuple[str, str], str] | None = None,
    ) -> ResolvedFactChange:
        kind = str(payload.fact_kind)
        if kind not in EDUCATION_CONTENT_FIELDS:
            raise ValueError("该维护入口只处理教育资料。")
        source = self.repository.source_for_reference(
            actor=actor,
            fact_kind=kind,
            reference=payload.target_ref or payload.creation_ref,
            creation=payload.creation_ref is not None,
        )
        result = self.repository.read_records(
            actor=actor,
            kind=kind,
            fields=tuple(sorted(EDUCATION_CONTENT_FIELDS[kind])),
            source_keys=(source,),
            as_of=effective_at,
            action="maintain" if payload.creation_ref else "read",
            creation=bool(payload.creation_ref),
        )
        if len(result.records) != 1:
            raise ValueError("修改对象当前无法核实。")
        target = result.records[0]
        version = target["version"]
        if (version == 0) != (payload.creation_ref is not None):
            raise ValueError("对象已改变，请重新选择。")
        segments: list[dict[str, Any]] = []
        for segment in target.get("segments", ()):
            start, end = _at(segment["effective_at"]), _at(segment.get("valid_to"))
            if start is None:
                raise ValueError("正式资料缺少生效时间。")
            if start <= effective_at and (end is None or effective_at < end):
                segments.append(segment)
        if len(segments) > 1:
            raise ValueError("该时点的正式资料存在冲突，不能选取其中一条修改。")
        before = dict(segments[0]["content"]) if segments else {}
        if not before and version and payload.operation == "upsert":
            raise ValueError("该时点没有可延续的原资料；请明确完整补正或未来安排。")
        merged = {**before, **self.resolve_fields(actor, kind, payload.fields)}
        if kind == "offering_member_source" and "source_kind" in payload.fields:
            merged.pop(
                "class_source_key"
                if merged["source_kind"] == "independent_group"
                else "group_source_key",
                None,
            )
        if kind == "teaching_organization_scope" and "scope_basis" in payload.fields:
            selected = {
                "stage": "stage_source_key",
                "fixed_grade": "grade_source_key",
                "academic_grade": "grade_group_source_key",
            }[str(merged["scope_basis"])]
            for field in {"stage_source_key", "grade_source_key", "grade_group_source_key"} - {
                selected
            }:
                merged.pop(field, None)
        content = merged if payload.operation in {"upsert", "correct"} else None
        if content is None and payload.fields:
            raise ValueError("结束和撤销不接受替换字段。")
        material = {
            "request": str(request_id),
            "payload": payload.model_dump(mode="json"),
            "version": version,
            "effective_at": effective_at.isoformat(),
        }
        change_set = CanonicalFactChangeSetV1.model_validate_json(
            json.dumps(
                {
                    "schema_version": 3,
                    "source_sha256": hashlib.sha256(
                        json.dumps(material, sort_keys=True, ensure_ascii=False).encode()
                    ).hexdigest(),
                    "base_version": result.school_fact_version,
                    "changes": [
                        {
                            "schema_version": 3,
                            "fact_kind": kind,
                            "operation": payload.operation,
                            "source_key": source,
                            "expected_version": version,
                            "effective_at": effective_at.isoformat(),
                            "correction_reason": payload.reason
                            if payload.operation in {"correct", "cancel"}
                            else None,
                            "content": content,
                            "origin": {"sheet_code": "education_maintenance", "row_number": 1},
                        }
                    ],
                }
            )
        )
        label = str(before.get("display_name") or EDUCATION_LABELS[kind])
        labels = None
        if kind == "education_class" and version:
            from app.composition.school_fact_education_labels import class_labels

            labels = class_labels(
                repository=self.repository,
                external=self.external,
                actor=actor,
                names={source: label},
                as_of=effective_at,
                school_fact_version=result.school_fact_version,
            )
            label = labels.labels[source]
            labels.revalidate(actor=actor)

        def revalidate_summary(current: StaffActorContext) -> None:
            result.proof.revalidate(actor=current)
            if labels is not None:
                labels.revalidate(actor=current)

        resolved = ResolvedFactChange(
            change_set=change_set,
            summary=FactChangeSummary(
                fact_kind=payload.fact_kind,
                label=label,
                reference=payload.target_ref or str(payload.creation_ref),
                operation=payload.operation,
                before=self.public_fields(actor, before) if version else None,
                after=self.public_fields(actor, merged, creation_references=creation_references)
                if content is not None
                else None,
                effective_at=effective_at,
                reason=payload.reason,
                source_label="学校正式登记资料",
            ),
            summary_revalidate=revalidate_summary,
        )
        result.proof.revalidate(actor=actor)
        return resolved


__all__ = [
    "EDUCATION_LABELS",
    "ENUM_LABELS",
    "FIELD_LABELS",
    "REFERENCE_KINDS",
    "EducationProductAdapter",
    "education_field_descriptor",
    "public_name",
]
