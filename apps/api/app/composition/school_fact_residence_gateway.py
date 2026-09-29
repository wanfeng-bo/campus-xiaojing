"""Finite F6 tools: query, prepare and receipt remain separate owner operations."""

import json
from collections.abc import Callable
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, Literal, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AwareDatetime, Field, JsonValue, ValidationError, model_validator

from app.composition.school_fact_answer_evidence import answer_evidence_material
from app.composition.school_fact_directory_context import bind_directory_identity
from app.composition.school_fact_directory_product import FactChangeInput
from app.composition.school_fact_family_staff_gateway import FamilyStaffToolGateway
from app.composition.school_fact_read_evidence import validate_fallback_read
from app.composition.school_fact_residence_product import ResidenceBatchInput
from app.platform.assistant.contracts import JsonObject, ModuleResultV1
from app.platform.assistant.runtime import AssistantRuntimeError, ContinuationEvidence, HistoryCheck
from app.platform.assistant.tool_loop import ModelToolCall, ToolDescriptor, ToolExecutionResult
from app.platform.contracts import FrozenContractModel
from app.platform.school_facts.f6_policy import F6_FACT_KINDS
from app.platform.school_facts.school_scope_reference import school_scope_reference_coordinates

MODULE = "school_facts.residence"
FAMILY_NAME = "school_facts.family.residence"
RESIDENCE_INSTRUCTION = (
    "住宿楼、房间、床位和正式安排使用residence查询。"
    "职工可按正式名称或受控引用精确查询；仅在校关系支持用正式学号定位学生，"
    "姓名与学号同时提供时取交集，同名或同号候选须先消歧；续查使用正式结果返回的引用。"
    "name按资料类型匹配对象自身正式名称，不是楼/房间/床位拼成的路径，也不是业务编号；"
    "楼房床和住宿查询可用name与related_ref取交集，例如已选房间引用加床位名称。"
    "已返回且获权的名称、楼房床信息和正式引用可直接复用，只补充缺失信息或消歧。"
    "未录入、走读、已结束、正式空缺及冲突不同；楼房床是正式安排，不证明实际到宿。"
    "同一请求若既选定对象又要求另一项住宿名单，须分别按各自范围处理；"
    "前项对象选择不能无声缩小后项明确要求的获准集合。集合有下一页时继续获准续读，"
    "尚未查完就如实说明，不能把局部结果说成全部。"
    "学生在某时点无床位记录，不能推断走读或从未入住；若用户询问其住宿情况，"
    "须在同一事实时点按获权范围补查在校关系中的寄宿方式，再合并回答。"
    "在校关系无权限、歧义或未录入时，只说明床位查询结果，不能推断走读。"
    "混合楼的寄宿区域以正式用途资料为准。回答先说所问内容，来源放在依据中；引用用于工具续查。"
    "写入必须先prepare显示完整差异并由用户确认；回执核对原操作，当前事实需用query重新读取。"
    "需要维护时，describe与prepare_transfer可直接调用；精细定位locate_change及通用prepare"
    "按能力标识检索后调用；"
    "describe返回字段、获准新增槽位和学校时区。"
    "已有正式学号、含时区生效时点及同楼目标房床正式名称的换床，可用prepare_transfer"
    "一次完成获权定位、创建槽位核对及整批预检；它只产生待确认方案，不执行修改。"
    "用户若明确说了旧楼、旧房或旧床，须把各项原样交给prepare_transfer核对；"
    "不能为绕过核对而删掉或改写用户旧名称。唯一原安排与用户旧名称不同，且该名称未指向"
    "另一正式对象时，工具可把名称差异明确写入待确认方案；必须请用户核对正式旧值，"
    "不能称作别名已匹配或修改已完成。实际对象冲突仍须澄清。"
    "已有明确对象、日期和修改意图后，prepare按当前权限核对完整前后、住宿资格和关联影响，"
    "不要求模型重复逐项业务校验；换床用一次prepare同批结束旧安排并登记新安排。"
    "职工已知正式学号和含学校时区的时点，准备换床时可选择locate_change，"
    "按旧正式安排限定同楼目标房床；目标正式名称不确定时可省略或按原话尝试，"
    "它返回有界获权正式候选供你判断或追问，不会把口语名称自动认作正式别名。"
    "定位只是只读坐标，不代替获准新增槽位的describe、完整prepare或用户确认；"
    "不确定的对象、范围和时间仍先查询或澄清。"
    "用户未写时区的口语日期，以本校正式fact_timezone_name为解释依据；"
    "首次带日期查询或维护前，先无日期查询或describe取得当前学校时区，再转换为含偏移的参数。"
    "学校时区未知时先澄清，不能凭中文、页面显示或浏览器时区猜测；日期回答优先用time_labels。"
)

FAMILY_RESIDENCE_INSTRUCTION = (
    "家长按本次对话目标，通过school_facts.family.residence查询获权孩子的"
    "正式住宿安排与住校方式；可在同一对话分别查多个获权孩子，无需切换界面。"
    "该结果已包含同一事实时点的在校关系住校属性；无当前床位记录时，"
    "按返回的住校方式区分走读、寄宿但未登记床位、属性未知，不能凭空推断从未入住。"
    "床位是学校正式安排，不证明实际到宿。来源、事实时间、覆盖不足和冲突按结果说明。"
    "家长查询不含其他学生或室友名单，也不产生住宿维护权限。"
)

ResidenceKind = Literal[
    "dormitory_building_profile",
    "dorm_room",
    "dorm_bed",
    "student_bed_residence",
    "fact_quality",
    "student_school_relationship",
]

_NAME_HELP = {
    "dormitory_building_profile": "寄宿用途资料自身的display_name，不是地点路径。",
    "dorm_room": "房间自身正式room_name，不包含楼名或校区路径。",
    "dorm_bed": "床位自身正式display_name，不是business_code或楼房床路径。",
    "student_bed_residence": "学生姓名student_name，不是房间名、床位名或学号。",
    "student_school_relationship": "学生姓名；可与student_number取交集，同名或同号须消歧。",
    "fact_quality": "不支持name；使用已返回的依据reference或寄宿对象related_ref。",
}


class ResidencePrepareInput(ResidenceBatchInput):
    additional_changes: tuple[FactChangeInput, ...] = Field(default=(), max_length=299)

    @model_validator(mode="after")
    def exact_owner_batch(self) -> Any:
        ResidenceBatchInput.model_validate_json(self.model_dump_json())
        targets = [
            (c.fact_kind, c.target_ref or c.creation_ref) for c in (self, *self.additional_changes)
        ]
        if len(set(targets)) != len(targets):
            raise ValueError("同批不可重复目标。")
        return self


