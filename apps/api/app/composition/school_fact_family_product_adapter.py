"""F3 maintenance adapter backed by an authorized narrow repository."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol, cast
from uuid import UUID

from pydantic import JsonValue

from app.composition.school_fact_directory_product import (
    FactChangeInput,
    FactChangeSummary,
    ResolvedFactChange,
)
from app.composition.school_fact_family_authority import FamilyFactAuthorityDenied
from app.platform.contracts import StaffActorContext
from app.platform.school_facts.application import SchoolFactChangeRejected
from app.platform.school_facts.contracts import (
    CanonicalFactChangeSetV1,
    CanonicalFactChangeV1,
    CanonicalFactContentV1,
    FactOriginV1,
    SchoolFactKind,
    fact_content_enums,
    fact_content_fields,
)
from app.platform.school_facts.f3_policy import F3_FACT_FIELDS, F3_FACT_KINDS
from app.platform.school_facts.f7_coverage_basis import verify_coverage_basis


@dataclass(frozen=True, slots=True)
class FamilyMaintenanceTarget:
    fact_kind: str
    source_key: str
    expected_version: int
    school_fact_version: int
    content: Mapping[str, JsonValue]
    label: str
    reference: str


class FamilyMaintenanceRepository(Protocol):
    def prepare_target(
        self,
        *,
        actor: StaffActorContext,
        fact_kind: str,
        target_ref: str | None,
        creation_ref: str | None,
    ) -> FamilyMaintenanceTarget: ...
    def resolve_field_references(
        self, *, actor: StaffActorContext, fact_kind: str, fields: Mapping[str, JsonValue]
    ) -> Mapping[str, JsonValue]: ...
    def public_field_references(
        self, *, actor: StaffActorContext, fact_kind: str, fields: Mapping[str, JsonValue]
    ) -> Mapping[str, JsonValue]: ...
    def creation_choices(
        self, *, actor: StaffActorContext, fact_kind: str
    ) -> tuple[Mapping[str, JsonValue], ...]: ...
    def reference_for(
        self, *, actor: StaffActorContext, fact_kind: str, source_key: str
    ) -> str: ...


class FamilyStaffQueryRepository(FamilyMaintenanceRepository, Protocol):
    def query(
        self,
        *,
        actor: StaffActorContext,
        fact_kind: str,
        reference: str | None = None,
        query_text: str | None = None,
        student_number: str | None = None,
        related_field: str | None = None,
        related_ref: str | None = None,
        as_of: datetime | None = None,
        limit: int = 20,
        requested_fields: tuple[str, ...] | None = None,
    ) -> dict[str, Any]: ...


class ExternalFamilyReferencePort(Protocol):
    def choices(self, *, actor: StaffActorContext, fact_kind: str) -> dict[str, JsonValue]: ...
    def resolve(self, *, actor: StaffActorContext, fact_kind: str, reference: str) -> str: ...
    def reference_for(
        self, *, actor: StaffActorContext, fact_kind: str, source_key: str
    ) -> str: ...
    def location_name(
        self, *, actor: StaffActorContext, reference: str, as_of: datetime | None
    ) -> dict[str, JsonValue]: ...


def _public_fields(fact_kind: str) -> frozenset[str]:
    return frozenset(
        field.removesuffix("_source_key") + "_ref" if field.endswith("_source_key") else field
        for field in F3_FACT_FIELDS[fact_kind]
    )


_F3_ENUM_LABELS = {
    "staff": "教职工",
    "student_contact": "学生联系人",
    "active": "有效",
    "inactive": "停用",
    "merged": "已合并",
    "enrolled": "在读",
    "suspended": "休学",
    "transferred_out": "已转出",
    "withdrawn": "已退学",
    "graduated": "已毕业",
    "day": "走读",
    "boarding": "住校",
    "draft": "草稿",
    "confirmed": "已确认",
    "rejected": "已拒绝",
    "substantive": "正式任职",
    "part_time": "兼职",
    "acting": "临时代理",
    "father": "父亲",
    "mother": "母亲",
    "grandfather": "祖父或外祖父",
    "grandmother": "祖母或外祖母",
    "other": "其他",
    "verified_guardian": "已核实监护人",
    "recognized_caregiver": "已认定照护人",
    "guardian_student_profile": "监护人学生档案",
    "caregiver_student_summary": "照护人学生摘要",
    "pending_verification": "待核验",
    "student_profile": "学生档案",
}

_F3_RELATED_FIELDS = {
    "student": {"person_ref": "person"},
    "student_school_relationship": {
        "student_ref": "student",
        "primary_campus_ref": "location",
        "administrative_campus_ref": "location",
    },
    "student_contact": {"student_ref": "student", "contact_person_ref": "person"},
    "guardian_relationship": {
        "guardian_person_ref": "person",
        "student_ref": "student",
        "review_of_relationship_ref": "guardian_relationship",
    },
    "person_region": {"person_ref": "person"},
    "login_mapping": {"person_ref": "person"},
    "assignment": {"person_ref": "person", "organization_ref": "organization"},
}

_F3_FIELD_LABELS = {
    "business_code": "人员编号",
    "display_name": "姓名",
    "person_kind": "人员类型",
    "lifecycle_status": "生命周期状态",
    "provider_code": "登录提供方",
    "provider_tenant_key": "提供方租户",
    "external_subject": "外部账号标识",
    "person_ref": "人员",
    "status": "状态",
    "organization_ref": "组织",
    "position_title": "岗位名称",
    "semantic_role_code": "语义角色",
    "student_ref": "学生",
    "student_number": "学号",
    "enrollment_status": "在校状态",
    "residence_mode": "住校方式",
    "primary_campus_ref": "主要校区",
    "administrative_campus_ref": "行政校区",
    "contact_person_ref": "联系人",
    "relationship_kind": "关系类型",
    "priority": "联系顺序",
    "is_emergency": "紧急联系人",
    "guardian_person_ref": "家长或监护人",
    "access_purpose": "家校访问用途",
    "verification_source": "核验来源",
    "verification_reference": "核验依据",
    "review_of_relationship_ref": "被纠正家校关系",
    "district_code": "区县代码",
    "district_name": "区县名称",
    "region_purpose": "地区用途",
    "valid_to": "有效截止时间",
}


def _schema_has_datetime(items: list[object]) -> bool:
    for item in items:
        if isinstance(item, dict):
            schema_item = cast(dict[str, object], item)
            if schema_item.get("format") == "date-time":
                return True
    return False


def _field_descriptor(name: str, fact_kind: str) -> dict[str, JsonValue]:
    canonical = name.removesuffix("_ref") + "_source_key" if name.endswith("_ref") else name
    allowed, required = fact_content_fields(
        fact_kind, schema_version=4 if fact_kind == "student_school_relationship" else 1
    )
    if canonical not in allowed:
        raise ValueError("F3 field is outside the canonical contract")
    properties = CanonicalFactContentV1.model_json_schema()["properties"]
    schema = cast(Mapping[str, object], properties[canonical])
    raw_any_of = schema.get("anyOf", ())
    any_of = cast(list[object], raw_any_of) if isinstance(raw_any_of, list) else []
    if name.endswith("_ref"):
        field_type = "reference"
        reference_kind = _F3_RELATED_FIELDS.get(fact_kind, {}).get(name)
    elif canonical in fact_content_enums(fact_kind) or canonical == "residence_mode":
        field_type, reference_kind = "select", None
    elif canonical == "valid_to" or _schema_has_datetime(any_of):
        field_type, reference_kind = "datetime", None
    elif canonical == "priority":
        field_type, reference_kind = "number", None
    elif canonical.startswith("is_"):
        field_type, reference_kind = "boolean", None
    else:
        field_type, reference_kind = "text", None
    enums = fact_content_enums(fact_kind)
    values = enums.get(
        canonical, frozenset({"day", "boarding"}) if canonical == "residence_mode" else ()
    )
    return {
        "name": name,
        "label": _F3_FIELD_LABELS.get(name, name),
        "type": field_type,
        "required": canonical in required,
        "reference_kind": reference_kind,
        "options": [{"value": value, "label": _F3_ENUM_LABELS[value]} for value in sorted(values)],
    }


def family_field_descriptor(name: str, fact_kind: str) -> dict[str, JsonValue]:
    """Expose canonical, static F3 field labels to the model-material adapter."""
    return _field_descriptor(name, fact_kind)


def _query_support(fact_kind: str) -> dict[str, JsonValue]:
    name_fields = ["display_name"] if fact_kind == "person" else []
    related_fields = [
        {
            "field": field,
            "kind": kind,
            "label": _F3_FIELD_LABELS.get(field, field),
        }
        for field, kind in sorted(_F3_RELATED_FIELDS.get(fact_kind, {}).items())
    ]
    guidance = "名称条件仅精确匹配正式姓名。" if name_fields else "该事实类型不支持名称条件。"
    if fact_kind == "student_school_relationship":
        guidance += "支持按所问时点精确匹配正式学号，须有学号字段读取权限，同号候选不能自动合并。"
    if related_fields:
        guidance += "可先取得关联对象引用，再明确选择关联字段查询；不猜测同类的多个关系。"
    guidance += "也可使用已返回的本类型对象引用，或查看当前获准集合；这些条件不增加权限。"
    return cast(
        dict[str, JsonValue],
        {
            "name_fields": name_fields,
            "name_match": "exact",
            "student_number": fact_kind == "student_school_relationship",
            "related_kinds": sorted({str(item["kind"]) for item in related_fields}),
            "related_fields": related_fields,
            "reference": True,
            "all_authorized": True,
            "related_match": "direct",
            "guidance": guidance,
        },
    )


class SchoolFactFamilyProductAdapter:
    def __init__(self, repository: FamilyMaintenanceRepository) -> None:
        self.repository = repository

    def describe(self, *, actor: StaffActorContext, fact_kind: str) -> dict[str, JsonValue]:
        if fact_kind not in F3_FACT_KINDS:
            raise ValueError("不支持的F3维护事实类别。")
        return cast(
            dict[str, JsonValue],
            {
                "kind": "fact_fields",
                "fact_kind": fact_kind,
                "label": {
                    "student": "学生基础资料",
                    "student_school_relationship": "学生在校关系",
                    "student_contact": "学生联系人",
                    "guardian_relationship": "已核实家校关系",
                    "person_region": "人员地区",
                    "person": "人员基础资料",
                    "login_mapping": "登录身份映射",
                    "assignment": "在校任职",
                }[fact_kind],
                "fields": [
                    _field_descriptor(field, fact_kind)
                    for field in sorted(_public_fields(fact_kind))
                ],
                "query_support": _query_support(fact_kind),
                "creation_choices": list(
                    self.repository.creation_choices(actor=actor, fact_kind=fact_kind)
                ),
                "notice": "引用只能选择当前获准对象；家校关系申请与核验必须由不同当前操作者完成。",
            },
        )

    def reference_for(self, *, actor: StaffActorContext, fact_kind: str, source_key: str) -> str:
        if fact_kind not in F3_FACT_KINDS:
            raise ValueError("不支持的F3维护事实类别。")
        return self.repository.reference_for(
            actor=actor, fact_kind=fact_kind, source_key=source_key
        )

    def resolve_change(
        self,
        *,
        actor: StaffActorContext,
        payload: FactChangeInput,
        request_id: UUID,
        effective_at: datetime,
    ) -> ResolvedFactChange:
        fact_kind = str(payload.fact_kind)
        if fact_kind not in F3_FACT_KINDS:
            raise ValueError("不支持的F3维护事实类别。")
        if not set(payload.fields) <= _public_fields(fact_kind):
            raise ValueError("维护字段不属于该F3事实类别。")
        target = self.repository.prepare_target(
            actor=actor,
            fact_kind=fact_kind,
            target_ref=payload.target_ref,
            creation_ref=payload.creation_ref,
        )
        if target.fact_kind != fact_kind:
            raise ValueError("维护引用与事实类别不匹配。")
        if (payload.creation_ref is not None) != (target.expected_version == 0):
            raise ValueError("创建引用与当前事实版本不匹配。")
        if payload.operation in {"upsert", "reactivate"}:
            resolved = self.repository.resolve_field_references(
                actor=actor, fact_kind=fact_kind, fields=payload.fields
            )
            if not set(resolved) <= F3_FACT_FIELDS[fact_kind]:
                raise ValueError("引用解析返回了越界字段。")
            merged = dict(target.content)
            merged.update(resolved)
            content = CanonicalFactContentV1.model_validate(merged)
        else:
            if payload.fields:
                raise ValueError("结束或撤销不接受替换字段。")
            content = None
        material = {
            "request": str(request_id),
            "payload": payload.model_dump(mode="json"),
            "effective_at": effective_at.isoformat(),
            "target_version": target.expected_version,
        }
        digest = hashlib.sha256(
            json.dumps(material, ensure_ascii=False, sort_keys=True).encode()
        ).hexdigest()
        change = CanonicalFactChangeV1(
            operation=payload.operation,
            fact_kind=cast(SchoolFactKind, fact_kind),
            source_key=target.source_key,
            expected_version=target.expected_version,
            effective_at=effective_at,
            origin=FactOriginV1(sheet_code="f3_maintenance", row_number=1),
            content=content,
        )
        change_set = CanonicalFactChangeSetV1(
            source_sha256=digest, base_version=target.school_fact_version, changes=(change,)
        )
        before = (
            dict(
                self.repository.public_field_references(
                    actor=actor,
                    fact_kind=fact_kind,
                    fields=target.content,
                )
            )
            if payload.target_ref is not None
            else None
        )
        after = dict(before or {})
        after.update(payload.fields)
        summary = FactChangeSummary(
            fact_kind=payload.fact_kind,
            label=target.label,
            reference=target.reference,
            operation=payload.operation,
            before=before,
            after=after if content is not None else None,
            effective_at=effective_at,
            reason=payload.reason,
            source_label="合成工程资料经正式事实维护通道发布",
        )
        return ResolvedFactChange(change_set=change_set, summary=summary)


class FamilyStaffProductQuery:
    """Map the fixed private F3 projection to the existing DirectoryWorkbench contract."""

    def __init__(
        self,
        repository: FamilyStaffQueryRepository,
        adapter: SchoolFactFamilyProductAdapter,
        external_references: ExternalFamilyReferencePort,
        *,
        quality_reader: Any = None,
    ) -> None:
        self.repository = repository
        self.adapter = adapter
        self.external_references = external_references
        self.quality_reader = quality_reader

    @property
    def evidence(self):
        return getattr(self.repository, "evidence", None)

    def describe(self, *, actor: StaffActorContext, fact_kind: str) -> dict[str, JsonValue]:
        return self.adapter.describe(actor=actor, fact_kind=fact_kind)

    def reference_choices(self, *, actor, fact_kind: str, field_name: str):
        from app.composition.school_fact_directory_product import reference_choice_kind

        kind = reference_choice_kind(self.describe(actor=actor, fact_kind=fact_kind), field_name)
        if kind in {"organization", "location"}:
            return self.external_references.choices(actor=actor, fact_kind=kind)
        return self.query(actor=actor, fact_kind=kind)

    def student_profile(
        self,
        *,
        actor: StaffActorContext,
        name: str | None,
        student_number: str | None,
        fields: tuple[str, ...],
        as_of: datetime | None,
        _allow_formal_future: bool = False,
    ) -> dict[str, JsonValue]:
        """Resolve a bounded student selection, then read only requested fact owners."""
        if name is None and student_number is None:
            raise ValueError("A formal name or student number is required")
        if any(
            value is not None and (type(value) is not str or not value.strip() or len(value) > 1000)
            for value in (name, student_number)
        ):
            raise ValueError("Invalid student selection")
        owners = {
            "name": "display_name",
            "student_number": "student_number",
            "campus": "primary_campus_ref",
            "region": "district_name",
            "enrollment_status": "enrollment_status",
            "residence_mode": "residence_mode",
        }
        if not fields or len(set(fields)) != len(fields) or not set(fields) <= owners.keys():
            raise ValueError("Invalid student profile fields")
        evidence: list[JsonValue] = []
        issues: list[JsonValue] = []
        formal_future = _allow_formal_future and as_of is not None and as_of > datetime.now(UTC)

        def read(kind: str, selected: tuple[str, ...], **selection: Any) -> list[dict[str, Any]]:
            try:
                view = self.query(
                    actor=actor,
                    fact_kind=kind,
                    as_of=None
                    if formal_future and kind != "student_school_relationship"
                    else as_of,
                    limit=10,
                    requested_fields=selected,
                    **(
                        {"_allow_formal_future": True}
                        if formal_future and kind == "student_school_relationship"
                        else {}
                    ),
                    **selection,
                )
            except PermissionError:
                issues.append({"fact_kind": kind, "status": "forbidden"})
                return []
            evidence.append(view)
            status = view["status"]
            if view["truncated"] or status not in {
                "known",
                "no_current_records",
                "object_unresolved",
            }:
                issues.append({"fact_kind": kind, "status": status, "truncated": view["truncated"]})
                return []
            return cast(list[dict[str, Any]], view["records"])

        def result(status: str, **data: JsonValue) -> dict[str, JsonValue]:
            return {
                "kind": "f3_student_profile",
                "status": "incomplete" if status == "known" and issues else status,
                "selection": {
                    "name": name,
                    "student_number": student_number,
                    "fields": list(fields),
                    "as_of": as_of.isoformat() if as_of is not None else None,
                },
                **data,
                "limitations": issues,
                "coverage": {
                    "status": "unknown",
                    "description": "仅当前获权资料，不代表现实资料完整。",
                },
                "evidence": evidence,
            }

        number_first = name is None or (formal_future and student_number is not None)
        people = read("person", ("display_name",), query_text=name) if not number_first else []
        if issues:
            return result("incomplete")
        candidates: list[tuple[dict[str, Any], dict[str, Any], dict[str, Any] | None]] = []
        school_fields = tuple(
            owners[f]
            for f in fields
            if f in {"student_number", "campus", "enrollment_status", "residence_mode"}
        )
        selection_fields = (
            ("student_number",) if student_number is not None or len(people) > 1 else school_fields
        )
        if number_first:
            # The existing fixed Reader filters authorized relationship keys at
            # the requested fact time. Never enumerate people and filter later.
            relationships = read(
                "student_school_relationship",
                ("student_number", "student_ref"),
                student_number=student_number,
            )
            for school in relationships:
                student_ref = school["fields"].get("student_ref")
                if not isinstance(student_ref, str):
                    issues.append({"fact_kind": "student", "status": "selection_unverified"})
                    continue
                students = read("student", ("person_ref",), reference=student_ref)
                if len(students) != 1:
                    issues.append({"fact_kind": "student", "status": "selection_unverified"})
                    continue
                student = students[0]
                person_ref = student["fields"].get("person_ref")
                if not isinstance(person_ref, str):
                    issues.append({"fact_kind": "person", "status": "selection_unverified"})
                    continue
                matched = read("person", ("display_name",), reference=person_ref)
                if len(matched) != 1:
                    issues.append({"fact_kind": "person", "status": "selection_unverified"})
                    continue
                if name is not None and matched[0]["fields"].get("display_name") != name:
                    continue
                candidates.append((matched[0], student, school))
        for person in people:
            students = read(
                "student",
                ("person_ref",),
                related_field="person_ref",
                related_ref=person["reference"],
            )
            if len(students) > 1:
                issues.append({"fact_kind": "student", "status": "ambiguous"})
                continue
            for student in students:
                school = None
                if selection_fields:
                    relationships = read(
                        "student_school_relationship",
                        selection_fields,
                        related_field="student_ref",
                        related_ref=student["reference"],
                    )
                    if len(relationships) > 1:
                        issues.append(
                            {"fact_kind": "student_school_relationship", "status": "ambiguous"}
                        )
                        continue
                    school = relationships[0] if relationships else None
                    if student_number is not None:
                        if school is None or "student_number" not in school["fields"]:
                            issues.append(
                                {
                                    "fact_kind": "student_school_relationship",
                                    "status": "selection_unverified",
                                }
                            )
                            continue
                        if school["fields"]["student_number"] != student_number:
                            continue
                candidates.append((person, student, school))
        if issues:
            return result("incomplete")
        if len(candidates) != 1:
            choices: list[JsonValue] = [
                {
                    "name": person["fields"].get("display_name"),
                    "student_number": school["fields"].get("student_number") if school else None,
                    "tool_context": {"reference": student["reference"]},
                }
                for person, student, school in candidates
            ]
            return result(
                "ambiguous" if candidates else "object_unresolved",
                candidates=choices,
                clarification={"accepted_fields": ["name", "student_number"]},
            )
        person, student, school = candidates[0]
        if school_fields and not set(school_fields) <= set(selection_fields):
            relationships = read(
                "student_school_relationship",
                school_fields,
                related_field="student_ref",
                related_ref=student["reference"],
            )
            if len(relationships) > 1:
                issues.append({"fact_kind": "student_school_relationship", "status": "ambiguous"})
                return result("incomplete")
            school = relationships[0] if relationships else None
        profile: dict[str, JsonValue] = {}
        sources: dict[str, JsonValue] = {}
        quality_targets: dict[str, tuple[str, str, str, datetime]] = {}
        for field in fields:
            record = person if field == "name" else school
            if field == "region":
                regions = read(
                    "person_region",
                    ("district_name",),
                    related_field="person_ref",
                    related_ref=person["reference"],
                )
                if len(regions) > 1:
                    profile[field] = {"status": "ambiguous"}
                    issues.append({"fact_kind": "person_region", "status": "ambiguous"})
                    continue
                record = regions[0] if regions else None
            if record is None:
                owner_kind = "person_region" if field == "region" else "student_school_relationship"
                statuses = [
                    issue["status"]
                    for issue in issues
                    if isinstance(issue, dict) and issue.get("fact_kind") == owner_kind
                ]
                profile[field] = {"status": statuses[-1] if statuses else "no_current_records"}
                continue
            values = record["fields"]
            key = owners[field]
            if key.endswith("_ref") and key not in values:
                canonical = key.removesuffix("_ref") + "_source_key"
                if canonical in values and values[canonical] is None:
                    values = {**values, key: None}
            if key not in values:
                profile[field] = {"status": "not_returned"}
                issues.append({"field": field, "status": "not_returned"})
                continue
            value = values[key]
            sources[field] = record["source"]
            quality_field = (
                "primary_campus_source_key" if field == "campus" else key
            )
            if field != "name" and self.quality_reader is not None:
                quality_targets[field] = (
                    "person_region" if field == "region" else "student_school_relationship",
                    cast(str, record["reference"]),
                    quality_field,
                    as_of or datetime.fromisoformat(cast(str, evidence[-1]["queried_at"])),
                )
            if field == "campus" and value is not None:
                # F2 owns location names. Its separate proof cannot be represented by
                # the F3 capture alone; preserve complete-query reread until composed.
                if self.evidence is not None:
                    self.evidence.unsupported()
                location = self.external_references.location_name(
                    actor=actor,
                    reference=value,
                    as_of=as_of,
                )
                evidence.append(location)
                profile[field] = {"status": location["status"], "value": location.get("name")}
                if location["status"] != "known":
                    issues.append({"field": field, "status": location["status"]})
                if "source" in location:
                    sources[field] = {
                        "relationship": record["source"],
                        "location": location["source"],
                    }
            else:
                profile[field] = {
                    "status": "not_recorded" if value is None else "known",
                    "value": _F3_ENUM_LABELS.get(value, value)
                    if isinstance(value, str) and field in {"enrollment_status", "residence_mode"}
                    else value,
                }
        quality: dict[str, JsonValue] = {}
        grouped_targets: dict[
            tuple[str, str, datetime], list[tuple[str, str]]
        ] = {}
        for field, (kind, reference, selected, factual_at) in quality_targets.items():
            grouped_targets.setdefault((kind, reference, factual_at), []).append(
                (field, selected)
            )
        for (kind, reference, factual_at), selections in grouped_targets.items():
            requested_names = tuple(dict.fromkeys(name for _, name in selections))
            try:
                resolved_kind, source_key = self.repository.source_for_reference(
                    actor, reference
                )
                if resolved_kind != kind:
                    raise ValueError("Student profile quality target changed.")
                current = self.quality_reader.query_quality_for_target(
                    actor=actor,
                    target_kind=kind,
                    target_key=source_key,
                    field_names=requested_names,
                    as_of=factual_at,
                )
            except (PermissionError, FamilyFactAuthorityDenied, SchoolFactChangeRejected):
                for field, _ in selections:
                    quality[field] = {"status": "not_authorized", "risks": []}
                continue
            for claim in current.records:
                if claim.get("field_name") not in (None, *requested_names):
                    raise ValueError("Student profile quality exceeded requested field.")
            for field, selected in selections:
                risks: list[str] = []
                details: list[JsonValue] = []
                for claim in current.records:
                    if claim.get("field_name") not in (None, selected):
                        continue
                    category = claim.get("quality_kind")
                    if category == "dispute" and claim.get("dispute_status") == "open":
                        risks.append("学校登记了这项资料的争议")
                    elif category == "source" and claim.get("source_status") == "unavailable":
                        risks.append("这项资料的来源目前无法核实")
                    elif category == "coverage" and claim.get("coverage_status") == "partial":
                        risks.append("学校声明这项资料登记未覆盖完整范围")
                    elif category == "review" and isinstance(claim.get("review_due_at"), str):
                        try:
                            due = datetime.fromisoformat(cast(str, claim["review_due_at"]))
                        except ValueError:
                            due = None
                        if due is not None and due.utcoffset() is not None and due <= factual_at:
                            risks.append("这项资料已超过登记的复核时间")
                    details.append({
                        key: value for key, value in claim.items()
                        if key not in {"source_key", "owner_source_key", "coverage_basis"}
                    })
                quality[field] = {
                    "status": "claims" if details else "no_current_claims",
                    "risks": list(dict.fromkeys(risks)),
                    "details": details,
                }
        return result(
            "known",
            profile=profile,
            sources=sources,
            tool_context={"person_ref": person["reference"], "student_ref": student["reference"]},
            **({"quality": quality} if quality else {}),
        )

    def student_eligibility_profile(self, **selection: Any) -> dict[str, JsonValue]:
        """Current identity labels identify a student; SSR facts use their requested time."""
        return self.student_profile(**selection, _allow_formal_future=True)

    def query_eligibility(self, **selection: Any) -> dict[str, JsonValue]:
        """Explicit F6 dependency port; ordinary F3 tool inputs cannot select it."""
        return self.query(**selection, _allow_formal_future=True)

    def query(
        self,
        *,
        actor: StaffActorContext,
        fact_kind: str,
        reference: str | None = None,
        query_text: str | None = None,
        student_number: str | None = None,
        related_field: str | None = None,
        related_ref: str | None = None,
        as_of: datetime | None = None,
        limit: int = 20,
        requested_fields: tuple[str, ...] | None = None,
        include_quality: bool = False,
        _allow_formal_future: bool = False,
    ) -> dict[str, JsonValue]:
        selection: dict[str, Any] = {}
        if _allow_formal_future:
            if fact_kind != "student_school_relationship":
                raise ValueError("Future eligibility belongs to student school relationships")
            selection["_allow_formal_future"] = True
        if student_number is not None:
            selection["student_number"] = student_number
        if requested_fields is not None:
            selection["requested_fields"] = tuple(
                field.removesuffix("_ref") + "_source_key" if field.endswith("_ref") else field
                for field in requested_fields
            )
        raw = self.repository.query(
            actor=actor,
            fact_kind=fact_kind,
            reference=reference,
            query_text=query_text,
            related_field=related_field,
            related_ref=related_ref,
            as_of=as_of,
            limit=limit,
            **selection,
        )
        queried_at = str(raw["queried_at"])
        records: list[JsonValue] = []
        for raw_item in raw["records"]:
            item = cast(dict[str, Any], raw_item)
            content = dict(item["content"])
            fields: dict[str, JsonValue] = {}
            for name, value in content.items():
                if name.endswith("_source_key") and value is not None:
                    public = name.removesuffix("_source_key") + "_ref"
                    target_kind = _field_descriptor(public, fact_kind)["reference_kind"]
                    if not isinstance(target_kind, str) or not isinstance(value, str):
                        raise ValueError("Invalid private F3 relation projection")
                    reference_port = (
                        self.external_references
                        if target_kind in {"location", "organization"}
                        else self.repository
                    )
                    value = reference_port.reference_for(
                        actor=actor, fact_kind=target_kind, source_key=value
                    )
                    name = public
                fields[name] = cast(JsonValue, value)
            valid_from = str(item["valid_from"])
            valid_to = item["valid_to"]
            source_published_at = item["source_published_at"]
            source_name = item.get("source_name")
            source_reference = item["source_reference"]
            source_trace_reference = item.get("source_trace_reference")
            if source_name is not None and (
                not isinstance(source_name, str) or not source_name.strip()
            ):
                raise ValueError("Invalid F3 source name")
            if not isinstance(source_reference, str) or not source_reference:
                raise ValueError("Invalid F3 source reference")
            if source_trace_reference is not None and (
                not isinstance(source_trace_reference, str) or not source_trace_reference
            ):
                raise ValueError("Invalid F3 source trace reference")
            source_label = source_name or "学校事实正式发布"
            records.append(
                {
                    "reference": item["reference"],
                    "fact_kind": fact_kind,
                    "label": item["label"],
                    "fields": fields,
                    "version": item["version"],
                    "valid_from": valid_from,
                    "valid_to": valid_to,
                    "source": {
                        "label": source_label,
                        "reference": source_reference,
                        "trace_reference": source_trace_reference,
                        "published_at": source_published_at,
                    },
                }
            )
        view: dict[str, JsonValue] = {
            "kind": "directory_facts",
            "status": raw["status"],
            "fact_kind": fact_kind,
            "records": records,
            "matched_count": len(records),
            "returned_count": len(records),
            "truncated": bool(raw["truncated"]),
            "next_cursor": None,
            "queried_at": queried_at,
            "factual_at": as_of.isoformat() if as_of else queried_at,
            "coverage": {
                "status": str(raw["coverage"]),
                "description": "仅表示当前身份获准的固定F3事实范围，现实覆盖程度未知。",
            },
        }
        if include_quality:
            if self.quality_reader is None:
                view["quality"] = {"status": "source_unavailable"}
            elif len(records) != 1:
                view["quality"] = {
                    "status": "select_exact_target" if records else "no_target",
                }
            else:
                item = cast(dict[str, Any], raw["records"][0])
                kind, source = self.repository.source_for_reference(actor, item["reference"])
                if kind != fact_kind:
                    raise ValueError("Quality target changed during F3 projection.")
                names = tuple(
                    sorted(set(cast(dict[str, object], item["content"])) & F3_FACT_FIELDS[kind])
                )
                if not names:
                    view["quality"] = {"status": "no_returned_fields"}
                else:
                    try:
                        quality = self.quality_reader.query_quality_for_target(
                            actor=actor,
                            target_kind=kind,
                            target_key=source,
                            field_names=names,
                            as_of=as_of or datetime.fromisoformat(queried_at),
                        )
                    except (PermissionError, FamilyFactAuthorityDenied, SchoolFactChangeRejected):
                        view["quality"] = {"status": "not_authorized"}
                    else:
                        notes = []
                        for claim in quality.records:
                            category = claim.get("quality_kind")
                            if category == "source":
                                state = claim.get("source_status")
                                notes.append(
                                    "学校登记的来源状态为可用（非独立核实）"
                                    if state == "available"
                                    else "学校登记的来源状态需要核对"
                                )
                            elif (
                                category == "coverage"
                                and claim.get("coverage_status") == "partial"
                            ):
                                notes.append("学校声明此项资料覆盖不完整")
                            elif (
                                category == "coverage"
                                and claim.get("coverage_status") == "complete"
                            ):
                                if (
                                    type(item.get("version")) is int
                                    and type(getattr(quality, "school_fact_version", None)) is int
                                    and verify_coverage_basis(
                                        claim.get("coverage_basis"),
                                        school_id=actor.school_id,
                                        owner_kind="person",
                                        owner_source_key=str(claim.get("owner_source_key")),
                                        target_fact_kind=kind,
                                        target_source_key=source,
                                        field_name=claim.get("field_name"),
                                        purpose=None,
                                        contact_kind=None,
                                        factual_at=as_of or datetime.fromisoformat(queried_at),
                                        school_fact_version=quality.school_fact_version,
                                        members=((source, item["version"]),),
                                    )
                                ):
                                    notes.append("学校已核对该项正式登记的覆盖范围")
                            elif category == "dispute":
                                notes.append("学校登记了这项资料的争议")
                            elif category == "review":
                                notes.append("学校登记了这项资料的复核安排")
                            elif category == "correction":
                                notes.append("学校登记了这项资料的关联纠正")
                        view["quality"] = {
                            "status": "claims" if quality.records else "no_current_claims",
                            "summary": list(dict.fromkeys(notes)),
                            "details": [
                                {
                                    key: value for key, value in claim.items()
                                    if key not in {
                                        "coverage_basis", "source_key", "owner_source_key"
                                    }
                                }
                                for claim in quality.records
                            ],
                        }
        return view
