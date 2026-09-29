"""Finite selected-child teaching tool; the model cannot supply an identity."""

from __future__ import annotations

import hashlib
import json
from typing import Literal, Self

from pydantic import AwareDatetime, Field, ValidationError, model_validator

from app.composition.school_fact_answer_evidence import answer_evidence_material
from app.composition.school_fact_directory_context import bind_directory_identity
from app.composition.school_fact_education_gateway import education_model_material
from app.composition.school_fact_read_evidence import validate_fallback_read
from app.platform.assistant.runtime import AssistantRuntimeError, HistoryCheck
from app.platform.assistant.tool_loop import ToolDescriptor, ToolExecutionResult
from app.platform.contracts import FrozenContractModel

NAME = "school_facts.family.teaching"
FAMILY_TEACHING_INSTRUCTION = (
    "当前已安装教学查询和学校领导目录：任课、课程请用teaching；学校领导用leadership，"
    "不用孩子任教关系推断领导。一个孩子可有多门同学科课程与共同任教，保持对应关系。"
    "未录入不等于正式空缺；无当前安排可以完成查询，不为找到正值自动换对象或扩大范围。"
    "持续任教按已登记有效期跨学期沿用；未来只能说明按目前安排，不能冒称学校再次确认。"
    "回答先说所问的姓名、课程或渠道；来源按需说明，内部编号与工具状态留在维护详情。"
)


class FamilyTeachingInput(FrozenContractModel):
    subject_name: str | None = Field(default=None, min_length=1, max_length=120, pattern=r"\S")
    course_name: str | None = Field(default=None, min_length=1, max_length=120, pattern=r"\S")
    include_official_contacts: bool = False
    as_of: AwareDatetime | None = Field(
        default=None,
        description="所问事实时点，含时区；指定时必须省略period或用period=current，不能同时查询学期。",
    )
    period: Literal["current", "current_term", "next_term"] = Field(
        default="current",
        description="当前或正式学期；current_term/next_term不能与as_of并用。",
    )

    @model_validator(mode="after")
    def finite_time_selection(self) -> Self:
        if self.as_of is not None and self.period != "current":
            raise ValueError("指定事实时点与学期查询不能并用。")
        return self


class FamilyTeachingToolGateway:
    descriptors = (
        ToolDescriptor(
            name=NAME,
            owner="school_facts.f5",
            kind="read",
            description=(
                "查询本次目标孩子的正式课程、相关任课老师和来源；可选学科、课程、历史/未来时点或学期。"
                "无需先查孩子编号、班级或全校目录，受控查询会完成必要关联。需要联系时才取官方渠道。"
                "不返回同学名单、私人号码、逐节课表；多课程多人保留归属，无记录/空缺/冲突分别说明。"
            ),
            parameters=FamilyTeachingInput.model_json_schema(),
        ),
    )

    def __init__(self, *, resolve_actor, query):
        self.resolve_actor, self.query = resolve_actor, query

    def for_turn(self, actor_context_ref):
        return _Turn(self, actor_context_ref)


class _Turn:
    def __init__(self, owner, actor_context_ref):
        self.owner, self.actor_context_ref, self._checks = owner, actor_context_ref, []

    @property
    def descriptors(self):
        return self.owner.descriptors

    def revalidate(self, actor_context_ref):
        if actor_context_ref != self.actor_context_ref:
            raise AssistantRuntimeError("Teaching identity changed")
        actor = self.owner.resolve_actor(actor_context_ref).actor
        with bind_directory_identity(actor_context_ref):
            if any(not self._valid(evidence, actor) for evidence in self._checks):
                raise AssistantRuntimeError("Teaching evidence changed")

    def _valid(self, evidence, actor):
        def current():
            # The callback runs before this trusted release scope exits, after
            # duplicate gateway/history checks, and never survives a model wait.
            latest = self.owner.resolve_actor(self.actor_context_ref).actor
            with bind_directory_identity(self.actor_context_ref):
                return latest == actor and evidence.valid(actor) is True

        return validate_fallback_read(
            (NAME, self.actor_context_ref, actor, id(evidence)),
            current,
            time_window=getattr(evidence, "time_window", None),
        )

    def execute(self, call, context):
        self.revalidate(context.actor_context_ref)
        if call.name != NAME:
            raise AssistantRuntimeError("Teaching capability unavailable")
        try:
            args = FamilyTeachingInput.model_validate_json(json.dumps(call.arguments))
        except ValidationError:
            # Only input validation is recoverable here; identity, evidence and
            # unexpected query failures must retain their existing fail-closed path.
            return ToolExecutionResult(
                model_content={
                    "status": "unsupported_selection",
                    "message": "请核对学科、课程和所问时点；时点须含时区，不能同时指定学期。",
                }
            )
        actor = self.owner.resolve_actor(context.actor_context_ref).actor
        with bind_directory_identity(context.actor_context_ref):
            result = self.owner.query.query(actor=actor, **args.model_dump())
        public, evidence = result.public(), result.evidence
        material = answer_evidence_material(
            education_model_material(public, teaching_answer=True, query_answer=True)
        )
        if evidence is None:
            return ToolExecutionResult(
                model_content=material,
                fact_card=public,
                history_execution_note="read_returned_no_values"
                if public == {"status": "denied"}
                else None,
            )
        self._checks.append(evidence)

        def current():
            # Register this original evidence/actor only. _valid's release callback
            # still resolves the live identity before checking the complete proof.
            with bind_directory_identity(self.actor_context_ref):
                return self._valid(evidence, actor)

        key = hashlib.sha256(
            (self.actor_context_ref + NAME + json.dumps(call.arguments, sort_keys=True)).encode()
        ).hexdigest()
        return ToolExecutionResult(
            model_content=material,
            fact_card=public,
            history_check=HistoryCheck(
                current, revalidate=current, key=key, revalidation_covers_content=True
            ),
        )
