"""Finite staff teaching tools with complete current proof and send-time checks."""

from __future__ import annotations

import json
from collections.abc import Callable
from copy import deepcopy
from typing import Any, Literal, Self, cast

from pydantic import AwareDatetime, Field, model_validator

from app.composition.school_fact_answer_evidence import answer_evidence_material
from app.composition.school_fact_assistant_gateway import ProductActor
from app.composition.school_fact_education_external import EducationRelatedReadIncomplete
from app.composition.school_fact_education_gateway import education_model_material
from app.composition.school_fact_education_reader import EducationReadStale
from app.composition.school_fact_read_evidence import validation_batch
from app.composition.school_fact_staff_teaching_query import StaffTeachingQueryService
from app.platform.assistant.contracts import JsonObject
from app.platform.assistant.runtime import AssistantRuntimeError, HistoryCheck
from app.platform.assistant.tool_loop import (
    ModelToolCall,
    ToolCallContext,
    ToolDescriptor,
    ToolExecutionResult,
)
from app.platform.authorization.secure_reader_contracts import SecureReadDenied
from app.platform.contracts import FrozenContractModel, StaffActorContext
from app.platform.school_facts.application import SchoolFactChangeRejected

MODULE = "school_facts.teaching"
TEACHING_ASSISTANT_INSTRUCTION = (
    "持续开课、学科任课、本人教什么及教研适用范围使用教学query。本人身份由服务器定位，不传人员编号。"
    "班名、年级、班序和校区分别传；不从名称推导正式关系。查询数学等学科时传subject_name，具体课程传course_name。"
    "一门学科可能有多门课程、多个开课和共同任教，按返回对应关系回答，不合并同名对象。"
    "普通答复只给所问课程、老师、班级和必要时间；用户要联系时才请求official contacts。"
    "当前、下学期按正式教育时间查询，不能自己加月份。无登记不等于确定没有安排或名单完整。"
    "行政班来源与独立教学名单分清；独立名单不自动跟随转班，教研成员不获得学生明细权限。"
)


class TeachingQueryInput(FrozenContractModel):
    view: Literal["my_teaching", "teaching", "my_organizations"] = "my_teaching"
    class_reference: str | None = Field(default=None, min_length=8, max_length=1600)
    target_reference: str | None = Field(
        default=None,
        max_length=1600,
        pattern=r"^(?:f4:(?:teaching_assignment|teaching_offering):[0-9a-f]{64}|schoolv1:f5:(?:teaching_assignment|teaching_offering):o:[A-Za-z0-9_-]+:[0-9a-f]{64})$",
        description="已正式返回的任教或开课引用；仅view=teaching，与班级、学科、课程名称条件互斥。任教引用只查该任教，不展开同课其他教师；开课引用查询该开课获准集合。",
    )
    name: str | None = Field(
        default=None,
        min_length=1,
        max_length=200,
        description="正式班名，原样匹配；年级和班序另填。",
    )
    grade_name: str | None = Field(default=None, min_length=1, max_length=100)
    campus_name: str | None = Field(default=None, min_length=1, max_length=200)
    class_number: int | None = Field(default=None, strict=True, ge=1)
    subject_name: str | None = Field(
        default=None,
        min_length=1,
        max_length=100,
        description="学科正式名称，如数学；可对应多门课程。",
    )
    course_name: str | None = Field(
        default=None, min_length=1, max_length=200, description="具体课程正式名称，和学科分开。"
    )
    as_of: AwareDatetime | None = Field(
        default=None,
        description=(
            "所问事实时点，含时区；指定时须省略period或用period=current。授权始终检查现在。"
        ),
    )
    period: Literal["current", "current_term", "next_term"] = Field(
        default="current", description="当前或正式学期；current_term/next_term不能与as_of并用。",
    )
    include_contacts: bool = Field(
        default=False, strict=True, description="仅用户请求联系老师时开启，返回获准官方渠道。"
    )

    @model_validator(mode="after")
    def finite_selection(self) -> Self:
        has_class = any(
            value is not None
            for value in (
                self.class_reference,
                self.name,
                self.grade_name,
                self.campus_name,
                self.class_number,
            )
        )
        if (
            self.as_of is not None
            and self.period != "current"
            or self.view == "teaching"
            and not (has_class or self.subject_name or self.course_name or self.target_reference)
            or self.view != "teaching"
            and (has_class or self.course_name)
            or self.view == "my_organizations"
            and self.include_contacts
            or self.target_reference is not None
            and (
                self.view != "teaching"
                or has_class
                or self.subject_name is not None
                or self.course_name is not None
            )
        ):
            raise ValueError("需要明确且一致的教学条件。")
        return self


