"""Family-domain readers bind to one freshly verified relationship per tool call."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from copy import deepcopy
from typing import Literal, Protocol, cast

from pydantic import AwareDatetime, Field

from app.composition.school_fact_answer_evidence import answer_evidence_material
from app.composition.school_fact_directory_context import bind_directory_identity
from app.composition.school_fact_family_education_query import FamilyEducationQueryService
from app.composition.school_fact_read_evidence import validate_fallback_read
from app.domains.students.family_read import (
    FamilyQueryResult,
    FamilyQueryService,
    FamilyReadEvidence,
)
from app.platform.assistant.contracts import JsonObject
from app.platform.assistant.runtime import AssistantRuntimeError, HistoryCheck
from app.platform.assistant.tool_loop import (
    ModelToolCall,
    ToolCallContext,
    ToolDescriptor,
    ToolExecutionResult,
)
from app.platform.contracts import ActorContext, FrozenContractModel, GuardianActorContext

FAMILY_MODULE = "school_facts.family"
FAMILY_ASSISTANT_INSTRUCTION = (
    "孩子资料由工具所绑定的当前获权家校关系核验；姓名及聊天自述本身不授予权限。"
    "家长可在对话中选取任一当前获权孩子，教职工身份仍须显式选择，权限不能混用。"
    "profile中的名称与资料为本次获准字段；field_restrictions是访问限制，不是未录入。"
    "field_meanings定义各字段的事实归属；同一资料中的字段不能互相移作其他对象的属性。"
    "missing只解释获准字段的本次记录缺项；coverage未知不支持学校全部或唯一结论。"
    "已定位范围的无当前记录也是有效查询结果，不为得到正值自行换孩子、时间或范围。"
    "只回答所问的实际资料，来源需要时展开；sources中的追溯引用不进入普通正文。"
    "quality仅是学校对所问字段的独立质量声明；有争议或来源故障等风险应简明说明，"
    "没有质量声明不等于资料已经独立核实。"
    "历史时点必须在当前仍有效且获准的家校关系期间；照护人不能读取历史或监护人专属字段。"
    "班级、班主任、年级与本人职务只通过已安装的education能力读取，不能由学生资料推断；课程与教学内容不属于该能力。"
)


class FamilyIdentity(Protocol):
    @property
    def actor(self) -> ActorContext: ...


class FamilyProfileInput(FrozenContractModel):
    as_of: AwareDatetime | None = Field(
        default=None, description="只在用户明确询问历史时填写；默认查询当前。"
    )


FamilyProfileField = Literal[
    "name", "enrollment_status", "campus", "student_number", "residence_mode", "region"
]


class FamilyModelProfileInput(FamilyProfileInput):
    fields: tuple[FamilyProfileField, ...] = Field(
        min_length=1,
        max_length=6,
        description=(
            "明确选择本次问题所需字段：name姓名、enrollment_status在校状态、campus当前校区、"
            "student_number学号、residence_mode走读或住宿、region孩子居住地区。"
            "只取完成当前问题需要的字段，不自动附带姓名或其他资料。"
        ),
    )


class FamilyEducationInput(FrozenContractModel):
    fields: tuple[
        Literal[
            "class", "grade", "teachers", "classroom", "positions", "cohorts", "official_contacts"
        ],
        ...,
    ] = Field(min_length=1, max_length=7)
    as_of: AwareDatetime | None = Field(
        default=None, description="用户明确指定的历史或未来时点；默认当前。"
    )


_FIELD_SOURCE_OWNERS = {
    "name": ("person",),
    "enrollment_status": ("student_school_relationship",),
    "campus": ("student_school_relationship", "location"),
    "student_number": ("student_school_relationship",),
    "residence_mode": ("student_school_relationship",),
    "region": ("person_region",),
}


def _selected_profile(
    result: FamilyQueryResult, fields: tuple[FamilyProfileField, ...]
) -> JsonObject:
    """Minimize an already authorized result; requested fields never grant access."""
    public = result.public()
    requested = set(fields)
    approved = cast(list[str], public["authorized_fields"])
    authorized = [field for field in approved if field in requested]
    public["profile"] = {
        field: deepcopy(value) for field, value in result.profile.items() if field in authorized
    }
    public["authorized_fields"] = authorized
    public["missing"] = [field for field in result.missing if field in authorized]
    conflicts = [field for field in result.conflicting_fields if field in authorized]
    public["conflicting_fields"] = conflicts
    if (
        result.status == "conflict"
        and result.conflicting_fields
        and all(field in approved for field in result.conflicting_fields)
        and not conflicts
    ):
        public["status"] = "available"
    public["field_restrictions"] = {
        field: reason
        for field, reason in cast(dict[str, str], public["field_restrictions"]).items()
        if field in requested
    }
    source_owners = {owner for field in authorized for owner in _FIELD_SOURCE_OWNERS[field]}
    public["sources"] = [
        deepcopy(source) for source in result.sources if source.get("fact") in source_owners
    ]
    quality = public.get("quality")
    if isinstance(quality, dict):
        public["quality"] = {
            field: deepcopy(item) for field, item in quality.items() if field in authorized
        }
    return cast(JsonObject, public)


class FamilyProfileToolGateway:
    descriptors = (
        ToolDescriptor(
            name=FAMILY_MODULE + ".profile",
            owner=FAMILY_MODULE,
            kind="read",
            description="查询本次目标孩子的获准学生及在校资料、最小地区和来源。后台核验该孩子的当前家校关系；监护人可查获准期间历史，照护人仅当前摘要。字段拒绝、记录缺失与覆盖不足分别返回。",
            parameters=FamilyModelProfileInput.model_json_schema(),
        ),
    )

    def __init__(
        self,
        *,
        resolve_actor: Callable[[str], FamilyIdentity],
        query: FamilyQueryService,
        education: FamilyEducationQueryService | None = None,
    ) -> None:
        self.resolve_actor, self.query = resolve_actor, query
        self.education = education
        if education is not None:
            self.descriptors = self.descriptors + (
                ToolDescriptor(
                    name=FAMILY_MODULE + ".education",
                    owner=FAMILY_MODULE,
                    kind="read",
                    description="查询本次目标孩子的班级、年级、班主任、教室、本人岗位与届别。只限当前有效监护人关系；可问已发布未来安排。公开联系为组织或教室服务渠道，不是老师私号；不提供其他学生名单。",
                    parameters=FamilyEducationInput.model_json_schema(),
                ),
            )

    def for_turn(self, actor_context_ref: str) -> _FamilyProfileTurn:
        return _FamilyProfileTurn(self, actor_context_ref)


class _FamilyProfileTurn:
    def __init__(self, owner: FamilyProfileToolGateway, actor_context_ref: str) -> None:
        self.owner, self.actor_context_ref = owner, actor_context_ref
        self._checks: list[FamilyReadEvidence] = []

    @property
    def descriptors(self) -> tuple[ToolDescriptor, ...]:
        return self.owner.descriptors

    def revalidate(self, actor_context_ref: str) -> None:
        if actor_context_ref != self.actor_context_ref:
            raise AssistantRuntimeError("family context changed")
        current = self.owner.resolve_actor(actor_context_ref)
        if type(current.actor) is not GuardianActorContext:
            raise AssistantRuntimeError("selected family context required")
        with bind_directory_identity(actor_context_ref):
            if any(not self._valid(check, current.actor) for check in self._checks):
                raise AssistantRuntimeError("family evidence changed before use")

    def _valid(self, evidence: FamilyReadEvidence, actor: ActorContext) -> bool:
        def current() -> bool:
            latest = self.owner.resolve_actor(self.actor_context_ref).actor
            with bind_directory_identity(self.actor_context_ref):
                return latest == actor and evidence.valid(actor)

        # Only this exact owner's complete evidence shares one release callback.
        # The live identity and full domain reread still run before batch exit.
        return validate_fallback_read(
            (id(self.owner), self.actor_context_ref, actor, id(evidence)),
            current,
            time_window=getattr(evidence, "time_window", None),
        )

    def execute(self, call: ModelToolCall, context: ToolCallContext) -> ToolExecutionResult:
        self.revalidate(context.actor_context_ref)
        if call.name == FAMILY_MODULE + ".education" and self.owner.education is not None:
            return self._education(call, context)
        if call.name != FAMILY_MODULE + ".profile":
            raise AssistantRuntimeError("family capability not installed")
        payload = FamilyModelProfileInput.model_validate_json(json.dumps(call.arguments))
        actor = self.owner.resolve_actor(context.actor_context_ref).actor
        with bind_directory_identity(context.actor_context_ref):
            result = self.owner.query.query(
                actor=actor, as_of=payload.as_of,
                **(
                    {"quality_fields": payload.fields}
                    if getattr(self.owner.query, "quality_enabled", False) is True else {}
                ),
            )
        public = _selected_profile(result, payload.fields)
        material = deepcopy(public)
        meanings = {
            "name": "孩子的姓名",
            "enrollment_status": "孩子的在校状态",
            "campus": "孩子在校关系中的主要校区名称",
            "student_number": "孩子在本校的学号",
            "residence_mode": "孩子的走读或住宿情况",
            "region": "孩子的家庭居住地区",
        }
        material["field_meanings"] = {key: meanings[key] for key in payload.fields}
        profile = material.get("profile")
        if isinstance(profile, dict):
            labels = {
                "enrollment_status": {
                    "enrolled": "在校",
                    "suspended": "休学",
                    "graduated": "已毕业",
                    "transferred_out": "已转出",
                    "withdrawn": "已退学",
                },
                "residence_mode": {"day": "走读", "boarding": "住宿"},
            }
            for field, values in labels.items():
                value = profile.get(field)
                if isinstance(value, str):
                    profile[field] = values.get(value, value)
            region = profile.get("region")
            if isinstance(region, dict):
                region.pop("district_code", None)
        sources = material.get("sources")
        if isinstance(sources, list):
            for source in sources:
                if isinstance(source, dict):
                    source.pop("reference", None)
                    source.pop("source_trace_reference", None)
        quality = material.get("quality")
        if isinstance(quality, dict):
            for item in quality.values():
                if isinstance(item, dict):
                    item.pop("details", None)
        evidence = result.evidence
        if evidence is None:
            return ToolExecutionResult(
                model_content=answer_evidence_material(material),
                fact_card=public,
                history_execution_note="read_returned_no_values"
                if result.status == "denied"
                and not result.profile
                and not result.sources
                and not result.missing
                and not result.conflicting_fields
                and result.relationship_kind is None
                else None,
            )
        self._checks.append(evidence)

        def current() -> bool:
            return self._valid(evidence, actor)

        key = hashlib.sha256(
            (
                self.actor_context_ref + call.name + json.dumps(call.arguments, sort_keys=True)
            ).encode()
        ).hexdigest()
        return ToolExecutionResult(
            model_content=answer_evidence_material(material),
            fact_card=public,
            history_check=HistoryCheck(
                current, revalidate=current, key=key, revalidation_covers_content=True
            ),
        )

    def _education(self, call: ModelToolCall, context: ToolCallContext) -> ToolExecutionResult:
        payload = FamilyEducationInput.model_validate_json(json.dumps(call.arguments))
        actor = self.owner.resolve_actor(context.actor_context_ref).actor
        with bind_directory_identity(context.actor_context_ref):
            result = self.owner.education.query(
                actor=actor, fields=payload.fields, as_of=payload.as_of
            )
        public = result.public()
        from app.composition.school_fact_education_gateway import education_model_material

        material = deepcopy(public)
        # Keep scoped publication provenance with its actual fact owner. Source
        # locators and batch identifiers belong only to the fact-card trace.
        material["sources"] = [
            {
                key: value
                for key, value in source.items()
                if key
                in {
                    "fact",
                    "source_kind",
                    "label",
                    "summary",
                    "published_at",
                    "recorded_at",
                    "effective_at",
                    "valid_to",
                }
            }
            for source in material.get("sources", [])
            if isinstance(source, dict)
        ]
        material = education_model_material(material)
        evidence = result.evidence
        if evidence is None:
            return ToolExecutionResult(
                model_content=answer_evidence_material(material),
                fact_card=public,
                history_execution_note="read_returned_no_values"
                if public == {"status": "denied"}
                else None,
            )
        self._checks.append(evidence)

        def current() -> bool:
            return self._valid(evidence, actor)

        key = hashlib.sha256(
            (
                self.actor_context_ref + call.name + json.dumps(call.arguments, sort_keys=True)
            ).encode()
        ).hexdigest()
        return ToolExecutionResult(
            model_content=answer_evidence_material(material),
            fact_card=public,
            history_check=HistoryCheck(
                current, revalidate=current, key=key, revalidation_covers_content=True
            ),
        )