def _prepare_schema() -> dict[str, Any]:
    schema = ResidencePrepareInput.model_json_schema()
    schema["properties"]["fact_kind"] = {
        "type": "string",
        "enum": sorted(F6_FACT_KINDS | {"fact_quality", "student_school_relationship"}),
    }
    return schema


def _view_fingerprint(value: Any) -> str:
    def stable(item: Any) -> Any:
        if isinstance(item, dict):
            data = cast(dict[str, Any], item)
            return {
                key: stable(child)
                for key, child in data.items()
                if key not in {"queried_at", "factual_at"}
            }
        if isinstance(item, list):
            values = cast(list[Any], item)
            return [stable(child) for child in values]
        return item

    return json.dumps(stable(value), sort_keys=True, ensure_ascii=False)


class FamilyResidenceInput(FrozenContractModel):
    as_of: AwareDatetime | None = Field(
        default=None, description="明确询问的历史或未来时点；省略表示当前。"
    )


class ResidenceQueryInput(FrozenContractModel):
    fact_kind: ResidenceKind = "student_bed_residence"
    reference: str | None = Field(default=None, min_length=8, max_length=1600)
    name: str | None = Field(
        default=None,
        min_length=1,
        max_length=200,
        description="按fact_kind精确匹配正式名称：寄宿用途display_name、房间room_name、床位display_name、"
        "住宿记录的学生姓名；不是业务编号或楼房床路径。在校关系支持学生姓名，质量依据不支持。",
    )
    student_number: str | None = Field(
        default=None,
        min_length=1,
        max_length=1000,
        description="仅在校关系查询使用的正式学号；按获准字段、范围和所问时点精确匹配，同号须消歧。",
    )
    related_ref: str | None = Field(
        default=None,
        min_length=8,
        max_length=255,
        description="正式查询返回的学生、房间、床位或地点引用，不接受猜造编号。"
        "楼房床和住宿查询可与name取交集，例如房间引用加床位自身名称；在校关系须按related_field选关联。",
    )
    related_field: (
        Literal["student_ref", "primary_campus_ref", "administrative_campus_ref"] | None
    ) = Field(default=None, description="仅在校关系查询使用，与 related_ref 成对。")
    as_of: AwareDatetime | None = None
    cursor: str | None = Field(default=None, max_length=8000)
    limit: int = Field(default=25, strict=True, ge=1, le=100)

    @model_validator(mode="after")
    def owned_number_selection(self) -> Any:
        if self.fact_kind != "student_school_relationship":
            if self.student_number is not None or self.related_field is not None:
                raise ValueError("学号和关联字段仅用于学生在校关系。")
        else:
            inferred_student = self.related_ref is not None and self.related_ref.startswith(
                "f3:student:"
            )
            if (
                self.student_number is not None
                and not self.student_number.strip()
                or self.related_field is not None
                and self.related_ref is None
                or self.related_ref is not None
                and self.related_field is None
                and not inferred_student
                or sum(
                    (
                        bool(self.name or self.student_number),
                        bool(self.related_ref),
                        bool(self.reference),
                    )
                )
                > 1
                or self.cursor is not None
            ):
                raise ValueError("在校关系要求唯一选择和完整关联条件。")
        if self.fact_kind == "fact_quality" and self.name is not None:
            raise ValueError("质量依据不支持名称查询。")
        return self


class ResidenceDescribeInput(FrozenContractModel):
    fact_kind: ResidenceKind


class ResidenceLocateChangeInput(FrozenContractModel):
    """One student's formal old lodging and bounded same-building target choices."""

    student_number: str = Field(min_length=1, max_length=1000)
    as_of: AwareDatetime
    to_room_name: str | None = Field(default=None, min_length=1, max_length=200)
    to_bed_name: str | None = Field(default=None, min_length=1, max_length=200)


class ResidenceTransferInput(FrozenContractModel):
    """A formally identified same-building bed transfer, prepared but never executed."""

    student_number: str = Field(min_length=1, max_length=1000)
    effective_at: AwareDatetime
    from_building_name: str | None = Field(default=None, min_length=1, max_length=200)
    from_room_name: str | None = Field(default=None, min_length=1, max_length=200)
    from_bed_name: str | None = Field(default=None, min_length=1, max_length=200)
    to_room_name: str = Field(min_length=1, max_length=200)
    to_bed_name: str = Field(min_length=1, max_length=200)
    creation_ref: str | None = Field(
        default=None,
        min_length=8,
        max_length=255,
        description="仅当获准创建项有歧义时，从当前describe或本工具候选原样选一项；不能猜造。",
    )


def _selection_failure(arguments: Any, *, describe: bool = False) -> ToolExecutionResult:
    """Only static public contract hints, never user values or exception diagnostics."""
    candidate = arguments.get("fact_kind") if isinstance(arguments, dict) else None
    kind = candidate if isinstance(candidate, str) and candidate in _NAME_HELP else None
    fields: list[JsonValue] = (
        ["fact_kind"] if describe else [str(field) for field in ResidenceQueryInput.model_fields]
    )
    if not describe:
        if kind != "student_school_relationship":
            fields = [f for f in fields if f not in {"student_number", "related_field"}]
        elif kind is not None:
            fields.remove("cursor")
        if kind == "fact_quality":
            fields.remove("name")
    return ToolExecutionResult(
        model_content={
            "status": "unsupported_selection",
            "code": "invalid_residence_selection",
            "message": (
                "该选择不符合寄宿查询合同；请按下列字段与组合修正，"
                "不要猜造引用或反复改猜名称。"
            ),
            "allowed_fields": fields,
            "selection_help": {
                "fact_kind": list(_NAME_HELP),
                "name": _NAME_HELP[kind]
                if kind is not None
                else "名称含义由fact_kind确定；请先选择资料类型。",
                "combine": (
                    "在校关系选择reference、姓名/学号、"
                    "或完整related_field+related_ref中的一组；姓名与学号可取交集。"
                )
                if kind == "student_school_relationship"
                else (
                    "楼房床及住宿支持name+related_ref交集；"
                    "质量依据仅用reference或寄宿对象related_ref。"
                ),
                "related_ref": (
                    "只能复用正式结果返回的引用；在校关系关联字段为student_ref、"
                    "primary_campus_ref或administrative_campus_ref。"
                ),
                "as_of": (
                    "省略表示当前；指定日期须含时区偏移，"
                    "学校时区可由describe或无日期query取得。"
                ),
                "reuse": "已有获权名称和引用可复用；分页cursor只续查原条件，同名或同号须消歧。",
            },
        }
    )


