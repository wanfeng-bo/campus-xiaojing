"""F3 Staff family-fact model tools and assistant module port."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from copy import deepcopy
from dataclasses import replace
from typing import Literal, Protocol, cast
from uuid import uuid4

from pydantic import AwareDatetime, Field, JsonValue, ValidationError, model_validator

from app.composition.school_fact_answer_evidence import answer_evidence_material
from app.composition.school_fact_assistant_gateway import ProductActor
from app.composition.school_fact_directory_action_material import (
    own_receipt_model_material,
    own_receipts_model_material,
    receipt_model_material,
)
from app.composition.school_fact_directory_gateway import (
    DirectoryReceiptInput,
    FactConversationActions,
)
from app.composition.school_fact_directory_product import (
    DirectoryFactProductService,
    F3MaintenanceFactKind,
    FactChangeInput,
    FactPreflightRejected,
    PrepareAccessDenied,
    validate_own_receipt_read,
)
from app.composition.school_fact_directory_product import (
    prepare_argument_errors as _prepare_argument_errors,
)
from app.composition.school_fact_family_product_adapter import family_field_descriptor
from app.composition.school_fact_product_contacts import ContactProductUnavailable
from app.composition.school_fact_read_evidence import validate_fallback_read, validation_batch
from app.platform.assistant.contracts import (
    ActionOfferV1,
    CapsuleUnchangedV1,
    JsonObject,
    ModuleCallV1,
    ModuleConfirmationCallV1,
    ModuleRecoveryCallV1,
    ModuleResultCheckCallV1,
    ModuleResultV1,
    ResultCheckHandleV1,
    ValidationReceiptV1,
)
from app.platform.assistant.runtime import AssistantRuntimeError, HistoryCheck
from app.platform.assistant.tool_loop import (
    ModelToolCall,
    ToolCallContext,
    ToolDescriptor,
    ToolExecutionResult,
)
from app.platform.contracts import FrozenContractModel, StaffActorContext
from app.platform.school_facts.f3_policy import F3_FACT_FIELDS, F3_FACT_KINDS

MODULE = "school_facts.family_staff"
FAMILY_STAFF_ASSISTANT_INSTRUCTION = (
    "当前使用教职工身份，学生和家校资料按该身份的职责范围读取、预检。"
    "日常答复只给所问结论和必要限制，不主动罗列其他可查项目；"
    "未返回的个人属性保持未知，不能根据姓名推断性别或使用带有此推断的称谓。"
    "按姓名、学号或二者交集查询学生的基本资料，使用student_profile并仅选所问字段；"
    "用户给出的名称是待核对条件，不需要先证明是完整姓名；原样查询，不截短、补全或改写。"
    "selection回显实际查询条件，不是已确认事实；不得用不同条件的结果回答原对象。"
    "需要追问时只索取clarification.accepted_fields当前支持的查询条件，不提出工具无法使用的条件。"
    "该领域工具负责获权消歧及关联，不必自行逐层遍历。返回ambiguous才请用户补充区分条件；"
    "incomplete表示尚有候选无法核对，不能把未能核对当作不匹配。"
    "profile为所问资料，sources为必要来源；not_returned是本次未获返回，"
    "no_current_records是获权范围没有当前记录，两者不等于现实不存在。"
    "学生查询对象由正式工具返回的对象引用和用户提供的区分条件确定；"
    "遇到同名且仍无明确目标时，列出获准的候选姓名和学号等必要区分信息，"
    "由用户在对话中说明目标，再继续该目标的资料查询。"
    "read.fields只选本次问题和必要消歧所需字段；fields=[]只定位获权对象，保留可读名称和工具引用。"
    "普通回答不自动附带未问的编号、用途、状态等资料；明确询问这些字段时可选择。"
    "unreturned_fields只表示本次获准投影未返回，不能据此说未录入或不存在。"
    "本校学号是student_school_relationship.student_number，person.business_code是人员编号；"
    "按person到student再到student_school_relationship的获权引用核对学号，"
    "student本表没有学号字段不表示无法核对学号。"
    "学校身份及家长孩子选择控件用于切换执行身份，不用于选择教职工查询的学生。"
    "person_region是所关联人员的居住地区，不表示学生校区所在地；"
    "学生校区取student_school_relationship的校区关联及对应正式地点名称，"
    "residence_mode只说明住校或走读，不定义校区。"
    "核验、登记与家长查看是不同权限；班级、班主任与教学能力尚未接入，不推测。"
)


def family_staff_instruction(*, education_enabled: bool = False) -> str:
    """Trusted composition advertises only capabilities it actually installs."""
    if not education_enabled:
        return FAMILY_STAFF_ASSISTANT_INSTRUCTION
    return FAMILY_STAFF_ASSISTANT_INSTRUCTION.replace(
        "核验、登记与家长查看是不同权限；班级、班主任与教学能力尚未接入，不推测。",
        "核验、登记与家长查看是不同权限；班级和班主任资料使用已提供的教育领域工具核实。",
    )


FAMILY_FACT_LABELS = {
    "person": "人员基础资料",
    "login_mapping": "登录身份映射",
    "assignment": "在校任职",
    "student": "学生基础资料",
    "student_school_relationship": "学生在校关系",
    "student_contact": "学生联系人",
    "guardian_relationship": "已核实家校关系",
    "person_region": "人员地区",
}


class FamilyReadInput(FrozenContractModel):
    fact_kind: F3MaintenanceFactKind
    query_text: str | None = Field(
        default=None,
        min_length=1,
        max_length=1000,
        description=(
            "仅person按当前获准display_name精确匹配用户给出的名称，原样传入，不截短或补全；学生姓名先查person取得引用，"
            "再用related_field=person_ref和related_ref查询student。"
        ),
    )
    student_number: str | None = Field(
        default=None,
        min_length=1,
        max_length=1000,
        description="仅student_school_relationship按本校学号精确查询；不能与其他定位条件并用。",
    )
    reference: str | None = Field(default=None, min_length=8, max_length=1600)
    related_field: str | None = Field(
        default=None,
        min_length=5,
        max_length=100,
        description=(
            "student和person_region用person_ref关联person；"
            "student_school_relationship用student_ref关联student。"
            "其他关联字段可查describe；校区引用由地点领域读取正式名称。"
        ),
    )
    related_ref: str | None = Field(
        default=None,
        min_length=8,
        max_length=1600,
        description="此前正式查询返回的关联对象引用；必须与related_field同时传入。",
    )
    as_of: AwareDatetime | None = None
    limit: int = Field(default=10, strict=True, ge=1, le=20)

    @model_validator(mode="after")
    def exact_selection(self):
        if self.student_number is not None and (
            self.fact_kind != "student_school_relationship" or not self.student_number.strip()
        ):
            raise ValueError("student_number requires an exact school relationship selection")
        if (self.related_field is None) != (self.related_ref is None):
            raise ValueError("related_field and related_ref are required together")
        if (
            sum(
                (
                    self.query_text is not None,
                    self.student_number is not None,
                    self.reference is not None,
                    self.related_ref is not None,
                )
            )
            > 1
        ):
            raise ValueError("choose an exact name, object reference, or related reference")
        return self


_MODEL_READ_FIELDS = {
    kind: frozenset(
        field.removesuffix("_source_key") + "_ref" if field.endswith("_source_key") else field
        for field in fields
    )
    for kind, fields in F3_FACT_FIELDS.items()
}
_MODEL_FIELDS_SCHEMA: JsonObject = {
    "items": {
        "type": "string",
        "enum": [
            field for field in sorted({f for fields in _MODEL_READ_FIELDS.values() for f in fields})
        ],
    }
}


class FamilyModelReadInput(FamilyReadInput):
    """Task selection for model tools only; the direct HTTP read contract stays unchanged."""

    fields: tuple[str, ...] = Field(
        max_length=12,
        description=(
            "明确选择本次问题或必要消歧需要的该事实族字段；[]只定位对象取得可读标签和后续引用。"
            "person的display_name是姓名、business_code是人员编号；本校学号只在"
            "student_school_relationship.student_number，校区关联为primary_campus_ref，"
            "residence_mode是住校方式；person_region的district_name是居住区县名称，"
            "district_code是区县代码，region_purpose是地区用途。其他字段可查describe。"
            "不能选择另一事实族字段；字段选择不授予权限，不自动附带未问的字段。"
        ),
        json_schema_extra=_MODEL_FIELDS_SCHEMA,
    )
    include_quality: bool = Field(
        default=False,
        strict=True,
        description="询问此正式对象、所选字段的独立来源、覆盖或争议声明时才选用；"
        "须先定位唯一获准对象，不扩大读取范围。",
    )

    @model_validator(mode="after")
    def task_fields(self):
        if (
            len(set(self.fields)) != len(self.fields)
            or not set(self.fields) <= _MODEL_READ_FIELDS[self.fact_kind]
        ):
            raise ValueError("Select unique fields belonging to this F3 fact kind")
        return self


class FamilyModelPrepareInput(FactChangeInput):
    """Model declaration of the existing F3 handler boundary; HTTP stays shared."""

    fact_kind: F3MaintenanceFactKind


class FamilyDescribeInput(FrozenContractModel):
    fact_kind: F3MaintenanceFactKind


class FamilyStudentProfileInput(FrozenContractModel):
    name: str | None = Field(
        default=None,
        min_length=1,
        max_length=1000,
        description="用户给出的姓名或称呼，原样匹配获权正式姓名；不截短称呼、不补全，不预先认定名称不完整。",
    )
    student_number: str | None = Field(
        default=None,
        min_length=1,
        max_length=1000,
        description="用户给出的本校学号，可独立查询；同时提供姓名时取交集，不是人员编号。",
    )
    fields: tuple[
        Literal[
            "name", "student_number", "campus", "region", "enrollment_status", "residence_mode"
        ],
        ...,
    ] = Field(
        min_length=1,
        max_length=6,
        description="仅选所问资料：姓名、学号、主要校区、居住区县、在校状态、住校方式。消歧条件填在name/student_number，不因消歧再加入回答字段。",
    )
    as_of: AwareDatetime | None = Field(
        default=None, description="仅明确查询历史时填写，当前权限仍须有效。"
    )

    @model_validator(mode="after")
    def unique_fields(self):
        if not any(
            value is not None and value.strip() for value in (self.name, self.student_number)
        ):
            raise ValueError("Provide an exact name or student number")
        if len(set(self.fields)) != len(self.fields):
            raise ValueError("Select unique student profile fields")
        return self


class FamilyOwnReceiptsInput(FrozenContractModel):
    limit: int = Field(default=5, strict=True, ge=1, le=20)


class FamilyOwnReceiptInput(FrozenContractModel):
    include_audit: bool = Field(
        default=False, strict=True, description="仅查询登记时间、版本或审计详情时开启。"
    )
    receipt_ref: str = Field(strict=True, pattern=r"^sf2receipt_[0-9a-f]{64}$")
    target_offset: int = Field(default=0, strict=True, ge=0, le=50_000)
    target_limit: int = Field(default=20, strict=True, ge=1, le=20)


class FamilyReceiptInput(DirectoryReceiptInput):
    """Use the same bounded original-action selectors as other School Facts tools."""

    action_number: int | None = Field(
        default=None,
        strict=True,
        gt=0,
        description="本对话学生与家校资料工具返回的修改编号；仅用于该领域原操作，"
        "不能代入其他领域编号。与原正式引用二选一，不是确认或执行权限。",
    )


class FamilyStaffQueryPort(Protocol):
    def reference_choices(
        self, *, actor: StaffActorContext, fact_kind: str, field_name: str
    ) -> JsonObject: ...
    def student_profile(
        self,
        *,
        actor: StaffActorContext,
        name: str | None,
        student_number: str | None,
        fields: tuple[str, ...],
        as_of: AwareDatetime | None,
    ) -> JsonObject: ...
    def query(
        self,
        *,
        actor: StaffActorContext,
        fact_kind: str,
        query_text: str | None,
        student_number: str | None = None,
        reference: str | None,
        related_field: str | None,
        related_ref: str | None,
        as_of: AwareDatetime | None,
        limit: int,
        include_quality: bool = False,
    ) -> JsonObject: ...
    def describe(self, *, actor: StaffActorContext, fact_kind: str) -> JsonObject: ...


def _descriptor(
    action: str,
    kind: Literal["read", "prepare", "result"],
    description: str,
    schema: type[FrozenContractModel],
) -> ToolDescriptor:
    return ToolDescriptor(
        name=f"{MODULE}.{action}",
        owner=MODULE,
        kind=kind,
        description=description,
        parameters=schema.model_json_schema(),
        load_on_demand=action in {"read", "describe", "prepare", "receipt"},
    )


def _safe_material(view: JsonObject) -> JsonObject:
    """Keep readable evidence while confining opaque references to tool_context."""

    def clean(
        value: JsonValue,
        *,
        fact_kind: str | None = None,
        fields: bool = False,
        source: bool = False,
    ) -> JsonValue:
        if isinstance(value, dict):
            record_kind = value.get("fact_kind")
            if isinstance(record_kind, str):
                fact_kind = record_kind
            public: JsonObject = {}
            private: JsonObject = {}
            labels: JsonObject = {}
            enum_values: JsonObject = {}
            for key, item in value.items():
                if key in {
                    "source_reference",
                    "source_trace_reference",
                    "trace_reference",
                    "source_key",
                } or (source and key == "reference"):
                    continue
                descriptor = None
                if fields and isinstance(fact_kind, str):
                    try:
                        descriptor = family_field_descriptor(key, fact_kind)
                    except ValueError:
                        pass
                if descriptor is not None:
                    labels[key] = descriptor["label"]
                    if descriptor["type"] == "reference":
                        private[key] = item
                        continue
                    if descriptor["type"] == "select":
                        options = descriptor["options"]
                        for option in options if isinstance(options, list) else ():
                            if isinstance(option, dict) and option.get("value") == item:
                                enum_values[key] = item
                                item = option["label"]
                                break
                if key in {
                    "reference",
                    "target_ref",
                    "creation_ref",
                    "proposal_ref",
                    "version",
                    "expected_version",
                    "target_version",
                }:
                    private[key] = item
                else:
                    public[key] = clean(
                        item,
                        fact_kind=fact_kind,
                        fields=key in {"fields", "content"},
                        source=key == "source",
                    )
            if labels:
                public["field_labels"] = labels
            if enum_values:
                private["field_values"] = enum_values
            if private:
                public["tool_context"] = private
            return public
        if isinstance(value, list):
            return [clean(item, fact_kind=fact_kind) for item in value]
        return value

    result = clean(view)
    if not isinstance(result, dict):
        raise ValueError("invalid family fact view")
    return result


def _selected_read_material(view: JsonObject, fields: tuple[str, ...]) -> JsonObject:
    """Select already authorized values without altering formal cards or read evidence."""
    selected = deepcopy(view)
    selected["requested_fields"] = list(fields)
    quality = selected.get("quality")
    if isinstance(quality, dict):
        quality.pop("details", None)
    records = selected.get("records")
    if isinstance(records, list):
        for record in records:
            if not isinstance(record, dict):
                continue
            returned: set[str] = set()
            for key in ("fields", "content"):
                values = record.get(key)
                if isinstance(values, dict):
                    normalized = dict(values)
                    for name, value in values.items():
                        if value is None and name.endswith("_source_key"):
                            # The existing DTO leaves absent canonical relations
                            # unrenamed. Preserve that authorized null, never a raw key.
                            normalized.setdefault(name.removesuffix("_source_key") + "_ref", None)
                    record[key] = {
                        name: value for name, value in normalized.items() if name in fields
                    }
                    returned.update(normalized)
            unreturned: list[JsonValue] = [field for field in fields if field not in returned]
            if unreturned:
                record["unreturned_fields"] = unreturned
    # Minimize before label/enum expansion so omitted values cannot survive in
    # field_labels or tool_context.field_values. Readable identity labels remain.
    return _safe_material(selected)


class FamilyStaffToolGateway(FactConversationActions):
    # Bound by trusted composition; a tool argument cannot select its executor owner.
    module_ref = MODULE
    maintenance_fact_kinds = F3_FACT_KINDS
    prepare_input_model = FactChangeInput
    material = staticmethod(_safe_material)

    descriptors = (
        _descriptor(
            "student_profile",
            "read",
            "按用户给出的姓名或本校学号查获权学生基本资料；至少一项，同时提供时取交集；名称原样匹配正式资料，有界完成同名消歧和所问资料关联。只返回所选姓名、学号、校区、地区、在校状态或住校方式；不能核对的候选不视为不匹配，不自动选择同名学生。",
            FamilyStudentProfileInput,
        ),
        _descriptor(
            "read",
            "read",
            "读取当前职责获准的学生、在校、联系人、已核实家校关系或可选地区事实。引用仅用于后续工具选择。",
            FamilyModelReadInput,
        ),
        _descriptor(
            "describe",
            "read",
            "查看指定 F3 事实族的字段和获准创建项；不授予维护权限。"
            "返回的creation_choices仅是获准新增槽位，不代表已有对象的更新许可；空列表也不证明没有维护权。",
            FamilyDescribeInput,
        ),
        _descriptor(
            "prepare",
            "prepare",
            "准备一项正式事实修改候选；只预检，必须由用户在完整差异中明确确认。",
            FamilyModelPrepareInput,
        ),
        _descriptor(
            "own_receipts",
            "read",
            "列出本人在当前学校事实读取范围内仍可核验的有限历史回执；不恢复维护权。",
            FamilyOwnReceiptsInput,
        ),
        _descriptor(
            "own_receipt",
            "read",
            "用用户提供或先前工具返回的原receipt_ref直接核对本人原操作，无需先列目录；"
            "原引用未知时可用own_receipts查找。每次重验当前身份、本人归属和相关权限；"
            "用户同时询问当前事实时，才按返回对象引用另行读取。"
            "目标按target_offset/target_limit有界返回，仅在任务需要时按read_next续取；"
            "未返回目标不表示不存在，每页仍核验原回执及当前权限。",
            FamilyOwnReceiptInput,
        ),
        _descriptor(
            "receipt",
            "result",
            "只读核对本对话原正式修改回执；可用已返回的修改编号或原引用，"
            "都省略时核对本对话最近准备项。指定项不可用时不改查最近项；"
            "历史依据失效不妨碍按原操作核验，结果未知时不重新执行。",
            FamilyReceiptInput,
        ),
    )

    def __init__(
        self,
        *,
        resolve_actor: Callable[[str], ProductActor],
        query: FamilyStaffQueryPort,
        product: DirectoryFactProductService,
        retention_seconds: int = 86400,
    ) -> None:
        super().__init__(retention_seconds=retention_seconds)
        self.resolve_actor, self.query, self.product = resolve_actor, query, product

    def identity(self, ref: str) -> ProductActor:
        identity = self.resolve_actor(ref)
        if type(identity.actor) is not StaffActorContext:
            raise PermissionError("F3 Staff capability requires StaffActorContext")
        return identity

    def for_turn(self, actor_context_ref: str):
        return _FamilyStaffTurn(self, actor_context_ref)

    def execute(self, call: ModelToolCall, context: ToolCallContext) -> ToolExecutionResult:
        try:
            identity = self.identity(context.actor_context_ref)
            if call.name == f"{self.module_ref}.student_profile":
                payload = FamilyStudentProfileInput.model_validate_json(json.dumps(call.arguments))
                view = self.query.student_profile(
                    actor=identity.actor,
                    name=payload.name,
                    student_number=payload.student_number,
                    fields=payload.fields,
                    as_of=payload.as_of,
                )
                material_view = {
                    key: value for key, value in view.items() if key != "evidence"
                }
                quality = material_view.get("quality")
                if isinstance(quality, dict):
                    material_view["quality"] = {
                        field: {key: value for key, value in item.items() if key != "details"}
                        if isinstance(item, dict) else item
                        for field, item in quality.items()
                    }
                material = answer_evidence_material(_safe_material(material_view))
                return ToolExecutionResult(
                    model_content=material,
                    fact_card={**view, "family_owner": self.module_ref},
                )
            if call.name == f"{self.module_ref}.read":
                payload = FamilyModelReadInput.model_validate_json(json.dumps(call.arguments))
                if payload.fact_kind not in F3_FACT_KINDS:
                    raise ValueError("unsupported F3 fact kind")
                view = self.query.query(
                    actor=identity.actor,
                    fact_kind=payload.fact_kind,
                    query_text=payload.query_text,
                    **(
                        {"student_number": payload.student_number}
                        if payload.student_number is not None
                        else {}
                    ),
                    reference=payload.reference,
                    related_field=payload.related_field,
                    related_ref=payload.related_ref,
                    as_of=payload.as_of,
                    limit=payload.limit,
                    include_quality=payload.include_quality,
                )
                return ToolExecutionResult(
                    model_content=answer_evidence_material(
                        _selected_read_material(view, payload.fields)
                    ),
                    fact_card={**view, "family_owner": self.module_ref},
                )
            if call.name == f"{self.module_ref}.describe":
                payload = FamilyDescribeInput.model_validate(call.arguments)
                if payload.fact_kind not in F3_FACT_KINDS:
                    raise ValueError("unsupported F3 fact kind")
                view = self.query.describe(actor=identity.actor, fact_kind=payload.fact_kind)
                return ToolExecutionResult(model_content=_safe_material(view), fact_card=view)
            if call.name == f"{self.module_ref}.prepare":
                try:
                    payload = self.prepare_input_model.model_validate_json(
                        json.dumps(call.arguments)
                    )
                except ValidationError as error:
                    return ToolExecutionResult(
                        model_content={
                            "status": "rejected",
                            "reason": "参数、对象或候选不可用；没有执行写入。",
                            "code": "invalid_arguments",
                            "errors": _prepare_argument_errors(error, self.prepare_input_model),
                        }
                    )
                if payload.fact_kind not in self.maintenance_fact_kinds:
                    raise ValueError("F3 Staff tool cannot prepare another fact family")
                proposal, action_number = self.prepare_action(
                    context,
                    identity,
                    lambda: self.product.prepare(
                        actor=identity.actor,
                        session_binding=identity.session_binding,
                        payload=payload,
                        request_id=context.request_id,
                    ),
                )
                view = proposal.model_dump(mode="json")
                view["family_owner"] = self.module_ref
                material = self.material(view)
                tool_context = cast(JsonObject, material["tool_context"])
                tool_context.pop("proposal_ref", None)
                tool_context["action_number"] = action_number
                tool_context["action_owner"] = self.module_ref
                return ToolExecutionResult(
                    model_content=material,
                    fact_card=view,
                    continuation=self.result_continuation(
                        context=context,
                        original=identity,
                        resolve_actor=self.identity,
                        product=self.product,
                        reference=proposal.proposal_ref,
                        action_number=action_number,
                        module_ref=self.module_ref,
                    ),
                    module_result=self._result(proposal.proposal_ref, 1, view, action=True),
                )
            if call.name == f"{self.module_ref}.own_receipts":
                payload = FamilyOwnReceiptsInput.model_validate(call.arguments)
                page = self.product.own_receipts(
                    actor=identity.actor,
                    session_binding=identity.session_binding,
                    request_id=context.request_id,
                    limit=payload.limit,
                )
                view = page.model_dump(mode="json")
                return ToolExecutionResult(
                    model_content=own_receipts_model_material(
                        view, detail_tool=f"{self.module_ref}.own_receipt"
                    ),
                    fact_card=view,
                )
            if call.name == f"{self.module_ref}.own_receipt":
                payload = FamilyOwnReceiptInput.model_validate(call.arguments)
                receipt = self.product.own_receipt(
                    actor=identity.actor,
                    session_binding=identity.session_binding,
                    receipt_ref=payload.receipt_ref,
                    request_id=context.request_id,
                    target_offset=payload.target_offset,
                    target_limit=payload.target_limit,
                )
                view = receipt.model_dump(mode="json")
                return ToolExecutionResult(
                    model_content=own_receipt_model_material(
                        view,
                        target_offset=payload.target_offset,
                        include_audit=payload.include_audit,
                        target_limit=payload.target_limit,
                        detail_tool=f"{self.module_ref}.own_receipt",
                    ),
                    fact_card=view,
                )
            if call.name == f"{self.module_ref}.receipt":
                payload = FamilyReceiptInput.model_validate(call.arguments)
                action_number = payload.action_number
                if action_number is not None:
                    ref = self._action_reference(context, identity, action_number)
                elif payload.proposal_ref is not None:
                    ref = payload.proposal_ref
                else:
                    recent = self.recent_action(context, identity)
                    action_number, ref = recent if recent is not None else (None, None)
                if ref is None:
                    return ToolExecutionResult(
                        model_content={
                            "status": "clarification_required",
                            "reason": "本对话修改编号或原回执引用不可用，请核对原操作；"
                            "不改查其他最近项或重新执行。",
                        }
                    )
                receipt = self.product.receipt(
                    actor=identity.actor,
                    session_binding=identity.session_binding,
                    proposal_ref=ref,
                    request_id=context.request_id,
                )
                view = receipt.model_dump(mode="json")
                if action_number is not None:
                    if self._action_reference(context, identity, action_number) != ref:
                        raise ValueError("original action selection expired")
                else:
                    action_number = self._remember_action(context, identity, ref)
                material = receipt_model_material(view, action_number=action_number)
                cast(JsonObject, material["tool_context"])["action_owner"] = self.module_ref
                return ToolExecutionResult(
                    model_content=material,
                    fact_card=view,
                )
            raise AssistantRuntimeError("capability not installed")
        except PermissionError as error:
            return ToolExecutionResult(
                model_content={
                    "status": "denied",
                    "reason": "本次未能获准核对原回执，不能据此判断原操作结果；不重新执行。"
                    if call.name
                    in {
                        self.module_ref + ".receipt",
                        self.module_ref + ".own_receipt",
                        self.module_ref + ".own_receipts",
                    }
                    else "当前 Staff 身份未获准此事实范围或字段。",
                    **(
                        {"code": error.code, "reason": error.reason}
                        if call.name == self.module_ref + ".prepare"
                        and isinstance(error, PrepareAccessDenied)
                        else {}
                    ),
                },
                history_execution_note="read_returned_no_values"
                if call.name
                in {
                    self.module_ref + ".read",
                    self.module_ref + ".student_profile",
                    self.module_ref + ".describe",
                }
                else None,
            )
        except (ValueError, ContactProductUnavailable) as error:
            return ToolExecutionResult(
                model_content={
                    **(
                        {"code": "owner_rejected"}
                        if call.name == self.module_ref + ".prepare"
                        else {}
                    ),
                    "status": "rejected",
                    "reason": "本次未能核对原回执，不能据此判断原操作结果；不重新执行。"
                    if call.name
                    in {
                        self.module_ref + ".receipt",
                        self.module_ref + ".own_receipt",
                        self.module_ref + ".own_receipts",
                    }
                    else "参数、对象或候选不可用；没有执行写入。",
                    **(
                        {"code": "preflight_rejected", "reason": error.reason}
                        if call.name == self.module_ref + ".prepare"
                        and isinstance(error, FactPreflightRejected)
                        else {}
                    ),
                }
            )

    def handle(self, request: ModuleCallV1) -> ModuleResultV1:
        raise AssistantRuntimeError("use family fact tool contract")

    def recover(self, request: ModuleRecoveryCallV1) -> ModuleResultV1:
        raise ContactProductUnavailable("no recovery mutation")

    def confirm(self, request: ModuleConfirmationCallV1) -> ModuleResultV1:
        ref = request.action_offer_ref.removeprefix("action_")
        if (
            request.module_ref != self.module_ref
            or request.action_offer_ref != "action_" + ref
            or request.module_state_ref != "state_" + ref
            or request.state_revision != 1
        ):
            raise ContactProductUnavailable("invalid confirmation binding")
        identity = self.identity(request.actor_context_ref)
        receipt = self.product.confirm(
            actor=identity.actor,
            session_binding=identity.session_binding,
            proposal_ref=ref,
            request_id=uuid4(),
        )
        return self._result(
            ref, 2, receipt.model_dump(mode="json"), check=receipt.status == "unknown"
        )

    def check_result(self, request: ModuleResultCheckCallV1) -> ModuleResultV1:
        ref = request.result_check_ref.removeprefix("receipt_")
        if request.module_ref != self.module_ref or request.result_check_ref != "receipt_" + ref:
            raise ContactProductUnavailable("invalid result binding")
        identity = self.identity(request.actor_context_ref)
        receipt = self.product.receipt(
            actor=identity.actor,
            session_binding=identity.session_binding,
            proposal_ref=ref,
            request_id=uuid4(),
        )
        return self._result(
            ref, 2, receipt.model_dump(mode="json"), check=receipt.status == "unknown"
        )

    def _result(
        self,
        ref: str,
        revision: int,
        payload: JsonObject,
        *,
        action: bool = False,
        check: bool = False,
    ) -> ModuleResultV1:
        return ModuleResultV1(
            version="1",
            validation_receipt=ValidationReceiptV1(
                status="accepted", safe_result_family=self.module_ref
            ),
            locked_notice=None,
            staged_state_ref="state_" + ref,
            state_revision=revision,
            capsule_update=CapsuleUnchangedV1(kind="unchanged"),
            presentation_mode="direct",
            display_kind="card",
            display_payload=payload,
            display_template=None,
            field_map=None,
            model_safe_narration=None,
            slot_map=None,
            fallback_presentation=None,
            model_safe_note=None,
            action_offer=ActionOfferV1(
                action_offer_ref="action_" + ref, safe_label="已核对对象和完整差异，确认正式修改"
            )
            if action
            else None,
            recovery_control=None,
            result_check_handle=ResultCheckHandleV1(result_check_ref="receipt_" + ref)
            if check
            else None,
            safe_error_projection=None,
            diagnostic_ref=None,
        )


class _FamilyStaffTurn:
    def __init__(self, owner: FamilyStaffToolGateway, actor_context_ref: str) -> None:
        self.owner = owner
        self.actor_context_ref = actor_context_ref
        self._checks: list[Callable[[], bool]] = []

    @property
    def descriptors(self) -> tuple[ToolDescriptor, ...]:
        return self.owner.descriptors

    def validation_scope(self):
        return validation_batch()

    def revalidate(self, actor_context_ref: str) -> None:
        if actor_context_ref != self.actor_context_ref:
            raise AssistantRuntimeError("context changed")
        self.owner.identity(actor_context_ref)
        with validation_batch():
            for check in self._checks:
                if not check():
                    raise AssistantRuntimeError("F3 Staff result changed or authorization expired")

    def execute(self, call: ModelToolCall, context: ToolCallContext) -> ToolExecutionResult:
        self.revalidate(context.actor_context_ref)
        evidence = None
        backend = getattr(self.owner.query, "evidence", None)
        if (
            call.name
            in {self.owner.module_ref + ".read", self.owner.module_ref + ".student_profile"}
            and backend is not None
        ):
            identity = self.owner.identity(context.actor_context_ref)
            with backend.capture(
                actor=identity.actor, current_time=call.arguments.get("as_of") is None
            ) as capture:
                result = self.owner.execute(call, context)
                evidence = (
                    capture.seal()
                    if _evidence_complete(result.fact_card or result.model_content)
                    else None
                )
                # F3's older evidence signature does not cover the separate
                # quality projection. Until it does, compare a fresh exact
                # authorized result before each model release/use.
                if call.arguments.get("include_quality") is True or isinstance(
                    (result.fact_card or {}).get("quality"), dict
                ):
                    evidence = None
        else:
            result = self.owner.execute(call, context)
        if result.model_content.get("status") in {
            "denied",
            "rejected",
            "source_unavailable",
            "clarification_required",
        }:
            return result
        expected = _digest(result.fact_card or result.model_content)
        saved = call.model_copy(deep=True)
        fact_card = result.fact_card or {}
        tool_context = result.model_content.get("tool_context")
        action_number = (
            tool_context.get("action_number") if isinstance(tool_context, dict) else None
        )

        def current() -> bool:
            identity = self.owner.identity(context.actor_context_ref)
            if evidence is not None:
                return evidence.check(identity.actor)
            if saved.name == self.owner.module_ref + ".student_profile":

                def require_identity() -> None:
                    current_identity = self.owner.identity(context.actor_context_ref)
                    if (
                        current_identity.actor != identity.actor
                        or current_identity.session_binding != identity.session_binding
                    ):
                        raise AssistantRuntimeError("Student profile identity changed")

                def reread_profile() -> bool:
                    require_identity()
                    latest = self.owner.execute(saved, context)
                    require_identity()
                    return _digest(latest.fact_card or latest.model_content) == expected

                # Retain the entire authorized reread, including F2 campus data.
                # Only identical dependencies within one release boundary merge.
                return validate_fallback_read(
                    (
                        id(self.owner),
                        id(self.owner.resolve_actor),
                        context.actor_context_ref,
                        identity.actor,
                        identity.session_binding,
                        saved.name,
                        json.dumps(saved.arguments, sort_keys=True),
                        expected,
                    ),
                    reread_profile,
                )
            if saved.name in {
                self.owner.module_ref + ".own_receipts",
                self.owner.module_ref + ".own_receipt",
            }:

                def require_identity() -> None:
                    current_identity = self.owner.identity(context.actor_context_ref)
                    if (
                        current_identity.actor != identity.actor
                        or current_identity.session_binding != identity.session_binding
                    ):
                        raise PermissionError("Own receipt identity changed")

                page_selection = (
                    FamilyOwnReceiptsInput.model_validate(saved.arguments)
                    if saved.name == self.owner.module_ref + ".own_receipts"
                    else None
                )
                detail_selection = (
                    FamilyOwnReceiptInput.model_validate(saved.arguments)
                    if saved.name == self.owner.module_ref + ".own_receipt"
                    else None
                )
                validate_own_receipt_read(
                    self.owner.product,
                    actor=identity.actor,
                    session_binding=identity.session_binding,
                    request_id=context.request_id,
                    expected=fact_card,
                    require_identity=require_identity,
                    identity_binding=(id(self.owner.resolve_actor), context.actor_context_ref),
                    limit=page_selection.limit if page_selection is not None else None,
                    receipt_ref=detail_selection.receipt_ref
                    if detail_selection is not None
                    else None,
                    target_offset=detail_selection.target_offset
                    if detail_selection is not None
                    else 0,
                    target_limit=detail_selection.target_limit
                    if detail_selection is not None
                    else 20,
                )
                return True
            if saved.name == self.owner.module_ref + ".prepare":
                ref = fact_card.get("proposal_ref")
                if not isinstance(ref, str) or not isinstance(action_number, int):
                    return False
                if self.owner._action_reference(context, identity, action_number) != ref:  # pyright: ignore[reportPrivateUsage]
                    return False
                view = self.owner.product.proposal(
                    actor=identity.actor,
                    session_binding=identity.session_binding,
                    proposal_ref=ref,
                ).model_dump(mode="json")
                view["family_owner"] = self.owner.module_ref
            elif saved.name == self.owner.module_ref + ".receipt":
                ref = fact_card.get("reference")
                if not isinstance(ref, str) or not isinstance(action_number, int):
                    return False
                if self.owner._action_reference(context, identity, action_number) != ref:  # pyright: ignore[reportPrivateUsage]
                    return False
                view = self.owner.product.receipt(
                    actor=identity.actor,
                    session_binding=identity.session_binding,
                    proposal_ref=ref,
                    request_id=context.request_id,
                ).model_dump(mode="json")
            else:
                latest = self.owner.execute(saved, context)
                view = latest.fact_card or latest.model_content
            return _digest(view) == expected

        self._checks.append(current)
        return replace(
            result,
            history_check=HistoryCheck(
                current,
                revalidate=current,
                revalidation_covers_content=True,
                key=hashlib.sha256(
                    (
                        context.actor_context_ref
                        + saved.name
                        + expected
                        + (
                            json.dumps(saved.arguments, sort_keys=True)
                            if saved.name == self.owner.module_ref + ".student_profile"
                            else ""
                        )
                    ).encode()
                ).hexdigest(),
            ),
        )


def _evidence_complete(value: JsonValue) -> bool:
    # A caught auxiliary failure must remain on the full reread path, so recovery
    # cannot be hidden by the proof of only the successfully observed dependencies.
    if isinstance(value, dict):
        return all(_evidence_complete(item) for item in value.values())
    if isinstance(value, list):
        return all(_evidence_complete(item) for item in value)
    return value not in {"source_unavailable", "incomplete"} if isinstance(value, str) else True


def _digest(value: JsonObject) -> str:
    def stable(item: JsonValue) -> JsonValue:
        if isinstance(item, dict):
            return {
                key: stable(child)
                for key, child in item.items()
                if key not in {"queried_at", "factual_at", "family_owner"}
            }
        if isinstance(item, list):
            return [stable(child) for child in item]
        return item

    return hashlib.sha256(
        json.dumps(stable(value), sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


__all__ = [
    "MODULE",
    "FamilyReadInput",
    "FamilyDescribeInput",
    "FamilyReceiptInput",
    "FamilyStaffQueryPort",
    "FamilyStaffToolGateway",
]
