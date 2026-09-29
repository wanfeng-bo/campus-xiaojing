from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from threading import RLock
from typing import Annotated, Literal, Protocol
from uuid import uuid4

from pydantic import Field, StringConstraints, ValidationError, model_validator

from app.composition.school_fact_answer_evidence import answer_evidence_material
from app.composition.school_fact_product_contacts import (
    ContactChangeInput,
    ContactProductUnavailable,
    ContactReceipt,
    OfficialContactProductService,
    contact_query_view,
    model_contact_query_view,
)
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
from app.platform.assistant.runtime import (
    AssistantRuntimeError,
    ContinuationEvidence,
    HistoryCheck,
    ResultContinuation,
)
from app.platform.assistant.tool_loop import (
    ModelToolCall,
    ToolCallContext,
    ToolDescriptor,
    ToolExecutionResult,
)
from app.platform.contracts import FrozenContractModel, StaffActorContext
from app.platform.school_facts.read_policy import (
    OFFICIAL_CONTACT_MODULE,
    OfficialContactQueryResult,
)


class ProductActor(Protocol):
    @property
    def actor(self) -> StaffActorContext: ...
    @property
    def session_binding(self) -> str: ...


def fact_result_continuation(
    reference: str, read: Callable[[], tuple[JsonObject, JsonObject]]
) -> ResultContinuation:
    """Keep only a read callback; revalidate the exact original receipt on every release."""
    def load() -> ContinuationEvidence:
        material, card = read()
        if card.get("status") not in {"succeeded", "rejected"}:
            # A read of an unresolved original receipt is not the final result
            # event. Leave its one-shot continuation unclaimed until the same
            # controlled receipt path establishes the authoritative outcome.
            raise AssistantRuntimeError("original execution result is not terminal")
        expected = deepcopy((material, card))

        def current() -> bool:
            return read() == expected

        return ContinuationEvidence(
            model_content=deepcopy(material), fact_card=deepcopy(card),
            history_check=HistoryCheck(
                current, revalidate=current, revalidation_covers_content=True,
            ),
        )

    return ResultContinuation(reference=reference, load=load)


class QueryRepository(Protocol):
    def read_official_contacts(
        self,
        *,
        actor: StaffActorContext,
        campus_name: str | None = None,
        campus_ref: str | None = None,
        subject: Literal["official_contacts", "campuses"] = "official_contacts",
        scope: Literal["selected", "all_authorized"] = "selected",
    ) -> OfficialContactQueryResult: ...


class ContactQueryInput(FrozenContractModel):
    subject: Literal["official_contacts", "campuses"] = Field(
        default="official_contacts",
        description="official_contacts查询校区直接所属联系；campuses列出有效校区记录，不证明已录入联系。",
    )
    scope: Literal["selected", "all_authorized"] = Field(
        default="selected",
        description=(
            "selected按给定校区名或引用解析；all_authorized明确查询当前身份全部获准校区，"
            "不得再给校区名或引用。用户明确改为总体列表时不要沿用旧校区限制。"
        ),
    )
    campus_name: (
        Annotated[
            str, StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=100)
        ]
        | None
    ) = Field(
        default=None,
        description=(
            "按名称选择校区时填写；与campus_ref二选一，不能同时提供。"
            "已有返回的校区引用时省略本字段；all_authorized时名称与引用均省略。"
        ),
    )
    campus_ref: Annotated[str, StringConstraints(strict=True, max_length=100)] | None = Field(
        default=None,
        description=(
            "按已返回的校区引用选择时填写；与campus_name二选一，不能同时提供。"
            "使用本字段时省略campus_name；all_authorized时名称与引用均省略。"
        ),
    )

    @model_validator(mode="after")
    def exclusive_selection(self):
        if self.campus_name is not None and self.campus_ref is not None:
            raise ValueError("choose campus name or a returned campus reference")
        if self.scope == "all_authorized" and (
            self.campus_name is not None or self.campus_ref is not None
        ):
            raise ValueError("an all-authorized query cannot also select one campus")
        return self


class ContactReceiptInput(FrozenContractModel):
    proposal_ref: (
        Annotated[str, StringConstraints(strict=True, pattern=r"^sf3plan_[A-Za-z0-9_-]{20,96}$")]
        | None
    ) = None


_QUERY = OFFICIAL_CONTACT_MODULE + ".query"
_PREPARE = OFFICIAL_CONTACT_MODULE + ".prepare"
_RECEIPT = OFFICIAL_CONTACT_MODULE + ".receipt"