def _public_result(result: Any, *, valid: Any) -> Any:
    public = result.public()
    if result.evidence is None:
        return ToolExecutionResult(
            model_content=answer_evidence_material(residence_model_material(public)),
            fact_card=public,
            history_execution_note="read_returned_no_values"
            if public.get("status") == "denied"
            else None,
        )
    check = HistoryCheck(valid, revalidate=valid, revalidation_covers_content=True)
    if not check.valid():
        raise AssistantRuntimeError("Residence evidence changed before use")
    return ToolExecutionResult(
        model_content=answer_evidence_material(residence_model_material(public)),
        fact_card=public,
        history_check=check,
    )


def residence_model_material(public: Any) -> Any:
    material = deepcopy(public)
    # Shared prepare owns action numbers and continuation. Supply only its
    # opaque proposal locator; it removes that locator before returning to the model.
    if isinstance(material.get("proposal_ref"), str):
        material.setdefault("tool_context", {})["proposal_ref"] = material.pop("proposal_ref")
    try:
        zone = ZoneInfo(
            material.get("fact_timezone_name")
            or material.get("summary", {}).get("fact_timezone_name")
        )
    except (KeyError, TypeError, ValueError, ZoneInfoNotFoundError):
        zone = None
    if zone is None:
        material["limitations"] = list(
            dict.fromkeys([*material.get("limitations", []), "school_timezone_unavailable"])
        )

    def label_times(value: Any, inherited: Any = zone) -> None:
        if isinstance(value, list):
            for child in cast(list[Any], value):
                label_times(child, inherited)
        elif isinstance(value, dict):
            mapping = cast(dict[str, Any], value)
            current_zone = inherited
            if "fact_timezone_name" in mapping:
                try:
                    current_zone = ZoneInfo(mapping["fact_timezone_name"])
                except (TypeError, ValueError, ZoneInfoNotFoundError):
                    current_zone = None
            for key, child in list(mapping.items()):
                if (
                    current_zone is not None
                    and key
                    in {
                        "as_of",
                        "factual_at",
                        "queried_at",
                        "valid_from",
                        "valid_to",
                        "effective_at",
                        "published_at",
                        "verified_at",
                        "review_due_at",
                    }
                    and isinstance(child, str)
                ):
                    try:
                        stamp = datetime.fromisoformat(child)
                        if stamp.utcoffset() is not None:
                            local = stamp.astimezone(current_zone)
                            mapping.setdefault("time_labels", {})[key] = (
                                f"{local:%Y-%m-%d %H:%M:%S}（{current_zone.key}，UTC{local:%z}）"
                            )
                    except ValueError:
                        pass
                else:
                    label_times(child, current_zone)

    label_times(material)
    coordinates: list[Any] = []
    for index, row in enumerate(material.get("records", [])):
        context: dict[str, Any] = {"record_index": index}
        for key in tuple(row):
            if key in {"reference", "version", "fact_kind"} or key.endswith("_ref"):
                context[key] = row.pop(key)
        if len(context) > 1:
            coordinates.append(context)
        evidence = {key: row.pop(key) for key in ("source", "quality") if key in row}
        if evidence:
            row["supporting_evidence"] = evidence
    if coordinates:
        material["tool_context"] = {"targets": coordinates}
    return material


class FamilyResidenceToolGateway:
    descriptors = (
        ToolDescriptor(
            name=FAMILY_NAME,
            owner="school_facts.f6",
            kind="read",
            description="查询本次目标孩子的正式楼、房间、床位和有效期；支持当前关系期间历史及已发布未来，来源质量不足会明确说明。无需孩子编号，不返回室友或其他学生。",
            parameters=FamilyResidenceInput.model_json_schema(),
        ),
    )

    def __init__(self, *, resolve_actor: Any, query: Any) -> None:
        self.resolve_actor, self.query = resolve_actor, query

    def for_turn(self, actor_context_ref: Any) -> Any:
        return _FamilyResidenceTurn(self, actor_context_ref)


class _FamilyResidenceTurn:
    def __init__(self, owner: Any, reference: Any) -> None:
        self._checks: list[Any]
        self.owner, self.actor_context_ref, self._checks = owner, reference, []

    @property
    def descriptors(self) -> Any:
        return self.owner.descriptors

    def revalidate(self, reference: Any) -> Any:
        if reference != self.actor_context_ref or any(not check() for check in self._checks):
            raise AssistantRuntimeError("Residence identity or evidence changed")

    def execute(self, call: Any, context: Any) -> Any:
        self.revalidate(context.actor_context_ref)
        if call.name != FAMILY_NAME:
            raise AssistantRuntimeError("Residence capability unavailable")
        try:
            args = FamilyResidenceInput.model_validate_json(json.dumps(call.arguments))
        except ValidationError:
            return ToolExecutionResult(
                model_content={
                    "status": "unsupported_selection",
                    "message": "请核对含时区的查询时点。",
                }
            )
        identity = self.owner.resolve_actor(context.actor_context_ref)
        actor = identity.actor
        with bind_directory_identity(context.actor_context_ref):
            result = self.owner.query.query(actor=actor, **args.model_dump())

        def current() -> Any:
            def fresh() -> Any:
                latest = self.owner.resolve_actor(self.actor_context_ref)
                with bind_directory_identity(self.actor_context_ref):
                    return latest == identity and result.evidence.valid(latest.actor)

            return validate_fallback_read(
                (FAMILY_NAME, self.actor_context_ref, actor, id(result.evidence)),
                fresh,
                time_window=getattr(result.evidence, "time_window", None),
            )

        if result.evidence is not None:
            self._checks.append(current)
        return _public_result(result, valid=current)


