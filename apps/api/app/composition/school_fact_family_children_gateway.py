"""Family-owned target selection over unchanged single-child domain readers."""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
from typing import Any, Literal, Protocol, cast

from app.composition.school_fact_directory_gateway import CombinedFactTurnGateway
from app.composition.school_fact_product_identity import (
    ProductFamilyIdentity,
    ProductIdentityRejected,
    SchoolFactIdentitySessionService,
)
from app.composition.school_fact_read_evidence import validation_batch
from app.platform.assistant.contracts import JsonObject
from app.platform.assistant.runtime import AssistantRuntimeError, HistoryCheck
from app.platform.assistant.tool_loop import (
    ModelToolCall,
    ToolCallContext,
    ToolDescriptor,
    ToolExecutionResult,
    ToolGateway,
)

CHILDREN_TOOL = "school_facts.family.children"
FAMILY_CHILDREN_INSTRUCTION = (
    "家长可在同一对话查询当前获准的任一孩子，无需切换页面；孩子引用只是查询目标，不授予权限。"
    "children工具给当前可查孩子的最小标签与child_ref；缺少候选时先核对，不能编造引用。"
    "每次孩子查询显式带目标child_ref。模型根据用户姓名、明确改口和最近对话对象理解目标。"
    "单人指代承接最近明确的对象，复数指代按上下文承接已明确的对象集合。"
    "最近对象按用户最后明确查询或消歧的意图判断，不按工具返回顺序；"
    "同时查询多个孩子后，单人指代无法唯一确定就追问，不能取最后一条工具结果。"
    "有多个合理指代才追问；首次只说我的孩子而有多名候选且无明确上下文时请追问。"
    "不把界面默认孩子或更早对象覆盖当前目标。"
    "一轮可分别查询多个获准孩子，分别保留归属，不拼接字段或将一个孩子的结果说成另一个的。"
    "未安装的业务能力不能由其他事实类型推断，也不为查不到擅自换人。"
)


class _ChildGateway(Protocol):
    descriptors: tuple[ToolDescriptor, ...]

    def for_turn(self, actor_context_ref: str) -> ToolGateway: ...


class _SchoolGateway(_ChildGateway, Protocol):
    def school_read_eligibility(
        self, actor_context_ref: str,
    ) -> Literal["available", "denied", "unavailable"]: ...


class FamilyChildrenToolGateway:
    def __init__(
        self, *, identity: SchoolFactIdentitySessionService,
        gateways: tuple[_ChildGateway, ...], school_tool_names: frozenset[str] = frozenset(),
    ) -> None:
        self.identity, self.gateways = identity, gateways
        self.school_tool_names = school_tool_names
        self.school_gateways: dict[str, _SchoolGateway] = {}
        descriptors: list[ToolDescriptor] = []
        for gateway in gateways:
            for descriptor in gateway.descriptors:
                if descriptor.kind != "read":
                    raise ValueError("Family targets only wrap installed read tools")
                parameters = deepcopy(descriptor.parameters)
                if descriptor.name in school_tool_names:
                    if not callable(getattr(gateway, "school_read_eligibility", None)):
                        raise ValueError("School-scoped family reads require an owner basis check")
                    self.school_gateways[descriptor.name] = cast(_SchoolGateway, gateway)
                else:
                    properties = cast(JsonObject, parameters.setdefault("properties", {}))
                    properties["child_ref"] = {
                        "type": "string", "minLength": 32, "maxLength": 128,
                        "description": (
                            "children工具或先前获权结果给出的孩子引用；按当前问题目标选择。"
                        ),
                    }
                descriptors.append(descriptor.model_copy(update={"parameters": parameters}))
        self.descriptors = (
            ToolDescriptor(name=CHILDREN_TOOL, owner="school_facts.family", kind="read",
                           description="列出当前家长在本校仍获准查询的孩子最小标签和查询引用；不读取孩子资料或名单。多孩查询和指代按这份候选定位。",
                           parameters={"type": "object", "properties": {},
                                       "additionalProperties": False}),
            *descriptors,
        )

    def for_turn(self, actor_context_ref: str) -> _FamilyChildrenTurn:
        return _FamilyChildrenTurn(self, actor_context_ref)