def _descriptor(
    name: str, kind: str, description: str, schema: type[FrozenContractModel]
) -> ToolDescriptor:
    return ToolDescriptor.model_validate(
        {
            "name": name,
            "owner": OFFICIAL_CONTACT_MODULE,
            "kind": kind,
            "description": description,
            "parameters": schema.model_json_schema(),
        }
    )


@dataclass(frozen=True, slots=True)
class _RecentReceipt:
    reference: str
    expires_at: datetime


class OfficialContactToolGateway:
    """Product-owned capability adapter; the assistant kernel knows no school rules."""

    descriptors = (
        _descriptor(
            _QUERY,
            "read",
            "查询合成工程学校中已发布、当前有效的校区直接所属官方联系，或列出获准查询的校区记录。"
            "subject选择联系或校区，scope区分指定校区与全部获准校区；校区列表不证明已有联系方式。"
            "只支持校区直接联系，不支持部门、值班岗位、下级地点的专属联系或地点导航，不能把校区联系当作它们。"
            "selection_required表示尚未指定校区，不自动选唯一候选；unmatched表示请求校区未匹配。"
            "no_current_records只表示本次范围无当前可返回记录，可能涉及时间或发布过滤，不证明尚未录入或现实不存在。"
            "同名候选先用名称、路径和引用辨认；需要对照联系时只查询所需候选的引用，"
            "可以明确归属地比较，但不能替用户决定唯一目标，也不能为消歧默扩为全校联系。"
            "denied表示当前权限下请求被拒绝；权限未变化时不要反复换名称、引用或目录路径尝试同一目标，"
            "也不要要求用户提供另一参数来重试这个被拒目标。"
            "本工具只返回查询时当前有效的发布记录，不含尚未生效的计划；"
            "返回记录的valid_from、valid_to不能证明过去或未来时点的有效完整名单。"
            "对话最多显示10项，过长结果还会缩减。"
            "数量仅按listing解释，截断或匹配数未知不能把当前展示条数说成总数；display_limited仅表示无法显示，不表示无记录。"
            "来源覆盖与本次列表数量独立，coverage未知不能说现实全校只有这些；变更对象或范围须重新查询。",
            ContactQueryInput,
        ),
        _descriptor(
            _PREPARE,
            "prepare",
            "为已查询到的官方联系方式提出修改：必须给返回的联系引用、新值、服务时间和修改原因。"
            "服务时间只传实际值或null；未要求变更时照抄最新读取值，未知或未录入传null，"
            "不得把「保持原样」「不填」等操作说明写入事实字段。"
            "此工具只预检并生成供用户检查的候选，绝不执行；正式修改必须由用户点击界面上的确认控件。",
            ContactChangeInput,
        ),
        _descriptor(
            _RECEIPT,
            "result",
            "查询正式修改的权威回执。可以提供候选引用；省略时查询当前身份在本对话最近准备的候选。"
            "没有可用引用时会要求澄清；结果未知时只查询，不重新准备或执行修改。",
            ContactReceiptInput,
        ),
    )

    def __init__(
        self,
        *,
        resolve_actor: Callable[[str], ProductActor],
        require_read: Callable[[StaffActorContext], object],
        query: QueryRepository,
        contacts: OfficialContactProductService,
        receipt_retention_seconds: int,
        max_recent_receipts: int = 128,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._resolve = resolve_actor
        self._require_read = require_read
        self._query = query
        self._contacts = contacts
        if type(receipt_retention_seconds) is not int or receipt_retention_seconds < 1:
            raise ValueError("Receipt retention must be explicitly configured.")
        if type(max_recent_receipts) is not int or max_recent_receipts < 1:
            raise ValueError("Recent receipt capacity must be positive.")
        self._receipt_retention_seconds = receipt_retention_seconds
        self._max_recent_receipts = max_recent_receipts
        self._clock = clock or (lambda: datetime.now(UTC))
        self._recent_receipts: dict[tuple[str, str], _RecentReceipt] = {}
        self._receipt_lock = RLock()

    def for_turn(self, actor_context_ref: str) -> _OfficialContactTurnGateway:
        return _OfficialContactTurnGateway(self, actor_context_ref, self._revalidate)

    def revalidate(self, actor_context_ref: str) -> None:
        self._revalidate(actor_context_ref, require_fact_read=True)

    def _revalidate(self, actor_context_ref: str, require_fact_read: bool) -> None:
        try:
            identity = self._resolve(actor_context_ref)
            if require_fact_read:
                self._require_read(identity.actor)
        except Exception:
            raise AssistantRuntimeError("current context unavailable") from None

    def require_receipt_access(self, actor_context_ref: str) -> None:
        try:
            identity = self._resolve(actor_context_ref)
            self._contacts.require_receipt_access(identity.actor)
        except Exception:
            raise AssistantRuntimeError("current receipt authorization unavailable") from None

    def execute(self, call: ModelToolCall, context: ToolCallContext) -> ToolExecutionResult:
        identity = self._resolve(context.actor_context_ref)
        try:
            if call.name == _QUERY:
                # The real Reader owns the denial result; do not preempt its decision.
                try:
                    selection = ContactQueryInput.model_validate(call.arguments)
                except ValidationError:
                    has_name = call.arguments.get("campus_name") is not None
                    has_ref = call.arguments.get("campus_ref") is not None
                    if has_name and has_ref:
                        reason_code = "query_selector_conflict"
                        reason = (
                            "campus_name与campus_ref只能提供一个；已有返回的校区引用时，"
                            "只保留campus_ref并省略campus_name。此参数错误不需要重新读取事实。"
                        )
                    elif call.arguments.get("scope") == "all_authorized" and (has_name or has_ref):
                        reason_code = "query_scope_conflict"
                        reason = (
                            "all_authorized不能同时指定校区名称或引用；若请求仍针对指定校区，"
                            "使用selected并只提供一个校区选择条件。"
                        )
                    else:
                        reason_code = "invalid_query_arguments"
                        reason = (
                            "查询参数不符合工具合同；请核对字段类型和允许值后修正。本次未读取事实。"
                        )
                    return ToolExecutionResult(
                        model_content={
                            "status": "rejected",
                            "reason_code": reason_code,
                            "reason": reason,
                        }
                    )
                result = self._query.read_official_contacts(
                    actor=identity.actor,
                    campus_name=selection.campus_name,
                    campus_ref=selection.campus_ref,
                    subject=selection.subject,
                    scope=selection.scope,
                )
                view = model_contact_query_view(contact_query_view(result))
                public = view.model_dump(mode="json")
                history: JsonObject = {
                    key: public[key]
                    for key in (
                        "status",
                        "query",
                        "candidates",
                        "resolved_campuses",
                        "listing",
                        "display_notice",
                    )
                }
                history["items"] = [
                    {
                        "reference": item.reference,
                        "campus_ref": item.campus_ref,
                        "campus_name": item.campus_name,
                        "contact_kind": item.contact_kind,
                    }
                    for item in view.items
                ]
                return ToolExecutionResult(
                    model_content=answer_evidence_material(public),
                    fact_card=public,
                    history_content=history,
                )
            if call.name == _PREPARE:
                self._require_read(identity.actor)
                proposal = self._contacts.prepare(
                    actor=identity.actor,
                    session_binding=identity.session_binding,
                    payload=ContactChangeInput.model_validate(call.arguments),
                    request_id=context.request_id,
                )
                self._remember_receipt(context, proposal.proposal_ref)
                public = proposal.model_dump(mode="json")
                return ToolExecutionResult(
                    model_content={"status": "awaiting_explicit_confirmation", **public},
                    fact_card=public,
                    continuation=self._continuation(
                        context, identity, proposal.proposal_ref,
                        campus_ref=proposal.summary.before.campus_ref,
                        contact_ref=proposal.summary.before.reference,
                    ),
                    module_result=self._result(
                        proposal.proposal_ref,
                        revision=1,
                        payload=public,
                        action=True,
                    ),
                )
            if call.name == _RECEIPT:
                self._require_read(identity.actor)
                query = ContactReceiptInput.model_validate(call.arguments)
                reference = query.proposal_ref or self._recent_receipt(context)
                if reference is None:
                    return ToolExecutionResult(
                        model_content={
                            "status": "clarification_required",
                            "reason": (
                                "本对话当前没有可核对的候选引用，请提供原候选或回执引用；"
                                "不要重新发起修改。"
                            ),
                        }
                    )
                receipt = self._contacts.receipt(
                    actor=identity.actor,
                    session_binding=identity.session_binding,
                    proposal_ref=reference,
                    request_id=context.request_id,
                )
                public = receipt.model_dump(mode="json")
                return ToolExecutionResult(model_content=public, fact_card=public)
            raise AssistantRuntimeError("capability not installed")
        except (ValueError, ContactProductUnavailable):
            return ToolExecutionResult(
                model_content={
                    "status": "rejected",
                    "reason": "输入无效、事实已变化或候选不可用；请查询当前事实后澄清。",
                }
            )
        except PermissionError:
            return ToolExecutionResult(
                model_content={"status": "denied", "reason": "当前履职身份未获准此操作。"}
            )

    def _continuation(
        self, context: ToolCallContext, original: ProductActor, reference: str,
        *, campus_ref: str, contact_ref: str,
    ) -> ResultContinuation:
        def read() -> tuple[JsonObject, JsonObject]:
            identity = self._resolve(context.actor_context_ref)
            if (
                identity.actor != original.actor
                or identity.session_binding != original.session_binding
            ):
                raise AssistantRuntimeError("original receipt identity changed")
            self._require_read(identity.actor)
            self._contacts.require_invoked(
                actor=identity.actor, session_binding=identity.session_binding,
                proposal_ref=reference,
            )
            receipt = self._contacts.receipt(
                actor=identity.actor, session_binding=identity.session_binding,
                proposal_ref=reference, request_id=context.request_id,
            )
            view = receipt.model_dump(mode="json")
            material = deepcopy(view)
            if receipt.status == "succeeded":
                # Stable original selectors carry no current fact/permission claim.
                # A model still has to query and match the exact contact reference.
                material["tool_context"] = {
                    "original_contact_ref": contact_ref,
                    "read_current": {
                        "tool": _QUERY, "arguments": {"campus_ref": campus_ref},
                    },
                }
            return material, view

        return fact_result_continuation(reference, read)

    def _remember_receipt(self, context: ToolCallContext, reference: str) -> None:
        key = (context.actor_context_ref, context.conversation_ref)
        with self._receipt_lock:
            self.purge_expired()
            self._recent_receipts.pop(key, None)
            if len(self._recent_receipts) >= self._max_recent_receipts:
                self._recent_receipts.pop(next(iter(self._recent_receipts)))
            self._recent_receipts[key] = _RecentReceipt(
                reference, self._clock() + timedelta(seconds=self._receipt_retention_seconds)
            )

    def _recent_receipt(self, context: ToolCallContext) -> str | None:
        with self._receipt_lock:
            self.purge_expired()
            entry = self._recent_receipts.get((context.actor_context_ref, context.conversation_ref))
            return None if entry is None else entry.reference

    def purge_expired(self, *, now: datetime | None = None) -> int:
        observed = now or self._clock()
        with self._receipt_lock:
            expired = [
                key for key, item in self._recent_receipts.items() if item.expires_at <= observed
            ]
            for key in expired:
                del self._recent_receipts[key]
            return len(expired)

    def handle(self, request: ModuleCallV1) -> ModuleResultV1:
        raise AssistantRuntimeError("this capability uses the controlled tool contract")

    @staticmethod
    def _proposal(private_ref: str, prefix: str) -> str:
        if not private_ref.startswith(prefix):
            raise ContactProductUnavailable("invalid action reference")
        reference = ContactReceiptInput(proposal_ref=private_ref.removeprefix(prefix)).proposal_ref
        assert reference is not None
        return reference

    def confirm(self, request: ModuleConfirmationCallV1) -> ModuleResultV1:
        ref = self._proposal(request.action_offer_ref, "action_")
        if (
            request.module_ref != OFFICIAL_CONTACT_MODULE
            or request.module_state_ref != "state_" + ref
            or request.state_revision != 1
        ):
            raise ContactProductUnavailable("invalid confirmation binding")
        identity = self._resolve(request.actor_context_ref)
        receipt = self._contacts.confirm(
            actor=identity.actor,
            session_binding=identity.session_binding,
            proposal_ref=ref,
            request_id=uuid4(),
        )
        return self._receipt_result(receipt)

    def recover(self, request: ModuleRecoveryCallV1) -> ModuleResultV1:
        raise ContactProductUnavailable("this fact action has no recovery mutation")

    def check_result(self, request: ModuleResultCheckCallV1) -> ModuleResultV1:
        if request.module_ref != OFFICIAL_CONTACT_MODULE:
            raise ContactProductUnavailable("invalid result binding")
        ref = self._proposal(request.result_check_ref, "receipt_")
        identity = self._resolve(request.actor_context_ref)
        receipt = self._contacts.receipt(
            actor=identity.actor,
            session_binding=identity.session_binding,
            proposal_ref=ref,
            request_id=uuid4(),
        )
        return self._receipt_result(receipt)

    @classmethod
    def _receipt_result(cls, receipt: ContactReceipt) -> ModuleResultV1:
        return cls._result(
            receipt.reference,
            revision=2,
            payload=receipt.model_dump(mode="json"),
            check=receipt.status == "unknown",
        )

    @staticmethod
    def _result(
        ref: str, *, revision: int, payload: JsonObject, action: bool = False, check: bool = False
    ) -> ModuleResultV1:
        return ModuleResultV1(
            version="1",
            validation_receipt=ValidationReceiptV1(
                status="accepted", safe_result_family=OFFICIAL_CONTACT_MODULE
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
                action_offer_ref="action_" + ref, safe_label="确认以上官方联系方式修改"
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


class _OfficialContactTurnGateway:
    """One accepted run's read boundary; no protection state is shared across runs."""

    def __init__(
        self,
        owner: OfficialContactToolGateway,
        actor_context_ref: str,
        revalidate_actor: Callable[[str, bool], None],
    ) -> None:
        self._owner = owner
        self._revalidate_actor = revalidate_actor
        self._actor_context_ref = actor_context_ref
        self._protected_facts_released = False
        self._protected_receipt_released = False

    @property
    def descriptors(self) -> tuple[ToolDescriptor, ...]:
        return self._owner.descriptors

    def revalidate(self, actor_context_ref: str) -> None:
        if actor_context_ref != self._actor_context_ref:
            raise AssistantRuntimeError("current context unavailable")
        self._revalidate_actor(actor_context_ref, self._protected_facts_released)
        if self._protected_receipt_released:
            self._owner.require_receipt_access(actor_context_ref)

    def execute(self, call: ModelToolCall, context: ToolCallContext) -> ToolExecutionResult:
        self.revalidate(context.actor_context_ref)
        result = self._owner.execute(call, context)
        if self._contains_protected_facts(call, result):
            # Monotonic for this run. The loop checks again before accepting the result,
            # before each later tool/model release, control issuance, and answer settlement.
            self._protected_facts_released = True
        if call.name == _RECEIPT and result.fact_card is not None:
            self._protected_receipt_released = True
        if call.name in {_QUERY, _RECEIPT}:
            expected = self._history_digest(result.model_content)
            saved_call = call.model_copy(deep=True)

            def still_current() -> bool:
                # Re-read through the same domain authorization, never repeat a prepare.
                self.revalidate(context.actor_context_ref)
                actual = self._owner.execute(saved_call, context)
                self.revalidate(context.actor_context_ref)
                return self._history_digest(actual.model_content) == expected

            def still_authorized() -> bool:
                # Current evidence was freshly read before disclosure. Subsequent
                # boundaries recheck permission, as for other current tool results;
                # they do not claim an atomic snapshot spanning a model response.
                self.revalidate(context.actor_context_ref)
                return True

            binding = (
                context.actor_context_ref
                + saved_call.name
                + json.dumps(saved_call.arguments, sort_keys=True, ensure_ascii=False)
                + expected
            )
            result = replace(
                result,
                history_check=HistoryCheck(
                    still_current,
                    revalidate=still_authorized,
                    key=hashlib.sha256(binding.encode()).hexdigest(),
                ),
            )
        return result

    @staticmethod
    def _history_digest(content: JsonObject) -> str:
        # Query observation time changes each read; all object, permission-projected
        # values, versions, validity, scope, coverage and ordering remain significant.
        stable = {key: value for key, value in content.items() if key != "queried_at"}
        return hashlib.sha256(
            json.dumps(stable, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()

    @staticmethod
    def _contains_protected_facts(call: ModelToolCall, result: ToolExecutionResult) -> bool:
        if call.name == _QUERY:
            public = result.model_content
            if public.get("status") in ("denied", "source_unavailable"):
                # A denial must carry no contact values or campus candidates to qualify.
                return bool(
                    public.get("items")
                    or public.get("candidates")
                    or public.get("resolved_campuses")
                )
        # This includes not_recorded with a resolved campus, ambiguity, proposals and
        # authoritative receipts. Input errors without a fact card release no facts.
        return result.fact_card is not None or result.module_result is not None
