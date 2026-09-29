"""Product projection and input mapping over the authorized F2 domain Reader."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from secrets import token_urlsafe
from threading import RLock
from time import monotonic
from typing import TYPE_CHECKING, cast
from uuid import UUID

from pydantic import JsonValue

from app.composition.school_fact_directory_product import (
    FactChangeInput,
    FactChangeSummary,
    FactQueryInput,
    ResolvedFactChange,
)
from app.composition.school_fact_f2_query_repository import F2FactQueryRepository, F2PublicFact
from app.composition.school_fact_product_contacts import ContactProductUnavailable
from app.platform.assistant.contracts import JsonObject
from app.platform.assistant.diagnostics import timed
from app.platform.authorization.f2_reader_compiler import query_selector_support
from app.platform.contracts import StaffActorContext
from app.platform.school_facts.contracts import (
    F2_QUALITY_TARGET_KINDS,
    CanonicalFactChangeSetV2,
    CanonicalFactChangeSetV4,
    CanonicalFactChangeV2,
    CanonicalFactChangeV4,
    CanonicalFactContentV2,
    CanonicalFactContentV4,
    FactOriginV1,
    fact_content_enums,
    fact_content_fields,
)
from app.platform.school_facts.f2_quality import QualityFact, evaluate_quality
from app.platform.school_facts.f7_coverage_basis import (
    mother_set_digest,
    verify_coverage_basis,
)
from app.platform.school_facts.read_policy import SchoolFactAuthorizationUnavailable

if TYPE_CHECKING:
    from app.composition.school_fact_read_capture import F2EvidenceBackend

FACT_LABELS = {
    "school": "学校",
    "person": "人员",
    "organization": "组织",
    "assignment": "正式任职",
    "organization_campus_relationship": "组织与校区",
    "location": "校区与地点",
    "location_alias": "地点别名",
    "person_contact": "个人联系方式",
    "official_contact": "官方联系方式",
    "policy": "制度目录",
    "policy_version": "制度版本与原文",
    "policy_scope": "制度适用范围",
    "organization_profile": "组织职能",
    "organization_membership": "非任职成员",
    "organization_location": "组织办公与接待地点",
    "person_workplace": "人员及任职办公地点",
    "school_service": "学校服务目录",
    "fact_quality": "事实来源与质量",
}
FIELD_LABELS = {
    "display_name": "正式名称",
    "business_code": "业务编码",
    "short_name": "简称",
    "timezone_name": "时区",
    "lifecycle_status": "有效状态",
    "person_kind": "人员类别",
    "kind": "类别",
    "status": "关系状态",
    "person_ref": "人员",
    "organization_ref": "组织",
    "parent_ref": "上级对象",
    "campus_ref": "校区",
    "location_ref": "地点",
    "assignment_ref": "任职",
    "policy_ref": "制度",
    "official_contact_ref": "官方渠道",
    "publishing_organization_ref": "发布组织",
    "supersedes_policy_version_ref": "替代的制度版本",
    "position_title": "岗位名称",
    "semantic_role_code": "正式岗位语义",
    "assignment_kind": "任职性质",
    "appointment_source": "任命来源",
    "approval_reference": "批准依据",
    "display_path": "地点路径",
    "alias": "别名",
    "alias_kind": "别名用途",
    "aliases": "其他别名",
    "contact_kind": "联系类型",
    "contact_value": "联系方式",
    "is_primary": "是否正式主要项",
    "visibility_scope": "可见范围",
    "audience_code": "明确对象范围依据",
    "consent_reference": "个人联系依据",
    "owner_kind": "所属对象类型",
    "service_hours": "服务时间",
    "category_code": "制度分类",
    "classification": "正文可见级别",
    "published_at": "发布时间",
    "effective_from": "生效时间",
    "effective_to": "失效时间",
    "valid_to": "结束时间",
    "content_text": "完整制度原文",
    "version_note": "版本说明",
    "organization_type": "正式组织类型",
    "function_summary": "正式职能说明",
    "basis_reference": "正式依据",
    "membership_kind": "成员性质",
    "purpose": "地点用途",
    "audience_description": "服务对象",
    "service_summary": "服务说明",
    "quality_kind": "质量事项",
    "target_fact_kind": "对应事实类型",
    "target_ref": "对应事实对象",
    "replacement_ref": "纠正后的事实对象",
    "responsibility": "发布与核实责任",
    "verified_at": "核实时间",
    "evidence_reference": "证据依据",
    "coverage_status": "覆盖状态",
    "negative_code": "正式否定事项",
    "dispute_status": "争议状态",
    "source_status": "来源可用状态",
    "last_success_at": "最近成功取得来源时间",
    "review_due_at": "下次核实期限",
}
REFERENCE_KINDS = {
    "person_ref": "person",
    "organization_ref": "organization",
    "campus_ref": "location",
    "location_ref": "location",
    "assignment_ref": "assignment",
    "policy_ref": "policy",
    "official_contact_ref": "official_contact",
    "publishing_organization_ref": "organization",
    "supersedes_policy_version_ref": "policy_version",
}
ENUM_LABELS = {
    "active": "有效",
    "inactive": "停用",
    "staff": "教职工",
    "other": "其他",
    "public": "按正式发布范围公开",
    "internal": "校内获权身份",
    "school_authenticated": "校内获权身份",
    "private": "本人及明确获权维护者",
    "restricted": "明确授权对象",
    "declared_audience": "明确受众",
    "phone": "电话",
    "mobile": "手机",
    "email": "邮箱",
    "office": "办公",
    "reception": "接待",
    "member": "成员",
    "participant": "参与者",
    "partial": "部分覆盖",
    "complete": "完整覆盖",
    "open": "待核实",
    "resolved": "已核实处置",
    "available": "来源可用",
    "unavailable": "来源不可用",
    "source": "来源与责任",
    "coverage": "覆盖声明",
    "negative": "正式否定",
    "dispute": "争议",
    "review": "时效核实",
    "correction": "纠错依据",
}


# Identical field/value codes can name different kinds of objects. Keep those
# labels contextual while preserving the canonical field names and enum values.
FIELD_CONTEXT_LABELS = {("organization", "display_path"): "组织路径"}
ENUM_CONTEXT_LABELS = {
    ("official_contact", "owner_kind", "school"): "学校",
    ("official_contact", "owner_kind", "organization"): "组织",
    ("official_contact", "owner_kind", "location"): "地点",
    ("organization_profile", "organization_type", "department"): "部门",
    ("organization_profile", "organization_type", "grade_group"): "年级组",
    ("organization_profile", "organization_type", "teaching_research_group"): "教研组",
    ("organization_profile", "organization_type", "lesson_preparation_group"): "备课组",
    ("organization_profile", "organization_type", "committee"): "委员会",
    ("person", "person_kind", "other"): "其他人员",
    ("organization_profile", "organization_type", "other"): "其他组织",
}


# Model-facing classifications shared by query and proposal projections. They do
# not reinterpret school-defined codes or free-form supporting evidence values.
SCHOOL_DEFINED_CODE_FIELDS = frozenset(
    {"kind", "semantic_role_code", "audience_code", "category_code", "alias_kind"}
)
PROVENANCE_FIELDS = (
    "basis_reference",
    "approval_reference",
    "consent_reference",
    "evidence_reference",
)


def json_value(value: object) -> JsonValue:
    if value is None or type(value) in (str, int, float, bool):
        return cast(JsonValue, value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, (list, tuple)):
        return [json_value(item) for item in cast(list[object] | tuple[object, ...], value)]
    if isinstance(value, dict):
        return {str(k): json_value(v) for k, v in cast(dict[object, object], value).items()}
    raise ValueError("Unsupported public fact value")


def query_support_view(fact_kind: str) -> JsonObject:
    support = query_selector_support(fact_kind)
    names = cast(list[str], support["name_fields"])
    related = cast(list[str], support["related_kinds"])
    guidance = (
        "名称条件仅精确匹配："
        + "、".join(FIELD_LABELS.get(field, field) for field in names)
        + "；不匹配简称、部分名称或关联对象名称。"
        if names
        else "该事实类型不支持名称条件。"
    )
    if related:
        guidance += (
            "可先查询"
            + "、".join(FACT_LABELS[kind] for kind in related)
            + "取得获权对象引用，再查询直接关联；不包含隐含下级或间接关系。"
        )
    guidance += "也可使用已返回的本类型对象引用，或明确选择当前获准集合；这些条件不增加权限。"
    return {**cast(JsonObject, json_value(support)), "guidance": guidance}


def record_view(record: F2PublicFact) -> JsonObject:
    fields = cast(JsonObject, json_value(record.content))
    if record.fact_kind == "fact_quality":
        fields.pop("coverage_basis", None)
    label = next(
        (
            str(fields[k])
            for k in ("display_name", "position_title", "alias", "contact_value")
            if fields.get(k)
        ),
        FACT_LABELS[record.fact_kind],
    )
    return {
        "reference": record.fact_ref,
        "fact_kind": record.fact_kind,
        "label": label,
        "fields": fields,
        "version": record.version,
        "valid_from": record.valid_from.isoformat(),
        "valid_to": record.valid_to.isoformat() if record.valid_to else None,
        "source": {
            "kind": record.source_kind,
            "label": "学校事实发布记录",
            "reference": record.publication_ref,
            "published_at": record.published_at.isoformat(),
        },
    }


def _quality_fact(record: F2PublicFact) -> QualityFact:
    return QualityFact(
        record.fact_ref, record.fact_kind, record.content, record.valid_from, record.valid_to
    )


class DirectoryPageUnavailable(ContactProductUnavailable):
    """A continuation cannot be combined with its prior authorized page."""


@dataclass(frozen=True)
class _DirectoryPage:
    actor: StaffActorContext
    selection: FactQueryInput
    native_cursor: str | None
    offset: int
    fingerprint: str | None
    read_version: int | None
    total: int | None
    factual_at: datetime
    expires: float


def _page_fingerprint(records: tuple[F2PublicFact, ...]) -> str:
    return hashlib.sha256(repr(records).encode()).hexdigest()


def _page_record_size(record: F2PublicFact) -> int:
    # Mirror the existing explicit policy excerpt only for sizing. The official
    # maintenance response still receives the complete authorized body.
    value = record_view(record)
    fields = cast(JsonObject, value["fields"])
    body = fields.get("content_text")
    if isinstance(body, str):
        fields["content_text"] = body[:1200]
    return len(json.dumps(value, ensure_ascii=False).encode()) + 320


class DirectoryProductAdapter:
    def __init__(
        self, repository: F2FactQueryRepository, *, evidence: F2EvidenceBackend | None = None
    ):
        self.repository = repository
        self.evidence = evidence
        self._pages: dict[str, _DirectoryPage] = {}
        self._page_lock = RLock()

    @timed("facts")
    def query(
        self, *, actor: StaffActorContext, payload: FactQueryInput, bounded_pages: bool = True
    ) -> JsonObject:
        # Existing internal reference selectors have no public continuation API.
        # They retain their original single Reader page, bounded at 100 rows.
        if not bounded_pages and payload.cursor is not None:
            raise ValueError("Internal reference choices cannot consume a directory cursor.")
        selection = payload.model_copy(update={"cursor": None})
        with self._page_lock:
            now = monotonic()
            self._pages = {key: value for key, value in self._pages.items() if value.expires > now}
            continuation = self._pages.get(payload.cursor) if payload.cursor else None
        if payload.cursor and (
            continuation is None
            or continuation.actor != actor
            or continuation.selection != selection
        ):
            raise DirectoryPageUnavailable("目录续读已失效，请按原目标重新查询。")
        result = self.repository.query(
            actor=actor,
            fact_kind=payload.fact_kind,
            reference=payload.reference,
            name=payload.query_text,
            related_ref=payload.related_ref,
            factual_at=continuation.factual_at if continuation else payload.as_of,
            scope=payload.scope,
            cursor=continuation.native_cursor if continuation else None,
            page_size=100,
        )
        if bounded_pages:
            fingerprint = _page_fingerprint(result.records)
            if continuation and (
                result.status not in {"known", "ambiguous"}
                or result.read_version != continuation.read_version
                or result.matched_count != continuation.total
                or (
                    continuation.fingerprint is not None and fingerprint != continuation.fingerprint
                )
                or result.factual_at != continuation.factual_at
            ):
                raise DirectoryPageUnavailable("目录资料或权限已变化，请重新查询后再续读。")
            offset = continuation.offset if continuation else 0
            delivered: list[F2PublicFact] = []
            material_size = 0
            for record in result.records[offset : offset + payload.page_size]:
                size = _page_record_size(record)
                if delivered and material_size + size > 8000:
                    break
                delivered.append(record)
                material_size += size
            end = offset + len(delivered)
            next_cursor = None
            if end < len(result.records) or result.next_cursor:
                next_cursor = token_urlsafe(24)
                page = _DirectoryPage(
                    actor,
                    selection,
                    continuation.native_cursor if continuation else None,
                    end,
                    fingerprint,
                    result.read_version,
                    result.matched_count,
                    result.factual_at or result.queried_at,
                    continuation.expires if continuation else now + 600,
                )
                if end == len(result.records):
                    page = replace(
                        page, native_cursor=result.next_cursor, offset=0, fingerprint=None
                    )
                with self._page_lock:
                    if len(self._pages) >= 1024:
                        self._pages.pop(next(iter(self._pages)))
                    self._pages[next_cursor] = page
            result = replace(
                result,
                records=tuple(delivered),
                has_more=next_cursor is not None,
                next_cursor=next_cursor,
            )
        else:
            # Native cursors contain private authorization claims. This internal
            # consumer has no next-page contract; preserve has_more explicitly.
            result = replace(result, next_cursor=None)
        factual_at = payload.as_of or result.factual_at or result.queried_at
        quality_records: tuple[F2PublicFact, ...] = ()
        quality_status = "not_queried"
        quality_truncated = False
        if payload.fact_kind != "fact_quality" and result.status in {"known", "no_current_records"}:
            quality_result = self.repository.query(
                actor=actor,
                fact_kind="fact_quality",
                quality_target_kind=payload.fact_kind,
                factual_at=factual_at,
                scope="all_authorized",
            )
            quality_records = quality_result.records
            quality_status = quality_result.status
            quality_truncated = quality_result.has_more
        verified_complete_refs: set[str] = set()
        if (
            payload.fact_kind == "organization_membership"
            and payload.related_ref is not None
            and payload.reference is None
            and payload.query_text is None
            and not result.has_more
            and result.matched_count == len(result.records)
            and quality_status == "known"
            and not quality_truncated
        ):
            owner_key = self.repository.source_for(
                actor=actor, fact_kind="organization", reference=payload.related_ref
            )
            members = tuple(
                (
                    self.repository.source_for(
                        actor=actor,
                        fact_kind="organization_membership",
                        reference=row.fact_ref,
                    ),
                    row.version,
                )
                for row in result.records
            )
            for row in quality_records:
                content = row.content
                if (
                    content.get("quality_kind") == "coverage"
                    and content.get("coverage_status") == "complete"
                    and content.get("organization_ref") == payload.related_ref
                    and type(quality_result.read_version) is int
                    and result.read_version in {None, quality_result.read_version}
                    and verify_coverage_basis(
                        content.get("coverage_basis"),
                        school_id=actor.school_id,
                        owner_kind="organization",
                        owner_source_key=owner_key,
                        target_fact_kind="organization_membership",
                        target_source_key=None,
                        field_name=None,
                        purpose=None,
                        contact_kind=None,
                        factual_at=factual_at,
                        school_fact_version=quality_result.read_version,
                        members=members,
                    )
                ):
                    verified_complete_refs.add(row.fact_ref)
        if (
            payload.fact_kind in F2_QUALITY_TARGET_KINDS
            and len(result.records) == 1
            and not result.has_more
            and result.status == "known"
            and (payload.reference == result.records[0].fact_ref or payload.query_text is not None)
            and quality_status == "known"
            and not quality_truncated
            and type(quality_result.read_version) is int
            and result.read_version in {None, quality_result.read_version}
        ):
            selected = result.records[0]
            selected_key = self.repository.source_for(
                actor=actor, fact_kind=payload.fact_kind, reference=selected.fact_ref
            )
            owner_ref_field = {
                "school": "school_ref",
                "organization": "organization_ref",
                "person": "person_ref",
                "location": "location_ref",
            }
            for row in quality_records:
                content = row.content
                owner_kind = content.get("owner_kind")
                if (
                    content.get("quality_kind") != "coverage"
                    or content.get("coverage_status") != "complete"
                    or content.get("target_fact_kind") != payload.fact_kind
                    or content.get("target_ref") != selected.fact_ref
                    or owner_kind not in owner_ref_field
                ):
                    continue
                owner_ref = content.get(owner_ref_field[owner_kind])
                if owner_kind == "school":
                    owner_key = None
                elif isinstance(owner_ref, str):
                    owner_key = self.repository.source_for(
                        actor=actor, fact_kind=owner_kind, reference=owner_ref
                    )
                else:
                    continue
                verified = verify_coverage_basis(
                    content.get("coverage_basis"),
                    school_id=actor.school_id,
                    owner_kind=owner_kind,
                    owner_source_key=owner_key,
                    target_fact_kind=payload.fact_kind,
                    target_source_key=selected_key,
                    field_name=None,
                    purpose=content.get("purpose"),
                    contact_kind=content.get("contact_kind"),
                    factual_at=factual_at,
                    school_fact_version=quality_result.read_version,
                    members=((selected_key, selected.version),),
                )
                if verified:
                    verified_complete_refs.add(row.fact_ref)
        quality = evaluate_quality(
            fact_kind=payload.fact_kind,
            reference=payload.reference,
            related_ref=payload.related_ref,
            name=payload.query_text,
            factual_at=factual_at,
            records=tuple(_quality_fact(row) for row in result.records),
            statements=tuple(_quality_fact(row) for row in quality_records),
            query_status=result.status,
            evidence_status=quality_status,
            evidence_truncated=quality_truncated,
            records_truncated=result.has_more,
            verified_complete_claim_refs=frozenset(verified_complete_refs),
        )
        status = "denied" if result.status == "forbidden" else result.status
        if quality["existence"] == "formal_negative":
            status = "formal_negative"
        return {
            "kind": "directory_facts",
            "status": status,
            "fact_kind": payload.fact_kind,
            "records": [record_view(record) for record in result.records],
            "matched_count": result.matched_count,
            "returned_count": len(result.records),
            "truncated": result.has_more,
            "next_cursor": result.next_cursor,
            "queried_at": datetime.now(UTC).isoformat(),
            "factual_at": factual_at.isoformat(),
            "query": payload.model_dump(mode="json"),
            "query_support": query_support_view(payload.fact_kind),
            "coverage": {"status": quality["coverage"], "description": result.query_scope},
            "quality": quality,
        }

    def require_query(self, *, actor: StaffActorContext, payload: FactQueryInput) -> None:
        result = self.repository.query(
            actor=actor,
            fact_kind=payload.fact_kind,
            reference=payload.reference,
            name=payload.query_text,
            related_ref=payload.related_ref,
            factual_at=payload.as_of,
            scope=payload.scope,
            page_size=1,
        )
        if result.status == "unsupported_selection":
            raise ValueError("Unsupported fact query selection")
        if result.status == "forbidden":
            raise PermissionError("Current fact access denied")
        if result.status == "source_unavailable":
            raise SchoolFactAuthorizationUnavailable("Current fact source unavailable")

    @timed("scope")
    def access(
        self,
        *,
        actor: StaffActorContext,
        fact_kind: str | None = None,
        purpose: str = "read",
        target_ref: str | None = None,
        fields: tuple[str, ...] = (),
    ) -> JsonObject:
        result = self.repository.access(
            actor=actor, fact_kind=fact_kind, purpose=purpose, target_ref=target_ref, fields=fields
        )
        for item in cast(list[dict[str, object]], result["families"]):
            item["label"] = FACT_LABELS[str(item["fact_kind"])]
        return cast(JsonObject, json_value(result))

    def describe(self, *, actor: StaffActorContext, fact_kind: str) -> JsonObject:
        allowed, required = fact_content_fields(fact_kind, schema_version=2)
        enums = fact_content_enums(fact_kind)
        properties = CanonicalFactContentV2.model_json_schema()["properties"]
        fields: list[JsonValue] = []
        for canonical in sorted(allowed):
            name = (
                canonical.removesuffix("_source_key") + "_ref"
                if canonical.endswith("_source_key")
                else canonical
            )
            definition = properties[canonical]
            candidates = definition.get("anyOf", [definition])
            value_type = next((v.get("type") for v in candidates if v.get("type") != "null"), None)
            field_type = (
                "reference"
                if name.endswith("_ref")
                else "select"
                if canonical in enums
                else "boolean"
                if value_type == "boolean"
                else "number"
                if value_type == "integer"
                else "datetime"
                if any(v.get("format") == "date-time" for v in candidates)
                else "lines"
                if value_type == "array"
                else "textarea"
                if canonical
                in {
                    "content_text",
                    "function_summary",
                    "service_summary",
                    "basis_reference",
                    "version_note",
                }
                else "text"
            )
            reference_kind = REFERENCE_KINDS.get(name)
            if name == "parent_ref":
                reference_kind = fact_kind
            fields.append(
                {
                    "name": name,
                    "label": FIELD_CONTEXT_LABELS.get(
                        (fact_kind, name), FIELD_LABELS.get(name, name)
                    ),
                    "type": field_type,
                    "required": canonical in required,
                    "reference_kind": reference_kind,
                    "options": [
                        {
                            "value": item,
                            "label": ENUM_CONTEXT_LABELS.get(
                                (fact_kind, canonical, item), ENUM_LABELS.get(item, item)
                            ),
                        }
                        for item in sorted(enums.get(canonical, ()))
                    ],
                }
            )
        return {
            "kind": "fact_fields",
            "fact_kind": fact_kind,
            "query_support": query_support_view(fact_kind),
            "label": FACT_LABELS[fact_kind],
            "fields": fields,
            "creation_choices": json_value(
                self.repository.creation_choices(actor=actor, fact_kind=fact_kind)
            ),
            "notice": (
                "关系引用选择已获权的正式对象；仅新增用户明确确认的事实，不由名字或岗位推断权限。"
                "创建项只列获准新增选择，空创建项不代表已有对象不能修改；当前维护资格用access的maintain用途核验。"
            ),
        }

    @timed("facts")
    def original(
        self,
        *,
        actor: StaffActorContext,
        reference: str,
        offset: int,
        limit: int,
        as_of: datetime | None = None,
    ) -> JsonObject:
        result = self.repository.query(
            actor=actor, fact_kind="policy_version", reference=reference, factual_at=as_of
        )
        if result.status == "forbidden":
            raise PermissionError("Policy body denied")
        if result.status == "source_unavailable":
            raise SchoolFactAuthorizationUnavailable("Policy source unavailable")
        if len(result.records) != 1:
            raise ValueError("Policy version unavailable")
        record = result.records[0]
        body = record.content.get("content_text")
        if not isinstance(body, str) or offset > len(body):
            raise ValueError("Invalid policy body range")
        end = min(offset + limit, len(body))
        return {
            "kind": "policy_original",
            "reference": reference,
            "version": record.version,
            "text": body[offset:end],
            "offset": offset,
            "end": end,
            "length": len(body),
            "complete": offset == 0 and end == len(body),
            "has_more": end < len(body),
            "source": record_view(record)["source"],
            "notice": "正式原文，区别于模型解释。",
        }

    def resolve_change(
        self,
        *,
        actor: StaffActorContext,
        payload: FactChangeInput,
        request_id: UUID,
        effective_at: datetime,
    ) -> ResolvedFactChange:
        target = self.repository.prepare_target(
            actor=actor,
            fact_kind=payload.fact_kind,
            target_ref=payload.target_ref,
            creation_ref=payload.creation_ref,
        )
        merged = dict(target.content)
        if "coverage_basis" in payload.fields:
            raise ValueError("Coverage proof is generated by the authorized fact Reader.")
        complete = False
        if payload.operation in {"upsert", "reactivate"}:
            merged.update(
                self.repository.resolve_field_references(
                    actor=actor, fact_kind=payload.fact_kind, fields=payload.fields
                )
            )
            complete = (
                payload.fact_kind == "fact_quality"
                and merged.get("quality_kind") == "coverage"
                and merged.get("coverage_status") == "complete"
            )
            if complete:
                target_kind = merged.get("target_fact_kind")
                target_key = merged.get("target_source_key")
                owner_collection = (
                    target_kind == "organization_membership" and merged.get("purpose") is None
                ) or (
                    target_kind == "organization_location"
                    and merged.get("purpose") in {"office", "reception"}
                )
                if isinstance(target_key, str) and target_kind in F2_QUALITY_TARGET_KINDS:
                    target_ref = self.repository.reference_for(
                        actor=actor, fact_kind=target_kind, source_key=target_key
                    )
                    selected = self.repository.prepare_target(
                        actor=actor, fact_kind=target_kind, target_ref=target_ref
                    )
                    if (
                        selected.source_key != target_key
                        or selected.school_fact_version != target.school_fact_version
                        or selected.expected_version < 1
                        or selected.safe_record.valid_from > effective_at
                        or selected.safe_record.valid_to is not None
                        and effective_at >= selected.safe_record.valid_to
                    ):
                        raise PermissionError("Complete exact target cannot be verified.")
                    members = ((target_key, selected.expected_version),)
                elif (
                    owner_collection
                    and merged.get("owner_kind") == "organization"
                    and target_key is None
                    and isinstance(merged.get("organization_source_key"), str)
                ):
                    owner_key = merged["organization_source_key"]
                    owner_ref = self.repository.reference_for(
                        actor=actor, fact_kind="organization", source_key=owner_key
                    )
                    population = self.repository.query(
                        actor=actor,
                        fact_kind=target_kind,
                        related_ref=owner_ref,
                        factual_at=effective_at,
                        page_size=100,
                    )
                    if (
                        population.status not in {"known", "no_current_records"}
                        or population.has_more
                        or population.matched_count != len(population.records)
                        or population.read_version not in {None, target.school_fact_version}
                    ):
                        raise PermissionError("Complete owner collection cannot be verified.")
                    members = tuple(
                        (
                            self.repository.source_for(
                                actor=actor, fact_kind=target_kind, reference=row.fact_ref
                            ),
                            row.version,
                        )
                        for row in population.records
                        if target_kind == "organization_membership"
                        or row.content.get("purpose") == merged["purpose"]
                    )
                else:
                    raise ValueError("This quality scope has no verified complete-set adapter.")
                count, digest = mother_set_digest(members)
                owner_kind = merged["owner_kind"]
                owner_key = merged.get({
                    "organization": "organization_source_key",
                    "person": "person_source_key",
                    "location": "location_source_key",
                }.get(owner_kind, ""))
                merged["coverage_basis"] = {
                    "schema_version": 1,
                    "school_id": str(actor.school_id),
                    "owner_kind": owner_kind,
                    "owner_source_key": owner_key,
                    "target_fact_kind": target_kind,
                    "target_source_key": target_key,
                    "field_name": None,
                    "purpose": merged.get("purpose"),
                    "contact_kind": None,
                    "valid_from": effective_at.isoformat(),
                    "valid_to": merged.get("valid_to"),
                    "school_fact_version": target.school_fact_version,
                    "member_count": count,
                    "member_sha256": digest,
                }
            else:
                merged.pop("coverage_basis", None)
            # Reader JSONB and nested public fields carry ISO dates as JSON strings.
            # JSON mode retains strict scalar validation and timezone requirements.
            content_type = CanonicalFactContentV4 if complete else CanonicalFactContentV2
            content = content_type.model_validate_json(
                json.dumps(json_value(merged), ensure_ascii=False)
            )
        else:
            if payload.fields:
                raise ValueError("结束或撤销不接受替换内容。")
            content = None
        material = {
            "request": str(request_id),
            "payload": payload.model_dump(mode="json"),
            "effective_at": effective_at.isoformat(),
        }
        digest = hashlib.sha256(
            json.dumps(material, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()
        change_type = CanonicalFactChangeV4 if complete else CanonicalFactChangeV2
        change = change_type(
            operation=payload.operation,
            fact_kind=payload.fact_kind,
            source_key=target.source_key,
            expected_version=target.expected_version,
            effective_at=effective_at,
            origin=FactOriginV1(sheet_code="f2_maintenance", row_number=1),
            content=content,
        )
        change_set_type = CanonicalFactChangeSetV4 if complete else CanonicalFactChangeSetV2
        change_set = change_set_type(
            source_sha256=digest, base_version=target.school_fact_version, changes=(change,)
        )
        before = record_view(target.safe_record)
        public_before = cast(JsonObject, before["fields"]) if payload.target_ref else None
        after = dict(public_before or {})
        after.update(payload.fields)
        summary = FactChangeSummary(
            fact_kind=payload.fact_kind,
            label=str(before["label"]),
            reference=payload.target_ref or payload.creation_ref or "",
            operation=payload.operation,
            before=public_before,
            after=after if content else None,
            effective_at=effective_at,
            reason=payload.reason,
            source_label="学校事实维护",
        )
        return ResolvedFactChange(change_set=change_set, summary=summary)