class _FamilyChildrenTurn:
    def __init__(self, owner: FamilyChildrenToolGateway, reference: str) -> None:
        self.owner, self.actor_context_ref = owner, reference
        self._turns: dict[str, CombinedFactTurnGateway] = {}
        self._checks: list[HistoryCheck] = []

    @property
    def descriptors(self) -> tuple[ToolDescriptor, ...]:
        return self.owner.descriptors

    @contextmanager
    def validation_scope(self) -> Generator[None]:
        # Delegates bind each real child separately. The family principal grants
        # no database rows and must never be installed as a database actor.
        with validation_batch():
            yield

    def revalidate(self, reference: str) -> None:
        if reference != self.actor_context_ref:
            raise AssistantRuntimeError("family conversation changed")
        current = self.owner.identity.resolve_assistant_identity(reference)
        if type(current) is not ProductFamilyIdentity:
            raise AssistantRuntimeError("family conversation required")
        for check in self._checks:
            if not check.valid():
                raise AssistantRuntimeError("family target evidence changed")

    def _children(self) -> list[JsonObject]:
        children = self.owner.identity.family_children(self.actor_context_ref)
        return [{"child_ref": item.child_ref, "label": item.label} for item in children]

    def _catalog_result(self, status: str) -> ToolExecutionResult:
        children = self._children()

        def current() -> bool:
            try:
                # Focus is not evidence or authority. Relationship membership and
                # labels are current; a new child also invalidates an empty list.
                def fingerprint(rows: list[JsonObject]) -> set[tuple[Any, Any]]:
                    return {(r["child_ref"], r["label"]) for r in rows}
                return fingerprint(self._children()) == fingerprint(children)
            except ProductIdentityRejected:
                return False

        check = HistoryCheck(current, revalidate=current, revalidation_covers_content=True)
        self._checks.append(check)
        return ToolExecutionResult(model_content=cast(JsonObject, {
                                       "status": status, "children": children}),
                                   history_check=check)

    def _school_target(self, name: str) -> tuple[str | None, str]:
        # Each school directory owner decides its own eligible relationship kind.
        # Checking candidates reads no directory data and never unions child rights.
        gateway = self.owner.school_gateways[name]
        unavailable = False
        for child in self.owner.identity.family_children(self.actor_context_ref):
            try:
                target = self.owner.identity.resolve_family_child(
                    self.actor_context_ref, child.child_ref,
                )
                eligibility = gateway.school_read_eligibility(target.actor_context_ref)
            except ProductIdentityRejected:
                continue
            if eligibility == "available":
                return child.child_ref, "available"
            if eligibility != "denied":
                unavailable = True
        return None, "unavailable" if unavailable else "denied"

    def execute(self, call: ModelToolCall, context: ToolCallContext) -> ToolExecutionResult:
        self.revalidate(context.actor_context_ref)
        if call.name == CHILDREN_TOOL:
            if call.arguments:
                return ToolExecutionResult(model_content={"status": "unsupported_selection"})
            return self._catalog_result("available")
        names = {descriptor.name for descriptor in self.descriptors}
        if call.name not in names:
            raise AssistantRuntimeError("family capability not installed")
        arguments = dict(call.arguments)
        child_ref = arguments.pop("child_ref", None)
        if call.name in self.owner.school_tool_names:
            if child_ref is not None:
                return ToolExecutionResult(model_content={"status": "unsupported_selection"})
            child_ref, status = self._school_target(call.name)
            if child_ref is None:
                return ToolExecutionResult(model_content={"status": status},
                                           history_execution_note="read_returned_no_values")
        elif child_ref is None:
            children = self.owner.identity.family_children(self.actor_context_ref)
            if not children:
                return self._catalog_result("denied")
            if len(children) != 1:
                return self._catalog_result("child_selection_required")
            child_ref = children[0].child_ref
        if not isinstance(child_ref, str):
            return ToolExecutionResult(model_content={"status": "unsupported_selection"})
        try:
            target = self.owner.identity.resolve_family_child(self.actor_context_ref, child_ref)
        except ProductIdentityRejected:
            return ToolExecutionResult(model_content={"status": "denied"},
                                       history_execution_note="read_returned_no_values")
        if child_ref not in self._turns:
            self._turns[child_ref] = CombinedFactTurnGateway(*(
                gateway.for_turn(target.actor_context_ref) for gateway in self.owner.gateways
            ))
        delegate = self._turns[child_ref]
        inner_context = replace(context, actor_context_ref=target.actor_context_ref)
        with delegate.validation_scope():
            result = delegate.execute(ModelToolCall(call_id=call.call_id, name=call.name,
                                                   arguments=arguments), inner_context)
        original_check = result.history_check

        def current() -> bool:
            try:
                latest = self.owner.identity.resolve_family_child(self.actor_context_ref, child_ref)
                if latest != target:
                    return False
                with delegate.validation_scope():
                    return original_check is None or original_check.valid()
            except (ProductIdentityRejected, AssistantRuntimeError):
                return False

        check = HistoryCheck(current, revalidate=current, revalidation_covers_content=True)
        self._checks.append(check)
        material = deepcopy(result.model_content)
        material["subject"] = (
            {"scope": "本校获准学校目录"}
            if call.name in self.owner.school_tool_names
            else {"child_ref": child_ref, "label": target.display_label}
        )
        card = deepcopy(result.fact_card) if result.fact_card is not None else None
        if card is not None:
            card["subject"] = deepcopy(material["subject"])
        return replace(result, model_content=material, fact_card=card, history_check=check)