class ResidenceToolGateway(FamilyStaffToolGateway):
    module_ref = MODULE
    maintenance_fact_kinds = F6_FACT_KINDS | {"fact_quality"}
    prepare_input_model = ResidencePrepareInput
    material = staticmethod(residence_model_material)

    def _result(
        self,
        ref: str,
        revision: int,
        payload: JsonObject,
        *,
        action: bool = False,
        check: bool = False,
    ) -> ModuleResultV1:
        # F6 proposals are reviewed through the residence workbench's complete
        # impact view before its separate explicit confirmation. The generic
        # conversation control would otherwise confirm a summary directly.
        return super()._result(ref, revision, payload, action=False, check=check)

    descriptors = (
        ToolDescriptor(
            name=MODULE + ".query",
            owner=MODULE,
            kind="read",
            description="按对象自身正式名称、已获准对象或关联引用查询；name可与related_ref取交集，"
            "不是拼接地点路径。复用结果中的正式引用；同名需消歧，设施权限不包含住户名单。",
            parameters=ResidenceQueryInput.model_json_schema(),
        ),
        ToolDescriptor(
            name=MODULE + ".describe",
            owner=MODULE,
            kind="read",
            description="查看寄宿维护字段、当前获准创建项和学校时区，可直接与prepare配合；"
            "字段说明与创建引用不授予权限。",
            parameters=ResidenceDescribeInput.model_json_schema(),
            load_on_demand=False,
        ),
        ToolDescriptor(
            name=MODULE + ".locate_change",
            owner=MODULE,
            kind="read",
            description="按获准正式学号和含时区事实时点读取学生原住宿，并通过稳定关联限定同楼目标。"
            "目标房/床正式名称可选；精确无匹配时只返回有界获权正式候选，不自动选择口语别名或第一条。"
            "只读定位，不生成修改方案；之后仍须describe获准创建项、prepare完整预检和用户明确确认。",
            parameters=ResidenceLocateChangeInput.model_json_schema(),
            load_on_demand=True,
        ),
        ToolDescriptor(
            name=MODULE + ".prepare_transfer",
            owner=MODULE,
            kind="prepare",
            description="已知正式学号、含时区生效时点、同楼目标房间及床位正式名称时，"
            "用户明确给出的旧楼/房/床名称须原样提供，缺失的旧名称可省略；"
            "唯一原安排的名称差异会在待确认方案中明确列出，实际对象冲突不会准备；"
            "一次核对原安排和目标、获准创建项，并调用原正式预检生成整批待确认换床方案；"
            "遇到歧义、无权、质量或来源问题只返回状态，不擅自选对象或执行修改。",
            parameters=ResidenceTransferInput.model_json_schema(),
            load_on_demand=False,
        ),
        *(
            d.model_copy(
                update={
                    "name": MODULE + "." + d.name.rsplit(".", 1)[1],
                    "owner": MODULE,
                    **(
                        {
                            "parameters": _prepare_schema(),
                            "description": "按当前权限核对资格、完整前后和关联影响并准备修改差异；"
                            "已有明确对象与时点后，无须模型重复逐项业务校验。"
                            "换床需同批结束旧安排与登记新安排；"
                            "质量声明单独核对，必须由用户明确确认。",
                        }
                        if d.name.endswith(".prepare")
                        else {}
                    ),
                }
            )
            for d in FamilyStaffToolGateway.descriptors
            if d.name.rsplit(".", 1)[1] in {"prepare", "receipt", "own_receipt", "own_receipts"}
        ),
    )

    def __init__(
        self,
        *,
        resolve_actor: Any,
        query: Any,
        product: Any,
        describe_adapter: Any,
        retention_seconds: Any = 86400,
        quality_adapter: Any = None,
        eligibility_adapter: Any = None,
    ) -> None:
        super().__init__(
            resolve_actor=resolve_actor,
            query=query,
            product=product,
            retention_seconds=retention_seconds,
        )
        self.describe_adapter = describe_adapter
        self.quality_adapter = quality_adapter
        self.eligibility_adapter = eligibility_adapter
        self.residence_query = query

    def for_turn(self, actor_context_ref: Any) -> Any:
        return _ResidenceTurn(self, actor_context_ref, super().for_turn(actor_context_ref))

    def _prepare_transfer(
        self,
        call: Any,
        context: Any,
        *,
        prepare_execute: Callable[[ModelToolCall, Any], ToolExecutionResult],
    ) -> ToolExecutionResult:
        try:
            args = ResidenceTransferInput.model_validate_json(json.dumps(call.arguments))
        except ValidationError:
            return ToolExecutionResult(
                model_content={
                    "status": "unsupported_selection",
                    "message": "请提供正式学号、含时区的生效时点和同楼目标房间、床位的正式名称。",
                }
            )

        identity = self.identity(context.actor_context_ref)
        located = self._locate_change(
            ModelToolCall(
                call_id=call.call_id,
                name=MODULE + ".locate_change",
                arguments={
                    "student_number": args.student_number,
                    "as_of": args.effective_at.isoformat(),
                    "to_room_name": args.to_room_name,
                    "to_bed_name": args.to_bed_name,
                },
            ),
            context,
        )
        if located.model_content.get("status") != "located":
            return located
        original = located.model_content.get("original")
        if not isinstance(original, dict) or any(
            not isinstance(original.get(key), str)
            for key in ("building_name", "room_name", "bed_name")
        ):
            return ToolExecutionResult(
                model_content={"status": "incomplete", "stage": "original_residence"},
                history_check=located.history_check,
            )
        expected_old = {
            "building_name": args.from_building_name,
            "room_name": args.from_room_name,
            "bed_name": args.from_bed_name,
        }
        coordinates = located.model_content.get("tool_context")
        if not isinstance(coordinates, dict) or any(
            not isinstance(coordinates.get(key), str)
            for key in (
                "student_ref", "original_residence_ref", "target_bed_ref",
                "original_building_ref", "original_room_ref", "original_bed_ref",
            )
        ):
            return ToolExecutionResult(
                model_content={"status": "incomplete", "stage": "change_coordinates"},
                history_check=located.history_check,
            )

        checks = [located.history_check] if located.history_check is not None else []

        def proof_for(dependencies: tuple[HistoryCheck, ...]) -> HistoryCheck:
            def fresh() -> bool:
                return self.identity(context.actor_context_ref) == identity and all(
                    check.valid() for check in dependencies
                )

            return HistoryCheck(fresh, revalidate=fresh, revalidation_covers_content=True)

        mismatches = {
            field: name for field, name in expected_old.items()
            if name is not None and original[field] != name
        }
        selections = {
            "building_name": ("dormitory_building_profile", None),
            "room_name": ("dorm_room", coordinates["original_building_ref"]),
            "bed_name": ("dorm_bed", coordinates["original_room_ref"]),
        }
        # An unknown spelling is not an alias. Before exposing a discrepancy
        # preview, check only the user's stated old coordinates in the already
        # authorized original scope. A known different object still needs the
        # user's clarification; a weak/denied lookup cannot justify a preview.
        for field, name in mismatches.items():
            fact_kind, related_ref = selections[field]
            old_name_result = self.execute(
                ModelToolCall(
                    call_id=call.call_id,
                    name=MODULE + ".query",
                    arguments={
                        "fact_kind": fact_kind, "name": name,
                        "as_of": args.effective_at.isoformat(), "limit": 2,
                        **({"related_ref": related_ref} if related_ref is not None else {}),
                    },
                ),
                context,
            )
            if old_name_result.history_check is not None:
                checks.append(old_name_result.history_check)
            content = old_name_result.model_content
            status = content.get("status")
            if (
                status == "no_current_records"
                and content.get("records") == []
                and not content.get("has_more") and not content.get("truncated")
                and old_name_result.history_check is not None
            ):
                continue
            proof = proof_for(tuple(checks))
            if not proof.valid():
                raise AssistantRuntimeError("Residence original-name evidence changed")
            failure = (
                "object_mismatch" if status in {"available", "known"}
                else "incomplete" if status == "no_current_records"
                else status if status in {
                    "denied", "ambiguous", "partial", "conflict", "incomplete",
                    "source_unavailable", "unavailable", "unsupported_selection",
                } else "unavailable"
            )
            return ToolExecutionResult(
                model_content={
                    "status": failure, "stage": "original_residence",
                    **({"original": original} if failure != "denied" else {}),
                },
                history_check=proof,
            )

        reason = "按本次明确请求调整床位安排"
        if mismatches:
            labels = {"building_name": "旧楼", "room_name": "旧房", "bed_name": "旧床"}
            differences = "；".join(
                f"{labels[field]}：用户填写“{name}”，正式登记“{original[field]}”"
                for field, name in mismatches.items()
            )
            reason += (
                "。名称不一致：" + differences
                + "。确认前请核对：本方案调整的是上述正式登记的原住宿。"
            )
            if len(reason) > 1000:
                return ToolExecutionResult(
                    model_content={
                        "status": "unsupported_selection", "stage": "original_residence",
                        "original": original,
                        "message": "名称差异无法完整纳入本次确认，请先核对正式原住宿名称。",
                    },
                    history_check=proof_for(tuple(checks)),
                )

        described = self.execute(
            ModelToolCall(
                call_id=call.call_id,
                name=MODULE + ".describe",
                arguments={"fact_kind": "student_bed_residence"},
            ),
            context,
        )
        if described.history_check is not None:
            checks.append(described.history_check)
        proof = proof_for(tuple(checks))
        if not proof.valid():
            raise AssistantRuntimeError("Residence transfer evidence changed")
        description_status = described.model_content.get("status")
        if description_status is not None:
            allowed = {
                "denied", "source_unavailable", "unavailable", "partial", "conflict",
                "unsupported_selection", "ambiguous",
            }
            return ToolExecutionResult(
                model_content={
                    "status": description_status
                    if description_status in allowed else "unavailable",
                    "stage": "creation_slot",
                },
                history_check=proof,
            )

        choices = described.model_content.get("creation_choices")
        if not isinstance(choices, list) or any(
            not isinstance(choice, dict)
            or not isinstance(choice.get("creation_ref"), str)
            or not isinstance(choice.get("label"), str)
            for choice in choices
        ):
            return ToolExecutionResult(
                model_content={"status": "unavailable", "stage": "creation_slot"},
                history_check=proof,
            )
        if not choices:
            return ToolExecutionResult(
                model_content={"status": "denied", "stage": "creation_slot"},
                history_check=proof,
            )
        if args.creation_ref is None and len(choices) != 1:
            return ToolExecutionResult(
                model_content={
                    "status": "ambiguous",
                    "stage": "creation_slot",
                    "candidates": choices,
                },
                history_check=proof,
            )
        selected = args.creation_ref or choices[0]["creation_ref"]
        if selected not in {choice["creation_ref"] for choice in choices}:
            return ToolExecutionResult(
                model_content={"status": "unsupported_selection", "stage": "creation_slot"},
                history_check=proof,
            )

        prepared = prepare_execute(
            ModelToolCall(
                call_id=call.call_id,
                name=MODULE + ".prepare",
                arguments={
                    "fact_kind": "student_bed_residence",
                    "target_ref": coordinates["original_residence_ref"],
                    "operation": "end",
                    "fields": {},
                    "effective_at": args.effective_at.isoformat(),
                    "reason": reason,
                    "additional_changes": [
                        {
                            "fact_kind": "student_bed_residence",
                            "creation_ref": selected,
                            "operation": "upsert",
                            "fields": {
                                "student_ref": coordinates["student_ref"],
                                "bed_ref": coordinates["target_bed_ref"],
                                "valid_to": None,
                            },
                            "effective_at": args.effective_at.isoformat(),
                            "reason": reason,
                        }
                    ],
                },
            ),
            context,
        )
        final_proof = proof_for(
            (*checks, prepared.history_check)
            if prepared.history_check is not None else tuple(checks)
        )
        if not final_proof.valid():
            raise AssistantRuntimeError("Residence transfer evidence changed after preparation")
        material = prepared.model_content
        if prepared.module_result is not None:
            material = {
                **material,
                "preflight_scope": {
                    "effective_at": args.effective_at.isoformat(),
                    "current_occupancy_queried": False,
                    "executed": False,
                    "meaning": (
                        "本结果核验的是拟议安排在所填生效时间起的发布条件，尚未执行。"
                        "它没有查询目标床位此刻的入住情况，不能据此说床位当前空闲。"
                    ),
                },
            }
        if mismatches and prepared.module_result is not None:
            material = {
                **material,
                "original_name_review": {
                    "status": "explicit_review_required",
                    "supplied": mismatches,
                    "formal": original,
                    "message": "名称差异已写入正式待确认方案；核对原住宿后才能明确确认。尚未修改。",
                },
            }
        continuation = prepared.continuation
        if continuation is not None and prepared.module_result is not None:
            original_continuation = continuation
            student_ref = coordinates["student_ref"]
            effective_at = args.effective_at.isoformat()

            def load_transfer_result() -> ContinuationEvidence:
                evidence = original_continuation.load()
                if evidence.fact_card.get("status") != "succeeded":
                    return evidence
                result_material = deepcopy(evidence.model_content)
                result_material["readback_scope"] = {
                    "effective_at": effective_at,
                    "queried": False,
                    "meaning": (
                        "原回执已证明本次操作成功；以下只保留刚确认任务的学生和生效时间，"
                        "不是当前安排或届时安排的查询结果。请分别读取当前及生效时点的正式资料，"
                        "说明现在与届时各是什么安排；读取受限或失败时如实说明，不重做原操作。"
                    ),
                }
                tool_context = cast(JsonObject, result_material["tool_context"])
                tool_context["read_after_confirmation"] = [
                    {
                        "scope": "current",
                        "tool": MODULE + ".query",
                        "arguments": {
                            "fact_kind": "student_bed_residence", "related_ref": student_ref,
                        },
                    },
                    {
                        "scope": "effective_time",
                        "tool": MODULE + ".query",
                        "arguments": {
                            "fact_kind": "student_bed_residence", "related_ref": student_ref,
                            "as_of": effective_at,
                        },
                    },
                ]
                # A completed change invalidates the old preview. Keep only its
                # task coordinates; new facts still come from controlled queries.
                # The original receipt's current authorization remains mandatory.
                return replace(evidence, model_content=result_material)

            continuation = replace(original_continuation, load=load_transfer_result)
        return replace(
            prepared, model_content=material, history_check=final_proof,
            continuation=continuation,
        )

    def _locate_change(self, call: Any, context: Any) -> ToolExecutionResult:
        try:
            args = ResidenceLocateChangeInput.model_validate_json(json.dumps(call.arguments))
        except ValidationError:
            return ToolExecutionResult(
                model_content={
                    "status": "unsupported_selection",
                    "message": "请提供正式学号和含时区的事实时点；目标房床名称未确定可先省略。",
                }
            )
        identity = self.identity(context.actor_context_ref)
        checks: list[HistoryCheck] = []
        time_material: dict[str, Any] = {}
        review_deadlines: list[datetime] = []
        clock = getattr(getattr(self.residence_query, "repository", None), "clock", None)
        if not callable(clock):
            def current_clock() -> datetime:
                return datetime.now(UTC)

            clock = current_clock

        def quality_issue(rows: list[dict[str, Any]]) -> tuple[str, str] | None:
            disputed = unavailable = False
            for row in rows:
                evidence = row.get("supporting_evidence")
                if evidence is None:
                    continue
                if not isinstance(evidence, dict):
                    unavailable = True
                    continue
                quality = evidence.get("quality", [])
                if not isinstance(quality, list):
                    unavailable = True
                    continue
                for item in quality:
                    if not isinstance(item, dict):
                        unavailable = True
                        continue
                    disputed |= item.get("dispute_status") == "open"
                    unavailable |= item.get("source_status") in {
                        "unavailable", "unreachable", "stale"
                    }
                    due_value = item.get("review_due_at")
                    if due_value is None:
                        continue
                    try:
                        if not isinstance(due_value, str):
                            raise ValueError("Invalid quality review time")
                        due = datetime.fromisoformat(due_value)
                        if due.utcoffset() is None:
                            raise ValueError("Quality review time has no zone")
                    except ValueError:
                        unavailable = True
                        continue
                    if due <= clock():
                        unavailable = True
                    else:
                        review_deadlines.append(due)
            if disputed:
                return "conflict", "quality_disputed"
            if unavailable:
                return "partial", "source_quality_unavailable"
            return None

        def result(content: dict[str, Any]) -> ToolExecutionResult:
            if content["status"] != "denied":
                content = {**time_material, **content}
                content["limitations"] = list(
                    dict.fromkeys(
                        [
                            *time_material.get("limitations", []),
                            *content.get("limitations", []),
                        ]
                    )
                )
                if content["status"] == "located":
                    basis = content.get("time_basis")
                    zone_name = content.get("fact_timezone_name")
                    try:
                        if (
                            not isinstance(basis, dict)
                            or basis.get("status") != "available"
                            or not isinstance(zone_name, str)
                        ):
                            raise ValueError("School time basis unavailable")
                        ZoneInfo(zone_name)
                    except (ValueError, ZoneInfoNotFoundError):
                        content = {
                            "status": "partial",
                            "stage": "time_basis",
                            "as_of": args.as_of.isoformat(),
                            "fact_timezone_name": zone_name,
                            "time_basis": basis,
                            "limitations": list(
                                dict.fromkeys(
                                    [*content["limitations"], "school_timezone_unavailable"]
                                )
                            ),
                        }
                content = residence_model_material(content)

            def valid() -> bool:
                return (
                    self.identity(context.actor_context_ref) == identity
                    and all(clock() < deadline for deadline in review_deadlines)
                    and all(check.valid() for check in checks)
                )

            proof = HistoryCheck(valid, revalidate=valid, revalidation_covers_content=True)
            if not proof.valid():
                raise AssistantRuntimeError("Residence locator evidence changed")
            return ToolExecutionResult(
                model_content=content,
                history_check=proof,
                history_execution_note="read_returned_no_values"
                if content["status"] == "denied"
                else None,
            )

        def select_one(stage: str, fact_kind: ResidenceKind, **selection: Any) -> Any:
            original = self.execute(
                ModelToolCall(
                    call_id=call.call_id,
                    name=MODULE + ".query",
                    arguments={
                        "fact_kind": fact_kind,
                        "as_of": args.as_of.isoformat(),
                        "limit": 2,
                        **selection,
                    },
                ),
                context,
            )
            if original.history_check is not None:
                checks.append(original.history_check)
            content = original.model_content
            status = content.get("status")

            def failed(
                reason: str,
                *,
                extra_limitations: tuple[str, ...] = (),
                supporting_evidence: Any = None,
            ) -> ToolExecutionResult:
                if reason == "denied":
                    return result({"status": "denied"})
                detail: dict[str, Any] = {
                    "status": reason,
                    "stage": stage,
                    "as_of": args.as_of.isoformat(),
                }
                for key in ("coverage", "limitations"):
                    if key in content:
                        detail[key] = content[key]
                if extra_limitations:
                    detail["limitations"] = list(
                        dict.fromkeys([*detail.get("limitations", []), *extra_limitations])
                    )
                if supporting_evidence is not None:
                    detail["supporting_evidence"] = supporting_evidence
                return result(detail)

            if status not in {"available", "known"}:
                return failed(
                    status if status in {
                        "denied", "no_current_records", "ambiguous", "conflict",
                        "partial", "incomplete", "unsupported_selection", "unavailable",
                    } else "unavailable"
                )
            rows = content.get("records")
            if not isinstance(rows, list):
                return failed("unavailable")
            if len(rows) > 1:
                return failed("ambiguous")
            if content.get("truncated") or content.get("has_more"):
                return failed("incomplete")
            if len(rows) != 1:
                return failed("no_current_records")
            issue = quality_issue(rows)
            if issue is not None:
                return failed(
                    issue[0],
                    extra_limitations=(issue[1],),
                    supporting_evidence=rows[0].get("supporting_evidence"),
                )
            return content, rows[0]

        def list_candidates(
            stage: str, fact_kind: ResidenceKind, name_field: str,
            related_ref: str, original: dict[str, Any],
        ) -> ToolExecutionResult:
            response = self.execute(
                ModelToolCall(
                    call_id=call.call_id,
                    name=MODULE + ".query",
                    arguments={
                        "fact_kind": fact_kind,
                        "as_of": args.as_of.isoformat(),
                        "related_ref": related_ref,
                        "limit": 10,
                    },
                ),
                context,
            )
            if response.history_check is not None:
                checks.append(response.history_check)
            content = response.model_content
            status = content.get("status")
            if status == "denied":
                return result({"status": "denied"})
            detail: dict[str, Any] = {
                "stage": stage,
                "as_of": args.as_of.isoformat(),
                "original": original,
            }
            for key in ("coverage", "limitations"):
                if key in content:
                    detail[key] = content[key]
            if status not in {"available", "known"}:
                detail["status"] = status if status in {
                    "no_current_records", "ambiguous", "conflict", "partial", "incomplete",
                    "unavailable",
                } else "unavailable"
                return result(detail)
            rows = content.get("records")
            if not isinstance(rows, list) or content.get("truncated") or content.get("has_more"):
                detail["status"] = "incomplete"
            elif not rows:
                detail["status"] = "no_current_records"
            elif issue := quality_issue(rows):
                detail["status"] = issue[0]
                detail["limitations"] = list(
                    dict.fromkeys([*detail.get("limitations", []), issue[1]])
                )
            elif any(not isinstance(row.get(name_field), str) for row in rows):
                detail["status"] = "incomplete"
            elif len({row[name_field] for row in rows}) != len(rows):
                detail["status"] = "ambiguous"
            else:
                detail["status"] = stage + "_candidates"
                detail["candidates"] = [{name_field: row[name_field]} for row in rows]
            return result(detail)

        with bind_directory_identity(context.actor_context_ref):
            student = select_one(
                "student", "student_school_relationship", student_number=args.student_number
            )
            if isinstance(student, ToolExecutionResult):
                return student
            student_ref = student[1].get("fields", {}).get("student_ref")
            if not isinstance(student_ref, str) or not (
                student_ref.startswith("f3:student:")
                or school_scope_reference_coordinates(student_ref) == ("f3", "student", False)
            ):
                return result({"status": "incomplete", "stage": "student"})
            old = select_one("original_residence", "student_bed_residence", related_ref=student_ref)
            if isinstance(old, ToolExecutionResult):
                return old
            time_material.update(
                {
                    key: old[0][key]
                    for key in ("fact_timezone_name", "time_basis", "time_labels", "limitations")
                    if key in old[0]
                }
            )
            old_row = old[1]
            if any(
                not isinstance(old_row.get(field), str) or not old_row[field].strip()
                for field in ("building_name", "room_name", "bed_name")
            ):
                return result({"status": "incomplete", "stage": "original_residence"})
            old_names = {
                "building_name": old_row["building_name"],
                "room_name": old_row["room_name"],
                "bed_name": old_row["bed_name"],
            }
            building = select_one(
                "building", "dormitory_building_profile", name=old_row["building_name"]
            )
            if isinstance(building, ToolExecutionResult):
                return building
            building_ref = building[0].get("tool_context", {}).get("targets", [{}])[0].get(
                "reference"
            )
            if not isinstance(building_ref, str):
                return result({"status": "incomplete", "stage": "building"})
            original_room = select_one(
                "original_room", "dorm_room", name=old_row["room_name"],
                related_ref=building_ref,
            )
            if isinstance(original_room, ToolExecutionResult):
                return original_room
            original_room_ref = (
                original_room[0].get("tool_context", {}).get("targets", [{}])[0].get("reference")
            )
            original_bed_ref = (
                old[0].get("tool_context", {}).get("targets", [{}])[0].get("bed_ref")
            )
            if not isinstance(original_room_ref, str) or not isinstance(original_bed_ref, str):
                return result({"status": "incomplete", "stage": "original_bed"})
            original_bed = select_one(
                "original_bed", "dorm_bed", reference=original_bed_ref,
                related_ref=original_room_ref,
            )
            if isinstance(original_bed, ToolExecutionResult):
                return original_bed
            if "display_name" not in original_bed[1]:
                return result({"status": "incomplete", "stage": "original_bed"})
            if original_bed[1].get("display_name") != old_row["bed_name"]:
                return result({"status": "object_mismatch", "stage": "original_bed"})
            if args.to_room_name is None:
                return list_candidates(
                    "target_room", "dorm_room", "room_name", building_ref, old_names
                )
            room = select_one(
                "target_room", "dorm_room", name=args.to_room_name, related_ref=building_ref
            )
            if isinstance(room, ToolExecutionResult):
                if room.model_content.get("status") == "no_current_records":
                    return list_candidates(
                        "target_room", "dorm_room", "room_name", building_ref, old_names
                    )
                return room
            room_ref = room[0].get("tool_context", {}).get("targets", [{}])[0].get(
                "reference"
            )
            if not isinstance(room_ref, str):
                return result({"status": "incomplete", "stage": "target_room"})
            if args.to_bed_name is None:
                return list_candidates(
                    "target_bed", "dorm_bed", "display_name", room_ref, old_names
                )
            bed = select_one(
                "target_bed", "dorm_bed", name=args.to_bed_name, related_ref=room_ref
            )
            if isinstance(bed, ToolExecutionResult):
                if bed.model_content.get("status") == "no_current_records":
                    return list_candidates(
                        "target_bed", "dorm_bed", "display_name", room_ref, old_names
                    )
                return bed
            original_ref = old[0].get("tool_context", {}).get("targets", [{}])[0].get(
                "reference"
            )
            bed_ref = bed[0].get("tool_context", {}).get("targets", [{}])[0].get(
                "reference"
            )
            if not isinstance(original_ref, str) or not isinstance(bed_ref, str):
                return result({"status": "incomplete", "stage": "change_coordinates"})
            limitations = list(
                dict.fromkeys(
                    [
                        *old[0].get("limitations", []),
                        *bed[0].get("limitations", []),
                    ]
                )
            )
            return result({
                "status": "located",
                "as_of": args.as_of.isoformat(),
                "original": old_names,
                "target": {
                    "building_name": old_row["building_name"],
                    "room_name": room[1].get("room_name"),
                    "bed_name": bed[1].get("display_name"),
                },
                "coverage": {
                    "original_residence": old[0].get("coverage"),
                    "target_bed": bed[0].get("coverage"),
                },
                "supporting_evidence": {
                    "original_residence": old_row.get("supporting_evidence"),
                    "target_bed": bed[1].get("supporting_evidence"),
                },
                "limitations": limitations,
                "tool_context": {
                    "student_ref": student_ref,
                    "original_residence_ref": original_ref,
                    "original_building_ref": building_ref,
                    "original_room_ref": original_room_ref,
                    "original_bed_ref": original_bed_ref,
                    "target_bed_ref": bed_ref,
                },
                "next_step": (
                    "这只是获权对象定位；还需获准创建项和正式prepare完整预检，"
                    "没有执行修改。"
                ),
            })

    def execute(self, call: Any, context: Any) -> Any:
        if call.name == MODULE + ".prepare_transfer":
            return self._prepare_transfer(
                call,
                context,
                prepare_execute=super().for_turn(context.actor_context_ref).execute,
            )
        if call.name == MODULE + ".locate_change":
            return self._locate_change(call, context)
        if call.name not in {MODULE + ".query", MODULE + ".describe"}:
            return super().execute(call, context)
        identity = self.identity(context.actor_context_ref)
        try:
            model = (
                ResidenceDescribeInput if call.name.endswith(".describe") else ResidenceQueryInput
            )
            args: Any = model.model_validate_json(json.dumps(call.arguments))
        except ValidationError:
            return _selection_failure(call.arguments, describe=call.name.endswith(".describe"))
        if args.fact_kind != "student_school_relationship" and (
            getattr(args, "related_field", None) is not None
            or getattr(args, "student_number", None) is not None
        ):
            return _selection_failure(call.arguments)
        with bind_directory_identity(context.actor_context_ref):
            if call.name.endswith(".describe"):
                try:
                    view = self.describe_adapter.describe(actor=identity.actor, **args.model_dump())
                except PermissionError:
                    return ToolExecutionResult(
                        model_content={"status": "denied"},
                        history_execution_note="read_returned_no_values",
                    )

                def current_description() -> Any:
                    with bind_directory_identity(context.actor_context_ref):
                        return (
                            self.identity(context.actor_context_ref) == identity
                            and self.describe_adapter.describe(
                                actor=identity.actor, **args.model_dump()
                            )
                            == view
                        )

                return ToolExecutionResult(
                    model_content=deepcopy(view),
                    fact_card=view,
                    history_check=HistoryCheck(
                        current_description,
                        revalidate=current_description,
                        revalidation_covers_content=True,
                    ),
                )
            if args.fact_kind in {"fact_quality", "student_school_relationship"}:
                adapter = (
                    self.quality_adapter
                    if args.fact_kind == "fact_quality"
                    else self.eligibility_adapter
                )
                if adapter is None:
                    return ToolExecutionResult(model_content={"status": "unavailable"})
                if args.name is not None and args.fact_kind == "fact_quality":
                    return _selection_failure(call.arguments)
                selection = args.model_dump(
                    exclude={"fact_kind", "name", "related_field", "student_number"}
                )
                if args.fact_kind == "student_school_relationship":
                    selection["name"] = args.name
                    selection["related_field"] = args.related_field
                    selection["student_number"] = args.student_number

                def read_dependent_view() -> Any:
                    value = adapter.query(actor=identity.actor, **selection)
                    decorate = getattr(self.describe_adapter, "with_time", None)
                    return (
                        decorate(identity.actor, value, as_of=args.as_of)
                        if callable(decorate)
                        else value
                    )

                try:
                    view = read_dependent_view()
                except PermissionError:
                    return ToolExecutionResult(
                        model_content={"status": "denied"},
                        history_execution_note="read_returned_no_values",
                    )
                except ValueError:
                    return _selection_failure(call.arguments)

                def current_quality() -> Any:
                    with bind_directory_identity(context.actor_context_ref):
                        return self.identity(
                            context.actor_context_ref
                        ) == identity and _view_fingerprint(
                            read_dependent_view()
                        ) == _view_fingerprint(view)

                return ToolExecutionResult(
                    model_content=answer_evidence_material(residence_model_material(view)),
                    fact_card=view,
                    history_check=HistoryCheck(
                        current_quality,
                        revalidate=current_quality,
                        revalidation_covers_content=True,
                    ),
                )
            result = self.residence_query.query(
                actor=identity.actor,
                **args.model_dump(exclude={"related_field", "student_number"}),
            )

        def current() -> Any:
            def fresh() -> Any:
                latest = self.identity(context.actor_context_ref)
                with bind_directory_identity(context.actor_context_ref):
                    return latest == identity and result.evidence.valid(latest.actor)

            return validate_fallback_read(
                (MODULE, context.actor_context_ref, identity.actor, id(result.evidence)),
                fresh,
                time_window=getattr(result.evidence, "time_window", None),
            )

        return _public_result(result, valid=current)


class _ResidenceTurn:
    def __init__(self, owner: Any, reference: str, retained: Any) -> None:
        self.owner, self.reference, self.retained = owner, reference, retained
        self._checks: list[Any] = []

    @property
    def descriptors(self) -> Any:
        return self.owner.descriptors

    def revalidate(self, reference: str) -> None:
        self.retained.revalidate(reference)
        if reference != self.reference or any(not check() for check in self._checks):
            raise AssistantRuntimeError("Residence evidence changed")

    def execute(self, call: Any, context: Any) -> Any:
        if call.name not in {
            MODULE + ".query", MODULE + ".describe", MODULE + ".locate_change",
            MODULE + ".prepare_transfer",
        }:
            return self.retained.execute(call, context)
        self.revalidate(context.actor_context_ref)
        result = (
            self.owner._prepare_transfer(
                call, context, prepare_execute=self.retained.execute
            )
            if call.name == MODULE + ".prepare_transfer"
            else self.owner.execute(call, context)
        )
        if result.history_check is not None:
            self._checks.append(result.history_check.valid)
        return result