class StaffTeachingGateway:
    module_ref = MODULE
    descriptors = (
        ToolDescriptor(
            name=MODULE + ".query",
            owner=MODULE,
            kind="read",
            description="读取本人任课、指定班级/学科/课程的持续任教，或本人教研成员及正式适用范围；仅请求必要字段。",
            parameters=TeachingQueryInput.model_json_schema(),
        ),
    )

    def __init__(
        self, *, resolve_actor: Callable[[str], ProductActor], query: StaffTeachingQueryService
    ):
        self.resolve_actor, self.query = resolve_actor, query

    def identity(self, reference: str) -> ProductActor:
        identity = self.resolve_actor(reference)
        if type(identity.actor) is not StaffActorContext:
            raise PermissionError("教学职工查询需要当前职工身份。")
        return identity

    def for_turn(self, actor_context_ref: str) -> _TeachingTurn:
        return _TeachingTurn(self, actor_context_ref)

    def execute(self, call: ModelToolCall, context: ToolCallContext) -> ToolExecutionResult:
        try:
            if call.name != MODULE + ".query":
                raise ValueError("未知教学操作。")
            identity = self.identity(context.actor_context_ref)
            expected_actor, session = identity.actor, identity.session_binding
            payload = TeachingQueryInput.model_validate_json(json.dumps(call.arguments))
            result = self.query.query(identity.actor, **payload.model_dump())

            def current() -> bool:
                fresh = self.identity(context.actor_context_ref)
                if fresh.actor != expected_actor or fresh.session_binding != session:
                    return False
                result.private_proof.revalidate(actor=fresh.actor)
                return True

            check = HistoryCheck(current, revalidate=current, revalidation_covers_content=True)
            if not check.valid():
                raise EducationReadStale("教学结果依据已变化。")
            public = cast(JsonObject, deepcopy(result.public_result))
            return ToolExecutionResult(
                model_content=answer_evidence_material(education_model_material(
                    public, teaching_answer=True, query_answer=True
                )),
                fact_card={
                    **deepcopy(public),
                    "family_owner": MODULE,
                    "evidence": cast(Any, deepcopy(list(result.evidence))),
                },
                history_check=check,
            )
        except EducationReadStale:
            status, message = "stale", "正式教学资料或身份已变化，请重新核对。"
        except EducationRelatedReadIncomplete as error:
            return ToolExecutionResult(model_content=error.public_result())
        except (PermissionError, SchoolFactChangeRejected, SecureReadDenied):
            status, message = "denied", "当前身份没有取得所问教学资料的完整读取权限。"
        except ValueError:
            status, message = "unsupported_selection", "请核对班级、学科、课程和所问时点。"
        except Exception:
            status, message = "source_unavailable", "正式教学资料暂时无法核实，请稍后再试。"
        return ToolExecutionResult(
            model_content={"status": status, "message": message},
            history_execution_note="read_returned_no_values"
            if call.name == MODULE + ".query" and status == "denied"
            else None,
        )


class _TeachingTurn:
    def __init__(self, owner: StaffTeachingGateway, actor_context_ref: str):
        self.owner, self.actor_context_ref = owner, actor_context_ref
        self._checks: list[HistoryCheck] = []

    @property
    def descriptors(self) -> tuple[ToolDescriptor, ...]:
        return self.owner.descriptors

    def validation_scope(self):
        return validation_batch()

    def revalidate(self, actor_context_ref: str) -> None:
        if actor_context_ref != self.actor_context_ref:
            raise AssistantRuntimeError("Teaching identity context changed")
        self.owner.identity(actor_context_ref)
        with validation_batch():
            if any(not check.valid() for check in self._checks):
                raise AssistantRuntimeError("Teaching evidence changed")

    def execute(self, call: ModelToolCall, context: ToolCallContext) -> ToolExecutionResult:
        self.revalidate(context.actor_context_ref)
        result = self.owner.execute(call, context)
        if result.history_check is not None:
            self._checks.append(result.history_check)
        return result
